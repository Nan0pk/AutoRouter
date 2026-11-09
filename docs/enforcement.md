# What is enforced, and what is only advice

AutoRouter is a **methodology + toolkit**. Some of it is deterministic code that runs in CI;
much of it is advice to a model or a harness. This page is the single source of truth for which
is which. The README, `docs/routing-model.md` and `docs/harness-integration.md` must agree with
it — `tests/test_docs_claims.py` fails the build when they do not.

## Status

The [Hermes setup command](../SETUP.md) installs a native delegation backend
restricted to the routine/specialist role routes, plus root-turn instructions
and request middleware. Complex requests require delegation; child turns are
excluded. The backend refuses unknown roles and supplies no fallback providers.
Setup normalizes auxiliary/fallback settings, with private backups for undo.
Live verification observes actual API models, usage and parent/child result
return; a skipped specialist cannot pass.

The installed profile is selectable as **AutoRouter → AutoRouter-All** by
default. Verification checks the shared picker inventory and native model
selection as well as the live handoff. The virtual model keeps its visible name;
the native Chat Completions request carries the pinned routine model. Client
construction pins the saved login, endpoint and account headers, and preserves
the raw response model. Other routine transports are rejected before activation.
Ordinary manual model selection disables automatic root routing.

Hermes hooks/middleware can fail open on unexpected errors, so this is not a
global billing boundary. Other plugins and manual changes remain limitations.
Free selection needs fresh zero input/output prices; unknown quota stays unknown.
There is no runtime quota ledger or provider-side spending cap. The table below
continues to describe the offline toolkit and existing OpenCode example.

| Capability | Status | What actually runs | Evidence |
|---|---|---|---|
| Benchmark provenance + evidence coverage | **Enforced** (offline) | `scripts/score_models.py` rejects an observation with no `source` / `source_kind` / `benchmark_version` / `collected_on` / `conditions`; emits `insufficient evidence` instead of a tier below `--min-coverage` | `scripts/check.sh`, `schema/scored.schema.json` |
| Tier assignment + router config generation | **Enforced** (offline) | `score_models.py`, `build_router.py` (skips null tiers) | `scripts/check.sh` |
| Config validation | **Enforced** | `scripts/validate_router.py`; JSON Schema in CI | CI `Schema validation` step |
| Complexity gate | **Enforced in the OpenCode example** (`examples/opencode/plugins/gate-router`); **advisory** in the generic toolkit | The plugin runs `scripts/classify.py --json -- <prompt>` on a session's first prompt and switches the session to the tier's model before any request. Outside that example nothing calls the gate | `scripts/check.sh` runs the plugin's unit tests; a live probe reads the chosen model back from the session |
| Model selection per prompt | **Enforced in the OpenCode example** — deterministic (`classify.py` + the `tiers` map). `opencode-auto-model` (an LLM decider) remains a supported alternative, but is referenced, not vendored, and not in CI | the plugin pins the session model; a manually pinned session is never overridden | `examples/opencode/plugins/gate-router/` |
| Routing decision ledger | **Implemented, not enforced** | The plugin appends one JSON line per decision (class, tier, model, gate duration — never the prompt text). Nothing reads it back: no monthly rollup, no cap, no refusal | `gate-router.jsonl` in the state dir |
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

## Why the gate is wired in, and quotas are not

1. **The injection points belong to the harness.** AutoRouter assumes only `chooseModel(task)`
   and `delegate(subtask, agent)`. The OpenCode plugin is one implementation of `chooseModel`;
   the generic toolkit stays harness-agnostic.
2. **An LLM decider was the weak link.** Routing used to be delegated to a decider model. It
   over-routed trivial prompts to the frontier tier in live sessions, and the same prompts routed
   cheaply when replayed, i.e. the decision was not reproducible. `classify.py` is deterministic,
   ~30 ms, costs nothing, and its error profile is measured on 162 labelled prompts (0%
   under-routing / 16.5% over-routing). Trading an unmeasured decider error for a measured
   over-routing rate is the point of the swap.
3. **The ledger is deliberate, the budget is not built.** Decisions are logged so the over-routing
   rate can be checked against real traffic. A quota ledger needs persistence and a trust
   boundary ($/month counters keyed on the subscription, surviving restarts, shared across
   sessions) — that is a product decision, not a script.

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

- **Quota savings are unvalidated.** No *valid* measured run yet compares AutoRouter against a
  fixed model. The argument that routing cheap work down saves quota is a design rationale, not a
  result. See [`eval/`](../eval/README.md) for the harness and its status: the first measured run
  was invalidated by provider faults, and is being re-run under the fail-closed rules
  (`valid_evidence` is only true when every task executed).
- **Gate accuracy is measured, but only for the classifier.** 0% under-routing / ~17%
  over-routing on 162 labelled prompts (`tests/gate/README.md`). That is a property of
  `classify.py`, not of any router that may or may not call it.
- **Capability scores are evidence-backed and coverage-reported**, which is a statement about
  data quality, not about routing quality.
