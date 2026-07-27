# Personal Multi-Agent System

A Telegram-fronted personal assistant. A central router (not yet built) will dispatch
messages to specialised agents — notes, tasks/calendar, a job-application pipeline, recall.

The **notes agent** is being built first:
- [`Requirement_Md/notes-agent-hld.md`](Requirement_Md/notes-agent-hld.md) — the original spec (data model, LLM contracts, search/ranking, all 6 build phases).
- [`Requirement_Md/notes-agent-lld.md`](Requirement_Md/notes-agent-lld.md) — how it's actually built: real schemas, real function signatures, real bugs found and fixed, where the implementation deviates from the spec and why.

**Current status: Phases 1–5 complete** — capture, hybrid search/query, and the
update/resolve/Undo path all work end-to-end over Telegram. Phase 6 (Google Docs projection,
plus a user-requested Google Calendar two-way sync beyond the HLD's own scope) is planned but
paused before any code was written. See the LLD for the full picture.

## Architecture (current)

```
Telegram ──(long-polling)──▶ transport/telegram/adapter.py ──▶ Redis (arq queue) ──▶ worker
                                  whitelist, dedupe,                                    │
                                  enqueue (messages + callbacks)                        │
                                                                                         ▼
                                                              agents/notes/__init__.py:handle()
                                                                classify_intent -> capture/query/
                                                                update/unknown, dispatches to
                                                                capture.py / query.py / resolve.py
                                                                                         │
                                                                                         ▼
                                                        services/{llm,embeddings,entries,search,
                                                                  pending_actions}.py
                                                                                         │
                                                                                         ▼
                                                              Postgres (pgvector) + Redis
```

See the LLD for the full per-flow breakdown (capture/query/update), the actual data model,
and every config setting.

## Repo layout

Full breakdown of every module's purpose lives in the LLD (§3). Quick orientation:

`app/` config + FastAPI entrypoint · `core/schemas.py` every Pydantic model + domain enums ·
`db/` SQLAlchemy models + Alembic migrations · `transport/telegram/` the only aiogram-aware
code · `workers/queue.py` the arq worker (where all real work happens) · `services/` the
external-facing layer (LLM, embeddings, Postgres writes, hybrid search, Redis pending-state)
· `agents/notes/` the three flows (capture/query/resolve) · `evals/` the eval harness.

## Local setup

```
docker compose up -d
python -m venv .venv
.venv/Scripts/pip install -r requirements.txt   # .venv/bin/pip on macOS/Linux
copy .env.example .env                          # then fill in TELEGRAM_BOT_TOKEN etc.
.venv/Scripts/python -m alembic upgrade head
.venv/Scripts/python -m uvicorn app.main:app --port 8000   # terminal 1
.venv/Scripts/python -m arq workers.queue.WorkerSettings   # terminal 2
```

## Build phases

This repo is built **one phase at a time**, per HLD §14 — no modules are scaffolded ahead of
the phase currently in progress. **Phases 1–5 are complete**; Phase 6 (Docs + Calendar) is
planned but paused. See the LLD for exactly what exists today and the HLD for the full
6-phase design.

## License

This project is licensed under the [MIT License](LICENSE).
