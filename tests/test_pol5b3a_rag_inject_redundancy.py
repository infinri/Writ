"""POL-5b-3a: collapse redundant cache reads / JSON parses in writ-rag-inject.sh.

writ-rag-inject.sh is the UserPromptSubmit RAG bridge -- it fires on every user
turn, the hottest hook in the system. Four behavior-preserving collapses:

  1. main-path `$CACHE` 4-field extraction (LOADED_RULE_IDS / REMAINING_BUDGET /
     PREFER_RULE_IDS / DETECTED_DOMAIN): 4 `python3` spawns -> 1 (sed-split).
  2. orchestrator path: direct-file-read for is_orchestrator + a second
     `_writ_session read` for the status line -> 1 cache read, reused.
  3. escalation block's second `_writ_session read` -> reuse the main `$CACHE`.
  4. META `rule_ids`+`cost` double-parse at 3 sites -> 1 spawn each.

Source-shape guards prove the redundancy is gone; behavioral guards (run the hook
via bash against the live server) prove nothing broke.

RED until writ-rag-inject.sh is refactored.
"""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import tempfile
import uuid
from pathlib import Path

import pytest

SKILL_DIR = Path(__file__).resolve().parent.parent
HOOK = SKILL_DIR / "hooks" / "scripts" / "writ-rag-inject.sh"
WRIT_SESSION_PY = str(SKILL_DIR / "bin" / "lib" / "writ-session.py")
SRC = HOOK.read_text()


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def _load_writ_session():
    spec = importlib.util.spec_from_file_location("writ_session_5b3a", WRIT_SESSION_PY)
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


def _run(envelope: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["bash", str(HOOK)],
        input=envelope,
        capture_output=True,
        text=True,
        cwd=str(SKILL_DIR),
        env={**os.environ, "WRIT_HOST": "localhost"},
        timeout=25,
    )


@pytest.fixture()
def seeded():
    """Seed a session cache via the module's own _read_cache/_write_cache so the
    daemon (which reads the same /tmp file) sees identical state. Returns a
    (session_id, seed_fn) pair; cleans the cache file on teardown."""
    mod = _load_writ_session()
    sid = f"test-5b3a-{uuid.uuid4().hex[:8]}"

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
    assert "Traceback" not in r.stderr, f"python traceback in hook stderr:\n{r.stderr[:500]}"
    assert "SyntaxError" not in r.stderr, f"python SyntaxError in hook stderr:\n{r.stderr[:500]}"


# --------------------------------------------------------------------------- #
# 1. source-shape guards -- the redundancy is gone
# --------------------------------------------------------------------------- #
class TestRedundancyRemoved:
    def test_cache_read_count_is_one(self) -> None:
        # A1: was 3 daemon round-trips (standalone `mode get`, orchestrator read,
        # main read), then 2 after POL-5b-3a; now ONE `_writ_session read` whose
        # $CACHE is reused everywhere (orchestrator branch via the CACHE_DATA
        # alias, main path, escalation, backward-context). mode + is_orchestrator
        # derive from it via the jq-first parsed_field/parsed_bool helpers.
        # Count the command substitution, not bare comment mentions.
        n = SRC.count("$(_writ_session read")
        assert n == 1, (
            f"expected exactly 1 `_writ_session read` call (single shared read); found {n}"
        )

    def test_orchestrator_direct_file_read_gone(self) -> None:
        # the shell-interpolated direct cache-file open used only by the old
        # is_orchestrator check; the other two `writ-session-{session_id}` uses
        # are python-arg style and stay.
        assert "writ-session-${SESSION_ID}.json" not in SRC, (
            "is_orchestrator must derive from a shared `_writ_session read`, "
            "not a direct cache-file open"
        )

    def test_main_channels_via_single_prompt_bundle(self) -> None:
        # #8 superseded POL-5b-3a's "collapse to one parse": the broad/always-on/
        # methodology channels moved into ONE warm /prompt-bundle call. The endpoint
        # reads the cache + renders, so the main-path `$CACHE` 4-field parse is GONE
        # from the hook entirely (not just collapsed).
        assert SRC.count("/prompt-bundle") >= 1, "main channels must route through /prompt-bundle"
        assert 'echo "$CACHE" | python3' not in SRC, (
            "main-path $CACHE field-parse must be gone -- the endpoint reads the cache"
        )

    def test_main_meta_parse_moved_server_side(self) -> None:
        # The broad /query META (rule_ids+cost) is parsed + applied server-side; the
        # hook gets the rendered text + meta from the bundle instead.
        assert 'echo "$META_JSON" | parse_writ_meta' not in SRC, (
            "broad META is handled server-side; no hook-side parse_writ_meta"
        )

    def test_methodology_meta_parse_moved_server_side(self) -> None:
        assert 'echo "$METHOD_META_JSON" | parse_writ_meta' not in SRC, (
            "methodology-companion META is handled server-side; no hook-side parse_writ_meta"
        )

    def test_orchestrator_meta_parse_moved_server_side(self) -> None:
        """WAS test_orchestrator_meta_double_parse_collapsed, which asserted the
        orchestrator companion's META was parsed EXACTLY ONCE (it had been parsed
        twice; POL-5b-3a collapsed it to one).

        Plan dfacff61 deleted the hand-rolled orchestrator companion outright: a
        master now goes through the shared /prompt-bundle call like every other
        session, and the endpoint parses and applies the companion META
        server-side. So the count is zero, not one, and the redundancy the old
        assertion guarded cannot recur by any edit to this hook, because there is
        no hook-side parse left to duplicate.

        Re-keyed to the state that actually holds rather than deleted outright:
        an equality-to-one pin would now have to be an equality-to-zero pin, and
        this says WHY zero is right. It sits beside its two siblings above, which
        make the same "moved server-side" claim for the broad and methodology
        channels.
        """
        assert SRC.count('echo "$ORCH_METHOD_META_JSON" | parse_writ_meta') == 0, (
            "the hand-rolled orchestrator companion META parse is back; the "
            "master must use the shared /prompt-bundle path, which parses and "
            "applies that META server-side"
        )
        assert "ORCH_METHOD_META_JSON" not in SRC, (
            "the orchestrator companion's hook-side META variable is back"
        )


# --------------------------------------------------------------------------- #
# 2. behavioral guards -- nothing broke (live server)
# --------------------------------------------------------------------------- #
@requires_server
class TestBehaviorPreserved:
    def test_work_mode_runs_past_request_gate(self, seeded) -> None:
        # always-on / methodology injection live AFTER the collapse #1 block and
        # AFTER the REQUEST-build gate. Their presence proves the collapsed field
        # extraction produced a valid REQUEST (a broken collapse exits at the gate).
        sid, seed = seeded
        seed(mode="work")
        env = json.dumps({
            "session_id": sid,
            "hook_event_name": "UserPromptSubmit",
            "prompt": "How do I add a new controller endpoint with input validation",
        })
        r = _run(env)
        assert r.returncode == 0, f"exit {r.returncode}; stderr={r.stderr[:300]!r}"
        _no_py_crash(r)
        assert ("ALWAYS-ACTIVE RULES" in r.stdout) or ("[Writ:" in r.stdout), (
            f"work-mode hook injected nothing past the gate; stdout={r.stdout[:300]!r}"
        )

    def test_orchestrator_status_line(self, seeded) -> None:
        # collapse #2: the unified read must yield a CACHE_DATA the status-line
        # python can parse into the `[Writ: ...]` line.
        sid, seed = seeded
        seed(mode="work", is_orchestrator=True)
        env = json.dumps({
            "session_id": sid,
            "hook_event_name": "UserPromptSubmit",
            "prompt": "Plan the refactor of the session module into submodules",
        })
        r = _run(env)
        assert r.returncode == 0, f"exit {r.returncode}; stderr={r.stderr[:300]!r}"
        _no_py_crash(r)
        assert "[Writ:" in r.stdout, (
            f"orchestrator status line missing; stdout={r.stdout[:300]!r}"
        )

    def test_short_prompt_fast_path(self, seeded) -> None:
        # prompt below MIN_QUERY_LENGTH (10): exits at the length gate, clean.
        sid, seed = seeded
        seed(mode="work")
        env = json.dumps({
            "session_id": sid,
            "hook_event_name": "UserPromptSubmit",
            "prompt": "hi",
        })
        r = _run(env)
        assert r.returncode == 0, f"exit {r.returncode}; stderr={r.stderr[:300]!r}"
        _no_py_crash(r)

    def test_new_session_no_cache_file(self) -> None:
        # brand-new sid (no cache file): daemon returns defaults, mode unset ->
        # mode-classification directive fires; collapse must not crash on defaults.
        sid = f"test-5b3a-fresh-{uuid.uuid4().hex[:8]}"
        env = json.dumps({
            "session_id": sid,
            "hook_event_name": "UserPromptSubmit",
            "prompt": "Explain how the retrieval pipeline ranks candidate rules",
        })
        try:
            r = _run(env)
            assert r.returncode == 0, f"exit {r.returncode}; stderr={r.stderr[:300]!r}"
            _no_py_crash(r)
        finally:
            p = Path(tempfile.gettempdir()) / f"writ-session-{sid}.json"
            if p.exists():
                p.unlink()
