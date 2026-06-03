"""Inspect + clean Firefox profile locks then re-trigger /auth/eldo."""
import paramiko, time

ssh = paramiko.SSHClient()
ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
ssh.connect("192.168.2.220", username="root", password="123456", timeout=15)

def run(cmd, timeout=30):
    _, stdout, stderr = ssh.exec_command(cmd, timeout=timeout)
    return stdout.read().decode(errors="replace").strip(), stderr.read().decode(errors="replace").strip()

print("===== Camoufox processes =====")
out, _ = run("ps aux | grep -iE 'camoufox|firefox' | grep -v grep")
print(out or "(none)")

print("\n===== Lock files in main profile =====")
out, _ = run("ls -la /opt/BotPasteDon/chrome_profile_eldo/ | grep -iE 'lock|parent|singleton'")
print(out or "(no lock files)")

print("\n===== Lock files in bak profiles =====")
out, _ = run("ls -la /opt/BotPasteDon/chrome_profile_eldo_bak1/ /opt/BotPasteDon/chrome_profile_eldo_bak2/ 2>/dev/null | grep -iE 'lock|parent|singleton'")
print(out or "(none)")

print("\n===== Kill any leftover camoufox =====")
run("pkill -9 -f camoufox-bin ; pkill -9 -f camoufox ; sleep 2")
out, _ = run("ps aux | grep -iE 'camoufox|firefox' | grep -v grep")
print(out or "(clean)")

print("\n===== Remove lock files (all 3 profiles) =====")
out, _ = run(
    "for p in chrome_profile_eldo chrome_profile_eldo_bak1 chrome_profile_eldo_bak2; do "
    "rm -fv /opt/BotPasteDon/$p/parent.lock /opt/BotPasteDon/$p/.parentlock "
    "/opt/BotPasteDon/$p/lock /opt/BotPasteDon/$p/.lock 2>&1; done"
)
print(out or "(no files to remove)")

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

print("\n===== Auth log tail =====")
out, _ = run("ls -t /tmp/auth*.log | head -1")
log = out.strip()
out, _ = run(f"tail -n 60 {log}")
print(out)

print("\n===== Scanner log (wait 20s) =====")
time.sleep(20)
out, _ = run("tail -n 15 /tmp/eldo_scanner.log")
print(out)

ssh.close()
