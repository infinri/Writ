"""A hook that writes into the live checkout has ignored the isolation it was given.

WRIT_CACHE_DIR is how a test, an audit, or a sandboxed session redirects everything Writ
writes somewhere disposable. A hook that builds its path as `$WRIT_DIR/cache/$SESSION_ID`
resolves that against its OWN location, so it writes into the real repository no matter
what the caller set -- and the caller has no way to tell, because the hook still exits 0
and the sandbox it was handed simply stays empty.

Found on 2026-08-08, in validate-file.sh: an audit run with WRIT_CACHE_DIR pointed at a
throwaway directory left `cache/test-audit-session-0001/` sitting in the checkout
afterwards. That one was fixed; two siblings on the same pending-test path were not, so
the same audit would have leaked through them. Isolation that one writer ignores is not
isolation, which is why the scan below covers every shell file rather than the three that
were known to be wrong.

Two layers, because either alone is weak:
  - the SCAN proves no hook constructs a cache path without the seam, including hooks
    written after this one. It reads whole files through tests/_scope.py, so the roots it
    searches are checked against the universe before it can report a zero.
  - the BEHAVIOURAL tests prove the seam is actually honoured at runtime, which a scan for
    a spelling cannot show.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from tests._scope import DEFAULT_IGNORE, Universe, scan, shell_file

# autouse: pins cwd to a sandbox so `mode set` cannot delete THIS repo's gate artifacts.
from tests.fixtures.session_state import sandbox_cwd  # noqa: F401

REPO = Path(__file__).resolve().parent.parent

MARK_HOOK = REPO / "hooks" / "scripts" / "writ-mark-pending-test.sh"
RUN_HOOK = REPO / "hooks" / "scripts" / "writ-run-pending-tests.sh"
HELPER = REPO / "bin" / "lib" / "writ-session.py"

# Same declared universe as test_session_identity_no_fallback.py, and for the same reason:
# a cache path can be built in any shell file, so a scan over one directory would report a
# zero it never earned. docs/ is ignored -- the only shell under it is the archived
# pressure-run transcripts, which nothing sources and nothing executes.
SHELL_UNIVERSE = Universe(
    base=REPO,
    dirs=("hooks/scripts", "hooks/git", "bin", "bin/lib", "scripts", "scripts/lib"),
    match=shell_file,
    ignore=DEFAULT_IGNORE + ("docs",),
)
SCAN_ROOTS = tuple(REPO / d for d in SHELL_UNIVERSE.dirs)

# A cache directory built from the script's own location: `$WRIT_DIR/cache`,
# `${SKILL_DIR}/cache`, `$_WRIT_SKILL_DIR/cache`. The correct form names the same default
# INSIDE the seam, so `_without_seam` below removes every `${WRIT_CACHE_DIR:-...}`
# expansion before this pattern runs and what is left is the hardcoded kind.
HARDCODED_CACHE = re.compile(r"\$\{?(?:WRIT_DIR|SKILL_DIR|_WRIT_SKILL_DIR)\}?/cache")

SEAM = re.compile(r"\$\{WRIT_CACHE_DIR:-[^}]*\}")
COMMENT_LINE = re.compile(r"^[ \t]*#.*$", re.M)


def _without_seam(text: str) -> str:
    """Drop the correct construction and the prose, leaving only real path-building.

    The seam goes first because `${WRIT_CACHE_DIR:-$SKILL_DIR/cache}` contains the very
    substring being searched for -- scanning the raw text would flag the fix as the defect.
    Whole-line comments go too: validate-file.sh describes the old broken path verbatim in
    the comment explaining why it changed, and a scan that flagged that would be answered
    by deleting the explanation.
    """
    return COMMENT_LINE.sub("", SEAM.sub("", text))


class TestNoHookBuildsACachePathFromItsOwnLocation:
    def test_the_scan_finds_no_hardcoded_cache_root(self):
        offenders = scan(HARDCODED_CACHE, roots=SCAN_ROOTS, universe=SHELL_UNIVERSE,
                         transform=_without_seam)
        assert offenders == {}, (
            f"these build a cache path that ignores WRIT_CACHE_DIR, so an isolated run "
            f"still writes into the live checkout: {offenders}. Use "
            f'"${{WRIT_CACHE_DIR:-$WRIT_DIR/cache}}/$SESSION_ID".'
        )

    def test_the_pattern_matches_a_planted_offender(self, tmp_path):
        """Anti-vacuity for the regex: a green scan must mean the string is absent, not
        that the pattern never had teeth."""
        planted = 'MARKER_DIR="$WRIT_DIR/cache/$PARENT_SID"'
        assert HARDCODED_CACHE.findall(_without_seam(planted)), (
            "the pattern does not match the exact line this test exists to prevent"
        )

    def test_the_transform_does_not_erase_a_planted_offender(self):
        """Anti-vacuity for the transform, which is the part that could quietly swallow
        everything: stripping the seam and the comments must leave real code alone."""
        text = (
            '# an unguarded "$SKILL_DIR/cache/$SESSION_ID" is the defect\n'
            'GOOD="${WRIT_CACHE_DIR:-$SKILL_DIR/cache}/$SESSION_ID"\n'
            'BAD="$WRIT_DIR/cache/$SESSION_ID"\n'
        )
        assert HARDCODED_CACHE.findall(_without_seam(text)) == ["$WRIT_DIR/cache"], (
            "the transform must remove the comment and the seam and keep the bare one"
        )


def _env(cache_dir: Path) -> dict:
    env = os.environ.copy()
    env["WRIT_CACHE_DIR"] = str(cache_dir)
    env["WRIT_FRICTION_LOG"] = str(cache_dir / "friction.log")
    env["WRIT_LOG_ROOT"] = str(cache_dir / "logs")
    return env


def _seed_work_mode(cache_dir: Path, sid: str) -> None:
    subprocess.run([sys.executable, str(HELPER), "mode", "set", "work", sid],
                   env=_env(cache_dir), check=True, capture_output=True, text=True)


class TestTheSeamIsHonouredAtRuntime:
    """The scan pins a spelling; these pin the behaviour it is a proxy for.

    Every assertion is POSITIVE -- the marker is found inside the sandbox -- so a failing
    run reports the leak without this test itself needing to write anywhere real. The
    live-checkout check afterwards is a second reading of the same run, not the primary
    one, and it cleans up what a broken hook created rather than leaving it behind.
    """

    SID = "cache-seam-session"

    @pytest.fixture(autouse=True)
    def _no_leak_before_or_after(self):
        """Clear this session's directory in the live cache around every test here.

        BEFORE, so "the directory exists afterwards" can only mean this run created it --
        without that, one earlier red run leaves a directory behind and every later run
        reads it as a fresh leak. AFTER, so a red run does not contaminate the checkout it
        is complaining about, or poison the next run into failing for the previous one's
        reason. The path is safe to remove unconditionally: SID is a literal that only
        these tests ever write under.
        """
        leaked = REPO / "cache" / self.SID
        shutil.rmtree(leaked, ignore_errors=True)
        yield
        shutil.rmtree(leaked, ignore_errors=True)

    def test_the_pending_test_marker_lands_in_the_redirected_cache(self, tmp_path):
        _seed_work_mode(tmp_path, self.SID)
        # A path matching the bundled `python-generic` src glob (`*/src/*.py`), which the
        # hook checks by pattern and never opens. Naming a file in this repo instead would
        # tie the test to Writ's own layout, which does not use src/.
        envelope = {
            "session_id": self.SID,
            "hook_event_name": "PostToolUse",
            "tool_name": "Write",
            "tool_input": {"file_path": "/proj/src/widget.py"},
        }
        res = subprocess.run(["bash", str(MARK_HOOK)], input=json.dumps(envelope),
                             capture_output=True, text=True, env=_env(tmp_path), timeout=30)
        assert res.returncode == 0, res.stderr

        marker = tmp_path / self.SID / "pending-tests.txt"
        assert marker.is_file(), (
            f"the marker was not written inside WRIT_CACHE_DIR; the hook resolved its own "
            f"location instead. stderr={res.stderr!r}"
        )
        assert "/proj/src/widget.py" in marker.read_text()

    def test_nothing_was_written_into_the_live_checkout(self, tmp_path):
        """The leak itself, read from the repository rather than from the sandbox.

        The sandbox assertion above and this one are not the same check. A hook could
        satisfy that one and still write to both places; only this reads the repository.
        """
        self.test_the_pending_test_marker_lands_in_the_redirected_cache(tmp_path)

        leaked = REPO / "cache" / self.SID
        assert not leaked.exists(), (
            f"the hook wrote {leaked} into the live checkout while WRIT_CACHE_DIR "
            f"pointed at {tmp_path}"
        )

    def test_the_stop_hook_reads_the_marker_the_write_hook_left(self, tmp_path):
        """The two hooks must agree on WHERE, in both configurations.

        A one-sided fix is worse than no fix: the writer moves into the sandbox, the reader
        keeps looking in the checkout, and the pending-test run silently stops happening
        with every hook still exiting 0.

        The marker names a source file with no test, so the hook consumes the marker and
        stops. Consumption happens before that check, so it still proves the read; planting
        a real path would make this test run the suite it points at.
        """
        _seed_work_mode(tmp_path, self.SID)
        marker_dir = tmp_path / self.SID
        marker_dir.mkdir(parents=True, exist_ok=True)
        (marker_dir / "pending-tests.txt").write_text("writ/no_such_module_xyz.py\n")

        envelope = {"session_id": self.SID, "hook_event_name": "Stop"}
        res = subprocess.run(["bash", str(RUN_HOOK)], input=json.dumps(envelope),
                             capture_output=True, text=True, env=_env(tmp_path), timeout=180)
        assert res.returncode in (0, 2), res.stderr

        assert (marker_dir / "pending-tests.txt").read_text() == "", (
            "the Stop hook did not consume the marker in WRIT_CACHE_DIR, so it was reading "
            "a different directory than the PostToolUse hook writes to"
        )
