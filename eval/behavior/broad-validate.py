import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.getcwd())
from _harness import main  # noqa: E402

import a  # noqa: E402
import b  # noqa: E402
import c  # noqa: E402


def write(obj):
    fd, path = tempfile.mkstemp(suffix=".json")
    with os.fdopen(fd, "w") as f:
        json.dump(obj, f)
    return path


GOOD = write({"x": 1})
BAD = write([1, 2])


def all_three_modules_validate():
    for mod in (a, b, c):
        assert mod.load(GOOD) == {"x": 1}, mod.__name__
        try:
            mod.load(BAD)
        except ValueError:
            continue
        raise AssertionError(f"{mod.__name__}.load accepted a JSON list")


main([("all_three_modules_validate", all_three_modules_validate)])
