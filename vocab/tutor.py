"""로컬 워커 — 서버가 쌓아 둔 일감을 LLM 으로 처리해 돌려놓는다.

서버에는 LLM 자격증명이 없다. 분석에 쓰는 프록시(`localhost:9000`)는 이 맥북에만
있고, 그 구조를 유지하는 편이 비용도 0원이고 서버도 가볍다. 그래서 LLM 이 필요한 일은
서버가 "해 달라" 고 쌓아 두고, 이 워커가 가져가 처리한 뒤 결과만 밀어 넣는다.

세 가지를 처리한다.

- **뜻 채우기** — 수업 PC 가 후보만 뽑아 보낸 어휘에 뜻을 쓴다. 후보 선정은 빈도 규칙이라
  그 PC 에서도 돌지만, 뜻은 LLM 이 필요하다. 뜻이 없는 동안 그 어휘는 카드가 되지 않으므로
  이 워커가 며칠 밀려도 빈 문제가 나가지는 않는다 — 새 카드가 늦어질 뿐이다.
- **작문 첨삭** — 주 1회 과제로 쓴 글을 고쳐 준다.
- **막힌 카드의 기억술** — 여러 번 놓친 카드에만 붙인다. 니모닉은 초기 회상에는 강하나
  시간이 지나면 이점이 감쇠하고 자동 생성 품질의 편차가 크므로, 기본 장치가 아니라
  탈출구로만 쓴다.

    python -m vocab.tutor            # 한 번 돌고 끝
    python -m vocab.tutor --dry-run  # 무엇을 처리할지만 본다
"""

from __future__ import annotations

import argparse
import logging
import sys

import requests

import analyzer
import config

logger = logging.getLogger(__name__)

TIMEOUT = 60

#: 한 번에 처리할 개수 상한. LLM 호출이 건당 수십 초라 무한정 돌면 곤란하다.
MAX_PER_RUN = 5

#: 뜻은 이 배수만큼 더 처리한다. 첨삭·기억술과 달리 응답이 두 줄이라 빠르고, 이게 밀리면
#: 원격에서 들어온 어휘가 카드로 나오지 못한 채 쌓인다.
GLOSS_MULTIPLIER = 4

COMPOSITION_PROMPT = """You are an English tutor for a Korean-speaking adult learner.

They were asked to write a short paragraph using these words they are currently studying:
{words}

Here is what they wrote:
---
{text}
---

Give feedback in Korean, in this exact structure and nothing else:

## 고친 글
(their paragraph rewritten naturally, keeping their content and voice — not a different essay)

## 무엇을 고쳤나
(3-5 bullets. Each: what they wrote → the natural version → why, in one line.
Focus on errors that would recur, not one-off typos.)

## 목표 단어 사용
(For each target word: did they use it correctly and naturally? One line each.
If a word was forced in awkwardly, say so and show a natural use.)

## 다음에 해볼 것
(One concrete thing to try in next week's writing.)

Be specific and warm. Do not pad. Write all commentary in natural Korean; keep English
examples in English."""

MNEMONIC_PROMPT = """You are helping a Korean-speaking adult remember an English {kind} \
they keep forgetting.

{kind}: {display}
뜻: {meaning}
실제로 만난 문장:
{examples}

Make ONE memory hook, in Korean, at most three sentences.

Prefer, in this order:
1. A morphological hook — real prefix/root/suffix, or a related English word they likely know.
2. A keyword hook — a Korean or English word that sounds like part of it, tied to the meaning
   by a concrete image.
3. A collocation hook — the phrase it almost always appears in.

Rules: the hook must connect to THIS meaning, not just the sound. No invented etymology —
if you are not sure a root is real, use a different kind of hook. Return only the hook text,
no headings, no preamble."""


GLOSS_PROMPT = """You are making a vocabulary card for a Korean-speaking adult learner \
who is studying English.

{kind}: {display}
Sentences where they actually met it:
{examples}

Write the gloss for THIS use — the sense that fits the sentences above, not every sense the
dictionary lists. Return exactly two lines, nothing else:

뜻: (Korean meaning, at most 15 words. No part-of-speech label, no romanization.)
영영: (a short English definition, at most 15 words)

If the sentences are too garbled to tell what it means, return exactly: 판단불가"""


class TutorError(RuntimeError):
    pass


def _require() -> str:
    if not config.VOCAB_SERVER_URL:
        raise TutorError("VOCAB_SERVER_URL 이 설정되지 않았습니다.")
    if not config.VOCAB_INGEST_TOKEN:
        raise TutorError("VOCAB_INGEST_TOKEN 이 설정되지 않았습니다.")
    return config.VOCAB_SERVER_URL


def _headers() -> dict[str, str]:
    return {"X-Ingest-Token": config.VOCAB_INGEST_TOKEN}


def fetch_tasks() -> dict:
    base = _require()
    response = requests.get(f"{base}/api/tasks", headers=_headers(), timeout=TIMEOUT)
    if not response.ok:
        raise TutorError(f"일감을 받지 못했습니다 ({response.status_code}): {response.text[:200]}")
    return response.json()


def submit_results(glosses: list[dict], mnemonics: list[dict], feedback: list[dict]) -> dict:
    if not glosses and not mnemonics and not feedback:
        return {"glosses": 0, "mnemonics": 0, "feedback": 0}

    base = _require()
    response = requests.post(
        f"{base}/api/tasks",
        json={"glosses": glosses, "mnemonics": mnemonics, "feedback": feedback},
        headers=_headers(),
        timeout=TIMEOUT,
    )
    if not response.ok:
        raise TutorError(f"결과를 넣지 못했습니다 ({response.status_code}): {response.text[:200]}")
    return response.json()


def write_feedback(task: dict) -> str | None:
    words = "\n".join(
        f"- {w['display']} ({w['meaning_kr']})" for w in task.get("words", [])
    )
    prompt = COMPOSITION_PROMPT.format(words=words, text=task["text"])
    raw, reason = analyzer.complete(prompt, label="작문 첨삭")
    if not raw:
        logger.warning("첨삭 실패 (%s): %s", task["week_start"], reason)
        return None
    return raw.strip()


def write_gloss(task: dict) -> dict | None:
    """후보 어휘 하나의 뜻. 실패하면 None — 다음 실행에서 다시 잡힌다."""
    examples = "\n".join(f'  "{s}"' for s in task.get("examples") or []) or "  (없음)"
    kind = "expression" if task.get("kind") == "expression" else "word"
    prompt = GLOSS_PROMPT.format(kind=kind, display=task["display"], examples=examples)
    raw, reason = analyzer.complete(prompt, label="뜻 채우기")
    if not raw:
        logger.warning("뜻 생성 실패 (%s): %s", task["display"], reason)
        return None

    meaning_kr = meaning_en = ""
    for line in raw.strip().splitlines():
        line = line.strip()
        if line.startswith("뜻:"):
            meaning_kr = line.split(":", 1)[1].strip()
        elif line.startswith("영영:"):
            meaning_en = line.split(":", 1)[1].strip()

    if not meaning_kr:
        # "판단불가" 이거나 형식을 벗어난 응답. 지어낸 뜻을 넣느니 비워 두는 편이 낫다.
        logger.warning("뜻을 읽지 못해 건너뜁니다 (%s): %s", task["display"], raw.strip()[:60])
        return None

    return {
        "word_id": task["word_id"],
        "meaning_kr": meaning_kr[:500],
        "meaning_en": meaning_en[:500] or None,
    }


def make_mnemonic(task: dict) -> str | None:
    examples = "\n".join(f'  "{s}"' for s in task.get("examples") or []) or "  (없음)"
    kind = "expression" if task.get("kind") == "expression" else "word"
    prompt = MNEMONIC_PROMPT.format(
        kind=kind, display=task["display"], meaning=task["meaning_kr"], examples=examples
    )
    raw, reason = analyzer.complete(prompt, label="기억술")
    if not raw:
        logger.warning("기억술 생성 실패 (%s): %s", task["display"], reason)
        return None

    text = raw.strip()
    # 프롬프트를 무시하고 장문을 뱉는 모델이 있다. 카드 아래 한 칸에 들어갈 분량이 아니면 버린다.
    if len(text) > 400:
        logger.warning("기억술이 너무 길어 버립니다 (%s, %d자)", task["display"], len(text))
        return None
    return text


def run(*, dry_run: bool = False, limit: int = MAX_PER_RUN) -> dict:
    tasks = fetch_tasks()
    pending_feedback = tasks.get("compositions", [])[:limit]
    pending_mnemonics = tasks.get("mnemonics", [])[:limit]
    # 뜻은 한 건이 짧아 여러 개를 돌려도 부담이 적고, 밀리면 새 카드가 아예 안 나온다.
    pending_glosses = tasks.get("glosses", [])[: limit * GLOSS_MULTIPLIER]

    if dry_run:
        return {
            "dry_run": True,
            "compositions": [t["week_start"] for t in pending_feedback],
            "mnemonics": [t["display"] for t in pending_mnemonics],
            "glosses": [t["display"] for t in pending_glosses],
        }

    glosses = []
    for task in pending_glosses:
        item = write_gloss(task)
        if item:
            glosses.append(item)
            logger.info("뜻 완료: %s — %s", task["display"], item["meaning_kr"])

    feedback = []
    for task in pending_feedback:
        text = write_feedback(task)
        if text:
            feedback.append({"composition_id": task["composition_id"], "text": text})
            logger.info("첨삭 완료: %s 주", task["week_start"])

    mnemonics = []
    for task in pending_mnemonics:
        text = make_mnemonic(task)
        if text:
            mnemonics.append({"word_id": task["word_id"], "text": text})
            logger.info("기억술 완료: %s", task["display"])

    applied = submit_results(glosses, mnemonics, feedback)
    applied["attempted"] = {
        "feedback": len(pending_feedback),
        "mnemonics": len(pending_mnemonics),
        "glosses": len(pending_glosses),
    }
    return applied


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="서버의 LLM 일감을 로컬에서 처리한다")
    parser.add_argument("--dry-run", action="store_true", help="처리하지 않고 목록만 본다")
    parser.add_argument("--limit", type=int, default=MAX_PER_RUN, help="한 번에 처리할 최대 개수")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    try:
        result = run(dry_run=args.dry_run, limit=args.limit)
    except TutorError as e:
        logger.error("%s", e)
        return 1
    except requests.RequestException as e:
        logger.error("서버에 연결하지 못했습니다: %s", e)
        return 1

    if result.get("dry_run"):
        print(f"뜻 대기 {len(result['glosses'])}건: {', '.join(result['glosses']) or '없음'}")
        print(f"첨삭 대기 {len(result['compositions'])}건: {', '.join(result['compositions']) or '없음'}")
        print(f"기억술 대기 {len(result['mnemonics'])}건: {', '.join(result['mnemonics']) or '없음'}")
    else:
        attempted = result["attempted"]
        print(
            f"뜻 {result['glosses']}/{attempted['glosses']}건, "
            f"첨삭 {result['feedback']}/{attempted['feedback']}건, "
            f"기억술 {result['mnemonics']}/{attempted['mnemonics']}건 반영"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
