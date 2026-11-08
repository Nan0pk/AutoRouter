"""Native Hermes routing. Core retains all agent execution and tool permissions."""
import json
import os
import threading
import time
from pathlib import Path

from .policy import MAX_CATALOG_AGE

try:
    from .classifier import classify
except ImportError:
    from scripts.classify import classify


def active_home():
    from hermes_constants import get_hermes_home
    return Path(get_hermes_home())


def read_policy():
    return json.loads((active_home()/"autorouter/policy.json").read_text())


class Router:
    def __init__(self, policy, log=None):
        self.policy = policy
        self.rows = {r["id"]: r for r in policy["models"]}
        if len(self.rows) != len(policy["models"]) or any(policy.get(k) not in self.rows for k in ("routine", "specialist")) or policy["routine"] == policy["specialist"]:
            raise ValueError("AutoRouter needs two distinct configured model routes; rerun setup")
        self.log = log
        self.states = {}; self.current = {}; self.lock = threading.RLock()

    def event(self, kind, **kwargs):
        if not self.log:
            return
        allowed = ("session_id", "parent_session_id", "turn_id", "task_id", "model", "provider",
                   "response_model", "api_call_count", "subagent_id", "state", "reason", "usage")
        event = {"event": kind, "at": time.time(), **{k: kwargs[k] for k in allowed if k in kwargs}}
        data = (json.dumps(event, default=str)+"\n").encode()
        fd = os.open(self.log, os.O_WRONLY|os.O_APPEND|os.O_CREAT|os.O_NOFOLLOW, 0o600)
        try:
            os.write(fd, data)
        finally:
            os.close(fd)

    def pre_turn(self, session_id="", turn_id="", model="", user_message="", parent_session_id="", **kwargs):
        if self.policy["mode"] == "free" and time.time()-self.policy["created_at"] > MAX_CATALOG_AGE:
            return {"action": "block", "message": "AutoRouter free-price data expired. Rerun AutoRouter setup before continuing."}
        routine = self.rows[self.policy["routine"]]
        child = bool(parent_session_id)
        manual = model not in (routine["model"], routine["id"])
        text = user_message if isinstance(user_message, str) else json.dumps(user_message)
        required = not child and not manual and classify(text)["delegate"]
        with self.lock:
            if len(self.states) > 1024:
                oldest = next(iter(self.states)); self.states.pop(oldest)
            self.states[(session_id, turn_id)] = {"required": required, "forced": False, "parent_session_id": parent_session_id}
            self.current[session_id] = turn_id
        self.event("turn", session_id=session_id, turn_id=turn_id, model=model,
                   parent_session_id=parent_session_id, state="child" if child else "manual" if manual else "specialist" if required else "routine")
        if child:
            return None
        if manual:
            return {"context": "AutoRouter automatic routing is inactive for this manually selected model. Your manual choice is preserved."}
        if required:
            return {"context": "AutoRouter: this task requires the specialist. First call delegate_task with tasks=[{goal: the user's full task, context: all relevant conversation facts and file paths, route: specialist}]. Preserve the user's work. Use the specialist result to answer; do not redo completed work. If delegation fails, report the failure rather than silently completing on another model."}
        return {"context": "AutoRouter: handle routine work directly. If blocked, use delegate_task with route=specialist and include the blocker, attempts, file paths and relevant context."}

    def state(self, session_id, turn_id):
        return self.states.get((session_id, turn_id or self.current.get(session_id, "")), {})

    def llm_request(self, request, session_id="", turn_id="", **kwargs):
        with self.lock:
            state = self.state(session_id, turn_id)
            if not state.get("required") or state.get("forced"):
                return None
            state["forced"] = True
        # Unknown/missing delegation tools fail visibly at the provider instead of
        # quietly returning an unrestricted request. Setup verifies this tool exists.
        result = dict(request)
        mode = kwargs.get("api_mode", "chat_completions")
        if mode == "anthropic_messages":
            result["tool_choice"] = {"type": "tool", "name": "delegate_task"}
        elif mode in ("codex_responses", "responses"):
            result["tool_choice"] = {"type": "function", "name": "delegate_task"}
        else:
            result["tool_choice"] = {"type": "function", "function": {"name": "delegate_task"}}
        return {"request": result, "source": "autorouter", "reason": "complex root requires specialist"}

    def tool_request(self, tool_name, args, session_id="", turn_id="", **kwargs):
        if tool_name != "delegate_task":
            return None
        with self.lock:
            required = self.state(session_id, turn_id).get("required")
        if required and isinstance(args.get("tasks"), list):
            return {"args": {**args, "tasks": [{**t, "route": "specialist"} for t in args["tasks"]]},
                    "source": "autorouter", "reason": "preserve root escalation"}
        return None

    def route_task(self, task):
        role = task.get("route") or ("specialist" if classify(task.get("goal", ""))["delegate"] else "routine")
        if role not in ("routine", "specialist"):
            raise ValueError("Choose the routine or specialist AutoRouter route")
        if self.policy["mode"] == "free" and time.time()-self.policy["created_at"] > MAX_CATALOG_AGE:
            raise ValueError("Free pricing expired; rerun setup")
        row = self.rows[self.policy[role]]
        return {"provider": row["runtime_provider"], "model": row["model"],
                "label": role, "fallback_providers": []}

    def api_event(self, **kwargs):
        state = self.state(kwargs.get("session_id", ""), kwargs.get("turn_id", ""))
        self.event("api", **{**kwargs, "parent_session_id": state.get("parent_session_id", "")})


def register(ctx):
    from agent.delegation_provider import DelegationProvider, DelegationRouteError
    try:
        router = Router(read_policy(), active_home()/"autorouter/events.jsonl")
    except (OSError, ValueError, KeyError, TypeError):
        ctx.register_hook("pre_llm_call", lambda **kw: {"action": "block", "message": "AutoRouter policy is missing or invalid; rerun setup or undo."})
        return

    class AutoRouterBackend(DelegationProvider):
        name = "autorouter"

        def task_parameters(self, config):
            return {"route": {"type": "string", "enum": ["routine", "specialist"],
                              "description": "Use specialist for difficult work, security/data risks, or a blocker; routine for simple subtasks."}}

        def description(self, config):
            return "AutoRouter chooses the configured model for the route. Include full task context; never invent provider or model parameters."

        def route_task(self, task, config):
            try:
                return router.route_task(task)
            except ValueError as exc:
                raise DelegationRouteError(str(exc)) from None

    ctx.register_delegation_provider(AutoRouterBackend())
    ctx.register_hook("pre_llm_call", router.pre_turn)
    ctx.register_middleware("llm_request", router.llm_request)
    ctx.register_middleware("tool_request", router.tool_request)
    ctx.register_hook("post_api_request", router.api_event)
    ctx.register_hook("subagent_start", lambda **kw: router.event("child_start", **kw))
    ctx.register_hook("subagent_stop", lambda **kw: router.event("child_stop", **kw))
