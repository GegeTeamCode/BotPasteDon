# config.py
from dotenv import load_dotenv
import os

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")

# Đọc list từ chuỗi, tách bằng dấu phẩy
CHANNEL_IDS = [
    int(x) for x in os.getenv("CHANNEL_IDS", "").split(",") if x
]

# 3. Cấu hình Auto Scanner (Quét đơn hàng tự động)
SCANNER_CONFIG = {
    # Tự động bật scanner khi bot khởi động
    "auto_start": True,

    # Whitelist: Chỉ lấy đơn hàng chứa các từ này (để trống = lấy tất cả)
    "whitelist": "Divine Orb, Chaos Orb, Mirror of Kalandra, Gold, Boss Materials, Runes, Currency, Gems",

    # Blacklist: Bỏ qua đơn hàng chứa các từ này
    "blacklist": "Boosting, Leveling, Account",

    # Platform được bật
    "platforms": {
        "g2g": True,
        "eldorado": True
    },

    # Thời gian giữa các lần quét (giây)
    "scan_interval_min": 15,
    "scan_interval_max": 25,

    # Webhook URLs - Mapping theo game
    # QUAN TRỌNG: Thứ tự quan trọng, game cụ thể để trước!
    "webhooks": {
        # Default: Kênh Diablo 4 (fallback)
        "default": "https://discord.com/api/webhooks/1459307138970157212/VdvkJJ9DLpfNr25fKAmV9DAg0RDga8Dk-X8skjs1jYetiT3S1C0BAxtpcMFFrG2zaRpu",

        "mappings": [
            # Diablo 4 - Kênh Diablo 4
            {
                "game": "Diablo 4",
                "keywords": ["diablo 4", "diablo iv", "d4"],
                "url": "https://discord.com/api/webhooks/1459307138970157212/VdvkJJ9DLpfNr25fKAmV9DAg0RDga8Dk-X8skjs1jYetiT3S1C0BAxtpcMFFrG2zaRpu"
            },
            # Path of Exile 2 - Kênh POE2
            {
                "game": "Path of Exile 2",
                "keywords": ["poe2", "path of exile 2", "poe 2"],
                "url": "https://discord.com/api/webhooks/1459316628285161665/0kI4Yuz6L4uhlLwBPtMil6S4VnQM88K2sCcbWS_NaUYdZegydaNFFUeFvJeYQbyesu1o"
            },
            # Path of Exile 1 - Kênh POE1
            {
                "game": "Path of Exile",
                "keywords": ["path of exile", "poe1", "poe 1"],
                "url": "https://discord.com/api/webhooks/1466723389128310926/lWfmVwh4i-4ckIbjGNH3ujJSYOtTa8ECGoOORZzFDlyGS9_7GfZv9zQWnra4xonr5KJH"
            }
        ]
    },

    # Các trường hiển thị khi gửi (theo extension config)
    "fields": {
        "showLabels": False,
        "platform": True,
        "customerName": True,
        "orderId": True,
        "game": False,      # Tắt theo yêu cầu
        "server": False,    # Tắt theo yêu cầu
        "itemName": True,
        "quantity": True,
        "character": True,
        "price": False,     # Tắt theo yêu cầu
        "url": True
    }
}
