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
    순위 = 전사문 안에서 몇 번 반복됐는가. 동률이면 희귀한 쪽.

밴드가 이미 "배울 값이 있는가" 를 판정한다. 그 안에서까지 빈도로 줄을 세우면 밴드가
잘라낸 4.2 라는 면에 다시 달라붙는다 — 실제로 112개 후보 중 상위 12개가 전부
zipf 4.0~4.19 로 나왔고, adversary·intensify·infiltrate·disarm·wield 같은 알짜는
13위 밖으로 밀려 LLM 에게 가지도 못했다.

밴드 안에서는 **이 에피소드가 무엇에 관한 것인가** 가 결정해야 한다. 반복된 낱말이
그 에피소드의 주제어다. 부수 효과도 좋다 — 반복된 낱말은 예문이 여러 개 딸려오므로
문맥을 돌려 쓸 수 있는 카드가 된다. ceasefire 가 예문 12개를 갖게 된 것이 그 원리다.

(예전 주석은 Karatas et al. (2025) 를 근거로 들었는데 잘못 가져다 썼다. 그 결과는
고빈도 단어가 *최적화된 스케줄로 학습했을 때* 더 크게 향상됐다는 것이지, 무엇을
고를지에 대한 근거가 아니다.)
"""

from __future__ import annotations

import logging
import re
from collections import Counter

from . import banding

logger = logging.getLogger(__name__)

TOKEN_RE = re.compile(r"[A-Za-z]+(?:['’][A-Za-z]+)?")

#: 세 글자 이하는 기능어이거나 약어다.
MIN_LENGTH = 4

#: 팟캐스트 정형구. 광고 낭독·제작진 크레딧·구독 안내에서 나오는 말이라 에피소드
#: 내용과 무관하고, 빈도만 보면 core 밴드에 들어와 매번 후보 위쪽을 차지한다.
BOILERPLATE = frozenset("""
podcast podcasts sponsor sponsors sponsored newsletter subscribe subscription
listener listeners episode episodes host hosted hosting produce produced producer
edit edited editor engineer engineering intern fact-check checked
npr spotify apple support supporter donate membership member
transcript audio download stream streaming
""".split())


def _key(text: str) -> str:
    """비교용 키. 후보 집계와 같은 규칙(원형)으로 맞춘다."""
    text = banding.normalize(text)
    return text if banding.is_phrase(text) else banding.lemma(text)


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

    # 후보는 원형(lemma)을 키로 센다. 제외 목록은 저장소의 표면형이라 그대로 비교하면
    # 'accusations' 를 이미 외우는 중인데 'accusation' 이 새 후보로 다시 올라온다.
    # 실제로 표제어 268개 중 60개가 원형과 다르다.
    skip = {_key(word) for word in exclude} | {_key(word) for word in BOILERPLATE}
    # 후보 키가 원형이므로 고유명사도 같은 규칙으로 맞춘다. 안 그러면
    # 'Emirates' 를 걸러 놓고도 원형 'emirate' 가 후보로 올라온다.
    proper = {_key(word) for word in _proper_nouns(transcript)}

    scored: list[tuple[int, float, str]] = []
    for word, count in counts(transcript).items():
        if word in skip or word in proper:
            continue
        zipf = banding.zipf(word)
        if zipf is None or not (banding.RARE_MAX <= zipf < banding.KNOWN_MIN):
            continue
        scored.append((count, zipf, word))

    # 많이 나온 것 먼저, 같은 횟수면 희귀한 쪽 먼저.
    scored.sort(key=lambda row: (-row[0], row[1], row[2]))
    return [word for _, _, word in scored[:limit]]


def _handled_from_server() -> set[str] | None:
    """서버가 아는 "이미 다루는 표제어". 못 물어보면 None.

    실패는 조용히 넘기지 않고 반드시 남긴다. 게이트웨이와 이 저장소의
    `VOCAB_INGEST_TOKEN` 은 서로 다른 파일에 있어서 어긋나기 쉬운데, 그러면 매일
    아침 401 을 받고 로컬로 물러난 채 이미 외우는 단어를 다시 후보로 올리게 된다.
    로그가 없으면 그 상태가 몇 주씩 이어져도 알 길이 없다.
    """
    try:
        import requests

        import config
    except ImportError as e:
        # 컨테이너에는 config 도 requests 도 없다. 서버 쪽에서 부를 일은 없지만,
        # 부르더라도 파이프라인이 죽는 대신 로컬로 물러나야 한다.
        logger.debug("서버 조회에 필요한 모듈 없음: %s", e)
        return None

    if not config.VOCAB_SERVER_URL or not config.VOCAB_INGEST_TOKEN:
        logger.info("VOCAB_SERVER_URL/TOKEN 이 없어 로컬 저장소로 후보를 거릅니다")
        return None

    try:
        response = requests.get(
            f"{config.VOCAB_SERVER_URL}/api/handled",
            headers={"X-Ingest-Token": config.VOCAB_INGEST_TOKEN},
            timeout=10,
        )
    except Exception as e:
        logger.warning("서버에서 학습 상태를 못 받아 로컬로 물러납니다: %s", e)
        return None

    if not response.ok:
        logger.warning(
            "서버가 학습 상태를 거부했습니다 (%s): %s — 로컬로 물러납니다. "
            "게이트웨이와 이 저장소의 VOCAB_INGEST_TOKEN 이 같은지 확인하세요.",
            response.status_code, response.text[:120],
        )
        return None

    try:
        return {word.lower() for word in response.json()["headwords"]}
    except (ValueError, KeyError, TypeError) as e:
        logger.warning("서버 응답을 해석하지 못했습니다 (%s) — 로컬로 물러납니다", e)
        return None


def _handled_from_local() -> set[str]:
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


def already_handled() -> set[str]:
    """이미 학습 중이거나 안다고 표시한 표제어.

    학습 상태는 서버가 원본이므로 서버에 먼저 묻는다. 카드가 서버로 옮겨간 뒤로
    로컬 Card 테이블은 비어 있어서, 로컬만 보면 이미 외우는 중인 단어를 다시 후보로
    올리게 된다.

    서버가 안 뜨거나 설정이 없으면 로컬로 물러난다. 후보 선정은 이것 없이도 성립하고,
    서버가 잠깐 안 된다고 아침 파이프라인이 멈추면 안 된다.
    """
    from_server = _handled_from_server()
    if from_server is not None:
        return from_server
    return _handled_from_local()


def for_episode(transcript: str, *, limit: int = 15) -> list[str]:
    """파이프라인이 부르는 진입점. 저장소를 참고해 이미 다루는 단어를 뺀다."""
    return from_transcript(transcript, limit=limit, exclude=already_handled())
