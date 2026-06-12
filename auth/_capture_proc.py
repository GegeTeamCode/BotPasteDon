"""Out-of-process Eldorado Camoufox capture worker.

Lives in its own module (never the `python -m auth.main` __main__) so
multiprocessing `spawn` can re-import it cleanly in the child regardless of how
the auth service was launched. Kept import-light (only `os` at module level);
auth.main is imported lazily inside the worker so importing this module in the
parent does NOT re-import the running service.

Driven by auth.main._eldo_capture_isolated, which spawns this worker, reads the
result off a queue, and SIGKILLs the worker's process group if the capture hangs
(Playwright close()-spin — see docs/operations.md "Camoufox Playwright
TypeError (coreBundle.js)").
"""
import os


def capture_worker(profile_dir, result_q):
    """Subprocess entry: run one Camoufox capture, return the dict via queue.

    os.setsid() first so the parent can SIGKILL the whole browser tree (Playwright
    node driver + camoufox-bin) as one process group if close() hangs.
    """
    try:
        os.setsid()
    except Exception:
        pass
    data = {}
    try:
        from auth.main import EldoAuth
        data = EldoAuth._capture_single(profile_dir)
    except Exception as e:  # noqa: BLE001 — never let the worker die silently
        try:
            from auth.main import logger
            logger.error("[ELDO] capture worker crashed on %s: %s", profile_dir, e)
        except Exception:
            pass
        data = {}
    try:
        result_q.put(data)
    except Exception:
        pass
