#!/usr/bin/env python3
"""Shared summary emitter for hooks that produce verbose tool output.

Reads a log file (full tool output), emits a terse stderr summary that
references the log path. Claude reads the log only if the summary does not
pinpoint the cause. Always exits 0; caller picks the hook exit code.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path


# ── Input normalization: THE PARSER IS THE GUARD ───────────────────────────────
# Every text-format pattern below is line-anchored with `^` under `re.M`, and the
# runners color the LEADING token of exactly the lines those patterns anchor on,
# so an SGR escape sits between the line start and the anchor's first literal
# character and no anchor can match. This parser is the DECIDER for the
# pending-tests Stop refusal: `writ-run-pending-tests.sh` does not trust the
# runner's exit code, it asks this helper and treats an empty summary as a pass.
# So whatever this parser cannot read becomes an ALLOW, which is why the layer
# that decides is the layer that must normalize.
#
# `writ-run-pending-tests.sh` also unsets the color variables before it runs the
# runner, but that arm is HYGIENE for the log the agent may read, never the
# control: `runner_command` is project configuration (`bin/lib/test_paths.py`
# reads `<cwd>/.claude/writ.json`), so a project can legitimately configure
# `pytest --color=yes`, and pytest's `--color` flag outranks every environment
# variable. No producer-side suppression can guarantee a plain log.
#
# SGR ONLY: escape, open bracket, numeric parameters, the letter `m`. That is the
# sequence measured in the real log this hook produced, and stripping exactly it
# made the anchors match. WIDENING PROTOCOL: a new sequence is added together
# with a fixture carrying that exact sequence, taken from real runner output; a
# widening with no fixture is speculation and is rejected. Deliberately absent,
# so the next reader does not think they were forgotten: cursor and erase
# sequences, OSC strings, charset selection, and a bare carriage return (which
# defeats these anchors for a different reason, `re.M` not treating it as a line
# boundary). None was observed in the measured log.
#
# NOT `tests/_ansi.py::plain()`, for two reasons, and the second is the one that
# matters: `bin/lib/` cannot import from `tests/`, AND `plain()` also collapses
# all whitespace, which would fold the whole log into ONE line and destroy the
# line structure every anchor here depends on, trading one silent no-match for
# another. The two layers share the knowledge, not the function; the durable
# artifact is docs/adr/ADR-guard-input-normalization.md.
_ANSI_SGR = re.compile(r"\x1b\[[0-9;]*m")


def _strip_ansi(text: str) -> str:
    """Text with SGR color escapes removed. Line structure is preserved."""
    return _ANSI_SGR.sub("", text)


def summarize_pytest(text: str) -> tuple[int, list[str]]:
    fails = re.findall(r"^FAILED .*$", text, re.M)
    first_assert = re.search(r"^E\s+.*$", text, re.M)
    lines: list[str] = []
    if fails:
        lines.append(fails[0])
    if first_assert:
        lines.append(first_assert.group(0))
    return len(fails), lines


def summarize_phpunit(text: str) -> tuple[int, list[str]]:
    # PHPUnit prints `1) ...` numbered lists for BOTH failures and warnings,
    # and a run can contain a warnings section AND a failures section in the
    # same output (e.g. Magento with Allure config missing + failing tests).
    # Two-stage filter:
    #   1. Require a `FAILURES!` or `ERRORS!` headline -- proves real failures
    #      exist somewhere in the output.
    #   2. Require `::` in each matched line -- failure/error entries use the
    #      `1) FQCN::methodName` form; warning entries have free-form text.
    if not re.search(r"^(FAILURES!|ERRORS!)", text, re.M):
        return 0, []
    fails = re.findall(r"^\d+\) .*::.+$", text, re.M)
    return len(fails), fails[:1]


def summarize_gotest(text: str) -> tuple[int, list[str]]:
    fails = re.findall(r"^--- FAIL: .*$", text, re.M)
    return len(fails), fails[:1]


def summarize_json(text: str) -> tuple[int, list[str]]:
    try:
        findings = json.loads(text)
    except (json.JSONDecodeError, ValueError):
        return 0, []
    if not isinstance(findings, list):
        return 0, []
    errs = [f for f in findings if isinstance(f, dict) and f.get("severity") == "error"]
    if not errs:
        return 0, []
    f = errs[0]
    line = (f"{f.get('file', '?')}:{f.get('line', 0)} "
            f"[{f.get('tool', '?')}] {f.get('message', '')}")
    return len(errs), [line]


DISPATCH = {
    "pytest": summarize_pytest,
    "phpunit": summarize_phpunit,
    "gotest": summarize_gotest,
    "json": summarize_json,
}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--format", required=True, choices=list(DISPATCH))
    ap.add_argument("--log", required=True, help="Path to full output log")
    ap.add_argument("--rule", default="ENF-POST-007", help="Rule code prefix")
    ap.add_argument("--label", default="findings",
                    help="Plural noun used in the count line")
    args = ap.parse_args()

    log_path = Path(args.log)
    if not log_path.is_file():
        return 0

    try:
        text = log_path.read_text(errors="replace")
    except OSError:
        return 0

    # Normalize ONCE, here: this is the single site all four formats flow
    # through, so the five line-anchored patterns never see presentation bytes
    # and a format added later inherits the strip for free. Per format would be
    # three copies of the same line. BEFORE the dispatch call, not after, so the
    # line captured into the agent-facing summary below is clean as well.
    #
    # The `json` format flows through this too and its behavior is unchanged.
    # Measured rather than reasoned: `json.loads` REJECTS a raw escape byte
    # inside a string (JSONDecodeError, "Invalid control character"), and the
    # six-character escaped spelling decodes to a real escape byte, so a
    # parseable document carries color only as six ordinary characters that this
    # pattern does not match.
    text = _strip_ansi(text)

    count, first_lines = DISPATCH[args.format](text)
    if count == 0:
        return 0

    print(f"[{args.rule}] {count} {args.label}. First:", file=sys.stderr)
    for line in first_lines:
        if line:
            print(f"  {line}", file=sys.stderr)
    print(f"Full log: {log_path}", file=sys.stderr)
    print("Read the log only if the first finding does not pinpoint the cause.",
          file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
