from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

import config
import messenger
import weekly_analyzer
from sources.base import Episode
from sources.planetmoney import PlanetMoney

DATA_DIR = Path(__file__).parent / "data" / "weekly"
STUDY_DAYS = 5

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger(__name__)


def _parse_date(value: str | None) -> date:
    if not value:
        return date.today()
    return datetime.strptime(value, "%Y-%m-%d").date()


def _episode_payload(episode: Episode) -> dict:
    return {
        "source": episode.source_name,
        "title": episode.title,
        "episode_url": episode.episode_url,
        "audio_url": episode.audio_url,
        "published": episode.published,
        "podcast_url": episode.podcast_url,
        "duration_sec": episode.duration_sec,
        "transcript": episode.transcript,
    }


def _episode_from_payload(payload: dict) -> Episode:
    return Episode(
        title=payload["title"],
        audio_url=payload["audio_url"],
        transcript=payload.get("transcript", ""),
        source_name=payload["source"],
        episode_url=payload["episode_url"],
        duration_sec=payload["duration_sec"],
        published=payload["published"],
        podcast_url=payload.get("podcast_url", ""),
    )


def _plan_path(start_date: date) -> Path:
    return DATA_DIR / f"planetmoney-{start_date:%Y-%m-%d}.json"


def _latest_plan_path() -> Path | None:
    paths = sorted(DATA_DIR.glob("planetmoney-*.json"))
    return paths[-1] if paths else None


def _load_plan(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _save_plan(path: Path, plan: dict) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(plan, ensure_ascii=False, indent=2), encoding="utf-8")


def _study_dates(start_date: date) -> list[date]:
    dates = []
    current = start_date
    while len(dates) < STUDY_DAYS:
        if current.weekday() < 5:
            dates.append(current)
        current += timedelta(days=1)
    return dates


def _first_study_date(plan: dict) -> date:
    start_date = datetime.strptime(plan["start_date"], "%Y-%m-%d").date()
    return _study_dates(start_date)[0]


def prepare(run_date: date) -> Path:
    config.validate()

    logger.info("Planet Money 주간 학습 준비 시작 (start_date: %s)", run_date)
    episode = PlanetMoney().fetch_latest()
    if not episode:
        raise RuntimeError("Planet Money transcript가 있는 최신 에피소드를 찾지 못했습니다.")

    weekly_analysis, fail_reason = weekly_analyzer.analyze_weekly(episode)
    if not weekly_analysis:
        raise RuntimeError(f"Planet Money 주간 분석 실패: {fail_reason}")

    study_dates = _study_dates(run_date)
    plan = {
        "prepared_at": datetime.now().isoformat(timespec="seconds"),
        "start_date": f"{run_date:%Y-%m-%d}",
        "end_date": f"{study_dates[-1]:%Y-%m-%d}",
        "study_days": STUDY_DAYS,
        "study_dates": [f"{day:%Y-%m-%d}" for day in study_dates],
        "episode": _episode_payload(episode),
        "weekly_analysis": weekly_analysis,
        "sent_dates": [],
    }
    path = _plan_path(run_date)
    _save_plan(path, plan)
    logger.info("Planet Money 주간 학습 계획 저장 완료: %s", path)
    return path


def send(run_date: date) -> bool:
    """새 Planet Money 에피소드를 한 번 알린다.

    예전에는 평일마다 그날 몫의 단어·표현을 보냈다. 지금은 어휘를 꺼내는 일을 복습
    웹앱이 맡으므로, 텔레그램은 "이번 주 에피소드가 이것" 이라고 한 번 알리는 데까지만
    한다. 주간 분석 자체는 계속 만든다 — 그게 어휘 저장소의 재료다.
    """
    config.validate()

    path = _latest_plan_path()
    if not path:
        raise RuntimeError("저장된 Planet Money 주간 학습 계획이 없습니다. 먼저 prepare를 실행하세요.")

    plan = _load_plan(path)
    if run_date != _first_study_date(plan):
        logger.info("이번 주 에피소드는 이미 첫날에 알렸습니다: %s", run_date)
        return False

    run_date_text = f"{run_date:%Y-%m-%d}"
    sent_dates = plan.setdefault("sent_dates", [])
    if run_date_text in sent_dates:
        logger.info("이미 전송한 날짜라 스킵: %s", run_date_text)
        return False

    episode = _episode_from_payload(plan["episode"])
    if not messenger.send_episode(episode, app_url=config.VOCAB_APP_URL):
        raise RuntimeError(f"Planet Money 에피소드 알림 전송 실패: {run_date_text}")

    sent_dates.append(run_date_text)
    _save_plan(path, plan)
    logger.info("Planet Money 에피소드 알림 전송 완료: %s", run_date_text)
    return True


def main() -> int:
    parser = argparse.ArgumentParser(description="Planet Money 주간 어휘 준비 / 에피소드 알림")
    subparsers = parser.add_subparsers(dest="command", required=True)

    prepare_parser = subparsers.add_parser("prepare", help="최신 Planet Money 에피소드로 평일 5일치 학습 계획 생성")
    prepare_parser.add_argument("--date", help="학습 시작일 (YYYY-MM-DD), 기본값: 오늘")

    send_parser = subparsers.add_parser("send", help="이번 주 에피소드를 텔레그램으로 한 번 알린다")
    send_parser.add_argument("--date", help="전송 기준일 (YYYY-MM-DD), 기본값: 오늘")

    args = parser.parse_args()
    try:
        if args.command == "prepare":
            path = prepare(_parse_date(args.date))
            print(path)
            return 0
        if args.command == "send":
            send(_parse_date(args.date))
            return 0
    except Exception as e:
        logger.exception("weekly_study 실행 실패")
        try:
            messenger.send_error(str(e))
        except Exception:
            pass
        return 1

    return 1


if __name__ == "__main__":
    sys.exit(main())
