"""Shared helpers: D-006 canonical form for generated values, digests, variant
serialization, prototype invocation and result bookkeeping.

The canonical form here is computed only for values this harness generated
itself (so it never parses untrusted text). It follows D-006 as written: keys
sorted by code point, no whitespace, UTF-8, minimal escaping, integers only.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import subprocess
import time
from collections import defaultdict

MAX_SAFE = 2**53 - 1


# ---------------------------------------------------------------- canonical

def canonical_text(value) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True,
                      allow_nan=False)


def digest(value) -> str:
    return "sha256:" + hashlib.sha256(canonical_text(value).encode("utf-8")).hexdigest()


def text_digest(text: str) -> str:
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


def seal(request: dict, adapter_id: str, key_hex: str) -> dict:
    mac = hmac.new(bytes.fromhex(key_hex), canonical_text(request).encode("utf-8"), hashlib.sha256)
    return {"adapter_id": adapter_id, "mac": mac.hexdigest()}


def max_depth(value, d: int = 0) -> int:
    """D-006/D-018 traversal depth: root 0, array elements, member names and
    member values at parent + 1."""
    if isinstance(value, list):
        return max([d] + [max_depth(v, d + 1) for v in value])
    if isinstance(value, dict):
        if not value:
            return d
        return max([d + 1] + [max_depth(v, d + 1) for v in value.values()])
    return d


def clone(value):
    return json.loads(json.dumps(value))


# ------------------------------------------------- variant (non-canonical) text

WS = [" ", "\t", "\n", "\r", "  ", "\r\n"]


def _ws(rng, p):
    return rng.choice(WS) if rng.random() < p else ""


def dump_string(s: str, rng, p_escape: float) -> str:
    out = ['"']
    for ch in s:
        cp = ord(ch)
        if ch == '"':
            out.append('\\"' if rng.random() < 0.7 else "\\u0022")
        elif ch == "\\":
            out.append("\\\\" if rng.random() < 0.7 else "\\u005c")
        elif cp < 0x20:
            short = {0x08: "\\b", 0x0C: "\\f", 0x0A: "\\n", 0x0D: "\\r", 0x09: "\\t"}.get(cp)
            if short and rng.random() < 0.5:
                out.append(short)
            else:
                out.append("\\u%04x" % cp if rng.random() < 0.5 else "\\u%04X" % cp)
        elif ch == "/" and rng.random() < p_escape:
            out.append("\\/")
        elif cp > 0xFFFF and rng.random() < p_escape:
            v = cp - 0x10000
            hi, lo = 0xD800 + (v >> 10), 0xDC00 + (v & 0x3FF)
            fmt = "\\u%04x" if rng.random() < 0.5 else "\\u%04X"
            out.append(fmt % hi + fmt % lo)
        elif cp <= 0xFFFF and rng.random() < p_escape:
            out.append(("\\u%04x" if rng.random() < 0.5 else "\\u%04X") % cp)
        else:
            out.append(ch)
    out.append('"')
    return "".join(out)


def dump_variant(value, rng, p_ws: float = 0.3, p_escape: float = 0.1, permute: bool = True) -> str:
    """Serialize a generated value with random key order, insignificant
    whitespace and escape spellings. Parses to exactly `value`."""
    if isinstance(value, bool) or value is None:
        return json.dumps(value)
    if isinstance(value, int):
        return str(value)
    if isinstance(value, str):
        return dump_string(value, rng, p_escape)
    if isinstance(value, list):
        if not value:
            return "[" + _ws(rng, p_ws) + "]"
        parts = [_ws(rng, p_ws) + dump_variant(v, rng, p_ws, p_escape, permute) + _ws(rng, p_ws) for v in value]
        return "[" + ",".join(parts) + "]"
    if isinstance(value, dict):
        keys = list(value)
        if permute:
            rng.shuffle(keys)
        if not keys:
            return "{" + _ws(rng, p_ws) + "}"
        parts = [_ws(rng, p_ws) + dump_string(k, rng, p_escape) + _ws(rng, p_ws) + ":" + _ws(rng, p_ws)
                 + dump_variant(value[k], rng, p_ws, p_escape, permute) + _ws(rng, p_ws) for k in keys]
        return "{" + ",".join(parts) + "}"
    raise TypeError(type(value))


# ----------------------------------------------------------- prototype I/O

class Proto:
    def __init__(self, name: str, argv: list[str], timeout: float = 600.0):
        self.name, self.argv, self.timeout = name, argv, timeout
        self.invocations = 0

    def run(self, command: str, cases: list[dict]) -> tuple[dict, list[str]]:
        """cases: [{"id", "input"|"input_text"}]; returns ({id: output}, problems)."""
        lines = []
        for c in cases:
            line = {"id": c["id"]}
            if "input_text" in c:
                line["input_text"] = c["input_text"]
            else:
                line["input"] = c["input"]
            lines.append(json.dumps(line, ensure_ascii=False, separators=(",", ":")))
        payload = ("\n".join(lines) + "\n").encode("utf-8") if lines else b""
        return self.run_raw(command, payload, [c["id"] for c in cases])

    def run_raw(self, command: str, payload: bytes, ids: list[str]) -> tuple[dict, list[str]]:
        self.invocations += 1
        problems = []
        try:
            proc = subprocess.run([*self.argv, *command.split(" ")], input=payload, capture_output=True,
                                  timeout=self.timeout)
        except subprocess.TimeoutExpired:
            return {}, [f"timeout after {self.timeout}s"]
        if proc.returncode != 0:
            problems.append(f"exit status {proc.returncode}: {proc.stderr[-300:].decode('utf-8', 'replace')}")
        out = {}
        # split on LF only: str.splitlines() also splits on U+2028, U+0085, etc.
        for raw in proc.stdout.decode("utf-8", "replace").split("\n"):
            if not raw.strip():
                continue
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                problems.append("non-JSON output line")
                continue
            rid = msg.get("id") if isinstance(msg, dict) else None
            if rid in out:
                problems.append(f"duplicate id {rid!r}")
            elif isinstance(msg, dict) and "output" in msg:
                out[rid] = msg["output"]
            else:
                problems.append(f"line without output for {rid!r}")
        wanted = set(ids)
        missing = [i for i in ids if i not in out]
        if missing:
            problems.append(f"{len(missing)} ids unanswered, e.g. {missing[:3]}")
        extra = [i for i in out if i not in wanted]
        if extra:
            problems.append(f"{len(extra)} unknown ids answered")
        return out, problems


# ------------------------------------------------------------- bookkeeping

MAX_EXAMPLES = 5


class Results:
    """Per-invariant pass/fail counts per prototype, plus findings."""

    def __init__(self, campaign: str):
        self.campaign = campaign
        self.counts = defaultdict(lambda: defaultdict(lambda: {"pass": 0, "fail": 0}))
        self.examples = defaultdict(list)
        self.mismatches = []
        self.mismatch_count = 0
        self.clusters = {}
        self.protocol = defaultdict(list)
        self.notes = []

    def check(self, inv: str, proto: str, ok: bool, case_id: str = "", detail=None, case=None):
        self.counts[inv][proto]["pass" if ok else "fail"] += 1
        if not ok and len(self.examples[(inv, proto)]) < MAX_EXAMPLES:
            ex = {"invariant": inv, "proto": proto, "case_id": case_id, "detail": detail}
            if case is not None:
                ex["case"] = case
            self.examples[(inv, proto)].append(ex)
        return ok

    def mismatch(self, case: dict, outputs: dict, note: str = ""):
        self.mismatch_count += 1
        vals = list(outputs.values())
        if len(vals) == 2 and all(isinstance(v, dict) for v in vals):
            diff = sorted(k for k in set(vals[0]) | set(vals[1]) if vals[0].get(k, "<missing>") != vals[1].get(k, "<missing>"))
        else:
            diff = ["<whole output>"]
        sig = f"{case.get('command')}: differs in {','.join(diff)}" + (" (prefix before concurrency)" if note else "")
        cl = self.clusters.setdefault(sig, {"count": 0, "first_case_id": case["id"]})
        cl["count"] += 1
        if cl["count"] == 1:
            cl["example"] = {"case": case, "outputs": outputs}
        if len(self.mismatches) < 40:
            self.mismatches.append({"case_id": case["id"], "command": case.get("command"), "note": note,
                                    "case": case, "outputs": outputs})

    def failed(self) -> bool:
        return self.mismatch_count > 0 or any(v["fail"] for d in self.counts.values() for v in d.values()) \
            or any(self.protocol.values())

    def to_json(self) -> dict:
        return {
            "invariants": {inv: dict(per) for inv, per in sorted(self.counts.items())},
            "violations": [e for exs in self.examples.values() for e in exs],
            "differential_mismatches": self.mismatch_count,
            "mismatch_clusters": self.clusters,
            "mismatch_examples": self.mismatches[:10],
            "protocol_problems": {k: v[:10] for k, v in self.protocol.items() if v},
            "notes": self.notes,
        }


def run_both(protos: list[Proto], command: str, cases: list[dict], res: Results) -> dict:
    """Run cases through every prototype; returns {proto_name: {id: output}}."""
    outs = {}
    for p in protos:
        started = time.perf_counter()
        o, probs = p.run(command, cases)
        if probs:
            res.protocol[p.name].extend(f"{command}: {x}" for x in probs)
        outs[p.name] = o
        res.notes.append(f"{p.name} {command}: {len(cases)} cases in {time.perf_counter() - started:.2f}s")
    return outs


def differential(protos: list[Proto], outs: dict, cases: list[dict], res: Results, skip=lambda c: False):
    if len(protos) < 2:
        return
    a, b = protos[0].name, protos[1].name
    for c in cases:
        if skip(c):
            continue
        oa, ob = outs[a].get(c["id"]), outs[b].get(c["id"])
        same = oa == ob
        res.check("differential.exact_output", "both", same, c["id"])
        if not same:
            res.mismatch(c, {a: oa, b: ob})


def run_probes(protos: list[Proto], cases: list[dict], res: Results, prefix: str):
    """Unspecified inputs, one process per case so a crash cannot hide other results.
    Oracle (M1-PARITY-SUITE "Prototype interface"): the process answers the line and
    exits 0; a refusal is an output, never a crash. Outputs are compared differentially."""
    outs = {p.name: {} for p in protos}
    for c in cases:
        for p in protos:
            o, probs = p.run(c["command"], [c])
            outs[p.name][c["id"]] = o.get(c["id"])
            ok = not probs
            res.check(f"{prefix}.probe_answered_without_crash", p.name, ok, c["id"], probs[:2],
                      case=None if ok else c)
    differential(protos, outs, cases, res)
    return outs
