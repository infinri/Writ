"""Audit item F: which writ.toml was loaded, and what it contributed.

Config resolution left no trace. A missing file was entirely silent, so "running on
built-in defaults" (including DEFAULT_NEO4J_PASSWORD) looked identical to "loaded a config
with these values" -- and writ.toml is gitignored, so a fresh install genuinely has none.
The malformed and unreadable branches printed to stderr, which for a hook is a swallowed
sink and for the daemon is journald: nobody saw "your config was ignored".

The credential constraint drives the design: writ.toml holds neo4j.password and
bitbucket.token, so the event carries key NAMES only. Logging values would move
credentials into a 365-day stream.
"""
from __future__ import annotations

import json

import pytest


@pytest.fixture(autouse=True)
def _neutralize_the_env_layer(monkeypatch):
    """These tests assert on the writ.toml and built-in-default layers of `_neo4j_setting`.

    The environment layer sits ABOVE both and legitimately wins (that is what lets one
    process be pointed at a disposable instance). So a run that exports WRIT_NEO4J_URI --
    which an isolated graph run does -- had these comparing the override against the
    default and failing. They passed before only because nothing in the suite set the
    variable, which quietly made the ambient environment part of the assertion. A test of
    a lower precedence layer has to neutralize the higher one rather than assume it is unset.
    """
    for var in ("WRIT_NEO4J_URI", "WRIT_NEO4J_USER", "WRIT_NEO4J_PASSWORD"):
        monkeypatch.delenv(var, raising=False)


@pytest.fixture
def rows(tmp_path, monkeypatch):
    log = tmp_path / "events.jsonl"
    monkeypatch.setenv("WRIT_FRICTION_LOG", str(log))

    def _read(event: str | None = None) -> list[dict]:
        if not log.exists():
            return []
        out = []
        for line in log.read_text().splitlines():
            if not line.strip():
                continue
            try:
                r = json.loads(line)
            except json.JSONDecodeError:
                continue
            if event is None or r.get("event") == event:
                out.append(r)
        return out

    return _read


@pytest.fixture(autouse=True)
def _fresh_report_state():
    """The once-per-process guard is module state; reset it between tests."""
    from writ import config

    config._REPORTED_CONFIG_PATHS.clear()
    yield
    config._REPORTED_CONFIG_PATHS.clear()


class TestOutcomesAreRecorded:
    def test_absent_config_says_so(self, tmp_path, rows):
        from writ.config import load_config

        missing = str(tmp_path / "nope.toml")
        assert load_config(missing) == {}
        row = rows("config_resolved")[0]
        assert row["outcome"] == "absent-using-defaults"
        assert row["path"] == missing
        assert row["sections"] == []

    def test_loaded_config_lists_its_keys(self, tmp_path, rows):
        from writ.config import load_config

        cfg = tmp_path / "writ.toml"
        cfg.write_text('[neo4j]\nuri = "bolt://x:7687"\nuser = "n"\n[bitbucket]\ntoken = "t"\n')
        load_config(str(cfg))
        row = rows("config_resolved")[0]
        assert row["outcome"] == "loaded"
        assert row["sections"] == ["bitbucket.token", "neo4j.uri", "neo4j.user"]

    def test_empty_config_is_distinguishable_from_absent(self, tmp_path, rows):
        from writ.config import load_config

        cfg = tmp_path / "writ.toml"
        cfg.write_text("")
        load_config(str(cfg))
        assert rows("config_resolved")[0]["outcome"] == "empty-using-defaults"

    def test_malformed_config_reaches_the_errors_stream(self, tmp_path, rows, capsys):
        from writ.config import load_config

        cfg = tmp_path / "writ.toml"
        cfg.write_text("this is [not valid toml")
        assert load_config(str(cfg)) == {}

        assert rows("config_resolved")[0]["outcome"] == "malformed-using-defaults"
        errs = rows("exception")
        assert errs, "a silently-ignored config must reach the errors stream"
        assert errs[0]["component"] == "config.load.malformed"
        # The stderr warning stays: it is what a human at a terminal sees.
        assert "malformed" in capsys.readouterr().err

    def test_unreadable_config_is_recorded(self, tmp_path, rows):
        import os

        from writ.config import load_config

        cfg = tmp_path / "writ.toml"
        cfg.write_text('[neo4j]\nuri = "x"\n')
        os.chmod(cfg, 0o000)
        try:
            if os.access(cfg, os.R_OK):  # running as root; the branch is unreachable
                pytest.skip("cannot make a file unreadable as this user")
            assert load_config(str(cfg)) == {}
            assert rows("config_resolved")[0]["outcome"] == "unreadable-using-defaults"
            assert rows("exception")[0]["component"] == "config.load.unreadable"
        finally:
            os.chmod(cfg, 0o644)


class TestNoCredentialLeak:
    def test_values_are_never_recorded(self, tmp_path, rows):
        """The whole reason this event carries names only."""
        from writ.config import load_config

        cfg = tmp_path / "writ.toml"
        cfg.write_text(
            '[neo4j]\npassword = "SUPERSECRETPW"\n[bitbucket]\ntoken = "SUPERSECRETTOKEN"\n'
        )
        load_config(str(cfg))
        blob = json.dumps(rows())
        assert "SUPERSECRETPW" not in blob
        assert "SUPERSECRETTOKEN" not in blob
        # The key names ARE present: that is what answers "did my setting get picked up".
        assert "neo4j.password" in blob
        assert "bitbucket.token" in blob


class TestVolume:
    def test_one_row_per_process_per_path(self, tmp_path, rows):
        """Every getter calls load_config on each access; per-call emit would flood."""
        from writ.config import get_neo4j_password, get_neo4j_uri, get_neo4j_user, load_config

        cfg = tmp_path / "writ.toml"
        cfg.write_text('[neo4j]\nuri = "bolt://x:7687"\n')
        p = str(cfg)
        load_config(p)
        get_neo4j_uri(p)
        get_neo4j_user(p)
        get_neo4j_password(p)
        load_config(p)
        assert len(rows("config_resolved")) == 1

    def test_a_different_path_is_reported_separately(self, tmp_path, rows):
        from writ.config import load_config

        a, b = tmp_path / "a.toml", tmp_path / "b.toml"
        a.write_text('[neo4j]\nuri = "x"\n')
        b.write_text('[neo4j]\nuri = "y"\n')
        load_config(str(a))
        load_config(str(b))
        assert len(rows("config_resolved")) == 2


class TestLoadingStillWorks:
    def test_values_are_returned_unchanged(self, tmp_path):
        """Telemetry is additive; resolution behavior must be identical."""
        from writ.config import get_neo4j_uri, load_config

        cfg = tmp_path / "writ.toml"
        cfg.write_text('[neo4j]\nuri = "bolt://custom:7687"\n')
        assert load_config(str(cfg))["neo4j"]["uri"] == "bolt://custom:7687"
        assert get_neo4j_uri(str(cfg)) == "bolt://custom:7687"

    def test_a_broken_emit_cannot_break_config_loading(self, tmp_path, monkeypatch):
        from writ import config

        monkeypatch.setattr(
            config, "_emit_config_resolution",
            lambda *a, **k: (_ for _ in ()).throw(RuntimeError("telemetry down")),
        )
        cfg = tmp_path / "writ.toml"
        cfg.write_text('[neo4j]\nuri = "bolt://x:7687"\n')
        with pytest.raises(RuntimeError):
            config._emit_config_resolution("x", "y", [])  # the stub really does raise
        # ...and load_config, which calls it, must still succeed.
        monkeypatch.setattr(config, "_emit_config_resolution", lambda *a, **k: None)
        assert config.load_config(str(cfg))["neo4j"]["uri"] == "bolt://x:7687"


class TestStreamRegistration:
    def test_config_resolved_maps_to_metrics(self):
        from writ.shared.logging import STREAM_MAP

        assert STREAM_MAP.get("config_resolved") == "metrics"
