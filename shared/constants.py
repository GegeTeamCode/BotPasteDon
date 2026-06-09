"""Constants used across all bots."""

import platform
from pathlib import Path

# Base directories
BASE_DIR = Path(__file__).resolve().parent.parent
PROOF_DIR = BASE_DIR / "proofs"
CACHE_DIR = BASE_DIR / "cache"
DATA_DIR = BASE_DIR / "data"
PROFILE_DIR = BASE_DIR / "profiles"

# Platform names
PLATFORM_ELDORADO = "eldorado"
PLATFORM_G2G = "g2g"

# Order states
ORDER_DETECTED = "DETECTED"
ORDER_NOTIFIED = "NOTIFIED"
ORDER_THREAD_CREATED = "THREAD_CREATED"
ORDER_DELIVERING = "DELIVERING"
ORDER_COMPLETED = "COMPLETED"
ORDER_FAILED = "FAILED"
ORDER_RETRY_PENDING = "RETRY_PENDING"

# URLs
URL_DEFAULTS = {
    "g2g": "https://www.g2g.com/g2g-user/sale?status=preparing",
    "eldorado": "https://www.eldorado.gg/dashboard/orders/sold?orderState=PendingDelivery&displayFilter=DisplaySellingOrders",
}

# Cache settings
CACHE_MAX_AGE_HOURS = 3

# User-Agent (detect OS at runtime)
if platform.system() == "Linux":
    DEFAULT_USER_AGENT = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
else:
    DEFAULT_USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
