"""Program item 3 (security baseline): the published development Neo4j password is refused.

DEFAULT_NEO4J_PASSWORD is published in this repository, so connecting with it means connecting
with a password anyone can read. The refusal lives in one place, Neo4jConnection.__init__, because
every daemon, CLI, doctor, script and benchmark connection is constructed there.

No network: nothing here talks to Neo4j. The neo4j driver creates connections lazily, so
constructing a Neo4jConnection (the allowed cases) opens no socket, and every test closes what it
built. No test reads or writes the real install's config file; the values used for "some other
password" are built at runtime so no credential-shaped literal sits in this file.

New names (DevPasswordRefused, refuse_dev_password, ...) are imported inside each test, so a
missing implementation fails that test with an ImportError naming the missing symbol rather than
taking the whole module down at collection.
"""
from __future__ import annotations

import asyncio
import sys
import uuid
from pathlib import Path

import pytest
import typer
from typer.testing import CliRunner

from tests._graph import ISOLATED_NEO4J_PASSWORD
from writ.config import DEFAULT_NEO4J_PASSWORD

REPO = Path(__file__).resolve().parent.parent
WORKFLOW = REPO / ".github" / "workflows" / "pr.yml"

OPT_IN = "WRIT_ALLOW_DEV_PASSWORD"
REMEDY = "writ neo4j set-password"


@pytest.fixture()
def dev_password_env(monkeypatch):
    """The environment of an install still on the development password, no opt-in."""
    monkeypatch.setenv("WRIT_NEO4J_PASSWORD", DEFAULT_NEO4J_PASSWORD)
    monkeypatch.delenv(OPT_IN, raising=False)


def _make_connection(password: str):
    from writ.graph.db import Neo4jConnection

    return Neo4jConnection("bolt://localhost:7688", "neo4j", password)


class TestConnectionRefusal:
    def test_the_development_password_raises_and_names_the_remedy_and_the_opt_in(
        self, dev_password_env
    ):
        from writ.config import DevPasswordRefused

        with pytest.raises(DevPasswordRefused) as caught:
            _make_connection(DEFAULT_NEO4J_PASSWORD)
        text = str(caught.value)
        assert REMEDY in text
        assert OPT_IN in text

    def test_no_driver_is_created_when_the_password_is_refused(
        self, dev_password_env, monkeypatch
    ):
        from writ.config import DevPasswordRefused

        created = []
        monkeypatch.setattr(
            "writ.graph.db.AsyncGraphDatabase.driver",
            lambda *args, **kwargs: created.append((args, kwargs)),
        )
        with pytest.raises(DevPasswordRefused):
            _make_connection(DEFAULT_NEO4J_PASSWORD)
        assert created == [], "the refusal must come before any driver exists"

    def test_the_refusal_message_never_contains_the_password_value(self, dev_password_env):
        from writ.config import dev_password_refusal_message

        assert DEFAULT_NEO4J_PASSWORD not in dev_password_refusal_message()

    def test_the_exit_code_constant_is_78(self):
        from writ.config import DEV_REFUSAL_EXIT_CODE

        assert DEV_REFUSAL_EXIT_CODE == 78

    def test_the_remedy_command_constant_is_the_set_password_command(self):
        from writ.config import REMEDY_COMMAND

        assert REMEDY_COMMAND == REMEDY


class TestOptInIsExact:
    @pytest.mark.parametrize("value", ["1", " 1 "])
    def test_only_exactly_one_permits_the_development_password(
        self, value, monkeypatch
    ):
        monkeypatch.setenv(OPT_IN, value)
        from writ.config import dev_password_allowed

        assert dev_password_allowed() is True
        db = _make_connection(DEFAULT_NEO4J_PASSWORD)
        asyncio.run(db.close())

    @pytest.mark.parametrize("value", ["true", "yes", "0", ""])
    def test_near_miss_values_are_refused(self, value, monkeypatch):
        from writ.config import DevPasswordRefused, dev_password_allowed

        monkeypatch.setenv(OPT_IN, value)
        assert dev_password_allowed() is False
        with pytest.raises(DevPasswordRefused):
            _make_connection(DEFAULT_NEO4J_PASSWORD)

    def test_an_unset_opt_in_is_refused(self, monkeypatch):
        from writ.config import dev_password_allowed

        monkeypatch.delenv(OPT_IN, raising=False)
        assert dev_password_allowed() is False

    def test_dev_password_allowed_reads_a_mapping_when_given_one(self, monkeypatch):
        from writ.config import dev_password_allowed

        monkeypatch.delenv(OPT_IN, raising=False)
        assert dev_password_allowed({OPT_IN: "1"}) is True
        assert dev_password_allowed({OPT_IN: "yes"}) is False
        assert dev_password_allowed({}) is False


class TestOtherPasswordsConstructNormally:
    def test_the_isolated_test_graph_password_is_accepted(self, dev_password_env):
        db = _make_connection(ISOLATED_NEO4J_PASSWORD)
        asyncio.run(db.close())

    def test_a_private_password_is_accepted(self, dev_password_env):
        db = _make_connection(uuid.uuid4().hex)
        asyncio.run(db.close())

    def test_refuse_dev_password_returns_none_for_the_isolated_password(self, dev_password_env):
        from writ.config import refuse_dev_password

        assert refuse_dev_password(ISOLATED_NEO4J_PASSWORD) is None

    def test_the_isolated_password_differs_from_the_development_default(self):
        """The suite's own guarantee: it can never be caught by the refusal."""
        assert ISOLATED_NEO4J_PASSWORD != DEFAULT_NEO4J_PASSWORD


class TestServeRefusesBeforeBinding:
    def test_serve_exits_78_names_the_command_and_never_reaches_uvicorn(
        self, dev_password_env, monkeypatch
    ):
        """A stub uvicorn stands in for the real one, so the test runs whether or not uvicorn is
        installed and any path into it is recorded instead of binding a port."""
        import types

        from writ.cli import app

        reached = []

        def _config_must_not_be_built(*args, **kwargs):
            reached.append((args, kwargs))
            raise AssertionError("uvicorn.Config reached: a refused serve must bind nothing")

        stub = types.ModuleType("uvicorn")
        stub.Config = _config_must_not_be_built
        stub.Server = _config_must_not_be_built
        stub.run = _config_must_not_be_built
        monkeypatch.setitem(sys.modules, "uvicorn", stub)
        result = CliRunner().invoke(app, ["serve", "--port", "1"])
        assert reached == []
        assert result.exit_code == 78, result.output
        assert REMEDY in result.output
        assert DEFAULT_NEO4J_PASSWORD not in result.output


class TestCliOpensRefuseCleanly:
    def test_writ_db_exits_78_instead_of_a_traceback(self, dev_password_env, capsys):
        from writ import cli

        async def _open():
            async with cli._writ_db():
                raise AssertionError("the body must not run when the password is refused")

        with pytest.raises(typer.Exit) as caught:
            asyncio.run(_open())
        assert caught.value.exit_code == 78
        assert REMEDY in capsys.readouterr().err


class TestDaemonLifespanRefuses:
    def test_lifespan_raises_before_the_pipeline_is_built(self, dev_password_env, monkeypatch):
        import writ.server as server
        from writ.config import DevPasswordRefused

        built = []

        async def _pipeline_must_not_be_built(*args, **kwargs):
            built.append(True)
            raise AssertionError("build_pipeline reached with a refused password")

        monkeypatch.setattr("writ.server.build_pipeline", _pipeline_must_not_be_built)

        async def _run():
            async with server.lifespan(server.app):
                pass

        with pytest.raises(DevPasswordRefused):
            asyncio.run(_run())
        assert built == []


class TestCiKeepsItsThrowawayServices:
    def test_the_workflow_opts_in_at_workflow_level(self):
        text = WORKFLOW.read_text(encoding="utf-8")
        top_env = text.split("\nenv:", 1)
        assert len(top_env) == 2, "pr.yml has no top-level env: block"
        block = top_env[1].split("\njobs:", 1)[0]
        assert 'WRIT_ALLOW_DEV_PASSWORD: "1"' in block


class TestDoctorNeo4jPasswordCheck:
    def _check(self, monkeypatch, state):
        from writ.session.doctor import DoctorOptions, check_neo4j_password

        monkeypatch.setattr("writ.session.doctor._neo4j_dev_password_state", lambda: state)
        return check_neo4j_password(DoctorOptions())

    def test_refused_state_fails_and_names_the_command(self, monkeypatch):
        from writ.session.doctor import STATUS_FAIL

        result = self._check(monkeypatch, "refused")
        assert result.name == "neo4j-password"
        assert result.status == STATUS_FAIL
        assert REMEDY in result.detail
        assert result.fixable is False

    def test_allowed_state_warns_and_names_the_opt_in(self, monkeypatch):
        from writ.session.doctor import STATUS_WARN

        result = self._check(monkeypatch, "allowed")
        assert result.status == STATUS_WARN
        assert OPT_IN in result.detail

    def test_private_state_is_ok(self, monkeypatch):
        from writ.session.doctor import STATUS_OK

        result = self._check(monkeypatch, "private")
        assert result.status == STATUS_OK

    @pytest.mark.parametrize("state", ["refused", "allowed", "private"])
    def test_no_detail_ever_contains_the_password(self, monkeypatch, state):
        result = self._check(monkeypatch, state)
        assert DEFAULT_NEO4J_PASSWORD not in result.detail

    def test_the_state_seam_reports_refused_for_the_default_without_opt_in(
        self, dev_password_env
    ):
        from writ.session.doctor import _neo4j_dev_password_state

        assert _neo4j_dev_password_state() == "refused"

    def test_the_state_seam_reports_allowed_for_the_default_with_opt_in(
        self, dev_password_env, monkeypatch
    ):
        from writ.session.doctor import _neo4j_dev_password_state

        monkeypatch.setenv(OPT_IN, "1")
        assert _neo4j_dev_password_state() == "allowed"

    def test_the_state_seam_reports_private_for_any_other_password(self, monkeypatch):
        from writ.session.doctor import _neo4j_dev_password_state

        monkeypatch.setenv("WRIT_NEO4J_PASSWORD", uuid.uuid4().hex)
        monkeypatch.delenv(OPT_IN, raising=False)
        assert _neo4j_dev_password_state() == "private"
