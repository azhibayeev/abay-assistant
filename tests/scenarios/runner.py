"""Запуск inbox-flow на сценарии — повторяет _process_response из handlers.py.

Mock Trello — stateful: фиксирует все операции (create/move/comment/...) и
возвращает их в transcript. Реальный LLM, реальный ToolExecutor.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

from abay_assistant.bot.handlers import PROMPT_PATH
from abay_assistant.services.llm import LLMClient
from abay_assistant.services.obsidian import ObsidianClient
from abay_assistant.services.trello import TrelloClient
from abay_assistant.tools.definitions import TOOLS
from abay_assistant.tools.executor import ToolExecutor

MAX_TOOL_ROUNDS = 8

DEFAULT_LISTS = [
    "сегодня", "неделя", "Май", "Мяч на стороне",
    "Изучить", "Backlog", "архив", "ГОТОВО",
]


@dataclass
class ToolCall:
    name: str
    input: dict[str, Any]
    result: str

    def short(self) -> str:
        inp_short = json.dumps(self.input, ensure_ascii=False)[:200]
        res_short = self.result[:200] if self.result else ""
        return f"{self.name}({inp_short}) → {res_short}"


@dataclass
class Transcript:
    final_text: str
    tool_calls: list[ToolCall] = field(default_factory=list)
    rounds: int = 0
    board_after: dict[str, list[dict]] = field(default_factory=dict)

    def render(self) -> str:
        lines = [f"=== rounds: {self.rounds} ==="]
        if self.tool_calls:
            lines.append("=== tool calls ===")
            for tc in self.tool_calls:
                lines.append(f"  • {tc.short()}")
        else:
            lines.append("(no tool calls)")
        lines.append("=== final text ===")
        lines.append(self.final_text or "(empty)")
        lines.append("=== board after ===")
        for col, cards in self.board_after.items():
            if cards:
                names = ", ".join(f"«{c['name']}» (in {col})" for c in cards)
                lines.append(f"  {col}: {names}")
        return "\n".join(lines)


def _make_stateful_trello(initial_board: dict[str, list[dict]]) -> tuple[MagicMock, dict]:
    """Создать MockTrello с состоянием. board_state мутируется при tool calls.

    initial_board: {column_name: [{id, name, desc?, labels?, due?, ...}]}
    """
    # Нормализовать список колонок: добавить дефолтные имена + любые из initial_board.
    list_names = list(DEFAULT_LISTS)
    for col in initial_board:
        if col not in list_names:
            list_names.append(col)
    lists_map = {name: f"list_{i}" for i, name in enumerate(list_names)}
    name_by_id = {lid: name for name, lid in lists_map.items()}

    # Состояние: {list_id: [card_dict]} (карточка хранит idList)
    state: dict[str, list[dict]] = {lid: [] for lid in lists_map.values()}
    for col, cards in initial_board.items():
        lid = lists_map[col]
        for c in cards:
            card = dict(c)
            card.setdefault("id", f"card_{uuid.uuid4().hex[:8]}")
            card["idList"] = lid
            card.setdefault("desc", "")
            card.setdefault("labels", [])
            card.setdefault("due", None)
            state[lid].append(card)

    def _all_open_cards() -> list[dict]:
        out = []
        for cards in state.values():
            out.extend(cards)
        return out

    def _find_card(card_id: str) -> dict | None:
        for cards in state.values():
            for c in cards:
                if c["id"] == card_id:
                    return c
        return None

    def _resolve_list_id(name_query: str) -> str:
        """Воспроизводим логику trello._find_list — exact + substring."""
        lower = name_query.lower()
        for n, lid in lists_map.items():
            if n.lower() == lower:
                return lid
        for n, lid in lists_map.items():
            if lower in n.lower():
                return lid
        raise ValueError(f"Список '{name_query}' не найден")

    trello = MagicMock(spec=TrelloClient)

    async def get_lists():
        return dict(lists_map)

    async def get_all_cards():
        return [dict(c) for c in _all_open_cards()]

    async def get_cards(list_name: str):
        lid = _resolve_list_id(list_name)
        return [dict(c) for c in state[lid]]

    async def get_card(card_id: str):
        c = _find_card(card_id)
        return dict(c) if c else {}

    async def create_card(name: str, list_name: str = "Сегодня", desc: str = "",
                          labels=None, due=None):
        lid = _resolve_list_id(list_name)
        card = {
            "id": f"new_{uuid.uuid4().hex[:8]}",
            "name": name,
            "desc": desc,
            "labels": [{"id": l, "name": l} for l in (labels or [])],
            "due": due,
            "idList": lid,
        }
        state[lid].append(card)
        return dict(card)

    async def update_card(card_id: str, **fields):
        c = _find_card(card_id)
        if not c:
            return {}
        for k, v in fields.items():
            c[k] = v
        return dict(c)

    async def move_card(card_id: str, list_name: str):
        c = _find_card(card_id)
        if not c:
            return {}
        new_lid = _resolve_list_id(list_name)
        # Удалить из старой колонки
        for cards in state.values():
            if c in cards:
                cards.remove(c)
                break
        c["idList"] = new_lid
        state[new_lid].append(c)
        return dict(c)

    async def archive_card(card_id: str):
        c = _find_card(card_id)
        if not c:
            return {}
        for cards in state.values():
            if c in cards:
                cards.remove(c)
                break
        return {"id": card_id, "closed": True}

    async def add_comment(card_id: str, text: str):
        c = _find_card(card_id)
        if c is not None:
            c.setdefault("_comments", []).append(text)
        return {"id": "comment", "text": text}

    async def add_label_to_card(card_id: str, label_id: str):
        return None

    async def add_checklist_item(card_id: str, text: str):
        return {"id": "item", "text": text}

    async def set_cover(card_id: str, color: str):
        c = _find_card(card_id)
        if c is not None:
            c["cover"] = {"color": color}
        return dict(c) if c else {}

    async def get_labels():
        return [
            {"id": "lbl_strat", "name": "Стратегия"},
            {"id": "lbl_b2b", "name": "B2B / B2G"},
            {"id": "lbl_team", "name": "Команда"},
            {"id": "lbl_fin", "name": "Финансы"},
            {"id": "lbl_prod", "name": "Продукт"},
            {"id": "lbl_pers", "name": "Личное"},
            {"id": "lbl_urg", "name": "Срочно"},
            {"id": "lbl_money", "name": "Денежная"},
        ]

    trello.get_lists = AsyncMock(side_effect=get_lists)
    trello.get_all_cards = AsyncMock(side_effect=get_all_cards)
    trello.get_cards = AsyncMock(side_effect=get_cards)
    trello.get_card = AsyncMock(side_effect=get_card)
    trello.create_card = AsyncMock(side_effect=create_card)
    trello.update_card = AsyncMock(side_effect=update_card)
    trello.move_card = AsyncMock(side_effect=move_card)
    trello.archive_card = AsyncMock(side_effect=archive_card)
    trello.add_comment = AsyncMock(side_effect=add_comment)
    trello.add_label_to_card = AsyncMock(side_effect=add_label_to_card)
    trello.add_checklist_item = AsyncMock(side_effect=add_checklist_item)
    trello.set_cover = AsyncMock(side_effect=set_cover)
    trello.get_labels = AsyncMock(side_effect=get_labels)

    def snapshot() -> dict[str, list[dict]]:
        out = {}
        for lid, cards in state.items():
            col_name = name_by_id[lid]
            out[col_name] = [
                {"id": c["id"], "name": c["name"], "desc": c.get("desc", "")[:100]}
                for c in cards
            ]
        return out

    return trello, {"snapshot": snapshot}


async def run_scenario(
    scenario: dict,
    *,
    vault_path: Path,
) -> Transcript:
    """Прогнать сценарий через реальный LLM + stateful mock Trello.

    scenario: {message, role, board, ...}
    """
    initial_board = scenario.get("board", {}) or {}
    role = scenario.get("role", "owner")
    message_text = scenario["message"]

    trello, helpers = _make_stateful_trello(initial_board)
    obsidian = ObsidianClient(vault_path=vault_path)
    executor = ToolExecutor(trello=trello, obsidian=obsidian, calendar=None)

    # Системный промпт — реальный, из файла.
    template = PROMPT_PATH.read_text(encoding="utf-8")
    from datetime import datetime
    now = datetime.now().strftime("%Y-%m-%d %H:%M (%A)")
    system = template.replace("{role}", role).replace("{now}", now)

    llm = LLMClient()

    messages = [{"role": "user", "content": message_text}]
    transcript = Transcript(final_text="")

    resp = await llm.chat_with_tools(messages=messages, tools=TOOLS, system=system)

    for round_idx in range(MAX_TOOL_ROUNDS):
        transcript.rounds = round_idx + 1
        text_parts = []
        tool_uses = []
        for block in resp.content:
            if block.type == "text":
                text_parts.append(block.text)
            elif block.type == "tool_use":
                tool_uses.append(block)

        if not tool_uses:
            transcript.final_text = "\n".join(text_parts)
            break

        # request_clarification — финальный «ответ» бота
        if len(tool_uses) == 1 and tool_uses[0].name == "request_clarification":
            transcript.final_text = tool_uses[0].input.get("question", "")
            transcript.tool_calls.append(
                ToolCall(name="request_clarification",
                         input=dict(tool_uses[0].input),
                         result=transcript.final_text)
            )
            break

        # Выполнить tool calls
        tool_results = []
        for tu in tool_uses:
            try:
                result = await executor.execute(tu.name, dict(tu.input), context={"telegram_id": 111})
            except Exception as e:
                result = f"Ошибка: {e}"
            transcript.tool_calls.append(
                ToolCall(name=tu.name, input=dict(tu.input), result=result)
            )
            tool_results.append({
                "type": "tool_result",
                "tool_use_id": tu.id,
                "content": result,
            })

        # Сериализовать content и продолжить диалог
        assistant_content = []
        for block in resp.content:
            if block.type == "text":
                assistant_content.append({"type": "text", "text": block.text})
            elif block.type == "tool_use":
                assistant_content.append({
                    "type": "tool_use",
                    "id": block.id,
                    "name": block.name,
                    "input": block.input,
                })
        messages.append({"role": "assistant", "content": assistant_content})
        messages.append({"role": "user", "content": tool_results})

        resp = await llm.chat_with_tools(messages=messages, tools=TOOLS, system=system)
    else:
        # Раунды израсходованы
        text_parts = []
        for block in resp.content:
            if block.type == "text":
                text_parts.append(block.text)
        transcript.final_text = "\n".join(text_parts)

    transcript.board_after = helpers["snapshot"]()
    return transcript
