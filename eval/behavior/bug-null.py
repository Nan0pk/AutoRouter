import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.getcwd())
from _harness import main  # noqa: E402

import user  # noqa: E402


def missing_user_does_not_raise():
    assert user.display_name(None) in ("", None), user.display_name(None)


def present_user_still_works():
    class Profile:
        name = "ada"

    class Account:
        profile = Profile()

    assert user.display_name(Account()) == "ada"


main([
    ("missing_user_does_not_raise", missing_user_does_not_raise),
    ("present_user_still_works", present_user_still_works),
])
