"""Unit tests for dual-server ERP routing (currency games → .102).

No network / no server. Env is set BEFORE importing shared.config so the
ERP_TARGETS registry is built with both a main and a currency target.

Run:  python tests/test_erp_routing.py
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Configure both ERP targets before importing config (module builds ERP_TARGETS
# at import time from these env vars).
os.environ["ERP_WEBHOOK_URL"] = "http://100/api/method/gege.new_order"
os.environ["ERP_API_KEY_ELDO"] = "ke_main"
os.environ["ERP_API_KEY_G2G"] = "kg_main"
os.environ["ERP_WEBHOOK_URL_CURRENCY"] = "http://102/api/method/gege.new_order"
# currency keys default to the main keys (same secrets) — leave unset on purpose.

import shared.config as cfg  # noqa: E402
from status_sync.erp_client import ERPClient  # noqa: E402


def test_target_id_for_game():
    assert cfg.erp_target_id_for_game("Path of Exile") == "currency"
    assert cfg.erp_target_id_for_game("path of exile 2") == "currency"      # case-insensitive
    assert cfg.erp_target_id_for_game("Torchlight: Infinite") == "currency"
    assert cfg.erp_target_id_for_game("Diablo 4") == "main"
    assert cfg.erp_target_id_for_game("") == "main"
    assert cfg.erp_target_id_for_game("Some New Game") == "main"            # unknown → main


def test_config_target_and_keys():
    t = cfg.erp_target_for_game("Path of Exile")
    assert t["id"] == "currency"
    assert t["webhook_url"] == "http://102/api/method/gege.new_order"
    # derived sync urls
    assert t["status_update_url"].endswith(".status_update")
    assert t["pending_orders_url"].endswith(".get_pending_marketplace_orders")
    # currency reuses the main keys (unset → fallback)
    assert cfg.erp_key_for_target(t, "eldorado") == "ke_main"
    assert cfg.erp_key_for_target(t, "g2g") == "kg_main"

    m = cfg.erp_target_for_game("Diablo 4")
    assert m["id"] == "main"
    assert m["webhook_url"] == "http://100/api/method/gege.new_order"


def test_fallback_to_main_when_currency_unset(monkeypatch=None):
    # Simulate currency webhook not configured → currency games fall back to main.
    saved = cfg.ERP_WEBHOOK_URL_CURRENCY
    try:
        cfg.ERP_WEBHOOK_URL_CURRENCY = ""
        assert cfg.erp_target_id_for_game("Path of Exile") == "main"
    finally:
        cfg.ERP_WEBHOOK_URL_CURRENCY = saved


def test_client_pick_target():
    c = ERPClient(game_resolver=lambda oid: {"O1": "Path of Exile"}.get(oid, ""))
    # 1) explicit tag wins
    assert c._pick_target({"_erp_target": "currency"})["id"] == "currency"
    assert c._pick_target({"_erp_target": "main"})["id"] == "main"
    # 2) by game in payload
    assert c._pick_target({"game": "Path of Exile"})["id"] == "currency"
    assert c._pick_target({"game": "Diablo 4"})["id"] == "main"
    # 3) by resolver when no game/tag
    assert c._pick_target({"external_order_id": "O1"})["id"] == "currency"
    assert c._pick_target({"external_order_id": "O2"})["id"] == "main"
    # 4) empty payload → main
    assert c._pick_target({})["id"] == "main"


def test_client_key_selection():
    c = ERPClient()
    main = c._target_by_id("main")
    cur = c._target_by_id("currency")
    assert c._key(main, "g2g") == "kg_main"
    assert c._key(main, "eldorado") == "ke_main"
    # currency reuses main keys
    assert c._key(cur, "g2g") == "kg_main"
    assert c._key(cur, "eldorado") == "ke_main"


def _run():
    failures = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"PASS {name}")
            except AssertionError as e:
                failures += 1
                print(f"FAIL {name}: {e!r}")
            except Exception as e:
                failures += 1
                print(f"ERROR {name}: {type(e).__name__}: {e}")
    if failures:
        print(f"\n{failures} test(s) failed")
        sys.exit(1)
    print("\nAll routing tests passed")


if __name__ == "__main__":
    _run()
