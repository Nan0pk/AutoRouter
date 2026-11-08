import copy
import json
import shutil
import tempfile
import unittest
from pathlib import Path

from autorouter.plugin import Router
from autorouter.policy import select_models
from autorouter.hermes import runtime, worker
from test_setup_policy import model


class PluginRouting(unittest.TestCase):
    def setUp(self):
        self.policy = select_models({"models": [model("go", "small"), model("go", "strong", price=3)]}, "subscriptions")
        self.router = Router(self.policy)
        self.tools = [{"type": "function", "function": {"name": "delegate_task"}}]

    def turn(self, text, **extra):
        return self.router.pre_turn(session_id="one", turn_id="turn", model="small",
                                    user_message=text, parent_session_id="", **extra)

    def test_complex_root_forces_specialist_even_if_goal_was_simplified(self):
        result = self.turn("why does this deadlock under load when we refactor the async pipeline")
        self.assertIn("specialist", result["context"])
        req = self.router.llm_request(request={"tools": self.tools}, session_id="one", turn_id="turn")
        self.assertEqual(req["request"]["tool_choice"]["function"]["name"], "delegate_task")
        args = {"tasks": [{"goal": "fix it", "route": "routine"}]}
        effective = self.router.tool_request("delegate_task", args, session_id="one", turn_id="turn")
        route = self.router.route_task(effective["args"]["tasks"][0])
        self.assertEqual(route["model"], "strong")
        self.assertEqual(route["fallback_providers"], [])
        self.assertEqual(args["tasks"][0]["route"], "routine")

    def test_simple_manual_and_child_turns_do_not_force_delegation(self):
        self.turn("rename foo to bar")
        self.assertIsNone(self.router.llm_request(request={"tools": self.tools}, session_id="one", turn_id="turn"))
        self.router.pre_turn(session_id="child", turn_id="c", parent_session_id="one", model="strong", user_message="security vulnerability")
        self.assertIsNone(self.router.llm_request(request={"tools": self.tools}, session_id="child", turn_id="c"))
        text = self.router.pre_turn(session_id="manual", turn_id="m", model="my-pinned-model", user_message="security vulnerability")
        self.assertIn("manual", text["context"].lower())
        self.assertIsNone(self.router.llm_request(request={"tools": self.tools}, session_id="manual", turn_id="m"))

    def test_sessions_turns_and_retries_are_isolated(self):
        self.turn("security vulnerability in authentication")
        self.router.pre_turn(session_id="two", turn_id="turn", model="small", user_message="hello")
        self.assertIsNone(self.router.llm_request(request={"tools": self.tools}, session_id="two", turn_id="turn"))
        first = self.router.llm_request(request={"tools": self.tools}, session_id="one", turn_id="turn")
        self.assertIsNotNone(first)
        self.assertIsNone(self.router.llm_request(request={"tools": self.tools}, session_id="one", turn_id="turn"))
        self.router.pre_turn(session_id="one", turn_id="next", model="small", user_message="hello")
        self.assertIsNone(self.router.llm_request(request={"tools": self.tools}, session_id="one", turn_id="next"))

    def test_backend_rejects_unknown_routes_and_never_exposes_arbitrary_models(self):
        with self.assertRaises(ValueError):
            self.router.route_task({"route": "paid-provider/expensive"})
        for role in ("routine", "specialist"):
            self.assertIn(self.router.route_task({"route": role})["model"], {"small", "strong"})

    def test_bad_policy_is_blocked_and_logs_do_not_save_prompts(self):
        with tempfile.TemporaryDirectory() as d:
            log = Path(d)/"events.jsonl"
            router = Router(self.policy, log)
            router.pre_turn(session_id="s", turn_id="t", model="small", user_message="secret user text")
            self.assertNotIn("secret user text", log.read_text())
            policy = copy.deepcopy(self.policy); policy["specialist"] = "missing/model"
            with self.assertRaises(ValueError):
                Router(policy)

    def test_plugins_load_through_real_hermes_discovery(self):
        try:
            runtime()
        except ValueError:
            self.skipTest("native Hermes integration test requires an installed runtime")
        with tempfile.TemporaryDirectory() as d:
            home = Path(d); (home/"autorouter").mkdir()
            policy = copy.deepcopy(self.policy)
            for row in policy["models"]:
                row.update(runtime_provider="autorouter-opencode-go",
                           auth_ref={"binary": "/unavailable-opencode", "account": "test", "organization": "workspace"})
            (home/"autorouter/policy.json").write_text(json.dumps(policy))
            root = Path(__file__).resolve().parents[1]
            for source,target in (("autorouter", "autorouter"), ("opencode-go", "model-providers/autorouter-opencode")):
                dst = home/"plugins"/target
                shutil.copytree(root/"integrations/hermes"/source, dst)
                shutil.copytree(root/"autorouter", dst/"routerlib", ignore=shutil.ignore_patterns("__pycache__"))
                shutil.copy2(root/"scripts/classify.py", dst/"routerlib/classifier.py")
            (home/"config.yaml").write_text(json.dumps({"plugins": {"enabled": ["autorouter"]}, "delegation": {"backend": "autorouter"}}))
            result = worker("plugin_check", home=home)
            self.assertTrue(result["loaded"])
            self.assertTrue(result["go_bridge"])
            self.assertTrue(result["bridge_known"])
            self.assertIn({"name": "autorouter", "available": True}, result["backends"])
