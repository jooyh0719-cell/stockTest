import os
import json
import uuid
import base64
import requests

from pathlib import Path
from decimal import Decimal, ROUND_DOWN, ROUND_CEILING
from datetime import datetime


# ============================================================
# 설정 및 환경변수
# ============================================================

CLIENT_ID = os.getenv("TOSS_CLIENT_ID", "")
CLIENT_SECRET = os.getenv("TOSS_CLIENT_SECRET", "")
ACCOUNT_SEQ = os.getenv("TOSS_ACCOUNT_SEQ", "")
FIXIE_URL = os.getenv("FIXIE_URL", "")

API_BASE_URL = "https://openapi.tossinvest.com"

# 콤마(,)로 구분된 종목 목록 받기 (기본값: BULZ,SOXL)
SYMBOLS_ENV = os.getenv("SYMBOLS", "BULZ,IONQ")
SYMBOLS = [s.strip().upper() for s in SYMBOLS_ENV.split(",") if s.strip()]

TOTAL_STEPS = 40
TIMEOUT = 20

# 전략 원금: 쉼표로 종목별 원금 지정 가능 (예: "1000,2000") 또는 단일값 지정 시 공통 적용
STRATEGY_CAPITAL_USD_ENV = os.getenv("STRATEGY_CAPITAL_USD", "")

GITHUB_TOKEN = os.getenv("GITHUB_TOKEN", "")
GITHUB_REPOSITORY = os.getenv("GITHUB_REPOSITORY", "")
GITHUB_REF_NAME = os.getenv("GITHUB_REF_NAME", "main")
GITHUB_STATE_PATH = os.getenv("GITHUB_STATE_PATH", "strategy_state.json")

STATE_FILE = Path("strategy_state.json")

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

SESSION = requests.Session()

if FIXIE_URL:
    SESSION.proxies.update({
        "http": FIXIE_URL,
        "https": FIXIE_URL,
    })


# ============================================================
# 공통 함수
# ============================================================

def log(message):
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{timestamp}] {message}", flush=True)


def notify(message):
    log(message)

    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return

    try:
        SESSION.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            json={
                "chat_id": TELEGRAM_CHAT_ID,
                "text": message,
            },
            timeout=TIMEOUT,
        )
    except Exception as exc:
        log(f"텔레그램 전송 실패: {exc}")


def dec(value, default="0"):
    try:
        if value is None or value == "":
            return Decimal(default)

        if isinstance(value, str):
            value = value.replace(",", "").replace("$", "").strip()

        return Decimal(str(value))
    except Exception:
        return Decimal(default)


def money(value):
    return dec(value).quantize(
        Decimal("0.01"),
        rounding=ROUND_DOWN,
    )


def floor_shares(value):
    return int(
        dec(value).to_integral_value(rounding=ROUND_DOWN)
    )


def ceil_decimal(value, places=2):
    unit = Decimal("1").scaleb(-places)
    return dec(value).quantize(
        unit,
        rounding=ROUND_CEILING,
    )


def recursive_dicts(obj):
    if isinstance(obj, dict):
        yield obj
        for value in obj.values():
            yield from recursive_dicts(value)

    elif isinstance(obj, list):
        for value in obj:
            yield from recursive_dicts(value)


def first_value(obj, keys):
    for item in recursive_dicts(obj):
        for key in keys:
            if key in item and item[key] is not None:
                return item[key]

    return None


# ============================================================
# 토스 API 요청
# ============================================================

def api_request(
    method,
    path,
    token=None,
    params=None,
    payload=None,
):
    url = f"{API_BASE_URL}{path}"

    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
    }

    if token:
        headers["Authorization"] = f"Bearer {token}"

    if ACCOUNT_SEQ:
        headers["x-tossinvest-account"] = str(ACCOUNT_SEQ)

    response = SESSION.request(
        method=method,
        url=url,
        headers=headers,
        params=params,
        json=payload,
        timeout=TIMEOUT,
    )

    log(f"API {method} {path} status={response.status_code}")

    if not response.ok:
        log(f"API 오류 응답: {response.text[:2000]}")
        response.raise_for_status()

    if not response.text.strip():
        return {}

    return response.json()


# ============================================================
# 인증 및 계좌
# ============================================================

def get_token():
    if not CLIENT_ID or not CLIENT_SECRET:
        raise RuntimeError("TOSS_CLIENT_ID 또는 TOSS_CLIENT_SECRET이 없습니다.")

    response = SESSION.post(
        f"{API_BASE_URL}/oauth2/token",
        data={
            "grant_type": "client_credentials",
            "client_id": CLIENT_ID,
            "client_secret": CLIENT_SECRET,
        },
        headers={
            "Content-Type": "application/x-www-form-urlencoded"
        },
        timeout=TIMEOUT,
    )

    log(f"토큰 발급 status={response.status_code}")

    if not response.ok:
        log(f"토큰 오류: {response.text[:1500]}")
        response.raise_for_status()

    data = response.json()
    token = data.get("access_token") or data.get("accessToken")

    if not token:
        raise RuntimeError(f"토큰 응답에 토큰이 없습니다: {data}")

    return token


def verify_account(token):
    if not ACCOUNT_SEQ:
        raise RuntimeError("TOSS_ACCOUNT_SEQ가 설정되지 않았습니다.")

    data = api_request("GET", "/api/v1/accounts", token=token)
    log("계좌 조회 성공")

    accounts = data.get("result", [])

    if isinstance(accounts, list) and accounts:
        matched = any(
            str(item.get("accountSeq")) == str(ACCOUNT_SEQ)
            for item in accounts
            if isinstance(item, dict)
        )

        if not matched:
            raise RuntimeError("TOSS_ACCOUNT_SEQ가 계좌 조회 결과와 일치하지 않습니다.")

    return data


# ============================================================
# 시세 및 계좌 데이터 (파라미터로 symbol 처리)
# ============================================================

def get_price(token, symbol):
    data = api_request(
        "GET",
        "/api/v1/prices",
        token=token,
        params={"symbols": symbol},
    )

    value = first_value(
        data,
        [
            "currentPrice",
            "current_price",
            "lastPrice",
            "last_price",
            "tradePrice",
            "trade_price",
            "price",
        ],
    )

    price = dec(value)

    if price <= 0:
        raise RuntimeError(
            f"[{symbol}] 현재가를 찾을 수 없습니다: "
            + json.dumps(data, ensure_ascii=False)[:1500]
        )

    log(f"{symbol} 현재가: ${price}")
    return price


def get_buying_power(token):
    data = api_request(
        "GET",
        "/api/v1/buying-power",
        token=token,
        params={"currency": "USD"},
    )

    value = first_value(
        data,
        [
            "cashBuyingPower",
            "cash_buying_power",
            "buyingPower",
            "buying_power",
            "availableAmount",
            "available_amount",
            "orderableAmount",
            "orderable_amount",
            "amount",
        ],
    )

    if value is None:
        raise RuntimeError(
            "매수 가능 금액 필드를 찾지 못했습니다: "
            + json.dumps(data, ensure_ascii=False)[:1500]
        )

    amount = dec(value)

    if amount < 0:
        raise RuntimeError(f"매수 가능 금액이 음수입니다: {amount}")

    log(f"USD 매수 가능 금액: ${amount}")
    return amount


def get_holding(token, symbol):
    data = api_request("GET", "/api/v1/holdings", token=token)

    for item in recursive_dicts(data):
        item_symbol = (
            item.get("symbol")
            or item.get("ticker")
            or item.get("stockCode")
        )

        if str(item_symbol).upper() != symbol.upper():
            continue

        quantity_value = (
            item.get("quantity")
            or item.get("holdingQuantity")
            or item.get("holding_quantity")
            or item.get("balanceQuantity")
            or item.get("qty")
            or 0
        )

        avg_value = (
            item.get("averagePrice")
            or item.get("average_price")
            or item.get("avgPrice")
            or item.get("avg_price")
            or item.get("purchaseAveragePrice")
            or item.get("evaluatedPrice")
            or 0
        )

        return {
            "quantity": max(0, floor_shares(quantity_value)),
            "avg_price": dec(avg_value),
        }

    return {
        "quantity": 0,
        "avg_price": Decimal("0"),
    }


def get_open_orders(token, symbol):
    data = api_request(
        "GET",
        "/api/v1/orders",
        token=token,
        params={"status": "OPEN"},
    )

    orders = []

    for item in recursive_dicts(data):
        item_symbol = (
            item.get("symbol")
            or item.get("ticker")
            or item.get("stockCode")
        )

        order_id = (
            item.get("orderId")
            or item.get("order_id")
            or item.get("id")
        )

        if str(item_symbol).upper() == symbol.upper() and order_id:
            orders.append(item)

    return orders


# ============================================================
# 상태 파일 관리
# ============================================================

def github_state_url():
    if not GITHUB_TOKEN or not GITHUB_REPOSITORY:
        return None

    return (
        f"https://api.github.com/repos/{GITHUB_REPOSITORY}"
        f"/contents/{GITHUB_STATE_PATH}"
    )


def load_state():
    url = github_state_url()

    if url:
        headers = {
            "Authorization": f"Bearer {GITHUB_TOKEN}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }

        response = SESSION.get(
            url,
            params={"ref": GITHUB_REF_NAME},
            headers=headers,
            timeout=TIMEOUT,
        )

        if response.status_code == 200:
            file_data = response.json()
            raw = base64.b64decode(file_data["content"]).decode("utf-8")
            log("GitHub 상태 파일 로드 완료")
            return json.loads(raw)

        if response.status_code == 404:
            log("GitHub 상태 파일 없음")
            return {}

        log(f"GitHub 상태 로드 오류: {response.text[:1000]}")
        response.raise_for_status()

    if STATE_FILE.exists():
        with STATE_FILE.open("r", encoding="utf-8") as file:
            return json.load(file)

    return {}


def save_state(state):
    content = json.dumps(state, ensure_ascii=False, indent=2)
    url = github_state_url()

    if not url:
        with STATE_FILE.open("w", encoding="utf-8") as file:
            file.write(content)
        log("로컬 상태 파일 저장 완료")
        return

    headers = {
        "Authorization": f"Bearer {GITHUB_TOKEN}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }

    response = SESSION.get(
        url,
        params={"ref": GITHUB_REF_NAME},
        headers=headers,
        timeout=TIMEOUT,
    )

    payload = {
        "message": "Update multi-symbol strategy state",
        "content": base64.b64encode(content.encode("utf-8")).decode("utf-8"),
        "branch": GITHUB_REF_NAME,
    }

    if response.status_code == 200:
        payload["sha"] = response.json()["sha"]
    elif response.status_code != 404:
        log(f"GitHub 상태 확인 오류: {response.text[:1000]}")
        response.raise_for_status()

    result = SESSION.put(
        url,
        headers=headers,
        json=payload,
        timeout=TIMEOUT,
    )

    if not result.ok:
        log(f"GitHub 상태 저장 오류: {result.text[:1000]}")
        result.raise_for_status()

    log("GitHub 상태 파일 저장 완료")


# ============================================================
# 주문 전송
# ============================================================

def place_order(token, symbol, side, quantity, price, loc=False, reason=""):
    quantity = int(quantity)
    price = money(price)

    if quantity <= 0 or price <= 0:
        log(f"잘못된 주문이므로 생략: {symbol} {side}, {quantity}, {price}")
        return None

    order_id_short = uuid.uuid4().hex[:16]

    payload = {
        "clientOrderId": f"{symbol.lower()}-{order_id_short}",
        "symbol": symbol,
        "side": side,
        "orderType": "LIMIT",
        "quantity": str(quantity),
        "price": str(price),
        "timeInForce": "CLS" if loc else "DAY",
    }

    log(
        f"실제 주문 전송 [{symbol}]: {side} {quantity}주 "
        f"@ ${price}; LOC={loc}; {reason}"
    )

    result = api_request(
        "POST",
        "/api/v1/orders",
        token=token,
        payload=payload,
    )

    log(f"[{symbol}] 주문 응답: " + json.dumps(result, ensure_ascii=False)[:2000])
    return result


# ============================================================
# 전략 계산
# ============================================================

def get_symbol_capital(symbol, symbol_index, state_symbol, buying_power):
    """
    종목별 원금 산정 로직:
    1. 환경변수 STRATEGY_CAPITAL_USD가 콤마 구분("1000,2000")이면 순서대로 매핑
    2. 단일값이면 동일하게 적용
    3. 지정된 바가 없으면 이전 state 설정값 사용
    4. 모두 없으면 현재 매수 가능 금액을 할당
    """
    configured_parts = [p.strip() for p in STRATEGY_CAPITAL_USD_ENV.split(",") if p.strip()]

    capital = Decimal("0")

    if configured_parts:
        if symbol_index < len(configured_parts):
            capital = dec(configured_parts[symbol_index])
        else:
            capital = dec(configured_parts[0])

    if capital <= 0 and dec(state_symbol.get("strategy_capital_usd")) > 0:
        capital = dec(state_symbol["strategy_capital_usd"])

    if capital <= 0:
        # 매수가능 금액 할당 (종목수 대비 나눔 등 원하시는 비율로 조정 가능)
        capital = buying_power
        log(f"[{symbol}] 최초 전략 원금 자동 설정: ${capital}")

    state_symbol["strategy_capital_usd"] = str(capital)

    if capital <= 0:
        raise RuntimeError(f"[{symbol}] 전략 원금이 0 이하입니다. STRATEGY_CAPITAL_USD를 설정하세요.")

    return capital


def calculate_orders(price, holding, capital):
    quantity = holding["quantity"]
    avg_price = holding["avg_price"]

    if quantity <= 0 or avg_price <= 0:
        avg_price = price

    one_buy_budget = capital / Decimal(TOTAL_STEPS)

    if one_buy_budget <= 0:
        raise RuntimeError("1회 매수 예산이 0 이하입니다.")

    if quantity > 0:
        t_value = ceil_decimal(
            Decimal(quantity) * avg_price / one_buy_budget,
            2,
        )
    else:
        t_value = Decimal("0")

    star_percent = Decimal("10") - t_value / Decimal("2")

    star_price = avg_price * (
        Decimal("1") + star_percent / Decimal("100")
    )

    sell_price_10 = avg_price * Decimal("1.10")

    buy_orders = []

    if t_value < Decimal("20"):
        half_budget = one_buy_budget / Decimal("2")

        q1 = floor_shares(half_budget / avg_price)
        q2 = (
            floor_shares(half_budget / star_price)
            if star_price > 0
            else 0
        )

        if q1 > 0:
            buy_orders.append({
                "quantity": q1,
                "price": avg_price,
                "loc": True,
                "reason": "V2.2 전반전 평균단가 매수",
            })

        if q2 > 0:
            buy_orders.append({
                "quantity": q2,
                "price": star_price,
                "loc": True,
                "reason": f"V2.2 전반전 별값 매수 T={t_value}",
            })

    else:
        q = (
            floor_shares(one_buy_budget / star_price)
            if star_price > 0
            else 0
        )

        if q > 0:
            buy_orders.append({
                "quantity": q,
                "price": star_price,
                "loc": True,
                "reason": f"V2.2 후반전 별값 매수 T={t_value}",
            })

    sell_orders = []

    if quantity > 0:
        q1 = quantity // 4
        q2 = quantity - q1

        if q1 > 0:
            sell_orders.append({
                "quantity": q1,
                "price": star_price,
                "loc": True,
                "reason": "V2.2 1/4 매도",
            })

        if q2 > 0:
            sell_orders.append({
                "quantity": q2,
                "price": sell_price_10,
                "loc": False,
                "reason": "V2.2 3/4 매도 평균단가 +10%",
            })

    return {
        "one_buy_budget": one_buy_budget,
        "T": t_value,
        "star_percent": star_percent,
        "avg_price": avg_price,
        "star_price": star_price,
        "buy_orders": buy_orders,
        "sell_orders": sell_orders,
    }


# ============================================================
# 메인 루프
# ============================================================

def main():
    notify(f"무한매수법 V2.2 멀티 종목 실행 시작 ({', '.join(SYMBOLS)})")

    state = load_state()
    token = get_token()

    verify_account(token)

    # 매수 가능 예수금은 전체 공통 공유
    buying_power = get_buying_power(token)

    # 종목별 상태 저장용 dict 초기화 체크
    if "symbols" not in state:
        state["symbols"] = {}

    summary_messages = []

    for index, symbol in enumerate(SYMBOLS):
        log("=" * 60)
        log(f"[{symbol}] 전략 처리 개시")

        if symbol not in state["symbols"]:
            state["symbols"][symbol] = {}

        sym_state = state["symbols"][symbol]

        price = get_price(token, symbol)
        holding = get_holding(token, symbol)

        log(f"[{symbol}] 보유 수량={holding['quantity']}, 평균단가=${holding['avg_price']}")

        open_orders = get_open_orders(token, symbol)

        if open_orders:
            log(f"[{symbol}] 미체결 주문 {len(open_orders)}건 발견. 스킵합니다.")
            summary_messages.append(f"[{symbol}] 미체결 주문 존재로 스킵됨")
            continue

        capital = get_symbol_capital(symbol, index, sym_state, buying_power)
        plan = calculate_orders(price, holding, capital)

        log(f"[{symbol}] 전략 원금=${capital}")
        log(f"[{symbol}] 1회 매수 예산=${plan['one_buy_budget']:.2f}")
        log(f"[{symbol}] T 추정값={plan['T']}, 별값=${money(plan['star_price'])}")

        # 1. 매도 주문
        for order in plan["sell_orders"]:
            place_order(
                token,
                symbol,
                "SELL",
                order["quantity"],
                order["price"],
                loc=order["loc"],
                reason=order["reason"],
            )

        # 2. 매수 주문
        for order in plan["buy_orders"]:
            estimated_cost = Decimal(order["quantity"]) * dec(order["price"])

            if estimated_cost > buying_power:
                log(
                    f"[{symbol}] 매수 생략: 필요 금액 ${estimated_cost:.2f} > "
                    f"잔여 매수 가능 금액 ${buying_power:.2f}"
                )
                continue

            place_order(
                token,
                symbol,
                "BUY",
                order["quantity"],
                order["price"],
                loc=order["loc"],
                reason=order["reason"],
            )

            buying_power -= estimated_cost

        # 종목별 State 업데이트
        sym_state["last_run_at"] = datetime.now().isoformat()
        sym_state["last_price"] = str(price)
        sym_state["last_T_estimated"] = str(plan["T"])
        sym_state["last_star_price"] = str(money(plan["star_price"]))

        summary_messages.append(
            f"[{symbol}] 현재가:${price} | T:{plan['T']} | 별값:${money(plan['star_price'])}"
        )

    # 전체 상태 파일 저장
    state["last_updated"] = datetime.now().isoformat()
    save_state(state)

    # 통합 메시지 알림
    notify_text = "무한매수법 V2.2 계산 완료\n" + "\n".join(summary_messages)
    notify(notify_text)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        notify(f"실행 오류: {type(exc).__name__}: {exc}")
        raise
