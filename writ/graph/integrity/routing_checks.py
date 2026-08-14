"""Routing/reachability invariants (floors, triggers, push, actions).

Moved verbatim from the former writ/graph/integrity.py (Wave 2 mixin split); methods read self._driver / self._database set by IntegrityChecker.__init__."""
from __future__ import annotations

from writ.graph.integrity._common import (
    EXPECTED_FLOORS,
    KNOWN_ACTIONS,
    WIRED_ROUTES,
    _FLOOR_NODE_LABELS,
    _GRAPH_ID_COALESCE,
)

# The two routes that let a delivery channel SELECT a node by itself, with no
# per-node key: 'semantic' admits it to the Stage-1 ranked pool
# (RulesRetrievalPipeline._routes_for), 'pull' admits it to the companion's
# prompt-keyword channel. Every other wired route needs a key on the node
# (floor_modes / action_triggers / trigger_keywords) to fire, which is why
# detect_delivery_orphans tests the keys and these two routes as one conjunction.
_SELECTING_ROUTES = ("semantic", "pull")


class RoutingChecksMixin:
    async def detect_floor_completeness(self) -> dict | None:
        """Invariant A: the floor computed from node `floor_modes` == Appendix B
        per mode (EXPECTED_FLOORS), exact set equality.

        A missing member (a floor node that lost its tag -> silently absent from
        the floor) AND a spurious member (a node wrongly tagged into a floor) both
        fail. Returns None when every mode matches, else {mode: {missing, spurious}}.
        """
        if self._driver is None:
            return None
        nid = _GRAPH_ID_COALESCE.format(v="n")
        all_floor_ids = sorted(set().union(*EXPECTED_FLOORS.values()))
        computed: dict[str, set[str]] = {m: set() for m in EXPECTED_FLOORS}
        async with self._driver.session(database=self._database) as session:
            # Corpus-presence guard: skip when the methodology floor corpus is not
            # loaded (e.g. a crafted test graph of a few rules). If NONE of the
            # expected floor nodes exist, this is not the real corpus -- mirrors
            # detect_category_reachability skipping when no Category nodes exist.
            # If the nodes EXIST but lost their floor_modes, computed stays empty
            # and the regression is still reported.
            present = await session.run(
                f"MATCH (n) WHERE {nid} IN $ids RETURN count(n) AS c", ids=all_floor_ids
            )
            if (await present.single())["c"] == 0:
                return None
            result = await session.run(
                f"MATCH (n) WHERE labels(n)[0] IN {_FLOOR_NODE_LABELS} "
                f"AND n.floor_modes IS NOT NULL RETURN {nid} AS id, n.floor_modes AS modes"
            )
            async for r in result:
                for m in (r["modes"] or []):
                    computed.setdefault(m, set()).add(r["id"])
        mismatches: dict[str, dict] = {}
        for mode, expected in EXPECTED_FLOORS.items():
            got = computed.get(mode, set())
            missing = sorted(expected - got)
            spurious = sorted(got - expected)
            if missing or spurious:
                mismatches[mode] = {"missing": missing, "spurious": spurious}
        # A floor_modes value naming an unknown mode is itself a violation.
        for mode, got in computed.items():
            if mode not in EXPECTED_FLOORS and got:
                mismatches[mode] = {"missing": [], "spurious": sorted(got)}
        return mismatches or None

    async def detect_trigger_keyword_invariant(self) -> dict | None:
        """Invariant B (1.6/1.7): two clauses on pull routing.

        (1) CONTENT-PARITY: every `trigger_keyword` must appear VERBATIM (whole
            word, case-insensitive) in the node's own trigger/statement/body/tags.
            A keyword that drifts from the content would silently pull the node on
            text the node never claims (the wrong-tag breach §3.5 warns of).
        (2) PULL-REACHABILITY: a methodology node in a pull-routed category (its
            Category.routes includes 'pull') with NO floor_modes, NO action_triggers
            AND NO trigger_keywords is unreachable -- pull is its only route and it
            has no keys. (The 0.3 orphan class, one level down.)

        Returns None when clean, else {parity_violations:[{id, keyword}],
        pull_orphans:[id]}.
        """
        if self._driver is None:
            return None
        nid = _GRAPH_ID_COALESCE.format(v="n")
        async with self._driver.session(database=self._database) as session:
            result = await session.run(
                f"MATCH (n)-[:BELONGS_TO]->(c:Category) "
                f"WHERE labels(n)[0] IN {_FLOOR_NODE_LABELS} "
                f"RETURN {nid} AS id, n.trigger AS trigger, n.statement AS statement, "
                f"n.body AS body, n.tags AS tags, n.floor_modes AS floor_modes, "
                f"n.action_triggers AS action_triggers, n.trigger_keywords AS trigger_keywords, "
                f"c.routes AS routes"
            )
            rows = [r.data() async for r in result]
        import re as _re

        parity_violations: list[dict] = []
        pull_orphans: list[str] = []
        for r in rows:
            text = " ".join([
                r.get("trigger") or "", r.get("statement") or "", r.get("body") or "",
                " ".join(r.get("tags") or []),
            ])
            kws = r.get("trigger_keywords") or []
            for kw in kws:
                if not _re.search(rf"\b{_re.escape(kw)}\b", text, _re.IGNORECASE):
                    parity_violations.append({"id": r["id"], "keyword": kw})
            routes = r.get("routes") or []
            if (
                "pull" in routes
                and not (r.get("floor_modes") or [])
                and not (r.get("action_triggers") or [])
                and not kws
            ):
                pull_orphans.append(r["id"])
        if not parity_violations and not pull_orphans:
            return None
        return {
            "parity_violations": sorted(parity_violations, key=lambda v: (v["id"], v["keyword"])),
            "pull_orphans": sorted(pull_orphans),
        }

    async def detect_push_reachability(self) -> dict | None:
        """1.8: the push analog of detect_trigger_keyword_invariant's pull half.

        (1) EMPTY-ACTION-ROUTE: a Category whose `routes` include 'action' but with
            NO retrievable methodology member declaring `action_triggers` -- the
            push route is advertised yet unreachable (the empty-route hole, the
            action analog of the 0.3 category-reachability check).
        (2) PUSH-ORPHAN: a methodology node under an action-routed Category with NO
            floor_modes, NO trigger_keywords AND NO action_triggers -- unreachable
            by the companion. Same triple-empty condition as `pull_orphan`; on a
            clean corpus the two coincide for pull-routed categories, so the unique
            coverage is action-routed-but-NOT-pull categories (e.g. CAT-COMM-001),
            which the pull-orphan clause cannot reach.

        Returns None when clean, else {empty_action_routes:[cat], push_orphans:[id]}.
        """
        if self._driver is None:
            return None
        nid = _GRAPH_ID_COALESCE.format(v="n")
        async with self._driver.session(database=self._database) as session:
            cats_res = await session.run(
                "MATCH (c:Category) WHERE 'action' IN c.routes RETURN c.category_id AS cat"
            )
            action_cats = [r["cat"] async for r in cats_res]
            members_res = await session.run(
                f"MATCH (n)-[:BELONGS_TO]->(c:Category) "
                f"WHERE 'action' IN c.routes AND labels(n)[0] IN {_FLOOR_NODE_LABELS} "
                f"RETURN c.category_id AS cat, {nid} AS id, n.floor_modes AS floor_modes, "
                f"n.action_triggers AS action_triggers, n.trigger_keywords AS trigger_keywords"
            )
            members = [r.data() async for r in members_res]
            # DEAD-TAG: action_triggers on a node the trigger index can NEVER load
            # (any label outside RETRIEVABLE_METHODOLOGY_LABELS, e.g. a Rule) is an
            # inert tag -- it parses, persists, and pushes nothing. Catch the class
            # so a tag authored on the wrong node type fails loud (1.8b).
            dead_res = await session.run(
                f"MATCH (n) WHERE n.action_triggers IS NOT NULL AND size(n.action_triggers) > 0 "
                f"AND NOT labels(n)[0] IN {_FLOOR_NODE_LABELS} "
                f"RETURN {nid} AS id"
            )
            dead_action_tags = sorted([r["id"] async for r in dead_res])
        cats_with_action: set[str] = set()
        push_orphans: list[str] = []
        for m in members:
            if m.get("action_triggers"):
                cats_with_action.add(m["cat"])
            if (
                not (m.get("floor_modes") or [])
                and not (m.get("trigger_keywords") or [])
                and not (m.get("action_triggers") or [])
            ):
                push_orphans.append(m["id"])
        empty_action_routes = sorted(c for c in action_cats if c not in cats_with_action)
        push_orphans = sorted(set(push_orphans))
        if not empty_action_routes and not push_orphans and not dead_action_tags:
            return None
        return {
            "empty_action_routes": empty_action_routes,
            "push_orphans": push_orphans,
            "dead_action_tags": dead_action_tags,
        }

    async def detect_action_vocabulary_closure(self) -> dict | None:
        """1.8: the action analog of EXPECTED_FLOORS' unknown-mode clause.

        Every `action_triggers` value across methodology nodes must be a member of
        KNOWN_ACTIONS (the actions the live push path can emit). A value outside it
        tags a node the push path can never reach -- a silent no-op, the stranded
        class. Returns None when clean, else {node_id: sorted([unknown_action])}.
        (No corpus-presence guard needed: a graph with no action_triggers yields no
        rows and returns None naturally.)
        """
        if self._driver is None:
            return None
        nid = _GRAPH_ID_COALESCE.format(v="n")
        async with self._driver.session(database=self._database) as session:
            result = await session.run(
                f"MATCH (n) WHERE labels(n)[0] IN {_FLOOR_NODE_LABELS} "
                f"AND n.action_triggers IS NOT NULL AND size(n.action_triggers) > 0 "
                f"RETURN {nid} AS id, n.action_triggers AS actions"
            )
            rows = [(r["id"], r["actions"]) async for r in result]
        violations: dict[str, list[str]] = {}
        for node_id, actions in rows:
            unknown = sorted(a for a in (actions or []) if a not in KNOWN_ACTIONS)
            if unknown:
                violations[node_id] = unknown
        return violations or None

    async def detect_route_implementation_closure(self) -> dict | None:
        """1.9: the ROUTE analog of detect_action_vocabulary_closure.

        Every value in a Category's `routes` must be a member of WIRED_ROUTES --
        the routes whose delivery mechanism is implemented. A route that is VALID
        but UNWIRED parses, persists, and delivers nothing, so no member is
        reachable BY THAT ROUTE; when it is the category's ONLY route, the members
        are undeliverable outright. That is not hypothetical: CAT-DISC-001 declared
        `ride_along` alone and stranded 26 members with no check failing, because
        each sibling invariant here is scoped to the route it was written for
        (`pull_orphans` fires only for pull-routed categories, `push_orphans` only
        for action-routed ones), so a category routed to NEITHER slipped between
        them. detect_delivery_orphans is the companion that separates the stranded
        case from a merely inert route on an otherwise wired category.

        Returns None when every Category declares only wired routes, else
        {category_id: sorted([unwired_route])}. Guards mirror
        detect_floor_completeness exactly: None when there is no driver, and SKIP
        (None) when the graph holds no Category nodes, so a crafted test graph of a
        few rules is never reported as a corpus-wide breach.
        """
        if self._driver is None:
            return None
        async with self._driver.session(database=self._database) as session:
            present = await session.run("MATCH (c:Category) RETURN count(c) AS c")
            if (await present.single())["c"] == 0:
                return None
            result = await session.run(
                "MATCH (c:Category) WHERE c.routes IS NOT NULL "
                "RETURN c.category_id AS cat, c.routes AS routes"
            )
            rows = [(r["cat"], r["routes"]) async for r in result]
        violations: dict[str, list[str]] = {}
        for cat, routes in rows:
            unwired = sorted({r for r in (routes or []) if r not in WIRED_ROUTES})
            if unwired:
                violations[cat] = unwired
        return violations or None

    async def detect_delivery_orphans(self) -> dict | None:
        """1.9: the union-scoped orphan check -- no channel can select this node.

        A Skill, Playbook, Technique or AntiPattern (the four labels the
        methodology companion can load) is UNDELIVERABLE when all five hold at
        once: no `floor_modes` (the mode floor cannot seat it), no
        `action_triggers` (no push can reach it), no `trigger_keywords` (no pull
        can match it), and its category routes contain neither `semantic` (no
        admission to the ranked pool) nor `pull`. That conjunction is the whole
        definition of undeliverable, and it is deliberately a CONJUNCTION: ONE
        live channel is sufficient, so a floor-only node (SKL-PROC-MODE-001,
        floored in conversation and work while declaring no edges) and an
        action-only node (SKL-PROC-REVRECV-001, pushed by the wired
        `review-feedback` action) are correctly silent here. Flagging them would
        make the check noise and get it switched off.

        Rules and ForbiddenResponses are out of scope by label: they reach the
        agent on the always-on injection channel (INJECTION_RULE_WHERE), which
        consults no route and no trigger key.

        Returns None when every such node has at least one channel, else
        {node_id: {label, category, routes}} -- keyed by node id, valued with the
        evidence that made it an orphan. Guards mirror detect_floor_completeness:
        None when there is no driver, SKIP (None) when the graph holds no Category
        nodes, since without categories every node would look route-less and the
        check would report the entire corpus as orphaned.
        """
        if self._driver is None:
            return None
        nid = _GRAPH_ID_COALESCE.format(v="n")
        async with self._driver.session(database=self._database) as session:
            present = await session.run("MATCH (c:Category) RETURN count(c) AS c")
            if (await present.single())["c"] == 0:
                return None
            result = await session.run(
                f"MATCH (n)-[:BELONGS_TO]->(c:Category) "
                f"WHERE labels(n)[0] IN {_FLOOR_NODE_LABELS} "
                f"RETURN {nid} AS id, labels(n)[0] AS label, "
                f"c.category_id AS cat, c.routes AS routes, "
                f"n.floor_modes AS floor_modes, n.action_triggers AS action_triggers, "
                f"n.trigger_keywords AS trigger_keywords"
            )
            rows = [r.data() async for r in result]
        # A node with more than one BELONGS_TO category is deliverable if ANY of
        # them opens a channel, so `deliverable` wins over a candidate row.
        deliverable: set[str] = set()
        candidates: dict[str, dict] = {}
        for r in rows:
            node_id = r["id"]
            routes = r.get("routes") or []
            has_channel = (
                bool(r.get("floor_modes"))
                or bool(r.get("action_triggers"))
                or bool(r.get("trigger_keywords"))
                or any(route in routes for route in _SELECTING_ROUTES)
            )
            if has_channel:
                deliverable.add(node_id)
                continue
            candidates[node_id] = {
                "label": r.get("label"),
                "category": r.get("cat"),
                "routes": sorted(routes),
            }
        orphans = {
            node_id: ev
            for node_id, ev in sorted(candidates.items())
            if node_id not in deliverable
        }
        return orphans or None
