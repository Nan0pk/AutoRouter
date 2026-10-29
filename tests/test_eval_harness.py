"""Tests for the evaluation harness: fail-closed adapters and fair accounting.

Run with:  python3 -m unittest discover -s tests -p "test_*.py"
"""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "eval"))

import run_eval  # noqa: E402

TASK = {"id": "probe", "category": "mechanical", "prompt": "do the thing", "checks": []}


class AdapterResultValidation(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.workdir = Path(self._tmp.name)
        self.pins = run_eval.load_pins()
        self.addCleanup(self._tmp.cleanup)
        self.addCleanup(os.environ.pop, "AUTOROUTER_EVAL_CMD", None)

    def run_with(self, cmd):
        os.environ["AUTOROUTER_EVAL_CMD"] = cmd
        return run_eval.run_command(TASK, "workhorse", "provider/model", self.pins, self.workdir)

    def test_nonzero_exit_is_an_infrastructure_error(self):
        res = self.run_with("/bin/false")
        self.assertEqual(res["status"], "error")
        self.assertFalse(res["usage_valid"])
        self.assertIn("exited", res["error"])

    def test_no_json_output_is_an_infrastructure_error(self):
        res = self.run_with("true")
        self.assertEqual(res["status"], "error")
        self.assertIn("no JSON", res["error"])

    def test_missing_usage_is_an_infrastructure_error(self):
        res = self.run_with("""echo '{}'""")
        self.assertEqual(res["status"], "error")
        self.assertIn("usage", res["error"])

    def test_zero_usage_is_an_infrastructure_error(self):
        res = self.run_with("""echo '{"tokens_in": 0, "tokens_out": 0}'""")
        self.assertEqual(res["status"], "error")
        self.assertFalse(res["usage_valid"])

    def test_negative_or_float_usage_is_an_infrastructure_error(self):
        for payload in ('{"tokens_in": -1, "tokens_out": 5}',
                        '{"tokens_in": 1.5, "tokens_out": 5}',
                        '{"tokens_in": "10", "tokens_out": 5}'):
            res = self.run_with(f"echo '{payload}'")
            self.assertEqual(res["status"], "error", payload)
            self.assertNotIn("usage_valid", ())  # guard against silent pass

    def test_valid_usage_is_accepted_and_reported_counts_are_kept(self):
        res = self.run_with(
            """echo '{"tokens_in": 1200, "tokens_out": 300, "latency_ms": 42, "retries": 2, "escalations": 1}'"""
        )
        self.assertEqual(res["status"], "ok")
        self.assertTrue(res["usage_valid"])
        self.assertEqual((res["tokens_in"], res["tokens_out"]), (1200, 300))
        self.assertEqual(res["latency_ms"], 42)
        self.assertEqual((res["reported_retries"], res["reported_escalations"]), (2, 1))


class OutcomeAccounting(unittest.TestCase):
    def setUp(self):
        self.pins = run_eval.load_pins()
        self.task = next(t for t in run_eval.load_tasks() if t["id"] == "mech-port")
        self._orig_execute = run_eval.execute
        self.addCleanup(setattr, run_eval, "execute", self._orig_execute)

    def test_adapter_error_is_not_a_model_failure_and_is_not_retried(self):
        calls = []

        def broken(executor, task, role, model, pins, workdir):
            calls.append(role)
            return run_eval._adapter_error("boom", 5)

        run_eval.execute = broken
        rec = run_eval.run_task("fixed_workhorse_retry", self.task, "command", self.pins)
        self.assertEqual(rec["outcome"], "infrastructure_error")
        self.assertEqual(rec["adapter_error"], "boom")
        self.assertEqual(len(rec["attempts"]), 1, "an adapter error must not be retried")
        self.assertEqual(rec["retries"], 0)

    def test_executor_reported_counts_land_in_attempts_and_totals(self):
        def noisy(executor, task, role, model, pins, workdir):
            return run_eval._ok(100, 10, 1, 2, 1, [model])

        run_eval.execute = noisy
        rec = run_eval.run_task("fixed_workhorse", self.task, "command", self.pins)
        self.assertEqual(rec["reported_retries"], 2)
        self.assertEqual(rec["reported_escalations"], 1)
        self.assertEqual(rec["retries"], 2)
        self.assertEqual(rec["attempts"][0]["reported_retries"], 2)

    def test_retry_baseline_retries_the_same_model_without_escalating(self):
        run_eval.execute = self._fail_then_pass
        rec = run_eval.run_task("fixed_workhorse_retry", self.task, "command", self.pins)
        self.assertEqual(rec["outcome"], "success")
        self.assertEqual(rec["harness_retries"], 1)
        self.assertEqual(rec["harness_escalations"], 0)
        self.assertEqual(rec["attempts"][0]["model"], rec["attempts"][1]["model"])

    def _fail_then_pass(self, executor, task, role, model, pins, workdir):
        # No files are written, so the first verify fails and the retry passes
        # only if the harness actually runs a second attempt and we patch then.
        state = getattr(self, "_calls", 0)
        self._calls = state + 1
        if state >= 1:
            patch = run_eval.load_json(run_eval.EVAL / "stub_patches.json")["mech-port"]["files"]
            for name, body in patch.items():
                (Path(workdir) / name).write_text(body, encoding="utf-8")
        return run_eval._ok(100, 10, 1, 0, 0, [model])


class ReportValidity(unittest.TestCase):
    def setUp(self):
        self.pins = run_eval.load_pins()
        self.task = next(t for t in run_eval.load_tasks() if t["id"] == "mech-port")

    def test_stub_is_never_valid_evidence(self):
        rec = run_eval.run_task("fixed_workhorse", self.task, "stub", self.pins)
        report = run_eval.build_report([rec], self.pins, "stub", ["fixed_workhorse"])
        self.assertTrue(report["synthetic"])
        self.assertFalse(report["valid_evidence"])
        self.assertIn("synthetic", " ".join(report["validity"]["reasons"]).lower())

    def test_clean_command_run_is_valid_only_with_a_sandbox(self):
        rec = run_eval.run_task("fixed_workhorse", self.task, "stub", self.pins)
        report = run_eval.build_report([rec], self.pins, "command", ["fixed_workhorse"])
        if run_eval.detect_sandbox() == "none":
            self.assertFalse(report["valid_evidence"])
            self.assertIn("sandbox", " ".join(report["validity"]["reasons"]).lower())
        else:
            self.assertTrue(report["valid_evidence"], report["validity"]["reasons"])

    def test_one_adapter_error_invalidates_the_whole_report(self):
        rec = run_eval.run_task("fixed_workhorse", self.task, "stub", self.pins)
        bad = dict(rec)
        bad["outcome"] = "infrastructure_error"
        bad["adapter_error"] = "boom"
        report = run_eval.build_report([rec, bad], self.pins, "command", ["fixed_workhorse"])
        self.assertFalse(report["valid_evidence"])
        self.assertIn("adapter", " ".join(report["validity"]["reasons"]).lower())
        arm = report["arms"]["fixed_workhorse"]
        self.assertEqual(arm["infrastructure_errors"], 1)
        self.assertEqual(len(arm["adapter_errors"]), 1)
        self.assertEqual(len(arm["failures"]), 0, "adapter errors must not be listed as model failures")

    def test_success_rate_excludes_infrastructure_errors(self):
        ok = run_eval.run_task("fixed_workhorse", self.task, "stub", self.pins)
        bad = dict(ok, outcome="infrastructure_error", adapter_error="boom", success=False)
        arm = run_eval.summarise([ok, bad], "fixed_workhorse", self.pins)
        self.assertEqual(arm["tasks"], 2)
        self.assertEqual(arm["tasks_scored"], 1)
        self.assertEqual(arm["success_rate"], 1.0)
        self.assertEqual(arm["success_rate_of_all_tasks"], 0.5)


class BehaviouralVerification(unittest.TestCase):
    """Static text checks are weak; behavioural tests must carry the verdict."""

    def setUp(self):
        self.pins = run_eval.load_pins()
        self.task = next(t for t in run_eval.load_tasks() if t["id"] == "sec-path")
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)

    @unittest.skipIf(run_eval.detect_sandbox() == "none", "no sandbox available")
    def test_static_checks_pass_while_behaviour_fails(self):
        workdir = Path(self._tmp.name) / "sec-path"
        import shutil

        shutil.copytree(run_eval.EVAL / self.task["fixture"], workdir)
        # A plausible-looking "fix": mentions realpath, never joins the raw
        # name, and still lets an absolute path escape the base directory.
        (workdir / "files.py").write_text(
            "import os\n\n\ndef read_file(base, name):\n"
            "    path = os.path.realpath(os.path.join(base, name.replace('../', '')))\n"
            "    with open(path) as f:\n        return f.read()\n"
        )
        passed, failures = run_eval.verify(workdir, self.task["checks"])
        self.assertEqual(failures, [], "the planted solution must satisfy the static checks")
        behavior = run_eval.run_behavior(
            self.task, workdir, self.pins, run_eval.detect_sandbox()
        )
        self.assertEqual(behavior["status"], "ok")
        self.assertLess(behavior["passed"], behavior["total"], "behaviour must catch the traversal")

    def test_missing_sandbox_invalidates_the_report(self):
        rec = run_eval.run_task("fixed_workhorse", self.task, "stub", self.pins, sandbox="none")
        self.assertEqual(rec["behavior"]["status"], "skipped")
        self.assertFalse(rec["verified"])
        report = run_eval.build_report([rec], self.pins, "command", ["fixed_workhorse"])
        self.assertFalse(report["valid_evidence"])
        self.assertIn("sandbox", " ".join(report["validity"]["reasons"]).lower())
        self.assertEqual(report["sandbox"]["mechanism"], "none")

    def test_verified_flag_is_recorded(self):
        rec = run_eval.run_task("fixed_workhorse", self.task, "stub", self.pins)
        if run_eval.detect_sandbox() == "none":
            self.assertFalse(rec["verified"])
        else:
            self.assertTrue(rec["verified"])
            self.assertGreater(rec["behavior"]["total"], 0)


class ScopeStatement(unittest.TestCase):
    def test_report_says_it_is_the_gate_based_policy(self):
        pins = run_eval.load_pins()
        task = next(t for t in run_eval.load_tasks() if t["id"] == "mech-port")
        rec = run_eval.run_task("fixed_workhorse", task, "stub", pins)
        report = run_eval.build_report([rec], pins, "stub", ["fixed_workhorse"])
        self.assertEqual(report["policy"]["name"], "gate-based routing policy")
        self.assertFalse(report["policy"]["exercises_opencode_plugin"])
        self.assertFalse(report["policy"]["exercises_delegation"])
        self.assertIn("does not exercise the OpenCode plugin", run_eval.render_markdown(report))


class MarkdownSurface(unittest.TestCase):
    def test_invalid_runs_say_so(self):
        pins = run_eval.load_pins()
        task = next(t for t in run_eval.load_tasks() if t["id"] == "mech-port")
        rec = run_eval.run_task("fixed_workhorse", task, "stub", pins)
        rec["outcome"] = "infrastructure_error"
        rec["adapter_error"] = "boom"
        report = run_eval.build_report([rec], pins, "command", ["fixed_workhorse"])
        md = run_eval.render_markdown(report)
        self.assertIn("INVALID RUN", md)
        self.assertIn("not evidence", md)
        self.assertIn("Infrastructure / adapter errors", md)


if __name__ == "__main__":
    unittest.main()
