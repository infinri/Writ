"""#5: Glob is gated by the runtime-lens read-evidence gate.

Glob (file enumeration) previously skipped writ-debug-code-gate entirely (it was
not in the Grep|Read matcher, and the gate's tool switch fell through to allow).
In the runtime/debug lens (debug.md lacking Evidence + Narrowing), a source hunt
(**/*.py) is premature code exploration and is blocked; a log/doc/navigation glob
(**/*.log, src/**) is allowed -- classified by the pattern's extension, fail-open.
"""
from __future__ import annotations

import importlib
import json
import os
import sys
import uuid
from pathlib import Path

import pytest

SKILL_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir))
HOOKS_JSON = os.path.join(SKILL_ROOT, "hooks", "hooks.json")


def _imp(name):
    if SKILL_ROOT not in sys.path:
        sys.path.insert(0, SKILL_ROOT)
    return importlib.import_module(name)


def _seed(sid, **fields):
    cache = _imp("writ.session.cache")
    data = cache._read_cache(sid)
    data.update(fields)
    cache._write_cache(sid, data)


def _glob_env(pattern, path):
    return {"tool_name": "Glob", "tool_input": {"pattern": pattern, "path": path}}


class TestGlobReadGate:
    def test_source_glob_blocked_in_debug_lens(self, tmp_path: Path):
        gates = _imp("writ.session.gates")
        sid = f"glob-{uuid.uuid4().hex[:8]}"
        _seed(sid, mode="debug")  # debug -> runtime source_type, no debug.md -> lens closed
        res = gates._can_read_code_check(sid, _glob_env("**/*.py", str(tmp_path)), SKILL_ROOT)
        assert res["can_read"] is False
        assert "DEBUG-EVIDENCE-FIRST" in (res["reason"] or "")

    def test_log_glob_allowed_in_debug_lens(self, tmp_path: Path):
        gates = _imp("writ.session.gates")
        sid = f"glob-{uuid.uuid4().hex[:8]}"
        _seed(sid, mode="debug")
        res = gates._can_read_code_check(sid, _glob_env("**/*.log", str(tmp_path)), SKILL_ROOT)
        assert res["can_read"] is True

    def test_no_extension_glob_allowed(self, tmp_path: Path):
        # src/** has no clear code extension -> fail-open allow (navigation).
        gates = _imp("writ.session.gates")
        sid = f"glob-{uuid.uuid4().hex[:8]}"
        _seed(sid, mode="debug")
        res = gates._can_read_code_check(sid, _glob_env("src/**", str(tmp_path)), SKILL_ROOT)
        assert res["can_read"] is True

    def test_non_runtime_mode_allows_source_glob(self, tmp_path: Path):
        gates = _imp("writ.session.gates")
        sid = f"glob-{uuid.uuid4().hex[:8]}"
        _seed(sid, mode="investigate")  # not runtime source_type by default
        res = gates._can_read_code_check(sid, _glob_env("**/*.py", str(tmp_path)), SKILL_ROOT)
        assert res["can_read"] is True


class TestGlobSearchDir:
    def test_glob_search_dir_uses_path(self):
        gates = _imp("writ.session.gates")
        assert gates._resolve_read_search_dir("Glob", {"path": "/proj/src"}) == "/proj/src"


class TestMatcherWired:
    def test_glob_in_debug_code_gate_matcher(self):
        data = json.loads(open(HOOKS_JSON).read())["hooks"]
        scripts = []
        for g in data.get("PreToolUse", []):
            if "Glob" in g.get("matcher", "").split("|"):
                scripts += [h["command"].rstrip('"').rsplit("/", 1)[-1] for h in g.get("hooks", [])]
        assert "writ-debug-code-gate.sh" in scripts
