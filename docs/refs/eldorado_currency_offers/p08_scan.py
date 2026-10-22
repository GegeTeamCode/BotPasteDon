# p08: quet board 104 to hop Aion 2 Kinah + anh chup offer cua GegeTeam (truoc) + offer G2G GeGeTeam.
# Chi GET. Nhip 1,1s/luot (elcommon). Ket qua: combos.json, board_all.json, me_*.json, g2g_gegeteam.json
import json
import math
import sys
import time
from elcommon import get, dump, HERE

tag = sys.argv[1] if len(sys.argv) > 1 else "before"
tree = json.loads((HERE / "tree.json").read_text())


def walk(nodes, path, out):
    for n in nodes or []:
        p = path + [n]
        kids = n.get("childTradeEnvironments") or []
        if kids:
            walk(kids, p, out)
        else:
            out.append(p)


leaves = []
walk(tree["tradeEnvironments"], [], leaves)
combos = []
for p in leaves:
    combos.append({
        "region": p[0]["value"], "faction": p[1]["value"], "server": p[2]["value"],
        "env_id": p[2]["id"], "region_id": p[0]["id"], "faction_id": p[1]["id"],
        "hidden": any(n.get("isHidden") for n in p),
        "original_key": p[2].get("originalValueKey"),
    })
dump("combos.json", combos)
print("combos", len(combos), "hidden", sum(c["hidden"] for c in combos))

# 1) Offer cua minh (auth) — truoc khi ghi
st, sc = get("predefinedOffersUser/me/stateCount", auth=True)
print("stateCount", st, sc)
dump(f"me_statecount_{tag}.json", sc)
me_all = []
for state in ("Active", "Paused", "Closed", "Offline"):
    page = 1
    while True:
        st, d = get("predefinedOffers/me", {"offerState": state, "pageIndex": page, "pageSize": 50},
                    auth=True)
        rows = (d.get("results") or []) if isinstance(d, dict) else []
        for r in rows:
            r["_state_query"] = state
        me_all += rows
        print(f"/me {state} p{page}", st, "recordCount",
              d.get("recordCount") if isinstance(d, dict) else d)
        if not isinstance(d, dict) or page >= (d.get("totalPages") or 1):
            break
        page += 1
dump(f"me_all_{tag}.json", me_all)
st, d360 = get("predefinedOffers/me", {"gameId": "360", "category": "Currency", "pageIndex": 1,
                                        "pageSize": 50}, auth=True)
dump(f"me_360_{tag}.json", d360)
print("/me gameId=360", st, "recordCount", d360.get("recordCount") if isinstance(d360, dict) else d360)

if tag != "before" or "meonly" in sys.argv:
    raise SystemExit(0)

# 2) Board tung to hop
board = {}
t0 = time.time()
for i, c in enumerate(combos, 1):
    rows, page, rc = [], 1, None
    while True:
        st, d = get("predefinedOffers/augmentedGame/offers",
                    {"gameId": "360", "category": "Currency", "tradeEnvironmentId": c["env_id"],
                     "pageIndex": page, "pageSize": 100})
        if st != 200 or not isinstance(d, dict):
            print("  board loi", c["env_id"], st, str(d)[:200])
            break
        rc = d.get("recordCount")
        rows += d.get("results") or []
        if page >= math.ceil((rc or 0) / 100):
            break
        page += 1
    board[c["env_id"]] = {"recordCount": rc, "rows": rows, "read_at": time.time()}
    if i % 20 == 0:
        print(f"  {i}/{len(combos)} board, {time.time() - t0:.0f}s")
dump("board_all.json", board)
print("board xong", len(board), "to hop,", sum(len(v["rows"]) for v in board.values()), "dong")

# 3) Offer G2G cua GeGeTeam (cong khai, 1-2 luot)
g2g = []
for page in (1, 2):
    st, d = get("https://sls.g2g.com/offer/search",
                {"seo_term": "aion-2-kinah", "seller": "GeGeTeam", "page_size": 100, "page": page,
                 "include_offline": 1, "currency": "USD", "country": "VN", "v": "v2"})
    res = ((d.get("payload") or {}).get("results") or []) if isinstance(d, dict) else []
    g2g += res
    print("g2g page", page, st, len(res))
    if len(res) < 100:
        break
dump("g2g_gegeteam.json", g2g)
