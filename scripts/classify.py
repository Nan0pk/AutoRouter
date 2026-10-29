#!/usr/bin/env python3
"""Complexity gate.

Decides whether a prompt needs the full routing hierarchy or can stay on the
current/default model, and whether mid-chat delegation is worth it.

Usage:
    python3 scripts/classify.py "rename this variable"
    echo "redesign the storage layer" | python3 scripts/classify.py --stdin
    python3 scripts/classify.py --json "why does this deadlock under load?"

Output classes:
    trivial   -> route "main"       (no hierarchy, no decision call)
    standard  -> route "hierarchy"  (cheapest capable tier, no delegation)
    complex   -> route "hierarchy"  (mid/high tier, delegation allowed)
"""
import argparse
import json
import re
import sys

# phrase -> weight added to the complexity score
COMPLEX = {
    "architecture": 3, "architect": 3, "redesign": 3, "system design": 3,
    "migrate": 2, "migration": 2, "refactor": 2, "multi-file": 2, "across files": 2,
    "distributed": 2, "race condition": 3, "deadlock": 3, "concurrency": 2,
    "thread": 1, "async": 1, "debug": 2, "root cause": 2, "why does": 2,
    "performance": 2, "optimi": 1, "security": 2, "vulnerab": 2, "scalab": 1,
    "end-to-end": 2, "pipeline": 1, "integrate": 1, "design": 1,
}
SIMPLE = {
    "rename": 3, "typo": 3, "format": 2, "formatting": 2, "one-liner": 3,
    "what is": 2, "explain": 1, "lookup": 2, "hello": 3, "hi": 3, "thanks": 3,
    "print": 1, "list": 1, "show me": 1, "convert": 1, "replace": 2,
}


def _hit(kw, text):
    """Match a keyword at a word boundary, allowing suffixes (debug -> debugging)."""
    return re.search(r"\b" + re.escape(kw) + r"\w*", text) is not None


def classify(text, main_floor=-2, complex_threshold=5):
    t = text.strip().lower()
    words = re.findall(r"[a-z0-9_]+", t)
    n = len(words)
    score = 0
    reasons = []

    if n <= 3:
        score -= 3
        reasons.append("very-short")
    elif n <= 8:
        score -= 1
        reasons.append("short")
    elif n >= 60:
        score += 2
        reasons.append("long")
    elif n >= 25:
        score += 1
        reasons.append("medium-long")

    for kw, w in COMPLEX.items():
        if _hit(kw, t):
            score += w
            reasons.append(f"+{kw}({w})")
    for kw, w in SIMPLE.items():
        if _hit(kw, t):
            score -= w
            reasons.append(f"-{kw}({w})")

    if text.count("```") >= 2:
        score += 1
        reasons.append("code-block")
    if len(re.findall(r"^\s*[-*\d]+[.)]?\s", text, re.M)) >= 3:
        score += 1
        reasons.append("list")
    if re.search(r"\b(first|then|next|finally|after that|step \d)\b", t):
        score += 1
        reasons.append("multi-step")

    if score <= main_floor:
        cls, route, tier, delegate = "trivial", "main", None, False
    elif score <= 1:
        cls, route, tier, delegate = "standard", "hierarchy", "T1", False
    elif score < complex_threshold:
        cls, route, tier, delegate = "standard", "hierarchy", "T2", False
    else:
        cls, route, tier, delegate = "complex", "hierarchy", "T3+", True

    return {
        "class": cls,
        "route": route,
        "tier_hint": tier,
        "delegate": delegate,
        "score": score,
        "reasons": reasons,
    }


def main():
    ap = argparse.ArgumentParser(description="Complexity gate for auto-routing.")
    ap.add_argument("prompt", nargs="*", help="prompt text (or use --stdin)")
    ap.add_argument("--stdin", action="store_true", help="read prompt from stdin")
    ap.add_argument("--json", action="store_true", help="print JSON only")
    ap.add_argument("--main-floor", type=int, default=-2)
    ap.add_argument("--complex-threshold", type=int, default=5)
    args = ap.parse_args()

    if args.stdin or not args.prompt:
        text = sys.stdin.read()
    else:
        text = " ".join(args.prompt)

    result = classify(text, args.main_floor, args.complex_threshold)
    if args.json:
        print(json.dumps(result, indent=2))
    else:
        print(
            f"class={result['class']} route={result['route']} "
            f"tier_hint={result['tier_hint']} delegate={result['delegate']} "
            f"score={result['score']}"
        )
        if result["reasons"]:
            print("reasons:", ", ".join(result["reasons"]))
        print("json:", json.dumps(result))


if __name__ == "__main__":
    main()
