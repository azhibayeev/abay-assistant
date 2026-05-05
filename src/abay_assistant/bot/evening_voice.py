"""Вечерний свод в группе: буфер голосовых от Абая, эхо-подтверждение через 5 мин, выполнение действий через LLM+Trello."""

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import StrEnum

from aiogram import Bot, Router
from aiogram.enums import ParseMode
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery
from loguru import logger

from abay_assistant.config import get_settings
from abay_assistant.services.llm import LLMClient
from abay_assistant.services.trello import TrelloClient
from abay_assistant.tools.definitions import TOOLS
from abay_assistant.tools.executor import ToolExecutor


VOICE_TIMEOUT_SECONDS = 5 * 60
SESSION_TTL = timedelta(hours=3)
MAX_TOOL_ROUNDS = 8


class VoicePhase(StrEnum):
    COLLECTING = "collecting"
    AWAITING_CONFIRMATION = "awaiting_confirmation"
    PROCESSING = "processing"


@dataclass
class VoiceSession:
    telegram_id: int
    today_cards: list[dict] = field(default_factory=list)
    today_events: list[dict] = field(default_factory=list)
    transcripts: list[str] = field(default_factory=list)
    timer_task: asyncio.Task | None = None
    phase: str = VoicePhase.COLLECTING
    combined_text: str = ""
    started_at: datetime = field(default_factory=datetime.now)


_sessions: dict[int, VoiceSession] = {}

_bot: Bot | None = None
_llm: LLMClient | None = None
_trello: TrelloClient | None = None
_executor: ToolExecutor | None = None

evening_voice_router = Router()


def setup(bot: Bot, llm: LLMClient, trello: TrelloClient, executor: ToolExecutor) -> None:
    global _bot, _llm, _trello, _executor
    _bot = bot
    _llm = llm
    _trello = trello
    _executor = executor


# ─── Состояние ───

def has_active_session(telegram_id: int) -> bool:
    s = _sessions.get(telegram_id)
    if s is None:
        return False
    if datetime.now() - s.started_at > SESSION_TTL:
        cancel_session(telegram_id)
        return False
    return True


def is_collecting(telegram_id: int) -> bool:
    s = _sessions.get(telegram_id)
    return s is not None and s.phase == VoicePhase.COLLECTING


def is_awaiting_confirmation(telegram_id: int) -> bool:
    s = _sessions.get(telegram_id)
    return s is not None and s.phase == VoicePhase.AWAITING_CONFIRMATION


def cancel_session(telegram_id: int) -> bool:
    s = _sessions.pop(telegram_id, None)
    if s and s.timer_task and not s.timer_task.done():
        s.timer_task.cancel()
    return s is not None


def cleanup_expired() -> int:
    """Удалить просроченные сессии. Возвращает кол-во удалённых."""
    now = datetime.now()
    expired = [tid for tid, s in _sessions.items() if now - s.started_at > SESSION_TTL]
    for tid in expired:
        cancel_session(tid)
        logger.info("evening_voice: сессия {} удалена по TTL", tid)
    return len(expired)


# ─── Запуск ───

def start_session(telegram_id: int, today_cards: list[dict], today_events: list[dict]) -> None:
    """Начать сессию сбора голосовых от Абая. Таймер запустится с первого голосового."""
    cancel_session(telegram_id)
    _sessions[telegram_id] = VoiceSession(
        telegram_id=telegram_id,
        today_cards=today_cards,
        today_events=today_events,
    )
    logger.info("evening_voice: сессия для {} начата", telegram_id)


# ─── Приём голосовых ───

async def append_voice(telegram_id: int, transcript: str) -> None:
    """Добавить транскрипт. Перезапустить 5-минутный таймер."""
    s = _sessions.get(telegram_id)
    if s is None:
        return

    # Если пришёл новый голос после эхо — это поправка, начинаем сначала
    if s.phase == VoicePhase.AWAITING_CONFIRMATION:
        s.transcripts = []
        s.combined_text = ""
        s.phase = VoicePhase.COLLECTING
        logger.info("evening_voice: {} прислал новый голос после эхо — сброс", telegram_id)

    s.transcripts.append(transcript.strip())

    # Перезапуск таймера
    if s.timer_task and not s.timer_task.done():
        s.timer_task.cancel()
    s.timer_task = asyncio.create_task(_timeout_handler(telegram_id))
    logger.info(
        "evening_voice: транскрипт от {} добавлен ({} всего), таймер 5 мин перезапущен",
        telegram_id, len(s.transcripts),
    )


async def _timeout_handler(telegram_id: int) -> None:
    """Через 5 мин после последнего голосового — отправить эхо в группу."""
    try:
        await asyncio.sleep(VOICE_TIMEOUT_SECONDS)
    except asyncio.CancelledError:
        return

    s = _sessions.get(telegram_id)
    if s is None or s.phase != VoicePhase.COLLECTING:
        return

    combined = " ".join(t for t in s.transcripts if t).strip()
    if not combined:
        cancel_session(telegram_id)
        return

    s.combined_text = combined
    s.phase = VoicePhase.AWAITING_CONFIRMATION

    settings = get_settings()
    chat_id = settings.telegram_group_id or settings.telegram_owner_id
    mention = _mention_assistant()

    text = (
        f"{mention}, я понял Вас так:\n\n"
        f"<i>«{_escape_html(combined)}»</i>\n\n"
        f"Всё верно? Если да — создам новые задачи и обновлю доску."
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Да, всё верно", callback_data="evgroup_yes"),
        InlineKeyboardButton(text="✏️ Поправить", callback_data="evgroup_edit"),
        InlineKeyboardButton(text="❌ Отменить", callback_data="evgroup_cancel"),
    ]])

    try:
        await _bot.send_message(chat_id, text, parse_mode=ParseMode.HTML, reply_markup=kb)
        logger.info("evening_voice: эхо отправлено в группу для {}", telegram_id)
    except Exception as e:
        logger.error("evening_voice: ошибка отправки эхо: {}", e)


# ─── Подтверждение и обработка ───

@evening_voice_router.callback_query(lambda c: c.data and c.data.startswith("evgroup_"))
async def on_confirmation_callback(callback: CallbackQuery) -> None:
    """Обработать кнопки эхо-подтверждения. Реагируем только на assistant."""
    settings = get_settings()
    if callback.from_user.id != settings.telegram_assistant_id:
        await callback.answer("Только Абай может ответить.", show_alert=False)
        return

    s = _sessions.get(settings.telegram_assistant_id)
    if s is None:
        await callback.answer("Сессия завершена.", show_alert=False)
        return

    action = callback.data.split("_", 1)[1]
    await callback.answer()

    if action == "yes":
        s.phase = VoicePhase.PROCESSING
        try:
            await callback.message.edit_text(
                f"✅ Принято. Обрабатываю Ваш ответ...\n\n<i>«{_escape_html(s.combined_text)}»</i>",
                parse_mode=ParseMode.HTML,
            )
        except Exception:
            pass
        try:
            await _process_via_llm(s)
        finally:
            cancel_session(settings.telegram_assistant_id)

    elif action == "edit":
        s.phase = VoicePhase.COLLECTING
        s.transcripts = []
        s.combined_text = ""
        if s.timer_task and not s.timer_task.done():
            s.timer_task.cancel()
        try:
            await callback.message.edit_text(
                "✏️ Хорошо, жду новые голосовые. Через 5 минут после последнего соберу заново.",
            )
        except Exception:
            pass

    elif action == "cancel":
        cancel_session(settings.telegram_assistant_id)
        try:
            await callback.message.edit_text("❌ Отменено.")
        except Exception:
            pass


async def _process_via_llm(session: VoiceSession) -> None:
    """Передать ответ Абая в LLM с Trello-инструментами. Создать/переместить карточки."""
    settings = get_settings()
    chat_id = settings.telegram_group_id or settings.telegram_owner_id

    # Контекст: текущие карточки «Сегодня» — для сопоставления
    cards_context = "\n".join(
        f"- [{c.get('id', '?')}] {c.get('name', '?')}" for c in session.today_cards
    ) or "(пусто)"

    today_str = datetime.now().strftime("%Y-%m-%d (%A)")

    system = f"""Ты разбираешь голосовой ответ Абая на вечерний свод и выполняешь действия в Trello.

Сегодня: {today_str}

Текущие карточки в колонке «Сегодня» (с ID):
{cards_context}

Твоя задача — из ответа Абая извлечь и выполнить:

1. **Завершённые задачи** («сделал X», «прошла встреча с Y») → move_trello_card в колонку «ГОТОВО». Карточка из текущего списка выше — используй её ID.
2. **Перенесённые задачи** → если упомянута дата:
   - сегодня/завтра → колонка «сегодня» (через update_trello_card с due)
   - на этой неделе → колонка «неделя»
   - дальше → нужный месяц (Май, Июнь и т.д.)
3. **Новые задачи** (включая те, что Абай услышал из переписок WhatsApp) → create_trello_card в «сегодня». Если упомянут ответственный — добавь через тире: «Название — Имя».
4. **Делегированные задачи** (передал X сделать Y, ждём ОТ КОГО-ТО действия) → move_trello_card в «Мяч на стороне» + rename_trello_card с ответственным через тире. НЕ путать с «прошла встреча» — это завершено, идёт в ГОТОВО.

**Возможный дубль:** если create_trello_card вернул `status=possible_duplicate`, в ответе будут `candidates` — ID существующих карточек. НЕ повторяй create. Вместо этого работай с найденной карточкой: add_trello_comment / move_trello_card / update_trello_card.

Если Абай сказал что-то непонятное или неактуальное для задач — пропусти, не выдумывай.

После всех действий напиши КРАТКО (3-7 строк) что сделал, на русском, обращаясь к Абаю на «Вы». Без эмодзи в начале строк, без markdown-заголовков ##."""

    user_message = f"Ответ Абая на вечерний свод:\n\n«{session.combined_text}»"

    messages = [{"role": "user", "content": user_message}]
    actions_done: list[str] = []

    try:
        for _round in range(MAX_TOOL_ROUNDS):
            resp = await _llm.chat_with_tools(messages=messages, tools=TOOLS, system=system)

            text_parts = []
            tool_uses = []
            for block in resp.content:
                if block.type == "text":
                    text_parts.append(block.text)
                elif block.type == "tool_use":
                    tool_uses.append(block)

            if not tool_uses:
                final_text = "\n".join(text_parts).strip()
                await _send_result(chat_id, final_text, actions_done)
                return

            # Выполнить tools
            messages.append({"role": "assistant", "content": resp.content})
            tool_results = []
            for tu in tool_uses:
                try:
                    result = await _executor.execute(tu.name, tu.input, context={"telegram_id": session.telegram_id})
                    result_str = str(result) if result is not None else "OK"
                    # NB: при возврате status=possible_duplicate карточка НЕ создана —
                    # не записываем в actions_done, иначе fallback-сводка соврёт.
                    if not _is_no_op_result(result_str):
                        actions_done.append(_describe_action(tu.name, tu.input))
                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": tu.id,
                        "content": result_str[:500],
                    })
                except Exception as e:
                    logger.error("evening_voice: tool {} ошибка: {}", tu.name, e)
                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": tu.id,
                        "content": f"Ошибка: {e}",
                        "is_error": True,
                    })
            messages.append({"role": "user", "content": tool_results})

        # Превышен лимит раундов — отправим сводку действий
        await _send_result(chat_id, "Готово.", actions_done)

    except Exception as e:
        logger.error("evening_voice: ошибка LLM-обработки: {}", e)
        await _send_result(
            chat_id,
            "Не удалось обработать ответ через LLM. Что успел сделать:",
            actions_done,
        )


async def _send_result(chat_id: int, text: str, actions: list[str]) -> None:
    """Отправить итог в группу. Если LLM ничего не написал — собрать из действий."""
    if not text.strip():
        if actions:
            text = "Сделал:\n" + "\n".join(f"• {a}" for a in actions)
        else:
            text = "Ничего нового извлечь не получилось."
    try:
        await _bot.send_message(chat_id, _escape_html(text), parse_mode=ParseMode.HTML)
    except Exception:
        try:
            await _bot.send_message(chat_id, text)
        except Exception as e:
            logger.error("evening_voice: не удалось отправить итог: {}", e)


# ─── Вспомогательные ───

def _mention_assistant() -> str:
    """Сформировать упоминание Абая для группы."""
    s = get_settings()
    if s.telegram_assistant_username:
        return f"@{s.telegram_assistant_username}"
    if s.telegram_assistant_id:
        return f'<a href="tg://user?id={s.telegram_assistant_id}">Абай</a>'
    return "Абай"


def _escape_html(text: str) -> str:
    """Экранировать HTML-спецсимволы для безопасной вставки в parse_mode=HTML."""
    return (
        text.replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
    )


def _is_no_op_result(result_str: str) -> bool:
    """Результат tool, при котором действие фактически НЕ произошло — сводку не загрязнять."""
    if not result_str:
        return False
    return "possible_duplicate" in result_str or result_str.startswith("Ошибка")


def _describe_action(tool_name: str, inp: dict) -> str:
    """Краткое описание выполненного действия для сводки."""
    if tool_name == "create_trello_card":
        return f"создал карточку «{inp.get('name', '?')}»"
    if tool_name == "move_trello_card":
        return f"перемещение → «{inp.get('target_list', '?')}»"
    if tool_name == "update_trello_card":
        return f"обновил карточку {inp.get('card_id', '?')[:8]}"
    if tool_name == "rename_trello_card":
        return f"переименовал в «{inp.get('new_name', '?')}»"
    if tool_name == "add_trello_comment":
        return "добавил комментарий"
    return tool_name
