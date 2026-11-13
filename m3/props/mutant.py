#!/usr/bin/env python3
"""Self-test proxy: runs the real adapter and injects one wrong answer into its output
(some modes also rewrite the input first, to reproduce a wrong adapter behavior).

  python3 m3/props/mutant.py --real target/release/examples/m3_line_adapter --mode MODE broker-scenario

Modes:
  m1_b36            the frozen M1 b-36 expectation instead of the approved difference: after a
                    reconcile that resolved receipts, an identical retry is refused
                    RECONCILIATION_REQUIRED (and the refusal is counted) instead of replaying
  conc_extra        one IDEMPOTENCY_CONFLICT of a concurrent group is reported as effect_completed
  refusals          the final refusal count is one higher
  c4_ledger_first   wrong precedence: a C4 step answered with a decision-stage refusal is
                    answered LEDGER_UNAVAILABLE instead (the M1 ledger-first order)
  c4_decision_first wrong precedence: a C4 step due LEDGER_UNAVAILABLE is answered APPROVAL_REQUIRED
  c4_reserve_code   a C4 step due LEDGER_UNAVAILABLE is answered with a reserve-stage code
                    (GRANT_UNKNOWN, IDEMPOTENCY_CONFLICT, RECONCILIATION_REQUIRED, or a replay; rotating)
  heal_on_restart   restart_broker heals the dependencies: the input gets `set_dependency ... up`
                    after every restart taken while one was down, and those answers are removed
  c4_hidden_effect, c4_hidden_consumption, c4_hidden_dispatch, c4_hidden_uncertain
                    the `ledger_detail` right after a C4 LEDGER_UNAVAILABLE refusal shows an
                    effect, a consumed permit, a dispatched receipt, or an uncertain commit
                    (failure record with possible progress) that the refusal hides
  f_grant_overuse, f_double_dispatch, f_duplicate_effect, f_effect_without_permit, f_unsafe_reconcile,
  f_redispatch, f_group_outcome
                    faulted concurrent groups (`concurrent_faults`): the final `ledger_detail` shows a
                    grant used twice, a key dispatched twice, a duplicate effect, an effect without a
                    consumed permit, a receipt without evidence reconciled as observed; a retry of an
                    uncertain receipt answers effect_completed; one LEDGER_UNAVAILABLE of the group is
                    reported as effect_completed
"""
from __future__ import annotations

import argparse
import copy
import json
import subprocess
import sys

MODES = ["m1_b36", "conc_extra", "refusals", "c4_ledger_first", "c4_decision_first", "c4_reserve_code",
         "heal_on_restart", "c4_hidden_effect", "c4_hidden_consumption", "c4_hidden_dispatch",
         "c4_hidden_uncertain", "f_grant_overuse", "f_double_dispatch", "f_duplicate_effect",
         "f_effect_without_permit", "f_unsafe_reconcile", "f_redispatch", "f_group_outcome"]
LU = {"code": "LEDGER_UNAVAILABLE", "result": "refused"}
RESERVE_CODES = [{"code": "GRANT_UNKNOWN", "result": "refused"}, {"code": "IDEMPOTENCY_CONFLICT", "result": "refused"},
                 {"code": "RECONCILIATION_REQUIRED", "result": "refused"},
                 {"replayed": True, "result": "effect_completed"}]
_rot = [0]


def ledger_down_steps(inp) -> list:
    """Per step: was the ledger down when it ran (and was it a sealed execute)."""
    steps = (inp or {}).get("steps") or []
    down = ((inp or {}).get("initial") or {}).get("dependencies", {}).get("ledger") == "down"
    marks = []
    for s in steps:
        if s.get("op") == "set_dependency" and s.get("name") == "ledger":
            down = s.get("status") == "down"
        marks.append(down and s.get("op") == "execute" and s.get("seal") is not None)
    return marks


def heal_input(inp):
    """Insert `set_dependency x up` after every restart taken while x is down; returns the new
    input and the indices of the inserted steps."""
    inp = copy.deepcopy(inp)
    down = {k: v == "down" for k, v in (inp.get("initial") or {}).get("dependencies", {}).items()}
    steps, inserted, index = [], [], {}
    for i, s in enumerate(inp.get("steps") or []):
        index[i] = len(steps)
        steps.append(s)
        if s.get("op") == "set_dependency":
            down[s.get("name")] = s.get("status") == "down"
        elif s.get("op") == "restart_broker":
            for name in sorted(n for n, d in down.items() if d):
                inserted.append(len(steps))
                steps.append({"op": "set_dependency", "name": name, "status": "up"})
                down[name] = False
    for s in steps:
        if s.get("op") == "present_permit" and isinstance(s.get("of_step"), int) and s["of_step"] in index:
            s["of_step"] = index[s["of_step"]]
    inp["steps"] = steps
    return inp, inserted


def mutate(mode: str, out, inp):
    if not isinstance(out, dict) or not isinstance(out.get("steps"), list):
        return out
    steps = out["steps"]
    isteps = (inp or {}).get("steps") or []
    if mode == "m1_b36":
        resolved = False
        for i, s in enumerate(steps):
            if isinstance(s, dict) and s.get("result") == "reconciled" and s.get("count", 0) > 0:
                resolved = True
            elif resolved and s == {"replayed": True, "result": "effect_completed"}:
                steps[i] = {"code": "RECONCILIATION_REQUIRED", "result": "refused"}
                if isinstance(out.get("final"), dict):
                    out["final"]["refusals"] = out["final"].get("refusals", 0) + 1
    elif mode == "conc_extra":
        for s in steps:
            o = s.get("outcomes") if isinstance(s, dict) else None
            if isinstance(o, dict) and o.get("IDEMPOTENCY_CONFLICT"):
                o["IDEMPOTENCY_CONFLICT"] -= 1
                if not o["IDEMPOTENCY_CONFLICT"]:
                    del o["IDEMPOTENCY_CONFLICT"]
                o["effect_completed"] = o.get("effect_completed", 0) + 1
                break
    elif mode == "refusals" and isinstance(out.get("final"), dict):
        out["final"]["refusals"] = out["final"].get("refusals", 0) + 1
    elif mode in ("c4_ledger_first", "c4_decision_first", "c4_reserve_code"):
        for i, c4 in enumerate(ledger_down_steps(inp)):
            r = steps[i] if i < len(steps) else None
            if not c4 or not isinstance(r, dict) or r.get("result") != "refused":
                continue
            if mode == "c4_ledger_first" and r.get("code") not in ("LEDGER_UNAVAILABLE", "INGRESS_UNAUTHENTICATED"):
                steps[i] = dict(LU)
            elif mode == "c4_decision_first" and r == LU:
                steps[i] = {"code": "APPROVAL_REQUIRED", "result": "refused"}
            elif mode == "c4_reserve_code" and r == LU:
                steps[i] = dict(RESERVE_CODES[_rot[0] % len(RESERVE_CODES)])
                _rot[0] += 1
                if steps[i].get("replayed") and isinstance(out.get("final"), dict):
                    out["final"]["refusals"] = out["final"].get("refusals", 0) - 1
    elif mode.startswith("c4_hidden_"):
        marks = ledger_down_steps(inp)
        for i in range(1, len(steps)):
            d = steps[i]
            if not (i < len(isteps) and isteps[i].get("op") == "ledger_detail" and marks[i - 1]
                    and steps[i - 1] == LU and isinstance(d, dict)):
                continue
            k = "fixture-tenant/hidden"
            if mode == "c4_hidden_effect":
                d["effects"][k] = 1
            elif mode == "c4_hidden_consumption":
                d["consumed"][k + "/applied"] = 1
            elif mode == "c4_hidden_dispatch":
                d["receipts"][k] = {"dispatches": 1, "permit": 99, "status": "dispatched"}
                d["dispatch_events"] += 1
            elif d.get("failures"):
                d["failures"][-1] = dict(d["failures"][-1], possible_progress="dispatched_with_effect",
                                         stage="commit", outcome_unknown=1)
    elif mode.startswith("f_"):
        gi = next((i for i, s in enumerate(isteps) if s.get("op") == "concurrent_faults"), None)
        if gi is None:
            return out
        b = steps[-1] if isinstance(steps[-1], dict) else {}
        if mode == "f_group_outcome":
            o = steps[gi].get("outcomes", {})
            if o.get("LEDGER_UNAVAILABLE"):
                o["LEDGER_UNAVAILABLE"] -= 1
                if not o["LEDGER_UNAVAILABLE"]:
                    del o["LEDGER_UNAVAILABLE"]
                o["effect_completed"] = o.get("effect_completed", 0) + 1
                b["refusals"] = b.get("refusals", 0) - 1
        elif mode == "f_grant_overuse" and b.get("grant_uses"):
            g = sorted(b["grant_uses"])[0]
            b["grant_uses"][g] = 2
            b["grant_use_rows"][g] = 2
        elif mode in ("f_double_dispatch", "f_unsafe_reconcile"):
            for k, r in sorted(b.get("receipts", {}).items()):
                if mode == "f_double_dispatch" and r["dispatches"] == 1:
                    r["dispatches"] = 2
                    b["dispatch_events"] += 1
                    break
                if mode == "f_unsafe_reconcile" and r["status"] == "unknown_reconcile":
                    r["status"] = "observed_success"
                    break
        elif mode == "f_duplicate_effect" and b.get("effects"):
            k = sorted(b["effects"])[0]
            b["effects"][k] = 2
        elif mode == "f_effect_without_permit":
            b.setdefault("effects", {})["fixture-tenant/no-permit"] = 1
        elif mode == "f_redispatch":
            for i in range(gi + 1, len(steps)):
                if isteps[i].get("op") == "reconcile":
                    break
                if steps[i] == {"code": "RECONCILIATION_REQUIRED", "result": "refused"}:
                    steps[i] = {"result": "effect_completed"}
                    b["refusals"] = b.get("refusals", 0) - 1
                    break
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--real", required=True)
    ap.add_argument("--mode", required=True, choices=MODES)
    ap.add_argument("command")
    args = ap.parse_args()
    inputs, lines, removed = {}, [], {}
    for raw in sys.stdin.buffer.read().decode("utf-8").split("\n"):
        if not raw.strip():
            continue
        env = json.loads(raw)
        if isinstance(env, dict) and isinstance(env.get("input"), dict):
            if args.mode == "heal_on_restart":
                env["input"], removed[env.get("id")] = heal_input(env["input"])
            inputs[env.get("id")] = env["input"]
        lines.append(json.dumps(env, ensure_ascii=False, separators=(",", ":")))
    payload = ("\n".join(lines) + "\n").encode("utf-8") if lines else b""
    proc = subprocess.run([args.real, args.command], input=payload, capture_output=True)
    for raw in proc.stdout.decode("utf-8").split("\n"):
        if not raw.strip():
            continue
        msg = json.loads(raw)
        out, inp = msg.get("output"), inputs.get(msg.get("id"))
        drop = removed.get(msg.get("id"))
        if drop and isinstance(out, dict) and isinstance(out.get("steps"), list):
            out["steps"] = [s for i, s in enumerate(out["steps"]) if i not in set(drop)]
        elif args.mode != "heal_on_restart":
            out = mutate(args.mode, out, inp)
        msg["output"] = out
        sys.stdout.write(json.dumps(msg, ensure_ascii=False, separators=(",", ":")) + "\n")
    sys.stderr.buffer.write(proc.stderr)
    return proc.returncode


if __name__ == "__main__":
    sys.exit(main())
