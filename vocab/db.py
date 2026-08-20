"""엔진/세션 준비.

로컬은 SQLite(`data/vocab.db`), 서버는 Postgres. `VOCAB_DB_URL` 로 덮어쓴다.
"""

from __future__ import annotations

import os
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from .models import Base

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_SQLITE_PATH = REPO_ROOT / "data" / "vocab.db"


def database_url() -> str:
    url = os.environ.get("VOCAB_DB_URL")
    if url:
        # Fly/Neon 이 주는 postgres:// 를 SQLAlchemy 2.x 가 이해하는 형태로.
        if url.startswith("postgres://"):
            url = url.replace("postgres://", "postgresql+psycopg://", 1)
        return url
    DEFAULT_SQLITE_PATH.parent.mkdir(parents=True, exist_ok=True)
    return f"sqlite:///{DEFAULT_SQLITE_PATH}"


def make_engine(url: str | None = None, *, echo: bool = False) -> Engine:
    url = url or database_url()
    kwargs = {"echo": echo, "future": True}
    if url.startswith("sqlite"):
        kwargs["connect_args"] = {"check_same_thread": False}
    engine = create_engine(url, **kwargs)
    if url.startswith("sqlite"):
        # 외래키 ON DELETE CASCADE 는 SQLite 에서 기본 비활성이다.
        from sqlalchemy import event

        @event.listens_for(engine, "connect")
        def _fk_on(dbapi_conn, _):  # pragma: no cover - 연결 훅
            dbapi_conn.execute("PRAGMA foreign_keys=ON")

    return engine


def create_all(engine: Engine) -> None:
    Base.metadata.create_all(engine)


def session_factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(engine, expire_on_commit=False, future=True)
