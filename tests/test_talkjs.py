"""Unit test cho đính kèm TalkJS: hàm băm id + đọc metadata media.

Không mạng, không server, không cần phiên Eldorado.

Các vector băm dưới đây lấy từ chính hàm băm trong bundle chatbox của TalkJS
(module 31 của browser-bundle-release-c964445.js, chạy bằng Node rồi đối chiếu).
Nếu test này đỏ thì đính kèm sẽ hỏng dạng "conversation_not_found" hoặc
"Sender does not exist" — đừng sửa vector, hãy tìm xem ai đụng vào hàm băm.

Run:  python tests/test_talkjs.py
"""

import os
import struct
import sys
import tempfile
import zlib

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from shared import media_probe  # noqa: E402
from workers.talkjs_client import TalkJSClient, talkjs_internal_id  # noqa: E402

# id ngoài → id nội bộ, xác minh bằng chính code TalkJS
HASH_VECTORS = {
    "13c52d0b-0b06-4a66-8b43-6b866f13b77a": "8896f65fd29c51229a3f",  # user GegeTeam
    "a1ba7472-8b55-4397-8ec7-dafcd97da682": "40e33a10970a6e429c63",  # conversation
    "ada2da3a-5a37-4ffd-b4ce-4a22075f8918": "6a66ad2912907fecb0c4",  # conversation
    "6feda118-a1fc-42fe-a0f4-c01bb5f4e8cf": "fcd8b48055848545e7f3",  # conversation
    "ABC-Mixed-Case-123": "16ea3da2188916ab353a",                    # giữ nguyên hoa/thường
    "co-dau-tiếng-việt": "c7f3be2428ab8e8dd679",                     # UTF-8, không chuẩn hoá
    "x": "11f6ad8ec52a2984abaa",
}


def test_internal_id():
    for external, expected in HASH_VECTORS.items():
        got = talkjs_internal_id(external)
        assert got == expected, f"{external}: mong đợi {expected}, nhận {got}"
        assert len(got) == 20, f"id nội bộ phải dài 20 ký tự hex, nhận {len(got)}"
    print(f"✓ talkjs_internal_id: {len(HASH_VECTORS)} vector khớp")


def test_safe_name():
    # Client TalkJS tách NFD rồi xoá dấu tổ hợp
    assert TalkJSClient._safe_name("đơn hàng.mp4") == "đon hang.mp4"
    assert TalkJSClient._safe_name("plain.png") == "plain.png"
    print("✓ _safe_name: bỏ dấu tổ hợp giống client")


def _make_png(w, h):
    raw = b""
    for y in range(h):
        raw += b"\x00" + bytes([200, 100, 50]) * w

    def chunk(t, d):
        c = t + d
        return struct.pack(">I", len(d)) + c + struct.pack(">I", zlib.crc32(c) & 0xFFFFFFFF)

    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw, 1))
            + chunk(b"IEND", b""))


def test_probe_png():
    with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
        f.write(_make_png(320, 200))
        path = f.name
    try:
        info = media_probe.probe(path)
        assert info["subtype"] == "image", info
        assert (info.get("width"), info.get("height")) == (320, 200), info
        assert info["size"] > 0
        print("✓ media_probe: đọc đúng 320x200 từ PNG")
    finally:
        os.unlink(path)


def test_probe_unknown_never_raises():
    with tempfile.NamedTemporaryFile(suffix=".bin", delete=False) as f:
        f.write(b"khong phai media")
        path = f.name
    try:
        info = media_probe.probe(path)
        assert info["size"] == 16, info
        assert info["subtype"] is None, info
        assert "width" not in info
        print("✓ media_probe: file lạ không ném lỗi, chỉ thiếu số đo")
    finally:
        os.unlink(path)


def test_subtype_map():
    assert media_probe.subtype_for("a.MP4") == "video"
    assert media_probe.subtype_for("a.jpeg") == "image"
    assert media_probe.subtype_for("a.mp3") == "audio"
    assert media_probe.subtype_for("a.zip") is None
    assert media_probe.subtype_for("khong-co-duoi") is None
    print("✓ subtype_for: ánh xạ đuôi file đúng")


if __name__ == "__main__":
    test_internal_id()
    test_safe_name()
    test_probe_png()
    test_probe_unknown_never_raises()
    test_subtype_map()
    print("\nTẤT CẢ ĐỀU XANH")
