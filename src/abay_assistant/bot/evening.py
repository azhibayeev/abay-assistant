"""Вечерний свод — state machine для пошагового обзора дня."""

from dataclasses import dataclass, field
from datetime import datetime, timedelta
import re

from aiogram import Bot, Router
from aiogram.enums import ParseMode
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery
from loguru import logger

from abay_assistant.db import save_daily_stat
from abay_assistant.enums import EveningPhase, TaskStatus, FailedAction, TrelloList
from abay_assistant.services.trello import TrelloClient
from abay_assistant.services.obsidian import ObsidianClient


@dataclass
class TaskResult:
    name: str
    card_id: str
    status: str = ""
    comment: str = ""
    failed_action: str = ""
    followups: list[str] = field(default_factory=list)


SESSION_TTL = timedelta(hours=2)


@dataclass
class EveningSession:
    telegram_id: int
    tasks: list[TaskResult] = field(default_factory=list)
    events: list[dict] = field(default_factory=list)
    current_step: int = 0
    phase: str = EveningPhase.TASKS
    energy: int = 0
    charged_by: str = ""
    drained_by: str = ""
    created_at: datetime = field(default_factory=datetime.now)

    @property
    def current_task(self) -> TaskResult | None:
        if self.current_step < len(self.tasks):
            return self.tasks[self.current_step]
        return None


# Активные сессии
_sessions: dict[int, EveningSession] = {}

# Сервисы
_bot: Bot | None = None
_trello: TrelloClient | None = None
_obsidian: ObsidianClient | None = None

# Роутер
evening_router = Router()

# ─── Клавиатуры ───

TASK_KB = InlineKeyboardMarkup(inline_keyboard=[
    [
        InlineKeyboardButton(text="✅ Сделано", callback_data="eve_done"),
        InlineKeyboardButton(text="⏭ Перенесено", callback_data="eve_postponed"),
    ],
    [
        InlineKeyboardButton(text="🤝 Мяч на стороне", callback_data="eve_waiting"),
        InlineKeyboardButton(text="⛔ Не получилось", callback_data="eve_failed"),
    ],
])

YESNO_KB = InlineKeyboardMarkup(inline_keyboard=[[
    InlineKeyboardButton(text="Да", callback_data="eve_followup_yes"),
    InlineKeyboardButton(text="Нет", callback_data="eve_followup_no"),
]])

FAILED_ACTION_KB = InlineKeyboardMarkup(inline_keyboard=[[
    InlineKeyboardButton(text="Оставить на завтра", callback_data="eve_fail_keep"),
    InlineKeyboardButton(text="В Backlog", callback_data="eve_fail_backlog"),
    InlineKeyboardButton(text="Архивировать", callback_data="eve_fail_archive"),
]])

ENERGY_KB = InlineKeyboardMarkup(inline_keyboard=[[
    InlineKeyboardButton(text="1", callback_data="eve_energy_1"),
    InlineKeyboardButton(text="2", callback_data="eve_energy_2"),
    InlineKeyboardButton(text="3", callback_data="eve_energy_3"),
    InlineKeyboardButton(text="4", callback_data="eve_energy_4"),
    InlineKeyboardButton(text="5", callback_data="eve_energy_5"),
]])


def setup(bot: Bot, trello: TrelloClient, obsidian: ObsidianClient) -> None:
    global _bot, _trello, _obsidian
    _bot = bot
    _trello = trello
    _obsidian = obsidian


# ─── Callback handler ───

@evening_router.callback_query(lambda c: c.data and c.data.startswith("eve_"))
async def on_evening_callback(callback: CallbackQuery) -> None:
    tid = callback.from_user.id
    session = _sessions.get(tid)
    if not session:
        await callback.answer("Сессия завершена")
        return

    data = callback.data
    await callback.answer()
    task = session.current_task

    # ── Основные кнопки задачи ──

    if data == "eve_done" and task:
        task.status = TaskStatus.DONE
        try:
            await _trello.archive_card(task.card_id)
        except Exception as e:
            logger.error("Ошибка архивации {}: {}", task.card_id, e)
        session.phase = EveningPhase.FOLLOWUP_ASK
        await _send(tid, f"✅ <b>{task.name}</b> — архивировал.\n\nЕсть вытекающие задачи?", reply_markup=YESNO_KB)

    elif data == "eve_postponed" and task:
        task.status = TaskStatus.POSTPONED
        session.phase = EveningPhase.POSTPONE_WHEN
        await _send(tid, f"⏭ <b>{task.name}</b>\n\nНа когда перенести?")

    elif data == "eve_waiting" and task:
        task.status = TaskStatus.WAITING
        session.phase = EveningPhase.WAITING_WHO
        await _send(tid, f"🤝 <b>{task.name}</b>\n\nНа чьей стороне мяч?")

    elif data == "eve_failed" and task:
        task.status = TaskStatus.FAILED
        session.phase = EveningPhase.FAILED_REASON
        await _send(tid, f"⛔ <b>{task.name}</b>\n\nПочему не получилось?")

    # ── Follow-up (после ✅) ──

    elif data == "eve_followup_yes":
        session.phase = EveningPhase.FOLLOWUP_NAME
        await _send(tid, "Напиши название новой задачи:")

    elif data == "eve_followup_no":
        session.current_step += 1
        session.phase = EveningPhase.TASKS
        await _next_task_or_energy(session)

    # ── Failed action (после ⛔) ──

    elif data == "eve_fail_keep" and task:
        task.failed_action = FailedAction.KEEP
        session.current_step += 1
        session.phase = EveningPhase.TASKS
        await _send(tid, "Оставил в «Сегодня».")
        await _next_task_or_energy(session)

    elif data == "eve_fail_backlog" and task:
        task.failed_action = FailedAction.BACKLOG
        try:
            await _trello.move_card(task.card_id, TrelloList.BACKLOG)
        except Exception as e:
            logger.error("Ошибка перемещения в Backlog {}: {}", task.card_id, e)
        session.current_step += 1
        session.phase = EveningPhase.TASKS
        await _send(tid, "Перенёс в «Backlog».")
        await _next_task_or_energy(session)

    elif data == "eve_fail_archive" and task:
        task.failed_action = FailedAction.ARCHIVE
        try:
            await _trello.archive_card(task.card_id)
        except Exception as e:
            logger.error("Ошибка архивации {}: {}", task.card_id, e)
        session.current_step += 1
        session.phase = EveningPhase.TASKS
        await _send(tid, "Архивировал.")
        await _next_task_or_energy(session)

    # ── Энергия ──

    elif data.startswith("eve_energy_"):
        energy = int(data.split("_")[-1])
        session.energy = energy
        session.phase = EveningPhase.CHARGED
        await _send(tid, "Что зарядило сегодня?")


# ─── Public API ───

def _is_expired(session: EveningSession) -> bool:
    return datetime.now() - session.created_at > SESSION_TTL


def cleanup_expired_sessions() -> int:
    """Удалить просроченные сессии. Возвращает кол-во удалённых."""
    expired = [tid for tid, s in _sessions.items() if _is_expired(s)]
    for tid in expired:
        del _sessions[tid]
        logger.info("Сессия {} удалена по TTL", tid)
    return len(expired)


def has_active_session(telegram_id: int) -> bool:
    session = _sessions.get(telegram_id)
    if session is None:
        return False
    if _is_expired(session):
        del _sessions[telegram_id]
        logger.info("Сессия {} истекла по TTL", telegram_id)
        return False
    return True


def cancel_session(telegram_id: int) -> bool:
    """Отменить активную сессию. Возвращает True если была активная."""
    if telegram_id in _sessions:
        del _sessions[telegram_id]
        return True
    return False


async def start_session(telegram_id: int, today_cards: list[dict], today_events: list[dict]) -> None:
    tasks = [TaskResult(name=c["name"], card_id=c["id"]) for c in today_cards]
    session = EveningSession(telegram_id=telegram_id, tasks=tasks, events=today_events)
    _sessions[telegram_id] = session

    if not tasks:
        await _send(telegram_id, "Сегодня задач не было. Спокойной ночи!")
        del _sessions[telegram_id]
        return

    await _send(telegram_id, f"Пройдёмся по дню? У тебя <b>{len(tasks)}</b> задач.")
    await _ask_current_task(session)


async def handle_response(telegram_id: int, text: str) -> bool:
    """Обработать текстовый ответ. Возвращает True если обработано."""
    session = _sessions.get(telegram_id)
    if not session:
        return False

    text = text.strip()
    task = session.current_task

    if session.phase == EveningPhase.FOLLOWUP_NAME and task:
        try:
            await _trello.create_card(
                name=text,
                list_name=TrelloList.TODAY,
                desc=f"Возникла из: {task.name}",
            )
            task.followups.append(text)
            await _send(telegram_id, f"Создал: <b>{text}</b>\n\nЕщё вытекающие?", reply_markup=YESNO_KB)
            session.phase = EveningPhase.FOLLOWUP_ASK
        except Exception as e:
            logger.error("Ошибка создания карточки: {}", e)
            await _send(telegram_id, "Ошибка создания. Попробуй ещё раз:")

    elif session.phase == EveningPhase.POSTPONE_WHEN and task:
        task.comment = text
        due = _parse_due(text)
        if due:
            try:
                await _trello.update_card(task.card_id, due=due)
                await _send(telegram_id, f"Поставил срок: <b>{due[:10]}</b>")
            except Exception as e:
                logger.error("Ошибка установки due {}: {}", task.card_id, e)
        session.current_step += 1
        session.phase = EveningPhase.TASKS
        await _next_task_or_energy(session)

    elif session.phase == EveningPhase.WAITING_WHO and task:
        person = text
        old_name = task.name
        new_name = f"{old_name} — {person}"
        try:
            # Переименовать с историей
            await _trello.add_comment(
                task.card_id,
                f"📝 Переименовано: «{old_name}» → «{new_name}»",
            )
            await _trello.update_card(task.card_id, name=new_name)
            # Переместить в "Мяч на стороне"
            await _trello.move_card(task.card_id, TrelloList.WAITING)
            task.name = new_name
            task.comment = f"мяч у {person}"
            await _send(telegram_id, f"Переместил в «Мяч на стороне»: <b>{new_name}</b>")
        except Exception as e:
            logger.error("Ошибка перемещения в Мяч на стороне {}: {}", task.card_id, e)
            await _send(telegram_id, f"Ошибка: {e}")
        session.current_step += 1
        session.phase = EveningPhase.TASKS
        await _next_task_or_energy(session)

    elif session.phase == EveningPhase.FAILED_REASON and task:
        task.comment = text
        session.phase = EveningPhase.FAILED_ACTION
        await _send(telegram_id, "Что делаем с карточкой?", reply_markup=FAILED_ACTION_KB)

    elif session.phase == EveningPhase.ENERGY:
        try:
            energy = int(text)
            if 1 <= energy <= 5:
                session.energy = energy
                session.phase = EveningPhase.CHARGED
                await _send(telegram_id, "Что зарядило сегодня?")
                return True
        except ValueError:
            pass
        await _send(telegram_id, "Введи число от 1 до 5")

    elif session.phase == EveningPhase.CHARGED:
        session.charged_by = text
        session.phase = EveningPhase.DRAINED
        await _send(telegram_id, "Что выжало?")

    elif session.phase == EveningPhase.DRAINED:
        session.drained_by = text
        session.phase = EveningPhase.DONE
        await _finish(session)

    elif session.phase in (EveningPhase.TASKS, EveningPhase.FOLLOWUP_ASK, EveningPhase.FAILED_ACTION):
        await _send(telegram_id, "Нажми на кнопку выше ☝️")

    return True


# ─── Навигация ───

async def _next_task_or_energy(session: EveningSession) -> None:
    if session.current_step < len(session.tasks):
        await _ask_current_task(session)
    else:
        session.phase = EveningPhase.ENERGY
        await _send(
            session.telegram_id,
            "Все задачи разобраны.\n\nЭнергия дня от 1 до 5?",
            reply_markup=ENERGY_KB,
        )


async def _ask_current_task(session: EveningSession) -> None:
    task = session.current_task
    num = session.current_step + 1
    total = len(session.tasks)
    await _send(
        session.telegram_id,
        f"<b>[{num}/{total}]</b> {task.name}",
        reply_markup=TASK_KB,
    )


# ─── Завершение ───

async def _finish(session: EveningSession) -> None:
    tid = session.telegram_id

    done_list = []
    postponed_list = []
    failed_list = []
    waiting_list = []

    for task in session.tasks:
        if task.status == TaskStatus.DONE:
            done_list.append(task)
        elif task.status == TaskStatus.POSTPONED:
            postponed_list.append(task)
        elif task.status == TaskStatus.FAILED:
            failed_list.append(task)
        elif task.status == TaskStatus.WAITING:
            waiting_list.append(task)

    # Daily note — append evening review, preserve existing content
    today = datetime.now().strftime("%Y-%m-%d")

    lines = [
        "",
        "## Вечерний свод",
        "",
        "### Что было сделано",
    ]
    for t in done_list:
        line = f"- ✅ {t.name}"
        if t.followups:
            line += " → " + ", ".join(t.followups)
        lines.append(line)
    if not done_list:
        lines.append("- (ничего не завершено)")

    if postponed_list:
        lines.append("")
        lines.append("### Перенесено")
        for t in postponed_list:
            lines.append(f"- ⏭ {t.name} — {t.comment}")

    if waiting_list:
        lines.append("")
        lines.append("### Мяч на стороне")
        for t in waiting_list:
            lines.append(f"- 🤝 {t.name}")

    if failed_list:
        lines.append("")
        lines.append("### Не получилось")
        for t in failed_list:
            action = {
                FailedAction.KEEP: "оставлено",
                FailedAction.BACKLOG: "→ Backlog",
                FailedAction.ARCHIVE: "архив",
            }.get(t.failed_action, "")
            lines.append(f"- ⛔ {t.name} — {t.comment} [{action}]")

    lines.extend(["", "### Энергия",
        f"- Уровень: {session.energy}/5",
        f"- Зарядило: {session.charged_by}",
        f"- Выжало: {session.drained_by}",
    ])

    await _obsidian.append_daily("\n".join(lines))

    # Сохранить структурированную статистику в БД для weekly report
    save_daily_stat(
        date=today,
        done_count=len(done_list),
        postponed_count=len(postponed_list),
        failed_count=len(failed_list),
        energy=session.energy,
        charged_by=session.charged_by,
        drained_by=session.drained_by,
    )

    summary = (
        f"Записал итоги дня.\n\n"
        f"✅ Сделано: {len(done_list)}\n"
        f"⏭ Перенесено: {len(postponed_list)}\n"
        f"🤝 Мяч на стороне: {len(waiting_list)}\n"
        f"⛔ Не получилось: {len(failed_list)}\n"
        f"Энергия: {session.energy}/5\n\n"
        f"Спокойной ночи!"
    )
    await _send(tid, summary)
    del _sessions[tid]
    logger.info("Вечерний свод завершён для {}", tid)


def _parse_due(text: str) -> str | None:
    """Попытаться распознать дату из текста. Возвращает ISO строку или None."""
    text_lower = text.lower().strip()
    now = datetime.now()

    # «послезавтра» (check before «завтра» since it contains the substring)
    if "послезавтра" in text_lower:
        d = now + timedelta(days=2)
        return d.strftime("%Y-%m-%dT09:00:00")

    # «завтра»
    if "завтра" in text_lower:
        d = now + timedelta(days=1)
        return d.strftime("%Y-%m-%dT09:00:00")

    # «через N дней/дня/день»
    m = re.search(r"через\s+(\d+)\s+(?:день|дня|дней)", text_lower)
    if m:
        d = now + timedelta(days=int(m.group(1)))
        return d.strftime("%Y-%m-%dT09:00:00")

    # «в понедельник/вторник/...»
    days_map = {
        "понедельник": 0, "вторник": 1, "среду": 2, "среда": 2,
        "четверг": 3, "пятницу": 4, "пятница": 4,
        "субботу": 5, "суббота": 5, "воскресенье": 6,
    }
    for day_name, day_num in days_map.items():
        if day_name in text_lower:
            current_day = now.weekday()
            diff = (day_num - current_day) % 7
            if diff == 0:
                diff = 7  # следующая неделя
            d = now + timedelta(days=diff)
            return d.strftime("%Y-%m-%dT09:00:00")

    # «на следующей неделе»
    if "следующ" in text_lower and "недел" in text_lower:
        diff = (0 - now.weekday()) % 7 + 7  # следующий понедельник
        d = now + timedelta(days=diff)
        return d.strftime("%Y-%m-%dT09:00:00")

    # Прямая дата: «1 мая», «15.05», «2026-05-01»
    months_map = {
        "январ": 1, "феврал": 2, "март": 3, "апрел": 4,
        "мая": 5, "май": 5, "июн": 6, "июл": 7, "август": 8,
        "сентябр": 9, "октябр": 10, "ноябр": 11, "декабр": 12,
    }
    # «15 мая», «1 июня»
    m = re.search(r"(\d{1,2})\s+(\w+)", text_lower)
    if m:
        day = int(m.group(1))
        month_text = m.group(2)
        for key, month_num in months_map.items():
            if month_text.startswith(key):
                year = now.year
                try:
                    d = datetime(year, month_num, day, 9, 0)
                    if d < now:
                        d = datetime(year + 1, month_num, day, 9, 0)
                    return d.strftime("%Y-%m-%dT09:00:00")
                except ValueError:
                    pass

    # «15.05» или «15.05.2026»
    m = re.search(r"(\d{1,2})\.(\d{1,2})(?:\.(\d{4}))?", text_lower)
    if m:
        day = int(m.group(1))
        month = int(m.group(2))
        year = int(m.group(3)) if m.group(3) else now.year
        try:
            d = datetime(year, month, day, 9, 0)
            if d < now and not m.group(3):
                d = datetime(year + 1, month, day, 9, 0)
            return d.strftime("%Y-%m-%dT09:00:00")
        except ValueError:
            pass

    return None


async def _send(telegram_id: int, text: str, reply_markup=None) -> None:
    try:
        await _bot.send_message(
            telegram_id, text, parse_mode=ParseMode.HTML, reply_markup=reply_markup
        )
    except Exception as e:
        logger.error("Ошибка отправки {}: {}", telegram_id, e)
