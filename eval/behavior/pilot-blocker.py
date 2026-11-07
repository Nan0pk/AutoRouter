"""Public reproduction used to decide whether the pilot needs a specialist."""
import os
import sys

sys.path.insert(0, os.getcwd())
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _harness import main  # noqa: E402

import aggregate  # noqa: E402


def drops_zero_total():
    assert aggregate.total_by_key([{"k": "a", "v": 2}, {"k": "a", "v": -2}]) == {}


main([("drops_zero_total", drops_zero_total)])
