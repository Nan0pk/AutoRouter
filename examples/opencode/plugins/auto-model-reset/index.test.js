// Behaviour tests for auto-model-reset.
//
// Run: node --test examples/opencode/plugins/auto-model-reset/index.test.js
//
// The bug these lock down: the plugin used to reset *every* session to
// router/auto at the end of a turn, including sessions the user had started on
// a manually picked model.

import test from "node:test"
import assert from "node:assert/strict"

import plugin from "./index.js"

const AUTO = { providerID: "router", id: "auto" }
const ROUTER_ANY = { providerID: "router", id: "some-other" }
const MANUAL = { providerID: "opencode-go", id: "deepseek-v4.1-flash" }
const MANUAL2 = { providerID: "opencode-go", id: "claude-haiku-5-5" }

const tick = (ms = 5) => new Promise((resolve) => setTimeout(resolve, ms))

function memoryStorage() {
  const map = new Map()
  return {
    map,
    async get(key) {
      return map.get(key)
    },
    async set(key, value) {
      map.set(key, value)
    },
    async remove(key) {
      map.delete(key)
    },
  }
}

function harness(options = {}) {
  const models = new Map()
  const parents = new Map()
  const switched = []
  const handlers = {}
  const queue = []
  const waiters = []
  let closed = false

  const emit = (event) => {
    // Resolve a waiting iterator *with* the event; resolving without it would
    // drop the event on the floor.
    const waiter = waiters.shift()
    if (waiter) waiter(event)
    else queue.push(event)
  }

  const ctx = {
    options,
    storage: options.storage ?? memoryStorage(),
    model: {
      async default() {
        return { data: { providerID: "router", modelID: "auto" } }
      },
    },
    session: {
      async hook(name, fn) {
        handlers[name] = fn
        return { dispose() {} }
      },
      async get({ sessionID }) {
        return { id: sessionID, model: models.get(sessionID), parentID: parents.get(sessionID) }
      },
      async switchModel({ sessionID, model }) {
        switched.push({ sessionID, model })
        models.set(sessionID, model)
        emit({ type: "session.model.selected", data: { sessionID, model } })
      },
    },
    event: {
      subscribe({ signal }) {
        return (async function* () {
          while (!closed && !signal?.aborted) {
            if (queue.length) {
              yield queue.shift()
              continue
            }
            yield await new Promise((resolve) => waiters.push(resolve))
          }
        })()
      },
    },
  }

  return {
    ctx,
    models,
    parents,
    switched,
    handlers,
    emit,
    close() {
      closed = true
      while (waiters.length) waiters.shift()(undefined)
    },
  }
}

async function emit(env, event) {
  env.emit(event)
  await tick()
}

async function prompt(env, sessionID) {
  assert.ok(env.handlers.prompt, "plugin registered a prompt hook")
  await env.handlers.prompt({ sessionID, prompt: { text: "hello" } })
  await tick()
}

async function created(env, id, model) {
  if (model) env.models.set(id, model)
  await emit(env, { type: "session.created", data: { session: { id, model } } })
}

async function selected(env, id, model) {
  env.models.set(id, model)
  await emit(env, { type: "session.model.selected", data: { sessionID: id, model } })
}

async function stepEnded(env, id, finish = "stop") {
  await emit(env, { type: "session.step.ended", data: { sessionID: id, finish } })
}

function setup(options = {}) {
  const env = harness(options)
  return plugin.setup(env.ctx).then((dispose) => {
    env.dispose = dispose
    return env
  })
}

test("auto session: resets to router/auto after a terminal step", async () => {
  const env = await setup()
  await created(env, "s1")
  await prompt(env, "s1")
  await selected(env, "s1", MANUAL) // the router's pick during admission
  await stepEnded(env, "s1")
  await tick(20)

  assert.equal(env.switched.length, 1)
  assert.deepEqual(env.switched[0].model, AUTO)
  await env.dispose()
  env.close()
})

test("manual session: a model picked before the first prompt is never reset", async () => {
  // This is the reported bug.
  const env = await setup({ admissionWindowMs: 20 })
  await created(env, "s2")
  await selected(env, "s2", MANUAL) // user picks a model after the session opens
  await tick(40) // they take a moment before typing
  await prompt(env, "s2")
  await stepEnded(env, "s2")
  await tick(20)

  assert.equal(env.switched.length, 0, "must not undo a manual model choice")
  await env.dispose()
  env.close()
})

test("manual session: mid-conversation switch is respected", async () => {
  const env = await setup({ admissionWindowMs: 20 })
  await created(env, "s3")
  await prompt(env, "s3")
  await selected(env, "s3", MANUAL) // the router's pick for this turn
  await stepEnded(env, "s3")
  await tick(20)
  assert.equal(env.switched.length, 1, "first auto turn resets as usual")

  // Between turns the user overrides the model (no turn active).
  await selected(env, "s3", MANUAL2)
  await tick(40)
  await prompt(env, "s3")
  await stepEnded(env, "s3")
  await tick(20)
  assert.equal(env.switched.length, 1, "the override must survive the turn")

  // And the following turn stays manual too.
  await prompt(env, "s3")
  await stepEnded(env, "s3")
  await tick(20)
  assert.equal(env.switched.length, 1)
  await env.dispose()
  env.close()
})

test("router switch just before the prompt hook still counts as auto", async () => {
  // The router plugin may be listed first, so its admission switch can land
  // before this plugin's prompt hook runs.
  const env = await setup({ admissionWindowMs: 50 })
  await created(env, "s4")
  await selected(env, "s4", MANUAL) // router switch during admission
  await prompt(env, "s4") // immediately after
  await stepEnded(env, "s4")
  await tick(20)

  assert.equal(env.switched.length, 1)
  assert.deepEqual(env.switched[0].model, AUTO)
  await env.dispose()
  env.close()
})

test("unknown pre-existing session on a concrete model is left alone", async () => {
  const env = await setup()
  env.models.set("s5", MANUAL) // loaded before the plugin: no created/selected event
  await prompt(env, "s5")
  await stepEnded(env, "s5")
  await tick(20)

  assert.equal(env.switched.length, 0)
  await env.dispose()
  env.close()
})

test("child sessions are not reset when onlyRoot is on", async () => {
  const env = await setup()
  await created(env, "parent")
  env.parents.set("child", "parent")
  env.models.set("child", AUTO)
  await prompt(env, "child")
  await stepEnded(env, "child")
  await tick(20)

  assert.equal(env.switched.length, 0)
  await env.dispose()
  env.close()
})

test("a new prompt cancels a pending reset", async () => {
  const env = await setup({ delayMs: 50 })
  await created(env, "s7")
  await prompt(env, "s7")
  await stepEnded(env, "s7")
  await prompt(env, "s7") // still working
  await tick(80)

  assert.equal(env.switched.length, 0)
  await env.dispose()
  env.close()
})

test("tool-call steps do not reset mid-turn", async () => {
  const env = await setup()
  await created(env, "s8")
  await prompt(env, "s8")
  await stepEnded(env, "s8", "tool-calls")
  await tick(20)

  assert.equal(env.switched.length, 0)
  await env.dispose()
  env.close()
})

test("origin survives a plugin reload (storage)", async () => {
  const storage = memoryStorage()
  const first = await setup({ admissionWindowMs: 20, storage })
  await created(first, "s9")
  await selected(first, "s9", MANUAL)
  await tick(40)
  await prompt(first, "s9")
  await stepEnded(first, "s9")
  await tick(20)
  assert.equal(first.switched.length, 0)
  await first.dispose()
  first.close()

  const second = await setup({ admissionWindowMs: 20, storage })
  await prompt(second, "s9")
  await stepEnded(second, "s9")
  await tick(20)
  assert.equal(second.switched.length, 0, "a reload must not resurrect the bug")
  await second.dispose()
  second.close()
})

test("explicitly choosing router/auto re-enables resets", async () => {
  const env = await setup({ admissionWindowMs: 20 })
  await created(env, "s10")
  await selected(env, "s10", MANUAL)
  await tick(40)
  await prompt(env, "s10")
  await stepEnded(env, "s10")
  await tick(20)
  assert.equal(env.switched.length, 0)

  await selected(env, "s10", AUTO) // user goes back to auto
  await prompt(env, "s10")
  await selected(env, "s10", MANUAL2)
  await stepEnded(env, "s10")
  await tick(20)
  assert.equal(env.switched.length, 1)
  assert.deepEqual(env.switched[0].model, AUTO)
  await env.dispose()
  env.close()
})

test("router/* variants still count as auto", async () => {
  const env = await setup()
  await created(env, "s11", ROUTER_ANY)
  await prompt(env, "s11")
  await selected(env, "s11", MANUAL)
  await stepEnded(env, "s11")
  await tick(20)
  assert.equal(env.switched.length, 1)
  await env.dispose()
  env.close()
})
