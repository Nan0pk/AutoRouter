#!/usr/bin/env python3
"""Use the active OpenCode credential for an isolated Hermes Go handoff run."""
import argparse
import hashlib
import json
import os
import shlex
import shutil
import subprocess
import sys
import time
from pathlib import Path

import isolation

HERE = Path(__file__).resolve().parent
INTEGRATIONS = ("opencode", "opencode-go")


def safe_header(value):
    return isinstance(value, str) and bool(value) and value.isascii() and value.isprintable() and not any(
        char.isspace() for char in value
    )


def active_credential(export, integration):
    if not isinstance(export, list) or any(
        not isinstance(row, dict) or row.get("integrationID") != integration for row in export
    ):
        raise ValueError("unexpected OpenCode export format or integration")
    active = [row for row in export if row.get("active") is True]
    if len(active) != 1:
        raise ValueError("the selected OpenCode integration must have exactly one active account")
    entry = active[0]
    value = entry.get("value")
    if not isinstance(value, dict) or value.get("type") not in ("key", "oauth"):
        raise ValueError("the active OpenCode credential must be an API key or Console OAuth login")
    organization = None
    if value["type"] == "oauth":
        metadata = value.get("metadata")
        expires = value.get("expires")
        if integration != "opencode" or value.get("methodID") != "device" or not isinstance(metadata, dict) or (
            metadata.get("server") != "https://opencode.ai/console"
        ):
            raise ValueError("OAuth must be an official OpenCode Console device login")
        if type(expires) not in (int, float) or not expires > time.time() * 1000:
            raise ValueError("OpenCode access token expired; refresh the login in OpenCode before running")
        organization = metadata.get("orgID")
        if not safe_header(organization):
            raise ValueError("the OpenCode OAuth login must select a valid workspace")
    key = value.get("access" if value["type"] == "oauth" else "key")
    credential_id = entry.get("id")
    if not safe_header(key) or not isinstance(credential_id, str) or not credential_id:
        raise ValueError("the active OpenCode credential entry is incomplete")
    return key, credential_id, organization


def export_credential(binary, integration):
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
    return active_credential(export, integration)


def binary_path(value):
    path = shutil.which(value)
    if not path:
        raise ValueError(f"executable unavailable: {value}")
    return Path(path).absolute()


def managed_runtime(binary):
    home = Path.home().resolve()
    hermes_home = Path(os.environ.get("HERMES_HOME", home / ".hermes")).expanduser().resolve()
    source = hermes_home / "hermes-agent"
    if binary != home / ".local/bin/hermes" and not binary.is_relative_to(source):
        return None
    state = hermes_home / "installs" / hashlib.sha256(str(source).encode()).hexdigest()[:16]
    facts = state / "facts.json"
    if not facts.is_file():
        return None  # Older source installs keep their dependencies in the source tree.
    try:
        selected = json.loads(facts.read_text())["packages"]["venv"]["environment"]
        venv = Path(selected).resolve()
        if venv.name != "venv" or venv.parent.parent != state / "environments" or not all(
            (venv / name).is_file() for name in ("pyvenv.cfg", "bin/python")
        ):
            raise ValueError
    except (OSError, ValueError, KeyError, TypeError):
        raise ValueError("Hermes has no usable selected dependency environment; run hermes pm repair") from None
    return source, venv


def runtime_mounts(binary, extra, managed=None):
    home = Path.home().resolve()
    hermes_home = Path(os.environ.get("HERMES_HOME", home / ".hermes")).expanduser().resolve()
    paths = [binary, HERE / "adapters/hermes_go.py", *extra]
    if managed:
        paths.append(managed[1])
    if binary.is_relative_to(hermes_home) or binary == home / ".local/bin/hermes":
        paths.extend(path for path in (hermes_home / "hermes-agent", hermes_home / "tools",
                                       home / ".local/share/uv/python") if path.is_dir())
    roots = (hermes_home, home / ".local/share/opencode", home / ".config/opencode")
    runtime = tuple(hermes_home / name for name in ("bin", "hermes-agent", "tools"))
    if managed:
        runtime += (managed[1],)
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
        managed = managed_runtime(hermes)
        mounts = runtime_mounts(hermes, args.runtime_mount, managed)
        key, credential_id, organization = export_credential(opencode, args.integration)
    except ValueError as exc:
        parser.error(str(exc))
    env = {
        **os.environ,
        "OPENCODE_GO_API_KEY": key,
        "AUTOROUTER_HERMES_ORG_ID": organization or "",
        "AUTOROUTER_HERMES": str(hermes),
        "AUTOROUTER_EVAL_PROFILE": json.dumps([str(hermes), args.integration, credential_id, organization]),
        "AUTOROUTER_EVAL_CMD": shlex.join(["python3", str(HERE / "adapters/hermes_go.py")]),
        "AUTOROUTER_EVAL_MOUNTS": json.dumps(mounts),
        "AUTOROUTER_EVAL_FORWARD": json.dumps([
            "AUTOROUTER_HERMES", "OPENCODE_GO_API_KEY", "AUTOROUTER_HERMES_ORG_ID",
        ]),
    }
    if managed:
        source, venv = managed
        # Reuse the installed dependencies without provisioning a new environment per worker.
        command = [str(venv / "bin/python"), "-I", "-c",
                   f"import sys; sys.path.insert(0, {str(source)!r}); "
                   "from hermes_cli.main import main; sys.exit(main())"]
        env["AUTOROUTER_HERMES_RUNTIME"] = json.dumps(command)
        env["AUTOROUTER_EVAL_FORWARD"] = json.dumps([
            "AUTOROUTER_HERMES", "AUTOROUTER_HERMES_RUNTIME", "OPENCODE_GO_API_KEY",
            "AUTOROUTER_HERMES_ORG_ID",
        ])
    # The key travels in the child environment. Neither argv nor checkpoint JSON receives it.
    command = [sys.executable, str(HERE / "run_handoff.py"), *pilot]
    if "--resume" not in pilot:
        command.extend(("--executor", "command"))
    return subprocess.run(command, env=env).returncode


if __name__ == "__main__":
    raise SystemExit(main())
