import ast
import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.getcwd())
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
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


def unused_import_removed():
    tree = ast.parse(Path(util.__file__).read_text())
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            assert all(alias.name != "os" for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            assert node.module != "os"


main([("load_still_works", load_still_works),
      ("unused_import_removed", unused_import_removed)])
