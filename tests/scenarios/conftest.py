"""Override глобального _patch_settings для сценариев — нужен реальный API key."""

import os
from pathlib import Path

import pytest
from dotenv import load_dotenv


@pytest.fixture(autouse=True)
def _patch_settings(monkeypatch):
    """Загружаем настоящий .env вместо тестовых заглушек.

    Сценарии гонят реальный LLM. Глобальный conftest.py подменяет
    ANTHROPIC_API_KEY="test-key", что для scenarios даёт 401.
    """
    env_path = Path(__file__).resolve().parents[2] / ".env"
    if env_path.exists():
        load_dotenv(env_path, override=True)

    # Минимально нужные значения должны прийти из .env, но добавим safe defaults
    # для тех, что не критичны для LLM-вызова.
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", os.getenv("TELEGRAM_BOT_TOKEN", "test-token"))
    monkeypatch.setenv("TELEGRAM_OWNER_ID", os.getenv("TELEGRAM_OWNER_ID", "111"))
    monkeypatch.setenv("TELEGRAM_ASSISTANT_ID", os.getenv("TELEGRAM_ASSISTANT_ID", "222"))
    monkeypatch.setenv("DATABASE_URL", "sqlite:///:memory:")

    from abay_assistant.config import get_settings
    get_settings.cache_clear()
