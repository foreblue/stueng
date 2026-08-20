import json
import logging
import re

import requests

import config
from sources.base import Episode

logger = logging.getLogger(__name__)

try:
    from vocab import candidates
except ImportError:  # wordfreq/sqlalchemy 가 없는 환경 — 예전 방식으로 돌아간다
    candidates = None

#: 후보를 몇 개까지 제시할지. 3개를 고르게 하면서 선택지를 남긴다.
CANDIDATE_LIMIT = 12

PROMPT_TEMPLATE = """You are an English teacher helping a Korean-speaking adult learn English through NPR podcasts.

Podcast: "{source}" — "{title}"

---
{transcript}
---

Analyze this transcript and extract:
{vocabulary_instruction}
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
      "usage_note": "Korean usage note",
      "example": "exact quote from the transcript containing this expression"
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


#: 후보를 뽑을 수 있을 때. 무엇을 배울지는 빈도가 정하고, LLM 은 설명만 쓴다.
CANDIDATE_INSTRUCTION = """- vocabulary: exactly 3 words chosen FROM THIS LIST ONLY:
{candidates}

  These were selected by corpus frequency: common enough to be worth knowing, rare enough
  that the learner probably does not know them yet, and not already in their deck.
  Pick the 3 that matter most for understanding this episode. Do not add words outside the list."""

#: 후보를 못 뽑았을 때의 폴백. 예전 방식이라 선정 품질이 떨어지지만 파이프라인은 돈다.
FALLBACK_INSTRUCTION = (
    "- vocabulary: exactly 3 advanced or domain-specific words (skip basic words), "
    "prioritized by how frequently they are used in everyday English."
)


def vocabulary_instruction(transcript: str) -> str:
    """어휘 지시문. 후보를 뽑을 수 있으면 그 목록으로 가둔다."""
    if candidates is None:
        logger.warning("vocab.candidates 를 불러오지 못해 예전 방식으로 어휘를 고릅니다")
        return FALLBACK_INSTRUCTION

    picked = candidates.for_episode(transcript, limit=CANDIDATE_LIMIT)
    if len(picked) < 3:
        logger.warning("빈도 기반 후보가 %d개뿐이라 예전 방식으로 돌아갑니다", len(picked))
        return FALLBACK_INSTRUCTION

    logger.info("빈도 기반 후보 %d개: %s", len(picked), ", ".join(picked))
    return CANDIDATE_INSTRUCTION.format(candidates="\n".join(f"    {w}" for w in picked))


#: 주간 계획은 5일 × 3개 = 15개가 필요하다. 고를 여지를 남겨 넉넉히 준다.
WEEKLY_CANDIDATE_LIMIT = 25

#: 후보가 이 아래로 떨어지면 목록으로 가두는 의미가 없다. Planet Money 는 전사문
#: 페이지가 아직 안 올라온 주에 짧은 요약만 잡히는 일이 있어 후보가 10개 안팎으로 준다.
WEEKLY_CANDIDATE_MINIMUM = 8

WEEKLY_CANDIDATE_INSTRUCTION = """Draw every vocabulary item FROM THIS LIST ONLY:
{candidates}

These were selected by corpus frequency: common enough to be worth knowing, rare enough that
the learner probably does not know them yet, and not already in their deck. Spread them across
the five days so that each day holds together thematically. Do not add words outside the list.
{scarcity}"""

WEEKLY_FALLBACK_INSTRUCTION = (
    "Choose advanced, domain-specific, or high-utility words from the transcript."
)

#: 5일 × 3개 = 15개를 채울 수 없을 때. 목록 밖으로 나가는 것보다 반복이 낫다 —
#: 어차피 같은 항목을 다른 날 다시 인출하는 것은 분산 학습 그 자체다.
WEEKLY_SCARCITY_NOTE = (
    "\nThere are fewer than 15 candidates, so the same word may appear on more than one day. "
    "When you repeat one, use a different example sentence and highlight a different sense or "
    "collocation. Never go outside the list to fill a slot."
)


def weekly_vocabulary_instruction(transcript: str) -> str:
    """주간 계획용 어휘 지시문. 하루치보다 많은 후보가 필요하다."""
    if candidates is None:
        logger.warning("vocab.candidates 를 불러오지 못해 예전 방식으로 어휘를 고릅니다")
        return WEEKLY_FALLBACK_INSTRUCTION

    picked = candidates.for_episode(transcript, limit=WEEKLY_CANDIDATE_LIMIT)
    if len(picked) < WEEKLY_CANDIDATE_MINIMUM:
        logger.warning("빈도 기반 후보가 %d개뿐이라 예전 방식으로 돌아갑니다", len(picked))
        return WEEKLY_FALLBACK_INSTRUCTION

    logger.info("빈도 기반 주간 후보 %d개: %s", len(picked), ", ".join(picked))
    return WEEKLY_CANDIDATE_INSTRUCTION.format(
        candidates="\n".join(f"    {word}" for word in picked),
        scarcity=WEEKLY_SCARCITY_NOTE if len(picked) < 15 else "",
    )


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


def complete(prompt: str, *, label: str = "LLM") -> tuple[str, str]:
    """설정된 backend 를 순서대로 시도해 응답 텍스트를 얻는다.

    (응답, 실패사유) 를 돌려준다. 성공하면 실패사유가 빈 문자열이다.
    어휘 분석과 작문 첨삭이 같은 프록시·같은 폴백 순서를 쓰도록 여기로 모았다.
    """
    errors = []
    for model in config.AI_MODELS:
        name = _backend_label(model)
        logger.info("%s 호출 중... (backend: %s, model: %s, %d자)", label, name, model, len(prompt))
        try:
            raw = _call_chat(prompt, model)
            logger.info("%s 응답 수신 (backend: %s, model: %s, %d자)", label, name, model, len(raw))
            return raw, ""
        except requests.exceptions.ConnectionError:
            reason = f"[{name}] 프록시 연결 실패 ({config.PROXY_URL})"
        except requests.exceptions.Timeout:
            reason = f"[{name}] 타임아웃 ({config.AI_TIMEOUT_SEC}초 초과, model: {model})"
        except requests.exceptions.HTTPError as e:
            status = e.response.status_code if e.response is not None else "unknown"
            reason = f"[{name}] HTTP 오류 {status} (model: {model}){_response_excerpt(e.response)}"
        except Exception as e:
            reason = f"[{name}] {e}"

        errors.append(reason)
        logger.warning("%s — 다음 backend 시도", reason)

    logger.error("모든 backend 실패")
    return "", "\n".join(errors)


def analyze(episode: Episode) -> tuple[dict, str]:
    """에피소드를 분석하여 (결과 dict, 실패사유) 튜플을 반환한다.

    성공 시 실패사유는 빈 문자열, 실패 시 결과는 빈 dict.
    """
    prompt = PROMPT_TEMPLATE.format(
        source=episode.source_name,
        title=episode.title,
        transcript=episode.transcript,
        vocabulary_instruction=vocabulary_instruction(episode.transcript),
    )

    raw, reason = complete(prompt, label="LLM")
    if not raw:
        return {}, reason

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
