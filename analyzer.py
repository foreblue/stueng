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


def analyze(episode: Episode) -> tuple[dict, str]:
    """에피소드를 분석하여 (결과 dict, 실패사유) 튜플을 반환한다.

    성공 시 실패사유는 빈 문자열, 실패 시 결과는 빈 dict.
    """
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
    errors = []
    for name, call in backends:
        logger.info("LLM 호출 중... (backend: %s, 전사문 %d자)", name, len(prompt))
        try:
            raw = call()
            logger.info("LLM 응답 수신 (backend: %s, %d자)", name, len(raw))
            break
        except requests.exceptions.ConnectionError:
            reason = f"[{name}] 프록시 연결 실패 ({config.PROXY_URL})"
            errors.append(reason)
            logger.warning("%s — 다음 backend 시도", reason)
        except requests.exceptions.Timeout:
            reason = f"[{name}] 타임아웃 (120초 초과)"
            errors.append(reason)
            logger.warning("%s — 다음 backend 시도", reason)
        except requests.exceptions.HTTPError as e:
            reason = f"[{name}] HTTP 오류 {e.response.status_code}"
            errors.append(reason)
            logger.warning("%s — 다음 backend 시도", reason)
        except Exception as e:
            reason = f"[{name}] {e}"
            errors.append(reason)
            logger.warning("%s — 다음 backend 시도", reason)

    if not raw:
        logger.error("모든 backend 실패. 빈 결과 반환")
        return {}, "\n".join(errors)

    try:
        return _parse_json(raw), ""
    except json.JSONDecodeError as e:
        logger.error("JSON 파싱 실패: %s\n응답: %s", e, raw[:300])
        return {}, f"LLM 응답 JSON 파싱 실패: {e}"


if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO)
    from sources.upfirst import UpFirst

    ep = UpFirst().fetch_latest()
    if not ep:
        print("No episode found")
        sys.exit(1)

    print(f"Analyzing: {ep.title}")
    result, reason = analyze(ep)
    if reason:
        print(f"Failure: {reason}")
    print(json.dumps(result, ensure_ascii=False, indent=2))
