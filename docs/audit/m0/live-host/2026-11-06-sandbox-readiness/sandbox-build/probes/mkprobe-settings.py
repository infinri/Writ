#!/usr/bin/env python3
"""Write a settings.json containing exactly one SR-06 probe hook (arms B and C1).

    mkprobe-settings.py <out.json> <experiment> <event> <matcher> <behavior> [--arg X]
                        [--timeout N] [--on-failure block] [--allow TOOL ...]

Arm B: no --on-failure. Arm C1: --on-failure block. Nothing else differs.
"""
import argparse
import json

p = argparse.ArgumentParser()
p.add_argument("out")
p.add_argument("experiment")
p.add_argument("event")
p.add_argument("matcher")
p.add_argument("behavior")
p.add_argument("--arg", default="")
p.add_argument("--timeout", type=int)
p.add_argument("--on-failure", dest="on_failure")
p.add_argument("--allow", nargs="*", default=[])
a = p.parse_args()

hook = {"type": "command", "command": f"/opt/probes/probe.sh {a.experiment} {a.behavior} {a.arg}".strip()}
if a.timeout is not None:
    hook["timeout"] = a.timeout
if a.on_failure:
    hook["onFailure"] = a.on_failure
settings = {
    "permissions": {"allow": a.allow, "deny": [], "ask": []},
    "hooks": {a.event: [{"matcher": a.matcher, "hooks": [hook]}]},
}
with open(a.out, "w") as fh:
    json.dump(settings, fh, indent=2)
    fh.write("\n")
