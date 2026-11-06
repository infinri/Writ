#!/usr/bin/env python3
"""Arm C2: add "onFailure": "block" to every PreToolUse hook in a copy of V1's hooks.json.

    make_c2.py <hooks.json path>    (edits in place; prints the count)
Only PreToolUse registrations change; the file keeps V1's key order otherwise.
"""
import json
import sys

path = sys.argv[1]
with open(path) as fh:
    cfg = json.load(fh)
count = 0
for group in cfg["hooks"]["PreToolUse"]:
    for hook in group["hooks"]:
        hook["onFailure"] = "block"
        count += 1
with open(path, "w") as fh:
    json.dump(cfg, fh, indent=2)
    fh.write("\n")
print(count)
if count != 14:
    sys.exit(f"expected 14 PreToolUse registrations, changed {count}")
