# Notes Agent — High Level Design & Build Spec

**Component of:** Personal multi-agent assistant (Telegram-fronted)
**Scope of this document:** the Notes agent only — capture, update, query, and render.
**Status:** ready to build. Phases 1–5 defined with acceptance criteria.

---

## 1. Context

The wider system is a Telegram-fronted personal assistant. A central router classifies each
inbound message and dispatches to one of several agents: **notes**, tasks/calendar, a job
application pipeline, and recall. This document specifies the **notes agent**, which is being
built first because it is the smallest slice that exercises the full stack — transport, async
queue, persistence, structured LLM output, retrieval, and human-in-the-loop confirmation.

The router itself is stubbed for now (see §11). Everything in this spec assumes messages
arrive already destined for the notes agent.

### 1.1 Core design decisions (already settled — do not revisit)

| # | Decision | Rationale |
|---|---|---|
| D1 | **Postgres is the source of truth. Google Docs is a rendered projection.** Nothing ever reads from a Doc. | Prose in a Doc cannot be reliably updated or queried. Re-render from rows instead. |
| D2 | **Deadlines are a property (`due_at`), not a category.** | An office deadline is still office work. "Deadlines" becomes a cross-category view. |
| D3 | **`body` is immutable.** Updates change status, due date, tags, title — never the original captured text. | Preserves an audit trail and prevents LLM drift from destroying the original input. |
| D4 | **Categories are a fixed enum.** | Free-form categories produce `learning` / `self-learning` / `study` sprawl within a week. |
| D5 | **Only `services/entries.py` writes to the `entries` table.** | Single choke point for debugging bad mutations. |
| D6 | **The agent core is transport-agnostic.** Telegram lives behind an adapter. | Enables a future web UI without a rewrite. |
| D7 | **Every LLM call returns a validated Pydantic model.** Never parse free text. | Turns the model into an unreliable function with a typed signature. |
| D8 | **Search queries a view (`searchable_items`), never the `entries` table directly.** | Later agents (calendar, job pipeline) join the view via `UNION ALL`. Search, ranking, and resolution code then never change. |
| D9 | **Ranking includes a recency term from day one**, even at low weight. | Entity resolution degrades with volume — "the Intuit thing" is unambiguous at 20 entries, hopeless at 2000. Retrofitting scoring after threshold tuning means retuning everything. |
| D10 | **Every job payload carries a `conversation_id`**, unused for now. | When short-term memory lands, only the Redis layer gets written — no threading an ID through six call sites. |

### 1.2 Non-goals for this phase

- Reminders / notification scheduling (deferred to v2 — see §12)
- The central router's real implementation
- Any other agent (tasks/calendar, job pipeline, recall)
- Multi-user support (single whitelisted user only)
- Voice notes, images, file attachments

---

## 2. Requirements

### 2.1 Functional

**Capture**

- **FR-1** — A free-text Telegram message is converted into a structured entry with
  `category`, `kind`, `title`, `tags`, and optional `due_at`.
- **FR-2** — The original message text is stored verbatim as `body` and never modified.
- **FR-3** — The bot echoes back its interpretation so the user can spot misclassification
  immediately.
- **FR-4** — Relative dates ("tomorrow", "next Friday", "in 3 days") resolve correctly against
  the user's timezone (`Asia/Kolkata`).

**Update**

- **FR-5** — A message expressing an update ("mark the Intuit follow-up as done") resolves to a
  specific existing entry.
- **FR-6** — When the target is ambiguous, the bot presents up to 3 candidates as inline
  keyboard buttons plus a "None of these" option, and waits.
- **FR-7** — When the target is unambiguous, the mutation applies immediately and the reply
  carries an **Undo** button.
- **FR-8** — Supported operations: `complete`, `reopen`, `set_due`, `add_tag`, `retitle`,
  `archive`.
- **FR-9** — Every mutation writes a row to `entry_events` with a JSON diff.

**Query**

- **FR-10** — Natural-language questions over stored entries ("what's open in office work",
  "what did I finish this week", "notes about JWT").
- **FR-11** — Retrieval is hybrid: full-text (`tsvector`) + semantic (`pgvector`), fused.
- **FR-12** — Structured filters (category, status, due window) are applied as SQL predicates,
  not left to the model.
- **FR-13** — Answers cite the entries they drew from, so the user can verify.

**Render**

- **FR-14** — Each category renders to its own Google Doc, rebuilt from Postgres.
- **FR-15** — Re-render is debounced (≥60s) and idempotent.

**Operational**

- **FR-16** — Only the whitelisted `chat_id` is served. All other messages are dropped silently.
- **FR-17** — Duplicate Telegram `update_id` values are ignored (idempotency).

### 2.2 Non-functional

| ID | Requirement |
|---|---|
| NFR-1 | Telegram webhook returns 200 within **2s**, always. Work happens in a background worker. |
| NFR-2 | Capture round-trip (message sent → confirmation received) p95 **< 5s**. |
| NFR-3 | A worker crash mid-task must not lose the message. Queue jobs are durable and retried. |
| NFR-4 | Every LLM call is traced with input, output, latency, token cost. |
| NFR-5 | Intent classification accuracy **≥ 90%** on the eval set before Phase 5 begins. |
| NFR-6 | Category extraction accuracy **≥ 85%** on the eval set. |
| NFR-7 | Secrets come from environment only. Never committed, never logged. |
| NFR-8 | A failed LLM schema validation retries once with the error appended, then falls back to asking the user. |

---

## 3. Architecture

### 3.1 Components

```
Telegram  ──webhook──▶  FastAPI (transport/telegram)
                             │  validate, dedupe, enqueue, return 200
                             ▼
                        Redis (arq queue)
                             │
                             ▼
                        Worker (agents/notes)
                             │
              ┌──────────────┼──────────────┐
              ▼              ▼              ▼
          capture.py     resolve.py      query.py
              │              │              │
              └──────────────┼──────────────┘
                             ▼
                   services/entries.py  ◀── the only DB writer
                   services/search.py
                   services/llm.py
                             │
                             ▼
                   Postgres + pgvector
                             │
                             ▼
                   services/docs.py ──▶ Google Docs (write-only projection)
```

### 3.2 Boundaries

- Nothing under `core/`, `agents/`, or `services/` imports `aiogram` or any Telegram symbol.
- The transport adapter exposes exactly one entry point:
  `async def handle_message(user_id: int, text: str, msg_id: int) -> Reply`
- `Reply` is a transport-neutral object: `text`, optional `buttons: list[Button]`,
  optional `attachments`. The Telegram adapter translates it into an aiogram call.

### 3.3 Message lifecycle

1. Webhook receives update. Verify secret token header. Check `chat_id` whitelist.
2. Check `processed_updates` for `update_id`. If present, return 200 and stop.
3. Enqueue job. Return 200. **Elapsed: <100ms.**
4. Worker immediately sends "…" placeholder message, records its `message_id`.
5. Worker runs the agent. On each stage boundary it **edits** that same message rather than
   sending new ones.
6. Final result replaces the placeholder text, with buttons attached if needed.
7. On unhandled exception: edit placeholder to an error message, log the trace, do not retry
   LLM-side failures automatically (avoid burning credits on a poison message).

---

### 3.4 Job payload

Every enqueued job carries this envelope. `conversation_id` is threaded through now and left
unused (D10) — it is the seam where short-term memory attaches later.

```python
class JobPayload(BaseModel):
    update_id: int
    user_id: int
    chat_id: int
    msg_id: int
    text: str
    conversation_id: str      # D10 — derived from chat_id for now; unused downstream
    enqueued_at: datetime
```

`conversation_id` is currently just `f"tg:{chat_id}"`. When conversation memory lands it becomes
the Redis key prefix for the turn buffer, and the only code that changes is the classifier's
prompt assembly. Pass it into `agents/notes/__init__.py:handle()` and let it sit unused rather
than adding the parameter later.

---

## 4. Data model

```sql
create extension if not exists vector;
create extension if not exists pg_trgm;

create type entry_category as enum ('office_work', 'self_learning', 'personal', 'ideas');
create type entry_kind     as enum ('note', 'task');
create type entry_status   as enum ('open', 'done', 'archived');
create type event_type     as enum ('created','updated','completed','reopened','archived');

create table entries (
  id            uuid primary key default gen_random_uuid(),
  user_id       bigint not null,
  category      entry_category not null,
  kind          entry_kind not null,
  title         text not null,
  body          text not null,
  status        entry_status not null default 'open',
  due_at        timestamptz,
  tags          text[] not null default '{}',
  embedding     vector(1024),
  source_msg_id bigint,
  created_at    timestamptz not null default now(),
  updated_at    timestamptz not null default now(),

  search_tsv tsvector generated always as (
    setweight(to_tsvector('english', coalesce(title, '')), 'A') ||
    setweight(to_tsvector('english', coalesce(body,  '')), 'B')
  ) stored
);

create index entries_tsv_idx    on entries using gin  (search_tsv);
create index entries_embed_idx  on entries using hnsw (embedding vector_cosine_ops);
create index entries_lookup_idx on entries (user_id, status, category, due_at);
create index entries_tags_idx   on entries using gin  (tags);

create table entry_events (
  id            bigserial primary key,
  entry_id      uuid not null references entries(id) on delete cascade,
  event         event_type not null,
  changes       jsonb not null default '{}',
  source_msg_id bigint,
  created_at    timestamptz not null default now()
);

create index entry_events_entry_idx on entry_events (entry_id, created_at desc);

create table processed_updates (
  update_id  bigint primary key,
  created_at timestamptz not null default now()
);

-- D8: the search surface. Today it wraps one table. Later it UNIONs several.
-- services/search.py queries THIS, never `entries` directly.
create view searchable_items as
select
  id,
  user_id,
  'notes'::text as source,
  title,
  body,
  embedding,
  search_tsv,
  status::text as status,
  category::text as category,
  due_at,
  created_at,
  updated_at
from entries
where status <> 'archived';
```

**Notes on the schema**

- `embedding` dimension is `1024` assuming Voyage `voyage-3-lite`. Change to `1536` for OpenAI
  `text-embedding-3-small`. Keep it in one config constant, not scattered across migrations.
- `status` is only semantically meaningful for `kind = 'task'`, but notes can still be archived.
  No check constraint — keeping it permissive is simpler than modelling the exception.
- `entry_events.changes` holds `{"field": {"from": x, "to": y}}`. This is what powers "what did
  I finish this week" and every post-mortem on a bad mutation.
- Deadlines view: `where due_at is not null and status = 'open' order by due_at`.

---

## 5. LLM contracts

All models live in `core/schemas.py`. All calls go through `services/llm.py`, which wraps
structured output, validation, one retry with the validation error appended, tracing, and
timeout.

```python
class IntentResult(BaseModel):
    action: Literal["capture", "update", "query", "unknown"]
    confidence: float = Field(ge=0, le=1)
    reasoning: str = Field(max_length=200)


class CapturedEntry(BaseModel):
    category: Category
    kind: Kind
    title: str = Field(max_length=80)      # short, human-scannable, used for matching
    tags: list[str] = Field(default_factory=list, max_length=5)
    due_at: datetime | None = None
    due_source_text: str | None = None      # the raw phrase, e.g. "next Friday" — for audit


class UpdateRequest(BaseModel):
    target_hint: str                        # the phrase identifying which entry
    operation: Literal["complete", "reopen", "set_due", "add_tag", "retitle", "archive"]
    value: str | None = None                # new due date / tag / title, if applicable


class QueryPlan(BaseModel):
    search_text: str | None = None
    category: Category | None = None
    status: Status | None = None
    due_before: datetime | None = None
    completed_after: datetime | None = None
    limit: int = 10
```

**Prompting rules**

- Always inject current datetime **and** the timezone (`Asia/Kolkata`) into any prompt that
  might resolve a relative date. Never let the model assume "now".
- Validate `due_at` after extraction: reject dates more than 2 years out or in the past by more
  than a day — those are almost always parse errors, not intent.
- Use a small/cheap model for `IntentResult` and `QueryPlan`. Use the stronger model for
  `CapturedEntry` and answer synthesis.

---

## 6. Process flows

### 6.1 Capture

```
message
  → classify intent            → action = "capture"
  → extract CapturedEntry      (LLM, strong model)
  → validate due_at
  → embed(title + "\n" + body) → vector
  → entries.create()
  → entry_events.log("created")
  → reply: "📁 office_work · task · due Fri 31 Jul
            Follow up with Intuit recruiter
            [Change category] [Delete]"
  → schedule debounced Doc re-render for that category
```

### 6.2 Update — the hard path

```
message
  → classify intent            → action = "update"
  → extract UpdateRequest      (LLM)
  → search.hybrid(target_hint, user_id, status='open')  → ranked candidates
  → resolve:
       if top.score >= AUTO_THRESHOLD and (top.score - second.score) >= GAP_THRESHOLD:
             apply immediately
             reply "✅ Marked done: <title>  [Undo]"
       elif candidates:
             reply "Which one?"  with up to 3 buttons + [None of these]
             → store pending action in Redis, keyed by callback_id, TTL 1h
             → on callback: apply, edit message to confirm
       else:
             reply "Couldn't find anything matching '<hint>'. Closest: <top 2>"
  → entry_events.log(operation)
  → schedule Doc re-render
```

Start with `AUTO_THRESHOLD = 0.75`, `GAP_THRESHOLD = 0.15` (on fused RRF scores, normalised).
**Tune these against the eval set, not against vibes.** Over-eager auto-apply is worse than an
extra tap — a wrong silent mutation destroys trust in the whole system.

### 6.3 Query

```
message
  → classify intent            → action = "query"
  → extract QueryPlan          (LLM, cheap model)
  → apply structured filters as SQL WHERE clauses
  → if search_text present: hybrid search within the filtered set
  → synthesise answer from the retrieved rows (LLM, strong model)
  → reply with answer + a compact list of the entries used
```

Filters are SQL. Only the ranking and the final phrasing are model work. This keeps "show me
everything open in office work" exact rather than approximate.

### 6.4 Hybrid search

Runs against `searchable_items` (D8), **never against `entries`**.

Reciprocal Rank Fusion over two independent rankings, plus a recency term (D9):

```
fts_rank  = ts_rank_cd(search_tsv, websearch_to_tsquery('english', q))
vec_rank  = 1 - (embedding <=> query_embedding)
recency   = exp(-age_days / RECENCY_HALFLIFE_DAYS)

score(d)  = 1/(60 + rank_fts(d)) + 1/(60 + rank_vec(d)) + RECENCY_WEIGHT * recency(d)
```

Run both rankings as CTEs in a single query, fuse in SQL, return top-k. RRF needs no score
normalisation between the two systems, which is exactly why it's the right default here.

Start with `RECENCY_WEIGHT = 0.005` and `RECENCY_HALFLIFE_DAYS = 30`. That weight is small
enough to be near-inert at low volume — it only breaks ties between otherwise equal matches.
The point is that the term **exists and is wired into the query** before thresholds get tuned;
raising the weight later is a config change rather than a rescoring exercise.

Both constants live in `config.py`. The eval suite must print scores with recency isolated so
its contribution stays visible as the corpus grows.

### 6.5 Doc rendering

- Triggered by a debounced job (60s) after any mutation to a category.
- Read all non-archived entries for that category, ordered: overdue → due soon → undated tasks
  → notes.
- Rebuild the Doc body wholesale via the Docs API batch update. Do not attempt incremental
  patching.
- Store `doc_id` per category in a small `category_docs` table; create the Doc on first render.

---

## 7. Repository layout

```
app/
  main.py                      FastAPI app, webhook route, health check
  config.py                    pydantic-settings, all env vars
  transport/
    telegram/
      adapter.py               aiogram wiring, webhook handler
      keyboards.py             inline keyboard builders
      formatting.py            Reply -> Telegram markdown + buttons
  core/
    schemas.py                 all Pydantic models (LLM contracts + Reply)
    llm.py                     structured output, retry, tracing
    router.py                  intent classification (stub for now)
  agents/
    notes/
      capture.py
      resolve.py
      query.py
      __init__.py              handle(user_id, text, msg_id) -> Reply
  services/
    entries.py                 CRUD — THE ONLY MODULE THAT WRITES entries
    search.py                  hybrid search
    embeddings.py
    docs.py                    Google Docs projection
  db/
    models.py                  SQLAlchemy models
    session.py
    migrations/                alembic
  workers/
    queue.py                   arq worker definition, job functions
evals/
  cases.jsonl
  run.py
tests/
docker-compose.yml             postgres+pgvector, redis
.env.example
```

---

## 8. Stack

| Concern | Choice | Note |
|---|---|---|
| API | FastAPI | webhook + health only |
| Telegram | aiogram 3 | async-native, good inline keyboard API |
| Queue | arq | Redis-backed, async-first, lighter than Celery |
| DB | Postgres 16 + pgvector | single store for rows, FTS, and vectors |
| ORM | SQLAlchemy 2.0 async + Alembic | |
| LLM | Anthropic API | strong model for extraction/synthesis, cheap for classification |
| Embeddings | Voyage `voyage-3-lite` | 1024-dim; swap freely, keep dim in config |
| Tracing | Langfuse (self-hosted) | required from Phase 3 onward, not optional |
| Config | pydantic-settings | |
| Local dev | docker-compose + long polling | webhook only needed in deployment |

Use **long polling** in development — no tunnel needed. Switch to webhook for deployment. The
adapter should support both behind one flag.

---

## 9. Build phases

Each phase must be working end-to-end before the next begins.

### Phase 1 — Plumbing, zero LLM

Echo bot: message → webhook → arq → worker → reply.
docker-compose with Postgres and Redis. Health check. `chat_id` whitelist middleware.
`processed_updates` dedupe.

*Acceptance:* send "hello", receive "echo: hello". Kill the worker mid-job, restart it, job
completes. Send from a different account, get nothing.

### Phase 2 — Persistence, still zero LLM

`/note <text>` inserts a row with `category='personal'`, `kind='note'`, title = first 80 chars.
`/list` returns the 10 most recent. Alembic migration for the full schema in §4.

*Acceptance:* notes survive a full stack restart. `/list` shows them.

### Phase 3 — LLM extraction

`services/llm.py` with structured output + retry + Langfuse tracing.
Plain messages (no slash command) route to capture. Bot echoes its interpretation.
Embeddings generated and stored.

*Acceptance:* "remind me to submit the timesheet by Friday" → `office_work` / `task` /
`due_at` = the correct upcoming Friday 23:59 IST. **Write the first 20 eval cases here.**

### Phase 4 — Query path

Hybrid search per §6.4. `QueryPlan` extraction. SQL filters + fused ranking + answer synthesis
with citations.

*Acceptance:* "what's open in office work" returns exactly the right rows. "notes about JWT"
finds an entry that never contained the literal string "JWT" — this proves the semantic half is
actually wired up, not just the FTS half.

### Phase 5 — Update path

`UpdateRequest` extraction, candidate resolution, threshold logic, inline keyboard
disambiguation, pending-action store in Redis, Undo, `entry_events` logging.

*Acceptance:* with three similar open entries, "mark the Intuit one done" presents a choice
rather than guessing. With one clear match, it applies and offers Undo. Undo actually reverts
and logs a second event.

### Phase 6 — Docs projection

Google OAuth (one-time, refresh token stored), `category_docs` table, debounced re-render.

*Acceptance:* mutate an entry, wait 60s, the Doc reflects it. Manually edit the Doc, trigger a
re-render, the manual edit is overwritten — confirming D1 holds.

---

## 10. Evals

Not optional, and not something to add at the end. Start at Phase 3.

`evals/cases.jsonl` — one case per line:

```json
{"id": "cap-001", "text": "submit timesheet by friday", "expect": {"action": "capture", "category": "office_work", "kind": "task", "has_due": true}}
{"id": "upd-003", "text": "the intuit follow up is done", "expect": {"action": "update", "operation": "complete"}}
{"id": "qry-002", "text": "what do i have open this week", "expect": {"action": "query", "status": "open", "has_due_filter": true}}
```

`evals/run.py` executes every case, compares against `expect`, prints a per-field accuracy
table and a confusion matrix for category. Run it on **every** prompt change.

Target ≥40 cases by Phase 5, weighted toward the messy real inputs you'd actually type on a
phone — lowercase, no punctuation, half-finished thoughts.

---

## 11. Router stub

For now, `core/router.py` sends everything to the notes agent. It exposes the interface the
real router will implement later:

```python
async def route(user_id: int, text: str) -> AgentName: ...
```

Keep the signature stable. When the other agents land, only this function's body changes.

---

## 12. Deferred to v2

- **Reminders.** Once entries carry `due_at`, a periodic arq job scans for entries due within a
  window and pushes a Telegram message. Needs a `reminded_at` column to avoid duplicates and a
  user-configurable lead time. Deliberately out of scope here — build the data layer right
  first, and reminders become a small job on top.
- **Short-term conversation memory.** Last ~5 turns per `conversation_id` in a Redis list with a
  few hours' TTL, prepended to the intent classifier prompt only. Resolves references like
  "actually tag that as urgent" immediately after a capture. Attaches at the classifier boundary
  and touches nothing else — which is precisely why it defers cleanly. Do **not** put turn
  history in Postgres; chitchat isn't worth durable storage and TTL expiry is free garbage
  collection.
- **Cross-domain recall.** "What's going on with Intuit" spanning notes, calendar events, and
  job pipeline rows. Enabled by D8 — extend `searchable_items` with `UNION ALL` branches as
  each agent lands. Requires no change to `services/search.py`.
- Recurring entries
- Sub-entries / checklists
- Multi-user support
- Voice note capture (Telegram voice → transcription → capture)

---

## 13. Environment

```
TELEGRAM_BOT_TOKEN=
TELEGRAM_ALLOWED_CHAT_IDS=      # comma-separated
TELEGRAM_WEBHOOK_SECRET=
TELEGRAM_MODE=polling           # polling | webhook

DATABASE_URL=postgresql+asyncpg://...
REDIS_URL=redis://localhost:6379

ANTHROPIC_API_KEY=
MODEL_FAST=                     # intent, query plan
MODEL_STRONG=                   # extraction, synthesis

VOYAGE_API_KEY=
EMBEDDING_DIM=1024

LANGFUSE_PUBLIC_KEY=
LANGFUSE_SECRET_KEY=
LANGFUSE_HOST=

GOOGLE_CLIENT_ID=               # phase 6
GOOGLE_CLIENT_SECRET=
GOOGLE_REFRESH_TOKEN=

USER_TIMEZONE=Asia/Kolkata

RECENCY_WEIGHT=0.005            # D9 — near-inert at low volume, raise as corpus grows
RECENCY_HALFLIFE_DAYS=30
AUTO_THRESHOLD=0.75             # §6.2 — tune against evals, not vibes
GAP_THRESHOLD=0.15
```

---

## 14. Instructions for the implementing agent

- Build **one phase at a time**. Do not scaffold ahead — no empty modules for phases not yet
  reached.
- Respect the boundaries in §3.2 and decision D5 strictly. If you find yourself writing to
  `entries` from outside `services/entries.py`, stop and refactor.
- Every LLM call goes through `services/llm.py`. No direct SDK calls in agent code.
- `services/search.py` queries `searchable_items` only (D8). A raw `from entries` in that module
  is a bug even though it would work today.
- Thread `conversation_id` through the job payload and into `handle()` from Phase 1 (D10). It
  will be unused until v2. Leave it unused — do not "clean it up".
- Write the Alembic migration before the models it supports.
- At the end of each phase, state which acceptance criteria pass and which do not. Do not begin
  the next phase with a failing criterion.
- Prefer readable synchronous-looking async code over cleverness. This codebase will be read
  more than it is written.
