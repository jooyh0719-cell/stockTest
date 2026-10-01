import os
import json
import uuid
import base64
import requests

from decimal import Decimal, ROUND_DOWN, ROUND_CEILING
from datetime import datetime
from pathlib import Path


# ============================================================
# 라오어 무한매수법 V2.2 테스트용 - BULZ
#
# 실제 주문 전송 버전
#
# 주의:
# - 주문 요청은 실제 계좌에 전송된다.
# - LOC 주문 필드 및 API 응답 구조는 별도 검증 필요.
# - T는 현재 보유 원가를 기준으로 추정한다.
# - 원금 소진 시 쿼터 매도 로직은 완전 구현되지 않았다.
# ============================================================


# ------------------------------------------------------------
# 1. 환경변수
# ------------------------------------------------------------

CLIENT_ID = os.getenv("TOSS_CLIENT_ID", "")
CLIENT_SECRET = os.getenv("TOSS_CLIENT_SECRET", "")
ACCOUNT_SEQ = os.getenv("TOSS_ACCOUNT_SEQ", "")

FIXIE_URL = os.getenv("FIXIE_URL", "")

API_BASE_URL = "https://openapi.tossinvest.com"

SYMBOL = "BULZ"
TOTAL_STEPS = 40

# 전략에 배정할 고정 원금(USD).
# 설정하지 않으면 최초 실행 시 USD 매수 가능 금액을 기준으로 사용.
STRATEGY_CAPITAL_USD = os.getenv(
    "STRATEGY_CAPITAL_USD", ""
).strip()

# 상태 파일
STATE_FILE = Path("strategy_state.json")

# GitHub Actions 상태 유지용
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN", "")
GITHUB_REPOSITORY = os.getenv("GITHUB_REPOSITORY", "")
GITHUB_REF_NAME = os.getenv("GITHUB_REF_NAME", "main")
GITHUB_STATE_PATH = os.getenv(
    "GITHUB_STATE_PATH", "strategy_state.json"
)

# 텔레그램 알림(선택)
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

TIMEOUT = 20

SESSION = requests.Session()

if FIXIE_URL:
    SESSION.proxies.update({
        "http": FIXIE_URL,
        "https": FIXIE_URL,
    })


# ------------------------------------------------------------
# 2. 공통 함수
# ------------------------------------------------------------

def log(message):
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{now}] {message}", flush=True)


def notify(message):
    log(message)

    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return

    try:
        SESSION.post(
            f"https://api.telegram.org/"
            f"bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            json={
                "chat_id": TELEGRAM_CHAT_ID,
                "text": message,
            },
            timeout=TIMEOUT,
        )
    except Exception as exc:
        log(f"텔레그램 알림 실패: {exc}")


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


def first_value(obj, keys, default=None):
    for item in recursive_dicts(obj):
        for key in keys:
            if key in item and item[key] is not None:
                return item[key]

    return default


def api_request(
    method,
    path,
    *,
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

    response = SESSION.request(
        method=method,
        url=url,
        headers=headers,
        params=params,
        json=payload,
        timeout=TIMEOUT,
    )

    log(
        f"API {method} {path} "
        f"status={response.status_code}"
    )

    if not response.ok:
        log(f"API 오류 응답: {response.text[:2000]}")
        response.raise_for_status()

    if not response.text.strip():
        return {}

    return response.json()


# ------------------------------------------------------------
# 3. 상태 저장
# ------------------------------------------------------------

def github_state_url():
    if not GITHUB_REPOSITORY or not GITHUB_TOKEN:
        return None

    return (
        "https://api.github.com/repos/"
        f"{GITHUB_REPOSITORY}/contents/{GITHUB_STATE_PATH}"
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
            data = response.json()

            content = base64.b64decode(
                data["content"]
            ).decode("utf-8")

            state = json.loads(content)
            log("GitHub 상태 파일 로드 완료")

            return state

        if response.status_code == 404:
            log("GitHub 상태 파일 없음: 최초 생성")
            return {}

        log(f"GitHub 상태 조회 실패: {response.text[:1000]}")
        response.raise_for_status()

    if STATE_FILE.exists():
        with STATE_FILE.open("r", encoding="utf-8") as file:
            return json.load(file)

    return {}


def save_state(state):
    text_content = json.dumps(
        state,
        ensure_ascii=False,
        indent=2,
    )

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

        payload = {
            "message": "Update BULZ strategy state",
            "content": base64.b64encode(
                text_content.encode("utf-8")
            ).decode("utf-8"),
            "branch": GITHUB_REF_NAME,
        }

        if response.status_code == 200:
            payload["sha"] = response.json()["sha"]
        elif response.status_code != 404:
            log(f"GitHub 상태 확인 실패: {response.text[:1000]}")
            response.raise_for_status()

        result = SESSION.put(
            url,
            headers=headers,
            json=payload,
            timeout=TIMEOUT,
        )

        if not result.ok:
            log(f"GitHub 상태 저장 실패: {result.text[:1000]}")
            result.raise_for_status()

        log("GitHub 상태 파일 저장 완료")
        return

    with STATE_FILE.open("w", encoding="utf-8") as file:
        file.write(text_content)

    log("로컬 상태 파일 저장 완료")


# ------------------------------------------------------------
# 4. 토큰 및 계좌
# ------------------------------------------------------------

def get_token():
    if not CLIENT_ID or not CLIENT_SECRET:
        raise RuntimeError(
            "TOSS_CLIENT_ID와 TOSS_CLIENT_SECRET을 설정해야 합니다."
        )

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
        log(f"토큰 발급 실패: {response.text[:1000]}")
        response.raise_for_status()

    data = response.json()

    token = (
        data.get("access_token")
        or data.get("accessToken")
    )

    if not token:
        raise RuntimeError(
            f"토큰 응답에 토큰이 없습니다: {data}"
        )

    return token


def verify_account(token):
    if not ACCOUNT_SEQ:
        raise RuntimeError(
            "TOSS_ACCOUNT_SEQ 환경변수를 설정해야 합니다."
        )

    data = api_request(
        "GET",
        "/api/v1/accounts",
        token=token,
    )

    log(
        "계좌 조회 응답: "
        + json.dumps(data, ensure_ascii=False)[:1500]
    )

    return data


# ------------------------------------------------------------
# 5. 시세 및 계좌 정보
# ------------------------------------------------------------

def get_price(token):
    data = api_request(
        "GET",
        "/api/v1/prices",
        token=token,
        params={"symbols": SYMBOL},
    )

    price = first_value(
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

    price = dec(price)

    if price <= 0:
        raise RuntimeError(
            "현재가를 응답에서 찾지 못했습니다: "
            + json.dumps(data, ensure_ascii=False)[:1500]
        )

    log(f"{SYMBOL} 현재가: ${price}")

    return price


def get_buying_power(token):
    data = api_request(
        "GET",
        "/api/v1/buying-power",
        token=token,
        params={"currency": "USD"},
    )

    amount = first_value(
        data,
        [
            "buyingPower",
            "buying_power",
            "availableAmount",
            "available_amount",
            "orderableAmount",
            "orderable_amount",
            "amount",
        ],
    )

    amount = dec(amount)

    if amount < 0:
        raise RuntimeError(
            f"매수 가능 금액 오류: {data}"
        )

    log(f"USD 매수 가능 금액: ${amount}")

    return amount


def get_bulz_holding(token):
    data = api_request(
        "GET",
        "/api/v1/holdings",
        token=token,
    )

    for item in recursive_dicts(data):
        symbol = (
            item.get("symbol")
            or item.get("ticker")
            or item.get("stockCode")
        )

        if str(symbol).upper() != SYMBOL:
            continue

        quantity = dec(
            item.get("quantity")
            or item.get("holdingQuantity")
            or item.get("holding_quantity")
            or item.get("qty")
        )

        avg_price = dec(
            item.get("averagePrice")
            or item.get("average_price")
            or item.get("avgPrice")
            or item.get("avg_price")
            or item.get("purchaseAveragePrice")
        )

        return {
            "quantity": max(0, floor_shares(quantity)),
            "avg_price": avg_price,
            "raw": item,
        }

    return {
        "quantity": 0,
        "avg_price": Decimal("0"),
        "raw": {},
    }


def get_open_orders(token):
    data = api_request(
        "GET",
        "/api/v1/orders",
        token=token,
        params={"status": "OPEN"},
    )

    orders = []

    for item in recursive_dicts(data):
        symbol = (
            item.get("symbol")
            or item.get("ticker")
            or item.get("stockCode")
        )

        if str(symbol).upper() != SYMBOL:
            continue

        order_id = (
            item.get("orderId")
            or item.get("order_id")
            or item.get("id")
        )

        if order_id:
            orders.append(item)

    return orders


# ------------------------------------------------------------
# 6. 실제 주문 전송
# ------------------------------------------------------------

def place_order(
    token,
    side,
    quantity,
    price,
    *,
    loc=False,
    reason="",
):
    quantity = int(quantity)
    price = money(price)

    if quantity <= 0 or price <= 0:
        log(
            f"주문 생략: side={side}, "
            f"quantity={quantity}, price={price}"
        )
        return None

    payload = {
        "clientOrderId": (
            f"bulz-{side.lower()}-{uuid.uuid4().hex[:20]}"
        ),
        "symbol": SYMBOL,
        "side": side,
        "orderType": "LIMIT",
        "quantity": str(quantity),
        "price": str(price),
        "timeInForce": "CLS" if loc else "DAY",
    }

    order_label = "LOC" if loc else "일반 지정가"

    log(
        f"실주문 전송: {side} {quantity}주 @ ${price} "
        f"[{order_label}] / {reason}"
    )

    result = api_request(
        "POST",
        "/api/v1/orders",
        token=token,
        payload=payload,
    )

    log(
        "주문 응답: "
        + json.dumps(result, ensure_ascii=False)[:2000]
    )

    return result


# ------------------------------------------------------------
# 7. V2.2 전략 계산
# ------------------------------------------------------------

def get_strategy_capital(state, buying_power):
    configured = dec(STRATEGY_CAPITAL_USD)

    if configured > 0:
        capital = configured

    elif dec(state.get("strategy_capital_usd")) > 0:
        capital = dec(state["strategy_capital_usd"])

    else:
        capital = buying_power

    if capital <= 0:
        raise RuntimeError(
            "전략 원금이 0 이하입니다. "
            "STRATEGY_CAPITAL_USD를 확인하세요."
        )

    if not state.get("strategy_capital_usd"):
        state["strategy_capital_usd"] = str(capital)
        log(f"전략 기준 원금 최초 설정: ${capital}")

    return capital


def calculate_orders(price, holding, capital):
    quantity = holding["quantity"]
    avg_price = holding["avg_price"]

    if quantity <= 0 or avg_price <= 0:
        avg_price = price

    one_buy_budget = capital / Decimal(TOTAL_STEPS)

    if one_buy_budget <= 0:
        raise RuntimeError("1회 매수 예산이 0 이하입니다.")

    # 현재 보유분 원가로 계산한 추정 T.
    # 실제 누적 체결액 기반의 T와는 차이가 발생할 수 있다.
    if quantity > 0 and avg_price > 0:
        current_cost_basis = Decimal(quantity) * avg_price

        t_value = ceil_decimal(
            current_cost_basis / one_buy_budget,
            2,
        )
    else:
        t_value = Decimal("0")

    star_percent = Decimal("10") - (
        t_value / Decimal("2")
    )

    star_price = avg_price * (
        Decimal("1") + star_percent / Decimal("100")
    )

    sell_10_percent_price = (
        avg_price * Decimal("1.10")
    )

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
                "reason": "V2.2 전반전: 평균단가 LOC",
            })

        if q2 > 0:
            buy_orders.append({
                "quantity": q2,
                "price": star_price,
                "loc": True,
                "reason": (
                    f"V2.2 전반전: T={t_value}, 별값 LOC"
                ),
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
                "reason": (
                    f"V2.2 후반전: T={t_value}, 별값 LOC"
                ),
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
                "reason": f"V2.2 1/4 매도: T={t_value}",
            })

        if q2 > 0:
            sell_orders.append({
                "quantity": q2,
                "price": sell_10_percent_price,
                "loc": False,
                "reason": "V2.2 3/4 매도: 평균단가 +10%",
            })

    return {
        "one_buy_budget": one_buy_budget,
        "T_estimated": t_value,
        "star_percent": star_percent,
        "avg_price": avg_price,
        "star_price": star_price,
        "buy_orders": buy_orders,
        "sell_orders": sell_orders,
    }


# ------------------------------------------------------------
# 8. 메인 실행
# ------------------------------------------------------------

def main():
    notify("BULZ V2.2 실행 시작 - 실제 주문 모드")

    state = load_state()
    token = get_token()

    verify_account(token)

    price = get_price(token)
    buying_power = get_buying_power(token)
    holding = get_bulz_holding(token)

    log(
        f"보유 현황: quantity={holding['quantity']}, "
        f"avg_price=${holding['avg_price']}"
    )

    # 기존 미체결 주문이 있으면 중복 주문 방지
    open_orders = get_open_orders(token)

    if open_orders:
        log(
            f"{SYMBOL} 미체결 주문 {len(open_orders)}건 발견. "
            "중복 주문 방지를 위해 실행을 중단합니다."
        )

        log(
            "미체결 주문: "
            + json.dumps(open_orders, ensure_ascii=False)[:2000]
        )

        return

    capital = get_strategy_capital(
        state,
        buying_power,
    )

    plan = calculate_orders(
        price=price,
        holding=holding,
        capital=capital,
    )

    log("=" * 55)
    log(f"종목: {SYMBOL}")
    log(f"전략 원금: ${capital}")
    log(f"1회 매수 예산: ${plan['one_buy_budget']:.2f}")
    log(f"현재가: ${price}")
    log(f"평균단가 기준: ${plan['avg_price']}")
    log(f"T 추정값: {plan['T_estimated']}")
    log(f"별값(%): {plan['star_percent']}%")
    log(f"별값 가격: ${money(plan['star_price'])}")
    log("=" * 55)

    # 매도 주문
    for order in plan["sell_orders"]:
        place_order(
            token,
            "SELL",
            order["quantity"],
            order["price"],
            loc=order["loc"],
            reason=order["reason"],
        )

    # 매수 주문
    for order in plan["buy_orders"]:
        estimated_cost = (
            Decimal(order["quantity"])
            * dec(order["price"])
        )

        if estimated_cost > buying_power:
            log(
                f"매수 주문 생략: 예상 금액 "
                f"${estimated_cost:.2f}가 매수 가능 금액 "
                f"${buying_power:.2f}를 초과"
            )
            continue

        result = place_order(
            token,
            "BUY",
            order["quantity"],
            order["price"],
            loc=order["loc"],
            reason=order["reason"],
        )

        if result is not None:
            buying_power -= estimated_cost

    state["last_run_at"] = datetime.now().isoformat()
    state["last_price"] = str(price)
    state["last_T_estimated"] = str(plan["T_estimated"])
    state["last_star_price"] = str(
        money(plan["star_price"])
    )

    save_state(state)

    notify(
        f"BULZ V2.2 계산 완료\n"
        f"현재가: ${price}\n"
        f"보유 수량: {holding['quantity']}\n"
        f"T 추정값: {plan['T_estimated']}\n"
        f"별값: ${money(plan['star_price'])}"
    )


if __name__ == "__main__":
    try:
        main()

    except Exception as exc:
        notify(
            f"BULZ V2.2 실행 오류: "
            f"{type(exc).__name__}: {exc}"
        )
        raise