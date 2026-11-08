"""Select eligible models without treating missing facts as free usage."""
import math
import time

MAX_CATALOG_AGE = 86400
MODES = ("subscriptions", "provider", "models", "free")


def price(value):
    return type(value) in (int, float) and math.isfinite(value) and value >= 0


def depleted(row):
    return any(type(w.get("used_percent")) in (int, float) and w["used_percent"] >= 100
               for w in (row.get("quota") or {}).get("windows", []))


def select_models(inventory, mode, providers=(), models=(), confirmed_allowances=()):
    if mode not in MODES:
        raise ValueError("Choose subscriptions, provider, models, or free")
    if mode == "provider" and len(set(providers)) != 1:
        raise ValueError("Choose exactly one connected provider")
    if mode == "models" and not models:
        raise ValueError("Choose exact provider/model IDs")
    now = time.time(); selected = []; excluded = []
    seen = set()
    for row in inventory.get("models", []):
        identifier = row.get("id")
        if identifier in seen or identifier != f"{row.get('provider')}/{row.get('model')}":
            raise ValueError("Inventory contains ambiguous model IDs")
        seen.add(identifier)
        if mode == "provider" and row["provider"] not in providers:
            continue
        if mode == "models" and identifier not in models:
            continue
        reason = None
        fetched = row.get("fetched_at")
        fresh = type(fetched) in (int, float) and math.isfinite(fetched) and 0 <= now-fetched <= MAX_CATALOG_AGE
        if row.get("harness_supported") is False:
            reason = row.get("compatibility_reason", "model unavailable in this harness")
        elif row.get("tools") is not True:
            reason = "tool support unknown or unavailable"
        elif depleted(row):
            reason = "reported allowance exhausted"
        elif mode == "free" or row.get("billing") == "free":
            if not (fresh and row.get("source") and row.get("billing") == "free" and
                    all(price(row.get(k)) and row[k] == 0 for k in ("input_price", "output_price"))):
                reason = "fresh verified zero input/output prices required"
            elif mode == "subscriptions":
                reason = "this mode selects included subscription allowances"
        elif row.get("billing") not in ("included", "free") and not (
            row.get("billing") == "unknown" and row["provider"] in confirmed_allowances
        ):
            reason = "additional spending or unknown billing is excluded"
        if reason:
            excluded.append({"id": identifier, "reason": reason})
        else:
            selected.append(dict(row))
    if mode == "models" and set(models) != {r["id"] for r in selected}:
        raise ValueError("Every selected model must be available and eligible; inspect billing, quota and exact IDs")
    if len(selected) < 2:
        raise ValueError("Need two distinct eligible models; inspect connected providers, billing, quota and tool support")
    def cost(row):
        return row["input_price"]*10 + row["output_price"] if all(price(row.get(k)) for k in ("input_price", "output_price")) else math.inf
    routine = min(selected, key=cost)
    others = [r for r in selected if r["id"] != routine["id"]]
    def strength(row):
        verified = row.get("capability_verified") is True and price(row.get("capability")) and row["capability"] <= 1
        return (verified, row.get("capability", 0) if verified else 0, row.get("reasoning") is True)
    specialist = max(others, key=strength)
    evidence = any(r.get("capability_verified") is True for r in selected)
    return {"version": 1, "mode": mode, "created_at": now, "models": selected,
            "routine": routine["id"], "specialist": specialist["id"], "excluded": excluded,
            "allowance_assertions": [{"provider": p, "source": "user assertion", "confirmed_at": now}
                                     for p in confirmed_allowances],
            "reasons": ["Routine: lowest known published cost among eligible tool-capable models.",
                        "Specialist: verified capability evidence where supplied; otherwise a provisional choice from declared reasoning support.",
                        "Quota remains unknown when the provider does not report it.",
                        "No provider-side spending cap is claimed."], "capability_evidence": "verified" if evidence else "provisional"}
