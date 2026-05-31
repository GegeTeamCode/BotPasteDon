import sqlite3, json, sys

conn = sqlite3.connect("/opt/BotPasteDon/data/orders.db")
conn.row_factory = sqlite3.Row

oid = sys.argv[1] if len(sys.argv) > 1 else "%"
row = conn.execute("SELECT * FROM orders WHERE order_id LIKE ?", (f"%{oid}%",)).fetchone()
if row:
    for k in row.keys():
        v = row[k]
        if k == "raw_data" and v:
            print(f"{k}: {v[:800]}")
        else:
            print(f"{k}: {v}")
else:
    print("Not found")

# Also show all recent orders
print("\n=== Recent orders ===")
rows = conn.execute("SELECT order_id, platform, status, item_name, game, server FROM orders ORDER BY created_at DESC LIMIT 5").fetchall()
for r in rows:
    print(dict(r))

conn.close()
