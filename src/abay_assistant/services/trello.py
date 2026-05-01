"""Trello API клиент — реальная реализация через httpx."""

import asyncio
import time
from typing import Any

import httpx
from loguru import logger

from abay_assistant.config import get_settings

MAX_RETRIES = 3
RETRY_DELAYS = [1, 3, 5]


class TrelloClient:
    """Клиент для работы с Trello REST API."""

    BASE = "https://api.trello.com/1"

    def __init__(self) -> None:
        s = get_settings()
        self._auth = {"key": s.trello_api_key, "token": s.trello_token}
        self._board_id = s.trello_board_id
        self._lists_cache: dict[str, str] = {}  # name -> id
        self._lists_cache_ts: float = 0  # время последнего обновления

    # ------------------------------------------------------------------
    # internal helpers
    # ------------------------------------------------------------------

    async def _request(
        self, method: str, path: str, **kwargs: Any
    ) -> Any:
        params = {**self._auth, **kwargs.pop("params", {})}
        last_error = None
        for attempt in range(MAX_RETRIES):
            try:
                async with httpx.AsyncClient(timeout=15) as client:
                    resp = await client.request(
                        method, f"{self.BASE}{path}", params=params, **kwargs
                    )
                    resp.raise_for_status()
                    return resp.json()
            except (httpx.TimeoutException, httpx.ConnectError) as e:
                last_error = e
                if attempt < MAX_RETRIES - 1:
                    delay = RETRY_DELAYS[attempt]
                    logger.warning(
                        "Trello {} {} ошибка (попытка {}/{}): {}. Повтор через {}с",
                        method, path, attempt + 1, MAX_RETRIES, type(e).__name__, delay,
                    )
                    await asyncio.sleep(delay)
                else:
                    logger.error("Trello {} {} ошибка после {} попыток: {}", method, path, MAX_RETRIES, e)
            except httpx.HTTPStatusError as e:
                if e.response.status_code == 429 and attempt < MAX_RETRIES - 1:
                    delay = RETRY_DELAYS[attempt]
                    logger.warning("Trello 429 rate limit, retry через {}с", delay)
                    await asyncio.sleep(delay)
                    last_error = e
                else:
                    raise
        raise last_error

    async def _ensure_lists_cache(self) -> None:
        # Обновлять кэш каждые 5 минут чтобы подхватывать новые колонки
        if not self._lists_cache or (time.monotonic() - self._lists_cache_ts) > 300:
            await self.get_lists()

    async def _list_id_by_name(self, list_name: str) -> str:
        await self._ensure_lists_cache()
        result = self._find_list(list_name)
        if result:
            return result
        # Не нашли — принудительно обновить кэш (колонку могли только что создать)
        await self.get_lists()
        result = self._find_list(list_name)
        if result:
            return result
        raise ValueError(f"Список '{list_name}' не найден на доске")

    def _find_list(self, list_name: str) -> str | None:
        lower = list_name.lower()
        # Точное совпадение (case-insensitive)
        for name, lid in self._lists_cache.items():
            if name.lower() == lower:
                return lid
        # Поиск по подстроке (для "Неделя" → "неделя 27– 3 май.")
        for name, lid in self._lists_cache.items():
            if lower in name.lower():
                return lid
        return None

    # ------------------------------------------------------------------
    # public methods
    # ------------------------------------------------------------------

    async def get_lists(self) -> dict[str, str]:
        """Получить все списки доски. Возвращает {name: id}."""
        data = await self._request("GET", f"/boards/{self._board_id}/lists")
        self._lists_cache = {item["name"]: item["id"] for item in data}
        self._lists_cache_ts = time.monotonic()
        logger.debug("Trello lists: {}", list(self._lists_cache.keys()))
        return self._lists_cache

    async def get_cards(self, list_name: str) -> list[dict]:
        """Карточки из списка по имени."""
        list_id = await self._list_id_by_name(list_name)
        cards = await self._request(
            "GET", f"/lists/{list_id}/cards",
            params={"fields": "name,desc,labels,due,cover,shortUrl,dateLastActivity"},
        )
        logger.debug("Trello get_cards('{}'): {} шт.", list_name, len(cards))
        return cards

    async def get_card(self, card_id: str) -> dict:
        """Получить одну карточку по ID."""
        return await self._request(
            "GET", f"/cards/{card_id}",
            params={"fields": "name,desc,labels,due,idList"},
        )

    async def get_all_cards(self) -> list[dict]:
        """Все карточки доски."""
        cards = await self._request(
            "GET", f"/boards/{self._board_id}/cards",
            params={"fields": "name,desc,labels,due,cover,idList,shortUrl"},
        )
        logger.debug("Trello get_all_cards: {} шт.", len(cards))
        return cards

    async def create_card(
        self,
        name: str,
        list_name: str = "Сегодня",
        desc: str = "",
        labels: list[str] | None = None,
        due: str | None = None,
    ) -> dict:
        """Создать карточку. labels — список label id."""
        list_id = await self._list_id_by_name(list_name)
        params: dict[str, Any] = {"name": name, "idList": list_id}
        if desc:
            params["desc"] = desc
        if labels:
            params["idLabels"] = ",".join(labels)
        if due:
            params["due"] = due
        card = await self._request("POST", "/cards", params=params)
        logger.info("Trello создана карточка '{}' в '{}'", name, list_name)
        return card

    async def update_card(self, card_id: str, **fields: Any) -> dict:
        """Обновить поля карточки (name, desc, due, idLabels, ...)."""
        card = await self._request("PUT", f"/cards/{card_id}", params=fields)
        logger.info("Trello обновлена карточка {}", card_id)
        return card

    async def move_card(self, card_id: str, list_name: str) -> dict:
        """Переместить карточку в другой список."""
        list_id = await self._list_id_by_name(list_name)
        card = await self._request(
            "PUT", f"/cards/{card_id}", params={"idList": list_id}
        )
        logger.info("Trello карточка {} перемещена в '{}'", card_id, list_name)
        return card

    async def archive_card(self, card_id: str) -> dict:
        """Архивировать карточку."""
        card = await self._request(
            "PUT", f"/cards/{card_id}", params={"closed": "true"}
        )
        logger.info("Trello карточка {} архивирована", card_id)
        return card

    async def add_checklist_item(self, card_id: str, text: str) -> dict:
        """Добавить пункт в чек-лист карточки. Создаёт чек-лист если нет."""
        # Получить чек-листы карточки
        checklists = await self._request(
            "GET", f"/cards/{card_id}/checklists"
        )
        if checklists:
            cl_id = checklists[0]["id"]
        else:
            # Создать чек-лист
            cl = await self._request(
                "POST", "/checklists", params={"idCard": card_id, "name": "Шаги"}
            )
            cl_id = cl["id"]

        item = await self._request(
            "POST", f"/checklists/{cl_id}/checkItems",
            params={"name": text, "pos": "bottom"},
        )
        logger.info("Trello добавлен пункт '{}' в карточку {}", text, card_id)
        return item

    async def add_comment(self, card_id: str, text: str) -> dict:
        """Добавить комментарий к карточке."""
        comment = await self._request(
            "POST", f"/cards/{card_id}/actions/comments",
            params={"text": text},
        )
        logger.info("Trello добавлен комментарий к карточке {}", card_id)
        return comment

    async def add_label_to_card(self, card_id: str, label_id: str) -> None:
        """Добавить метку к карточке (не убирая существующие)."""
        await self._request(
            "POST", f"/cards/{card_id}/idLabels",
            params={"value": label_id},
        )
        logger.info("Trello метка {} добавлена к карточке {}", label_id, card_id)

    async def rename_list(self, list_name: str, new_name: str) -> dict:
        """Переименовать список (колонку)."""
        list_id = await self._list_id_by_name(list_name)
        result = await self._request(
            "PUT", f"/lists/{list_id}", params={"name": new_name}
        )
        # Обновить кеш
        if list_name in self._lists_cache:
            lid = self._lists_cache.pop(list_name)
            self._lists_cache[new_name] = lid
        logger.info("Trello список '{}' переименован в '{}'", list_name, new_name)
        return result

    async def set_cover(self, card_id: str, color: str) -> dict:
        """Установить цвет обложки карточки.

        Цвета: red, orange, green, blue, yellow, purple, pink, sky, lime, black.
        """
        card = await self._request(
            "PUT", f"/cards/{card_id}",
            params={"cover[color]": color, "cover[size]": "full"},
        )
        logger.info("Trello обложка {} → {}", card_id, color)
        return card

    async def get_labels(self) -> list[dict]:
        """Получить метки доски."""
        labels = await self._request(
            "GET", f"/boards/{self._board_id}/labels"
        )
        logger.debug("Trello labels: {} шт.", len(labels))
        return labels
