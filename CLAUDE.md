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
- `src/abay_assistant/main.py` — точка входа
- `src/abay_assistant/config.py` — конфигурация из .env
- `src/abay_assistant/db.py` — модели БД
- `src/abay_assistant/bot/handlers.py` — Telegram хендлеры
- `src/abay_assistant/services/` — клиенты внешних сервисов

## Команды
- `uv run python -m abay_assistant.main` — запуск бота
- `uv run pytest` — запуск тестов

## Язык
- Код и комментарии — на русском и английском
- Бот общается на русском языке

## gstack
Use the /browse skill from gstack for all web browsing. Available skills: /office-hours, /plan-ceo-review, /plan-eng-review, /plan-design-review, /design-consultation, /design-shotgun, /design-html, /review, /ship, /land-and-deploy, /canary, /benchmark, /browse, /qa, /qa-only, /design-review, /setup-browser-cookies, /setup-deploy, /setup-gbrain, /retro, /investigate, /document-release, /cso, /autoplan, /plan-devex-review, /devex-review, /careful, /freeze, /guard, /unfreeze, /gstack-upgrade, /learn.
