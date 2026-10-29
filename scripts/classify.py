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

Overrides
---------
Three risk families force a prompt up to `complex` regardless of how short or
mechanical it looks:

    security     auth, injection, secrets, privilege, deserialisation, ...
    data_loss    destructive or irreversible operations on data
    broad_scope  many files, public APIs, repo-wide or structural rewrites

Short high-risk prompts are therefore never penalised for being short: "fix
security vulnerability" is complex work, not a three-word triviality.

Calibration set and thresholds: tests/gate/labeled_prompts.csv +
scripts/calibrate_gate.py.
"""
import argparse
import json
import re
import sys

# regex fragment -> weight added to the complexity score
COMPLEX = {
    r"\barchitect\w*": 3,
    r"\bredesign\w*": 3,
    r"\brestructur\w*": 3,
    r"\bsystem design\b": 3,
    r"\bmigrat\w*": 2,
    r"\brefactor\w*": 2,
    r"\bmulti-file\b": 2,
    r"\bacross (files|modules|services|repositor\w*)\b": 2,
    r"\bdistributed\b": 2,
    r"\brace condition\w*": 4,
    r"\bdeadlock\w*": 4,
    r"\bconcurren\w*": 2,
    r"\bthread\w*": 1,
    r"\basync\w*": 1,
    r"\bdebug\w*": 2,
    r"\broot cause\b": 3,
    r"\bwhy (does|do|is|are|did)\b": 3,
    r"\bregression\b": 2,
    r"\b(intermittent|flaky|non-deterministic)\b": 2,
    r"\bperformance\b": 2,
    r"\boptimi[sz]\w*": 1,
    r"\bsecur\w*": 2,
    r"\bvulnerab\w*": 2,
    r"\bscalab\w*": 1,
    r"\bend[ -]to[ -]end\b": 2,
    r"\bpipeline\w*": 1,
    r"\bintegrat\w*": 1,
    r"\bdesign\w*": 1,
    r"\bunif(y|ied|ication)\b": 2,
    r"\bconsolidat\w*": 2,
    r"\bdeduplicat\w*": 2,
    r"\bhardening\b": 2,
    r"\bidempoten\w*": 2,
}

SIMPLE = {
    r"\brename\w*": 3,
    r"\btypo\w*": 3,
    r"\bhello\b": 3,
    r"\bhi\b": 3,
    r"\bthanks\b": 3,
    r"\bone-liner\b": 3,
    r"\bwhat is\b": 2,
    r"\bprint\b": 1,
    r"\blist\b": 1,
    r"\bshow me\b": 1,
}

# High-stakes families that must never be routed down because of their length.
RISK = {
    "security": [
        r"\bsecur\w*",
        r"\bvulnerab\w*",
        r"\bCVE-\d+",
        r"\bexploit\w*",
        r"\bsql injection\b",
        r"\b(command|code|prompt) injection\b",
        r"\bxss\b",
        r"\bcsrf\b",
        r"\bssrf\b",
        r"\brce\b",
        r"\b(auth|authentication|authori[sz]ation|authn|authz)\b[^.]{0,20}\b(bypass|leak|flaw|bug|broken|missing)\b",
        r"\b(bypass|broken|missing|flawed)\b[^.]{0,20}\b(auth|authentication|authori[sz]ation)\b",
        r"\bprivilege escalation\b",
        r"\b(secret|credential|api key|token)s?\b[^.]{0,20}\b(leak|expos|log|commit)",
        r"\bleak(s|ed|ing)?\b[^.]{0,20}\b(secret|credential|token|key|record)",
        r"\bsaniti[sz]\w*",
        r"\bdeseriali[sz]\w*",
        r"\bpath traversal\b",
        r"\brow-level (security|authorization)\b",
        r"\btenant isolation\b",
        r"\bharden\w*",
        r"\b(stuffing|brute[ -]force)\b",
        r"\b(encrypt|decrypt)\w*",
        r"\bpentest\w*",
    ],
    "data_loss": [
        r"\bdata loss\b",
        r"\blose (the |all )?data\b",
        r"\bdrop\w*\b[^.]{0,20}\b(table|column|database|schema)s?\b",
        r"\btruncat\w*\b[^.]{0,20}\b(table|column|database|schema)s?\b",
        r"\broll[ -]?back\b",
        r"\bdouble[ -]?charg\w*",
        r"\b(payment|billing|invoice|charge)\w*\b[^.]{0,40}\b(double|twice|duplicate|wrong)\b",
        r"\b(deploy\w*|release|migration)\b[^.]{0,30}\b(atomic|reversible|canary|blue[ -]green)\b",
        r"\brm -rf\b",
        r"\bwipe\w*",
        r"\bpurge\w*",
        r"\bdelete\w*\b[^.]{0,40}\b(production|prod|table|rows?|records?|users?|accounts?|data)\b",
        r"\b(production|prod)\b[^.]{0,20}\b(delete|drop|truncate|wipe|purge)\b",
        r"\bcorrupt\w*",
        r"\birreversible\b",
        r"\brollback\b",
        r"\bbackfill\b",
        r"\boverwrite\w*\b[^.]{0,30}\b(data|files?|records?|table)\b",
        r"\bbilling\b[^.]{0,30}\b(double|charg|refund)",
        r"\bmigrat\w*\b[^.]{0,30}\b(database|schema|table|data|storage|production)\b",
        r"\bgdpr\b",
        r"\bblast radius\b",
        r"\bdeletion request\w*",
    ],
    "broad_scope": [
        r"\b\d+ (files?|services?|modules?|call sites?|repos?)\b",
        r"\b(all|every|each|entire|whole)\b[^.]{0,30}\b(files?|call sites?|tests?|suite|repositor\w*|repo|codebase|services?|modules?|packages?)\b",
        r"\b(repo|codebase|system|org|company)-wide\b",
        r"\b(monorepo|monolith)\w*\b",
        r"\bacross the (codebase|repo|repository|project|system)\b",
        r"\bpublic api\b",
        r"\bbreaking (change|api)\b",
        r"\b(redesign|rewrite|re-architect|rearchitect|overhaul|restructure)\w*\b",
        r"\bfrom scratch\b",
        r"\bgreenfield\b",
        r"\bmigrate (the )?(whole|entire|all)\b",
    ],
}

# A structural noun plus a change verb means the blast radius is large even
# when the prompt is short ("plan the architecture for failover").
STRUCTURAL_NOUNS = re.compile(
    r"\b(architecture|schema|data model|storage( layer)?|pipeline|infrastructure|"
    r"platform|strategy|system|layer|module|service|auth(entication)?)\b"
)
CHANGE_VERBS = re.compile(
    r"\b(redesign|restructure|rewrite|migrate|replace|introduce|design|plan|build|extract|"
    r"split|unify|consolidate|port|move|rework|overhaul|unify)\b"
)

# Definitional questions are lookups even when they mention big words
# ("what is a monorepo?").
DEFINITIONAL = re.compile(r"^\s*(what|who|when|where)\s+(is|are|was|were|does|do)\b", re.I)

# A causal/diagnostic question about a concrete failure is complex debugging,
# no matter how few words it uses. "explain this stack trace" is not included:
# reading one stack trace is normal work, chasing a cause is not.
DIAGNOSTIC = re.compile(
    r"(\bwhy (does|do|is|are|did)\b|\bhow come\b|\broot cause\b|"
    r"\bwhat'?s causing\b|\bfind (it|out why|the cause|cause|out what)\b|"
    r"\bwhat('s| is) (this|the) (error|warning|traceback|stack trace|exception)\b|"
    r"\bexplain (the|this) (feedback|failure|outage|incident|cascade|loop)\b|"
    r"\binvestigat\w*\b)",
    re.I,
)
FAILURE_TERMS = re.compile(
    r"\b(error|exception|fail\w*|crash\w*|hang\w*|stuck|lock\w*|timeout\w*|leak\w*|"
    r"oom|killed|50[03]|regression|slow\w*|degrad\w*|corrupt\w*|shift\w*|wrong|"
    r"inconsistent|flaky|intermittent|non-deterministic|race|deadlock\w*|duplicat\w*|"
    r"double[ -]charg\w*|warning|traceback|stack trace|back(ing)? up|backlog|"
    r"takes? down|outage|cascade|feedback loop)\b",
    re.I,
)
DIAGNOSTIC_BONUS = 5

# A numeric capacity target ("50k events per second", "10x traffic") is a
# scaling problem, not a tweak.
CAPACITY_TARGET = re.compile(
    r"(\b\d+\s*(k|m|x|×)?\s*(events?|requests?|reqs?|users?|rps|qps|rows?|"
    r"messages?|transactions?)\b[^.]{0,20}\b(per|/|a)\b\s*(second|sec|minute|hour|day)\b"
    r"|\b\d+\s*(x|×)\s*(traffic|load|volume)\b"
    r"|\b(throughput|p99|qps|rps)\b)",
    re.I,
)
CAPACITY_BONUS = 5

MAIN_FLOOR = -2
COMPLEX_THRESHOLD = 5


def _hits(table, text):
    out = []
    for pattern, weight in table.items():
        if re.search(pattern, text, re.I):
            out.append((pattern, weight))
    return out


def _risk_hits(text):
    return {group: [p for p in patterns if re.search(p, text, re.I)] for group, patterns in RISK.items()}


def classify(text, main_floor=MAIN_FLOOR, complex_threshold=COMPLEX_THRESHOLD):
    t = text.strip().lower()
    words = re.findall(r"[a-z0-9_]+", t)
    n = len(words)
    score = 0
    reasons = []

    length_penalty = 0
    length_reason = None
    if n <= 3:
        length_penalty = -3
        length_reason = "very-short"
    elif n <= 8:
        length_penalty = -1
        length_reason = "short"
    elif n >= 60:
        length_penalty = 2
        length_reason = "long"
    elif n >= 25:
        length_penalty = 1
        length_reason = "medium-long"

    for pattern, weight in _hits(COMPLEX, t):
        score += weight
        reasons.append(f"+{pattern}({weight})")
    for pattern, weight in _hits(SIMPLE, t):
        score -= weight
        reasons.append(f"-{pattern}({weight})")

    if text.count("```") >= 2:
        score += 1
        reasons.append("code-block")
    if len(re.findall(r"^\s*[-*\d]+[.)]?\s", text, re.M)) >= 3:
        score += 1
        reasons.append("list")
    if re.search(r"\b(first|then|next|finally|after that|step \d)\b", t):
        score += 1
        reasons.append("multi-step")

    if STRUCTURAL_NOUNS.search(t) and CHANGE_VERBS.search(t):
        score += 4
        reasons.append("structural-change(4)")

    if DIAGNOSTIC.search(t) and FAILURE_TERMS.search(t):
        score += DIAGNOSTIC_BONUS
        reasons.append(f"diagnostic-question({DIAGNOSTIC_BONUS})")

    if CAPACITY_TARGET.search(t):
        score += CAPACITY_BONUS
        reasons.append(f"capacity-target({CAPACITY_BONUS})")

    risk = _risk_hits(text)
    overrides = [g for g, hits in risk.items() if hits]
    if overrides and DEFINITIONAL.match(t):
        # "what is a monorepo?" is a lookup, not a repo-wide change.
        overrides = [g for g in overrides if g != "broad_scope"]
        if overrides:
            reasons.append("definitional-question: broad-scope override suppressed")

    if overrides:
        # Deliberate: high-stakes prompts are not discounted for being short.
        reasons.append("length-signal-skipped(risk-override)")
        reasons.extend(f"override:{g}" for g in overrides)
        return {
            "class": "complex",
            "route": "hierarchy",
            "tier_hint": "T3+" if {"security", "data_loss"} & set(overrides) else "T2",
            "delegate": True,
            "score": score,
            "risk": overrides,
            "overrides": overrides,
            "reasons": reasons,
        }

    score += length_penalty
    if length_reason:
        reasons.append(length_reason)

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
        "risk": overrides,
        "overrides": [],
        "reasons": reasons,
    }


def main():
    ap = argparse.ArgumentParser(description="Complexity gate for auto-routing.")
    ap.add_argument("prompt", nargs="*", help="prompt text (or use --stdin)")
    ap.add_argument("--stdin", action="store_true", help="read prompt from stdin")
    ap.add_argument("--json", action="store_true", help="print JSON only")
    ap.add_argument("--main-floor", type=int, default=MAIN_FLOOR)
    ap.add_argument("--complex-threshold", type=int, default=COMPLEX_THRESHOLD)
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
        if result["overrides"]:
            print("overrides:", ", ".join(result["overrides"]))
        if result["reasons"]:
            print("reasons:", ", ".join(result["reasons"]))
        print("json:", json.dumps(result))


if __name__ == "__main__":
    main()
