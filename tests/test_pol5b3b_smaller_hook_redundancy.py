"""POL-5b-3b: collapse redundant round-trips / parses in the 3 smaller hooks.

  A. writ-read-rag.sh   -- reuse $MODE (drop 2nd `mode get`); META double-parse -> 1
  B. auto-approve-gate.sh -- defer PROJECT_ROOT + CURRENT_MODE behind the approval
                             gate (gate-first); the common non-approval prompt does
                             neither the python walk nor the mode round-trip
  C. writ-posttool-rag.sh -- META double-parse -> 1

Source-shape guards prove the redundancy is gone; behavioral guards (run each hook
via bash against the live server) prove nothing broke.

RED until the three hooks are refactored.
"""

from __future__ import annotations

import importlib.util
import json
import os
import re
import subprocess
import tempfile
import uuid
from pathlib import Path

import pytest

from tests.fixtures.session_state import write_evidence_transcript

SKILL_DIR = Path(__file__).resolve().parent.parent
HOOKS = SKILL_DIR / "hooks" / "scripts"
WRIT_SESSION_PY = str(SKILL_DIR / "bin" / "lib" / "writ-session.py")

READRAG = HOOKS / "writ-read-rag.sh"
AUTOAPPROVE = HOOKS / "auto-approve-gate.sh"
POSTTOOL = HOOKS / "writ-posttool-rag.sh"

READRAG_SRC = READRAG.read_text()
AUTOAPPROVE_SRC = AUTOAPPROVE.read_text()
POSTTOOL_SRC = POSTTOOL.read_text()

SAMPLE_SOURCE = str(SKILL_DIR / "writ" / "server.py")  # detect_language -> python


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def _load_writ_session():
    spec = importlib.util.spec_from_file_location("writ_session_5b3b", WRIT_SESSION_PY)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _server_up() -> bool:
    try:
        import urllib.request

        from tests._daemon import _health_url

        with urllib.request.urlopen(_health_url(), timeout=1) as r:
            return r.status == 200
    except Exception:
        return False


requires_server = pytest.mark.skipif(not _server_up(), reason="writ server not running")


def _run(hook: Path, envelope: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["bash", str(hook)],
        input=envelope,
        capture_output=True,
        text=True,
        cwd=str(SKILL_DIR),
        env={**os.environ, "WRIT_HOST": "localhost"},
        timeout=25,
    )


@pytest.fixture()
def seeded():
    mod = _load_writ_session()
    sid = f"test-5b3b-{uuid.uuid4().hex[:8]}"

    def seed(**fields):
        cache = mod._read_cache(sid)
        cache.update(fields)
        mod._write_cache(sid, cache)
        return sid

    yield sid, seed
    p = Path(tempfile.gettempdir()) / f"writ-session-{sid}.json"
    if p.exists():
        p.unlink()


def _no_py_crash(r: subprocess.CompletedProcess) -> None:
    assert "Traceback" not in r.stderr, f"python traceback:\n{r.stderr[:500]}"
    assert "SyntaxError" not in r.stderr, f"python SyntaxError:\n{r.stderr[:500]}"


# --------------------------------------------------------------------------- #
# A. writ-read-rag.sh source-shape
# --------------------------------------------------------------------------- #
class TestReadRagShape:
    def test_single_mode_get_call(self) -> None:
        n = READRAG_SRC.count('$(_writ_session "mode get"')
        assert n == 1, f"read-rag must fetch mode once and reuse it; found {n} `mode get` calls"

    def test_meta_double_parse_collapsed(self) -> None:
        # D-WRITMETA-SH: routed through the centralized parse_writ_meta helper.
        n = READRAG_SRC.count('echo "$META_JSON" | parse_writ_meta')
        assert n == 1, f"read-rag META rule_ids+cost must route through parse_writ_meta once; found {n}"


# --------------------------------------------------------------------------- #
# B. auto-approve-gate.sh source-shape -- deferred past the gate
# --------------------------------------------------------------------------- #
class TestAutoApproveShape:
    # The fetches must be deferred behind the approval-relevance gate. Today they
    # sit at column 0 (unconditional, run on every turn). After the defer they are
    # indented inside an `if approval-related` block -- so no top-level occurrence.
    def test_mode_get_not_unconditional(self) -> None:
        assert re.search(r'^CURRENT_MODE=\$\(_writ_session "mode get"', AUTOAPPROVE_SRC, re.M) is None, (
            "auto-approve must not fetch mode unconditionally (column 0); it belongs "
            "inside the approval-relevance gate"
        )

    def test_project_root_not_unconditional(self) -> None:
        assert re.search(r"^PROJECT_ROOT=\$\(python3", AUTOAPPROVE_SRC, re.M) is None, (
            "auto-approve must not compute PROJECT_ROOT unconditionally (column 0); it "
            "belongs inside the approval-relevance gate"
        )


# --------------------------------------------------------------------------- #
# C. writ-posttool-rag.sh source-shape
# --------------------------------------------------------------------------- #
class TestPosttoolShape:
    def test_meta_double_parse_collapsed(self) -> None:
        # D-WRITMETA-SH: routed through the centralized parse_writ_meta helper.
        n = POSTTOOL_SRC.count('echo "$META_JSON" | parse_writ_meta')
        assert n == 1, f"posttool-rag META rule_ids+cost must route through parse_writ_meta once; found {n}"


# --------------------------------------------------------------------------- #
# behavioral guards (live server)
# --------------------------------------------------------------------------- #
@requires_server
class TestReadRagBehavior:
    def test_review_mode_reads_source(self, seeded) -> None:
        sid, seed = seeded
        seed(mode="review")
        env = json.dumps({
            "session_id": sid,
            "hook_event_name": "PreToolUse",
            "tool_name": "Read",
            "tool_input": {"file_path": SAMPLE_SOURCE},
        })
        r = _run(READRAG, env)
        assert r.returncode == 0, f"exit {r.returncode}; stderr={r.stderr[:300]!r}"
        _no_py_crash(r)

    def test_work_mode_fast_exit(self, seeded) -> None:
        sid, seed = seeded
        seed(mode="work")
        env = json.dumps({
            "session_id": sid,
            "hook_event_name": "PreToolUse",
            "tool_name": "Read",
            "tool_input": {"file_path": SAMPLE_SOURCE},
        })
        r = _run(READRAG, env)
        assert r.returncode == 0, f"exit {r.returncode}; stderr={r.stderr[:300]!r}"
        _no_py_crash(r)


@requires_server
class TestAutoApproveBehavior:
    def test_plain_non_approval_no_directive(self, seeded) -> None:
        sid, seed = seeded
        seed(mode="work")
        env = json.dumps({
            "session_id": sid,
            "hook_event_name": "UserPromptSubmit",
            "prompt": "lets refactor the parser module structure",
        })
        r = _run(AUTOAPPROVE, env)
        assert r.returncode == 0, f"exit {r.returncode}; stderr={r.stderr[:300]!r}"
        _no_py_crash(r)
        assert "approval pattern detected" not in r.stdout

    def test_approval_emits_directive(self, seeded, tmp_path) -> None:
        # proves the deferred PROJECT_ROOT + CURRENT_MODE still feed the match path.
        # The approval-match path emits one of two directives depending on whether
        # a Work-mode gate is pending: the generic "[Writ: approval pattern
        # detected]" when none is, or the gate-advance "[Writ: <phase> gate
        # approved -> <next>] (... no agent self-approval)" when one is. Either
        # proves the match path fired with the deferred vars (the non-approval
        # siblings emit NEITHER), so accept both rather than pin one daemon gate
        # state -- the seeded session's gate state is daemon-resolved, not fixed
        # by the file-cache seed.
        #
        # A transcript fixture with a real approval-request marker is now required
        # for the exact tier to reach either directive at all (approval-integrity
        # cycle, defect 2): without it the hook would take the ask-directive path
        # instead, and this deferred-variable test would stop proving what it was
        # written for.
        sid, seed = seeded
        seed(mode="work")
        transcript_path = write_evidence_transcript(tmp_path)
        env = json.dumps({
            "session_id": sid,
            "hook_event_name": "UserPromptSubmit",
            "prompt": "approved",
            "transcript_path": transcript_path,
        })
        r = _run(AUTOAPPROVE, env)
        assert r.returncode == 0, f"exit {r.returncode}; stderr={r.stderr[:300]!r}"
        _no_py_crash(r)
        assert (
            "[Writ: approval pattern detected]" in r.stdout
            or "gate approved" in r.stdout
        ), f"approval-match path emitted no directive; stdout={r.stdout!r}"

    def test_looks_like_approval_miss_path(self, seeded) -> None:
        # "good question" -> LOOKS_LIKE_APPROVAL yes, IS_APPROVAL no: exercises the
        # deferred fetch inside the miss-friction branch. No directive emitted.
        sid, seed = seeded
        seed(mode="work")
        env = json.dumps({
            "session_id": sid,
            "hook_event_name": "UserPromptSubmit",
            "prompt": "good question",
        })
        r = _run(AUTOAPPROVE, env)
        assert r.returncode == 0, f"exit {r.returncode}; stderr={r.stderr[:300]!r}"
        _no_py_crash(r)
        assert "approval pattern detected" not in r.stdout


@requires_server
class TestPosttoolBehavior:
    def test_write_source_runs_clean(self, seeded) -> None:
        sid, seed = seeded
        seed(mode="work")
        env = json.dumps({
            "session_id": sid,
            "hook_event_name": "PostToolUse",
            "tool_name": "Write",
            "tool_input": {
                "file_path": "/tmp/pol5b3b_sample.py",
                "content": "class Sample:\n    def handler(self):\n        return 1\n",
            },
        })
        r = _run(POSTTOOL, env)
        assert r.returncode == 0, f"exit {r.returncode}; stderr={r.stderr[:300]!r}"
        _no_py_crash(r)
