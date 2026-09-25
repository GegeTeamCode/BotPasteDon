"""Đọc kích thước/thời lượng của ảnh và video bằng cách phân tích header.

Không phụ thuộc ffmpeg/Pillow — worker chỉ cần vài con số để gắn vào
attachment TalkJS (width/height/duration), nên parse header là đủ và rẻ.

Thiếu số đo không phải lỗi: TalkJS chấp nhận attachment không có
width/height/duration, chỉ là thumbnail bị giật một nhịp khi tải.
"""

import os
import struct
from typing import Optional, Tuple

# Đuôi file → subtype của TalkJS. Suy từ chính client TalkJS (hàm sendFile):
# voice/video/image/audio, còn lại không có subtype.
VIDEO_EXTS = {"mp4", "mov", "webm", "avi", "mkv", "m4v"}
IMAGE_EXTS = {"png", "jpg", "jpeg", "gif", "bmp", "webp"}
AUDIO_EXTS = {"mp3", "wav", "oga", "aac", "opus", "m4a"}

# Box MP4 có thể chứa box con
_MP4_CONTAINERS = {b"moov", b"trak", b"mdia", b"minf", b"stbl", b"udta"}


def subtype_for(filename: str) -> Optional[str]:
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    if ext in VIDEO_EXTS:
        return "video"
    if ext in IMAGE_EXTS:
        return "image"
    if ext in AUDIO_EXTS:
        return "audio"
    return None


def _probe_mp4(path: str) -> dict:
    out: dict = {}

    def walk(f, end: int, depth: int):
        while f.tell() < end - 7:
            start = f.tell()
            hdr = f.read(8)
            if len(hdr) < 8:
                return
            size, typ = struct.unpack(">I4s", hdr)
            hsize = 8
            if size == 1:
                size = struct.unpack(">Q", f.read(8))[0]
                hsize = 16
            elif size == 0:
                size = end - start
            if size < hsize:
                return
            stop = start + size
            if typ == b"mvhd":
                d = f.read(size - hsize)
                if len(d) >= 20:
                    ver = d[0]
                    if ver == 1 and len(d) >= 32:
                        scale, dur = struct.unpack(">IQ", d[20:32])
                    else:
                        scale, dur = struct.unpack(">II", d[12:20])
                    if scale:
                        out["duration"] = dur / scale
            elif typ == b"stsd":
                d = f.read(size - hsize)
                if len(d) >= 48:
                    fmt = d[12:16].decode("latin1", "replace")
                    if fmt in ("avc1", "hvc1", "hev1", "vp09", "av01"):
                        w, h = struct.unpack(">HH", d[40:44])
                        if w and h:
                            out["width"], out["height"] = w, h
            elif typ in _MP4_CONTAINERS and depth < 6:
                walk(f, stop, depth + 1)
            f.seek(stop)

    with open(path, "rb") as f:
        walk(f, os.path.getsize(path), 0)
    return out


def _probe_png(head: bytes) -> Optional[Tuple[int, int]]:
    if len(head) >= 24 and head[12:16] == b"IHDR":
        return struct.unpack(">II", head[16:24])
    return None


def _probe_gif(head: bytes) -> Optional[Tuple[int, int]]:
    if len(head) >= 10:
        return struct.unpack("<HH", head[6:10])
    return None


def _probe_jpeg(path: str) -> Optional[Tuple[int, int]]:
    with open(path, "rb") as f:
        if f.read(2) != b"\xff\xd8":
            return None
        while True:
            b = f.read(1)
            while b and b != b"\xff":
                b = f.read(1)
            if not b:
                return None
            marker = f.read(1)
            while marker == b"\xff":
                marker = f.read(1)
            if not marker:
                return None
            m = marker[0]
            # SOF0..SOF15, bỏ qua DHT(c4)/JPG(c8)/DAC(cc) vốn không mang kích thước
            if 0xC0 <= m <= 0xCF and m not in (0xC4, 0xC8, 0xCC):
                seg = f.read(7)
                if len(seg) < 7:
                    return None
                h, w = struct.unpack(">HH", seg[3:7])
                return w, h
            seg_len = f.read(2)
            if len(seg_len) < 2:
                return None
            f.seek(struct.unpack(">H", seg_len)[0] - 2, os.SEEK_CUR)


def probe(path: str) -> dict:
    """Trả về {size, subtype, width?, height?, duration?}.

    Không bao giờ ném lỗi — file lạ chỉ đơn giản là thiếu số đo.
    """
    info: dict = {"size": os.path.getsize(path)}
    name = os.path.basename(path)
    info["subtype"] = subtype_for(name)

    try:
        with open(path, "rb") as f:
            head = f.read(32)

        if head.startswith(b"\x89PNG\r\n\x1a\n"):
            wh = _probe_png(head)
        elif head.startswith(b"GIF8"):
            wh = _probe_gif(head)
        elif head.startswith(b"\xff\xd8"):
            wh = _probe_jpeg(path)
        elif len(head) >= 8 and head[4:8] == b"ftyp":
            mp4 = _probe_mp4(path)
            info.update(mp4)
            wh = None
        else:
            wh = None

        if wh:
            info["width"], info["height"] = wh
    except Exception:
        pass  # thiếu số đo không phải lỗi

    return info
