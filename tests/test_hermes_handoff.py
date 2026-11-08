"""Verify the selected credential and checkpoint round trip with fake CLIs."""
import json
import hashlib
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "eval"))
sys.path.insert(0, str(ROOT / "eval/adapters"))

import hermes_go  # noqa: E402
import isolation  # noqa: E402
import run_hermes_handoff  # noqa: E402

FAKE_EXPORT = '''
import json, os, sys, time
assert sys.argv[1:] == ["auth", "export", "opencode", "--standalone"]
value = {"type": "key", "key": "dummy-go-key"}
if os.environ.get("PILOT_TEST_OAUTH"):
    value = {"type": "oauth", "methodID": "device", "access": "dummy-access",
             "refresh": "dummy-refresh", "expires": int(time.time() * 1000) + 60000,
             "metadata": {"server": "https://opencode.ai/console",
                          "orgID": os.environ.get("PILOT_TEST_ORG", "wrk_test")}}
print(json.dumps([
    {"integrationID": "opencode", "active": False, "id": "old",
     "value": {"type": "key", "key": "inactive-key"}},
    {"integrationID": "opencode", "active": True,
     "id": os.environ.get("PILOT_TEST_CREDENTIAL", "active"),
     "value": value},
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
assert os.environ["OPENCODE_GO_API_KEY"] in ("dummy-go-key", "dummy-access")
assert "dummy-refresh" not in os.environ.values()
assert os.environ["HERMES_WRITE_SAFE_ROOT"] == str(Path.cwd()) == "/work"
assert "AUTOROUTER_OPENCODE_AUTH_EXPORT" not in os.environ
assert args["--provider"] == "opencode-go"
assert args["--toolsets"] == "file"
assert home == Path.home() and not (home / ".env").exists()
config = json.loads((home / "config.yaml").read_text())
assert config["fallback_models"] == []
assert config["model"]["base_url"] == "https://opencode.ai/inference/go/openai/v1"
if os.environ["OPENCODE_GO_API_KEY"] == "dummy-access":
    assert config["model"]["default_headers"]["x-opencode-org-id"] == "wrk_test"

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
    def oauth_row(self):
        return {"integrationID": "opencode", "active": True, "id": "account",
                "value": {"type": "oauth", "methodID": "device", "access": "dummy-access",
                          "refresh": "dummy-refresh", "expires": int(time.time() * 1000) + 60000,
                          "metadata": {"server": "https://opencode.ai/console", "orgID": "wrk_test"}}}

    def test_current_console_oauth_selects_only_access_and_workspace(self):
        row = self.oauth_row()
        self.assertEqual(run_hermes_handoff.active_credential([row], "opencode"),
                         ("dummy-access", "account", "wrk_test"))

    def test_expired_or_foreign_oauth_is_rejected(self):
        for change in ({"expires": 0}, {"metadata": {"server": "https://example.com", "orgID": "wrk_test"}},
                       {"metadata": {"server": "https://opencode.ai/console"}},
                       {"metadata": {"server": "https://opencode.ai/console", "orgID": "bad\nheader"}}):
            row = self.oauth_row()
            row["value"].update(change)
            with self.subTest(change=change), self.assertRaises(ValueError):
                run_hermes_handoff.active_credential([row], "opencode")

    def test_incomplete_oauth_and_other_integrations_are_rejected(self):
        row = {"integrationID": "opencode", "active": True, "id": "account",
               "value": {"type": "oauth", "access": "dummy-oauth-token"}}
        with self.assertRaises(ValueError):
            run_hermes_handoff.active_credential([row], "opencode")
        with self.assertRaisesRegex(ValueError, "integration"):
            run_hermes_handoff.active_credential([row], "opencode-go")

    def test_multiple_active_accounts_are_rejected(self):
        row = {"integrationID": "opencode", "active": True, "id": "one",
               "value": {"type": "key", "key": "dummy-key"}}
        with self.assertRaisesRegex(ValueError, "exactly one"):
            run_hermes_handoff.active_credential([row, {**row, "id": "two"}], "opencode")

    def test_hermes_provider_fallback_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            usage = Path(directory) / "usage.json"
            usage.write_text(json.dumps({"completed": True, "model": "x", "provider": "opencode-zen"}))
            with self.assertRaisesRegex(RuntimeError, "pinned OpenCode Go model"):
                hermes_go.read_usage(usage, "x")


class ManagedRuntime(unittest.TestCase):
    def test_selected_venv_is_mounted_without_the_profile(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            source = home / "hermes-agent"
            source.mkdir()
            state = home / "installs" / hashlib.sha256(str(source).encode()).hexdigest()[:16]
            venv = state / "environments/generation/venv"
            (venv / "bin").mkdir(parents=True)
            (venv / "pyvenv.cfg").touch()
            (venv / "bin/python").touch()
            (state / "facts.json").write_text(json.dumps({"packages": {"venv": {"environment": str(venv)}}}))
            binary = source / ".hermes/bin/hermes"
            with patch.dict(os.environ, {"HERMES_HOME": str(home)}):
                runtime = run_hermes_handoff.managed_runtime(binary)
                self.assertEqual(runtime, (source, venv))
                mounts = run_hermes_handoff.runtime_mounts(binary, [], runtime)
                self.assertIn(str(venv), mounts)
                self.assertNotIn(str(home), mounts)
                self.assertNotIn(str(state), mounts)
                with self.assertRaises(ValueError):
                    run_hermes_handoff.runtime_mounts(binary, [home], runtime)
                (state / "facts.json").write_text(json.dumps({"packages": {"venv": {"environment": str(home)}}}))
                with self.assertRaises(ValueError):
                    run_hermes_handoff.managed_runtime(binary)


@unittest.skipUnless(isolation.available() and isolation.available(network=True),
                     "both Bubblewrap modes are required")
class HermesRoundTrip(unittest.TestCase):
    def test_oauth_handoff_resume(self):
        from unittest.mock import patch
        with patch.dict(os.environ, {"PILOT_TEST_OAUTH": "1"}):
            self.test_migrated_key_and_handoff_resume()

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
            if os.environ.get("PILOT_TEST_OAUTH"):
                changed_workspace = subprocess.run(
                    cmd + ["--resume"], env={**os.environ, "PILOT_TEST_ORG": "wrk_other"},
                    capture_output=True, text=True, timeout=30,
                )
                self.assertNotEqual(changed_workspace.returncode, 0)
                self.assertIn("profile changed", changed_workspace.stderr)
            second = subprocess.run(cmd + ["--resume"], capture_output=True, text=True, timeout=30)
            self.assertEqual(second.returncode, 0, second.stderr)
            state = (run / "state.json").read_text()
            for secret in ("dummy-go-key", "dummy-access", "dummy-refresh"):
                self.assertNotIn(secret, state + first.stdout + second.stdout)
            saved = json.loads(state)
            self.assertEqual(saved["phase"], "completed", saved)
            self.assertEqual([a["phase"] for a in saved["attempts"]],
                             ["draft", "repair", "specialist", "finish"])
            self.assertEqual([a["execution"]["tokens_in"] for a in saved["attempts"]], [22] * 4)
