from functools import lru_cache
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(extra="ignore")

    gmail_from: str
    gmail_to: str
    gmail_app_password: str
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""
    openrouter_api_key: str = ""
    openrouter_model: str = "meta-llama/llama-3.3-70b:free"
    site_url: str = ""  # e.g. http://192.168.1.100:3006 — shown as "Edit profile" link in email footer


@lru_cache
def get_settings() -> Settings:
    return Settings()
