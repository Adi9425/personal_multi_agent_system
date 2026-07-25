import logging

_configured = False


def configure_logging(level: int = logging.INFO) -> None:
    """Single place that sets up logging. Safe to call from every entrypoint
    (polling script, worker, FastAPI startup) — only configures handlers once."""
    global _configured
    if _configured:
        return

    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)-5s %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    # Third-party libraries are noisy at INFO; keep them at WARNING unless debugging.
    for noisy_logger in ("aiogram.event", "sqlalchemy.engine"):
        logging.getLogger(noisy_logger).setLevel(logging.WARNING)

    _configured = True
