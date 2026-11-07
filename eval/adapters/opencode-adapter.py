#!/usr/bin/env python3
"""OpenCode adapter for eval/run_eval.py --executor command.

Contract (see eval/run_eval.py and eval/README.md):

    AUTOROUTER_MODEL=provider/model \
    AUTOROUTER_TASK_ID=mech-rename \
    AUTOROUTER_WORKDIR=/tmp/... \
    eval/adapters/opencode-adapter.py      # prompt arrives on stdin

The adapter runs one non-interactive OpenCode session in the working directory,
on exactly the model it was given, with the neutral `eval` agent (no
orchestration, no delegation), and prints one JSON object on stdout:

    {"tokens_in": N, "tokens_out": N, "latency_ms": N,
     "retries": N, "escalations": 0, "trace": ["provider/model"]}

Nothing else goes to stdout. Diagnostics go to stderr.

Why the `eval` agent: the AutoRouter config's default agent is a delegating
orchestrator. Measuring a "fixed" arm through it would silently re-route the
work to other models and make the arm meaningless. The agent is installed as
`agents/eval.md` by scripts/install_opencode.sh.

Requirements: the `opencode` CLI on PATH, and `opencode models` must list the
requested model.
"""
import json
import os
import subprocess
import sys
import time

AGENT = os.environ.get("AUTOROUTER_AGENT", "eval")
EXTRA_ARGS = os.environ.get("AUTOROUTER_OPENCODE_ARGS", "").split()
# OpenCode has no per-run temperature flag; the pinned temperature is advisory
# to the adapter (pins.json settings.tool_policy says the same).
MODEL_ENV = "AUTOROUTER_MODEL"
WORKDIR_ENV = "AUTOROUTER_WORKDIR"


def fail(message, code=2):
    print(f"opencode-adapter: {message}", file=sys.stderr)
    print(json.dumps({"error": message}), file=sys.stderr)
    return code


def main():
    model = os.environ.get(MODEL_ENV, "").strip()
    workdir = os.environ.get(WORKDIR_ENV, "").strip()
    if not model:
        return fail(f"{MODEL_ENV} is not set")
    if not workdir or not os.path.isdir(workdir):
        return fail(f"{WORKDIR_ENV} is not a directory: {workdir!r}")

    prompt = sys.stdin.read()
    if not prompt.strip():
        return fail("empty prompt on stdin")

    cmd = [
        "opencode", "run",
        "--format", "json",
        "--model", model,
        "--agent", AGENT,
        "--archived",
        "--auto",
    ] + EXTRA_ARGS

    started = time.monotonic()
    try:
        proc = subprocess.run(
            cmd,
            cwd=workdir,
            input=prompt,
            text=True,
            capture_output=True,
        )
    except FileNotFoundError:
        return fail("opencode CLI not found on PATH")
    latency_ms = int((time.monotonic() - started) * 1000)

    tokens_in = tokens_out = 0
    retries = 0
    steps = 0
    error = None
    trace = []
    for line in proc.stdout.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            event = json.loads(line)
        except ValueError:
            continue
        kind = event.get("type")
        if kind == "step_finish":
            usage = event.get("part", {}).get("tokens") or {}
            tokens_in += int(usage.get("input") or 0)
            tokens_out += int(usage.get("output") or 0) + int(usage.get("reasoning") or 0)
            steps += 1
            used = ((event.get("part") or {}).get("model") or {})
            if isinstance(used, dict) and used.get("modelID"):
                tag = f"{used.get('providerID', '?')}/{used['modelID']}"
                if tag not in trace:
                    trace.append(tag)
        elif kind == "retry":
            retries += 1
        elif kind == "error":
            error = json.dumps(event.get("error") or event)[:300]

    if proc.returncode != 0:
        tail = (proc.stderr or proc.stdout or "").strip().splitlines()
        return fail(f"opencode exited {proc.returncode}: {(tail[-1][:300] if tail else 'no output')}")
    if error:
        return fail(f"opencode reported an error: {error}")
    if steps == 0:
        return fail("opencode produced no step_finish events (no usage data)")
    if tokens_in + tokens_out <= 0:
        return fail(f"zero usage (in={tokens_in}, out={tokens_out})")

    print(json.dumps({
        "tokens_in": tokens_in,
        "tokens_out": tokens_out,
        "latency_ms": latency_ms,
        "retries": retries,
        "escalations": 0,
        "trace": trace or [model],
    }))
    return 0


if __name__ == "__main__":
    sys.exit(main())
