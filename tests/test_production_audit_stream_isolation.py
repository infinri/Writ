"""Hermetic guards for tests/_audit_stream.py, the single owner of the
PRODUCTION audit-stream path derivation, row reader, attribution predicate
and counting helper (used by tests/test_advance_phase_token_gate.py's
module-scoped before/after guard against `<skill>/var/logs/<project>/audit.jsonl`).

Every test names, in its own docstring, the one mutation of
tests/_audit_stream.py (or, for the last class, of
tests/test_advance_phase_token_gate.py's token-path resolution) that
reddens it.

FULLY HERMETIC: stdlib-only fixtures (`tmp_path`, `monkeypatch`), pure
source-text reads for the last class, no daemon, no Neo4j, no subprocess.
This file guards the GUARD, so it must never itself touch the real
production audit stream.

CORRECTLY RED as of the testing phase, before tests/_audit_stream.py's
functions are implemented: every function there currently raises
NotImplementedError, so every test in this file that calls one fails on
that exception rather than on the specific mutation named in its
docstring. That is expected -- this file defines the contract the
implementation phase must satisfy, not a claim that the contract is met
yet.
"""
from __future__ import annotations

import json
from pathlib import Path

from tests._audit_stream import (
    attributable_rows,
    count_attributable,
    count_rows,
    is_attributable,
    production_audit_path,
    read_rows,
)

TESTS_DIR = Path(__file__).resolve().parent

# Verbatim from the live audit stream at
# var/logs/github.com/infinri/Writ/audit.jsonl:343 (read 2026-09-09) -- the
# exact shape tests/test_advance_phase_token_gate.py's tokenless-advance
# refusal writes.
LIVE_SELFAPPROVAL_ROW = {
    "ts": "2026-09-08T15:37:13Z",
    "session": "selfapproval-d1786c3f",
    "mode": None,
    "event": "agent_self_approval_blocked",
    "had_token": False,
    "had_expected": False,
    "confirmation_source": "tool",
}

# A foreign row from the same file, unrelated to this test module (a
# write_attempt gate decision from an ordinary scratch-zone write). Must
# NOT be attributed.
FOREIGN_WRITE_ATTEMPT_ROW = {
    "ts": "2026-09-08T13:29:20Z",
    "session": "dfacff61-23d5-474e-846c-2e2f0f0ea482",
    "mode": "work",
    "event": "write_attempt",
    "file_path": "/tmp/example/scratchpad/token_boundary.py",
    "result": "allow",
    "gate_status": "scratch_zone",
    "phase": "planning",
}


def _read_test_module(filename: str) -> str:
    """Pure source-text read, relative to tests/. Never imports or executes
    the target file, so a collection error in a daemon-dependent module
    (e.g. test_advance_phase_token_gate.py's currently-missing
    tests._daemon attributes) cannot propagate into this file's collection."""
    path = TESTS_DIR / filename
    assert path.exists(), f"expected {path} to exist"
    return path.read_text()


class TestProductionAuditPathIsEnvironmentIndependent:
    def test_path_unchanged_when_writ_log_root_and_friction_log_are_set(
        self, tmp_path, monkeypatch
    ) -> None:
        """Reddened by reimplementing production_audit_path() as
        emit_destination("audit"), which reads WRIT_LOG_ROOT and
        WRIT_FRICTION_LOG and would resolve to the throwaway root under this
        test's own env -- the exact change that would make the guard blind
        to the leak it exists to catch."""
        baseline = production_audit_path()
        monkeypatch.setenv("WRIT_LOG_ROOT", str(tmp_path / "log-root"))
        monkeypatch.setenv("WRIT_FRICTION_LOG", str(tmp_path / "collapsed.jsonl"))
        redirected = production_audit_path()
        assert redirected == baseline, (
            "production_audit_path() must not read WRIT_LOG_ROOT or "
            f"WRIT_FRICTION_LOG; got {redirected} with both env vars pointed "
            f"at a tmp dir, expected the unchanged {baseline}"
        )

    def test_path_is_under_the_default_log_root(self) -> None:
        """Reddened by deriving the root from anything other than the
        durable XDG state root's logs/ (e.g. a hardcoded /tmp path, or
        Path.home()).

        CONTAINMENT, NOT DEPTH. This asserted `path.parent.parent.name ==
        "logs"` when it was written, which presumes the project scope is ONE
        path segment. It is not: `resolve_project` answers this checkout's
        clone-stable identity `github.com/infinri/Writ`, three segments, so
        the real stream is `logs/github.com/infinri/Writ/audit.jsonl` and
        the old assertion read `infinri`. The only implementations that could
        satisfy it would flatten the project into one segment, which names a
        path that does not exist, so `read_rows` would return [] and the
        module guard would pass VACUOUSLY forever: the blind guard this cycle
        exists to prevent. Depth is therefore not asserted at all, and
        containment under the default log root is, which is the property
        the docstring above actually claims.
        """
        from writ.shared.state_root import default_log_root

        var_logs = Path(default_log_root())
        path = production_audit_path()
        assert path.name == "audit.jsonl"
        assert var_logs in path.parents, (path, var_logs)

    def test_path_project_segment_is_this_repo(self) -> None:
        """The project segment must resolve to THIS checkout's identity, not
        an unresolved/ungitted fallback. Reddened by deriving the project
        from the process cwd (which pytest may run from anywhere) instead of
        this repo's root explicitly."""
        path = production_audit_path()
        assert path.parent.name not in ("", "_unresolved")


class TestAttributionPredicateMatchesRealRowShape:
    def test_matches_live_row_copied_verbatim(self) -> None:
        """Reddened by keying is_attributable on "session_id", the field
        name these rows do not carry (the live rows carry "session")."""
        assert is_attributable(LIVE_SELFAPPROVAL_ROW) is True

    def test_does_not_match_foreign_write_attempt_row(self) -> None:
        """Reddened by a predicate broad enough to match every row in the
        file (e.g. unconditionally returning True, or keying on a field
        every row carries like "ts")."""
        assert is_attributable(FOREIGN_WRITE_ATTEMPT_ROW) is False

    def test_matches_on_session_prefix_without_the_event(self) -> None:
        """A row carrying the selfapproval session prefix but some other
        event must still be attributed -- the predicate is an OR, not an
        AND, over (session prefix, event). Reddened by requiring both
        conditions simultaneously."""
        row = {**LIVE_SELFAPPROVAL_ROW, "event": "some_other_event"}
        assert is_attributable(row) is True

    def test_matches_on_event_without_the_session_prefix(self) -> None:
        """A row carrying the exact event under an unrelated session id (an
        interactive developer hitting the same route by hand) must still be
        attributed. Reddened by requiring the session prefix in addition to
        the event."""
        row = {**LIVE_SELFAPPROVAL_ROW, "session": "some-other-session-id"}
        assert is_attributable(row) is True

    def test_count_attributable_counts_only_matching_rows(self) -> None:
        """Reddened by count_attributable counting every row regardless of
        is_attributable's verdict (e.g. `return len(rows)`)."""
        rows = [LIVE_SELFAPPROVAL_ROW, FOREIGN_WRITE_ATTEMPT_ROW, LIVE_SELFAPPROVAL_ROW]
        assert count_attributable(rows) == 2


class TestCountingHelperNeverCreatesTheProductionStream:
    def test_read_rows_on_absent_path_returns_empty_and_creates_nothing(
        self, tmp_path
    ) -> None:
        """Reddened by opening the file in append mode, or by adding a
        mkdir(parents=True) inside read_rows."""
        absent = tmp_path / "does-not-exist" / "audit.jsonl"
        assert read_rows(absent) == []
        assert not absent.exists()
        assert not absent.parent.exists()

    def test_count_rows_on_absent_path_returns_zero_and_creates_nothing(
        self, tmp_path
    ) -> None:
        """Same guard as above, for the raw-count helper the failure message
        reports alongside the attributable count."""
        absent = tmp_path / "still-absent" / "audit.jsonl"
        assert count_rows(absent) == 0
        assert not absent.exists()

    def test_attributable_rows_on_absent_path_returns_empty_and_creates_nothing(
        self, tmp_path
    ) -> None:
        absent = tmp_path / "also-absent" / "audit.jsonl"
        assert attributable_rows(absent) == []
        assert not absent.exists()

    def test_read_rows_on_a_real_file_parses_every_line(self, tmp_path) -> None:
        """Sanity pin against the vacuous-stub failure mode: a read_rows that
        unconditionally returns [] would pass every absent-path assertion
        above while never actually reading a present file. Reddened by
        exactly that: `def read_rows(path): return []`."""
        path = tmp_path / "audit.jsonl"
        path.write_text(
            json.dumps(LIVE_SELFAPPROVAL_ROW)
            + "\n"
            + json.dumps(FOREIGN_WRITE_ATTEMPT_ROW)
            + "\n"
        )
        rows = read_rows(path)
        assert rows == [LIVE_SELFAPPROVAL_ROW, FOREIGN_WRITE_ATTEMPT_ROW]
        assert count_rows(path) == 2
        assert attributable_rows(path) == [LIVE_SELFAPPROVAL_ROW]


class TestTokenGateModuleResolvesThroughProductionTokenPath:
    """Source-text scan only (see _read_test_module) -- the behavioral half
    of this guard (invoking the module's own token-path helper and
    comparing it to writ.session.gate_token.gate_token_path under a
    monkeypatched TMPDIR) lives in
    tests/test_advance_phase_token_gate.py::TestTokenPathResolvesThroughProductionResolver,
    since that module already depends on tests._daemon and is not
    collectible standalone before the implementation phase lands.
    """

    def test_module_imports_gate_token_path(self) -> None:
        """Reddened by restoring a locally reimplemented
        os.path.join(tempfile.gettempdir(), f"writ-gate-token-{session_id}")
        instead of importing the production resolver."""
        src = _read_test_module("test_advance_phase_token_gate.py")
        assert "from writ.session.gate_token import" in src, (
            "test_advance_phase_token_gate.py must import "
            "gate_token_path from writ.session.gate_token"
        )
        assert "gate_token_path" in src

    def test_module_does_not_reimplement_the_join_via_tempfile(self) -> None:
        """Reddened by restoring tempfile.gettempdir() as the token-path
        root: writ.session.gate_token.gate_token_path hardcodes /tmp (it
        must match the bash writer auto-approve-gate.sh byte-for-byte), so a
        TMPDIR-based local join diverges from it the instant TMPDIR is set,
        and the writer (this test) and the daemon's reader
        (server.read_gate_token -> gate_token_path) would disagree."""
        src = _read_test_module("test_advance_phase_token_gate.py")
        assert "tempfile.gettempdir()" not in src, (
            "test_advance_phase_token_gate.py must not reimplement the "
            "token-path join via tempfile.gettempdir(); it must resolve "
            "the path through writ.session.gate_token.gate_token_path(session_id)"
        )
