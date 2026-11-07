#!/usr/bin/env python3
"""Deliberately wrong prototype wrapper for the M1-05 mutation smoke test.

  mutant.py --real <proto binary> --mutation <name> <command>

Proxies the JSON Lines protocol to a real prototype and corrupts one behavior:
  dup-accept     canonical: a DUPLICATE_KEY rejection becomes a last-wins canonical form
  ignore-artifact check-acceptance: ACCEPTANCE_ARTIFACT_MISMATCH becomes {"ok": true}
  double-grant   broker-scenario: every used grant reports one extra use
  redispatch     broker-scenario: RECONCILIATION_REQUIRED becomes a fresh dispatch
  leak-codes     external-refusal: every internal code passes through
  scope-allow    decide: RESOURCE_SCOPE_MISMATCH becomes allow
  forbid-ignored decide: a deny from a forbid statement becomes allow
  replay-commit  transition-scenario: ACCEPTANCE_REPLAYED becomes a commit
Never used as a reference; only to show that the oracles catch faults.
"""
import argparse
import hashlib
import json
import subprocess
import sys


def canonical(v):
    return json.dumps(v, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def mutate(m, command, inp, out):
    if m == "dup-accept" and command == "canonical" and out.get("error") == "DUPLICATE_KEY":
        try:
            v = json.loads(inp["input_text"])  # Python is last-wins: exactly the forbidden behavior
            t = canonical(v)
            return {"canonical": t, "digest": "sha256:" + hashlib.sha256(t.encode()).hexdigest()}
        except (ValueError, KeyError):
            return out
    if m == "ignore-artifact" and command == "check-acceptance" and out.get("refusal") == "ACCEPTANCE_ARTIFACT_MISMATCH":
        return {"ok": True}
    if m == "leak-codes" and command == "external-refusal":
        code = (inp.get("input") or {}).get("internal_code") if isinstance(inp.get("input"), dict) else None
        if isinstance(code, str):
            return {"external": code}
    if m == "scope-allow" and command == "decide" and out.get("reason_code") == "RESOURCE_SCOPE_MISMATCH":
        return dict(out, status="allow")
    if m == "forbid-ignored" and command == "decide" and out.get("status") == "deny" and out.get("matched_rule_ids"):
        b = (inp.get("input") or {}).get("bundle") or {}
        forbids = {s["statement_id"] for s in b.get("statements", []) if s.get("effect") == "forbid"}
        if forbids & set(out["matched_rule_ids"]):
            return dict(out, status="allow")
    if command == "broker-scenario" and isinstance(out.get("final"), dict):
        fin = out["final"]
        if m == "double-grant":
            fin["grant_uses"] = {g: (n + 1 if n else n) for g, n in fin.get("grant_uses", {}).items()}
        if m == "redispatch":
            for i, r in enumerate(out.get("steps", [])):
                if r == {"code": "RECONCILIATION_REQUIRED", "result": "refused"}:
                    out["steps"][i] = {"result": "effect_completed"}
                    fin["effects_applied"] += 1
                    fin["consumed_permits"] += 1
                    fin["refusals"] -= 1
    if m == "replay-commit" and command == "transition-scenario" and isinstance(out.get("final"), dict):
        for i, r in enumerate(out.get("steps", [])):
            if r == {"code": "ACCEPTANCE_REPLAYED", "result": "refused"}:
                out["final"]["revision"] += 1
                out["final"]["refusals"] -= 1
                out["final"]["receipts"] += 1
                out["steps"][i] = {"result": "committed", "revision": out["final"]["revision"]}
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--real", required=True)
    ap.add_argument("--mutation", required=True)
    ap.add_argument("command")
    a = ap.parse_args()
    data = sys.stdin.buffer.read()
    inputs = {}
    for line in data.decode("utf-8").split("\n"):
        if line.strip():
            try:
                msg = json.loads(line)
                inputs[msg.get("id")] = msg
            except ValueError:
                pass
    proc = subprocess.run([a.real, a.command], input=data, capture_output=True)
    out_lines = []
    for line in proc.stdout.decode("utf-8").split("\n"):
        if not line.strip():
            continue
        msg = json.loads(line)
        if isinstance(msg.get("output"), dict) and msg.get("id") in inputs:
            msg["output"] = mutate(a.mutation, a.command, inputs[msg["id"]], msg["output"])
        out_lines.append(json.dumps(msg, ensure_ascii=False, separators=(",", ":")))
    sys.stdout.write("\n".join(out_lines) + ("\n" if out_lines else ""))
    sys.stderr.buffer.write(proc.stderr)
    return proc.returncode


if __name__ == "__main__":
    sys.exit(main())
