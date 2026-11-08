import json
import subprocess
import time
import unittest
from unittest.mock import patch

from autorouter.policy import select_models
from autorouter.credentials import opencode_credential
from autorouter.hermes import go_metadata
from autorouter.probe import select_usable


def model(provider, name, billing="included", price=1, **changes):
    return {"id": f"{provider}/{name}", "provider": provider, "model": name,
            "runtime_provider": provider, "billing": billing, "input_price": price,
            "output_price": price, "tools": True, "reasoning": True,
            "source": "https://provider.example/models", "fetched_at": time.time(),
            "quota": None, **changes}


class Selection(unittest.TestCase):
    def test_current_go_catalog_nested_capabilities_and_prices(self):
        r = go_metadata({"cost": {"text": {"input": {"price": .15}, "output": {"price": .6}}},
                         "capabilities": {"tools": {"supported": True}, "reasoning": {"supported": True}}})
        self.assertEqual(r, {"input_price": .15, "output_price": .6, "tools": True, "reasoning": True})

    def setUp(self):
        self.rows = [model("go", "small"), model("go", "large", price=3),
                     model("other", "small"), model("other", "large", price=4),
                     model("free", "one", "free", 0), model("free", "two", "free", 0),
                     model("paid", "paid", "metered"), model("unknown", "model", "unknown")]
        self.inventory = {"models": self.rows}

    def test_four_modes_and_provider_scoped_ids(self):
        p = select_models(self.inventory, "subscriptions")
        self.assertEqual({r["provider"] for r in p["models"]}, {"go", "other"})
        p = select_models(self.inventory, "provider", providers=["go"])
        self.assertEqual({r["provider"] for r in p["models"]}, {"go"})
        p = select_models(self.inventory, "models", models=["go/small", "other/large"])
        self.assertEqual({r["id"] for r in p["models"]}, {"go/small", "other/large"})
        p = select_models(self.inventory, "free")
        self.assertEqual({r["provider"] for r in p["models"]}, {"free"})
        self.assertNotEqual(p["routine"], p["specialist"])

    def test_paid_unknown_stale_and_exhausted_are_excluded(self):
        self.rows += [model("bad", "old", "free", 0, fetched_at=0),
                      model("bad", "bool", "free", False), model("bad", "negative", "free", -1),
                      model("bad", "missing", "free", None), model("bad", "nan", "free", float("nan")),
                      model("bad", "exhausted", quota={"windows": [{"used_percent": 100}]}),
                      model("bad", "notools", tools=False)]
        self.rows += [model("bad", "harness", harness_supported=False)]
        for mode in ("subscriptions", "free"):
            p = select_models(self.inventory, mode)
            self.assertFalse(any(r["provider"] in ("bad", "paid", "unknown") for r in p["models"]))

    def test_exact_selection_and_two_models_are_required(self):
        for args in ({"mode": "provider", "providers": ["paid"]},
                     {"mode": "models", "models": ["go/small"]},
                     {"mode": "models", "models": ["small", "other/large"]},
                     {"mode": "models", "models": ["go/small", "paid/paid"]},
                     {"mode": "provider", "providers": []}):
            with self.subTest(args=args), self.assertRaises(ValueError):
                select_models(self.inventory, **args)

    def test_allowance_assertion_is_explicit_and_quota_stays_unknown(self):
        self.rows += [model("unknown", "second", "unknown")]
        p = select_models(self.inventory, "provider", providers=["unknown"],
                          confirmed_allowances=["unknown"])
        self.assertTrue(p["allowance_assertions"])
        self.assertTrue(all(r["quota"] is None for r in p["models"]))
        with self.assertRaises(ValueError):
            select_models(self.inventory, "provider", providers=["paid"], confirmed_allowances=["paid"])

    def test_unverified_capability_is_not_used_as_benchmark_evidence(self):
        self.rows[0].update(capability=999, capability_verified=False)
        p = select_models(self.inventory, "provider", providers=["go"])
        self.assertIn("provisional", " ".join(p["reasons"]).lower())

    def test_rejected_account_models_are_removed_before_install(self):
        seen = []
        def probe(row):
            seen.append(row["id"])
            return {"available": row["id"] != "go/small", "reason": "provider rejected model"}
        p = select_usable(self.inventory, "subscriptions", probe=probe)
        self.assertNotIn("go/small", {r["id"] for r in p["models"]})
        self.assertEqual(len(seen), len(set(seen)))
        with self.assertRaises(ValueError):
            select_usable(self.inventory, "models", models=["go/small", "go/large"], probe=probe)


class Credentials(unittest.TestCase):
    def row(self):
        return {"integrationID": "opencode", "id": "account", "active": True,
                "value": {"type": "oauth", "methodID": "device", "access": "secret-access",
                          "refresh": "secret-refresh", "expires": time.time()*1000+60000,
                          "metadata": {"server": "https://opencode.ai/console", "orgID": "workspace"}}}

    def export(self, rows, expected=None):
        with patch("autorouter.credentials.subprocess.run", return_value=subprocess.CompletedProcess([], 0, json.dumps(rows))):
            return opencode_credential("opencode", expected)

    def test_only_access_is_returned_and_account_is_pinned(self):
        c = self.export([self.row()])
        self.assertEqual(c["token"], "secret-access")
        self.assertNotIn("secret-refresh", repr(c))
        self.assertEqual(c["account"], "account")
        with self.assertRaises(ValueError):
            self.export([self.row()], {"account": "different", "organization": "workspace"})

    def test_expiry_origin_workspace_and_active_account_fail_closed(self):
        for change in ({"expires": 0}, {"metadata": {"server": "https://evil.example", "orgID": "workspace"}},
                       {"metadata": {"server": "https://opencode.ai/console", "orgID": "bad\nheader"}}):
            row = self.row(); row["value"].update(change)
            with self.subTest(change=change), self.assertRaises(ValueError) as e:
                self.export([row])
            self.assertNotIn("secret-access", str(e.exception))
        with self.assertRaises(ValueError):
            self.export([self.row(), self.row()])
