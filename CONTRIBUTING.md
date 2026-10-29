# Contributing to AutoRouter

Thanks for helping! This is a **methodology + toolkit**, so contributions are mostly about
*keeping the process honest* and *widening coverage*.

## Ground rules

- **Never fabricate benchmark numbers.** If data is missing, omit the observation. Recorded
  conflicts are kept, not averaged.
- **Prefer agentic evidence** (Terminal-Bench, DeepSWE) over saturated single-shot SWE-bench.
- **Discount reward-hacked / leaky models** and record the caveat.
- **Every model description must include a capability ceiling** (`"Not for ..."`). The validator
  warns when one is missing.
- **No secrets or tokens in the tree.** CI fails on `github_pat` / `x-access-token`.
- **Scripts stay dependency-free** (stdlib Python 3). The schema check in CI may `pip install
  jsonschema`; that dependency must not leak into the core scripts.

## Dev setup

- Python 3.11+.
- Nothing to install for the core scripts.

## Run the checks

```bash
bash scripts/check.sh
```

This is exactly what CI runs: a token-leak guard, byte-compile, gate unit tests, gate
calibration (under- vs over-routing budgets), scoring, building both harness configs, and
validating every config (including the OpenCode example against the catalog).

## Changing the complexity gate

- Label new prompts **before** running the classifier (`tests/gate/README.md` has the rubric).
- Put prompts you did not tune on in `split: test`.
- `python3 scripts/calibrate_gate.py --split dev` and `--split test` must both stay within the
  under-routing (≤5%) and over-routing (≤20%) budgets, with zero high-risk prompts below
  `complex`.
- Never trade under-routing for over-routing: sending hard work to a weak model is the expensive
  mistake.

## Adding or updating a model

1. Add **one row per observation** to `data/benchmarks.example.csv` (long format, joined on
   `provider`+`model`) and one row per model to `data/quota.example.csv` (economics, including
   `cache_read_cost`/`cache_write_cost`). Never add a number without `source`, `source_kind`,
   `benchmark_version`, `collected_on`, and `conditions` — the scorer rejects it.
2. Run `python3 scripts/score_models.py --benchmarks … --quota … --out scored.json`.
3. **Review the assigned tier.** The tool is a starter; apply judgement — e.g. a model with huge
   SWE-bench but weak agentic scores may deserve a lower tier than the raw number suggests.
4. `python3 scripts/build_router.py --scored scored.json --harness opencode` produces a starter
   config. **Refine the descriptions** and make sure each has a ceiling.
5. Update `data/catalog.example.json` if you want the validator to resolve the model.
6. If a model fills a delegation gap and beats the incumbent, **demote the incumbent** and update
   the agents under `examples/opencode/agents/`.

## Adding a harness adapter

- Document an injection point in `docs/harness-integration.md`.
- Provide a config under `examples/<harness>/`.
- If code is needed, keep it optional and dependency-free where possible.

## Data formats

- `benchmarks.example.csv` — long format:
  `provider,model,metric,value,source,source_kind,benchmark_version,collected_on,conditions,notes`.
  Omit a row entirely when a model was not measured; do not write `n/a` placeholders.
  `source_kind` must be `independent`, `aggregate`, or `lab_card`.
- `quota.example.csv` — `provider,model,monthly_limit_usd,req_per_month,input_cost,output_cost,cache_read_cost,cache_write_cost,notes`.
- `scored.json` (generated) — validated against `schema/scored.schema.json`; a model below
  `--min-coverage` has `tier: null` and `tier_status: "insufficient evidence"`.
- `data/catalog.example.json` — list of `{providerID, id, enabled, status, variants: [id, …]}`.

## Pull requests

- One focused change per PR.
- `bash scripts/check.sh` must be green.
- Call out source conflicts and reward-hacking caveats in the PR description.
- Keep `schema/autorouter.schema.json` and the examples in sync.

## Code style

- Stdlib only for `scripts/*.py`; readable, `argparse`-based CLIs.
- Keep scripts composable (`stdin` / `--json` / `--out`).
- Prefer clarity over cleverness.

## Reporting issues

Include the harness, the model catalog snapshot, the router config, and what you expected the
router to do versus what it did.
