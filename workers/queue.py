import asyncio
import logging
import uuid

import agents.notes
from agents.notes.resolve import apply_operation, confirmation_text, undo
from app.config import settings
from app.logging_config import configure_logging
from core.schemas import Button, CallbackPayload, JobPayload, Reply
from db.models import EntryCategory, EntryKind
from db.session import async_session
from services import dispatch_patterns, kv_notes, pending_actions
from services.entries import create_entry, list_recent
from services.users import is_admin
from workers.pool import REDIS_SETTINGS

configure_logging()
logger = logging.getLogger(__name__)

_PATTERN_USAGE = (
    "Usage:\n"
    "/pattern add extract <save|get> <priority> <regex>\\n<note>\n"
    "/pattern add reject <save|get> <regex>\\n<note>\n"
    "/pattern list [flow]\n"
    "/pattern enable <ref>\n"
    "/pattern disable <ref>\n"
    "(ref looks like p12 or r3 — the id shown by /pattern list, prefixed by kind)"
)


def _format_pattern_row(row: dict) -> str:
    status = "on " if row["enabled"] else "off"
    return f"p{row['id']} [{status}] {row['flow']} pri={row['priority']} hits={row['match_count']}\n  {row['pattern']}\n  note: {row['note']}"


def _format_reject_row(row: dict) -> str:
    status = "on " if row["enabled"] else "off"
    return f"r{row['id']} [{status}] {row['flow']} vetoes={row['veto_count']}\n  {row['pattern']}\n  note: {row['note']}"


async def _handle_pattern_command(job: JobPayload, text: str) -> Reply:
    if not is_admin(job.user_id):
        return Reply(text="Admin only.")

    first_line, _, rest = text.partition("\n")
    note = rest.strip()

    # Safe to fully split just to peek at the subcommand/flow tokens — only the "add" branch
    # below needs a maxsplit re-split, since the regex argument itself may contain spaces.
    args = first_line.split()

    if len(args) == 1:
        return Reply(text=_PATTERN_USAGE)

    sub = args[1].lower()

    if sub == "list":
        flow_filter = args[2].lower() if len(args) > 2 else None
        patterns, rejects = await dispatch_patterns.list_all()
        if flow_filter:
            patterns = [p for p in patterns if p["flow"] == flow_filter]
            rejects = [r for r in rejects if r["flow"] == flow_filter]
        if not patterns and not rejects:
            return Reply(text="No patterns.")
        lines = [_format_pattern_row(p) for p in patterns] + [_format_reject_row(r) for r in rejects]
        return Reply(text="\n\n".join(lines))

    if sub in ("enable", "disable"):
        if len(args) != 3 or len(args[2]) < 2 or args[2][0] not in ("p", "r"):
            return Reply(text=_PATTERN_USAGE)
        kind, ref_id = args[2][0], args[2][1:]
        if not ref_id.isdigit():
            return Reply(text=_PATTERN_USAGE)
        found = await dispatch_patterns.set_enabled(kind, int(ref_id), enabled=(sub == "enable"))
        if not found:
            return Reply(text=f"No pattern with ref {args[2]}.")
        return Reply(text=f"{args[2]} {'enabled' if sub == 'enable' else 'disabled'}.")

    if sub == "add":
        if len(args) < 3:
            return Reply(text=_PATTERN_USAGE)
        pattern_type = args[2].lower()
        if not note:
            return Reply(text="Missing note — put it on the line after the command.\n\n" + _PATTERN_USAGE)

        if pattern_type == "extract":
            # maxsplit=5: "/pattern add extract <flow> <priority> " is 5 tokens; everything
            # after is kept as one unsplit remainder, so a regex containing spaces survives.
            parsed = first_line.split(None, 5)
            if len(parsed) != 6:
                return Reply(text=_PATTERN_USAGE)
            _, _, _, flow, priority_str, regex = parsed
            if flow not in ("save", "get") or not priority_str.isdigit():
                return Reply(text=_PATTERN_USAGE)
            row_id, failures = await dispatch_patterns.add_extract_pattern(
                flow=flow, priority=int(priority_str), pattern=regex, note=note, created_by=job.user_id,
            )
            if failures:
                return Reply(text=f"p{row_id} added but DISABLED — failed validation:\n" + "\n".join(failures))
            return Reply(text=f"p{row_id} added and enabled.")

        if pattern_type == "reject":
            parsed = first_line.split(None, 4)  # "/pattern add reject <flow> " = 4 tokens
            if len(parsed) != 5:
                return Reply(text=_PATTERN_USAGE)
            _, _, _, flow, regex = parsed
            if flow not in ("save", "get"):
                return Reply(text=_PATTERN_USAGE)
            row_id, failures = await dispatch_patterns.add_reject_pattern(
                flow=flow, pattern=regex, note=note, created_by=job.user_id,
            )
            if failures:
                return Reply(text=f"r{row_id} added but DISABLED — failed validation:\n" + "\n".join(failures))
            return Reply(text=f"r{row_id} added and enabled.")

        return Reply(text=_PATTERN_USAGE)

    return Reply(text=_PATTERN_USAGE)


async def _build_reply(job: JobPayload) -> Reply:
    text = job.text.strip()

    if text == "/pattern" or text.startswith("/pattern "):
        return await _handle_pattern_command(job, text)

    if text == "/list":
        async with async_session() as session:
            entries = await list_recent(session, user_id=job.user_id)
        if not entries:
            return Reply(text="No notes yet.")
        lines = [f"{i}. {e.title} ({e.created_at:%Y-%m-%d %H:%M})" for i, e in enumerate(entries, start=1)]
        return Reply(text="\n".join(lines))

    if text == "/note" or text.startswith("/note "):
        body = text[len("/note") :].strip()
        if not body:
            return Reply(text="Usage: /note <text>")
        async with async_session() as session:
            entry = await create_entry(
                session,
                user_id=job.user_id,
                category=EntryCategory.personal,
                kind=EntryKind.note,
                title=body[:80],
                body=body,
                source_msg_id=job.msg_id,
            )
        return Reply(text=f"saved: {entry.title}")

    # Zero-LLM-first fast path for "save X: Y" messages — falls back to a cheap LLM
    # extractor for messy phrasing, but a save/remember trigger always ends here (never the
    # general capture pipeline below). Returns None only if there's no trigger at all.
    kv_reply = await kv_notes.try_handle_save(job.user_id, text, job.msg_id)
    if kv_reply is not None:
        return kv_reply

    # Phase 3: plain messages (no slash command, no KV match) route to LLM-driven capture.
    return await agents.notes.handle(user_id=job.user_id, text=job.text, msg_id=job.msg_id)


async def handle_message(ctx, payload: dict) -> None:
    # Deferred import: transport.telegram.adapter imports workers.pool (producer side),
    # so importing adapter at module load time here would be circular.
    from transport.telegram.adapter import send_reply

    job = JobPayload.model_validate(payload)

    if settings.worker_test_delay_seconds:
        logger.info(
            "test delay: sleeping %ss before send (update_id=%s)",
            settings.worker_test_delay_seconds,
            job.update_id,
        )
        await asyncio.sleep(settings.worker_test_delay_seconds)

    reply = await _build_reply(job)

    message_id = await send_reply(job.chat_id, reply)
    logger.info("sent reply for update_id=%s", job.update_id)

    if reply.buttons:
        # The pending-action token(s) were created before this message existed (resolve.py
        # needs the token to build the button before anything is sent) — attach the
        # now-known (chat_id, message_id) so a later callback can edit this exact message.
        for button in reply.buttons:
            await pending_actions.attach(button.callback_data, {"chat_id": job.chat_id, "message_id": message_id})


async def handle_callback(ctx, payload: dict) -> None:
    from transport.telegram.adapter import edit_reply

    cb = CallbackPayload.model_validate(payload)
    data = await pending_actions.fetch(cb.token)

    if data is None:
        await edit_reply(cb.chat_id, cb.message_id, Reply(text="This action has expired."))
        return

    await pending_actions.delete(cb.token)  # one-shot use

    if data["kind"] == "cancel":
        await edit_reply(cb.chat_id, cb.message_id, Reply(text="OK, no changes made."))
        return

    if data["kind"] == "disambiguation_choice":
        entry, changes = await apply_operation(
            operation=data["operation"],
            entry_id=uuid.UUID(data["entry_id"]),
            value=data["value"],
            source_msg_id=data["source_msg_id"],
        )
        undo_token = await pending_actions.store(
            {"kind": "undo", "entry_id": str(entry.id), "changes": changes}
        )
        await edit_reply(
            cb.chat_id,
            cb.message_id,
            Reply(text=confirmation_text(data["operation"], entry, data["value"]),
                  buttons=[Button(label="Undo", callback_data=undo_token)]),
        )
        # This new Undo button also needs its (chat_id, message_id) attached — it's the
        # same message we just edited, so we already know both.
        await pending_actions.attach(undo_token, {"chat_id": cb.chat_id, "message_id": cb.message_id})
        return

    if data["kind"] == "undo":
        entry, _ = await undo(entry_id=uuid.UUID(data["entry_id"]), changes=data["changes"])
        await edit_reply(cb.chat_id, cb.message_id, Reply(text=f"Undone: {entry.title}"))
        return

    logger.warning("unknown pending action kind=%s for token=%s", data.get("kind"), cb.token)


async def on_startup(ctx) -> None:
    await dispatch_patterns.start()


async def on_shutdown(ctx) -> None:
    await dispatch_patterns.stop()


class WorkerSettings:
    functions = [handle_message, handle_callback]
    redis_settings = REDIS_SETTINGS
    on_startup = on_startup
    on_shutdown = on_shutdown
    # Phase 3 jobs now involve real LLM calls (classify + extract + embed, sequentially) —
    # a few seconds typically, but the old 30s Phase-1 echo-tuned ceiling is too tight a
    # margin. 60s gives real headroom without reverting to arq's oversized 300s default.
    job_timeout = 60
