import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.getcwd())
from _harness import main  # noqa: E402


def port_is_9090():
    with open("config.yaml") as f:
        lines = [ln.strip() for ln in f if ln.strip().startswith("port:")]
    assert lines == ["port: 9090"], lines


def config_still_loads():
    with open("config.yaml") as f:
        text = f.read()
    assert "host: localhost" in text and "workers: 4" in text, text


main([("port_is_9090", port_is_9090), ("config_still_loads", config_still_loads)])
