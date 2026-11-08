"""Verify the selected credential and checkpoint round trip with fake CLIs."""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "eval"))
sys.path.insert(0, str(ROOT / "eval/adapters"))

import hermes_go  # noqa: E402
import isolation  # noqa: E402
import run_hermes_handoff  # noqa: E402

FAKE_EXPORT = '''
import json, os, sys
assert sys.argv[1:] == ["auth", "export", "opencode", "--standalone"]
print(json.dumps([
    {"integrationID": "opencode", "active": False, "id": "old",
     "value": {"type": "key", "key": "inactive-key"}},
    {"integrationID": "opencode", "active": True,
     "id": os.environ.get("PILOT_TEST_CREDENTIAL", "active"),
     "value": {"type": "key", "key": "dummy-go-key"}},
]))
'''

FAKE_HERMES = '''
import json, os, sys
from pathlib import Path

if sys.argv[1:] == ["--version"]:
    assert "OPENCODE_GO_API_KEY" not in os.environ
    print("Hermes Agent v0.23.0")
    raise SystemExit()

args = dict(zip(sys.argv[1::2], sys.argv[2::2]))
home = Path(os.environ["HERMES_HOME"])
assert os.environ["OPENCODE_GO_API_KEY"] == "dummy-go-key"
assert os.environ["HERMES_WRITE_SAFE_ROOT"] == str(Path.cwd()) == "/work"
assert "AUTOROUTER_OPENCODE_AUTH_EXPORT" not in os.environ
assert args["--provider"] == "opencode-go"
assert args["--toolsets"] == "file"
assert home == Path.home() and not (home / ".env").exists()
assert json.loads((home / "config.yaml").read_text())["fallback_models"] == []

prompt, model = args["-z"], args["--model"]
summary = Path("summary.py")
aggregate = Path("aggregate.py")
if prompt.startswith("Draft"):
    summary.write_text("from aggregate import total_by_key\\n\\ndef summarize(rows):\\n"
                       "    return list(total_by_key(rows).items())\\n")
elif model == "claude-haiku-5-5":
    assert "RuntimeError" in prompt and "return list" in summary.read_text()
    aggregate.write_text(
        "def total_by_key(rows):\\n"
        "    totals = {}\\n"
        "    for row in rows:\\n"
        "        k = row['k']\\n"
        "        totals[k] = totals.get(k, 0) + row['v']\\n"
        "    return {k: v for k, v in totals.items() if v}\\n"
    )
elif prompt.startswith("The aggregator passed"):
    summary.write_text("from aggregate import total_by_key\\n\\ndef summarize(rows):\\n"
                       "    return sorted(total_by_key(rows).items())\\n")
Path(args["--usage-file"]).write_text(json.dumps({
    "completed": True, "failed": False, "partial": False, "interrupted": False,
    "model": model, "provider": "opencode-go", "input_tokens": 20, "output_tokens": 5,
    "auxiliary": {"input_tokens": 2, "output_tokens": 1},
}))
'''


class CredentialContract(unittest.TestCase):
    def test_oauth_and_other_integrations_are_rejected(self):
        row = {"integrationID": "opencode", "active": True, "id": "account",
               "value": {"type": "oauth", "access": "dummy-oauth-token"}}
        with self.assertRaisesRegex(ValueError, "must be an API key"):
            run_hermes_handoff.active_key([row], "opencode")
        with self.assertRaisesRegex(ValueError, "integration"):
            run_hermes_handoff.active_key([row], "opencode-go")

    def test_multiple_active_accounts_are_rejected(self):
        row = {"integrationID": "opencode", "active": True, "id": "one",
               "value": {"type": "key", "key": "dummy-key"}}
        with self.assertRaisesRegex(ValueError, "exactly one"):
            run_hermes_handoff.active_key([row, {**row, "id": "two"}], "opencode")

    def test_hermes_provider_fallback_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            usage = Path(directory) / "usage.json"
            usage.write_text(json.dumps({"completed": True, "model": "x", "provider": "opencode-zen"}))
            with self.assertRaisesRegex(RuntimeError, "pinned OpenCode Go model"):
                hermes_go.read_usage(usage, "x")


@unittest.skipUnless(isolation.available() and isolation.available(network=True),
                     "both Bubblewrap modes are required")
class HermesRoundTrip(unittest.TestCase):
    def test_migrated_key_and_handoff_resume(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            opencode, hermes = root / "opencode", root / "hermes"
            for path, source in ((opencode, FAKE_EXPORT), (hermes, FAKE_HERMES)):
                path.write_text("#!/usr/bin/python3\n" + source)
                path.chmod(0o700)
            run = root / "pilot"
            cmd = [sys.executable, str(ROOT / "eval/run_hermes_handoff.py"),
                   "--opencode", str(opencode), "--hermes", str(hermes),
                   "--", "--run-dir", str(run)]
            first = subprocess.run(cmd + ["--steps", "2"], capture_output=True, text=True, timeout=30)
            self.assertEqual(first.returncode, 0, first.stderr)
            self.assertEqual(json.loads(first.stdout)["phase"], "specialist")
            different_account = subprocess.run(
                cmd + ["--resume"], env={**os.environ, "PILOT_TEST_CREDENTIAL": "other-account"},
                capture_output=True, text=True, timeout=30,
            )
            self.assertNotEqual(different_account.returncode, 0)
            self.assertIn("profile changed", different_account.stderr)
            second = subprocess.run(cmd + ["--resume"], capture_output=True, text=True, timeout=30)
            self.assertEqual(second.returncode, 0, second.stderr)
            state = (run / "state.json").read_text()
            self.assertNotIn("dummy-go-key", state + first.stdout + second.stdout)
            saved = json.loads(state)
            self.assertEqual(saved["phase"], "completed", saved)
            self.assertEqual([a["phase"] for a in saved["attempts"]],
                             ["draft", "repair", "specialist", "finish"])
            self.assertEqual([a["execution"]["tokens_in"] for a in saved["attempts"]], [22] * 4)
