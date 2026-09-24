"""#4: NotebookEdit must be gated like any other write.

NotebookEdit uses notebook_path/new_source instead of file_path/content. Before #4
that shape slipped past the whole stack: the hook parser and the server gate both
read file_path only, so a notebook cell edit resolved to an empty path and the work
gate (which blocks ALL writes before plan approval) allowed it -- an unintentional
bypass. This pins the normalization (parser + gate) and the gate decision.
"""
from __future__ import annotations

import importlib
import importlib.util
import json
import os
import subprocess
import sys
import uuid

import pytest

SKILL_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir))
PARSE_PY = os.path.join(SKILL_ROOT, "bin", "lib", "parse-hook-stdin.py")
HOOKS_JSON = os.path.join(SKILL_ROOT, "hooks", "hooks.json")

NB_ENVELOPE = {
    "tool_name": "NotebookEdit",
    "tool_input": {
        "notebook_path": "/app/analysis.ipynb",
        "new_source": "pw = get_secret('DB_PASSWORD')",
        "cell_type": "code",
        "edit_mode": "replace",
    },
}


def _imp(name):
    if SKILL_ROOT not in sys.path:
        sys.path.insert(0, SKILL_ROOT)
    return importlib.import_module(name)


def _seed(sid, **fields):
    cache = _imp("writ.session.cache")
    data = cache._read_cache(sid)
    data.update(fields)
    cache._write_cache(sid, data)


class TestParserNormalizesNotebookEdit:
    def test_file_path_and_content_flattened(self):
        out = subprocess.run(
            ["python3", PARSE_PY], input=json.dumps(NB_ENVELOPE),
            capture_output=True, text=True, timeout=10,
        ).stdout
        d = json.loads(out)
        assert d["file_path"] == "/app/analysis.ipynb"
        assert d["content"] == "pw = get_secret('DB_PASSWORD')"

    def test_shell_form_sets_hook_file_path(self):
        out = subprocess.run(
            ["python3", PARSE_PY, "--shell"], input=json.dumps(NB_ENVELOPE),
            capture_output=True, text=True, timeout=10,
        ).stdout
        assert "HOOK_FILE_PATH=/app/analysis.ipynb" in out

    def test_delete_cell_has_no_content(self):
        env = {"tool_name": "NotebookEdit",
               "tool_input": {"notebook_path": "/a.ipynb", "edit_mode": "delete", "cell_id": "c1"}}
        out = subprocess.run(["python3", PARSE_PY], input=json.dumps(env),
                             capture_output=True, text=True, timeout=10).stdout
        d = json.loads(out)
        assert d["file_path"] == "/a.ipynb"
        assert d["content"] == ""


class TestGateParsesNotebookPath:
    def test_parse_file_path_picks_notebook_path(self):
        gates = _imp("writ.session.gates")
        assert gates._parse_file_path_from_envelope(NB_ENVELOPE) == "/app/analysis.ipynb"

    def test_file_path_still_wins_when_both_present(self):
        gates = _imp("writ.session.gates")
        env = {"tool_input": {"file_path": "/x.py", "notebook_path": "/y.ipynb"}}
        assert gates._parse_file_path_from_envelope(env) == "/x.py"


class TestNotebookEditGateDecision:
    def test_work_mode_blocks_notebook_edit_before_plan(self):
        gates = _imp("writ.session.gates")
        sid = f"nb-gate-{uuid.uuid4().hex[:8]}"
        _seed(sid, mode="work", approved_gates=[], current_phase="planning")
        res = gates._can_write_check(sid, NB_ENVELOPE, skill_dir=SKILL_ROOT)
        assert res["can_write"] is False
        assert "ENF-GATE" in (res["reason"] or "")

    def test_non_work_mode_allows_notebook_edit(self):
        gates = _imp("writ.session.gates")
        sid = f"nb-inv-{uuid.uuid4().hex[:8]}"
        _seed(sid, mode="investigate")
        res = gates._can_write_check(sid, NB_ENVELOPE, skill_dir=SKILL_ROOT)
        assert res["can_write"] is True


class TestMatchersWired:
    def test_notebookedit_in_pre_and_post_write_matchers(self):
        data = json.loads(open(HOOKS_JSON).read())["hooks"]
        def _scripts_for(event, tool):
            out = []
            for g in data.get(event, []):
                matcher = g.get("matcher", "")
                if tool in matcher.split("|"):
                    out += [h["command"].rstrip('"').rsplit("/", 1)[-1] for h in g.get("hooks", [])]
            return out
        assert "writ-pre-write-dispatch.sh" in _scripts_for("PreToolUse", "NotebookEdit")
        assert "writ-posttool-rag.sh" in _scripts_for("PostToolUse", "NotebookEdit")
