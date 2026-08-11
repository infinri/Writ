"""Session identity comes from the hook payload, or the hook records a critical error.

WHY THERE IS NO FALLBACK. Two shapes of "helpful" fallback were producing silently wrong
answers, and both were indistinguishable from a normal run:

  - `/tmp/writ-current-session` is ONE global file, rewritten on every UserPromptSubmit in
    EVERY Claude Code session on this machine. It names whichever session took a turn most
    recently. Verified live 2026-08-08: it held `cdp-12096297` while the active session was
    `84bd3178-...`. The telemetry drain keyed off it and stranded 999 rows across 16
    sessions before anyone noticed.
  - `ps -o ppid=` and `md5(cwd:user)-date` synthesize an id that can never equal the one
    Claude Code uses, so state written under it is written to a session that does not exist
    and is never read again.

The stakes are not uniform. For the drain it is lost telemetry; for the gate hooks it is an
approval, a plan validation, a violation sweep, or a manual-testing GRANT applied to a
session other than the one whose user spoke. None of those left a trace.

Claude Code documents `session_id` as universal and authoritative on every hook event
(docs/reference/claude-code-blackbox.md), so an empty one is a broken invariant rather than
a case to paper over. Hooks now record `critical_error` and do nothing.

WHY THIS FILE WAS REWRITTEN. It existed, it passed, and the fallbacks were still live. It
missed them twice over, and both misses are now pinned by TestTheScanReachesWhatItMissed:

  1. It globbed `hooks/scripts/*.sh` ONLY. The synthesis had been deleted from 9 hooks and
     left in `bin/lib/common.sh`, which is the one file every hook sources -- so the scan
     read 37 copies of the fix and never opened the original. It now scans bin/lib too.
  2. Its pointer regex keyed on the DESTINATION VARIABLE NAME (`SESSION_ID|PARENT_SESSION`),
     so `PARENT="$(cat /tmp/writ-current-session ...)"` in writ-subagent-start.sh was
     invisible for want of one word. A scan that only recognizes the spellings already
     fixed can only ever confirm the last fix. It now matches ANY assignment from that
     file, however the variable is named.

PART 1 OF THE ISOLATION CYCLE (session identity) NARROWS THE READER SET FURTHER. Before
this cycle, four callers had no hook payload and so read the pointer as a last resort:
`resolve_current_session_id()` itself (tiers 3 and 4, serving `mode set` with no sid and
`writ doctor`), `bin/audit-region.sh`, `hooks/git/post-commit`, and
`session-start-bootstrap.sh`'s pre-rotation read. This cycle deletes the resolver's own
pointer/mtime-glob tiers and converts `bin/audit-region.sh` to require `--session` or
`$CLAUDE_SESSION_ID`, so only TWO no-payload readers remain: `hooks/git/post-commit` (a git
hook, which Claude Code never invokes with an envelope) and `session-start-bootstrap.sh`
(which is asking a different question -- "which session did the harness just rotate away
from" -- not "which session am I", and is exempted separately below). This file is RED on
that narrowing until `bin/audit-region.sh` stops reading the pointer: today it still does,
so `NO_PAYLOAD_POINTER_READERS` below must drop it to make the scan in
`test_no_hook_reads_the_global_pointer_as_its_session` catch it like any other offender.
Per TEST-TDD-001: skeletons approved before implementation.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

from tests._scope import DEFAULT_IGNORE, Universe, scan, shell_file

REPO = Path(__file__).resolve().parent.parent
HOOKS = REPO / "hooks" / "scripts"
LIB = REPO / "bin" / "lib"
COMMON_SH = LIB / "common.sh"

# BOTH directories. `bin/lib` is not an afterthought here: load_hook_env and
# detect_session_id live there and are called by ~20 hooks, so a fallback in that file has
# a wider blast radius than a fallback in any single hook.
SCANNED_DIRS = (HOOKS, LIB)

# THE SCOPE, DECLARED AND CHECKED (tests/_scope.py).
#
# Widening this scan to bin/lib was remembered, not enforced: nothing stopped the next
# absence claim from globbing one directory again and reporting the zero it did not earn.
# SHELL_UNIVERSE names every directory a shell file can live in here, and `scan` refuses to
# run at all if SCAN_ROOTS misses one of them, or if a shell file turns up outside the
# universe. Narrowing the roots below now fails the two scans instead of quieting them.
#
# docs/ is ignored because the only shell under it is the archived pressure-run transcripts
# in docs/pressure-runs/PSR-00*, which nothing sources and nothing executes.
SHELL_UNIVERSE = Universe(
    base=REPO,
    dirs=("hooks/scripts", "hooks/git", "bin", "bin/lib", "scripts", "scripts/lib"),
    match=shell_file,
    ignore=DEFAULT_IGNORE + ("docs",),
)
SCAN_ROOTS = tuple(REPO / d for d in SHELL_UNIVERSE.dirs)

# A session id built from process or environment facts rather than read from the payload.
SYNTHETIC = re.compile(r"ps\s+-o\s+ppid=|md5sum\s*\|\s*cut\s+-c1-12")

POINTER = "/tmp/writ-current-session"

# The pointer file used as a SOURCE of session identity: ANY assignment whose right-hand
# side READS the file, whatever the variable is called and however it is read (`cat`, or a
# `<` redirect). Naming the path without reading it -- writ-state-write-gate's
# POINTER_FILE, writ-bash-write-gate's _POINTER, this docstring -- is a different and
# correct use and stays unmatched, because otherwise the cheapest way to pass this test
# would be to hide the path.
POINTER_AS_SOURCE = re.compile(
    r"[A-Za-z_][A-Za-z0-9_]*=[^\n]*?(?:\bcat\b|<)[^\n]*?" + re.escape(POINTER)
)

# The scan the fallbacks walked past, kept so the widening can be shown to be the thing
# that catches them (TestTheScanReachesWhatItMissed) rather than asserted to be.
LEGACY_POINTER_AS_SOURCE = re.compile(
    r"(?:SESSION_ID|PARENT_SESSION)\s*=.*\bcat\s+" + re.escape(POINTER)
)

# THE ONE EXEMPTION, by name, with its reason.
#
# session-start-bootstrap.sh reads the pointer at SessionStart to recover the PRE-rotation
# session id, and that id is by definition NOT in the payload: the payload carries the new
# one. It is not asking "which session am I" -- it already knows -- it is asking "which
# session did the harness just rotate away from", and the answer is used only to carry a
# MODE forward, guarded to the same project, with gates reset.
#
# It is a weaker instance of the same shape and it is NOT endorsed here: if two Claude Code
# sessions on this machine interleave, the pointer can name the other one and this would
# carry that session's mode. Removing it means removing the rotation carry-forward, which
# is a product decision, not a cleanup (out of scope for Part 1 -- see plan.md). Flagged
# for that decision; exempted by name so the scan stays sharp for every other file in the
# meantime.
POINTER_READ_EXEMPT = frozenset({"session-start-bootstrap.sh"})

# ONE MORE, surfaced by covering the whole universe rather than hooks/scripts + bin/lib.
#
# `hooks/git/post-commit` is a git hook: git hands it no Claude Code envelope, ever, so it
# has no payload to take an identity from and the pointer is the only session name
# available to it. `test_the_no_payload_exemptions_are_still_no_payload_callers` holds it
# to exactly one read.
#
# `bin/audit-region.sh` used to belong here too (it is also a no-payload CLI, not a hook),
# but Part 1 of the isolation cycle converts it to require `--session` or
# `$CLAUDE_SESSION_ID` and removes its pointer read entirely: an operator invoking a CLI
# can be asked for `--session`, whereas git cannot be asked for anything. It is deliberately
# ABSENT from this set now, so the general scan below (not a separate exemption) is what
# proves its read is gone -- if the read comes back, this file fails at
# `test_no_hook_reads_the_global_pointer_as_its_session`, not silently through a stale
# allowlist entry.
NO_PAYLOAD_POINTER_READERS = frozenset({"post-commit"})

_FULL_LINE_COMMENT = re.compile(r"^[ \t]*#.*$", re.M)
_TRAILING_COMMENT = re.compile(r"[ \t]#[ \t].*$", re.M)


def _strip_comments(src: str) -> str:
    """Scan the CODE, not the prose about the code.

    Every one of these hooks now carries a comment explaining which fallback was removed
    and why, and those comments quote the fallback. Matching them would make the only
    passing move "stop writing down what happened", which is the opposite of the point.

    Deliberately conservative: a full-line `#`, or a `#` fenced by whitespace on both
    sides. `${t#*[.,]}` and `${path%%/*}` are untouched (no whitespace before the `#`), and
    the transform only ever REMOVES text, so it can hide an offender but never invent one.
    """
    return _TRAILING_COMMENT.sub("", _FULL_LINE_COMMENT.sub("", src))


def _hook_sources(dirs: tuple[Path, ...] = SCANNED_DIRS) -> list[tuple[Path, str]]:
    """(path, comment-stripped source) for every shell file in the scanned dirs."""
    return [(p, _strip_comments(p.read_text(errors="replace")))
            for d in dirs for p in sorted(d.glob("*.sh"))]


def _offenders(pattern: re.Pattern, sources: list[tuple[Path, str]],
               exempt: frozenset = frozenset()) -> dict[str, list]:
    return {p.name: pattern.findall(src) for p, src in sources
            if p.name not in exempt and pattern.search(src)}


class TestNoHookInventsASession:
    def test_the_scan_covers_the_shared_helpers_not_just_the_hooks(self) -> None:
        """The miss that made this whole cycle necessary: 9 hooks were fixed, the file
        they all source was not, and the scan could not see it."""
        scanned = {p.name for p, _ in _hook_sources()}
        assert "common.sh" in scanned, (
            "bin/lib/common.sh is out of the scan. load_hook_env and detect_session_id "
            "live there and ~20 hooks call them, so a fallback there reaches further than "
            "a fallback in any single hook"
        )
        assert len([p for p, _ in _hook_sources() if p.parent == HOOKS]) > 20

    def test_no_synthetic_session_ids(self) -> None:
        offenders = scan(SYNTHETIC, roots=SCAN_ROOTS, universe=SHELL_UNIVERSE,
                         transform=_strip_comments)
        assert offenders == {}, (
            f"a hook synthesizes a session id from PID or cwd. That id can never match the "
            f"one Claude Code uses, so everything written under it is lost silently: {offenders}"
        )

    def test_no_hook_reads_the_global_pointer_as_its_session(self) -> None:
        """Part 1: `bin/audit-region.sh` is no longer in the exempt set, so if its pointer
        read (currently at line 27-29) is still there, this scan must catch it exactly as
        it would catch any hook that grew a new one -- RED until that read is removed."""
        offenders = scan(POINTER_AS_SOURCE, roots=SCAN_ROOTS, universe=SHELL_UNIVERSE,
                         transform=_strip_comments,
                         exempt=POINTER_READ_EXEMPT | NO_PAYLOAD_POINTER_READERS)
        assert offenders == {}, (
            f"a hook takes its session identity from {POINTER}, which names "
            f"whichever session on this machine took a turn most recently: {offenders}"
        )

    def test_the_scans_are_not_vacuous(self) -> None:
        """Both regexes must match the code they were written against, or they are decoration."""
        assert SYNTHETIC.search('SESSION_ID=$(ps -o ppid= -p $PPID)')
        assert SYNTHETIC.search('SESSION_ID=$(echo x | md5sum | cut -c1-12)-$(date)')
        assert POINTER_AS_SOURCE.search(f'SESSION_ID=$(cat {POINTER} 2>/dev/null)')
        # The exact line that slipped past the old regex, verbatim from writ-subagent-start.sh.
        assert POINTER_AS_SOURCE.search(f"""PARENT="$(cat {POINTER} 2>/dev/null || echo '')\"""")
        # And a `<` redirect, which no `cat`-only pattern can see.
        assert POINTER_AS_SOURCE.search(f"""PREV="$(tr -d '[:space:]' < {POINTER})\"""")

    def test_naming_the_pointer_as_a_protected_path_is_allowed(self) -> None:
        """writ-state-write-gate guards the pointer FILE from being written. That is a
        different use and must not be flagged, or the fix would push it to hide the path.
        Part 1 leaves this file exactly as it is (it names the path, it does not read it
        as a session id), so this stays a pinning test, not a conversion target."""
        assert not POINTER_AS_SOURCE.search(f'POINTER_FILE="{POINTER}"')
        assert not POINTER_AS_SOURCE.search(f'_POINTER = "{POINTER}"')
        assert (HOOKS / "writ-state-write-gate.sh").exists()
        gate_src = (HOOKS / "writ-state-write-gate.sh").read_text()
        assert "writ-current-session" in gate_src
        assert not POINTER_AS_SOURCE.search(_strip_comments(gate_src)), (
            "writ-state-write-gate.sh only names the pointer as a protected path; it must "
            "never start READING it as a session id"
        )

    def test_enforce_violations_comment_is_not_mistaken_for_a_read(self) -> None:
        """enforce-violations.sh:30 (line number as of this cycle's plan; verify on read)
        is a COMMENT quoting the fallback this whole file exists to remove, not a live
        read. Its actual identity line uses `writ_require_session`, payload-only, already.
        Part 1 leaves this file untouched -- it is already correct -- so this is a pin
        against the plan's own correction of the original description, not a conversion."""
        src = (HOOKS / "enforce-violations.sh").read_text()
        assert "writ_require_session" in src
        assert not POINTER_AS_SOURCE.search(_strip_comments(src)), (
            "enforce-violations.sh must not gain a live pointer read; its only mention of "
            f"{POINTER} must stay inside a comment"
        )

    def test_publishing_the_pointer_is_not_reading_it(self) -> None:
        """Both writers still WRITE the pointer, and neither must be flagged for it.

        Two callers read it after Part 1 and not one of them has a hook payload to read an
        id from: `hooks/git/post-commit` (a git hook -- git hands it no envelope, ever) and
        `session-start-bootstrap.sh`'s rotation carry-forward (asking which session the
        harness just rotated away from, not which session this is). Deleting the write
        would not remove the risk for either one; it would leave them with NO signal at
        all, which used to be racier (the mtime glob) and is now simply nothing, since
        Part 1 deletes that glob too. Removing the pointer write is explicitly out of
        scope for this cycle (plan.md, "Deliberately out of scope").
        """
        assert not POINTER_AS_SOURCE.search(f'echo "$SESSION_ID" > {POINTER}')
        rag = _strip_comments((HOOKS / "writ-rag-inject.sh").read_text())
        assert f"> {POINTER}" in rag, "the publish was removed; re-check the two readers first"

        # The SECOND writer, missed by the first plan and documented at HANDBOOK.md:37.
        approve = _strip_comments((HOOKS / "auto-approve-gate.sh").read_text())
        assert f"> {POINTER}" in approve, (
            "auto-approve-gate.sh is the second pointer writer (HANDBOOK.md:37); Part 1 "
            "keeps both writes, it only narrows who is allowed to read the result"
        )

    def test_the_one_exemption_is_still_the_read_it_was_granted_for(self) -> None:
        """An allowlist nobody re-checks is a hole. This asserts the exempted file still
        contains exactly the read it was exempted for, and that the exemption is load-
        bearing (the regex really does match it), so it cannot quietly become a blank
        cheque for a second, different pointer read in the same file."""
        for name in POINTER_READ_EXEMPT:
            raw = (HOOKS / name).read_text()
            hits = POINTER_AS_SOURCE.findall(_strip_comments(raw))
            assert len(hits) == 1, (
                f"{name} is exempt for ONE read (the pre-rotation id) and now has "
                f"{len(hits)}. Review the new one on its own merits."
            )
            assert "carry-forward-mode" in raw, (
                f"{name} no longer carries the mode forward, so the reason for its "
                f"exemption is gone -- delete the exemption and the read together"
            )

    def test_the_no_payload_exemption_is_still_the_no_payload_caller(self) -> None:
        """Same standard for the one caller Part 1 leaves in this set.

        It is exempt for one reason only -- nothing hands it a hook payload -- so it is
        held to the single read it was exempted for. If it ever grows a second read, or
        starts reading a payload, the exemption stops being the one that was granted.
        `bin/audit-region.sh` is deliberately NOT checked here: after Part 1 it should
        have ZERO reads, which is what the general scan above proves, not this allowlist
        pin."""
        paths = {"post-commit": REPO / "hooks" / "git" / "post-commit"}
        assert set(paths) == set(NO_PAYLOAD_POINTER_READERS)
        for name, path in paths.items():
            src = _strip_comments(path.read_text())
            hits = POINTER_AS_SOURCE.findall(src)
            assert len(hits) == 1, (
                f"{name} is exempt for ONE read and now has {len(hits)}. Review the new "
                f"one on its own merits."
            )
            assert "hook_event_name" not in src and "load_hook_env" not in src, (
                f"{name} now reads a hook payload, so it has a real session id to use and "
                f"the reason for its exemption is gone"
            )

    def test_audit_region_no_longer_names_or_reads_the_pointer(self) -> None:
        """Capability 6/8 (Part 1): bin/audit-region.sh drops the pointer entirely -- no
        read, and (since it is the whole point) no residual reference that looks like one.
        RED today: audit-region.sh:27-29 still reads the pointer as its fallback."""
        src = _strip_comments((REPO / "bin" / "audit-region.sh").read_text())
        assert not POINTER_AS_SOURCE.search(src), (
            "bin/audit-region.sh must not read the pointer as a session-id source after "
            "Part 1; it now requires --session or $CLAUDE_SESSION_ID"
        )
        assert "CLAUDE_SESSION_ID" in src, (
            "bin/audit-region.sh must accept $CLAUDE_SESSION_ID as its env fallback"
        )


class TestTheScanReachesWhatItMissed:
    """Anti-vacuity, against the real misses rather than against a slogan.

    Each test reconstructs the code that was live while the old scan reported clean, and
    asserts twice: the strengthened scan flags it, AND the old scan did not. Without the
    second half, "the scan catches it" is unfalsifiable -- a regex that matches everything
    would pass too.
    """

    # Verbatim, as they stood in bin/lib/common.sh's load_hook_env.
    HISTORICAL_SYNTHESIS = (
        '    if [ -z "${HOOK_SESSION_ID:-}" ]; then\n'
        '        HOOK_SESSION_ID=$(ps -o ppid= -p $PPID 2>/dev/null | tr -d \' \')\n'
        '    fi\n'
        '    if [ -z "${HOOK_SESSION_ID:-}" ]; then\n'
        '        HOOK_SESSION_ID=$(echo "${PWD}:${USER}" | md5sum | cut -c1-12)-$(date +%Y%m%d)\n'
        '    fi\n'
    )
    # Verbatim, as it stood at writ-subagent-start.sh line 151.
    HISTORICAL_PARENT_READ = f"""PARENT="$(cat {POINTER} 2>/dev/null || echo '')"\n"""

    @pytest.fixture
    def fake_repo(self, tmp_path: Path) -> tuple[Path, Path]:
        """A hooks/scripts + bin/lib pair holding the REAL files, ready to be re-broken."""
        hooks = tmp_path / "hooks" / "scripts"
        lib = tmp_path / "bin" / "lib"
        hooks.mkdir(parents=True)
        lib.mkdir(parents=True)
        shutil.copy(COMMON_SH, lib / "common.sh")
        shutil.copy(HOOKS / "writ-subagent-start.sh", hooks / "writ-subagent-start.sh")
        return hooks, lib

    def test_the_unbroken_copy_is_clean(self, fake_repo) -> None:
        """The control. If this failed, every assertion below would pass for free."""
        hooks, lib = fake_repo
        sources = _hook_sources((hooks, lib))
        assert _offenders(SYNTHETIC, sources) == {}
        assert _offenders(POINTER_AS_SOURCE, sources) == {}

    def test_a_synthesis_in_bin_lib_common_sh_is_caught(self, fake_repo) -> None:
        hooks, lib = fake_repo
        target = lib / "common.sh"
        target.write_text(target.read_text() + "\n" + self.HISTORICAL_SYNTHESIS)

        assert "common.sh" in _offenders(SYNTHETIC, _hook_sources((hooks, lib)))

    def test_the_old_hooks_only_glob_would_have_missed_that_synthesis(self, fake_repo) -> None:
        """The exact hole: the fallback sat in bin/lib and the scan only opened
        hooks/scripts, so it reported clean while every hook still synthesized."""
        hooks, lib = fake_repo
        target = lib / "common.sh"
        target.write_text(target.read_text() + "\n" + self.HISTORICAL_SYNTHESIS)

        assert _offenders(SYNTHETIC, _hook_sources((hooks,))) == {}, (
            "the hooks-only glob flagged it, so widening to bin/lib was not what fixed "
            "this test -- re-derive what the miss actually was"
        )

    def test_the_parent_form_is_caught(self, fake_repo) -> None:
        hooks, lib = fake_repo
        target = hooks / "writ-subagent-start.sh"
        target.write_text(target.read_text() + "\n" + self.HISTORICAL_PARENT_READ)

        offenders = _offenders(POINTER_AS_SOURCE, _hook_sources((hooks, lib)))
        assert "writ-subagent-start.sh" in offenders

    def test_the_old_variable_name_regex_would_have_missed_the_parent_form(
        self, fake_repo
    ) -> None:
        """`PARENT=` is neither `SESSION_ID=` nor `PARENT_SESSION=`, and that one word was
        the entire difference between caught and invisible."""
        hooks, lib = fake_repo
        target = hooks / "writ-subagent-start.sh"
        target.write_text(target.read_text() + "\n" + self.HISTORICAL_PARENT_READ)

        assert _offenders(LEGACY_POINTER_AS_SOURCE, _hook_sources((hooks, lib))) == {}, (
            "the destination-name regex flagged it, so dropping the name requirement was "
            "not what fixed this test -- re-derive what the miss actually was"
        )

    def test_comment_stripping_hides_prose_but_not_code(self) -> None:
        """The stripper earns its place only if it is this narrow."""
        assert SYNTHETIC.search(_strip_comments("# fell back to ps -o ppid= once")) is None
        assert SYNTHETIC.search(_strip_comments("X=1  # ps -o ppid= here")) is None
        assert SYNTHETIC.search(_strip_comments("SID=$(ps -o ppid= -p $PPID)")) is not None
        # Parameter expansion is not a comment.
        assert _strip_comments('printf "%s" "${t#*[.,]}"') == 'printf "%s" "${t#*[.,]}"'


def _bash(snippet: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess:
    script = f'source "{COMMON_SH}" >/dev/null 2>&1\n{snippet}\n'
    merged = {**os.environ, **(env or {})}
    # WRIT_FRICTION_LOG collapses EVERY stream into one file, by design, for the suite's
    # isolation fixture. Any test asserting that a record reached the `errors` stream must
    # clear it or it is asserting against a single-file mode that production never uses.
    # Four tests here failed exactly that way before this line existed.
    merged.pop("WRIT_FRICTION_LOG", None)
    return subprocess.run(["bash", "-c", script], capture_output=True, text=True,
                          timeout=60, env=merged)


class TestTheHelpersReturnEmptyRatherThanGuessing:
    """The behavioral half. The scans above prove the code is gone; these prove what
    replaced it, which is the part a regex cannot check."""

    @pytest.mark.parametrize("payload,expected", [
        ('{"session_id":"s-1"}', "s-1"),
        ('{"session_id":"parent","agent_id":"a-1"}', "a-1"),   # sub-agent identity wins
        ('{"agent_id":"a-only"}', "a-only"),
    ])
    def test_detect_session_id_returns_the_payload_identity(
        self, payload: str, expected: str
    ) -> None:
        proc = _bash(f"detect_session_id {shlex.quote(payload)}")
        assert proc.returncode == 0
        assert proc.stdout.strip() == expected

    @pytest.mark.parametrize("payload", ["", "{}", "garbage", '{"session_id":""}',
                                         '{"session_id":null}'])
    def test_detect_session_id_prints_nothing_when_the_payload_has_none(
        self, payload: str
    ) -> None:
        proc = _bash(f"detect_session_id {shlex.quote(payload)}")
        assert proc.stdout.strip() == "", "it printed an id it could not know"

    def test_detect_session_id_still_returns_zero_when_empty(self) -> None:
        """It must not fail the caller. Most callers legitimately do work that needs no
        session (a gate that only inspects a file path), and every hook runs under
        `set -euo pipefail`, so a non-zero return here would abort them mid-hook."""
        proc = _bash('set -euo pipefail\nSID=$(detect_session_id "")\necho "alive:[$SID]"')
        assert proc.returncode == 0
        assert "alive:[]" in proc.stdout

    @pytest.mark.parametrize("no_jq", [False, True], ids=["jq", "python"])
    def test_load_hook_env_leaves_the_id_empty_on_both_parser_arms(self, no_jq: bool) -> None:
        """The jq-less machine is exactly where a resurrected fallback would hide longest."""
        env = {"WRIT_NO_JQ": "1"} if no_jq else {}
        script = ('set -euo pipefail\n'
                  'printf %s \'{"hook_event_name":"PreToolUse"}\' | '
                  '{ load_hook_env; echo "sid:[${HOOK_SESSION_ID}]"; }')
        proc = _bash(script, env)
        assert proc.returncode == 0, "load_hook_env killed its caller over a missing id"
        assert "sid:[]" in proc.stdout, (
            f"load_hook_env produced an id the payload never carried: {proc.stdout!r}"
        )

    @pytest.mark.parametrize("no_jq", [False, True], ids=["jq", "python"])
    def test_load_hook_env_still_returns_the_id_when_there_is_one(self, no_jq: bool) -> None:
        env = {"WRIT_NO_JQ": "1"} if no_jq else {}
        script = ('set -euo pipefail\n'
                  'printf %s \'{"session_id":"s-9","hook_event_name":"PreToolUse"}\' | '
                  '{ load_hook_env; echo "sid:[${HOOK_SESSION_ID}]"; }')
        proc = _bash(script, env)
        assert "sid:[s-9]" in proc.stdout


class TestRequireSession:
    @pytest.mark.parametrize("payload,expected", [
        ('{"session_id":"s-1"}', "s-1"),
        ('{"session_id":"parent","agent_id":"a-1"}', "a-1"),   # sub-agent identity wins
        ('{"agent_id":"a-only"}', "a-only"),
    ])
    def test_it_returns_the_payload_identity(self, payload: str, expected: str) -> None:
        proc = _bash(f"writ_require_session {json.dumps(payload)} probe")
        assert proc.returncode == 0
        assert proc.stdout.strip() == expected

    @pytest.mark.parametrize("payload", ['{}', '""', "garbage", '{"session_id":""}',
                                         '{"session_id":null}'])
    def test_it_fails_rather_than_guessing(self, payload: str) -> None:
        proc = _bash(f"writ_require_session {json.dumps(payload)} probe")
        assert proc.returncode == 1, f"expected failure for {payload!r}, got {proc.stdout!r}"
        assert proc.stdout.strip() == "", "it printed an id it could not know"

    def test_failure_is_announced_on_stderr(self) -> None:
        """Claude Code surfaces hook stderr, so the operator sees this in the session
        rather than only in a file nobody greps."""
        proc = _bash("writ_require_session '{}' probe")
        assert "[WRIT CRITICAL]" in proc.stderr

    # BOTH PARSER ARMS, and this class was the one place in this file that did not check
    # them. It cost a real divergence: the jq filter was `.agent_id // .session_id`, whose
    # `//` falls through on null and false but NOT on an empty string, so
    # {"agent_id":"","session_id":"real-sess"} returned rc=1 under jq and rc=0 under
    # WRIT_NO_JQ=1. Whether three governance hooks could establish an identity at all
    # depended on whether jq was installed, which is precisely what the seam forbids.
    @pytest.mark.parametrize("no_jq", [False, True], ids=["jq", "python"])
    @pytest.mark.parametrize("payload,expected", [
        ('{"session_id":"s-1"}', "s-1"),
        ('{"session_id":"parent","agent_id":"a-1"}', "a-1"),
        ('{"agent_id":"a-only"}', "a-only"),
        # The divergence itself: an empty agent_id means "not a sub-agent", so it is a
        # session_id case rather than a refusal.
        ('{"agent_id":"","session_id":"real-sess"}', "real-sess"),
        ('{"agent_id":null,"session_id":"real-sess"}', "real-sess"),
    ])
    def test_both_arms_resolve_the_identity_identically(
        self, payload: str, expected: str, no_jq: bool
    ) -> None:
        env = {"WRIT_NO_JQ": "1"} if no_jq else {}
        proc = _bash(f"writ_require_session {json.dumps(payload)} probe", env)
        assert proc.returncode == 0, (
            f"{'python' if no_jq else 'jq'} arm refused {payload!r}: {proc.stderr}"
        )
        assert proc.stdout.strip() == expected

    @pytest.mark.parametrize("no_jq", [False, True], ids=["jq", "python"])
    @pytest.mark.parametrize("payload", ['{}', '{"session_id":""}', '{"session_id":null}',
                                         '{"agent_id":"","session_id":""}'])
    def test_both_arms_refuse_identically(self, payload: str, no_jq: bool) -> None:
        """The other direction. Matching only on the success cases would let an arm that
        never refuses anything pass, and a hook that accepts an empty identity writes
        state under a key nothing will ever read back."""
        env = {"WRIT_NO_JQ": "1"} if no_jq else {}
        proc = _bash(f"writ_require_session {json.dumps(payload)} probe", env)
        assert proc.returncode == 1, (
            f"{'python' if no_jq else 'jq'} arm accepted {payload!r} -> {proc.stdout!r}"
        )
        assert proc.stdout.strip() == ""


class TestCriticalErrorsAreRecorded:
    def test_it_lands_on_the_errors_stream_with_severity(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            _bash('writ_critical probe-hook "something broke" s-9',
                  {"WRIT_LOG_ROOT": tmp})
            rows = [json.loads(line)
                    for p in Path(tmp).rglob("errors.jsonl")
                    for line in p.read_text().splitlines() if line.strip()]
        assert rows, "no row reached the errors stream"
        row = rows[-1]
        assert row["event"] == "critical_error"
        assert row["severity"] == "critical"
        assert row["component"] == "probe-hook"
        assert row["session"] == "s-9"

    def test_a_hostile_message_cannot_break_the_json(self) -> None:
        """The message is operator-facing text that may contain anything. If it could
        corrupt the row, the one event class that must survive would be the one that does
        not parse.

        SINGLE-quoted via shlex.quote, not json.dumps. The first version used json.dumps,
        which emits a DOUBLE-quoted shell string, so bash ran the `$(cmd)` substitution
        before writ_critical ever saw it and the test measured its own harness. The
        function takes the message as "$2", an argv element that is never re-expanded.
        """
        nasty = 'quotes " and $(cmd) and \\ backslash and \n newline'
        with tempfile.TemporaryDirectory() as tmp:
            _bash(f"writ_critical probe {shlex.quote(nasty)} s-1", {"WRIT_LOG_ROOT": tmp})
            rows = [json.loads(line)
                    for p in Path(tmp).rglob("errors.jsonl")
                    for line in p.read_text().splitlines() if line.strip()]
        assert rows, "no row reached the errors stream"
        message = rows[-1]["message"]
        assert "$(cmd)" in message, "a command substitution was expanded instead of logged"
        assert '"' in message and "\\" in message

    @pytest.mark.parametrize("no_jq", [False, True], ids=["jq", "python"])
    def test_both_arms_record_it(self, no_jq: bool) -> None:
        """The jq-less machine is exactly where a silent failure would hide longest."""
        env = {"WRIT_LOG_ROOT": ""}
        with tempfile.TemporaryDirectory() as tmp:
            env["WRIT_LOG_ROOT"] = tmp
            if no_jq:
                env["WRIT_NO_JQ"] = "1"
            _bash('writ_critical arm-probe "msg" s-2', env)
            rows = [json.loads(line)
                    for p in Path(tmp).rglob("errors.jsonl")
                    for line in p.read_text().splitlines() if line.strip()]
        assert rows and rows[-1]["component"] == "arm-probe"


# Hooks that must survive a payload with no session id. Each one was read for what it does
# with the id before being listed: these either need none, or now record a critical and
# no-op. WRIT_PORT points at a closed port so every case takes the daemon-unreachable
# branch and the run stays hermetic (no live daemon, no real logs).
NO_SESSION_CASES = [
    ("friction-logger.sh", {"hook_event_name": "Stop"}),
    ("enforce-violations.sh", {"hook_event_name": "Stop"}),
    ("validate-exit-plan.sh", {"hook_event_name": "PreToolUse",
                               "tool_name": "ExitPlanMode", "tool_input": {}}),
    ("writ-precompact.sh", {"hook_event_name": "PreCompact"}),
    ("writ-session-end.sh", {"hook_event_name": "SessionEnd"}),
    ("writ-read-rag.sh", {"hook_event_name": "PreToolUse", "tool_name": "Read",
                          "tool_input": {"file_path": "/etc/hostname"}}),
    ("writ-posttool-rag.sh", {"hook_event_name": "PostToolUse", "tool_name": "Write",
                              "tool_input": {"file_path": "/tmp/writ-probe.py"}}),
    ("writ-pre-write-dispatch.sh", {"hook_event_name": "PreToolUse", "tool_name": "Write",
                                    "tool_input": {"file_path": "/tmp/writ-probe.py",
                                                   "content": "x = 1\n"}}),
]


def _run_hook(hook: str, payload: dict, tmp: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["bash", str(HOOKS / hook)], input=json.dumps(payload), text=True,
        capture_output=True, timeout=120,
        # cwd is the tmp dir so any project-root walk lands there and not in this repo.
        cwd=tmp,
        env={**os.environ, "WRIT_LOG_ROOT": tmp, "WRIT_CACHE_DIR": tmp,
             "WRIT_NO_AUTOSTART": "1", "WRIT_PORT": "1"})


class TestHooksDoNotBlockWhenIdentityIsUnknown:
    """Recording a critical error must not become a new way to break the session.

    These hooks run on the user's turn. A missing session id is a Writ problem, and Writ
    failing loudly is right, but it must not take the turn down with it.
    """

    @pytest.mark.parametrize("hook,payload", NO_SESSION_CASES,
                             ids=[c[0] for c in NO_SESSION_CASES])
    def test_it_exits_zero_without_a_session(self, hook: str, payload: dict) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            proc = _run_hook(hook, payload, tmp)
        assert proc.returncode == 0, (
            f"{hook} exited {proc.returncode} with no session id; a missing id must be "
            f"recorded, not turned into a blocked turn. stderr={proc.stderr[-400:]!r}"
        )


class TestNothingIsFiledUnderTheEmptyString:
    """`_cache_path("")` is `writ-session-.json` -- there is no empty-id guard in
    writ/session/cache.py, so any hook that reaches a MUTATING session call with an empty
    id creates a real cache file keyed on nothing, and files that turn's rules, coverage
    or gate state in it. Nothing can ever read it back.

    This is the assertion the removal needed and did not have: deleting the synthesis
    turned "written to a session that does not exist" into "written to the empty string",
    which is the same lost write with a shorter name.
    """

    @pytest.mark.parametrize("hook,payload", NO_SESSION_CASES,
                             ids=[c[0] for c in NO_SESSION_CASES])
    def test_no_hook_creates_a_cache_for_the_empty_session(
        self, hook: str, payload: dict
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            _run_hook(hook, payload, tmp)
            stray = sorted(p.name for p in Path(tmp).rglob("writ-session-.json*"))
        assert stray == [], (
            f"{hook} wrote session state under the empty string: {stray}. Guard the "
            f"session-keyed calls behind `[ -n \"$SESSION_ID\" ]` and record a critical."
        )
