"""writ.graph.integrity -- graph integrity/reconcile checks.

This package replaces the former single integrity.py module. IntegrityChecker is
composed from per-domain check mixins; each mixin holds its cluster's check methods
verbatim and reads self._driver / self._database. This module stays the import seam:
writ.graph.integrity.IntegrityChecker and the module-level names below remain
importable at this exact path. __init__ and run_all_checks (the orchestrator, with
its fuzz-verified exit-code aggregation) stay on the facade, moved verbatim.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

from writ.graph.integrity._common import (
    EXPECTED_FLOORS,
    KNOWN_ACTIONS,
    # Declared public in _common.__all__ and used by parity_checks, but never
    # re-exported here, so `writ.graph.integrity.PARITY_EXEMPT_PROVENANCE` -- the
    # path the design docs and the sequencing guard reference -- did not resolve.
    PARITY_EXEMPT_PROVENANCE,
    _ALWAYS_ON_CAP,
    _DEPRECATED_PASS_API,
    _FENCE_RE,
    _FLOOR_NODE_LABELS,
    _PY_FENCE_LANGS,
    _UNIVERSAL_FLOOR,
    _normalized_code_blocks,
    _python_blocks,
    lint_rule_examples,
    resolve_parity_oracle,
)
from writ.graph.integrity._query import _QueryMixin
from writ.graph.integrity.structural_checks import StructuralChecksMixin
from writ.graph.integrity.frequency_checks import FrequencyChecksMixin
from writ.graph.integrity.parity_checks import ParityChecksMixin
from writ.graph.integrity.edge_contract_checks import EdgeContractChecksMixin
from writ.graph.integrity.content_checks import ContentChecksMixin
from writ.graph.integrity.routing_checks import RoutingChecksMixin
from writ.graph.integrity.artifact_checks import ArtifactChecksMixin

if TYPE_CHECKING:
    from neo4j import AsyncDriver
    from pathlib import Path

__all__ = [
    "EXPECTED_FLOORS",
    "IntegrityChecker",
    "KNOWN_ACTIONS",
    "_ALWAYS_ON_CAP",
    "_DEPRECATED_PASS_API",
    "_FENCE_RE",
    "_FLOOR_NODE_LABELS",
    "_PY_FENCE_LANGS",
    "_UNIVERSAL_FLOOR",
    "_normalized_code_blocks",
    "_python_blocks",
    "lint_rule_examples",
]


class IntegrityChecker(
    _QueryMixin,
    StructuralChecksMixin, FrequencyChecksMixin, ParityChecksMixin, EdgeContractChecksMixin, ContentChecksMixin, RoutingChecksMixin, ArtifactChecksMixin,
):
    """Runs integrity checks against the rule graph."""

    def __init__(self, driver: AsyncDriver, database: str = "neo4j") -> None:
        self._driver = driver
        self._database = database
        # Optional fallback for callers (e.g. `writ prune`) that invoke
        # detect_parity_violations() with no bible_dir argument.
        self._default_bible_dir: Path | None = None

    async def run_all_checks(
        self, skip_redundancy: bool = False, bible_dir: Path | None = None,
        project: str = "writ", artifact_path: Path | None = None,
    ) -> dict:
        """Run all integrity checks. Returns findings dict.

        Returns exit_code: 0 if clean, 1 if any findings.
        """
        findings: dict = {
            "conflicts": await self.detect_conflicts(),
            "orphans": await self.detect_orphans(),
            "stale": await self.detect_stale(),
        }

        if not skip_redundancy:
            try:
                findings["redundant"] = await self.detect_redundant()
            except RuntimeError as exc:
                # detect_redundant() raises when sentence-transformers
                # is not installed. Catch here so the other four checks
                # (conflicts, orphans, stale, confidence-defaults) still
                # complete and report; surface the missing-dep state via
                # a separate key the caller (writ validate) can print
                # explicitly. exit_code below excludes this key on
                # purpose: "we could not run the check" is informational,
                # not a finding.
                findings["redundant"] = []
                findings["redundancy_unavailable"] = str(exc)
        else:
            findings["redundant"] = []

        findings["unreviewed"] = await self.check_unreviewed_count()
        findings["frequency_stale"] = await self.detect_frequency_stale()
        findings["graduation_flags"] = await self.detect_graduation_flags()

        orphans_all, orphan_counts = await self.detect_orphans_all_labels()
        findings["orphans_all_labels"] = orphans_all
        findings["orphan_counts_by_type"] = orphan_counts
        findings["dangling_dispatched_roles"] = await self.detect_dangling_dispatched_roles()

        # Phase 0: parity violations (graph nodes absent from markdown) and
        # category reachability (nodes with no BELONGS_TO category edge).
        #
        # Cycle 7 defect 1: these four return FALSY when they have no oracle, and
        # falsy is what "clean" looks like to the truthy sweep below, so four checks
        # that never ran were indistinguishable from four that passed. Resolve the
        # oracle once, and when it cannot be trusted say so in a finding.
        # parity_checks_skipped is NON-gating on purpose: a dict is truthy, so
        # gating it would fail every bible-absent checkout for reporting honestly.
        parity_dir, parity_skip_reason = resolve_parity_oracle(
            bible_dir, self._default_bible_dir
        )
        _PARITY_CHECK_KEYS = (
            "parity_violations", "edge_parity", "prop_parity",
            "methodology_field_drift",
        )
        if parity_skip_reason:
            findings["parity_checks_skipped"] = {
                "reason": parity_skip_reason,
                "checks": list(_PARITY_CHECK_KEYS),
            }
            # Set the falsy defaults DIRECTLY. Calling the detectors with None would
            # let each one fall back to self._default_bible_dir, which is the very
            # directory the resolver just rejected.
            findings["parity_violations"] = []
            findings["edge_parity"] = None
            findings["prop_parity"] = None
            findings["methodology_field_drift"] = None
        else:
            findings["parity_checks_skipped"] = None
            findings["parity_violations"] = await self.detect_parity_violations(
                parity_dir, project
            )
            findings["edge_parity"] = await self.detect_edge_parity(parity_dir, project)
            findings["prop_parity"] = await self.detect_prop_parity(parity_dir, project)
            findings["methodology_field_drift"] = (
                await self.detect_methodology_field_drift(parity_dir, project)
            )
        findings["dispatch_invokes"] = await self.detect_dispatch_invokes_invariant()
        findings["dispatch_prose_parity"] = await self.detect_dispatch_prose_parity()
        findings["teaches_source"] = await self.detect_teaches_source_invariant()
        findings["floor_completeness"] = await self.detect_floor_completeness()
        findings["trigger_keyword_invariant"] = await self.detect_trigger_keyword_invariant()
        findings["push_reachability"] = await self.detect_push_reachability()
        findings["action_vocabulary"] = await self.detect_action_vocabulary_closure()
        # 1.9: the route half of the same class. A route declared with no delivery
        # implementation, and a methodology node no channel can select, were both
        # silent until these two ran (the `ride_along` failure: 26 stranded nodes,
        # zero failing checks). Both are gating: they land outside _NON_GATING, so
        # the generic truthy sweep below picks them up with no exit-code edit.
        findings["route_implementation_closure"] = await self.detect_route_implementation_closure()
        findings["delivery_orphans"] = await self.detect_delivery_orphans()
        findings["example_lint"] = await self.detect_example_lint()
        findings["domain_enum"] = await self.detect_domain_enum_invariant()
        findings["counter_nodes_parity"] = await self.detect_counter_nodes_parity()
        findings["dispatched_by_parity"] = await self.detect_dispatched_by_parity()
        findings["enforceable_severity"] = await self.detect_enforceable_severity_coupling()
        findings["forbidden_phrase_overlap"] = await self.detect_forbidden_phrase_overlap()
        findings["shared_code_example"] = await self.detect_shared_code_example()
        reachability = await self.detect_category_reachability()
        findings["category_reachability"] = reachability

        # 3.6a: mandatory retrieval invariant. A mandatory rule reachable by
        # neither the ranked pool (it's excluded) nor the injection path is the
        # 29-stranded bug class -- a silent no-op gate. Plus the ranked-exclusion
        # parity and the summary-render budget guards (WRIT-BLUEPRINT 3.5/3.6a).
        findings["stranded_mandatory"] = await self.detect_stranded_mandatory()
        findings["ranked_exclusion_mismatch"] = await self.detect_ranked_exclusion_mismatch()
        findings["always_on_budget_breach"] = await self.detect_always_on_budget_breach()

        # Approach A: dangling rule_id refs in the cached abstraction artifact
        # (bible/abstractions.json) -- the artifact drifted from the corpus.
        findings["artifact_dangling_rule_ids"] = await self.detect_artifact_dangling_rule_ids(
            artifact_path, project
        )
        # Cycle 7 defect 2b: the coverage the markdown-parity exemption gives up.
        # The oracle cannot see bible/abstractions.json, so the ABSTRACTS edge set
        # is compared against the artifact itself, in both directions. Gating by
        # omission from _NON_GATING below.
        findings["artifact_abstracts_parity"] = await self.detect_artifact_abstracts_parity(
            artifact_path, project
        )

        # Exit code: any GATING finding that is truthy fails validate. The keys in
        # _NON_GATING are populated for reporting but never gate (informational /
        # count-only). category_reachability needs a custom predicate -- it returns a
        # populated dict even when clean (skipped, or no missing-category nodes), so it
        # is excluded from the generic truthy sweep and checked explicitly. This
        # data-driven form replaces ~28 hand-written per-finding `if findings[x]:`
        # blocks (M6); it is fuzz-verified equivalent across 200k findings combinations,
        # so a NEW gating check now auto-counts with no exit-code edit -- only a new
        # NON-gating key needs adding to the set below.
        _NON_GATING = {
            "redundancy_unavailable",  # informational: missing sentence-transformers
            "unreviewed",              # informational count
            "frequency_stale",         # informational
            "graduation_flags",        # informational (candidates, not failures)
            "orphan_counts_by_type",   # count companion to orphans_all_labels
            "parity_checks_skipped",   # cycle 7: honest skip report, not a finding
        }
        has_issues = any(
            bool(v) for k, v in findings.items()
            if k not in _NON_GATING and k != "category_reachability"
        ) or (
            # reachability fails only when it actually ran and a node lacks a category.
            not reachability.get("skipped")
            and bool(reachability.get("nodes_without_category"))
        )

        findings["exit_code"] = 1 if has_issues else 0
        return findings
