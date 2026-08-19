"""analyzer.py fallback 테스트"""
import os
import sys
from unittest.mock import patch

import requests

sys.path.insert(0, os.path.dirname(__file__))

import analyzer
from sources.base import Episode


SAMPLE_EPISODE = Episode(
    title="Test Episode",
    audio_url="https://example.com/audio.mp3",
    transcript="This is a short transcript.",
    source_name="Up First",
    episode_url="https://example.com/episode",
    duration_sec=600,
    published="2026-06-01",
)


def _http_error(status: int, text: str) -> requests.exceptions.HTTPError:
    response = requests.Response()
    response.status_code = status
    response._content = text.encode("utf-8")
    return requests.exceptions.HTTPError(response=response)


def test_analyze_uses_configured_models_in_order():
    calls = []

    def fake_call(_prompt, model):
        calls.append(model)
        if model == "bad-model":
            raise _http_error(500, "temporary backend failure")
        return '{"vocabulary": [], "expressions": [], "key_sentences": []}'

    with (
        patch.object(analyzer.config, "AI_MODELS", ["bad-model", "good-model"]),
        patch.object(analyzer, "_call_chat", fake_call),
    ):
        result, reason = analyzer.analyze(SAMPLE_EPISODE)

    assert calls == ["bad-model", "good-model"]
    assert result == {"vocabulary": [], "expressions": [], "key_sentences": []}
    assert reason == ""


def test_http_error_reason_includes_response_body():
    def fake_call(_prompt, _model):
        raise _http_error(500, "Authentication required. Please run 'agent login' first.")

    with (
        patch.object(analyzer.config, "AI_MODELS", ["claude-4.6-sonnet-medium-thinking"]),
        patch.object(analyzer, "_call_chat", fake_call),
    ):
        result, reason = analyzer.analyze(SAMPLE_EPISODE)

    assert result == {}
    assert "HTTP 오류 500" in reason
    assert "Authentication required" in reason
    assert "claude-4.6-sonnet-medium-thinking" in reason


def test_default_models_keep_cursor_and_claude():
    assert analyzer.config.DEFAULT_AI_MODELS == [
        "opus-4.8",
        "auto",
        "claude/claude-opus-4-8",
    ]


def test_backend_label_resolves_claude_provider_prefix():
    assert analyzer._backend_label("claude/claude-opus-4-8") == "claude"
    assert analyzer._backend_label("opus-4.8") == "cursor"
    assert analyzer._backend_label("auto") == "cursor"


def test_default_timeout_allows_slow_weekly_models():
    assert analyzer.config.AI_TIMEOUT_SEC == 300


if __name__ == "__main__":
    tests = [
        test_analyze_uses_configured_models_in_order,
        test_http_error_reason_includes_response_body,
        test_default_models_keep_cursor_and_claude,
        test_backend_label_resolves_claude_provider_prefix,
        test_default_timeout_allows_slow_weekly_models,
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
            print(f"ERROR: {test.__name__} — {e}")

    print(f"\n{'='*40}")
    print(f"결과: {len(tests) - failed}/{len(tests)} 통과")
    sys.exit(1 if failed else 0)
