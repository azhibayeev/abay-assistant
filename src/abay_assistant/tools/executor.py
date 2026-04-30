"""Исполнитель tool calls от Claude."""

import json
from datetime import datetime, timedelta
from typing import Any

from loguru import logger

from abay_assistant.services.trello import TrelloClient
from abay_assistant.services.obsidian import ObsidianClient
from abay_assistant.services.calendar import CalendarClient
from abay_assistant.services.web import web_search, web_fetch
from abay_assistant.db import create_reminder, log_tool_usage

LABELS_CACHE_TTL = timedelta(minutes=5)


class ToolExecutor:
    """Принимает tool_use блок от Claude, вызывает нужный сервис."""

    def __init__(
        self,
        trello: TrelloClient,
        obsidian: ObsidianClient,
        calendar: CalendarClient | None = None,
    ) -> None:
        self.trello = trello
        self.obsidian = obsidian
        self.calendar = calendar
        self._labels_cache: list[dict] | None = None
        self._labels_cached_at: datetime | None = None

    async def _get_labels_cached(self) -> list[dict]:
        """Получить метки с кешированием на 5 минут."""
        now = datetime.now()
        if (
            self._labels_cache is not None
            and self._labels_cached_at is not None
            and now - self._labels_cached_at < LABELS_CACHE_TTL
        ):
            return self._labels_cache
        self._labels_cache = await self.trello.get_labels()
        self._labels_cached_at = now
        return self._labels_cache

    async def execute(
        self, tool_name: str, tool_input: dict[str, Any], context: dict | None = None
    ) -> str:
        """Выполнить tool call. Возвращает строку результата для tool_result."""
        logger.info("Tool call: {}({})", tool_name, tool_input)

        # Логировать использование tool
        telegram_id = (context or {}).get("telegram_id")
        if telegram_id:
            try:
                log_tool_usage(telegram_id, tool_name)
            except Exception:
                pass  # не блокировать tool call из-за метрики

        try:
            result = await self._dispatch(tool_name, tool_input, context)
            return json.dumps(result, ensure_ascii=False) if isinstance(result, (dict, list)) else str(result)
        except Exception as e:
            logger.error("Tool {} ошибка: {}", tool_name, e)
            return f"Ошибка: {e}"

    async def _dispatch(
        self, name: str, inp: dict[str, Any], context: dict | None = None
    ) -> Any:
        match name:
            case "create_trello_card":
                return await self._create_card(inp)
            case "update_trello_card":
                return await self._update_card(inp)
            case "move_trello_card":
                return await self.trello.move_card(inp["card_id"], inp["target_list"])
            case "add_trello_comment":
                return await self.trello.add_comment(inp["card_id"], inp["text"])
            case "add_checklist_item":
                return await self.trello.add_checklist_item(inp["card_id"], inp["text"])
            case "get_trello_cards":
                return await self._get_cards(inp)
            case "append_obsidian_daily":
                path = await self.obsidian.append_daily(inp["content"])
                return f"Записано в {path}"
            case "save_entity_note":
                path = await self.obsidian.save_entity_note(
                    entity_type=inp["entity_type"],
                    entity_name=inp["entity_name"],
                    content=inp["content"],
                    meta_update=inp.get("meta"),
                )
                return f"Записано в {path}"
            case "update_entity_meta":
                fields = {
                    k: v for k, v in inp.items()
                    if k not in ("entity_type", "entity_name") and v is not None
                }
                path = await self.obsidian.update_entity_meta(
                    inp["entity_type"], inp["entity_name"], **fields
                )
                return f"Метаданные обновлены: {path}"
            case "get_entity":
                text = await self.obsidian.get_entity(inp["entity_type"], inp["entity_name"])
                return text if text else f"Заметка о '{inp['entity_name']}' не найдена."
            case "list_entities":
                entities = await self.obsidian.list_entities(inp["entity_type"])
                if not entities:
                    return "Список пуст."
                return entities
            case "search_knowledge":
                results = await self.obsidian.search(inp["query"])
                if not results:
                    return "Ничего не найдено."
                return results
            case "set_reminder":
                return self._set_reminder(inp, context)
            case "create_calendar_event":
                return await self._create_event(inp)
            case "web_search":
                return await web_search(inp["query"])
            case "web_fetch":
                return await web_fetch(inp["url"])
            case "request_clarification":
                return inp["question"]
            case _:
                return f"Неизвестный инструмент: {name}"

    def _set_reminder(self, inp: dict[str, Any], context: dict | None) -> str:
        telegram_id = (context or {}).get("telegram_id")
        if not telegram_id:
            return "Ошибка: не удалось определить пользователя для напоминания."

        try:
            remind_at = datetime.fromisoformat(inp["remind_at"])
        except (ValueError, KeyError) as e:
            return f"Ошибка формата даты: {e}"

        reminder = create_reminder(
            telegram_id=telegram_id,
            text=inp["text"],
            remind_at=remind_at,
        )
        time_str = remind_at.strftime("%d.%m.%Y %H:%M")
        return f"Напоминание установлено на {time_str}: {reminder.text}"

    async def _create_card(self, inp: dict[str, Any]) -> dict:
        # Найти label ID по имени если указан (кешированный запрос)
        labels = None
        if label_name := inp.get("label"):
            all_labels = await self._get_labels_cached()
            for lb in all_labels:
                if lb["name"].lower() == label_name.lower():
                    labels = [lb["id"]]
                    break

        return await self.trello.create_card(
            name=inp["name"],
            list_name=inp.get("list_name", "Сегодня"),
            desc=inp.get("desc", ""),
            labels=labels,
            due=inp.get("due"),
        )

    async def _update_card(self, inp: dict[str, Any]) -> dict:
        fields = {}
        if "desc" in inp:
            fields["desc"] = inp["desc"]
        if "due" in inp:
            fields["due"] = inp["due"]
        result = await self.trello.update_card(inp["card_id"], **fields)

        # Добавить метку если указана
        if label_name := inp.get("label"):
            all_labels = await self._get_labels_cached()
            for lb in all_labels:
                if lb["name"].lower() == label_name.lower():
                    try:
                        await self.trello.add_label_to_card(inp["card_id"], lb["id"])
                    except Exception:
                        pass  # метка уже стоит — Trello вернёт ошибку
                    break

        return result

    async def _create_event(self, inp: dict[str, Any]) -> str:
        if not self.calendar or not self.calendar.enabled:
            return "Ошибка: Google Calendar не настроен."
        start = datetime.fromisoformat(inp["start"])
        end_str = inp.get("end")
        end = datetime.fromisoformat(end_str) if end_str else start + timedelta(hours=1)
        result = await self.calendar.create_event(
            summary=inp["summary"], start=start, end=end,
        )
        return f"Событие создано: {inp['summary']} ({start.strftime('%d.%m %H:%M')}–{end.strftime('%H:%M')})"

    async def _get_cards(self, inp: dict[str, Any]) -> list[dict]:
        cards = await self.trello.get_cards(inp["list_name"])
        # Вернуть упрощённый вид для LLM (с desc для контекста)
        return [
            {
                "id": c["id"],
                "name": c["name"],
                "desc": c.get("desc", "")[:200],  # обрезать длинные desc
                "labels": [lb["name"] for lb in c.get("labels", [])],
                "due": c.get("due"),
            }
            for c in cards
        ]
