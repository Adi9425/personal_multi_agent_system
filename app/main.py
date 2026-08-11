import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Header, HTTPException, Request

from app.config import settings
from app.logging_config import configure_logging
from transport.telegram.adapter import process_webhook_update, run_polling

configure_logging()
logger = logging.getLogger(__name__)

_polling_task: asyncio.Task | None = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _polling_task
    if settings.telegram_mode == "polling":
        logger.info("starting Telegram long-polling")
        _polling_task = asyncio.create_task(run_polling())
    yield
    if _polling_task is not None:
        _polling_task.cancel()


app = FastAPI(lifespan=lifespan)


@app.get("/health")
async def health() -> dict:
    return {"status": "ok"}


@app.post("/telegram/webhook")
async def telegram_webhook(
    request: Request,
    x_telegram_bot_api_secret_token: str | None = Header(default=None),
) -> dict:
    if settings.telegram_mode != "webhook":
        raise HTTPException(status_code=404)
    # An unset secret (empty string, the default) must never match — otherwise an attacker
    # who sends no header at all, or an empty one, would authenticate against a
    # never-configured secret. Verified this is a real gap: "" != "" is False, so a bare
    # equality check alone would silently accept it.
    if not settings.telegram_webhook_secret or x_telegram_bot_api_secret_token != settings.telegram_webhook_secret:
        raise HTTPException(status_code=401)

    update_data = await request.json()
    await process_webhook_update(update_data)
    return {"ok": True}
