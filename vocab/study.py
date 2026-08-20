"""복습 세션 로직.

조사에서 나온 원리를 코드로 옮긴 곳이다. 네 가지가 여기서 결정된다.

1. **세션 내 재인출.** 틀린 카드는 다음 날로 넘어가지 않는다. 같은 세션 안에서
   몇 장 뒤에 다시 나오고, 맞힐 때까지 세션이 끝나지 않는다.
   (Rawson & Dunlosky 2022 — successive relearning)
2. **형식 승급.** 같은 카드를 4지선다 -> 원문 빈칸 -> 한→영 산출 순으로 올린다.
   인출 노력을 키우면서 정답률은 유지하고, 만들어지는 지식의 종류도 넓힌다.
3. **예문 순환.** 안정된 카드는 출제할 때마다 다른 원문 문장을 쓴다.
   (문맥 다양성 — 같은 문맥만 반복하면 그 문맥에 묶인 표상이 만들어진다)
   단 신규 단계에서는 한 예문에 고정한다. 어휘력이 낮은 단계에서 문맥을 흩뿌리면
   오히려 방해가 된다는 조절 효과가 보고돼 있다.
4. **객관 지표 기록.** 자기평가 rating 뿐 아니라 정오답·반응 시간을 남긴다.
   학습자의 체감은 실제 효과와 어긋난다(Karpicke & Roediger 2008).

세션 상태를 따로 들고 있지 않는다. 큐는 매번 DB 에서 다시 계산한다. 탭을 닫거나
서버가 재시작해도 "아직 못 맞힌 카드" 가 그대로 살아 있어야 하기 때문이다.
"""

from __future__ import annotations

import datetime as dt
import os
import random
import re
from dataclasses import dataclass, field
from zoneinfo import ZoneInfo

from sqlalchemy import case, exists, func, select
from sqlalchemy.orm import Session

from . import banding, scheduler
from .models import (
    KIND_EXPRESSION,
    SOURCE_CORRECTION,
    STAGE_CLOZE,
    STAGE_PRODUCTION,
    STAGE_RECOGNITION,
    Card,
    Occurrence,
    ReviewLog,
    Word,
)

BLANK = "____"

#: 하루에 새로 들이는 카드 수. 615개 core 어휘를 두 달에 걸쳐 소화하는 속도.
NEW_PER_DAY = 10

#: 틀린 카드를 다시 내기 전에 끼워 넣는 다른 카드 수. 바로 다시 물으면 인출이 아니라
#: 단기 기억 되뇌기가 된다.
RETRY_GAP = 3

#: 형식 승급 기준 (FSRS stability, 일 단위).
CLOZE_MIN_STABILITY = 21.0
PRODUCTION_MIN_STABILITY = 60.0

#: 이 시간을 넘겨 맞히면 Hard 로 본다. 자기평가 대신 쓰는 객관 지표.
SLOW_MS = {
    STAGE_RECOGNITION: 8_000,
    STAGE_CLOZE: 20_000,
    STAGE_PRODUCTION: 25_000,
}

#: 이만큼 실패를 반복하면 leech. 니모닉 제안 같은 탈출구를 여기에만 건다.
LEECH_LAPSES = 8

#: 하루의 경계. 자정 직후 학습을 전날 몫으로 치기 위해 새벽 4시로 둔다.
DAY_START_HOUR = 4

#: 4지선다 보기 수
CHOICE_COUNT = 4


def timezone() -> ZoneInfo:
    return ZoneInfo(os.environ.get("VOCAB_TZ", "Asia/Seoul"))


def now_utc() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


def day_start(now: dt.datetime | None = None) -> dt.datetime:
    """오늘 학습일의 시작 시각(UTC). 새벽 4시 이전이면 어제 몫이다."""
    now = now or now_utc()
    local = now.astimezone(timezone())
    start = local.replace(hour=DAY_START_HOUR, minute=0, second=0, microsecond=0)
    if local < start:
        start -= dt.timedelta(days=1)
    return start.astimezone(dt.UTC)


# --------------------------------------------------------------------------
# 신규 카드 투입
# --------------------------------------------------------------------------


def _new_word_query():
    """카드가 아직 없는 어휘를 우선순위 순으로.

    1. 수업에서 내가 틀려 교정받은 표현 — 개인화의 핵심 재료다.
    2. core 밴드 (known 은 이미 알 확률이 높고, rare 는 조어에 가깝다)
    3. 예문이 많은 것 — 문맥 순환이 가능한 카드가 더 값지다.
    4. 먼저 만난 것
    """
    is_correction = (
        select(1)
        .where(Occurrence.word_id == Word.id, Occurrence.source_kind == SOURCE_CORRECTION)
        .exists()
    )
    occurrence_count = (
        select(func.count())
        .select_from(Occurrence)
        .where(Occurrence.word_id == Word.id)
        .scalar_subquery()
    )
    band_rank = case(
        {banding.BAND_CORE: 0, banding.BAND_KNOWN: 1, banding.BAND_RARE: 2},
        value=Word.band,
        else_=3,
    )
    return (
        select(Word)
        .where(~exists().where(Card.word_id == Word.id), Word.known.is_(False))
        .order_by(
            is_correction.desc(),
            band_rank.asc(),
            occurrence_count.desc(),
            Word.first_seen.asc(),
            Word.id.asc(),
        )
    )


def introduced_today(session: Session, now: dt.datetime | None = None) -> int:
    return (
        session.scalar(
            select(func.count()).select_from(Card).where(Card.created_at >= day_start(now))
        )
        or 0
    )


def introduce(session: Session, count: int, now: dt.datetime | None = None) -> list[Card]:
    """새 어휘를 카드로 만든다. 만든 즉시 due 다."""
    if count <= 0:
        return []
    now = now or now_utc()
    words = session.scalars(_new_word_query().limit(count)).all()
    cards = [
        Card(word_id=word.id, stage=STAGE_RECOGNITION, state=scheduler.STATE_LEARNING,
             step=0, due=now, created_at=now)
        for word in words
    ]
    session.add_all(cards)
    session.flush()
    return cards


# --------------------------------------------------------------------------
# 큐
# --------------------------------------------------------------------------


@dataclass
class QueueState:
    """지금 이 순간 세션이 어떤 상태인지."""

    retry_ready: list[int] = field(default_factory=list)
    retry_waiting: list[int] = field(default_factory=list)
    due: list[int] = field(default_factory=list)
    new_remaining: int = 0

    @property
    def remaining(self) -> int:
        return len(self.retry_ready) + len(self.retry_waiting) + len(self.due) + self.new_remaining

    @property
    def finished(self) -> bool:
        return self.remaining == 0


def queue_state(session: Session, now: dt.datetime | None = None) -> QueueState:
    now = now or now_utc()
    start = day_start(now)

    logs = session.scalars(
        select(ReviewLog).where(ReviewLog.reviewed_at >= start).order_by(ReviewLog.reviewed_at, ReviewLog.id)
    ).all()

    last_index: dict[int, int] = {}
    last_correct: dict[int, bool] = {}
    for index, log in enumerate(logs):
        last_index[log.card_id] = index
        last_correct[log.card_id] = log.correct

    # 오늘 마지막 시도가 오답이면 아직 못 맞힌 것이다.
    unresolved = [card_id for card_id, ok in last_correct.items() if not ok]
    suspended = set(
        session.scalars(select(Card.id).where(Card.id.in_(unresolved), Card.suspended.is_(True))).all()
    ) if unresolved else set()

    ready, waiting = [], []
    for card_id in unresolved:
        if card_id in suspended:
            continue
        gap = len(logs) - 1 - last_index[card_id]
        (ready if gap >= RETRY_GAP else waiting).append(card_id)

    ready.sort(key=lambda cid: last_index[cid])
    waiting.sort(key=lambda cid: last_index[cid])

    due = session.scalars(
        select(Card.id)
        .where(Card.due <= now, Card.suspended.is_(False), Card.id.notin_(unresolved or [-1]))
        .order_by(Card.due.asc())
    ).all()

    remaining_new = max(0, NEW_PER_DAY - introduced_today(session, now))
    if remaining_new:
        available = session.scalar(
            select(func.count()).select_from(_new_word_query().limit(remaining_new).subquery())
        ) or 0
        remaining_new = min(remaining_new, available)

    return QueueState(
        retry_ready=list(ready),
        retry_waiting=list(waiting),
        due=list(due),
        new_remaining=remaining_new,
    )


def next_card(session: Session, now: dt.datetime | None = None) -> Card | None:
    """다음에 낼 카드 한 장.

    순서: 재인출 대기가 끝난 오답 -> 예정된 복습 -> 신규 투입 -> (남은 게 그것뿐이면)
    간격을 못 채운 오답. 마지막 항이 있어야 세션이 못 맞힌 카드를 남긴 채 끝나지 않는다.
    """
    now = now or now_utc()
    state = queue_state(session, now)

    if state.retry_ready:
        return session.get(Card, state.retry_ready[0])
    if state.due:
        return session.get(Card, state.due[0])
    if state.new_remaining:
        cards = introduce(session, 1, now)
        if cards:
            return cards[0]
    if state.retry_waiting:
        return session.get(Card, state.retry_waiting[0])
    return None


# --------------------------------------------------------------------------
# 출제
# --------------------------------------------------------------------------


def make_cloze(sentence: str, headword: str) -> tuple[str, str] | None:
    """예문에서 표제어를 가린다. 굴절형도 잡는다.

    'collaborate' 를 찾을 때 문장에 있는 것은 'collaborating' 이다. 표면형을
    그대로 정답으로 삼되, 채점에서는 원형도 받아 준다.
    """
    if not sentence or not headword:
        return None

    if banding.is_phrase(headword):
        pattern = re.escape(headword).replace(r"\ ", r"[\s-]+").replace(r"\-", r"[\s-]+")
        match = re.search(pattern, sentence, re.IGNORECASE)
        if not match:
            return None
        return sentence[: match.start()] + BLANK + sentence[match.end() :], match.group(0)

    target = banding.lemma(headword)
    for match in re.finditer(r"[A-Za-z]+(?:['’][A-Za-z]+)?", sentence):
        token = match.group(0)
        if token.lower() == headword or banding.lemma(token) == target:
            return sentence[: match.start()] + BLANK + sentence[match.end() :], token
    return None


def ordered_occurrences(session: Session, card: Card, *, rotate: bool) -> list[Occurrence]:
    """이번에 쓸 예문 후보를 선호 순으로.

    신규 단계에서는 첫 예문에 고정한다 — 어휘력이 낮은 단계에서 문맥을 흩뿌리면
    오히려 방해가 된다는 조절 효과가 보고돼 있다. 승급한 뒤부터는 적게 쓴 것을
    먼저 고르고, 직전에 쓴 것은 다른 선택지가 있으면 뒤로 민다.
    """
    occurrences = list(card.word.occurrences)
    if not occurrences or not rotate:
        return occurrences

    used = dict(
        session.execute(
            select(ReviewLog.occurrence_id, func.count())
            .where(ReviewLog.card_id == card.id, ReviewLog.occurrence_id.is_not(None))
            .group_by(ReviewLog.occurrence_id)
        ).all()
    )
    return sorted(
        occurrences,
        key=lambda o: (o.id == card.last_occurrence_id, used.get(o.id, 0), o.id),
    )


def _distractors(session: Session, word: Word, count: int, rng: random.Random) -> list[str]:
    """오답 보기. 같은 종류·같은 밴드에서 먼저 고른다. 뜻이 겹치면 안 된다."""
    pool = session.scalars(
        select(Word.meaning_kr)
        .where(Word.id != word.id, Word.kind == word.kind, Word.band == word.band)
        .order_by(func.random())
        .limit(count * 6)
    ).all()
    if len(set(pool)) < count:
        extra = session.scalars(
            select(Word.meaning_kr)
            .where(Word.id != word.id, Word.kind == word.kind)
            .order_by(func.random())
            .limit(count * 6)
        ).all()
        pool = list(pool) + list(extra)

    seen = {word.meaning_kr}
    picked: list[str] = []
    for meaning in pool:
        if meaning in seen:
            continue
        seen.add(meaning)
        picked.append(meaning)
        if len(picked) == count:
            break
    rng.shuffle(picked)
    return picked


@dataclass
class Question:
    card_id: int
    stage: str
    kind: str
    display: str
    meaning_kr: str
    prompt: str
    answer: str
    choices: list[str] | None = None
    hint: str | None = None
    occurrence_id: int | None = None
    sentence: str | None = None
    source_label: str | None = None
    source_url: str | None = None
    leech: bool = False

    @property
    def typed(self) -> bool:
        return self.choices is None


def build_question(session: Session, card: Card, *, rng: random.Random | None = None) -> Question:
    rng = rng or random.Random()
    word = card.word
    stability = card.stability or 0.0

    stage = STAGE_RECOGNITION
    occurrence: Occurrence | None = None
    cloze: tuple[str, str] | None = None

    if stability >= PRODUCTION_MIN_STABILITY:
        stage = STAGE_PRODUCTION
        candidates = ordered_occurrences(session, card, rotate=True)
        occurrence = candidates[0] if candidates else None
    elif stability >= CLOZE_MIN_STABILITY:
        # 예문 하나가 빈칸이 안 된다고 바로 포기하지 않는다. 굴절형·표기 차이 때문에
        # 어떤 예문에는 표제어가 그대로 안 들어 있을 수 있다.
        for candidate in ordered_occurrences(session, card, rotate=True):
            made = make_cloze(candidate.sentence, word.headword)
            if made:
                stage, occurrence, cloze = STAGE_CLOZE, candidate, made
                break

    if stage == STAGE_RECOGNITION:
        candidates = ordered_occurrences(session, card, rotate=False)
        occurrence = candidates[0] if candidates else None

    source_label = (
        f"{occurrence.source_title} · {occurrence.occurred_on:%Y-%m-%d}" if occurrence else None
    )
    common = dict(
        card_id=card.id,
        stage=stage,
        kind=word.kind,
        display=word.display,
        meaning_kr=word.meaning_kr,
        occurrence_id=occurrence.id if occurrence else None,
        sentence=occurrence.sentence if occurrence else None,
        source_label=source_label,
        source_url=occurrence.source_url if occurrence else None,
        leech=card.leech,
    )

    if stage == STAGE_CLOZE and cloze:
        blanked, surface = cloze
        return Question(prompt=blanked, answer=surface, hint=word.meaning_kr, **common)

    if stage == STAGE_PRODUCTION:
        label = "이 뜻의 표현은?" if word.kind == KIND_EXPRESSION else "이 뜻의 단어는?"
        return Question(prompt=f"{label}\n{word.meaning_kr}", answer=word.display, **common)

    choices = _distractors(session, word, CHOICE_COUNT - 1, rng) + [word.meaning_kr]
    rng.shuffle(choices)
    return Question(prompt=word.display, answer=word.meaning_kr, choices=choices, **common)


# --------------------------------------------------------------------------
# 채점
# --------------------------------------------------------------------------


def _edit_distance(a: str, b: str) -> int:
    if abs(len(a) - len(b)) > 1:
        return 2
    previous = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        current = [i]
        for j, cb in enumerate(b, 1):
            current.append(
                min(previous[j] + 1, current[j - 1] + 1, previous[j - 1] + (ca != cb))
            )
        previous = current
    return previous[-1]


def check_answer(question: Question, given: str) -> tuple[bool, bool]:
    """(정답인가, 아슬아슬했는가).

    타이핑 답안은 철자 하나 차이까지는 맞은 것으로 보되 Hard 로 기록한다.
    철자를 완전히 익히지 못한 상태를 Good 으로 올리면 간격이 과하게 벌어진다.
    """
    given_norm = banding.normalize(given)
    answer_norm = banding.normalize(question.answer)
    if not given_norm:
        return False, False
    if given_norm == answer_norm:
        return True, False

    if question.choices is not None:
        return False, False

    # 굴절형 차이는 맞은 것으로 본다 — 빈칸의 표면형이 'collaborating' 이어도 'collaborate' 를 받는다.
    if not banding.is_phrase(answer_norm) and banding.lemma(given_norm) == banding.lemma(answer_norm):
        return True, True

    if len(answer_norm) >= 5 and _edit_distance(given_norm, answer_norm) <= 1:
        return True, True

    return False, False


def rating_for(*, correct: bool, near_miss: bool, stage: str, response_ms: int | None,
               self_easy: bool = False) -> int:
    if not correct:
        return scheduler.RATING_AGAIN
    if near_miss:
        return scheduler.RATING_HARD
    if response_ms is not None and response_ms > SLOW_MS.get(stage, 10_000):
        return scheduler.RATING_HARD
    if self_easy:
        return scheduler.RATING_EASY
    return scheduler.RATING_GOOD


@dataclass
class Result:
    correct: bool
    near_miss: bool
    rating: int
    answer: str
    meaning_kr: str
    sentence: str | None
    source_label: str | None
    source_url: str | None
    due: dt.datetime
    interval_days: int
    finished: bool
    remaining: int


def answer(
    session: Session,
    question: Question,
    given: str,
    *,
    response_ms: int | None = None,
    self_easy: bool = False,
    now: dt.datetime | None = None,
    engine: scheduler.FSRSScheduler | scheduler.FixedScheduler | None = None,
) -> Result:
    """답을 채점하고 카드 상태를 갱신한 뒤 기록을 남긴다."""
    now = now or now_utc()
    engine = engine or scheduler.build()

    card = session.get(Card, question.card_id)
    if card is None:
        raise LookupError(f"카드를 찾을 수 없습니다: {question.card_id}")

    correct, near_miss = check_answer(question, given)
    rating = rating_for(
        correct=correct,
        near_miss=near_miss,
        stage=question.stage,
        response_ms=response_ms,
        self_easy=self_easy,
    )

    # 같은 세션에서 이미 틀렸다가 다시 온 카드인지 — successive relearning 표시.
    retry = session.scalar(
        select(func.count())
        .select_from(ReviewLog)
        .where(ReviewLog.card_id == card.id, ReviewLog.reviewed_at >= day_start(now))
    ) or 0

    before = engine.review(card, rating, now, duration_ms=response_ms)

    card.reps += 1
    if not correct:
        card.lapses += 1
        if card.lapses >= LEECH_LAPSES:
            card.leech = True
    card.stage = question.stage
    if question.occurrence_id:
        card.last_occurrence_id = question.occurrence_id

    session.add(
        ReviewLog(
            card_id=card.id,
            rating=rating,
            state=before.state,
            reviewed_at=now,
            due=before.due,
            stability=before.stability,
            difficulty=before.difficulty,
            elapsed_days=before.elapsed_days,
            last_elapsed_days=before.last_elapsed_days,
            scheduled_days=before.scheduled_days,
            correct=correct,
            response_ms=response_ms,
            stage=question.stage,
            occurrence_id=question.occurrence_id,
            in_session_retry=retry > 0,
        )
    )
    session.flush()

    state = queue_state(session, now)
    due = card.due or now
    return Result(
        correct=correct,
        near_miss=near_miss,
        rating=rating,
        answer=question.answer,
        meaning_kr=question.meaning_kr,
        sentence=question.sentence,
        source_label=question.source_label,
        source_url=question.source_url,
        due=due,
        interval_days=max(0, round((due - now).total_seconds() / 86400)),
        finished=state.finished,
        remaining=state.remaining,
    )


# --------------------------------------------------------------------------
# 현황
# --------------------------------------------------------------------------


@dataclass
class Progress:
    due: int
    new_available: int
    unresolved: int
    reviewed_today: int
    accuracy_today: float | None
    total_cards: int
    total_words: int
    #: 지금 낼 게 없을 때, 다음 카드가 due 가 되는 시각. 학습 단계 카드는 몇 분 뒤 돌아온다.
    next_due: dt.datetime | None = None


def progress(session: Session, now: dt.datetime | None = None) -> Progress:
    now = now or now_utc()
    state = queue_state(session, now)

    logs = session.scalars(
        select(ReviewLog).where(ReviewLog.reviewed_at >= day_start(now))
    ).all()
    accuracy = (sum(1 for log in logs if log.correct) / len(logs)) if logs else None

    return Progress(
        due=len(state.due),
        new_available=state.new_remaining,
        unresolved=len(state.retry_ready) + len(state.retry_waiting),
        reviewed_today=len(logs),
        accuracy_today=accuracy,
        total_cards=session.scalar(select(func.count()).select_from(Card)) or 0,
        total_words=session.scalar(select(func.count()).select_from(Word)) or 0,
        next_due=session.scalar(
            select(func.min(Card.due)).where(Card.due > now, Card.suspended.is_(False))
        ),
    )
