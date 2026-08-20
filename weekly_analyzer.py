import json
import logging
import re

import requests

import analyzer
from sources.base import Episode

logger = logging.getLogger(__name__)
STUDY_DAYS = 5

PROMPT_TEMPLATE = """You are an English teacher helping a Korean-speaking adult learn English through NPR podcasts.

Podcast: "{source}" — "{title}"

---
{transcript}
---

Create a 5-day weekday study plan from this transcript.

{vocabulary_instruction}

For each day:
- vocabulary: exactly 3 words for that day.
- expressions: exactly 3 useful idioms, collocations, or fixed phrases from the transcript.

Prefer items that are useful in everyday English, business, economics, policy, or news conversations.
Avoid repeating the same word or expression across different days unless it is central to the episode.

Use this exact JSON shape and key names:
{{
  "lessons": [
    {{
      "day": 1,
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
          "example": "exact quote from the transcript if available"
        }}
      ]
    }}
  ]
}}

Return exactly 5 lessons. All Korean text must be natural, fluent Korean.
Examples must be exact quotes from the transcript.

Return ONLY valid JSON with no markdown, no explanation, no code fences."""


def _parse_json(raw: str) -> dict:
    raw = raw.strip()
    raw = re.sub(r"^```(?:json)?\s*", "", raw)
    raw = re.sub(r"\s*```$", "", raw)
    return json.loads(raw)


def _normalize_plan(plan: dict) -> dict:
    lessons = plan.get("lessons", [])
    if not isinstance(lessons, list):
        lessons = []

    normalized = []
    for i, lesson in enumerate(lessons[:STUDY_DAYS], 1):
        if not isinstance(lesson, dict):
            lesson = {}
        normalized.append(
            {
                "day": int(lesson.get("day") or i),
                "vocabulary": lesson.get("vocabulary", [])[:3],
                "expressions": lesson.get("expressions", [])[:3],
            }
        )

    return {"lessons": normalized}


def _validate_plan(plan: dict) -> None:
    lessons = plan.get("lessons", [])
    if len(lessons) != STUDY_DAYS:
        raise ValueError(f"lessons는 {STUDY_DAYS}개여야 합니다: {len(lessons)}개")

    for lesson in lessons:
        day = lesson.get("day")
        vocab_count = len(lesson.get("vocabulary", []))
        expr_count = len(lesson.get("expressions", []))
        if vocab_count != 3 or expr_count != 3:
            raise ValueError(f"day {day}: vocabulary/expression 개수가 3개가 아닙니다 ({vocab_count}/{expr_count})")


def analyze_weekly(episode: Episode) -> tuple[dict, str]:
    prompt = PROMPT_TEMPLATE.format(
        source=episode.source_name,
        title=episode.title,
        transcript=episode.transcript,
        vocabulary_instruction=analyzer.weekly_vocabulary_instruction(episode.transcript),
    )

    errors = []
    for model in analyzer.config.AI_MODELS:
        backend = analyzer._backend_label(model)
        logger.info("주간 LLM 호출 중... (backend: %s, model: %s, 전사문 %d자)", backend, model, len(prompt))
        try:
            raw = analyzer._call_chat(prompt, model)
            logger.info("주간 LLM 응답 수신 (backend: %s, model: %s, %d자)", backend, model, len(raw))
            plan = _normalize_plan(_parse_json(raw))
            _validate_plan(plan)
            return plan, ""
        except requests.exceptions.ConnectionError:
            reason = f"[{backend}] 프록시 연결 실패 ({analyzer.config.PROXY_URL})"
        except requests.exceptions.Timeout:
            reason = f"[{backend}] 타임아웃 ({analyzer.config.AI_TIMEOUT_SEC}초 초과, model: {model})"
        except requests.exceptions.HTTPError as e:
            status = e.response.status_code if e.response is not None else "unknown"
            reason = f"[{backend}] HTTP 오류 {status} (model: {model}){analyzer._response_excerpt(e.response)}"
        except (json.JSONDecodeError, KeyError, TypeError) as e:
            reason = f"[{backend}] 주간 분석 JSON 파싱 실패 (model: {model}): {e}"
        except Exception as e:
            reason = f"[{backend}] {e}"

        errors.append(reason)
        logger.warning("%s — 다음 backend 시도", reason)

    logger.error("모든 backend 주간 분석 실패")
    return {}, "\n".join(errors)
