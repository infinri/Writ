"""Campaign `broker`: sealed ingress, approval grants, permits, idempotency,
faults and reconciliation (`broker-scenario`, BROKER.md, D-008..D-012, D-020).

Seals are HMAC-SHA256 under the scenario's fixture ingress key over the D-006
canonical bytes of the request (BROKER.md "Seal").

Oracles:
- per-operation codes that do not depend on history: unsealed, forged,
  foreign-adapter or altered-after-sealing requests are INGRESS_UNAUTHENTICATED;
  wrong credential or untrusted channel is HUMAN_UNAUTHENTICATED; a direct
  adapter call is PERMIT_MISSING; a deny-class request is refused with its
  decision code; require_approval without a grant is APPROVAL_REQUIRED (with the
  ledger and policy up); presenting the permit of a step that issued none is
  PERMIT_MISSING; presenting an issued permit is PERMIT_REQUEST_MISMATCH,
  PERMIT_INVALID, PERMIT_EXPIRED or PERMIT_REPLAYED in the adapter's order
  (dispatch already consumed it);
- run invariants: grant uses never exceed max_uses (1) and equal the number of
  first reservations made under that grant (refusals never consume); effects
  applied equal dispatches and never exceed consumed permits; receipts equal
  distinct reserved keys; refusals counted exactly; a key whose effect became
  uncertain is never dispatched again;
- metamorphic: inserting restart_broker anywhere changes no other step and not
  the final counters (D-020); inserting a guaranteed refusal changes nothing but
  refusals + 1;
- concurrency (16/32/64 operations with shared grants and shared or
  conflicting idempotency keys, several principals): the observed outcome counts
  and final counters must be produced by at least one serial order of the same
  operations under a small abstract model of execute steps 5-9 (searched
  exhaustively with memoization, not sampled).
"""
from __future__ import annotations

import json
from collections import Counter
from pathlib import Path

from .common import Results, clone, differential, digest, run_both, run_probes, seal

NAME = "broker"
FIX = Path(__file__).resolve().parents[2] / "fixtures"
FIX_KEY = "1" * 64
OTHER_KEY = "2" * 64
T0 = 1793966400  # 2026-11-06T12:00:00Z
NON_REFUSAL = {"effect_completed", "effect_failed", "crashed", "replayed", "grant_issued", "effect_applied",
               "reconciled", "restarted", "bundle_activated", "time_advanced", "dependency_set"}


def ts(sec):
    import time as _t
    return _t.strftime("%Y-%m-%dT%H:%M:%SZ", _t.gmtime(sec))


def ref_bundle():
    return json.loads((FIX / "sets" / "golden-decisions.jsonl").read_text().splitlines()[0])["input"]["bundle"]


REF = ref_bundle()
ALT = clone(REF)
ALT["statements"].append({"statement_id": "zz-unrelated", "effect": "permit", "action_kinds": ["memory.write"],
                          "reason_code": "UNRELATED_PERMIT", "resource_prefixes": ["memory:other-tenant/x/"]})
ALT["version"] = "0.1.1"

PRINCIPALS = [("agent:claude-session-1", "session-1"), ("agent:claude-session-2", "session-2"), ("agent:subagent-1", "session-3")]
# variant name -> (kind, resource suffix, decision class, deny code)
VARIANTS = {
    "W1": ("file.write", "workspace:fixture-tenant/fixture-project/src/app.py", "allow", None),
    "W2": ("file.write", "workspace:fixture-tenant/fixture-project/src/other.py", "allow", None),
    "R1": ("file.read", "workspace:fixture-tenant/fixture-project/README.md", "allow", None),
    "P1": ("repository.publish", "repository:fixture-tenant/fixture-project/main", "require_approval", None),
    "P2": ("repository.publish", "repository:fixture-tenant/fixture-project/other", "require_approval", None),
    "X1": ("process.execute", "workspace:fixture-tenant/fixture-project/bin/run", "require_approval", None),
    "D1": ("file.write", "workspace:fixture-tenant/fixture-project/secrets/key.txt", "deny", "SECRETS_PATH_FORBIDDEN"),
    "E1": ("network.egress", "network:fixture-tenant/fixture-project/example.org", "deny", "NETWORK_EGRESS_FORBIDDEN"),
    "U1": ("shell.raw", "workspace:fixture-tenant/fixture-project/src/app.py", "deny", "UNKNOWN_CAPABILITY"),
}


def request(variant, key, principal=0, trace=""):
    kind, res, _, _ = VARIANTS[variant]
    pid, sess = PRINCIPALS[principal]
    return {"action": {"action_digest": "sha256:" + "a" * 64, "attributes": {}, "kind": kind,
                       "requested_capabilities": [], "resource": {"canonical_resource_id": res, "uri": "x://" + variant}},
            "agent_id": None, "contract_version": "2.0", "deadline_at": None, "expected_state_revision": None,
            "idempotency_key": key, "parent_session_id": None, "policy_bundle_sha": None,
            "principal": {"authn_method": "os_peer_credential", "id": pid, "roles": ["agent"],
                          "verified_by": "writ-host-adapter:fixture"},
            "project_id": "fixture-project", "request_id": "req-1", "session_id": sess, "tenant_id": "fixture-tenant",
            "trace_id": "trace-1" + trace}


def initial(now=T0):
    return {"bundle": clone(REF),
            "channels": {"approval-ui": {"agent:claude-session-1": "cred-agent", "human:alice": "cred-alice",
                                         "human:bob": "cred-bob"}},
            "dependencies": {"ledger": "up", "policy": "up"},
            "ingress_keys": {"writ-host-adapter:fixture": FIX_KEY, "writ-host-adapter:other": OTHER_KEY},
            "now": ts(now), "permit_key": "4" * 64, "state_revision": 5}


def execute(req, grant=None, fault=None, seal_kind="valid", rng=None):
    if seal_kind == "valid":
        s = seal(req, "writ-host-adapter:fixture", FIX_KEY)
    elif seal_kind == "absent":
        s = None
    elif seal_kind == "forged":
        s = {"adapter_id": "writ-host-adapter:fixture", "mac": "0" * 64}
    elif seal_kind == "other_adapter":
        s = seal(req, "writ-host-adapter:other", OTHER_KEY)
    elif seal_kind == "unregistered":
        s = seal(req, "writ-host-adapter:rogue", "3" * 64)
    else:  # altered after sealing
        s = seal(req, "writ-host-adapter:fixture", FIX_KEY)
        req = clone(req)
        req["trace_id"] += "-altered"
    op = {"op": "execute", "request": req, "seal": s}
    if grant is not None:
        op["grant_ref"] = grant
    if fault:
        op["fault"] = fault
    return op


def approve(req, grant, human="human:alice", cred=None, channel="approval-ui", verdict="approved", ttl=None):
    creds = {"human:alice": "cred-alice", "human:bob": "cred-bob", "agent:claude-session-1": "cred-agent"}
    op = {"op": "approve", "for_request": req, "channel": channel, "human": human,
          "credential": cred if cred is not None else creds.get(human, "guess"), "verdict": verdict, "grant_ref": grant}
    if ttl is not None:
        op["ttl_seconds"] = ttl
    return op


# ------------------------------------------------------------- sequences

def gen_sequence(rng, idx, allow_deps=True, allow_conc=True):
    now = T0
    steps, info = [], []  # info: per step dict for op-level oracles
    deps = {"ledger": "up", "policy": "up"}
    grants = {}  # label -> (variant, principal)
    for _ in range(rng.randint(3, 14)):
        r = rng.random()
        v = rng.choice(list(VARIANTS))
        pr = rng.choice([0, 0, 1, 2])
        key = rng.choice(["k1", "k2", "k3"])
        if r < 0.22:
            g = rng.choice(["g1", "g2", "g3"])
            mode = rng.choices(["ok", "wrong_cred", "bad_channel", "self", "denied", "bob"], [6, 1, 1, 1, 1, 2])[0]
            req = request(v, key, pr)
            if mode == "ok":
                op = approve(req, g)
            elif mode == "bob":
                op = approve(req, g, human="human:bob", ttl=rng.choice([None, 5, 60, 3600]))
            elif mode == "wrong_cred":
                op = approve(req, g, cred="guess")
            elif mode == "bad_channel":
                op = approve(req, g, channel="chat")
            elif mode == "self":
                op = approve(req, g, human="agent:claude-session-1")
            else:
                op = approve(req, g, verdict="denied")
            steps.append(op)
            info.append({"kind": "approve", "mode": mode, "variant": v})
        elif r < 0.62 and steps and rng.random() < 0.45:
            # retry an earlier execute, or execute what an earlier approval approved
            prev = [(st, inf) for st, inf in zip(steps, info) if inf["kind"] in ("execute", "approve")]
            if not prev:
                continue
            st, inf = rng.choice(prev)
            if inf["kind"] == "execute":
                req, g, v = st["request"], st.get("grant_ref"), inf["variant"]
                seal_kind = "valid"
            else:
                req, g, v = clone(st["for_request"]), st["grant_ref"], inf["variant"]
                req["idempotency_key"] = rng.choice(["k1", "k2", "k3"])
                seal_kind = "valid"
            fault = rng.choices([None, "crash_after_reserve", "crash_after_dispatch", "effect_fails"], [6, 1, 1, 1])[0]
            steps.append(execute(req, g, fault, seal_kind))
            info.append({"kind": "execute", "variant": v, "seal": seal_kind, "deps": dict(deps), "grant": g,
                         "fault": fault, "now": now})
        elif r < 0.62:
            g = rng.choice([None, "g1", "g2", "g3", "g-forged"]) if VARIANTS[v][2] != "allow" or rng.random() < 0.2 else None
            seal_kind = rng.choices(["valid", "absent", "forged", "other_adapter", "unregistered", "altered"],
                                    [14, 1, 1, 1, 1, 1])[0]
            fault = rng.choices([None, "crash_before_reserve", "crash_after_reserve", "crash_after_dispatch", "effect_fails"],
                                [10, 1, 1, 1, 1])[0]
            req = request(v, key, pr, trace=rng.choice(["", "", "", "-b"]))
            steps.append(execute(req, g, fault, seal_kind))
            info.append({"kind": "execute", "variant": v, "seal": seal_kind, "deps": dict(deps), "grant": g,
                         "fault": fault, "now": now})
        elif r < 0.72 and steps:
            j = rng.randrange(len(steps))
            src = steps[j].get("request") or steps[j].get("for_request") or request(v, key, pr)
            same = rng.random() < 0.7
            req = src if same else request(v, key, pr, trace="-other")
            op = {"op": "present_permit", "of_step": j, "request": req}
            tamper = rng.choice([None, None, None, "mac", "expiry"])
            if tamper:
                op["tamper"] = tamper
            steps.append(op)
            info.append({"kind": "present", "of": j, "same": same and src is steps[j].get("request"), "tamper": tamper,
                         "now": now})
        elif r < 0.75:
            steps.append({"op": "apply_without_permit", "request": request(v, key, pr)})
            info.append({"kind": "apply_without_permit"})
        elif r < 0.8:
            steps.append({"op": "reconcile"})
            info.append({"kind": "reconcile"})
        elif r < 0.85:
            steps.append({"op": "restart_broker"})
            info.append({"kind": "restart"})
        elif r < 0.89:
            now += rng.choice([1, 10, 31, 300, 601, 3600])
            steps.append({"op": "advance_time", "now": ts(now)})
            info.append({"kind": "time"})
        elif r < 0.92:
            steps.append({"op": "activate_bundle", "bundle": clone(rng.choice([REF, ALT]))})
            info.append({"kind": "bundle"})
        elif r < 0.95 and allow_deps:
            name = rng.choice(["ledger", "policy"])
            status = "up" if deps[name] == "down" or rng.random() < 0.3 else "down"
            deps[name] = status
            steps.append({"op": "set_dependency", "name": name, "status": status})
            info.append({"kind": "deps"})
        elif allow_conc:
            ops = []
            for _ in range(rng.randint(2, 6)):
                vv = rng.choice(["W1", "W2", "P1", "P2"])
                gg = rng.choice(["g1", "g2"]) if VARIANTS[vv][2] == "require_approval" else None
                ops.append(execute(request(vv, rng.choice(["k1", "k2", "c1"]), 0), gg))
            steps.append({"op": "concurrent", "operations": ops})
            info.append({"kind": "concurrent"})
    return {"id": f"brs-{idx:06d}", "command": "broker-scenario", "input": {"initial": initial(), "steps": steps},
            "meta": {"family": "sequence", "info": info}}


def check_ops(res, pname, case, out):
    """Per-operation oracles whose result does not depend on hidden history."""
    steps = case["input"]["steps"]
    info = case["meta"]["info"]
    got = out["steps"]
    for i, (op, inf, r) in enumerate(zip(steps, info, got)):
        want = None
        if inf["kind"] == "execute":
            if inf["seal"] != "valid":
                want = {"result": "refused", "code": "INGRESS_UNAUTHENTICATED"}
            elif inf["deps"]["ledger"] == "down":
                want = {"result": "refused", "code": "LEDGER_UNAVAILABLE"}
            elif inf["deps"]["policy"] == "down":
                want = {"result": "refused", "code": "POLICY_UNAVAILABLE"}
            elif VARIANTS[inf["variant"]][2] == "deny":
                want = {"result": "refused", "code": VARIANTS[inf["variant"]][3]}
            elif VARIANTS[inf["variant"]][2] == "require_approval" and inf["grant"] is None:
                want = {"result": "refused", "code": "APPROVAL_REQUIRED"}
        elif inf["kind"] == "approve" and inf["mode"] in ("wrong_cred", "bad_channel"):
            want = {"result": "refused", "code": "HUMAN_UNAUTHENTICATED"}
        elif inf["kind"] == "apply_without_permit":
            want = {"result": "refused", "code": "PERMIT_MISSING"}
        elif inf["kind"] == "present":
            j = inf["of"]
            src, srcr, srci = steps[j], got[j], info[j]
            issued = srci["kind"] == "execute" and isinstance(srcr, dict) and not srcr.get("replayed") and (
                srcr.get("result") in ("effect_completed", "effect_failed") or
                (srcr.get("result") == "crashed" and src.get("fault") == "crash_after_dispatch"))
            if srci["kind"] == "execute" and isinstance(srcr, dict) and srcr.get("result") == "crashed" and \
                    src.get("fault") == "crash_after_reserve":
                issued = False
            if srci["kind"] == "concurrent":
                want = None  # which permit a concurrent step "issued" is not specified (several or none)
            elif not issued:
                want = {"result": "refused", "code": "PERMIT_MISSING"}
            else:
                same_req = op["request"] == src["request"]
                if not same_req:
                    want = {"result": "refused", "code": "PERMIT_REQUEST_MISMATCH"}
                elif inf["tamper"]:
                    want = {"result": "refused", "code": "PERMIT_INVALID"}
                elif inf["now"] == srci["now"] + 30:
                    want = None  # exact expiry instant: the spec does not say whether expiry is inclusive
                elif inf["now"] > srci["now"] + 30:
                    want = {"result": "refused", "code": "PERMIT_EXPIRED"}
                else:
                    want = {"result": "refused", "code": "PERMIT_REPLAYED"}
        if want is not None:
            ok = r == want
            res.check(f"broker.op.{inf['kind']}", pname, ok, case["id"], {"step": i, "got": r, "want": want},
                      case=None if ok else case)


def check_run(res, pname, case, out):
    cid = case["id"]
    if not isinstance(out, dict) or not isinstance(out.get("steps"), list) or \
            len(out["steps"]) != len(case["input"]["steps"]):
        res.check("broker.output_shape", pname, False, cid, out, case=case)
        return
    fin = out.get("final") or {}
    steps = case["input"]["steps"]
    refusals = 0
    for r in out["steps"]:
        if not isinstance(r, dict):
            continue
        if r.get("result") == "refused":
            refusals += 1
        if r.get("result") == "concurrent":
            refusals += sum(n for k, n in (r.get("outcomes") or {}).items() if k not in NON_REFUSAL)
    ok = fin.get("refusals") == refusals
    res.check("broker.refusals_counted_exactly", pname, ok, cid, {"final": fin.get("refusals"), "counted": refusals},
              case=None if ok else case)
    gu = fin.get("grant_uses") or {}
    ok = all(isinstance(n, int) and 0 <= n <= 1 for n in gu.values())
    res.check("broker.grant_uses_never_exceed_max_uses", pname, ok, cid, gu, case=None if ok else case)
    ok = isinstance(fin.get("effects_applied"), int) and fin.get("effects_applied", 0) <= fin.get("consumed_permits", -1)
    res.check("broker.effects_applied_le_consumed_permits", pname, ok, cid, fin, case=None if ok else case)
    if any(s.get("op") == "concurrent" for s in steps):
        return  # sequential accounting below needs a known order
    reserved, uncertain = set(), set()
    consumed_by = Counter()
    dispatches = applied = 0
    redispatch = False
    for op, r in zip(steps, out["steps"]):
        if op.get("op") == "approve" and r == {"result": "grant_issued"}:
            # re-approval under an existing label replaces that grant (observed in both; spec silent)
            consumed_by.pop(op["grant_ref"], None)
        if op.get("op") != "execute" or not isinstance(r, dict):
            continue
        key = op["request"]["idempotency_key"]
        res_name, replayed = r.get("result"), r.get("replayed")
        reserved_now = (res_name in ("effect_completed", "effect_failed") and not replayed) or \
            (res_name == "crashed" and op.get("fault") in ("crash_after_reserve", "crash_after_dispatch"))
        dispatched = (res_name in ("effect_completed", "effect_failed") and not replayed) or \
            (res_name == "crashed" and op.get("fault") == "crash_after_dispatch")
        if dispatched:
            if key in uncertain:
                redispatch = True
            dispatches += 1
            if res_name != "effect_failed":
                applied += 1
        if res_name == "crashed" and op.get("fault") == "crash_after_dispatch":
            uncertain.add(key)
        if reserved_now and key not in reserved:
            reserved.add(key)
            if op.get("grant_ref") and VARIANTS_BY_KIND.get(op["request"]["action"]["kind"]) == "require_approval":
                consumed_by[op["grant_ref"]] += 1
    res.check("broker.uncertain_effect_never_redispatched", pname, not redispatch, cid, out,
              case=case if redispatch else None)
    ok = fin.get("effects_applied") == applied
    res.check("broker.effects_applied_equal_dispatches", pname, ok, cid, {"final": fin.get("effects_applied"), "counted": applied},
              case=None if ok else case)
    ok = fin.get("consumed_permits") == dispatches
    res.check("broker.consumed_permits_equal_dispatches", pname, ok, cid,
              {"final": fin.get("consumed_permits"), "counted": dispatches}, case=None if ok else case)
    ok = sum((fin.get("receipts") or {}).values()) == len(reserved)
    res.check("broker.receipts_equal_distinct_reserved_keys", pname, ok, cid,
              {"final": fin.get("receipts"), "reserved": sorted(reserved)}, case=None if ok else case)
    ok = all(gu.get(g, 0) == n for g, n in consumed_by.items()) and all(n == consumed_by.get(g, 0) for g, n in gu.items())
    res.check("broker.grant_uses_equal_first_reservations_refusals_consume_nothing", pname, ok, cid,
              {"final": gu, "counted": dict(consumed_by)}, case=None if ok else case)


VARIANTS_BY_KIND = {"file.write": "allow", "file.read": "allow", "repository.publish": "require_approval",
                    "process.execute": "require_approval"}


def insert_twin(rng, case, kind):
    c = clone(case)
    steps = c["input"]["steps"]
    pos = rng.randint(0, len(steps))
    if kind == "restart":
        ins = {"op": "restart_broker"}
    else:
        ins = rng.choice([execute(request("W1", "fresh-" + str(pos)), seal_kind="absent"),
                          approve(request("P1", "k1"), "g9", cred="guess"),
                          {"op": "apply_without_permit", "request": request("W1", "k1")}])
    steps.insert(pos, ins)
    for s in steps:
        if s.get("op") == "present_permit" and s is not ins and s["of_step"] >= pos:
            s["of_step"] += 1
    c["id"] = case["id"] + f"-{kind}{pos}"
    c["meta"] = {"family": f"inserted_{kind}", "twin_of": case["id"], "pos": pos}
    return c


# ------------------------------------------------------------ concurrency

def gen_concurrent(rng, idx, n):
    """Approvals first, then one concurrent group of n executes."""
    pool_variants = rng.sample(["W1", "W2", "P1", "P2", "X1"], rng.randint(2, 4))
    keys = rng.choice([["k1"], ["k1", "k2"], ["k1", "k2", "k3", "k4"], [f"u{i}" for i in range(n)]])
    principals = rng.choice([[0], [0, 1], [0, 1, 2]])
    steps, grants = [], {}
    for v in pool_variants:
        if VARIANTS[v][2] == "require_approval":
            for p in principals:
                g = f"g-{v}-{p}"
                grants[(v, p)] = g
                steps.append(approve(request(v, "k1", p), g))
    types = []
    for _ in range(rng.randint(1, 3)):
        v = rng.choice(pool_variants)
        p = rng.choice(principals)
        trace = rng.choice(["", "", "-b"])
        g = None
        if VARIANTS[v][2] == "require_approval":
            g = grants[(v, p)] if rng.random() < 0.8 else rng.choice(list(grants.values()) + ["g-forged"])
        fault = rng.choices([None, "effect_fails", "crash_after_dispatch"], [8, 1, 1])[0]
        types.append((v, p, trace, g, fault))
    ops, op_types = [], []
    for i in range(n):
        v, p, trace, g, fault = rng.choice(types)
        key = keys[i] if len(keys) == n else rng.choice(keys)
        ops.append(execute(request(v, key, p, trace), g, fault))
        op_types.append([v, p, trace, g, fault, key])
    steps.append({"op": "concurrent", "operations": ops})
    return {"id": f"brc-{idx:06d}-x{n}", "command": "broker-scenario", "input": {"initial": initial(), "steps": steps},
            "meta": {"family": "concurrent", "n": n, "op_types": op_types, "grants": {f"{k[0]}|{k[1]}": g for k, g in grants.items()}}}


def model_step(state, t, fps):
    """Abstract serial semantics of BROKER.md execute steps 4-9 for a sealed request
    whose kernel decision class is fixed (allow or require_approval), ledger and
    policy up, grants issued with max_uses 1 and unexpired. key None = a key used by
    this operation only (its receipt is counted, never looked up again)."""
    v, p, trace, g, fault, key = t
    receipts, uniq, uses, eff, perm = state
    cls = VARIANTS[v][2]
    if cls == "require_approval" and g is None:
        return state, "APPROVAL_REQUIRED"
    fp = (v, p, trace, key)
    egrant = g if cls == "require_approval" else None
    rec = dict(receipts)
    if key is not None and key in rec:
        rfp, rg, status = rec[key]
        if rfp != fp or rg != egrant:
            return state, "IDEMPOTENCY_CONFLICT"
        if status in ("observed_success", "observed_failure"):
            return state, "replayed"
        return state, "RECONCILIATION_REQUIRED"  # dispatched (no reserved-only states without crash_after_reserve)
    uses = dict(uses)
    if cls == "require_approval":
        bound = BOUND_GRANTS.get(g)
        if bound is None:
            return state, "GRANT_UNKNOWN"
        if bound != (v, p):
            return state, "GRANT_MISMATCH"
        if uses.get(g, 0) >= 1:
            return state, "GRANT_EXHAUSTED"
        uses[g] = uses.get(g, 0) + 1
    perm += 1
    if fault == "crash_after_dispatch":
        status, out, eff = "dispatched", "crashed", eff + 1
    elif fault == "effect_fails":
        status, out = "observed_failure", "effect_failed"
    else:
        status, out, eff = "observed_success", "effect_completed", eff + 1
    u = dict(uniq)
    if key is None:
        u[status] = u.get(status, 0) + 1
    else:
        rec[key] = (fp, egrant, status)
    return (tuple(sorted(rec.items())), tuple(sorted(u.items())), tuple(sorted(uses.items())), eff, perm), out


BOUND_GRANTS = {}


def serializable(case, observed: dict, final: dict) -> bool:
    """Is there a serial order of the group's operations producing exactly the observed
    outcome counts and final counters? Exhaustive DFS over operation types, memoized."""
    BOUND_GRANTS.clear()
    for k, g in case["meta"]["grants"].items():
        v, p = k.split("|")
        BOUND_GRANTS[g] = (v, int(p))
    keycount = Counter(t[5] for t in case["meta"]["op_types"])
    types = Counter(tuple(t[:5]) + ((t[5] if keycount[t[5]] > 1 else None),) for t in case["meta"]["op_types"])
    tlist = sorted(types, key=repr)
    obs = {k: v for k, v in observed.items() if v}
    total = sum(types.values())
    if sum(obs.values()) != total:
        return False
    n_t = len(tlist)
    seen = set()
    budget = [300000]

    def final_ok(state):
        receipts, uniq, uses, eff, perm = state
        fin_receipts = Counter(s for _, (_, _, s) in receipts)
        fin_receipts.update(dict(uniq))
        g_all = {g: 0 for g in BOUND_GRANTS}
        g_all.update(dict(uses))
        want = {"effects_applied": eff, "consumed_permits": perm, "receipts": dict(fin_receipts), "grant_uses": g_all}
        return all(final.get(k) == v for k, v in want.items())

    def assign(remaining, options, chain_counts):
        """Can the non-chain operations be placed at states where they do not change
        state so that outcome counts become exactly the observed ones?"""
        need = Counter(obs)
        need.subtract(chain_counts)
        if any(v < 0 for v in need.values()):
            return False
        keys = sorted(need)
        start = tuple(need[k] for k in keys)
        frontier = {start}
        for i in range(n_t):
            m = remaining[i]
            if m == 0:
                continue
            outs = sorted(options.get(i, ()))
            if not outs:
                return False
            nxt = set()
            for vec in frontier:
                # distribute m operations over the allowed outcomes
                def rec(j, left, v):
                    if left == 0:
                        nxt.add(v)
                        return
                    if j == len(outs) - 1:
                        if outs[j] not in keys:
                            return
                        idx = keys.index(outs[j])
                        if v[idx] >= left:
                            w = list(v)
                            w[idx] -= left
                            nxt.add(tuple(w))
                        return
                    idx = keys.index(outs[j]) if outs[j] in keys else None
                    for take in range(0, left + 1):
                        if take and (idx is None or v[idx] < take):
                            break
                        w = list(v)
                        if take:
                            w[idx] -= take
                        rec(j + 1, left - take, tuple(w))
                rec(0, m, vec)
            frontier = nxt
            if not frontier:
                return False
        return any(not any(v) for v in frontier)

    def dfs(remaining, state, options, chain_counts):
        # options: {type index: set of outcomes it yields without changing state in a visited state}
        opts = {i: set(o) for i, o in options.items()}
        changing = []
        for i, t in enumerate(tlist):
            if remaining[i] == 0:
                continue
            ns, out = model_step(state, t, None)
            if ns == state:
                opts.setdefault(i, set()).add(out)
            else:
                changing.append((i, ns, out))
        memo = (remaining, state, tuple(sorted((i, tuple(sorted(o))) for i, o in opts.items())),
                tuple(sorted(chain_counts.items())))
        if memo in seen:
            return False
        seen.add(memo)
        budget[0] -= 1
        if budget[0] <= 0:
            raise TimeoutError
        if final_ok(state) and assign(remaining, opts, chain_counts):
            return True
        for i, ns, out in changing:
            r2 = list(remaining)
            r2[i] -= 1
            c2 = dict(chain_counts)
            c2[out] = c2.get(out, 0) + 1
            if c2[out] > obs.get(out, 0):
                continue
            if dfs(tuple(r2), ns, opts, c2):
                return True
        return False

    import sys
    sys.setrecursionlimit(10000)
    try:
        return dfs(tuple(types[t] for t in tlist), ((), (), (), 0, 0), {}, {})
    except TimeoutError:
        return None  # search budget exhausted: undetermined, reported separately


# --------------------------------------------------------------------- run

def probes():
    """Inputs whose result the spec does not define: differential only."""
    w = request("W1", "k1")
    bad = [("unknown_op", [{"op": "zz_unknown_operation"}]),
           ("unknown_fault", [execute(w, fault="zz_fault")]),
           ("execute_without_request", [{"op": "execute", "seal": None}]),
           ("present_bad_index", [{"op": "present_permit", "of_step": 7, "request": w}]),
           ("present_negative_index", [{"op": "present_permit", "of_step": -1, "request": w}]),
           ("unknown_dependency", [{"op": "set_dependency", "name": "graph", "status": "down"}]),
           ("bad_dependency_status", [{"op": "set_dependency", "name": "ledger", "status": "sideways"}]),
           ("time_backwards", [execute(w), {"op": "advance_time", "now": "2020-01-01T00:00:00Z"},
                               {"op": "present_permit", "of_step": 0, "request": w}]),
           ("approve_zero_ttl", [approve(request("P1", "k1"), "g1", ttl=0), execute(request("P1", "k1"), "g1")]),
           ("approve_negative_ttl", [approve(request("P1", "k1"), "g1", ttl=-5), execute(request("P1", "k1"), "g1")]),
           ("grant_label_reused", [approve(request("P1", "k1"), "g1"), approve(request("P2", "k1"), "g1"),
                                   execute(request("P1", "k1"), "g1")]),
           ("approve_bad_verdict", [approve(request("P1", "k1"), "g1", verdict="maybe"), execute(request("P1", "k1"), "g1")]),
           ("concurrent_nested", [{"op": "concurrent", "operations": [{"op": "concurrent", "operations": [execute(w)]}]}]),
           ("concurrent_empty", [{"op": "concurrent", "operations": []}]),
           ("empty_steps", [])]
    out = []
    for name, steps in bad:
        out.append({"id": f"brp-{name}", "command": "broker-scenario", "input": {"initial": initial(), "steps": steps},
                    "meta": {"family": "probe"}})
    out.append({"id": "brp-empty-initial", "command": "broker-scenario",
                "input": {"initial": {}, "steps": [execute(w)]}, "meta": {"family": "probe"}})
    out.append({"id": "brp-no-steps-member", "command": "broker-scenario", "input": {"initial": initial()},
                "meta": {"family": "probe"}})
    return out


def gen_cases(rng, n):
    cases, i = [], 0
    while len(cases) < n:
        i += 1
        simple = rng.random() < 0.5
        c = gen_sequence(rng, i, allow_deps=not simple, allow_conc=not simple and rng.random() < 0.5)
        cases.append(c)
        if simple:
            cases.append(insert_twin(rng, c, "restart"))
            cases.append(insert_twin(rng, c, "refusal"))
    return cases


def run(protos, rng, n, res: Results, repeats: int = 5):
    probe_cases = probes()
    run_probes(protos, probe_cases, res, "broker")
    cases = gen_cases(rng, n)
    outs = run_both(protos, "broker-scenario", cases, res)
    by_id = {c["id"]: c for c in cases}
    for p in protos:
        o = outs[p.name]
        for c in cases:
            out = o.get(c["id"])
            if c["meta"]["family"] == "probe":
                continue
            check_run(res, p.name, c, out)
            if c["meta"]["family"] == "sequence" and isinstance(out, dict) and isinstance(out.get("steps"), list) \
                    and len(out["steps"]) == len(c["input"]["steps"]):
                check_ops(res, p.name, c, out)
            if c["meta"]["family"].startswith("inserted_"):
                base = o.get(c["meta"]["twin_of"])
                pos = c["meta"]["pos"]
                ok = isinstance(out, dict) and isinstance(base, dict) and "steps" in out and "steps" in base
                if ok:
                    s2 = list(out["steps"])
                    ins = s2.pop(pos)
                    f2 = dict(out["final"])
                    if c["meta"]["family"] == "inserted_refusal":
                        f2["refusals"] = f2.get("refusals", 0) - 1
                        ok = ins.get("result") == "refused"
                    else:
                        ok = ins == {"result": "restarted"}
                    ok = ok and s2 == base["steps"] and f2 == base["final"]
                inv = "broker.restart_is_transparent" if c["meta"]["family"] == "inserted_restart" else \
                    "broker.inserted_refusal_changes_nothing_else"
                res.check(inv, p.name, ok, c["id"], {"with": out, "without": base}, case=None if ok else c)
    has_conc = lambda c: any(s.get("op") == "concurrent" for s in c["input"]["steps"])  # noqa: E731
    differential(protos, outs, cases, res, skip=has_conc)
    if len(protos) == 2:
        a, b = protos[0].name, protos[1].name
        for c in filter(has_conc, cases):
            first = next(i for i, s in enumerate(c["input"]["steps"]) if s.get("op") == "concurrent")
            oa, ob = outs[a].get(c["id"]), outs[b].get(c["id"])
            pa = oa.get("steps", [])[:first] if isinstance(oa, dict) else oa
            pb = ob.get("steps", [])[:first] if isinstance(ob, dict) else ob
            res.check("differential.deterministic_prefix_before_concurrency", "both", pa == pb, c["id"])
            if pa != pb:
                res.mismatch(c, {a: oa, b: ob}, note=f"prefix of {first} steps differs")

    conc, i = [], 0
    while len(conc) < max(12, n // 10):
        i += 1
        conc.append(gen_concurrent(rng, i, rng.choice([16, 32, 64])))
    dist = {}
    for rep in range(repeats):
        couts = run_both(protos, "broker-scenario", conc, res)
        for p in protos:
            for c in conc:
                out = couts[p.name].get(c["id"])
                check_run(res, p.name, c, out)
                if not isinstance(out, dict) or "steps" not in out:
                    continue
                grp = out["steps"][-1]
                observed = grp.get("outcomes") if isinstance(grp, dict) else None
                ok = isinstance(observed, dict) and grp.get("result") == "concurrent" and \
                    all(r == {"result": "grant_issued"} for r in out["steps"][:-1])
                if ok:
                    ok = serializable(c, observed, out.get("final") or {})
                if ok is None:
                    res.check("broker.concurrent_serialization_search_completed", p.name, False, c["id"],
                              {"outcomes": observed})
                    continue
                res.check("broker.concurrent_group_equals_a_serialization", p.name, ok, c["id"],
                          {"outcomes": observed, "final": out.get("final")}, case=None if ok else c)
                sig = ",".join(f"{k}={v}" for k, v in sorted((observed or {}).items()))
                dist.setdefault(f"{c['id']}:{p.name}", Counter())[sig] += 1
                dist.setdefault(f"{p.name}:x{c['meta']['n']}", Counter())[sig] += 1
    res.concurrency_distribution = {k: dict(v) for k, v in dist.items()}
    res.notes.append(f"concurrency: {len(conc)} groups x {repeats} repeats per prototype")
    return probe_cases + cases + conc, outs


def extra_report(res):
    dist = getattr(res, "concurrency_distribution", {})
    per_group = {k: v for k, v in dist.items() if k.startswith("brc-")}
    return {"concurrency_groups_with_multiple_outcomes": {k: v for k, v in per_group.items() if len(v) > 1},
            "concurrency_groups": len(per_group),
            "concurrency_groups_nondeterministic": sum(1 for v in per_group.values() if len(v) > 1)}
