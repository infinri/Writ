"""One funnel for every hook reply (plan.md dfacff61-23d5-474e-846c-2e2f0f0ea482).

THE PROPERTY THIS FILE OWNS: an OUT capture row must contain the bytes actually
written to stdout, never a reconstruction of them. The live defect is
writ-pre-write-dispatch.sh:297, which logs the rule-prose ingredients instead of
the additionalContext envelope printed at lines 290 to 296. Every byte-identity
assertion below compares a captured row against the subprocess's own captured
stdout, never against a value this test reconstructs by hand.

SEAM CONTRACT for the implementer, none of which exists yet:

  bin/lib/common.sh::blackbox_enabled
      A predicate, no output, exit 0 when capture is on (WRIT_BLACKBOX=1 or the
      sentinel file $HOME/.claude/writ-blackbox.on exists), exit non-zero otherwise.

  bin/lib/common.sh::emit_hook_reply <payload> [hook] [session]
      Prints payload verbatim to stdout (nothing at all for an empty payload), then,
      only when blackbox_enabled, records a direction: out row through blackbox_log
      carrying the identical payload string. hook defaults to the outermost caller's
      basename (BASH_SOURCE's last frame, so a call routed through emit_deny is
      labelled with the ORIGINAL hook, never "common"); session defaults to
      ${HOOK_SESSION_ID:-}.

  tests/_inventory.py::direct_blackbox_out_calls() -> list[str]
      Hook scripts under hooks/scripts/ whose comment-stripped source still calls
      `blackbox_log out` directly (CHECK 1, plan.md Decision 2). Must be empty.

  tests/_inventory.py::envelope_emitting_scripts(*, scripts_dir: Path = HOOK_SCRIPTS_DIR)
      Hook scripts whose comment-stripped source contains "hookSpecificOutput" or
      calls emit_deny/emit_ask (CHECK 2). Must be non-empty, and every member must
      also reach the funnel (emit_hook_reply, emit_deny or emit_ask somewhere in its
      own source).

  tests/_inventory.py::bare_envelope_emissions(*, scripts_dir: Path = HOOK_SCRIPTS_DIR)
      -> list[tuple[str, int]] of (script, line) findings (CHECK 3, the heuristic).
      The scripts_dir keyword is the seam this file's precision fixtures need: it
      lets the scanner's matching be pinned against synthetic shapes in a tmp
      directory instead of being described only against today's live tree, the same
      parameterization writ/hooks_lint.py::lint_hooks already uses for plugin_root.

Everything above is imported or called exactly as named; a test that needs a symbol
which does not exist yet fails loudly (pytest.fail with "skeleton: ...") rather than
skipping, per ENF-GATE-007.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from tests._strace import trace_execve_result

REPO = Path(__file__).resolve().parent.parent
COMMON_SH = REPO / "bin" / "lib" / "common.sh"
HOOKS_DIR = REPO / "hooks" / "scripts"

# A minimal hookSpecificOutput envelope, used everywhere the exact content does not
# matter, only that it round-trips byte for byte. No single quotes, no `$`, no
# backticks: it is embedded inside a single-quoted shell argument below, where bash
# performs no interpolation at all, so any of those would change what the child
# process actually receives.
PAYLOAD = json.dumps({
    "hookSpecificOutput": {
        "hookEventName": "PreToolUse",
        "permissionDecision": "allow",
        "additionalContext": "example context for the funnel byte identity test",
    },
})


def _run_bash(snippet: str, *, home: Path, extra_env: dict | None = None):
    """Source common.sh, then run snippet, exactly the direct bash -c exercise the
    plan's Testing shape section calls for (style of tests/test_debug_gating.py)."""
    env = os.environ.copy()
    env["HOME"] = str(home)
    env.pop("WRIT_BLACKBOX", None)
    env["WRIT_NO_AUTOSTART"] = "1"
    if extra_env:
        env.update(extra_env)
    command = f'source "{COMMON_SH}" && {snippet}'
    return subprocess.run(
        ["bash", "-c", command], capture_output=True, text=True, env=env, timeout=30,
    )


def _inventory():
    try:
        import tests._inventory as inventory
    except ImportError as exc:
        pytest.fail(f"skeleton: tests/_inventory.py failed to import: {exc}")
    return inventory


def _require(module, *names) -> None:
    missing = [n for n in names if not hasattr(module, n)]
    if missing:
        pytest.fail(f"skeleton: {module.__name__} has no {', '.join(missing)} yet")


# --------------------------------------------------------------------------------- #
# blackbox_enabled: the one shared predicate (Refinement One)
# --------------------------------------------------------------------------------- #


class TestBlackboxEnabledPredicate:
    """Refinement One (plan.md Decision 1): one predicate shared by load_hook_env,
    blackbox_log and the funnel, so a future caller that forgets to test capture
    still no-ops instead of forking a subshell to find out.

    Every assertion below is on blackbox_enabled's OWN exit status, never on
    echoed text. `foo && echo YES || echo NO` swallows a "command not found"
    (bash exit 127) into the `||` arm exactly the way a real `false` (exit 1)
    would, so an echo-text assertion cannot tell "the predicate returned false"
    from "the predicate does not exist": both print NO and both leave the
    overall pipeline at exit 0. `declare -F` is asserted FIRST and separately
    (exit 0 only when the function is actually defined), so a missing function
    fails loudly there instead of reading as a passing false.
    """

    def _assert_defined(self, home: Path) -> None:
        exists = _run_bash("declare -F blackbox_enabled", home=home)
        assert exists.returncode == 0, (
            "blackbox_enabled is not defined (declare -F failed); calling it "
            f"below would be indistinguishable from a real false: "
            f"stderr={exists.stderr!r}"
        )

    def test_true_when_the_sentinel_file_exists(self, tmp_path) -> None:
        home = tmp_path / "home"
        (home / ".claude").mkdir(parents=True)
        (home / ".claude" / "writ-blackbox.on").write_text("on")
        self._assert_defined(home)
        result = _run_bash("blackbox_enabled", home=home)
        assert result.stderr == "", (
            f"blackbox_enabled wrote to stderr: {result.stderr!r}"
        )
        assert result.returncode == 0, (
            f"expected exit 0 (true) with the sentinel present, got "
            f"{result.returncode}"
        )

    def test_true_when_writ_blackbox_env_var_is_1(self, tmp_path) -> None:
        home = tmp_path / "home"
        home.mkdir()
        self._assert_defined(home)
        result = _run_bash(
            "blackbox_enabled", home=home, extra_env={"WRIT_BLACKBOX": "1"},
        )
        assert result.stderr == "", (
            f"blackbox_enabled wrote to stderr: {result.stderr!r}"
        )
        assert result.returncode == 0, (
            f"expected exit 0 (true) with WRIT_BLACKBOX=1, got {result.returncode}"
        )

    def test_false_with_neither_signal(self, tmp_path) -> None:
        home = tmp_path / "home"
        home.mkdir()
        self._assert_defined(home)
        result = _run_bash("blackbox_enabled", home=home)
        assert result.stderr == "", (
            "blackbox_enabled wrote to stderr on the false path; a \"command "
            "not found\" message here is the positive signal that a pass would "
            f"be spurious: {result.stderr!r}"
        )
        assert result.returncode == 1, (
            f"expected exit 1 (false) with neither signal set, got "
            f"{result.returncode} (127 would mean the function does not exist, "
            "which _assert_defined above should already have caught)"
        )


# --------------------------------------------------------------------------------- #
# emit_hook_reply: stdout, capture on, capture off, empty payload, unwritable log
# --------------------------------------------------------------------------------- #


class TestEmitHookReplyStdout:
    def test_writes_the_payload_verbatim_to_stdout(self, tmp_path) -> None:
        home = tmp_path / "home"
        home.mkdir()
        result = _run_bash(f"emit_hook_reply '{PAYLOAD}'", home=home)
        assert result.returncode == 0, result.stderr
        assert result.stdout == PAYLOAD + "\n", (
            f"stdout {result.stdout!r} is not the payload verbatim"
        )

    def test_empty_payload_produces_no_stdout(self, tmp_path) -> None:
        home = tmp_path / "home"
        home.mkdir()
        result = _run_bash('emit_hook_reply ""', home=home)
        assert result.returncode == 0, result.stderr
        assert result.stdout == ""


class TestEmitHookReplyCaptureOff:
    """Capture disabled: no sentinel, no WRIT_BLACKBOX. Same stdout, no log file,
    no row, and (proven separately below) no process spawned at all."""

    def test_no_log_file_is_created_and_stdout_is_unchanged(self, tmp_path) -> None:
        home = tmp_path / "home"
        home.mkdir()
        log = tmp_path / "should-not-exist.jsonl"
        result = _run_bash(
            f"emit_hook_reply '{PAYLOAD}' myhook sess-1", home=home,
            extra_env={"WRIT_BLACKBOX_LOG": str(log)},
        )
        assert result.returncode == 0, result.stderr
        assert result.stdout == PAYLOAD + "\n"
        assert not log.exists(), "capture was disabled but a log file appeared anyway"

    def test_the_funnel_adds_no_process_when_capture_is_off(self, tmp_path) -> None:
        """Refinement One: with capture off the cost is one variable test plus one
        [ -f ] stat, not a fork. THE PROPERTY IS DIFFERENTIAL, not an absolute
        floor of zero: strace always records at least the execve of the program
        it launches, and common.sh's own source-time `dirname` fork predates
        this cycle, so `bash -c 'source common.sh'` alone never traces to 0
        (measured: 2, on this machine). Asserting `== 0` is unreachable and
        would never pass even after a correct implementation.

        So this counts a BASELINE (source common.sh, no funnel call) against a
        MEASURED run (source common.sh plus one emit_hook_reply call) under the
        identical harness, HOME and env, and asserts the two execve counts are
        EQUAL. That fails the moment the funnel forks anything extra on the off
        path, independent of strace's own floor or of how many forks common.sh
        happens to make today.
        """
        if shutil.which("strace") is None:
            pytest.skip("strace unavailable; cannot count execve")
        home = tmp_path / "home"
        home.mkdir()
        env = os.environ.copy()
        env["HOME"] = str(home)
        env.pop("WRIT_BLACKBOX", None)

        def _traced(command: str):
            result, text = trace_execve_result(
                ["bash", "-c", command], env=env, timeout=30,
            )
            return result, text.count("execve(")

        baseline_result, baseline_count = _traced(f'source "{COMMON_SH}"')
        measured_result, measured_count = _traced(
            f"source \"{COMMON_SH}\" && emit_hook_reply '{PAYLOAD}' myhook sess-1",
        )

        # ANTI-VACUITY 1: a broken strace invocation (missing binary, permission
        # failure, wrong path) would leave BOTH counts at 0, which are equal
        # while measuring nothing.
        assert baseline_count > 0, (
            f"baseline execve count is zero; strace measured nothing "
            f"(stderr={baseline_result.stderr!r})"
        )
        # ANTI-VACUITY 2: a typo'd function name adds no fork either, which
        # would also leave the counts equal for free. That is the same
        # absent-reads-as-working shape already fixed twice in this file.
        # Confirm the funnel actually ran by checking its own stdout.
        assert measured_result.stdout == PAYLOAD + "\n", (
            "emit_hook_reply did not actually run in the measured case (its "
            f"stdout does not match the payload it should have printed): "
            f"{measured_result.stdout!r}"
        )

        assert measured_count == baseline_count, (
            f"capture is disabled but the funnel still spawned a process: "
            f"baseline execve count={baseline_count}, "
            f"measured execve count={measured_count}"
        )


class TestEmitHookReplyCaptureOn:
    def test_records_a_row_byte_identical_to_stdout(self, tmp_path) -> None:
        home = tmp_path / "home"
        (home / ".claude").mkdir(parents=True)
        (home / ".claude" / "writ-blackbox.on").write_text("on")
        log = home / ".claude" / "writ-blackbox.jsonl"
        result = _run_bash(f"emit_hook_reply '{PAYLOAD}' myhook sess-1", home=home)
        assert result.returncode == 0, result.stderr
        assert result.stdout == PAYLOAD + "\n"
        assert log.exists(), "capture was enabled but no log file was created"
        rows = [json.loads(ln) for ln in log.read_text().splitlines() if ln.strip()]
        assert len(rows) == 1, rows
        row = rows[0]
        assert row["direction"] == "out"
        assert row["hook"] == "myhook"
        assert row["session"] == "sess-1"
        assert row["payload"] == result.stdout, (
            "the captured payload is not byte-identical to what emit_hook_reply "
            f"actually wrote to stdout: captured={row['payload']!r} "
            f"stdout={result.stdout!r}"
        )

    def test_an_empty_payload_creates_no_row_even_with_capture_enabled(
        self, tmp_path
    ) -> None:
        home = tmp_path / "home"
        (home / ".claude").mkdir(parents=True)
        (home / ".claude" / "writ-blackbox.on").write_text("on")
        log = home / ".claude" / "writ-blackbox.jsonl"
        result = _run_bash('emit_hook_reply ""', home=home)
        assert result.returncode == 0, result.stderr
        assert result.stdout == ""
        assert not log.exists(), "an empty payload produced a capture row"


class TestEmitHookReplyUnwritableLog:
    def test_stdout_matches_the_writable_case_and_exit_is_zero(self, tmp_path) -> None:
        if hasattr(os, "geteuid") and os.geteuid() == 0:
            pytest.skip("running as root; a read-only directory would not block root")

        home_writable = tmp_path / "home-writable"
        (home_writable / ".claude").mkdir(parents=True)
        (home_writable / ".claude" / "writ-blackbox.on").write_text("on")
        baseline = _run_bash(
            f"emit_hook_reply '{PAYLOAD}' myhook sess-1", home=home_writable,
        )
        assert baseline.returncode == 0, baseline.stderr

        home = tmp_path / "home-unwritable"
        (home / ".claude").mkdir(parents=True)
        (home / ".claude" / "writ-blackbox.on").write_text("on")
        readonly_dir = tmp_path / "readonly"
        readonly_dir.mkdir()
        log = readonly_dir / "writ-blackbox.jsonl"
        readonly_dir.chmod(0o500)
        try:
            result = _run_bash(
                f"emit_hook_reply '{PAYLOAD}' myhook sess-1", home=home,
                extra_env={"WRIT_BLACKBOX_LOG": str(log)},
            )
        finally:
            readonly_dir.chmod(0o700)

        assert result.returncode == 0, result.stderr
        assert result.stdout == baseline.stdout, (
            "an unwritable capture log path changed the hook's own stdout"
        )
        assert not log.exists()


class TestWritePathRatchetHoldsWithCaptureOff:
    """Capability 3, satisfied by importing the existing ratchet's own constants
    and measurement functions rather than restating a number: this file must never
    hold its own copy of PYTHON_BUDGET, TOTAL_BUDGET or REAL_PROCESS_BUDGET."""

    def test_the_write_path_process_budgets_still_hold_with_capture_off(self) -> None:
        if shutil.which("strace") is None:
            pytest.skip("strace unavailable; cannot count execve")
        from tests.test_write_path_process_budget import (
            HOOKS as WRITE_PATH_HOOKS_DIR,
            PYTHON_BUDGET,
            REAL_PROCESS_BUDGET,
            TOTAL_BUDGET,
            WRITE_PATH_HOOKS,
            _counts,
            _real_processes,
        )

        python_total = 0
        overall_total = 0
        real_total = 0
        for hook in WRITE_PATH_HOOKS:
            if not (WRITE_PATH_HOOKS_DIR / hook).exists():
                continue
            python, total = _counts(hook)
            python_total += python
            overall_total += total
            real_total += _real_processes(hook)

        assert python_total <= PYTHON_BUDGET, (
            f"capture-off write path spawned {python_total} python interpreters, "
            f"more than the existing ratchet of {PYTHON_BUDGET}"
        )
        assert overall_total <= TOTAL_BUDGET, (
            f"capture-off write path spawned {overall_total} processes, more than "
            f"the existing ratchet of {TOTAL_BUDGET}"
        )
        assert real_total <= REAL_PROCESS_BUDGET, (
            f"capture-off write path spawned {real_total} real processes, more "
            f"than the existing ratchet of {REAL_PROCESS_BUDGET}"
        )


# --------------------------------------------------------------------------------- #
# emit_deny / emit_ask: unchanged envelope bytes, correctly labelled row
# --------------------------------------------------------------------------------- #


TRICKY_REASON = (
    'reason with "double quotes" and\n'
    "an embedded newline, plus a $(dangerous) substitution attempt and a `backtick`"
)


class TestEmitDenyAskByteIdentity:
    """Capability: emit_deny and emit_ask must keep emitting the exact pre-change
    envelope for the same reason string, including one containing double quotes
    and newlines (the encoding argument the comment above emit_ask already makes:
    the reason rides an env var into python so it cannot forge the envelope)."""

    def test_emit_deny_matches_the_pre_change_envelope(self, tmp_path) -> None:
        home = tmp_path / "home"
        home.mkdir()
        result = _run_bash(
            'emit_deny "$TRICKY_REASON"', home=home,
            extra_env={"TRICKY_REASON": TRICKY_REASON},
        )
        assert result.returncode == 0, result.stderr
        expected = json.dumps({
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": TRICKY_REASON,
            },
        })
        assert result.stdout == expected + "\n", (
            f"emit_deny's envelope changed: got {result.stdout!r}, expected "
            f"{expected + chr(10)!r}"
        )

    def test_emit_ask_matches_the_pre_change_envelope(self, tmp_path) -> None:
        home = tmp_path / "home"
        home.mkdir()
        result = _run_bash(
            'emit_ask "$TRICKY_REASON"', home=home,
            extra_env={"TRICKY_REASON": TRICKY_REASON},
        )
        assert result.returncode == 0, result.stderr
        expected = json.dumps({
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "ask",
                "permissionDecisionReason": TRICKY_REASON,
            },
        })
        assert result.stdout == expected + "\n", (
            f"emit_ask's envelope changed: got {result.stdout!r}, expected "
            f"{expected + chr(10)!r}"
        )


class TestEmitDenyRecordedHookLabel:
    """Refinement Two (plan.md Decision 1): the hook label resolves the OUTERMOST
    BASH_SOURCE frame, so a row recorded through emit_deny carries the CALLING
    script's basename, never "common" (the frame above the funnel inside
    emit_deny is common.sh itself)."""

    def test_a_row_recorded_through_emit_deny_carries_the_calling_hooks_basename(
        self, tmp_path
    ) -> None:
        home = tmp_path / "home"
        (home / ".claude").mkdir(parents=True)
        (home / ".claude" / "writ-blackbox.on").write_text("on")
        log = home / ".claude" / "writ-blackbox.jsonl"
        fake_hook = tmp_path / "fake-caller-hook.sh"
        fake_hook.write_text(
            "#!/bin/bash\n"
            f'source "{COMMON_SH}"\n'
            'emit_deny "denied for testing, ask the user to retry"\n'
        )
        env = os.environ.copy()
        env["HOME"] = str(home)
        env["WRIT_NO_AUTOSTART"] = "1"
        result = subprocess.run(
            ["bash", str(fake_hook)], capture_output=True, text=True, env=env,
            timeout=30,
        )
        assert result.returncode == 0, result.stderr
        assert log.exists(), "capture was enabled but emit_deny recorded no row"
        rows = [json.loads(ln) for ln in log.read_text().splitlines() if ln.strip()]
        assert rows, "the capture log exists but holds no parsed rows"
        assert rows[-1]["hook"] == "fake-caller-hook", (
            f"emit_deny recorded hook={rows[-1]['hook']!r}; expected the calling "
            "script's own basename, not common.sh's name"
        )


# --------------------------------------------------------------------------------- #
# CHECK 1: no direct blackbox_log out calls under hooks/scripts/
# --------------------------------------------------------------------------------- #


class TestNoDirectBlackboxOutCalls:
    """CHECK 1 (plan.md Decision 2), the exact guard with no false-positive
    surface: only emit_hook_reply in bin/lib/common.sh may write an OUT row, so a
    hook script that cannot call the logger cannot log something other than what
    it sent."""

    def test_direct_blackbox_out_calls_population_is_empty(self) -> None:
        inventory = _inventory()
        _require(inventory, "direct_blackbox_out_calls")
        offenders = inventory.direct_blackbox_out_calls()
        assert offenders == [], (
            f"hook scripts still call blackbox_log out directly, bypassing the "
            f"funnel: {offenders}"
        )


# --------------------------------------------------------------------------------- #
# CHECK 2: envelope-emitting scripts, non-empty, every member reaches the funnel
# --------------------------------------------------------------------------------- #


class TestEnvelopeEmittingScriptsCallTheFunnel:
    """CHECK 2 (plan.md Decision 2), the anti-vacuity guard: a scanner that
    silently matched nothing would make every dependent assertion pass on any
    tree, so the population is asserted non-empty in its own right here (it also
    joins the parametrized non-empty list in test_count_pin_discipline.py)."""

    def test_the_population_is_non_empty(self) -> None:
        inventory = _inventory()
        _require(inventory, "envelope_emitting_scripts")
        scripts = inventory.envelope_emitting_scripts()
        assert scripts, (
            "envelope_emitting_scripts() derived nothing; a scanner that silently "
            "stopped matching would read as full compliance"
        )

    def test_every_member_also_reaches_the_funnel(self) -> None:
        inventory = _inventory()
        _require(inventory, "envelope_emitting_scripts")
        scripts = inventory.envelope_emitting_scripts()
        assert scripts, "precondition: the population must be non-empty to test anything"
        funnel_markers = ("emit_hook_reply", "emit_deny", "emit_ask")
        missing = []
        for name in scripts:
            text = (HOOKS_DIR / name).read_text(encoding="utf-8", errors="replace")
            stripped = "\n".join(
                ln for ln in text.splitlines() if not ln.lstrip().startswith("#")
            )
            if not any(marker in stripped for marker in funnel_markers):
                missing.append(name)
        assert missing == [], (
            "scripts that build a hookSpecificOutput envelope but never reach the "
            f"funnel (emit_hook_reply, emit_deny or emit_ask): {missing}"
        )


# --------------------------------------------------------------------------------- #
# CHECK 3: bare envelope emissions, real tree plus synthetic-fixture precision
# --------------------------------------------------------------------------------- #


class TestBareEnvelopeEmissionsOnTheRealTree:
    """CHECK 3 (plan.md Decision 2) applied to the tree as it stands once every
    listed script is converted: the heuristic finding list is empty."""

    def test_the_derived_finding_list_is_empty(self) -> None:
        inventory = _inventory()
        _require(inventory, "bare_envelope_emissions")
        findings = inventory.bare_envelope_emissions()
        assert findings == [], (
            f"bare, uncaptured hookSpecificOutput emissions remain outside the "
            f"funnel: {findings}"
        )


class TestBareEnvelopeEmissionScannerPrecision:
    """CHECK 3's precision, pinned by a synthetic fixture per shape rather than by
    reading the live tree (plan.md non-negotiable 2). A scanner whose matching
    silently broke would otherwise read as full compliance forever, because the
    live tree only ever proves the shapes it happens to contain today.
    """

    def _write(self, tmp_path: Path, name: str, content: str) -> None:
        (tmp_path / name).write_text(content)

    def _findings(self, tmp_path: Path) -> list:
        inventory = _inventory()
        _require(inventory, "bare_envelope_emissions")
        return inventory.bare_envelope_emissions(scripts_dir=tmp_path)

    # ── Shapes that MUST be flagged ────────────────────────────────────────────

    def test_a_bare_python_heredoc_printing_an_envelope_is_flagged(self, tmp_path) -> None:
        self._write(
            tmp_path, "bare-heredoc.sh",
            "#!/bin/bash\n"
            "python3 <<'PY'\n"
            "import json\n"
            "print(json.dumps({'hookSpecificOutput': {'hookEventName': 'PreToolUse'}}))\n"
            "PY\n",
        )
        findings = self._findings(tmp_path)
        assert ("bare-heredoc.sh", 2) in findings, findings

    def test_a_bare_python_dash_c_multiline_string_is_flagged(self, tmp_path) -> None:
        self._write(
            tmp_path, "bare-python-c.sh",
            "#!/bin/bash\n"
            'python3 -c "\n'
            "import json\n"
            "print(json.dumps({'hookSpecificOutput': {'hookEventName': 'PreToolUse'}}))\n"
            '"\n',
        )
        findings = self._findings(tmp_path)
        assert ("bare-python-c.sh", 2) in findings, findings

    def test_a_bare_jq_dash_n_envelope_is_flagged(self, tmp_path) -> None:
        self._write(
            tmp_path, "bare-jq.sh",
            "#!/bin/bash\n"
            'jq -n \'{hookSpecificOutput: {hookEventName: "PreToolUse"}}\'\n',
        )
        findings = self._findings(tmp_path)
        assert ("bare-jq.sh", 2) in findings, findings

    def test_a_bare_echo_of_a_variable_built_from_an_envelope_construct_is_flagged(
        self, tmp_path
    ) -> None:
        self._write(
            tmp_path, "bare-echo-var.sh",
            "#!/bin/bash\n"
            'VAR=$(python3 -c "import json; '
            "print(json.dumps({'hookSpecificOutput': {'hookEventName': 'PreToolUse'}}))\"" + ")\n"
            'echo "$VAR"\n',
        )
        findings = self._findings(tmp_path)
        assert ("bare-echo-var.sh", 3) in findings, findings
        assert ("bare-echo-var.sh", 2) not in findings, (
            f"the captured VAR=$(...) assignment line must not itself be a finding: {findings}"
        )

    def test_a_printf_piped_into_python_dash_c_is_flagged(self, tmp_path) -> None:
        self._write(
            tmp_path, "bare-printf-pipe.sh",
            "#!/bin/bash\n"
            'printf \'%s\' "$INPUT" | python3 -c "\n'
            "import json\n"
            "print(json.dumps({'hookSpecificOutput': {'hookEventName': 'PreToolUse'}}))\n"
            '"\n',
        )
        findings = self._findings(tmp_path)
        assert ("bare-printf-pipe.sh", 2) in findings, (
            f"a pipe into an emitting python3 -c must not exempt the line: {findings}"
        )

    def test_a_stderr_only_redirect_does_not_exempt_the_line(self, tmp_path) -> None:
        """The regression this pin exists for: the existing _REDIRECT regex in
        writ/hooks_lint.py matches a stderr-only redirect and would wave the line
        through, which would silently exempt two real emissions in this tree
        (writ-pre-write-dispatch.sh line 290, writ-posttool-rag.sh line 266)."""
        self._write(
            tmp_path, "stderr-only-redirect.sh",
            "#!/bin/bash\n"
            'echo \'{"hookSpecificOutput": {"hookEventName": "PreToolUse"}}\' '
            '2>>"$WRIT_HOOK_LOG_SINK"\n',
        )
        findings = self._findings(tmp_path)
        assert ("stderr-only-redirect.sh", 2) in findings, (
            f"a stderr-only redirect wrongly exempted an emitting line: {findings}"
        )

    def test_an_env_var_prefix_holding_a_substitution_is_not_a_capture(
        self, tmp_path
    ) -> None:
        """The hole review reproduced rather than reasoned about: an env-var PREFIX
        whose value contains a command substitution feeds a SEPARATE command, and
        captures nothing at all. A rule that reads any `$(` anywhere on the opening
        line as capture waves the whole emission through.

        This fixture is the pre-conversion shape of two real lines, writ-read-rag.sh
        line 230 and writ-pre-write-dispatch.sh line 288. CHECK 2 cannot catch it
        either, because the script already reaches the funnel on another line, which
        is the ordinary state of every converted script. So a converted script that
        gains a NEW bare emission would be invisible to all three checks: exactly the
        recurrence this scanner exists to prevent.
        """
        self._write(
            tmp_path, "env-prefix-substitution.sh",
            "#!/bin/bash\n"
            'emit_hook_reply "$SOME_OTHER_REPLY"\n'
            'WRIT_AC="[Writ: file-context rules for $(basename "$FILE_PATH")]\n'
            '$RULES_TEXT" python3 <<\'PY\' 2>>"$WRIT_HOOK_LOG_SINK" || true\n'
            "import json, os\n"
            'print(json.dumps({"hookSpecificOutput": {\n'
            '    "hookEventName": "PreToolUse",\n'
            '    "additionalContext": os.environ.get("WRIT_AC", ""),\n'
            "}}))\n"
            "PY\n",
        )
        findings = self._findings(tmp_path)
        assert ("env-prefix-substitution.sh", 3) in findings, (
            "an env-var prefix holding a command substitution was read as a capture, "
            f"so a real bare emission went unflagged: {findings}"
        )

    # ── Shapes that MUST NOT be flagged ─────────────────────────────────────────

    def test_a_whole_line_comment_naming_the_key_is_not_flagged(self, tmp_path) -> None:
        self._write(
            tmp_path, "comment-only.sh",
            "#!/bin/bash\n"
            "# Emits a hookSpecificOutput reply on success.\n"
            'echo "plain informational text"\n',
        )
        assert self._findings(tmp_path) == []

    def test_a_captured_command_substitution_and_its_heredoc_body_are_not_flagged(
        self, tmp_path
    ) -> None:
        self._write(
            tmp_path, "captured-heredoc.sh",
            "#!/bin/bash\n"
            "DECISION=$(cat <<'JSON'\n"
            '{"hookSpecificOutput": {"hookEventName": "PreToolUse"}}\n'
            "JSON\n"
            ")\n"
            'echo "captured, never printed bare"\n',
        )
        assert self._findings(tmp_path) == []

    def test_a_true_stdout_redirect_to_stderr_is_not_flagged(self, tmp_path) -> None:
        self._write(
            tmp_path, "stdout-to-stderr.sh",
            "#!/bin/bash\n"
            'echo \'{"hookSpecificOutput": {"hookEventName": "PreToolUse"}}\' >&2\n',
        )
        assert self._findings(tmp_path) == []


# --------------------------------------------------------------------------------- #
# The census correctly names the real event for a converted hook's OUT row
# --------------------------------------------------------------------------------- #


class TestCensusFilesConvertedHooksUnderRealEvents:
    """Capability: a census built from converted hooks' OUT rows files them under
    PreToolUse, PostToolUse and SubagentStart with the mechanisms present in each
    envelope, and produces no unknown|<hook>|out class for a converted hook."""

    def _out_row(self, hook: str, event: str, mechanism_payload: dict, pid: int) -> dict:
        payload = json.dumps({
            "hookSpecificOutput": {"hookEventName": event, **mechanism_payload},
        })
        return {
            "ts": "2026-08-29T00:00:00+00:00", "hook": hook, "direction": "out",
            "session": "sess-1", "pid": pid, "payload": payload,
        }

    def test_converted_hooks_file_under_their_real_event_never_unknown(self) -> None:
        try:
            import writ.analysis.blackbox as blackbox
        except ImportError as exc:
            pytest.fail(f"skeleton: writ/analysis/blackbox.py does not exist yet: {exc}")

        records = [
            self._out_row(
                "writ-pre-write-dispatch", "PreToolUse",
                {"additionalContext": "[Writ: file-context rules for x.py]"}, 100,
            ),
            self._out_row(
                "writ-posttool-rag", "PostToolUse",
                {"additionalContext": "[Writ: rag rules]"}, 101,
            ),
            self._out_row(
                "writ-subagent-start", "SubagentStart",
                {"additionalContext": "[Writ: mode directive]"}, 102,
            ),
        ]

        census = blackbox.build_census(records)
        by_class = census["records"]
        assert "PreToolUse|writ-pre-write-dispatch|out" in by_class, by_class
        assert "PostToolUse|writ-posttool-rag|out" in by_class, by_class
        assert "SubagentStart|writ-subagent-start|out" in by_class, by_class

        unknown_classes = [k for k in by_class if k.startswith("unknown|")]
        assert unknown_classes == [], (
            f"a converted hook's OUT row still filed under an unknown event: "
            f"{unknown_classes}"
        )

        pre_write_entry = by_class["PreToolUse|writ-pre-write-dispatch|out"]
        assert pre_write_entry["mechanisms"].get("additionalContext"), (
            "the converted hook's mechanism was not recorded: "
            f"{pre_write_entry['mechanisms']!r}"
        )
