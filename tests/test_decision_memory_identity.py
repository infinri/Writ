"""Decision Memory Phase 1b: project-identity substrate.

Test skeleton for the capability gate defined in capabilities.md and plan.md.
Every test in this file is RED until the implementer builds the substrate.

Run interpreter: .venv/bin/python -m pytest (has onnxruntime; system python3 errors
on embedding imports).

Neo4j-gated tests use the same db_clean fixture/skip pattern as
test_decision_memory_records.py:97-115. Pure-Python tests (caps 3, 4, 5) run without
Neo4j and will be RED on ImportError for the new modules.

Capability map:
  Cap 1 -- remote_url round-trips through create_project / get_projects (Neo4j)
  Cap 2 -- create_project without remote_url backward-compat sentinel (Neo4j)
  Cap 3 -- normalize_remote_url is 1:1 and clone-stable (pure Python)
  Cap 4 -- derive_project_identity: remote -> name; no-remote -> abspath; never bare "writ" (pure Python, runner stub)
  Cap 5 -- derive_project_identity raises NotInRepoError when rev-parse fails (pure Python, runner stub)
  Cap 6 -- FAIL-LOUD name->remote_url: ProjectIdentityConflict on conflicting re-register (Neo4j)
  Cap 7 -- remote_url -> name REUSE: ensure_project_registered reuses existing name, no second :Project (Neo4j)
  Cap 8 -- SEAM register-before-capture: ensure_project_registered merges :Project; resolve_project_for_cwd returns derived name (Neo4j)
  Cap 9 -- SEAM no-repo path: ensure_project_registered returns None, no :Project registered (Neo4j or pure)

ENF-SYS-005 note: Caps 6-9 require a real Neo4j connection to validate MERGE/collision
semantics. Mock-only tests of ProjectIdentityConflict or the REUSE lookup would prove
nothing (a mock returns whatever you configure). Those tests are Neo4j-gated and marked
as requiring integration-level verification against a real database.

Wave 1 Cycle 1 (plan.md, C4) adds TestCreateProjectConcurrency: create_project's
check-then-act race (db.py:1243-1283 reads remote_url, then runs a SEPARATE
MERGE+SET with no transaction) means two concurrent conflicting create_project
calls can both pass the read check and the second's unconditional SET silently
overwrites the first with no conflict raised. Same ENF-SYS-005 constraint as
Cap 6-9: this is a real concurrency claim, so it is proven against live Neo4j
via asyncio.gather, never mocks.
"""

from __future__ import annotations

import asyncio
import subprocess
from typing import Callable

import pytest
import pytest_asyncio

from writ.config import get_neo4j_password, get_neo4j_uri, get_neo4j_user
from writ.graph.db import Neo4jConnection


# ---------------------------------------------------------------------------
# Runner stub helpers (the git-seam injection; plan ## Analysis / ## Test approach)
# ---------------------------------------------------------------------------

def _make_runner(responses: dict[str, subprocess.CompletedProcess]) -> Callable:
    """Build a subprocess.run-shaped callable from a mapping of git-subcommand -> result.

    Matching is by substring scan of the args list for a known token (e.g. "rev-parse",
    "get-url") so the stub is robust to flag ordering. Returns a CompletedProcess whose
    type matches subprocess.run's real return type -- the implementer's arg lists must
    contain these tokens; if they differ, the test will raise KeyError and that is the
    implementer's job to reconcile.
    """
    def _runner(args, *, cwd=None, capture_output=False, text=False, timeout=None, **_kwargs):
        # args is a list like ["git", "rev-parse", "--show-toplevel"]
        for token, result in responses.items():
            if token in args:
                return result
        tokens_seen = [a for a in args if isinstance(a, str)]
        raise KeyError(f"runner stub has no mapping for args tokens {tokens_seen!r}; "
                       f"registered tokens: {list(responses)!r}")
    return _runner


def _completed(returncode: int, stdout: str = "", stderr: str = "") -> subprocess.CompletedProcess:
    """Build a real subprocess.CompletedProcess (not a mock) with the given fields."""
    return subprocess.CompletedProcess(
        args=[], returncode=returncode, stdout=stdout, stderr=stderr
    )


# Pre-built canned responses for common git scenarios (TEST-FIXTURE-002: minimal setup).

def _runner_with_remote(repo_root: str, remote_url: str) -> Callable:
    """Runner stub for a repo with an origin remote."""
    return _make_runner({
        "rev-parse": _completed(0, stdout=repo_root + "\n"),
        "get-url": _completed(0, stdout=remote_url + "\n"),
    })


def _runner_no_remote(repo_root: str) -> Callable:
    """Runner stub for a repo with NO origin remote."""
    return _make_runner({
        "rev-parse": _completed(0, stdout=repo_root + "\n"),
        "get-url": _completed(128, stderr="fatal: No such remote 'origin'\n"),
    })


def _runner_no_repo() -> Callable:
    """Runner stub for a cwd that is not inside any git work tree."""
    return _make_runner({
        "rev-parse": _completed(128, stderr="fatal: not a git repository\n"),
    })


# ---------------------------------------------------------------------------
# Neo4j-gated fixture -- mirrors test_decision_memory_records.py:97-115
# Distinct project name prefix "test-dm-id" avoids all "test-dm" (1a) collisions.
# Teardown explicitly DETACH DELETEs :Project nodes because clear_project
# (db.py:1078-1080) filters by n.project, which :Project nodes do NOT carry.
# Two delete passes cover all test-created nodes:
#   1. By name prefix -- catches nodes created with explicit test-prefixed names.
#   2. By repo_root prefix -- catches nodes whose names are DERIVED (e.g. cap 8
#      registers "github.com/org/fresh-project" which does not start with the prefix).
#      All test-created :Project nodes share the _TEST_REPO_ROOT prefix in repo_root,
#      so this pass catches the leakers. No production node uses /tmp/fake-test-*.
# ---------------------------------------------------------------------------

_TEST_PROJECT_PREFIX = "test-dm-id"
_TEST_REPO_ROOT = "/tmp/fake-test-identity-repo"
_TEST_BIBLE_ROOT = "bible"
# A SYNTHETIC remote is required because db_clean intentionally preserves real
# projects, so a fixture using the real git@bitbucket.org:acme/writ.git remote
# would collide with the actual bitbucket.org/acme/writ :Project node
# (TEST-ISOLATE-003). Its derived name is bitbucket.org/acme/test-dm-id-fixture.
_TEST_REMOTE_SSH = "git@bitbucket.org:acme/test-dm-id-fixture.git"


@pytest_asyncio.fixture()
async def db_clean():
    """Connect to Neo4j, wipe test-dm-id project data, yield, clean up.

    Skips when Neo4j is unreachable. Clears the 'test-dm-id' project scope
    AND DETACH DELETEs any :Project nodes created by this suite, since
    clear_project does not touch registry nodes (db.py:1078-1080).
    """
    conn = Neo4jConnection(get_neo4j_uri(), get_neo4j_user(), get_neo4j_password())
    try:
        async with conn._driver.session(database=conn._database) as s:
            await (await s.run("RETURN 1 AS ok")).consume()
    except Exception:
        await conn.close()
        pytest.skip("Neo4j unreachable")

    await _wipe_test_projects(conn)
    yield conn
    await _wipe_test_projects(conn)
    await conn.close()


async def _wipe_test_projects(conn: Neo4jConnection) -> None:
    """Wipe test-scoped data: project scope AND all test-created :Project registry nodes.

    Two delete passes:
    1. By name prefix -- catches nodes registered with explicit test-prefixed names.
    2. By repo_root prefix -- catches nodes whose names are DERIVED (cap 8 registers
       "github.com/org/fresh-project" which does not start with _TEST_PROJECT_PREFIX).
       Every test-created :Project node uses a repo_root under _TEST_REPO_ROOT, so
       this pass catches any leakers from derived-name registration paths.
    No test creates a literal "writ" node (cap 7 uses a test-prefixed alias instead),
    so no targeted "writ" delete is needed.
    """
    await conn.clear_project(_TEST_PROJECT_PREFIX)
    async with conn._driver.session(database=conn._database) as s:
        # Pass 1: delete by name prefix.
        await (await s.run(
            "MATCH (p:Project) WHERE p.name STARTS WITH $prefix DETACH DELETE p",
            prefix=_TEST_PROJECT_PREFIX,
        )).consume()
        # Pass 2: delete by repo_root prefix (catches derived-name nodes from cap 8).
        await (await s.run(
            "MATCH (p:Project) WHERE p.repo_root STARTS WITH $root_prefix DETACH DELETE p",
            root_prefix=_TEST_REPO_ROOT,
        )).consume()


# ---------------------------------------------------------------------------
# Cap 1 (Neo4j): remote_url round-trips through create_project / get_projects
# ---------------------------------------------------------------------------

class TestRemoteUrlRoundTrip:
    """Cap 1 -- remote_url stored by create_project and returned by get_projects."""

    @pytest.mark.asyncio
    async def test_remote_url_returned_by_get_projects(self, db_clean) -> None:
        # Cap 1: create_project with remote_url; get_projects must include it.
        # RED now: create_project accepts only 3 positional args (db.py:1082-1093),
        # no remote_url param -- this will raise TypeError.
        name = f"{_TEST_PROJECT_PREFIX}-cap1"
        remote = "git@github.com:org/repo.git"
        await db_clean.create_project(name, _TEST_REPO_ROOT, _TEST_BIBLE_ROOT, remote_url=remote)
        projects = {p["name"]: p for p in await db_clean.get_projects()}
        assert name in projects, f"project {name!r} not in get_projects() result"
        assert projects[name].get("remote_url") == remote, (
            f"expected remote_url={remote!r}, got {projects[name].get('remote_url')!r}"
        )

    @pytest.mark.asyncio
    async def test_remote_url_merge_idempotent(self, db_clean) -> None:
        # Cap 1 (idempotency): calling create_project with the same remote_url twice
        # MERGES the same node and does not raise or create a duplicate.
        name = f"{_TEST_PROJECT_PREFIX}-cap1-idem"
        remote = "https://github.com/org/repo.git"
        await db_clean.create_project(name, _TEST_REPO_ROOT, _TEST_BIBLE_ROOT, remote_url=remote)
        await db_clean.create_project(name, _TEST_REPO_ROOT, _TEST_BIBLE_ROOT, remote_url=remote)
        matches = [p for p in await db_clean.get_projects() if p["name"] == name]
        assert len(matches) == 1, f"expected exactly 1 :Project for {name!r}, got {len(matches)}"
        assert matches[0].get("remote_url") == remote


# ---------------------------------------------------------------------------
# Cap 2 (Neo4j): backward-compat sentinel -- 3-arg create_project still works
# ---------------------------------------------------------------------------

class TestCreateProjectBackwardCompat:
    """Cap 2 -- create_project without remote_url stays green (additive change sentinel)."""

    @pytest.mark.asyncio
    async def test_three_arg_create_project_succeeds(self, db_clean) -> None:
        # Cap 2: 3-positional-arg call must succeed and project must be retrievable.
        # This is the backward-compat sentinel. It MAY pass now (current signature accepts
        # 3 args) -- it MUST stay green after the remote_url param is added.
        name = f"{_TEST_PROJECT_PREFIX}-cap2"
        await db_clean.create_project(name, _TEST_REPO_ROOT, _TEST_BIBLE_ROOT)
        projects = {p["name"]: p for p in await db_clean.get_projects()}
        assert name in projects
        # remote_url absent or None for a 3-arg call
        assert projects[name].get("remote_url") is None or "remote_url" not in projects[name]


# ---------------------------------------------------------------------------
# Cap 3 (pure Python): normalize_remote_url is 1:1 and clone-stable
# ---------------------------------------------------------------------------

class TestNormalizeRemoteUrl:
    """Cap 3 -- normalize_remote_url produces identical host/org/repo across https and ssh forms."""

    def test_github_https_and_ssh_normalize_identically(self) -> None:
        # Cap 3: https and ssh forms of the same GitHub repo must normalize to the same string.
        # RED now: module writ.session.git_identity does not exist -- ImportError.
        from writ.session.git_identity import normalize_remote_url

        https_form = "https://github.com/org/repo.git"
        ssh_form = "git@github.com:org/repo.git"
        assert normalize_remote_url(https_form) == normalize_remote_url(ssh_form), (
            f"GitHub https {https_form!r} and ssh {ssh_form!r} must normalize identically"
        )

    def test_github_normalizes_to_host_org_repo(self) -> None:
        # Cap 3: normalized form is the host/org/repo triple, no scheme or .git suffix.
        from writ.session.git_identity import normalize_remote_url

        result = normalize_remote_url("https://github.com/org/repo.git")
        assert result == "github.com/org/repo", (
            f"expected 'github.com/org/repo', got {result!r}"
        )

    def test_bitbucket_https_with_credentials_and_ssh_normalize_identically(self) -> None:
        # Cap 3: https-with-user@ and ssh forms of the same Bitbucket repo normalize identically.
        from writ.session.git_identity import normalize_remote_url

        https_cred = "https://user@bitbucket.org/acme/writ.git"
        ssh_form = "git@bitbucket.org:acme/writ.git"
        assert normalize_remote_url(https_cred) == normalize_remote_url(ssh_form), (
            f"Bitbucket https-credentials {https_cred!r} and ssh {ssh_form!r} must normalize identically"
        )

    def test_bitbucket_normalizes_to_host_org_repo(self) -> None:
        # Cap 3: normalized form for Bitbucket is the host/org/repo triple.
        from writ.session.git_identity import normalize_remote_url

        result = normalize_remote_url("git@bitbucket.org:acme/writ.git")
        assert result == "bitbucket.org/acme/writ", (
            f"expected 'bitbucket.org/acme/writ', got {result!r}"
        )

    def test_github_and_bitbucket_normalization_are_distinct(self) -> None:
        # Cap 3: a GitHub repo and a Bitbucket repo with the same org/name path
        # must NOT normalize to the same string (hosts differ).
        from writ.session.git_identity import normalize_remote_url

        github = normalize_remote_url("https://github.com/org/repo.git")
        bitbucket = normalize_remote_url("https://bitbucket.org/org/repo.git")
        assert github != bitbucket, (
            f"GitHub and Bitbucket normalization must differ; both produced {github!r}"
        )


# ---------------------------------------------------------------------------
# Cap 4 (pure Python, runner stub): derive_project_identity behavior
# ---------------------------------------------------------------------------

class TestDeriveProjectIdentity:
    """Cap 4 -- derive_project_identity returns correct (repo_root, remote_url, name) tuples."""

    def test_with_remote_name_equals_normalized_remote_url(self) -> None:
        # Cap 4: when origin exists, name == normalize_remote_url(remote_url).
        # RED now: module writ.session.git_identity does not exist -- ImportError.
        from writ.session.git_identity import derive_project_identity, normalize_remote_url

        runner = _runner_with_remote(
            repo_root="/abs/repo",
            remote_url="git@github.com:org/repo.git",
        )
        repo_root, remote_url, name = derive_project_identity("/abs/repo/src", runner=runner)
        assert repo_root == "/abs/repo"
        assert remote_url == "git@github.com:org/repo.git"
        assert name == normalize_remote_url(remote_url), (
            f"expected name={normalize_remote_url(remote_url)!r}, got {name!r}"
        )

    def test_without_remote_name_is_abspath_repo_root(self) -> None:
        # Cap 4: when origin is absent, remote_url is None and name is the abspath repo_root.
        from writ.session.git_identity import derive_project_identity

        runner = _runner_no_remote(repo_root="/abs/repo-no-remote")
        repo_root, remote_url, name = derive_project_identity("/abs/repo-no-remote", runner=runner)
        assert remote_url is None
        assert name == "/abs/repo-no-remote", (
            f"expected name to be abspath repo_root '/abs/repo-no-remote', got {name!r}"
        )

    def test_name_is_never_bare_writ_for_a_real_repo_with_remote(self) -> None:
        # Cap 4: the guard rule -- derived name must NEVER be the bare literal "writ"
        # for a real repo with a remote. The normalized host/org/repo form is always
        # more qualified (e.g. "github.com/org/writ") and never the bare 4-char "writ".
        from writ.session.git_identity import derive_project_identity

        runner = _runner_with_remote(
            repo_root="/abs/some-writ-repo",
            remote_url="https://github.com/org/writ.git",
        )
        _, _, name = derive_project_identity("/abs/some-writ-repo", runner=runner)
        assert name != "writ", (
            f"derive_project_identity must not return the bare 'writ' literal for a real repo; got {name!r}"
        )

    def test_name_is_never_bare_writ_for_a_real_repo_without_remote(self) -> None:
        # Cap 4: even without a remote, the name is the abspath repo_root (never "writ").
        from writ.session.git_identity import derive_project_identity

        runner = _runner_no_remote(repo_root="/abs/my-project")
        _, _, name = derive_project_identity("/abs/my-project", runner=runner)
        assert name != "writ", (
            f"derive_project_identity must not return the bare 'writ' literal; got {name!r}"
        )


# ---------------------------------------------------------------------------
# Cap 5 (pure Python, runner stub): NotInRepoError when rev-parse fails
# ---------------------------------------------------------------------------

class TestDeriveProjectIdentityNotInRepo:
    """Cap 5 -- derive_project_identity raises NotInRepoError when not in a git work tree."""

    def test_raises_not_in_repo_error_when_rev_parse_fails(self) -> None:
        # Cap 5: when runner returns non-zero for rev-parse, NotInRepoError must be raised.
        # RED now: module writ.session.git_identity does not exist -- ImportError.
        from writ.session.git_identity import NotInRepoError, derive_project_identity

        runner = _runner_no_repo()
        with pytest.raises(NotInRepoError):
            derive_project_identity("/not/a/git/repo/somewhere", runner=runner)

    def test_not_in_repo_error_is_exception_subclass(self) -> None:
        # Cap 5: NotInRepoError must be importable and be an Exception subclass.
        from writ.session.git_identity import NotInRepoError

        assert issubclass(NotInRepoError, Exception), (
            f"NotInRepoError must be a subclass of Exception, got {NotInRepoError.__mro__!r}"
        )


# ---------------------------------------------------------------------------
# Cap 6 (Neo4j): FAIL-LOUD ProjectIdentityConflict on name->remote_url mismatch
# ---------------------------------------------------------------------------

class TestProjectIdentityConflict:
    """Cap 6 -- create_project raises ProjectIdentityConflict when remote_url conflicts with existing.

    ENF-SYS-005: This behavior cannot be proven with mocks (a mock returns what you
    configure). The MERGE + conflict-check is a real MATCH against a live :Project node.
    This test requires Neo4j.
    """

    @pytest.mark.asyncio
    async def test_conflicting_remote_url_raises_project_identity_conflict(self, db_clean) -> None:
        # Cap 6: first register with remote_url R1; second register with same name but
        # remote_url R2 (different) must raise ProjectIdentityConflict.
        # RED now: ProjectIdentityConflict does not exist in db.py -- ImportError.
        from writ.graph.db import ProjectIdentityConflict

        name = f"{_TEST_PROJECT_PREFIX}-cap6-conflict"
        await db_clean.create_project(name, _TEST_REPO_ROOT, _TEST_BIBLE_ROOT,
                                      remote_url="git@github.com:org/repo-A.git")
        with pytest.raises(ProjectIdentityConflict):
            await db_clean.create_project(name, _TEST_REPO_ROOT, _TEST_BIBLE_ROOT,
                                          remote_url="git@github.com:org/repo-B.git")

    @pytest.mark.asyncio
    async def test_same_remote_url_re_register_does_not_raise(self, db_clean) -> None:
        # Cap 6 (idempotent path): same name + same remote_url must NOT raise (idempotent MERGE).
        from writ.graph.db import ProjectIdentityConflict

        name = f"{_TEST_PROJECT_PREFIX}-cap6-idem"
        remote = "git@github.com:org/same-repo.git"
        await db_clean.create_project(name, _TEST_REPO_ROOT, _TEST_BIBLE_ROOT, remote_url=remote)
        # This must not raise -- same remote_url is the idempotent path.
        try:
            await db_clean.create_project(name, _TEST_REPO_ROOT, _TEST_BIBLE_ROOT, remote_url=remote)
        except ProjectIdentityConflict:
            pytest.fail("create_project raised ProjectIdentityConflict for same remote_url (idempotent re-call)")

    @pytest.mark.asyncio
    async def test_project_identity_conflict_is_importable(self, db_clean) -> None:
        # Cap 6: ProjectIdentityConflict must be importable from writ.graph.db.
        from writ.graph.db import ProjectIdentityConflict

        assert issubclass(ProjectIdentityConflict, Exception), (
            "ProjectIdentityConflict must be a subclass of Exception"
        )


# ---------------------------------------------------------------------------
# Cap 7 (Neo4j): remote_url -> name REUSE via ensure_project_registered
# ---------------------------------------------------------------------------

class TestRemoteUrlNameReuse:
    """Cap 7 -- ensure_project_registered reuses an existing name when remote_url matches.

    ENF-SYS-005: The REUSE lookup is a real get_projects() call + Python scan; testing
    with a mocked db would prove nothing about whether a second :Project is created.
    This test requires Neo4j.

    The bootstrap-alias sub-case (plan ## Analysis / Bootstrap note) is simulated with a
    test-prefixed name (not the literal "writ"). create_project MERGEs on name alone
    (db.py:1088), so create_project("writ", ...) would overwrite a real "writ" registry
    node if Phase 1d ever bootstraps one. The prefixed alias covers the same invariant:
    a short custom name bound to a remote_url is reused over the derived host/org/repo
    name, without any risk to the production registry.
    """

    @pytest.mark.asyncio
    async def test_ensure_project_registered_reuses_existing_name_for_known_remote(
        self, db_clean
    ) -> None:
        # Cap 7: pre-register a project with remote_url R; call ensure_project_registered
        # with a runner that derives the same R; returned name must be the pre-registered
        # name and no second :Project must be created.
        from writ.session.registration import ensure_project_registered

        existing_name = f"{_TEST_PROJECT_PREFIX}-existing"
        writ_remote = _TEST_REMOTE_SSH
        await db_clean.create_project(
            existing_name, _TEST_REPO_ROOT, _TEST_BIBLE_ROOT,
            remote_url=writ_remote,
        )

        projects_before = [p["name"] for p in await db_clean.get_projects()
                           if p["name"].startswith(_TEST_PROJECT_PREFIX)]
        count_before = len(projects_before)

        runner = _runner_with_remote(
            repo_root=_TEST_REPO_ROOT,
            remote_url=writ_remote,
        )
        returned_name = await ensure_project_registered(
            db_clean, _TEST_REPO_ROOT, bible_root=_TEST_BIBLE_ROOT, runner=runner
        )
        assert returned_name == existing_name, (
            f"expected ensure_project_registered to reuse existing name {existing_name!r}, "
            f"got {returned_name!r}"
        )

        projects_after = [p["name"] for p in await db_clean.get_projects()
                          if p["name"].startswith(_TEST_PROJECT_PREFIX)]
        assert len(projects_after) == count_before, (
            f"expected no new :Project node (count={count_before}), "
            f"but count is now {len(projects_after)}: {projects_after}"
        )

    @pytest.mark.asyncio
    async def test_writ_bootstrap_alias_reuse(self, db_clean) -> None:
        # Cap 7 (bootstrap-alias scenario): a short custom name bound to a remote_url is
        # reused over the derived host/org/repo name. This simulates the plan's bootstrap
        # note -- e.g. Writ's name "writ" stays "writ" (not "bitbucket.org/acme/writ")
        # because the REUSE lookup finds the already-registered remote_url and returns the
        # existing name. We use a test-prefixed alias (NOT the literal "writ") because
        # create_project MERGEs on name alone and would overwrite a real "writ" node.
        from writ.session.registration import ensure_project_registered

        alias_name = f"{_TEST_PROJECT_PREFIX}-writ-alias"
        writ_remote = _TEST_REMOTE_SSH
        await db_clean.create_project(
            alias_name, _TEST_REPO_ROOT, _TEST_BIBLE_ROOT,
            remote_url=writ_remote,
        )
        runner = _runner_with_remote(
            repo_root=_TEST_REPO_ROOT,
            remote_url=writ_remote,
        )
        returned_name = await ensure_project_registered(
            db_clean, _TEST_REPO_ROOT, bible_root=_TEST_BIBLE_ROOT, runner=runner
        )
        # Must reuse alias_name, NOT mint the synthetic derived name. Alias-reuse is
        # remote-agnostic, so the synthetic remote exercises the same invariant.
        assert returned_name == alias_name, (
            f"expected REUSE of bootstrapped alias {alias_name!r}, got {returned_name!r}"
        )
        assert returned_name != "bitbucket.org/acme/test-dm-id-fixture", (
            f"ensure_project_registered must not mint a derived name when remote_url is already registered"
        )


# ---------------------------------------------------------------------------
# Cap 8 (Neo4j): SEAM register-before-capture
# ---------------------------------------------------------------------------

class TestSeamRegisterBeforeCapture:
    """Cap 8 -- ensure_project_registered merges :Project so resolve_project_for_cwd returns
    the derived name, not the bare 'writ' fallback.

    ENF-SYS-005: The MERGE + resolve interaction requires a real Neo4j graph.
    """

    @pytest.mark.asyncio
    async def test_ensure_project_registered_enables_cwd_resolution(self, db_clean) -> None:
        # Cap 8: after ensure_project_registered, resolve_project_for_cwd(cwd) must return
        # the derived name, NOT the bare "writ" fallback.
        from writ.session.registration import ensure_project_registered

        fresh_root = _TEST_REPO_ROOT + "-cap8"
        remote = "https://github.com/org/fresh-project.git"
        runner = _runner_with_remote(repo_root=fresh_root, remote_url=remote)

        # An unregistered cwd resolves to NO project. It used to fall back to "writ",
        # which silently filed another project's records under this one and let a caller
        # from an unregistered directory read this project's records as its own.
        cwd = fresh_root + "/src/module.py"
        pre_name = await db_clean.resolve_project_for_cwd(cwd)
        assert pre_name == "", (
            f"expected no project before registration, got {pre_name!r}"
        )

        returned_name = await ensure_project_registered(
            db_clean, cwd, bible_root=_TEST_BIBLE_ROOT, runner=runner
        )
        assert returned_name is not None
        assert returned_name != "writ", (
            f"ensure_project_registered must not return the bare 'writ' fallback for a real repo; "
            f"got {returned_name!r}"
        )

        post_name = await db_clean.resolve_project_for_cwd(cwd)
        assert post_name == returned_name, (
            f"resolve_project_for_cwd returned {post_name!r} but ensure_project_registered "
            f"returned {returned_name!r}; they must agree after registration"
        )
        assert post_name != "writ", (
            f"resolve_project_for_cwd must not return the bare 'writ' fallback after registration; "
            f"got {post_name!r}"
        )

    @pytest.mark.asyncio
    async def test_ensure_project_registered_returns_derived_name(self, db_clean) -> None:
        # Cap 8: return value of ensure_project_registered is the derived name string, not None.
        from writ.session.registration import ensure_project_registered

        fresh_root = _TEST_REPO_ROOT + "-cap8b"
        remote = "git@github.com:org/another-project.git"
        runner = _runner_with_remote(repo_root=fresh_root, remote_url=remote)

        result = await ensure_project_registered(
            db_clean, fresh_root + "/src", bible_root=_TEST_BIBLE_ROOT, runner=runner
        )
        assert isinstance(result, str) and result != "", (
            f"expected a non-empty string name, got {result!r}"
        )


# ---------------------------------------------------------------------------
# Cap 9 (Neo4j): SEAM no-repo path -- returns None, no :Project created
# ---------------------------------------------------------------------------

class TestSeamNoRepo:
    """Cap 9 -- ensure_project_registered returns None for a cwd in no git repo and
    registers no :Project node.

    ENF-SYS-005: Asserting that no :Project was created requires a real db query;
    a mocked db.create_project would not prove the function actually skipped it.
    """

    @pytest.mark.asyncio
    async def test_no_repo_cwd_returns_none(self, db_clean) -> None:
        # Cap 9: runner makes rev-parse fail (not in a git repo); result must be None.
        # RED now: writ.session.registration does not exist -- ImportError.
        from writ.session.registration import ensure_project_registered

        runner = _runner_no_repo()
        result = await ensure_project_registered(
            db_clean, "/not/in/any/repo", bible_root=_TEST_BIBLE_ROOT, runner=runner
        )
        assert result is None, (
            f"expected None for a cwd in no git repo, got {result!r}"
        )

    @pytest.mark.asyncio
    async def test_no_repo_cwd_registers_no_project(self, db_clean) -> None:
        # Cap 9: no :Project node should be created when cwd is not in a git repo.
        from writ.session.registration import ensure_project_registered

        projects_before = await db_clean.get_projects()
        names_before = {p["name"] for p in projects_before}

        runner = _runner_no_repo()
        await ensure_project_registered(
            db_clean, "/not/in/any/repo", bible_root=_TEST_BIBLE_ROOT, runner=runner
        )

        projects_after = await db_clean.get_projects()
        names_after = {p["name"] for p in projects_after}
        new_names = names_after - names_before
        assert not new_names, (
            f"expected no new :Project nodes, but these were created: {new_names!r}"
        )

    @pytest.mark.asyncio
    async def test_no_repo_does_not_scope_under_writ_fallback(self, db_clean) -> None:
        # Cap 9 (guard rule 2): the no-repo path must never scope anything under "writ".
        # Confirmed by result=None (no name returned) and no project created. Belt-and-suspenders.
        from writ.session.registration import ensure_project_registered

        runner = _runner_no_repo()
        result = await ensure_project_registered(
            db_clean, "/dev/null/nowhere", bible_root=_TEST_BIBLE_ROOT, runner=runner
        )
        assert result != "writ", (
            f"ensure_project_registered must not return 'writ' for a no-repo cwd; got {result!r}"
        )
        # result should be None (verified above); belt-and-suspenders check here.
        assert result is None, (
            f"expected None, not a scope name, for a no-repo cwd; got {result!r}"
        )


# ---------------------------------------------------------------------------
# C4 (Wave 1 Cycle 1, plan.md): create_project check-then-act race
# ---------------------------------------------------------------------------

async def _warm_connection_pool(conn: Neo4jConnection) -> None:
    """Fire two concurrent throwaway queries so the driver's connection pool
    already holds 2 warm connections before a real concurrency race.

    Empirically verified (plan.md C4 test-skeleton note): on a FRESH
    Neo4jConnection, the first concurrent create_project pair races for a
    single idle pooled connection -- one task reuses it immediately while the
    other pays a cold connection-open cost, which accidentally serializes the
    two calls enough that the read-then-act race never manifests (the loser
    always arrives "late" and correctly observes the winner's write, masking
    the bug). Warming 2 connections first removes that cold-start bias so the
    gather below exercises genuine concurrent execution.
    """
    async def _noop() -> None:
        async with conn._driver.session(database=conn._database) as s:
            await (await s.run("RETURN 1")).consume()

    await asyncio.gather(_noop(), _noop())


class TestCreateProjectConcurrency:
    """C4 -- create_project must be atomic under concurrency, and apply_constraints
    must add a serializing uniqueness constraint on :Project(name).

    ENF-SYS-005: db.py:1243-1283 reads remote_url, THEN runs a SEPARATE
    MERGE+SET (two session.run calls, no transaction) -- a check-then-act race.
    Only a real concurrent run against live Neo4j can prove two conflicting
    create_project calls resolve to exactly one winner; a mocked db.create_project
    would prove nothing about whether the race is actually closed.
    """

    @pytest.mark.asyncio
    async def test_concurrent_conflicting_create_project_one_wins(self, db_clean) -> None:
        # RED today: both concurrent calls can pass the pre-MERGE read check (neither
        # sees the other's write yet), so the second's unconditional SET silently
        # overwrites the first -- 0 conflicts raised, not 1, and the stored remote_url
        # may not match either "winner" deterministically.
        # The warm-up (see _warm_connection_pool) is required to actually observe this
        # on a fresh connection -- verified empirically: without it, the first pooled
        # connection is grabbed by whichever task starts first and the other pays a
        # cold-open delay, which accidentally serializes the two calls and hides the race.
        from writ.graph.db import ProjectIdentityConflict

        await db_clean.apply_constraints()
        await _warm_connection_pool(db_clean)
        name = f"{_TEST_PROJECT_PREFIX}-cap-c4-race"
        remotes = ["git@github.com:org/A.git", "git@github.com:org/B.git"]
        results = await asyncio.gather(
            db_clean.create_project(name, _TEST_REPO_ROOT, _TEST_BIBLE_ROOT, remotes[0]),
            db_clean.create_project(name, _TEST_REPO_ROOT, _TEST_BIBLE_ROOT, remotes[1]),
            return_exceptions=True,
        )

        conflicts = [r for r in results if isinstance(r, ProjectIdentityConflict)]
        winners = [r for r in results if isinstance(r, str)]
        unexpected = [
            r for r in results
            if isinstance(r, BaseException) and not isinstance(r, ProjectIdentityConflict)
        ]
        assert not unexpected, f"unexpected non-conflict exception(s): {unexpected!r}"
        assert len(conflicts) == 1, (
            f"expected exactly 1 ProjectIdentityConflict out of 2 concurrent conflicting "
            f"create_project calls, got {len(conflicts)}: results={results!r}"
        )
        assert len(winners) == 1, (
            f"expected exactly 1 winning (non-raising) create_project call, "
            f"got {len(winners)}: results={results!r}"
        )

        winner_index = 0 if isinstance(results[0], str) else 1
        expected_remote = remotes[winner_index]
        projects = {p["name"]: p for p in await db_clean.get_projects()}
        assert name in projects, f"project {name!r} was not registered at all"
        assert projects[name].get("remote_url") == expected_remote, (
            f"stored remote_url {projects[name].get('remote_url')!r} does not match "
            f"the non-raising winner's remote {expected_remote!r} -- the loser's "
            f"unconditional SET clobbered the winner's write (the check-then-act race)"
        )

    @pytest.mark.asyncio
    async def test_apply_constraints_includes_project_name_unique(self, db_clean) -> None:
        # RED today: no uniqueness constraint statement on :Project(name) exists in
        # db.py's apply_constraints (db.py:1031-1063), so this list comprehension
        # finds nothing.
        await db_clean.apply_constraints()
        constraints = await db_clean.list_constraints()
        project_constraints = [
            c for c in constraints
            if (c.get("labelsOrTypes") or []) == ["Project"] and c.get("type") == "UNIQUENESS"
        ]
        matching = [
            c for c in project_constraints
            if tuple(c.get("properties") or []) == ("name",)
        ]
        assert matching, (
            f"no uniqueness constraint on :Project(name) found; "
            f"existing :Project constraints: {project_constraints!r}"
        )
