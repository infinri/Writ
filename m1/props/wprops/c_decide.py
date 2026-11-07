"""Campaign `decide`: kernel authorization (command `decide`).

Two oracle families, both stated in SCENARIOS.md "Kernel: decisions" and D-008:

1. Evaluation-order oracle. A clean request (trusted issuer, matching revision,
   known capabilities, canonical in-scope resource, well-formed transition with
   the binding exactly when the bundle's protection classification requires it)
   gets zero or more injected faults, each tagged with the check it alone fails.
   The decision must be the (status, reason_code) of the earliest check in the
   documented order: schema; requested-capabilities redundancy; deadline; policy
   availability; bundle rejected at load; pin; issuer; state revision;
   capability knowledge; canonical form; scope; transition support; resource
   shape; attributes; composite; protection. Pre-statement decisions match no rules.

2. Metamorphic relations on clean requests that reach the statements:
   adding a matching forbid never yields allow or require_approval; removing
   every statement that mentions the request's capabilities yields
   deny/DEFAULT_DENY; a cross-tenant or cross-project resource yields
   deny/RESOURCE_SCOPE_MISMATCH and the same output for an existing and a
   nonexistent foreign tenant; statement order never changes the status; key
   order and whitespace never change the output; pinning the loaded bundle's
   own sha changes nothing; an extra role never cancels a matched forbid;
   approval request ids differ when to_state or from_state differ.

Bundle load (D-019 N-4, D-020): a generated bundle is labelled defective when it
declares transition support without a valid workflow_protection, duplicates or
self-edges a protected state or edge, has an unscoped permit/approval transition
statement, or has a protected edge reachable by an unprotected permitted route
(small BFS below). Defective bundles must give unavailable/POLICY_BUNDLE_REJECTED.
"""
from __future__ import annotations

import json
from pathlib import Path

from .common import Results, clone, differential, dump_variant, run_both

NAME = "decide"
FIX = Path(__file__).resolve().parents[2] / "fixtures"
STATES = ["draft", "planning", "verifying", "reviewing", "accepted", "deployed", "released"]
TENANTS = ["t1", "t2", "fixture-tenant"]
PROJECTS = ["p1", "p2", "fixture-project"]
GHOST_TENANT = "t-ghost-0"
ISSUER = "writ-host-adapter:fixture"
ROLES = ["agent", "operator", "release-manager", "reviewer"]
KINDS = ["file.read", "file.write", "memory.write", "network.egress", "process.execute", "repository.publish",
         "agent.dispatch"]
SCHEME = {"file.read": "workspace", "file.write": "workspace", "memory.write": "memory", "network.egress": "network",
          "process.execute": "workspace", "repository.publish": "repository", "agent.dispatch": "agent",
          "workflow.transition": "workflow"}
BINDING = ("acceptance_record_id", "artifact_snapshot_id", "artifact_digest")
NOW = "2026-11-07T11:00:00Z"
PRE_STATEMENT = {
    "REQUEST_JSON_INVALID", "REQUEST_SCHEMA_INVALID", "REQUESTED_CAPABILITIES_INVALID", "DEADLINE_EXCEEDED",
    "POLICY_UNAVAILABLE", "POLICY_BUNDLE_REJECTED", "POLICY_VERSION_MISMATCH", "PRINCIPAL_UNVERIFIED",
    "STATE_REVISION_CONFLICT", "UNKNOWN_CAPABILITY", "RESOURCE_NOT_CANONICAL", "RESOURCE_SCOPE_MISMATCH",
    "UNSUPPORTED_CAPABILITY", "WORKFLOW_RESOURCE_INVALID", "TRANSITION_MALFORMED", "COMPOSITE_NOT_SUPPORTED",
    "ACCEPTANCE_BINDING_REQUIRED", "ACCEPTANCE_BINDING_UNEXPECTED"}
FAULT_ORDER = [  # (fault name, order, status, reason)
    ("schema", 1, "invalid", "REQUEST_SCHEMA_INVALID"),
    ("redundant", 2, "invalid", "REQUESTED_CAPABILITIES_INVALID"),
    ("deadline", 3, "invalid", "DEADLINE_EXCEEDED"),
    ("nobundle", 4, "unavailable", "POLICY_UNAVAILABLE"),
    ("badbundle", 5, "unavailable", "POLICY_BUNDLE_REJECTED"),
    ("pin", 6, "unavailable", "POLICY_VERSION_MISMATCH"),
    ("issuer", 7, "deny", "PRINCIPAL_UNVERIFIED"),
    ("revision", 8, "invalid", "STATE_REVISION_CONFLICT"),
    ("unknown_cap", 9, "deny", "UNKNOWN_CAPABILITY"),
    ("unknown_transition", 9, "deny", "UNKNOWN_CAPABILITY"),
    ("noncanonical", 10, "invalid", "RESOURCE_NOT_CANONICAL"),
    ("scope", 11, "deny", "RESOURCE_SCOPE_MISMATCH"),
    ("unsupported", 12, "deny", "UNSUPPORTED_CAPABILITY"),
    ("wfres", 13, "invalid", "WORKFLOW_RESOURCE_INVALID"),
    ("attr", 14, "invalid", "TRANSITION_MALFORMED"),
    ("composite", 15, "invalid", "COMPOSITE_NOT_SUPPORTED"),
    ("binding", 16, "invalid", None),  # REQUIRED or UNEXPECTED, set at injection
]
FAULTS = {f[0]: f for f in FAULT_ORDER}
APPLY_ORDER = ["nobundle", "badbundle", "unsupported", "unknown_transition", "wfres", "scope", "attr", "binding",
               "composite", "unknown_cap", "redundant", "noncanonical", "deadline", "pin", "issuer", "revision",
               "schema"]
BUNDLE_FAULTS = {"nobundle", "badbundle", "unsupported", "unknown_transition"}
TRANSITION_ONLY = {"unsupported", "unknown_transition", "wfres", "attr", "composite", "binding"}


def load_bundles():
    ref = json.loads((FIX / "sets" / "golden-decisions.jsonl").read_text().splitlines()[0])["input"]["bundle"]
    tx = json.loads((FIX / "transition" / "m1-transition.bundle.json").read_text())
    return ref, tx


REF_BUNDLE, TX_BUNDLE = load_bundles()


def sha(rng):
    return "sha256:" + "".join(rng.choice("0123456789abcdef") for _ in range(64))


# ------------------------------------------------------------ bundle helpers

def covers(st, kind):
    return kind in st["action_kinds"]


def supports_transitions(b):
    return any(covers(s, "workflow.transition") for s in b["statements"]) or "workflow_protection" in b


def is_protected(b, frm, to):
    wp = b.get("workflow_protection") or {}
    return to in wp.get("protected_to_states", []) or \
        {"from_state": frm, "to_state": to} in wp.get("protected_transitions", [])


def bypassable(b) -> bool:
    """D-019 N-4 load rule: a protected edge A->B (B not a protected state) reachable
    from A by a path of statement-permitted/approvable transitions that uses no
    protected edge and enters no protected state."""
    wp = b.get("workflow_protection")
    if not wp:
        return False
    pstates = set(wp.get("protected_to_states", []))
    pedges = {(e["from_state"], e["to_state"]) for e in wp.get("protected_transitions", [])}
    named = set(pstates) | {x for e in pedges for x in e}
    for s in b["statements"]:
        named |= set(s.get("from_states", [])) | set(s.get("to_states", []))
    edges = set()
    for s in b["statements"]:
        if s["effect"] == "forbid" or not covers(s, "workflow.transition") or "to_states" not in s:
            continue
        for f in s.get("from_states", sorted(named)):
            for t in s["to_states"]:
                if f != t and (f, t) not in pedges and t not in pstates:
                    edges.add((f, t))
    for a, bb in pedges:
        if bb in pstates:
            continue
        seen, todo = {a}, [a]
        while todo:
            x = todo.pop()
            for f, t in edges:
                if f == x and t not in seen:
                    if t == bb:
                        return True
                    seen.add(t)
                    todo.append(t)
    return False


def defects(b) -> list[str]:
    out = []
    wp = b.get("workflow_protection")
    if supports_transitions(b) and not wp:
        out.append("protection_missing")
    if wp is not None:
        if set(wp) != {"acceptance_policy_bundle_sha", "protected_to_states", "protected_transitions"}:
            out.append("protection_schema")
        ps = wp.get("protected_to_states", [])
        pe = [(e.get("from_state"), e.get("to_state")) for e in wp.get("protected_transitions", [])]
        if len(set(ps)) != len(ps):
            out.append("duplicate_state")
        if len(set(pe)) != len(pe):
            out.append("duplicate_edge")
        if any(f == t for f, t in pe):
            out.append("self_edge")
        if any(not isinstance(x, str) or not x.islower() or " " in x for x in ps):
            out.append("protection_schema")
    for s in b["statements"]:
        if covers(s, "workflow.transition") and s["effect"] != "forbid" and "to_states" not in s:
            out.append("unscoped_transition_statement")
    if not out and bypassable(b):
        out.append("bypassable")
    return out


def random_bundle(rng):
    known = sorted(set(rng.sample(KINDS, rng.randint(2, len(KINDS)))) | {"workflow.transition"})
    stmts = []
    for i in range(rng.randint(2, 8)):
        eff = rng.choices(["forbid", "require_approval", "permit"], [2, 3, 5])[0]
        tr = rng.random() < 0.6
        kinds = ["workflow.transition"] if tr else rng.sample([k for k in known if k != "workflow.transition"], 1)
        s = {"statement_id": f"s{i}-{eff[:4]}", "effect": eff, "action_kinds": kinds, "reason_code": f"R_S{i}_{eff.upper()}"}
        if rng.random() < 0.3:
            s["tenants"] = [rng.choice(TENANTS)]
        if rng.random() < 0.3:
            s["principal_roles"] = rng.sample(ROLES, rng.randint(1, 2))
        if rng.random() < 0.5:
            t, p = rng.choice(TENANTS), rng.choice(PROJECTS)
            s["resource_prefixes"] = [f"{SCHEME[kinds[0]]}:{t}/{p}/" if rng.random() < 0.8 else f"{SCHEME[kinds[0]]}:{t}/"]
        if tr:
            if eff != "forbid" or rng.random() < 0.6:
                s["to_states"] = rng.sample(STATES, rng.randint(1, 2))
            if rng.random() < 0.5:
                s["from_states"] = rng.sample(STATES, rng.randint(1, 3))
        stmts.append(s)
    b = {"bundle_id": f"rand-{rng.randint(0, 10**6)}", "contract_version": "2.0", "description": None,
         "known_capabilities": known, "publisher": "m1-props", "publisher_signature": None,
         "statements": stmts, "trusted_principal_issuers": [ISSUER], "version": "1.0.0"}
    if supports_transitions(b) or rng.random() < 0.2:
        pstates = rng.sample(STATES, rng.randint(0, 3))
        edges = []
        for _ in range(rng.randint(0, 2)):
            f, t = rng.sample([s for s in STATES], 2)
            if t not in pstates and {"from_state": f, "to_state": t} not in edges:
                edges.append({"from_state": f, "to_state": t})
        b["workflow_protection"] = {"acceptance_policy_bundle_sha": sha(rng), "protected_to_states": pstates,
                                    "protected_transitions": edges}
    return b


def inject_bundle_defect(rng, b):
    b = clone(b)
    kinds = ["protection_missing", "duplicate_state", "duplicate_edge", "self_edge", "unscoped", "bypass",
             "protection_schema"]
    d = rng.choice(kinds)
    wp = b.get("workflow_protection")
    tr_stmt = [s for s in b["statements"] if covers(s, "workflow.transition")]
    if d == "protection_missing":
        if not tr_stmt:
            b["statements"].append({"statement_id": "zz-forbid-tr", "effect": "forbid",
                                    "action_kinds": ["workflow.transition"], "reason_code": "R_ZZ"})
        b.pop("workflow_protection", None)
    elif d in ("duplicate_state", "duplicate_edge", "self_edge", "protection_schema", "bypass") and wp is None:
        return None
    elif d == "duplicate_state":
        if not wp["protected_to_states"]:
            wp["protected_to_states"] = ["accepted"]
        wp["protected_to_states"].append(rng.choice(wp["protected_to_states"]))
    elif d == "duplicate_edge":
        if not wp["protected_transitions"]:
            wp["protected_transitions"] = [{"from_state": "zz-a", "to_state": "zz-b"}]
        wp["protected_transitions"].append(dict(rng.choice(wp["protected_transitions"])))
    elif d == "self_edge":
        s = rng.choice(STATES)
        wp["protected_transitions"].append({"from_state": s, "to_state": s})
    elif d == "protection_schema":
        r = rng.random()
        if r < 0.4:
            del wp["acceptance_policy_bundle_sha"]
        elif r < 0.7:
            wp["protected_to_states"].append("Accepted")
        else:
            wp["extra"] = []
    elif d == "unscoped":
        b["statements"].append({"statement_id": "zz-unscoped", "effect": rng.choice(["permit", "require_approval"]),
                                "action_kinds": ["workflow.transition"], "reason_code": "R_UNSCOPED"})
    elif d == "bypass":
        a, mid, bb = "zz-src", "zz-mid", "zz-dst"
        if bb in wp["protected_to_states"]:
            return None
        wp["protected_transitions"].append({"from_state": a, "to_state": bb})
        b["statements"].append({"statement_id": "zz-r1", "effect": "permit", "action_kinds": ["workflow.transition"],
                                "from_states": [a], "to_states": [mid], "reason_code": "R_R1"})
        b["statements"].append({"statement_id": "zz-r2", "effect": rng.choice(["permit", "require_approval"]),
                                "action_kinds": ["workflow.transition"], "from_states": [mid], "to_states": [bb],
                                "reason_code": "R_R2"})
    b["bundle_id"] += "-defect"
    if not defects(b):
        return None
    return b


def no_support_variant(b, known_transition: bool):
    b = clone(b)
    b["statements"] = [s for s in b["statements"] if not covers(s, "workflow.transition")]
    b.pop("workflow_protection", None)
    if not known_transition:
        b["known_capabilities"] = [k for k in b["known_capabilities"] if k != "workflow.transition"]
    return b


# ----------------------------------------------------------- request helpers

def base_request(rng, kind, tenant, project, path, attrs, roles, revision):
    rid = f"req-{rng.randint(0, 10**9)}"
    return {
        "action": {"action_digest": "sha256:" + "b" * 64, "attributes": attrs, "kind": kind,
                   "requested_capabilities": [],
                   "resource": {"canonical_resource_id": f"{SCHEME.get(kind, 'workspace')}:{tenant}/{project}/{path}",
                                "uri": "x://" + path}},
        "agent_id": None, "contract_version": "2.0", "deadline_at": None, "expected_state_revision": revision,
        "idempotency_key": "idem-" + rid, "parent_session_id": None, "policy_bundle_sha": None,
        "principal": {"authn_method": "os_peer_credential", "id": f"agent:session-{rng.randint(1, 3)}",
                      "roles": roles, "verified_by": ISSUER},
        "project_id": project, "request_id": rid, "session_id": "session-1", "tenant_id": tenant,
        "trace_id": "trace-" + rid}


def clean_case(rng):
    """Returns (bundle, state_revision, request, info) for a fault-free request."""
    fam = rng.choices(["ref", "tx", "random"], [2, 3, 5])[0]
    b = clone(REF_BUNDLE if fam == "ref" else TX_BUNDLE if fam == "tx" else random_bundle(rng))
    if fam == "random" and defects(b):
        return None
    tenants = sorted({t for s in b["statements"] for t in s.get("tenants", [])} |
                     {p.split(":")[1].split("/")[0] for s in b["statements"] for p in s.get("resource_prefixes", [])}
                     | set(TENANTS[:2]))
    tenant = rng.choice(tenants)
    project = rng.choice(PROJECTS + [p.split("/")[1] for s in b["statements"] for p in s.get("resource_prefixes", [])
                                     if p.count("/") >= 2])
    roles = sorted(set(rng.sample(ROLES, rng.randint(0, 2))))
    srev = rng.randint(0, 50)
    transition = supports_transitions(b) and rng.random() < 0.7
    if transition:
        frm, to = rng.sample(STATES, 2)
        attrs = {"from_state": frm, "to_state": to}
        if is_protected(b, frm, to):
            attrs.update({"acceptance_record_id": "acc-1", "artifact_snapshot_id": "snap-1",
                          "artifact_digest": "sha256:" + "b" * 64})
        req = base_request(rng, "workflow.transition", tenant, project, rng.choice(["wf-1", "wf-2", "w"]), attrs,
                           roles, srev)
    else:
        kind = rng.choice([k for k in b["known_capabilities"] if k != "workflow.transition"])
        req = base_request(rng, kind, tenant, project, rng.choice(["src/app.py", "main", "secrets/key.txt", "a/b"]), {},
                           roles, rng.choice([None, srev]))
        if rng.random() < 0.25:
            others = [k for k in b["known_capabilities"] if k not in (kind, "workflow.transition")]
            if others:
                req["action"]["requested_capabilities"] = rng.sample(others, 1)
    return b, srev, req, {"family": fam, "transition": transition}


def apply_fault(rng, name, b, srev, req, info, variants):
    a = req["action"]
    caps = a["requested_capabilities"]
    if name == "schema":
        r = rng.random()
        if r < 0.3:
            del req["principal"]
        elif r < 0.5:
            req["contract_version"] = "1.0"
        elif r < 0.7:
            req["expected_state_revision"] = -1 - rng.randint(0, 9)
        elif r < 0.85:
            req["unexpected_field"] = True
        else:
            del a["action_digest"]
        return b, None
    if name == "redundant":
        caps.append(a["kind"])
        return b, None
    if name == "deadline":
        req["deadline_at"] = rng.choice(["2026-11-07T10:59:59Z", "2026-11-07T00:00:00Z", "2025-01-01T00:00:00Z"])
        return b, None
    if name == "nobundle":
        return None, None
    if name == "badbundle":
        bb = inject_bundle_defect(rng, b)
        return (bb, None) if bb is not None else ("skip", None)
    if name == "pin":
        req["policy_bundle_sha"] = sha(rng)
        return b, None
    if name == "issuer":
        if "principal" not in req:
            return "skip", None
        req["principal"]["verified_by"] = rng.choice(["writ-host-adapter:rogue", "WRIT-HOST-ADAPTER:FIXTURE",
                                                     "writ-host-adapter:fixture "])
        return b, None
    if name == "revision":
        req["expected_state_revision"] = srev + rng.randint(1, 5) if rng.random() < 0.7 else max(0, srev - 1) \
            if srev else srev + 1
        return b, None
    if name == "unknown_cap":
        caps.append(rng.choice(["shell.raw", "zz.unknown", "File.write", "file.write "]))
        return b, None
    if name == "unknown_transition":
        return no_support_variant(b, False), None
    if name == "unsupported":
        return no_support_variant(b, True), None
    if name == "noncanonical":
        rid = a["resource"]["canonical_resource_id"]
        scheme, rest = rid.split(":", 1)
        segs = rest.split("/")
        r = rng.random()
        if r < 0.3:
            segs.insert(rng.randint(2, len(segs)), "..")
        elif r < 0.5:
            segs.insert(rng.randint(2, len(segs)), ".")
        elif r < 0.75:
            segs.insert(rng.randint(2, len(segs)), "")
        else:
            segs.append("")
        a["resource"]["canonical_resource_id"] = scheme + ":" + "/".join(segs)
        return b, None
    if name == "scope":
        rid = a["resource"]["canonical_resource_id"]
        scheme, rest = rid.split(":", 1)
        segs = rest.split("/")
        if rng.random() < 0.6:
            segs[0] = rng.choice([t for t in TENANTS + [GHOST_TENANT] if t != req["tenant_id"]])
        else:
            segs[1] = rng.choice([p for p in PROJECTS + ["p-ghost"] if p != req["project_id"]])
        a["resource"]["canonical_resource_id"] = scheme + ":" + "/".join(segs)
        return b, None
    if name == "wfres":
        rid = a["resource"]["canonical_resource_id"]
        r = rng.random()
        if r < 0.4:
            a["resource"]["canonical_resource_id"] = rid + "/" + rng.choice(STATES)
        elif r < 0.7:
            head, wf = rid.rsplit("/", 1)
            a["resource"]["canonical_resource_id"] = head + "/" + wf.upper() + "X"
        else:
            head, wf = rid.rsplit("/", 1)
            a["resource"]["canonical_resource_id"] = head + "/9" + wf
        return b, None
    if name == "attr":
        at = a["attributes"]
        r = rng.choice(["drop_to", "drop_from", "same", "extra", "upper", "list", "partial", "space", "nonstring"])
        if r == "drop_to":
            del at["to_state"]
        elif r == "drop_from":
            del at["from_state"]
        elif r == "same":
            at["to_state"] = at["from_state"]
        elif r == "extra":
            at[rng.choice(["note", "approved", "role", "priority"])] = rng.choice(["x", True, 1])
        elif r == "upper":
            at["to_state"] = at["to_state"].capitalize()
        elif r == "list":
            at["to_state"] = [at["to_state"]]
        elif r == "partial":
            if all(k in at for k in BINDING):
                del at[rng.choice(BINDING)]
            else:
                at[rng.choice(BINDING)] = "x-1"
        elif r == "space":
            at["to_state"] = at["to_state"] + " "
        else:
            if all(k in at for k in BINDING):
                at["acceptance_record_id"] = 7
            else:
                at["from_state"] = 3
        return b, None
    if name == "composite":
        others = [k for k in KINDS if k in (b or {}).get("known_capabilities", KINDS)]
        caps.append(rng.choice(others))
        return b, None
    if name == "binding":
        at = a["attributes"]
        if all(k in at for k in BINDING):
            for k in BINDING:
                del at[k]
            return b, "ACCEPTANCE_BINDING_REQUIRED"
        at.update({"acceptance_record_id": "acc-9", "artifact_snapshot_id": "snap-9", "artifact_digest": "sha256:" + "9" * 64})
        return b, "ACCEPTANCE_BINDING_UNEXPECTED"
    raise ValueError(name)


def gen_fault_case(rng, idx):
    base = clean_case(rng)
    if base is None:
        return None
    b, srev, req, info = base
    applicable = [f for f in FAULTS if info["transition"] or f not in TRANSITION_ONLY]
    k = rng.choices([0, 1, 2, 3], [2, 5, 3, 1])[0]
    picks = []
    while len(picks) < k:
        f = rng.choice(applicable)
        if f in picks or (f in BUNDLE_FAULTS and BUNDLE_FAULTS & set(picks)):
            continue
        if f == "binding" and "attr" in picks:  # attribute damage changes what protection sees
            continue
        if f == "attr" and "binding" in picks:
            continue
        picks.append(f)
    # application order chosen so that no fault repairs another (schema last)
    picks.sort(key=APPLY_ORDER.index)
    tagged = []
    for f in picks:
        nb, code = apply_fault(rng, f, b, srev, req, info, None)
        if isinstance(nb, str):
            return None
        b = nb
        _, order, status, reason = FAULTS[f]
        tagged.append((order, status, code or reason, f))
    inp = {"bundle": b, "state_revision": srev, "now": NOW, "request": req}
    exp = min(tagged) if tagged else None
    meta = {"faults": picks, "family": info["family"], "transition": info["transition"],
            "expect": None if exp is None else {"status": exp[1], "reason_code": exp[2]}}
    return {"id": f"dec-{idx:06d}-{'+'.join(picks) or 'clean'}", "command": "decide", "input": inp, "meta": meta}


def matching_forbid(rng, req, b):
    a = req["action"]
    st = {"statement_id": "zz-added-forbid", "effect": "forbid", "action_kinds": [a["kind"]],
          "reason_code": "R_ADDED_FORBID"}
    if a["requested_capabilities"] and rng.random() < 0.3:
        st["action_kinds"] = [rng.choice(a["requested_capabilities"])]
    if rng.random() < 0.5:
        st["tenants"] = [req["tenant_id"]]
    if rng.random() < 0.5 and req["principal"]["roles"]:
        st["principal_roles"] = [rng.choice(req["principal"]["roles"])]
    if rng.random() < 0.5:
        rid = a["resource"]["canonical_resource_id"]
        st["resource_prefixes"] = [rid[:rng.randint(len(rid.split(":")[0]) + 1, len(rid))]]
    if st["action_kinds"] == ["workflow.transition"]:
        if rng.random() < 0.5:
            st["to_states"] = [a["attributes"]["to_state"]]
        if rng.random() < 0.3:
            st["from_states"] = [a["attributes"]["from_state"]]
    return st


def gen_relation_cases(rng, idx):
    """A clean request plus metamorphic variants; all share meta.rel."""
    base = clean_case(rng)
    if base is None:
        return []
    b, srev, req, info = base
    rel = f"rel-{idx:06d}"
    mk = lambda tag, bb, rq: {"id": f"{rel}-{tag}", "command": "decide",  # noqa: E731
                              "input": {"bundle": bb, "state_revision": srev, "now": NOW, "request": rq},
                              "meta": {"rel": rel, "tag": tag, "family": info["family"],
                                       "transition": info["transition"]}}
    out = [mk("base", b, req), mk("base-dup", b, req)]
    tw = mk("text", None, None)
    del tw["input"]
    tw["input_text"] = dump_variant({"bundle": b, "state_revision": srev, "now": NOW, "request": req}, rng,
                                    p_ws=0.3, p_escape=0.15)
    out.append(tw)
    bb = clone(b)
    bb["statements"].insert(rng.randint(0, len(bb["statements"])), matching_forbid(rng, req, b))
    out.append(mk("add-forbid", bb, req))
    covered = {req["action"]["kind"], *req["action"]["requested_capabilities"]}
    bb = clone(b)
    bb["statements"] = [s for s in bb["statements"] if not covered & set(s["action_kinds"])]
    out.append(mk("remove-covering", bb, req))
    bb = clone(b)
    rng.shuffle(bb["statements"])
    out.append(mk("shuffle-statements", bb, req))
    for tag, tenant in (("scope-existing", rng.choice([t for t in TENANTS if t != req["tenant_id"]])),
                        ("scope-ghost", GHOST_TENANT)):
        rq = clone(req)
        rid = rq["action"]["resource"]["canonical_resource_id"]
        scheme, rest = rid.split(":", 1)
        segs = rest.split("/")
        segs[0] = tenant
        rq["action"]["resource"]["canonical_resource_id"] = scheme + ":" + "/".join(segs)
        out.append(mk(tag, b, rq))
    rq = clone(req)
    rq["principal"]["roles"] = sorted(set(rq["principal"]["roles"]) | set(rng.sample(ROLES, 2)))
    out.append(mk("extra-roles", b, rq))
    if info["transition"]:
        at = req["action"]["attributes"]
        for tag, field in (("other-to", "to_state"), ("other-from", "from_state")):
            rq = clone(req)
            a2 = rq["action"]["attributes"]
            a2[field] = rng.choice([s for s in STATES if s not in (at["from_state"], at["to_state"])])
            for k in BINDING:
                a2.pop(k, None)
            if is_protected(b, a2["from_state"], a2["to_state"]):
                a2.update({"acceptance_record_id": "acc-1", "artifact_snapshot_id": "snap-1",
                           "artifact_digest": "sha256:" + "b" * 64})
            out.append(mk(tag, b, rq))
    return out


def gen_approval_pair(rng, idx):
    """Two requests that differ only in to_state or only in from_state, under a bundle
    where both are approvable (TX bundle: deployed and released need approval)."""
    b = clone(TX_BUNDLE)
    roles = [rng.choice(["operator", "reviewer"])]  # agent role would hit the release forbid
    srev = rng.randint(0, 50)
    frm = rng.choice(["draft", "planning", "verifying", "reviewing", "accepted"])
    pairs = []
    for tag in ("to", "from"):
        if tag == "to":
            a1, a2 = {"from_state": frm, "to_state": "deployed"}, {"from_state": frm, "to_state": "released"}
        else:
            f2 = rng.choice([s for s in ["draft", "planning", "verifying", "reviewing", "accepted"] if s != frm])
            to = rng.choice(["deployed", "released"])
            a1, a2 = {"from_state": frm, "to_state": to}, {"from_state": f2, "to_state": to}
        for a in (a1, a2):
            a.update({"acceptance_record_id": "acc-1", "artifact_snapshot_id": "snap-1", "artifact_digest": "sha256:" + "b" * 64})
        base = base_request(rng, "workflow.transition", "fixture-tenant", "fixture-project", "wf-1", a1, roles, srev)
        other = clone(base)
        other["action"]["attributes"] = a2
        rel = f"apr-{idx:06d}-{tag}"
        for t, rq in (("a", base), ("b", other)):
            pairs.append({"id": f"{rel}-{t}", "command": "decide",
                          "input": {"bundle": b, "state_revision": srev, "now": NOW, "request": rq},
                          "meta": {"apr": rel, "side": t}})
    return pairs


def gen_cases(rng, n):
    cases, i = [], 0
    while len(cases) < n:
        i += 1
        if rng.random() < 0.05:
            cases.extend(gen_approval_pair(rng, i))
        elif rng.random() < 0.9:
            c = gen_fault_case(rng, i)
            if c:
                cases.append(c)
        else:
            cases.extend(gen_relation_cases(rng, i))
    return cases


def sig(o):
    return (o or {}).get("status"), (o or {}).get("reason_code")


def run(protos, rng, n, res: Results):
    cases = gen_cases(rng, n)
    outs = run_both(protos, "decide", cases, res)
    # second phase: pin the loaded bundle's own sha (from each prototype's base output)
    pin_cases = {}
    for p in protos:
        pc = []
        for c in cases:
            if c["meta"].get("tag") == "base":
                o = outs[p.name].get(c["id"])
                if isinstance(o, dict) and isinstance(o.get("policy_bundle_sha"), str) and o.get("status") != "unavailable":
                    inp = clone(c["input"])
                    inp["request"]["policy_bundle_sha"] = o["policy_bundle_sha"]
                    pc.append({"id": c["id"] + "-pinned", "command": "decide", "input": inp, "meta": c["meta"]})
        o2, probs = p.run("decide", pc)
        res.protocol[p.name].extend(probs)
        pin_cases[p.name] = (pc, o2)

    for p in protos:
        o = outs[p.name]
        rels = {}
        for c in cases:
            out = o.get(c["id"])
            m = c["meta"]
            if "expect" in m:
                if m["expect"] is not None:
                    ok = isinstance(out, dict) and sig(out) == (m["expect"]["status"], m["expect"]["reason_code"])
                    inv = "decide.evaluation_order_single_fault" if len(m["faults"]) == 1 else \
                        "decide.evaluation_order_multi_fault"
                    res.check(inv, p.name, ok, c["id"], {"got": sig(out), "want": m["expect"], "faults": m["faults"]},
                              case=None if ok else c)
                    if ok:
                        res.check("decide.pre_statement_decision_matches_no_rule", p.name,
                                  out.get("matched_rule_ids") == [], c["id"], out)
                    if "badbundle" not in m["faults"] and "nobundle" not in m["faults"]:
                        okb = sig(out) != ("unavailable", "POLICY_BUNDLE_REJECTED")
                        res.check("decide.valid_generated_bundle_loads", p.name, okb, c["id"], out,
                                  case=None if okb else c)
                else:
                    okb = isinstance(out, dict) and out.get("reason_code") not in PRE_STATEMENT
                    res.check("decide.clean_request_reaches_statements", p.name, okb, c["id"],
                              {"got": sig(out), "family": m["family"]}, case=None if okb else c)
                if isinstance(out, dict) and out.get("status") == "allow" and m["transition"]:
                    at = c["input"]["request"]["action"]["attributes"]
                    b = c["input"]["bundle"]
                    if b and is_protected(b, at.get("from_state"), at.get("to_state")):
                        okp = all(k in at for k in BINDING)
                        res.check("decide.protected_transition_without_binding_never_allowed", p.name, okp, c["id"],
                                  out, case=None if okp else c)
            if "rel" in m:
                rels.setdefault(m["rel"], {})[m["tag"]] = (c, out)
        for rel, g in rels.items():
            check_relations(res, p.name, g)
        aprs = {}
        for c in cases:
            if "apr" in c["meta"]:
                aprs.setdefault(c["meta"]["apr"], {})[c["meta"]["side"]] = o.get(c["id"])
        for rel, g in aprs.items():
            oa, ob = g.get("a"), g.get("b")
            both = all(isinstance(x, dict) and x.get("status") == "require_approval" for x in (oa, ob))
            res.check("decide.approval_pair_both_require_approval", p.name, both, rel, {"a": sig(oa), "b": sig(ob)})
            if both:
                ok = oa.get("approval_request_reference") != ob.get("approval_request_reference")
                res.check("decide.approval_request_id_binds_from_and_to_state", p.name, ok, rel,
                          {"a": oa.get("approval_request_reference"), "b": ob.get("approval_request_reference")})
        pc, o2 = pin_cases[p.name]
        for c in pc:
            base_out = o.get(c["id"][:-len("-pinned")])
            got = o2.get(c["id"])
            ok = isinstance(got, dict) and sig(got) == sig(base_out) and \
                got.get("matched_rule_ids") == base_out.get("matched_rule_ids")
            res.check("decide.pin_own_bundle_sha_is_neutral", p.name, ok, c["id"], {"pinned": got, "base": base_out},
                      case=None if ok else c)
    differential(protos, outs, cases, res)
    if len(protos) == 2:
        a, b = protos[0].name, protos[1].name
        ca, oa = pin_cases[a]
        cb, ob = pin_cases[b]
        for c in ca:
            if c["id"] in ob:
                same = oa.get(c["id"]) == ob.get(c["id"])
                res.check("differential.exact_output", "both", same, c["id"])
                if not same:
                    res.mismatch(c, {a: oa.get(c["id"]), b: ob.get(c["id"])})
    return cases, outs


def check_relations(res, pname, g):
    base = g.get("base")
    if not base:
        return
    bc, bo = base
    if not isinstance(bo, dict):
        res.check("decide.output_is_object", pname, False, bc["id"], bo)
        return
    reached = bo.get("reason_code") not in PRE_STATEMENT
    st = bo.get("status")
    if "base-dup" in g:
        res.check("decide.deterministic_same_input", pname, g["base-dup"][1] == bo, bc["id"])
    if "text" in g:
        ok = g["text"][1] == bo
        res.check("decide.key_order_whitespace_escape_invariance", pname, ok, bc["id"],
                  {"text": g["text"][1], "value": bo}, case=None if ok else g["text"][0])
    if "add-forbid" in g:
        c, o = g["add-forbid"]
        ok = sig(o)[0] not in ("allow", "require_approval") and (not reached or sig(o)[0] == "deny")
        res.check("decide.adding_matching_forbid_never_allows", pname, ok, c["id"], {"base": sig(bo), "got": sig(o)},
                  case=None if ok else c)
    if "remove-covering" in g and reached:
        c, o = g["remove-covering"]
        ok = sig(o) == ("deny", "DEFAULT_DENY")
        res.check("decide.removing_covering_statements_default_denies", pname, ok, c["id"],
                  {"base": sig(bo), "got": sig(o)}, case=None if ok else c)
    if "shuffle-statements" in g:
        c, o = g["shuffle-statements"]
        ok = sig(o)[0] == st
        res.check("decide.statement_order_does_not_change_status", pname, ok, c["id"], {"base": sig(bo), "got": sig(o)},
                  case=None if ok else c)
        if ok and st == bo.get("status"):
            res.check("decide.statement_order_does_not_change_matched_set", pname,
                      sorted(o.get("matched_rule_ids") or []) == sorted(bo.get("matched_rule_ids") or []), c["id"],
                      {"base": bo.get("matched_rule_ids"), "got": o.get("matched_rule_ids")})
    for tag in ("scope-existing", "scope-ghost"):
        if tag in g:
            c, o = g[tag]
            ok = sig(o)[0] not in ("allow", "require_approval")
            if reached:
                ok = sig(o) == ("deny", "RESOURCE_SCOPE_MISMATCH")
            res.check("decide.cross_tenant_resource_never_allowed", pname, ok, c["id"], {"base": sig(bo), "got": sig(o)},
                      case=None if ok else c)
    if "scope-existing" in g and "scope-ghost" in g:
        o1, o2 = g["scope-existing"][1], g["scope-ghost"][1]
        keys = ("status", "reason_code", "matched_rule_ids", "safe_message", "approval_request_reference")
        ok = isinstance(o1, dict) and isinstance(o2, dict) and all(o1.get(k) == o2.get(k) for k in keys)
        res.check("decide.existing_vs_nonexistent_foreign_tenant_indistinguishable", pname, ok, bc["id"],
                  {"existing": o1, "ghost": o2}, case=None if ok else g["scope-ghost"][0])
    if "extra-roles" in g:
        c, o = g["extra-roles"]
        stmts = {s["statement_id"]: s for s in (bc["input"]["bundle"] or {}).get("statements", [])}
        forbid_matched = st == "deny" and any(stmts.get(r, {}).get("effect") == "forbid"
                                              for r in bo.get("matched_rule_ids") or [])
        if forbid_matched:
            ok = sig(o)[0] == "deny"
            res.check("decide.extra_role_never_cancels_matched_forbid", pname, ok, c["id"],
                      {"base": sig(bo), "got": sig(o)}, case=None if ok else c)
    for tag in ("other-to", "other-from"):
        if tag in g and st == "require_approval":
            c, o = g[tag]
            if isinstance(o, dict) and o.get("status") == "require_approval":
                ok = o.get("approval_request_reference") != bo.get("approval_request_reference")
                res.check("decide.approval_request_id_binds_from_and_to_state", pname, ok, c["id"],
                          {"base": bo.get("approval_request_reference"), "got": o.get("approval_request_reference")},
                          case=None if ok else c)
