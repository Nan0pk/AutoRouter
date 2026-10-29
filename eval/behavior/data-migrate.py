import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.getcwd())
from _harness import main  # noqa: E402

import migrate  # noqa: E402


class Cursor:
    def __init__(self, fail_on=None):
        self.log = []
        self.fail_on = fail_on

    def execute(self, sql):
        self.log.append(sql)
        if self.fail_on and self.fail_on in sql:
            raise RuntimeError("simulated database failure")


class Conn:
    def __init__(self, fail_on=None):
        self.cur = Cursor(fail_on)

    def cursor(self):
        return self.cur

    def commit(self):
        self.cur.log.append("COMMIT")


def happy_path_is_transactional():
    conn = Conn()
    migrate.upgrade(conn)
    log = conn.cur.log
    assert any("BEGIN" in s.upper() for s in log), log
    assert any("DROP" in s.upper() for s in log), log


def failure_rolls_back_and_propagates():
    conn = Conn(fail_on="DROP")
    try:
        migrate.upgrade(conn)
    except RuntimeError:
        pass
    else:
        raise AssertionError("the failure was swallowed")
    log = conn.cur.log
    assert any("ROLLBACK" in s.upper() for s in log), log


main([
    ("happy_path_is_transactional", happy_path_is_transactional),
    ("failure_rolls_back_and_propagates", failure_rolls_back_and_propagates),
])
