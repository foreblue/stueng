"""간격 스케줄러.

직접 구현하지 않는다. `fsrs` 패키지(DSR 모델)를 감싸서 우리 Card 행과 주고받는
얇은 층만 둔다. 여기서 하는 일은 세 가지다.

1. 우리 Card <-> fsrs.Card 변환
2. 연구 근거가 있는 기본값 고정 (learning steps 3회, 목표 파지율 0.9)
3. 복습 직전 상태 스냅샷 — fsrs 6.x 의 ReviewLog 는 rating 과 시각만 담기 때문에,
   나중에 파라미터를 재최적화할 때 쓸 재료는 우리가 직접 남겨야 한다.

`VOCAB_SCHEDULER=fixed` 로 두면 Karatas et al. (2025) 가 검증한 [1, 3, 9, 17]일
고정 간격으로 바뀐다. FSRS 가 실제로 더 나은지 나중에 비교할 대조군으로 남겨둔다.
"""

from __future__ import annotations

import datetime as dt
import logging
import os
from dataclasses import dataclass

import fsrs

from .models import Card

logger = logging.getLogger(__name__)

#: 목표 파지율. Wilson et al. (2019) 의 최적 정답률 ~85% 와 FSRS 권장 범위가 만나는 값.
DEFAULT_DESIRED_RETENTION = 0.9

#: 신규 카드가 졸업하기까지 거치는 인출 횟수. Nakata (2017) 에서 5회까지 이득이
#: 있었으나 3회 이후로는 세션 부담 대비 수익이 급감하고, 장기 파지는 분산이 지배한다.
LEARNING_STEPS = (
    dt.timedelta(minutes=1),
    dt.timedelta(minutes=6),
    dt.timedelta(minutes=12),
)

#: 틀린 카드가 돌아오는 간격. 실제 재출제는 세션 큐가 담당하므로 짧게 둔다.
RELEARNING_STEPS = (dt.timedelta(minutes=5),)

#: 복습 간 간격(일). 앞의 세 칸은 Karatas et al. (2025) 최적화 조건의 실제 스케줄
#: — 1일차 학습 후 3, 9, 17일차에 인출했으므로 간격이 +2, +6, +8 이다.
#: 논문은 17일에서 끝나므로 그 뒤는 우리가 이어 붙인 값이다.
FIXED_INTERVALS_DAYS = (2, 6, 8, 30, 90, 180)

RATING_AGAIN = 1
RATING_HARD = 2
RATING_GOOD = 3
RATING_EASY = 4

STATE_LEARNING = 1
STATE_REVIEW = 2
STATE_RELEARNING = 3


@dataclass(frozen=True)
class Snapshot:
    """복습 직전 카드 상태. ReviewLog 에 그대로 들어간다."""

    state: int
    due: dt.datetime | None
    stability: float | None
    difficulty: float | None
    elapsed_days: int
    last_elapsed_days: int
    scheduled_days: int


def _desired_retention() -> float:
    raw = os.environ.get("VOCAB_DESIRED_RETENTION")
    if not raw:
        return DEFAULT_DESIRED_RETENTION
    try:
        value = float(raw)
    except ValueError:
        return DEFAULT_DESIRED_RETENTION
    # 0.7 미만은 사실상 잊어버리는 스케줄이고, 0.97 이상은 복습량이 폭발한다.
    return min(max(value, 0.70), 0.97)


def parameters() -> tuple[float, ...] | None:
    """`VOCAB_FSRS_PARAMS` 에 담긴 개인 최적화 파라미터.

    `python -m vocab.optimize` 가 내 복습 기록으로 학습해 내놓는 값이다. 없으면
    fsrs 의 기본 파라미터를 쓴다 — 기본값도 대규모 데이터로 맞춰진 것이라 나쁘지 않고,
    기록이 적을 때 억지로 맞추면 오히려 나빠진다.
    """
    raw = os.environ.get("VOCAB_FSRS_PARAMS", "").strip()
    if not raw:
        return None
    try:
        values = tuple(float(part) for part in raw.replace(" ", "").split(","))
    except ValueError:
        logger.warning("VOCAB_FSRS_PARAMS 를 읽지 못해 기본 파라미터를 씁니다: %r", raw[:60])
        return None
    if len(values) != len(fsrs.Scheduler().parameters):
        logger.warning(
            "VOCAB_FSRS_PARAMS 길이가 %d개여야 하는데 %d개입니다. 기본값을 씁니다",
            len(fsrs.Scheduler().parameters), len(values),
        )
        return None
    return values


def _days(delta: dt.timedelta | None) -> int:
    return max(0, round(delta.total_seconds() / 86400)) if delta else 0


def _snapshot(card: Card, now: dt.datetime) -> Snapshot:
    elapsed = _days(now - card.last_review) if card.last_review else 0
    scheduled = _days(card.due - card.last_review) if card.last_review and card.due else 0
    previous = card.reviews[-1].elapsed_days if card.reviews else 0
    return Snapshot(
        state=card.state,
        due=card.due,
        stability=card.stability,
        difficulty=card.difficulty,
        elapsed_days=elapsed,
        last_elapsed_days=previous,
        scheduled_days=scheduled,
    )


class FSRSScheduler:
    """기본 스케줄러. 기억 상태를 모델링해 목표 파지율에 맞는 간격을 역산한다."""

    name = "fsrs"

    def __init__(self, desired_retention: float | None = None) -> None:
        self.desired_retention = desired_retention or _desired_retention()
        self.parameters = parameters()
        options = {
            "desired_retention": self.desired_retention,
            "learning_steps": list(LEARNING_STEPS),
            "relearning_steps": list(RELEARNING_STEPS),
        }
        if self.parameters:
            options["parameters"] = self.parameters
            logger.info("개인 최적화 FSRS 파라미터를 사용합니다")

        try:
            self._scheduler = fsrs.Scheduler(**options)
        except ValueError as e:
            # fsrs 는 파라미터마다 허용 범위를 검증한다. 잘못된 값을 그대로 두면
            # 스케줄러를 만들 때마다 터지므로 복습이 통째로 멈춘다. 설정 하나 때문에
            # 서비스가 죽는 것보다 기본 파라미터로 계속 도는 편이 낫다.
            logger.warning("VOCAB_FSRS_PARAMS 가 유효 범위를 벗어났습니다 (%s). 기본값을 씁니다", e)
            self.parameters = None
            options.pop("parameters")
            self._scheduler = fsrs.Scheduler(**options)

    def review(self, card: Card, rating: int, now: dt.datetime, *, duration_ms: int | None = None) -> Snapshot:
        """카드를 제자리에서 갱신하고, 갱신 전 상태를 돌려준다."""
        before = _snapshot(card, now)

        # fsrs 는 Review/Relearning 카드에 기억 상태가 채워져 있다고 단언한다.
        # 비어 있는 경로가 둘 있다 — FixedScheduler 로 돌리다 되돌아온 카드(고정 간격은
        # stability 를 만들지 않는다)와 손으로 고친 행이다. 그대로 넘기면 복습할 때마다
        # 터지므로, 기억 상태를 모르는 카드는 학습 단계부터 다시 쌓게 한다.
        state = card.state
        step = card.step
        if state != STATE_LEARNING and (card.stability is None or card.difficulty is None):
            state, step = STATE_LEARNING, 0

        source = fsrs.Card(
            card_id=card.id or 1,
            state=fsrs.State(state),
            step=step,
            stability=card.stability,
            difficulty=card.difficulty,
            due=card.due,
            last_review=card.last_review,
        )
        updated, _ = self._scheduler.review_card(
            source,
            fsrs.Rating(rating),
            review_datetime=now,
            review_duration=duration_ms,
        )

        card.state = int(updated.state)
        card.step = updated.step
        card.stability = updated.stability
        card.difficulty = updated.difficulty
        card.due = updated.due
        card.last_review = updated.last_review or now
        return before


class FixedScheduler:
    """Karatas et al. (2025) 의 고정 간격. FSRS 와 비교할 대조군.

    성공한 복습마다 간격 표의 다음 칸으로 넘어가고, 틀리면 처음으로 돌아간다.
    기억 상태를 모델링하지 않으므로 stability/difficulty 는 비워 둔다.
    """

    name = "fixed"

    def __init__(self, intervals_days: tuple[int, ...] = FIXED_INTERVALS_DAYS) -> None:
        self.intervals = intervals_days

    def review(self, card: Card, rating: int, now: dt.datetime, *, duration_ms: int | None = None) -> Snapshot:
        before = _snapshot(card, now)

        if rating == RATING_AGAIN:
            step = 0
            card.state = STATE_RELEARNING
            card.due = now + RELEARNING_STEPS[0]
        else:
            step = min((card.step or 0) + 1, len(self.intervals))
            card.state = STATE_REVIEW
            card.due = now + dt.timedelta(days=self.intervals[step - 1])

        card.step = step
        card.last_review = now
        return before


def build(name: str | None = None) -> FSRSScheduler | FixedScheduler:
    name = (name or os.environ.get("VOCAB_SCHEDULER") or "fsrs").lower()
    return FixedScheduler() if name == "fixed" else FSRSScheduler()
