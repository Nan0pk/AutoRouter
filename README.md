# AutoRouter

[![CI](https://github.com/Nan0pk/AutoRouter/actions/workflows/ci.yml/badge.svg)](https://github.com/Nan0pk/AutoRouter/actions/workflows/ci.yml)

A **methodology + toolkit** for building **quota-aware, capability-tiered automatic model routing** in any AI coding harness.

Instead of pinning one model forever, AutoRouter teaches a harness to:

1. **Judge each model's real capability** from reputable benchmarks.
2. **Respect provider quotas** — subscription allowances, rate windows, peak pricing.
3. **Tier models** into a clean capability ladder (`T0`–`T5`).
4. **Route each task** to the cheapest model that can do it well.
5. **Gate trivial prompts** so simple work never touches the full hierarchy.
6. **Delegate mid-conversation** to cheaper or stronger models for subtasks.

The point is not a specific config — it's a **reproducible process** you can run against
*any* provider catalog to produce *your own* router.

---

## Core ideas (the short version)

- **Capability × quota is the real objective.** A model is "best" only relative to how much of
  it you can actually use. The most capable models usually have the *smallest* allowances.
- **Tiers make routing legible.** Collapse dozens of models into `T0`–`T5` bands; route by task
  difficulty, not by model name.
- **Ceilings make routing work.** Every model description must say what it is **not** for.
  A router told to *"pick the cheapest model that can handle it well"* will respect an explicit
  capability ceiling.
- **Not every prompt needs the hierarchy.** A complexity gate sends trivial prompts to the main
  model and only escalates real work — and it is measured: under-routing and over-routing are
  counted separately, and security / data-loss / broad-scope prompts always escalate.
- **Delegation beats re-routing for cache.** Keep the session model fixed; hand subtasks to
  pinned subagents on other models.
- **Evidence has a shelf life and a shape.** Every benchmark number carries its source, the date
  it was collected, the benchmark version, and the conditions it was measured under. A score
  without provenance is a rumour; a score with thin coverage is not a tier.

---

## Implementation status

This is a **methodology + toolkit**, not a turnkey runtime router. Here is what runs today versus
what you must wire into a harness.

| Capability | Status | Where |
|---|---|---|
| Quota-aware tiers + capability-ceiling descriptions | **Implemented** (config) | `examples/`, `schema/` |
| Offline scoring → tiers → config builder | **Implemented** | `scripts/score_models.py`, `build_router.py` |
| Config validation | **Implemented** — custom checks; `--schema` for real JSON Schema | `scripts/validate_router.py` |
| Provenance, evidence coverage, "insufficient evidence" tiers | **Implemented** | `scripts/score_models.py`, `schema/scored.schema.json` |
| Explicit token-mix + cache cost model (no blended input+output) | **Implemented** | `scripts/score_models.py` |
| Complexity gate (heuristic) | **Implemented and calibrated** (162 labelled prompts; 0% under-routing, 16.5% over-routing on the test split), but **advisory** — nothing calls it at runtime | `scripts/classify.py`, `tests/gate/` |
| Model selection per prompt | **External** — the OpenCode example relies on the `opencode-auto-model` plugin (referenced, not vendored or tested here) | `examples/opencode/` |
| Quota ledger / budget enforcement | **Not implemented** — quota is descriptive guidance only, with no counter or cap | — |
| Delegation orchestration (pinned subagents) | **Advisory** (OpenCode agent prompts; nothing verifies the hand-off) | `examples/opencode/agents/` |
| Return to `auto` after a turn | **Implemented** (OpenCode plugin) | `examples/opencode/plugins/` |
| End-to-end evaluation (success, cost, latency, escalations) | **Not yet run** — harness only, no results | `eval/` |

**Read [`docs/enforcement.md`](docs/enforcement.md) before relying on any of this.** It records,
per capability, what actually runs versus what is advice. The short version: the methodology and
the offline checks are real and CI-enforced; the **complexity gate and quota economics are
advisory**, not runtime policy, unless a harness implements them. **No quota-saving claim in this
repository is backed by a measurement.**

---

## Repository layout

```
AutoRouter/
├── README.md
├── docs/
│   ├── methodology.md          # the full thought process (start here)
│   ├── routing-model.md        # tiers, complexity gate, delegation, pseudo-code
│   └── harness-integration.md  # how to port this to any harness
├── schema/
│   ├── autorouter.schema.json  # JSON Schema for a harness-agnostic router config
│   └── scored.schema.json      # JSON Schema for the scored model table
├── examples/
│   ├── opencode/               # a complete, working OpenCode setup (Go plan)
│   │   ├── opencode.jsonc
│   │   ├── agents/             # orchestrator + delegation subagents
│   │   └── plugins/            # auto-model-reset (return to auto after a turn)
│   └── generic/
│       └── router.config.json  # harness-agnostic example
├── scripts/
│   ├── classify.py             # complexity gate (route vs. stay-main, delegate vs. inline)
│   ├── calibrate_gate.py       # labelled-set calibration: under- vs over-routing
│   ├── score_models.py         # benchmark + quota CSVs  ->  capability/value tiers
│   ├── build_router.py         # scored tiers            ->  a starter router config
│   └── validate_router.py      # validate a router config + resolve model IDs
├── tests/
│   ├── gate/labeled_prompts.csv # 162 labelled prompts + risk families
│   └── test_classify.py         # gate unit + calibration-budget tests
├── data/
│   ├── benchmarks.example.csv  # long-format observations + provenance (Oct-2026 Go data)
│   └── quota.example.csv       # quota/economics inputs, incl. cache prices
└── templates/
    ├── orchestrator.md         # self-aware delegation orchestrator prompt
    └── subagent.md             # pinned-model subagent template
```

---

## Quick start — build a router

The pipeline mirrors how this repo's OpenCode example was made:

```bash
# 1. Complexity gate — does a prompt need the hierarchy at all?
python3 scripts/classify.py "rename this variable"

# 2. Score models from capability + quota data.
#    Each score reports its evidence coverage; sparse models get
#    "insufficient evidence" instead of a tier. Cost is priced from an
#    explicit token mix + cache model, not a blended input+output number.
python3 scripts/score_models.py \
  --benchmarks data/benchmarks.example.csv \
  --quota data/quota.example.csv \
  --input-tokens 20000 --output-tokens 2000 --cache-hit-rate 0.5 \
  --out scored.json

# 3. Generate a starter router config from the scored tiers
python3 scripts/build_router.py --scored scored.json --harness opencode --out my-router.json

# 4. Validate it (and resolve model IDs against a live catalog, if you have one)
python3 scripts/validate_router.py --config my-router.json --catalog catalog.json
```

All scripts are dependency-free Python 3.

---

## The worked example: OpenCode + Go plan

`examples/opencode/` contains a complete, working setup built with this methodology against
**OpenCode Go** in October 2026:

- An **11-model `T0`–`T5` ladder** in `opencode.jsonc` (quota-aware).
- A self-aware **`auto` orchestrator** agent that runs on the auto-routed model and delegates
  subtasks to pinned-model subagents (`helper`, `researcher`, `specialist`, `expert`).
- An **`auto-model-reset` plugin** that returns the session to `router/auto` when a turn ends, so
  it never gets stuck on the last routed model.

See `docs/methodology.md` for why each model landed where it did.

---

## Porting to another harness

AutoRouter is harness-agnostic because it only assumes two primitives:

1. **`chooseModel(task)`** — pick a model for a unit of work.
2. **`delegate(subtask, agent)`** — run a subtask on a different model.

Everything else is configuration. See **[docs/harness-integration.md](docs/harness-integration.md)**
for injection points (plugins/hooks, agent configs, or an LLM proxy) and per-harness notes.

---

## Design principles

- **Never invent benchmark numbers.** If a model is absent from a leaderboard, mark it `n/a`.
  Treat self-reported lab cards as upper bounds.
- **Prefer agentic evidence.** For coding agents, Terminal-Bench / DeepSWE outrank saturated
  single-shot SWE-bench.
- **Watch for reward-hacking.** Some models' coding scores are inflated by answer leakage.
- **Reserve the scarce.** Tiny-quota frontier models are for genuinely hard work only.
- **Keep the cache.** Delegate (child sessions) instead of re-routing the main model every turn.
- **Stay reproducible.** Config is data → tiers → config; re-run when models or prices change.

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md). Run `bash scripts/check.sh` before opening a PR — it's
the same check CI runs.

## License

MIT — see [LICENSE](LICENSE).
