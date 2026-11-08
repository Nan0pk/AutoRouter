#!/usr/bin/env python3
"""Use the active OpenCode API key for an isolated Hermes Go handoff run."""
import argparse
import json
import os
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

import isolation

HERE = Path(__file__).resolve().parent
INTEGRATIONS = ("opencode", "opencode-go")


def active_key(export, integration):
    if not isinstance(export, list) or any(
        not isinstance(row, dict) or row.get("integrationID") != integration for row in export
    ):
        raise ValueError("unexpected OpenCode export format or integration")
    active = [row for row in export if row.get("active") is True]
    if len(active) != 1:
        raise ValueError("the selected OpenCode integration must have exactly one active account")
    entry = active[0]
    value = entry.get("value")
    if not isinstance(value, dict) or value.get("type") != "key":
        raise ValueError("the active OpenCode credential must be an API key")
    key = value.get("key")
    credential_id = entry.get("id")
    if not isinstance(key, str) or not key or not key.isascii() or not key.isprintable() or any(
        char.isspace() for char in key
    ) or not isinstance(credential_id, str) or not credential_id:
        raise ValueError("the active OpenCode API key entry is incomplete")
    return key, credential_id


def export_key(binary, integration):
    try:
        process = subprocess.run(
            [binary, "auth", "export", integration, "--standalone"],
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            text=True, encoding="utf-8", timeout=30,
        )
    except (OSError, UnicodeError, subprocess.TimeoutExpired):
        raise ValueError("could not export the selected OpenCode integration") from None
    if process.returncode:
        raise ValueError("OpenCode rejected the selected integration export")
    try:
        export = json.loads(process.stdout)
    except ValueError:
        raise ValueError("OpenCode did not return a JSON credential export") from None
    return active_key(export, integration)


def binary_path(value):
    path = shutil.which(value)
    if not path:
        raise ValueError(f"executable unavailable: {value}")
    return Path(path).absolute()


def runtime_mounts(binary, extra):
    home = Path.home().resolve()
    hermes_home = Path(os.environ.get("HERMES_HOME", home / ".hermes")).expanduser().resolve()
    paths = [binary, HERE / "adapters/hermes_go.py", *extra]
    if binary.is_relative_to(hermes_home) or binary == home / ".local/bin/hermes":
        paths.extend(path for path in (hermes_home / "hermes-agent", hermes_home / "tools",
                                       home / ".local/share/uv/python") if path.is_dir())
    roots = (hermes_home, home / ".local/share/opencode", home / ".config/opencode")
    runtime = tuple(hermes_home / name for name in ("bin", "hermes-agent", "tools"))
    for path in paths:
        resolved = path.expanduser().resolve()
        if any(root.is_relative_to(resolved) for root in roots) or any(
            resolved.is_relative_to(root) for root in roots[1:]
        ):
            raise ValueError("mount only the Hermes runtime, outside credential directories")
        if resolved.is_relative_to(hermes_home) and not any(
            resolved.is_relative_to(root) for root in runtime
        ):
            raise ValueError("mount only the Hermes runtime, outside the Hermes profile")
    return list(dict.fromkeys(str(path.expanduser().absolute()) for path in paths))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--opencode", default="opencode", help="OpenCode v2 executable")
    parser.add_argument("--hermes", default="hermes", help="Hermes executable")
    parser.add_argument("--integration", choices=INTEGRATIONS, default="opencode")
    parser.add_argument("--runtime-mount", action="append", type=Path, default=[],
                        help="additional read-only Hermes runtime path")
    parser.add_argument("pilot_args", nargs=argparse.REMAINDER,
                        help="arguments for run_handoff.py after --")
    args = parser.parse_args()
    if not args.pilot_args or args.pilot_args[0] != "--" or len(args.pilot_args) == 1:
        parser.error("pass -- followed by the checkpoint pilot arguments")
    pilot = args.pilot_args[1:]
    if any(arg == "--executor" or arg.startswith("--executor=") for arg in pilot):
        parser.error("the Hermes wrapper selects the command executor")
    try:
        opencode = binary_path(args.opencode)
        hermes = binary_path(args.hermes)
        if not isolation.available() or not isolation.available(network=True):
            raise ValueError("the Hermes pilot requires both Bubblewrap modes")
        mounts = runtime_mounts(hermes, args.runtime_mount)
        key, credential_id = export_key(opencode, args.integration)
    except ValueError as exc:
        parser.error(str(exc))
    env = {
        **os.environ,
        "OPENCODE_GO_API_KEY": key,
        "AUTOROUTER_HERMES": str(hermes),
        "AUTOROUTER_EVAL_PROFILE": json.dumps([str(hermes), args.integration, credential_id]),
        "AUTOROUTER_EVAL_CMD": shlex.join(["python3", str(HERE / "adapters/hermes_go.py")]),
        "AUTOROUTER_EVAL_MOUNTS": json.dumps(mounts),
        "AUTOROUTER_EVAL_FORWARD": json.dumps(["AUTOROUTER_HERMES", "OPENCODE_GO_API_KEY"]),
    }
    # The key travels in the child environment. Neither argv nor checkpoint JSON receives it.
    command = [sys.executable, str(HERE / "run_handoff.py"), *pilot]
    if "--resume" not in pilot:
        command.extend(("--executor", "command"))
    return subprocess.run(command, env=env).returncode


if __name__ == "__main__":
    raise SystemExit(main())
