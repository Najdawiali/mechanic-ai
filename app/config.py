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
    # Which model role each engine call uses ("reasoning" = the larger model, "fast" = the smaller one).
    # Chosen by the evaluation: run eval/run.py with a different value and compare passes and time.
    # Measured 2026-11-16 (case scenarios, 24 runs): the fast model writes hints in 2.5-7 s against 15-25 s,
    # with the same content on every case-in-KB run read; off-topic cases are kept out in code, not by the model.
    llm_role_advise: str = "fast"
    llm_role_general: str = "fast"
    llm_role_summary: str = "reasoning"
    llm_role_translate: str = "fast"
    llm_role_translate_ur: str = "fast"

    database_url: str = "postgresql://mechanic:mechanic@localhost:5432/mechanic_ai"

    # File storage: S3, or anything that speaks the S3 API. The bucket is shared with the platform.
    s3_bucket: str = "mechanic-ai"
    s3_endpoint_url: str | None = None  # None = AWS S3. Local development: http://localhost:8333
    s3_region: str = "us-east-1"
    # None = the standard AWS credential chain (an IAM role in production).
    s3_access_key_id: SecretStr | None = None
    s3_secret_access_key: SecretStr | None = None
    ingest_max_file_mb: int = 50

    # The review screen (/review). It is switched off until a password is set here. A reviewer signs
    # in with their own name as the user name and this password; the name is recorded on what they approve.
    review_password: SecretStr | None = None

    # The assistant's API (/ai). The platform's backend sends this key as "Authorization: Bearer <key>".
    # Unset = the API refuses every call.
    ai_service_key: SecretStr | None = None
    # Serve draft records as well as approved ones. For development and evaluation only: in production
    # the assistant serves approved records only.
    kb_serve_drafts: bool = False
    # The emergency number in the safety texts (Jordan: 911, the unified number).
    emergency_number: str = "911"
    # An outside source of diagnostic data (app/engine/external.py): "off", or "web" (app/web, project doc
    # web-search-design.md). Web results go to mechanics only after the provider's written OK (design 2.12).
    external_source: str = "off"
    # Web search: Exa first, Tavily only if Exa fails. Time limits in seconds; the budget covers the whole lookup.
    exa_api_key: SecretStr | None = None
    tavily_api_key: SecretStr | None = None
    web_search_type: str = "auto"  # Exa's search type; "auto" until scripts/web_verify.py compares it with "fast"
    web_timeout_s: float = 6.0
    web_fallback_timeout_s: float = 5.0
    web_budget_s: float = 8.0
    web_max_results: int = 5
    web_searches_per_session: int = 6
    # Sites never used as a source, comma-separated: they republish manuals without showing any right to them.
    web_blocked_domains: str = "lemon-manuals.la,lemon-manuals.org.ua,charm.li,17vin.com"
    db_pool_max: int = 10

    @property
    def web_blocked(self) -> tuple[str, ...]:
        return tuple(d.strip().lower() for d in self.web_blocked_domains.split(",") if d.strip())


@lru_cache
def get_settings() -> Settings:
    return Settings()
