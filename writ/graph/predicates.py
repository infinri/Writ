"""Single-source Cypher predicates for the mandatory / always-on retrieval invariant.

Both the ``/always-on`` injection endpoint (``server.py``) and the
``writ validate`` integrity checker (``integrity.py``) import these constants so
the injection selection and its loud validator cannot drift apart. The
29-stranded bug (WRIT-BLUEPRINT 3.5/3.6a) was exactly two mechanisms keyed on
different fields with nothing failing loud; one definition closes that class.

The predicates assume the bound variable is ``r`` (a ``:Rule`` node).
"""

# Rules selected for always-on injection. UNION model (WRIT-BLUEPRINT 3.5): a
# rule reaches the agent by injection if it is a mandatory obligation OR flagged
# always-on (applicability = always, which MAY be advisory, e.g. ENF-COMMS-001).
# `mandatory` and `always_on` are orthogonal-but-overlapping, not one concept.
#
# Program item 6: a superseded rule (the target of a SUPERSEDES edge, flag derived by
# refresh_superseded_flags) leaves both channels. Each predicate is fully
# parenthesized so embedding it in a larger WHERE can never let the OR escape.
_NOT_SUPERSEDED = "coalesce(r.superseded, false) = false"
INJECTION_RULE_WHERE = f"((r.mandatory = true OR r.always_on = true) AND {_NOT_SUPERSEDED})"

# Rules INCLUDED in the ranked retrieval pool (pipeline load / BM25 / vector).
# Its complement over all Rules is {excluded-from-ranked}, which the validator
# asserts equals {mandatory} union {superseded}. Keep in lockstep with pipeline.py /
# keyword.py / corpus_footprint.py.
RANKED_INCLUDE_WHERE = f"((r.mandatory IS NULL OR r.mandatory = false) AND {_NOT_SUPERSEDED})"
