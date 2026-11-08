"""Filesystem isolation for eval workers and network isolation for verifiers."""
import functools
import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WORKDIR = "/work"
SYSTEM_DIRS = ("/usr", "/bin", "/lib", "/lib64")
NETWORK_FILES = (
    "/etc/hosts", "/etc/resolv.conf", "/etc/nsswitch.conf", "/etc/ssl/certs",
)
PROTECTED = tuple(
    ROOT / name for name in ("eval/fixtures", "eval/behavior", "eval/tasks.jsonl",
                             "eval/pins.json", "eval/stub_patches.json")
)


class IsolationError(RuntimeError):
    pass


def adapter_settings(workdir=None):
    """Expose only operator-selected files and environment variables."""
    mounts = json.loads(os.environ.get("AUTOROUTER_EVAL_MOUNTS", "[]"))
    forward = json.loads(os.environ.get("AUTOROUTER_EVAL_FORWARD", "[]"))
    if not isinstance(mounts, list) or not all(isinstance(p, str) for p in mounts):
        raise IsolationError("AUTOROUTER_EVAL_MOUNTS must be a JSON array of absolute paths")
    if not isinstance(forward, list) or not all(
        isinstance(k, str) and k.isidentifier()
        and (k.startswith("AUTOROUTER_") or k.endswith("_API_KEY"))
        for k in forward
    ):
        raise IsolationError("AUTOROUTER_EVAL_FORWARD must list AUTOROUTER_ or *_API_KEY variables")
    result = []
    protected = PROTECTED
    if workdir is not None:
        protected += (Path(workdir).resolve().parent,)
    for path in mounts:
        source = Path(path)
        if not source.is_absolute() or not source.exists():
            raise IsolationError(f"read-only adapter mount does not exist: {path}")
        source = source.resolve()
        if any(source.is_relative_to(p) or p.is_relative_to(source) for p in protected):
            raise IsolationError("adapter mounts cannot expose original fixtures, grading inputs or other trials")
        destination = Path(os.path.normpath(path))
        if any(destination.is_relative_to(Path(p)) for p in (WORKDIR, "/proc", "/dev", "/run")):
            raise IsolationError(f"adapter mount would overlap a sandbox path: {source}")
        result.append((str(source), str(destination)))
    return result, {key: os.environ[key] for key in forward if key in os.environ}


def command_argv(workdir, command, *, network=False, mounts=(), readonly=False):
    if not shutil.which("bwrap"):
        raise IsolationError("Bubblewrap (bwrap) is required")
    if any(ROOT.is_relative_to(Path(p)) for p in SYSTEM_DIRS):
        raise IsolationError("put the AutoRouter checkout outside system runtime directories")
    argv = ["bwrap", "--unshare-all", "--die-with-parent", "--new-session",
            "--cap-drop", "ALL"]
    if network or can_unshare_network():
        # When isolating the verifier, util-linux can create an empty network
        # namespace even on hosts that deny Bubblewrap loopback setup.
        argv.append("--share-net")
        if not network:
            argv[:0] = ["unshare", "--user", "--map-root-user", "--net", "--"]
    for path in SYSTEM_DIRS + (NETWORK_FILES if network else ()):
        if Path(path).exists():
            argv.extend(("--ro-bind", path, path))
    argv.extend(("--proc", "/proc", "--dev", "/dev", "--tmpfs", "/tmp",
                 "--dir", "/home/eval"))
    for src, dest in mounts:
        argv.extend(("--ro-bind", str(src), str(dest)))
    argv.extend(("--ro-bind" if readonly else "--bind", str(Path(workdir).resolve()),
                 WORKDIR, "--chdir", WORKDIR, "--"))
    return argv + list(command)


@functools.lru_cache(maxsize=1)
def can_unshare_network():
    if not shutil.which("unshare"):
        return False
    try:
        probe = subprocess.run(["unshare", "--user", "--map-root-user", "--net", "--",
                                "/usr/bin/true"], capture_output=True, timeout=5)
        return probe.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def run(workdir, command, *, network=False, mounts=(), readonly=False, env=None,
        input_text=None, timeout=60):
    argv = command_argv(workdir, command, network=network, mounts=mounts, readonly=readonly)
    # bwrap lives in a separate PID namespace and dies with this client, so a
    # timeout cannot leave a worker running against a later checkpoint.
    return subprocess.run(
        argv, env={"PATH": "/usr/local/bin:/usr/bin:/bin", "HOME": "/home/eval",
                   "LC_ALL": "C.UTF-8", "PYTHONDONTWRITEBYTECODE": "1", **(env or {})},
        input=input_text, capture_output=True, text=True, errors="replace", timeout=timeout,
    )


@functools.lru_cache(maxsize=2)
def available(network=False):
    """Probe the exact boundary we will use. Never fall back to the host."""
    try:
        with tempfile.TemporaryDirectory(prefix="autorouter-sandbox-probe-") as workdir:
            return run(workdir, ["/usr/bin/true"], network=network, timeout=5).returncode == 0
    except (OSError, subprocess.TimeoutExpired, IsolationError):
        return False
