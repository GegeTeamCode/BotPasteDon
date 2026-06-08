"""SQLite database for persistent order state across all bots."""

import json
import sqlite3
import time
import threading
from pathlib import Path
from typing import Optional, List, Dict

from shared.constants import CACHE_MAX_AGE_HOURS
from shared.logging_config import setup_logger

logger = setup_logger("db")


class Database:
    def __init__(self, db_path: str = "data/orders.db"):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._init_db()

    def _get_conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self):
        with self._lock:
            conn = self._get_conn()
            try:
                conn.executescript("""
                    CREATE TABLE IF NOT EXISTS orders (
                        order_id TEXT PRIMARY KEY,
                        platform TEXT NOT NULL,
                        status TEXT NOT NULL,
                        order_url TEXT,
                        game TEXT,
                        server TEXT,
                        item_name TEXT,
                        quantity TEXT,
                        character TEXT,
                        customer_name TEXT,
                        discord_thread_id TEXT,
                        discord_channel_id TEXT,
                        webhook_sent_at DATETIME,
                        delivery_started_at DATETIME,
                        delivery_completed_at DATETIME,
                        error_message TEXT,
                        retry_count INTEGER DEFAULT 0,
                        raw_data TEXT,
                        created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                        updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
                    );

                    CREATE INDEX IF NOT EXISTS idx_orders_status
                        ON orders(platform, status);

                    CREATE TABLE IF NOT EXISTS heartbeat (
                        service_name TEXT PRIMARY KEY,
                        last_beat DATETIME,
                        pid INTEGER
                    );
                """)
                conn.commit()

                # Migrations — add columns if missing
                try:
                    conn.execute("ALTER TABLE orders ADD COLUMN retry_data TEXT")
                    conn.commit()
                except Exception:
                    pass
            finally:
                conn.close()

    def insert_order(self, platform: str, order_id: str, order_data: dict) -> bool:
        with self._lock:
            conn = self._get_conn()
            try:
                conn.execute(
                    """INSERT OR IGNORE INTO orders
                       (order_id, platform, status, order_url, game, server,
                        item_name, quantity, character, customer_name, raw_data)
                       VALUES (?, ?, 'DETECTED', ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (order_id, platform,
                     order_data.get("url", ""),
                     order_data.get("game", ""),
                     order_data.get("server", ""),
                     order_data.get("itemName", ""),
                     order_data.get("quantity", ""),
                     order_data.get("character", ""),
                     order_data.get("customerName", ""),
                     json.dumps(order_data, ensure_ascii=False)),
                )
                conn.commit()
                return conn.total_changes > 0
            except sqlite3.IntegrityError:
                return False
            finally:
                conn.close()

    def update_order_status(self, order_id: str, status: str, **kwargs):
        with self._lock:
            conn = self._get_conn()
            try:
                sets = ["status = ?", "updated_at = CURRENT_TIMESTAMP"]
                vals = [status]
                for key, val in kwargs.items():
                    col = key
                    sets.append(f"{col} = ?")
                    vals.append(val)
                vals.append(order_id)
                conn.execute(
                    f"UPDATE orders SET {', '.join(sets)} WHERE order_id = ?",
                    vals,
                )
                conn.commit()
            finally:
                conn.close()

    def get_orders_by_status(self, platform: str, status: str) -> List[Dict]:
        with self._lock:
            conn = self._get_conn()
            try:
                rows = conn.execute(
                    "SELECT * FROM orders WHERE platform = ? AND status = ?",
                    (platform, status),
                ).fetchall()
                return [dict(r) for r in rows]
            finally:
                conn.close()

    def is_order_in_status(self, order_id: str, status: str) -> bool:
        with self._lock:
            conn = self._get_conn()
            try:
                row = conn.execute(
                    "SELECT 1 FROM orders WHERE order_id = ? AND status = ?",
                    (order_id, status),
                ).fetchone()
                return row is not None
            finally:
                conn.close()

    def is_order_processed(self, order_id: str) -> bool:
        with self._lock:
            conn = self._get_conn()
            try:
                row = conn.execute(
                    "SELECT status FROM orders WHERE order_id = ?",
                    (order_id,),
                ).fetchone()
                return row is not None
            finally:
                conn.close()

    def get_order(self, order_id: str) -> Optional[Dict]:
        with self._lock:
            conn = self._get_conn()
            try:
                row = conn.execute(
                    "SELECT * FROM orders WHERE order_id = ?",
                    (order_id,),
                ).fetchone()
                return dict(row) if row else None
            finally:
                conn.close()

    def cleanup_old_orders(self, max_age_hours: int = CACHE_MAX_AGE_HOURS):
        with self._lock:
            conn = self._get_conn()
            try:
                # Remove completed/failed orders older than max_age
                conn.execute(
                    """DELETE FROM orders
                       WHERE status IN ('COMPLETED', 'FAILED')
                       AND updated_at < datetime('now', ?)""",
                    (f"-{max_age_hours} hours",),
                )
                # Remove DETECTED orders older than 24h (no longer pending on API)
                conn.execute(
                    """DELETE FROM orders
                       WHERE status = 'DETECTED'
                       AND updated_at < datetime('now', '-24 hours')""",
                )
                conn.commit()
            finally:
                conn.close()

    def mark_erp_synced(self, order_id: str):
        with self._lock:
            conn = self._get_conn()
            try:
                conn.execute(
                    "UPDATE orders SET erp_synced = 1, updated_at = CURRENT_TIMESTAMP WHERE order_id = ?",
                    (order_id,),
                )
                conn.commit()
            finally:
                conn.close()

    def get_unsynced_orders(self, max_retries: int = 50) -> List[Dict]:
        with self._lock:
            conn = self._get_conn()
            try:
                rows = conn.execute(
                    """SELECT * FROM orders
                       WHERE erp_synced = 0
                       AND status NOT IN ("DETECTED", "FAILED")
                       AND erp_retry_count < ?
                       ORDER BY created_at ASC""",
                    (max_retries,),
                ).fetchall()
                return [dict(r) for r in rows]
            finally:
                conn.close()

    def increment_erp_retry(self, order_id: str):
        with self._lock:
            conn = self._get_conn()
            try:
                conn.execute(
                    "UPDATE orders SET erp_retry_count = erp_retry_count + 1, updated_at = CURRENT_TIMESTAMP WHERE order_id = ?",
                    (order_id,),
                )
                conn.commit()
            finally:
                conn.close()

    def update_heartbeat(self, service_name: str, pid: int):
        with self._lock:
            conn = self._get_conn()
            try:
                conn.execute(
                    """INSERT OR REPLACE INTO heartbeat (service_name, last_beat, pid)
                       VALUES (?, datetime('now'), ?)""",
                    (service_name, pid),
                )
                conn.commit()
            finally:
                conn.close()

    def get_stale_services(self, threshold_seconds: int = 90) -> List[Dict]:
        with self._lock:
            conn = self._get_conn()
            try:
                rows = conn.execute(
                    """SELECT service_name, last_beat, pid FROM heartbeat
                       WHERE last_beat < datetime('now', ? || ' seconds')""",
                    (f"-{threshold_seconds}",),
                ).fetchall()
                return [dict(r) for r in rows]
            finally:
                conn.close()
