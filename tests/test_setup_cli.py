import json
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

from test_setup_policy import model
from autorouter.cli import choose

ROOT = Path(__file__).resolve().parents[1]


class SetupCommand(unittest.TestCase):
    def test_interactive_menu_accepts_each_choice(self):
        values = ["subscriptions", "provider", "models", "free"]
        for number,expected in enumerate(values, 1):
            self.assertEqual(choose("Choose: ", values, lambda _: str(number)), expected)

    def run_cli(self, *args, input_text=None):
        return subprocess.run([sys.executable, str(ROOT/"scripts/setup_autorouter.py"), *args],
                              input=input_text, capture_output=True, text=True, timeout=15)

    def test_unsupported_harness_fails_before_writes_or_discovery(self):
        p = self.run_cli("setup", "imaginary", "--yes")
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("not supported", p.stderr)

    def test_noninteractive_requires_explicit_mode_and_confirmation(self):
        p = self.run_cli("setup", "hermes")
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("--mode", p.stderr)

    def test_four_dry_run_modes_use_same_selection_and_change_nothing(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d); inventory = root/"inventory.json"; home = root/"home"
            rows = [model("go", "small"), model("go", "large", price=2),
                    model("free", "small", "free", 0), model("free", "large", "free", 0)]
            inventory.write_text(json.dumps({"models": rows, "providers": [{"id": "go"}, {"id": "free"}]}))
            for mode,extra in (("subscriptions", []), ("provider", ["--provider", "go"]),
                               ("models", ["--model", "go/small", "--model", "go/large"]), ("free", [])):
                p = self.run_cli("setup", "hermes", "--mode", mode, "--dry-run", "--inventory", str(inventory), "--home", str(home), *extra)
                self.assertEqual(p.returncode, 0, p.stderr)
                self.assertIn("Routine:", p.stdout)
                self.assertIn("Specialist:", p.stdout)
                self.assertFalse(home.exists())

    def test_inventory_cannot_authorize_installing_fake_free_pricing(self):
        p = self.run_cli("setup", "hermes", "--mode", "free", "--yes", "--inventory", "/does-not-exist")
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("dry-run", p.stderr)
