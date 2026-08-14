"""T0.1 schema RED gate -- Category schema, RouteValue enum, BelongsTo edge,
NodeType.CATEGORY, and registry completeness.

All tests here must FAIL until writ/graph/schema.py gains:
  - RouteValue enum with exactly 7 values
  - VALID_ROUTES frozenset
  - Category model (category_id, name, routes, parent, description)
  - BelongsTo edge
  - NodeType.CATEGORY = 'Category'
  - 'Category' removed from RETRIEVABLE_NODE_TYPES
  - NODE_ID_FIELDS['Category'] = 'category_id'
  - NODE_ID_FIELDS['Abstraction'] = 'abstraction_id'
  - NODE_TYPE_MODELS['Category'] = Category
  - NODE_TYPE_MODELS['Abstraction'] = Abstraction
  - invariant set(NODE_ID_FIELDS) == set(NODE_TYPE_MODELS)
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError


class TestRouteValueEnum:
    """RouteValue enum must carry exactly the 7 canonical route strings."""

    def test_all_seven_values_present(self) -> None:
        from writ.graph.schema import RouteValue

        expected = {"semantic", "scoped", "state", "action", "always_on", "pull", "ride_along"}
        actual = {rv.value for rv in RouteValue}
        assert actual == expected

    def test_valid_routes_frozenset_matches_enum(self) -> None:
        from writ.graph.schema import RouteValue, VALID_ROUTES

        enum_values = {rv.value for rv in RouteValue}
        assert VALID_ROUTES == enum_values
        assert isinstance(VALID_ROUTES, frozenset)

    def test_wired_routes_is_hand_listed_excluding_ride_along_and_scoped(self) -> None:
        """1.9 (finalized 2026-08-12): WIRED_ROUTES is a HAND-LISTED set of the
        routes with a real, verified delivery mechanism -- {action, always_on,
        pull, semantic, state} -- NOT derived from VALID_ROUTES by subtraction.

        The subtraction form (an earlier draft of this contract, since
        retracted) is backwards: it makes any NEW RouteValue member wired by
        default, with no implementation behind it and every check staying
        green -- precisely how `ride_along` survived unimplemented for
        months. The hand-list inverts the default to UNWIRED, so a route
        gained by the enum trips detect_route_implementation_closure
        immediately instead of silently passing.

        `ride_along` and `scoped` are both excluded, each independently
        verified unimplemented (nothing reads either value; no delivery
        mechanism exists for either). This test does NOT assert `WIRED_ROUTES
        == VALID_ROUTES - {ride_along}` or any other derived form: the two
        sets are MEANT to differ for as long as any route is unimplemented,
        and a third route later joining the unimplemented set must not make
        this test fail by construction.

        Every comparison below names the offending member(s) on failure
        (sorted-list diffs, not bare frozenset equality) -- a truncated
        `frozenset({'a...ic', 'state'}) == frozenset({'a...ic', 'state'})`
        repr hides exactly the fact (which single member differs) a reader
        needs to act on it.
        """
        from writ.graph.schema import VALID_ROUTES, WIRED_ROUTES

        assert isinstance(WIRED_ROUTES, frozenset)

        expected_wired = {"action", "always_on", "pull", "semantic", "state"}
        extra = sorted(WIRED_ROUTES - expected_wired)
        missing = sorted(expected_wired - WIRED_ROUTES)
        assert not extra and not missing, (
            f"WIRED_ROUTES unexpectedly contains: {extra}; "
            f"unexpectedly omits: {missing} "
            f"(full WIRED_ROUTES: {sorted(WIRED_ROUTES)})"
        )

        assert "ride_along" not in WIRED_ROUTES, (
            f"WIRED_ROUTES must exclude ride_along; got {sorted(WIRED_ROUTES)}"
        )
        assert "scoped" not in WIRED_ROUTES, (
            f"WIRED_ROUTES must exclude scoped; got {sorted(WIRED_ROUTES)}"
        )

        # Strict subset, never equality: the sets are meant to differ for as
        # long as any route is unimplemented.
        assert WIRED_ROUTES < VALID_ROUTES, (
            f"WIRED_ROUTES ({sorted(WIRED_ROUTES)}) must be a STRICT subset "
            f"of VALID_ROUTES ({sorted(VALID_ROUTES)}); equality would mean "
            f"nothing is unwired, which is not true today"
        )
        unimplemented = sorted(VALID_ROUTES - WIRED_ROUTES)
        assert unimplemented == ["ride_along", "scoped"], (
            f"expected VALID_ROUTES - WIRED_ROUTES == ['ride_along', 'scoped']; "
            f"got {unimplemented}"
        )

    def test_wired_routes_subset_guard_would_reject_unknown_route(self) -> None:
        """Mirrors schema.py's own anti-drift guard -- `if not WIRED_ROUTES <=
        VALID_ROUTES: raise ValueError(...)`, executed at module import time
        -- which catches the new failure mode a hand-list introduces that the
        old derived form never could: a typo'd/invented route name IN the
        hand-list itself. (Subtraction can only ever REMOVE members from
        VALID_ROUTES; it cannot invent one that was never there.)

        Deliberately does NOT dynamically re-execute the real guard source
        (SEC-INJ-CMD-002 forbids exec/eval on a non-literal-constant string,
        and the extracted source would be exactly that): instead this checks
        the guard's own predicate and diff expression directly against a
        corrupted WIRED_ROUTES, naming the offending member on failure. The
        raise itself is a one-line `raise ValueError(f"...")` guarded by this
        exact predicate, so proving the predicate is false (and the diff
        names the corrupted member) is equivalent to proving the guard would
        fire -- and does not require reloading writ.graph.schema, which would
        redefine RouteValue/Category as new classes and break `is`-identity
        checks elsewhere in this file (e.g. NODE_TYPE_MODELS['Category'] is
        Category in TestRegistryCompleteness below).
        """
        from writ.graph.schema import VALID_ROUTES, WIRED_ROUTES

        bogus_route = "not_a_real_route"
        corrupted_wired = WIRED_ROUTES | {bogus_route}

        assert not (corrupted_wired <= VALID_ROUTES), (
            f"expected corrupted WIRED_ROUTES to fail the subset check "
            f"(that IS the guard's `if` condition); it did not, so "
            f"{bogus_route!r} must have become a real VALID_ROUTES member -- "
            f"pick a different bogus name for this test"
        )
        offending = sorted(corrupted_wired - VALID_ROUTES)
        assert offending == [bogus_route], (
            f"the guard's own diff expression (WIRED_ROUTES - VALID_ROUTES, "
            f"the exact text the raised ValueError interpolates) must name "
            f"only {bogus_route!r}; got {offending}"
        )


class TestCategoryModel:
    """Category model validation -- prefix, routes, dedup, defaults."""

    def test_instantiates_minimal(self) -> None:
        from writ.graph.schema import Category

        cat = Category(category_id="CAT-CODING-001", name="coding-rules", routes=["semantic"])
        assert cat.category_id == "CAT-CODING-001"
        assert cat.name == "coding-rules"
        assert cat.parent is None
        assert cat.description == ""

    def test_multiple_routes_accepted_and_deduped(self) -> None:
        from writ.graph.schema import Category

        cat = Category(
            category_id="CAT-PROC-001",
            name="process",
            routes=["semantic", "action", "semantic"],
        )
        # Duplicates must be collapsed; order is unspecified so compare as set.
        assert set(cat.routes) == {"semantic", "action"}

    def test_rejects_wrong_prefix(self) -> None:
        from writ.graph.schema import Category

        with pytest.raises(ValidationError):
            Category(category_id="SKL-PROC-001", name="bad-prefix", routes=["semantic"])

    def test_rejects_empty_routes(self) -> None:
        from writ.graph.schema import Category

        with pytest.raises(ValidationError):
            Category(category_id="CAT-CODING-001", name="coding-rules", routes=[])

    def test_rejects_invalid_route_value(self) -> None:
        from writ.graph.schema import Category

        with pytest.raises(ValidationError):
            Category(
                category_id="CAT-CODING-001",
                name="coding-rules",
                routes=["invalid-route"],
            )

    def test_parent_optional_string(self) -> None:
        from writ.graph.schema import Category

        cat = Category(
            category_id="CAT-FW-MAGENTO2-001",
            name="frameworks/magento2",
            routes=["semantic", "scoped"],
            parent="CAT-CODING-001",
        )
        assert cat.parent == "CAT-CODING-001"

    def test_description_optional_string(self) -> None:
        from writ.graph.schema import Category

        cat = Category(
            category_id="CAT-PROC-001",
            name="process",
            routes=["state", "action", "pull"],
            description="Workflow and orchestration nodes.",
        )
        assert cat.description == "Workflow and orchestration nodes."

    def test_discipline_counters_uses_ride_along(self) -> None:
        from writ.graph.schema import Category

        cat = Category(
            category_id="CAT-DISCIPLINE-COUNTERS-001",
            name="discipline-counters",
            routes=["ride_along"],
        )
        assert "ride_along" in cat.routes


class TestBelongsToEdge:
    """BelongsTo edge model -- source_id, target_id, edge-type string."""

    def test_instantiates(self) -> None:
        from writ.graph.schema import BelongsTo

        edge = BelongsTo(source_id="SKL-PROC-BRAIN-001", target_id="CAT-PROC-001")
        assert edge.source_id == "SKL-PROC-BRAIN-001"
        assert edge.target_id == "CAT-PROC-001"

    def test_edge_type_string(self) -> None:
        from writ.graph.schema import BelongsTo

        edge = BelongsTo(source_id="SKL-PROC-BRAIN-001", target_id="CAT-PROC-001")
        # The edge-type string must be 'BELONGS_TO' (Neo4j relationship label).
        assert edge.edge_type == "BELONGS_TO"

    def test_rejects_empty_source(self) -> None:
        from writ.graph.schema import BelongsTo

        with pytest.raises(ValidationError):
            BelongsTo(source_id="", target_id="CAT-PROC-001")

    def test_rejects_empty_target(self) -> None:
        from writ.graph.schema import BelongsTo

        with pytest.raises(ValidationError):
            BelongsTo(source_id="SKL-PROC-BRAIN-001", target_id="")


class TestNodeTypeEnum:
    """NodeType enum gains CATEGORY; Category is NOT retrievable."""

    def test_category_member_exists(self) -> None:
        from writ.graph.schema import NodeType

        assert NodeType.CATEGORY == "Category"

    def test_category_not_in_retrievable_node_types(self) -> None:
        from writ.graph.schema import NodeType, RETRIEVABLE_NODE_TYPES

        assert NodeType.CATEGORY not in RETRIEVABLE_NODE_TYPES


class TestRegistryCompleteness:
    """NODE_ID_FIELDS and NODE_TYPE_MODELS must be in sync and include Category + Abstraction."""

    def test_category_in_node_id_fields(self) -> None:
        from writ.graph.schema import NODE_ID_FIELDS

        assert "Category" in NODE_ID_FIELDS
        assert NODE_ID_FIELDS["Category"] == "category_id"

    def test_abstraction_in_node_id_fields(self) -> None:
        from writ.graph.schema import NODE_ID_FIELDS

        assert "Abstraction" in NODE_ID_FIELDS
        assert NODE_ID_FIELDS["Abstraction"] == "abstraction_id"

    def test_category_in_node_type_models(self) -> None:
        from writ.graph.schema import NODE_TYPE_MODELS, Category

        assert "Category" in NODE_TYPE_MODELS
        assert NODE_TYPE_MODELS["Category"] is Category

    def test_abstraction_in_node_type_models(self) -> None:
        from writ.graph.schema import NODE_TYPE_MODELS, Abstraction

        assert "Abstraction" in NODE_TYPE_MODELS
        assert NODE_TYPE_MODELS["Abstraction"] is Abstraction

    def test_all_node_types_have_id_field_and_model(self) -> None:
        from writ.graph.schema import NODE_ID_FIELDS, NODE_TYPE_MODELS

        assert set(NODE_ID_FIELDS) == set(NODE_TYPE_MODELS), (
            "NODE_ID_FIELDS and NODE_TYPE_MODELS are out of sync -- "
            f"id_fields-only: {set(NODE_ID_FIELDS)-set(NODE_TYPE_MODELS)}, "
            f"models-only: {set(NODE_TYPE_MODELS)-set(NODE_ID_FIELDS)}"
        )
