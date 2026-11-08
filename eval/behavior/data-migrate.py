import os
import sqlite3
import sys
from contextlib import closing

sys.path.insert(0, os.getcwd())
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _harness import main  # noqa: E402

import migrate  # noqa: E402


def make_db():
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE users (id INTEGER PRIMARY KEY, legacy_flag INTEGER)")
    conn.execute("INSERT INTO users VALUES (1, 7)")
    conn.commit()
    return conn


def happy_path_is_transactional():
    with closing(make_db()) as conn:
        migrate.upgrade(conn)
        assert not conn.in_transaction
        assert conn.execute("SELECT * FROM users").fetchall() == [(1,)]


def failure_rolls_back_and_propagates():
    commit_attempted = False

    def fail_commit(action, operation, *_):
        nonlocal commit_attempted
        if action == sqlite3.SQLITE_TRANSACTION and operation == "COMMIT":
            commit_attempted = True
            return sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_OK

    with closing(make_db()) as conn:
        conn.set_authorizer(fail_commit)
        try:
            migrate.upgrade(conn)
        except sqlite3.DatabaseError:
            pass
        else:
            raise AssertionError("the commit failure was swallowed")
        assert commit_attempted, "schema change was not wrapped in a transaction"
        assert not conn.in_transaction, "the failed transaction is still open"
        assert conn.execute("SELECT * FROM users").fetchall() == [(1, 7)]


main([
    ("happy_path_is_transactional", happy_path_is_transactional),
    ("failure_rolls_back_and_propagates", failure_rolls_back_and_propagates),
])
