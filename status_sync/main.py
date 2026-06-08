"""status_sync — long-running process that syncs marketplace state to ERP.

Runs G2G + Eldorado sync cycles every STATUS_SYNC_INTERVAL_SEC (default 1800s = 30m).

Usage:
    python -m status_sync
"""

import argparse
import asyncio
import os
import signal

from shared.config import DATABASE_PATH, STATUS_SYNC_INTERVAL_SEC
from shared.database import Database
from shared.logging_config import setup_logger

from status_sync.erp_client import ERPClient
from status_sync.g2g_sync import G2GSync
from status_sync.eldo_sync import EldoSync

logger = setup_logger("status_sync")


_shutdown = asyncio.Event()


def _install_signals():
    def _handler(sig, frame):
        logger.info("Signal %s — shutdown requested", sig)
        try:
            _shutdown.set()
        except Exception:
            pass

    signal.signal(signal.SIGTERM, _handler)
    signal.signal(signal.SIGINT, _handler)


async def _heartbeat(db: Database):
    while not _shutdown.is_set():
        try:
            db.update_heartbeat("status_sync", os.getpid())
        except Exception as e:
            logger.warning("heartbeat write failed: %s", e)
        try:
            await asyncio.wait_for(_shutdown.wait(), timeout=30)
        except asyncio.TimeoutError:
            pass


async def _cycle_loop(interval: int, g2g: G2GSync, eldo: EldoSync):
    while not _shutdown.is_set():
        logger.info("=== status_sync cycle start ===")
        # Run G2G + Eldo in parallel — they don't share auth or DB writes for same rows
        results = await asyncio.gather(
            g2g.run_once(), eldo.run_once(), return_exceptions=True,
        )
        for name, r in zip(("g2g", "eldo"), results):
            if isinstance(r, Exception):
                logger.error("%s cycle failed: %s", name, r)
        logger.info("=== cycle done. sleep %ds ===", interval)
        try:
            await asyncio.wait_for(_shutdown.wait(), timeout=interval)
        except asyncio.TimeoutError:
            pass


async def _main(interval: int, once: bool):
    db = Database(DATABASE_PATH)
    erp = ERPClient()
    if not erp.url:
        logger.error("ERP_STATUS_UPDATE_URL not set in .env — aborting")
        return
    logger.info("status_sync starting | interval=%ds | erp_url=%s", interval, erp.url)

    g2g = G2GSync(db, erp)
    eldo = EldoSync(db, erp)

    if once:
        logger.info("Single cycle mode (--once)")
        await asyncio.gather(g2g.run_once(), eldo.run_once(), return_exceptions=True)
        return

    hb_task = asyncio.create_task(_heartbeat(db))
    loop_task = asyncio.create_task(_cycle_loop(interval, g2g, eldo))

    await _shutdown.wait()
    hb_task.cancel()
    loop_task.cancel()
    for t in (hb_task, loop_task):
        try:
            await t
        except (asyncio.CancelledError, Exception):
            pass
    logger.info("status_sync stopped")


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--interval", type=int, default=STATUS_SYNC_INTERVAL_SEC,
                        help="Seconds between cycles (default %(default)d)")
    parser.add_argument("--once", action="store_true",
                        help="Run one cycle then exit (for testing / cron-style use)")
    args = parser.parse_args()

    _install_signals()
    try:
        asyncio.run(_main(args.interval, args.once))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
