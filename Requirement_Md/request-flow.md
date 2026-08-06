# Request → Answer flow: what's sitting where

Traces one Telegram message through the system end to end, in the order the code actually
runs today (not the HLD's original phase order — several zero-LLM fast paths have been added
since). For each stage: which file/function owns it, and whether it costs an LLM/embedding
call or not.

## Diagram

```mermaid
flowchart TD
    A[Telegram message] --> B["transport/telegram/adapter.py\non_message → handle_update()"]
    B --> C{"_check_access()\nservices/users.py + services/rate_limit.py"}
    C -- "trial expired / suspended / rate-limited" --> C1[Reject reply, nothing enqueued]
    C -- allowed --> D["Dedupe: ProcessedUpdate insert\ndb/models.py — IntegrityError = duplicate"]
    D --> E["Enqueue job → Redis\nworkers/pool.py"]
    E --> F["arq worker picks up job\nworkers/queue.py: handle_message → _build_reply()"]

    F --> G{"/list or /note ?"}
    G -- "/list" --> G1["services/entries.py: list_recent()"]
    G -- "/note ..." --> G2["services/entries.py: create_entry()\nkind=note, no LLM"]
    G -- "plain text" --> H

    H{"kv_notes.try_handle_save()\nsave/remember trigger?"}
    H -- "no trigger" --> M
    H -- "clean 'key: value' shape" --> H1["SAVE_PATTERN regex match\nZERO LLM calls"]
    H -- "trigger present, messy phrasing" --> H2["_extract_fact()\n1x Haiku call (fact-extract)\n+ 1x Voyage embed (embed-fact)"]
    H1 --> H3["_save_fact()\nservices/entries.py: create_entry()\nkind=fact, category=personal"]
    H2 --> H3
    H3 --> Z[Reply sent]

    M["agents/notes/__init__.py: handle()"]
    M --> N{"chitchat.try_handle()\ngreeting/smalltalk?"}
    N -- match --> N1["Canned reply\nZERO LLM"]
    N -- no match --> O

    O{"kv_notes.try_handle_get()\n'what's X' trigger?"}
    O -- "no trigger" --> P
    O -- "trigger, exact fact title match" --> O1["services/entries.py: find_fact()\nZERO LLM"]
    O -- "trigger, no fact match" --> O2["agents/notes/query.py: query()\n(skips classify_intent)"]

    P["classify_intent()\n1x Haiku call (classify-intent)"]
    P --> Q{action}
    Q -- capture --> R["agents/notes/capture.py: capture()\n1x Sonnet (capture-extract)\n+ 1x Voyage (embed-capture)"]
    Q -- query --> S["agents/notes/query.py: query()\n1x Haiku (query-extract-plan)\n+ 1x Voyage (embed-query)\n+ 1x Sonnet (query-synthesize-answer)"]
    Q -- update --> T["agents/notes/resolve.py: resolve()\n1x Haiku (resolve-extract-update-request)\n+ hybrid_search for disambiguation"]
    Q -- unknown --> U["Flat fallback reply\nZERO LLM"]

    S --> S1["services/search.py: hybrid_search()\nFTS + pgvector + recency, RRF-ranked"]
    T --> S1

    R --> Z
    S --> Z
    T --> Z
    U --> Z
    O1 --> Z
    O2 --> Z
    G1 --> Z
    G2 --> Z

    Z["Reply sent to Telegram\ntransport/telegram/adapter.py: send_reply()"]
```

## Stage-by-stage table

| # | Stage | File : function | LLM / embed calls |
|---|---|---|---|
| 1 | Receive message | `transport/telegram/adapter.py:on_message` | — |
| 2 | Access control (trial/suspended/rate-limit) | `transport/telegram/adapter.py:_check_access` → `services/users.py`, `services/rate_limit.py` | — |
| 3 | Dedupe | `transport/telegram/adapter.py:handle_update` → `db/models.py:ProcessedUpdate` | — |
| 4 | Enqueue | `workers/pool.py` (Redis via arq) | — |
| 5 | `/list`, `/note` | `workers/queue.py:_build_reply` → `services/entries.py` | — |
| 6a | Save a fact, clean syntax ("save X: Y") | `services/kv_notes.py:try_handle_save` → `SAVE_PATTERN` regex | **zero** |
| 6b | Save a fact, messy phrasing (trigger word present, no clean separator) | `services/kv_notes.py:_extract_fact` | 1x Haiku (`fact-extract`) + 1x Voyage (`embed-fact`) |
| 7 | Greeting / small talk ("hi", "are you alive") | `services/chitchat.py:try_handle` | **zero** |
| 8a | "what's X" — exact fact match | `services/kv_notes.py:try_handle_get` → `services/entries.py:find_fact` | **zero** |
| 8b | "what's X" — no fact match | `agents/notes/query.py:query` (classify_intent skipped) | see row 10 |
| 9 | Intent classification (everything else) | `agents/notes/__init__.py:classify_intent` | 1x Haiku (`classify-intent`) |
| 10a | capture | `agents/notes/capture.py:capture` | 1x Sonnet (`capture-extract`) + 1x Voyage (`embed-capture`) |
| 10b | query | `agents/notes/query.py:query` | 1x Haiku (`query-extract-plan`) + 1x Voyage (`embed-query`) + 1x Sonnet (`query-synthesize-answer`) |
| 10c | update | `agents/notes/resolve.py:resolve` | 1x Haiku (`resolve-extract-update-request`) |
| 10d | unknown (not caught by chitchat) | flat fallback string | **zero** |
| 11 | Search (used by query + update) | `services/search.py:hybrid_search` | — (reads embeddings already computed) |
| 12 | Send reply | `transport/telegram/adapter.py:send_reply` | — |

## Where storage actually lives

- **`entries` table** (`db/models.py:Entry`) — the single store for everything: notes, tasks,
  *and* facts (`EntryKind.fact`, added this session — `user_kv_notes` no longer exists).
  `services/entries.py` is the only writer (D5).
- **`usage_records`** — every LLM/embed call in the table above writes one row here
  (`services/usage.py`), which is how the "zero calls" claims above are actually verified,
  not just asserted.
- **Redis** — the arq job queue (`workers/pool.py`) and rate-limit counters
  (`services/rate_limit.py`); nothing durable.

## Reading this against the context-engineering doc

Rows 6a, 7, and 8a are the fast paths added this session specifically to avoid paying stage
9's fixed classify-intent overhead (Module 1's "fixed overhead dwarfs user content" point) for
message shapes that are cheap to recognize deterministically. Everything that reaches stage 9
still pays the full multi-call pipeline described in `context-engineering-learning-path.md` —
those fast paths reduce *how often* you reach that pipeline, not its per-call cost; Modules
2-6 of that doc are what shrink the pipeline itself.
