#!/usr/bin/env bash
# AutoRouter self-check. Run locally or in CI: bash scripts/check.sh
set -euo pipefail
cd "$(dirname "$0")/.."

echo "==> security: no token in tree"
if grep -rInE "github_pat_[A-Za-z0-9_]{20,}|ghp_[A-Za-z0-9]{30,}|x-access-token:[A-Za-z0-9_]{20,}" . --exclude-dir=.git >/dev/null 2>&1; then
  echo "ERROR: possible access token committed"; exit 1
fi

echo "==> byte-compile scripts"
python3 -m py_compile scripts/*.py eval/*.py eval/adapters/*.py

echo "==> unit tests (gate calibration + docs claims)"
python3 -m unittest discover -s tests -p "test_*.py"

echo "==> eval harness self-test (synthetic; proves the plumbing, not the router)"
python3 eval/run_eval.py --self-test

echo "==> gate calibration (under-routing and over-routing reported separately)"
python3 scripts/calibrate_gate.py --split test --max-under 0.05 --max-over 0.20 --json \
  | python3 -c "
import json, sys
d = json.load(sys.stdin)
assert not d['failures'], d['failures']
assert d['risk_missed'] == 0, d['risk_missed_cases']
print(f\"   n={d['n']} under={d['under_rate']:.1%} over={d['over_rate']:.1%} \"
      f\"risk_kept={d['risk_total'] - d['risk_missed']}/{d['risk_total']}\")
"

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
tiers = {r["tier"] for r in rows if r["tier"]}
assert {"T2", "T5"} <= tiers, tiers
assert any(r["capability"] is not None for r in rows)

# Provenance: every metric that contributed to a score carries a full record.
required = {"value", "source", "source_kind", "benchmark_version", "collected_on", "conditions"}
for r in rows:
    for metric, ev in r["evidence"].items():
        missing = required - set(ev)
        assert not missing, (r["key"], metric, missing)
        assert ev["source_kind"] in {"independent", "aggregate", "lab_card"}, ev
    assert 0.0 <= r["coverage"] <= 1.0, r
    assert r["metrics_used"] or r["tier_status"] == "insufficient evidence", r["key"]
    assert "blended_cost" not in r, r["key"]

# Sparse models must not receive a tier.
sparse = {r["model"]: r for r in rows if r["coverage"] < 0.5}
assert sparse, "expected at least one sparse model in the example data"
for name, r in sparse.items():
    assert r["tier"] is None, (name, r["tier"])
    assert r["tier_status"] == "insufficient evidence", (name, r["tier_status"])
data = json.load(open("scored.json"))
summary = data["provenance_summary"]
assert summary["observations"] == 97, summary
assert summary["verified"] == 0, "the example dataset is illustrative and must not claim verification"
assert summary["unverified"] == 97, summary
assert data["cost_model"]["cache_prices_estimated"] is True, "cache prices are derived estimates"
for r in rows:
    for metric, ev in r["evidence"].items():
        assert ev["provenance_status"] in {"verified", "unverified", "estimated"}, (r["key"], metric)
        if ev["provenance_status"] == "verified":
            assert ev["source_url"].startswith("http") and ev["archive_url"].startswith("http"), ev
    assert r["verified_coverage"] == 0.0, r["key"]

print("   tiers:", sorted(tiers), "| sparse:", sorted(sparse))
print(f"   provenance: {summary['verified']}/{summary['observations']} verified")
PY

echo "==> provenance: verified needs a source URL and an archived snapshot"
python3 - <<'PY'
import json, subprocess, sys
def run(bench):
    return subprocess.run(
        [sys.executable, "scripts/score_models.py", "--benchmarks", bench,
         "--quota", "tests/fixtures/verified-quota.csv", "--json"],
        capture_output=True, text=True)

ok = run("tests/fixtures/verified-benchmarks.csv")
assert ok.returncode == 0, ok.stderr
d = json.loads(ok.stdout)
assert d["provenance_summary"]["verified"] == 2, d["provenance_summary"]
assert d["models"][0]["verified_coverage"] > 0.5, d["models"][0]["verified_coverage"]

missing_archive = run("tests/fixtures/verified-missing-archive-benchmarks.csv")
assert missing_archive.returncode != 0, "verified without archive_url must be rejected"
assert "archive_url" in missing_archive.stderr, missing_archive.stderr

no_derivation = run("tests/fixtures/estimated-no-derivation-benchmarks.csv")
assert no_derivation.returncode != 0, "estimated without derivation must be rejected"
assert "derivation" in no_derivation.stderr, no_derivation.stderr
print("   ok (URL + snapshot required; estimates require a derivation)")
PY

echo "==> provenance: --require-verified withholds tiers when nothing is verified"
python3 scripts/score_models.py --benchmarks data/benchmarks.example.csv \
  --quota data/quota.example.csv --require-verified --json 2>/dev/null \
  | python3 -c "
import json, sys
d = json.load(sys.stdin)
assert all(r['tier'] is None for r in d['models']), 'unverified data must not yield tiers'
assert {r['tier_status'] for r in d['models']} == {'insufficient verified evidence'}
print('   ok (0 tiers from unverified evidence)')
"

echo "==> cost model: explicit token mix + cache, not blended input+output"
python3 - <<'PY'
import json, subprocess
def score(hit):
    out = subprocess.run(
        ["python3", "scripts/score_models.py",
         "--benchmarks", "data/benchmarks.example.csv",
         "--quota", "data/quota.example.csv",
         "--cache-hit-rate", str(hit), "--json"],
        capture_output=True, text=True, check=True).stdout
    return {r["model"]: r for r in json.loads(out)["models"]}
cheap, cached = score(0.0), score(0.9)
m = "deepseek-v4.1-flash"
assert cheap[m]["est_cost_per_request"] > cached[m]["est_cost_per_request"], m
assert cheap[m]["est_cost_per_request"] > 0
assert "blended_cost" not in cheap[m]
print(f"   {m}: ${cheap[m]['est_cost_per_request']:.5f} (0% cache) -> "
      f"${cached[m]['est_cost_per_request']:.5f} (90% cache)")
PY

echo "==> provenance: source conflicts are recorded, not averaged"
python3 - <<'PY'
import json, subprocess
out = subprocess.run(
    ["python3", "scripts/score_models.py",
     "--benchmarks", "tests/fixtures/conflict-benchmarks.csv",
     "--quota", "tests/fixtures/conflict-quota.csv", "--json"],
    capture_output=True, text=True, check=True).stdout
row = json.loads(out)["models"][0]
assert row["evidence"]["deepswe"]["value"] == 70.0, row["evidence"]["deepswe"]
assert row["conflicts"], "conflicting deepswe readings must be recorded"
assert row["conflicts"][0]["metric"] == "deepswe"
assert len(row["conflicts"][0]["reported"]) == 2
print("   retained independent reading, recorded conflict")
PY

echo "==> provenance: rows without provenance are rejected"
if python3 scripts/score_models.py \
  --benchmarks tests/fixtures/missing-provenance-benchmarks.csv \
  --quota tests/fixtures/missing-provenance-quota.csv >/dev/null 2>&1; then
  echo "ERROR: score_models.py accepted an observation with no source"; exit 1
fi
echo "   ok (rejected)"

echo "==> schema: scored.json (optional jsonschema)"
if python3 -c "import jsonschema" 2>/dev/null; then
  python3 - <<'PY'
import json, jsonschema
schema = json.load(open("schema/scored.schema.json"))
data = json.load(open("scored.json"))
jsonschema.validate(data, schema)
print("   ok")
PY
else
  echo "   skipped (jsonschema not installed)"
fi

echo "==> build router (opencode + generic)"
python3 scripts/build_router.py --scored scored.json --harness opencode --out my-router.json
python3 scripts/build_router.py --scored scored.json --harness generic --out generic.json

echo "==> validate configs"
python3 scripts/validate_router.py --config examples/generic/router.config.json
python3 scripts/validate_router.py --config examples/opencode/opencode.jsonc --catalog data/catalog.example.json
python3 scripts/validate_router.py --config my-router.json
python3 scripts/validate_router.py --config generic.json

echo "==> validator: plugin order does not hide the model table"
python3 scripts/validate_router.py --config tests/fixtures/plugin-order-router-first.json | tail -1 \
  | grep -q "^OK$"

echo "==> validator regression: string-form variants"
out=$(python3 scripts/validate_router.py --config tests/fixtures/variant-string.json --catalog data/catalog.example.json 2>&1 || true)
if printf '%s' "$out" | grep -q "Traceback"; then
  echo "ERROR: validator crashed on string-form variants"; exit 1
fi
echo "   ok (no crash)"

if command -v node >/dev/null 2>&1; then
  echo "==> node: auto-model-reset behaviour (manual models are never reset)"
  node --test examples/opencode/plugins/auto-model-reset/index.test.js

  echo "==> node: gate-router (deterministic tier mapping, ledger, inheritance)"
  node --test examples/opencode/plugins/gate-router/index.test.js

  echo "==> gate-router: policy against the real classify.py"
  node --input-type=module -e "
import { execFileSync } from 'node:child_process'
import { tierFor } from './examples/opencode/plugins/gate-router/index.js'
const cases = [
  ['rename foo to bar', 'T2'],
  ['fix the SQL injection in repo.py', 'T4'],
  ['the app deadlocks under load; the async pipeline refactor introduced it', 'T3'],
  ['add config validation to all three modules', 'T3'],
]
for (const [prompt, want] of cases) {
  const out = execFileSync('python3', ['scripts/classify.py', '--json', '--', prompt], { encoding: 'utf8' })
  const got = tierFor(JSON.parse(out))
  if (got !== want) {
    console.error('MISMATCH:', JSON.stringify(prompt), 'got', got, 'want', want)
    process.exit(1)
  }
}
console.log('   ok (4 real gate decisions map to the expected tiers)')
"

  echo "==> node syntax check (example plugins)"
  node --input-type=module -e "await import('./examples/opencode/plugins/auto-model-reset/index.js')" >/dev/null
  node --input-type=module -e "await import('./examples/opencode/plugins/gate-router/index.js')" >/dev/null
fi

echo "==> install script: syntax, fresh install, idempotency"
bash -n scripts/install_opencode.sh
install_tmp=$(mktemp -d -t autorouter-install-XXXXXX)
bash scripts/install_opencode.sh --config-dir "$install_tmp" >/dev/null
test -f "$install_tmp/opencode.jsonc"
test -f "$install_tmp/agents/eval.md"
test -f "$install_tmp/plugins/auto-model-reset/index.js"
test -f "$install_tmp/plugins/gate-router/index.js"
grep -q '"package": "./plugins/gate-router"' "$install_tmp/opencode.jsonc"
grep -q "gateArgs" "$install_tmp/opencode.jsonc"
if grep -q "__AUTOROUTER_" "$install_tmp/opencode.jsonc"; then
  echo "ERROR: installer left an unresolved placeholder in opencode.jsonc"; exit 1
fi
bash scripts/install_opencode.sh --config-dir "$install_tmp" | grep -q "Already up to date"
rm -rf "$install_tmp"
echo "   ok (installs, re-runs without changes)"

echo "==> eval adapter: fails closed without env, refuses an empty prompt"
if python3 eval/adapters/opencode-adapter.py </dev/null >/dev/null 2>&1; then
  echo "ERROR: adapter must fail when AUTOROUTER_MODEL/AUTOROUTER_WORKDIR are unset"; exit 1
fi
if AUTOROUTER_MODEL=stub/stub AUTOROUTER_WORKDIR=/tmp python3 eval/adapters/opencode-adapter.py </dev/null >/dev/null 2>&1; then
  echo "ERROR: adapter must fail on an empty prompt"; exit 1
fi
echo "   ok (rejects missing env and empty prompt; no model call made)"

echo "==> eval adapter: a transient 429 is retried, billed and reported"
fake_dir=$(mktemp -d -t autorouter-fake-XXXXXX)
cat > "$fake_dir/fake-opencode" <<'FAKE'
#!/usr/bin/env bash
count_file="$(dirname "$0")/count"
n=$(cat "$count_file" 2>/dev/null || echo 0); n=$((n+1)); echo "$n" > "$count_file"
if [ "$n" -lt 3 ]; then
  printf '%s\n' '{"type":"error","error":{"type":"provider.rate-limit","message":"[overloaded] server is overloaded","status":429}}'
  exit 1
fi
printf '%s\n' '{"type":"step_finish","part":{"tokens":{"input":100,"output":20}}}'
FAKE
chmod +x "$fake_dir/fake-opencode"
result=$(printf 'x\n' | AUTOROUTER_MODEL=stub/stub AUTOROUTER_WORKDIR=/tmp \
  AUTOROUTER_OPENCODE_BIN="$fake_dir/fake-opencode" \
  python3 "$PWD/eval/adapters/opencode-adapter.py" 2>/dev/null) || {
  echo "ERROR: adapter did not recover from a transient fault"; rm -rf "$fake_dir"; exit 1; }
printf '%s' "$result" | python3 -c "
import json, sys
d = json.load(sys.stdin)
assert d['tokens_in'] == 100 and d['tokens_out'] == 20, d
assert d['retries'] == 2, d
print('   ok (2 retries reported, tokens from the successful attempt)')
"
rm -rf "$fake_dir"

echo "OK: all checks passed"
