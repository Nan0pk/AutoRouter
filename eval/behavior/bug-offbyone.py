import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.getcwd())
from _harness import main  # noqa: E402

import paginator  # noqa: E402


def full_pages():
    items = [1, 2, 3, 4, 5]
    assert paginator.page(items, 2, 0) == [1, 2], paginator.page(items, 2, 0)
    assert paginator.page(items, 2, 1) == [3, 4], paginator.page(items, 2, 1)


def last_partial_page():
    assert paginator.page([1, 2, 3, 4, 5], 2, 2) == [5]


def out_of_range_is_empty():
    assert paginator.page([1, 2, 3], 2, 9) == []


main([
    ("full_pages", full_pages),
    ("last_partial_page", last_partial_page),
    ("out_of_range_is_empty", out_of_range_is_empty),
])
