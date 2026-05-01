"""Обработчики расписания — утренняя сводка, полуденный чек-ин, вечерний свод, weekly-отчёт, напоминания, проактивные фичи."""

import asyncio
import calendar as cal_mod
import json
import re
from collections import defaultdict
from datetime import date as date_type, datetime, timedelta
from pathlib import Path

from aiogram import Bot, Router
from aiogram.enums import ParseMode
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery
from loguru import logger

from abay_assistant.config import get_settings
from abay_assistant.db import claim_due_reminders, get_daily_stats
from abay_assistant.enums import TrelloList
from abay_assistant.services.llm import LLMClient
from abay_assistant.services.trello import TrelloClient
from abay_assistant.services.calendar import CalendarClient
from abay_assistant.services.obsidian import ObsidianClient

MORNING_PROMPT = Path(__file__).resolve().parent.parent / "prompts" / "morning.md"
WEEKLY_PROMPT = Path(__file__).resolve().parent.parent / "prompts" / "weekly.md"

# Сервисы — инициализируются через setup()
_bot: Bot | None = None
_llm: LLMClient | None = None
_trello: TrelloClient | None = None
_calendar: CalendarClient | None = None
_obsidian: ObsidianClient | None = None

# Дедупликация уведомлений о встречах (in-memory, сбрасывается при рестарте)
_notified_meetings: set[str] = set()

# Роутер для nudge callback-ов
nudge_router = Router()


def setup(
    bot: Bot,
    llm: LLMClient,
    trello: TrelloClient,
    calendar: CalendarClient,
    obsidian: ObsidianClient,
) -> None:
    global _bot, _llm, _trello, _calendar, _obsidian
    _bot = bot
    _llm = llm
    _trello = trello
    _calendar = calendar
    _obsidian = obsidian


def _target_chat() -> int:
    """Вернуть ID чата для проактивных сообщений: группа (если настроена) или owner DM."""
    s = get_settings()
    return s.telegram_group_id or s.telegram_owner_id


def _recipient_ids() -> list[int]:
    """Telegram ID пользователей для рассылки."""
    s = get_settings()
    ids = [s.telegram_owner_id]
    if s.telegram_assistant_id:
        ids.append(s.telegram_assistant_id)
    return ids


async def _send_to_all(text: str) -> None:
    """Отправить сообщение в группу (если настроена) или всем пользователям с retry при 429."""
    from abay_assistant.bot.handlers import _md_to_html
    from aiogram.enums import ParseMode

    html = _md_to_html(text)
    s = get_settings()
    targets = [s.telegram_group_id] if s.telegram_group_id else _recipient_ids()

    for tid in targets:
        for attempt in range(3):
            try:
                await _bot.send_message(tid, html, parse_mode=ParseMode.HTML)
                break
            except Exception as e:
                error_text = str(e).lower()
                if "retry after" in error_text or "too many requests" in error_text:
                    delay = 2 ** attempt
                    logger.warning("Telegram 429 для {}, retry через {}с", tid, delay)
                    await asyncio.sleep(delay)
                else:
                    logger.error("Не удалось отправить сообщение {}: {}", tid, e)
                    break


# ─────────────────────────────────────────────
# Каждые 60 сек — Напоминания
# ─────────────────────────────────────────────

async def check_reminders() -> None:
    """Проверить и отправить наступившие напоминания.

    Атомарно помечает как sent ДО отправки — предотвращает дубли.
    Если отправка провалится, напоминание уже не повторится (trade-off: лучше потерять, чем спамить).
    """
    try:
        claimed = claim_due_reminders()
        for reminder in claimed:
            try:
                await _bot.send_message(
                    reminder.telegram_id,
                    f"Напоминание: {reminder.text}",
                )
                logger.info("Напоминание #{} отправлено → {}", reminder.id, reminder.telegram_id)
            except Exception as e:
                logger.error("Ошибка отправки напоминания #{}: {}", reminder.id, e)
    except Exception as e:
        logger.error("Ошибка check_reminders: {}", e)


# ─────────────────────────────────────────────
# Каждые 10 мин — очистка зависших сессий
# ─────────────────────────────────────────────

async def cleanup_sessions() -> None:
    """Очистить просроченные вечерние сессии."""
    try:
        from abay_assistant.bot.evening import cleanup_expired_sessions
        removed = cleanup_expired_sessions()
        if removed:
            logger.info("Очищено {} просроченных сессий", removed)
    except Exception as e:
        logger.error("Ошибка cleanup_sessions: {}", e)


# ─────────────────────────────────────────────
# 9:00 — Утренняя сводка
# ─────────────────────────────────────────────

async def morning_briefing() -> None:
    """Собрать данные и отправить умную утреннюю сводку."""
    logger.info("Запуск утренней сводки")

    try:
        now = datetime.now()

        # Основные данные
        today_cards = await _get_today_cards()
        today_events = await _get_today_events()
        stale_cards = await _get_stale_cards()
        overdue_cards = await _get_overdue_cards()

        # Проактивные данные
        tomorrow_events = await _get_tomorrow_events()
        crm_followups = await _get_crm_followups(days_threshold=14)

        two_weeks_ago = now - timedelta(days=14)
        stats = get_daily_stats(
            two_weeks_ago.strftime("%Y-%m-%d"),
            now.strftime("%Y-%m-%d"),
        )
        energy_insight = _compute_energy_insight(stats, now.weekday())

        # Сформировать промпт
        template = MORNING_PROMPT.read_text(encoding="utf-8")
        system = (
            template
            .replace("{date}", now.strftime("%Y-%m-%d, %A"))
            .replace("{today_cards}", json.dumps(today_cards, ensure_ascii=False))
            .replace("{today_events}", json.dumps(today_events, ensure_ascii=False))
            .replace("{stale_cards}", json.dumps(stale_cards, ensure_ascii=False))
            .replace("{overdue_cards}", json.dumps(overdue_cards, ensure_ascii=False))
            .replace("{tomorrow_events}", json.dumps(tomorrow_events, ensure_ascii=False))
            .replace("{crm_followups}", json.dumps(crm_followups, ensure_ascii=False))
            .replace("{energy_insight}", energy_insight or "(нет данных)")
        )

        text = await _llm.chat(
            messages=[{"role": "user", "content": "Сформируй утреннюю сводку."}],
            system=system,
            max_tokens=2048,
        )

        await _send_to_all(text)
        logger.info("Утренняя сводка отправлена")

    except Exception as e:
        logger.error("Ошибка утренней сводки: {}", e)


_COVER_EMOJI = {
    "red": "🔴",
    "orange": "🟠",
    "green": "🟢",
    "blue": "🔵",
}


def _cover_emoji(card: dict) -> str:
    """Вернуть эмодзи цвета обложки карточки."""
    color = (card.get("cover") or {}).get("color")
    return _COVER_EMOJI.get(color, "⬜")


async def _get_today_cards() -> list[dict]:
    """Карточки из списка 'Сегодня'."""
    try:
        cards = await _trello.get_cards(TrelloList.TODAY)
        return [
            {
                "name": c["name"],
                "labels": [lb["name"] for lb in c.get("labels", [])],
                "cover": _cover_emoji(c),
            }
            for c in cards
        ]
    except Exception as e:
        logger.error("Trello get_cards ошибка: {}", e)
        return []


async def _get_today_events() -> list[dict]:
    """События из Google Calendar на сегодня."""
    try:
        return await _calendar.get_events()
    except Exception as e:
        logger.error("Calendar get_events ошибка: {}", e)
        return []


async def _get_stale_cards() -> list[dict]:
    """Карточки из 'Мяч на стороне'."""
    try:
        cards = await _trello.get_cards(TrelloList.WAITING)
        return [{"name": c["name"]} for c in cards]
    except Exception as e:
        logger.error("Trello stale cards ошибка: {}", e)
        return []


async def _get_overdue_cards() -> list[dict]:
    """Карточки с просроченным дедлайном (due < now) из всех активных списков."""
    now = datetime.now().isoformat()
    overdue = []
    for list_name in (TrelloList.TODAY, TrelloList.WEEK):
        try:
            cards = await _trello.get_cards(list_name)
            for c in cards:
                due = c.get("due")
                if due and due < now:
                    overdue.append({
                        "name": c["name"],
                        "due": due[:10],
                        "list": list_name,
                    })
        except Exception as e:
            logger.error("Trello overdue '{}' ошибка: {}", list_name, e)
    return overdue


# ─────────────────────────────────────────────
# 14:00 — Полуденный чек-ин
# ─────────────────────────────────────────────

async def noon_checkin() -> None:
    """Полуденный чек-ин с прогрессом по задачам."""
    logger.info("Запуск полуденного чек-ина")
    try:
        today_cards = await _get_today_cards()
        overdue_cards = await _get_overdue_cards()
        count = len(today_cards)

        if count == 0:
            text = "Полдня позади. Список «Сегодня» пуст — добавь задачи или отдыхай."
        else:
            names = "\n".join(f"  {c.get('cover', '⬜')} {c['name']}" for c in today_cards[:7])
            text = (
                f"Полдня позади. В «Сегодня» осталось <b>{count}</b> задач:\n"
                f"{names}\n\n"
            )

            if overdue_cards:
                overdue_names = "\n".join(
                    f"  ⚠️ {c['name']} (срок: {c['due']})" for c in overdue_cards[:5]
                )
                text += f"<b>Просроченные:</b>\n{overdue_names}\n\n"

            text += "Что-то поменялось в планах?"

        chat_id = _target_chat()
        try:
            await _bot.send_message(chat_id, text, parse_mode=ParseMode.HTML)
        except Exception:
            await _bot.send_message(chat_id, text)
        logger.info("Полуденный чек-ин отправлен")

    except Exception as e:
        logger.error("Ошибка полуденного чек-ина: {}", e)


# ─────────────────────────────────────────────
# 22:30 — Вечерний свод
# ─────────────────────────────────────────────

async def evening_review() -> None:
    """Запустить пошаговый вечерний свод."""
    logger.info("Запуск вечернего свода")

    from abay_assistant.bot.evening import start_session

    try:
        # Получить карточки с ID (нужны для архивации)
        raw_cards = await _trello.get_cards(TrelloList.TODAY)
        today_cards = [
            {"id": c["id"], "name": c["name"]}
            for c in raw_cards
        ]
        today_events = await _get_today_events()

        s = get_settings()
        # Сессия привязана к owner_id, но сообщения пойдут в группу (если настроена)
        await start_session(s.telegram_owner_id, today_cards, today_events)
        logger.info("Вечерний свод запущен")

    except Exception as e:
        logger.error("Ошибка вечернего свода: {}", e)


# ─────────────────────────────────────────────
# Воскресенье 21:00 — Еженедельный отчёт
# ─────────────────────────────────────────────

async def weekly_report() -> None:
    """Собрать данные за неделю и отправить отчёт."""
    logger.info("Запуск weekly-отчёта")

    try:
        # Период: последние 7 дней
        today = datetime.now()
        week_ago = today - timedelta(days=7)
        week_range = f"{week_ago.strftime('%d.%m')} — {today.strftime('%d.%m.%Y')}"

        # Собрать daily notes за неделю
        daily_notes = await _get_weekly_daily_notes(week_ago, today)

        # Структурированная статистика из БД
        stats = get_daily_stats(
            week_ago.strftime("%Y-%m-%d"),
            today.strftime("%Y-%m-%d"),
        )
        total_done = sum(s.done_count for s in stats)
        total_postponed = sum(s.postponed_count for s in stats)
        total_failed = sum(s.failed_count for s in stats)
        energy_stats = [s for s in stats if s.energy > 0]
        avg_energy = round(sum(s.energy for s in energy_stats) / len(energy_stats), 1) if energy_stats else 0
        charged_items = [s.charged_by for s in stats if s.charged_by]
        drained_items = [s.drained_by for s in stats if s.drained_by]

        weekly_stats = (
            f"Завершено задач: {total_done}\n"
            f"Перенесено: {total_postponed}\n"
            f"Не получилось: {total_failed}\n"
            f"Средняя энергия: {avg_energy}/5\n"
            f"Что заряжало: {'; '.join(charged_items) or '(нет данных)'}\n"
            f"Что выжимало: {'; '.join(drained_items) or '(нет данных)'}"
        )

        # Текущее состояние Trello
        today_cards = await _get_today_cards()
        backlog_cards = await _safe_get_cards(TrelloList.BACKLOG)
        stale_cards = await _get_stale_cards()

        # Промпт
        template = WEEKLY_PROMPT.read_text(encoding="utf-8")
        system = (
            template
            .replace("{week_range}", week_range)
            .replace("{weekly_stats}", weekly_stats)
            .replace("{daily_notes}", daily_notes)
            .replace("{today_count}", str(len(today_cards)))
            .replace("{backlog_count}", str(len(backlog_cards)))
            .replace("{stale_cards}", json.dumps(stale_cards, ensure_ascii=False))
        )

        text = await _llm.chat(
            messages=[{"role": "user", "content": "Сформируй еженедельный отчёт."}],
            system=system,
        )

        await _send_to_all(text)
        logger.info("Weekly-отчёт отправлен")

    except Exception as e:
        logger.error("Ошибка weekly-отчёта: {}", e)


async def _get_weekly_daily_notes(start: datetime, end: datetime) -> str:
    """Прочитать daily notes за период."""
    notes = []
    current = start
    while current <= end:
        date_str = current.strftime("%Y-%m-%d")
        content = await _obsidian.read_note(f"Daily/{date_str}.md")
        if content:
            notes.append(f"--- {date_str} ---\n{content}")
        current += timedelta(days=1)
    return "\n\n".join(notes) if notes else "(нет записей за неделю)"


async def _safe_get_cards(list_name: str) -> list[dict]:
    """Получить карточки, вернуть пустой список при ошибке."""
    try:
        cards = await _trello.get_cards(list_name)
        return [{"name": c["name"]} for c in cards]
    except Exception as e:
        logger.error("Trello get_cards('{}') ошибка: {}", list_name, e)
        return []


# ─────────────────────────────────────────────
# Проактивные хелперы
# ─────────────────────────────────────────────

def _match_event_to_crm(event_summary: str, people: list[dict]) -> str | None:
    """Найти имя из CRM в названии события. Case-insensitive."""
    lower = event_summary.lower()
    for person in people:
        name = person.get("name", "")
        if name and len(name) >= 3 and name.lower() in lower:
            return name
    return None


def _filter_forgotten_contacts(
    people: list[dict],
    cutoff_days: int = 21,
    roles: tuple[str, ...] = ("партнёр", "клиент"),
) -> list[dict]:
    """Найти контакты с last_updated старше cutoff_days и ролью из roles."""
    today = date_type.today()
    result = []
    for person in people:
        role = person.get("role", "")
        if role not in roles:
            continue
        last_updated_str = person.get("last_updated", "")
        if not last_updated_str:
            result.append({
                "name": person["name"], "role": role,
                "last_updated": "(никогда)", "days_ago": 9999,
            })
            continue
        try:
            last_updated = date_type.fromisoformat(str(last_updated_str))
            days_ago = (today - last_updated).days
            if days_ago > cutoff_days:
                result.append({
                    "name": person["name"], "role": role,
                    "last_updated": str(last_updated_str), "days_ago": days_ago,
                })
        except (ValueError, TypeError):
            continue
    result.sort(key=lambda x: x["days_ago"], reverse=True)
    return result


def _find_stuck_cards(cards: list[dict], stale_days: int = 3) -> list[dict]:
    """Найти карточки без активности >stale_days по dateLastActivity."""
    cutoff = datetime.now() - timedelta(days=stale_days)
    result = []
    for c in cards:
        dla = c.get("dateLastActivity", "")
        if not dla:
            continue
        try:
            activity = datetime.fromisoformat(dla.replace("Z", "+00:00"))
            activity_naive = activity.replace(tzinfo=None)
            if activity_naive < cutoff:
                days_inactive = (datetime.now() - activity_naive).days
                result.append({
                    "id": c["id"],
                    "name": c["name"],
                    "days_inactive": days_inactive,
                    "cover": c.get("cover"),
                })
        except (ValueError, TypeError):
            continue
    return result


def _compute_energy_insight(
    stats: list,
    today_weekday: int,
    low_threshold: float = 3.0,
) -> str | None:
    """Средняя энергия по дням недели. Предупреждение если сегодняшний день слабый."""
    day_energies: dict[int, list[int]] = defaultdict(list)

    for s in stats:
        if s.energy <= 0:
            continue
        try:
            stat_date = datetime.strptime(s.date, "%Y-%m-%d")
            day_energies[stat_date.weekday()].append(s.energy)
        except ValueError:
            continue

    values = day_energies.get(today_weekday, [])
    if len(values) < 2:
        return None

    avg = sum(values) / len(values)
    if avg < low_threshold:
        day_names = [
            "понедельник", "вторник", "среда", "четверг",
            "пятница", "суббота", "воскресенье",
        ]
        day_name = day_names[today_weekday]
        return (
            f"По {day_name}ам средняя энергия {avg:.1f}/5 "
            f"(за {len(values)} нед.) — запланируй более лёгкий день."
        )
    return None


async def _get_tomorrow_events() -> list[dict]:
    """Завтрашние события из Calendar."""
    try:
        tomorrow = datetime.now() + timedelta(days=1)
        return await _calendar.get_events(date=tomorrow)
    except Exception as e:
        logger.error("Calendar tomorrow ошибка: {}", e)
        return []


async def _get_crm_followups(days_threshold: int = 14) -> list[dict]:
    """CRM-контакты, требующие внимания (без обновления >days_threshold дней)."""
    try:
        people = await _obsidian.list_entities("person")
        stale = _filter_forgotten_contacts(people, cutoff_days=days_threshold)
        return stale[:5]
    except Exception as e:
        logger.error("CRM followups ошибка: {}", e)
        return []


# ─────────────────────────────────────────────
# Каждые 5 мин — Подготовка к встречам
# ─────────────────────────────────────────────

async def meeting_prep() -> None:
    """Проверить календарь: если через ~30 минут встреча с человеком из CRM — отправить контекст."""
    try:
        chat_id = _target_chat()
        now = datetime.now()

        events = await _calendar.get_events(date=now)
        if not events:
            return

        people = await _obsidian.list_entities("person")

        for event in events:
            start_str = event.get("start", "")
            summary = event.get("summary", "")

            try:
                event_start = datetime.fromisoformat(start_str)
                event_start_naive = event_start.replace(tzinfo=None)
            except (ValueError, TypeError):
                continue

            # Окно: через 25-35 минут
            if not (now + timedelta(minutes=25) <= event_start_naive <= now + timedelta(minutes=35)):
                continue

            dedup_key = f"{start_str}|{summary}"
            if dedup_key in _notified_meetings:
                continue

            matched_name = _match_event_to_crm(summary, people)
            if not matched_name:
                continue

            crm_text = await _obsidian.get_entity_summary("person", matched_name)
            time_str = event_start_naive.strftime("%H:%M")
            message = f"Через ~30 мин ({time_str}): {summary}\n\nКонтекст из CRM:\n{crm_text}"

            await _bot.send_message(chat_id, message)
            _notified_meetings.add(dedup_key)
            logger.info("Meeting prep: {} (CRM: {})", summary, matched_name)

        # Очистить старые ключи (оставить только сегодняшние)
        today_prefix = now.strftime("%Y-%m-%d")
        stale_keys = {k for k in _notified_meetings if not k.startswith(today_prefix)}
        _notified_meetings.difference_update(stale_keys)

    except Exception as e:
        logger.error("Ошибка meeting_prep: {}", e)


# ─────────────────────────────────────────────
# Воскресенье 20:00 — Забытые контакты
# ─────────────────────────────────────────────

async def forgotten_contacts() -> None:
    """Еженедельный скан: контакты без обновления >21 дня."""
    logger.info("Запуск forgotten_contacts")
    try:
        chat_id = _target_chat()
        people = await _obsidian.list_entities("person")
        stale = _filter_forgotten_contacts(people)

        if not stale:
            logger.info("forgotten_contacts: нет забытых контактов")
            return

        lines = ["Контакты без обновления >21 дня:\n"]
        for p in stale[:10]:
            days = p["days_ago"]
            days_label = f"{days} дн." if days < 9999 else "никогда"
            lines.append(f"  — {p['name']} ({p['role']}) — {days_label}")
        lines.append("\nОткрой /who <имя> чтобы добавить обновление.")

        await _bot.send_message(chat_id, "\n".join(lines))
        logger.info("forgotten_contacts: {} контактов", len(stale))

    except Exception as e:
        logger.error("Ошибка forgotten_contacts: {}", e)


# ─────────────────────────────────────────────
# Каждый день 20:00 — Застрявшие задачи
# ─────────────────────────────────────────────

async def stuck_tasks() -> None:
    """Ежедневный скан: карточки без активности >3 дней — интерактивно."""
    logger.info("Запуск stuck_tasks")
    try:
        chat_id = _target_chat()

        today_raw = await _trello.get_cards(TrelloList.TODAY)
        stuck = _find_stuck_cards(today_raw, stale_days=3)

        if not stuck:
            logger.info("stuck_tasks: нет проблемных задач")
            return

        await _bot.send_message(
            chat_id,
            f"Застряли в «Сегодня» без активности >3 дней ({len(stuck)}):",
        )

        for c in stuck[:5]:
            emoji = _cover_emoji(c)
            card_id = c["id"]
            days = c["days_inactive"]
            kb = InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="✅ Сделано", callback_data=f"nudge_done_{card_id}"),
                InlineKeyboardButton(text="📦 Backlog", callback_data=f"stuck_backlog_{card_id}"),
                InlineKeyboardButton(text="🗑 Архив", callback_data=f"stuck_archive_{card_id}"),
            ]])
            await _bot.send_message(
                chat_id,
                f"{emoji} <b>{c['name']}</b> — {days} дн. без активности",
                parse_mode=ParseMode.HTML,
                reply_markup=kb,
            )

        logger.info("stuck_tasks: отправлено ({} карточек)", len(stuck))

    except Exception as e:
        logger.error("Ошибка stuck_tasks: {}", e)


# ─────────────────────────────────────────────
# 12:00, 16:00 — Напоминание по карточкам
# ─────────────────────────────────────────────

async def card_nudge() -> None:
    """Выбрать 2-3 карточки из «Сегодня» и спросить статус по каждой с кнопками."""
    logger.info("Запуск card_nudge")
    try:
        chat_id = _target_chat()
        cards = await _trello.get_cards(TrelloList.TODAY)
        if not cards:
            return

        # Выбрать карточки с дедлайном на сегодня или просроченные
        now = datetime.now()
        today_str = now.strftime("%Y-%m-%d")
        urgent = []
        rest = []

        for c in cards:
            due = c.get("due", "")
            if due and due[:10] <= today_str:
                urgent.append(c)
            else:
                rest.append(c)

        # До 3 срочных, потом остальные
        nudge_cards = urgent[:3]
        if len(nudge_cards) < 3:
            nudge_cards += rest[:3 - len(nudge_cards)]

        if not nudge_cards:
            return

        hour = now.hour
        greeting = "Полдня позади." if hour < 14 else "День идёт к концу."

        await _bot.send_message(
            chat_id,
            f"{greeting} Пройдёмся по задачам ({len(cards)} в «Сегодня»):",
        )

        # Отправить каждую карточку с кнопками и контекстом
        for c in nudge_cards:
            emoji = _cover_emoji(c)
            card_id = c["id"]

            # Собрать контекст: дедлайн, дни без активности
            details = []
            due = c.get("due", "")
            if due:
                due_date = due[:10]
                if due_date < today_str:
                    details.append(f"⚠️ просрочено ({due_date})")
                elif due_date == today_str:
                    details.append(f"срок сегодня")
                else:
                    details.append(f"срок {due_date}")

            dla = c.get("dateLastActivity", "")
            if dla:
                try:
                    activity = datetime.fromisoformat(dla.replace("Z", "+00:00")).replace(tzinfo=None)
                    days_inactive = (now - activity).days
                    if days_inactive >= 3:
                        details.append(f"{days_inactive} дн. без движения")
                except (ValueError, TypeError):
                    pass

            detail_str = f" — {', '.join(details)}" if details else ""

            kb = InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="✅ Сделано", callback_data=f"nudge_done_{card_id}"),
                InlineKeyboardButton(text="🔄 В процессе", callback_data=f"nudge_wip_{card_id}"),
                InlineKeyboardButton(text="⏭ Перенести", callback_data=f"nudge_postpone_{card_id}"),
            ]])
            await _bot.send_message(
                chat_id,
                f"{emoji} <b>{c['name']}</b>{detail_str}",
                parse_mode=ParseMode.HTML,
                reply_markup=kb,
            )

        logger.info("card_nudge: отправлено ({} карточек)", len(nudge_cards))

    except Exception as e:
        logger.error("Ошибка card_nudge: {}", e)


# ─────────────────────────────────────────────
# Nudge callback handlers
# ─────────────────────────────────────────────

@nudge_router.callback_query(lambda c: c.data and (
    c.data.startswith("nudge_") or c.data.startswith("stuck_") or c.data.startswith("wait_")
))
async def on_nudge_callback(callback: CallbackQuery) -> None:
    data = callback.data
    await callback.answer()

    # Парсим: {prefix}_{action}_{card_id}
    parts = data.split("_", 2)
    if len(parts) < 3:
        return
    action = parts[1]
    card_id = parts[2]

    try:
        card = await _trello.get_card(card_id)
        card_name = card.get("name", "?")
    except Exception:
        card_name = "?"

    try:
        if action == "done":
            await _trello.archive_card(card_id)
            await callback.message.edit_text(f"✅ <b>{card_name}</b> — архивировал.", parse_mode=ParseMode.HTML)

        elif action == "wip":
            await callback.message.edit_text(f"🔄 <b>{card_name}</b> — ок, работаешь.", parse_mode=ParseMode.HTML)

        elif action == "postpone":
            tomorrow = (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%dT09:00:00")
            await _trello.update_card(card_id, due=tomorrow)
            await callback.message.edit_text(f"⏭ <b>{card_name}</b> — перенёс на завтра.", parse_mode=ParseMode.HTML)

        elif action == "backlog":
            await _trello.move_card(card_id, TrelloList.BACKLOG)
            await callback.message.edit_text(f"📦 <b>{card_name}</b> — в Backlog.", parse_mode=ParseMode.HTML)

        elif action == "archive":
            await _trello.archive_card(card_id)
            await callback.message.edit_text(f"🗑 <b>{card_name}</b> — архивировал.", parse_mode=ParseMode.HTML)

        elif action == "ok":
            await callback.message.edit_text(f"👌 <b>{card_name}</b> — ок, ждём.", parse_mode=ParseMode.HTML)

    except Exception as e:
        logger.error("Callback {} ошибка {}: {}", action, card_id, e)


# ─────────────────────────────────────────────
# 11:00 — Пинг «Мяч на стороне»
# ─────────────────────────────────────────────

async def waiting_ping() -> None:
    """Карточки из «Мяч на стороне» без активности >5 дней — интерактивный пинг."""
    logger.info("Запуск waiting_ping")
    try:
        chat_id = _target_chat()
        cards = await _trello.get_cards(TrelloList.WAITING)
        if not cards:
            return

        stale = _find_stuck_cards(cards, stale_days=5)
        if not stale:
            logger.info("waiting_ping: нет зависших карточек")
            return

        await _bot.send_message(
            chat_id,
            f"🤝 «Мяч на стороне» — {len(stale)} карточек без движения >5 дней:",
        )

        for c in stale[:5]:
            card_id = c["id"]
            days = c["days_inactive"]
            kb = InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="📩 Напомнить", callback_data=f"wait_wip_{card_id}"),
                InlineKeyboardButton(text="👌 Ждём", callback_data=f"wait_ok_{card_id}"),
                InlineKeyboardButton(text="🗑 Архив", callback_data=f"wait_archive_{card_id}"),
            ]])
            await _bot.send_message(
                chat_id,
                f"<b>{c['name']}</b> — {days} дн.",
                parse_mode=ParseMode.HTML,
                reply_markup=kb,
            )

        logger.info("waiting_ping: отправлено ({} карточек)", len(stale))

    except Exception as e:
        logger.error("Ошибка waiting_ping: {}", e)


# ─────────────────────────────────────────────
# Названия месяцев (русские)
# ─────────────────────────────────────────────

_MONTH_NAMES = {
    1: "Январь", 2: "Февраль", 3: "Март", 4: "Апрель",
    5: "Май", 6: "Июнь", 7: "Июль", 8: "Август",
    9: "Сентябрь", 10: "Октябрь", 11: "Ноябрь", 12: "Декабрь",
}

_MONTH_NAMES_LOWER = {k: v.lower() for k, v in _MONTH_NAMES.items()}
_MONTH_NAME_TO_NUM = {v: k for k, v in _MONTH_NAMES_LOWER.items()}


def _get_current_week_range() -> tuple[date_type, date_type]:
    """Получить начало (пн) и конец (вс) текущей недели."""
    today = date_type.today()
    monday = today - timedelta(days=today.weekday())
    sunday = monday + timedelta(days=6)
    return monday, sunday


def _format_week_name(monday: date_type, sunday: date_type) -> str:
    """Сформировать название колонки недели: 'неделя 28– 3 мая'."""
    month_genitive = {
        1: "янв.", 2: "фев.", 3: "мар.", 4: "апр.",
        5: "мая", 6: "июня", 7: "июля", 8: "авг.",
        9: "сен.", 10: "окт.", 11: "нояб.", 12: "дек.",
    }
    if monday.month == sunday.month:
        return f"неделя {monday.day}– {sunday.day} {month_genitive[monday.month]}"
    return f"неделя {monday.day}– {sunday.day} {month_genitive[sunday.month]}"


def _classify_due_date(due_str: str) -> str | None:
    """Определить целевую колонку по дедлайну.

    Возвращает: 'today', 'week', 'month:N' (N=номер месяца), 'next_year', None.
    """
    try:
        due = datetime.fromisoformat(due_str.replace("Z", "+00:00")).date()
    except (ValueError, TypeError):
        return None

    today = date_type.today()
    if due <= today:
        return "today"

    monday, sunday = _get_current_week_range()
    if due <= sunday:
        return "week"

    # В текущем году — колонка месяца
    if due.year == today.year:
        return f"month:{due.month}"

    # Следующий год
    if due.year > today.year:
        return "next_year"

    return None


def _find_month_list_name(list_names: list[str], target_month: int) -> str | None:
    """Найти колонку месяца среди существующих списков."""
    target_lower = _MONTH_NAMES_LOWER.get(target_month, "")
    if not target_lower:
        return None
    for name in list_names:
        if target_lower in name.lower():
            return name
    return None


# ─────────────────────────────────────────────
# Ежедневно 8:30 — Авто-сортировка карточек
# ─────────────────────────────────────────────

async def sort_cards_by_due() -> None:
    """Переместить карточки в правильные колонки по дедлайну.

    - Просроченные / сегодня → Сегодня
    - На этой неделе → Неделя
    - В этом году → колонка месяца (Май, Июнь и т.д.)
    - Следующий год → В следующем году
    """
    logger.info("Запуск sort_cards_by_due")
    try:
        lists = await _trello.get_lists()
        list_names = list(lists.keys())

        # Маппинг list_id → list_name
        id_to_name = {v: k for k, v in lists.items()}

        # Получить все карточки
        all_cards = await _trello.get_all_cards()
        moved = 0

        for card in all_cards:
            due = card.get("due")
            if not due:
                continue

            card_list = id_to_name.get(card.get("idList"), "")

            # Не перемещать из "Готово", "Мяч на стороне", "Изучить"
            skip_lists = ("готово", "мяч на стороне", "изучить")
            if any(s in card_list.lower() for s in skip_lists):
                continue

            target = _classify_due_date(due)
            if not target:
                continue

            # Определить целевую колонку
            target_list_name = None
            if target == "today":
                target_list_name = str(TrelloList.TODAY)
            elif target == "week":
                target_list_name = str(TrelloList.WEEK)
            elif target.startswith("month:"):
                month_num = int(target.split(":")[1])
                target_list_name = _find_month_list_name(list_names, month_num)
            elif target == "next_year":
                # Найти колонку "В следующем году"
                for name in list_names:
                    if "следующ" in name.lower():
                        target_list_name = name
                        break

            if not target_list_name:
                continue

            # Не перемещать если уже в правильной колонке
            target_lower = target_list_name.lower()
            if target_lower in card_list.lower() or card_list.lower() in target_lower:
                continue

            try:
                await _trello.move_card(card["id"], target_list_name)
                moved += 1
                logger.info("Sort: '{}' → '{}'", card["name"], target_list_name)
            except Exception as e:
                logger.error("Sort move error '{}': {}", card["name"], e)

        logger.info("sort_cards_by_due: перемещено {} карточек", moved)

    except Exception as e:
        logger.error("Ошибка sort_cards_by_due: {}", e)


# ─────────────────────────────────────────────
# Понедельник 0:10 — Обновление названий колонок
# ─────────────────────────────────────────────

async def update_list_names() -> None:
    """Обновить названия колонок: неделя (каждый пн), месяц (1-го числа)."""
    logger.info("Запуск update_list_names")
    try:
        lists = await _trello.get_lists()
        list_names = list(lists.keys())
        today = date_type.today()

        # 1. Обновить название колонки "Неделя"
        monday, sunday = _get_current_week_range()
        new_week_name = _format_week_name(monday, sunday)

        # Найти текущую колонку недели
        week_list = None
        for name in list_names:
            if "неделя" in name.lower():
                week_list = name
                break

        if week_list and week_list != new_week_name:
            await _trello.rename_list(week_list, new_week_name)
            logger.info("Колонка недели: '{}' → '{}'", week_list, new_week_name)

        # 2. Обновить колонку месяца (1-го числа)
        if today.day <= 7:  # Первая неделя месяца — проверить
            current_month_name = _MONTH_NAMES[today.month]
            # Проверить, нет ли уже колонки текущего месяца
            has_current = any(current_month_name.lower() in n.lower() for n in list_names)
            if not has_current:
                # Найти колонку прошлого месяца
                prev_month = today.month - 1 if today.month > 1 else 12
                prev_month_list = _find_month_list_name(list_names, prev_month)
                if prev_month_list:
                    await _trello.rename_list(prev_month_list, current_month_name)
                    logger.info("Колонка месяца: '{}' → '{}'", prev_month_list, current_month_name)

        logger.info("update_list_names завершено")

    except Exception as e:
        logger.error("Ошибка update_list_names: {}", e)
