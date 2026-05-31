"""Test TalkJS WebSocket file attachment with non-empty text."""
import paramiko, sys
sys.stdout.reconfigure(encoding='utf-8')

ssh = paramiko.SSHClient()
ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
ssh.connect('192.168.2.220', username='root', password='123456', allow_agent=False, look_for_keys=False, timeout=15)

script = r"""
import asyncio, json, sys
sys.path.insert(0, '/opt/BotPasteDon')
from shared.eldo_auth import EldoAuthManager
from shared.eldo_api import EldoradoAPIClient
from workers.talkjs_client import TalkJSClient

async def test():
    mgr = EldoAuthManager(auth_url='http://localhost:8010')
    auth = await mgr.get_auth()
    api = EldoradoAPIClient(mgr)
    jwt = api.get_talkjs_auth(auth)
    user_id = api.get_talkjs_user_id(jwt)
    detail = api.get_order_detail("7cd87bff-997a-4826-84c8-eb5f06f58b47", auth)
    conv_id = detail.get("talkJsConversationId", "")

    tc = TalkJSClient()
    tc.auth_token = jwt
    tc.user_id = user_id
    connected = await tc.connect()

    # Use the already uploaded file URL
    file_url = "https://firebasestorage.googleapis.com/v0/b/klets-3642/o/user_files%2F49mLECOW%2Fbce234f5-8015-433e-84c9-eeb63a1d3b6c%2Ftest_proof.png?alt=media&token=42fbd6f6-7c1a-4d66-bc82-2d77849ee6a5"

    payloads = [
        # With text + attachment
        {"type": "UserMessage", "text": " ", "attachment": {
            "type": "image", "url": file_url, "size": 69, "filename": "test_proof.png",
        }},
        # Just attachment, no text
        {"type": "UserMessage", "attachment": {
            "type": "image", "url": file_url, "size": 69, "filename": "test_proof.png",
        }},
    ]

    for i, payload in enumerate(payloads):
        rid = tc._get_request_id()
        msg = [rid, "POST", f"/conversations/{conv_id}/messages", payload, {}]
        await tc.ws.send(json.dumps(msg))
        resp = await tc._wait_response(rid, timeout=5)
        if resp and len(resp) > 2:
            print(f'[{i}] {resp[1]} {json.dumps(resp[2])[:200]}')
        else:
            print(f'[{i}] timeout')

    await tc.close()

asyncio.run(test())
"""

sftp = ssh.open_sftp()
with sftp.file('/tmp/test_talkjs6.py', 'w') as f:
    f.write(script)
sftp.close()

stdin, stdout, stderr = ssh.exec_command('/opt/BotPasteDon/venv/bin/python /tmp/test_talkjs6.py', timeout=30)
print(stdout.read().decode())
print('ERR:', stderr.read().decode()[:300])
ssh.close()
