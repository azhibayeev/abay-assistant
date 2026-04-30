"""Обработчики расписания — утренняя сводка, полуденный чек-ин, вечерний свод, weekly-отчёт, напоминания, проактивные фичи."""

import asyncio
import json
from collections import defaultdict
from datetime import date as date_type, datetime, timedelta
from pathlib import Path

from aiogram import Bot
from aiogram.enums import ParseMode
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


def _recipient_ids() -> list[int]:
    """Telegram ID пользователей для рассылки."""
    s = get_settings()
    ids = [s.telegram_owner_id]
    if s.telegram_assistant_id:
        ids.append(s.telegram_assistant_id)
    return ids


async def _send_to_all(text: str) -> None:
    """Отправить сообщение всем пользователям с retry при 429."""
    from abay_assistant.bot.handlers import _md_to_html
    from aiogram.enums import ParseMode

    html = _md_to_html(text)
    for tid in _recipient_ids():
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


async def _get_today_cards() -> list[dict]:
    """Карточки из списка 'Сегодня'."""
    try:
        cards = await _trello.get_cards(TrelloList.TODAY)
        return [
            {"name": c["name"], "labels": [lb["name"] for lb in c.get("labels", [])]}
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
            names = "\n".join(f"  — {c['name']}" for c in today_cards[:7])
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

        s = get_settings()
        try:
            await _bot.send_message(s.telegram_owner_id, text, parse_mode=ParseMode.HTML)
        except Exception:
            await _bot.send_message(s.telegram_owner_id, text)
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
                    "name": c["name"],
                    "days_inactive": days_inactive,
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
        s = get_settings()
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

            await _bot.send_message(s.telegram_owner_id, message)
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
        s = get_settings()
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

        await _bot.send_message(s.telegram_owner_id, "\n".join(lines))
        logger.info("forgotten_contacts: {} контактов", len(stale))

    except Exception as e:
        logger.error("Ошибка forgotten_contacts: {}", e)


# ─────────────────────────────────────────────
# Каждый день 20:00 — Застрявшие задачи
# ─────────────────────────────────────────────

async def stuck_tasks() -> None:
    """Ежедневный скан: карточки без активности >3 дней + просроченные."""
    logger.info("Запуск stuck_tasks")
    try:
        s = get_settings()
        parts = []

        # Застряли в «Сегодня»
        today_raw = await _trello.get_cards(TrelloList.TODAY)
        stuck = _find_stuck_cards(today_raw, stale_days=3)
        if stuck:
            lines = ["Застряли в «Сегодня» (без активности >3 дней):"]
            for c in stuck[:7]:
                lines.append(f"  — {c['name']} ({c['days_inactive']} дн.)")
            lines.append("Перенести в Backlog, завершить или удалить?")
            parts.append("\n".join(lines))

        # Просроченные
        overdue = await _get_overdue_cards()
        if overdue:
            lines = ["Просроченные задачи:"]
            for c in overdue[:7]:
                lines.append(f"  — {c['name']} (срок: {c['due']}, список: {c['list']})")
            parts.append("\n".join(lines))

        if not parts:
            logger.info("stuck_tasks: нет проблемных задач")
            return

        await _bot.send_message(s.telegram_owner_id, "\n\n".join(parts))
        logger.info("stuck_tasks: уведомление отправлено")

    except Exception as e:
        logger.error("Ошибка stuck_tasks: {}", e)
