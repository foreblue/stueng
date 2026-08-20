import html
import logging
import time
from datetime import date

import requests

import config
from sources.base import Episode

logger = logging.getLogger(__name__)

TELEGRAM_API = f"https://api.telegram.org/bot{config.TELEGRAM_BOT_TOKEN}"


def _e(text: str) -> str:
    """HTML 이스케이프"""
    return html.escape(str(text))


def _format_episode(episode: Episode, *, app_url: str = "") -> str:
    """에피소드 소개.

    어휘는 더 이상 여기에 싣지 않는다. 읽기만 하는 노출은 학습이 되지 않는다는 것이
    이 프로젝트가 조사에서 얻은 결론이고(Karpicke & Roediger 2008), 어휘를 꺼내는 일은
    복습 웹앱이 맡는다. 이 메시지의 목적은 하나다 — 오늘 팟캐스트를 실제로 듣게 하는 것.
    """
    duration_min = episode.duration_sec // 60
    listen_url = episode.podcast_url or episode.episode_url or episode.audio_url

    lines = [
        f"🎧 <b>{_e(episode.source_name)}</b> · {date.today():%Y-%m-%d}",
        "",
        f"<b>{_e(episode.title)}</b> (~{duration_min}분)",
        (
            f"<a href=\"{_e(listen_url)}\">오디오 듣기</a>  "
            f"<a href=\"{_e(episode.episode_url)}\">원문 보기</a>"
        ),
    ]
    if app_url:
        lines += ["", f"📇 <a href=\"{_e(app_url)}\">오늘의 단어 복습하기</a>"]
    return "\n".join(lines)


def _format_due(due: int, new_available: int, unresolved: int, app_url: str) -> str:
    """복습 알림. 문제를 여기서 풀게 하지 않고 앱을 열게 만든다."""
    total = due + unresolved
    if not total and not new_available:
        return ""

    bits = []
    if total:
        bits.append(f"복습 {total}장")
    if new_available:
        bits.append(f"새 단어 {new_available}개")

    lines = [f"📇 <b>오늘의 단어</b> — {' · '.join(bits)}"]
    if unresolved:
        lines.append(f"어제 못 맞힌 카드 {unresolved}장이 남아 있습니다.")
    lines += ["", f"<a href=\"{_e(app_url)}\">복습하러 가기</a>"]
    return "\n".join(lines)


def _split_message(text: str) -> list[str]:
    if len(text) <= config.TELEGRAM_MAX_LENGTH:
        return [text]

    parts = []
    while len(text) > config.TELEGRAM_MAX_LENGTH:
        split_at = text.rfind("\n\n", 0, config.TELEGRAM_MAX_LENGTH)
        if split_at == -1:
            split_at = config.TELEGRAM_MAX_LENGTH
        parts.append(text[:split_at])
        text = text[split_at:].lstrip()
    if text:
        parts.append(text)
    return parts


def _send_message(text: str) -> bool:
    url = f"https://api.telegram.org/bot{config.TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": config.TELEGRAM_CHAT_ID,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    for attempt in range(2):
        try:
            resp = requests.post(url, json=payload, timeout=15)
            if resp.ok:
                return True
            logger.warning("Telegram error (attempt %d): %s", attempt + 1, resp.text)
        except requests.RequestException as e:
            logger.warning("Telegram request failed (attempt %d): %s", attempt + 1, e)
        if attempt == 0:
            time.sleep(5)
    return False


def send_episode(episode: Episode, *, app_url: str = "", fail_reason: str = "") -> bool:
    """오늘의 에피소드를 알린다. 어휘는 웹앱에서 푼다."""
    text = _format_episode(episode, app_url=app_url)
    if fail_reason:
        text += f"\n\n⚠️ <b>AI 분석 실패</b>\n<pre>{_e(fail_reason)}</pre>"

    ok = True
    for part in _split_message(text):
        ok = _send_message(part) and ok
        time.sleep(0.5)
    return ok


def send_due(due: int, new_available: int, unresolved: int, app_url: str) -> bool:
    """복습할 게 있을 때만 보낸다. 빈 알림은 알림을 무시하게 만든다."""
    text = _format_due(due, new_available, unresolved, app_url)
    if not text:
        return False
    return _send_message(text)


def send_error(message: str) -> None:
    _send_message(f"⚠️ stueng 오류\n\n{_e(message)}")
