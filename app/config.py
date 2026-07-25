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

    # Test aid for the Phase 1 worker-restart durability check (see acceptance criteria).
    # Sleeps before the echo send so a worker can be killed mid-job on purpose.
    worker_test_delay_seconds: int = 0

    @property
    def allowed_chat_ids(self) -> set[int]:
        return {int(cid.strip()) for cid in self.telegram_allowed_chat_ids.split(",") if cid.strip()}


settings = Settings()
