from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")

    telegram_bot_token: str
    telegram_webhook_secret: str = ""
    telegram_mode: Literal["polling", "webhook"] = "polling"

    # Deliberately reverses the HLD's FR-16 single-chat_id whitelist — the bot now accepts
    # any Telegram user, auto-registering them on a free trial (services/users.py). This is
    # the ONLY identity that grants unrestricted, permanent access, bypassing trial/rate
    # limits entirely. Comma-separated (same pattern the old whitelist var used). Never
    # commit real values here — this repo is public; real IDs live only in .env.
    admin_telegram_user_ids: str = ""

    # The superuser role migrations run as (db/migrations/env.py) -- also a Postgres
    # superuser, which unconditionally bypasses Row-Level Security (see the
    # f1f3d4c93e50 migration). Never used for real request traffic once app_database_url
    # is set below.
    database_url: str = "postgresql+asyncpg://notes:notes@localhost:5432/notes"

    # The restricted, non-superuser role (notes_app) RLS policies actually apply to. Falls
    # back to database_url (the superuser role) if unset, purely so the app keeps working
    # before this is configured -- but RLS is a no-op against that fallback. Set this for
    # RLS to actually do anything: see the f1f3d4c93e50 migration's comment for the one-time
    # `ALTER ROLE notes_app PASSWORD ...` step this depends on.
    app_database_url: str | None = None

    redis_url: str = "redis://localhost:6379"

    # Plain level name ("DEBUG", "INFO", ...) so raising verbosity — e.g. to see every
    # QueryPlan agents/notes/query.py extracts — is a .env change, not a code change. See
    # app/logging_config.py.
    log_level: str = "INFO"

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

    # §6.2 — tune against the eval set, not vibes. Scores are normalized against the
    # theoretical max fused score (see agents/notes/resolve.py) before comparing to these.
    auto_threshold: float = 0.75
    gap_threshold: float = 0.15

    # Free-trial gate (services/users.py). Not in the HLD (multi-user support is an explicit
    # non-goal there) — added at the user's request to open this up beyond a single admin.
    # Both dimensions checked; expired if EITHER is exceeded — the trial model itself is
    # undecided, so this stays a tunable config pair rather than committing to one rule.
    trial_days: int = 14
    trial_request_limit: int = 50

    # Redis fixed-window throttle (services/rate_limit.py) — independent of trial status,
    # protects against runaway API cost from any single user hammering the bot.
    rate_limit_max_per_minute: int = 10

    @property
    def admin_telegram_user_id_set(self) -> set[int]:
        return {int(uid.strip()) for uid in self.admin_telegram_user_ids.split(",") if uid.strip()}

    @property
    def effective_app_database_url(self) -> str:
        return self.app_database_url or self.database_url


settings = Settings()
