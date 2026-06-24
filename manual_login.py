"""
Tool đăng nhập thủ công — 2 profiles cho Auth Service
======================================================
Chạy file này để đăng nhập G2G + Eldorado.
Auth Service sẽ dùng 2 profiles này để capture JWT/cookies.

LƯU Ý: Tắt Bot trước khi dùng!
"""
import time
import os
from shared.driver_manager import get_driver

PROFILES = {
    "1": {
        "name": "chrome_profile_g2g",
        "desc": "G2G — capture JWT",
        "url": "https://www.g2g.com/login",
    },
    "2": {
        "name": "chrome_profile_eldo",
        "desc": "Eldorado — capture cookies",
        "url": "https://www.eldorado.gg/login",
    },
}


def login_single(key: str):
    info = PROFILES[key]
    print(f"\n{'='*50}")
    print(f"  {info['desc']}")
    print(f"  Profile: {info['name']}")
    print(f"  URL: {info['url']}")
    print(f"{'='*50}\n")

    driver = None
    try:
        driver = get_driver(profile_dir=info["name"])
        driver.get(info["url"])
        print("Browser opened. Please:")
        print("  1. Login")
        print("  2. Enter 2FA if needed")
        print("  3. Check 'Remember me'")
        input("\nPress ENTER when done...")
        time.sleep(2)
        print(f"  {info['desc']} saved!")
    except Exception as e:
        print(f"Error: {e}")
    finally:
        if driver:
            driver.quit()


def main():
    print("=" * 50)
    print("  Login Tool — 2 Profiles for Auth Service")
    print("=" * 50)
    print()
    print("  [1] G2G")
    print("  [2] Eldorado")
    print("  [3] Both (G2G then Eldorado)")
    print()

    choice = input("Choose (1-3): ").strip()

    if choice == "3":
        login_single("1")
        print()
        login_single("2")
    elif choice in ("1", "2"):
        login_single(choice)
    else:
        print("Invalid choice")
        return

    print("\nDone. Copy profiles to LXC:")
    print('  scp -r "d:\\Project\\GeGeERPNext\\BotPasteDon\\chrome_profile_g2g" root@192.168.2.220:/opt/BotPasteDon/')
    print('  scp -r "d:\\Project\\GeGeERPNext\\BotPasteDon\\chrome_profile_eldo" root@192.168.2.220:/opt/BotPasteDon/')


if __name__ == "__main__":
    main()
