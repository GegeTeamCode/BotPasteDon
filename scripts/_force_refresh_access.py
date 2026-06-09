"""Driver: SCP _g2g_force_remote.py to 220 and run it.

Pauses watchdog so the remote Chrome can use a clone of chrome_profile_g2g
without lock fights, then restarts watchdog.
"""
import sys
import time
from pathlib import Path

import paramiko

sys.stdout.reconfigure(encoding="utf-8", errors="backslashreplace")

LOCAL_REMOTE = Path(r"d:\Code Bot\BotPasteDon\scripts\_g2g_force_remote.py")

s = paramiko.SSHClient()
s.set_missing_host_key_policy(paramiko.AutoAddPolicy())
s.connect("192.168.2.220", username="root", password="123456", timeout=15)

print("Pause watchdog...")
s.exec_command("pgrep -f 'watchdog.py' | xargs -r kill -9")
time.sleep(2)

sftp = s.open_sftp()
sftp.put(str(LOCAL_REMOTE), "/tmp/_g2g_force.py")
sftp.close()

_, o, e = s.exec_command("/opt/BotPasteDon/venv/bin/python /tmp/_g2g_force.py", timeout=240)
print(o.read().decode(errors="replace"))
err = e.read().decode(errors="replace")
if err.strip():
    print("STDERR:")
    print(err[:3000])

print("\nRestart watchdog...")
s.exec_command(
    "cd /opt/BotPasteDon && nohup venv/bin/python scripts/watchdog.py "
    "</dev/null >/tmp/watchdog.log 2>&1 & disown"
)
time.sleep(2)
s.close()
