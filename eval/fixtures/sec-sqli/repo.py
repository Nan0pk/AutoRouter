def find_user(conn, name):
    cur = conn.cursor()
    cur.execute(f"SELECT id, name FROM users WHERE name = '{name}'")
    return cur.fetchall()
