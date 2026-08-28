"""vocab 웹앱 테스트"""

import datetime as dt
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(__file__))

# 앱이 import 시점에 엔진을 만들므로 그 전에 설정해야 한다.
_TMP = tempfile.mkdtemp(prefix="vocab-app-test-")
os.environ["VOCAB_DB_URL"] = f"sqlite:///{Path(_TMP) / 'test.db'}"
os.environ["VOCAB_PASSWORD"] = "hunter2"
os.environ["VOCAB_SECRET_KEY"] = "test-secret-key"
os.environ["VOCAB_INGEST_TOKEN"] = "ingest-secret"
os.environ.pop("VOCAB_ENV", None)

from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import delete, select  # noqa: E402

from vocab import study  # noqa: E402
from vocab.app import main, security  # noqa: E402
from vocab.models import (  # noqa: E402
    KIND_WORD,
    STAGE_PRODUCTION,
    Card,
    Composition,
    Occurrence,
    ReviewLog,
    Word,
    sentence_key,
)

TODAY = dt.date(2026, 8, 20)


def fresh_client(*, words=1, with_examples=True):
    """빈 DB 로 초기화한 클라이언트. 테스트끼리 상태를 물려주지 않는다."""
    main.create_all(main.engine)
    with main.Session_() as session:
        for model in (ReviewLog, Composition, Card, Occurrence, Word):
            session.execute(delete(model))
        for i in range(words):
            word = Word(
                headword=f"word{i}", display=f"word{i}", kind=KIND_WORD,
                meaning_kr=f"뜻{i}", band="core", first_seen=TODAY,
            )
            session.add(word)
            session.flush()
            if with_examples:
                text = f"A sentence with word{i} inside."
                session.add(
                    Occurrence(
                        word_id=word.id, sentence=text, sentence_hash=sentence_key(text),
                        source_kind="upfirst", source_title="테스트 에피소드",
                        source_url="https://example.com/ep", occurred_on=TODAY,
                    )
                )
        session.commit()

    client = TestClient(main.app)
    with client:
        pass  # lifespan 실행
    return client


def login(client):
    response = client.post("/login", data={"password": "hunter2"}, follow_redirects=False)
    assert response.status_code == 303, response.status_code
    return client


# --------------------------------------------------------------------------
# 인증
# --------------------------------------------------------------------------


def test_anonymous_visitor_is_sent_to_login():
    client = fresh_client()
    for path in ("/", "/study", "/words", "/stats", "/export"):
        response = client.get(path, follow_redirects=False)
        assert response.status_code == 303, path
        assert response.headers["location"] == "/login", path


def test_wrong_password_is_rejected():
    client = fresh_client()
    response = client.post("/login", data={"password": "nope"}, follow_redirects=False)
    assert response.status_code == 401
    assert "비밀번호가 맞지 않습니다" in response.text


def test_login_grants_access():
    client = login(fresh_client())
    assert client.get("/").status_code == 200


def test_partial_request_gets_redirect_header_not_a_login_page():
    """조각 요청에 로그인 HTML 을 밀어 넣으면 문제 영역이 로그인 폼으로 바뀐다."""
    client = fresh_client()
    response = client.get("/study/card", headers={"X-Partial": "1"}, follow_redirects=False)
    assert response.status_code == 204
    assert response.headers["X-Redirect"] == "/login"


def test_health_and_manifest_are_public():
    client = fresh_client()
    assert client.get("/healthz").text == "ok"
    manifest = client.get("/manifest.webmanifest").json()
    assert manifest["display"] == "standalone"
    assert client.get("/sw.js").status_code == 200


# --------------------------------------------------------------------------
# 복습 흐름
# --------------------------------------------------------------------------


def test_study_page_serves_a_question():
    client = login(fresh_client(words=5))
    page = client.get("/study")
    assert page.status_code == 200
    assert 'name="token"' in page.text
    assert 'class="choice"' in page.text


def test_answering_correctly_shows_the_source_sentence():
    client = login(fresh_client(words=5))
    client.get("/study")

    with main.Session_() as session:
        card = study.next_card(session)
        question = study.build_question(session, card)
        session.commit()
        token = main._issue(question, study.now_utc())
        answer = question.answer

    response = client.post("/study/answer", data={"token": token, "given": answer})
    assert response.status_code == 200
    assert "정답" in response.text
    assert "A sentence with" in response.text, "원문 문장을 보여 줘야 한다"
    assert "테스트 에피소드" in response.text


def test_wrong_answer_keeps_the_card_in_the_session():
    client = login(fresh_client(words=5))
    with main.Session_() as session:
        card = study.next_card(session)
        question = study.build_question(session, card)
        session.commit()
        token = main._issue(question, study.now_utc())
        card_id = card.id

    response = client.post("/study/answer", data={"token": token, "given": "틀린 답"})
    assert "오답" in response.text
    assert "다시 나옵니다" in response.text

    with main.Session_() as session:
        state = study.queue_state(session)
        assert card_id in state.retry_ready + state.retry_waiting


def test_forged_token_is_rejected():
    client = login(fresh_client(words=2))
    response = client.post("/study/answer", data={"token": "forged.token.value", "given": "x"})
    assert response.status_code == 400


def test_token_signed_with_another_key_is_rejected():
    """정답이 토큰에 들어 있으므로 서명이 뚫리면 채점이 무의미해진다."""
    client = login(fresh_client(words=2))
    from itsdangerous import URLSafeTimedSerializer

    forged = URLSafeTimedSerializer("different-key", salt=main.QUESTION_SALT).dumps(
        {"card_id": 1, "stage": "recognition", "answer": "뜻0",
         "occurrence_id": None, "choices": None, "shown_at": study.now_utc().isoformat()}
    )
    assert client.post("/study/answer", data={"token": forged, "given": "뜻0"}).status_code == 400


def test_expired_token_asks_for_a_new_question():
    client = login(fresh_client(words=2))
    original = security.QUESTION_MAX_AGE
    security.QUESTION_MAX_AGE = -1  # 발급 직후에도 만료로 본다
    try:
        with main.Session_() as session:
            card = study.next_card(session)
            question = study.build_question(session, card)
            session.commit()
            token = main._issue(question, study.now_utc())
        response = client.post("/study/answer", data={"token": token, "given": "x"})
        assert response.status_code == 409
    finally:
        security.QUESTION_MAX_AGE = original


def test_response_time_is_measured_on_the_server():
    """토큰에 담긴 출제 시각으로 잰다. 클라이언트가 보낸 값을 믿지 않는다."""
    client = login(fresh_client(words=5))
    shown_at = study.now_utc() - dt.timedelta(seconds=12)

    with main.Session_() as session:
        card = study.next_card(session)
        question = study.build_question(session, card)
        session.commit()
        token = main._issue(question, shown_at)
        answer = question.answer

    client.post("/study/answer", data={"token": token, "given": answer})
    with main.Session_() as session:
        entry = session.scalar(select(ReviewLog))
        assert 11_000 <= entry.response_ms <= 14_000, entry.response_ms


def test_session_end_screen_explains_the_wait():
    client = login(fresh_client(words=0))
    page = client.get("/study")
    assert "지금 낼 카드가 없습니다" in page.text


# --------------------------------------------------------------------------
# 어휘
# --------------------------------------------------------------------------


def test_word_search_filters_by_headword_and_meaning():
    client = login(fresh_client(words=3))
    assert client.get("/words?q=word1").text.count('class="w-display"') == 1
    assert client.get("/words?q=뜻2").text.count('class="w-display"') == 1
    assert client.get("/words?q=없는단어").text.count('class="w-display"') == 0


def test_marking_a_word_known_suspends_its_card_without_deleting_history():
    client = login(fresh_client(words=2))
    with main.Session_() as session:
        card = study.next_card(session)
        session.commit()
        word_id, card_id = card.word_id, card.id

    response = client.post(f"/words/{word_id}/known", headers={"X-Partial": "1"})
    assert "다시 학습하기" in response.text

    with main.Session_() as session:
        card = session.get(Card, card_id)
        assert card is not None, "카드를 지우면 지금까지의 복습 기록이 날아간다"
        assert card.suspended is True
        assert session.get(Word, word_id).known is True
        assert card_id not in study.queue_state(session).due


def test_known_button_returns_to_the_word_page():
    """조각만 돌려주면 CSS 도 내비게이션도 없는 맨 HTML 에 사용자가 갇힌다.

    어휘 상세 화면에는 조각을 갈아 끼울 자바스크립트가 없다 — study.js 는 #card 가
    있을 때만 붙는다.
    """
    client = login(fresh_client(words=1))
    with main.Session_() as session:
        word_id = session.scalar(select(Word.id))

    response = client.post(f"/words/{word_id}/known", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == f"/words/{word_id}"

    page = client.post(f"/words/{word_id}/known", follow_redirects=True)
    assert "<html" in page.text, "완전한 페이지로 돌아와야 한다"
    assert "다시 학습하기" in page.text or "이미 아는 단어" in page.text


def test_javascript_urls_are_not_rendered_as_links():
    """source_url 은 NPR 피드와 /ingest 에서 온다 — 우리가 쓴 값이 아니다.

    속성 이스케이프는 javascript: 스킴을 막지 못한다.
    """
    client = login(fresh_client(words=1))
    with main.Session_() as session:
        occurrence = session.scalar(select(Occurrence))
        occurrence.source_url = "javascript:alert(document.cookie)"
        word_id = occurrence.word_id
        session.commit()

    page = client.get(f"/words/{word_id}").text
    assert "javascript:" not in page
    assert "테스트 에피소드" in page, "링크만 빠지고 출처 표시는 남아야 한다"

    assert main._external_url("https://example.com/ep") == "https://example.com/ep"
    assert main._external_url("http://example.com/ep") == "http://example.com/ep"
    assert main._external_url("JavaScript:alert(1)") is None
    assert main._external_url("data:text/html,<script>") is None
    assert main._external_url(None) is None


def test_unknown_word_id_is_a_404():
    client = login(fresh_client())
    assert client.get("/words/99999").status_code == 404


# --------------------------------------------------------------------------
# 로컬 연동
# --------------------------------------------------------------------------


PAYLOAD = {
    "entries": [
        {
            "display": "ratchet up", "kind": "expression",
            "meaning_kr": "단계적으로 강화하다", "source_kind": "planetmoney",
            "source_title": "테스트", "occurred_on": "2026-08-20",
            "sentence": "They ratchet up the pressure.",
        }
    ]
}


def test_ingest_requires_the_shared_token():
    client = fresh_client(words=0)
    assert client.post("/ingest", json=PAYLOAD).status_code == 401
    assert client.post("/ingest", json=PAYLOAD,
                       headers={"X-Ingest-Token": "wrong"}).status_code == 401


def test_ingest_is_idempotent():
    client = fresh_client(words=0)
    headers = {"X-Ingest-Token": "ingest-secret"}

    first = client.post("/ingest", json=PAYLOAD, headers=headers).json()
    second = client.post("/ingest", json=PAYLOAD, headers=headers).json()

    assert (first["words_created"], first["occurrences_created"]) == (1, 1)
    assert (second["words_created"], second["occurrences_created"]) == (0, 0)
    assert second["total_words"] == 1


def test_ingest_does_not_need_a_login_cookie():
    """로컬 cron 이 부르는 경로다. 사람 세션과 섞지 않는다."""
    client = fresh_client(words=0)
    assert client.post("/ingest", json=PAYLOAD,
                       headers={"X-Ingest-Token": "ingest-secret"}).status_code == 200


def test_ingest_rejects_malformed_entries():
    client = fresh_client(words=0)
    bad = {"entries": [{"display": "", "kind": "word", "meaning_kr": "뜻",
                        "source_kind": "upfirst", "occurred_on": "2026-08-20"}]}
    assert client.post("/ingest", json=bad,
                       headers={"X-Ingest-Token": "ingest-secret"}).status_code == 422


def test_handled_endpoint_lists_words_to_skip():
    """로컬 파이프라인이 새 후보를 고를 때 뺄 목록. 카드가 서버로 옮겨간 뒤로
    로컬만 보고는 알 수 없다."""
    client = login(fresh_client(words=2))
    with main.Session_() as session:
        card = study.next_card(session)
        carded = card.word.headword
        other = session.scalars(select(Word).where(Word.id != card.word_id)).first()
        other.band = "known"
        session.commit()

    payload = client.get("/api/handled", headers={"X-Ingest-Token": "ingest-secret"}).json()
    assert carded in payload["headwords"], "카드가 있는 어휘는 빠져야 한다"
    assert other.headword in payload["headwords"], "known 밴드도 빠져야 한다"


def test_handled_endpoint_requires_the_token():
    client = fresh_client(words=1)
    assert client.get("/api/handled").status_code == 401


def test_export_contains_every_table():
    client = login(fresh_client(words=2))
    with main.Session_() as session:
        card = study.next_card(session)
        question = study.build_question(session, card)
        session.commit()
        token = main._issue(question, study.now_utc())
    client.post("/study/answer", data={"token": token, "given": question.answer})

    payload = client.get("/export").json()
    assert set(payload) == {"exported_at", "word", "occurrence", "card", "review_log"}
    assert len(payload["review_log"]) == 1
    assert isinstance(payload["card"][0]["due"], str), "시각은 ISO 문자열로 나가야 한다"


def test_stats_page_renders_with_no_history():
    client = login(fresh_client(words=1))
    page = client.get("/stats")
    assert page.status_code == 200
    assert "아직 기록이 없습니다" in page.text


# --------------------------------------------------------------------------
# 주 1회 작문
# --------------------------------------------------------------------------


def _practised_cards(count=3):
    """쓰기 과제를 낼 수 있을 만큼 인출해 본 카드를 만든다."""
    now = study.now_utc()
    with main.Session_() as session:
        for i in range(count):
            word = Word(headword=f"prod{i}", display=f"prod{i}", kind=KIND_WORD,
                        meaning_kr=f"산출{i}", band="core", first_seen=TODAY)
            session.add(word)
            session.flush()
            session.add(Card(word_id=word.id, stage=STAGE_PRODUCTION, reps=4,
                             stability=70.0, difficulty=5.0, state=2, due=now,
                             last_review=now - dt.timedelta(hours=1), created_at=now))
        session.commit()


def test_write_page_explains_when_there_is_no_task():
    client = login(fresh_client(words=1))
    assert "이번 주 과제 없음" in client.get("/write").text


def test_write_page_offers_three_words():
    client = login(fresh_client(words=0))
    _practised_cards()
    page = client.get("/write").text
    assert "prod0" in page and "prod1" in page and "prod2" in page
    assert "<textarea" in page


def test_submitting_a_composition_marks_it_pending():
    client = login(fresh_client(words=0))
    _practised_cards()
    client.get("/write")
    response = client.post("/write", data={"text": "I used prod0 today."},
                           follow_redirects=True)
    assert "첨삭 대기" in response.text

    tasks = client.get("/api/tasks", headers={"X-Ingest-Token": "ingest-secret"}).json()
    assert len(tasks["compositions"]) == 1
    assert tasks["compositions"][0]["text"] == "I used prod0 today."


def test_empty_composition_is_rejected_by_the_endpoint():
    client = login(fresh_client(words=0))
    _practised_cards()
    client.get("/write")
    assert client.post("/write", data={"text": "  "}).status_code == 400


def test_tasks_endpoint_requires_the_token():
    client = fresh_client(words=0)
    assert client.get("/api/tasks").status_code == 401
    assert client.post("/api/tasks", json={"mnemonics": [], "feedback": []}).status_code == 401


def test_worker_results_land_on_the_right_rows():
    client = login(fresh_client(words=0))
    _practised_cards()
    client.get("/write")
    client.post("/write", data={"text": "draft"})

    with main.Session_() as session:
        task_id = session.scalar(select(Composition.id))
        word_id = session.scalar(select(Word.id))

    headers = {"X-Ingest-Token": "ingest-secret"}
    result = client.post("/api/tasks", headers=headers, json={
        "mnemonics": [{"word_id": word_id, "text": "기억술 훅"}],
        "feedback": [{"composition_id": task_id, "text": "## 고친 글\n..."}],
    }).json()

    assert result == {"glosses": 0, "mnemonics": 1, "feedback": 1}
    with main.Session_() as session:
        assert session.get(Word, word_id).mnemonic == "기억술 훅"
        task = session.get(Composition, task_id)
        assert task.feedback.startswith("## 고친 글")
        assert task.feedback_at is not None
        assert task.awaiting_feedback is False


def test_worker_results_ignore_rows_that_disappeared():
    """워커가 도는 사이에 어휘를 지웠을 수 있다. 없는 id 로 터지면 안 된다."""
    client = fresh_client(words=0)
    result = client.post("/api/tasks", headers={"X-Ingest-Token": "ingest-secret"}, json={
        "mnemonics": [{"word_id": 99999, "text": "x"}],
        "feedback": [{"composition_id": 99999, "text": "y"}],
    })
    assert result.status_code == 200
    assert result.json() == {"glosses": 0, "mnemonics": 0, "feedback": 0}


def test_mnemonic_shows_only_on_leech_cards():
    client = login(fresh_client(words=2))
    with main.Session_() as session:
        card = study.next_card(session)
        card.word.mnemonic = "기억술 훅"
        question = study.build_question(session, card)
        session.commit()
        token = main._issue(question, study.now_utc())
        card_id = card.id

    plain = client.post("/study/answer", data={"token": token, "given": question.answer})
    assert "기억술" not in plain.text, "잘 나가는 카드에 붙이면 인출을 대신해 버린다"

    with main.Session_() as session:
        session.get(Card, card_id).leech = True
        session.commit()
        card = session.get(Card, card_id)
        question = study.build_question(session, card)
        session.commit()
        token = main._issue(question, study.now_utc())

    stuck = client.post("/study/answer", data={"token": token, "given": question.answer})
    assert "기억술 훅" in stuck.text


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
