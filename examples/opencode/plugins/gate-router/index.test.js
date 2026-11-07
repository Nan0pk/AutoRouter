// Behaviour tests for gate-router.
//
// Run: node --test examples/opencode/plugins/gate-router/index.test.js
//
// No network, no real opencode, no real python. The gate is a tiny shell script
// written into a temp dir, so the real execFile path is exercised.

import test from "node:test"
import assert from "node:assert/strict"
import { chmodSync, existsSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs"
import { tmpdir } from "node:os"
import { join } from "node:path"

import plugin, { ledgerLine, parseRef, tierFor } from "./index.js"

const ROUTER = { providerID: "router", id: "auto" }
const T1 = "opencode-go/mimo-v2.6-flash"
const T2 = "opencode-go/deepseek-v4.1-flash"
const T3 = "opencode-go/claude-haiku-5-5"
const T4 = "opencode-go/deepseek-v4-pro"
const PROMPT = "SECRET-PROMPT-TEXT rename foo to bar"

const TRIVIAL = { class: "trivial", route: "main", tier_hint: null, delegate: false, score: -4, risk: [], overrides: [], reasons: ["short"] }

let dir
test.beforeEach(() => {
  dir = mkdtempSync(join(tmpdir(), "gate-router-test-"))
})
test.afterEach(() => {
  rmSync(dir, { recursive: true, force: true })
})

// Writes an executable gate. It prints `decision` as JSON, or exits non-zero,
// and appends a line to callLog on every invocation when given.
function makeGate(name, { decision = TRIVIAL, exitCode = 0, callLog } = {}) {
  const path = join(dir, name)
  const lines = ["#!/bin/sh"]
  if (callLog) lines.push(`echo called >> '${callLog}'`)
  if (exitCode) {
    lines.push("echo boom-from-gate >&2", `exit ${exitCode}`)
  } else {
    lines.push(`cat <<'JSON_EOF'\n${JSON.stringify(decision)}\nJSON_EOF`)
  }
  writeFileSync(path, lines.join("\n") + "\n")
  chmodSync(path, 0o755)
  return path
}

// Fake OpenCode context. `sessions` is a Map of mutable session objects.
function harness({ options = {}, sessions = {} } = {}) {
  const store = new Map(Object.entries(sessions))
  const added = []
  const switched = []
  let promptHandler
  const ctx = {
    options,
    provider: {
      async transform(fn) {
        fn({ add(entry) { added.push(entry) } })
      },
    },
    session: {
      hook(name, fn) {
        if (name === "prompt") promptHandler = fn
      },
      async get({ sessionID }) {
        return store.get(sessionID)
      },
      async switchModel({ sessionID, model }) {
        switched.push({ sessionID, model })
        const s = store.get(sessionID)
        if (s) s.model = { providerID: model.providerID, id: model.modelID }
      },
    },
    model: {
      async default() {
        return { data: { ...ROUTER } }
      },
      async list() {
        return { data: [] }
      },
    },
  }
  return {
    ctx,
    added,
    switched,
    store,
    async prompt(sessionID, text = PROMPT) {
      await promptHandler({ sessionID, prompt: { text } })
    },
  }
}

async function setup(opts) {
  const h = harness(opts)
  await plugin.setup(h.ctx)
  return h
}

const freshSession = (extra = {}) => ({ model: { providerID: "router", id: "auto" }, ...extra })

function ledgerEntries(file) {
  if (!existsSync(file)) return []
  return readFileSync(file, "utf8")
    .split("\n")
    .filter(Boolean)
    .map((l) => JSON.parse(l))
}

test("registers the virtual router/auto provider and model", async () => {
  const h = await setup({ options: { gateArgs: [] } })
  assert.equal(h.added.length, 1)
  const entry = h.added[0]
  assert.equal(entry.info.id, "router")
  assert.equal(entry.info.activation, "enabled")
  assert.equal(entry.models.length, 1)
  assert.equal(entry.models[0].id, "auto")
  assert.equal(entry.models[0].providerID, "router")
  assert.equal(entry.models[0].name, "Auto")
})

test("picker name comes from options.name", async () => {
  const h = await setup({ options: { name: "Gate" } })
  assert.equal(h.added[0].models[0].name, "Gate")
})

test("trivial decision routes to the configured T2 ref (no variant key)", async () => {
  const gate = makeGate("trivial.sh", { decision: TRIVIAL })
  const ledger = join(dir, "ledger.jsonl")
  const h = await setup({
    options: { gateCommand: gate, gateArgs: ["classify.py"], ledger, tiers: { T2: T2 } },
    sessions: { s1: freshSession() },
  })
  await h.prompt("s1")
  assert.deepEqual(h.switched, [
    { sessionID: "s1", model: { providerID: "opencode-go", modelID: "deepseek-v4.1-flash" } },
  ])
  assert.equal("variant" in h.switched[0].model, false)
})

test("security risk routes to T4 and preserves #variant", async () => {
  const gate = makeGate("security.sh", {
    decision: { ...TRIVIAL, class: "risky", route: "hierarchy", tier_hint: "T2", risk: ["security"] },
  })
  const h = await setup({
    options: { gateCommand: gate, gateArgs: ["classify.py"], ledger: join(dir, "l.jsonl"), tiers: { T2, T4: `${T4}#high` } },
    sessions: { s1: freshSession() },
  })
  await h.prompt("s1")
  assert.deepEqual(h.switched[0].model, { providerID: "opencode-go", modelID: "deepseek-v4-pro", variant: "high" })
})

test("data_loss risk also routes to T4", async () => {
  assert.equal(tierFor({ risk: ["data_loss"], tier_hint: "T1" }), "T4")
})

test("tier_hint T3+ routes to T3", async () => {
  const gate = makeGate("t3.sh", {
    decision: { ...TRIVIAL, class: "hard", route: "hierarchy", tier_hint: "T3+", risk: [] },
  })
  const h = await setup({
    options: { gateCommand: gate, gateArgs: ["classify.py"], ledger: join(dir, "l.jsonl"), tiers: { T2, T3 } },
    sessions: { s1: freshSession() },
  })
  await h.prompt("s1")
  assert.equal(h.switched[0].model.modelID, "claude-haiku-5-5")
})

test("broad_scope routes to T3 (synthetic decision; classify.py gave risk [] for 'what is a migration')", async () => {
  const gate = makeGate("broad.sh", {
    decision: { ...TRIVIAL, class: "standard", route: "hierarchy", tier_hint: "T1", risk: ["broad_scope"] },
  })
  const h = await setup({
    options: { gateCommand: gate, gateArgs: ["classify.py"], ledger: join(dir, "l.jsonl"), tiers: { T1, T2, T3 } },
    sessions: { s1: freshSession() },
  })
  await h.prompt("s1")
  assert.equal(h.switched[0].model.modelID, "claude-haiku-5-5")
})

test("manually pinned session is not switched and writes no ledger line", async () => {
  const callLog = join(dir, "calls.log")
  const gate = makeGate("pinned.sh", { callLog })
  const ledger = join(dir, "l.jsonl")
  const h = await setup({
    options: { gateCommand: gate, gateArgs: ["classify.py"], ledger, tiers: { T2 } },
    sessions: { s1: { model: { providerID: "opencode-go", id: "claude-haiku-5-5" } } },
  })
  await h.prompt("s1")
  assert.deepEqual(h.switched, [])
  assert.equal(existsSync(ledger), false)
  assert.equal(existsSync(callLog), false, "gate must not run for pinned sessions")
})

test("child session inherits the parent's concrete model and never runs the gate", async () => {
  const callLog = join(dir, "calls.log")
  // Would fail loudly if it ever ran.
  const gate = makeGate("should-not-run.sh", { exitCode: 9, callLog })
  const h = await setup({
    options: { gateCommand: gate, gateArgs: ["classify.py"], ledger: join(dir, "l.jsonl"), tiers: { T2 } },
    sessions: {
      parent: { model: { providerID: "opencode-go", id: "deepseek-v4-pro", variant: "high" } },
      child: freshSession({ parentID: "parent" }),
    },
  })
  await h.prompt("child")
  assert.deepEqual(h.switched, [
    { sessionID: "child", model: { providerID: "opencode-go", modelID: "deepseek-v4-pro", variant: "high" } },
  ])
  assert.equal(existsSync(callLog), false, "gate must not be invoked for child sessions")
})

test("child whose parent is also on router is left alone", async () => {
  const h = await setup({
    options: { gateArgs: ["x"], tiers: { T2 } },
    sessions: { parent: freshSession(), child: freshSession({ parentID: "parent" }) },
  })
  await h.prompt("child")
  assert.deepEqual(h.switched, [])
})

test("gate exits non-zero -> routes to T2 and ledger carries gate_error", async () => {
  const gate = makeGate("fail.sh", { exitCode: 3 })
  const ledger = join(dir, "l.jsonl")
  const h = await setup({
    options: { gateCommand: gate, gateArgs: ["classify.py"], ledger, tiers: { T2 } },
    sessions: { s1: freshSession() },
  })
  await h.prompt("s1")
  assert.equal(h.switched[0].model.modelID, "deepseek-v4.1-flash")
  const [line] = ledgerEntries(ledger)
  assert.equal(line.gate_error, "exit 3")
  assert.equal(line.tier, "T2")
  assert.equal(line.model, T2)
})

test("gate failure with failClosed leaves the model alone but still records gate_error", async () => {
  const gate = makeGate("fail-closed.sh", { exitCode: 3 })
  const ledger = join(dir, "l.jsonl")
  const h = await setup({
    options: { gateCommand: gate, gateArgs: ["classify.py"], ledger, failClosed: true, tiers: { T2 } },
    sessions: { s1: freshSession() },
  })
  await h.prompt("s1")
  assert.deepEqual(h.switched, [])
  const [line] = ledgerEntries(ledger)
  assert.equal(line.gate_error, "exit 3")
  assert.equal("tier" in line, false)
})

test("missing gateArgs routes to T2 and flags gate: missing-config without running a gate", async () => {
  const callLog = join(dir, "calls.log")
  const gate = makeGate("noargs.sh", { callLog })
  const ledger = join(dir, "l.jsonl")
  const h = await setup({
    options: { gateCommand: gate, ledger, tiers: { T2 } },
    sessions: { s1: freshSession() },
  })
  await h.prompt("s1")
  assert.equal(h.switched[0].model.modelID, "deepseek-v4.1-flash")
  assert.equal(existsSync(callLog), false)
  const [line] = ledgerEntries(ledger)
  assert.equal(line.gate, "missing-config")
  assert.equal(line.tier, "T2")
})

test("missing tier falls back to the next cheaper configured tier", async () => {
  const gate = makeGate("t4-missing.sh", {
    decision: { ...TRIVIAL, risk: ["security"] },
  })
  const ledger = join(dir, "l.jsonl")
  const h = await setup({
    options: { gateCommand: gate, gateArgs: ["classify.py"], ledger, tiers: { T2, T3 } },
    sessions: { s1: freshSession() },
  })
  await h.prompt("s1")
  assert.equal(h.switched[0].model.modelID, "claude-haiku-5-5")
  const [line] = ledgerEntries(ledger)
  assert.equal(line.tier, "T3")
  assert.equal(line.tier_requested, "T4")
})

test("no tier configured at all leaves the session alone without throwing", async () => {
  const gate = makeGate("none.sh")
  const ledger = join(dir, "l.jsonl")
  const h = await setup({
    options: { gateCommand: gate, gateArgs: ["classify.py"], ledger, tiers: {} },
    sessions: { s1: freshSession() },
  })
  await h.prompt("s1")
  assert.deepEqual(h.switched, [])
  assert.equal(existsSync(ledger), false)
})

test("concurrent prompts for one session run the gate once", async () => {
  const callLog = join(dir, "calls.log")
  const gate = makeGate("once.sh", { callLog })
  const h = await setup({
    options: { gateCommand: gate, gateArgs: ["classify.py"], ledger: join(dir, "l.jsonl"), tiers: { T2 } },
    sessions: { s1: freshSession() },
  })
  await Promise.all([h.prompt("s1"), h.prompt("s1")])
  assert.equal(readFileSync(callLog, "utf8").trim().split("\n").length, 1)
  assert.equal(h.switched.length, 1)
})

test("ledger line is valid JSON, has no prompt text, and tier/model are correct", async () => {
  const gate = makeGate("ledger.sh", {
    decision: { class: "hard", route: "hierarchy", tier_hint: "T2", delegate: true, score: 3, risk: ["security"], overrides: [], reasons: [] },
  })
  const ledger = join(dir, "l.jsonl")
  const h = await setup({
    options: { gateCommand: gate, gateArgs: ["classify.py"], ledger, tiers: { T2, T4: `${T4}#high` } },
    sessions: { s1: freshSession() },
  })
  await h.prompt("s1")

  const raw = readFileSync(ledger, "utf8")
  assert.equal(raw.endsWith("\n"), true)
  assert.equal(raw.includes("SECRET-PROMPT-TEXT"), false, "prompt text must not be logged")
  assert.equal(raw.includes("rename foo"), false)

  const [line] = ledgerEntries(ledger)
  assert.equal(line.sessionID, "s1")
  assert.equal(line.class, "hard")
  assert.equal(line.route, "hierarchy")
  assert.equal(line.tier_hint, "T2")
  assert.deepEqual(line.risk, ["security"])
  assert.equal(line.delegate, true)
  assert.equal(line.tier, "T4")
  assert.equal(line.model, `${T4}#high`)
  assert.equal(line.prompt_chars, PROMPT.length)
  assert.equal(typeof line.gate_ms, "number")
  assert.equal(typeof line.ts, "string")
  assert.equal(Number.isNaN(Date.parse(line.ts)), false)
})

test("ledger appends one line per routed prompt", async () => {
  const gate = makeGate("two.sh")
  const ledger = join(dir, "sub", "dir", "l.jsonl")
  const h = await setup({
    options: { gateCommand: gate, gateArgs: ["classify.py"], ledger, tiers: { T2 } },
    sessions: { s1: freshSession(), s2: freshSession() },
  })
  await h.prompt("s1")
  await h.prompt("s2")
  assert.equal(ledgerEntries(ledger).length, 2)
})

test("ledger write failure does not throw", async () => {
  const gate = makeGate("ledger-fail.sh")
  // A regular file where a directory is needed makes mkdir fail.
  const blocker = join(dir, "blocker")
  writeFileSync(blocker, "x")
  const h = await setup({
    options: { gateCommand: gate, gateArgs: ["classify.py"], ledger: join(blocker, "l.jsonl"), tiers: { T2 } },
    sessions: { s1: freshSession() },
  })
  await h.prompt("s1")
  assert.equal(h.switched.length, 1)
})

test("tierFor implements the mapping policy", () => {
  assert.equal(tierFor({ route: "main", tier_hint: null, risk: [] }), "T2")
  assert.equal(tierFor({ tier_hint: "T2", risk: [] }), "T2")
  assert.equal(tierFor({ tier_hint: "T1", risk: [] }), "T1")
  assert.equal(tierFor({ tier_hint: "T3+", risk: [] }), "T3")
  assert.equal(tierFor({ tier_hint: "T1", risk: ["broad_scope"] }), "T3")
  assert.equal(tierFor({ tier_hint: "T3+", risk: ["security"] }), "T4")
  assert.equal(tierFor({ tier_hint: null, risk: ["security", "broad_scope"] }), "T4")
  assert.equal(tierFor({ tier_hint: null, risk: ["data_loss"] }), "T4")
  assert.equal(tierFor({ tier_hint: null, delegate: true, risk: [] }), "T2", "delegate does not change tier")
  assert.equal(tierFor(undefined), "T2")
})

test("parseRef handles provider/model, #variant, and invalid input", () => {
  assert.deepEqual(parseRef("opencode-go/mimo-v2.6-flash"), { providerID: "opencode-go", modelID: "mimo-v2.6-flash" })
  assert.deepEqual(parseRef("opencode-go/deepseek-v4-pro#high"), { providerID: "opencode-go", modelID: "deepseek-v4-pro", variant: "high" })
  assert.equal("variant" in parseRef("a/b"), false)
  assert.equal(parseRef("nomodel"), undefined)
  assert.equal(parseRef("/x"), undefined)
  assert.equal(parseRef("a/"), undefined)
  assert.equal(parseRef("a/b#"), undefined)
  assert.equal(parseRef(42), undefined)
})

test("ledgerLine emits one JSON line and drops non-whitelisted keys", () => {
  const line = ledgerLine({ ts: "2026-01-01T00:00:00.000Z", sessionID: "s", tier: "T2", prompt: "leak me", undef: undefined })
  assert.equal(line.endsWith("\n"), true)
  assert.equal(line.split("\n").length, 2)
  const obj = JSON.parse(line)
  assert.deepEqual(obj, { ts: "2026-01-01T00:00:00.000Z", sessionID: "s", tier: "T2" })
})
