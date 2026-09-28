import os
import math
import time
import uuid
from decimal import Decimal, ROUND_DOWN, InvalidOperation

import requests

# ============================================================
# Toss Securities Open API - SNDL 무한매수법
# ============================================================
# 핵심 변경점
# 1) yfinance 제거 -> 토스증권 /prices 사용
# 2) 계좌번호가 아니라 accountSeq 사용
# 3) API 응답의 result envelope을 고려한 파싱
# 4) 기존 미체결 주문 중복 방지
# 5) DRY_RUN=True 기본값: 절대로 실제 주문을 내지 않음
# 6) 실제 주문은 DRY_RUN=False 로 명시적으로 변경해야 함
#
# 필요한 환경변수
#   TOSS_CLIENT_ID
#   TOSS_CLIENT_SECRET
#   TOSS_ACCOUNT_SEQ
#   FIXIE_URL (선택)
#
# 기존에 TOSS_ACCOUNT_NO를 사용했다면:
#   TOSS_ACCOUNT_SEQ로 변경하는 것을 권장합니다.
#
# GitHub Actions에서는 반드시:
#   DRY_RUN=true
# 로 먼저 테스트한 뒤 로그가 정상인지 확인하세요.
# ============================================================


# ------------------------------------------------------------
# 1. 환경설정
# ------------------------------------------------------------

CLIENT_ID = os.environ.get("TOSS_CLIENT_ID")
CLIENT_SECRET = os.environ.get("TOSS_CLIENT_SECRET")

# 토스 Open API에서 GET /api/v1/accounts로 확인되는 accountSeq
ACCOUNT_SEQ = os.environ.get("TOSS_ACCOUNT_SEQ")

# 기존 Secret 이름과의 호환용.
# 단, 이 값이 "실제 계좌번호"라면 사용하지 마세요.
if not ACCOUNT_SEQ:
    ACCOUNT_SEQ = os.environ.get("TOSS_ACCOUNT_NO")

FIXIE_URL = os.environ.get("FIXIE_URL")

# 안전장치:
# 환경변수 DRY_RUN=false 로 명시적으로 지정했을 때만 실주문.
DRY_RUN = os.environ.get("DRY_RUN", "true").strip().lower() != "false"

API_BASE_URL = "https://openapi.tossinvest.com"

PORTFOLIO_CONFIG = {
    "SNDL": {
        "allocation_ratio": 1.00,
    }
}

# 총자산을 40등분
TOTAL_STEPS = 40

# 가격 소수점.
# 일반적인 미국 주식 지정가 주문용.
# 토스가 invalid-tick-size를 반환하면 해당 종목의 호가단위에 맞춰 조정하세요.
PRICE_DECIMALS = 2

TIMEOUT = 10

proxies = {
    "http": FIXIE_URL,
    "https": FIXIE_URL,
} if FIXIE_URL else None


# ------------------------------------------------------------
# 2. 공통 유틸
# ------------------------------------------------------------

def require_env():
    missing = []

    if not CLIENT_ID:
        missing.append("TOSS_CLIENT_ID")
    if not CLIENT_SECRET:
        missing.append("TOSS_CLIENT_SECRET")
    if not ACCOUNT_SEQ:
        missing.append("TOSS_ACCOUNT_SEQ")

    if missing:
        raise RuntimeError(
            "필수 환경변수가 없습니다: " + ", ".join(missing)
        )


def print_my_ip():
    try:
        res = requests.get(
            "https://api.ipify.org",
            proxies=proxies,
            timeout=5,
        )
        res.raise_for_status()
        print(f"[Check] 외부 접근 IP: {res.text.strip()}")
    except Exception as e:
        print(f"⚠️ IP 조회 실패: {e}")


def safe_json(res):
    try:
        return res.json()
    except Exception:
        return {"_raw_text": res.text}


def api_error_text(res):
    data = safe_json(res)

    if isinstance(data, dict):
        err = data.get("error")
        if isinstance(err, dict):
            code = err.get("code", "")
            message = err.get("message", "")
            return f"{code}: {message}".strip(": ")

    return res.text[:1000]


def request_with_retry(method, url, **kwargs):
    """
    토스 API 429/5xx에 대한 간단한 재시도.
    """
    max_retry = 3

    for attempt in range(max_retry):
        try:
            res = requests.request(
                method,
                url,
                proxies=proxies,
                timeout=TIMEOUT,
                **kwargs,
            )

            if res.status_code not in (429, 500, 502, 503, 504):
                return res

            retry_after = res.headers.get("Retry-After")
            if retry_after:
                try:
                    wait = min(float(retry_after), 10.0)
                except ValueError:
                    wait = 1.0
            else:
                wait = 2 ** attempt

            print(
                f"⚠️ API {res.status_code}, "
                f"{wait:.1f}초 후 재시도..."
            )
            time.sleep(wait)

        except requests.RequestException:
            if attempt == max_retry - 1:
                raise
            time.sleep(2 ** attempt)

    return res


def get_headers(token, account_required=False):
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }

    if account_required:
        headers["X-Tossinvest-Account"] = str(ACCOUNT_SEQ)

    return headers


def result_of(data):
    """
    토스 API의 {result: ...} envelope을 제거.
    """
    if isinstance(data, dict) and "result" in data:
        return data["result"]
    return data


def first_number(obj, keys, default=None):
    """
    중첩된 dict/list에서 흔히 쓰이는 숫자 필드를 찾는다.
    """
    if isinstance(obj, dict):
        for key in keys:
            if key in obj and obj[key] not in (None, ""):
                try:
                    return float(obj[key])
                except (ValueError, TypeError):
                    pass

        for value in obj.values():
            found = first_number(value, keys, None)
            if found is not None:
                return found

    elif isinstance(obj, list):
        for item in obj:
            found = first_number(item, keys, None)
            if found is not None:
                return found

    return default


def recursive_items(obj):
    """
    holdings/orders처럼 response schema가 중첩될 수 있는 경우
    dict/list 내부의 dict를 재귀적으로 찾는다.
    """
    if isinstance(obj, dict):
        yield obj
        for value in obj.values():
            yield from recursive_items(value)

    elif isinstance(obj, list):
        for item in obj:
            yield from recursive_items(item)


def to_decimal(value):
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return None


def round_price(price):
    d = to_decimal(price)
    if d is None:
        raise ValueError(f"가격 변환 실패: {price}")

    quantum = Decimal("1").scaleb(-PRICE_DECIMALS)
    return d.quantize(quantum, rounding=ROUND_DOWN)


# ------------------------------------------------------------
# 3. 인증 / 계좌
# ------------------------------------------------------------

def get_access_token():
    url = f"{API_BASE_URL}/oauth2/token"

    payload = {
        "grant_type": "client_credentials",
        "client_id": CLIENT_ID,
        "client_secret": CLIENT_SECRET,
    }

    res = request_with_retry(
        "POST",
        url,
        data=payload,
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
        },
    )

    if res.status_code != 200:
        raise RuntimeError(
            f"토큰 발급 실패 ({res.status_code}): "
            f"{api_error_text(res)}"
        )

    data = safe_json(res)
    token = data.get("access_token")

    if not token:
        raise RuntimeError(f"access_token이 없습니다: {data}")

    return token


def verify_account(token):
    """
    현재 환경변수의 accountSeq가 실제 계좌 목록에 존재하는지 확인.
    """
    url = f"{API_BASE_URL}/api/v1/accounts"

    res = request_with_retry(
        "GET",
        url,
        headers=get_headers(token),
    )

    if res.status_code != 200:
        raise RuntimeError(
            f"계좌 조회 실패 ({res.status_code}): "
            f"{api_error_text(res)}"
        )

    data = result_of(safe_json(res))

    if isinstance(data, dict):
        accounts = data.get("accounts", data.get("items", []))
    else:
        accounts = data

    if not isinstance(accounts, list):
        accounts = []

    print("\n[계좌 확인]")
    found = False

    for account in accounts:
        if not isinstance(account, dict):
            continue

        seq = (
            account.get("accountSeq")
            or account.get("account_seq")
        )

        print(
            f"  accountSeq={seq}, "
            f"account={account.get('accountNumber', '')}"
        )

        if str(seq) == str(ACCOUNT_SEQ):
            found = True

    if not found:
        raise RuntimeError(
            f"TOSS_ACCOUNT_SEQ={ACCOUNT_SEQ} 를 "
            f"GET /api/v1/accounts에서 찾지 못했습니다.\n"
            f"실제 accountSeq를 TOSS_ACCOUNT_SEQ에 등록하세요."
        )

    print(f"✅ accountSeq 확인: {ACCOUNT_SEQ}")


# ------------------------------------------------------------
# 4. 현재가
# ------------------------------------------------------------

def get_current_price(token, symbol):
    url = f"{API_BASE_URL}/api/v1/prices"

    res = request_with_retry(
        "GET",
        url,
        headers=get_headers(token),
        params={"symbols": symbol},
    )

    if res.status_code != 200:
        raise RuntimeError(
            f"{symbol} 현재가 조회 실패 "
            f"({res.status_code}): {api_error_text(res)}"
        )

    data = result_of(safe_json(res))

    # 공식 응답 예:
    # {"result":[{"symbol":"SNDL","lastPrice":"..."}]}
    if isinstance(data, list):
        for item in data:
            if isinstance(item, dict):
                if str(item.get("symbol", "")).upper() == symbol.upper():
                    price = to_decimal(item.get("lastPrice"))
                    if price and price > 0:
                        return price

    # 혹시 단일 object 형태로 오는 경우
    if isinstance(data, dict):
        price = to_decimal(data.get("lastPrice"))
        if price and price > 0:
            return price

    raise RuntimeError(f"{symbol} 현재가를 찾지 못했습니다: {data}")


# ------------------------------------------------------------
# 5. 예수금 / 보유주식
# ------------------------------------------------------------

def get_buying_power(token, currency="USD"):
    res = request_with_retry(
        "GET",
        f"{API_BASE_URL}/api/v1/buying-power",
        headers=get_headers(token, account_required=True),
        params={"currency": currency},
    )

    data = safe_json(res)

    result = data.get("result", {}) if isinstance(data, dict) else {}
    cash_buying_power = result.get("cashBuyingPower")

    if cash_buying_power is None:
        raise RuntimeError(
            f"buying-power 금액을 찾지 못했습니다: {data}"
        )

    try:
        return float(cash_buying_power)
    except (TypeError, ValueError):
        raise RuntimeError(
            f"cashBuyingPower 값이 숫자가 아닙니다: {cash_buying_power}"
        )

def get_holdings(token):
    url = f"{API_BASE_URL}/api/v1/holdings"

    res = request_with_retry(
        "GET",
        url,
        headers=get_headers(token, account_required=True),
    )

    if res.status_code != 200:
        raise RuntimeError(
            f"보유주식 조회 실패 "
            f"({res.status_code}): {api_error_text(res)}"
        )

    data = result_of(safe_json(res))

    # symbol/ticker를 가진 dict를 찾아 보유수량/평균단가 추출
    holdings = {}

    for item in recursive_items(data):
        symbol = item.get("symbol") or item.get("ticker")

        if not symbol:
            continue

        quantity_raw = (
            item.get("quantity")
            or item.get("shares")
            or item.get("holdingQuantity")
        )

        avg_price_raw = (
            item.get("averagePrice")
            or item.get("avgPrice")
            or item.get("averagePurchasePrice")
        )

        quantity = to_decimal(quantity_raw)
        avg_price = to_decimal(avg_price_raw)

        if quantity is None or quantity <= 0:
            continue

        if avg_price is None:
            avg_price = Decimal("0")

        holdings[str(symbol).upper()] = {
            "shares": int(quantity),
            "avg_price": avg_price,
        }

    return holdings


# ------------------------------------------------------------
# 6. 주문 조회 / 중복 방지
# ------------------------------------------------------------

def get_orders(token):
    """
    주문 목록 조회.
    API의 실제 response가 list/result/items 중 어느 형태여도 최대한 대응.
    """
    url = f"{API_BASE_URL}/api/v1/orders"

    res = request_with_retry(
        "GET",
        url,
        headers=get_headers(token, account_required=True),
    )

    if res.status_code != 200:
        raise RuntimeError(
            f"주문 목록 조회 실패 "
            f"({res.status_code}): {api_error_text(res)}"
        )

    return result_of(safe_json(res))


def is_pending_order(order):
    if not isinstance(order, dict):
        return False

    status = str(
        order.get("status")
        or order.get("orderStatus")
        or order.get("state")
        or ""
    ).upper()

    # 명확한 종료 상태
    terminal = {
        "FILLED",
        "PARTIALLY_FILLED",  # 아래에서 별도 처리
        "CANCELED",
        "CANCELLED",
        "REJECTED",
        "EXPIRED",
        "DONE",
        "COMPLETED",
    }

    if status == "PARTIALLY_FILLED":
        return True

    if status in terminal:
        return False

    # OPEN / PENDING / RECEIVED / ACCEPTED 등
    pending_words = (
        "OPEN",
        "PENDING",
        "WAIT",
        "RECEIVED",
        "ACCEPTED",
        "SUBMITTED",
        "PROCESS",
    )

    return any(word in status for word in pending_words)


def get_pending_orders_for_symbol(orders, symbol):
    found = []

    for item in recursive_items(orders):
        item_symbol = item.get("symbol") or item.get("ticker")

        if not item_symbol:
            continue

        if str(item_symbol).upper() != symbol.upper():
            continue

        if is_pending_order(item):
            found.append(item)

    return found


def print_pending_orders(pending):
    if not pending:
        print("  미체결 주문: 없음")
        return

    print(f"  미체결 주문: {len(pending)}건")

    for order in pending:
        print(
            "   - "
            f"side={order.get('side')}, "
            f"status={order.get('status') or order.get('orderStatus')}, "
            f"price={order.get('price')}, "
            f"qty={order.get('quantity') or order.get('orderQuantity')}"
        )


# ------------------------------------------------------------
# 7. 주문
# ------------------------------------------------------------

def place_order(
    token,
    symbol,
    side,
    price,
    quantity,
):
    price = round_price(price)
    quantity = int(quantity)

    if quantity <= 0:
        print("⚠️ 주문 수량이 0이어서 주문하지 않습니다.")
        return None

    payload = {
        "symbol": symbol,
        "side": side,
        "orderType": "LIMIT",
        "price": str(price),
        "quantity": quantity,
        # 중복 요청 방지용 clientOrderId
        "clientOrderId": str(uuid.uuid4()),
    }

    print(
        f"   >>> 주문 예정: {side} {symbol} "
        f"{quantity}주 @ ${price}"
    )

    if DRY_RUN:
        print("   [DRY_RUN] 실제 주문을 보내지 않았습니다.")
        return {
            "dryRun": True,
            "payload": payload,
        }

    url = f"{API_BASE_URL}/api/v1/orders"

    res = request_with_retry(
        "POST",
        url,
        headers=get_headers(token, account_required=True),
        json=payload,
    )

    if res.status_code not in (200, 201):
        print(
            f"   ❌ 주문 실패 ({res.status_code}): "
            f"{api_error_text(res)}"
        )
        return None

    data = safe_json(res)
    print(f"   ✅ 주문 접수: {data}")
    return data


# ------------------------------------------------------------
# 8. 전략 계산
# ------------------------------------------------------------

def calculate_strategy(
    total_account_value,
    allocation_ratio,
    shares,
    avg_price,
    current_price,
):
    one_buy_budget = (
        total_account_value
        * Decimal(str(allocation_ratio))
        / Decimal(TOTAL_STEPS)
    )

    if one_buy_budget <= 0:
        return None

    if shares <= 0:
        buy_qty = int(
            one_buy_budget / current_price
        )

        if buy_qty < 1:
            buy_qty = 1

        return {
            "one_buy_budget": one_buy_budget,
            "t_turn": Decimal("0"),
            "target_sell_price": None,
            "orders": [
                {
                    "side": "BUY",
                    "price": current_price,
                    "quantity": buy_qty,
                    "reason": "INITIAL_BUY",
                }
            ],
        }

    t_turn = (
        Decimal(shares) * avg_price
    ) / one_buy_budget

    star_percent = max(
        Decimal("10.0") - (t_turn * Decimal("0.25")),
        Decimal("0.0"),
    )

    target_sell_price = (
        avg_price
        * (Decimal("1") + star_percent / Decimal("100"))
    )

    orders = []

    # 기존 코드의 전략 유지
    orders.append(
        {
            "side": "SELL",
            "price": target_sell_price,
            "quantity": shares,
            "reason": "TARGET_SELL",
        }
    )

    half_budget = one_buy_budget / Decimal("2")

    if t_turn < Decimal("20"):
        qty1 = int(half_budget / avg_price)

        if qty1 < 1:
            qty1 = 1

        buy_price2 = avg_price * Decimal("1.05")
        qty2 = int(half_budget / buy_price2)

        if qty2 < 1:
            qty2 = 1

        orders.append(
            {
                "side": "BUY",
                "price": avg_price,
                "quantity": qty1,
                "reason": "AVERAGE_PRICE_BUY",
            }
        )

        orders.append(
            {
                "side": "BUY",
                "price": buy_price2,
                "quantity": qty2,
                "reason": "PLUS_5_PERCENT_BUY",
            }
        )

    else:
        qty = int(one_buy_budget / avg_price)

        if qty < 1:
            qty = 1

        orders.append(
            {
                "side": "BUY",
                "price": avg_price,
                "quantity": qty,
                "reason": "AVERAGE_PRICE_BUY",
            }
        )

    return {
        "one_buy_budget": one_buy_budget,
        "t_turn": t_turn,
        "star_percent": star_percent,
        "target_sell_price": target_sell_price,
        "orders": orders,
    }


# ------------------------------------------------------------
# 9. 실행
# ------------------------------------------------------------

def run():
    print("=" * 70)
    print("🚀 Toss Securities SNDL 무한매수법")
    print("=" * 70)

    print(f"실행 모드: {'DRY_RUN' if DRY_RUN else 'LIVE ORDER'}")
    print(f"accountSeq: {ACCOUNT_SEQ}")
    print(f"종목: {', '.join(PORTFOLIO_CONFIG.keys())}")

    if not DRY_RUN:
        print("\n⚠️⚠️⚠️ 실제 주문 모드입니다. ⚠️⚠️⚠️")
    else:
        print("\n🟢 DRY_RUN=True: 실제 주문을 절대 전송하지 않습니다.")

    require_env()
    print_my_ip()

    # --------------------------------------------------------
    # 인증
    # --------------------------------------------------------
    token = get_access_token()
    print("✅ OAuth 토큰 발급 성공")

    verify_account(token)

    # --------------------------------------------------------
    # 계좌 상태
    # --------------------------------------------------------
    cash_balance = get_buying_power(token, "USD")
    holdings = get_holdings(token)
    orders = get_orders(token)

    print("\n[계좌 현황]")
    print(f"- USD 매수 가능 금액: ${cash_balance:,.2f}")

    total_stock_eval = Decimal("0")
    stock_details = {}

    # --------------------------------------------------------
    # 종목별 현재가 / 보유
    # --------------------------------------------------------
    for symbol, config in PORTFOLIO_CONFIG.items():
        current_price = get_current_price(token, symbol)

        pos = holdings.get(
            symbol.upper(),
            {
                "shares": 0,
                "avg_price": Decimal("0"),
            },
        )

        shares = int(pos["shares"])
        avg_price = Decimal(str(pos["avg_price"]))

        eval_amount = (
            Decimal(shares) * current_price
        )

        total_stock_eval += eval_amount

        pending = get_pending_orders_for_symbol(
            orders,
            symbol,
        )

        stock_details[symbol] = {
            "shares": shares,
            "avg_price": avg_price,
            "current_price": current_price,
            "eval_amount": eval_amount,
            "pending_orders": pending,
            "allocation_ratio": Decimal(
                str(config["allocation_ratio"])
            ),
        }

        print(f"\n[{symbol}]")
        print(f"- 현재가: ${current_price}")
        print(f"- 보유수량: {shares}")
        print(f"- 평균매수가: ${avg_price}")
        print(f"- 평가금액: ${eval_amount:,.2f}")

        print_pending_orders(pending)

    total_account_value = (
        cash_balance + total_stock_eval
    )

    print("\n[총자산]")
    print(f"- USD 현금: ${cash_balance:,.2f}")
    print(f"- 주식 평가액: ${total_stock_eval:,.2f}")
    print(f"- 계산상 총자산: ${total_account_value:,.2f}")

    # --------------------------------------------------------
    # 전략 실행
    # --------------------------------------------------------
    for symbol, info in stock_details.items():
        shares = info["shares"]
        avg_price = info["avg_price"]
        current_price = info["current_price"]
        pending = info["pending_orders"]
        ratio = info["allocation_ratio"]

        strategy = calculate_strategy(
            total_account_value,
            ratio,
            shares,
            avg_price,
            current_price,
        )

        if strategy is None:
            print(f"\n⚠️ {symbol}: 전략 계산 실패")
            continue

        print(f"\n[{symbol} 전략]")
        print(
            f"- 1회 매수예산: "
            f"${strategy['one_buy_budget']:,.2f}"
        )

        if shares > 0:
            print(
                f"- T-turn: "
                f"{strategy['t_turn']:.4f}"
            )
            print(
                f"- 목표 수익률: "
                f"{strategy['star_percent']:.2f}%"
            )
            print(
                f"- 목표 매도가: "
                f"${strategy['target_sell_price']:.2f}"
            )

        # ----------------------------------------------------
        # 중복 주문 방지
        # ----------------------------------------------------
        #
        # 현재 종목에 미체결 주문이 하나라도 있으면
        # 새 주문을 전부 넣지 않는다.
        #
        # 이유:
        # GitHub Actions가 하루에 여러 번 실행되거나
        # 이전 주문이 장시간 미체결일 경우 주문 누적 방지.
        #
        if pending:
            print(
                f"🛑 {symbol}: 미체결 주문 "
                f"{len(pending)}건이 있어 새 주문을 만들지 않습니다."
            )
            continue

        # ----------------------------------------------------
        # 초기 매수
        # ----------------------------------------------------
        if shares == 0:
            order = strategy["orders"][0]

            required_cash = (
                order["price"]
                * Decimal(order["quantity"])
            )

            if cash_balance < required_cash:
                print(
                    f"⚠️ {symbol}: 초기 매수 필요금액 "
                    f"${required_cash:,.2f}, "
                    f"매수가능금액 ${cash_balance:,.2f}"
                )
                continue

            place_order(
                token,
                symbol,
                order["side"],
                order["price"],
                order["quantity"],
            )

            continue

        # ----------------------------------------------------
        # 보유 중 주문
        # ----------------------------------------------------
        #
        # BUY는 매수 가능 현금을 초과하지 않도록
        # 각 주문 직전에 다시 계산한다.
        #
        for order in strategy["orders"]:
            side = order["side"]
            price = order["price"]
            quantity = order["quantity"]

            if side == "BUY":
                required_cash = (
                    price * Decimal(quantity)
                )

                if cash_balance < required_cash:
                    print(
                        f"⚠️ {symbol}: BUY 생략 - "
                        f"필요 ${required_cash:,.2f}, "
                        f"가능 ${cash_balance:,.2f}"
                    )
                    continue

                result = place_order(
                    token,
                    symbol,
                    side,
                    price,
                    quantity,
                )

                # LIVE 주문이 실제 접수됐다면
                # 같은 실행에서 추가 BUY가 현금을 초과하지 않도록
                # 로컬 cash를 차감.
                if not DRY_RUN and result:
                    cash_balance -= required_cash

            elif side == "SELL":
                place_order(
                    token,
                    symbol,
                    side,
                    price,
                    quantity,
                )

    print("\n" + "=" * 70)
    print("🎉 실행 완료")
    print(
        "실제 주문 여부:",
        "LIVE" if not DRY_RUN else "DRY_RUN",
    )
    print("=" * 70)


if __name__ == "__main__":
    try:
        run()
    except KeyboardInterrupt:
        print("\n사용자가 실행을 중단했습니다.")
    except Exception as e:
        print("\n❌ 프로그램 오류")
        print(f"{type(e).__name__}: {e}")
        raise
