"""Trạng thái API G2G có đăng nhập, chia sẻ giữa các tiến trình qua một file JSON.

Scanner G2G gọi `record_g2g_api()` sau mỗi vòng quét; dashboard đọc lại bằng
`read_g2g_api_health()` và trả trong `/api/status` → GeGe Console (.11) mở cảnh
báo đỏ `paste_g2g_api` khi lỗi kéo dài.

Vì sao có module này: 23/10/2026 G2G (AWS WAF) chặn IP của .220 ~48 phút. Mọi
lệnh có JWT trả 403 `ForbiddenException` trong khi auth vẫn `backend refresh OK`
và `/health` xanh; scanner nuốt 403 thành danh sách rỗng nên không ai biết —
6 đơn kẹt bước báo giao, 5 đơn mới không vào ERP.

Mốc thời gian trong file là epoch (giây) để bên đọc khỏi dính lệch múi giờ.
Stdlib-only, không bao giờ raise — gọi được từ mọi vòng lặp.
"""

import json
import os
import threading
import time

from shared.alerts import clear_ops_alert, send_ops_alert
from shared.config import DATABASE_PATH
from shared.logging_config import setup_logger

logger = setup_logger("g2g.api_health")

G2G_API_HEALTH_PATH = os.path.join(os.path.dirname(DATABASE_PATH) or ".", "g2g_api_health.json")
ALERT_KEY = "g2g-api-auth"
# 401 lúc JWT vừa xoay thường tự hết sau 1 vòng quét (10-15s) → chỉ báo khi lỗi
# liên tục đủ lâu.
ALERT_AFTER = 180
# Ghi lại file dù trạng thái không đổi, để bên đọc phân biệt "vẫn OK" với
# "scanner chết, file đứng im".
WRITE_EVERY = 60
STALE_AFTER = 300

_lock = threading.Lock()
_state = {"ok": True, "since": None, "status": None, "error": "", "last_ok": None}
_alerted = False
_last_write = 0.0


def _write(snapshot: dict, path: str) -> None:
    tmp = path + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(snapshot, f)
        os.replace(tmp, path)
    except Exception as e:
        logger.warning("[G2G] không ghi được %s: %s", path, e)


def record_g2g_api(ok: bool, status=None, error: str = "", path: str = None) -> None:
    """Ghi kết quả một lệnh G2G có JWT (ok=False khi AuthError / không lấy được JWT).

    Lỗi liên tục từ ALERT_AFTER giây → OPS ALERT `g2g-api-auth` một lần cho cả đợt;
    lần OK đầu tiên sau đó tự gỡ."""
    global _alerted, _last_write
    path = path or G2G_API_HEALTH_PATH
    try:
        now = time.time()
        fire = recovered = False
        with _lock:
            prev_ok, prev_status = _state["ok"], _state["status"]
            if ok:
                if not prev_ok:
                    recovered = _alerted
                    logger.info("[G2G] API có đăng nhập chạy lại sau %ds lỗi",
                                int(now - (_state["since"] or now)))
                _state.update(ok=True, since=None, status=None, error="", last_ok=now)
                _alerted = False
            else:
                if prev_ok:
                    _state["since"] = now
                _state.update(ok=False, status=status, error=(error or "")[:300])
                if not _alerted and now - _state["since"] >= ALERT_AFTER:
                    _alerted = fire = True
            changed = ok != prev_ok or _state["status"] != prev_status
            if not (changed or fire or now - _last_write >= WRITE_EVERY):
                return
            _last_write = now
            snapshot = dict(_state, updated_at=now)
            failing_for = int(now - _state["since"]) if _state["since"] else 0
        _write(snapshot, path)
        if fire:
            code = f"HTTP {status}" if status else "không lấy được JWT"
            hint = (" — 403 thường là G2G chặn IP .220: thử cùng JWT từ IP khác để chắc"
                    if status == 403 else "")
            send_ops_alert(
                ALERT_KEY,
                f"[G2G] API có đăng nhập lỗi {code} liên tục {failing_for // 60} phút: đơn G2G mới "
                f"không vào ERP, đơn đã giao không báo được lên G2G{hint}. Lỗi: {error[:200]}",
            )
        if recovered:
            clear_ops_alert(ALERT_KEY)
            logger.warning("OPS ALERT CLEARED [%s]", ALERT_KEY)
    except Exception as e:
        logger.warning("[G2G] record_g2g_api lỗi: %s", e)


def read_g2g_api_health(path: str = None) -> dict:
    """Trạng thái cho dashboard. `state`: ok | failing | stale | unknown."""
    path = path or G2G_API_HEALTH_PATH
    try:
        with open(path, encoding="utf-8") as f:
            d = json.load(f)
    except FileNotFoundError:
        return {"state": "unknown", "alert_after": ALERT_AFTER}
    except Exception as e:
        return {"state": "unknown", "error": f"đọc file lỗi: {e}", "alert_after": ALERT_AFTER}
    now = time.time()
    updated = float(d.get("updated_at") or 0)
    ok = bool(d.get("ok"))
    since = d.get("since")
    out = {
        "ok": ok,
        "http_status": d.get("status"),
        "error": d.get("error") or "",
        "since": since,
        "last_ok": d.get("last_ok"),
        "updated_at": updated,
        "age_seconds": int(now - updated),
        "failing_seconds": int(now - since) if not ok and since else 0,
        "alert_after": ALERT_AFTER,
    }
    out["state"] = "stale" if now - updated > STALE_AFTER else ("ok" if ok else "failing")
    return out
