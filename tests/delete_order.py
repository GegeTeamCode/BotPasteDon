import sqlite3, sys
db_path = "/opt/BotPasteDon/data/orders.db"
oid = sys.argv[1] if len(sys.argv) > 1 else ""
conn = sqlite3.connect(db_path)
conn.execute("DELETE FROM orders WHERE order_id LIKE ?", (f"%{oid}%",))
conn.commit()
print(f"Deleted orders matching: %{oid}%")
conn.close()
