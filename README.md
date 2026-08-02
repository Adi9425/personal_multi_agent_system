# Personal Multi-Agent System

A personal multi-agent system, fronted by Telegram, for managing your own data: notes, tasks
and deadlines, and quick personal facts (profile links, credentials, anything you'd otherwise
jot down and lose). A central router dispatches messages to specialised agents — **notes** is
the first one built; a job-application pipeline and a recall/memory agent are planned.

**Try the bot:** [@Adis_jacob_bot](https://t.me/Adis_jacob_bot)

## What it does

Talk to it like a person, in whatever shape the thought comes in:

- **Capture** — `submit timesheet by friday`, `stateless auth tokens carry an expiry claim...`
  → filed as a task or note, categorized and searchable, no rigid syntax required.
- **Save a quick fact** — `save my linkedin profile: https://linkedin.com/in/...`,
  `save this as my college dance videos https://youtube.com/watch?v=...` → stored under a key
  you can ask for again later, whether it's your own info or someone else's.
- **Ask** — `what's my linkedin profile`, `what do I have open this week`, `notes about JWT`
  → hybrid full-text + vector search over everything you've saved, with a synthesized answer
  and cited sources.
- **Update** — `mark the timesheet as done`, `push the passport renewal to next month` → with
  disambiguation and Undo when it's not obvious which entry you mean.

Common message shapes (`save X: Y`, `what's X`, `hi`, `are you alive`) are recognized with
zero-LLM pattern matching before ever reaching a model — see
[`Requirement_Md/request-flow.md`](Requirement_Md/request-flow.md) for exactly which messages
take the fast path versus the full LLM pipeline, and why.

## Docs

- [`Requirement_Md/notes-agent-hld.md`](Requirement_Md/notes-agent-hld.md) — the original spec (data model, LLM contracts, search/ranking).
- [`Requirement_Md/notes-agent-lld.md`](Requirement_Md/notes-agent-lld.md) — how it's actually built: real schemas, real function signatures, real bugs found and fixed, where the implementation deviates from the spec and why.
- [`Requirement_Md/request-flow.md`](Requirement_Md/request-flow.md) — a message's full path from Telegram to reply, stage by stage, with which calls are zero-LLM and which aren't.
- [`Requirement_Md/context-engineering-learning-path.md`](Requirement_Md/context-engineering-learning-path.md) — the token-reduction curriculum applied to this codebase.

## Architecture (current)

```
Telegram ──(long-polling)──▶ transport/telegram/adapter.py ──▶ Redis (arq queue) ──▶ worker
                                access control, dedupe,                                │
                                enqueue (messages + callbacks)                         │
                                                                                        ▼
                                                     workers/queue.py:_build_reply()
                                                       /list, /note, save/remember fast path ──▶ services/kv_notes.py
                                                                                        │
                                                                                        ▼
                                                     agents/notes/__init__.py:handle()
                                                       chitchat + "what's X" fast paths, else
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

See the LLD and `request-flow.md` for the full per-flow breakdown, the actual data model, and
every config setting.

## Repo layout

Full breakdown of every module's purpose lives in the LLD (§3). Quick orientation:

`app/` config + FastAPI entrypoint · `core/schemas.py` every Pydantic model + domain enums ·
`db/` SQLAlchemy models + Alembic migrations · `transport/telegram/` the only aiogram-aware
code · `workers/queue.py` the arq worker (where all real work happens) · `services/` the
external-facing layer (LLM, embeddings, Postgres writes, hybrid search, Redis pending-state,
zero-LLM fast paths for facts/chitchat) · `agents/notes/` the LLM-driven flows
(capture/query/resolve) · `evals/` the eval harness.

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

## License

This project is licensed under the [MIT License](LICENSE).
