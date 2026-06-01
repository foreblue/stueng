"""messenger.py 포맷 테스트"""
import sys
import os
from datetime import date

sys.path.insert(0, os.path.dirname(__file__))

# Telegram API 호출 없이 테스트하기 위해 환경변수 설정
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "fake_token")
os.environ.setdefault("TELEGRAM_CHAT_ID", "123456")

from sources.base import Episode
from messenger import _format_analysis, _format_weekly_lesson, _split_message

SAMPLE_EPISODE = Episode(
    title="Test Podcast Episode",
    audio_url="https://example.com/audio.mp3",
    transcript="This is a test transcript.",
    source_name="Up First",
    episode_url="https://example.com/episode",
    duration_sec=600,
    published="2026-04-01",
)

SAMPLE_ANALYSIS = {
    "vocabulary": [
        {
            "word": "unprecedented",
            "definition_kr": "전례 없는",
            "definition_en": "never done or known before",
            "example": "This is an unprecedented situation.",
        }
    ],
    "expressions": [
        {
            "expression": "on the fence",
            "meaning_kr": "결정을 못 내리고 있는",
            "usage_note": "Used when someone hasn't made a decision yet.",
        }
    ],
    "key_sentences": [
        {
            "sentence": "The government has yet to respond to the crisis.",
            "translation_kr": "정부는 아직 이 위기에 대응하지 않았습니다.",
            "explanation_kr": "'has yet to'는 '아직 ~하지 않았다'는 완료 부정 표현입니다.",
        }
    ],
}


def test_translation_in_message():
    """번역이 메시지에 포함되는지 확인"""
    result = _format_analysis(SAMPLE_EPISODE, SAMPLE_ANALYSIS)
    assert "번역: 정부는 아직 이 위기에 대응하지 않았습니다." in result, (
        "번역 문장이 메시지에 포함되어야 합니다."
    )
    print("PASS: 번역 포함 확인")


def test_explanation_still_present():
    """문법 설명도 함께 표시되는지 확인"""
    result = _format_analysis(SAMPLE_EPISODE, SAMPLE_ANALYSIS)
    assert "완료 부정 표현" in result, "문법 설명이 메시지에 포함되어야 합니다."
    print("PASS: 문법 설명 포함 확인")


def test_no_translation_field_graceful():
    """translation_kr 필드가 없어도 오류 없이 동작하는지 확인"""
    analysis = {
        "vocabulary": [],
        "expressions": [],
        "key_sentences": [
            {
                "sentence": "Old format sentence.",
                "explanation_kr": "구 형식 설명.",
                # translation_kr 없음
            }
        ],
    }
    result = _format_analysis(SAMPLE_EPISODE, analysis)
    assert "Old format sentence." in result
    assert "번역:" not in result
    print("PASS: translation_kr 없어도 정상 동작")


def test_message_split():
    """긴 메시지가 올바르게 분할되는지 확인"""
    long_text = "A" * 3000 + "\n\n" + "B" * 3000
    parts = _split_message(long_text)
    assert len(parts) == 2
    assert all(len(p) <= 4096 for p in parts)
    print("PASS: 메시지 분할 정상 동작")


def test_html_escape():
    """HTML 특수문자가 이스케이프되는지 확인"""
    analysis_with_html = {
        "vocabulary": [],
        "expressions": [],
        "key_sentences": [
            {
                "sentence": "Score: 10 > 5 & 3 < 4",
                "translation_kr": "점수: 10 > 5 & 3 < 4",
                "explanation_kr": "비교 표현",
            }
        ],
    }
    result = _format_analysis(SAMPLE_EPISODE, analysis_with_html)
    assert "&gt;" in result or "&amp;" in result, "HTML 이스케이프가 적용되어야 합니다."
    print("PASS: HTML 이스케이프 정상 동작")


def test_example_sentence_fallback():
    """LLM이 example_sentence 키를 써도 예문이 표시되는지 확인"""
    analysis = {
        "vocabulary": [
            {
                "word": "sedentary",
                "definition_kr": "앉아서 지내는",
                "definition_en": "spending much time seated",
                "example_sentence": "Modern life is increasingly sedentary.",
            }
        ],
        "expressions": [],
        "key_sentences": [],
    }
    result = _format_analysis(SAMPLE_EPISODE, analysis)
    assert "Modern life is increasingly sedentary." in result
    print("PASS: example_sentence fallback 정상 동작")


def test_weekly_lesson_format():
    lesson = {
        "day": 2,
        "vocabulary": [
            {
                "word": "loophole",
                "definition_kr": "허점",
                "definition_en": "an ambiguity that can be exploited",
                "example": "They found a loophole.",
            }
        ],
        "expressions": [
            {
                "expression": "tie it back to",
                "meaning_kr": "~와 연결 짓다",
                "usage_note": "어떤 주제를 더 큰 맥락과 연결할 때 씁니다.",
                "example": "We can tie it back to the economy.",
            }
        ],
    }
    result = _format_weekly_lesson(
        SAMPLE_EPISODE,
        lesson,
        run_date=date(2026, 6, 5),
        start_date=date(2026, 6, 4),
        total_days=5,
    )
    assert "Day 2/5" in result
    assert "오늘의 주요 단어 3개" in result
    assert "loophole" in result
    assert "tie it back to" in result
    assert "Podcasts 앱" in result
    print("PASS: 주간 학습 포맷 정상 동작")


def test_weekly_lesson_uses_podcast_episode_url_for_listen_link():
    episode = Episode(
        title="Planet Money Episode",
        audio_url="https://example.com/audio.mp3",
        transcript="",
        source_name="Planet Money",
        episode_url="https://www.npr.org/episode",
        duration_sec=600,
        published="2026-06-03",
        podcast_url="https://podcasts.apple.com/us/podcast/example/id290783428?i=123",
    )
    lesson = {"day": 1, "vocabulary": [], "expressions": []}
    result = _format_weekly_lesson(
        episode,
        lesson,
        run_date=date(2026, 6, 4),
        start_date=date(2026, 6, 4),
        total_days=5,
    )
    assert 'href="https://podcasts.apple.com/us/podcast/example/id290783428?i=123"' in result
    assert 'href="https://example.com/audio.mp3"' not in result
    print("PASS: 주간 학습 오디오 링크 episode URL 사용")


def test_weekly_lesson_uses_episode_url_for_podcasts_app_link():
    episode = Episode(
        title="Planet Money Episode",
        audio_url="https://example.com/audio.mp3",
        transcript="",
        source_name="Planet Money",
        episode_url="https://www.npr.org/episode",
        duration_sec=600,
        published="2026-06-03",
        podcast_url="https://podcasts.apple.com/us/podcast/example/id290783428?i=123",
    )
    lesson = {"day": 1, "vocabulary": [], "expressions": []}
    result = _format_weekly_lesson(
        episode,
        lesson,
        run_date=date(2026, 6, 4),
        start_date=date(2026, 6, 4),
        total_days=5,
    )
    assert 'Podcasts 앱</a>' in result
    assert result.count('href="https://podcasts.apple.com/us/podcast/example/id290783428?i=123"') == 2
    print("PASS: Podcasts 앱 링크 episode URL 사용")


def test_weekly_lesson_day_display_skips_weekends():
    lesson = {"day": 7, "vocabulary": [], "expressions": []}
    result = _format_weekly_lesson(
        SAMPLE_EPISODE,
        lesson,
        run_date=date(2026, 6, 2),
        start_date=date(2026, 5, 27),
        total_days=5,
    )
    assert "Day 5/5" in result
    print("PASS: 주간 학습 회차 표시 주말 제외")


if __name__ == "__main__":
    tests = [
        test_translation_in_message,
        test_explanation_still_present,
        test_no_translation_field_graceful,
        test_message_split,
        test_html_escape,
        test_example_sentence_fallback,
        test_weekly_lesson_format,
        test_weekly_lesson_uses_podcast_episode_url_for_listen_link,
        test_weekly_lesson_uses_episode_url_for_podcasts_app_link,
        test_weekly_lesson_day_display_skips_weekends,
    ]
    failed = 0
    for t in tests:
        try:
            t()
        except AssertionError as e:
            print(f"FAIL: {t.__name__} — {e}")
            failed += 1
        except Exception as e:
            print(f"ERROR: {t.__name__} — {e}")
            failed += 1

    print(f"\n{'='*40}")
    print(f"결과: {len(tests) - failed}/{len(tests)} 통과")
    sys.exit(1 if failed else 0)
