"""vocab.sync 테스트 — 로컬에서 서버로 어휘를 밀어 올리는 경로"""

import datetime as dt
import os
import sys
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(__file__))

os.environ.setdefault("TELEGRAM_BOT_TOKEN", "fake_token")
os.environ.setdefault("TELEGRAM_CHAT_ID", "123456")

import requests  # noqa: E402

import config  # noqa: E402
from vocab import collect, sync  # noqa: E402
from vocab.models import KIND_WORD  # noqa: E402


class FakeResponse:
    def __init__(self, payload, status=200):
        self._payload = payload
        self.status_code = status
        self.ok = 200 <= status < 300
        self.text = str(payload)

    def json(self):
        return self._payload


def entry(display="stalemate"):
    return collect.Entry(
        display=display, kind=KIND_WORD, meaning_kr="교착 상태",
        source_kind="upfirst", source_title="테스트", occurred_on=dt.date(2026, 8, 20),
        sentence="The talks reached a stalemate.",
    )


def configured(**overrides):
    """서버 설정을 잠시 채운다."""
    values = {"VOCAB_SERVER_URL": "https://vocab.example.com",
              "VOCAB_INGEST_TOKEN": "secret", "VOCAB_APP_URL": "https://vocab.example.com"}
    values.update(overrides)
    return patch.multiple(config, **values)


# --------------------------------------------------------------------------


def test_payload_matches_the_server_schema():
    body = sync._payload(entry())
    assert body["occurred_on"] == "2026-08-20", "날짜는 ISO 문자열로 나가야 한다"
    assert set(body) == {
        "display", "kind", "meaning_kr", "source_kind", "source_title", "occurred_on",
        "meaning_en", "usage_note", "sentence", "translation_kr", "source_url",
    }


def test_push_requires_server_url():
    with configured(VOCAB_SERVER_URL=""), \
         patch.object(collect, "gather", return_value=([entry()], collect.Stats())), \
         patch.object(collect, "collect", return_value=collect.Stats()):
        try:
            sync.push()
        except sync.SyncError as e:
            assert "VOCAB_SERVER_URL" in str(e)
        else:
            raise AssertionError("서버 주소 없이 밀어 올리면 안 된다")


def test_push_requires_ingest_token():
    with configured(VOCAB_INGEST_TOKEN=""), \
         patch.object(collect, "gather", return_value=([entry()], collect.Stats())), \
         patch.object(collect, "collect", return_value=collect.Stats()):
        try:
            sync.push()
        except sync.SyncError as e:
            assert "VOCAB_INGEST_TOKEN" in str(e)
        else:
            raise AssertionError("토큰 없이 밀어 올리면 안 된다")


def test_dry_run_writes_nothing_anywhere():
    """"보내지 않고 내용만 확인" 은 로컬 저장소도 건드리지 않는다는 뜻으로 읽힌다."""
    stats = collect.Stats(files=3)
    with configured(), \
         patch.object(collect, "gather", return_value=([entry("a"), entry("b")], stats)), \
         patch.object(collect, "collect") as local, \
         patch.object(requests, "post") as post:
        result = sync.push(dry_run=True)
        post.assert_not_called()
        local.assert_not_called()

    assert result["entries"] == 2
    assert result["by_source"] == {"upfirst": 2}


def test_push_batches_large_payloads():
    entries = [entry(f"word{i}") for i in range(sync.BATCH + 5)]
    response = FakeResponse({"received": 1, "words_created": 1, "words_updated": 0,
                             "occurrences_created": 1, "total_words": 7})
    with configured(), \
         patch.object(collect, "gather", return_value=(entries, collect.Stats())), \
         patch.object(collect, "collect", return_value=collect.Stats()), \
         patch.object(requests, "post", return_value=response) as post:
        totals = sync.push()

    assert post.call_count == 2, "BATCH 를 넘으면 나눠 보낸다"
    assert totals["words_created"] == 2, "배치별 결과가 합산돼야 한다"
    assert totals["total_words"] == 7, "총계는 마지막 응답의 값을 쓴다"


def test_push_sends_the_ingest_token_as_a_header():
    with configured(), \
         patch.object(collect, "gather", return_value=([entry()], collect.Stats())), \
         patch.object(collect, "collect", return_value=collect.Stats()), \
         patch.object(requests, "post", return_value=FakeResponse({"total_words": 1})) as post:
        sync.push()

    assert post.call_args.kwargs["headers"]["X-Ingest-Token"] == "secret"
    assert post.call_args.args[0] == "https://vocab.example.com/ingest"


def test_push_raises_on_server_error():
    with configured(), \
         patch.object(collect, "gather", return_value=([entry()], collect.Stats())), \
         patch.object(collect, "collect", return_value=collect.Stats()), \
         patch.object(requests, "post", return_value=FakeResponse({"detail": "nope"}, status=401)):
        try:
            sync.push()
        except sync.SyncError as e:
            assert "401" in str(e)
        else:
            raise AssertionError("서버가 거부하면 조용히 성공하면 안 된다")


def test_push_updates_the_local_store_too():
    """로컬 저장소는 어휘의 원본이다. 서버로 보내면서 로컬을 건너뛰면 둘이 갈라진다."""
    with configured(), \
         patch.object(collect, "gather", return_value=([entry()], collect.Stats())), \
         patch.object(collect, "collect", return_value=collect.Stats()) as local, \
         patch.object(requests, "post", return_value=FakeResponse({"total_words": 1})):
        sync.push()

    local.assert_called_once()
    assert local.call_args.kwargs["prepared"] is not None, "같은 파일을 두 번 파싱하지 않는다"


def test_notify_sends_only_when_something_is_due():
    import messenger

    progress = {"due": 0, "new_available": 0, "unresolved": 0}
    with configured(), patch.object(sync, "server_progress", return_value=progress), \
         patch.object(messenger, "_send_message", return_value=True) as send:
        assert sync.notify() is False
        send.assert_not_called()

    with configured(), \
         patch.object(sync, "server_progress", return_value={"due": 5, "new_available": 2, "unresolved": 1}), \
         patch.object(messenger, "_send_message", return_value=True) as send:
        assert sync.notify() is True
        assert "복습 6장" in send.call_args[0][0]


def test_status_uses_the_machine_endpoint():
    with configured(), \
         patch.object(requests, "get", return_value=FakeResponse({"due": 3})) as get:
        assert sync.server_progress() == {"due": 3}
    assert get.call_args.args[0] == "https://vocab.example.com/api/progress"


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
