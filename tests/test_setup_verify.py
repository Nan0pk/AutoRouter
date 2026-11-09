import hashlib
import unittest
import tempfile
import json
import subprocess
import types
from pathlib import Path
from unittest.mock import patch

from autorouter.verify import evaluate_handoff, verify_install
from autorouter.install import install, save, fingerprint, status
from test_setup_install import json_renderer
from autorouter.policy import select_models
from test_setup_policy import model


class Verification(unittest.TestCase):
    def setUp(self):
        self.policy = select_models({"models": [model("go", "small"), model("go", "strong", price=2)]}, "subscriptions")
        self.output = "AR_CHECK_test:42"
        self.usage = {"completed": True, "model": "small", "provider": "go", "input_tokens": 100, "output_tokens": 20}
        self.events = [{"event": "api", "session_id": "root", "parent_session_id": "", "model": "small", "response_model": "small", "provider": "go", "usage": {"input_tokens": 10, "output_tokens": 5}},
                       {"event": "api", "session_id": "child", "parent_session_id": "root", "model": "strong", "response_model": "strong", "provider": "go", "usage": {"input_tokens": 20, "output_tokens": 5}},
                       {"event": "child_stop", "parent_session_id": "root", "child_session_id": "child", "child_status": "completed",
                        "result_sha256": hashlib.sha256(self.output.encode()).hexdigest()}]

    def test_two_real_routes_and_returned_child_result_are_required(self):
        result = evaluate_handoff(self.policy, self.events, self.usage, self.output)
        self.assertTrue(result["handoff_verified"])
        self.assertEqual(len(result["observed_models"]), 2)

    def test_virtual_picker_name_requires_the_real_upstream_response_model(self):
        policy = {**self.policy, "picker_model": "AutoRouter-All"}
        usage = {**self.usage, "provider": "autorouter", "model": "AutoRouter-All"}
        events = [{**self.events[0], "provider": "autorouter", "model": "AutoRouter-All"}, *self.events[1:]]
        self.assertTrue(evaluate_handoff(policy, events, usage, self.output)["handoff_verified"])
        events[0]["response_model"] = "AutoRouter-All"
        with self.assertRaises(ValueError):
            evaluate_handoff(policy, events, usage, self.output)

    def test_skipped_child_wrong_model_missing_usage_and_empty_result_fail(self):
        import copy
        cases = []
        events = copy.deepcopy(self.events); events[1]["response_model"] = "wrong"; cases.append((events, self.usage, self.output))
        events = copy.deepcopy(self.events); events[1]["usage"] = {}; cases.append((events, self.usage, self.output))
        events = copy.deepcopy(self.events); events[2]["child_status"] = "skipped"; cases.append((events, self.usage, self.output))
        cases += [(self.events[:1], self.usage, self.output), (self.events, {**self.usage, "completed": False}, self.output),
                  (self.events, self.usage, ""), (self.events, self.usage, "parent answered something else")]
        for events,usage,output in cases:
            with self.subTest(events=events, output=output), self.assertRaises(ValueError):
                evaluate_handoff(self.policy, events, usage, output)

    def test_failed_reverification_invalidates_old_success(self):
        with tempfile.TemporaryDirectory() as d:
            home = Path(d)
            install(home, self.policy, renderer=json_renderer)
            report = home/"autorouter/verification.json"; save(report, {"handoff_verified": True})
            manifest = home/"autorouter/install.json"; data = json.loads(manifest.read_text())
            data["verification_sha256"] = fingerprint(report); save(manifest, data)
            self.assertEqual(status(home)["state"], "verified")
            with patch("autorouter.verify.worker", side_effect=ValueError("expired login")), self.assertRaises(ValueError):
                verify_install(home, live=True)
            self.assertEqual(status(home)["state"], "configured_unverified")

    def test_success_cannot_be_attached_to_a_changed_generation(self):
        with tempfile.TemporaryDirectory() as d:
            home = Path(d); install(home, self.policy, renderer=json_renderer)
            self.usage.update(provider="autorouter", model="AutoRouter-All")
            self.events[0].update(provider="autorouter", model="AutoRouter-All")
            def run(args, **kwargs):
                Path(args[-1]).write_text(json.dumps(self.usage))
                (home/"autorouter/events.jsonl").write_text("".join(json.dumps(e)+"\n" for e in self.events))
                manifest = home/"autorouter/install.json"; data = json.loads(manifest.read_text())
                data["hashes"]["config.yaml"] = "new-generation"; save(manifest, data)
                return subprocess.CompletedProcess(args, 0, self.output, "")
            check = {"loaded": True, "backends": [{"name": "autorouter", "available": True}]}
            picker = {"models": ["AutoRouter-All"], "selected_model": "AutoRouter-All", "selection_succeeded": True}
            with patch("autorouter.verify.worker", side_effect=lambda action, **kw: picker if action == "picker_check" else check), patch("autorouter.verify.runtime", return_value=(Path("/source"), Path("/python"))), patch("autorouter.verify.uuid.uuid4", return_value=types.SimpleNamespace(hex="test")), patch("autorouter.verify.subprocess.run", side_effect=run):
                with self.assertRaisesRegex(ValueError, "changed during verification"):
                    verify_install(home, live=True)
            self.assertFalse((home/"autorouter/verification.json").exists())
