# ADR: The test suite gets its own Neo4j instance, and the tripwire is deleted

Status: accepted (test graph isolation cycle, 2026-08-13). Amended 2026-08-31 by the suite
start state cycle: Decision 6 adds the session start wipe, and Decision 3 gains a recorded
divergence between what it says the isolated instance is warmed FROM and what the code
actually does. Amended 2026-09-16 by the corpus floor cycle: Decision 6's completeness
verdict stops being a hand written list of expectations and is derived from the tracked
corpus dump, which adds one new session start refusal (see the amendment below).

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

**DIVERGENCE, found 2026-08-31 and recorded rather than fixed.** The paragraph above states
the intent; the code does something else on any machine that has a `bible/` tree. The
preflight warms through `tests/_corpus.ensure_corpus`, and that function tries `bible/`
FIRST and only falls back to `writ-corpus.cypher` when `bible/` is absent. On a clean
checkout and in CI the two agree, because there is no `bible/` and the fallback is the only
branch available. On a developer machine that has one (about 140 markdown files on the
machine where this was found) the isolated instance is warmed from `bible/`, so the ADR and
the code have disagreed since the fallback was added in this same cycle, and the
disagreement predates the 2026-08-31 amendment below.

It is recorded and not repaired here on purpose. Switching the preflight to `replay_dump`
would honour this paragraph and would make the start state identical across machines, but
it also changes what the corpus IS on a machine with `bible/`, which can move
content-sensitive assertions. The property the start state cycle owed was per-machine run to
run determinism, and warming from `bible/` delivers that just as well. Changing the corpus
SOURCE is a separate decision with its own blast radius, and doing it silently inside a
cycle about determinism is how a second variable enters an experiment. Anyone taking that
decision should read Decision 6 first: the fallback trigger recorded there (a rebuild past
30 seconds) is the measurement that would justify the switch.

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

### 6. The isolated run starts from a wiped graph, not an inherited one (amendment, 2026-08-31)

Decision 3 gave the run a complete CORPUS at session start. It did not give it a known
GRAPH, and the difference is the whole of this amendment.

`clear_all` preserves `RECORD_LABELS` (`Memory`, `Decision`, `FileChange`, `Commit`,
`Project`) by default, every corpus-level restore preserves them, and `ensure_corpus` checks
floor counts only and deliberately never restores records. So the only thing that removes a
record is an explicit `clear_all(preserve_labels=frozenset())`, which under isolation runs
for real in exactly two early modules. Whatever a later module leaves behind is never
cleared for the rest of that run, and it survives into the next run, because Decision 3 also
dropped the end-of-suite restore on an isolated instance. Measured: 651 record nodes out of
1,119, so 57 percent of the isolated graph was residue from previous runs, and it grew every
run. Two consecutive runs against that graph are two different experiments, which is exactly
the property the isolation cycle set out to buy and did not finish buying.

So `_preflight_isolated_graph` now takes three steps between the isolation verdict and the
corpus warm: census every label, delete every node through
`tests/_graph.py::wipe_everything` (which routes to `clear_all(preserve_labels=frozenset())`
and holds no Cypher text of its own), then warm and time the rebuild. The order is asserted
by source inspection in `tests/test_cycle8_graph_isolation.py`, because each pair of it is
load bearing: census before delete or the report can only say zero, delete after the
classification or a target nobody approved receives a delete statement, delete before the
warm or the warm is what gets undone.

Why the wipe is correct HERE and wrong everywhere else. The preservation rule exists because
a `Decision` record has no file to rebuild from, which is a statement about a graph somebody
cares about. The disposable instance holds only test residue, and the incident the rule was
written for was records destroyed on the PRODUCTION instance. The wipe is therefore scoped
to the one place where the rule has no subject, and it is unreachable from anywhere else:
one caller, inside a function that runs only under isolation, against a target that same
function has already classified as not production. The guard inside `clear_all` remains the
second line, and its `FullWipeRefused` is converted to a `pytest.UsageError` carrying the
isolation remedy, because anything escaping `pytest_sessionstart` that is not a `UsageError`
becomes an INTERNALERROR with no remedy attached, and a refusal that names no way out is a
deadlock.

The preflight prints exactly one line, on every run:

```text
graph isolation: wiped 468 nodes (Rule 288, Abstraction 62, Category 22, ...), corpus rebuilt in 2.1s
```

That line is the positive signal, and without it this decision could not be verified at all.
"The two runs matched" is produced identically by a preflight that worked and by a preflight
that silently did nothing against a graph that happened to be clean, so agreement alone
would be agreement between two things that may both have done nothing. The line also prints
the cost this decision adds, every run, forever: a printed cost cannot drift unnoticed. The
budget set before the code was written was one census read, one whole-graph delete and one
corpus rebuild per RUN, zero extra queries per test, with a stated trigger at 30 seconds of
rebuild for reconsidering the corpus source. First measurement on the machine where it was
built: 2.1 seconds, so the trigger is a long way off and `ensure_corpus` stays.

Verification is an operator procedure, not shipped machinery, for the same reason the census
probe in Decision 4's replacement is: the property spans two pytest sessions and a test that
runs the suite from inside the suite is not a test. Run the suite twice against the same
never-reset instance and compare three things, in the order they discriminate: both report
lines name a non-zero wiped count, the summary counts and the SETS of skipped test ids match
(sets, because two runs can report the same skip COUNT for different reasons, which is why
the runs use `-rs`), and the per-label censuses taken after each run match label for label.
Counts only, never node ids: records carry per-run session ids and timestamps, so the ids
legitimately differ and asserting on them would fail on a correct system. The per-run half
is shipped as `tests/test_suite_start_idempotence.py`, which proves in seconds what the
two-run procedure costs 22 minutes to prove.

## Amendment (2026-09-16): the completeness floor is derived from `writ-corpus.cypher`

CONTEXT. Decision 6's third refusal asks `tests/_corpus.py::is_complete` whether the warm
left the isolated instance holding the whole corpus, and that predicate was hand written.
It floored `Rule` plus four labels, while the label list beside it declared eleven and
named neither `Abstraction` nor `Category`. So it returned True on a graph holding ZERO
`AntiPattern`, `Technique`, `ForbiddenResponse`, `PressureScenario`, `Rationalization` or
`WorkedExample`, the preflight accepted that graph, and `ensure_corpus` then no-opped on
it. A floor written by hand is blind to every label nobody remembered to write down, which
is a different failure from being merely stale.

DECISION. `tests/_inventory.py::corpus_floor()` derives the population, label to count,
from the tracked `writ-corpus.cypher`, and `tests/_corpus.py` reads `EXPECTED`, `MIN_RULES`
and `_LABELS` from it. The label LIST comes from the corpus too, so `Abstraction` and
`Category` entered the floor without anyone naming them, and a label type added to the
corpus later enters it the same way.

THE SOURCE IS THE DUMP, NOT `bible/`, and that follows from what this ADR already records
about the two. Neo4j is canonical, `writ-corpus.cypher` is the tracked shipped form, and
`bible/` is a local, gitignored, derived export refreshed with `writ export-cypher`. A
floor derived from `bible/` would be underivable exactly where it matters most: on CI and
on a clean checkout, neither of which has one. The dump is present in every checkout, is
what CI replays, and is already what `tests/_graph.py::replay_dump` uses.

REJECTED: keep the floors hand written and add a drift test that compares them against the
corpus. That test has to read the corpus to know what the floor should be, which is this
derivation with a second copy to keep in step, and it still cannot floor a label nobody put
on the list. `Abstraction` and `Category` are that failure, measured. Also rejected: a
presence only floor of one node per declared label, which fixes the blindness but loosens
`Rule` from its real floor down to one, waving through the half replayed corpus this repo
has already been bitten by.

THE NEW REFUSAL, stated because a developer will meet it. Deriving moves `Rule` and `Skill`
up to what the dump actually ships and gives nine labels a floor they never had. On CI, on
a clean checkout and on the disposable instance the graph is replayed FROM this dump, so
the floor is met by construction. On a developer machine the preflight warms from `bible/`
first, the divergence Decision 3 already records, so exactly one tree can newly refuse: one
whose `bible/` holds FEWER nodes of some label than the tracked dump. The comparison is
`>=`, so the opposite direction is safe, and adding nodes to `bible/` before running
`writ export-cypher` is the common case.

Because that refusal is new, it names the way out rather than only the problem, which is
the standing rule here: a guard that names no action is a deadlock, not a control.
`tests/_corpus.py::corpus_shortfall` returns the verdict as `{label: (live, required)}`
instead of a bool, the preflight passes it to `isolation_refusal_message`, and the message
prints, per short label, what the graph has, what the floor requires, and two remedies:
`writ export-cypher` when the corpus legitimately changed, or the existing
`WRIT_TEST_NO_ISOLATION=1`. The block is emitted only when a shortfall is supplied, so the
production target and unreachable refusals, which measure no corpus, still print no census
and no remedy for one.

Query budget is unchanged. The derivation is one file read of a tracked file and zero graph
queries, `methodology_counts` keeps its single round trip, and `corpus_shortfall` is pure
over a census the preflight already holds.

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

Added by the 2026-08-31 amendment (Decision 6):

- Warm the wiped instance by replaying the tracked `writ-corpus.cypher` instead of calling
  `ensure_corpus`. Rejected for this cycle, and the reason is recorded as a divergence in
  Decision 3 rather than buried here: it would honour what Decision 3 says and would make
  the start state identical across machines, but on a machine with `bible/` it also changes
  what the corpus IS, which can move content-sensitive assertions. The property owed was per
  machine run to run determinism. Revisit it when the printed rebuild time crosses 30
  seconds, which is a measurement rather than a preference.
- Force `scripts/test-graph.sh up` to replay on every invocation instead of only when it had
  to create or start the container. Rejected: with the suite warming itself at session start
  that is a second answer to a question that now has one, and it would still do nothing for
  a bare `pytest`, which is how the suite is usually run.
- Audit the 54 fixtures that call `clear_all()` with no reimport. Rejected on the same ground
  the original audit alternative was rejected on: "which state does a run start from" is ONE
  fact, and a call-site audit is stale the next time somebody adds a fixture. The wipe makes
  those fixtures' end state irrelevant to the NEXT run, which is the half that mattered.
- Drop `--maxfail=10` from the Makefile so a red run always reaches the same end state.
  Rejected: the wipe already makes run N+1 independent of run N's end state, so maxfail buys
  nothing on that axis and is inert on a green run. Removing it would cost the CI ergonomics
  the Makefile comment defends.
- Address the test daemon on port 8799 in the same cycle. Rejected: it is the other plausible
  cross-run carrier, but `pytest_sessionfinish` already stops it and no measurement
  implicates it. Fixing an unimplicated component in the same change as a measured one makes
  the measurement unattributable.
- Parallelise the suite with xdist to recover the runtime the rebuild costs. Rejected as
  backwards: one shared Neo4j instance, one test daemon on a fixed port, one cache dir per
  run and fixed `/tmp` trace paths are all shared globals, and adding a mechanism whose
  correctness depends on tests not sharing state, in the cycle whose purpose is to admit that
  they do, is the wrong order.

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
- (2026-08-31) `tests/test_graph_wipe_guard.py::TestEveryWholeGraphWipeIsGated` had to learn
  that `disposable_graph` is one way to gate a whole-graph wipe rather than the definition of
  gating: a `pytest_sessionstart` hook cannot request a fixture, so demanding one there
  demands the impossible. It now recognizes three mechanisms, each with its own DETECTOR that
  reads the mechanism off the source (the fixture as a real parameter; a module-scoped autouse
  guard that consults `targets_production` and FAILS on it; the preflight, checked on four
  facts including that the classifier runs before the delete and that `FullWipeRefused`
  becomes a `pytest.UsageError`). Nothing was added to a name allowlist. Each detector returns
  False the moment its mechanism decays, proved by mutating the real source in memory, and a
  planted ungated wipe in a third module still fails both tests, proved against a synthetic
  module in a tmp directory and once against the live tree.
- (2026-08-31) An isolated run begins from a graph holding nothing but the freshly warmed
  corpus. Any test that silently depended on residue left by an earlier module, or by an
  earlier RUN, now fails. That dependency was never visible before, because the residue only
  ever grew, so surfacing it is the point rather than a side effect.
- (2026-08-31) Query budget, restated for the amendment: two reads and one whole-graph
  delete at session start, plus a corpus rebuild that used to be a no-op on a warm instance.
  Zero additional queries in any test. Measured at 2.1 seconds of rebuild on the machine
  where it was built, against roughly 614 seconds for the first half of a full run. The
  number is printed on every run, so the next person does not have to re-measure it to know
  whether it moved.
- (2026-08-31) CI is unaffected. `.github/workflows/pr.yml` sets `WRIT_TEST_NO_ISOLATION=1`,
  so the preflight never runs there, and each CI job gets a fresh service container anyway.
  This is a local-developer fix and CI structurally cannot verify it, which is why the
  verification in Decision 6 is an operator procedure.
- Verification is an operator probe, not shipped machinery: record the production census
  through `get_production_neo4j_uri()`, run the suite, record it again, and identical counts
  are the proof. A permanent before-and-after sentinel would be a second tripwire, which is
  the thing being removed. If the census ever moves, do NOT reinstate the connection-level
  guard; bisect by transport instead, starting with `TestNoSecondGraphTransport`, then any
  `env=` in `tests/` that REPLACES `os.environ` rather than extending it, then how any
  daemon the run started was launched.
