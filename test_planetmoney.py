"""sources/planetmoney.py 가드 테스트"""
import os
import sys
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(__file__))

from sources import planetmoney
from sources.planetmoney import PlanetMoney


class FakeFeed:
    def __init__(self, entries):
        self.entries = entries


def _make_entry(**kwargs):
    defaults = {
        "title": "Sample Planet Money",
        "id": "sample-guid",
        "published": "2026-06-03",
        "enclosures": [],
        "itunes_duration": "0",
    }
    defaults.update(kwargs)

    class Entry(SimpleNamespace):
        def get(self, key, default=None):
            return getattr(self, key, default)

    return Entry(**defaults)


def test_fetch_latest_returns_planet_money_episode():
    entry = _make_entry(
        title="Planet Money Ep",
        link="https://www.npr.org/2026/06/03/nx-s1-1234567/planet-money-ep",
        enclosures=[{"type": "audio/mpeg", "href": "https://example.com/pm.mp3"}],
        itunes_duration="1800",
    )

    with (
        patch.object(planetmoney.feedparser, "parse", lambda _url: FakeFeed([entry])),
        patch.object(planetmoney, "_fetch_transcript", lambda _aid: "planet money transcript"),
        patch.object(planetmoney, "_fetch_apple_episode_url", lambda _title: "https://podcasts.apple.com/episode"),
    ):
        ep = PlanetMoney().fetch_latest()

    assert ep is not None
    assert ep.source_name == "Planet Money"
    assert ep.title == "Planet Money Ep"
    assert ep.audio_url == "https://example.com/pm.mp3"
    assert ep.duration_sec == 1800
    assert ep.transcript == "planet money transcript"
    assert ep.podcast_url == "https://podcasts.apple.com/episode"


def test_entry_without_transcript_is_skipped():
    bad = _make_entry(
        title="No Transcript",
        link="https://www.npr.org/2026/06/03/nx-s1-1111111/no-transcript",
    )
    good = _make_entry(
        title="With Transcript",
        link="https://www.npr.org/2026/06/04/nx-s1-2222222/with-transcript",
    )

    def fake_transcript(article_id):
        if article_id == "nx-s1-1111111":
            return ""
        return "full transcript"

    with (
        patch.object(planetmoney.feedparser, "parse", lambda _url: FakeFeed([bad, good])),
        patch.object(planetmoney, "_fetch_transcript", fake_transcript),
        patch.object(planetmoney, "_fetch_apple_episode_url", lambda _title: ""),
    ):
        ep = PlanetMoney().fetch_latest()

    assert ep is not None
    assert ep.title == "With Transcript"


def test_apple_episode_url_lookup_selects_planet_money_episode():
    class Response:
        def raise_for_status(self):
            pass

        def json(self):
            return {
                "results": [
                    {
                        "collectionId": 123,
                        "trackName": "Planet Money Ep",
                        "trackViewUrl": "https://example.com/wrong",
                    },
                    {
                        "collectionId": planetmoney.APPLE_COLLECTION_ID,
                        "trackName": "Planet Money Ep",
                        "trackViewUrl": "https://podcasts.apple.com/right",
                    },
                ]
            }

    with patch.object(planetmoney.requests, "get", lambda *_args, **_kwargs: Response()):
        assert planetmoney._fetch_apple_episode_url("Planet Money Ep") == "https://podcasts.apple.com/right"


if __name__ == "__main__":
    tests = [
        test_fetch_latest_returns_planet_money_episode,
        test_entry_without_transcript_is_skipped,
        test_apple_episode_url_lookup_selects_planet_money_episode,
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

    print(f"\n{'='*40}")
    print(f"결과: {len(tests) - failed}/{len(tests)} 통과")
    sys.exit(1 if failed else 0)
