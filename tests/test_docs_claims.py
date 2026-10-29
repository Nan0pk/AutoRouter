"""Consistency tests for the enforced-vs-advisory claims in the docs.

docs/enforcement.md is the single source of truth. These tests fail when a doc
starts claiming runtime enforcement for a mechanism that is only advice.
"""
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DOCS = [ROOT / "README.md", *sorted((ROOT / "docs").glob("*.md"))]
ENFORCEMENT = ROOT / "docs" / "enforcement.md"

# Present-tense enforcement claims about policy mechanisms. "can enforce" and
# "would enforce" are capability statements, not claims about this repository.
FORBIDDEN = [
    re.compile(r"\benforces\b[^.\n]{0,40}\b(gate|quotas?|budget|ledger)\b", re.I),
    re.compile(r"\b(is|are) enforced\b[^.\n]{0,40}\b(gate|quotas?|budget|ledger)\b", re.I),
    re.compile(r"\b(quota|budget) (is|are) enforced\b", re.I),
    re.compile(r"\bgate is enforced\b", re.I),
    re.compile(r"\b(gate|quotas?|budget|ledger)\b[^.\n]{0,40}\benforces\b", re.I),
    re.compile(r"\bguarantees?\b[^.\n]{0,30}\b(quota|budget|savings)\b", re.I),
]
MEASURED_SAVINGS = re.compile(r"\b\d+(\.\d+)?%\s*(quota|cost|token)\s*(saving|reduction)\b", re.I)


def read(path):
    return path.read_text(encoding="utf-8")


class EnforcementDocExists(unittest.TestCase):
    def test_present(self):
        self.assertTrue(ENFORCEMENT.exists())

    def test_covers_the_key_mechanisms(self):
        text = read(ENFORCEMENT).lower()
        for needle in ("complexity gate", "quota ledger", "model selection",
                       "delegation orchestration", "advisory"):
            self.assertIn(needle, text)

    def test_states_savings_are_unvalidated(self):
        self.assertIn("quota savings are unvalidated", read(ENFORCEMENT).lower())


class DocsPointAtTheBoundary(unittest.TestCase):
    """Every doc that describes routing behaviour must link the status page."""

    def test_readme_links_enforcement(self):
        self.assertIn("docs/enforcement.md", read(ROOT / "README.md"))

    def test_routing_model_links_enforcement(self):
        self.assertIn("enforcement.md", read(ROOT / "docs" / "routing-model.md"))

    def test_harness_integration_links_enforcement(self):
        self.assertIn("enforcement.md", read(ROOT / "docs" / "harness-integration.md"))


class StatusTableConsistency(unittest.TestCase):
    def _rows(self, path):
        rows = {}
        for line in read(path).splitlines():
            if not line.startswith("|"):
                continue
            cells = [c.strip() for c in line.strip("|").split("|")]
            if len(cells) >= 2:
                rows[cells[0].lower()] = " | ".join(cells[1:])
        return rows

    def test_readme_marks_gate_and_quota_as_not_enforced(self):
        rows = self._rows(ROOT / "README.md")
        gate = next((v for k, v in rows.items() if "complexity gate" in k), None)
        quota = next((v for k, v in rows.items() if "quota" in k and "ledger" in k), None)
        self.assertIsNotNone(gate)
        self.assertIsNotNone(quota)
        self.assertIn("advisory", gate.lower())
        self.assertIn("not implemented", quota.lower())

    def test_enforcement_doc_agrees(self):
        rows = self._rows(ENFORCEMENT)
        gate = next((v for k, v in rows.items() if "complexity gate" in k), None)
        quota = next((v for k, v in rows.items() if "quota" in k and "ledger" in k), None)
        self.assertIsNotNone(gate)
        self.assertIsNotNone(quota)
        self.assertIn("advisory", gate.lower())
        self.assertIn("not implemented", quota.lower())


class GateClaims(unittest.TestCase):
    """Headline claims must carry their caveats, not bury them."""

    def test_readme_headline_says_in_sample(self):
        lines = [ln for ln in read(ROOT / "README.md").splitlines()
                 if "complexity gate" in ln.lower() and "calibrated" in ln.lower()]
        self.assertTrue(lines, "no complexity-gate status row found")
        self.assertIn("in-sample", lines[0].lower())
        self.assertIn("not held-out", lines[0].lower())

    def test_gate_readme_leading_warning(self):
        text = read(ROOT / "tests" / "gate" / "README.md").lower()
        head = text[:600]
        self.assertIn("in-sample", head)
        self.assertTrue(
            "not** held-out accuracy" in head or "not held-out accuracy" in head,
            "the gate README must say up front that the numbers are not held-out accuracy",
        )

    def test_complex_does_not_imply_frontier(self):
        text = read(ROOT / "docs" / "routing-model.md").lower()
        self.assertIn("does not mean \"start on a frontier model\"", text)
        self.assertIn("t2", text)


class DataClaims(unittest.TestCase):
    """Provenance metadata is not verification; the docs must not imply it is."""

    def test_readme_states_the_dataset_is_unverified(self):
        text = read(ROOT / "README.md").lower()
        self.assertIn("0/97", text)
        self.assertNotIn("real oct-2026", text)

    def test_data_readme_documents_the_three_valued_status(self):
        text = read(ROOT / "data" / "README.md").lower()
        for status in ("verified", "unverified", "estimated"):
            self.assertIn(status, text)
        self.assertIn("illustrative", text)
        self.assertIn("archive_url", text)

    def test_cache_prices_are_labelled_estimates(self):
        header = read(ROOT / "data" / "quota.example.csv").splitlines()[0]
        self.assertIn("cache_price_source", header)
        body = read(ROOT / "data" / "quota.example.csv").lower()
        self.assertIn("estimated", body)


class DecisionCadence(unittest.TestCase):
    def test_policies_are_alternatives_not_a_rule_and_an_exception(self):
        text = read(ROOT / "docs" / "routing-model.md").lower()
        self.assertIn("session-sticky", text)
        self.assertIn("turn-adaptive", text)
        self.assertNotIn("one decision per session.", text)
        self.assertIn("decision cadence", text)


class NoOverclaims(unittest.TestCase):
    def test_no_present_tense_enforcement_claims(self):
        failures = []
        for path in DOCS:
            if path == ENFORCEMENT:
                continue
            for lineno, line in enumerate(read(path).splitlines(), start=1):
                for pattern in FORBIDDEN:
                    if pattern.search(line):
                        failures.append(f"{path.name}:{lineno}: {line.strip()}")
        self.assertEqual(failures, [], "docs claim runtime enforcement that does not exist")

    def test_no_unmeasured_savings_claim(self):
        failures = []
        for path in DOCS:
            for lineno, line in enumerate(read(path).splitlines(), start=1):
                if MEASURED_SAVINGS.search(line):
                    failures.append(f"{path.name}:{lineno}: {line.strip()}")
        self.assertEqual(failures, [], "docs quote a quota/cost saving that no eval backs")

    def test_advisory_sections_say_so(self):
        for name in ("routing-model.md", "harness-integration.md"):
            self.assertIn("advisory", read(ROOT / "docs" / name).lower(), name)


if __name__ == "__main__":
    unittest.main()
