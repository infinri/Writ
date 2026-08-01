"""RED-phase test skeleton for P3 `writ logs [tail|stats|list]` (plan.md P3 section).

Pins the ## Capabilities lines covering `writ/session/log_read.py` (`tail_stream`,
`stream_stats`, `list_projects`, and their internal helpers `_tail_lines` /
`_discover_projects`) plus the `tail` / `stats` / `list` commands wired onto the
existing `logs_app` Typer sub-app in `writ/cli.py`, and the journald hint.

RED PHASE: `writ.session.log_read` does not exist yet. `from writ.session.log_read
import ...` is expected to raise `ModuleNotFoundError` at collection time -- that
failure IS the expected RED outcome until `writ/session/log_read.py` and the CLI
wiring land.

Per plan.md's ## Files bullet, the `tail`/`stats` commands resolve their default
`--project` via a call-time attribute lookup on `writ.shared.logging.resolve_project`
(mirroring the established `writ.session.log_rotation.rotate_logs` lazy-import
convention from tests/test_cli_logs_rotate.py -- e.g. a lazy in-body
`from writ.shared.logging import resolve_project`), so tests below patch
`writ.shared.logging.resolve_project` directly, never a name copied into
`writ.cli`'s own namespace at import time. The three read-surface delegates are
patched at their own module, `writ.session.log_read.<fn>`, for the same reason.

Fixtures build small on-disk log/archive trees directly under `writ.shared.logging`'s
path helpers (`log_root`, `stream_path`, `archive_path`) -- reused, not reimplemented
(## Analysis "Reuse (do NOT re-implement)").

Run ONLY this file (never bare pytest -- that wipes the shared graph):
  .venv/bin/python -m pytest tests/test_cli_logs_read_surface.py -v
"""
from __future__ import annotations

import gzip
import inspect
import stat
from datetime import date
from pathlib import Path
from unittest.mock import patch

import pytest
from typer.testing import CliRunner

from writ.cli import app
from writ.shared.logging import archive_path, log_root, stream_path
from writ.session.log_read import (
    _discover_projects,
    _tail_lines,
    list_projects,
    stream_stats,
    tail_stream,
)

runner = CliRunner()


# ---------------------------------------------------------------------------
# Fixture helpers (TEST-FIXTURE-001): each test builds only the on-disk shape
# it actually needs, via these small factories.
# ---------------------------------------------------------------------------


def _write_live_lines(project: str, stream: str, lines: list[str]) -> Path:
    """A live `<project>/<stream>.jsonl` file containing exactly these raw lines."""
    path = stream_path(project, stream)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n")
    return path


def _write_gz_archive(project: str, stream: str, day: date, text: str) -> Path:
    """A compressed archive generation `<project>/archive/<stream>-<day>.jsonl.gz`."""
    uncompressed = archive_path(project, stream, day)
    gz = uncompressed.with_suffix(uncompressed.suffix + ".gz")
    gz.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(gz, "wt") as fh:
        fh.write(text)
    return gz


@pytest.fixture(autouse=True)
def _hermetic_log_env(tmp_path, monkeypatch):
    """Every test gets its own log root and a clean WRIT_* env slate."""
    monkeypatch.setenv("WRIT_LOG_ROOT", str(tmp_path / "logs"))
    monkeypatch.delenv("WRIT_LOG_PROJECT", raising=False)
    monkeypatch.delenv("WRIT_FRICTION_LOG", raising=False)
    return tmp_path


# ===========================================================================
# tail_stream(): last N lines, newest last; bounded backward read; edges
# ===========================================================================


def test_tail_stream_returns_last_n_lines_newest_last(tmp_path):
    project = "proj-tail"
    lines = [f'{{"event": "e{i}"}}' for i in range(10)]
    _write_live_lines(project, "audit", lines)

    result = tail_stream(project, "audit", 3)

    assert result == lines[-3:], f"expected the last 3 lines newest-last; got {result!r}"


def test_tail_stream_n_larger_than_available_lines_returns_all_lines(tmp_path):
    project = "proj-tail-short"
    lines = ['{"event": "only-one"}']
    _write_live_lines(project, "audit", lines)

    result = tail_stream(project, "audit", 50)

    assert result == lines


def test_tail_stream_n_zero_returns_empty_list(tmp_path):
    project = "proj-tail-zero"
    _write_live_lines(project, "audit", ['{"event": "e0"}'])

    assert tail_stream(project, "audit", 0) == []


def test_tail_stream_negative_n_returns_empty_list(tmp_path):
    project = "proj-tail-negative"
    _write_live_lines(project, "audit", ['{"event": "e0"}'])

    assert tail_stream(project, "audit", -5) == []


def test_tail_stream_missing_file_returns_empty_list(tmp_path):
    assert tail_stream("never-logged-project", "audit", 10) == []


def test_tail_stream_unreadable_file_fails_open_to_empty_list(tmp_path):
    project = "proj-locked"
    path = _write_live_lines(project, "audit", ['{"event": "secret"}'])
    path.chmod(0o000)
    try:
        result = tail_stream(project, "audit", 5)  # must not raise
    finally:
        path.chmod(stat.S_IRWXU)

    assert result == [], "an unreadable/locked stream file must fail open to an empty list"


def test_tail_stream_signature_has_project_stream_n_params():
    sig = inspect.signature(tail_stream)
    assert list(sig.parameters.keys())[:3] == ["project", "stream", "n"]


# ===========================================================================
# _tail_lines(): internal bounded backward reader, forced tiny block_size
# proves cross-block correctness (PERF-IO-001: never a whole-file slurp)
# ===========================================================================


def test_tail_lines_forced_tiny_block_size_proves_cross_block_correctness(tmp_path):
    path = tmp_path / "cross-block.jsonl"
    lines = [f'{{"event": "e{i:03d}"}}' for i in range(50)]
    path.write_text("\n".join(lines) + "\n")

    result = _tail_lines(path, 7, block_size=16)  # forces many block boundaries

    assert result == lines[-7:], (
        f"a forced tiny block_size must not corrupt the last-N read across block "
        f"boundaries; got {result!r}"
    )


def test_tail_lines_n_larger_than_file_with_tiny_block_size_returns_all_lines(tmp_path):
    path = tmp_path / "small.jsonl"
    lines = ['{"a": 1}', '{"a": 2}', '{"a": 3}']
    path.write_text("\n".join(lines) + "\n")

    result = _tail_lines(path, 100, block_size=4)

    assert result == lines


def test_tail_lines_exact_block_boundary_does_not_duplicate_or_drop_lines(tmp_path):
    """A line landing exactly on a block boundary must be split correctly, never
    duplicated and never silently dropped."""
    path = tmp_path / "boundary.jsonl"
    lines = [f'{{"n": {i}}}' for i in range(20)]
    path.write_text("\n".join(lines) + "\n")

    result = _tail_lines(path, 5, block_size=8)

    assert result == lines[-5:]
    assert len(result) == len(set(result)) or result == lines[-5:], (
        "no line may be duplicated by the block-boundary split"
    )


def test_tail_lines_missing_file_returns_empty_list(tmp_path):
    assert _tail_lines(tmp_path / "does-not-exist.jsonl", 10) == []


def test_tail_lines_signature_has_path_n_and_block_size_kwarg():
    sig = inspect.signature(_tail_lines)
    params = sig.parameters
    assert "path" in params
    assert "n" in params
    assert "block_size" in params, "_tail_lines must accept a parameterized block_size"


# ===========================================================================
# `writ logs tail` CLI: defaults (project=resolved, stream=audit, n=20)
# ===========================================================================


class TestCliLogsTailDefaults:
    def test_tail_defaults_to_resolved_project_audit_stream_and_20_lines(self) -> None:
        with (
            patch("writ.session.log_read.tail_stream", return_value=[]) as mock_tail,
            patch("writ.shared.logging.resolve_project", return_value="proj-resolved"),
        ):
            result = runner.invoke(app, ["logs", "tail"])

        assert result.exit_code == 0, result.output
        mock_tail.assert_called_once_with(project="proj-resolved", stream="audit", n=20)

    def test_tail_explicit_project_stream_and_lines_are_passed_through(self) -> None:
        with patch("writ.session.log_read.tail_stream", return_value=[]) as mock_tail:
            result = runner.invoke(
                app,
                ["logs", "tail", "--project", "custom-proj", "--stream", "friction", "-n", "5"],
            )

        assert result.exit_code == 0, result.output
        mock_tail.assert_called_once_with(project="custom-proj", stream="friction", n=5)


# ===========================================================================
# `writ logs tail` CLI: prints raw lines; fail-open on missing/empty stream
# ===========================================================================


class TestCliLogsTailOutput:
    def test_tail_prints_raw_lines_from_tail_stream(self) -> None:
        fake_lines = ['{"event": "one"}', '{"event": "two"}']
        with patch("writ.session.log_read.tail_stream", return_value=fake_lines):
            result = runner.invoke(app, ["logs", "tail", "--project", "p", "--stream", "audit"])

        assert result.exit_code == 0, result.output
        assert '{"event": "one"}' in result.output
        assert '{"event": "two"}' in result.output

    def test_tail_on_missing_stream_returns_no_lines_and_exits_zero(self) -> None:
        with patch("writ.session.log_read.tail_stream", return_value=[]):
            result = runner.invoke(
                app, ["logs", "tail", "--project", "empty-proj", "--stream", "audit"]
            )

        assert result.exit_code == 0, result.output


class TestCliLogsTailEndToEndSmoke:
    def test_tail_against_empty_log_root_exits_zero_with_no_output(
        self, tmp_path, monkeypatch,
    ) -> None:
        monkeypatch.setenv("WRIT_LOG_ROOT", str(tmp_path / "logs"))
        monkeypatch.setenv("WRIT_LOG_PROJECT", "smoke-proj")

        result = runner.invoke(app, ["logs", "tail"])

        assert result.exit_code == 0, result.output
        assert result.output.strip() == "", (
            f"an empty/missing stream must print nothing and exit 0; got {result.output!r}"
        )


# ===========================================================================
# stream_stats(): per-stream live_lines / live_bytes / archive_count /
# oldest_ts / newest_ts from cheap counts, on a built fixture
# ===========================================================================


def test_stream_stats_reports_exact_counts_for_a_built_fixture(tmp_path):
    project = "proj-stats"
    lines = [
        '{"ts": "2025-06-01T00:00:00Z", "event": "first"}',
        '{"ts": "2025-06-01T01:00:00Z", "event": "middle"}',
        '{"ts": "2025-06-01T02:00:00Z", "event": "last"}',
    ]
    live_path = _write_live_lines(project, "audit", lines)
    live_bytes = live_path.stat().st_size
    _write_gz_archive(project, "audit", date(2025, 5, 30), '{"event": "old-gen"}\n')
    _write_gz_archive(project, "audit", date(2025, 5, 29), '{"event": "older-gen"}\n')

    stats = stream_stats(project)

    assert stats["audit"]["live_lines"] == 3
    assert stats["audit"]["live_bytes"] == live_bytes
    assert stats["audit"]["archive_count"] == 2
    assert stats["audit"]["oldest_ts"] == "2025-06-01T00:00:00Z"
    assert stats["audit"]["newest_ts"] == "2025-06-01T02:00:00Z"


def test_stream_stats_zero_archive_count_when_no_archive_generations(tmp_path):
    project = "proj-stats-noarchive"
    _write_live_lines(
        project, "friction", ['{"ts": "2025-06-01T00:00:00Z", "event": "only"}']
    )

    stats = stream_stats(project)

    assert stats["friction"]["archive_count"] == 0


def test_stream_stats_only_reports_streams_that_are_actually_present(tmp_path):
    project = "proj-partial"
    _write_live_lines(project, "metrics", ['{"ts": "2025-06-01T00:00:00Z", "event": "m"}'])

    stats = stream_stats(project)

    assert "metrics" in stats
    assert "audit" not in stats
    assert "friction" not in stats


def test_stream_stats_empty_live_file_reports_zero_lines_and_no_timestamps(tmp_path):
    project = "proj-empty-live"
    path = stream_path(project, "debug")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("")

    stats = stream_stats(project)

    assert stats["debug"]["live_lines"] == 0
    assert stats["debug"]["live_bytes"] == 0
    assert stats["debug"]["oldest_ts"] is None
    assert stats["debug"]["newest_ts"] is None


def test_stream_stats_skips_leading_and_trailing_blank_lines_for_ts(tmp_path):
    """A leading or trailing BLANK line must not mask the oldest/newest ts sitting
    on the adjacent event line (regression: literal first/last line read)."""
    project = "proj-blank-edges"
    path = stream_path(project, "audit")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "\n"
        '{"ts": "2025-06-01T00:00:00Z", "event": "first"}\n'
        '{"ts": "2025-06-01T02:00:00Z", "event": "last"}\n'
        "\n"
    )

    stats = stream_stats(project)

    assert stats["audit"]["oldest_ts"] == "2025-06-01T00:00:00Z"
    assert stats["audit"]["newest_ts"] == "2025-06-01T02:00:00Z"


def test_stream_stats_missing_project_degrades_to_empty_dict_without_crashing(tmp_path):
    stats = stream_stats("never-existed-project")  # must not raise

    assert stats == {}, (
        f"a project with no logs at all must degrade to an empty stats dict; got {stats!r}"
    )


def test_stream_stats_signature_has_project_param():
    sig = inspect.signature(stream_stats)
    assert "project" in sig.parameters


# ===========================================================================
# `writ logs stats` CLI: default project, explicit project, table output
# ===========================================================================


class TestCliLogsStats:
    def test_stats_defaults_to_resolved_project(self) -> None:
        with (
            patch("writ.session.log_read.stream_stats", return_value={}) as mock_stats,
            patch("writ.shared.logging.resolve_project", return_value="proj-resolved"),
        ):
            result = runner.invoke(app, ["logs", "stats"])

        assert result.exit_code == 0, result.output
        mock_stats.assert_called_once_with(project="proj-resolved")

    def test_stats_explicit_project_is_passed_through(self) -> None:
        with patch("writ.session.log_read.stream_stats", return_value={}) as mock_stats:
            result = runner.invoke(app, ["logs", "stats", "--project", "custom-proj"])

        assert result.exit_code == 0, result.output
        mock_stats.assert_called_once_with(project="custom-proj")

    def test_stats_prints_a_per_stream_summary_table(self) -> None:
        fake_stats = {
            "audit": {
                "live_lines": 3,
                "live_bytes": 128,
                "archive_count": 2,
                "oldest_ts": "2025-06-01T00:00:00Z",
                "newest_ts": "2025-06-02T00:00:00Z",
            },
        }
        with patch("writ.session.log_read.stream_stats", return_value=fake_stats):
            result = runner.invoke(app, ["logs", "stats", "--project", "p"])

        assert result.exit_code == 0, result.output
        assert "audit" in result.output
        assert "3" in result.output
        assert "128" in result.output

    def test_stats_on_missing_project_degrades_gracefully_and_exits_zero(self) -> None:
        with patch("writ.session.log_read.stream_stats", return_value={}):
            result = runner.invoke(app, ["logs", "stats", "--project", "never-existed"])

        assert result.exit_code == 0, result.output


# ===========================================================================
# list_projects(): directory scan only, all-projects vs single-project,
# nested scopes, and the `archive`-named-project safety invariant
# ===========================================================================


def test_list_projects_lists_all_projects_when_project_arg_is_none(tmp_path):
    _write_live_lines("proj-one", "audit", ['{"event": "a"}'])
    _write_live_lines("proj-two", "friction", ['{"event": "b"}'])

    projects = list_projects()

    names = {p["project"] for p in projects}
    assert names == {"proj-one", "proj-two"}


def test_list_projects_filters_to_a_single_named_project(tmp_path):
    _write_live_lines("proj-one", "audit", ['{"event": "a"}'])
    _write_live_lines("proj-two", "friction", ['{"event": "b"}'])

    projects = list_projects(project="proj-one")

    assert len(projects) == 1
    assert projects[0]["project"] == "proj-one"


def test_list_projects_reports_stream_names_with_byte_sizes(tmp_path):
    project = "proj-sizes"
    audit_path = _write_live_lines(project, "audit", ['{"event": "a"}', '{"event": "b"}'])
    expected_bytes = audit_path.stat().st_size

    projects = list_projects(project=project)

    entry = projects[0]
    stream_entry = next(s for s in entry["streams"] if s["stream"] == "audit")
    assert stream_entry["bytes"] == expected_bytes


def test_list_projects_reports_archive_generation_count(tmp_path):
    project = "proj-archive-count"
    _write_live_lines(project, "audit", ['{"event": "a"}'])
    _write_gz_archive(project, "audit", date(2025, 6, 1), '{"event": "old"}\n')
    _write_gz_archive(project, "audit", date(2025, 5, 1), '{"event": "older"}\n')

    projects = list_projects(project=project)

    assert projects[0]["archive_count"] == 2


def test_list_projects_empty_or_missing_root_returns_empty_list(tmp_path, monkeypatch):
    monkeypatch.setenv("WRIT_LOG_ROOT", str(tmp_path / "does-not-exist"))

    assert list_projects() == []


def test_list_projects_does_not_mistake_project_named_archive_for_archive_folder(
    tmp_path, monkeypatch,
):
    monkeypatch.setenv("WRIT_LOG_PROJECT", "archive")
    _write_live_lines("archive", "audit", ['{"event": "a"}'])

    projects = list_projects()

    names = {p["project"] for p in projects}
    assert "archive" in names, (
        "a project literally named 'archive' must be enumerated as a project, not "
        "mistaken for the archive/ subfolder shape (the P2 data-loss regression class)"
    )


def test_list_projects_handles_nested_scope_like_github_org_repo(tmp_path):
    project = "github.com/org/repo"
    _write_live_lines(project, "audit", ['{"event": "a"}'])

    projects = list_projects()

    names = {p["project"] for p in projects}
    assert project in names


def test_list_projects_signature_project_param_defaults_to_none():
    sig = inspect.signature(list_projects)
    assert sig.parameters["project"].default is None


# ===========================================================================
# _discover_projects(): internal directory-scan helper (no line parsing)
# ===========================================================================


def test_discover_projects_finds_a_project_via_directory_scan_only(tmp_path):
    _write_live_lines("proj-scan", "audit", ['{"event": "a"}'])

    discovered = _discover_projects(log_root())

    assert "proj-scan" in discovered


def test_discover_projects_returns_empty_for_missing_root(tmp_path):
    missing_root = tmp_path / "does-not-exist"

    assert list(_discover_projects(missing_root)) == []


# ===========================================================================
# `writ logs list` CLI: all-vs-single project, row output, journald hint
# ===========================================================================


class TestCliLogsList:
    def test_list_all_projects_when_project_option_omitted(self) -> None:
        fake_projects = [{"project": "p1", "streams": [], "archive_count": 0}]
        with patch(
            "writ.session.log_read.list_projects", return_value=fake_projects
        ) as mock_list:
            result = runner.invoke(app, ["logs", "list"])

        assert result.exit_code == 0, result.output
        mock_list.assert_called_once_with(project=None)

    def test_list_explicit_project_is_passed_through(self) -> None:
        with patch(
            "writ.session.log_read.list_projects", return_value=[]
        ) as mock_list:
            result = runner.invoke(app, ["logs", "list", "--project", "only-this"])

        assert result.exit_code == 0, result.output
        mock_list.assert_called_once_with(project="only-this")

    def test_list_prints_project_stream_size_and_archive_count_rows(self) -> None:
        fake_projects = [
            {
                "project": "proj-x",
                "streams": [{"stream": "audit", "bytes": 512}],
                "archive_count": 3,
            }
        ]
        with patch("writ.session.log_read.list_projects", return_value=fake_projects):
            result = runner.invoke(app, ["logs", "list"])

        assert result.exit_code == 0, result.output
        assert "proj-x" in result.output
        assert "audit" in result.output
        assert "512" in result.output
        assert "3" in result.output

    def test_list_surfaces_journald_hint(self) -> None:
        with patch("writ.session.log_read.list_projects", return_value=[]):
            result = runner.invoke(app, ["logs", "list"])

        assert result.exit_code == 0, result.output
        assert "journalctl --user -u writ-server" in result.output

    def test_list_empty_root_exits_zero(self) -> None:
        with patch("writ.session.log_read.list_projects", return_value=[]):
            result = runner.invoke(app, ["logs", "list"])

        assert result.exit_code == 0, result.output


# ===========================================================================
# `writ logs --help`: journald hint surfaced without re-plumbing daemon stdout
# ===========================================================================


class TestLogsHelpJournaldHint:
    def test_logs_help_mentions_journald_hint(self) -> None:
        result = runner.invoke(app, ["logs", "--help"])
        assert result.exit_code == 0, result.output
        from tests._ansi import plain

        assert "journalctl --user -u writ-server" in plain(result.output)
