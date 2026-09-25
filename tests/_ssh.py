"""Kết nối SSH tới máy bot cho các script test chạy tay.

Ưu tiên **key SSH** (agent hoặc ~/.ssh) — đó là cách các máy trong nhà vẫn vào
nhau, và là lý do không cần mật khẩu trong repo. Mật khẩu chỉ dùng khi đặt
``BOT_SSH_PASSWORD``, và không bao giờ được ghi vào file.

    export BOT_SSH_PASSWORD=...   # chỉ khi key không dùng được
    python tests/test_g2g_api.py
"""

import os

import paramiko

BOT_HOST = os.environ.get("BOT_SSH_HOST", "192.168.2.220")
BOT_USER = os.environ.get("BOT_SSH_USER", "root")


def connect(timeout: int = 15) -> paramiko.SSHClient:
    """Trả về SSHClient đã kết nối tới máy bot."""
    password = os.environ.get("BOT_SSH_PASSWORD") or None
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    client.connect(
        BOT_HOST,
        username=BOT_USER,
        password=password,
        # Có mật khẩu thì dùng thẳng; không thì để paramiko tự tìm key.
        allow_agent=password is None,
        look_for_keys=password is None,
        timeout=timeout,
    )
    return client
