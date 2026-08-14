"""The doctrine-versus-records split: one predicate, one allowlist.

Retrieval shares ONE graph across every project on the machine, so the filter at
the end of RetrievalPipeline._filter_candidates is a confidentiality boundary.
Before this module that boundary was scoped by a node's `project` TAG, with
`project=None` disabling it outright, and the plan's Part 3 records why tag
scoping cannot be the answer here: all 287 Rule nodes and every methodology node
in the live corpus carry `project: "writ"` (pipeline.py also defaults an untagged
node to "writ"), so scoping by tag would have excluded the ENTIRE doctrine corpus
from every project except this one. Isolation tests would still have passed while
Writ silently stopped injecting any rule anywhere else.

So the scope is decided by node TYPE, which is intrinsic to the label rather than
to any node's data:

  doctrine  Rule + the five retrievable methodology labels. Universal by design,
            so it reaches every caller regardless of the tag it carries.
  records   Memory, Decision, FileChange, Commit and anything else. Private to
            one project, so scoped to {caller_project, "_shared"}.

THE ASYMMETRY IS THE DESIGN. DOCTRINE_NODE_TYPES is an explicit allowlist and
everything absent from it is a record, so a node type nobody has anticipated yet
is EXCLUDED from another project's results. It fails closed into a visible
retrieval gap someone notices, never open into a silent cross-project admission.
That is the whole reason node-type scoping was chosen over re-tagging the corpus
or widening the allowed-project set.
"""

from __future__ import annotations

# The tag that means "every project may read this record". Kept as a named
# constant so the two branches below cannot drift from the string the M.3
# registry and ingest paths write.
SHARED_PROJECT = "_shared"

# WRITTEN OUT, NOT DERIVED. Building this from a shared constant (e.g.
# trigger_index.RETRIEVABLE_METHODOLOGY_LABELS) would make a newly added label
# doctrine automatically, which is the fail-OPEN direction this module exists to
# refuse: a new label would silently become readable by every project. An
# explicit literal forces the widening to be a deliberate edit here, reviewed as
# a confidentiality change, and tests/test_retrieval_node_scope.py pins the exact
# membership so the edit cannot pass unnoticed.
DOCTRINE_NODE_TYPES = frozenset({
    "Rule",
    "Skill",
    "Playbook",
    "Technique",
    "AntiPattern",
    "ForbiddenResponse",
})


def is_visible(
    node_type: str,
    node_project: str | None,
    caller_project: str | None,
) -> bool:
    """Whether a candidate node may be returned to a caller from `caller_project`.

    node_type is the graph label (pipeline metadata's `node_type`), node_project
    the node's own `project` tag, caller_project the project the request resolved
    to, or None/"" when it resolved to none (an unregistered cwd, or a caller
    that sends no project at all).

    Never raises: a malformed or empty node_type is treated as a record, because
    raising here would take down retrieval for every candidate behind the
    malformed one rather than only for it.
    """
    if node_type in DOCTRINE_NODE_TYPES:
        return True
    # No caller project means no records, only the explicitly shared ones. The
    # `in {caller_project, ...}` form below would otherwise admit an untagged
    # record (node_project None) to an unscoped caller, which is the fail-open
    # this predicate replaced.
    if not caller_project:
        return node_project == SHARED_PROJECT
    return node_project in (caller_project, SHARED_PROJECT)
