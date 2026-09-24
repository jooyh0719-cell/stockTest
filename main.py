import os
import math
import requests

# GitHub Secrets에서 인증 정보를 가져옵니다.
CLIENT_ID = os.environ.get("TOSS_CLIENT_ID")
CLIENT_SECRET = os.environ.get("TOSS_CLIENT_SECRET")

API_BASE_URL = "https://openapi.tossinvest.com"

# =====================================================================
# 📌 다종목 및 예수금 비율 설정 (합계가 1.0(100%)이 되도록 설정)
# =====================================================================
# 예시: TQQQ 60%, SOXL 40% 설정 (1개 종목만 할 경우 "TQQQ": {"allocation_ratio": 1.00} 설정)
PORTFOLIO_CONFIG = {
    "TQQQ": {"allocation_ratio": 0.60},
    "SOXL": {"allocation_ratio": 0.40}
}

def get_access_token():
    """토스증권 OAuth2 액세스 토큰 발급"""
    url = f"{API_BASE_URL}/oauth2/token"
    payload = {
        "grant_type": "client_credentials",
        "client_id": CLIENT_ID,
        "client_secret": CLIENT_SECRET
    }
    res = requests.post(url, data=payload, timeout=10)
    if res.status_code == 200:
        return res.json().get("access_token")
    else:
        raise Exception(f"토큰 발급 실패: {res.text}")

def get_account_summary(token):
    """계좌의 실시간 예수금(현금) 및 전체 보유 주식 정보를 조회합니다."""
    headers = {"Authorization": f"Bearer {token}"}
    
    # 1. 예수금(현금 잔고) 조회
    acc_res = requests.get(f"{API_BASE_URL}/v1/account/balance", headers=headers, timeout=10)
    cash_balance = 0.0
    if acc_res.status_code == 200:
        acc_data = acc_res.json()
        cash_balance = float(acc_data.get("output", {}).get("dnca_tot_amt", acc_data.get("cash_balance", 0.0)))
    
    # 2. 보유 포지션 조회
    pos_res = requests.get(f"{API_BASE_URL}/v1/account/positions", headers=headers, timeout=10)
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
                
    return cash_balance, positions

def get_current_price(token, symbol):
    """종목의 실시간 현재가를 조회합니다."""
    headers = {"Authorization": f"Bearer {token}"}
    res = requests.get(f"{API_BASE_URL}/v1/market/quote?symbol={symbol}", headers=headers, timeout=10)
    if res.status_code == 200:
        p_data = res.json()
        price = float(p_data.get("last_price", p_data.get("stck_prpr", 0.0)))
        if price > 0:
            return price
    raise Exception(f"[{symbol}] 현재가 조회 실패: {res.text}")

def place_order(token, symbol, side, order_type, price, quantity):
    """토스증권 API 주문 전송 함수"""
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json"
    }
    payload = {
        "symbol": symbol,
        "side": side,
        "order_type": order_type,
        "price": round(price, 2),
        "quantity": quantity
    }
    res = requests.post(f"{API_BASE_URL}/v1/orders", headers=headers, json=payload, timeout=10)
    return res.json()

def run_dynamic_multi_infinite_buying():
    print("🚀 [동적 예수금 연동 무한매수법] 자동 주문을 시작합니다...\n")
    try:
        token = get_access_token()
        cash_balance, positions = get_account_summary(token)
        
        # 1. 총 보유 주식 평가금액 및 각 종목별 정보 파악
        total_stock_eval = 0.0
        stock_details = {}
        
        for symbol in PORTFOLIO_CONFIG.keys():
            pos = positions.get(symbol, {"shares": 0, "avg_price": 0.0})
            cur_price = get_current_price(token, symbol)
            eval_amount = pos["shares"] * cur_price
            total_stock_eval += eval_amount
            
            stock_details[symbol] = {
                "shares": pos["shares"],
                "avg_price": pos["avg_price"],
                "current_price": cur_price,
                "eval_amount": eval_amount
            }
            
        # 2. 계좌의 실시간 총 자산 (예수금 + 모든 주식 평가금)
        total_account_value = cash_balance + total_stock_eval
        print(f"📊 [계좌 자산 현황]")
        print(f"- 실시간 예수금: ${cash_balance:,.2f}")
        print(f"- 주식 총 평가액: ${total_stock_eval:,.2f}")
        print(f"- 계좌 총 자산: ${total_account_value:,.2f}\n")
        print("=" * 60)

        # 3. 종목별 독립 주문 실행
        for symbol, config in PORTFOLIO_CONFIG.items():
            ratio = config["allocation_ratio"]
            symbol_capital = total_account_value * ratio  # 해당 종목에 할당된 실시간 총 예산
            one_buy_budget = symbol_capital / 40.0         # 1회분 매수 예산 ($)
            
            info = stock_details[symbol]
            shares = info["shares"]
            avg_price = info["avg_price"]
            current_price = info["current_price"]
            
            print(f"\n🔹 [{symbol}] 주문 처리 (비율: {int(ratio*100)}% | 할당 예산: ${symbol_capital:,.2f})")
            print(f"   └ 1회 매수 예산: ${one_buy_budget:.2f} | 현재가: ${current_price:.2f} | 보유: {shares}주 (평단가: ${avg_price:.2f})")

            # A. 신규 진입 (0주 보유 시)
            if shares == 0:
                buy_qty = max(math.floor(one_buy_budget / current_price), 1)
                res = place_order(token, symbol, "BUY", "LOC", current_price, buy_qty)
                print(f"   ✅ [신규 진입] LOC 매수: {buy_qty}주 @ ${current_price:.2f}")
                continue

            # B. 기존 진행 중 (v2.2~v3.0 별지점 로직)
            total_invested = shares * avg_price
            t_turn = total_invested / one_buy_budget  # 현재 진행 회차(T) 동적 역산
            
            # 별지점 목표 수익률 계산: 10% - (회차 × 0.25%)
            star_percent = max(10.0 - (t_turn * 0.25), 0.0)
            target_sell_price = avg_price * (1 + (star_percent / 100.0))

            # 지정가 매도 예약
            sell_res = place_order(token, symbol, "SELL", "LIMIT", target_sell_price, shares)
            print(f"   ✅ [매도 예약] 전량({shares}주) @ ${target_sell_price:.2f} (+{star_percent:.2f}%)")

            # LOC 매수 (전반전 vs 후반전)
            half_budget = one_buy_budget / 2.0
            if t_turn < 20: # 전반전 (평단가 LOC + 평단가 5% 위 LOC)
                loc1_price = avg_price
                loc1_qty = math.floor(half_budget / loc1_price)
                if loc1_qty > 0:
                    place_order(token, symbol, "BUY", "LOC", loc1_price, loc1_qty)
                    
                loc2_price = avg_price * 1.05
                loc2_qty = math.floor(half_budget / loc2_price)
                if loc2_qty > 0:
                    place_order(token, symbol, "BUY", "LOC", loc2_price, loc2_qty)
                print(f"   ✅ [전반전 매수] 1) {loc1_qty}주 @ ${loc1_price:.2f} / 2) {loc2_qty}주 @ ${loc2_price:.2f}")

            else: # 후반전 (평단가 LOC 올인)
                loc_qty = math.floor(one_buy_budget / avg_price)
                if loc_qty > 0:
                    place_order(token, symbol, "BUY", "LOC", avg_price, loc_qty)
                print(f"   ✅ [후반전 매수] {loc_qty}주 @ ${avg_price:.2f}")

        print("\n🎉 모든 종목의 자동 주문 제출이 완료되었습니다!")

    except Exception as e:
        print(f"\n❌ 오류 발생: {e}")

if __name__ == "__main__":
    run_dynamic_multi_infinite_buying()
