#!/usr/bin/env python3
"""Test stub for the parity driver. Answers from an answers file, with injected faults.

Env: STUB_ANSWERS (JSON file id -> output), STUB_MODE in
  echo | wrong-field | missing | not-json | crash | hang | duplicate | nondeterministic
"""
import json
import os
import random
import sys
import time

answers = json.load(open(os.environ["STUB_ANSWERS"], encoding="utf-8"))
mode = os.environ.get("STUB_MODE", "echo")
lines = [json.loads(l) for l in sys.stdin if l.strip()]
if mode == "crash":
    sys.exit(3)
if mode == "hang":
    time.sleep(30)
out = []
for i, msg in enumerate(lines):
    output = json.loads(json.dumps(answers[msg["id"]]))
    if isinstance(output, dict) and list(output) == ["one_of"]:
        output = output["one_of"][0]
    if mode == "wrong-field" and i == 0:
        key = sorted(output)[0]
        output[key] = "TAMPERED"
    if mode == "missing" and i == 0:
        continue
    if mode == "nondeterministic" and i == 0:
        output["noise"] = random.random()
    out.append(json.dumps({"id": msg["id"], "output": output}))
    if mode == "duplicate" and i == 0:
        out.append(out[-1])
if mode == "not-json":
    out.insert(0, "this is not json")
sys.stdout.write("\n".join(out) + "\n")
