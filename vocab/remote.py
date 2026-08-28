"""수업 PC 에서 도는 추출기 — 전사문에서 후보를 뽑아 서버로 바로 보낸다.

수업은 이 맥이 아니라 다른 PC 에서 돌아간다. 녹음 파일을 맥으로 옮겨 처리하는 길
(`skills/english-class/scripts/import.sh`) 과 별개로, **그 PC 가 단어까지 뽑아 서버에
직접 밀어 넣는** 길이 여기다. 맥에서 아무 명령도 돌리지 않아도 어휘가 쌓인다.
(서버 컨테이너 자체는 이 맥에서 도니 맥이 꺼져 있으면 안 된다. 없어지는 것은 손이지
장비가 아니다.)

보내는 방법이 둘이고, **노트 쪽이 주(主)다.**

- **`--note` — 수업 노트를 그대로 올린다.** `english-class` 스킬이 그 PC 에서 노트를
  쓰고 있다면 뜻·예문·교정이 이미 다 들어 있다. 그걸 만든 것도 그 PC 의 Claude 다.
  표를 읽는 규칙은 `vocab.notes` 에 있고 맥의 `collect` 와 같은 코드다.
- **`--transcript` / `--audio` — 전사문에서 후보를 뽑는다.** 노트에 담기지 않은,
  전사문에서 반복된 낱말을 줍는 보조 경로다. 선정 규칙(빈도 밴드 + 반복 횟수)은
  `vocab.candidates` 를 그대로 쓴다. 다만 **뜻을 쓸 수는 없다** — 규칙과 달리 뜻은
  LLM 이 필요하고, 이 경로는 사람이 부르는 자리가 아니다. 그래서 뜻을 비워 보내고
  서버는 카드로 만들지 않은 채 쌓아 둔다. 맥에서 `vocab.tutor` 가 채우면 출제된다.

    python -m vocab.remote --note "영어수업 2026-08-28.md"
    python -m vocab.remote --note "영어수업 2026-08-28.md" --dry-run
    python -m vocab.remote --transcript transcript.md
    python -m vocab.remote --audio class.mkv          # faster-whisper 가 있으면 전사부터

이 PC 에 필요한 것은 `wordfreq simplemma requests` 셋뿐이다. sqlalchemy 도 DB 도 필요
없다 — 저장소는 서버에 있고, 여기서는 만들지 않는다.

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
from urllib.parse import urlparse

import requests

from . import banding, candidates, notes

logger = logging.getLogger(__name__)

# `models` 를 가져오지 않는다. 그쪽은 sqlalchemy 를 끌고 오는데 이 PC 에는 DB 가 없다.
# 종류 문자열 둘은 여기 적어 두고, 어긋나면 test_vocab_remote 가 잡는다. 출처 문자열은
# 노트 파서가 이미 들고 있으니 거기서 가져온다.
KIND_WORD = "word"
KIND_EXPRESSION = "expression"
SOURCE_CLASS = notes.SOURCE_CLASS
SOURCE_CORRECTION = notes.SOURCE_CORRECTION

#: 한 수업에서 뽑을 후보 수. 수업 하나는 팟캐스트 한 편보다 어휘 밀도가 낮고, 하루
#: 새 카드가 10장이라 그보다 조금 많게 잡는다.
DEFAULT_LIMIT = 12

BATCH = 200
TIMEOUT = 30

#: 평문 HTTP 를 허용하는 유일한 대상. 나가는 트래픽이 아니라 자기 자신이다.
LOOPBACK = frozenset({"127.0.0.1", "::1", "localhost"})

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
    #
    # 접두사 비교로는 안 된다. `http://127.0.0.1.attacker.example` 가 그대로 통과해
    # 토큰이 남의 호스트로 평문 전송된다. 호스트 이름을 끝까지 보고 판정한다.
    parsed = urlparse(url)
    if parsed.scheme != "https" and parsed.hostname not in LOOPBACK:
        raise RemoteError(
            f"HTTPS 로 보내야 합니다 (지금: {url}). 토큰이 평문으로 나갑니다. "
            "VOCAB_SERVER_URL 을 https 주소로 두세요."
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


def handled() -> set[str]:
    """이미 학습 중이거나 안다고 표시한 표제어. 서버에 직접 묻는다.

    `candidates.already_handled()` 를 쓰지 않는 이유: 그쪽은 `config` 를 거쳐
    `VOCAB_INGEST_TOKEN`(넓은 토큰)으로 묻는데, 이 PC 에는 좁은 토큰만 있다. 게다가
    실패를 `logger.debug` 로만 남기고 로컬 저장소로 물러나는데, 여기엔 저장소가 없어
    **빈 집합**이 된다 — 이미 외우는 단어가 후보 자리를 조용히 차지한다.
    """
    url, token = _server()
    try:
        response = requests.get(
            f"{url}/api/handled", headers={"X-Ingest-Token": token}, timeout=TIMEOUT
        )
    except requests.RequestException as e:
        logger.warning("학습 상태를 못 받았습니다 (%s) — 이미 아는 단어가 섞일 수 있습니다", e)
        return set()

    if not response.ok:
        logger.warning(
            "학습 상태를 거부당했습니다 (%s) — 이미 아는 단어가 섞일 수 있습니다. "
            "VOCAB_REMOTE_TOKEN 이 서버의 값과 같은지 확인하세요.",
            response.status_code,
        )
        return set()

    try:
        words = {w.lower() for w in response.json()["headwords"]}
    except (ValueError, KeyError, TypeError) as e:
        logger.warning("학습 상태를 해석하지 못했습니다 (%s)", e)
        return set()

    logger.info("이미 다루는 표제어 %d개를 후보에서 뺍니다", len(words))
    return words


def build(
    transcript: str,
    *,
    occurred_on: dt.date,
    tutor: str = "",
    topic: str = "",
    limit: int = DEFAULT_LIMIT,
) -> list[dict]:
    """전사문 → `/ingest` 페이로드. 뜻은 비운다."""
    words = candidates.from_transcript(transcript, limit=limit, exclude=handled())
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
        for sentence in examples_for(word, pool) or [None]:
            entries.append(_payload({
                "display": word,
                "meaning_kr": "",  # 뜻은 맥의 `vocab.tutor` 가 채운다
                "sentence": sentence,
                "source_kind": SOURCE_CLASS,
                "source_title": title,
                "occurred_on": occurred_on,
            }))
    return entries


def _payload(row: dict) -> dict:
    """`notes` 가 준 dict 를 `/ingest` 페이로드로. 날짜만 문자열로 바꾼다."""
    display = row["display"]
    return {
        "display": display,
        # 서버가 표기 형태로 다시 정한다(`collect.Entry.__post_init__`). 여기서 맞춰
        # 보내는 것은 페이로드를 혼자 읽어도 말이 되게 하려는 것뿐이다.
        "kind": KIND_EXPRESSION if banding.is_phrase(display) else KIND_WORD,
        "meaning_kr": row.get("meaning_kr", ""),
        "usage_note": row.get("usage_note"),
        "sentence": row.get("sentence"),
        "source_kind": row["source_kind"],
        "source_title": row["source_title"],
        "occurred_on": row["occurred_on"].isoformat(),
    }


def build_from_note(path: str) -> list[dict]:
    """수업 노트 → 페이로드. 뜻이 이미 있으므로 뜻 대기열을 거치지 않는다."""
    rows = notes.parse_file(path)
    if not rows:
        raise RemoteError(
            f"노트에서 어휘를 읽지 못했습니다: {path}\n"
            "파일 이름이 '영어수업 YYYY-MM-DD.md' 형식이어야 하고, "
            "'새 단어·표현' 또는 '교정' 표가 있어야 합니다."
        )
    return [_payload(row) for row in rows]


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
        description="수업 노트나 전사문에서 어휘를 뽑아 복습 서버로 보낸다"
    )
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--note", help="수업 노트 '영어수업 YYYY-MM-DD.md' (뜻·교정 포함)")
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
        if args.note:
            entries = build_from_note(args.note)
        else:
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
        pending = sum(1 for e in entries if not e["meaning_kr"])
        corrections = sum(1 for e in entries if e["source_kind"] == SOURCE_CORRECTION)
        label = "어휘" if args.note else "후보"
        print(f"{label} {len(shown)}개 (항목 {len(entries)}건"
              + (f", 교정 {corrections}건" if corrections else "")
              + f"): {', '.join(shown)}")

        if args.dry_run:
            return 0

        result = send(entries)
        print(
            f"전송 {result['received']}건 → 새 어휘 {result['words_created']}, "
            f"보강 {result['words_updated']}, 새 예문 {result['occurrences_created']} "
            f"(서버 총 어휘 {result['total_words']})"
        )
        if pending:
            print(f"뜻이 빈 것 {pending}건 — 맥에서 `python -m vocab.tutor` 가 채우면 출제됩니다.")
        else:
            print("뜻이 다 있어 바로 출제 대상입니다.")
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
