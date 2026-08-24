"""이미 쌓인 데이터를 손보는 일회성 작업들.

Alembic 을 두지 않았으므로 스키마가 아니라 **내용**을 고치는 것만 여기서 다룬다.
컬럼을 더하거나 지우는 일이 생기면 그때는 Alembic 을 들여야 한다.

    python -m vocab.migrate kinds --dry-run
    python -m vocab.migrate kinds

서버에서 돌릴 때는 컨테이너 안에서:

    docker exec stueng python -m vocab.migrate kinds
"""

from __future__ import annotations

import argparse
import logging
import sys
from dataclasses import dataclass, field

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from .db import create_all, make_engine, session_factory
from .models import ReviewLog, Word, kind_for

logger = logging.getLogger(__name__)


@dataclass
class KindReport:
    checked: int = 0
    retagged: int = 0
    merged: int = 0
    moved_occurrences: int = 0
    dropped_duplicates: int = 0
    changes: list[str] = field(default_factory=list)


def _weight(session: Session, word: Word) -> tuple[int, int, int]:
    """병합할 때 어느 쪽을 남길지. 클수록 남긴다.

    학습 기록이 붙은 쪽이 무조건 우선이다 — 카드를 지우면 review_log 가 따라
    지워지고 그건 되살릴 수 없다. 그 다음은 문맥이 많은 쪽, 마지막은 먼저 만난 쪽.
    """
    reviews = 0
    if word.card:
        reviews = session.scalar(
            select(func.count()).select_from(ReviewLog).where(ReviewLog.card_id == word.card.id)
        ) or 0
    return (1 if word.card else 0, reviews, len(word.occurrences))


def _merge(session: Session, keep: Word, drop: Word, report: KindReport) -> None:
    """`drop` 의 예문을 `keep` 으로 옮기고 `drop` 을 지운다."""
    existing = {o.sentence_hash for o in keep.occurrences}
    for occurrence in list(drop.occurrences):
        if occurrence.sentence_hash in existing:
            report.dropped_duplicates += 1
            continue
        occurrence.word_id = keep.id
        existing.add(occurrence.sentence_hash)
        report.moved_occurrences += 1

    # 비어 있던 칸만 채운다. 남기는 쪽의 뜻을 덮어쓰지 않는다.
    for attr in ("meaning_en", "usage_note", "mnemonic"):
        if not getattr(keep, attr) and getattr(drop, attr):
            setattr(keep, attr, getattr(drop, attr))
    keep.first_seen = min(keep.first_seen, drop.first_seen)
    keep.known = keep.known or drop.known

    session.flush()
    session.execute(delete(Word).where(Word.id == drop.id))
    report.merged += 1


def fix_kinds(session: Session, *, dry_run: bool = False) -> KindReport:
    """종류를 표기 형태에 맞춘다.

    바꾸려는 종류로 이미 같은 표제어가 있으면 둘을 합친다. 그대로 두면
    (표제어, 종류) 유니크 제약에 걸린다.
    """
    report = KindReport()
    words = session.scalars(select(Word)).all()
    report.checked = len(words)

    index = {(w.headword, w.kind): w for w in words}

    for word in words:
        target = kind_for(word.display)
        if target == word.kind:
            continue

        clash = index.get((word.headword, target))
        if clash is None:
            report.changes.append(f"{word.display}: {word.kind} → {target}")
            if not dry_run:
                index.pop((word.headword, word.kind), None)
                word.kind = target
                index[(word.headword, target)] = word
            report.retagged += 1
            continue

        # 같은 표제어가 양쪽에 있다. 학습 기록이 붙은 쪽을 남긴다.
        keep, drop = (clash, word)
        if _weight(session, word) > _weight(session, clash):
            keep, drop = (word, clash)

        report.changes.append(
            f"{word.display}: {word.kind}+{target} 중복 → "
            f"{'카드 있는' if keep.card else '문맥 많은'} 쪽으로 병합"
        )
        if not dry_run:
            _merge(session, keep, drop, report)
            keep.kind = target
            index.pop((drop.headword, drop.kind), None)
            index[(keep.headword, target)] = keep
        else:
            report.merged += 1

    if not dry_run:
        session.commit()
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="쌓인 어휘 데이터를 손본다")
    sub = parser.add_subparsers(dest="command", required=True)
    kinds = sub.add_parser("kinds", help="종류(word/expression)를 표기 형태에 맞춘다")
    kinds.add_argument("--dry-run", action="store_true", help="바꾸지 않고 무엇이 바뀔지만 본다")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    engine = make_engine()
    create_all(engine)
    with session_factory(engine)() as session:
        report = fix_kinds(session, dry_run=args.dry_run)

    head = "[미리보기] " if args.dry_run else ""
    print(f"{head}어휘 {report.checked}개 확인")
    print(f"  종류 변경 {report.retagged}개  ·  병합 {report.merged}개")
    if not args.dry_run:
        print(f"  예문 이동 {report.moved_occurrences}개  ·  중복 예문 제거 {report.dropped_duplicates}개")
    for line in report.changes[:40]:
        print(f"    {line}")
    if len(report.changes) > 40:
        print(f"    … 외 {len(report.changes) - 40}건")
    return 0


if __name__ == "__main__":
    sys.exit(main())
