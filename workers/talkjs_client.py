"""TalkJS WebSocket Client for Eldorado chat messaging."""

import asyncio
import json
import re
from typing import Optional, Dict
from dataclasses import dataclass

import aiohttp
import websockets

from shared.constants import DEFAULT_USER_AGENT
from shared.logging_config import setup_logger

logger = setup_logger("talkjs")


@dataclass
class TalkJSConfig:
    app_id: str = "49mLECOW"
    ws_url: str = "wss://realtime.talkjs.com/v1/{app_id}/realtime/{user_id}"


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

    # File attachment via REST API removed — TalkJS REST /say needs browser session.
    # Proof URLs are sent as text links instead (see eldorado_worker.py).

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

    # ── File Upload ──

    async def upload_file(self, file_path: str, conversation_id: str) -> Optional[dict]:
        """Upload file to Firebase Storage via resumable upload, return file info."""
        import mimetypes
        from pathlib import Path
        from urllib.parse import quote

        file = Path(file_path)
        if not file.exists():
            logger.error(f"File not found: {file_path}")
            return None

        filename = file.name
        content_type = mimetypes.guess_type(filename)[0] or "application/octet-stream"
        file_size = file.stat().st_size
        firebase_base = "https://firebasestorage.googleapis.com/v0/b/klets-3642/o"
        # Add timestamp to avoid collision with existing files on Firebase
        ts = int(asyncio.get_event_loop().time() * 1000)
        unique_name = f"{file.stem}_{ts}{file.suffix}"
        storage_path = f"user_files/{self.config.app_id}/{conversation_id}/{unique_name}"
        encoded_path = quote(storage_path, safe="")

        try:
            async with aiohttp.ClientSession() as session:
                # Step 1: Start resumable upload
                start_headers = {
                    "x-goog-upload-protocol": "resumable",
                    "x-goog-upload-command": "start",
                    "x-firebase-storage-version": "webjs/9.23.0",
                    "Content-Type": "application/json",
                }
                start_body = json.dumps({
                    "name": storage_path,
                    "cacheControl": "private, max-age=86400",
                    "contentType": content_type,
                    "metadata": {"draft": "true"},
                })
                async with session.post(
                    f"{firebase_base}?name={encoded_path}",
                    headers=start_headers, data=start_body,
                ) as resp:
                    upload_url = resp.headers.get("x-goog-upload-url", "")
                    if not upload_url:
                        logger.error(f"Firebase upload start failed: {resp.status}")
                        return None

                # Step 2: Upload file data + finalize
                upload_headers = {
                    "x-goog-upload-command": "upload, finalize",
                    "x-goog-upload-offset": "0",
                }
                with open(file_path, "rb") as f:
                    file_data = f.read()
                async with session.post(
                    upload_url, headers=upload_headers, data=file_data,
                ) as resp:
                    if resp.status != 200:
                        logger.error(f"Firebase upload finalize failed: {resp.status}")
                        return None
                    result = await resp.json()
                    download_tokens = result.get("downloadTokens", "")
                    if not download_tokens:
                        logger.error("No downloadTokens in Firebase response")
                        return None

                # Step 3: Remove draft metadata
                file_url = (
                    f"{firebase_base}/{encoded_path}"
                    f"?alt=media&token={download_tokens}"
                )
                try:
                    patch_url = (
                        f"{firebase_base}/{encoded_path}"
                        f"?alt=media&token={download_tokens}"
                    )
                    async with session.patch(
                        patch_url,
                        headers={"Content-Type": "application/json"},
                        data=json.dumps({"metadata": {"draft": None}}),
                    ) as patch_resp:
                        if patch_resp.status not in (200, 204):
                            logger.warning(f"Draft removal: {patch_resp.status}")
                except Exception as e:
                    logger.warning(f"Draft removal failed (non-fatal): {e}")

                logger.info(f"File uploaded: {filename} ({file_size} bytes)")
                return {
                    "url": file_url,
                    "filename": filename,
                    "size": file_size,
                    "contentType": content_type,
                }
        except Exception as e:
            logger.error(f"File upload error: {e}")
            return None

    # _build_attachment removed — proof URLs sent as text, no file attachments.
