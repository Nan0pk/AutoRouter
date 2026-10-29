def upgrade(conn):
    cur = conn.cursor()
    cur.execute("ALTER TABLE users DROP COLUMN legacy_flag")
    conn.commit()
