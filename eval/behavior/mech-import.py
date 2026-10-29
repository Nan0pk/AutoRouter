import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.getcwd())
from _harness import main  # noqa: E402

import util  # noqa: E402


def load_still_works():
    fd, path = tempfile.mkstemp(suffix=".json")
    with os.fdopen(fd, "w") as f:
        json.dump({"a": 1}, f)
    try:
        assert util.load(path) == {"a": 1}
    finally:
        os.unlink(path)


main([("load_still_works", load_still_works)])
