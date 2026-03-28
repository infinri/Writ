"""Contradiction, orphan, staleness, and redundancy detection.

Per ARCH-ORG-001: integrity logic lives here, raw queries in db.py.
Per ARCH-DI-001: receives db connection via constructor injection.
Per PY-ASYNC-001: all operations are async.
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from neo4j import AsyncDriver

# Per ARCH-CONST-001: named constants for detection thresholds.
REDUNDANCY_SIMILARITY_THRESHOLD = 0.95


class IntegrityChecker:
    """Runs integrity checks against the rule graph."""

    def __init__(self, driver: AsyncDriver, database: str = "neo4j") -> None:
        self._driver = driver
        self._database = database

    async def detect_conflicts(self) -> list[dict]:
        """Find all rule pairs connected by CONFLICTS_WITH edges."""
        query = """
            MATCH (a:Rule)-[:CONFLICTS_WITH]->(b:Rule)
            WHERE a.rule_id < b.rule_id
            RETURN a.rule_id AS rule_a, b.rule_id AS rule_b
            ORDER BY rule_a
        """
        async with self._driver.session(database=self._database) as session:
            result = await session.run(query)
            return [record.data() async for record in result]

    async def detect_orphans(self) -> list[str]:
        """Find rules with zero edges (unreachable by traversal)."""
        query = """
            MATCH (r:Rule)
            WHERE NOT (r)--()
            RETURN r.rule_id AS rule_id
            ORDER BY rule_id
        """
        async with self._driver.session(database=self._database) as session:
            result = await session.run(query)
            return [record["rule_id"] async for record in result]

    async def detect_stale(self) -> list[dict]:
        """Find rules past their staleness window."""
        today = date.today()
        query = """
            MATCH (r:Rule)
            WHERE r.last_validated IS NOT NULL
                AND r.staleness_window IS NOT NULL
            RETURN r.rule_id AS rule_id,
                   r.last_validated AS last_validated,
                   r.staleness_window AS staleness_window
        """
        async with self._driver.session(database=self._database) as session:
            result = await session.run(query)
            stale: list[dict] = []
            async for record in result:
                data = record.data()
                last_val = data["last_validated"]
                window = data["staleness_window"]
                # Neo4j stores dates as strings from our migration.
                if isinstance(last_val, str):
                    last_val = date.fromisoformat(last_val)
                expiry = last_val + timedelta(days=int(window))
                if expiry < today:
                    stale.append({
                        "rule_id": data["rule_id"],
                        "last_validated": str(last_val),
                        "expired_on": str(expiry),
                    })
            return stale

    async def detect_redundant(self) -> list[dict]:
        """Find rule pairs with near-identical trigger+statement text.

        Uses embedding cosine similarity. Requires sentence-transformers.
        """
        query = """
            MATCH (r:Rule)
            WHERE r.mandatory IS NULL OR r.mandatory = false
            RETURN r.rule_id AS rule_id,
                   r.trigger AS trigger,
                   r.statement AS statement
        """
        async with self._driver.session(database=self._database) as session:
            result = await session.run(query)
            rules = [record.data() async for record in result]

        if len(rules) < 2:
            return []

        try:
            from sentence_transformers import SentenceTransformer
        except ImportError:
            return []

        model = SentenceTransformer("all-MiniLM-L6-v2")
        texts = [f"{r['trigger']} {r['statement']}" for r in rules]
        embeddings = model.encode(texts, normalize_embeddings=True)

        redundant: list[dict] = []
        for i in range(len(rules)):
            for j in range(i + 1, len(rules)):
                similarity = float(embeddings[i] @ embeddings[j])
                if similarity >= REDUNDANCY_SIMILARITY_THRESHOLD:
                    redundant.append({
                        "rule_a": rules[i]["rule_id"],
                        "rule_b": rules[j]["rule_id"],
                        "similarity": round(similarity, 4),
                    })
        return redundant

    async def detect_confidence_defaults(self) -> list[str]:
        """List rules still at migration default confidence."""
        query = """
            MATCH (r:Rule)
            WHERE r.confidence = 'production-validated'
            RETURN r.rule_id AS rule_id
            ORDER BY rule_id
        """
        async with self._driver.session(database=self._database) as session:
            result = await session.run(query)
            return [record["rule_id"] async for record in result]

    async def check_query_rule_ratio(self, query_count: int) -> dict | None:
        """Warn if ground-truth query-to-rule ratio drops below 1:10."""
        query = "MATCH (r:Rule) WHERE r.mandatory IS NULL OR r.mandatory = false RETURN count(r) AS count"
        async with self._driver.session(database=self._database) as session:
            result = await session.run(query)
            record = await result.single()
            rule_count = record["count"]

        ratio_threshold = 10
        if rule_count > 0 and query_count * ratio_threshold < rule_count:
            return {
                "rule_count": rule_count,
                "query_count": query_count,
                "required_queries": rule_count // ratio_threshold,
                "message": f"Query set covers {query_count}/{rule_count} rules "
                           f"(need at least {rule_count // ratio_threshold} queries for 1:{ratio_threshold} ratio)",
            }
        return None

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
        """Find rules with zero frequency over rolling window."""
        query = """
            MATCH (r:Rule)
            WHERE (coalesce(r.times_seen_positive, 0) + coalesce(r.times_seen_negative, 0)) = 0
              AND (r.last_seen IS NULL
                   OR r.last_seen < datetime() - duration({days: $window_days}))
            RETURN r.rule_id AS rule_id, r.last_seen AS last_seen
            ORDER BY rule_id
        """
        async with self._driver.session(database=self._database) as session:
            result = await session.run(query, window_days=window_days)
            return [record.data() async for record in result]

    async def detect_graduation_flags(self) -> list[dict]:
        """Find rules that reached graduation threshold with ratio below minimum."""
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

    async def run_all_checks(self, skip_redundancy: bool = False) -> dict:
        """Run all integrity checks. Returns findings dict.

        Returns exit_code: 0 if clean, 1 if any findings.
        """
        findings: dict = {
            "conflicts": await self.detect_conflicts(),
            "orphans": await self.detect_orphans(),
            "stale": await self.detect_stale(),
        }

        if not skip_redundancy:
            findings["redundant"] = await self.detect_redundant()
        else:
            findings["redundant"] = []

        findings["unreviewed"] = await self.check_unreviewed_count()
        findings["frequency_stale"] = await self.detect_frequency_stale()
        findings["graduation_flags"] = await self.detect_graduation_flags()

        has_issues = any(
            findings[k] for k in ("conflicts", "orphans", "stale", "redundant")
        )
        findings["exit_code"] = 1 if has_issues else 0
        return findings
