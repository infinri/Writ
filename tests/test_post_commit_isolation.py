"""The post-commit hook must reach NOTHING when a test says the daemon is down.

MEASURED 2026-09-21: 182 of 275 `Commit` records in the PRODUCTION graph (66%)
carry a project like
`/tmp/pytest-of-lucio.saldivar/pytest-18/test_post_commit_fails_open_wh0/repo-postcmt`,
most recently that afternoon. They were written by
`test_decision_memory_commit.py::test_post_commit_fails_open_when_daemon_down`,
whose whole premise is that the daemon is unreachable.

WHY THE PREMISE WAS FALSE. That test sets `WRIT_PORT=19999`. The hook resolves its
transport at `.git/hooks/post-commit:106-108`: if `$HOME/.cache/writ/run/writ.sock`
exists it passes `--unix-socket`, and the hook's own comment at :102 records that
curl then IGNORES the port. One lever was set; the other was the one in use. This
project already wrote that lesson down as "the address you SET is not the one used".

WHY THIS PIN READS THE GRAPH AND NOT THE ENV DICT. Asserting that `_hook_env`
contains a dead socket path would pass while the hook resolved a third way, which is
exactly how this survived: the original test asserts the hook exits 0, and it does,
whether or not it wrote. So this counts records at the DESTINATION.

WHICH GRAPH. The hook talks to the daemon over HTTP, and the daemon owns the
PRODUCTION graph, so a leaked record lands there and not in the suite's isolated
instance. This connects to production deliberately, READ ONLY, because that is the
container the side effect would appear in. Reading the wrong container is how the
same class of bug is missed twice.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

SKILL_ROOT = Path(__file__).resolve().parent.parent
if str(SKILL_ROOT) not in sys.path:
    sys.path.insert(0, str(SKILL_ROOT))

POST_HOOK = SKILL_ROOT / "hooks" / "git" / "post-commit"

# The daemon's own graph, not the suite's isolated one. Named explicitly rather than
# taken from get_neo4j_uri(), which the suite repoints at the disposable instance.
PRODUCTION_BOLT = "bolt://localhost:7687"


def _commit_count_for(project_substring: str) -> int | None:
    """Commit records at the PRODUCTION graph whose project contains the substring.

    Runs in a SUBPROCESS with every NEO4J/WRIT_NEO4J variable stripped, so the child
    resolves the daemon's own credentials from writ.toml instead of the suite's.
    Measured: called in-process under pytest this raises AuthError, because conftest
    points the suite at the disposable instance and those credentials do not
    authenticate against production. Reading the wrong container, or failing to read
    the right one, are the two ways this pin could pass for the wrong reason.
    """
    code = (
        "import asyncio, sys\n"
        "from writ.config import get_neo4j_password, get_neo4j_user\n"
        "from writ.graph.db import Neo4jConnection\n"
        "async def go():\n"
        "    db = Neo4jConnection(%r, get_neo4j_user(), get_neo4j_password())\n"
        "    try:\n"
        "        rows = await db._run('MATCH (c:Commit) WHERE c.project CONTAINS $s "
        "RETURN count(c) AS n', s=sys.argv[1])\n"
        "        print(rows[0]['n'] if rows else 0)\n"
        "    finally:\n"
        "        await db.close()\n"
        "asyncio.run(go())\n" % PRODUCTION_BOLT
    )
    env = {k: v for k, v in os.environ.items()
           if "NEO4J" not in k.upper() and k != "WRIT_TEST_GRAPH"}
    try:
        out = subprocess.run(
            [sys.executable, "-c", code, project_substring],
            cwd=str(SKILL_ROOT), env=env, capture_output=True, text=True, timeout=30,
        )
    except Exception as exc:
        print(f"[isolation-pin] subprocess failed: {type(exc).__name__}: {exc}")
        return None
    if out.returncode != 0:
        print(f"[isolation-pin] production read failed: {out.stderr.strip()[-200:]}")
        return None
    try:
        return int(out.stdout.strip())
    except ValueError:
        print(f"[isolation-pin] unparseable count: {out.stdout!r}")
        return None


def _init_repo(path: Path) -> Path:
    path.mkdir(parents=True)
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@e",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@e"}
    subprocess.run(["git", "init", "-q", str(path)], check=True, env=env)
    (path / "f.txt").write_text("x\n")
    subprocess.run(["git", "-C", str(path), "add", "f.txt"], check=True, env=env)
    subprocess.run(["git", "-C", str(path), "commit", "-qm", "seed"], check=True, env=env)
    return path


def _isolated_env(repo: Path) -> dict:
    """BOTH transports neutralised. The port alone is not isolation."""
    env = os.environ.copy()
    env["WRIT_PORT"] = "19999"
    env["WRIT_SOCKET"] = str(repo / "definitely-not-a-socket")
    env["GIT_DIR"] = str(repo / ".git")
    env["WRIT_DIR"] = str(SKILL_ROOT)
    return env


@pytest.mark.skipif(not POST_HOOK.exists(), reason="post-commit hook source absent")
class TestPostCommitReachesNothingWhenIsolated:
    def test_running_the_hook_writes_no_record_to_the_production_graph(self, tmp_path):
        repo = _init_repo(tmp_path / "repo-isolation-probe")
        marker = "repo-isolation-probe"

        before = _commit_count_for(marker)
        if before is None:
            pytest.fail(
                "the production graph is unreachable, so this pin cannot observe the "
                "destination a leak would land in. Start it rather than skipping: a "
                "skip here is indistinguishable from isolation that works."
            )
        assert before == 0, f"{before} stale records already exist for {marker}"

        result = subprocess.run(
            ["bash", str(POST_HOOK)], cwd=str(repo), env=_isolated_env(repo),
            capture_output=True, text=True, timeout=15,
        )
        assert result.returncode == 0, (
            f"the hook must fail open and never block a commit; got {result.returncode}"
        )

        after = _commit_count_for(marker)
        assert after == 0, (
            f"{after} Commit record(s) reached the PRODUCTION graph from an isolated "
            f"test. Neutralising WRIT_PORT alone is not isolation: the hook prefers "
            f"--unix-socket when $WRIT_SOCKET exists and curl then ignores the port."
        )

    def test_the_env_helper_neutralises_the_socket_too(self, tmp_path):
        """The env is not the pin, but a socket path that happens to EXIST would make
        the pin above pass for the wrong reason, so the helper's own output is checked."""
        repo = tmp_path / "repo-env-probe"
        repo.mkdir()
        env = _isolated_env(repo)
        assert env["WRIT_PORT"] == "19999"
        sock = Path(env["WRIT_SOCKET"])
        assert not sock.exists(), "the stand-in socket path must not exist"


class TestThePinCanActuallySeeALeak:
    """SENSITIVITY CONTROL. Every assertion above expects zero, so a counter that
    always returned zero would satisfy all of them. This proves the counter sees
    real leaked records, using the ones the broken test already wrote rather than
    creating a new leak to demonstrate the leak."""

    def test_the_counter_reports_the_known_historical_leak(self):
        leaked = _commit_count_for("repo-postcmt")
        if leaked is None:
            pytest.fail("production graph unreachable; the pin above cannot be trusted")
        assert leaked > 0, (
            "expected the historical pollution from "
            "test_post_commit_fails_open_when_daemon_down to still be visible "
            f"(182 records measured 2026-09-21); got {leaked}. If this is now 0 the "
            "records were cleaned up, and this control needs a different witness."
        )
