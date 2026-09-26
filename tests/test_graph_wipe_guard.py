"""The whole-graph wipe refuses unless the target is explicitly disposable.

Regression cover for the incident that produced the guard: a full pytest run
destroyed the live graph's runtime records (2 Memory nodes against 98 on-disk
memory files; Decision, FileChange and Commit all 0), because two test fixtures
called `clear_all` with an empty preserve set against the interactive instance.
Rules came back from `bible/`; a Decision record has no source and did not.

These tests never touch a real Neo4j. `clear_all` is exercised through a fake
driver that RECORDS the statements it is asked to run, so "nothing was deleted"
is proved by the absence of a delete statement rather than assumed from the
absence of an exception.

Anti-vacuity is explicit rather than implied. `TestGuardIsNotVacuous` stubs the
guard to always allow and shows the same call then issues the delete, so a
refusal test cannot pass because the code path was unreachable, and
`TestGuardObservesTheCondition` moves one input at a time so a constant-answer
guard fails.
"""

from __future__ import annotations

import ast
import re
import uuid
from pathlib import Path

import pytest

from writ.graph.db import FullWipeRefused
from writ.graph.db._safety import (
    TEST_GRAPH_ENV_VAR,
    TEST_GRAPH_OPT_IN,
    assert_full_wipe_allowed,
    full_wipe_allowed,
    how_to_run_safely,
    instance_key,
    is_production_instance,
    marker_present,
)
from writ.graph.db.maintenance_store import MaintenanceStoreMixin

PROD_URI = "bolt://localhost:7687"
DISPOSABLE_URI = "bolt://localhost:7688"

REPO_ROOT = Path(__file__).resolve().parent.parent


# --- A fake connection that records instead of deleting -----------------------


class _FakeResult:
    async def consume(self) -> None:
        return None


class _FakeSession:
    def __init__(self, log: list[tuple[str, dict]]) -> None:
        self._log = log

    async def __aenter__(self) -> "_FakeSession":
        return self

    async def __aexit__(self, *exc_info: object) -> bool:
        return False

    async def run(self, query: str, **params: object) -> _FakeResult:
        self._log.append((query, params))
        return _FakeResult()


class _FakeDriver:
    def __init__(self, log: list[tuple[str, dict]]) -> None:
        self._log = log

    def session(self, database: str | None = None) -> _FakeSession:
        return _FakeSession(self._log)


class FakeConnection(MaintenanceStoreMixin):
    """The REAL clear_all over a driver that records statements.

    Subclassing the production mixin (rather than reimplementing the guard) is
    what makes these tests worth anything: they run the shipped code path.
    """

    def __init__(self, uri: str | None) -> None:
        self.queries: list[tuple[str, dict]] = []
        self._driver = _FakeDriver(self.queries)
        self._database = "neo4j"
        self._uri = uri

    @property
    def deletes(self) -> list[str]:
        return [q for q, _ in self.queries if "DETACH DELETE" in q]


@pytest.fixture()
def production_is_localhost_7687(monkeypatch):
    """Pin production identity so these tests never depend on the local writ.toml.

    `_safety` imports get_production_neo4j_uri from writ.config at call time, so
    patching the config module reaches every caller.
    """
    monkeypatch.setattr(
        "writ.config.get_production_neo4j_uri", lambda path=None: PROD_URI
    )


@pytest.fixture()
def marker_absent(monkeypatch):
    monkeypatch.delenv(TEST_GRAPH_ENV_VAR, raising=False)


@pytest.fixture()
def marker_set(monkeypatch):
    monkeypatch.setenv(TEST_GRAPH_ENV_VAR, TEST_GRAPH_OPT_IN)


def _fixture_function(fixture):
    """The undecorated function behind a pytest fixture (pytest blocks direct calls)."""
    return getattr(fixture, "__wrapped__", fixture)


# The unscoped whole-graph delete `clear_all` issues when the preserve set is
# empty. It appears here as an ASSERTION TARGET and is never executed: these
# tests run over a fake driver that records statements instead of a Neo4j
# session, and in the refusal cases the point is proving it was never produced.
#
# tests/test_graph_dump.py::TestNoRawWholeGraphDeletes scans the tree for this
# exact constant to catch deletes that bypass clear_all's record preservation,
# and carries a narrow, self-limiting exemption for this file. Writing the
# statement dynamically to slip past that scan would be the wrong fix: a
# scanner that cannot see this occurrence could not see a real one either.
EVERYTHING_WIPE = "MATCH (n) DETACH DELETE n"


def assert_is_the_everything_wipe(statements: list[str]) -> None:
    """Assert exactly one unscoped whole-graph delete was issued."""
    assert statements == [EVERYTHING_WIPE], (
        f"expected exactly the unscoped everything-wipe, got {statements}"
    )


# --- 1. The refusal actually refuses, and deletes nothing ---------------------


class TestRefusalDeletesNothing:
    """Marker absent: the destructive path must refuse AND leave the graph alone."""

    @pytest.mark.asyncio
    async def test_full_wipe_against_production_raises(
        self, marker_absent, production_is_localhost_7687
    ) -> None:
        conn = FakeConnection(PROD_URI)
        with pytest.raises(FullWipeRefused):
            await conn.clear_all(preserve_labels=frozenset())

    @pytest.mark.asyncio
    async def test_refused_wipe_issues_no_statement_at_all(
        self, marker_absent, production_is_localhost_7687
    ) -> None:
        # The load-bearing assertion. An exception alone would not prove the
        # delete had not already been dispatched -- clear_all's own docstring
        # records that `session.run` is lazy and had to be explicitly consumed.
        conn = FakeConnection(PROD_URI)
        with pytest.raises(FullWipeRefused):
            await conn.clear_all(preserve_labels=frozenset())
        assert conn.queries == [], (
            "a refused wipe must not reach the database at all; "
            f"these statements were issued: {conn.queries}"
        )

    @pytest.mark.asyncio
    async def test_marker_set_but_pointed_at_production_still_refuses(
        self, marker_set, production_is_localhost_7687
    ) -> None:
        # The sticky-env-var failure mode: WRIT_TEST_GRAPH exported once in a
        # shell profile must not authorize wiping the real instance months later.
        conn = FakeConnection(PROD_URI)
        with pytest.raises(FullWipeRefused):
            await conn.clear_all(preserve_labels=frozenset())
        assert conn.queries == []

    @pytest.mark.asyncio
    async def test_separate_instance_without_marker_refuses(
        self, marker_absent, production_is_localhost_7687
    ) -> None:
        # The other half: a config edit or a stray URI must not be enough on its
        # own. A human has to say the graph is disposable.
        conn = FakeConnection(DISPOSABLE_URI)
        with pytest.raises(FullWipeRefused):
            await conn.clear_all(preserve_labels=frozenset())
        assert conn.queries == []

    @pytest.mark.asyncio
    async def test_unknown_uri_refuses(
        self, marker_set, production_is_localhost_7687
    ) -> None:
        # A connection whose target cannot be identified is treated as production.
        conn = FakeConnection(None)
        with pytest.raises(FullWipeRefused):
            await conn.clear_all(preserve_labels=frozenset())
        assert conn.queries == []

    @pytest.mark.asyncio
    async def test_loopback_alias_does_not_disguise_production(
        self, marker_set, production_is_localhost_7687
    ) -> None:
        # 127.0.0.1:7687 is a different STRING and the same SERVER. String
        # equality would have called this "not production" and wiped it.
        conn = FakeConnection("bolt://127.0.0.1:7687")
        with pytest.raises(FullWipeRefused):
            await conn.clear_all(preserve_labels=frozenset())
        assert conn.queries == []

    @pytest.mark.asyncio
    async def test_scheme_change_does_not_disguise_production(
        self, marker_set, production_is_localhost_7687
    ) -> None:
        conn = FakeConnection("neo4j://localhost:7687")
        with pytest.raises(FullWipeRefused):
            await conn.clear_all(preserve_labels=frozenset())
        assert conn.queries == []


# --- 2. With the marker present it proceeds ----------------------------------


class TestMarkerPresentProceeds:
    @pytest.mark.asyncio
    async def test_disposable_instance_with_marker_issues_the_delete(
        self, marker_set, production_is_localhost_7687
    ) -> None:
        conn = FakeConnection(DISPOSABLE_URI)
        await conn.clear_all(preserve_labels=frozenset())
        # A properly isolated instance must still get the full wipe.
        assert_is_the_everything_wipe(conn.deletes)

    @pytest.mark.asyncio
    async def test_remote_host_with_marker_is_allowed(
        self, marker_set, production_is_localhost_7687
    ) -> None:
        conn = FakeConnection("bolt://scratch.example.test:7687")
        await conn.clear_all(preserve_labels=frozenset())
        assert conn.deletes


# --- 3. The ~89 record-preserving calls are untouched ------------------------


class TestDefaultCallsAreNotGated:
    """The bare `clear_all()` calls were never the problem and must not regress.

    They preserve runtime records, and the corpus they do delete rebuilds from
    bible/ or writ-corpus.cypher. Gating them would have broken ~89 call sites
    to fix a bug caused by 7.
    """

    @pytest.mark.asyncio
    async def test_bare_clear_all_runs_against_production_without_a_marker(
        self, marker_absent, production_is_localhost_7687
    ) -> None:
        conn = FakeConnection(PROD_URI)
        await conn.clear_all()
        assert conn.deletes, "the record-preserving default must not be gated"
        query, params = conn.queries[0]
        assert "NOT any(l IN labels(n) WHERE l IN $preserve)" in query
        # Project joined RECORD_LABELS in cycle 6a. It belongs on both axes the
        # set governs: a registry entry is authored by create_project and never
        # by ingest, so a wipe that took it had nothing to restore it from (that
        # destroyed the registry twice), and it carries a local repo_root path
        # that must never ship in the public dump.
        # Plan f7fc2b37-9a53-4011-a69f-e6b97f5e45fe, item B: FeedbackBatch joined
        # RECORD_LABELS. It is runtime replay-protection state with no bible or
        # dump home (get_all_nodes_for_dump excludes it), so a corpus replay must
        # preserve it exactly as it preserves the other four record labels.
        assert params["preserve"] == [
            "Commit", "Decision", "FeedbackBatch", "FileChange", "Memory", "Project",
        ]

    @pytest.mark.asyncio
    async def test_partial_preserve_set_runs_without_a_marker(
        self, marker_absent, production_is_localhost_7687
    ) -> None:
        conn = FakeConnection(PROD_URI)
        await conn.clear_all(preserve_labels=frozenset({"Memory"}))
        assert conn.deletes
        assert conn.queries[0][1]["preserve"] == ["Memory"]

    @pytest.mark.asyncio
    async def test_corpus_replay_preserve_set_is_never_empty(
        self, marker_absent, production_is_localhost_7687
    ) -> None:
        # writ/graph/dump.py computes RECORD_LABELS - labels_in_dump. If a real
        # dump could carry all four record labels that would resolve to an empty
        # set and trip this guard on a legitimate restore. get_all_nodes_for_dump
        # structurally EXCLUDES record labels, so it cannot; this pins that the
        # corpus-replay path stays outside the guard.
        from writ.graph.db._common import RECORD_LABELS

        dump_file = REPO_ROOT / "writ-corpus.cypher"
        if not dump_file.exists():
            pytest.skip("writ-corpus.cypher absent; not a writ checkout")
        labels_in_dump = set(
            re.findall(
                r"CREATE \(:([A-Za-z]+)", dump_file.read_text(encoding="utf-8")
            )
        )
        assert RECORD_LABELS - labels_in_dump, (
            "the shipped dump carries every runtime-record label, so a corpus "
            "replay would resolve to an everything-wipe and hit the guard"
        )
        conn = FakeConnection(PROD_URI)
        await conn.clear_all(preserve_labels=RECORD_LABELS - labels_in_dump)
        assert conn.deletes


# --- 4. Anti-vacuity -----------------------------------------------------------


class TestGuardIsNotVacuous:
    """Prove the guard is what blocks the delete, not some unrelated failure.

    Without this, every refusal test above could pass against a `clear_all` that
    raised for a different reason, or against a fake that never ran anything.
    """

    @pytest.mark.asyncio
    async def test_stubbing_the_guard_to_always_allow_lets_the_delete_through(
        self, marker_absent, production_is_localhost_7687, monkeypatch
    ) -> None:
        # Identical call to test_refused_wipe_issues_no_statement_at_all, with
        # ONLY the guard replaced. If the guard were a no-op, that test would be
        # vacuous -- and this one shows the call is otherwise perfectly capable
        # of issuing the delete.
        monkeypatch.setattr(
            "writ.graph.db._safety.assert_full_wipe_allowed",
            lambda *args, **kwargs: None,
        )
        conn = FakeConnection(PROD_URI)
        await conn.clear_all(preserve_labels=frozenset())
        # With the guard stubbed out the wipe must proceed. If it does not, the
        # refusal tests are passing for a reason other than the guard.
        assert_is_the_everything_wipe(conn.deletes)

    def test_an_always_allow_stub_disagrees_with_the_real_predicate(
        self, marker_absent, production_is_localhost_7687
    ) -> None:
        # The mutation stated directly: an always-True predicate must NOT satisfy
        # the contract the real predicate does. If these agreed, the contract
        # would assert nothing.
        def always_allow(uri, production_uri=None):
            return True

        assert always_allow(PROD_URI) != full_wipe_allowed(PROD_URI), (
            "the real guard agrees with an always-allow stub on the production "
            "URI with no marker; the guard is not discriminating"
        )

    def test_an_always_deny_stub_also_disagrees_with_the_real_predicate(
        self, marker_set, production_is_localhost_7687
    ) -> None:
        # The other direction. A guard that refuses everything would pass every
        # refusal test above while making the feature unusable.
        def always_deny(uri, production_uri=None):
            return False

        assert always_deny(DISPOSABLE_URI) != full_wipe_allowed(DISPOSABLE_URI), (
            "the real guard agrees with an always-deny stub on a properly "
            "isolated instance; the guard never permits anything"
        )


class TestGuardObservesTheCondition:
    """One input moves at a time, so a constant-answer guard cannot pass."""

    def test_decision_matrix(self, monkeypatch) -> None:
        cases = [
            # (marker present, uri, expected allowed)
            (False, PROD_URI, False),
            (True, PROD_URI, False),
            (False, DISPOSABLE_URI, False),
            (True, DISPOSABLE_URI, True),
        ]
        for marker, uri, expected in cases:
            if marker:
                monkeypatch.setenv(TEST_GRAPH_ENV_VAR, TEST_GRAPH_OPT_IN)
            else:
                monkeypatch.delenv(TEST_GRAPH_ENV_VAR, raising=False)
            actual = full_wipe_allowed(uri, production_uri=PROD_URI)
            assert actual is expected, (
                f"marker={marker} uri={uri}: expected allowed={expected}, got {actual}"
            )

    def test_exactly_one_of_four_combinations_allows(self, monkeypatch) -> None:
        # Guards against a predicate that drifts from AND to OR: three of the
        # four combinations must deny.
        results = []
        for marker in (False, True):
            for uri in (PROD_URI, DISPOSABLE_URI):
                if marker:
                    monkeypatch.setenv(TEST_GRAPH_ENV_VAR, TEST_GRAPH_OPT_IN)
                else:
                    monkeypatch.delenv(TEST_GRAPH_ENV_VAR, raising=False)
                results.append(full_wipe_allowed(uri, production_uri=PROD_URI))
        assert results.count(True) == 1, (
            f"exactly one combination may allow the wipe, got {results}"
        )

    def test_marker_requires_the_exact_opt_in_value(self, monkeypatch) -> None:
        for near_miss in ("", "0", "true", "TRUE", "yes", "on", "1x"):
            monkeypatch.setenv(TEST_GRAPH_ENV_VAR, near_miss)
            assert not marker_present(), f"{near_miss!r} must not read as consent"
        monkeypatch.setenv(TEST_GRAPH_ENV_VAR, TEST_GRAPH_OPT_IN)
        assert marker_present()


# --- 5. Instance identity ------------------------------------------------------


class TestInstanceKey:
    @pytest.mark.parametrize(
        "uri",
        [
            "bolt://localhost:7687",
            "bolt://127.0.0.1:7687",
            "bolt://[::1]:7687",
            "neo4j://localhost:7687",
            "bolt+s://LOCALHOST:7687",
            "bolt://localhost",  # port omitted, defaults to 7687
        ],
    )
    def test_every_spelling_of_the_local_default_is_one_instance(self, uri) -> None:
        assert instance_key(uri) == ("localhost", 7687), (
            f"{uri} must resolve to the same instance as {PROD_URI}; a spelling "
            "that reads as a different server would authorize wiping production"
        )

    def test_a_different_port_is_a_different_instance(self) -> None:
        assert instance_key(DISPOSABLE_URI) == ("localhost", 7688)
        assert instance_key(DISPOSABLE_URI) != instance_key(PROD_URI)

    def test_a_different_host_is_a_different_instance(self) -> None:
        assert instance_key("bolt://db.example.test:7687") == ("db.example.test", 7687)

    @pytest.mark.parametrize("uri", [None, "", "   ", "bolt://"])
    def test_unidentifiable_uris_return_none(self, uri) -> None:
        assert instance_key(uri) is None

    def test_unidentifiable_uri_is_treated_as_production(self) -> None:
        assert is_production_instance(None, production_uri=PROD_URI) is True
        assert is_production_instance("bolt://", production_uri=PROD_URI) is True

    def test_unidentifiable_production_blocks_everything(self) -> None:
        # If we cannot say what production is, nothing can be proven distinct.
        assert is_production_instance(DISPOSABLE_URI, production_uri="") is True


# --- 6. The refusal tells a developer what to do -------------------------------


class TestRefusalIsActionable:
    def test_message_names_the_env_vars_the_port_and_the_docker_command(
        self, marker_absent
    ) -> None:
        with pytest.raises(FullWipeRefused) as excinfo:
            assert_full_wipe_allowed(PROD_URI, production_uri=PROD_URI)
        message = str(excinfo.value)
        assert TEST_GRAPH_ENV_VAR in message
        assert "WRIT_NEO4J_URI" in message
        assert "docker run" in message
        assert "7688" in message, "the message must name a concrete alternate port"

    def test_message_explains_why_records_cannot_be_rebuilt(self, marker_absent) -> None:
        with pytest.raises(FullWipeRefused) as excinfo:
            assert_full_wipe_allowed(PROD_URI, production_uri=PROD_URI)
        assert "Decision" in str(excinfo.value)

    def test_marker_set_refusal_says_the_target_is_wrong_not_the_marker(
        self, marker_set
    ) -> None:
        with pytest.raises(FullWipeRefused) as excinfo:
            assert_full_wipe_allowed(PROD_URI, production_uri=PROD_URI)
        message = str(excinfo.value)
        assert "is set" in message and "production instance" in message, (
            "when the marker IS set the message must say the TARGET is wrong, "
            f"not repeat 'set the marker'; got: {message}"
        )

    def test_instructions_are_shared_by_the_refusal_and_the_skip(
        self, marker_absent
    ) -> None:
        # One source, so the exception and the pytest skip cannot drift into
        # telling a developer two different things about the same requirement.
        with pytest.raises(FullWipeRefused) as excinfo:
            assert_full_wipe_allowed(PROD_URI, production_uri=PROD_URI)
        assert how_to_run_safely() in str(excinfo.value)

    def test_allowed_wipe_raises_nothing(self, marker_set) -> None:
        assert_full_wipe_allowed(DISPOSABLE_URI, production_uri=PROD_URI)


# --- 7. The config seam --------------------------------------------------------


class TestConfigSeam:
    def test_env_overrides_the_config_file(self, tmp_path, monkeypatch) -> None:
        from writ.config import get_neo4j_uri, get_neo4j_user

        toml_file = tmp_path / "writ.toml"
        toml_file.write_text('[neo4j]\nuri = "bolt://from-file:7687"\nuser = "fileuser"\n')
        monkeypatch.setenv("WRIT_NEO4J_URI", "bolt://from-env:7688")
        monkeypatch.setenv("WRIT_NEO4J_USER", "envuser")
        assert get_neo4j_uri(str(toml_file)) == "bolt://from-env:7688"
        assert get_neo4j_user(str(toml_file)) == "envuser"

    def test_password_env_override_beats_the_built_in_default(
        self, tmp_path, monkeypatch
    ) -> None:
        # Value generated at runtime: a credential literal in source is itself a
        # finding, and the assertion only needs the value to round-trip.
        from writ.config import DEFAULT_NEO4J_PASSWORD, get_neo4j_password

        secret = uuid.uuid4().hex
        monkeypatch.setenv("WRIT_NEO4J_PASSWORD", secret)
        resolved = get_neo4j_password(str(tmp_path / "absent.toml"))
        assert resolved == secret
        assert resolved != DEFAULT_NEO4J_PASSWORD

    def test_config_file_wins_when_env_is_unset(self, tmp_path, monkeypatch) -> None:
        from writ.config import get_neo4j_uri

        monkeypatch.delenv("WRIT_NEO4J_URI", raising=False)
        toml_file = tmp_path / "writ.toml"
        toml_file.write_text('[neo4j]\nuri = "bolt://from-file:7687"\n')
        assert get_neo4j_uri(str(toml_file)) == "bolt://from-file:7687"

    def test_default_wins_when_env_and_file_are_absent(
        self, tmp_path, monkeypatch
    ) -> None:
        from writ.config import DEFAULT_NEO4J_URI, get_neo4j_uri

        monkeypatch.delenv("WRIT_NEO4J_URI", raising=False)
        assert get_neo4j_uri(str(tmp_path / "absent.toml")) == DEFAULT_NEO4J_URI

    def test_blank_env_does_not_shadow_the_config_file(
        self, tmp_path, monkeypatch
    ) -> None:
        from writ.config import get_neo4j_uri

        monkeypatch.setenv("WRIT_NEO4J_URI", "   ")
        toml_file = tmp_path / "writ.toml"
        toml_file.write_text('[neo4j]\nuri = "bolt://from-file:7687"\n')
        assert get_neo4j_uri(str(toml_file)) == "bolt://from-file:7687"

    def test_production_uri_ignores_the_env_override(
        self, tmp_path, monkeypatch
    ) -> None:
        # THE bypass this closes: if WRIT_NEO4J_URI fed both sides of the
        # comparison, setting it would make every instance look non-production.
        from writ.config import get_production_neo4j_uri

        toml_file = tmp_path / "writ.toml"
        toml_file.write_text('[neo4j]\nuri = "bolt://from-file:7687"\n')
        monkeypatch.setenv("WRIT_NEO4J_URI", "bolt://from-env:7688")
        assert get_production_neo4j_uri(str(toml_file)) == "bolt://from-file:7687"

    def test_setting_the_env_override_alone_cannot_authorize_a_wipe(
        self, tmp_path, monkeypatch
    ) -> None:
        # End to end over both getters: pointing WRIT_NEO4J_URI back AT
        # production must not launder it into a disposable graph.
        from writ.config import get_neo4j_uri, get_production_neo4j_uri

        toml_file = tmp_path / "writ.toml"
        toml_file.write_text(f'[neo4j]\nuri = "{PROD_URI}"\n')
        monkeypatch.setenv("WRIT_NEO4J_URI", "bolt://127.0.0.1:7687")
        monkeypatch.setenv(TEST_GRAPH_ENV_VAR, TEST_GRAPH_OPT_IN)
        assert not full_wipe_allowed(
            get_neo4j_uri(str(toml_file)),
            production_uri=get_production_neo4j_uri(str(toml_file)),
        )


# --- 8. The gate cannot be dropped silently ------------------------------------


# --- The two recognized gating mechanisms ---------------------------------------
#
# The ratchet below asks one question of every module that performs an
# everything-wipe: is this wipe PROVABLY gated, and does tripping the gate
# produce a clean operator-facing error rather than a confusing one? For most of
# the suite the answer is the `disposable_graph` fixture. It is not the only
# possible answer, and treating it as the only one demands the impossible of a
# `pytest_sessionstart` hook, which runs before fixtures exist.
#
# So there are TWO recognized mechanisms, and each is DETECTED rather than
# granted. Nothing here is a name allowlist: a module named in EXPECTED_GATING
# whose mechanism decays is a failure, not an exemption, and a module gated by
# neither mechanism fails whatever it is called.
GATE_FIXTURE = "disposable_graph fixture"
GATE_MODULE_SAFETY = "module-scoped autouse target guard"
GATE_PREFLIGHT = "session-start preflight"

WIPE_CALL = "preserve_labels=frozenset()"
PREFLIGHT_FUNC = "_preflight_isolated_graph"
CONFTEST = REPO_ROOT / "tests" / "conftest.py"


def _top_level_functions(source: str):
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []
    return [n for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]


def _fixture_gated(path: Path, source: str | None = None) -> bool:
    """True when some function in the module takes `disposable_graph` as a PARAMETER.

    A parameter, not a substring. Mentioning the name in a comment is not
    wiring, which is the distinction test_the_db_fixtures_take_disposable_graph
    _as_a_parameter already draws for the `db` fixtures specifically; this
    applies it to the recognition itself so a module cannot be recognized as
    gated by talking about the gate.
    """
    text = path.read_text(encoding="utf-8") if source is None else source
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return False
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        args = node.args
        params = {a.arg for a in (*args.posonlyargs, *args.args, *args.kwonlyargs)}
        if "disposable_graph" in params:
            return True
    return False


def _is_autouse_fixture(node) -> bool:
    """True when this function is decorated as a pytest fixture with autouse=True."""
    for decorator in node.decorator_list:
        if not isinstance(decorator, ast.Call):
            continue
        func = decorator.func
        name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
        if name != "fixture":
            continue
        for keyword in decorator.keywords:
            if (
                keyword.arg == "autouse"
                and isinstance(keyword.value, ast.Constant)
                and keyword.value.value is True
            ):
                return True
    return False


def _autouse_target_guard(path: Path, source: str | None = None) -> bool:
    """True when the module refuses a non-disposable target before any of its tests run.

    THREE facts, all read off the source:

      1. the guard is a pytest fixture with `autouse=True`, so it covers every
         test in scope rather than only the ones that remember to request it,
      2. it consults `targets_production`, the same (host, port) comparison
         `clear_all` uses, rather than a bespoke check that could disagree,
      3. it REFUSES with `pytest.fail`. A guard that skips on a production
         target is not a gate: a mass skip is cheap to produce and
         indistinguishable from a green run, which is the lesson this repo has
         already paid for twice. Skipping on an UNREACHABLE instance is fine and
         is not what this looks at.

    Drop the autouse, the comparison or the failure and this returns False, so
    the mechanism has to keep working to keep being recognized.
    """
    text = path.read_text(encoding="utf-8") if source is None else source
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return False
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if not _is_autouse_fixture(node):
            continue
        segment = ast.get_source_segment(text, node) or ""
        if "targets_production(" in segment and "pytest.fail(" in segment:
            return True
    return False


def _routing_wipers(path: Path, source: str | None = None) -> set[str]:
    """Top-level functions in `path` whose wipe is ISSUED THROUGH `clear_all`.

    Routing is the first half of the preflight mechanism. A function that wrote
    its own `MATCH (n) DETACH DELETE n` would bypass `assert_full_wipe_allowed`
    entirely, so it is not recognized here no matter who calls it.
    """
    text = path.read_text(encoding="utf-8") if source is None else source
    names: set[str] = set()
    for node in _top_level_functions(text):
        segment = ast.get_source_segment(text, node) or ""
        if WIPE_CALL in segment and "clear_all(" in segment:
            names.add(node.name)
    return names


def _dotted_module_name(path: Path) -> str | None:
    """`tests/_graph.py` -> `tests._graph`, or None for anything outside the repo."""
    try:
        relative = path.resolve().relative_to(REPO_ROOT)
    except ValueError:
        return None
    return ".".join(relative.with_suffix("").parts)


def _preflight_body(conftest_source: str) -> str:
    for node in _top_level_functions(conftest_source):
        if node.name == PREFLIGHT_FUNC:
            return ast.get_source_segment(conftest_source, node) or ""
    return ""


def _preflight_gated(path: Path, conftest_source: str | None = None) -> bool:
    """True when this module's wipe is gated by the session-start preflight.

    FOUR facts, all read off the source, none assumed:

      1. the module issues its everything-wipe through `clear_all`, so
         `assert_full_wipe_allowed` still runs before any session opens,
      2. `_preflight_isolated_graph` imports that exact function FROM this
         module and calls it,
      3. `classify_isolation` runs BEFORE that call, so the target has already
         been proven reachable and not production when the delete is issued,
      4. the preflight converts `FullWipeRefused` into a `pytest.UsageError`,
         which is the clean operator-facing error the fixture otherwise
         provides. Without it a tripped gate is an INTERNALERROR traceback with
         no remedy on it, which is the confusing failure this ratchet's
         docstring names.

    Any one of the four going missing returns False and the module reads as
    ungated, which is the point: this recognizes a MECHANISM, and a mechanism
    that stopped working is not a mechanism.
    """
    wipers = _routing_wipers(path)
    dotted = _dotted_module_name(path)
    if not wipers or dotted is None:
        return False
    text = CONFTEST.read_text(encoding="utf-8") if conftest_source is None else conftest_source
    body = _preflight_body(text)
    if not body:
        return False
    try:
        tree = ast.parse(body)
    except SyntaxError:
        return False
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == dotted:
            imported.update(alias.name for alias in node.names)
    called = imported & wipers
    if not called:
        return False
    classify_at = body.find("classify_isolation(")
    if classify_at == -1:
        return False
    for name in sorted(called):
        call_at = body.find(f"{name}(")
        if call_at == -1 or call_at < classify_at:
            return False
    return "FullWipeRefused" in body and "UsageError" in body


def _gating_mechanism(path: Path) -> str | None:
    """Which recognized mechanism gates this module's wipe, or None."""
    if _fixture_gated(path):
        return GATE_FIXTURE
    if _autouse_target_guard(path):
        return GATE_MODULE_SAFETY
    if _preflight_gated(path):
        return GATE_PREFLIGHT
    return None


class TestEveryWholeGraphWipeIsGated:
    """A ratchet: a new everything-wipe in the suite must route through a gate.

    The guard in clear_all is the real protection, but a test that trips it gets
    an error rather than a skip, which reads as a broken suite. So every module
    that performs an everything-wipe has to be gated by a mechanism that both
    proves the target is disposable AND turns a refusal into a clean message.

    THREE MECHANISMS ARE RECOGNIZED, and the difference matters to whoever adds
    the next one:

      * GATE_FIXTURE, `disposable_graph`, taken as a real function PARAMETER.
        The normal answer for a test module. It skips with the exact commands to
        stand an instance up.
      * GATE_MODULE_SAFETY, the module's own `autouse` fixture that consults
        `targets_production` and FAILS on a production target. For a module
        where every test wipes, so requesting the gate per test would be
        ceremony; stricter than the fixture, because it fails rather than skips.
      * GATE_PREFLIGHT, the session-start wipe in
        `tests/conftest.py::_preflight_isolated_graph`. A `pytest_sessionstart`
        hook cannot request a fixture, so the fixture answer is not available to
        it; it earns the same two properties another way, and `_preflight_gated`
        above checks all four of the facts that make that true rather than
        taking the module's word for it.

    NONE OF THE THREE IS A NAME. Each is a detector that reads the mechanism off
    the source and returns False the moment the mechanism decays, which is what
    the mutation controls below demonstrate. If you are adding a FOURTH, add it
    here with its own detector and its own controls. Adding a module name to
    EXPECTED_GATING without a detector would turn this ratchet into an
    allowlist, and an allowlist is how a guard stops guarding.
    """

    _WIPE_CALL = WIPE_CALL

    # Module name -> the mechanism that gates it. This pins BOTH halves: a new
    # wiping module changes the key set, and an existing one whose gate decays
    # changes its value. A bare set of names would have caught only the first.
    EXPECTED_GATING = {
        "test_db_category.py": GATE_FIXTURE,
        "test_graph_dump.py": GATE_FIXTURE,
        "test_suite_start_idempotence.py": GATE_MODULE_SAFETY,
        "_graph.py": GATE_PREFLIGHT,
    }

    def _modules_with_everything_wipes(self, root: Path | None = None) -> list[Path]:
        base = (REPO_ROOT / "tests") if root is None else root
        return [
            path
            for path in sorted(base.rglob("*.py"))
            if path.name != Path(__file__).name
            and self._WIPE_CALL in path.read_text(encoding="utf-8")
        ]

    def test_the_known_wiping_modules_and_their_gates_are_unchanged(self) -> None:
        found = {p.name: _gating_mechanism(p) for p in self._modules_with_everything_wipes()}
        assert found == self.EXPECTED_GATING, (
            "the population of everything-wipes in the suite, or the mechanism "
            "gating one of them, changed. A new wipe must be gated by the "
            "disposable_graph fixture (test modules) or by the session-start "
            "preflight, and this map updated. A None value means a module wipes "
            f"the whole graph with no recognized gate at all. Found: {found}"
        )

    def test_every_wiping_module_is_gated_by_a_recognized_mechanism(self) -> None:
        ungated = [
            path.relative_to(REPO_ROOT).as_posix()
            for path in self._modules_with_everything_wipes()
            if _gating_mechanism(path) is None
        ]
        assert not ungated, (
            "these modules perform a whole-graph wipe without a recognized gate "
            "(neither a disposable_graph parameter nor the session-start "
            f"preflight): {ungated}"
        )

    # --- Positive controls: prove the ratchet still bites -----------------------
    # A guard that recognizes more shapes is a guard closer to recognizing
    # everything, so the recognition is exercised against synthetic modules in a
    # tmp directory rather than against the live tree alone.

    def test_a_synthetic_ungated_wipe_is_still_caught(self, tmp_path: Path) -> None:
        """The control that matters: routing through clear_all is NOT gating.

        This planted module does everything `tests/_graph.py` does except be
        reached from the preflight, and it must still read as ungated. If it
        did not, the preflight arm would be recognizing "calls clear_all",
        which every wiping module already does, and the ratchet would be dead.
        """
        planted = tmp_path / "test_synthetic_ungated_wipe.py"
        planted.write_text(
            "async def wipe(conn):\n"
            f"    await conn.clear_all({WIPE_CALL})\n",
            encoding="utf-8",
        )

        found = self._modules_with_everything_wipes(root=tmp_path)

        assert [p.name for p in found] == ["test_synthetic_ungated_wipe.py"], (
            f"the scan did not find the planted everything-wipe at {planted}"
        )
        assert _gating_mechanism(planted) is None, (
            "a module that wipes the whole graph, gated by neither the "
            "disposable_graph fixture nor the session-start preflight, must be "
            "reported as ungated"
        )

    def test_a_synthetic_fixture_gated_wipe_is_recognized(self, tmp_path: Path) -> None:
        """The other direction: a correctly gated new module must NOT be a
        false positive, or the next author's fix would not clear the ratchet."""
        gated = tmp_path / "test_synthetic_gated_wipe.py"
        gated.write_text(
            "async def db(disposable_graph, conn):\n"
            f"    await conn.clear_all({WIPE_CALL})\n",
            encoding="utf-8",
        )

        assert _gating_mechanism(gated) == GATE_FIXTURE

    def test_naming_the_fixture_without_taking_it_is_not_recognized(
        self, tmp_path: Path
    ) -> None:
        """Mentioning the gate is not passing through it."""
        talker = tmp_path / "test_synthetic_mentions_the_fixture.py"
        talker.write_text(
            "# this one really ought to use disposable_graph one day\n"
            "async def wipe(conn):\n"
            f"    await conn.clear_all({WIPE_CALL})\n",
            encoding="utf-8",
        )

        assert _gating_mechanism(talker) is None

    def test_a_raw_wipe_that_bypasses_clear_all_is_not_recognized(
        self, tmp_path: Path
    ) -> None:
        """First of the four preflight facts, checked on its own: the mechanism
        is `clear_all` doing the refusing, so a hand-rolled delete carrying the
        same keyword is not a routing wiper no matter who calls it."""
        raw = tmp_path / "test_synthetic_raw_wipe.py"
        raw.write_text(
            f"# {WIPE_CALL}\n"
            "async def wipe(session):\n"
            '    await session.run("MATCH (n) DETACH DELETE n")\n',
            encoding="utf-8",
        )

        assert _routing_wipers(raw) == set()
        assert _gating_mechanism(raw) is None

    _AUTOUSE_GUARD = (
        "@pytest.fixture(scope=\"module\", autouse=True)\n"
        "def _require_disposable(){body}\n"
        "async def wipe(conn):\n"
        f"    await conn.clear_all({WIPE_CALL})\n"
    )
    _REFUSING_BODY = (
        ":\n"
        "    if targets_production():\n"
        '        pytest.fail("refusing to wipe production")\n'
    )

    def test_an_autouse_target_guard_that_fails_is_recognized(
        self, tmp_path: Path
    ) -> None:
        guarded = tmp_path / "test_synthetic_autouse_guard.py"
        guarded.write_text(
            self._AUTOUSE_GUARD.format(body=self._REFUSING_BODY), encoding="utf-8"
        )

        assert _gating_mechanism(guarded) == GATE_MODULE_SAFETY

    def test_the_same_guard_is_not_recognized_when_it_is_not_autouse(
        self, tmp_path: Path
    ) -> None:
        """The autouse flag is the whole difference between a guard that covers
        every test and a guard that covers the ones that remembered to ask."""
        optional = tmp_path / "test_synthetic_optional_guard.py"
        optional.write_text(
            self._AUTOUSE_GUARD.format(body=self._REFUSING_BODY).replace(
                ", autouse=True", ""
            ),
            encoding="utf-8",
        )

        assert _gating_mechanism(optional) is None

    def test_a_guard_that_only_skips_on_production_is_not_recognized(
        self, tmp_path: Path
    ) -> None:
        """A mass skip is cheap to produce and indistinguishable from a green
        run. Refusing to wipe production has to FAIL to count as a gate."""
        skipper = tmp_path / "test_synthetic_skipping_guard.py"
        skipper.write_text(
            self._AUTOUSE_GUARD.format(
                body=self._REFUSING_BODY.replace("pytest.fail(", "pytest.skip(")
            ),
            encoding="utf-8",
        )

        assert _gating_mechanism(skipper) is None

    def test_the_preflight_gate_is_conditional_on_classifying_first(self) -> None:
        """Third of the four facts, proved by mutation rather than by reading.

        The recognition must go RED if the preflight ever stops classifying the
        target before issuing the delete. Mutating the real conftest source in
        memory is what shows the detector observes that, instead of returning
        True for `tests/_graph.py` because of its name.
        """
        graph_module = REPO_ROOT / "tests" / "_graph.py"
        real = CONFTEST.read_text(encoding="utf-8")

        assert _preflight_gated(graph_module, conftest_source=real), (
            "the live preflight must be recognized as gating tests/_graph.py, "
            "or the mutation below proves nothing"
        )
        mutated = real.replace("classify_isolation(", "no_longer_classifying(")
        assert not _preflight_gated(graph_module, conftest_source=mutated), (
            "a preflight that wipes without classifying the target first must "
            "not count as a gate"
        )

        # The ORDER half of the same fact, mutated separately: classifying
        # somewhere in the function is not the property, classifying BEFORE the
        # delete is. This hoists the wipe above the classifier and leaves both
        # calls present, so only the ordering changes.
        hoisted = real.replace(
            "    uri = resolved_uri()", "    wipe_everything()\n    uri = resolved_uri()", 1
        )
        assert not _preflight_gated(graph_module, conftest_source=hoisted), (
            "a preflight that issues the delete before the classifier has "
            "decided must not count as a gate, even though it classifies later"
        )

    def test_the_preflight_gate_is_conditional_on_actually_calling_the_wiper(self) -> None:
        """Second fact: the preflight has to import the wiping function FROM
        this module and call it. A preflight that no longer does is not gating
        anything, and `tests/_graph.py` would then be an ungated everything-wipe
        sitting in the tree with nobody watching it."""
        graph_module = REPO_ROOT / "tests" / "_graph.py"
        real = CONFTEST.read_text(encoding="utf-8")
        mutated = real.replace("        wipe_everything,\n", "")

        assert not _preflight_gated(graph_module, conftest_source=mutated), (
            "a module whose wiper the preflight does not import is not gated "
            "by the preflight"
        )

    def test_the_preflight_gate_is_conditional_on_the_clean_refusal(self) -> None:
        """Fourth fact: a refusal that reaches the operator as an INTERNALERROR
        traceback is the confusing failure this ratchet's docstring names, so it
        does not satisfy the gate either."""
        graph_module = REPO_ROOT / "tests" / "_graph.py"
        real = CONFTEST.read_text(encoding="utf-8")
        mutated = real.replace("FullWipeRefused", "SomeOtherError")

        assert not _preflight_gated(graph_module, conftest_source=mutated), (
            "a preflight that does not convert FullWipeRefused into a "
            "pytest.UsageError leaves a tripped gate looking like a crash"
        )

    def test_the_db_fixtures_take_disposable_graph_as_a_parameter(self) -> None:
        # Mentioning the name in a comment is not wiring. Parse the fixture
        # signatures and require the parameter.
        offenders = []
        for path in self._modules_with_everything_wipes():
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                if node.name != "db":
                    continue
                params = {a.arg for a in node.args.args}
                if "disposable_graph" not in params:
                    offenders.append(
                        f"{path.relative_to(REPO_ROOT).as_posix()}::{node.name}"
                    )
        assert not offenders, (
            f"these wiping `db` fixtures do not request disposable_graph: {offenders}"
        )

    def test_the_fixture_is_defined_in_conftest_not_per_file(self) -> None:
        # Placement matters: a copy-pasted per-file check is the thing the next
        # test author forgets.
        conftest = (REPO_ROOT / "tests" / "conftest.py").read_text(encoding="utf-8")
        assert "def disposable_graph(" in conftest

    def test_the_guard_lives_in_clear_all_not_only_in_the_fixture(self) -> None:
        # The fixture is convenience; the helper is the invariant. If this call
        # disappears, an ungated caller wipes production again.
        source = (
            REPO_ROOT / "writ" / "graph" / "db" / "maintenance_store.py"
        ).read_text(encoding="utf-8")
        assert "assert_full_wipe_allowed" in source


class TestDisposableGraphFixtureSkips:
    """The fixture skips (rather than errors) when no isolated instance exists."""

    def test_fixture_skips_with_actionable_message_when_marker_absent(
        self, monkeypatch
    ) -> None:
        monkeypatch.delenv(TEST_GRAPH_ENV_VAR, raising=False)
        monkeypatch.setattr(
            "writ.config.get_production_neo4j_uri", lambda path=None: PROD_URI
        )
        monkeypatch.setattr("writ.config.get_neo4j_uri", lambda path=None: PROD_URI)
        from tests.conftest import disposable_graph

        generator = _fixture_function(disposable_graph)()
        with pytest.raises(BaseException) as excinfo:
            next(generator)
        assert excinfo.typename == "Skipped", (
            f"the fixture must SKIP, not error; got {excinfo.typename}"
        )
        assert TEST_GRAPH_ENV_VAR in str(excinfo.value)
        assert "docker run" in str(excinfo.value)

    def test_fixture_yields_when_an_isolated_instance_is_configured(
        self, monkeypatch
    ) -> None:
        monkeypatch.setenv(TEST_GRAPH_ENV_VAR, TEST_GRAPH_OPT_IN)
        monkeypatch.setattr(
            "writ.config.get_production_neo4j_uri", lambda path=None: PROD_URI
        )
        monkeypatch.setattr(
            "writ.config.get_neo4j_uri", lambda path=None: DISPOSABLE_URI
        )
        from tests.conftest import disposable_graph

        generator = _fixture_function(disposable_graph)()
        next(generator)  # must not raise Skipped
        with pytest.raises(StopIteration):
            next(generator)
