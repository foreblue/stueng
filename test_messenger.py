"""messenger.py 포맷 테스트"""
import os
import sys
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(__file__))

# Telegram API 호출 없이 테스트하기 위해 환경변수 설정
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "fake_token")
os.environ.setdefault("TELEGRAM_CHAT_ID", "123456")

import messenger
from messenger import _format_due, _format_episode, _split_message
from sources.base import Episode

SAMPLE_EPISODE = Episode(
    title="Test Podcast Episode",
    audio_url="https://example.com/audio.mp3",
    transcript="This is a test transcript.",
    source_name="Up First",
    episode_url="https://example.com/episode",
    duration_sec=600,
    published="2026-04-01",
)

APP_URL = "https://vocab.example.com"


# --------------------------------------------------------------------------
# 에피소드 알림
# --------------------------------------------------------------------------


def test_episode_message_carries_title_duration_and_links():
    text = _format_episode(SAMPLE_EPISODE)
    assert "Test Podcast Episode" in text
    assert "~10분" in text
    assert "https://example.com/episode" in text
    assert "Up First" in text


def test_episode_message_never_contains_vocabulary():
    """어휘 본문을 텔레그램으로 보내던 동작을 되살리지 않기 위한 회귀 테스트.

    읽기만 하는 노출은 학습이 되지 않는다는 것이 이 구조를 바꾼 이유다.
    단어를 꺼내는 일은 복습 웹앱이 맡는다.
    """
    text = _format_episode(SAMPLE_EPISODE, app_url=APP_URL)
    for banned in ("어휘", "Vocabulary", "표현 (Expressions)", "핵심 문장", "뜻:", "예문:"):
        assert banned not in text, banned


def test_episode_message_links_to_the_review_app():
    assert APP_URL in _format_episode(SAMPLE_EPISODE, app_url=APP_URL)
    assert "복습" in _format_episode(SAMPLE_EPISODE, app_url=APP_URL)


def test_episode_message_without_app_url_omits_the_link():
    text = _format_episode(SAMPLE_EPISODE)
    assert "복습하기" not in text


def test_podcast_url_wins_for_the_listen_link():
    episode = Episode(**{**SAMPLE_EPISODE.__dict__, "podcast_url": "https://podcasts.example/ep"})
    assert "https://podcasts.example/ep" in _format_episode(episode)


def test_html_is_escaped():
    episode = Episode(**{**SAMPLE_EPISODE.__dict__, "title": "Tom & Jerry <script>"})
    text = _format_episode(episode)
    assert "&amp;" in text and "&lt;script&gt;" in text
    assert "<script>" not in text


def test_analysis_failure_is_reported():
    ok = _format_episode(SAMPLE_EPISODE)
    with patch.object(messenger, "_send_message", return_value=True) as send:
        messenger.send_episode(SAMPLE_EPISODE, fail_reason="프록시 연결 실패")
    sent = send.call_args[0][0]
    assert "AI 분석 실패" in sent
    assert "프록시 연결 실패" in sent
    assert "AI 분석 실패" not in ok


# --------------------------------------------------------------------------
# 복습 알림
# --------------------------------------------------------------------------


def test_due_message_shows_counts():
    text = _format_due(due=12, new_available=5, unresolved=0, app_url=APP_URL)
    assert "복습 12장" in text
    assert "새 단어 5개" in text
    assert APP_URL in text


def test_due_message_counts_unresolved_cards_as_review():
    text = _format_due(due=4, new_available=0, unresolved=3, app_url=APP_URL)
    assert "복습 7장" in text
    assert "못 맞힌 카드 3장" in text


def test_due_message_is_empty_when_there_is_nothing_to_do():
    assert _format_due(due=0, new_available=0, unresolved=0, app_url=APP_URL) == ""


def test_send_due_does_not_send_empty_notifications():
    """빈 알림을 보내면 알림 자체를 무시하게 된다."""
    with patch.object(messenger, "_send_message", return_value=True) as send:
        assert messenger.send_due(0, 0, 0, APP_URL) is False
        send.assert_not_called()

        assert messenger.send_due(3, 0, 0, APP_URL) is True
        send.assert_called_once()


# --------------------------------------------------------------------------
# 분할
# --------------------------------------------------------------------------


def test_message_split():
    long_text = "\n\n".join(["문단 " + "가" * 100 for _ in range(60)])
    parts = _split_message(long_text)
    assert len(parts) > 1
    assert all(len(part) <= 4096 for part in parts)


def test_short_message_is_not_split():
    assert _split_message("짧은 메시지") == ["짧은 메시지"]


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
