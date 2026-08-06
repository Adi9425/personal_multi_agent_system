# Context Engineering — Learning Path (target: -90% token consumption)

**Purpose of this document:** unlike `context-management-token-reduction.md` (one safe,
no-risk optimization — prompt caching), this is the full curriculum: every concept a context/AI
engineer needs, taught against this actual repo's actual code, sequenced so the reductions
compound toward a 90% cut. Some steps here are aggressive and carry real accuracy risk — each
one says so, and each one is gated behind `evals/run.py`, which already exists in this repo for
exactly this reason (§10 of the HLD: "not optional, not something to add at the end").

**Read this first, it changes how you should read everything below:** "token consumption" has
two different meanings, and conflating them is the single most common mistake people make when
they say they want to "cut tokens by X%":

| | Raw tokens | Billed tokens |
|---|---|---|
| What it measures | How many tokens actually pass through the model | What you pay for |
| Changed by caching? | No — a cached token still occupies a context-window slot and still gets attended over | Yes — a cache *read* is billed at a fraction of a normal input token |
| Changed by shorter prompts? | Yes | Yes |
| Changed by fewer calls? | Yes | Yes |

Prompt caching (Module 4) is the highest-leverage single lever for **billed** tokens and does
nothing for **raw** tokens. Schema minimization, prompt compression, and call consolidation
(Modules 1-3) are the levers for **raw** tokens, and they also lower the *billed* number even
without caching. The 90% target in this doc is a billed-token-equivalent target, reached by
stacking both kinds — the worked numbers in §11 show why that's the honest way to get there,
not a hand-wave.

---

## Module 0 — Measure before you touch anything

You cannot know if any of this worked without a number to compare against. This repo already
has the instrumentation for this — [services/llm.py:41,54](../services/llm.py#L41)
creates a Langfuse trace/generation on every single call, and
[services/llm.py:76-81](../services/llm.py#L76-L81) already records `input_tokens` and
`output_tokens` per call. Nobody has looked at this data yet.

**Concept:** you cannot optimize what you haven't profiled. In LLM systems specifically, intuition
about "which prompt is expensive" is usually wrong — the fixed tool-schema and system-prompt
overhead is almost always bigger than people expect relative to the actual user content, and you
only find that out by measuring, not by reading the prompt and guessing.

**Build step:** open the Langfuse dashboard for this project (host is
`settings.langfuse_host`, [app/config.py:37](../app/config.py#L37)) and, for a day of real
usage, pull: total input tokens and output tokens grouped by `trace_name` (`classify-intent`,
`capture-extract`, `query-extract-plan`, `query-synthesize-answer`,
`resolve-extract-update-request`). This is your baseline. Every later module's "expected
impact" number should be checked against this, not against the illustrative estimates in this
doc.

**Why this is module 0 and not an afterthought:** every number in §11 is *illustrative* — built
from counting words in the actual prompt strings, not from a real tokenizer run. That's on
purpose: it's enough to decide what order to attack things in, but a real context engineer
replaces illustrative numbers with measured ones before declaring victory. Treat §11 as a
hypothesis, and Module 0's dashboard as the experiment that confirms or corrects it.

---

## Module 1 — Anatomy of one LLM call (what you're actually paying for)

Every call in this system, via `call_structured`
([services/llm.py:31-113](../services/llm.py#L31-L113)), is billed for exactly these
components, every single time, with nothing shared between calls today:

1. **System prompt** — the instructional text (`INTENT_SYSTEM_PROMPT`, `_extraction_system_prompt()`, etc.)
2. **Tool definition** — `response_model.model_json_schema()`, wrapped in a `{name, description, input_schema}` dict ([services/llm.py:44-48](../services/llm.py#L44-L48))
3. **Tool-use scaffolding** — Anthropic adds its own fixed-format overhead to every request that includes `tools` (instructions to the model about how tool-calling works) — you don't write this text, but it's real tokens billed on every call
4. **User message** — the actual thing the person typed
5. **Output** — the structured JSON the model generates back

For a short capture message ("submit timesheet by friday", ~5 words), (1)+(2)+(3) together
dwarf (4). This is the single most important fact in this whole document: **for a low-traffic,
single-user personal bot, almost all of the token cost is fixed overhead repeated on every
call, not the user's actual content.** That reframes the whole problem: you're not trying to
compress what the user typed (there's nothing to compress — it's already short), you're trying
to stop re-paying for the same boilerplate every single time.

This also tells you what *won't* help here, so you don't waste effort on it: things like
`gzip`-style text compression of the user's message, or aggressive summarization of a 5-word
capture, save near-zero tokens because the user's content was never the expensive part.

---

## Module 2 — Schema minimization (the "structured output tax")

**Concept:** every framework (Pydantic included) that auto-generates JSON Schema from a model
class includes metadata the model doesn't need to produce correct output: a `"title"` for
every single field (Pydantic capitalizes the field name — `"Action"`, `"Confidence"`,
`"Reasoning"`), a top-level `"title"` for the whole schema, and (for enums/nested models)
`"$defs"` blocks with their own titles. None of this improves the model's ability to fill the
schema correctly — the field name and `"description"` (if you write one) already carry that
information. This is sometimes called the **structured-output tax**: you get reliability
(D7's whole reason for using tool-calling everywhere) at the cost of a more verbose wire format
than free-text would need — worth paying, but worth minimizing.

**Where it is in this repo:** [services/llm.py:44-48](../services/llm.py#L44-L48) calls
`response_model.model_json_schema()` directly, unmodified, for every one of the 5 response
models in `core/schemas.py`. `IntentResult`, `CapturedEntry`, `QueryPlan`, `AnswerSynthesis`,
`UpdateRequest` — five schemas, each carrying this tax, each sent in full on every call to that
flow, forever.

**Build step:** write a small schema post-processor in `services/llm.py` — strip every
`"title"` key recursively (Anthropic's tool-calling doesn't need them; the JSON key names in
`properties` already are the field names) — before putting the schema into the `tool` dict.
This is a pure token-shape change with zero behavior change: the model still receives complete
type/enum/required information, just without redundant titles.

**Test:** run `evals/run.py` before and after — per-field accuracy must be identical (this
change removes only decorative metadata, nothing semantic). Compare `input_tokens` for the same
`trace_name` before/after in Langfuse to confirm the schema actually shrank.

**Expected impact:** modest on its own (schema titles are maybe 15-25% of the schema's own
token count), but it's free (zero risk, zero accuracy trade-off) and it compounds with every
other module, so it goes first.

---

## Module 3 — Prompt compression (eval-gated)

**Concept:** system prompts written for a first working version are usually more verbose than
the model actually needs, because the person writing them erred toward over-explaining while
they didn't yet trust the model to get it right. Once you have an eval set, you can treat prompt
length as a variable to optimize against a metric, not just prose to read for clarity — cut a
sentence, rerun evals, see if accuracy holds. This is fundamentally different from Module 2:
Module 2 removes tokens with *zero* information content; this module removes tokens that *do*
carry information, betting that the model doesn't need it stated as explicitly as it's stated
today.

**Where it is in this repo:** `INTENT_SYSTEM_PROMPT`
([agents/notes/__init__.py:13-29](../agents/notes/__init__.py#L13-L29)) spends real estate on
worked examples for every intent ("e.g. ...") plus a whole paragraph on the capture/unknown
tie-break rule. Every one of those examples is a hypothesis that the model needs it to get the
classification right — some may be load-bearing (removing them breaks a real eval case), some
may not be (the model would've gotten it right anyway, from the one-line intent definition
alone).

**Build step:** for each system prompt, try removing one example/clause at a time, rerun
`evals/run.py`'s per-field accuracy after each removal, and keep the removal only if accuracy
doesn't drop. This is literally an ablation study — the same methodology as pruning a neural
network, applied to prose instead of weights.

**Test:** the eval harness itself is the test. This is the one module where "does it still
pass evals" *is* the acceptance criterion, not a side check.

**Expected impact:** typically 20-40% of a hand-written system prompt turns out to be
redundant with what the model already infers from the schema + field names alone — but this
number is specific to each prompt and only Module 0's measurement + this module's ablation
process will tell you the real one for this repo's prompts.

---

## Module 4 — Prompt caching (billed-token lever, the one covered in depth already)

**Concept:** Anthropic lets you mark a prefix of your request (`cache_control: {"type":
"ephemeral"}` on a system or tool content block) as reusable. The first call pays a small
write premium; every subsequent call within the cache window (a few minutes, refreshed on each
hit) that sends the *identical* prefix pays roughly a tenth of the normal input-token rate for
that portion. It changes billed cost, not raw token count (Module 0's raw `input_tokens` number
in Langfuse won't move; only what you're billed for those tokens does — Anthropic reports the
cache-specific counts as *separate* usage fields, `cache_creation_input_tokens` and
`cache_read_input_tokens`, precisely so this distinction is visible).

**Prerequisite this repo doesn't have yet:** a cache boundary needs a stable prefix. Today's
system prompts interleave static instructions with a dynamic "Current date/time: ..." suffix
built inline — see `context-management-token-reduction.md` for the full split-and-cache plan
already written for this. That plan is Modules 4's concrete build steps; this doc doesn't repeat
it, just places it correctly in the sequence: **do this after Modules 2-3, not before** — you
want to cache the *already-minimized* prefix, not cache verbose text and then have to bust the
cache every time you trim it later.

**Expected impact:** this is where the big number comes from. Once the static prefix is
~10-20% of what it started as (Modules 2+3), caching it means every call after the first one in
a session pays ~10% of that already-small number. See §11 for the compounding math.

---

## Module 5 — Call consolidation (the aggressive, accuracy-risk lever)

**Concept:** every LLM call pays the fixed overhead in Module 1 once, in full, no matter how
small the actual task. If two calls' fixed overhead can be paid once instead of twice, you've
cut raw tokens by roughly half of one call's overhead — a much bigger win than trimming either
prompt, because you're eliminating a whole payment, not shrinking one.

**Where it is in this repo:** `classify_intent()` and each flow's own extraction call
(`capture.extract()`, `query.extract_plan()`, `resolve.extract_update_request()`) are always
called sequentially — see [agents/notes/__init__.py:42-71](../agents/notes/__init__.py#L42-L71).
For `capture` and `update`, that's 2 full round trips (2x the Module 1 overhead) to answer what
is, structurally, one question: "what does this message mean, and what are its fields?"

**Why this is flagged as aggressive, not a default:** the HLD/LLD deliberately separated "is
this even notes-related" (classify) from "what does this mean" (extract) as two phases built at
different times (Phase 3 vs Phases 3-5). A merged schema has to be a **discriminated union** —
one call returns `action` plus *only the fields relevant to that action*, conditionally. That's
a harder generation task for the model than four small independent schemas, and the failure
mode is worse: a wrong guess now corrupts both classification and extraction in one shot,
instead of failing at one stage where you can catch it.

**Build step (do this in a spike branch, not directly on the working system):**
1. Design one Pydantic model: `action: Literal[...]`, plus every field from `CapturedEntry`,
   `QueryPlan`, and `UpdateRequest` made `Optional`, all in one schema.
2. Point it at `model_fast` (Haiku) first — if the cheap model can already hit the accuracy bar,
   there's no reason to spend Sonnet tokens on it.
3. Run the **existing** `evals/run.py` cases against the merged call. Every `action` accuracy
   and every field-accuracy number it already reports must match or beat the two-call baseline.
4. If accuracy drops for `capture` specifically (extraction is the harder task and currently
   uses `model_strong`/Sonnet — see Module 6), keep `capture` on its own two-call path and merge
   only `update` (simpler schema, already on `model_fast` today) — a partial win is fine; this
   isn't all-or-nothing.

**Expected impact:** cuts raw tokens for whichever flows merge cleanly by roughly one full
call's worth of overhead — the single largest raw-token reduction in this document, and the
reason the 90% target is reachable at all rather than capped around 60-70% from caching alone.

---

## Module 6 — Model right-sizing and cascading (a cost lever, not a token-count lever — know the difference)

**Concept:** `model_strong` (Sonnet) vs `model_fast` (Haiku) changes $/token, not tokens/call.
Belongs in a context-engineering curriculum anyway because "reduce cost" and "reduce tokens" are
sibling goals people conflate, and knowing which lever does which is exactly the literacy this
whole document is trying to build (see the framing table at the top).

**Where it is in this repo:** [app/config.py:30-31](../app/config.py#L30-L31) —
`capture.extract()` and `query`'s synthesis step use `model_strong`; everything else already
uses `model_fast`.

**Concept extension — cascading:** a stronger pattern than picking one model per call site is
routing *within* a call site by difficulty: try the cheap model first, and only escalate to the
strong model when the cheap model's own confidence is low or validation fails. `IntentResult`
already carries a `confidence` field ([core/schemas.py:57](../core/schemas.py#L57)) that today
is recorded but never *used* — it's a natural gate: e.g. only send borderline-confidence
captures through the expensive extraction path with extra care, and fast-path the
high-confidence majority. Not something to build blindly — needs the eval set to define
"borderline" against real accuracy data, same discipline as Module 3.

**Build step:** first, using Module 0's real data, check whether `capture.extract()` on
`model_fast` alone already clears the accuracy bar in `evals/run.py`. If yes, this is a
zero-effort win (config change) before anything fancier. Only build actual confidence-gated
cascading if straight downgrade fails the evals.

**Expected impact:** $-per-token, not token count — don't count this toward the 90% raw/billed
token target in §11; track it as a separate cost-axis win.

---

## Module 7 — Retrieval / context minimization (already partly done — learn from what's here)

**Concept:** when an LLM call's input includes retrieved data (search results, DB rows,
history), the size of that retrieved context is a design choice, not a given — top-k limits,
similarity floors, and recency weighting are all ways of answering "how much retrieved context
does the model actually need to answer correctly," and over-including it wastes tokens exactly
like an unminimized schema does, just with dynamic data instead of static.

**Where it already exists in this repo — study this before building anything new:**
- `min_vector_similarity` ([app/config.py:49](../app/config.py#L49)) — a hard floor so
  `hybrid_search` (`services/search.py`) never includes vector matches below a measured
  relevance threshold, discovered empirically ("0.617 relevant vs 0.416 unrelated") rather than
  guessed.
- `QueryPlan.limit = 10` ([core/schemas.py:91](../core/schemas.py#L91)) — a hard cap on how many
  rows ever reach the synthesis prompt's `rows_summary` ([query.py:74-78](../agents/notes/query.py#L74-L78)).
- The two-tier floor-on/floor-off fallback in `resolve()`
  ([resolve.py:136-156](../agents/notes/resolve.py#L136-L156)) — floor stays on for the common
  case, and is only relaxed for the specific "closest 2" degraded-UX fallback, so the common
  path never pays for (or shows) irrelevant context.

This module's "build step" is really a **read step**: these three are worked examples of context
minimization done deliberately, with a measured reason behind each threshold, already in this
codebase. Internalize the pattern (cap it, measure the cutoff, don't guess) before applying it
anywhere new — e.g. if `limit` is ever raised as the corpus grows, re-derive the new cap the
same way `min_vector_similarity` was derived, not by picking a bigger round number.

---

## Module 8 — Memory hierarchy design (for the paused conversation-memory feature)

**Concept:** "memory" in an LLM system isn't one thing — it's a hierarchy with different
budgets and different forgetting rules at each level:

| Level | This repo's example | Budget | Forgetting rule |
|---|---|---|---|
| Working memory (this call only) | the current message's `text` | unbounded (it's the task) | never — it's the point of the call |
| Short-term / session memory | the paused conversation-memory plan (HLD §12) | last ~5 turns | TTL (a few hours) |
| Long-term memory | Postgres `entries` table | unbounded | never (explicit user action only — archive/delete) |

Each level needs its *own* token budget, because they get combined into a single prompt at call
time and none of them should be allowed to silently dominate. The long-term store is already
budget-safe by construction (it's retrieved through Module 7's minimization, never dumped whole
into a prompt). The short-term layer is the one still being designed, and it's the one at risk
of an *unbounded* budget if built carelessly — this was flagged already in
`context-management-token-reduction.md` §2.4, and restated here as the general pattern it's an
instance of: **every memory level needs an explicit, enforced token cap**, not just documentation
saying it should have one. For session memory that means storing a *compact summary* per turn
(`action`, short `summary`, `entry_id`) rather than raw text, and truncating what does get
stored — not because any single turn is expensive, but because it gets replayed into every
classify call for the rest of its TTL window, so its cost multiplies by however many messages
follow it.

**Build step:** when `services/conversation_memory.py` gets built, enforce the per-turn
character cap and the compact-summary shape *in the write path* (`append_turn()`), not as a
formatting nicety at render time — this makes the budget structural instead of a convention that
erodes.

---

## Module 9 — What doesn't apply here (judgment, not a checklist)

A real context engineer knows which techniques a given system *doesn't* need, not just the full
list. Two that are commonly reached for and don't fit this project:

- **Batch APIs** (Anthropic's Message Batches endpoint gives a further cost discount for
  asynchronous, non-latency-sensitive workloads). This bot answers a Telegram message in
  real time (NFR-1) — there is no batch of requests to accumulate. Applicable only if this
  system grows an offline/background job (e.g. bulk re-categorizing old entries), not to the
  live chat path.
- **Semantic response caching** (cache the *answer* to a previously-seen similar question, not
  just a prompt prefix). Valuable for FAQ-shaped traffic with real repetition (customer
  support bots). This is a single-user personal notes bot — the query "what's due this week"
  asked twice five minutes apart should return *different* results (the corpus changed), so
  caching the answer would return stale data. Low value here; don't build it.

Knowing to skip these is as much a part of the skillset as knowing to build Modules 1-8.

---

## 11. The compounding math (illustrative — replace with Module 0's real numbers)

Rough counts from reading the actual prompt strings in this repo (not a real tokenizer — treat
as order-of-magnitude, per Module 0's own caveat) for one `capture` message ("submit timesheet
by friday" — 2 calls today):

| Stage | classify_intent | capture.extract | Total (2 calls) |
|---|---|---|---|
| **Baseline today** | ~250 (system) + ~100 (schema) + ~10 (user) = ~360 in, ~30 out | ~300 (system) + ~150 (schema) + ~10 (user) = ~460 in, ~70 out | ~820 in + ~100 out ≈ **920 tokens** |
| **After Module 2** (strip schema titles, ~20%) | ~250 + ~80 + ~10 = ~340 | ~300 + ~120 + ~10 = ~430 | ~770 in + ~100 out ≈ **870** |
| **After Module 3** (prompt ablation, ~30% off system prose) | ~175 + ~80 + ~10 = ~265 | ~210 + ~120 + ~10 = ~340 | ~605 in + ~100 out ≈ **705** |
| **After Module 5** (merge into 1 call, if it clears evals) | — | ~1 call, roughly the larger of the two overheads, ~350 in | ~350 in + ~80 out ≈ **430** |
| **After Module 4** (cache the static prefix, 2nd+ message in a session) | cached portion billed at ~0.1x | same | static portion (~90% of the ~350 in) effectively ≈ 35 + ~35 uncached dynamic/user ≈ **70 in** + ~80 out ≈ **150 billed-equivalent** |

`920 -> ~150` on the *second and later* message of a session is an **~84% reduction**, and
that's before Module 6's model-routing cost savings are even counted (those reduce $, this table
counts tokens). The **first** message of a session doesn't get Module 4's discount (nothing
cached yet) and lands at the ~430 mark from Module 5 alone — an ~53% reduction on its own. Since
this is a personal bot used in bursts (several messages per sitting), the realistic
session-average lands closer to the second row than the first, which is what puts a genuine 90%
*aggregate* reduction in reach — but say exactly that, with the caveat, rather than quoting one
cherry-picked number. **Module 0 replaces every estimate in this table with a real one — do that
before reporting a final percentage to anyone.**

---

## 12. Suggested build order

1. Module 0 (measure) — always first, re-run after every later module to check the hypothesis.
2. Module 2 (schema minimization) — free, zero risk.
3. Module 3 (prompt ablation) — cheap, eval-gated.
4. Module 4 (prompt caching) — the plan in `context-management-token-reduction.md`, now applied
   to the already-trimmed prompts from steps 2-3.
5. Module 6's cheap check (does `capture.extract` already pass evals on `model_fast`?) — do this
   before Module 5, it's a one-line config change to try.
6. Module 5 (call consolidation) — on a spike branch, eval-gated, hardest and highest-value.
7. Module 8 — fold into the conversation-memory feature whenever that's resumed.
8. Re-measure with Module 0 and compare to the real baseline, not the illustrative one.
