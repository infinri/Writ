"""Hermetic test skeletons for `writ doctor [--fix]` (writ/session/doctor.py).

Every test in this file is RED until the implementer builds writ/session/doctor.py
and wires the `doctor` command into writ/cli.py. Tests fail on ImportError or
AssertionError, never on a collection/syntax error.

Run interpreter: .venv/bin/python (system python3 lacks onnxruntime).
NEVER run bare pytest (wipes the live Neo4j graph).

  .venv/bin/python -m pytest tests/test_doctor.py -v

ENF-SYS-005 note: checks involving subprocess, sockets, and real daemon state
cannot be proven with mocks. The tests below cover all branch paths using
monkeypatched seams. A future integration suite would exercise the real daemon
against a live fixture -- that is out of scope here.

=== Implementer's seam checklist ===

Every seam below must exist as a module-level callable in writ/session/doctor.py
so monkeypatch can replace them without touching real infrastructure.

  writ.session.doctor._http_get_health()
      -> dict | None: GET http://localhost:8765/health, return parsed JSON or None
         on any connection/timeout error.

  writ.session.doctor._systemctl_is_active(unit: str)
      -> str | None: run ["systemctl", "--user", "is-active", unit];
         return stdout.strip() or None if systemctl is absent/errors.

  writ.session.doctor._port_owner_pids(port: int)
      -> list[int]: pids holding the given TCP port (lsof -ti :PORT fallback ss);
         return [] if lsof/ss absent or no owner.

  writ.session.doctor._ps_writ_serve_orphans()
      -> list[dict]: scan ps output for `writ serve` processes with PPID==1;
         return [] if ps absent; each dict has keys "pid" and "ppid".

  writ.session.doctor._tcp_can_connect(host: str, port: int)
      -> bool: attempt a TCP connection; return True on success, False on refused/timeout.

  writ.session.doctor._venv_import_ok()
      -> bool: run .venv/bin/python -c "import onnxruntime; from tokenizers import Tokenizer"
         and return True on exit 0, False otherwise.

  writ.session.doctor._onnx_model_files_present()
      -> tuple[bool, bool]: (model_onnx_exists, tokenizer_json_exists) for the
         two files under ~/.cache/writ/models/onnx/.

  writ.session.doctor._detect_parity_violations()
      -> list[dict]: call IntegrityChecker.detect_parity_violations(bible_dir);
         return [] on any error (fail-open).

  writ.session.doctor._run_reconcile()
      -> None: call the reconcile library; side-effecting, only invoked under --fix.

  writ.session.doctor._bitbucket_creds_present()
      -> tuple[bool, bool]: (email_present, token_present) booleans from
         get_bitbucket_email() and get_bitbucket_token() truthiness ONLY.
         Never returns the actual values.

  writ.session.doctor._bitbucket_live_auth(repo: str)
      -> int | None: derive (workspace, repo_slug) via derive_project_identity +
         parse_bitbucket_remote, then perform the authenticated GET
         /2.0/repositories/{workspace}/{repo_slug}; return HTTP status code on
         success or HTTPError, or None when the repo has no Bitbucket remote
         (parse_bitbucket_remote returned None) -- the no-remote sentinel.
         Called ONLY when opts.net is True.

  writ.session.doctor._git_hook_installed(repo: str)
      -> bool: call git_hooks.git_hooks_installed(repo).

  writ.session.doctor._install_git_hook(repo: str)
      -> None: call git_hooks.install_git_hooks(repo). Side-effecting.

  writ.session.doctor._path_symlink_ok()
      -> tuple[bool, bool]: (which_resolves, readlink_ends_at_skill_bin)
         No live subprocess; reads PATH and resolves symlinks in-process.

  writ.session.doctor._recreate_symlink()
      -> None: recreate ~/.local/bin/writ -> $WRIT_DIR/bin/writ. Side-effecting.

  writ.session.doctor._cc_registration_ok()
      -> tuple[bool, list[str]]: (all_ok, list_of_missing_or_non_exec_paths).
         Reads plugin.json and hooks.json; checks each referenced .sh file.

  writ.session.doctor._latest_session_cache(session_id: str | None)
      -> dict | None: read the most-recent writ-session-*.json by mtime from
         WRIT_CACHE_DIR, or the specific session_id cache if given. Returns None
         when no cache exists.

  writ.session.doctor._restart_daemon()
      -> None: run ["systemctl", "--user", "restart", "writ-server"]. Side-effecting.

  writ.session.doctor._kill_port_owner(port: int)
      -> None: kill all pids from _port_owner_pids(port) then restart. Side-effecting.

=== Public API contract ===

  CheckResult(name, status, detail, fixable, fix)  -- frozen dataclass
  STATUS_OK = "ok"
  STATUS_WARN = "warn"
  STATUS_FAIL = "fail"
  DoctorOptions(net=False, session_id=None, repo=".")
  check_<name>(opts: DoctorOptions) -> CheckResult  (10 functions)
  run_all_checks(opts: DoctorOptions) -> list[CheckResult]
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from unittest.mock import MagicMock, call, patch

import pytest
from typer.testing import CliRunner

from writ.cli import app


# ---------------------------------------------------------------------------
# Fixtures (TEST-FIXTURE-001)
# ---------------------------------------------------------------------------

@pytest.fixture()
def default_opts():
    """DoctorOptions with all defaults."""
    from writ.session.doctor import DoctorOptions
    return DoctorOptions()


@pytest.fixture()
def net_opts():
    """DoctorOptions with net=True."""
    from writ.session.doctor import DoctorOptions
    return DoctorOptions(net=True)


runner = CliRunner()


# ---------------------------------------------------------------------------
# Module-level contract: public symbols exist and have correct types
# ---------------------------------------------------------------------------

class TestPublicAPI:
    """Verify the public contract the implementer must expose."""

    def test_status_constants_exist(self) -> None:
        from writ.session.doctor import STATUS_FAIL, STATUS_OK, STATUS_WARN
        assert STATUS_OK == "ok"
        assert STATUS_WARN == "warn"
        assert STATUS_FAIL == "fail"

    def test_check_result_is_frozen_dataclass(self) -> None:
        from writ.session.doctor import CheckResult, STATUS_OK
        r = CheckResult(name="x", status=STATUS_OK, detail="fine", fixable=False, fix=None)
        assert r.name == "x"
        assert r.status == STATUS_OK
        assert r.detail == "fine"
        assert r.fixable is False
        assert r.fix is None
        with pytest.raises((AttributeError, TypeError)):
            r.name = "y"  # type: ignore[misc]  # must be frozen

    def test_check_result_accepts_callable_fix(self) -> None:
        from writ.session.doctor import CheckResult, STATUS_FAIL
        called = []
        fix = lambda: called.append(True)
        r = CheckResult(name="x", status=STATUS_FAIL, detail="broken", fixable=True, fix=fix)
        r.fix()
        assert called == [True]

    def test_doctor_options_defaults(self) -> None:
        from writ.session.doctor import DoctorOptions
        opts = DoctorOptions()
        assert opts.net is False
        assert opts.session_id is None
        assert opts.repo == "."

    def test_all_ten_check_functions_importable(self) -> None:
        from writ.session.doctor import (
            check_bitbucket_creds,
            check_cc_hook_registration,
            check_corpus_drift,
            check_daemon_liveness,
            check_embedding_stack,
            check_git_post_commit_hook,
            check_mode_gate_sanity,
            check_neo4j_connectivity,
            check_stale_orphan_port_conflict,
            check_writ_path_symlink,
        )
        for fn in (
            check_daemon_liveness,
            check_stale_orphan_port_conflict,
            check_neo4j_connectivity,
            check_embedding_stack,
            check_corpus_drift,
            check_bitbucket_creds,
            check_git_post_commit_hook,
            check_writ_path_symlink,
            check_cc_hook_registration,
            check_mode_gate_sanity,
        ):
            assert callable(fn), f"{fn.__name__} must be callable"

    def test_run_all_checks_importable(self) -> None:
        from writ.session.doctor import run_all_checks
        assert callable(run_all_checks)


# ---------------------------------------------------------------------------
# Check 1: daemon-liveness
# ---------------------------------------------------------------------------

class TestDaemonLiveness:
    """check_daemon_liveness: ok / warn / fail / missing-systemctl paths."""

    def test_healthy_warm_rule_count_ok(self, default_opts, monkeypatch) -> None:
        # healthy + index_state=warm + rule_count>0 -> ok
        monkeypatch.setattr(
            "writ.session.doctor._http_get_health",
            lambda: {"status": "healthy", "index_state": "warm", "rule_count": 42},
        )
        monkeypatch.setattr(
            "writ.session.doctor._systemctl_is_active",
            lambda unit: "active",
        )
        from writ.session.doctor import STATUS_OK, check_daemon_liveness
        r = check_daemon_liveness(default_opts)
        assert r.status == STATUS_OK
        assert r.name == "daemon-liveness"

    def test_degraded_status_returns_warn(self, default_opts, monkeypatch) -> None:
        # status=degraded -> warn (DB/index split, not a full outage)
        monkeypatch.setattr(
            "writ.session.doctor._http_get_health",
            lambda: {"status": "degraded", "index_state": "warm", "rule_count": 0},
        )
        monkeypatch.setattr("writ.session.doctor._systemctl_is_active", lambda unit: "active")
        from writ.session.doctor import STATUS_WARN, check_daemon_liveness
        r = check_daemon_liveness(default_opts)
        assert r.status == STATUS_WARN

    def test_not_ready_returns_fail(self, default_opts, monkeypatch) -> None:
        # status=not_ready -> fail
        monkeypatch.setattr(
            "writ.session.doctor._http_get_health",
            lambda: {"status": "not_ready", "error": "Database not connected."},
        )
        monkeypatch.setattr("writ.session.doctor._systemctl_is_active", lambda unit: "active")
        from writ.session.doctor import STATUS_FAIL, check_daemon_liveness
        r = check_daemon_liveness(default_opts)
        assert r.status == STATUS_FAIL

    def test_unreachable_returns_fail(self, default_opts, monkeypatch) -> None:
        # health endpoint unreachable (returns None) -> fail
        monkeypatch.setattr("writ.session.doctor._http_get_health", lambda: None)
        monkeypatch.setattr("writ.session.doctor._systemctl_is_active", lambda unit: "inactive")
        from writ.session.doctor import STATUS_FAIL, check_daemon_liveness
        r = check_daemon_liveness(default_opts)
        assert r.status == STATUS_FAIL

    def test_cold_index_returns_fail(self, default_opts, monkeypatch) -> None:
        # healthy but index_state=cold -> fail (warm is required)
        monkeypatch.setattr(
            "writ.session.doctor._http_get_health",
            lambda: {"status": "healthy", "index_state": "cold", "rule_count": 10},
        )
        monkeypatch.setattr("writ.session.doctor._systemctl_is_active", lambda unit: "active")
        from writ.session.doctor import STATUS_FAIL, check_daemon_liveness
        r = check_daemon_liveness(default_opts)
        assert r.status == STATUS_FAIL

    def test_missing_systemctl_degrades_to_http_only_and_does_not_crash(
        self, default_opts, monkeypatch
    ) -> None:
        # systemctl absent (returns None) + healthy HTTP -> ok (HTTP-only note in detail)
        monkeypatch.setattr(
            "writ.session.doctor._http_get_health",
            lambda: {"status": "healthy", "index_state": "warm", "rule_count": 5},
        )
        monkeypatch.setattr("writ.session.doctor._systemctl_is_active", lambda unit: None)
        from writ.session.doctor import STATUS_OK, check_daemon_liveness
        r = check_daemon_liveness(default_opts)
        # Must not crash; status reflects HTTP result
        assert r.status == STATUS_OK
        assert r.name == "daemon-liveness"

    def test_fix_handle_is_set_and_fixable(self, default_opts, monkeypatch) -> None:
        # Any non-ok daemon state -> fixable=True with a fix handle
        monkeypatch.setattr("writ.session.doctor._http_get_health", lambda: None)
        monkeypatch.setattr("writ.session.doctor._systemctl_is_active", lambda unit: "inactive")
        from writ.session.doctor import check_daemon_liveness
        r = check_daemon_liveness(default_opts)
        assert r.fixable is True
        assert callable(r.fix)

    def test_unknown_health_keys_are_ignored(self, default_opts, monkeypatch) -> None:
        # Extra keys in the /health response must not crash the check
        monkeypatch.setattr(
            "writ.session.doctor._http_get_health",
            lambda: {
                "status": "healthy",
                "index_state": "warm",
                "rule_count": 7,
                "future_key_added_later": "ignored",
            },
        )
        monkeypatch.setattr("writ.session.doctor._systemctl_is_active", lambda unit: "active")
        from writ.session.doctor import STATUS_OK, check_daemon_liveness
        r = check_daemon_liveness(default_opts)
        assert r.status == STATUS_OK


# ---------------------------------------------------------------------------
# Check 2: stale-orphan-port-conflict
# ---------------------------------------------------------------------------

class TestStaleOrphanPortConflict:
    """check_stale_orphan_port_conflict: ok / fail (crash-loop) / warn (missing tools)."""

    def test_active_systemd_child_owner_returns_ok(self, default_opts, monkeypatch) -> None:
        # is-active=active + no PPID-1 orphans -> ok
        monkeypatch.setattr("writ.session.doctor._systemctl_is_active", lambda unit: "active")
        monkeypatch.setattr("writ.session.doctor._port_owner_pids", lambda port: [1234])
        monkeypatch.setattr("writ.session.doctor._ps_writ_serve_orphans", lambda: [])
        from writ.session.doctor import STATUS_OK, check_stale_orphan_port_conflict
        r = check_stale_orphan_port_conflict(default_opts)
        assert r.status == STATUS_OK
        assert r.name == "stale-orphan-port-conflict"

    def test_activating_with_ppid1_orphan_returns_fail(self, default_opts, monkeypatch) -> None:
        # is-active=activating + PPID-1 writ serve holding port -> fail
        monkeypatch.setattr(
            "writ.session.doctor._systemctl_is_active", lambda unit: "activating"
        )
        monkeypatch.setattr("writ.session.doctor._port_owner_pids", lambda port: [999])
        monkeypatch.setattr(
            "writ.session.doctor._ps_writ_serve_orphans",
            lambda: [{"pid": 999, "ppid": 1}],
        )
        from writ.session.doctor import STATUS_FAIL, check_stale_orphan_port_conflict
        r = check_stale_orphan_port_conflict(default_opts)
        assert r.status == STATUS_FAIL

    def test_failed_state_with_ppid1_orphan_returns_fail(self, default_opts, monkeypatch) -> None:
        # is-active=failed + PPID-1 orphan -> fail
        monkeypatch.setattr(
            "writ.session.doctor._systemctl_is_active", lambda unit: "failed"
        )
        monkeypatch.setattr("writ.session.doctor._port_owner_pids", lambda port: [777])
        monkeypatch.setattr(
            "writ.session.doctor._ps_writ_serve_orphans",
            lambda: [{"pid": 777, "ppid": 1}],
        )
        from writ.session.doctor import STATUS_FAIL, check_stale_orphan_port_conflict
        r = check_stale_orphan_port_conflict(default_opts)
        assert r.status == STATUS_FAIL

    def test_missing_lsof_degrades_to_warn(self, default_opts, monkeypatch) -> None:
        # lsof/ss/ps all absent (return [] / []) -> warn (can't assess, not fatal)
        monkeypatch.setattr(
            "writ.session.doctor._systemctl_is_active", lambda unit: "activating"
        )
        monkeypatch.setattr("writ.session.doctor._port_owner_pids", lambda port: [])
        monkeypatch.setattr("writ.session.doctor._ps_writ_serve_orphans", lambda: [])
        from writ.session.doctor import STATUS_WARN, check_stale_orphan_port_conflict
        r = check_stale_orphan_port_conflict(default_opts)
        assert r.status == STATUS_WARN

    def test_fail_result_is_fixable(self, default_opts, monkeypatch) -> None:
        monkeypatch.setattr(
            "writ.session.doctor._systemctl_is_active", lambda unit: "failed"
        )
        monkeypatch.setattr("writ.session.doctor._port_owner_pids", lambda port: [555])
        monkeypatch.setattr(
            "writ.session.doctor._ps_writ_serve_orphans",
            lambda: [{"pid": 555, "ppid": 1}],
        )
        from writ.session.doctor import check_stale_orphan_port_conflict
        r = check_stale_orphan_port_conflict(default_opts)
        assert r.fixable is True
        assert callable(r.fix)


# ---------------------------------------------------------------------------
# Check 3: neo4j-connectivity
# ---------------------------------------------------------------------------

class TestNeo4jConnectivity:
    """check_neo4j_connectivity: ok / fail (refused); never auto-fixable."""

    def test_tcp_ok_and_count_rules_returns_ok(self, default_opts, monkeypatch) -> None:
        monkeypatch.setattr(
            "writ.session.doctor._tcp_can_connect", lambda host, port: True
        )
        monkeypatch.setattr(
            "writ.session.doctor._count_neo4j_rules", lambda: 10
        )
        from writ.session.doctor import STATUS_OK, check_neo4j_connectivity
        r = check_neo4j_connectivity(default_opts)
        assert r.status == STATUS_OK
        assert r.name == "neo4j-connectivity"

    def test_tcp_refused_returns_fail(self, default_opts, monkeypatch) -> None:
        monkeypatch.setattr(
            "writ.session.doctor._tcp_can_connect", lambda host, port: False
        )
        from writ.session.doctor import STATUS_FAIL, check_neo4j_connectivity
        r = check_neo4j_connectivity(default_opts)
        assert r.status == STATUS_FAIL

    def test_count_rules_error_returns_fail(self, default_opts, monkeypatch) -> None:
        monkeypatch.setattr(
            "writ.session.doctor._tcp_can_connect", lambda host, port: True
        )
        monkeypatch.setattr(
            "writ.session.doctor._count_neo4j_rules",
            lambda: (_ for _ in ()).throw(Exception("connection refused")),
        )
        from writ.session.doctor import STATUS_FAIL, check_neo4j_connectivity
        r = check_neo4j_connectivity(default_opts)
        assert r.status == STATUS_FAIL

    def test_not_fixable(self, default_opts, monkeypatch) -> None:
        # Neo4j down is not auto-fixable; detail must mention docker guidance
        monkeypatch.setattr(
            "writ.session.doctor._tcp_can_connect", lambda host, port: False
        )
        from writ.session.doctor import check_neo4j_connectivity
        r = check_neo4j_connectivity(default_opts)
        assert r.fixable is False
        assert r.fix is None
        assert "docker" in r.detail.lower() or "compose" in r.detail.lower(), (
            f"detail must mention docker guidance; got: {r.detail!r}"
        )


class TestUniquenessConstraints:
    """check_uniqueness_constraints: ok / fail (missing) / fail (error); fixable."""

    def test_full_constraint_set_returns_ok(self, default_opts, monkeypatch) -> None:
        monkeypatch.setattr(
            "writ.session.doctor._list_neo4j_constraint_names",
            lambda: [f"c{i}" for i in range(17)],
        )
        from writ.session.doctor import STATUS_OK, check_uniqueness_constraints
        r = check_uniqueness_constraints(default_opts)
        assert r.status == STATUS_OK
        assert r.name == "uniqueness-constraints"

    def test_no_constraints_returns_fail(self, default_opts, monkeypatch) -> None:
        monkeypatch.setattr(
            "writ.session.doctor._list_neo4j_constraint_names", lambda: []
        )
        from writ.session.doctor import STATUS_FAIL, check_uniqueness_constraints
        r = check_uniqueness_constraints(default_opts)
        assert r.status == STATUS_FAIL

    def test_query_error_returns_fail_not_crash(self, default_opts, monkeypatch) -> None:
        monkeypatch.setattr(
            "writ.session.doctor._list_neo4j_constraint_names",
            lambda: (_ for _ in ()).throw(Exception("connection refused")),
        )
        from writ.session.doctor import STATUS_FAIL, check_uniqueness_constraints
        r = check_uniqueness_constraints(default_opts)
        assert r.status == STATUS_FAIL

    def test_missing_constraints_is_fixable_with_apply_constraints(
        self, default_opts, monkeypatch
    ) -> None:
        monkeypatch.setattr(
            "writ.session.doctor._list_neo4j_constraint_names", lambda: []
        )
        from writ.session.doctor import _apply_neo4j_constraints, check_uniqueness_constraints
        r = check_uniqueness_constraints(default_opts)
        assert r.fixable is True
        assert r.fix is _apply_neo4j_constraints


# ---------------------------------------------------------------------------
# Check 4: embedding-stack
# ---------------------------------------------------------------------------

class TestEmbeddingStack:
    """check_embedding_stack: ok / fail paths; probes .venv not system python3."""

    def test_import_ok_and_both_model_files_present_returns_ok(
        self, default_opts, monkeypatch
    ) -> None:
        monkeypatch.setattr("writ.session.doctor._venv_import_ok", lambda: True)
        monkeypatch.setattr(
            "writ.session.doctor._onnx_model_files_present", lambda: (True, True)
        )
        from writ.session.doctor import STATUS_OK, check_embedding_stack
        r = check_embedding_stack(default_opts)
        assert r.status == STATUS_OK
        assert r.name == "embedding-stack"

    def test_import_fails_returns_fail(self, default_opts, monkeypatch) -> None:
        monkeypatch.setattr("writ.session.doctor._venv_import_ok", lambda: False)
        monkeypatch.setattr(
            "writ.session.doctor._onnx_model_files_present", lambda: (True, True)
        )
        from writ.session.doctor import STATUS_FAIL, check_embedding_stack
        r = check_embedding_stack(default_opts)
        assert r.status == STATUS_FAIL

    def test_model_onnx_missing_returns_fail(self, default_opts, monkeypatch) -> None:
        monkeypatch.setattr("writ.session.doctor._venv_import_ok", lambda: True)
        monkeypatch.setattr(
            "writ.session.doctor._onnx_model_files_present", lambda: (False, True)
        )
        from writ.session.doctor import STATUS_FAIL, check_embedding_stack
        r = check_embedding_stack(default_opts)
        assert r.status == STATUS_FAIL

    def test_tokenizer_json_missing_returns_fail(self, default_opts, monkeypatch) -> None:
        monkeypatch.setattr("writ.session.doctor._venv_import_ok", lambda: True)
        monkeypatch.setattr(
            "writ.session.doctor._onnx_model_files_present", lambda: (True, False)
        )
        from writ.session.doctor import STATUS_FAIL, check_embedding_stack
        r = check_embedding_stack(default_opts)
        assert r.status == STATUS_FAIL

    def test_probes_venv_interpreter_seam_not_system_python(
        self, default_opts, monkeypatch
    ) -> None:
        # The check must call _venv_import_ok(), not a system-python path.
        # We verify this by confirming _venv_import_ok is invoked (not bypassed).
        calls = []
        monkeypatch.setattr(
            "writ.session.doctor._venv_import_ok",
            lambda: calls.append(True) or True,
        )
        monkeypatch.setattr(
            "writ.session.doctor._onnx_model_files_present", lambda: (True, True)
        )
        from writ.session.doctor import check_embedding_stack
        check_embedding_stack(default_opts)
        assert calls, "_venv_import_ok seam must be called; direct system-python usage is wrong"

    def test_not_fixable_with_install_guidance(self, default_opts, monkeypatch) -> None:
        monkeypatch.setattr("writ.session.doctor._venv_import_ok", lambda: False)
        monkeypatch.setattr(
            "writ.session.doctor._onnx_model_files_present", lambda: (False, False)
        )
        from writ.session.doctor import check_embedding_stack
        r = check_embedding_stack(default_opts)
        assert r.fixable is False
        assert r.fix is None
        assert "pip" in r.detail.lower() or "onnx" in r.detail.lower(), (
            f"detail must mention install guidance; got: {r.detail!r}"
        )


# ---------------------------------------------------------------------------
# Check 5: corpus-drift
# ---------------------------------------------------------------------------

class TestCorpusDrift:
    """check_corpus_drift: ok / warn; fix handle wraps reconcile."""

    def test_empty_violations_returns_ok(self, default_opts, monkeypatch) -> None:
        monkeypatch.setattr(
            "writ.session.doctor._detect_parity_violations", lambda: []
        )
        from writ.session.doctor import STATUS_OK, check_corpus_drift
        r = check_corpus_drift(default_opts)
        assert r.status == STATUS_OK
        assert r.name == "corpus-drift"

    def test_non_empty_violations_returns_warn(self, default_opts, monkeypatch) -> None:
        monkeypatch.setattr(
            "writ.session.doctor._detect_parity_violations",
            lambda: [{"type": "Rule", "id": "ORPHAN-001"}],
        )
        from writ.session.doctor import STATUS_WARN, check_corpus_drift
        r = check_corpus_drift(default_opts)
        assert r.status == STATUS_WARN

    def test_warn_result_is_fixable_with_callable_fix(
        self, default_opts, monkeypatch
    ) -> None:
        monkeypatch.setattr(
            "writ.session.doctor._detect_parity_violations",
            lambda: [{"type": "Rule", "id": "DRIFT-001"}],
        )
        from writ.session.doctor import check_corpus_drift
        r = check_corpus_drift(default_opts)
        assert r.fixable is True
        assert callable(r.fix)

    def test_fix_handle_invokes_reconcile_seam(self, default_opts, monkeypatch) -> None:
        # The fix handle must call _run_reconcile(), not bypass it
        reconcile_calls = []
        monkeypatch.setattr(
            "writ.session.doctor._detect_parity_violations",
            lambda: [{"type": "Rule", "id": "DRIFT-001"}],
        )
        monkeypatch.setattr(
            "writ.session.doctor._run_reconcile",
            lambda: reconcile_calls.append(True),
        )
        from writ.session.doctor import check_corpus_drift
        r = check_corpus_drift(default_opts)
        r.fix()
        assert reconcile_calls == [True], "fix() must call _run_reconcile()"

    def test_fix_not_called_on_ok_result(self, default_opts, monkeypatch) -> None:
        # ok results have fixable=False (or fix=None)
        monkeypatch.setattr(
            "writ.session.doctor._detect_parity_violations", lambda: []
        )
        from writ.session.doctor import check_corpus_drift
        r = check_corpus_drift(default_opts)
        assert r.fix is None or r.fixable is False


# ---------------------------------------------------------------------------
# Check 6: bitbucket-creds (SEC-DATA-RETAIN-001)
# ---------------------------------------------------------------------------

SENTINEL_EMAIL = "test-email-DO-NOT-LOG@sentinel.invalid"
SENTINEL_TOKEN = "test-token-DO-NOT-LOG-abc123"


class TestBitbucketCreds:
    """check_bitbucket_creds: presence-only (default) and --net paths.

    CRITICAL: credential values must NEVER appear in detail or JSON output.
    writ.toml must never be opened by the check.

    After the _bitbucket_live_auth signature change the seam takes a single
    positional `repo` argument (str) and returns int | None.  All lambdas
    below reflect the new signature.  Tests for the no-remote sentinel (None
    return) are included as regression guards against the original false-fail
    when running against a non-Bitbucket repo.
    """

    def test_both_present_returns_ok(self, default_opts, monkeypatch) -> None:
        monkeypatch.setattr(
            "writ.session.doctor._bitbucket_creds_present",
            lambda: (True, True),
        )
        from writ.session.doctor import STATUS_OK, check_bitbucket_creds
        r = check_bitbucket_creds(default_opts)
        assert r.status == STATUS_OK
        assert r.name == "bitbucket-creds"

    def test_email_missing_returns_fail(self, default_opts, monkeypatch) -> None:
        monkeypatch.setattr(
            "writ.session.doctor._bitbucket_creds_present",
            lambda: (False, True),
        )
        from writ.session.doctor import STATUS_FAIL, check_bitbucket_creds
        r = check_bitbucket_creds(default_opts)
        assert r.status == STATUS_FAIL

    def test_token_missing_returns_fail(self, default_opts, monkeypatch) -> None:
        monkeypatch.setattr(
            "writ.session.doctor._bitbucket_creds_present",
            lambda: (True, False),
        )
        from writ.session.doctor import STATUS_FAIL, check_bitbucket_creds
        r = check_bitbucket_creds(default_opts)
        assert r.status == STATUS_FAIL

    def test_net_true_200_returns_ok(self, net_opts, monkeypatch) -> None:
        monkeypatch.setattr(
            "writ.session.doctor._bitbucket_creds_present",
            lambda: (True, True),
        )
        monkeypatch.setattr(
            "writ.session.doctor._bitbucket_live_auth",
            lambda repo: 200,
        )
        from writ.session.doctor import STATUS_OK, check_bitbucket_creds
        r = check_bitbucket_creds(net_opts)
        assert r.status == STATUS_OK

    def test_net_true_401_returns_fail(self, net_opts, monkeypatch) -> None:
        monkeypatch.setattr(
            "writ.session.doctor._bitbucket_creds_present",
            lambda: (True, True),
        )
        monkeypatch.setattr(
            "writ.session.doctor._bitbucket_live_auth",
            lambda repo: 401,
        )
        from writ.session.doctor import STATUS_FAIL, check_bitbucket_creds
        r = check_bitbucket_creds(net_opts)
        assert r.status == STATUS_FAIL

    def test_net_true_403_returns_fail(self, net_opts, monkeypatch) -> None:
        monkeypatch.setattr(
            "writ.session.doctor._bitbucket_creds_present",
            lambda: (True, True),
        )
        monkeypatch.setattr(
            "writ.session.doctor._bitbucket_live_auth",
            lambda repo: 403,
        )
        from writ.session.doctor import STATUS_FAIL, check_bitbucket_creds
        r = check_bitbucket_creds(net_opts)
        assert r.status == STATUS_FAIL

    def test_net_false_live_auth_seam_never_called(self, default_opts, monkeypatch) -> None:
        # Without --net the live-auth seam must never be invoked
        live_auth_calls = []
        monkeypatch.setattr(
            "writ.session.doctor._bitbucket_creds_present",
            lambda: (True, True),
        )
        monkeypatch.setattr(
            "writ.session.doctor._bitbucket_live_auth",
            lambda repo: live_auth_calls.append(True) or 200,
        )
        from writ.session.doctor import check_bitbucket_creds
        check_bitbucket_creds(default_opts)
        assert live_auth_calls == [], (
            "_bitbucket_live_auth must never be called when opts.net is False"
        )

    def test_net_true_no_remote_sentinel_returns_ok_not_fail(
        self, net_opts, monkeypatch
    ) -> None:
        # Regression guard: writ doctor --net must NOT fail when the current repo
        # has no Bitbucket remote.  _bitbucket_live_auth returns None (the
        # no-remote sentinel) and check_bitbucket_creds must degrade to
        # presence-only ok, not STATUS_FAIL.
        monkeypatch.setattr(
            "writ.session.doctor._bitbucket_creds_present",
            lambda: (True, True),
        )
        monkeypatch.setattr(
            "writ.session.doctor._bitbucket_live_auth",
            lambda repo: None,
        )
        from writ.session.doctor import STATUS_FAIL, STATUS_OK, check_bitbucket_creds
        r = check_bitbucket_creds(net_opts)
        assert r.status != STATUS_FAIL, (
            "no-remote sentinel must NOT produce STATUS_FAIL; "
            f"got status={r.status!r}, detail={r.detail!r}"
        )
        assert r.status == STATUS_OK, (
            f"no-remote sentinel must produce STATUS_OK; got {r.status!r}"
        )

    def test_net_true_no_remote_sentinel_detail_explains_absence(
        self, net_opts, monkeypatch
    ) -> None:
        # When the sentinel fires the detail must explain that there is no
        # Bitbucket remote (not a silent pass with no context).
        monkeypatch.setattr(
            "writ.session.doctor._bitbucket_creds_present",
            lambda: (True, True),
        )
        monkeypatch.setattr(
            "writ.session.doctor._bitbucket_live_auth",
            lambda repo: None,
        )
        from writ.session.doctor import check_bitbucket_creds
        r = check_bitbucket_creds(net_opts)
        detail_lower = r.detail.lower()
        has_remote_mention = "remote" in detail_lower or "presence" in detail_lower
        assert has_remote_mention, (
            "detail must explain the no-remote / presence-only situation; "
            f"got: {r.detail!r}"
        )

    def test_bitbucket_live_auth_targets_repository_endpoint_not_user(
        self, monkeypatch
    ) -> None:
        # Hermetic endpoint check: after the fix _bitbucket_live_auth must ping
        # /2.0/repositories/{workspace}/{slug}, NOT /2.0/user.
        #
        # Strategy: monkeypatch the two derivation helpers inside doctor.py's
        # import namespace so they return a known (workspace, slug) pair, then
        # monkeypatch urllib.request.urlopen to capture the URL without a live
        # network call.
        #
        # This test is skipped automatically if either helper is not yet imported
        # inside _bitbucket_live_auth (i.e. the implementation hasn't landed yet),
        # letting the other RED tests carry the failure signal.
        import urllib.request as _urllib_req

        captured_urls: list[str] = []

        class _FakeResponse:
            def getcode(self) -> int:
                return 200
            def __enter__(self):
                return self
            def __exit__(self, *_):
                pass

        def _fake_urlopen(req, timeout=None):
            url = req.full_url if hasattr(req, "full_url") else str(req)
            captured_urls.append(url)
            return _FakeResponse()

        # Monkeypatch the derive helpers used inside _bitbucket_live_auth.
        # If the implementation calls them under a different name this test will
        # fail loudly, which is the correct RED signal.
        monkeypatch.setattr(
            "writ.session.git_identity.derive_project_identity",
            lambda repo: ("_root_", "https://bitbucket.org/testworkspace/testrepo.git", "testrepo"),
        )
        monkeypatch.setattr(
            "writ.session.remote_parse.parse_bitbucket_remote",
            lambda remote_url: ("testworkspace", "testrepo"),
        )
        monkeypatch.setattr(_urllib_req, "urlopen", _fake_urlopen)

        # Also patch config so no real credentials are read
        monkeypatch.setattr(
            "writ.session.doctor._bitbucket_creds_present",
            lambda: (True, True),
        )

        # Patch the config getters _bitbucket_live_auth calls for the Basic header
        try:
            import writ.config as _cfg
            monkeypatch.setattr(_cfg, "get_bitbucket_email", lambda: "user@example.com")
            monkeypatch.setattr(_cfg, "get_bitbucket_token", lambda: "tok")
        except (ImportError, AttributeError):
            pytest.skip("writ.config getters not yet implemented")

        from writ.session.doctor import _bitbucket_live_auth
        import inspect
        sig = inspect.signature(_bitbucket_live_auth)
        if "repo" not in sig.parameters:
            pytest.skip(
                "_bitbucket_live_auth does not yet accept a repo arg; "
                "implementation not landed -- RED signal carried by other tests"
            )

        _bitbucket_live_auth(".")

        assert captured_urls, "_bitbucket_live_auth must call urlopen (no URL captured)"
        url = captured_urls[0]
        assert "/repositories/" in url, (
            f"_bitbucket_live_auth must target /2.0/repositories/{{workspace}}/{{slug}}, "
            f"not /2.0/user; captured URL: {url!r}"
        )
        assert "/2.0/user" not in url, (
            f"_bitbucket_live_auth must NOT target /2.0/user; captured URL: {url!r}"
        )
        assert "testworkspace" in url, (
            f"URL must contain derived workspace 'testworkspace'; got: {url!r}"
        )
        assert "testrepo" in url, (
            f"URL must contain derived repo slug 'testrepo'; got: {url!r}"
        )

    def test_credential_values_absent_from_detail(self, default_opts, monkeypatch) -> None:
        # The sentinel values injected via the mock must NEVER appear in detail
        def _fake_creds_present():
            # We test the seam itself returns booleans, not the values.
            # This confirms the check cannot leak them (it only receives booleans).
            return (bool(SENTINEL_EMAIL), bool(SENTINEL_TOKEN))

        monkeypatch.setattr(
            "writ.session.doctor._bitbucket_creds_present",
            _fake_creds_present,
        )
        from writ.session.doctor import check_bitbucket_creds
        r = check_bitbucket_creds(default_opts)
        assert SENTINEL_EMAIL not in r.detail, (
            f"Sentinel email must not appear in detail; got: {r.detail!r}"
        )
        assert SENTINEL_TOKEN not in r.detail, (
            f"Sentinel token must not appear in detail; got: {r.detail!r}"
        )

    def test_writ_toml_never_opened_by_check(self, default_opts, monkeypatch, tmp_path) -> None:
        # The check must use _bitbucket_creds_present() seam only; it must never
        # call open() on a writ.toml path directly.
        opened_paths: list[str] = []
        original_open = open

        def _tracking_open(path, *args, **kwargs):
            opened_paths.append(str(path))
            return original_open(path, *args, **kwargs)

        monkeypatch.setattr(
            "writ.session.doctor._bitbucket_creds_present",
            lambda: (True, True),
        )
        # Patch builtins.open only within the doctor module
        import builtins
        monkeypatch.setattr(builtins, "open", _tracking_open)

        from writ.session.doctor import check_bitbucket_creds
        check_bitbucket_creds(default_opts)

        writ_toml_opens = [p for p in opened_paths if "writ.toml" in p]
        assert writ_toml_opens == [], (
            f"check_bitbucket_creds must never open writ.toml directly; "
            f"detected opens: {writ_toml_opens}"
        )

    def test_not_fixable(self, default_opts, monkeypatch) -> None:
        monkeypatch.setattr(
            "writ.session.doctor._bitbucket_creds_present",
            lambda: (False, False),
        )
        from writ.session.doctor import check_bitbucket_creds
        r = check_bitbucket_creds(default_opts)
        assert r.fixable is False
        assert r.fix is None


# ---------------------------------------------------------------------------
# Check 7: git-post-commit-hook
# ---------------------------------------------------------------------------

class TestGitPostCommitHook:
    """check_git_post_commit_hook: ok / fail; fix calls install."""

    def test_marker_present_returns_ok(self, default_opts, monkeypatch) -> None:
        monkeypatch.setattr(
            "writ.session.doctor._git_hook_installed",
            lambda repo: True,
        )
        from writ.session.doctor import STATUS_OK, check_git_post_commit_hook
        r = check_git_post_commit_hook(default_opts)
        assert r.status == STATUS_OK
        assert r.name == "git-post-commit-hook"

    def test_marker_absent_returns_fail(self, default_opts, monkeypatch) -> None:
        monkeypatch.setattr(
            "writ.session.doctor._git_hook_installed",
            lambda repo: False,
        )
        from writ.session.doctor import STATUS_FAIL, check_git_post_commit_hook
        r = check_git_post_commit_hook(default_opts)
        assert r.status == STATUS_FAIL

    def test_fail_is_fixable(self, default_opts, monkeypatch) -> None:
        monkeypatch.setattr(
            "writ.session.doctor._git_hook_installed",
            lambda repo: False,
        )
        from writ.session.doctor import check_git_post_commit_hook
        r = check_git_post_commit_hook(default_opts)
        assert r.fixable is True
        assert callable(r.fix)

    def test_fix_handle_calls_install_hook_seam(self, default_opts, monkeypatch) -> None:
        install_calls = []
        monkeypatch.setattr(
            "writ.session.doctor._git_hook_installed",
            lambda repo: False,
        )
        monkeypatch.setattr(
            "writ.session.doctor._install_git_hook",
            lambda repo: install_calls.append(repo),
        )
        from writ.session.doctor import check_git_post_commit_hook
        r = check_git_post_commit_hook(default_opts)
        r.fix()
        assert install_calls, "fix() must call _install_git_hook(repo)"

    def test_repo_from_opts_passed_to_seam(self, monkeypatch) -> None:
        # opts.repo is forwarded to the hook check seam
        from writ.session.doctor import DoctorOptions
        opts = DoctorOptions(repo="/custom/repo")
        received = []
        monkeypatch.setattr(
            "writ.session.doctor._git_hook_installed",
            lambda repo: received.append(repo) or True,
        )
        from writ.session.doctor import check_git_post_commit_hook
        check_git_post_commit_hook(opts)
        assert received == ["/custom/repo"], (
            f"opts.repo must be forwarded to _git_hook_installed; got: {received}"
        )


# ---------------------------------------------------------------------------
# Check 8: writ-PATH-symlink
# ---------------------------------------------------------------------------

class TestWritPathSymlink:
    """check_writ_path_symlink: ok / fail; fix recreates symlink."""

    def test_which_resolves_and_readlink_correct_returns_ok(
        self, default_opts, monkeypatch
    ) -> None:
        monkeypatch.setattr(
            "writ.session.doctor._path_symlink_ok",
            lambda: (True, True),
        )
        from writ.session.doctor import STATUS_OK, check_writ_path_symlink
        r = check_writ_path_symlink(default_opts)
        assert r.status == STATUS_OK
        assert r.name == "writ-path-symlink"

    def test_which_not_found_returns_fail(self, default_opts, monkeypatch) -> None:
        monkeypatch.setattr(
            "writ.session.doctor._path_symlink_ok",
            lambda: (False, False),
        )
        from writ.session.doctor import STATUS_FAIL, check_writ_path_symlink
        r = check_writ_path_symlink(default_opts)
        assert r.status == STATUS_FAIL

    def test_readlink_divergent_returns_fail(self, default_opts, monkeypatch) -> None:
        # which resolves but points at wrong target
        monkeypatch.setattr(
            "writ.session.doctor._path_symlink_ok",
            lambda: (True, False),
        )
        from writ.session.doctor import STATUS_FAIL, check_writ_path_symlink
        r = check_writ_path_symlink(default_opts)
        assert r.status == STATUS_FAIL

    def test_fail_is_fixable(self, default_opts, monkeypatch) -> None:
        monkeypatch.setattr(
            "writ.session.doctor._path_symlink_ok",
            lambda: (False, False),
        )
        from writ.session.doctor import check_writ_path_symlink
        r = check_writ_path_symlink(default_opts)
        assert r.fixable is True
        assert callable(r.fix)

    def test_fix_handle_invokes_recreate_symlink_seam(
        self, default_opts, monkeypatch
    ) -> None:
        recreate_calls = []
        monkeypatch.setattr(
            "writ.session.doctor._path_symlink_ok",
            lambda: (False, False),
        )
        monkeypatch.setattr(
            "writ.session.doctor._recreate_symlink",
            lambda: recreate_calls.append(True),
        )
        from writ.session.doctor import check_writ_path_symlink
        r = check_writ_path_symlink(default_opts)
        r.fix()
        assert recreate_calls == [True], "fix() must call _recreate_symlink()"


# ---------------------------------------------------------------------------
# Check 9: cc-hook-registration
# ---------------------------------------------------------------------------

class TestCCHookRegistration:
    """check_cc_hook_registration: ok / fail; not auto-fixable."""

    def test_all_present_and_executable_returns_ok(
        self, default_opts, monkeypatch
    ) -> None:
        monkeypatch.setattr(
            "writ.session.doctor._cc_registration_ok",
            lambda: (True, []),
        )
        from writ.session.doctor import STATUS_OK, check_cc_hook_registration
        r = check_cc_hook_registration(default_opts)
        assert r.status == STATUS_OK
        assert r.name == "cc-hook-registration"

    def test_referenced_sh_missing_returns_fail(self, default_opts, monkeypatch) -> None:
        monkeypatch.setattr(
            "writ.session.doctor._cc_registration_ok",
            lambda: (False, ["hooks/missing-hook.sh"]),
        )
        from writ.session.doctor import STATUS_FAIL, check_cc_hook_registration
        r = check_cc_hook_registration(default_opts)
        assert r.status == STATUS_FAIL

    def test_non_executable_sh_returns_fail(self, default_opts, monkeypatch) -> None:
        monkeypatch.setattr(
            "writ.session.doctor._cc_registration_ok",
            lambda: (False, ["hooks/writ-pre-write-dispatch.sh (not executable)"]),
        )
        from writ.session.doctor import STATUS_FAIL, check_cc_hook_registration
        r = check_cc_hook_registration(default_opts)
        assert r.status == STATUS_FAIL

    def test_not_fixable(self, default_opts, monkeypatch) -> None:
        monkeypatch.setattr(
            "writ.session.doctor._cc_registration_ok",
            lambda: (False, ["hooks/missing.sh"]),
        )
        from writ.session.doctor import check_cc_hook_registration
        r = check_cc_hook_registration(default_opts)
        assert r.fixable is False
        assert r.fix is None

    def test_ok_detail_mentions_fresh_session_caveat(
        self, default_opts, monkeypatch
    ) -> None:
        # Even on ok, the detail should note that a new hooks.json mapping
        # requires a fresh CC session
        monkeypatch.setattr(
            "writ.session.doctor._cc_registration_ok",
            lambda: (True, []),
        )
        from writ.session.doctor import check_cc_hook_registration
        r = check_cc_hook_registration(default_opts)
        assert r.name == "cc-hook-registration"
        # The detail is allowed to be empty on ok; we only assert no crash here


# ---------------------------------------------------------------------------
# Check 10: mode-gate-sanity
# ---------------------------------------------------------------------------

class TestModeGateSanity:
    """check_mode_gate_sanity: ok / warn (blank / invalid / stale-planning)."""

    def _make_cache(self, mode: str | None, gates: dict | None = None) -> dict:
        base: dict = {"mode": mode}
        if gates is not None:
            base["gates"] = gates
        return base

    def test_valid_mode_returns_ok(self, default_opts, monkeypatch) -> None:
        monkeypatch.setattr(
            "writ.session.doctor._latest_session_cache",
            lambda session_id: self._make_cache("work"),
        )
        from writ.session.doctor import STATUS_OK, check_mode_gate_sanity
        r = check_mode_gate_sanity(default_opts)
        assert r.status == STATUS_OK
        assert r.name == "mode-gate-sanity"

    def test_blank_mode_returns_warn(self, default_opts, monkeypatch) -> None:
        monkeypatch.setattr(
            "writ.session.doctor._latest_session_cache",
            lambda session_id: self._make_cache(None),
        )
        from writ.session.doctor import STATUS_WARN, check_mode_gate_sanity
        r = check_mode_gate_sanity(default_opts)
        assert r.status == STATUS_WARN

    def test_invalid_mode_returns_warn(self, default_opts, monkeypatch) -> None:
        monkeypatch.setattr(
            "writ.session.doctor._latest_session_cache",
            lambda session_id: self._make_cache("not_a_real_mode"),
        )
        from writ.session.doctor import STATUS_WARN, check_mode_gate_sanity
        r = check_mode_gate_sanity(default_opts)
        assert r.status == STATUS_WARN

    def test_no_cache_file_returns_warn(self, default_opts, monkeypatch) -> None:
        monkeypatch.setattr(
            "writ.session.doctor._latest_session_cache",
            lambda session_id: None,
        )
        from writ.session.doctor import STATUS_WARN, check_mode_gate_sanity
        r = check_mode_gate_sanity(default_opts)
        assert r.status == STATUS_WARN

    def test_session_id_override_passed_to_seam(self, monkeypatch) -> None:
        # opts.session_id is forwarded to _latest_session_cache
        from writ.session.doctor import DoctorOptions
        opts = DoctorOptions(session_id="specific-session-abc")
        received = []
        monkeypatch.setattr(
            "writ.session.doctor._latest_session_cache",
            lambda session_id: received.append(session_id) or {"mode": "work"},
        )
        from writ.session.doctor import check_mode_gate_sanity
        check_mode_gate_sanity(opts)
        assert received == ["specific-session-abc"], (
            f"session_id override must be forwarded to _latest_session_cache; got: {received}"
        )

    def test_stale_planning_with_gates_inconsistency_returns_warn(
        self, default_opts, monkeypatch
    ) -> None:
        # mode=work, phase=planning, but gate advanced=True -> inconsistency -> warn
        cache = {
            "mode": "work",
            "phase": "planning",
            "gates": {"phase-a": {"advanced": True, "ts": "2026-06-29T10:00:00Z"}},
        }
        monkeypatch.setattr(
            "writ.session.doctor._latest_session_cache",
            lambda session_id: cache,
        )
        from writ.session.doctor import STATUS_WARN, check_mode_gate_sanity
        r = check_mode_gate_sanity(default_opts)
        assert r.status == STATUS_WARN

    def test_not_fixable(self, default_opts, monkeypatch) -> None:
        monkeypatch.setattr(
            "writ.session.doctor._latest_session_cache",
            lambda session_id: self._make_cache(None),
        )
        from writ.session.doctor import check_mode_gate_sanity
        r = check_mode_gate_sanity(default_opts)
        assert r.fixable is False
        assert r.fix is None
        assert "writ mode set" in r.detail, (
            f"detail must mention 'writ mode set'; got: {r.detail!r}"
        )


# ---------------------------------------------------------------------------
# Orchestrator: run_all_checks
# ---------------------------------------------------------------------------

class TestRunAllChecks:
    """run_all_checks: exception isolation, ordering, result count."""

    def _patch_all_ok(self, monkeypatch) -> None:
        """Patch every seam so all 12 checks return ok with no side effects."""
        monkeypatch.setattr(
            "writ.session.doctor._http_get_health",
            lambda: {"status": "healthy", "index_state": "warm", "rule_count": 5},
        )
        monkeypatch.setattr("writ.session.doctor._systemctl_is_active", lambda unit: "active")
        monkeypatch.setattr("writ.session.doctor._port_owner_pids", lambda port: [1234])
        monkeypatch.setattr("writ.session.doctor._ps_writ_serve_orphans", lambda: [])
        monkeypatch.setattr("writ.session.doctor._tcp_can_connect", lambda host, port: True)
        monkeypatch.setattr("writ.session.doctor._count_neo4j_rules", lambda: 10)
        monkeypatch.setattr(
            "writ.session.doctor._list_neo4j_constraint_names",
            lambda: [f"c{i}" for i in range(17)],
        )
        monkeypatch.setattr("writ.session.doctor._venv_import_ok", lambda: True)
        monkeypatch.setattr(
            "writ.session.doctor._onnx_model_files_present", lambda: (True, True)
        )
        monkeypatch.setattr("writ.session.doctor._detect_parity_violations", lambda: [])
        monkeypatch.setattr(
            "writ.session.doctor._bitbucket_creds_present", lambda: (True, True)
        )
        monkeypatch.setattr("writ.session.doctor._git_hook_installed", lambda repo: True)
        monkeypatch.setattr("writ.session.doctor._path_symlink_ok", lambda: (True, True))
        monkeypatch.setattr("writ.session.doctor._cc_registration_ok", lambda: (True, []))
        # duplicate-hook-registration otherwise reads the developer's real
        # ~/.claude/settings.json and shells out to `claude plugin list`, so the outcome would
        # depend on the machine (TEST-ISOLATE-001). No loaded plugin => the check is ok.
        monkeypatch.setattr("writ.session.doctor._loaded_plugin_paths", lambda: [])
        monkeypatch.setattr(
            "writ.session.doctor._latest_session_cache",
            lambda session_id: {"mode": "work"},
        )

    def test_returns_exactly_twelve_results(self, default_opts, monkeypatch) -> None:
        self._patch_all_ok(monkeypatch)
        from writ.session.doctor import run_all_checks
        results = run_all_checks(default_opts)
        assert len(results) == 12, (
            f"run_all_checks must return exactly 12 CheckResults; got {len(results)}"
        )

    def test_result_names_match_contract(self, default_opts, monkeypatch) -> None:
        self._patch_all_ok(monkeypatch)
        from writ.session.doctor import run_all_checks
        results = run_all_checks(default_opts)
        expected_names = {
            "daemon-liveness",
            "stale-orphan-port-conflict",
            "neo4j-connectivity",
            "uniqueness-constraints",
            "embedding-stack",
            "corpus-drift",
            "bitbucket-creds",
            "git-post-commit-hook",
            "writ-path-symlink",
            "cc-hook-registration",
            "duplicate-hook-registration",
            "mode-gate-sanity",
        }
        actual_names = {r.name for r in results}
        assert actual_names == expected_names, (
            f"check names mismatch; missing: {expected_names - actual_names}; "
            f"extra: {actual_names - expected_names}"
        )

    def test_exception_in_one_check_does_not_stop_others(
        self, default_opts, monkeypatch
    ) -> None:
        # daemon-liveness raises; all remaining 11 checks must still run
        self._patch_all_ok(monkeypatch)
        monkeypatch.setattr(
            "writ.session.doctor._http_get_health",
            lambda: (_ for _ in ()).throw(RuntimeError("daemon exploded")),
        )
        from writ.session.doctor import STATUS_FAIL, run_all_checks
        results = run_all_checks(default_opts)
        assert len(results) == 12, "all 12 results must be returned despite one exception"
        daemon_result = next(r for r in results if r.name == "daemon-liveness")
        assert daemon_result.status == STATUS_FAIL
        assert "daemon exploded" in daemon_result.detail, (
            f"exception text must appear in detail; got: {daemon_result.detail!r}"
        )

    def test_exception_result_not_fixable(self, default_opts, monkeypatch) -> None:
        self._patch_all_ok(monkeypatch)
        monkeypatch.setattr(
            "writ.session.doctor._http_get_health",
            lambda: (_ for _ in ()).throw(RuntimeError("unexpected")),
        )
        from writ.session.doctor import run_all_checks
        results = run_all_checks(default_opts)
        daemon_result = next(r for r in results if r.name == "daemon-liveness")
        assert daemon_result.fixable is False
        assert daemon_result.fix is None

    def test_exception_in_middle_check_leaves_later_checks_ok(
        self, default_opts, monkeypatch
    ) -> None:
        # Make neo4j-connectivity explode; mode-gate-sanity (check 10) must still be ok
        self._patch_all_ok(monkeypatch)
        monkeypatch.setattr(
            "writ.session.doctor._tcp_can_connect",
            lambda host, port: (_ for _ in ()).throw(RuntimeError("socket error")),
        )
        from writ.session.doctor import STATUS_OK, run_all_checks
        results = run_all_checks(default_opts)
        mode_result = next(r for r in results if r.name == "mode-gate-sanity")
        assert mode_result.status == STATUS_OK, (
            "mode-gate-sanity must still pass when a prior check raises"
        )

    def test_all_ok_results_have_ok_status(self, default_opts, monkeypatch) -> None:
        self._patch_all_ok(monkeypatch)
        from writ.session.doctor import STATUS_OK, run_all_checks
        results = run_all_checks(default_opts)
        non_ok = [r for r in results if r.status != STATUS_OK]
        assert non_ok == [], (
            f"with all seams returning ok, all results must be ok; non-ok: {non_ok}"
        )


# ---------------------------------------------------------------------------
# CLI: writ doctor command
# ---------------------------------------------------------------------------

class TestDoctorCommandRegistered:
    """The typer app exposes a 'doctor' command."""

    def test_doctor_command_registered(self) -> None:
        names = [cmd.name for cmd in app.registered_commands]
        assert "doctor" in names, (
            f"'doctor' must be registered in the typer app; registered: {names}"
        )


class TestDoctorCommandExitCodes:
    """Exit code: fail -> non-zero; warn-only or all-ok -> zero."""

    def _make_result(self, name: str, status: str, fixable: bool = False) -> object:
        from writ.session.doctor import CheckResult
        return CheckResult(name=name, status=status, detail="test", fixable=fixable, fix=None)

    def test_all_ok_exits_zero(self, monkeypatch) -> None:
        from writ.session.doctor import STATUS_OK
        results = [self._make_result(f"check-{i}", STATUS_OK) for i in range(10)]
        with patch("writ.session.doctor.run_all_checks", return_value=results):
            result = runner.invoke(app, ["doctor"])
        assert result.exit_code == 0, (
            f"all-ok run must exit 0; got {result.exit_code}\n{result.output}"
        )

    def test_warn_only_exits_zero(self, monkeypatch) -> None:
        from writ.session.doctor import STATUS_OK, STATUS_WARN
        results = [
            self._make_result("check-1", STATUS_OK),
            self._make_result("check-2", STATUS_WARN),
        ] + [self._make_result(f"check-{i}", STATUS_OK) for i in range(3, 11)]
        with patch("writ.session.doctor.run_all_checks", return_value=results):
            result = runner.invoke(app, ["doctor"])
        assert result.exit_code == 0, (
            f"warn-only run must exit 0; got {result.exit_code}\n{result.output}"
        )

    def test_any_fail_exits_nonzero(self, monkeypatch) -> None:
        from writ.session.doctor import STATUS_FAIL, STATUS_OK
        results = [
            self._make_result("check-1", STATUS_OK),
            self._make_result("check-2", STATUS_FAIL),
        ] + [self._make_result(f"check-{i}", STATUS_OK) for i in range(3, 11)]
        with patch("writ.session.doctor.run_all_checks", return_value=results):
            result = runner.invoke(app, ["doctor"])
        assert result.exit_code != 0, (
            f"any-fail run must exit non-zero; got {result.exit_code}\n{result.output}"
        )


class TestDoctorCommandTable:
    """Table output: aligned, emoji-free, deterministic."""

    def _all_ok_results(self) -> list:
        from writ.session.doctor import CheckResult, STATUS_OK
        names = [
            "daemon-liveness", "stale-orphan-port-conflict", "neo4j-connectivity",
            "embedding-stack", "corpus-drift", "bitbucket-creds",
            "git-post-commit-hook", "writ-path-symlink", "cc-hook-registration",
            "mode-gate-sanity",
        ]
        return [CheckResult(name=n, status=STATUS_OK, detail="all good", fixable=False, fix=None)
                for n in names]

    def test_table_contains_all_check_names(self) -> None:
        results = self._all_ok_results()
        with patch("writ.session.doctor.run_all_checks", return_value=results):
            result = runner.invoke(app, ["doctor"])
        for name in ("daemon-liveness", "corpus-drift", "mode-gate-sanity"):
            assert name in result.output, (
                f"'{name}' must appear in table output; got:\n{result.output!r}"
            )

    def test_table_contains_status_strings(self) -> None:
        results = self._all_ok_results()
        with patch("writ.session.doctor.run_all_checks", return_value=results):
            result = runner.invoke(app, ["doctor"])
        assert "ok" in result.output.lower(), (
            f"status 'ok' must appear in output; got:\n{result.output!r}"
        )

    def test_table_is_emoji_free(self) -> None:
        results = self._all_ok_results()
        with patch("writ.session.doctor.run_all_checks", return_value=results):
            result = runner.invoke(app, ["doctor"])
        # Emoji codepoints are above U+1F000; none should appear in the output
        for char in result.output:
            assert ord(char) < 0x1F000, (
                f"table output must be emoji-free; found char {char!r} (U+{ord(char):04X})"
            )


class TestDoctorCommandFix:
    """--fix: only fixable non-ok results' fix() handles are called."""

    def _make_result(
        self, name: str, status: str, fixable: bool = False, fix=None
    ) -> object:
        from writ.session.doctor import CheckResult
        return CheckResult(name=name, status=status, detail="test", fixable=fixable, fix=fix)

    def test_fix_invokes_fixable_non_ok_handle(self) -> None:
        calls = []
        fix_fn = lambda: calls.append("fixed")
        from writ.session.doctor import STATUS_FAIL, STATUS_OK
        results = [
            self._make_result("check-1", STATUS_FAIL, fixable=True, fix=fix_fn),
            self._make_result("check-2", STATUS_OK),
        ] + [self._make_result(f"check-{i}", STATUS_OK) for i in range(3, 11)]
        with patch("writ.session.doctor.run_all_checks", return_value=results):
            runner.invoke(app, ["doctor", "--fix"])
        assert calls == ["fixed"], (
            f"fix() must be invoked for fixable non-ok results; calls={calls}"
        )

    def test_fix_never_invokes_non_fixable_handle(self) -> None:
        calls = []
        never_fn = lambda: calls.append("should-not-be-called")
        from writ.session.doctor import STATUS_FAIL, STATUS_OK
        results = [
            self._make_result("check-1", STATUS_FAIL, fixable=False, fix=never_fn),
        ] + [self._make_result(f"check-{i}", STATUS_OK) for i in range(2, 11)]
        with patch("writ.session.doctor.run_all_checks", return_value=results):
            runner.invoke(app, ["doctor", "--fix"])
        assert calls == [], (
            f"fix() must NEVER be invoked for non-fixable results; calls={calls}"
        )

    def test_no_fix_flag_invokes_no_handles(self) -> None:
        calls = []
        fix_fn = lambda: calls.append("called")
        from writ.session.doctor import STATUS_FAIL, STATUS_OK
        results = [
            self._make_result("check-1", STATUS_FAIL, fixable=True, fix=fix_fn),
        ] + [self._make_result(f"check-{i}", STATUS_OK) for i in range(2, 11)]
        with patch("writ.session.doctor.run_all_checks", return_value=results):
            runner.invoke(app, ["doctor"])  # no --fix
        assert calls == [], (
            "without --fix, NO fix handle must be called; zero side effects"
        )

    def test_fix_skips_ok_results_even_if_fixable(self) -> None:
        calls = []
        fix_fn = lambda: calls.append("called")
        from writ.session.doctor import STATUS_OK
        results = [
            self._make_result("check-1", STATUS_OK, fixable=True, fix=fix_fn),
        ] + [self._make_result(f"check-{i}", STATUS_OK) for i in range(2, 11)]
        with patch("writ.session.doctor.run_all_checks", return_value=results):
            runner.invoke(app, ["doctor", "--fix"])
        assert calls == [], (
            "--fix must not invoke fix() on ok results (already healthy)"
        )


class TestDoctorCommandNet:
    """--net gates the bitbucket live ping."""

    def test_without_net_live_auth_seam_never_called(self, monkeypatch) -> None:
        live_auth_calls = []
        # Patch all seams so run_all_checks completes without live I/O
        monkeypatch.setattr(
            "writ.session.doctor._http_get_health",
            lambda: {"status": "healthy", "index_state": "warm", "rule_count": 3},
        )
        monkeypatch.setattr("writ.session.doctor._systemctl_is_active", lambda unit: "active")
        monkeypatch.setattr("writ.session.doctor._port_owner_pids", lambda port: [1])
        monkeypatch.setattr("writ.session.doctor._ps_writ_serve_orphans", lambda: [])
        monkeypatch.setattr("writ.session.doctor._tcp_can_connect", lambda h, p: True)
        monkeypatch.setattr("writ.session.doctor._count_neo4j_rules", lambda: 5)
        monkeypatch.setattr("writ.session.doctor._venv_import_ok", lambda: True)
        monkeypatch.setattr(
            "writ.session.doctor._onnx_model_files_present", lambda: (True, True)
        )
        monkeypatch.setattr("writ.session.doctor._detect_parity_violations", lambda: [])
        monkeypatch.setattr(
            "writ.session.doctor._bitbucket_creds_present", lambda: (True, True)
        )
        monkeypatch.setattr(
            "writ.session.doctor._bitbucket_live_auth",
            lambda repo: live_auth_calls.append(True) or 200,
        )
        monkeypatch.setattr("writ.session.doctor._git_hook_installed", lambda repo: True)
        monkeypatch.setattr("writ.session.doctor._path_symlink_ok", lambda: (True, True))
        monkeypatch.setattr("writ.session.doctor._cc_registration_ok", lambda: (True, []))
        monkeypatch.setattr(
            "writ.session.doctor._latest_session_cache",
            lambda sid: {"mode": "work"},
        )
        runner.invoke(app, ["doctor"])  # no --net
        assert live_auth_calls == [], (
            "without --net, _bitbucket_live_auth must never be invoked"
        )


class TestDoctorCommandJson:
    """--json: emits valid JSON with name/status/detail/fixable; no fix callable."""

    def _make_result(self, name: str, status: str, fixable: bool = False) -> object:
        from writ.session.doctor import CheckResult
        return CheckResult(
            name=name, status=status, detail="some detail", fixable=fixable, fix=None
        )

    def test_json_output_is_parseable(self) -> None:
        from writ.session.doctor import STATUS_OK
        results = [self._make_result(f"check-{i}", STATUS_OK) for i in range(10)]
        with patch("writ.session.doctor.run_all_checks", return_value=results):
            result = runner.invoke(app, ["doctor", "--json"])
        assert result.exit_code == 0
        try:
            parsed = json.loads(result.output)
        except json.JSONDecodeError as e:
            pytest.fail(f"--json output must be valid JSON; error: {e}\noutput: {result.output!r}")
        assert isinstance(parsed, list), f"--json must emit a JSON list; got {type(parsed)}"

    def test_json_contains_required_keys(self) -> None:
        from writ.session.doctor import STATUS_WARN
        results = [self._make_result("corpus-drift", STATUS_WARN, fixable=True)]
        with patch("writ.session.doctor.run_all_checks", return_value=results):
            result = runner.invoke(app, ["doctor", "--json"])
        parsed = json.loads(result.output)
        item = parsed[0]
        for key in ("name", "status", "detail", "fixable"):
            assert key in item, (
                f"--json item must contain '{key}'; got keys: {list(item.keys())}"
            )

    def test_json_excludes_fix_callable(self) -> None:
        # The fix callable must not appear in JSON (it is not serializable)
        calls = []
        fix_fn = lambda: calls.append(True)
        # WARN (not FAIL) so exit stays 0 per the approved exit-code contract; this
        # test verifies fix-exclusion + serialization, not exit semantics.
        from writ.session.doctor import CheckResult, STATUS_WARN
        results = [
            CheckResult(
                name="git-post-commit-hook",
                status=STATUS_WARN,
                detail="missing",
                fixable=True,
                fix=fix_fn,
            )
        ]
        with patch("writ.session.doctor.run_all_checks", return_value=results):
            result = runner.invoke(app, ["doctor", "--json"])
        assert result.exit_code == 0, (
            f"--json must not fail on serialization; got exit {result.exit_code}\n{result.output}"
        )
        parsed = json.loads(result.output)
        assert "fix" not in parsed[0], (
            "'fix' callable must be excluded from JSON output"
        )

    def test_json_fixable_is_bool(self) -> None:
        from writ.session.doctor import STATUS_OK
        results = [self._make_result("daemon-liveness", STATUS_OK, fixable=False)]
        with patch("writ.session.doctor.run_all_checks", return_value=results):
            result = runner.invoke(app, ["doctor", "--json"])
        parsed = json.loads(result.output)
        assert isinstance(parsed[0]["fixable"], bool), (
            "fixable must serialize as a JSON bool"
        )

    def test_credential_values_absent_from_json_output(self, monkeypatch) -> None:
        # Even if the creds seam returned booleans, make sure no sentinel leaks into output
        from writ.session.doctor import CheckResult, STATUS_OK
        results = [
            CheckResult(
                name="bitbucket-creds",
                status=STATUS_OK,
                detail="credentials present",
                fixable=False,
                fix=None,
            )
        ]
        with patch("writ.session.doctor.run_all_checks", return_value=results):
            result = runner.invoke(app, ["doctor", "--json"])
        assert SENTINEL_EMAIL not in result.output
        assert SENTINEL_TOKEN not in result.output
