# Evaluation — does routing actually help?

This directory exists to **test the routing claim**, not to support it. The claim under test is:

> Routing each task to the cheapest capable tier (AutoRouter) beats pinning one model, either on
> success (vs. a cheap workhorse) or on cost/quota (vs. a strong model).

Until a **measured** run exists, that claim is unvalidated. See "Status" below.

## Scope: the gate-based policy, not the OpenCode integration

This harness evaluates **`scripts/classify.py` + the tier map in `pins.json` + one escalation**.
It does **not** exercise the `opencode-auto-model` plugin, its decider model, or the delegation
setup in `examples/opencode/`. So a result here answers "is this routing policy better than
pinning a model?" — not "does the OpenCode setup work?". Every report states this in its
`policy` block, and the markdown header repeats it.

## Arms

| Arm | Model | Behaviour |
|---|---|---|
| `fixed_workhorse` | `opencode-go/deepseek-v4.1-flash` (T2) | one model for every task, one attempt |
| `fixed_workhorse_retry` | same T2 model | one attempt, then **one retry on the same model** if the checks fail |
| `fixed_strong` | `opencode-go/gemini-4-argon` (T5) | one model for every task, one attempt |
| `autorouter` | per-task | runs `scripts/classify.py`, picks a tier, escalates once to T5 when the gate allows delegation and the checks fail |

The fixed arms differ **only** in model and retry policy. `fixed_workhorse_retry` exists so the
benefit of *routing* can be separated from the benefit of a *second attempt*: without it, any
AutoRouter gain could be explained by "it got to try twice". Compare `autorouter` against
`fixed_workhorse_retry`, not only against `fixed_workhorse`.

## Validity (fail closed)

A report is evidence only when **every task executed and returned usable usage data**. The
executor name is not evidence: `--executor command` does not make a run valid.

| Situation | Recorded as | Effect on the report |
|---|---|---|
| Non-zero exit, timeout, no JSON on stdout | `infrastructure_error` | excluded from scoring; `valid_evidence: false`; listed under "adapter errors" |
| Missing, zero, negative or non-integer `tokens_in`/`tokens_out` | `infrastructure_error` | same |
| Sandbox fails to start | `infrastructure_error` | same |
| No sandbox available | live run refuses to start | stub can still exercise report plumbing with grading skipped |
| Assertions fail after a clean run | `model_failure` | counted in the success rate; listed under "failure cases" |
| All checks pass | `success` | counted |

An adapter error is **never retried** (that would hide the fault) and **never counted as a model
failure** (that would make a broken adapter look like a weak model). Success rates use the number
of tasks actually scored, and `success_rate_of_all_tasks` is reported beside it so the exclusion
is visible rather than flattering.

## Success metric

Binary per task: **all static checks pass AND all behavioural checks pass**.

- **Static** checks verify syntax for edited Python modules. The shipped tasks accept
  different local names, loops, and formatting styles.
- **Behavioural** (`eval/behavior/<task>.py`) imports the solution and exercises it with
  adversarial inputs. For `sec-path` that includes sibling, absolute-path and symlink escapes;
  for `sec-sqli` a `' OR '1'='1` payload that must arrive as a bound parameter; for `sec-shell`
  an argument that must stay a single vector element; for `data-migrate` an injected SQLite
  commit failure must preserve the old schema and data and propagate the error.

Regression tests cover both directions. A compiling path traversal must fail. An equivalent
null guard, dictionary comprehension, or JSON validation using a different name must pass.

- `success_rate` = tasks fully solved / tasks.
- `partial_check_rate` = checks passed / checks total (secondary; shows near-misses).
- `cost_per_success_usd` = total cost / successes — the number that actually matters if you are
  buying capability.

## Metrics reported

| Metric | How it is obtained |
|---|---|
| Success / partial checks | harness verification after the attempt (static + behavioural) |
| Behaviourally verified | tasks whose behavioural suite actually ran in a sandbox |
| Modeled cost (USD) | reported tokens × pinned prices and cache assumptions. The estimate can differ from the provider bill. |
| Estimated quota proxy | adapter attempts per model divided by its estimated monthly requests. This cannot reveal the actual remaining allowance. |
| Latency | wall clock per attempt (executor-reported or measured), plus gate time |
| Retries | harness-driven second attempts **plus** executor-reported retries, reported separately |
| Escalations | a second attempt on a **higher** tier (harness) plus executor-reported escalations |
| Routing overhead | measured wall time of the `classify.py` call, reported separately from task latency |
| Failure cases | every failed task per arm, with the exact checks that failed and the gate decision |

## Task set

`tasks.jsonl` — 13 self-contained tasks over small fixtures in `fixtures/`:

| Category | n | What it probes |
|---|---|---|
| mechanical | 3 | rename, config value, unused import |
| bugfix | 3 | off-by-one, missing null guard, failing assertion |
| debugging | 2 | mutation during iteration, cache that never invalidates |
| security | 3 | SQL injection, shell injection, path traversal |
| broad | 1 | the same validation change in three modules |
| data-safety | 1 | a destructive migration made reversible |

Prompts are deliberately short and realistic; several are *ambiguous* on purpose, because a
router that only works on well-labelled prompts is not useful. `debug-mutation` and
`debug-cache`, for instance, are gate mistakes the harness should surface.

## Isolation

A measured run requires Linux Bubblewrap and an offline grader. Generation mounts the disposable
copy as `/work`. It has network access for inference and sees only system runtime files and
operator-selected read-only mounts. The original fixtures and graders stay outside that worker.
Verification runs in a separate network namespace with the candidate files and required tests
mounted read-only. The grader uses the system Python and SQLite.

Use a trusted adapter that starts its worker inside this boundary. Attaching to a preexisting
harness service can move tool execution outside it. The supplied OpenCode ACP adapter starts
a private process and permits only workspace file tools.

`AUTOROUTER_EVAL_MOUNTS` lists absolute file or directory paths as JSON. Use narrow mounts for
adapter code, executables, and any explicitly chosen credential file. The subprocess gets a
clean environment. `AUTOROUTER_EVAL_FORWARD` explicitly selects `AUTOROUTER_*` settings and
`*_API_KEY` variables. Secrets belong in the local environment or credential store.

`--sandbox none` is available for stub plumbing only. A measured run refuses to start when
either sandbox mode is unavailable.

## Pinned versions and settings

`pins.json` pins: model ids and versions, tier→model mapping, prices and monthly allowances,
intended generation settings, timeout, retry budget, sandbox policy, the cost model, and
SHA-256 fingerprints of the task descriptions and the full fixture and grading suite.
`run_eval.py` refuses to run if those inputs drift. Model settings must be supported by
the adapter. The OpenCode adapter pins the model and optional effort through ACP.

## Running it

Plumbing only (synthetic, safe, fast, what CI runs):

```bash
python3 eval/run_eval.py --self-test
```

Real measured run — the command gets the prompt on stdin, edits files in `$AUTOROUTER_WORKDIR`,
and prints one JSON line with `tokens_in`, `tokens_out`, `latency_ms`, `retries`, `escalations`,
`trace`:

```bash
export AUTOROUTER_EVAL_CMD='/absolute/path/to/my-harness-adapter'
export AUTOROUTER_EVAL_MOUNTS='["/absolute/path/to/my-harness-adapter"]'
python3 eval/run_eval.py --executor command \
  --out eval/results/run-$(date +%F).json \
  --markdown eval/results/run-$(date +%F).md
```

Run each arm at least 5 times per task before comparing (the harness currently records one
sample per task; variance is a known gap, see below), and record any prompt/temperature change in
`pins.json`.

## Checkpoint handoff pilot

`run_handoff.py` exercises a separate two-file task. The workhorse drafts a summary and
tries to fix the aggregation bug. One repair receives the public reproduction and its
diagnostic. If the blocker persists, a specialist gets a separate copy and may change only
`aggregate.py`. After the public check passes, the workhorse finishes `summary.py`.
Independent regression checks grade the result. The earlier checkpoint remains available.

Try the offline scenario from the repository root.

```bash
run_root="$(mktemp -d)"
python3 eval/run_handoff.py --run-dir "$run_root/pilot" --steps 2
python3 eval/run_handoff.py --run-dir "$run_root/pilot" --resume
```

The first command parks the scripted run at `specialist`. The second should report
`completed`. `state.json` records each phase, selected model, check result, and usage.
The pilot refuses to resume if a checkpoint was changed or a previous turn's outcome
is uncertain. A fresh run always uses a new directory.

For live testing, use an installed OpenCode v2 with ACP model selection and end-turn usage.
Provide a supported provider key in your local environment. This example forwards the
OpenCode Go key by name and works with two models available on that account.

```bash
adapter="$PWD/eval/adapters/opencode_acp.py"
export AUTOROUTER_OPENCODE="$(command -v opencode)"
export AUTOROUTER_EVAL_CMD="$(python3 -c 'import shlex, sys; print(shlex.join(["python3", sys.argv[1]]))' "$adapter")"
export AUTOROUTER_EVAL_MOUNTS="$(python3 -c 'import json, sys; print(json.dumps(sys.argv[1:]))' "$adapter" "$AUTOROUTER_OPENCODE")"
export AUTOROUTER_EVAL_FORWARD='["AUTOROUTER_OPENCODE","OPENCODE_GO_API_KEY"]'

live_root="$(mktemp -d)"
python3 eval/run_handoff.py --run-dir "$live_root/pilot" --executor command \
  --workhorse "$WORKHORSE_MODEL" --specialist "$SPECIALIST_MODEL" --steps 2
python3 eval/run_handoff.py --run-dir "$live_root/pilot" --resume
```

Set both model variables to distinct exact `provider/model` values from your catalog.
The optional `/variant` suffix pins effort when supported. The adapter can also import
a selected legacy `auth.json` by explicitly mounting it and forwarding its path as
`AUTOROUTER_OPENCODE_AUTH`. Keep the mount narrow and the main OpenCode profile separate.
The worker uses a disposable home. It verifies the ACP model selection response and
records turn tokens. [OpenCode ACP](https://opencode.ai/docs/cli/acp/)

The live pilot can consume provider usage. If the workhorse solves the blocker, the
specialist is skipped. A pilot proves the control flow for this scenario. The comparative
eval, a second harness, and live quota reservation remain separate work.

## Status

**No measured run is committed.** `eval/results/` is empty on purpose. A real run must pass the
validity rules above before anyone may cite it; a report with any adapter error is stamped
`INVALID RUN` and `valid_evidence: false`.

Why the stub numbers are not evidence: `--executor stub` applies canned patches and canned token
and latency figures (`pins.json → stub`). It encodes the hypothesis — which model *would* solve
which task — and then reports it back. That makes it a useful test of the harness and a
worthless test of the router, so synthetic reports are stamped `"synthetic": true`,
`"valid_evidence": false`, and `run_eval.py` refuses to write one into `eval/results/` without
`--force`.

Consequently, **the quota-saving claim in the README remains unvalidated**. What exists today is
the instrument, not the measurement.

## Known limitations

- **13 tasks, one sample each.** Enough to exercise the harness and expose obvious routing
  mistakes; not enough for a confidence interval. The stub's 85% is illustrative of the *shape*
  of the comparison, not of any model's ability.
- **Verification scope.** Syntax and behavioural checks cannot judge every design choice. A passing patch
  may be bad code.
- **No tools or network.** Fixtures are tiny; real agentic work is not represented.
- **Latency is environment-dependent.** Compare arms within one run, never across runs.
- **Escalation is capped at one.** A real orchestrator may escalate repeatedly; that is not
  measured here.
- **The gate is the router's weak point.** Two debugging prompts route to T1/T2 and fail in the
  AutoRouter arm, which is exactly the under-routing the calibration set tries to bound.
- **Adaptive retries can hide work.** The harness records executor-reported retries and
  escalations, but cannot verify them; an adapter that silently retries internally will look
  cheaper than it is unless it reports honestly.
