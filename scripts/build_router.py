#!/usr/bin/env python3
"""Generate a starter router config from scored tiers.

Reads the JSON produced by score_models.py and emits either an OpenCode
(`opencode-auto-model` plugin) config or a harness-agnostic config.

Descriptions are generated from tier templates and MUST be refined by hand —
they are a starting point, not a substitute for judgement.

Usage:
    python3 scripts/build_router.py --scored scored.json --harness opencode --out my-router.json
    python3 scripts/build_router.py --scored scored.json --harness generic --out generic.json
"""
import argparse
import json
import sys

TIER_INFO = {
    "T0": ("throwaway", "mechanical or disposable work", "real feature work, logic, or debugging"),
    "T1": ("volume", "simple, high-volume, low-stakes work", "multi-file or logic-heavy tasks"),
    "T2": ("routine workhorse", "most day-to-day engineering", "deep architecture or the hardest debugging"),
    "T3": ("strong", "hard-ish work: multi-step fixes, subtle logic, careful refactors", "the absolute frontier"),
    "T4": ("hard", "hard multi-file work, complex refactors, and subtle bugs", "long-horizon routine work"),
    "T5": ("frontier", "the hardest, highest-stakes work only", "routine tasks"),
}


def ref(row):
    """Compose provider/model from a scored row (accepts already-qualified ids)."""
    model = row["model"]
    if "/" in model:
        return model
    provider = (row.get("provider") or "").strip()
    return f"{provider}/{model}" if provider else model


def describe(row):
    name, use, notfor = TIER_INFO[row["tier"]]
    cap = row.get("capability")
    req = row.get("req_per_month")
    bits = [f"{row['tier']} {name}."]
    if cap is not None:
        bits.append(f"Capability {cap:.2f}.")
    if req is not None:
        bits.append(f"Allowance ~{req} req/mo.")
    bits.append(f"Use for {use}.")
    bits.append(f"Not for {notfor}.")
    if row.get("notes"):
        bits.append(row["notes"])
    return " ".join(bits)


def pick_fallback(rows):
    """Prefer a T2 row with the best value, then capacity, then capability."""
    pool = [r for r in rows if r["tier"] == "T2"] or rows
    pool = sorted(
        pool,
        key=lambda r: (r.get("value") or 0, r.get("capacity_score") or 0, r.get("capability") or 0),
        reverse=True,
    )
    return pool[0] if pool else None


def main():
    ap = argparse.ArgumentParser(description="Build a starter router config.")
    ap.add_argument("--scored", required=True)
    ap.add_argument("--harness", choices=["opencode", "generic"], default="opencode")
    ap.add_argument("--out", help="output file (default: stdout)")
    ap.add_argument("--delegation", action="store_true", help="include an empty delegation block (generic only)")
    args = ap.parse_args()

    data = json.load(open(args.scored, encoding="utf-8"))
    rows = data["models"] if isinstance(data, dict) else data
    rows = [r for r in rows if r.get("capability") is not None]
    rows.sort(key=lambda r: (r["tier"], -(r.get("capability") or 0)))

    fbrow = pick_fallback(rows)
    fallback = ref(fbrow) if fbrow else None

    if args.harness == "opencode":
        models = {ref(r): describe(r) for r in rows}
        cfg = {
            "plugins": [
                {
                    "package": "opencode-auto-model",
                    "options": {"fallback": fallback, "models": models},
                }
            ]
        }
    else:
        models = [
            {
                "id": ref(r),
                "tier": r["tier"],
                "capability": r.get("capability"),
                "quota": {
                    "requests_per_month": r.get("req_per_month"),
                    "monthly_limit_usd": r.get("monthly_limit_usd"),
                },
                "description": describe(r),
            }
            for r in rows
        ]
        cfg = {
            "version": 1,
            "default_model": fallback,
            "fallback": fallback,
            "gate": {"enabled": True, "main_floor": -2, "complex_threshold": 5},
            "models": models,
        }
        if args.delegation:
            cfg["delegation"] = {
                "enabled": True,
                "orchestrator_prompt": "templates/orchestrator.md",
                "agents": [],
            }

    out = json.dumps(cfg, indent=2)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            f.write(out + "\n")
        print(f"Wrote {args.out}", file=sys.stderr)
    else:
        print(out)


if __name__ == "__main__":
    main()
