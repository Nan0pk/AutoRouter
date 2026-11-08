"""Bounded availability checks for shortlisted models; never probe the entire catalog."""
import copy
import json
import urllib.error
import urllib.request
import uuid

from .credentials import GO_URL, NoRedirect, opencode_credential
from .policy import select_models


def probe_model(row):
    if row["runtime_provider"] != "autorouter-opencode-go":
        return {"available": None, "reason": "Native model access will be checked by the installed handoff"}
    credential = opencode_credential(row["auth_ref"]["binary"], row["auth_ref"])
    headers = {"Authorization": "Bearer "+credential["token"], "User-Agent": "AutoRouter-Setup/0.1",
               "x-opencode-session": "ses_"+uuid.uuid4().hex, "Content-Type": "application/json"}
    if credential["organization"]:
        headers["x-opencode-org-id"] = credential["organization"]
    data = json.dumps({"model": row["model"], "messages": [{"role": "user", "content": "Reply OK."}], "max_tokens": 1}).encode()
    try:
        req = urllib.request.Request(GO_URL+"/chat/completions", data=data, headers=headers)
        with urllib.request.build_opener(NoRedirect).open(req, timeout=30) as response:
            result = json.load(response)
        if result.get("model") != row["model"] or not isinstance(result.get("usage"), dict):
            raise ValueError("Provider probe did not confirm the selected model and usage")
        return {"available": True, "usage": result["usage"]}
    except urllib.error.HTTPError as exc:
        if exc.code in (400, 403, 404):
            return {"available": False, "reason": f"Provider rejected this model/account/harness (HTTP {exc.code})"}
        raise ValueError(f"Provider availability check failed (HTTP {exc.code}); no alternate account or paid route used") from None
    except (OSError, ValueError) as exc:
        raise ValueError("Provider availability check failed; retry after checking the saved login and connection") from None


def select_usable(inventory, mode, providers=(), models=(), confirmed_allowances=(), probe=probe_model):
    inventory = copy.deepcopy(inventory); checked = {}; checks = []
    while len(checks) < 12:
        policy = select_models(inventory, mode, providers, models, confirmed_allowances)
        retry = False
        for identifier in (policy["routine"], policy["specialist"]):
            if identifier in checked:
                continue
            row = next(r for r in inventory["models"] if r["id"] == identifier)
            result = probe(row); checked[identifier] = result
            checks.append({"id": identifier, **result})
            if result["available"] is False:
                row.update(harness_supported=False, compatibility_reason=result["reason"])
                retry = True; break
        if not retry:
            policy["availability_checks"] = checks
            return policy
    raise ValueError("Could not find an accessible model pair within 12 checks; select specific known-working models")
