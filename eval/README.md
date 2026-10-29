# Evaluation — does routing actually help?

This directory exists to **test the routing claim**, not to support it. The claim under test is:

> Routing each task to the cheapest capable tier (AutoRouter) beats pinning one model, either on
> success (vs. a cheap workhorse) or on cost/quota (vs. a strong model).

Until a **measured** run exists, that claim is unvalidated. See "Status" below.

## Arms

| Arm | Model | Behaviour |
|---|---|---|
| `fixed_workhorse` | `opencode-go/deepseek-v4.1-flash` (T2) | one model for every task, no escalation |
| `fixed_strong` | `opencode-go/gemini-4-argon` (T5) | one model for every task, no escalation |
| `autorouter` | per-task | runs `scripts/classify.py`, picks a tier, escalates once to T5 when the gate allows delegation and the checks fail |

The two fixed arms differ **only** in model. The AutoRouter arm differs in model choice and in
being allowed one escalation, so the comparison answers "is routing worth its overhead?" rather
than "is the frontier model better?".

## Success metric

Binary per task: **all declarative checks pass**. Checks are `contains`, `not_contains`, `regex`,
and `compiles` over the task's fixture files; they never execute model output.

- `success_rate` = tasks fully solved / tasks.
- `partial_check_rate` = checks passed / checks total (secondary; shows near-misses).
- `cost_per_success_usd` = total cost / successes — the number that actually matters if you are
  buying capability.

## Metrics reported

| Metric | How it is obtained |
|---|---|
| Success / partial checks | harness verification after the attempt |
| Cost (USD) | measured `tokens_in` / `tokens_out` × pinned prices, with the cache model from `pins.json` (cache hits billed at `cache_read_cost`, misses at `cache_write_cost`) |
| Quota | attempts per model / that model's `req_per_month`, and the max share of any monthly allowance |
| Latency | wall clock per attempt (executor-reported or measured), plus gate time |
| Retries | a second attempt on the same task |
| Escalations | a second attempt on a **higher** tier |
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

**No measured run is committed.** `eval/results/` is empty on purpose.

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
