"""Runs each drill-declared refusal whose mechanism is permissionDecisionReason as a
real subprocess, with the blackbox sentinel written into the isolated fake HOME, and
asserts the captured OUT row's payload is byte-identical to what the hook actually
wrote to stdout (plan.md dfacff61-23d5-474e-846c-2e2f0f0ea482, Decision 1 and the
Testing shape section).

THE PROPERTY THIS FILE OWNS: an OUT capture row must contain the bytes actually
written to stdout, never a reconstruction of them. writ-pre-write-dispatch.sh's
allow path is the live counter-example (line 297 logs the rule-prose ingredients
instead of the additionalContext envelope printed at lines 290 to 296), so this
file drives that path through a daemon stub and checks the same property there.

Reuses tests/firedrill/_harness.py::run_hook and the census tests/firedrill/_census.py
already declares, per the dispatch brief: no second harness, no invented fixtures for
the four DEFERRED_SCRIPTS the drill cannot reach.
"""
from __future__ import annotations

import http.server
import json
import threading
from contextlib import contextmanager
from pathlib import Path

import pytest

from tests.firedrill._census import REFUSALS, deferred_scripts, refusing_scripts
from tests.firedrill._harness import Isolation, make_isolation, real_blackbox_snapshot, run_hook

_PERMISSION_REFUSALS = [
    r for r in REFUSALS if r.generic and r.mechanism == "permissionDecisionReason"
]
assert _PERMISSION_REFUSALS, "no permissionDecisionReason refusals declared; precondition"


def _write_sentinel(iso: Isolation) -> None:
    (iso.fake_home / ".claude" / "writ-blackbox.on").write_text("on")


def _capture_log_path(iso: Isolation) -> Path:
    return iso.fake_home / ".claude" / "writ-blackbox.jsonl"


def _read_rows(iso: Isolation) -> list[dict]:
    path = _capture_log_path(iso)
    if not path.exists():
        return []
    return [json.loads(ln) for ln in path.read_text().splitlines() if ln.strip()]


class TestRealCaptureLogStillUntouchedWithCaptureOn:
    """Standing check (harness trap 3), repeated here with the sentinel actually
    turned ON: even so, it is written into the isolated fake HOME, never the
    developer's real ~/.claude/writ-blackbox.jsonl."""

    def test_a_refusal_run_with_capture_enabled_does_not_touch_the_real_capture_log(
        self, tmp_path
    ) -> None:
        before = real_blackbox_snapshot()
        entry = _PERMISSION_REFUSALS[0]
        iso = make_isolation(tmp_path, session_id="out-capture-real-log-guard")
        _write_sentinel(iso)
        setup = entry.setup(iso)
        run_hook(entry.script, setup["envelope"], iso, extra_env=setup.get("extra_env"))
        after = real_blackbox_snapshot()
        assert after == before, (
            "a drill hook run with capture ON appended to the developer's REAL "
            f"capture log (before={before}, after={after})"
        )


class TestDeferredScriptsAreNotDoubleCovered:
    """Non-negotiable 5: the drill must not fake a trigger to manufacture coverage
    for a script the census declares as deferred. Asserted against the census's
    own declared split, never a hardcoded script list."""

    def test_no_deferred_script_also_carries_a_declared_refusal_entry(self) -> None:
        deferred = deferred_scripts()
        assert deferred, "deferred_scripts() derived nothing to check against"
        covered = refusing_scripts()
        # ANTI-VACUITY: an empty `covered` set would make `overlap` empty for free,
        # regardless of whether any deferred script is really double-covered, which
        # is the same "absence reads as a pass" shape as a missing function. This
        # is asserted separately so a broken refusing_scripts() fails HERE rather
        # than making the real assertion below spuriously true.
        assert covered, "refusing_scripts() derived nothing to check against"
        overlap = deferred & covered
        assert overlap == set(), (
            f"a script is both DEFERRED and declared with a real trigger: {overlap}; "
            "discharge the deferral instead of leaving both true"
        )


class TestEachPermissionRefusalCapturesExactlyItsOwnStdout:
    """Capability: every drill-declared refusal whose mechanism is
    permissionDecisionReason, run as a real subprocess with capture enabled in an
    isolated HOME, produces exactly one OUT row whose payload equals the hook's
    stdout."""

    @pytest.mark.parametrize(
        "entry", _PERMISSION_REFUSALS, ids=[e.id for e in _PERMISSION_REFUSALS]
    )
    def test_one_out_row_equals_the_hooks_actual_stdout(self, tmp_path, entry) -> None:
        iso = make_isolation(tmp_path, session_id=f"out-capture-{entry.id}")
        _write_sentinel(iso)
        setup = entry.setup(iso)
        result = run_hook(
            entry.script, setup["envelope"], iso, extra_env=setup.get("extra_env"),
        )

        assert result.returncode == 0, (
            f"{entry.id}: hook exited {result.returncode}, stderr={result.stderr!r}"
        )
        decision = result.permission_decision()
        assert decision == entry.permission_decision, (
            f"{entry.id}: expected permissionDecision={entry.permission_decision!r}, "
            f"got {decision!r}"
        )

        rows = _read_rows(iso)
        hook_label = entry.script[:-3] if entry.script.endswith(".sh") else entry.script
        out_rows = [
            r for r in rows if r.get("direction") == "out" and r.get("hook") == hook_label
        ]
        assert out_rows, (
            f"{entry.id}: no OUT row captured for hook={hook_label!r}; rows seen: "
            f"{rows!r}"
        )
        assert len(out_rows) == 1, (
            f"{entry.id}: expected exactly one OUT row, got {len(out_rows)}: {out_rows!r}"
        )
        assert out_rows[0]["payload"] == result.stdout, (
            f"{entry.id}: captured OUT payload is not byte-identical to the hook's "
            f"actual stdout. captured={out_rows[0]['payload']!r} "
            f"stdout={result.stdout!r}"
        )


@contextmanager
def _stub_pre_write_daemon(pre_write_response: dict, always_on_response: dict):
    """A loopback HTTP server answering POST /pre-write-check and GET /always-on.

    Non-negotiable 6: writ-pre-write-dispatch.sh's allow path needs BOTH routes
    reachable, or the hook takes the degenerate no-daemon arm and emits nothing at
    all, which would prove nothing about the byte-identity property under test.
    stub_analyze_server in the harness answers only POST on any path, so this is a
    small sibling built the same way (loopback HTTPServer on a background thread),
    not a second harness for the rest of the drill.

    If either route is ever unreachable through this stub, the calling test must
    fail loudly on the resulting empty stdout rather than skip: a skip here would
    silently stop proving the one property this file exists to prove.
    """
    pre_write_body = json.dumps(pre_write_response).encode("utf-8")
    always_on_body = json.dumps(always_on_response).encode("utf-8")

    class _Handler(http.server.BaseHTTPRequestHandler):
        def do_POST(self) -> None:  # noqa: N802 (stdlib-mandated name)
            length = int(self.headers.get("Content-Length", 0) or 0)
            self.rfile.read(length)
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(pre_write_body)))
            self.end_headers()
            self.wfile.write(pre_write_body)

        def do_GET(self) -> None:  # noqa: N802 (stdlib-mandated name)
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(always_on_body)))
            self.end_headers()
            self.wfile.write(always_on_body)

        def log_message(self, *args: object) -> None:  # silence stdlib access log
            return

    server = http.server.HTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server.server_address[1]
    finally:
        server.shutdown()
        thread.join(timeout=5)


_RAG_RULES_MARKER = "[TEST-RULE-MARKER] example applicable rule text for the allow path"


class TestPreWriteDispatchAllowPathCapturesThePrintedEnvelope:
    """Capability: writ-pre-write-dispatch.sh's allow-path OUT row is the
    additionalContext envelope it printed, not the concatenated rule prose (the
    live defect at line 297: it logs RAG_RULES_RAW + AO_WRITE_BLOCK instead of the
    envelope printed at lines 290 to 296, which is the committed
    unknown|writ-pre-write-dispatch|out class, count 68, empty
    hook_specific_output_keys and mechanisms)."""

    def test_the_out_row_is_the_printed_envelope_not_the_rule_prose(
        self, tmp_path
    ) -> None:
        iso = make_isolation(tmp_path, session_id="pre-write-allow-path")
        _write_sentinel(iso)
        target = iso.project_root / "src" / "widget.py"
        pre_write_response = {
            "decision": "allow",
            "reason": "",
            "rag_rules": _RAG_RULES_MARKER,
            "rag_meta": {"rule_ids": [], "tokens": 5},
            "mode": "work",
        }
        always_on_response = {"rules": []}
        envelope = {
            "session_id": iso.session_id,
            "hook_event_name": "PreToolUse",
            "tool_name": "Write",
            "tool_input": {"file_path": str(target), "content": "print('hi')\n"},
        }

        with _stub_pre_write_daemon(pre_write_response, always_on_response) as port:
            result = run_hook(
                "writ-pre-write-dispatch.sh", envelope, iso,
                extra_env={"WRIT_PORT": str(port), "WRIT_HOST": "127.0.0.1"},
            )

        assert result.returncode == 0, (
            f"hook exited {result.returncode}, stderr={result.stderr!r}"
        )
        assert result.stdout.strip(), (
            "no stdout at all; the hook took the degenerate no-daemon arm instead "
            "of the real allow path this test needs to exercise, so the stub is "
            "not answering both routes"
        )
        stdout_doc = result.stdout_json()
        assert stdout_doc is not None, f"stdout did not parse as JSON: {result.stdout!r}"
        hso = stdout_doc.get("hookSpecificOutput") or {}
        assert hso.get("hookEventName") == "PreToolUse", stdout_doc
        printed_context = hso.get("additionalContext") or ""
        assert _RAG_RULES_MARKER in printed_context, (
            f"the printed envelope does not carry the stubbed rule text: {stdout_doc!r}"
        )

        rows = _read_rows(iso)
        out_rows = [
            r for r in rows
            if r.get("direction") == "out" and r.get("hook") == "writ-pre-write-dispatch"
        ]
        assert out_rows, (
            f"no OUT row captured for writ-pre-write-dispatch; rows seen: {rows!r}"
        )
        assert len(out_rows) == 1, out_rows
        captured_payload = out_rows[0]["payload"]

        assert captured_payload == result.stdout, (
            "the captured OUT payload is not byte-identical to the hook's actual "
            f"stdout: captured={captured_payload!r} stdout={result.stdout!r}"
        )
        assert captured_payload != _RAG_RULES_MARKER, (
            "the captured row is the bare rule-prose reconstruction, not the "
            "additionalContext envelope actually printed; this is the exact "
            "defect at writ-pre-write-dispatch.sh:297"
        )
