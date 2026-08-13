"""Seam guard for the Wave 2 integrity.py -> writ/graph/integrity/ package split.

The split moves IntegrityChecker's 42 methods verbatim onto per-domain check
mixins, keeping __init__ + run_all_checks on the facade. It MUST preserve the
seam: writ.graph.integrity.IntegrityChecker stays a class (monkeypatched in
test_cli_rename via its dotted method path), _driver/_database/_default_bible_dir
stay instance attrs, all method names stay stable, and the module-level names
below stay importable from writ.graph.integrity.

RED today: test_integritychecker_composed_from_check_mixins imports the new
check submodules, which do not exist until the split lands.

1.9 update: detect_route_implementation_closure and detect_delivery_orphans
landed on RoutingChecksMixin (writ/graph/integrity/routing_checks.py)
alongside detect_trigger_keyword_invariant/detect_push_reachability; both are
added to EXPECTED_METHODS below in alphabetical position. This list asserts
hard equality (test_facade_defines_only_init_and_run_all_checks), so it fails
on an omission (a method silently dropped) exactly as loudly as on an extra
(a method added without updating this pin).
"""
from __future__ import annotations

import pytest

# Full method surface of IntegrityChecker at main HEAD 0d71c1b (introspected).
EXPECTED_METHODS = [
    "__init__", "_diff_node_props", "_fetch_graph_nodes", "_field_vs_edges_parity",
    "_get_categories_without_route", "_get_nodes_without_belongs_to",
    "_load_methodology_source_props", "_run", "check_unreviewed_count",
    "detect_action_vocabulary_closure", "detect_always_on_budget_breach",
    "detect_artifact_abstracts_parity",
    "detect_artifact_dangling_rule_ids", "detect_category_reachability",
    "detect_confidence_defaults", "detect_conflicts", "detect_counter_nodes_parity",
    "detect_dangling_dispatched_roles", "detect_delivery_orphans",
    "detect_dispatch_invokes_invariant",
    "detect_dispatched_by_parity", "detect_domain_enum_invariant", "detect_edge_parity",
    "detect_enforceable_severity_coupling", "detect_example_lint",
    "detect_floor_completeness", "detect_forbidden_phrase_overlap",
    "detect_frequency_stale", "detect_graduation_flags", "detect_methodology_field_drift",
    "detect_orphans", "detect_orphans_all_labels", "detect_parity_violations",
    "detect_prop_parity", "detect_push_reachability", "detect_ranked_exclusion_mismatch",
    "detect_redundant", "detect_route_implementation_closure",
    "detect_shared_code_example", "detect_stale",
    "detect_stranded_mandatory", "detect_teaches_source_invariant",
    "detect_trigger_keyword_invariant", "get_all_nodes", "get_category_count",
    "run_all_checks",
]

# (submodule, check-mixin class) the facade composes IntegrityChecker from.
EXPECTED_MIXINS = [
    ("writ.graph.integrity._query", "_QueryMixin"),
    ("writ.graph.integrity.structural_checks", "StructuralChecksMixin"),
    ("writ.graph.integrity.frequency_checks", "FrequencyChecksMixin"),
    ("writ.graph.integrity.parity_checks", "ParityChecksMixin"),
    ("writ.graph.integrity.edge_contract_checks", "EdgeContractChecksMixin"),
    ("writ.graph.integrity.content_checks", "ContentChecksMixin"),
    ("writ.graph.integrity.routing_checks", "RoutingChecksMixin"),
    ("writ.graph.integrity.artifact_checks", "ArtifactChecksMixin"),
]

# Module-level names other modules import from writ.graph.integrity.
EXPECTED_MODULE_NAMES = [
    "IntegrityChecker", "KNOWN_ACTIONS", "lint_rule_examples",
    "_normalized_code_blocks", "_FENCE_RE",
]

# The facade class body itself defines only these (every check is a mixin).
FACADE_OWN = {"__init__", "run_all_checks"}


class TestMixinComposition:
    def test_integritychecker_composed_from_check_mixins(self) -> None:
        import importlib

        from writ.graph.integrity import IntegrityChecker

        mro = IntegrityChecker.__mro__
        for module_path, cls_name in EXPECTED_MIXINS:
            mod = importlib.import_module(module_path)  # RED today: submodule absent
            mixin = getattr(mod, cls_name)
            assert mixin in mro, (
                f"{cls_name} from {module_path} must be a base of IntegrityChecker; "
                f"MRO is {[c.__name__ for c in mro]}"
            )


class TestMethodSurfaceParity:
    @pytest.mark.parametrize("name", EXPECTED_METHODS)
    def test_method_present(self, name: str) -> None:
        from writ.graph.integrity import IntegrityChecker

        assert callable(getattr(IntegrityChecker, name, None)), (
            f"IntegrityChecker.{name} was lost in the split"
        )

    def test_facade_defines_only_init_and_run_all_checks(self) -> None:
        from writ.graph.integrity import IntegrityChecker

        resolved = {
            n for n in dir(IntegrityChecker)
            if callable(getattr(IntegrityChecker, n))
            and (not n.startswith("__") or n == "__init__")
        }
        assert resolved == set(EXPECTED_METHODS), (
            "resolved method surface diverged; "
            f"extras: {sorted(resolved - set(EXPECTED_METHODS))}; "
            f"missing: {sorted(set(EXPECTED_METHODS) - resolved)}"
        )
        facade_defined = {
            n for n, v in vars(IntegrityChecker).items()
            if (callable(v) or isinstance(v, staticmethod))
            and (not n.startswith("__") or n == "__init__")
        }
        assert facade_defined == FACADE_OWN, (
            f"facade must define only {sorted(FACADE_OWN)}; found {sorted(facade_defined)}"
        )


class TestSeamPreserved:
    def test_module_level_names_importable(self) -> None:
        import writ.graph.integrity as integ

        missing = [n for n in EXPECTED_MODULE_NAMES if not hasattr(integ, n)]
        assert missing == [], (
            f"names not re-exported from writ.graph.integrity: {missing}"
        )

    def test_construct_sets_attrs(self) -> None:
        from writ.graph.integrity import IntegrityChecker

        checker = IntegrityChecker(None)
        assert checker._driver is None
        assert checker._database == "neo4j"
        assert checker._default_bible_dir is None
