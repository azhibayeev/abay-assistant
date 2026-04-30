import asyncio
import html as html_lib
import re
import tempfile
import time
from datetime import datetime
from pathlib import Path

from aiogram import Router, F, Bot
from aiogram.enums import ParseMode
from aiogram.filters import CommandStart
from aiogram.types import Message as TgMessage
from loguru import logger

from abay_assistant.config import get_settings
from abay_assistant.db import save_message, get_recent_messages, get_active_reminders, delete_reminder
from abay_assistant.enums import UserRole, TrelloList
from abay_assistant.services.llm import LLMClient
from abay_assistant.services.trello import TrelloClient
from abay_assistant.services.obsidian import ObsidianClient
from abay_assistant.services.whisper import WhisperClient
from abay_assistant.services.calendar import CalendarClient
from abay_assistant.bot.evening import has_active_session, handle_response as evening_handle, start_session, cancel_session
from abay_assistant.bot.crm_browser import (
    has_pending_note,
    cancel_pending_note,
    save_pending_note,
    show_entity_list,
    show_entity_card,
)
from abay_assistant.tools.definitions import TOOLS
from abay_assistant.tools.executor import ToolExecutor

router = Router()

# Сервисы — инициализируются при старте через setup()
_llm: LLMClient | None = None
_executor: ToolExecutor | None = None
_whisper: WhisperClient | None = None
_trello: TrelloClient | None = None
_obsidian: ObsidianClient | None = None
_calendar: CalendarClient | None = None

PROMPT_PATH = Path(__file__).resolve().parent.parent / "prompts" / "inbox.md"

MAX_TOOL_ROUNDS = 3
MAX_VOICE_SIZE = 20 * 1024 * 1024  # 20 MB

# CRM-кеш имён для авто-линковки
_crm_names_cache: list[str] = []
_crm_names_cache_ts: float = 0.0
_CRM_CACHE_TTL = 300  # 5 минут


def setup(
    llm: LLMClient,
    trello: TrelloClient,
    obsidian: ObsidianClient,
    whisper: WhisperClient,
    calendar: CalendarClient | None = None,
) -> None:
    """Инициализировать сервисы для хендлеров."""
    global _llm, _executor, _whisper, _trello, _obsidian, _calendar
    _llm = llm
    _trello = trello
    _obsidian = obsidian
    _executor = ToolExecutor(trello=trello, obsidian=obsidian)
    _whisper = whisper
    _calendar = calendar


def _user_role(telegram_id: int) -> str:
    s = get_settings()
    if telegram_id == s.telegram_owner_id:
        return UserRole.OWNER
    if telegram_id == s.telegram_assistant_id:
        return UserRole.ASSISTANT
    return UserRole.UNKNOWN


def _build_system_prompt(role: str) -> str:
    template = PROMPT_PATH.read_text(encoding="utf-8")
    now = datetime.now().strftime("%Y-%m-%d %H:%M (%A)")
    return template.replace("{role}", role).replace("{now}", now)


def _db_messages_to_llm(db_msgs: list) -> list[dict]:
    """Конвертировать сообщения из БД в формат Anthropic API."""
    return [{"role": m.role, "content": m.text} for m in db_msgs]


async def _get_crm_names() -> list[str]:
    """Получить имена из CRM с кешированием (5 мин TTL)."""
    global _crm_names_cache, _crm_names_cache_ts
    now = time.monotonic()
    if _crm_names_cache and (now - _crm_names_cache_ts) < _CRM_CACHE_TTL:
        return _crm_names_cache

    names = []
    try:
        for entity_type in ("person", "project"):
            entities = await _obsidian.list_entities(entity_type)
            for e in entities:
                name = e.get("name", "")
                if name:
                    names.append(name)
        _crm_names_cache = names
        _crm_names_cache_ts = now
    except Exception as e:
        logger.debug("CRM names cache update failed: {}", e)
    return _crm_names_cache


def _detect_unlinked_crm_mentions(
    user_text: str, crm_names: list[str], llm_response: str,
) -> list[str]:
    """Найти имена CRM, упомянутые пользователем, но не сохранённые LLM."""
    if not crm_names:
        return []
    user_lower = user_text.lower()
    resp_lower = llm_response.lower()
    save_markers = ("записано", "сохранено", "save_entity", "добавлено в crm")
    mentioned = []
    for name in crm_names:
        if len(name) < 4:
            continue
        if name.lower() in user_lower:
            # Проверить, что LLM уже не сохранил
            if not any(marker in resp_lower for marker in save_markers):
                mentioned.append(name)
    return mentioned


def _md_to_html(text: str) -> str:
    """Конвертировать базовый markdown в Telegram HTML.

    Сначала экранирует HTML-сущности, потом применяет markdown-конвертацию.
    """
    # Экранировать HTML entities для предотвращения injection
    text = html_lib.escape(text)
    # **bold** → <b>bold</b>
    text = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", text)
    # *italic* → <i>italic</i>
    text = re.sub(r"(?<!\*)\*(?!\*)(.+?)(?<!\*)\*(?!\*)", r"<i>\1</i>", text)
    # `code` → <code>code</code>
    text = re.sub(r"`(.+?)`", r"<code>\1</code>", text)
    return text


async def _send_with_retry(coro_factory, max_retries: int = 3) -> None:
    """Выполнить отправку с retry при Telegram 429 (Too Many Requests)."""
    for attempt in range(max_retries):
        try:
            await coro_factory()
            return
        except Exception as e:
            error_text = str(e).lower()
            if "retry after" in error_text or "too many requests" in error_text:
                # Экспоненциальный backoff: 1s, 2s, 4s
                delay = 2 ** attempt
                logger.warning("Telegram 429, retry через {}с", delay)
                await asyncio.sleep(delay)
            else:
                raise
    # Последняя попытка — без обработки ошибки
    await coro_factory()


async def _send_reply(message: TgMessage, text: str) -> None:
    """Отправить ответ с HTML-форматированием, fallback на plain text."""
    html = _md_to_html(text)
    try:
        await _send_with_retry(lambda: message.answer(html, parse_mode=ParseMode.HTML))
    except Exception:
        try:
            await message.answer(text)
        except Exception as e:
            logger.error("Не удалось отправить сообщение: {}", e)


@router.message(CommandStart())
async def cmd_start(message: TgMessage) -> None:
    role = _user_role(message.from_user.id)
    if role == UserRole.UNKNOWN:
        await message.answer("Доступ ограничен.")
        return

    name = message.from_user.first_name or "друг"
    await message.answer(f"Привет, {name}! Я ассистент Абая. Пиши — помогу разобраться.")
    logger.info("/start от {} (role={})", message.from_user.id, role)


@router.message(F.text == "/help")
async def cmd_help(message: TgMessage) -> None:
    """Список доступных команд."""
    role = _user_role(message.from_user.id)
    if role == UserRole.UNKNOWN:
        await message.answer("Доступ ограничен.")
        return

    text = (
        "<b>Команды:</b>\n\n"
        "/start — начать работу с ботом\n"
        "/evening — запустить вечерний свод\n"
        "/cancel — отменить вечерний свод / ввод заметки\n"
        "/reminders — список активных напоминаний\n"
        "/who — список людей в CRM (inline-кнопки)\n"
        "/who Расул — карточка человека\n"
        "/project — список проектов (inline-кнопки)\n"
        "/project DMS — карточка проекта\n"
        "/health — проверка состояния бота\n"
        "/help — эта справка\n\n"
        "<b>Что умею:</b>\n"
        "— Создавать/перемещать/архивировать задачи в Trello\n"
        "— Запоминать людей, проекты и связи (CRM)\n"
        "— Распознавать голосовые сообщения\n"
        "— Напоминания (\"напомни через 2 часа...\")\n"
        "— Поиск в интернете (\"найди контакты компании...\")\n"
        "— Утренняя сводка (9:00), чек-ин (14:00), вечерний свод (22:30)\n"
        "— Weekly-отчёт (воскресенье 21:00)\n\n"
        "Просто пиши или отправляй голосовое — разберусь."
    )
    await message.answer(text, parse_mode=ParseMode.HTML)


@router.message(F.text == "/cancel")
async def cmd_cancel(message: TgMessage) -> None:
    """Отменить вечерний свод или ввод заметки."""
    role = _user_role(message.from_user.id)
    if role == UserRole.UNKNOWN:
        await message.answer("Доступ ограничен.")
        return

    tid = message.from_user.id
    if cancel_pending_note(tid):
        await message.answer("Ввод заметки отменён.")
    elif cancel_session(tid):
        await message.answer("Вечерний свод отменён.")
    else:
        await message.answer("Нет активной сессии.")


@router.message(F.text == "/health")
async def cmd_health(message: TgMessage) -> None:
    """Проверка здоровья бота."""
    role = _user_role(message.from_user.id)
    if role == UserRole.UNKNOWN:
        await message.answer("Доступ ограничен.")
        return

    checks = []

    # DB
    try:
        from abay_assistant.db import get_session
        from sqlalchemy import text
        with get_session() as session:
            session.execute(text("SELECT 1"))
        checks.append("✅ БД: OK")
    except Exception as e:
        checks.append(f"❌ БД: {e}")

    # Trello
    try:
        await _trello.get_cards(TrelloList.TODAY)
        checks.append("✅ Trello: OK")
    except Exception as e:
        checks.append(f"❌ Trello: {e}")

    # LLM
    checks.append(f"✅ LLM: подключен" if _llm else "❌ LLM: не инициализирован")

    # Obsidian vault
    vault_exists = _obsidian and _obsidian.vault.exists()
    checks.append(f"✅ Vault: {_obsidian.vault}" if vault_exists else "❌ Vault: не найден")

    # Whisper
    whisper_ok = _whisper and _whisper.enabled
    checks.append("✅ Whisper: OK" if whisper_ok else "⚠️ Whisper: не настроен")

    # Calendar
    calendar_ok = _calendar and _calendar.enabled
    checks.append("✅ Calendar: OK" if calendar_ok else "⚠️ Calendar: не настроен")

    await message.answer("<b>Health check:</b>\n" + "\n".join(checks), parse_mode=ParseMode.HTML)


@router.message(F.text.startswith("/reminders"))
async def cmd_reminders(message: TgMessage) -> None:
    """Показать активные напоминания и удалить по номеру."""
    role = _user_role(message.from_user.id)
    if role == UserRole.UNKNOWN:
        await message.answer("Доступ ограничен.")
        return

    tid = message.from_user.id
    parts = message.text.split(maxsplit=1)

    # /reminders del 5 — удалить напоминание #5
    if len(parts) > 1 and parts[1].startswith("del"):
        try:
            reminder_id = int(parts[1].split()[-1])
            if delete_reminder(reminder_id, tid):
                await message.answer(f"Напоминание #{reminder_id} удалено.")
            else:
                await message.answer(f"Напоминание #{reminder_id} не найдено.")
        except (ValueError, IndexError):
            await message.answer("Использование: /reminders del <номер>")
        return

    reminders = get_active_reminders(tid)
    if not reminders:
        await message.answer("Активных напоминаний нет.")
        return

    lines = ["<b>Активные напоминания:</b>\n"]
    for r in reminders:
        time_str = r.remind_at.strftime("%d.%m %H:%M")
        lines.append(f"  #{r.id} — {time_str}: {r.text}")
    lines.append("\nУдалить: /reminders del <номер>")
    await _send_html(message, "\n".join(lines))


@router.message(F.text.startswith("/who"))
async def cmd_who(message: TgMessage) -> None:
    """Показать информацию о человеке из CRM."""
    role = _user_role(message.from_user.id)
    if role == UserRole.UNKNOWN:
        await message.answer("Доступ ограничен.")
        return

    parts = message.text.split(maxsplit=1)
    if len(parts) < 2:
        await show_entity_list(message, "person")
        return

    name = parts[1].strip()
    await show_entity_card(message, "person", name)


@router.message(F.text.startswith("/project"))
async def cmd_project(message: TgMessage) -> None:
    """Показать информацию о проекте из CRM."""
    role = _user_role(message.from_user.id)
    if role == UserRole.UNKNOWN:
        await message.answer("Доступ ограничен.")
        return

    parts = message.text.split(maxsplit=1)
    if len(parts) < 2:
        await show_entity_list(message, "project")
        return

    name = parts[1].strip()
    await show_entity_card(message, "project", name)


async def _send_html(message: TgMessage, text: str) -> None:
    """Отправить HTML с fallback."""
    try:
        await message.answer(text, parse_mode=ParseMode.HTML)
    except Exception:
        # Убрать HTML теги
        clean = re.sub(r"<[^>]+>", "", text)
        await message.answer(clean)


@router.message(F.text == "/evening")
async def cmd_evening(message: TgMessage) -> None:
    """Запустить вечерний свод вручную."""
    role = _user_role(message.from_user.id)
    if role == UserRole.UNKNOWN:
        await message.answer("Доступ ограничен.")
        return

    raw_cards = await _trello.get_cards(TrelloList.TODAY)
    today_cards = [{"id": c["id"], "name": c["name"]} for c in raw_cards]
    await start_session(message.from_user.id, today_cards, [])


@router.message(F.voice)
async def handle_voice(message: TgMessage, bot: Bot) -> None:
    """Голосовое сообщение: скачать → Whisper → обработать как текст."""
    role = _user_role(message.from_user.id)
    if role == UserRole.UNKNOWN:
        await message.answer("Доступ ограничен.")
        return

    if not _whisper or not _whisper.enabled:
        await message.answer("Голосовые сообщения пока не поддерживаются (не настроен OpenAI ключ).")
        return

    # Проверить размер файла
    if message.voice.file_size and message.voice.file_size > MAX_VOICE_SIZE:
        await message.answer("Голосовое слишком большое (макс. 20 МБ). Попробуй короче.")
        return

    # Скачать файл
    file = await bot.get_file(message.voice.file_id)
    with tempfile.NamedTemporaryFile(suffix=".ogg", delete=False) as tmp:
        tmp_path = Path(tmp.name)
    await bot.download_file(file.file_path, tmp_path)

    try:
        text = await _whisper.transcribe(tmp_path)
    except Exception as e:
        logger.error("Whisper ошибка: {}", e)
        await message.answer("Не удалось распознать голосовое. Попробуй текстом.")
        return
    finally:
        tmp_path.unlink(missing_ok=True)

    if not text:
        await message.answer("Не удалось распознать речь.")
        return

    # Показать распознанный текст и обработать через inbox
    await message.answer(f"Распознано: {text}")
    await _process_inbox(message, role, text)


@router.message(F.text)
async def handle_text(message: TgMessage) -> None:
    role = _user_role(message.from_user.id)
    if role == UserRole.UNKNOWN:
        await message.answer("Доступ ограничен.")
        logger.warning("Попытка доступа от {}", message.from_user.id)
        return

    tid = message.from_user.id

    # Если ожидается заметка CRM — сохранить
    if has_pending_note(tid):
        result = await save_pending_note(tid, message.text)
        if result:
            await message.answer(result)
        return

    # Если идёт вечерний свод — перехватываем
    if has_active_session(tid):
        await evening_handle(tid, message.text)
        return

    await _process_inbox(message, role, message.text)


async def _process_inbox(message: TgMessage, role: str, text: str) -> None:
    """Основной inbox-цикл: сообщение → LLM → tools → ответ."""
    tid = message.from_user.id

    # 1. Сохранить входящее сообщение
    save_message(tid, "user", text)

    # 2. Загрузить контекст
    recent = get_recent_messages(tid, limit=15)
    llm_messages = _db_messages_to_llm(recent)

    # 3. Системный промпт
    system = _build_system_prompt(role)

    # 4. Вызов LLM с tools
    try:
        resp = await _llm.chat_with_tools(
            messages=llm_messages,
            tools=TOOLS,
            system=system,
        )
    except Exception as e:
        error_type = type(e).__name__
        logger.error("LLM ошибка ({}): {}", error_type, e)
        if "timeout" in str(e).lower() or "Timeout" in error_type:
            await message.answer("Claude не отвечает (timeout). Попробуй через минуту.")
        elif "rate" in str(e).lower():
            await message.answer("Слишком много запросов. Подожди немного.")
        else:
            await message.answer("Произошла ошибка при обработке. Попробуй ещё раз.")
        return

    # 5. Обработка tool_use в цикле (до MAX_TOOL_ROUNDS раундов)
    context = {"telegram_id": tid}
    reply_text = await _process_response(resp, llm_messages, system, context)

    # 6. Отправить ответ
    if reply_text:
        await _send_reply(message, reply_text)

    # 7. Сохранить ответ бота
    if reply_text:
        save_message(tid, "assistant", reply_text)

    # 8. CRM авто-линковка: если упомянуты имена из CRM, а LLM не сохранил
    if reply_text and role == UserRole.OWNER:
        try:
            crm_names = await _get_crm_names()
            unlinked = _detect_unlinked_crm_mentions(text, crm_names, reply_text)
            if unlinked:
                hint = "Упомянуты: " + ", ".join(unlinked) + ". Записать в CRM?"
                await message.answer(hint)
        except Exception as e:
            logger.debug("CRM auto-link hint failed: {}", e)

    logger.info("Inbox: {} (role={}) → {}", tid, role, reply_text[:80] if reply_text else "(пусто)")


async def _process_response(
    resp, messages: list[dict], system: str, context: dict | None = None
) -> str:
    """Обработать ответ LLM: выполнить tool calls если есть, вернуть финальный текст.

    Поддерживает до MAX_TOOL_ROUNDS раундов tool calls (для web_search → web_fetch цепочек).
    """
    current_messages = list(messages)
    current_resp = resp

    for _round in range(MAX_TOOL_ROUNDS):
        text_parts = []
        tool_uses = []

        for block in current_resp.content:
            if block.type == "text":
                text_parts.append(block.text)
            elif block.type == "tool_use":
                tool_uses.append(block)

        if not tool_uses:
            return "\n".join(text_parts)

        # request_clarification — вернуть вопрос напрямую
        if len(tool_uses) == 1 and tool_uses[0].name == "request_clarification":
            return tool_uses[0].input.get("question", "Уточни, пожалуйста.")

        # Выполнить tool calls
        tool_results = []
        for tu in tool_uses:
            result = await _executor.execute(tu.name, tu.input, context=context)
            tool_results.append({
                "type": "tool_result",
                "tool_use_id": tu.id,
                "content": result,
            })

        # Добавить assistant response + tool results
        current_messages.append(
            {"role": "assistant", "content": _serialize_content(current_resp.content)}
        )
        current_messages.append({"role": "user", "content": tool_results})

        # Вызвать LLM снова
        try:
            current_resp = await _llm.chat_with_tools(
                messages=current_messages,
                tools=TOOLS,
                system=system,
            )
        except Exception as e:
            logger.error("LLM tool round {} ошибка: {}", _round + 1, e)
            return "Выполнено."

    # Если все раунды израсходованы — собрать текст из последнего ответа
    final_texts = []
    for block in current_resp.content:
        if block.type == "text":
            final_texts.append(block.text)
    return "\n".join(final_texts) if final_texts else "Выполнено."


def _serialize_content(content_blocks) -> list[dict]:
    """Сериализовать content blocks в формат для API."""
    result = []
    for block in content_blocks:
        if block.type == "text":
            result.append({"type": "text", "text": block.text})
        elif block.type == "tool_use":
            result.append({
                "type": "tool_use",
                "id": block.id,
                "name": block.name,
                "input": block.input,
            })
    return result
