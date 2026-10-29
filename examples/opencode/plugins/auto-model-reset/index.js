// auto-model-reset
//
// Returns a session's model to `router/auto` when a turn finishes, so a
// conversation is never left "stuck" on the last model the router picked as its
// main model. The next prompt then re-routes through the hierarchy.
//
// IMPORTANT: only sessions that *started on auto* are reset. If you pick a
// model yourself, that session is left alone for the rest of its life (until
// you pick `router/auto` again). Resetting a manually chosen model would undo
// your choice and, worse, silently stop the router from routing that session
// (the router only acts while the session's model is on the `router` provider).
//
// How the origin is decided
// -------------------------
// The router switches models during prompt admission, and plugin hooks run in
// config order, so by the time this plugin sees a prompt the session may
// already hold the router's pick. The origin is therefore latched from the
// best available evidence, in this order:
//
//   1. `session.created` with no model (or a `router/*` model) -> auto.
//   2. A model switch to `router/*` (ours, or yours) -> auto.
//   3. A switch to a concrete model while a turn is active -> the router -> auto.
//   4. A switch to a concrete model while no turn is active is *ambiguous*: it
//      is either the router's admission switch (if it lands just before this
//      plugin's prompt hook) or you picking a model between turns. It is
//      remembered and resolved at the next prompt: within `admissionWindowMs`
//      it was the router, otherwise it was you -> manual.
//
// Once a session is manual it stays manual. State is persisted through
// `ctx.storage`, so a config reload or server restart does not resurrect the
// bug. Nothing is reset when the origin is unknown.
//
// Caveat: a concrete switch that arrives *while a turn is active* is treated as
// the router's admission pick, because the TUI may commit a model change at
// prompt time too. To make a manual override stick in an auto session, pick the
// model between turns (or start a new session); the plugin then latches it as
// manual at the next prompt.
//
// Why `session.step.ended` and not `session.idle`?
//   The server does not deliver `session.idle` / `session.status` to plugin
//   subscribers — they are ephemeral client events. `session.step.ended` *is*
//   delivered. We only reset on a terminal step (finish !== "tool-calls") so the
//   model is never swapped mid-turn.
//
// Options (opencode.jsonc plugins[].options):
//   delayMs           - ms after a turn ends before resetting (default 0).
//                       Raise it (e.g. 1800000) to only reset after long
//                       inactivity and keep an active conversation warm.
//   onlyRoot          - skip subagent child sessions (default true).
//   admissionWindowMs - how long before a prompt a concrete switch still counts
//                       as the router's admission switch (default 500). The
//                       window only has to cover "this plugin's prompt hook
//                       starts just after the router's switch", so keep it
//                       small: it is also the window in which a manual pick
//                       made immediately before Enter would be misread.
//   debug             - append diagnostics to the debug file (default false).
//   debugFile         - debug log path (default: <tmp>/auto-model-reset.log).

import { appendFileSync } from "node:fs"
import { tmpdir } from "node:os"
import { join } from "node:path"

const ROUTER = { providerID: "router", id: "auto" }
const STORAGE_KEY = "sessions"
const MAX_TRACKED = 500

const isRouterRef = (model) => model?.providerID === ROUTER.providerID

export default {
  id: "auto-model-reset",

  async setup(ctx) {
    const opts = ctx.options || {}
    const delayMs = typeof opts.delayMs === "number" ? Math.max(0, opts.delayMs) : 0
    const onlyRoot = opts.onlyRoot !== false
    const admissionWindowMs =
      typeof opts.admissionWindowMs === "number" ? Math.max(0, opts.admissionWindowMs) : 500
    const debug = opts.debug === true
    const debugFile = typeof opts.debugFile === "string" ? opts.debugFile : join(tmpdir(), "auto-model-reset.log")

    const log = (...a) => {
      if (!debug) return
      try {
        appendFileSync(debugFile, `${new Date().toISOString()} ${a.join(" ")}\n`)
      } catch {
        /* ignore */
      }
    }

    // sessionID -> "auto" | "manual"
    const origins = new Map()
    // Sessions with a turn in flight (set on prompt admission, cleared on a terminal step).
    const turnActive = new Set()
    // sessionID -> timestamp of a concrete switch seen while no turn was active.
    const pendingSwitch = new Map()
    const timers = new Map()
    const controller = new AbortController()

    // What does a brand-new session start on? Only an auto default is resettable.
    let defaultIsAuto = true
    try {
      const current = await ctx.model.default()
      const ref = current?.data ?? current
      defaultIsAuto = !ref || isRouterRef(ref)
    } catch {
      /* no default readable: assume auto, the documented setup */
    }

    // ---- persisted origin state -------------------------------------------
    let saveTimer
    const persist = () => {
      if (!ctx.storage?.set) return
      clearTimeout(saveTimer)
      saveTimer = setTimeout(() => {
        saveTimer = undefined
        const snapshot = {}
        for (const [id, origin] of [...origins].slice(-MAX_TRACKED)) snapshot[id] = origin
        Promise.resolve(ctx.storage.set(STORAGE_KEY, snapshot)).catch(() => {})
      }, 500)
      saveTimer.unref?.()
    }

    const remember = (sessionID, origin) => {
      if (origins.get(sessionID) === origin) return
      origins.set(sessionID, origin)
      log("origin", sessionID, "->", origin)
      persist()
    }

    try {
      const saved = await ctx.storage?.get?.(STORAGE_KEY)
      if (saved && typeof saved === "object") {
        for (const [id, origin] of Object.entries(saved)) {
          if (origin === "auto" || origin === "manual") origins.set(id, origin)
        }
        log("restored", origins.size, "origins")
      }
    } catch {
      /* storage is optional; start empty */
    }

    // ---- reset -------------------------------------------------------------
    const cancel = (sessionID) => {
      const t = timers.get(sessionID)
      if (t) {
        clearTimeout(t)
        timers.delete(sessionID)
      }
    }

    const reset = async (sessionID) => {
      const origin = origins.get(sessionID)
      if (origin !== "auto") {
        log("skip", sessionID, "origin=", origin ?? "unknown")
        return
      }
      try {
        const session = await ctx.session.get({ sessionID })
        if (!session || !session.model) return
        if (onlyRoot && session.parentID) return
        if (isRouterRef(session.model)) return
        await ctx.session.switchModel({ sessionID, model: ROUTER })
        log("switched", sessionID, "-> router/auto")
      } catch (error) {
        console.error("auto-model-reset:", error)
      }
    }

    const schedule = (sessionID) => {
      cancel(sessionID)
      const t = setTimeout(() => {
        timers.delete(sessionID)
        void reset(sessionID)
      }, delayMs)
      timers.set(sessionID, t)
    }

    // A new prompt means the session is active again: cancel any pending reset
    // and resolve the origin while we still can.
    await ctx.session.hook("prompt", async (event) => {
      const sessionID = String(event.sessionID)
      cancel(sessionID)
      turnActive.add(sessionID)

      const now = Date.now()
      const switchedAt = pendingSwitch.get(sessionID)
      pendingSwitch.delete(sessionID)

      if (switchedAt !== undefined) {
        // A concrete switch arrived while the session was idle. The router only
        // switches during admission, so "just before this prompt" is the router
        // and anything older is the user.
        if (now - switchedAt <= admissionWindowMs) {
          if (origins.get(sessionID) !== "manual") remember(sessionID, "auto")
        } else if (origins.get(sessionID) === "auto") {
          remember(sessionID, "manual")
        }
      }

      if (!origins.has(sessionID)) {
        let model
        try {
          model = (await ctx.session.get({ sessionID }))?.model
        } catch {
          /* treat as unknown below */
        }
        if (model) remember(sessionID, isRouterRef(model) ? "auto" : "manual")
        else remember(sessionID, defaultIsAuto ? "auto" : "manual")
      }
    })

    log("setup delayMs=", delayMs, "onlyRoot=", onlyRoot, "window=", admissionWindowMs)

    void (async () => {
      try {
        for await (const event of ctx.event.subscribe({
          types: [
            "session.created",
            "session.model.selected",
            "session.deleted",
            "session.step.ended",
            "session.idle",
            "session.status",
          ],
          signal: controller.signal,
        })) {
          const data = event.data ?? {}
          const sessionID = String(data.sessionID ?? data.session?.id ?? data.id ?? "")
          if (!sessionID) continue

          if (event.type === "session.created") {
            const session = data.session ?? data
            if (!origins.has(sessionID)) {
              const model = session?.model
              remember(sessionID, !model ? (defaultIsAuto ? "auto" : "manual") : isRouterRef(model) ? "auto" : "manual")
            }
            continue
          }

          if (event.type === "session.model.selected") {
            const model = data.model ?? data
            if (isRouterRef(model)) {
              // Our own reset, or the user explicitly choosing auto.
              remember(sessionID, "auto")
              cancel(sessionID)
            } else if (turnActive.has(sessionID)) {
              // The router's pick during admission.
              if (origins.get(sessionID) !== "manual") remember(sessionID, "auto")
            } else {
              // Ambiguous: resolved at the next prompt (admission window).
              pendingSwitch.set(sessionID, Date.now())
            }
            continue
          }

          if (event.type === "session.deleted") {
            origins.delete(sessionID)
            pendingSwitch.delete(sessionID)
            turnActive.delete(sessionID)
            cancel(sessionID)
            persist()
            continue
          }

          if (event.type === "session.step.ended") {
            const finish = data.finish ?? data.rawFinish
            log("step.ended", sessionID, "finish=", finish)
            if (finish === "tool-calls") {
              cancel(sessionID) // still working; don't reset mid-turn
              continue
            }
            turnActive.delete(sessionID)
            schedule(sessionID)
          } else if (event.type === "session.idle") {
            schedule(sessionID)
          } else if (event.type === "session.status") {
            if (data.status?.type === "idle") schedule(sessionID)
            else cancel(sessionID)
          }
        }
      } catch (error) {
        if (!controller.signal.aborted) console.error("auto-model-reset subscribe:", error)
      }
    })()

    return async () => {
      controller.abort()
      for (const t of timers.values()) clearTimeout(t)
      timers.clear()
      if (saveTimer) {
        clearTimeout(saveTimer)
        const snapshot = {}
        for (const [id, origin] of [...origins].slice(-MAX_TRACKED)) snapshot[id] = origin
        await Promise.resolve(ctx.storage?.set?.(STORAGE_KEY, snapshot)).catch(() => {})
      }
    }
  },
}
