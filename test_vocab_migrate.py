"""종류(word/expression) 정리 테스트"""

import datetime as dt
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))

from sqlalchemy import select

from vocab import collect, migrate
from vocab.db import create_all, make_engine, session_factory
from vocab.models import (
    KIND_EXPRESSION,
    KIND_WORD,
    Card,
    Occurrence,
    ReviewLog,
    Word,
    kind_for,
    sentence_key,
)

TODAY = dt.date(2026, 8, 25)
NOW = dt.datetime(2026, 8, 25, 3, 0, tzinfo=dt.UTC)


def make_session():
    engine = make_engine("sqlite://")
    create_all(engine)
    return session_factory(engine)()


def add(session, display, kind, *, sentences=(), first_seen=TODAY, carded=False, reviews=0):
    word = Word(headword=display.lower(), display=display, kind=kind, meaning_kr=f"{display}의 뜻",
                band="core", first_seen=first_seen)
    session.add(word)
    session.flush()
    for text in sentences:
        session.add(Occurrence(word_id=word.id, sentence=text, sentence_hash=sentence_key(text),
                               source_kind="upfirst", source_title="출처", occurred_on=first_seen))
    if carded or reviews:
        card = Card(word_id=word.id, due=NOW)
        session.add(card)
        session.flush()
        for _ in range(reviews):
            session.add(ReviewLog(card_id=card.id, rating=3, state=2, reviewed_at=NOW,
                                  correct=True, stage="recognition", elapsed_days=0,
                                  last_elapsed_days=0, scheduled_days=0))
    session.flush()
    return word


# --------------------------------------------------------------------------
# 종류 판정
# --------------------------------------------------------------------------


def test_kind_follows_the_written_form():
    assert kind_for("ceasefire") == KIND_WORD
    assert kind_for("backfire") == KIND_WORD
    assert kind_for("missing in action") == KIND_EXPRESSION
    assert kind_for("tax avoidance") == KIND_EXPRESSION
    assert kind_for("wait-and-see") == KIND_EXPRESSION


def test_entry_ignores_the_kind_it_was_given():
    """LLM 이 어느 바구니에 넣었든, 수업 노트 표에서 왔든, /ingest 로 왔든 형태가 정한다."""
    wrong_word = collect.Entry(display="missing in action", kind=KIND_WORD, meaning_kr="뜻",
                               source_kind="upfirst", source_title="t", occurred_on=TODAY)
    wrong_expr = collect.Entry(display="backfire", kind=KIND_EXPRESSION, meaning_kr="뜻",
                               source_kind="upfirst", source_title="t", occurred_on=TODAY)
    assert wrong_word.kind == KIND_EXPRESSION
    assert wrong_expr.kind == KIND_WORD


# --------------------------------------------------------------------------
# 재분류
# --------------------------------------------------------------------------


def test_mismatched_words_are_retagged():
    session = make_session()
    add(session, "backfire", KIND_EXPRESSION)
    add(session, "tax avoidance", KIND_WORD)
    add(session, "ceasefire", KIND_WORD)
    session.commit()

    report = migrate.fix_kinds(session)
    assert report.retagged == 2 and report.merged == 0

    kinds = {w.display: w.kind for w in session.scalars(select(Word)).all()}
    assert kinds == {"backfire": KIND_WORD, "tax avoidance": KIND_EXPRESSION,
                     "ceasefire": KIND_WORD}


def test_dry_run_changes_nothing():
    session = make_session()
    add(session, "backfire", KIND_EXPRESSION)
    session.commit()

    report = migrate.fix_kinds(session, dry_run=True)
    assert report.retagged == 1
    assert session.scalar(select(Word)).kind == KIND_EXPRESSION, "미리보기가 데이터를 바꿨다"


def test_already_correct_data_is_left_alone():
    session = make_session()
    add(session, "ceasefire", KIND_WORD)
    add(session, "break the ice", KIND_EXPRESSION)
    session.commit()

    report = migrate.fix_kinds(session)
    assert (report.retagged, report.merged) == (0, 0)


# --------------------------------------------------------------------------
# 병합
# --------------------------------------------------------------------------


def test_collision_keeps_the_side_with_review_history():
    """카드를 지우면 review_log 가 CASCADE 로 따라 지워진다. 되살릴 수 없는 데이터다."""
    session = make_session()
    add(session, "missing in action", KIND_EXPRESSION, sentences=["A."])
    add(session, "missing in action", KIND_WORD, sentences=["B.", "C."], reviews=3)
    session.commit()

    report = migrate.fix_kinds(session)
    assert report.merged == 1

    words = session.scalars(select(Word)).all()
    assert len(words) == 1
    survivor = words[0]
    assert survivor.kind == KIND_EXPRESSION, "종류는 형태를 따라야 한다"
    assert survivor.card is not None, "학습 기록이 붙은 쪽이 남아야 한다"
    assert len(session.scalars(select(ReviewLog)).all()) == 3


def test_merge_moves_contexts_and_drops_duplicates():
    session = make_session()
    add(session, "tax avoidance", KIND_EXPRESSION, sentences=["Shared.", "Only in A."])
    add(session, "tax avoidance", KIND_WORD, sentences=["Shared.", "Only in B."], reviews=1)
    session.commit()

    report = migrate.fix_kinds(session)
    assert report.moved_occurrences == 1, "겹치지 않는 예문만 옮긴다"
    assert report.dropped_duplicates == 1

    survivor = session.scalar(select(Word))
    sentences = sorted(o.sentence for o in survivor.occurrences)
    assert sentences == ["Only in A.", "Only in B.", "Shared."]
    assert len(session.scalars(select(Occurrence)).all()) == 3


def test_merge_without_cards_keeps_the_richer_side():
    session = make_session()
    add(session, "buy up", KIND_EXPRESSION, sentences=["one"])
    add(session, "buy up", KIND_WORD, sentences=["two", "three", "four"])
    session.commit()

    migrate.fix_kinds(session)
    survivor = session.scalar(select(Word))
    assert len(survivor.occurrences) == 4
    assert survivor.kind == KIND_EXPRESSION


def test_merge_takes_the_earliest_first_seen():
    session = make_session()
    add(session, "buy up", KIND_EXPRESSION, first_seen=dt.date(2026, 7, 1))
    add(session, "buy up", KIND_WORD, first_seen=dt.date(2026, 4, 1), sentences=["a", "b"])
    session.commit()

    migrate.fix_kinds(session)
    assert session.scalar(select(Word)).first_seen == dt.date(2026, 4, 1)


def test_merge_fills_empty_fields_without_overwriting():
    session = make_session()
    keeper = add(session, "buy up", KIND_WORD, sentences=["a", "b"], reviews=1)
    keeper.usage_note = "남아야 하는 설명"
    other = add(session, "buy up", KIND_EXPRESSION)
    other.usage_note = "덮어쓰면 안 되는 설명"
    other.mnemonic = "비어 있던 칸은 채운다"
    session.commit()

    migrate.fix_kinds(session)
    survivor = session.scalar(select(Word))
    assert survivor.usage_note == "남아야 하는 설명"
    assert survivor.mnemonic == "비어 있던 칸은 채운다"


def test_migration_is_idempotent():
    session = make_session()
    add(session, "backfire", KIND_EXPRESSION)
    add(session, "buy up", KIND_EXPRESSION, sentences=["one"])
    add(session, "buy up", KIND_WORD, sentences=["two"])
    session.commit()

    migrate.fix_kinds(session)
    second = migrate.fix_kinds(session)
    assert (second.retagged, second.merged) == (0, 0), "두 번 돌려도 더 바뀌면 안 된다"


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
