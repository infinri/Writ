"""Artifact-freshness check (bible/abstractions.json vs live rules).

Moved verbatim from the former writ/graph/integrity.py (Wave 2 mixin split); methods read self._driver / self._database set by IntegrityChecker.__init__."""
from __future__ import annotations

from pathlib import Path


class ArtifactChecksMixin:
    async def detect_artifact_dangling_rule_ids(
        self, artifact_path: Path | None = None, project: str = "writ"
    ) -> list | None:
        """Freshness guard on bible/abstractions.json (Approach A).

        The cached abstraction artifact names rule_ids that must exist as Rule
        nodes in the graph. A rule_id absent from the graph is a dangling
        reference -- the artifact has drifted from the corpus (a rule was renamed
        or deleted without regenerating the artifact). Returns a list of
        {"rule_id", "abstraction_id"} for each dangling ref, or None when every
        rule_id resolves. When the artifact file is absent (no `writ compress`
        has run yet), returns None (skip). Default path is the repo-root
        bible/abstractions.json (DEFAULT_ABSTRACTIONS_ARTIFACT). Project-scoped
        existence mirrors the parity detectors' coalesce(project, 'writ') idiom.
        """
        if artifact_path is None:
            from writ.compression.abstractions import DEFAULT_ABSTRACTIONS_ARTIFACT

            artifact_path = DEFAULT_ABSTRACTIONS_ARTIFACT
        if not artifact_path.exists() or self._driver is None:
            return None

        # Corpus-presence guard (mirrors detect_domain_enum_invariant /
        # detect_floor_completeness): the artifact describes the real corpus,
        # so checking it against a crafted unit-test graph of a few rules would
        # false-fire every rule_id as dangling. The real corpus always carries
        # Category nodes; a crafted test graph does not. Skip when absent.
        if await self.get_category_count() == 0:
            return None

        import json as _json

        data = _json.loads(artifact_path.read_text(encoding="utf-8"))
        artifact_project = data.get("project", project)
        abstractions = data.get("abstractions", [])

        referenced: set[str] = set()
        for abst in abstractions:
            for rid in abst.get("rule_ids", []):
                referenced.add(rid)
        if not referenced:
            return None

        async with self._driver.session(database=self._database) as session:
            result = await session.run(
                "MATCH (r:Rule) WHERE coalesce(r.project, 'writ') = $project "
                "AND r.rule_id IN $ids RETURN r.rule_id AS rule_id",
                project=artifact_project,
                ids=sorted(referenced),
            )
            present = {record["rule_id"] async for record in result}

        dangling: list[dict] = []
        for abst in abstractions:
            abs_id = abst.get("abstraction_id")
            for rid in abst.get("rule_ids", []):
                if rid not in present:
                    dangling.append({"rule_id": rid, "abstraction_id": abs_id})
        return dangling or None

    async def detect_artifact_abstracts_parity(
        self, artifact_path: Path | None = None, project: str = "writ"
    ) -> dict | None:
        """Both-directions parity for the ABSTRACTS edge set (cycle 7).

        detect_artifact_dangling_rule_ids only asks whether the rule_ids the
        artifact names EXIST; it never compares edges. Meanwhile the markdown
        parity oracle is structurally blind to Abstraction (nothing under
        bible/**/*.md can author one), so after cycle 7 exempts it there, this is
        the ONLY check standing where the abstraction layer is observable: it holds
        the artifact the layer is built from.

        Compares (abstraction_id, rule_id) pairs in both directions. Returns None in
        parity, else {"stale": [...], "missing": [...]}: stale is live in the graph
        but absent from the artifact (regenerate with `writ compress`), missing is
        declared by the artifact but never materialized (re-run
        `writ import-markdown`). Its sibling's three guards apply -- artifact absent,
        driver absent, and the corpus-presence skip -- plus a fourth this check
        needs and the sibling does not: an unmaterialized layer (zero ABSTRACTS
        edges) is absence, not drift. See the guard's own comment below for why
        the corpus-presence skip cannot stand in for it.
        """
        if artifact_path is None:
            from writ.compression.abstractions import DEFAULT_ABSTRACTIONS_ARTIFACT

            artifact_path = DEFAULT_ABSTRACTIONS_ARTIFACT
        if not artifact_path.exists() or self._driver is None:
            return None
        if await self.get_category_count() == 0:
            return None

        import json as _json

        data = _json.loads(artifact_path.read_text(encoding="utf-8"))
        artifact_project = data.get("project", project)
        artifact_pairs: set[tuple[str, str]] = set()
        for abst in data.get("abstractions", []):
            abs_id = abst.get("abstraction_id")
            if not abs_id:
                continue
            for rid in abst.get("rule_ids", []):
                artifact_pairs.add((abs_id, rid))

        async with self._driver.session(database=self._database) as session:
            result = await session.run(
                "MATCH (a:Abstraction)-[:ABSTRACTS]->(r:Rule) "
                "WHERE coalesce(a.project, 'writ') = $project "
                "AND coalesce(r.project, 'writ') = $project "
                "RETURN a.abstraction_id AS abstraction_id, r.rule_id AS rule_id",
                project=artifact_project,
            )
            live_pairs = {
                (record["abstraction_id"], record["rule_id"])
                async for record in result
                if record["abstraction_id"] is not None
                and record["rule_id"] is not None
            }

        # MATERIALIZATION GUARD. An empty edge set means the layer was never
        # built, which is ABSENCE, not drift, and the two need different
        # answers. The abstraction layer is materialized by finish_import, NOT
        # by ingest_path, so `clear_all()` followed by `ingest_path(bible)` --
        # what most corpus fixtures do, and what a fresh checkout looks like
        # before the first `writ compress` -- leaves zero ABSTRACTS edges while
        # the artifact still declares 186. Without this, every such graph
        # reports the whole artifact as missing and gates the build.
        #
        # The category-presence guard above does not cover it: ingest_path
        # writes all 22 Category nodes from markdown, so categories are present
        # exactly when abstractions are not. This is the same tradeoff the
        # sibling check already accepts, and it costs the one case a
        # never-materialized layer cannot be told apart from: wholesale
        # deletion of every edge at once. Partial loss still reports, because
        # any surviving edge puts us back on the comparison path.
        if not live_pairs:
            return None

        stale = sorted(live_pairs - artifact_pairs)
        missing = sorted(artifact_pairs - live_pairs)
        if not stale and not missing:
            return None
        return {"stale": stale, "missing": missing}
