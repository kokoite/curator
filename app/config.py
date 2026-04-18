"""Application configuration via pydantic-settings, loaded from .env."""

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    gemini_api_key: str
    classifier_model: str = "gemma-4-31b-it"
    generator_model: str = "gemini-2.5-pro"
    classifier_max_retries: int = 1

    headless: bool = True
    page_timeout_ms: int = 30000


settings = Settings()
