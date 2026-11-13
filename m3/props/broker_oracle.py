"""M3-08 property gate, `broker` campaign, oracle-only mode against the production adapter.

Reuses the frozen M1 `broker` campaign (m1/props/wprops/c_broker.py) read-only: its
generators (sequences, restart twins, concurrent groups, probes), its run invariants
(`check_run`), its per-operation execute and approval oracles (`check_ops`) and its
exhaustive serializability search (`serializable`). Nothing is written into m1/
(`sys.dont_write_bytecode` is set before the import). There is one implementation
under test (the production broker behind `m3_line_adapter broker-scenario`), so the M1
differential comparison is replaced by oracles only.

Inputs kept out of the oracle domain (D-037 item 5, approved production differences):
- `apply_without_permit` steps are removed from generated sequences (production has no
  effect path without a permit; the refusal-insertion twin never inserts one);
- presenting an absent permit is removed: a `present_permit` whose `of_step` is not an
  execute is removed statically; one whose execute issued no permit is removed after the
  adapter reports `NO_PERMIT_TO_PRESENT`, but only when the M1 rule also says no permit
  was issued (otherwise that answer is a failure, `broker.m3.present_permit_issued_is_presentable`).

Approved differences applied in the oracle instead of the M1 expectation:
- b-36 (D-035 item 3, D-036 item 4): after `crash_after_dispatch` the receipt stays
  `dispatched`; an identical retry before reconciliation is `RECONCILIATION_REQUIRED`;
  `reconcile` resolves each dispatched receipt from the controlled adapter's durable
  evidence (count = number of dispatched receipts) to `observed_success`; an identical
  retry afterwards replays `{"replayed": true, "result": "effect_completed"}` and applies
  no further effect (M1 kept `unknown_reconcile` and answered `RECONCILIATION_REQUIRED`).
- Permit presentation (D-033 item 2, D-035 item 1): order fingerprint, MAC, expiry, then
  the active-bundle check (`PERMIT_STALE` for a permit bound to a bundle other than the
  active one, or with no active policy); grant-backed permits expire at
  min(grant expiry, issue time + 30 s). The exact expiry instant stays unasserted (M1).

Adapter limitation (not a semantic choice): the adapter drives concurrent groups of
unfaulted executes only, so generated concurrent groups have their faults removed
(the M1 abstract model then has no crash or failure branch in the group).
"""
from __future__ import annotations

import hashlib
import json
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
M1_PROPS = ROOT / "m1" / "props"
M1_FILES = ("wprops/c_broker.py", "wprops/common.py")

sys.dont_write_bytecode = True  # never write into the frozen m1/ tree
if str(M1_PROPS) not in sys.path:
    sys.path.insert(0, str(M1_PROPS))
from wprops import c_broker as m1  # noqa: E402
from wprops.common import Proto, Results, clone, digest  # noqa: E402

EXCLUDED_OPS = ("apply_without_permit",)
ABSENT_PERMIT = "NO_PERMIT_TO_PRESENT"
PERMIT_TTL = 30
DEFAULT_GRANT_TTL = 600
REPLAY = {"completed": {"replayed": True, "result": "effect_completed"},
          "failed": {"replayed": True, "result": "effect_failed"}}


def m1_digests() -> dict:
    return {f: "sha256:" + hashlib.sha256((M1_PROPS / f).read_bytes()).hexdigest() for f in M1_FILES}


# ------------------------------------------------------------------ generation

def remove_steps(case: dict, drop: set) -> dict:
    """Remove step indices `drop`; remap present_permit targets and drop presentations
    of removed steps (repeated until stable)."""
    c = clone(case)
    while drop:
        steps, info = c["input"]["steps"], c["meta"]["info"]
        new_index, k = {}, 0
        for i in range(len(steps)):
            if i not in drop:
                new_index[i] = k
                k += 1
        ns, ni, more = [], [], set()
        for i, (s, inf) in enumerate(zip(steps, info)):
            if i in drop:
                continue
            if s.get("op") == "present_permit":
                j = s["of_step"]
                if j not in new_index:
                    more.add(new_index[i])
                s = dict(s, of_step=new_index.get(j, j))
                inf = dict(inf, of=new_index.get(j, j))
            ns.append(s)
            ni.append(inf)
        c["input"]["steps"], c["meta"]["info"] = ns, ni
        drop = more
    return c


def adapt_sequence(case: dict) -> dict:
    """Out-of-domain operations removed (D-037 item 5): apply_without_permit, and
    presentations whose target step is not an execute (they can never name a permit)."""
    steps = case["input"]["steps"]
    drop = {i for i, s in enumerate(steps) if s.get("op") in EXCLUDED_OPS}
    absent = {i for i, s in enumerate(steps) if s.get("op") == "present_permit"
              and not (0 <= s["of_step"] < len(steps) and steps[s["of_step"]].get("op") == "execute")}
    case["meta"]["excluded"] = {"apply_without_permit": len(drop), "present_permit_of_non_execute": len(absent)}
    drop |= absent
    # Adapter limitation: restart builds a new storage wrapper, so the C4 `ledger: down`
    # injection does not survive restart_broker (M1: dependency status is configuration
    # outside the broker process). Such restarts are removed rather than modelled.
    down = False
    for i, s in enumerate(steps):
        if s.get("op") == "set_dependency" and s.get("name") == "ledger":
            down = s.get("status") == "down"
        elif s.get("op") == "restart_broker" and down:
            drop.add(i)
            case["meta"]["restart_removed_ledger_down"] = case["meta"].get("restart_removed_ledger_down", 0) + 1
    return remove_steps(case, drop)


def insert_refusal(rng, case: dict) -> dict:
    """M1 refusal-insertion twin without apply_without_permit: an unsealed execute
    (INGRESS_UNAUTHENTICATED) or an approval with a wrong credential (HUMAN_UNAUTHENTICATED)."""
    c = clone(case)
    steps = c["input"]["steps"]
    pos = rng.randint(0, len(steps))
    ins = rng.choice([m1.execute(m1.request("W1", "fresh-" + str(pos)), seal_kind="absent"),
                      m1.approve(m1.request("P1", "k1"), "g9", cred="guess")])
    steps.insert(pos, ins)
    for s in steps:
        if s.get("op") == "present_permit" and s is not ins and s["of_step"] >= pos:
            s["of_step"] += 1
    c["id"] = case["id"] + f"-refusal{pos}"
    c["meta"] = {"family": "inserted_refusal", "twin_of": case["id"], "pos": pos}
    return c


def gen_base(rng, n: int) -> list:
    """M1 `gen_cases` order and RNG use for the base sequences; twins are made after
    pruning (so a twin is always derived from the case actually run)."""
    base, i = [], 0
    while len(base) < n:
        i += 1
        simple = rng.random() < 0.5
        c = m1.gen_sequence(rng, i, allow_deps=not simple, allow_conc=not simple and rng.random() < 0.5)
        c = adapt_sequence(c)
        c["meta"]["simple"] = simple
        base.append(c)
    return base


def gen_b36(rng, idx: int) -> dict:
    """Focused b-36 sequences built from the M1 request, approve and execute builders (the M1
    generator rarely retries an identical request after crash_after_dispatch and reconcile):
    crash after dispatch, optional retries before reconciliation, restarts, repeated
    reconciliation, retries after it, permit presentation and an unrelated execute."""
    now = m1.T0
    deps = {"ledger": "up", "policy": "up"}
    steps, info = [], []

    def add(op, inf):
        steps.append(op)
        info.append(inf)

    v = rng.choice(["W1", "W2", "R1", "P1", "X1"])
    pr = rng.choice([0, 1])
    key = rng.choice(["k1", "k2"])
    req = m1.request(v, key, pr)
    g = None
    if m1.VARIANTS[v][2] == "require_approval":
        g = "g1"
        add(m1.approve(req, g), {"kind": "approve", "mode": "ok", "variant": v})

    def ex(fault=None, r=req, gr=g, vv=v):
        add(m1.execute(r, gr, fault), {"kind": "execute", "variant": vv, "seal": "valid", "deps": dict(deps),
                                       "grant": gr, "fault": fault, "now": now})

    ex("crash_after_dispatch")
    crash = len(steps) - 1
    for _ in range(rng.randint(0, 2)):
        ex()
    for _ in range(rng.randint(1, 6)):
        r = rng.random()
        if r < 0.3:
            add({"op": "reconcile"}, {"kind": "reconcile"})
        elif r < 0.6:
            ex(rng.choice([None, None, None, "effect_fails"]))
        elif r < 0.7:
            add({"op": "restart_broker"}, {"kind": "restart"})
        elif r < 0.8:
            now += rng.choice([1, 10])
            add({"op": "advance_time", "now": m1.ts(now)}, {"kind": "time"})
        elif r < 0.9:
            add({"op": "present_permit", "of_step": crash, "request": req},
                {"kind": "present", "of": crash, "same": True, "tamper": None, "now": now})
        else:
            other = m1.request("W2" if v != "W2" else "W1", "k3", pr)
            add(m1.execute(other), {"kind": "execute", "variant": "W2" if v != "W2" else "W1", "seal": "valid",
                                    "deps": dict(deps), "grant": None, "fault": None, "now": now})
    if not any(s.get("op") == "reconcile" for s in steps):
        add({"op": "reconcile"}, {"kind": "reconcile"})
    ex()
    return {"id": f"brb36-{idx:06d}", "command": "broker-scenario",
            "input": {"initial": m1.initial(), "steps": steps},
            "meta": {"family": "sequence", "info": info, "simple": True,
                     "excluded": {"apply_without_permit": 0, "present_permit_of_non_execute": 0}}}


def gen_concurrent(rng, idx: int, n: int) -> dict:
    """M1 concurrent group with faults removed (the adapter drives unfaulted groups only)."""
    c = m1.gen_concurrent(rng, idx, n)
    for op in c["input"]["steps"][-1]["operations"]:
        op.pop("fault", None)
    for t in c["meta"]["op_types"]:
        t[4] = None
    return c


def probes() -> list:
    """M1 probes except those presenting an absent permit (D-037 item 5)."""
    return [p for p in m1.probes() if p["id"] not in ("brp-present_bad_index", "brp-present_negative_index")]


# --------------------------------------------------------------------- oracles

def is_unsupported(out) -> bool:
    return isinstance(out, dict) and "unsupported" in out


def m1_issued(steps, got, j) -> bool:
    """The M1 `check_ops` rule: did step j issue a permit?"""
    src, srcr = steps[j], got[j] if j < len(got) else None
    if src.get("op") != "execute" or not isinstance(srcr, dict) or srcr.get("replayed"):
        return False
    return srcr.get("result") in ("effect_completed", "effect_failed") or (
        srcr.get("result") == "crashed" and src.get("fault") == "crash_after_dispatch")


def track(case: dict, out: dict, res: Results, pname: str):
    """Production-specific sequential oracles (b-36, permit presentation, reconcile,
    final receipt statuses). Runs up to the first concurrent step (after one the
    receipts are not known without an order)."""
    cid = case["id"]
    steps, info, got = case["input"]["steps"], case["meta"]["info"], out["steps"]
    now = m1.T0
    bundle = digest(m1.REF)
    policy_up = ledger_up = True
    grants = {}    # label -> expiry of the current grant
    receipts = {}  # key -> {"req", "grant", "status", "grant_expiry", "via_reconcile"}
    permits = {}   # step -> {"issued", "expires", "bundle", "req"}
    complete = True

    def check(inv, i, r, want):
        if isinstance(want, (set, frozenset)):
            ok = isinstance(r, dict) and r.get("result") == "refused" and r.get("code") in want
            want = sorted(want)
        else:
            ok = r == want
        res.check(inv, pname, ok, cid, {"step": i, "got": r, "want": want}, case=None if ok else case)

    for i, (op, inf, r) in enumerate(zip(steps, info, got)):
        kind = op.get("op")
        if kind == "concurrent":
            complete = False
            break
        if kind == "advance_time":
            now = _parse(op["now"])
        elif kind == "activate_bundle":
            bundle = digest(op["bundle"])
        elif kind == "set_dependency":
            if op["name"] == "ledger":
                ledger_up = op["status"] == "up"
            elif op["name"] == "policy":
                policy_up = op["status"] == "up"
        elif kind == "approve":
            if r == {"result": "grant_issued"}:
                grants[op["grant_ref"]] = now + (op.get("ttl_seconds") or DEFAULT_GRANT_TTL)
        elif kind == "reconcile":
            pending = [k for k, v in receipts.items() if v["status"] == "dispatched"]
            if ledger_up and all(v["status"] != "unknown" for v in receipts.values()):
                check("broker.m3.b36_reconcile_resolves_every_dispatched_receipt_from_evidence", i, r,
                      {"count": len(pending), "result": "reconciled"})
            if isinstance(r, dict) and r.get("result") == "reconciled":
                for k in pending:
                    receipts[k].update(status="observed_success", via_reconcile=True)
        elif kind == "present_permit":
            p = permits.get(op["of_step"])
            if p is None:
                continue
            if op["request"] != p["req"]:
                want = {"result": "refused", "code": "PERMIT_REQUEST_MISMATCH"}
            elif op.get("tamper"):
                want = {"result": "refused", "code": "PERMIT_INVALID"}
            elif p["expires"] is None or now == p["expires"]:
                want = None  # grant expiry unknown, or the exact expiry instant (unspecified in M1)
            elif now > p["expires"]:
                want = {"result": "refused", "code": "PERMIT_EXPIRED"}
            elif not policy_up:
                want = {"PERMIT_STALE", "POLICY_UNAVAILABLE"}  # D-033 item 2: no active policy fails closed
            elif bundle != p["bundle"]:
                want = {"result": "refused", "code": "PERMIT_STALE"}
            else:
                want = {"result": "refused", "code": "PERMIT_REPLAYED"}
            if want is not None:
                check("broker.m3.present_permit_production_order", i, r, want)
        elif kind == "execute":
            key = op["request"]["idempotency_key"]
            rec = receipts.get(key)
            eligible = (inf.get("seal") == "valid" and ledger_up and policy_up and op.get("fault") is None
                        and rec is not None and rec["req"] == op["request"] and rec["grant"] == op.get("grant_ref"))
            if eligible and rec["status"] == "dispatched":
                check("broker.m3.b36_identical_retry_before_reconcile_requires_reconciliation", i, r,
                      {"result": "refused", "code": "RECONCILIATION_REQUIRED"})
            elif eligible and rec["status"] == "observed_success" and rec["via_reconcile"]:
                check("broker.m3.b36_identical_retry_after_evidence_reconcile_replays", i, r, REPLAY["completed"])
            elif eligible and rec["status"] in ("observed_success", "observed_failure"):
                check("broker.m3.identical_retry_of_observed_receipt_replays", i, r,
                      REPLAY["completed" if rec["status"] == "observed_success" else "failed"])
            if not isinstance(r, dict):
                continue
            result, fault = r.get("result"), op.get("fault")
            fresh = result in ("effect_completed", "effect_failed") and not r.get("replayed")
            status = None
            if fresh:
                status = "observed_success" if result == "effect_completed" else "observed_failure"
            elif result == "crashed" and fault == "crash_after_reserve":
                status = "reserved"
            elif result == "crashed" and fault == "crash_after_dispatch":
                status = "dispatched"
            if status is None:
                continue
            if rec is not None and rec["status"] != "reserved":
                rec["status"] = "unknown"  # a second reservation of a settled key: not modelled
                continue
            if rec is None:
                approval = m1.VARIANTS_BY_KIND.get(op["request"]["action"]["kind"]) == "require_approval"
                g = op.get("grant_ref")
                rec = {"req": op["request"], "grant": g, "via_reconcile": False,
                       "grant_expiry": grants.get(g, -1) if approval else None}
                receipts[key] = rec
            rec["status"] = status
            if status in ("observed_success", "observed_failure", "dispatched"):
                ge = rec["grant_expiry"]
                expires = None if ge == -1 else (now + PERMIT_TTL if ge is None else min(now + PERMIT_TTL, ge))
                permits[i] = {"req": op["request"], "bundle": bundle, "expires": expires}
    if complete and all(v["status"] != "unknown" for v in receipts.values()):
        want = dict(Counter(v["status"] for v in receipts.values()))
        fin = (out.get("final") or {}).get("receipts")
        ok = fin == want
        res.check("broker.m3.final_receipt_statuses", pname, ok, cid, {"final": fin, "want": want},
                  case=None if ok else case)


def _parse(stamp: str) -> int:
    import calendar
    import time as _t
    return calendar.timegm(_t.strptime(stamp, "%Y-%m-%dT%H:%M:%SZ"))


def check_sequence(res: Results, pname: str, case: dict, out) -> None:
    cid = case["id"]
    ok = isinstance(out, dict) and not is_unsupported(out) and "adapter_error" not in out
    res.check("broker.m3.answered_in_domain", pname, ok, cid, out, case=None if ok else case)
    if not ok:
        return
    m1.check_run(res, pname, case, out, None)
    if not (isinstance(out.get("steps"), list) and len(out["steps"]) == len(case["input"]["steps"])):
        return
    if case["meta"]["family"] == "sequence":
        # M1 per-operation oracles for execute and approve; presentations use the production
        # order (track) and executes under `ledger: down` the C4 rule (check_c4)
        info = case["meta"]["info"]
        view = dict(case, meta=dict(case["meta"], info=[
            dict(x, kind="present_m3") if x["kind"] == "present" else
            dict(x, kind="execute_c4") if is_c4(x) else x for x in info]))
        m1.check_ops(res, pname, view, out)
        check_c4(res, pname, case, out)
        track(case, out, res, pname)


def is_c4(inf: dict) -> bool:
    return inf["kind"] == "execute" and inf["seal"] == "valid" and inf["deps"]["ledger"] == "down"


def check_c4(res: Results, pname: str, case: dict, out) -> None:
    """`ledger: down` in the adapter is C4, partial transaction unavailability (D-037 item 7):
    lifecycle transactions fail at begin, the refusal audit and time floor stay available.
    M1 BROKER.md execute step 2 puts LEDGER_UNAVAILABLE before the decision; under C4 a
    refusal that needs no lifecycle transaction (deny, POLICY_UNAVAILABLE, APPROVAL_REQUIRED)
    is answered by the decision instead. Not an approved exception: the oracle asserts fail
    closed (refused with LEDGER_UNAVAILABLE or the decision's own code, never an effect) and the
    report counts how often the decision's code wins (`c4_decision_code_before_ledger`)."""
    for i, (inf, r) in enumerate(zip(case["meta"]["info"], out["steps"])):
        if not is_c4(inf):
            continue
        cls, deny = m1.VARIANTS[inf["variant"]][2], m1.VARIANTS[inf["variant"]][3]
        allowed = {"LEDGER_UNAVAILABLE"}
        if inf["deps"]["policy"] == "down":
            allowed.add("POLICY_UNAVAILABLE")
        elif cls == "deny":
            allowed.add(deny)
        elif cls == "require_approval" and inf["grant"] is None:
            allowed.add("APPROVAL_REQUIRED")
        ok = isinstance(r, dict) and r.get("result") == "refused" and r.get("code") in allowed
        res.check("broker.m3.c4_ledger_down_fails_closed", pname, ok, case["id"],
                  {"step": i, "got": r, "allowed": sorted(allowed)}, case=None if ok else case)
        if ok and r["code"] != "LEDGER_UNAVAILABLE":
            res.c4_decision_first = getattr(res, "c4_decision_first", 0) + 1


def check_twin(res: Results, pname: str, case: dict, out, base) -> None:
    pos = case["meta"]["pos"]
    restart = case["meta"]["family"] == "inserted_restart"
    ok = all(isinstance(x, dict) and isinstance(x.get("steps"), list) and isinstance(x.get("final"), dict)
             for x in (out, base)) and len(out["steps"]) == len(base["steps"]) + 1
    if ok:
        s2 = list(out["steps"])
        ins = s2.pop(pos)
        f2 = dict(out["final"])
        if restart:
            ok = ins == {"result": "restarted"}
        else:
            f2["refusals"] = f2.get("refusals", 0) - 1
            ok = isinstance(ins, dict) and ins.get("result") == "refused"
        ok = ok and s2 == base["steps"] and f2 == base["final"]
    inv = "broker.restart_is_transparent" if restart else "broker.inserted_refusal_changes_nothing_else"
    res.check(inv, pname, ok, case["id"], {"with": out, "without": base}, case=None if ok else case)


def check_group(res: Results, pname: str, case: dict, out) -> str | None:
    """Run invariants plus serializability of one concurrent group; returns the outcome signature."""
    cid = case["id"]
    ok = isinstance(out, dict) and not is_unsupported(out) and "adapter_error" not in out
    res.check("broker.m3.answered_in_domain", pname, ok, cid, out, case=None if ok else case)
    if not ok:
        return None
    m1.check_run(res, pname, case, out, None)
    if "steps" not in out or not out["steps"]:
        return None
    grp = out["steps"][-1]
    observed = grp.get("outcomes") if isinstance(grp, dict) else None
    ok = isinstance(observed, dict) and grp.get("result") == "concurrent" and \
        all(r == {"result": "grant_issued"} for r in out["steps"][:-1])
    if ok:
        ok = m1.serializable(case, observed, out.get("final") or {})
    if ok is None:
        res.check("broker.concurrent_serialization_search_completed", pname, False, cid, {"outcomes": observed})
        return None
    res.check("broker.concurrent_group_equals_a_serialization", pname, ok, cid,
              {"outcomes": observed, "final": out.get("final")}, case=None if ok else case)
    return ",".join(f"{k}={v}" for k, v in sorted((observed or {}).items()))


# ------------------------------------------------------------------------ run

def run_cases(proto: Proto, cases: list, res: Results) -> dict:
    out, problems = proto.run("broker-scenario", cases)
    res.protocol[proto.name].extend(f"broker-scenario: {p}" for p in problems)
    return out


def prune(proto: Proto, base: list, res: Results, rounds: int = 20) -> tuple[list, int]:
    """Remove presentations of permits the adapter reports absent, where the M1 rule
    agrees no permit was issued; repeat until no case needs it."""
    pending, done, pruned = list(base), {}, 0
    for _ in range(rounds):
        if not pending:
            break
        out = run_cases(proto, pending, res)
        again = []
        for c in pending:
            o = out.get(c["id"])
            u = o.get("unsupported") if isinstance(o, dict) else None
            if not (isinstance(u, dict) and u.get("reason") == ABSENT_PERMIT and isinstance(u.get("step"), int)):
                done[c["id"]] = c
                continue
            i = u["step"]
            steps = c["input"]["steps"]
            issued = m1_issued(steps, o.get("steps") or [], steps[i]["of_step"])
            ok = res.check("broker.m3.present_permit_issued_is_presentable", proto.name, not issued, c["id"],
                           {"step": i, "of_step": steps[i]["of_step"]}, case=c if issued else None)
            if not ok:
                done[c["id"]] = c
                continue
            pruned += 1
            again.append(remove_steps(c, {i}))
        pending = again
    for c in pending:
        res.check("broker.m3.pruning_converged", proto.name, False, c["id"])
        done[c["id"]] = c
    return [done[c["id"]] for c in base], pruned


def run(proto: Proto, rng, n: int, res: Results, repeats: int) -> dict:
    stats = {}
    probe_cases = probes()
    pout = {}
    for c in probe_cases:  # one process per probe, so a crash cannot hide other results
        o, probs = proto.run(c["command"], [c])
        pout[c["id"]] = o.get(c["id"])
        ok = not probs
        res.check("broker.probe_answered_without_crash", proto.name, ok, c["id"], probs[:2], case=None if ok else c)

    generated = gen_base(rng, n)
    generated += [gen_b36(rng, i) for i in range(1, max(20, n // 8) + 1)]
    base, pruned = prune(proto, generated, res)
    cases = []
    for c in base:
        cases.append(c)
        if c["meta"].pop("simple"):
            cases.append(m1.insert_twin(rng, c, "restart"))
            cases.append(insert_refusal(rng, c))
    for c in cases:
        assert not any(s.get("op") in EXCLUDED_OPS for s in c["input"]["steps"])
    outs = [run_cases(proto, cases, res) for _ in range(2)]
    for c in cases:
        a, b = outs[0].get(c["id"]), outs[1].get(c["id"])
        first = next((i for i, s in enumerate(c["input"]["steps"]) if s.get("op") == "concurrent"), None)
        if first is not None:  # a concurrent group fixes no order: compare the prefix before it
            a, b = [x.get("steps", [])[:first] if isinstance(x, dict) else x for x in (a, b)]
        ok = a == b
        res.check("broker.m3.deterministic_across_runs", proto.name, ok, c["id"],
                  {"first": outs[0].get(c["id"]), "second": outs[1].get(c["id"])}, case=None if ok else c)
    out = outs[0]
    for c in cases:
        if c["meta"]["family"] == "sequence":
            check_sequence(res, proto.name, c, out.get(c["id"]))
        else:
            check_twin(res, proto.name, c, out.get(c["id"]), out.get(c["meta"]["twin_of"]))

    conc, i = [], 0
    while len(conc) < max(12, n // 10):
        i += 1
        conc.append(gen_concurrent(rng, i, rng.choice([16, 32, 64])))
    dist = {}
    for _ in range(repeats):
        cout = run_cases(proto, conc, res)
        for c in conc:
            sig = check_group(res, proto.name, c, cout.get(c["id"]))
            if sig is not None:
                dist.setdefault(c["id"], Counter())[sig] += 1
                dist.setdefault(f"x{c['meta']['n']}", Counter())[sig] += 1
    stats.update({
        "probes": len(probe_cases), "sequences": len(base), "twins": len(cases) - len(base),
        "concurrent_groups": len(conc), "concurrent_sizes": dict(Counter(c["meta"]["n"] for c in conc)),
        "repeats": repeats,
        "excluded_steps": {
            "apply_without_permit": sum(c["meta"]["excluded"]["apply_without_permit"] for c in base),
            "present_permit_of_non_execute": sum(c["meta"]["excluded"]["present_permit_of_non_execute"]
                                                 for c in base),
            "present_permit_of_execute_without_permit": pruned,
            "restart_while_ledger_down": sum(c["meta"].get("restart_removed_ledger_down", 0) for c in base)},
        "c4_decision_code_before_ledger": getattr(res, "c4_decision_first", 0),
        "operations": dict(Counter(s.get("op") for c in cases for s in c["input"]["steps"])),
        "concurrency_distribution": {k: dict(v) for k, v in dist.items() if not k.startswith("brc-")},
        "concurrency_groups_with_multiple_outcomes": sum(1 for k, v in dist.items()
                                                         if k.startswith("brc-") and len(v) > 1),
    })
    return stats
