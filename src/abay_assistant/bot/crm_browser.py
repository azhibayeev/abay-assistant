"""CRM-навигация через inline-кнопки в Telegram."""

import hashlib
import re
from datetime import datetime, timedelta

from aiogram import Bot, Router
from aiogram.enums import ParseMode
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    Message as TgMessage,
)
from loguru import logger

from abay_assistant.services.obsidian import ObsidianClient

crm_router = Router()

PENDING_NOTE_TTL = timedelta(minutes=30)

# Сервисы — инициализируются через setup_crm()
_bot: Bot | None = None
_obsidian: ObsidianClient | None = None

# Pending notes: telegram_id → (entity_type, entity_name, created_at)
_pending_notes: dict[int, tuple[str, str, datetime]] = {}

# Маппинг hash → name для callback data (предотвращение коллизий)
_cb_names: dict[str, str] = {}


def setup_crm(bot: Bot, obsidian: ObsidianClient) -> None:
    global _bot, _obsidian
    _bot = bot
    _obsidian = obsidian


def _name_to_cb(name: str) -> str:
    """Короткий хеш имени для callback data. Сохраняет маппинг."""
    # Если имя помещается — используем напрямую
    if len(name) <= 40:
        _cb_names[name] = name
        return name
    # Иначе используем хеш
    h = hashlib.md5(name.encode()).hexdigest()[:12]
    _cb_names[h] = name
    return h


def _cb_to_name(cb_key: str) -> str:
    """Восстановить имя из callback key."""
    return _cb_names.get(cb_key, cb_key)


def has_pending_note(telegram_id: int) -> bool:
    info = _pending_notes.get(telegram_id)
    if info is None:
        return False
    _, _, created_at = info
    if datetime.now() - created_at > PENDING_NOTE_TTL:
        del _pending_notes[telegram_id]
        return False
    return True


def cancel_pending_note(telegram_id: int) -> bool:
    if telegram_id in _pending_notes:
        del _pending_notes[telegram_id]
        return True
    return False


async def save_pending_note(telegram_id: int, text: str) -> str | None:
    """Сохранить pending note. Возвращает подтверждение или None."""
    info = _pending_notes.pop(telegram_id, None)
    if not info:
        return None

    entity_type, entity_name, _ = info
    path = await _obsidian.save_entity_note(
        entity_type=entity_type,
        entity_name=entity_name,
        content=text,
    )
    return f"Записано в {entity_name}: {path}"


# ─── Клавиатуры ───

def _cb(action: str, etype_code: str, name: str) -> str:
    """Сформировать callback_data с защитой от коллизий."""
    key = _name_to_cb(name)
    return f"crm_{action}_{etype_code}_{key}"


def _entity_list_kb(entities: list[dict], entity_type: str) -> InlineKeyboardMarkup:
    """Список сущностей как кнопки."""
    t = "p" if entity_type == "person" else "j"
    buttons = []
    for e in entities:
        name = e.get("name", "?")
        buttons.append([InlineKeyboardButton(text=name, callback_data=_cb("v", t, name))])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def _entity_card_kb(entity_type: str, entity_name: str) -> InlineKeyboardMarkup:
    """Кнопки карточки: Связи, Все записи, Добавить заметку."""
    t = "p" if entity_type == "person" else "j"
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="Связи", callback_data=_cb("l", t, entity_name)),
            InlineKeyboardButton(text="Все записи", callback_data=_cb("e", t, entity_name)),
        ],
        [
            InlineKeyboardButton(text="Добавить заметку", callback_data=_cb("a", t, entity_name)),
        ],
    ])


def _links_kb(links: list[str], link_type: str, back_type: str, back_name: str) -> InlineKeyboardMarkup:
    """Кнопки связей (проекты человека или люди проекта)."""
    t = "p" if link_type == "person" else "j"
    buttons = []
    for name in links:
        buttons.append([InlineKeyboardButton(text=name, callback_data=_cb("v", t, name))])
    # Кнопка «Назад»
    bt = "p" if back_type == "person" else "j"
    buttons.append([
        InlineKeyboardButton(text="← Назад", callback_data=_cb("v", bt, back_name))
    ])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


# ─── Публичные функции для handlers.py ───

async def show_entity_list(message: TgMessage, entity_type: str) -> None:
    """Показать список сущностей как inline-кнопки."""
    entities = await _obsidian.list_entities(entity_type)
    if not entities:
        label = "людей" if entity_type == "person" else "проектов"
        await message.answer(f"В базе пока нет {label}.")
        return

    label = "Люди в CRM" if entity_type == "person" else "Проекты в CRM"
    kb = _entity_list_kb(entities, entity_type)
    await message.answer(f"<b>{label}:</b>", parse_mode=ParseMode.HTML, reply_markup=kb)


async def show_entity_card(target: TgMessage | CallbackQuery, entity_type: str, entity_name: str) -> None:
    """Показать карточку сущности с кнопками."""
    summary = await _obsidian.get_entity_summary(entity_type, entity_name)
    kb = _entity_card_kb(entity_type, entity_name)

    if isinstance(target, CallbackQuery):
        await target.message.edit_text(summary, parse_mode=ParseMode.HTML, reply_markup=kb)
    else:
        await _send_html(target, summary, reply_markup=kb)


# ─── Callback handler ───

@crm_router.callback_query(lambda c: c.data and c.data.startswith("crm_"))
async def on_crm_callback(callback: CallbackQuery) -> None:
    """Обработка CRM-кнопок."""
    data = callback.data
    await callback.answer()

    # Парсим: crm_{action}_{type}_{cb_key}
    parts = data.split("_", 3)
    if len(parts) < 4:
        return

    _, action, etype_code, cb_key = parts
    name = _cb_to_name(cb_key)
    entity_type = "person" if etype_code == "p" else "project"

    if action == "v":
        # View — показать карточку
        await show_entity_card(callback, entity_type, name)

    elif action == "l":
        # Links — показать связи
        meta = await _obsidian.get_entity_meta(entity_type, name)
        if entity_type == "person":
            links = meta.get("projects", [])
            link_type = "project"
            label = "Проекты"
        else:
            links = meta.get("people", [])
            link_type = "person"
            label = "Люди"

        if not links:
            await callback.message.edit_text(
                f"У <b>{name}</b> нет связей.",
                parse_mode=ParseMode.HTML,
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                    InlineKeyboardButton(
                        text="← Назад",
                        callback_data=_cb("v", etype_code, name),
                    )
                ]]),
            )
            return

        kb = _links_kb(links, link_type, entity_type, name)
        await callback.message.edit_text(
            f"<b>{label} — {name}:</b>",
            parse_mode=ParseMode.HTML,
            reply_markup=kb,
        )

    elif action == "e":
        # Entries — показать все записи
        text = await _obsidian.get_entity(entity_type, name)
        if not text:
            display = f"Записей о <b>{name}</b> нет."
        else:
            # Убрать frontmatter для отображения
            _, body = ObsidianClient._parse_frontmatter(text)
            # Убрать wiki-ссылки
            display = re.sub(r"\[\[([^\]]+)\]\]", r"\1", body)
            if len(display) > 3500:
                display = display[:3500] + "\n\n…(обрезано)"

        back_kb = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(
                text="← Назад",
                callback_data=_cb("v", etype_code, name),
            )
        ]])

        try:
            await callback.message.edit_text(
                display, parse_mode=ParseMode.HTML, reply_markup=back_kb
            )
        except Exception:
            clean = re.sub(r"<[^>]+>", "", display)
            await callback.message.edit_text(clean, reply_markup=back_kb)

    elif action == "a":
        # Add note — ждём текст от пользователя
        tid = callback.from_user.id
        _pending_notes[tid] = (entity_type, name, datetime.now())

        back_kb = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(
                text="Отмена",
                callback_data=_cb("v", etype_code, name),
            )
        ]])

        await callback.message.edit_text(
            f"Напиши заметку для <b>{name}</b>.\n/cancel — отмена.",
            parse_mode=ParseMode.HTML,
            reply_markup=back_kb,
        )


async def _send_html(message: TgMessage, text: str, reply_markup=None) -> None:
    try:
        await message.answer(text, parse_mode=ParseMode.HTML, reply_markup=reply_markup)
    except Exception:
        clean = re.sub(r"<[^>]+>", "", text)
        await message.answer(clean, reply_markup=reply_markup)
