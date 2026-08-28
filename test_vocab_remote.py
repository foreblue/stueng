"""수업 PC 추출기 + 뜻 대기 왕복 테스트

두 가지를 본다.

1. `vocab.remote` — 전사문에서 후보를 뽑아 뜻 없이 페이로드를 만든다 (수업 PC 에서 돌 것).
2. 서버 — 뜻 없는 어휘를 받아 두되 카드로 내보내지 않고, `/api/tasks` 로 내주고 채운다.
"""

import datetime as dt
import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(__file__))

# 앱이 import 시점에 엔진을 만들므로 그 전에 설정해야 한다.
_TMP = tempfile.mkdtemp(prefix="vocab-remote-test-")
os.environ["VOCAB_DB_URL"] = f"sqlite:///{Path(_TMP) / 'test.db'}"
os.environ["VOCAB_PASSWORD"] = "hunter2"
os.environ["VOCAB_SECRET_KEY"] = "test-secret-key"
os.environ["VOCAB_INGEST_TOKEN"] = "ingest-secret"
os.environ["VOCAB_REMOTE_TOKEN"] = "remote-secret"
os.environ.pop("VOCAB_ENV", None)

from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import delete, select  # noqa: E402

from vocab import compose, remote, study  # noqa: E402
from vocab.app import main, security  # noqa: E402
from vocab.models import (  # noqa: E402
    KIND_EXPRESSION,
    KIND_WORD,
    PENDING_GLOSS,
    SOURCE_CLASS,
    SOURCE_CORRECTION,
    Card,
    Composition,
    Occurrence,
    ReviewLog,
    Word,
)

HEADERS = {"X-Ingest-Token": "ingest-secret"}
REMOTE_HEADERS = {"X-Ingest-Token": "remote-secret"}
TODAY = dt.date(2026, 8, 28)

TRANSCRIPT = """# 수업 전사

**Tutor** `00:00:12` The company decided to capitulate once the sanctions began to bite.

**Me** `00:00:31` So they were capitulating because of the monetary pressure, right?

**Tutor** `00:01:04` Exactly. The monetary side is what forced it. Their jurisdiction had
no say in the matter at all.

**Me** `00:01:40` I see, that makes the jurisdiction question much clearer to me now.
"""


def fresh_client():
    main.create_all(main.engine)
    with main.Session_() as session:
        for model in (ReviewLog, Composition, Card, Occurrence, Word):
            session.execute(delete(model))
        session.commit()
    return TestClient(main.app)


def _remote_env(**overrides):
    env = {"VOCAB_SERVER_URL": "https://stueng.deepheart.duckdns.org",
           "VOCAB_REMOTE_TOKEN": "remote-secret", "VOCAB_INGEST_TOKEN": ""}
    env.update(overrides)
    return patch.dict(os.environ, env, clear=False)


def _entry(display, meaning="", sentence=None):
    return {
        "display": display,
        "kind": KIND_WORD,
        "meaning_kr": meaning,
        "source_kind": SOURCE_CLASS,
        "source_title": "영어수업 2026-08-28",
        "occurred_on": TODAY.isoformat(),
        "sentence": sentence,
    }


# --------------------------------------------------------------------------
# 원격 추출기
# --------------------------------------------------------------------------


def test_constants_match_the_models():
    """`remote` 는 sqlalchemy 를 피하려고 문자열을 따로 들고 있다. 어긋나면 여기서 잡는다."""
    assert remote.KIND_WORD == KIND_WORD
    assert remote.KIND_EXPRESSION == KIND_EXPRESSION
    assert remote.SOURCE_CLASS == SOURCE_CLASS


def test_speaker_labels_and_timestamps_are_stripped():
    """예문에 `**Tutor** `00:00:12`` 가 남으면 카드가 읽히지 않는다."""
    lines = remote.clean_lines(TRANSCRIPT)
    assert lines, "본문이 전부 걸러졌다"
    for line in lines:
        assert "Tutor" not in line and "**" not in line, line
        assert "00:" not in line, line


def test_examples_match_inflected_forms():
    """후보는 원형으로 올라온다. 표면형만 찾으면 예문을 못 붙인다."""
    pool = remote.sentences(TRANSCRIPT)
    found = remote.examples_for("capitulate", pool)
    assert found, "capitulate 의 예문을 못 찾았다"
    # 전사문에는 capitulate 와 capitulating 이 각각 한 번씩 나온다.
    assert any("capitulating" in s for s in found), found


def test_build_sends_words_without_meanings():
    with patch.object(remote, "handled", return_value=set()):
        entries = remote.build(TRANSCRIPT, occurred_on=TODAY, tutor="Anna", limit=5)

    assert entries, "후보가 하나도 안 나왔다"
    for entry in entries:
        assert entry["meaning_kr"] == PENDING_GLOSS, entry
        assert entry["source_kind"] == SOURCE_CLASS
        assert entry["source_title"] == "영어수업 2026-08-28 · Anna"
        assert entry["occurred_on"] == "2026-08-28"

    displays = {e["display"] for e in entries}
    assert "capitulate" in displays, displays


def test_build_respects_what_the_server_already_handles():
    """이미 외우는 중인 단어를 원격이 다시 올리면 안 된다."""
    with patch.object(remote, "handled", return_value={"capitulate"}):
        entries = remote.build(TRANSCRIPT, occurred_on=TODAY, limit=5)
    assert "capitulate" not in {e["display"] for e in entries}


def test_build_keeps_the_word_even_with_no_usable_sentence():
    """예문이 없다고 어휘를 버리지 않는다. 뜻은 나중에 붙는다."""
    with patch.object(remote, "handled", return_value=set()), \
         patch.object(remote, "sentences", return_value=[]):
        entries = remote.build(TRANSCRIPT, occurred_on=TODAY, limit=3)
    assert entries
    assert all(e["sentence"] is None for e in entries)


# --------------------------------------------------------------------------
# 서버 — 뜻 대기
# --------------------------------------------------------------------------


def test_ingest_accepts_an_entry_with_no_meaning():
    client = fresh_client()
    response = client.post(
        "/ingest", json={"entries": [_entry("capitulate", sentence="They capitulated.")]},
        headers=HEADERS,
    )
    assert response.status_code == 200, response.text
    assert response.json()["words_created"] == 1

    with main.Session_() as session:
        word = session.scalar(select(Word).where(Word.headword == "capitulate"))
        assert word.meaning_kr == PENDING_GLOSS


def test_a_word_without_a_meaning_never_becomes_a_card():
    """뜻이 비어 있으면 출제되지 않는다. 빈 칸짜리 문제가 나가는 것이 최악이다."""
    client = fresh_client()
    client.post("/ingest", json={"entries": [_entry("capitulate")]}, headers=HEADERS)

    with main.Session_() as session:
        assert study.introduce(session, 10) == []


def test_the_word_becomes_a_card_once_the_meaning_lands():
    client = fresh_client()
    client.post("/ingest", json={"entries": [_entry("capitulate")]}, headers=HEADERS)

    tasks = client.get("/api/tasks", headers=HEADERS).json()
    pending = [t for t in tasks["glosses"] if t["display"] == "capitulate"]
    assert pending, tasks["glosses"]

    applied = client.post(
        "/api/tasks",
        json={"glosses": [{"word_id": pending[0]["word_id"], "meaning_kr": "항복하다"}]},
        headers=HEADERS,
    )
    assert applied.json()["glosses"] == 1

    with main.Session_() as session:
        cards = study.introduce(session, 10)
        assert len(cards) == 1
        session.commit()
        word = session.scalar(select(Word).where(Word.headword == "capitulate"))
        assert word.meaning_kr == "항복하다"


def test_a_real_meaning_is_never_overwritten_by_the_worker():
    """수업 노트가 먼저 제대로 채웠다면 생성된 뜻이 덮지 않는다."""
    client = fresh_client()
    client.post("/ingest", json={"entries": [_entry("capitulate", meaning="굴복하다")]},
                headers=HEADERS)

    with main.Session_() as session:
        word_id = session.scalar(select(Word.id).where(Word.headword == "capitulate"))
        assert compose.words_without_gloss(session) == []

    client.post("/api/tasks",
                json={"glosses": [{"word_id": word_id, "meaning_kr": "항복하다"}]},
                headers=HEADERS)
    with main.Session_() as session:
        assert session.get(Word, word_id).meaning_kr == "굴복하다"


def test_a_later_note_fills_the_meaning_the_remote_left_empty():
    """같은 표제어가 수업 노트로 다시 들어오면 그때 뜻이 붙는다."""
    client = fresh_client()
    client.post("/ingest", json={"entries": [_entry("capitulate")]}, headers=HEADERS)
    client.post("/ingest", json={"entries": [_entry("capitulate", meaning="굴복하다")]},
                headers=HEADERS)

    with main.Session_() as session:
        word = session.scalar(select(Word).where(Word.headword == "capitulate"))
        assert word.meaning_kr == "굴복하다"
        assert compose.words_without_gloss(session) == []


def test_gloss_queue_is_ordered_and_carries_examples():
    client = fresh_client()
    client.post(
        "/ingest",
        json={"entries": [
            _entry("capitulate", sentence="They capitulated under pressure."),
            _entry("jurisdiction"),
        ]},
        headers=HEADERS,
    )
    glosses = client.get("/api/tasks", headers=HEADERS).json()["glosses"]
    assert {g["display"] for g in glosses} == {"capitulate", "jurisdiction"}
    by_display = {g["display"]: g for g in glosses}
    assert by_display["capitulate"]["examples"] == ["They capitulated under pressure."]
    assert by_display["jurisdiction"]["examples"] == []


def test_handled_failure_is_loud_not_silent():
    """조용히 빈 집합으로 물러나면 이미 외우는 단어가 후보 자리를 차지한다."""
    class Refused:
        ok = False
        status_code = 401

    with _remote_env(), patch.object(remote.requests, "get", return_value=Refused()), \
         patch.object(remote.logger, "warning") as warn:
        assert remote.handled() == set()
    assert warn.called, "거부당하고도 아무 말이 없으면 안 된다"


def test_handled_uses_the_narrow_token():
    """넓은 토큰을 쓰면 이 PC 에 둘 이유가 없는 권한을 쓰는 것이다."""
    class Ok:
        ok = True
        @staticmethod
        def json():
            return {"headwords": ["Capitulate"]}

    with _remote_env(), patch.object(remote.requests, "get", return_value=Ok()) as get:
        assert remote.handled() == {"capitulate"}
    assert get.call_args.kwargs["headers"]["X-Ingest-Token"] == "remote-secret"
    assert get.call_args.args[0].endswith("/api/handled")


def test_the_narrow_token_can_read_handled_words():
    """서버가 이 경로를 열어 주지 않으면 위 조회가 매번 401 로 물러난다."""
    client = fresh_client()
    assert client.get("/api/handled", headers=REMOTE_HEADERS).status_code == 200


# --------------------------------------------------------------------------
# 수업 노트 경로 — 뜻이 이미 있는 쪽
# --------------------------------------------------------------------------

NOTE = """# 영어수업 2026-08-28

- 튜터: Anna
- 주제: job interview

## 내 표현 교정

| 내가 한 말 | 자연스러운 표현 | 왜 |
| --- | --- | --- |
| I am working there **since 2020** | I **have been working** there since 2020 | since 는 현재완료와 쓴다 |

## 새 단어·표현

| 표현 | 뜻 | 예문 |
| --- | --- | --- |
| **capitulate** | 굴복하다 | They `capitulated` after negotiations. |
| take on | (일을) 맡다 | I'd love to take on more responsibility. |
| | | |
"""


def _note_file():
    path = Path(_TMP) / "영어수업 2026-08-28.md"
    path.write_text(NOTE, encoding="utf-8")
    return path


def test_note_entries_arrive_with_meanings():
    """노트 경로는 뜻 대기열을 거치지 않는다. 그 PC 의 Claude 가 이미 썼다."""
    entries = remote.build_from_note(str(_note_file()))
    assert entries
    for entry in entries:
        assert entry["meaning_kr"], entry
        assert entry["source_title"] == "영어수업 2026-08-28 · Anna · job interview"
        assert entry["occurred_on"] == "2026-08-28"


def test_note_keeps_corrections_apart_from_new_words():
    """교정은 출처가 달라야 한다. 출제 순서에서 가장 먼저 오는 재료다."""
    by_display = {e["display"]: e for e in remote.build_from_note(str(_note_file()))}
    correction = by_display["I have been working there since 2020"]
    assert correction["source_kind"] == SOURCE_CORRECTION
    assert correction["usage_note"] == "내가 한 말: I am working there since 2020"
    assert by_display["capitulate"]["source_kind"] == SOURCE_CLASS


def test_markdown_emphasis_never_reaches_the_card():
    """노트는 고친 부분을 **강조**해 둔다. 그게 어휘로 넘어오면 카드에 별표가 뜨고
    headword 에도 섞여 같은 표현을 다른 어휘로 세게 된다."""
    entries = remote.build_from_note(str(_note_file()))
    for entry in entries:
        for field in ("display", "meaning_kr", "sentence", "usage_note"):
            value = entry.get(field) or ""
            assert "*" not in value and "`" not in value, (field, value)

    by_display = {e["display"]: e for e in entries}
    assert "I have been working there since 2020" in by_display
    assert by_display["capitulate"]["sentence"] == "They capitulated after negotiations."


def test_other_markdown_constructs_are_unwrapped_too():
    """노트는 마크다운 문서다. 강조만 막으면 다음 구문에서 같은 버그가 난다."""
    from vocab import notes as n

    assert n.clean("[[동물 표현]] 참고") == "동물 표현 참고"
    assert n.clean("[[영어수업 2026-08-20|지난 수업]] 표현") == "지난 수업 표현"
    assert n.clean("자세히는 [여기](https://example.com) 참고") == "자세히는 여기 참고"
    # 취소선은 지운다 — 살리면 틀린 표현을 함께 가르치게 된다.
    assert n.clean("~~I am working~~ I have been working") == "I have been working"


def test_an_escaped_pipe_does_not_split_the_cell():
    """표 셀 안의 `\\|` 에서 잘리면 뜻의 뒷부분이 통째로 사라진다."""
    from vocab import notes as n

    rows = n.markdown_tables(
        "## 새 단어·표현\n\n| 표현 | 뜻 |\n| --- | --- |\n| pipe dream | 헛된 꿈 (a \\| b) |\n"
    )["새 단어·표현"]
    assert rows[1] == ["pipe dream", "헛된 꿈 (a | b)"]


def test_note_kind_follows_the_written_form():
    by_display = {e["display"]: e for e in remote.build_from_note(str(_note_file()))}
    assert by_display["capitulate"]["kind"] == KIND_WORD
    assert by_display["take on"]["kind"] == KIND_EXPRESSION


def test_empty_template_rows_are_dropped():
    """스킬 템플릿이 남기는 빈 행이 어휘가 되면 안 된다."""
    displays = {e["display"] for e in remote.build_from_note(str(_note_file()))}
    assert "" not in displays
    assert len(displays) == 3


def test_a_misnamed_note_stops_loudly():
    """날짜를 못 읽으면 조용히 0건을 보내는 대신 멈춘다."""
    path = Path(_TMP) / "수업메모.md"
    path.write_text(NOTE, encoding="utf-8")
    try:
        remote.build_from_note(str(path))
    except remote.RemoteError as e:
        assert "영어수업 YYYY-MM-DD" in str(e)
    else:
        raise AssertionError("이름 규칙이 틀리면 멈춰야 한다")


def test_the_note_lands_on_the_server_ready_to_study():
    """노트로 들어온 어휘는 뜻이 있으므로 바로 카드가 된다."""
    client = fresh_client()
    entries = remote.build_from_note(str(_note_file()))
    assert client.post("/ingest", json={"entries": entries},
                       headers=REMOTE_HEADERS).status_code == 200

    with main.Session_() as session:
        assert compose.words_without_gloss(session) == [], "뜻 대기열에 남으면 안 된다"
        assert len(study.introduce(session, 10)) > 0


def test_the_mac_and_the_pc_read_the_note_the_same_way():
    """규칙이 두 벌이 되면 어느 한쪽만 고치는 날이 온다. 같은 코드임을 고정한다."""
    from vocab import collect as collect_mod

    path = _note_file()
    mac = collect_mod.parse_class_note(path)
    pc = remote.build_from_note(str(path))
    assert [e.display for e in mac] == [e["display"] for e in pc]
    assert [e.meaning_kr for e in mac] == [e["meaning_kr"] for e in pc]
    assert [e.source_kind for e in mac] == [e["source_kind"] for e in pc]


# --------------------------------------------------------------------------
# 토큰 범위 — 수업 PC 가 가진 열쇠로 무엇을 열 수 있나
# --------------------------------------------------------------------------


def test_remote_token_can_push_vocabulary():
    client = fresh_client()
    response = client.post("/ingest", json={"entries": [_entry("capitulate")]},
                           headers=REMOTE_HEADERS)
    assert response.status_code == 200, response.text


def test_remote_token_cannot_read_anything():
    """수업 PC 의 토큰이 새도 복습 기록은 넘어가지 않아야 한다."""
    client = fresh_client()
    # /api/handled 는 뺄 목록이라 의도적으로 열려 있다. 나머지는 닫혀야 한다.
    for path in ("/api/export", "/api/tasks", "/api/progress"):
        response = client.get(path, headers=REMOTE_HEADERS)
        assert response.status_code == 401, f"{path} 가 {response.status_code} 로 열렸다"


def test_remote_token_cannot_write_worker_results():
    """읽기만 막고 쓰기를 열어 두면 기억술·첨삭을 덮어쓸 수 있다."""
    client = fresh_client()
    response = client.post("/api/tasks", json={"glosses": []}, headers=REMOTE_HEADERS)
    assert response.status_code == 401


def test_the_wide_token_still_opens_everything():
    """좁은 토큰을 더한다고 기존 경로(`vocab.sync push`)가 막히면 안 된다."""
    client = fresh_client()
    assert client.post("/ingest", json={"entries": [_entry("capitulate")]},
                       headers=HEADERS).status_code == 200
    assert client.get("/api/export", headers=HEADERS).status_code == 200


def test_an_empty_header_never_authorizes():
    """설정되지 않은 토큰이 후보에 남으면 빈 헤더가 통과한다. 그 자리를 지킨다."""
    client = fresh_client()
    with patch.object(security, "remote_token", return_value=""), \
         patch.object(security, "ingest_token", return_value="ingest-secret"):
        assert client.post("/ingest", json={"entries": []},
                           headers={"X-Ingest-Token": ""}).status_code == 401
        assert client.post("/ingest", json={"entries": []}).status_code == 401


def test_ingest_is_unavailable_when_no_token_is_configured():
    client = fresh_client()
    with patch.object(security, "remote_token", return_value=""), \
         patch.object(security, "ingest_token", return_value=""):
        assert client.post("/ingest", json={"entries": []},
                           headers=HEADERS).status_code == 503


# --------------------------------------------------------------------------
# 전송 경로 — 토큰이 평문으로 나가지 않는가
# --------------------------------------------------------------------------


def test_plain_http_is_refused():
    """토큰을 평문으로 내보내느니 멈춘다."""
    with _remote_env(VOCAB_SERVER_URL="http://192.168.45.93:8010"):
        try:
            remote._server()
        except remote.RemoteError as e:
            assert "HTTPS" in str(e)
        else:
            raise AssertionError("평문 HTTP 로 보내면 안 된다")


def test_a_hostname_that_merely_starts_with_the_loopback_is_refused():
    """접두사 비교였다면 http://127.0.0.1.attacker.example 이 통과해
    토큰이 남의 호스트로 평문 전송된다."""
    for url in ("http://127.0.0.1.attacker.example", "http://127.0.0.1.evil.test/x",
                "http://localhost.attacker.example"):
        with _remote_env(VOCAB_SERVER_URL=url):
            try:
                remote._server()
            except remote.RemoteError:
                pass
            else:
                raise AssertionError(f"평문으로 나가면 안 된다: {url}")


def test_loopback_is_allowed_for_local_testing():
    with _remote_env(VOCAB_SERVER_URL="http://127.0.0.1:8010"):
        url, token = remote._server()
        assert url == "http://127.0.0.1:8010"
        assert token == "remote-secret"


def test_the_narrow_token_is_preferred():
    with _remote_env(VOCAB_INGEST_TOKEN="wide-secret"):
        _, token = remote._server()
        assert token == "remote-secret", "넓은 토큰을 먼저 집으면 안 된다"


def test_the_wide_token_works_but_warns():
    """좁은 토큰이 없다고 멈추지는 않는다. 다만 조용히 넘어가지도 않는다."""
    with _remote_env(VOCAB_REMOTE_TOKEN="", VOCAB_INGEST_TOKEN="wide-secret"), \
         patch.object(remote.logger, "warning") as warn:
        _, token = remote._server()
    assert token == "wide-secret"
    assert warn.called, "넓은 토큰을 쓰면서 아무 말도 안 하면 안 된다"


if __name__ == "__main__":
    tests = [
        test_constants_match_the_models,
        test_speaker_labels_and_timestamps_are_stripped,
        test_examples_match_inflected_forms,
        test_build_sends_words_without_meanings,
        test_build_respects_what_the_server_already_handles,
        test_build_keeps_the_word_even_with_no_usable_sentence,
        test_ingest_accepts_an_entry_with_no_meaning,
        test_a_word_without_a_meaning_never_becomes_a_card,
        test_the_word_becomes_a_card_once_the_meaning_lands,
        test_a_real_meaning_is_never_overwritten_by_the_worker,
        test_a_later_note_fills_the_meaning_the_remote_left_empty,
        test_gloss_queue_is_ordered_and_carries_examples,
        test_handled_failure_is_loud_not_silent,
        test_handled_uses_the_narrow_token,
        test_the_narrow_token_can_read_handled_words,
        test_note_entries_arrive_with_meanings,
        test_note_keeps_corrections_apart_from_new_words,
        test_markdown_emphasis_never_reaches_the_card,
        test_other_markdown_constructs_are_unwrapped_too,
        test_an_escaped_pipe_does_not_split_the_cell,
        test_note_kind_follows_the_written_form,
        test_empty_template_rows_are_dropped,
        test_a_misnamed_note_stops_loudly,
        test_the_note_lands_on_the_server_ready_to_study,
        test_the_mac_and_the_pc_read_the_note_the_same_way,
        test_remote_token_can_push_vocabulary,
        test_remote_token_cannot_read_anything,
        test_remote_token_cannot_write_worker_results,
        test_the_wide_token_still_opens_everything,
        test_an_empty_header_never_authorizes,
        test_ingest_is_unavailable_when_no_token_is_configured,
        test_plain_http_is_refused,
        test_a_hostname_that_merely_starts_with_the_loopback_is_refused,
        test_loopback_is_allowed_for_local_testing,
        test_the_narrow_token_is_preferred,
        test_the_wide_token_works_but_warns,
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
