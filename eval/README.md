# Evaluation — does routing actually help?

This directory exists to **test the routing claim**, not to support it. The claim under test is:

> Routing each task to the cheapest capable tier (AutoRouter) beats pinning one model, either on
> success (vs. a cheap workhorse) or on cost/quota (vs. a strong model).

Until a **measured** run exists, that claim is unvalidated. See "Status" below.

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
| No sandbox available (behaviour skipped) | `skipped` | scored on static checks only, but **the report is invalid** |
| Assertions fail after a clean run | `model_failure` | counted in the success rate; listed under "failure cases" |
| All checks pass | `success` | counted |

An adapter error is **never retried** (that would hide the fault) and **never counted as a model
failure** (that would make a broken adapter look like a weak model). Success rates use the number
of tasks actually scored, and `success_rate_of_all_tasks` is reported beside it so the exclusion
is visible rather than flattering.

## Success metric

Binary per task: **all static checks pass AND all behavioural checks pass**.

- **Static** (`contains`, `not_contains`, `regex`, `compiles`) is a cheap pre-filter over the
  fixture files. It is deliberately weak and is **not** sufficient on its own: a file can mention
  `realpath` and still allow traversal.
- **Behavioural** (`eval/behavior/<task>.py`) imports the solution and exercises it with
  adversarial inputs. For `sec-path` that means a sibling-directory escape and an absolute path;
  for `sec-sqli` a `' OR '1'='1` payload that must arrive as a bound parameter; for `sec-shell`
  an argument that must stay a single vector element; for `data-migrate` an injected failure that
  must roll back and propagate.

The harness includes a regression test for exactly the gap the static checks have: a planted
`files.py` that satisfies every static check (it mentions `realpath`, it does not contain the
literal unsafe join) but still leaks a file through an absolute path. Static: pass. Behaviour:
fail. If a future change makes the behavioural layer stop catching that, the test fails.

- `success_rate` = tasks fully solved / tasks.
- `partial_check_rate` = checks passed / checks total (secondary; shows near-misses).
- `cost_per_success_usd` = total cost / successes — the number that actually matters if you are
  buying capability.

## Metrics reported

| Metric | How it is obtained |
|---|---|
| Success / partial checks | harness verification after the attempt (static + behavioural) |
| Behaviourally verified | tasks whose behavioural suite actually ran in a sandbox |
| Cost (USD) | measured `tokens_in` / `tokens_out` × pinned prices, with the cache model from `pins.json` (cache hits billed at `cache_read_cost`, misses at `cache_write_cost`) |
| Quota | attempts per model / that model's `req_per_month`, and the max share of any monthly allowance |
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

Behavioural tests execute **model-written code**, so they run in a sandbox:

| Mechanism | Isolation | Used when |
|---|---|---|
| `bwrap` (bubblewrap) | new user/net/pid/ipc/uts namespaces, read-only host root, only the workdir writable | preferred; `--sandbox auto` probes for it |
| `unshare -rn` | network namespace only; filesystem is **not** isolated | fallback when bwrap is unavailable |
| `none` | none — behavioural tests are skipped | never valid evidence |

`--sandbox none` does not run behavioural tests and the report is stamped invalid. The
`sandbox` block in the JSON/Markdown report records which mechanism was used and how many
behavioural suites ran or were skipped.

**The adapter itself is not sandboxed.** It has to reach the model over the network, so the
harness cannot enforce the `tool_policy` from `pins.json` on it — that setting is advisory to the
adapter. Isolation here applies to the code that runs *after* the model answers.

## Pinned versions and settings

`pins.json` pins: model ids and versions, tier→model mapping, prices and monthly allowances,
`temperature: 0`, `max_tokens: 2048`, timeout, retry budget, sandbox policy, the cost model, and
a **SHA-256 of `tasks.jsonl`**. `run_eval.py` refuses to run when the task set changed without
`pins.json` being updated in the same commit — a silent task-set edit is how evals start lying.

## Running it

Plumbing only (synthetic, safe, fast, what CI runs):

```bash
python3 eval/run_eval.py --self-test
```

Real measured run — the command gets the prompt on stdin, edits files in `$AUTOROUTER_WORKDIR`,
and prints one JSON line with `tokens_in`, `tokens_out`, `latency_ms`, `retries`, `escalations`,
`trace`:

```bash
export AUTOROUTER_EVAL_CMD='./my-harness-adapter'
python3 eval/run_eval.py --executor command \
  --out eval/results/run-$(date +%F).json \
  --markdown eval/results/run-$(date +%F).md
```

Run each arm at least 5 times per task before comparing (the harness currently records one
sample per task; variance is a known gap, see below), and record any prompt/temperature change in
`pins.json`.

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
- **Static checks.** `compiles`/`contains`/`regex` cannot judge design quality. A passing patch
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
