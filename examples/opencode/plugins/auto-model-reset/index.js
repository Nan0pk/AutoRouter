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
//   1. `session.created` with a `router/*` model -> auto. With no model it says
//      nothing yet: the default may be auto or concrete, and the router may not
//      have registered its provider; the first prompt resolves it.
//   2. A model switch to `router/*` (ours, or yours) -> auto.
//   3. A switch to a concrete model while a turn is active -> the router -> auto.
//   4. The TUI pins a model by calling switchModel() immediately *before*
//      session.prompt(), so a manual pick always lands just before the prompt
//      hook too. When this plugin is listed before the router plugin, its hook
//      runs before the router switches: a `router/*` model means "nothing was
//      pinned, the router is about to route" (-> auto), and a concrete model
//      means "you pinned it" (-> manual). That case is exact.
//   5. When this plugin is listed *after* the router, a concrete model at the
//      prompt hook is ambiguous: the router may have switched milliseconds
//      earlier. There the timing window is the only signal (a router switch is
//      ~0 ms old, a human pick is usually older). Listing this plugin first
//      avoids the guess entirely; the example does.
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
//   admissionWindowMs - fallback only, used when this plugin is listed *after*
//                       the router plugin: how fresh a concrete switch must be
//                       to count as the router's admission switch (default
//                       150). Ignored once the plugin has observed a prompt
//                       where the session was still on `router/*`, which proves
//                       it runs before the router.
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
      typeof opts.admissionWindowMs === "number" ? Math.max(0, opts.admissionWindowMs) : 150
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

    // Set once this plugin has seen a prompt while the session was still on a
    // router/* model: proof that its hook runs before the router's, so a
    // concrete model at admission can only be a user pick.
    let orderFirst = false
    let orderLogged = false

    // What does a session with no model run on? Resolved at prompt time, not at
    // setup: when this plugin is listed first, the router's provider has not
    // been registered yet and the default lookup can come back in another shape.
    const isDefaultAuto = async () => {
      try {
        const current = await ctx.model.default()
        const ref = current?.data ?? current
        return !ref || isRouterRef(ref)
      } catch {
        return true // documented setup is an auto default
      }
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

      let model
      try {
        model = (await ctx.session.get({ sessionID }))?.model
      } catch {
        /* treat as unknown below */
      }

      log(
        "prompt",
        sessionID,
        "model=",
        JSON.stringify(model ?? null),
        "orderFirst=",
        orderFirst,
        "pendingAge=",
        switchedAt === undefined ? "none" : now - switchedAt,
      )

      if (!model) {
        // No model yet: we are ahead of the router, and the session will use
        // whatever the default is.
        orderFirst = true
        remember(sessionID, (await isDefaultAuto()) ? "auto" : "manual")
      } else if (isRouterRef(model)) {
        // The session is explicitly on router/*. We run before the router.
        orderFirst = true
        if (!orderLogged) {
          orderLogged = true
          log("order", "reset plugin runs before the router: manual pins are classified exactly")
        }
        remember(sessionID, "auto")
      } else if (orderFirst) {
        // We run before the router; a concrete model here was pinned by the user
        // (the router only switches a session that is on router/*).
        remember(sessionID, "manual")
      } else if (switchedAt !== undefined && now - switchedAt <= admissionWindowMs) {
        // Listed after the router: a switch this fresh is its admission switch.
        if (!orderLogged) {
          orderLogged = true
          log("order", "reset plugin runs after the router: using the admission window")
        }
        if (origins.get(sessionID) !== "manual") remember(sessionID, "auto")
      } else {
        remember(sessionID, "manual")
      }
      pendingSwitch.delete(sessionID)
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
            log("created", sessionID, "model=", JSON.stringify(session?.model ?? null))
            // Creation with no model says nothing yet: the default may be auto
            // or concrete, and the router may not have registered its provider.
            // Leave the origin unknown and decide at the first prompt.
            if (!origins.has(sessionID) && session?.model) {
              remember(sessionID, isRouterRef(session.model) ? "auto" : "manual")
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
