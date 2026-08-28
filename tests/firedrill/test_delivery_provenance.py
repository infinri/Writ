"""Layer 4 of the drill (plan.md Decision 3): each refusal's mechanism against
`writ.shared.delivery`, and a report (never an assertion) of every classification
whose provenance is unproven.

`classify_delivery` gains two new mechanism names this cycle -- `exit2_stderr` and
`exit_nonzero_stderr` -- because the table has no term for an exit code today and
the single most important unresolved question in this cycle is what the harness
does with a Stop hook that exits 1. `delivery_provenance(event, mechanism)` answers
"observed" only when `docs/reference/blackbox-census.json` holds a matching record
class, "unproven" otherwise. The drill asserts delivery ONLY where provenance is
observed; everything else is reported by name, never asserted, and the artifact
starts holding zero records (capture only just started), which this module pins as
a passing state in its own right, not merely a fallback.

SEAM CONTRACT for the implementer: `writ/shared/delivery.py` must expose a
module-level `CENSUS_PATH: Path` (default `docs/reference/blackbox-census.json`)
that `delivery_provenance` resolves FRESH at call time -- not bound once at
import -- so these tests can monkeypatch the attribute directly. Same seam-naming
convention `tests/test_role_write_scope.py` already uses for
`writ.session.role_scope.fetch_declared_scope`.
"""
from __future__ import annotations

import json

import pytest

from writ.shared.delivery import classify_delivery
from tests.firedrill._census import REFUSALS


def _delivery_provenance():
    try:
        from writ.shared.delivery import delivery_provenance
    except ImportError as exc:
        pytest.fail(f"skeleton: writ/shared/delivery.py has no delivery_provenance() yet: {exc}")
    return delivery_provenance


def _delivery_module():
    import writ.shared.delivery as delivery_mod

    if not hasattr(delivery_mod, "CENSUS_PATH"):
        pytest.fail("skeleton: writ.shared.delivery has no CENSUS_PATH attribute yet")
    return delivery_mod


class TestNewMechanismsClassify:
    @pytest.mark.parametrize("mechanism", ["exit2_stderr", "exit_nonzero_stderr"])
    def test_a_real_classification_is_returned_not_unknown(self, mechanism) -> None:
        result = classify_delivery("Stop", mechanism)
        assert result in ("model", "debug-log", "user", "state", "rejected", "unknown")
        assert result != "unknown", (
            f"{mechanism} classified as unknown -- the table has no term for it yet "
            "(this is the fix this cycle adds)"
        )


# A regression pin on the mechanisms/events already in the table: adding the two new
# mechanisms above must not perturb any of these answers. Values are the function's
# OWN current, correct behavior (docs/reference/claude-code-blackbox.md), not invented.
_EXISTING_CASES = [
    pytest.param("UserPromptSubmit", "stdout", "model", id="userpromptsubmit-stdout"),
    pytest.param("PreToolUse", "stdout", "debug-log", id="pretooluse-stdout"),
    pytest.param(
        "PreToolUse", "permissionDecisionReason", "model", id="pretooluse-permission-reason"
    ),
    pytest.param("PreToolUse", "additionalContext", "model", id="pretooluse-additional-context"),
    pytest.param("PostCompact", "additionalContext", "rejected", id="postcompact-additional-context"),
    pytest.param("Stop", "systemMessage", "user", id="stop-system-message"),
    pytest.param("Stop", "state", "state", id="stop-state"),
    pytest.param(None, None, "unknown", id="none-none"),
]
assert len(_EXISTING_CASES) == 8


class TestPreExistingMechanismsUnchanged:
    @pytest.mark.parametrize("event,mechanism,expected", _EXISTING_CASES)
    def test_answer_is_unchanged(self, event, mechanism, expected) -> None:
        assert classify_delivery(event, mechanism) == expected


class TestDeliveryProvenance:
    def test_a_missing_census_file_is_unproven(self, tmp_path, monkeypatch) -> None:
        delivery_provenance = _delivery_provenance()
        delivery_mod = _delivery_module()
        monkeypatch.setattr(delivery_mod, "CENSUS_PATH", tmp_path / "does-not-exist.json")
        assert delivery_provenance("Stop", "exit2_stderr") == "unproven"

    def test_an_empty_census_artifact_is_unproven_for_every_declared_entry(
        self, tmp_path, monkeypatch
    ) -> None:
        delivery_provenance = _delivery_provenance()
        delivery_mod = _delivery_module()
        empty = tmp_path / "census.json"
        empty.write_text(json.dumps({"records": {}, "events_never_observed": []}))
        monkeypatch.setattr(delivery_mod, "CENSUS_PATH", empty)

        results = {
            entry.id: delivery_provenance(entry.event, entry.mechanism) for entry in REFUSALS
        }
        assert results, "no declared entries were evaluated"
        assert set(results.values()) == {"unproven"}, (
            f"an empty census artifact must make every entry unproven: {results!r}"
        )

    def test_a_populated_census_flips_a_matching_entry_to_observed(
        self, tmp_path, monkeypatch
    ) -> None:
        """SEAM CONTRACT: the artifact keys a record class as
        "<event>|<hook>|<direction>" and carries a "mechanisms" list of the
        `classify_delivery`-recognized mechanism names actually observed for it.
        This is the schema this test requires; if the implementer's
        `writ/analysis/blackbox.py` writer shapes it differently, this test and
        that writer must be reconciled in the same change."""
        delivery_provenance = _delivery_provenance()
        delivery_mod = _delivery_module()
        populated = tmp_path / "census.json"
        populated.write_text(json.dumps({
            "records": {
                "PreToolUse|writ-state-write-gate.sh|out": {
                    "count": 5,
                    "mechanisms": ["permissionDecisionReason"],
                },
            },
        }))
        monkeypatch.setattr(delivery_mod, "CENSUS_PATH", populated)

        assert delivery_provenance("PreToolUse", "permissionDecisionReason") == "observed"
        assert delivery_provenance("Stop", "exit2_stderr") == "unproven"


class TestDrillReportsUnprovenDeliverySet:
    """The drill-wide property: every declared refusal's delivery is EITHER
    reported (unproven) or asserted (observed), never silently skipped and never
    asserted without provenance."""

    def test_every_declared_entry_is_classified_and_only_observed_asserts_delivery(
        self,
    ) -> None:
        delivery_provenance = _delivery_provenance()
        unproven_ids: list[str] = []
        for entry in REFUSALS:
            provenance = delivery_provenance(entry.event, entry.mechanism)
            assert provenance in ("observed", "unproven"), (entry.id, provenance)
            if provenance == "observed":
                bucket = classify_delivery(entry.event, entry.mechanism)
                assert bucket and bucket != "unknown", (
                    f"{entry.id}: provenance is observed but classify_delivery has no "
                    f"real answer for it ({bucket!r})"
                )
            else:
                # UNPROVEN: reported by name below, never asserted on.
                unproven_ids.append(entry.id)

        # NOT `isinstance(unproven_ids, list)`, which was the assertion here and is a
        # tautology: a list is always a list, so it passed with the production code
        # reverted. The real property is that the two buckets PARTITION the declared
        # set, so a classifier that silently returned a third value, or dropped an
        # entry, fails here instead of reading as full coverage.
        observed_ids = [
            e.id for e in REFUSALS
            if delivery_provenance(e.event, e.mechanism) == "observed"
        ]
        assert sorted(observed_ids + unproven_ids) == sorted(e.id for e in REFUSALS), (
            "observed and unproven must partition the declared refusals exactly"
        )
        assert not set(observed_ids) & set(unproven_ids), (
            "an entry cannot be both observed and unproven"
        )

    def test_the_drill_passes_when_the_census_artifact_holds_zero_records(
        self, tmp_path, monkeypatch
    ) -> None:
        """A zero-record artifact makes EVERY entry unproven, and that is a PASS.

        This drives CENSUS_PATH at an empty artifact rather than reading the real
        committed one. The earlier version read the real file and asserted only
        `provenance in ("observed", "unproven")`, which is every possible value, so it
        passed no matter what: it claimed to exercise the zero-record boundary while
        the committed artifact held 1771 records and 19 of 26 entries resolved to
        observed. A boundary test that cannot fail at its own boundary is worse than no
        test, because it reads as coverage of the case nobody has actually checked.
        """
        delivery_provenance = _delivery_provenance()
        delivery_mod = _delivery_module()
        empty = tmp_path / "empty-census.json"
        empty.write_text(json.dumps({"records": {}, "events_never_observed": []}))
        monkeypatch.setattr(delivery_mod, "CENSUS_PATH", empty)

        assert REFUSALS, "no declared refusals, so this assertion would be vacuous"
        for entry in REFUSALS:
            provenance = delivery_provenance(entry.event, entry.mechanism)
            assert provenance == "unproven", (
                f"{entry.id}: a zero-record census must leave every entry unproven, "
                f"got {provenance!r}"
            )
