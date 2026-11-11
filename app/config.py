"""Settings, read from environment variables or the .env file in the repo root."""

from functools import lru_cache

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # SecretStr keeps the key out of logs, reprs and tracebacks.
    deepinfra_api_key: SecretStr | None = None
    llm_base_url: str = "https://api.deepinfra.com/v1/openai"

    # Role -> model id. The rest of the code asks for a role, never for a model name.
    llm_reasoning_model: str = "google/gemma-4-31B-it"
    llm_fast_model: str = "google/gemma-4-26B-A4B-it"
    llm_vision_model: str = "google/gemma-4-31B-it"

    llm_timeout_s: float = 90.0

    database_url: str = "postgresql://mechanic:mechanic@localhost:5432/mechanic_ai"


@lru_cache
def get_settings() -> Settings:
    return Settings()
