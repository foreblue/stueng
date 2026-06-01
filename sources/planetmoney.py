from __future__ import annotations

import logging
import re

import feedparser
import requests
from bs4 import BeautifulSoup

from .base import Episode, PodcastSource

logger = logging.getLogger(__name__)

RSS_URL = "https://feeds.npr.org/510289/podcast.xml"
TEXT_BASE = "https://text.npr.org"
HEADERS = {"User-Agent": "Mozilla/5.0"}
APPLE_COLLECTION_ID = 290783428


def _normalize_title(title: str) -> str:
    return re.sub(r"\s+", " ", title).strip().casefold()


def _extract_article_id(url: str) -> str | None:
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
    body = soup.find("body")
    if body and "no-transcript" in body.get("class", []):
        logger.info("No transcript available for %s", article_id)
        return ""

    container = soup.find("div", class_="paragraphs-container")
    if not container:
        return ""

    return container.get_text(separator=" ", strip=True)


def _fetch_apple_episode_url(title: str) -> str:
    try:
        resp = requests.get(
            "https://itunes.apple.com/search",
            params={
                "term": title,
                "media": "podcast",
                "entity": "podcastEpisode",
                "limit": 10,
            },
            timeout=10,
        )
        resp.raise_for_status()
    except requests.RequestException as e:
        logger.warning("Apple Podcasts episode URL lookup failed for %r: %s", title, e)
        return ""

    target = _normalize_title(title)
    fallback = ""
    for item in resp.json().get("results", []):
        url = item.get("trackViewUrl", "")
        if item.get("collectionId") != APPLE_COLLECTION_ID or not url:
            continue
        if _normalize_title(item.get("trackName", "")) == target:
            return url
        if not fallback:
            fallback = url
    return fallback


class PlanetMoney(PodcastSource):
    def fetch_latest(self) -> Episode | None:
        logger.info("RSS 피드 파싱 중: %s", RSS_URL)
        feed = feedparser.parse(RSS_URL)
        if not feed.entries:
            logger.error("Planet Money RSS 피드가 비어있음")
            return None

        logger.info("RSS 피드 항목 수: %d개, 최신 에피소드 탐색 중...", len(feed.entries))
        for entry in feed.entries:
            link = getattr(entry, "link", None)
            if not link:
                logger.warning(
                    "planetmoney RSS entry에 link 없음, 스킵: title=%r guid=%r published=%r",
                    getattr(entry, "title", ""),
                    getattr(entry, "id", ""),
                    getattr(entry, "published", ""),
                )
                continue

            article_id = _extract_article_id(link)
            if not article_id:
                logger.debug("article_id 추출 실패: %s", link)
                continue

            logger.info("전사문 가져오는 중: %s (id: %s)", entry.title, article_id)
            transcript = _fetch_transcript(article_id)
            if not transcript:
                logger.info("전사문 없음, 다음 에피소드로 건너뜀: %s", entry.title)
                continue

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
                source_name="Planet Money",
                episode_url=link,
                duration_sec=duration_sec,
                published=entry.get("published", ""),
                podcast_url=_fetch_apple_episode_url(entry.title),
            )

        logger.error("No Planet Money episode with transcript found")
        return None
