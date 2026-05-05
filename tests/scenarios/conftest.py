"""Override глобального _patch_settings для сценариев — нужен реальный API key."""

import os
from pathlib import Path

import pytest
from dotenv import load_dotenv
from sqlmodel import SQLModel, create_engine


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

    # In-memory DB с инициализированными таблицами — иначе set_reminder и
    # log_tool_usage падают на «no such table». Сделано как у in_memory_db
    # фикстуры в верхнем conftest.
    engine = create_engine("sqlite:///:memory:", echo=False)
    SQLModel.metadata.create_all(engine)
    import abay_assistant.db as db_module
    monkeypatch.setattr(db_module, "_engine", engine)
    monkeypatch.setattr(db_module, "_get_engine", lambda: engine)
