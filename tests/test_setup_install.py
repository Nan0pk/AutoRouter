import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from autorouter.install import install, undo, status, render_config
from autorouter.policy import select_models
from test_setup_policy import model


def json_renderer(text, policy):
    config = json.loads(text or "{}"); config["test_autorouter"] = policy["routine"]
    return json.dumps(config)


class Install(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        self.config = self.home/"config.yaml"
        self.config.write_text('{"user_setting": "keep", "private_existing_key": "fake-private"}\n')
        self.before = self.config.read_bytes()
        self.policy = select_models({"models": [model("go", "small"), model("go", "strong", price=3)]}, "subscriptions")

    def test_install_status_idempotence_and_exact_undo(self):
        result = install(self.home, self.policy, renderer=json_renderer)
        self.assertTrue(result["installed"])
        self.assertEqual(status(self.home)["state"], "configured_unverified")
        self.assertFalse(install(self.home, self.policy, renderer=json_renderer)["changed"])
        self.assertEqual(json.loads(self.config.read_text())["user_setting"], "keep")
        self.assertEqual(os.stat(self.home/"autorouter/policy.json").st_mode & 0o777, 0o600)
        self.assertNotIn("fake-private", (self.home/"autorouter/install.json").read_text())
        undo(self.home)
        self.assertEqual(self.config.read_bytes(), self.before)
        self.assertFalse((self.home/"plugins/autorouter").exists())

    def test_undo_preflights_every_file_before_writing(self):
        install(self.home, self.policy, renderer=json_renderer)
        plugin = self.home/"plugins/autorouter/__init__.py"
        plugin.write_text("# later user edit\n")
        before = self.config.read_bytes()
        with self.assertRaisesRegex(ValueError, "changed"):
            undo(self.home)
        self.assertEqual(self.config.read_bytes(), before)
        self.assertEqual(plugin.read_text(), "# later user edit\n")
        self.assertEqual(status(self.home)["state"], "changed")

    def test_python_runtime_caches_are_not_mistaken_for_user_edits(self):
        install(self.home, self.policy, renderer=json_renderer)
        cache = self.home/"plugins/autorouter/__pycache__"
        cache.mkdir(); (cache/"__init__.cpython-314.pyc").write_bytes(b"compiled cache")
        self.assertEqual(status(self.home)["state"], "configured_unverified")
        undo(self.home)
        self.assertEqual(self.config.read_bytes(), self.before)

    def test_partial_install_failure_rolls_back(self):
        from autorouter import install as module
        original = module.replace_path; count = 0
        def fail(source, destination, displaced):
            nonlocal count
            count += 1
            if count == 2:
                raise OSError("injected disk error")
            return original(source, destination, displaced)
        with patch.object(module, "replace_path", side_effect=fail), self.assertRaises(OSError):
            install(self.home, self.policy, renderer=json_renderer)
        self.assertEqual(self.config.read_bytes(), self.before)
        self.assertFalse((self.home/"plugins/autorouter").exists())
        self.assertFalse((self.home/"autorouter/transaction.json").exists())

    def test_failed_undo_restores_the_installed_state(self):
        from autorouter import install as module
        install(self.home, self.policy, renderer=json_renderer)
        before = self.config.read_bytes(); original = module.copy_path; failed = False
        def fail(source, destination):
            nonlocal failed
            if destination == self.config and not failed:
                failed = True
                raise OSError("injected undo write error")
            return original(source, destination)
        with patch.object(module, "copy_path", side_effect=fail), self.assertRaises(OSError):
            undo(self.home)
        self.assertEqual(self.config.read_bytes(), before)
        self.assertEqual(status(self.home)["state"], "configured_unverified")

    def test_symlink_and_foreign_destination_are_refused(self):
        self.config.unlink(); elsewhere = self.home/"elsewhere"; elsewhere.write_text("untouched")
        self.config.symlink_to(elsewhere)
        with self.assertRaises(ValueError):
            install(self.home, self.policy, renderer=json_renderer)
        self.assertEqual(elsewhere.read_text(), "untouched")

    def test_native_yaml_preserves_unrelated_settings_and_closes_paid_fallbacks(self):
        text = "# my settings\nuser_setting: keep\nfallback_model: paid/model\nauxiliary:\n  vision:\n    provider: paid\n    model: expensive\n    fallback_chain: [paid]\nplugins:\n  enabled: [my-plugin]\n"
        try:
            rendered = render_config(text, self.policy)
        except ValueError as e:
            if "installation" in str(e):
                self.skipTest("native YAML check requires installed Hermes")
            raise
        self.assertIn("# my settings", rendered)
        self.assertIn("user_setting: keep", rendered)
        self.assertIn("my-plugin", rendered)
        self.assertNotIn("expensive", rendered)
        self.assertNotIn("paid/model", rendered)
        self.assertIn("backend: autorouter", rendered)
