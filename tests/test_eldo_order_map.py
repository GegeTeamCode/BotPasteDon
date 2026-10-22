"""Unit test cho scanners/eldorado_scanner_api.py::_map_order_data — đơn Aion 2 Kinah.

Không mạng, không auth: đơn giả theo đúng cấu trúc đơn Eldorado thật (đơn Aion 2 đầu tiên,
23/10/2026). Đơn Aion 2 phải vào ERP cùng khuôn đơn G2G Aion 2: game "Aion 2", server =
itemName = nhãn "Server - Phe [Vùng]", số lượng theo Mil Kinah (1 = 1M), đơn giá USD/1M.
Đơn D4 Gold và PoE giữ nguyên cách cũ.
Cần venv có requirements.txt (curl_cffi, aiohttp).

Run:  python tests/test_eldo_order_map.py
"""

import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

from scanners.eldorado_scanner_api import EldoradoAPIScanner  # noqa: E402


def _env(*levels):
    return [{"name": n, "value": v, "id": str(i)} for i, (n, v) in enumerate(levels)]


def _order(game_id, category, title, env, attrs, unit, ppu, qty, total, fee):
    return {
        "id": "a11f9438-8ab7-40a9-cef8-08df302c7ece",
        "purchaseQuantity": qty,
        "totalPrice": {"amount": total, "currency": "USD"},
        "sellerPayments": {"sellerFees": {"amount": fee, "currency": "USD"}},
        "buyerInfo": {"user": {"username": "buyer"}},
        "deliveryDetails": [],
        "createdDate": "2026-10-22T17:10:45Z",
        "orderOfferDetails": {
            "gameId": game_id, "category": category, "gameCategoryTitle": title,
            "offerTitle": None, "tradeEnvironmentProperties": env,
            "offerAttributesProperties": attrs,
            "orderPricing": {"unitSystem": unit, "pricePerUnit": {"amount": ppu, "currency": "USD"}},
        },
    }


MAP = object.__new__(EldoradoAPIScanner)._map_order_data


def test_aion2_kinah_like_g2g():
    o = _order("360", "Currency", "Kinah",
               _env(("Region", "Europe"), ("Faction", "Asmodian"), ("Server", "Zikel")),
               [], "Unit1000000", 0.76699, 10, 7.67, 0.39)
    m = MAP(o, None)
    assert m["game"] == "Aion 2", m["game"]
    assert m["server"] == m["itemName"] == "Zikel - Asmodian [EU]", (m["server"], m["itemName"])
    assert m["quantity"] == "10", m["quantity"]
    assert abs(float(m["unit_price"]) - 0.76699) < 1e-12, m["unit_price"]
    assert m["total_price"] == "7.67" and m["earning"] == "7.28", (m["total_price"], m["earning"])
    print("ok aion2_kinah_like_g2g")


def test_aion2_regions():
    for region, label in (("Asia", "Asia"), ("NA East", "NA East"), ("NA West", "NA West"),
                          ("Latin America", "Latin America")):
        o = _order("360", "Currency", "Kinah",
                   _env(("Region", region), ("Faction", "Elyos"), ("Server", "Siel")),
                   [], "Unit1000000", 0.77, 20, 15.4, 0.8)
        assert MAP(o, None)["server"] == f"Siel - Elyos [{label}]"
    print("ok aion2_regions")


def test_aion2_missing_level_keeps_old_server():
    # Thiếu cấp Server (offer chỉ gắn Region) → không đoán, giữ env đầu như cũ.
    o = _order("360", "Currency", "Kinah", _env(("Region", "Asia")), [],
               "Unit1000000", 0.77, 10, 7.7, 0.4)
    m = MAP(o, None)
    assert m["server"] == "Asia" and m["itemName"] == "Kinah" and m["quantity"] == "10", m
    print("ok aion2_missing_level_keeps_old_server")


def test_d4_gold_unchanged():
    o = _order("132", "Currency", "Gold", _env(("Server", "Seasonal Realm - Softcore")), [],
               "Unit1000000000", 0.054, 60, 3.24, 0.2)
    m = MAP(o, None)
    assert m["server"] == "Seasonal Realm - Softcore" and m["quantity"] == "60000", m
    assert abs(float(m["unit_price"]) - 5.4e-05) < 1e-15, m["unit_price"]
    print("ok d4_gold_unchanged")


def test_poe_orb_unchanged():
    o = _order("2", "Currency", "Currency",
               _env(("Platform", "PC"), ("League", "Curse of The Allflames SC")),
               [{"attributeId": "path-of-exile-orbs", "value": "Divine Orb"}],
               "Unit1", 0.022, 250, 5.5, 0.3)
    m = MAP(o, None)
    assert (m["game"], m["server"], m["itemName"], m["quantity"]) == \
        ("Path of Exile", "PC", "Divine Orb", "250"), m
    print("ok poe_orb_unchanged")


if __name__ == "__main__":
    test_aion2_kinah_like_g2g()
    test_aion2_regions()
    test_aion2_missing_level_keeps_old_server()
    test_d4_gold_unchanged()
    test_poe_orb_unchanged()
    print("\nTẤT CẢ ĐỀU XANH")
