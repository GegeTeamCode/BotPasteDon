"""Send test webhook to Discord, verify coordinator creates thread."""
import paramiko, json, shlex, sys
sys.stdout.reconfigure(encoding='utf-8')

ssh = paramiko.SSHClient()
ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
ssh.connect('192.168.2.220', username='root', password='123456',
            allow_agent=False, look_for_keys=False, timeout=15)

# Get webhook URL
stdin, stdout, stderr = ssh.exec_command('grep WEBHOOK_DEFAULT /opt/BotPasteDon/.env', timeout=10)
webhook_url = stdout.read().decode().strip().split('=', 1)[1]
print(f'Webhook: ...{webhook_url[-20:]}')

# Build message
order_id = '1779277519044HQUA-1'
order_url = 'https://www.g2g.com/order/item/1779277519044HQUA-1'
content = (
    "**G2G | TestCustomer**\n"
    f"[{order_id}](<{order_url}>)\n"
    "**Any Items - Aspects** | Qty: **1**\n"
    f"`{order_url}`"
)

payload = json.dumps({"content": content})
cmd = ' '.join([
    'curl -s -X POST', shlex.quote(webhook_url),
    '-H', shlex.quote('content-type: application/json'),
    '-d', shlex.quote(payload),
])

print('Sending webhook...')
stdin, stdout, stderr = ssh.exec_command(cmd, timeout=15)
resp = stdout.read().decode()
print(f'Response: {resp[:100]}' if resp else 'Sent OK (empty = success)')

import time; time.sleep(5)

# Check coordinator log for thread creation
print('\nCoordinator log (last 10 lines):')
stdin, stdout, stderr = ssh.exec_command('tail -10 /tmp/coordinator.log', timeout=10)
print(stdout.read().decode())

ssh.close()
