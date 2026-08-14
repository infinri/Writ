"""POL-6f: approval_workflow -> writ/session/approval_workflow.py.

The phase-advance / gate-validation cluster (cmd_advance_phase/cmd_current_phase + the
validators + the _GATE_VALIDATORS registry) moves to approval_workflow.py, importing only
lower layers. _validate_citations moves with it (its sole caller is _validate_phase_a).

Per TEST-TDD-001: skeletons approved before implementation. RED until the move lands.
"""

from __future__ import annotations

import importlib
import importlib.util
import io
import json
import os
import secrets
import sys
import uuid

import pytest

from tests.fixtures.session_state import write_bound_gate_token

SKILL_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir))
FACADE_PATH = os.path.join(SKILL_ROOT, "bin", "lib", "writ-session.py")
AW_PATH = os.path.join(SKILL_ROOT, "writ", "session", "approval_workflow.py")

PLAN = """# Plan
## Files
- foo.py
## Analysis
What and why, contracts, integration points.
## Rules Applied
No matching rules.
## Capabilities
- [ ] does the thing
"""


def _load_facade():
    spec = importlib.util.spec_from_file_location("writ_session_pol6f", FACADE_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _imp(name):
    if SKILL_ROOT not in sys.path:
        sys.path.insert(0, SKILL_ROOT)
    return importlib.import_module(name)


def _seed(sid, **fields):
    cache = _imp("writ.session.cache")
    data = cache._read_cache(sid)
    data.update(fields)
    cache._write_cache(sid, data)


def _write_token(sid):
    # A BOUND token (gate + plan fingerprint), derived from the seeded cache the way the
    # production mint derives it: cmd_advance_phase refuses an unbound one-line token.
    return write_bound_gate_token(sid, secrets.token_hex(16))


def _advance(facade, sid, project_root, token, monkeypatch):
    monkeypatch.setattr("sys.stdin", io.StringIO("approved"))
    import contextlib
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        facade.cmd_advance_phase(sid, str(project_root), token)
    return json.loads(buf.getvalue().strip())


class TestModuleAndAcyclic:
    def test_module_exists(self):
        assert os.path.isfile(AW_PATH)

    def test_imports(self):
        assert _imp("writ.session.approval_workflow") is not None

    def test_does_not_import_facade(self):
        with open(AW_PATH) as f:
            lines = f.read().splitlines()
        imports = "\n".join(l for l in lines if l.strip().startswith(("import ", "from ")))
        assert "writ_session" not in imports and "writ-session" not in imports
        assert "spec_from_file_location" not in "\n".join(lines)

    def test_imports_lower_layers(self):
        with open(AW_PATH) as f:
            src = f.read()
        assert "from writ.session.cache import" in src
        assert "from writ.session.mode_engine import" in src
        assert "from writ.session.locators import" in src


class TestValidators:
    def test_validate_phase_a_complete_plan_passes(self, tmp_path):
        aw = _imp("writ.session.approval_workflow")
        (tmp_path / "plan.md").write_text(PLAN)
        assert aw._validate_phase_a(str(tmp_path), "") is None

    def test_validate_phase_a_missing_section_reported(self, tmp_path):
        aw = _imp("writ.session.approval_workflow")
        (tmp_path / "plan.md").write_text("# Plan\n## Files\n- a.py\n")
        err = aw._validate_phase_a(str(tmp_path), "")
        assert err is not None and "Analysis" in err

    def test_validate_citations_moved(self):
        aw = _imp("writ.session.approval_workflow")
        # cited rule absent from the loaded set -> flagged; empty loaded -> nothing provable
        assert aw._validate_citations({"FOO-001"}, {"BAR-001"}) == {"FOO-001"}
        assert aw._validate_citations({"FOO-001"}, set()) == set()

    def test_validate_test_skeletons_finds_marked_test(self, tmp_path):
        aw = _imp("writ.session.approval_workflow")
        td = tmp_path / "tests"
        td.mkdir()
        (td / "test_x.py").write_text("def test_foo():\n    assert True\n")
        assert aw._validate_test_skeletons(str(tmp_path), "") is None


class TestAdvancePhaseBehavior:
    def test_bad_token_does_not_advance(self, tmp_path, monkeypatch):
        monkeypatch.setenv("WRIT_CACHE_DIR", str(tmp_path / "cache"))
        (tmp_path / "cache").mkdir()
        f = _load_facade()
        sid = f"aw-{uuid.uuid4().hex[:8]}"
        _seed(sid, mode="work", current_phase="planning", gates_approved=[])
        (tmp_path / "plan.md").write_text(PLAN)
        r = _advance(f, sid, tmp_path, "wrong-token", monkeypatch)
        assert r["advanced"] is False
        assert "token" in r["reason"].lower()

    def test_phase_a_then_test_skeletons_advance(self, tmp_path, monkeypatch):
        monkeypatch.setenv("WRIT_CACHE_DIR", str(tmp_path / "cache"))
        (tmp_path / "cache").mkdir()
        f = _load_facade()
        sid = f"aw-{uuid.uuid4().hex[:8]}"
        _seed(sid, mode="work", current_phase="planning", gates_approved=[])
        (tmp_path / "plan.md").write_text(PLAN)
        token = _write_token(sid)

        r1 = _advance(f, sid, tmp_path, token, monkeypatch)
        assert r1["advanced"] is True
        assert r1["gate"] == "phase-a"
        assert r1["phase"] == "testing"

        # Now the test-skeletons gate: a marked test file must exist in the project.
        td = tmp_path / "tests"
        td.mkdir()
        (td / "test_x.py").write_text("def test_foo():\n    assert True\n")
        token2 = _write_token(sid)
        r2 = _advance(f, sid, tmp_path, token2, monkeypatch)
        assert r2["advanced"] is True
        assert r2["gate"] == "test-skeletons"
        assert r2["phase"] == "implementation"

    def test_current_phase_reports(self, tmp_path, monkeypatch, capsys):
        monkeypatch.setenv("WRIT_CACHE_DIR", str(tmp_path / "cache"))
        (tmp_path / "cache").mkdir()
        f = _load_facade()
        sid = f"aw-{uuid.uuid4().hex[:8]}"
        _seed(sid, mode="work", current_phase="testing")
        f.cmd_current_phase(sid)
        out = capsys.readouterr().out
        assert "testing" in out


class TestSourceShape:
    def test_facade_no_inline_defs(self):
        with open(FACADE_PATH) as f:
            src = f.read()
        assert "def cmd_advance_phase(" not in src
        assert "def _validate_phase_a(" not in src
        assert "def _validate_citations(" not in src
        assert "from writ.session.approval_workflow import" in src

    def test_module_defines_core(self):
        with open(AW_PATH) as f:
            src = f.read()
        assert "def cmd_advance_phase(" in src
        assert "_GATE_VALIDATORS" in src
        assert "def _validate_citations(" in src
