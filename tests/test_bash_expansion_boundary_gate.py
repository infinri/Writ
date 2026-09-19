"""The Bash write gate performs the shell expansions its project-boundary
classification depends on (plan.md / capabilities.md, session
dfacff61-23d5-474e-846c-2e2f0f0ea482).

THE ORACLE, reproduced here rather than restated as a table: for a spelling S, a
REAL `bash -c 'printf "%s\\n" S'` process is asked what S resolves to, under the
SAME environment and cwd the extractor is given. A spelling is a MATCH when the
extractor's classified abspath equals that oracle answer (after the same
cwd-join/normpath the classification loop itself applies); a DIVERGE when the
extractor's abspath equals the pre-fix LITERAL join and DIFFERS from the oracle
(a disclosed, deliberate residue); an UNKNOWN when the extractor emits an
`unknown` row and no `local`/`outside` row at all, because a parameter it cannot
resolve makes the apparent path stop being evidence.

Every entry's verdict is judged by running the real shell at test time. Nothing
here hardcodes "this spelling denies, that one allows" -- adding a spelling to the
population requires no new expected value, only a new Case naming the verdict
class the property itself must then prove.

RED-BY-CONSTRUCTION, not by skeleton. `expand_word` does not exist in
hooks/scripts/writ-bash-write-gate.sh yet, so the MATCH-class assertions below
fail today for the real reason: the extractor's classified abspath is the
pre-fix literal cwd-join, which disagrees with what the oracle says a real shell
does. The few tests that depend on a marker that does not exist AT ALL yet (the
`UNEXPANDED FORMS` header block, the `expand_word` function itself) call
`pytest.fail` with a `skeleton:` message instead, matching this suite's existing
convention (tests/test_project_boundary.py, tests/firedrill/test_refusal_inventory.py)
of failing loudly rather than skipping.

SCOPE NOTE on the DIVERGE population (say-so-loudly, per the dispatch brief): this
file's DIVERGE class covers exactly the three directory-stack tilde forms plan.md's
Analysis section 1 names and gives concrete oracle-testable examples for (`~+`,
`~-`, a numbered `~N`). plan.md's "Deliberately NOT performed" list also names
command substitution, process substitution, arithmetic expansion, brace expansion,
pathname expansion, the parameter-expansion operator forms, and positional/special
parameters -- none of which capabilities.md's 17 items name an oracle-testable
spelling for, and building real, non-flaky bash oracles for all of them (glob
expansion in particular needs real matching files on disk) is out of this file's
scope. The header-ratchet below is therefore a consistency check between THIS
population and the header block, not an exhaustive enumeration of every
unimplemented bash mechanism; if the implementer's header block also discloses the
other mechanisms, that is additional, uncontradicted documentation this file does
not pin either way.

ENVIRONMENT NOTE, since HOME's un/set-ness is part of what is under test: every
case builds its OWN env dict rather than touching the real process environment,
copying `os.environ` first (so PATH etc. still resolve `bash`/`python3`) and then
pinning HOME, OLDPWD and a second, HOME-independent variable to real, EXISTING
directories under `tmp_path` (a real bash on this host silently ignores an
inherited OLDPWD that does not point at a directory it can chdir into for `cd -`
bookkeeping, so a synthetic marker string is not good enough here) and popping the
sentinel unset name so its absence is a property of the harness, never a
coincidence of whatever the developer's own shell happens to export. The SAME env
dict is handed to both the extractor subprocess and the oracle shell for every
case: comparing a shell running under a DIFFERENT environment than the extractor
saw would prove nothing.
"""
from __future__ import annotations

import getpass
import os
import pwd
import re
import shlex
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

import pytest

# autouse: pins cwd to a sandbox, this suite's standing convention (tests/test_bash_write_gate.py).
from tests.fixtures.session_state import sandbox_cwd  # noqa: F401
from tests._inventory import EXPAND_MARK_BEGIN, EXPAND_MARK_END, expand_word_copy_sites
from tests.test_bash_write_gate import (
    HOOK_SH,
    SKILL_ROOT,
    _run_hook,
    _seed,
    run_extractor,
)
from tests.test_project_boundary import _post_approval_cache
from tests.test_worktree_safety_extractor import _env as _worktree_env
from tests.test_worktree_safety_extractor import _run as _run_worktree_hook
from tests.test_worktree_safety_extractor import cache_root, project  # noqa: F401

SENTINEL_UNSET_VAR = "NOPE_UNSET"
SECOND_VAR_NAME = "WRIT_EXPANSION_TEST_VAR2"
LOGIN = getpass.getuser()
WT_HOOK = os.path.join(SKILL_ROOT, "hooks", "scripts", "writ-worktree-safety.sh")


def _gates():
    from writ.session import gates
    return gates


# --------------------------------------------------------------------------- #
# Fixture: one real, isolated environment per test, built fresh from tmp_path.
# --------------------------------------------------------------------------- #
@dataclass
class Fixture:
    tmp_path: Path
    project: Path
    home: Path
    oldpwd: Path
    var2_dir: Path
    stackdir: Path
    env: dict            # HOME present (the fixture home dir)
    env_no_home: dict     # HOME genuinely absent
    env_empty_home: dict  # HOME present but EMPTY, which bash treats as its value


def _build_env(*, home: str | None, oldpwd: str, var2: str) -> dict:
    env = dict(os.environ)
    if home is None:
        env.pop("HOME", None)
    else:
        env["HOME"] = home
    env.pop(SENTINEL_UNSET_VAR, None)
    env["OLDPWD"] = oldpwd
    env[SECOND_VAR_NAME] = var2
    return env


@pytest.fixture
def boundary_fixture(tmp_path: Path) -> Fixture:
    project_dir = tmp_path / "proj"
    home_dir = tmp_path / "home"
    oldpwd_dir = tmp_path / "oldpwd"
    var2_dir = tmp_path / "var2target"
    stackdir = tmp_path / "stackdir"
    for d in (project_dir, home_dir, oldpwd_dir, var2_dir, stackdir):
        d.mkdir()
    env = _build_env(home=str(home_dir), oldpwd=str(oldpwd_dir), var2=str(var2_dir))
    env_no_home = _build_env(home=None, oldpwd=str(oldpwd_dir), var2=str(var2_dir))
    # HOME="" is NOT the same as HOME unset, and the difference is a real divergence:
    # `env -i HOME= bash -c 'printf %s ~/x'` prints `/x`, while with HOME genuinely unset
    # bash consults the password database. Measured on this machine.
    env_empty_home = _build_env(home="", oldpwd=str(oldpwd_dir), var2=str(var2_dir))
    return Fixture(tmp_path, project_dir, home_dir, oldpwd_dir, var2_dir, stackdir,
                    env, env_no_home, env_empty_home)


# --------------------------------------------------------------------------- #
# The two real-process runners: the extractor (production code under test) and
# the oracle (a real bash, asked the identical question).
# --------------------------------------------------------------------------- #
def _extract_rows(cmd: str, cwd: Path, env: dict) -> list[tuple[str, ...]]:
    """Every row the extractor prints for CMD, as FULL tab-split tuples -- not
    the 2-column shape tests/test_bash_write_gate.py's `_extract()` assumes,
    because the `unknown` row this cycle adds may carry more than one field
    after the kind column."""
    p, lines = run_extractor(cmd, str(cwd), env=env, timeout=15)
    assert p.returncode == 0, (cmd, p.returncode, p.stdout, p.stderr)
    rows: list[tuple[str, ...]] = []
    for line in lines:
        if "\t" in line:
            rows.append(tuple(line.split("\t")))
    return rows


def _classified_abspath(rows: list[tuple[str, ...]]) -> str:
    hits = [r for r in rows if r and r[0] in ("local", "outside")]
    assert len(hits) == 1, f"expected exactly one local/outside row, got {rows!r}"
    return hits[0][1]


def _oracle_word(spelling: str, cwd: Path, env: dict, *, prelude: str = "") -> str:
    """Ask a real bash process what SPELLING resolves to in ARGUMENT position.

    Contract: exactly one line of output. `prelude` may run setup statements that
    build shell state the spelling itself depends on (directory-stack `pushd`s for
    the `~N` residue form); it must never itself print anything (redirected to
    /dev/null by its own callers).
    """
    script = "%sprintf '%%s\\n' %s" % (prelude, spelling)
    p = subprocess.run(["bash", "-c", script], cwd=str(cwd), env=env,
                       capture_output=True, text=True, timeout=10)
    assert p.returncode == 0, (spelling, p.returncode, p.stdout, p.stderr)
    lines = p.stdout.splitlines()
    assert len(lines) == 1, (
        f"oracle contract violated for {spelling!r}: expected exactly one line, "
        f"got {lines!r} (stderr={p.stderr!r})"
    )
    return lines[0]


def _expected_abspath(cwd: Path, oracle_word: str) -> str:
    """The SAME join/normpath the classification loop itself applies, the line reading
    `ap = t if os.path.isabs(t) else os.path.normpath(os.path.join(cwd, t))` in the hook
    (quoted rather than cited by number: this cycle's own insertions moved it, and a
    line citation here has gone stale twice already): absolute words pass through,
    relative words join under cwd.
    This is not the thing under test -- the EXPANSION is -- so it is computed
    identically for every entry rather than asserted per-case."""
    return oracle_word if os.path.isabs(oracle_word) else os.path.normpath(
        os.path.join(str(cwd), oracle_word))


def _literal_join(cwd: Path, word: str) -> str:
    """The pre-fix behaviour: no expansion at all, just today's cwd-join. Used
    only by the DIVERGE assertions to prove the gate's abspath is EXACTLY that
    (not merely "not the oracle's"), so a divergence that silently changes shape
    also reddens."""
    return word if os.path.isabs(word) else os.path.normpath(os.path.join(str(cwd), word))


def _unexpanded_forms_from_header(source: str) -> set[str]:
    """Names-only block the hook header must carry, parsed the same way
    tests/test_bash_egress_gate.py's `uncovered_prefix_names` parses its own
    sibling `UNCOVERED PREFIXES` block."""
    try:
        start = source.index("# UNEXPANDED FORMS BEGIN")
    except ValueError:
        pytest.fail(
            "skeleton: hooks/scripts/writ-bash-write-gate.sh has no "
            "'# UNEXPANDED FORMS BEGIN' block yet (plan.md ## Files)"
        )
    start = source.index("\n", start) + 1
    end = source.index("# UNEXPANDED FORMS END", start)
    body = " ".join(line.lstrip("#").strip() for line in source[start:end].splitlines())
    return {n.strip() for n in body.split(",") if n.strip()}


# --------------------------------------------------------------------------- #
# The population. Every entry's verdict is PROVED, never assumed, by the tests
# below; this table only names a spelling, its verdict CLASS, and (for DIVERGE)
# the form name the header block must also carry.
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class Case:
    id: str
    spelling_template: str          # may contain "{p}", substituted with a unique probe basename
    verdict: str                    # "MATCH" | "DIVERGE" | "UNKNOWN"
    home: str = "fixture"           # "fixture" | "none" | "empty"
    form: str | None = None         # required when verdict == "DIVERGE"
    prelude_builder: Callable[[Fixture], str] = field(default=lambda fx: "")


POPULATION: list[Case] = [
    # -- the six measured bypasses (resolve OUTSIDE today, must classify `outside`
    #    and deny via the project boundary once expanded) --
    Case("bare_tilde_slash", "~/{p}", "MATCH"),
    Case("bare_tilde", "~", "MATCH"),
    Case("tilde_login_dotted", "~%s/{p}" % LOGIN, "MATCH"),
    Case("dollar_home", "$HOME/{p}", "MATCH"),
    Case("dollar_brace_home", "${HOME}/{p}", "MATCH"),
    Case("double_quoted_dollar_home", '"$HOME/{p}"', "MATCH"),

    # -- the four legitimate in-project spellings that must STAY allowed --
    Case("double_quoted_tilde_stays_inside", '"~/{p}"', "MATCH"),
    Case("single_quoted_tilde_stays_inside", "'~/{p}'", "MATCH"),
    Case("single_quoted_dollar_home_stays_inside", "'$HOME/{p}'", "MATCH"),
    Case("mid_word_tilde_stays_inside", "src/~/{p}", "MATCH"),

    # -- additional MATCH controls named in plan.md's Test design section --
    Case("plain_relative_control", "src/{p}", "MATCH"),
    Case("dotted_relative_control", "./src/{p}", "MATCH"),
    Case("dotdot_traversal_control", "../../../etc/{p}", "MATCH"),
    Case("escaped_tilde", "\\~/{p}", "MATCH"),
    Case("escaped_dollar_home", "\\$HOME/{p}", "MATCH"),
    # MEASURED FINDING, reported loudly rather than adjusted to fit (dispatch brief's
    # own instruction): `shlex.split(..., posix=False)` -- unchanged by this plan, and
    # the ONLY tokenizer in this file -- splits a QUOTE-THEN-UNQUOTED word into TWO
    # tokens at the closing quote (verified directly: `shlex.split('cp x "$HOME"/y',
    # posix=False)` yields `['cp', 'x', '"$HOME"', '/y']`, four tokens, not three), while
    # an UNQUOTED-THEN-QUOTE word (`partial_quote_suffix` below, `$HOME"/{p}"`) and a
    # TILDE-THEN-QUOTE word (`tilde_quoted_suffix` below, `~/"{p}"`) both stay ONE token.
    # For a `cp` destination this means `cand[-1]` (the last non-flag positional) picks
    # only the trailing `/{p}` fragment and silently drops the quoted `"$HOME"` fragment
    # as an earlier, ignored positional -- a token-BOUNDARY defect, not an expansion one.
    # `expand_word` runs PER TOKEN (plan.md's own words: "a single left-to-right scan"
    # over one collected token), so it cannot reunite two tokens shlex already split
    # apart. This entry is kept as a MATCH assertion (the desired end state really is
    # "resolves like $HOME/{p}") rather than removed or weakened: if it is STILL red
    # after expand_word ships exactly as plan.md describes, that redness is real signal
    # that this token-adjacency gap needs its own fix, not a sign the test is wrong.
    Case("partial_quote_prefix", '"$HOME"/{p}', "MATCH"),
    Case("partial_quote_suffix", '$HOME"/{p}"', "MATCH"),
    Case("tilde_quoted_suffix", '~/"{p}"', "MATCH"),
    Case("second_env_var_not_home", "$%s/{p}" % SECOND_VAR_NAME, "MATCH"),
    Case("tilde_root", "~root", "MATCH"),
    Case("tilde_unknown_login", "~nosuchuser123456xyz/{p}", "MATCH"),
    Case("single_quoted_nope_unset_stays_inside", "'$%s/{p}'" % SENTINEL_UNSET_VAR, "MATCH"),
    Case("home_removed_tilde_fallback", "~/{p}", "MATCH", home="none"),
    # HOME PRESENT BUT EMPTY is a THIRD state, not a spelling of "unset". Review caught
    # the gate conflating them with a falsiness test, so an empty HOME fell back to the
    # password database while bash uses the empty value and resolves `~/x` to `/x`. That
    # matters wherever the project root IS the invoking user's real home, because the
    # passwd answer then reads as in-project for a write the shell sends to the
    # filesystem root. Reddens when the tilde branch tests `not val` instead of
    # `val is None`.
    Case("empty_home_uses_the_empty_value", "~/{p}", "MATCH", home="empty"),

    # -- the three directory-stack tilde forms: disclosed residue, DIVERGE --
    Case("tilde_pwd", "~+/{p}", "DIVERGE", form="tilde-pwd"),
    Case("tilde_oldpwd", "~-/{p}", "DIVERGE", form="tilde-oldpwd"),
    Case("tilde_dirstack", "~1/{p}", "DIVERGE", form="tilde-dirstack",
         prelude_builder=lambda fx: "pushd %s >/dev/null; " % shlex.quote(str(fx.stackdir))),

    # -- the unresolved-variable ask --
    Case("nope_unset_unknown_ask", "$%s/{p}" % SENTINEL_UNSET_VAR, "UNKNOWN"),
]

MATCH_CASES = [c for c in POPULATION if c.verdict == "MATCH"]
DIVERGE_CASES = [c for c in POPULATION if c.verdict == "DIVERGE"]
UNKNOWN_CASES = [c for c in POPULATION if c.verdict == "UNKNOWN"]


def _case_env(fx: Fixture, case: Case) -> dict:
    if case.home == "fixture":
        return fx.env
    return fx.env_empty_home if case.home == "empty" else fx.env_no_home


def _case_dest(case: Case) -> str:
    """Substitute the probe basename WITHOUT str.format.

    `.format` reads `{HOME}` in the `${HOME}/{p}` template as a replacement field and
    raises KeyError before the extractor is ever called, so that case could not run on
    any implementation, passing or failing. A brace form is one of the seven measured
    bypasses, so losing it would have left the spelling untested while the file looked
    like it covered it. `.replace` substitutes the one placeholder and leaves every other
    brace alone.
    """
    probe = "probe_%s.txt" % case.id
    return case.spelling_template.replace("{p}", probe)


# --------------------------------------------------------------------------- #
# 0. non-vacuity: the population and every verdict class are non-empty.
# --------------------------------------------------------------------------- #
class TestPopulationIsNonVacuous:
    def test_population_is_non_empty(self):
        """Reddens if POPULATION is ever emptied, which would make every test
        below pass on any tree."""
        assert POPULATION

    @pytest.mark.parametrize("cls", ["MATCH", "DIVERGE", "UNKNOWN"])
    def test_every_verdict_class_has_at_least_one_entry(self, cls):
        assert any(c.verdict == cls for c in POPULATION), cls

    def test_every_diverge_entry_carries_a_form_name(self):
        """Reddens if a divergence entry is added with no form name (capability 14's
        other half)."""
        assert all(c.form for c in DIVERGE_CASES), DIVERGE_CASES


# --------------------------------------------------------------------------- #
# 1. MATCH class: the gate's classified abspath must equal the real oracle's.
# --------------------------------------------------------------------------- #
class TestMatchClassAgreesWithTheRealShell:
    """Reddens for the six bypass spellings if expand_word's matching branch is
    deleted or narrowed: a leading-tilde deletion, a login-name charset narrowed
    to [A-Za-z0-9_] (drops the dotted name), a brace-form drop, a name-HOME
    special case instead of reading the environment, or a HOME-env-only tilde
    with no passwd-database fallback. Reddens for the four legitimate spellings
    (and the other MATCH controls) if expansion is applied to dequote's OUTPUT
    instead of the raw token, which would wrongly resolve all of them and turn
    the fix into false refusals."""

    @pytest.mark.parametrize(
        "case", MATCH_CASES, ids=[c.id for c in MATCH_CASES],
    )
    def test_gate_abspath_equals_the_oracles(self, boundary_fixture, case):
        fx = boundary_fixture
        if case.id == "tilde_login_dotted" and "." not in LOGIN:
            pytest.skip("current OS login has no dot; this measured spelling needs one")
        env = _case_env(fx, case)
        dest = _case_dest(case)
        rows = _extract_rows("cp README.md %s" % dest, fx.project, env)
        got = _classified_abspath(rows)
        oracle = _oracle_word(dest, fx.project, env)
        expected = _expected_abspath(fx.project, oracle)
        assert got == expected, (case.id, dest, got, expected, rows)


# --------------------------------------------------------------------------- #
# 2. DIVERGE class: the gate stays literal AND that literal genuinely differs
#    from what a real shell does. Two-sided on purpose.
# --------------------------------------------------------------------------- #
class TestDivergentFormsStayLiteralButTrulyDiverge:
    @pytest.mark.parametrize("case", DIVERGE_CASES, ids=[c.id for c in DIVERGE_CASES])
    def test_diverges_in_both_directions(self, boundary_fixture, case):
        """Reddens if a divergence silently closes (the gate starts expanding and
        now matches the oracle) or silently changes shape (the gate's join no
        longer equals today's un-expanded literal join) -- either way, someone
        must re-label this entry deliberately rather than have it pass quietly."""
        fx = boundary_fixture
        dest = _case_dest(case)
        rows = _extract_rows("cp README.md %s" % dest, fx.project, fx.env)
        got = _classified_abspath(rows)
        literal = _literal_join(fx.project, dest)
        assert got == literal, (
            f"{case.id}: the gate's abspath is no longer today's un-expanded "
            f"literal join ({literal!r}); it changed shape and must be "
            f"re-labelled, not silently accepted: got {got!r}"
        )
        prelude = case.prelude_builder(fx)
        oracle = _oracle_word(dest, fx.project, fx.env, prelude=prelude)
        oracle_abspath = _expected_abspath(fx.project, oracle)
        assert got != oracle_abspath, (
            f"{case.id} no longer diverges from the oracle ({oracle_abspath!r}); "
            "if expand_word now legitimately handles this form, re-label the "
            "entry deliberately rather than leaving a false divergence pin"
        )


# --------------------------------------------------------------------------- #
# 3. UNKNOWN class: an unresolved parameter emits `unknown` and no local/outside
#    row at all -- the raw extractor half of capability 8.
# --------------------------------------------------------------------------- #
class TestUnknownClassEmitsNoLocalOrOutsideRow:
    @pytest.mark.parametrize("case", UNKNOWN_CASES, ids=[c.id for c in UNKNOWN_CASES])
    def test_unknown_row_with_no_local_or_outside_row(self, boundary_fixture, case):
        """Reddens if the unknown branch falls back to appending the literal
        value, which would emit `local` (or `outside`) instead of `unknown`."""
        fx = boundary_fixture
        dest = _case_dest(case)
        rows = _extract_rows("cp README.md %s" % dest, fx.project, fx.env)
        kinds = {r[0] for r in rows if r}
        assert "unknown" in kinds, rows
        assert "local" not in kinds and "outside" not in kinds, rows


# --------------------------------------------------------------------------- #
# capability 1: the six-bypass abspath is refused by the real project-boundary
# predicate, in-process, no daemon, no flake.
# --------------------------------------------------------------------------- #
class TestBypassAbspathIsRefusedByTheProjectBoundary:
    def test_tilde_slash_bypass_is_denied_with_enf_project_boundary(self, boundary_fixture):
        """Reddens if the leading-tilde branch of expand_word is deleted: the row
        returns to `<cwd>/~/probe.txt` and the kind returns to `local`, so this
        abspath is never even OFFERED to `_can_write_check` as an outside path."""
        fx = boundary_fixture
        probe = "probe_boundary_deny.txt"
        rows = _extract_rows("cp README.md ~/%s" % probe, fx.project, fx.env)
        outside_hits = [r for r in rows if r and r[0] == "outside"]
        assert outside_hits, (
            f"expected an `outside` row for a real home-directory bypass; got {rows!r}"
        )
        abspath = outside_hits[0][1]
        assert abspath == str(fx.home / probe), (abspath, rows)
        cache = _post_approval_cache(str(fx.project))
        result = _gates()._can_write_check(
            "expand-boundary-deny", {"tool_input": {"file_path": abspath}}, SKILL_ROOT, cache)
        assert result["can_write"] is False, result
        assert "ENF-PROJECT-BOUNDARY" in (result["reason"] or ""), result


# --------------------------------------------------------------------------- #
# capability 6 (direct half): the four legitimate spellings are not just
# classified `local`, they are actually ALLOWED by `_can_write_check` too.
# --------------------------------------------------------------------------- #
class TestLegitimateInProjectSpellingsStayAllowed:
    LEGIT_TEMPLATES = ['"~/{p}"', "'~/{p}'", "'$HOME/{p}'", "src/~/{p}"]

    @pytest.mark.parametrize("tmpl", LEGIT_TEMPLATES, ids=lambda t: t)
    def test_stays_local_and_can_write_allows_it(self, boundary_fixture, tmpl):
        """Reddens if expansion is applied to the post-dequote value, which
        resolves all four of these and converts the fix into four false
        refusals."""
        fx = boundary_fixture
        dest = tmpl.format(p="probe_legit.txt")
        rows = _extract_rows("cp README.md %s" % dest, fx.project, fx.env)
        local_hits = [r for r in rows if r and r[0] == "local"]
        assert local_hits, (dest, rows)
        abspath = local_hits[0][1]
        cache = _post_approval_cache(str(fx.project))
        result = _gates()._can_write_check(
            "expand-boundary-allow", {"tool_input": {"file_path": abspath}}, SKILL_ROOT, cache)
        assert result["can_write"] is True, result


# --------------------------------------------------------------------------- #
# capability 4: the three $HOME spellings resolve to the SAME abspath as each
# other, not merely each matching the oracle independently.
# --------------------------------------------------------------------------- #
class TestTheThreeHomeSpellingsAgreeWithEachOther:
    def test_dollar_home_brace_and_double_quoted_forms_are_identical(self, boundary_fixture):
        """Reddens if the brace form is dropped from the parameter branch, or if
        a double-quoted span is treated like a single-quoted one (any one of the
        three diverging from the other two is the failure)."""
        fx = boundary_fixture
        probe = "probe_home_forms.txt"
        dests = ["$HOME/%s" % probe, "${HOME}/%s" % probe, '"$HOME/%s"' % probe]
        abspaths = set()
        for dest in dests:
            rows = _extract_rows("cp README.md %s" % dest, fx.project, fx.env)
            abspaths.add(_classified_abspath(rows))
        assert abspaths == {str(fx.home / probe)}, abspaths


# --------------------------------------------------------------------------- #
# capability 9: credential classification precedes the unknown-ask arm.
# --------------------------------------------------------------------------- #
class TestCredentialClassificationPrecedesTheUnknownAsk:
    """Reddens if the unknown check is ordered before is_cred in the
    classification loop."""

    def test_unresolved_variable_credential_target_still_denies(self, tmp_path):
        sid = "expand-cred-order-unset"
        _seed(sid, mode="conversation")
        out = _run_hook("cp seed.txt $%s/.env" % SENTINEL_UNSET_VAR, sid, str(tmp_path))
        assert out is not None, "the credential-shaped unresolved target was silent"
        assert out.get("permissionDecision") == "deny", out
        assert "SEC-CREDENTIAL-WRITE" in out.get("permissionDecisionReason", ""), out

    def test_expanded_tilde_credential_target_still_denies(self, tmp_path):
        sid = "expand-cred-order-tilde"
        _seed(sid, mode="conversation")
        out = _run_hook("cp seed.txt ~/.env", sid, str(tmp_path))
        assert out is not None, "the expanded-tilde credential target was silent"
        assert out.get("permissionDecision") == "deny", out
        assert "SEC-CREDENTIAL-WRITE" in out.get("permissionDecisionReason", ""), out


# --------------------------------------------------------------------------- #
# capability 8, full-hook half: the ask itself, naming the unresolved variable.
# --------------------------------------------------------------------------- #
class TestUnresolvedVariableDrivesAnAskThroughTheRealHook:
    def test_full_hook_asks_naming_the_unresolved_variable(self, tmp_path):
        """Reddens if the unknown branch never reaches emit_ask, or if the
        reason it builds does not name the unresolved variable."""
        sid = "expand-unknown-ask-sid"
        _seed(sid, mode="conversation")
        out = _run_hook("cp README.md $%s/probe.txt" % SENTINEL_UNSET_VAR, sid, str(tmp_path))
        assert out is not None, "the unresolved-variable target was silent"
        assert out.get("permissionDecision") == "ask", out
        assert SENTINEL_UNSET_VAR in out.get("permissionDecisionReason", ""), out


class TestUnknownAskIsDeclaredInTheFiredrillCensus:
    def test_declared_ask_arm_is_a_real_working_refusal(self, tmp_path):
        """Ties tests/firedrill/_census.py's new ask-arm declaration to real
        coverage: reddens if that census entry stops matching a real subprocess
        ask, which is exactly what would make the declaration decorative rather
        than load-bearing."""
        from tests.firedrill._census import by_id
        from tests.firedrill._harness import make_isolation, run_hook

        entry = by_id("bash-write-unresolved-variable-ask")
        iso = make_isolation(tmp_path, session_id="expand-census-ask")
        setup = entry.setup(iso)
        result = run_hook(entry.script, setup["envelope"], iso,
                          extra_env=setup.get("extra_env"))
        assert result.permission_decision() == "ask", result.stdout
        audit = result.audit()
        assert audit, "audit stream is empty; the hook recorded nothing"
        rows = [r for r in audit
                if r.get("event") == "gate_decision" and r.get("gate") == entry.gate_name]
        assert rows, audit
        assert rows[-1].get("decision") == "ask", rows


# --------------------------------------------------------------------------- #
# capability 10: expansion reaches every write vector's target, not only `cp`.
# --------------------------------------------------------------------------- #
VECTORS: dict[str, str] = {
    "spaced_redirect": "echo x > %s",
    "glued_redirect": "echo x >%s",
    "tee": "tee %s",
    "dd_of": "dd if=/dev/zero of=%s",
    "sed_i": "sed -i s/a/b/ %s",
    "cp_dash_t": "cp -t %s README.md",
}
DISCRIMINATING_SPELLINGS = ["~/{p}", '"~/{p}"', "$HOME/{p}", "'$HOME/{p}'",
                           "$%s/{p}" % SENTINEL_UNSET_VAR]


class TestExpansionCrossesEveryWriteVector:
    """Reddens if expand_word is wired at one call site and the others keep
    calling dequote: each vector's classification must agree with the
    already-oracle-verified `cp` destination for the SAME spelling."""

    @pytest.mark.parametrize(
        "vector,tmpl",
        [(v, t) for v in VECTORS for t in DISCRIMINATING_SPELLINGS],
        ids=["%s-%s" % (v, t) for v in VECTORS for t in DISCRIMINATING_SPELLINGS],
    )
    def test_vector_agrees_with_the_cp_destination_reference(self, boundary_fixture, vector, tmpl):
        fx = boundary_fixture
        probe = "probe_vec_%s.txt" % vector
        dest = tmpl.format(p=probe)
        reference_rows = _extract_rows("cp README.md %s" % dest, fx.project, fx.env)
        vector_rows = _extract_rows(VECTORS[vector] % dest, fx.project, fx.env)
        if tmpl == "$%s/{p}" % SENTINEL_UNSET_VAR:
            ref_kinds = {r[0] for r in reference_rows if r}
            vec_kinds = {r[0] for r in vector_rows if r}
            assert "unknown" in ref_kinds, reference_rows
            assert "unknown" in vec_kinds, (vector, vector_rows)
            assert "local" not in vec_kinds and "outside" not in vec_kinds, (vector, vector_rows)
        else:
            reference = _classified_abspath(reference_rows)
            got = _classified_abspath(vector_rows)
            assert got == reference, (vector, dest, got, reference)

    def test_group_wrapped_cp_to_bare_tilde_is_expanded(self, boundary_fixture):
        """The group-wrapped `(cp README.md ~)` form: reddens if
        strip_unbalanced_close is not called as expand_word's own first step,
        since the token expand_word would otherwise see is `~)`, whose
        tilde-prefix runs to `)` rather than to a login name, staying literal
        while the shell itself still copies to $HOME."""
        fx = boundary_fixture
        rows = _extract_rows("(cp README.md ~)", fx.project, fx.env)
        abspath = _classified_abspath(rows)
        assert abspath == str(fx.home), rows


class TestAssignmentPositionSettledByOracle:
    """The two spellings plan.md deliberately leaves to the oracle rather than
    to anyone's reading of the bash manual, because the tilde sits after an `=`
    inside a word. Neither test hardcodes EXPANDS vs literal -- each computes
    the real bash answer at run time and asserts the extractor agrees with it,
    so a wrong guess here is impossible by construction; only a wrong extractor
    is."""

    def test_dd_of_assignment_position_tilde(self, boundary_fixture):
        fx = boundary_fixture
        probe = "probe_dd_of_assign.txt"
        whole_word = "of=~/%s" % probe
        oracle_whole = _oracle_word(whole_word, fx.project, fx.env)
        assert oracle_whole.startswith("of="), oracle_whole
        expected = _expected_abspath(fx.project, oracle_whole[len("of="):])
        rows = _extract_rows("dd if=/dev/zero of=~/%s" % probe, fx.project, fx.env)
        got = _classified_abspath(rows)
        assert got == expected, (oracle_whole, got, rows)

    def test_cp_target_directory_assignment_position_tilde(self, boundary_fixture):
        fx = boundary_fixture
        probe = "probe_cp_td_assign.txt"
        whole_word = "--target-directory=~/%s" % probe
        oracle_whole = _oracle_word(whole_word, fx.project, fx.env)
        assert oracle_whole.startswith("--target-directory="), oracle_whole
        expected = _expected_abspath(fx.project, oracle_whole[len("--target-directory="):])
        rows = _extract_rows("cp --target-directory=~/%s README.md" % probe, fx.project, fx.env)
        got = _classified_abspath(rows)
        assert got == expected, (oracle_whole, got, rows)


# --------------------------------------------------------------------------- #
# capability 11: interpreter-scanned paths are NOT expanded.
# --------------------------------------------------------------------------- #
class TestInterpreterScannedPathsAreNotExpanded:
    def test_interpreter_tilde_path_is_not_expanded(self, boundary_fixture):
        """Reddens if expand_word is applied to scan_tokens hits: the row would
        then carry the fixture home directory instead of today's un-expanded
        cwd-join."""
        fx = boundary_fixture
        cmd = "python3 -c \"open('~/x','w')\""
        rows = _extract_rows(cmd, fx.project, fx.env)
        local_hits = [r for r in rows if r and r[0] == "local"]
        assert local_hits, rows
        assert local_hits[0][1] == str(fx.project / "~" / "x"), rows


# --------------------------------------------------------------------------- #
# capability 12: HOME removed from both sides -- the passwd-database fallback,
# and $HOME itself becoming an unknown ask.
# --------------------------------------------------------------------------- #
class TestHomeRemovedFallbackAndUnknownAsk:
    def test_bare_tilde_still_resolves_via_the_password_database(self, boundary_fixture):
        """Reddens if the tilde branch reads only os.environ['HOME'] and has no
        fallback: with HOME genuinely absent, both the gate and a real bash fall
        back to the invoking user's own passwd-database home directory."""
        fx = boundary_fixture
        probe = "probe_home_removed.txt"
        rows = _extract_rows("cp README.md ~/%s" % probe, fx.project, fx.env_no_home)
        got = _classified_abspath(rows)
        oracle = _oracle_word("~/%s" % probe, fx.project, fx.env_no_home)
        expected = _expected_abspath(fx.project, oracle)
        assert got == expected, (got, expected, rows)

    def test_dollar_home_becomes_an_unknown_ask_when_home_is_removed(self, boundary_fixture):
        """The paired half: with HOME genuinely absent (a missing key, not a bad
        value), $HOME/probe.txt must ask rather than silently resolve to
        `/probe.txt` (the filesystem root) the way an ordinary unresolved
        variable does."""
        fx = boundary_fixture
        probe = "probe_home_removed_dollar.txt"
        rows = _extract_rows("cp README.md $HOME/%s" % probe, fx.project, fx.env_no_home)
        kinds = {r[0] for r in rows if r}
        assert "unknown" in kinds, rows
        assert "local" not in kinds and "outside" not in kinds, rows


# --------------------------------------------------------------------------- #
# capability 14: the header's UNEXPANDED FORMS block names exactly the forms
# the population records as divergences.
# --------------------------------------------------------------------------- #
class TestUnexpandedFormsHeaderRatchet:
    def test_header_names_exactly_the_populations_diverge_forms(self):
        """Reddens if a form gains expansion in the code without leaving the
        block, or if a divergence entry is added with no form name (the
        non-empty half is TestPopulationIsNonVacuous.
        test_every_diverge_entry_carries_a_form_name)."""
        src = Path(HOOK_SH).read_text()
        declared = _unexpanded_forms_from_header(src)
        population_forms = {c.form for c in DIVERGE_CASES}
        assert declared == population_forms, (declared, population_forms)


# --------------------------------------------------------------------------- #
# capability 15 (2412ba38 cycle): the mirror block is untouched, in EITHER
# file that now carries an inline expand_word copy.
# --------------------------------------------------------------------------- #
class TestExpandWordDoesNotLiveInTheMirrorBlock:
    """Once writ-worktree-safety.sh gains its own inline expand_word copy
    (plan.md 2412ba38-51e1-4b73-895b-7b240a3c21d3 ## Files), THAT file's own
    MIRROR BEGIN/END span -- shared with writ-bash-write-gate.sh and
    writ/session/bash_tokens.py for split_control_operators and its siblings
    -- must stay exactly as free of expand_word as the write gate's always
    was. Parametrized over both hook files rather than two copies of the
    write-gate-only test, so a fourth EXPAND site that lands inside either
    hook's MIRROR span is caught by the same assertion."""

    @pytest.mark.parametrize(
        "hook_path", [HOOK_SH, WT_HOOK],
        ids=["writ-bash-write-gate.sh", "writ-worktree-safety.sh"],
    )
    def test_expand_word_is_not_defined_inside_the_mirror_block(self, hook_path):
        """Reddens if any part of expand_word is placed between MIRROR BEGIN and
        MIRROR END, the block writ-worktree-safety.sh, writ-bash-write-gate.sh
        and writ/session/bash_tokens.py mirror byte-for-byte."""
        src = Path(hook_path).read_text()
        if "def expand_word(" not in src:
            pytest.fail(
                "skeleton: %s has no expand_word function yet (plan.md "
                "2412ba38-51e1-4b73-895b-7b240a3c21d3 ## Files)" % hook_path
            )
        mirror_start = src.index("# MIRROR BEGIN split_control_operators")
        mirror_end = src.index("# MIRROR END split_control_operators")
        def_pos = src.index("def expand_word(")
        assert not (mirror_start <= def_pos <= mirror_end), (
            "%s: expand_word must not live inside the MIRROR BEGIN/END block "
            "shared with writ-worktree-safety.sh, writ-bash-write-gate.sh and "
            "writ/session/bash_tokens.py" % hook_path
        )


########################################################################
# The worktree hook resolves a tilde the way the shell does
# (plan.md / capabilities.md, session 2412ba38-51e1-4b73-895b-7b240a3c21d3).
#
# THE PIN THIS SECTION REPLACES pinned TODAY's broken verdict
# (TestWorktreeHookBlindnessIsRecordedNotFixed: `git worktree add ~/evil x`
# denies with a gitignore remedy that cannot work, because os.path.abspath
# never expands a tilde). It is gone, not merely renamed: every class below
# asserts the CORRECT verdict, judged against a real bash oracle, and this
# whole section reddens against the pre-fix hook for the real reason -- the
# hook still denies naming a directory the shell will never create there.
#
# THE ORACLE DISCIPLINE is the one this file already established for the
# write gate's own expansion boundary: `_oracle_word` and `_expected_abspath`
# (defined above, unchanged) are reused as-is rather than re-implemented,
# because a second hand-rolled "what should this resolve to" helper is
# exactly the parity-guard mistake this repo has already shipped once (one
# side of a comparison that quietly stopped being real).
########################################################################


def _worktree_oracle_outside(cwd: Path, env: dict, spelling: str) -> bool:
    """True when a real bash, asked what SPELLING resolves to under CWD and
    ENV, lands outside CWD's own tree -- the same is-under-repo-root test the
    hook's own classification performs, computed independently through the
    oracle rather than read off the hook's verdict."""
    word = _oracle_word(spelling, cwd, env)
    abspath = _expected_abspath(cwd, word)
    repo_root = str(cwd)
    return not (abspath == repo_root or abspath.startswith(repo_root + os.sep))


def _worktree_oracle_rel(cwd: Path, env: dict, spelling: str) -> str:
    """The oracle's answer for SPELLING, expressed relative to CWD -- the same
    os.path.relpath the hook's own classification computes once a target is
    known to be inside the repo. Callers only use this once
    `_worktree_oracle_outside` has already confirmed the word resolves
    inside."""
    word = _oracle_word(spelling, cwd, env)
    abspath = _expected_abspath(cwd, word)
    return os.path.relpath(abspath, str(cwd))


@dataclass(frozen=True)
class WorktreeVerdictCase:
    id: str
    spelling: str


# The verdict-table population (plan.md ## Analysis, DECISION 2's table): every
# entry the oracle-driven property test below must classify correctly, with NO
# hardcoded "this one denies" label anywhere in this module -- only the spelling.
WORKTREE_VERDICT_POPULATION: list[WorktreeVerdictCase] = [
    # -- resolve OUTSIDE the repo under the real environment: must be SILENT --
    WorktreeVerdictCase("bare_tilde_slash", "~/evil"),
    WorktreeVerdictCase("dollar_home", "$HOME/evil"),
    WorktreeVerdictCase("dollar_brace_home", "${HOME}/evil"),
    WorktreeVerdictCase("tilde_root", "~root/evil"),
    # -- a real shell keeps these literal, so they resolve INSIDE the repo and
    #    must still DENY, each reason naming the directory bash really creates --
    WorktreeVerdictCase("double_quoted_tilde_stays_inside", '"~/evil"'),
    WorktreeVerdictCase("single_quoted_tilde_stays_inside", "'~/evil'"),
    WorktreeVerdictCase("single_quoted_dollar_home_stays_inside", "'$HOME/evil'"),
    WorktreeVerdictCase("escaped_tilde", "\\~/evil"),
    WorktreeVerdictCase("tilde_unknown_login", "~nosuchuser123456xyz/evil"),
]


# --------------------------------------------------------------------------- #
# capability: the whole population is non-vacuous, and it genuinely contains
# both an outside-the-repo case and an inside-the-repo case under the real
# environment -- otherwise the property test below could pass by every case
# quietly landing on the same side.
# --------------------------------------------------------------------------- #
class TestWorktreeVerdictPopulationIsNonVacuous:
    def test_population_is_non_empty(self):
        assert WORKTREE_VERDICT_POPULATION

    def test_both_inside_and_outside_cases_exist_for_the_real_environment(
        self, cache_root, project
    ):
        env = _worktree_env(cache_root)
        outsides = [
            c for c in WORKTREE_VERDICT_POPULATION
            if _worktree_oracle_outside(project, env, c.spelling)
        ]
        insides = [
            c for c in WORKTREE_VERDICT_POPULATION
            if not _worktree_oracle_outside(project, env, c.spelling)
        ]
        assert outsides, "no case in the population resolves outside the repo root"
        assert insides, "no case in the population resolves inside the repo root"


# --------------------------------------------------------------------------- #
# THE REPLACEMENT: TestWorktreeHookAgreesWithTheShellOnTilde (plan.md ## Files
# / "The test that must redden, and what replaces it"). Covers capabilities
# 1 ("no permission decision" + oracle confirms outside), 4 ($HOME and
# ${HOME} agree with the bare tilde), 5 (the literal-kept spellings still
# deny, naming the real created directory) and 6 (an unknown login name still
# denies as project-local).
# --------------------------------------------------------------------------- #
class TestWorktreeHookAgreesWithTheShellOnTilde:
    """Replaces TestWorktreeHookBlindnessIsRecordedNotFixed, which pinned
    TODAY's false refusal. For every spelling in WORKTREE_VERDICT_POPULATION,
    the real hook and a real bash oracle are asked the same question under
    the SAME cwd and env, and the hook must deny a project-local target ONLY
    when the oracle says the word resolves inside the repo root -- never a
    hardcoded "this spelling denies" table.

    Reddens today (pre-fix) for every OUTSIDE case: os.path.abspath never
    expands a tilde or a parameter, so the pre-fix hook classifies '~/evil'
    (and its $HOME/${HOME} siblings and '~root/evil') as project-local under
    the repo root and denies with a gitignore remedy the oracle proves cannot
    work, because the real target is the fixture's own real $HOME (or
    root's), outside the repo.
    """

    @pytest.mark.parametrize(
        "case", WORKTREE_VERDICT_POPULATION,
        ids=[c.id for c in WORKTREE_VERDICT_POPULATION],
    )
    def test_hook_denies_iff_the_oracle_resolves_inside_the_repo_root(
        self, cache_root, project, case
    ):
        env = _worktree_env(cache_root)
        cmd = "git worktree add %s evil-branch" % case.spelling
        decision, reason = _run_worktree_hook(cmd, cache_root, project)
        if _worktree_oracle_outside(project, env, case.spelling):
            assert decision is None, (case.id, decision, reason)
        else:
            assert decision == "deny", (case.id, decision, reason)
            rel = _worktree_oracle_rel(project, env, case.spelling)
            top = rel.split(os.sep)[0]
            assert top in reason, (case.id, top, reason)


# --------------------------------------------------------------------------- #
# capability: with the repo root set to the invoking user's own home, the
# SAME 'git worktree add ~/evil evil-branch' now resolves INSIDE the repo and
# must DENY naming the real created directory, never '~' or '~/'. This is the
# non-vacuity proof the fix is not a blanket loosening: expansion can move a
# verdict in EITHER direction and both directions now agree with the shell.
# The implementer's own seam: tests/test_worktree_safety_extractor.py's `_env`
# copies os.environ AT CALL TIME, so monkeypatch.setenv reaches the hook
# subprocess with no new parameter needed.
# --------------------------------------------------------------------------- #
class TestRepoRootAsHomeIsTheNonVacuityProof:
    def test_same_command_denies_naming_evil_when_repo_root_is_home(
        self, cache_root, project, monkeypatch
    ):
        monkeypatch.setenv("HOME", str(project))
        env = _worktree_env(cache_root)
        assert not _worktree_oracle_outside(project, env, "~/evil"), (
            "fixture invariant broken: with HOME=project, ~/evil must resolve "
            "INSIDE the repo for this to be the non-vacuity proof"
        )
        decision, reason = _run_worktree_hook(
            "git worktree add ~/evil evil-branch", cache_root, project
        )
        assert decision == "deny", (decision, reason)
        assert "evil/" in reason, reason
        assert "~" not in reason, reason


# --------------------------------------------------------------------------- #
# capability: no reason the hook can emit names a .gitignore remedy for a
# target that a real bash resolves outside the repo root -- the property that
# matters most, asserted over a population rather than for one command.
# --------------------------------------------------------------------------- #
OUTSIDE_SPELLINGS: tuple[str, ...] = ("~/evil", "$HOME/evil", "${HOME}/evil", "~root/evil")


class TestNoReasonNamesAGitignoreRemedyForAPathTheShellPutsOutsideTheRepo:
    """The rule is that a refusal must never hand the user advice that cannot
    work. Every spelling here is asserted OUTSIDE by a real bash oracle
    before the property is judged, so a fixture that stopped meaning what it
    says reddens loudly instead of silently making the property vacuous."""

    def test_outside_spellings_population_is_non_empty(self):
        assert OUTSIDE_SPELLINGS

    @pytest.mark.parametrize("spelling", OUTSIDE_SPELLINGS, ids=lambda s: s)
    def test_no_gitignore_remedy_when_the_oracle_confirms_outside(
        self, cache_root, project, spelling
    ):
        env = _worktree_env(cache_root)
        assert _worktree_oracle_outside(project, env, spelling), (
            "fixture invariant broken: %r must resolve outside the sandboxed "
            "repo root for this property to mean anything" % spelling
        )
        decision, reason = _run_worktree_hook(
            "git worktree add %s evil-branch" % spelling, cache_root, project
        )
        assert not (decision == "deny" and "gitignore" in (reason or "").lower()), (
            spelling, decision, reason
        )


# --------------------------------------------------------------------------- #
# capability: a target whose FIRST segment carries an unresolved simple-name
# parameter still denies naming the top segment when THAT segment is
# knowable (scratch/$NOPE/x denies naming scratch/); the ask population below
# covers the case where the first segment itself is unresolvable.
# --------------------------------------------------------------------------- #
class TestUnresolvedParameterOutsideTheFirstSegmentStillDeniesNamingTheTop:
    def test_unresolved_variable_in_a_later_segment_still_denies_naming_the_top(
        self, cache_root, project, monkeypatch
    ):
        monkeypatch.delenv(SENTINEL_UNSET_VAR, raising=False)
        cmd = "git worktree add scratch/$%s/x evil-branch" % SENTINEL_UNSET_VAR
        decision, reason = _run_worktree_hook(cmd, cache_root, project)
        assert decision == "deny", (decision, reason)
        assert "scratch/" in reason, reason


@dataclass(frozen=True)
class WorktreeAskCase:
    id: str
    spelling: str
    form: str    # the UNRESOLVED FORMS header name this case corresponds to
    names: str   # the substring the reason must literally name


# The ask population (plan.md ## Analysis, DECISION 2 + "Header disclosure"):
# every form the worktree hook cannot resolve, and therefore asks about
# rather than guessing a gitignore remedy for. TestWorktreeUnresolvedFormsHeaderRatchet
# below DERIVES the header's expected names from this same population, so the
# header and the population cannot silently drift apart.
WORKTREE_ASK_POPULATION: list[WorktreeAskCase] = [
    WorktreeAskCase("tilde_pwd", "~+/evil", "tilde-pwd", "~+"),
    WorktreeAskCase("tilde_oldpwd", "~-/evil", "tilde-oldpwd", "~-"),
    WorktreeAskCase("tilde_dirstack", "~1/evil", "tilde-dirstack", "~1"),
    WorktreeAskCase(
        "unresolved_parameter", "$%s/evil" % SENTINEL_UNSET_VAR,
        "unresolved-parameter", SENTINEL_UNSET_VAR,
    ),
]


class TestWorktreeAskPopulationIsNonVacuous:
    def test_population_is_non_empty(self):
        assert WORKTREE_ASK_POPULATION

    def test_every_case_carries_a_form_name(self):
        assert all(c.form for c in WORKTREE_ASK_POPULATION), WORKTREE_ASK_POPULATION


# --------------------------------------------------------------------------- #
# capability: ~+/x, ~-/x and ~1/x, and an unresolved simple-name parameter in
# the FIRST path segment ($NOPE/x), each produce permissionDecision `ask`
# whose reason names the form and the way out, and none of them emits a
# gitignore remedy. Also the sentinel-print proof for the NEW ask path: if
# the implementation forgot to print the completion sentinel as the ask
# branch's last line, the bash consumer would read the truncated block as a
# fault and substitute writ_decider_fault's own reason, which this test
# distinguishes from a real, working ask.
# --------------------------------------------------------------------------- #
class TestUnresolvableFormsAskInsteadOfGuessingAGitignoreRemedy:
    @pytest.mark.parametrize(
        "case", WORKTREE_ASK_POPULATION, ids=[c.id for c in WORKTREE_ASK_POPULATION],
    )
    def test_each_unresolvable_form_asks_naming_the_form_with_no_gitignore_remedy(
        self, cache_root, project, monkeypatch, case
    ):
        monkeypatch.delenv(SENTINEL_UNSET_VAR, raising=False)
        cmd = "git worktree add %s evil-branch" % case.spelling
        decision, reason = _run_worktree_hook(cmd, cache_root, project)
        assert decision == "ask", (case.id, decision, reason)
        assert "ENF-DECIDER-INCOMPLETE" not in reason, (
            "the ask branch did not print the completion sentinel as its "
            "last line, so the consumer read it as a decider fault instead "
            "of the real ask: %s, %r" % (case.id, reason)
        )
        assert case.names in reason, (case.id, reason)
        assert "gitignore" not in reason.lower(), (case.id, reason)
        assert "spell the path literally" in reason.lower(), (case.id, reason)


# --------------------------------------------------------------------------- #
# capability: the worktree hook's header carries an UNRESOLVED FORMS block
# (a marker family distinct from the write gate's own UNEXPANDED FORMS, so
# the two ratchets cannot be conflated) naming exactly the forms the ask
# population above covers.
# --------------------------------------------------------------------------- #
def _unresolved_forms_from_worktree_header(source: str) -> set[str]:
    """Names-only block writ-worktree-safety.sh's header must carry, parsed
    the same shape this file's own `_unexpanded_forms_from_header` parses the
    write gate's sibling block."""
    try:
        start = source.index("# UNRESOLVED FORMS BEGIN")
    except ValueError:
        pytest.fail(
            "skeleton: hooks/scripts/writ-worktree-safety.sh has no "
            "'# UNRESOLVED FORMS BEGIN' block yet (plan.md ## Files)"
        )
    start = source.index("\n", start) + 1
    end = source.index("# UNRESOLVED FORMS END", start)
    body = " ".join(line.lstrip("#").strip() for line in source[start:end].splitlines())
    return {n.strip() for n in body.split(",") if n.strip()}


class TestWorktreeUnresolvedFormsHeaderRatchet:
    def test_header_names_exactly_the_ask_populations_forms(self):
        """Reddens if a form gains resolution in the code without leaving the
        header block, or if an ask case is added with no matching header
        name."""
        src = Path(WT_HOOK).read_text()
        declared = _unresolved_forms_from_worktree_header(src)
        population_forms = {c.form for c in WORKTREE_ASK_POPULATION}
        assert declared == population_forms, (declared, population_forms)


# --------------------------------------------------------------------------- #
# capability: every ask and every deny is recorded through log_gate_decision
# with the judged path in the target column, and an ask row never falls
# through to the allow record.
# --------------------------------------------------------------------------- #
class TestWorktreeAskIsDeclaredInTheFiredrillCensusAndNeverFallsThroughToAllow:
    """Ties tests/firedrill/_census.py's new ask-arm declaration to real
    coverage, the same way TestUnknownAskIsDeclaredInTheFiredrillCensus above
    ties the write gate's own unresolved-variable ask to one: reddens if the
    census entry stops matching a real ask, or if the audit row it produces
    is recorded as anything other than 'ask' -- in particular, never
    silently as 'allow'."""

    def test_declared_ask_arm_is_a_real_working_refusal_recorded_as_ask(self, tmp_path):
        from tests.firedrill._census import by_id, matches_action_marker
        from tests.firedrill._harness import make_isolation, run_hook

        entry = by_id("worktree-safety-unresolvable-tilde-ask")
        iso = make_isolation(tmp_path, session_id="expand-worktree-census-ask")
        setup = entry.setup(iso)
        result = run_hook(entry.script, setup["envelope"], iso,
                          extra_env=setup.get("extra_env"))
        assert result.permission_decision() == "ask", result.stdout
        assert matches_action_marker(result.permission_reason()), result.permission_reason()
        audit = result.audit()
        assert audit, "audit stream is empty; the hook recorded nothing"
        rows = [r for r in audit
                if r.get("event") == "gate_decision" and r.get("gate") == entry.gate_name]
        assert rows, audit
        assert rows[-1].get("decision") == "ask", rows
        assert not any(r.get("decision") == "allow" for r in rows), (
            "an ask row fell through to an allow record", rows
        )


class TestDenyAuditRowCarriesTheExpandedPathNotTheRawToken:
    """The deny half of the same capability, and plan.md ## Analysis's "THE
    AUDIT ROW" consumer: once the hook expands the target before classifying
    it, the column log_gate_decision records must carry the directory bash
    will really create, not the raw tilde token nobody could act on."""

    def test_the_target_column_is_the_expanded_relative_path(self, tmp_path):
        from tests.firedrill._harness import make_isolation, run_hook, write_cache

        iso = make_isolation(tmp_path, session_id="expand-worktree-audit-deny")
        write_cache(iso, {"mode": "work"})
        envelope = {
            "session_id": iso.session_id,
            "hook_event_name": "PreToolUse",
            "tool_name": "Bash",
            "tool_input": {"command": "git worktree add ~/evil evil-branch"},
        }
        result = run_hook("writ-worktree-safety.sh", envelope, iso,
                          extra_env={"HOME": str(iso.project_root)})
        assert result.permission_decision() == "deny", result.stdout
        rows = [r for r in result.audit()
                if r.get("event") == "gate_decision" and r.get("gate") == "worktree-safety"]
        assert rows, result.audit()
        assert rows[-1].get("target") == "evil", rows
        assert "~" not in (rows[-1].get("target") or ""), rows


# --------------------------------------------------------------------------- #
# The EXPAND marker family: writ/session/bash_expand.py plus an inline,
# byte-identical copy in each of the two Bash hooks (plan.md ## Files /
# ## Analysis "FINDING: the sibling helper cannot be reached..."). The
# population is DERIVED (tests/_inventory.py::expand_word_copy_sites), never
# a hardcoded three-file tuple, so a fourth pasted copy fails by name.
# --------------------------------------------------------------------------- #
EXPAND_NAMESPACE_NAMES = frozenset({"os", "re", "pwd", "strip_unbalanced_close"})

# A representative spelling table, not an exhaustive one: enough forms to
# tell apart the tilde branch, the parameter branch, quoting, escaping and an
# unresolved name, without re-deriving the write gate's own 20-entry
# population (that file's job, not this one's).
EXPAND_SPELLING_TABLE: tuple[str, ...] = (
    "~/probe", "~", "~root", "~nosuchuser123456xyz/probe",
    "$HOME/probe", "${HOME}/probe", "'$HOME/probe'", '"~/probe"',
    "\\~/probe", "~+/probe", "~-/probe", "~1/probe",
    "$%s/probe" % SENTINEL_UNSET_VAR,
)


def _expand_copy_namespace(block: str, path: str) -> dict:
    """One EXPAND-block copy, exec'd in a namespace holding ONLY os, re, pwd
    and strip_unbalanced_close (capability: "the EXPAND block contains no
    import statement and execs in a namespace holding only ..."). `exec` here
    runs REPO SOURCE TEXT this test just read off disk, never
    attacker-influenced input -- the same precedented pattern
    tests/test_bash_control_operator_split.py's `_mirror_ns` already uses for
    the sibling MIRROR block, one marker family over."""
    from writ.session.bash_tokens import strip_unbalanced_close

    ns = {"os": os, "re": re, "pwd": pwd, "strip_unbalanced_close": strip_unbalanced_close}
    exec(compile(block, "<expand:%s>" % path, "exec"), ns)  # noqa: S102
    return ns


def _real_expand_sites_or_skeleton_fail() -> dict[str, str]:
    sites = expand_word_copy_sites()
    if not sites:
        pytest.fail(
            "skeleton: no file under writ/, hooks/, bin/ or scripts/ carries "
            "the '# EXPAND BEGIN expand_word' marker yet (plan.md ## Files)"
        )
    return sites


class TestExpandWordCopySitesIsDerivedNotHardcoded:
    """The population must come from scanning the tree, never from a fixed
    three-file tuple, so a fourth pasted copy is caught BY NAME and a
    detector that quietly returns {} cannot pass silently. Pinned against
    SYNTHETIC files under tmp_path, the way
    tests/test_daemon_leak_guard.py::TestDaemonStartingHooksIsDerivedNotHardcoded
    pins `daemon_starting_hooks`."""

    def test_the_real_tree_yields_a_non_empty_map(self):
        sites = expand_word_copy_sites()
        assert isinstance(sites, dict)
        assert sites, (
            "expand_word_copy_sites() found no '# EXPAND BEGIN expand_word' "
            "marker anywhere under writ/, hooks/, bin/ or scripts/ (plan.md "
            "## Files)"
        )

    def test_the_real_population_names_at_least_the_three_files_the_plan_expects(self):
        sites = _real_expand_sites_or_skeleton_fail()
        names = {Path(p).name for p in sites}
        assert names >= {
            "writ-bash-write-gate.sh", "writ-worktree-safety.sh", "bash_expand.py",
        }, names

    def test_a_synthetic_file_with_the_markers_is_found(self, tmp_path):
        f = tmp_path / "made-up.py"
        f.write_text(
            "%s\nX = 1\n%s\n" % (EXPAND_MARK_BEGIN, EXPAND_MARK_END)
        )
        sites = expand_word_copy_sites(roots=(tmp_path,))
        assert str(f) in sites, sites
        assert sites[str(f)].strip() == "X = 1", sites[str(f)]

    def test_a_file_with_no_markers_is_absent(self, tmp_path):
        f = tmp_path / "unrelated.py"
        f.write_text("X = 1\n")
        sites = expand_word_copy_sites(roots=(tmp_path,))
        assert str(f) not in sites, sites

    def test_a_drifted_copy_still_registers_but_reads_as_a_mismatch(self, tmp_path):
        """A fourth pasted copy that has DRIFTED from the other three must
        still be FOUND by name (present in the map) so the identity test
        below can report it as a mismatch, rather than the derivation
        silently excluding anything that does not match today's exact
        text."""
        a = tmp_path / "a.py"
        b = tmp_path / "b.py"
        a.write_text("%s\nX = 1\n%s\n" % (EXPAND_MARK_BEGIN, EXPAND_MARK_END))
        b.write_text("%s\nX = 2\n%s\n" % (EXPAND_MARK_BEGIN, EXPAND_MARK_END))
        sites = expand_word_copy_sites(roots=(tmp_path,))
        assert str(a) in sites and str(b) in sites
        assert sites[str(a)] != sites[str(b)], (
            "a deliberately drifted pair of synthetic copies read as "
            "identical; the derivation is not reading the block text at all"
        )


class TestEveryRealExpandCopyIsByteIdenticalAndImportless:
    """Capability: every file carrying the EXPAND markers holds a
    byte-identical block, and none of them may import anything -- the same
    contract the MIRROR block already holds split_control_operators to,
    one marker family over."""

    def test_every_copy_in_the_real_tree_is_byte_identical(self):
        sites = _real_expand_sites_or_skeleton_fail()
        assert len(set(sites.values())) == 1, sorted(sites)

    def test_none_of_the_real_copies_contain_an_import_statement(self):
        sites = _real_expand_sites_or_skeleton_fail()
        for path, block in sites.items():
            offenders = [
                ln for ln in block.splitlines()
                if ln.strip().startswith(("import ", "from "))
            ]
            assert not offenders, (path, offenders)


class TestEachExpandCopyExecsInTheDeclaredNamespaceAndAgrees:
    """Capability: each copy computes identical (value, unresolved) pairs
    over a shared spelling table, execing bare in a namespace holding ONLY
    os, re, pwd and strip_unbalanced_close. ORACLE DISCIPLINE note: this is a
    copy-vs-copy identity check, not a copy-vs-shell one -- expand_word's own
    agreement with a real bash is TestWorktreeHookAgreesWithTheShellOnTilde's
    job, exercised through the real hook, not through this exec'd copy."""

    def test_each_copy_execs_bare_and_defines_expand_word(self):
        sites = _real_expand_sites_or_skeleton_fail()
        for path, block in sites.items():
            ns = _expand_copy_namespace(block, path)
            assert "expand_word" in ns, (path, sorted(ns))

    @pytest.mark.parametrize("spelling", EXPAND_SPELLING_TABLE, ids=lambda s: s)
    def test_every_copy_agrees_with_every_other_on_the_spelling_table(
        self, spelling, monkeypatch
    ):
        monkeypatch.delenv(SENTINEL_UNSET_VAR, raising=False)
        sites = _real_expand_sites_or_skeleton_fail()
        results = {}
        for path, block in sites.items():
            ns = _expand_copy_namespace(block, path)
            results[path] = ns["expand_word"](spelling)
        assert len(set(results.values())) == 1, (spelling, results)


class TestBothHooksRebindExpandWordFromThePackageAfterTheInlineCopy:
    """Capability: both hooks prepare sys.path and rebind expand_word from
    writ.session.bash_expand AFTER the inline copy -- the same pattern and
    the same reason as the MIRROR block's own package rebind
    (test_bash_control_operator_split.py::TestThePackageCopyIsTheOneThatRuns).
    The sys.path insert is ALREADY present in both hooks (it is what makes
    the MIRROR import resolve too), so only the NEW import needs to exist,
    and it must come AFTER this file's own EXPAND END marker: a rebind
    before the inline copy would shadow nothing."""

    @pytest.mark.parametrize(
        "hook", [HOOK_SH, WT_HOOK], ids=["write-gate", "worktree-hook"],
    )
    def test_the_hook_rebinds_expand_word_from_the_package_after_the_inline_copy(
        self, hook
    ):
        src = Path(hook).read_text()
        if EXPAND_MARK_END not in src:
            pytest.fail(
                "skeleton: %s has no '# EXPAND END expand_word' marker yet "
                "(plan.md ## Files)" % hook
            )
        rebind_needle = "from writ.session.bash_expand import expand_word"
        assert rebind_needle in src, hook
        assert src.index(rebind_needle) > src.index(EXPAND_MARK_END), (
            "%s rebinds expand_word before its own inline EXPAND END marker" % hook
        )
