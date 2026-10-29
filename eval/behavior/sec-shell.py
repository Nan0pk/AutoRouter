import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.getcwd())
from _harness import main  # noqa: E402

import archive  # noqa: E402

HOSTILE = "x; touch /tmp/pwned"


def captures_call():
    calls = {}

    class Fake:
        @staticmethod
        def run(args, **kwargs):
            calls["args"] = args
            calls["kwargs"] = kwargs

            class Done:
                returncode = 0

            return Done()

    original = subprocess.run
    subprocess.run = Fake.run
    try:
        archive.extract(HOSTILE, "/tmp/dest")
    finally:
        subprocess.run = original
    return calls


def shell_is_not_enabled():
    calls = captures_call()
    assert not calls["kwargs"].get("shell"), calls["kwargs"]


def arguments_are_a_vector_not_a_string():
    calls = captures_call()
    assert isinstance(calls["args"], (list, tuple)), calls["args"]


def hostile_input_stays_one_argument():
    calls = captures_call()
    assert HOSTILE in calls["args"], calls["args"]


main([
    ("shell_is_not_enabled", shell_is_not_enabled),
    ("arguments_are_a_vector_not_a_string", arguments_are_a_vector_not_a_string),
    ("hostile_input_stays_one_argument", hostile_input_stays_one_argument),
])
