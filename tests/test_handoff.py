"""Regression tests for the isolation boundary and checkpoint round trip."""
import json
import os
import shlex
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "eval"))

import isolation  # noqa: E402
import run_eval  # noqa: E402


@unittest.skipUnless(isolation.available(), "offline Bubblewrap grader is unavailable")
class Verifier(unittest.TestCase):
    def test_equivalent_patches_pass(self):
        cases = {
            "bug-null": {"user.py": "def display_name(user):\n"
                                   "    return user.profile.name if user is not None else ''\n"},
            "debug-mutation": {"aggregate.py": "def total_by_key(rows):\n"
                                              "    totals = {}\n"
                                              "    for row in rows:\n"
                                              "        key = row['k']\n"
                                              "        totals[key] = totals.get(key, 0) + row['v']\n"
                                              "    return {k: v for k, v in totals.items() if v}\n"},
            "broad-validate": {
                f"{name}.py": "import json\n\ndef load(path):\n"
                              "    with open(path) as f:\n"
                              "        obj = json.load(f)\n"
                              "    if not isinstance(obj, dict):\n"
                              "        raise ValueError('expected an object')\n"
                              "    return obj\n"
                for name in ("a", "b", "c")
            },
        }
        tasks = {task["id"]: task for task in run_eval.load_tasks()}
        pins = run_eval.load_pins()
        for task_id, files in cases.items():
            with self.subTest(task=task_id), tempfile.TemporaryDirectory() as workdir:
                for filename, source in files.items():
                    (Path(workdir) / filename).write_text(source)
                task = tasks[task_id]
                self.assertEqual(run_eval.verify(workdir, task["checks"])[1], [])
                result = run_eval.run_behavior(task, workdir, pins, "bwrap")
                self.assertEqual(result["status"], "ok", result)
                self.assertGreater(result["total"], 0)
                self.assertEqual(result["passed"], result["total"], result)

    def test_empty_grade_is_rejected(self):
        task = {"behavior": "behavior/debug-mutation.py"}
        with tempfile.TemporaryDirectory() as workdir:
            (Path(workdir) / "aggregate.py").write_text(
                "print('{\"checks\": []}', flush=True)\nraise SystemExit(0)\n"
            )
            result = run_eval.run_behavior(task, workdir, run_eval.load_pins(), "bwrap")
        self.assertEqual(result["status"], "broke", result)

    def test_verifier_has_read_only_candidate_and_different_network(self):
        host_net = os.readlink("/proc/self/ns/net")
        with tempfile.TemporaryDirectory() as workdir:
            candidate = Path(workdir) / "example"
            candidate.write_text("before")
            command = [
                "/bin/sh", "-c",
                'test "$(readlink /proc/self/ns/net)" != "$1" && '
                '! (printf after > example) && test "$(cat example)" = before',
                "check", host_net,
            ]
            result = isolation.run(workdir, command, readonly=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(candidate.read_text(), "before")


@unittest.skipUnless(isolation.available(network=True), "Bubblewrap is unavailable")
class WorkerBoundary(unittest.TestCase):
    def test_worker_cannot_see_originals_or_unrelated_environment(self):
        with tempfile.TemporaryDirectory() as root:
            host_file = Path(root) / "gold"
            host_file.write_text("pristine")
            workdir = Path(root) / "trial"
            workdir.mkdir()
            grader = run_eval.EVAL / "behavior/debug-mutation.py"
            cmd = (
                f"test ! -e {shlex.quote(str(host_file))} && "
                f"test ! -e {shlex.quote(str(grader))} && "
                'test -z "$AUTOROUTER_HOST_ONLY" && printf changed > result && '
                """echo '{"tokens_in": 2, "tokens_out": 1}'"""
            )
            with patch.dict(os.environ, {"AUTOROUTER_EVAL_CMD": cmd,
                                        "AUTOROUTER_HOST_ONLY": "unforwarded"}):
                result = run_eval.run_command(
                    {"id": "probe", "prompt": "probe"}, "workhorse", "test/worker",
                    run_eval.load_pins(), workdir,
                )
            self.assertEqual(result["status"], "ok", result)
            self.assertEqual(host_file.read_text(), "pristine")
            self.assertEqual((workdir / "result").read_text(), "changed")

    def test_grader_mount_is_rejected(self):
        with patch.dict(os.environ, {"AUTOROUTER_EVAL_MOUNTS": json.dumps([str(ROOT / "eval")])}):
            with self.assertRaisesRegex(isolation.IsolationError, "grading inputs"):
                isolation.adapter_settings()


@unittest.skipUnless(isolation.available() and isolation.available(network=True),
                     "both Bubblewrap modes are required")
class CheckpointRoundTrip(unittest.TestCase):
    def test_private_acp_worker_round_trip_resumes_in_a_new_process(self):
        adapter = ROOT / "eval/adapters/opencode_acp.py"
        fake = ROOT / "tests/fixtures/fake-opencode-acp.py"
        env = {
            **os.environ,
            "AUTOROUTER_EVAL_CMD": f"python3 {shlex.quote(str(adapter))}",
            "AUTOROUTER_EVAL_MOUNTS": json.dumps([str(adapter), str(fake)]),
            "AUTOROUTER_EVAL_FORWARD": json.dumps(["AUTOROUTER_OPENCODE"]),
            "AUTOROUTER_OPENCODE": str(fake),
        }
        with tempfile.TemporaryDirectory() as root:
            directory = Path(root) / "run"
            cmd = [sys.executable, str(ROOT / "eval/run_handoff.py"), "--run-dir", str(directory)]
            first = subprocess.run(
                cmd + ["--executor", "command", "--workhorse", "test/workhorse/fast",
                       "--specialist", "test/specialist", "--steps", "2"],
                env=env, text=True, capture_output=True, timeout=30,
            )
            self.assertEqual(first.returncode, 0, first.stderr)
            self.assertEqual(json.loads(first.stdout)["phase"], "specialist")
            saved = (directory / "checkpoint-2/summary.py").read_bytes()
            second = subprocess.run(cmd + ["--resume"], env=env, text=True,
                                    capture_output=True, timeout=30)
            self.assertEqual(second.returncode, 0, second.stderr)
            state = run_eval.load_json(directory / "state.json")
            self.assertEqual(state["phase"], "completed", state)
            self.assertEqual([a["model"] for a in state["attempts"]],
                             ["test/workhorse/fast", "test/workhorse/fast",
                              "test/specialist", "test/workhorse/fast"])
            self.assertEqual((directory / "checkpoint-3/summary.py").read_bytes(), saved)
            self.assertEqual((directory / "checkpoint-2/summary.py").read_bytes(), saved)
            self.assertEqual([a["execution"]["tokens_in"] for a in state["attempts"]], [12] * 4)
