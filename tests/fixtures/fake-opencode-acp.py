#!/usr/bin/python3
"""Offline fake that speaks the subset of OpenCode v2 ACP used by the pilot."""
import json
import sys
from pathlib import Path


def respond(request, result):
    print(json.dumps({"jsonrpc": "2.0", "id": request["id"], "result": result}), flush=True)


def edit_files(model, prompt):
    summary = Path("summary.py")
    aggregate = Path("aggregate.py")
    if model == "test/specialist":
        assert "Prior public check:" in prompt
        assert "RuntimeError" in prompt
        assert "return list" in summary.read_text()
        aggregate.write_text(
            "def total_by_key(rows):\n"
            "    totals = {}\n"
            "    for row in rows:\n"
            "        key = row['k']\n"
            "        totals[key] = totals.get(key, 0) + row['v']\n"
            "    return {k: v for k, v in totals.items() if v}\n"
        )
    elif prompt.startswith("Draft"):
        summary.write_text("from aggregate import total_by_key\n\ndef summarize(rows):\n"
                           "    return list(total_by_key(rows).items())\n")
    elif prompt.startswith("The aggregator passed"):
        assert "return {k: v" in aggregate.read_text()
        summary.write_text("from aggregate import total_by_key\n\ndef summarize(rows):\n"
                           "    return sorted(total_by_key(rows).items())\n")


assert sys.argv[1:] == ["acp"]
for line in sys.stdin:
    request = json.loads(line)
    method = request["method"]
    params = request["params"]
    if method == "initialize":
        respond(request, {"protocolVersion": 1})
    elif method == "session/new":
        assert params["cwd"] == str(Path.cwd()) == "/work"
        respond(request, {"sessionId": "test-session"})
    elif method == "session/set_config_option":
        model = params["value"]
        options = [{"id": "model", "currentValue": model}]
        if model.endswith("/fast"):
            options = [{"id": "model", "currentValue": model.removesuffix("/fast")},
                       {"id": "effort", "currentValue": "fast"}]
        respond(request, {"configOptions": options})
    elif method == "session/prompt":
        edit_files(model, params["prompt"][0]["text"])
        respond(request, {"stopReason": "end_turn",
                          "usage": {"inputTokens": 12, "outputTokens": 3}})
    else:
        raise AssertionError(method)
