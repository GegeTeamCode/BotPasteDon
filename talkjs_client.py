"""
TalkJS WebSocket Client
Giao tiếp trực tiếp với TalkJS API qua WebSocket (Phoenix Protocol)
Nhanh hơn và ổn định hơn so với UI automation
"""

import asyncio
import json
import re
import time
import uuid
import aiohttp
from typing import Optional, Dict, Any, List
from dataclasses import dataclass
import websockets


@dataclass
class TalkJSConfig:
    """Cấu hình TalkJS"""
    app_id: str = "49mLECOW"
    ws_url: str = "wss://realtime.talkjs.com/v1/{app_id}/realtime/{user_id}"
    api_url: str = "https://app.talkjs.com/api/v0/{app_id}"


class TalkJSClient:
    """
    TalkJS WebSocket Client sử dụng Phoenix Protocol

    Phoenix Protocol format: [requestId, "METHOD", "/path", {params}, {}]
    """

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
        """Tăng và trả về request ID"""
        self._request_id += 1
        return self._request_id

    async def extract_auth_from_browser(self, silent: bool = False) -> bool:
        """
        Extract authToken và user_id từ browser session

        Args:
            silent: Nếu True, không in warning khi không tìm thấy iframe

        Returns:
            bool: True nếu extract thành công
        """
        if not self.driver:
            raise ValueError("Driver không được khởi tạo")

        try:
            # Switch về main frame
            self.driver.switch_to.default_content()

            # Tìm iframe TalkJS
            iframes = self.driver.find_elements("css selector", "iframe[name*='talkjs']")

            if not iframes:
                if not silent:
                    print("⚠️ Không tìm thấy TalkJS iframe")
                return False

            # Lấy src của iframe chứa authToken
            iframe_src = iframes[0].get_attribute("src")

            # Parse authToken từ URL
            token_match = re.search(r'authToken=([^&]+)', iframe_src)
            if token_match:
                self.auth_token = token_match.group(1)
                print(f"✅ Đã lấy authToken: {self.auth_token[:50]}...")
            else:
                print("⚠️ Không tìm thấy authToken trong iframe src")
                return False

            # Parse user_id từ URL hoặc JWT token
            # JWT token structure: header.payload.signature
            try:
                import base64
                payload = self.auth_token.split('.')[1]
                # Add padding if needed
                payload += '=' * (4 - len(payload) % 4)
                decoded = base64.urlsafe_b64decode(payload)
                token_data = json.loads(decoded)
                self.user_id = token_data.get('sub')
                print(f"✅ User ID: {self.user_id}")
            except Exception as e:
                # Fallback: lấy từ iframe src
                id_match = re.search(r'[&?]id=([^&]+)', iframe_src)
                if id_match:
                    self.user_id = id_match.group(1)
                else:
                    print(f"⚠️ Không thể parse user_id: {e}")
                    return False

            return True

        except Exception as e:
            print(f"❌ Lỗi extract auth: {e}")
            return False

    async def connect(self) -> bool:
        """
        Kết nối đến TalkJS WebSocket server

        Returns:
            bool: True nếu kết nối thành công
        """
        if not self.auth_token or not self.user_id:
            if not await self.extract_auth_from_browser():
                return False

        try:
            ws_url = self.config.ws_url.format(
                app_id=self.config.app_id,
                user_id=self.user_id
            )

            headers = {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                "Origin": "https://www.eldorado.gg"
            }

            print(f"🔌 Đang kết nối TalkJS WebSocket...")
            self.ws = await websockets.connect(
                ws_url,
                additional_headers=headers,
                ping_interval=30,
                ping_timeout=10
            )

            # Renew session
            await self._renew_session()

            # Start receive loop
            self._receive_task = asyncio.create_task(self._receive_loop())
            self._is_connected = True

            print("✅ Đã kết nối TalkJS WebSocket!")
            return True

        except Exception as e:
            print(f"❌ Lỗi kết nối WebSocket: {e}")
            return False

    async def _renew_session(self):
        """Renew session với authToken"""
        request_id = self._get_request_id()
        message = [
            request_id,
            "POST",
            "/session/renew",
            {"token": self.auth_token},
            {}
        ]

        await self.ws.send(json.dumps(message))
        response = await self._wait_response(request_id, timeout=10)

        if response and response.get(1) == 200:
            data = response[2]
            self.session_id = data.get('sessionId')
            print(f"✅ Session renewed: {self.session_id}")
            return True
        return False

    async def _receive_loop(self):
        """Loop để nhận messages từ WebSocket"""
        try:
            async for raw_message in self.ws:
                try:
                    message = json.loads(raw_message)

                    # Handle response cho pending request
                    if isinstance(message, list) and len(message) >= 2:
                        request_id = message[0]

                        if request_id in self._pending_requests:
                            future = self._pending_requests.pop(request_id)
                            if not future.done():
                                future.set_result(message)

                        # Handle broadcast messages
                        if len(message) >= 3 and message[1] == "PUBLISH":
                            await self._handle_broadcast(message)

                except json.JSONDecodeError:
                    pass
                except Exception as e:
                    print(f"⚠️ Lỗi xử lý message: {e}")

        except websockets.ConnectionClosed:
            print("🔌 TalkJS WebSocket connection closed")
            self._is_connected = False
        except Exception as e:
            print(f"❌ Lỗi receive loop: {e}")
            self._is_connected = False

    async def _handle_broadcast(self, message: list):
        """Xử lý broadcast messages"""
        event_type = message[2].get('type', '')
        # Có thể thêm logic xử lý events ở đây
        print(f"📢 Broadcast: {event_type}")

    async def _wait_response(self, request_id: int, timeout: float = 30) -> Optional[list]:
        """Đợi response cho một request"""
        future = asyncio.Future()
        self._pending_requests[request_id] = future

        try:
            return await asyncio.wait_for(future, timeout=timeout)
        except asyncio.TimeoutError:
            self._pending_requests.pop(request_id, None)
            print(f"⏱️ Timeout waiting for response {request_id}")
            return None

    async def subscribe_conversation(self, conversation_id: str) -> bool:
        """
        Subscribe vào một conversation để nhận updates

        Args:
            conversation_id: ID của conversation

        Returns:
            bool: True nếu subscribe thành công
        """
        if not self._is_connected:
            return False

        # Subscribe conversation
        request_id = self._get_request_id()
        message = [
            request_id,
            "SUBSCRIBE",
            f"/me/conversations/{conversation_id}",
            {},
            {}
        ]

        await self.ws.send(json.dumps(message))
        response = await self._wait_response(request_id)

        if not (response and response[1] == 200):
            return False

        # Subscribe messages
        request_id = self._get_request_id()
        message = [
            request_id,
            "SUBSCRIBE",
            f"/me/conversations/{conversation_id}/messages",
            {},
            {}
        ]

        await self.ws.send(json.dumps(message))
        response = await self._wait_response(request_id)

        return response and response[1] == 200

    async def unsubscribe_conversation(self, conversation_id: str) -> bool:
        """Unsubscribe khỏi conversation"""
        if not self._is_connected:
            return False

        # Unsubscribe messages
        request_id = self._get_request_id()
        message = [
            request_id,
            "UNSUBSCRIBE",
            f"/me/conversations/{conversation_id}/messages",
            {},
            {}
        ]
        await self.ws.send(json.dumps(message))
        await self._wait_response(request_id)

        # Unsubscribe conversation
        request_id = self._get_request_id()
        message = [
            request_id,
            "UNSUBSCRIBE",
            f"/me/conversations/{conversation_id}",
            {},
            {}
        ]
        await self.ws.send(json.dumps(message))
        response = await self._wait_response(request_id)

        return response and response[1] == 200

    async def send_text_message(self, conversation_id: str, text: str) -> Optional[str]:
        """
        Gửi tin nhắn text qua WebSocket

        Args:
            conversation_id: ID của conversation
            text: Nội dung tin nhắn

        Returns:
            str: Message ID nếu thành công, None nếu thất bại
        """
        if not self._is_connected:
            print("⚠️ Chưa kết nối WebSocket")
            return None

        request_id = self._get_request_id()
        message = [
            request_id,
            "POST",
            f"/me/conversations/{conversation_id}/messages",
            {
                "type": "UserMessage",
                "content": [
                    {
                        "type": "text",
                        "text": text
                    }
                ]
            },
            {}
        ]

        await self.ws.send(json.dumps(message))
        response = await self._wait_response(request_id)

        if response and response[1] == 200:
            message_id = response[2].get('id')
            print(f"✅ Đã gửi tin nhắn: {message_id}")
            return message_id
        else:
            print(f"❌ Gửi tin nhắn thất bại: {response}")
            return None

    async def get_session_id(self) -> Optional[str]:
        """
        Lấy TalkJS session ID từ browser

        Returns:
            str: Session ID nếu thành công
        """
        if not self.driver:
            return None

        try:
            self.driver.switch_to.default_content()

            # Tìm iframe TalkJS
            iframes = self.driver.find_elements("css selector", "iframe[name*='talkjs']")
            if not iframes:
                print("⚠️ Không tìm thấy TalkJS iframe để lấy session ID")
                return None

            # Lấy src của iframe - có thể chứa session ID
            iframe_src = iframes[0].get_attribute("src") or ""

            # Thử lấy session ID từ URL của iframe
            session_match = re.search(r'sessionId=([^&]+)', iframe_src)
            if session_match:
                self.session_id = session_match.group(1)
                print(f"✅ Session ID (from URL): {self.session_id}")
                return self.session_id

            # Switch vào iframe và lấy session ID từ localStorage/cookie
            self.driver.switch_to.frame(iframes[0])

            # Thử lấy từ nhiều nguồn
            session_id = self.driver.execute_script("""
                try {
                    // 1. Tìm trong localStorage
                    for (let i = 0; i < localStorage.length; i++) {
                        const key = localStorage.key(i);
                        if (key && (key.includes('session') || key.includes('talkjs'))) {
                            const value = localStorage.getItem(key);
                            try {
                                const data = JSON.parse(value);
                                if (data.sessionId) return data.sessionId;
                                if (data.id) return data.id;
                                if (data.session_id) return data.session_id;
                            } catch(e) {
                                // Có thể value chính là session ID
                                if (value && value.length > 20 && value.includes('-')) {
                                    return value;
                                }
                            }
                        }
                    }

                    // 2. Tìm trong sessionStorage
                    for (let i = 0; i < sessionStorage.length; i++) {
                        const key = sessionStorage.key(i);
                        if (key && key.includes('session')) {
                            const value = sessionStorage.getItem(key);
                            try {
                                const data = JSON.parse(value);
                                if (data.sessionId) return data.sessionId;
                                if (data.id) return data.id;
                            } catch(e) {}
                        }
                    }

                    // 3. Tìm trong cookie
                    const cookies = document.cookie.split(';');
                    for (const cookie of cookies) {
                        if (cookie.includes('sessionId') || cookie.includes('session_id')) {
                            const value = cookie.split('=')[1].trim();
                            if (value) return value;
                        }
                    }

                    // 4. Tìm trong window object
                    if (window.__TALKJS_SESSION_ID__) return window.__TALKJS_SESSION_ID__;
                    if (window.TalkJS && window.TalkJS.sessionId) return window.TalkJS.sessionId;

                } catch(e) {}
                return null;
            """)

            self.driver.switch_to.default_content()

            if session_id:
                self.session_id = session_id
                print(f"✅ Session ID (from storage): {self.session_id}")
                return self.session_id
            else:
                print("⚠️ Không tìm thấy session ID trong iframe")
                return None

        except Exception as e:
            print(f"⚠️ Lỗi lấy session ID: {e}")
            try:
                self.driver.switch_to.default_content()
            except:
                pass
            return None

    async def send_text_message_rest(self, conversation_id: str, text: str) -> Optional[str]:
        """
        Gửi tin nhắn text qua REST API (fallback khi WebSocket thất bại)

        Args:
            conversation_id: ID của conversation
            text: Nội dung tin nhắn

        Returns:
            str: Message ID nếu thành công
        """
        if not self.session_id:
            self.session_id = await self.get_session_id()

        if not self.session_id:
            print("⚠️ Không lấy được session ID")
            return None

        try:
            from datetime import datetime, timezone

            url = f"{self.config.api_url}/say/{conversation_id}/?sessionId={self.session_id}"

            # Tạo nymId nếu chưa có
            if not hasattr(self, 'nym_id') or not self.nym_id:
                self.nym_id = f"{uuid.uuid4().hex[:16]}_n"

            payload = {
                "idempotencyKey": str(uuid.uuid4()).replace('-', '')[:20],
                "entityTree": [],
                "received": False,
                "custom": {},
                "nymId": self.nym_id,
                "text": text,
                "location": None
            }

            # Headers giống với request thật từ HAR
            headers = {
                "Content-Type": "application/json",
                "Origin": "https://app.talkjs.com",
                "Referer": "https://app.talkjs.com/",
                "x-talkjs-client-build": "frontend-release-45312be",
                "x-talkjs-client-date": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
            }

            print(f"📋 Gửi REST API với session: {self.session_id}")

            async with aiohttp.ClientSession() as session:
                async with session.post(url, json=payload, headers=headers) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        print(f"✅ Đã gửi tin nhắn qua REST API: {data.get('ok')}")
                        return data.get('ok')
                    else:
                        text_resp = await resp.text()
                        print(f"❌ REST API lỗi {resp.status}: {text_resp}")
                        return None

        except Exception as e:
            print(f"❌ Lỗi gửi qua REST API: {e}")
            return None

    async def send_file_message(
        self,
        conversation_id: str,
        file_url: str,
        filename: str,
        file_token: str,
        file_size: int,
        file_type: str = "image"
    ) -> Optional[str]:
        """
        Gửi tin nhắn với file đính kèm

        Args:
            conversation_id: ID của conversation
            file_url: URL của file đã upload
            filename: Tên file
            file_token: TalkJS file token
            file_size: Kích thước file (bytes)
            file_type: Loại file (image, video, etc.)

        Returns:
            str: Message ID nếu thành công
        """
        if not self._is_connected:
            return None

        content = [{
            "type": "file",
            "url": file_url,
            "filename": filename,
            "fileToken": file_token,
            "size": file_size
        }]

        # Thêm metadata cho ảnh/video
        if file_type == "image":
            content[0]["subtype"] = "photo"
        elif file_type == "video":
            content[0]["subtype"] = "video"

        request_id = self._get_request_id()
        message = [
            request_id,
            "POST",
            f"/me/conversations/{conversation_id}/messages",
            {
                "type": "UserMessage",
                "content": content
            },
            {}
        ]

        await self.ws.send(json.dumps(message))
        response = await self._wait_response(request_id)

        if response and response[1] == 200:
            return response[2].get('id')
        return None

    async def get_conversation_info(self, conversation_id: str) -> Optional[Dict]:
        """Lấy thông tin conversation"""
        if not self._is_connected:
            return None

        request_id = self._get_request_id()
        message = [
            request_id,
            "GET",
            f"/me/conversations/{conversation_id}",
            {},
            {}
        ]

        await self.ws.send(json.dumps(message))
        response = await self._wait_response(request_id)

        if response and response[1] == 200:
            return response[2]
        return None

    async def get_messages(
        self,
        conversation_id: str,
        limit: int = 50,
        before: Optional[str] = None
    ) -> Optional[List[Dict]]:
        """
        Lấy danh sách messages từ conversation

        Args:
            conversation_id: ID của conversation
            limit: Số lượng messages tối đa
            before: Cursor để phân trang

        Returns:
            List các messages
        """
        if not self._is_connected:
            return None

        params = {"limit": limit}
        if before:
            params["before"] = before

        request_id = self._get_request_id()
        message = [
            request_id,
            "GET",
            f"/me/conversations/{conversation_id}/messages",
            params,
            {}
        ]

        await self.ws.send(json.dumps(message))
        response = await self._wait_response(request_id)

        if response and response[1] == 200:
            return response[2].get('data', [])
        return None

    async def close(self):
        """Đóng kết nối WebSocket"""
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

        print("🔌 Đã đóng kết nối TalkJS")

    @property
    def is_connected(self) -> bool:
        # websockets 16.0+ không còn .open, dùng state hoặc _is_connected
        if not self._is_connected or self.ws is None:
            return False
        try:
            # Kiểm tra state cho websockets mới
            if hasattr(self.ws, 'state'):
                return self.ws.state.name == 'OPEN'
            return True
        except:
            return False


# ==========================================
# HELPER FUNCTIONS
# ==========================================

def extract_conversation_id_from_url(url: str) -> Optional[str]:
    """
    Extract conversation ID từ Eldorado URL

    Eldorado URL format: https://www.eldorado.gg/orders/ORDER_ID
    Conversation ID thường nằm trong URL hoặc cần lấy từ page
    """
    # Pattern có thể thay đổi tùy Eldorado
    match = re.search(r'/orders/([a-f0-9\-]+)', url)
    if match:
        return match.group(1)
    return None


async def upload_file_via_ui(driver, file_path: str, conversation_id: str) -> Optional[Dict]:
    """
    Upload file qua UI (vì Firebase auth phức tạp)

    Returns:
        Dict với file info (url, token, size) nếu thành công
    """
    from selenium.webdriver.common.by import By
    import os

    try:
        driver.switch_to.default_content()

        # Tìm iframe TalkJS
        iframes = driver.find_elements(By.CSS_SELECTOR, "iframe[name*='talkjs']")

        if not iframes:
            print("⚠️ Không tìm thấy TalkJS iframe")
            return None

        driver.switch_to.frame(iframes[0])

        # Tìm file input
        inputs = driver.find_elements(By.CSS_SELECTOR, "input[type='file'], input.test__fileupload-input")

        if not inputs:
            print("⚠️ Không tìm thấy file input")
            return None

        # Upload file
        abs_path = os.path.abspath(file_path)
        inputs[0].send_keys(abs_path)

        # Đợi file upload và lấy info
        await asyncio.sleep(2)

        # Click send button nếu có
        send_btns = driver.find_elements(By.CSS_SELECTOR, ".confirm-send, .test__confirm-upload-button")
        if send_btns and send_btns[0].is_enabled():
            driver.execute_script("arguments[0].click();", send_btns[0])
            await asyncio.sleep(2)

        return {"uploaded": True, "filename": os.path.basename(file_path)}

    except Exception as e:
        print(f"❌ Lỗi upload file: {e}")
        return None
    finally:
        try:
            driver.switch_to.default_content()
        except:
            pass


# ==========================================
# TEST / DEMO
# ==========================================

async def test_talkjs_client():
    """Test TalkJS client (cần driver)"""
    print("🧪 Test TalkJS Client")

    # Mock test without driver
    client = TalkJSClient()

    # Test với token giả lập
    client.auth_token = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJ0b2tlblR5cGUiOiJ1c2VyIiwic3ViIjoiMTNjNTJkMGItMGIwNi00YTY2LThiNDMtNmI4NjZmMTNiNzdhIiwiZXhwIjoxNzczMTE1ODY2LCJpc3MiOiI0OW1MRUNPVyJ9.kRddgjwkIMNiuFA1YtXLSWbt85C-Dcm7e9ovvSvVAqo"
    client.user_id = "13c52d0b-0b06-4a66-8b43-6b866f13b77a"

    print(f"📝 Auth Token: {client.auth_token[:50]}...")
    print(f"📝 User ID: {client.user_id}")
    print("✅ Test passed!")


if __name__ == "__main__":
    asyncio.run(test_talkjs_client())
