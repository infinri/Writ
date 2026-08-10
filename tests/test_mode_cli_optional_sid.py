"""PIECE 1 wiring: optional <session_id> for `mode set|switch|init`, and the doctor
falsy-branch delegation to the canonical resolver.

Per plan.md:
  - `mode set|switch|init <mode>` (no trailing sid) resolves the current session via
    cache.resolve_current_session_id() and applies the mode to it.
  - `mode set|switch|init <mode> <sid>` (explicit sid) is byte-for-byte unchanged --
    no resolver call at all.
  - `mode get <sid>` is unchanged (sid always required for get).
  - An unresolvable session (resolver returns None) with no sid prints the usage
    message and exits 2, exactly like the current "too few argv" failure.
  - doctor._latest_session_cache(None) delegates to cache.resolve_current_session_id()
    (pointer/env first) instead of a bare mtime max over the cache dir.

`writ.session.cache.resolve_current_session_id` and the optional-sid branch in
`writ.session.cli_dispatch._cli_mode` do not exist yet -- this file is RED until
PIECE 1 lands. Per TEST-TDD-001: skeletons approved before implementation.

Hermetic: WRIT_CACHE_DIR -> tmp_path (matches test_pol6b2_cache_dir_env.py); the
session-pointer constant is monkeypatched to a tmp_path file (never real /tmp);
CLAUDE_SESSION_ID / CLAUDE_JOB_DIR are set/deleted per case.
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

    def test_mode_set_no_sid_and_unresolvable_prints_usage_to_stderr(self, capsys):
        with pytest.raises(SystemExit):
            cli_dispatch._cli_mode(["writ-session.py", "mode", "set", "work"])
        captured = capsys.readouterr()
        assert "Usage" in captured.err

    def test_mode_set_no_sid_unresolvable_writes_no_cache_file(self, tmp_path):
        with pytest.raises(SystemExit):
            cli_dispatch._cli_mode(["writ-session.py", "mode", "set", "work"])
        assert list(tmp_path.glob("writ-session-*.json")) == []


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

    def test_latest_session_cache_none_falls_back_to_mtime_when_resolver_fails(self, tmp_path):
        from writ.session import doctor

        # No env signals, no pointer file -- resolver falls through to its own
        # mtime-glob last resort, so the doctor's documented fallback still works
        # offline exactly as before.
        cache._write_cache("only-cache-present", cache._read_cache("only-cache-present"))
        result = doctor._latest_session_cache(None)
        assert result is not None
        assert result.get("mode") is None

    def test_latest_session_cache_with_explicit_session_id_is_unchanged(self):
        from writ.session import doctor

        cache._write_cache("explicit-doctor-sid", cache._read_cache("explicit-doctor-sid"))
        result = doctor._latest_session_cache("explicit-doctor-sid")
        assert result is not None

    def test_latest_session_cache_none_and_nothing_resolvable_returns_none(self):
        from writ.session import doctor

        assert doctor._latest_session_cache(None) is None
