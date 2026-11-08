#!/usr/bin/env python3
"""Run a file-only Hermes turn on a pinned OpenCode Go model."""
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

PROVIDER = "opencode-go"


def read_usage(path, model):
    try:
        report = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise RuntimeError("Hermes did not write a valid usage report") from None
    if not isinstance(report, dict) or report.get("completed") is not True or any(
        report.get(flag) for flag in ("failed", "partial", "interrupted")
    ):
        raise RuntimeError("Hermes did not complete the turn")
    if report.get("provider") != PROVIDER or report.get("model") != model:
        raise RuntimeError("Hermes did not report the pinned OpenCode Go model")
    auxiliary = report.get("auxiliary", {})
    if not isinstance(auxiliary, dict):
        raise RuntimeError("Hermes reported invalid auxiliary usage")
    tokens = []
    for field in ("input_tokens", "output_tokens"):
        main, aux = report.get(field), auxiliary.get(field, 0)
        if any(type(value) is not int or value < 0 for value in (main, aux)):
            raise RuntimeError("Hermes reported invalid token usage")
        tokens.append(main + aux)
    return {"tokens_in": tokens[0], "tokens_out": tokens[1], "trace": [f"{PROVIDER}/{model}"]}


def check_version(binary, env):
    result = subprocess.run([binary, "--version"], env=env, stdin=subprocess.DEVNULL,
                            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                            text=True, errors="replace", timeout=15)
    version = re.search(r"\bv?(\d+)\.(\d+)\.(\d+)\b", result.stdout)
    if result.returncode or not version or tuple(map(int, version.groups())) < (0, 23, 0):
        raise RuntimeError("the sandbox needs a working Hermes v0.23.0 or later")


def main():
    workdir = Path(os.environ.get("AUTOROUTER_WORKDIR", ""))
    if workdir != Path("/work") or not workdir.is_mount():
        raise RuntimeError("run this adapter through the isolated evaluator")
    provider, _, model = os.environ.get("AUTOROUTER_MODEL", "").partition("/")
    if provider != PROVIDER or not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_.-]*", model):
        raise RuntimeError("select an exact opencode-go/model ID without an effort suffix")
    key = os.environ.get("OPENCODE_GO_API_KEY")
    if not key:
        raise RuntimeError("no OpenCode Go key was forwarded")
    binary = os.environ.get("AUTOROUTER_HERMES", "hermes")
    prompt = sys.stdin.read()
    with tempfile.TemporaryDirectory(prefix="autorouter-hermes-") as directory:
        home = Path(directory)
        # JSON is a subset of YAML. Keep Hermes' normal profile out of the worker.
        (home / "config.yaml").write_text(json.dumps({
            "agent": {"max_turns": 12},
            "fallback_models": [],
            "terminal": {"backend": "local", "cwd": str(workdir)},
            "auxiliary": {"title_generation": {"enabled": False, "model_upgrade_enabled": False}},
        }), encoding="utf-8")
        env = {key: value for key, value in os.environ.items() if key in ("PATH", "LC_ALL")}
        env.update(HOME=str(home), HERMES_HOME=str(home), HERMES_WRITE_SAFE_ROOT=str(workdir))
        check_version(binary, env)
        env["OPENCODE_GO_API_KEY"] = key
        usage = home / "usage.json"
        process = subprocess.run(
            [binary, "-z", prompt, "--provider", PROVIDER, "--model", model,
             "--toolsets", "file", "--usage-file", str(usage)],
            cwd=workdir, env=env, stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        if process.returncode:
            raise RuntimeError(f"Hermes turn failed (exit {process.returncode})")
        print(json.dumps(read_usage(usage, model)))


if __name__ == "__main__":
    try:
        main()
    except (OSError, RuntimeError, subprocess.TimeoutExpired) as exc:
        sys.exit(f"Hermes adapter: {exc}")
