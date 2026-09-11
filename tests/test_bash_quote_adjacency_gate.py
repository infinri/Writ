"""`shlex.split(cmd, comments=False, posix=False)` ends a token at the closing quote of a
span that STARTED that token, so one shell word arrives as TWO tokens whenever an unquoted
fragment is glued after a leading quoted span (plan.md / capabilities.md, session
2412ba38-51e1-4b73-895b-7b240a3c21d3).

    shlex.split('cp x "$HOME"/y', comments=False, posix=False)
    -> ['cp', 'x', '"$HOME"', '/y']        one bash word, TWO tokens

Three DIFFERENT consequences were measured through the real extractor: `cp x
"$HOME"escape.txt` read as project-local while bash wrote OUTSIDE the project (an escape,
more permissive); `cp x "src"/escape.txt` read as `/escape.txt` outside the project while
bash wrote `src/escape.txt` (an over-block); `echo hi > "$HOME"/escape.txt` kept the
quoted head and dropped the FILENAME. The fix is `rejoin_glued_words(text, toks)`, added to
`writ/session/bash_tokens.py`'s MIRROR block, plus turning `dequote` (currently duplicated
outside the mirror in both hooks) into a quote-state walk instead of a matched-outer-pair
test, because the rejoin alone moves quote characters INTO tokens other consumers dequote.

THE THREE CONSEQUENCE ROWS ARE ASSERTED AGAINST A REAL BASH ORACLE, never against a
hand-written expected string: a `subprocess` call to `bash -c` resolves the same spelling,
and the gate's classified abspath must equal what the shell actually does. That is what
makes this a parity test rather than a restatement of the fix.

RED-BY-CONSTRUCTION, not by skeleton, matching this suite's existing convention
(tests/test_bash_expansion_boundary_gate.py, tests/test_project_boundary.py): neither
`rejoin_glued_words` nor the moved `dequote` exists in `writ/session/bash_tokens.py` yet, so
every test that needs one calls `pytest.fail` with a `skeleton:` message rather than letting
a bare import raise at collection time, which would fail for the wrong reason (an
ImportError proves nothing about behavior).

Idioms reused rather than modelled (both sides of a parity assertion must be real
callers, never a hand-rolled model of one path -- this repo's own recorded history):
`run_extractor`, `_run_hook`, `_seed`, `_extract`, `EXTRACTOR_SENTINEL` from
tests/test_bash_write_gate.py; `_extract_egress` from tests/test_bash_egress_gate.py;
`_extract_rows`, `_classified_abspath`, `_oracle_word`, `_expected_abspath` from
tests/test_bash_expansion_boundary_gate.py; `_run` (as `_run_worktree_hook`), `cache_root`
and `project` from tests/test_worktree_safety_extractor.py; `bash_token_split_sites` and
`python_shlex_split_callers` from tests/_inventory.py; `strip_group_opener` (already shipped,
untouched by this cycle) straight from writ/session/bash_tokens.py.
"""
from __future__ import annotations

import importlib
import os
import re
import shlex
import uuid
from dataclasses import dataclass
from pathlib import Path

import pytest

# autouse: pins cwd to a sandbox, this suite's standing convention.
from tests.fixtures.session_state import sandbox_cwd  # noqa: F401
from tests._inventory import bash_token_split_sites, python_shlex_split_callers
from tests.test_bash_egress_gate import _extract_egress
from tests.test_bash_expansion_boundary_gate import (
    _classified_abspath,
    _expected_abspath,
    _extract_rows,
    _oracle_word,
)
from tests.test_bash_write_gate import (
    EXTRACTOR_SENTINEL,
    HOOK_SH,
    SKILL_ROOT,
    _extract,
    _run_hook,
    _seed,
    run_extractor,
)
from tests.test_worktree_safety_extractor import _run as _run_worktree_hook
from tests.test_worktree_safety_extractor import cache_root, project  # noqa: F401
from writ.session.bash_tokens import strip_group_opener

WT_HOOK = os.path.join(SKILL_ROOT, "hooks", "scripts", "writ-worktree-safety.sh")
MIRROR_BEGIN = "# MIRROR BEGIN split_control_operators"
MIRROR_END = "# MIRROR END split_control_operators"


def _bash_tokens_module():
    return importlib.import_module("writ.session.bash_tokens")


def _rejoin_glued_words(text: str, toks: list) -> list:
    fn = getattr(_bash_tokens_module(), "rejoin_glued_words", None)
    if fn is None:
        pytest.fail(
            "skeleton: writ.session.bash_tokens.rejoin_glued_words does not exist yet "
            "(plan.md ## Files: writ/session/bash_tokens.py)"
        )
    return fn(text, toks)


def _dequote(tok: str) -> str:
    fn = getattr(_bash_tokens_module(), "dequote", None)
    if fn is None:
        pytest.fail(
            "skeleton: writ.session.bash_tokens.dequote does not exist yet -- it moves "
            "into the MIRROR block as a quote-state walk (plan.md ## Files)"
        )
    return fn(tok)


@dataclass
class Fixture:
    project: Path
    home: Path
    env: dict


@pytest.fixture
def adjacency_fixture(tmp_path: Path) -> Fixture:
    """Just what the three consequence rows need: a project cwd, a HOME outside it, and
    an env carrying that HOME. No oldpwd, no second variable, no directory stack --
    tests/test_bash_expansion_boundary_gate.py's `boundary_fixture` builds those for a
    different cycle's spellings, and this one does not read them."""
    project = tmp_path / "proj"
    home = tmp_path / "home"
    project.mkdir()
    home.mkdir()
    env = dict(os.environ)
    env["HOME"] = str(home)
    return Fixture(project, home, env)


# --------------------------------------------------------------------------- #
# capabilities 1-3: rejoin_glued_words, as a pure function over a measured
# truth table -- no hook, no subprocess, no oracle needed for these.
# --------------------------------------------------------------------------- #
REJOIN_TRUTH_TABLE = [
    pytest.param(
        'cp x "$HOME"/y', ["cp", "x", '"$HOME"/y'],
        id="quote_then_unquoted_suffix",
    ),
    pytest.param(
        'cp x "a"b"c"', ["cp", "x", '"a"b"c"'],
        id="multi_fragment_transitive_merge",
    ),
    pytest.param(
        "cp x 'single'/y", ["cp", "x", "'single'/y"],
        id="single_quoted_prefix",
    ),
    pytest.param(
        'cp x "$HOME"/y "$HOME"/z', ["cp", "x", '"$HOME"/y', '"$HOME"/z'],
        id="two_destinations_stay_separate",
    ),
    pytest.param(
        '"c"p readme .env', ['"c"p', "readme", ".env"],
        id="mid_word_quote_split_verb",
    ),
]


class TestRejoinGluedWordsReunitesTheMeasuredTruthTable:
    """Capability 1. Every case here starts from a REAL `shlex.split` call on the same
    text the function is handed, never a hand-typed token list, so a change to shlex's
    own splitting behavior would show up here before it reached the merge logic."""

    @pytest.mark.parametrize("text,expected", REJOIN_TRUTH_TABLE)
    def test_rejoin_reunites_the_measured_spelling(self, text, expected):
        raw = shlex.split(text, comments=False, posix=False)
        got = _rejoin_glued_words(text, raw)
        assert got == expected, (text, raw, got, expected)

    def test_merging_is_transitive_across_more_than_two_fragments(self):
        """`"a"b"c"` is three quote-boundary hops, not one: reddens if the merge stops
        after the first pair instead of continuing while the next gap is still empty."""
        text = 'cp x "a"b"c"'
        raw = shlex.split(text, comments=False, posix=False)
        assert raw == ["cp", "x", '"a"', 'b"c"'], raw  # the shape this case depends on
        got = _rejoin_glued_words(text, raw)
        assert got == ["cp", "x", '"a"b"c"'], got


class TestRejoinGluedWordsLeavesASpacedPairAlone:
    """Capability 2. Together with the truth table above, this is the pair of tests an
    "always merge" implementation cannot pass: forcing every gap to merge reddens this
    one (`"$HOME"` and `/y` would wrongly become one token), and forcing the gap test to
    "never merge" reddens the truth table above instead. Neither wrong shape can pass
    both classes at once."""

    def test_a_real_whitespace_gap_is_never_merged(self):
        text = 'cp x "$HOME" /y'
        raw = shlex.split(text, comments=False, posix=False)
        assert raw == ["cp", "x", '"$HOME"', "/y"], raw
        got = _rejoin_glued_words(text, raw)
        assert got == raw, (text, raw, got)


class TestRejoinGluedWordsFailSafe:
    """Capability 3. A wrong merge is worse than the defect it repairs, so both failure
    modes of the cursor walk must decline rather than guess."""

    def test_a_token_not_found_at_or_after_the_cursor_returns_input_unchanged(self):
        toks = ["cp", "ZZZ", "y"]
        got = _rejoin_glued_words("cp x y", toks)
        assert got == toks, got

    def test_a_gap_holding_anything_other_than_whitespace_returns_input_unchanged(self):
        # Between "cp" (ends at index 2) and "a" (found at index 3) sits "X", which is
        # not whitespace and not empty: the fail-safe must decline, not merge past it.
        toks = ["cp", "a"]
        got = _rejoin_glued_words("cpXa", toks)
        assert got == toks, got


# --------------------------------------------------------------------------- #
# capabilities 4-6: the three consequence rows, through the real extractor and
# compared against a REAL bash oracle -- never a hand-written expected string.
# --------------------------------------------------------------------------- #
class TestTheThreeConsequenceRowsAgreeWithTheRealShell:
    def test_the_escape_row_is_gated_instead_of_approved_as_project_local(
        self, adjacency_fixture,
    ):
        """`cp x "$HOME"escape.txt`: before the fix `cand[-1]` sees only the trailing
        `escape.txt` fragment, which looks project-local; a real shell resolves the
        WHOLE word to a path under HOME, outside the project. Reddens today because the
        gate's abspath is the pre-fix literal join, not the oracle's."""
        fx = adjacency_fixture
        dest = '"$HOME"escape.txt'
        rows = _extract_rows("cp README.md %s" % dest, fx.project, fx.env)
        oracle = _oracle_word(dest, fx.project, fx.env)
        expected = _expected_abspath(fx.project, oracle)
        outside_hits = [r for r in rows if r and r[0] == "outside"]
        assert outside_hits, (
            f"expected an `outside` row for the boundary-escape spelling {dest!r}, "
            f"matching the real shell's answer {expected!r}; got {rows!r}"
        )
        assert outside_hits[0][1] == expected, (outside_hits, expected, rows)

    def test_the_over_block_direction_classifies_as_project_local(self, adjacency_fixture):
        """`cp x "src"/escape.txt`: before the fix the leftover `/escape.txt` fragment
        LOOKS absolute and reads `outside`, while bash resolves the whole word to a
        relative path under the project. Reddens today because the pre-fix path is
        `/escape.txt`, not the oracle's project-relative answer."""
        fx = adjacency_fixture
        dest = '"src"/escape.txt'
        rows = _extract_rows("cp README.md %s" % dest, fx.project, fx.env)
        oracle = _oracle_word(dest, fx.project, fx.env)
        expected = _expected_abspath(fx.project, oracle)
        local_hits = [r for r in rows if r and r[0] == "local"]
        assert local_hits, (
            f"expected a `local` row for the over-block spelling {dest!r}, matching "
            f"the real shell's answer {expected!r}; got {rows!r}"
        )
        assert local_hits[0][1] == expected, (local_hits, expected, rows)

    @pytest.mark.parametrize("cmd_tmpl", [
        pytest.param('echo hi > "$HOME"/escape.txt', id="spaced_redirect"),
        pytest.param('echo hi >"$HOME"/escape.txt', id="glued_redirect"),
    ])
    def test_the_dropped_filename_direction_keeps_the_whole_word(
        self, adjacency_fixture, cmd_tmpl,
    ):
        """`echo hi > "$HOME"/escape.txt`: before the fix the redirect target is only
        the trailing `/escape.txt` fragment, so the FILENAME half of the word is
        dropped; a real shell resolves the whole word to a path under HOME. Both the
        spaced and the already-one-token glued spelling must resolve identically."""
        fx = adjacency_fixture
        dest = '"$HOME"/escape.txt'
        rows = _extract_rows(cmd_tmpl, fx.project, fx.env)
        oracle = _oracle_word(dest, fx.project, fx.env)
        expected = _expected_abspath(fx.project, oracle)
        got = _classified_abspath(rows)
        assert got == expected, (cmd_tmpl, got, expected, rows)


# --------------------------------------------------------------------------- #
# capability 7: the credential guard, once the rejoined verb dequotes to "cp".
# --------------------------------------------------------------------------- #
class TestMidWordQuoteSplitCredentialWrite:
    def test_the_real_hook_denies_with_sec_credential_write(self, tmp_path):
        """Reddens today (the strict xfail this cycle retires, at
        tests/test_bash_pattern_spelling_gate.py's
        TestDeferredResidueStrictXfail.test_mid_word_quote_split_credential_write_would_deny):
        without the rejoin, `"c"p` never dequotes to `cp` and the credential arm never
        sees a `cred` row at all."""
        sid = "adj-cred-%s" % uuid.uuid4().hex[:8]
        _seed(sid, mode="conversation")
        out = _run_hook('"c"p readme .env', sid, str(tmp_path))
        assert out is not None, "the mid-word quote split credential write was silent"
        assert out.get("permissionDecision") == "deny", out
        assert "SEC-CREDENTIAL-WRITE" in out.get("permissionDecisionReason", ""), out


# --------------------------------------------------------------------------- #
# capability 8: dequote as a quote-state walk, replacing the matched-outer-pair rule.
# --------------------------------------------------------------------------- #
class TestDequoteIsAQuoteStateWalk:
    @pytest.mark.parametrize("tok,expected", [
        pytest.param('"c"p', "cp", id="verb_split_by_a_leading_quote_pair"),
        pytest.param('"a"b"c"', "abc", id="multiple_quote_spans_in_one_token"),
        pytest.param('"$HOME"/y', "$HOME/y", id="quote_then_unquoted_suffix_no_expansion"),
        pytest.param("'single'/y", "single/y", id="single_quoted_prefix_no_expansion"),
        pytest.param('"cp"', "cp", id="fully_double_quoted_unchanged_from_the_old_rule"),
        pytest.param("'(cp'", "(cp", id="fully_single_quoted_unchanged_from_the_old_rule"),
        pytest.param("'-exec'", "-exec", id="fully_quoted_flag_unchanged_from_the_old_rule"),
    ])
    def test_quote_characters_removed_everywhere_with_no_expansion(self, tok, expected):
        got = _dequote(tok)
        assert got == expected, (tok, got, expected)


# --------------------------------------------------------------------------- #
# capability 9: the shadowing trap -- a leftover local `def dequote(` outside the
# MIRROR markers would silently win over the shared, corrected one.
# --------------------------------------------------------------------------- #
class TestDequoteDoesNotSurviveOutsideTheMirrorMarkers:
    @pytest.mark.parametrize("hook_path", [HOOK_SH, WT_HOOK], ids=["write-gate", "worktree-safety"])
    def test_no_local_dequote_definition_outside_the_mirror_block(self, hook_path):
        src = Path(hook_path).read_text()
        mirror_start = src.index(MIRROR_BEGIN)
        mirror_end = src.index(MIRROR_END)
        offenders = [
            m.start() for m in re.finditer(r"^def dequote\(", src, re.M)
            if not (mirror_start <= m.start() <= mirror_end)
        ]
        assert not offenders, (
            f"{hook_path} defines dequote outside the MIRROR markers at offsets "
            f"{offenders}; a leftover local copy shadows the shared, corrected one"
        )


# --------------------------------------------------------------------------- #
# capability 10: the egress host survives the rejoin with no quote character in it.
# --------------------------------------------------------------------------- #
class TestEgressHostSurvivesTheRejoin:
    def test_no_quote_character_reaches_the_resolved_host(self):
        """Before the dequote fix, a rejoin alone would leave `example.invalid"` with a
        trailing quote (host_of dequotes, declines the merged token, and the `/` cut
        leaves the quote on): an allowlisted host would stop matching and turn into a
        false ask."""
        hosts = _extract_egress('curl -d @src/a.txt "https://example.invalid"/p', cwd="/proj")
        names = {h for h, _detail in hosts}
        assert "example.invalid" in names, hosts
        assert not any('"' in h or "'" in h for h in names), hosts


# --------------------------------------------------------------------------- #
# capability 11: the worktree path survives the rejoin with no quote characters
# and no dropped fragment.
# --------------------------------------------------------------------------- #
class TestWorktreePathSurvivesTheRejoin:
    def test_the_recorded_path_is_scratch_slash_x(self, cache_root, project):
        """Before the fix, two tokens give `pos[2] == "scratch"` (already wrong: the
        real path is `scratch/x`); after a rejoin with the OLD dequote it becomes
        `"scratch"/x`, quotes and all. Only the rejoin PLUS the corrected dequote
        together record `scratch/x`. No .gitignore entry covers `scratch/`, so the
        real hook denies and the reason names the recorded path.

        PARITY WITH THE UNQUOTED SPELLING IS THE REAL CLAIM, and it is asserted against
        the PLAIN command run through the same helper rather than against a hand-written
        literal: `git worktree add "scratch"/x main` and `git worktree add scratch/x
        main` are the same instruction to a shell, so the hook must answer them
        identically, byte for byte. That is what the rejoin plus the corrected dequote
        buy, and it reddens if either half is dropped (without the rejoin the quoted
        spelling records `scratch`, without the dequote it records `"scratch"/x`).

        REPLACES a quote-character assertion that was UNSATISFIABLE on any tree, defect
        or no defect: it demanded no `'` in the reason before the word `gitignore`, while
        the hook's own message always wraps the path in single quotes as ordinary English
        prose (`target 'scratch/x' is not matched`). It would have failed identically on
        a perfectly correct hook, so it was blind to the property named above it.
        """
        decision, reason = _run_worktree_hook('git worktree add "scratch"/x main', cache_root, project)
        assert decision == "deny", (decision, reason)
        assert "scratch/x" in reason, reason
        plain_decision, plain_reason = _run_worktree_hook(
            "git worktree add scratch/x main", cache_root, project)
        assert (decision, reason) == (plain_decision, plain_reason), (
            "the quoted spelling must resolve exactly as the unquoted one; got "
            f"{(decision, reason)!r} versus {(plain_decision, plain_reason)!r}"
        )


# --------------------------------------------------------------------------- #
# capability 12: the fail-safe for unbalanced quotes is unaffected by the rejoin.
# --------------------------------------------------------------------------- #
class TestUnbalancedQuotesStillFailOpen:
    def test_the_extractor_prints_the_sentinel_and_exits_zero(self):
        """This is a non-regression pin: shlex.split itself still raises ValueError on
        an unbalanced quote before rejoin_glued_words is ever called, and that except
        arm is explicitly untouched by this cycle (plan.md's ## The two call sites)."""
        p, lines = run_extractor('cp x "unterminated', "/proj")
        assert p.returncode == 0, (p.returncode, p.stdout, p.stderr)
        assert EXTRACTOR_SENTINEL in p.stdout.splitlines(), p.stdout
        assert lines == [], lines


# --------------------------------------------------------------------------- #
# capability 13: properties that depend on quote characters staying ON the
# token, which the rejoin must not disturb.
# --------------------------------------------------------------------------- #
class TestQuoteDependentPropertiesSurviveTheRejoin:
    def test_a_quoted_semicolon_is_still_not_a_separator(self):
        assert _extract("grep ';' src/app.py") == set()

    def test_a_quoted_redirect_operator_is_still_not_a_redirect(self):
        assert _extract("grep '>' src/app.py") == set()

    def test_a_quoted_exec_flag_still_yields_no_rows(self):
        assert _extract("grep -- '-exec' src/app.py") == set()

    def test_a_quoted_target_followed_by_a_glued_separator_still_resolves(self):
        assert ("local", "/proj/src/log.txt") in _extract('echo x > "src/log.txt"; ls')

    def test_strip_group_opener_of_a_quoted_paren_verb_is_unchanged(self):
        assert strip_group_opener("'(cp'") == "'(cp'"


# --------------------------------------------------------------------------- #
# capability 14: the heredoc residue is unchanged in both direction and value.
# --------------------------------------------------------------------------- #
class TestHeredocResidueIsUnchangedByTheRejoin:
    def test_glued_opener_heredoc_still_yields_exactly_the_body_lines_target(self):
        """The glued opener `<<'EOF'>docs/notes.txt` is already ONE shlex token before
        this cycle (a token starting unquoted absorbs), so the rejoin has nothing to
        merge here; pinned as a non-regression alongside
        tests/test_bash_newline_separator_gate.py:407-416, which owns this property."""
        cmd = "cat <<'EOF'>docs/notes.txt\ncp seed.txt src/x.py\nEOF\n"
        assert _extract(cmd) == {("local", "/proj/src/x.py")}, cmd


# --------------------------------------------------------------------------- #
# capabilities 16-17: the two derived populations, pinned against SYNTHETIC
# fixtures (never only the real tree's current wording).
# --------------------------------------------------------------------------- #
class TestBashTokenSplitSitesIsDerivedNotHardcoded:
    """The population must come from scanning hooks/scripts/, never from a fixed list
    of names, matching tests/test_daemon_leak_guard.py's own convention for
    daemon_starting_hooks()."""

    def test_the_real_scripts_dir_yields_a_non_empty_map(self):
        """Reddened by a derivation that silently returns {}, which would make every
        other assertion in this class pass vacuously."""
        population = bash_token_split_sites()
        assert isinstance(population, dict)
        assert population, (
            "bash_token_split_sites() returned an empty map against the real "
            "hooks/scripts/ tree"
        )

    def test_both_real_hooks_read_true_once_every_call_is_wrapped(self):
        """RED today: both hooks call shlex.split raw, with no rejoin_glued_words
        wrapper anywhere. Goes green only once both call sites are wrapped on the same
        line (plan.md ## The two call sites)."""
        population = bash_token_split_sites()
        assert population == {
            "writ-bash-write-gate.sh": True,
            "writ-worktree-safety.sh": True,
        }, population

    def test_a_synthetic_script_with_every_call_wrapped_reads_true(self, tmp_path):
        script = tmp_path / "made-up-hook.sh"
        script.write_text(
            "python3 <<PY\n"
            "raw_tokens = rejoin_glued_words(pre, shlex.split(pre, comments=False, posix=False))\n"
            "PY\n"
        )
        population = bash_token_split_sites(scripts_dir=tmp_path)
        assert population.get("made-up-hook.sh") is True, population

    def test_a_synthetic_script_that_keeps_the_raw_call_and_drops_the_wrap_reads_false(
        self, tmp_path,
    ):
        """Reddened by a derivation that assumes every shlex.split call it finds is
        wrapped, instead of checking for the wrap on the same line."""
        script = tmp_path / "made-up-hook.sh"
        script.write_text(
            "python3 <<PY\n"
            "raw_tokens = shlex.split(pre, comments=False, posix=False)\n"
            "PY\n"
        )
        population = bash_token_split_sites(scripts_dir=tmp_path)
        assert population.get("made-up-hook.sh") is False, population

    def test_a_synthetic_script_that_only_mentions_the_call_in_a_comment_is_excluded(
        self, tmp_path,
    ):
        """A mention is not a call: whole-line comments are blanked before the scan, so
        a script that only describes shlex.split in prose never enters the population
        (and is never falsely reported as unwrapped)."""
        script = tmp_path / "made-up-hook.sh"
        script.write_text(
            "# this hook used to call shlex.split(cmd) directly\n"
            "echo hello\n"
        )
        population = bash_token_split_sites(scripts_dir=tmp_path)
        assert "made-up-hook.sh" not in population, population

    def test_a_script_with_no_shlex_split_call_is_absent_from_the_map(self, tmp_path):
        script = tmp_path / "unrelated-hook.sh"
        script.write_text("echo hello\n")
        population = bash_token_split_sites(scripts_dir=tmp_path)
        assert "unrelated-hook.sh" not in population, population

    def test_a_docstring_only_mention_is_excluded_the_way_a_comment_mention_is(
        self, tmp_path,
    ):
        """The case blanking `#` comments cannot reach, and the one the real tree hit:
        the shared MIRROR block DOCUMENTS this defect, so its `rejoin_glued_words`
        docstring shows the raw call three times. Those lines are python string data, not
        comments, and before the predicate read code through `ast` both real hooks read
        False on them while both real call sites were correctly wrapped."""
        script = tmp_path / "made-up-hook.sh"
        script.write_text(
            "python3 <<PY\n"
            "def rejoin_glued_words(text, toks):\n"
            '    """Repairs what this spelling breaks:\n'
            "\n"
            "        shlex.split(text, comments=False, posix=False)\n"
            '    """\n'
            "    return toks\n"
            "PY\n"
        )
        population = bash_token_split_sites(scripts_dir=tmp_path)
        assert "made-up-hook.sh" not in population, population

    def test_a_docstring_mention_beside_a_real_unwrapped_call_still_reads_false(
        self, tmp_path,
    ):
        """THE ANTI-VACUITY CASE for the test above. Skipping prose must not become
        skipping the file: a script that both DESCRIBES the call and really makes one,
        unwrapped, has to stay in the population and read False. A predicate that
        excluded anything with a docstring mention would drop this script out of the map
        entirely and pass the test above for the wrong reason."""
        script = tmp_path / "made-up-hook.sh"
        script.write_text(
            "python3 <<PY\n"
            "def rejoin_glued_words(text, toks):\n"
            '    """Repairs what this spelling breaks:\n'
            "\n"
            "        shlex.split(text, comments=False, posix=False)\n"
            '    """\n'
            "    return toks\n"
            "\n"
            "raw_tokens = shlex.split(pre, comments=False, posix=False)\n"
            "PY\n"
        )
        population = bash_token_split_sites(scripts_dir=tmp_path)
        assert population.get("made-up-hook.sh") is False, population

    def test_an_unparsable_python_body_fails_closed_rather_than_silently_passing(
        self, tmp_path,
    ):
        """The stated fail-safe, asserted rather than trusted. A body that does not parse
        contributes no prose spans, so a mention inside it counts as a CALL and the
        script reads False: a guard that cannot tell demands a look instead of passing
        quietly. Reddens if the parse failure is ever made to skip the body's matches."""
        script = tmp_path / "made-up-hook.sh"
        script.write_text(
            "python3 <<PY\n"
            "def broken(:\n"
            '    """mentions shlex.split(text) in prose"""\n'
            "PY\n"
        )
        population = bash_token_split_sites(scripts_dir=tmp_path)
        assert population.get("made-up-hook.sh") is False, population


class TestTheProseExclusionIsConditionalNotVacuous:
    """MUTATION, not assertion alone. Each mutation below is applied to a copy of the
    REAL hook source under tmp_path, so what is proven is that the derivation's answer
    depends on the thing it claims to depend on."""

    @staticmethod
    def _hook_copy(tmp_path: Path, path: str, old: str, new: str) -> dict:
        src = Path(path).read_text()
        assert src.count(old) >= 1, ("mutation target not found", path, old)
        (tmp_path / Path(path).name).write_text(src.replace(old, new))
        return bash_token_split_sites(scripts_dir=tmp_path)

    def test_unwrapping_the_real_call_reddens_the_verdict(self, tmp_path):
        """The wrap is what the True depends on: strip `rejoin_glued_words(` off the
        production call site and the same hook must read False."""
        population = self._hook_copy(
            tmp_path, HOOK_SH,
            "raw_tokens = rejoin_glued_words(pre, shlex.split(pre, comments=False, posix=False))",
            "raw_tokens = shlex.split(pre, comments=False, posix=False)",
        )
        assert population.get("writ-bash-write-gate.sh") is False, population

    def test_adding_an_unwrapped_call_inside_the_mirror_span_is_still_seen(self, tmp_path):
        """THE BLIND-SPOT GUARD. Excluding the MIRROR span was the rejected fix, because
        `shlex` really is in scope inside it: the hook's heredoc opens with
        `import os, re, shlex, sys` and the span runs inside that same program. A raw
        call planted between the markers therefore EXECUTES, and the derivation must
        still see it."""
        population = self._hook_copy(
            tmp_path, HOOK_SH,
            "def rejoin_glued_words(text, toks):",
            "def planted(text):\n    return shlex.split(text, posix=False)\n\n\n"
            "def rejoin_glued_words(text, toks):",
        )
        assert population.get("writ-bash-write-gate.sh") is False, population


class TestPythonShlexSplitCallersStaysEmptyAgainstTheRealTree:
    def test_no_module_under_writ_calls_shlex_split_today(self):
        """A future tokenizer added under writ/ must not sit outside every population:
        this is the anti-blindness companion to bash_token_split_sites(), scoped to
        hooks/scripts/. Empty today is the correct answer, not a vacuous one -- the
        synthetic case below proves the scanner can see a call when one exists."""
        population = python_shlex_split_callers()
        assert population == {}, population

    def test_a_synthetic_module_that_calls_shlex_split_is_counted(self, tmp_path, monkeypatch):
        """`python_shlex_split_callers` keys its result on `path.relative_to(REPO)`, the
        module-level constant, not on `package_dir` -- so a `package_dir` planted under
        the system temp dir (outside REPO) makes that `relative_to` raise before the
        derivation can answer. REPO is monkeypatched to `tmp_path` for this test only,
        which both satisfies the relative_to call and keeps the real repo untouched."""
        import tests._inventory as inv

        monkeypatch.setattr(inv, "REPO", tmp_path)
        pkg = tmp_path / "made_up_pkg"
        pkg.mkdir()
        (pkg / "tokenizer.py").write_text(
            "import shlex\n"
            "def f(s):\n"
            "    return shlex.split(s, posix=False)\n"
        )
        population = inv.python_shlex_split_callers(package_dir=pkg)
        assert population == {"made_up_pkg/tokenizer.py": 1}, population

    def test_a_docstring_mention_of_shlex_split_is_not_miscounted_as_a_call(
        self, tmp_path, monkeypatch,
    ):
        import tests._inventory as inv

        monkeypatch.setattr(inv, "REPO", tmp_path)
        pkg = tmp_path / "made_up_pkg"
        pkg.mkdir()
        (pkg / "tokenizer.py").write_text(
            '"""Calls shlex.split(s, posix=False) internally."""\n'
            "def f(s):\n"
            "    return []\n"
        )
        population = inv.python_shlex_split_callers(package_dir=pkg)
        assert population == {}, population
