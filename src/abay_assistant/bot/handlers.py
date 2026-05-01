import asyncio
import html as html_lib
import re
import tempfile
import time
from datetime import datetime
from pathlib import Path

from aiogram import Router, F, Bot
from aiogram.enums import ChatAction, ParseMode
from aiogram.filters import CommandStart
from aiogram.types import Message as TgMessage
from loguru import logger

from abay_assistant.config import get_settings
from abay_assistant.db import save_message, get_recent_messages, get_active_reminders, delete_reminder, get_tool_stats, get_message_stats, save_idea, get_ideas, update_idea_status
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

MAX_TOOL_ROUNDS = 8
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
    _executor = ToolExecutor(trello=trello, obsidian=obsidian, calendar=calendar)
    _whisper = whisper
    _calendar = calendar


def _user_role(telegram_id: int) -> str:
    s = get_settings()
    if telegram_id == s.telegram_owner_id:
        return UserRole.OWNER
    if telegram_id == s.telegram_assistant_id:
        return UserRole.ASSISTANT
    return UserRole.UNKNOWN


async def _check_access(message: TgMessage) -> str | None:
    """Проверить доступ пользователя. Возвращает роль или None (остановить обработку).

    В группах: неизвестных пользователей игнорирует молча.
    Если group_id не настроен — показывает ID чата для конфигурации.
    """
    role = _user_role(message.from_user.id)
    is_group = message.chat.type in ("group", "supergroup")

    if role == UserRole.UNKNOWN:
        if not is_group:
            await message.answer("Доступ ограничен.")
            logger.warning("Попытка доступа от {}", message.from_user.id)
        return None

    if is_group:
        s = get_settings()
        if not s.telegram_group_id:
            await message.answer(
                f"Chat ID этой группы: <code>{message.chat.id}</code>\n"
                f"Добавь <code>TELEGRAM_GROUP_ID={message.chat.id}</code> в .env и перезапусти бота.",
                parse_mode=ParseMode.HTML,
            )
            return None
        if message.chat.id != s.telegram_group_id:
            return None

    return role


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


TG_MSG_LIMIT = 4000  # Telegram limit 4096, с запасом на HTML-теги


def _split_message(text: str, limit: int = TG_MSG_LIMIT) -> list[str]:
    """Разбить длинное сообщение на части по абзацам."""
    if len(text) <= limit:
        return [text]

    parts = []
    current = ""
    for line in text.split("\n"):
        if len(current) + len(line) + 1 > limit:
            if current:
                parts.append(current)
            current = line[:limit]  # на случай одной строки > limit
        else:
            current = current + "\n" + line if current else line
    if current:
        parts.append(current)
    return parts or [text[:limit]]


async def _send_reply(message: TgMessage, text: str) -> None:
    """Отправить ответ с HTML-форматированием, разбивая длинные сообщения."""
    for chunk in _split_message(text):
        html = _md_to_html(chunk)
        try:
            await _send_with_retry(lambda h=html: message.answer(h, parse_mode=ParseMode.HTML))
        except Exception:
            try:
                await message.answer(chunk)
            except Exception as e:
                logger.error("Не удалось отправить сообщение: {}", e)


@router.message(CommandStart())
async def cmd_start(message: TgMessage) -> None:
    role = await _check_access(message)
    if not role:
        return

    name = message.from_user.first_name or "друг"
    await message.answer(f"Привет, {name}! Я ассистент Абая. Пиши — помогу разобраться.")
    logger.info("/start от {} (role={})", message.from_user.id, role)


@router.message(F.text == "/help")
async def cmd_help(message: TgMessage) -> None:
    """Список доступных команд."""
    role = await _check_access(message)
    if not role:
        return

    text = (
        "<b>Команды:</b>\n\n"
        "/start — начать работу с ботом\n"
        "/board — обзор доски (все колонки)\n"
        "/evening — запустить вечерний свод\n"
        "/cancel — отменить вечерний свод / ввод заметки\n"
        "/reminders — список активных напоминаний\n"
        "/who — список людей в CRM (inline-кнопки)\n"
        "/who Расул — карточка человека\n"
        "/project — список проектов (inline-кнопки)\n"
        "/project DMS — карточка проекта\n"
        "/health — проверка состояния бота\n"
        "/idea <текст> — записать идею по улучшению бота\n"
        "/ideas — список открытых идей\n"
        "/stats — статистика за 7 дней\n"
        "/help — эта справка\n\n"
        "<b>Что умею:</b>\n"
        "— Создавать/перемещать/архивировать задачи в Trello\n"
        "— Запоминать людей, проекты и связи (CRM)\n"
        "— Распознавать голосовые сообщения\n"
        "— Напоминания (\"напомни через 2 часа...\")\n"
        "— Поиск в интернете (\"найди контакты компании...\")\n"
        "— Утренняя сводка (9:00), nudge (12/16), вечерний свод (22:30)\n"
        "— Weekly-отчёт (воскресенье 21:00)\n"
        "— Реплай на nudge = комментарий к карточке\n\n"
        "Просто пиши или отправляй голосовое — разберусь."
    )
    await message.answer(text, parse_mode=ParseMode.HTML)


@router.message(F.text == "/cancel")
async def cmd_cancel(message: TgMessage) -> None:
    """Отменить вечерний свод или ввод заметки."""
    role = await _check_access(message)
    if not role:
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
    role = await _check_access(message)
    if not role:
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


@router.message(F.text == "/stats")
async def cmd_stats(message: TgMessage) -> None:
    """Статистика использования бота за 7 дней."""
    role = await _check_access(message)
    if not role or role != UserRole.OWNER:
        return

    msg_stats = get_message_stats(days=7)
    tool_stats = get_tool_stats(days=7)

    lines = ["<b>Статистика за 7 дней:</b>\n"]
    lines.append(f"Сообщений: {msg_stats['user_messages']} от тебя, {msg_stats['bot_messages']} от бота\n")

    if tool_stats:
        lines.append("<b>Tool calls:</b>")
        for ts in tool_stats[:10]:
            lines.append(f"  {ts['tool_name']}: {ts['count']}")
    else:
        lines.append("Tool calls: нет данных")

    await _send_html(message, "\n".join(lines))


@router.message(F.text.startswith("/idea"))
async def cmd_idea(message: TgMessage) -> None:
    """Записать идею по улучшению бота или посмотреть список."""
    role = await _check_access(message)
    if not role:
        return

    parts = message.text.split(maxsplit=1)
    command = parts[0].lower()

    # /ideas — список открытых идей
    if command == "/ideas":
        ideas = get_ideas(status="open")
        if not ideas:
            await message.answer("Открытых идей нет.")
            return
        lines = ["<b>Идеи по улучшению бота:</b>\n"]
        for idea in ideas:
            date_str = idea.created_at.strftime("%d.%m")
            lines.append(f"  #{idea.id} ({date_str}) — {idea.text}")
        lines.append("\nГотово: /idea done <номер>")
        await _send_html(message, "\n".join(lines))
        return

    # /idea done 5 — пометить как сделанную
    if len(parts) > 1 and parts[1].startswith("done"):
        try:
            idea_id = int(parts[1].split()[-1])
            if update_idea_status(idea_id, "done"):
                await message.answer(f"Идея #{idea_id} помечена как сделанная.")
            else:
                await message.answer(f"Идея #{idea_id} не найдена.")
        except (ValueError, IndexError):
            await message.answer("Использование: /idea done <номер>")
        return

    # /idea <текст> — записать новую идею
    if len(parts) < 2:
        await message.answer(
            "Использование:\n"
            "/idea <текст> — записать идею\n"
            "/ideas — список идей\n"
            "/idea done <номер> — пометить как сделанную"
        )
        return

    idea = save_idea(message.from_user.id, parts[1])
    await message.answer(f"Идея #{idea.id} записана: {idea.text}")


@router.message(F.text.startswith("/reminders"))
async def cmd_reminders(message: TgMessage) -> None:
    """Показать активные напоминания и удалить по номеру."""
    role = await _check_access(message)
    if not role:
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
    role = await _check_access(message)
    if not role:
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
    role = await _check_access(message)
    if not role:
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


_BOARD_COVER_EMOJI = {"red": "🔴", "orange": "🟠", "green": "🟢", "blue": "🔵"}


@router.message(F.text == "/board")
async def cmd_board(message: TgMessage) -> None:
    """Мгновенный обзор доски без LLM."""
    role = await _check_access(message)
    if not role:
        return

    try:
        lists = await _trello.get_lists()
        all_cards = await _trello.get_all_cards()

        # Группировать карточки по list_id
        id_to_name = {v: k for k, v in lists.items()}
        cards_by_list: dict[str, list[dict]] = {}
        for c in all_cards:
            list_name = id_to_name.get(c.get("idList"), "")
            if list_name:
                cards_by_list.setdefault(list_name, []).append(c)

        # Порядок колонок
        priority_order = ["сегодня", "неделя", "мяч на стороне"]
        skip_lists = ["готово"]

        def sort_key(name: str) -> int:
            lower = name.lower()
            for i, prefix in enumerate(priority_order):
                if prefix in lower:
                    return i
            if lower in skip_lists:
                return 100
            return 10

        sorted_lists = sorted(lists.keys(), key=sort_key)

        lines = ["<b>📋 Доска</b>\n"]
        for list_name in sorted_lists:
            if any(s in list_name.lower() for s in skip_lists):
                continue
            cards = cards_by_list.get(list_name, [])
            if not cards:
                lines.append(f"<b>{list_name}</b> (0)")
                continue

            lines.append(f"\n<b>{list_name}</b> ({len(cards)})")
            for c in cards[:5]:
                cover_color = (c.get("cover") or {}).get("color")
                emoji = _BOARD_COVER_EMOJI.get(cover_color, "⬜")
                due = c.get("due", "")
                due_str = ""
                if due:
                    due_date = due[:10]
                    today_str = datetime.now().strftime("%Y-%m-%d")
                    if due_date < today_str:
                        due_str = " ⚠️"
                    elif due_date == today_str:
                        due_str = " 🔥"
                lines.append(f"  {emoji} {c['name']}{due_str}")
            if len(cards) > 5:
                lines.append(f"  <i>... +{len(cards) - 5}</i>")

        await _send_html(message, "\n".join(lines))
    except Exception as e:
        logger.error("Ошибка /board: {}", e)
        await message.answer("Ошибка загрузки доски.")


@router.message(F.text == "/evening")
async def cmd_evening(message: TgMessage) -> None:
    """Запустить вечерний свод вручную."""
    role = await _check_access(message)
    if not role:
        return

    raw_cards = await _trello.get_cards(TrelloList.TODAY)
    today_cards = [{"id": c["id"], "name": c["name"]} for c in raw_cards]
    await start_session(message.from_user.id, today_cards, [])


@router.message(F.voice)
async def handle_voice(message: TgMessage, bot: Bot) -> None:
    """Голосовое сообщение: скачать → Whisper → обработать как текст."""
    role = await _check_access(message)
    if not role:
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


def _extract_card_id_from_reply(message: TgMessage) -> str | None:
    """Извлечь card_id из inline-кнопок сообщения, на которое ответили."""
    reply = message.reply_to_message
    if not reply or not reply.reply_markup:
        return None
    for row in reply.reply_markup.inline_keyboard:
        for btn in row:
            data = btn.callback_data or ""
            if data.startswith(("nudge_", "stuck_", "wait_")):
                # callback_data: {prefix}_{action}_{card_id}
                parts = data.split("_", 2)
                if len(parts) >= 3:
                    return parts[2]
    return None


@router.message(F.text)
async def handle_text(message: TgMessage) -> None:
    role = await _check_access(message)
    if not role:
        return

    tid = message.from_user.id

    # Реплай на nudge/stuck/waiting → комментарий к карточке
    card_id = _extract_card_id_from_reply(message)
    if card_id:
        try:
            now = datetime.now().strftime("%d.%m.%Y %H:%M")
            await _trello.add_comment(card_id, f"💬 {now}\n{message.text}")
            card = await _trello.get_card(card_id)
            card_name = card.get("name", "?")
            await message.answer(f"Записал комментарий к «{card_name}».")
        except Exception as e:
            logger.error("Ошибка комментария к {}: {}", card_id, e)
            await message.answer("Не удалось записать комментарий.")
        return

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

    # 4. Показать индикатор «печатает...»
    await message.bot.send_chat_action(message.chat.id, ChatAction.TYPING)

    # 5. Вызов LLM с tools (с доп. retry при rate limit)
    resp = None
    for llm_attempt in range(2):
        try:
            resp = await _llm.chat_with_tools(
                messages=llm_messages,
                tools=TOOLS,
                system=system,
            )
            break
        except Exception as e:
            error_type = type(e).__name__
            logger.error("LLM ошибка попытка {} ({}): {}", llm_attempt + 1, error_type, e)
            if llm_attempt == 0 and "rate" in str(e).lower():
                await message.answer("Обрабатываю... подожди немного.")
                await asyncio.sleep(30)
                await message.bot.send_chat_action(message.chat.id, ChatAction.TYPING)
                continue
            if "timeout" in str(e).lower() or "Timeout" in error_type:
                await message.answer("Claude не отвечает (timeout). Попробуй через минуту.")
            elif "rate" in str(e).lower():
                await message.answer("Слишком много запросов. Подожди минуту и попробуй снова.")
            else:
                await message.answer("Произошла ошибка при обработке. Попробуй ещё раз.")
            return
    if resp is None:
        await message.answer("Не удалось получить ответ. Попробуй позже.")
        return

    # 6. Обработка tool_use в цикле (до MAX_TOOL_ROUNDS раундов)
    context = {"telegram_id": tid}
    reply_text = await _process_response(resp, llm_messages, system, context, message=message)

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


def _truncate_tool_results(messages: list[dict], keep_recent: int = 2) -> list[dict]:
    """Обрезать старые tool_result сообщения, оставив только последние N раундов полными.

    Для ранних раундов заменяет content на краткие сводки, чтобы контекст не разрастался.
    """
    # Найти все пары assistant(tool_use) + user(tool_result)
    tool_round_indices = []
    for i, msg in enumerate(messages):
        if msg["role"] == "user" and isinstance(msg.get("content"), list):
            if any(isinstance(b, dict) and b.get("type") == "tool_result" for b in msg["content"]):
                tool_round_indices.append(i)

    if len(tool_round_indices) <= keep_recent:
        return messages

    # Обрезать старые раунды
    truncated = list(messages)
    old_indices = tool_round_indices[:-keep_recent]
    for idx in old_indices:
        old_content = truncated[idx]["content"]
        if not isinstance(old_content, list):
            continue
        short_results = []
        for block in old_content:
            if isinstance(block, dict) and block.get("type") == "tool_result":
                original = block.get("content", "")
                # Оставить только первые 100 символов результата
                short = original[:100] + "..." if len(original) > 100 else original
                short_results.append({
                    "type": "tool_result",
                    "tool_use_id": block["tool_use_id"],
                    "content": short,
                })
            else:
                short_results.append(block)
        truncated[idx] = {"role": "user", "content": short_results}
    return truncated


def _build_actions_summary(actions: list[str]) -> str:
    """Построить сводку выполненных действий для fallback-ответа."""
    if not actions:
        return "Выполнено."

    lines = ["Выполнено:"]
    for action in actions:
        lines.append(f"• {action}")
    return "\n".join(lines)


# Названия инструментов → человекочитаемые описания
_TOOL_DESCRIPTIONS = {
    "create_trello_card": "Создана карточка: {name}",
    "update_trello_card": "Обновлена карточка {card_id}",
    "move_trello_card": "Перемещена карточка → {target_list}",
    "add_trello_comment": "Комментарий к карточке {card_id}",
    "add_checklist_item": "Пункт в чек-лист: {text}",
    "get_trello_cards": "Загружены карточки: {list_name}",
    "save_entity_note": "CRM: {entity_name}",
    "update_entity_meta": "CRM мета: {entity_name}",
    "set_reminder": "Напоминание: {text}",
    "append_obsidian_daily": "Заметка в дневник",
    "web_search": "Поиск: {query}",
    "web_fetch": "Загрузка страницы",
}


def _describe_tool_action(tool_name: str, tool_input: dict) -> str:
    """Человекочитаемое описание выполненного tool call."""
    template = _TOOL_DESCRIPTIONS.get(tool_name, tool_name)
    try:
        return template.format(**tool_input)
    except (KeyError, IndexError):
        return tool_name


async def _process_response(
    resp, messages: list[dict], system: str, context: dict | None = None,
    *, message: TgMessage | None = None,
) -> str:
    """Обработать ответ LLM: выполнить tool calls если есть, вернуть финальный текст.

    Поддерживает до MAX_TOOL_ROUNDS раундов tool calls (для web_search → web_fetch цепочек).
    При сбое финального LLM-вызова — возвращает сводку выполненных действий.
    """
    current_messages = list(messages)
    current_resp = resp
    actions_done: list[str] = []  # Трекинг выполненных действий

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
            # Трекать действие (кроме get_trello_cards — это чтение)
            if tu.name != "get_trello_cards":
                actions_done.append(_describe_tool_action(tu.name, tu.input))

        # Добавить assistant response + tool results
        current_messages.append(
            {"role": "assistant", "content": _serialize_content(current_resp.content)}
        )
        current_messages.append({"role": "user", "content": tool_results})

        # Обрезать старые tool results чтобы контекст не разрастался
        current_messages = _truncate_tool_results(current_messages, keep_recent=2)

        # Обновить typing-индикатор перед следующим раундом
        if message:
            try:
                await message.bot.send_chat_action(message.chat.id, ChatAction.TYPING)
            except Exception:
                pass

        # Вызвать LLM снова
        try:
            current_resp = await _llm.chat_with_tools(
                messages=current_messages,
                tools=TOOLS,
                system=system,
            )
        except Exception as e:
            logger.error("LLM tool round {} ошибка: {}", _round + 1, e)
            return _build_actions_summary(actions_done)

    # Если все раунды израсходованы — собрать текст из последнего ответа
    final_texts = []
    for block in current_resp.content:
        if block.type == "text":
            final_texts.append(block.text)
    return "\n".join(final_texts) if final_texts else _build_actions_summary(actions_done)


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
