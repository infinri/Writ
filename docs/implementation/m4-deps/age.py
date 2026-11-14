#!/usr/bin/env python3
"""WV-1 release-age helper: new packages younger than 14 days at 2026-11-14 and, for each, the
newest semver-compatible, non-yanked, non-prerelease version at least 14 days old (pin candidate).

  python3 -I docs/implementation/m4-deps/age.py PACKAGES_CSV > young.tsv
"""
import csv
import json
import sys
import time
import urllib.request
from datetime import datetime, timedelta, timezone

CUTOFF = datetime(2026, 11, 14, tzinfo=timezone.utc)
LIMIT = CUTOFF - timedelta(days=14)


def compat(a, b):
    pa, pb = a.split("."), b.split(".")
    return pa[0] == pb[0] and (pa[0] != "0" or pa[1] == pb[1])


def main():
    rows = [r for r in csv.DictReader(open(sys.argv[1])) if r["new"] == "True" and float(r["age_days_at_2026-11-14"]) < 14]
    print("name\tversion\tpublished\tage_days\tpin_candidate\tcandidate_published")
    for r in rows:
        req = urllib.request.Request(f"https://crates.io/api/v1/crates/{r['name']}", headers={"User-Agent": "writ-dependency-review"})
        with urllib.request.urlopen(req, timeout=60) as f:
            versions = json.load(f)["versions"]
        best = None
        for v in versions:
            created = datetime.fromisoformat(v["created_at"].replace("Z", "+00:00"))
            if v["yanked"] or "-" in v["num"] or created > LIMIT or not compat(v["num"], r["version"]):
                continue
            best = v
            break
        print("\t".join([r["name"], r["version"], r["published"], r["age_days_at_2026-11-14"],
                         best["num"] if best else "none", best["created_at"] if best else ""]))
        time.sleep(1.0)


if __name__ == "__main__":
    main()
