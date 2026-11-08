#!/usr/bin/env python3
"""Checkpoint pilot: one workhorse repair, one specialist, then finish."""
import argparse
import fcntl
import hashlib
import json
import os
import shutil
from pathlib import Path

import isolation
import run_eval

FILES = {"aggregate.py", "summary.py"}
TERMINAL = {"completed", "model_failure", "infrastructure_error"}
NEXT = {
    "draft": ("finish", "repair"),
    "repair": ("finish", "specialist"),
    "specialist": ("finish", "model_failure"),
    "finish": ("completed", "model_failure"),
}
PUBLIC_CHECK = (
    "Public reproduction: total_by_key([{'k': 'a', 'v': 2}, "
    "{'k': 'a', 'v': -2}]) must return {} without raising."
)
PROMPTS = {
    "draft": "Draft summarize(rows) in summary.py using total_by_key from aggregate.py. "
             "Fix the zero-total bug in aggregate.py if you can. Sorting comes in the final step.",
    "repair": "Fix the zero-total bug in aggregate.py using the reproduction and check result. "
              "Preserve summary.py.",
    "specialist": "Resolve the remaining aggregation blocker. Edit aggregate.py only. "
                  "Preserve the workhorse's summary.py.",
    "finish": "The aggregator passed the public check. Finish summarize(rows) in summary.py "
              "to return a list of (key, total) tuples sorted by key, excluding zero totals. "
              "Use total_by_key and preserve aggregate.py.",
}


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def manifest(workdir):
    files = list(workdir.iterdir())
    if {file.name for file in files} != FILES or any(
        file.is_symlink() or not file.is_file() for file in files
    ):
        raise ValueError("a pilot workspace must contain only aggregate.py and summary.py")
    return {file.name: digest(file) for file in sorted(files)}


def save(directory, state):
    temp = directory / "state.tmp"
    with temp.open("w", encoding="utf-8") as file:
        json.dump(state, file, indent=2)
        file.write("\n")
        file.flush()
        os.fsync(file.fileno())
    temp.replace(directory / "state.json")


def start(directory, args, pins):
    workhorse = args.workhorse or pins["tiers"]["T2"]["model"]
    specialist = args.specialist or pins["tiers"]["T3"]["model"]
    if workhorse == specialist or not all(
        "/" in m and not any(c.isspace() for c in m) for m in (workhorse, specialist)
    ):
        raise ValueError("choose two distinct provider/model profiles")
    executor = args.executor or "stub"
    command_hash = adapter_fingerprint(executor)
    initial = directory / "checkpoint-0"
    shutil.copytree(run_eval.EVAL / "fixtures/debug-mutation", initial)
    (initial / "summary.py").write_text("def summarize(rows):\n    raise NotImplementedError\n")
    state = {
        "version": 1,
        "suite_sha256": pins["suite_sha256"],
        "executor": executor,
        "adapter_sha256": command_hash,
        "synthetic": executor == "stub",
        "profiles": {"workhorse": workhorse, "specialist": specialist},
        "phase": "draft",
        "in_flight": None,
        "files": manifest(initial),
        "attempts": [],
    }
    save(directory, state)
    return state


def resume(directory, args, pins):
    state = run_eval.load_json(directory / "state.json")
    if state.get("version") != 1 or state["suite_sha256"] != pins["suite_sha256"]:
        raise ValueError("checkpoint format or pinned suite changed")
    if any((args.executor, args.workhorse, args.specialist)):
        raise ValueError("resume uses the executor and profiles saved in state.json")
    if state["in_flight"]:
        raise ValueError("the previous attempt was interrupted during dispatch or grading; "
                         "its outcome is uncertain. Start a new trial to avoid replay.")
    if state["phase"] not in TERMINAL and state["adapter_sha256"] != adapter_fingerprint(state["executor"]):
        raise ValueError("adapter command or profile changed since the checkpoint")
    return state


def adapter_fingerprint(executor):
    if executor == "stub":
        return None
    command = os.environ.get("AUTOROUTER_EVAL_CMD")
    if not command or not isolation.available(network=True):
        raise ValueError("command pilot requires AUTOROUTER_EVAL_CMD and Bubblewrap")
    profile = os.environ.get("AUTOROUTER_EVAL_PROFILE")
    if profile:
        command = json.dumps([command, profile])
    return hashlib.sha256(command.encode()).hexdigest()


def run_worker(state, phase, pins, workdir):
    specialist = phase == "specialist"
    role = "specialist" if specialist else "workhorse"
    model = state["profiles"][role]
    task = {"id": "checkpoint-pilot", "prompt": PROMPTS[phase]}
    task["prompt"] += " Edit only the two existing files as directed. " + PUBLIC_CHECK
    if phase in ("repair", "specialist"):
        task["prompt"] += " Prior public check: " + state["attempts"][-1]["feedback"]
    if state["executor"] == "command":
        return model, run_eval.run_command(task, role, model, pins, workdir)
    if phase == "draft":
        (workdir / "summary.py").write_text(
            "from aggregate import total_by_key\n\ndef summarize(rows):\n"
            "    return list(total_by_key(rows).items())\n"
        )
    elif phase == "finish":
        (workdir / "summary.py").write_text(
            "from aggregate import total_by_key\n\ndef summarize(rows):\n"
            "    return sorted(total_by_key(rows).items())\n"
        )
    task["id"] = "debug-mutation"
    return model, run_eval.run_stub(task, role, pins, workdir)


def grade(phase, workdir, pins):
    scripts = ("debug-mutation", "pilot-summary") if phase == "finish" else ("pilot-blocker",)
    feedback = []
    for script in scripts:
        result = run_eval.run_behavior({"behavior": f"behavior/{script}.py"},
                                       workdir, pins, "bwrap")
        if result["status"] in ("sandbox_error", "skipped"):
            return "infrastructure_error", str(result["reason"])[:400]
        if result["status"] != "ok":
            feedback.append(str(result["reason"]))
        else:
            feedback.extend(f"{check['name']}: {check['detail']}"
                            for check in result["checks"] if not check["ok"])
    return ("failed", "; ".join(feedback)[:400]) if feedback else ("passed", "checks passed")


def advance(directory, state, pins):
    phase = state["phase"]
    number = len(state["attempts"])
    checkpoint = directory / f"checkpoint-{number}"
    previous = manifest(checkpoint)
    if previous != state["files"]:
        raise ValueError("checkpoint files have changed since the previous attempt")
    workdir = directory / f"checkpoint-{number + 1}"
    state["in_flight"] = phase
    save(directory, state)
    shutil.copytree(checkpoint, workdir)
    model, result = run_worker(state, phase, pins, workdir)
    updated = None
    if result["status"] != "ok":
        verdict, feedback = "infrastructure_error", result["error"]
    else:
        try:
            updated = manifest(workdir)
        except ValueError as exc:
            verdict, feedback = "failed", str(exc)
        else:
            protected = {"repair": "summary.py", "specialist": "summary.py", "finish": "aggregate.py"}
            name = protected.get(phase)
            if name and updated[name] != previous[name]:
                verdict, feedback = "failed", f"{name} changed outside the worker's scope"
                updated = None
            else:
                verdict, feedback = grade(phase, workdir, pins)
    if verdict == "infrastructure_error":
        next_phase = verdict
    elif updated is None:
        # Invalid workspaces never become inputs to another worker.
        next_phase = "model_failure"
    else:
        next_phase = NEXT[phase][0 if verdict == "passed" else 1]
    state["attempts"].append({"phase": phase, "model": model, "execution": result,
                              "verdict": verdict, "feedback": feedback})
    state["phase"] = next_phase
    if updated is not None:
        state["files"] = updated
    state["in_flight"] = None
    save(directory, state)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", required=True, type=Path)
    parser.add_argument("--executor", choices=run_eval.EXECUTORS)
    parser.add_argument("--workhorse")
    parser.add_argument("--specialist")
    parser.add_argument("--steps", type=int, choices=range(1, 5), default=4,
                        help="stop at a committed checkpoint after this many turns")
    parser.add_argument("--resume", action="store_true")
    args = parser.parse_args()

    directory = args.run_dir.resolve()
    if any(directory.is_relative_to(path) for path in isolation.PROTECTED):
        parser.error("run directory cannot be inside a fixture or grader directory")
    pins = run_eval.load_pins()
    if not isolation.available():
        parser.error("the pilot requires an offline Bubblewrap grader")
    try:
        if not args.resume:
            directory.mkdir(mode=0o700, parents=True)
        with (directory / ".lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            state = resume(directory, args, pins) if args.resume else start(directory, args, pins)
            for _ in range(args.steps):
                if state["phase"] in TERMINAL:
                    break
                advance(directory, state, pins)
    except (OSError, ValueError) as exc:
        parser.error(str(exc))
    print(json.dumps({"phase": state["phase"], "synthetic": state["synthetic"],
                      "attempts": len(state["attempts"]), "state": str(directory / "state.json")}))
    return int(state["phase"] in {"model_failure", "infrastructure_error"})


if __name__ == "__main__":
    raise SystemExit(main())
