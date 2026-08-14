"""Part 3 (isolation cycle v2): writ/retrieval/node_scope.py, the single source for
the doctrine-versus-records split.

plan.md ("Part 3: cross-project retrieval scope, as a doctrine leak") rejects
re-tagging the 287 Rule nodes and rejects widening the allowed-project set, and
picks NODE-TYPE scoping instead: doctrine (Rule + the five methodology labels)
reaches every project regardless of its `project` tag, and everything else is a
record, scoped to {caller_project, "_shared"}. The deciding argument is what
happens to a node type nobody has thought of yet: under this design it is
excluded (fails CLOSED into a visible retrieval gap); under every rejected
alternative it would have been admitted (fails OPEN, silently).

These are pure-predicate unit tests -- no Neo4j, no pipeline, no I/O. The
integration proof against a real graph (that the predicate is actually wired
into RetrievalPipeline._filter_candidates, and that the real corpus still
reaches every project) lives in test_cross_project_retrieval_isolation.py;
splitting the two means a failure here says "the predicate itself is wrong"
rather than "something in five stages of retrieval is wrong".

RED today: writ/retrieval/node_scope.py does not exist.
"""

from __future__ import annotations

import pytest


# ---------------------------------------------------------------------------
# DOCTRINE_NODE_TYPES: the explicit allowlist
# ---------------------------------------------------------------------------


class TestDoctrineNodeTypesAllowlist:
    """The allowlist is the whole safety argument, so its exact membership is
    pinned rather than sampled. A silently narrowed set would strand a real
    methodology label as a "record" and hide it from every other project; a
    silently widened set would leak the next record label into every project."""

    def test_allowlist_contains_exactly_the_six_named_types(self) -> None:
        from writ.retrieval.node_scope import DOCTRINE_NODE_TYPES

        assert DOCTRINE_NODE_TYPES == {
            "Rule", "Skill", "Playbook", "Technique", "AntiPattern", "ForbiddenResponse",
        }

    def test_allowlist_is_a_set_not_a_list(self) -> None:
        """Membership testing (`in`) is the whole hot-path use; a list would work too
        but a future edit that appends a duplicate would go unnoticed forever."""
        from writ.retrieval.node_scope import DOCTRINE_NODE_TYPES

        assert isinstance(DOCTRINE_NODE_TYPES, (set, frozenset))

    def test_known_record_labels_are_absent_from_the_allowlist(self) -> None:
        """Memory/Decision/FileChange/Commit are records by the plan's own account
        (Memory's docstring says it "must never enter the RAG pipeline"). Their
        absence here is the other half of the allowlist's correctness -- an
        allowlist that also included them would not be an allowlist."""
        from writ.retrieval.node_scope import DOCTRINE_NODE_TYPES

        for record_label in ("Memory", "Decision", "FileChange", "Commit", "Project"):
            assert record_label not in DOCTRINE_NODE_TYPES, (
                f"{record_label} is a record type and must not be treated as doctrine"
            )


# ---------------------------------------------------------------------------
# is_visible(node_type, node_project, caller_project): the predicate
# ---------------------------------------------------------------------------


class TestDoctrinePassesRegardlessOfProjectTag:
    """The catastrophic-fix guard at the predicate level. All 287 Rule nodes and
    every methodology node in the live corpus carry `project: "writ"`; a caller
    from any other project must still see them."""

    @pytest.mark.parametrize("doctrine_type", [
        "Rule", "Skill", "Playbook", "Technique", "AntiPattern", "ForbiddenResponse",
    ])
    def test_doctrine_type_visible_to_a_different_caller_project(self, doctrine_type: str) -> None:
        from writ.retrieval.node_scope import is_visible

        assert is_visible(doctrine_type, node_project="writ", caller_project="proj-a") is True

    @pytest.mark.parametrize("doctrine_type", [
        "Rule", "Skill", "Playbook", "Technique", "AntiPattern", "ForbiddenResponse",
    ])
    def test_doctrine_type_visible_with_no_caller_project(self, doctrine_type: str) -> None:
        """An unscoped caller (authoring.py's dedup suggestions, `writ query`) still
        gets the full rulebook -- a completeness degradation, never a leak."""
        from writ.retrieval.node_scope import is_visible

        assert is_visible(doctrine_type, node_project="writ", caller_project=None) is True

    def test_doctrine_type_visible_even_when_project_tags_match(self) -> None:
        """Anti-vacuity: doctrine must pass on a MATCHING tag too, not only on a
        mismatched one -- a predicate that only special-cased the mismatch would
        still (accidentally) look correct on every test above."""
        from writ.retrieval.node_scope import is_visible

        assert is_visible("Rule", node_project="proj-a", caller_project="proj-a") is True


class TestRecordsAreScopedToCallerOrShared:
    def test_record_type_visible_to_its_own_project(self) -> None:
        from writ.retrieval.node_scope import is_visible

        assert is_visible("Decision", node_project="proj-a", caller_project="proj-a") is True

    def test_record_type_not_visible_to_a_different_project(self) -> None:
        from writ.retrieval.node_scope import is_visible

        assert is_visible("Decision", node_project="proj-b", caller_project="proj-a") is False

    def test_record_type_tagged_shared_is_visible_to_any_caller(self) -> None:
        from writ.retrieval.node_scope import is_visible

        assert is_visible("Decision", node_project="_shared", caller_project="proj-a") is True
        assert is_visible("Decision", node_project="_shared", caller_project="proj-z") is True

    def test_record_type_not_visible_with_no_caller_project(self) -> None:
        """The other half of the inverted default: with no caller project, a
        project-specific record must NOT be treated as search-all. Only doctrine
        gets the no-project pass; a record's project is never None or "_shared"
        in practice, so this must resolve to False."""
        from writ.retrieval.node_scope import is_visible

        assert is_visible("Decision", node_project="proj-a", caller_project=None) is False


class TestUnknownNodeTypeFailsClosedAsARecord:
    """The design's core asymmetry, and the reason node-type scoping was chosen
    over every rejected alternative: a type nobody has anticipated yet must be
    treated as a record (excluded from another project), not as doctrine
    (admitted everywhere). The failure direction is a visible retrieval gap,
    never a silent cross-project admission."""

    def test_unrecognized_type_is_excluded_from_another_project(self) -> None:
        from writ.retrieval.node_scope import is_visible

        assert is_visible("SomeFutureNodeType", node_project="proj-b", caller_project="proj-a") is False

    def test_unrecognized_type_is_visible_to_its_own_project(self) -> None:
        """Anti-vacuity: an unknown type is not blanket-refused, only scoped like any
        other record -- a predicate that always returned False here would pass the
        test above for the wrong reason."""
        from writ.retrieval.node_scope import is_visible

        assert is_visible("SomeFutureNodeType", node_project="proj-a", caller_project="proj-a") is True

    def test_unrecognized_type_is_excluded_with_no_caller_project(self) -> None:
        from writ.retrieval.node_scope import is_visible

        assert is_visible("SomeFutureNodeType", node_project="proj-a", caller_project=None) is False

    def test_empty_string_node_type_is_treated_as_a_record_not_a_crash(self) -> None:
        """A metadata dict missing node_type (pipeline.py's own default is the
        string "Rule", per `m.get("node_type", "Rule")") should never reach here
        as an empty string in practice, but the predicate must not raise on
        malformed input -- raising here would take down retrieval for every
        candidate behind it, not just the malformed one."""
        from writ.retrieval.node_scope import is_visible

        assert is_visible("", node_project="proj-b", caller_project="proj-a") is False
