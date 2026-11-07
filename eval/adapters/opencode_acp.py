#!/usr/bin/env python3
"""One pinned OpenCode v2 ACP turn inside the evaluator's disposable worker."""
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path


class ACP:
    def __init__(self, process):
        self.process = process
        self.request_id = 0
        self.retries = 0

    def send(self, message):
        self.process.stdin.write(json.dumps({"jsonrpc": "2.0", **message}) + "\n")
        self.process.stdin.flush()

    def call(self, method, params):
        self.request_id += 1
        self.send({"id": self.request_id, "method": method, "params": params})
        for line in self.process.stdout:
            message = json.loads(line)
            if "method" not in message and message.get("id") == self.request_id:
                if "error" in message:
                    raise RuntimeError(f"{method} failed (ACP code {message['error'].get('code')})")
                return message["result"]
            if message.get("method") == "session/update":
                update = message["params"]["update"]
                if update.get("sessionUpdate") == "retry":
                    self.retries += 1
            elif "method" in message and "id" in message:
                if message["method"] == "session/request_permission":
                    self.send({"id": message["id"], "result": {"outcome": {"outcome": "cancelled"}}})
                else:
                    self.send({"id": message["id"],
                               "error": {"code": -32601, "message": "Unsupported client request"}})
        raise RuntimeError(f"OpenCode ACP exited during {method}")


def configure(home, workdir):
    # A candidate from a previous attempt cannot supply harness configuration
    # or executable plugins on the next attempt.
    forbidden = {"opencode.json", "opencode.jsonc", "AGENTS.md"}
    for path in workdir.rglob("*"):
        if path.is_symlink() or path.name.startswith(".") or path.name in forbidden:
            raise RuntimeError(f"disallowed project file for an eval worker: {path.name}")
    config = home / ".config/opencode/opencode.json"
    config.parent.mkdir(parents=True)
    config.write_text(json.dumps({
        "update": "disable",
        "permissions": [
            {"action": "*", "resource": "*", "effect": "deny"},
            *({"action": action, "resource": "*", "effect": "allow"}
              for action in ("read", "edit", "glob", "grep")),
        ],
    }))
    auth = os.environ.get("AUTOROUTER_OPENCODE_AUTH")
    if auth:
        destination = home / ".local/share/opencode/auth.json"
        destination.parent.mkdir(parents=True)
        shutil.copyfile(auth, destination)
        destination.chmod(0o600)


def prompt_turn(client, model, workdir, prompt):
    init = client.call("initialize", {"protocolVersion": 1, "clientCapabilities": {}})
    if init.get("protocolVersion") != 1:
        raise RuntimeError("OpenCode did not negotiate ACP protocol v1")
    session = client.call("session/new", {"cwd": str(workdir), "mcpServers": []})
    session_id = session["sessionId"]
    reply = client.call("session/set_config_option", {
        "sessionId": session_id, "configId": "model", "value": model,
    })
    options = {option["id"]: option.get("currentValue") for option in reply["configOptions"]}
    selected = options.get("model")
    if selected != model and f"{selected}/{options.get('effort')}" != model:
        raise RuntimeError("OpenCode did not confirm the pinned model or effort")
    turn = client.call("session/prompt", {
        "sessionId": session_id,
        "prompt": [{"type": "text", "text": prompt}],
    })
    if turn.get("stopReason") not in ("end_turn", "max_tokens", "max_turn_requests", "refusal"):
        raise RuntimeError("OpenCode did not complete the ACP turn")
    usage = turn.get("usage") or {}
    return {"tokens_in": usage.get("inputTokens"), "tokens_out": usage.get("outputTokens"),
            "retries": client.retries, "trace": [model]}


def main():
    workdir = Path(os.environ["AUTOROUTER_WORKDIR"])
    if workdir != Path("/work") or not workdir.is_mount():
        raise RuntimeError("run this adapter through the isolated evaluator")
    model = os.environ["AUTOROUTER_MODEL"]
    binary = os.environ.get("AUTOROUTER_OPENCODE", "opencode")
    with tempfile.TemporaryDirectory(prefix="autorouter-opencode-") as path:
        home = Path(path)
        configure(home, workdir)
        env = {key: val for key, val in os.environ.items()
               if key in ("PATH", "LC_ALL") or key.endswith("_API_KEY")}
        env["HOME"] = str(home)
        with subprocess.Popen(
            [binary, "acp"], cwd=workdir, env=env, stdin=subprocess.PIPE,
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, encoding="utf-8",
        ) as process:
            try:
                result = prompt_turn(ACP(process), model, workdir, sys.stdin.read())
            finally:
                process.stdin.close()
                try:
                    process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
    print(json.dumps(result))


if __name__ == "__main__":
    try:
        main()
    except (OSError, RuntimeError, ValueError, KeyError, TypeError) as exc:
        sys.exit(f"OpenCode adapter: {exc}")
