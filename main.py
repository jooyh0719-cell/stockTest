import os
import json
import uuid
import base64
import requests

from decimal import Decimal, ROUND_DOWN, ROUND_CEILING
from datetime import datetime
from pathlib import Path


# ============================================================
# 라오어 무한매수법 V2.2 - BULZ
# 실제 주문 전송 버전
# ============================================================

CLIENT_ID = os.getenv("TOSS_CLIENT_ID", "")
CLIENT_SECRET = os.getenv("TOSS_CLIENT_SECRET", "")
ACCOUNT_SEQ = os.getenv("TOSS_ACCOUNT_SEQ", "")

FIXIE_URL = os.getenv("FIXIE_URL", "")
API_BASE_URL = "https://openapi.tossinvest.com"

SYMBOL = "BULZ"
TOTAL_STEPS = 40

STRATEGY_CAPITAL_USD = os.getenv(
    "STRATEGY_CAPITAL_USD", ""
).strip()

STATE_FILE = Path("strategy_state.json")

GITHUB_TOKEN = os.getenv("GITHUB_TOKEN", "")
GITHUB_REPOSITORY = os.getenv("GITHUB_REPOSITORY", "")
GITHUB_REF_NAME = os.getenv("GITHUB_REF_NAME", "main")
GITHUB_STATE_PATH = os.getenv(
    "GITHUB_STATE_PATH", "strategy_state.json"
)

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

TIMEOUT = 20

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
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{now}] {message}", flush=True)


def notify(message):
    log(message)

    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return

    try:
        SESSION.post(
            f"https://api.telegram.org/bot"
            f"{TELEGRAM_BOT_TOKEN}/sendMessage",
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


# ============================================================
# 토스 API 공통 요청
# 핵심 수정: x-tossinvest-account 헤더
# ============================================================

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

    # 토스 API에서 요구하는 계좌 헤더
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


# ============================================================
# 상태 저장
# ============================================================

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

            log("GitHub 상태 파일 로드 완료")
            return json.loads(content)

        if response.status_code == 404:
            log("GitHub 상태 파일 없음: 최초 생성")
            return {}

        log(
            f"GitHub 상태 조회 실패: "
            f"{response.text[:1000]}"
        )
        response.raise_for_status()

    if STATE_FILE.exists():
        with STATE_FILE.open("r", encoding="utf-8") as file:
            return json.load(file)

    return {}


def save_state(state):
    content = json.dumps(
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