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


def _format_analysis(episode: Episode, analysis: dict) -> str:
    # raw fallback
    if "raw" in analysis:
        duration_min = episode.duration_sec // 60
        return (
            f"📰 <b>{_e(episode.title)}</b> (~{duration_min}min)\n"
            f"🎧 <a href=\"{_e(episode.audio_url)}\">오디오 듣기</a>  "
            f"📄 <a href=\"{_e(episode.episode_url)}\">원문 보기</a>\n\n"
            f"{_e(analysis['raw'])}"
        )

    lines = []
    duration_min = episode.duration_sec // 60

    # 헤더
    lines.append(f"📰 <b>{_e(episode.source_name)}: {_e(episode.title)}</b> (~{duration_min}min)")
    lines.append(
        f"🎧 <a href=\"{_e(episode.audio_url)}\">오디오 듣기</a>  "
        f"📄 <a href=\"{_e(episode.episode_url)}\">원문 보기</a>"
    )

    # 어휘
    vocabulary = analysis.get("vocabulary", [])
    if vocabulary:
        lines.append("")
        lines.append("📚 <b>어휘 (Vocabulary)</b>")
        for item in vocabulary:
            word = _e(item.get("word", ""))
            def_kr = _e(item.get("definition_kr") or item.get("korean_definition", ""))
            def_en = _e(item.get("definition_en", ""))
            example = _e(item.get("example", ""))
            lines.append("")
            lines.append(f"• <b>{word}</b> — {def_kr}")
            lines.append(f"  <i>{def_en}</i>")
            if example:
                lines.append(f"  예문: \"{example}\"")

    # 표현
    expressions = analysis.get("expressions", [])
    if expressions:
        lines.append("")
        lines.append("💬 <b>표현 (Expressions)</b>")
        for item in expressions:
            expr = _e(item.get("expression") or item.get("phrase", ""))
            meaning = _e(item.get("meaning_kr") or item.get("korean_meaning", ""))
            usage = _e(item.get("usage_note", ""))
            lines.append("")
            lines.append(f"• <b>{expr}</b>")
            lines.append(f"  뜻: {meaning}")
            if usage:
                lines.append(f"  사용법: {usage}")

    # 핵심 문장
    key_sentences = analysis.get("key_sentences", [])
    if key_sentences:
        lines.append("")
        lines.append("📝 <b>핵심 문장 (Key Sentences)</b>")
        for i, item in enumerate(key_sentences, 1):
            sentence = _e(item.get("sentence", ""))
            translation = _e(item.get("translation_kr", ""))
            explanation = _e(item.get("explanation_kr", ""))
            lines.append("")
            lines.append(f"{i}. \"{sentence}\"")
            if translation:
                lines.append("")
                lines.append(f"   번역: {translation}")
            if explanation:
                lines.append("")
                lines.append(f"   → {explanation}")

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


def send(episode: Episode, analysis: dict) -> None:
    today = date.today().strftime("%Y-%m-%d")
    header = f"🌅 <b>오늘의 영어 공부 — {today}</b>"
    body = _format_analysis(episode, analysis)
    full_message = f"{header}\n\n{body}"

    parts = _split_message(full_message)
    for part in parts:
        _send_message(part)
        time.sleep(0.5)


def send_error(message: str) -> None:
    _send_message(f"⚠️ stueng 오류\n\n{_e(message)}")
