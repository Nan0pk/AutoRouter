#!/usr/bin/env python3
"""Validate an AutoRouter config.

By default this runs **custom structural checks** (model-ref format, tiers,
ceiling hints, fallback membership) and, with --catalog, resolves model IDs and
variants. It is not JSON Schema validation. Pass --schema PATH to additionally
validate against a JSON Schema (requires the optional `jsonschema` package).

Usage:
    python3 scripts/validate_router.py --config examples/generic/router.config.json
    python3 scripts/validate_router.py --config my-router.json --catalog data/catalog.example.json
    python3 scripts/validate_router.py --config my-router.json --schema schema/autorouter.schema.json
"""
import argparse
import json
import re
import sys

MODEL_REF = re.compile(r"^[^/]+/[^#]+(#.+)?$")
TIERS = {"T0", "T1", "T2", "T3", "T4", "T5"}
CEILING_HINTS = (
    "not ", "avoid", "never", "reserve", "only", "weak", "sparingly",
    "limit", "escalate", "subject to", "don't", "do not", "cannot", "can't",
)


def strip_jsonc(s):
    out, i, n, in_str, esc = [], 0, len(s), False, False
    while i < n:
        c = s[i]
        if in_str:
            out.append(c)
            if esc:
                esc = False
            elif c == "\\":
                esc = True
            elif c == '"':
                in_str = False
            i += 1
            continue
        if c == '"':
            in_str = True
            out.append(c)
            i += 1
            continue
        if c == "/" and i + 1 < n and s[i + 1] == "/":
            while i < n and s[i] != "\n":
                i += 1
            continue
        if c == "/" and i + 1 < n and s[i + 1] == "*":
            i += 2
            while i + 1 < n and not (s[i] == "*" and s[i + 1] == "/"):
                i += 1
            i += 2
            continue
        out.append(c)
        i += 1
    text = "".join(out)
    return re.sub(r",(\s*[}\]])", r"\1", text)


def load(path):
    raw = open(path, encoding="utf-8").read()
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return json.loads(strip_jsonc(raw))


def split_ref(ref):
    prov, rest = ref.split("/", 1)
    mid, _, variant = rest.partition("#")
    return prov, mid, variant or None


def catalog_variants(entry):
    """Variants may be plain strings or {id: ...} objects; normalise to ids."""
    return [v.get("id") if isinstance(v, dict) else v for v in (entry.get("variants") or [])]


def main():
    ap = argparse.ArgumentParser(description="Validate an AutoRouter config.")
    ap.add_argument("--config", required=True)
    ap.add_argument("--catalog")
    ap.add_argument("--schema", help="also validate against this JSON Schema (requires jsonschema)")
    args = ap.parse_args()

    errors, warnings = [], []
    try:
        original = load(args.config)
    except Exception as e:  # noqa: BLE001
        print(f"ERROR: cannot parse {args.config}: {e}")
        return 1

    if args.schema:
        try:
            import jsonschema
        except ImportError:
            errors.append("--schema requires the 'jsonschema' package (pip install jsonschema)")
        else:
            try:
                jsonschema.validate(original, load(args.schema))
            except Exception as e:  # noqa: BLE001
                errors.append(f"schema validation failed: {str(e).splitlines()[0]}")

    # Accept both the generic shape and the orchestration plugin shape.
    cfg = original
    models = cfg.get("models")
    if models is None and "plugins" in cfg:
        # Plugin order is meaningful at runtime (hooks run in list order), so
        # find the entry that actually carries the model table instead of
        # assuming it is first.
        entries = [p for p in cfg["plugins"] if isinstance(p, dict)]
        entry = next(
            (p for p in entries
             if isinstance(p.get("options"), dict)
             and p["options"].get("models") is not None),
            entries[0] if entries else {},
        )
        opts = entry.get("options", {}) if isinstance(entry, dict) else {}
        models = opts.get("models")
        if isinstance(models, dict):
            models = [{"id": k, "description": v} for k, v in models.items()]
        cfg = {"models": models, "fallback": opts.get("fallback")}

    if not isinstance(models, list) or not models:
        errors.append("no models found (expected `models` array or plugin options.models)")

    catalog = None
    if args.catalog:
        cat = load(args.catalog)
        lst = cat if isinstance(cat, list) else cat.get("models") or cat.get("data") or []
        catalog = {(m["providerID"], m["id"]): m for m in lst if "providerID" in m and "id" in m}

    seen = set()
    t2 = []
    for m in models or []:
        mid = m.get("id")
        if not mid or not MODEL_REF.match(mid):
            errors.append(f"invalid model ref: {mid!r}")
            continue
        if mid in seen:
            warnings.append(f"duplicate model: {mid}")
        seen.add(mid)

        tier = m.get("tier")
        if tier is not None and tier not in TIERS:
            errors.append(f"{mid}: invalid tier {tier!r}")
        if tier == "T2":
            t2.append(mid)

        desc = (m.get("description") or "").lower()
        if len(desc) < 20:
            warnings.append(f"{mid}: description shorter than 20 chars")
        if desc and not any(h in desc for h in CEILING_HINTS):
            warnings.append(f"{mid}: description has no capability ceiling ('Not for ...')")

        if catalog is not None:
            prov, mid_name, variant = split_ref(mid)
            entry = catalog.get((prov, mid_name))
            if not entry:
                errors.append(f"{mid}: not found in catalog")
            else:
                if entry.get("enabled") is False:
                    warnings.append(f"{mid}: catalog model is disabled")
                if variant and variant not in catalog_variants(entry):
                    errors.append(f"{mid}: variant #{variant} not in {catalog_variants(entry)}")

    fb = cfg.get("fallback")
    if fb is None:
        warnings.append("no fallback set")
    elif fb not in seen:
        errors.append(f"fallback {fb!r} is not among the configured models")

    mode = "yes" if args.schema else "no (custom checks)"
    print(f"Config: {args.config}")
    print(f"Models: {len(seen)}  |  T2 candidates: {len(t2)}  |  schema: {mode}")
    for w in warnings:
        print(f"  WARN  {w}")
    for e in errors:
        print(f"  ERROR {e}")
    if not errors:
        print("OK")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
