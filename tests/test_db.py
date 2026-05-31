import sqlite3
c = sqlite3.connect("/opt/BotPasteDon/data/orders.db")
c.execute("PRAGMA journal_mode=DELETE")
c.execute("CREATE TABLE IF NOT EXISTS test_t (id TEXT PRIMARY KEY)")
c.commit()
rows = c.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
print("tables:", rows)
c.close()
