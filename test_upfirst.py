"""sources/upfirst.py 가드 테스트"""
import sys
import os
import logging
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(__file__))

from sources import upfirst
from sources.upfirst import UpFirst


class FakeFeed:
    def __init__(self, entries):
        self.entries = entries


def _make_entry(**kwargs):
    """feedparser entry 유사 객체. .get()과 속성 접근 모두 지원."""
    defaults = {
        "title": "Sample Title",
        "id": "sample-guid",
        "published": "2026-04-17",
        "enclosures": [],
        "itunes_duration": "0",
    }
    defaults.update(kwargs)

    class Entry(SimpleNamespace):
        def get(self, key, default=None):
            return getattr(self, key, default)

    return Entry(**defaults)


def test_entry_without_link_skipped_and_logged(monkeypatch, caplog):
    """link 필드 누락 entry는 스킵되고 경고 로그가 남는다."""
    bad_entry = _make_entry(title="No Link Ep", id="guid-bad", published="2026-04-17")
    # SimpleNamespace라 link 속성 자체가 없음
    assert not hasattr(bad_entry, "link")

    good_entry = _make_entry(
        title="Good Ep",
        id="guid-good",
        link="https://www.npr.org/2026/04/17/nx-s1-1234567/good-ep",
        enclosures=[{"type": "audio/mpeg", "href": "https://example.com/a.mp3"}],
        itunes_duration="600",
    )

    monkeypatch.setattr(upfirst.feedparser, "parse", lambda url: FakeFeed([bad_entry, good_entry]))
    monkeypatch.setattr(upfirst, "_fetch_transcript", lambda aid: "transcript content")

    with caplog.at_level(logging.WARNING, logger="sources.upfirst"):
        ep = UpFirst().fetch_latest()

    assert ep is not None
    assert ep.title == "Good Ep"
    assert ep.episode_url == "https://www.npr.org/2026/04/17/nx-s1-1234567/good-ep"
    assert ep.audio_url == "https://example.com/a.mp3"
    assert ep.duration_sec == 600

    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert any("link 없음" in r.getMessage() for r in warnings), "경고 로그가 남아야 한다"
    msg = next(r.getMessage() for r in warnings if "link 없음" in r.getMessage())
    assert "No Link Ep" in msg
    assert "guid-bad" in msg
    assert "2026-04-17" in msg


def test_all_entries_without_link_returns_none(monkeypatch, caplog):
    """모든 entry가 link 없으면 None을 반환하고 예외는 던지지 않는다."""
    entries = [
        _make_entry(title="No Link A", id="guid-a"),
        _make_entry(title="No Link B", id="guid-b"),
    ]
    monkeypatch.setattr(upfirst.feedparser, "parse", lambda url: FakeFeed(entries))
    # 호출되면 안 되지만 안전하게 stub
    monkeypatch.setattr(upfirst, "_fetch_transcript", lambda aid: "")

    with caplog.at_level(logging.WARNING, logger="sources.upfirst"):
        ep = UpFirst().fetch_latest()

    assert ep is None
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert sum(1 for r in warnings if "link 없음" in r.getMessage()) == 2


def test_empty_feed_returns_none(monkeypatch):
    """피드가 비었으면 None 반환."""
    monkeypatch.setattr(upfirst.feedparser, "parse", lambda url: FakeFeed([]))
    assert UpFirst().fetch_latest() is None


def test_normal_flow_no_regression(monkeypatch):
    """정상 entry 하나만 있을 때 회귀 없음."""
    entry = _make_entry(
        title="Normal Ep",
        id="guid-n",
        link="https://www.npr.org/2026/04/18/nx-s1-9999999/normal-ep",
        enclosures=[{"type": "audio/mpeg", "href": "https://example.com/n.mp3"}],
        itunes_duration="900",
        published="2026-04-18",
    )
    monkeypatch.setattr(upfirst.feedparser, "parse", lambda url: FakeFeed([entry]))
    monkeypatch.setattr(upfirst, "_fetch_transcript", lambda aid: "full transcript")

    ep = UpFirst().fetch_latest()
    assert ep is not None
    assert ep.title == "Normal Ep"
    assert ep.transcript == "full transcript"
    assert ep.source_name == "Up First"
    assert ep.episode_url == "https://www.npr.org/2026/04/18/nx-s1-9999999/normal-ep"
    assert ep.duration_sec == 900
    assert ep.published == "2026-04-18"


def test_entry_without_article_id_skipped(monkeypatch):
    """link는 있지만 article_id 패턴 추출 실패 entry는 스킵된다."""
    bad = _make_entry(
        title="Weird URL",
        id="guid-w",
        link="https://www.npr.org/some-other-path",
    )
    good = _make_entry(
        title="Good",
        id="guid-g",
        link="https://www.npr.org/2026/04/18/nx-s1-1111111/good",
    )
    monkeypatch.setattr(upfirst.feedparser, "parse", lambda url: FakeFeed([bad, good]))
    monkeypatch.setattr(upfirst, "_fetch_transcript", lambda aid: "t")

    ep = UpFirst().fetch_latest()
    assert ep is not None
    assert ep.title == "Good"


# ---- pytest 없을 때 직접 실행용 간이 러너 ----
if __name__ == "__main__":
    try:
        import pytest  # type: ignore
        sys.exit(pytest.main([__file__, "-v"]))
    except ImportError:
        pass

    # 최소 monkeypatch/caplog shim
    class _MP:
        def __init__(self):
            self._undo = []

        def setattr(self, target, name_or_value, value=None):
            if value is None:
                # setattr("module.path.attr", value) 형태는 미지원 — 객체 기반만
                raise NotImplementedError
            obj, name, val = target, name_or_value, value
            old = getattr(obj, name)
            setattr(obj, name, val)
            self._undo.append((obj, name, old))

        def undo(self):
            for obj, name, old in reversed(self._undo):
                setattr(obj, name, old)
            self._undo.clear()

    class _CapLog:
        def __init__(self):
            self.records = []
            self._handler = None
            self._logger = None

        def at_level(self, level, logger=""):
            self._logger = logging.getLogger(logger)
            self._prev_level = self._logger.level
            self._logger.setLevel(level)

            parent = self

            class H(logging.Handler):
                def emit(self_inner, record):
                    parent.records.append(record)

            self._handler = H(level=level)
            self._logger.addHandler(self._handler)
            return self

        def __enter__(self):
            return self

        def __exit__(self, *a):
            if self._handler and self._logger:
                self._logger.removeHandler(self._handler)
                self._logger.setLevel(self._prev_level)

    tests = [
        ("test_entry_without_link_skipped_and_logged", test_entry_without_link_skipped_and_logged, True),
        ("test_all_entries_without_link_returns_none", test_all_entries_without_link_returns_none, True),
        ("test_empty_feed_returns_none", test_empty_feed_returns_none, False),
        ("test_normal_flow_no_regression", test_normal_flow_no_regression, False),
        ("test_entry_without_article_id_skipped", test_entry_without_article_id_skipped, False),
    ]
    failed = 0
    for name, fn, needs_caplog in tests:
        mp = _MP()
        try:
            if needs_caplog:
                fn(mp, _CapLog())
            else:
                fn(mp)
            print(f"PASS: {name}")
        except AssertionError as e:
            print(f"FAIL: {name} — {e}")
            failed += 1
        except Exception as e:
            print(f"ERROR: {name} — {type(e).__name__}: {e}")
            failed += 1
        finally:
            mp.undo()

    print(f"\n{'='*40}")
    print(f"결과: {len(tests) - failed}/{len(tests)} 통과")
    sys.exit(1 if failed else 0)
