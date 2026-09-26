"""Rule CRUD, scoring, graduation, and query methods.

Moved verbatim from the former writ/graph/db.py (Wave 2 mixin split); methods read self._driver / self._database set by Neo4jConnection.__init__."""
from __future__ import annotations

import json

from writ.frequency import (
    DEFAULT_GRADUATION_RATIO_MIN,
    DEFAULT_GRADUATION_THRESHOLD,
    evaluate_graduation,
)
from writ.graph.db._common import _node_write_spec

# FeedbackBatch replay records live this long; the client stops resending a pending batch
# well inside it (writ.session.feedback.FEEDBACK_RESEND_MAX_AGE_DAYS), so a resend always
# finds its record. Each applied batch prunes at most FEEDBACK_BATCH_PRUNE_LIMIT expired ones.
FEEDBACK_BATCH_TTL_DAYS = 30
FEEDBACK_BATCH_PRUNE_LIMIT = 100


class RuleStoreMixin:
    async def get_rule(self, rule_id: str) -> dict | None:
        """Fetch a single rule node by rule_id. Returns None if not found."""
        query = "MATCH (r:Rule {rule_id: $rule_id}) RETURN r"
        record = await self._run_single(query, rule_id=rule_id)
        if record is None:
            return None
        return dict(record["r"])

    async def create_rule(self, rule_data: dict, source_origin: str = "ingest") -> str:
        """Create or update a Rule node. Idempotent via MERGE on rule_id.

        Phase 1 Rule carries rationalization_counters (list[dict]) and nested
        structures that Neo4j can't store natively; _coerce_neo4j_value serializes
        those to JSON strings.

        source_origin (0.10) records whether this node has a markdown home: ingest
        callers leave the default 'ingest'; graph-first writers (/propose, cli add/edit)
        pass 'graph-authored'. Reconcile-on-ingest exempts graph-authored nodes from
        deletion. The kwarg wins over any value in rule_data, so a graduated node
        re-ingested from its new source file correctly flips back to 'ingest'.
        """
        query = """
            MERGE (r:Rule {rule_id: $rule_id, project: $project})
            SET r += $props
            RETURN r.rule_id AS rule_id
        """
        # Identity + props via the shared spec (B5.2) so the per-node and batch
        # ingest paths are byte-identical; the 0.10 prop-parity + M.2 composite-key
        # invariants now live in _node_write_spec.
        _, _, node_id, project, props = _node_write_spec("Rule", rule_data, source_origin)
        record = await self._run_single(
            query, rule_id=node_id, project=project, props=props
        )
        return record["rule_id"]

    async def count_rules(self) -> int:
        """Return total Rule node count."""
        query = "MATCH (r:Rule) RETURN count(r) AS count"
        record = await self._run_single(query)
        return record["count"]

    async def get_all_rules(self, project: str | None = None) -> list[dict]:
        """Fetch all Rule nodes. Returns list of property dicts.

        M.2: when `project` is given, only that project's Rules are returned;
        the default (None) spans every project and is backward-compatible with
        the pre-M.2 single-arg callers.
        """
        if project is None:
            query = "MATCH (r:Rule) RETURN r ORDER BY r.rule_id"
            params: dict = {}
        else:
            query = "MATCH (r:Rule) WHERE r.project = $project RETURN r ORDER BY r.rule_id"
            params = {"project": project}
        rows = await self._run(query, **params)
        return [dict(record["r"]) for record in rows]

    async def get_rules_by_authority(self, authority: str) -> list[dict]:
        """Fetch all Rule nodes with a given authority value."""
        query = """
            MATCH (r:Rule)
            WHERE r.authority = $authority
            RETURN r
            ORDER BY r.last_validated DESC
        """
        rows = await self._run(query, authority=authority)
        return [dict(record["r"]) for record in rows]

    async def update_rule_authority(self, rule_id: str, authority: str) -> bool:
        """Update the authority property on a Rule node. Returns True if found."""
        query = """
            MATCH (r:Rule {rule_id: $rule_id})
            SET r.authority = $authority
            RETURN r.rule_id AS rule_id
        """
        record = await self._run_single(query, rule_id=rule_id, authority=authority)
        return record is not None

    async def update_rule_confidence(self, rule_id: str, confidence: str) -> bool:
        """Update the confidence property on a Rule node. Returns True if found."""
        query = """
            MATCH (r:Rule {rule_id: $rule_id})
            SET r.confidence = $confidence
            RETURN r.rule_id AS rule_id
        """
        record = await self._run_single(query, rule_id=rule_id, confidence=confidence)
        return record is not None

    async def increment_positive(self, rule_id: str) -> bool:
        """Increment times_seen_positive and update last_seen. Returns True if found."""
        query = """
            MATCH (r:Rule {rule_id: $rule_id})
            SET r.times_seen_positive = coalesce(r.times_seen_positive, 0) + 1,
                r.last_seen = datetime()
            RETURN r.rule_id AS rule_id
        """
        record = await self._run_single(query, rule_id=rule_id)
        return record is not None

    async def increment_negative(self, rule_id: str) -> bool:
        """Increment times_seen_negative and update last_seen. Returns True if found."""
        query = """
            MATCH (r:Rule {rule_id: $rule_id})
            SET r.times_seen_negative = coalesce(r.times_seen_negative, 0) + 1,
                r.last_seen = datetime()
            RETURN r.rule_id AS rule_id
        """
        record = await self._run_single(query, rule_id=rule_id)
        return record is not None

    async def evaluate_and_flip_graduation(
        self,
        rule_id: str,
        threshold: int = DEFAULT_GRADUATION_THRESHOLD,
        ratio_min: float = DEFAULT_GRADUATION_RATIO_MIN,
    ) -> str | None:
        """6.3a: when a PROPOSED rule crosses the frequency threshold, flip its
        provenance proposed -> graduation_pending -- a CANDIDATE for the human
        promotion gate (6.3b/6.3c). Returns the new provenance when it flips, else None.

        North Star: this is the statistical crossing, NOT approval. It MUST NOT promote
        authority (stays ai-provisional until a human gates it) and MUST NOT write to
        bible/ source. Idempotent and one-directional: ONLY a 'proposed' node flips; the
        decision is delegated to frequency.evaluate_graduation (single source of truth).
        """
        async with self._driver.session(database=self._database) as session:
            read = await session.run(
                "MATCH (r:Rule {rule_id: $rule_id}) RETURN "
                "coalesce(r.times_seen_positive, 0) AS pos, "
                "coalesce(r.times_seen_negative, 0) AS neg, r.provenance AS provenance",
                rule_id=rule_id,
            )
            rec = await read.single()
            if rec is None or rec["provenance"] != "proposed":
                return None
            grad = evaluate_graduation(rec["pos"], rec["neg"], threshold, ratio_min)
            if not grad.graduated:
                return None
            # Guarded SET: only flip if still 'proposed'. Report the flip from the write
            # counter, not optimism: under a concurrent race the loser's WHERE matches 0
            # rows (the winner already flipped), so properties_set==0 -> this call did NOT
            # flip -> return None. Returning "graduation_pending" unconditionally would be
            # a false positive (the docstring's idempotency/one-flip contract). Mirrors the
            # counters.* pattern delete_rule uses.
            result = await session.run(
                "MATCH (r:Rule {rule_id: $rule_id}) WHERE r.provenance = 'proposed' "
                "SET r.provenance = 'graduation_pending'",
                rule_id=rule_id,
            )
            summary = await result.consume()
            if summary.counters.properties_set == 0:
                return None
        return "graduation_pending"

    async def apply_feedback_batch(
        self,
        signals: list[tuple[str, str]],
        threshold: int = DEFAULT_GRADUATION_THRESHOLD,
        ratio_min: float = DEFAULT_GRADUATION_RATIO_MIN,
        batch_id: str | None = None,
    ) -> dict:
        """Apply a batch of (rule_id, "positive"|"negative") signals in ONE transaction.

        The batched form of increment_positive / increment_negative plus
        evaluate_and_flip_graduation: one session, one execute_write, one UNWIND
        increment that returns the post-increment counts and provenance, then (only
        when some proposed rule crossed) one guarded UNWIND flip in the same
        transaction. A rule sent twice is counted twice, faithful to the input.

        Batch-safe: the increment's SET write-locks every touched node until commit, so
        the provenance it returns cannot change underneath this transaction, and a
        concurrent batch on the same rule blocks, then reads graduation_pending and
        flips nothing. execute_write retries a transient failure by re-running the
        whole function; a rolled-back attempt applied nothing, so a retry cannot
        double-count. Any other exception rolls back the whole batch.

        North Star, as evaluate_and_flip_graduation: never promotes authority, never
        writes bible/ source; frequency.evaluate_graduation decides the crossing.

        With `batch_id`, the same transaction first MERGEs (:FeedbackBatch {batch_id}).
        A stored result means an earlier transaction committed this batch: it is
        returned with `replayed: true` and nothing else runs. Otherwise the batch is
        applied, its result stored on the record, and up to FEEDBACK_BATCH_PRUNE_LIMIT
        records older than FEEDBACK_BATCH_TTL_DAYS are deleted. The record and the
        increments commit or roll back together, so "record exists" means "applied".
        """
        if not signals:
            return {"recorded": [], "not_found": [], "graduation_pending": [], "applied": 0}
        counts: dict[str, list[int]] = {}
        for rule_id, signal in signals:
            row = counts.setdefault(rule_id, [0, 0])
            row[0 if signal == "positive" else 1] += 1
        rows = [{"rule_id": rid, "pos": p, "neg": n} for rid, (p, n) in counts.items()]

        async def _work(tx) -> dict:
            if batch_id is not None:
                merged = await tx.run(
                    "MERGE (b:FeedbackBatch {batch_id: $batch_id}) "
                    "ON CREATE SET b.created_at = datetime() RETURN b.result AS stored",
                    batch_id=batch_id,
                )
                rec = await merged.single()
                if rec is not None and rec["stored"] is not None:
                    return {**json.loads(rec["stored"]), "replayed": True}
            result = await tx.run(
                "UNWIND $rows AS row MATCH (r:Rule {rule_id: row.rule_id}) "
                "SET r.times_seen_positive = coalesce(r.times_seen_positive, 0) + row.pos, "
                "r.times_seen_negative = coalesce(r.times_seen_negative, 0) + row.neg, "
                "r.last_seen = datetime() "
                "RETURN r.rule_id AS rule_id, r.times_seen_positive AS pos, "
                "r.times_seen_negative AS neg, r.provenance AS provenance",
                rows=rows,
            )
            recorded: list[str] = []
            crossed: list[str] = []
            async for rec in result:
                rid = rec["rule_id"]
                if rid not in recorded:
                    recorded.append(rid)
                if rec["provenance"] != "proposed" or rid in crossed:
                    continue
                if evaluate_graduation(rec["pos"], rec["neg"], threshold, ratio_min).graduated:
                    crossed.append(rid)
            flipped: list[str] = []
            if crossed:
                flip = await tx.run(
                    "UNWIND $ids AS id MATCH (r:Rule {rule_id: id}) "
                    "WHERE r.provenance = 'proposed' "
                    "SET r.provenance = 'graduation_pending' RETURN r.rule_id AS rule_id",
                    ids=crossed,
                )
                flipped = [rec["rule_id"] async for rec in flip]
            answer = {
                "recorded": recorded,
                "not_found": [rid for rid in counts if rid not in recorded],
                "graduation_pending": flipped,
                "applied": len(recorded),
            }
            if batch_id is not None:
                await tx.run(
                    "MATCH (b:FeedbackBatch {batch_id: $batch_id}) SET b.result = $result",
                    batch_id=batch_id, result=json.dumps(answer),
                )
                await tx.run(
                    "MATCH (b:FeedbackBatch) "
                    "WHERE b.created_at < datetime() - duration({days: $ttl}) "
                    "WITH b LIMIT $limit DELETE b",
                    ttl=FEEDBACK_BATCH_TTL_DAYS, limit=FEEDBACK_BATCH_PRUNE_LIMIT,
                )
            return answer

        async with self._driver.session(database=self._database) as session:
            return await session.execute_write(_work)

    async def delete_rule(self, rule_id: str) -> bool:
        """Delete a Rule node and all its edges. Returns True if a node was deleted.

        Reports the outcome from the result summary's deletion counter rather
        than `RETURN count(r)`: counting a variable bound to a node the same
        query just DETACH DELETEd is not contractually defined across Neo4j
        versions, whereas counters.nodes_deleted is the canonical signal.
        """
        query = """
            MATCH (r:Rule {rule_id: $rule_id})
            DETACH DELETE r
        """
        async with self._driver.session(database=self._database) as session:
            result = await session.run(query, rule_id=rule_id)
            summary = await result.consume()
            return summary.counters.nodes_deleted > 0

    async def count_by_authority(self) -> dict[str, int]:
        """Count rules grouped by authority value."""
        query = """
            MATCH (r:Rule)
            RETURN coalesce(r.authority, 'human') AS authority, count(r) AS count
            ORDER BY authority
        """
        async with self._driver.session(database=self._database) as session:
            result = await session.run(query)
            return {record["authority"]: record["count"] async for record in result}

    async def get_rule_statements(self, rule_ids: list[str]) -> dict[str, str]:
        """Return {rule_id: statement} for the given ids (one batched read).

        Absent ids are omitted. Used to render rule detail in PR comments without
        an N+1 per-rule fetch (PERF-BATCH-001).
        """
        if not rule_ids:
            return {}
        async with self._driver.session(database=self._database) as session:
            result = await session.run(
                "MATCH (r:Rule) WHERE r.rule_id IN $ids "
                "RETURN r.rule_id AS rule_id, r.statement AS statement",
                ids=rule_ids,
            )
            return {r["rule_id"]: (r.get("statement") or "") async for r in result}
