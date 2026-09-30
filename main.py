import os
import time
import uuid
from decimal import Decimal, ROUND_DOWN, InvalidOperation

import requests
import sys
import io
import traceback
from contextlib import redirect_stdout, redirect_stderr

# ============================================================
# 1. 환경변수 / 기본 설정
# ============================================================

CLIENT_ID = os.environ.get("TOSS_CLIENT_ID")
CLIENT_SECRET = os.environ.get("TOSS_CLIENT_SECRET")

ACCOUNT_SEQ = os.environ.get("TOSS_ACCOUNT_SEQ")

# 기존 환경변수와의 호환
if not ACCOUNT_SEQ:
    ACCOUNT_SEQ = os.environ.get("TOSS_ACCOUNT_NO")

FIXIE_URL = os.environ.get("FIXIE_URL")

# DRY_RUN이 "false"일 때만 실제 주문
DRY_RUN = (
    os.environ.get("DRY_RUN", "false")
    .strip()
    .lower()
    != "false"
)

API_BASE_URL = "https://openapi.tossinvest.com"

TOTAL_STEPS = 40
PRICE_DECIMALS = 2
TIMEOUT = 10


PORTFOLIO_CONFIG = {
    # "SNDL": {
    "494310": {
        "allocation_ratio": 1.00,
    }
}


# ============================================================
# 2. Proxy
# ============================================================

proxies = None

if FIXIE_URL:
    proxies = {
        "http": FIXIE_URL,
        "https": FIXIE_URL,
    }

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")


def send_telegram(message):
    """텔레그램 알림 전송. 알림 실패가 매매 로직을 중단시키지 않도록 처리."""
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("[TELEGRAM] 설정 누락: TELEGRAM_BOT_TOKEN 또는 TELEGRAM_CHAT_ID")
        return False

    url = (
        f"https://api.telegram.org/bot"
        f"{TELEGRAM_BOT_TOKEN}/sendMessage"
    )

    # 텔레그램 메시지 길이 제한을 고려해 잘라서 전송
    max_length = 3500
    if len(message) > max_length:
        message = "...(앞부분 생략)\n" + message[-max_length:]

    try:
        response = requests.post(
            url,
            json={
                "chat_id": TELEGRAM_CHAT_ID,
                "text": message,
            },
            proxies=proxies,
            timeout=10,
        )
        response.raise_for_status()

        result = response.json()
        if not result.get("ok"):
            print("[TELEGRAM] 전송 실패:", result)
            return False

        print("[TELEGRAM] 알림 전송 성공")
        return True

    except Exception as exc:
        # 알림 실패 때문에 매매 결과 처리를 중단하지 않음
        print(f"[TELEGRAM] 알림 전송 오류: {type(exc).__name__}: {exc}")
        return False
# ============================================================
# 3. 공통 HTTP / JSON 처리
# ============================================================

def safe_json(res):
    """
    requests.Response 또는 이미 파싱된 dict/list를 모두 처리.
    """

    if isinstance(res, (dict, list)):
        return res

    try:
        return res.json()
    except Exception:
        return {
            "_raw_text": getattr(res, "text", "")
        }


def api_error_text(res_or_data):
    """
    requests.Response / dict / list 모두 처리.
    """

    if isinstance(res_or_data, requests.Response):

        data = safe_json(res_or_data)

        if isinstance(data, dict):

            err = data.get("error")

            if isinstance(err, dict):

                code = err.get("code", "")
                message = err.get("message", "")

                result = (
                    f"{code}: {message}"
                    .strip(": ")
                )

                if result:
                    return result

            return str(data)[:3000]

        return res_or_data.text[:3000]

    data = res_or_data

    if isinstance(data, dict):

        err = data.get("error")

        if isinstance(err, dict):

            code = err.get("code", "")
            message = err.get("message", "")

            result = (
                f"{code}: {message}"
                .strip(": ")
            )

            if result:
                return result

        return str(data)[:3000]

    return str(data)[:3000]


def request_with_retry(method, url, **kwargs):

    max_retry = 3
    last_res = None

    for attempt in range(max_retry):

        try:

            res = requests.request(
                method,
                url,
                proxies=proxies,
                timeout=TIMEOUT,
                **kwargs,
            )

            last_res = res

            # 정상 또는 일반적인 4xx는 바로 반환
            if res.status_code not in (
                429,
                500,
                502,
                503,
                504,
            ):
                return res

            if attempt < max_retry - 1:

                wait_sec = 2 ** attempt

                print(
                    f"[RETRY] HTTP {res.status_code}, "
                    f"{wait_sec}초 후 재시도..."
                )

                time.sleep(wait_sec)

        except requests.RequestException as e:

            if attempt >= max_retry - 1:
                raise

            wait_sec = 2 ** attempt

            print(
                f"[RETRY] 네트워크 오류: {e} / "
                f"{wait_sec}초 후 재시도..."
            )

            time.sleep(wait_sec)

    return last_res


# ============================================================
# 4. Header
# ============================================================

def get_headers(token, account_required=False):

    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }

    if account_required:

        headers["X-Tossinvest-Account"] = str(
            ACCOUNT_SEQ
        )

    return headers


# ============================================================
# 5. 데이터 파싱 Helper
# ============================================================

def result_of(data):

    if (
        isinstance(data, dict)
        and "result" in data
    ):
        return data["result"]

    return data


def recursive_items(value):

    if isinstance(value, dict):

        yield value

        for v in value.values():
            yield from recursive_items(v)

    elif isinstance(value, list):

        for item in value:
            yield from recursive_items(item)


def to_decimal(value):

    if value is None:
        return None

    try:

        if isinstance(value, Decimal):
            return value

        text = str(value).strip()

        if not text:
            return None

        return Decimal(text)

    except (
        InvalidOperation,
        ValueError,
        TypeError,
    ):

        return None


def normalize_price(price):

    price = to_decimal(price)

    if price is None:
        return None

    return price.quantize(
        Decimal("0.01"),
        rounding=ROUND_DOWN,
    )


# ============================================================
# 6. OAuth
# ============================================================

def get_access_token():

    if not CLIENT_ID:
        raise RuntimeError(
            "TOSS_CLIENT_ID가 없습니다."
        )

    if not CLIENT_SECRET:
        raise RuntimeError(
            "TOSS_CLIENT_SECRET가 없습니다."
        )

    res = request_with_retry(
        "POST",
        f"{API_BASE_URL}/oauth2/token",
        headers={
            "Content-Type":
                "application/x-www-form-urlencoded",
        },
        data={
            "grant_type":
                "client_credentials",
            "client_id":
                CLIENT_ID,
            "client_secret":
                CLIENT_SECRET,
        },
    )

    if res.status_code >= 400:

        raise RuntimeError(
            f"OAuth 토큰 발급 실패 "
            f"({res.status_code}): "
            f"{api_error_text(res)}"
        )

    data = safe_json(res)

    token = data.get("access_token")

    if not token:

        raise RuntimeError(
            f"access_token을 찾지 못했습니다: "
            f"{data}"
        )

    print(
        "[OK] OAuth access token 발급 성공"
    )

    return token


# ============================================================
# 7. 계좌 목록
# ============================================================

def get_accounts(token):

    res = request_with_retry(
        "GET",
        f"{API_BASE_URL}/api/v1/accounts",
        headers=get_headers(token),
    )

    if res.status_code >= 400:

        raise RuntimeError(
            f"계좌 목록 조회 실패 "
            f"({res.status_code}): "
            f"{api_error_text(res)}"
        )

    return safe_json(res)


# ============================================================
# 8. Account Seq 검증
# ============================================================

def verify_account_seq(token):

    if not ACCOUNT_SEQ:

        raise RuntimeError(
            "TOSS_ACCOUNT_SEQ가 설정되어 있지 않습니다."
        )

    data = get_accounts(token)

    result = result_of(data)

    found = False

    for item in recursive_items(result):

        account_seq = item.get(
            "accountSeq"
        )

        if account_seq is None:
            continue

        if str(account_seq) == str(
            ACCOUNT_SEQ
        ):

            found = True
            break

    if not found:

        raise RuntimeError(
            f"TOSS_ACCOUNT_SEQ={ACCOUNT_SEQ} 를 "
            f"GET /api/v1/accounts에서 찾지 못했습니다."
        )

    print(
        f"[OK] accountSeq 검증 성공: "
        f"{ACCOUNT_SEQ}"
    )


# ============================================================
# 9. Buying Power
# ============================================================

def get_buying_power(
    token,
    currency="USD",
):

    res = request_with_retry(
        "GET",
        f"{API_BASE_URL}/api/v1/buying-power",
        headers=get_headers(
            token,
            account_required=True,
        ),
        params={
            "currency": currency,
        },
    )

    if res.status_code >= 400:

        raise RuntimeError(
            f"buying-power 조회 실패 "
            f"({res.status_code}): "
            f"{api_error_text(res)}"
        )

    data = safe_json(res)

    result = (
        data.get("result", {})
        if isinstance(data, dict)
        else {}
    )

    cash_buying_power = result.get(
        "cashBuyingPower"
    )

    if cash_buying_power is None:

        raise RuntimeError(
            f"buying-power 금액을 찾지 못했습니다: "
            f"{data}"
        )

    value = to_decimal(
        cash_buying_power
    )

    if value is None:

        raise RuntimeError(
            f"cashBuyingPower 파싱 실패: "
            f"{cash_buying_power}"
        )

    print(
        f"[OK] USD buying power: {value}"
    )

    return value


# ============================================================
# 10. 현재가
# ============================================================

def get_current_price(
    token,
    symbol,
):

    res = request_with_retry(
        "GET",
        f"{API_BASE_URL}/api/v1/prices",
        headers=get_headers(token),
        params={
            "symbols": symbol,
        },
    )

    if res.status_code >= 400:

        raise RuntimeError(
            f"{symbol} 현재가 조회 실패 "
            f"({res.status_code}): "
            f"{api_error_text(res)}"
        )

    data = result_of(
        safe_json(res)
    )

    if isinstance(data, list):

        for item in data:

            if not isinstance(item, dict):
                continue

            item_symbol = str(
                item.get("symbol", "")
            ).upper()

            if item_symbol != symbol.upper():
                continue

            price = to_decimal(
                item.get("lastPrice")
            )

            if (
                price is not None
                and price > 0
            ):
                return price

    elif isinstance(data, dict):

        price = to_decimal(
            data.get("lastPrice")
        )

        if (
            price is not None
            and price > 0
        ):
            return price

    raise RuntimeError(
        f"{symbol} 현재가를 찾지 못했습니다: "
        f"{data}"
    )


# ============================================================
# 11. 보유 종목
# ============================================================

def get_holdings(token):

    res = request_with_retry(
        "GET",
        f"{API_BASE_URL}/api/v1/holdings",
        headers=get_headers(
            token,
            account_required=True,
        ),
    )

    if res.status_code >= 400:

        raise RuntimeError(
            f"보유종목 조회 실패 "
            f"({res.status_code}): "
            f"{api_error_text(res)}"
        )

    return safe_json(res)


# ============================================================
# 12. 특정 종목 포지션
# ============================================================

def get_position(
    token,
    symbol,
):

    data = get_holdings(token)

    result = result_of(data)

    symbol = symbol.upper()

    for item in recursive_items(result):

        item_symbol = (
            item.get("symbol")
            or item.get("ticker")
        )

        if not item_symbol:
            continue

        if (
            str(item_symbol).upper()
            != symbol
        ):
            continue

        quantity_raw = item.get(
            "quantity"
        )

        if quantity_raw is None:
            quantity_raw = item.get(
                "shares"
            )

        if quantity_raw is None:
            quantity_raw = item.get(
                "holdingQuantity"
            )

        avg_price_raw = item.get(
            "averagePrice"
        )

        if avg_price_raw is None:
            avg_price_raw = item.get(
                "avgPrice"
            )

        if avg_price_raw is None:
            avg_price_raw = item.get(
                "averagePurchasePrice"
            )

        quantity = to_decimal(
            quantity_raw
        )

        avg_price = to_decimal(
            avg_price_raw
        )

        if quantity is None:
            quantity = Decimal("0")

        print(
            f"[POSITION] {symbol}: "
            f"quantity={quantity}, "
            f"avg_price={avg_price}"
        )

        return {
            "symbol": symbol,
            "quantity": quantity,
            "avg_price": avg_price,
        }

    print(
        f"[POSITION] {symbol}: 보유하지 않음"
    )

    return {
        "symbol": symbol,
        "quantity": Decimal("0"),
        "avg_price": None,
    }


# ============================================================
# 13. 주문 목록
#
# 중요:
# Toss API에서 status 필드가 필수.
#
# OPEN = 진행 중 / 미체결 주문 조회
# ============================================================

def get_orders(token):

    res = request_with_retry(
        "GET",
        f"{API_BASE_URL}/api/v1/orders",
        headers=get_headers(
            token,
            account_required=True,
        ),
        params={
            "status": "OPEN",
        },
    )

    if res.status_code >= 400:

        print(
            "[DEBUG] orders HTTP status:",
            res.status_code,
        )

        print(
            "[DEBUG] orders response:",
            res.text[:5000],
        )

        raise RuntimeError(
            f"주문 목록 조회 실패 "
            f"({res.status_code}): "
            f"{api_error_text(res)}"
        )

    data = safe_json(res)

    print(
        "[DEBUG] orders response:",
        data,
    )

    return data


# ============================================================
# 14. 미체결 주문 확인
# ============================================================

def get_pending_orders_for_symbol(
    token,
    symbol,
):

    data = get_orders(token)

    result = result_of(data)

    symbol = symbol.upper()

    pending = []

    for item in recursive_items(result):

        item_symbol = (
            item.get("symbol")
            or item.get("ticker")
        )

        if not item_symbol:
            continue

        if (
            str(item_symbol).upper()
            != symbol
        ):
            continue

        # status=OPEN으로 이미 서버에서
        # 진행 중 주문만 조회했으므로
        # 여기서는 종목만 확인.
        pending.append(item)

    print(
        f"[ORDER] {symbol} pending orders: "
        f"{len(pending)}"
    )

    return pending


# ============================================================
# 15. 주문 생성
# ============================================================

def place_order(
    token,
    symbol,
    side,
    quantity,
    price,
):

    quantity = int(quantity)

    price = normalize_price(price)

    if quantity <= 0:

        print(
            f"[SKIP] 주문수량 0: "
            f"{symbol} {side}"
        )

        return None

    if (
        price is None
        or price <= 0
    ):

        raise RuntimeError(
            f"주문가격이 올바르지 않습니다: "
            f"{price}"
        )

    payload = {
        "symbol": symbol,
        "side": side,
        "orderType": "LIMIT",
        "price": str(price),
        "quantity": quantity,
        "clientOrderId": str(uuid.uuid4()),
    }

    # --------------------------------------------------------
    # DRY RUN
    # --------------------------------------------------------

    if DRY_RUN:

        print(
            "[DRY_RUN] 실제 주문 전송 안 함"
        )

        print(
            "[DRY_RUN] POST /api/v1/orders"
        )

        print(
            "[DRY_RUN] payload:",
            payload,
        )

        return {
            "dryRun": True,
            "payload": payload,
        }

    # --------------------------------------------------------
    # LIVE
    # --------------------------------------------------------

    res = request_with_retry(
        "POST",
        f"{API_BASE_URL}/api/v1/orders",
        headers=get_headers(
            token,
            account_required=True,
        ),
        json=payload,
    )

    if res.status_code >= 400:

        print(
            "[DEBUG] order HTTP status:",
            res.status_code,
        )

        print(
            "[DEBUG] order response:",
            res.text[:5000],
        )

        raise RuntimeError(
            f"주문 실패 "
            f"({res.status_code}): "
            f"{api_error_text(res)}"
        )

    data = safe_json(res)

    print(
        f"[ORDER OK] "
        f"{symbol} {side} "
        f"{quantity}주 @ {price}"
    )

    print(
        "[ORDER RESPONSE]",
        data,
    )

    return data


# ============================================================
# 16. 전략 계산
# ============================================================

def calculate_strategy(
    total_account_value,
    current_price,
    position,
    allocation_ratio,
):

    one_buy_budget = (
        total_account_value
        * Decimal(
            str(allocation_ratio)
        )
        / Decimal(TOTAL_STEPS)
    )

    quantity = position["quantity"]
    avg_price = position["avg_price"]

    # ========================================================
    # 신규 진입
    # ========================================================

    if (
        quantity <= 0
        or avg_price is None
    ):

        buy_quantity = int(
            one_buy_budget
            / current_price
        )

        return {
            "mode": "NEW",
            "one_buy_budget":
                one_buy_budget,
            "buy_quantity":
                buy_quantity,
            "buy_price":
                current_price,
        }

    # ========================================================
    # 기존 보유
    # ========================================================

    if one_buy_budget > 0:

        t_turn = (
            quantity
            * avg_price
            / one_buy_budget
        )

    else:

        t_turn = Decimal("0")

    star_percent = max(
        Decimal("10")
        - t_turn
        * Decimal("0.25"),
        Decimal("0"),
    )

    target_sell_price = (
        avg_price
        * (
            Decimal("1")
            + star_percent
            / Decimal("100")
        )
    )

    target_sell_price = normalize_price(
        target_sell_price
    )

    # ========================================================
    # 매수 전략
    # ========================================================

    if t_turn < Decimal("20"):

        budget_1 = (
            one_buy_budget
            / Decimal("2")
        )

        budget_2 = (
            one_buy_budget
            / Decimal("2")
        )

        buy_quantity_1 = int(
            budget_1
            / avg_price
        )

        buy_quantity_2 = int(
            budget_2
            / (
                avg_price
                * Decimal("1.05")
            )
        )

        buy_price_1 = normalize_price(
            avg_price
        )

        buy_price_2 = normalize_price(
            avg_price
            * Decimal("1.05")
        )

    else:

        buy_quantity_1 = int(
            one_buy_budget
            / avg_price
        )

        buy_quantity_2 = 0

        buy_price_1 = normalize_price(
            avg_price
        )

        buy_price_2 = None

    return {
        "mode": "HOLDING",

        "one_buy_budget":
            one_buy_budget,

        "quantity":
            quantity,

        "avg_price":
            avg_price,

        "t_turn":
            t_turn,

        "star_percent":
            star_percent,

        "target_sell_price":
            target_sell_price,

        "buy_quantity_1":
            buy_quantity_1,

        "buy_price_1":
            buy_price_1,

        "buy_quantity_2":
            buy_quantity_2,

        "buy_price_2":
            buy_price_2,
    }


# ============================================================
# 17. 종목별 실행
# ============================================================

def run_symbol(
    token,
    symbol,
    total_account_value,
):

    config = PORTFOLIO_CONFIG[
        symbol
    ]

    allocation_ratio = Decimal(
        str(
            config[
                "allocation_ratio"
            ]
        )
    )

    print()
    print("=" * 60)
    print(
        f"[SYMBOL] {symbol}"
    )
    print("=" * 60)

    # --------------------------------------------------------
    # 현재가
    # --------------------------------------------------------

    current_price = get_current_price(
        token,
        symbol,
    )

    print(
        f"[PRICE] {symbol}: "
        f"{current_price}"
    )

    # --------------------------------------------------------
    # 보유
    # --------------------------------------------------------

    position = get_position(
        token,
        symbol,
    )

    # --------------------------------------------------------
    # 미체결 주문
    # --------------------------------------------------------

    pending_orders = (
        get_pending_orders_for_symbol(
            token,
            symbol,
        )
    )

    if pending_orders:

        print(
            f"[SKIP] {symbol}: "
            f"미체결 주문이 "
            f"{len(pending_orders)}개 "
            f"있어서 신규 주문을 만들지 않습니다."
        )

        return

    # --------------------------------------------------------
    # 전략 계산
    # --------------------------------------------------------

    strategy = calculate_strategy(
        total_account_value=
            total_account_value,
        current_price=
            current_price,
        position=
            position,
        allocation_ratio=
            allocation_ratio,
    )

    print()
    print("[STRATEGY]")
    print(strategy)

    # ========================================================
    # 신규 진입
    # ========================================================

    if strategy["mode"] == "NEW":

        buy_quantity = strategy[
            "buy_quantity"
        ]

        buy_price = strategy[
            "buy_price"
        ]

        print(
            f"[NEW BUY] "
            f"{symbol} "
            f"{buy_quantity}주 "
            f"@ {buy_price}"
        )

        if buy_quantity > 0:

            place_order(
                token=token,
                symbol=symbol,
                side="BUY",
                quantity=buy_quantity,
                price=buy_price,
            )

        else:

            print(
                "[SKIP] 1회 매수예산으로 "
                "1주도 매수할 수 없습니다."
            )

        return

    # ========================================================
    # 기존 보유
    # ========================================================

    quantity = strategy[
        "quantity"
    ]

    # --------------------------------------------------------
    # 매도 목표
    # --------------------------------------------------------

    sell_price = strategy[
        "target_sell_price"
    ]

    if quantity > 0:

        print(
            f"[SELL TARGET] "
            f"{symbol} "
            f"{quantity}주 "
            f"@ {sell_price}"
        )

        place_order(
            token=token,
            symbol=symbol,
            side="SELL",
            quantity=int(quantity),
            price=sell_price,
        )

    # --------------------------------------------------------
    # LIVE에서는 같은 실행에서
    # 매도 + 매수를 동시에 넣지 않음
    # --------------------------------------------------------

    if not DRY_RUN:

        print(
            "[LIVE] 매도 주문을 제출했으므로 "
            "이번 실행에서는 추가 매수를 넣지 않습니다."
        )

        return

    # ========================================================
    # DRY RUN 매수 계산 출력
    # ========================================================

    buy_quantity_1 = strategy[
        "buy_quantity_1"
    ]

    buy_price_1 = strategy[
        "buy_price_1"
    ]

    if buy_quantity_1 > 0:

        print(
            f"[DRY_RUN BUY 1] "
            f"{symbol} "
            f"{buy_quantity_1}주 "
            f"@ {buy_price_1}"
        )

        place_order(
            token=token,
            symbol=symbol,
            side="BUY",
            quantity=buy_quantity_1,
            price=buy_price_1,
        )

    buy_quantity_2 = strategy[
        "buy_quantity_2"
    ]

    buy_price_2 = strategy[
        "buy_price_2"
    ]

    if (
        buy_quantity_2 > 0
        and buy_price_2 is not None
    ):

        print(
            f"[DRY_RUN BUY 2] "
            f"{symbol} "
            f"{buy_quantity_2}주 "
            f"@ {buy_price_2}"
        )

        place_order(
            token=token,
            symbol=symbol,
            side="BUY",
            quantity=buy_quantity_2,
            price=buy_price_2,
        )


# ============================================================
# 18. Main
# ============================================================

def run_trading():

    print("=" * 60)
    print(
        "SNDL Toss Securities Auto Trading"
    )
    print("=" * 60)

    print(
        f"[CONFIG] DRY_RUN={DRY_RUN}"
    )

    print(
        f"[CONFIG] ACCOUNT_SEQ="
        f"{ACCOUNT_SEQ}"
    )

    print(
        f"[CONFIG] FIXIE="
        f"{'사용' if FIXIE_URL else '미사용'}"
    )

    # --------------------------------------------------------
    # 환경변수 확인
    # --------------------------------------------------------

    if not CLIENT_ID:

        raise RuntimeError(
            "TOSS_CLIENT_ID가 없습니다."
        )

    if not CLIENT_SECRET:

        raise RuntimeError(
            "TOSS_CLIENT_SECRET가 없습니다."
        )

    if not ACCOUNT_SEQ:

        raise RuntimeError(
            "TOSS_ACCOUNT_SEQ가 없습니다."
        )

    # --------------------------------------------------------
    # OAuth
    # --------------------------------------------------------

    token = get_access_token()

    # --------------------------------------------------------
    # Account Seq 검증
    # --------------------------------------------------------

    verify_account_seq(token)

    # --------------------------------------------------------
    # Buying Power
    # --------------------------------------------------------

    buying_power = get_buying_power(
        token,
        currency="USD",
    )

    total_account_value = (
        buying_power
    )

    print(
        f"[ACCOUNT] "
        f"전략 기준금액 = "
        f"{total_account_value} USD"
    )

    # --------------------------------------------------------
    # 종목별 실행
    # --------------------------------------------------------

    for symbol in PORTFOLIO_CONFIG:

        try:

            run_symbol(
                token=token,
                symbol=symbol,
                total_account_value=
                    total_account_value,
            )

        except Exception as e:

            print(
                f"[ERROR] {symbol}: {e}"
            )

            raise

    print()
    print("=" * 60)
    print("실행 완료")
    print("=" * 60)

class LogTee(io.StringIO):
    """실행 로그를 GitHub Actions와 메모리에 동시에 기록."""

    def __init__(self, original):
        super().__init__()
        self.original = original

    def write(self, text):
        self.original.write(text)
        self.original.flush()
        return super().write(text)

    def flush(self):
        self.original.flush()


def main():
    log_buffer = io.StringIO()
    stdout_tee = LogTee(sys.stdout)
    stderr_tee = LogTee(sys.stderr)

    success = False

    try:
        with redirect_stdout(stdout_tee), redirect_stderr(stderr_tee):
            try:
                print("=== SNDL 자동매매 시작 ===")
                print(f"[CONFIG] DRY_RUN={DRY_RUN}")

                run_trading()
                success = True

                print("=== SNDL 자동매매 정상 종료 ===")

            except Exception:
                print("=== SNDL 자동매매 오류 ===")
                traceback.print_exc()

    finally:
        # stdout/stderr 양쪽의 로그를 합쳐 알림
        log_text = stdout_tee.getvalue() + stderr_tee.getvalue()

        status = "정상 종료" if success else "오류 발생"
        mode = "DRY_RUN (모의 실행)" if DRY_RUN else "LIVE (실주문 모드)"

        message = (
            f"[SNDL 자동매매] {status}\n"
            f"모드: {mode}\n\n"
            f"{log_text[-3500:]}"
        )

        send_telegram(message)

        stdout_tee.close()
        stderr_tee.close()


# ============================================================
# 19. Entry Point
# ============================================================

if __name__ == "__main__":
    main()