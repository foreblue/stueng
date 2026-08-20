"""내 복습 기록으로 FSRS 파라미터를 다시 학습한다.

FSRS 기본 파라미터는 수억 건의 공개 복습 기록으로 맞춘 값이라 출발점으로 충분하다.
다만 사람마다 망각 곡선이 다르고, 특히 이 서비스는 출제 형식을 승급시키므로 (4지선다
-> 빈칸 -> 산출) 난이도 분포가 일반적인 플래시카드와 다르다. 기록이 충분히 쌓이면
내 데이터로 맞추는 편이 낫다.

    python -m vocab.optimize                 # 서버에서 기록을 받아 학습
    python -m vocab.optimize --source local  # 로컬 DB 의 기록으로 학습
    python -m vocab.optimize --check         # 기록이 충분한지만 확인

학습에는 torch 가 필요해 서버 이미지에 넣지 않았다. 여기서만 쓴다:

    pip install "fsrs[optimizer]"

결과로 나온 21개 값을 서버에 심으면 끝이다:

    fly secrets set VOCAB_FSRS_PARAMS="0.21,1.29,..."
"""

from __future__ import annotations

import argparse
import datetime as dt
import logging
import sys

import fsrs
import requests
from sqlalchemy import select

import config
from .db import make_engine, session_factory
from .models import ReviewLog

logger = logging.getLogger(__name__)

#: 이보다 적으면 학습을 거부한다. 소수의 기록에 맞추면 기본 파라미터보다 나빠진다.
MINIMUM_REVIEWS = 400

#: 이 아래에서는 돌려는 주되 결과를 믿지 말라고 경고한다.
COMFORTABLE_REVIEWS = 1_000


def _from_local() -> list[fsrs.ReviewLog]:
    with session_factory(make_engine())() as session:
        rows = session.scalars(
            select(ReviewLog).order_by(ReviewLog.card_id, ReviewLog.reviewed_at)
        ).all()
        return [
            fsrs.ReviewLog(
                card_id=row.card_id,
                rating=fsrs.Rating(row.rating),
                review_datetime=row.reviewed_at,
                review_duration=row.response_ms,
            )
            for row in rows
        ]


def _from_server() -> list[fsrs.ReviewLog]:
    if not config.VOCAB_SERVER_URL or not config.VOCAB_INGEST_TOKEN:
        raise RuntimeError("VOCAB_SERVER_URL 과 VOCAB_INGEST_TOKEN 이 필요합니다.")

    # /export 는 사람 세션이 필요하다. 기계용은 /api/export 로 ingest 토큰을 쓴다.
    response = requests.get(
        f"{config.VOCAB_SERVER_URL}/api/export",
        headers={"X-Ingest-Token": config.VOCAB_INGEST_TOKEN},
        timeout=120,
    )
    if not response.ok:
        raise RuntimeError(f"기록을 받지 못했습니다 ({response.status_code}): {response.text[:200]}")

    return [
        fsrs.ReviewLog(
            card_id=row["card_id"],
            rating=fsrs.Rating(int(row["rating"])),
            review_datetime=dt.datetime.fromisoformat(row["reviewed_at"]),
            review_duration=row.get("response_ms"),
        )
        for row in response.json()["review_log"]
    ]


def load(source: str) -> list[fsrs.ReviewLog]:
    logs = _from_local() if source == "local" else _from_server()
    logs.sort(key=lambda entry: (entry.card_id, entry.review_datetime))
    return logs


def describe(logs: list[fsrs.ReviewLog]) -> str:
    if not logs:
        return "복습 기록이 없습니다."

    cards = len({entry.card_id for entry in logs})
    span = max(e.review_datetime for e in logs) - min(e.review_datetime for e in logs)
    lines = [
        f"복습 기록 {len(logs)}건 · 카드 {cards}장 · 기간 {span.days}일",
    ]
    if len(logs) < MINIMUM_REVIEWS:
        lines.append(
            f"최소 {MINIMUM_REVIEWS}건은 있어야 학습합니다. "
            f"{MINIMUM_REVIEWS - len(logs)}건 더 쌓이면 다시 돌려 주세요."
        )
    elif len(logs) < COMFORTABLE_REVIEWS:
        lines.append(
            f"{COMFORTABLE_REVIEWS}건 이상에서 결과가 안정적입니다. "
            "지금 나온 값은 참고 정도로 보세요."
        )
    else:
        lines.append("학습하기 충분합니다.")
    return "\n".join(lines)


def optimize(logs: list[fsrs.ReviewLog]) -> list[float]:
    if len(logs) < MINIMUM_REVIEWS:
        raise RuntimeError(
            f"복습 기록이 {len(logs)}건뿐입니다. {MINIMUM_REVIEWS}건 미만에서 맞춘 파라미터는 "
            "기본값보다 나쁠 수 있습니다."
        )

    # Optimizer 는 torch 가 없으면 생성자에서 ImportError 를 던지는 껍데기다.
    # 서버 이미지를 가볍게 두려고 일부러 넣지 않았으므로, 여기서 친절히 안내한다.
    try:
        return fsrs.Optimizer(logs).compute_optimal_parameters(verbose=True)
    except ImportError as e:
        raise RuntimeError(
            '학습에는 torch 가 필요합니다: pip install "fsrs[optimizer]"'
        ) from e


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="복습 기록으로 FSRS 파라미터를 학습한다")
    parser.add_argument("--source", choices=("server", "local"), default="server")
    parser.add_argument("--check", action="store_true", help="기록이 충분한지만 확인")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    try:
        logs = load(args.source)
        print(describe(logs))
        if args.check:
            return 0

        values = optimize(logs)
        joined = ",".join(f"{value:.4f}" for value in values)
        print()
        print("학습된 파라미터:")
        print(f"  {joined}")
        print()
        print("서버에 심으려면:")
        print(f'  fly secrets set VOCAB_FSRS_PARAMS="{joined}"')
        return 0
    except (RuntimeError, requests.RequestException) as e:
        logger.error("%s", e)
        return 1


if __name__ == "__main__":
    sys.exit(main())
