"""Native provider bridge to the existing OpenCode Console login."""
from .credentials import GO_URL, opencode_credential


def register_provider_bridge():
    from providers import get_provider_profile, register_provider
    from .plugin import read_policy
    policy = read_policy()
    rows = [r for r in policy["models"] if r["runtime_provider"] == "autorouter-opencode-go"]
    if not rows:
        return
    references = [r["auth_ref"] for r in rows]
    if any(r != references[0] for r in references):
        raise ValueError("AutoRouter OpenCode account references conflict; rerun setup")
    expected = references[0]
    base = get_provider_profile("opencode-go")
    if base is None:
        raise ValueError("Hermes OpenCode Go provider support is required")

    class ExistingOpenCodeLogin(type(base)):
        def resolve_api_key_secret(self):
            # Raise on error: returning None would permit unrelated env/pool credentials.
            credential = opencode_credential(expected["binary"], expected)
            return credential["token"], "autorouter-existing-opencode-login"

        def runtime_base_url(self, **kwargs):
            return GO_URL

    headers = {"User-Agent": "AutoRouter-Hermes/0.1"}
    if expected["organization"]:
        headers["x-opencode-org-id"] = expected["organization"]
    profile = ExistingOpenCodeLogin(name="autorouter-opencode-go", base_url=GO_URL,
                                   env_vars=(), ambient_credentials=True, default_headers=headers,
                                   default_aux_model=next(r["model"] for r in rows if r["id"] == policy["routine"])
                                   if policy["routine"] in {r["id"] for r in rows} else rows[0]["model"])
    register_provider(profile)
