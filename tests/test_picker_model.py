"""A visible routing profile must also produce a real, pinned inference request."""
from dataclasses import dataclass, field
import types
import sys
import unittest
from unittest.mock import patch

from autorouter.policy import select_models
from test_setup_policy import model


@dataclass
class Profile:
    name: str
    base_url: str = "https://provider.example/v1"
    api_mode: str = "chat_completions"
    aliases: tuple = ()
    display_name: str = ""
    env_vars: tuple = ()
    auth_type: str = "api_key"
    registry_auth_type: str = ""
    ambient_credentials: bool = False
    default_aux_model: str = ""
    fallback_models: tuple = ()
    model_aliases: dict = field(default_factory=dict)
    models_dev_id: str = ""
    hidden: bool = False
    picker_catalog: object = None
    default_headers: dict = field(default_factory=lambda: {"account": "pinned"})
    model_capabilities: dict = field(default_factory=dict)

    def build_api_kwargs_extras(self, *, model=None, **kwargs):
        return {}, {"reasoning_effort": "high"} if model == "small" else {}

    def build_extra_body(self, *, model=None, **kwargs):
        return {"upstream_model": model}

    def get_max_tokens(self, model):
        return 1234 if model == "small" else None


class PickerModel(unittest.TestCase):
    def profile(self):
        from autorouter.model_provider import make_profile
        policy = select_models({"models": [model("go", "small"), model("go", "large", price=3)]}, "all")
        return make_profile(policy, Profile("go"))

    def test_named_model_is_listed_and_stays_named_in_config(self):
        profile = self.profile()
        self.assertEqual(profile.name, "autorouter")
        self.assertEqual(profile.fetch_models(), ["AutoRouter-All"])
        self.assertEqual(profile.fallback_models, ("AutoRouter-All",))
        self.assertEqual(profile.normalize_model_id("AutoRouter-All"), "AutoRouter-All")

    def test_only_the_pinned_real_model_goes_on_the_wire_with_native_quirks(self):
        profile = self.profile()
        body, top = profile.build_api_kwargs_extras(model="AutoRouter-All")
        self.assertEqual(top, {"model": "small", "reasoning_effort": "high"})
        self.assertEqual(profile.build_extra_body(model="AutoRouter-All"), {"upstream_model": "small"})
        self.assertEqual(profile.get_max_tokens("AutoRouter-All"), 1234)
        with self.assertRaises(ValueError):
            profile.build_api_kwargs_extras(model="arbitrary-paid-model")

    def test_unsupported_routine_protocol_is_rejected(self):
        from autorouter.model_provider import make_profile
        policy = select_models({"models": [model("go", "small"), model("go", "large")]}, "all")
        with self.assertRaisesRegex(ValueError, "Chat Completions"):
            make_profile(policy, Profile("go", api_mode="anthropic_messages"))

    def test_credentials_delegate_to_selected_provider_without_exporting_them(self):
        runtime = types.SimpleNamespace(resolve_runtime_provider=lambda **kw: {
            "api_key": "local-test-secret", "base_url": "https://provider.example/v1", "api_mode": "chat_completions"})
        with patch.dict(sys.modules, {"hermes_cli.runtime_provider": runtime}):
            profile = self.profile()
            self.assertEqual(profile.resolve_api_key_secret()[0], "local-test-secret")
            self.assertEqual(profile.runtime_base_url(), "https://provider.example/v1")
            self.assertNotIn("local-test-secret", repr(profile))
            self.assertEqual(profile.seed_pool_credentials(), [])
            entry = types.SimpleNamespace(access_token="AutoRouter-All")
            self.assertEqual(profile.prepare_pool_entry(entry, "AutoRouter-All"), "")
            self.assertEqual(entry.access_token, "AutoRouter-All")
            sdk = types.SimpleNamespace(OpenAI=lambda **kwargs: kwargs)
            with patch.dict(sys.modules, {"openai": sdk}):
                client = profile.create_client(api_key="AutoRouter-All", base_url="https://wrong.example",
                                               default_headers={"account": "wrong", "session": "keep"})
            self.assertEqual(client["api_key"], "local-test-secret")
            self.assertEqual(client["base_url"], "https://provider.example/v1")
            self.assertEqual(client["default_headers"], {"account": "pinned", "session": "keep"})
