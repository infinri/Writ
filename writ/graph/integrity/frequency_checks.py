"""Frequency/telemetry & retrieval-reachability invariants.

Moved verbatim from the former writ/graph/integrity.py (Wave 2 mixin split); methods read self._driver / self._database set by IntegrityChecker.__init__."""
from __future__ import annotations

from writ.graph.integrity._common import (
    INJECTION_RULE_WHERE,
    RANKED_INCLUDE_WHERE,
    _ALWAYS_ON_CAP,
    estimate_tokens,
)


async def mandatory_rule_ids(session) -> set[str]:
    """Live rule ids authored `mandatory=true`: the obligation set the always-on channel must cover.

    A superseded mandatory rule is no longer an obligation (program item 6), so it is
    omitted. One read for every caller: detect_stranded_mandatory and
    detect_ranked_exclusion_mismatch below, the benchmark's channel split
    (benchmarks/bench_targets.py) and scripts/measure_retrieval.py. Runs on the caller's
    open session, so it adds no connection of its own.
    """
    result = await session.run(
        "MATCH (r:Rule) WHERE r.mandatory = true AND coalesce(r.superseded, false) = false "
        "RETURN r.rule_id AS id"
    )
    return {record["id"] async for record in result}


async def superseded_rule_ids(session) -> set[str]:
    """Rule ids flagged superseded: excluded from both retrieval channels (program item 6)."""
    result = await session.run(
        "MATCH (r:Rule) WHERE r.superseded = true RETURN r.rule_id AS id"
    )
    return {record["id"] async for record in result}


class FrequencyChecksMixin:
    async def detect_stranded_mandatory(
        self, injection_where: str = INJECTION_RULE_WHERE
    ) -> list[str]:
        """List mandatory rules NOT reachable by the always-on injection path.

        Invariant (WRIT-BLUEPRINT 3.5): every `mandatory=true` rule MUST be
        injected. Mandatory rules are excluded from the ranked pool, so injection
        is their only path to the agent; one reachable by neither is the
        29-stranded bug class. The obligation set is the authored `mandatory=true`
        data (the fixed anchor); the injection set is whatever the shared
        INJECTION_RULE_WHERE predicate selects. `injection_where` is a parameter
        (defaulting to the production constant) so the check is testable against
        any predicate and never collapses to predicate-minus-itself: feed it the
        old `r.always_on = true` and it reports the 29 stranded; feed it the union
        and it reports none.
        """
        if self._driver is None:
            return []
        async with self._driver.session(database=self._database) as session:
            mandatory = await mandatory_rule_ids(session)
            result = await session.run(
                f"MATCH (r:Rule) WHERE {injection_where} RETURN r.rule_id AS id"
            )
            injected = {record["id"] async for record in result}
        return sorted(mandatory - injected)

    async def detect_ranked_exclusion_mismatch(
        self, ranked_include_where: str = RANKED_INCLUDE_WHERE
    ) -> dict | None:
        """Assert `{excluded-from-ranked} == {mandatory} | {superseded}`.

        The ranked retrieval pool (pipeline load / BM25 / vector) includes rules
        matching RANKED_INCLUDE_WHERE; its complement over all Rules is the
        excluded set, which MUST equal the live mandatory set plus the superseded
        set. The two result keys keep their names; "mandatory" in them means that
        expected set. If they diverge (for example, the pool ever keys exclusion
        on `always_on`/`severity` instead of `mandatory`), a non-mandatory rule is
        silently dropped from ranking or a mandatory rule is ranked. Returns None when equal, else the two-sided
        difference.
        """
        if self._driver is None:
            return None
        async with self._driver.session(database=self._database) as session:
            result = await session.run("MATCH (r:Rule) RETURN r.rule_id AS id")
            all_rules = {record["id"] async for record in result}
            result = await session.run(
                f"MATCH (r:Rule) WHERE {ranked_include_where} RETURN r.rule_id AS id"
            )
            ranked_included = {record["id"] async for record in result}
            mandatory = await mandatory_rule_ids(session) | await superseded_rule_ids(session)
        excluded_from_ranked = all_rules - ranked_included
        if excluded_from_ranked == mandatory:
            return None
        return {
            "excluded_not_mandatory": sorted(excluded_from_ranked - mandatory),
            "mandatory_not_excluded": sorted(mandatory - excluded_from_ranked),
        }

    async def detect_always_on_budget_breach(
        self,
        cap: int = _ALWAYS_ON_CAP,
        injection_where: str = INJECTION_RULE_WHERE,
    ) -> dict | None:
        """Assert the summary-rendered injection bundle stays under the cap.

        Cap-compliance rests on the summary render (trigger+statement); a refactor
        to full-prose render breaches (~205% of cap at today's corpus) and corpus
        growth breaches near ~54 rules. This makes the measurement an
        authoring-time guard. Returns None when under cap, else the overage.
        """
        if self._driver is None:
            return None
        async with self._driver.session(database=self._database) as session:
            result = await session.run(
                f"MATCH (r:Rule) WHERE {injection_where} "
                "RETURN r.trigger AS trigger, r.statement AS statement"
            )
            rows = [record.data() async for record in result]
        total = sum(estimate_tokens(x.get("trigger"), x.get("statement")) for x in rows)
        if total < cap:
            return None
        return {"total_tokens": total, "cap": cap, "rule_count": len(rows)}

    async def check_unreviewed_count(
        self,
        warning_percentage: float = 0.10,
        warning_floor: int = 5,
    ) -> dict | None:
        """Warn if unreviewed AI-provisional rules exceed threshold.

        Threshold: max(warning_floor, warning_percentage * total_rules).
        Returns warning dict if exceeded, None otherwise.
        """
        total_query = "MATCH (r:Rule) RETURN count(r) AS total"
        unreviewed_query = """
            MATCH (r:Rule)
            WHERE r.authority = 'ai-provisional'
            RETURN count(r) AS unreviewed
        """
        async with self._driver.session(database=self._database) as session:
            total_result = await session.run(total_query)
            total_record = await total_result.single()
            total = total_record["total"]

            unreviewed_result = await session.run(unreviewed_query)
            unreviewed_record = await unreviewed_result.single()
            unreviewed = unreviewed_record["unreviewed"]

        if unreviewed == 0:
            return None

        threshold = max(warning_floor, int(warning_percentage * total))
        if unreviewed >= threshold:
            return {
                "unreviewed": unreviewed,
                "total": total,
                "threshold": threshold,
                "message": f"{unreviewed} unreviewed AI-provisional rules "
                           f"(threshold: {threshold})",
            }
        return None

    async def detect_frequency_stale(self, window_days: int = 90) -> list[dict]:
        """Find rules with zero observed frequency: times_seen_positive + times_seen_negative == 0.

        The feedback-only telemetry timestamp (set by mark_seen_*) is absent on any rule with zero
        observations, so the former age clause that referenced it was both unreachable behind this
        times-seen==0 gate and a source of Neo4j UnknownPropertyKey warnings; it was dropped (FIX-3).
        `window_days` is retained for signature/CLI compatibility.
        """
        query = """
            MATCH (r:Rule)
            WHERE (coalesce(r.times_seen_positive, 0) + coalesce(r.times_seen_negative, 0)) = 0
            RETURN r.rule_id AS rule_id
            ORDER BY rule_id
        """
        return [record.data() for record in await self._run(query)]

    async def detect_graduation_flags(self) -> list[dict]:
        """Find rules that reached graduation threshold with ratio below minimum.

        These rules need human review per evolution plan Phase 4c.
        """
        from writ.frequency import (
            DEFAULT_GRADUATION_RATIO_MIN,
            DEFAULT_GRADUATION_THRESHOLD,
            evaluate_graduation,
        )

        query = """
            MATCH (r:Rule)
            WHERE (coalesce(r.times_seen_positive, 0) + coalesce(r.times_seen_negative, 0))
                  >= $threshold
            RETURN r.rule_id AS rule_id,
                   coalesce(r.times_seen_positive, 0) AS pos,
                   coalesce(r.times_seen_negative, 0) AS neg
        """
        async with self._driver.session(database=self._database) as session:
            result = await session.run(
                query, threshold=DEFAULT_GRADUATION_THRESHOLD,
            )
            records = [record.data() async for record in result]

        flagged: list[dict] = []
        for rec in records:
            grad = evaluate_graduation(
                rec["pos"], rec["neg"],
                DEFAULT_GRADUATION_THRESHOLD,
                DEFAULT_GRADUATION_RATIO_MIN,
            )
            if grad.flagged:
                flagged.append({
                    "rule_id": rec["rule_id"],
                    "ratio": round(grad.ratio, 4),
                    "n": grad.n,
                })
        return flagged
