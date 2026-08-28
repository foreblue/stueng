"""수업 PC 에서 도는 추출기 — 전사문에서 후보를 뽑아 서버로 바로 보낸다.

수업은 이 맥이 아니라 다른 PC 에서 돌아간다. 녹음 파일을 맥으로 옮겨 처리하는 길
(`skills/english-class/scripts/import.sh`) 과 별개로, **그 PC 가 단어까지 뽑아 서버에
직접 밀어 넣는** 길이 여기다. 맥에서 아무 명령도 돌리지 않아도 어휘가 쌓인다.
(서버 컨테이너 자체는 이 맥에서 도니 맥이 꺼져 있으면 안 된다. 없어지는 것은 손이지
장비가 아니다.)

옮길 수 있는 것과 없는 것이 갈린다.

- **후보 선정은 옮겨진다.** 규칙(빈도 밴드 + 전사문 내 반복 횟수)이라 재현 가능하고,
  `vocab.candidates` 를 그대로 쓴다. 팟캐스트와 같은 코드가 같은 기준으로 고른다.
- **뜻은 못 옮긴다.** LLM 자격증명이 맥에만 있다. 그래서 뜻을 비워서 보내고,
  서버는 그 어휘를 카드로 만들지 않은 채 `/api/tasks` 에 쌓는다. 맥에서
  `python -m vocab.tutor` 가 돌 때 채워지고, 그때부터 출제된다.

    python -m vocab.remote --transcript transcript.md
    python -m vocab.remote --transcript transcript.md --dry-run
    python -m vocab.remote --audio class.mkv          # faster-whisper 가 있으면 전사부터

이 PC 에 필요한 것은 `wordfreq simplemma requests python-dotenv` 넷뿐이다. sqlalchemy 도
DB 도 필요 없다 — 저장소는 서버에 있고, 여기서는 만들지 않는다.

**닿는 길.** 게이트웨이(Traefik)가 이미 `stueng.deepheart.duckdns.org` 를 정식 인증서로
서비스하고 있고, 443 은 LAN 에도 열려 있다. 공유기가 헤어핀 NAT 을 지원하지 않아 집
안에서 공인 IP 로는 못 돌아오지만, **이름을 LAN IP 로 풀어 주면 그대로 닿는다.** 수업
PC 의 hosts 파일에 한 줄이면 되고, 인증서 검증도 정상으로 통과한다.

    192.168.45.93  stueng.deepheart.duckdns.org
    VOCAB_SERVER_URL=https://stueng.deepheart.duckdns.org

TLS 검증을 끄거나 컨테이너를 LAN 에 새로 여는 길은 쓰지 않는다. 전자는 토큰을 중간자
앞에 내놓는 것이고, 후자는 이미 있는 게이트웨이를 두고 노출 면을 하나 더 만드는 것이다.

**토큰은 좁은 것을 쓴다.** `VOCAB_INGEST_TOKEN` 은 `/api/export`(DB 전체 덤프)까지 여는
열쇠다. 수업 PC 에는 `/ingest` 에서만 통하는 `VOCAB_REMOTE_TOKEN` 을 준다. 자세한 것은
`skills/english-class/SKILL.md` 의 "수업 PC 에서 바로 보내기".
"""

from __future__ import annotations

import argparse
import datetime as dt
import logging
import os
import re
import sys

import requests

from . import banding, candidates

logger = logging.getLogger(__name__)

# `models` 를 가져오지 않는다. 그쪽은 sqlalchemy 를 끌고 오는데 이 PC 에는 DB 가 없다.
# 필요한 것은 문자열 셋뿐이라 적어 두고, 어긋나면 test_vocab_remote 가 잡는다.
KIND_WORD = "word"
KIND_EXPRESSION = "expression"
SOURCE_CLASS = "class"

#: 한 수업에서 뽑을 후보 수. 수업 하나는 팟캐스트 한 편보다 어휘 밀도가 낮고, 하루
#: 새 카드가 10장이라 그보다 조금 많게 잡는다.
DEFAULT_LIMIT = 12

BATCH = 200
TIMEOUT = 30

#: 예문으로 쓸 문장 길이. 너무 짧으면 문맥이 없고, 너무 길면 카드에 안 들어간다.
MIN_SENTENCE = 20
MAX_SENTENCE = 240

#: 한 어휘에 붙일 예문 수. 서버가 (어휘, 문장) 으로 합치므로 겹쳐도 안전하다.
MAX_EXAMPLES = 3

#: `merge_transcript.py` 가 붙이는 화자·시각 머리표. 예문에 들어가면 안 된다.
#: `merge_transcript.py` 는 **Tutor** `00:12:30` 본문 형태로 쓴다. 시각을 감싼 백틱까지
#: 같이 떼지 않으면 예문 앞에 `00:12:30` 이 남는다.
_STAMP = r"[`\[(]?\d{1,2}:\d{2}(?::\d{2})?[`\])]?"
SPEAKER_RE = re.compile(
    r"^\s*(?:[-*]\s*)?(?:\*\*)?(?:Tutor|Me|화자불명)(?:\*\*)?\s*"
    rf"(?:{_STAMP})?\s*[:\-]?\s*",
    re.IGNORECASE,
)
TIMESTAMP_RE = re.compile(rf"^\s*{_STAMP}\s*")
SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+")
WORD_RE = re.compile(r"[A-Za-z]+(?:['’][A-Za-z]+)?")


class RemoteError(RuntimeError):
    pass


def _server() -> tuple[str, str]:
    """서버 주소와 토큰. `config` 를 거치지 않고 환경변수를 직접 본다.

    수업 PC 에는 이 저장소의 `.env` 가 없다. 있어야 할 값이 둘뿐이라 그 PC 의
    환경변수에 두는 편이 낫고, 없을 때 무엇이 없는지 정확히 말해 준다.

    토큰은 좁은 것(`VOCAB_REMOTE_TOKEN`)을 먼저 본다. 넓은 것으로도 돌아가긴 하지만
    이 PC 에 둘 이유가 없는 권한이라, 그때는 로그로 알린다.
    """
    url = os.environ.get("VOCAB_SERVER_URL", "").rstrip("/")
    token = os.environ.get("VOCAB_REMOTE_TOKEN", "")
    if not token:
        token = os.environ.get("VOCAB_INGEST_TOKEN", "")
        if token:
            logger.warning(
                "VOCAB_REMOTE_TOKEN 이 없어 VOCAB_INGEST_TOKEN 으로 보냅니다 — "
                "그 토큰은 /api/export(DB 전체 덤프)까지 엽니다. 좁은 토큰을 쓰세요."
            )

    missing = []
    if not url:
        missing.append("VOCAB_SERVER_URL")
    if not token:
        missing.append("VOCAB_REMOTE_TOKEN")
    if missing:
        raise RemoteError(f"환경변수가 없습니다: {', '.join(missing)}")

    # 평문 HTTP 로 토큰을 보내지 않는다. 루프백은 예외 — 나가는 트래픽이 아니다.
    if not url.startswith("https://") and not url.startswith("http://127.0.0.1"):
        raise RemoteError(
            f"HTTPS 로 보내야 합니다 (지금: {url}). 토큰이 평문으로 나갑니다. "
            "hosts 파일에 '192.168.45.93 stueng.deepheart.duckdns.org' 를 넣고 "
            "https 주소를 쓰세요."
        )
    return url, token


def clean_lines(transcript: str) -> list[str]:
    """화자·시각 머리표를 떼어 낸 본문 줄."""
    lines = []
    for raw in transcript.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        line = SPEAKER_RE.sub("", line, count=1)
        line = TIMESTAMP_RE.sub("", line, count=1)
        line = line.strip()
        if line:
            lines.append(line)
    return lines


def sentences(transcript: str) -> list[str]:
    out = []
    for line in clean_lines(transcript):
        for piece in SENTENCE_SPLIT_RE.split(line):
            piece = piece.strip()
            if MIN_SENTENCE <= len(piece) <= MAX_SENTENCE:
                out.append(piece)
    return out


def examples_for(word: str, pool: list[str]) -> list[str]:
    """그 어휘가 실제로 나온 문장. 지어내지 않는다 — 없으면 빈 목록이다.

    표면형이 아니라 원형으로 맞춘다. 전사문에 'negotiating' 만 있어도 후보는
    'negotiate' 로 올라오기 때문이다.
    """
    target = banding.lemma(word)
    found = []
    for sentence in pool:
        if any(banding.lemma(t) == target for t in WORD_RE.findall(sentence)):
            found.append(sentence)
            if len(found) >= MAX_EXAMPLES:
                break
    return found


def build(
    transcript: str,
    *,
    occurred_on: dt.date,
    tutor: str = "",
    topic: str = "",
    limit: int = DEFAULT_LIMIT,
) -> list[dict]:
    """전사문 → `/ingest` 페이로드. 뜻은 비운다."""
    words = candidates.for_episode(transcript, limit=limit)
    if not words:
        return []

    # 노트 경로(`parse_class_note`)와 같은 제목 규칙. 같은 수업이 두 경로로 들어와도
    # 출처가 한 줄로 보이고, 표제어가 겹치면 서버가 합친다.
    title = " · ".join(
        x for x in [f"영어수업 {occurred_on:%Y-%m-%d}", tutor, topic] if x
    )
    pool = sentences(transcript)

    entries = []
    for word in words:
        found = examples_for(word, pool) or [None]
        for sentence in found:
            entries.append({
                "display": word,
                # 서버가 표기 형태로 다시 정한다(`collect.Entry.__post_init__`). 여기서
                # 맞춰 보내는 것은 페이로드를 혼자 읽어도 말이 되게 하려는 것뿐이다.
                "kind": KIND_EXPRESSION if banding.is_phrase(word) else KIND_WORD,
                "meaning_kr": "",
                "source_kind": SOURCE_CLASS,
                "source_title": title,
                "occurred_on": occurred_on.isoformat(),
                "sentence": sentence,
            })
    return entries


def send(entries: list[dict]) -> dict:
    url, token = _server()
    totals = {"received": 0, "words_created": 0, "words_updated": 0, "occurrences_created": 0}
    last: dict = {}
    for start in range(0, len(entries), BATCH):
        chunk = entries[start : start + BATCH]
        response = requests.post(
            f"{url}/ingest",
            json={"entries": chunk},
            headers={"X-Ingest-Token": token},
            timeout=TIMEOUT,
        )
        if not response.ok:
            raise RemoteError(f"ingest 실패 ({response.status_code}): {response.text[:200]}")
        last = response.json()
        for key in totals:
            totals[key] += last.get(key, 0)
    totals["total_words"] = last.get("total_words", 0)
    return totals


def transcribe(path: str, *, model: str = "small") -> str:
    """faster-whisper 로 전사한다. 맥의 mlx-whisper 는 Apple Silicon 전용이라 못 쓴다."""
    try:
        from faster_whisper import WhisperModel
    except ImportError:
        raise RemoteError(
            "faster-whisper 가 없습니다 — pip install faster-whisper. "
            "또는 전사문을 직접 넘기세요: --transcript <파일>"
        )

    logger.info("전사 중 (%s, 모델 %s) — 수업 하나면 몇 분 걸립니다", path, model)
    whisper = WhisperModel(model, compute_type="int8")
    segments, _ = whisper.transcribe(path, language="en", condition_on_previous_text=False)
    return "\n".join(segment.text.strip() for segment in segments)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="수업 전사문에서 어휘 후보를 뽑아 복습 서버로 보낸다"
    )
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--transcript", help="전사문 파일 (.md/.txt)")
    source.add_argument("--audio", help="녹음 파일 — faster-whisper 로 여기서 전사한다")
    parser.add_argument("--date", help="수업 날짜 YYYY-MM-DD (기본: 오늘)")
    parser.add_argument("--tutor", default="", help="튜터 이름")
    parser.add_argument("--topic", default="", help="수업 주제")
    parser.add_argument("--limit", type=int, default=DEFAULT_LIMIT, help="뽑을 후보 수")
    parser.add_argument("--model", default="small", help="faster-whisper 모델 (--audio 일 때)")
    parser.add_argument("--dry-run", action="store_true", help="보내지 않고 무엇이 갈지만 본다")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    try:
        # 수업 날짜는 그 PC 의 달력 날짜다. UTC 로 잡으면 밤 수업이 다음 날로 밀린다.
        today = dt.datetime.now().astimezone().date()
        occurred_on = dt.date.fromisoformat(args.date) if args.date else today
    except ValueError:
        logger.error("날짜 형식이 잘못됐습니다: %s (YYYY-MM-DD)", args.date)
        return 1

    try:
        if args.audio:
            transcript = transcribe(args.audio, model=args.model)
        else:
            with open(args.transcript, encoding="utf-8") as f:
                transcript = f.read()

        entries = build(
            transcript,
            occurred_on=occurred_on,
            tutor=args.tutor,
            topic=args.topic,
            limit=args.limit,
        )
        if not entries:
            print("후보가 없습니다. 전사문이 비었거나, 나온 낱말이 모두 이미 다루는 것입니다.")
            return 0

        shown = sorted({e["display"] for e in entries})
        print(f"후보 {len(shown)}개 (예문 {len(entries)}건): {', '.join(shown)}")

        if args.dry_run:
            return 0

        result = send(entries)
        print(
            f"전송 {result['received']}건 → 새 어휘 {result['words_created']}, "
            f"보강 {result['words_updated']}, 새 예문 {result['occurrences_created']} "
            f"(서버 총 어휘 {result['total_words']})"
        )
        print("뜻은 비어 있습니다. 맥에서 `python -m vocab.tutor` 가 채우면 출제됩니다.")
        return 0

    except RemoteError as e:
        logger.error("%s", e)
        return 1
    except OSError as e:
        logger.error("파일을 읽지 못했습니다: %s", e)
        return 1
    except requests.RequestException as e:
        logger.error("서버에 연결하지 못했습니다: %s", e)
        return 1


if __name__ == "__main__":
    sys.exit(main())
