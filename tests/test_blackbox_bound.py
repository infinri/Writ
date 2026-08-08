"""blackbox_log must not be able to run forever once switched on.

Pins every checkbox in the capabilities.md section "The capture cannot run forever".

Why this exists: the capture is opt-in via WRIT_BLACKBOX=1 or the sentinel file
~/.claude/writ-blackbox.on, and nothing bounded it. Measured 2026-08-06 on this
machine, the sentinel was dated 19 June and ~/.claude/writ-blackbox.jsonl had
reached 1,482,042,343 bytes, still growing, costing 35ms and 2 python spawns on
every write (270ms with capture, 235ms without, 12 runs each). A debug switch with
no size cap and no expiry is indistinguishable from a leak (ABS-ARCHITECTURE-021
on permanent flags), so the cap is the cleanup path the flag never had.

It must self-disable LOUDLY: a capture that stops without saying so is a debugging
trap of its own, hence the friction row.

Per TEST-TDD-001: skeletons approved before implementation. Harness idiom follows
tests/test_b2_json_helpers.py: source common.sh in a bash -c and observe.
"""

from __future__ import annotations

import json
import shlex
import subprocess
from pathlib import Path

import pytest

COMMON_SH = str(Path(__file__).resolve().parent.parent / "bin/lib/common.sh")

DEFAULT_CAP_BYTES = 256 * 1024 * 1024


def _capture(
    tmp_path: Path,
    payload: str = '{"probe": 1}',
    *,
    log_bytes: int | None = None,
    max_bytes: int | None = None,
    enabled: bool = True,
    friction_log: Path | None = None,
) -> tuple[Path, subprocess.CompletedProcess]:
    """Run blackbox_log once against a temp log of a chosen size. Returns the log path."""
    tmp_path.mkdir(parents=True, exist_ok=True)
    log = tmp_path / "bb.jsonl"
    if log_bytes is not None:
        # Newline-TERMINATED filler, exact byte count preserved. Without the newline
        # an appended record concatenates onto the filler's line and _records cannot
        # see it, which reads as "did not capture" and inverts these assertions.
        log.write_bytes(b"x" * (log_bytes - 1) + b"\n" if log_bytes else b"")

    env_parts = [f"WRIT_BLACKBOX_LOG={shlex.quote(str(log))}"]
    if enabled:
        env_parts.append("WRIT_BLACKBOX=1")
    if max_bytes is not None:
        env_parts.append(f"WRIT_BLACKBOX_MAX_BYTES={max_bytes}")
    if friction_log is not None:
        env_parts.append(f"WRIT_FRICTION_LOG={shlex.quote(str(friction_log))}")
    # HOME is redirected so the real ~/.claude/writ-blackbox.on sentinel on the
    # developer's machine cannot switch capture on underneath a test that is
    # asserting it stays off.
    env_parts.append(f"HOME={shlex.quote(str(tmp_path / 'home'))}")

    script = (
        f"source {shlex.quote(COMMON_SH)}; "
        f"printf '%s' {shlex.quote(payload)} | {' '.join(env_parts)} "
        f"blackbox_log in test-hook test-session"
    )
    proc = subprocess.run(
        ["bash", "-c", script], capture_output=True, text=True, timeout=30
    )
    return log, proc


def _records(log: Path) -> list[str]:
    """Only the appended JSON records, not the synthetic filler bytes.

    The filler written by _capture to reach a target size has no newline, so a naive
    line count treats it as one record and every "must not append" assertion passes
    or fails for the wrong reason. Records are JSON objects, so select on that.
    """
    if not log.exists():
        return []
    out = []
    for ln in log.read_text(errors="replace").splitlines():
        ln = ln.strip()
        if not ln.startswith("{"):
            continue
        try:
            json.loads(ln)
        except ValueError:
            continue
        out.append(ln)
    return out


# --------------------------------------------------------------------------- #
# 1. The cap
# --------------------------------------------------------------------------- #
class TestSizeCap:
    def test_captures_below_the_cap(self, tmp_path: Path) -> None:
        log, _ = _capture(tmp_path, log_bytes=10, max_bytes=1000)
        assert len(_records(log)) == 1, "a small log must still be appended to"

    def test_stops_capturing_over_the_cap(self, tmp_path: Path) -> None:
        log, _ = _capture(tmp_path, log_bytes=1001, max_bytes=1000)
        assert len(_records(log)) == 0, (
            "an over-cap log must not grow; this is the leak the cap exists to stop"
        )

    def test_exactly_at_the_cap_still_captures(self, tmp_path: Path) -> None:
        """The boundary is 'over', not 'at': at the cap the budget is not yet spent."""
        log, _ = _capture(tmp_path, log_bytes=1000, max_bytes=1000)
        assert len(_records(log)) == 1

    def test_far_over_the_cap_stops(self, tmp_path: Path) -> None:
        """Distinct from the one-byte case above: an order-of-magnitude overrun must
        take the same branch, not a different one."""
        log, _ = _capture(tmp_path, log_bytes=100000, max_bytes=1000)
        assert len(_records(log)) == 0

    @pytest.mark.parametrize("bad_cap", ["abc", "", "12x", "9" * 30, "-5"])
    def test_unparseable_cap_falls_back_to_the_default_and_still_enforces(
        self, bad_cap: str, tmp_path: Path
    ) -> None:
        """`[ x -gt y ]` on a non-numeric or overflowing value exits non-zero, which
        an `if` reads as false: without validation a typo in this one variable would
        silently stop enforcing the cap. Under the default cap a small log captures,
        which is the observable proof the comparison still ran."""
        log = tmp_path / "bb.jsonl"
        tmp_path.mkdir(parents=True, exist_ok=True)
        log.write_bytes(b"x" * 9 + b"\n")
        script = (
            f"source {shlex.quote(COMMON_SH)}; "
            f"printf '%s' '{{\"probe\": 1}}' | "
            f"WRIT_BLACKBOX=1 WRIT_BLACKBOX_LOG={shlex.quote(str(log))} "
            f"WRIT_BLACKBOX_MAX_BYTES={shlex.quote(bad_cap)} "
            f"HOME={shlex.quote(str(tmp_path / 'home'))} "
            f"blackbox_log in test-hook test-session"
        )
        proc = subprocess.run(
            ["bash", "-c", script], capture_output=True, text=True, timeout=30
        )
        assert proc.returncode == 0
        assert len(_records(log)) == 1, (
            f"a bad cap ({bad_cap!r}) must fall back to the default, not disable the "
            f"comparison"
        )

    def test_default_cap_is_applied_at_runtime(self, tmp_path: Path) -> None:
        """Not just present in the source text: with the env var unset, a sparse log
        one byte over 256 MiB must stop capture. truncate makes that file instantly
        and costs no disk."""
        tmp_path.mkdir(parents=True, exist_ok=True)
        log = tmp_path / "bb.jsonl"
        subprocess.run(
            ["truncate", "-s", str(DEFAULT_CAP_BYTES + 1), str(log)],
            capture_output=True, timeout=30, check=True,
        )
        script = (
            f"source {shlex.quote(COMMON_SH)}; "
            f"printf '%s' '{{\"probe\": 1}}' | "
            f"WRIT_BLACKBOX=1 WRIT_BLACKBOX_LOG={shlex.quote(str(log))} "
            f"HOME={shlex.quote(str(tmp_path / 'home'))} "
            f"blackbox_log in test-hook test-session"
        )
        subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=60)
        assert log.stat().st_size == DEFAULT_CAP_BYTES + 1, (
            "nothing may be appended past the default cap"
        )

    def test_no_stderr_leak_when_the_log_does_not_exist_yet(self, tmp_path: Path) -> None:
        """`wc -c < missing 2>/dev/null` cannot suppress the message: bash reports the
        failed redirect before wc runs, leaking a raw error to the hook's stderr on
        every first capture."""
        _, proc = _capture(tmp_path, log_bytes=None, max_bytes=1000)
        assert proc.stderr.strip() == "", f"stderr leaked: {proc.stderr!r}"

    def test_absent_log_is_treated_as_zero_bytes(self, tmp_path: Path) -> None:
        """First capture of a session: no file yet, so nothing to compare, and the
        cap must not read a missing file as infinite."""
        log, _ = _capture(tmp_path, log_bytes=None, max_bytes=1000)
        assert len(_records(log)) == 1

    def test_cap_is_configurable(self, tmp_path: Path) -> None:
        """Same log size, opposite outcomes, decided only by the env var."""
        log_small, _ = _capture(tmp_path / "a", log_bytes=500, max_bytes=100)
        log_large, _ = _capture(tmp_path / "b", log_bytes=500, max_bytes=100000)
        assert len(_records(log_small)) == 0
        assert len(_records(log_large)) == 1

    def test_default_cap_constant_is_documented_in_source(self) -> None:
        """The default must be a named, greppable value, not an inline literal, so
        an operator can find what bound their capture."""
        src = Path(COMMON_SH).read_text()
        assert "WRIT_BLACKBOX_MAX_BYTES" in src
        assert str(DEFAULT_CAP_BYTES) in src or "268435456" in src


# --------------------------------------------------------------------------- #
# 2. It disables loudly
# --------------------------------------------------------------------------- #
class TestDisablesLoudly:
    def test_over_cap_emits_a_friction_row(self, tmp_path: Path) -> None:
        friction = tmp_path / "friction.log"
        _capture(tmp_path, log_bytes=2000, max_bytes=1000, friction_log=friction)
        assert friction.exists(), "hitting the cap must leave a trace"
        rows = [ln for ln in friction.read_text().splitlines() if ln.strip()]
        events = []
        for ln in rows:
            try:
                events.append(json.loads(ln).get("event"))
            except ValueError:
                continue
        assert "blackbox_capture_disabled" in events

    def test_friction_row_names_the_path_and_size(self, tmp_path: Path) -> None:
        """Enough to act on without going hunting: which file, how big."""
        friction = tmp_path / "friction.log"
        log, _ = _capture(tmp_path, log_bytes=2000, max_bytes=1000, friction_log=friction)
        text = friction.read_text()
        assert str(log) in text
        assert "2000" in text

    def test_row_is_emitted_once_not_per_invocation(self, tmp_path: Path) -> None:
        """Measured during implementation: announcing the cap on every hook cost
        ~20ms per write, one friction-append spawn each, so the announcement was
        more expensive than the capture it replaced."""
        friction = tmp_path / "friction.log"
        for _ in range(4):
            _capture(tmp_path, log_bytes=2000, max_bytes=1000, friction_log=friction)
        rows = [
            ln for ln in friction.read_text().splitlines()
            if "blackbox_capture_disabled" in ln
        ]
        assert len(rows) == 1, f"expected exactly one announcement, got {len(rows)}"

    def test_a_stale_marker_does_not_silence_a_later_crossing(self, tmp_path: Path) -> None:
        """The documented remediation is to delete the oversized log. If the marker
        survives that, the next crossing stops capture in silence, which is the exact
        failure this feature exists to prevent."""
        friction = tmp_path / "friction.log"
        # First crossing announces.
        log, _ = _capture(tmp_path, log_bytes=2000, max_bytes=1000, friction_log=friction)
        assert (tmp_path / "bb.jsonl.capped").exists()
        # Operator deletes the log; the marker is left behind.
        log.unlink()
        # A small log is under the cap, which must clear the stale marker.
        _capture(tmp_path, log_bytes=10, max_bytes=1000, friction_log=friction)
        assert not (tmp_path / "bb.jsonl.capped").exists(), (
            "an under-cap capture must clear the stale marker"
        )
        # Second crossing must announce again.
        friction.write_text("")
        _capture(tmp_path, log_bytes=5000, max_bytes=1000, friction_log=friction)
        assert "blackbox_capture_disabled" in friction.read_text(), (
            "a later crossing must announce, not inherit the first one's silence"
        )

    def test_marker_creation_failure_does_not_repeat_the_announcement(
        self, tmp_path: Path
    ) -> None:
        """If the marker cannot be written, announcing anyway would spawn
        friction-append on every write, which is the cost this feature removes."""
        friction = tmp_path / "friction.log"
        d = tmp_path / "ro"
        d.mkdir(parents=True, exist_ok=True)
        log = d / "bb.jsonl"
        log.write_bytes(b"x" * 1999 + b"\n")
        d.chmod(0o500)  # readable + executable, not writable
        try:
            for _ in range(3):
                script = (
                    f"source {shlex.quote(COMMON_SH)}; "
                    f"printf '%s' '{{\"probe\": 1}}' | "
                    f"WRIT_BLACKBOX=1 WRIT_BLACKBOX_LOG={shlex.quote(str(log))} "
                    f"WRIT_BLACKBOX_MAX_BYTES=1000 "
                    f"WRIT_FRICTION_LOG={shlex.quote(str(friction))} "
                    f"HOME={shlex.quote(str(tmp_path / 'home'))} "
                    f"blackbox_log in test-hook test-session"
                )
                subprocess.run(
                    ["bash", "-c", script], capture_output=True, text=True, timeout=30
                )
            rows = [
                ln for ln in (friction.read_text() if friction.exists() else "").splitlines()
                if "blackbox_capture_disabled" in ln
            ]
            assert len(rows) == 0, (
                f"an unmarkable announcement must stay silent rather than repeat "
                f"per write; got {len(rows)} rows"
            )
        finally:
            d.chmod(0o700)

    def test_row_survives_a_quote_in_the_log_path(self, tmp_path: Path) -> None:
        """The path is escaped, not interpolated raw: an unescaped quote produces
        invalid JSON that the writer drops silently, losing exactly the path and size
        this row exists to report."""
        friction = tmp_path / "friction.log"
        d = tmp_path / 'we"ird'
        d.mkdir(parents=True, exist_ok=True)
        log = d / "bb.jsonl"
        log.write_bytes(b"x" * 1999 + b"\n")
        script = (
            f"source {shlex.quote(COMMON_SH)}; "
            f"printf '%s' '{{\"probe\": 1}}' | "
            f"WRIT_BLACKBOX=1 WRIT_BLACKBOX_LOG={shlex.quote(str(log))} "
            f"WRIT_BLACKBOX_MAX_BYTES=1000 "
            f"WRIT_FRICTION_LOG={shlex.quote(str(friction))} "
            f"HOME={shlex.quote(str(tmp_path / 'home'))} "
            f"blackbox_log in test-hook test-session"
        )
        subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=30)
        rows = [
            json.loads(ln) for ln in friction.read_text().splitlines()
            if ln.strip().startswith("{")
        ]
        capped = [r for r in rows if r.get("event") == "blackbox_capture_disabled"]
        assert capped, "the row must survive a quoted path"
        assert capped[0].get("size_bytes") == 2000, (
            f"the row lost its fields to broken JSON: {capped[0]}"
        )

    def test_under_cap_emits_no_disabled_row(self, tmp_path: Path) -> None:
        friction = tmp_path / "friction.log"
        _capture(tmp_path, log_bytes=10, max_bytes=1000, friction_log=friction)
        assert "blackbox_capture_disabled" not in (
            friction.read_text() if friction.exists() else ""
        )


# --------------------------------------------------------------------------- #
# 3. Off stays off
# --------------------------------------------------------------------------- #
class TestStaysOffWhenNotEnabled:
    def test_no_capture_without_env_or_sentinel(self, tmp_path: Path) -> None:
        log, _ = _capture(tmp_path, log_bytes=None, max_bytes=1000, enabled=False)
        assert len(_records(log)) == 0

    def test_disabled_path_consumes_stdin(self, tmp_path: Path) -> None:
        """The off path already does `cat >/dev/null`; without it a piping caller
        can take SIGPIPE. Pin it so the cap work does not remove it."""
        _, proc = _capture(tmp_path, log_bytes=None, max_bytes=1000, enabled=False)
        assert proc.returncode == 0
        assert proc.stderr.strip() == ""

    def test_disabled_by_sentinel_absence_even_with_a_big_cap(self, tmp_path: Path) -> None:
        log, _ = _capture(tmp_path, log_bytes=None, max_bytes=10**9, enabled=False)
        assert len(_records(log)) == 0
