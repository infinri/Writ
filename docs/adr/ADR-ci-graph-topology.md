# ADR: CI runs two Neo4j instances, and the production one carries a seeded witness

Status: accepted
Date: 2026-09-22
Plan: `.claude/plans/2412ba38-51e1-4b73-895b-7b240a3c21d3/plan.md`
Touches: `.github/workflows/pr.yml`, `tests/test_post_commit_isolation.py` (read, not
changed), `Makefile`, `tests/conftest.py`, `scripts/ci_shard.py` (amendment below)

## Context

The `test` job published exactly one Neo4j service, on host port 7688, because the
suite's disposable instance has to differ from the production address for the
destructive-wipe guard to open at all (see `ADR-test-graph-isolation.md`). That left
nothing answering on 7687.

`tests/test_post_commit_isolation.py` names `bolt://localhost:7687` on purpose. The
post-commit hook talks to the daemon, the daemon owns the production graph, and a
leaked `Commit` record therefore lands there and not in the suite's instance. The
module counts records at the destination rather than asserting on an env dict,
because the previous version of this pin asserted the hook exits 0, and it does,
whether or not it wrote. Its reader, `_commit_count_for`, runs in a subprocess with
every `NEO4J` variable and `WRIT_TEST_GRAPH` stripped, so it resolves the daemon's
own credentials instead of the suite's.

With nothing on 7687 that reader returns `None`, and the module converts `None` into
a `pytest.fail` with the message "a skip here is indistinguishable from isolation
that works". So all three of its tests failed in CI, and had never once run there.

Separately, `tests/test_bash_irreversible_gate.py` reads a pre-cycle revision of a
hook with `git show ad01ca5:hooks/scripts/writ-bash-write-gate.sh`, which a depth-1
clone cannot resolve.

## Decision 1: a second Neo4j service on the production address

The `test` job publishes a second `neo4j:5` service, `neo4j-production-address`, on
`7687:7687`, alongside the existing one on `7688:7687`. CI's topology now matches a
developer machine: production on 7687, disposable on 7688.

`NEO4J_AUTH` is `neo4j/writdevpass`, and that is load bearing rather than
copy-paste. The counter's subprocess strips the environment and resolves
`get_neo4j_user()` and `get_neo4j_password()`, whose defaults are `neo4j` and
`writdevpass` (`writ/config.py`). Wrong credentials there do not read as "wrong
password", they read as "production unreachable", which is the `pytest.fail` above
and not a skip.

Only 7687 is published for the new service. The disposable service already holds host
7474, and two services in one job cannot publish the same host port; the cypher-shell
healthcheck runs inside the container against its own fixed 7687, so no published HTTP
port is needed.

Two independent reasons the new instance is safe from a wipe, both read rather than
assumed:

1. The wipe permission is `marker_present() and not is_production_instance(uri)`
   (`writ/graph/db/_safety.py`). There is no `writ.toml` in CI, so
   `get_production_neo4j_uri()` falls back to `DEFAULT_NEO4J_URI`, which is
   `bolt://localhost:7687` (`writ/config.py`). The production address is precisely
   the one the guard refuses.
2. The only code in the suite that names 7687 at all is `_commit_count_for`, which
   runs a `MATCH (c:Commit) ... RETURN count(c)` and carries no marker.

Adding a service changes no environment variable, so it cannot change which instance
the suite resolves. `WRIT_NEO4J_URI: bolt://localhost:7688` and
`WRIT_TEST_NO_ISOLATION: "1"` are still set on the job, and `tests/_graph.py` returns
having mutated nothing when the opt-out is set (its three env writes are `setdefault`
in any case, so an explicit outer value always wins).

## Decision 2: the alternative that was ruled out

The obvious cheaper change is to publish the existing disposable instance on 7687
instead of 7688, so one container serves both addresses.

Rejected. The wipe guard's entire discriminator is that the suite's instance key
differs from the production instance key. Republishing the test instance on the
production address makes those two keys compare EQUAL, which does two things at once:
it shuts the wipe gate, so every `disposable_graph` test skips again (the exact
regression the 7688 move was made to fix), and it removes the distinction the
post-commit pin depends on, so the pin would be counting records in the same graph
the suite is free to empty. Making the test instance look like production defeats the
guard rather than satisfying the test.

## Decision 3: the witness is seeded by the workflow, not by the test

`TestThePinCanActuallySeeALeak::test_the_counter_reports_real_commits_for_this_project`
is the sensitivity control. Every other assertion in that module expects zero, so a
counter that always returned zero would satisfy all of them; this one asserts the
counter returns non-zero for project `github.com/infinri/Writ`. On a developer
machine the witness is the project's own commit history. A fresh container has none.

A workflow step before `make test` therefore seeds exactly one node:

```
MERGE (c:Commit {commit_hash: $h, project: $p}) ON CREATE SET c.subject = ...
```

with `commit_hash` `ci-witness-0000000` and `project` `github.com/infinri/Writ`. The
label and property names are taken from `_commit_count_for`'s own query, so the
witness is what that counter counts, not what this document guesses it counts.

The test file was NOT changed. It is read-only against production by design, and a
create-then-delete that died between the two halves would write the exact kind of
stray record the file exists to catch. That is not hypothetical here: 182 of 275
`Commit` records in the production graph on 2026-09-21 were stray test records, which
is why this module was written.

## Decision 4: a post-suite step proves the witness survived

A step after `make test`, with `if: always()`, re-counts the witness by
`commit_hash` and fails the job if it is gone. This is the no-wipe proof, and it is
what makes seeding a witness worth doing rather than relaxing the assertion: the claim
"nothing in the suite wipes the production-address instance" is now checked on every
run against the artifact, instead of being inferred from a green suite.

`if: always()` is deliberate. A wipe and a test failure are exactly the pair that must
not be confused, so the check has to run when the suite is red.

## Decision 5: `fetch-depth: 0` on the test job only

The test job's `actions/checkout@v4` gains `fetch-depth: 0`, matching
`.github/workflows/publish.yml`. `git show <rev>:<path>` appears exactly once under
`tests/`, at `tests/test_bash_irreversible_gate.py`, and without history it fails with
`fatal: invalid object name 'ad01ca5'`.

The `bench` job's checkout is left shallow. It runs `benchmarks/bench_targets.py`,
which reads no git history, and a deeper clone there is a change nothing asks for.

## Decision 6: a temporary diagnostic, with its removal named

`Makefile` gains `PYTEST_MAXFAIL ?= 10` and the `test` recipe reads it instead of the
literal. The default is unchanged, so `make test` with no arguments still passes
`--maxfail=10` exactly as `HANDBOOK.md` and `docs/reference/testing.md` document.
`PYTEST_ADDOPTS` cannot do this job: pytest PREPENDS its contents to the command line,
so the recipe's own explicit `--maxfail` would win over anything set there.

The `test` job sets `PYTEST_MAXFAIL: "0"` (pytest reads 0 as no limit) and
`PYTEST_ADDOPTS: "-rfE"` so one run names every failure and error instead of stopping
at the tenth. Both lines are temporary and are deleted in this cycle's final commit;
the capability list requires `git grep -n "PYTEST_MAXFAIL\|PYTEST_ADDOPTS" .github/`
to return nothing before the cycle closes. The Makefile knob stays: it has the old
default and is a knob, not a diagnostic.

## Amendment 2026-09-24: the topology is instantiated once per shard

Plan: `.claude/plans/6bd39d6d-6668-46eb-9a23-b2eb8edede83/plan.md`

The single `test` job became a four-way matrix, `test-shard` (`test shard 0` to
`test shard 3`), plus an aggregating job still named `test` that holds no services
and passes only when every shard passed and `scripts/ci_shard.py --check` proves the
shards covered every test file exactly once. Every decision above holds, applied per
shard: each shard's runner starts both services (7688 disposable, 7687 production
address), seeds its own witness, and re-counts it with `if: always()` after its own
run; each keeps `fetch-depth: 0`, because which shard holds
`tests/test_bash_irreversible_gate.py` is decided by the timings, not by hand.

What changes is the reach of each proof. A shard's no-wipe check covers only the
files that shard ran, since the instances are per runner and nothing crosses between
them. The four checks together cover the whole suite, because the partition check
guarantees every file ran in exactly one shard, and that is why the aggregator runs it
rather than trusting the matrix. `tests/test_post_commit_isolation.py` runs in one
shard; the other three still seed and verify a witness, which is cheap and keeps the
proof on every runner that ran tests.

## Deferred: the `bible/` coverage gap

Recorded here rather than dropped, with the figures measured on 2026-09-22:

- `bible/` has ZERO tracked files. It is gitignored (`.gitignore:61: /bible/`), so a
  runner checks out a repository in which that directory does not exist.
- 51 test files import `requires_bible` from `tests/_bible_guard.py`.
- Those files carry 566 `def test_` lines. After parametrization that is what CI's
  roughly 730 skips are.
- `writ-corpus.cypher` IS tracked, and `setup-writ` already migrates it into Neo4j.

The guard is correct given its premise: with no `bible/` on disk, those tests have
nothing to read. The premise is what is worth revisiting, because the shipped corpus
is tracked, so CI could regenerate `bible/` from it and un-skip all 566.

Deferred because un-skipping 566 tests that have never run on a runner will surface an
unknown number of new failures, and folding that into this cycle would block the
v1.8.0 tag behind an open-ended investigation. It needs its own cycle, whose first
step is a run that reports how many of the 566 actually pass.

## Also deferred, named rather than fixed

`tests/plugin/test_fresh_install_smoke.py` targets `http://localhost:8765/health`
behind the `claude` CLI, which no runner has. It sits in the population that the
truncated run never reached, so the complete run says whether it skips or fails before
anything is done about it. It is a bare positional string with no `base_url=` binding,
so it does not trip the widened guard in `tests/test_w5_live_env.py`.

## Consequences

- The `test` job now starts two Neo4j containers. Both are pulled from the same
  `neo4j:5` image, so the added setup cost is container start plus the healthcheck
  start period, not a second image pull.
- `tests/test_post_commit_isolation.py` runs in CI for the first time: three tests
  that execute rather than skip or fail.
- A future change that makes the suite wipe 7687 fails the job at the witness check
  with a message naming the instance, instead of passing green and being found weeks
  later in the production graph.
- The witness node is a real `Commit` record in the CI container only. That container
  is destroyed with the job, so it does not accumulate.
