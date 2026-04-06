import json
import logging
import re

import requests

import config
from sources.base import Episode

logger = logging.getLogger(__name__)

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

_CLAUDE_MODEL  = "claude/claude-sonnet-4-6"
_CURSOR_MODEL  = "cursor/claude-4.6-sonnet-medium-thinking"
_GEMINI_MODEL  = "gemini/gemini-2.5-pro"


def _call_chat(prompt: str, model: str) -> str:
    url = f"{config.PROXY_URL}/v1/chat/completions"
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
    }
    resp = requests.post(url, json=payload, timeout=120)
    resp.raise_for_status()
    data = resp.json()
    return data["choices"][0]["message"]["content"]


def _parse_json(raw: str) -> dict:
    raw = raw.strip()
    raw = re.sub(r"^```(?:json)?\s*", "", raw)
    raw = re.sub(r"\s*```$", "", raw)
    return json.loads(raw)


def analyze(episode: Episode) -> dict:
    prompt = PROMPT_TEMPLATE.format(
        source=episode.source_name,
        title=episode.title,
        transcript=episode.transcript,
    )

    backends = [
        ("claude",  lambda: _call_chat(prompt, _CLAUDE_MODEL)),
        ("cursor",  lambda: _call_chat(prompt, _CURSOR_MODEL)),
        ("gemini",  lambda: _call_chat(prompt, _GEMINI_MODEL)),
    ]

    raw = None
    for name, call in backends:
        logger.info("LLM 호출 중... (backend: %s, 전사문 %d자)", name, len(prompt))
        try:
            raw = call()
            logger.info("LLM 응답 수신 (backend: %s, %d자)", name, len(raw))
            break
        except requests.exceptions.Timeout:
            logger.warning("[%s] 타임아웃 — 다음 backend 시도", name)
        except requests.exceptions.HTTPError as e:
            logger.warning("[%s] HTTP 오류 %s — 다음 backend 시도", name, e.response.status_code)
        except Exception as e:
            logger.warning("[%s] 실패: %s — 다음 backend 시도", name, e)

    if not raw:
        logger.error("모든 backend 실패. 빈 결과 반환")
        return {}

    try:
        return _parse_json(raw)
    except json.JSONDecodeError as e:
        logger.error("JSON 파싱 실패: %s\n응답: %s", e, raw[:300])
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
