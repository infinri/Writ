"""PIECE 1 wiring (Part 1 of the isolation cycle): optional <session_id> for
`mode set|switch|init`, and the doctor falsy-branch delegation to the canonical resolver
-- now that the resolver's two guessing tiers are gone.

Per plan.md:
  - `mode set|switch|init <mode>` (no trailing sid) resolves the current session via
    cache.resolve_current_session_id(), which now answers only from $CLAUDE_SESSION_ID or
    basename($CLAUDE_JOB_DIR). No pointer file, no mtime glob.
  - `mode set|switch|init <mode> <sid>` (explicit sid) is byte-for-byte unchanged -- no
    resolver call at all.
  - `mode get <sid>` is unchanged (sid always required for get).
  - An unresolvable session (resolver returns None) with no sid exits 2 with a message that
    NAMES both accepted env vars (CLAUDE_SESSION_ID, CLAUDE_JOB_DIR) and the explicit-sid
    form, because a human typing a bare `mode set work` is a real path and deserves to be
    told what to do next rather than a bare "Usage" line.
  - doctor._latest_session_cache(None) delegates to cache.resolve_current_session_id() and,
    now that the resolver's own mtime-glob last resort is gone, does NOT fall back to its
    OWN local re-implementation of that glob (doctor.py:493-501) either: an unresolvable
    session means the doctor reports there is none, not the newest cache in the directory.

This file is RED until PIECE 1's tier deletion and doctor's local-glob removal land:
`TestUnresolvableSessionErrorsLoud` expects specific env-var names in the error text that
today's generic "Usage" message does not contain, and
`TestDoctorFalsyBranchDelegatesToResolver.test_latest_session_cache_none_reports_none_*`
expects None where today's doctor still returns the newest cache on disk. Per
TEST-TDD-001: skeletons approved before implementation.

Hermetic: WRIT_CACHE_DIR -> tmp_path (matches test_pol6b2_cache_dir_env.py); the
session-pointer seam is monkeypatched to a tmp_path file with raising=False so the same
call is harmless whether or not the resolver still has that attribute; CLAUDE_SESSION_ID /
CLAUDE_JOB_DIR are set/deleted per case. No test here touches the real
/tmp/writ-current-session.
"""

from __future__ import annotations

import json
import os

import pytest

from writ.session import cache, cli_dispatch, mode_engine

# autouse: pins cwd to a sandbox so `mode set` cannot delete THIS repo's gate artifacts.
from tests.fixtures.session_state import sandbox_cwd  # noqa: F401


@pytest.fixture(autouse=True)
def _isolated_cache_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("WRIT_CACHE_DIR", str(tmp_path))
    monkeypatch.delenv("CLAUDE_SESSION_ID", raising=False)
    monkeypatch.delenv("CLAUDE_JOB_DIR", raising=False)
    pointer = tmp_path / "writ-current-session"
    monkeypatch.setattr(cache, "_SESSION_POINTER_PATH", str(pointer), raising=False)
    yield


def _cache_json(session_id: str) -> dict:
    path = os.path.join(cache._cache_dir(), f"writ-session-{session_id}.json")
    with open(path) as f:
        return json.load(f)


class TestOptionalSidAppliesToResolvedSession:
    def test_mode_set_with_no_sid_resolves_current_session_via_env(self, monkeypatch):
        monkeypatch.setenv("CLAUDE_SESSION_ID", "resolved-sid-1")
        cli_dispatch._cli_mode(["writ-session.py", "mode", "set", "work"])
        assert _cache_json("resolved-sid-1")["mode"] == "work"

    def test_mode_init_with_no_sid_resolves_current_session(self, monkeypatch):
        monkeypatch.setenv("CLAUDE_SESSION_ID", "resolved-sid-2")
        cli_dispatch._cli_mode(["writ-session.py", "mode", "init", "debug"])
        assert _cache_json("resolved-sid-2")["mode"] == "debug"

    def test_mode_switch_with_no_sid_resolves_current_session(self, monkeypatch):
        monkeypatch.setenv("CLAUDE_SESSION_ID", "resolved-sid-3")
        cli_dispatch._cli_mode(["writ-session.py", "mode", "set", "work"])
        cli_dispatch._cli_mode(["writ-session.py", "mode", "switch", "review"])
        assert _cache_json("resolved-sid-3")["mode"] == "review"

    def test_mode_set_no_sid_honors_orchestrator_flag(self, monkeypatch):
        monkeypatch.setenv("CLAUDE_SESSION_ID", "resolved-sid-4")
        cli_dispatch._cli_mode(["writ-session.py", "mode", "set", "work", "--orchestrator"])
        assert _cache_json("resolved-sid-4")["is_orchestrator"] is True

    def test_mode_set_no_sid_reads_its_own_mode_not_a_decoy_pointers(
        self, tmp_path, monkeypatch
    ):
        """A pointer naming a DIFFERENT session, with a DIFFERENT mode already recorded,
        must not redirect this call: env wins outright and the pointer is never read."""
        pointer = tmp_path / "writ-current-session"
        pointer.write_text("decoy-sid-different-mode")
        cli_dispatch._cli_mode(["writ-session.py", "mode", "set", "debug", "decoy-sid-different-mode"])
        monkeypatch.setenv("CLAUDE_SESSION_ID", "own-sid")
        cli_dispatch._cli_mode(["writ-session.py", "mode", "set", "review"])
        assert _cache_json("own-sid")["mode"] == "review"
        assert _cache_json("decoy-sid-different-mode")["mode"] == "debug", (
            "the decoy's own state must be untouched by the other session's mode set"
        )


class TestExplicitSidUnchanged:
    def test_mode_set_with_explicit_sid_ignores_resolver(self, monkeypatch):
        # Env would resolve to a DIFFERENT session; the explicit sid must win untouched.
        monkeypatch.setenv("CLAUDE_SESSION_ID", "env-should-be-ignored")
        cli_dispatch._cli_mode(["writ-session.py", "mode", "set", "work", "explicit-sid-1"])
        assert _cache_json("explicit-sid-1")["mode"] == "work"
        assert not os.path.exists(
            os.path.join(cache._cache_dir(), "writ-session-env-should-be-ignored.json")
        )

    def test_mode_set_with_explicit_sid_does_not_call_resolver(self, monkeypatch):
        calls = []
        monkeypatch.setattr(
            cache, "resolve_current_session_id", lambda: calls.append(1) or "should-not-be-used"
        )
        cli_dispatch._cli_mode(["writ-session.py", "mode", "set", "work", "explicit-sid-2"])
        assert calls == [], "resolver must not be invoked when a sid argv is supplied"
        assert _cache_json("explicit-sid-2")["mode"] == "work"

    def test_mode_get_still_requires_explicit_sid(self, capsys):
        with pytest.raises(SystemExit) as exc_info:
            cli_dispatch._cli_mode(["writ-session.py", "mode", "get"])
        assert exc_info.value.code == 2


class TestUnresolvableSessionErrorsLoud:
    def test_mode_set_no_sid_and_unresolvable_exits_2(self, capsys):
        # No env signals, no pointer file, no cache files anywhere -- nothing resolves.
        with pytest.raises(SystemExit) as exc_info:
            cli_dispatch._cli_mode(["writ-session.py", "mode", "set", "work"])
        assert exc_info.value.code == 2

    def test_mode_set_no_sid_and_unresolvable_names_both_env_vars(self, capsys):
        """A human typing `mode set work` with no session in scope needs to be told
        exactly what Writ will accept, not a bare usage line. Pinned per plan.md: 'names
        both accepted env vars and the explicit-sid form ... says plainly why the guess
        was removed'."""
        with pytest.raises(SystemExit):
            cli_dispatch._cli_mode(["writ-session.py", "mode", "set", "work"])
        captured = capsys.readouterr()
        assert "CLAUDE_SESSION_ID" in captured.err, (
            f"error message must name the env var a human or CC could export: {captured.err!r}"
        )
        assert "CLAUDE_JOB_DIR" in captured.err, (
            f"error message must name the second accepted env var: {captured.err!r}"
        )

    def test_mode_set_no_sid_and_unresolvable_names_the_explicit_sid_recovery(self, capsys):
        with pytest.raises(SystemExit):
            cli_dispatch._cli_mode(["writ-session.py", "mode", "set", "work"])
        captured = capsys.readouterr()
        assert "session_id" in captured.err.lower(), (
            f"error message must name the explicit-sid form as the recovery path: "
            f"{captured.err!r}"
        )

    def test_mode_set_no_sid_unresolvable_writes_no_cache_file(self, tmp_path):
        with pytest.raises(SystemExit):
            cli_dispatch._cli_mode(["writ-session.py", "mode", "set", "work"])
        assert list(tmp_path.glob("writ-session-*.json")) == []

    def test_mode_set_no_sid_refuses_even_with_a_populated_pointer_and_other_caches(
        self, tmp_path
    ):
        """The refusal must hold even when the OLD guessing tiers would have had an
        answer: a populated pointer plus an unrelated cache sitting in the same directory
        must not be mistaken for this session's identity."""
        pointer = tmp_path / "writ-current-session"
        pointer.write_text("some-other-sessions-id")
        cache._write_cache("some-other-sessions-id", cache._read_cache("some-other-sessions-id"))

        with pytest.raises(SystemExit) as exc_info:
            cli_dispatch._cli_mode(["writ-session.py", "mode", "set", "work"])
        assert exc_info.value.code == 2
        # The unrelated session's cache must be untouched by this refused call.
        with open(tmp_path / "writ-session-some-other-sessions-id.json") as f:
            untouched = json.load(f)
        assert untouched.get("mode") is None


class TestDoctorFalsyBranchDelegatesToResolver:
    def test_latest_session_cache_none_uses_resolver_result(self, monkeypatch):
        from writ.session import doctor

        monkeypatch.setenv("CLAUDE_SESSION_ID", "doctor-resolved-sid")
        cache._write_cache("doctor-resolved-sid", cache._read_cache("doctor-resolved-sid"))
        # A second, newer cache exists too -- the bare mtime-max would pick this one,
        # but the resolver (env-first) must win instead.
        cache._write_cache("doctor-newer-but-wrong", cache._read_cache("doctor-newer-but-wrong"))
        newer_path = os.path.join(
            cache._cache_dir(), "writ-session-doctor-newer-but-wrong.json"
        )
        os.utime(newer_path, None)

        result = doctor._latest_session_cache(None)
        resolved_path = os.path.join(
            cache._cache_dir(), "writ-session-doctor-resolved-sid.json"
        )
        with open(resolved_path) as f:
            expected = json.load(f)
        assert result == expected

    def test_latest_session_cache_none_reports_none_when_resolver_fails_despite_a_cache(
        self, tmp_path
    ):
        """Capability: `writ doctor` with no resolvable session reports that there is
        none instead of reporting the newest cache in the directory. No env signals, no
        pointer file -- the resolver now returns None outright (its own mtime-glob last
        resort is gone), and the doctor's LOCAL re-implementation of that same glob
        (doctor.py:493-501) must be gone too, or this still finds the cache below and
        this test stays green for the wrong reason.
        """
        from writ.session import doctor

        cache._write_cache("only-cache-present", cache._read_cache("only-cache-present"))
        assert doctor._latest_session_cache(None) is None

    def test_latest_session_cache_none_ignores_populated_pointer_and_multiple_caches(
        self, tmp_path
    ):
        """The combined hazard: a populated pointer AND several caches on disk, still with
        no env signal. Both old guessing tiers have a candidate answer; the doctor must
        still report none."""
        from writ.session import doctor

        pointer = tmp_path / "writ-current-session"
        pointer.write_text("tier-embed-directive-597898df")
        cache._write_cache("sid-alpha", cache._read_cache("sid-alpha"))
        cache._write_cache("sid-beta", cache._read_cache("sid-beta"))
        newer_path = os.path.join(cache._cache_dir(), "writ-session-sid-beta.json")
        os.utime(newer_path, (9999, 9999))

        assert doctor._latest_session_cache(None) is None

    def test_latest_session_cache_reads_its_own_mode_not_the_decoy_pointers(
        self, tmp_path, monkeypatch
    ):
        """Point the pointer at a decoy session whose mode differs, and assert the doctor
        reports the ENV-resolved session's own mode, not the decoy's."""
        from writ.session import doctor

        pointer = tmp_path / "writ-current-session"
        pointer.write_text("decoy-session-with-different-mode")
        decoy_cache = cache._read_cache("decoy-session-with-different-mode")
        decoy_cache["mode"] = "debug"
        cache._write_cache("decoy-session-with-different-mode", decoy_cache)

        own_cache = cache._read_cache("own-session")
        own_cache["mode"] = "review"
        cache._write_cache("own-session", own_cache)
        monkeypatch.setenv("CLAUDE_SESSION_ID", "own-session")

        result = doctor._latest_session_cache(None)
        assert result is not None
        assert result["mode"] == "review", (
            f"the doctor must report the resolved session's own mode, not the "
            f"pointer-named decoy's: got {result!r}"
        )

    def test_latest_session_cache_with_explicit_session_id_is_unchanged(self):
        from writ.session import doctor

        cache._write_cache("explicit-doctor-sid", cache._read_cache("explicit-doctor-sid"))
        result = doctor._latest_session_cache("explicit-doctor-sid")
        assert result is not None

    def test_latest_session_cache_none_and_nothing_resolvable_returns_none(self):
        from writ.session import doctor

        assert doctor._latest_session_cache(None) is None
