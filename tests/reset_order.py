import paramiko
ssh = paramiko.SSHClient()
ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
ssh.connect('192.168.2.220', username='root', password='123456', allow_agent=False, look_for_keys=False, timeout=15)

script = r"""
import sqlite3, sys
conn = sqlite3.connect('/opt/BotPasteDon/data/orders.db')
order_id = sys.argv[1] if len(sys.argv) > 1 else ''
if order_id:
    conn.execute("UPDATE orders SET status='NOTIFIED', retry_data=NULL WHERE order_id LIKE ?", (order_id + '%',))
    conn.commit()
    cur = conn.execute("SELECT order_id, status, discord_thread_id FROM orders WHERE order_id LIKE ?", (order_id + '%',))
    for r in cur:
        print(f'{r[0]} | {r[1]} | thread={r[2]}')
conn.close()
print('Done')
"""

# Write script to server and run it
sftp = ssh.open_sftp()
with sftp.file('/tmp/reset_order.py', 'w') as f:
    f.write(script)
sftp.close()

stdin, stdout, stderr = ssh.exec_command('python3 /tmp/reset_order.py 7cd87bff-997a-4826-84c8-eb5f06f58b47', timeout=10)
print(stdout.read().decode())
print(stderr.read().decode())
ssh.close()
