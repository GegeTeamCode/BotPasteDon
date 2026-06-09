"""Driver: scp _g2g_js_grep_remote.py to 220, run, fetch output."""
import sys
import time
from pathlib import Path

import paramiko

sys.stdout.reconfigure(encoding="utf-8", errors="backslashreplace")

s = paramiko.SSHClient()
s.set_missing_host_key_policy(paramiko.AutoAddPolicy())
s.connect("192.168.2.220", username="root", password="123456", timeout=15)

s.exec_command("pgrep -f 'watchdog.py' | xargs -r kill -9")
time.sleep(2)

sftp = s.open_sftp()
sftp.put(str(Path(__file__).parent / "_g2g_js_grep_remote.py"), "/tmp/_g2g_js_grep.py")
sftp.close()

_, o, e = s.exec_command("/opt/BotPasteDon/venv/bin/python /tmp/_g2g_js_grep.py", timeout=300)
print(o.read().decode(errors="replace"))
err = e.read().decode(errors="replace")
if err.strip():
    print("STDERR:")
    print(err[:3000])

s.exec_command(
    "cd /opt/BotPasteDon && nohup venv/bin/python scripts/watchdog.py "
    "</dev/null >/tmp/watchdog.log 2>&1 & disown"
)
time.sleep(2)
s.close()
