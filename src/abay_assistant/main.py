import asyncio
import signal
import sys
import traceback

from aiogram import Bot, Dispatcher
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from loguru import logger

from abay_assistant.config import get_settings
from abay_assistant.db import init_db
from abay_assistant.bot.handlers import router, setup as setup_handlers
from abay_assistant.bot.evening import setup as setup_evening, evening_router
from abay_assistant.bot.crm_browser import crm_router, setup_crm
from abay_assistant.bot.scheduled import (
    setup as setup_scheduled,
    morning_briefing,
    noon_checkin,
    evening_review,
    weekly_report,
    check_reminders,
    meeting_prep,
    forgotten_contacts,
    stuck_tasks,
    cleanup_sessions,
    sort_cards_by_due,
    update_list_names,
    card_nudge,
)
from abay_assistant.services.llm import LLMClient
from abay_assistant.services.trello import TrelloClient
from abay_assistant.services.obsidian import ObsidianClient
from abay_assistant.services.whisper import WhisperClient
from abay_assistant.services.calendar import CalendarClient


def setup_logging() -> None:
    logger.remove()
    logger.add(sys.stderr, level="INFO", format="{time:HH:mm:ss} | {level:<7} | {message}")
    logger.add(
        "logs/abay_{time:YYYY-MM-DD}.log",
        level="DEBUG",
        rotation="10 MB",
        retention="30 days",
        compression="gz",
        format="{time:YYYY-MM-DD HH:mm:ss} | {level:<7} | {name}:{line} | {message}",
    )


def setup_scheduler(scheduler: AsyncIOScheduler) -> None:
    """Настроить cron-задачи. Часовой пояс — Алматы (GMT+5)."""
    # 9:00 — утренняя сводка
    scheduler.add_job(morning_briefing, "cron", hour=9, minute=0)
    # 14:00 — полуденный чек-ин
    scheduler.add_job(noon_checkin, "cron", hour=14, minute=0)
    # 22:30 — вечерний свод
    scheduler.add_job(evening_review, "cron", hour=22, minute=30)
    # Воскресенье 21:00 — еженедельный отчёт
    scheduler.add_job(weekly_report, "cron", day_of_week="sun", hour=21, minute=0)
    # Каждые 60 секунд — проверка напоминаний
    scheduler.add_job(check_reminders, "interval", seconds=60)
    # Каждые 10 минут — очистка зависших вечерних сессий
    scheduler.add_job(cleanup_sessions, "interval", minutes=10)
    # Каждые 5 минут — подготовка к встречам (CRM-контекст)
    scheduler.add_job(meeting_prep, "interval", minutes=5)
    # Воскресенье 20:00 — забытые контакты
    scheduler.add_job(forgotten_contacts, "cron", day_of_week="sun", hour=20, minute=0)
    # Каждый день 20:00 — застрявшие задачи
    scheduler.add_job(stuck_tasks, "cron", hour=20, minute=0)
    # Каждый день 8:30 — авто-сортировка карточек по дедлайну
    scheduler.add_job(sort_cards_by_due, "cron", hour=8, minute=30)
    # Понедельник 0:10 — обновить названия колонок (неделя + месяц)
    scheduler.add_job(update_list_names, "cron", day_of_week="mon", hour=0, minute=10)
    # 12:00 и 16:00 — напоминание по карточкам
    scheduler.add_job(card_nudge, "cron", hour=12, minute=0)
    scheduler.add_job(card_nudge, "cron", hour=16, minute=0)
    logger.info(
        "Расписание: 8:30 sort, 9:00 утро, 12/16 nudge, 14:00 чек-ин, 20:00 stuck, "
        "22:30 вечер, пн 0:10 list-names, вс 20:00 контакты, вс 21:00 weekly, "
        "meeting_prep/5мин, напоминания/60с (Asia/Almaty)"
    )


async def main() -> None:
    setup_logging()
    logger.info("Инициализация базы данных...")
    init_db()

    # Инициализация сервисов
    s = get_settings()
    llm = LLMClient()
    trello = TrelloClient()
    obsidian = ObsidianClient(vault_path=s.vault_path)
    whisper = WhisperClient()
    calendar = CalendarClient()

    bot = Bot(token=s.telegram_bot_token)

    setup_handlers(llm=llm, trello=trello, obsidian=obsidian, whisper=whisper, calendar=calendar)
    setup_scheduled(bot=bot, llm=llm, trello=trello, calendar=calendar, obsidian=obsidian)
    setup_evening(bot=bot, trello=trello, obsidian=obsidian)
    setup_crm(bot=bot, obsidian=obsidian)
    logger.info("Сервисы инициализированы (LLM, Trello, Obsidian, Whisper, Calendar, CRM)")

    # Расписание
    scheduler = AsyncIOScheduler(timezone=s.timezone)
    setup_scheduler(scheduler)
    scheduler.start()

    dp = Dispatcher()
    dp.include_router(evening_router)
    dp.include_router(crm_router)
    dp.include_router(router)

    # Graceful shutdown
    loop = asyncio.get_event_loop()

    def _shutdown_signal() -> None:
        logger.info("Получен сигнал завершения, останавливаю...")
        scheduler.shutdown(wait=False)
        # Отменяем polling — dp.start_polling() выбросит CancelledError
        for task in asyncio.all_tasks(loop):
            task.cancel()

    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, _shutdown_signal)

    # Ping owner при старте
    try:
        await bot.send_message(s.telegram_owner_id, "Бот запущен.")
    except Exception:
        pass

    logger.info("Бот запускается (polling)...")
    try:
        await dp.start_polling(bot)
    except asyncio.CancelledError:
        pass
    finally:
        scheduler.shutdown(wait=False)
        try:
            await bot.send_message(s.telegram_owner_id, "Бот остановлен.")
        except Exception:
            pass
        await bot.session.close()
        logger.info("Бот остановлен.")


async def _run_with_crash_ping() -> None:
    """Обёртка: при крэше отправить ping в Telegram."""
    try:
        await main()
    except Exception:
        error = traceback.format_exc()
        logger.critical("Бот упал:\n{}", error)
        try:
            s = get_settings()
            bot = Bot(token=s.telegram_bot_token)
            short = error[-500:] if len(error) > 500 else error
            await bot.send_message(s.telegram_owner_id, f"Бот упал:\n\n{short}")
            await bot.session.close()
        except Exception:
            pass
        raise


if __name__ == "__main__":
    asyncio.run(_run_with_crash_ping())
