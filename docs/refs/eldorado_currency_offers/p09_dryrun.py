# p09: dung bang dry-run gia hang 8 cho 104 to hop Aion 2 Kinah tren Eldorado.
# Chi doc file da quet (board_all.json, combos.json, g2g_gegeteam.json, me_360_*). Khong goi mang.
# Luat (nguoi dung chot 22/10/2026):
#   - xep board theo gia tang dan, bo dong GegeTeam;
#   - >= 8 dong: gia = dong 8 - 1 buoc; neu <= dong 7 thi = dong 8, ghi "hoa gia";
#   - 1-7 dong: bo dong > 2x trung vi board (treo gia), gia = dong dat nhat con lai + 1 buoc;
#   - 0 dong: bo qua.
import csv
import json
import re
import statistics
import sys
from collections import Counter
from decimal import Decimal
from pathlib import Path

D = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).resolve().parent
OUT = Path(sys.argv[2]) if len(sys.argv) > 2 else D
STEP = Decimal("0.00001")
OUTLIER_X = Decimal(2)
G2G_PARKED = 29          # G2G >= $29 = offer dang treo gia (tam ngung)
DEFAULT_STOCK = 20       # trung vi ton 33 offer G2G gia that cua GeGeTeam (22/10)
STOCK_CAP = 100
REGION_G2G = {"Europe": "EU", "Asia": "Asia", "NA East": "NA East", "NA West": "NA West"}
ME = "gegeteam"

combos = json.loads((D / "combos.json").read_text())
board = json.loads((D / "board_all.json").read_text())
g2g = json.loads((D / "g2g_gegeteam.json").read_text())
g2g_by = {}
for o in g2g:
    m = re.match(r"^(.*?) - (Elyos|Asmodian) \[(.*)\]$", o["title"])
    if m:
        g2g_by[(m.group(3), m.group(2), m.group(1).strip())] = o


def dec(x) -> Decimal:
    return Decimal(repr(x)).quantize(Decimal("0.00001"))


rows_out = []
for c in combos:
    b = board[c["env_id"]]
    rows = [r for r in b["rows"] if (r["user"]["username"] or "").lower() != ME]
    own = len(b["rows"]) - len(rows)
    rows.sort(key=lambda r: r["offer"]["pricePerUnit"]["amount"])
    prices = [dec(r["offer"]["pricePerUnit"]["amount"]) for r in rows]
    n = len(prices)
    rec = {
        "region": c["region"], "server": c["server"], "faction": c["faction"],
        "env_id": c["env_id"], "attr": "", "board_rows": n,
        "p1": str(prices[0]) if n else "", "median": "", "p7": "", "p8_or_last": "",
        "proposed": "", "rank": "", "case": "", "stock": "", "min": "",
        "g2g_price": "", "g2g_stock": "", "g2g_state": "", "note": [],
    }
    if own:
        rec["note"].append(f"board co {own} dong GegeTeam (da bo)")
    if n:
        med = statistics.median(prices)
        rec["median"] = str(med.quantize(STEP))
    if n == 0:
        rec["case"] = "trong"
        rec["note"].append("bang trong - bo qua")
    elif n >= 8:
        rec["case"] = ">=8"
        p7, p8 = prices[6], prices[7]
        rec["p7"], rec["p8_or_last"] = str(p7), str(p8)
        p = p8 - STEP
        if p <= p7:
            p = p8
            rec["note"].append(f"hoa gia voi dong 8 ({p8}); dong 7 = {p7}")
        if p8 > OUTLIER_X * med:
            rec["note"].append(f"dong 8 > 2x trung vi ({rec['median']})")
    else:
        rec["case"] = "<8"
        keep = [x for x in prices if x <= OUTLIER_X * med]
        drop = [(rows[i]["user"]["username"], str(x)) for i, x in enumerate(prices)
                if x > OUTLIER_X * med]
        rec["p8_or_last"] = str(max(keep))
        p = max(keep) + STEP
        rec["note"].append(f"duoi 8 dong; nguong treo gia = 2x trung vi = {(OUTLIER_X * med).quantize(STEP)}")
        if drop:
            rec["note"].append("da bo: " + ", ".join(f"{u} {x}" for u, x in drop))
    if n:
        lo = 1 + sum(1 for x in prices if x < p)
        hi = 1 + sum(1 for x in prices if x <= p)
        rec["proposed"] = str(p)
        rec["rank"] = str(lo) if lo == hi else f"{lo}-{hi}"
        # Ton / min
        go = g2g_by.get((REGION_G2G.get(c["region"], "?"), c["faction"], c["server"]))
        if go:
            rec["g2g_price"] = go["unit_price_in_usd"]
            rec["g2g_stock"] = go["available_qty"]
        if go and go["unit_price_in_usd"] < G2G_PARKED:
            rec["g2g_state"] = "ban that"
            stock = min(int(go["available_qty"]), STOCK_CAP)
            if go["available_qty"] > STOCK_CAP:
                rec["note"].append(f"ton G2G {go['available_qty']} > {STOCK_CAP}: ha xuong {STOCK_CAP}")
        else:
            rec["g2g_state"] = "treo $30" if go else "khong co tren G2G"
            stock = DEFAULT_STOCK
            rec["note"].append("can chu xac nhan co hang (G2G " + rec["g2g_state"] + ")")
        mins = Counter(r["offer"]["minQuantity"] for r in rows[:10])
        mn = sorted(mins.items(), key=lambda kv: (-kv[1], kv[0]))[0][0]
        rec["stock"], rec["min"] = stock, min(mn, stock)
    rec["note"] = "; ".join(rec["note"])
    rows_out.append(rec)

cols = ["region", "server", "faction", "env_id", "attr", "board_rows", "p1", "median", "p7",
        "p8_or_last", "proposed", "rank", "case", "stock", "min", "g2g_price", "g2g_stock",
        "g2g_state", "note"]
with open(OUT / "dryrun_aion2_el.csv", "w", newline="") as fh:
    w = csv.DictWriter(fh, fieldnames=cols)
    w.writeheader()
    w.writerows(rows_out)

hdr = ["Region", "Server", "Faction", "env", "dòng", "giá #1", "dòng 7", "dòng 8 / cuối",
       "đề xuất", "hạng", "tồn", "min", "G2G mình", "ghi chú"]
lines = ["| " + " | ".join(hdr) + " |", "|" + "---|" * len(hdr)]
for r in rows_out:
    g = f"{r['g2g_price']} ({r['g2g_state']}, tồn {r['g2g_stock']})" if r["g2g_price"] != "" else r["g2g_state"]
    lines.append("| " + " | ".join(str(x) for x in [
        r["region"], r["server"], r["faction"], r["env_id"], r["board_rows"], r["p1"], r["p7"],
        r["p8_or_last"], r["proposed"], r["rank"], r["stock"], r["min"], g, r["note"]]) + " |")
(OUT / "dryrun_aion2_el.md").write_text("\n".join(lines) + "\n")

cases = Counter(r["case"] for r in rows_out)
print("to hop", len(rows_out), dict(cases))
print("hoa gia", sum("hoa gia" in r["note"] for r in rows_out))
print("g2g state", Counter(r["g2g_state"] for r in rows_out))
print("min", Counter(r["min"] for r in rows_out), "stock", Counter(r["stock"] for r in rows_out).most_common(8))
for r in rows_out:
    if r["case"] != ">=8" or "hoa" in r["note"] or "2x" in r["note"]:
        print(r["region"], r["faction"], r["server"], r["board_rows"], r["p7"], r["p8_or_last"],
              r["proposed"], r["rank"], "|", r["note"])
