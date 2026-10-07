"""Program item 6, workstream G: the TrustEvent record label (capabilities 1-5).

A TrustEvent is the attribution history behind a Rule's flat trust props: who
approved, disputed or verified a rule, when, how and from which session. It is a
RECORD label (like Decision and FeedbackBatch): preserved across corpus wipes,
never dumped, never reconciled away. Identity (os_login, git_name) lives ONLY on
these nodes, so a regenerated writ-corpus.cypher can never carry a person's name.

Capability map
  Cap 1 -- "TrustEvent" in RECORD_LABELS; default clear_all() and a corpus-only
           dump replay both leave every TrustEvent in place (real graph)
  Cap 2 -- get_all_nodes_for_dump returns no TrustEvent; the rendered dump holds
           neither the label nor any identity value (real graph)
  Cap 3 -- create_trust_event stamps provenance 'record' + source_origin
           'graph-authored'; a full reconcile deletes none and clears no prop (real graph)
  Cap 4 -- apply_constraints creates trustevent_event_id_project_unique and the
           trustevent_rule_id index; eight concurrent creates of one
           (event_id, project) leave one node (real graph)
  Cap 5 -- TrustEvent rejects a kind outside approval/dispute/verify; the Rule model
           has no approved_by and no write path sets one

Mocks cannot prove the constraint, the preserve-on-wipe or the concurrency claims
(ENF-SYS-005), so every real-graph test below runs on the isolated instance reached
through tests/_graph.py. Run under the shared flock; never with
WRIT_TEST_NO_ISOLATION.
"""

from __future__ import annotations

import asyncio
import re
from pathlib import Path

import pytest
import pytest_asyncio

from tests._graph import connection

PROJECT = "test-trust"
OS_LOGIN_SENTINEL = "zz-os-login-sentinel"
GIT_NAME_SENTINEL = "Zz Git Name Sentinel"


def _event(**overrides) -> dict:
    """Minimal well-formed TrustEvent dict (every required field supplied)."""
    defaults = {
        "event_id": "te-0000000000000000000000000000aaaa",
        "project": PROJECT,
        "rule_id": "TRUST-REC-001",
        "kind": "approval",
        "ts": "2026-10-06T00:00:00+00:00",
        "via": "review_promote",
        "session_id": "sess-trust-001",
        "os_login": OS_LOGIN_SENTINEL,
        "git_name": GIT_NAME_SENTINEL,
        "note": "",
    }
    return {**defaults, **overrides}


def _rule_md(rule_id: str) -> str:
    return f"""<!-- RULE START: {rule_id} -->
## Rule {rule_id}

**Domain**: security
**Severity**: Medium
**Scope**: Component

### Trigger
When a thing happens.

### Statement
A statement for {rule_id}.

### Violation
```python
x = 1
```

### Pass
```python
y = 2
```

### Enforcement
Code review.

### Rationale
A rationale for {rule_id}.

<!-- RULE END: {rule_id} -->
"""


def _make_bible(tmp: Path, rule_ids: list[str]) -> Path:
    d = tmp / "bible"
    d.mkdir(parents=True, exist_ok=True)
    (d / "rules.md").write_text("\n".join(_rule_md(r) for r in rule_ids), encoding="utf-8")
    return d


async def _reachable_or_skip(conn) -> None:
    try:
        async with conn._driver.session(database=conn._database) as s:
            await (await s.run("RETURN 1 AS ok")).consume()
    except Exception:
        await conn.close()
        pytest.skip("isolated test Neo4j (bolt://localhost:7688) unreachable; run "
                    "`bash scripts/test-graph.sh up`")


@pytest_asyncio.fixture()
async def db():
    """A connection to the isolated graph with the test project scope wiped around it."""
    conn = connection()
    await _reachable_or_skip(conn)
    await conn.clear_project(PROJECT)
    yield conn
    await conn.clear_project(PROJECT)
    await conn.close()


@pytest_asyncio.fixture()
async def wipe_db(disposable_graph):
    """A corpus-wiped graph, for the clear_all and replay claims only.

    Uses the record-preserving default clear_all (never an empty preserve set), so the
    TrustEvents each test creates are removed by project afterwards."""
    conn = connection()
    await _reachable_or_skip(conn)
    await conn.clear_all()
    yield conn
    await conn.clear_all()
    for project in ("writ", "other-proj", PROJECT):
        await conn.clear_project(project)
    await conn.close()


@pytest.fixture(scope="module", autouse=True)
def _restore_corpus_after_module():
    """The wipe tests leave the shared graph empty; put the corpus back for later modules."""
    yield
    from tests._corpus import ensure_corpus

    ensure_corpus()


async def _count_events(conn, event_id: str | None = None) -> int:
    where = "WHERE n.event_id = $eid" if event_id else ""
    rows = await conn._run(f"MATCH (n:TrustEvent) {where} RETURN count(n) AS c", eid=event_id)
    return [r["c"] for r in rows][0]


async def _event_props(conn, event_id: str) -> dict | None:
    rows = await conn._run(
        "MATCH (n:TrustEvent {event_id: $eid}) RETURN properties(n) AS p", eid=event_id)
    rows = list(rows)
    return dict(rows[0]["p"]) if rows else None


# ---------------------------------------------------------------------------
# Capability 5 (pure): the model and the identity-stays-off-Rule decision
# ---------------------------------------------------------------------------


class TestTrustEventModel:
    def test_trust_event_kinds_are_exactly_approval_dispute_verify(self) -> None:
        from writ.graph.schema import TRUST_EVENT_KINDS

        assert set(TRUST_EVENT_KINDS) == {"approval", "dispute", "verify"}

    @pytest.mark.parametrize("kind", ["approval", "dispute", "verify"])
    def test_each_valid_kind_is_accepted(self, kind: str) -> None:
        from writ.graph.schema import TrustEvent

        assert TrustEvent(**_event(kind=kind)).kind == kind

    @pytest.mark.parametrize("kind", ["edit", "approve", "", "APPROVAL", "reject"])
    def test_a_kind_outside_the_vocabulary_is_rejected(self, kind: str) -> None:
        from pydantic import ValidationError

        from writ.graph.schema import TrustEvent

        with pytest.raises(ValidationError) as excinfo:
            TrustEvent(**_event(kind=kind))
        assert "kind" in str(excinfo.value)

    def test_the_note_defaults_to_an_empty_string(self) -> None:
        from writ.graph.schema import TrustEvent

        data = _event()
        data.pop("note")
        assert TrustEvent(**data).note == ""

    def test_the_record_stamps_default_to_record_and_graph_authored(self) -> None:
        from writ.graph.schema import TrustEvent

        model = TrustEvent(**_event())
        assert model.provenance == "record"
        assert model.source_origin == "graph-authored"

    def test_identity_fields_are_carried_on_the_event(self) -> None:
        from writ.graph.schema import TrustEvent

        model = TrustEvent(**_event())
        assert model.os_login == OS_LOGIN_SENTINEL
        assert model.git_name == GIT_NAME_SENTINEL
        assert model.session_id == "sess-trust-001"
        assert model.via == "review_promote"

    def test_trust_event_is_a_record_not_a_retrieval_or_ingest_node(self) -> None:
        # Outside NODE_TYPE_MODELS and NODE_ID_FIELDS, so no retrieval, ingest dispatch
        # or parity path can see it (the Decision/FeedbackBatch precedent).
        from writ.graph.schema import (
            NODE_ID_FIELDS,
            NODE_TYPE_MODELS,
            RETRIEVABLE_NODE_TYPES,
            TrustEvent,
        )

        assert TrustEvent not in NODE_TYPE_MODELS.values()
        assert "TrustEvent" not in NODE_TYPE_MODELS
        assert "TrustEvent" not in NODE_ID_FIELDS
        assert "TrustEvent" not in {getattr(t, "value", t) for t in RETRIEVABLE_NODE_TYPES}


class TestRuleCarriesNoApprover:
    def test_the_rule_model_has_no_approved_by_field(self) -> None:
        from writ.graph.schema import Rule

        assert "approved_by" not in Rule.model_fields

    def test_no_identity_field_is_declared_on_the_rule_model(self) -> None:
        from writ.graph.schema import Rule

        leaks = {f for f in Rule.model_fields if f in {"os_login", "git_name", "approver"}}
        assert not leaks, f"identity belongs on TrustEvent only, found on Rule: {leaks}"

    def test_the_rule_model_declares_the_nine_trust_props(self) -> None:
        # Four authored plus five graph-only, all with defaults so an old rule still validates.
        from writ.graph.schema import Rule

        for name in ("layer", "basis", "deliberate", "verify_interval_days", "approved_at",
                     "approval_via", "last_verified", "disputed", "superseded"):
            assert name in Rule.model_fields, f"Rule must declare {name}"
            assert not Rule.model_fields[name].is_required(), f"{name} must have a default"

    def test_the_graph_only_set_is_the_five_props_and_is_runtime_exempt(self) -> None:
        from writ.graph.schema import (
            MANAGED_PROP_NAMES,
            RUNTIME_EXEMPT_PROPS,
            TRUST_GRAPH_ONLY_PROPS,
        )

        five = {"approved_at", "approval_via", "last_verified", "disputed", "superseded"}
        assert set(TRUST_GRAPH_ONLY_PROPS) == five
        assert five <= set(RUNTIME_EXEMPT_PROPS)
        assert not (five & set(MANAGED_PROP_NAMES))

    def test_no_python_source_under_writ_names_approved_by(self) -> None:
        # No write path may set an approver on a Rule: the name appears nowhere.
        root = Path(__file__).resolve().parent.parent / "writ"
        hits = [
            str(p.relative_to(root))
            for p in root.rglob("*.py")
            if re.search(r"\bapproved_by\b", p.read_text(encoding="utf-8"))
        ]
        assert hits == [], f"approved_by must not exist in writ/: {hits}"

    @pytest.mark.asyncio
    async def test_set_rule_trust_props_refuses_an_approver_key(self, db) -> None:
        await db.create_rule(_rule_dict("TRUST-NOAPPR-001"))
        with pytest.raises(ValueError):
            await db.set_rule_trust_props("TRUST-NOAPPR-001", {"approved_by": "someone"})
        rows = list(await db._run(
            "MATCH (r:Rule {rule_id: 'TRUST-NOAPPR-001'}) RETURN r.approved_by AS v"))
        assert rows[0]["v"] is None

    @pytest.mark.asyncio
    async def test_set_rule_trust_props_refuses_the_derived_superseded_flag(self, db) -> None:
        await db.create_rule(_rule_dict("TRUST-NOSUP-001"))
        with pytest.raises(ValueError):
            await db.set_rule_trust_props("TRUST-NOSUP-001", {"superseded": True})

    @pytest.mark.asyncio
    async def test_set_rule_trust_props_writes_allowlisted_keys_and_returns_the_project(
        self, db
    ) -> None:
        await db.create_rule(_rule_dict("TRUST-OKPROPS-001"))
        project = await db.set_rule_trust_props(
            "TRUST-OKPROPS-001",
            {"disputed": True, "last_verified": "2026-10-06", "approval_via": "review_promote"},
        )
        assert project == PROJECT
        rows = list(await db._run(
            "MATCH (r:Rule {rule_id: 'TRUST-OKPROPS-001'}) "
            "RETURN r.disputed AS d, r.last_verified AS lv, r.approval_via AS via"))
        assert rows[0]["d"] is True
        assert rows[0]["lv"] == "2026-10-06"
        assert rows[0]["via"] == "review_promote"

    @pytest.mark.asyncio
    async def test_set_rule_trust_props_returns_none_for_a_missing_rule(self, db) -> None:
        assert await db.set_rule_trust_props("TRUST-DOES-NOT-EXIST-001", {"disputed": True}) is None


def _rule_dict(rule_id: str, project: str = PROJECT) -> dict:
    return {
        "rule_id": rule_id, "domain": "Testing", "severity": "high", "scope": "slice",
        "trigger": "t", "statement": "s", "violation": "v", "pass_example": "p",
        "enforcement": "e", "rationale": "r", "last_validated": "2026-03-15",
        "project": project,
    }


# ---------------------------------------------------------------------------
# Capability 1 (real graph): preserved on clear_all and on a corpus-only replay
# ---------------------------------------------------------------------------


class TestTrustEventIsARecordLabel:
    def test_trust_event_is_in_record_labels(self) -> None:
        from writ.graph.db._common import RECORD_LABELS

        assert "TrustEvent" in RECORD_LABELS

    def test_record_labels_is_the_only_list_that_spells_the_label(self) -> None:
        # The preserve, import-preserve and dump-exclusion behaviors all derive from
        # RECORD_LABELS; a second hand-kept enumeration in production code would drift.
        root = Path(__file__).resolve().parent.parent / "writ"
        spelled = [
            str(p.relative_to(root))
            for p in root.rglob("*.py")
            if '"TrustEvent"' in p.read_text(encoding="utf-8")
            or "'TrustEvent'" in p.read_text(encoding="utf-8")
        ]
        assert set(spelled) <= {"graph/db/_common.py"}, spelled

    @pytest.mark.asyncio
    async def test_a_default_clear_all_leaves_every_trust_event(self, wipe_db) -> None:
        await wipe_db.create_rule(_rule_dict("TRUST-WIPE-001", project="writ"))
        await wipe_db.create_trust_event(**_event(event_id="te-wipe-1", project="writ"))
        await wipe_db.create_trust_event(**_event(event_id="te-wipe-2", project="other-proj"))
        await wipe_db.clear_all()
        assert await wipe_db.count_rules() == 0, "the corpus itself must still be wiped"
        assert await _count_events(wipe_db) == 2, "clear_all deleted a TrustEvent"

    @pytest.mark.asyncio
    async def test_a_corpus_only_dump_replay_leaves_every_trust_event(self, wipe_db) -> None:
        from writ.graph.dump import import_cypher_dump, render_cypher_dump

        await wipe_db.create_trust_event(**_event(event_id="te-replay-1"))
        script = render_cypher_dump(
            [{"id": "R-CORPUS-TRUST-1", "label": "Rule",
              "props": {"rule_id": "R-CORPUS-TRUST-1", "statement": "s"}}], [])
        await import_cypher_dump(wipe_db, script)
        assert await _count_events(wipe_db, "te-replay-1") == 1, (
            "a corpus-only replay deleted a TrustEvent; it must preserve record labels "
            "absent from the incoming dump")
        props = await _event_props(wipe_db, "te-replay-1")
        assert props["os_login"] == OS_LOGIN_SENTINEL, "replay must not rewrite the event"


# ---------------------------------------------------------------------------
# Capability 2 (real graph): never in the dump, no identity in the rendered text
# ---------------------------------------------------------------------------


class TestTrustEventNeverShipsInTheDump:
    @pytest.mark.asyncio
    async def test_get_all_nodes_for_dump_returns_no_trust_event(self, db) -> None:
        await db.create_rule(_rule_dict("TRUST-DUMP-001"))
        await db.create_trust_event(**_event(event_id="te-dump-1", rule_id="TRUST-DUMP-001"))
        nodes = await db.get_all_nodes_for_dump()
        assert "TrustEvent" not in {n["label"] for n in nodes}
        assert not [n for n in nodes if n["id"] is None], "no null-id row may reach the render"
        assert "TRUST-DUMP-001" in {n["id"] for n in nodes}, "the Rule itself must still dump"

    @pytest.mark.asyncio
    async def test_the_rendered_dump_holds_no_label_and_no_identity_value(self, db) -> None:
        from writ.graph.dump import render_cypher_dump

        await db.create_rule(_rule_dict("TRUST-DUMP-002"))
        await db.create_trust_event(
            **_event(event_id="te-dump-2", rule_id="TRUST-DUMP-002", note="zz-note-sentinel"))
        script = render_cypher_dump(
            await db.get_all_nodes_for_dump(), await db.get_all_edges_cross_type())
        assert "TrustEvent" not in script
        assert OS_LOGIN_SENTINEL not in script
        assert GIT_NAME_SENTINEL not in script
        assert "zz-note-sentinel" not in script
        assert "te-dump-2" not in script

    @pytest.mark.asyncio
    async def test_a_rule_that_was_approved_dumps_no_person(self, db) -> None:
        # The Rule's own props ship in the dump, so identity must never be copied onto it.
        from writ.graph.dump import render_cypher_dump

        await db.create_rule(_rule_dict("TRUST-DUMP-003"))
        await db.set_rule_trust_props(
            "TRUST-DUMP-003", {"approved_at": "2026-10-06T00:00:00+00:00",
                               "approval_via": "review_promote", "last_verified": "2026-10-06"})
        await db.create_trust_event(**_event(event_id="te-dump-3", rule_id="TRUST-DUMP-003"))
        script = render_cypher_dump(
            await db.get_all_nodes_for_dump(), await db.get_all_edges_cross_type())
        assert "TRUST-DUMP-003" in script
        assert OS_LOGIN_SENTINEL not in script and GIT_NAME_SENTINEL not in script


# ---------------------------------------------------------------------------
# Capability 3 (real graph): record stamps, and reconcile leaves the record alone
# ---------------------------------------------------------------------------


class TestCreateTrustEvent:
    @pytest.mark.asyncio
    async def test_create_trust_event_returns_the_event_id(self, db) -> None:
        assert await db.create_trust_event(**_event(event_id="te-ret-1")) == "te-ret-1"

    @pytest.mark.asyncio
    async def test_stored_provenance_is_record_and_source_origin_graph_authored(self, db) -> None:
        await db.create_trust_event(**_event(event_id="te-stamp-1"))
        props = await _event_props(db, "te-stamp-1")
        assert props["provenance"] == "record"
        assert props["source_origin"] == "graph-authored"

    @pytest.mark.asyncio
    async def test_every_field_is_stored_on_the_node(self, db) -> None:
        await db.create_trust_event(
            **_event(event_id="te-fields-1", kind="dispute", via="review_dispute",
                     note="the claim is out of date"))
        props = await _event_props(db, "te-fields-1")
        assert props["project"] == PROJECT
        assert props["rule_id"] == "TRUST-REC-001"
        assert props["kind"] == "dispute"
        assert props["via"] == "review_dispute"
        assert props["session_id"] == "sess-trust-001"
        assert props["os_login"] == OS_LOGIN_SENTINEL
        assert props["git_name"] == GIT_NAME_SENTINEL
        assert props["note"] == "the claim is out of date"
        assert props["ts"] == "2026-10-06T00:00:00+00:00"

    @pytest.mark.asyncio
    async def test_the_same_event_id_is_idempotent(self, db) -> None:
        await db.create_trust_event(**_event(event_id="te-idem-1"))
        await db.create_trust_event(**_event(event_id="te-idem-1"))
        assert await _count_events(db, "te-idem-1") == 1

    @pytest.mark.asyncio
    async def test_the_same_event_id_in_two_projects_is_two_nodes(self, db) -> None:
        await db.create_trust_event(**_event(event_id="te-proj-1"))
        await db.create_trust_event(**_event(event_id="te-proj-1", project=PROJECT + "-b"))
        try:
            assert await _count_events(db, "te-proj-1") == 2
        finally:
            await db.clear_project(PROJECT + "-b")

    @pytest.mark.asyncio
    async def test_an_invalid_kind_writes_nothing(self, db) -> None:
        from pydantic import ValidationError

        with pytest.raises(ValidationError):
            await db.create_trust_event(**_event(event_id="te-bad-1", kind="edit"))
        assert await _count_events(db, "te-bad-1") == 0

    @pytest.mark.asyncio
    async def test_identity_is_never_written_to_the_rule_node(self, db) -> None:
        await db.create_rule(_rule_dict("TRUST-LOCAL-001"))
        await db.create_trust_event(**_event(event_id="te-local-1", rule_id="TRUST-LOCAL-001"))
        rows = list(await db._run(
            "MATCH (r:Rule {rule_id: 'TRUST-LOCAL-001'}) RETURN properties(r) AS p"))
        values = {str(v) for v in dict(rows[0]["p"]).values()}
        assert OS_LOGIN_SENTINEL not in values and GIT_NAME_SENTINEL not in values


class TestTrustEventSurvivesReconcile:
    @pytest.mark.asyncio
    async def test_a_full_reconcile_deletes_no_trust_event_and_clears_no_prop(
        self, db, tmp_path
    ) -> None:
        from writ.graph.methodology_ingest import ingest_path, reconcile

        bible = _make_bible(tmp_path, ["TRUST-RECON-001", "TRUST-RECON-002"])
        await ingest_path(bible, db, project=PROJECT)
        # A TrustEvent for a rule the bible declares, and one whose rule is gone: neither
        # is in the oracle, and neither may be deleted.
        await db.create_trust_event(**_event(event_id="te-recon-1", rule_id="TRUST-RECON-001"))
        await db.create_trust_event(**_event(event_id="te-recon-2", rule_id="TRUST-GONE-001"))
        before = {e: await _event_props(db, e) for e in ("te-recon-1", "te-recon-2")}

        result = await reconcile(bible, db, project=PROJECT)

        assert "te-recon-1" not in result["deleted_nodes"]
        assert "te-recon-2" not in result["deleted_nodes"]
        for event_id, props in before.items():
            assert await _event_props(db, event_id) == props, (
                f"reconcile changed or cleared a prop on {event_id}")
        flat = {str(k) for k in result["cleared_props"]}
        assert not {k for k in flat if k.startswith("te-")}, result["cleared_props"]


# ---------------------------------------------------------------------------
# Capability 4 (real graph): the constraint, the index, and concurrent MERGE
# ---------------------------------------------------------------------------


class TestTrustEventSchema:
    @pytest.mark.asyncio
    async def test_apply_constraints_creates_the_unique_constraint_and_the_index(self, db) -> None:
        await db.apply_constraints()
        constraint_names = {c.get("name") for c in await db.list_constraints()}
        assert "trustevent_event_id_project_unique" in constraint_names, constraint_names
        index_names = {i.get("name") for i in await db.list_indexes()}
        assert "trustevent_rule_id" in index_names, index_names

    @pytest.mark.asyncio
    async def test_apply_constraints_is_idempotent_with_the_new_statements(self, db) -> None:
        await db.apply_constraints()
        failed = await db.apply_constraints()
        assert not failed, f"a second apply_constraints reported failures: {failed}"

    @pytest.mark.asyncio
    async def test_the_constraint_is_keyed_on_event_id_and_project(self, db) -> None:
        await db.apply_constraints()
        rows = [c for c in await db.list_constraints()
                if c.get("name") == "trustevent_event_id_project_unique"]
        assert rows, "constraint missing"
        text = str(rows[0])
        assert "event_id" in text and "project" in text, text


class TestConcurrentCreateTrustEvent:
    @pytest.mark.asyncio
    async def test_eight_concurrent_creates_of_one_key_leave_one_node(self, db) -> None:
        await db.apply_constraints()
        data = _event(event_id="te-conc-1")
        results = await asyncio.gather(
            *[db.create_trust_event(**data) for _ in range(8)], return_exceptions=True)
        errors = [r for r in results if isinstance(r, BaseException)]
        assert not errors, f"a concurrent MERGE raised instead of converging: {errors}"
        assert await _count_events(db, "te-conc-1") == 1, (
            "concurrent MERGEs forked duplicate TrustEvent nodes")

    @pytest.mark.asyncio
    async def test_concurrent_creates_of_distinct_keys_each_land_once(self, db) -> None:
        await db.apply_constraints()
        await asyncio.gather(*[
            db.create_trust_event(**_event(event_id=f"te-many-{i}")) for i in range(8)
        ])
        rows = list(await db._run(
            "MATCH (n:TrustEvent {project: $p}) WHERE n.event_id STARTS WITH 'te-many-' "
            "RETURN count(n) AS c", p=PROJECT))
        assert rows[0]["c"] == 8
