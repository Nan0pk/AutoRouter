import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.getcwd())
from _harness import main  # noqa: E402

import cache  # noqa: E402


def invalidate_removes_the_key():
    c = cache.Cache()
    c.put("a", 1)
    assert c.get("a") == 1
    c.invalidate("a")
    assert c.get("a") is None, c.get("a")


def invalidate_missing_key_is_safe():
    cache.Cache().invalidate("nope")


def other_keys_survive():
    c = cache.Cache()
    c.put("a", 1)
    c.put("b", 2)
    c.invalidate("a")
    assert c.get("b") == 2


main([
    ("invalidate_removes_the_key", invalidate_removes_the_key),
    ("invalidate_missing_key_is_safe", invalidate_missing_key_is_safe),
    ("other_keys_survive", other_keys_survive),
])
