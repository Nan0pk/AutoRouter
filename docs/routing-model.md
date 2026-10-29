# Routing model — tiers, gate, delegation

This document specifies the *runtime* behavior an AutoRouter implementation should provide.
It is the contract that `schema/autorouter.schema.json` and the example configs implement.

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

## 2. Complexity gate

The gate runs **before** routing and returns one of three classes.

```
classify(prompt) -> {
  "class": "trivial" | "standard" | "complex",
  "route": "main" | "hierarchy",
  "tier_hint": "T1" | "T2" | "T3+" | null,
  "delegate": bool,
  "score": int,
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

Signals (illustrative — tune per domain):
- **Length**: very short → negative; long → positive.
- **Complexity keywords**: architecture, refactor, migrate, debug, race, multi-file, design,
  performance, security, "why does" → positive.
- **Simplicity keywords**: rename, format, typo, "what is", explain, one-liner, lookup → negative.
- **Structure**: code fences, numbered steps, many newlines → positive.

Reference implementation: `scripts/classify.py`.

> If the gate returns `route: "main"`, the harness must **not** run the routing decision or
> spawn subagents. This is the answer to *"which prompt doesn't need the full hierarchy."*

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

Hard requirements:
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

## 5. Worked decision matrix (OpenCode Go, Oct 2026)

| Prompt | Gate | Route | Delegation |
|---|---|---|---|
| "rename `foo` to `bar`" | trivial | current model | none |
| "fix this failing unit test" | standard | `T1`/`T2` (deepseek-flash) | none |
| "add pagination to the users endpoint" | standard | `T2` (deepseek-flash) | maybe helper |
| "why does this deadlock under load?" | complex | `T3` (haiku) → escalate | → `expert` |
| "redesign the storage layer" | complex | `T2` hub + | → `expert`/`T5` |
| "clean up 40 files of boilerplate" | standard | cheap (`T1`) | → `helper` |
| "research how our cache invalidation works" | standard | `T2` hub | → `researcher` |

## 6. Expansion

The ladder is data, not code. To add capability:

1. Add the model to the catalog with `tier`, `capability`, `quota`, and a **ceiling** description.
2. If it beats the incumbent at its tier, **demote** the incumbent.
3. Add a pinned subagent for it if it fills a delegation gap.
4. Re-validate (`scripts/validate_router.py`) and re-run `scripts/score_models.py`.
