# ADR: The test suite gets its own Neo4j instance, and the tripwire is deleted

Status: accepted (test graph isolation cycle, 2026-08-13).

## Context

The suite shared one Neo4j instance with the interactive daemon. A full wipe
(`clear_all(preserve_labels=frozenset())`) deletes the runtime records (`Memory`,
`Decision`, `FileChange`, `Commit`), and a `Decision` record has no source on disk, so it
cannot be rebuilt at all. Rules and methodology nodes come back from `bible/` or from the
tracked `writ-corpus.cypher`; records do not.

The history matters here, because the previous attempt at this same problem looked like a
fix and was not one, and the reason it failed is the whole argument of this record.

On 2026-08-08 the suite was redirected at a second instance with
`WRIT_NEO4J_URI=bolt://localhost:7688`. The run still emptied production: the Rule count
on 7687 went from 287 to 0. The cause went unfound for four days, and what was added
instead of a fix was a connection-level tripwire in `tests/conftest.py`
(`_refuse_production_graph_when_isolated`), which monkeypatched
`Neo4jConnection.__init__` to raise if a connection was ever opened against the
production `(host, port)` during an isolated run. It read green for those four days.

The cause, verified line by line for this cycle rather than inferred: two test modules,
`tests/test_import_markdown_unified.py` and `tests/test_compress_on_ingest.py`, each
defined the same helper.

```python
def _neo4j_container() -> str:
    return os.environ.get("WRIT_TEST_NEO4J_CONTAINER", "writ-neo4j")
```

`WRIT_TEST_NEO4J_CONTAINER` was set nowhere in the repository, so the default always won,
and the default names the PRODUCTION container. Both modules then ran
`docker exec <container> cypher-shell` for every count and for every wipe, including
`DETACH DELETE`. So those two files resolved their target from a container NAME while
every other graph access in the suite resolved it from `writ.config.get_neo4j_uri()`.
Under the redirect the two resolutions pointed at different servers, and the wipes landed
on the real one.

The tripwire could not see any of that. A `docker exec` subprocess never constructs a
`Neo4jConnection`, so the guarded constructor was never called. The lesson, stated the
way it should have been stated in 2026-08-08:

> **The guard watched the connection layer while the leak was at target resolution, one
> layer up.** A transport chose its server from something other than the shared
> configuration, and no amount of watching connections can observe that choice.

One more premise had to be corrected before anything could be fixed in CI. Setting
`WRIT_TEST_GRAPH=1` in the CI job, which is the obvious repair, changes nothing:
`full_wipe_allowed` is `marker_present() and not is_production_instance(uri)`
(`writ/graph/db/_safety.py:137-139`), CI has no `writ.toml` (it is untracked), so
`get_neo4j_uri()` and `get_production_neo4j_uri()` both fall back to the same
`DEFAULT_NEO4J_URI` and the two instance keys compare EQUAL. The marker records intent
and cannot verify the target. Every `disposable_graph`-gated test had therefore been
skipping in CI since the day it was written.

## Decision

### 1. The second transport is deleted, not taught about isolation

The alternative was to make `_neo4j_container()` return the test container when the suite
is isolated. Rejected, and the reason generalizes past this instance.

`_cypher` only counted nodes and `_clear_graph` only wiped the corpus. Both are trivially
expressible through `Neo4jConnection`, which already honours the environment override, so
the docker path bought nothing the driver does not provide; it cost a process spawn per
query and a stdout integer parse that existed only because `cypher-shell --format plain`
returns text. Teaching that path about isolation would add a special case to a CLASS of
bug (a second way to name the target) instead of removing the class, and it would leave a
future contributor facing two correct-looking ways to reach the graph, one of which needs
an environment variable nobody remembers to set. The tree had already run that
experiment: the variable existed, was never set once, and its default pointed at
production for its entire life.

After the deletion there is exactly one module in the suite that opens a graph,
`tests/_graph.py`, and it resolves its target from `writ.config` and nothing else. That
is what makes a single assignment in one file move the whole suite, subprocesses
included: every child inherits `os.environ`, and no `subprocess.run` in `tests/` passes a
REPLACEMENT environment (verified across the tree; the daemon is started with `nohup`
from the pytest process, not through systemd, so it inherits too).

### 2. Isolation is the default for the whole suite, forced at conftest import

Rejected alternative: isolate only the destructive tests, which is what the previous
state already was. It leaves the stated goal unmet on every default run, and the records
it protects are destroyed by any wipe path, not only by the ones a fixture happens to
gate.

The env assignment sits at `tests/conftest.py` top level because at least seventeen
modules bind `NEO4J_URI = get_neo4j_uri()` at their OWN import. pytest imports the
rootdir conftest before any test module, so an assignment there is read by all of them
and an assignment anywhere later is read by none of the ones that already cached the
production URI. This is the same argument the file already makes for `WRIT_PORT` and
`WRIT_CACHE_DIR`.

Three connection values are set with `setdefault`, not assignment, so an outer
environment that already names an instance wins (that is how CI points at its own
service). The password is as load-bearing as the URI: the disposable container carries
`writtestpass` while a local `writ.toml` carries the production password, so redirecting
the URI alone produces an auth failure that reads as "Neo4j unreachable" and skips half
the suite. A skip-shaped failure is precisely what this repo keeps mistaking for green.

`WRIT_TEST_GRAPH=1` is forced on that path and only on that path. It cannot authorize a
wipe of production even if someone overrides the URI back to it, because the other half
of the permission is environment-blind by design.

### 3. pytest refuses, `make` starts

The lifecycle split by caller, deliberately.

`make test` depends on a new `test-graph-up` target, so the documented entry point never
fails for a missing container and a contributor types one command that already existed.
`scripts/test-graph.sh` owns the recipe: `up` creates the container when absent, starts it
when stopped, waits for bolt to answer, and replays `writ-corpus.cypher` only when it had
to create or start something; an already-serving instance is success and exits 0. `down`
stops it and keeps the data. The script also refuses, non-zero, to publish host bolt port
7687 at all, and does nothing under the opt-out.

Bare `pytest` starts nothing. It refuses at session start, before the first test, in three
cases: the resolved URI IS the production `(host, port)`; the disposable instance does not
answer; the instance answers but the replay left the corpus below the census. The refusal
message names the resolved URI, `make test-graph-up`, and the opt-out.

Rejected: auto-starting the container inside `pytest_sessionstart`. It puts a docker side
effect inside the test runner, which is the kind of helpfulness that later gets blamed for
something unrelated, and it gives CI no single entry point to call.

Rejected, and this is the tempting one: SKIPPING rather than refusing when the instance is
unreachable. A mass skip is cheap to produce and indistinguishable from a green run, and
this suite has already paid for that lesson twice. Refusing costs one command to clear and
cannot be mistaken for success.

Warm from the tracked `writ-corpus.cypher`, not from `bible/`, because `bible/` is
untracked and a clean checkout has none. Its census (287 Rule, 16 Skill, 15 Playbook, 20
Phase, 5 SubagentRole, 62 Abstraction) satisfies `tests/_corpus.is_complete()` with
Playbook and SubagentRole exactly at their floors and no headroom. Recorded here rather
than discovered later: if a future dump loses one Playbook, the preflight refuses the
whole run instead of letting a partial corpus scatter odd failures. That is the correct
direction and the one the contract was written for.

### 4. The tripwire is DELETED, not extended, and the single-path design replaces it

Both the fixture and the comment block explaining it are gone. Not kept as defence in
depth. The argument is short: **a guard that is blind to the live instance of the class it
names is worse than no guard, because it answers the question "is this covered?" with a
yes.** It was blind for four days while the leak it was written for was in the tree.

Extending it was considered and rejected on the same ground that makes it wrong today. To
observe a `docker exec` the tripwire would have to monkeypatch `subprocess`, which means
watching every process the suite spawns for a shape that names a container, then
maintaining that list against the next transport somebody invents (`neo4j-admin`, a bolt
client in another language, a REST call to 7474). That is a detector chasing a space of
transports, and the previous version of exactly that detector shipped with a comment
asserting coverage it did not have.

What replaces it is prevention at the layer where the leak actually occurred:

- `tests/_graph.py` is the only thing in the suite that opens a graph, and it resolves its
  target from `writ.config`.
- `pytest_sessionstart` verifies that resolution ONCE, before the first test, for every
  transport at the same time, because every transport reads the same configuration. A
  subprocess is covered by the same check as an in-process driver.

That is prevention rather than detection: after the change there is no expressible way for
a test to reach a server the preflight did not approve, short of writing a new transport.

Nothing in Python can stop a future contributor from typing
`subprocess.run(["docker", ...])`, so the smallest honest complement ships too:
`TestNoSecondGraphTransport` in `tests/test_graph_dump.py` fails the build on any argv list
under `tests/` whose HEAD is `docker`, `docker-compose`, `cypher-shell` or `neo4j-admin`.
It is the pin that keeps the design true, not an alternative to it. It keys on the head of
the list rather than on the substring "docker" anywhere, because `["bash", "docker", "git"]`
in the tool-prerequisite tests is an allowlist of tool NAMES and must keep passing. It
ships with an EMPTY exemption map, because after this cycle the tree needs none, and
inventing an exemption would license exactly the shape being forbidden. It lives beside
`TestNoRawWholeGraphDeletes` rather than in the cycle's own file: two guards of the same
family in two files is a drift shape this repo has already been bitten by.

### 5. CI moves the PORT, and the bench job deliberately does not

The test job publishes the neo4j service on `7688:7687` and sets `WRIT_NEO4J_URI`,
`WRIT_NEO4J_PASSWORD` and `WRIT_TEST_GRAPH` at job level. The port is the load-bearing
half, per the Context above: the marker alone leaves the gate shut because both sides of
the instance comparison fall back to the same default. With the port changed, CI runs in
the same shape as a local isolated run, so the two stop being different configurations
that fail differently.

The service health check needed no edit, verified rather than assumed: it runs
`cypher-shell` INSIDE the container against the container's own fixed 7687, so no
published host port appears in it. `.github/actions/setup-writ` needed no edit either,
because its migrate and verify steps resolve through `writ.config` and therefore read the
job env. That is the same property that makes the whole design work.

The bench job stays on 7687 on purpose. It never requests `disposable_graph`, its numbers
are a historical series that should not gain a new variable, and keeping one job on the
default preserves a live proof that the non-isolated path still works end to end.

## Alternatives considered

- Make `_neo4j_container()` return the test container when isolated. Rejected in Decision
  1: it special-cases an instance of the bug instead of removing the class, and the
  env-switchable container name is the exact mechanism that already failed.
- Audit the 45 test files that call `clear_all` and fix them one at a time. Rejected:
  the property being fixed is "which server the suite is pointed at", which is ONE fact,
  not 45. An audit of call sites is stale the next time somebody adds a fixture. Not one
  of those call sites is edited by this cycle.
- Isolate per test worker, or teach the suite to run against two instances at once. Both
  rejected as larger designs than the defect requires. The defect is that the suite had
  two ways to name one server.
- Keep the connection-level tripwire as defence in depth, or extend it to watch
  `subprocess`. Both rejected in Decision 4.
- Auto-start the container from `pytest_sessionstart`. Rejected in Decision 3.
- Skip instead of refusing when the disposable instance is unreachable. Rejected in
  Decision 3: a mass skip is indistinguishable from green.
- Set `WRIT_TEST_GRAPH=1` in CI and leave the service on 7687. Rejected because it does
  nothing, and shipping it would have produced a CI change that looks like a fix.
- Replace the `docker run` recipe inside `writ/graph/db/_safety.py::how_to_run_safely()`
  with a pointer to `scripts/test-graph.sh`. Rejected for this cycle:
  `tests/test_graph_wipe_guard.py` asserts that refusal message contains "docker run", so
  the change churns a tested safety message for a cosmetic dedup, and the copy count stays
  at two either way because the docs copy became a pointer instead.
- Disable isolation globally if some test proves impossible to run isolated. Rejected in
  advance: the answer is to mark that test. A test that requires production data to pass
  is a finding of its own.

## Consequences

- A contributor now needs a running disposable Neo4j, or the opt-out. `make test` starts
  it; bare `pytest` refuses with the command. The accepted cost, stated plainly: a
  developer with no docker daemon cannot run even the pure unit tests until they set
  `WRIT_TEST_NO_ISOLATION=1` once. That is the price of never silently falling back to the
  real graph, and the refusal message names the variable.
- `WRIT_TEST_NO_ISOLATION=1` restores the previous behaviour exactly, including the
  end-of-suite corpus restore and `disposable_graph` skipping. Under it the suite runs
  against whatever `writ.toml` configures, which on a developer machine is the live graph.
- Tests that have never executed anywhere now execute: `tests/test_db_category.py` and
  `test_graph_dump.py`'s `TestCypherDumpRoundTrip` and `TestRecordPreservationOnReplay`.
  Assertions that have never run are assertions nobody has debugged, so a failure there is
  a finding about those tests, not a reason to weaken isolation.
- The isolated instance starts with no runtime records, while production carries roughly
  200 `Memory` nodes, and the corpus dump deliberately excludes records. Any test that
  silently depended on live memories being present will fail. That dependency is itself a
  defect and surfacing it is the point.
- Three wiping fixtures stop skipping and leave the graph EMPTY at teardown, so
  `tests/test_db_category.py` and `tests/test_graph_dump.py` each gained one module-scoped
  restore. That is at most two extra dump replays per run, bounded by module count rather
  than test count; the rejected function-scoped placement would have been one replay per
  test.
- Query budget: two round trips at session start (one reachability probe, one census) and
  ZERO at session end, since an isolated run drops the restore that costs a full dump
  replay today. On a warm instance the net change is negative. Worst case is one replay at
  session start on a cold instance, plus the two module-scoped ones.
- The disposable bolt port is now written in two places by necessity,
  `scripts/test-graph.sh` and `tests/_graph.py::ISOLATED_NEO4J_URI`, so a parity test reads
  both files and compares. This repo has been bitten by an unpinned duplicated seam before.
- No file under `writ/` is touched, so there is no daemon restart, no runtime behaviour
  change, and a plain `git revert` of the cycle restores the previous behaviour exactly.
  The container can be left running afterwards with no effect.
- Verification is an operator probe, not shipped machinery: record the production census
  through `get_production_neo4j_uri()`, run the suite, record it again, and identical counts
  are the proof. A permanent before-and-after sentinel would be a second tripwire, which is
  the thing being removed. If the census ever moves, do NOT reinstate the connection-level
  guard; bisect by transport instead, starting with `TestNoSecondGraphTransport`, then any
  `env=` in `tests/` that REPLACES `os.environ` rather than extending it, then how any
  daemon the run started was launched.
