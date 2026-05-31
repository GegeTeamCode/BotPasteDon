"""Test G2G delivery API endpoints — dry run (start_deliver only, will check response)."""
import paramiko, json, base64, time, shlex, sys
sys.stdout.reconfigure(encoding='utf-8')

ORDER_ID = "1779277519044HQUA-1"

ssh = paramiko.SSHClient()
ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
ssh.connect('192.168.2.220', username='root', password='123456',
            allow_agent=False, look_for_keys=False, timeout=15)

# Get fresh auth
stdin, stdout, stderr = ssh.exec_command('curl -s http://localhost:8010/auth/g2g', timeout=15)
auth = json.loads(stdout.read().decode())
jwt = auth['jwt_token']
parts = jwt.split('.')
payload = parts[1] + '=' * (-len(parts[1]) % 4)
claims = json.loads(base64.urlsafe_b64decode(payload))
seller_id = claims['sub']

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
        '-H', shlex.quote('referer: https://www.g2g.com/g2g-user/sale'),
        '-H', shlex.quote('content-type: application/json'),
    ]
    if data:
        cmd.extend(['-d', shlex.quote(json.dumps(data))])
    stdin, stdout, stderr = ssh.exec_command(' '.join(cmd), timeout=20)
    resp = stdout.read().decode()
    try:
        return json.loads(resp)
    except:
        return {"raw": resp[:200]}

# 1. Test start_deliver
print(f'=== 1. START_DELIVER ({ORDER_ID}) ===')
j = api('PUT', f'/order/item/{ORDER_ID}/start_deliver', {'seller_id': seller_id})
print(f'code: {j.get("code")}')
if j.get('code') == 2000:
    p = j.get('payload', {})
    print(f'status: {p.get("status")}')
    print(f'keys: {list(p.keys())[:15]}')
    print('SUCCESS - start_deliver worked!')
elif j.get('code') == 4041:
    print('Order not found or already started')
elif j.get('code') == 4001:
    msgs = j.get('messages', [])
    for m in msgs:
        print(f'  error: {m.get("text", "")}')
else:
    print(json.dumps(j, indent=2, ensure_ascii=False)[:500])

# 2. Check order status after start_deliver
print(f'\n=== 2. ORDER STATUS AFTER START_DELIVER ===')
j2 = api('GET', f'/order/item/{ORDER_ID}', {'seller_id': seller_id})
if j2.get('code') == 2000:
    p = j2['payload']
    print(f'status: {p.get("status")}')
    print(f'offer_title: {p.get("offer_title", "")[:60]}')
    print(f'purchased_qty: {p.get("purchased_qty")}')
    print(f'delivered_qty: {p.get("delivered_qty")}')
    print(f'buyer_id: {p.get("buyer_id")}')

    # 3. Test mark_as_delivering
    print(f'\n=== 3. MARK_AS_DELIVERING ({ORDER_ID}) ===')
    j3 = api('PUT', f'/order/item/{ORDER_ID}/mark_as_delivering', {'seller_id': seller_id})
    print(f'code: {j3.get("code")}')
    if j3.get('code') == 2000:
        p3 = j3.get('payload', {})
        print(f'status: {p3.get("status")}')
        print('SUCCESS - mark_as_delivering worked!')
    else:
        msgs = j3.get('messages', [])
        for m in msgs:
            print(f'  error: {m.get("text", "")}')

    # 4. Check status again
    print(f'\n=== 4. ORDER STATUS AFTER MARK_AS_DELIVERING ===')
    j4 = api('GET', f'/order/item/{ORDER_ID}', {'seller_id': seller_id})
    if j4.get('code') == 2000:
        p4 = j4['payload']
        print(f'status: {p4.get("status")}')

    # 5. Test upload_url (just get URL, don't upload)
    print(f'\n=== 5. UPLOAD_URL ===')
    j5 = api('GET', '/order/upload_url', {'name': 'proof.png', 'upload_type': 'delivery_proof'})
    print(f'code: {j5.get("code")}')
    if j5.get('code') == 2000:
        upload_url = (j5.get('payload') or {}).get('upload_url', '')
        print(f'upload_url: {upload_url[:80]}...' if upload_url else 'No URL returned')
    else:
        print(json.dumps(j5, indent=2, ensure_ascii=False)[:300])

    # 6. Test delivered_qty
    print(f'\n=== 6. DELIVERED_QTY ===')
    j6 = api('PUT', f'/order/item/{ORDER_ID}/delivered_qty', {'seller_id': seller_id}, {'qty': 1})
    print(f'code: {j6.get("code")}')
    if j6.get('code') == 2000:
        print('SUCCESS - delivered_qty worked!')
    else:
        msgs = j6.get('messages', [])
        for m in msgs:
            print(f'  error: {m.get("text", "")}')

    # 7. Check final status
    print(f'\n=== 7. FINAL ORDER STATUS ===')
    j7 = api('GET', f'/order/item/{ORDER_ID}', {'seller_id': seller_id})
    if j7.get('code') == 2000:
        p7 = j7['payload']
        print(f'status: {p7.get("status")}')
        print(f'delivered_qty: {p7.get("delivered_qty")}')
        print(f'purchased_qty: {p7.get("purchased_qty")}')
else:
    print('Could not get order detail')

ssh.close()
print('\n=== DONE ===')
