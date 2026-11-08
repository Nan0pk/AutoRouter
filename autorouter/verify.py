"""Prove a native installed two-model handoff; a skipped specialist cannot pass."""
import hashlib
import json
import os
import subprocess
import tempfile
import time
import uuid
from pathlib import Path

from .hermes import runtime, worker
from .install import fingerprint, save, status


def tokens(usage):
    if not isinstance(usage, dict):
        return False
    inp = usage.get("input_tokens", usage.get("prompt_tokens", 0))
    out = usage.get("output_tokens", usage.get("completion_tokens", 0))
    return type(inp) is int and type(out) is int and inp >= 0 and out >= 0 and inp+out > 0


def evaluate_handoff(policy, events, usage, output):
    rows = {r["id"]: r for r in policy["models"]}
    routine = rows[policy["routine"]]; specialist = rows[policy["specialist"]]
    if not output.strip() or usage.get("completed") is not True or any(usage.get(k) for k in ("failed", "partial", "interrupted")) or not tokens(usage):
        raise ValueError("Hermes did not complete a usable parent turn")
    if usage.get("provider") != routine["runtime_provider"] or usage.get("model") != routine["model"]:
        raise ValueError("Parent usage did not confirm the configured model")
    digest = hashlib.sha256(output.strip().encode()).hexdigest()
    stops = [e for e in events if e.get("event") == "child_stop" and e.get("child_status") in ("completed", "success", "ok") and e.get("result_sha256") == digest]
    if not stops:
        raise ValueError("No successful specialist result returned unchanged to the parent; handoff not verified")
    for stop in stops:
        root = stop.get("parent_session_id"); child = stop.get("child_session_id")
        if not root or not child or root == child:
            continue
        calls = [e for e in events if e.get("event") == "api" and e.get("session_id") in (root, child)]
        root_calls = [e for e in calls if e.get("session_id") == root]
        child_calls = [e for e in calls if e.get("session_id") == child and e.get("parent_session_id") == root]
        def valid(call, row):
            return call.get("provider") == row["runtime_provider"] and call.get("model") == row["model"] and call.get("response_model") == row["model"] and tokens(call.get("usage"))
        if root_calls and child_calls and all(valid(e, routine) for e in root_calls) and all(valid(e, specialist) for e in child_calls):
            return {"configured": True, "routing_verified": True, "handoff_verified": True,
                    "observed_models": [policy["routine"], policy["specialist"]],
                    "parent_session": root, "child_session": child, "api_calls": len(calls),
                    "result_sha256": digest, "usage": usage, "checked_at": time.time()}
    raise ValueError("Actual API routes, response models or usage did not confirm two distinct configured models")


def verify_install(home, live=False):
    home = Path(home).absolute()
    state = status(home)
    if state["state"] not in ("configured_unverified", "verified"):
        raise ValueError("AutoRouter is absent, changed, or needs recovery; run status/setup first")
    policy = json.loads((home/"autorouter/policy.json").read_text())
    check = worker("plugin_check", home=home)
    if not check["loaded"] or {"name": "autorouter", "available": True} not in check["backends"]:
        raise ValueError("Hermes did not load the installed AutoRouter backend")
    if not live:
        return {"configured": True, "routing_verified": False, "handoff_verified": False,
                "reason": "Live inference was not requested"}
    source, python = runtime()
    code = f"import sys; sys.path.insert(0, {str(source)!r}); from hermes_cli.main import main; sys.exit(main())"
    events_path = home/"autorouter/events.jsonl"
    offset = events_path.stat().st_size if events_path.exists() else 0
    marker = "AR_CHECK_"+uuid.uuid4().hex
    expected = marker+":42"
    prompt = ("AutoRouter setup verification: perform a security review exercise using a specialist. "
              "Do not access files, network or real data. Delegate exactly one task with route=specialist: "
              f"calculate 17+25 and reply with exactly {expected}. "
              "Wait for its completed result, then return that result unchanged, with no surrounding explanation. "
              "Do not answer the delegated task yourself.")
    env = {**os.environ, "HERMES_HOME": str(home), "HERMES_DISABLE_LAZY_INSTALLS": "1"}
    try:
        with tempfile.TemporaryDirectory(prefix="autorouter-live-check-") as work:
            usage_path = Path(work)/"usage.json"
            process = subprocess.run([str(python), "-I", "-c", code, "-z", prompt,
                                      "--toolsets", "delegation", "--usage-file", str(usage_path)],
                                     cwd=work, env=env, stdin=subprocess.DEVNULL,
                                     capture_output=True, text=True, timeout=240)
            if process.returncode or not usage_path.exists():
                raise ValueError("Hermes live check failed before producing usable usage; installation remains unverified")
            usage = json.loads(usage_path.read_text())
            if process.stdout.strip() != expected:
                raise ValueError("Hermes did not return the specialist's expected check result; installation remains unverified")
            with events_path.open() as log:
                log.seek(offset)
                events = [json.loads(line) for line in log if line.strip()]
            report = evaluate_handoff(policy, events, usage, process.stdout)
    except subprocess.TimeoutExpired:
        raise ValueError("Hermes live handoff check timed out; installation remains unverified") from None
    report_path = home/"autorouter/verification.json"
    save(report_path, report)
    manifest_path = home/"autorouter/install.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["verification_sha256"] = fingerprint(report_path)
    save(manifest_path, manifest)
    return report
