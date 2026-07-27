# Notes Agent — Low-Level Design (as built)

**Companion to:** `notes-agent-hld.md` (the spec this was built against)
**Status:** Phases 1–5 complete and committed. Phase 6 (Docs projection + Calendar sync)
planned but paused before any code was written — see §11.
**Purpose of this document:** describe what actually exists in the repository today — real
schemas, real function signatures, real config keys, real bugs found and fixed — as opposed
to the HLD, which describes what was *intended*. Where the two disagree, this document says
so explicitly and explains why.

---

## 1. What this system actually does today

A Telegram bot, backed by Postgres + Redis + Anthropic/Voyage/Langfuse, that:

- **Captures** free-text messages as structured notes/tasks (category, kind, title, tags,
  due date), using an LLM for classification and extraction, plus a real embedding for later
  semantic search.
- **Answers questions** about those notes/tasks using hybrid search (full-text + vector,
  fused) and an LLM-written synthesis with a deterministic citation list.
- **Resolves and applies updates** ("mark the Intuit one done") by finding the right entry —
  auto-applying when confident, asking via inline-keyboard buttons when not — with a working
  Undo, and a full audit trail of every mutation.
- Two deterministic slash commands, `/note <text>` and `/list`, bypass the LLM entirely for
  simple, fast, guaranteed-correct capture/listing.

Everything runs as two local Python processes (a FastAPI+aiogram polling process, and an arq
worker) plus two Docker containers (Postgres with pgvector, Redis). No public URL, no
deployment — this is a local personal tool.

---

## 2. Architecture — as built

```
 Telegram (long-polling — no public URL needed)
        │
        ▼
 uvicorn process: app/main.py
   ├─ FastAPI /health, /telegram/webhook (unused in current polling mode)
   └─ transport/telegram/adapter.py
        ├─ on_message()        -> handle_update()   -> whitelist, dedupe, enqueue
        └─ on_callback()       -> handle_callback_query() -> whitelist, dedupe,
                                    answer_callback_query(), enqueue
        │
        ▼
 Redis (arq queue)
        │
        ▼
 arq worker process: workers/queue.py
   ├─ handle_message   -> /note, /list (deterministic) OR agents.notes.handle() (LLM path)
   └─ handle_callback  -> fetches pending_actions token, applies via agents/notes/resolve.py,
                           edits the original Telegram message
        │
        ▼
 agents/notes/__init__.py:handle()
   classify_intent() -> capture | query | update | unknown
        │            │           │
        ▼            ▼           ▼
   capture.py    query.py    resolve.py
        │            │           │
        └────────────┴───────────┘
                     │
                     ▼
      services/{llm, embeddings, entries, search, pending_actions}.py
                     │
                     ▼
           Postgres (pgvector) + Redis (pending-action tokens)
```

Two processes, not one, is deliberate: the webhook/polling side must always respond fast
(NFR-1), so real work — LLM calls, embeddings, DB writes — happens in the worker, off that
path (NFR-3: a worker crash can't lose a message, because the job lives durably in Redis).

---

## 3. Repo layout (what exists today, not the HLD's full target-state tree)

| Path | Role |
|---|---|
| `app/config.py` | Single `pydantic-settings` object. Every secret/tunable lives here, nothing hardcoded elsewhere. |
| `app/main.py` | FastAPI app; `/health`; starts the aiogram polling loop as a background task in `TELEGRAM_MODE=polling`. |
| `app/logging_config.py` | One shared logging setup, called idempotently from every entrypoint. |
| `core/schemas.py` | Every Pydantic model in the system: transport contracts (`JobPayload`, `CallbackPayload`, `Reply`, `Button`) and LLM contracts (`IntentResult`, `CapturedEntry`, `QueryPlan`, `AnswerSynthesis`, `UpdateRequest`) — plus the domain enums (`EntryCategory`, `EntryKind`, `EntryStatus`, `EventType`), which live here (not `db/models.py`) so persistence depends on domain vocabulary, not the other way round. |
| `db/models.py` | SQLAlchemy ORM models: `ProcessedUpdate`, `Entry`, `EntryEvent`. Imports its enums from `core.schemas`. |
| `db/session.py` | Async engine + `async_session()` factory. |
| `db/migrations/` | Alembic. Three migrations so far (see §4). |
| `transport/telegram/adapter.py` | The **only** module that imports `aiogram`. Whitelist/dedupe/enqueue for both messages and callback presses; `send_reply()`/`edit_reply()` are the only things that ever call the Telegram API to talk back. |
| `workers/pool.py` | Producer-side Redis pool (`get_pool()`), used by the adapter to enqueue jobs and by `services/pending_actions.py` to store tokens. |
| `workers/queue.py` | arq `WorkerSettings`; `handle_message` and `handle_callback` job functions. |
| `services/llm.py` | The **only** module that calls the Anthropic API. Structured output via forced tool-use, one retry with the validation error appended, Langfuse tracing, timeout. |
| `services/embeddings.py` | The only module that calls Voyage. `embed(text) -> list[float]`, 512-dim. |
| `services/entries.py` | The **only** module that writes to `entries` (D5) — `create_entry` plus six FR-8 mutation operations, a generic `revert_entry` for Undo, all logging their own `entry_events` row. |
| `services/search.py` | The only module that queries `searchable_items` (D8) — hybrid RRF search, never `entries` directly. |
| `services/pending_actions.py` | Redis-backed short-token store for disambiguation choices and Undo, since Telegram's `callback_data` is capped at 64 bytes. |
| `agents/notes/__init__.py` | `handle(user_id, text, msg_id) -> Reply` — classifies intent, dispatches to the three flow modules below. |
| `agents/notes/capture.py` | Extraction (Sonnet) → embed → `services/entries.create_entry()` → formatted confirmation. |
| `agents/notes/query.py` | `QueryPlan` extraction (Haiku) → `services/search.hybrid_search()` → answer synthesis (Sonnet) → deterministic source list. |
| `agents/notes/resolve.py` | `UpdateRequest` extraction (Haiku) → candidate resolution (normalized RRF scores) → immediate-apply-with-Undo or disambiguation-with-buttons. |
| `evals/cases.jsonl`, `evals/run.py` | 20 hand-written cases; checks per-field accuracy across all three flows (action, category, kind, has_due, status, has_due_filter, operation) plus a category confusion matrix. |

Not present, and deliberately so: `core/router.py` (no second agent exists yet to route
between), `services/docs.py` / `services/calendar_sync.py` (Phase 6, paused).

---

## 4. Data model — actual schema (three migrations, applied in order)

### `processed_updates` — FR-17 dedupe
```sql
update_id  bigint primary key   -- NOT auto-incrementing — this is Telegram's own id
created_at timestamptz not null default now()
```
`update_id` has no `nextval()` default — the first migration originally generated one via
Alembic autogenerate's default heuristic for single-integer-PK columns, which was wrong
(Telegram assigns this value, we never should) and had to be fixed with an explicit
`autoincrement=False` before it was ever relied on.

### `entries` — the source of truth (D1)
```sql
id            uuid primary key default gen_random_uuid()
user_id       bigint not null
category      entry_category not null   -- office_work | self_learning | personal | ideas
kind          entry_kind not null       -- note | task
title         text not null
body          text not null             -- D3: immutable after creation, always
status        entry_status not null default 'open'   -- open | done | archived
due_at        timestamptz
tags          text[] not null default '{}'
embedding     vector(512)               -- see note below: HLD assumed 1024
source_msg_id bigint
created_at    timestamptz not null default now()
updated_at    timestamptz not null default now()
search_tsv    tsvector generated always as (...) stored   -- title weight A, body weight B

indexes: gin(search_tsv), hnsw(embedding vector_cosine_ops), btree(user_id,status,category,due_at), gin(tags)
```

**Real deviation from the HLD**: §4 says `vector(1024)` "assuming Voyage voyage-3-lite."
That assumption is factually wrong — `voyage-3-lite`'s actual API only accepts a fixed
512-dimensional output (confirmed against the live API; requesting 1024 is rejected outright
with `InvalidRequestError`). Fixed via a dedicated migration
(`d9f4f924077e_fix_embedding_dimension_to_512...`) that drops and recreates the dependent
view and HNSW index around the column-type change. `app/config.py`'s `embedding_dim` is the
single source of truth for this number, exactly as the HLD intended — just corrected to the
number that's actually true.

### `entry_events` — audit trail (FR-9)
```sql
id            bigserial primary key
entry_id      uuid not null references entries(id) on delete cascade
event         event_type not null   -- created | updated | completed | reopened | archived
changes       jsonb not null default '{}'   -- {"field": {"from": x, "to": y}, ...}
source_msg_id bigint
created_at    timestamptz not null default now()

index: btree(entry_id, created_at)   -- ascending, not DESC as the HLD's literal SQL shows;
                                     -- a deliberate, documented simplification since nothing
                                     -- reads this table's ordering yet
```
Every single mutation — including the original `create_entry()` — writes one of these.
`revert_entry()` (Undo) is itself a mutation and gets its own row too, which is precisely
what Phase 5's third acceptance criterion checks.

### `searchable_items` — the only thing `services/search.py` is allowed to query (D8)
```sql
create view searchable_items as
select id, user_id, 'notes'::text as source, title, body, embedding, search_tsv,
       status::text, category::text, due_at, created_at, updated_at
from entries
where status <> 'archived'
```

---

## 5. LLM contracts — actual Pydantic models (`core/schemas.py`)

```python
class IntentResult(BaseModel):
    action: Literal["capture", "update", "query", "unknown"]
    confidence: float = Field(ge=0, le=1)
    reasoning: str = Field(max_length=200)   # this 200-char cap gets hit by Haiku often
                                              # enough that the retry-on-validation-failure
                                              # path (NFR-8) is exercised constantly in
                                              # practice, not just in theory

class CapturedEntry(BaseModel):
    category: EntryCategory
    kind: EntryKind
    title: str = Field(max_length=80)
    tags: list[str] = Field(default_factory=list, max_length=5)
    due_at: datetime | None = None
    due_source_text: str | None = None
    # field_validator on due_at: rejects >2 years future / >1 day past — these are almost
    # always model parse errors, not real intent, and rejecting them triggers the one retry

class QueryPlan(BaseModel):
    search_text: str | None = None
    category: EntryCategory | None = None
    status: EntryStatus | None = None
    due_before: datetime | None = None
    completed_after: datetime | None = None
    limit: int = 10

class AnswerSynthesis(BaseModel):
    answer: str = Field(max_length=500)   # deliberately just the prose — the entry list is
                                           # always rendered separately, deterministically,
                                           # never left to the model's own formatting

class UpdateRequest(BaseModel):
    target_hint: str
    operation: Literal["complete", "reopen", "set_due", "add_tag", "retitle", "archive"]
    value: str | None = None   # ISO datetime string for set_due — the model resolves
                                # "next Friday" itself, since this field is plain str per
                                # the HLD, not a typed datetime like CapturedEntry.due_at
```

Every one of these is produced by `services/llm.py:call_structured()` via **forced
Anthropic tool-use** (a single tool whose `input_schema` is the model's own
`model_json_schema()`, `tool_choice={"type": "tool", "name": ...}`) — never free-text
parsing. On a `ValidationError`, the conversation gets a `tool_result` block (not a plain
text message — Anthropic's API rejects a bare text follow-up after a `tool_use` block with a
400) containing the Pydantic error, and one retry is attempted before giving up
(`LLMValidationError`, caught by `agents/notes/__init__.py` and turned into a "could you
rephrase?" reply).

---

## 6. The three message flows, concretely

### 6.1 Capture

```
"stateless auth tokens carry an expiry claim..."
  -> classify_intent()          Haiku, forced tool-use -> IntentResult(action="capture")
  -> capture.extract()          Sonnet -> CapturedEntry
  -> embed(title + "\n" + body) Voyage voyage-3-lite -> 512-dim vector
  -> services.entries.create_entry()
       INSERT entries (...)
       INSERT entry_events (event=created, changes={field: {from: null, to: value}, ...})
       one transaction, one commit
  -> Reply: "📚 self_learning · note\n<title>"
```

The intent prompt explicitly biases toward `capture` for bare factual statements with no
"note:"/"remember" framing — an earlier version of the prompt misclassified exactly this
kind of message as `query` or `unknown` in live testing (confirmed reproducible: same input
text, two different wrong answers on two separate calls), fixed by adding an explicit
worked example and an "when genuinely unsure, prefer capture" tie-break rule.

### 6.2 Query

```
"what's open in office work"
  -> classify_intent() -> "query"
  -> query.extract_plan()   Haiku -> QueryPlan(category=office_work, status=open, search_text=None)
  -> [no search_text -> skip embedding]
  -> services.search.hybrid_search(plan, query_embedding=None)
       SQL only: WHERE user_id=... AND category=... AND status=...
       ORDER BY due_at ASC NULLS LAST, created_at DESC
  -> if zero rows: "I couldn't find anything matching that." (no synthesis call at all)
  -> else: AnswerSynthesis (Sonnet, sees only the retrieved rows, never the whole table)
  -> Reply: "<plain 1-2 sentence answer>\n\n1. <deterministically formatted entry>\n..."
```

For topic-based queries ("notes about JWT"), `search_text` triggers an embedding call and
the full RRF fusion (full-text + vector + recency, per §6.4's exact formula). A minimum
cosine-similarity floor (`min_vector_similarity`, default 0.5) was added after live testing
showed the un-floored RRF ranking would surface a genuinely unrelated entry as a citation
just because it was the least-bad of two candidates in a tiny corpus — RRF is rank-based,
not distance-based, so with few documents it ranks *everything* in the filtered set.

### 6.3 Update / resolve

```
"mark the intuit one done"
  -> classify_intent() -> "update"
  -> resolve.extract_update_request()   Haiku -> UpdateRequest(target_hint="the intuit one",
                                                                 operation="complete")
  -> embed(target_hint) -> vector
  -> _target_hint_search_text(target_hint)   "intuit OR one" style filler-stripped OR-query
                                              (see §7.2 — this fixes a real FTS bug)
  -> hybrid_search(..., status=open, apply_similarity_floor=True)
       zero rows? -> retry with apply_similarity_floor=False for a "closest 2" hint only
  -> normalize scores against MAX_POSSIBLE_SCORE (theoretical best-case fused score)
  -> top >= AUTO_THRESHOLD(0.75) and (top - second) >= GAP_THRESHOLD(0.15)?
       YES -> apply_operation() -> services.entries.<op>_entry() -> store Undo token in
              Redis (services/pending_actions.py) -> Reply with an inline "Undo" button
       NO  -> up to 3 candidates as buttons (+ "None of these") -> store one
              pending_actions token per button -> Reply, buttons only, no mutation yet
```

**Button press** (`transport/telegram/adapter.py:on_callback` -> `workers/queue.py:
handle_callback`): fetch the token's stored data from Redis, delete it (one-shot), dispatch
on its `"kind"` (`disambiguation_choice` -> apply + new Undo token; `undo` -> `revert_entry`;
`cancel` -> no-op), then `edit_reply()` the *original* Telegram message in place (never a new
message) — which is why `send_reply()` had to start returning the sent message's id, and
why `pending_actions.attach()` exists (the token is created, and its button sent, *before*
the message id is known).

---

## 7. Real bugs found and fixed during this build (not hypothetical — every one below broke
something in live testing before being caught)

1. **`autoincrement` default on `processed_updates.update_id`** — SQLAlchemy's "auto"
   heuristic silently attached a Postgres sequence to a column that must only ever hold
   Telegram's own id. Fixed by making `autoincrement=False` explicit.
2. **Anthropic tool-use retry protocol** — appending a plain-text "that didn't validate..."
   user message after a `tool_use` response is rejected by the API (400: "each tool_use
   block must have a corresponding tool_result block"). Fixed by using a proper `tool_result`
   content block referencing the original `tool_use_id`.
3. **`voyage-3-lite` embedding dimension** — HLD assumed 1024; the model is fixed at 512.
   Required an actual schema migration to fix (§4).
4. **`Decimal` vs `float`** — Postgres returns the computed `score` expression as
   `decimal.Decimal` via asyncpg, not `float`; dividing it against a Python float constant
   (`MAX_POSSIBLE_SCORE`) raised `TypeError` until made explicit with `float(...)`.
5. **Bind-parameter `::type` casts inside `sqlalchemy.text()`** — `:name::type` immediately
   adjacent is misparsed by SQLAlchemy's bind-parameter regex (raises a raw Postgres syntax
   error, since it's left un-substituted). Fixed by wrapping the parameter in parens:
   `(:name)::type`.
6. **Intent-classification bias** — bare factual statements with no explicit "note"/
   "remember" framing were misclassified as `query` or `unknown` (reproducibly — same exact
   input text, two different wrong answers across two calls). Fixed with an explicit
   worked example and a "prefer capture when unsure" rule in the system prompt.
7. **RRF ranks everything, doesn't threshold** — with a tiny corpus, the un-floored vector
   ranking surfaced clearly-irrelevant rows as query citations. Fixed with
   `min_vector_similarity`, a new (not-in-the-HLD) config constant.
8. **`websearch_to_tsquery` ANDs every word** — a colloquial `target_hint` like "the intuit
   one" produces `'intuit' & 'one'`, and since no real entry ever contains the literal word
   "one," the FTS half of the fused score went completely inert for every resolution query
   containing common filler words — leaving ranking entirely to noisy short-text vector
   similarity. Fixed with a local (to `resolve.py` only) filler-word-stripping,
   OR-joining transform — Phase 4's query path is unaffected.
9. **Disambiguation force-filling an irrelevant 3rd candidate** — a direct consequence of
   #7's floor being disabled wholesale for update-resolution (needed for the "closest 2"
   fallback message) — it also let an unrelated entry fill the 3rd button slot whenever
   fewer than 3 genuine matches existed. Fixed with a two-tier strategy: try with the
   similarity floor ON first; only retry floor-OFF if that returns literally zero rows.

Every one of these was caught by actually running the code against the real Anthropic/
Voyage/Postgres APIs during development, not by inspection — several (#2, #6, #8, #9) only
surfaced when testing the exact phrasing a real user would type, not the tidy example
sentences from the HLD itself.

---

## 8. Configuration reference (`app/config.py`, all in `.env`, gitignored)

| Setting | Default | Notes |
|---|---|---|
| `TELEGRAM_BOT_TOKEN` / `TELEGRAM_ALLOWED_CHAT_IDS` / `TELEGRAM_WEBHOOK_SECRET` / `TELEGRAM_MODE` | — / — / "" / "polling" | Whitelist is a set of ints parsed from the comma-separated string. |
| `DATABASE_URL` / `REDIS_URL` | local docker defaults | |
| `USER_TIMEZONE` | Asia/Kolkata | Injected into every prompt that resolves relative dates (FR-4). |
| `EMBEDDING_DIM` | **512** | Corrected from the HLD's assumed 1024 — see §4, §7.3. |
| `WORKER_TEST_DELAY_SECONDS` | 0 | Phase 1 durability-test aid only; always 0 in normal operation. |
| `ANTHROPIC_API_KEY`, `MODEL_FAST` (claude-haiku-4-5), `MODEL_STRONG` (claude-sonnet-5) | | |
| `VOYAGE_API_KEY` | | |
| `LANGFUSE_PUBLIC_KEY` / `LANGFUSE_SECRET_KEY` / `LANGFUSE_HOST` | | |
| `RECENCY_WEIGHT` / `RECENCY_HALFLIFE_DAYS` | 0.005 / 30 | §6.4, unchanged from HLD's suggested starting values. |
| `MIN_VECTOR_SIMILARITY` | **0.5** | Not in the HLD — added after §7.7's bug. |
| `AUTO_THRESHOLD` / `GAP_THRESHOLD` | 0.75 / 0.15 | §6.2, compared against scores normalized by `MAX_POSSIBLE_SCORE` (a constant computed in `agents/notes/resolve.py`, not config — see §6.3). |

---

## 9. Testing infrastructure

- **`evals/run.py`**: loads `evals/cases.jsonl` (20 cases spanning capture/update/query),
  runs the real classifier + real extraction against each, prints per-field accuracy and a
  category confusion matrix. Current run: 100% on every field except intent `action`
  (typically 90-100% across runs — two cases with genuinely ambiguous "learn/read X"
  phrasing occasionally flip between `capture` and `query`, which is real model variance,
  not a bug).
- Every phase's acceptance criteria were verified against the **real** external services
  (real Telegram bot, real Anthropic/Voyage API calls, real Postgres/Redis in Docker) — no
  mocking anywhere in this build. Test data was created and cleaned up directly via `psql`
  and one-off Python scripts throughout development.
- Langfuse traces every `services/llm.py` call (input, output, latency, token usage) —
  confirmed working by inspecting the live dashboard during Phase 3.

---

## 10. Local dev setup (as it actually works today)

```
docker compose up -d                                  # Postgres (pgvector) + Redis
.venv/Scripts/python -m alembic upgrade head           # 3 migrations
.venv/Scripts/python -m uvicorn app.main:app --port 8000   # polling loop + health check
.venv/Scripts/python -m arq workers.queue.WorkerSettings   # the actual work happens here
```
`.env` holds every credential; `.env.example` is the checked-in template. Two processes,
run in separate terminals (or as background tasks) — killing/restarting either is safe
(NFR-3: durable queue means a dead worker never loses a message).

---

## 11. What's not built

- **Google Docs projection** (HLD Phase 6) and **Google Calendar two-way sync** (added scope,
  user-requested, not in the HLD) — fully planned (see the plan file / conversation history
  for the detailed design: OAuth setup, `category_docs`/`calendar_sync` tables, debounced
  re-render via arq job dedup, push+poll sync with last-write-wins conflict resolution) but
  **paused before any code was written**. Nothing to roll back.
- **Gmail integration** — discussed, explicitly deferred by the user, no design committed.
- **`core/router.py`** — the cross-agent router stub. There is still only one agent (notes),
  so this would be a function with one hardcoded branch and no caller. Deferred until a
  second agent actually exists, per the HLD's own non-goals (§1.2).
- Everything in HLD §12 (reminders, short-term conversation memory, cross-domain recall,
  recurring entries, multi-user, voice capture) — explicitly v2, untouched.
