"""
Tool đăng nhập thủ công cho 4 Chrome Profiles
=============================================
Chạy file này để đăng nhập vào Eldorado/G2G cho cả Scanner và Worker.

LƯU Ý: Tắt Bot trước khi dùng tool này!
"""

import time
import os
from driver_manager import get_driver

# Danh sách 4 profiles
PROFILES = {
    "1": {
        "name": "chrome_profile_eldo_worker",
        "desc": "📦 Worker Eldorado (Trả hàng)",
        "url": "https://www.eldorado.gg/login",
        "platform": "ELDORADO"
    },
    "2": {
        "name": "chrome_profile_g2g_worker",
        "desc": "📦 Worker G2G (Trả hàng)",
        "url": "https://www.g2g.com/login",
        "platform": "G2G"
    },
    "3": {
        "name": "chrome_profile_eldo_scanner",
        "desc": "🔍 Scanner Eldorado (Quét đơn)",
        "url": "https://www.eldorado.gg/login",
        "platform": "ELDORADO"
    },
    "4": {
        "name": "chrome_profile_g2g_scanner",
        "desc": "🔍 Scanner G2G (Quét đơn)",
        "url": "https://www.g2g.com/login",
        "platform": "G2G"
    },
    "5": {
        "name": "ALL",
        "desc": "🚀 Mở TẤT CẢ 4 profiles (lần lượt)",
        "url": "",
        "platform": "ALL"
    }
}

def print_header():
    os.system('cls' if os.name == 'nt' else 'clear')
    print("=" * 60)
    print("   🔐 TOOL ĐĂNG NHẬP THỦ CÔNG - 4 PROFILES")
    print("=" * 60)
    print()
    print("⚠️  LƯU Ý: TẮT BOT TRƯỚC KHI DÙNG TOOL NÀY!")
    print()
    print("📋 Danh sách Profiles:")
    print("-" * 60)
    for key, info in PROFILES.items():
        if key != "5":
            print(f"   [{key}] {info['desc']}")
            print(f"       Thư mục: {info['name']}")
    print("-" * 60)
    print(f"   [5] {PROFILES['5']['desc']}")
    print("-" * 60)
    print()

def login_single(profile_key: str):
    """Đăng nhập 1 profile"""
    info = PROFILES[profile_key]

    print(f"\n{'='*50}")
    print(f"🚀 Khởi động: {info['desc']}")
    print(f"📁 Profile: {info['name']}")
    print(f"🔗 URL: {info['url']}")
    print(f"{'='*50}\n")

    driver = None
    try:
        driver = get_driver(info["name"])
        driver.get(info["url"])

        print("✅ Trình duyệt đã mở!")
        print()
        print("📝 HƯỚNG DẪN:")
        print(f"   1. Đăng nhập vào {info['platform']}")
        print("   2. Nhập mã 2FA nếu có")
        print("   3. Tích vào 'Remember me' / 'Stay signed in'")
        print("   4. Kiểm tra đã login thành công")
        print()
        print("⛔ ĐỪNG TẮT CỬA SỔ NÀY!")
        print("-" * 50)

        input("⌨️  BẤM [ENTER] KHI ĐÃ ĐĂNG NHẬP XONG...\n")

        print("⏳ Đang lưu cookies và đóng...")

        # Thêm chút delay để đảm bảo cookies được lưu
        time.sleep(2)

        print(f"✅ [{info['platform']}] Đã lưu thành công!")

    except Exception as e:
        print(f"❌ Lỗi: {e}")
    finally:
        if driver:
            driver.quit()

def login_all():
    """Đăng nhập tất cả 4 profiles lần lượt"""
    print("\n🚀 SẼ MỒ LẦN LƯỢT 4 PROFILES:")
    print("   1. Worker Eldorado")
    print("   2. Worker G2G")
    print("   3. Scanner Eldorado")
    print("   4. Scanner G2G")
    print()

    confirm = input("👉 Tiếp tục? (y/n): ").strip().lower()
    if confirm != 'y':
        print("❌ Đã hủy.")
        return

    for key in ["1", "2", "3", "4"]:
        login_single(key)

        if key != "4":  # Không hỏi ở profile cuối
            print("\n⏳ Chờ 3 giây trước profile tiếp theo...")
            time.sleep(3)

    print("\n" + "=" * 60)
    print("🎉 HOÀN THÀNH! Đã đăng nhập xong 4 profiles.")
    print("✅ Giờ bạn có thể chạy Bot: python GegeOrder.py")
    print("=" * 60)

def check_profile_exists(profile_name: str) -> bool:
    """Kiểm tra thư mục profile có tồn tại không"""
    profile_path = os.path.join(os.getcwd(), profile_name)
    return os.path.exists(profile_path)

def main():
    print_header()

    choice = input("👉 Chọn profile (1-5): ").strip()

    if choice == "5":
        login_all()
    elif choice in ["1", "2", "3", "4"]:
        login_single(choice)
    else:
        print("❌ Lựa chọn không hợp lệ!")
        return

    print("\n⏳ Đang thoát...")
    time.sleep(1)

if __name__ == "__main__":
    main()
