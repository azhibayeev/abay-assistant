# Abay Assistant

ИИ-ассистент Абая — Telegram-бот с LLM, интеграциями Trello, Google Calendar и Obsidian vault.

## Запуск (разработка)

```bash
cp .env.example .env
# Заполнить .env реальными токенами

uv run python -m abay_assistant.main
```

## Структура

- `src/abay_assistant/` — основной код
- `abay-vault/` — Obsidian vault
- `tests/` — тесты

## Стек

- Python 3.11+, aiogram 3, FastAPI, SQLModel, Anthropic SDK
