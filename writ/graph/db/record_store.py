"""Decision-memory records (Decision/FileChange/Commit).

Moved verbatim from the former writ/graph/db.py (Wave 2 mixin split); methods read self._driver / self._database set by Neo4jConnection.__init__."""
from __future__ import annotations

from writ.graph.schema import Chunk, Commit, Decision, Document, FileChange, TrustEvent
from writ.graph.db._common import RECORD_ID_FIELDS, _coerce_neo4j_value, _now_iso


class RecordStoreMixin:
    async def _create_record(self, model, id_field: str) -> str:
        """MERGE a decision-memory record node and return its id.

        Shared by create_decision/create_filechange/create_commit, which differ
        only by (label, id_field) and their pre-build ts handling. label and
        id_field are code-controlled (a model class name and a fixed literal),
        never user input, so interpolating them into the Cypher is safe -- the
        same controlled interpolation create_methodology_node uses; the values
        still pass as $params. label is type(model).__name__, which must equal
        the model's Neo4j node label (holds for Decision/FileChange/Commit); a
        future caller whose class name diverges from its label must not use this.
        """
        props = {
            k: _coerce_neo4j_value(v) for k, v in model.model_dump().items()
        }
        props["provenance"] = "record"
        props["source_origin"] = "graph-authored"
        label = type(model).__name__
        record = await self._write_single(
            f"MERGE (n:{label} {{{id_field}: ${id_field}, project: $project}}) "
            "SET n += $props "
            f"RETURN n.{id_field} AS {id_field}",
            **{id_field: getattr(model, id_field), "project": model.project, "props": props},
        )
        return record[id_field]

    async def create_decision(self, **decision_data) -> str:
        """Create or update a Decision record. Idempotent via MERGE on (decision_id, project)."""
        return await self._create_record(Decision(**decision_data), RECORD_ID_FIELDS[Decision.__name__])

    async def create_filechange(self, **filechange_data) -> str:
        """Create or update a FileChange record. Idempotent via MERGE on (change_id, project)."""
        filechange_data.setdefault("ts", _now_iso())
        return await self._create_record(FileChange(**filechange_data), RECORD_ID_FIELDS[FileChange.__name__])

    async def create_commit(self, **commit_data) -> str:
        """Create or update a Commit record. Idempotent via MERGE on (commit_hash, project)."""
        commit_data.setdefault("ts", _now_iso())
        return await self._create_record(Commit(**commit_data), RECORD_ID_FIELDS[Commit.__name__])

    async def create_trust_event(self, **event_data) -> str:
        """Create or update a TrustEvent record. Idempotent via MERGE on (event_id, project)."""
        event_data.setdefault("ts", _now_iso())
        return await self._create_record(TrustEvent(**event_data), RECORD_ID_FIELDS[TrustEvent.__name__])

    async def create_memory(
        self,
        *,
        name: str,
        project: str,
        description: str = "",
        type: str = "",
        body: str = "",
        links: list | None = None,
        path: str = "",
        session_id: str = "",
        updated_at: str = "",
        status: str = "live",
    ) -> str:
        """Create or update a Memory record. Idempotent via MERGE on (name, project).

        The auto-memory mirror: one node per memory FILE, so an edit of that file
        re-MERGEs the same node instead of appending a second one.

        Written with explicit props instead of through _create_record because Memory
        deliberately has NO schema model: it must stay out of NodeType /
        NODE_TYPE_MODELS / NODE_ID_FIELDS so no retrieval or parity path can treat a
        memory as a rule candidate. provenance='record' is what exempts it from
        reconcile/prune, exactly as for Decision/FileChange/Commit. `type` mirrors
        the graph property name (user/feedback/project/reference).
        """
        props = {
            "description": description,
            "type": type,
            "body": body,
            "links": [str(link) for link in (links or [])],
            "path": path,
            "session_id": session_id,
            "updated_at": updated_at or _now_iso(),
            "status": status or "live",
            "provenance": "record",
            "source_origin": "graph-authored",
        }
        record = await self._write_single(
            "MERGE (m:Memory {name: $name, project: $project}) "
            "SET m += $props "
            "RETURN m.name AS name",
            name=name, project=project, props=props,
        )
        return record["name"]

    async def list_memories(
        self, project: str, include_deleted: bool = False
    ) -> list[dict]:
        """The project's Memory records, most-recently-updated first.

        Tombstoned records (status='deleted') are excluded unless include_deleted:
        a memory whose file the user deleted must stop reading as live. A separate
        project-scoped read, never /query -- Memory is excluded from
        RETRIEVABLE_NODE_TYPES and must never enter the RAG pipeline.
        """
        rows = [dict(r) for r in await self._run(
            "MATCH (m:Memory {project: $project}) "
            "WHERE $include_deleted OR coalesce(m.status, 'live') <> 'deleted' "
            "RETURN m.name AS name, m.description AS description, m.type AS type, "
            "m.status AS status, m.path AS path, m.links AS links, "
            "m.updated_at AS updated_at "
            "ORDER BY m.updated_at DESC",
            project=project, include_deleted=include_deleted,
        )]
        for row in rows:
            row["links"] = row.get("links") or []
        return rows

    async def list_all_memories(self) -> list[dict]:
        """Every Memory node, across every project. Read-only, one entry per node.

        The audit read behind `writ memory audit`, which has to see the whole
        Memory population at once: a memory filed under the WRONG project is
        invisible to `list_memories`, because that read is project-scoped and so
        can only ever confirm what a node already claims about itself.

        Deliberately unfiltered and unparameterized: tombstoned nodes are
        included (a mis-filed tombstone is still mis-filed), and the properties
        come back VERBATIM with no coalesce, because an audit must be able to
        tell a missing `project`/`path` from a defaulted one.
        """
        return [dict(r) for r in await self._run(
            "MATCH (m:Memory) "
            "RETURN m.name AS name, m.project AS project, m.path AS path, "
            "m.status AS status, m.type AS type, m.updated_at AS updated_at "
            "ORDER BY m.project, m.name",
        )]

    async def tombstone_missing_memories(
        self, project: str, existing_names
    ) -> int:
        """Tombstone every live Memory in `project` whose file is gone. Returns the count.

        The deletion reconciler behind `writ memory backfill`: `existing_names` is the
        set of memory names still present on disk, so anything else is a file the user
        deleted. The node is RETAINED with status='deleted' (audit trail), never
        hard-deleted. Idempotent: an already-tombstoned node is not re-matched.

        An EMPTY existing_names tombstones the project's whole live set, which is only
        correct when the memory directory was actually read and found empty -- callers
        must not call this after a failed listing.
        """
        names = sorted({str(n) for n in (existing_names or [])})
        record = await self._run_single(
            "MATCH (m:Memory {project: $project}) "
            "WHERE NOT m.name IN $names AND coalesce(m.status, 'live') <> 'deleted' "
            "SET m.status = 'deleted', m.tombstoned_at = $ts "
            "RETURN count(m) AS tombstoned",
            project=project, names=names, ts=_now_iso(),
        )
        return record["tombstoned"] if record else 0

    @staticmethod
    def _parse_planned_files(raw) -> list[dict]:
        """Parse the planned_files property into a list[dict].

        An empty list is stored natively (the _coerce_neo4j_value `and v` guard
        means [] never becomes the string '[]'), so handle both the JSON-string
        and the native-list forms.
        """
        import json
        if isinstance(raw, str):
            try:
                parsed = json.loads(raw)
            except (ValueError, TypeError):
                return []
        elif isinstance(raw, list):
            parsed = raw
        else:
            return []
        return [c for c in parsed if isinstance(c, dict)]

    async def get_open_decisions_for_path(
        self, project: str, path: str
    ) -> list[dict]:
        """Decisions in `project` with an OPEN claim on `path`, most-recent first.

        A Decision matches when any planned_files entry has the given path and
        resolved is False. Results are the Decision node dicts, sorted by ts
        descending. A Decision with empty planned_files never matches.
        """
        rows = [dict(r) for r in await self._run(
            "MATCH (d:Decision {project: $project}) "
            "RETURN d.decision_id AS decision_id, d.planned_files AS planned_files, "
            "d.governing_rule_ids AS governing_rule_ids, d.ts AS ts",
            project=project,
        )]

        matches = []
        for row in rows:
            claims = self._parse_planned_files(row.get("planned_files"))
            if any(
                c.get("path") == path and c.get("resolved") is False
                for c in claims
            ):
                matches.append(row)
        matches.sort(key=lambda r: r.get("ts") or "", reverse=True)
        return matches

    async def resolve_file_claims(self, project: str, path: str) -> int:
        """Flip resolved False->True on every Decision claim for `path` in `project`.

        Read-modify-write on the JSON blob: for each matching Decision, set only
        the matching-path entries to resolved=True (never the reverse, so a re-run
        is a no-op), re-encode, and persist. Returns the number of Decisions
        updated.
        """
        import json
        async with self._driver.session(database=self._database) as session:
            result = await session.run(
                "MATCH (d:Decision {project: $project}) "
                "RETURN d.decision_id AS decision_id, d.planned_files AS planned_files",
                project=project,
            )
            rows = [dict(r) async for r in result]

            updated = 0
            for row in rows:
                claims = self._parse_planned_files(row.get("planned_files"))
                changed = False
                for claim in claims:
                    if claim.get("path") == path and claim.get("resolved") is False:
                        claim["resolved"] = True
                        changed = True
                if not changed:
                    continue
                await session.run(
                    "MATCH (d:Decision {decision_id: $decision_id, project: $project}) "
                    "SET d.planned_files = $planned_files",
                    decision_id=row["decision_id"], project=project,
                    planned_files=json.dumps(claims),
                )
                updated += 1
        return updated

    async def get_latest_filechange_per_path(
        self, project: str, paths: list[str]
    ) -> dict[str, dict]:
        """Return the most-recent FileChange reason per path for `project`.

        One batched, index-backed read (filechange_project_path) over the given
        paths. For a path with multiple FileChange records the latest by `ts`
        wins: ORDER BY n.ts DESC then collect(n)[0] (Community Edition has no
        APOC, so no apoc.agg.first). The caller passes ALREADY-NORMALIZED paths so
        the IN-list join cannot silently miss. Returns
        {path -> {reason, change_type, commit_hash, ts}} for MATCHED paths only;
        an unmatched path is simply absent (skipped, no comment).
        """
        if not paths:
            return {}
        rows = [dict(r) for r in await self._run(
            "MATCH (n:FileChange) "
            "WHERE n.project = $project AND n.path IN $paths "
            "WITH n ORDER BY n.ts DESC "
            "WITH n.path AS path, collect(n)[0] AS latest "
            "OPTIONAL MATCH (c:Commit {commit_hash: latest.commit_hash, project: $project}) "
            "RETURN path, latest.reason AS reason, "
            "latest.change_type AS change_type, "
            "latest.commit_hash AS commit_hash, latest.ts AS ts, "
            "latest.queried_rule_ids AS queried_rule_ids, "
            "latest.cited_rule_ids AS cited_rule_ids, "
            "c.subject AS commit_subject",
            project=project, paths=paths,
        )]
        return {
            row["path"]: {
                "reason": row.get("reason"),
                "change_type": row.get("change_type"),
                "commit_hash": row.get("commit_hash"),
                "ts": row.get("ts"),
                "queried_rule_ids": row.get("queried_rule_ids") or [],
                "cited_rule_ids": row.get("cited_rule_ids") or [],
                "commit_subject": row.get("commit_subject"),
            }
            for row in rows
        }

    async def get_decisions_for_paths(
        self, project: str, paths: list[str], per_path: int = 1
    ) -> dict[str, list[dict]]:
        """The Decisions behind each path's most recent MOTIVATED_BY-linked FileChanges.

        One batched, index-backed read (filechange_project_path) modeled on
        get_latest_filechange_per_path: FileChange -[MOTIVATED_BY]-> Decision, both ends
        scoped to `project`, newest change first (ties by change id, descending), at most
        `per_path` changes per path, each joined to its Commit for the subject. A change with no edge never matches, so an
        unplanned fix-up after a planned change does not hide the decision. The caller
        passes ALREADY-NORMALIZED paths. Returns {path -> [decision fields plus reason,
        change_ts, commit_hash, commit_subject]}, deduped by decision_id keeping the most
        recent change; unmatched paths are absent and empty paths return {} unread.
        """
        if not paths:
            return {}
        rows = [dict(r) for r in await self._run(
            "MATCH (n:FileChange)-[:MOTIVATED_BY]->(d:Decision) "
            "WHERE n.project = $project AND n.path IN $paths AND d.project = $project "
            "WITH n, d ORDER BY n.ts DESC, n.change_id DESC "
            "WITH n.path AS path, collect({n: n, d: d})[0..$per_path] AS hits "
            "UNWIND hits AS h "
            "WITH path, h.n AS n, h.d AS d "
            "OPTIONAL MATCH (c:Commit {commit_hash: n.commit_hash, project: $project}) "
            "RETURN path, d.decision_id AS decision_id, d.title AS title, "
            "d.rationale AS rationale, d.planned_files AS planned_files, "
            "d.governing_rule_ids AS governing_rule_ids, d.phase AS phase, "
            "d.ts AS decision_ts, n.reason AS reason, n.ts AS change_ts, "
            "n.commit_hash AS commit_hash, c.subject AS commit_subject "
            "ORDER BY path, change_ts DESC, n.change_id DESC",
            project=project, paths=paths, per_path=per_path,
        )]
        out: dict[str, list[dict]] = {}
        for row in rows:
            hits = out.setdefault(row["path"], [])
            if any(h["decision_id"] == row.get("decision_id") for h in hits):
                continue
            hits.append({
                "decision_id": row.get("decision_id"),
                "title": row.get("title"),
                "rationale": row.get("rationale"),
                "planned_files": self._parse_planned_files(row.get("planned_files")),
                "governing_rule_ids": row.get("governing_rule_ids") or [],
                "phase": row.get("phase"),
                "decision_ts": row.get("decision_ts"),
                "reason": row.get("reason"),
                "change_ts": row.get("change_ts"),
                "commit_hash": row.get("commit_hash"),
                "commit_subject": row.get("commit_subject"),
            })
        return out

    async def get_recent_decisions(
        self, project: str, limit: int = 20
    ) -> list[dict]:
        """The most-recent Decisions in `project`, newest first (Phase 2 recall).

        One project-scoped, ts-ordered read backed by the decision_project index.
        planned_files is a JSON STRING in Community Edition (no APOC), so it is
        parsed Python-side with _parse_planned_files. Returns decision dicts with
        the fields recall renders: decision_id, title, rationale, planned_files
        (parsed), governing_rule_ids, phase, ts. Recall is intentionally a
        SEPARATE query, not /query: Decision is excluded from
        RETRIEVABLE_NODE_TYPES and must never enter the RAG pipeline.
        """
        rows = [dict(r) for r in await self._run(
            "MATCH (d:Decision {project: $project}) "
            "RETURN d.decision_id AS decision_id, d.title AS title, "
            "d.rationale AS rationale, d.planned_files AS planned_files, "
            "d.governing_rule_ids AS governing_rule_ids, "
            "d.phase AS phase, d.ts AS ts "
            "ORDER BY d.ts DESC LIMIT $limit",
            project=project, limit=limit,
        )]
        for row in rows:
            row["planned_files"] = self._parse_planned_files(
                row.get("planned_files")
            )
            row["governing_rule_ids"] = row.get("governing_rule_ids") or []
        return rows

    async def get_document_hashes(self, project: str) -> dict[str, dict]:
        """doc_id -> {source_hash, kind} for every Document of `project` (program item 5)."""
        doc_id = RECORD_ID_FIELDS["Document"]
        rows = await self._run(
            f"MATCH (d:Document {{project: $project}}) "
            f"RETURN d.{doc_id} AS doc_id, d.source_hash AS source_hash, d.kind AS kind",
            project=project,
        )
        return {r["doc_id"]: {"source_hash": r["source_hash"], "kind": r["kind"]} for r in rows}

    async def replace_document(self, project: str, document: Document, chunks: list[Chunk]) -> int:
        """Write one Document and exactly `chunks` as its chunk set, in ONE statement.

        MERGE the Document and lock it with SET (a concurrent replace of the same file waits
        here), delete every previous chunk of the document, create the new chunks with their
        CONTAINS edges, then the PRECEDES chain by ordinal. The UNWIND sits in a subquery so a
        zero-chunk document still writes. Returns the number of chunks written.
        """
        doc_id = RECORD_ID_FIELDS["Document"]
        props = {k: _coerce_neo4j_value(v) for k, v in document.model_dump().items()}
        rows = [{k: _coerce_neo4j_value(v) for k, v in c.model_dump().items()} for c in chunks]
        await self._write_single(
            f"MERGE (d:Document {{{doc_id}: $doc_id, project: $project}}) "
            "SET d += $props "
            "WITH d "
            "CALL { WITH d "
            f"  MATCH (old:Chunk {{project: $project, {doc_id}: $doc_id}}) DETACH DELETE old }} "
            "CALL { WITH d "
            "  UNWIND $chunks AS row CREATE (c:Chunk) SET c = row CREATE (d)-[:CONTAINS]->(c) } "
            "CALL { WITH d "
            "  MATCH (d)-[:CONTAINS]->(a:Chunk), (d)-[:CONTAINS]->(b:Chunk) "
            "  WHERE b.ordinal = a.ordinal + 1 CREATE (a)-[:PRECEDES]->(b) } "
            f"RETURN d.{doc_id} AS doc_id",
            doc_id=getattr(document, doc_id), project=project, props=props, chunks=rows,
        )
        return len(chunks)

    async def delete_documents(self, project: str, doc_ids: list[str]) -> int:
        """DETACH DELETE the named Documents of `project` and their chunks, in one statement.
        Returns the number of nodes removed; an empty list touches nothing."""
        if not doc_ids:
            return 0
        doc_id = RECORD_ID_FIELDS["Document"]
        record = await self._write_single(
            "MATCH (n) WHERE (n:Document OR n:Chunk) AND n.project = $project "
            f"AND n.{doc_id} IN $doc_ids "
            "DETACH DELETE n RETURN count(n) AS removed",
            project=project, doc_ids=list(doc_ids),
        )
        return int(record["removed"]) if record else 0
