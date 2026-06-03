"""Upload open_eldo_vnc.py to server and launch it in the background on DISPLAY=:99."""
import paramiko
from pathlib import Path

HOST = "192.168.2.220"
LOCAL = Path(__file__).parent / "open_eldo_vnc.py"
REMOTE = "/tmp/open_eldo_vnc.py"
LOG = "/tmp/open_eldo_vnc.log"

ssh = paramiko.SSHClient()
ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
ssh.connect(HOST, username="root", password="123456", timeout=15)

# Upload
sftp = ssh.open_sftp()
sftp.put(str(LOCAL), REMOTE)
sftp.close()
print(f"[deploy] Uploaded {LOCAL} -> {REMOTE}")

# Kill any old viewer
ssh.exec_command("pkill -f open_eldo_vnc.py 2>/dev/null; sleep 1")

# Launch detached on DISPLAY=:99
cmd = (
    f"cd /opt/BotPasteDon && "
    f"DISPLAY=:99 nohup venv/bin/python -u {REMOTE} > {LOG} 2>&1 &"
)
_, stdout, stderr = ssh.exec_command(cmd, timeout=10)
stdout.channel.recv_exit_status()
print(f"[deploy] Launched. Log: {LOG}")

import time
time.sleep(8)

_, stdout, _ = ssh.exec_command(f"tail -n 50 {LOG}")
print("\n===== Log tail =====")
print(stdout.read().decode(errors="replace"))

_, stdout, _ = ssh.exec_command("ps aux | grep open_eldo_vnc.py | grep -v grep")
print("===== Process =====")
print(stdout.read().decode(errors="replace") or "(not running)")

ssh.close()
print("\n>>> Connect VNC to 192.168.2.220:5900 (password: 123456) to view.")
