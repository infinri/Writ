"""Campaign `transition`: broker promotion transactions (`transition-scenario`).

Oracles (SCENARIOS.md "Broker: transition-scenario", D-018 A6, D-019 N-3):
- promote check order: a valid promote commits; one injected fault gives the
  code of its step (permit missing, request mismatch, stale authorization,
  protection view, request shape, protection binding, compare-and-set,
  acceptance); two faults give the earlier step; refusals change nothing;
- sequence invariants on every generated run: refusals counted exactly; receipts
  equal distinct committed keys; revision accounting; consumed record ids
  unique and only from committed protected promotions; a record is never
  consumed twice; a protected commit always carried a record; replays repeat the
  original result; an effect key in unknown_reconcile is never re-dispatched;
- inserting a refusal (fresh key, null permit; or reconcile of an unknown key)
  anywhere leaves every other step and the final state unchanged except +1 refusal;
- concurrency: promote_concurrent of 16/32/64 commits exactly once; `concurrent`
  groups of 16/32/64 promotes or effects match the outcome of some serialization
  (closed-form for these symmetric groups); small mixed groups (<= 5 ops) must
  equal the outcome of one of their serial orders run through the same prototype.
"""
from __future__ import annotations

import itertools
import json
from collections import Counter

from .c_accept import CLASSES, ident, idn, sha, ts
from .common import Results, clone, differential, digest, dump_variant, run_both, run_probes

NAME = "transition"


def _k(x):
    return json.dumps(x, sort_keys=True)
STATES = ["draft", "planning", "verifying", "reviewing", "accepted", "deployed", "released"]
EPOCH = 1793530800  # 2026-11-01T11:00:00Z
REFUSED = "refused"


# ------------------------------------------------------------------ builders

def make_initial(rng, state=None):
    now = EPOCH + rng.randint(0, 5 * 86400)
    pol_sha, auth_sha = sha(rng), sha(rng)
    nreq = rng.randint(1, 3)
    cids = sorted({ident(rng) for _ in range(nreq)})
    req = {"acceptance_policy_bundle_sha": pol_sha, "acceptance_policy_id": idn(rng),
           "acceptance_policy_version": str(rng.randint(1, 99)),
           "criteria": [{"criterion_id": c, "required_class": rng.choice(list(CLASSES))} for c in cids]}
    pstates = sorted(set(rng.sample(["accepted", "deployed", "released", "reviewing"], rng.randint(1, 3))))
    edges = []
    if rng.random() < 0.7:
        f, t = rng.choice([("draft", "verifying"), ("planning", "verifying"), ("verifying", "reviewing"),
                           ("draft", "planning")])
        if t not in pstates:
            edges.append({"from_state": f, "to_state": t})
    st = {"tenant_id": idn(rng), "project_id": idn(rng), "task_id": idn(rng), "workflow_id": ident(rng),
          "state": state or rng.choice(["draft", "planning", "verifying", "reviewing"]),
          "revision": rng.randint(0, 1000), "artifact_snapshot_id": idn(rng), "artifact_digest": sha(rng),
          "acceptance_policy_id": req["acceptance_policy_id"], "acceptance_policy_version": req["acceptance_policy_version"],
          "acceptance_policy_bundle_sha": pol_sha, "acceptance_requirements": req, "authorization_bundle_sha": auth_sha,
          "protection": {"acceptance_policy_bundle_sha": pol_sha, "authorization_bundle_sha": auth_sha,
                         "protected_to_states": pstates, "protected_transitions": edges},
          "effect_prerequisites": {"repository.publish": {"required_states": ["accepted", "deployed", "released"]}},
          "last_promotion": None, "now": ts(now), "consumed_record_ids": [], "revoked_evaluator_ids": []}
    return st, {"now": now}


def protected(st, frm, to):
    p = st.get("protection") or {}
    return to in p.get("protected_to_states", []) or {"from_state": frm, "to_state": to} in p.get("protected_transitions", [])


def make_record(rng, st, now, to, rid=None):
    rec = {"contract_version": "m1-draft-3", "acceptance_record_id": rid or ("acc-" + idn(rng)),
           "tenant_id": st["tenant_id"], "project_id": st["project_id"], "task_id": st["task_id"],
           "workflow_id": st["workflow_id"], "transition": {"from_state": st["state"], "to_state": to},
           "artifact_snapshot_id": st["artifact_snapshot_id"], "artifact_digest": st["artifact_digest"],
           "acceptance_policy_id": st["acceptance_policy_id"], "acceptance_policy_version": st["acceptance_policy_version"],
           "acceptance_policy_bundle_sha": st["acceptance_policy_bundle_sha"], "state_revision": st["revision"],
           "status": "accepted", "issued_at": ts(now - rng.randint(1, 600)), "expires_at": ts(now + rng.randint(3600, 7200)),
           "criteria": [{"criterion_id": c["criterion_id"], "mandatory": True, "required_class": c["required_class"],
                         "status": "pass", "evaluator_id": "ev-" + c["criterion_id"],
                         "evaluator_trust": CLASSES[c["required_class"]],
                         "fresh_until": ts(now + rng.randint(3000, 3600))}
                        for c in st["acceptance_requirements"]["criteria"]]}
    return seal_record(rec)


def seal_record(rec):
    rec.pop("record_digest", None)
    rec["record_digest"] = digest(rec)
    return rec


def make_request(st, kind, attrs, rid, resource=None, expected=True):
    return {"action": {"action_digest": "sha256:" + "b" * 64, "attributes": attrs, "kind": kind,
                       "requested_capabilities": [],
                       "resource": {"canonical_resource_id": resource or f"workflow:{st['tenant_id']}/{st['project_id']}/{st['workflow_id']}",
                                    "uri": "writ-workflow://x"}},
            "agent_id": None, "contract_version": "2.0", "deadline_at": None,
            "expected_state_revision": st["revision"] if expected else None, "idempotency_key": "idem-" + rid,
            "parent_session_id": None, "policy_bundle_sha": None,
            "principal": {"authn_method": "os_peer_credential", "id": "agent:session-1", "roles": ["agent"],
                          "verified_by": "writ-host-adapter:fixture"},
            "project_id": st["project_id"], "request_id": "req-" + rid, "session_id": "session-1",
            "tenant_id": st["tenant_id"], "trace_id": "trace-" + rid}


def make_promote(rng, st, now, to, key, record=None, rid=None):
    prot = protected(st, st["state"], to)
    attrs = {"from_state": st["state"], "to_state": to}
    if prot:
        record = record or make_record(rng, st, now, to)
        attrs.update({"acceptance_record_id": record["acceptance_record_id"],
                      "artifact_snapshot_id": record["artifact_snapshot_id"], "artifact_digest": record["artifact_digest"]})
    else:
        record = None
    req = make_request(st, "workflow.transition", attrs, rid or idn(rng))
    op = {"op": "promote", "idempotency_key": key, "request": req, "record": record,
          "permit": {"request_fingerprint": digest(req), "record_digest": record["record_digest"] if record else None,
                     "authorization_bundle_sha": st["authorization_bundle_sha"]}}
    return op


def refingerprint(op):
    op["permit"]["request_fingerprint"] = digest(op["request"])
    return op


def make_effect(rng, st, key, rid=None, fault=None):
    lp = st["last_promotion"] or {}
    attrs = {"workflow_id": st["workflow_id"], "artifact_snapshot_id": lp.get("artifact_snapshot_id", st["artifact_snapshot_id"]),
             "artifact_digest": lp.get("artifact_digest", st["artifact_digest"]),
             "promotion_receipt_id": lp.get("receipt_id", f"promotion:{st['workflow_id']}:{st['revision']}")}
    req = make_request(st, "repository.publish", attrs, rid or idn(rng),
                       resource=f"repository:{st['tenant_id']}/{st['project_id']}/releases", expected=False)
    op = {"op": "effect", "idempotency_key": key, "request": req,
          "permit": {"request_fingerprint": digest(req), "authorization_bundle_sha": st["authorization_bundle_sha"]}}
    if fault:
        op["fault"] = fault
    return op


def promoted_initial(rng):
    st, ctx = make_initial(rng, state="accepted")
    rid = "acc-" + idn(rng)
    st["consumed_record_ids"] = [rid]
    st["last_promotion"] = {"acceptance_policy_bundle_sha": st["acceptance_policy_bundle_sha"],
                            "artifact_digest": st["artifact_digest"], "artifact_snapshot_id": st["artifact_snapshot_id"],
                            "authorization_bundle_sha": st["authorization_bundle_sha"],
                            "receipt_id": f"promotion:{st['workflow_id']}:{st['revision']}", "record_id": rid,
                            "to_state": "accepted"}
    return st, ctx


def pick_target(rng, st, want_protected=None):
    opts = [s for s in STATES if s != st["state"]]
    if want_protected is not None:
        opts = [s for s in opts if protected(st, st["state"], s) == want_protected]
    return rng.choice(opts) if opts else None


# ------------------------------------------------------ (a) promote fault table

PROMOTE_FAULTS = {  # name: (step, code or None when set by the fault, applies to protected?, unprotected?)
    "permit_null": (2, "PERMIT_MISSING", True, True),
    "fingerprint_random": (3, "PERMIT_REQUEST_MISMATCH", True, True),
    "request_changed_after_permit": (3, "PERMIT_REQUEST_MISMATCH", True, True),
    "permit_bundle_stale": (4, "PERMIT_POLICY_STALE", True, True),
    "protection_absent": (5, "PROTECTION_UNAVAILABLE", True, True),
    "protection_other_bundle": (5, "PROTECTION_UNAVAILABLE", True, True),
    "protection_dependency": (5, "POLICY_DEPENDENCY_MISMATCH", True, True),
    "request_scope": (6, "REQUEST_SCOPE_MISMATCH", True, True),
    "caps_own_kind": (6, "REQUESTED_CAPABILITIES_INVALID", True, True),
    "caps_composite": (6, "COMPOSITE_NOT_SUPPORTED", True, True),
    "record_null": (7, "ACCEPTANCE_REQUIRED", True, False),
    "binding_attrs_removed": (7, "ACCEPTANCE_REQUIRED", True, False),
    "permit_record_digest": (7, "PERMIT_BINDING_MISMATCH", True, False),
    "attrs_disagree_record": (7, "PERMIT_BINDING_MISMATCH", True, False),
    "unexpected_record": (7, "ACCEPTANCE_BINDING_UNEXPECTED", False, True),
    "unexpected_attrs": (7, "ACCEPTANCE_BINDING_UNEXPECTED", False, True),
    "unexpected_permit_digest": (7, "ACCEPTANCE_BINDING_UNEXPECTED", False, True),
    "expected_revision": (8, "REVISION_CONFLICT", True, True),
    "from_state": (8, "REVISION_CONFLICT", True, True),
    "record_artifact": (9, "ACCEPTANCE_ARTIFACT_MISMATCH", True, False),
    "record_scope": (9, "ACCEPTANCE_SCOPE_MISMATCH", True, False),
    "record_revision": (9, "ACCEPTANCE_REVISION_MISMATCH", True, False),
    "record_policy": (9, "ACCEPTANCE_POLICY_MISMATCH", True, False),
    "record_replayed": (9, "ACCEPTANCE_REPLAYED", True, False),
    "evaluator_revoked": (9, "ACCEPTANCE_EVALUATOR_REVOKED", True, False),
    "criterion_failed": (9, "ACCEPTANCE_INCOMPLETE", True, False),
    "record_expired": (9, "ACCEPTANCE_EXPIRED", True, False),
    "record_status": (9, "ACCEPTANCE_RECORD_INVALID", True, False),
}
# faults that edit the request and must keep a matching fingerprint afterwards
REQUEST_EDITS = {"request_scope", "caps_own_kind", "caps_composite", "binding_attrs_removed", "attrs_disagree_record",
                 "unexpected_attrs", "expected_revision", "from_state", "record_artifact", "record_scope",
                 "record_revision", "record_policy", "evaluator_revoked", "criterion_failed", "record_expired",
                 "record_status"}


def apply_promote_fault(rng, name, st, op, ctx):
    req, rec, permit = op["request"], op["record"], op["permit"]
    at = req["action"]["attributes"]

    def resync_record():
        seal_record(rec)
        at.update({"acceptance_record_id": rec["acceptance_record_id"], "artifact_snapshot_id": rec["artifact_snapshot_id"],
                   "artifact_digest": rec["artifact_digest"]})
        permit["record_digest"] = rec["record_digest"]

    if name == "permit_null":
        op["permit"] = None
    elif name == "fingerprint_random":
        permit["request_fingerprint"] = sha(rng)
    elif name == "request_changed_after_permit":
        ctx["no_refingerprint"] = True
        req[rng.choice(["trace_id", "request_id", "session_id"])] += "-x"
    elif name == "permit_bundle_stale":
        permit["authorization_bundle_sha"] = sha(rng)
    elif name == "protection_absent":
        st["protection"] = None
    elif name == "protection_other_bundle":
        st["protection"]["authorization_bundle_sha"] = sha(rng)
    elif name == "protection_dependency":
        st["protection"]["acceptance_policy_bundle_sha"] = sha(rng)
    elif name == "request_scope":
        r = rng.choice(["kind", "tenant", "project", "resource"])
        if r == "kind":
            req["action"]["kind"] = "repository.publish"
        elif r == "tenant":
            req["tenant_id"] = "other-" + req["tenant_id"]
        elif r == "project":
            req["project_id"] = "other-" + req["project_id"]
        else:
            req["action"]["resource"]["canonical_resource_id"] += "-other"
    elif name == "caps_own_kind":
        req["action"]["requested_capabilities"] = ["workflow.transition"]
    elif name == "caps_composite":
        req["action"]["requested_capabilities"] = [rng.choice(["repository.publish", "process.execute", "file.write"])]
    elif name == "record_null":
        op["record"] = None
        if rng.random() < 0.5:
            permit["record_digest"] = None
    elif name == "binding_attrs_removed":
        for k in ("acceptance_record_id", "artifact_snapshot_id", "artifact_digest"):
            at.pop(k, None)
    elif name == "permit_record_digest":
        permit["record_digest"] = sha(rng)
    elif name == "attrs_disagree_record":
        k = rng.choice(["acceptance_record_id", "artifact_snapshot_id", "artifact_digest"])
        at[k] = sha(rng) if k == "artifact_digest" else "x" + at[k]
    elif name == "unexpected_record":
        op["record"] = make_record(rng, st, ctx["now"], at["to_state"])
    elif name == "unexpected_attrs":
        at.update({"acceptance_record_id": "acc-x", "artifact_snapshot_id": st["artifact_snapshot_id"],
                   "artifact_digest": st["artifact_digest"]})
    elif name == "unexpected_permit_digest":
        permit["record_digest"] = sha(rng)
    elif name == "expected_revision":
        req["expected_state_revision"] = st["revision"] + rng.randint(1, 3)
    elif name == "from_state":
        prot = protected(st, st["state"], at["to_state"])
        opts = [s for s in STATES if s not in (st["state"], at["to_state"]) and protected(st, s, at["to_state"]) == prot]
        if not opts:
            ctx["invalid"] = True
            return
        at["from_state"] = rng.choice(opts)
    elif name.startswith("record_") or name in ("evaluator_revoked", "criterion_failed"):
        if name == "record_artifact":
            if rng.random() < 0.5:
                rec["artifact_snapshot_id"] = "snap-x" + idn(rng)
            else:
                rec["artifact_digest"] = sha(rng)
        elif name == "record_scope":
            f = rng.choice(["tenant_id", "project_id", "task_id"])
            rec[f] = "x" + rec[f]
        elif name == "record_revision":
            rec["state_revision"] = st["revision"] + rng.randint(1, 5)
        elif name == "record_policy":
            rec["acceptance_policy_version"] = str(int(rec["acceptance_policy_version"]) + 1)
        elif name == "record_replayed":
            st["consumed_record_ids"] = [rec["acceptance_record_id"]]
        elif name == "evaluator_revoked":
            st["revoked_evaluator_ids"] = [rng.choice(rec["criteria"])["evaluator_id"]]
        elif name == "criterion_failed":
            rng.choice(rec["criteria"])["status"] = rng.choice(["fail", "unknown", "waived"])
        elif name == "record_expired":
            rec["expires_at"] = ts(ctx["now"])
        elif name == "record_status":
            rec["status"] = rng.choice(["rejected", "incomplete", "needs_human_review"])
        if name != "record_replayed":
            resync_record()


def gen_promote_fault_case(rng, idx):
    st, ctx = make_initial(rng)
    prot_wanted = rng.random() < 0.6
    to = pick_target(rng, st, prot_wanted)
    if to is None:
        return None
    op = make_promote(rng, st, ctx["now"], to, "k1")
    prot = op["record"] is not None
    names = [n for n, v in PROMOTE_FAULTS.items() if (v[2] if prot else v[3])]
    k = rng.choices([0, 1, 2], [1, 6, 3])[0]
    picks = []
    while len(picks) < k:
        n = rng.choice(names)
        if n in picks or (picks and PROMOTE_FAULTS[n][0] == PROMOTE_FAULTS[picks[0]][0]):
            continue
        # record_null / binding removal make record faults impossible
        if {n, *picks} & {"record_null", "permit_null"} and {n, *picks} & {x for x in PROMOTE_FAULTS if PROMOTE_FAULTS[x][0] == 9}:
            continue
        if {"binding_attrs_removed"} & {n, *picks} and {n, *picks} & {x for x in PROMOTE_FAULTS if PROMOTE_FAULTS[x][0] == 9}:
            continue
        picks.append(n)
    # request-editing faults first, then refingerprint, then fingerprint/permit faults
    picks.sort(key=lambda n: (n not in REQUEST_EDITS, PROMOTE_FAULTS[n][0] != 9, n))
    fctx = dict(ctx)
    for n in picks:
        if op["permit"] is None and n not in REQUEST_EDITS:
            return None  # permit already removed; this fault has nothing to edit
        apply_promote_fault(rng, n, st, op, fctx)
        if fctx.get("invalid"):
            return None
        if n in REQUEST_EDITS and op["permit"] is not None and not fctx.get("no_refingerprint"):
            refingerprint(op)
    exp_steps = sorted((PROMOTE_FAULTS[n][0], PROMOTE_FAULTS[n][1]) for n in picks)
    r0 = st["revision"]
    if exp_steps:
        expect = {"steps": [{"result": "refused", "code": exp_steps[0][1]}],
                  "final": {"state": st["state"], "revision": r0, "consumed_record_ids": sorted(st["consumed_record_ids"]),
                            "receipts": 0, "refusals": 1, "effects": {}}}
    else:
        expect = {"steps": [{"result": "committed", "revision": r0 + 1}],
                  "final": {"state": to, "revision": r0 + 1,
                            "consumed_record_ids": sorted(st["consumed_record_ids"] + ([op["record"]["acceptance_record_id"]] if prot else [])),
                            "receipts": 1, "refusals": 0, "effects": {}}}
    return {"id": f"trp-{idx:06d}-{'+'.join(picks) or 'valid'}", "command": "transition-scenario",
            "input": {"initial": st, "steps": [op]}, "meta": {"family": "promote_faults", "faults": picks,
                                                             "protected": prot, "expect": expect}}


# -------------------------------------------------------- (b) random sequences

def gen_sequence(rng, idx):
    pre = rng.random() < 0.4
    st, ctx = promoted_initial(rng) if pre else make_initial(rng)
    initial = clone(st)
    now = ctx["now"]
    pred = clone(st)  # generator-side guess; oracles never use it
    steps, history = [], []
    for _ in range(rng.randint(3, 12)):
        r = rng.random()
        if r < 0.35:
            to = pick_target(rng, pred)
            if to is None:
                continue
            op = make_promote(rng, pred, now, to, rng.choice(["k1", "k2", "k3", "k" + idn(rng)]))
            if rng.random() < 0.25:
                n = rng.choice([x for x, v in PROMOTE_FAULTS.items() if v[2 if op["record"] else 3] and v[0] != 5])
                fctx = {"now": now}
                apply_promote_fault(rng, n, clone(pred), op, fctx)
                if n in REQUEST_EDITS and op["permit"] is not None and not fctx.get("no_refingerprint"):
                    refingerprint(op)
            elif rng.random() < 0.15:
                op["fault"] = "crash_before_commit"
            else:
                pred["revision"] += 1
                if op["record"]:
                    pred["last_promotion"] = {"receipt_id": f"promotion:{pred['workflow_id']}:{pred['revision']}",
                                              "artifact_snapshot_id": pred["artifact_snapshot_id"],
                                              "artifact_digest": pred["artifact_digest"]}
                pred["state"] = to
            steps.append(op)
            history.append(op)
        elif r < 0.45 and history:
            prev = clone(rng.choice(history))
            if rng.random() < 0.5 and prev["op"] in ("promote", "effect"):
                prev["request"]["trace_id"] += "-v2"
                if prev.get("permit"):
                    prev["permit"]["request_fingerprint"] = digest(prev["request"])
            steps.append(prev)
        elif r < 0.65:
            key = rng.choice(["e1", "e2", "e3"])
            fault = rng.choice([None, None, "crash_after_dispatch", "crash_before_reserve"])
            op = make_effect(rng, pred, key, fault=fault)
            if rng.random() < 0.15:
                op["permit"] = None
            steps.append(op)
            history.append(op)
        elif r < 0.72:
            steps.append({"op": "reconcile", "idempotency_key": rng.choice(["e1", "e2", "e3", "zz"]),
                          "observed": rng.choice(["applied", "not_applied"])})
        elif r < 0.77:
            snap, dig = idn(rng), sha(rng)
            steps.append({"op": "update_artifact", "artifact_snapshot_id": snap, "artifact_digest": dig})
            pred["revision"] += 1
            pred["artifact_snapshot_id"], pred["artifact_digest"] = snap, dig
        elif r < 0.81:
            v = str(int(pred["acceptance_policy_version"]) + 1)
            psha = sha(rng)
            reqs = clone(pred["acceptance_requirements"])
            reqs.update({"acceptance_policy_version": v, "acceptance_policy_bundle_sha": psha})
            steps.append({"op": "activate_policy", "acceptance_policy_version": v, "acceptance_policy_bundle_sha": psha,
                          "acceptance_requirements": reqs})
            pred.update({"acceptance_policy_version": v, "acceptance_policy_bundle_sha": psha, "acceptance_requirements": reqs})
        elif r < 0.85:
            asha = sha(rng)
            prot = clone(pred["protection"] or initial["protection"])
            prot.update({"authorization_bundle_sha": asha, "acceptance_policy_bundle_sha": pred["acceptance_policy_bundle_sha"]})
            steps.append({"op": "activate_authorization_bundle", "authorization_bundle_sha": asha, "protection": prot})
            pred.update({"authorization_bundle_sha": asha, "protection": prot})
        elif r < 0.88:
            ev = rng.choice(["ev-" + c["criterion_id"] for c in pred["acceptance_requirements"]["criteria"]] + ["ev-other"])
            steps.append({"op": "revoke_evaluator", "evaluator_id": ev})
        elif r < 0.92:
            now += rng.choice([1, 60, 3600, 7200])
            steps.append({"op": "advance_time", "now": ts(now)})
        else:
            ops = []
            for _ in range(rng.randint(2, 4)):
                if rng.random() < 0.5:
                    to = pick_target(rng, pred)
                    if to:
                        ops.append(make_promote(rng, pred, now, to, rng.choice(["k1", "c1", "c2"])))
                else:
                    ops.append(make_effect(rng, pred, rng.choice(["e1", "e2"])))
            if ops:
                steps.append({"op": "concurrent", "operations": ops})
    if not steps:
        return None
    return {"id": f"trs-{idx:06d}", "command": "transition-scenario", "input": {"initial": initial, "steps": steps},
            "meta": {"family": "sequence"}}


def results_flat(steps_in, steps_out):
    """[(op, result)] in program order, concurrent groups expanded in submission order."""
    out = []
    for op, res in zip(steps_in, steps_out):
        if op.get("op") == "concurrent" and isinstance(res, dict) and isinstance(res.get("results"), list):
            out.extend(zip(op["operations"], res["results"]))
        else:
            out.append((op, res))
    return out


def check_sequence(res, pname, case, out):
    inp = case["input"]
    cid = case["id"]
    if not isinstance(out, dict) or not isinstance(out.get("steps"), list) or len(out["steps"]) != len(inp["steps"]):
        res.check("transition.output_shape", pname, False, cid, out, case=case)
        return
    fin = out.get("final") or {}
    flat = results_flat(inp["steps"], out["steps"])
    refusals = sum(1 for _, r in flat if isinstance(r, dict) and r.get("result") == REFUSED)
    for op, r in zip(inp["steps"], out["steps"]):
        if op.get("op") == "promote_concurrent" and isinstance(r, dict):
            refusals += sum((r.get("refused") or {}).values())
    res.check("transition.refusals_counted_exactly", pname, fin.get("refusals") == refusals, cid,
              {"final": fin.get("refusals"), "counted": refusals}, case=None if fin.get("refusals") == refusals else case)
    # receipts and revision accounting
    committed_keys, commits, updates = set(), 0, 0
    consumed = list(inp["initial"]["consumed_record_ids"])
    double_consume, unprotected_record_commit = False, False
    protection = inp["initial"].get("protection")
    seq_ok = True
    rev = inp["initial"]["revision"]
    first_result = {}
    replay_ok = True
    in_concurrent = set()
    for op, r in zip(inp["steps"], out["steps"]):
        if op.get("op") == "concurrent":
            for o2 in op["operations"]:
                in_concurrent.add(id(o2))
    group_end = {}
    for op, r in zip(inp["steps"], out["steps"]):
        if op.get("op") == "concurrent" and op["operations"]:
            group_end[id(op["operations"][-1])] = op["operations"]
    for op, r in flat:
        if not isinstance(r, dict):
            continue
        conc = id(op) in in_concurrent
        if op.get("op") == "promote" and r.get("result") == "committed":
            if r.get("replayed"):
                k = op["idempotency_key"]
                if k in first_result and first_result[k] != {kk: vv for kk, vv in r.items() if kk != "replayed"}:
                    replay_ok = False
                continue
            commits += 1
            committed_keys.add(op["idempotency_key"])
            first_result[op["idempotency_key"]] = dict(r)
            at = op["request"]["action"]["attributes"]
            if protection and (at.get("to_state") in protection.get("protected_to_states", []) or
                               {"from_state": at.get("from_state"), "to_state": at.get("to_state")}
                               in protection.get("protected_transitions", [])):
                if op.get("record") is None:
                    unprotected_record_commit = True
                else:
                    rid = op["record"].get("acceptance_record_id")
                    if rid in consumed:
                        double_consume = True
                    consumed.append(rid)
        elif op.get("op") == "update_artifact" and r.get("result") == "artifact_updated":
            updates += 1
        elif op.get("op") == "activate_authorization_bundle" and r.get("result") == "authorization_bundle_activated":
            protection = op.get("protection")

    for op, r in zip(inp["steps"], out["steps"]):
        if not isinstance(r, dict):
            continue
        if op.get("op") == "promote_concurrent":
            commits += r.get("committed", 0)
            rev += r.get("committed", 0)
        elif op.get("op") == "concurrent":
            rev += sum(1 for r3 in r.get("results") or [] if isinstance(r3, dict) and not r3.get("replayed")
                       and r3.get("result") in ("committed", "artifact_updated"))
        elif r.get("result") in ("committed", "artifact_updated") and not r.get("replayed"):
            rev += 1
            if r.get("revision") != rev:
                seq_ok = False
    ok = fin.get("receipts") == len(committed_keys) + sum(
        (r or {}).get("committed", 0) for op, r in zip(inp["steps"], out["steps"]) if op.get("op") == "promote_concurrent")
    res.check("transition.receipts_equal_distinct_committed_keys", pname, ok, cid,
              {"final": fin.get("receipts"), "keys": sorted(committed_keys)}, case=None if ok else case)
    ok = fin.get("revision") == inp["initial"]["revision"] + commits + updates
    res.check("transition.revision_accounting", pname, ok, cid,
              {"final": fin.get("revision"), "initial": inp["initial"]["revision"], "commits": commits, "updates": updates},
              case=None if ok else case)
    res.check("transition.sequential_commit_revisions_increment", pname, seq_ok, cid, out["steps"],
              case=None if seq_ok else case)
    res.check("transition.replay_repeats_original_result", pname, replay_ok, cid, out["steps"],
              case=None if replay_ok else case)
    res.check("transition.record_never_consumed_twice", pname, not double_consume, cid, out,
              case=case if double_consume else None)
    res.check("transition.protected_commit_always_has_record", pname, not unprotected_record_commit, cid, out,
              case=case if unprotected_record_commit else None)
    fc = fin.get("consumed_record_ids")
    ok = isinstance(fc, list) and fc == sorted(set(fc))
    if not any(o.get("op") in ("promote_concurrent",) for o in inp["steps"]):
        ok = ok and sorted(set(consumed)) == fc
    res.check("transition.consumed_ids_sorted_unique_and_from_commits", pname, ok, cid,
              {"final": fc, "expected": sorted(set(consumed))}, case=None if ok else case)
    # effects: never re-dispatched while uncertain
    status = {}
    redispatch = False
    for op, r in flat:
        if op.get("op") == "effect" and isinstance(r, dict):
            k = op["idempotency_key"]
            dispatched = (r.get("result") == "effect_completed" and not r.get("replayed")) or \
                (r.get("result") == "crashed" and op.get("fault") == "crash_after_dispatch")
            if dispatched and status.get(k) in ("unknown_reconcile", "completed"):
                redispatch = True
            if r.get("result") == "effect_completed" and not r.get("replayed"):
                status[k] = "completed"
            elif r.get("result") == "crashed" and op.get("fault") == "crash_after_dispatch":
                status[k] = "unknown_reconcile"
        elif op.get("op") == "reconcile" and isinstance(r, dict) and r.get("result") == "reconciled":
            k = op["idempotency_key"]
            if status.get(k) != "unknown_reconcile":
                redispatch = True  # reconciliation fabricated for a key that was not uncertain
            status[k] = "completed" if op.get("observed") == "applied" else None
    status = {k: v for k, v in status.items() if v}
    res.check("transition.uncertain_effect_never_redispatched", pname, not redispatch, cid, out,
              case=case if redispatch else None)
    ok = fin.get("effects") == status
    res.check("transition.final_effects_match_observed_dispatches", pname, ok, cid,
              {"final": fin.get("effects"), "observed": status}, case=None if ok else case)


def insert_refusal(rng, case):
    """Metamorphic twin: a guaranteed refusal inserted at a random position."""
    c = clone(case)
    steps = c["input"]["steps"]
    pos = rng.randint(0, len(steps))
    if rng.random() < 0.5:
        st = c["input"]["initial"]
        op = make_promote(rng, st, 0, "zz-nowhere", "fresh-" + idn(rng))
        op["permit"] = None
        op["record"] = None
        ins = op
    else:
        ins = {"op": "reconcile", "idempotency_key": "never-used-" + idn(rng), "observed": "applied"}
    steps.insert(pos, ins)
    c["id"] = case["id"] + f"-ins{pos}"
    c["meta"] = {"family": "inserted_refusal", "twin_of": case["id"], "pos": pos}
    return c


# ------------------------------------------------------------ (c) concurrency

def gen_conc_promote(rng, idx, n):
    st, ctx = make_initial(rng)
    to = pick_target(rng, st)
    if to is None:
        return None
    rec_proto = make_promote(rng, st, ctx["now"], to, "k0")
    record = rec_proto["record"]
    nvar = rng.randint(1, 3)
    variants = []
    for v in range(nvar):
        op = make_promote(rng, st, ctx["now"], to, "x", record=clone(record) if record else None, rid=f"v{v}-{idn(rng)}")
        variants.append(op)
    keys = rng.choice([[f"k{i}" for i in range(n)], ["k0", "k1", "k2"], ["k0"]])
    ops = []
    for i in range(n):
        o = clone(rng.choice(variants))
        o["idempotency_key"] = keys[i % len(keys)] if len(keys) < n else keys[i]
        ops.append(o)
    return {"id": f"trc-{idx:06d}-promote{n}", "command": "transition-scenario",
            "input": {"initial": st, "steps": [{"op": "concurrent", "operations": ops}]},
            "meta": {"family": "conc_promote", "n": n, "protected": record is not None, "to": to}}


def promote_outcomes(case):
    """Permitted step result lists: the first serialized op wins (all valid initially)."""
    ops = case["input"]["steps"][0]["operations"]
    r0 = case["input"]["initial"]["revision"]
    fps = [digest({k: v for k, v in o.items() if k != "fault"}) for o in ops]
    allowed = {}
    for w, ow in enumerate(ops):
        res = []
        for i, o in enumerate(ops):
            if i == w:
                res.append({"result": "committed", "revision": r0 + 1})
            elif o["idempotency_key"] == ow["idempotency_key"]:
                res.append({"result": "committed", "revision": r0 + 1, "replayed": True} if fps[i] == fps[w]
                           else {"result": "refused", "code": "IDEMPOTENCY_CONFLICT"})
            else:
                res.append({"result": "refused", "code": "REVISION_CONFLICT"})
        allowed[_k(res)] = (w, res)
    return allowed


def gen_conc_effect(rng, idx, n):
    st, ctx = promoted_initial(rng)
    nkeys = rng.choice([1, 2, 4])
    variants = [make_effect(rng, st, "x", rid=f"v{v}-{idn(rng)}") for v in range(rng.randint(1, 3))]
    ops = []
    for i in range(n):
        o = clone(rng.choice(variants))
        o["idempotency_key"] = f"e{rng.randrange(nkeys)}"
        if rng.random() < 0.15:
            o["fault"] = "crash_after_dispatch"
        ops.append(o)
    return {"id": f"trc-{idx:06d}-effect{n}", "command": "transition-scenario",
            "input": {"initial": st, "steps": [{"op": "concurrent", "operations": ops}]},
            "meta": {"family": "conc_effect", "n": n}}


def effect_key_outcomes(ops, idxs):
    fps = {i: digest({k: v for k, v in ops[i].items() if k != "fault"}) for i in idxs}
    allowed = set()
    for w in idxs:
        res = []
        crash = ops[w].get("fault") == "crash_after_dispatch"
        for i in idxs:
            if i == w:
                res.append({"result": "crashed"} if crash else {"result": "effect_completed"})
            elif fps[i] != fps[w]:
                res.append({"result": "refused", "code": "IDEMPOTENCY_CONFLICT"})
            else:
                res.append({"result": "refused", "code": "RECONCILIATION_REQUIRED"} if crash
                           else {"result": "effect_completed", "replayed": True})
        allowed.add(_k(res))
    return allowed


def gen_conc_mixed(rng, idx):
    pre = rng.random() < 0.5
    st, ctx = promoted_initial(rng) if pre else make_initial(rng)
    ops = []
    for _ in range(rng.randint(2, 5)):
        r = rng.random()
        if r < 0.4:
            to = pick_target(rng, st)
            ops.append(make_promote(rng, st, ctx["now"], to, rng.choice(["k1", "k2"])))
        elif r < 0.75:
            ops.append(make_effect(rng, st, rng.choice(["e1", "e2"]), fault=rng.choice([None, None, "crash_after_dispatch"])))
        elif r < 0.9:
            ops.append({"op": "update_artifact", "artifact_snapshot_id": idn(rng), "artifact_digest": sha(rng)})
        else:
            ops.append({"op": "revoke_evaluator", "evaluator_id": "ev-" + st["acceptance_requirements"]["criteria"][0]["criterion_id"]})
    return {"id": f"trm-{idx:06d}-mixed{len(ops)}", "command": "transition-scenario",
            "input": {"initial": st, "steps": [{"op": "concurrent", "operations": ops}]},
            "meta": {"family": "conc_mixed"}}


def gen_promote_concurrent(rng, idx, n):
    st, ctx = make_initial(rng)
    to = pick_target(rng, st)
    if to is None:
        return None
    op = make_promote(rng, st, ctx["now"], to, "pc")
    valid = rng.random() < 0.8
    if not valid:
        op["permit"] = None
    r0 = st["revision"]
    expect = {"steps": [{"result": "concurrent", "committed": 1, "refused": {"REVISION_CONFLICT": n - 1}}
                        if valid else {"result": "concurrent", "committed": 0, "refused": {"PERMIT_MISSING": n}}],
              "final": {"state": to if valid else st["state"], "revision": r0 + (1 if valid else 0),
                        "consumed_record_ids": [op["record"]["acceptance_record_id"]] if (valid and op["record"]) else [],
                        "receipts": 1 if valid else 0, "refusals": n - 1 if valid else n, "effects": {}}}
    if n == 1 and valid:
        expect["steps"][0]["refused"] = {}
    return {"id": f"trpc-{idx:06d}-x{n}", "command": "transition-scenario",
            "input": {"initial": st, "steps": [{"op": "promote_concurrent", "count": n, "promote": op}]},
            "meta": {"family": "promote_concurrent", "n": n, "expect": expect}}


# --------------------------------------------------------------------- run

def probes(rng):
    """Inputs whose result the spec does not define: differential only."""
    st, ctx = make_initial(rng)
    to = pick_target(rng, st)
    op = make_promote(rng, st, ctx["now"], to, "k1")
    bad = [("unknown_op", [{"op": "zz_unknown_operation"}]),
           ("unknown_fault", [dict(op, fault="zz_fault")]),
           ("effect_fault_on_promote", [dict(op, fault="crash_after_dispatch")]),
           ("promote_concurrent_zero", [{"op": "promote_concurrent", "count": 0, "promote": op}]),
           ("promote_concurrent_negative", [{"op": "promote_concurrent", "count": -1, "promote": op}]),
           ("reconcile_bad_observed", [{"op": "reconcile", "idempotency_key": "e1", "observed": "maybe"}]),
           ("time_backwards", [{"op": "advance_time", "now": "2020-01-01T00:00:00Z"}, op]),
           ("concurrent_empty", [{"op": "concurrent", "operations": []}]),
           ("empty_steps", [])]
    out = [{"id": f"trq-{name}", "command": "transition-scenario", "input": {"initial": st, "steps": steps},
            "meta": {"family": "probe"}} for name, steps in bad]
    out.append({"id": "trq-empty-initial", "command": "transition-scenario", "input": {"initial": {}, "steps": [op]},
                "meta": {"family": "probe"}})
    return out


def gen_cases(rng, n):
    cases, i = [], 0
    while len(cases) < n:
        i += 1
        r = rng.random()
        c = gen_promote_fault_case(rng, i) if r < 0.45 else gen_sequence(rng, i)
        if c:
            cases.append(c)
            if c["meta"]["family"] in ("promote_faults", "sequence") and rng.random() < 0.15 and \
                    not any(s.get("op") in ("concurrent", "promote_concurrent") for s in c["input"]["steps"]):
                cases.append({"id": c["id"] + "-text", "command": "transition-scenario",
                              "input_text": dump_variant(c["input"], rng, p_ws=0.2, p_escape=0.1),
                              "meta": {"family": "text_twin", "twin_of": c["id"]}})
            if c["meta"]["family"] == "sequence" and rng.random() < 0.5 and \
                    not any(s.get("op") == "concurrent" for s in c["input"]["steps"]):
                cases.append(insert_refusal(rng, c))
    return cases


def gen_concurrency(rng, n):
    conc, i = [], 0
    target = max(12, n // 8)
    while len(conc) < target:
        i += 1
        size = rng.choice([16, 32, 64])
        r = rng.random()
        c = gen_promote_concurrent(rng, i, size) if r < 0.25 else gen_conc_promote(rng, i, size) if r < 0.55 \
            else gen_conc_effect(rng, i, size) if r < 0.8 else gen_conc_mixed(rng, i)
        if c:
            conc.append(c)
    return conc


def serial_cases(case):
    ops = case["input"]["steps"][0]["operations"]
    out = []
    for perm in itertools.permutations(range(len(ops))):
        out.append({"id": case["id"] + "-perm-" + "".join(map(str, perm)), "command": "transition-scenario",
                    "input": {"initial": case["input"]["initial"], "steps": [ops[j] for j in perm]},
                    "meta": {"perm": perm, "of": case["id"]}})
    return out


def run(protos, rng, n, res: Results, repeats: int = 5):
    probe_cases = probes(rng)
    run_probes(protos, probe_cases, res, "transition")
    cases = gen_cases(rng, n)
    outs = run_both(protos, "transition-scenario", cases, res)
    for p in protos:
        o = outs[p.name]
        for c in cases:
            out = o.get(c["id"])
            fam = c["meta"]["family"]
            if fam == "probe":
                continue
            if fam == "text_twin":
                base = o.get(c["meta"]["twin_of"])
                twin = next(x for x in cases if x["id"] == c["meta"]["twin_of"])
                ok = out == base
                res.check("transition.key_order_whitespace_escape_invariance", p.name, ok, c["id"],
                          {"text": out, "value": base}, case=None if ok else twin)
                continue
            if fam == "promote_faults":
                exp = c["meta"]["expect"]
                ok = out == exp
                nf = len(c["meta"]["faults"])
                inv = "transition.valid_promote_commits" if nf == 0 else \
                    "transition.promote_single_fault_exact" if nf == 1 else "transition.promote_multi_fault_earliest_step"
                res.check(inv, p.name, ok, c["id"], {"got": out, "want": exp, "faults": c["meta"]["faults"]},
                          case=None if ok else c)
            check_sequence(res, p.name, c, out)
            if fam == "inserted_refusal":
                base = o.get(c["meta"]["twin_of"])
                pos = c["meta"]["pos"]
                ok = isinstance(out, dict) and isinstance(base, dict) and "steps" in out and "steps" in base
                if ok:
                    s2 = list(out["steps"])
                    ins = s2.pop(pos)
                    f2 = dict(out["final"])
                    f2["refusals"] = f2.get("refusals", 0) - 1
                    ok = s2 == base["steps"] and f2 == base["final"] and ins.get("result") == REFUSED
                res.check("transition.inserted_refusal_changes_nothing_else", p.name, ok, c["id"],
                          {"with": out, "without": base}, case=None if ok else c)
    has_conc = lambda c: "input" in c and any(s.get("op") in ("concurrent", "promote_concurrent")  # noqa: E731
                                              for s in c["input"].get("steps", []))
    differential(protos, outs, cases, res, skip=has_conc)
    # sequences with a concurrent group may legitimately differ inside and after the group;
    # the deterministic prefix before the first group must still be identical
    if len(protos) == 2:
        a, b = protos[0].name, protos[1].name
        for c in filter(has_conc, cases):
            first = next(i for i, s in enumerate(c["input"]["steps"]) if s.get("op") in ("concurrent", "promote_concurrent"))
            oa, ob = outs[a].get(c["id"]), outs[b].get(c["id"])
            pa = (oa or {}).get("steps", [None])[:first] if isinstance(oa, dict) else oa
            pb = (ob or {}).get("steps", [None])[:first] if isinstance(ob, dict) else ob
            res.check("differential.deterministic_prefix_before_concurrency", "both", pa == pb, c["id"])
            if pa != pb:
                res.mismatch(c, {a: oa, b: ob}, note=f"prefix of {first} steps differs")

    # concurrency, repeated
    conc = gen_concurrency(rng, n)
    serial = [s for c in conc if c["meta"]["family"] == "conc_mixed" for s in serial_cases(c)]
    souts = run_both(protos, "transition-scenario", serial, res) if serial else {p.name: {} for p in protos}
    differential(protos, souts, serial, res)
    dist = {}
    for rep in range(repeats):
        couts = run_both(protos, "transition-scenario", conc, res)
        for p in protos:
            for c in conc:
                out = couts[p.name].get(c["id"])
                check_concurrent(res, p.name, c, out, souts[p.name], dist, rep)
    res.concurrency_distribution = {k: dict(v) for k, v in dist.items()}
    res.notes.append(f"concurrency: {len(conc)} groups x {repeats} repeats per prototype; {len(serial)} serial orders")
    return probe_cases + cases + conc, outs


def outcome_sig(out):
    if not isinstance(out, dict) or not out.get("steps"):
        return "no-output"
    s = out["steps"][0]
    if "results" in s:
        return ",".join(f"{k}={v}" for k, v in sorted(Counter(
            (r.get("code") or r.get("result")) + ("/replayed" if r.get("replayed") else "") for r in s["results"]).items()))
    return f"committed={s.get('committed')};refused={s.get('refused')}"


def check_concurrent(res, pname, c, out, serial_outs, dist, rep):
    fam = c["meta"]["family"]
    cid = c["id"]
    key = f"{pname}:{fam}:{c['meta'].get('n', '')}"
    d = dist.setdefault(cid + ":" + pname, Counter())
    d[outcome_sig(out)] += 1
    dist.setdefault(key, Counter())[outcome_sig(out)] += 1
    check_sequence(res, pname, c, out)
    if fam == "promote_concurrent":
        ok = out == c["meta"]["expect"]
        res.check("transition.promote_concurrent_commits_exactly_once", pname, ok, cid,
                  {"got": out, "want": c["meta"]["expect"]}, case=None if ok else c)
        return
    if not isinstance(out, dict) or not out.get("steps") or "results" not in out["steps"][0]:
        res.check("transition.concurrent_output_shape", pname, False, cid, out, case=c)
        return
    results = out["steps"][0]["results"]
    if fam == "conc_promote":
        allowed = promote_outcomes(c)
        ok = _k(results) in allowed
        res.check("transition.concurrent_promotes_equal_a_serialization", pname, ok, cid,
                  {"got": outcome_sig(out)}, case=None if ok else c)
        if ok:
            w = allowed[_k(results)][0]
            st0 = c["input"]["initial"]
            prot = c["meta"]["protected"]
            want_final = {"state": c["meta"]["to"], "revision": st0["revision"] + 1,
                          "consumed_record_ids": [c["input"]["steps"][0]["operations"][w]["record"]["acceptance_record_id"]]
                          if prot else [], "receipts": 1,
                          "refusals": sum(1 for r in results if r["result"] == REFUSED), "effects": {}}
            okf = out.get("final") == want_final
            res.check("transition.concurrent_promotes_final_state", pname, okf, cid,
                      {"got": out.get("final"), "want": want_final}, case=None if okf else c)
    elif fam == "conc_effect":
        ops = c["input"]["steps"][0]["operations"]
        by_key = {}
        for i, o in enumerate(ops):
            by_key.setdefault(o["idempotency_key"], []).append(i)
        ok = True
        eff = {}
        for k, idxs in by_key.items():
            got = [results[i] for i in idxs]
            if _k(got) not in effect_key_outcomes(ops, idxs):
                ok = False
            eff[k] = "unknown_reconcile" if any(r == {"result": "crashed"} for r in got) else "completed"
        res.check("transition.concurrent_effects_equal_a_serialization_per_key", pname, ok, cid,
                  {"got": outcome_sig(out)}, case=None if ok else c)
        okf = isinstance(out.get("final"), dict) and out["final"].get("effects") == eff and \
            out["final"].get("revision") == c["input"]["initial"]["revision"]
        res.check("transition.concurrent_effects_final_state", pname, okf, cid, {"got": out.get("final"), "effects": eff},
                  case=None if okf else c)
    elif fam == "conc_mixed":
        ops = c["input"]["steps"][0]["operations"]
        candidates = []
        for sid, so in serial_outs.items():
            if not sid.startswith(cid + "-perm-") or not isinstance(so, dict) or "steps" not in so:
                continue
            perm = [int(x) for x in sid.rsplit("-", 1)[1]]
            reordered = [None] * len(ops)
            for pos, j in enumerate(perm):
                reordered[j] = so["steps"][pos]
            candidates.append((reordered, so["final"]))
        ok = any(results == r and out.get("final") == f for r, f in candidates)
        res.check("transition.concurrent_mixed_equals_some_serial_order", pname, ok, cid,
                  {"got": out, "n_serial_orders": len(candidates)}, case=None if ok else c)


def extra_report(res):
    dist = getattr(res, "concurrency_distribution", {})
    agg = {k: v for k, v in dist.items() if k.count(":") == 2 and not k.startswith("tr")}
    per_group = {k: v for k, v in dist.items() if k not in agg}
    return {"concurrency_outcomes_by_family": agg,
            "concurrency_groups_with_multiple_outcomes": {k: v for k, v in per_group.items() if len(v) > 1}}
