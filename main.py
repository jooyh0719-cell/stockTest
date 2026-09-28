import os
import math
import requests
import yfinance as yf

# ----------------------------------------------------
# 1. 환경변수 및 Fixie 고정 IP 프록시 설정
# ----------------------------------------------------
CLIENT_ID = os.environ.get("TOSS_CLIENT_ID")
CLIENT_SECRET = os.environ.get("TOSS_CLIENT_SECRET")
ACCOUNT_NO = os.environ.get("TOSS_ACCOUNT_NO")
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

def get_account_summary(token):
    headers = {"Authorization": f"Bearer {token}"}
    
    # 계좌번호 파라미터 추가
    params = {}
    if ACCOUNT_NO:
        params["account_no"] = ACCOUNT_NO

    # 1. 예수금 조회
    acc_res = requests.get(f"{API_BASE_URL}/v1/account/balance", headers=headers, params=params, proxies=proxies, timeout=10)
    cash_balance = 0.0
    if acc_res.status_code == 200:
        acc_data = acc_res.json()
        cash_balance = float(acc_data.get("output", {}).get("dnca_tot_amt", acc_data.get("cash_balance", 0.0)))
    else:
        print(f"⚠️ 예수금 조회 응답 오류: {acc_res.text}")

    # 2. 보유 포지션 조회
    pos_res = requests.get(f"{API_BASE_URL}/v1/account/positions", headers=headers, params=params, proxies=proxies, timeout=10)
    positions = {}
    if pos_res.status_code == 200:
        pos_data = pos_res.json()
        items = pos_data.get("positions", pos_data.get("output", []))
        for item in items:
            sym = item.get("symbol", item.get("pdno"))
            qty = int(item.get("quantity", item.get("hldg_qty", 0)))
            avg_p = float(item.get("average_price", item.get("pavg", 0.0)))
            if qty > 0:
                positions[sym] = {"shares": qty, "avg_price": avg_p}
    else:
        print(f"⚠️ 포지션 조회 응답 오류: {pos_res.text}")

    return cash_balance, positions

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

def place_order(token, symbol, side, order_type, price, quantity):
    """토스증권 API 주문 전송 및 응답 검증"""
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json"
    }
    
    valid_order_type = "LIMIT" if order_type == "LOC" else order_type
    
    payload = {
        "symbol": symbol,
        "side": side,
        "order_type": valid_order_type,
        "price": str(round(price, 2)),
        "quantity": str(quantity)
    }
    if ACCOUNT_NO:
        payload["account_no"] = ACCOUNT_NO

    res = requests.post(f"{API_BASE_URL}/v1/orders", headers=headers, json=payload, proxies=proxies, timeout=10)
    res_data = res.json()
    
    # API 호출 결과 로그 출력
    if res.status_code in [200, 201] and res_data.get("status") != "FAILED":
        print(f"   ✅ 주문 성공 응답: {res_data}")
    else:
        print(f"   ❌ 주문 실패 응답: {res_data}")
        
    return res_data

def run_dynamic_multi_infinite_buying():
    print("🚀 [동적 예수금 연동 무한매수법] 자동 주문을 시작합니다...\n")
    try:
        token = get_access_token()
        cash_balance, positions = get_account_summary(token)

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
        print(f"📊 [계좌 자산 현황]")
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

            # 신규 진입시 예수금이 주가보다 작으면 매수 시도 안함
            if shares == 0:
                if cash_balance < current_price:
                    print(f"   ⚠️ 예수금(${cash_balance:.2f})이 현재가(${current_price:.2f})보다 부족하여 주문을 제출할 수 없습니다.")
                    continue
                buy_qty = max(math.floor(one_buy_budget / current_price), 1)
                place_order(token, symbol, "BUY", "LIMIT", current_price, buy_qty)
                continue

            total_invested = shares * avg_price
            t_turn = total_invested / one_buy_budget if one_buy_budget > 0 else 0

            star_percent = max(10.0 - (t_turn * 0.25), 0.0)
            target_sell_price = avg_price * (1 + (star_percent / 100.0))

            place_order(token, symbol, "SELL", "LIMIT", target_sell_price, shares)

            half_budget = one_buy_budget / 2.0
            if t_turn < 20:
                loc1_price = avg_price
                loc1_qty = max(math.floor(half_budget / loc1_price), 1 if cash_balance >= loc1_price else 0)
                if loc1_qty > 0:
                    place_order(token, symbol, "BUY", "LIMIT", loc1_price, loc1_qty)

                loc2_price = avg_price * 1.05
                loc2_qty = max(math.floor(half_budget / loc2_price), 1 if cash_balance >= loc2_price else 0)
                if loc2_qty > 0:
                    place_order(token, symbol, "BUY", "LIMIT", loc2_price, loc2_qty)
            else:
                loc_price = avg_price
                loc_qty = max(math.floor(one_buy_budget / loc_price), 1 if cash_balance >= loc_price else 0)
                if loc_qty > 0:
                    place_order(token, symbol, "BUY", "LIMIT", loc_price, loc_qty)

        print("\n🎉 모든 종목의 자동 주문 제출이 완료되었습니다!")

    except Exception as e:
        print(f"\n❌ 오류 발생: {e}")

if __name__ == "__main__":
    print_my_ip()
    run_dynamic_multi_infinite_buying()
