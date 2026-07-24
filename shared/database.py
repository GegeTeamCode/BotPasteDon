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
        # 9 processes share this file; WAL allows one writer at a time. A longer
        # busy_timeout lets a writer wait out contention (SQLite-native retry)
        # instead of raising "database is locked". Writes here are tiny, so 15s
        # is ample headroom for higher order volume.
        conn.execute("PRAGMA busy_timeout=15000")
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

                    CREATE TABLE IF NOT EXISTS marketplace_status (
                        platform TEXT NOT NULL,
                        order_id TEXT NOT NULL,
                        order_item_id TEXT,
                        marketplace_state TEXT NOT NULL,
                        marketplace_state_at INTEGER,
                        last_synced_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                        last_pushed_at DATETIME,
                        push_attempts INTEGER DEFAULT 0,
                        raw_data TEXT,
                        PRIMARY KEY (platform, order_id)
                    );

                    CREATE INDEX IF NOT EXISTS idx_marketplace_status_state
                        ON marketplace_status(platform, marketplace_state);

                    CREATE TABLE IF NOT EXISTS marketplace_state_counts (
                        platform TEXT NOT NULL,
                        state TEXT NOT NULL,
                        count INTEGER NOT NULL,
                        fetched_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                        PRIMARY KEY (platform, state)
                    );

                    CREATE TABLE IF NOT EXISTS marketplace_disputes (
                        platform TEXT NOT NULL,
                        case_id TEXT NOT NULL,
                        order_id TEXT NOT NULL,
                        case_status TEXT NOT NULL,
                        report_case TEXT,
                        report_reason TEXT,
                        report_qty INTEGER,
                        first_seen_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                        last_synced_at DATETIME,
                        notified_pushed_at DATETIME,
                        raw_data TEXT,
                        PRIMARY KEY (platform, case_id)
                    );

                    CREATE TABLE IF NOT EXISTS pending_dispatches (
                        order_id TEXT PRIMARY KEY,
                        worker_url TEXT NOT NULL,
                        task_data TEXT NOT NULL,
                        attempt_count INTEGER DEFAULT 0,
                        last_attempt_at DATETIME,
                        next_retry_at DATETIME,
                        last_error TEXT,
                        created_at DATETIME DEFAULT CURRENT_TIMESTAMP
                    );
                """)
                conn.commit()

                # Migrations — add columns if missing (idempotent; on an existing
                # DB each ALTER raises "duplicate column" and is ignored). These
                # columns ran in prod but were never in CREATE TABLE nor any ALTER,
                # so a fresh DB used to crash with "no such column: erp_synced".
                for col, ddl in (
                    ("retry_data", "TEXT"),
                    ("erp_synced", "INTEGER DEFAULT 0"),
                    ("erp_retry_count", "INTEGER DEFAULT 0"),
                ):
                    try:
                        conn.execute(f"ALTER TABLE orders ADD COLUMN {col} {ddl}")
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
        # Disabled 2026-06-26 (#7): keep full order history for investigation.
        # Volume is small (~tens of orders/day) so the table grows slowly and
        # SQLite handles it for years. Previously this purged COMPLETED older than
        # max_age_hours and DETECTED older than 24h — which destroyed exactly the
        # evidence needed to debug filter/extract issues. Callers are unchanged
        # (this is now a no-op) so it can be re-enabled later if needed.
        return

    # ── pending_dispatches (coordinator → worker dispatch retry) ──────────────

    def queue_dispatch(self, order_id: str, worker_url: str, task_data_json: str):
        """Enqueue a task that failed to dispatch. INSERT OR REPLACE — a fresh
        click on the button resets the retry counter."""
        with self._lock:
            conn = self._get_conn()
            try:
                conn.execute(
                    """INSERT OR REPLACE INTO pending_dispatches
                       (order_id, worker_url, task_data, attempt_count,
                        last_attempt_at, next_retry_at, last_error, created_at)
                       VALUES (?, ?, ?, 0, NULL, datetime('now'), NULL,
                               CURRENT_TIMESTAMP)""",
                    (order_id, worker_url, task_data_json),
                )
                conn.commit()
            finally:
                conn.close()

    def get_due_dispatches(self) -> List[Dict]:
        """Pending dispatches whose next_retry_at is now-or-past (NULL = due)."""
        with self._lock:
            conn = self._get_conn()
            try:
                rows = conn.execute(
                    """SELECT * FROM pending_dispatches
                       WHERE next_retry_at IS NULL
                          OR next_retry_at <= datetime('now')
                       ORDER BY created_at ASC""",
                ).fetchall()
                return [dict(r) for r in rows]
            finally:
                conn.close()

    def mark_dispatch_attempt(self, order_id: str, error: str,
                              next_retry_at_iso: str, attempt_count: int):
        with self._lock:
            conn = self._get_conn()
            try:
                conn.execute(
                    """UPDATE pending_dispatches
                       SET attempt_count = ?,
                           last_attempt_at = CURRENT_TIMESTAMP,
                           next_retry_at = ?,
                           last_error = ?
                       WHERE order_id = ?""",
                    (attempt_count, next_retry_at_iso, error, order_id),
                )
                conn.commit()
            finally:
                conn.close()

    def remove_dispatch(self, order_id: str):
        with self._lock:
            conn = self._get_conn()
            try:
                conn.execute(
                    "DELETE FROM pending_dispatches WHERE order_id = ?",
                    (order_id,),
                )
                conn.commit()
            finally:
                conn.close()

    def mark_retry_attempt(self, order_id: str, retry_data_json: str,
                           error_message: str, retry_count: int):
        """Update order to RETRY_PENDING with new retry context.
        Keeps status as RETRY_PENDING and bumps retry_count."""
        with self._lock:
            conn = self._get_conn()
            try:
                conn.execute(
                    """UPDATE orders SET
                           status = 'RETRY_PENDING',
                           retry_data = ?,
                           error_message = ?,
                           retry_count = ?,
                           updated_at = CURRENT_TIMESTAMP
                       WHERE order_id = ?""",
                    (retry_data_json, error_message, retry_count, order_id),
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

    def get_order_game(self, order_id: str) -> str:
        """Return the recorded game for an order (''  if unknown).

        Used by status_sync to route each ERP status_update to the server that
        owns the order (currency games → .102, everything else → .100).
        """
        if not order_id:
            return ""
        with self._lock:
            conn = self._get_conn()
            try:
                row = conn.execute(
                    "SELECT game FROM orders WHERE order_id = ? LIMIT 1", (order_id,),
                ).fetchone()
                return (row["game"] if row and row["game"] else "") or ""
            except Exception:
                return ""
            finally:
                conn.close()

    def get_unsynced_orders(
        self,
        max_retries: int = 50,
        platform: Optional[str] = None,
        claim_stale_after_sec: int = 180,
    ) -> List[Dict]:
        """Orders that still need an ERP push.

        erp_synced semantics: 0 = needs push, 1 = synced, 2 = in-flight (claimed).
        Returns unsynced (0) plus any in-flight (2) whose claim has gone stale
        (process died mid-push), so a crashed claim is eventually retried.
        Pass ``platform`` so a scanner only retries its own orders — otherwise two
        scanners racing the same order create duplicate ERP Sell Orders.
        """
        with self._lock:
            conn = self._get_conn()
            try:
                sql = """SELECT * FROM orders
                         WHERE (erp_synced = 0
                                OR (erp_synced = 2
                                    AND updated_at <= datetime('now', ?)))
                         AND status NOT IN ("DETECTED", "FAILED",
                                            "EXTRACT_FAILED", "NEEDS_MANUAL")
                         AND erp_retry_count < ?"""
                params: list = [f"-{int(claim_stale_after_sec)} seconds", max_retries]
                if platform is not None:
                    sql += " AND platform = ?"
                    params.append(platform)
                sql += " ORDER BY created_at ASC"
                rows = conn.execute(sql, params).fetchall()
                return [dict(r) for r in rows]
            finally:
                conn.close()

    def claim_erp_order(self, order_id: str, claim_stale_after_sec: int = 180) -> bool:
        """Atomically mark an order in-flight (erp_synced=2) before pushing to ERP.

        Returns True only if this caller won the claim. Wins when the order is
        unsynced (0) or its previous in-flight claim is stale. The conditional
        UPDATE is the mutual-exclusion primitive: with two scanners racing, only
        one row update takes effect, so only one ERP push goes out per order.
        """
        with self._lock:
            conn = self._get_conn()
            try:
                cur = conn.execute(
                    """UPDATE orders
                       SET erp_synced = 2, updated_at = CURRENT_TIMESTAMP
                       WHERE order_id = ?
                       AND (erp_synced = 0
                            OR (erp_synced = 2
                                AND updated_at <= datetime('now', ?)))""",
                    (order_id, f"-{int(claim_stale_after_sec)} seconds"),
                )
                conn.commit()
                return cur.rowcount > 0
            finally:
                conn.close()

    def release_erp_order(self, order_id: str):
        """Release an in-flight claim back to unsynced (e.g. the push failed)."""
        with self._lock:
            conn = self._get_conn()
            try:
                conn.execute(
                    "UPDATE orders SET erp_synced = 0, updated_at = CURRENT_TIMESTAMP "
                    "WHERE order_id = ? AND erp_synced = 2",
                    (order_id,),
                )
                conn.commit()
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

    # ── marketplace_status (status_sync) ───────────────────────────────────

    def get_marketplace_status(self, platform: str, order_id: str) -> Optional[Dict]:
        with self._lock:
            conn = self._get_conn()
            try:
                row = conn.execute(
                    "SELECT * FROM marketplace_status WHERE platform=? AND order_id=?",
                    (platform, order_id),
                ).fetchone()
                return dict(row) if row else None
            finally:
                conn.close()

    def upsert_marketplace_status(self, platform: str, order_id: str,
                                  marketplace_state: str,
                                  order_item_id: Optional[str] = None,
                                  marketplace_state_at: Optional[int] = None,
                                  raw_data: Optional[str] = None,
                                  mark_pushed: bool = False) -> Optional[str]:
        """Insert or update marketplace state. Returns previous state if existing
        (so caller can decide whether to push diff), or None if inserted new."""
        with self._lock:
            conn = self._get_conn()
            try:
                row = conn.execute(
                    "SELECT marketplace_state FROM marketplace_status WHERE platform=? AND order_id=?",
                    (platform, order_id),
                ).fetchone()
                prev = row["marketplace_state"] if row else None

                if row:
                    sql = ("UPDATE marketplace_status SET marketplace_state=?, "
                           "order_item_id=COALESCE(?, order_item_id), "
                           "marketplace_state_at=COALESCE(?, marketplace_state_at), "
                           "raw_data=COALESCE(?, raw_data), "
                           "last_synced_at=CURRENT_TIMESTAMP"
                           + (", last_pushed_at=CURRENT_TIMESTAMP" if mark_pushed else "")
                           + " WHERE platform=? AND order_id=?")
                    conn.execute(sql, (marketplace_state, order_item_id,
                                       marketplace_state_at, raw_data, platform, order_id))
                else:
                    conn.execute(
                        "INSERT INTO marketplace_status "
                        "(platform, order_id, order_item_id, marketplace_state, "
                        " marketplace_state_at, raw_data, last_pushed_at) "
                        "VALUES (?, ?, ?, ?, ?, ?, " +
                        ("CURRENT_TIMESTAMP" if mark_pushed else "NULL") + ")",
                        (platform, order_id, order_item_id, marketplace_state,
                         marketplace_state_at, raw_data),
                    )
                conn.commit()
                return prev
            finally:
                conn.close()

    def mark_marketplace_pushed(self, platform: str, order_id: str, success: bool):
        with self._lock:
            conn = self._get_conn()
            try:
                if success:
                    conn.execute(
                        "UPDATE marketplace_status SET last_pushed_at=CURRENT_TIMESTAMP, "
                        "push_attempts=0 WHERE platform=? AND order_id=?",
                        (platform, order_id),
                    )
                else:
                    conn.execute(
                        "UPDATE marketplace_status SET push_attempts=push_attempts+1 "
                        "WHERE platform=? AND order_id=?",
                        (platform, order_id),
                    )
                conn.commit()
            finally:
                conn.close()

    def get_marketplace_state_counts(self, platform: str) -> Dict[str, int]:
        with self._lock:
            conn = self._get_conn()
            try:
                rows = conn.execute(
                    "SELECT state, count FROM marketplace_state_counts WHERE platform=?",
                    (platform,),
                ).fetchall()
                return {r["state"]: r["count"] for r in rows}
            finally:
                conn.close()

    def set_marketplace_state_counts(self, platform: str, counts: Dict[str, int]):
        with self._lock:
            conn = self._get_conn()
            try:
                for state, count in counts.items():
                    conn.execute(
                        "INSERT OR REPLACE INTO marketplace_state_counts "
                        "(platform, state, count, fetched_at) VALUES (?, ?, ?, CURRENT_TIMESTAMP)",
                        (platform, state, count),
                    )
                conn.commit()
            finally:
                conn.close()

    def get_unpushed_marketplace(self, platform: str, states, limit: int = 1000,
                                 created_json_path: Optional[str] = None,
                                 created_min=None):
        """Terminal-state orders recorded locally but never successfully pushed to ERP
        (last_pushed_at IS NULL). Optionally bound to orders created on/after a cutoff
        (created_min) read from raw_data via json_extract(created_json_path) — used to
        skip pre-ERP-go-live orders that ERP never had. Newest first so recent
        completions reconcile (and credit) first.
        """
        if not states:
            return []
        with self._lock:
            conn = self._get_conn()
            try:
                ph = ",".join("?" * len(states))
                sql = ("SELECT order_id, order_item_id, marketplace_state, "
                       "marketplace_state_at, raw_data FROM marketplace_status "
                       f"WHERE platform=? AND last_pushed_at IS NULL "
                       f"AND marketplace_state IN ({ph})")
                params = [platform, *states]
                if created_json_path is not None and created_min is not None:
                    sql += " AND json_extract(raw_data, ?) >= ?"
                    params += [created_json_path, created_min]
                sql += " ORDER BY last_synced_at DESC LIMIT ?"
                params.append(limit)
                rows = conn.execute(sql, params).fetchall()
                return [dict(r) for r in rows]
            finally:
                conn.close()

    # ── marketplace_disputes (G2G cases) ────────────────────────────────────

    def get_dispute(self, platform: str, case_id: str) -> Optional[Dict]:
        with self._lock:
            conn = self._get_conn()
            try:
                row = conn.execute(
                    "SELECT * FROM marketplace_disputes WHERE platform=? AND case_id=?",
                    (platform, case_id),
                ).fetchone()
                return dict(row) if row else None
            finally:
                conn.close()

    def upsert_dispute(self, platform: str, case_id: str, order_id: str,
                       case_status: str, report_case: Optional[str] = None,
                       report_reason: Optional[str] = None,
                       report_qty: Optional[int] = None,
                       raw_data: Optional[str] = None,
                       mark_notified: bool = False) -> Optional[str]:
        """Insert or update dispute. Returns previous case_status if existing."""
        with self._lock:
            conn = self._get_conn()
            try:
                row = conn.execute(
                    "SELECT case_status FROM marketplace_disputes WHERE platform=? AND case_id=?",
                    (platform, case_id),
                ).fetchone()
                prev = row["case_status"] if row else None

                if row:
                    sql = ("UPDATE marketplace_disputes SET case_status=?, "
                           "report_case=COALESCE(?, report_case), "
                           "report_reason=COALESCE(?, report_reason), "
                           "report_qty=COALESCE(?, report_qty), "
                           "raw_data=COALESCE(?, raw_data), "
                           "last_synced_at=CURRENT_TIMESTAMP"
                           + (", notified_pushed_at=CURRENT_TIMESTAMP" if mark_notified else "")
                           + " WHERE platform=? AND case_id=?")
                    conn.execute(sql, (case_status, report_case, report_reason,
                                       report_qty, raw_data, platform, case_id))
                else:
                    conn.execute(
                        "INSERT INTO marketplace_disputes "
                        "(platform, case_id, order_id, case_status, report_case, "
                        " report_reason, report_qty, raw_data, last_synced_at, notified_pushed_at) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP, " +
                        ("CURRENT_TIMESTAMP" if mark_notified else "NULL") + ")",
                        (platform, case_id, order_id, case_status, report_case,
                         report_reason, report_qty, raw_data),
                    )
                conn.commit()
                return prev
            finally:
                conn.close()

    def set_dispute_notified(self, platform: str, case_id: str, notified: bool) -> None:
        """Mark/clear that ERP currently holds a pushed alert for this case.

        notified_pushed_at doubles as the "active pushed alert" flag: set it after a
        successful alert-ON push, clear it after a successful alert-OFF (case closed)
        push. This makes ON/OFF idempotent and survives a failed push (flag stays as-is
        so the next cycle retries) without re-pushing a clear for cases ERP never saw.
        """
        val = "CURRENT_TIMESTAMP" if notified else "NULL"
        with self._lock:
            conn = self._get_conn()
            try:
                conn.execute(
                    f"UPDATE marketplace_disputes SET notified_pushed_at={val} "
                    "WHERE platform=? AND case_id=?",
                    (platform, case_id),
                )
                conn.commit()
            finally:
                conn.close()
