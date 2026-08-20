"""주 1회 작문 과제와 로컬 워커 테스트"""

import datetime as dt
import json
import os
import sys
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(__file__))

os.environ.setdefault("TELEGRAM_BOT_TOKEN", "fake_token")
os.environ.setdefault("TELEGRAM_CHAT_ID", "123456")

import requests  # noqa: E402
from sqlalchemy import select  # noqa: E402

import config  # noqa: E402
from vocab import compose, scheduler, tutor  # noqa: E402
from vocab.db import create_all, make_engine, session_factory  # noqa: E402
from vocab.models import (  # noqa: E402
    KIND_WORD,
    STAGE_CLOZE,
    STAGE_PRODUCTION,
    STAGE_RECOGNITION,
    Card,
    Composition,
    Word,
)

#: 2026-08-20 은 목요일. 그 주 월요일은 08-17.
THURSDAY = dt.datetime(2026, 8, 20, 3, 0, tzinfo=dt.UTC)
MONDAY = dt.date(2026, 8, 17)


def make_session():
    engine = make_engine("sqlite://")
    create_all(engine)
    return session_factory(engine)()


def add_card(session, display, *, stage=STAGE_CLOZE, reps=3, last_review=None,
             stability=25.0, leech=False, known=False, mnemonic=None, suspended=False):
    word = Word(headword=display.lower(), display=display, kind=KIND_WORD,
                meaning_kr=f"{display}의 뜻", band="core", first_seen=dt.date(2026, 8, 1),
                known=known, mnemonic=mnemonic)
    session.add(word)
    session.flush()
    card = Card(word_id=word.id, stage=stage, reps=reps, stability=stability,
                difficulty=5.0, state=scheduler.STATE_REVIEW, leech=leech,
                suspended=suspended, lapses=9 if leech else 0,
                due=THURSDAY, last_review=last_review or THURSDAY - dt.timedelta(days=1))
    session.add(card)
    session.flush()
    return word


def configured():
    return patch.multiple(
        config,
        VOCAB_SERVER_URL="https://vocab.example.com",
        VOCAB_INGEST_TOKEN="secret",
    )


class FakeResponse:
    def __init__(self, payload, status=200):
        self._payload, self.status_code = payload, status
        self.ok = 200 <= status < 300
        self.text = str(payload)

    def json(self):
        return self._payload


# --------------------------------------------------------------------------
# 주차 경계
# --------------------------------------------------------------------------


def test_week_starts_on_monday():
    assert compose.week_start(THURSDAY) == MONDAY
    sunday = dt.datetime(2026, 8, 23, 3, 0, tzinfo=dt.UTC)
    assert compose.week_start(sunday) == MONDAY, "일요일은 아직 같은 주"
    next_monday = dt.datetime(2026, 8, 24, 3, 0, tzinfo=dt.UTC)
    assert compose.week_start(next_monday) == dt.date(2026, 8, 24)


# --------------------------------------------------------------------------
# 과제 생성
# --------------------------------------------------------------------------


def test_task_needs_enough_practised_cards():
    """인출해 본 적 없는 단어로 문장을 지으면 사전을 베끼는 일이 된다."""
    session = make_session()
    add_card(session, "alpha", reps=1)
    session.commit()
    assert compose.ensure(session, THURSDAY) is None


def test_task_uses_three_recently_practised_words():
    session = make_session()
    for name in ("alpha", "beta", "gamma", "delta"):
        add_card(session, name)
    session.commit()

    task = compose.ensure(session, THURSDAY)
    assert task is not None
    assert task.week_start == MONDAY
    assert len(task.word_list) == compose.WORDS_PER_TASK
    assert all("display" in w and "meaning_kr" in w for w in task.word_list)


def test_promoted_cards_are_preferred():
    """산출 단계까지 올라간 카드는 이미 한→영으로 꺼내 봤으니 쓰기에 알맞다."""
    session = make_session()
    add_card(session, "recog", stage=STAGE_RECOGNITION)
    add_card(session, "cloze", stage=STAGE_CLOZE)
    add_card(session, "produce", stage=STAGE_PRODUCTION)
    add_card(session, "another", stage=STAGE_PRODUCTION)
    session.commit()

    picked = [w["display"] for w in compose.ensure(session, THURSDAY).word_list]
    assert "recog" not in picked, f"가장 낮은 단계가 밀려야 한다: {picked}"


def test_stale_and_suspended_cards_are_skipped():
    session = make_session()
    old = THURSDAY - dt.timedelta(days=compose.RECENT_DAYS + 5)
    add_card(session, "stale", last_review=old)
    add_card(session, "suspended", suspended=True)
    add_card(session, "known", known=True)
    add_card(session, "fresh")
    session.commit()

    assert compose.ensure(session, THURSDAY) is None, "쓸 만한 카드가 하나뿐이면 과제를 내지 않는다"


def test_only_one_task_per_week():
    session = make_session()
    for name in ("alpha", "beta", "gamma"):
        add_card(session, name)
    session.commit()

    first = compose.ensure(session, THURSDAY)
    second = compose.ensure(session, THURSDAY + dt.timedelta(days=2))
    assert first.id == second.id
    assert session.scalar(select(Composition.week_start)) == MONDAY


# --------------------------------------------------------------------------
# 제출
# --------------------------------------------------------------------------


def _task(session):
    for name in ("alpha", "beta", "gamma"):
        add_card(session, name)
    session.commit()
    return compose.ensure(session, THURSDAY)


def test_submitting_marks_it_awaiting_feedback():
    session = make_session()
    task = _task(session)
    compose.submit(session, task, "  I wrote something.  ", THURSDAY)
    session.commit()

    assert task.text == "I wrote something.", "앞뒤 공백은 정리한다"
    assert task.submitted_at == THURSDAY
    assert task.awaiting_feedback is True
    assert compose.pending_feedback(session) == [task]


def test_empty_submission_is_rejected():
    session = make_session()
    task = _task(session)
    try:
        compose.submit(session, task, "   ", THURSDAY)
    except ValueError:
        pass
    else:
        raise AssertionError("빈 글은 제출되면 안 된다")
    assert task.text is None


def test_resubmitting_clears_the_old_feedback():
    """고친 글에 옛 첨삭이 붙어 있으면 무엇이 반영된 것인지 알 수 없다."""
    session = make_session()
    task = _task(session)
    compose.submit(session, task, "first draft", THURSDAY)
    task.feedback = "이전 첨삭"
    task.feedback_at = THURSDAY
    session.commit()

    compose.submit(session, task, "second draft", THURSDAY + dt.timedelta(hours=1))
    assert task.feedback is None and task.feedback_at is None
    assert task.awaiting_feedback is True


# --------------------------------------------------------------------------
# 기억술 대상
# --------------------------------------------------------------------------


def test_only_leeches_get_a_mnemonic():
    """니모닉은 시간이 지나면 이점이 감쇠한다. 정말 막힌 카드에만 붙인다."""
    session = make_session()
    add_card(session, "fine")
    add_card(session, "stuck", leech=True)
    session.commit()

    assert [w.display for w in compose.leeches_without_mnemonic(session)] == ["stuck"]


def test_words_that_already_have_a_mnemonic_are_not_requeued():
    session = make_session()
    add_card(session, "stuck", leech=True, mnemonic="이미 있는 기억술")
    session.commit()
    assert compose.leeches_without_mnemonic(session) == []


def test_known_words_are_not_requeued():
    session = make_session()
    add_card(session, "stuck", leech=True, known=True)
    session.commit()
    assert compose.leeches_without_mnemonic(session) == []


# --------------------------------------------------------------------------
# 로컬 워커
# --------------------------------------------------------------------------


TASKS = {
    "mnemonics": [
        {"word_id": 7, "display": "bipartisan", "meaning_kr": "양당의",
         "kind": "word", "examples": ["a bipartisan deal"]}
    ],
    "compositions": [
        {"composition_id": 3, "week_start": "2026-08-17",
         "words": [{"id": 1, "display": "leverage", "meaning_kr": "영향력"}],
         "text": "I have leverage."}
    ],
}


def test_tutor_requires_configuration():
    with patch.multiple(config, VOCAB_SERVER_URL="", VOCAB_INGEST_TOKEN="secret"):
        try:
            tutor.fetch_tasks()
        except tutor.TutorError as e:
            assert "VOCAB_SERVER_URL" in str(e)
        else:
            raise AssertionError("서버 주소 없이 돌면 안 된다")


def test_dry_run_makes_no_llm_calls():
    import analyzer

    with configured(), patch.object(requests, "get", return_value=FakeResponse(TASKS)), \
         patch.object(analyzer, "complete") as complete, \
         patch.object(requests, "post") as post:
        result = tutor.run(dry_run=True)
        complete.assert_not_called()
        post.assert_not_called()

    assert result["compositions"] == ["2026-08-17"]
    assert result["mnemonics"] == ["bipartisan"]


def test_tutor_round_trip():
    import analyzer

    with configured(), patch.object(requests, "get", return_value=FakeResponse(TASKS)), \
         patch.object(analyzer, "complete", return_value=("생성된 결과", "")), \
         patch.object(requests, "post",
                      return_value=FakeResponse({"mnemonics": 1, "feedback": 1})) as post:
        result = tutor.run()

    body = post.call_args.kwargs["json"]
    assert body["feedback"] == [{"composition_id": 3, "text": "생성된 결과"}]
    assert body["mnemonics"] == [{"word_id": 7, "text": "생성된 결과"}]
    assert post.call_args.kwargs["headers"]["X-Ingest-Token"] == "secret"
    assert result["attempted"] == {"feedback": 1, "mnemonics": 1}


def test_llm_failure_skips_that_item_without_losing_the_others():
    """한 건이 실패해도 나머지는 반영돼야 한다. 다음 실행에서 실패한 것만 다시 잡힌다."""
    import analyzer

    answers = iter([("첨삭 결과", ""), ("", "프록시 연결 실패")])
    with configured(), patch.object(requests, "get", return_value=FakeResponse(TASKS)), \
         patch.object(analyzer, "complete", side_effect=lambda *a, **k: next(answers)), \
         patch.object(requests, "post",
                      return_value=FakeResponse({"mnemonics": 0, "feedback": 1})) as post:
        tutor.run()

    body = post.call_args.kwargs["json"]
    assert len(body["feedback"]) == 1
    assert body["mnemonics"] == []


def test_overlong_mnemonic_is_discarded():
    """카드 아래 한 칸에 들어갈 분량이 아니면 버린다."""
    import analyzer

    with configured(), patch.object(analyzer, "complete", return_value=("가" * 500, "")):
        assert tutor.make_mnemonic(TASKS["mnemonics"][0]) is None

    with configured(), patch.object(analyzer, "complete", return_value=("  짧은 기억술  ", "")):
        assert tutor.make_mnemonic(TASKS["mnemonics"][0]) == "짧은 기억술"


def test_composition_prompt_carries_words_and_text():
    import analyzer

    captured = {}

    def fake(prompt, **kwargs):
        captured["prompt"] = prompt
        return "결과", ""

    with configured(), patch.object(analyzer, "complete", side_effect=fake):
        tutor.write_feedback(TASKS["compositions"][0])

    assert "leverage" in captured["prompt"]
    assert "I have leverage." in captured["prompt"]
    assert "Korean" in captured["prompt"]


def test_nothing_to_do_makes_no_post():
    with configured(), \
         patch.object(requests, "get", return_value=FakeResponse({"mnemonics": [], "compositions": []})), \
         patch.object(requests, "post") as post:
        result = tutor.run()
        post.assert_not_called()
    assert result == {"mnemonics": 0, "feedback": 0,
                      "attempted": {"feedback": 0, "mnemonics": 0}}


def test_limit_caps_work_per_run():
    tasks = {
        "mnemonics": [dict(TASKS["mnemonics"][0], word_id=i) for i in range(10)],
        "compositions": [],
    }
    import analyzer

    with configured(), patch.object(requests, "get", return_value=FakeResponse(tasks)), \
         patch.object(analyzer, "complete", return_value=("훅", "")), \
         patch.object(requests, "post", return_value=FakeResponse({"mnemonics": 2, "feedback": 0})) as post:
        tutor.run(limit=2)

    assert len(post.call_args.kwargs["json"]["mnemonics"]) == 2


def test_words_column_survives_a_deleted_word():
    """과제는 표시 문자열을 함께 저장한다. 어휘가 지워져도 기록이 남아야 한다."""
    session = make_session()
    task = _task(session)
    stored = json.loads(task.words)
    assert stored[0]["display"]
    assert stored[0]["meaning_kr"]


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
