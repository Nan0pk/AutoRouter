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

Transient provider faults
-------------------------
A 429 / "server is overloaded" / "prefill_cap_refused" is an upstream condition,
not a model failure. The adapter retries the whole run with backoff, bounded by
AUTOROUTER_ADAPTER_BUDGET_S (default 840 s) so a slow run cannot outlive the
harness timeout. Every attempt's tokens are counted (they are billed) and the
extra attempts are reported as `retries`, so a run that needed three tries never
looks as cheap as one that needed one. Anything else — zero usage, no JSON, a
non-zero exit without a transient signature — is still a hard failure.

Why the `eval` agent: the AutoRouter config's default agent is a delegating
orchestrator. Measuring a "fixed" arm through it would silently re-route the
work to other models and make the arm meaningless. The agent is installed as
`agents/eval.md` by scripts/install_opencode.sh.

Environment:
    AUTOROUTER_MODEL            required, provider/model[#variant]
    AUTOROUTER_WORKDIR          required, working directory
    AUTOROUTER_AGENT            agent name, default `eval`
    AUTOROUTER_OPENCODE_BIN     CLI to run, default `opencode`
    AUTOROUTER_ATTEMPT_TIMEOUT_S kill and retry a single stalled attempt, default 240
    AUTOROUTER_ADAPTER_BUDGET_S total retry budget, default 840
    AUTOROUTER_MAX_ATTEMPTS     default 3
    AUTOROUTER_OPENCODE_ARGS    extra CLI args, space separated
"""
import json
import os
import shutil
import subprocess
import sys
import time

AGENT = os.environ.get("AUTOROUTER_AGENT", "eval")
EXTRA_ARGS = os.environ.get("AUTOROUTER_OPENCODE_ARGS", "").split()
OPENCODE = os.environ.get("AUTOROUTER_OPENCODE_BIN", "opencode")
MAX_ATTEMPTS = max(1, int(os.environ.get("AUTOROUTER_MAX_ATTEMPTS", "3")))
BUDGET_S = float(os.environ.get("AUTOROUTER_ADAPTER_BUDGET_S", "840"))
# A single attempt that stalls (provider hangs, session never finishes) is killed
# and retried instead of burning the whole harness timeout on one call.
ATTEMPT_TIMEOUT_S = float(os.environ.get("AUTOROUTER_ATTEMPT_TIMEOUT_S", "240"))
# OpenCode exposes no per-run temperature flag; the pinned temperature is
# advisory to this adapter (pins.json settings.tool_policy says the same).
MODEL_ENV = "AUTOROUTER_MODEL"
WORKDIR_ENV = "AUTOROUTER_WORKDIR"

TRANSIENT_MARKERS = (
    "[overloaded]",
    "prefill_cap_refused",
    "rate-limit",
    "rate limit",
    "429",
    "temporarily unavailable",
    "server is overloaded",
)


def fail(message, code=2):
    print(f"opencode-adapter: {message}", file=sys.stderr)
    return code


def parse_stream(stdout):
    """Return (tokens_in, tokens_out, steps, retries, error, trace) from the JSON event stream."""
    tokens_in = tokens_out = steps = retries = 0
    error = None
    trace = []
    for line in stdout.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            event = json.loads(line)
        except ValueError:
            continue
        kind = event.get("type")
        if kind == "step_finish":
            part = event.get("part") or {}
            usage = part.get("tokens") or {}
            tokens_in += int(usage.get("input") or 0)
            tokens_out += int(usage.get("output") or 0) + int(usage.get("reasoning") or 0)
            steps += 1
            used = part.get("model") or {}
            if isinstance(used, dict) and used.get("modelID"):
                tag = f"{used.get('providerID', '?')}/{used['modelID']}"
                if tag not in trace:
                    trace.append(tag)
        elif kind == "retry":
            retries += 1
        elif kind == "error":
            error = json.dumps(event.get("error") or event)[:300]
    return tokens_in, tokens_out, steps, retries, error, trace


def is_transient(text):
    low = (text or "").lower()
    return any(marker in low for marker in TRANSIENT_MARKERS)


def run_once(model, workdir, prompt):
    cmd = [OPENCODE, "run", "--format", "json", "--model", model,
           "--agent", AGENT, "--archived", "--auto"] + EXTRA_ARGS
    started = time.monotonic()
    try:
        proc = subprocess.run(cmd, cwd=workdir, input=prompt, text=True,
                              capture_output=True, timeout=ATTEMPT_TIMEOUT_S)
    except FileNotFoundError:
        return {"fatal": f"{OPENCODE!r} not found on PATH", "latency_ms": 0}
    except subprocess.TimeoutExpired:
        return {
            "fatal": f"attempt timed out after {int(ATTEMPT_TIMEOUT_S)}s",
            "transient": True,
            "latency_ms": int((time.monotonic() - started) * 1000),
        }
    latency_ms = int((time.monotonic() - started) * 1000)
    tokens_in, tokens_out, steps, retries, error, trace = parse_stream(proc.stdout)
    result = {
        "latency_ms": latency_ms,
        "tokens_in": tokens_in,
        "tokens_out": tokens_out,
        "steps": steps,
        "retries": retries,
        "trace": trace,
        "transient": False,
    }
    if proc.returncode != 0:
        tail = (proc.stderr or proc.stdout or "").strip().splitlines()
        detail = tail[-1][:300] if tail else "no output"
        result.update(
            fatal=f"{OPENCODE} exited {proc.returncode}: {detail}",
            transient=is_transient(detail),
        )
        return result
    if error:
        result.update(fatal=f"{OPENCODE} reported an error: {error}", transient=is_transient(error))
        return result
    if steps == 0:
        result.update(fatal="no step_finish events (no usage data)", transient=False)
        return result
    if tokens_in + tokens_out <= 0:
        result.update(fatal=f"zero usage (in={tokens_in}, out={tokens_out})", transient=False)
        return result
    return result


def main():
    model = os.environ.get(MODEL_ENV, "").strip()
    workdir = os.environ.get(WORKDIR_ENV, "").strip()
    if not model:
        return fail(f"{MODEL_ENV} is not set")
    if not workdir or not os.path.isdir(workdir):
        return fail(f"{WORKDIR_ENV} is not a directory: {workdir!r}")
    if not shutil.which(OPENCODE):
        return fail(f"opencode CLI not found: {OPENCODE}")

    prompt = sys.stdin.read()
    if not prompt.strip():
        return fail("empty prompt on stdin")

    started = time.monotonic()
    deadline = started + BUDGET_S
    totals = {"tokens_in": 0, "tokens_out": 0, "retries": 0}
    trace = []
    attempts = 0
    last_error = "no attempt made"

    while attempts < MAX_ATTEMPTS:
        attempts += 1
        result = run_once(model, workdir, prompt)
        # Bill every attempt, including the ones that ended in a transient fault:
        # those tokens were really spent.
        totals["tokens_in"] += result.get("tokens_in", 0)
        totals["tokens_out"] += result.get("tokens_out", 0)
        totals["retries"] += result.get("retries", 0)
        for tag in result.get("trace", []):
            if tag not in trace:
                trace.append(tag)

        if "fatal" not in result:
            print(json.dumps({
                "tokens_in": totals["tokens_in"],
                "tokens_out": totals["tokens_out"],
                "latency_ms": int((time.monotonic() - started) * 1000),
                "retries": totals["retries"] + (attempts - 1),
                "escalations": 0,
                "trace": trace or [model],
            }))
            return 0

        last_error = result["fatal"]
        remaining = deadline - time.monotonic()
        if not result.get("transient") or attempts >= MAX_ATTEMPTS or remaining < 60:
            break
        backoff = 3 if attempts == 1 else 8
        print(f"opencode-adapter: transient fault (attempt {attempts}/{MAX_ATTEMPTS}): "
              f"{last_error}; retrying in {backoff}s", file=sys.stderr)
        time.sleep(backoff)

    return fail(f"failed after {attempts} attempt(s): {last_error}")


if __name__ == "__main__":
    sys.exit(main())
