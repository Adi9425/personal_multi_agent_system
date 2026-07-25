# Personal Multi-Agent System

A Telegram-fronted personal assistant. A central router (not yet built) will dispatch
messages to specialised agents — notes, tasks/calendar, a job-application pipeline, recall.

The **notes agent** is being built first — see [`Requirement_Md/notes-agent-hld.md`](Requirement_Md/notes-agent-hld.md)
for the full design (data model, LLM contracts, search/ranking, all 6 build phases).

**Current status: Phase 1 — Plumbing, zero LLM.** Just the transport → queue → worker →
reply path, whitelist, and dedupe. No persistence beyond dedupe, no LLM, no notes logic yet.

## Architecture (Phase 1)

```
Telegram ──(long-polling or webhook)──▶ transport/telegram/adapter.py
                                             │  1. whitelist check          (FR-16)
                                             │  2. processed_updates dedupe (FR-17)
                                             │  3. enqueue JobPayload
                                             ▼
                                        Redis (arq queue)
                                             │
                                             ▼
                                        workers/queue.py
                                             │  (Phase 1: echoes the message back)
                                             ▼
                                        Telegram reply
```

Postgres is only used for the `processed_updates` dedupe table in this phase — the full
`entries` schema, search, and LLM extraction arrive in later phases.

## Repo layout (Phase 1 subset)

| Path | Purpose |
|---|---|
| `app/config.py` | `pydantic-settings` — the single source of truth for every env var. Secrets are never hardcoded (NFR-7); everything is read from `.env`. |
| `core/schemas.py` | Transport-neutral contracts: `JobPayload` (crosses the Telegram → Redis boundary, carries the unused `conversation_id` per D10), `Reply`/`Button` (what a handler returns; the Telegram adapter is the only thing that translates these into actual Telegram calls — D6). |
| `db/models.py` | SQLAlchemy models. Phase 1 defines only `ProcessedUpdate` (backs FR-17). The full `entries`/`entry_events` schema from HLD §4 lands in Phase 2. |
| `db/session.py` | Async SQLAlchemy engine + session factory, built from `app.config.settings.database_url`. |
| `db/migrations/` | Alembic migrations — one incremental migration per phase's schema additions. |
| `transport/telegram/adapter.py` | aiogram `Bot`/`Dispatcher`. The **only** module that imports aiogram (D6/§3.2) — everything else in the app is transport-agnostic, so a future web UI wouldn't need a rewrite. Exposes the shared whitelist → dedupe → enqueue path used by both polling and webhook. |
| `workers/queue.py` | `arq` worker definition and job functions. Work happens here, off the request path, so the Telegram webhook can return in <2s (NFR-1) and a worker crash mid-job can't lose the message (NFR-3). |
| `docker-compose.yml` | Postgres (`pgvector/pgvector` image — extension unused until Phase 2, but avoids an image swap later) + Redis, for local dev. |
| `requirements.txt` | Python dependencies, installed into `.venv` via `pip`. |
| `.env.example` | Template for every required environment variable — copy to `.env` (gitignored) and fill in real values. |
| `Requirement_Md/notes-agent-hld.md` | The spec. Drives every phase of this build; not something to improvise around. |

## Local setup

```
docker compose up -d
python -m venv .venv
.venv/Scripts/pip install -r requirements.txt   # .venv/bin/pip on macOS/Linux
copy .env.example .env                          # then fill in TELEGRAM_BOT_TOKEN etc.
.venv/Scripts/python -m alembic upgrade head
```

## Build phases

This repo is built **one phase at a time**, per HLD §14 — no modules are scaffolded ahead
of the phase currently in progress. Phase 1 is in progress now; see the HLD for what Phases
2–6 add (persistence, LLM extraction, hybrid search, update/undo flow, Google Docs projection).

## License

This project is licensed under the [MIT License](LICENSE).
