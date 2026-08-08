"""INV-9: defer code-reading until runtime evidence (the debug-lens "code last" gate).

DEBUG-MODE-PROPOSAL.md line 126 hook #2: in the runtime lens, block code search/reading
until debug.md has Evidence + Narrowing content. Runtime data (Bash) and reading
debug.md/logs/non-code stay allowed; the gate is fail-open and runtime-lens-only.

Loads writ-session.py as a module (mirrors tests/test_inv4_coverage_map.py); the hook
e2e drives writ-debug-code-gate.sh with synthetic PreToolUse envelopes.
"""
from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import uuid
from pathlib import Path

import pytest

HELPER_PATH = os.path.join(os.path.dirname(__file__), os.pardir, "bin", "lib", "writ-session.py")
_spec = importlib.util.spec_from_file_location("writ_session_inv9", HELPER_PATH)
writ_session = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(writ_session)

SKILL_DIR = Path(__file__).resolve().parent.parent
HOOK = str(SKILL_DIR / "hooks" / "scripts" / "writ-debug-code-gate.sh")
PLUGIN_HOOKS = SKILL_DIR / "hooks" / "hooks.json"
SID = "test-inv9-codegate"

EVIDENCE_FULL = (
    "## Symptom\nslow checkout\n\n"
    "## Evidence\nStack trace at app.py:42; log 2026-06-01T10:00 shows a 3s query.\n\n"
    "## Narrowing\nOnly the /checkout endpoint, SKU pattern ABC-*.\n\n"
    "## Root cause\n\n"
)
EVIDENCE_EMPTY = "## Symptom\nx\n\n## Evidence\n\n## Narrowing\n\n## Root cause\n\n"
EVIDENCE_PLACEHOLDER = (
    "## Evidence\n<runtime data points: a stack trace, a log line>\n\n"
    "## Narrowing\n<the smallest affected unit>\n\n"
)
EVIDENCE_MISSING_NARROWING = "## Evidence\nreal trace here\n\n## Root cause\n\n"


def _proj(tmp_path, debug_md=None):
    proj = tmp_path / "proj"
    (proj / ".git").mkdir(parents=True, exist_ok=True)
    if debug_md is not None:
        (proj / "debug.md").write_text(debug_md)
    return proj


def _seed(monkeypatch, tmp_path, mode="debug", source_type=None):
    cache_dir = tmp_path / "cache"
    cache_dir.mkdir(exist_ok=True)
    monkeypatch.setenv("WRIT_CACHE_DIR", str(cache_dir))
    cache = {"session_id": SID, "mode": mode, "citation_log": []}
    if source_type is not None:
        cache["source_type"] = source_type
    with open(writ_session._cache_path(SID), "w") as f:
        json.dump(cache, f)


def _check(tool_name, file_path=None, pattern=None, path=None):
    ti = {}
    if file_path is not None:
        ti["file_path"] = file_path
    if pattern is not None:
        ti["pattern"] = pattern
    if path is not None:
        ti["path"] = path
    env = {"session_id": SID, "tool_name": tool_name, "tool_input": ti}
    return writ_session._can_read_code_check(SID, env, "")


class TestValidateEvidenceNarrowing:
    def test_full_passes(self, tmp_path) -> None:
        p = _proj(tmp_path, EVIDENCE_FULL) / "debug.md"
        assert writ_session._validate_evidence_narrowing(str(p)) is None

    def test_empty_section_fails(self, tmp_path) -> None:
        p = _proj(tmp_path, EVIDENCE_EMPTY) / "debug.md"
        assert writ_session._validate_evidence_narrowing(str(p)) is not None

    def test_placeholder_only_fails(self, tmp_path) -> None:
        p = _proj(tmp_path, EVIDENCE_PLACEHOLDER) / "debug.md"
        assert writ_session._validate_evidence_narrowing(str(p)) is not None

    def test_missing_narrowing_fails(self, tmp_path) -> None:
        p = _proj(tmp_path, EVIDENCE_MISSING_NARROWING) / "debug.md"
        assert writ_session._validate_evidence_narrowing(str(p)) is not None


class TestGateDeniesPreEvidence:
    def test_grep_denied(self, tmp_path, monkeypatch) -> None:
        _seed(monkeypatch, tmp_path, "debug")
        proj = _proj(tmp_path, EVIDENCE_EMPTY)
        assert _check("Grep", pattern="foo", path=str(proj))["can_read"] is False

    def test_source_read_denied(self, tmp_path, monkeypatch) -> None:
        _seed(monkeypatch, tmp_path, "debug")
        proj = _proj(tmp_path, EVIDENCE_EMPTY)
        assert _check("Read", file_path=str(proj / "app.py"))["can_read"] is False


class TestGateAllowsEvidenceGathering:
    def test_read_debug_md_allowed(self, tmp_path, monkeypatch) -> None:
        _seed(monkeypatch, tmp_path, "debug")
        proj = _proj(tmp_path, EVIDENCE_EMPTY)
        assert _check("Read", file_path=str(proj / "debug.md"))["can_read"] is True

    def test_read_log_allowed(self, tmp_path, monkeypatch) -> None:
        _seed(monkeypatch, tmp_path, "debug")
        proj = _proj(tmp_path, EVIDENCE_EMPTY)
        assert _check("Read", file_path=str(proj / "server.log"))["can_read"] is True

    def test_read_test_file_allowed(self, tmp_path, monkeypatch) -> None:
        _seed(monkeypatch, tmp_path, "debug")
        proj = _proj(tmp_path, EVIDENCE_EMPTY)
        assert _check("Read", file_path=str(proj / "tests" / "test_app.py"))["can_read"] is True


class TestGateOpensWithEvidence:
    def test_source_read_allowed_with_evidence(self, tmp_path, monkeypatch) -> None:
        _seed(monkeypatch, tmp_path, "debug")
        proj = _proj(tmp_path, EVIDENCE_FULL)
        assert _check("Read", file_path=str(proj / "app.py"))["can_read"] is True

    def test_grep_allowed_with_evidence(self, tmp_path, monkeypatch) -> None:
        _seed(monkeypatch, tmp_path, "debug")
        proj = _proj(tmp_path, EVIDENCE_FULL)
        assert _check("Grep", pattern="foo", path=str(proj))["can_read"] is True


class TestLensScoping:
    def test_work_mode_allows_code_read(self, tmp_path, monkeypatch) -> None:
        _seed(monkeypatch, tmp_path, "work")
        proj = _proj(tmp_path, EVIDENCE_EMPTY)
        assert _check("Read", file_path=str(proj / "app.py"))["can_read"] is True

    def test_investigate_nonruntime_allows(self, tmp_path, monkeypatch) -> None:
        _seed(monkeypatch, tmp_path, "investigate", source_type="code")
        proj = _proj(tmp_path, EVIDENCE_EMPTY)
        assert _check("Read", file_path=str(proj / "app.py"))["can_read"] is True

    def test_investigate_runtime_denies(self, tmp_path, monkeypatch) -> None:
        _seed(monkeypatch, tmp_path, "investigate", source_type="runtime")
        proj = _proj(tmp_path, EVIDENCE_EMPTY)
        assert _check("Read", file_path=str(proj / "app.py"))["can_read"] is False


class TestFailOpen:
    def test_malformed_read_allowed(self, tmp_path, monkeypatch) -> None:
        _seed(monkeypatch, tmp_path, "debug")
        _proj(tmp_path, EVIDENCE_EMPTY)
        # No file_path -> cannot determine target -> allow (never wedge).
        assert _check("Read")["can_read"] is True


class TestHookStructureAndE2E:
    def test_hook_references_tools_and_check(self) -> None:
        assert Path(HOOK).exists(), f"{HOOK} does not exist yet"
        body = Path(HOOK).read_text()
        assert "Grep" in body and "Read" in body
        assert "can-read-code" in body
        assert "deny" in body

    def test_e2e_grep_denied_in_runtime_lens(self, tmp_path, monkeypatch) -> None:
        _seed(monkeypatch, tmp_path, "debug")
        proj = _proj(tmp_path, EVIDENCE_EMPTY)
        env = {**os.environ, "WRIT_CACHE_DIR": str(tmp_path / "cache")}
        envelope = {"session_id": SID, "tool_name": "Grep",
                    "tool_input": {"pattern": "foo", "path": str(proj)}}
        r = subprocess.run(["bash", HOOK], input=json.dumps(envelope),
                           capture_output=True, text=True, env=env, cwd=str(proj), timeout=20)
        assert r.returncode == 0, f"stderr={r.stderr[:500]}"
        assert '"permissionDecision"' in r.stdout and '"deny"' in r.stdout, \
            f"Grep in runtime lens pre-evidence must be denied; stdout={r.stdout[:400]}"

    def test_e2e_log_read_not_denied(self, tmp_path, monkeypatch) -> None:
        _seed(monkeypatch, tmp_path, "debug")
        proj = _proj(tmp_path, EVIDENCE_EMPTY)
        env = {**os.environ, "WRIT_CACHE_DIR": str(tmp_path / "cache")}
        envelope = {"session_id": SID, "tool_name": "Read",
                    "tool_input": {"file_path": str(proj / "server.log")}}
        r = subprocess.run(["bash", HOOK], input=json.dumps(envelope),
                           capture_output=True, text=True, env=env, cwd=str(proj), timeout=20)
        assert r.returncode == 0
        assert '"deny"' not in r.stdout, f"reading a .log must not be denied; stdout={r.stdout[:400]}"


class TestTelemetryIsKeyedToTheRealSession:
    """This gate's own rows must be attributable to the session that produced them.

    THE DEFECT, measured live 2026-08-08. The hook calls hook_instrument but set neither
    SESSION_ID nor HOOK_SESSION_ID, and the exit trap keys every buffered row on exactly
    those (`${SESSION_ID:-${HOOK_SESSION_ID:-}}`), which falls through to the literal id
    "unknown". writ-events-unknown.buf held ~153 hook_execution rows, dominated by this
    gate: real telemetry, from real sessions, permanently unattributable.

    load_hook_env is not the fix here -- it READS STDIN, and the hook has already
    consumed the envelope into $STDIN_DATA -- so the id comes from the payload the hook
    already parsed, and from nowhere else.
    """

    def _run(self, tmp_path, envelope, proj):
        env = {**os.environ,
               "WRIT_CACHE_DIR": str(tmp_path / "cache"),
               "WRIT_LOG_ROOT": str(tmp_path / "logs")}
        return subprocess.run(["bash", HOOK], input=json.dumps(envelope),
                              capture_output=True, text=True, env=env,
                              cwd=str(proj), timeout=20)

    def _buffers(self, tmp_path):
        return {p.name: p.read_text(errors="replace")
                for p in (tmp_path / "cache").glob("writ-events-*.buf")}

    def test_the_buffered_row_is_keyed_to_the_payloads_session(
        self, tmp_path, monkeypatch
    ) -> None:
        _seed(monkeypatch, tmp_path, "debug")
        proj = _proj(tmp_path, EVIDENCE_EMPTY)
        envelope = {"session_id": SID, "tool_name": "Grep",
                    "tool_input": {"pattern": "foo", "path": str(proj)}}

        result = self._run(tmp_path, envelope, proj)

        assert result.returncode == 0, f"stderr={result.stderr[:400]}"
        buffers = self._buffers(tmp_path)
        assert f"writ-events-{SID}.buf" in buffers, (
            f"this gate's telemetry was not filed under the session that ran it: "
            f"{sorted(buffers)}"
        )
        assert "writ-events-unknown.buf" not in buffers, (
            "rows still land under the literal id 'unknown' and cannot be traced back "
            "to any session"
        )
        assert "writ-debug-code-gate" in buffers[f"writ-events-{SID}.buf"]

    def test_a_subagents_read_is_filed_under_the_agent_not_its_parent(
        self, tmp_path, monkeypatch
    ) -> None:
        """agent_id wins over session_id, matching load_hook_env. Filing a sub-agent's
        reads under its parent would silently merge two sessions' telemetry."""
        _seed(monkeypatch, tmp_path, "debug")
        proj = _proj(tmp_path, EVIDENCE_EMPTY)
        envelope = {"session_id": "parent-of-" + SID, "agent_id": SID,
                    "tool_name": "Grep",
                    "tool_input": {"pattern": "foo", "path": str(proj)}}

        self._run(tmp_path, envelope, proj)

        buffers = self._buffers(tmp_path)
        assert f"writ-events-{SID}.buf" in buffers, sorted(buffers)
        assert f"writ-events-parent-of-{SID}.buf" not in buffers, (
            "the sub-agent's read was filed under its parent session"
        )

    def test_the_allowed_path_is_keyed_too(self, tmp_path, monkeypatch) -> None:
        """Anti-vacuity: the deny path is the loud one, and it is the quiet allow that
        produces the volume. Both must be attributable."""
        _seed(monkeypatch, tmp_path, "debug")
        proj = _proj(tmp_path, EVIDENCE_FULL)
        envelope = {"session_id": SID, "tool_name": "Read",
                    "tool_input": {"file_path": str(proj / "app.py")}}

        result = self._run(tmp_path, envelope, proj)

        assert '"deny"' not in result.stdout, "this case must be the allow path"
        buffers = self._buffers(tmp_path)
        assert f"writ-events-{SID}.buf" in buffers, sorted(buffers)
        assert "writ-events-unknown.buf" not in buffers


class TestRegistration:
    def _registers(self, manifest_path: Path) -> bool:
        data = json.loads(manifest_path.read_text())
        hooks = data.get("hooks", data)
        pre = hooks.get("PreToolUse", []) if isinstance(hooks, dict) else []
        for entry in pre:
            matcher = entry.get("matcher", "")
            cmds = " ".join(h.get("command", "") for h in entry.get("hooks", []))
            if ("Grep" in matcher or "Read" in matcher) and "writ-debug-code-gate" in cmds:
                return True
        return False

    def test_plugin_registers(self) -> None:
        assert self._registers(PLUGIN_HOOKS), \
            "hooks/hooks.json must register writ-debug-code-gate under a Grep|Read PreToolUse matcher"
