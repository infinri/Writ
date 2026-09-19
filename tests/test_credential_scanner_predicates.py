"""Behavioral tests for SEC-CRYPTO-KEY-001's identifier-based credential
predicate in bin/lib/analyzers-regex.sh (IDENT_ASSIGN / IDENT_ASSIGN_LOWER /
PLACEHOLDER / NAMESPACE_PREFIX).

Written against plan.md
`.claude/plans/2412ba38-51e1-4b73-895b-7b240a3c21d3/plan.md` and its sibling
capabilities.md. This module owns capabilities 1-11 (the predicate's own
behavior). Capabilities 12-14 belong to tests/test_gate_token_leak_guard.py,
tests/test_no_tool_prereqs.py and tests/test_phase3b_approval_rewrap.py,
which are a different phase of this plan and out of this module's scope.
Capabilities 15-17 are operational (a tree-wide diff, a file-count check, a
full-suite run) and are verified by running commands, not by test code.

DRIVE THE REAL SCANNER (ENF-SYS-005): every assertion below runs the real
`bin/run-analysis.sh` as a subprocess against a real file on disk and reads
the JSON findings it prints, exactly as tests/test_prewrite_reconstruction.py
and tests/test_auth_scan_suppression.py already do. No regex object from
bin/lib/analyzers-regex.sh is ever re-declared, copied or imported here --
this repo has already shipped a parity guard that hand-rolled one of the two
paths it compared and stayed green for months while the real path was
broken. Assertions key on the finding's `tool` field
(`writ-crypto-scan/<name>`), never only on the rule id, so a case cannot
pass because a different pattern happened to fire on the same line.

FIXTURE CONSTRUCTION DISCIPLINE (both rules this repo already uses at
tests/test_auth_scan_suppression.py:29-45,84-92): every secret-shaped
fixture VALUE is assembled at runtime from at least two concatenated
fragments, never as one contiguous quoted literal a vendor pattern would
match; and every fixture LINE is assembled the same way
(`ident + op + prefix + q + value + q`), never as a single f-string template
spelling `IDENT = "`. Every module-level constant name below was also
audited by hand to hold none of IDENT_ASSIGN's own keyword substrings
(TOKEN, SECRET, PASSWORD, API_KEY, PRIVATE_KEY, ACCESS_KEY, AUTH_KEY) on its
LEFT-hand side, so this file's own source contains no `<credential-shaped
name> = "<8+ char literal>"` for the very scanner under test -- or any other
scanner in bin/lib/analyzers-regex.sh -- to catch when the pre-write hook
scans this file.

RED PHASE, measured against the unmodified bin/lib/analyzers-regex.sh (see
this cycle's dispatch report for the per-test verdict): the bypass
(TestBarePrefixesStillCaught's prefixed cases) and the two false positives
(TestNamespacePrefixesAdmitted, TestBraceTemplatesAdmitted's plain case) are
live defects and fail today. The vendor-pattern, weak-password and
conditional-exemption classes are regression anchors for properties that
already hold today and must keep holding after the fix (plan.md says so
explicitly for the vendor patterns); they are expected to pass now.
`TestResidualBlindSpot` is the one test in this file with INVERTED polarity:
it pins a gap the fix deliberately, permanently opens, so it is only a real
xfail once NAMESPACE_PREFIX exists. Before the fix it XPASSes, and because
it is `strict=True`, pytest reports that XPASS as a failure -- which is this
one test's correct "fails now" outcome.

Run: .venv/bin/python3 -m pytest tests/test_credential_scanner_predicates.py -v
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
RUN_ANALYSIS = REPO / "bin" / "run-analysis.sh"


# --------------------------------------------------------------------------- #
# Subprocess plumbing, carried over from
# tests/test_prewrite_reconstruction.py:65-112 for the same reason given
# there: the suite's own run command does not put .venv/bin on PATH, and
# bin/run-analysis.sh shells out to a bare `ruff`.
# --------------------------------------------------------------------------- #


def _find_ruff() -> str | None:
    found = shutil.which("ruff")
    if found:
        return found
    venv_ruff = REPO / ".venv" / "bin" / "ruff"
    return str(venv_ruff) if venv_ruff.is_file() else None


RUFF = _find_ruff()


def _env_with_ruff_on_path() -> dict:
    env = dict(os.environ)
    if RUFF is not None:
        ruff_dir = str(Path(RUFF).resolve().parent)
        env["PATH"] = f"{ruff_dir}{os.pathsep}{env.get('PATH', '')}"
    return env


def _run_analysis(project_root: Path, file_path: Path) -> list[dict]:
    """Invoke the REAL bin/run-analysis.sh as a subprocess and parse the JSON
    findings array it prints. Never stubbed, never re-implemented."""
    result = subprocess.run(
        ["bash", str(RUN_ANALYSIS), "--project-root", str(project_root), str(file_path)],
        capture_output=True,
        text=True,
        env=_env_with_ruff_on_path(),
        timeout=30,
    )
    try:
        findings = json.loads(result.stdout)
    except json.JSONDecodeError:
        pytest.fail(
            f"bin/run-analysis.sh did not print a JSON array: stdout={result.stdout!r} "
            f"stderr={result.stderr!r}"
        )
    assert isinstance(findings, list), f"not a list: {findings!r}"
    return findings


def _write_and_analyze(tmp_path: Path, content: str, name: str = "fixture.py") -> list[dict]:
    """Write `content` to a fresh file under `tmp_path` and return the REAL
    findings bin/run-analysis.sh emits for it."""
    target = tmp_path / name
    target.write_text(content)
    return _run_analysis(tmp_path, target)


def _tool_names(findings: list[dict]) -> list[str]:
    return [f.get("tool", "") for f in findings]


def _has_tool(findings: list[dict], tool_suffix: str) -> bool:
    """True when some finding's `tool` field is exactly
    `writ-crypto-scan/<tool_suffix>`."""
    wanted = "writ-crypto-scan/" + tool_suffix
    return any(f.get("tool") == wanted for f in findings)


def _has_rule(findings: list[dict], rule: str) -> bool:
    return any(f.get("rule") == rule for f in findings)


# --------------------------------------------------------------------------- #
# Fixture fragments.
#
# Every secret-shaped value is assembled from at least two concatenated
# pieces so this module's own source text never contains one contiguous run
# that any SEC-CRYPTO-KEY-001 pattern (vendor or identifier) would match.
# Every constant name below was checked by hand against IDENT_ASSIGN's own
# keyword list so none of them, if the fix ever widens that list, becomes a
# credential-shaped left-hand side pointed at one of these literals.
# --------------------------------------------------------------------------- #

_Q = '"'  # the quote character, held in a variable so no fixture line below
          # ever spells `IDENT = "` as one contiguous source literal.

# A realistic non-vendor secret: mixed-case alnum, 18 characters, matching no
# vendor prefix. Mixed case also means it can never fullmatch NAMESPACE_PREFIX
# (lowercase-only), independent of what it ends in.
_STRONG_LITERAL = "aB3dE9" + "fK2mN7" + "pQ5rT1"

# The two identifier forms plan.md's own examples use, held as plain VALUES.
# Neither is ever the left-hand side of a real assignment IN THIS FILE.
_IDENT_ALLCAPS = "API_TOKEN"
_IDENT_LOWER_EQ = "password"
_IDENT_LOWER_COLON = "token"

# The six string prefixes plan.md's bypass covers, plus the bare form.
_STRING_PREFIXES = ["", "f", "r", "b", "rb", "u", "fr"]


def _assign_line(ident: str, op: str, prefix: str, value: str) -> str:
    """Build one `<ident><op><prefix>"<value>"` fixture-file source line.

    `op` carries its own surrounding whitespace (`" = "` or `": "`), matching
    plan.md's own examples (`API_TOKEN = "..."`, `token: r"..."`). Assembled
    from separate concatenation operands -- never one f-string template
    spelling `IDENT = "` -- per this module's fixture-construction discipline.
    """
    return ident + op + prefix + _Q + value + _Q + "\n"


# Vendor secret fragments: each concatenates at least two pieces so no single
# quoted literal in THIS file's source satisfies its own vendor regex.
_STRIPE_LIVE = "sk_" + "live_" + ("A" * 20)
_STRIPE_TEST = "sk_" + "test_" + ("B" * 20)
_AWS_KEY_ID = "AKIA" + "1234567890ABCDEF"
_SLACK_VALUE_PLAIN = "xox" + "b-" + "1234567890123"
_SLACK_VALUE_NAMESPACE_SHAPED = "xox" + "b-" + "1234567890" + "-"
_GITHUB_PAT = "ghp_" + ("C" * 24)
_GITHUB_OAUTH = "gho_" + ("D" * 24)
_PEM_HEADER_BLOCK = "-----BEGIN " + "RSA PRIVATE KEY-----"

_VENDOR_CASES = [
    ("stripe-live", _Q + _STRIPE_LIVE + _Q + "\n"),
    ("stripe-test", _Q + _STRIPE_TEST + _Q + "\n"),
    ("aws-access-key", _Q + _AWS_KEY_ID + _Q + "\n"),
    ("slack-token", _Q + _SLACK_VALUE_PLAIN + _Q + "\n"),
    ("github-pat", _Q + _GITHUB_PAT + _Q + "\n"),
    ("github-oauth", _Q + _GITHUB_OAUTH + _Q + "\n"),
    ("pem-private-key", _PEM_HEADER_BLOCK + "\n"),
]

# Weak-but-real passwords plan.md names as the scanner's core value. Neither
# this file's own left-hand names nor these values are credential-identifier
# shaped by themselves; they only become findings once assigned via
# _assign_line below to one of the two identifiers above.
_WEAK_VALUE_REPEATED = "a" * 8  # "aaaaaaaa"
_WEAK_VALUE_DICTIONARY = "password" + "123456"  # entropy tie w/ the longest namespace prefix
_WEAK_VALUE_SHORT_DICT = "admin" + "1234567"
_WEAK_VALUE_PHRASE = "correct" + "horse" + "battery" + "staple"

# The five refused namespace prefixes plan.md names verbatim.
_NAMESPACE_PREFIXES = [
    "phase3b-",
    "approve-",
    "evidence-",
    "reviewpromote-",
    "advance-from-complete-",
]

# Near-misses: each differs from a genuine namespace prefix in exactly the
# one way capabilities.md calls out, so the exemption's CONDITION (not a
# blanket pass on anything starting "approve") is pinned, not assumed.
_NEAR_MISS_VALUES = [
    "Approve-",       # capital first letter -- NAMESPACE_PREFIX requires [a-z]
    "APPROVE-",       # all caps -- same reason
    "approve_x",      # no trailing separator
    "approve.name-",  # a dot, outside [a-z0-9_-]
    "approve- ",      # trailing space after the separator
]

# Brace-template values plan.md names: the literal bin/lib/test_paths.py:150
# case, and the f-string-built prefix tests/test_decision_memory_capture.py:86
# declares.
_BRACE_TEMPLATE_PLAIN = "{" + "session_cache" + "}"
_BRACE_TEMPLATE_FSTRING = "{" + "_TEST_SCOPE" + "}" + "-"

# --- Review follow-up: the PLACEHOLDER anchoring hole --------------------- #
#
# PLACEHOLDER was consulted with `.match()` against a `^`-anchored pattern, so it
# anchored only the START of the captured value and six characters of decoy
# laundered an arbitrary real secret past it. Reproduced before the fix: both
# `{name}<payload>` and `<placeholder><payload>` were ADMITTED while the same
# payload bare was FLAGGED. The angle half was PRE-EXISTING; the brace half
# arrived with this cycle's fix, by analogy, which is how a live hole got a
# second instance.
#
# Each pair below is one placeholder FORM in two states: pure, which must stay
# admitted, and glued to a payload, which must be refused. Same construction as
# _NEAR_MISS_VALUES: the exemption's CONDITION is pinned, not assumed.

# A pure placeholder of each form. Every one is 8+ characters on purpose -- see
# test_every_admitted_case_clears_the_eight_char_floor, which exists so none of
# these can pass because of the length floor instead of the exemption.
_ANGLE_TEMPLATE_PLAIN = "<" + "placeholder" + ">"
_WORD_VALUE_YOUR = "your-" + "api-key-here"
_WORD_VALUE_TEST = "test-" + "mode-engine"
_WORD_VALUE_EXAMPLE = "example" + "-value-here"

_PLACEHOLDER_ADMITTED_CASES = [
    ("brace", _BRACE_TEMPLATE_PLAIN),
    ("brace-with-trailing-separator", _BRACE_TEMPLATE_FSTRING),
    ("angle", _ANGLE_TEMPLATE_PLAIN),
    ("word-your", _WORD_VALUE_YOUR),
    ("word-test", _WORD_VALUE_TEST),
    ("word-example", _WORD_VALUE_EXAMPLE),
]

# The same forms with a real mixed-case secret glued on. _STRONG_LITERAL is
# mixed-case, so it lies outside the placeholder tail alphabet in every case.
_PLACEHOLDER_LAUNDERED_CASES = [
    ("brace", "{" + "name" + "}" + _STRONG_LITERAL),
    ("angle", "<" + "placeholder" + ">" + _STRONG_LITERAL),
    ("word-your", "your-" + _STRONG_LITERAL),
    ("word-test", "test" + _STRONG_LITERAL),
    ("word-example", "example" + _STRONG_LITERAL),
]

# The disclosed blind spot: lowercase/hyphen, ends in a separator, no vendor
# prefix -- exactly the NAMESPACE_PREFIX shape, on a genuinely secret-shaped
# value.
_BLIND_SPOT_VALUE = "correct" + "-horse-" + "battery-"


# --------------------------------------------------------------------------- #
# Capabilities 1 & 2
# --------------------------------------------------------------------------- #


class TestBarePrefixesStillCaught:
    """A credential-named identifier assigned a non-vendor secret literal is
    reported whether the literal is bare or carries a string prefix, in both
    the ALL-CAPS and lowercase identifier forms (`=` and `:`)."""

    @pytest.mark.parametrize("prefix", _STRING_PREFIXES)
    def test_allcaps_identifier_reports_for_every_prefix(self, tmp_path, prefix):
        content = _assign_line(_IDENT_ALLCAPS, " = ", prefix, _STRONG_LITERAL)
        findings = _write_and_analyze(tmp_path, content)
        assert _has_tool(findings, "credential-literal-assign"), (
            f"prefix {prefix!r} on {_IDENT_ALLCAPS} must still report "
            f"credential-literal-assign; got tools={_tool_names(findings)!r}"
        )

    @pytest.mark.parametrize("prefix", _STRING_PREFIXES)
    def test_lowercase_identifier_eq_reports_for_every_prefix(self, tmp_path, prefix):
        content = _assign_line(_IDENT_LOWER_EQ, " = ", prefix, _STRONG_LITERAL)
        findings = _write_and_analyze(tmp_path, content)
        assert _has_tool(findings, "credential-literal-assign"), (
            f"prefix {prefix!r} on {_IDENT_LOWER_EQ!r} (=) must still report; "
            f"got tools={_tool_names(findings)!r}"
        )

    @pytest.mark.parametrize("prefix", _STRING_PREFIXES)
    def test_lowercase_identifier_colon_reports_for_every_prefix(self, tmp_path, prefix):
        content = _assign_line(_IDENT_LOWER_COLON, ": ", prefix, _STRONG_LITERAL)
        findings = _write_and_analyze(tmp_path, content)
        assert _has_tool(findings, "credential-literal-assign"), (
            f"prefix {prefix!r} on {_IDENT_LOWER_COLON!r} (:) must still report; "
            f"got tools={_tool_names(findings)!r}"
        )


# --------------------------------------------------------------------------- #
# Capability 3
# --------------------------------------------------------------------------- #


class TestVendorPatternsPinned:
    """Each of the seven vendor patterns still reports under its own tool
    name. Seven, not five: sk_test_ and gho_ each have their own row in
    SECRET_PATTERNS (plan.md's correction to the exploration findings)."""

    @pytest.mark.parametrize("tool_name, content", _VENDOR_CASES)
    def test_vendor_pattern_reports_under_its_own_tool_name(self, tmp_path, tool_name, content):
        findings = _write_and_analyze(tmp_path, content)
        assert _has_tool(findings, tool_name), (
            f"expected writ-crypto-scan/{tool_name}; got tools={_tool_names(findings)!r}"
        )

    def test_all_seven_vendor_tool_names_are_distinct(self):
        names = [name for name, _ in _VENDOR_CASES]
        assert len(names) == 7, f"expected seven vendor cases, got {len(names)}: {names!r}"
        assert len(set(names)) == 7, f"vendor tool names must be distinct: {names!r}"


# --------------------------------------------------------------------------- #
# Capability 4
# --------------------------------------------------------------------------- #


class TestVendorWinsOverShapeExemption:
    """A literal that is BOTH vendor-shaped and namespace-shaped is still
    reported under the vendor tool name: the vendor loop is unconditional and
    untouched by the identifier-branch exemption."""

    def test_slack_token_ending_in_separator_still_reports_as_slack_token(self, tmp_path):
        content = _assign_line("SLACK_TOKEN", " = ", "", _SLACK_VALUE_NAMESPACE_SHAPED)
        findings = _write_and_analyze(tmp_path, content)
        assert _has_tool(findings, "slack-token"), (
            f"a slack-shaped value ending in '-' must still report as "
            f"writ-crypto-scan/slack-token even though it is also "
            f"namespace-shaped; got tools={_tool_names(findings)!r}"
        )


# --------------------------------------------------------------------------- #
# Capability 5
# --------------------------------------------------------------------------- #


class TestNamespacePrefixesAdmitted:
    """Each of the five refused namespace prefixes assigned to
    GATE_TOKEN_SESSION_PREFIX produces no SEC-CRYPTO-KEY-001 finding."""

    @pytest.mark.parametrize("value", _NAMESPACE_PREFIXES)
    def test_namespace_prefix_produces_no_finding(self, tmp_path, value):
        content = _assign_line("GATE_TOKEN_SESSION_PREFIX", " = ", "", value)
        findings = _write_and_analyze(tmp_path, content)
        assert not _has_rule(findings, "SEC-CRYPTO-KEY-001"), (
            f"namespace prefix {value!r} must not produce a SEC-CRYPTO-KEY-001 "
            f"finding; got {findings!r}"
        )


# --------------------------------------------------------------------------- #
# Capability 6
# --------------------------------------------------------------------------- #


class TestBraceTemplatesAdmitted:
    """A brace-template value assigned to a TOKEN-named constant produces no
    SEC-CRYPTO-KEY-001 finding: the bin/lib/test_paths.py:150 case, and the
    tests/test_decision_memory_capture.py:86 f-string-prefix case."""

    def test_plain_brace_template_produces_no_finding(self, tmp_path):
        content = _assign_line("_SESSION_CACHE_TOKEN", " = ", "", _BRACE_TEMPLATE_PLAIN)
        findings = _write_and_analyze(tmp_path, content)
        assert not _has_rule(findings, "SEC-CRYPTO-KEY-001"), (
            f"a bare brace template must not report; got {findings!r}"
        )

    def test_fstring_prefixed_brace_template_produces_no_finding(self, tmp_path):
        content = _assign_line("GATE_TOKEN_SESSION_PREFIX", " = ", "f", _BRACE_TEMPLATE_FSTRING)
        findings = _write_and_analyze(tmp_path, content)
        assert not _has_rule(findings, "SEC-CRYPTO-KEY-001"), (
            f"an f-string brace template ending in '-' must not report; "
            f"got {findings!r}"
        )


# --------------------------------------------------------------------------- #
# Capability 7
# --------------------------------------------------------------------------- #


class TestWeakPasswordsStillCaught:
    """The adversarial weak passwords are still reported -- the scanner's
    core value, which an over-wide namespace exemption would gut."""

    @pytest.mark.parametrize(
        "value",
        [
            _WEAK_VALUE_REPEATED,
            _WEAK_VALUE_DICTIONARY,
            _WEAK_VALUE_SHORT_DICT,
            _WEAK_VALUE_PHRASE,
        ],
    )
    def test_weak_password_still_reports(self, tmp_path, value):
        content = _assign_line(_IDENT_ALLCAPS, " = ", "", value)
        findings = _write_and_analyze(tmp_path, content)
        assert _has_tool(findings, "credential-literal-assign"), (
            f"weak password {value!r} must still report; "
            f"got tools={_tool_names(findings)!r}"
        )


# --------------------------------------------------------------------------- #
# Capability 8
# --------------------------------------------------------------------------- #


class TestNamespaceExemptionIsConditional:
    """The exemption is a whole-value SHAPE test, not a blanket pass on
    anything that merely starts with "approve": each near-miss differs from a
    genuine namespace prefix in exactly one property and must still be
    reported."""

    @pytest.mark.parametrize("value", _NEAR_MISS_VALUES)
    def test_near_miss_value_still_reports(self, tmp_path, value):
        content = _assign_line("GATE_TOKEN_SESSION_PREFIX", " = ", "", value)
        findings = _write_and_analyze(tmp_path, content)
        assert _has_tool(findings, "credential-literal-assign"), (
            f"near-miss {value!r} must still report (not namespace-exempt); "
            f"got tools={_tool_names(findings)!r}"
        )


# --------------------------------------------------------------------------- #
# Capability 9
# --------------------------------------------------------------------------- #


class TestExemptionReadsCapturedValueOnly:
    """NAMESPACE_PREFIX is checked against m.group(1) -- the literal between
    the quotes -- and nothing else on the line. A real secret followed by a
    trailing comment that contains a namespace-shaped word must still be
    reported."""

    def test_trailing_comment_containing_namespace_word_does_not_suppress(self, tmp_path):
        line = _assign_line(_IDENT_ALLCAPS, " = ", "", _STRONG_LITERAL).rstrip("\n")
        content = line + "  # " + "approve-" + " session\n"
        findings = _write_and_analyze(tmp_path, content)
        assert _has_tool(findings, "credential-literal-assign"), (
            f"a real secret must still report even with a trailing comment "
            f"mentioning a namespace word; got tools={_tool_names(findings)!r}"
        )


# --------------------------------------------------------------------------- #
# Capability 10
# --------------------------------------------------------------------------- #


class TestUnchangedBoundaries:
    """The 8-character floor and comment-skipping are untouched by this
    change."""

    def test_literal_under_eight_chars_produces_no_finding(self, tmp_path):
        content = _assign_line(_IDENT_ALLCAPS, " = ", "", "vp-")
        findings = _write_and_analyze(tmp_path, content)
        assert not _has_rule(findings, "SEC-CRYPTO-KEY-001"), (
            f"a 3-character literal must stay below the 8-char floor; "
            f"got {findings!r}"
        )

    def test_commented_out_assignment_produces_no_finding(self, tmp_path):
        content = "# " + _assign_line(_IDENT_ALLCAPS, " = ", "", _STRONG_LITERAL)
        findings = _write_and_analyze(tmp_path, content)
        assert not _has_rule(findings, "SEC-CRYPTO-KEY-001"), (
            f"a commented-out credential assignment must not report; "
            f"got {findings!r}"
        )


# --------------------------------------------------------------------------- #
# Capability 11
# --------------------------------------------------------------------------- #


class TestResidualBlindSpot:
    """The disclosed blind spot -- a real secret hand-crafted as
    `word-word-` -- is pinned as a STRICT xfail so it reddens the day it is
    ever closed. See the module docstring's RED PHASE note for why this one
    test's polarity is inverted relative to every other test in this file."""

    @pytest.mark.xfail(
        strict=True,
        reason=(
            "ACCEPTED residual blind spot (plan.md 'The residual blind spot, "
            "stated in the direction it fails'): a real secret whose entire "
            "value is lowercase/digits/hyphen/underscore and ends in a bare "
            "separator is indistinguishable, by shape alone, from a "
            "namespace prefix, and NAMESPACE_PREFIX exempts it. strict=True "
            "on purpose: if this ever starts passing, the blind spot has "
            "been closed by some other mechanism and this marker must be "
            "removed, not widened."
        ),
    )
    def test_blind_spot_value_is_reported(self, tmp_path):
        content = _assign_line(_IDENT_ALLCAPS, " = ", "", _BLIND_SPOT_VALUE)
        findings = _write_and_analyze(tmp_path, content)
        assert _has_tool(findings, "credential-literal-assign"), (
            f"blind-spot value {_BLIND_SPOT_VALUE!r} is not reported (this IS "
            f"the accepted gap); got tools={_tool_names(findings)!r}"
        )


# --------------------------------------------------------------------------- #
# Review follow-up: PLACEHOLDER is a WHOLE-VALUE judgment
#
# Added after review reproduced a CRITICAL against the first cut of this cycle:
# PLACEHOLDER was consulted with `.match()`, which anchors only the START of the
# captured value, so a decoy prefix laundered an arbitrary real secret at any
# length, any case, any entropy. TestBraceTemplatesAdmitted above could not see
# it, because it only ever asserts that a PURE placeholder is admitted -- the
# admitted half of a two-sided property. These are the refused half.
# --------------------------------------------------------------------------- #


class TestPlaceholderExemptionIsWholeValue:
    """A placeholder must be a placeholder all the way through, not a
    placeholder glued to a payload. Each form is pinned in both directions:
    the pure value stays admitted, the same form plus a real secret is
    reported."""

    @pytest.mark.parametrize("label, value", _PLACEHOLDER_ADMITTED_CASES)
    def test_pure_placeholder_produces_no_finding(self, tmp_path, label, value):
        content = _assign_line(_IDENT_ALLCAPS, " = ", "", value)
        findings = _write_and_analyze(tmp_path, content)
        assert not _has_rule(findings, "SEC-CRYPTO-KEY-001"), (
            f"pure placeholder {value!r} ({label}) must stay admitted; "
            f"got {findings!r}"
        )

    @pytest.mark.parametrize("label, value", _PLACEHOLDER_LAUNDERED_CASES)
    def test_placeholder_glued_to_a_payload_still_reports(self, tmp_path, label, value):
        content = _assign_line(_IDENT_ALLCAPS, " = ", "", value)
        findings = _write_and_analyze(tmp_path, content)
        assert _has_tool(findings, "credential-literal-assign"), (
            f"a real secret laundered behind a {label} placeholder decoy "
            f"({value!r}) must be reported; got tools={_tool_names(findings)!r}"
        )

    @pytest.mark.parametrize("label, value", _PLACEHOLDER_ADMITTED_CASES)
    def test_every_admitted_case_clears_the_eight_char_floor(self, label, value):
        """Kills the way this class could pass for the wrong reason: a pure
        placeholder shorter than eight characters would produce no finding
        because of IDENT_ASSIGN's length floor, proving nothing about the
        exemption."""
        assert len(value) >= 8, (
            f"admitted case {label} is {value!r}, only {len(value)} characters, "
            f"so it never reaches the exemption at all"
        )

    def test_the_reproduced_decoy_prefix_is_reported(self, tmp_path):
        """The exact shape review reproduced against the first cut: a brace
        decoy of six characters in front of a real value."""
        value = "{" + "name" + "}" + _STRONG_LITERAL + "_extra_real_value_appended"
        content = _assign_line(_IDENT_ALLCAPS, " = ", "", value)
        findings = _write_and_analyze(tmp_path, content)
        assert _has_tool(findings, "credential-literal-assign"), (
            f"the reproduced decoy-prefix laundering shape {value!r} must be "
            f"reported; got tools={_tool_names(findings)!r}"
        )
