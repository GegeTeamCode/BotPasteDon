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
    "blacklist": "Boosting, Leveling, Account, Custom oder",

    # Title mapping: Ghi đè itemName khi đơn G2G có title khớp pattern
    # "title_pattern": chuỗi cần chứa trong title (không phân biệt hoa/thường)
    # "display_name": itemName sẽ bị thay thế bằng giá trị này
    "G2G_TITLE_MAP": [
        {
            "title_pattern": "Any Grand Gems",
            "display_name": "Custom - Grand Gems"
        },
        {
            "title_pattern": "Flawless Horadric",
            "display_name": "Custom - Flawless Horadric Gems"
        }
    ],

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
        "default": os.getenv("WEBHOOK_DEFAULT", ""),

        "mappings": [
            # Diablo 4 - Kênh Diablo 4
            {
                "game": "Diablo 4",
                "keywords": ["diablo 4", "diablo iv", "d4"],
                "url": os.getenv("WEBHOOK_DIABLO4", "")
            },
            # Path of Exile 2 - Kênh POE2
            {
                "game": "Path of Exile 2",
                "keywords": ["poe2", "path of exile 2", "poe 2"],
                "url": os.getenv("WEBHOOK_POE2", "")
            },
            # Path of Exile 1 - Kênh POE1
            {
                "game": "Path of Exile",
                "keywords": ["path of exile", "poe1", "poe 1"],
                "url": os.getenv("WEBHOOK_POE1", "")
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
