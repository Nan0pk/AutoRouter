import os
import sys
import tempfile

sys.path.insert(0, os.getcwd())
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _harness import main  # noqa: E402

import files  # noqa: E402


def sibling_file_cannot_be_read():
    base = tempfile.mkdtemp()
    outside = tempfile.mkdtemp()
    with open(os.path.join(base, "ok.txt"), "w") as f:
        f.write("ok")
    with open(os.path.join(outside, "secret.txt"), "w") as f:
        f.write("SECRET")
    assert files.read_file(base, "ok.txt") == "ok"
    relative_escape = os.path.relpath(os.path.join(outside, "secret.txt"), base)
    try:
        leaked = files.read_file(base, relative_escape)
    except Exception:
        return
    assert "SECRET" not in leaked, leaked


def absolute_path_cannot_be_read():
    base = tempfile.mkdtemp()
    outside = tempfile.mkdtemp()
    with open(os.path.join(outside, "secret.txt"), "w") as f:
        f.write("SECRET")
    try:
        leaked = files.read_file(base, os.path.join(outside, "secret.txt"))
    except Exception:
        return
    assert "SECRET" not in leaked, leaked


def symlink_cannot_escape_base():
    with tempfile.TemporaryDirectory() as root:
        base = os.path.join(root, "base")
        os.mkdir(base)
        outside = os.path.join(root, "secret.txt")
        with open(outside, "w") as f:
            f.write("SECRET")
        os.symlink(outside, os.path.join(base, "link"))
        try:
            leaked = files.read_file(base, "link")
        except Exception:
            return
        assert leaked is None or "SECRET" not in leaked, leaked


main([
    ("sibling_file_cannot_be_read", sibling_file_cannot_be_read),
    ("absolute_path_cannot_be_read", absolute_path_cannot_be_read),
    ("symlink_cannot_escape_base", symlink_cannot_escape_base),
])
