# Harness integration — using AutoRouter anywhere

AutoRouter assumes only two primitives. Any harness that can answer these can use this project:

1. **`chooseModel(task) -> model`** — pick a model for a unit of work.
2. **`delegate(subtask, agent) -> result`** — run a subtask on a different model.

Everything else (tiers, gate, criteria, delegation pool) is configuration produced by the
pipeline in the README.

> **Enforced or advisory?** See [`enforcement.md`](enforcement.md). Of the two primitives above,
> neither is provided by this repository: it produces the config that a harness feeds to its own
> `chooseModel` / `delegate`. The complexity gate and quota figures are **advisory** here.

---

## 1. The harness-agnostic config

`schema/autorouter.schema.json` defines a portable config:

```jsonc
{
  "version": 1,
  "default_model": "provider/model-a",
  "fallback": "provider/model-b",
  "gate": { "enabled": true, "main_floor": -2, "complex_threshold": 5 },
  "models": [
    {
      "id": "provider/model-a",
      "tier": "T2",
      "capability": 0.62,
      "quota": { "requests_per_month": 173330 },
      "description": "Routine workhorse. … Not for deep architecture."
    }
  ],
  "delegation": {
    "enabled": true,
    "orchestrator_prompt": "templates/orchestrator.md",
    "agents": [
      { "name": "helper", "model": "provider/cheap", "tier": "T1" },
      { "name": "expert", "model": "provider/frontier", "tier": "T5" }
    ]
  }
}
```

Convert this to your harness's native shape with `scripts/build_router.py`, or hand-write it.

## 2. Injection points by harness type

| Harness style | Where to inject | Notes |
|---|---|---|
| **Plugin / hook** (OpenCode plugin, VS Code ext, custom loop) | Pre-request hook: run the gate, then `chooseModel`, then set the session model | Best fidelity; can also inject delegation tools. This is the only style that can *enforce* the gate |
| **Agent config** (OpenCode agents, Claude Code subagents, Codex agents) | One **primary orchestrator** agent + N **subagent** agents with pinned models | No code; relies on the model following the orchestrator prompt |
| **LLM proxy** (LiteLLM, custom gateway) | Inspect request metadata (first message, session id) and rewrite the target model | Harness-agnostic, but delegation is harder (proxy can't spawn subagents) |
| **Wrapper script** | Classify + route before invoking the CLI, pass `--model` | Simplest; per-invocation, not per-session |

### OpenCode (worked example)

- **Router:** `opencode-auto-model` plugin (`model: "router/auto"`), configured via
  `plugins[].options.models` — this is the `T0`–`T5` ladder.
- **Decider:** `opencode/jev-1.13` via the OpenCode Console connection (returns a choice *from*
  the candidate set, so it can't miss the exact-match).
- **Orchestrator:** a `primary` agent (`agents/auto.md`) set as `default_agent`.
- **Delegation:** `mode: subagent` agents with pinned `model:` fields.

See `examples/opencode/`.

### Claude Code

- **Router:** set `model` per session (or use `/model`), or wrap the CLI and pass `--model`.
- **Orchestrator:** the main agent's system prompt.
- **Delegation:** project/user **subagents** (`.claude/agents/*.md`) with `model:` frontmatter;
  the main agent invokes them via the Task/subagent tool.

### Codex / other CLIs

- **Router:** a wrapper that classifies the first prompt and passes the model flag.
- **Delegation:** most CLIs lack subagents — emulate with the wrapper invoking a second process on
  a different model, or skip delegation and rely on routing only.

## 3. Porting checklist

- [ ] Inventory models + capabilities from the provider catalog.
- [ ] Collect quota/economics (allowances, rate windows, peak pricing).
- [ ] Gather capability evidence (agentic first) and score with `scripts/score_models.py`.
- [ ] Assign `T0`–`T5`; pick the best model per tier (prefer quota within a band).
- [ ] Write **ceiling** descriptions for every candidate.
- [ ] Set a **safe fallback** (cheap + capable, `T2`).
- [ ] Decide whether to **enforce** the complexity gate or accept it as advice. If you enforce it,
      add a test that asserts the model chosen for a labelled prompt; if you do not, say so in
      your docs (`scripts/classify.py`, `tests/gate/` are the calibration inputs).
- [ ] Wire the **decider** (fast, constrained to the candidate set if possible).
- [ ] Optionally add **delegation** subagents + an orchestrator prompt.
- [ ] Validate (`scripts/validate_router.py`) and resolve all model IDs.
- [ ] Re-run when models/prices change; demote superseded models.

## 4. Anti-patterns

- **Re-routing the main model every turn** — destroys the prompt cache; use delegation instead.
- **No ceiling in descriptions** — the router over-picks cheap models for hard work.
- **Expensive/weak fallback** — a failed decision should never cost more or do worse.
- **Routing deprecated or non-tool models** — filter them out first.
- **Trusting self-reported cards alone** — cross-check with independent leaderboards.
- **Delegating trivial work** — the gate exists specifically to avoid this.
