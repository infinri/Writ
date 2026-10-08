"""Program item 5, workstream P, phase 1: the collector registry and the allowlist collapse.

A collector is one retrieval source returning units plus a change stamp. The registry is
validated against the schema allowlists at import and fails closed:

  * validate_collectors accepts RANKED_COLLECTORS and raises CollectorRegistryError for an
    unknown label, a doctrine collector naming a non-doctrine label, a record collector naming
    a doctrine, non-record or NODE_ID_FIELDS label, an empty or repeated name, an empty label
    tuple and a label claimed twice (23)
  * writ.retrieval.pipeline validates RANKED_COLLECTORS at module level with a raising guard,
    and the registry's labels are exactly DOCTRINE_NODE_TYPES, Rule first then
    RANKED_METHODOLOGY_LABELS in order (24)
  * _load_candidates returns the same candidates in the same order with the same metadata
    through the collectors (25; the statement count on the real graph is in
    tests/test_item5_p_graph.py)
  * Collected.stamp is fingerprint_digest(units), computed only when read (26)
  * NODE_TYPE_MODELS and NODE_ID_FIELDS derive from one table, RETRIEVABLE_NODE_TYPES is
    derived, the id-coalesce strings and id-field resolvers are byte-identical, and the record
    id map is guarded against RECORD_LABELS (27 to 30)
  * DOCTRINE_NODE_TYPES and RECORD_LABELS stay written out as literals (31)

Workstream D edits this file after P lands, only where it pins RECORD_ID_FIELDS or the
validated collector set exactly; record-label assertions here are therefore written as
"the original labels are present with these ids" and "keys equal RECORD_LABELS" so adding
Document and Chunk to both maps keeps them true.

No graph: the loader runs against a recording fake driver.

RED today: writ/retrieval/collectors.py does not exist; the schema, _common and pipeline
modules have none of the derivations or guards.

Run: flock <lock> .venv/bin/python3 -m pytest tests/test_collector_registry.py
"""
from __future__ import annotations

import ast
import asyncio
import hashlib
import importlib
import json
import re
import sys
import types
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from tests.test_db_record_write_helper import (
    _commit_data,
    _decision_data,
    _filechange_data,
)
from tests.test_trust_records import _event
from writ.graph import schema
from writ.graph.db.record_store import RecordStoreMixin
from writ.graph.db import _common

REPO = Path(__file__).resolve().parent.parent


def _open_question_data(**overrides) -> dict:
    """Minimal well-formed OpenQuestion dict (program item 7a)."""
    defaults = {
        "question_id": "OQ-0123456789", "project": "test-oq", "question": "Is the cache safe?",
        "status": "open", "opened_at": "2026-10-07T00:00:00+00:00",
    }
    return {**defaults, **overrides}

ORIGINAL_RECORD_IDS = {
    "Memory": "name", "Decision": "decision_id", "FileChange": "change_id",
    "Commit": "commit_hash", "Project": "name", "FeedbackBatch": "batch_id",
    "TrustEvent": "event_id", "OpenQuestion": "question_id",
}
DOCTRINE = frozenset({"Rule", "Skill", "Playbook", "Technique", "AntiPattern", "ForbiddenResponse"})
NODE_TABLE = [
    ("Rule", "rule_id"), ("Abstraction", "abstraction_id"), ("Category", "category_id"),
    ("Skill", "skill_id"), ("Playbook", "playbook_id"), ("Technique", "technique_id"),
    ("AntiPattern", "antipattern_id"), ("ForbiddenResponse", "forbidden_id"),
    ("Phase", "phase_id"), ("Rationalization", "rationalization_id"),
    ("PressureScenario", "scenario_id"), ("WorkedExample", "example_id"),
    ("SubagentRole", "role_id"),
]
GOLDEN_COALESCE = (
    "coalesce({v}.abstraction_id, {v}.antipattern_id, {v}.category_id, {v}.example_id, "
    "{v}.forbidden_id, {v}.phase_id, {v}.playbook_id, {v}.rationalization_id, {v}.role_id, "
    "{v}.rule_id, {v}.scenario_id, {v}.skill_id, {v}.technique_id)"
)
GOLDEN_ID_OR = (
    "n.abstraction_id = $p OR n.antipattern_id = $p OR n.category_id = $p OR "
    "n.example_id = $p OR n.forbidden_id = $p OR n.phase_id = $p OR n.playbook_id = $p OR "
    "n.rationalization_id = $p OR n.role_id = $p OR n.rule_id = $p OR n.scenario_id = $p OR "
    "n.skill_id = $p OR n.technique_id = $p"
)


def _collectors():
    return importlib.import_module("writ.retrieval.collectors")


def _pipeline_mod():
    return importlib.import_module("writ.retrieval.pipeline")


async def _no_units(db):
    return []


def _collector(**overrides):
    c = _collectors()
    fields = {"name": "probe", "labels": ("Rule",), "scope": "doctrine", "collect": _no_units}
    fields.update(overrides)
    return c.Collector(**fields)


# ===========================================================================
# 23. validate_collectors
# ===========================================================================


class TestValidateCollectors:
    def test_the_error_is_a_value_error(self):
        assert issubclass(_collectors().CollectorRegistryError, ValueError)

    def test_the_ranked_registry_is_accepted(self):
        assert _collectors().validate_collectors(_pipeline_mod().RANKED_COLLECTORS) is None

    def test_a_well_formed_doctrine_collector_is_accepted(self):
        _collectors().validate_collectors([_collector()])

    def test_a_well_formed_record_collector_is_accepted(self):
        _collectors().validate_collectors([_collector(name="decisions", labels=("Decision",), scope="record")])

    def test_two_collectors_with_disjoint_labels_are_accepted(self):
        _collectors().validate_collectors([
            _collector(name="a", labels=("Rule",)), _collector(name="b", labels=("Skill", "Playbook")),
        ])

    @pytest.mark.parametrize("label", ["Nope", "rule", "", "Chunkk"])
    def test_an_unknown_label_fails_closed(self, label):
        with pytest.raises(_collectors().CollectorRegistryError):
            _collectors().validate_collectors([_collector(labels=(label,))])

    @pytest.mark.parametrize("label", ["Abstraction", "Category", "Phase", "SubagentRole", "Decision", "Memory"])
    def test_a_doctrine_collector_naming_a_non_doctrine_label_is_rejected(self, label):
        with pytest.raises(_collectors().CollectorRegistryError):
            _collectors().validate_collectors([_collector(labels=(label,), scope="doctrine")])

    @pytest.mark.parametrize("label", sorted(DOCTRINE))
    def test_a_record_collector_naming_a_doctrine_label_is_rejected(self, label):
        with pytest.raises(_collectors().CollectorRegistryError):
            _collectors().validate_collectors([_collector(labels=(label,), scope="record")])

    @pytest.mark.parametrize("label", ["Abstraction", "Category", "Phase", "WorkedExample", "Nope"])
    def test_a_record_collector_naming_a_non_record_label_is_rejected(self, label):
        with pytest.raises(_collectors().CollectorRegistryError):
            _collectors().validate_collectors([_collector(labels=(label,), scope="record")])

    def test_a_record_collector_naming_a_node_id_fields_label_is_rejected_even_when_listed_as_a_record(self, monkeypatch):
        monkeypatch.setattr(_common, "RECORD_LABELS", _common.RECORD_LABELS | {"Abstraction"})
        with pytest.raises(_collectors().CollectorRegistryError):
            _collectors().validate_collectors([_collector(labels=("Abstraction",), scope="record")])

    @pytest.mark.parametrize("name", ["", None])
    def test_an_empty_name_is_rejected(self, name):
        with pytest.raises(_collectors().CollectorRegistryError):
            _collectors().validate_collectors([_collector(name=name)])

    def test_a_repeated_name_is_rejected(self):
        with pytest.raises(_collectors().CollectorRegistryError):
            _collectors().validate_collectors([
                _collector(name="same", labels=("Rule",)), _collector(name="same", labels=("Skill",)),
            ])

    def test_an_empty_label_tuple_is_rejected(self):
        with pytest.raises(_collectors().CollectorRegistryError):
            _collectors().validate_collectors([_collector(labels=())])

    def test_a_label_claimed_by_two_collectors_is_rejected(self):
        with pytest.raises(_collectors().CollectorRegistryError):
            _collectors().validate_collectors([
                _collector(name="a", labels=("Rule", "Skill")), _collector(name="b", labels=("Skill",)),
            ])

    def test_a_label_repeated_inside_one_collector_is_rejected(self):
        with pytest.raises(_collectors().CollectorRegistryError):
            _collectors().validate_collectors([_collector(labels=("Rule", "Rule"))])

    def test_one_bad_collector_among_good_ones_fails_the_whole_registry(self):
        with pytest.raises(_collectors().CollectorRegistryError):
            _collectors().validate_collectors([
                _collector(name="a", labels=("Rule",)), _collector(name="b", labels=("Nope",)),
            ])

    def test_validation_does_not_mutate_its_input(self):
        collectors = [_collector(name="a", labels=("Rule",)), _collector(name="b", labels=("Skill",))]
        before = list(collectors)
        _collectors().validate_collectors(collectors)
        assert collectors == before


# ===========================================================================
# 24. The ranked registry
# ===========================================================================


def _module_level_calls_and_asserts(path: Path):
    tree = ast.parse(path.read_text(encoding="utf-8"))
    calls, asserts = [], []
    for node in tree.body:
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Call):
            calls.append(node.value)
        if isinstance(node, ast.Assert):
            asserts.append(node)
    return calls, asserts


class TestRankedRegistry:
    def test_the_labels_are_exactly_the_doctrine_set_rule_first_then_the_ranked_methodology_labels_in_order(self):
        from writ.retrieval.node_scope import DOCTRINE_NODE_TYPES

        labels = [label for c in _pipeline_mod().RANKED_COLLECTORS for label in c.labels]
        assert labels == ["Rule", *schema.RANKED_METHODOLOGY_LABELS]
        assert set(labels) == set(DOCTRINE_NODE_TYPES)
        assert len(labels) == len(set(labels))

    def test_there_are_two_doctrine_collectors_named_rules_and_methodology(self):
        collectors = _pipeline_mod().RANKED_COLLECTORS
        assert [(c.name, c.scope) for c in collectors] == [("rules", "doctrine"), ("methodology", "doctrine")]
        assert collectors[0].labels == ("Rule",)
        assert collectors[1].labels == tuple(schema.RANKED_METHODOLOGY_LABELS)

    def test_the_pipeline_module_validates_the_registry_at_module_level(self):
        calls, _asserts = _module_level_calls_and_asserts(REPO / "writ" / "retrieval" / "pipeline.py")
        # Workstream D widens the one call to `(*RANKED_COLLECTORS, DOCUMENT_COLLECTOR)`, so
        # the pin is: exactly one module-level validate_collectors call, and its argument is
        # RANKED_COLLECTORS itself or a tuple that spreads it.
        def _names_ranked(arg) -> bool:
            if getattr(arg, "id", "") == "RANKED_COLLECTORS":
                return True
            return isinstance(arg, ast.Tuple) and any(
                isinstance(e, ast.Starred) and getattr(e.value, "id", "") == "RANKED_COLLECTORS"
                for e in arg.elts)

        validating = [
            c for c in calls
            if getattr(c.func, "id", getattr(c.func, "attr", "")) == "validate_collectors"
            and c.args and _names_ranked(c.args[0])
        ]
        assert len(validating) == 1

    def test_the_guard_is_a_raise_not_an_assert(self):
        _calls, asserts = _module_level_calls_and_asserts(REPO / "writ" / "retrieval" / "pipeline.py")
        assert not [a for a in asserts if "RANKED_COLLECTORS" in ast.dump(a)]

    def test_a_corrupted_registry_stops_validation(self):
        bad = [*_pipeline_mod().RANKED_COLLECTORS[:1], _collector(name="rules", labels=("Skill",))]
        with pytest.raises(_collectors().CollectorRegistryError):
            _collectors().validate_collectors(bad)


# ===========================================================================
# 25. _load_candidates through the collectors
# ===========================================================================


class _FakeResult:
    def __init__(self, key, rows):
        self._key, self._rows = key, rows

    def __aiter__(self):
        return self._gen()

    async def _gen(self):
        for row in self._rows:
            yield {self._key: dict(row)}


class _FakeSession:
    def __init__(self, driver):
        self._driver = driver

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def run(self, query, **params):
        self._driver.seen.append(query)
        if "MATCH (r:Rule)" in query:
            return _FakeResult("r", self._driver.rules)
        label = re.search(r"MATCH \(n:(\w+)\)", query).group(1)
        return _FakeResult("n", self._driver.methodology.get(label, []))


class _FakeDriver:
    def __init__(self, rules, methodology):
        self.rules, self.methodology, self.seen = rules, methodology, []

    def session(self, database=None):
        return _FakeSession(self)


class _FakeDB:
    _database = "neo4j"

    def __init__(self, rules=(), methodology=None):
        self._driver = _FakeDriver(list(rules), methodology or {})


RULE_ROWS = [
    {"rule_id": "R-B", "trigger": "tb", "statement": "sb", "severity": "high"},
    {"rule_id": "R-A", "trigger": "ta", "statement": "sa"},
]
METHODOLOGY_ROWS = {
    "Skill": [
        {"skill_id": "SK-1", "trigger": "ts", "statement": "ss", "body": "b0"},
        {"trigger": "a node with no id is skipped", "statement": "x"},
    ],
    "AntiPattern": [{"antipattern_id": "AP-1", "statement": "ax", "named_in": "Book"}],
    "ForbiddenResponse": [{
        "forbidden_id": "FRB-1", "statement": "st",
        "forbidden_phrases": '["you are right", "absolutely"]', "what_to_say_instead": "say less",
    }],
}
EXPECTED_CANDIDATES = [
    {"rule_id": "R-B", "trigger": "tb", "statement": "sb", "severity": "high", "node_type": "Rule"},
    {"rule_id": "R-A", "trigger": "ta", "statement": "sa", "node_type": "Rule"},
    {"skill_id": "SK-1", "trigger": "ts", "statement": "ss", "body": "b0", "rule_id": "SK-1",
     "node_type": "Skill", "mandatory": False},
    {"antipattern_id": "AP-1", "statement": "ax", "named_in": "Book", "rule_id": "AP-1",
     "node_type": "AntiPattern", "mandatory": False, "body": "Book"},
    {"forbidden_id": "FRB-1", "statement": "st",
     "forbidden_phrases": '["you are right", "absolutely"]', "what_to_say_instead": "say less",
     "rule_id": "FRB-1", "node_type": "ForbiddenResponse", "mandatory": False,
     "body": "you are right absolutely say less"},
]


class TestLoadCandidatesThroughTheCollectors:
    def _load(self):
        db = _FakeDB(RULE_ROWS, METHODOLOGY_ROWS)
        candidates, metadata = asyncio.run(_pipeline_mod()._load_candidates(db))
        return db, candidates, metadata

    def test_the_candidates_are_the_rules_then_the_methodology_nodes_in_label_order(self):
        _db, candidates, _meta = self._load()
        assert candidates == EXPECTED_CANDIDATES

    def test_the_metadata_is_keyed_by_rule_id_and_holds_the_candidate_objects(self):
        _db, candidates, metadata = self._load()
        assert list(metadata) == [c["rule_id"] for c in candidates]
        for c in candidates:
            assert metadata[c["rule_id"]] is c

    def test_the_statements_are_the_rule_query_then_one_ordered_query_per_ranked_label(self):
        db, _c, _m = self._load()
        seen = db._driver.seen
        assert len(seen) == 6
        assert "MATCH (r:Rule)" in seen[0] and "ORDER BY r.rule_id" in seen[0]
        expected = [
            f"MATCH (n:{label}) RETURN n ORDER BY n.{schema.NODE_ID_FIELDS[label]}"
            for label in schema.RANKED_METHODOLOGY_LABELS
        ]
        assert seen[1:] == expected

    def test_the_rule_query_still_uses_the_single_source_inclusion_predicate(self):
        from writ.graph.predicates import RANKED_INCLUDE_WHERE

        db, _c, _m = self._load()
        assert " ".join(RANKED_INCLUDE_WHERE.split()) in " ".join(db._driver.seen[0].split())

    def test_an_empty_graph_yields_no_candidates_and_still_issues_six_statements(self):
        db = _FakeDB()
        candidates, metadata = asyncio.run(_pipeline_mod()._load_candidates(db))
        assert (candidates, metadata) == ([], {})
        assert len(db._driver.seen) == 6

    def test_each_collector_returns_its_half(self):
        db = _FakeDB(RULE_ROWS, METHODOLOGY_ROWS)
        rules, methodology = _pipeline_mod().RANKED_COLLECTORS
        assert [u["rule_id"] for u in asyncio.run(rules.run(db)).units] == ["R-B", "R-A"]
        assert [u["rule_id"] for u in asyncio.run(methodology.run(db)).units] == ["SK-1", "AP-1", "FRB-1"]


# ===========================================================================
# 26. Collected.stamp and fingerprint_digest
# ===========================================================================


class TestStamp:
    def _run(self, units, stamp_fn=None):
        async def collect(db):
            return units

        kwargs = {"stamp_fn": stamp_fn} if stamp_fn is not None else {}
        return asyncio.run(_collector(collect=collect, **kwargs).run(object()))

    def test_run_passes_the_database_to_the_collect_function(self):
        sentinel = object()
        collect = AsyncMock(return_value=[{"a": 1}])
        got = asyncio.run(_collector(collect=collect).run(sentinel))
        collect.assert_awaited_once_with(sentinel)
        assert got.units == [{"a": 1}]
        assert isinstance(got, _collectors().Collected)

    def test_the_stamp_equals_fingerprint_digest_of_the_units(self):
        units = [{"rule_id": "A", "statement": "s"}, {"rule_id": "B", "statement": "t"}]
        assert self._run(units).stamp == _collectors().fingerprint_digest(units)

    def test_the_stamp_changes_only_when_the_units_change(self):
        a = self._run([{"rule_id": "A", "statement": "s"}])
        same = self._run([{"statement": "s", "rule_id": "A"}])
        changed = self._run([{"rule_id": "A", "statement": "edited"}])
        assert a.stamp == same.stamp
        assert a.stamp != changed.stamp

    def test_the_stamp_is_not_computed_unless_read_and_is_computed_once(self):
        stamp_fn = MagicMock(return_value="custom-stamp")
        got = self._run([{"a": 1}], stamp_fn=stamp_fn)
        assert stamp_fn.call_count == 0
        assert got.stamp == "custom-stamp"
        assert got.stamp == "custom-stamp"
        assert stamp_fn.call_count == 1
        stamp_fn.assert_called_once_with([{"a": 1}])

    def test_running_the_ranked_collectors_computes_no_stamp(self):
        db = _FakeDB(RULE_ROWS, METHODOLOGY_ROWS)
        calls = []
        for c in _pipeline_mod().RANKED_COLLECTORS:
            spy = MagicMock(side_effect=lambda units: calls.append(units) or "x")
            asyncio.run(_collector(name=c.name, labels=c.labels, collect=c.collect, stamp_fn=spy).run(db))
        assert calls == []

    def test_fingerprint_digest_keeps_its_definition(self):
        value = {"b": [1, 2], "a": {"z": 1, "y": None}}
        expected = hashlib.sha256(json.dumps(value, sort_keys=True, default=str).encode("utf-8")).hexdigest()
        assert _collectors().fingerprint_digest(value) == expected

    def test_fingerprint_digest_is_still_importable_from_the_pipeline_module(self):
        assert _pipeline_mod().fingerprint_digest is _collectors().fingerprint_digest

    def test_pipeline_fingerprint_keys_and_values_are_unchanged(self):
        from writ.retrieval.traversal import AdjacencyCache

        pipeline = _pipeline_mod()
        candidates = [{"rule_id": "R-1", "trigger": "t", "statement": "s", "tags": "", "body": "",
                       "mandatory": False, "project": "writ"}]
        metadata = {c["rule_id"]: c for c in candidates}
        cache = AdjacencyCache()
        inputs = pipeline.PipelineInputs(
            candidates=candidates, rule_metadata=metadata, adjacency_cache=cache,
            abstractions=[], node_routes=None)
        fp = pipeline.pipeline_fingerprint(inputs)
        rule_ids, texts = pipeline._vector_corpus(candidates)
        digest = _collectors().fingerprint_digest
        assert fp["index_version"] == str(pipeline.RETRIEVAL_INDEX_VERSION)
        assert fp["bm25"] == pipeline._compute_bm25_hash(candidates)
        assert fp["hnsw"] == pipeline._compute_corpus_hash_from_text(rule_ids, texts)
        assert fp["metadata"] == digest(metadata)
        assert fp["edges"] == digest(cache.snapshot())
        assert fp["routes"] == digest(None)
        assert fp["abstractions"] == digest([])


# ===========================================================================
# 27, 28. The node-type table and the derived retrievable set
# ===========================================================================


def _exec_source_as(path: Path, name: str, transform=None):
    """Execute a module's source in a throwaway module object. A control for the import-time
    guards: they run on import, so the only way to test a failure is to run the source again."""
    source = path.read_text(encoding="utf-8")
    if transform is not None:
        source = transform(source)
    module = types.ModuleType(name)
    module.__file__ = str(path)
    sys.modules[name] = module
    try:
        exec(compile(source, str(path), "exec"), module.__dict__)
    finally:
        sys.modules.pop(name, None)
    return module


class TestNodeTypeTable:
    def test_the_models_and_id_fields_keep_todays_keys_values_and_order(self):
        assert list(schema.NODE_ID_FIELDS.items()) == NODE_TABLE
        assert list(schema.NODE_TYPE_MODELS) == [label for label, _ in NODE_TABLE]
        assert [m.value for m in schema.NodeType] == [label for label, _ in NODE_TABLE]

    def test_the_models_are_the_schema_classes(self):
        for label, _id in NODE_TABLE:
            model = schema.NODE_TYPE_MODELS[label]
            assert model is getattr(schema, label)
            assert _id in model.model_fields

    def test_ingest_reads_the_same_objects(self):
        from writ.graph import ingest

        assert ingest.NODE_TYPE_MODELS is schema.NODE_TYPE_MODELS
        assert ingest.NODE_ID_FIELDS is schema.NODE_ID_FIELDS

    def test_neither_registry_is_a_second_dict_literal(self):
        path = REPO / "writ" / "graph" / "schema.py"
        for name in ("NODE_TYPE_MODELS", "NODE_ID_FIELDS"):
            assert not isinstance(_assigned_value(path, name), ast.Dict), (
                f"{name} must derive from the one node-type table, not repeat the 13 labels"
            )

    def test_the_unmodified_schema_source_executes_cleanly(self):
        _exec_source_as(REPO / "writ" / "graph" / "schema.py", "writ.graph._probe_schema_control")

    def test_import_raises_value_error_when_nodetype_has_a_member_the_table_lacks(self):
        def add_member(source: str) -> str:
            marker = '    SUBAGENT_ROLE = "SubagentRole"\n'
            assert marker in source
            return source.replace(marker, marker + '    EXTRA_PROBE = "ExtraProbe"\n', 1)

        with pytest.raises(ValueError):
            _exec_source_as(REPO / "writ" / "graph" / "schema.py", "writ.graph._probe_schema_extra", add_member)

    def test_the_guard_is_not_an_assert(self):
        tree = ast.parse((REPO / "writ" / "graph" / "schema.py").read_text(encoding="utf-8"))
        module_asserts = [n for n in tree.body if isinstance(n, ast.Assert)]
        assert not [a for a in module_asserts if "NodeType" in ast.dump(a)]


class TestRetrievableNodeTypes:
    def test_it_holds_the_same_seven_nodetype_members(self):
        assert schema.RETRIEVABLE_NODE_TYPES == frozenset({
            schema.NodeType.RULE, schema.NodeType.ABSTRACTION, schema.NodeType.SKILL,
            schema.NodeType.PLAYBOOK, schema.NodeType.TECHNIQUE, schema.NodeType.ANTIPATTERN,
            schema.NodeType.FORBIDDEN_RESPONSE,
        })
        assert all(isinstance(m, schema.NodeType) for m in schema.RETRIEVABLE_NODE_TYPES)

    def test_it_is_derived_from_the_ranked_methodology_labels(self):
        derived = frozenset({
            schema.NodeType.RULE, schema.NodeType.ABSTRACTION,
            *(schema.NodeType(label) for label in schema.RANKED_METHODOLOGY_LABELS),
        })
        assert schema.RETRIEVABLE_NODE_TYPES == derived

    def test_the_source_defines_the_ranked_labels_before_the_retrievable_set(self):
        source = (REPO / "writ" / "graph" / "schema.py").read_text(encoding="utf-8")
        assert source.index("RANKED_METHODOLOGY_LABELS: tuple[str, ...] = (") < source.index(
            "RETRIEVABLE_NODE_TYPES = ")

    def test_the_set_does_not_spell_the_methodology_members_out_again(self):
        source = (REPO / "writ" / "graph" / "schema.py").read_text(encoding="utf-8")
        start = source.index("RETRIEVABLE_NODE_TYPES = ")
        block = source[start: source.index("\n\n", start)]
        assert "NodeType.SKILL" not in block and "NodeType.FORBIDDEN_RESPONSE" not in block


# ===========================================================================
# 29. The id-field strings and resolvers
# ===========================================================================


class TestIdFieldResolution:
    def test_the_coalesce_string_is_byte_identical(self):
        assert _common._GRAPH_ID_COALESCE == GOLDEN_COALESCE

    def test_the_or_match_string_is_byte_identical(self):
        assert _common._id_or_match("n", "p") == GOLDEN_ID_OR
        assert _common._id_or_match("x", "y") == GOLDEN_ID_OR.replace("n.", "x.").replace("$p", "$y")

    def test_both_strings_are_built_from_one_field_tuple(self):
        assert _common._GRAPH_ID_FIELDS == tuple(sorted(set(schema.NODE_ID_FIELDS.values())))

    @pytest.mark.parametrize("label,id_field", NODE_TABLE)
    def test_id_field_for_label_returns_the_registered_field(self, label, id_field):
        assert _common._id_field_for_label(label) == id_field

    @pytest.mark.parametrize("label", ["Nope", "Decision", "Memory", ""])
    def test_id_field_for_label_raises_key_error_for_an_unknown_label(self, label):
        with pytest.raises(KeyError):
            _common._id_field_for_label(label)

    @pytest.mark.parametrize("label,id_field", NODE_TABLE)
    def test_node_write_spec_resolves_the_same_id_field(self, label, id_field):
        spec = _common._node_write_spec(label, {id_field: "X-1", "project": "p"})
        assert spec[0] == label and spec[1] == id_field and spec[2] == "X-1" and spec[3] == "p"

    @pytest.mark.parametrize("label", ["Nope", "Decision", "Commit", ""])
    def test_node_write_spec_raises_the_same_error_for_an_unknown_label(self, label):
        with pytest.raises(ValueError, match=f"Unknown node_type: {label}"):
            _common._node_write_spec(label, {})

    def test_node_write_spec_raises_the_same_error_for_a_missing_id(self):
        with pytest.raises(ValueError, match="Rule data missing required rule_id"):
            _common._node_write_spec("Rule", {})
        with pytest.raises(ValueError, match="Skill data missing required skill_id"):
            _common._node_write_spec("Skill", {"rule_id": "X"})

    def test_the_methodology_comprehension_is_untouched(self):
        assert _common.METHODOLOGY_NODE_ID_FIELDS == {
            k: v for k, v in schema.NODE_ID_FIELDS.items() if k != "Rule"}


# ===========================================================================
# 30. The record id map
# ===========================================================================


class TestRecordIdFields:
    def test_the_keys_equal_the_record_labels(self):
        assert set(_common.RECORD_ID_FIELDS) == set(_common.RECORD_LABELS)

    def test_the_original_record_labels_keep_their_id_fields(self):
        for label, id_field in ORIGINAL_RECORD_IDS.items():
            assert _common.RECORD_ID_FIELDS[label] == id_field

    def test_no_record_label_is_in_the_node_registries(self):
        for label in _common.RECORD_LABELS:
            assert label not in schema.NODE_ID_FIELDS
            assert label not in schema.NODE_TYPE_MODELS

    def test_record_edge_endpoints_keep_their_content(self):
        assert _common._RECORD_EDGE_ENDPOINTS == {
            **schema.NODE_ID_FIELDS,
            "Decision": "decision_id", "FileChange": "change_id", "Commit": "commit_hash",
            "Project": "name", "OpenQuestion": "question_id",
        }

    def test_the_edge_endpoint_allowlist_is_written_out(self):
        tree = ast.parse((REPO / "writ" / "graph" / "db" / "_common.py").read_text(encoding="utf-8"))
        literals = [
            n for n in ast.walk(tree)
            if isinstance(n, ast.Assign) and any(getattr(t, "id", "") == "_RECORD_EDGE_ENDPOINT_LABELS"
                                                 for t in n.targets)
        ]
        assert len(literals) == 1
        assert isinstance(literals[0].value, ast.Tuple)
        assert [e.value for e in literals[0].value.elts] == [
            "Decision", "FileChange", "Commit", "Project", "OpenQuestion"]

    def test_the_unmodified_common_source_executes_cleanly(self):
        _exec_source_as(REPO / "writ" / "graph" / "db" / "_common.py", "writ.graph.db._probe_common_control")

    @pytest.mark.parametrize("registry", ["NODE_ID_FIELDS", "NODE_TYPE_MODELS"])
    def test_import_raises_when_a_record_label_appears_in_a_node_registry(self, monkeypatch, registry):
        monkeypatch.setitem(getattr(schema, registry), "Decision", "decision_id")
        with pytest.raises(ValueError):
            _exec_source_as(REPO / "writ" / "graph" / "db" / "_common.py", "writ.graph.db._probe_common_reg")

    def test_import_raises_when_the_record_labels_and_the_id_map_disagree(self):
        def add_label(source: str) -> str:
            start = source.index("RECORD_LABELS: frozenset[str] = frozenset(")
            end = source.index("})", start)
            return source[:end] + ', "ExtraRecord"' + source[end:]

        with pytest.raises(ValueError):
            _exec_source_as(
                REPO / "writ" / "graph" / "db" / "_common.py", "writ.graph.db._probe_common_labels", add_label)


class TestRecordCreatorsSendTheSameParameters:
    CASES = [
        ("create_decision", "Decision", _decision_data),
        ("create_filechange", "FileChange", _filechange_data),
        ("create_commit", "Commit", _commit_data),
        ("create_trust_event", "TrustEvent", _event),
        ("create_open_question", "OpenQuestion", _open_question_data),
    ]

    @pytest.mark.parametrize("method,label,data", CASES)
    def test_the_query_and_parameter_names_come_from_the_record_id_map(self, method, label, data):
        calls: list[dict] = []

        class _Probe(RecordStoreMixin):
            async def _write_single(self, query, **params):
                calls.append({"query": query, "params": params})
                return dict(params)

        conn = _Probe()
        payload = data()
        id_field = ORIGINAL_RECORD_IDS[label]
        asyncio.run(getattr(conn, method)(**payload))
        assert len(calls) == 1
        assert calls[0]["query"] == (
            f"MERGE (n:{label} {{{id_field}: ${id_field}, project: $project}}) "
            f"SET n += $props RETURN n.{id_field} AS {id_field}"
        )
        assert set(calls[0]["params"]) == {id_field, "project", "props"}
        assert calls[0]["params"][id_field] == payload[id_field]
        assert calls[0]["params"]["props"]["provenance"] == "record"
        assert calls[0]["params"]["props"]["source_origin"] == "graph-authored"
        assert _common.RECORD_ID_FIELDS[label] == id_field


# ===========================================================================
# 31. Kept explicit
# ===========================================================================


def _assigned_value(path: Path, name: str):
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and any(getattr(t, "id", "") == name for t in node.targets):
            return node.value
        if isinstance(node, ast.AnnAssign) and getattr(node.target, "id", "") == name:
            return node.value
    raise AssertionError(f"{name} is not assigned in {path}")


def _is_frozenset_of_string_literals(value) -> bool:
    return (
        isinstance(value, ast.Call) and getattr(value.func, "id", "") == "frozenset"
        and len(value.args) == 1 and isinstance(value.args[0], ast.Set)
        and all(isinstance(e, ast.Constant) and isinstance(e.value, str) for e in value.args[0].elts)
    )


class TestKeptExplicit:
    def test_doctrine_node_types_keeps_its_exact_membership(self):
        from writ.retrieval.node_scope import DOCTRINE_NODE_TYPES

        assert DOCTRINE_NODE_TYPES == DOCTRINE

    def test_doctrine_node_types_is_a_literal(self):
        value = _assigned_value(REPO / "writ" / "retrieval" / "node_scope.py", "DOCTRINE_NODE_TYPES")
        assert _is_frozenset_of_string_literals(value)

    def test_record_labels_keeps_the_original_members(self):
        assert set(ORIGINAL_RECORD_IDS) <= set(_common.RECORD_LABELS)

    def test_record_labels_is_a_literal(self):
        value = _assigned_value(REPO / "writ" / "graph" / "db" / "_common.py", "RECORD_LABELS")
        assert _is_frozenset_of_string_literals(value)

    def test_ranked_methodology_labels_is_the_source_not_a_derivation(self):
        assert schema.RANKED_METHODOLOGY_LABELS == (
            "Skill", "Playbook", "Technique", "AntiPattern", "ForbiddenResponse")
        value = _assigned_value(REPO / "writ" / "graph" / "schema.py", "RANKED_METHODOLOGY_LABELS")
        assert isinstance(value, ast.Tuple)


# ===========================================================================
# Workstream D (program item 5, phases 2 and 3): Document and Chunk, DOCUMENT_COLLECTOR.
# D's parts of this shared file; P's cases above are untouched apart from the one
# validate_collectors call-site pin in TestRankedRegistry, which D's widened call changes.
# ===========================================================================


class TestDocumentAndChunkAreRecordLabels:
    def test_both_labels_are_record_labels_with_their_id_fields(self):
        assert {"Document", "Chunk"} <= set(_common.RECORD_LABELS)
        assert _common.RECORD_ID_FIELDS["Document"] == "doc_id"
        assert _common.RECORD_ID_FIELDS["Chunk"] == "chunk_id"
        assert set(_common.RECORD_ID_FIELDS) == set(_common.RECORD_LABELS)

    def test_neither_label_is_a_node_type_a_node_id_field_or_doctrine(self):
        from writ.retrieval.node_scope import DOCTRINE_NODE_TYPES

        for label in ("Document", "Chunk"):
            assert label not in {t.value for t in schema.NodeType}
            assert label not in schema.NODE_TYPE_MODELS
            assert label not in schema.NODE_ID_FIELDS
            assert label not in DOCTRINE_NODE_TYPES
        assert DOCTRINE_NODE_TYPES == DOCTRINE, "adding records must leave the doctrine set exactly as it was"

    def test_no_document_or_chunk_property_name_is_a_node_id_fields_value(self):
        from writ.graph.schema import Chunk, Document

        id_fields = set(schema.NODE_ID_FIELDS.values())
        assert not set(Document.model_fields) & id_fields
        assert not set(Chunk.model_fields) & id_fields

    def test_the_record_edge_endpoint_allowlist_does_not_widen(self):
        assert set(_common._RECORD_EDGE_ENDPOINTS) - set(schema.NODE_ID_FIELDS) == {
            "Decision", "FileChange", "Commit", "Project", "OpenQuestion"}

    def test_the_models_stamp_records_and_default_their_origin(self):
        from writ.graph.schema import Chunk, Document

        document = Document(doc_id="docs/a.md", project="p", path="docs/a.md", kind="docs", title="T",
                            source_hash="h", chunk_count=1, ingested_at="2026-10-07T00:00:00+00:00")
        chunk = Chunk(chunk_id="p:docs/a.md#0000", project="p", doc_id="docs/a.md", ordinal=0,
                      breadcrumb="T", text="body", est_tokens=1, source_hash="h")
        for model in (document, chunk):
            assert (model.provenance, model.source_origin) == ("record", "graph-authored")

    @pytest.mark.parametrize("kind", ["docs", "adr", "readme", "claude_md", "memory"])
    def test_each_document_kind_is_accepted(self, kind):
        from writ.graph.schema import Document

        assert Document(doc_id="x", project="p", path="x", kind=kind, title="T", source_hash="h",
                        chunk_count=0, ingested_at="t").kind == kind

    @pytest.mark.parametrize("kind", ["", "doc", "ADR", "rule", "notes"])
    def test_a_kind_outside_the_vocabulary_is_rejected(self, kind):
        from pydantic import ValidationError

        from writ.graph.schema import Document

        with pytest.raises(ValidationError) as excinfo:
            Document(doc_id="x", project="p", path="x", kind=kind, title="T", source_hash="h",
                     chunk_count=0, ingested_at="t")
        assert "kind" in str(excinfo.value)

    def test_the_chunk_id_embeds_the_project_so_two_projects_never_collide(self):
        from writ.graph.schema import Chunk

        ids = {Chunk(chunk_id=f"{p}:README.md#0000", project=p, doc_id="README.md", ordinal=0, breadcrumb="R",
                     text="t", est_tokens=1, source_hash="h").chunk_id for p in ("proj-a", "proj-b")}
        assert len(ids) == 2


class TestDocumentCollector:
    def test_it_is_a_record_collector_named_documents_claiming_document_and_chunk(self):
        collector = _pipeline_mod().DOCUMENT_COLLECTOR
        assert isinstance(collector, _collectors().Collector)
        assert (collector.name, collector.scope) == ("documents", "record")
        assert collector.labels == ("Document", "Chunk")

    def test_it_validates_together_with_the_ranked_registry(self):
        mod = _pipeline_mod()
        assert _collectors().validate_collectors((*mod.RANKED_COLLECTORS, mod.DOCUMENT_COLLECTOR)) is None

    def test_it_claims_no_label_a_ranked_collector_claims(self):
        mod = _pipeline_mod()
        ranked = {label for c in mod.RANKED_COLLECTORS for label in c.labels}
        assert not ranked & set(mod.DOCUMENT_COLLECTOR.labels)
        assert ranked == set(DOCTRINE)

    def test_the_module_validates_the_widened_registry_in_one_raising_call(self):
        calls, asserts = _module_level_calls_and_asserts(REPO / "writ" / "retrieval" / "pipeline.py")
        validating = [c for c in calls
                      if getattr(c.func, "id", getattr(c.func, "attr", "")) == "validate_collectors"]
        assert len(validating) == 1
        dumped = ast.dump(validating[0].args[0])
        assert "RANKED_COLLECTORS" in dumped and "DOCUMENT_COLLECTOR" in dumped
        assert not [a for a in asserts if "COLLECTOR" in ast.dump(a)]

    def test_the_default_stamp_is_the_shared_fingerprint_digest(self):
        collector = _pipeline_mod().DOCUMENT_COLLECTOR
        assert collector.stamp_fn is _collectors().fingerprint_digest
        units = [{"chunk_id": "p:docs/a.md#0000", "next_id": None}]
        assert _collectors().Collected(units, collector.stamp_fn).stamp == _collectors().fingerprint_digest(units)

    def test_a_copy_claiming_chunk_as_doctrine_is_rejected(self):
        with pytest.raises(_collectors().CollectorRegistryError):
            _collectors().validate_collectors([_collector(name="documents", labels=("Chunk",), scope="doctrine")])

    def test_a_copy_claiming_document_as_doctrine_is_rejected(self):
        with pytest.raises(_collectors().CollectorRegistryError):
            _collectors().validate_collectors([_collector(name="documents", labels=("Document",), scope="doctrine")])

    @pytest.mark.parametrize("missing", ["Chunk", "Document"])
    def test_a_record_label_missing_from_record_labels_is_rejected(self, monkeypatch, missing):
        monkeypatch.setattr(_common, "RECORD_LABELS", _common.RECORD_LABELS - {missing})
        with pytest.raises(_collectors().CollectorRegistryError):
            _collectors().validate_collectors(
                [_collector(name="documents", labels=("Document", "Chunk"), scope="record")])

    def test_a_second_collector_claiming_chunk_is_rejected_beside_the_document_collector(self):
        mod = _pipeline_mod()
        extra = _collector(name="chunks-again", labels=("Chunk",), scope="record")
        with pytest.raises(_collectors().CollectorRegistryError):
            _collectors().validate_collectors((*mod.RANKED_COLLECTORS, mod.DOCUMENT_COLLECTOR, extra))
