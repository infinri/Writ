"""Cycle D skeletons: four guards that report the wrong thing.

  1. The Bash gate matches the raw command TEXT against seven patterns
     (`writ-bash-write-gate.sh:153-158`) and then builds a refusal that always
     interpolates `$STATE_DIR_GUARD` (`:160`) no matter which one fired. So a
     command matching `manual_test_grant` is told it "names Writ gate state
     ('<...>/var/session')" when it named no such thing. That misdirection, not
     the breadth of the match, is why eight refusals went unexplained.
     One pattern is also genuinely too broad: `manual_test_grant` is the
     minter's MODULE name, so it matches `tests/test_manual_test_grant.py` as
     readily as `bin/lib/manual_test_grant.py`.
  2. `assert_safe_to_wipe` (`benchmarks/_corpus_safety.py:33`) refuses only when
     graph-first nodes exist. It never asks WHICH instance it is about to wipe.
     The guard that looks like it covers this is scoped away: `clear_all` calls
     `assert_full_wipe_allowed` only when the preserve set is EMPTY
     (`maintenance_store.py:37-47`), and a bare `clear_all()` preserves
     `RECORD_LABELS`, which is not.
  3. `test-paths-defaults.json:67` sends every session and every project to one
     `/tmp/writ-phpunit-cache`, and `runner_for` (`test_paths.py:147-163`)
     returns the command verbatim with no substitution of any kind.
  4. Nothing detects a half-applied install whose permission allowlist never
     landed. `bootstrap-plugin.sh:267` only WARNS when the patch fails and the
     SessionStart detector keys on the venv (`session-start-bootstrap.sh:66`),
     so venv-present-but-allowlist-absent looks healthy while every read-only
     Writ command prompts. `doctor.py` has 15 checks and none reads permissions.

WHAT IS DELIBERATELY NOT CHANGED. The gate's state-directory arm is not relaxed.
Four tests here are must-not-regress negatives proving it still refuses, because
the only way to admit the commands that tripped it (pytest, git, curl) is to
admit three interpreters that can each write a file. `_readonly_inspection`
(`:133-151`) already allows plain grep/cat/ls pipelines and the Read tool covers
the rest.

Per ENF-GATE-007: skeletons written and approved before implementation.
Per ABS-TESTING-041: the gate tests run the real hook as a real subprocess with
real payloads. A reimplementation of its matcher would only test itself.
Per TEST-ISOLATE-003: every settings file, cache dir and state dir is under
tmp_path. Nothing here reads or writes the developer's real ~/.claude.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
GATE = REPO / "hooks" / "scripts" / "writ-bash-write-gate.sh"
INSTALL_MODULE = REPO / "bin" / "lib" / "writ_install.py"
TEST_PATHS_MODULE = REPO / "bin" / "lib" / "test_paths.py"

# The minter this gate protects, and the test file that shares its stem. The
# first must stay refused; the second must stop being refused. Built from parts
# so this module can be grepped for either without the literals colliding.
_MINTER_STEM = "manual_test_grant"
_MINTER_PATH = f"bin/lib/{_MINTER_STEM}.py"
_MINTER_TEST_PATH = f"tests/test_{_MINTER_STEM}.py"


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

def _gate(command: str, *, cache_dir: str | None = None) -> dict:
    """Run the real Bash gate on `command`; return its parsed decision.

    Returns {} when the hook emitted nothing, which is how it signals "no
    opinion" (the tool proceeds). A deny is the `hookSpecificOutput` envelope
    `emit_deny` writes (`common.sh:857-868`).
    """
    env = dict(os.environ)
    env["WRIT_DIR"] = str(REPO)
    if cache_dir is not None:
        env["WRIT_CACHE_DIR"] = cache_dir
    # session_id is REQUIRED: the hook exits at `[ -z "$SESSION_ID" ] && exit 0`
    # (`:105`) before any arm, so a payload without one silently reaches no gate
    # at all. The first draft of this helper omitted it and made two
    # must-not-regress tests look like failures.
    payload = json.dumps({
        "tool_name": "Bash",
        "session_id": "cycle-d-probe",
        "tool_input": {"command": command},
    })
    proc = subprocess.run(
        ["bash", str(GATE)],
        input=payload, capture_output=True, text=True, env=env, timeout=60,
    )
    for stream in (proc.stdout, proc.stderr):
        for line in stream.splitlines():
            line = line.strip()
            if not line.startswith("{"):
                continue
            try:
                doc = json.loads(line)
            except ValueError:
                continue
            if "hookSpecificOutput" in doc:
                return doc["hookSpecificOutput"]
    return {}


def _decision(command: str, **kw) -> str:
    return _gate(command, **kw).get("permissionDecision", "")


def _reason(command: str, **kw) -> str:
    return _gate(command, **kw).get("permissionDecisionReason", "")


class _FakeResult:
    def __init__(self, row: dict) -> None:
        self._row = row

    async def single(self) -> dict:
        return self._row


class _FakeSession:
    def __init__(self, row: dict) -> None:
        self._row = row

    async def __aenter__(self) -> "_FakeSession":
        return self

    async def __aexit__(self, *exc) -> bool:
        return False

    async def run(self, query: str, **params) -> _FakeResult:
        return _FakeResult(self._row)


class _FakeDriver:
    def __init__(self, row: dict) -> None:
        self._row = row

    def session(self, **kw) -> _FakeSession:
        return _FakeSession(self._row)


class _FakeDb:
    """The two attributes `assert_safe_to_wipe` reads, plus the uri it must start
    reading. Not a mock of the guard: the guard's own logic runs untouched, this
    only stands in for a Neo4j connection so no server is needed to ask which
    instance a refusal is about."""

    def __init__(self, uri: str, graph_first_count: int = 0) -> None:
        self._uri = uri
        self._database = "neo4j"
        self._driver = _FakeDriver({"c": graph_first_count})


def _run_installer(*argv: str) -> subprocess.CompletedProcess:
    """Run the installer CLI, refusing to accept an argparse error as an answer.

    `check-settings` does not exist yet, so argparse exits 2 with "invalid
    choice" and the word "writ" in its usage line. That satisfied every
    assertion in this class, which made four tests pass while proving nothing.
    Any caller that reads a returncode has to rule it out first.
    """
    proc = subprocess.run(
        [sys.executable, str(INSTALL_MODULE), *argv],
        capture_output=True, text=True, timeout=120,
    )
    if "invalid choice" in proc.stderr:
        pytest.fail(f"skeleton: the {argv[0]!r} subcommand does not exist yet")
    return proc


def _installer_entries() -> tuple[tuple[str, ...], tuple[str, ...]]:
    """(BASE_ALLOW, DENY) read from the module that owns them."""
    sys.path.insert(0, str(REPO / "bin" / "lib"))
    try:
        import writ_install

        return writ_install.BASE_ALLOW, writ_install.DENY
    finally:
        sys.path.pop(0)


def _run_test_paths(*argv: str, cwd: Path | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(TEST_PATHS_MODULE), *argv],
        capture_output=True, text=True, timeout=60,
        cwd=str(cwd) if cwd else None,
    )


@pytest.fixture()
def magento_config() -> dict:
    """The bundled defaults, loaded through the production loader."""
    sys.path.insert(0, str(REPO / "bin" / "lib"))
    try:
        import test_paths

        return test_paths.load_config(REPO)
    finally:
        sys.path.pop(0)


def _require_kwarg(fn, name: str) -> None:
    """Legible skeleton failure instead of a TypeError from a missing kwarg."""
    import inspect

    if name not in inspect.signature(fn).parameters:
        pytest.fail(f"skeleton: {fn.__name__}() has no {name} parameter yet")


def _test_paths_module():
    sys.path.insert(0, str(REPO / "bin" / "lib"))
    try:
        import test_paths

        return test_paths
    finally:
        sys.path.pop(0)


# --------------------------------------------------------------------------- #
# Capability 1-5: the gate refuses what it protects, and says what it matched
# --------------------------------------------------------------------------- #

class TestGateStateMatchPrecision:
    """Real hook, real payloads (ABS-TESTING-041).

    Three of these five are must-not-regress: the minter path, a write to the
    state directory, and read-only inspection. Loosening any of them is the
    failure mode this cycle has to avoid, so they are asserted alongside the fix
    rather than after it.
    """

    def test_the_refusal_names_what_actually_matched(self, tmp_path: Path) -> None:
        """A command naming the minter must not be told it named the state dir.

        The state directory is pointed at tmp_path and the command never mentions
        it, so any appearance of that path in the reason is the message reporting
        a trigger that did not fire.
        """
        state = tmp_path / "session-state"
        state.mkdir()
        reason = _reason(f"bash {_MINTER_PATH} --mint", cache_dir=str(state))
        assert reason, "the minter invocation was not refused at all"
        assert str(state) not in reason, (
            "the refusal names the state directory, which this command never "
            f"mentioned: {reason}"
        )
        assert _MINTER_STEM in reason, (
            f"the refusal does not name the pattern that matched: {reason}"
        )

    def test_the_minter_path_is_still_refused(self, tmp_path: Path) -> None:
        state = tmp_path / "session-state"
        state.mkdir()
        assert _decision(f"bash {_MINTER_PATH} --mint", cache_dir=str(state)) == "deny"

    def test_a_test_file_sharing_the_minter_stem_is_allowed(self, tmp_path: Path) -> None:
        """The whole friction class: a test file cannot mint anything.

        `tests/test_<stem>.py` matches the bare module-name pattern today, so a
        pytest run naming it is refused. Reproduced twice this session.
        """
        state = tmp_path / "session-state"
        state.mkdir()
        decision = _decision(
            f".venv/bin/python -m pytest {_MINTER_TEST_PATH} -q", cache_dir=str(state)
        )
        assert decision != "deny", (
            "running the minter's own test file is refused, which is the false "
            "positive this cycle removes"
        )

    def test_a_write_to_the_state_directory_is_still_refused(self, tmp_path: Path) -> None:
        """Must-not-regress: the arm that is deliberately NOT relaxed."""
        state = tmp_path / "session-state"
        state.mkdir()
        decision = _decision(f"echo tampered > {state}/writ-session-abc.json",
                             cache_dir=str(state))
        assert decision == "deny", "a write into gate state was not refused"

    def test_read_only_inspection_of_gate_state_is_still_allowed(self, tmp_path: Path) -> None:
        """Must-not-regress: `_readonly_inspection` (`:133-151`) keeps working."""
        state = tmp_path / "session-state"
        state.mkdir()
        (state / "writ-session-abc.json").write_text("{}")
        decision = _decision(f"cat {state}/writ-session-abc.json", cache_dir=str(state))
        assert decision != "deny", "plain read-only inspection was refused"


# --------------------------------------------------------------------------- #
# Capability 6-8: a destructive benchmark asks which instance it is wiping
# --------------------------------------------------------------------------- #

class TestBenchmarkRequiresDisposableInstance:
    """`clear_all`'s guard is scoped to an EMPTY preserve set
    (`maintenance_store.py:37-47`), and the benchmarks pass a non-empty one, so
    nothing today stops a destructive benchmark from wiping the live corpus. The
    snapshot is recovery; this is prevention.
    """

    @staticmethod
    def _assert_safe_to_wipe():
        sys.path.insert(0, str(REPO / "benchmarks"))
        try:
            import _corpus_safety

            return _corpus_safety.assert_safe_to_wipe
        finally:
            sys.path.pop(0)

    def test_refuses_an_instance_that_is_not_marked_disposable(self, monkeypatch) -> None:
        import asyncio

        monkeypatch.delenv("WRIT_TEST_GRAPH", raising=False)
        guard = self._assert_safe_to_wipe()
        with pytest.raises(RuntimeError) as caught:
            asyncio.run(guard(_FakeDb("bolt://localhost:7687")))
        assert "disposable" in str(caught.value).lower(), caught.value

    def test_the_refusal_carries_the_shared_standing_up_instructions(self, monkeypatch) -> None:
        """DRY-DUP-001: the benchmark refusal and the pytest skip must not tell a
        developer two different things about the same requirement."""
        import asyncio

        from writ.graph.db._safety import how_to_run_safely

        monkeypatch.delenv("WRIT_TEST_GRAPH", raising=False)
        guard = self._assert_safe_to_wipe()
        with pytest.raises(RuntimeError) as caught:
            asyncio.run(guard(_FakeDb("bolt://localhost:7687")))
        expected = how_to_run_safely().splitlines()[0]
        assert expected in str(caught.value), (
            f"refusal does not carry the shared instructions: {caught.value}"
        )

    def test_a_disposable_instance_passes_and_graph_first_nodes_still_refuse(
        self, monkeypatch
    ) -> None:
        """Must-not-regress: the graph-first check keeps working, and a properly
        isolated instance is not blocked."""
        import asyncio

        monkeypatch.setenv("WRIT_TEST_GRAPH", "1")
        guard = self._assert_safe_to_wipe()

        asyncio.run(guard(_FakeDb("bolt://localhost:7688")))

        with pytest.raises(RuntimeError) as caught:
            asyncio.run(guard(_FakeDb("bolt://localhost:7688", graph_first_count=3)))
        assert "graph-first" in str(caught.value), caught.value


# --------------------------------------------------------------------------- #
# Capability 9-11: one PHPUnit cache directory per session
# --------------------------------------------------------------------------- #

class TestPhpunitCacheIsSessionScoped:

    # The bundled glob is `*/app/code/*/Test/Unit/*.php`, so a leading segment is
    # required: a path starting at `app/code` does not match and runner_for
    # correctly returns ("", "").
    _MAGENTO_TEST = "/srv/shop/app/code/Vendor/Mod/Test/Unit/ThingTest.php"

    def test_two_sessions_get_two_cache_directories(self, magento_config) -> None:
        tp = _test_paths_module()
        _require_kwarg(tp.runner_for, "session_id")
        first, _ = tp.runner_for(self._MAGENTO_TEST, magento_config, session_id="sess-A")
        second, _ = tp.runner_for(self._MAGENTO_TEST, magento_config, session_id="sess-B")
        assert "sess-A" in first, first
        assert "sess-B" in second, second
        assert first != second, (
            "both sessions were handed the same PHPUnit cache directory"
        )

    def test_a_command_without_the_placeholder_is_returned_unchanged(
        self, magento_config
    ) -> None:
        """The other five bundled patterns have no placeholder and must not be
        rewritten by the substitution step."""
        tp = _test_paths_module()
        _require_kwarg(tp.runner_for, "session_id")
        cmd, _ = tp.runner_for("src/thing/tests/Unit/ThingTest.php", magento_config,
                               session_id="sess-A")
        assert "{" not in cmd, f"substitution damaged a placeholder-free command: {cmd}"
        assert "sess-A" not in cmd, cmd

    def test_an_absent_session_id_falls_back_to_the_shared_directory(
        self, magento_config
    ) -> None:
        """CLEAN-ERR-001: a test runner must never fail to run for want of a
        cache path, so no id yields the previous shared directory, stated rather
        than silent. This is the one path where the original sharing remains."""
        tp = _test_paths_module()
        cmd, _ = tp.runner_for(self._MAGENTO_TEST, magento_config)
        assert "--cache-directory=" in cmd, cmd
        assert "{session_cache}" not in cmd, (
            f"the placeholder leaked into the command unsubstituted: {cmd}"
        )

    def test_the_cli_accepts_a_session_argument(self, tmp_path: Path) -> None:
        """`writ-run-pending-tests.sh` reaches this only through the CLI, so the
        flag has to exist there, not just in the function signature."""
        proc = _run_test_paths("runner-for", self._MAGENTO_TEST, "--session", "sess-A",
                               cwd=REPO)
        assert proc.returncode == 0, proc.stderr
        assert "sess-A" in proc.stdout, f"stdout={proc.stdout!r} stderr={proc.stderr!r}"

    def test_the_hook_passes_the_session_id_through(self) -> None:
        """The substitution is inert unless the caller supplies an id, and the
        hook is the only production caller."""
        source = (REPO / "hooks" / "scripts" / "writ-run-pending-tests.sh").read_text()
        assert "runner-for" in source
        line = next(ln for ln in source.splitlines() if "runner-for" in ln)
        assert "--session" in line, (
            f"the hook does not pass a session id, so the fix is inert: {line.strip()}"
        )


# --------------------------------------------------------------------------- #
# Capability 12-14: a half-applied install becomes visible
# --------------------------------------------------------------------------- #

class TestPermissionsAllowlistIsDiagnosable:
    """The merge itself is already delivered and covered by 78 passing install
    tests. What is added here is a diagnostic for the PARTIAL state, where the
    venv exists so the SessionStart detector stays quiet.
    """

    def test_check_settings_reports_the_missing_entries(self, tmp_path: Path) -> None:
        target = tmp_path / "settings.json"
        target.write_text(json.dumps({"permissions": {"allow": ["Bash(ls:*)"], "deny": []}}))
        proc = _run_installer("check-settings", "--target", str(target))
        assert proc.returncode != 0, "an unpatched settings file reported as complete"
        report = proc.stdout + proc.stderr
        allow, _ = _installer_entries()
        assert any(entry in report for entry in allow), (
            f"no shipped allow entry was named as missing: {report!r}"
        )
        assert "Bash(ls:*)" not in report, (
            f"the user's own entry was reported as missing: {report!r}"
        )

    def test_check_settings_reports_nothing_missing_after_a_patch(self, tmp_path: Path) -> None:
        target = tmp_path / "settings.json"
        seeded = _run_installer("settings", "--target", str(target),
                                "--skill-dir", str(REPO))
        assert seeded.returncode == 0, seeded.stderr
        proc = _run_installer("check-settings", "--target", str(target))
        assert proc.returncode == 0, (
            f"a fully patched settings file reported missing entries: "
            f"{proc.stdout!r} {proc.stderr!r}"
        )

    def test_check_settings_does_not_crash_on_a_malformed_file(self, tmp_path: Path) -> None:
        target = tmp_path / "settings.json"
        target.write_text("{ this is not json")
        proc = _run_installer("check-settings", "--target", str(target))
        assert proc.returncode != 0, "malformed settings reported as complete"
        assert "Traceback" not in proc.stderr, proc.stderr
        assert "json" in (proc.stdout + proc.stderr).lower(), (
            f"the failure does not say the file was unparseable: {proc.stderr!r}"
        )

    def test_check_settings_does_not_crash_on_an_absent_file(self, tmp_path: Path) -> None:
        proc = _run_installer("check-settings", "--target", str(tmp_path / "nope.json"))
        assert proc.returncode != 0
        assert "Traceback" not in proc.stderr, proc.stderr
        report = (proc.stdout + proc.stderr).lower()
        assert "not found" in report or "no such file" in report or "missing" in report, (
            f"the failure does not say the file is absent: {proc.stderr!r}"
        )

    def test_the_doctor_check_is_registered(self) -> None:
        from writ.session import doctor

        names = [name for name, _ in doctor._CHECKS]
        assert "permissions-allowlist" in names, f"not registered: {names}"

    def test_the_doctor_check_fails_when_entries_are_missing(self, monkeypatch) -> None:
        from writ.session import doctor

        _require(doctor, "_missing_allow_entries", "check_permissions_allowlist")
        monkeypatch.setattr(
            doctor, "_missing_allow_entries",
            lambda: ["Bash(*writ/bin/writ query *)", "Bash(*writ/bin/writ status*)"],
        )
        result = doctor.check_permissions_allowlist(doctor.DoctorOptions())
        assert result.status != doctor.STATUS_OK
        assert "2" in result.detail, result.detail
        assert result.fixable, "the patcher is the fix and must be offered"

    def test_the_doctor_check_passes_when_present(self, monkeypatch) -> None:
        from writ.session import doctor

        _require(doctor, "_missing_allow_entries", "check_permissions_allowlist")
        monkeypatch.setattr(doctor, "_missing_allow_entries", lambda: [])
        result = doctor.check_permissions_allowlist(doctor.DoctorOptions())
        assert result.status == doctor.STATUS_OK, result.detail

    def test_the_doctor_check_survives_an_unreadable_settings_file(self, monkeypatch) -> None:
        from writ.session import doctor

        _require(doctor, "_missing_allow_entries", "check_permissions_allowlist")

        def _boom() -> list[str]:
            raise RuntimeError("settings.json unreadable")

        monkeypatch.setattr(doctor, "_missing_allow_entries", _boom)
        result = doctor.check_permissions_allowlist(doctor.DoctorOptions())
        assert result.status != doctor.STATUS_OK
        assert "unreadable" in result.detail

    def test_check_settings_reads_the_installers_own_entry_list(self, tmp_path: Path) -> None:
        """Guards against drift. If the check compared against a second, private
        copy of the entry list, that copy could diverge from BASE_ALLOW and the
        check would silently always-fail. An empty settings file must therefore
        report EVERY entry the installer ships, counted from the installer."""
        allow, deny = _installer_entries()
        target = tmp_path / "settings.json"
        target.write_text(json.dumps({"permissions": {"allow": [], "deny": []}}))
        proc = _run_installer("check-settings", "--target", str(target))
        report = proc.stdout + proc.stderr
        missing = [e for e in allow + deny if e in report]
        assert len(missing) == len(allow) + len(deny), (
            f"reported {len(missing)} of {len(allow) + len(deny)} shipped entries; "
            "the check is not reading the installer's own list"
        )


def _require(module, *names) -> None:
    """Fail with a legible skeleton message instead of an AttributeError.

    `monkeypatch.setattr` raises AttributeError when the target does not exist
    yet, which is error-shaped rather than assertion-shaped RED
    (TEC-PROC-RED-VERIFY-001). This states what is missing instead.
    """
    missing = [name for name in names if not hasattr(module, name)]
    if missing:
        pytest.fail(f"skeleton: {module.__name__} has no {', '.join(missing)} yet")
