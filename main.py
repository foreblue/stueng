from __future__ import annotations

import json
import logging
import sys
import time
from datetime import date
from pathlib import Path

import config
import analyzer
import messenger
from sources.planetmoney import PlanetMoney
from sources.upfirst import UpFirst

DATA_DIR = Path(__file__).parent / "data"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger(__name__)

SOURCE_REGISTRY = {
    "planetmoney": PlanetMoney,
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
    start_total = time.time()
    logger.info("=" * 60)
    logger.info("stueng 시작")
    logger.info("=" * 60)

    # 1단계: 설정 검증
    logger.info("[1/5] 설정 검증 중...")
    try:
        config.validate()
        logger.info("[1/5] 설정 검증 완료")
    except EnvironmentError as e:
        logger.error("[1/5] 설정 오류: %s", e)
        sys.exit(1)

    # 2단계: 소스 로드
    logger.info("[2/5] 소스 로드 중... (활성 소스: %s)", config.ACTIVE_SOURCES)
    sources = load_sources()
    if not sources:
        logger.error("[2/5] 활성 소스 없음. 종료합니다.")
        sys.exit(1)
    logger.info("[2/5] 소스 로드 완료: %d개", len(sources))

    try:
        for source in sources:
            source_name = source.__class__.__name__

            # 3단계: 에피소드 가져오기
            logger.info("[3/5] %s에서 에피소드 가져오는 중...", source_name)
            t0 = time.time()
            episode = source.fetch_latest()
            elapsed = time.time() - t0

            if not episode:
                logger.warning("[3/5] %s: 에피소드 없음 (%.1fs)", source_name, elapsed)
                continue

            logger.info(
                "[3/5] 에피소드 가져오기 완료 (%.1fs): %s (전사문 %d자, %d분)",
                elapsed,
                episode.title,
                len(episode.transcript),
                episode.duration_sec // 60,
            )

            # 4단계: AI 분석
            logger.info("[4/5] AI 분석 시작...")
            t0 = time.time()
            analysis, fail_reason = analyzer.analyze(episode)
            elapsed = time.time() - t0

            if not analysis:
                logger.warning("[4/5] 분석 결과 없음 (%.1fs): %s", elapsed, fail_reason)
            else:
                vocab_count = len(analysis.get("vocabulary", []))
                expr_count = len(analysis.get("expressions", []))
                sent_count = len(analysis.get("key_sentences", []))
                logger.info(
                    "[4/5] AI 분석 완료 (%.1fs): 어휘 %d개, 표현 %d개, 핵심문장 %d개",
                    elapsed,
                    vocab_count,
                    expr_count,
                    sent_count,
                )

            # 5단계: 텔레그램 전송
            logger.info("[5/5] 텔레그램 전송 중...")
            t0 = time.time()
            messenger.send(episode, analysis, fail_reason=fail_reason)
            elapsed = time.time() - t0
            logger.info("[5/5] 텔레그램 전송 완료 (%.1fs)", elapsed)

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
            logger.info("결과 저장 완료: %s", out_path)

    except Exception as e:
        logger.exception("예상치 못한 오류 발생")
        try:
            messenger.send_error(str(e))
        except Exception:
            pass
        sys.exit(1)

    total_elapsed = time.time() - start_total
    logger.info("=" * 60)
    logger.info("stueng 완료 (총 소요시간: %.1fs)", total_elapsed)
    logger.info("=" * 60)


if __name__ == "__main__":
    main()
