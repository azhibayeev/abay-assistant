"""Исполнитель tool calls от Claude."""

import json
from datetime import datetime, timedelta
from typing import Any

from loguru import logger
from rapidfuzz import fuzz, process

from abay_assistant.services.trello import TrelloClient
from abay_assistant.services.obsidian import ObsidianClient
from abay_assistant.services.calendar import CalendarClient
from abay_assistant.services.web import web_search, web_fetch
from abay_assistant.db import create_reminder, log_tool_usage

LABELS_CACHE_TTL = timedelta(minutes=5)
ACTIVE_CARDS_CACHE_TTL = timedelta(seconds=60)
DUPLICATE_THRESHOLD = 85
FIND_THRESHOLD = 50  # для find_trello_card — порог ниже, потому что это поиск, а не дедуп
# Колонки, в которых не ищем дубли — туда уходит уже отработанное.
EXCLUDED_LIST_NAMES = frozenset({"архив", "готово"})


def summarize_tool_args(tool_name: str, inp: dict[str, Any]) -> str:
    """Краткое описание аргументов tool call для логов и сводок.

    Возвращает пустую строку для tools, по которым нет смысла хранить args
    (read-only запросы — get_*, list_*, search_*).
    """
    if tool_name == "create_trello_card":
        name = (inp.get("name") or "?")[:80]
        lst = inp.get("list_name", "Сегодня")
        return f"create→{lst}: {name}"
    if tool_name == "move_trello_card":
        return f"move→{inp.get('target_list', '?')}"
    if tool_name == "rename_trello_card":
        new = (inp.get("new_name") or "?")[:80]
        return f"rename: {new}"
    if tool_name == "update_trello_card":
        parts = []
        if inp.get("due"):
            parts.append(f"due={inp['due'][:10]}")
        if inp.get("desc"):
            parts.append("desc")
        if inp.get("name"):
            parts.append("name")
        return f"update: {','.join(parts)}" if parts else "update"
    if tool_name == "add_trello_comment":
        text = (inp.get("text") or "")[:60]
        return f"comment: {text}"
    if tool_name == "add_checklist_item":
        return f"checklist+: {(inp.get('text') or '?')[:60]}"
    if tool_name == "set_trello_card_cover":
        return f"cover={inp.get('color', '?')}"
    if tool_name == "create_calendar_event":
        return f"event: {(inp.get('summary') or '?')[:60]}"
    if tool_name == "set_reminder":
        return f"reminder: {(inp.get('text') or '?')[:60]}"
    if tool_name == "save_personal_pattern":
        person = inp.get("person", "?")
        obs = (inp.get("observation") or "?")[:80]
        return f"pattern: {person} — {obs}"
    if tool_name == "save_entity_note":
        return f"crm: {(inp.get('entity_name') or '?')[:60]}"
    if tool_name == "update_entity_meta":
        return f"crm-meta: {(inp.get('entity_name') or '?')[:60]}"
    if tool_name == "append_obsidian_daily":
        return "daily-note"
    return ""  # read-only / служебные — не логируем args


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
        self._active_cards_cache: list[dict] | None = None
        self._active_cards_cached_at: datetime | None = None
        self._list_id_to_name: dict[str, str] = {}

    async def _get_active_cards_cached(self) -> list[dict]:
        """Открытые карточки доски, минус «архив»/«ГОТОВО». Кеш 60с."""
        now = datetime.now()
        if (
            self._active_cards_cache is not None
            and self._active_cards_cached_at is not None
            and now - self._active_cards_cached_at < ACTIVE_CARDS_CACHE_TTL
        ):
            return self._active_cards_cache

        lists = await self.trello.get_lists()  # {name: id}
        self._list_id_to_name = {lid: name for name, lid in lists.items()}
        excluded_ids = {
            lid for name, lid in lists.items()
            if name.lower() in EXCLUDED_LIST_NAMES
        }
        all_cards = await self.trello.get_all_cards()
        self._active_cards_cache = [
            c for c in all_cards if c.get("idList") not in excluded_ids
        ]
        self._active_cards_cached_at = now
        return self._active_cards_cache

    async def _find_duplicate_candidates(self, name: str) -> list[dict]:
        """Карточки с fuzzy-сходством >= DUPLICATE_THRESHOLD к данному имени."""
        cards = await self._get_active_cards_cached()
        if not cards:
            return []
        choices = {c["id"]: c.get("name", "") for c in cards if c.get("name")}
        if not choices:
            return []
        matches = process.extract(
            name,
            choices,
            scorer=fuzz.token_set_ratio,
            limit=3,
            score_cutoff=DUPLICATE_THRESHOLD,
        )
        cards_by_id = {c["id"]: c for c in cards}
        out = []
        for matched_name, score, card_id in matches:
            card = cards_by_id.get(card_id)
            if not card:
                continue
            out.append({
                "id": card["id"],
                "name": matched_name,
                "list": self._list_id_to_name.get(card.get("idList", ""), "?"),
                "score": int(score),
            })
        return out

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

        # Логировать использование tool с кратким описанием аргументов
        telegram_id = (context or {}).get("telegram_id")
        if telegram_id:
            try:
                log_tool_usage(telegram_id, tool_name, summarize_tool_args(tool_name, tool_input))
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
            case "rename_trello_card":
                return await self._rename_card(inp)
            case "move_trello_card":
                return await self.trello.move_card(inp["card_id"], inp["target_list"])
            case "add_trello_comment":
                return await self.trello.add_comment(inp["card_id"], inp["text"])
            case "set_trello_card_cover":
                return await self.trello.set_cover(inp["card_id"], inp["color"])
            case "add_checklist_item":
                return await self.trello.add_checklist_item(inp["card_id"], inp["text"])
            case "get_trello_cards":
                return await self._get_cards(inp)
            case "find_trello_card":
                return await self._find_card(inp)
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
            case "save_personal_pattern":
                path = await self.obsidian.append_personal_pattern(
                    inp["person"], inp["observation"]
                )
                return f"Паттерн записан: {path}"
            case "get_personal_patterns":
                text = await self.obsidian.read_personal_patterns(inp["person"])
                return text or f"Паттернов о {inp['person']} пока нет."
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

    async def _rename_card(self, inp: dict[str, Any]) -> dict:
        """Переименовать карточку с сохранением истории в комментарии."""
        card_id = inp["card_id"]
        new_name = inp["new_name"]
        # Получить старое название
        old_card = await self.trello.get_card(card_id)
        old_name = old_card.get("name", "?")
        if old_name == new_name:
            return {"status": "unchanged", "name": new_name}
        # Записать историю в комментарий
        await self.trello.add_comment(
            card_id, f"📝 Переименовано: «{old_name}» → «{new_name}»"
        )
        # Переименовать
        result = await self.trello.update_card(card_id, name=new_name)
        return result

    async def _create_card(self, inp: dict[str, Any]) -> dict:
        name = inp["name"]
        force = bool(inp.get("force", False))

        # Fuzzy-проверка дублей. Источник проблемы: forward-flow stateless,
        # а в inbox LLM экономит на get_trello_cards. Без этой защиты
        # одна и та же задача попадает на доску по 2-3 раза.
        if not force:
            try:
                candidates = await self._find_duplicate_candidates(name)
            except Exception as e:
                logger.warning("Fuzzy duplicate check failed: {}", e)
                candidates = []
            if candidates:
                logger.info("Duplicate suspect for '{}': {}", name, candidates)
                return {
                    "status": "possible_duplicate",
                    "message": (
                        "Найдены похожие карточки. Обнови существующую "
                        "(update_trello_card / add_trello_comment / move_trello_card / rename_trello_card). "
                        "Если уверен, что нужна именно новая карточка — повтори "
                        "create_trello_card с force=true."
                    ),
                    "candidates": candidates,
                }

        # Найти label ID по имени если указан (кешированный запрос)
        labels = None
        if label_name := inp.get("label"):
            all_labels = await self._get_labels_cached()
            for lb in all_labels:
                if lb["name"].lower() == label_name.lower():
                    labels = [lb["id"]]
                    break

        result = await self.trello.create_card(
            name=name,
            list_name=inp.get("list_name", "Сегодня"),
            desc=inp.get("desc", ""),
            labels=labels,
            due=inp.get("due"),
        )
        # Сбрасываем кеш — следующая проверка должна увидеть свежую карточку.
        self._active_cards_cache = None
        return result

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
                "cover_color": (c.get("cover") or {}).get("color"),
            }
            for c in cards
        ]

    async def _find_card(self, inp: dict[str, Any]) -> list[dict]:
        """Fuzzy-поиск по всем активным колонкам. Возвращает топ совпадений с score."""
        query = inp["query"]
        limit = int(inp.get("limit", 5))
        cards = await self._get_active_cards_cached()
        if not cards:
            return []
        choices = {c["id"]: c.get("name", "") for c in cards if c.get("name")}
        if not choices:
            return []
        # WRatio комбинирует partial/token-sort/token-set + штрафы за длину.
        # Лучше для поисковых запросов («Абеке инвест» → «Встреча с Абеке по инвест-линии»),
        # тогда как token_set_ratio для дедупа точнее.
        matches = process.extract(
            query,
            choices,
            scorer=fuzz.WRatio,
            limit=limit,
            score_cutoff=FIND_THRESHOLD,
        )
        cards_by_id = {c["id"]: c for c in cards}
        out = []
        for matched_name, score, card_id in matches:
            card = cards_by_id.get(card_id)
            if not card:
                continue
            out.append({
                "id": card["id"],
                "name": matched_name,
                "list": self._list_id_to_name.get(card.get("idList", ""), "?"),
                "due": card.get("due"),
                "score": int(score),
            })
        return out
