"""One scratch zone, stamped once, read by two REAL processes with two REAL different
`TMPDIR` values -- the proof `tests/test_write_door_parity.py` deliberately avoids.

THE DEFECT. `writ/session/project_boundary.py::in_scratch_zone` used to resolve
`os.path.realpath(tempfile.gettempdir())` fresh on every call. The write gate runs in two
OS processes -- the daemon, and the CLI subprocess `hooks/scripts/writ-bash-write-gate.sh`
shells out to when the daemon is unreachable -- and `tempfile.gettempdir()` is a per-process
answer. Measured through the real gate on identical session state: `can_write=false` with
`[ENF-GATE-PLAN]` in one process, `can_write=true` in the other, for the same target.

THE FIX this module proves: the zone is stamped into the session cache once, at `mode set`
(`mode_engine._apply_mode_set`, beside `project_root`), and both doors read the stamp
(`project_boundary.scratch_zone`) instead of resolving `tempfile.gettempdir()` themselves.
`in_scratch_zone(target, root, zone)` becomes a pure three-argument predicate; the module
imports no `tempfile` at all.

WHY THIS FILE EXISTS AND NOT ANOTHER ONE. `tests/test_write_door_parity.py`'s `doors`
fixture sets BOTH `TMPDIR` and `tempfile.tempdir` to the SAME directory, and its own
comment says otherwise "every row would agree for the wrong reason" -- that neutralization
was correct for what that file measures (Bash-door / Write-door agreement) and it is the
reason the two-process divergence survived undetected. This module drives the daemon and
the CLI fallback as two REAL OS processes with two REAL different `TMPDIR` values instead.

THE TWO MEASURED PRECONDITIONS, both enforced as fixture assertions so the proof cannot go
vacuous:
  1. `tempfile.gettempdir()` only honours `TMPDIR` when that directory EXISTS and is
     WRITABLE; otherwise it silently falls through its candidate list. Every zone here is
     created and probed as real, writable storage.
  2. A zone NESTED inside another does not diverge: a target under a zone nested inside
     `/tmp` is ALSO "inside /tmp", so a process with no TMPDIR at all (which falls back to
     `/tmp`) would agree with it by accident. `tempfile.gettempdir()`'s POSIX candidate list
     checks `/tmp` before `/var/tmp`, so every zone this module mints lives under `/var/tmp`
     -- outside the default fallback -- and `TestTheProbeIsNonVacuous` asserts the three
     zones are real, writable, mutually non-nested, and produce three DIFFERENT
     `gettempdir()` answers under three real subprocess environments.

THE DISCRIMINATING PAIR (capabilities 2 and 3) is what makes agreement meaningful rather
than coincidental, because "both deny" is agreement for the wrong reason:
  - a target under `zone_s` (the STAMPED zone, neither process's own live TMPDIR) must
    ALLOW on both doors;
  - a target under `zone_b` (the CLI's OWN live TMPDIR) but outside `zone_s` must DENY on
    both doors. Pre-fix, the CLI resolves its own TMPDIR and allows this target while the
    daemon (TMPDIR=zone_a) denies it -- the measured defect, in the direction that converts
    a DENY into an ALLOW.
Both rows compare the `reason` string too, not only the boolean: a shared `false` reached
through two different tags is still two behaviours.

THE CHAIN, NOT JUST THE PRODUCER. `stamped_session` runs the REAL `mode set` subprocess,
asserts the cache it produced, and every consuming test reads THAT cache through the daemon
and the CLI -- never a hand-written `scratch_zone` value -- so a fix that stamps correctly
but a consumer that discards the value cannot read green here.

THE TRAPS THIS HARNESS AVOIDS:
  - The daemon is driven over its OWN `socket_path` (`tests/_daemon.py::start_isolated_daemon`),
    never `base_url` alone: `bin/lib/writ_daemon_client.py` prefers an existing unix socket
    over `base_url`, and a test that named a dead port once wrote real audit rows into the
    operator's interactive daemon.
  - The CLI fallback is the EXACT command `hooks/scripts/writ-bash-write-gate.sh:3291`
    reaches (via `_writ_session can-write` in `bin/lib/common.sh`, on its
    daemon-unreachable branch): `python3 bin/lib/writ-session.py can-write <sid>
    --skill-dir <skill>` with the tool envelope on stdin. `TestTheCliInvocationMatchesTheRealFallback`
    pins that this harness is still running that command and not a hand-rolled equivalent.
  - `stop_isolated_daemon`'s verdict is asserted at teardown, not discarded.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests._daemon import start_isolated_daemon, stop_isolated_daemon

SKILL_ROOT = Path(__file__).resolve().parent.parent
HELPER = SKILL_ROOT / "bin" / "lib" / "writ-session.py"
HOOK_SH = SKILL_ROOT / "hooks" / "scripts" / "writ-bash-write-gate.sh"
COMMON_SH = SKILL_ROOT / "bin" / "lib" / "common.sh"

_LIB = str(SKILL_ROOT / "bin" / "lib")
if _LIB not in sys.path:
    sys.path.insert(0, _LIB)
import writ_daemon_client  # noqa: E402


# --------------------------------------------------------------------------------- #
# Shared helpers.
# --------------------------------------------------------------------------------- #


def _envelope(path: str) -> dict:
    return {"tool_input": {"file_path": path}}


def _cache_file(cache_dir: Path, sid: str) -> Path:
    return Path(cache_dir) / f"writ-session-{sid}.json"


def _write_raw_cache(cache_dir: Path, sid: str, data: dict) -> None:
    """A raw cache write, bypassing `writ.session.cache._write_cache`'s own defaults --
    this is how the fixture below plants an 'older cache' that never carried the
    `scratch_zone` key, the shape `_read_cache`'s backfill exists to handle."""
    _cache_file(cache_dir, sid).write_text(json.dumps(data))


def _read_raw_cache(cache_dir: Path, sid: str) -> dict:
    """Read the cache file directly off disk -- the ARTIFACT the daemon and the CLI both
    actually read -- rather than through `_read_cache`'s own backfill, so a producer-side
    assertion cannot be satisfied by the reader's defaults papering over a stamp that was
    never written."""
    return json.loads(_cache_file(cache_dir, sid).read_text())


def _none_nests(*dirs: Path) -> bool:
    reals = [os.path.realpath(str(d)) for d in dirs]
    for i, a in enumerate(reals):
        for j, b in enumerate(reals):
            if i == j:
                continue
            if a == b or a.startswith(b + os.sep) or b.startswith(a + os.sep):
                return False
    return True


def _base_env(cache_dir: Path, log_root: Path) -> dict:
    """The environment every `mode set` / CLI-fallback subprocess in this module shares,
    minus `TMPDIR` (each caller sets its own). `WRIT_CACHE_DIR` is the one value all three
    processes -- the daemon, the CLI fallback, and `mode set` -- must agree on: it is what
    makes "the same cache" a fact this module can assert rather than assume."""
    env = dict(os.environ)
    env.pop("TMPDIR", None)
    env["WRIT_CACHE_DIR"] = str(cache_dir)
    env["WRIT_LOG_ROOT"] = str(log_root)
    env["WRIT_FRICTION_LOG"] = str(log_root / "workflow-friction.log")
    return env


def _gettempdir_probe(env: dict) -> str:
    """`os.path.realpath(tempfile.gettempdir())` as seen by a real subprocess run under
    `env` -- the anti-vacuity probe capability 4 requires."""
    p = subprocess.run(
        [sys.executable, "-c",
         "import os, tempfile; print(os.path.realpath(tempfile.gettempdir()))"],
        env=env, capture_output=True, text=True, timeout=15,
    )
    assert p.returncode == 0, f"gettempdir probe failed: {p.stderr}"
    return p.stdout.strip()


def _run_mode_set(project_root: Path, sid: str, cache_dir: Path, log_root: Path,
                  zone: Path) -> subprocess.CompletedProcess:
    """`python3 bin/lib/writ-session.py mode set work <sid>`, cwd=`project_root`,
    TMPDIR=`zone` -- the real command that stamps `cache["scratch_zone"]`
    (`mode_engine._apply_mode_set`, beside `cache["project_root"] = os.getcwd()`)."""
    env = _base_env(cache_dir, log_root)
    env["TMPDIR"] = str(zone)
    return subprocess.run(
        [sys.executable, str(HELPER), "mode", "set", "work", sid],
        cwd=str(project_root), env=env, capture_output=True, text=True, timeout=30,
    )


def _run_can_write_cli(sid: str, target: str, cache_dir: Path, log_root: Path,
                       zone: Path) -> dict:
    """The exact CLI fallback `hooks/scripts/writ-bash-write-gate.sh:3291` reaches when the
    daemon is unreachable: `python3 bin/lib/writ-session.py can-write <sid> --skill-dir
    <skill>` with the tool envelope on stdin, in a subprocess whose own live TMPDIR is
    `zone`. Normalizes `cmd_can_write`'s `{"decision": ...}` shape to `{"can_write":
    bool, "reason": str|None}` so it compares directly against the daemon's response."""
    env = _base_env(cache_dir, log_root)
    env["TMPDIR"] = str(zone)
    p = subprocess.run(
        [sys.executable, str(HELPER), "can-write", sid, "--skill-dir", str(SKILL_ROOT)],
        input=json.dumps(_envelope(target)), env=env, cwd=str(SKILL_ROOT),
        capture_output=True, text=True, timeout=30,
    )
    assert p.returncode == 0, (
        f"the can-write CLI must exit 0; got {p.returncode}\n{p.stderr[-2000:]}"
    )
    decision = json.loads(p.stdout.strip())
    return {
        "can_write": decision.get("decision") != "deny",
        "reason": decision.get("reason"),
    }


def _daemon_can_write(daemon: dict, sid: str, target: str) -> dict:
    """The daemon's verdict, over its OWN socket_path -- never `base_url` alone, per this
    repo's keystone that the client prefers an existing unix socket over `base_url`."""
    status, text = writ_daemon_client.post_json(
        f"/session/{sid}/can-write",
        {"tool_input": {"file_path": target}, "skill_dir": str(SKILL_ROOT)},
        socket_path=daemon["socket_path"], base_url=daemon["base_url"], timeout=5.0,
    )
    assert status == 200, f"daemon returned {status}: {text[:300]}"
    body = json.loads(text)
    return {"can_write": bool(body.get("can_write")), "reason": body.get("reason")}


# --------------------------------------------------------------------------------- #
# Fixtures: three real, mutually non-nested zones; a project; a shared cache; a daemon.
# --------------------------------------------------------------------------------- #


@pytest.fixture(scope="module")
def zones():
    """Three real, writable, mutually non-nested scratch-zone candidates, all rooted
    under `/var/tmp` rather than under pytest's own `tmp_path` (which lives under `/tmp`,
    the OS default candidate `tempfile.gettempdir()` falls back to with no TMPDIR set at
    all -- a zone nested there would not diverge, which is exactly the trap the first
    manual probe of this defect hit)."""
    base = Path(tempfile.mkdtemp(prefix="writ-scratch-parity-", dir="/var/tmp"))
    zone_s = base / "zone-s-stamped"
    zone_a = base / "zone-a-daemon"
    zone_b = base / "zone-b-cli"
    for z in (zone_s, zone_a, zone_b):
        z.mkdir()
    try:
        yield SimpleNamespace(s=zone_s, a=zone_a, b=zone_b, base=base)
    finally:
        shutil.rmtree(base, ignore_errors=True)


@pytest.fixture(scope="module")
def project_root(tmp_path_factory):
    root = tmp_path_factory.mktemp("scratch-parity-project")
    (root / ".git").mkdir()
    return root


@pytest.fixture(scope="module")
def cache_dir(tmp_path_factory):
    return tmp_path_factory.mktemp("scratch-parity-cache")


@pytest.fixture(scope="module")
def log_root(tmp_path_factory):
    d = tmp_path_factory.mktemp("scratch-parity-logs")
    (d / "logs").mkdir()
    return d / "logs"


@pytest.fixture(scope="module")
def own_daemon(zones, cache_dir, tmp_path_factory):
    """This module's own daemon, its live TMPDIR pinned to `zones.a` -- the environment
    it inherits, per plan.md's "What proves it".

    `start_isolated_daemon` reads `os.environ` directly and has no parameter for a
    variable it does not itself manage, so TMPDIR is set on THIS process around the
    launcher call only, in a try/finally, and restored immediately after -- no other
    fixture in this module observes a moved default temp directory.
    """
    tmp_dir = tmp_path_factory.mktemp("scratch-parity-daemon")
    log_root_ = tmp_dir / "logs"
    old_tmpdir = os.environ.get("TMPDIR")
    os.environ["TMPDIR"] = str(zones.a)
    try:
        daemon = start_isolated_daemon(
            log_root=str(log_root_),
            socket_path=str(tmp_dir / "writ.sock"),
            cache_dir=str(cache_dir),
            tcp_readonly=True,
        )
    finally:
        if old_tmpdir is None:
            os.environ.pop("TMPDIR", None)
        else:
            os.environ["TMPDIR"] = old_tmpdir
    if not daemon.get("started"):
        pytest.skip(daemon["reason"])
    yield daemon
    verdict = stop_isolated_daemon(daemon)
    assert verdict["stopped"], (
        f"own_daemon teardown could not stop the isolated daemon on port "
        f"{verdict.get('port')}: surviving pids {verdict.get('pids')}, "
        f"health {verdict.get('health')!r}"
    )


@pytest.fixture()
def stamped_session(project_root, cache_dir, log_root, zones) -> str:
    """A fresh session whose cache carries a REAL `scratch_zone` stamp, produced by the
    REAL `mode set` subprocess -- the producer half of the chain this module proves.
    Every consuming test reads THIS cache through the daemon and the CLI; none hand-writes
    the `scratch_zone` value directly."""
    sid = f"scratch-parity-{uuid.uuid4().hex[:8]}"
    result = _run_mode_set(project_root, sid, cache_dir, log_root, zones.s)
    assert result.returncode == 0, (
        f"mode set must exit 0; got {result.returncode}\n{result.stderr[-2000:]}"
    )
    cache = _read_raw_cache(cache_dir, sid)
    assert cache.get("scratch_zone") == os.path.realpath(str(zones.s)), (
        f"the fixture's own producer step failed to stamp the zone; cache={cache}"
    )
    return sid


@pytest.fixture()
def unstamped_session(project_root, cache_dir) -> str:
    """An 'older' cache: `project_root` recorded, `scratch_zone` OMITTED entirely -- what
    a cache written before this field existed looks like on disk."""
    sid = f"scratch-parity-nozone-{uuid.uuid4().hex[:8]}"
    _write_raw_cache(cache_dir, sid, {
        "mode": "work",
        "current_phase": None,
        "gates_approved": [],
        "gates_approved_plan": {},
        "project_root": str(project_root),
        "is_subagent": False,
    })
    raw = _read_raw_cache(cache_dir, sid)
    assert "scratch_zone" not in raw, (
        "fixture setup must OMIT scratch_zone, not merely empty it, to model a cache "
        "written before the field existed"
    )
    return sid


# --------------------------------------------------------------------------------- #
# Capability 4: the probe is non-vacuous.
# --------------------------------------------------------------------------------- #


class TestTheProbeIsNonVacuous:
    def test_the_three_zones_exist_are_writable_and_mutually_non_nested(self, zones) -> None:
        for z in (zones.s, zones.a, zones.b):
            assert z.is_dir(), f"{z} must exist"
            probe = z / ".writ-write-probe"
            probe.write_text("x")
            assert probe.read_text() == "x"
            probe.unlink()
        assert _none_nests(zones.s, zones.a, zones.b), (
            f"the three zones must not contain one another, or a target under the "
            f"nested one would also be 'inside' the ancestor and every row would agree "
            f"for the wrong reason: {zones.s}, {zones.a}, {zones.b}"
        )

    def test_gettempdir_reports_three_different_directories(
        self, zones, cache_dir, log_root
    ) -> None:
        env_s = _base_env(cache_dir, log_root)
        env_s["TMPDIR"] = str(zones.s)
        env_a = _base_env(cache_dir, log_root)
        env_a["TMPDIR"] = str(zones.a)
        env_b = _base_env(cache_dir, log_root)
        env_b["TMPDIR"] = str(zones.b)

        reported = {
            "s": _gettempdir_probe(env_s),
            "a": _gettempdir_probe(env_a),
            "b": _gettempdir_probe(env_b),
        }
        assert len(set(reported.values())) == 3, (
            f"the probe environments collapsed onto fewer than three real directories: "
            f"{reported}; if this ever passes with fewer than three, every parity "
            f"assertion below would be passing vacuously"
        )
        assert reported["s"] == os.path.realpath(str(zones.s))
        assert reported["a"] == os.path.realpath(str(zones.a))
        assert reported["b"] == os.path.realpath(str(zones.b))


# --------------------------------------------------------------------------------- #
# Capability 1: `mode set` stamps the zone.
# --------------------------------------------------------------------------------- #


class TestModeSetStampsTheZone:
    def test_mode_set_stamps_scratch_zone_into_the_cache(
        self, project_root, cache_dir, log_root, zones
    ) -> None:
        sid = f"scratch-parity-stamp-{uuid.uuid4().hex[:8]}"
        result = _run_mode_set(project_root, sid, cache_dir, log_root, zones.s)
        assert result.returncode == 0, (
            f"mode set must exit 0; got {result.returncode}\n{result.stderr[-2000:]}"
        )
        cache = _read_raw_cache(cache_dir, sid)
        assert cache.get("scratch_zone") == os.path.realpath(str(zones.s)), (
            f"expected scratch_zone stamped to realpath(TMPDIR={zones.s}); "
            f"got {cache.get('scratch_zone')!r} from cache {cache}"
        )
        assert os.path.realpath(str(cache.get("project_root") or "")) == \
            os.path.realpath(str(project_root)), (
            f"project_root must still be stamped beside scratch_zone, unchanged: {cache}"
        )

    def test_a_second_mode_set_with_a_different_tmpdir_restamps(
        self, project_root, cache_dir, log_root, zones
    ) -> None:
        """`mode set` re-stamps on every call: the process that declares the mode is the
        one whose answer wins, every time it is run."""
        sid = f"scratch-parity-restamp-{uuid.uuid4().hex[:8]}"
        first = _run_mode_set(project_root, sid, cache_dir, log_root, zones.s)
        assert first.returncode == 0
        second = _run_mode_set(project_root, sid, cache_dir, log_root, zones.a)
        assert second.returncode == 0
        cache = _read_raw_cache(cache_dir, sid)
        assert cache.get("scratch_zone") == os.path.realpath(str(zones.a)), (
            f"a second mode set with a different TMPDIR must overwrite the earlier "
            f"stamp, not keep the first: {cache}"
        )


# --------------------------------------------------------------------------------- #
# Capabilities 2 and 3: both processes read the stamp -- the discriminating pair.
# --------------------------------------------------------------------------------- #


class TestBothProcessesReadTheStamp:
    def test_a_target_under_the_stamped_zone_allows_on_both_processes_with_the_same_reason(
        self, own_daemon, stamped_session, cache_dir, log_root, zones
    ) -> None:
        target = str(zones.s / "scratch-write.py")
        daemon_verdict = _daemon_can_write(own_daemon, stamped_session, target)
        cli_verdict = _run_can_write_cli(stamped_session, target, cache_dir, log_root, zones.b)

        assert daemon_verdict["can_write"] is True, (
            f"the daemon (its own live TMPDIR={zones.a}) denied a target under the "
            f"STAMPED zone {zones.s}: {daemon_verdict}"
        )
        assert cli_verdict["can_write"] is True, (
            f"the CLI fallback (its own live TMPDIR={zones.b}) denied a target under "
            f"the STAMPED zone {zones.s}: {cli_verdict}. Under today's code neither "
            f"process resolves zone_s at all, so both deny -- this is the direction "
            f"the fix must flip."
        )
        assert daemon_verdict["reason"] == cli_verdict["reason"], (
            f"both processes allowed, but for different reasons: "
            f"daemon={daemon_verdict!r} cli={cli_verdict!r}"
        )

    def test_a_target_under_the_clis_own_tmpdir_outside_the_stamp_denies_on_both(
        self, own_daemon, stamped_session, cache_dir, log_root, zones
    ) -> None:
        target = str(zones.b / "scratch-write.py")
        daemon_verdict = _daemon_can_write(own_daemon, stamped_session, target)
        cli_verdict = _run_can_write_cli(stamped_session, target, cache_dir, log_root, zones.b)

        assert daemon_verdict["can_write"] is False, (
            f"the daemon allowed a target outside the stamped zone: {daemon_verdict}"
        )
        assert cli_verdict["can_write"] is False, (
            f"THE MEASURED DEFECT'S DIRECTION: pre-fix, the CLI fallback resolves its "
            f"own live TMPDIR={zones.b} and allows a target under it even though the "
            f"session's stamped zone ({zones.s}) is elsewhere. Got {cli_verdict}"
        )
        assert "ENF-GATE-PLAN" in (daemon_verdict["reason"] or ""), daemon_verdict
        assert daemon_verdict["reason"] == cli_verdict["reason"], (
            f"both processes denied, but for different reasons: "
            f"daemon={daemon_verdict!r} cli={cli_verdict!r}"
        )


# --------------------------------------------------------------------------------- #
# Capability 5: an absent stamp fails closed identically in both processes.
# --------------------------------------------------------------------------------- #


class TestAbsentStampFailsClosedIdentically:
    def test_daemon_does_not_fall_back_to_its_own_live_tmpdir(
        self, own_daemon, unstamped_session, zones
    ) -> None:
        target = str(zones.a / "scratch-write.py")
        verdict = _daemon_can_write(own_daemon, unstamped_session, target)
        assert verdict["can_write"] is False, (
            f"an absent stamp must not fall back to the daemon's own live "
            f"TMPDIR={zones.a}: {verdict}"
        )

    def test_cli_does_not_fall_back_to_its_own_live_tmpdir(
        self, unstamped_session, cache_dir, log_root, zones
    ) -> None:
        target = str(zones.b / "scratch-write.py")
        verdict = _run_can_write_cli(unstamped_session, target, cache_dir, log_root, zones.b)
        assert verdict["can_write"] is False, (
            f"an absent stamp must not fall back to the CLI's own live "
            f"TMPDIR={zones.b}: {verdict}"
        )

    def test_both_processes_deny_with_the_identical_reason(
        self, own_daemon, unstamped_session, cache_dir, log_root, zones
    ) -> None:
        daemon_verdict = _daemon_can_write(
            own_daemon, unstamped_session, str(zones.a / "x.py")
        )
        cli_verdict = _run_can_write_cli(
            unstamped_session, str(zones.b / "x.py"), cache_dir, log_root, zones.b
        )
        assert daemon_verdict["can_write"] is False
        assert cli_verdict["can_write"] is False
        assert daemon_verdict["reason"] == cli_verdict["reason"], (
            f"an absent stamp must fail closed IDENTICALLY, not merely both-deny: "
            f"daemon={daemon_verdict!r} cli={cli_verdict!r}"
        )


# --------------------------------------------------------------------------------- #
# Capability 6: the absent-stamp refusal names the repair.
# --------------------------------------------------------------------------------- #


class TestAbsentStampRefusalNamesTheRepair:
    """Targets under `tests/` inside a zone are EXCLUDED (`*/tests/*` in
    gate-categories.json), so the refusal these tests read comes from
    `_check_project_boundary` / `boundary_refusal`, the one place the
    'need no declaration' / repair sentence is chosen -- not the generic
    `[ENF-GATE-PLAN]` static string the other capability-5 tests above assert on."""

    def test_the_daemons_refusal_names_mode_set_as_the_repair(
        self, own_daemon, unstamped_session, zones
    ) -> None:
        target = str(zones.a / "tests" / "x.py")
        verdict = _daemon_can_write(own_daemon, unstamped_session, target)
        reason = verdict["reason"] or ""
        assert verdict["can_write"] is False, verdict
        assert "mode set work" in reason, (
            f"refusal must name the repair command verbatim: {reason!r}"
        )
        assert "re-stamp" in reason, (
            f"refusal must say what re-running mode set DOES: {reason!r}"
        )
        assert "need no declaration" not in reason, (
            f"an absent-stamp refusal must not repeat the sentence that is only true "
            f"when a zone IS recorded: {reason!r}"
        )

    def test_the_cli_refusal_names_mode_set_as_the_repair(
        self, unstamped_session, cache_dir, log_root, zones
    ) -> None:
        target = str(zones.b / "tests" / "x.py")
        verdict = _run_can_write_cli(unstamped_session, target, cache_dir, log_root, zones.b)
        reason = verdict["reason"] or ""
        assert verdict["can_write"] is False, verdict
        assert "mode set work" in reason
        assert "re-stamp" in reason
        assert "need no declaration" not in reason

    def test_both_processes_choose_the_same_refusal_sentence(
        self, own_daemon, unstamped_session, cache_dir, log_root, zones
    ) -> None:
        """THE TWO TARGETS DIFFER ON PURPOSE, WHICH IS WHY THE COMPARISON IS
        SENTENCE-SHAPED AND NOT STRING EQUALITY.

        Each process is probed with a target under ITS OWN live `TMPDIR` (the daemon's
        `zone_a`, the CLI's `zone_b`). That asymmetry carries the property: given ONE
        shared target, a process still falling back to its own live temp dir could agree
        with the other by accident and this row would pass for the wrong reason. Keeping
        the two targets is the point of the test, so they stay.

        The cost of keeping them is that the two reasons can never be byte-equal.
        `project_boundary._where` names the refused path INSIDE the refusal, a deliberate
        disclosure the rest of this suite pins, so a refusal built for `zone_a/tests/x.py`
        differs from one built for `zone_b/tests/x.py` by exactly those path characters.
        An earlier revision asserted raw `==` on the two reasons and was UNSATISFIABLE ON
        ANY TREE: it failed against a correct implementation and a broken one alike, so it
        distinguished nothing. Measured while fixing it: with kind, root and zone held
        equal and only `file_path` varied, the two strings differ ONLY in the path
        characters and share a 938-character identical tail.

        What is asserted instead is what this test's NAME always claimed: both processes
        chose the same SENTENCE. Each verdict's own requested path (in both spellings, raw
        and resolved, since `_where` appends the resolved one when they differ) is
        replaced by a placeholder in its own reason, and everything else is compared
        byte for byte -- the project root, the declaration instructions, and the whole
        no-zone repair sentence. It still goes RED if one process selects `_SCRATCH`
        ("need no declaration") where the other selects the repair text, which is the
        divergence this class exists to catch; proven by mutation, not by construction.
        DO NOT "simplify" this back into `==` on the raw strings.
        """
        def sentence(reason: str | None, target: str) -> str:
            text = reason or ""
            for spelling in (target, os.path.realpath(target)):
                text = text.replace(spelling, "<TARGET>")
            return text

        daemon_target = str(zones.a / "tests" / "x.py")
        cli_target = str(zones.b / "tests" / "x.py")
        daemon_verdict = _daemon_can_write(own_daemon, unstamped_session, daemon_target)
        cli_verdict = _run_can_write_cli(
            unstamped_session, cli_target, cache_dir, log_root, zones.b
        )
        # BOTH MUST ACTUALLY DENY, or the comparison below is vacuous: under a live
        # per-process fallback both processes ALLOW (each target sits inside the
        # resolving process's own temp dir) and a shared `reason: None` would satisfy
        # any comparison for the wrong reason entirely.
        assert daemon_verdict["can_write"] is False, daemon_verdict
        assert cli_verdict["can_write"] is False, cli_verdict

        daemon_sentence = sentence(daemon_verdict["reason"], daemon_target)
        cli_sentence = sentence(cli_verdict["reason"], cli_target)
        # A deny always carries text; two empty strings would compare equal and prove
        # nothing, so the emptiness is refused rather than silently compared.
        assert daemon_sentence and cli_sentence, (
            f"both refusals must carry text to compare: "
            f"daemon={daemon_verdict!r} cli={cli_verdict!r}"
        )
        assert daemon_sentence == cli_sentence, (
            f"both processes denied, but chose DIFFERENT refusal sentences once each "
            f"one's own requested path is factored out:\n"
            f"  daemon ({daemon_target}) -> {daemon_sentence!r}\n"
            f"  cli    ({cli_target}) -> {cli_sentence!r}"
        )


# --------------------------------------------------------------------------------- #
# Capability 10: the schema key, plus _read_cache's backfill.
# --------------------------------------------------------------------------------- #


class TestDefaultCacheCarriesTheScratchZoneKey:
    def test_default_cache_has_an_empty_scratch_zone(self) -> None:
        from writ.session.cache import _default_cache
        assert _default_cache()["scratch_zone"] == ""

    def test_read_cache_backfills_scratch_zone_on_an_old_cache_file(
        self, cache_dir, monkeypatch
    ) -> None:
        monkeypatch.setenv("WRIT_CACHE_DIR", str(cache_dir))
        from writ.session.cache import _read_cache

        sid = f"scratch-parity-oldcache-{uuid.uuid4().hex[:8]}"
        _write_raw_cache(cache_dir, sid, {"mode": "work"})  # no scratch_zone key at all
        cache = _read_cache(sid)
        assert "scratch_zone" in cache
        assert cache["scratch_zone"] == ""


# --------------------------------------------------------------------------------- #
# Capability 11: a dispatched sub-agent reads the PARENT cache's stamped zone.
# --------------------------------------------------------------------------------- #


class TestDispatchedSubagentReadsTheParentsStamp:
    """The dispatch arm resolves the zone from the PARENT's cache -- the same
    `_read_cache(parent_session_id)` call that already supplies `parent_root` -- and the
    child's own cache stamps nothing, matching `project_root`'s own precedent
    (`subagent_seed` deliberately does not inherit it)."""

    def _child_cache(self, parent_sid: str) -> dict:
        return {
            "is_subagent": True,
            "cache_source": "subagent_start",
            "agent_type": "writ-implementer",
            "role_source": "envelope",
            "role_write_scope": None,
            "parent_session_id": parent_sid,
        }

    def test_a_target_under_the_parents_stamped_zone_is_allowed(
        self, cache_dir, project_root, zones, monkeypatch
    ) -> None:
        monkeypatch.setenv("WRIT_CACHE_DIR", str(cache_dir))
        from writ.session import gates
        from writ.session.cache import _write_cache

        parent_sid = f"scratch-parity-parent-{uuid.uuid4().hex[:8]}"
        _write_cache(parent_sid, {
            "project_root": str(project_root),
            "scratch_zone": os.path.realpath(str(zones.s)),
        })
        child_cache = self._child_cache(parent_sid)
        child_sid = f"scratch-parity-child-{uuid.uuid4().hex[:8]}"
        target = str(zones.s / "child-scratch-write.py")
        result = gates._can_write_check(child_sid, _envelope(target), "", child_cache)
        assert result["can_write"] is True, (
            f"a dispatched sub-agent must be exempt for a target inside the PARENT's "
            f"stamped zone: {result}"
        )

    def test_the_childs_own_cache_carries_no_scratch_zone(
        self, cache_dir, project_root, zones, monkeypatch
    ) -> None:
        monkeypatch.setenv("WRIT_CACHE_DIR", str(cache_dir))
        from writ.session.cache import _read_cache, _write_cache

        parent_sid = f"scratch-parity-parent-{uuid.uuid4().hex[:8]}"
        _write_cache(parent_sid, {
            "project_root": str(project_root),
            "scratch_zone": os.path.realpath(str(zones.s)),
        })
        child_sid = f"scratch-parity-child-{uuid.uuid4().hex[:8]}"
        _write_cache(child_sid, self._child_cache(parent_sid))
        assert _read_cache(child_sid).get("scratch_zone") == "", (
            "a dispatched child's own cache must not inherit the parent's scratch_zone "
            "stamp, the same reason it does not inherit project_root"
        )


# --------------------------------------------------------------------------------- #
# The harness itself: pinned against drifting from the real fallback it claims to run.
# --------------------------------------------------------------------------------- #


class TestTheCliInvocationMatchesTheRealFallback:
    """A test that models one of the two paths it compares is this repo's recorded
    parity failure (the cross-path parity guard that hand-rolled one route and proved
    only that the CLI matched a MODEL of it). These pins assert the shell code this
    module's `_run_can_write_cli` claims to be running still says what this docstring
    claims it says, so a rewrite of either script surfaces here rather than silently
    leaving this harness testing a path production no longer takes."""

    def test_the_bash_gate_still_falls_back_through_writ_session_can_write(self) -> None:
        hook_src = HOOK_SH.read_text()
        assert '_writ_session can-write "$SESSION_ID" --skill-dir "$SKILL_DIR"' in hook_src, (
            "writ-bash-write-gate.sh's daemon-unreachable fallback no longer calls "
            "_writ_session can-write; this harness's CLI invocation would then be "
            "modeling a path instead of running it"
        )

    def test_common_sh_can_write_fallback_still_shells_to_writ_session_can_write(self) -> None:
        common_src = COMMON_SH.read_text()
        assert (
            'python3 "$helper" can-write "$session_id" --skill-dir "$cw_skill_dir"'
            in common_src
        ), (
            "bin/lib/common.sh's can-write fallback no longer matches the invocation "
            "this harness drives; update _run_can_write_cli to follow it"
        )
