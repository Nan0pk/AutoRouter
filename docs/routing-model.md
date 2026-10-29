# Routing model — tiers, gate, delegation

This document specifies the *runtime* behavior an AutoRouter implementation **should** provide.
It is the contract that `schema/autorouter.schema.json` and the example configs describe.

> **Status: mostly advisory.** See [`enforcement.md`](enforcement.md) for the authoritative
> enforced-vs-advisory table. No runtime path in this repository calls the gate (§2) or
> decrements a quota. Model selection (§3) is done by the harness — in the OpenCode example, by
> the external `opencode-auto-model` plugin. Delegation (§4) is prompt-level advice. The
> classifier's own error profile *is* measured (§2b); the route is not.

---

## 1. Tier ladder

| Tier | Intent | Delegate? | Escalate when |
|---|---|---|---|
| `T0` | throwaway / unlimited | no | never (floor) |
| `T1` | cheap, high-volume | no | task has real logic |
| `T2` | routine workhorse | no | multi-file / subtle |
| `T3` | strong | yes | needs architecture / deep debug |
| `T4` | hard | yes | frontier-level |
| `T5` | frontier | yes | — |

Each candidate carries: `id`, `tier`, `capability` (0–1), `quota` (requests/mo or $/mo),
and a `description` with an explicit **ceiling**.

A candidate also carries its evidence state: `evidence_coverage` (0–1, the share of metric
weight actually measured with provenance) and `evidence_status` (`ok` or
`insufficient evidence`). A model whose evidence is insufficient has no tier and must not be
selected by the ladder; route to it only by explicit human choice.

## 2. Complexity gate

The gate runs **before** routing and returns one of three classes.

```
classify(prompt) -> {
  "class": "trivial" | "standard" | "complex",
  "route": "main" | "hierarchy",
  "tier_hint": "T1" | "T2" | "T3+" | null,
  "delegate": bool,
  "score": int,
  "risk": ["security" | "data_loss" | "broad_scope", ...],
  "overrides": [str, ...],          # risk families that forced this class
  "reasons": [str, ...]
}
```

Decision table:

| Score | Class | Route | Tier hint | Delegate |
|---|---|---|---|---|
| `<= -2` | trivial | `main` (current/default model) | — | no |
| `-1 … 1` | standard | `hierarchy` | `T1` | no |
| `2 … 4` | standard | `hierarchy` | `T2` | no |
| `>= 5` | complex | `hierarchy` | `T3+` | yes |

Signals (calibrated in `scripts/classify.py`):
- **Length**: very short → negative; long → positive — *unless a risk override fires*.
- **Complexity keywords**: architecture, refactor, migrate, debug, race, multi-file, design,
  performance, security, "why does" → positive.
- **Simplicity keywords**: rename, typo, "what is", one-liner, greetings → negative.
- **Structure**: code fences, numbered steps, many newlines → positive.
- **Diagnosis**: a causal question ("why does/why is", "find the root cause", "investigate")
  about a concrete failure (lock, hang, leak, regression, 500s, corruption) → complex.
- **Capacity**: a numeric target ("50k events per second", "10x traffic", p99/throughput) →
  complex.
- **Structural change**: a structural noun (architecture, schema, pipeline, strategy) plus a
  change verb (design, migrate, restructure, introduce) → complex.

### 2a. Risk overrides (never route high-stakes work down)

Three families force `complex` + `delegate` regardless of prompt length:

| Family | Examples | Tier hint |
|---|---|---|
| `security` | vulnerability, injection, auth bypass, leaked token, privilege escalation, encryption, credential stuffing | `T3+` |
| `data_loss` | drop/truncate table, purge, migration, roll back, GDPR deletion, double-charge | `T3+` |
| `broad_scope` | "across 40 files", "all services", public API, monorepo, redesign/rewrite, across the codebase | `T2` (hub + helper) |

The length penalty is **skipped entirely** when an override fires, so
`"fix security vulnerability"` is complex work rather than a three-word triviality. Broad-scope
mechanical work still delegates — to a cheap helper, not the frontier — because its difficulty
is breadth, not depth.

Definitional questions are exempt from the broad-scope override: `"what is a monorepo?"` is a
lookup, not a repo-wide change.

### 2b. Measured error profile

The gate is calibrated against 162 labelled prompts (`tests/gate/labeled_prompts.csv`).
Under-routing (hard work sent to a weaker model) and over-routing (strong quota burned on easy
work) are reported separately, because they do not cost the same:

| Split | n | Under-routing | Over-routing | Risk prompts kept complex |
|---|---|---|---|---|
| dev | 83 | 0.0% | 16.9% | 13/13 |
| test | 79 | 0.0% | 16.5% | 13/13 |
| all | 162 | 0.0% | 16.7% | 26/26 |

`scripts/calibrate_gate.py` enforces `--max-under 0.05` and `--max-over 0.20` on the test split,
and fails if any security / data-loss / broad-scope prompt lands below `complex`. The remaining
over-routing is concentrated in 5–8 word mechanical edits whose object ("add a blank line" vs
"add pagination") a keyword gate cannot see.

Reference implementation: `scripts/classify.py`.

> If the gate returns `route: "main"`, the harness must **not** run the routing decision or
> spawn subagents. This is the answer to *"which prompt doesn't need the full hierarchy."*

> **Advisory in practice.** Nothing in this repository calls `classify.py` on a live prompt. The
> OpenCode example asks the orchestrator model to apply these rules itself (`agents/auto.md`);
> a harness that wants the gate enforced must call it in its pre-request path and test the
> resulting model choice — see [`enforcement.md`](enforcement.md).

## 3. Routing decision

```
route(task):
  g = classify(task)
  if g.route == "main":
      return current_model            # no hierarchy, no decision call
  candidates = [m for m in models if m.enabled and supports(tools, text)]
  if len(candidates) <= 1:
      return candidates[0] or fallback
  choice = decider(task, candidates, efforts)   # e.g. a fast "System One" model
  picked = exact_match(choice.model, candidates)
  if not picked:
      return fallback                 # MUST be a cheap, high-quota model
  return apply_effort(picked, choice.effort)
```

Hard requirements (**for a harness that implements this step** — none of them are enforced by
this repository):
- **One decision per session.** Do not re-route every turn (preserve the prompt cache).
- **Fallback is safe.** It should be cheap *and* capable (a `T2` workhorse), so a failed
  decision never lands on an expensive or weak model.
- **Exact-match the choice** against candidate IDs; if it misses, fall back — never guess.

## 4. Delegation (mid-conversation)

Delegation lets the session use the best model per *subtask* without changing its own model.

```
delegate(subtask, agents, orchestrator_model):
  best = pick_agent(subtask, agents)      # by tier fit vs. task difficulty
  if best.tier <= orchestrator_tier and is_trivial(subtask):
      return inline()                     # cheaper to just do it
  return run_child_session(agent=best, brief=make_brief(subtask))
```

Design rules:
- **Pinned models.** Each delegation agent has an explicit model (not `router/auto`), so the
  router leaves it alone.
- **Self-aware orchestrator.** The orchestrator prompt keeps goal/progress/difficulty in view and
  re-routes adaptively; it delegates proactively, not on request.
- **Self-contained briefs.** Child sessions start with fresh context — pass goal, context,
  constraints, and expected output.
- **One level deep.** Subagents return to the orchestrator; they don't chain-delegate.

See `templates/orchestrator.md`, `templates/subagent.md`, and
`examples/opencode/agents/`.

## 4b. Returning to auto after a turn (anti-stickiness)

A session should not stay pinned to the last model the router picked. Add a small companion
plugin that switches the session back to `router/auto` when a turn finishes, so the next prompt
re-routes through the hierarchy.

- **Signal:** the server delivers `session.step.ended` to plugin subscribers, but **not**
  `session.idle` / `session.status` — those are ephemeral client events. Reset only when a step
  is terminal (`finish !== "tool-calls"`) so the model is never swapped mid-turn.
- **Scope:** root sessions only (`session.parentID` empty), so pinned delegation subagents are
  left alone.
- **Debounce:** `delayMs` after the turn ends; a new `prompt` cancels a pending reset. `0` means
  reset after every turn; a large value keeps an active conversation on one warm-cache model and
  only resets after real inactivity.
- **Tradeoff:** resetting per turn means the router decides on the next prompt's text alone, so a
  contentless follow-up ("ok", "yes") routes weakly. Prefer a debounce if that matters.

See `examples/opencode/plugins/auto-model-reset/index.js`.

**Default policy in the example:** *turn-adaptive* (`delayMs: 0`) — the next prompt re-routes
after every turn. Choose *session-sticky* by raising `delayMs` (e.g. `1800000`) or disabling the
plugin. Pick one as your documented default and measure both before claiming a quota win.

## 5. Worked decision matrix (OpenCode Go, Oct 2026)

| Prompt | Gate | Route | Delegation |
|---|---|---|---|
| "rename `foo` to `bar`" | trivial | current model | none |
| "fix this failing unit test" | standard | `T1`/`T2` (deepseek-flash) | none |
| "add pagination to the users endpoint" | standard | `T2` (deepseek-flash) | maybe helper |
| "why does this deadlock under load?" | complex | `T3` (haiku) → escalate | → `expert` |
| "redesign the storage layer" | complex | `T2` hub + | → `expert`/`T5` |
| "clean up 40 files of boilerplate" | complex (broad scope) | `T2` hub | → `helper` |
| "research how our cache invalidation works" | standard | `T2` hub | → `researcher` |

## 6. Expansion

The ladder is data, not code. To add capability:

1. Add the model to the catalog with `tier`, `capability`, `quota`, and a **ceiling** description.
2. If it beats the incumbent at its tier, **demote** the incumbent.
3. Add a pinned subagent for it if it fills a delegation gap.
4. Re-validate (`scripts/validate_router.py`) and re-run `scripts/score_models.py`.
