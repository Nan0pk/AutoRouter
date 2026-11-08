"""Regressions found while running the checkpoint pilot against OpenCode v2."""
import importlib.util
import io
import json
import os
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("opencode_adapter", ROOT / "eval/adapters/opencode_acp.py")
adapter = importlib.util.module_from_spec(spec)
spec.loader.exec_module(adapter)


class RetryAccounting(unittest.TestCase):
    def test_current_retry_notifications_count_scheduled_attempts(self):
        def update(value):
            return {"sessionUpdate": "session_info_update", "_meta": {"opencode/retry": value}}

        first = {"attempt": 1, "nextRetryAt": "2026-11-08T00:00:00Z",
                 "error": {"type": "provider.rate-limit", "message": "Rate limited", "status": 429}}
        second = {**first, "attempt": 2}
        for updates, expected in (
            ([update(first)], 1),
            ([update(first), update(first), update(None)], 1),
            ([update(first), update(second)], 2),
            ([update(first), update(None), update(first)], 2),
            ([{"sessionUpdate": "session_info_update", "title": "Unrelated"}], 0),
            ([{"sessionUpdate": "retry"}], 1),
        ):
            with self.subTest(updates=updates):
                messages = [{"jsonrpc": "2.0", "method": "session/update",
                             "params": {"sessionId": "probe", "update": u}} for u in updates]
                messages.append({"jsonrpc": "2.0", "id": 1, "result": {"stopReason": "end_turn"}})
                process = types.SimpleNamespace(
                    stdin=io.StringIO(),
                    stdout=io.StringIO("".join(json.dumps(m) + "\n" for m in messages)),
                )
                client = adapter.ACP(process)
                client.call("session/prompt", {"sessionId": "probe", "prompt": []})
                self.assertEqual(client.retries, expected)


class CredentialImport(unittest.TestCase):
    def test_export_is_imported_into_disposable_home(self):
        self.assertTrue(callable(getattr(adapter, "import_auth", None)), "v2 credential import is missing")
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            export = root / "selected.json"
            selected = [{"integrationID": "opencode", "value": {"type": "api", "key": "test-only"}}]
            export.write_text(json.dumps(selected))
            home = root / "home"
            home.mkdir()
            binary = root / "opencode"
            binary.write_text(
                "#!/usr/bin/python3\nimport json, os, pathlib, sys\n"
                "assert sys.argv[1:] == ['auth', 'import', '--standalone']\n"
                "data = json.load(sys.stdin)\n"
                "pathlib.Path(os.environ['HOME'], 'imported.json').write_text(json.dumps(data))\n"
            )
            binary.chmod(0o700)
            with patch.dict(os.environ, {"AUTOROUTER_OPENCODE_AUTH_EXPORT": str(export)}, clear=True):
                adapter.import_auth(str(binary), {"HOME": str(home)})
            self.assertEqual(json.loads((home / "imported.json").read_text()), selected)
            self.assertEqual(json.loads(export.read_text()), selected)

    def test_import_failure_does_not_expose_credentials(self):
        self.assertTrue(callable(getattr(adapter, "import_auth", None)), "v2 credential import is missing")
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            export = root / "selected.json"
            export.write_text("[]")
            binary = root / "opencode"
            binary.write_text("#!/bin/sh\necho credential-must-not-leak >&2\nexit 1\n")
            binary.chmod(0o700)
            with patch.dict(os.environ, {"AUTOROUTER_OPENCODE_AUTH_EXPORT": str(export)}, clear=True):
                with self.assertRaisesRegex(RuntimeError, "credential import failed") as error:
                    adapter.import_auth(str(binary), {"HOME": str(root)})
            self.assertNotIn("credential-must-not-leak", str(error.exception))

    def test_ambiguous_credential_sources_are_rejected(self):
        self.assertTrue(callable(getattr(adapter, "import_auth", None)), "v2 credential import is missing")
        with patch.dict(os.environ, {"AUTOROUTER_OPENCODE_AUTH": "/legacy",
                                    "AUTOROUTER_OPENCODE_AUTH_EXPORT": "/export"}, clear=True):
            with self.assertRaisesRegex(RuntimeError, "one credential source"):
                adapter.import_auth("unused", {})


class WorkerPermissions(unittest.TestCase):
    def test_selected_provider_is_allowed_without_enabling_other_tools(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            workdir = root / "work"
            workdir.mkdir()
            with patch.dict(os.environ, {"AUTOROUTER_MODEL": "opencode-go/deepseek-v4.1-flash"}, clear=True):
                adapter.configure(root / "home", workdir)
            rules = json.loads((root / "home/.config/opencode/opencode.json").read_text())["permissions"]
            self.assertEqual(rules[0], {"action": "*", "resource": "*", "effect": "deny"})
            allowed = {(r["action"], r["resource"]) for r in rules if r["effect"] == "allow"}
            self.assertEqual(allowed, {("read", "*"), ("edit", "*"), ("glob", "*"),
                                       ("grep", "*"), ("provider.use", "opencode-go")})


if __name__ == "__main__":
    unittest.main()
