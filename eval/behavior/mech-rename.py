import os
import sys

sys.path.insert(0, os.getcwd())
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _harness import main  # noqa: E402

import app  # noqa: E402


def renamed_api_works():
    account = app.Account("ada")
    assert app.get_user(user=account) == "ada"


main([("renamed_api_works", renamed_api_works)])
