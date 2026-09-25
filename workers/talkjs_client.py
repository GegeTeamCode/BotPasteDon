"""TalkJS client cho chat Eldorado: WebSocket (tin nhắn text) + đính kèm file thật.

TalkJS có HAI mặt API, và chúng nhận payload khác nhau:

* **Realtime WS** (``wss://realtime.talkjs.com/v1``) — dùng cho text. Tin nhắn
  đi dưới dạng khối ``content``. Gửi file qua đường này thì BẮT BUỘC có
  ``fileToken``, mà route cấp fileToken chỉ mở cho secret key phía server —
  bot chỉ có JWT người dùng nên không đi được.
* **Backend cũ** (``https://app.talkjs.com/api/v0``) — chính là đường trình
  duyệt dùng. ``POST /say/{conversationId}/`` nhận thẳng ``attachment`` kèm URL
  Firebase, rồi TalkJS **tự đúc fileToken**. Đây là đường duy nhất gửi được
  đính kèm thật bằng JWT người dùng.

Hai id trong URL/payload của backend cũ là id NỘI BỘ (SHA-1 rút gọn), không
phải id ngoài — xem :func:`talkjs_internal_id`.
"""

import asyncio
import hashlib
import json
import mimetypes
import os
import re
import unicodedata
import uuid
from typing import Optional, Dict
from dataclasses import dataclass
from urllib.parse import quote

import aiohttp
import websockets

from shared.constants import DEFAULT_USER_AGENT
from shared.logging_config import setup_logger
from shared import media_probe

logger = setup_logger("talkjs")

# Đuôi file được TalkJS phục vụ inline (xem được ngay trong chat) thay vì bắt
# tải về. Chép đúng danh sách trong client TalkJS.
_INLINE_EXTS = {
    "mp4", "webp", "avi", "webm", "wav", "mp3", "oga", "aac", "opus", "gif",
    "jpeg", "jpg", "png", "bmp", "pdf", "txt", "md", "log",
}


def talkjs_internal_id(external_id: str) -> str:
    """Đổi id ngoài (user id / conversation id) sang id nội bộ của TalkJS.

    TalkJS băm SHA-1 rồi lấy 10 byte đầu (20 ký tự hex). Nym còn thêm hậu tố
    ``_n``; conversation thì không. Đã đối chiếu với chính hàm băm trong bundle
    chatbox của TalkJS, khớp tuyệt đối kể cả chuỗi Unicode.
    """
    return hashlib.sha1(external_id.encode("utf-8")).hexdigest()[:20]


@dataclass
class TalkJSConfig:
    app_id: str = "49mLECOW"
    ws_url: str = "wss://realtime.talkjs.com/v1/{app_id}/realtime/{user_id}"
    backend_url: str = "https://app.talkjs.com/api/v0"
    firebase_url: str = "https://firebasestorage.googleapis.com/v0/b/klets-3642/o"
    # Header client-build của backend cũ. Lấy từ tên bundle mà trang chatbox
    # nạp: https://app.talkjs.com/app/{appId}/user/0/inbox/chats → thẻ
    # <script src=".../browser-bundle-release-<hash>.js">. Nếu TalkJS đổi bản
    # và backend bắt đầu từ chối, cập nhật chuỗi này.
    client_build: str = "frontend-release-c964445"


class TalkJSClient:
    def __init__(self, driver=None):
        self.driver = driver
        self.config = TalkJSConfig()
        self.ws: Optional[websockets.WebSocketClientProtocol] = None
        self.auth_token: Optional[str] = None
        self.user_id: Optional[str] = None
        self.session_id: Optional[str] = None
        self._request_id = 0
        self._pending_requests: Dict[int, asyncio.Future] = {}
        self._receive_task: Optional[asyncio.Task] = None
        self._is_connected = False

    def _get_request_id(self) -> int:
        self._request_id += 1
        return self._request_id

    async def extract_auth_from_browser(self, silent: bool = False) -> bool:
        if not self.driver:
            raise ValueError("Driver not initialized")
        try:
            self.driver.switch_to.default_content()
            iframes = self.driver.find_elements("css selector", "iframe[name*='talkjs']")
            if not iframes:
                if not silent:
                    logger.warning("TalkJS iframe not found")
                return False

            iframe_src = iframes[0].get_attribute("src")
            token_match = re.search(r'authToken=([^&]+)', iframe_src)
            if token_match:
                self.auth_token = token_match.group(1)
                logger.debug(f"Auth token extracted (length={len(self.auth_token)})")
            else:
                logger.warning("authToken not found in iframe src")
                return False

            import base64
            try:
                payload = self.auth_token.split('.')[1]
                payload += '=' * (4 - len(payload) % 4)
                decoded = base64.urlsafe_b64decode(payload)
                token_data = json.loads(decoded)
                self.user_id = token_data.get('sub')
                logger.info(f"User ID: {self.user_id}")
            except Exception as e:
                id_match = re.search(r'[&?]id=([^&]+)', iframe_src)
                if id_match:
                    self.user_id = id_match.group(1)
                else:
                    logger.warning(f"Cannot parse user_id: {e}")
                    return False
            return True
        except Exception as e:
            logger.error(f"Auth extraction error: {e}")
            return False

    async def connect(self) -> bool:
        if not self.auth_token or not self.user_id:
            if not await self.extract_auth_from_browser():
                return False
        try:
            # Connect TalkJS Realtime SDK WebSocket
            ws_url = self.config.ws_url.format(
                app_id=self.config.app_id, user_id=self.user_id
            )
            headers = {
                "User-Agent": DEFAULT_USER_AGENT,
                "Origin": "https://www.eldorado.gg",
            }
            self.ws = await websockets.connect(
                ws_url, additional_headers=headers,
                ping_interval=30, ping_timeout=10,
            )
            self._receive_task = asyncio.create_task(self._receive_loop())
            await self._renew_session()

            self._is_connected = True
            logger.info("TalkJS WebSocket connected")
            return True
        except Exception as e:
            logger.error(f"WebSocket connection error: {e}")
            return False

    async def _renew_session(self):
        request_id = self._get_request_id()
        message = [request_id, "POST", "/session/renew", {"token": self.auth_token}, {}]
        await self.ws.send(json.dumps(message))
        response = await self._wait_response(request_id, timeout=10)
        if response:
            logger.info(f"Session renew response: {json.dumps(response)[:500]}")
        if response and len(response) > 2 and response[1] == 200:
            data = response[2] if isinstance(response[2], dict) else {}
            self.session_id = data.get('sessionId')
            logger.info(f"Session renewed: {self.session_id}")
            return True
        return False

    async def _receive_loop(self):
        try:
            async for raw_message in self.ws:
                try:
                    message = json.loads(raw_message)
                    if isinstance(message, list) and len(message) >= 2:
                        request_id = message[0]
                        if request_id in self._pending_requests:
                            future = self._pending_requests.pop(request_id)
                            if not future.done():
                                future.set_result(message)
                except json.JSONDecodeError:
                    pass
                except Exception as e:
                    logger.warning(f"Message processing error: {e}")
        except websockets.ConnectionClosed:
            logger.info("TalkJS WebSocket closed")
            self._is_connected = False
        except Exception as e:
            logger.error(f"Receive loop error: {e}")
            self._is_connected = False

    async def _wait_response(self, request_id: int, timeout: float = 30) -> Optional[list]:
        future = asyncio.Future()
        self._pending_requests[request_id] = future
        try:
            return await asyncio.wait_for(future, timeout=timeout)
        except asyncio.TimeoutError:
            self._pending_requests.pop(request_id, None)
            logger.warning(f"Timeout for request {request_id}")
            return None

    async def send_text_message(self, conversation_id: str, text: str) -> Optional[str]:
        if not self._is_connected:
            return None
        request_id = self._get_request_id()
        message = [
            request_id, "POST",
            f"/conversations/{conversation_id}/messages",
            {"type": "UserMessage", "text": text},
            {},
        ]
        await self.ws.send(json.dumps(message))
        response = await self._wait_response(request_id)
        if response and len(response) > 2 and response[1] == 200:
            msg_id = response[2].get('id')
            logger.info(f"Message sent: {msg_id}")
            return msg_id
        logger.warning(f"WS msg failed: {str(response)[:200] if response else 'timeout'}")
        return None

    @property
    def is_connected(self) -> bool:
        if not self._is_connected or self.ws is None:
            return False
        try:
            if hasattr(self.ws, 'state'):
                return self.ws.state.name == 'OPEN'
            return True
        except:
            return False

    async def close(self):
        self._is_connected = False
        if self._receive_task:
            self._receive_task.cancel()
            try:
                await self._receive_task
            except asyncio.CancelledError:
                pass
        if self.ws:
            await self.ws.close()
            self.ws = None
        logger.info("TalkJS connection closed")

    # ── Đính kèm file ──

    @staticmethod
    def _safe_name(filename: str) -> str:
        """Bỏ dấu tổ hợp khỏi tên file, giống hệt client TalkJS.

        Client chạy ``name.normalize("NFD").replace(/[\\u0300-\\u036f]/g, "")``
        — tức tách dấu ra rồi xoá, nên "đơn.mp4" thành "đon.mp4".
        """
        decomposed = unicodedata.normalize("NFD", filename)
        return "".join(c for c in decomposed if not (0x0300 <= ord(c) <= 0x036F))

    async def upload_file(self, file_path: str,
                          display_name: Optional[str] = None) -> Optional[dict]:
        """Đẩy file lên Firebase Storage đúng cách client TalkJS làm.

        Ba chi tiết phải khớp, nếu không file lên được nhưng hiển thị sai:

        * đường lưu là ``user_files/{appId}/{uuid-không-gạch}/{tên gốc}`` —
          đoạn giữa là UUID ngẫu nhiên, KHÔNG phải conversation id;
        * ``contentDisposition: inline`` cho mp4/png/… để người mua xem ngay
          trong chat thay vì bị tải file về;
        * cờ ``draft: true`` lúc tạo rồi gỡ sau khi finalize.

        ``display_name`` là tên hiển thị cho người mua. Cần truyền vì file
        tải từ ERP nằm ở ``/tmp/erp_evidence_XXXX.mp4``, tên đó vô nghĩa.
        """
        if not os.path.exists(file_path):
            logger.error(f"Không thấy file: {file_path}")
            return None

        filename = self._safe_name(display_name or os.path.basename(file_path))
        ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
        content_type = mimetypes.guess_type(filename)[0] or "application/octet-stream"
        disposition = "inline" if ext in _INLINE_EXTS else "attachment"

        with open(file_path, "rb") as f:
            data = f.read()

        storage_path = f"user_files/{self.config.app_id}/{uuid.uuid4().hex}/{filename}"
        encoded = quote(storage_path, safe="")
        base = self.config.firebase_url

        try:
            async with aiohttp.ClientSession() as session:
                start_body = json.dumps({
                    "name": storage_path,
                    "cacheControl": "private, max-age=86400",
                    "contentType": content_type,
                    "contentDisposition":
                        f"{disposition}; filename*=utf-8''{quote(filename)};",
                    "metadata": {"draft": "true"},
                })
                async with session.post(
                    f"{base}?name={encoded}", data=start_body, headers={
                        "x-goog-upload-protocol": "resumable",
                        "x-goog-upload-command": "start",
                        "x-goog-upload-header-content-length": str(len(data)),
                        "x-goog-upload-header-content-type": content_type,
                        "x-firebase-storage-version": "webjs/9.23.0",
                        "Content-Type": "application/json",
                    },
                ) as resp:
                    upload_url = resp.headers.get("x-goog-upload-url", "")
                    if not upload_url:
                        logger.error(f"Firebase từ chối mở phiên upload: HTTP {resp.status}")
                        return None

                async with session.post(upload_url, data=data, headers={
                    "x-goog-upload-command": "upload, finalize",
                    "x-goog-upload-offset": "0",
                }) as resp:
                    if resp.status != 200:
                        logger.error(f"Firebase finalize lỗi: HTTP {resp.status}")
                        return None
                    result = await resp.json()

                token = result.get("downloadTokens", "")
                if not token:
                    logger.error("Firebase không trả downloadTokens")
                    return None

                file_url = f"{base}/{encoded}?alt=media&token={token}"

                # Gỡ cờ draft — không gỡ thì file vẫn xem được nhưng TalkJS coi
                # là bản nháp và có thể dọn đi.
                try:
                    async with session.patch(
                        file_url, headers={"Content-Type": "application/json"},
                        data=json.dumps({"metadata": {"draft": None}}),
                    ) as resp:
                        if resp.status not in (200, 204):
                            logger.warning(f"Gỡ cờ draft: HTTP {resp.status}")
                except Exception as e:
                    logger.warning(f"Gỡ cờ draft thất bại (không nghiêm trọng): {e}")

            info = media_probe.probe(file_path)
            info["url"] = file_url
            info["filename"] = filename
            logger.info(f"Đã lên Firebase: {filename} ({len(data)} bytes)")
            return info
        except Exception as e:
            logger.error(f"Lỗi upload file: {e}")
            return None

    async def send_attachment(self, conversation_id: str, file_path: str,
                              display_name: Optional[str] = None) -> Optional[str]:
        """Gửi file thành ĐÍNH KÈM THẬT trong chat. Trả về id tin nhắn.

        Phải đi qua backend cũ ``POST /say/{convId}/`` chứ không phải realtime
        WS — lý do ở docstring đầu module. Cần ``session_id`` nên hãy
        :meth:`connect` trước.
        """
        if not self.auth_token or not self.user_id:
            logger.error("Chưa có auth token / user id, không gửi đính kèm được")
            return None
        if not self.session_id:
            logger.error("Chưa có sessionId — phải connect() trước khi gửi đính kèm")
            return None

        info = await self.upload_file(file_path, display_name)
        if not info:
            return None

        attachment = {
            "type": "file",
            "url": info["url"],
            "size": info["size"],
            "filename": info["filename"],
        }
        for key in ("subtype", "width", "height"):
            if info.get(key):
                attachment[key] = info[key]
        if info.get("duration"):
            attachment["duration"] = round(info["duration"])

        payload = {
            "idempotencyKey": uuid.uuid4().hex,
            "entityTree": [],
            "received": False,
            "custom": {},
            "nymId": f"{talkjs_internal_id(self.user_id)}_n",
            "attachment": attachment,
            "location": None,
            "referencedMessageId": None,
        }
        endpoint = (
            f"{self.config.backend_url}/{self.config.app_id}"
            f"/say/{talkjs_internal_id(conversation_id)}/"
            f"?sessionId={self.session_id}"
        )

        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(endpoint, data=json.dumps(payload), headers={
                    "Accept": "application/json",
                    "Content-Type": "application/json",
                    "x-talkjs-client-build": self.config.client_build,
                    "Authorization": f"bearer {self.auth_token}",
                    "Origin": "https://app.talkjs.com",
                    "User-Agent": DEFAULT_USER_AGENT,
                }) as resp:
                    body = await resp.text()
                    if resp.status != 200:
                        logger.warning(
                            f"Gửi đính kèm lỗi: HTTP {resp.status} {body[:200]}")
                        return None
                    msg_id = ""
                    try:
                        msg_id = (json.loads(body) or {}).get("ok", "")
                    except ValueError:
                        pass
                    logger.info(
                        f"Đã gửi đính kèm {info['filename']} "
                        f"({attachment.get('subtype', 'file')}) → {msg_id}")
                    return msg_id or "ok"
        except Exception as e:
            logger.error(f"Lỗi gửi đính kèm: {e}")
            return None
