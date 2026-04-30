"""Whisper STT — транскрипция голосовых через Groq Whisper API."""

from pathlib import Path

import openai
from loguru import logger

from abay_assistant.config import get_settings

GROQ_BASE_URL = "https://api.groq.com/openai/v1"


class WhisperClient:
    """Клиент для Groq Whisper API (speech-to-text)."""

    def __init__(self) -> None:
        key = get_settings().groq_api_key
        self._enabled = bool(key)
        if self._enabled:
            self._client = openai.AsyncOpenAI(
                api_key=key,
                base_url=GROQ_BASE_URL,
            )
        else:
            logger.warning("GROQ_API_KEY не задан — голосовые не будут транскрибироваться")

    @property
    def enabled(self) -> bool:
        return self._enabled

    async def transcribe(self, audio_path: Path) -> str:
        """Транскрибировать аудиофайл. Возвращает текст."""
        if not self._enabled:
            raise RuntimeError("Whisper не настроен: нет GROQ_API_KEY")

        with open(audio_path, "rb") as f:
            resp = await self._client.audio.transcriptions.create(
                model="whisper-large-v3",
                file=f,
                language="ru",
            )

        text = resp.text.strip()
        logger.info("Whisper (Groq): '{}' ({} символов)", text[:60], len(text))
        return text
