import copy
import json
import os
import shutil
import subprocess
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

    def test_selecting_virtual_model_activates_routing(self):
        self.router.pre_turn(session_id="one", turn_id="turn", model="AutoRouter-All",
                             user_message="security vulnerability in authentication")
        self.assertIsNotNone(self.router.llm_request(request={"tools": self.tools}, session_id="one", turn_id="turn"))

    def test_physical_routine_model_selected_manually_disables_the_new_router(self):
        router = Router({**self.policy, "picker_model": "AutoRouter-All"})
        result = router.pre_turn(session_id="s", turn_id="t", model="small",
                                 user_message="security vulnerability in authentication")
        self.assertIn("manual", result["context"].lower())
        self.assertIsNone(router.llm_request(request={"tools": self.tools}, session_id="s", turn_id="t"))

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
        self.assertIsNotNone(self.router.llm_request(request={"tools": self.tools}, session_id="one", turn_id="turn"))
        self.router.tool_request("delegate_task", {"tasks": [{"goal": "work"}]}, session_id="one", turn_id="turn")
        self.assertIsNone(self.router.llm_request(request={"tools": self.tools}, session_id="one", turn_id="turn"))
        self.router.pre_turn(session_id="one", turn_id="next", model="small", user_message="hello")
        self.assertIsNone(self.router.llm_request(request={"tools": self.tools}, session_id="one", turn_id="next"))

    def test_free_rows_expire_from_original_catalog_time_in_every_mode(self):
        for mode in ("all", "provider", "models", "free"):
            rows = [model("free", "one", "free", 0), model("free", "two", "free", 0)]
            kwargs = {"providers": ["free"]} if mode == "provider" else {"models": [r["id"] for r in rows]} if mode == "models" else {}
            policy = select_models({"models": rows}, mode, **kwargs)
            for row in policy["models"]:
                row["fetched_at"] = 0
            router = Router(policy)
            result = router.pre_turn(session_id="s", turn_id="t", model="one", user_message="hi")
            self.assertEqual(result["action"], "block")
            with self.assertRaises(ValueError):
                router.route_task({"route": "specialist"})

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
            self.assertEqual(result["picker_models"], ["AutoRouter-All"])
            self.assertEqual(result["wire_model"], "small")
            self.assertIn({"name": "autorouter", "available": True}, result["backends"])
            source, python = runtime()
            code = '''import sys,json,io,contextlib
sys.path.insert(0,sys.argv[1])
with contextlib.redirect_stdout(io.StringIO()),contextlib.redirect_stderr(io.StringIO()):
    import httpx
    from unittest.mock import patch
    from providers import get_provider_profile
    profile=get_provider_profile("autorouter")
    seen={}
    def response(request):
        seen["workspace"]=request.headers.get("x-opencode-org-id")
        seen["model"]=json.loads(request.content)["model"]
        return httpx.Response(200,json={"id":"test","object":"chat.completion","created":0,"model":"different-upstream-model",
            "choices":[{"index":0,"message":{"role":"assistant","content":"ok"},"finish_reason":"stop"}]})
    with httpx.Client(transport=httpx.MockTransport(response)) as http:
        route={"api_key":"fake-local-key","base_url":"https://example.invalid/v1","api_mode":"chat_completions"}
        with patch("hermes_cli.runtime_provider.resolve_runtime_provider",return_value=route):
            client=profile.create_client(api_key="AutoRouter-All",base_url="https://wrong.invalid",default_headers={"x-opencode-org-id":"wrong"},http_client=http)
        body,top=profile.build_api_kwargs_extras(model="AutoRouter-All")
        result=client.chat.completions.create(**top,messages=[{"role":"user","content":"ok"}],max_tokens=1)
        assert seen == {"workspace":"workspace","model":"small"},seen
        assert client.api_key == "fake-local-key"
        assert result.model == "different-upstream-model",result.model
print("pinned headers and raw response model preserved")
'''
            captured = subprocess.run([str(python), "-I", "-c", code, str(source)],
                                      env={**os.environ, "HERMES_HOME": str(home), "HERMES_DISABLE_LAZY_INSTALLS": "1"},
                                      capture_output=True, text=True, timeout=30)
            self.assertEqual(captured.returncode, 0, captured.stderr)
            self.assertIn("raw response model preserved", captured.stdout)
