"""Test G2G API endpoints — run locally via paramiko."""
import paramiko, json, base64, time, shlex, os, sys
sys.stdout.reconfigure(encoding='utf-8')

ORDER_ID = "1779277519044HQUA"

ssh = paramiko.SSHClient()
ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
ssh.connect('192.168.2.220', username='root', password='123456',
            allow_agent=False, look_for_keys=False, timeout=15)

# Get auth
stdin, stdout, stderr = ssh.exec_command('curl -s http://localhost:8010/auth/g2g', timeout=15)
auth = json.loads(stdout.read().decode())
jwt = auth['jwt_token']
parts = jwt.split('.')
payload = parts[1] + '=' * (-len(parts[1]) % 4)
claims = json.loads(base64.urlsafe_b64decode(payload))
seller_id = claims['sub']
exp_in = int(claims['exp'] - time.time())
print(f'AUTH: seller_id={seller_id}, jwt_exp_in={exp_in}s')

UA = 'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36'

def api(method, path, params=None, data=None):
    url = f'https://sls.g2g.com{path}'
    if params:
        url += '?' + '&'.join(f'{k}={v}' for k, v in params.items())
    cmd = [
        'curl -s', '-X', method, shlex.quote(url),
        '-H', shlex.quote('authorization: ' + jwt),
        '-H', shlex.quote('user-agent: ' + UA),
        '-H', shlex.quote('origin: https://www.g2g.com'),
        '-H', shlex.quote('content-type: application/json'),
    ]
    if data:
        cmd.extend(['-d', shlex.quote(json.dumps(data))])
    stdin, stdout, stderr = ssh.exec_command(' '.join(cmd), timeout=20)
    return json.loads(stdout.read().decode())

# 1. List all orders by status
print('\n=== 1. LIST ORDERS BY STATUS ===')
for status in ['preparing', 'delivering', 'delivered', 'completed']:
    j = api('GET', '/order/list_my_order', {'seller_id': seller_id, 'status': status})
    results = (j.get('payload') or {}).get('results') or []
    code = j.get('code', '?')
    print(f'  {status}: code={code}, count={len(results)}')
    for o in results[:5]:
        oid = o.get('order_item_id', '')
        title = o.get('offer_title', '')[:50]
        qty = o.get('purchased_qty', '')
        print(f'    {oid} | qty={qty} | {title}')

# 2. Try order detail with different ID formats
print(f'\n=== 2. ORDER DETAIL (trying formats) ===')
for oid in [ORDER_ID, ORDER_ID + '-1', ORDER_ID.lower(), ORDER_ID.lower() + '-1']:
    j = api('GET', f'/order/item/{oid}', {'seller_id': seller_id})
    code = j.get('code')
    if code == 2000:
        p = j['payload']
        print(f'  {oid}: FOUND! status={p.get("status")}, title={p.get("offer_title","")[:50]}')
        print(f'    purchased={p.get("purchased_qty")}, delivered={p.get("delivered_qty")}')
        print(f'    buyer_id={p.get("buyer_id")}, unit_price={p.get("unit_price")}')
        # Save for next tests
        working_oid = oid
        order_data = p
        break
    else:
        msg = j.get('messages', [{}])[0].get('text', '')[:50] if j.get('messages') else ''
        print(f'  {oid}: code={code} {msg}')
else:
    print('\n  Order not found in any format. Using first preparing order for API test.')
    j = api('GET', '/order/list_my_order', {'seller_id': seller_id, 'status': 'preparing'})
    results = (j.get('payload') or {}).get('results') or []
    if results:
        o = results[0]
        working_oid = o.get('order_item_id', '')
        print(f'  Using: {working_oid}')
        # Get full detail
        j = api('GET', f'/order/item/{working_oid}', {'seller_id': seller_id})
        if j.get('code') == 2000:
            order_data = j['payload']
        else:
            order_data = o
    else:
        print('  No preparing orders found!')
        ssh.close()
        exit(1)

# 3. Test start_deliver (DRY RUN — will NOT actually execute)
print(f'\n=== 3. DELIVERY API TEST (dry run) ===')
print(f'Order: {working_oid}')
print(f'Status: {order_data.get("status")}')
print(f'Title: {order_data.get("offer_title","")[:60]}')
print(f'Qty: {order_data.get("purchased_qty")}')
print()

# Check available transitions
print('Available API endpoints to test:')
print(f'  PUT /order/item/{working_oid}/start_deliver?seller_id={seller_id}')
print(f'  PUT /order/item/{working_oid}/mark_as_delivering?seller_id={seller_id}')
print(f'  PUT /order/item/{working_oid}/delivered_qty?seller_id={seller_id}')
print(f'  GET  /order/upload_url?name=proof.png&upload_type=delivery_proof')

ssh.close()
