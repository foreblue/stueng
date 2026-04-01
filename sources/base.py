from __future__ import annotations
from abc import ABC, abstractmethod
from dataclasses import dataclass


@dataclass
class Episode:
    title: str
    audio_url: str       # MP3 직접 링크
    transcript: str      # 전사문 전체 텍스트
    source_name: str     # "Up First", "Planet Money" 등
    episode_url: str     # 원문 페이지 링크
    duration_sec: int    # 재생 시간(초)
    published: str       # 발행일 문자열


class PodcastSource(ABC):
    @abstractmethod
    def fetch_latest(self) -> Episode | None:
        """최신 에피소드 1개를 가져온다. 전사문이 없으면 None 반환."""
        pass
