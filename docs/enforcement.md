# What is enforced, and what is only advice

AutoRouter is a **methodology + toolkit**. Some of it is deterministic code that runs in CI;
much of it is advice to a model or a harness. This page is the single source of truth for which
is which. The README, `docs/routing-model.md` and `docs/harness-integration.md` must agree with
it — `tests/test_docs_claims.py` fails the build when they do not.

## Status

| Capability | Status | What actually runs | Evidence |
|---|---|---|---|
| Benchmark provenance + evidence coverage | **Enforced** (offline) | `scripts/score_models.py` rejects an observation with no `source` / `source_kind` / `benchmark_version` / `collected_on` / `conditions`; emits `insufficient evidence` instead of a tier below `--min-coverage` | `scripts/check.sh`, `schema/scored.schema.json` |
| Tier assignment + router config generation | **Enforced** (offline) | `score_models.py`, `build_router.py` (skips null tiers) | `scripts/check.sh` |
| Config validation | **Enforced** | `scripts/validate_router.py`; JSON Schema in CI | CI `Schema validation` step |
| Complexity gate | **Advisory** | `scripts/classify.py` is a library + CLI. **No runtime path calls it.** In the OpenCode example the orchestrator model is *asked* to follow `agents/auto.md`; nothing verifies that it did | `tests/gate/`, `scripts/calibrate_gate.py` measure the heuristic, not the route |
| Model selection per prompt | **External** | The `opencode-auto-model` plugin named in `examples/opencode/opencode.jsonc`. It is referenced, not vendored, and not exercised by this repo's CI | config only |
| Quota ledger / budget enforcement | **Not implemented** | Nothing. Quota appears as descriptive text in model descriptions and as `req_per_month` hints in scoring. There is no counter, no ledger, no cap | — |
| Delegation orchestration | **Advisory** | Prompt-level instructions (`agents/auto.md`, `templates/orchestrator.md`). No check that a subtask actually went to the pinned agent | — |
| Return to `auto` after a turn | **Enforced** (OpenCode only) | `examples/opencode/plugins/auto-model-reset/index.js`, for sessions whose origin is `auto` (manual model choices are never reset); CI runs its behavioural tests | `scripts/check.sh` |

## What "advisory" means here

An advisory mechanism **cannot fail**. If the model ignores it, no test goes red and no request
is rejected. Specifically:

- `classify.py` will happily return `complex` for a prompt; nothing stops a harness from routing
  it to a `T1` model anyway.
- The tier ladder is a config that a router *may* consult. `build_router.py` produces a starter
  config; it does not deploy or police it.
- Quota numbers (`req_per_month`, `monthly_limit_usd`) are inputs to a *value* calculation and
  wording in a description. No code decrements them, and no code refuses a request when they
  are exhausted.
- Delegation rules ("delegate mechanical work to `helper`") are instructions in a system prompt.

So: **the methodology is real, the measurements in this repo are real, and the runtime policy is
advice.** Where the README says a mechanism is advisory, this is what that means.

## Why the gate and quotas are not wired in

1. **The injection points belong to the harness.** AutoRouter assumes only `chooseModel(task)`
   and `delegate(subtask, agent)`. Every harness implements those differently; a single
   implementation would be a plugin for one tool, not a harness-agnostic toolkit.
2. **Wiring a heuristic in changes behaviour silently.** The gate is right about under-routing
   on the labelled set, but it over-routes ~17% of short mechanical prompts. Enforcing it would
   spend real quota on that error; the OpenCode example deliberately leaves the decision with
   the model, where the cost is visible in the conversation.
3. **A quota ledger needs persistence and a trust boundary.** Monthly counters keyed on
   subscription, surviving restarts, shared across sessions — that is a product decision, not a
   script.

## What enforcement would actually require

Pick one injection point and make it **assertable** — a test that fails when the policy is
violated, not just a classifier unit test.

| Route | Mechanism | Must also ship |
|---|---|---|
| OpenCode plugin | Pre-request hook: call `classify.py`, set the session model, refuse or downgrade on risk overrides | A test harness that runs a prompt and asserts the chosen model; a JSONL ledger with monthly rollups |
| LLM proxy | Inspect the first message + session id, rewrite the target model | Ledger at the proxy, keyed on provider account; per-model caps; 429-style refusal |
| Wrapper CLI | Classify, then pass `--model`, refuse when the ledger is exhausted | The ledger, plus an escape hatch (`--force`) and an audit log |

Until one of those exists, treat every routing and quota statement in this repo as a
**hypothesis to test**, not a guarantee.

## Claims

- **Quota savings are unvalidated.** No measured run compares AutoRouter against a fixed model.
  The argument that routing cheap work down saves quota is a design rationale, not a result.
  See [`eval/`](../eval/README.md) for the harness that would produce the number, and its current
  status: built and CI-tested, **no measured run committed**.
- **Gate accuracy is measured, but only for the classifier.** 0% under-routing / ~17%
  over-routing on 162 labelled prompts (`tests/gate/README.md`). That is a property of
  `classify.py`, not of any router that may or may not call it.
- **Capability scores are evidence-backed and coverage-reported**, which is a statement about
  data quality, not about routing quality.
