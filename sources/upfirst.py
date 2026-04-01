from __future__ import annotations
import re
import logging

import feedparser
import requests
from bs4 import BeautifulSoup

from .base import Episode, PodcastSource

logger = logging.getLogger(__name__)

RSS_URL = "https://feeds.npr.org/510318/podcast.xml"
TEXT_BASE = "https://text.npr.org"
HEADERS = {"User-Agent": "Mozilla/5.0"}


def _extract_article_id(url: str) -> str | None:
    # 예: https://www.npr.org/2026/03/31/nx-s1-5767002/iran-war-...
    m = re.search(r"/\d{4}/\d{2}/\d{2}/([^/]+)/", url)
    return m.group(1) if m else None


def _fetch_transcript(article_id: str) -> str:
    url = f"{TEXT_BASE}/{article_id}"
    try:
        resp = requests.get(url, headers=HEADERS, timeout=15)
        resp.raise_for_status()
    except requests.RequestException as e:
        logger.warning("Failed to fetch transcript for %s: %s", article_id, e)
        return ""

    soup = BeautifulSoup(resp.text, "html.parser")

    # 전사문 없는 에피소드 체크 (body class에 no-transcript 포함)
    body = soup.find("body")
    if body and "no-transcript" in body.get("class", []):
        logger.info("No transcript available for %s", article_id)
        return ""

    container = soup.find("div", class_="paragraphs-container")
    if not container:
        return ""

    return container.get_text(separator=" ", strip=True)


class UpFirst(PodcastSource):
    def fetch_latest(self) -> Episode | None:
        feed = feedparser.parse(RSS_URL)
        if not feed.entries:
            logger.error("Up First RSS feed is empty")
            return None

        for entry in feed.entries:
            article_id = _extract_article_id(entry.link)
            if not article_id:
                continue

            transcript = _fetch_transcript(article_id)
            if not transcript:
                logger.info("Skipping %s: no transcript", entry.title)
                continue

            # 오디오 URL: enclosures에서 첫 번째 audio/mpeg
            audio_url = ""
            for enc in getattr(entry, "enclosures", []):
                if "audio" in enc.get("type", ""):
                    audio_url = enc.get("href", "") or enc.get("url", "")
                    break

            duration_sec = 0
            raw_dur = entry.get("itunes_duration", "0")
            if str(raw_dur).isdigit():
                duration_sec = int(raw_dur)

            return Episode(
                title=entry.title,
                audio_url=audio_url,
                transcript=transcript,
                source_name="Up First",
                episode_url=entry.link,
                duration_sec=duration_sec,
                published=entry.get("published", ""),
            )

        logger.error("No Up First episode with transcript found")
        return None


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    ep = UpFirst().fetch_latest()
    if ep:
        print(f"Title: {ep.title}")
        print(f"Duration: {ep.duration_sec // 60}min")
        print(f"Audio: {ep.audio_url[:80]}...")
        print(f"Transcript length: {len(ep.transcript)} chars")
        print(f"\nTranscript preview:\n{ep.transcript[:500]}")
    else:
        print("No episode found")
