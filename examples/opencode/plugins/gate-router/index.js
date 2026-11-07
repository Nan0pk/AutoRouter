// gate-router
//
// ENFORCED routing gate for OpenCode. Unlike opencode-auto-model (an LLM decider
// that picks once per session from prompt text), this plugin asks
// `scripts/classify.py` (calibrated heuristic, ~30 ms, no network) for a
// decision on every first prompt of a root session that sits on `router/auto`,
// maps it to a tier, and pins the session to that tier's model.
//
// Policy (tierFor, implement-exactly):
//   risk has "security" | "data_loss"     -> T4
//   tier_hint === "T3+"                   -> T3
//   risk has "broad_scope"                -> T3
//   tier_hint === "T1"                    -> T1
//   otherwise (main route, null/T2 hint)  -> T2
// `delegate` does NOT change the tier. It is only recorded in the ledger. It is
// NOT enforced here: nothing in this plugin hands work to subagents. It is a hint
// for the orchestrator prompt, not a routing action.
//
// Scope and honesty notes:
// - Only the FIRST prompt of a session is routed, and only while the session is
//   still on `router/*`. A user who picks a model manually is respected: the
//   plugin never overrides a non-router model.
// - Child sessions (session.parentID) never run the gate. They inherit the
//   parent's concrete model. This is enforced only for the first prompt of the
//   child while it is still on `router/*`.
// - Tier fallback (T4->T3->T2->T1) only goes to CHEAPER configured tiers. If no
//   tier at all is configured, the session is left alone (console.error, once).
// - The gate runs as `classify.py --json -- <prompt>`, so a prompt that begins
//   with "-" is not parsed as a flag.
// - The ledger never records prompt text. Only the whitelisted fields in
//   LEDGER_FIELDS are written, and gate errors are fixed strings (no argv echo).
//
// Options (opencode.jsonc plugins[].options):
//   tiers         { T1..T4: "provider/model" | "provider/model#variant" }
//   gateCommand   default "python3"
//   gateArgs      required, e.g. ["/abs/path/to/scripts/classify.py"]; without it
//                 every prompt routes to T2 and the ledger records
//                 gate: "missing-config"
//   gateTimeoutMs default 5000
//   ledger        default $XDG_STATE_HOME/opencode/gate-router.jsonl
//   name          picker name for router/auto, default "Auto"
//   failClosed    default false. true: on gate failure leave the model alone
//                 instead of routing to T2.

import { execFile } from "node:child_process"
import { appendFile, mkdir } from "node:fs/promises"
import { homedir } from "node:os"
import { dirname, join } from "node:path"

// `@opencode/plugin` is provided by the OpenCode host when the plugin runs
// inside it. It is not installed in this repo, so the import is optional: the
// pure helpers and tests work without it. Plugin.define is identity.
let Plugin = null
let Model = null
let Provider = null
try {
  ;({ Plugin, Model, Provider } = await import("@opencode/plugin"))
} catch {
  /* not available outside the OpenCode host; use plain-object fallbacks */
}

const PROVIDER_ID = "router"
const MODEL_ID = "auto"
const DEFAULT_GATE_TIMEOUT_MS = 5000
// Tier fallback order when a tier is not configured: cheaper tiers only.
const FALLBACK_ORDER = ["T4", "T3", "T2", "T1"]
// Only these keys are ever written to the ledger (no prompt text, no extras).
const LEDGER_FIELDS = [
  "ts",
  "sessionID",
  "class",
  "route",
  "tier_hint",
  "risk",
  "delegate",
  "tier",
  "tier_requested",
  "model",
  "gate",
  "gate_ms",
  "gate_error",
  "prompt_chars",
]

// ---- pure helpers (exported for tests) -------------------------------------

// Maps a classify.py decision to a tier. See the policy in the header.
export function tierFor(decision) {
  const d = decision && typeof decision === "object" ? decision : {}
  const risk = Array.isArray(d.risk) ? d.risk : []
  if (risk.includes("security") || risk.includes("data_loss")) return "T4"
  if (d.tier_hint === "T3+") return "T3"
  if (risk.includes("broad_scope")) return "T3"
  if (d.tier_hint === "T1") return "T1"
  return "T2"
}

// "provider/model" or "provider/model#variant" -> { providerID, modelID, variant? }.
// The variant key is omitted when there is no "#". Invalid refs return undefined.
export function parseRef(ref) {
  if (typeof ref !== "string") return undefined
  const input = ref.trim()
  const slash = input.indexOf("/")
  if (slash <= 0) return undefined
  const hash = input.indexOf("#", slash + 1)
  const modelID = input.slice(slash + 1, hash === -1 ? undefined : hash)
  const variant = hash === -1 ? undefined : input.slice(hash + 1)
  if (!modelID || variant === "") return undefined
  return {
    providerID: input.slice(0, slash),
    modelID,
    ...(variant ? { variant } : {}),
  }
}

// Serialises a ledger entry as one JSON line. Unknown keys are dropped, and so
// are undefined values (JSON.stringify drops them).
export function ledgerLine(entry) {
  const out = {}
  for (const key of LEDGER_FIELDS) {
    if (entry && entry[key] !== undefined) out[key] = entry[key]
  }
  return `${JSON.stringify(out)}\n`
}

// Returns { tier, ref } for the requested tier, or the next cheaper configured
// tier. Returns undefined when nothing usable is configured.
export function resolveTier(tier, tiers) {
  const start = FALLBACK_ORDER.indexOf(tier)
  if (start === -1) return undefined
  for (let i = start; i < FALLBACK_ORDER.length; i++) {
    const candidate = FALLBACK_ORDER[i]
    const ref = tiers?.[candidate]
    const parsed = parseRef(ref)
    if (parsed) return { tier: candidate, ref: parsed }
  }
  return undefined
}

// ---- internals --------------------------------------------------------------

function defaultLedgerPath() {
  const stateHome = process.env.XDG_STATE_HOME ?? join(homedir(), ".local", "state")
  return join(stateHome, "opencode", "gate-router.jsonl")
}

// Fixed strings only. error.message from execFile includes the argv, which
// includes the prompt text, so it must never reach the ledger.
function gateErrorMessage(error, timeoutMs) {
  if (error?.killed) return `timeout after ${timeoutMs}ms`
  if (typeof error?.code === "number") return `exit ${error.code}`
  if (typeof error?.code === "string") return `spawn ${error.code}`
  return "gate failed"
}

// Runs the gate and resolves to its parsed JSON decision object.
function runGate(options, text, timeoutMs) {
  const command = options.gateCommand ?? "python3"
  // "--" before the text so a prompt that starts with "-" is not parsed as a
  // flag by classify.py's argparse.
  const args = [...options.gateArgs, "--json", "--", text]
  return new Promise((resolve, reject) => {
    execFile(command, args, { timeout: timeoutMs }, (error, stdout) => {
      if (error) {
        reject(Object.assign(new Error(gateErrorMessage(error, timeoutMs)), { code: error.code, killed: error.killed }))
        return
      }
      let decision
      try {
        decision = JSON.parse(String(stdout))
      } catch {
        reject(new Error("gate output is not valid JSON"))
        return
      }
      if (!decision || typeof decision !== "object" || Array.isArray(decision)) {
        reject(new Error("gate output is not a JSON object"))
        return
      }
      resolve(decision)
    })
  })
}

async function writeLedger(file, entry) {
  try {
    await mkdir(dirname(file), { recursive: true })
    await appendFile(file, ledgerLine({ ts: new Date().toISOString(), ...entry }))
  } catch (error) {
    console.error("gate-router: ledger write failed:", error?.code ?? "error")
  }
}

function modelInfo(providerID, modelID, name) {
  if (Model?.Info?.default && Provider?.ID?.make && Model?.ID?.make) {
    return { ...Model.Info.default(Provider.ID.make(providerID), Model.ID.make(modelID)), name }
  }
  return {
    id: modelID,
    modelID,
    providerID,
    name,
    capabilities: { tools: true, input: ["text", "image"], output: ["text"] },
    variants: [],
    time: { released: 0 },
    cost: [],
    status: "active",
    enabled: true,
    limit: { context: 200_000, output: 32_000 },
  }
}

function modelKey(model) {
  return `${model.providerID}/${model.modelID}${model.variant ? `#${model.variant}` : ""}`
}

// ---- plugin -----------------------------------------------------------------

const plugin = {
  id: "gate-router",

  async setup(ctx) {
    const options = ctx.options ?? {}
    const tiers = options.tiers && typeof options.tiers === "object" ? options.tiers : {}
    const name = typeof options.name === "string" ? options.name : "Auto"
    const failClosed = options.failClosed === true
    const gateTimeoutMs =
      typeof options.gateTimeoutMs === "number" && options.gateTimeoutMs > 0
        ? options.gateTimeoutMs
        : DEFAULT_GATE_TIMEOUT_MS
    const ledgerFile = typeof options.ledger === "string" ? options.ledger : defaultLedgerPath()

    let warnedNoTier = false
    // sessionIDs with a routing decision in flight. A second prompt for the same
    // session returns immediately instead of running the gate again.
    const inflight = new Set()

    await ctx.provider.transform((providers) => {
      const providerID = Provider?.ID?.make ? Provider.ID.make(PROVIDER_ID) : PROVIDER_ID
      providers.add({
        info: {
          id: providerID,
          name: "",
          activation: "enabled",
          package: "@opencode/ai/providers/openai-compatible",
          // Never called: sessions are switched to a concrete tier before any request.
          settings: { baseURL: "http://127.0.0.1:9/v1", apiKey: "" },
        },
        models: [modelInfo(PROVIDER_ID, MODEL_ID, name)],
      })
    })

    // Subagent child sessions inherit the parent's concrete model.
    async function inherit(sessionID, parentID) {
      const parent = await ctx.session.get({ sessionID: parentID })
      const m = parent?.model
      if (!m || m.providerID === PROVIDER_ID) return
      const modelID = m.modelID ?? m.id
      if (!modelID) return
      const model = { providerID: String(m.providerID), modelID: String(modelID), ...(m.variant ? { variant: String(m.variant) } : {}) }
      await ctx.session.switchModel({ sessionID, model })
    }

    async function route(sessionID, text) {
      const session = await ctx.session.get({ sessionID })
      if (!session) return
      // A session on any non-router model was pinned by the user: leave it alone.
      const current = session.model ?? (await ctx.model.default())?.data
      if (current?.providerID !== PROVIDER_ID) return

      if (session.parentID) {
        await inherit(sessionID, session.parentID)
        return
      }

      const t0 = Date.now()
      const entry = { sessionID, prompt_chars: text.length }
      let tier
      let decision

      if (!Array.isArray(options.gateArgs)) {
        // Without gateArgs the script path is unknown: fail to T2, flag it.
        tier = "T2"
        entry.gate = "missing-config"
      } else {
        try {
          decision = await runGate(options, text, gateTimeoutMs)
          tier = tierFor(decision)
        } catch (error) {
          entry.gate_error = error?.message ?? "gate failed"
          if (failClosed) {
            entry.gate_ms = Date.now() - t0
            await writeLedger(ledgerFile, entry)
            return
          }
          tier = "T2"
        }
      }
      entry.gate_ms = Date.now() - t0

      if (decision) {
        entry.class = decision.class
        entry.route = decision.route
        entry.tier_hint = decision.tier_hint ?? null
        entry.risk = Array.isArray(decision.risk) ? decision.risk : []
        entry.delegate = decision.delegate === true
      }

      const resolved = resolveTier(tier, tiers)
      if (!resolved) {
        if (!warnedNoTier) {
          warnedNoTier = true
          console.error("gate-router: no tier configured in options.tiers; leaving sessions unrouted")
        }
        return
      }

      const model = resolved.ref
      await ctx.session.switchModel({ sessionID, model })

      entry.tier = resolved.tier
      if (resolved.tier !== tier) entry.tier_requested = tier
      entry.model = modelKey(model)
      await writeLedger(ledgerFile, entry)
    }

    await ctx.session.hook("prompt", async (event) => {
      const sessionID = String(event.sessionID)
      if (inflight.has(sessionID)) return
      inflight.add(sessionID)
      try {
        await route(sessionID, String(event.prompt?.text ?? ""))
      } catch (error) {
        console.error("gate-router:", error?.message ?? "routing failed")
      } finally {
        inflight.delete(sessionID)
      }
    })
  },
}

export default Plugin ? Plugin.define(plugin) : plugin
