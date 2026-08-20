"""인증과 서명.

혼자 쓰는 서비스라 계정 테이블이 없다. 비밀번호 하나와 서명 쿠키면 충분하다.
토큰을 URL 에 담는 방식은 쓰지 않는다 — 브라우저 히스토리, 공유 링크, 리퍼러로 샌다.

로컬 파이프라인이 어휘를 밀어 넣는 `/ingest` 는 사람이 아니라 기계가 부르므로
별도 시크릿 헤더를 쓴다. 세션 쿠키와 섞지 않는다.
"""

from __future__ import annotations

import hmac
import os
import secrets
from typing import Any

from fastapi import HTTPException, Request, status
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

SESSION_COOKIE = "vocab_session"
SESSION_MAX_AGE = 60 * 60 * 24 * 90  # 90일. 혼자 쓰는 폰 홈화면 앱이라 길게 둔다.

#: 출제 토큰의 수명. 문제를 띄워 놓고 하루 뒤에 답하는 건 정상 흐름이 아니다.
QUESTION_MAX_AGE = 60 * 60 * 6


class ConfigError(RuntimeError):
    pass


def secret_key() -> str:
    key = os.environ.get("VOCAB_SECRET_KEY")
    if key:
        return key
    if os.environ.get("VOCAB_ENV") == "production":
        raise ConfigError("VOCAB_SECRET_KEY 가 필요합니다. 없으면 재시작마다 로그아웃됩니다.")
    # 개발용. 프로세스가 살아 있는 동안만 유효하다.
    os.environ["VOCAB_SECRET_KEY"] = secrets.token_urlsafe(32)
    return os.environ["VOCAB_SECRET_KEY"]


def password() -> str:
    value = os.environ.get("VOCAB_PASSWORD", "")
    if not value and os.environ.get("VOCAB_ENV") == "production":
        raise ConfigError("VOCAB_PASSWORD 가 설정되지 않았습니다.")
    return value


def ingest_token() -> str:
    return os.environ.get("VOCAB_INGEST_TOKEN", "")


def _serializer(salt: str) -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(secret_key(), salt=salt)


def sign(payload: Any, *, salt: str) -> str:
    return _serializer(salt).dumps(payload)


def unsign(token: str, *, salt: str, max_age: int) -> Any:
    try:
        return _serializer(salt).loads(token, max_age=max_age)
    except SignatureExpired:
        raise HTTPException(status.HTTP_409_CONFLICT, "문제가 만료됐습니다. 다시 받아 주세요.")
    except BadSignature:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "잘못된 요청입니다.")


def check_password(given: str) -> bool:
    expected = password()
    if not expected:
        return False
    # 길이까지 감추지는 못하지만, 문자별 조기 종료는 막는다.
    return hmac.compare_digest(given.encode("utf-8"), expected.encode("utf-8"))


def issue_session() -> str:
    return sign({"v": 1}, salt="session")


def is_logged_in(request: Request) -> bool:
    token = request.cookies.get(SESSION_COOKIE)
    if not token:
        return False
    try:
        _serializer("session").loads(token, max_age=SESSION_MAX_AGE)
    except (BadSignature, SignatureExpired):
        return False
    return True


def require_ingest_token(request: Request) -> None:
    expected = ingest_token()
    if not expected:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "ingest 가 설정되지 않았습니다.")
    given = request.headers.get("x-ingest-token", "")
    if not hmac.compare_digest(given.encode("utf-8"), expected.encode("utf-8")):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "ingest 토큰이 일치하지 않습니다.")
