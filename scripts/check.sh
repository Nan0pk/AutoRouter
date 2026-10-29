#!/usr/bin/env bash
# AutoRouter self-check. Run locally or in CI: bash scripts/check.sh
set -euo pipefail
cd "$(dirname "$0")/.."

echo "==> security: no token in tree"
if grep -rInE "github_pat_[A-Za-z0-9_]{20,}|ghp_[A-Za-z0-9]{30,}|x-access-token:[A-Za-z0-9_]{20,}" . --exclude-dir=.git >/dev/null 2>&1; then
  echo "ERROR: possible access token committed"; exit 1
fi

echo "==> byte-compile scripts"
python3 -m py_compile scripts/*.py

echo "==> complexity gate assertions"
python3 scripts/classify.py --json "rename foo to bar" \
  | python3 -c "import json,sys; d=json.load(sys.stdin); assert d['route']=='main' and not d['delegate'], d"
python3 scripts/classify.py --json "add pagination to the users endpoint" \
  | python3 -c "import json,sys; d=json.load(sys.stdin); assert d['route']=='hierarchy' and not d['delegate'], d"
python3 scripts/classify.py --json "why does this deadlock under load when we refactor the async pipeline" \
  | python3 -c "import json,sys; d=json.load(sys.stdin); assert d['route']=='hierarchy' and d['delegate'], d"

echo "==> score models"
python3 scripts/score_models.py \
  --benchmarks data/benchmarks.example.csv \
  --quota data/quota.example.csv \
  --out scored.json >/dev/null
python3 - <<'PY'
import json
rows = json.load(open("scored.json"))["models"]
tiers = {r["tier"] for r in rows}
assert {"T2", "T5"} <= tiers, tiers
assert any(r["capability"] is not None for r in rows)
print("   tiers:", sorted(tiers))
PY

echo "==> build router (opencode + generic)"
python3 scripts/build_router.py --scored scored.json --harness opencode --out my-router.json
python3 scripts/build_router.py --scored scored.json --harness generic --out generic.json

echo "==> validate configs"
python3 scripts/validate_router.py --config examples/generic/router.config.json
python3 scripts/validate_router.py --config examples/opencode/opencode.jsonc --catalog data/catalog.example.json
python3 scripts/validate_router.py --config my-router.json
python3 scripts/validate_router.py --config generic.json

echo "==> validator regression: string-form variants"
out=$(python3 scripts/validate_router.py --config tests/fixtures/variant-string.json --catalog data/catalog.example.json 2>&1 || true)
if printf '%s' "$out" | grep -q "Traceback"; then
  echo "ERROR: validator crashed on string-form variants"; exit 1
fi
echo "   ok (no crash)"

if command -v node >/dev/null 2>&1; then
  echo "==> node syntax check (example plugins)"
  node --input-type=module -e "await import('./examples/opencode/plugins/auto-model-reset/index.js')" >/dev/null
fi

echo "OK: all checks passed"
