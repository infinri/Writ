import json
import sys

m = json.load(sys.stdin)
for p in sorted(m["packages"], key=lambda p: p["name"]):
    if p.get("links"):
        print(p["name"], p["version"], "links=" + p["links"])
