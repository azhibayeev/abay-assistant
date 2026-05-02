from functools import lru_cache

from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    telegram_bot_token: str
    telegram_owner_id: int
    telegram_assistant_id: int = 0
    telegram_assistant_username: str = ""
    telegram_group_id: int = 0

    anthropic_api_key: str = ""
    groq_api_key: str = ""

    trello_api_key: str = ""
    trello_token: str = ""
    trello_board_id: str = ""

    vault_path: str = "./abay-vault"
    database_url: str = "sqlite:///abay.db"
    timezone: str = "Asia/Almaty"

    # Webhook (опционально — если не задан, используется polling)
    webhook_url: str = ""
    webhook_port: int = 8443
    webhook_cert_path: str = ""
    webhook_key_path: str = ""

    model_config = {"env_file": ".env", "env_file_encoding": "utf-8"}


@lru_cache
def get_settings() -> Settings:
    return Settings()
