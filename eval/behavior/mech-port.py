import os
import sys

sys.path.insert(0, os.getcwd())
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _harness import main  # noqa: E402


def port_is_9090():
    with open("config.yaml") as f:
        settings = [line.split("#", 1)[0].strip().split(":", 1) for line in f]
    ports = [value.strip() for key, *values in settings for value in values
             if key.strip() == "port"]
    assert ports == ["9090"], ports


def config_still_loads():
    with open("config.yaml") as f:
        text = f.read()
    assert "host: localhost" in text and "workers: 4" in text, text


main([("port_is_9090", port_is_9090), ("config_still_loads", config_still_loads)])
