"""Cycle E2c skeletons: every bash client on the socket, and no way to forget it.

E2a taught `common.sh` about the daemon's unix socket. E2b moved the three python
callers. The census still reported state-touching TCP writes arriving minutes ago,
and the cause was not the one I gave the user.

WHAT I GOT WRONG, recorded because the correction is the reason this cycle exists.
I claimed the blocker was E2a's guard treating a DEFAULTED `WRIT_HOST`/`WRIT_PORT`
as an explicit override. Comparing, per file, the line that sources `common.sh`
against the line that assigns the variable disproves it: in all six hooks that
source it the assignment comes AFTER (15/21, 19/23, 16/23, 18/23, 25/33, 18/20), so
`WRIT_CURL_TRANSPORT` is populated correctly in every one. Confirmed at runtime:
`_writ_session can-write` adds ZERO census rows.

THE REAL CAUSE is plainer. E2a added the transport to `common.sh`'s own invocations
and never touched the hooks that build their OWN curl commands.
`writ-bash-write-gate.sh` (can-write) and `writ-memory-capture.sh` (/memory-record) are the two still
writing, which matches the census exactly: `can-write` at 21:43 and `/memory-record`
at 21:44.

WHY THE COMPLETENESS TEST IS THE POINT. Every one of these sites was invisible to
E2a because nothing asserted completeness: the E2a test read `common.sh` alone. A
rule that walks every hook sourcing `common.sh` and fails on a daemon curl without
the transport is what stops the next call site being written on TCP. It is a
source-text rule, which is weaker than behaviour, and it is the right instrument
because the failure mode is an omission at authoring time, not a wrong result.

Per ENF-GATE-007: skeletons written and approved before implementation.
Per ABS-TESTING-041: the per-hook tests RUN the hooks against a curl shim that
records the real argv, because "did this call carry the transport" is a property of
the command actually issued, not of the source that builds it.
Per TEST-ISOLATE-003: the shim intercepts every curl, so no test reaches the live
daemon, and no test writes to the real socket path.
"""
from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
HOOKS = REPO / "hooks" / "scripts"

# Hooks that source common.sh AND curl the daemon. Each must carry the transport.
# Named rather than globbed so a new hook shows up as a test failure to triage
# instead of being silently swept in.
MIGRATED_HOOKS = (
    "writ-bash-write-gate.sh",
    "writ-memory-capture.sh",
    "writ-rag-inject.sh",
    "writ-subagent-start.sh",
    "validate-rules.sh",
)

# The two forms that count as carrying the transport: the shared variable, or the
# URL-aware wrapper/helper that resolves it.
TRANSPORT_FORMS = ("WRIT_CURL_TRANSPORT", "_writ_transport_for", "_writ_curl_with_fallback")

# Marks a URL as the daemon's rather than some other host's.
# Marks a URL as the daemon's. WRIT_HOST/WRIT_PORT are the commonest form and were
# missing from the first draft, which is why the detector found 4 daemon curls where
# there are 8: `"http://${WRIT_HOST}:${WRIT_PORT}/recall"` matched nothing.
#
# 7474 is Neo4j's browser port, not the daemon's, and writ-rag-inject.sh:56 curls it
# with a literal URL carrying none of these hints, so it stays out without needing an
# exclusion.
_DAEMON_URL_HINTS = ("WRIT_SESSION_BASE", "WRIT_HEALTH_URL", "MEMORY_URL", "COMPANION_URL",
                     "writ_base", "ANALYZE_URL", "WRIT_HOST", "WRIT_PORT", "8765")

_CURL_SHIM = """#!/usr/bin/env bash
# Records the argv of every curl the hook issues, then answers plausibly so the
# hook's own parsing does not change behaviour.
printf '%s\\n' "$*" >> "$WRIT_CURL_LOG"
printf '%s' '{"ok": true, "should_skip": false, "known": true, "mode": "work", "decision": "allow"}'
exit 0
"""


@pytest.fixture()
def curl_shim(tmp_path):
    """A PATH-shadowing curl that logs its arguments. Returns (env, log_path)."""
    bindir = tmp_path / "bin"
    bindir.mkdir()
    shim = bindir / "curl"
    shim.write_text(_CURL_SHIM)
    shim.chmod(0o755)
    log = tmp_path / "curl.log"
    log.write_text("")
    env = {
        **os.environ,
        "PATH": f"{bindir}:{os.environ.get('PATH', '')}",
        "WRIT_CURL_LOG": str(log),
        "WRIT_DIR": str(REPO),
        # A socket path that EXISTS as a socket, so the transport is selected without
        # any daemon behind it. The shim answers, so nothing needs to be listening.
        "WRIT_SOCKET": str(tmp_path / "w.sock"),
    }
    import socket as _socket

    s = _socket.socket(_socket.AF_UNIX, _socket.SOCK_STREAM)
    s.bind(str(tmp_path / "w.sock"))
    yield env, log
    s.close()


def _daemon_curl_lines(source: str) -> list[tuple[int, str]]:
    """(lineno, text) for each curl invocation whose URL looks like the daemon's.

    Multi-line invocations are joined first: every one of these hooks puts the URL on
    a continuation line, which is exactly why a naive line-by-line rule found nothing
    in Cycle E2a.
    """
    lines = source.splitlines()
    joined: list[tuple[int, str]] = []
    buffer, start = "", 0
    for index, raw in enumerate(lines, start=1):
        stripped = raw.strip()
        if not buffer:
            if stripped.startswith("#"):
                continue
            start = index
        buffer = f"{buffer} {stripped}" if buffer else stripped
        if stripped.endswith("\\"):
            buffer = buffer[:-1]
            continue
        if "curl " in buffer and any(h in buffer for h in _DAEMON_URL_HINTS):
            joined.append((start, buffer))
        buffer = ""
    return joined


# --------------------------------------------------------------------------- #
# Capability 8, 9: omitting the transport becomes a test failure
# --------------------------------------------------------------------------- #

class TestEveryDaemonCurlCarriesTheTransport:
    """The rule that would have caught all of this in E2a.

    E2a's completeness test read `common.sh` and nothing else, so five hooks and a
    git hook kept curling the daemon over TCP with no test objecting.
    """

    @pytest.mark.parametrize("hook", MIGRATED_HOOKS)
    def test_the_hook_carries_the_transport_on_every_daemon_curl(self, hook: str) -> None:
        source = (HOOKS / hook).read_text()
        offenders = [
            f"{hook}:{lineno}"
            for lineno, text in _daemon_curl_lines(source)
            if not any(form in text for form in TRANSPORT_FORMS)
        ]
        assert not offenders, (
            f"daemon curls with no transport: {offenders}. Add "
            "${WRIT_CURL_TRANSPORT} or route through writ_http_get/writ_http_post."
        )

    def test_the_detector_sees_the_sites_the_census_named(self) -> None:
        """A completeness rule that matches nothing proves nothing.

        E2a's version compared curl and URL on the SAME line and every one of these
        hooks puts the URL on a continuation line, so it found zero and stayed green.
        This anchors the detector to the two sites the CENSUS independently named
        (`can-write` and `/memory-record`) rather than to a count I guessed: my first
        draft asserted >= 8 against a real 7, which is the same mistake in miniature.
        """
        # ANCHORED TO THE ROUTE, NOT THE LINE NUMBER. This held two hardcoded line numbers
        # (writ-bash-write-gate.sh:1311, writ-memory-capture.sh:88) and cycle J's guard
        # moved the first one to 1430, failing a test about detector completeness for a
        # reason that has nothing to do with completeness. The census named ROUTES
        # (`can-write` and `/memory-record`); those are what must be found.
        must_see = {
            "writ-bash-write-gate.sh": "can-write",
            "writ-memory-capture.sh": "memory-record",
        }
        # TWO CLAIMS, SPLIT, because tying them together needs a variable resolver:
        # writ-memory-capture.sh sets MEMORY_URL on line 25 and uses "$MEMORY_URL" at the
        # curl on line 88, so no window around the curl contains the route string.
        #   (a) this hook is the one the census named  -> the route appears in the file
        #   (b) the detector is not vacuous here       -> it finds a daemon curl in it
        for hook, route in must_see.items():
            source = (HOOKS / hook).read_text()
            assert route in source, (
                f"{hook} no longer references {route}; the census anchor is stale and this "
                "test needs re-aiming rather than adjusting"
            )
            hits = _daemon_curl_lines(source)
            assert hits, (
                f"the detector found no daemon curl in {hook}, which the census named as a "
                "live TCP writer"
            )
        # And it must see the whole population, not only the two known ones.
        every = {
            hook: [ln for ln, _ in _daemon_curl_lines((HOOKS / hook).read_text())]
            for hook in MIGRATED_HOOKS
        }
        assert all(every.values()), (
            f"the detector found no daemon curl in some hooks: "
            f"{[h for h, v in every.items() if not v]}"
        )

    def test_the_failure_names_the_file_and_line(self) -> None:
        """An offender list without a location makes the next author hunt for it."""
        fake = "\n".join([
            "#!/usr/bin/env bash",
            'source "$SKILL_DIR/bin/lib/common.sh"',
            'curl -sf --connect-timeout 0.2 \\',
            '    -X POST "${WRIT_SESSION_BASE}/session/x/can-write" \\',
            '    -d "$BODY"',
        ])
        offenders = _daemon_curl_lines(fake)
        assert offenders, "the detector missed a multi-line daemon curl"
        lineno, _text = offenders[0]
        assert lineno == 3, f"the reported line is {lineno}, not the curl's own line"


# --------------------------------------------------------------------------- #
# Capability 1, 2: the two hooks the census is still naming
# --------------------------------------------------------------------------- #

class TestTheTwoLiveWriters:
    """`can-write` and `/memory-record`, last seen at 21:43 and 21:44.

    Driven against the curl shim rather than the live daemon: the property is the
    argv the hook issues, and reading it back from a census row would need a real
    daemon plus a log-scrape to say the same thing less directly.
    """

    def test_the_memory_capture_hook_passes_the_transport(self, curl_shim) -> None:
        env, log = curl_shim
        # THE FILE MUST EXIST. The hook filters on `*/.claude/projects/*/memory/*.md`
        # AND parses the file before posting, so a path that merely matches the glob
        # exits before any daemon attempt. Two drafts of this test proved nothing for
        # that reason: the first used a repo path, the second a non-existent one.
        mem = Path(env["WRIT_CURL_LOG"]).parent / ".claude" / "projects" / "p" / "memory"
        mem.mkdir(parents=True, exist_ok=True)
        target = mem / "probe.md"
        target.write_text(
            "---\nname: probe\ndescription: probe\nmetadata:\n  type: project\n---\n\nBody.\n"
        )
        payload = (
            '{"session_id": "e2c-probe", "tool_name": "Write", '
            f'"tool_input": {{"file_path": "{target}"}}}}'
        )
        subprocess.run(
            ["bash", str(HOOKS / "writ-memory-capture.sh")],
            input=payload, cwd=str(REPO), env=env,
            capture_output=True, text=True, timeout=60,
        )
        calls = [c for c in log.read_text().splitlines() if "memory-record" in c]
        assert calls, (
            "the hook issued no /memory-record curl; the probe payload did not reach it"
        )
        assert all("--unix-socket" in c for c in calls), (
            f"/memory-record went over TCP: {calls}"
        )

    def test_the_write_gate_passes_the_transport_on_can_write(self, curl_shim) -> None:
        env, log = curl_shim
        # A write target inside the REPO. A /tmp path is not a project write, so the
        # gate never reaches its can-write branch and the first draft of this test
        # asserted on an empty log.
        payload = (
            '{"tool_name": "Bash", "session_id": "e2c-probe", '
            f'"tool_input": {{"command": "echo hi > {REPO}/probe-e2c.txt"}}}}'
        )
        subprocess.run(
            ["bash", str(HOOKS / "writ-bash-write-gate.sh")],
            input=payload, cwd=str(REPO), env=env,
            capture_output=True, text=True, timeout=60,
        )
        calls = [c for c in log.read_text().splitlines() if "can-write" in c]
        assert calls, "the gate issued no can-write curl; the probe payload missed it"
        assert all("--unix-socket" in c for c in calls), (
            f"can-write went over TCP: {calls}"
        )


# --------------------------------------------------------------------------- #
# Capability 6, 7: post-commit, which cannot borrow the shared variable
# --------------------------------------------------------------------------- #

class TestPostCommitResolvesItsOwnTransport:
    """`hooks/git/post-commit` does NOT source `common.sh`: its header holds it to
    "System python3 and curl only". Same constraint that put the session-id
    comparison in `bin/lib/pointer_session.py` during Cycle B, so it resolves the
    socket itself rather than gaining a bash dependency.
    """

    POST_COMMIT = REPO / "hooks" / "git" / "post-commit"

    def test_it_still_does_not_source_common_sh(self) -> None:
        """Must-not-regress: the constraint that shapes the whole file."""
        source = self.POST_COMMIT.read_text()
        assert "bin/lib/common.sh" not in source, (
            "post-commit now sources common.sh, which its header forbids"
        )

    def test_it_uses_the_socket_when_one_exists(self, curl_shim, tmp_path) -> None:
        env, log = curl_shim
        subprocess.run(
            ["bash", str(self.POST_COMMIT)],
            cwd=str(REPO), env=env, capture_output=True, text=True, timeout=60,
        )
        calls = [c for c in log.read_text().splitlines() if "commit/capture" in c]
        assert calls, "post-commit issued no /commit/capture curl"
        assert all("--unix-socket" in c for c in calls), (
            f"/commit/capture went over TCP with a socket present: {calls}"
        )

    def test_it_uses_tcp_when_no_socket_exists(self, tmp_path) -> None:
        """The ordinary state on a machine whose daemon has not restarted yet, so it
        must not become an error (CLEAN-ERR-001)."""
        bindir = tmp_path / "bin"
        bindir.mkdir()
        shim = bindir / "curl"
        shim.write_text(_CURL_SHIM)
        shim.chmod(0o755)
        log = tmp_path / "curl.log"
        log.write_text("")
        env = {
            **os.environ,
            "PATH": f"{bindir}:{os.environ.get('PATH', '')}",
            "WRIT_CURL_LOG": str(log),
            "WRIT_SOCKET": str(tmp_path / "absent.sock"),
        }
        subprocess.run(
            ["bash", str(self.POST_COMMIT)],
            cwd=str(REPO), env=env, capture_output=True, text=True, timeout=60,
        )
        calls = [c for c in log.read_text().splitlines() if "commit/capture" in c]
        assert calls, "post-commit issued no curl at all without a socket"
        assert all("--unix-socket" not in c for c in calls), (
            f"post-commit passed a socket flag with no socket present: {calls}"
        )


# --------------------------------------------------------------------------- #
# Capability 10, 11: the fallbacks the migration must not break
# --------------------------------------------------------------------------- #

class TestFallbacksSurvive:
    """Every migrated hook keeps its existing behaviour when the daemon is absent.
    A transport change must not convert a graceful degradation into a failure.
    """

    @pytest.mark.parametrize("hook", MIGRATED_HOOKS)
    def test_the_hook_exits_zero_with_no_daemon_and_no_socket(self, hook: str, tmp_path) -> None:
        env = {
            **os.environ,
            "WRIT_DIR": str(REPO),
            "WRIT_SOCKET": str(tmp_path / "absent.sock"),
            "WRIT_PORT": "19998",  # nothing listening
        }
        result = subprocess.run(
            ["bash", str(HOOKS / hook)],
            input='{"session_id": "e2c-probe", "tool_name": "Write", "tool_input": {}}',
            cwd=str(REPO), env=env, capture_output=True, text=True, timeout=90,
        )
        assert result.returncode == 0, (
            f"{hook} exited {result.returncode} with no daemon reachable; "
            f"stderr={result.stderr[:200]}"
        )

    def test_a_stale_socket_does_not_break_a_migrated_hook(self, tmp_path) -> None:
        """The E2b failure mode, re-checked at the hook level: a socket FILE with no
        listener must not stop the hook."""
        import socket as _socket

        stale = tmp_path / "w.sock"
        s = _socket.socket(_socket.AF_UNIX, _socket.SOCK_STREAM)
        s.bind(str(stale))
        s.close()
        env = {
            **os.environ,
            "WRIT_DIR": str(REPO),
            "WRIT_SOCKET": str(stale),
            "WRIT_PORT": "19998",
        }
        result = subprocess.run(
            ["bash", str(HOOKS / "writ-memory-capture.sh")],
            input='{"session_id": "e2c-probe", "tool_name": "Write", "tool_input": {}}',
            cwd=str(REPO), env=env, capture_output=True, text=True, timeout=90,
        )
        assert result.returncode == 0, (
            f"a stale socket made the hook exit {result.returncode}: "
            f"{result.stderr[:200]}"
        )


# --------------------------------------------------------------------------- #
# Capability 12: the measurement that gates the flag
# --------------------------------------------------------------------------- #

class TestTheCensusIsTheGate:
    """The flag is flipped on evidence, not on a belief that the migration is done.

    This test does NOT read the live census: that is an operational check over a real
    session, and asserting on a log this suite also writes to would be circular. What
    it pins is that the evidence path still exists and still names writes only.
    """

    def test_the_census_still_records_a_tcp_write(self, tmp_path, monkeypatch) -> None:
        import json

        monkeypatch.setenv("WRIT_FRICTION_LOG", str(tmp_path / "events.jsonl"))
        from writ.server import transport

        transport.note_request("tcp", "POST", "/session/abc/can-write")
        rows = [
            json.loads(line)
            for line in (tmp_path / "events.jsonl").read_text().splitlines()
            if line.strip()
        ]
        assert any(r.get("event") == "daemon_tcp_write" for r in rows), rows

    def test_the_census_still_ignores_socket_traffic(self, tmp_path, monkeypatch) -> None:
        """Otherwise the migration could never drive the count to zero."""
        import json

        log = tmp_path / "events.jsonl"
        monkeypatch.setenv("WRIT_FRICTION_LOG", str(log))
        from writ.server import transport

        transport.note_request("socket", "POST", "/session/abc/can-write")
        rows = [
            json.loads(line) for line in log.read_text().splitlines() if line.strip()
        ] if log.exists() else []
        assert not [r for r in rows if r.get("event") == "daemon_tcp_write"], rows
