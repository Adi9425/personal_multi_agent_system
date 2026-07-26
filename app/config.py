from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")

    telegram_bot_token: str
    telegram_allowed_chat_ids: str  # comma-separated, e.g. "123456789,987654321"
    telegram_webhook_secret: str = ""
    telegram_mode: Literal["polling", "webhook"] = "polling"

    database_url: str = "postgresql+asyncpg://notes:notes@localhost:5432/notes"
    redis_url: str = "redis://localhost:6379"

    user_timezone: str = "Asia/Kolkata"

    # HLD §4 note: keep this in one config constant, not scattered across migrations.
    # 512 is voyage-3-lite's actual (fixed) output size — the HLD assumed 1024, which is
    # wrong for this specific model (confirmed against the live Voyage API in Phase 3).
    # Change to 1536 for OpenAI text-embedding-3-small.
    embedding_dim: int = 512

    # Test aid for the Phase 1 worker-restart durability check (see acceptance criteria).
    # Sleeps before the echo send so a worker can be killed mid-job on purpose.
    worker_test_delay_seconds: int = 0

    anthropic_api_key: str
    model_fast: str = "claude-haiku-4-5-20251001"  # intent classification, query plan
    model_strong: str = "claude-sonnet-5"  # extraction, synthesis

    voyage_api_key: str

    langfuse_public_key: str
    langfuse_secret_key: str
    langfuse_host: str = "https://cloud.langfuse.com"

    # §6.4 — near-inert at low volume; raise as the corpus grows. Both live here, not
    # hardcoded in the query, so tuning is a config change rather than a rescoring exercise.
    recency_weight: float = 0.005
    recency_halflife_days: int = 30

    # Not in the HLD — added after observing that RRF's vec CTE ranked *every* row with a
    # non-null embedding regardless of actual relevance (no similarity floor), so an unrelated
    # entry could still show up as a "source" in a tiny corpus. 0.5 is a starting point based
    # on one real measurement (0.617 relevant vs 0.416 unrelated, voyage-3-lite) — tune against
    # the eval set as real usage data accumulates, same philosophy as AUTO_THRESHOLD in §6.2.
    min_vector_similarity: float = 0.5

    @property
    def allowed_chat_ids(self) -> set[int]:
        return {int(cid.strip()) for cid in self.telegram_allowed_chat_ids.split(",") if cid.strip()}


settings = Settings()
