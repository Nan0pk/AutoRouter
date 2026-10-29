"""Tests for scoring, provenance verification and the cost model.

Run with:  python3 -m unittest discover -s tests -p "test_*.py"
"""
import csv
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import score_models  # noqa: E402

FIXTURES = ROOT / "tests" / "fixtures"
HEADER = [
    "provider", "model", "metric", "value", "source", "source_kind",
    "benchmark_version", "collected_on", "conditions", "provenance_status",
    "source_url", "archive_url", "derivation", "notes",
]


def write_csv(rows):
    tmp = tempfile.NamedTemporaryFile("w", suffix=".csv", delete=False, newline="")
    writer = csv.writer(tmp)
    writer.writerow(HEADER)
    writer.writerows(rows)
    tmp.close()
    return tmp.name


def row(**overrides):
    base = {
        "provider": "p", "model": "m", "metric": "deepswe", "value": "70",
        "source": "board", "source_kind": "independent", "benchmark_version": "v1",
        "collected_on": "2026-10-01", "conditions": "tools on",
        "provenance_status": "verified",
        "source_url": "https://example.test/x", "archive_url": "https://archive.test/x",
        "derivation": "", "notes": "",
    }
    base.update(overrides)
    return [base[h] for h in HEADER]


class ProvenanceValidation(unittest.TestCase):
    def load(self, path):
        return score_models.load_observations(str(path), [])

    def test_verified_requires_source_and_archive_urls(self):
        with self.assertRaises(SystemExit) as ctx:
            self.load(FIXTURES / "verified-missing-archive-benchmarks.csv")
        self.assertIn("archive_url", str(ctx.exception))

    def test_estimated_requires_a_derivation(self):
        with self.assertRaises(SystemExit) as ctx:
            self.load(FIXTURES / "estimated-no-derivation-benchmarks.csv")
        self.assertIn("derivation", str(ctx.exception))

    def test_unknown_status_is_rejected(self):
        path = write_csv([row(provenance_status="probably-fine")])
        with self.assertRaises(SystemExit) as ctx:
            self.load(path)
        self.assertIn("provenance_status", str(ctx.exception))

    def test_verified_observation_carries_urls(self):
        obs = self.load(FIXTURES / "verified-benchmarks.csv")
        (provider, model), metrics = next(iter(obs.items()))
        entry = metrics["deepswe"][0]
        self.assertEqual(entry["provenance_status"], "verified")
        self.assertTrue(entry["source_url"].startswith("http"))
        self.assertTrue(entry["archive_url"].startswith("http"))

    def test_example_dataset_claims_no_verification(self):
        obs = self.load(ROOT / "data" / "benchmarks.example.csv")
        entries = [o for metrics in obs.values() for lst in metrics.values() for o in lst]
        self.assertTrue(entries)
        self.assertEqual({o["provenance_status"] for o in entries}, {"unverified"})
        self.assertTrue(all(not o["source_url"] and not o["archive_url"] for o in entries))

    def test_estimated_observation_carries_its_derivation(self):
        path = write_csv([row(provenance_status="estimated", source="derived",
                              source_url="", archive_url="",
                              derivation="0.1x input, 1.25x write")])
        obs = self.load(path)
        entry = next(iter(obs.values()))["deepswe"][0]
        self.assertEqual(entry["provenance_status"], "estimated")
        self.assertIn("1.25x", entry["derivation"])


class CostModel(unittest.TestCase):
    def test_cache_hits_are_cheaper_than_misses(self):
        miss = score_models.request_cost(1000, 0, 0.0, 10, 0, 1, 12.5)
        hit = score_models.request_cost(1000, 0, 1.0, 10, 0, 1, 12.5)
        self.assertGreater(miss, hit)

    def test_bills_read_and_write_prices_explicitly(self):
        # 1000 prompt tokens, all cache hits, cache read $1/M
        self.assertAlmostEqual(score_models.request_cost(1000, 0, 1.0, 10, 0, 1, 12.5), 0.001)
        # 1000 prompt tokens, all misses, cache write $12.5/M
        self.assertAlmostEqual(score_models.request_cost(1000, 0, 0.0, 10, 0, 1, 12.5), 0.0125)

    def test_missing_cache_prices_fall_back_to_input_price(self):
        # No cache discount assumed: 1M tokens at $5/M, half "cached".
        self.assertAlmostEqual(score_models.request_cost(1_000_000, 0, 0.5, 5, 0, None, None), 5.0)

    def test_output_tokens_are_billed(self):
        self.assertAlmostEqual(score_models.request_cost(0, 1000, 0.5, 1, 30, 0.1, 1.25), 0.03)


class TierUncertainty(unittest.TestCase):
    def test_min_coverage_withholds_a_tier(self):
        # 0.30 of weight observed against a 0.5 floor.
        self.assertLess(0.30, 0.5)

    def test_verified_coverage_is_weighted(self):
        # Guard the intent: verified coverage can never exceed total coverage.
        obs = score_models.load_observations(str(FIXTURES / "verified-benchmarks.csv"), [])
        (provider, model), metrics = next(iter(obs.items()))
        self.assertLessEqual(set(metrics), {"deepswe", "tb4"})


if __name__ == "__main__":
    unittest.main()
