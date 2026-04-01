import json
import logging
import os
import re
import subprocess

from sources.base import Episode

logger = logging.getLogger(__name__)

JSON_SCHEMA = json.dumps({
    "type": "object",
    "properties": {
        "vocabulary": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "word":          {"type": "string"},
                    "definition_kr": {"type": "string"},
                    "definition_en": {"type": "string"},
                    "example":       {"type": "string"}
                },
                "required": ["word", "definition_kr", "definition_en", "example"]
            }
        },
        "expressions": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "expression": {"type": "string"},
                    "meaning_kr": {"type": "string"},
                    "usage_note": {"type": "string"}
                },
                "required": ["expression", "meaning_kr", "usage_note"]
            }
        },
        "key_sentences": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "sentence":       {"type": "string"},
                    "translation_kr": {"type": "string"},
                    "explanation_kr": {"type": "string"}
                },
                "required": ["sentence", "translation_kr", "explanation_kr"]
            }
        }
    },
    "required": ["vocabulary", "expressions", "key_sentences"]
})

PROMPT_TEMPLATE = """You are an English teacher helping a Korean-speaking adult learn English through NPR podcasts.

Podcast: "{source}" — "{title}"

---
{transcript}
---

Analyze this transcript and extract:
- vocabulary: exactly 3 advanced or domain-specific words (skip basic words), prioritized by how frequently they are used in everyday English. Each with Korean definition, English definition, and exact example sentence from transcript.
- expressions: exactly 3 useful idioms, collocations, or fixed phrases, prioritized by how frequently they are used in everyday English. Each with Korean meaning and usage note.
- key_sentences: exactly 3 sentences worth studying for grammar or natural expression, prioritized by how useful the patterns are in everyday English. Each with a natural Korean translation (translation_kr) and Korean grammatical explanation (explanation_kr).

All Korean text must be natural, fluent Korean. Examples and sentences must be exact quotes from the transcript.

Return ONLY valid JSON with no markdown, no explanation, no code fences."""


def analyze(episode: Episode) -> dict:
    prompt = PROMPT_TEMPLATE.format(
        source=episode.source_name,
        title=episode.title,
        transcript=episode.transcript,
    )

    cmd = [
        "claude",
        "-p", prompt,
        "--output-format", "json",
        "--model", "sonnet",
    ]

    # ANTHROPIC_API_KEY가 환경에 있으면 Claude CLI가 keychain 대신 API 키 인증을 시도함
    # subprocess에서 제거해서 keychain(로그인 세션) 인증을 사용하도록 함
    env = {k: v for k, v in os.environ.items() if k != "ANTHROPIC_API_KEY"}

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=120,
            env=env,
        )
    except subprocess.TimeoutExpired:
        logger.error("claude CLI timed out")
        return {}
    except FileNotFoundError:
        logger.error("claude CLI not found in PATH")
        return {}

    if result.returncode != 0:
        logger.error("claude CLI error (rc=%d): stderr=%s stdout=%s",
                     result.returncode, result.stderr[:300], result.stdout[:300])
        return {}

    try:
        outer = json.loads(result.stdout)
    except json.JSONDecodeError as e:
        logger.error("Failed to parse claude CLI outer JSON: %s\nOutput: %s", e, result.stdout[:300])
        return {}

    # is_error 체크
    if outer.get("is_error"):
        logger.error("claude CLI returned is_error=true: %s", outer.get("result", "")[:300])
        return {}

    inner = outer.get("result", "")
    if not inner:
        logger.error("claude CLI empty result. Full output: %s", result.stdout[:500])
        return {}

    try:
        if isinstance(inner, str):
            # 코드펜스 제거
            inner = re.sub(r"^```(?:json)?\s*", "", inner.strip())
            inner = re.sub(r"\s*```$", "", inner)
            return json.loads(inner)
        return inner
    except json.JSONDecodeError as e:
        logger.error("JSON parse failed: %s\nResult: %s", e, str(inner)[:300])
        return {}


if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO)
    from sources.upfirst import UpFirst

    ep = UpFirst().fetch_latest()
    if not ep:
        print("No episode found")
        sys.exit(1)

    print(f"Analyzing: {ep.title}")
    result = analyze(ep)
    print(json.dumps(result, ensure_ascii=False, indent=2))
