import os
import math
import time
import uuid
from decimal import Decimal, ROUND_DOWN, InvalidOperation

import requests


# ============================================================
# 1. 환경변수 / 기본 설정
# ============================================================

CLIENT_ID = os.environ.get("TOSS_CLIENT_ID")
CLIENT_SECRET = os.environ.get("TOSS_CLIENT_SECRET")

# 반드시 accountSeq를 사용
ACCOUNT_SEQ = os.environ.get("TOSS_ACCOUNT_SEQ")

# 기존 환경변수와의 호환
if not ACCOUNT_SEQ:
    ACCOUNT_SEQ = os.environ.get("TOSS_ACCOUNT_NO")

FIXIE_URL = os.environ.get("FIXIE_URL")

# false일 때만 LIVE
DRY_RUN = os.environ.get("DRY_RUN", "true").strip().lower() != "false"

API_BASE_URL = "https://openapi.tossinvest.com"

TOTAL_STEPS = 40
PRICE_DECIMALS = 2
TIMEOUT = 10

PORTFOLIO_CONFIG = {
    "SNDL": {
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


# ============================================================
# 3. 공통 HTTP 함수
# ============================================================

def safe_json(res):
    """
    requests.Response 또는 이미 파싱된 dict/list 모두 처리.
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
    Response 또는 dict/list 모두 안전하게 오류 메시지를 추출.
    """

    if isinstance(res_or_data, requests.Response):
        data = safe_json(res_or_data)

        if isinstance(data, dict):
            err = data.get("error")

            if isinstance(err, dict):
                code = err.get("code", "")
                message = err.get("message", "")

                result = f"{code}: {message}".strip(": ")

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

            result = f"{code}: {message}".strip(": ")

            if result:
                return result

        return str(data)[:3000]

    return str(data)[:3000]


def request_with_retry(method, url, **kwargs):
    """
    Toss API 호출.
    일시적인 429 / 5xx에 대해서만 재시도.
    """

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
        headers["X-Tossinvest-Account"] = str(ACCOUNT_SEQ)

    return headers


# ============================================================
# 5. 데이터 파싱 Helper
# ============================================================

def result_of(data):
    """
    Toss API의
        {"result": ...}
    구조를 벗겨낸다.
    """

    if isinstance(data, dict) and "result" in data:
        return data["result"]

    return data


def recursive_items(value):
    """
    dict/list 내부를 재귀적으로 탐색하면서
    dict만