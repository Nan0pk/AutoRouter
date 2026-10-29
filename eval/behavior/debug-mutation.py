import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.getcwd())
from _harness import main  # noqa: E402

import aggregate  # noqa: E402


def zero_totals_are_dropped_without_raising():
    rows = [{"k": "a", "v": 1}, {"k": "b", "v": 0}, {"k": "a", "v": -1}]
    assert aggregate.total_by_key(rows) == {}


def normal_totals_are_kept():
    assert aggregate.total_by_key([{"k": "a", "v": 2}]) == {"a": 2}
    assert aggregate.total_by_key([{"k": "a", "v": 2}, {"k": "a", "v": 3}]) == {"a": 5}


main([
    ("zero_totals_are_dropped_without_raising", zero_totals_are_dropped_without_raising),
    ("normal_totals_are_kept", normal_totals_are_kept),
])
