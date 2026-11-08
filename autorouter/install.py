"""Locked, recoverable local installation with exact backups and conflict-aware undo."""
import fcntl
import hashlib
import json
import os
import shutil
import tempfile
import uuid
from contextlib import contextmanager
from pathlib import Path

from .hermes import worker

OWNED = ("plugins/autorouter", "plugins/model-providers/autorouter-opencode",
         "autorouter/policy.json", "config.yaml")  # activate configuration last
ROOT = Path(__file__).resolve().parents[1]


def guard(path, contents=False):
    if any(p.is_symlink() for p in (path, *path.parents)):
        raise ValueError("Setup refuses symlinked installation paths")
    if contents and path.is_dir() and any(p.is_symlink() for p in path.rglob("*")):
        raise ValueError("Setup refuses symlinked plugin contents")


def fingerprint(path):
    guard(path, contents=True)
    if not path.exists():
        return None
    if path.is_file():
        return hashlib.sha256(path.read_bytes()).hexdigest()
    h = hashlib.sha256()
    for p in sorted(path.rglob("*")):
        if "__pycache__" in p.relative_to(path).parts or p.suffix == ".pyc":
            continue  # ordinary Python execution must not make undo think source was edited
        h.update((str(p.relative_to(path)) + ("/" if p.is_dir() else "\0")).encode())
        if p.is_file():
            h.update(p.read_bytes())
    return h.hexdigest()


def write_private(path, data):
    guard(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temp = tempfile.mkstemp(prefix=".autorouter-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as out:
            out.write(data); out.flush(); os.fsync(out.fileno())
        os.replace(temp, path)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)


def save(path, value):
    write_private(path, (json.dumps(value, indent=2, allow_nan=False)+"\n").encode())


@contextmanager
def locked(home):
    guard(home)
    home.mkdir(parents=True, exist_ok=True, mode=0o700)
    for name in OWNED:
        guard(home/name, contents=True)
    state = home/"autorouter"; state.mkdir(mode=0o700, exist_ok=True)
    guard(state/".lock")
    with (state/".lock").open("a") as lock:
        os.chmod(state/".lock", 0o600)
        try:
            fcntl.flock(lock, fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:
            raise ValueError("Another AutoRouter setup is running") from None
        yield state


def copy_path(source, destination):
    guard(source, contents=True)
    if not source.exists():
        return
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if source.is_dir():
        shutil.copytree(source, destination)
    else:
        shutil.copy2(source, destination); os.chmod(destination, 0o600)


def remove(path):
    guard(path, contents=True)
    if path.is_dir():
        shutil.rmtree(path)
    elif path.exists():
        path.unlink()


def replace_path(source, destination, displaced):
    guard(destination)
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if destination.exists():
        displaced.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.replace(destination, displaced)
    os.replace(source, destination)


def backup_dir(state, name):
    if not isinstance(name, str) or len(name) != 32 or any(c not in "0123456789abcdef" for c in name):
        raise ValueError("Invalid AutoRouter backup reference")
    path = state/"backups"/name
    guard(path)
    return path


def restore(home, backup, old_hashes):
    if set(old_hashes) != set(OWNED):
        raise ValueError("Invalid AutoRouter backup manifest")
    for name in OWNED:
        if fingerprint(backup/"previous"/name) != old_hashes[name]:
            raise ValueError("AutoRouter backup changed; restore manually")
    for name in OWNED:
        remove(home/name)
        copy_path(backup/"previous"/name, home/name)
    prior = backup/"previous-install.json"
    if prior.exists():
        write_private(home/"autorouter/install.json", prior.read_bytes())
    else:
        remove(home/"autorouter/install.json")


def recover(home, state):
    journal = state/"transaction.json"
    if not journal.exists():
        return
    data = json.loads(journal.read_text()); backup = backup_dir(state, data["backup"])
    if set(data["before"]) != set(OWNED) or set(data["after"]) != set(OWNED):
        raise ValueError("Invalid AutoRouter transaction manifest")
    if any(fingerprint(home/p) not in (None, data["before"][p], data["after"][p]) for p in OWNED):
        raise ValueError("Pending setup conflicts with changed user files; recover manually")
    restore(home, backup, data["before"])
    journal.unlink()


def render_config(text, policy):
    return worker("render_config", {"text": text, "policy": policy})["text"]


def install(home, policy, renderer=render_config):
    from .plugin import Router
    Router(policy)  # validate before writing anything
    home = Path(home).absolute()
    with locked(home) as state:
        recover(home, state)
        previous = json.loads((state/"install.json").read_text()) if (state/"install.json").exists() else None
        before = {p: fingerprint(home/p) for p in OWNED}
        if previous and any(before[p] != previous["hashes"][p] for p in OWNED):
            raise ValueError("Installed files changed after setup; preserve those edits before reconfiguring")
        generation = state/"staging"/uuid.uuid4().hex
        generation.mkdir(parents=True, mode=0o700)
        try:
            for source, target in (("autorouter", OWNED[0]), ("opencode-go", OWNED[1])):
                destination = generation/target
                shutil.copytree(ROOT/"integrations/hermes"/source, destination)
                shutil.copytree(ROOT/"autorouter", destination/"routerlib", ignore=shutil.ignore_patterns("__pycache__"))
                shutil.copy2(ROOT/"scripts/classify.py", destination/"routerlib/classifier.py")
            save(generation/"autorouter/policy.json", policy)
            text = (home/"config.yaml").read_text() if (home/"config.yaml").exists() else ""
            write_private(generation/"config.yaml", renderer(text, policy).encode())
            after = {p: fingerprint(generation/p) for p in OWNED}
            if previous and before == after:
                return {"installed": True, "changed": False, "home": str(home)}
            name = uuid.uuid4().hex; backup = backup_dir(state, name)
            backup.mkdir(parents=True, mode=0o700)
            for p in OWNED:
                copy_path(home/p, backup/"previous"/p)
            if previous:
                save(backup/"previous-install.json", previous)
            save(state/"transaction.json", {"backup": name, "before": before, "after": after})
            try:
                for p in OWNED:
                    replace_path(generation/p, home/p, generation/"displaced"/p)
                save(state/"install.json", {"version": 1, "backup": name, "before": before, "hashes": after})
                (state/"transaction.json").unlink()
            except BaseException:
                restore(home, backup, before)
                (state/"transaction.json").unlink()
                raise
        finally:
            shutil.rmtree(generation, ignore_errors=True)
    return {"installed": True, "changed": True, "home": str(home), "backup": str(backup)}


def status(home):
    home = Path(home).absolute(); state = home/"autorouter"
    if (state/"transaction.json").exists():
        return {"state": "recovery_required"}
    if not (state/"install.json").exists():
        return {"state": "not_installed"}
    manifest = json.loads((state/"install.json").read_text())
    changed = [p for p in OWNED if fingerprint(home/p) != manifest["hashes"].get(p)]
    if changed:
        return {"state": "changed", "files": changed}
    report = state/"verification.json"
    verified = report.exists() and fingerprint(report) == manifest.get("verification_sha256")
    return {"state": "verified" if verified else "configured_unverified", "home": str(home)}


def undo(home):
    home = Path(home).absolute()
    with locked(home) as state:
        recover(home, state)
        path = state/"install.json"
        if not path.exists():
            raise ValueError("No AutoRouter installation to undo")
        manifest = json.loads(path.read_text())
        if set(manifest["hashes"]) != set(OWNED):
            raise ValueError("Invalid AutoRouter installation manifest")
        if any(fingerprint(home/p) != manifest["hashes"][p] for p in OWNED):
            raise ValueError("Installed files changed; undo refuses to overwrite later user edits")
        target = backup_dir(state, manifest["backup"])
        # Preflight the complete original backup before creating an undo journal.
        if set(manifest["before"]) != set(OWNED) or any(fingerprint(target/"previous"/p) != manifest["before"][p] for p in OWNED):
            raise ValueError("AutoRouter backup changed; restore manually")
        name = uuid.uuid4().hex; rollback = backup_dir(state, name)
        rollback.mkdir(parents=True, mode=0o700)
        for p in OWNED:
            copy_path(home/p, rollback/"previous"/p)
        save(rollback/"previous-install.json", manifest)
        save(state/"transaction.json", {"backup": name, "before": manifest["hashes"], "after": manifest["before"]})
        try:
            restore(home, target, manifest["before"])
            (state/"transaction.json").unlink()
        except BaseException:
            restore(home, rollback, manifest["hashes"])
            (state/"transaction.json").unlink()
            raise
    return {"state": "restored", "home": str(home)}
