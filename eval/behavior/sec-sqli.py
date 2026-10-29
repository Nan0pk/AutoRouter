import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.getcwd())
from _harness import main  # noqa: E402

import repo  # noqa: E402

HOSTILE = "' OR '1'='1"


class Cursor:
    def __init__(self):
        self.sql = None
        self.params = None

    def execute(self, sql, params=None):
        self.sql, self.params = sql, params

    def fetchall(self):
        return []


class Conn:
    def __init__(self):
        self.cur = Cursor()

    def cursor(self):
        return self.cur


def hostile_input_is_not_interpolated():
    conn = Conn()
    repo.find_user(conn, HOSTILE)
    sql = (conn.cur.sql or "").lower()
    assert HOSTILE.lower() not in sql, sql
    assert "or '1'='1" not in sql, sql


def hostile_input_is_passed_as_a_parameter():
    conn = Conn()
    repo.find_user(conn, HOSTILE)
    params = conn.cur.params
    assert params is not None, "query was executed with no parameters"
    assert HOSTILE in tuple(params), params


def query_still_targets_users():
    conn = Conn()
    repo.find_user(conn, "ada")
    assert "from users" in (conn.cur.sql or "").lower(), conn.cur.sql


main([
    ("hostile_input_is_not_interpolated", hostile_input_is_not_interpolated),
    ("hostile_input_is_passed_as_a_parameter", hostile_input_is_passed_as_a_parameter),
    ("query_still_targets_users", query_still_targets_users),
])
