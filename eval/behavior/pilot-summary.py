"""Final grading for the pilot's completed summary."""
import os
import sys

sys.path.insert(0, os.getcwd())
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _harness import main  # noqa: E402

import summary  # noqa: E402


def summarizes_in_key_order():
    rows = [{"k": "z", "v": 3}, {"k": "a", "v": 1}, {"k": "drop", "v": 0},
            {"k": "a", "v": 4}, {"k": "z", "v": -1}]
    assert summary.summarize(rows) == [("a", 5), ("z", 2)]
    assert summary.summarize([]) == []


main([("summarizes_in_key_order", summarizes_in_key_order)])
