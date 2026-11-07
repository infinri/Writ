"""Campaign `acceptance`: Acceptance Gate Integrity (`check-acceptance`).

Generator: a random valid (record, current, requirements) triple, then zero or
more independent mutations, each tagged with the check in SEMANTICS.md
("Checks, in this order; the first failure is the result") that it alone
violates. The oracle is that table and the stated order:
- the unmutated triple returns {"ok": true};
- a single mutation of a bound field returns exactly its tagged refusal;
- several independent mutations return the refusal of the earliest check
  (criteria ordered by criterion_id, then sub-check);
- mutations of fields the spec documents as unbound (advisory criteria, record
  id when not consumed, criterion order, unrelated revocations or consumed ids,
  `issued_at == now`) keep {"ok": true};
- key order, whitespace and escape spelling (input_text) never change the result.
"""
from __future__ import annotations

from itertools import combinations

from .common import Results, clone, differential, digest, dump_variant, run_both

NAME = "acceptance"
CLASSES = {"mechanical": "M", "evidence": "E", "human": "H"}
ORDER = {
    "CURRENT_STATE_MALFORMED": 1, "REQUIREMENTS_MALFORMED": 2, "REQUIREMENTS_POLICY_MISMATCH": 3,
    "ACCEPTANCE_RECORD_MALFORMED": 4, "ACCEPTANCE_RECORD_INVALID": 5, "ACCEPTANCE_REPLAYED": 6,
    "ACCEPTANCE_SCOPE_MISMATCH": 7, "ACCEPTANCE_TRANSITION_MISMATCH": 8, "ACCEPTANCE_ARTIFACT_MISMATCH": 9,
    "ACCEPTANCE_POLICY_MISMATCH": 10, "ACCEPTANCE_REVISION_MISMATCH": 11, "ACCEPTANCE_EXPIRED": 12,
}
EPOCH_2026 = 1767225600  # 2026-01-01T00:00:00Z


def ts(sec: int) -> str:
    import time as _t
    return _t.strftime("%Y-%m-%dT%H:%M:%SZ", _t.gmtime(sec))


def ident(rng, first="abcdefghijklmnopqrstuvwxyz", n=(1, 12)):
    rest = "abcdefghijklmnopqrstuvwxyz0123456789_-"
    return rng.choice(first) + "".join(rng.choice(rest) for _ in range(rng.randint(n[0] - 1, n[1] - 1)))


def idn(rng):  # tenant/project/task/record/snapshot/policy id pattern ^[a-z0-9][a-z0-9_-]{0,63}$
    return ident(rng, "abcdefghijklmnopqrstuvwxyz0123456789")


def sha(rng):
    return "sha256:" + "".join(rng.choice("0123456789abcdef") for _ in range(64))


def make_triple(rng):
    now = EPOCH_2026 + rng.randint(0, 300 * 86400)
    issued = now - rng.choice([0, 1, rng.randint(2, 7200)])
    expires = now + rng.randint(2, 7200)
    tenant, project, task, wf = idn(rng), idn(rng), idn(rng), ident(rng)
    frm = ident(rng)
    to = ident(rng)
    while to == frm:
        to = ident(rng)
    snap, adig = idn(rng), sha(rng)
    pol_id, pol_ver, pol_sha = idn(rng), str(rng.randint(0, 999999999)), sha(rng)
    rev = rng.choice([0, 1, rng.randint(2, 10**6), 2**53 - 1])
    nreq = rng.randint(1, 5)
    cids = set()
    while len(cids) < nreq:
        cids.add(ident(rng))
    req_crit = [{"criterion_id": c, "required_class": rng.choice(list(CLASSES))} for c in sorted(cids)]
    rng.shuffle(req_crit)
    evals = set()
    rec_crit = []
    for rc in req_crit:
        ev = ident(rng)
        while ev in evals:
            ev = ident(rng)
        evals.add(ev)
        rec_crit.append({"criterion_id": rc["criterion_id"], "mandatory": True, "required_class": rc["required_class"],
                         "status": "pass", "evaluator_id": ev, "evaluator_trust": CLASSES[rc["required_class"]],
                         "fresh_until": ts(now + rng.randint(1, 7200))})
    for _ in range(rng.randint(0, 2)):  # advisory criteria: shape only
        c = ident(rng)
        if c in cids:
            continue
        cids.add(c)
        cls = rng.choice(["model_assisted", "model_assisted", "mechanical", "evidence", "human"])
        rec_crit.append({"criterion_id": c, "mandatory": False, "required_class": cls,
                         "status": rng.choice(["pass", "fail", "unknown", "stale", "waived"]),
                         "evaluator_id": ident(rng), "evaluator_trust": rng.choice(["U", "MA", "M", "E", "H"]),
                         "fresh_until": ts(now + rng.randint(-7200, 7200))})
    rng.shuffle(rec_crit)
    record = {"contract_version": "m1-draft-3", "acceptance_record_id": idn(rng), "tenant_id": tenant,
              "project_id": project, "task_id": task, "workflow_id": wf,
              "transition": {"from_state": frm, "to_state": to}, "artifact_snapshot_id": snap,
              "artifact_digest": adig, "acceptance_policy_id": pol_id, "acceptance_policy_version": pol_ver,
              "acceptance_policy_bundle_sha": pol_sha, "state_revision": rev, "status": "accepted",
              "issued_at": ts(issued), "expires_at": ts(expires), "criteria": rec_crit}
    current = {"tenant_id": tenant, "project_id": project, "task_id": task, "workflow_id": wf,
               "transition": {"from_state": frm, "to_state": to}, "artifact_snapshot_id": snap,
               "artifact_digest": adig, "acceptance_policy_id": pol_id, "acceptance_policy_version": pol_ver,
               "acceptance_policy_bundle_sha": pol_sha, "state_revision": rev, "now": ts(now),
               "consumed_record_ids": [idn(rng) for _ in range(rng.randint(0, 3))],
               "revoked_evaluator_ids": [ident(rng) for _ in range(rng.randint(0, 3))]}
    current["consumed_record_ids"] = [x for x in current["consumed_record_ids"] if x != record["acceptance_record_id"]]
    current["revoked_evaluator_ids"] = [x for x in current["revoked_evaluator_ids"] if x not in evals]
    requirements = {"acceptance_policy_id": pol_id, "acceptance_policy_version": pol_ver,
                    "acceptance_policy_bundle_sha": pol_sha, "criteria": req_crit}
    ctx = {"now": now, "issued": issued, "expires": expires}
    return {"record": record, "current": current, "requirements": requirements}, ctx


def other(rng, old, gen):
    v = gen(rng)
    while v == old:
        v = gen(rng)
    return v


def key_of(code, cid=None, sub=0, extra=False):
    if code in ORDER:
        return (ORDER[code], 0, "", 0)
    if code in ("ACCEPTANCE_CRITERIA_MISSING", "ACCEPTANCE_CRITERIA_MISMATCH") and not extra and sub == 0:
        return (13, 0, cid, 0)
    if extra:
        return (13, 1, "", 0)
    if sub:
        return (14, 0, cid, sub)
    if code == "STATUS":
        return (15, 0, "", 0)
    raise ValueError(code)


def required(t):
    return [c for c in t["record"]["criteria"] if c["mandatory"] and
            c["criterion_id"] in {r["criterion_id"] for r in t["requirements"]["criteria"]}]


def advisory(t):
    req = {r["criterion_id"] for r in t["requirements"]["criteria"]}
    return [c for c in t["record"]["criteria"] if c["criterion_id"] not in req]


# Each mutation: (name, slots(t)->set, apply(rng, t, ctx)-> (code, key) or None if not applicable)
# code "ok" means the field is documented as unbound.

def m_current_malformed(rng, t, ctx):
    c = t["current"]
    choice = rng.choice(["extra", "missing", "bad_now", "now_type", "neg_rev", "consumed_type", "upper_tenant",
                         "no_to_state", "bad_digest", "feb30"])
    if choice == "extra":
        c["extra_" + ident(rng)] = 1
    elif choice == "missing":
        del c[rng.choice(list(c))]
    elif choice == "bad_now":
        c["now"] = c["now"].replace("Z", "+00:00")
    elif choice == "now_type":
        c["now"] = ctx["now"]
    elif choice == "neg_rev":
        c["state_revision"] = -1 - rng.randint(0, 5)
    elif choice == "consumed_type":
        c["consumed_record_ids"] = "x"
    elif choice == "upper_tenant":
        c["tenant_id"] = c["tenant_id"].upper() + "X"
    elif choice == "no_to_state":
        del c["transition"]["to_state"]
    elif choice == "bad_digest":
        c["artifact_digest"] = c["artifact_digest"].upper()
    else:
        c["now"] = "2026-02-30T10:00:00Z"
    return "CURRENT_STATE_MALFORMED", key_of("CURRENT_STATE_MALFORMED"), choice


def m_requirements_malformed(rng, t, ctx):
    r = t["requirements"]
    choice = rng.choice(["model_assisted", "empty", "dup_ids", "extra", "version_pattern", "bad_class", "missing"])
    if choice == "model_assisted":
        rng.choice(r["criteria"])["required_class"] = "model_assisted"
    elif choice == "empty":
        r["criteria"] = []
    elif choice == "dup_ids":
        r["criteria"].append(clone(rng.choice(r["criteria"])))
    elif choice == "extra":
        r["extra"] = True
    elif choice == "version_pattern":
        r["acceptance_policy_version"] = "v" + r["acceptance_policy_version"]
    elif choice == "bad_class":
        rng.choice(r["criteria"])["required_class"] = "Mechanical"
    else:
        del r[rng.choice(list(r))]
    return "REQUIREMENTS_MALFORMED", key_of("REQUIREMENTS_MALFORMED"), choice


def m_req_policy(rng, t, ctx):
    side = rng.choice(["requirements", "current"])
    f = rng.choice(["acceptance_policy_id", "acceptance_policy_version", "acceptance_policy_bundle_sha"])
    gen = {"acceptance_policy_id": idn, "acceptance_policy_version": lambda r: str(r.randint(0, 999999999)),
           "acceptance_policy_bundle_sha": sha}[f]
    t[side][f] = other(rng, t[side][f], gen)
    return "REQUIREMENTS_POLICY_MISMATCH", key_of("REQUIREMENTS_POLICY_MISMATCH"), f"{side}.{f}"


def m_record_malformed(rng, t, ctx):
    r = t["record"]
    choice = rng.choice(["extra", "missing", "contract_version", "status_enum", "trust_enum", "ma_mandatory",
                         "criteria_empty", "dup_criteria", "ts_feb30", "ts_hour24", "ts_lower_z", "ts_offset",
                         "upper_id", "neg_revision", "crit_extra_field", "mandatory_type", "bad_digest_pattern"])
    if choice == "extra":
        r["extra_" + ident(rng)] = "x"
    elif choice == "missing":
        del r[rng.choice([k for k in r if k != "record_digest"])]
    elif choice == "contract_version":
        r["contract_version"] = rng.choice(["m1-draft-2", "m1-draft-4", "M1-DRAFT-3", ""])
    elif choice == "status_enum":
        r["status"] = rng.choice(["Accepted", "ok", "pass", ""])
    elif choice == "trust_enum":
        rng.choice(r["criteria"])["evaluator_trust"] = rng.choice(["X", "m", "HUMAN", ""])
    elif choice == "ma_mandatory":
        r["criteria"].append({"criterion_id": "zz-ma-" + ident(rng), "mandatory": True, "required_class": "model_assisted",
                              "status": "pass", "evaluator_id": ident(rng), "evaluator_trust": "MA",
                              "fresh_until": ts(ctx["now"] + 60)})
    elif choice == "criteria_empty":
        r["criteria"] = []
    elif choice == "dup_criteria":
        r["criteria"].append(clone(rng.choice(r["criteria"])))
    elif choice == "ts_feb30":
        r["issued_at"] = "2026-02-30T00:00:00Z"
    elif choice == "ts_hour24":
        r["expires_at"] = r["expires_at"][:11] + "24:00:00Z"
    elif choice == "ts_lower_z":
        r["expires_at"] = r["expires_at"][:-1] + "z"
    elif choice == "ts_offset":
        r["issued_at"] = r["issued_at"][:-1] + "+00:00"
    elif choice == "upper_id":
        r["acceptance_record_id"] = "A" + r["acceptance_record_id"]
    elif choice == "neg_revision":
        r["state_revision"] = -1
    elif choice == "crit_extra_field":
        rng.choice(r["criteria"])["note"] = "x"
    elif choice == "mandatory_type":
        rng.choice(r["criteria"])["mandatory"] = "true"
    else:
        r["artifact_digest"] = "sha256:" + "g" * 64
    return "ACCEPTANCE_RECORD_MALFORMED", key_of("ACCEPTANCE_RECORD_MALFORMED"), choice


def m_digest(rng, t, ctx):
    ctx["corrupt_digest"] = True
    return "ACCEPTANCE_RECORD_INVALID", key_of("ACCEPTANCE_RECORD_INVALID"), "record_digest"


def m_validity(rng, t, ctx):
    r = t["record"]
    choice = rng.choice(["expires_eq_issued", "expires_before_issued", "issued_after_now"])
    if choice == "expires_eq_issued":
        r["expires_at"] = r["issued_at"]
    elif choice == "expires_before_issued":
        r["expires_at"] = ts(ctx["issued"] - rng.randint(1, 3600))
    else:
        r["issued_at"] = ts(ctx["now"] + 1)
        if ctx["expires"] <= ctx["now"] + 1:
            return None
    return "ACCEPTANCE_RECORD_INVALID", key_of("ACCEPTANCE_RECORD_INVALID"), choice


def m_replay(rng, t, ctx):
    t["current"]["consumed_record_ids"].insert(rng.randint(0, len(t["current"]["consumed_record_ids"])),
                                               t["record"]["acceptance_record_id"])
    return "ACCEPTANCE_REPLAYED", key_of("ACCEPTANCE_REPLAYED"), "consumed"


def _field_mut(fields, code, gens):
    def apply(rng, t, ctx):
        side = rng.choice(["record", "current"])
        f = rng.choice(fields)
        gen = gens[f]
        if f in ("from_state", "to_state"):
            tr = t[side]["transition"]
            new = other(rng, tr[f], gen)
            otherf = "to_state" if f == "from_state" else "from_state"
            if new == tr[otherf]:
                return None
            tr[f] = new
        else:
            t[side][f] = other(rng, t[side][f], gen)
        return code, key_of(code), f"{side}.{f}"
    return apply


m_scope = _field_mut(["tenant_id", "project_id", "task_id", "workflow_id"], "ACCEPTANCE_SCOPE_MISMATCH",
                     {"tenant_id": idn, "project_id": idn, "task_id": idn, "workflow_id": ident})
m_transition = _field_mut(["from_state", "to_state"], "ACCEPTANCE_TRANSITION_MISMATCH",
                          {"from_state": ident, "to_state": ident})
m_artifact = _field_mut(["artifact_snapshot_id", "artifact_digest"], "ACCEPTANCE_ARTIFACT_MISMATCH",
                        {"artifact_snapshot_id": idn, "artifact_digest": sha})


def m_record_policy(rng, t, ctx):
    f = rng.choice(["acceptance_policy_id", "acceptance_policy_version", "acceptance_policy_bundle_sha"])
    gen = {"acceptance_policy_id": idn, "acceptance_policy_version": lambda r: str(r.randint(0, 999999999)),
           "acceptance_policy_bundle_sha": sha}[f]
    t["record"][f] = other(rng, t["record"][f], gen)
    return "ACCEPTANCE_POLICY_MISMATCH", key_of("ACCEPTANCE_POLICY_MISMATCH"), f"record.{f}"


def m_revision(rng, t, ctx):
    side = rng.choice(["record", "current"])
    old = t[side]["state_revision"]
    t[side]["state_revision"] = rng.choice([old + 1 if old < 2**53 - 1 else old - 1, max(0, old - 1) if old else 1,
                                            rng.randint(0, 2**53 - 1)])
    if t[side]["state_revision"] == old:
        return None
    return "ACCEPTANCE_REVISION_MISMATCH", key_of("ACCEPTANCE_REVISION_MISMATCH"), side


def m_expired(rng, t, ctx):
    choice = rng.choice(["now_at_expiry", "now_after_expiry", "expires_before_now"])
    if choice == "expires_before_now":
        if ctx["now"] - ctx["issued"] < 1:
            return None
        t["record"]["expires_at"] = ts(rng.randint(ctx["issued"] + 1, ctx["now"]))
    else:
        t["current"]["now"] = ts(ctx["expires"] + (0 if choice == "now_at_expiry" else rng.randint(1, 86400)))
    return "ACCEPTANCE_EXPIRED", key_of("ACCEPTANCE_EXPIRED"), choice


def _req_pick(rng, t):
    req = required(t)
    return rng.choice(req) if req else None


def m_crit_missing(rng, t, ctx):
    c = _req_pick(rng, t) if rng.random() < 0.5 else None
    if c is not None:
        t["record"]["criteria"].remove(c)
        if not t["record"]["criteria"]:
            return None
        cid = c["criterion_id"]
        how = "removed_from_record"
    else:
        cid = ident(rng)
        if cid in {c["criterion_id"] for c in t["record"]["criteria"]}:
            return None
        t["requirements"]["criteria"].append({"criterion_id": cid, "required_class": rng.choice(list(CLASSES))})
        how = "added_to_requirements"
    ctx.setdefault("touched_crit", set()).add(cid)
    return "ACCEPTANCE_CRITERIA_MISSING", key_of("ACCEPTANCE_CRITERIA_MISSING", cid), how


def m_crit_mismatch(rng, t, ctx):
    choice = rng.choice(["not_mandatory", "reclassed", "extra_mandatory"])
    if choice == "extra_mandatory":
        cid = ident(rng)
        if cid in {c["criterion_id"] for c in t["record"]["criteria"]} | \
                {c["criterion_id"] for c in t["requirements"]["criteria"]}:
            return None
        cls = rng.choice(list(CLASSES))
        t["record"]["criteria"].append({"criterion_id": cid, "mandatory": True, "required_class": cls, "status": "pass",
                                        "evaluator_id": ident(rng), "evaluator_trust": CLASSES[cls],
                                        "fresh_until": ts(ctx["now"] + 600)})
        return "ACCEPTANCE_CRITERIA_MISMATCH", key_of("ACCEPTANCE_CRITERIA_MISMATCH", extra=True), choice
    c = _req_pick(rng, t)
    if c is None:
        return None
    if choice == "not_mandatory":
        c["mandatory"] = False
    else:
        new = other(rng, c["required_class"], lambda r: r.choice(list(CLASSES)))
        c["required_class"] = new
        c["evaluator_trust"] = CLASSES[new] if rng.random() < 0.5 else c["evaluator_trust"]
    ctx.setdefault("touched_crit", set()).add(c["criterion_id"])
    return "ACCEPTANCE_CRITERIA_MISMATCH", key_of("ACCEPTANCE_CRITERIA_MISMATCH", c["criterion_id"]), choice


def m_trust(rng, t, ctx):
    c = _req_pick(rng, t)
    if c is None:
        return None
    c["evaluator_trust"] = other(rng, c["evaluator_trust"], lambda r: r.choice(["U", "MA", "M", "E", "H"]))
    ctx.setdefault("touched_crit", set()).add(c["criterion_id"])
    return "ACCEPTANCE_UNTRUSTED_EVALUATOR", key_of("x", c["criterion_id"], 1), c["evaluator_trust"]


def m_revoked(rng, t, ctx):
    c = _req_pick(rng, t)
    if c is None:
        return None
    t["current"]["revoked_evaluator_ids"].append(c["evaluator_id"])
    ctx.setdefault("touched_crit", set()).add(c["criterion_id"])
    return "ACCEPTANCE_EVALUATOR_REVOKED", key_of("x", c["criterion_id"], 2), c["evaluator_id"]


def m_stale(rng, t, ctx):
    c = _req_pick(rng, t)
    if c is None:
        return None
    if rng.random() < 0.5:
        c["status"] = "stale"
        how = "status_stale"
    else:
        c["fresh_until"] = ts(ctx["now"] - rng.choice([0, 1, rng.randint(2, 86400)]))
        how = "fresh_until_not_after_now"
    ctx.setdefault("touched_crit", set()).add(c["criterion_id"])
    return "ACCEPTANCE_STALE", key_of("x", c["criterion_id"], 3), how


def m_incomplete(rng, t, ctx):
    c = _req_pick(rng, t)
    if c is None:
        return None
    c["status"] = rng.choice(["fail", "unknown", "waived"])
    ctx.setdefault("touched_crit", set()).add(c["criterion_id"])
    return "ACCEPTANCE_INCOMPLETE", key_of("x", c["criterion_id"], 4), c["status"]


def m_status(rng, t, ctx):
    t["record"]["status"] = rng.choice(["rejected", "incomplete", "needs_human_review"])
    return "ACCEPTANCE_RECORD_INVALID", key_of("STATUS"), t["record"]["status"]


# --- unbound fields (documented as not part of the binding): result stays ok
def u_record_id(rng, t, ctx):
    t["record"]["acceptance_record_id"] = other(rng, t["record"]["acceptance_record_id"], idn)
    if t["record"]["acceptance_record_id"] in t["current"]["consumed_record_ids"]:
        return None
    return "ok", None, "record_id_changed_not_consumed"


def u_advisory(rng, t, ctx):
    adv = advisory(t)
    if not adv:
        return None
    c = rng.choice(adv)
    f = rng.choice(["status", "evaluator_trust", "fresh_until", "evaluator_id"])
    c[f] = {"status": lambda: rng.choice(["pass", "fail", "unknown", "stale", "waived"]),
            "evaluator_trust": lambda: rng.choice(["U", "MA", "M", "E", "H"]),
            "fresh_until": lambda: ts(ctx["now"] - rng.randint(0, 9999)),
            "evaluator_id": lambda: ident(rng)}[f]()
    if f == "evaluator_id" and rng.random() < 0.5:
        t["current"]["revoked_evaluator_ids"].append(c["evaluator_id"])
    return "ok", None, f"advisory.{f}"


def u_order(rng, t, ctx):
    rng.shuffle(t["record"]["criteria"])
    rng.shuffle(t["requirements"]["criteria"])
    return "ok", None, "criteria_order"


def u_unrelated(rng, t, ctx):
    used = {c["evaluator_id"] for c in required(t)}
    ev = ident(rng)
    if ev in used:
        return None
    t["current"]["revoked_evaluator_ids"].append(ev)
    rid = idn(rng)
    if rid != t["record"]["acceptance_record_id"]:
        t["current"]["consumed_record_ids"].append(rid)
    return "ok", None, "unrelated_revocation_and_consumption"


def u_issued_now(rng, t, ctx):
    t["record"]["issued_at"] = t["current"]["now"]
    return "ok", None, "issued_at_equals_now"


MUTATIONS = [
    # name, slot, fn, weight
    ("current_malformed", "current.shape", m_current_malformed, 3),
    ("requirements_malformed", "requirements.shape", m_requirements_malformed, 3),
    ("requirements_policy", "req_policy", m_req_policy, 3),
    ("record_malformed", "record.shape", m_record_malformed, 5),
    ("record_digest", "digest", m_digest, 3),
    ("record_validity", "time", m_validity, 3),
    ("replay", "record.id", m_replay, 3),
    ("scope", "scope", m_scope, 4),
    ("transition", "transition", m_transition, 4),
    ("artifact", "artifact", m_artifact, 4),
    ("record_policy", "record.policy", m_record_policy, 3),
    ("revision", "revision", m_revision, 3),
    ("expired", "time", m_expired, 3),
    ("criteria_missing", "crit", m_crit_missing, 4),
    ("criteria_mismatch", "crit", m_crit_mismatch, 4),
    ("untrusted", "crit", m_trust, 4),
    ("revoked", "crit", m_revoked, 3),
    ("stale", "crit", m_stale, 4),
    ("incomplete", "crit", m_incomplete, 4),
    ("record_status", "record.status", m_status, 3),
    ("u_record_id", "record.id", u_record_id, 2),
    ("u_advisory", "advisory", u_advisory, 2),
    ("u_order", "order", u_order, 1),
    ("u_unrelated", "unrelated", u_unrelated, 1),
    ("u_issued_now", "time", u_issued_now, 1),
]
CODE_FOR_KEY = {}


def expected_code(code_keys):
    """Earliest failing check among (code, key) pairs; 'ok' if none fails."""
    failing = [(k, c) for c, k in code_keys if c != "ok"]
    if not failing:
        return "ok"
    return min(failing)[1]


def gen_cases(rng, n):
    cases = []
    names = [m[0] for m in MUTATIONS]
    weights = [m[3] for m in MUTATIONS]
    by_name = {m[0]: m for m in MUTATIONS}
    i = 0
    while len(cases) < n:
        i += 1
        t, ctx = make_triple(rng)
        r = rng.random()
        k = 0 if r < 0.08 else (1 if r < 0.6 else rng.randint(2, 4))
        picks, slots, tries = [], set(), 0
        while len(picks) < k and tries < 30:
            tries += 1
            name = rng.choices(names, weights)[0]
            slot = by_name[name][1]
            if slot in slots and slot != "crit":
                continue
            picks.append(name)
            slots.add(slot)
        # shape (malformation) mutations last, so no later mutation repairs them
        picks.sort(key=lambda nm: by_name[nm][1].endswith(".shape"))
        chosen, applied = [], []
        for name in picks:
            _, slot, fn, _ = by_name[name]
            snap = clone(t)
            sctx = dict(ctx, touched_crit=set(ctx.get("touched_crit", set())))
            before = len(ctx.get("touched_crit", ()))
            try:
                out = fn(rng, t, ctx)
            except (KeyError, TypeError, IndexError, AttributeError, ValueError):
                out = None  # an earlier mutation removed what this one needs
            # several criterion mutations are independent only on distinct criteria
            if out is not None and slot == "crit" and len(ctx.get("touched_crit", ())) == before \
                    and out[1][1] == 0 and sum(by_name[c][1] == "crit" for c in chosen):
                out = None
            if out is None:
                t.update(snap)
                ctx.clear()
                ctx.update(sctx)
                continue
            chosen.append(name)
            applied.append(out)
        if len(chosen) < k:
            continue
        rec = t["record"]
        rec.pop("record_digest", None)
        try:
            rec["record_digest"] = digest(rec)
        except (TypeError, ValueError):
            continue
        if ctx.get("corrupt_digest"):
            d = rec["record_digest"]
            pos = rng.randrange(7, len(d))
            rec["record_digest"] = d[:pos] + ("0" if d[pos] != "0" else "1") + d[pos + 1:]
        exp = expected_code([(c, key) for c, key, _ in applied])
        case = {"id": f"acc-{len(cases):06d}-{'+'.join(chosen) or 'valid'}", "command": "check-acceptance",
                "input": t, "meta": {"mutations": [{"name": nm, "code": c, "detail": d}
                                                   for nm, (c, _, d) in zip(chosen, applied)],
                                     "expect": exp, "n_mutations": len(chosen)}}
        cases.append(case)
        if rng.random() < 0.15:  # metamorphic twin: same triple as raw text with permuted keys/whitespace/escapes
            cases.append({"id": case["id"] + "-text", "command": "check-acceptance",
                          "input_text": dump_variant(t, rng, p_ws=0.3, p_escape=0.2),
                          "meta": dict(case["meta"], twin_of=case["id"])})
    return cases


def run(protos, rng, n, res: Results):
    cases = gen_cases(rng, n)
    outs = run_both(protos, "check-acceptance", cases, res)
    for p in protos:
        o = outs[p.name]
        for c in cases:
            out = o.get(c["id"])
            exp = c["meta"]["expect"]
            want = {"ok": True} if exp == "ok" else {"refusal": exp}
            nm = c["meta"]["n_mutations"]
            muts = c["meta"]["mutations"]
            unbound_only = all(m["code"] == "ok" for m in muts)
            if nm == 0:
                inv = "acceptance.valid_triple_ok"
            elif unbound_only:
                inv = "acceptance.unbound_field_mutation_keeps_ok"
            elif nm == 1:
                inv = "acceptance.single_bound_mutation_exact_refusal"
            else:
                inv = "acceptance.multi_mutation_earliest_check"
            res.check(inv, p.name, out == want, c["id"], {"got": out, "want": want, "mutations": muts},
                      case=None if out == want else c)
            if exp != "ok":
                res.check("acceptance.bound_mutation_never_ok", p.name, out != {"ok": True}, c["id"], out,
                          case=None if out != {"ok": True} else c)
            if "twin_of" in c["meta"]:
                res.check("acceptance.text_permutation_invariance", p.name, out == o.get(c["meta"]["twin_of"]),
                          c["id"], {"text": out, "value": o.get(c["meta"]["twin_of"])})
    differential(protos, outs, cases, res)
    return cases, outs
