"""로컬 산출물 -> 어휘 저장소.

세 소스를 하나의 어휘 테이블로 모은다.

1. `data/<날짜>.json`                  — Up First 일일 분석
2. `data/weekly/planetmoney-*.json`    — Planet Money 주간 학습 계획
3. `~/mylogs/study/영어수업 *.md`       — english-class 스킬이 만든 수업 노트
                                          (표를 읽는 규칙은 `notes` 에 있다. 수업 PC 의
                                           `remote` 도 같은 코드를 쓴다.)

멱등하다. 같은 파일을 몇 번 돌려도 어휘는 (표제어, 종류) 로, 예문은 (어휘, 문장) 로
합쳐진다. 새 예문이 붙으면 그만큼 문맥이 쌓인다.

    python -m vocab.collect            # 증분 수집
    python -m vocab.collect --rebuild  # 저장소를 비우고 다시 만든다
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import logging
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from . import banding, notes
from .db import REPO_ROOT, create_all, make_engine, session_factory
from .models import (
    Card,
    KIND_EXPRESSION,
    KIND_WORD,
    SOURCE_PLANETMONEY,
    SOURCE_UPFIRST,
    Occurrence,
    Word,
    kind_for,
    sentence_key,
)

logger = logging.getLogger(__name__)

DATA_DIR = REPO_ROOT / "data"
WEEKLY_DIR = DATA_DIR / "weekly"
CLASS_NOTES_DIR = Path.home() / "mylogs" / "study"

DAILY_FILE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}\.json$")

SOURCE_BY_NAME = {
    "up first": SOURCE_UPFIRST,
    "planet money": SOURCE_PLANETMONEY,
}


@dataclass
class Entry:
    """수집된 어휘 한 건. DB 에 넣기 전의 중간 표현."""

    display: str
    kind: str
    meaning_kr: str
    source_kind: str
    source_title: str
    occurred_on: dt.date
    meaning_en: str | None = None
    usage_note: str | None = None
    sentence: str | None = None
    translation_kr: str | None = None
    source_url: str | None = None

    def __post_init__(self) -> None:
        # 어디서 왔든(LLM 분류, 수업 노트 표, /ingest 페이로드) 종류는 형태가 정한다.
        self.kind = kind_for(self.display)

    @property
    def headword(self) -> str:
        return banding.normalize(self.display)


@dataclass
class Stats:
    files: int = 0
    entries: int = 0
    words_created: int = 0
    words_updated: int = 0
    occurrences_created: int = 0
    skipped: list[str] = field(default_factory=list)


# --------------------------------------------------------------------------
# 파서
# --------------------------------------------------------------------------


def _clean(value: object) -> str:
    return re.sub(r"\s+", " ", str(value or "").strip())


def _source_kind(name: str) -> str:
    return SOURCE_BY_NAME.get(_clean(name).lower(), SOURCE_UPFIRST)


def _entries_from_analysis(
    analysis: dict,
    *,
    source_kind: str,
    source_title: str,
    source_url: str | None,
    occurred_on: dt.date,
) -> list[Entry]:
    """`vocabulary` / `expressions` 블록을 Entry 로 편다.

    key_sentences 는 어휘가 아니라 문장이라 여기서 다루지 않는다. 문장 카드가
    필요해지면 별도 종류로 붙인다.
    """
    entries: list[Entry] = []

    for item in analysis.get("vocabulary") or []:
        display = _clean(item.get("word"))
        meaning = _clean(item.get("definition_kr") or item.get("korean_definition"))
        if not display or not meaning:
            continue
        entries.append(
            Entry(
                display=display,
                kind=KIND_WORD,
                meaning_kr=meaning,
                meaning_en=_clean(item.get("definition_en")) or None,
                sentence=_clean(item.get("example") or item.get("example_sentence")) or None,
                source_kind=source_kind,
                source_title=source_title,
                source_url=source_url,
                occurred_on=occurred_on,
            )
        )

    for item in analysis.get("expressions") or []:
        display = _clean(item.get("expression") or item.get("phrase"))
        meaning = _clean(item.get("meaning_kr") or item.get("korean_meaning"))
        if not display or not meaning:
            continue
        entries.append(
            Entry(
                display=display,
                kind=KIND_EXPRESSION,
                meaning_kr=meaning,
                usage_note=_clean(item.get("usage_note")) or None,
                sentence=_clean(item.get("example")) or None,
                source_kind=source_kind,
                source_title=source_title,
                source_url=source_url,
                occurred_on=occurred_on,
            )
        )

    return entries


def parse_daily(path: Path) -> list[Entry]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    analysis = payload.get("analysis") or {}
    if not analysis or "raw" in analysis:
        return []

    occurred_on = dt.date.fromisoformat(payload.get("date") or path.stem)
    return _entries_from_analysis(
        analysis,
        source_kind=_source_kind(payload.get("source", "")),
        source_title=_clean(payload.get("title")),
        source_url=_clean(payload.get("episode_url")) or None,
        occurred_on=occurred_on,
    )


def parse_weekly(path: Path) -> list[Entry]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    episode = payload.get("episode") or {}
    lessons = (payload.get("weekly_analysis") or {}).get("lessons") or []
    if not lessons:
        return []

    study_dates = [dt.date.fromisoformat(d) for d in payload.get("study_dates") or []]
    fallback = dt.date.fromisoformat(payload["start_date"])

    source_kind = _source_kind(episode.get("source", "Planet Money"))
    source_title = _clean(episode.get("title"))
    source_url = _clean(episode.get("episode_url")) or None

    entries: list[Entry] = []
    for lesson in lessons:
        day = int(lesson.get("day") or 1)
        occurred_on = study_dates[day - 1] if day - 1 < len(study_dates) else fallback
        entries.extend(
            _entries_from_analysis(
                lesson,
                source_kind=source_kind,
                source_title=source_title,
                source_url=source_url,
                occurred_on=occurred_on,
            )
        )
    return entries


def parse_class_note(path: Path) -> list[Entry]:
    """수업 노트 → 어휘. 표를 읽는 규칙은 `notes` 에 있다.

    파서를 그쪽에 둔 이유는 수업이 다른 PC 에서 돌기 때문이다. 그 PC 의 `vocab.remote`
    가 같은 노트를 읽어 서버로 바로 보내는데, 규칙이 두 벌이면 어느 한쪽만 고치는 날이
    온다.
    """
    return [Entry(kind=KIND_WORD, **row) for row in notes.parse_file(path)]


# --------------------------------------------------------------------------
# 저장
# --------------------------------------------------------------------------


def upsert(session: Session, entry: Entry, stats: Stats) -> Word | None:
    headword = entry.headword
    if not headword:
        return None

    word = session.scalar(
        select(Word).where(Word.headword == headword, Word.kind == entry.kind)
    )
    if word is None:
        band, zipf = banding.classify(headword)
        word = Word(
            headword=headword,
            display=entry.display,
            kind=entry.kind,
            meaning_kr=entry.meaning_kr,
            meaning_en=entry.meaning_en,
            usage_note=entry.usage_note,
            zipf=zipf,
            band=band,
            first_seen=entry.occurred_on,
        )
        session.add(word)
        session.flush()
        stats.words_created += 1
    else:
        changed = False
        # 처음 만난 날짜가 더 이르면 당긴다. 파일을 순서 없이 읽어도 맞는다.
        if entry.occurred_on < word.first_seen:
            word.first_seen = entry.occurred_on
            changed = True
        # 비어 있던 칸만 채운다. 먼저 들어온 뜻을 덮어쓰지 않는다.
        # meaning_kr 이 여기 있는 이유: 원격이 뜻 없이 보낸 어휘를 나중에 수업 노트나
        # 팟캐스트 분석이 같은 표제어로 다시 만나면 그때 채워진다. 뜻이 이미 있으면
        # 빈 값으로 밀리지 않는다 — 조건이 양쪽 다 보기 때문이다.
        for attr in ("meaning_kr", "meaning_en", "usage_note"):
            if not getattr(word, attr) and getattr(entry, attr):
                setattr(word, attr, getattr(entry, attr))
                changed = True
        if changed:
            word.updated_at = dt.datetime.now(dt.UTC)
            stats.words_updated += 1

    if entry.sentence:
        key = sentence_key(entry.sentence)
        exists = session.scalar(
            select(Occurrence.id).where(
                Occurrence.word_id == word.id, Occurrence.sentence_hash == key
            )
        )
        if not exists:
            session.add(
                Occurrence(
                    word_id=word.id,
                    sentence=entry.sentence,
                    sentence_hash=key,
                    translation_kr=entry.translation_kr,
                    definition_kr=entry.meaning_kr or None,
                    usage_note=entry.usage_note,
                    source_kind=entry.source_kind,
                    source_title=entry.source_title,
                    source_url=entry.source_url,
                    occurred_on=entry.occurred_on,
                )
            )
            stats.occurrences_created += 1

    return word


def gather() -> tuple[list[Entry], Stats]:
    stats = Stats()
    entries: list[Entry] = []

    for path in sorted(DATA_DIR.glob("*.json")):
        if not DAILY_FILE_RE.match(path.name):
            continue
        try:
            found = parse_daily(path)
        except Exception as e:
            stats.skipped.append(f"{path.name}: {e}")
            continue
        stats.files += 1
        entries.extend(found)

    for path in sorted(WEEKLY_DIR.glob("*.json")):
        try:
            found = parse_weekly(path)
        except Exception as e:
            stats.skipped.append(f"{path.name}: {e}")
            continue
        stats.files += 1
        entries.extend(found)

    if CLASS_NOTES_DIR.is_dir():
        for path in sorted(CLASS_NOTES_DIR.glob("영어수업 *.md")):
            try:
                found = parse_class_note(path)
            except Exception as e:
                stats.skipped.append(f"{path.name}: {e}")
                continue
            stats.files += 1
            entries.extend(found)

    stats.entries = len(entries)
    return entries, stats


def collect(*, rebuild: bool = False, prepared: tuple[list[Entry], Stats] | None = None) -> Stats:
    """어휘 저장소를 갱신한다.

    `prepared` 로 이미 읽어 둔 결과를 넘길 수 있다. sync 가 같은 파일을 두 번 파싱하지
    않게 하려는 것이다.
    """
    engine = make_engine()
    create_all(engine)
    Session_ = session_factory(engine)

    entries, stats = prepared if prepared is not None else gather()

    with Session_() as session:
        if rebuild:
            _rebuild(session, stats)

        for entry in entries:
            upsert(session, entry, stats)
        session.commit()

    return stats


def _rebuild(session: Session, stats: Stats) -> None:
    """어휘를 비우고 다시 만든다. **카드가 달린 어휘는 건드리지 않는다.**

    `word` 를 통째로 지우면 DB 의 ON DELETE CASCADE 가 card -> review_log 까지
    따라 내려간다. review_log 는 어디에도 사본이 없는 유일한 데이터이고 소급해서
    만들 수도 없는데, 그것이 `--rebuild` 라는 이름 뒤에 숨어 있었다.

    카드가 있는 어휘는 남겨도 손해가 없다. 어차피 이어지는 upsert 가 같은
    (표제어, 종류) 로 찾아 갱신한다.
    """
    protected = select(Card.word_id).scalar_subquery()

    kept = session.scalar(
        select(func.count()).select_from(Word).where(Word.id.in_(protected))
    ) or 0
    session.execute(delete(Occurrence).where(Occurrence.word_id.notin_(protected)))
    session.execute(delete(Word).where(Word.id.notin_(protected)))
    session.commit()

    if kept:
        stats.skipped.append(f"카드가 달린 어휘 {kept}개는 복습 기록 보호를 위해 유지")
        logger.warning("카드가 달린 어휘 %d개는 지우지 않았습니다 (복습 기록 보호)", kept)


def summarize(stats: Stats) -> str:
    engine = make_engine()
    Session_ = session_factory(engine)
    lines = [
        f"파일 {stats.files}개에서 어휘 항목 {stats.entries}건 수집",
        f"  새 어휘 {stats.words_created}  보강 {stats.words_updated}  새 예문 {stats.occurrences_created}",
    ]
    with Session_() as session:
        total = session.scalar(select(func.count()).select_from(Word)) or 0
        occ = session.scalar(select(func.count()).select_from(Occurrence)) or 0
        lines.append(f"저장소 현황: 어휘 {total}개 / 예문 {occ}개")

        rows = session.execute(
            select(Word.band, Word.kind, func.count()).group_by(Word.band, Word.kind)
        ).all()
        if rows:
            lines.append("  밴드별:")
            for band, kind, count in sorted(rows):
                lines.append(f"    {band:<6} {kind:<11} {count:>4}")

        multi = session.execute(
            select(func.count())
            .select_from(
                select(Occurrence.word_id)
                .group_by(Occurrence.word_id)
                .having(func.count() > 1)
                .subquery()
            )
        ).scalar_one()
        lines.append(f"  예문 2개 이상 확보한 어휘: {multi}개 (문맥 순환 가능)")

    if stats.skipped:
        lines.append(f"  건너뛴 파일 {len(stats.skipped)}개:")
        lines.extend(f"    {s}" for s in stats.skipped[:10])

    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="전사 산출물에서 어휘 저장소를 만든다")
    parser.add_argument("--rebuild", action="store_true", help="저장소를 비우고 다시 만든다")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s %(message)s",
    )
    stats = collect(rebuild=args.rebuild)
    print(summarize(stats))
    return 0


if __name__ == "__main__":
    sys.exit(main())
