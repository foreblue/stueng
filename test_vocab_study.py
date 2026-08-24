"""vocab 세션 로직·스케줄러 테스트"""

import contextlib
import datetime as dt
import os
import random
import sys

sys.path.insert(0, os.path.dirname(__file__))

from sqlalchemy import select

from vocab import scheduler, study
from vocab.db import create_all, make_engine, session_factory
from vocab.models import (
    KIND_EXPRESSION,
    KIND_WORD,
    SOURCE_CORRECTION,
    SOURCE_UPFIRST,
    STAGE_CLOZE,
    STAGE_PRODUCTION,
    STAGE_RECOGNITION,
    Card,
    Occurrence,
    ReviewLog,
    Word,
    sentence_key,
)

TODAY = dt.date(2026, 8, 20)
NOON = dt.datetime(2026, 8, 20, 3, 0, tzinfo=dt.UTC)  # 서울 정오


@contextlib.contextmanager
def settings(**overrides):
    """study 모듈 상수를 잠시 바꾼다."""
    original = {k: getattr(study, k) for k in overrides}
    for key, value in overrides.items():
        setattr(study, key, value)
    try:
        yield
    finally:
        for key, value in original.items():
            setattr(study, key, value)


def make_session():
    engine = make_engine("sqlite://")
    create_all(engine)
    return session_factory(engine)()


def add_word(session, display, meaning, *, kind=KIND_WORD, band="core", sentences=(),
             source=SOURCE_UPFIRST, first_seen=TODAY, known=False):
    word = Word(
        headword=display.lower(), display=display, kind=kind, meaning_kr=meaning,
        band=band, known=known, first_seen=first_seen,
    )
    session.add(word)
    session.flush()
    for text in sentences:
        session.add(
            Occurrence(
                word_id=word.id, sentence=text, sentence_hash=sentence_key(text),
                source_kind=source, source_title="출처", occurred_on=first_seen,
            )
        )
    session.flush()
    return word


def add_card(session, word, *, stability=None, due=NOON, state=scheduler.STATE_REVIEW):
    card = Card(word_id=word.id, due=due, state=state, stability=stability,
                difficulty=5.0 if stability else None, created_at=due)
    session.add(card)
    session.flush()
    return card


def log(session, card, *, correct, at):
    session.add(
        ReviewLog(card_id=card.id, rating=3 if correct else 1, state=2, reviewed_at=at,
                  correct=correct, stage=STAGE_RECOGNITION, elapsed_days=0,
                  last_elapsed_days=0, scheduled_days=0)
    )
    session.flush()


# --------------------------------------------------------------------------
# 하루 경계
# --------------------------------------------------------------------------


def test_day_starts_at_4am_local():
    """새벽 2시 학습은 전날 몫이다. 자정 경계로 끊으면 밤샘 학습이 두 날로 쪼개진다."""
    seoul = study.timezone()
    late_night = dt.datetime(2026, 8, 21, 2, 0, tzinfo=seoul)
    morning = dt.datetime(2026, 8, 21, 9, 0, tzinfo=seoul)

    assert study.day_start(late_night).astimezone(seoul).date() == dt.date(2026, 8, 20)
    assert study.day_start(morning).astimezone(seoul).date() == dt.date(2026, 8, 21)


# --------------------------------------------------------------------------
# 신규 카드 투입 우선순위
# --------------------------------------------------------------------------


def test_corrections_are_introduced_first():
    """수업에서 내가 틀린 표현이 팟캐스트 어휘보다 먼저다."""
    session = make_session()
    add_word(session, "ceasefire", "휴전", sentences=["a", "b", "c"])
    add_word(session, "I really like it", "자연스러운 표현", kind=KIND_EXPRESSION,
             sentences=["I really like it."], source=SOURCE_CORRECTION)
    session.commit()

    cards = study.introduce(session, 1, NOON)
    assert cards[0].word.display == "I really like it"


def test_only_core_band_becomes_cards():
    """known/rare 는 카드로 만들지 않는다.

    순위만 매기고 거르지 않으면 core 가 바닥난 뒤 이미 아는 단어(settlements,
    poverty…)와 조어로 매일 10장씩 카드가 생긴다. /api/handled 는 그 둘을
    "이미 다루는 것" 으로 빼고 있으므로 시스템의 두 쪽이 서로 다른 말을 하게 된다.
    """
    session = make_session()
    add_word(session, "poverty", "빈곤", band="known", sentences=["x", "y", "z"])
    add_word(session, "malinvestment", "잘못된 투자", band="rare")
    add_word(session, "stalemate", "교착", band="core")
    session.commit()

    cards = study.introduce(session, 10, NOON)
    assert [c.word.display for c in cards] == ["stalemate"]


def test_no_new_cards_when_core_runs_out():
    """억지로 채우는 것보다 0 이라고 말하는 편이 정직하다."""
    session = make_session()
    add_word(session, "poverty", "빈곤", band="known")
    session.commit()

    assert study.introduce(session, 10, NOON) == []
    assert study.queue_state(session, NOON).new_remaining == 0


def test_words_with_more_examples_come_first():
    """예문이 많은 카드가 더 값지다 — 문맥 순환이 가능하기 때문."""
    session = make_session()
    add_word(session, "alpha", "가", sentences=["one"])
    add_word(session, "beta", "나", sentences=["one", "two", "three"])
    session.commit()

    cards = study.introduce(session, 2, NOON)
    assert [c.word.display for c in cards] == ["beta", "alpha"]


def test_known_marked_words_are_never_introduced():
    session = make_session()
    add_word(session, "obvious", "뻔한", known=True)
    session.commit()

    assert study.introduce(session, 5, NOON) == []


def test_daily_new_limit_is_respected():
    session = make_session()
    for i in range(10):
        add_word(session, f"word{i}", f"뜻{i}")
    session.commit()

    with settings(NEW_PER_DAY=3):
        study.introduce(session, 3, NOON)
        assert study.queue_state(session, NOON).new_remaining == 0
        assert study.introduced_today(session, NOON) == 3


# --------------------------------------------------------------------------
# 세션 내 재인출 (successive relearning)
# --------------------------------------------------------------------------


def test_failed_card_waits_before_coming_back():
    """바로 다시 물으면 인출이 아니라 단기 기억 되뇌기가 된다."""
    session = make_session()
    card = add_card(session, add_word(session, "alpha", "가"))
    log(session, card, correct=False, at=NOON)
    session.commit()

    with settings(NEW_PER_DAY=0, RETRY_GAP=3):
        state = study.queue_state(session, NOON)
        assert state.retry_waiting == [card.id]
        assert state.retry_ready == []


def test_failed_card_returns_after_gap():
    session = make_session()
    failed = add_card(session, add_word(session, "alpha", "가"))
    others = [add_card(session, add_word(session, f"w{i}", f"뜻{i}")) for i in range(3)]

    log(session, failed, correct=False, at=NOON)
    for i, other in enumerate(others, 1):
        log(session, other, correct=True, at=NOON + dt.timedelta(seconds=i))
    session.commit()

    with settings(NEW_PER_DAY=0, RETRY_GAP=3):
        state = study.queue_state(session, NOON + dt.timedelta(minutes=1))
        assert state.retry_ready == [failed.id]


def test_session_never_ends_with_unresolved_card():
    """간격을 못 채웠어도 남은 게 그것뿐이면 낸다. 못 맞힌 채로 끝나면 안 된다."""
    session = make_session()
    card = add_card(session, add_word(session, "alpha", "가"))
    log(session, card, correct=False, at=NOON)
    session.commit()

    with settings(NEW_PER_DAY=0, RETRY_GAP=3):
        state = study.queue_state(session, NOON)
        assert not state.finished
        assert study.next_card(session, NOON).id == card.id


def test_card_answered_correctly_leaves_the_retry_queue():
    session = make_session()
    card = add_card(session, add_word(session, "alpha", "가"))
    log(session, card, correct=False, at=NOON)
    log(session, card, correct=True, at=NOON + dt.timedelta(minutes=1))
    session.commit()

    with settings(NEW_PER_DAY=0):
        state = study.queue_state(session, NOON + dt.timedelta(minutes=2))
        assert state.retry_ready == [] and state.retry_waiting == []


def test_retry_queue_survives_restart():
    """세션 상태를 메모리에 들고 있지 않으므로 새 세션에서도 그대로 보인다."""
    engine = make_engine("sqlite://")
    create_all(engine)
    factory = session_factory(engine)

    with factory() as session:
        card = add_card(session, add_word(session, "alpha", "가"))
        log(session, card, correct=False, at=NOON)
        session.commit()
        card_id = card.id

    with factory() as fresh, settings(NEW_PER_DAY=0):
        assert study.queue_state(fresh, NOON).retry_waiting == [card_id]


# --------------------------------------------------------------------------
# 빈칸 만들기
# --------------------------------------------------------------------------


def test_cloze_blanks_exact_word():
    made = study.make_cloze("The ceasefire is showing cracks.", "ceasefire")
    assert made == ("The ____ is showing cracks.", "ceasefire")


def test_cloze_catches_inflected_form():
    """예문에 있는 것은 'collaborating' 이고 표제어는 'collaborate' 다."""
    made = study.make_cloze("After collaborating with Columbia, she brings the answer.", "collaborate")
    assert made is not None
    blanked, surface = made
    assert surface == "collaborating"
    assert study.BLANK in blanked and "collaborating" not in blanked


def test_cloze_handles_phrases():
    made = study.make_cloze("Scientists say that sitting is the new smoking.", "sitting is the new smoking")
    assert made is not None
    assert made[0] == "Scientists say that ____."


def test_cloze_returns_none_when_word_absent():
    assert study.make_cloze("A totally unrelated sentence.", "ceasefire") is None


# --------------------------------------------------------------------------
# 형식 승급
# --------------------------------------------------------------------------


def test_new_card_is_multiple_choice():
    session = make_session()
    for i in range(5):
        add_word(session, f"filler{i}", f"채움{i}")
    card = add_card(session, add_word(session, "stalemate", "교착 상태",
                                      sentences=["The talks reached a stalemate."]), stability=1.0)
    session.commit()

    question = study.build_question(session, card, rng=random.Random(1))
    assert question.stage == STAGE_RECOGNITION
    assert question.choices is not None and len(question.choices) == study.CHOICE_COUNT
    assert question.answer in question.choices


def test_stable_card_promotes_to_cloze():
    session = make_session()
    card = add_card(
        session,
        add_word(session, "stalemate", "교착 상태", sentences=["The talks reached a stalemate."]),
        stability=study.CLOZE_MIN_STABILITY + 1,
    )
    session.commit()

    question = study.build_question(session, card)
    assert question.stage == STAGE_CLOZE
    assert study.BLANK in question.prompt
    assert question.answer == "stalemate"
    assert question.hint == "교착 상태", "빈칸 문제에는 뜻을 힌트로 준다"


def test_very_stable_card_promotes_to_production():
    session = make_session()
    card = add_card(
        session,
        add_word(session, "stalemate", "교착 상태", sentences=["The talks reached a stalemate."]),
        stability=study.PRODUCTION_MIN_STABILITY + 1,
    )
    session.commit()

    question = study.build_question(session, card)
    assert question.stage == STAGE_PRODUCTION
    assert question.answer == "stalemate"
    assert "교착 상태" in question.prompt
    assert question.choices is None


def test_cloze_falls_back_when_no_example_contains_the_word():
    """빈칸을 못 만들면 4지선다로 내려간다. 낼 문제가 없어지면 안 된다."""
    session = make_session()
    for i in range(5):
        add_word(session, f"filler{i}", f"채움{i}")
    card = add_card(
        session,
        add_word(session, "stalemate", "교착 상태", sentences=["표제어가 없는 문장."]),
        stability=study.CLOZE_MIN_STABILITY + 1,
    )
    session.commit()

    question = study.build_question(session, card, rng=random.Random(1))
    assert question.stage == STAGE_RECOGNITION


def test_cloze_tries_other_examples_before_giving_up():
    session = make_session()
    card = add_card(
        session,
        add_word(session, "leverage", "영향력",
                 sentences=["빈칸을 만들 수 없는 문장.", "It is a key piece of leverage."]),
        stability=study.CLOZE_MIN_STABILITY + 1,
    )
    session.commit()

    question = study.build_question(session, card)
    assert question.stage == STAGE_CLOZE
    assert question.prompt == "It is a key piece of ____."


# --------------------------------------------------------------------------
# 예문 순환
# --------------------------------------------------------------------------


def test_recognition_stage_keeps_one_fixed_example():
    """신규 단계에서 문맥을 흩뿌리면 오히려 방해가 된다는 조절 효과가 있다."""
    session = make_session()
    for i in range(5):
        add_word(session, f"filler{i}", f"채움{i}")
    word = add_word(session, "leverage", "영향력", sentences=["first.", "second.", "third."])
    card = add_card(session, word, stability=1.0)
    card.last_occurrence_id = word.occurrences[0].id
    session.commit()

    question = study.build_question(session, card, rng=random.Random(1))
    assert question.sentence == "first."


def test_promoted_card_rotates_examples():
    session = make_session()
    word = add_word(session, "leverage", "영향력",
                    sentences=["A has leverage.", "B has leverage.", "C has leverage."])
    card = add_card(session, word, stability=study.CLOZE_MIN_STABILITY + 1)
    card.last_occurrence_id = word.occurrences[0].id
    session.commit()

    question = study.build_question(session, card)
    assert question.occurrence_id != word.occurrences[0].id, "직전에 쓴 예문은 뒤로 민다"


def test_rotation_prefers_least_used_example():
    session = make_session()
    word = add_word(session, "leverage", "영향력",
                    sentences=["A has leverage.", "B has leverage."])
    card = add_card(session, word, stability=study.CLOZE_MIN_STABILITY + 1)
    first, second = word.occurrences
    for _ in range(3):
        session.add(
            ReviewLog(card_id=card.id, rating=3, state=2, reviewed_at=NOON, correct=True,
                      stage=STAGE_CLOZE, occurrence_id=second.id, elapsed_days=0,
                      last_elapsed_days=0, scheduled_days=0)
        )
    session.commit()

    assert study.build_question(session, card).occurrence_id == first.id


# --------------------------------------------------------------------------
# 채점
# --------------------------------------------------------------------------


def _typed_question(answer="collaborating", stage=STAGE_CLOZE):
    return study.Question(card_id=1, stage=stage, kind=KIND_WORD, display=answer,
                          meaning_kr="뜻", prompt="p", answer=answer)


def test_exact_answer_is_correct():
    assert study.check_answer(_typed_question(), "collaborating") == (True, False)


def test_case_and_space_differences_are_forgiven():
    assert study.check_answer(_typed_question(), "  Collaborating  ") == (True, False)


def test_inflection_counts_as_near_miss():
    """원형으로 답해도 맞은 것으로 보되, 완전한 정답으로 올리지는 않는다."""
    correct, near = study.check_answer(_typed_question(), "collaborate")
    assert (correct, near) == (True, True)


def test_single_typo_counts_as_near_miss():
    correct, near = study.check_answer(_typed_question(answer="stalemate"), "stalmate")
    assert (correct, near) == (True, True)


def test_wrong_answer_is_wrong():
    assert study.check_answer(_typed_question(), "nonsense") == (False, False)


def test_empty_answer_is_wrong():
    assert study.check_answer(_typed_question(), "   ") == (False, False)


def test_multiple_choice_requires_exact_match():
    """4지선다는 오타 허용이 없다. 보기를 고르는 것이므로 근사 일치가 성립하지 않는다."""
    question = study.Question(card_id=1, stage=STAGE_RECOGNITION, kind=KIND_WORD,
                              display="x", meaning_kr="교착 상태", prompt="x",
                              answer="교착 상태", choices=["교착 상태", "휴전", "빈곤", "영향력"])
    assert study.check_answer(question, "교착 상태") == (True, False)
    assert study.check_answer(question, "교착") == (False, False)


def test_rating_mapping():
    assert study.rating_for(correct=False, near_miss=False, stage=STAGE_RECOGNITION,
                            response_ms=100) == scheduler.RATING_AGAIN
    assert study.rating_for(correct=True, near_miss=True, stage=STAGE_CLOZE,
                            response_ms=100) == scheduler.RATING_HARD
    assert study.rating_for(correct=True, near_miss=False, stage=STAGE_RECOGNITION,
                            response_ms=100) == scheduler.RATING_GOOD
    assert study.rating_for(correct=True, near_miss=False, stage=STAGE_RECOGNITION,
                            response_ms=100, self_easy=True) == scheduler.RATING_EASY


def test_slow_correct_answer_is_downgraded_to_hard():
    """자기평가 대신 반응 시간이라는 객관 지표를 쓴다."""
    slow = study.SLOW_MS[STAGE_RECOGNITION] + 1
    assert study.rating_for(correct=True, near_miss=False, stage=STAGE_RECOGNITION,
                            response_ms=slow) == scheduler.RATING_HARD


# --------------------------------------------------------------------------
# 기록
# --------------------------------------------------------------------------


def test_answer_writes_objective_metrics():
    session = make_session()
    word = add_word(session, "stalemate", "교착 상태", sentences=["The talks reached a stalemate."])
    card = add_card(session, word, stability=study.CLOZE_MIN_STABILITY + 1)
    session.commit()

    question = study.build_question(session, card)
    result = study.answer(session, question, "stalemate", response_ms=4321, now=NOON)
    session.commit()

    entry = session.scalar(select(ReviewLog))
    assert result.correct
    assert entry.response_ms == 4321
    assert entry.stage == STAGE_CLOZE
    assert entry.occurrence_id == word.occurrences[0].id
    assert entry.correct is True
    assert entry.in_session_retry is False


def test_learning_steps_are_not_mistaken_for_retries():
    """학습 단계(1/6/12분)로 같은 날 다시 나온 것은 재인출이 아니다.

    오답 여부를 안 보면 신규 카드의 2·3회차가 전부 재인출로 찍혀서, 이 필드로는
    아무것도 구분할 수 없게 된다.
    """
    session = make_session()
    for i in range(5):
        add_word(session, f"filler{i}", f"채움{i}")
    card = add_card(session, add_word(session, "stalemate", "교착 상태"), stability=1.0)
    session.commit()

    rng = random.Random(5)
    now = NOON
    for _ in range(3):
        question = study.build_question(session, card, rng=rng)
        study.answer(session, question, question.answer, now=now)
        now += dt.timedelta(minutes=7)
    session.commit()

    flags = [entry.in_session_retry for entry in session.scalars(select(ReviewLog)).all()]
    assert flags == [False, False, False], f"모두 정답인데 재인출로 찍혔다: {flags}"


def test_second_attempt_is_flagged_as_in_session_retry():
    session = make_session()
    for i in range(5):
        add_word(session, f"filler{i}", f"채움{i}")
    card = add_card(session, add_word(session, "stalemate", "교착 상태"), stability=1.0)
    session.commit()

    rng = random.Random(3)
    question = study.build_question(session, card, rng=rng)
    study.answer(session, question, "틀린 답", now=NOON)
    retry = study.build_question(session, card, rng=rng)
    study.answer(session, retry, retry.answer, now=NOON + dt.timedelta(minutes=1))
    session.commit()

    logs = session.scalars(select(ReviewLog).order_by(ReviewLog.reviewed_at)).all()
    assert [entry.in_session_retry for entry in logs] == [False, True]


def test_snapshot_records_state_before_review():
    """ReviewLog 는 복습 '직전' 상태를 담아야 재최적화에 쓸 수 있다."""
    session = make_session()
    card = add_card(session, add_word(session, "stalemate", "교착"), stability=30.0)
    before_stability = card.stability
    session.commit()

    question = study.Question(card_id=card.id, stage=STAGE_RECOGNITION, kind=KIND_WORD,
                              display="stalemate", meaning_kr="교착", prompt="p",
                              answer="교착", choices=["교착", "a", "b", "c"])
    study.answer(session, question, "교착", now=NOON)
    session.commit()

    entry = session.scalar(select(ReviewLog))
    assert entry.stability == before_stability
    assert card.stability != before_stability, "카드 자체는 갱신됐어야 한다"


def test_repeated_failures_mark_a_leech():
    session = make_session()
    card = add_card(session, add_word(session, "stalemate", "교착"))
    card.lapses = study.LEECH_LAPSES - 1
    session.commit()

    question = study.Question(card_id=card.id, stage=STAGE_RECOGNITION, kind=KIND_WORD,
                              display="stalemate", meaning_kr="교착", prompt="p",
                              answer="교착", choices=["교착", "a", "b", "c"])
    study.answer(session, question, "틀림", now=NOON)
    session.commit()

    assert card.leech is True


# --------------------------------------------------------------------------
# 스케줄러
# --------------------------------------------------------------------------


def test_new_card_graduates_after_three_retrievals():
    """Nakata (2017) — 첫 세션 인출 3회. FSRS learning steps 설정과 맞는지 확인한다."""
    session = make_session()
    card = add_card(session, add_word(session, "stalemate", "교착"),
                    state=scheduler.STATE_LEARNING)
    card.step = 0
    engine = scheduler.FSRSScheduler()

    now = NOON
    states, intervals = [], []
    for _ in range(3):
        engine.review(card, scheduler.RATING_GOOD, now)
        states.append(card.state)
        intervals.append(card.due - now)
        now = card.due

    assert states == [scheduler.STATE_LEARNING, scheduler.STATE_LEARNING, scheduler.STATE_REVIEW]
    assert intervals[0] < dt.timedelta(hours=1), "학습 단계에서는 분 단위로 돌아온다"
    assert intervals[2] >= dt.timedelta(days=1), "졸업하면 하루 이상 뒤로 간다"


def test_failing_a_review_card_sends_it_to_relearning():
    session = make_session()
    card = add_card(session, add_word(session, "stalemate", "교착"), stability=30.0)
    card.difficulty = 5.0
    card.last_review = NOON - dt.timedelta(days=10)

    scheduler.FSRSScheduler().review(card, scheduler.RATING_AGAIN, NOON)
    assert card.state == scheduler.STATE_RELEARNING
    assert card.due - NOON <= dt.timedelta(minutes=10)


def test_fixed_scheduler_matches_the_published_schedule():
    """Karatas et al. (2025): 1일차 학습 후 3, 9, 17일차 인출 -> 간격 +2, +6, +8."""
    session = make_session()
    card = add_card(session, add_word(session, "stalemate", "교착"))
    card.step = 0
    engine = scheduler.FixedScheduler()

    now = NOON
    gaps = []
    for _ in range(3):
        engine.review(card, scheduler.RATING_GOOD, now)
        gaps.append((card.due - now).days)
        now = card.due

    assert gaps == [2, 6, 8]


def test_fixed_scheduler_resets_on_failure():
    session = make_session()
    card = add_card(session, add_word(session, "stalemate", "교착"))
    engine = scheduler.FixedScheduler()
    engine.review(card, scheduler.RATING_GOOD, NOON)
    engine.review(card, scheduler.RATING_GOOD, NOON)
    engine.review(card, scheduler.RATING_AGAIN, NOON)
    assert card.step == 0 and card.state == scheduler.STATE_RELEARNING


def test_review_card_without_memory_state_is_recovered():
    """FixedScheduler 로 돌리다 FSRS 로 되돌아온 카드는 stability 가 비어 있다.

    그대로 넘기면 fsrs 가 단언에서 죽는다. 복습할 때마다 터지는 종류의 버그다.
    """
    session = make_session()
    card = add_card(session, add_word(session, "stalemate", "교착"),
                    state=scheduler.STATE_REVIEW)
    assert card.stability is None

    scheduler.FSRSScheduler().review(card, scheduler.RATING_GOOD, NOON)
    assert card.stability is not None
    assert card.due > NOON


def test_scheduler_selection_from_env():
    assert scheduler.build("fixed").name == "fixed"
    assert scheduler.build("fsrs").name == "fsrs"
    assert scheduler.build().name == "fsrs"


def test_desired_retention_is_clamped_to_a_sane_range():
    os.environ["VOCAB_DESIRED_RETENTION"] = "0.5"
    try:
        assert scheduler.FSRSScheduler().desired_retention == 0.70
    finally:
        del os.environ["VOCAB_DESIRED_RETENTION"]
    assert scheduler.FSRSScheduler().desired_retention == scheduler.DEFAULT_DESIRED_RETENTION


# --------------------------------------------------------------------------
# 현황
# --------------------------------------------------------------------------


def test_progress_reports_accuracy_and_next_due():
    session = make_session()
    card = add_card(session, add_word(session, "alpha", "가"),
                    due=NOON + dt.timedelta(minutes=6))
    log(session, card, correct=True, at=NOON)
    log(session, card, correct=False, at=NOON)
    session.commit()

    with settings(NEW_PER_DAY=0):
        state = study.progress(session, NOON)
    assert state.reviewed_today == 2
    assert state.accuracy_today == 0.5
    assert state.next_due == NOON + dt.timedelta(minutes=6)


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failed = 0
    for test in tests:
        try:
            test()
            print(f"PASS: {test.__name__}")
        except AssertionError as e:
            failed += 1
            print(f"FAIL: {test.__name__} — {e}")
        except Exception as e:
            failed += 1
            print(f"ERROR: {test.__name__} — {type(e).__name__}: {e}")

    print(f"\n{'=' * 40}")
    print(f"결과: {len(tests) - failed}/{len(tests)} 통과")
    sys.exit(1 if failed else 0)
