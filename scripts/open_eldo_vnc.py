"""Launch Camoufox visible on the LXC's Xvfb :99 with the eldo main profile.

Usage: scp this to server, then run:
    DISPLAY=:99 /opt/BotPasteDon/venv/bin/python /tmp/open_eldo_vnc.py
"""
import os
import sys
import time
from pathlib import Path

os.environ.setdefault("DISPLAY", ":99")

PROFILE = Path("/opt/BotPasteDon/chrome_profile_eldo")
URL = "https://www.eldorado.gg/"

from camoufox.sync_api import Camoufox

print(f"[open_eldo_vnc] DISPLAY={os.environ.get('DISPLAY')} profile={PROFILE}", flush=True)

with Camoufox(
    headless=False,
    humanize=True,
    window=(1280, 720),
    persistent_context=True,
    user_data_dir=str(PROFILE),
) as browser:
    page = browser.new_page()
    page.goto(URL, timeout=60000)
    print(f"[open_eldo_vnc] Opened {URL} - keep session alive. Ctrl+C to close.", flush=True)
    while True:
        time.sleep(60)
