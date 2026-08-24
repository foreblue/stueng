"""로컬 파이프라인 <-> 복습 서버.

어휘를 만드는 일은 계속 이 맥북이 한다. 팟캐스트 분석은 로컬 LLM 프록시
(`localhost:9000`)에 물려 있고 수업 녹음·전사는 macOS 전용이라 서버로 옮길 수 없다.
서버는 복습만 맡는다.

    python -m vocab.sync push            # 어휘를 모아 서버로 밀어 올린다
    python -m vocab.sync push --dry-run  # 보내지 않고 무엇이 갈지 본다
    python -m vocab.sync notify          # 오늘 복습할 게 있으면 텔레그램으로 알린다
    python -m vocab.sync status          # 서버 현황을 출력한다

`push` 는 매번 전체를 보낸다. 서버 쪽 upsert 가 멱등이라 증분을 추적할 필요가 없고,
추적 상태를 두면 그 상태가 틀어졌을 때 조용히 누락된다.
"""

from __future__ import annotations

import argparse
import logging
import sys

import requests

import config
from . import collect

logger = logging.getLogger(__name__)

#: 한 번에 보내는 어휘 수. 전체가 1,000건 남짓이라 크게 나눌 이유가 없다.
BATCH = 200

TIMEOUT = 30


class SyncError(RuntimeError):
    pass


def _require(*, need_token: bool = True) -> str:
    if not config.VOCAB_SERVER_URL:
        raise SyncError("VOCAB_SERVER_URL 이 설정되지 않았습니다.")
    if need_token and not config.VOCAB_INGEST_TOKEN:
        raise SyncError("VOCAB_INGEST_TOKEN 이 설정되지 않았습니다.")
    return config.VOCAB_SERVER_URL


def _headers() -> dict[str, str]:
    return {"X-Ingest-Token": config.VOCAB_INGEST_TOKEN}


def _payload(entry: collect.Entry) -> dict:
    return {
        "display": entry.display,
        "kind": entry.kind,
        "meaning_kr": entry.meaning_kr,
        "source_kind": entry.source_kind,
        "source_title": entry.source_title,
        "occurred_on": entry.occurred_on.isoformat(),
        "meaning_en": entry.meaning_en,
        "usage_note": entry.usage_note,
        "sentence": entry.sentence,
        "translation_kr": entry.translation_kr,
        "source_url": entry.source_url,
    }


def push(*, dry_run: bool = False) -> dict:
    """로컬 산출물을 읽어 로컬 저장소와 서버에 함께 반영한다."""
    prepared = collect.gather()
    entries, gather_stats = prepared

    if dry_run:
        by_source: dict[str, int] = {}
        for entry in entries:
            by_source[entry.source_kind] = by_source.get(entry.source_kind, 0) + 1
        return {"dry_run": True, "entries": len(entries), "by_source": by_source,
                "files": gather_stats.files, "skipped": gather_stats.skipped}

    local = collect.collect(prepared=prepared)
    logger.info(
        "로컬 저장소 갱신: 새 어휘 %d, 새 예문 %d", local.words_created, local.occurrences_created
    )

    base = _require()
    totals = {"received": 0, "words_created": 0, "words_updated": 0, "occurrences_created": 0}
    last: dict = {}

    for start in range(0, len(entries), BATCH):
        chunk = entries[start : start + BATCH]
        body = {"entries": [_payload(e) for e in chunk]}
        response = requests.post(f"{base}/ingest", json=body, headers=_headers(), timeout=TIMEOUT)
        if not response.ok:
            raise SyncError(f"ingest 실패 ({response.status_code}): {response.text[:200]}")
        last = response.json()
        for key in totals:
            totals[key] += last.get(key, 0)
        logger.info("전송 %d/%d", min(start + BATCH, len(entries)), len(entries))

    totals["total_words"] = last.get("total_words", 0)
    return totals


def server_progress() -> dict:
    base = _require()
    response = requests.get(f"{base}/api/progress", headers=_headers(), timeout=TIMEOUT)
    if not response.ok:
        raise SyncError(f"현황 조회 실패 ({response.status_code}): {response.text[:200]}")
    return response.json()


def notify() -> bool:
    """복습할 게 있을 때만 텔레그램으로 알린다.

    개수만 보내고 링크를 건다. 단어를 본문에 실으면 읽기만 하고 끝나는데, 그건 학습이
    아니라는 것이 이 서비스를 만든 이유다.
    """
    import messenger  # 로컬 전용 모듈이라 서버 코드에서 끌어 쓰지 않게 늦게 가져온다

    state = server_progress()
    app_url = config.VOCAB_APP_URL or config.VOCAB_SERVER_URL
    if not app_url:
        raise SyncError("VOCAB_APP_URL 이 설정되지 않았습니다.")

    return messenger.send_due(
        due=state["due"],
        new_available=state["new_available"],
        unresolved=state["unresolved"],
        app_url=app_url,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="로컬 어휘를 복습 서버와 맞춘다")
    sub = parser.add_subparsers(dest="command", required=True)

    push_parser = sub.add_parser("push", help="어휘를 모아 서버로 밀어 올린다")
    push_parser.add_argument("--dry-run", action="store_true",
                             help="아무것도 쓰지 않고 무엇이 갈지만 확인")
    sub.add_parser("notify", help="오늘 복습할 게 있으면 텔레그램으로 알린다")
    sub.add_parser("status", help="서버 현황 출력")

    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    try:
        if args.command == "push":
            result = push(dry_run=args.dry_run)
            if result.get("dry_run"):
                print(f"파일 {result['files']}개에서 어휘 항목 {result['entries']}건")
                for source, count in sorted(result["by_source"].items()):
                    print(f"  {source:<12} {count:>4}")
                for skipped in result["skipped"][:10]:
                    print(f"  건너뜀: {skipped}")
            else:
                print(
                    f"전송 {result['received']}건 → 새 어휘 {result['words_created']}, "
                    f"보강 {result['words_updated']}, 새 예문 {result['occurrences_created']} "
                    f"(서버 총 어휘 {result['total_words']})"
                )
            return 0

        if args.command == "notify":
            sent = notify()
            print("알림 전송" if sent else "복습할 게 없어 알리지 않음")
            return 0

        if args.command == "status":
            for key, value in server_progress().items():
                print(f"  {key:<16} {value}")
            return 0

    except SyncError as e:
        logger.error("%s", e)
        return 1
    except requests.RequestException as e:
        logger.error("서버에 연결하지 못했습니다: %s", e)
        return 1

    return 1


if __name__ == "__main__":
    sys.exit(main())
