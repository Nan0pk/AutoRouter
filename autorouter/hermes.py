"""Small version-bound bridge to the installed Hermes runtime; no new dependencies."""
import contextlib
import io
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

from .credentials import GO_URL, opencode_credential, opencode_get
from .policy import price


def runtime():
    source = Path(os.environ.get("AUTOROUTER_HERMES_SOURCE", Path.home()/".hermes/hermes-agent")).resolve()
    home = source.parent
    import hashlib
    facts = home/"installs"/hashlib.sha256(str(source).encode()).hexdigest()[:16]/"facts.json"
    try:
        venv = Path(json.loads(facts.read_text())["packages"]["venv"]["environment"]).resolve()
        if venv.parent.parent != facts.parent/"environments" or venv.name != "venv":
            raise ValueError
        python = venv/"bin/python"
        if not python.is_file():
            raise ValueError
    except (OSError, ValueError, KeyError, TypeError):
        python = next((source/p for p in ("venv/bin/python", ".venv/bin/python") if (source/p).is_file()), None)
        if python is None:
            raise ValueError("A working Hermes source installation is required; set AUTOROUTER_HERMES_SOURCE for a custom install") from None
    return source, python


def worker(action, payload=None, home=None, timeout=180):
    source, python = runtime()
    package = Path(__file__).resolve().parent.parent
    code = f"import sys,runpy; sys.path[:0]=[{str(package)!r},{str(source)!r}]; runpy.run_module('autorouter.hermes',run_name='__main__')"
    env = {**os.environ, "HERMES_DISABLE_LAZY_INSTALLS": "1"}
    if home is not None:
        env["HERMES_HOME"] = str(Path(home).absolute())
    p = subprocess.run([str(python), "-I", "-c", code, action], input=json.dumps(payload or {}),
                       capture_output=True, text=True, env=env, timeout=timeout)
    try:
        result = json.loads(p.stdout)
    except ValueError:
        raise ValueError("Hermes helper failed; check that the installed Hermes runtime works") from None
    if p.returncode or "error" in result:
        raise ValueError(result.get("error", "Hermes helper failed"))
    return result


def discover(home=None):
    return worker("discover", home=home)


def go_metadata(entry):
    costs = (entry.get("cost") or {}).get("text", {})
    capabilities = entry.get("capabilities") or {}
    return {"input_price": (costs.get("input") or {}).get("price"),
            "output_price": (costs.get("output") or {}).get("price"),
            "tools": (capabilities.get("tools") or {}).get("supported"),
            "reasoning": (capabilities.get("reasoning") or {}).get("supported")}


def raw_native_prices(entry):
    costs = entry.get("cost") if isinstance(entry, dict) else None
    costs = costs if isinstance(costs, dict) else {}
    return {"input_price": costs.get("input") if price(costs.get("input")) else None,
            "output_price": costs.get("output") if price(costs.get("output")) else None}


def go_allowance(usage):
    from datetime import datetime
    if not isinstance(usage, dict):
        return None
    windows = []
    for name in ("rolling", "weekly", "monthly"):
        if name not in usage:
            continue
        value = usage[name]
        if not isinstance(value, dict) or not price(value.get("percent")):
            return None
        try:
            reset = datetime.fromisoformat(value["resetsAt"].replace("Z", "+00:00"))
            if reset.tzinfo is None:
                return None
        except (KeyError, TypeError, AttributeError, ValueError):
            return None
        windows.append({"name": name, "used_percent": value["percent"], "reset_at": value["resetsAt"]})
    return {"windows": windows, "source": "https://opencode.ai/zen/go/v1/usage", "fetched_at": time.time()} if windows else None


def native_discover():
    from providers import list_providers
    from hermes_cli.auth import get_auth_status, is_provider_explicitly_configured
    from agent import models_dev
    providers = []; rows = []; errors = []
    try:
        if not models_dev._get_cache_path().exists() or time.time()-models_dev._get_cache_path().stat().st_mtime > 86400:
            models_dev.fetch_models_dev(force_refresh=True)
    except Exception:
        pass  # retain the real stale timestamp; free selection will reject it
    catalog = models_dev.fetch_models_dev(allow_network=True)
    for profile in list_providers():
        if profile.name.startswith("autorouter-"):
            continue
        try:
            if not is_provider_explicitly_configured(profile.name) or not get_auth_status(profile.name).get("logged_in"):
                continue
            quota = None
            try:
                usage = profile.fetch_account_usage()
                if usage:
                    quota = {"windows": [{"used_percent": w.used_percent,
                                           "reset_at": str(w.reset_at) if w.reset_at else None}
                                          for w in usage.windows], "source": usage.source}
            except Exception:
                pass
            providers.append({"id": profile.name, "billing": "unknown", "quota": quota,
                              "source": "Hermes saved login", "note": "Included allowance needs confirmation when billing cannot be established."})
            for name in models_dev.list_agentic_models(profile.name, allow_network=True):
                info = models_dev.get_model_info(profile.name, name, allow_network=False)
                if not info:
                    continue
                try:
                    fetched = models_dev._get_cache_path().stat().st_mtime
                except OSError:
                    fetched = 0
                catalog_provider = models_dev.models_dev_id_for(profile.name) or profile.name
                raw = catalog.get(catalog_provider, {}).get("models", {}).get(info.id, {})
                prices = raw_native_prices(raw)
                free = all(price(prices[k]) and prices[k] == 0 for k in prices)
                rows.append({"id": f"{profile.name}/{name}", "provider": profile.name, "model": name,
                             "runtime_provider": profile.name, "billing": "free" if free else "unknown",
                             **prices,
                             "tools": info.tool_call, "reasoning": info.reasoning,
                             "quota": quota, "source": "https://models.dev/api.json", "fetched_at": fetched})
        except Exception:
            errors.append({"provider": profile.name, "reason": "Saved login or model discovery unavailable"})
    binary = shutil.which("opencode")
    if binary:
        try:
            credential = opencode_credential(binary)
            data = opencode_get(GO_URL+"/models", credential)
            quota = None
            try:
                usage = opencode_get("https://opencode.ai/zen/go/v1/usage", credential)["usage"]
                quota = go_allowance(usage)
            except Exception:
                pass
            billing = "included" if quota and quota["windows"] else "unknown"
            auth_ref = {"binary": str(Path(binary).resolve()), "account": credential["account"],
                        "organization": credential["organization"]}
            providers = [p for p in providers if p["id"] != "opencode-go"]
            rows = [r for r in rows if r["provider"] != "opencode-go"]
            providers.append({"id": "opencode-go", "billing": billing, "quota": quota, "source": GO_URL+"/models"})
            for entry in data["data"]:
                name = entry["id"]; costs = entry.get("cost") or {}; capabilities = entry.get("capabilities") or {}
                metadata = go_metadata(entry)
                harness_supported = not (metadata["input_price"] == 0 and metadata["output_price"] == 0)
                rows.append({"id": "opencode-go/"+name, "provider": "opencode-go", "model": name,
                             "runtime_provider": "autorouter-opencode-go", "billing": billing,
                             **metadata, "quota": quota, "harness_supported": harness_supported,
                             "compatibility_reason": "OpenCode zero-price chat models require OpenCode, not external harnesses",
                             "source": GO_URL+"/models", "fetched_at": time.time(), "auth_ref": auth_ref})
        except Exception:
            errors.append({"provider": "opencode-go", "reason": "OpenCode Go login, model list or allowance unavailable"})
    return {"providers": providers, "models": rows, "errors": errors, "discovered_at": time.time()}


def native_plugin_check():
    from hermes_cli.plugins import discover_plugins, get_plugin_manager
    discover_plugins()
    from agent.delegation_backends import list_backends
    from providers import get_provider_profile
    backends = [{"name": b.name, "available": b.available} for b in list_backends()]
    bridge = get_provider_profile("autorouter-opencode-go")
    routing = get_provider_profile("autorouter")
    from hermes_cli.auth import resolve_provider
    try:
        bridge_known = resolve_provider("autorouter-opencode-go") == "autorouter-opencode-go"
    except Exception:
        bridge_known = False
    return {"backends": backends, "go_bridge": bool(bridge), "bridge_known": bridge_known,
            "picker_models": routing.fetch_models() if routing else [],
            "wire_model": routing.build_api_kwargs_extras(model=routing.fallback_models[0])[1]["model"] if routing else None,
            "loaded": "autorouter" in get_plugin_manager()._plugins}


def native_picker_check():
    from hermes_cli.plugins import discover_plugins
    discover_plugins()
    from hermes_cli.inventory import build_model_options_payload, load_picker_context
    from hermes_cli.model_switch import switch_model
    context = load_picker_context()
    payload = build_model_options_payload(context, include_unconfigured=False)
    row = next((r for r in payload["providers"] if r.get("slug") == "autorouter"), None)
    from .plugin import read_policy
    from .model_provider import model_name
    name = model_name(read_policy())
    selection = switch_model(raw_input=name, explicit_provider="autorouter", is_global=False,
                             current_provider=context.current_provider, current_model=context.current_model,
                             current_base_url=context.current_base_url, current_api_key="",
                             user_providers=context.user_providers, custom_providers=context.custom_providers)
    return {"provider": "autorouter", "models": row.get("models", []) if row else [],
            "selected_model": selection.new_model, "selection_succeeded": selection.success,
            "selection_error": selection.error_message if not selection.success else None}


def native_probe(row):
    from hermes_cli.runtime_provider import resolve_runtime_provider
    from agent.auxiliary_client import resolve_provider_client
    try:
        route = resolve_runtime_provider(requested=row["runtime_provider"], target_model=row["model"])
        client, actual = resolve_provider_client(
            row["runtime_provider"], model=row["model"], main_runtime=route,
            explicit_base_url=route["base_url"], explicit_api_key=route["api_key"], api_mode=route["api_mode"])
        if client is None or actual != row["model"]:
            return {"available": False, "reason": "Native provider could not open the pinned model"}
        client.chat.completions.create(model=actual, messages=[{"role": "user", "content": "Reply OK."}],
                                       max_tokens=1, timeout=30)
        return {"available": True}
    except Exception as exc:
        status = getattr(exc, "status_code", None)
        if status in (400, 401, 402, 403, 404):
            return {"available": False, "reason": f"Native provider rejected the pinned model (HTTP {status})"}
        raise ValueError("Native provider availability check failed; check the saved login and connection") from None


def native_render_config(payload):
    from ruamel.yaml import YAML
    from hermes_cli.config_defaults import DEFAULT_CONFIG
    from hermes_cli.config_version_stamp import read_config_version_stamp
    yaml = YAML(); config = yaml.load(payload["text"]) or {}
    if not isinstance(config, dict):
        raise ValueError("Hermes configuration must be a mapping")
    policy = payload["policy"]
    from providers import get_provider_profile
    from .model_provider import make_profile
    routine = next(r for r in policy["models"] if r["id"] == policy["routine"])
    base = get_provider_profile(routine["runtime_provider"])
    if base is None and routine["runtime_provider"] == "autorouter-opencode-go":
        base = get_provider_profile("opencode-go")
    if base is None:
        raise ValueError("The configured routine provider is unavailable")
    make_profile(policy, base)  # reject unsupported routine transports before any activation
    config["_config_version"] = read_config_version_stamp()[1]
    row = next(r for r in policy["models"] if r["id"] == policy["routine"])
    from .model_provider import model_name
    model = config.setdefault("model", {})
    if not isinstance(model, dict):
        raise ValueError("Hermes model configuration must be a mapping")
    model.update(provider="autorouter", default=model_name(policy), base_url="", api_key="", default_headers={})
    model.pop("auth_mode", None)
    for key in ("fallback_models", "fallback_providers", "fallback_chain"):
        config[key] = []
    config["fallback_model"] = []
    config["smart_model_routing"] = {"enabled": False}
    delegation = config.setdefault("delegation", {})
    delegation.update(backend="autorouter", provider=row["runtime_provider"], model=row["model"],
                      base_url="", api_key="", api_mode="", fallback_providers=[])
    auxiliary = config.setdefault("auxiliary", {})
    auxiliary["free_only"] = policy["mode"] == "free"
    for key in set(DEFAULT_CONFIG["auxiliary"]) | set(auxiliary):
        default = DEFAULT_CONFIG["auxiliary"].get(key)
        if not isinstance(default, dict) and not isinstance(auxiliary.get(key), dict):
            continue
        block = auxiliary.setdefault(key, {})
        if not isinstance(block, dict):
            raise ValueError("Hermes auxiliary configuration must be a mapping")
        block.update(provider=row["runtime_provider"], model=row["model"], base_url="", api_key="",
                     api_mode="", fallback_chain=[], extra_body={}, prefer_fast_model=False)
    auxiliary.setdefault("title_generation", {}).update(enabled=False, model_upgrade_enabled=False)
    plugins = config.setdefault("plugins", {})
    enabled = plugins.setdefault("enabled", [])
    if "autorouter" not in enabled:
        enabled.append("autorouter")
    if "autorouter" in plugins.get("disabled", []):
        plugins["disabled"].remove("autorouter")
    for toolsets in config.get("platform_toolsets", {}).values():
        if isinstance(toolsets, list) and "delegation" not in toolsets:
            toolsets.append("delegation")
    if "delegation" in config.get("disabled_toolsets", []):
        config["disabled_toolsets"].remove("delegation")
    out = io.StringIO(); yaml.dump(config, out)
    return {"text": out.getvalue()}


if __name__ == "__main__":
    try:
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            payload = json.load(sys.stdin)
            if sys.argv[1] == "discover":
                result = native_discover()
            elif sys.argv[1] == "plugin_check":
                result = native_plugin_check()
            elif sys.argv[1] == "picker_check":
                result = native_picker_check()
            elif sys.argv[1] == "probe":
                result = native_probe(payload)
            elif sys.argv[1] == "render_config":
                result = native_render_config(payload)
            else:
                raise ValueError("Unknown Hermes helper action")
        print(json.dumps(result, allow_nan=False))
    except Exception:
        print(json.dumps({"error": "Hermes helper could not complete the requested operation"}))
        raise SystemExit(1)
