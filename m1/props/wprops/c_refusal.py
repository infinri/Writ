"""Campaign `refusal`: internal-to-external refusal mapping (`external-refusal`).

Oracle (SEMANTICS.md "Internal codes versus external responses", D-018 A7,
D-019, D-020): exactly these twelve codes pass through unchanged; every other
code, including unknown ones and near-miss spellings (case, whitespace,
look-alike characters, prefixes), becomes PROMOTION_REFUSED. Escape spelling of
the same code in raw input text does not change the mapping. Whatever the
input, an `external` value is never anything but a pass-through code or
PROMOTION_REFUSED. Inputs that are not {"internal_code": <string>} have no
specified output: differential only.
"""
from __future__ import annotations

from .common import Results, differential, dump_variant, run_both

NAME = "refusal"
PASS = ["INPUT_JSON_INVALID", "REQUEST_JSON_INVALID", "REQUEST_SCHEMA_INVALID", "DEADLINE_EXCEEDED",
        "ACCEPTANCE_RECORD_MALFORMED", "TRANSITION_MALFORMED", "WORKFLOW_RESOURCE_INVALID",
        "REQUESTED_CAPABILITIES_INVALID", "COMPOSITE_NOT_SUPPORTED", "IDEMPOTENCY_CONFLICT",
        "RECONCILIATION_REQUIRED", "APPROVAL_REQUIRED"]
SECURITY = [
    "CURRENT_STATE_MALFORMED", "REQUIREMENTS_MALFORMED", "REQUIREMENTS_POLICY_MISMATCH", "ACCEPTANCE_RECORD_INVALID",
    "ACCEPTANCE_REPLAYED", "ACCEPTANCE_SCOPE_MISMATCH", "ACCEPTANCE_TRANSITION_MISMATCH",
    "ACCEPTANCE_ARTIFACT_MISMATCH", "ACCEPTANCE_POLICY_MISMATCH", "ACCEPTANCE_REVISION_MISMATCH",
    "ACCEPTANCE_EXPIRED", "ACCEPTANCE_CRITERIA_MISSING", "ACCEPTANCE_CRITERIA_MISMATCH",
    "ACCEPTANCE_UNTRUSTED_EVALUATOR", "ACCEPTANCE_EVALUATOR_REVOKED", "ACCEPTANCE_STALE", "ACCEPTANCE_INCOMPLETE",
    "ACCEPTANCE_REQUIRED", "ACCEPTANCE_BINDING_UNEXPECTED", "ACCEPTANCE_BINDING_REQUIRED", "PERMIT_MISSING",
    "PERMIT_REQUEST_MISMATCH", "PERMIT_BINDING_MISMATCH", "PERMIT_POLICY_STALE", "PROTECTION_UNAVAILABLE",
    "POLICY_DEPENDENCY_MISMATCH", "REQUEST_SCOPE_MISMATCH", "REVISION_CONFLICT", "STATE_REVISION_CONFLICT",
    "NOTHING_TO_RECONCILE", "EFFECT_PREREQUISITE_UNMET", "PROMOTION_RECEIPT_INVALID", "EFFECT_ARTIFACT_MISMATCH",
    "PROMOTION_STALE", "INGRESS_UNAUTHENTICATED", "LEDGER_UNAVAILABLE", "GRANT_UNKNOWN", "GRANT_MISMATCH",
    "GRANT_EXPIRED", "GRANT_EXHAUSTED", "GRANT_MALFORMED", "PERMIT_INVALID", "PERMIT_EXPIRED", "PERMIT_REPLAYED",
    "NOT_APPROVABLE", "SELF_APPROVAL_FORBIDDEN", "HUMAN_UNAUTHENTICATED", "POLICY_UNAVAILABLE",
    "POLICY_BUNDLE_REJECTED", "POLICY_VERSION_MISMATCH", "PRINCIPAL_UNVERIFIED", "UNKNOWN_CAPABILITY",
    "UNSUPPORTED_CAPABILITY", "RESOURCE_NOT_CANONICAL", "RESOURCE_SCOPE_MISMATCH", "DEFAULT_DENY",
    "CAPABILITY_NOT_PERMITTED", "SECRETS_PATH_FORBIDDEN", "NETWORK_EGRESS_FORBIDDEN", "AGENT_RELEASE_FORBIDDEN",
    "PROMOTION_REFUSED"]
LOOKALIKE = {"A": "А", "E": "Е", "O": "О", "I": "І", "_": "＿"}


def near_miss(rng, code):
    r = rng.choice(["lower", "space", "lead", "nul", "zwsp", "prefix", "suffix", "lookalike", "truncate", "tab",
                    "fullwidth", "dash"])
    if r == "lower":
        return code.lower()
    if r == "space":
        return code + " "
    if r == "lead":
        return " " + code
    if r == "nul":
        return code + "\x00"
    if r == "zwsp":
        i = rng.randint(1, len(code) - 1)
        return code[:i] + "​" + code[i:]
    if r == "prefix":
        return "X_" + code
    if r == "suffix":
        return code + "_V2"
    if r == "lookalike":
        idx = [i for i, ch in enumerate(code) if ch in LOOKALIKE]
        if not idx:
            return code + "́"
        i = rng.choice(idx)
        return code[:i] + LOOKALIKE[code[i]] + code[i + 1:]
    if r == "truncate":
        return code[:-1]
    if r == "tab":
        return code.replace("_", "\t", 1)
    if r == "fullwidth":
        return "".join(chr(ord(c) + 0xFEE0) if "A" <= c <= "Z" else c for c in code)
    return code.replace("_", "-")


def gen_cases(rng, n):
    cases = []

    def add(kind, inp=None, text=None, expect=None):
        c = {"id": f"ext-{len(cases):06d}-{kind}", "command": "external-refusal", "meta": {"kind": kind, "expect": expect}}
        if text is not None:
            c["input_text"] = text
        else:
            c["input"] = inp
        cases.append(c)

    for code in PASS:
        add("pass_through", {"internal_code": code}, expect=code)
    for code in SECURITY:
        add("security", {"internal_code": code}, expect="PROMOTION_REFUSED")
    while len(cases) < n:
        r = rng.random()
        if r < 0.3:
            code = rng.choice(PASS + SECURITY)
            miss = near_miss(rng, code)
            add("near_miss", {"internal_code": miss}, expect="PROMOTION_REFUSED" if miss not in PASS else miss)
        elif r < 0.45:
            alpha = "ABCDEFGHIJKLMNOPQRSTUVWXYZ_0123456789"
            code = "".join(rng.choice(alpha) for _ in range(rng.randint(1, 40)))
            add("random_unknown", {"internal_code": code}, expect=code if code in PASS else "PROMOTION_REFUSED")
        elif r < 0.65:
            code = rng.choice(PASS + SECURITY)
            add("escaped_text", text=dump_variant({"internal_code": code}, rng, p_ws=0.5, p_escape=0.6),
                expect=code if code in PASS else "PROMOTION_REFUSED")
        elif r < 0.8:
            code = rng.choice(PASS + SECURITY)
            add("repeat", {"internal_code": code}, expect=code if code in PASS else "PROMOTION_REFUSED")
        else:
            bad = rng.choice([{}, {"internal_code": None}, {"internal_code": 7}, {"internal_code": ["INPUT_JSON_INVALID"]},
                              {"internal_code": {"code": "INPUT_JSON_INVALID"}},
                              {"internal_code": "INPUT_JSON_INVALID", "extra": "DEADLINE_EXCEEDED"},
                              {"code": "INPUT_JSON_INVALID"}, [], "INPUT_JSON_INVALID", None, 1,
                              {"internal_code": "INPUT_JSON_INVALID", "internal_code_2": "PERMIT_MISSING"}])
            add("malformed_input", bad, expect=None)
    return cases


def run(protos, rng, n, res: Results):
    cases = gen_cases(rng, n)
    outs = run_both(protos, "external-refusal", cases, res)
    for p in protos:
        for c in cases:
            out = outs[p.name].get(c["id"])
            exp = c["meta"]["expect"]
            ext = out.get("external") if isinstance(out, dict) else None
            if ext is not None:
                ok = ext in PASS or ext == "PROMOTION_REFUSED"
                res.check("refusal.external_value_is_safe", p.name, ok, c["id"], out, case=None if ok else c)
            if exp is not None:
                ok = out == {"external": exp}
                res.check(f"refusal.{c['meta']['kind']}", p.name, ok, c["id"], {"got": out, "want": exp},
                          case=None if ok else c)
    differential(protos, outs, cases, res)
    return cases, outs
