"""Eldorado login tool — uses Camoufox to bypass Cloudflare.

Usage:
    python eldo_login.py

Camoufox (anti-detect Firefox) handles Cloudflare Turnstile automatically.
Login once, profile persists for auth service.
"""

import sys


def main():
    try:
        from camoufox.sync_api import Camoufox
    except ImportError:
        print("Camoufox not installed. Run:")
        print("  pip install camoufox && camoufox fetch")
        return

    from pathlib import Path
    profile = Path("chrome_profile_eldo")
    profile.mkdir(exist_ok=True)

    print("Opening Camoufox for Eldorado login...")
    print("Camoufox will auto-handle Cloudflare Turnstile.")
    print()

    with Camoufox(
        headless=False,
        humanize=True,
        window=(1280, 720),
        persistent_context=True,
        user_data_dir=str(profile),
    ) as browser:
        page = browser.new_page()
        page.goto("https://www.eldorado.gg/login", timeout=60000)

        print("Browser opened. Please:")
        print("  1. Login with Google")
        print("  2. Complete any verification")
        print("  3. Check that you're logged in")
        print()
        input("Press ENTER when done...")

        # Verify
        url = page.url
        if "login" not in url.lower():
            print(f"Login confirmed! URL: {url}")
        else:
            print(f"Warning: Still on login page: {url}")

    print("Profile saved.")
    print()
    print("Copy to LXC:")
    print('  scp -r "d:\\Code Bot\\BotPasteDon\\chrome_profile_eldo" root@192.168.2.220:/opt/BotPasteDon/')


if __name__ == "__main__":
    main()
