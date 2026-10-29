#!/usr/bin/env python3
"""Score models from provenance-tracked benchmark + quota data.

Inputs
------
`--benchmarks` is a **long-format** observation table: one row per
(provider, model, metric) measurement, carrying its own provenance:

    provider,model,metric,value,source,source_kind,benchmark_version,
    collected_on,conditions,notes

`--quota` is one row per (provider, model) with economics:

    provider,model,monthly_limit_usd,req_per_month,input_cost,output_cost,
    cache_read_cost,cache_write_cost,notes

Both files are joined on **(provider, model)**.

Outputs
-------
For every model: a weighted capability score, the **evidence coverage** that
backs it (fraction of metric weight actually observed), an explicit
token-mix + cache cost estimate, and a tier. Sparse models get
`"tier": null` / `"tier_status": "insufficient evidence"` instead of a tier.

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
REF_MAX = {"deepswe": 80.0, "tb4": 70.0, "swe_bench": 97.0, "gpqa": 95.0, "swe_pro": 70.0}
REF_ELO = {"lmarena_coding": (1400.0, 160.0)}  # elo 1400..1560 -> 0..1
REF_AA = 55.0

# Capability -> tier cuts (highest first).
TIER_CUTS = [("T5", 0.85), ("T4", 0.75), ("T3", 0.65), ("T2", 0.55), ("T1", 0.45)]

# Independent boards outrank aggregators, which outrank vendor cards.
SOURCE_RANK = {"independent": 3, "aggregate": 2, "lab_card": 1}
SOURCE_KINDS = set(SOURCE_RANK)

# Every observation must carry these, or the score is not reproducible.
REQUIRED_PROVENANCE = ("source", "source_kind", "benchmark_version", "collected_on", "conditions")

# Two observations for the same metric that differ by more than this are a
# recorded source conflict, not something to average away silently.
CONFLICT_REL_TOL = 0.05

INSUFFICIENT = "insufficient evidence"


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


def row_key(row, path, lineno):
    provider = (row.get("provider") or "").strip()
    model = (row.get("model") or "").strip()
    if not provider or not model:
        raise SystemExit(
            f"ERROR: {path}:{lineno}: both `provider` and `model` are required "
            f"(join key is (provider, model)); got provider={provider!r} model={model!r}"
        )
    return provider, model


def load_observations(path, warnings):
    """{(provider, model): {metric: [observation, ...]}}"""
    out = {}
    seen = set()
    for i, row in enumerate(read_csv(path), start=2):
        key = row_key(row, path, i)
        metric = (row.get("metric") or "").strip()
        if not metric:
            raise SystemExit(f"ERROR: {path}:{i}: empty `metric`")
        value = fnum(row.get("value"))
        if value is None:
            warnings.append(f"{path}:{i}: {key[0]}/{key[1]} {metric}: non-numeric value, skipped")
            continue
        missing = [c for c in REQUIRED_PROVENANCE if not (row.get(c) or "").strip()]
        if missing:
            raise SystemExit(
                f"ERROR: {path}:{i}: {key[0]}/{key[1]} {metric}: missing provenance "
                f"{missing}. Every observation needs source, source_kind, "
                f"benchmark_version, collected_on and conditions."
            )
        kind = row["source_kind"].strip()
        if kind not in SOURCE_KINDS:
            raise SystemExit(
                f"ERROR: {path}:{i}: unknown source_kind {kind!r}; expected one of {sorted(SOURCE_KINDS)}"
            )
        dedupe = (key, metric, row["source"].strip(), row["collected_on"].strip(),
                  row["benchmark_version"].strip(), value)
        if dedupe in seen:
            raise SystemExit(f"ERROR: {path}:{i}: duplicate observation {dedupe[:2]}")
        seen.add(dedupe)
        obs = {
            "value": value,
            "source": row["source"].strip(),
            "source_kind": kind,
            "benchmark_version": row["benchmark_version"].strip(),
            "collected_on": row["collected_on"].strip(),
            "conditions": row["conditions"].strip(),
            "notes": (row.get("notes") or "").strip(),
        }
        out.setdefault(key, {}).setdefault(metric, []).append(obs)
    return out


def load_quota(path):
    out = {}
    for i, row in enumerate(read_csv(path), start=2):
        key = row_key(row, path, i)
        if key in out:
            raise SystemExit(f"ERROR: {path}:{i}: duplicate quota row for {key[0]}/{key[1]}")
        out[key] = row
    return out


def select_observation(observations, metric):
    """Prefer independent > aggregate > lab card, then the most recent reading.

    Returns (chosen, conflict) where conflict is a dict when retained
    observations disagree beyond CONFLICT_REL_TOL.
    """
    ranked = sorted(
        observations,
        key=lambda o: (SOURCE_RANK[o["source_kind"]], o["collected_on"]),
        reverse=True,
    )
    chosen = ranked[0]
    values = [o["value"] for o in ranked]
    lo, hi = min(values), max(values)
    scale = max(abs(lo), abs(hi), 1e-9)
    conflict = None
    if (hi - lo) / scale > CONFLICT_REL_TOL:
        conflict = {
            "metric": metric,
            "retained": {
                "value": chosen["value"],
                "source": chosen["source"],
                "source_kind": chosen["source_kind"],
            },
            "reported": [
                {"value": o["value"], "source": o["source"], "source_kind": o["source_kind"]}
                for o in ranked
            ],
            "policy": "independent board retained; disagreement recorded, not averaged",
        }
    return chosen, conflict


def request_cost(input_tokens, output_tokens, cache_hit_rate, ic, oc, cr, cw):
    """Cost of one request under an explicit token mix + cache model.

    A cache miss is billed at the cache-write price, a hit at the cache-read
    price. Missing cache prices fall back to the plain input price (i.e. no
    caching discount is assumed).
    """
    cr = ic if cr is None else cr
    cw = ic if cw is None else cw
    hit = max(0.0, min(1.0, cache_hit_rate))
    cached = input_tokens * hit
    uncached = input_tokens - cached
    return (uncached * cw + cached * cr + output_tokens * oc) / 1_000_000.0


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
    ap.add_argument("--benchmarks", required=True, help="long-format observation CSV")
    ap.add_argument("--quota", required=True, help="quota/economics CSV")
    ap.add_argument("--out", help="write scored JSON here")
    ap.add_argument(
        "--weights",
        help="override weights, e.g. 'deepswe=0.4,tb4=0.3,swe_bench=0.2,lmarena_coding=0.05,aa_intelligence=0.05'",
    )
    ap.add_argument("--json", action="store_true", help="print JSON instead of a table")
    ap.add_argument(
        "--min-coverage",
        type=float,
        default=0.5,
        help="fraction of metric weight that must have evidence before a tier is assigned (default 0.5)",
    )
    ap.add_argument("--input-tokens", type=int, default=20000, help="prompt tokens per request (default 20000)")
    ap.add_argument("--output-tokens", type=int, default=2000, help="completion tokens per request (default 2000)")
    ap.add_argument(
        "--cache-hit-rate",
        type=float,
        default=0.5,
        help="fraction of prompt tokens served from cache (default 0.5)",
    )
    args = ap.parse_args()

    if not 0.0 <= args.min_coverage <= 1.0:
        raise SystemExit("ERROR: --min-coverage must be between 0 and 1")
    if not 0.0 <= args.cache_hit_rate <= 1.0:
        raise SystemExit("ERROR: --cache-hit-rate must be between 0 and 1")

    weights = dict(DEFAULT_WEIGHTS)
    if args.weights:
        weights = {}
        for pair in args.weights.split(","):
            k, _, v = pair.partition("=")
            weights[k.strip()] = float(v)
    total_weight = sum(weights.values())
    if total_weight <= 0:
        raise SystemExit("ERROR: weights must sum to a positive number")

    warnings = []
    bench = load_observations(args.benchmarks, warnings)
    quota = load_quota(args.quota)
    models = sorted(set(bench) | set(quota))

    rows = []
    for provider, model in models:
        b, q = bench.get((provider, model), {}), quota.get((provider, model), {})
        if not b:
            warnings.append(f"{provider}/{model}: no benchmark observations (insufficient evidence)")
        elif not (set(b) & set(weights)):
            warnings.append(
                f"{provider}/{model}: none of the observed metrics {sorted(b)} are in the "
                f"weight set (insufficient evidence)"
            )
        if not q:
            warnings.append(f"{provider}/{model}: no quota row")

        num = 0.0
        present, missing, conflicts, evidence = [], [], [], {}
        for metric, w in weights.items():
            observations = b.get(metric)
            if not observations:
                missing.append(metric)
                continue
            chosen, conflict = select_observation(observations, metric)
            nv = norm(metric, chosen["value"])
            if nv is None:
                missing.append(metric)
                warnings.append(f"{provider}/{model}: cannot normalise metric {metric!r}")
                continue
            num += w * nv
            present.append(metric)
            evidence[metric] = {
                "value": chosen["value"],
                "normalised": round(nv, 4),
                "source": chosen["source"],
                "source_kind": chosen["source_kind"],
                "benchmark_version": chosen["benchmark_version"],
                "collected_on": chosen["collected_on"],
                "conditions": chosen["conditions"],
                "notes": chosen["notes"],
            }
            if conflict:
                conflicts.append(conflict)

        observed_weight = sum(weights[m] for m in present)
        coverage = round(observed_weight / total_weight, 3)
        # Normalise over observed weight so a sparse model is not silently
        # scored low for being unmeasured -- coverage carries that signal.
        cap = round(num / observed_weight, 3) if present else None

        req = fnum(q.get("req_per_month"))
        lim = fnum(q.get("monthly_limit_usd"))
        ic, oc = fnum(q.get("input_cost")), fnum(q.get("output_cost"))
        cr, cw = fnum(q.get("cache_read_cost")), fnum(q.get("cache_write_cost"))

        cost = None
        if ic is not None and oc is not None:
            cost = round(
                request_cost(
                    args.input_tokens, args.output_tokens, args.cache_hit_rate, ic, oc, cr, cw
                ),
                6,
            )
        value = round(cap / (0.001 + cost), 3) if (cap is not None and cost is not None) else None
        capacity = round(cap * math.log10(req + 10), 3) if (cap is not None and req is not None) else None

        if cap is None or coverage < args.min_coverage:
            tier, status = None, INSUFFICIENT
        else:
            tier, status = tier_of(cap), "ok"

        notes = []
        for metric in present:
            n = evidence[metric].get("notes")
            if n:
                notes.append(n)
        qnote = (q.get("notes") or "").strip()
        if qnote:
            notes.append(qnote)
        note_text = "; ".join(dict.fromkeys(notes))

        rows.append(
            {
                "provider": provider,
                "model": model,
                "key": f"{provider}/{model}",
                "tier": tier,
                "tier_status": status,
                "tier_reason": (
                    f"evidence coverage {coverage:.2f} < required {args.min_coverage:.2f}"
                    if status == INSUFFICIENT and coverage < args.min_coverage
                    else ("no normalisable capability evidence" if cap is None else "ok")
                ),
                "capability": cap,
                "coverage": coverage,
                "metrics_used": present,
                "metrics_missing": missing,
                "evidence": evidence,
                "conflicts": conflicts,
                "req_per_month": int(req) if req is not None else None,
                "monthly_limit_usd": lim,
                "input_cost": ic,
                "output_cost": oc,
                "cache_read_cost": cr,
                "cache_write_cost": cw,
                "est_cost_per_request": cost,
                "value": value,
                "capacity_score": capacity,
                "notes": note_text,
            }
        )

    rows.sort(
        key=lambda r: (
            r["tier"] is None,
            -(int(r["tier"][1:]) if r["tier"] else -1),
            -(r["capability"] or 0),
        )
    )

    cost_model = {
        "input_tokens": args.input_tokens,
        "output_tokens": args.output_tokens,
        "cache_hit_rate": args.cache_hit_rate,
        "billing": "uncached input billed at cache_write_cost, cached input at cache_read_cost, "
                   "output at output_cost; cache prices fall back to input_cost when absent",
        "units": "USD per request",
    }
    payload = {
        "schema": "schema/scored.schema.json",
        "weights": weights,
        "min_coverage": args.min_coverage,
        "cost_model": cost_model,
        "models": rows,
    }

    if args.json:
        print(json.dumps(payload, indent=2))
    else:
        hdr = (
            f"{'model':34} {'tier':21} {'cap':>5} {'cov':>5} {'$/req':>8} {'value':>8} {'req/mo':>9}  notes"
        )
        print(hdr)
        print("-" * len(hdr))
        for r in rows:
            print(
                f"{r['model'][:34]:34} {(r['tier'] or r['tier_status']):21} "
                f"{_fmt(r['capability'], '.3f', 5)} {r['coverage']:>5.2f} "
                f"{_fmt(r['est_cost_per_request'], '.4f', 8)} {_fmt(r['value'], '.1f', 8)} "
                f"{_fmt(r['req_per_month'], 'd', 9)}  {r['notes'][:34]}"
            )
        for w in warnings:
            print(f"WARN  {w}", file=sys.stderr)

    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2)
        print(f"\nWrote {args.out} ({len(rows)} models)", file=sys.stderr)


if __name__ == "__main__":
    main()
