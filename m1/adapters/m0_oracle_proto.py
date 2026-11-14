#!/usr/bin/env python3
"""Protocol adapter exposing the M0 Python reference harness as a third parity participant.

Non-normative (D-001): used only for differential comparison and to exercise the
driver against a real implementation. It is not a prototype candidate and its
agreement with a fixture is not evidence that the fixture is right (R-ORACLE).

Run with the M0 venv:  m0/.venv/bin/python m1/adapters/m0_oracle_proto.py <command>
Commands: canonical, decide.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "m0"))

from writ_m0 import canonical, decision  # noqa: E402


def do_canonical(msg: dict) -> dict:
    try:
        value = json.loads(msg["input_text"])
        return {"canonical": canonical.canonical_bytes(value).decode("utf-8"), "digest": canonical.digest(value)}
    except (ValueError, canonical.CanonicalizationError) as exc:
        return {"error": type(exc).__name__}


def do_decide(msg: dict) -> dict:
    data = msg["input"]
    try:
        bundle = decision.load_bundle(data["bundle"]) if data["bundle"] is not None else None
    except decision.BundleError as exc:
        return {"error": "BUNDLE_REJECTED", "detail": str(exc)}
    return decision.decide(data["request"], bundle, decision.KernelState(data["state_revision"]),
                           decision.parse_time(data["now"]))


HANDLERS = {"canonical": do_canonical, "decide": do_decide}


def main() -> int:
    if len(sys.argv) != 2 or sys.argv[1] not in HANDLERS:
        print(f"usage: {sys.argv[0]} {{{','.join(HANDLERS)}}}", file=sys.stderr)
        return 2
    handler = HANDLERS[sys.argv[1]]
    for raw in sys.stdin:
        if raw.strip():
            msg = json.loads(raw)
            out = {"id": msg["id"], "output": handler(msg)}
            sys.stdout.write(json.dumps(out, ensure_ascii=False, separators=(",", ":")) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
