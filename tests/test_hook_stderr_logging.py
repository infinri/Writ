"""Hook stderr surfacing -- replace 2>/dev/null with $WRIT_HOOK_LOG.

Context: the legacy-cache KeyError went undetected for an entire session
because writ-rag-inject.sh and writ-posttool-rag.sh swallowed stderr from
the mutating `_writ_session update` calls. These tests pin the new
contract: stderr from those calls must reach a hook log file, not /dev/null.

We can't easily test the bash redirection itself, so we test the
underlying capability: WRIT_HOOK_LOG exists as a documented contract via a
helper, and the hook scripts reference it instead of /dev/null on the
mutating call sites.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys

import pytest
from pathlib import Path

SKILL_DIR = str(Path(__file__).resolve().parent.parent)
HOOKS_DIR = Path(SKILL_DIR) / "hooks" / "scripts"
INJECT_HOOK = f"{SKILL_DIR}/hooks/scripts/writ-rag-inject.sh"
POSTTOOL_HOOK = f"{SKILL_DIR}/hooks/scripts/writ-posttool-rag.sh"

# A mutating `_writ_session update`: one that banks rules, cost or always-on
# tokens onto the session cache. Line continuations are joined first so a call
# spread over five lines matches as one logical command.
_MUTATING_UPDATE_RE = re.compile(
    r"_writ_session update[^\n]*?--add-(?:rules|rule-objects|always-on-tokens)[^\n]*"
)


def _mutating_updates(body: str) -> list[str]:
    return _MUTATING_UPDATE_RE.findall(re.sub(r"\\\n\s*", " ", body))


# THE POPULATION IS DERIVED, NEVER LISTED.
#
# This used to be two named hooks, writ-rag-inject.sh and writ-posttool-rag.sh,
# the pair whose swallowed stderr hid the legacy-cache KeyError for a whole
# session. That list broke in both directions. It went RED when plan dfacff61
# moved writ-rag-inject.sh's cache update server-side, so the hook legitimately
# has no mutating update left and "expected at least one" became false for it.
# And it was blind to every other hook that does one: deriving the population on
# 2026-09-01 turned up writ-read-rag.sh and writ-pre-write-dispatch.sh, BOTH
# still redirecting to /dev/null, the very thing this module exists to forbid.
# Both are fixed; the derivation is what found them.
HOOKS_WITH_MUTATING_UPDATE = sorted(
    path.name for path in HOOKS_DIR.glob("*.sh") if _mutating_updates(path.read_text())
)


class TestHookStderrLogging:
    """The mutating writ-session.py calls AND friction-log emit blocks
    inside the hooks must redirect stderr to $WRIT_HOOK_LOG (default
    /tmp/writ-hooks.log), not /dev/null.

    We grep the hook scripts for the call sites and assert they reference
    the env var rather than /dev/null. This is a structural test -- it
    catches regressions where someone reverts the redirection.
    """

    def _friction_emit_blocks(self, body: str) -> list[str]:
        """Find inline `python3 -c "..."` blocks that conclude with the
        argv list + redirect; pick out the redirect tail (last 80 chars
        of the line that closes with `|| true`)."""
        import re
        # Match terminal lines that look like `" args 2>... || true`
        return re.findall(
            r'^"\s+"[^"\n]*"\s*(?:"[^"\n]*"\s*)*2>[^\n]+\|\|\s*true',
            body,
            re.MULTILINE,
        )

    def _read(self, path: str) -> str:
        with open(path) as f:
            return f.read()

    def _mutating_update_blocks(self, body: str) -> list[str]:
        """Extract each `_writ_session update ... || true` block, joining
        bash line-continuations so the regex over multi-line commands."""
        # Collapse `\` + newline into a single space so we can match
        # multi-line update calls as single logical lines.
        joined = re.sub(r"\\\n\s*", " ", body)
        return re.findall(
            r"_writ_session update[^\n]*?--add-(?:rules|rule-objects|always-on-tokens)[^\n]*",
            joined,
        )

    def test_the_mutating_update_population_is_not_empty(self) -> None:
        """Anti-vacuity. A derived population that matched nothing would make
        the parametrized test below vanish and this module read green while
        asserting nothing about any hook."""
        assert HOOKS_WITH_MUTATING_UPDATE, (
            "no hook performs a mutating _writ_session update; either every "
            "cache write moved server-side (delete this test) or the detector "
            "regex has drifted"
        )

    @pytest.mark.parametrize("hook_name", HOOKS_WITH_MUTATING_UPDATE)
    def test_mutating_update_uses_writ_hook_log(self, hook_name: str) -> None:
        """Every hook that banks rules onto the session cache must send that
        call's stderr to the hook log.

        MUTATION: changing any of these back to `2>/dev/null` turns that hook's
        parameter red. A hook that legitimately has NO mutating update is simply
        not in the population, and a hook that GAINS one is covered with no edit
        here.
        """
        blocks = self._mutating_update_blocks(self._read(str(HOOKS_DIR / hook_name)))
        assert blocks, (
            f"{hook_name} is in the derived population but no mutating update "
            "was extracted; the derivation and the extractor disagree"
        )
        bad = [b for b in blocks if "WRIT_HOOK_LOG" not in b and "writ-hooks.log" not in b]
        assert not bad, (
            f"{hook_name}: mutating update(s) missing WRIT_HOOK_LOG redirect:\n"
            + "\n".join(b[:240] for b in bad)
        )
        # Defense-in-depth: assert NO mutating update still ends in 2>/dev/null
        legacy = [b for b in blocks if "2>/dev/null" in b]
        assert not legacy, (
            f"{hook_name}: mutating update still uses 2>/dev/null:\n"
            + "\n".join(b[:240] for b in legacy)
        )

    def test_friction_emit_blocks_use_writ_hook_log(self) -> None:
        """The python3 -c blocks that emit friction events / write
        cache files must redirect stderr to WRIT_HOOK_LOG too. Catch
        regressions where a new emit slips in with 2>/dev/null."""
        for path in (INJECT_HOOK, POSTTOOL_HOOK):
            body = self._read(path)
            blocks = self._friction_emit_blocks(body)
            for b in blocks:
                # Each tail line must redirect to WRIT_HOOK_LOG, not /dev/null.
                assert "WRIT_HOOK_LOG" in b or "writ-hooks.log" in b, (
                    f"friction-emit/cache-write block in {path} still uses /dev/null:\n{b}"
                )

    def test_writ_session_update_failure_writes_to_hook_log(
        self, tmp_path
    ) -> None:
        """End-to-end: when writ-session.py update raises, the redirect
        target receives the stderr content."""
        log_path = tmp_path / "writ-hooks.log"
        env = os.environ.copy()
        env["WRIT_CACHE_DIR"] = str(tmp_path)

        # Trigger an error: invalid JSON to --add-rules forces a failure path.
        proc = subprocess.run(
            [
                "bash",
                "-c",
                f'python3 {SKILL_DIR}/bin/lib/writ-session.py update bogus-sid '
                f'--add-rules "not-a-json-array" 2>>"{log_path}" || true',
            ],
            capture_output=True,
            text=True,
            env=env,
        )
        assert log_path.exists(), "log path was not created by the redirect"
        content = log_path.read_text()
        assert content.strip(), (
            f"hook log is empty -- redirect did not capture stderr "
            f"(stdout={proc.stdout!r}, stderr={proc.stderr!r})"
        )
