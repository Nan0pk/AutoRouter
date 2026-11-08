"""Read a selected local login in memory. Never serialize returned credentials."""
import json
import math
import subprocess
import time
import urllib.request

GO_URL = "https://opencode.ai/inference/go/openai/v1"
CONSOLE = "https://opencode.ai/console"


def header(value):
    return isinstance(value, str) and bool(value) and value.isascii() and value.isprintable() and not any(c.isspace() for c in value)


def opencode_credential(binary="opencode", expected=None):
    try:
        p = subprocess.run([binary, "auth", "export", "opencode", "--standalone"],
                           stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                           stderr=subprocess.DEVNULL, text=True, timeout=30)
        rows = json.loads(p.stdout) if p.returncode == 0 else None
        if not isinstance(rows, list) or any(not isinstance(r, dict) or r.get("integrationID") != "opencode" for r in rows):
            raise ValueError
        active = [r for r in rows if r.get("active") is True]
        if len(active) != 1:
            raise ValueError
        row = active[0]; value = row["value"]
        organization = None
        if value["type"] == "oauth":
            expires = value.get("expires")
            if value.get("methodID") != "device" or value["metadata"].get("server") != CONSOLE:
                raise ValueError
            if type(expires) not in (int, float) or not math.isfinite(expires) or expires <= time.time()*1000:
                raise ValueError
            organization = value["metadata"]["orgID"]
            if not header(organization):
                raise ValueError
            token = value["access"]
        elif value["type"] == "key":
            token = value["key"]
        else:
            raise ValueError
        if not header(token) or not header(row.get("id")):
            raise ValueError
        result = {"token": token, "account": row["id"], "organization": organization}
        if expected and any(result[k] != expected.get(k) for k in ("account", "organization")):
            raise ValueError
        return result
    except (ValueError, TypeError, KeyError, OSError, subprocess.TimeoutExpired):
        raise ValueError("OpenCode login unavailable, expired, or changed; refresh it in OpenCode and rerun setup") from None


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def opencode_get(url, credential):
    if url not in (GO_URL + "/models", "https://opencode.ai/zen/go/v1/usage"):
        raise ValueError("Unexpected OpenCode metadata endpoint")
    headers = {"Authorization": "Bearer " + credential["token"], "User-Agent": "AutoRouter-Setup/0.1"}
    if credential["organization"]:
        headers["x-opencode-org-id"] = credential["organization"]
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.build_opener(NoRedirect).open(req, timeout=20) as response:
        return json.load(response)
