"""Smoke tests for hooks/scripts/writ-mark-pending-test.sh.

Exercises the hook with crafted JSON payloads to verify it writes the marker
file under the parent session id (not the worker's id) and respects the work-
mode + path-matching gates. Drives out the orchestrator-pattern bug per
plan.md "Fix: test hooks no-op under orchestrator pattern".

THE CACHE ROOT USED TO BE `REPO / "cache"`, AND THAT WAS THE DEFECT, NOT THE REQUIREMENT.
The hook built its marker path from its own location, so it wrote into the live checkout no
matter where WRIT_CACHE_DIR pointed -- and conftest.py:30 points it at a temp directory for
every test in this suite. These tests then asserted the live-repo path, which made them
pass only while the hook ignored the isolation it was handed, and made every full-suite run
deposit `cache/parent-<hex>/` directories in the repository. Asserting the leak IS the
requirement is the same shape as the fallback test that demanded a synthesized session id.

Each test now owns a cache root under tmp_path, passed explicitly to both the hook and the
mode helper rather than inherited, so the assertion names the directory the run was told to
use. `test_no_marker_reaches_the_live_checkout` reads the repository directly, because
every other assertion here would still pass if the hook wrote to BOTH places.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import uuid
from pathlib import Path

import pytest

# autouse: pins cwd to a sandbox so `mode set` cannot delete THIS repo's gate artifacts.
from tests.fixtures.session_state import sandbox_cwd  # noqa: F401

REPO = Path(__file__).resolve().parent.parent
HOOK = REPO / "hooks" / "scripts" / "writ-mark-pending-test.sh"
SESSION_HELPER = REPO / "bin" / "lib" / "writ-session.py"
LIVE_CACHE = REPO / "cache"


def _env(cache_root: Path) -> dict:
    env = os.environ.copy()
    env["WRIT_CACHE_DIR"] = str(cache_root)
    env["WRIT_FRICTION_LOG"] = str(cache_root / "friction.log")
    env["WRIT_LOG_ROOT"] = str(cache_root / "logs")
    return env


def _set_mode(session_id: str, mode: str, cache_root: Path) -> None:
    subprocess.run(
        [sys.executable, str(SESSION_HELPER), "mode", "set", mode, session_id],
        check=True, capture_output=True, env=_env(cache_root),
    )


def _run_hook(payload: dict, cwd: Path, cache_root: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["bash", str(HOOK)],
        input=json.dumps(payload),
        capture_output=True, text=True, cwd=cwd, env=_env(cache_root),
    )


def _make_sandbox(tmp_path: Path) -> Path:
    (tmp_path / "src").mkdir()
    (tmp_path / "tests").mkdir()
    f = tmp_path / "src" / "widget.py"
    f.write_text("def add(a, b): return a + b\n")
    return f


@pytest.fixture
def cache_root(tmp_path) -> Path:
    """This test's own cache directory. Removed with tmp_path, so nothing survives."""
    root = tmp_path / "writ-cache"
    root.mkdir()
    return root


@pytest.fixture
def parent_sid(cache_root):
    sid = f"parent-{uuid.uuid4().hex[:8]}"
    _set_mode(sid, "work", cache_root)
    return sid


@pytest.fixture
def worker_sid():
    return f"worker-{uuid.uuid4().hex[:8]}"


def test_single_session_payload_marker_under_session_id(tmp_path, parent_sid, cache_root):
    """No agent_id in payload: marker goes to cache/<session_id>/pending-tests.txt."""
    src = _make_sandbox(tmp_path)
    payload = {
        "session_id": parent_sid,
        "tool_input": {"file_path": str(src)},
        "tool_response": {},
    }
    result = _run_hook(payload, tmp_path, cache_root)
    assert result.returncode == 0, result.stderr

    marker = cache_root / parent_sid / "pending-tests.txt"
    assert marker.is_file(), f"expected marker at {marker}"
    assert str(src) in marker.read_text()


def test_subagent_payload_marker_under_parent_session(tmp_path, parent_sid, worker_sid, cache_root):
    """Sub-agent payload: marker goes to PARENT's cache dir, not worker's."""
    src = _make_sandbox(tmp_path)
    payload = {
        "session_id": parent_sid,
        "agent_id": worker_sid,
        "tool_input": {"file_path": str(src)},
        "tool_response": {},
    }
    result = _run_hook(payload, tmp_path, cache_root)
    assert result.returncode == 0, result.stderr

    parent_marker = cache_root / parent_sid / "pending-tests.txt"
    worker_marker = cache_root / worker_sid / "pending-tests.txt"
    assert parent_marker.is_file(), f"expected marker at parent path {parent_marker}"
    assert str(src) in parent_marker.read_text()
    assert not worker_marker.exists(), (
        f"marker should NOT appear under worker id; found {worker_marker}"
    )


def test_empty_session_id_exits_silently(tmp_path, cache_root):
    """Degenerate payload with no session_id: no marker created anywhere."""
    src = _make_sandbox(tmp_path)
    payload = {
        "session_id": "",
        "tool_input": {"file_path": str(src)},
        "tool_response": {},
    }
    result = _run_hook(payload, tmp_path, cache_root)
    assert result.returncode == 0
    empty_marker = cache_root / "" / "pending-tests.txt"
    assert not empty_marker.exists()


def test_parent_not_in_work_mode_no_marker(tmp_path, cache_root):
    """Parent session in conversation mode: hook short-circuits, no marker."""
    sid = f"convo-{uuid.uuid4().hex[:8]}"
    _set_mode(sid, "conversation", cache_root)
    src = _make_sandbox(tmp_path)
    payload = {
        "session_id": sid,
        "tool_input": {"file_path": str(src)},
        "tool_response": {},
    }
    result = _run_hook(payload, tmp_path, cache_root)
    assert result.returncode == 0
    marker = cache_root / sid / "pending-tests.txt"
    assert not marker.exists()


def test_path_outside_src_or_tests_no_marker(tmp_path, parent_sid, cache_root):
    """Write to docs/foo.md: path doesn't match src/*/tests/* pattern, no marker."""
    docs = tmp_path / "docs"
    docs.mkdir()
    f = docs / "notes.md"
    f.write_text("notes\n")
    payload = {
        "session_id": parent_sid,
        "tool_input": {"file_path": str(f)},
        "tool_response": {},
    }
    result = _run_hook(payload, tmp_path, cache_root)
    assert result.returncode == 0
    marker = cache_root / parent_sid / "pending-tests.txt"
    assert not marker.exists()


def test_is_error_true_no_marker(tmp_path, parent_sid, cache_root):
    """Failed write (tool_result_is_error=true): hook skips marker append."""
    src = _make_sandbox(tmp_path)
    payload = {
        "session_id": parent_sid,
        "tool_input": {"file_path": str(src)},
        "tool_response": {},
        "tool_result_is_error": True,
    }
    result = _run_hook(payload, tmp_path, cache_root)
    assert result.returncode == 0
    marker = cache_root / parent_sid / "pending-tests.txt"
    assert not marker.exists()


def test_test_file_path_also_marked(tmp_path, parent_sid, cache_root):
    """Writing a tests/ file (not src/) should also be marked."""
    _make_sandbox(tmp_path)
    test_file = tmp_path / "tests" / "test_widget.py"
    test_file.write_text("def test_x(): assert True\n")
    payload = {
        "session_id": parent_sid,
        "tool_input": {"file_path": str(test_file)},
        "tool_response": {},
    }
    result = _run_hook(payload, tmp_path, cache_root)
    assert result.returncode == 0
    marker = cache_root / parent_sid / "pending-tests.txt"
    assert marker.is_file()
    assert str(test_file) in marker.read_text()


def _make_magento_sandbox(tmp_path: Path) -> dict:
    """Create a minimal Magento module skeleton under tmp_path/app/code/Foo/Bar/."""
    module = tmp_path / "app" / "code" / "Foo" / "Bar"
    (module / "Model").mkdir(parents=True)
    (module / "Block").mkdir()
    (module / "etc").mkdir()
    (module / "view" / "frontend" / "layout").mkdir(parents=True)
    (module / "Test" / "Unit" / "Model").mkdir(parents=True)

    src = module / "Model" / "Baz.php"
    src.write_text("<?php\nnamespace Foo\\Bar\\Model;\nclass Baz {}\n")

    test = module / "Test" / "Unit" / "Model" / "BazTest.php"
    test.write_text("<?php\nnamespace Foo\\Bar\\Test\\Unit\\Model;\nclass BazTest {}\n")

    cfg = module / "etc" / "config.xml"
    cfg.write_text("<?xml version=\"1.0\"?><config/>\n")

    layout = module / "view" / "frontend" / "layout" / "default.xml"
    layout.write_text("<?xml version=\"1.0\"?><page/>\n")

    return {"src": src, "test": test, "cfg": cfg, "layout": layout}


def test_magento_source_path_marked(tmp_path, parent_sid, cache_root):
    """app/code/Foo/Bar/Model/Baz.php should trigger marker append."""
    paths = _make_magento_sandbox(tmp_path)
    payload = {
        "session_id": parent_sid,
        "tool_input": {"file_path": str(paths["src"])},
        "tool_response": {},
    }
    result = _run_hook(payload, tmp_path, cache_root)
    assert result.returncode == 0, result.stderr
    marker = cache_root / parent_sid / "pending-tests.txt"
    assert marker.is_file()
    assert str(paths["src"]) in marker.read_text()


def test_magento_test_path_marked(tmp_path, parent_sid, cache_root):
    """app/code/Foo/Bar/Test/Unit/Model/BazTest.php should trigger marker append."""
    paths = _make_magento_sandbox(tmp_path)
    payload = {
        "session_id": parent_sid,
        "tool_input": {"file_path": str(paths["test"])},
        "tool_response": {},
    }
    result = _run_hook(payload, tmp_path, cache_root)
    assert result.returncode == 0, result.stderr
    marker = cache_root / parent_sid / "pending-tests.txt"
    assert marker.is_file()
    assert str(paths["test"]) in marker.read_text()


def test_magento_etc_xml_not_marked(tmp_path, parent_sid, cache_root):
    """Config XML files under etc/ are NOT source files; no marker append."""
    paths = _make_magento_sandbox(tmp_path)
    payload = {
        "session_id": parent_sid,
        "tool_input": {"file_path": str(paths["cfg"])},
        "tool_response": {},
    }
    result = _run_hook(payload, tmp_path, cache_root)
    assert result.returncode == 0
    marker = cache_root / parent_sid / "pending-tests.txt"
    assert not marker.exists()


def test_magento_view_layout_not_marked(tmp_path, parent_sid, cache_root):
    """Layout XML under view/ is NOT a source file; no marker append."""
    paths = _make_magento_sandbox(tmp_path)
    payload = {
        "session_id": parent_sid,
        "tool_input": {"file_path": str(paths["layout"])},
        "tool_response": {},
    }
    result = _run_hook(payload, tmp_path, cache_root)
    assert result.returncode == 0
    marker = cache_root / parent_sid / "pending-tests.txt"
    assert not marker.exists()


def test_no_marker_reaches_the_live_checkout(tmp_path, parent_sid, cache_root):
    """The leak these tests used to require, asserted from the repository.

    Every other assertion in this file reads the sandbox, and all of them would still
    pass if the hook wrote to the sandbox AND to the checkout. This one names the live
    path directly. It is why the fix is a seam rather than a second write location.
    """
    before = {p.name for p in LIVE_CACHE.iterdir()} if LIVE_CACHE.exists() else set()

    src = _make_sandbox(tmp_path)
    payload = {
        "session_id": parent_sid,
        "tool_input": {"file_path": str(src)},
        "tool_response": {},
    }
    assert _run_hook(payload, tmp_path, cache_root).returncode == 0
    assert (cache_root / parent_sid / "pending-tests.txt").is_file(), (
        "the hook wrote nothing at all, so the absence below proves nothing"
    )

    after = {p.name for p in LIVE_CACHE.iterdir()} if LIVE_CACHE.exists() else set()
    assert after - before == set(), (
        f"the hook created {sorted(after - before)} in {LIVE_CACHE} while WRIT_CACHE_DIR "
        f"pointed at {cache_root}"
    )
