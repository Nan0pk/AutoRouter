#!/usr/bin/env python3
"""Score models from benchmark + quota data, assign capability tiers.

Joins a benchmarks CSV and a quota CSV on the `model` column, computes a
weighted capability score (0-1) and value metrics, assigns T0-T5 tiers, and
optionally writes the result as JSON for build_router.py.

Usage:
    python3 scripts/score_models.py \
        --benchmarks data/benchmarks.example.csv \
        --quota data/quota.example.csv \
        --out scored.json
"""
import argparse
import csv
import json
import math
import sys

DEFAULT_WEIGHTS = {
    "deepswe": 0.30,
    "tb4": 0.25,
    "swe_bench": 0.15,
    "lmarena_coding": 0.15,
    "aa_intelligence": 0.15,
}

# Reference maxima used to normalise each metric to 0-1.
REF_MAX = {"deepswe": 80.0, "tb4": 70.0, "swe_bench": 97.0, "gpqa": 95.0}
REF_ELO = {"lmarena_coding": (1400.0, 160.0)}  # elo 1400..1560 -> 0..1
REF_AA = 55.0

# Capability -> tier cuts (highest first).
TIER_CUTS = [("T5", 0.85), ("T4", 0.75), ("T3", 0.65), ("T2", 0.55), ("T1", 0.45)]


def fnum(x):
    if x is None:
        return None
    s = str(x).strip()
    if s == "" or s.lower() in ("n/a", "na", "none", "-", "null"):
        return None
    try:
        return float(s)
    except ValueError:
        return None


def norm(metric, val):
    if val is None:
        return None
    if metric in REF_MAX:
        return max(0.0, min(1.0, val / REF_MAX[metric]))
    if metric in REF_ELO:
        base, span = REF_ELO[metric]
        return max(0.0, min(1.0, (val - base) / span))
    if metric == "aa_intelligence":
        return max(0.0, min(1.0, val / REF_AA))
    return None


def read_csv(path):
    with open(path, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def tier_of(cap):
    if cap is None:
        return "T1"
    for tier, cut in TIER_CUTS:
        if cap >= cut:
            return tier
    return "T0"


def _fmt(value, spec, width):
    s = format(value, spec) if value is not None else "-"
    return f"{s:>{width}}"


def main():
    ap = argparse.ArgumentParser(description="Score models and assign tiers.")
    ap.add_argument("--benchmarks", required=True)
    ap.add_argument("--quota", required=True)
    ap.add_argument("--out", help="write scored JSON here")
    ap.add_argument(
        "--weights",
        help="override weights, e.g. 'deepswe=0.4,tb4=0.3,swe_bench=0.2,lmarena_coding=0.05,aa_intelligence=0.05'",
    )
    ap.add_argument("--json", action="store_true", help="print JSON instead of a table")
    args = ap.parse_args()

    weights = dict(DEFAULT_WEIGHTS)
    if args.weights:
        weights = {}
        for pair in args.weights.split(","):
            k, _, v = pair.partition("=")
            weights[k.strip()] = float(v)

    bench = {r["model"]: r for r in read_csv(args.benchmarks)}
    quota = {r["model"]: r for r in read_csv(args.quota)}
    models = sorted(set(bench) | set(quota))

    rows = []
    for m in models:
        b, q = bench.get(m, {}), quota.get(m, {})
        prov = (b.get("provider") or q.get("provider") or "").strip() or None
        num = den = 0.0
        present = []
        for metric, w in weights.items():
            nv = norm(metric, fnum(b.get(metric)))
            if nv is None:
                continue
            num += w * nv
            den += w
            present.append(metric)
        cap = round(num / den, 3) if den > 0 else None

        req = fnum(q.get("req_per_month"))
        lim = fnum(q.get("monthly_limit_usd"))
        ic, oc = fnum(q.get("input_cost")), fnum(q.get("output_cost"))
        blended = round(ic + oc, 3) if (ic is not None and oc is not None) else None

        value = round(cap / (0.01 + blended), 3) if (cap and blended is not None) else None
        capacity = round(cap * math.log10(req + 10), 3) if (cap and req) else None

        rows.append(
            {
                "model": m,
                "provider": prov,
                "tier": tier_of(cap),
                "capability": cap,
                "value": value,
                "capacity_score": capacity,
                "req_per_month": int(req) if req is not None else None,
                "monthly_limit_usd": lim,
                "input_cost": ic,
                "output_cost": oc,
                "blended_cost": blended,
                "metrics_used": present,
                "notes": (b.get("notes") or q.get("notes") or "").strip(),
            }
        )

    rows.sort(key=lambda r: (r["tier"], -(r["capability"] or 0)))

    if args.json:
        print(json.dumps({"models": rows}, indent=2))
    else:
        hdr = f"{'model':34} {'tier':4} {'cap':>5} {'value':>6} {'req/mo':>9} {'blend$':>7}  notes"
        print(hdr)
        print("-" * len(hdr))
        for r in rows:
            print(
                f"{r['model'][:34]:34} {r['tier']:4} "
                f"{_fmt(r['capability'], '.3f', 5)} {_fmt(r['value'], '.2f', 6)} "
                f"{_fmt(r['req_per_month'], 'd', 9)} {_fmt(r['blended_cost'], '.2f', 7)}  "
                f"{r['notes'][:38]}"
            )

    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            json.dump({"models": rows}, f, indent=2)
        print(f"\nWrote {args.out} ({len(rows)} models)", file=sys.stderr)


if __name__ == "__main__":
    main()
