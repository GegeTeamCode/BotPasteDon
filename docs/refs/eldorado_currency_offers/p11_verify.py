# p11: do lai offer thu + hang tren board + so /me truoc/sau. Chi GET.
#   p11_verify.py <offer_id> <env_id>
import json
import sys
import time
from decimal import Decimal
from elcommon import get, dump, HERE

oid, env_id = sys.argv[1], sys.argv[2]
st, d = get(f"predefinedOffers/{oid}")
o = (d.get("offer") if isinstance(d, dict) else None) or d
dump(f"offer_{oid}_public.json", d)
print("GET predefinedOffers/{id}", st)
if isinstance(o, dict):
    print({k: o.get(k) for k in ("offerState", "expireDate", "quantity", "minQuantity", "unitSystem",
                                 "deliveryMethod", "guaranteedDeliveryTime", "offerVersion")},
          "price", (o.get("pricePerUnit") or {}).get("amount"),
          "env", [t.get("id") for t in o.get("tradeEnvironmentValues") or []])
st, dp = get(f"predefinedOffers/{oid}/private", auth=True)
dump(f"offer_{oid}_private.json", dp)
print("GET predefinedOffers/{id}/private", st, str(dp)[:300])

st, b = get("predefinedOffers/augmentedGame/offers",
            {"gameId": "360", "category": "Currency", "tradeEnvironmentId": env_id,
             "pageIndex": 1, "pageSize": 100})
dump(f"board_{env_id}_post.json", b)
rows = b["results"]
rows.sort(key=lambda r: r["offer"]["pricePerUnit"]["amount"])
me_idx = [i for i, r in enumerate(rows) if r["offer"]["id"] == oid]
print("board", env_id, "recordCount", b.get("recordCount"), "dong cua minh (theo gia tang):",
      [i + 1 for i in me_idx], "| vi tri trong thu tu API:",
      [i + 1 for i, r in enumerate(b["results"]) if r["offer"]["id"] == oid])
for i, r in enumerate(rows[:11], 1):
    print(f"  {i:2} {r['user']['username']:16} {Decimal(repr(r['offer']['pricePerUnit']['amount'])):.5f}")
if me_idx:
    p = rows[me_idx[0]]["offer"]["pricePerUnit"]["amount"]
    lo = 1 + sum(1 for r in rows if r["offer"]["pricePerUnit"]["amount"] < p and r["offer"]["id"] != oid)
    print("hang theo gia (bo tie):", lo)
