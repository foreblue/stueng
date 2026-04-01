from __future__ import annotations

import json
import logging
import sys
from datetime import date
from pathlib import Path

import config
import analyzer
import messenger
from sources.upfirst import UpFirst

DATA_DIR = Path(__file__).parent / "data"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

SOURCE_REGISTRY = {
    "upfirst": UpFirst,
}


def load_sources():
    sources = []
    for name in config.ACTIVE_SOURCES:
        cls = SOURCE_REGISTRY.get(name)
        if cls:
            sources.append(cls())
        else:
            logger.warning("Unknown source: %s", name)
    return sources


def main():
    try:
        config.validate()
    except EnvironmentError as e:
        logger.error(str(e))
        sys.exit(1)

    sources = load_sources()
    if not sources:
        logger.error("No active sources configured")
        sys.exit(1)

    try:
        for source in sources:
            logger.info("Fetching from %s", source.__class__.__name__)
            episode = source.fetch_latest()
            if not episode:
                logger.warning("No episode from %s", source.__class__.__name__)
                continue

            logger.info("Analyzing: %s", episode.title)
            analysis = analyzer.analyze(episode)

            logger.info("Sending to Telegram")
            messenger.send(episode, analysis)

            # 결과 저장
            DATA_DIR.mkdir(exist_ok=True)
            today = date.today().strftime("%Y-%m-%d")
            out_path = DATA_DIR / f"{today}.json"
            payload = {
                "date": today,
                "source": episode.source_name,
                "title": episode.title,
                "episode_url": episode.episode_url,
                "audio_url": episode.audio_url,
                "published": episode.published,
                "duration_sec": episode.duration_sec,
                "transcript": episode.transcript,
                "analysis": analysis,
            }
            out_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            logger.info("Saved to %s", out_path)
            logger.info("Done")

    except Exception as e:
        logger.exception("Unhandled error")
        try:
            messenger.send_error(str(e))
        except Exception:
            pass
        sys.exit(1)


if __name__ == "__main__":
    main()
