import os
import math
import requests
import yfinance as yf

# ----------------------------------------------------
# 1. 환경변수 및 Fixie 고정 IP 프록시 설정
# ----------------------------------------------------
CLIENT_ID = os.environ.get("TOSS_CLIENT_ID")
CLIENT_SECRET = os.environ.get("TOSS_CLIENT_SECRET")
FIXIE_URL = os.environ.get("FIXIE_URL")

API_BASE_URL = "https://openapi.tossinvest.com"

proxies = None
if FIXIE_URL:
    proxies = {
        "http": FIXIE_URL,
        "https": FIXIE_URL
    }

PORTFOLIO_CONFIG = {
    "SNDL": {"allocation_ratio": 1.00}
}

def print_my_ip():
    try:
        ip = requests.get('https://api.ipify.org', proxies=proxies, timeout=5).text
        print(f"[Check] 토스 API로 접근하는 외부 IP: {ip}")
        return ip
    except Exception as e:
        print(f"❌ IP 조회 실패: {e}")
        return None

def get_access_token():
    url = f"{API_BASE_URL}/oauth2/token"
    payload = {
        "grant_type": "client_credentials",
        "client_id": CLIENT_ID,
        "client_secret": CLIENT_SECRET
    }
    res = requests.post(url, data=payload, proxies=proxies, timeout=10)
    if res.status_code == 200:
        return res.json().get("access_token")
    else:
        raise Exception(f"토큰 발급 실패: {res.text}")

def fetch_primary_account_info(token):
    """토스 API 계좌 목록에서 accountNo 및 accountSeq 추출"""
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json"
    }
    res = requests.get(f"{API_BASE_URL}/api/v1/accounts", headers=headers, proxies=proxies, timeout=10)
    
    if res.status_code == 200:
        data = res.json()
        accounts = data.get("result", []) if isinstance(data, dict) else data
        if accounts and len(accounts) > 0:
            first_acc = accounts[0]
            acc_no = first_acc.get("accountNo")
            acc_seq = first_acc.get("accountSeq")
            print(f"✅ [계좌 자동 감지 성공] accountNo: {acc_no} | accountSeq: {acc_seq}")
            return str(acc_no), str(acc_seq)
        else:
            raise Exception("❌ 연동된 토스증권 계좌를 찾을 수 없습니다.")
    else:
        raise Exception(f"❌ 계좌 목록 조회 실패: {res.text}")

def get_headers(token, account_val):
    return {
        "Authorization": f"Bearer {token}",
        "x-tossinvest-account": str(account_val),
        "Content-Type": "application/json"
    }

def get_account_summary(token, acc_no, acc_seq):
    # 1차 시도: accountSeq 사용 (토스 API 표준 규격)
    headers = get_headers(token, acc_seq)
    
    acc_res = requests.get(f"{API_BASE_URL}/api/v1/buying-power", headers=headers, proxies=proxies, timeout=10)
    
    # 만약 accountSeq로 실패 시 accountNo로 2차 재시도
    if acc_res.status_code != 200:
        print(f"🔄 accountSeq({acc_seq}) 호출 실패. accountNo({acc_no})로 재시도합니다...")
        headers = get_headers(token, acc_no)
        acc_res = requests.get(f"{API_BASE_URL}/api/v1/buying-power", headers=headers, proxies=proxies, timeout=10)

    cash_balance = 0.0
    if acc_res.status_code == 200:
        acc_data = acc_res.json()
        cash_balance = float(acc_data.get("buyingPower", acc_data.get("cashBalance", acc_data.get("amount", 0.0))))
        print(f"✅ 예수금 조회 성공: ${cash_balance:,.2f}")
    else:
        print(f"⚠️ 예수금 조회 응답 오류 (상태 {acc_res.status_code}): {acc_res.text}")

    # 보유 포지션 조회
    pos_res = requests.get(f"{API_BASE_URL}/api/v1/holdings", headers=headers, proxies=proxies, timeout=10)
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
        print(f"⚠️ 포지션 조회 응답 오류 (상태 {pos_res.status_code}): {pos_res.text}")

    return cash_balance, positions, headers

def get_current_price(symbol):
    try:
        ticker = yf.Ticker(symbol)
        price = ticker.fast_info['lastPrice']
        if price and price > 0:
            print(f"[Check] {symbol} 현재가 조회 성공(yfinance): ${price:.4f}")
            return float(price)
        return None
    except Exception as e:
        print(f"❌ {symbol} 주가 조회 예외 발생: {e}")
        return None

def place_order(headers, symbol, side, order_type, price, quantity):
    valid_order_type = "LIMIT" if order_type == "LOC" else order_type
    
    payload = {
        "symbol": symbol,
        "side": side,
        "orderType": valid_order_type,
        "price": str(round(price, 2)),
        "quantity": int(quantity)
    }

    res = requests.post(f"{API_BASE_URL}/api/v1/orders", headers=headers, json=payload, proxies=proxies, timeout=10)
    
    try:
        res_data = res.json()
    except Exception:
        res_data = res.text

    if res.status_code in [200, 201]:
        print(f"   ✅ 주문 성공 응답: {res_data}")
    else:
        print(f"   ❌ 주문 실패 응답 (상태코드 {res.status_code}): {res_data}")
        
    return res_data

def run_dynamic_multi_infinite_buying():
    print("🚀 [동적 예수금 연동 무한매수법] 자동 주문을 시작합니다...\n")
    try:
        token = get_access_token()
        
        # accountNo 및 accountSeq 자동 추출
        acc_no, acc_seq = fetch_primary_account_info(token)

        cash_balance, positions, valid_headers = get_account_summary(token, acc_no, acc_seq)

        total_stock_eval = 0.0
        stock_details = {}

        for symbol in PORTFOLIO_CONFIG.keys():
            pos = positions.get(symbol, {"shares": 0, "avg_price": 0.0})
            cur_price = get_current_price(symbol)
            
            if not cur_price:
                print(f"⚠️ {symbol} 현재가를 가져오지 못해 주문 처리를 스킵합니다.")
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
        print(f"\n📊 [계좌 자산 현황]")
        print(f"- 실시간 예수금: ${cash_balance:,.2f}")
        print(f"- 주식 총 평가액: ${total_stock_eval:,.2f}")
        print(f"- 계좌 총 자산: ${total_account_value:,.2f}\n")
        print("=" * 60)

        for symbol, config in PORTFOLIO_CONFIG.items():
            if symbol not in stock_details:
                continue

            ratio = config["allocation_ratio"]
            symbol_capital = total_account_value * ratio
            one_buy_budget = symbol_capital / 40.0

            info = stock_details[symbol]
            shares = info["shares"]
            avg_price = info["avg_price"]
            current_price = info["current_price"]

            print(f"\n🔹 [{symbol}] 주문 처리 (비율: {int(ratio*100)}% | 할당 예산: ${symbol_capital:,.2f})")
            print(f"   └ 1회 매수 예산: ${one_buy_budget:.2f} | 현재가: ${current_price:.2f} | 보유: {shares}주 (평단가: ${avg_price:.2f})")

            if shares == 0:
                if cash_balance < current_price:
                    print(f"   ⚠️ 예수금(${cash_balance:.2f})이 현재가(${current_price:.2f})보다 부족하여 주문을 제출할 수 없습니다.")
                    continue
                buy_qty = max(math.floor(one_buy_budget / current_price), 1)
                place_order(valid_headers, symbol, "BUY", "LIMIT", current_price, buy_qty)
                continue

            total_invested = shares * avg_price
            t_turn = total_invested / one_buy_budget if one_buy_budget > 0 else 0

            star_percent = max(10.0 - (t_turn * 0.25), 0.0)
            target_sell_price = avg_price * (1 + (star_percent / 100.0))

            place_order(valid_headers, symbol, "SELL", "LIMIT", target_sell_price, shares)

            half_budget = one_buy_budget / 2.0
            if t_turn < 20:
                loc1_price = avg_price
                loc1_qty = max(math.floor(half_budget / loc1_price), 1 if cash_balance >= loc1_price else 0)
                if loc1_qty > 0:
                    place_order(valid_headers, symbol, "BUY", "LIMIT", loc1_price, loc1_qty)

                loc2_price = avg_price * 1.05
                loc2_qty = max(math.floor(half_budget / loc2_price), 1 if cash_balance >= loc2_price else 0)
                if loc2_qty > 0:
                    place_order(valid_headers, symbol, "BUY", "LIMIT", loc2_price, loc2_qty)
            else:
                loc_price = avg_price
                loc_qty = max(math.floor(one_buy_budget / loc_price), 1 if cash_balance >= loc_price else 0)
                if loc_qty > 0:
                    place_order(valid_headers, symbol, "BUY", "LIMIT", loc_price, loc_qty)

        print("\n🎉 모든 종목의 자동 주문 제출이 완료되었습니다!")

    except Exception as e:
        print(f"\n❌ 오류 발생: {e}")

if __name__ == "__main__":
    print_my_ip()
    run_dynamic_multi_infinite_buying()
