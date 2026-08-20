"""전사문에서 학습 후보 단어를 뽑는다.

지금까지는 LLM 에게 "advanced or domain-specific 단어 3개" 를 맡겼다. 그 결과가
private, economic, impact, demand, nuclear 였다 — 성인 학습자가 이미 아는 단어다.
기준이 매번 달라지고 재현도 안 된다.

여기서는 순서를 뒤집는다. **후보는 빈도로 정하고, LLM 은 뜻과 예문만 쓴다.**
선정은 재현 가능한 규칙이 하고, 언어 생성은 언어 모델이 한다.

    후보 = 전사문의 낱말 중
           · core 밴드 (zipf 4.2 미만 = 이미 알 확률이 낮고, 2.0 이상 = 조어가 아님)
           · 고유명사가 아님
           · 이미 학습 중이거나 안다고 표시한 것이 아님
    순위 = 활용도(zipf) 우선. Karatas et al. (2025) 에서 격차가 가장 크게 벌어진 것도
           고빈도 단어였다. 여기에 전사문 안 반복 횟수를 약하게 얹는다.
"""

from __future__ import annotations

import math
import re
from collections import Counter

from . import banding

TOKEN_RE = re.compile(r"[A-Za-z]+(?:['’][A-Za-z]+)?")

#: 세 글자 이하는 기능어이거나 약어다.
MIN_LENGTH = 4

#: 전사문 안 반복 횟수가 순위에 기여하는 정도. 활용도(zipf)를 뒤집지 않을 만큼만.
REPEAT_WEIGHT = 0.3

#: 팟캐스트 정형구. 광고 낭독·제작진 크레딧·구독 안내에서 나오는 말이라 에피소드
#: 내용과 무관하고, 빈도만 보면 core 밴드에 들어와 매번 후보 위쪽을 차지한다.
BOILERPLATE = frozenset("""
podcast podcasts sponsor sponsors sponsored newsletter subscribe subscription
listener listeners episode episodes host hosted hosting produce produced producer
edit edited editor engineer engineering intern fact-check checked
npr spotify apple support supporter donate membership member
transcript audio download stream streaming
""".split())


def _proper_nouns(text: str) -> set[str]:
    """대문자로만 나타나는 낱말. 인명·지명·기관명은 어휘 학습 대상이 아니다.

    문장 첫머리 때문에 대문자가 되는 경우가 있으므로, 소문자로도 나타나면 보통 명사로 본다.
    """
    upper: Counter[str] = Counter()
    lower: Counter[str] = Counter()
    for match in TOKEN_RE.finditer(text):
        token = match.group(0)
        (upper if token[0].isupper() else lower)[token.lower()] += 1
    return {word for word, count in upper.items() if count > lower.get(word, 0)}


def counts(transcript: str) -> Counter[str]:
    """전사문의 낱말별 등장 횟수. 표제어(원형) 기준으로 합친다."""
    tallies: Counter[str] = Counter()
    for match in TOKEN_RE.finditer(transcript):
        token = match.group(0).lower()
        if len(token) < MIN_LENGTH:
            continue
        tallies[banding.lemma(token)] += 1
    return tallies


def from_transcript(
    transcript: str,
    *,
    limit: int = 15,
    exclude: frozenset[str] | set[str] = frozenset(),
) -> list[str]:
    """학습 가치가 높은 순서로 후보 표제어를 돌려준다."""
    if not transcript:
        return []

    skip = {word.lower() for word in exclude} | BOILERPLATE
    proper = _proper_nouns(transcript)

    scored: list[tuple[float, str]] = []
    for word, count in counts(transcript).items():
        if word in skip or word in proper:
            continue
        zipf = banding.zipf(word)
        if zipf is None or not (banding.RARE_MAX <= zipf < banding.KNOWN_MIN):
            continue
        scored.append((zipf + REPEAT_WEIGHT * math.log(count + 1), word))

    scored.sort(key=lambda pair: (-pair[0], pair[1]))
    return [word for _, word in scored[:limit]]


def already_handled() -> set[str]:
    """이미 학습 중이거나 안다고 표시한 표제어.

    저장소를 못 열면 빈 집합을 돌려준다. 후보 선정은 이것 없이도 성립하고,
    저장소가 없다고 파이프라인이 멈추면 안 된다.
    """
    try:
        from sqlalchemy import select

        from .db import make_engine, session_factory
        from .models import Card, Word

        with session_factory(make_engine())() as session:
            rows = session.execute(
                select(Word.headword).where(
                    Word.known.is_(True)
                    | Word.band.in_((banding.BAND_KNOWN, banding.BAND_RARE))
                    | Word.id.in_(select(Card.word_id))
                )
            ).all()
            return {row[0] for row in rows}
    except Exception:  # pragma: no cover - 저장소가 아직 없을 수 있다
        return set()


def for_episode(transcript: str, *, limit: int = 15) -> list[str]:
    """파이프라인이 부르는 진입점. 저장소를 참고해 이미 다루는 단어를 뺀다."""
    return from_transcript(transcript, limit=limit, exclude=already_handled())
