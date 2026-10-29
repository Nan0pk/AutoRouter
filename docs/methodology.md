# Methodology — how to build a quota-aware auto-router

This is the **thought process** behind AutoRouter. It is provider- and harness-agnostic: run
these steps against any model catalog to produce your own router. The OpenCode Go example in
`examples/opencode/` is one concrete instantiation.

---

## Step 1 — Inventory the catalog

Collect, per model, the objective facts your provider exposes:

| Field | Why it matters |
|---|---|
| `id`, `provider` | The reference used to route and resolve |
| context / output limits | Filters models for large-context tasks |
| capabilities (tools, input/output modalities) | Must support tools + text out for coding |
| cost (input / output / cache) | Baseline economics |
| variants (reasoning efforts) | A second routing axis (effort per task) |
| status / enabled | Skip deprecated or disabled models |

**Rule:** only consider models that are `enabled`, not `deprecated`, and support tools +
text output. Everything else is noise.

## Step 2 — Establish quota & economics

For **subscription plans**, quota is the scarce resource. Capture:

- **Monthly allowance** per model (often a dollar figure).
- **Rate windows** (e.g. 5-hour = 20%, weekly = 50%, monthly = 100%).
- **Estimated requests per window** — providers usually publish a table; use it as
  *capacity* rather than raw price.
- **Peak / off-peak** differences (a model can be cheaper at certain hours).

For **pay-as-you-go**, use blended cost per token as the budget.

**Key insight:** the *most capable* models almost always have the *smallest* allowances. Routing
must therefore treat capability and quota as a joint constraint, not separate ones.

## Step 3 — Gather capability evidence

Rank sources by usefulness **for coding agents**:

1. **Agentic / terminal** — Terminal-Bench 4.0, DeepSWE, FrontierSWE, CursorBench, SWE-Marathon.
   *Most important*: they measure multi-step, tool-using, long-horizon behavior.
2. **Coding** — SWE-bench Verified/Pro, LiveCodeBench, Aider Polyglot (if current).
3. **Human preference** — LMArena **coding** Elo (not the overall board).
4. **Aggregate indices** — Artificial Analysis Intelligence Index, LLMStat.
5. **Reasoning** — GPQA-Diamond, AIME, HLE, ARC-AGI.
6. **Lab model cards** — self-reported; treat as an *upper bound* and cross-check.

### Record provenance per observation

A benchmark value is only usable if you can re-derive it later. `data/benchmarks.example.csv`
is therefore **long format** — one row per `(provider, model, metric)` observation — and every
row carries:

| Column | Meaning |
|---|---|
| `source` | the board or card that published the number |
| `source_kind` | `independent`, `aggregate`, or `lab_card` |
| `benchmark_version` | the benchmark/leaderboard revision (e.g. `TB4.0`, `verified-2026-04`) |
| `collected_on` | ISO date you read it |
| `conditions` | tool use, trials, temperature, context, subset — whatever changes the number |
| `notes` | caveats: reward-hacking, conflicts, preview status |

`scripts/score_models.py` joins this table with quotas on **(provider, model)** — not on model
name alone, which collides across providers. Two observations that disagree by more than 5%
are a **recorded conflict**: the independent board is retained, the disagreement is emitted in
`conflicts`, and nothing is averaged silently.

### Uncertainty is a first-class output

Each model reports **evidence coverage** — the share of metric weight that actually has a
provenance-tracked observation. If coverage drops below `--min-coverage` (default `0.5`), or no
weighted metric has evidence, the model gets `"tier": null` and
`"tier_status": "insufficient evidence"` instead of a tier. It is still listed, with its costs,
so it can be routed to manually — but no automated ladder is built from a guess.

### Weighting rules

- For coding agents, **prefer agentic evidence** over saturated single-shot SWE-bench.
- **Watch for reward-hacking / leakage.** If a source flags "answers leaked" or similar,
  discount the affected model's coding score.
- **Reconcile conflicts.** When a lab card and an independent board disagree, prefer the
  independent board and record the conflict. Never average silently.
- **Never fabricate or backfill.** A missing observation is absent, never `0` or an average of
  other models. Let coverage, not imputation, carry the uncertainty.

## Step 4 — Capability × quota = value

Turn evidence into two numbers per model:

- **Capability** — a normalized 0–1 score, e.g.
  `0.30·DeepSWE + 0.25·TerminalBench + 0.15·SWE-bench + 0.15·ArenaCoding + 0.15·AA`.
  (Weights are yours to choose — document them. `scripts/score_models.py` makes them a flag.)
- **Value** — capability per unit of scarce resource, using an **explicit token mix + cache
  model** rather than a blended input+output price:

  ```
  cached   = input_tokens × cache_hit_rate
  uncached = input_tokens − cached
  cost_per_request = (uncached × cache_write_cost
                    + cached   × cache_read_cost
                    + output_tokens × output_cost) / 1e6

  value    = capability / (0.001 + cost_per_request)
  capacity = capability × log10(req_per_month + 10)
  ```

  The token mix, cache-hit rate, and averaging window are recorded in the output under
  `cost_model`, so a cost claim is reproducible and comparable. A blended per-token price hides
  the fact that most coding traffic is long cached prompt reads.

Plot the **cost/capability frontier**. The frontier *is* your model ladder: models inside it are
redundant, models on it are the ones worth routing to.

## Step 5 — Define tiers

Collapse the frontier into legible bands:

| Tier | Meaning | Typical role |
|---|---|---|
| **T0** | Throwaway / unlimited | mechanical, disposable work; quota-exhausted fallback |
| **T1** | Cheap, high-volume | simple, low-stakes, repetitive work |
| **T2** | Routine workhorse | day-to-day coding; the default + safe fallback |
| **T3** | Strong | hard-ish: multi-step fixes, subtle logic |
| **T4** | Hard | architecture, complex refactors, deep debugging |
| **T5** | Frontier | the hardest work; smallest quotas |

**Pick one best model per tier**, preferring **higher quota within a capability band** (so
routine work is sustainable) and reserving **tight-quota** models for the top tiers.

## Step 6 — Write routing criteria (the secret sauce)

For each candidate, write a three-part description:

1. **Tier / strength** — what it is for.
2. **Ceiling** — what it is explicitly **not** for ("Not for multi-file refactors", "Not for
   long agentic work", …).
3. **Quota** — the allowance, as a tiebreaker.

The **ceiling** is what makes difficulty-based routing work. A decider instructed to
*"pick the cheapest model that can handle this request well"* will only reach for a premium
model when the cheaper models' ceilings exclude the task.

> Example: `"T2 routine workhorse. … Not for deep architecture or the hardest debugging."`

## Step 7 — Add a complexity gate

Decide *when not to use the hierarchy*:

| Class | Route | Delegate? |
|---|---|---|
| **trivial** (greeting, rename, one-liner, lookup) | stay on main/default model | no |
| **standard** (single-file change, small fix, Q&A) | cheapest capable tier | no |
| **complex** (architecture, multi-file, subtle bug) | mid/high tier | yes |

Implement it cheaply: heuristics first (`scripts/classify.py`), optionally a small decision
model. The gate prevents routing/delegation overhead on prompts that never needed it.

## Step 8 — Delegation orchestration (optional, powerful)

To use the best model for *each part* **without** re-routing the main conversation (which
would reset the prompt cache):

- Keep the session on its routed model.
- Provide **pinned subagents** at different tiers (cheap, research, strong, frontier).
- An **orchestrator prompt** keeps awareness of the work and routes each subtask to the best
  agent, proactively and adaptively — escalating when something turns out hard, dropping to
  cheap when it's mechanical.

See `templates/orchestrator.md` and `examples/opencode/agents/auto.md`.

## Step 9 — Validate and iterate

- Validate the config against `schema/autorouter.schema.json` and resolve every model ID +
  variant (`scripts/validate_router.py`).
- Confirm the decider and fallback resolve to real, enabled models.
- Re-run the whole pipeline when models, prices, or quotas change. **Demote superseded models**
  (e.g. a `.3` over a `.2`) instead of keeping both.

---

## The one-line philosophy

> Judge capability from agentic evidence, budget it against real quota, compress it into tiers,
> gate the easy work away, and delegate the rest to the cheapest model that won't drop it.
