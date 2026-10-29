import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.getcwd())
from _harness import main  # noqa: E402

import calc  # noqa: E402
import test_calc  # noqa: E402


def test_module_passes():
    test_calc.test_add()


def addition_is_addition():
    assert calc.add(2, 2) == 4, calc.add(2, 2)
    assert calc.add(-1, 1) == 0, calc.add(-1, 1)


main([("test_module_passes", test_module_passes), ("addition_is_addition", addition_is_addition)])
