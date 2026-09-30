
import os
import sys
import io
import time
import uuid
import traceback
from decimal import Decimal, ROUND_DOWN, InvalidOperation
from contextlib import redirect_stdout, redirect_stderr

import requests


# ============================================================
# 1. 환경변수 / 기본 설정
# ============================================================

CLIENT_ID = os.environ.get("TOSS_CLIENT_ID")
CLIENT_SECRET = os.environ.get("TOSS_CLIENT_SECRET")

ACCOUNT_SEQ = (
    os.environ.get("TOSS_ACCOUNT_SEQ")
    or os.environ.get("TOSS_ACCOUNT_NO")
)

FIXIE_URL = os.environ.get("FIXIE_URL")

# 기존 동작 유지:
# DRY_RUN 환경변수가 없거나 "false"이면 LIVE 모드.
# 실주문 검증 전에는 GitHub Actions에서 DRY_RUN=true로 설정.
DRY_RUN = (
    os.environ.get("DRY_RUN", "false").strip().lower()
    != "false"
)

API_BASE_URL = "https://openapi.tossinvest.com"

TOTAL_STEPS = 40
TIMEOUT = 10

# 통화별 가격 정밀도
PRICE_DECIMALS_BY_CURRENCY = {
    "USD": 2,
    "KRW": 0,
}

# ============================================================
# 종목별 설정
#
# currency:
#   국내주식 = KRW
#   미국주식 = USD
#
# allocation_ratio:
#   해당 통화의 매수 가능 금액 중 전략에 배분할 비율.
#
# 같은 통화를 사용하는 종목들의 allocation_ratio 합은
# 1.0 이하로 설정한다.
# ============================================================

PORTFOLIO_CONFIG = {
    "494310": {
        "currency": "KRW",
        "allocation_ratio": 1.00,
    },

    # 미국주식을 추가할 때 아래와 같이 등록
    # "SNDL": {
    #     "currency": "USD",
    #     "allocation_ratio": 1.00,
    # },
}


# ============================================================
# 2. Proxy / Telegram
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
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print(
            "[TELEGRAM] 설정 누락: "
            "TELEGRAM_BOT_TOKEN 또는 TELEGRAM_CHAT_ID"
        )
        return False

    url = (
        f"https://api.telegram.org/bot"
        f"{TELEGRAM_BOT_TOKEN}/sendMessage"
    )

    # Telegram 메시지 길이 제한 고려
    if len(message) > 3500:
        message = "...(앞부분 생략)\n" + message[-3500:]

    try:
        response = requests.post(
            url,
            json={
                "chat_id": TELEGRAM_CHAT_ID,
                "text": message,
            },
            proxies=proxies,
            timeout=TIMEOUT,
        )
        response.raise_for_status()

        result = response.json()

        if not result.get("ok"):
            print("[TELEGRAM] 전송 실패")
            return False

        print("[TELEGRAM] 알림 전송 성공")
        return True

    except Exception as exc:
        # 예외 메시지에 토큰이 포함될 가능성을 피한다.
        print(
            f"[TELEGRAM] 전송 오류: "
            f"{type(exc).__name__}"
        )
        return False


# ============================================================
# 3. 공통 HTTP / JSON
# ============================================================

def safe_json(res):
    if isinstance(res, (dict, list)):
        return res

    try:
        return res.json()
    except Exception:
        return {
            "_raw_text": getattr(res, "text", "")
        }


def api_error_text(res_or_data):
    if isinstance(res_or_data, requests.Response):
        data = safe_json(res_or_data)
    else:
        data = res_or_data

    if isinstance(data, dict):
        err = data.get("error")

        if isinstance(err, dict):
            code = err.get("code", "")
            message = err.get("message", "")
            text = f"{code}: {message}".strip(": ")

            if text:
                return text

    if isinstance(res_or_data, requests.Response):
        return res_or_data.text[:3000]

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

            if res.status_code not in (
                429, 500, 502, 503, 504
            ):
                return res

            if attempt < max_retry - 1:
                wait_sec = 2 ** attempt
                print(
                    f"[RETRY] HTTP {res.status_code}, "
                    f"{wait_sec}초 후 재시도"
                )
                time.sleep(wait_sec)

        except requests.RequestException as exc:
            if attempt >= max_retry - 1:
                raise

            wait_sec = 2 ** attempt
            print(
                f"[RETRY] 네트워크 오류: "
                f"{type(exc).__name__}, "
                f"{wait_sec}초 후 재시도"
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
        headers["X-Tossinvest-Account"] = str(ACCOUNT_SEQ)

    return headers


# ============================================================
# 5. 데이터 파싱
# ============================================================

def result_of(data):
    if isinstance(data, dict) and "result" in data:
        return data["result"]

    return data


def recursive_items(value):
    if isinstance(value, dict):
        yield value

        for child in value.values():
            yield from recursive_items(child)

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

    except (InvalidOperation, ValueError, TypeError):
        return None


def normalize_price(price, currency):
    price = to_decimal(price)

    if price is None:
        return None

    currency = currency.upper()

    if currency not in PRICE_DECIMALS_BY_CURRENCY:
        raise ValueError(
            f"지원하지 않는 통화: {currency}"
        )

    decimals = PRICE_DECIMALS_BY_CURRENCY[currency]
    quantum = Decimal("1").scaleb(-decimals)

    return price.quantize(
        quantum,
        rounding=ROUND_DOWN,
    )


# ============================================================
# 6. OAuth
# ============================================================

def get_access_token():
    if not CLIENT_ID:
        raise RuntimeError("TOSS_CLIENT_ID가 없습니다.")

    if not CLIENT_SECRET:
        raise RuntimeError("TOSS_CLIENT_SECRET가 없습니다.")

    res = request_with_retry(
        "POST",
        f"{API_BASE_URL}/oauth2/token",
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
        },
        data={
            "grant_type": "client_credentials",
            "client_id": CLIENT_ID,
            "client_secret": CLIENT_SECRET,
        },
    )

    if res.status_code >= 400:
        raise RuntimeError(
            f"OAuth 토큰 발급 실패 ({res.status_code}): "
            f"{api_error_text(res)}"
        )

    data = safe_json(res)
    token = data.get("access_token")

    if not token:
        raise RuntimeError(
            f"access_token을 찾지 못했습니다: {data}"
        )

    print("[OK] OAuth access token 발급 성공")
    return token


# ============================================================
# 7. 계좌 검증
# ============================================================

def get_accounts(token):
    res = request_with_retry(
        "GET",
        f"{API_BASE_URL}/api/v1/accounts",
        headers=get_headers(token),
    )

    if res.status_code >= 400:
        raise RuntimeError(
            f"계좌 목록 조회 실패 ({res.status_code}): "
            f"{api_error_text(res)}"
        )

    return safe_json(res)


def verify_account_seq(token):
    if not ACCOUNT_SEQ:
        raise RuntimeError(
            "TOSS_ACCOUNT_SEQ가 설정되어 있지 않습니다."
        )

    data = get_accounts(token)
    found = False

    for item in recursive_items(result_of(data)):
        account_seq = item.get("accountSeq")

        if (
            account_seq is not None
            and str(account_seq) == str(ACCOUNT_SEQ)
        ):
            found = True
            break

    if not found:
        raise RuntimeError(
            "설정한 ACCOUNT_SEQ를 계좌 목록에서 찾지 못했습니다."
        )

    print("[OK] accountSeq 검증 성공")


# ============================================================
# 8. 통화별 매수 가능 금액
# ============================================================

def get_buying_power(token, currency):
    currency = currency.upper()

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
            f"{currency} buying-power 조회 실패 "
            f"({res.status_code}): {api_error_text(res)}"
        )

    data = safe_json(res)
    result = data.get("result", {}) if isinstance(data, dict) else {}

    value = to_decimal(result.get("cashBuyingPower"))

    if value is None:
        raise RuntimeError(
            f"{currency} cashBuyingPower 파싱 실패: {data}"
        )

    if value < 0:
        raise RuntimeError(
            f"{currency} 매수 가능 금액이 음수입니다: {value}"
        )

    print(f"[ACCOUNT] {currency} buying power: {value}")
    return value


# ============================================================
# 9. 현재가
# ============================================================

def get_current_price(token, symbol):
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
            f"{symbol} 현재가 조회 실패 ({res.status_code}): "
            f"{api_error_text(res)}"
        )

    data = result_of(safe_json(res))

    if isinstance(data, list):
        for item in data:
            if not isinstance(item, dict):
                continue

            item_symbol = str(
                item.get("symbol", "")
            ).upper()

            if item_symbol != symbol.upper():
                continue

            price = to_decimal(item.get("lastPrice"))

            if price is not None and price > 0:
                return price

    elif isinstance(data, dict):
        price = to_decimal(data.get("lastPrice"))

        if price is not None and price > 0:
            return price

    raise RuntimeError(
        f"{symbol} 현재가를 찾지 못했습니다: {data}"
    )


# ============================================================
# 10. 보유 종목 / 포지션
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
            f"보유종목 조회 실패 ({res.status_code}): "
            f"{api_error_text(res)}"
        )

    return safe_json(res)


def get_position(token, symbol):
    data = get_holdings(token)
    symbol = symbol.upper()

    for item in recursive_items(result_of(data)):
        item_symbol = (
            item.get("symbol")
            or item.get("ticker")
        )

        if not item_symbol:
            continue

        if str(item_symbol).upper() != symbol:
            continue

        quantity_raw = (
            item.get("quantity")
            if item.get("quantity") is not None
            else item.get("shares")
        )

        if quantity_raw is None:
            quantity_raw = item.get("holdingQuantity")

        avg_price_raw = (
            item.get("averagePrice")
            if item.get("averagePrice") is not None
            else item.get("avgPrice")
        )

        if avg_price_raw is None:
            avg_price_raw = item.get("averagePurchasePrice")

        quantity = to_decimal(quantity_raw)
        avg_price = to_decimal(avg_price_raw)

        if quantity is None:
            quantity = Decimal("0")

        print(
            f"[POSITION] {symbol}: "
            f"quantity={quantity}, avg_price={avg_price}"
        )

        return {
            "symbol": symbol,
            "quantity": quantity,
            "avg_price": avg_price,
        }

    print(f"[POSITION] {symbol}: 보유하지 않음")

    return {
        "symbol": symbol,
        "quantity": Decimal("0"),
        "avg_price": None,
    }


# ============================================================
# 11. 미체결 주문
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
        raise RuntimeError(
            f"주문 목록 조회 실패 ({res.status_code}): "
            f"{api_error_text(res)}"
        )

    data = safe_json(res)
    print("[DEBUG] orders response:", data)
    return data


def get_pending_orders_for_symbol(token, symbol):
    data = get_orders(token)
    symbol = symbol.upper()
    pending = []

    for item in recursive_items(result_of(data)):
        item_symbol = (
            item.get("symbol")
            or item.get("ticker")
        )

        if not item_symbol:
            continue

        if str(item_symbol).upper() == symbol:
            pending.append(item)

    print(
        f"[ORDER] {symbol} pending orders: {len(pending)}"
    )

    return pending


# ============================================================
# 12. 주문
# ============================================================

def place_order(
    token,
    symbol,
    currency,
    side,
    quantity,
    price,
):
    quantity = int(quantity)
    currency = currency.upper()
    price = normalize_price(price, currency)

    if quantity <= 0:
        print(f"[SKIP] 주문수량 0: {symbol} {side}")
        return None

    if price is None or price <= 0:
        raise RuntimeError(
            f"주문가격이 올바르지 않습니다: {price}"
        )

    payload = {
        "symbol": symbol,
        "side": side,
        "orderType": "LIMIT",
        "price": str(price),
        "quantity": quantity,
        "clientOrderId": str(uuid.uuid4()),
    }

    if DRY_RUN:
        print("[DRY_RUN] 실제 주문 전송 안 함")
        print("[DRY_RUN] POST /api/v1/orders")
        print("[DRY_RUN] currency:", currency)
        print("[DRY_RUN] payload:", payload)

        return {
            "dryRun": True,
            "currency": currency,
            "payload": payload,
        }

    # 통화는 주문 API에서 종목에 따라 결정될 수 있으므로
    # 임의의 currency 필드를 payload에 추가하지 않는다.
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
        raise RuntimeError(
            f"{symbol} 주문 실패 ({res.status_code}): "
            f"{api_error_text(res)}"
        )

    data = safe_json(res)

    print(
        f"[ORDER OK] {symbol} {side} "
        f"{quantity}주 @ {price} {currency}"
    )
    print("[ORDER RESPONSE]", data)

    return data


# ============================================================
# 13. 전략 계산
# ============================================================

def calculate_strategy(
    total_account_value,
    current_price,
    position,
    allocation_ratio,
    currency,
):
    one_buy_budget = (
        total_account_value
        * Decimal(str(allocation_ratio))
        / Decimal(TOTAL_STEPS)
    )

    quantity = position["quantity"]
    avg_price = position["avg_price"]

    if quantity <= 0 or avg_price is None:
        buy_quantity = int(one_buy_budget / current_price)

        return {
            "mode": "NEW",
            "currency": currency,
            "one_buy_budget": one_buy_budget,
            "buy_quantity": buy_quantity,
            "buy_price": current_price,
        }

    if one_buy_budget > 0:
        t_turn = quantity * avg_price / one_buy_budget
    else:
        t_turn = Decimal("0")

    star_percent = max(
        Decimal("10") - t_turn * Decimal("0.25"),
        Decimal("0"),
    )

    target_sell_price = normalize_price(
        avg_price * (
            Decimal("1") + star_percent / Decimal("100")
        ),
        currency,
    )

    if t_turn < Decimal("20"):
        budget_1 = one_buy_budget / Decimal("2")
        budget_2 = one_buy_budget / Decimal("2")

        buy_quantity_1 = int(budget_1 / avg_price)
        buy_quantity_2 = int(
            budget_2 / (avg_price * Decimal("1.05"))
        )

        buy_price_1 = normalize_price(avg_price, currency)
        buy_price_2 = normalize_price(
            avg_price * Decimal("1.05"),
            currency,
        )

    else:
        buy_quantity_1 = int(one_buy_budget / avg_price)
        buy_quantity_2 = 0

        buy_price_1 = normalize_price(avg_price, currency)
        buy_price_2 = None

    return {
        "mode": "HOLDING",
        "currency": currency,
        "one_buy_budget": one_buy_budget,
        "quantity": quantity,
        "avg_price": avg_price,
        "t_turn": t_turn,
        "star_percent": star_percent,
        "target_sell_price": target_sell_price,
        "buy_quantity_1": buy_quantity_1,
        "buy_price_1": buy_price_1,
        "buy_quantity_2": buy_quantity_2,
        "buy_price_2": buy_price_2,
    }


# ============================================================
# 14. 종목별 실행
# ============================================================

def run_symbol(
    token,
    symbol,
    currency,
    total_account_value,
):
    config = PORTFOLIO_CONFIG[symbol]

    allocation_ratio = Decimal(
        str(config["allocation_ratio"])
    )

    print()
    print("=" * 60)
    print(f"[SYMBOL] {symbol} / {currency}")
    print("=" * 60)

    current_price = get_current_price(token, symbol)

    print(
        f"[PRICE] {symbol}: "
        f"{current_price} {currency}"
    )

    position = get_position(token, symbol)

    pending_orders = get_pending_orders_for_symbol(
        token,
        symbol,
    )

    if pending_orders:
        print(
            f"[SKIP] {symbol}: 미체결 주문 "
            f"{len(pending_orders)}개가 있습니다."
        )
        return

    strategy = calculate_strategy(
        total_account_value=total_account_value,
        current_price=current_price,
        position=position,
        allocation_ratio=allocation_ratio,
        currency=currency,
    )

    print("[STRATEGY]")
    print(strategy)

    if strategy["mode"] == "NEW":
        buy_quantity = strategy["buy_quantity"]
        buy_price = strategy["buy_price"]

        print(
            f"[NEW BUY] {symbol} {buy_quantity}주 "
            f"@ {buy_price} {currency}"
        )

        if buy_quantity > 0:
            place_order(
                token=token,
                symbol=symbol,
                currency=currency,
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

    quantity = strategy["quantity"]
    sell_price = strategy["target_sell_price"]

    if quantity > 0:
        print(
            f"[SELL TARGET] {symbol} {quantity}주 "
            f"@ {sell_price} {currency}"
        )

        place_order(
            token=token,
            symbol=symbol,
            currency=currency,
            side="SELL",
            quantity=int(quantity),
            price=sell_price,
        )

    if not DRY_RUN:
        print(
            "[LIVE] 매도 주문 제출 후 "
            "이번 실행의 추가 매수를 생략합니다."
        )
        return

    # DRY_RUN에서는 기존 매수 전략도 계산해 출력
    buy_quantity_1 = strategy["buy_quantity_1"]
    buy_price_1 = strategy["buy_price_1"]

    if buy_quantity_1 > 0:
        print(
            f"[DRY_RUN BUY 1] {symbol} "
            f"{buy_quantity_1}주 @ {buy_price_1}"
        )

        place_order(
            token=token,
            symbol=symbol,
            currency=currency,
            side="BUY",
            quantity=buy_quantity_1,
            price=buy_price_1,
        )

    buy_quantity_2 = strategy["buy_quantity_2"]
    buy_price_2 = strategy["buy_price_2"]

    if buy_quantity_2 > 0 and buy_price_2 is not None:
        print(
            f"[DRY_RUN BUY 2] {symbol} "
            f"{buy_quantity_2}주 @ {buy_price_2}"
        )

        place_order(
            token=token,
            symbol=symbol,
            currency=currency,
            side="BUY",
            quantity=buy_quantity_2,
            price=buy_price_2,
        )


# ============================================================
# 15. 메인 매매 실행
# ============================================================

def run_trading():
    print("=" * 60)
    print("Toss Securities Multi-Currency Auto Trading")
    print("=" * 60)

    print(f"[CONFIG] DRY_RUN={DRY_RUN}")
    print(f"[CONFIG] ACCOUNT_SEQ={ACCOUNT_SEQ}")
    print(
        f"[CONFIG] FIXIE="
        f"{'사용' if FIXIE_URL else '미사용'}"
    )

    if not CLIENT_ID:
        raise RuntimeError("TOSS_CLIENT_ID가 없습니다.")

    if not CLIENT_SECRET:
        raise RuntimeError("TOSS_CLIENT_SECRET가 없습니다.")

    if not ACCOUNT_SEQ:
        raise RuntimeError("TOSS_ACCOUNT_SEQ가 없습니다.")

    if not PORTFOLIO_CONFIG:
        raise RuntimeError("PORTFOLIO_CONFIG가 비어 있습니다.")

    # 종목 설정 검증 및 통화별 배분 비율 합산
    allocation_by_currency = {}

    for symbol, config in PORTFOLIO_CONFIG.items():
        currency = str(config.get("currency", "")).upper()
        ratio = to_decimal(config.get("allocation_ratio"))

        if currency not in PRICE_DECIMALS_BY_CURRENCY:
            raise RuntimeError(
                f"{symbol}: 지원하지 않는 통화 {currency}"
            )

        if ratio is None or ratio < 0 or ratio > 1:
            raise RuntimeError(
                f"{symbol}: allocation_ratio는 0~1이어야 합니다."
            )

        allocation_by_currency[currency] = (
            allocation_by_currency.get(
                currency, Decimal("0")
            ) + ratio
        )

    for currency, ratio_sum in allocation_by_currency.items():
        if ratio_sum > Decimal("1"):
            raise RuntimeError(
                f"{currency} 종목의 allocation_ratio 합계가 "
                f"1.0을 초과합니다: {ratio_sum}"
            )

    token = get_access_token()
    verify_account_seq(token)

    # 각 통화별 매수 가능 금액은 한 번씩 조회
    buying_power_by_currency = {}

    for currency in sorted(allocation_by_currency):
        buying_power_by_currency[currency] = (
            get_buying_power(token, currency)
        )

    # 종목별 실행
    for symbol, config in PORTFOLIO_CONFIG.items():
        currency = config["currency"].upper()
        total_account_value = buying_power_by_currency[currency]

        print(
            f"[ACCOUNT] {symbol} 전략 기준금액 = "
            f"{total_account_value} {currency}"
        )

        run_symbol(
            token=token,
            symbol=symbol,
            currency=currency,
            total_account_value=total_account_value,
        )

    print()
    print("=" * 60)
    print("실행 완료")
    print("=" * 60)


# ============================================================
# 16. 로그 수집 및 텔레그램 알림
# ============================================================

class LogTee(io.StringIO):
    """콘솔과 메모리에 동시에 로그 기록."""

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
    stdout_tee = LogTee(sys.stdout)
    stderr_tee = LogTee(sys.stderr)

    caught_exception = None

    try:
        with redirect_stdout(stdout_tee), redirect_stderr(stderr_tee):
            try:
                print("=== 자동매매 시작 ===")
                print(f"[CONFIG] DRY_RUN={DRY_RUN}")

                run_trading()

                print("=== 자동매매 정상 종료 ===")

            except Exception as exc:
                caught_exception = exc
                print("=== 자동매매 오류 ===")
                traceback.print_exc()

    finally:
        log_text = (
            stdout_tee.getvalue()
            + stderr_tee.getvalue()
        )

        status = (
            "정상 종료"
            if caught_exception is None
            else "오류 발생"
        )

        mode = (
            "DRY_RUN (모의 실행)"
            if DRY_RUN
            else "LIVE (실주문 모드)"
        )

        configured_symbols = ", ".join(
            f"{symbol}/{config['currency']}"
            for symbol, config in PORTFOLIO_CONFIG.items()
        )

        message = (
            f"[자동매매] {status}\n"
            f"모드: {mode}\n"
            f"종목: {configured_symbols}\n\n"
            f"{log_text[-3500:]}"
        )

        try:
            send_telegram(message)
        finally:
            stdout_tee.close()
            stderr_tee.close()

    # 오류를 삼키지 않아 GitHub Actions에서도 실패 처리
    if caught_exception is not None:
        raise caught_exception


if __name__ == "__main__":
    main()
