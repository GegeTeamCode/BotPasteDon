"""Unified configuration for all bots. Loads from .env file."""

from dotenv import load_dotenv
import os
from pathlib import Path

load_dotenv()

# ── Discord Bot Tokens ──
BOT_TOKEN = os.getenv("BOT_TOKEN")  # Legacy: single bot
ELDO_WORKER_TOKEN = os.getenv("ELDO_WORKER_TOKEN") or BOT_TOKEN
G2G_WORKER_TOKEN = os.getenv("G2G_WORKER_TOKEN") or BOT_TOKEN

# ── Channel IDs ──
CHANNEL_IDS = [int(x) for x in os.getenv("CHANNEL_IDS", "").split(",") if x]
ELDO_WORKER_CHANNEL_ID = int(os.getenv("ELDO_WORKER_CHANNEL_ID") or "0") or None
G2G_WORKER_CHANNEL_ID = int(os.getenv("G2G_WORKER_CHANNEL_ID") or "0") or None

# ── Webhook URLs ──
WEBHOOK_DEFAULT = os.getenv("WEBHOOK_DEFAULT", "")
WEBHOOK_DIABLO4 = os.getenv("WEBHOOK_DIABLO4", "")
WEBHOOK_POE2 = os.getenv("WEBHOOK_POE2", "")
WEBHOOK_POE1 = os.getenv("WEBHOOK_POE1", "")
ELDO_WEBHOOK_URL = os.getenv("ELDO_WEBHOOK_URL", "")
G2G_WEBHOOK_URL = os.getenv("G2G_WEBHOOK_URL", "")

# ── ERP Webhook ──
ERP_WEBHOOK_URL = os.getenv("ERP_WEBHOOK_URL", "")
ERP_API_KEY = os.getenv("ERP_API_KEY", "")
ERP_API_KEY_ELDO = os.getenv("ERP_API_KEY_ELDO", "") or ERP_API_KEY
ERP_API_KEY_G2G = os.getenv("ERP_API_KEY_G2G", "") or ERP_API_KEY

# ── status_sync ──
# URL for marketplace state update endpoint on ERP (defaults derived from ERP_WEBHOOK_URL)
ERP_STATUS_UPDATE_URL = os.getenv("ERP_STATUS_UPDATE_URL", "")
if not ERP_STATUS_UPDATE_URL and ERP_WEBHOOK_URL:
    # ERP_WEBHOOK_URL points at .new_order — swap suffix
    ERP_STATUS_UPDATE_URL = ERP_WEBHOOK_URL.rsplit(".", 1)[0] + ".status_update"
STATUS_SYNC_INTERVAL_SEC = int(os.getenv("STATUS_SYNC_INTERVAL_SEC", "1800"))  # 30 min

# ERP go-live cutoff. status_sync only reconciles orphaned terminal orders created
# on/after this date — orders predating ERP were never in ERP and must not be pushed
# (would only produce no_so noise). ISO for Eldorado (raw_data.createdDate is ISO),
# epoch-ms for G2G (raw_data.created_at is epoch ms).
ERP_GO_LIVE_ISO = os.getenv("ERP_GO_LIVE_ISO", "2026-05-29")
ERP_GO_LIVE_MS = int(os.getenv("ERP_GO_LIVE_MS", "1779926400000"))  # 2026-05-29T00:00:00Z

# ── ERP-driven reconcile (closes the g2g list-window gap) ──
# ERP returns its non-terminal marketplace orders; the bot looks each up on the
# marketplace and pushes the real terminal state. URL derived from ERP_WEBHOOK_URL.
ERP_PENDING_ORDERS_URL = os.getenv("ERP_PENDING_ORDERS_URL", "")
if not ERP_PENDING_ORDERS_URL and ERP_WEBHOOK_URL:
    ERP_PENDING_ORDERS_URL = ERP_WEBHOOK_URL.rsplit(".", 1)[0] + ".get_pending_marketplace_orders"
# Run the ERP-driven reconcile every N status_sync cycles (heavy: per-order lookups).
ERP_RECONCILE_EVERY_N_CYCLES = int(os.getenv("ERP_RECONCILE_EVERY_N_CYCLES", "4"))  # ~2h @30m
ERP_RECONCILE_BATCH = int(os.getenv("ERP_RECONCILE_BATCH", "150"))  # max lookups per run
ERP_RECONCILE_THROTTLE_SEC = float(os.getenv("ERP_RECONCILE_THROTTLE_SEC", "0.4"))  # between lookups
ERP_RECONCILE_BACKOFF_H = int(os.getenv("ERP_RECONCILE_BACKOFF_H", "12"))  # re-check still-pending after

# ── Chrome / Selenium ──
CHROME_BINARY_PATH = os.getenv("CHROME_BINARY_PATH", "")
HEADLESS_MODE = os.getenv("HEADLESS_MODE", "false").lower() in ("true", "1", "yes")

# ── G2G API ──
G2G_USE_API = os.getenv("G2G_USE_API", "false").lower() in ("true", "1", "yes")
AUTH_SERVICE_URL = os.getenv("AUTH_SERVICE_URL", "http://localhost:8010")
SENDBIRD_SESSION_KEY = os.getenv("SENDBIRD_SESSION_KEY", "")

# ── Eldorado API ──
ELDO_USE_API = os.getenv("ELDO_USE_API", "false").lower() in ("true", "1", "yes")

# ── Database ──
# Resolve a relative DATABASE_PATH against the project root, NOT the process cwd.
# A process started from a different directory used to create an empty stray
# orders.db (e.g. /opt/BotPasteDon/scanners/orders.db) instead of the real one.
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_db_path = os.getenv("DATABASE_PATH", "data/orders.db")
DATABASE_PATH = _db_path if os.path.isabs(_db_path) else str(_PROJECT_ROOT / _db_path)

# ── G2G Auto-login ──
G2G_EMAIL = os.getenv("G2G_EMAIL", "")
G2G_PASSWORD = os.getenv("G2G_PASSWORD", "")

# ── Dashboard ──
DASHBOARD_PORT = int(os.getenv("DASHBOARD_PORT", "8766"))

# ── Scanner Configuration ──
SCANNER_CONFIG = {
    "auto_start": True,
    "whitelist": os.getenv(
        "SCANNER_WHITELIST",
        "Divine Orb, Chaos Orb, Exalted Orb, Mirror of Kalandra, Gold, Boss Materials, Runes, Currency, Gems, Flawless Horadric, Items, Husk, Lair Key, Crux, Key",
    ),
    "blacklist": os.getenv(
        "SCANNER_BLACKLIST",
        "Boosting, Leveling, Account, Custom oder",
    ),
    "G2G_TITLE_MAP": [
        {
            "title_pattern": "Any Grand Gems",
            "display_name": "Custom - Grand Gems"
        },
        {
            "title_pattern": "Flawless Horadric",
            "display_name": "Custom - Flawless Horadric Gems"
        },
        {
            "title_pattern": "18 Runes = 6x Jah Runes, 6x Que Runes, 6x Gar Runes (Heir of Perdition Pack Runes)",
            "display_name": "18 Runes = 6x Jah, 6x Que, 6x Gar"
        },
        {
            "title_pattern": "6x Jah Rune, 6x Que Rune, 6x Gar Rune (Heir of Perdition Runes Required)",
            "display_name": "6x Jah, 6x Que, 6x Gar"
        },
        {
            "title_pattern": "18 Runes = 6x Ohm Runes, 6x Wat Runes, 6x Cem Runes (Ring of Starless Skies Pack Runes)",
            "display_name": "18 Runes = 6x Ohm, 6x Wat, 6x Cem"
        },
        {
            "title_pattern": "6x Ohm Rune, 6x Wat Rune, 6x Cem Rune (Ring of Starless Skies Runes Required)",
            "display_name": "6x Ohm, 6x Wat, 6x Cem"
        },
        {
            "title_pattern": "1x Eom, 1x Lac, 1x Ceh(Harlequin Crest Runes)",
            "display_name": "1x Eom, 1x Lac, 1x Ceh"
        },
        {
            "title_pattern": "6x Tam, 6x Mot, 6x Yax",
            "display_name": "6x Tam, 6x Mot, 6x Yax"
        },
        {
            "title_pattern": "Corrupted Roots",
            "display_name": "Corrupted Roots"
        }
    ],
    "platforms": {
        "g2g": True,
        "eldorado": True,
    },
    "scan_interval_min": int(os.getenv("SCAN_INTERVAL_MIN", "15")),
    "scan_interval_max": int(os.getenv("SCAN_INTERVAL_MAX", "25")),
    "webhooks": {
        "default": WEBHOOK_DEFAULT or ELDO_WEBHOOK_URL,
        "mappings": [
            {"game": "Diablo 4", "keywords": ["diablo 4", "diablo iv", "d4"], "url": WEBHOOK_DIABLO4},
            {"game": "Path of Exile 2", "keywords": ["poe2", "path of exile 2", "poe 2", "fate of the vaal"], "url": WEBHOOK_POE2},
            {"game": "Path of Exile", "keywords": ["path of exile", "poe1", "poe 1"], "url": WEBHOOK_POE1},
        ],
    },
    "fields": {
        "showLabels": False,
        "platform": True,
        "customerName": True,
        "orderId": True,
        "game": False,
        "server": False,
        "itemName": True,
        "quantity": True,
        "character": True,
        "price": False,
        "url": True,
    },
}
