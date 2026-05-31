import sys
sys.path.insert(0, "/opt/BotPasteDon")
from shared.config import SCANNER_CONFIG
from scanners.base_scanner import check_keywords

config = SCANNER_CONFIG
text = "Items"
result = check_keywords(text, config)
print(f"check_keywords({text!r}, config) = {result}")

wl = config.get("whitelist", "")
bl = config.get("blacklist", "")
wl_list = [k.strip().lower() for k in wl.split(",") if k.strip()]
bl_list = [k.strip().lower() for k in bl.split(",") if k.strip()]
print(f"whitelist list: {wl_list}")
print(f"blacklist list: {bl_list}")

lower_text = text.lower()
print(f"lower_text: {lower_text!r}")

for k in bl_list:
    if k in lower_text:
        print(f"  BLACKLIST match: {k!r}")

for k in wl_list:
    match = k in lower_text
    print(f"  whitelist {k!r} in {lower_text!r}: {match}")
