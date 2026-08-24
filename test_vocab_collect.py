"""vocab 수집기·밴드 판정 테스트"""

import datetime as dt
import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(__file__))

from sqlalchemy import select

from unittest.mock import patch

import config
import requests

from vocab import banding, candidates, collect
from vocab.db import create_all, make_engine, session_factory
from vocab.models import KIND_EXPRESSION, KIND_WORD, Card, Occurrence, ReviewLog, Word


def _memory_session():
    engine = make_engine("sqlite://")
    create_all(engine)
    return session_factory(engine)()


# --------------------------------------------------------------------------
# 밴드 판정
# --------------------------------------------------------------------------


def test_common_words_are_demoted():
    """성인 학습자가 이미 알 단어는 known 밴드로 빠진다."""
    for word in ("poverty", "economic", "impact", "exports"):
        assert banding.band(word) == banding.BAND_KNOWN, word


def test_useful_advanced_words_stay_core():
    for word in ("jurisdiction", "sedentary", "capitulate", "monetary"):
        assert banding.band(word) == banding.BAND_CORE, word


def test_phrases_never_banded_by_frequency():
    """다어절·하이픈 표현은 wordfreq 값이 무의미하므로 항상 core."""
    for phrase in ("wait-and-see", "break the ice", "sitting is the new smoking"):
        assert banding.zipf(phrase) is None, phrase
        assert banding.band(phrase) == banding.BAND_CORE, phrase


def test_lemma_and_surface_both_considered():
    """굴절형과 원형 중 더 흔한 쪽을 쓴다. 한쪽만 보면 판정이 흔들린다."""
    from wordfreq import zipf_frequency

    assert banding.lemma("exports") == "export"
    expected = max(zipf_frequency("exports", "en"), zipf_frequency("export", "en"))
    assert banding.zipf("exports") == expected


def test_normalize_strips_punctuation_and_case():
    assert banding.normalize('  "Sedentary."  ') == "sedentary"
    assert banding.normalize("Break  the\tice") == "break the ice"


# --------------------------------------------------------------------------
# 후보 선정
# --------------------------------------------------------------------------

TRANSCRIPT = """
    Support for this podcast comes from our sponsor. Subscribe to our newsletter.
    The ceasefire is showing cracks. Iran and Pakistan traded accusations, and the
    monetary policy response has been restrictive. Inflation expectations remain
    elevated. Pakistan says the ceasefire holds. This episode was produced by Rennie.
"""


def test_boilerplate_is_excluded():
    """광고 낭독·제작진 크레딧에서 오는 말은 에피소드 내용과 무관하다."""
    picked = candidates.from_transcript(TRANSCRIPT, limit=30)
    for junk in ("podcast", "sponsor", "newsletter", "subscribe", "produce"):
        assert junk not in picked, junk


def test_proper_nouns_are_excluded_even_when_inflected():
    """후보 키가 원형이므로 고유명사도 원형으로 걸러야 한다.

    'Emirates' 를 고유명사로 잡아 놓고 원형 'emirate' 가 후보로 올라오면 걸러낸
    의미가 없다. LLM 에게 지명 조각을 학습 단어로 넘기게 된다.
    """
    text = ("The United Arab Emirates signed. Emirates officials met. "
            "Emirates again. Emirates. Emirates. Emirates.")
    assert candidates.from_transcript(text, limit=10) == []


def test_proper_nouns_are_excluded():
    """인명·지명·기관명은 어휘 학습 대상이 아니다."""
    picked = candidates.from_transcript(TRANSCRIPT, limit=30)
    assert "iran" not in picked and "pakistan" not in picked and "rennie" not in picked


def test_only_core_band_words_become_candidates():
    picked = candidates.from_transcript(TRANSCRIPT, limit=30)
    assert picked, "후보가 하나도 안 나오면 안 된다"
    for word in picked:
        assert banding.band(word) == banding.BAND_CORE, f"{word} -> {banding.band(word)}"


def test_exclude_set_is_honoured():
    picked = candidates.from_transcript(TRANSCRIPT, limit=30)
    assert "monetary" in picked
    trimmed = candidates.from_transcript(TRANSCRIPT, limit=30, exclude={"Monetary"})
    assert "monetary" not in trimmed, "대소문자 무관하게 빠져야 한다"


def test_repetition_breaks_ties_between_similarly_common_words():
    """반복은 순위를 뒤집는 힘이 아니라 비슷한 것들 사이의 타이브레이크다.

    활용도(zipf)가 먼저다 — 고빈도 단어에서 학습 격차가 가장 크게 벌어진다는 결과에
    맞춘 것이다. interim 과 portal 은 zipf 가 같으므로 반복 횟수가 순서를 정한다.
    """
    text = "The portal opened. " * 4 + "An interim report followed."
    picked = candidates.from_transcript(text, limit=10)
    assert picked.index("portal") < picked.index("interim")


def test_utility_outranks_repetition():
    """훨씬 흔한 단어는 반복 몇 번으로 밀리지 않는다."""
    text = "The stalemate continues. " * 5 + "There was one mention of jurisdiction."
    picked = candidates.from_transcript(text, limit=10)
    assert picked.index("jurisdiction") < picked.index("stalemate")


def test_empty_transcript_yields_nothing():
    assert candidates.from_transcript("") == []


def test_exclusion_matches_inflected_headwords():
    """저장소는 표면형('accusations')을, 후보는 원형('accusation')을 쓴다.

    맞춰 주지 않으면 이미 외우는 중인 단어가 매번 새 후보로 다시 올라온다.
    실제 저장소의 단일어 표제어 268개 중 60개가 원형과 달랐다.
    """
    text = "The accusations continue. Both sides traded accusations again. " * 4
    assert "accusation" in candidates.from_transcript(text, limit=10)
    assert candidates.from_transcript(text, limit=10, exclude={"accusations"}) == []


def test_exclusion_matches_regardless_of_case_and_spacing():
    text = "The accusations continue. Both sides traded accusations again. " * 4
    assert candidates.from_transcript(text, limit=10, exclude={"  Accusations. "}) == []


def test_server_failure_is_logged_not_swallowed():
    """조용히 물러나면 토큰이 어긋난 채 몇 주가 지나도 알 수 없다."""
    import logging

    class Bad:
        ok = False
        status_code = 401
        text = "토큰 불일치"

    records = []
    handler = logging.Handler()
    handler.emit = records.append
    logger = candidates.logger
    logger.addHandler(handler)
    try:
        with configured(), patch.object(requests, "get", return_value=Bad()):
            assert candidates._handled_from_server() is None
    finally:
        logger.removeHandler(handler)

    assert any("401" in r.getMessage() for r in records), "상태 코드가 로그에 남아야 한다"
    assert any("VOCAB_INGEST_TOKEN" in r.getMessage() for r in records), "무엇을 고칠지 알려야 한다"


def test_network_error_falls_back_quietly_but_logs():
    records = []
    import logging
    handler = logging.Handler(); handler.emit = records.append
    candidates.logger.addHandler(handler)
    try:
        with configured(), patch.object(requests, "get", side_effect=OSError("연결 실패")):
            assert candidates._handled_from_server() is None
    finally:
        candidates.logger.removeHandler(handler)
    assert records, "네트워크 실패도 로그로 남아야 한다"


def test_already_handled_prefers_the_server():
    """학습 상태는 서버가 원본이다. 로컬 Card 는 배포 후 비어 있다."""
    with patch.object(candidates, "_handled_from_server", return_value={"poverty"}), \
         patch.object(candidates, "_handled_from_local", return_value={"local-only"}):
        assert candidates.already_handled() == {"poverty"}


def test_already_handled_falls_back_when_the_server_is_down():
    """서버가 잠깐 안 된다고 아침 파이프라인이 멈추면 안 된다."""
    with patch.object(candidates, "_handled_from_server", return_value=None), \
         patch.object(candidates, "_handled_from_local", return_value={"local"}):
        assert candidates.already_handled() == {"local"}


def test_server_lookup_is_skipped_without_configuration():
    with patch.multiple(config, VOCAB_SERVER_URL="", VOCAB_INGEST_TOKEN=""):
        assert candidates._handled_from_server() is None


def configured():
    return patch.multiple(
        config,
        VOCAB_SERVER_URL="https://vocab.example.com",
        VOCAB_INGEST_TOKEN="secret",
    )


# --------------------------------------------------------------------------
# 파서
# --------------------------------------------------------------------------


def test_parse_daily_reads_vocabulary_and_expressions():
    payload = {
        "date": "2026-06-01",
        "source": "Up First",
        "title": "Test Episode",
        "episode_url": "https://example.com/ep",
        "analysis": {
            "vocabulary": [
                {
                    "word": "sedentary",
                    "definition_kr": "앉아서 거의 움직이지 않는",
                    "definition_en": "characterized by much sitting",
                    "example": "the harms of our modern sedentary lifestyle",
                }
            ],
            "expressions": [
                {
                    "expression": "sitting is the new smoking",
                    "meaning_kr": "앉아 있는 것이 흡연만큼 해롭다",
                    "usage_note": "'X is the new Y' 구조",
                    "example": "Scientists say that sitting is the new smoking.",
                }
            ],
            "key_sentences": [{"sentence": "무시되어야 한다", "translation_kr": "..."}],
        },
    }
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "2026-06-01.json"
        path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        entries = collect.parse_daily(path)

    assert len(entries) == 2, f"key_sentences는 어휘가 아니므로 제외: {len(entries)}"
    word, expression = entries
    assert word.kind == KIND_WORD and word.headword == "sedentary"
    assert word.sentence == "the harms of our modern sedentary lifestyle"
    assert word.occurred_on == dt.date(2026, 6, 1)
    assert expression.kind == KIND_EXPRESSION
    assert expression.source_kind == "upfirst"


def test_parse_daily_skips_failed_analysis():
    """분석이 실패해 raw 폴백만 있는 파일은 건너뛴다."""
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "2026-06-02.json"
        path.write_text(
            json.dumps({"date": "2026-06-02", "source": "Up First", "title": "x",
                        "analysis": {"raw": "파싱 실패한 응답"}}),
            encoding="utf-8",
        )
        assert collect.parse_daily(path) == []


def test_parse_weekly_maps_day_to_study_date():
    payload = {
        "start_date": "2026-06-01",
        "study_dates": ["2026-06-01", "2026-06-02", "2026-06-03", "2026-06-04", "2026-06-05"],
        "episode": {
            "source": "Planet Money",
            "title": "Korea Summer School",
            "episode_url": "https://example.com/pm",
        },
        "weekly_analysis": {
            "lessons": [
                {"day": 1, "vocabulary": [], "expressions": []},
                {
                    "day": 3,
                    "vocabulary": [
                        {"word": "malinvestment", "definition_kr": "잘못된 투자", "example": "q"}
                    ],
                    "expressions": [],
                },
            ]
        },
    }
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "planetmoney-2026-06-01.json"
        path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        entries = collect.parse_weekly(path)

    assert len(entries) == 1
    assert entries[0].occurred_on == dt.date(2026, 6, 3), "day 3 → study_dates[2]"
    assert entries[0].source_kind == "planetmoney"


CLASS_NOTE = """# 영어수업 2026-08-20

- 튜터: Sarah
- 주제: 재택근무

## 수업 요약

오늘은 재택근무에 대해 이야기했다.

## 내 표현 교정

| 내가 한 말 | 자연스러운 표현 | 왜 |
| --- | --- | --- |
| I very like remote work | I really like remote work | very는 동사를 못 꾸민다 |

## 새 단어·표현

| 표현 | 뜻 | 예문 |
| --- | --- | --- |
| commute | 통근하다 | My commute takes an hour. |
|  |  |  |

## 복습 과제

- [ ] 연습
"""


def test_heading_match_is_not_confused_by_a_combined_title():
    """`a or b and c` 는 `a or (b and c)` 로 묶인다 — '교정' 가드가 첫 항에 안 걸렸다.

    '## 새 단어·표현 교정' 같은 제목이 오면 교정 행이 새 단어 파서로 들어가
    display 에 '내가 한 말'(틀린 표현)이, meaning_kr 에 자연스러운 표현이 들어간다.
    정확히 거꾸로다.
    """
    note = CLASS_NOTE.replace("## 내 표현 교정", "## 새 단어·표현 교정")
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "영어수업 2026-08-20.md"
        path.write_text(note, encoding="utf-8")
        entries = collect.parse_class_note(path)

    corrections = [e for e in entries if e.source_kind == "correction"]
    assert corrections, "제목이 합쳐져도 교정으로 읽혀야 한다"
    assert corrections[0].headword == "i really like remote work"


def test_rebuild_never_deletes_review_history():
    """`--rebuild` 가 word 를 통째로 지우면 CASCADE 로 card -> review_log 까지 내려간다.

    review_log 는 어디에도 사본이 없고 소급해서 만들 수도 없는 데이터다.
    """
    session = _memory_session()
    stats = collect.Stats()
    collect.upsert(session, _entry("carded", "A sentence."), stats)
    collect.upsert(session, _entry("plain", "Another sentence."), stats)
    session.flush()

    word = session.scalar(select(Word).where(Word.headword == "carded"))
    card = Card(word_id=word.id, due=dt.datetime.now(dt.UTC))
    session.add(card)
    session.flush()
    session.add(
        ReviewLog(card_id=card.id, rating=3, state=2, reviewed_at=dt.datetime.now(dt.UTC),
                  correct=True, stage="recognition", elapsed_days=0,
                  last_elapsed_days=0, scheduled_days=0)
    )
    session.commit()

    collect._rebuild(session, collect.Stats())

    assert len(session.scalars(select(ReviewLog)).all()) == 1, "복습 기록이 지워졌다"
    assert len(session.scalars(select(Card)).all()) == 1
    remaining = [w.headword for w in session.scalars(select(Word)).all()]
    assert remaining == ["carded"], f"카드 없는 어휘만 지워야 한다: {remaining}"


def test_rebuild_reports_what_it_kept():
    session = _memory_session()
    stats = collect.Stats()
    collect.upsert(session, _entry("carded", "A sentence."), stats)
    session.flush()
    word = session.scalar(select(Word))
    session.add(Card(word_id=word.id, due=dt.datetime.now(dt.UTC)))
    session.commit()

    report = collect.Stats()
    collect._rebuild(session, report)
    assert any("복습 기록" in line for line in report.skipped), report.skipped


def test_parse_class_note_reads_both_tables():
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "영어수업 2026-08-20.md"
        path.write_text(CLASS_NOTE, encoding="utf-8")
        entries = collect.parse_class_note(path)

    by_source = {e.source_kind: e for e in entries}
    assert set(by_source) == {"correction", "class"}, f"{[e.source_kind for e in entries]}"

    correction = by_source["correction"]
    assert correction.headword == "i really like remote work"
    assert "very는" in correction.meaning_kr
    assert "I very like remote work" in correction.usage_note

    vocab = by_source["class"]
    assert vocab.headword == "commute"
    assert vocab.sentence == "My commute takes an hour."
    assert "Sarah" in vocab.source_title and "재택근무" in vocab.source_title


def test_parse_class_note_ignores_empty_template_rows():
    """템플릿이 남긴 빈 행이 어휘로 들어오면 안 된다."""
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "영어수업 2026-08-20.md"
        path.write_text(CLASS_NOTE, encoding="utf-8")
        entries = collect.parse_class_note(path)
    assert all(e.display.strip() for e in entries)
    assert len(entries) == 2


# --------------------------------------------------------------------------
# 저장 (멱등성·병합)
# --------------------------------------------------------------------------


def _entry(display, sentence, *, day="2026-06-01", kind=KIND_WORD, source="upfirst", **kw):
    return collect.Entry(
        display=display,
        kind=kind,
        meaning_kr=kw.pop("meaning_kr", "뜻"),
        source_kind=source,
        source_title=kw.pop("source_title", "제목"),
        occurred_on=dt.date.fromisoformat(day),
        sentence=sentence,
        **kw,
    )


def test_same_word_from_two_sources_merges_into_one_row():
    """같은 단어가 여러 에피소드에 나오면 한 줄에 예문이 쌓인다 — 문맥 순환의 전제."""
    session = _memory_session()
    stats = collect.Stats()
    collect.upsert(session, _entry("Sedentary", "First context sentence."), stats)
    collect.upsert(
        session,
        _entry("sedentary", "A different context entirely.", day="2026-07-01", source="planetmoney"),
        stats,
    )
    session.commit()

    words = session.scalars(select(Word)).all()
    assert len(words) == 1, "표제어 정규화 후 같은 단어는 한 줄"
    assert len(words[0].occurrences) == 2
    assert stats.words_created == 1 and stats.occurrences_created == 2


def test_duplicate_sentence_is_not_stored_twice():
    session = _memory_session()
    stats = collect.Stats()
    collect.upsert(session, _entry("offset", "The SAME   sentence."), stats)
    collect.upsert(session, _entry("offset", "the same sentence."), stats)
    session.commit()

    assert session.scalar(select(Occurrence).where(Occurrence.word_id == 1)) is not None
    assert len(session.scalars(select(Occurrence)).all()) == 1, "공백·대소문자 차이는 같은 문장"
    assert stats.occurrences_created == 1


def test_first_seen_moves_earlier_when_older_file_arrives_later():
    """파일을 순서 없이 읽어도 최초 등장일이 맞는다."""
    session = _memory_session()
    stats = collect.Stats()
    collect.upsert(session, _entry("deficit", "later.", day="2026-07-01"), stats)
    collect.upsert(session, _entry("deficit", "earlier.", day="2026-04-01"), stats)
    session.commit()

    word = session.scalar(select(Word))
    assert word.first_seen == dt.date(2026, 4, 1)


def test_existing_meaning_is_not_overwritten():
    """먼저 들어온 뜻을 나중 소스가 덮어쓰지 않는다. 빈 칸만 채운다."""
    session = _memory_session()
    stats = collect.Stats()
    collect.upsert(session, _entry("interim", "a.", meaning_kr="첫 번째 뜻"), stats)
    collect.upsert(
        session, _entry("interim", "b.", meaning_kr="두 번째 뜻", meaning_en="temporary"), stats
    )
    session.commit()

    word = session.scalar(select(Word))
    assert word.meaning_kr == "첫 번째 뜻"
    assert word.meaning_en == "temporary", "비어 있던 칸은 채워져야 한다"


def test_band_is_assigned_on_insert():
    session = _memory_session()
    stats = collect.Stats()
    collect.upsert(session, _entry("poverty", "s."), stats)
    collect.upsert(session, _entry("break the ice", "s.", kind=KIND_EXPRESSION), stats)
    session.commit()

    bands = {w.headword: (w.band, w.zipf) for w in session.scalars(select(Word)).all()}
    assert bands["poverty"][0] == banding.BAND_KNOWN
    assert bands["break the ice"] == (banding.BAND_CORE, None)


def test_entry_without_sentence_still_creates_word():
    """예문이 없어도 어휘는 만들어진다. 4지선다는 예문 없이도 낼 수 있다."""
    session = _memory_session()
    stats = collect.Stats()
    collect.upsert(session, _entry("lobbying", None), stats)
    session.commit()

    word = session.scalar(select(Word))
    assert word is not None and word.occurrences == []


if __name__ == "__main__":
    tests = [
        test_common_words_are_demoted,
        test_useful_advanced_words_stay_core,
        test_phrases_never_banded_by_frequency,
        test_lemma_and_surface_both_considered,
        test_normalize_strips_punctuation_and_case,
        test_boilerplate_is_excluded,
        test_proper_nouns_are_excluded,
        test_only_core_band_words_become_candidates,
        test_exclude_set_is_honoured,
        test_repetition_breaks_ties_between_similarly_common_words,
        test_utility_outranks_repetition,
        test_empty_transcript_yields_nothing,
        test_exclusion_matches_inflected_headwords,
        test_exclusion_matches_regardless_of_case_and_spacing,
        test_server_failure_is_logged_not_swallowed,
        test_network_error_falls_back_quietly_but_logs,
        test_already_handled_prefers_the_server,
        test_already_handled_falls_back_when_the_server_is_down,
        test_server_lookup_is_skipped_without_configuration,
        test_parse_daily_reads_vocabulary_and_expressions,
        test_parse_daily_skips_failed_analysis,
        test_parse_weekly_maps_day_to_study_date,
        test_proper_nouns_are_excluded_even_when_inflected,
        test_heading_match_is_not_confused_by_a_combined_title,
        test_rebuild_never_deletes_review_history,
        test_rebuild_reports_what_it_kept,
        test_parse_class_note_reads_both_tables,
        test_parse_class_note_ignores_empty_template_rows,
        test_same_word_from_two_sources_merges_into_one_row,
        test_duplicate_sentence_is_not_stored_twice,
        test_first_seen_moves_earlier_when_older_file_arrives_later,
        test_existing_meaning_is_not_overwritten,
        test_band_is_assigned_on_insert,
        test_entry_without_sentence_still_creates_word,
    ]
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
