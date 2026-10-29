#!/usr/bin/env python3
"""Calibrate the complexity gate against a labelled prompt set.

Under-routing (sending hard or high-stakes work to a weak model) and
over-routing (burning strong-model quota on easy work) have very different
costs, so they are counted and reported separately -- never as one "accuracy"
number.

Usage:
    python3 scripts/calibrate_gate.py --labels tests/gate/labeled_prompts.csv
    python3 scripts/calibrate_gate.py --labels tests/gate/labeled_prompts.csv --split test
    python3 scripts/calibrate_gate.py --labels tests/gate/labeled_prompts.csv --json

Exit status is non-zero when a threshold is exceeded, so CI can enforce the
gate's error profile.
"""
import argparse
import csv
import json
import sys

from classify import classify

RANK = {"trivial": 0, "standard": 1, "complex": 2}
CLASSES = ["trivial", "standard", "complex"]


def read_labels(path):
    with open(path, newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    for i, row in enumerate(rows, start=2):
        if row["label"] not in RANK:
            raise SystemExit(f"ERROR: {path}:{i}: bad label {row['label']!r}")
        if row.get("split") not in ("dev", "test"):
            raise SystemExit(f"ERROR: {path}:{i}: bad split {row.get('split')!r}")
    return rows


def evaluate(rows):
    results = []
    for row in rows:
        pred = classify(row["prompt"])
        label, got = row["label"], pred["class"]
        direction = "match"
        if RANK[got] < RANK[label]:
            direction = "under"
        elif RANK[got] > RANK[label]:
            direction = "over"
        results.append(
            {
                "id": row["id"],
                "split": row["split"],
                "family": row["family"],
                "risk": row.get("risk") or "",
                "prompt": row["prompt"],
                "label": label,
                "predicted": got,
                "direction": direction,
                "score": pred["score"],
                "overrides": pred["overrides"],
            }
        )
    return results


def summarise(results):
    n = len(results)
    confusion = {label: {pred: 0 for pred in CLASSES} for label in CLASSES}
    for r in results:
        confusion[r["label"]][r["predicted"]] += 1
    under = [r for r in results if r["direction"] == "under"]
    over = [r for r in results if r["direction"] == "over"]
    risk_rows = [r for r in results if r["risk"]]
    missed_risk = [r for r in risk_rows if r["predicted"] != "complex"]
    by_family = {}
    for r in results:
        b = by_family.setdefault(r["family"], {"n": 0, "under": 0, "over": 0})
        b["n"] += 1
        b["under"] += r["direction"] == "under"
        b["over"] += r["direction"] == "over"
    return {
        "n": n,
        "confusion": confusion,
        "under": len(under),
        "over": len(over),
        "under_rate": round(len(under) / n, 4) if n else 0.0,
        "over_rate": round(len(over) / n, 4) if n else 0.0,
        "risk_total": len(risk_rows),
        "risk_missed": len(missed_risk),
        "by_family": by_family,
        "under_cases": under,
        "over_cases": over,
        "risk_missed_cases": missed_risk,
    }


def print_report(split, s, max_under, max_over):
    print(f"Gate calibration (split={split}, n={s['n']})")
    print()
    header = "label \\ predicted"
    print(f"{header:18} {'trivial':>8} {'standard':>9} {'complex':>8}")
    for label in CLASSES:
        c = s["confusion"][label]
        print(f"{label:18} {c['trivial']:>8} {c['standard']:>9} {c['complex']:>8}")
    print()
    print(f"under-routing : {s['under']:>3}/{s['n']} = {s['under_rate']:.1%} "
          f"(max {max_under:.1%})  -- hard work sent to a weaker model")
    print(f"over-routing  : {s['over']:>3}/{s['n']} = {s['over_rate']:.1%} "
          f"(max {max_over:.1%})  -- strong quota burned on easy work")
    print(f"risk prompts  : {s['risk_total'] - s['risk_missed']:>3}/{s['risk_total']} "
          f"kept at complex (must be {s['risk_total']}/{s['risk_total']})")
    print()
    print("by family:")
    for family, b in sorted(s["by_family"].items()):
        print(f"  {family:24} n={b['n']:>3}  under={b['under']:>2}  over={b['over']:>2}")
    if s["risk_missed_cases"]:
        print("\nrisk prompts routed below complex:")
        for r in s["risk_missed_cases"]:
            print(f"  {r['id']} [{r['risk']}] -> {r['predicted']} (score {r['score']}): {r['prompt']}")
    if s["under_cases"]:
        print("\nunder-routed:")
        for r in s["under_cases"]:
            print(f"  {r['id']} {r['label']}->{r['predicted']} (score {r['score']}): {r['prompt']}")
    if s["over_cases"]:
        print("\nover-routed:")
        for r in s["over_cases"]:
            print(f"  {r['id']} {r['label']}->{r['predicted']} (score {r['score']}): {r['prompt']}")


def main():
    ap = argparse.ArgumentParser(description="Calibrate the complexity gate.")
    ap.add_argument("--labels", default="tests/gate/labeled_prompts.csv")
    ap.add_argument("--split", choices=["all", "dev", "test"], default="all")
    ap.add_argument("--max-under", type=float, default=0.05, help="max under-routing rate (default 0.05)")
    ap.add_argument("--max-over", type=float, default=0.20, help="max over-routing rate (default 0.20)")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    rows = read_labels(args.labels)
    if args.split != "all":
        rows = [r for r in rows if r["split"] == args.split]
    if not rows:
        raise SystemExit(f"ERROR: no rows for split {args.split!r}")

    s = summarise(evaluate(rows))
    failures = []
    if s["under_rate"] > args.max_under:
        failures.append(f"under-routing {s['under_rate']:.1%} > {args.max_under:.1%}")
    if s["over_rate"] > args.max_over:
        failures.append(f"over-routing {s['over_rate']:.1%} > {args.max_over:.1%}")
    if s["risk_missed"]:
        failures.append(f"{s['risk_missed']} high-risk prompt(s) routed below complex")

    if args.json:
        print(json.dumps({"split": args.split, **s, "failures": failures}, indent=2))
    else:
        print_report(args.split, s, args.max_under, args.max_over)
        print()
        if failures:
            for f in failures:
                print(f"FAIL  {f}")
        else:
            print(f"OK: under-routing and over-routing within budget on split={args.split}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
