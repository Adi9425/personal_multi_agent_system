import logging

from app.config import settings

_configured = False


def configure_logging(level: int | None = None) -> None:
    """Single place that sets up logging. Safe to call from every entrypoint
    (polling script, worker, FastAPI startup) — only configures handlers once.

    level defaults to settings.log_level (a plain name like "DEBUG"/"INFO") so raising
    verbosity — e.g. to see the per-query QueryPlan and row detail
    agents/notes/query.py and services/search.py log at DEBUG — is a LOG_LEVEL=DEBUG
    change in .env, not a code change."""
    global _configured
    if _configured:
        return

    resolved_level = level if level is not None else getattr(logging, settings.log_level.upper(), logging.INFO)

    logging.basicConfig(
        level=resolved_level,
        format="%(asctime)s %(levelname)-5s %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    # Third-party libraries are noisy at INFO; keep them at WARNING unless debugging.
    for noisy_logger in ("aiogram.event", "sqlalchemy.engine"):
        logging.getLogger(noisy_logger).setLevel(logging.WARNING)

    _configured = True
