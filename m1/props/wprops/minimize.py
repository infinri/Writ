"""Shrinking of differential mismatches and writing of regression cases.

Minimization is delta debugging on the input: for raw text, chunked character
deletion; for JSON values, removal of object members and array elements and
replacement of subtrees by their own children, keeping a change only while the
two prototypes still disagree in the same way (same set of differing output
fields). A minimized input is reported as a finding.

A regression case is written to regressions/<campaign>.jsonl only when the
original case carries a spec-derived exact expectation from its oracle and the
two prototypes disagree with exactly one of them matching it (expectation from
the spec, cross-checked by one independent implementation). The original, not
the minimized, input is written, because shrinking can change what the spec
expects. Everything else stays a reported finding.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from .common import clone, text_digest

MAX_EVALS = 400
COMMANDS = {"json": "canonical", "decide": "decide", "acceptance": "check-acceptance", "refusal": "external-refusal",
            "transition": "transition-scenario", "broker": "broker-scenario"}


def diff_sig(oa, ob):
    if isinstance(oa, dict) and isinstance(ob, dict):
        return tuple(sorted(k for k in set(oa) | set(ob) if oa.get(k, "<m>") != ob.get(k, "<m>")))
    return ("<whole>",)


class Shrinker:
    def __init__(self, protos, command):
        self.protos, self.command, self.evals = protos, command, 0

    def outputs(self, case):
        self.evals += 1
        outs = []
        for p in self.protos:
            o, _ = p.run(self.command, [case])
            outs.append(o.get(case["id"]))
        return outs

    def interesting(self, case, sig):
        if self.evals >= MAX_EVALS:
            return False
        oa, ob = self.outputs(case)
        return oa != ob and diff_sig(oa, ob) == sig

    def shrink_text(self, case, sig):
        text = case["input_text"]
        n = 2
        while len(text) >= 2 and self.evals < MAX_EVALS:
            chunk = max(1, len(text) // n)
            reduced = False
            for start in range(0, len(text), chunk):
                cand = text[:start] + text[start + chunk:]
                c2 = dict(case, input_text=cand)
                if self.interesting(c2, sig):
                    text, reduced = cand, True
                    n = max(n - 1, 2)
                    break
            if not reduced:
                if chunk == 1:
                    break
                n = min(len(text), n * 2)
        return dict(case, input_text=text)

    def shrink_value(self, case, sig):
        value = clone(case["input"])
        changed = True
        while changed and self.evals < MAX_EVALS:
            changed = False
            for path in list(paths(value)):
                for cand in candidates(value, path):
                    c2 = dict(case, input=cand)
                    if self.interesting(c2, sig):
                        value, changed = cand, True
                        break
                if changed or self.evals >= MAX_EVALS:
                    break
        return dict(case, input=value)


def paths(v, p=()):
    yield p
    if isinstance(v, dict):
        for k in list(v):
            yield from paths(v[k], p + (k,))
    elif isinstance(v, list):
        for i in range(len(v)):
            yield from paths(v[i], p + (i,))


def get(v, p):
    for k in p:
        v = v[k]
    return v


def candidates(root, path):
    node = get(root, path)
    out = []
    if isinstance(node, dict):
        for k in list(node):
            c = clone(root)
            del get(c, path)[k]
            out.append(c)
    elif isinstance(node, list):
        for i in range(len(node)):
            c = clone(root)
            del get(c, path)[i]
            out.append(c)
    if path and isinstance(node, (dict, list)):
        for child in (node.values() if isinstance(node, dict) else node):
            if isinstance(child, type(node)):
                c = clone(root)
                get(c, path[:-1])[path[-1]] = clone(child)
                out.append(c)
    return out


def expectation(campaign, case):
    """Spec-derived exact expectation carried by the case's oracle, or None."""
    m = case.get("meta") or {}
    exp = m.get("expect")
    if exp is None:
        return None
    if campaign == "json":
        if "canonical" in exp:
            return {"expect": {"canonical": exp["canonical"], "digest": text_digest(exp["canonical"])}}
        if "error" in exp and len(exp["error"]) == 1:
            return {"expect": {"error": next(iter(exp["error"]))}}
        return None
    if campaign == "acceptance":
        return {"expect": {"ok": True} if exp == "ok" else {"refusal": exp}}
    if campaign == "decide":
        return {"expect": {"status": exp["status"], "reason_code": exp["reason_code"], "matched_rule_ids": []},
                "compare": "fields", "fields": ["status", "reason_code", "matched_rule_ids"]}
    if campaign == "transition" and m.get("family") in ("promote_faults", "promote_concurrent"):
        return {"expect": exp}
    if campaign == "refusal":
        return {"expect": {"external": exp}}
    return None


def matches(e, out):
    if e.get("compare") == "fields":
        return isinstance(out, dict) and all(out.get(f) == e["expect"].get(f) for f in e["fields"])
    return out == e["expect"]


def minimize_findings(campaign, protos, res, regdir: Path, seed: int):
    if len(protos) != 2:
        return []
    command = COMMANDS[campaign]
    findings, regressions = [], []
    for sig_name, cl in list(res.clusters.items())[:12]:
        ex = cl.get("example") or {}
        case = ex.get("case")
        if not case or "prefix" in sig_name or not ({"input", "input_text"} & set(case)):
            findings.append({"cluster": sig_name, "count": cl["count"], "case_id": cl["first_case_id"],
                             "minimized": None, "outputs": ex.get("outputs")})
            continue
        sh = Shrinker(protos, case.get("command") or command)
        oa, ob = sh.outputs(case)
        sig = diff_sig(oa, ob)
        if oa == ob:
            findings.append({"cluster": sig_name, "count": cl["count"], "case_id": case["id"],
                             "note": "not reproducible in a single-case invocation (nondeterministic or batch-dependent)",
                             "outputs": ex.get("outputs")})
            continue
        work = case
        if "input_text" in case and campaign != "json":
            # strict-valid generated text: shrink its value with member order preserved
            try:
                v = json.loads(case["input_text"], object_pairs_hook=dict)
                alt = {k: x for k, x in case.items() if k != "input_text"}
                alt["input"] = v
                oa2, ob2 = sh.outputs(alt)
                if oa2 != ob2 and diff_sig(oa2, ob2) == sig:
                    work = alt
            except ValueError:
                pass
        small = sh.shrink_text(work, sig) if "input_text" in work else sh.shrink_value(work, sig)
        so = sh.outputs(small)
        mini = {k: small[k] for k in ("input", "input_text") if k in small}
        f = {"cluster": sig_name, "count": cl["count"], "case_id": case["id"], "minimized": mini,
             "minimized_outputs": {protos[0].name: so[0], protos[1].name: so[1]}, "evaluations": sh.evals,
             "original_meta": case.get("meta")}
        e = expectation(campaign, case)
        if e is not None:
            who = [p.name for p, o in zip(protos, (oa, ob)) if matches(e, o)]
            f["oracle_expectation"] = e["expect"]
            f["matches_oracle"] = who
            if len(who) == 1:
                regressions.append((case, e, sig_name))
        findings.append(f)
    if regressions:
        write_regressions(campaign, command, regressions, regdir, seed)
    return findings


def write_regressions(campaign, command, regressions, regdir: Path, seed: int):
    regdir.mkdir(parents=True, exist_ok=True)
    path = regdir / f"{campaign}.jsonl"
    existing = {}
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                c = json.loads(line)
                existing[c["id"]] = c
    for case, e, sig in regressions:
        body = case.get("input_text") if "input_text" in case else case.get("input")
        h = hashlib.sha256(json.dumps(body, sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:12]
        rid = f"m105-{campaign}-{h}"
        reg = {"id": rid}
        if "input_text" in case:
            reg["input_text"] = case["input_text"]
        else:
            reg["input"] = case["input"]
        reg["expect"] = e["expect"]
        reg["compare"] = e.get("compare", "exact")
        if "fields" in e:
            reg["fields"] = e["fields"]
        reg["meta"] = {"source": "m1/props (M1-05)", "seed": seed, "generated_case": case["id"],
                       "mismatch": sig, "oracle": (case.get("meta") or {}).get("kind")
                       or (case.get("meta") or {}).get("faults") or (case.get("meta") or {}).get("mutations"),
                       "review_status": "PENDING INDEPENDENT REVIEW"}
        existing.setdefault(rid, reg)
    path.write_text("".join(json.dumps(c, ensure_ascii=False, sort_keys=True, default=sorted) + "\n"
                            for c in sorted(existing.values(), key=lambda c: c["id"])), encoding="utf-8")
    manifest = regdir / "manifest.json"
    sets = []
    for f in sorted(regdir.glob("*.jsonl")):
        sets.append({"name": f"m105-{f.stem}", "command": COMMANDS[f.stem], "file": f.name, "hard_gate": False,
                     "provenance": "M1-05 regressions; expectation from the spec oracle (generated ones cross-checked by one "
                                   "prototype; hand-minimized ones derived from a frozen fixture by a semantics-preserving "
                                   "change); PENDING INDEPENDENT REVIEW"})
    manifest.write_text(json.dumps({"description": "M1-05 regression cases (not part of m1/fixtures)", "sets": sets},
                                   indent=2) + "\n", encoding="utf-8")
