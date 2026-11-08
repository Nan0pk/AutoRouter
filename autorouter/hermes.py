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


def discover():
    return worker("discover")


def go_metadata(entry):
    costs = (entry.get("cost") or {}).get("text", {})
    capabilities = entry.get("capabilities") or {}
    return {"input_price": (costs.get("input") or {}).get("price"),
            "output_price": (costs.get("output") or {}).get("price"),
            "tools": (capabilities.get("tools") or {}).get("supported"),
            "reasoning": (capabilities.get("reasoning") or {}).get("supported")}


def native_discover():
    from providers import list_providers
    from hermes_cli.auth import get_auth_status, is_provider_explicitly_configured
    from agent import models_dev
    providers = []; rows = []; errors = []
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
                free = info.cost_input == 0 and info.cost_output == 0
                rows.append({"id": f"{profile.name}/{name}", "provider": profile.name, "model": name,
                             "runtime_provider": profile.name, "billing": "free" if free else "unknown",
                             "input_price": info.cost_input, "output_price": info.cost_output,
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
                quota = {"windows": [{"name": key, "used_percent": value.get("percent"),
                                       "reset_at": value.get("resetsAt")} for key,value in usage.items()],
                         "source": "https://opencode.ai/zen/go/v1/usage", "fetched_at": time.time()}
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
                rows.append({"id": "opencode-go/"+name, "provider": "opencode-go", "model": name,
                             "runtime_provider": "autorouter-opencode-go", "billing": billing,
                             **go_metadata(entry), "quota": quota,
                             "source": GO_URL+"/models", "fetched_at": time.time(), "auth_ref": auth_ref})
        except Exception:
            errors.append({"provider": "opencode-go", "reason": "OpenCode Go login, model list or allowance unavailable"})
    return {"providers": providers, "models": rows, "errors": errors, "discovered_at": time.time()}


if __name__ == "__main__":
    try:
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            payload = json.load(sys.stdin)
            if sys.argv[1] == "discover":
                result = native_discover()
            else:
                raise ValueError("Unknown Hermes helper action")
        print(json.dumps(result, allow_nan=False))
    except Exception:
        print(json.dumps({"error": "Hermes helper could not complete the requested operation"}))
        raise SystemExit(1)
