#!/usr/bin/env python3
"""Evaluation harness: fixed-workhorse vs fixed-strong vs AutoRouter.

The point is to make the routing claim testable, not to assert it. This script
runs the same task set through three arms, verifies declarative checks, and
reports success, cost/quota, latency, retries/escalations and routing overhead
side by side -- including the tasks each arm failed.

Executors
---------
stub     Deterministic, synthetic. Applies canned patches and canned token and
         latency figures. It exists to test the harness in CI. Its results
         encode assumptions, so they are NOT evidence: every stub report is
         stamped "synthetic": true / "valid_evidence": false, and writing one
         into eval/results/ is refused.
command  The real path. Runs one command per attempt with the prompt on stdin:

             AUTOROUTER_MODEL=provider/model \
             AUTOROUTER_TASK_ID=mech-rename \
             AUTOROUTER_WORKDIR=/tmp/... \
             $AUTOROUTER_EVAL_CMD

         The command edits files in its working directory and prints a JSON
         object on stdout:

             {"tokens_in": 8123, "tokens_out": 640, "latency_ms": 9100,
              "retries": 0, "escalations": 0, "trace": ["provider/model"]}

         The harness verifies the files afterwards; it never executes model
         output.

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

ARMS = ("fixed_workhorse", "fixed_strong", "autorouter")
ARM_MODEL = {
    "fixed_workhorse": ("T2", "workhorse"),
    "fixed_strong": ("T5", "strong"),
}
EXECUTORS = ("stub", "command")


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
def run_stub(task, role, pins, workdir):
    spec = pins["stub"]
    patch = load_json(EVAL / "stub_patches.json").get(task["id"], {})
    applied = role in patch.get("solvable_by", [])
    if applied:
        for name, body in patch.get("files", {}).items():
            (Path(workdir) / name).write_text(body, encoding="utf-8")
    tokens = spec["tokens"][role]
    return {
        "applied": applied,
        "tokens_in": tokens["in"],
        "tokens_out": tokens["out"],
        "latency_ms": spec["latency_ms"][role],
        "retries": 0,
        "escalations": 0,
        "trace": [f"stub:{role}"],
    }


def run_command(task, role, model, pins, workdir):
    cmd = os.environ.get("AUTOROUTER_EVAL_CMD")
    if not cmd:
        raise SystemExit("ERROR: --executor command needs AUTOROUTER_EVAL_CMD")
    env = dict(os.environ)
    env.update(
        {
            "AUTOROUTER_MODEL": model,
            "AUTOROUTER_TASK_ID": task["id"],
            "AUTOROUTER_WORKDIR": str(workdir),
        }
    )
    started = time.monotonic()
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
    latency_ms = int((time.monotonic() - started) * 1000)
    try:
        payload = json.loads(proc.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError):
        payload = {}
    return {
        "applied": proc.returncode == 0,
        "tokens_in": int(payload.get("tokens_in") or 0),
        "tokens_out": int(payload.get("tokens_out") or 0),
        "latency_ms": int(payload.get("latency_ms") or latency_ms),
        "retries": int(payload.get("retries") or 0),
        "escalations": int(payload.get("escalations") or 0),
        "trace": payload.get("trace") or [model],
        "stderr": proc.stderr[-2000:] if proc.returncode else "",
    }


def execute(executor, task, role, model, pins, workdir):
    if executor == "stub":
        return run_stub(task, role, pins, workdir)
    return run_command(task, role, model, pins, workdir)


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
    """AutoRouter policy: gate, then pick a tier; complex work starts at T3 and may escalate."""
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
def run_task(arm, task, executor, pins, keep=None):
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

    def attempt(model_id, role_name, tier_name):
        res = execute(executor, task, role_name, model_id, pins, workdir)
        attempts.append({
            "model": model_id,
            "tier": tier_name,
            "role": role_name,
            "tokens_in": res["tokens_in"],
            "tokens_out": res["tokens_out"],
            "latency_ms": res["latency_ms"],
        })

    attempt(model, role, tier)
    passed, failures = verify(workdir, task["checks"])

    # AutoRouter escalates once when its first pick fails and the gate said the
    # task may delegate. Fixed arms never escalate -- that is the variable.
    if arm == "autorouter" and failures and decision and decision["escalate_to"]:
        retries += 1
        esc = pins["tiers"][decision["escalate_to"]]
        attempt(esc["model"], esc["role"], decision["escalate_to"])
        escalations += 1
        passed, failures = verify(workdir, task["checks"])

    tokens_in = sum(a["tokens_in"] for a in attempts)
    tokens_out = sum(a["tokens_out"] for a in attempts)
    cost = sum(price(a["model"], a["tokens_in"], a["tokens_out"], pins) for a in attempts)
    latency_ms = sum(a["latency_ms"] for a in attempts) + int(routing_ms)

    record = {
        "task": task["id"],
        "category": task["category"],
        "arm": arm,
        "success": not failures,
        "checks_passed": passed,
        "checks_total": len(task["checks"]),
        "failures": failures,
        "model": model,
        "tier": tier,
        "attempts": attempts,
        "tokens_in": tokens_in,
        "tokens_out": tokens_out,
        "cost_usd": round(cost, 6),
        "latency_ms": latency_ms,
        "routing_ms": round(routing_ms, 2),
        "retries": retries,
        "escalations": escalations,
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
    successes = sum(r["success"] for r in arm_records)
    checks_passed = sum(r["checks_passed"] for r in arm_records)
    checks_total = sum(r["checks_total"] for r in arm_records)
    cost = sum(r["cost_usd"] for r in arm_records)
    latencies = [r["latency_ms"] for r in arm_records]
    quota = {}
    for r in arm_records:
        for a in r["attempts"]:
            q = quota.setdefault(a["model"], {"requests": 0, "req_per_month": pins["models"][a["model"]]["req_per_month"]})
            q["requests"] += 1
    for model, q in quota.items():
        q["pct_of_monthly"] = round(100.0 * q["requests"] / q["req_per_month"], 4)
    return {
        "arm": arm,
        "tasks": n,
        "successes": successes,
        "success_rate": round(successes / n, 4) if n else 0.0,
        "partial_check_rate": round(checks_passed / checks_total, 4) if checks_total else 0.0,
        "total_cost_usd": round(cost, 6),
        "cost_per_success_usd": round(cost / successes, 6) if successes else None,
        "mean_latency_ms": round(statistics.mean(latencies)) if latencies else 0,
        "p95_latency_ms": round(sorted(latencies)[max(0, int(0.95 * len(latencies)) - 1)]) if latencies else 0,
        "retries": sum(r["retries"] for r in arm_records),
        "escalations": sum(r["escalations"] for r in arm_records),
        "routing_overhead_ms_total": round(sum(r["routing_ms"] for r in arm_records), 2),
        "routing_overhead_ms_mean": round(statistics.mean([r["routing_ms"] for r in arm_records]), 2) if n else 0.0,
        "quota": quota,
        "max_quota_pct": round(max((q["pct_of_monthly"] for q in quota.values()), default=0.0), 4),
        "failures": [
            {"task": r["task"], "category": r["category"], "failures": r["failures"],
             "model": r["model"], "gate": r["gate"]}
            for r in arm_records if not r["success"]
        ],
    }


def compare(arms):
    out = {}
    base = arms.get("fixed_workhorse")
    if base is None:
        return out
    for name in ("fixed_strong", "autorouter"):
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
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "synthetic": executor == "stub",
        "valid_evidence": executor != "stub",
        "warning": (
            "SYNTHETIC RUN: stub executor. These numbers test the harness, not the router. "
            "Do not cite them as evidence."
            if executor == "stub"
            else "Measured run. Check eval/README.md for the protocol before comparing runs."
        ),
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
    lines.append("| arm | success | checks | cost $ | $/success | mean ms | p95 ms | retries | escalations | routing ms | max quota % |")
    lines.append("|---|---|---|---|---|---|---|---|---|---|---|")
    for arm in arm_names:
        a = report["arms"][arm]
        cps = "n/a" if a["cost_per_success_usd"] is None else f"{a['cost_per_success_usd']:.4f}"
        lines.append(
            f"| {arm} | {a['successes']}/{a['tasks']} ({a['success_rate']:.0%}) | "
            f"{a['partial_check_rate']:.0%} | {a['total_cost_usd']:.4f} | {cps} | "
            f"{a['mean_latency_ms']} | {a['p95_latency_ms']} | {a['retries']} | {a['escalations']} | "
            f"{a['routing_overhead_ms_mean']:.1f} | {a['max_quota_pct']:.3f} |"
        )
    lines.append("")
    lines.append("## Comparison vs fixed-workhorse")
    lines.append("")
    lines.append("| comparison | success delta (pp) | cost delta | $/success delta |")
    lines.append("|---|---|---|---|")
    for key, c in report["comparison"].items():
        cost = "n/a" if c["cost_delta_pct"] is None else f"{c['cost_delta_pct']:+.1f}%"
        cps = "n/a" if c["cost_per_success_delta_pct"] is None else f"{c['cost_per_success_delta_pct']:+.1f}%"
        lines.append(f"| {key} | {c['success_delta_pp']:+.1f} | {cost} | {cps} |")
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
        lines.append("No failures in this run.")
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
    ap = argparse.ArgumentParser(description="Compare fixed-workhorse, fixed-strong and AutoRouter.")
    ap.add_argument("--executor", choices=EXECUTORS, default="stub")
    ap.add_argument("--tasks", help="comma-separated task ids (default: all)")
    ap.add_argument("--arms", help="comma-separated arms (default: all)")
    ap.add_argument("--out", help="write JSON report here")
    ap.add_argument("--markdown", help="write the markdown report here")
    ap.add_argument("--self-test", action="store_true", help="stub run + assert the report is well formed")
    ap.add_argument("--force", action="store_true", help="allow writing a synthetic report into eval/results/")
    args = ap.parse_args()

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
            records.append(run_task(arm, task, args.executor, pins))

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
                        "retries", "escalations", "routing_overhead_ms_mean", "failures", "quota"):
                assert key in a, (arm, key)
        print(f"self-test ok: {len(records)} task runs, {len(tasks)} tasks x {len(arms)} arms")
        print(f"  (synthetic) success: " + ", ".join(
            f"{arm}={report['arms'][arm]['successes']}/{report['arms'][arm]['tasks']}" for arm in arms))
        print(f"  (synthetic) cost:    " + ", ".join(
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
