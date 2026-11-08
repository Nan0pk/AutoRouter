import hashlib
import unittest

from autorouter.verify import evaluate_handoff
from autorouter.policy import select_models
from test_setup_policy import model


class Verification(unittest.TestCase):
    def setUp(self):
        self.policy = select_models({"models": [model("go", "small"), model("go", "strong", price=2)]}, "subscriptions")
        self.output = "AR_CHECK_test:42"
        self.usage = {"completed": True, "model": "small", "provider": "go", "input_tokens": 100, "output_tokens": 20}
        self.events = [{"event": "api", "session_id": "root", "parent_session_id": "", "model": "small", "response_model": "small", "provider": "go", "usage": {"input_tokens": 10, "output_tokens": 5}},
                       {"event": "api", "session_id": "child", "parent_session_id": "root", "model": "strong", "response_model": "strong", "provider": "go", "usage": {"input_tokens": 20, "output_tokens": 5}},
                       {"event": "child_stop", "parent_session_id": "root", "child_session_id": "child", "child_status": "completed",
                        "result_sha256": hashlib.sha256(self.output.encode()).hexdigest()}]

    def test_two_real_routes_and_returned_child_result_are_required(self):
        result = evaluate_handoff(self.policy, self.events, self.usage, self.output)
        self.assertTrue(result["handoff_verified"])
        self.assertEqual(len(result["observed_models"]), 2)

    def test_skipped_child_wrong_model_missing_usage_and_empty_result_fail(self):
        import copy
        cases = []
        events = copy.deepcopy(self.events); events[1]["response_model"] = "wrong"; cases.append((events, self.usage, self.output))
        events = copy.deepcopy(self.events); events[1]["usage"] = {}; cases.append((events, self.usage, self.output))
        events = copy.deepcopy(self.events); events[2]["child_status"] = "skipped"; cases.append((events, self.usage, self.output))
        cases += [(self.events[:1], self.usage, self.output), (self.events, {**self.usage, "completed": False}, self.output),
                  (self.events, self.usage, ""), (self.events, self.usage, "parent answered something else")]
        for events,usage,output in cases:
            with self.subTest(events=events, output=output), self.assertRaises(ValueError):
                evaluate_handoff(self.policy, events, usage, output)
