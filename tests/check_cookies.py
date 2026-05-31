import sqlite3

c = sqlite3.connect("/opt/BotPasteDon/chrome_profile_eldo/Default/Network/Cookies")
rows = c.execute("SELECT host_key, name, length(encrypted_value), path FROM cookies WHERE host_key LIKE '%talkjs%'").fetchall()
for r in rows:
    print(f"{r[0]} | {r[1]} | enc_len={r[2]} | {r[3]}")
if not rows:
    print("No TalkJS cookies")

print()
print("Top domains:")
rows2 = c.execute("SELECT host_key, count(*) FROM cookies GROUP BY host_key ORDER BY count(*) DESC LIMIT 15").fetchall()
for r in rows2:
    print(f"  {r[0]}: {r[1]} cookies")
c.close()
