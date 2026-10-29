"""Unit tests for the complexity gate.

Run with:  python3 -m unittest discover -s tests -p "test_*.py"
"""
import csv
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from classify import classify  # noqa: E402


class ReviewerCases(unittest.TestCase):
    """The four prompts that motivated the calibration."""

    def test_security_vulnerability_is_complex(self):
        r = classify("fix security vulnerability")
        self.assertEqual(r["class"], "complex")
        self.assertTrue(r["delegate"])
        self.assertIn("security", r["risk"])
        self.assertEqual(r["tier_hint"], "T3+")

    def test_deadlock_question_is_complex(self):
        r = classify("why does this deadlock under load?")
        self.assertEqual(r["class"], "complex")
        self.assertTrue(r["delegate"])

    def test_wide_rename_is_complex_but_not_frontier(self):
        r = classify("rename this variable across 40 files without breaking public APIs")
        self.assertEqual(r["class"], "complex")
        self.assertTrue(r["delegate"])
        self.assertIn("broad_scope", r["risk"])
        self.assertEqual(r["tier_hint"], "T2", "broad mechanical work delegates to a helper, not the frontier")

    def test_redesign_is_complex(self):
        r = classify("redesign the storage layer")
        self.assertEqual(r["class"], "complex")
        self.assertTrue(r["delegate"])


class RiskOverrides(unittest.TestCase):
    def test_short_high_risk_prompts_are_not_penalised(self):
        for prompt in ("fix security vulnerability", "drop the users table", "delete the prod db"):
            r = classify(prompt)
            self.assertEqual(r["class"], "complex", prompt)
            self.assertTrue(r["delegate"], prompt)
            self.assertNotIn("very-short", r["reasons"], prompt)
            self.assertIn("length-signal-skipped(risk-override)", r["reasons"], prompt)

    def test_security_override(self):
        for prompt in (
            "the auth token is leaking into the logs",
            "patch the sql injection",
            "implement rate limiting to stop credential stuffing",
            "add end-to-end encryption for stored messages",
        ):
            self.assertEqual(classify(prompt)["class"], "complex", prompt)

    def test_data_loss_override(self):
        for prompt in (
            "write a migration that drops the unused column",
            "the nightly job truncates the audit table",
            "make the schema change safe to roll back",
        ):
            self.assertEqual(classify(prompt)["class"], "complex", prompt)

    def test_broad_scope_override(self):
        for prompt in (
            "update all 120 services to the new logging format",
            "refactor the monorepo to share a common build",
            "replace the state machine across the codebase",
        ):
            r = classify(prompt)
            self.assertEqual(r["class"], "complex", prompt)
            self.assertTrue(r["delegate"], prompt)

    def test_definitional_question_is_not_broad_scope(self):
        r = classify("what is a monorepo?")
        self.assertNotEqual(r["class"], "complex")
        self.assertNotIn("broad_scope", r["risk"])


class TierHints(unittest.TestCase):
    """`complex` means "route up + delegate", not "start on the frontier"."""

    def test_broad_scope_starts_at_the_hub_tier(self):
        r = classify("rename this variable across 40 files without breaking public APIs")
        self.assertEqual((r["class"], r["tier_hint"]), ("complex", "T2"))

    def test_redesign_starts_at_the_hub_tier(self):
        r = classify("redesign the storage layer")
        self.assertEqual((r["class"], r["tier_hint"]), ("complex", "T2"))
        self.assertIn("broad_scope", r["risk"])
        self.assertTrue(r["delegate"])

    def test_security_and_data_loss_start_strong(self):
        for prompt in ("fix security vulnerability", "make the migration safe to roll back"):
            r = classify(prompt)
            self.assertEqual((r["class"], r["tier_hint"]), ("complex", "T3+"), prompt)

    def test_organic_complexity_starts_strong(self):
        r = classify("why does this deadlock under load?")
        self.assertEqual((r["class"], r["tier_hint"]), ("complex", "T3+"))
        self.assertEqual(r["risk"], [])


class Thresholds(unittest.TestCase):
    def test_trivial_stays_on_main(self):
        for prompt in ("rename foo to bar", "hello", "fix the typo in the README"):
            r = classify(prompt)
            self.assertEqual(r["class"], "trivial", prompt)
            self.assertEqual(r["route"], "main", prompt)
            self.assertFalse(r["delegate"], prompt)

    def test_standard_uses_hierarchy_without_delegation(self):
        for prompt in (
            "add pagination to the users endpoint",
            "fix the failing unit test in auth_test.go",
            "add a timeout to the fetch call",
        ):
            r = classify(prompt)
            self.assertEqual(r["class"], "standard", prompt)
            self.assertEqual(r["route"], "hierarchy", prompt)
            self.assertFalse(r["delegate"], prompt)

    def test_output_contract(self):
        r = classify("redesign the storage layer")
        for key in ("class", "route", "tier_hint", "delegate", "score", "risk", "overrides", "reasons"):
            self.assertIn(key, r)
        self.assertIn(r["class"], {"trivial", "standard", "complex"})


class LabeledSet(unittest.TestCase):
    """The calibration set must stay well-formed."""

    def setUp(self):
        self.path = ROOT / "tests" / "gate" / "labeled_prompts.csv"
        with open(self.path, newline="", encoding="utf-8") as f:
            self.rows = list(csv.DictReader(f))

    def test_size(self):
        self.assertGreaterEqual(len(self.rows), 100)
        self.assertLessEqual(len(self.rows), 200)

    def test_labels_and_splits(self):
        for row in self.rows:
            self.assertIn(row["label"], {"trivial", "standard", "complex"}, row["id"])
            self.assertIn(row["split"], {"dev", "test"}, row["id"])
            self.assertIn(row["risk"], {"", "security", "data_loss", "broad_scope"}, row["id"])
            self.assertTrue(row["prompt"].strip(), row["id"])

    def test_ids_are_unique(self):
        ids = [r["id"] for r in self.rows]
        self.assertEqual(len(ids), len(set(ids)))


class CalibrationBudget(unittest.TestCase):
    """The gate must keep its error profile on the labelled set."""

    def test_test_split_within_budget(self):
        sys.path.insert(0, str(ROOT / "scripts"))
        import calibrate_gate

        rows = [r for r in calibrate_gate.read_labels(str(ROOT / "tests" / "gate" / "labeled_prompts.csv"))
                if r["split"] == "test"]
        s = calibrate_gate.summarise(calibrate_gate.evaluate(rows))
        self.assertLessEqual(s["under_rate"], 0.05, s["under_cases"])
        self.assertLessEqual(s["over_rate"], 0.20, s["over_cases"])
        self.assertEqual(s["risk_missed"], 0, s["risk_missed_cases"])


if __name__ == "__main__":
    unittest.main()
