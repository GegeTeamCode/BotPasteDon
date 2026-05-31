"""Manual login tool for G2G — creates persistent Chrome profile.

Usage:
    python g2g_login.py

This opens a Chrome browser with a dedicated profile for G2G.
After logging in manually, the session persists for API auth.
"""

import time
import os
from shared.driver_manager import get_driver

PROFILE = "chrome_profile_g2g"
LOGIN_URL = "https://www.g2g.com/login"


def main():
    print("=" * 50)
    print("  G2G Login Tool")
    print("=" * 50)
    print()
    print(f"Profile: {PROFILE}")
    print(f"URL: {LOGIN_URL}")
    print()
    print("NOTE: Stop the bot before using this tool!")
    print()

    driver = None
    try:
        driver = get_driver(profile_dir=PROFILE)
        driver.get(LOGIN_URL)

        print("Browser opened. Please:")
        print("  1. Login to G2G")
        print("  2. Enter 2FA if prompted")
        print("  3. Check 'Remember me'")
        print()

        input("Press ENTER when done...")

        # Verify login by checking for auth cookies
        cookies = {c["name"]: c["value"] for c in driver.get_cookies()}
        has_session = any(k for k in cookies if "token" in k.lower() or "session" in k.lower())

        if has_session:
            print("Session cookies found. Login saved!")
        else:
            print("Warning: No session cookies detected.")
            print("Make sure you're fully logged in before closing.")

        time.sleep(2)

    except Exception as e:
        print(f"Error: {e}")
    finally:
        if driver:
            driver.quit()
        print("Done.")


if __name__ == "__main__":
    main()
