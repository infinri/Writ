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

C4 and restarts (D-038 items 5 and 7):
- every C4 execute step (`ledger: down`, sealed) has exactly one expected answer
  (`c4_expected`, stage-aware); `inserted_c4_inspection` twins put `ledger_detail` (a
  quiescent read-only inspection, otherwise a transparent restart) around each C4 step and
  prove the refusal reserved, consumed, dispatched and applied nothing, was audited, and left
  no possible progress (failure record `reserve begin no_change no_change`, or none);
- restarts while a dependency is down are generated and kept: the injected status persists
  until `set_dependency ... up`, so steps after the restart still see the outage.

Concurrent groups: the M1 groups run with their M1 faults removed (the `concurrent` op drives
unfaulted executes only; the M1 crash fault takes down the whole ledger and is not per
operation). Storage faults in groups use the generated-only `concurrent_faults` op
(`gen_fault_group`, `check_fault_group`): per-operation faults at T-reserve, T-dispatch and
T-observe (begin unavailable; commit issued and lost; commit issued and applied with an
unknown outcome), then recovery (restart, retries, reconcile).
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
    # Restarts while a dependency is down are kept (D-038 item 7): the adapter keeps the
    # injected status across restart_broker until `set_dependency ... up` (M1 BROKER.md:
    # dependency status is configuration outside the broker process). They are counted.
    c = remove_steps(case, drop)
    down = {"ledger": False, "policy": False}
    for s in c["input"]["steps"]:
        if s.get("op") == "set_dependency":
            down[s["name"]] = s.get("status") == "down"
        elif s.get("op") == "restart_broker" and any(down.values()):
            c["meta"]["restart_under_outage"] = c["meta"].get("restart_under_outage", 0) + 1
    return c


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


def gen_outage_restart(rng, idx: int) -> dict:
    """Focused D-038 item 7 sequences from the M1 builders: a dependency goes down, executes
    of every decision class run before and after one or more restarts, and only an explicit
    `set_dependency ... up` recovers (after which the same requests run normally)."""
    deps = {"ledger": "up", "policy": "up"}
    steps, info = [], []
    grants = {}

    def add(op, inf):
        steps.append(op)
        info.append(inf)

    def ex():
        v = rng.choice(list(m1.VARIANTS))
        g = grants.get(v) if m1.VARIANTS[v][2] == "require_approval" and rng.random() < 0.7 else None
        add(m1.execute(m1.request(v, rng.choice(["k1", "k2", "k3"])), g),
            {"kind": "execute", "variant": v, "seal": "valid", "deps": dict(deps), "grant": g, "fault": None,
             "now": m1.T0})

    for v, g in (("P1", "g1"), ("X1", "g2")):
        if rng.random() < 0.7:
            grants[v] = g
            add(m1.approve(m1.request(v, "k1"), g), {"kind": "approve", "mode": "ok", "variant": v})
    for name in rng.sample(["ledger", "policy"], rng.choice([1, 2])):
        deps[name] = "down"
        add({"op": "set_dependency", "name": name, "status": "down"}, {"kind": "deps"})
        for _ in range(rng.randint(0, 2)):
            ex()
    for _ in range(rng.randint(1, 3)):
        add({"op": "restart_broker"}, {"kind": "restart"})
        for _ in range(rng.randint(1, 3)):
            ex()
    for name in [n for n in deps if deps[n] == "down" and rng.random() < 0.7]:
        deps[name] = "up"
        add({"op": "set_dependency", "name": name, "status": "up"}, {"kind": "deps"})
        for _ in range(rng.randint(1, 2)):
            ex()
    return {"id": f"bro-{idx:06d}", "command": "broker-scenario",
            "input": {"initial": m1.initial(), "steps": steps},
            "meta": {"family": "sequence", "info": info, "simple": False, "restart_under_outage": sum(
                1 for s in steps if s.get("op") == "restart_broker"),
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


DECISION_STAGE = "decision"
RESERVE_OR_LATER = "reserve_or_later"


def ledger_up_answer(inf: dict) -> tuple[str, str | None]:
    """(stage, code) of the answer the M1 reference model (`check_ops` rules, ledger taken as
    up) gives the same execute step. The stage is derived from the rule that decides it, not
    from a code list (D-038 I-3 as corrected by the lead): ingress verification, the use-time
    decision (policy unavailable, every deny or invalid decision reason) and require_approval
    without a grant are decided before T-reserve (`Broker::execute_held`: `observe`,
    `authorize`, `ReserveRequest::new`); every other answer (grant refusals, idempotency
    conflict, reconciliation required, replay, resume, effect) is decided at or after T-reserve
    and depends on history, so its code is not needed."""
    if inf["seal"] != "valid":
        return DECISION_STAGE, "INGRESS_UNAUTHENTICATED"
    if inf["deps"]["policy"] == "down":
        return DECISION_STAGE, "POLICY_UNAVAILABLE"
    cls, deny = m1.VARIANTS[inf["variant"]][2], m1.VARIANTS[inf["variant"]][3]
    if cls == "deny":
        return DECISION_STAGE, deny
    if cls == "require_approval" and inf["grant"] is None:
        return DECISION_STAGE, "APPROVAL_REQUIRED"
    return RESERVE_OR_LATER, None


def ledger_up_code(inf: dict) -> str | None:
    return ledger_up_answer(inf)[1]


def c4_expected(inf: dict) -> str:
    """D-038 I-3: a refusal decided before T-reserve is returned unchanged under C4; anything
    decided at or after T-reserve is LEDGER_UNAVAILABLE (T-reserve fails at begin)."""
    stage, code = ledger_up_answer(inf)
    return code if stage == DECISION_STAGE else "LEDGER_UNAVAILABLE"


def check_c4(res: Results, pname: str, case: dict, out) -> None:
    """`ledger: down` in the adapter is C4, partial transaction unavailability (D-037 item 7):
    lifecycle transactions fail at begin, the refusal audit and time floor stay available.
    D-038 item 5 (I-3): exactly one expected code per C4 execute step (`c4_expected`); the
    earlier either-code allowance is gone. `c4_stage_tally` counts the expected codes per stage.
    D-038 item 7: C4 steps after a `restart_broker` taken while the ledger was down (not
    recovered since) are also counted under `broker.m3.restart_does_not_heal_ledger_outage`;
    executes with the ledger up after a restart taken while the policy was down (not recovered
    since) must be POLICY_UNAVAILABLE (`broker.m3.restart_does_not_heal_policy_outage`)."""
    tally = getattr(res, "c4_stage_tally", None)
    if tally is None:
        tally = res.c4_stage_tally = Counter()
    down = {"ledger": False, "policy": False}
    restarted_down = dict(down)
    for i, (op, inf, r) in enumerate(zip(case["input"]["steps"], case["meta"]["info"], out["steps"])):
        kind = op.get("op")
        if kind == "set_dependency":
            down[op["name"]] = op["status"] == "down"
            if not down[op["name"]]:
                restarted_down[op["name"]] = False
        elif kind == "restart_broker":
            restarted_down = {n: restarted_down[n] or down[n] for n in down}
        if inf["kind"] != "execute" or inf["seal"] != "valid":
            continue
        if is_c4(inf):
            code = c4_expected(inf)
            want = {"code": code, "result": "refused"}
            ok = r == want
            res.check("broker.m3.c4_stage_aware_precedence", pname, ok, case["id"],
                      {"step": i, "got": r, "want": want, "ledger_up_code": ledger_up_code(inf)},
                      case=None if ok else case)
            tally[f"{ledger_up_answer(inf)[0]}:{code}"] += 1
            if restarted_down["ledger"]:
                res.check("broker.m3.restart_does_not_heal_ledger_outage", pname, ok, case["id"],
                          {"step": i, "got": r, "want": want}, case=None if ok else case)
        elif restarted_down["policy"] and inf["deps"]["policy"] == "down":
            want = {"code": "POLICY_UNAVAILABLE", "result": "refused"}
            ok = r == want
            res.check("broker.m3.restart_does_not_heal_policy_outage", pname, ok, case["id"],
                      {"step": i, "got": r, "want": want}, case=None if ok else case)


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


# -------------------------------------------- C4 inspection twins (D-038 item 5)

DETAIL = "ledger_detail"
STATE_KEYS = ("receipts", "grant_uses", "grant_use_rows", "consumed", "effects", "dispatch_events")
C4_FAILURE = {"outcome_unknown": 0, "possible_progress": "no_change", "progress": "no_change",
              "stage": "begin", "transaction": "reserve"}


def insert_c4_inspection(case: dict) -> dict | None:
    """Twin of a sequence with `ledger_detail` right before and right after each C4 execute."""
    info = case["meta"]["info"]
    c4 = {i for i, inf in enumerate(info) if is_c4(inf)}
    if not c4:
        return None
    c = clone(case)
    steps, new_index, positions = [], {}, []
    for i, s in enumerate(c["input"]["steps"]):
        if i in c4:
            steps.append({"op": DETAIL})
            positions.append(len(steps))
        new_index[i] = len(steps)
        steps.append(s)
        if i in c4:
            steps.append({"op": DETAIL})
    for s in steps:
        if s.get("op") == "present_permit":
            s["of_step"] = new_index[s["of_step"]]
    c["input"]["steps"] = steps
    c["id"] = case["id"] + "-c4inspect"
    c["meta"] = {"family": "inserted_c4_inspection", "twin_of": case["id"], "positions": positions}
    return c


def is_detail(x) -> bool:
    return isinstance(x, dict) and x.get("result") == DETAIL


def check_c4_inspection(res: Results, pname: str, case: dict, out, base) -> None:
    cid = case["id"]
    pos = case["meta"]["positions"]
    ins = sorted({p - 1 for p in pos} | {p + 1 for p in pos})
    ok = all(isinstance(x, dict) and isinstance(x.get("steps"), list) for x in (out, base)) and \
        len(out["steps"]) == len(base["steps"]) + len(ins) and all(is_detail(out["steps"][k]) for k in ins)
    if ok:
        rest = [s for k, s in enumerate(out["steps"]) if k not in set(ins)]
        first = next((k for k, s in enumerate(case["input"]["steps"]) if s.get("op") == "concurrent"), None)
        if first is None:
            ok = rest == base["steps"] and out.get("final") == base.get("final")
        else:  # a concurrent group fixes no order: the answers before it
            n = sum(1 for k in range(first) if case["input"]["steps"][k].get("op") != DETAIL)
            ok = rest[:n] == base["steps"][:n]
    res.check("broker.m3.ledger_detail_is_transparent", pname, ok, cid, {"with": out, "without": base},
              case=None if ok else case)
    if not ok:
        return
    for p in pos:
        before, r, after = out["steps"][p - 1], out["steps"][p], out["steps"][p + 1]
        diff = {k: (before.get(k), after.get(k)) for k in STATE_KEYS if before.get(k) != after.get(k)}
        res.check("broker.m3.c4_refusal_reserves_consumes_dispatches_applies_nothing", pname, not diff, cid,
                  {"step": p, "got": r, "changed": diff}, case=case if diff else None)
        ok = after.get("refusals") == before.get("refusals", -2) + 1
        res.check("broker.m3.c4_refusal_audited", pname, ok, cid,
                  {"step": p, "before": before.get("refusals"), "after": after.get("refusals")},
                  case=None if ok else case)
        fb, fa = before.get("failures") or [], after.get("failures") or []
        new = fa[len(fb):] if fa[:len(fb)] == fb else None
        want = [C4_FAILURE] if isinstance(r, dict) and r.get("code") == "LEDGER_UNAVAILABLE" else []
        ok = new == want
        res.check("broker.m3.c4_refusal_leaves_no_possible_progress", pname, ok, cid,
                  {"step": p, "got": r, "new_failure_records": new, "want": want}, case=None if ok else case)


# ------------------------------- concurrent groups with storage faults (D-038 item 7)

STORAGE_FAULTS = tuple(f"{tx}_{k}" for tx in ("reserve", "dispatch", "observe")
                       for k in ("unavailable", "commit_lost", "commit_unknown"))
FAULT_LU = "LEDGER_UNAVAILABLE[storage-fault]"  # model label; M1's search refuses a bare one
RESERVE_REFUSALS = {"IDEMPOTENCY_CONFLICT", "RECONCILIATION_REQUIRED", "GRANT_UNKNOWN", "GRANT_MISMATCH",
                    "GRANT_EXHAUSTED"}
TENANT = "fixture-tenant/"


def fault_model_step(orig, state, t):
    """M1 `model_step` extended with the adapter's per-operation storage faults (the fault
    fires only when its transaction runs; `unavailable` fails at begin, `commit_lost` reports
    an unknown commit that committed nothing, `commit_unknown` one that committed)."""
    fault = t[4]
    if fault not in STORAGE_FAULTS:
        return orig(state, t, None)
    tx, kind = fault.split("_", 1)
    base = tuple(t[:4]) + (None,) + tuple(t[5:])
    ns, out = orig(state, base, None)

    def override(name):
        return orig(state, tuple(t[:4]) + (f"{name}:",) + tuple(t[5:]), None)[0]

    if out == "APPROVAL_REQUIRED":  # decided before T-reserve
        return state, out
    if tx == "reserve":
        if kind != "commit_unknown" or out == "replayed":
            return state, FAULT_LU
        if out in RESERVE_REFUSALS:  # refused inside T-reserve: rolled back, nothing to commit
            return state, out
        return override("after_reserve"), FAULT_LU
    if out != "effect_completed":  # refused or replayed at T-reserve: the fault never fires
        return ns, out
    if tx == "dispatch":
        return override("after_reserve" if kind != "commit_unknown" else "adapter_unavailable"), FAULT_LU
    if kind != "commit_unknown":
        return override("observe_unavailable"), FAULT_LU
    return ns, FAULT_LU  # T-observe committed: observed, effect applied, answer unknown


def fault_serializable(case: dict, observed: dict, final: dict):
    obs = {(FAULT_LU if k == "LEDGER_UNAVAILABLE" else k): v for k, v in observed.items()}
    orig = m1.model_step
    m1.model_step = lambda state, t, fps=None: fault_model_step(orig, state, t)
    try:
        return m1.serializable(case, obs, final)
    finally:
        m1.model_step = orig


def gen_fault_group(rng, idx: int, n: int) -> dict:
    """An M1 concurrent group (approvals, then n executes) with M1 faults removed and storage
    faults added (each M1 operation type gets one or two faults; each operation of the type
    runs with one of them or none), driven by `concurrent_faults`; then `ledger_detail` (A),
    recovery (optional restart, retries, reconcile, optional restart, retries, reconcile) and
    `ledger_detail` (B)."""
    c = m1.gen_concurrent(rng, idx, n)
    steps = c["input"]["steps"]
    grp = steps[-1]
    gi = len(steps) - 1
    grp["op"] = "concurrent_faults"
    choices = {}
    reps = {}
    for op, t in zip(grp["operations"], c["meta"]["op_types"]):
        op.pop("fault", None)
        base = tuple(t[:4])
        if base not in choices:
            choices[base] = [None] + rng.sample(STORAGE_FAULTS, rng.choice([1, 2]))
        f = rng.choice(choices[base])
        t[4] = f
        if f:
            op["storage_fault"] = f
        reps.setdefault(base + (t[5],), op)
        if f:  # retries favor a faulted operation of the key (uncertain or reserved receipts)
            reps[base + (t[5],)] = op
    keys = sorted(reps, key=repr)
    rng.shuffle(keys)
    keys.sort(key=lambda k: "storage_fault" not in reps[k])
    picks = keys[:8]
    retry = [(list(k), {"op": "execute", "request": reps[k]["request"], "seal": reps[k]["seal"],
                        **({"grant_ref": reps[k]["grant_ref"]} if "grant_ref" in reps[k] else {})}) for k in picks]
    steps.append({"op": DETAIL})
    plan = []
    for phase in ("pre", "post"):
        if rng.random() < 0.5:
            steps.append({"op": "restart_broker"})
        for k, op in retry:
            plan.append((len(steps), k))
            steps.append(clone(op))
        steps.append({"op": "reconcile"})
    steps.append({"op": DETAIL})
    c["id"] = f"brf-{idx:06d}-x{n}"
    c["meta"].update(family="fault_group", group_index=gi, retries=plan)
    return c


def detail_final(d: dict) -> dict:
    return {"receipts": dict(Counter(r["status"] for r in d["receipts"].values())),
            "grant_uses": d["grant_uses"], "effects_applied": sum(d["effects"].values()),
            "consumed_permits": sum(d["consumed"].values())}


def check_detail_safety(res: Results, pname: str, case: dict, d: dict, where: str) -> None:
    cid = case["id"]

    def chk(inv, bad):
        res.check(inv, pname, not bad, cid, {"at": where, "violations": bad[:5]}, case=case if bad else None)

    rows = d.get("grant_use_rows") or {}
    chk("broker.m3.fault_no_grant_overuse",
        [(g, n, rows.get(g, 0)) for g, n in d["grant_uses"].items() if not (0 <= n <= 1 and rows.get(g, 0) == n)])
    chk("broker.m3.fault_at_most_one_dispatch_per_key",
        [k for k, r in d["receipts"].items() if r["dispatches"] > 1] +
        ([("dispatch_events", d["dispatch_events"])]
         if d["dispatch_events"] != sum(r["dispatches"] for r in d["receipts"].values()) else []))
    consumed = Counter()
    for k, n in d["consumed"].items():
        consumed[k.rsplit("/", 1)[0]] += n
    chk("broker.m3.fault_no_duplicate_effect",
        [k for k, n in d["effects"].items() if n > 1] + [k for k, n in consumed.items() if n > 1])
    bad = []
    for k, n in d["effects"].items():
        r = d["receipts"].get(k)
        if not (d["consumed"].get(k + "/applied") == 1 and r and r["dispatches"] == 1 and r["permit"] is not None
                and r["status"] != "reserved"):
            bad.append(("effect", k))
    for k in consumed:
        r = d["receipts"].get(k)
        if not (r and r["dispatches"] == 1 and r["permit"] is not None and r["status"] != "reserved"):
            bad.append(("consumed", k))
    chk("broker.m3.fault_effect_only_with_dispatched_receipt_and_consumed_permit", bad)


def check_fault_group(res: Results, pname: str, case: dict, out) -> str | None:
    """Serializability of the faulted group against detail A (extended M1 model), safety of
    A and B, and a sequential model of the recovery steps from A that must end at B."""
    cid = case["id"]
    ok = isinstance(out, dict) and not is_unsupported(out) and "adapter_error" not in out and \
        isinstance(out.get("steps"), list) and len(out["steps"]) == len(case["input"]["steps"])
    res.check("broker.m3.answered_in_domain", pname, ok, cid, out, case=None if ok else case)
    if not ok:
        return None
    steps, got = case["input"]["steps"], out["steps"]
    gi = case["meta"]["group_index"]
    grp, a, b = got[gi], got[gi + 1], got[-1]
    ok = all(r == {"result": "grant_issued"} for r in got[:gi]) and isinstance(grp, dict) and \
        grp.get("result") == "concurrent" and isinstance(grp.get("outcomes"), dict) and is_detail(a) and is_detail(b)
    res.check("broker.m3.fault_group_shape", pname, ok, cid, got[gi:gi + 2], case=None if ok else case)
    if not ok:
        return None
    observed = grp["outcomes"]
    ser = fault_serializable(case, observed, detail_final(a))
    if ser is None:
        res.check("broker.concurrent_serialization_search_completed", pname, False, cid, {"outcomes": observed})
    else:
        res.check("broker.m3.fault_group_equals_a_serialization", pname, ser, cid,
                  {"outcomes": observed, "detail": a}, case=None if ser else case)
    check_detail_safety(res, pname, case, a, "after group")
    check_detail_safety(res, pname, case, b, "after recovery")

    owners = {}
    for t in case["meta"]["op_types"]:
        owners.setdefault(TENANT + t[5], set()).add(tuple(t[:4]))
    state = {k: {"status": r["status"], "consumed": next((x for x in ("applied", "failed")
                                                          if a["consumed"].get(f"{k}/{x}")), None)}
             for k, r in a["receipts"].items()}
    retries = {i: k for i, k in case["meta"]["retries"]}
    refusals = sum(n for k, n in observed.items() if k not in m1.NON_REFUSAL)
    for i in range(gi + 2, len(steps) - 1):
        op, r = steps[i], got[i]
        if isinstance(r, dict) and r.get("result") == "refused":
            refusals += 1
        if op["op"] == "restart_broker":
            ok = r == {"result": "restarted"}
            res.check("broker.m3.fault_recovery_restart", pname, ok, cid, {"step": i, "got": r},
                      case=None if ok else case)
        elif op["op"] == "reconcile":
            pending = [k for k, s in state.items() if s["status"] == "dispatched"]
            want = {"count": len(pending), "result": "reconciled"}
            ok = r == want
            res.check("broker.m3.fault_reconcile_resolves_only_from_evidence", pname, ok, cid,
                      {"step": i, "got": r, "want": want}, case=None if ok else case)
            for k in pending:
                s = state[k]
                s["status"] = {"applied": "observed_success", "failed": "observed_failure"}.get(s["consumed"],
                                                                                              "unknown_reconcile")
        elif op["op"] == "execute":
            t = retries[i]
            key = TENANT + t[4]
            s = state.get(key)
            cls = m1.VARIANTS[t[0]][2]
            shared = owners.get(key, set()) != {tuple(t[:4])}
            conflict = {"IDEMPOTENCY_CONFLICT"} if shared else set()
            code = r.get("code") if isinstance(r, dict) and r.get("result") == "refused" else None
            if s is None:
                ok = r == {"result": "effect_completed"} or (
                    cls == "require_approval" and code in {"GRANT_UNKNOWN", "GRANT_MISMATCH", "GRANT_EXHAUSTED"})
                inv = "broker.m3.fault_recovery_retry_answer"
            elif s["status"] == "reserved":
                ok = r == {"result": "effect_completed"} or code in conflict
                inv = "broker.m3.fault_recovery_retry_answer"
            elif s["status"] in ("dispatched", "unknown_reconcile"):
                ok = code in {"RECONCILIATION_REQUIRED"} | conflict
                inv = "broker.m3.fault_uncertain_receipt_never_redispatched"
            else:
                ok = r == REPLAY["completed" if s["status"] == "observed_success" else "failed"] or code in conflict
                inv = "broker.m3.fault_recovery_retry_answer"
            res.check(inv, pname, ok, cid, {"step": i, "got": r, "state": s, "shared_key": shared},
                      case=None if ok else case)
            if r == {"result": "effect_completed"}:
                state[key] = {"status": "observed_success", "consumed": "applied"}
    want = {k: s["status"] for k, s in state.items()}
    have = {k: r["status"] for k, r in b["receipts"].items()}
    want_c = {f"{k}/{s['consumed']}": 1 for k, s in state.items() if s["consumed"]}
    want_e = {k: 1 for k, s in state.items() if s["consumed"] == "applied"}
    ok = have == want and b["consumed"] == want_c and b["effects"] == want_e
    res.check("broker.m3.fault_recovery_final_state", pname, ok, cid,
              {"receipts": have, "want": want, "consumed": b["consumed"], "want_consumed": want_c},
              case=None if ok else case)
    ok = b["refusals"] == refusals
    res.check("broker.refusals_counted_exactly", pname, ok, cid, {"final": b["refusals"], "counted": refusals},
              case=None if ok else case)
    return ",".join(f"{k}={v}" for k, v in sorted(observed.items()))


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
    generated += [gen_outage_restart(rng, i) for i in range(1, max(20, n // 16) + 1)]
    base, pruned = prune(proto, generated, res)
    cases = []
    for c in base:
        cases.append(c)
        if c["meta"].pop("simple"):
            cases.append(m1.insert_twin(rng, c, "restart"))
            cases.append(insert_refusal(rng, c))
        twin = insert_c4_inspection(c)
        if twin is not None:
            cases.append(twin)
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
        elif c["meta"]["family"] == "inserted_c4_inspection":
            check_c4_inspection(res, proto.name, c, out.get(c["id"]), out.get(c["meta"]["twin_of"]))
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
    fstats = run_fault_groups(proto, rng, max(FAULT_GROUPS_MIN, n // FAULT_GROUPS_PER), res, repeats)
    stats.update({
        "probes": len(probe_cases), "sequences": len(base), "twins": len(cases) - len(base),
        "concurrent_groups": len(conc), "concurrent_sizes": dict(Counter(c["meta"]["n"] for c in conc)),
        "repeats": repeats,
        "excluded_steps": {
            "apply_without_permit": sum(c["meta"]["excluded"]["apply_without_permit"] for c in base),
            "present_permit_of_non_execute": sum(c["meta"]["excluded"]["present_permit_of_non_execute"]
                                                 for c in base),
            "present_permit_of_execute_without_permit": pruned,
            "restart_while_ledger_down": 0},
        "restarts_under_outage_kept": sum(c["meta"].get("restart_under_outage", 0) for c in base),
        "c4_inspection_twins": sum(1 for c in cases if c["meta"]["family"] == "inserted_c4_inspection"),
        "c4_expected_codes_by_stage": dict(sorted(getattr(res, "c4_stage_tally", Counter()).items())),
        "operations": dict(Counter(s.get("op") for c in cases for s in c["input"]["steps"])),
        "concurrency_distribution": {k: dict(v) for k, v in dist.items() if not k.startswith("brc-")},
        "concurrency_groups_with_multiple_outcomes": sum(1 for k, v in dist.items()
                                                         if k.startswith("brc-") and len(v) > 1),
        **fstats,
    })
    return stats


FAULT_GROUPS_MIN = 6
FAULT_GROUPS_PER = 40
FAULT_GROUP_SIZES = (16, 32, 64)


def run_fault_groups(proto: Proto, rng, count: int, res: Results, repeats: int) -> dict:
    groups = [gen_fault_group(rng, i, rng.choice(FAULT_GROUP_SIZES)) for i in range(1, count + 1)]
    with_lu = without = 0
    faults = Counter(t[4] or "none" for c in groups for t in c["meta"]["op_types"])
    for _ in range(repeats):
        out = run_cases(proto, groups, res)
        for c in groups:
            sig = check_fault_group(res, proto.name, c, out.get(c["id"]))
            if sig is not None:
                if "LEDGER_UNAVAILABLE" in sig:
                    with_lu += 1
                else:
                    without += 1
    return {"fault_groups": len(groups), "fault_group_sizes": dict(Counter(c["meta"]["n"] for c in groups)),
            "fault_group_operations_by_fault": dict(sorted(faults.items())),
            "fault_group_runs_with_storage_refusals": with_lu, "fault_group_runs_without": without,
            "fault_group_retries": sum(len(c["meta"]["retries"]) for c in groups)}
