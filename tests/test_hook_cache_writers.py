"""Layer 3 (fix/session-mode-preserve): no hook script may hand-roll a session-cache
write. Three inline-python blocks (writ-rag-inject.sh recall_briefed + escalation,
writ-cwd-changed.sh detected_domain) used the exact pre-fix shared-`.tmp` pattern that
produces torn writes (door A) -- and writ-cwd-changed.sh additionally wrote `{}` over
the cache on any bad read (door B, a wipe with no race). They must route through the
locked/atomic `writ-session.py update --set-*` path instead.

RED today: three `tmp = path + '.tmp'` writers exist and the --set-* flags are absent.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

HOOKS_DIR = Path(__file__).resolve().parent.parent / "hooks" / "scripts"

# Matches a hand-rolled temp writer: tmp = path + '.tmp'  (single or double quotes).
_HANDROLLED_TMP_RE = re.compile(r"""tmp\s*=\s*path\s*\+\s*['"]\.tmp['"]""")


def _hook_files() -> list[Path]:
    return sorted(HOOKS_DIR.glob("*.sh"))


class TestNoHandrolledCacheWriters:
    def test_no_hook_script_hand_rolls_a_tmp_cache_writer(self) -> None:
        offenders = []
        for f in _hook_files():
            for n, line in enumerate(f.read_text().splitlines(), 1):
                if _HANDROLLED_TMP_RE.search(line):
                    offenders.append(f"{f.name}:{n}")
        assert offenders == [], (
            "hook scripts must not hand-roll a session-cache write (shared '.tmp' + "
            f"os.rename, no lock/fsync); route through writ-session.py update. Found: {offenders!r}"
        )

    def test_no_hook_script_wipes_cache_to_empty_dict_on_bad_read(self) -> None:
        # writ-cwd-changed.sh's `cache = {}` on JSONDecodeError then write-back wiped
        # the whole session. After routing through the CLI update path this block is gone.
        cwd = HOOKS_DIR / "writ-cwd-changed.sh"
        src = cwd.read_text()
        assert "cache = {}" not in src, (
            "writ-cwd-changed.sh must not fall back to an empty-dict cache and write "
            "it back (this wipes mode/gates on any single bad read)"
        )


class TestNoDirectUnlockedCacheWriteInHooks:
    """Broader guard than the literal-'.tmp' fingerprint: a hook that imports the
    session module and calls _write_cache directly (as friction-logger.sh and
    writ-subagent-start.sh did) bypasses the mutate_cache lock exactly like a
    hand-rolled writer. No hook may call _write_cache directly; whole-dict writes
    go through mutate_cache (or the update CLI)."""

    def test_no_hook_calls_write_cache_directly(self) -> None:
        offenders = []
        for f in _hook_files():
            for n, line in enumerate(f.read_text().splitlines(), 1):
                if "_write_cache(" in line:
                    offenders.append(f"{f.name}:{n}")
        assert offenders == [], (
            "hook scripts must not call _write_cache directly (unlocked whole-dict "
            f"write, the lost-update root cause); use mutate_cache. Found: {offenders!r}"
        )

    def test_friction_logger_uses_mutate_cache(self) -> None:
        src = (HOOKS_DIR / "friction-logger.sh").read_text()
        assert "mutate_cache" in src, (
            "friction-logger.sh (a Stop hook firing every turn) must write "
            "phase_transitions_logged under mutate_cache, not a bare _write_cache"
        )

    def test_subagent_start_uses_mutate_cache(self) -> None:
        src = (HOOKS_DIR / "writ-subagent-start.sh").read_text()
        assert "mutate_cache" in src, (
            "writ-subagent-start.sh must initialize the sub-agent cache under "
            "mutate_cache, not a bare _write_cache"
        )


class TestHookWritersUseUpdateCli:
    def test_rag_inject_uses_set_flags(self) -> None:
        src = (HOOKS_DIR / "writ-rag-inject.sh").read_text()
        # The recall state writer is the recall section of /prompt-bundle, not a hook:
        # the server records the shown ids on the snapshot the recall request read
        # (docs/adr/ADR-prompt-injection-split.md), through the --mark-shown update verb.
        route = (Path(__file__).resolve().parent.parent / "writ" / "server" / "routes"
                 / "query.py").read_text()
        assert '"--mark-shown", "recall"' in route, (
            "the recall section of /prompt-bundle must record its shown ids via the "
            "--mark-shown recall update verb"
        )
        assert "--set-recall-briefed" not in route, "the retired flag must not be written"
        for token in ("--set-recall-briefed", "--mark-shown", "recall_briefed"):
            assert token not in src, (
                f"writ-rag-inject.sh no longer owns the recall briefing ({token}); a second "
                "writer of recall state would race the server's"
            )
        assert "--set-escalation-feedback-sent" in src, (
            "writ-rag-inject.sh must set escalation.feedback_sent via the update CLI"
        )

    def test_cwd_changed_writes_nothing_to_the_session_cache(self) -> None:
        # Program item 1b: the language marker's only cache reader filtered ranked
        # retrieval down to nothing, so the hook stopped caching it. It must not regain a
        # cache write.
        src = (HOOKS_DIR / "writ-cwd-changed.sh").read_text()
        assert "--set-detected-domain" not in src
        assert '"$SESSION_HELPER" update' not in src
