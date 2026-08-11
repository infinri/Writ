"""PIECE 1 (Part 1 of the isolation cycle): resolve_current_session_id() loses its two
guessing tiers.

Ordering after this cycle (first non-empty wins):
  1. $CLAUDE_SESSION_ID          (per-process; authoritative if CC sets it)
  2. basename($CLAUDE_JOB_DIR)   (per-job; concurrency-safe; trailing / stripped)
Returns None when neither resolves. There is no tier 3 and no tier 4 anymore.

WHY THE OTHER TWO TIERS ARE GONE. The resolver's own (pre-cycle) docstring called tier 3
"shared-global" and tier 4 "racy", and both words were literal. Tier 3 read
`/tmp/writ-current-session`, one file every Claude Code session on the machine overwrites
on every turn; tier 4 took the newest `writ-session-*.json` by mtime out of a cache
directory every project shares. CONFIRMED SYMPTOM: the user's magento project reported
Writ was in `work` mode, which it had never set, because both tiers resolved to a
DIFFERENT session's cache that happened to be the most recent. A later test run also left
the pointer naming a session (`tier-embed-directive-597898df`) that never had a cache at
all, which the old tier 4 could not distinguish from a legitimate signal.

`resolve_current_session_id()` now fails LOUD rather than guessing: callers get None and
decide for themselves (the CLI exits 2; the doctor reports "no session"; the resolver never
raises). This file is RED until the tiers are deleted from writ/session/cache.py -- the
classes below currently assert the OPPOSITE of what they now assert; that inversion is the
point of the diff, not a mistake in this file. Per TEST-TDD-001: skeletons approved before
implementation.

Hermetic: WRIT_CACHE_DIR is monkeypatched to tmp_path (same mechanism as
tests/test_pol6b2_cache_dir_env.py) so writ-session-*.json glob candidates never touch the
real /tmp. `_pointer_path()` monkeypatches `cache._SESSION_POINTER_PATH` to a tmp_path file
with `raising=False`, so it stays a no-op call whether or not that attribute still exists
on the module post-deletion -- setting an unused attribute on a module never errors, which
is exactly what lets the SAME helper double as "prove a populated pointer changes nothing"
after the tier that used to read it is gone. No test in this file ever reads or writes the
real /tmp/writ-current-session.
"""

from __future__ import annotations

import os

import pytest

from writ.session import cache


def _pointer_path(tmp_path, monkeypatch) -> str:
    """Point whatever pointer-path seam still exists at a tmp_path file.

    Never the real /tmp/writ-current-session: that file is rewritten by every Claude Code
    session on this machine on every turn (see MEMORY: project_global_session_pointer_flaw),
    so a test that touched it live could redirect or be redirected by a session that is not
    this test run.
    """
    pointer = tmp_path / "writ-current-session"
    monkeypatch.setattr(cache, "_SESSION_POINTER_PATH", str(pointer), raising=False)
    return str(pointer)


@pytest.fixture(autouse=True)
def _isolated_cache_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("WRIT_CACHE_DIR", str(tmp_path))
    monkeypatch.delenv("CLAUDE_SESSION_ID", raising=False)
    monkeypatch.delenv("CLAUDE_JOB_DIR", raising=False)
    yield


class TestResolverExists:
    def test_resolver_is_importable_and_callable(self):
        assert callable(cache.resolve_current_session_id)


class TestEnvSessionIdWins:
    def test_returns_claude_session_id_when_set_and_nonempty(self, monkeypatch):
        monkeypatch.setenv("CLAUDE_SESSION_ID", "sid-from-env-123")
        assert cache.resolve_current_session_id() == "sid-from-env-123"

    def test_env_session_id_wins_over_job_dir(self, monkeypatch):
        monkeypatch.setenv("CLAUDE_SESSION_ID", "sid-env-wins")
        monkeypatch.setenv("CLAUDE_JOB_DIR", "/home/user/.claude/jobs/sid-job-loses")
        assert cache.resolve_current_session_id() == "sid-env-wins"

    def test_env_session_id_is_unaffected_by_a_populated_pointer_file(
        self, tmp_path, monkeypatch
    ):
        """A pointer naming a DIFFERENT session cannot redirect this caller: with env set,
        the pointer is not merely outranked, it is never consulted at all."""
        pointer = _pointer_path(tmp_path, monkeypatch)
        with open(pointer, "w") as f:
            f.write("sid-pointer-must-not-win")
        monkeypatch.setenv("CLAUDE_SESSION_ID", "sid-env-wins-2")
        assert cache.resolve_current_session_id() == "sid-env-wins-2"

    def test_empty_string_env_session_id_is_not_treated_as_set(self, monkeypatch):
        monkeypatch.setenv("CLAUDE_SESSION_ID", "")
        monkeypatch.setenv("CLAUDE_JOB_DIR", "/home/user/.claude/jobs/sid-job-fallback")
        assert cache.resolve_current_session_id() == "sid-job-fallback"


class TestJobDirFallback:
    def test_returns_basename_of_job_dir_when_session_id_env_unset(self, monkeypatch):
        monkeypatch.setenv("CLAUDE_JOB_DIR", "/home/user/.claude/jobs/sid-job-456")
        assert cache.resolve_current_session_id() == "sid-job-456"

    def test_strips_trailing_slash_from_job_dir(self, monkeypatch):
        monkeypatch.setenv("CLAUDE_JOB_DIR", "/home/user/.claude/jobs/sid-job-789/")
        assert cache.resolve_current_session_id() == "sid-job-789"

    def test_job_dir_is_unaffected_by_a_populated_pointer_file(self, tmp_path, monkeypatch):
        pointer = _pointer_path(tmp_path, monkeypatch)
        with open(pointer, "w") as f:
            f.write("sid-pointer-must-not-win-2")
        monkeypatch.setenv("CLAUDE_JOB_DIR", "/home/user/.claude/jobs/sid-job-wins")
        assert cache.resolve_current_session_id() == "sid-job-wins"


class TestNoGuessingWhenEnvAndJobDirAreAbsent:
    """Capability: resolve_current_session_id() returns None when neither
    CLAUDE_SESSION_ID nor CLAUDE_JOB_DIR is set, even with a populated pointer file and
    several session caches present on disk. Every test here holds env and job-dir absent
    (the autouse fixture already deletes both) and proves the resolver refuses rather
    than guessing from whatever else happens to be lying around.
    """

    def test_returns_none_with_only_a_populated_pointer_file(self, tmp_path, monkeypatch):
        pointer = _pointer_path(tmp_path, monkeypatch)
        with open(pointer, "w") as f:
            f.write("sid-from-pointer-must-not-resolve")
        assert cache.resolve_current_session_id() is None

    def test_returns_none_despite_multiple_session_caches_on_disk(self, tmp_path, monkeypatch):
        """The exact tier-4 shape: several caches exist, one strictly newer than the
        others by mtime. The old resolver would have returned the newest; this one must
        return None regardless of which one is newest."""
        _pointer_path(tmp_path, monkeypatch)  # nonexistent -> irrelevant either way
        cache._write_cache("sid-older", cache._read_cache("sid-older"))
        older_path = os.path.join(str(tmp_path), "writ-session-sid-older.json")
        os.utime(older_path, (1000, 1000))

        cache._write_cache("sid-newer", cache._read_cache("sid-newer"))
        newer_path = os.path.join(str(tmp_path), "writ-session-sid-newer.json")
        os.utime(newer_path, (2000, 2000))

        assert cache.resolve_current_session_id() is None

    def test_returns_none_with_a_populated_pointer_and_multiple_caches_present(
        self, tmp_path, monkeypatch
    ):
        """The combined hazard named in the capability: a populated pointer AND several
        caches, at the same time, with neither env signal set. Both guessing tiers are
        live candidates simultaneously and the resolver must still answer None."""
        pointer = _pointer_path(tmp_path, monkeypatch)
        with open(pointer, "w") as f:
            f.write("sid-decoy-pointer")
        cache._write_cache("sid-decoy-pointer", cache._read_cache("sid-decoy-pointer"))
        cache._write_cache("sid-other-cache", cache._read_cache("sid-other-cache"))
        newer_path = os.path.join(str(tmp_path), "writ-session-sid-other-cache.json")
        os.utime(newer_path, (5000, 5000))

        assert cache.resolve_current_session_id() is None

    def test_returns_none_when_the_pointer_names_a_session_that_never_existed(
        self, tmp_path, monkeypatch
    ):
        """The real hazard observed today: a stray test run left the pointer naming
        `tier-embed-directive-597898df`, a session id with no cache file anywhere. The old
        tier 4 could not tell that apart from a legitimate id; this resolver must not even
        try, and must not raise while failing to."""
        pointer = _pointer_path(tmp_path, monkeypatch)
        with open(pointer, "w") as f:
            f.write("tier-embed-directive-597898df")
        assert cache.resolve_current_session_id() is None

    def test_returns_none_when_nothing_resolves_at_all(self, tmp_path, monkeypatch):
        _pointer_path(tmp_path, monkeypatch)  # nonexistent
        # tmp_path (the isolated cache dir) holds no writ-session-*.json files either.
        assert cache.resolve_current_session_id() is None


class TestResolverNeverRaises:
    def test_no_exception_escapes_when_everything_is_absent(self, tmp_path, monkeypatch):
        _pointer_path(tmp_path, monkeypatch)
        try:
            result = cache.resolve_current_session_id()
        except Exception as exc:  # noqa: BLE001
            pytest.fail(f"resolver must never raise, got {type(exc).__name__}: {exc}")
        assert result is None

    def test_no_exception_escapes_with_a_directory_where_the_old_pointer_seam_was(
        self, tmp_path, monkeypatch
    ):
        """A leftover directory at the pointer-path seam (a naive open() would raise
        IsADirectoryError) must not surface, whether or not the resolver still touches
        that seam at all after the tiers are deleted."""
        pointer_dir = tmp_path / "not-a-file"
        pointer_dir.mkdir()
        monkeypatch.setattr(cache, "_SESSION_POINTER_PATH", str(pointer_dir), raising=False)
        try:
            result = cache.resolve_current_session_id()
        except Exception as exc:  # noqa: BLE001
            pytest.fail(f"resolver must never raise, got {type(exc).__name__}: {exc}")
        assert result is None
