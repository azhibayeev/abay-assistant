"""Tests for abay_assistant.tools.executor — tool dispatch."""

import json
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, patch

import pytest

from abay_assistant.tools.executor import ToolExecutor


async def test_create_card_with_label(tool_executor, mock_trello):
    result = await tool_executor.execute(
        "create_trello_card",
        {"name": "Test card", "label": "Стратегия"},
    )
    mock_trello.create_card.assert_awaited_once()
    call_kwargs = mock_trello.create_card.call_args
    assert call_kwargs.kwargs["name"] == "Test card"
    assert call_kwargs.kwargs["labels"] == ["lbl1"]  # Стратегия label id


async def test_create_card_no_label(tool_executor, mock_trello):
    result = await tool_executor.execute(
        "create_trello_card",
        {"name": "Simple card"},
    )
    mock_trello.create_card.assert_awaited_once()
    call_kwargs = mock_trello.create_card.call_args
    assert call_kwargs.kwargs["labels"] is None


async def test_update_card(tool_executor, mock_trello):
    result = await tool_executor.execute(
        "update_trello_card",
        {"card_id": "abc123", "desc": "Updated description"},
    )
    mock_trello.update_card.assert_awaited_once_with("abc123", desc="Updated description")


async def test_move_card(tool_executor, mock_trello):
    result = await tool_executor.execute(
        "move_trello_card",
        {"card_id": "abc123", "target_list": "Backlog"},
    )
    mock_trello.move_card.assert_awaited_once_with("abc123", "Backlog")


async def test_get_cards(tool_executor, mock_trello):
    mock_trello.get_cards = AsyncMock(return_value=[
        {"id": "c1", "name": "Task 1", "labels": [{"name": "Продукт"}], "due": None},
        {"id": "c2", "name": "Task 2", "labels": [], "due": "2026-05-01"},
    ])
    result = await tool_executor.execute(
        "get_trello_cards",
        {"list_name": "Сегодня"},
    )
    parsed = json.loads(result)
    assert len(parsed) == 2
    assert parsed[0]["name"] == "Task 1"
    assert parsed[0]["labels"] == ["Продукт"]


async def test_set_reminder_success(tool_executor, in_memory_db):
    remind_at = (datetime.now() + timedelta(hours=2)).isoformat()
    result = await tool_executor.execute(
        "set_reminder",
        {"text": "Call partner", "remind_at": remind_at},
        context={"telegram_id": 111},
    )
    assert "Напоминание установлено" in result
    assert "Call partner" in result


async def test_set_reminder_no_context(tool_executor):
    result = await tool_executor.execute(
        "set_reminder",
        {"text": "Test", "remind_at": "2026-05-01T10:00:00"},
    )
    assert "Ошибка" in result


async def test_web_search(tool_executor):
    with patch("abay_assistant.tools.executor.web_search", new_callable=AsyncMock) as mock_ws:
        mock_ws.return_value = [{"title": "Result", "url": "https://example.com"}]
        result = await tool_executor.execute(
            "web_search",
            {"query": "test query"},
        )
        mock_ws.assert_awaited_once_with("test query")
        parsed = json.loads(result)
        assert parsed[0]["title"] == "Result"


async def test_web_fetch(tool_executor):
    with patch("abay_assistant.tools.executor.web_fetch", new_callable=AsyncMock) as mock_wf:
        mock_wf.return_value = "Page content here"
        result = await tool_executor.execute(
            "web_fetch",
            {"url": "https://example.com"},
        )
        mock_wf.assert_awaited_once_with("https://example.com")
        assert result == "Page content here"


async def test_unknown_tool(tool_executor):
    result = await tool_executor.execute(
        "nonexistent_tool",
        {},
    )
    assert "Неизвестный инструмент" in result


async def test_request_clarification(tool_executor):
    result = await tool_executor.execute(
        "request_clarification",
        {"question": "Что именно имеется в виду?"},
    )
    assert result == "Что именно имеется в виду?"


async def test_add_trello_comment(tool_executor, mock_trello):
    result = await tool_executor.execute(
        "add_trello_comment",
        {"card_id": "abc123", "text": "Встреча прошла, мяч на стороне Биби"},
    )
    mock_trello.add_comment.assert_awaited_once_with("abc123", "Встреча прошла, мяч на стороне Биби")


async def test_update_card_with_label(tool_executor, mock_trello):
    result = await tool_executor.execute(
        "update_trello_card",
        {"card_id": "abc123", "desc": "Описание", "label": "Срочно"},
    )
    mock_trello.update_card.assert_awaited_once_with("abc123", desc="Описание")
    mock_trello.add_label_to_card.assert_awaited_once_with("abc123", "lbl3")


async def test_update_card_label_not_found(tool_executor, mock_trello):
    """Если метка не найдена — не падает."""
    result = await tool_executor.execute(
        "update_trello_card",
        {"card_id": "abc123", "label": "Несуществующая"},
    )
    mock_trello.add_label_to_card.assert_not_awaited()


async def test_create_calendar_event(mock_trello, obsidian_client):
    from abay_assistant.tools.executor import ToolExecutor
    mock_cal = AsyncMock()
    mock_cal.enabled = True
    mock_cal.create_event = AsyncMock(return_value={"id": "evt1"})
    executor = ToolExecutor(trello=mock_trello, obsidian=obsidian_client, calendar=mock_cal)
    result = await executor.execute(
        "create_calendar_event",
        {"summary": "Встреча с Расулом", "start": "2026-05-02T10:00:00"},
    )
    mock_cal.create_event.assert_awaited_once()
    assert "Встреча с Расулом" in result


async def test_create_calendar_event_no_calendar(tool_executor):
    result = await tool_executor.execute(
        "create_calendar_event",
        {"summary": "Test", "start": "2026-05-02T10:00:00"},
    )
    assert "не настроен" in result


async def test_get_cards_includes_desc(tool_executor, mock_trello):
    mock_trello.get_cards = AsyncMock(return_value=[
        {"id": "c1", "name": "Task", "desc": "Some desc", "labels": [], "due": None},
    ])
    result = await tool_executor.execute("get_trello_cards", {"list_name": "Сегодня"})
    parsed = json.loads(result)
    assert parsed[0]["desc"] == "Some desc"


# ── Fuzzy duplicate detection в _create_card ────────────────────────────────

async def test_create_card_blocks_exact_duplicate(tool_executor, mock_trello):
    """Точное совпадение имени — карточка не создаётся, возвращается possible_duplicate."""
    mock_trello.get_all_cards = AsyncMock(return_value=[
        {"id": "old1", "name": "Встреча с Абеке по инвест-линии", "idList": "list_today"},
    ])
    result = await tool_executor.execute(
        "create_trello_card",
        {"name": "Встреча с Абеке по инвест-линии"},
    )
    mock_trello.create_card.assert_not_awaited()
    parsed = json.loads(result)
    assert parsed["status"] == "possible_duplicate"
    assert parsed["candidates"][0]["id"] == "old1"
    assert parsed["candidates"][0]["score"] >= 85


async def test_create_card_blocks_reordered_words(tool_executor, mock_trello):
    """token_set_ratio ловит перестановку и подмножества слов."""
    mock_trello.get_all_cards = AsyncMock(return_value=[
        {"id": "old1", "name": "Связаться с Ханом по China Railways", "idList": "list_today"},
    ])
    result = await tool_executor.execute(
        "create_trello_card",
        {"name": "Связаться с Ханом по China Railways (асинхрон)"},
    )
    mock_trello.create_card.assert_not_awaited()
    parsed = json.loads(result)
    assert parsed["status"] == "possible_duplicate"


async def test_create_card_allows_distinct_names(tool_executor, mock_trello):
    """Совершенно другая задача — создаётся нормально."""
    mock_trello.get_all_cards = AsyncMock(return_value=[
        {"id": "old1", "name": "Связаться с Ханом по China Railways", "idList": "list_today"},
    ])
    result = await tool_executor.execute(
        "create_trello_card",
        {"name": "Подготовить презентацию для совета директоров"},
    )
    mock_trello.create_card.assert_awaited_once()


async def test_create_card_force_bypasses_check(tool_executor, mock_trello):
    """force=true пробивает fuzzy-проверку, даже если есть похожая."""
    mock_trello.get_all_cards = AsyncMock(return_value=[
        {"id": "old1", "name": "Встреча с Абеке по инвест-линии", "idList": "list_today"},
    ])
    result = await tool_executor.execute(
        "create_trello_card",
        {"name": "Встреча с Абеке по инвест-линии", "force": True},
    )
    mock_trello.create_card.assert_awaited_once()


async def test_create_card_ignores_archive_column(tool_executor, mock_trello):
    """Карточки в «архив» и «ГОТОВО» не считаются дублями."""
    mock_trello.get_all_cards = AsyncMock(return_value=[
        {"id": "arch1", "name": "Встреча с Абеке по инвест-линии", "idList": "list_archive"},
        {"id": "done1", "name": "Встреча с Абеке по инвест-линии", "idList": "list_done"},
    ])
    result = await tool_executor.execute(
        "create_trello_card",
        {"name": "Встреча с Абеке по инвест-линии"},
    )
    mock_trello.create_card.assert_awaited_once()


async def test_create_card_returns_list_name_in_candidate(tool_executor, mock_trello):
    """В кандидатах присутствует имя колонки, чтобы LLM мог принять решение."""
    mock_trello.get_all_cards = AsyncMock(return_value=[
        {"id": "old1", "name": "DMS дискавери — партнёрство с Маратом", "idList": "list_week"},
    ])
    result = await tool_executor.execute(
        "create_trello_card",
        {"name": "DMS дискавери партнёрство с Маратом"},
    )
    parsed = json.loads(result)
    assert parsed["status"] == "possible_duplicate"
    assert parsed["candidates"][0]["list"] == "неделя 5–11 май"


async def test_find_card_returns_top_matches(tool_executor, mock_trello):
    """find_trello_card возвращает совпадения по fuzzy."""
    mock_trello.get_all_cards = AsyncMock(return_value=[
        {"id": "c1", "name": "Встреча с Абеке по инвест-линии", "idList": "list_today"},
        {"id": "c2", "name": "Связаться с Ханом по China Railways", "idList": "list_week"},
        {"id": "c3", "name": "DMS дискавери — партнёрство", "idList": "list_week"},
    ])
    result = await tool_executor.execute(
        "find_trello_card",
        {"query": "Абеке инвест"},
    )
    parsed = json.loads(result)
    assert len(parsed) >= 1
    assert parsed[0]["id"] == "c1"
    assert parsed[0]["score"] >= 60


async def test_find_card_no_match_returns_empty(tool_executor, mock_trello):
    """Если нет совпадений выше порога — пустой список."""
    mock_trello.get_all_cards = AsyncMock(return_value=[
        {"id": "c1", "name": "Совершенно другая задача", "idList": "list_today"},
    ])
    result = await tool_executor.execute(
        "find_trello_card",
        {"query": "квантовая физика"},
    )
    parsed = json.loads(result)
    assert parsed == []


async def test_find_card_excludes_archive(tool_executor, mock_trello):
    """Карточки в архиве/ГОТОВО не должны находиться."""
    mock_trello.get_all_cards = AsyncMock(return_value=[
        {"id": "arch1", "name": "Встреча с Абеке по инвест-линии", "idList": "list_archive"},
    ])
    result = await tool_executor.execute(
        "find_trello_card",
        {"query": "Абеке инвест"},
    )
    parsed = json.loads(result)
    assert parsed == []


async def test_find_card_includes_list_name(tool_executor, mock_trello):
    """Имя колонки возвращается, чтобы LLM мог сразу принять решение move_trello_card."""
    mock_trello.get_all_cards = AsyncMock(return_value=[
        {"id": "c1", "name": "Встреча с Абеке по инвест-линии", "idList": "list_today"},
    ])
    result = await tool_executor.execute(
        "find_trello_card",
        {"query": "Абеке"},
    )
    parsed = json.loads(result)
    assert parsed[0]["list"] == "сегодня"


async def test_create_card_invalidates_cache_on_success(tool_executor, mock_trello):
    """После успешного создания кеш сбрасывается — следующий вызов увидит новую карточку."""
    mock_trello.get_all_cards = AsyncMock(return_value=[])
    await tool_executor.execute(
        "create_trello_card",
        {"name": "Уникальная задача"},
    )
    # Если бы кеш не сбрасывался, второй вызов вернул бы пустой список и пропустил бы дубль.
    mock_trello.get_all_cards = AsyncMock(return_value=[
        {"id": "new1", "name": "Уникальная задача", "idList": "list_today"},
    ])
    result = await tool_executor.execute(
        "create_trello_card",
        {"name": "Уникальная задача"},
    )
    parsed = json.loads(result)
    assert parsed["status"] == "possible_duplicate"
