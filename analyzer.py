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
- vocabulary: exactly 3 advanced or domain-specific words (skip basic words), prioritized by how frequently they are used in everyday English.
- expressions: exactly 3 useful idioms, collocations, or fixed phrases, prioritized by how frequently they are used in everyday English.
- key_sentences: exactly 3 sentences worth studying for grammar or natural expression, prioritized by how useful the patterns are in everyday English.

Use this exact JSON shape and key names:
{{
  "vocabulary": [
    {{
      "word": "string",
      "definition_kr": "natural Korean definition",
      "definition_en": "English definition",
      "example": "exact quote from the transcript"
    }}
  ],
  "expressions": [
    {{
      "expression": "string",
      "meaning_kr": "natural Korean meaning",
      "usage_note": "Korean usage note"
    }}
  ],
  "key_sentences": [
    {{
      "sentence": "exact quote from the transcript",
      "translation_kr": "natural Korean translation",
      "explanation_kr": "Korean grammatical explanation"
    }}
  ]
}}

All Korean text must be natural, fluent Korean. Examples and sentences must be exact quotes from the transcript.

Return ONLY valid JSON with no markdown, no explanation, no code fences."""


def _backend_label(model: str) -> str:
    if "/" in model:
        provider = model.split("/", 1)[0].lower()
        if provider in ("anthropic", "claude"):
            return "claude"
        if provider == "cursor":
            return "cursor"
        if provider in ("google", "gemini"):
            return "gemini"
        if provider == "codex":
            return "codex"
    if model.startswith("gemini-"):
        return "gemini"
    if model.startswith("gpt-"):
        return "codex"
    if model == "auto":
        return "cursor"
    cursor_prefixes = ("claude-4.6", "claude-opus-4-8", "opus-4.8")
    if model.startswith(cursor_prefixes):
        return "cursor"
    if model.startswith("claude-"):
        return "claude"
    return model


def _response_excerpt(response: requests.Response | None) -> str:
    if response is None:
        return ""

    text = response.text.strip().replace("\n", " ")
    if not text:
        return ""
    if len(text) > 240:
        text = f"{text[:237]}..."
    return f": {text}"


def _call_chat(prompt: str, model: str) -> str:
    url = f"{config.PROXY_URL}/v1/chat/completions"
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
    }
    resp = requests.post(url, json=payload, timeout=config.AI_TIMEOUT_SEC)
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
        (model, _backend_label(model), lambda model=model: _call_chat(prompt, model))
        for model in config.AI_MODELS
    ]

    raw = None
    errors = []
    for model, name, call in backends:
        logger.info("LLM 호출 중... (backend: %s, model: %s, 전사문 %d자)", name, model, len(prompt))
        try:
            raw = call()
            logger.info("LLM 응답 수신 (backend: %s, model: %s, %d자)", name, model, len(raw))
            break
        except requests.exceptions.ConnectionError:
            reason = f"[{name}] 프록시 연결 실패 ({config.PROXY_URL})"
            errors.append(reason)
            logger.warning("%s — 다음 backend 시도", reason)
        except requests.exceptions.Timeout:
            reason = f"[{name}] 타임아웃 ({config.AI_TIMEOUT_SEC}초 초과, model: {model})"
            errors.append(reason)
            logger.warning("%s — 다음 backend 시도", reason)
        except requests.exceptions.HTTPError as e:
            status = e.response.status_code if e.response is not None else "unknown"
            reason = f"[{name}] HTTP 오류 {status} (model: {model}){_response_excerpt(e.response)}"
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
