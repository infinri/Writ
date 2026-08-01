"""One owner for the daemon-log path (logging blueprint section 6; doc now in git history only).

`WRIT_LOG` had three defaults, not the two the blueprint counted: the library's
`/tmp/writ-server.log` plus two different caller assignments
(`${WRIT_DATA}/server.log` in the plugin bootstrap, `/tmp/writ-server.log` in
ensure-server / bootstrap / writ-rag-inject). Which file the daemon's stdout landed in
therefore depended on which of five start paths happened to launch it, so "check the
server log" had no single answer.

`writ_default_server_log` in scripts/lib/writ-server-lib.sh is now the only resolver.
Deliberately off /tmp: systemd's tmpfiles.d declares `D /tmp`, which EMPTIES it at boot,
and a daemon log destroyed on every reboot is the one you most want after a
reboot-triggered failure (the same mechanism that lost the session caches).
"""
from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

import pytest

WRIT_ROOT = Path(__file__).resolve().parent.parent
LIB = WRIT_ROOT / "scripts" / "lib" / "writ-server-lib.sh"
START_PATHS = [
    WRIT_ROOT / "scripts" / "ensure-server.sh",
    WRIT_ROOT / "scripts" / "bootstrap.sh",
    WRIT_ROOT / "scripts" / "bootstrap-plugin.sh",
    WRIT_ROOT / "hooks" / "scripts" / "writ-rag-inject.sh",
    WRIT_ROOT / "hooks" / "scripts" / "session-start-bootstrap.sh",
]


def _resolve(**env) -> str:
    """Call the real bash resolver with a controlled environment."""
    clean = {k: "" for k in ("WRIT_LOG", "WRIT_LOG_ROOT", "CLAUDE_PLUGIN_ROOT",
                             "CLAUDE_PLUGIN_DATA", "WRIT_DIR")}
    clean.update(env)
    assignments = " ".join(f'{k}="{v}"' for k, v in clean.items())
    script = f'source "{LIB}"; {assignments} writ_default_server_log'
    r = subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=30,
                       env={**os.environ, **{k: "" for k in clean}})
    assert r.returncode == 0, r.stderr
    return r.stdout.strip()


class TestResolutionOrder:
    def test_explicit_writ_log_wins(self):
        assert _resolve(WRIT_LOG="/custom/x.log") == "/custom/x.log"

    def test_writ_log_root_is_honored(self):
        """The same override the Python router uses, so both land under one root."""
        assert _resolve(WRIT_LOG_ROOT="/logs") == "/logs/server.log"

    def test_plugin_install_uses_the_plugin_data_dir(self):
        """A plugin upgrade rewrites CLAUDE_PLUGIN_ROOT, so the log cannot live under it."""
        got = _resolve(CLAUDE_PLUGIN_ROOT="/p", CLAUDE_PLUGIN_DATA="/pdata")
        assert got == "/pdata/server.log"

    def test_plugin_without_data_dir_falls_back_to_cache(self):
        """Reproduces exactly what session-start-bootstrap.sh used to hardcode."""
        got = _resolve(CLAUDE_PLUGIN_ROOT="/p")
        assert got == f"{os.path.expanduser('~')}/.cache/writ/server.log"

    def test_standalone_derives_from_the_install_dir(self):
        assert _resolve(WRIT_DIR="/opt/writ") == "/opt/writ/var/logs/server.log"

    def test_explicit_beats_every_implicit_source(self):
        got = _resolve(WRIT_LOG="/win.log", WRIT_LOG_ROOT="/logs",
                       CLAUDE_PLUGIN_ROOT="/p", CLAUDE_PLUGIN_DATA="/pdata",
                       WRIT_DIR="/opt/writ")
        assert got == "/win.log"


class TestNeverTmp:
    @pytest.mark.parametrize("env", [
        {},
        {"WRIT_DIR": "/opt/writ"},
        {"CLAUDE_PLUGIN_ROOT": "/p", "CLAUDE_PLUGIN_DATA": "/pdata"},
        {"WRIT_LOG_ROOT": "/logs"},
    ])
    def test_no_implicit_path_lands_in_tmp(self, env):
        """/tmp is emptied at boot by tmpfiles.d; nothing may default there."""
        got = _resolve(**env)
        assert not got.startswith("/tmp/"), f"{env} resolved into /tmp: {got}"


class TestSingleOwner:
    def test_no_start_path_hardcodes_its_own_default(self):
        """Any reintroduced assignment silently re-splits the path across start paths."""
        offenders = []
        for path in START_PATHS:
            for lineno, line in enumerate(path.read_text().splitlines(), 1):
                if re.search(r'^\s*WRIT_LOG=(?!"\$\(writ_default_server_log\)")', line):
                    offenders.append(f"{path.name}:{lineno}: {line.strip()}")
        assert offenders == [], (
            "these set WRIT_LOG directly instead of calling writ_default_server_log:\n  "
            + "\n  ".join(offenders)
        )

    def test_every_start_path_reaches_the_resolver(self):
        """Either it sources the library, or the library it calls resolves for it."""
        for path in START_PATHS:
            text = path.read_text()
            assert "writ-server-lib.sh" in text, (
                f"{path.name} launches the daemon without the shared resolver"
            )


class TestLaunchSafety:
    def test_the_parent_directory_is_created(self):
        """The redirect creates the file, never its directory.

        On a fresh install var/logs does not exist yet, and $CLAUDE_PLUGIN_DATA may not
        exist before bootstrap, so without the mkdir the redirect fails and the daemon
        never starts -- a hard failure, not a degradation.
        """
        text = LIB.read_text()
        assert 'mkdir -p "$(dirname "$WRIT_LOG")"' in text
