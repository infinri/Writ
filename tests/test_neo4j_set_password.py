"""Program item 3: `writ neo4j set-password` (writ/neo4j_password.py) and its three CLI commands.

Two halves, kept apart on purpose (ENF-SYS-005):

* Unit tests with recording fakes. They prove CALL ORDER (nothing touches the database before the
  checks that need no database, the ALTER is never attempted after a failed probe, a failed write
  triggers the reverse ALTER) and the one path a real database cannot be made to produce on demand
  (the reverse ALTER failing too). They use tmp_path for every config file and never read or write
  the real install's config.
* Integration tests against the real ISOLATED Neo4j (scripts/test-graph.sh up). They prove what a
  mock cannot: that ALTER CURRENT USER really changes the password, that a rerun rotates, that a
  wrong configured password changes nothing, and that a rollback really restores the old one. They
  change the isolated graph's password, so a fixture finalizer restores the original whatever the
  test did.

No test builds a credential-shaped literal: every password is generated at runtime.

New names are imported inside each test so a missing implementation fails that test with an
ImportError naming the symbol, rather than failing collection of the whole module.
"""
from __future__ import annotations

import asyncio
import difflib
import os
import re
import stat
import subprocess
import sys
import threading
import uuid
from types import SimpleNamespace

import pytest
from typer.testing import CliRunner

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover
    import tomli as tomllib  # type: ignore[no-redef]

from writ.config import DEFAULT_NEO4J_PASSWORD

URI = "bolt://localhost:7688"
OTHER_URI = "bolt://localhost:7999"
USER = "neo4j"
ALNUM = re.compile(r"[A-Za-z0-9]{32}")


def _pw() -> str:
    return uuid.uuid4().hex


def _config_text(password: str | None, *, uri: str = URI) -> str:
    lines = ["# operator notes", "[neo4j]", f'uri = "{uri}"', f'user = "{USER}"']
    if password is not None:
        lines.append("password = " + '"' + password + '"')
    lines += ["", "[logs]", "keep = 3", ""]
    return "\n".join(lines)


@pytest.fixture()
def workdir(tmp_path, monkeypatch):
    """A tmp writ.toml naming URI, with the environment cleared of anything that overrides it."""
    monkeypatch.delenv("WRIT_NEO4J_PASSWORD", raising=False)
    monkeypatch.delenv("WRIT_ALLOW_DEV_PASSWORD", raising=False)
    monkeypatch.setenv("WRIT_NEO4J_URI", URI)
    monkeypatch.setenv("WRIT_NEO4J_USER", USER)
    old = _pw()
    path = tmp_path / "writ.toml"
    path.write_text(_config_text(old), encoding="utf-8")
    return SimpleNamespace(path=path, old=old, dir=tmp_path)


class Recorder:
    """Recording fakes for probe_fn, alter_fn, stage_fn and commit_fn, in call order.

    stage and commit wrap the real stage_config and commit_config, so the file on disk is real.
    The alter fake snapshots every staged temp file present when it runs: the staging guarantee
    is that the new password is on disk, 0600, BEFORE the database changes.
    """

    def __init__(
        self, *, probe_error=None, alter_errors=None, stage_error=None, commit_error=None,
        after_commit_error=None,
    ):
        self.calls: list[tuple] = []
        self.probe_error = probe_error
        self.alter_errors = list(alter_errors or [])  # one entry per alter call; None = succeed
        self.stage_error = stage_error
        self.commit_error = commit_error
        self.after_commit_error = after_commit_error
        self.staged_at_alter: list[list[tuple[str, int, str]]] = []
        self.dir = None

    async def probe(self, uri, user, secret, wait_seconds):
        self.calls.append(("probe", uri, user, secret, wait_seconds))
        if self.probe_error is not None:
            raise self.probe_error(secret)

    async def alter(self, uri, user, old, new):
        self.calls.append(("alter", uri, user, old, new))
        self.staged_at_alter.append([
            (p.name, stat.S_IMODE(p.stat().st_mode), p.read_text()) for p in _staged(self.dir)
        ])
        index = len(self.alters) - 1
        factory = self.alter_errors[index] if index < len(self.alter_errors) else None
        if factory is not None:
            raise factory(old, new)

    def stage(self, path, text):
        self.calls.append(("stage", path, text))
        if self.stage_error is not None:
            raise self.stage_error
        from writ.neo4j_password import stage_config

        return stage_config(path, text)

    def commit(self, staged, path):
        self.calls.append(("commit", staged, path))
        if self.commit_error is not None:
            raise self.commit_error
        from writ.neo4j_password import commit_config

        commit_config(staged, path)
        if self.after_commit_error is not None:
            raise self.after_commit_error

    @property
    def kinds(self) -> list[str]:
        return [call[0] for call in self.calls]

    @property
    def alters(self) -> list[tuple]:
        return [call for call in self.calls if call[0] == "alter"]

    @property
    def new(self) -> str:
        return self.alters[0][4]

    def run(self, path, **kwargs):
        from writ.neo4j_password import set_password

        self.dir = os.path.dirname(os.path.realpath(path))
        return asyncio.run(
            set_password(
                str(path),
                probe_fn=self.probe,
                alter_fn=self.alter,
                stage_fn=self.stage,
                commit_fn=self.commit,
                **kwargs,
            )
        )


def _staged(directory) -> list:
    """The atomic-write temp files in `directory` (.gitignore lists the same pattern). The lock
    file shares the pattern and is excluded: it stays by design (see LOCK_FILE_NAME)."""
    from pathlib import Path

    from writ.neo4j_password import LOCK_FILE_NAME

    return sorted(p for p in Path(directory).glob(".writ.toml.*") if p.name != LOCK_FILE_NAME)


# ---------------------------------------------------------------------------
# generate_password
# ---------------------------------------------------------------------------


class TestGeneratePassword:
    def test_it_is_thirty_two_letters_and_digits(self):
        from writ.neo4j_password import GENERATED_ALPHABET, generate_password

        value = generate_password()
        assert len(value) == 32
        assert set(value) <= set(GENERATED_ALPHABET)
        assert ALNUM.fullmatch(value)

    def test_the_length_constant_is_thirty_two(self):
        from writ.neo4j_password import GENERATED_LENGTH

        assert GENERATED_LENGTH == 32

    def test_fifty_calls_are_all_distinct(self):
        from writ.neo4j_password import generate_password

        assert len({generate_password() for _ in range(50)}) == 50

    def test_it_is_never_the_development_default(self):
        from writ.neo4j_password import generate_password

        assert all(generate_password() != DEFAULT_NEO4J_PASSWORD for _ in range(50))


# ---------------------------------------------------------------------------
# render_config
# ---------------------------------------------------------------------------


def _changed_lines(before: str, after: str) -> list[str]:
    diff = difflib.unified_diff(before.splitlines(), after.splitlines(), lineterm="", n=0)
    return [
        line for line in diff
        if line[:1] in "+-" and not line.startswith(("+++", "---"))
    ]


class TestRenderConfig:
    def test_no_existing_file_renders_a_parseable_neo4j_section(self):
        from writ.neo4j_password import render_config

        new = _pw()
        text = render_config(None, new)
        assert tomllib.loads(text) == {"neo4j": {"password": new}}

    def test_an_existing_password_line_is_the_only_line_that_changes(self):
        from writ.neo4j_password import render_config

        old, new = _pw(), _pw()
        existing = (
            "# keep this comment\n"
            "[logs]\nkeep = 3\n\n"
            "[egress]\nallow_hosts = [\"example.org\"]\n\n"
            "[neo4j]\n"
            f'uri = "{URI}"\n'
            "password = " + '"' + old + '"' + "  # trailing note\n"
            'user = "neo4j"\n'
        )
        rendered = render_config(existing, new)
        assert _changed_lines(existing, rendered) == [
            "-password = " + '"' + old + '"' + "  # trailing note",
            "+password = " + '"' + new + '"',
        ]
        parsed = tomllib.loads(rendered)
        assert parsed["neo4j"] == {"uri": URI, "password": new, "user": "neo4j"}
        assert parsed["logs"] == {"keep": 3}
        assert parsed["egress"] == {"allow_hosts": ["example.org"]}

    def test_a_neo4j_section_without_a_password_gets_it_after_the_header(self):
        from writ.neo4j_password import render_config

        new = _pw()
        existing = f'[neo4j]\nuri = "{URI}"\n\n[logs]\nkeep = 3\n'
        rendered = render_config(existing, new)
        lines = rendered.splitlines()
        assert lines[0] == "[neo4j]"
        assert lines[1] == "password = " + '"' + new + '"'
        assert tomllib.loads(rendered)["neo4j"] == {"uri": URI, "password": new}
        assert tomllib.loads(rendered)["logs"] == {"keep": 3}

    def test_a_file_without_a_neo4j_section_gets_one_appended(self):
        from writ.neo4j_password import render_config

        new = _pw()
        existing = "# comment\n[logs]\nkeep = 3\n"
        rendered = render_config(existing, new)
        assert rendered.startswith(existing), "the original bytes must be a prefix"
        parsed = tomllib.loads(rendered)
        assert parsed["neo4j"] == {"password": new}
        assert parsed["logs"] == {"keep": 3}

    def test_a_file_that_does_not_end_in_a_newline_still_parses(self):
        from writ.neo4j_password import render_config

        new = _pw()
        existing = "[logs]\nkeep = 3"
        rendered = render_config(existing, new)
        assert tomllib.loads(rendered) == {"logs": {"keep": 3}, "neo4j": {"password": new}}

    def test_a_neo4j_header_at_end_of_file_with_no_newline_still_parses(self):
        from writ.neo4j_password import render_config

        new = _pw()
        rendered = render_config("[logs]\nkeep = 3\n\n[neo4j]", new)
        assert tomllib.loads(rendered) == {"logs": {"keep": 3}, "neo4j": {"password": new}}

    def test_malformed_toml_is_refused(self):
        from writ.neo4j_password import PasswordChangeError, render_config

        with pytest.raises(PasswordChangeError):
            render_config("[neo4j\nuri = ", _pw())

    def test_an_inline_neo4j_table_is_refused(self):
        from writ.neo4j_password import PasswordChangeError, render_config

        with pytest.raises(PasswordChangeError):
            render_config('neo4j = {uri = "x"}\n', _pw())

    def test_a_top_level_dotted_neo4j_key_is_refused(self):
        from writ.neo4j_password import PasswordChangeError, render_config

        with pytest.raises(PasswordChangeError):
            render_config('neo4j.uri = "x"\n', _pw())


# ---------------------------------------------------------------------------
# stage_config and commit_config: the two halves of the atomic write
# ---------------------------------------------------------------------------


def _write(path, text) -> None:
    from writ.neo4j_password import commit_config, stage_config

    commit_config(stage_config(str(path), text), str(path))


class TestWriteConfig:
    def test_a_world_readable_file_becomes_0600_with_the_exact_text(self, tmp_path):
        path = tmp_path / "writ.toml"
        path.write_text("old\n")
        path.chmod(0o644)
        _write(path, "fresh = 1\n")
        assert path.read_text() == "fresh = 1\n"
        assert stat.S_IMODE(path.stat().st_mode) == 0o600

    def test_a_new_file_is_created_0600(self, tmp_path):
        path = tmp_path / "writ.toml"
        _write(path, "x = 1\n")
        assert stat.S_IMODE(path.stat().st_mode) == 0o600

    def test_no_temp_file_is_left_behind(self, tmp_path):
        path = tmp_path / "writ.toml"
        _write(path, "x = 1\n")
        assert sorted(p.name for p in tmp_path.iterdir()) == ["writ.toml"]
        assert _staged(tmp_path) == []

    def test_a_missing_directory_raises_oserror_and_leaves_nothing(self, tmp_path):
        from writ.neo4j_password import stage_config

        missing = tmp_path / "absent"
        with pytest.raises(OSError):
            stage_config(str(missing / "writ.toml"), "x = 1\n")
        assert not missing.exists()
        assert list(tmp_path.iterdir()) == []

    def test_staging_writes_a_0600_sibling_and_leaves_the_target_alone(self, tmp_path):
        from writ.neo4j_password import stage_config

        path = tmp_path / "writ.toml"
        path.write_text("old\n")
        staged = stage_config(str(path), "fresh = 1\n")
        assert os.path.dirname(staged) == str(tmp_path)
        assert _staged(tmp_path) == [tmp_path / os.path.basename(staged)]
        assert stat.S_IMODE(os.stat(staged).st_mode) == 0o600
        assert open(staged).read() == "fresh = 1\n"
        assert path.read_text() == "old\n"

    def test_staging_fsyncs_the_file_before_returning(self, tmp_path, monkeypatch):
        from writ.neo4j_password import stage_config

        synced = []
        real_fsync = os.fsync
        monkeypatch.setattr(
            os, "fsync", lambda fd: (synced.append(stat.S_ISREG(os.fstat(fd).st_mode)), real_fsync(fd))
        )
        stage_config(str(tmp_path / "writ.toml"), "x = 1\n")
        assert synced == [True]

    def test_commit_fsyncs_the_directory_after_the_rename(self, tmp_path, monkeypatch):
        from writ.neo4j_password import commit_config, stage_config

        path = tmp_path / "writ.toml"
        staged = stage_config(str(path), "x = 1\n")
        synced = []
        real_fsync = os.fsync

        def _record(fd):
            synced.append((stat.S_ISDIR(os.fstat(fd).st_mode), path.exists()))
            real_fsync(fd)

        monkeypatch.setattr(os, "fsync", _record)
        commit_config(staged, str(path))
        assert synced == [(True, True)]
        assert path.read_text() == "x = 1\n"
        assert _staged(tmp_path) == []

    def test_the_config_mode_constant_is_0600(self):
        from writ.neo4j_password import CONFIG_FILE_MODE

        assert CONFIG_FILE_MODE == 0o600


# ---------------------------------------------------------------------------
# set_password ordering, with recording fakes
# ---------------------------------------------------------------------------


class TestRefusalsBeforeTheDatabase:
    def test_a_uri_override_naming_another_instance_is_refused_before_probing(
        self, workdir, monkeypatch
    ):
        from writ.neo4j_password import PasswordChangeError

        monkeypatch.setenv("WRIT_NEO4J_URI", OTHER_URI)
        before = workdir.path.read_bytes()
        rec = Recorder()
        with pytest.raises(PasswordChangeError) as caught:
            rec.run(workdir.path)
        assert rec.calls == []
        assert workdir.path.read_bytes() == before
        assert "WRIT_NEO4J_URI" in str(caught.value)

    def test_a_malformed_file_is_refused_before_probing(self, workdir):
        from writ.neo4j_password import PasswordChangeError

        workdir.path.write_text("[neo4j\nuri = ", encoding="utf-8")
        before = workdir.path.read_bytes()
        rec = Recorder()
        with pytest.raises(PasswordChangeError):
            rec.run(workdir.path)
        assert rec.calls == []
        assert workdir.path.read_bytes() == before

    def test_an_inline_table_file_is_refused_before_probing(self, workdir):
        from writ.neo4j_password import PasswordChangeError

        workdir.path.write_text(f'neo4j = {{uri = "{URI}"}}\n', encoding="utf-8")
        before = workdir.path.read_bytes()
        rec = Recorder()
        with pytest.raises(PasswordChangeError):
            rec.run(workdir.path)
        assert rec.calls == []
        assert workdir.path.read_bytes() == before

    def test_an_unwritable_directory_is_refused_before_probing(self, workdir, monkeypatch):
        from writ.neo4j_password import PasswordChangeError

        monkeypatch.setattr("os.access", lambda *args, **kwargs: False)
        before = workdir.path.read_bytes()
        rec = Recorder()
        with pytest.raises(PasswordChangeError) as caught:
            rec.run(workdir.path)
        assert rec.calls == []
        assert workdir.path.read_bytes() == before
        assert "not writable" in str(caught.value)


class TestPartialFailureLeavesFileUntouched:
    def test_a_probe_failure_never_attempts_the_alter(self, workdir):
        from writ.neo4j_password import PasswordChangeError

        before = workdir.path.read_bytes()
        rec = Recorder(probe_error=lambda secret: RuntimeError(f"cannot log in as {secret}"))
        with pytest.raises(PasswordChangeError) as caught:
            rec.run(workdir.path)
        assert rec.kinds == ["probe"]
        assert _staged(workdir.dir) == []
        assert workdir.path.read_bytes() == before
        assert workdir.old not in str(caught.value)

    def test_the_probe_receives_the_configured_password_and_the_wait(self, workdir):
        rec = Recorder()
        rec.run(workdir.path, wait_seconds=7.0)
        probe = rec.calls[0]
        assert probe[0] == "probe"
        assert probe[3] == workdir.old
        assert probe[4] == 7.0

    def test_an_alter_failure_leaves_the_file_unchanged_and_leaks_no_password(self, workdir):
        from writ.neo4j_password import PasswordChangeError

        before = workdir.path.read_bytes()
        rec = Recorder(alter_errors=[lambda old, new: RuntimeError(f"rejected {old} -> {new}")])
        with pytest.raises(PasswordChangeError) as caught:
            rec.run(workdir.path)
        assert rec.kinds == ["probe", "stage", "alter"]
        assert _staged(workdir.dir) == []
        assert workdir.path.read_bytes() == before
        new = rec.new
        message = str(caught.value)
        assert workdir.old not in message
        assert new not in message

    def test_the_alter_never_interpolates_into_text_so_it_gets_old_then_new(self, workdir):
        rec = Recorder()
        rec.run(workdir.path)
        alter = rec.alters[0]
        assert alter[3] == workdir.old
        assert ALNUM.fullmatch(alter[4])
        assert alter[4] != workdir.old


class TestSuccessfulChange:
    def test_the_order_is_probe_then_alter_then_write(self, workdir):
        rec = Recorder()
        result = rec.run(workdir.path)
        assert rec.kinds == ["probe", "stage", "alter", "commit"]
        assert result.uri == URI
        assert result.config_path == os.path.realpath(workdir.path)

    def test_the_new_password_is_in_the_file_and_the_file_is_0600(self, workdir):
        rec = Recorder()
        rec.run(workdir.path)
        new = rec.new
        stored = tomllib.loads(workdir.path.read_text())
        assert stored["neo4j"]["password"] == new
        assert stored["neo4j"]["uri"] == URI
        assert stored["logs"] == {"keep": 3}
        assert stat.S_IMODE(workdir.path.stat().st_mode) == 0o600

    def test_the_result_holds_no_password(self, workdir):
        rec = Recorder()
        result = rec.run(workdir.path)
        new = rec.new
        assert new not in repr(result)
        assert workdir.old not in repr(result)

    def test_env_override_is_reported_when_the_env_variable_is_set(self, workdir, monkeypatch):
        monkeypatch.setenv("WRIT_NEO4J_PASSWORD", workdir.old)
        result = Recorder().run(workdir.path)
        assert result.env_override is True

    def test_env_override_is_false_when_the_env_variable_is_unset(self, workdir):
        assert Recorder().run(workdir.path).env_override is False

    def test_the_env_variable_is_the_old_password_when_it_is_set(self, workdir, monkeypatch):
        """The recovery path: the env value is what authenticates, not the file's."""
        recovery = _pw()
        monkeypatch.setenv("WRIT_NEO4J_PASSWORD", recovery)
        rec = Recorder()
        rec.run(workdir.path)
        assert rec.calls[0][3] == recovery
        assert rec.alters[0][3] == recovery


class TestWriteFailureAfterAlter:
    def test_the_database_is_changed_back_and_the_report_shows_no_password(self, workdir):
        from writ.neo4j_password import PasswordStoreFailed

        before = workdir.path.read_bytes()
        rec = Recorder(commit_error=OSError("no space left on device"))
        with pytest.raises(PasswordStoreFailed) as caught:
            rec.run(workdir.path)
        exc = caught.value
        assert rec.kinds == ["probe", "stage", "alter", "commit", "alter"]
        new = rec.new
        assert rec.alters[1][1:] == (URI, USER, new, workdir.old)
        assert _staged(workdir.dir) == []
        assert exc.rolled_back is True
        assert exc.new_password is None
        assert workdir.path.read_bytes() == before
        report = exc.report()
        assert new not in report
        assert workdir.old not in report
        assert "nothing changed" in report
        assert new not in str(exc)

    def test_a_failed_reverse_alter_reports_the_new_password_with_both_recovery_routes(
        self, workdir
    ):
        from writ.neo4j_password import PasswordStoreFailed

        rec = Recorder(
            commit_error=OSError("no space left on device"),
            alter_errors=[None, lambda old, new: RuntimeError("connection lost")],
        )
        with pytest.raises(PasswordStoreFailed) as caught:
            rec.run(workdir.path)
        exc = caught.value
        new = rec.new
        assert exc.rolled_back is False
        assert exc.new_password == new
        report = exc.report()
        assert new in report
        assert "chmod 600" in report
        assert "WRIT_NEO4J_PASSWORD=" in report
        assert workdir.old not in report

    def test_str_of_the_error_never_contains_the_new_password_even_when_unrolled(
        self, workdir
    ):
        from writ.neo4j_password import PasswordStoreFailed

        rec = Recorder(
            commit_error=OSError("disk full"),
            alter_errors=[None, lambda old, new: RuntimeError("connection lost")],
        )
        with pytest.raises(PasswordStoreFailed) as caught:
            rec.run(workdir.path)
        new = rec.new
        assert new not in str(caught.value)
        assert new not in repr(caught.value)


class TestStagedBeforeTheAlter:
    """The new password is on disk (0600, fsynced) before the database changes, so a hard kill
    between the ALTER and the rename leaves a recoverable copy; any failure after the ALTER
    changes the database back, and no failure leaves the staged copy behind."""

    def test_the_staged_file_exists_0600_with_the_new_password_when_the_alter_runs(self, workdir):
        rec = Recorder()
        rec.run(workdir.path)
        [snapshot] = rec.staged_at_alter
        assert len(snapshot) == 1
        _name, mode, text = snapshot[0]
        assert mode == 0o600
        assert tomllib.loads(text)["neo4j"]["password"] == rec.new
        assert _staged(workdir.dir) == []

    def test_a_staging_failure_never_reaches_the_database(self, workdir):
        from writ.neo4j_password import PasswordChangeError

        before = workdir.path.read_bytes()
        rec = Recorder(stage_error=OSError("no space left on device"))
        with pytest.raises(PasswordChangeError) as caught:
            rec.run(workdir.path)
        assert rec.kinds == ["probe", "stage"]
        assert workdir.path.read_bytes() == before
        assert "no space left on device" in str(caught.value)
        assert _staged(workdir.dir) == []

    @pytest.mark.parametrize(
        "interruption", [KeyboardInterrupt(), asyncio.CancelledError(), SystemExit(143)],
        ids=["sigint", "cancelled", "sigterm-handler-exit"],
    )
    def test_an_interruption_before_the_rename_is_rolled_back_and_re_raised(
        self, workdir, interruption
    ):
        before = workdir.path.read_bytes()
        rec = Recorder(commit_error=interruption)
        with pytest.raises(type(interruption)):
            rec.run(workdir.path)
        assert rec.kinds == ["probe", "stage", "alter", "commit", "alter"]
        assert rec.alters[1][1:] == (URI, USER, rec.new, workdir.old)
        assert workdir.path.read_bytes() == before
        assert _staged(workdir.dir) == []

    def test_a_non_oserror_from_the_rename_is_rolled_back_and_re_raised(self, workdir):
        before = workdir.path.read_bytes()
        rec = Recorder(commit_error=ValueError("unexpected"))
        with pytest.raises(ValueError, match="unexpected"):
            rec.run(workdir.path)
        assert rec.alters[1][1:] == (URI, USER, rec.new, workdir.old)
        assert workdir.path.read_bytes() == before
        assert _staged(workdir.dir) == []

    def test_an_interruption_whose_rollback_fails_reports_the_new_password(self, workdir):
        from writ.neo4j_password import PasswordStoreFailed

        rec = Recorder(
            commit_error=KeyboardInterrupt(),
            alter_errors=[None, lambda old, new: RuntimeError("connection lost")],
        )
        with pytest.raises(PasswordStoreFailed) as caught:
            rec.run(workdir.path)
        exc = caught.value
        assert exc.rolled_back is False
        assert exc.new_password == rec.new
        assert isinstance(exc.cause, KeyboardInterrupt)
        assert rec.new in exc.report()
        assert "KeyboardInterrupt" in exc.report()
        assert _staged(workdir.dir) == []

    def test_an_interruption_after_the_rename_landed_is_not_rolled_back(self, workdir):
        """File and database already agree on the new password: changing the database back
        would be what makes them disagree."""
        rec = Recorder(after_commit_error=KeyboardInterrupt())
        with pytest.raises(KeyboardInterrupt):
            rec.run(workdir.path)
        assert rec.kinds == ["probe", "stage", "alter", "commit"]
        assert tomllib.loads(workdir.path.read_text())["neo4j"]["password"] == rec.new
        assert _staged(workdir.dir) == []


def _hold_lock(directory):
    """Take the set-password lock from outside, as a concurrent run would. Returns the fd."""
    import fcntl

    from writ.neo4j_password import LOCK_FILE_NAME

    fd = os.open(os.path.join(directory, LOCK_FILE_NAME), os.O_RDWR | os.O_CREAT, 0o600)
    fcntl.flock(fd, fcntl.LOCK_EX)
    return fd


class SlowRecorder(Recorder):
    """A Recorder whose ALTER takes long enough for a concurrent run to arrive mid-change."""

    async def alter(self, uri, user, old, new):
        await asyncio.sleep(0.3)
        await super().alter(uri, user, old, new)


def _run_concurrently(path, recorders, **kwargs):
    results, errors = [None] * len(recorders), []

    def _one(index, rec):
        try:
            results[index] = rec.run(path, **kwargs)
        except BaseException as exc:  # noqa: BLE001 - surfaced by the assertion below
            errors.append(exc)

    threads = [threading.Thread(target=_one, args=(i, r)) for i, r in enumerate(recorders)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)
    assert errors == []
    return results


class TestConcurrentRuns:
    """One lock file beside writ.toml serialises every set-password run on it (flock conflicts
    between separate open() calls even within one process, so threads stand in for sessions)."""

    def test_a_second_concurrent_run_rotates_from_the_first_runs_new_password(self, workdir):
        first, second = SlowRecorder(), SlowRecorder()
        _run_concurrently(workdir.path, [first, second])
        olds = sorted([first.alters[0][3], second.alters[0][3]])
        news = {first.new, second.new}
        assert workdir.old in olds
        assert len(news & set(olds)) == 1
        final = tomllib.loads(workdir.path.read_text())["neo4j"]["password"]
        assert final in news and final not in olds
        assert _staged(workdir.dir) == []

    def test_two_concurrent_if_default_runs_from_the_default_alter_exactly_once(self, workdir):
        workdir.path.write_text(_config_text(None), encoding="utf-8")
        first, second = SlowRecorder(), SlowRecorder()
        results = _run_concurrently(workdir.path, [first, second], if_default=True)
        assert len(first.alters) + len(second.alters) == 1
        assert sorted(r.changed for r in results) == [False, True]
        winner = first if first.alters else second
        assert winner.alters[0][3] == DEFAULT_NEO4J_PASSWORD
        assert tomllib.loads(workdir.path.read_text())["neo4j"]["password"] == winner.new

    def test_if_default_on_a_private_password_changes_nothing(self, workdir):
        before = workdir.path.read_bytes()
        rec = Recorder()
        result = rec.run(workdir.path, if_default=True)
        assert result.changed is False
        assert rec.calls == []
        assert workdir.path.read_bytes() == before

    def test_if_default_on_the_default_rotates(self, workdir):
        workdir.path.write_text(_config_text(None), encoding="utf-8")
        rec = Recorder()
        assert rec.run(workdir.path, if_default=True).changed is True
        assert rec.kinds == ["probe", "stage", "alter", "commit"]

    def test_a_held_lock_times_out_and_changes_nothing(self, workdir):
        from writ.neo4j_password import PasswordChangeError

        before = workdir.path.read_bytes()
        fd = _hold_lock(workdir.dir)
        try:
            rec = Recorder()
            with pytest.raises(PasswordChangeError) as caught:
                rec.run(workdir.path, lock_timeout=0.3)
        finally:
            os.close(fd)
        assert rec.calls == []
        assert workdir.path.read_bytes() == before
        assert "another" in str(caught.value)
        assert "Nothing was changed" in str(caught.value)

    def test_the_lock_file_is_0600(self, workdir):
        from writ.neo4j_password import LOCK_FILE_NAME

        Recorder().run(workdir.path)
        assert stat.S_IMODE((workdir.dir / LOCK_FILE_NAME).stat().st_mode) == 0o600


# ---------------------------------------------------------------------------
# next_steps_text
# ---------------------------------------------------------------------------


class TestNextStepsText:
    def test_it_names_the_three_steps_and_the_install_dir(self):
        from writ.neo4j_password import PasswordChange, next_steps_text

        change = PasswordChange(config_path="/x/writ.toml", uri=URI, env_override=False)
        text = next_steps_text(change, "/opt/writ")
        assert "writ neo4j password" in text
        assert "docker compose -f /opt/writ/docker-compose.yml up -d neo4j" in text
        assert "systemctl --user restart writ-server" in text
        assert "WRIT_NEO4J_PASSWORD is set in this environment" not in text

    def test_it_recommends_the_inline_variable_never_a_persistent_export(self):
        """An export outlives the command in the shell, where it overrides writ.toml after the
        next rotation; the inline form scopes the value to the one compose call."""
        from writ.neo4j_password import PasswordChange, next_steps_text

        for override in (False, True):
            change = PasswordChange(config_path="/x/writ.toml", uri=URI, env_override=override)
            text = next_steps_text(change, "/opt/writ")
            assert (
                'WRIT_NEO4J_PASSWORD="$(writ neo4j password)" docker compose -f '
                "/opt/writ/docker-compose.yml up -d neo4j"
            ) in text
            assert "export" not in text.lower()

    def test_it_adds_the_override_note_when_the_env_variable_is_set(self):
        from writ.neo4j_password import PasswordChange, next_steps_text

        change = PasswordChange(config_path="/x/writ.toml", uri=URI, env_override=True)
        assert "WRIT_NEO4J_PASSWORD is set in this environment" in next_steps_text(change, "/opt/w")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _fake_set_password(path_holder: dict, written: str, *, error=None, changed=True):
    """An async stand-in for writ.neo4j_password.set_password that stores `written`."""

    async def _fake(config_path=None, *, wait_seconds=0.0, **kwargs):
        from writ.neo4j_password import PasswordChange

        path_holder["wait"] = wait_seconds
        path_holder["kwargs"] = kwargs
        if error is not None:
            raise error
        if changed:
            with open(config_path, "w", encoding="utf-8") as handle:
                handle.write("[neo4j]\npassword = " + '"' + written + '"' + "\n")
        return PasswordChange(
            config_path=config_path,
            uri=URI,
            env_override=bool(os.environ.get("WRIT_NEO4J_PASSWORD")),
            changed=changed,
        )

    return _fake


class TestSetPasswordCommand:
    def test_success_prints_next_steps_and_never_the_password(self, workdir, monkeypatch):
        from writ.cli import app

        written, seen = _pw(), {}
        monkeypatch.setattr(
            "writ.neo4j_password.set_password", _fake_set_password(seen, written)
        )
        result = CliRunner().invoke(app, ["neo4j", "set-password", "--config", str(workdir.path)])
        assert result.exit_code == 0, result.output
        assert written not in result.output
        assert "writ neo4j password" in result.output
        assert "docker compose" in result.output
        assert "systemctl --user restart writ-server" in result.output
        assert "overrides writ.toml" not in result.output

    def test_the_wait_option_reaches_set_password(self, workdir, monkeypatch):
        from writ.cli import app

        seen = {}
        monkeypatch.setattr(
            "writ.neo4j_password.set_password", _fake_set_password(seen, _pw())
        )
        result = CliRunner().invoke(
            app, ["neo4j", "set-password", "--config", str(workdir.path), "--wait", "90"]
        )
        assert result.exit_code == 0, result.output
        assert seen["wait"] == 90.0

    def test_an_env_override_adds_the_note(self, workdir, monkeypatch):
        from writ.cli import app

        monkeypatch.setenv("WRIT_NEO4J_PASSWORD", _pw())
        monkeypatch.setattr(
            "writ.neo4j_password.set_password", _fake_set_password({}, _pw())
        )
        result = CliRunner().invoke(app, ["neo4j", "set-password", "--config", str(workdir.path)])
        assert result.exit_code == 0, result.output
        assert "WRIT_NEO4J_PASSWORD is set in this environment and overrides writ.toml" in result.output

    def test_a_password_change_error_exits_1_with_the_message_on_stderr(
        self, workdir, monkeypatch
    ):
        from writ.cli import app
        from writ.neo4j_password import PasswordChangeError

        monkeypatch.setattr(
            "writ.neo4j_password.set_password",
            _fake_set_password({}, _pw(), error=PasswordChangeError("Nothing was changed.")),
        )
        result = CliRunner().invoke(app, ["neo4j", "set-password", "--config", str(workdir.path)])
        assert result.exit_code == 1
        assert "error: Nothing was changed." in result.stderr

    def test_an_unrolled_store_failure_exits_1_and_prints_the_recovery_report(
        self, workdir, monkeypatch
    ):
        from writ.cli import app
        from writ.neo4j_password import PasswordStoreFailed

        lost = _pw()
        failure = PasswordStoreFailed(
            config_path=str(workdir.path), uri=URI, cause=OSError("disk full"),
            rolled_back=False, new_password=lost,
        )
        monkeypatch.setattr(
            "writ.neo4j_password.set_password", _fake_set_password({}, _pw(), error=failure)
        )
        result = CliRunner().invoke(app, ["neo4j", "set-password", "--config", str(workdir.path)])
        assert result.exit_code == 1
        assert lost in result.stderr
        assert lost not in result.stdout


    def test_if_default_and_lock_timeout_reach_set_password(self, workdir, monkeypatch):
        from writ.cli import app

        seen = {}
        monkeypatch.setattr("writ.neo4j_password.set_password", _fake_set_password(seen, _pw()))
        result = CliRunner().invoke(
            app,
            ["neo4j", "set-password", "--config", str(workdir.path), "--if-default",
             "--lock-timeout", "5"],
        )
        assert result.exit_code == 0, result.output
        assert seen["kwargs"] == {"if_default": True, "lock_timeout": 5.0}

    def test_the_lock_timeout_defaults_to_sixty_seconds(self, workdir, monkeypatch):
        from writ.cli import app

        seen = {}
        monkeypatch.setattr("writ.neo4j_password.set_password", _fake_set_password(seen, _pw()))
        result = CliRunner().invoke(app, ["neo4j", "set-password", "--config", str(workdir.path)])
        assert result.exit_code == 0, result.output
        assert seen["kwargs"] == {"if_default": False, "lock_timeout": 60.0}

    def test_an_if_default_run_that_finds_a_private_password_says_so_and_exits_0(
        self, workdir, monkeypatch
    ):
        from writ.cli import app

        monkeypatch.setattr(
            "writ.neo4j_password.set_password", _fake_set_password({}, _pw(), changed=False)
        )
        result = CliRunner().invoke(
            app, ["neo4j", "set-password", "--config", str(workdir.path), "--if-default"]
        )
        assert result.exit_code == 0, result.output
        assert "already set" in result.output
        assert "Next steps" not in result.output


class TestPasswordCommand:
    def test_it_prints_the_stored_value_even_when_the_env_variable_differs(
        self, workdir, monkeypatch
    ):
        from writ.cli import app

        monkeypatch.setenv("WRIT_NEO4J_PASSWORD", _pw())
        result = CliRunner().invoke(app, ["neo4j", "password", "--config", str(workdir.path)])
        assert result.exit_code == 0, result.output
        assert result.stdout.strip() == workdir.old

    def test_it_exits_78_with_empty_stdout_when_only_the_default_is_configured(self, workdir):
        from writ.cli import app

        workdir.path.write_text(_config_text(None), encoding="utf-8")
        result = CliRunner().invoke(app, ["neo4j", "password", "--config", str(workdir.path)])
        assert result.exit_code == 78
        assert result.stdout == ""
        assert "writ neo4j set-password" in result.stderr
        assert DEFAULT_NEO4J_PASSWORD not in result.stderr

    def test_it_prints_the_default_when_the_opt_in_is_set(self, workdir, monkeypatch):
        from writ.cli import app

        workdir.path.write_text(_config_text(None), encoding="utf-8")
        monkeypatch.setenv("WRIT_ALLOW_DEV_PASSWORD", "1")
        result = CliRunner().invoke(app, ["neo4j", "password", "--config", str(workdir.path)])
        assert result.exit_code == 0, result.output
        assert result.stdout.strip() == DEFAULT_NEO4J_PASSWORD


class TestCheckCommand:
    def test_a_private_password_exits_0(self, workdir):
        from writ.cli import app

        result = CliRunner().invoke(app, ["neo4j", "check", "--config", str(workdir.path)])
        assert result.exit_code == 0, result.output

    def test_the_development_default_exits_78(self, workdir):
        from writ.cli import app

        workdir.path.write_text(_config_text(None), encoding="utf-8")
        result = CliRunner().invoke(app, ["neo4j", "check", "--config", str(workdir.path)])
        assert result.exit_code == 78
        assert "writ neo4j set-password" in result.stderr

    def test_the_opt_in_makes_the_default_exit_0(self, workdir, monkeypatch):
        from writ.cli import app

        workdir.path.write_text(_config_text(None), encoding="utf-8")
        monkeypatch.setenv("WRIT_ALLOW_DEV_PASSWORD", "1")
        result = CliRunner().invoke(app, ["neo4j", "check", "--config", str(workdir.path)])
        assert result.exit_code == 0, result.output


# ---------------------------------------------------------------------------
# The real isolated Neo4j (ENF-SYS-005)
# ---------------------------------------------------------------------------


@pytest.fixture()
def isolated_credentials(disposable_graph, monkeypatch, tmp_path):
    """The isolated instance, a tmp writ.toml naming it, and a guaranteed restore.

    Fails (never skips) if the resolved instance is production: these tests change a real
    password, and tests/_graph.targets_production (the wipe guard's (host, port) comparison) is
    the authority on what production is. The ORIGINAL password is restored on teardown whatever
    the test did, trying the password the file now holds and any the test recorded.
    """
    from tests._graph import targets_production
    from writ.config import get_neo4j_password, get_neo4j_uri, get_neo4j_user
    from writ.neo4j_password import alter_password, probe

    uri, user, original = get_neo4j_uri(), get_neo4j_user(), get_neo4j_password()
    if targets_production(uri):
        pytest.fail(f"refusing to change the password of the production instance {uri}")
    config = tmp_path / "writ.toml"
    config.write_text(
        f'[neo4j]\nuri = "{uri}"\nuser = "{user}"\n' + 'password = "' + original + '"\n'
    )
    monkeypatch.delenv("WRIT_NEO4J_PASSWORD", raising=False)
    state = SimpleNamespace(uri=uri, user=user, original=original, config=config, extra=[])
    yield state
    candidates = [tomllib.loads(config.read_text())["neo4j"]["password"], *state.extra]
    for candidate in candidates:
        if candidate == original:
            continue
        try:
            asyncio.run(alter_password(uri, user, candidate, original))
            break
        except Exception:
            continue
    try:
        asyncio.run(probe(uri, user, original))
    except Exception as exc:
        pytest.fail(
            f"the isolated instance no longer accepts its original password ({exc}). Recreate it "
            "(docker rm -f writ-test-neo4j, then scripts/test-graph.sh up); it holds only "
            "disposable data."
        )


def _stored(config) -> str:
    return tomllib.loads(config.read_text())["neo4j"]["password"]


class TestAgainstTheIsolatedNeo4j:
    def test_set_password_changes_the_database_and_saves_a_0600_file(self, isolated_credentials):
        from neo4j.exceptions import AuthError

        from writ.neo4j_password import probe, set_password

        s = isolated_credentials
        result = asyncio.run(set_password(str(s.config)))
        new = _stored(s.config)
        assert new != s.original
        assert ALNUM.fullmatch(new)
        asyncio.run(probe(s.uri, s.user, new))
        with pytest.raises(AuthError):
            asyncio.run(probe(s.uri, s.user, s.original))
        assert stat.S_IMODE(s.config.stat().st_mode) == 0o600
        assert result.config_path == os.path.realpath(s.config)

    def test_a_rerun_rotates_from_the_stored_password_to_a_different_one(
        self, isolated_credentials
    ):
        from writ.neo4j_password import probe, set_password

        s = isolated_credentials
        asyncio.run(set_password(str(s.config)))
        first = _stored(s.config)
        asyncio.run(set_password(str(s.config)))
        second = _stored(s.config)
        assert len({s.original, first, second}) == 3
        asyncio.run(probe(s.uri, s.user, second))

    def test_a_wrong_configured_password_changes_neither_database_nor_file(
        self, isolated_credentials
    ):
        from writ.neo4j_password import PasswordChangeError, probe, set_password

        s = isolated_credentials
        wrong = _pw()
        s.config.write_text(
            f'[neo4j]\nuri = "{s.uri}"\nuser = "{s.user}"\n' + 'password = "' + wrong + '"\n'
        )
        before = s.config.read_bytes()
        with pytest.raises(PasswordChangeError) as caught:
            asyncio.run(set_password(str(s.config)))
        assert s.config.read_bytes() == before
        assert wrong not in str(caught.value)
        asyncio.run(probe(s.uri, s.user, s.original))

    def test_a_failed_write_after_the_alter_is_rolled_back(self, isolated_credentials):
        from writ.neo4j_password import PasswordStoreFailed, probe, set_password

        s = isolated_credentials
        before = s.config.read_bytes()

        def _failing_commit(staged, path):
            raise OSError("simulated full disk")

        with pytest.raises(PasswordStoreFailed) as caught:
            asyncio.run(set_password(str(s.config), commit_fn=_failing_commit))
        exc = caught.value
        if not exc.rolled_back and exc.new_password:
            s.extra.append(exc.new_password)
        assert exc.rolled_back is True
        assert s.config.read_bytes() == before
        assert _staged(s.config.parent) == []
        asyncio.run(probe(s.uri, s.user, s.original))

    def test_two_concurrent_cli_runs_leave_a_working_0600_password_and_no_temp_file(
        self, isolated_credentials
    ):
        from neo4j.exceptions import AuthError

        from writ.neo4j_password import probe

        s = isolated_credentials
        argv = [sys.executable, "-c", "from writ.cli import app; app()",
                "neo4j", "set-password", "--config", str(s.config)]
        runs = [
            subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            for _ in range(2)
        ]
        outcomes = [run.communicate(timeout=120) for run in runs]
        assert [run.returncode for run in runs] == [0, 0], outcomes
        stored = _stored(s.config)
        assert stored != s.original
        asyncio.run(probe(s.uri, s.user, stored))
        with pytest.raises(AuthError):
            asyncio.run(probe(s.uri, s.user, s.original))
        assert stat.S_IMODE(s.config.stat().st_mode) == 0o600
        assert _staged(s.config.parent) == []

    def test_a_held_lock_makes_the_cli_exit_1_and_change_nothing(self, isolated_credentials):
        from writ.cli import app
        from writ.neo4j_password import probe

        s = isolated_credentials
        before = s.config.read_bytes()
        fd = _hold_lock(s.config.parent)
        try:
            result = CliRunner().invoke(
                app, ["neo4j", "set-password", "--config", str(s.config), "--lock-timeout", "0.3"]
            )
        finally:
            os.close(fd)
        assert result.exit_code == 1, result.output
        assert "another" in result.stderr
        assert s.config.read_bytes() == before
        asyncio.run(probe(s.uri, s.user, s.original))
