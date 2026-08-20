"""빈도 밴드 판정.

무엇을 외울지 LLM 판단에 맡기면 기준이 매번 달라진다. 여기서는 wordfreq의 Zipf
점수(= log10(십억 단어당 출현 횟수))로 객관적인 순위를 매긴다.

Zipf와 실제 순위의 대응 (wordfreq 영어 토큰 기준):

    rank   500  zipf 5.30      rank  4000  zipf 4.34
    rank  1000  zipf 5.03      rank  5000  zipf 4.22
    rank  2000  zipf 4.71      rank  7000  zipf 4.01
    rank  3000  zipf 4.50      rank  9000  zipf 3.85

Nation (2006): 문어 98% 커버에 8,000~9,000 word family, 상위 2,000~3,000이면 95%.
wordfreq는 word family가 아니라 토큰 단위라 순위가 부풀려져 있다.

임계값은 실제 수집 데이터를 보고 맞췄다. zipf 4.2~4.5 구간에는 settlements, poverty,
guarantee, abroad, researchers 처럼 성인 학습자가 이미 아는 단어가 몰려 있고,
4.0~4.2 구간부터 jurisdiction, monetary, interim, lobbying 처럼 배울 값이 있는 단어가
나온다. 그래서 경계를 4.2 로 둔다.

반대쪽 끝은 거의 비어 있다. zipf 2.0 아래는 malinvestment, overbuilding 정도뿐이고
capitulate, recuse, gerrymander 같은 것은 오히려 배울 가치가 높다. 원래 걱정했던 것과
달리 문제는 어려운 쪽이 아니라 쉬운 쪽이었다.
"""

from __future__ import annotations

import re

import simplemma
from wordfreq import zipf_frequency

#: 이 값 이상이면 이미 알고 있을 확률이 높다고 보고 후순위로 민다. (토큰 상위 ~5천)
KNOWN_MIN = 4.2

#: 이 값 미만은 조어·고유명사에 가까워 투자 대비 수익이 낮다.
RARE_MAX = 2.0

BAND_KNOWN = "known"  # 이미 알 확률 높음
BAND_CORE = "core"  # 학습 가치 최상
BAND_RARE = "rare"  # 조어·초희귀어. 만나면 좋지만 우선순위 낮음

#: 우선 큐에 들어가는 밴드
STUDY_BANDS = (BAND_CORE,)

_SINGLE_WORD_RE = re.compile(r"[a-z]+(?:['’][a-z]+)?")


def normalize(text: str) -> str:
    """표제어 비교용 정규화. 소문자 + 공백 정리 + 양끝 문장부호 제거."""
    text = re.sub(r"\s+", " ", (text or "").strip())
    return text.strip(" \t\"'“”‘’.,!?;:()[]").lower()


def lemma(word: str) -> str:
    """단일 낱말의 원형. 'exports' -> 'export', 'collaborating' -> 'collaborate'."""
    word = normalize(word)
    if not word or " " in word:
        return word
    try:
        return simplemma.lemmatize(word, lang="en").lower()
    except Exception:
        return word


def is_phrase(text: str) -> bool:
    """빈도 추정을 신뢰할 수 없는 다어절 표현인가.

    하이픈으로 이어진 것도 포함한다. wordfreq 는 다어절 문자열에 구성 낱말의 빈도를
    돌려주기 때문에 판정이 무의미해진다 — 'wait-and-see' 가 5.28, 'break the ice' 가
    4.78 로 나온다. 둘 다 실제로는 배워야 하는 표현이다.
    """
    text = normalize(text)
    if " " in text:
        return True
    parts = [p for p in text.split("-") if p]
    return len(parts) > 1


def zipf(text: str) -> float | None:
    """어휘의 Zipf 점수. 표현(다어절)은 신뢰할 수 없으므로 None.

    굴절형과 원형 중 더 흔한 쪽을 택한다. 'exports'만 보고 희귀하다고 판단하면
    안 되고, 반대로 원형만 보면 굴절형이 더 흔한 경우를 놓친다.
    """
    text = normalize(text)
    if not text or is_phrase(text):
        return None
    if not _SINGLE_WORD_RE.fullmatch(text):
        return None

    scores = [zipf_frequency(text, "en"), zipf_frequency(lemma(text), "en")]
    best = max(scores)
    return best if best > 0 else None


def band(text: str) -> str:
    """어휘의 학습 우선순위 밴드.

    다어절 표현(idiom·collocation)은 빈도 추정이 무의미하므로 항상 core 로 둔다.
    구성 낱말이 모두 흔해도 표현 자체는 모를 수 있다 — 'sitting is the new smoking'.
    """
    score = zipf(text)
    if score is None:
        return BAND_CORE
    if score >= KNOWN_MIN:
        return BAND_KNOWN
    if score < RARE_MAX:
        return BAND_RARE
    return BAND_CORE


def classify(text: str) -> tuple[str, float | None]:
    """(밴드, Zipf) 한 번에."""
    return band(text), zipf(text)
