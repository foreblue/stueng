import os
from dotenv import load_dotenv

load_dotenv()


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _env_csv(name: str, default: str) -> list[str]:
    raw = os.environ.get(name, default)
    values = [item.strip() for item in raw.split(",") if item.strip()]
    if values:
        return values
    return [item.strip() for item in default.split(",") if item.strip()]


TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
PROXY_URL = os.environ.get("PROXY_URL", "http://localhost:9000")

DEFAULT_AI_MODELS = [
    # gemini backend은 현재 기본 요청 대상에서 제외.
    # "gemini-2.5-flash",
    # "gemini-2.5-pro",
    # anthropic claude backend은 현재 기본 요청 대상에서 제외.
    # "claude-sonnet-4-6",
    "opus-4.8",  # cursor backend
    "gpt-5.5",  # codex backend
]
AI_MODELS = _env_csv("AI_MODELS", ",".join(DEFAULT_AI_MODELS))
AI_TIMEOUT_SEC = _env_int("AI_TIMEOUT_SEC", 120)

TELEGRAM_MAX_LENGTH = 4096

# 활성 소스 목록 - 추가 시 여기에 이름 추가
# 예: ACTIVE_SOURCES = ["upfirst", "planetmoney"]
ACTIVE_SOURCES = ["upfirst"]


def validate():
    missing = []
    if not TELEGRAM_BOT_TOKEN:
        missing.append("TELEGRAM_BOT_TOKEN")
    if not TELEGRAM_CHAT_ID:
        missing.append("TELEGRAM_CHAT_ID")
    if missing:
        raise EnvironmentError(f"Missing required env vars: {', '.join(missing)}")
