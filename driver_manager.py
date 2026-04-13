import os
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from webdriver_manager.chrome import ChromeDriverManager

def get_driver(profile_dir="chrome_profile"):
    options = Options()
    
    # 1. Cấu hình Profile (Giữ nguyên logic của bạn)
    current_path = os.getcwd()
    profile_path = os.path.join(current_path, profile_dir)
    options.add_argument(f"--user-data-dir={profile_path}")
    
    # 2. ANTI-DETECTION (CỰC KỲ QUAN TRỌNG CHO G2G/ELDORADO)
    # Tắt thông báo "Chrome is being controlled by automated software"
    options.add_experimental_option("excludeSwitches", ["enable-automation"])
    options.add_experimental_option('useAutomationExtension', False)
    
    # Che giấu dấu vết Selenium (Tránh bị web phát hiện là Bot)
    options.add_argument("--disable-blink-features=AutomationControlled")
    
    # 3. Cấu hình hiển thị & Hiệu năng
    options.add_argument("--start-maximized") # Luôn mở to cửa sổ để Element không bị che khuất
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--disable-gpu")
    options.add_argument("--log-level=3") # Tắt bớt log rác của Chrome trong Console

    # 4. Fix lỗi xung đột Port khi chạy 2 trình duyệt song song
    # (Đôi khi chạy 2 cái cùng lúc sẽ bị tranh nhau port debug)
    options.add_argument("--remote-debugging-port=0") 

    # Khởi tạo Driver
    try:
        service = Service(ChromeDriverManager().install())
        driver = webdriver.Chrome(service=service, options=options)
        
        # Xóa thuộc tính navigator.webdriver (Một lớp bảo vệ nữa)
        driver.execute_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined})")
        
        return driver
    except Exception as e:
        print(f"❌ Lỗi khởi tạo Chrome Driver: {e}")
        print("💡 Gợi ý: Hãy thử cập nhật Chrome hoặc tắt các cửa sổ Chrome đang chạy.")
        raise e