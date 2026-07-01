import unittest

from scanners.g2g_scanner_api import G2GAPIScanner


class G2GManualTitleMappingTest(unittest.TestCase):
    def setUp(self):
        self.scanner = G2GAPIScanner.__new__(G2GAPIScanner)
        self.scanner.config = {"G2G_TITLE_MAP": []}
        self.raw = {
            "order_item_id": "1782864177922GX19-1",
            "brand_keyword": {"en": "Diablo 4"},
            "offer_title": "SS14 Any Items - Aspects 1-4GA Max roll Amulet",
            "offer_attributes": [
                {"label": {"en": "Server"}, "value": "Season 14 - Softcore"},
                {"label": {"en": "Item Type"}, "value": "Gear"},
                {"label": {"en": "Gears"}, "value": "Amulet"},
            ],
            "purchased_qty": 22,
            "earning": "20.69",
            "commission_fee_amount": "1.10",
        }

    def test_auto_keeps_attribute_mapping(self):
        mapped = self.scanner._map_order_data(self.raw)
        self.assertEqual(mapped["itemName"], "Gear - Amulet")

    def test_manual_prefers_offer_title(self):
        mapped = self.scanner._map_order_data(self.raw, prefer_offer_title=True)
        self.assertEqual(mapped["itemName"], self.raw["offer_title"])
        self.assertEqual(mapped["server"], "Season 14 - Softcore")

    def test_manual_falls_back_for_blank_title(self):
        self.raw["offer_title"] = "   "
        mapped = self.scanner._map_order_data(self.raw, prefer_offer_title=True)
        self.assertEqual(mapped["itemName"], "Gear - Amulet")


if __name__ == "__main__":
    unittest.main()
