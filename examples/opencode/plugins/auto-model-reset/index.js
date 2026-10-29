// auto-model-reset
//
// Returns a session's model to `router/auto` when a turn finishes, so a
// conversation is never left "stuck" on the last model the router picked as its
// main model. The next prompt then re-routes through the hierarchy.
//
// Why `session.step.ended` and not `session.idle`?
//   The server does not deliver `session.idle` / `session.status` to plugin
//   subscribers — they are ephemeral client events. `session.step.ended` *is*
//   delivered. We only reset on a terminal step (finish !== "tool-calls") so the
//   model is never swapped mid-turn.
//
// Options (opencode.jsonc plugins[].options):
//   delayMs   - ms after a turn ends before resetting (default 0 = immediate).
//               Raise it (e.g. 1800000) to only reset after long inactivity and
//               keep an active conversation on one warm-cache model.
//   onlyRoot  - skip subagent child sessions (default true).
//   debug     - append diagnostics to the debug file (default false).
//   debugFile - debug log path (default: <tmp>/auto-model-reset.log).

import { appendFileSync } from "node:fs"
import { tmpdir } from "node:os"
import { join } from "node:path"

const ROUTER = { providerID: "router", id: "auto" }

export default {
  id: "auto-model-reset",

  async setup(ctx) {
    const opts = ctx.options || {}
    const delayMs = typeof opts.delayMs === "number" ? Math.max(0, opts.delayMs) : 0
    const onlyRoot = opts.onlyRoot !== false
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

    const timers = new Map()
    const controller = new AbortController()

    const cancel = (sessionID) => {
      const t = timers.get(sessionID)
      if (t) {
        clearTimeout(t)
        timers.delete(sessionID)
      }
    }

    const reset = async (sessionID) => {
      try {
        const session = await ctx.session.get({ sessionID })
        if (!session || !session.model) return
        log("reset-check", sessionID, "model=", JSON.stringify(session.model), "parent=", session.parentID ?? null)
        if (session.model.providerID === ROUTER.providerID) return
        if (onlyRoot && session.parentID) return
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

    // A new prompt means the session is active again: cancel any pending reset.
    await ctx.session.hook("prompt", (event) => cancel(String(event.sessionID)))

    log("setup delayMs=", delayMs, "onlyRoot=", onlyRoot)

    void (async () => {
      try {
        for await (const event of ctx.event.subscribe({
          types: ["session.step.ended", "session.idle", "session.status"],
          signal: controller.signal,
        })) {
          const sessionID = String(event.data?.sessionID ?? "")
          if (!sessionID) continue

          if (event.type === "session.step.ended") {
            const finish = event.data.finish ?? event.data.rawFinish
            log("step.ended", sessionID, "finish=", finish)
            if (finish === "tool-calls") {
              cancel(sessionID) // still working; don't reset mid-turn
              continue
            }
            schedule(sessionID)
          } else if (event.type === "session.idle") {
            schedule(sessionID)
          } else if (event.type === "session.status") {
            if (event.data.status?.type === "idle") schedule(sessionID)
            else cancel(sessionID)
          }
        }
      } catch (error) {
        if (!controller.signal.aborted) console.error("auto-model-reset subscribe:", error)
      }
    })()

    return () => {
      controller.abort()
      for (const t of timers.values()) clearTimeout(t)
      timers.clear()
    }
  },
}
