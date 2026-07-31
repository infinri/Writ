"""The bash side looked for the session cache in /tmp; the package looks in var/session.

Commit 152e722 moved session state off `/tmp` because this machine's tmpfiles.d declares
`D /tmp`, which EMPTIES the directory at boot. The package moved. Three bash copies of the
old default did not, and nothing failed loudly. Proven live before the fix:

    package  _cache_dir()                   -> /home/.../writ/var/session
    hook inline check, WRIT_CACHE_DIR unset -> /tmp  -> FileNotFoundError
    ls /tmp/writ-session-*.json             -> no such file
    real mode: work                         -> inline check reported: EMPTY

Consequences, worst last:

  1. writ-rag-inject.sh's "is the mode truly unset?" check always answered "unset", so the
     hook entered its auto-route branch every turn and printed "the mode is now 'work'"
     while `mode init` correctly declined (it resolves through the package and saw the
     existing mode). The message was unconditional; the write was conditional.
  2. The branch then set CURRENT_MODE to the hint, so the injected always-on bundle,
     methodology channel and rule scoping were chosen for a different mode than the gate
     enforced, for the whole turn.
  3. Both writ-rag-inject.sh and writ_ensure_server EXPORTED WRIT_CACHE_DIR defaulting to
     gettempdir() "so the daemon is born aligned" -- aligned to a directory holding no
     session caches for any session. That is how a gate_decision comes to be logged with
     "mode": null, and it re-creates the boot-wipe 152e722 fixed.

Test split, deliberately:
  * Resolution tests call the bash helpers directly. Pure, no writes, and they run with
    WRIT_CACHE_DIR UNSET, which is the only way to exercise the default that broke.
  * Hook-behavior tests run the hook with WRIT_CACHE_DIR pinned to a tmp dir and an
    unreachable port, mirroring tests/test_rag_inject_recall.py. They must never run
    unpinned: that would write into the developer's real session cache.
"""
from __future__ import annotations

import json
import os
import subprocess
import tempfile
import uuid
from pathlib import Path

import pytest

SKILL = Path(__file__).resolve().parent.parent
COMMON = SKILL / "bin" / "lib" / "common.sh"
HOOK = SKILL / "hooks" / "scripts" / "writ-rag-inject.sh"
SERVER_LIB = SKILL / "scripts" / "lib" / "writ-server-lib.sh"


def _bash(snippet: str, env: dict | None = None) -> str:
    """Run a snippet with common.sh sourced, returning stdout stripped.

    WRIT_CACHE_DIR is dropped from the inherited environment unless the caller sets it
    explicitly, so a test asking about the DEFAULT never inherits the suite's own pin.
    """
    full = f'set -euo pipefail\nsource "{COMMON}"\n{snippet}\n'
    e = {**os.environ, **(env or {})}
    if not (env or {}).get("WRIT_CACHE_DIR"):
        e.pop("WRIT_CACHE_DIR", None)
    r = subprocess.run(["bash", "-c", full], capture_output=True, text=True, timeout=20, env=e)
    assert r.returncode == 0, f"bash failed: {r.stderr}"
    return r.stdout.strip()


def _package_default() -> str:
    from writ.session.cache import _DEFAULT_CACHE_DIR
    return _DEFAULT_CACHE_DIR


class TestResolutionMatchesThePackage:
    """The invariant whose absence caused the defect."""

    def test_the_bash_default_equals_the_package_default(self):
        """Resolved on both sides and compared, not matched as source text."""
        env = {k: v for k, v in os.environ.items() if k != "WRIT_CACHE_DIR"}
        r = subprocess.run(
            ["bash", "-c", f'source "{COMMON}"; writ_session_cache_dir'],
            capture_output=True, text=True, timeout=20, env=env,
        )
        assert r.returncode == 0, r.stderr
        assert r.stdout.strip() == _package_default()

    def test_the_default_is_not_a_temp_directory(self):
        """The whole point of 152e722: tmpfiles.d empties /tmp at boot.

        The exit-code assertion is load-bearing: without it a missing function yields
        empty stdout, which trivially does not start with the temp dir, and this test
        passes against the defect.
        """
        env = {k: v for k, v in os.environ.items() if k != "WRIT_CACHE_DIR"}
        r = subprocess.run(
            ["bash", "-c", f'source "{COMMON}"; writ_session_cache_dir'],
            capture_output=True, text=True, timeout=20, env=env,
        )
        assert r.returncode == 0, f"resolver did not run: {r.stderr}"
        resolved = r.stdout.strip()
        assert resolved, "resolver printed nothing"
        assert not resolved.startswith(tempfile.gettempdir()), (
            f"the session cache default resolves into the temp dir ({resolved}); "
            "it is emptied at boot, which is the mode=None wipe"
        )

    def test_an_explicit_override_still_wins(self):
        """The suite's isolation depends on this precedence; only the fallback changed."""
        out = _bash("writ_session_cache_dir", env={"WRIT_CACHE_DIR": "/some/where/else"})
        assert out == "/some/where/else"


class TestDirectModeRead:
    """The classifier's stdlib-only read: no writ import, no daemon, so it cannot flake."""

    def test_it_reports_the_mode_of_a_seeded_session(self, tmp_path):
        sid = f"cdp-{uuid.uuid4().hex[:8]}"
        (tmp_path / f"writ-session-{sid}.json").write_text(json.dumps({"mode": "work"}))
        out = _bash(f'writ_session_mode_direct "{sid}"',
                    env={"WRIT_CACHE_DIR": str(tmp_path)})
        assert out == "work"

    def test_it_reports_empty_for_a_session_with_no_cache(self, tmp_path):
        out = _bash('writ_session_mode_direct "nope-does-not-exist"',
                    env={"WRIT_CACHE_DIR": str(tmp_path)})
        assert out == ""

    def test_it_reports_empty_when_the_mode_is_null(self, tmp_path):
        sid = f"cdp-{uuid.uuid4().hex[:8]}"
        (tmp_path / f"writ-session-{sid}.json").write_text(json.dumps({"mode": None}))
        out = _bash(f'writ_session_mode_direct "{sid}"',
                    env={"WRIT_CACHE_DIR": str(tmp_path)})
        assert out == ""

    def test_it_does_not_import_the_writ_package(self):
        """Deliberate: the classifier must not flake the way a package read can."""
        body = COMMON.read_text()
        fn = body[body.index("writ_session_mode_direct"):][:600]
        assert "import writ" not in fn and "from writ" not in fn


class TestNoStaleTempDefaultSurvives:
    @pytest.mark.parametrize("path", [HOOK, SERVER_LIB])
    def test_no_cache_dir_default_falls_back_to_a_temp_dir(self, path):
        """Both files defaulted WRIT_CACHE_DIR to gettempdir(); neither may again.

        Comment lines are stripped: the target is what the code DOES, and both files now
        explain the old default in a comment. Asserting over prose would make the comment
        that documents the fix fail the test for the fix.
        """
        code = "\n".join(
            line for line in path.read_text().splitlines()
            if not line.lstrip().startswith("#")
        )
        assert "gettempdir" not in code, (
            f"{path.name} still derives a directory from gettempdir(); the session cache "
            "must resolve to the durable var/session default"
        )

    def test_both_pin_sites_use_the_shared_resolver(self):
        """One definition, so the package moving cannot silently orphan a copy again."""
        for path in (HOOK, SERVER_LIB):
            assert "writ_session_cache_dir" in path.read_text(), (
                f"{path.name} must pin WRIT_CACHE_DIR via the shared resolver"
            )

    def test_the_hook_still_exports_the_pin(self):
        """FIX-2's contract: a hook-started daemon is born aligned. Only the value changes."""
        assert "export WRIT_CACHE_DIR" in HOOK.read_text()


def _envelope(session_id: str, prompt: str) -> str:
    return json.dumps({
        "session_id": session_id,
        "hook_event_name": "UserPromptSubmit",
        "prompt": prompt,
    })


def _run_hook(envelope: str, cache_dir: str) -> subprocess.CompletedProcess:
    """Unreachable port so the hook fails open without a daemon; cache dir pinned.

    WRIT_CACHE_DIR is ALWAYS set here. Running the hook unpinned would write into the
    developer's real session cache.
    """
    env = {**os.environ,
           "WRIT_CACHE_DIR": cache_dir,
           "WRIT_PORT": "19999",
           "WRIT_HOST": "localhost"}
    return subprocess.run(
        ["bash", str(HOOK)], input=envelope, capture_output=True, text=True,
        cwd=str(SKILL), env=env, timeout=25,
    )


AUTOROUTE_MARKER = "work mode set automatically"


class TestTheHookNeverClaimsAModeChangeItDidNotMake:
    """GUARDS, not red tests, and the reason is the defect itself.

    These pin that the auto-route feature works and does not announce a change it did not
    make. They pass before the fix too, because setting WRIT_CACHE_DIR is exactly what
    MASKS the resolution bug: the pre-fix inline check reads
    `WRIT_CACHE_DIR or gettempdir()`, so a pinned dir takes the correct branch. The bug is
    only reachable with WRIT_CACHE_DIR unset, which would mean writing into the developer's
    real session cache, so it is not reproduced here.

    That is also why the existing suite never caught this: every hook test pins the cache
    dir. The regression test for the actual defect is
    TestResolutionMatchesThePackage::test_the_bash_default_equals_the_package_default,
    which runs unpinned and touches nothing.
    """

    def test_it_says_nothing_about_setting_the_mode_when_one_is_already_set(self, tmp_path):
        """The observable symptom: message unconditional, write conditional."""
        cache = tmp_path / "cache"
        cache.mkdir()
        sid = f"cdp-{uuid.uuid4().hex[:8]}"
        (cache / f"writ-session-{sid}.json").write_text(json.dumps({"mode": "conversation"}))

        r = _run_hook(_envelope(sid, "implement the parser changes"), str(cache))
        assert AUTOROUTE_MARKER not in r.stdout, (
            "the hook announced a mode change while a mode was already set; "
            f"stdout:\n{r.stdout[:600]}"
        )

    def test_the_existing_mode_is_left_intact(self, tmp_path):
        cache = tmp_path / "cache"
        cache.mkdir()
        sid = f"cdp-{uuid.uuid4().hex[:8]}"
        f = cache / f"writ-session-{sid}.json"
        f.write_text(json.dumps({"mode": "conversation"}))

        _run_hook(_envelope(sid, "implement the parser changes"), str(cache))
        assert json.loads(f.read_text()).get("mode") == "conversation"

    def test_auto_routing_still_works_when_no_mode_is_set(self, tmp_path):
        """The fix must not disable the feature, only stop it lying."""
        cache = tmp_path / "cache"
        cache.mkdir()
        sid = f"cdp-{uuid.uuid4().hex[:8]}"

        r = _run_hook(_envelope(sid, "implement the parser changes"), str(cache))
        f = cache / f"writ-session-{sid}.json"
        assert f.exists(), "auto-route should have created the session cache"
        assert json.loads(f.read_text()).get("mode") == "work", (
            f"expected auto-route to set work mode; stdout:\n{r.stdout[:600]}"
        )
        assert AUTOROUTE_MARKER in r.stdout, (
            "when the mode IS set by auto-route, the hook must say so"
        )
