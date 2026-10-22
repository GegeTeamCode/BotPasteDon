# p06: doc cay tradeEnvironment day du cua Aion 2 Kinah: GET library/360/Currency.
import json
from elcommon import get, dump

data = None
for path in ("library/360/Currency", "library/Currency/360"):
    st, d = get(path, {"locale": "en-US"})
    print(path, st, type(d).__name__)
    if st == 200 and isinstance(d, dict):
        data = d
        break
if data is None:
    raise SystemExit("khong doc duoc cay env")
dump("tree.json", data)
print("keys:", sorted(data.keys()))
print({k: data[k] for k in data if k not in ("tradeEnvironments", "description", "faq",
                                              "seoContent", "attributes")})


def walk(nodes, path, out):
    for n in nodes or []:
        p = path + [n]
        kids = n.get("childTradeEnvironments") or []
        if kids:
            walk(kids, p, out)
        else:
            out.append(p)


te = data.get("tradeEnvironments") or []
print("root nodes:", len(te))
print(json.dumps({k: v for k, v in te[0].items() if k != "childTradeEnvironments"}, ensure_ascii=False))
leaves = []
walk(te, [], leaves)
print("leaf combos:", len(leaves))
from collections import Counter
print("depths:", Counter(len(p) for p in leaves))
for p in leaves[:5]:
    print(" / ".join(f"{n.get('name')}={n.get('value')}({n.get('id')})" for n in p))
