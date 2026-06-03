"""Deploy patched auth/main.py to server, restart auth, verify."""
import paramiko, time
from pathlib import Path

ssh = paramiko.SSHClient()
ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
ssh.connect("192.168.2.220", username="root", password="123456", timeout=15)

def run(cmd, timeout=30):
    _, stdout, stderr = ssh.exec_command(cmd, timeout=timeout)
    return stdout.read().decode(errors="replace").strip(), stderr.read().decode(errors="replace").strip()

def fire_and_forget(cmd, timeout=10):
    chan = ssh.get_transport().open_session()
    chan.settimeout(timeout)
    chan.exec_command(cmd)
    for _ in range(int(timeout * 2)):
        if chan.exit_status_ready():
            break
        time.sleep(0.5)
    chan.close()

def hard_kill_all():
    """Kill via pgrep|kill -9 on each pattern, in separate exec_command calls
    so the running shell doesn't self-match."""
    patterns = [
        "auth.main",
        "camoufox-bin",
        "chromedriver",
        "chrome_profile_g2g",
        "chrome_profile_eldo",
    ]
    for pat in patterns:
        # Use pgrep to get PIDs, then kill -9 each one. The pgrep -f matches
        # against the pattern but exits cleanly; the inner xargs kill doesn't
        # see the pattern string in its own cmdline.
        run(f"pgrep -f '{pat}' | xargs -r kill -9 2>/dev/null", timeout=10)
    time.sleep(2)

LOCAL = Path(r"d:\Code Bot\BotPasteDon\auth\main.py")
REMOTE = "/opt/BotPasteDon/auth/main.py"

print("===== Upload =====")
sftp = ssh.open_sftp()
sftp.put(str(LOCAL), REMOTE)
sftp.close()
out, _ = run(f"md5sum {REMOTE}")
print(out)

# Pick next log
out, _ = run("ls /tmp/auth*.log 2>/dev/null | sort")
nums = []
for path in [l.strip() for l in out.splitlines() if l.strip()]:
    digits = "".join(c for c in path.rsplit("/", 1)[-1] if c.isdigit())
    if digits:
        nums.append(int(digits))
next_log = f"/tmp/auth{max(nums) + 1 if nums else 1}.log"
print(f"Log -> {next_log}")

print("\n===== Stop watchdog =====")
run("pgrep -f 'watchdog.py' | xargs -r kill -9 2>/dev/null")
time.sleep(2)

print("\n===== Stop auth + ALL browsers (hard kill, separate sessions) =====")
hard_kill_all()
out, _ = run("ps aux | grep -E 'auth.main|camoufox-bin|chromedriver|chrome_profile' | grep -v grep | grep -v pgrep | grep -v 'kill -9'")
print(out or "(all clean)")

# Verify port 8010 is free
out, _ = run("ss -tlnp 2>/dev/null | grep ':8010' || echo 'port 8010 FREE'")
print(out)

# Verify lock files exist (proof for the patched startup cleanup to handle)
print("\n===== Pre-existing lock files (patched startup will clean these) =====")
out, _ = run("ls /opt/BotPasteDon/chrome_profile_eldo/ /opt/BotPasteDon/chrome_profile_g2g/ "
             "/opt/BotPasteDon/chrome_profile_eldo_bak1/ /opt/BotPasteDon/chrome_profile_eldo_bak2/ "
             "2>/dev/null | grep -iE 'lock|singleton'")
print(out or "(no lock files anywhere)")

# Plant fake locks to PROVE the startup cleanup works
print("\n===== Plant fake lock files to test startup cleanup =====")
run("touch /opt/BotPasteDon/chrome_profile_eldo/parent.lock "
    "/opt/BotPasteDon/chrome_profile_eldo/.parentlock "
    "/opt/BotPasteDon/chrome_profile_g2g/SingletonCookie")
out, _ = run("ls /opt/BotPasteDon/chrome_profile_eldo/parent.lock "
             "/opt/BotPasteDon/chrome_profile_eldo/.parentlock "
             "/opt/BotPasteDon/chrome_profile_g2g/SingletonCookie 2>&1")
print(out)

print(f"\n===== Start patched auth -> {next_log} =====")
fire_and_forget(
    f"cd /opt/BotPasteDon && HEADLESS_MODE=true setsid venv/bin/python -u -m auth.main "
    f"</dev/null >{next_log} 2>&1 & disown",
    timeout=8,
)
time.sleep(4)
out, _ = run("ps aux | grep 'auth.main' | grep -v grep")
print(out or "(NOT running!)")

print("\n===== Verify startup cleanup removed planted locks =====")
out, _ = run("ls /opt/BotPasteDon/chrome_profile_eldo/parent.lock "
             "/opt/BotPasteDon/chrome_profile_eldo/.parentlock "
             "/opt/BotPasteDon/chrome_profile_g2g/SingletonCookie 2>&1 | head")
print(out)

print("\n===== Wait 30s for service init =====")
time.sleep(30)
out, _ = run("curl -s -m 5 http://localhost:8010/health")
print("Health:", out or "(NO RESPONSE)")

print(f"\n===== Auth log tail ({next_log}) =====")
out, _ = run(f"tail -n 30 {next_log}")
print(out)

print("\n===== Trigger /auth/eldo (max 180s) =====")
out, _ = run(
    "curl -s -m 180 -o /tmp/eldo_resp.json -w 'HTTP %{http_code} in %{time_total}s\\n' "
    "http://localhost:8010/auth/eldo",
    timeout=200,
)
print(out)
out, _ = run(
    "python3 -c \"import json; d=json.load(open('/tmp/eldo_resp.json')); "
    "print('cookies:', len(d.get('cookies',{})),'| xsrf:', bool(d.get('xsrf_token')),"
    "'| logged_in:', d.get('logged_in'))\""
)
print(out)

print("\n===== Trigger /auth/g2g (max 180s) =====")
out, _ = run(
    "curl -s -m 180 -o /tmp/g2g_resp.json -w 'HTTP %{http_code} in %{time_total}s\\n' "
    "http://localhost:8010/auth/g2g",
    timeout=200,
)
print(out)
out, _ = run(
    "python3 -c \"import json; d=json.load(open('/tmp/g2g_resp.json')); "
    "print('jwt:', bool(d.get('jwt_token')),'| cookies:', len(d.get('cookies',{})))\""
)
print(out)

print(f"\n===== Auth log full ({next_log}) =====")
out, _ = run(f"cat {next_log}")
print(out)

print("\n===== Health final =====")
out, _ = run("curl -s -m 5 http://localhost:8010/health | python3 -m json.tool")
print(out)

print("\n===== Restart watchdog =====")
fire_and_forget(
    "cd /opt/BotPasteDon && setsid venv/bin/python scripts/watchdog.py "
    "</dev/null >/tmp/watchdog.log 2>&1 & disown",
    timeout=8,
)
time.sleep(3)
out, _ = run("ps aux | grep watchdog.py | grep -v grep")
print(out or "(NOT running)")

ssh.close()
