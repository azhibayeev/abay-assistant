"""Shared fixtures for all tests."""

import os
from datetime import datetime
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlmodel import SQLModel, create_engine, Session

from abay_assistant.services.obsidian import ObsidianClient
from abay_assistant.services.trello import TrelloClient
from abay_assistant.tools.executor import ToolExecutor


@pytest.fixture(autouse=True)
def _patch_settings(monkeypatch):
    """Prevent tests from loading real .env."""
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "test-token")
    monkeypatch.setenv("TELEGRAM_OWNER_ID", "111")
    monkeypatch.setenv("TELEGRAM_ASSISTANT_ID", "222")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    monkeypatch.setenv("TRELLO_API_KEY", "test-trello-key")
    monkeypatch.setenv("TRELLO_TOKEN", "test-trello-token")
    monkeypatch.setenv("TRELLO_BOARD_ID", "test-board-id")
    monkeypatch.setenv("GROQ_API_KEY", "")
    monkeypatch.setenv("DATABASE_URL", "sqlite:///:memory:")
    # Clear cached settings
    from abay_assistant.config import get_settings
    get_settings.cache_clear()


@pytest.fixture
def tmp_vault(tmp_path):
    """Create a temporary Obsidian vault with standard folders."""
    for folder in ("Daily", "Inbox", "People", "Projects", "Weekly", "System"):
        (tmp_path / folder).mkdir()
    return tmp_path


@pytest.fixture
def obsidian_client(tmp_vault):
    """ObsidianClient pointing to a temporary vault."""
    return ObsidianClient(vault_path=tmp_vault)


@pytest.fixture
def mock_trello():
    """Mock TrelloClient with AsyncMock methods."""
    trello = MagicMock(spec=TrelloClient)
    trello.get_cards = AsyncMock(return_value=[])
    trello.create_card = AsyncMock(return_value={"id": "card123", "name": "Test"})
    trello.update_card = AsyncMock(return_value={"id": "card123"})
    trello.move_card = AsyncMock(return_value={"id": "card123"})
    trello.archive_card = AsyncMock(return_value={"id": "card123"})
    trello.add_checklist_item = AsyncMock(return_value={"id": "item1"})
    trello.add_comment = AsyncMock(return_value={"id": "comment1"})
    trello.add_label_to_card = AsyncMock(return_value=None)
    trello.get_labels = AsyncMock(return_value=[
        {"id": "lbl1", "name": "Стратегия"},
        {"id": "lbl2", "name": "Продукт"},
        {"id": "lbl3", "name": "Срочно"},
        {"id": "lbl4", "name": "Денежная"},
    ])
    return trello


@pytest.fixture
def tool_executor(mock_trello, obsidian_client):
    """ToolExecutor with mocked Trello and real Obsidian (tmp vault)."""
    return ToolExecutor(trello=mock_trello, obsidian=obsidian_client)


@pytest.fixture
def in_memory_db(monkeypatch):
    """Set up an in-memory SQLite database for testing."""
    engine = create_engine("sqlite:///:memory:", echo=False)
    SQLModel.metadata.create_all(engine)

    def _patched_get_engine():
        return engine

    import abay_assistant.db as db_module
    monkeypatch.setattr(db_module, "_engine", engine)
    monkeypatch.setattr(db_module, "_get_engine", _patched_get_engine)
    return engine
