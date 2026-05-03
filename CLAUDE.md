# CLAUDE.md — Контекст для Claude Code

## Проект
ИИ-ассистент Абая — Telegram-бот, который помогает управлять задачами, заметками и расписанием.

## Пользователи
- **Алан** (owner) — владелец, основной пользователь
- **Абай** (assistant) — ассистент, второй пользователь

## Архитектура
- **Telegram бот** (aiogram 3) — интерфейс общения
- **FastAPI** — webhook сервер (пока polling для dev)
- **SQLite + SQLModel** — хранение сообщений и состояния
- **Anthropic Claude** — LLM для обработки сообщений
- **Trello** — управление задачами
- **Google Calendar** — расписание
- **Obsidian vault** — база знаний (markdown файлы)

## Ключевые файлы
- `src/abay_assistant/main.py` — точка входа, расписание, BotCommand-меню
- `src/abay_assistant/config.py` — конфигурация из .env
- `src/abay_assistant/db.py` — модели БД (Message, Reminder, ToolUsage с `args_summary`, DailyStat, Idea, WeeklyCache, User)
- `src/abay_assistant/bot/handlers.py` — Telegram хендлеры, forward-flow с inline-кнопками, `/pending`
- `src/abay_assistant/bot/scheduled.py` — cron-задачи (утро, свод 21:00, вечерний 22:30)
- `src/abay_assistant/bot/evening.py` — старый текстовый вечерний flow
- `src/abay_assistant/bot/evening_voice.py` — голосовой evening (буфер + эхо + LLM-разбор)
- `src/abay_assistant/prompts/inbox.md` — основной системный промпт
- `src/abay_assistant/prompts/morning.md` — промпт утренней сводки
- `src/abay_assistant/prompts/forward.md` — промпт для разбора пересланных сообщений (text-only, JSON-план)
- `src/abay_assistant/tools/definitions.py` — все tools для Claude (включая `save_personal_pattern`, `get_personal_patterns`)
- `src/abay_assistant/tools/executor.py` — диспетчер tool calls + `summarize_tool_args` (для `ToolUsage.args_summary`)
- `src/abay_assistant/services/obsidian.py` — vault: People, Projects, Daily, Patterns

## Vault (Obsidian)
- `People/{Имя}.md` — карточки людей (CRM, frontmatter + ### YYYY-MM-DD блоки)
- `Projects/{Название}.md` — карточки проектов
- `Daily/YYYY-MM-DD.md` — дневные заметки
- `Patterns/Алан.md`, `Patterns/Абай.md` — личные привычки/предпочтения, подгружаются в системный промпт (хвост 30 строк)

## Ключевые flow
- **Inbox**: текст/голос → LLM с tools → действия в Trello/CRM/Calendar
- **Forward**: пересланное сообщение → отдельный промпт `forward.md` → JSON-план → inline-кнопки «Сделать / Поправить / Не нужно»
- **Свод дня (21:00)**: факты из Trello (закрыто/архив/менялось/осталось) + календарь + действия бота из `ToolUsage.args_summary` + дневная заметка → LLM с жёстким промптом «только факты»
- **Вечерний свод (22:30)**: только просьба к Абаю + voice-сессия 5 мин → LLM-разбор

## Команды
- `uv run python -m abay_assistant.main` — запуск бота
- `uv run pytest` — запуск тестов

## Язык
- Код и комментарии — на русском и английском
- Бот общается на русском языке

## gstack
Use the /browse skill from gstack for all web browsing. Available skills: /office-hours, /plan-ceo-review, /plan-eng-review, /plan-design-review, /design-consultation, /design-shotgun, /design-html, /review, /ship, /land-and-deploy, /canary, /benchmark, /browse, /qa, /qa-only, /design-review, /setup-browser-cookies, /setup-deploy, /setup-gbrain, /retro, /investigate, /document-release, /cso, /autoplan, /plan-devex-review, /devex-review, /careful, /freeze, /guard, /unfreeze, /gstack-upgrade, /learn.
