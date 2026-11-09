"""Publish the configured router as a named Hermes model, without a proxy server."""
from dataclasses import fields

from .policy import PROFILE_NAMES, normalize_mode


def model_name(policy):
    return PROFILE_NAMES[normalize_mode(policy["mode"])]


def make_profile(policy, base):
    routine = next(r for r in policy["models"] if r["id"] == policy["routine"])
    name = model_name(policy)
    if base.api_mode != "chat_completions":
        raise ValueError("The named AutoRouter picker currently requires a Chat Completions routine model")

    def wire_model(model):
        if model not in (None, "", name, routine["model"]):
            raise ValueError("This AutoRouter profile only accepts its configured model")
        return routine["model"]

    def runtime():
        from hermes_cli.runtime_provider import resolve_runtime_provider
        result = resolve_runtime_provider(requested=routine["runtime_provider"], target_model=routine["model"])
        if result["api_mode"] != "chat_completions":
            raise ValueError("The routine provider does not offer the required native Chat Completions transport")
        return result

    class RoutedModelProfile(type(base)):
        def fetch_models(self, **kwargs):
            return [name]

        def normalize_model_id(self, model, **kwargs):
            # Keep the virtual id in settings and the picker. Only the API request is mapped.
            return name if model.lower() in (name.lower(), "auto") else None

        def resolve_api_key_secret(self):
            return runtime()["api_key"], "autorouter-selected-provider"

        def seed_pool_credentials(self, **kwargs):
            # The picker marker must never be ingested as an API credential.
            return []

        def prepare_pool_entry(self, entry, api_key):
            # Decline every pool row, including a marker ingested by an older build.
            # Hermes then uses resolve_api_key_secret above for this explicit provider.
            return ""

        def runtime_base_url(self, **kwargs):
            return runtime()["base_url"]

        def api_mode_for_model(self, model, **kwargs):
            wire_model(model)
            return "chat_completions"

        def create_client(self, **kwargs):
            # Hermes' live env watcher can mistake the picker marker for a changed key.
            # Retain its normal HTTP options, but bind every client to the saved login
            # and account headers. Use the native SDK so raw response.model is preserved.
            from openai import OpenAI
            route = runtime()
            kwargs.update(api_key=route["api_key"], base_url=route["base_url"],
                          default_headers={**(kwargs.get("default_headers") or {}), **dict(base.default_headers)})
            return OpenAI(**kwargs)

        def build_api_kwargs_extras(self, *, model=None, **kwargs):
            actual = wire_model(model)
            body, top = base.build_api_kwargs_extras(model=actual, **kwargs)
            return body, {**top, "model": actual}

        def build_extra_body(self, *, model=None, **kwargs):
            return base.build_extra_body(model=wire_model(model), **kwargs)

        def get_max_tokens(self, model):
            return base.get_max_tokens(wire_model(model))

        def get_model_context_length(self, model):
            return base.get_model_context_length(wire_model(model))

        def default_reasoning_config(self, model=None):
            return base.default_reasoning_config(wire_model(model))

        def request_headers(self, *, model="", **kwargs):
            return base.request_headers(model=wire_model(model), **kwargs)

        def max_tokens_param(self, *, model="", **kwargs):
            return base.max_tokens_param(model=wire_model(model), **kwargs)

        def fetch_account_usage(self, **kwargs):
            # The underlying provider owns its account-specific usage and auth handling.
            return base.fetch_account_usage(**kwargs)

    values = {f.name: getattr(base, f.name) for f in fields(base) if f.init}
    values.update(name="autorouter", display_name="AutoRouter", aliases=(), api_mode="chat_completions",
                  env_vars=("AUTOROUTER_ACTIVE_PROFILE",), auth_type="api_key",
                  registry_auth_type="api_key", ambient_credentials=True,
                  default_aux_model=routine["model"], fallback_models=(name,),
                  model_aliases={"auto": name}, models_dev_id="", hidden=False,
                  picker_catalog=lambda force_refresh=False: [name])
    # Copy capabilities, never invent a new context window or mark unknown prices as zero.
    capabilities = dict(base.model_capabilities.get(routine["model"], {}))
    capabilities.update(supports_tools=routine.get("tools") is True, supports_reasoning=routine.get("reasoning") is True)
    try:
        from agent.models_dev import get_model_info
        info = get_model_info(base.models_dev_id or base.name, routine["model"], allow_network=False)
        if info:
            capabilities.update(context_window=info.context_window, max_output_tokens=info.max_output)
    except (ImportError, AttributeError):
        pass
    values["model_capabilities"] = {name: capabilities}
    return RoutedModelProfile(**values)


def register_routing_model():
    import os
    from providers import get_provider_profile, register_provider
    from .plugin import read_policy
    policy = read_policy()
    routine = next(r for r in policy["models"] if r["id"] == policy["routine"])
    base = get_provider_profile(routine["runtime_provider"])
    if base is None or routine["runtime_provider"] == "autorouter":
        raise ValueError("The configured AutoRouter routine provider is unavailable")
    register_provider(make_profile(policy, base))
    # Hermes' picker tests env presence, rather than the provider-owned secret resolver.
    # This is only a visibility marker, never a credential. The resolver above always
    # returns the pinned native login or raises; it cannot fall back to this marker.
    os.environ["AUTOROUTER_ACTIVE_PROFILE"] = model_name(policy)
