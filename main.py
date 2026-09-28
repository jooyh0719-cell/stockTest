import os
import math
import requests
import yfinance as yf

# ----------------------------------------------------
# 1. 환경변수 및 고정 IP 프록시 설정
# ----------------------------------------------------
CLIENT_ID = os.environ.get("TOSS_CLIENT_ID")
CLIENT_SECRET = os.environ.get("TOSS_CLIENT_SECRET")
ENV_ACCOUNT_NO = os.environ.get("TOSS_ACCOUNT_NO")  # GitHub Secrets 등록된 계좌번호
FIXIE_URL = os.environ.get("FIXIE_URL")

# 토스증권 API Base URL (기본값: 실운영, 실패 시 Sandbox 전환)
API_BASE_URLS = [
    "https://openapi.tossinvest.com",          # Production (실운영)
    "https://open-api.tossinvest.com/sandbox"  # Sandbox (테스트)
]

proxies = {"http": FIXIE_URL, "https": FIXIE_URL} if FIXIE_URL else None

PORTFOLIO_CONFIG = {
    "SNDL": {"allocation_ratio": 1.00}
}

def print_my_ip():
    try:
        ip = requests.get('https://api.ipify.org', proxies=proxies, timeout=5).text
        print(f"[Check] 외부 접근 IP: {ip}")
    except Exception as e:
        print(f"❌ IP 조회 실패: {e}")

def get_access_token(base_url):
    url = f"{base_url}/oauth2/token"
    payload = {
        "grant_type": "client_credentials",
        "client_id": CLIENT_ID,
        "client_secret": CLIENT_SECRET
    }
    res = requests.post(url, data=payload, proxies=proxies, timeout=10)
    if res.status_code == 200:
        return res.json().get("access_token")
    else:
        raise Exception(f"토큰 발급 실패 ({res.status_code}): {res.text}")

def get_headers(token, account_no):
    return {
        "Authorization": f"Bearer {token}",
        "x-tossinvest-account": str(account_no),
        "Content-Type": "application/json"
    }

def fetch_valid_account_number(base_url, token):
    """API 응답 또는 환경변수에서 최우선 계좌번호를 확보합니다."""
    if ENV_ACCOUNT_NO:
        print(f"🔑 [환경변수 계좌번호 사용]: {ENV_ACCOUNT_NO}")
        return ENV_ACCOUNT_NO

    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    res = requests.get(f"{base_url}/api/v1/accounts", headers=headers, proxies=proxies, timeout=10)
    if res.status_code == 200:
        data = res.json()
        accounts = data.get("result", []) if isinstance(data, dict) else data
        if accounts:
            acc_no = accounts[0].get("accountNo")
            print(f"✅ [API 계좌 자동 감지]: {acc_no}")
            return str(acc_no)
    raise Exception("계좌번호를 찾을 수 없습니다. TOSS_ACCOUNT_NO 환경변수를 확인하세요.")

def get_account_summary(base_url, token, account_no):
    headers = get_headers(token, account_no)
    
    # 1. 예수금 조회 (USD 및 기본 통화 시도)
    cash_balance = 0.0
    for params in [{"currency": "USD"}, {}]:
        acc_res = requests.get(f"{base_url}/api/v1/buying-power", headers=headers, params=params, proxies=proxies, timeout=10)
        if acc_res.status_code == 200:
            acc_data = acc_res.json()
            cash_balance = float(acc_data.get("buyingPower", acc_data.get("cashBalance", acc_data.get("amount", 0.0))))
            print(f"✅ 예수금 조회 성공: ${cash_balance:,.2f}")
            break
    else:
        print(f"⚠️ 예수금 조회 실패: {acc_res.text}")

    # 2. 보유 포지션 조회
    pos_res = requests.get(f"{base_url}/api/v1/holdings", headers=headers, proxies=proxies, timeout=10)
    positions = {}
    if pos_res.status_code == 200:
        pos_data = pos_res.json()
        items = pos_data if isinstance(pos_data, list) else pos_data.get("holdings", pos_data.get("items", []))
        for item in items:
            sym = item.get("symbol", item.get("ticker"))
            qty = int(item.get("quantity", item.get("shares", 0)))
            avg_p = float(item.get("averagePrice", item.get("avgPrice", 0.0)))
            if sym and qty > 0:
                positions[sym] = {"shares": qty, "avg_price": avg_p}
        print(f"✅ 포지션 조회 성공: {positions}")
    else:
        print(f"⚠️ 포지션 조회 실패: {pos_res.text}")

    return cash_balance, positions

def get_current_price(symbol):
    try:
        ticker = yf.Ticker(symbol)
        price = ticker.fast_info['lastPrice']
        if price and price > 0:
            return float(price)
        return None
    except Exception as e:
        print(f"❌ {symbol} 현재가 조회 실패: {e}")
        return None

def place_order(base_url, token, account_no, symbol, side, order_type, price, quantity):
    headers = get_headers(token, account_no)
    payload = {
        "symbol": symbol,
        "side": side,
        "orderType": "LIMIT" if order_type == "LOC" else order_type,
        "price": str(round(price, 2)),
        "quantity": int(quantity)
    }
    res = requests.post(f"{base_url}/api/v1/orders", headers=headers, json=payload, proxies=proxies, timeout=10)
    print(f"   Order [{side}] Status {res.status_code}: {res.text}")
    return res.json() if res.status_code in [200, 201] else None

def run():
    print("🚀 [동적 예수금 연동 무한매수법] 실행\n")
    print_my_ip()

    active_base_url = None
    token = None
    account_no = None

    # URL 후보군 순회 (Prod -> Sandbox)
    for base_url in API_BASE_URLS:
        try:
            print(f"\n🔄 연결 시도중: {base_url}")
            token = get_access_token(base_url)
            account_no = fetch_valid_account_number(base_url, token)
            
            # 예수금 테스트 호출로 계좌 인식 여부 최종 검증
            headers = get_headers(token, account_no)
            test_res = requests.get(f"{base_url}/api/v1/buying-power", headers=headers, proxies=proxies, timeout=5)
            if test_res.status_code == 200:
                active_base_url = base_url
                print(f"✅ 접속 및 계좌 인증 성공!: {active_base_url}")
                break
            else:
                print(f"⚠️ 계좌 미인식 ({test_res.status_code}): {test_res.text}")
        except Exception as e:
            print(f"❌ 실패: {e}")

    if not active_base_url:
        print("\n❌ 모든 서버 환경에서 계좌 인증에 실패했습니다.")
        print("💡 [점검 포인트]")
        print("1. 토스증권 Open API 센터에서 API Key와 계좌번호가 정확히 연결되어 있는지 확인하세요.")
        print("2. TOSS_ACCOUNT_NO 환경변수에 하이픈(-)을 제외한 계좌번호 숫자만 등록되어 있는지 확인하세요.")
        return

    # 정상 접속 완료 후 로직 실행
    cash_balance, positions = get_account_summary(active_base_url, token, account_no)

    total_stock_eval = 0.0
    stock_details = {}

    for symbol in PORTFOLIO_CONFIG.keys():
        pos = positions.get(symbol, {"shares": 0, "avg_price": 0.0})
        cur_price = get_current_price(symbol)
        if not cur_price:
            continue
        eval_amount = pos["shares"] * cur_price
        total_stock_eval += eval_amount
        stock_details[symbol] = {
            "shares": pos["shares"],
            "avg_price": pos["avg_price"],
            "current_price": cur_price,
            "eval_amount": eval_amount
        }

    total_account_value = cash_balance + total_stock_eval
    print(f"\n📊 [자산 현황]")
    print(f"- 실시간 예수금: ${cash_balance:,.2f}")
    print(f"- 총 평가 자산: ${total_account_value:,.2f}\n")

    for symbol, config in PORTFOLIO_CONFIG.items():
        if symbol not in stock_details:
            continue
        ratio = config["allocation_ratio"]
        one_buy_budget = (total_account_value * ratio) / 40.0
        info = stock_details[symbol]
        shares, avg_price, current_price = info["shares"], info["avg_price"], info["current_price"]

        if shares == 0:
            if cash_balance < current_price:
                print(f"⚠️ 예수금(${cash_balance:.2f}) 부족으로 매수 불가")
                continue
            buy_qty = max(math.floor(one_buy_budget / current_price), 1)
            place_order(active_base_url, token, account_no, symbol, "BUY", "LIMIT", current_price, buy_qty)
            continue

        # 보유 중일 경우 매도/매수 주문
        t_turn = (shares * avg_price) / one_buy_budget if one_buy_budget > 0 else 0
        star_percent = max(10.0 - (t_turn * 0.25), 0.0)
        target_sell_price = avg_price * (1 + (star_percent / 100.0))
        place_order(active_base_url, token, account_no, symbol, "SELL", "LIMIT", target_sell_price, shares)

        half_budget = one_buy_budget / 2.0
        if t_turn < 20:
            place_order(active_base_url, token, account_no, symbol, "BUY", "LIMIT", avg_price, max(math.floor(half_budget / avg_price), 1))
            place_order(active_base_url, token, account_no, symbol, "BUY", "LIMIT", avg_price * 1.05, max(math.floor(half_budget / (avg_price * 1.05)), 1))
        else:
            place_order(active_base_url, token, account_no, symbol, "BUY", "LIMIT", avg_price, max(math.floor(one_buy_budget / avg_price), 1))

    print("\n🎉 모든 처리가 완료되었습니다.")

if __name__ == "__main__":
    run()
