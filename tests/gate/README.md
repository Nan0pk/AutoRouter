# Gate calibration set

`labeled_prompts.csv` is the labelled prompt set used to calibrate `scripts/classify.py`.
It has 162 rows (50 trivial / 60 standard / 52 complex) across 12 families, with a
`dev` / `test` split so the error profile can be re-checked without silently re-fitting.

| Column | Meaning |
|---|---|
| `id` | stable id (`g001`…); quote it in issues and PRs |
| `split` | `dev` (tuning) or `test` (reported in CI) |
| `label` | the class a careful reviewer would assign |
| `risk` | expected override family: `security`, `data_loss`, `broad_scope`, or empty |
| `family` | prompt family, for per-family reporting |
| `prompt` | the prompt itself |

## Labelling rubric

- **trivial** — no code change, or one mechanical edit in one place: greetings, lookups,
  renames/typos/formatting, single-value changes. Route stays on the main model.
- **standard** — a focused change or question in one area: small feature, single-file bugfix,
  an explanation that needs real reasoning. Hierarchy, no delegation.
- **complex** — multi-file or architectural work, subtle debugging (concurrency, regressions,
  root-cause hunts), security-sensitive changes, destructive/irreversible data operations, or
  broad-scope changes (many files, public APIs, repo-wide). Hierarchy + delegation.

When a prompt is genuinely borderline, prefer the **higher** label: under-routing is the error
that costs correctness, and the labelling rubric is deliberately conservative.

## What is measured

```bash
python3 scripts/calibrate_gate.py --split dev
python3 scripts/calibrate_gate.py --split test
python3 scripts/calibrate_gate.py --labels tests/gate/labeled_prompts.csv --json
```

- **Under-routing** — predicted class is below the label. Hard work sent to a weaker model.
  Budget: ≤5%.
- **Over-routing** — predicted class is above the label. Strong quota spent on easy work.
  Budget: ≤20%.
- **Risk recall** — every `security` / `data_loss` / `broad_scope` prompt must stay at
  `complex`. Zero exceptions.

`bash scripts/check.sh` enforces the budgets on the `test` split.

## Honest status

This set was used to **calibrate** the current heuristic, not to benchmark it. Vocabulary and
bonuses were added after inspecting both splits, so the reported numbers are an in-sample
description of the shipped rule set, not held-out generalisation. The `dev`/`test` split exists
so that *future* changes can be checked on prompts they were not tuned against; treat any
regression there as a real signal.

The remaining over-routing (≈17%) is concentrated in 5–8 word mechanical edits — "add a blank
line between functions" vs "add pagination to the users endpoint". A keyword gate cannot see the
object of the verb. Fixing that needs a small decision model, which would make the gate a
routing call of its own; that trade-off is unmeasured (see `eval/`).

## Adding prompts

1. Append rows with a new unique id; set `split: test` for anything you did not tune on.
2. Label from the rubric above, before running the classifier.
3. Run both splits. If under-routing rises, fix the rule, not the label — unless the label is
   wrong on reflection, in which case say why in the PR.
