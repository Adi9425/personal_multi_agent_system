import asyncio
import logging
import re
from dataclasses import dataclass

import asyncpg
from sqlalchemy import text

from app.config import settings
from db.session import async_session
from services.dispatch_pattern_fixtures import EXTRACT_FIXTURES_BY_FLOW, REJECT_FIXTURES_BY_FLOW

_VALID_FLOWS = {"save", "get"}

logger = logging.getLogger(__name__)

_RELOAD_CHANNEL = "dispatch_patterns_changed"
_FALLBACK_RELOAD_SECONDS = 300  # safety net for a missed (non-durable) NOTIFY

# asyncpg.connect() takes a plain postgres DSN, not the "+asyncpg" SQLAlchemy driver qualifier.
_RAW_DSN = settings.database_url.replace("postgresql+asyncpg://", "postgresql://")


@dataclass
class CompiledExtractPattern:
    id: int
    regex: re.Pattern


@dataclass
class CompiledRejectRule:
    id: int
    regex: re.Pattern


# D5-style: this module is the only reader/writer of dispatch_patterns/dispatch_reject_rules
# used on the hot path. Populated by reload_patterns(), read by get_patterns()/
# get_reject_rules() — never queried per-message, only at worker startup and on change.
_patterns: dict[str, list[CompiledExtractPattern]] = {}
_reject_rules: dict[str, list[CompiledRejectRule]] = {}

_listener_conn: asyncpg.Connection | None = None
_fallback_task: asyncio.Task | None = None


async def _fetch_and_compile() -> tuple[dict[str, list[CompiledExtractPattern]], dict[str, list[CompiledRejectRule]]]:
    async with async_session() as session:
        pattern_rows = await session.execute(
            text(
                "SELECT id, flow, pattern FROM dispatch_patterns "
                "WHERE enabled ORDER BY flow, priority ASC"
            )
        )
        reject_rows = await session.execute(
            text("SELECT id, flow, pattern FROM dispatch_reject_rules WHERE enabled ORDER BY flow")
        )

        patterns: dict[str, list[CompiledExtractPattern]] = {}
        for row in pattern_rows:
            try:
                regex = re.compile(row.pattern, re.IGNORECASE)
            except re.error:
                logger.error("dispatch_patterns id=%s has an invalid regex, skipping", row.id)
                continue
            patterns.setdefault(row.flow, []).append(CompiledExtractPattern(id=row.id, regex=regex))

        rejects: dict[str, list[CompiledRejectRule]] = {}
        for row in reject_rows:
            try:
                regex = re.compile(row.pattern, re.IGNORECASE)
            except re.error:
                logger.error("dispatch_reject_rules id=%s has an invalid regex, skipping", row.id)
                continue
            rejects.setdefault(row.flow, []).append(CompiledRejectRule(id=row.id, regex=regex))

        return patterns, rejects


async def reload_patterns() -> None:
    """Full re-fetch — cheap even at a few hundred rows, and only ever runs at worker
    startup, on a LISTEN/NOTIFY hit, or the periodic fallback tick, never per-message."""
    global _patterns, _reject_rules
    patterns, rejects = await _fetch_and_compile()
    _patterns = patterns
    _reject_rules = rejects
    logger.info("dispatch_patterns reloaded: %s", {flow: len(p) for flow, p in patterns.items()})


def get_patterns(flow: str) -> list[CompiledExtractPattern]:
    return _patterns.get(flow, [])


def get_reject_rules(flow: str) -> list[CompiledRejectRule]:
    return _reject_rules.get(flow, [])


async def record_match(pattern_id: int) -> None:
    """Best-effort — a telemetry write must never break a real save/get reply."""
    try:
        async with async_session() as session:
            await session.execute(
                text(
                    "UPDATE dispatch_patterns SET match_count = match_count + 1, "
                    "last_matched_at = now() WHERE id = :id"
                ),
                {"id": pattern_id},
            )
            await session.commit()
    except Exception:
        logger.warning("failed to record match for dispatch_pattern id=%s", pattern_id, exc_info=True)


async def record_veto(rule_id: int) -> None:
    try:
        async with async_session() as session:
            await session.execute(
                text(
                    "UPDATE dispatch_reject_rules SET veto_count = veto_count + 1, "
                    "last_matched_at = now() WHERE id = :id"
                ),
                {"id": rule_id},
            )
            await session.commit()
    except Exception:
        logger.warning("failed to record veto for dispatch_reject_rule id=%s", rule_id, exc_info=True)


def validate_extract_pattern(flow: str, pattern: str) -> list[str]:
    """Dry-run gate for a candidate extract pattern. Empty list = safe to enable. Never
    raises — a bad pattern is reported back to the admin, not a stack trace."""
    failures: list[str] = []

    try:
        compiled = re.compile(pattern, re.IGNORECASE)
    except re.error as e:
        return [f"regex does not compile: {e}"]

    required_groups = {"key", "value"} if flow == "save" else {"key"}
    missing = required_groups - set(compiled.groupindex)
    if missing:
        failures.append(f"missing required named group(s): {', '.join(sorted(missing))}")
        return failures  # further checks would just raise on a missing group

    for fixture in EXTRACT_FIXTURES_BY_FLOW.get(flow, []):
        match = compiled.match(fixture.text)
        if not fixture.should_match:
            if match:
                failures.append(f"incorrectly matched {fixture.text!r} (should not have matched)")
            continue
        if not match:
            failures.append(f"failed to match {fixture.text!r}")
            continue
        got_key = match.group("key").strip()
        if got_key != fixture.expected_key:
            failures.append(f"{fixture.text!r}: key {got_key!r} != expected {fixture.expected_key!r}")
        if flow == "save":
            got_value = match.group("value").strip()
            if got_value != fixture.expected_value:
                failures.append(
                    f"{fixture.text!r}: value {got_value!r} != expected {fixture.expected_value!r}"
                )

    return failures


def validate_reject_pattern(flow: str, pattern: str) -> list[str]:
    failures: list[str] = []
    try:
        compiled = re.compile(pattern, re.IGNORECASE)
    except re.error as e:
        return [f"regex does not compile: {e}"]

    for fixture in REJECT_FIXTURES_BY_FLOW.get(flow, []):
        matched = compiled.fullmatch(fixture.key) is not None
        if matched != fixture.should_reject:
            expectation = "reject" if fixture.should_reject else "NOT reject"
            failures.append(f"key {fixture.key!r} should {expectation}, but pattern {'did' if matched else 'did not'}")

    return failures


async def add_extract_pattern(
    *, flow: str, priority: int, pattern: str, note: str, created_by: int
) -> tuple[int, list[str]]:
    """Validates before writing. Inserted `enabled=false` if validation fails — visible and
    inspectable via `/pattern list`, not silently dropped; an admin can still force-enable
    since they're the only reviewer anyway."""
    if flow not in _VALID_FLOWS:
        raise ValueError(f"unknown flow {flow!r}, must be one of {_VALID_FLOWS}")

    failures = validate_extract_pattern(flow, pattern)
    async with async_session() as session:
        result = await session.execute(
            text(
                "INSERT INTO dispatch_patterns (flow, pattern, priority, enabled, note, created_by) "
                "VALUES (:flow, :pattern, :priority, :enabled, :note, :created_by) RETURNING id"
            ),
            {
                "flow": flow, "pattern": pattern, "priority": priority,
                "enabled": not failures, "note": note, "created_by": created_by,
            },
        )
        row_id = result.scalar_one()
        await session.commit()

    await _safe_reload("add_extract_pattern")  # immediate local consistency, no NOTIFY race
    return row_id, failures


async def add_reject_pattern(*, flow: str, pattern: str, note: str, created_by: int) -> tuple[int, list[str]]:
    if flow not in _VALID_FLOWS:
        raise ValueError(f"unknown flow {flow!r}, must be one of {_VALID_FLOWS}")

    failures = validate_reject_pattern(flow, pattern)
    async with async_session() as session:
        result = await session.execute(
            text(
                "INSERT INTO dispatch_reject_rules (flow, pattern, enabled, note, created_by) "
                "VALUES (:flow, :pattern, :enabled, :note, :created_by) RETURNING id"
            ),
            {"flow": flow, "pattern": pattern, "enabled": not failures, "note": note, "created_by": created_by},
        )
        row_id = result.scalar_one()
        await session.commit()

    await _safe_reload("add_reject_pattern")
    return row_id, failures


async def set_enabled(kind: str, row_id: int, enabled: bool) -> bool:
    """kind is 'p' (dispatch_patterns) or 'r' (dispatch_reject_rules). Returns False if no
    row with that id exists."""
    table = {"p": "dispatch_patterns", "r": "dispatch_reject_rules"}.get(kind)
    if table is None:
        raise ValueError(f"unknown kind {kind!r}, must be 'p' or 'r'")

    async with async_session() as session:
        result = await session.execute(
            text(f"UPDATE {table} SET enabled = :enabled WHERE id = :id"),  # table name from a fixed allow-list above, not user input
            {"enabled": enabled, "id": row_id},
        )
        found = result.rowcount > 0
        await session.commit()

    if found:
        await _safe_reload("set_enabled")
    return found


async def list_all() -> tuple[list[dict], list[dict]]:
    """Returns (patterns, reject_rules) as plain dicts, newest first, for /pattern list."""
    async with async_session() as session:
        pattern_rows = await session.execute(
            text(
                "SELECT id, flow, pattern, priority, enabled, note, match_count "
                "FROM dispatch_patterns ORDER BY flow, priority, id"
            )
        )
        reject_rows = await session.execute(
            text(
                "SELECT id, flow, pattern, enabled, note, veto_count "
                "FROM dispatch_reject_rules ORDER BY flow, id"
            )
        )
        return (
            [dict(row._mapping) for row in pattern_rows],
            [dict(row._mapping) for row in reject_rows],
        )


async def _safe_reload(reason: str) -> None:
    try:
        await reload_patterns()
    except Exception:
        logger.error("failed to reload dispatch patterns (%s)", reason, exc_info=True)


def _on_notify(_conn, _pid, _channel, payload) -> None:
    # asyncpg listener callbacks are sync — schedule the actual (async) reload rather than
    # awaiting here.
    logger.info("dispatch_patterns_changed notification received (table=%s), reloading", payload)
    asyncio.create_task(_safe_reload(f"notify:{payload}"))


async def _fallback_reload_loop() -> None:
    # Postgres NOTIFYs aren't durable — a listener connection blip loses one silently. This
    # just bounds how stale the cache can ever get if that happens, it's not the primary
    # refresh mechanism.
    while True:
        await asyncio.sleep(_FALLBACK_RELOAD_SECONDS)
        await _safe_reload("periodic fallback")


async def start() -> None:
    """Call once at worker startup (arq WorkerSettings.on_startup). Loads patterns, opens a
    dedicated, unpooled LISTEN connection (per Postgres LISTEN/NOTIFY best practice — never
    share this with the pooled SQLAlchemy engine), and starts the periodic fallback reload."""
    global _listener_conn, _fallback_task
    await reload_patterns()
    _listener_conn = await asyncpg.connect(dsn=_RAW_DSN)
    await _listener_conn.add_listener(_RELOAD_CHANNEL, _on_notify)
    _fallback_task = asyncio.create_task(_fallback_reload_loop())
    logger.info("dispatch_patterns: LISTEN/NOTIFY + fallback reload started")


async def stop() -> None:
    """Call once at worker shutdown (arq WorkerSettings.on_shutdown)."""
    global _listener_conn, _fallback_task
    if _fallback_task is not None:
        _fallback_task.cancel()
        _fallback_task = None
    if _listener_conn is not None:
        await _listener_conn.close()
        _listener_conn = None
