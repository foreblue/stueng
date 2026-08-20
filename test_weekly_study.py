"""weekly_study.py 주간 학습 테스트"""
import json
import os
import sys
import tempfile
from datetime import date
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(__file__))

import weekly_study
import weekly_analyzer
from sources.base import Episode


def _lesson(day):
    return {
        "day": day,
        "vocabulary": [
            {"word": f"word{day}", "definition_kr": "뜻", "definition_en": "definition", "example": "example"}
        ],
        "expressions": [
            {"expression": f"expr{day}", "meaning_kr": "의미", "usage_note": "사용법", "example": "example"}
        ],
    }


def _plan(start="2026-06-04"):
    return {
        "prepared_at": "2026-06-04T04:00:00",
        "start_date": start,
        "end_date": "2026-06-10",
        "study_days": 5,
        "study_dates": ["2026-06-04", "2026-06-05", "2026-06-08", "2026-06-09", "2026-06-10"],
        "episode": {
            "source": "Planet Money",
            "title": "Test Planet Money",
            "episode_url": "https://example.com/episode",
            "audio_url": "https://example.com/audio.mp3",
            "podcast_url": "https://podcasts.apple.com/episode",
            "published": "2026-06-03",
            "duration_sec": 1800,
            "transcript": "transcript",
        },
        "weekly_analysis": {"lessons": [_lesson(i) for i in range(1, 6)]},
        "sent_dates": [],
    }


def test_first_study_date_skips_weekends():
    """계획을 금요일에 세워도 첫 학습일은 평일 기준으로 잡힌다."""
    plan = _plan()
    assert weekly_study._first_study_date(plan) == date(2026, 6, 4)

    weekend_plan = dict(plan, start_date="2026-06-06")  # 토요일
    assert weekly_study._first_study_date(weekend_plan) == date(2026, 6, 8)


def test_episode_is_announced_once_on_the_first_study_day():
    """어휘를 평일마다 흘려보내던 동작을 대체한다 — 이제 에피소드만 한 번 알린다."""
    with tempfile.TemporaryDirectory() as tmp:
        data_dir = Path(tmp)
        path = data_dir / "planetmoney-2026-06-04.json"
        path.write_text(json.dumps(_plan(), ensure_ascii=False), encoding="utf-8")
        sent = []

        def fake_send(episode, **_kwargs):
            sent.append(episode.title)
            return True

        with (
            patch.object(weekly_study, "DATA_DIR", data_dir),
            patch.object(weekly_study.config, "validate", lambda: None),
            patch.object(weekly_study.messenger, "send_episode", fake_send),
        ):
            assert weekly_study.send(date(2026, 6, 4)) is True
            assert weekly_study.send(date(2026, 6, 4)) is False, "같은 날 두 번 보내지 않는다"

        assert len(sent) == 1
        assert json.loads(path.read_text(encoding="utf-8"))["sent_dates"] == ["2026-06-04"]


def test_no_message_on_the_other_weekdays():
    """예전에는 평일마다 Day N 을 보냈다. 지금은 첫날 말고는 조용하다."""
    with tempfile.TemporaryDirectory() as tmp:
        data_dir = Path(tmp)
        path = data_dir / "planetmoney-2026-06-04.json"
        path.write_text(json.dumps(_plan(), ensure_ascii=False), encoding="utf-8")
        sent = []

        with (
            patch.object(weekly_study, "DATA_DIR", data_dir),
            patch.object(weekly_study.config, "validate", lambda: None),
            patch.object(weekly_study.messenger, "send_episode",
                         lambda episode, **_k: sent.append(episode.title) or True),
        ):
            for day in (5, 8, 9, 10):
                assert weekly_study.send(date(2026, 6, day)) is False, day

        assert sent == []


def test_prepare_saves_weekly_plan():
    episode = Episode(
        title="Prepared Episode",
        audio_url="https://example.com/audio.mp3",
        transcript="transcript",
        source_name="Planet Money",
        episode_url="https://example.com/episode",
        duration_sec=1200,
        published="2026-06-03",
        podcast_url="https://podcasts.apple.com/episode",
    )

    with tempfile.TemporaryDirectory() as tmp:
        data_dir = Path(tmp)
        with (
            patch.object(weekly_study, "DATA_DIR", data_dir),
            patch.object(weekly_study.config, "validate", lambda: None),
            patch.object(weekly_study.PlanetMoney, "fetch_latest", lambda _self: episode),
            patch.object(
                weekly_study.weekly_analyzer,
                "analyze_weekly",
                lambda _ep: ({"lessons": [_lesson(i) for i in range(1, 6)]}, ""),
            ),
        ):
            path = weekly_study.prepare(date(2026, 6, 4))

        saved = json.loads(path.read_text(encoding="utf-8"))
        assert saved["start_date"] == "2026-06-04"
        assert saved["end_date"] == "2026-06-10"
        assert saved["study_dates"] == ["2026-06-04", "2026-06-05", "2026-06-08", "2026-06-09", "2026-06-10"]
        assert saved["episode"]["title"] == "Prepared Episode"
        assert saved["episode"]["podcast_url"] == "https://podcasts.apple.com/episode"
        assert len(saved["weekly_analysis"]["lessons"]) == 5


def test_weekly_plan_validation_rejects_incomplete_days():
    bad_plan = {"lessons": [_lesson(1)]}
    try:
        weekly_analyzer._validate_plan(bad_plan)
    except ValueError as e:
        assert "5개" in str(e)
    else:
        raise AssertionError("불완전한 주간 계획은 거부되어야 합니다.")


if __name__ == "__main__":
    tests = [
        test_first_study_date_skips_weekends,
        test_episode_is_announced_once_on_the_first_study_day,
        test_no_message_on_the_other_weekdays,
        test_prepare_saves_weekly_plan,
        test_weekly_plan_validation_rejects_incomplete_days,
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
