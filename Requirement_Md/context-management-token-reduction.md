# Context Management — Reducing LLM Tokenization

**Companion to:** `notes-agent-hld.md`, `notes-agent-lld.md`
**Scope:** every `AsyncAnthropic` / Voyage call this repo makes (`services/llm.py:call_structured`,
`services/embeddings.py:embed`) — not Claude Code's own context, the app's own LLM usage.
**Status:** analysis + proposed plan, nothing built yet.

---

## 1. How many LLM calls one message actually costs today

Every inbound message goes through `agents/notes/__init__.py:handle()`, which always calls
`classify_intent()` first, then dispatches:

| Intent | LLM calls | Where |
|---|---|---|
| `capture` | 2 — `classify_intent` (haiku) → `extract` (sonnet) | [capture.py:39-46](../agents/notes/capture.py#L39-L46) |
| `query` | 3 — `classify_intent` (haiku) → `extract_plan` (haiku) → synthesis (sonnet) | [query.py:38-45](../agents/notes/query.py#L38-L45), [query.py:79-92](../agents/notes/query.py#L79-L92) |
| `update` | 2 — `classify_intent` (haiku) → `extract_update_request` (haiku) | [resolve.py:57-64](../agents/notes/resolve.py#L57-L64) |
| `unknown` | 1 — `classify_intent` only | — |

Every one of these is a separate `messages.create` call through
[services/llm.py:call_structured](../services/llm.py#L31-L113): one `system` string built fresh
per call, one `tools=[...]` entry built fresh per call (`response_model.model_json_schema()`),
one user message. On a validation failure it retries once with the *entire* first turn
(system + tools + assistant's tool_use + a tool_result error) resent — see
[services/llm.py:97-110](../services/llm.py#L97-L110).

None of this is wrong — it matches the HLD's "every LLM call returns a validated Pydantic
model" rule (D7) and the phased capture/query/resolve split is deliberate (§14, one phase at a
time). The finding below is narrower: **nothing in this pipeline uses Anthropic prompt
caching**, and that's the single biggest lever available without touching accuracy or
architecture at all.

---

## 2. Findings, ranked by impact

### 2.1 No `cache_control` anywhere — the #1 lever, zero accuracy risk

Anthropic bills a cached prefix hit at a fraction of normal input-token cost (cache reads are
billed far below the normal input rate; the first call in a window pays a small write premium).
Two things in this codebase are **100% identical across repeated calls** and currently paid for
in full, every single time:

1. **The tool schema.** `response_model.model_json_schema()` in
   [services/llm.py:44-48](../services/llm.py#L44-L48) never changes for a given
   `response_model` — `IntentResult`'s schema on call #1 is byte-identical to its schema on
   call #10,000. It carries no `cache_control` today.
2. **The static instructional portion of every system prompt.** Look at
   [capture.py:_extraction_system_prompt](../agents/notes/capture.py#L19-L36),
   [query.py:_query_plan_system_prompt](../agents/notes/query.py#L19-L35), and
   [resolve.py:_update_request_system_prompt](../agents/notes/resolve.py#L38-L54): each is
   structured as *fixed instructions* + a *one-line dynamic suffix* (`Current date/time:
   {now.isoformat()}...`). The fixed part is already, by construction, a stable prefix — it's
   just never marked cacheable, and today it's rebuilt as one opaque string, so there's no
   place to put a cache boundary even if you wanted to.

Because this is a single-user personal bot (one person, bursty usage, several messages within
the same minute during a capture/query session), cache hit rates on both of these would likely
be high — the cache TTL (5 minutes by default, refreshed on every hit) comfortably spans a
back-and-forth conversation.

**This is pure upside**: same prompts, same accuracy, same schema — just billed differently.

### 2.2 Retry path doubles cost on validation failure

[services/llm.py:88-110](../services/llm.py#L88-L110): when `model_validate` fails, the next
attempt's `messages` list is the original user turn **plus** the full assistant response
(`response.content`) **plus** a tool_result error block. That's roughly 2x the input tokens for
that one job. This is inherent to Anthropic's tool-use protocol (a `tool_result` must
immediately follow a `tool_use`) and only fires on validation failure, so it's rare — but it
directly benefits from 2.1: if the system+tools prefix is cached, the retry's repeated prefix is
a cache read, not a full-price resend. No standalone fix needed beyond 2.1; noting it here so
the caching work accounts for it.

### 2.3 Three ad hoc copies of the same "current date/time" prompt fragment

`capture.py`, `query.py`, and `resolve.py` each independently build a
`f"Current date/time: {now.isoformat()} ({settings.user_timezone})..."` string
([capture.py:30](../agents/notes/capture.py#L30),
[query.py:33](../agents/notes/query.py#L33),
[resolve.py:53](../agents/notes/resolve.py#L53)). Not a token-cost problem by itself (the
information has to be sent somewhere), but it means there are three separate places that need
to agree on "this is the dynamic suffix, keep it separate from the cacheable prefix" once 2.1 is
implemented. Worth consolidating into one helper so the static/dynamic split is enforced in one
place instead of three.

### 2.4 The planned conversation-memory feature is a real future token-growth risk

The paused plan (`binary-gliding-eagle.md`, HLD §12) prepends the last ~5 turns to
`INTENT_SYSTEM_PROMPT` on **every** message, for the life of the Redis TTL (3h). Three things
worth baking in when that gets built, not after:

- **Cap each stored turn's text.** A raw long capture (a paragraph the user pasted in) stored
  verbatim would get replayed into every classify call for up to 5 turns × 3 hours. Truncate to
  something like 100-150 chars per turn before formatting, matching the spirit of `title`'s own
  80-char cap in `CapturedEntry`.
- **Store the compact summary, not the raw LLM output.** The plan already leans this way
  ("turns are stored as plain dicts... `append_turn(conversation_id, user_text, action, summary,
  entry_id=None)`") — keep it that way rather than dumping full extraction results into history.
- **Append history as its own dynamic block, after the cacheable static instructions — never
  spliced into the middle of `INTENT_SYSTEM_PROMPT`.** If history text gets interleaved into the
  static instructional prose, it breaks the cache prefix on *every single message* (history
  changes every turn by definition), silently undoing 2.1 specifically for the classifier —
  which is the highest-traffic call in the whole system (it fires on every message, including
  `unknown`).

### 2.5 Query synthesis's `rows_summary` scales with corpus size, and can't be cached (correctly)

[query.py:74-88](../agents/notes/query.py#L74-L88): the matched rows are formatted into the
synthesis system prompt, capped today by `QueryPlan.limit = 10`
([core/schemas.py:91](../core/schemas.py#L91)). This is inherently per-query dynamic data, so it
correctly does *not* belong in a cached prefix — flagging only so that if `limit` is ever raised
as the corpus grows, it's understood as a real (if minor) per-query cost driver, not something
caching will help with.

### 2.6 Model choice and `max_tokens` — not tokenization levers, noted to rule them out

`model_strong` (Sonnet) is used for `capture.extract` and `query`'s synthesis
([app/config.py:30-31](../app/config.py#L30-L31)) — a cost lever (Sonnet vs Haiku pricing) but
not a *token-count* lever; same tokens, different $/token. Separately, `max_tokens=1024` is
fixed for every call regardless of `response_model` size
([services/llm.py:62](../services/llm.py#L62)) — this only caps possible output length and
isn't billed unless actually generated, so it's not a cost lever at all. Both noted here so
they're not mistaken for tokenization fixes; out of scope for this plan.

---

## 3. Plan

### Design decisions

**Caching is additive to `services/llm.py:call_structured`, not a parallel code path.** Every
call site already goes through this one function (D7 in the HLD: "no direct SDK calls in agent
code") — the fix belongs there once, not in each of capture/query/resolve.

**The static/dynamic split becomes an explicit parameter, not a string-concatenation
convention.** Today each `_*_system_prompt()` function returns one opaque string. Splitting it
into `system_static: str` (the instructions — identical across calls) and `system_dynamic: str`
(the "Current date/time..." suffix, or the history block once §2.4 lands) makes the cache
boundary a real code seam instead of something that only holds by convention and silently
breaks if someone edits a prompt carelessly later.

**Tool schema caching is unconditional** — `response_model.model_json_schema()` never varies
per response_model, so every call site gets `cache_control` on its tool entry with no
call-site changes needed beyond `call_structured` itself.

**No change to retry behavior (§2.2).** It already benefits from caching once the prefix is
marked cacheable; adding separate logic to shrink the retry payload would be solving a problem
caching already solves, and would fight Anthropic's tool_use/tool_result pairing requirement for
no gain.

### Files touched

```
services/llm.py          call_structured() signature: system str -> (system_static: str,
                          system_dynamic: str = ""); build system as a list of two content
                          blocks with cache_control on the static one; add cache_control to
                          the tools[0] entry; log/record cache_creation_input_tokens and
                          cache_read_input_tokens from response.usage into the Langfuse
                          generation (alongside the existing input/output counts) so cache
                          hit rate is actually observable, not assumed
agents/notes/__init__.py classify_intent(): split INTENT_SYSTEM_PROMPT (fully static today —
                          system_dynamic="" until conversation memory lands)
agents/notes/capture.py  _extraction_system_prompt() -> return (static, dynamic) instead of
                          one string
agents/notes/query.py    _query_plan_system_prompt() -> same split; synthesis system prompt
                          in query() also split (static instructions vs the per-query
                          rows_summary, which is correctly the dynamic half here)
agents/notes/resolve.py  _update_request_system_prompt() -> same split
(new) services/prompting.py   current_time_suffix() -> the one shared "Current date/time:
                          {iso} ({tz})..." string builder, replacing the three duplicated
                          copies (§2.3)
```

### Build steps (each tested before the next)

**Step 1 — Shared dynamic-suffix helper**
- Add `services/prompting.py:current_time_suffix()`; replace the three duplicated builders in
  capture.py/query.py/resolve.py with calls to it.
- Test: existing capture/query/update flows produce byte-identical prompts to before (pure
  refactor, no behavior change) — confirm via the eval harness (`evals/run.py`).

**Step 2 — Split `call_structured`'s `system` param, no caching yet**
- Change the signature to `system_static: str, system_dynamic: str = ""`; internally
  concatenate them exactly as today (`system_static + system_dynamic`) when calling the API —
  this step is pure plumbing, output must be identical to pre-change behavior.
- Update all 4 call sites to pass the split.
- Test: same as Step 1 — no observable change, just confirms the plumbing didn't break
  anything before caching is turned on.

**Step 3 — Turn on caching**
- In `call_structured`, build `system` as
  `[{"type": "text", "text": system_static, "cache_control": {"type": "ephemeral"}}, {"type": "text", "text": system_dynamic}]`
  when `system_dynamic` is non-empty, or just the single cached block when it's empty.
  Add `"cache_control": {"type": "ephemeral"}` to the `tool` dict.
- Read `response.usage.cache_creation_input_tokens` / `cache_read_input_tokens` (fields the
  Anthropic SDK returns alongside `input_tokens`/`output_tokens`) and pass them into
  `generation.end(usage_details={...})` next to the existing two.
- Test directly: send two `capture` messages back-to-back (same `trace_name="capture-extract"`
  path) and confirm in Langfuse that the second call's `cache_read_input_tokens` is > 0 and
  noticeably larger than `input_tokens`. Repeat for `classify-intent` (the highest-traffic
  call) and `resolve-extract-update-request`.

**Step 4 — Confirm no regression across all 4 intents**
- Run the eval harness end-to-end (capture, query, update, unknown) and confirm classification
  accuracy / extraction correctness is unchanged — caching only changes billing, never model
  behavior, so any diff here means a bug in the split (e.g. a stray dynamic-looking sentence
  left in the "static" half, or vice versa).

### Verification

Compare Langfuse traces from before this change to after, for a normal multi-message session
(a few captures, a query, an update). Expect: `cache_read_input_tokens` > 0 on every call after
the first of each `trace_name` within the 5-minute cache window, and total billed input tokens
for the session measurably lower than the pre-change baseline — the exact percentage depends on
how much of each prompt is the static instructional block vs the per-message user text, but the
tool-schema portion alone (sent in full on literally every call today) is pure savings from
call #2 onward.

### Explicitly not built here

- **Merging `classify_intent` into each flow's own extraction call** (one structured call
  returning intent + parsed fields together) would cut a whole round trip for `capture` and
  `update`, but it works against the HLD/LLD's deliberate separation of "is this even
  notes-related" from "what does this mean," and raises real accuracy risk (a harder,
  conditional schema for the model to fill correctly, especially for borderline/`unknown`
  messages). Worth revisiting only if Langfuse data after Step 3 shows classify+extract
  round-trip latency/cost is still the dominant cost once caching is in — not a default
  recommendation.
- **Switching `model_strong` calls to `model_fast`** (§2.6) — a cost/accuracy trade-off
  unrelated to tokenization, not part of this plan.
- **The conversation-memory feature itself** (HLD §12) — not being built here; §2.4 above is
  guidance to fold into that plan when it's picked back up, not new scope for this one.
