"""주 1회 작문 과제.

Hulstijn & Laufer (2001) 의 Involvement Load Hypothesis: 같은 단어를 다뤄도 과제가
필요(need)·탐색(search)·평가(evaluation)를 많이 요구할수록 파지가 좋다. 세 과제를
비교한 실험에서 파지는 작문 > 빈칸 채우기 > 읽기 순이었고, 특히 평가 요소의 기여가 컸다.

그래서 넣되, **주 1회로 제한한다.** 매일 시키면 부담이 커서 이탈하고, 이탈하면 간격
반복이라는 본체까지 같이 멈춘다. 카드 복습은 매일, 작문은 주 1회다.

첨삭은 서버가 하지 않는다. LLM 은 로컬 프록시에만 있다. 제출한 글은 서버에 쌓여
있다가 로컬 워커가 가져가 첨삭하고 돌려놓는다.
"""

from __future__ import annotations

import datetime as dt
import json

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from . import study
from .models import STAGES, Card, Composition, ReviewLog, Word

#: 한 과제에 쓰는 어휘 수. 세 개면 문단 하나가 나오고, 더 늘리면 억지 문장이 된다.
WORDS_PER_TASK = 3

#: 이만큼은 인출해 본 카드라야 쓰기 과제에 올린다. 뜻도 모르는 단어로 문장을 지으면
#: 사전을 베끼는 일이 되고, 그건 관여도가 아니라 필사다.
MIN_REPS = 2

#: 최근 이 기간 안에 복습한 카드에서 고른다. 지금 머릿속에 있는 단어라야 의미가 있다.
RECENT_DAYS = 14


def week_start(now: dt.datetime | None = None) -> dt.date:
    """이번 주 월요일 (로컬 기준)."""
    local = (now or study.now_utc()).astimezone(study.timezone()).date()
    return local - dt.timedelta(days=local.weekday())


def pick_words(session: Session, now: dt.datetime | None = None) -> list[Word]:
    """이번 주 과제에 올릴 어휘.

    최근에 인출해 본 카드 중에서, 승급이 많이 된 것부터 고른다. 산출 단계까지 올라간
    카드는 이미 한→영으로 꺼내 본 적이 있으니 문장으로 쓰기에 알맞다.
    """
    now = now or study.now_utc()
    since = now - dt.timedelta(days=RECENT_DAYS)
    stage_rank = {stage: index for index, stage in enumerate(STAGES)}

    cards = session.scalars(
        select(Card)
        .join(Word)
        .where(
            Card.suspended.is_(False),
            Card.reps >= MIN_REPS,
            Card.last_review >= since,
            Word.known.is_(False),
        )
        .order_by(Card.last_review.desc())
        .limit(60)
    ).all()

    cards.sort(key=lambda card: (-stage_rank.get(card.stage, 0), -(card.stability or 0)))
    return [card.word for card in cards[:WORDS_PER_TASK]]


def ensure(session: Session, now: dt.datetime | None = None) -> Composition | None:
    """이번 주 과제를 가져온다. 없으면 만들되, 올릴 어휘가 모자라면 만들지 않는다."""
    now = now or study.now_utc()
    start = week_start(now)

    existing = session.scalar(select(Composition).where(Composition.week_start == start))
    if existing:
        return existing

    words = pick_words(session, now)
    if len(words) < WORDS_PER_TASK:
        # 학습을 막 시작해 인출해 본 카드가 적은 주. 과제를 억지로 내지 않는다.
        return None

    composition = Composition(
        week_start=start,
        words=json.dumps(
            [{"id": w.id, "display": w.display, "meaning_kr": w.meaning_kr} for w in words],
            ensure_ascii=False,
        ),
        created_at=now,
    )
    session.add(composition)
    session.flush()
    return composition


def submit(
    session: Session, composition: Composition, text: str, now: dt.datetime | None = None
) -> Composition:
    """작문을 제출한다. 첨삭은 로컬 워커가 나중에 채운다."""
    text = text.strip()
    if not text:
        raise ValueError("빈 글은 제출할 수 없습니다.")

    composition.text = text
    composition.submitted_at = now or study.now_utc()
    #: 다시 제출하면 이전 첨삭은 지운다. 고친 글에 옛 첨삭이 붙어 있으면 헷갈린다.
    composition.feedback = None
    composition.feedback_at = None
    session.flush()
    return composition


def history(session: Session, limit: int = 12) -> list[Composition]:
    return session.scalars(
        select(Composition).order_by(Composition.week_start.desc()).limit(limit)
    ).all()


def pending_feedback(session: Session) -> list[Composition]:
    """로컬 워커가 가져갈 것 — 제출됐는데 아직 첨삭이 없는 글."""
    return session.scalars(
        select(Composition)
        .where(Composition.submitted_at.is_not(None), Composition.feedback.is_(None))
        .order_by(Composition.week_start)
    ).all()


def leeches_without_mnemonic(session: Session, limit: int = 20) -> list[Word]:
    """반복해서 막힌 카드 중 아직 기억술이 없는 것.

    니모닉을 모든 카드에 붙이지 않는 이유: 초기 회상에는 강하지만 시간이 지나면 이점이
    감쇠하고, 자동 생성 품질의 편차가 크다. 정말 안 외워지는 카드에만 쓰는 탈출구다.
    """
    return session.scalars(
        select(Word)
        .join(Card)
        .where(Card.leech.is_(True), Word.mnemonic.is_(None), Word.known.is_(False))
        .order_by(Card.lapses.desc())
        .limit(limit)
    ).all()


def stats(session: Session) -> dict:
    return {
        "total": session.scalar(select(func.count()).select_from(Composition)) or 0,
        "submitted": session.scalar(
            select(func.count()).select_from(Composition).where(Composition.text.is_not(None))
        )
        or 0,
        "reviews": session.scalar(select(func.count()).select_from(ReviewLog)) or 0,
    }
