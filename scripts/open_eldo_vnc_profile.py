"""Launch Camoufox visible on Xvfb :99 with a specified eldo profile.

Usage (on server):
    DISPLAY=:99 venv/bin/python /tmp/open_eldo_vnc_profile.py <profile_name>

Example:
    DISPLAY=:99 venv/bin/python /tmp/open_eldo_vnc_profile.py chrome_profile_eldo_bak1
"""
import os
import sys
import time
from pathlib import Path

os.environ.setdefault("DISPLAY", ":99")

if len(sys.argv) < 2:
    sys.stderr.write("usage: open_eldo_vnc_profile.py <profile_name>\n")
    sys.exit(2)

profile_name = sys.argv[1]
PROFILE = Path("/opt/BotPasteDon") / profile_name
URL = "https://www.eldorado.gg/"

if not PROFILE.exists():
    sys.stderr.write(f"profile not found: {PROFILE}\n")
    sys.exit(3)

from camoufox.sync_api import Camoufox

print(f"[open_eldo_vnc_profile] DISPLAY={os.environ.get('DISPLAY')} profile={PROFILE}", flush=True)

with Camoufox(
    headless=False,
    humanize=True,
    window=(1280, 720),
    persistent_context=True,
    user_data_dir=str(PROFILE),
) as browser:
    page = browser.new_page()
    page.goto(URL, timeout=60000)
    print(f"[open_eldo_vnc_profile] Opened {URL} for {profile_name} — keep alive. Ctrl+C to close.", flush=True)
    while True:
        time.sleep(60)
