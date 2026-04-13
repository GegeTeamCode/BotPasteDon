# 🤖 GeGe Order Auto - Discord Automation Bot

Bot tự động hóa quy trình trả đơn hàng trên nền tảng **Eldorado** và **G2G** thông qua giao diện Discord. Hệ thống sử dụng Selenium với cơ chế "2 Động cơ" (2 trình duyệt chạy song song) để tối ưu hóa tốc độ.

## ✨ Tính năng chính

* **Đa luồng (Multi-Worker):** Xử lý Eldorado và G2G trên 2 trình duyệt riêng biệt, không bị chặn lẫn nhau.
* **Eldorado Fast Mode:** Nút "⚡ Khách vào" ưu tiên cực cao (Priority 0). Tự động F5 liên tục để xác nhận đơn hàng trong tích tắc.
* **Auto Upload & Chat:** Tự động upload bằng chứng (ảnh/video) và gửi lời cảm ơn vào khung chat của sàn.
* **Thông minh:** Tự động phát hiện Iframe (TalkJS), tự động chờ overlay biến mất, tự động kiểm tra trạng thái đơn hàng.
* **Quản lý Discord:** Tự động tạo Thread, khóa và lưu trữ Thread sau khi hoàn thành.

## 🛠️ Yêu cầu hệ thống

1.  **Python 3.10+**
2.  **Google Chrome** (Phiên bản mới nhất)
3.  **Tài khoản Discord Bot** (Có Token)

## 📂 Cấu trúc thư mục

```text
Gege_Order/
├── chrome_profile/         # Profile Chrome cho Eldorado (Tự tạo hoặc copy từ cũ)
├── chrome_profile_g2g/     # Profile Chrome cho G2G (Tự sinh ra khi chạy lần đầu)
├── proofs/                 # Thư mục lưu ảnh tạm (Tự sinh ra)
├── config.py               # File cấu hình Token & Channel
├── manual_login.py         # Tool mở profile và login thủ công
├── driver_manager.py       # Quản lý Chrome Driver & Profile
├── gegeorder.py            # File chạy chính (Main)
├── message.txt             # Nội dung tin nhắn chat (Tùy chọn)
├── order_queue.py          # Logic xử lý hàng đợi & Selenium
├── requirements.txt        # Danh sách thư viện
└── README.md               # Hướng dẫn sử dụng