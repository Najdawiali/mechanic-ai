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
    # Search by meaning. The model gives 2560 numbers per text; 1024 are kept (it is trained so
    # that the first numbers carry the most), which PostgreSQL can index. Changing the size needs a migration.
    llm_embedding_model: str = "Qwen/Qwen3-Embedding-4B"
    embedding_dimensions: int = 1024
    # A piece of text less similar to the query than this is not offered. Provisional: set from the
    # first real query (related pieces 0.64 to 0.68, unrelated 0.31 to 0.41); to be tuned in the evaluation.
    search_min_similarity: float = 0.5

    llm_timeout_s: float = 90.0

    database_url: str = "postgresql://mechanic:mechanic@localhost:5432/mechanic_ai"

    # File storage: S3, or anything that speaks the S3 API. The bucket is shared with the platform.
    s3_bucket: str = "mechanic-ai"
    s3_endpoint_url: str | None = None  # None = AWS S3. Local development: http://localhost:8333
    s3_region: str = "us-east-1"
    # None = the standard AWS credential chain (an IAM role in production).
    s3_access_key_id: SecretStr | None = None
    s3_secret_access_key: SecretStr | None = None
    ingest_max_file_mb: int = 50


@lru_cache
def get_settings() -> Settings:
    return Settings()
