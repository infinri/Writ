"""RED-phase test skeleton for P3 `writ logs backup` (plan.md P3 section).

Pins the ## Capabilities lines covering `writ/session/log_backup.py::backup_archives`,
the `writ logs backup` CLI command wired onto the existing `logs_app` Typer sub-app in
`writ/cli.py`, and the new `config.get_logs_backup_dest()` getter in `writ/config.py`.

RED PHASE: `writ.session.log_backup` does not exist yet and `config.get_logs_backup_dest`
has not been added to `writ/config.py` yet, and the `backup` command has not been wired
onto `logs_app` yet. `from writ.session.log_backup import backup_archives` is expected to
raise `ModuleNotFoundError` at collection time -- that failure IS the expected RED outcome
until `writ/session/log_backup.py` and the CLI/config wiring land.

Per plan.md's ## Files bullet, `logs backup` looks up `config.get_logs_backup_dest()` (and
delegates the actual copy work to `log_backup.backup_archives`) via a call-time attribute
lookup on their owning modules (mirroring the established `writ.session.log_rotation.
rotate_logs` lazy-import convention from tests/test_cli_logs_rotate.py) -- so tests below
patch `writ.session.log_backup.backup_archives` for the delegate and `writ.config.
get_logs_backup_dest` for the dest getter, never a name copied into `writ.cli`'s own
namespace at import time.

Fixtures build small on-disk archive trees directly under `writ.shared.logging`'s path
helpers (`log_root`, `stream_path`, `archive_dir` via `archive_path`) -- reused, not
reimplemented (## Analysis "Reuse (do NOT re-implement)").

Run ONLY this file (never bare pytest -- that wipes the shared graph):
  .venv/bin/python -m pytest tests/test_logs_backup.py -v
"""
from __future__ import annotations

import gzip
import inspect
import os
import stat
from datetime import date, datetime, timezone
from pathlib import Path
from unittest.mock import patch

import pytest
from typer.testing import CliRunner

from writ import config
from writ.cli import app
from writ.shared.logging import archive_path, log_root, stream_path
from writ.session.log_backup import backup_archives

runner = CliRunner()


# ---------------------------------------------------------------------------
# Fixture helpers (TEST-FIXTURE-001): each test builds only the on-disk shape
# it actually needs, via these small factories.
# ---------------------------------------------------------------------------


def _write_gz_archive(project: str, stream: str, day: date, text: str) -> Path:
    """A compressed archive generation `<project>/archive/<stream>-<day>.jsonl.gz`."""
    uncompressed = archive_path(project, stream, day)
    gz = uncompressed.with_suffix(uncompressed.suffix + ".gz")
    gz.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(gz, "wt") as fh:
        fh.write(text)
    return gz


def _write_uncompressed_archive(project: str, stream: str, day: date, text: str) -> Path:
    """An UNCOMPRESSED archive generation -- must never be copied by backup."""
    path = archive_path(project, stream, day)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


def _write_live_stream(project: str, stream: str, text: str) -> Path:
    """A live `<project>/<stream>.jsonl` file -- must never be copied by backup."""
    path = stream_path(project, stream)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


@pytest.fixture(autouse=True)
def _hermetic_log_env(tmp_path, monkeypatch):
    """Every test gets its own log root and a clean WRIT_* env slate."""
    monkeypatch.setenv("WRIT_LOG_ROOT", str(tmp_path / "logs"))
    monkeypatch.delenv("WRIT_LOG_PROJECT", raising=False)
    monkeypatch.delenv("WRIT_FRICTION_LOG", raising=False)
    return tmp_path


# ===========================================================================
# backup_archives(): copies ONLY *.jsonl.gz, preserving <project>/archive/
# ===========================================================================


def test_backup_copies_gz_archive_generation_preserving_project_archive_layout(tmp_path):
    project = "proj-a"
    gz = _write_gz_archive(project, "audit", date(2025, 6, 1), '{"event": "old"}\n')
    dest = tmp_path / "dest"

    summary = backup_archives(dest)

    expected = dest / gz.relative_to(log_root())
    assert expected.is_file(), f"expected {expected} to exist after backup_archives()"
    with gzip.open(expected, "rt") as fh:
        assert '"event": "old"' in fh.read()
    assert summary["copied"] == 1


def test_backup_copies_nested_project_scope_preserving_layout(tmp_path):
    """A clone-stable nested scope like github.com/org/repo must round-trip intact."""
    project = "github.com/org/repo"
    gz = _write_gz_archive(project, "friction", date(2025, 5, 1), '{"event": "nested"}\n')
    dest = tmp_path / "dest"

    summary = backup_archives(dest)

    expected = dest / "github.com" / "org" / "repo" / "archive" / gz.name
    assert expected == dest / gz.relative_to(log_root())
    assert expected.is_file(), (
        f"nested project scope must preserve its full <project>/archive/ layout "
        f"under dest; expected {expected}"
    )
    assert summary["copied"] == 1


def test_backup_copies_multiple_archive_generations_across_projects(tmp_path):
    gz1 = _write_gz_archive("proj-multi-a", "audit", date(2025, 6, 1), '{"event": "one"}\n')
    gz2 = _write_gz_archive("proj-multi-b", "metrics", date(2025, 6, 2), '{"event": "two"}\n')
    dest = tmp_path / "dest"

    summary = backup_archives(dest)

    assert (dest / gz1.relative_to(log_root())).is_file()
    assert (dest / gz2.relative_to(log_root())).is_file()
    assert summary["copied"] == 2


def test_backup_bytes_reflects_total_size_of_copied_files(tmp_path):
    gz1 = _write_gz_archive("proj-h", "audit", date(2025, 6, 8), '{"event": "one"}\n')
    gz2 = _write_gz_archive("proj-h", "friction", date(2025, 6, 8), '{"event": "two"}\n')
    dest = tmp_path / "dest"

    summary = backup_archives(dest)

    assert summary["bytes"] == gz1.stat().st_size + gz2.stat().st_size


# ===========================================================================
# backup_archives(): NEVER copies live .jsonl or uncompressed archive .jsonl
# ===========================================================================


def test_backup_never_copies_live_stream_file(tmp_path):
    project = "proj-b"
    live = _write_live_stream(project, "audit", '{"event": "live"}\n')
    _write_gz_archive(project, "audit", date(2025, 6, 1), '{"event": "archived"}\n')
    dest = tmp_path / "dest"

    backup_archives(dest)

    dest_live = dest / live.relative_to(log_root())
    assert not dest_live.exists(), "a live .jsonl stream file must never be copied to dest"
    assert live.is_file(), "the live source file must still be present at source"


def test_backup_never_copies_uncompressed_archive_jsonl(tmp_path):
    project = "proj-c"
    uncompressed = _write_uncompressed_archive(
        project, "metrics", date(2025, 6, 2), '{"event": "unzipped"}\n'
    )
    dest = tmp_path / "dest"

    backup_archives(dest)

    dest_uncompressed = dest / uncompressed.relative_to(log_root())
    assert not dest_uncompressed.exists(), (
        "an uncompressed archive .jsonl generation must never be copied to dest"
    )
    assert uncompressed.is_file(), "the uncompressed source generation must still be present"


def test_backup_is_copy_not_move_source_gz_remains_in_place(tmp_path):
    project = "proj-d"
    gz = _write_gz_archive(project, "audit", date(2025, 6, 3), '{"event": "keepme"}\n')
    dest = tmp_path / "dest"

    backup_archives(dest)

    assert gz.is_file(), (
        "backup_archives must COPY (not move) -- the source archive generation must "
        "remain in place after a successful backup"
    )


def test_backup_preserves_source_mtime_on_copy(tmp_path):
    """Copy = shutil.copy2 (preserves mtime) per plan.md's Contracts section."""
    project = "proj-g"
    gz = _write_gz_archive(project, "audit", date(2025, 6, 6), '{"event": "mtime"}\n')
    old_time = datetime(2020, 1, 1, tzinfo=timezone.utc).timestamp()
    os.utime(gz, (old_time, old_time))
    dest = tmp_path / "dest"

    backup_archives(dest)

    dest_file = dest / gz.relative_to(log_root())
    assert dest_file.stat().st_mtime == pytest.approx(gz.stat().st_mtime, abs=2)


# ===========================================================================
# backup_archives(): idempotent (skip same-size dest, re-copy on mismatch)
# ===========================================================================


def test_backup_skips_dest_file_with_matching_size_on_second_run(tmp_path):
    project = "proj-e"
    _write_gz_archive(project, "audit", date(2025, 6, 4), '{"event": "same"}\n')
    dest = tmp_path / "dest"

    first = backup_archives(dest)
    assert first["copied"] == 1
    assert first["skipped"] == 0

    second = backup_archives(dest)
    assert second["copied"] == 0, "an unchanged dest file must not be re-copied"
    assert second["skipped"] == 1, "an unchanged dest file must be counted as skipped"


def test_backup_recopies_dest_file_with_size_mismatch(tmp_path):
    project = "proj-f"
    gz = _write_gz_archive(project, "audit", date(2025, 6, 5), '{"event": "v1"}\n')
    dest = tmp_path / "dest"

    backup_archives(dest)
    dest_file = dest / gz.relative_to(log_root())
    dest_file.write_bytes(b"stale-mismatched-content-of-a-different-length")
    stale_size = dest_file.stat().st_size
    assert stale_size != gz.stat().st_size

    summary = backup_archives(dest)

    assert summary["copied"] == 1, "a dest file whose size differs from source must be re-copied"
    assert summary["skipped"] == 0
    assert dest_file.stat().st_size == gz.stat().st_size
    assert dest_file.stat().st_size != stale_size


# ===========================================================================
# backup_archives(): dest UNDER the log root must not re-copy its own output
# (regression: os.walk descending into dest -> unbounded backup/backup nesting)
# ===========================================================================


def test_backup_dest_inside_log_root_does_not_recopy_its_own_output(tmp_path):
    """A --dest that lives UNDER the walked log root must never re-copy backup's
    own prior *.jsonl.gz output into itself, which would nest backup/backup/...
    deeper on every run and grow disk without bound."""
    gz = _write_gz_archive("proj-self", "audit", date(2025, 6, 11), '{"event": "x"}\n')
    dest = log_root() / "backup"  # dest INSIDE the walked log root

    first = backup_archives(dest)
    second = backup_archives(dest)
    third = backup_archives(dest)

    # The archive is copied exactly once, then skipped on every subsequent run.
    assert first["copied"] == 1
    assert first["skipped"] == 0
    assert (dest / gz.relative_to(log_root())).is_file()
    # No nested self-copy: the dest subtree is pruned from the walk.
    assert not (dest / "backup").exists(), (
        "a dest under the log root must not re-copy its own output into a nested "
        "backup/backup/... tree"
    )
    # Idempotent + bounded: counts are stable across repeated runs.
    assert second == {"copied": 0, "skipped": 1, "bytes": 0, "errors": 0}
    assert third == second, f"repeated runs must be stable; got {third!r} vs {second!r}"


# ===========================================================================
# backup_archives(): fail-soft per-file (errors counted, run continues)
# ===========================================================================


def test_backup_counts_unreadable_source_file_as_error_and_continues(tmp_path):
    bad_gz = _write_gz_archive("proj-bad", "audit", date(2025, 6, 7), '{"event": "bad"}\n')
    good_gz = _write_gz_archive("proj-good", "audit", date(2025, 6, 7), '{"event": "good"}\n')
    bad_gz.chmod(0o000)
    dest = tmp_path / "dest"

    try:
        summary = backup_archives(dest)  # must not raise
    finally:
        bad_gz.chmod(stat.S_IRWXU)

    assert summary["errors"] == 1, f"the unreadable source must be counted as an error; got {summary!r}"
    good_dest = dest / good_gz.relative_to(log_root())
    assert good_dest.is_file(), "a failing source file must not abort the rest of the backup run"


# ===========================================================================
# backup_archives(): return-value shape + signature contract
# ===========================================================================


def test_backup_archives_returns_dict_with_exact_expected_keys(tmp_path):
    dest = tmp_path / "dest"
    summary = backup_archives(dest)
    assert set(summary.keys()) == {"copied", "skipped", "bytes", "errors"}
    assert all(isinstance(v, int) for v in summary.values())


def test_backup_archives_returns_all_zero_summary_when_log_root_missing(tmp_path, monkeypatch):
    monkeypatch.setenv("WRIT_LOG_ROOT", str(tmp_path / "does-not-exist"))
    dest = tmp_path / "dest"

    summary = backup_archives(dest)

    assert summary == {"copied": 0, "skipped": 0, "bytes": 0, "errors": 0}


def test_backup_archives_never_raises_on_uncreatable_dest(tmp_path):
    """ERR-HANDLE-003: a dest that can't be created/written must not crash the run."""
    project = "proj-uncreatable-dest"
    _write_gz_archive(project, "audit", date(2025, 6, 9), '{"event": "x"}\n')
    blocked_parent = tmp_path / "blocked"
    blocked_parent.mkdir()
    blocked_parent.chmod(stat.S_IREAD | stat.S_IEXEC)
    dest = blocked_parent / "dest"

    try:
        summary = backup_archives(dest)  # must not raise
    finally:
        blocked_parent.chmod(stat.S_IRWXU)

    assert summary["errors"] >= 1


def test_backup_archives_signature_accepts_dest_positional_and_optional_root_kwarg():
    sig = inspect.signature(backup_archives)
    params = sig.parameters
    assert "dest" in params
    assert "root" in params, "backup_archives must accept an injectable `root` kwarg"
    assert params["root"].default is None
    assert params["root"].kind == inspect.Parameter.KEYWORD_ONLY


def test_backup_archives_honors_explicit_root_kwarg_over_env(tmp_path, monkeypatch):
    """SOLID-DIP-001: an injected `root` must be honored independent of WRIT_LOG_ROOT,
    so a caller (or a future scheduled job) can point backup at an arbitrary tree."""
    other_root = tmp_path / "other-root"
    gz = _write_gz_archive("proj-root-kwarg", "audit", date(2025, 6, 10), '{"event": "r"}\n')
    # Relocate the fixture under other_root manually to prove `root=` is actually used,
    # not just log_root()'s env-driven default.
    real_root = log_root()
    relocated = other_root / gz.relative_to(real_root)
    relocated.parent.mkdir(parents=True, exist_ok=True)
    relocated.write_bytes(gz.read_bytes())
    dest = tmp_path / "dest"

    summary = backup_archives(dest, root=other_root)

    assert summary["copied"] == 1
    assert (dest / relocated.relative_to(other_root)).is_file()


# ===========================================================================
# config.get_logs_backup_dest() -- mirrors get_bitbucket_email / get_hnsw_cache_dir
# ===========================================================================


def test_get_logs_backup_dest_reads_configured_value(tmp_path):
    toml_path = tmp_path / "writ.toml"
    toml_path.write_text('[logs]\nbackup_dest = "~/writ-backups"\n')

    result = config.get_logs_backup_dest(str(toml_path))

    assert result == os.path.expanduser("~/writ-backups"), (
        "get_logs_backup_dest must expand a leading ~ (os.path.expanduser), "
        "mirroring get_hnsw_cache_dir"
    )


def test_get_logs_backup_dest_returns_none_when_logs_section_absent(tmp_path):
    toml_path = tmp_path / "writ.toml"
    toml_path.write_text('[neo4j]\nuri = "bolt://localhost:7687"\n')

    assert config.get_logs_backup_dest(str(toml_path)) is None


def test_get_logs_backup_dest_returns_none_when_backup_dest_key_absent(tmp_path):
    toml_path = tmp_path / "writ.toml"
    toml_path.write_text("[logs]\n")

    assert config.get_logs_backup_dest(str(toml_path)) is None


def test_get_logs_backup_dest_returns_none_when_config_file_absent(tmp_path):
    missing = tmp_path / "does-not-exist.toml"

    assert config.get_logs_backup_dest(str(missing)) is None


# ===========================================================================
# `writ logs backup` CLI: dest resolution order (option -> config -> error)
# ===========================================================================


class TestCliLogsBackupDestResolution:
    def test_backup_without_dest_and_no_configured_dest_exits_nonzero_with_clear_message(
        self,
    ) -> None:
        with patch("writ.config.get_logs_backup_dest", return_value=None):
            result = runner.invoke(app, ["logs", "backup"])

        assert result.exit_code != 0, (
            f"'writ logs backup' with no --dest and no configured backup_dest must "
            f"exit non-zero; got {result.exit_code}. Output: {result.output!r}"
        )
        assert "dest" in result.output.lower(), (
            f"the error message must clearly reference the missing destination; "
            f"got: {result.output!r}"
        )

    def test_backup_uses_configured_dest_when_option_omitted(self, tmp_path) -> None:
        configured = tmp_path / "configured-dest"
        fake_summary = {"copied": 0, "skipped": 0, "bytes": 0, "errors": 0}
        with (
            patch("writ.config.get_logs_backup_dest", return_value=str(configured)),
            patch(
                "writ.session.log_backup.backup_archives", return_value=fake_summary
            ) as mock_backup,
        ):
            result = runner.invoke(app, ["logs", "backup"])

        assert result.exit_code == 0, result.output
        assert mock_backup.call_count == 1
        called_dest = (
            mock_backup.call_args.args[0]
            if mock_backup.call_args.args
            else mock_backup.call_args.kwargs.get("dest")
        )
        assert str(called_dest) == str(configured), (
            f"the configured backup_dest must be used when --dest is omitted; "
            f"got {called_dest!r}"
        )

    def test_backup_dest_option_takes_precedence_over_configured_dest(self, tmp_path) -> None:
        configured = tmp_path / "configured-dest"
        explicit = tmp_path / "explicit-dest"
        fake_summary = {"copied": 0, "skipped": 0, "bytes": 0, "errors": 0}
        with (
            patch("writ.config.get_logs_backup_dest", return_value=str(configured)),
            patch(
                "writ.session.log_backup.backup_archives", return_value=fake_summary
            ) as mock_backup,
        ):
            result = runner.invoke(app, ["logs", "backup", "--dest", str(explicit)])

        assert result.exit_code == 0, result.output
        called_dest = (
            mock_backup.call_args.args[0]
            if mock_backup.call_args.args
            else mock_backup.call_args.kwargs.get("dest")
        )
        assert str(called_dest) == str(explicit), (
            "an explicit --dest must take precedence over the configured backup_dest"
        )


# ===========================================================================
# `writ logs backup` CLI: invokes backup_archives() and formats its summary
# ===========================================================================


class TestCliLogsBackupInvokesDelegateAndPrintsSummary:
    def test_backup_calls_backup_archives_exactly_once(self, tmp_path) -> None:
        dest = tmp_path / "dest"
        fake_summary = {"copied": 3, "skipped": 1, "bytes": 4096, "errors": 0}
        with patch(
            "writ.session.log_backup.backup_archives", return_value=fake_summary
        ) as mock_backup:
            result = runner.invoke(app, ["logs", "backup", "--dest", str(dest)])

        assert mock_backup.call_count == 1, (
            f"'writ logs backup' must call backup_archives() exactly once; "
            f"got {mock_backup.call_count} calls. Output: {result.output!r}"
        )
        assert result.exit_code == 0, result.output

    def test_backup_prints_copied_skipped_bytes_errors_summary(self, tmp_path) -> None:
        dest = tmp_path / "dest"
        fake_summary = {"copied": 3, "skipped": 1, "bytes": 4096, "errors": 0}
        with patch("writ.session.log_backup.backup_archives", return_value=fake_summary):
            result = runner.invoke(app, ["logs", "backup", "--dest", str(dest)])

        assert "copied=3" in result.output, result.output
        assert "skipped=1" in result.output, result.output
        assert "bytes=4096" in result.output, result.output
        assert "errors=0" in result.output, result.output


# ===========================================================================
# `writ logs backup` CLI: fail-soft (errors > 0 still exits 0)
# ===========================================================================


class TestCliLogsBackupFailSoft:
    def test_backup_exits_zero_and_prints_summary_when_errors_present(self, tmp_path) -> None:
        dest = tmp_path / "dest"
        fake_summary = {"copied": 2, "skipped": 0, "bytes": 100, "errors": 1}
        with patch("writ.session.log_backup.backup_archives", return_value=fake_summary):
            result = runner.invoke(app, ["logs", "backup", "--dest", str(dest)])

        assert result.exit_code == 0, (
            f"a partial-failure backup run (errors > 0) must still exit 0 (fail-soft); "
            f"got {result.exit_code}. Output: {result.output!r}"
        )
        assert "errors=1" in result.output, result.output


# ===========================================================================
# `writ logs backup --help`
# ===========================================================================


class TestCliLogsBackupHelp:
    def test_backup_help_exits_zero_and_mentions_dest(self) -> None:
        result = runner.invoke(app, ["logs", "backup", "--help"])
        assert result.exit_code == 0, result.output
        from tests._ansi import plain

        assert "--dest" in plain(result.output)


# ===========================================================================
# End-to-end smoke test against a real (empty) hermetic log root -- no
# mocking of backup_archives itself, exercising the CLI-to-backup wiring for
# the trivial all-zero case (mirrors TestLogsRotateEndToEndSmoke).
# ===========================================================================


class TestCliLogsBackupEndToEndSmoke:
    def test_backup_against_empty_log_root_exits_zero_with_all_zero_summary(
        self, tmp_path,
    ) -> None:
        dest = tmp_path / "dest"

        result = runner.invoke(app, ["logs", "backup", "--dest", str(dest)])

        assert result.exit_code == 0, (
            f"'writ logs backup' must exit 0 against an empty/missing log root; "
            f"got {result.exit_code}. Output: {result.output!r}"
        )
        assert "copied=0" in result.output, result.output
        assert "skipped=0" in result.output, result.output
        assert "bytes=0" in result.output, result.output
        assert "errors=0" in result.output, result.output
