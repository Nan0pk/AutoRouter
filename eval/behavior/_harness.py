"""Tiny runner for behavioural checks. Executed inside the sandbox."""
import json
import sys


def main(checks):
    results = []
    for name, fn in checks:
        try:
            fn()
            results.append({"name": name, "ok": True, "detail": ""})
        except Exception as e:  # noqa: BLE001 - a failing check is data, not a crash
            results.append({"name": name, "ok": False, "detail": f"{type(e).__name__}: {e}"})
    print(json.dumps({"checks": results}))
    sys.exit(0 if all(r["ok"] for r in results) else 1)
