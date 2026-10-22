# p01: doc /library (cong khai truoc, token neu 401/403), tim muc Aion.
import json
from elcommon import get, dump

st, data = get("library", {"locale": "en-US"})
auth = False
if st in (401, 403):
    st, data = get("library", {"locale": "en-US"}, auth=True)
    auth = True
print("library status", st, "auth", auth, "type", type(data).__name__,
      "len", len(data) if hasattr(data, "__len__") else "-")
dump("library.json", data)
if isinstance(data, list):
    keys = set()
    for g in data:
        keys |= set(g.keys())
    print("keys:", sorted(keys))
    for g in data:
        blob = json.dumps(g, ensure_ascii=False).lower()
        if "aion" in blob or "kinah" in blob:
            print(json.dumps(g, ensure_ascii=False)[:1500])
