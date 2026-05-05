"""Обработчики расписания — утренняя сводка, полуденный чек-ин, вечерний свод, weekly-отчёт, напоминания, проактивные фичи."""

import asyncio
import calendar as cal_mod
import json
import re
import tarfile
from collections import defaultdict
from datetime import date as date_type, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

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


def _log_scheduled_error(task_name: str, e: Exception) -> None:
    """Логировать ошибку запланированной задачи. Forbidden → INFO, остальное → ERROR.

    Forbidden от Telegram = пользователь не начинал диалог с ботом. Это не баг
    рантайма, и засорение ERROR-логов мешает мониторить реальные проблемы.
    """
    msg = str(e).lower()
    if "forbidden" in msg or "can't initiate conversation" in msg:
        logger.info("{} пропущен (Telegram Forbidden): {}", task_name, e)
    else:
        logger.error("Ошибка {}: {}", task_name, e)


async def _send_dm(target_id: int, text: str, reply_markup=None) -> None:
    """Отправить личное сообщение конкретному пользователю (с markdown→html)."""
    from abay_assistant.bot.handlers import _md_to_html
    from aiogram.enums import ParseMode

    if not target_id:
        return
    html = _md_to_html(text)
    try:
        await _bot.send_message(target_id, html, parse_mode=ParseMode.HTML, reply_markup=reply_markup)
    except Exception as e:
        # Forbidden — пользователь не начал диалог с ботом первым. Это не ошибка
        # рантайма (мы ничего не можем сделать), но засоряет ERROR-логи и crash-метрики.
        msg = str(e).lower()
        if "forbidden" in msg or "can't initiate conversation" in msg:
            logger.info("DM {} пропущен (юзер не писал боту): {}", target_id, e)
        else:
            logger.error("Не удалось отправить DM {}: {}", target_id, e)


async def _send_to_assistant(text: str, reply_markup=None) -> None:
    """Отправить личное сообщение Алану (ассистенту). Если ID не настроен — owner."""
    s = get_settings()
    target = s.telegram_assistant_id or s.telegram_owner_id
    await _send_dm(target, text, reply_markup=reply_markup)


async def _send_dm_both(text: str) -> None:
    """Отправить личные сообщения и Алану, и Абаю (НЕ в группу)."""
    s = get_settings()
    targets = []
    if s.telegram_assistant_id:
        targets.append(s.telegram_assistant_id)
    if s.telegram_owner_id and s.telegram_owner_id not in targets:
        targets.append(s.telegram_owner_id)
    for tid in targets:
        await _send_dm(tid, text)


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
    """Очистить просроченные сессии голосового буфера."""
    try:
        from abay_assistant.bot.evening_voice import cleanup_expired as cleanup_voice_expired
        removed = cleanup_voice_expired()
        if removed:
            logger.info("Очищено {} просроченных сессий", removed)
    except Exception as e:
        logger.error("Ошибка cleanup_sessions: {}", e)


# ─────────────────────────────────────────────
# 8:50 — Автоматическая чистка доски
# ─────────────────────────────────────────────

def _normalize_name(name: str) -> str:
    """Нормализовать название карточки для поиска дублей."""
    # Убрать ответственного после тире
    name = re.split(r"\s*[—–-]\s*(?=[А-ЯA-Z])", name)[0]
    # Привести к нижнему регистру, убрать лишние пробелы
    return re.sub(r"\s+", " ", name.lower().strip())


def _find_duplicates(cards: list[dict]) -> list[tuple[dict, dict]]:
    """Найти дубли: карточки с одинаковым нормализованным названием."""
    by_name: dict[str, list[dict]] = defaultdict(list)
    for c in cards:
        key = _normalize_name(c["name"])
        if len(key) >= 5:  # игнорировать слишком короткие
            by_name[key].append(c)

    duplicates = []
    for key, group in by_name.items():
        if len(group) < 2:
            continue
        # Оставить карточку с desc или комментариями, архивировать остальные
        group.sort(key=lambda c: len(c.get("desc", "")), reverse=True)
        keeper = group[0]
        for dupe in group[1:]:
            duplicates.append((keeper, dupe))

    return duplicates


def _guess_cover_color(card: dict) -> str | None:
    """Определить цвет обложки по контексту карточки."""
    name = card.get("name", "").lower()
    labels = [lb.get("name", "").lower() for lb in card.get("labels", [])]
    due = card.get("due", "")

    # Красный: срочно, денежная, контракт, оплата
    if "срочно" in " ".join(labels):
        return "red"
    if any(w in name for w in ("договор", "контракт", "оплат", "счёт", "💰")):
        return "red"
    if "денежная" in " ".join(labels):
        return "red"

    # Просроченный дедлайн → красный
    if due:
        try:
            due_date = datetime.fromisoformat(due.replace("Z", "+00:00")).date()
            if due_date <= date_type.today():
                return "red"
        except (ValueError, TypeError):
            pass

    # Зелёный: цикличная
    if any(w in name for w in ("еженедел", "ежедневн", "каждый", "каждую", "регулярн")):
        return "green"

    # Синий: изучить, стратегия
    if any(w in name for w in ("изучить", "исследов", "стратег", "разобрать")):
        return "blue"

    # По умолчанию: оранжевый (важно, но не срочно)
    return "orange"


async def board_cleanup() -> None:
    """Автоматическая чистка доски: дубли + обложки."""
    logger.info("Запуск board_cleanup")
    try:
        all_cards = await _trello.get_all_cards()
        actions = []

        # 1. Убрать дубли
        duplicates = _find_duplicates(all_cards)
        for keeper, dupe in duplicates:
            try:
                # Перенести инфо из дубля в основную
                dupe_desc = dupe.get("desc", "")
                if dupe_desc and not keeper.get("desc"):
                    await _trello.update_card(keeper["id"], desc=dupe_desc)
                await _trello.add_comment(
                    keeper["id"],
                    f"🔄 Дубль «{dupe['name']}» объединён и перемещён в архив.",
                )
                try:
                    await _trello.move_card(dupe["id"], TrelloList.ARCHIVE)
                except ValueError:
                    await _trello.archive_card(dupe["id"])
                actions.append(f"Дубль: «{dupe['name']}» → архив (оставил «{keeper['name']}»)")
                logger.info("Дубль в архив: '{}' (оставил '{}')", dupe["name"], keeper["name"])
            except Exception as e:
                logger.error("Ошибка при чистке дубля '{}': {}", dupe["name"], e)

        # 2. Назначить обложки карточкам без цвета
        for card in all_cards:
            cover = (card.get("cover") or {}).get("color")
            if cover:
                continue
            # Пропустить готовые и архивные
            if card.get("closed"):
                continue

            color = _guess_cover_color(card)
            if color:
                try:
                    await _trello.set_cover(card["id"], color)
                    actions.append(f"Обложка: «{card['name']}» → {_COVER_EMOJI.get(color, color)}")
                    logger.info("Обложка назначена: '{}' → {}", card["name"], color)
                except Exception as e:
                    logger.error("Ошибка назначения обложки '{}': {}", card["name"], e)

        if actions:
            # В личку Алану — техническая чистка, не для группы
            summary = "🧹 авто-чистка доски:\n" + "\n".join(f"• {a}" for a in actions)
            await _send_to_assistant(summary)
            logger.info("board_cleanup: {} действий, отправлено ассистенту", len(actions))
        else:
            logger.info("board_cleanup: доска чистая")

    except Exception as e:
        logger.error("Ошибка board_cleanup: {}", e)


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

        # В личку Алану — это его рабочий инструмент. Алан сам решит что важного сказать в группу.
        await _send_to_assistant(text)
        logger.info("Утренняя сводка отправлена ассистенту в личку")

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
# 22:30 — Вечерний свод в группе (для Абая)
# ─────────────────────────────────────────────

async def evening_review() -> None:
    """Вечерний свод: список задач + расписание + вопрос Абаю в группу.

    1. Сначала перетасует карточки по дедлайнам (на случай если кто-то с дедом сегодня
       завис в Неделе/месяце).
    2. Соберёт «Сегодня» + сегодняшние события календаря.
    3. Отправит в группу с упоминанием Абая (на «Вы»).
    4. Запустит сессию сбора голосовых: через 5 мин после последнего голоса от Абая —
       эхо-подтверждение, после «Да» — LLM создаст/перенесёт карточки.
    """
    logger.info("Запуск вечернего свода (группа)")

    from abay_assistant.bot.evening_voice import start_session as start_voice_session

    try:
        # 1. Освежить «Сегодня» — подтянуть карточки из «Недели»/месяцев с due на сегодня
        await sort_cards_by_due()

        # 2. Собрать данные
        raw_cards = await _trello.get_cards(TrelloList.TODAY)
        today_cards = [{"id": c["id"], "name": c["name"]} for c in raw_cards]
        today_events = await _get_today_events()

        # 3. Сформировать сообщение
        s = get_settings()
        mention = _mention_assistant()
        text = _format_evening_group_message(raw_cards, today_events, mention)

        chat_id = s.telegram_group_id or s.telegram_owner_id
        await _bot.send_message(chat_id, text, parse_mode=ParseMode.HTML)

        # 4. Запустить сессию сбора голосовых — реагирует только на Абая
        if s.telegram_assistant_id:
            start_voice_session(s.telegram_assistant_id, today_cards, today_events)

        logger.info("Вечерний свод (группа) отправлен, сессия голосовых открыта")

    except Exception as e:
        logger.error("Ошибка вечернего свода: {}", e)


def _mention_assistant() -> str:
    """Упоминание Абая через @username или tg://user link."""
    s = get_settings()
    if s.telegram_assistant_username:
        return f"@{s.telegram_assistant_username}"
    if s.telegram_assistant_id:
        return f'<a href="tg://user?id={s.telegram_assistant_id}">Абай</a>'
    return "Абай"


def _format_evening_group_message(cards: list[dict], events: list[dict], mention: str) -> str:
    """Собрать вечернее сообщение для группы — только просьба к Абаю.

    Список «Сегодня» и расписание уже шли в свод 21:00 (DM). Здесь — чистый сбор апдейтов.
    """
    today = datetime.now().strftime("%d.%m.%Y, %A")
    return (
        f"<b>Вечерний свод — {today}</b>\n\n"
        f"{mention}, что Вы сделали сегодня и какие новые задачи появились?\n"
        f"Также откройте, пожалуйста, WhatsApp и посмотрите, с кем была переписка сегодня — "
        f"что из этого нужно записать в задачи?\n\n"
        f"Можно отвечать голосом, в несколько сообщений. "
        f"Через 5 минут после последнего голосового я соберу всё вместе и переспрошу, правильно ли понял."
    )


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
    """Проверить календарь: если через ~30 минут встреча — отправить напоминание + CRM-контекст."""
    try:
        chat_id = _target_chat()
        tz = ZoneInfo(get_settings().timezone)
        now = datetime.now(tz)

        events = await _calendar.get_events(date=now)
        if not events:
            return

        people = await _obsidian.list_entities("person")

        for event in events:
            start_str = event.get("start", "")
            summary = event.get("summary", "")

            try:
                event_start = datetime.fromisoformat(start_str)
                # Привести к таймзоне Алматы если нет tzinfo
                if event_start.tzinfo is None:
                    event_start = event_start.replace(tzinfo=tz)
                else:
                    event_start = event_start.astimezone(tz)
            except (ValueError, TypeError):
                continue

            # Окно: через 25-35 минут
            if not (now + timedelta(minutes=25) <= event_start <= now + timedelta(minutes=35)):
                continue

            dedup_key = f"{start_str}|{summary}"
            if dedup_key in _notified_meetings:
                continue

            time_str = event_start.strftime("%H:%M")
            matched_name = _match_event_to_crm(summary, people)

            # КОРОТКОЕ сообщение в группу — все участники должны знать
            short_msg = f"⏰ через 30 мин ({time_str}): {summary}"
            await _bot.send_message(chat_id, short_msg)

            # ДЕТАЛЬНЫЙ контекст — в личку Алану (он подготовится)
            if matched_name:
                detail = f"⏰ через 30 мин: {summary}\n\nконтекст по {matched_name}:\n"
                crm_text = await _obsidian.get_entity_summary("person", matched_name)
                detail += crm_text

                meta = await _obsidian.get_entity_meta("person", matched_name)
                projects = meta.get("projects", [])
                if projects:
                    detail += f"\n\nпроекты: {', '.join(str(p) for p in projects)}"
                    try:
                        proj_meta = await _obsidian.get_entity_meta("project", projects[0])
                        decisions = proj_meta.get("decision_log", [])
                        if decisions:
                            detail += f"\nпоследнее решение по «{projects[0]}»: {decisions[-1]}"
                    except Exception:
                        pass

                # Связанные Trello-карточки
                try:
                    all_cards = await _trello.get_all_cards()
                    lists = await _trello.get_lists()
                    list_names = {v: k for k, v in lists.items()}
                    related = []
                    for c in all_cards:
                        cname = c.get("name", "").lower()
                        if matched_name.lower() in cname:
                            ln = list_names.get(c.get("idList"), "")
                            if ln.lower() not in ("готово", "архив"):
                                related.append((c["name"], ln))
                    if related:
                        detail += "\n\nоткрытые задачи:"
                        for name, lst in related[:3]:
                            detail += f"\n  — {name[:50]} ({lst})"
                except Exception as e:
                    logger.debug("Ошибка поиска карточек: {}", e)

                await _send_to_assistant(detail)

            _notified_meetings.add(dedup_key)
            logger.info("Meeting reminder: {} (CRM: {})", summary, matched_name or "—")

        # Очистить старые ключи
        today_str = now.strftime("%Y-%m-%d")
        stale_keys = {k for k in _notified_meetings if not k.startswith(today_str)}
        _notified_meetings.difference_update(stale_keys)

    except Exception as e:
        _log_scheduled_error("meeting_prep", e)


# ─────────────────────────────────────────────
# Понедельник 0:15 — Перенос карточек с прошлой недели
# ─────────────────────────────────────────────

async def weekly_carryover() -> None:
    """Понедельник после обновления колонок: пометить перенесённые карточки."""
    logger.info("Запуск weekly_carryover")
    try:
        cards = await _trello.get_cards(TrelloList.WEEK)
        if not cards:
            logger.info("weekly_carryover: колонка «Неделя» пуста")
            return

        today_str = datetime.now().strftime("%d.%m.%Y")
        commented = []

        for card in cards:
            try:
                await _trello.add_comment(
                    card["id"],
                    f"📅 Перенесено с прошлой недели ({today_str})",
                )
                commented.append(card["name"])
            except Exception as e:
                logger.error("Ошибка carryover '{}': {}", card["name"], e)

        if commented:
            lines = [f"📅 перенесено с прошлой недели ({len(commented)}):\n"]
            for name in commented:
                lines.append(f"— {name}")
            # В личку Алану — операционная инфа на старт недели
            await _send_to_assistant("\n".join(lines))

        logger.info("weekly_carryover: {} карточек помечено", len(commented))

    except Exception as e:
        logger.error("Ошибка weekly_carryover: {}", e)


# ─────────────────────────────────────────────
# Воскресенье 20:00 — Забытые контакты
# ─────────────────────────────────────────────

async def forgotten_contacts() -> None:
    """Еженедельный скан: контакты без обновления >21 дня."""
    logger.info("Запуск forgotten_contacts")
    try:
        # В личку Алану — он восстанавливает контакты
        s = get_settings()
        chat_id = s.telegram_assistant_id or s.telegram_owner_id
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
# Каждый день 21:00 — Сводка дня для ассистента
# ─────────────────────────────────────────────

async def daily_summary_for_assistant() -> None:
    """Свод дня: конкретные факты — что закрыто, что осталось, что было в календаре."""
    logger.info("Запуск daily_summary_for_assistant")
    try:
        s = get_settings()
        tz = ZoneInfo(s.timezone)
        now_local = datetime.now(tz)
        today_str = now_local.strftime("%Y-%m-%d")
        day_start_utc = now_local.replace(
            hour=0, minute=0, second=0, microsecond=0
        ).astimezone(ZoneInfo("UTC"))

        def _touched_today(card: dict) -> bool:
            ts = card.get("dateLastActivity")
            if not ts:
                return False
            try:
                dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
            except Exception:
                return False
            return dt >= day_start_utc

        # Сбор сырых данных по спискам
        async def _list_or_empty(lst: TrelloList) -> list[dict]:
            try:
                return await _trello.get_cards(lst)
            except Exception as e:
                logger.error("Trello get_cards('{}'): {}", lst, e)
                return []

        today_raw, done_raw, archive_raw = await asyncio.gather(
            _list_or_empty(TrelloList.TODAY),
            _list_or_empty(TrelloList.DONE),
            _list_or_empty(TrelloList.ARCHIVE),
        )

        today_cards = [f"{_cover_emoji(c)} {c['name']}" for c in today_raw]
        closed_today = [c["name"] for c in done_raw if _touched_today(c)]
        archived_today = [c["name"] for c in archive_raw if _touched_today(c)]
        moved_today = [c["name"] for c in today_raw if _touched_today(c)]

        events_lines = []
        for e in await _get_today_events():
            t = (e.get("start", "") or "")[11:16]
            summary_e = e.get("summary", "(без названия)")
            events_lines.append(f"{t} — {summary_e}" if t else summary_e)

        daily_note = ""
        try:
            daily_note = await _obsidian.read_note(f"Daily/{today_str}.md")
        except Exception:
            pass

        # Действия бота за день (из ToolUsage.args_summary) — группируем по типу
        from abay_assistant.db import get_tool_actions
        actions = get_tool_actions(days=1)
        actions_by_tool: dict[str, list[str]] = defaultdict(list)
        for a in actions:
            actions_by_tool[a["tool_name"]].append(a["args"])
        actions_lines = []
        for tname, items in sorted(actions_by_tool.items()):
            sample = "; ".join(items[:5])
            extra = f" (+ ещё {len(items) - 5})" if len(items) > 5 else ""
            actions_lines.append(f"{tname} ×{len(items)}: {sample}{extra}")

        def _bullet(items: list[str]) -> str:
            return "\n".join(f"- {x}" for x in items) if items else "(пусто)"

        context = (
            f"Дата: {today_str}\n\n"
            f"Закрыто за день ({len(closed_today)}):\n{_bullet(closed_today)}\n\n"
            f"Отправлено в архив ({len(archived_today)}):\n{_bullet(archived_today)}\n\n"
            f"Менялось в «Сегодня» за день ({len(moved_today)}):\n{_bullet(moved_today)}\n\n"
            f"Осталось в «Сегодня» к концу дня ({len(today_cards)}):\n{_bullet(today_cards)}\n\n"
            f"События календаря ({len(events_lines)}):\n{_bullet(events_lines)}\n\n"
            f"Действия бота за день ({len(actions)}):\n{_bullet(actions_lines)}\n\n"
            f"Дневная заметка:\n{daily_note or '(пусто)'}\n"
        )

        system = (
            "Ты пишешь свод дня для Абая. Жёсткие правила:\n"
            "- ТОЛЬКО конкретные факты из данных ниже: имена карточек, время событий, цитаты из заметки.\n"
            "- НИКАКИХ общих фраз: «продуктивный день», «активная работа», «много задач», "
            "«в фокусе текущие дела», «детальная работа с приоритетами» — это бан.\n"
            "- НИКАКИХ выдуманных метрик («N обращений говорят о...»). Если факта нет — не упоминай.\n"
            "- АБСОЛЮТНЫЙ ЗАПРЕТ на markdown-заголовки `##`, `###`. Только обычный текст и **жирный** через `**`.\n"
            "- Структура: 4 коротких блока — **Закрыли:**, **Осталось:**, **События/заметка:**, **Бот сделал:** "
            "(коротко обобщи действия бота — сколько карточек создано/перемещено/обновлено). "
            "По 1-3 строки в блоке. Если по блоку пусто — пропусти его, не пиши «нет данных».\n"
            "- Стиль разговорный, с маленькой буквы. Без пафоса.\n"
            "- Если день пустой (ничего не закрыто, ничего не менялось, событий нет) — "
            "так и напиши одной строкой, без украшательств.\n"
        )

        summary = await _llm.chat(
            messages=[{"role": "user", "content": context}],
            system=system,
            max_tokens=1024,
        )

        # Страховка: вырезать любые случайные markdown-заголовки из вывода LLM
        cleaned = re.sub(r"^#{1,6}\s*", "", summary, flags=re.MULTILINE).strip()

        header = f"📋 **Свод дня — {today_str}**\n\n"
        await _send_dm_both(header + cleaned)

        logger.info("daily_summary отправлен в личку")

    except Exception as e:
        logger.error("Ошибка daily_summary_for_assistant: {}", e)


# ─────────────────────────────────────────────
# Каждый день 20:00 — Застрявшие задачи
# ─────────────────────────────────────────────

async def stuck_tasks() -> None:
    """Ежедневный скан: карточки без активности >3 дней — интерактивно."""
    logger.info("Запуск stuck_tasks")
    try:
        # В личку Алану — это его рабочий инструмент
        s = get_settings()
        chat_id = s.telegram_assistant_id or s.telegram_owner_id

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
        _log_scheduled_error("stuck_tasks", e)


# ─────────────────────────────────────────────
# 12:00, 16:00 — Напоминание по карточкам
# ─────────────────────────────────────────────

async def card_nudge() -> None:
    """Выбрать 2-3 карточки из «Сегодня» и спросить статус по каждой с кнопками."""
    logger.info("Запуск card_nudge")
    try:
        # В личку Алану — он отвечает за статусы
        s = get_settings()
        chat_id = s.telegram_assistant_id or s.telegram_owner_id
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
        _log_scheduled_error("card_nudge", e)


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
        # В личку Алану — он пинает по контактам
        s = get_settings()
        chat_id = s.telegram_assistant_id or s.telegram_owner_id
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


# ─────────────────────────────────────────────
# Каждый день 4:00 — Snapshot CRM (Obsidian vault)
# ─────────────────────────────────────────────

async def crm_snapshot() -> None:
    """Создать tar.gz снапшот vault. Хранит 30 дней. Можно откатиться вручную."""
    logger.info("Запуск crm_snapshot")
    try:
        s = get_settings()
        vault = Path(s.vault_path)
        if not vault.exists():
            logger.warning("crm_snapshot: vault не найден: {}", vault)
            return

        snapshots_dir = vault.parent / "vault_snapshots"
        snapshots_dir.mkdir(exist_ok=True)

        today_str = datetime.now().strftime("%Y-%m-%d")
        archive_path = snapshots_dir / f"vault_{today_str}.tar.gz"

        # Создать архив
        with tarfile.open(archive_path, "w:gz") as tar:
            tar.add(vault, arcname=vault.name)

        size_kb = archive_path.stat().st_size // 1024
        logger.info("CRM snapshot: {} ({} KB)", archive_path.name, size_kb)

        # Удалить старые (>30 дней)
        cutoff = datetime.now() - timedelta(days=30)
        deleted = 0
        for f in snapshots_dir.glob("vault_*.tar.gz"):
            try:
                mtime = datetime.fromtimestamp(f.stat().st_mtime)
                if mtime < cutoff:
                    f.unlink()
                    deleted += 1
            except Exception as e:
                logger.error("Ошибка удаления старого snapshot {}: {}", f.name, e)
        if deleted:
            logger.info("CRM snapshot: удалено {} старых архивов", deleted)

    except Exception as e:
        logger.error("Ошибка crm_snapshot: {}", e)


# ─────────────────────────────────────────────
# Каждый день 9:30 — Pattern detection
# ─────────────────────────────────────────────

async def pattern_check() -> None:
    """Найти паттерны на доске и в CRM, сообщить о важных."""
    logger.info("Запуск pattern_check")
    try:
        cards = await _trello.get_all_cards()
        lists = await _trello.get_lists()
        list_names = {v: k for k, v in lists.items()}

        # Паттерн 1: один человек упоминается в 3+ активных карточках с дедлайном или мяч на стороне
        person_cards: dict[str, list[dict]] = defaultdict(list)
        active_lists = ["сегодня", "неделя", "Мяч на стороне"]
        for card in cards:
            list_name = list_names.get(card.get("idList"), "").lower()
            if not any(al.lower() in list_name for al in active_lists):
                continue
            # Извлечь имена через тире (например, "Звонок — Расул")
            name = card.get("name", "")
            m = re.search(r"—\s*(\w[\wа-яА-ЯёЁ]+)\s*$", name)
            if m:
                person_cards[m.group(1)].append(card)

        people_alerts = []
        for person, plist in person_cards.items():
            if len(plist) >= 3:
                people_alerts.append((person, plist))

        # Паттерн 2: карточки в "Мяч на стороне" >7 дней без активности
        from datetime import timezone
        now = datetime.now(timezone.utc)
        stale = []
        for card in cards:
            list_name = list_names.get(card.get("idList"), "").lower()
            if "мяч" not in list_name:
                continue
            last_activity = card.get("dateLastActivity", "")
            if last_activity:
                try:
                    last = datetime.fromisoformat(last_activity.replace("Z", "+00:00"))
                    days = (now - last).days
                    if days >= 7:
                        stale.append((card, days))
                except (ValueError, TypeError):
                    pass

        # Паттерн 3: просрочки > 3 дней
        overdue = []
        for card in cards:
            due = card.get("due")
            if not due:
                continue
            try:
                due_dt = datetime.fromisoformat(due.replace("Z", "+00:00"))
                days_late = (now - due_dt).days
                if days_late >= 3:
                    overdue.append((card, days_late))
            except (ValueError, TypeError):
                pass

        # Если ничего не нашли — молчим
        if not (people_alerts or stale or overdue):
            logger.info("pattern_check: ничего интересного")
            return

        lines = []
        if people_alerts:
            lines.append("📌 паттерны по людям:")
            for person, plist in people_alerts[:3]:
                titles = ", ".join(f'«{c["name"][:40]}»' for c in plist[:3])
                lines.append(f"  — {person}: {len(plist)} активных задач ({titles})")

        if stale:
            stale.sort(key=lambda x: x[1], reverse=True)
            lines.append("\n⏳ висят на стороне >7 дней:")
            for card, days in stale[:5]:
                lines.append(f"  — {card['name'][:50]} ({days} дн.)")

        if overdue:
            overdue.sort(key=lambda x: x[1], reverse=True)
            lines.append("\n🔴 просрочены >3 дней:")
            for card, days in overdue[:5]:
                lines.append(f"  — {card['name'][:50]} ({days} дн.)")

        # В личку Алану — он триажит и решает что нести в группу
        await _send_to_assistant("\n".join(lines))
        logger.info("pattern_check: отправлено в личку ассистенту, {} паттернов",
                     len(people_alerts) + len(stale) + len(overdue))

    except Exception as e:
        logger.error("Ошибка pattern_check: {}", e)
