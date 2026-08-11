"""Project registry (create_project is the TOCTOU/CAS keystone).

Moved verbatim from the former writ/graph/db.py (Wave 2 mixin split); methods read self._driver / self._database set by Neo4jConnection.__init__."""
from __future__ import annotations

import time

from writ.graph.db._common import (
    ProjectIdentityConflict,
)

# How long a cached :Project registry is trusted. Bounds the staleness a
# create_project in ANOTHER process leaves behind (that call cannot invalidate this
# process's cache), while keeping the steady-state cost of resolution at zero Neo4j
# round trips per request. Registration is rare and resolution is per prompt, so the
# ratio is what makes a cache correct here at all.
PROJECTS_CACHE_TTL_S = 30.0


class ProjectStoreMixin:
    async def create_project(
        self, name: str, repo_root: str, bible_root: str, remote_url: str | None = None
    ) -> str:
        """Register (or update) a project atomically. Idempotent via MERGE on name.

        remote_url is additive cross-clone identity (the dedup key); the scope key
        stays = name (M.1-compatible). A None remote_url argument never nulls an
        existing value, so a 3-arg call preserves any registered remote_url.

        C4 (audit): a single MERGE + guarded conditional SET + result read, replacing
        the former read-then-separate-MERGE check-then-act race (two concurrent
        conflicting creates could both pass the read check, and the loser's
        unconditional SET silently overwrote the winner). remote_url is set ONLY when
        a non-null arg is given AND the stored value is currently NULL
        (first-writer-wins). The unconditional SET of repo_root/bible_root forces the
        node write-lock BEFORE the FOREACH reads p.remote_url (the _cas_lock keystone),
        and the RETURNed stored remote_url drives the ProjectIdentityConflict raise, so
        a concurrent conflicting create raises rather than silently overwriting. The
        project_name_unique constraint (apply_constraints) serializes concurrent MERGE
        creates so the loser MATCHES the winner's committed node.
        """
        rec = await self._run_single(
            "MERGE (p:Project {name: $name}) "
            "SET p.repo_root = $repo_root, p.bible_root = $bible_root "
            "FOREACH (_ IN CASE WHEN $remote_url IS NOT NULL AND p.remote_url IS NULL "
            "THEN [1] ELSE [] END | SET p.remote_url = $remote_url) "
            "RETURN p.name AS name, p.remote_url AS remote_url",
            name=name, repo_root=repo_root, bible_root=bible_root,
            remote_url=remote_url,
        )
        # A registration changes the answer resolve_project_for_cwd gives, so the
        # cached registry below is dropped here. Without this a project registered
        # inside the daemon would stay invisible to every later resolution in the
        # same process, which reads as "unregistered cwd" and silently degrades
        # /recall to an empty briefing.
        self._projects_cache = None
        self._projects_cache_at = 0.0
        stored_remote = rec["remote_url"]
        if (
            remote_url is not None
            and stored_remote is not None
            and stored_remote != remote_url
        ):
            raise ProjectIdentityConflict(
                f"project {name!r} is already bound to remote_url "
                f"{stored_remote!r}; refusing to overwrite with {remote_url!r}"
            )
        return rec["name"]

    async def get_projects(self) -> list[dict]:
        """All registered projects as {name, repo_root, bible_root, remote_url}."""
        rows = await self._run(
            "MATCH (p:Project) RETURN p.name AS name, p.repo_root AS repo_root, "
            "p.bible_root AS bible_root, p.remote_url AS remote_url ORDER BY name"
        )
        return [dict(r) for r in rows]

    async def resolve_project_for_cwd(self, cwd: str) -> str:
        """Map a working directory to its project by longest repo_root prefix.

        A cwd under a registered project's repo_root resolves to that project;
        the longest matching repo_root wins (nested repos).

        An unregistered cwd resolves to "" (NO project), never to a default. The
        former default was the literal "writ", which labelled another project's
        query as this one's: retrieval scoped it to Writ's own records, `/recall`
        read back Writ's decisions for it, and the CLI reported Writ's project name
        for a repo that had never been registered. Every caller must branch on the
        empty answer and degrade explicitly (retrieval to doctrine-only, `/recall`
        to an empty briefing with a logged reason, the CLI to a plain message).

        THE REGISTRY READ IS CACHED IN THIS PROCESS, and the cache is read INLINE
        here rather than through a helper method: this function is also called
        unbound (`ProjectStoreMixin.resolve_project_for_cwd(stub, cwd)`) by callers
        that supply only get_projects, so a `self._helper()` call would raise
        AttributeError on them. getattr with a default is for the same reason: the
        attributes are created on first use, not in an __init__ this mixin does not own.

        Resolution sits on the hottest path in the system (every /query, and so every
        prompt), and it used to pay a Neo4j round trip per request to read a handful
        of rows that change only when a project is registered. create_project above
        drops the cache; PROJECTS_CACHE_TTL_S bounds the OTHER staleness, a
        registration by a DIFFERENT process (the git post-commit capture, `writ
        harvest`, the CLI), which cannot invalidate this process's copy and would
        otherwise stay invisible to a long-lived daemon until it restarted.
        """
        now = time.monotonic()
        projects = getattr(self, "_projects_cache", None)
        if projects is None or (now - getattr(self, "_projects_cache_at", 0.0)) >= PROJECTS_CACHE_TTL_S:
            projects = await self.get_projects()
            self._projects_cache = projects
            self._projects_cache_at = now
        best_name, best_len = "", -1
        for p in projects:
            root = p.get("repo_root") or ""
            if root and (cwd == root or cwd.startswith(root.rstrip("/") + "/")):
                if len(root) > best_len:
                    best_name, best_len = p["name"], len(root)
        return best_name
