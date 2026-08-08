"""writ_action_push builds its request body in bash instead of spawning python.

Pins every checkbox in the capabilities.md section "The request body loses its
python spawn without changing shape".

Why: the function spawned python purely to produce a four-key literal
({"action": ..., "prompt": "", "exclude_rule_ids": [], "budget_tokens": 2000}),
which cost roughly 25ms on the write path. All four callers pass an internal
literal token (gate-denial, review-feedback, bible-authoring, and a derived
PUSH_ACTION), so bash can build it.

The invariant that matters is EQUIVALENCE, exactly as tests/test_b2_json_helpers.py
guards for parsed_field: the bash-built body must be byte-identical to the
python-built one, and anything that is not a plain token must take the python path
rather than being interpolated into a JSON literal. Hand-rolling JSON escaping for
arbitrary input is how injection bugs get written (SEC-INJ-CMD-002), so the
allowlist decides, not a blocklist (ABS-SECURITY-024).

Per TEST-TDD-001: skeletons approved before implementation.
"""

from __future__ import annotations

import json
import shlex
import subprocess
from pathlib import Path

import pytest

COMMON_SH = str(Path(__file__).resolve().parent.parent / "bin/lib/common.sh")

# The four tokens the real callers pass today.
LIVE_TOKENS = ["gate-denial", "review-feedback", "bible-authoring", "sdd_phase_2"]

# Rejected by the allowlist: each would need real JSON escaping.
UNSAFE_TOKENS = [
    'a"b',          # quote: would break out of the JSON string
    "a\\b",         # backslash: escape-sequence hazard
    "a b",          # space
    "a\nb",         # newline
    "drop{}",       # braces
    "unicodeé",  # non-ASCII
]


def _body(action: str, *, force_python: bool = False) -> str:
    """Return the request body writ_action_push would POST for this action.

    Uses the extracted builder rather than running the full function, which would
    need a live daemon. WRIT_NO_BASH_JSON=1 forces the python fallback so the two
    paths can be compared byte for byte, mirroring the WRIT_NO_JQ seam that
    parsed_field already uses for the same purpose.
    """
    env = "WRIT_NO_BASH_JSON=1 " if force_python else ""
    script = (
        f"source {shlex.quote(COMMON_SH)}; "
        f"printf '%s' \"$({env}writ_action_push_body {shlex.quote(action)})\""
    )
    proc = subprocess.run(
        ["bash", "-c", script], capture_output=True, text=True, timeout=30
    )
    return proc.stdout


# --------------------------------------------------------------------------- #
# 1. Equivalence with the python path it replaces
# --------------------------------------------------------------------------- #
class TestBodyEquivalence:
    @pytest.mark.parametrize("action", LIVE_TOKENS)
    def test_bash_body_matches_python_body_byte_for_byte(self, action: str) -> None:
        bash_body = _body(action)
        # Anti-vacuity guard: before the builder exists BOTH paths return empty and
        # this equality holds for the wrong reason.
        assert bash_body.strip(), "builder produced nothing; equality would be vacuous"
        assert bash_body == _body(action, force_python=True), action

    @pytest.mark.parametrize("action", LIVE_TOKENS)
    def test_body_parses_and_carries_the_four_expected_keys(self, action: str) -> None:
        parsed = json.loads(_body(action))
        assert parsed == {
            "action": action,
            "prompt": "",
            "exclude_rule_ids": [],
            "budget_tokens": 2000,
        }

    def test_body_is_valid_json_for_every_live_token(self) -> None:
        for action in LIVE_TOKENS:
            json.loads(_body(action))  # raises on malformed output


# --------------------------------------------------------------------------- #
# 2. The allowlist decides, and unsafe input takes the python path
# --------------------------------------------------------------------------- #
class TestUnsafeTokensFallBack:
    @pytest.mark.parametrize("action", UNSAFE_TOKENS)
    def test_unsafe_token_still_produces_valid_json(self, action: str) -> None:
        """However it is produced, the output must be parseable: a token that broke
        out of the string literal would be caught here."""
        out = _body(action)
        if not out.strip():
            pytest.skip("builder declined this token outright, which is also safe")
        parsed = json.loads(out)
        assert parsed["action"] == action

    @pytest.mark.parametrize("action", UNSAFE_TOKENS)
    def test_unsafe_token_matches_the_python_path(self, action: str) -> None:
        """The fallback is only correct if it agrees with the path it falls back to."""
        assert _body(action) == _body(action, force_python=True), action

    def test_empty_action_produces_no_body(self) -> None:
        """Preserves today's contract: an empty action returns without a request."""
        assert _body("").strip() == ""


# --------------------------------------------------------------------------- #
# 3. The write path actually got cheaper
# --------------------------------------------------------------------------- #
class TestSpawnReduction:
    """Assert the spawn is gone from the FUNCTION, not from a whole-hook count.

    A whole-hook count cannot be asserted inside pytest: conftest sets
    WRIT_PORT to the test daemon port, nothing listens there, and the hook then
    takes its daemon-down path. Measured 2026-08-06: 8 python spawns in production
    against a live daemon, but only 4 under pytest's env, so a `count < 6`
    assertion would have passed without any change to the code. Tracing the one
    function is both precise and independent of whether a daemon is up.
    """

    def _python_spawns(self, snippet: str) -> int:
        script = (
            f"source {shlex.quote(COMMON_SH)} >/dev/null 2>&1; "
            f"PS4='+X+ '; set -x; {snippet}"
        )
        proc = subprocess.run(
            ["bash", "-c", script], capture_output=True, text=True, timeout=60
        )
        return sum(
            1 for ln in proc.stderr.splitlines()
            if ln.lstrip("+").startswith("X+ ") and " python3" in ln
        )

    def test_safe_token_body_spawns_no_python(self) -> None:
        assert self._python_spawns("writ_action_push_body gate-denial >/dev/null") == 0

    def test_forced_python_path_does_spawn(self) -> None:
        """Anti-vacuity: proves the probe can see a spawn, so the zero above means
        'no spawn' rather than 'the counter is broken'."""
        assert self._python_spawns(
            "WRIT_NO_BASH_JSON=1 writ_action_push_body gate-denial >/dev/null"
        ) >= 1

    def test_unsafe_token_takes_the_python_path(self) -> None:
        """The fallback must actually be the python path, not a bash guess."""
        assert self._python_spawns('writ_action_push_body \'a"b\' >/dev/null') >= 1
