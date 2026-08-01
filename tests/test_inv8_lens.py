"""INV-8: unify the four lenses under one source_type switch (closing increment).

audit (code) / explore (code) / research (web) / debug (runtime) are one engine
selected by source_type. INV-8 retrofits debug as the runtime lens, lets investigate
set its lens per-invocation, and adds `lens` -- the single switch mapping source_type
to the lens and the gate that enforces it.

Loads writ-session.py as a module (mirrors tests/test_inv4_coverage_map.py) and uses
the ingest API for the spine-doctrine edge (mirrors tests/test_inv6a_fanout.py).
"""
from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path

import pytest

from writ.graph.ingest import parse_edges_from_file

from tests._bible_guard import requires_bible

pytestmark = requires_bible


HELPER_PATH = os.path.join(os.path.dirname(__file__), os.pardir, "bin", "lib", "writ-session.py")
_spec = importlib.util.spec_from_file_location("writ_session_inv8", HELPER_PATH)
writ_session = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(writ_session)

SKILL_DIR = Path(__file__).resolve().parent.parent
RESEARCH_PATH = SKILL_DIR / "bible" / "methodology" / "PBK-PROC-RESEARCH-001.md"
SID = "test-inv8-lens"


def _seed(monkeypatch, tmp_path, mode, source_type=None):
    monkeypatch.setenv("WRIT_CACHE_DIR", str(tmp_path))
    cache = {"session_id": SID, "mode": mode, "citation_log": []}
    if source_type is not None:
        cache["source_type"] = source_type
    with open(writ_session._cache_path(SID), "w") as f:
        json.dump(cache, f)


def _lens(capsys):
    capsys.readouterr()
    writ_session.cmd_lens(SID)
    return json.loads(capsys.readouterr().out)


class TestDebugRetrofit:
    def test_debug_source_type_is_runtime(self) -> None:
        assert writ_session.MODE_CONFIG["debug"].get("source_type") == "runtime"
        assert writ_session._source_type_for_mode("debug") == "runtime"

    def test_debug_gate_sequence_unchanged(self) -> None:
        assert list(writ_session.MODE_CONFIG["debug"]["gate_sequence"]) == []


class TestSetSourceType:
    def test_set_valid_source_type(self, tmp_path, monkeypatch) -> None:
        _seed(monkeypatch, tmp_path, "investigate")
        writ_session.cmd_update(SID, ["--set-source-type", "web"])
        assert writ_session._read_cache(SID).get("source_type") == "web"

    def test_invalid_source_type_ignored(self, tmp_path, monkeypatch) -> None:
        _seed(monkeypatch, tmp_path, "investigate")
        writ_session.cmd_update(SID, ["--set-source-type", "banana"])
        assert writ_session._read_cache(SID).get("source_type") in (None, "")


class TestEffectiveSourceType:
    def test_session_value_wins_for_investigate(self, tmp_path, monkeypatch) -> None:
        _seed(monkeypatch, tmp_path, "investigate", "code")
        assert writ_session._effective_source_type(writ_session._read_cache(SID)) == "code"

    def test_debug_mode_defaults_to_runtime(self, tmp_path, monkeypatch) -> None:
        _seed(monkeypatch, tmp_path, "debug")
        assert writ_session._effective_source_type(writ_session._read_cache(SID)) == "runtime"

    def test_investigate_unset_is_none(self, tmp_path, monkeypatch) -> None:
        _seed(monkeypatch, tmp_path, "investigate")
        assert writ_session._effective_source_type(writ_session._read_cache(SID)) is None


class TestLensSwitch:
    def test_web_lens_is_hard_triangulation(self, tmp_path, capsys, monkeypatch) -> None:
        _seed(monkeypatch, tmp_path, "investigate", "web")
        lens = _lens(capsys)
        assert lens["source_type"] == "web"
        assert lens["enforcing_gate"] == "triangulation-gate"
        assert lens["gate_strictness"] == "hard"
        assert "research" in lens["lens"].lower()

    def test_code_lens_is_advisory_synthesis(self, tmp_path, capsys, monkeypatch) -> None:
        _seed(monkeypatch, tmp_path, "investigate", "code")
        lens = _lens(capsys)
        assert lens["enforcing_gate"] == "synthesis-gate"
        assert lens["gate_strictness"] == "advisory"
        assert "audit" in lens["lens"].lower() or "explore" in lens["lens"].lower()

    def test_runtime_lens_is_root_cause(self, tmp_path, capsys, monkeypatch) -> None:
        _seed(monkeypatch, tmp_path, "debug")
        lens = _lens(capsys)
        assert lens["source_type"] == "runtime"
        assert "root-cause" in lens["enforcing_gate"]
        assert "debug" in lens["lens"].lower()

    def test_no_lens_when_unset(self, tmp_path, capsys, monkeypatch) -> None:
        _seed(monkeypatch, tmp_path, "investigate")
        lens = _lens(capsys)
        assert lens["status"] == "no_lens"


class TestSpineDoctrine:
    def test_spine_invokes_debug_runtime_lens(self) -> None:
        # 1.3b: the runtime lens PBK-PROC-DEBUG-001 is a Playbook, so the spine
        # INVOKES it (applies inline) rather than DISPATCHES (reserved for roles).
        assert RESEARCH_PATH.exists(), f"{RESEARCH_PATH} missing"
        edges = parse_edges_from_file(RESEARCH_PATH)
        invoke = {e["target"] for e in edges if e.get("type") == "INVOKES"}
        assert "PBK-PROC-DEBUG-001" in invoke, (
            f"the spine must INVOKES the runtime lens PBK-PROC-DEBUG-001; got {sorted(invoke)}"
        )

    def test_spine_enumerates_four_lenses_and_source_types(self) -> None:
        body = RESEARCH_PATH.read_text(encoding="utf-8").lower()
        for lens in ("audit", "explore", "research", "debug"):
            assert lens in body, f"spine must name the {lens} lens"
        for st in ("code", "web", "runtime"):
            assert st in body, f"spine must name the {st} source_type"
