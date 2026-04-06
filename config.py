import os
from dotenv import load_dotenv

load_dotenv()

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
PROXY_URL = os.environ.get("PROXY_URL", "http://localhost:9000")

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
