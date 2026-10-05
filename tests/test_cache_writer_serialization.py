"""Layer 2 (fix/session-mode-preserve): every whole-dict session-cache writer must
go through the flock-guarded mutate_cache, not a bare _read_cache/_write_cache pair,
so a stale snapshot can never lost-update mode/current_phase/gates_approved (door C
of the mode=None wipe). Plus the three new cmd_update field-set handlers the hook
writers (layer 3) reuse.

RED today: the listed writers still call _write_cache directly, and the three
--set-* flags do not exist. Hermetic per TEST-ISOLATE-001 (WRIT_CACHE_DIR).
"""
from __future__ import annotations

import inspect
import os

import pytest

from writ.session.cache import _cache_path, _read_cache, _write_cache


def _seed(session_id: str, data: dict) -> None:
    _write_cache(session_id, data)


# (module attribute path, function) for every writer migrated to mutate_cache.
def _migrated_writers() -> list:
    from writ.session import (
        budget_tracking,
        feedback,
        gates,
        investigations,
        mode_engine,
        session_lifecycle,
        violations,
    )

    return [
        budget_tracking.cmd_update,
        mode_engine._mode_set,
        mode_engine._mode_switch,
        mode_engine._mode_init,
        gates._log_gate_denial,
        session_lifecycle.cmd_clear_rules_for_compaction,
        session_lifecycle.cmd_reset_after_compaction,
        investigations.cmd_record_analysis,
        feedback.cmd_auto_feedback,
        violations.cmd_add_pending_violation,
        violations.cmd_clear_pending_violations,
        violations.cmd_invalidate_gate,
    ]


class TestMigratedWritersUseMutateCache:
    @pytest.mark.parametrize("fn", _migrated_writers(), ids=lambda f: f.__name__)
    def test_writer_uses_mutate_cache_not_bare_write(self, fn) -> None:
        src = inspect.getsource(fn)
        assert "mutate_cache" in src, (
            f"{fn.__module__}.{fn.__name__} must perform its cache write under "
            f"mutate_cache (the per-session flock), not an unlocked read/write"
        )
        assert "_write_cache(" not in src, (
            f"{fn.__module__}.{fn.__name__} must not call _write_cache directly -- "
            f"an unlocked whole-dict write can clobber a newer mode/gates value"
        )


class TestNewUpdateHandlers:
    """The two field-set flags set exactly their field and never disturb an
    existing mode -- the property the hook writers rely on to stop wiping mode."""

    def _run(self, monkeypatch, tmp_path, args: list[str]):
        monkeypatch.setenv("WRIT_CACHE_DIR", str(tmp_path))
        from writ.session.budget_tracking import cmd_update

        session_id = "handler-sid"
        _seed(session_id, {"mode": "work", "current_phase": "planning", "gates_approved": ["phase-a"]})
        cmd_update(session_id, args)
        return _read_cache(session_id)

    def test_set_recall_briefed_sets_flag_and_preserves_mode(self, tmp_path, monkeypatch) -> None:
        cache = self._run(monkeypatch, tmp_path, ["--set-recall-briefed"])
        assert cache.get("recall_briefed") is True
        assert cache.get("mode") == "work"
        assert cache.get("gates_approved") == ["phase-a"]

    def test_set_escalation_feedback_sent_sets_nested_flag_and_preserves_mode(self, tmp_path, monkeypatch) -> None:
        cache = self._run(monkeypatch, tmp_path, ["--set-escalation-feedback-sent"])
        assert cache.get("escalation", {}).get("feedback_sent") is True
        assert cache.get("mode") == "work"


class TestAddViolationDedupNoWrite:
    """A duplicate pending-violation must be a true no-op: no lock, no rewrite, no
    mtime bump (validate-rules.sh re-adds the same triple across retry loops, and
    cache mtime is a freshness signal for sub-agent cache collection)."""

    def test_duplicate_violation_does_not_rewrite_cache(self, tmp_path, monkeypatch) -> None:
        monkeypatch.setenv("WRIT_CACHE_DIR", str(tmp_path))
        from writ.session.violations import cmd_add_pending_violation

        session_id = "dedup-noop-sid"
        _seed(session_id, {"mode": "work", "pending_violations": [
            {"rule_id": "R-1", "file": "a.py", "line": None, "evidence": "x"}]})
        path = _cache_path(session_id)
        before = os.stat(path)

        cmd_add_pending_violation(session_id, ["--rule", "R-1", "--file", "a.py"])

        after = os.stat(path)
        assert (after.st_ino, after.st_mtime_ns) == (before.st_ino, before.st_mtime_ns), (
            "a duplicate (rule, file, line) triple must not rewrite the cache file"
        )

    def test_new_violation_is_appended(self, tmp_path, monkeypatch) -> None:
        monkeypatch.setenv("WRIT_CACHE_DIR", str(tmp_path))
        from writ.session.violations import cmd_add_pending_violation

        session_id = "dedup-append-sid"
        _seed(session_id, {"mode": "work", "pending_violations": []})
        cmd_add_pending_violation(session_id, ["--rule", "R-9", "--file", "b.py", "--evidence", "e"])
        v = _read_cache(session_id)["pending_violations"]
        assert len(v) == 1 and v[0]["rule_id"] == "R-9"
        assert _read_cache(session_id)["mode"] == "work"
