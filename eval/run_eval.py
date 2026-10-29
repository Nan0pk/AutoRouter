#!/usr/bin/env python3
"""Evaluation harness: fixed-workhorse vs fixed-strong vs AutoRouter.

The point is to make the routing claim testable, not to assert it. This script
runs the same task set through the arms, verifies declarative checks, and
reports success, cost/quota, latency, retries/escalations and routing overhead
side by side -- including the tasks each arm failed.

Fail closed
-----------
A run is **only** valid evidence when every task actually executed and
returned usable usage data. Adapter failures are recorded as
`infrastructure_error` and **never** counted as model failures, and any such
failure makes the whole report `valid_evidence: false`. The executor name is
not evidence: `--executor command` does not imply a valid run.

Executors
---------
stub     Deterministic, synthetic. Applies canned patches and canned token and
         latency figures. It exists to test the harness in CI. Its results
         encode assumptions, so they are NOT evidence.
command  The real path. Runs one command per attempt with the prompt on stdin:

             AUTOROUTER_MODEL=provider/model \
             AUTOROUTER_TASK_ID=mech-rename \
             AUTOROUTER_WORKDIR=/tmp/... \
             $AUTOROUTER_EVAL_CMD

         The command edits files in its working directory and must print one
         JSON object on stdout:

             {"tokens_in": 8123, "tokens_out": 640, "latency_ms": 9100,
              "retries": 0, "escalations": 0, "trace": ["provider/model"]}

         Missing/zero usage, a non-zero exit, or unparseable stdout is an
         infrastructure error. The harness verifies the files afterwards; it
         never executes model output.

Arms
----
    fixed_workhorse        T2, one attempt
    fixed_workhorse_retry  T2, one retry on the same model (no escalation)
    fixed_strong           T5, one attempt
    autorouter             gate-chosen tier, one escalation when the gate allows

`fixed_workhorse_retry` exists so the benefit of *routing* can be separated
from the benefit of a *second attempt*.

Usage
-----
    python3 eval/run_eval.py --self-test
    AUTOROUTER_EVAL_CMD='./my-agent' python3 eval/run_eval.py --executor command \
        --out eval/results/run-2026-10-29.json
"""
import argparse
import hashlib
import json
import os
import shutil
import statistics
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

EVAL = Path(__file__).resolve().parent
ROOT = EVAL.parent
sys.path.insert(0, str(ROOT / "scripts"))

from score_models import request_cost  # noqa: E402

ARMS = ("fixed_workhorse", "fixed_workhorse_retry", "fixed_strong", "autorouter")
RETRY_ARMS = ("fixed_workhorse_retry", "autorouter")
ARM_MODEL = {
    "fixed_workhorse": ("T2", "workhorse"),
    "fixed_workhorse_retry": ("T2", "workhorse"),
    "fixed_strong": ("T5", "strong"),
}
EXECUTORS = ("stub", "command")

OUTCOMES = ("success", "model_failure", "infrastructure_error")


def load_json(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def load_tasks():
    tasks = []
    with open(EVAL / "tasks.jsonl", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                tasks.append(json.loads(line))
    return tasks


def task_set_hash():
    return hashlib.sha256((EVAL / "tasks.jsonl").read_bytes()).hexdigest()


def load_pins():
    pins = load_json(EVAL / "pins.json")
    actual = task_set_hash()
    if pins["task_set_sha256"] != actual:
        raise SystemExit(
            "ERROR: eval/tasks.jsonl changed since pins.json was written.\n"
            f"  pins:   {pins['task_set_sha256']}\n  actual: {actual}\n"
            "Update pins.json (models, settings, cost model) in the same commit as the task change."
        )
    return pins


# --------------------------------------------------------------------------- #
# verification
# --------------------------------------------------------------------------- #
def check_one(check, workdir):
    path = Path(workdir) / check["file"]
    if not path.exists():
        return False, f"{check['file']}: missing"
    text = path.read_text(encoding="utf-8", errors="replace")
    kind = check["type"]
    if kind == "contains":
        ok = check["value"] in text
    elif kind == "not_contains":
        ok = check["value"] not in text
    elif kind == "regex":
        import re

        ok = re.search(check["value"], text, re.M | re.S) is not None
    elif kind == "compiles":
        try:
            compile(text, str(path), "exec")
            ok = True
        except SyntaxError as e:
            return False, f"{check['file']}: does not compile ({e.msg})"
    else:
        return False, f"unknown check type {kind!r}"
    if ok:
        return True, None
    return False, f"{check['file']}: {kind} {check['value']!r} failed"


def verify(workdir, checks):
    passed = 0
    failures = []
    for check in checks:
        ok, reason = check_one(check, workdir)
        passed += ok
        if not ok:
            failures.append(reason)
    return passed, failures


# --------------------------------------------------------------------------- #
# executors
# --------------------------------------------------------------------------- #
def _ok(tokens_in, tokens_out, latency_ms, retries, escalations, trace):
    return {
        "status": "ok",
        "error": None,
        "usage_valid": True,
        "tokens_in": tokens_in,
        "tokens_out": tokens_out,
        "latency_ms": latency_ms,
        "reported_retries": retries,
        "reported_escalations": escalations,
        "trace": trace,
    }


def _adapter_error(message, latency_ms):
    return {
        "status": "error",
        "error": message,
        "usage_valid": False,
        "tokens_in": 0,
        "tokens_out": 0,
        "latency_ms": latency_ms,
        "reported_retries": 0,
        "reported_escalations": 0,
        "trace": [],
    }


def run_stub(task, role, pins, workdir):
    spec = pins["stub"]
    patch = load_json(EVAL / "stub_patches.json").get(task["id"], {})
    if role in patch.get("solvable_by", []):
        for name, body in patch.get("files", {}).items():
            (Path(workdir) / name).write_text(body, encoding="utf-8")
    tokens = spec["tokens"][role]
    return _ok(
        tokens["in"],
        tokens["out"],
        spec["latency_ms"][role],
        0,
        0,
        [f"stub:{role}"],
    )


def _valid_usage(payload):
    ti, to = payload.get("tokens_in"), payload.get("tokens_out")
    for value in (ti, to):
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            return False
    return (ti + to) > 0


def run_command(task, role, model, pins, workdir):
    cmd = os.environ.get("AUTOROUTER_EVAL_CMD")
    if not cmd:
        return _adapter_error("AUTOROUTER_EVAL_CMD is not set", 0)
    env = dict(os.environ)
    env.update(
        {
            "AUTOROUTER_MODEL": model,
            "AUTOROUTER_TASK_ID": task["id"],
            "AUTOROUTER_WORKDIR": str(workdir),
        }
    )
    started = time.monotonic()
    try:
        proc = subprocess.run(
            cmd,
            shell=True,
            cwd=workdir,
            env=env,
            input=task["prompt"],
            text=True,
            capture_output=True,
            timeout=pins["settings"]["timeout_s"],
        )
    except subprocess.TimeoutExpired:
        return _adapter_error(
            f"adapter timed out after {pins['settings']['timeout_s']}s",
            int((time.monotonic() - started) * 1000),
        )
    latency_ms = int((time.monotonic() - started) * 1000)

    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip().splitlines()
        tail = detail[-1][:300] if detail else ""
        return _adapter_error(f"adapter exited {proc.returncode}: {tail}", latency_ms)

    try:
        payload = json.loads(proc.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError):
        return _adapter_error("adapter printed no JSON result on stdout", latency_ms)
    if not isinstance(payload, dict):
        return _adapter_error("adapter result is not a JSON object", latency_ms)
    if not _valid_usage(payload):
        return _adapter_error(
            "adapter returned missing, non-integer or zero usage "
            f"(tokens_in={payload.get('tokens_in')!r}, tokens_out={payload.get('tokens_out')!r})",
            latency_ms,
        )
    return _ok(
        payload["tokens_in"],
        payload["tokens_out"],
        int(payload.get("latency_ms") or latency_ms),
        int(payload.get("retries") or 0),
        int(payload.get("escalations") or 0),
        payload.get("trace") or [model],
    )


def execute(executor, task, role, model, pins, workdir):
    if executor == "stub":
        return run_stub(task, role, pins, workdir)
    return run_command(task, role, model, pins, workdir)


# --------------------------------------------------------------------------- #
# behavioural tests (sandboxed -- this is the only place model-written code runs)
# --------------------------------------------------------------------------- #
SANDBOX_MODES = ("auto", "bwrap", "unshare", "none")


_SANDBOX_CACHE = {}


def detect_sandbox(requested="auto"):
    """Return the strongest available mechanism, or 'none'. Never assume one."""
    if requested in _SANDBOX_CACHE:
        return _SANDBOX_CACHE[requested]
    if requested != "auto":
        _SANDBOX_CACHE[requested] = requested
        return requested
    if shutil.which("bwrap"):
        probe = subprocess.run(
            ["bwrap", "--unshare-all", "--die-with-parent", "--ro-bind", "/", "/",
             "--dev", "/dev", "--proc", "/proc", "/bin/true"],
            capture_output=True,
        )
        if probe.returncode == 0:
            _SANDBOX_CACHE[requested] = "bwrap"
            return "bwrap"
    if shutil.which("unshare"):
        probe = subprocess.run(["unshare", "-rn", "/bin/true"], capture_output=True)
        if probe.returncode == 0:
            _SANDBOX_CACHE[requested] = "unshare"
            return "unshare"
    _SANDBOX_CACHE[requested] = "none"
    return "none"


def sandbox_argv(mechanism, script, workdir):
    python = sys.executable
    if mechanism == "bwrap":
        return [
            "bwrap", "--unshare-all", "--die-with-parent", "--clearenv",
            "--ro-bind", "/", "/", "--dev", "/dev", "--proc", "/proc",
            "--bind", str(workdir), str(workdir),
            "--chdir", str(workdir),
            "--setenv", "PATH", "/usr/local/bin:/usr/bin:/bin",
            "--setenv", "HOME", str(workdir),
            python, str(script),
        ]
    if mechanism == "unshare":
        return ["unshare", "-rn", "--", python, str(script)]
    return [python, str(script)]


def run_behavior(task, workdir, pins, mechanism):
    """Run the task's behavioural checks against the current workspace.

    Returns a dict with status:
      ok             checks ran; see passed/total/checks
      skipped        no sandbox available; behaviour was NOT verified
      sandbox_error  the sandbox itself failed to start (infrastructure)
      broke          the script produced no result (the code under test broke it)
    """
    if not task.get("behavior"):
        return {"status": "not_applicable", "reason": None, "checks": [], "passed": 0, "total": 0}
    script = EVAL / task["behavior"]
    if mechanism == "none":
        return {"status": "skipped", "reason": "no sandbox available (bwrap/unshare)",
                "checks": [], "passed": 0, "total": 0}
    cmd = sandbox_argv(mechanism, script, workdir)
    try:
        proc = subprocess.run(
            cmd,
            cwd=workdir,
            capture_output=True,
            text=True,
            timeout=pins["settings"].get("behavior_timeout_s", 60),
        )
    except subprocess.TimeoutExpired:
        return {"status": "broke", "reason": "behavioural tests timed out",
                "checks": [], "passed": 0, "total": 0}
    payload = None
    try:
        payload = json.loads(proc.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError):
        payload = None
    if payload is None:
        stderr = (proc.stderr or "").strip()
        if mechanism != "none" and (
            "bwrap:" in stderr or "unshare:" in stderr or "Operation not permitted" in stderr
        ):
            return {"status": "sandbox_error", "reason": stderr.splitlines()[-1][:300],
                    "checks": [], "passed": 0, "total": 0}
        tail = stderr.splitlines()[-1][:300] if stderr else "no output"
        return {"status": "broke", "reason": tail, "checks": [], "passed": 0, "total": 0}
    checks = payload.get("checks", [])
    return {
        "status": "ok",
        "reason": None,
        "checks": checks,
        "passed": sum(1 for c in checks if c.get("ok")),
        "total": len(checks),
    }


# --------------------------------------------------------------------------- #
# routing
# --------------------------------------------------------------------------- #
def gate(prompt):
    """Call the real classifier and time it: routing overhead is measured, not assumed."""
    started = time.monotonic()
    proc = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "classify.py"), "--json", prompt],
        capture_output=True,
        text=True,
        timeout=30,
    )
    elapsed_ms = (time.monotonic() - started) * 1000
    if proc.returncode != 0:
        raise SystemExit(f"ERROR: classify.py failed: {proc.stderr}")
    return json.loads(proc.stdout), elapsed_ms


def route(prompt, pins):
    """Gate-based policy: pick a tier; complex work starts where the gate points."""
    decision, routing_ms = gate(prompt)
    tier = "T2"
    if decision["route"] == "main":
        tier = "T2"
    elif decision["tier_hint"] in ("T1", "T2"):
        tier = decision["tier_hint"]
    elif decision["tier_hint"] == "T3+":
        tier = "T3"
    pick = pins["tiers"][tier]
    return {
        "tier": tier,
        "model": pick["model"],
        "role": pick["role"],
        "class": decision["class"],
        "delegate": decision["delegate"],
        "escalate_to": "T5" if decision["delegate"] else None,
        "routing_ms": routing_ms,
    }


def retry_plan(arm, failures, decision, pins):
    """One retry, or none. Fixed arms exist so this is a controlled variable."""
    if not failures or arm not in RETRY_ARMS:
        return None
    if arm == "fixed_workhorse_retry":
        tier, role = ARM_MODEL["fixed_workhorse"]
        return {
            "model": pins["tiers"][tier]["model"],
            "role": role,
            "tier": tier,
            "escalation": False,
        }
    if decision and decision["escalate_to"]:
        tier = decision["escalate_to"]
        return {
            "model": pins["tiers"][tier]["model"],
            "role": pins["tiers"][tier]["role"],
            "tier": tier,
            "escalation": True,
        }
    return None


def price(model, tokens_in, tokens_out, pins):
    m = pins["models"][model]
    hit = pins["cost_model"]["cache_hit_rate"]
    return request_cost(
        tokens_in,
        tokens_out,
        hit,
        m["input_cost"],
        m["output_cost"],
        m["cache_read_cost"],
        m["cache_write_cost"],
    )


# --------------------------------------------------------------------------- #
# running
# --------------------------------------------------------------------------- #
def run_task(arm, task, executor, pins, keep=None, sandbox="auto"):
    workdir = Path(keep or tempfile.mkdtemp(prefix="autorouter-eval-")) / task["id"]
    if workdir.exists():
        shutil.rmtree(workdir)
    shutil.copytree(EVAL / task["fixture"], workdir)

    routing_ms, decision = 0.0, None
    if arm == "autorouter":
        decision = route(task["prompt"], pins)
        model, role, tier = decision["model"], decision["role"], decision["tier"]
        routing_ms = decision["routing_ms"]
    else:
        tier, role = ARM_MODEL[arm]
        model = pins["tiers"][tier]["model"]

    attempts = []
    retries = escalations = 0
    adapter_error = None
    behavior = {"status": "not_applicable", "reason": None, "checks": [], "passed": 0, "total": 0}

    def attempt(model_id, role_name, tier_name):
        res = execute(executor, task, role_name, model_id, pins, workdir)
        attempts.append(
            {
                "model": model_id,
                "tier": tier_name,
                "role": role_name,
                "status": res["status"],
                "error": res["error"],
                "tokens_in": res["tokens_in"],
                "tokens_out": res["tokens_out"],
                "latency_ms": res["latency_ms"],
                "reported_retries": res["reported_retries"],
                "reported_escalations": res["reported_escalations"],
                "trace": res["trace"],
            }
        )
        return res

    mechanism = detect_sandbox(sandbox)

    def judge():
        """Static checks plus sandboxed behaviour. Returns (passed, failures, behavior)."""
        p, f = verify(workdir, task["checks"])
        b = run_behavior(task, workdir, pins, mechanism)
        p += b["passed"]
        f = list(f) + [f"behavior:{c['name']}: {c['detail']}" for c in b["checks"] if not c.get("ok")]
        if b["status"] == "broke":
            f.append(f"behavior: test runner broke: {b['reason']}")
        return p, f, b

    first = attempt(model, role, tier)
    passed, failures = 0, []

    if first["status"] != "ok":
        # Fail closed: a broken adapter is an infrastructure error, not a model
        # failure, and it is never retried (that would hide it).
        adapter_error = first["error"]
        outcome = "infrastructure_error"
    else:
        passed, failures, behavior = judge()
        if behavior["status"] == "sandbox_error":
            adapter_error = f"sandbox failed: {behavior['reason']}"
            outcome = "infrastructure_error"
            failures = [adapter_error]
            passed = 0
        else:
            plan = retry_plan(arm, failures, decision, pins)
            if plan:
                retries += 1
                second = attempt(plan["model"], plan["role"], plan["tier"])
                if second["status"] != "ok":
                    adapter_error = second["error"]
                    outcome = "infrastructure_error"
                    failures = [f"retry adapter error: {second['error']}"]
                    passed = 0
                else:
                    if plan["escalation"]:
                        escalations += 1
                    passed, failures, behavior = judge()
                    outcome = "success" if not failures else "model_failure"
            else:
                outcome = "success" if not failures else "model_failure"

    verified = behavior["status"] in ("ok", "not_applicable")

    ok_attempts = [a for a in attempts if a["status"] == "ok"]
    tokens_in = sum(a["tokens_in"] for a in attempts)
    tokens_out = sum(a["tokens_out"] for a in attempts)
    cost = sum(price(a["model"], a["tokens_in"], a["tokens_out"], pins) for a in ok_attempts)
    latency_ms = sum(a["latency_ms"] for a in attempts) + int(routing_ms)

    # Executor-reported counts are kept alongside harness-driven ones so an
    # adapter that retries internally cannot hide that work.
    reported_retries = sum(a["reported_retries"] for a in attempts)
    reported_escalations = sum(a["reported_escalations"] for a in attempts)

    record = {
        "task": task["id"],
        "category": task["category"],
        "arm": arm,
        "outcome": outcome,
        "success": outcome == "success",
        "checks_passed": passed,
        "checks_total": len(task["checks"]) + behavior["total"],
        "failures": failures,
        "adapter_error": adapter_error,
        "behavior": {
            "status": behavior["status"],
            "reason": behavior["reason"],
            "passed": behavior["passed"],
            "total": behavior["total"],
        },
        "sandbox": mechanism,
        "sandbox_requested": sandbox,
        "verified": verified,
        "model": model,
        "tier": tier,
        "attempts": attempts,
        "tokens_in": tokens_in,
        "tokens_out": tokens_out,
        "cost_usd": round(cost, 6),
        "latency_ms": latency_ms,
        "routing_ms": round(routing_ms, 2),
        "retries": retries + reported_retries,
        "escalations": escalations + reported_escalations,
        "harness_retries": retries,
        "harness_escalations": escalations,
        "reported_retries": reported_retries,
        "reported_escalations": reported_escalations,
        "gate": None if decision is None else {
            "class": decision["class"],
            "tier": decision["tier"],
            "delegate": decision["delegate"],
        },
    }
    if keep is None:
        shutil.rmtree(workdir.parent, ignore_errors=True)
    return record


def summarise(records, arm, pins):
    arm_records = [r for r in records if r["arm"] == arm]
    n = len(arm_records)
    infra = [r for r in arm_records if r["outcome"] == "infrastructure_error"]
    scored = [r for r in arm_records if r["outcome"] != "infrastructure_error"]
    successes = sum(r["success"] for r in scored)
    checks_passed = sum(r["checks_passed"] for r in scored)
    checks_total = sum(r["checks_total"] for r in scored)
    cost = sum(r["cost_usd"] for r in scored)
    latencies = [r["latency_ms"] for r in scored]
    attempts = [a for r in scored for a in r["attempts"] if a["status"] == "ok"]
    quota = {}
    for a in attempts:
        q = quota.setdefault(
            a["model"],
            {"requests": 0, "req_per_month": pins["models"][a["model"]]["req_per_month"]},
        )
        q["requests"] += 1
    for q in quota.values():
        q["pct_of_monthly"] = round(100.0 * q["requests"] / q["req_per_month"], 4)
    return {
        "arm": arm,
        "tasks": n,
        "tasks_scored": len(scored),
        "infrastructure_errors": len(infra),
        "behavioral_verified": sum(1 for r in scored if r["verified"]),
        "behavioral_skipped": sum(
            1 for r in scored if r.get("behavior", {}).get("status") == "skipped"
        ),
        "successes": successes,
        "success_rate": round(successes / len(scored), 4) if scored else 0.0,
        "success_rate_of_all_tasks": round(successes / n, 4) if n else 0.0,
        "partial_check_rate": round(checks_passed / checks_total, 4) if checks_total else 0.0,
        "total_cost_usd": round(cost, 6),
        "cost_per_success_usd": round(cost / successes, 6) if successes else None,
        "mean_latency_ms": round(statistics.mean(latencies)) if latencies else 0,
        "p95_latency_ms": round(sorted(latencies)[max(0, int(0.95 * len(latencies)) - 1)]) if latencies else 0,
        "retries": sum(r["retries"] for r in scored),
        "escalations": sum(r["escalations"] for r in scored),
        "harness_retries": sum(r["harness_retries"] for r in scored),
        "harness_escalations": sum(r["harness_escalations"] for r in scored),
        "reported_retries": sum(r["reported_retries"] for r in scored),
        "routing_overhead_ms_total": round(sum(r["routing_ms"] for r in scored), 2),
        "routing_overhead_ms_mean": round(statistics.mean([r["routing_ms"] for r in scored]), 2) if scored else 0.0,
        "quota": quota,
        "max_quota_pct": round(max((q["pct_of_monthly"] for q in quota.values()), default=0.0), 4),
        "failures": [
            {"task": r["task"], "category": r["category"], "failures": r["failures"],
             "model": r["model"], "gate": r["gate"]}
            for r in scored if not r["success"]
        ],
        "adapter_errors": [
            {"task": r["task"], "error": r["adapter_error"]} for r in infra
        ],
    }


def compare(arms):
    out = {}
    base = arms.get("fixed_workhorse")
    if base is None:
        return out
    for name in ("fixed_workhorse_retry", "fixed_strong", "autorouter"):
        if name not in arms:
            continue
        cur = arms[name]
        out[f"{name}_vs_fixed_workhorse"] = {
            "success_delta_pp": round(100 * (cur["success_rate"] - base["success_rate"]), 2),
            "cost_delta_pct": (
                round(100 * (cur["total_cost_usd"] / base["total_cost_usd"] - 1), 2)
                if base["total_cost_usd"] else None
            ),
            "cost_per_success_delta_pct": (
                round(100 * (cur["cost_per_success_usd"] / base["cost_per_success_usd"] - 1), 2)
                if base["cost_per_success_usd"] and cur["cost_per_success_usd"] else None
            ),
        }
    return out


def build_report(records, pins, executor, arm_names=ARMS):
    arms = {arm: summarise(records, arm, pins) for arm in arm_names}

    reasons = []
    if executor == "stub":
        reasons.append("synthetic executor: canned patches and canned usage")
    adapter_failures = [r for r in records if r["outcome"] == "infrastructure_error"]
    if adapter_failures:
        reasons.append(
            f"{len(adapter_failures)} task run(s) failed in the adapter and were excluded from scoring"
        )
    mechanism = records[0]["sandbox"] if records else detect_sandbox()
    unverified = [
        r for r in records
        if r["outcome"] != "infrastructure_error"
        and r.get("behavior", {}).get("status") in ("skipped", "broke")
    ]
    if mechanism == "none" and records:
        reasons.append("behavioural tests skipped: no sandbox (bwrap/unshare) available")
    elif unverified:
        reasons.append(f"{len(unverified)} task run(s) were not behaviourally verified")
    valid = not reasons

    if executor == "stub":
        warning = (
            "SYNTHETIC RUN: stub executor. These numbers test the harness, not the router. "
            "Do not cite them as evidence."
        )
    elif not valid:
        warning = (
            "INVALID RUN: the adapter failed; results are excluded and this report is not "
            "evidence. Fix the adapter and re-run."
        )
    else:
        warning = "Measured run. Check eval/README.md for the protocol before comparing runs."

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "synthetic": executor == "stub",
        "valid_evidence": valid,
        "validity": {"valid": valid, "reasons": reasons},
        "warning": warning,
        "sandbox": {
            "mechanism": mechanism,
            "applies_to": pins["sandbox"]["applies_to"],
            "behavioral_tests_run": sum(
                1 for r in records if r.get("behavior", {}).get("status") == "ok"
            ),
            "behavioral_tests_skipped": sum(
                1 for r in records if r.get("behavior", {}).get("status") == "skipped"
            ),
        },
        "pins": {
            "task_set_sha256": pins["task_set_sha256"],
            "pinned_on": pins["pinned_on"],
            "models": pins["models"],
            "tiers": pins["tiers"],
            "settings": pins["settings"],
            "cost_model": pins["cost_model"],
        },
        "arms": arms,
        "comparison": compare(arms),
        "records": records,
    }


def render_markdown(report, arm_names=None):
    arm_names = arm_names or list(report["arms"])
    lines = []
    lines.append("# AutoRouter evaluation")
    lines.append("")
    lines.append(f"Generated {report['generated_at']} | task set `{report['pins']['task_set_sha256'][:12]}`")
    lines.append("")
    lines.append(f"> **{report['warning']}**")
    lines.append("")
    if not report["validity"]["valid"]:
        for reason in report["validity"]["reasons"]:
            lines.append(f"- not evidence: {reason}")
        lines.append("")
    lines.append(f"Sandbox for behavioural tests: **{report['sandbox']['mechanism']}** "
                 f"({report['sandbox']['behavioral_tests_run']} run, "
                 f"{report['sandbox']['behavioral_tests_skipped']} skipped)")
    lines.append("")
    lines.append("| arm | success | infra errors | verified | checks | cost $ | $/success | mean ms | p95 ms | retries | escalations | routing ms | max quota % |")
    lines.append("|---|---|---|---|---|---|---|---|---|---|---|---|---|")
    for arm in arm_names:
        a = report["arms"][arm]
        cps = "n/a" if a["cost_per_success_usd"] is None else f"{a['cost_per_success_usd']:.4f}"
        lines.append(
            f"| {arm} | {a['successes']}/{a['tasks_scored']} ({a['success_rate']:.0%}) | "
            f"{a['infrastructure_errors']} | {a['behavioral_verified']}/{a['tasks_scored']} | "
            f"{a['partial_check_rate']:.0%} | "
            f"{a['total_cost_usd']:.4f} | {cps} | "
            f"{a['mean_latency_ms']} | {a['p95_latency_ms']} | {a['retries']} | {a['escalations']} | "
            f"{a['routing_overhead_ms_mean']:.1f} | {a['max_quota_pct']:.3f} |"
        )
    lines.append("")
    lines.append("## Comparison vs fixed-workhorse")
    lines.append("")
    lines.append("`fixed_workhorse_retry` isolates the effect of a second attempt from the effect of routing.")
    lines.append("")
    lines.append("| comparison | success delta (pp) | cost delta | $/success delta |")
    lines.append("|---|---|---|---|")
    for key, c in report["comparison"].items():
        cost = "n/a" if c["cost_delta_pct"] is None else f"{c['cost_delta_pct']:+.1f}%"
        cps = "n/a" if c["cost_per_success_delta_pct"] is None else f"{c['cost_per_success_delta_pct']:+.1f}%"
        lines.append(f"| {key} | {c['success_delta_pp']:+.1f} | {cost} | {cps} |")
    lines.append("")
    lines.append("## Infrastructure / adapter errors")
    lines.append("")
    any_adapter = False
    for arm in arm_names:
        errors = report["arms"][arm]["adapter_errors"]
        if not errors:
            continue
        any_adapter = True
        lines.append(f"### {arm}")
        lines.append("")
        for e in errors:
            lines.append(f"- `{e['task']}`: {e['error']}")
        lines.append("")
    if not any_adapter:
        lines.append("None. Every task executed and returned valid usage data.")
        lines.append("")
    lines.append("## Failure cases")
    lines.append("")
    any_failure = False
    for arm in arm_names:
        failures = report["arms"][arm]["failures"]
        if not failures:
            continue
        any_failure = True
        lines.append(f"### {arm}")
        lines.append("")
        for f in failures:
            gate = ""
            if f["gate"]:
                gate = f" (gate: {f['gate']['class']} -> {f['gate']['tier']})"
            lines.append(f"- `{f['task']}` [{f['category']}] on `{f['model']}`{gate}: {'; '.join(f['failures'])}")
        lines.append("")
    if not any_failure:
        lines.append("No model failures in this run.")
        lines.append("")
    lines.append("## Quota used")
    lines.append("")
    for arm in arm_names:
        q = report["arms"][arm]["quota"]
        parts = [f"`{m}` {v['requests']} req ({v['pct_of_monthly']:.3f}% of monthly)" for m, v in sorted(q.items())]
        lines.append(f"- **{arm}**: " + ("; ".join(parts) if parts else "none"))
    lines.append("")
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# main
# --------------------------------------------------------------------------- #
def main():
    ap = argparse.ArgumentParser(description="Compare fixed and gate-routed arms.")
    ap.add_argument("--executor", choices=EXECUTORS, default="stub")
    ap.add_argument("--tasks", help="comma-separated task ids (default: all)")
    ap.add_argument("--arms", help="comma-separated arms (default: all)")
    ap.add_argument("--out", help="write JSON report here")
    ap.add_argument("--markdown", help="write the markdown report here")
    ap.add_argument("--sandbox", choices=SANDBOX_MODES, default="auto",
                    help="isolation for behavioural tests (default: auto, prefers bubblewrap)")
    ap.add_argument("--self-test", action="store_true", help="stub run + assert the report is well formed")
    ap.add_argument("--force", action="store_true", help="allow writing a synthetic report into eval/results/")
    args = ap.parse_args()

    if args.executor == "command" and not os.environ.get("AUTOROUTER_EVAL_CMD"):
        raise SystemExit("ERROR: --executor command requires AUTOROUTER_EVAL_CMD to be set")

    pins = load_pins()
    tasks = load_tasks()
    if args.tasks:
        wanted = {t.strip() for t in args.tasks.split(",")}
        tasks = [t for t in tasks if t["id"] in wanted]
        missing = wanted - {t["id"] for t in tasks}
        if missing:
            raise SystemExit(f"ERROR: unknown tasks: {sorted(missing)}")
    arms = tuple(a.strip() for a in args.arms.split(",")) if args.arms else ARMS
    for arm in arms:
        if arm not in ARMS:
            raise SystemExit(f"ERROR: unknown arm {arm!r}")

    if args.executor == "stub" and args.out:
        try:
            Path(args.out).resolve().relative_to((EVAL / "results").resolve())
        except ValueError:
            pass
        else:
            if not args.force:
                raise SystemExit(
                    "ERROR: refusing to write a synthetic run into eval/results/.\n"
                    "Use --executor command for evidence, or pass --force for a local scratch run."
                )

    records = []
    for arm in arms:
        for task in tasks:
            records.append(run_task(arm, task, args.executor, pins, sandbox=args.sandbox))

    report = build_report(records, pins, args.executor, arms)
    markdown = render_markdown(report, arms)

    if args.self_test:
        assert {r["arm"] for r in records} == set(arms)
        assert len(records) == len(arms) * len(tasks)
        assert report["synthetic"] is True and report["valid_evidence"] is False
        assert "SYNTHETIC" in markdown
        for arm in arms:
            a = report["arms"][arm]
            for key in ("success_rate", "total_cost_usd", "mean_latency_ms", "p95_latency_ms",
                        "retries", "escalations", "routing_overhead_ms_mean", "failures", "quota",
                        "infrastructure_errors", "adapter_errors", "tasks_scored"):
                assert key in a, (arm, key)
        assert all(r["outcome"] in OUTCOMES for r in records)
        assert "sandbox" in report and report["sandbox"]["mechanism"] in ("bwrap", "unshare", "none")
        if report["sandbox"]["mechanism"] == "none":
            assert any("sandbox" in r.lower() for r in report["validity"]["reasons"])
        else:
            assert report["sandbox"]["behavioral_tests_run"] > 0
        print(f"self-test ok: {len(records)} task runs, {len(tasks)} tasks x {len(arms)} arms")
        print("  (synthetic) success: " + ", ".join(
            f"{arm}={report['arms'][arm]['successes']}/{report['arms'][arm]['tasks_scored']}" for arm in arms))
        print("  (synthetic) cost:    " + ", ".join(
            f"{arm}=${report['arms'][arm]['total_cost_usd']:.4f}" for arm in arms))

    if args.out:
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        with open(args.out, "w", encoding="utf-8") as f:
            json.dump(report, f, indent=2)
            f.write("\n")
        print(f"Wrote {args.out}", file=sys.stderr)
    if args.markdown:
        Path(args.markdown).parent.mkdir(parents=True, exist_ok=True)
        Path(args.markdown).write_text(markdown + "\n", encoding="utf-8")
        print(f"Wrote {args.markdown}", file=sys.stderr)
    if not args.out and not args.markdown and not args.self_test:
        print(markdown)


if __name__ == "__main__":
    main()
