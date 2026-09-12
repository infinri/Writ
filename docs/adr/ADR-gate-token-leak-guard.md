# ADR: A leaked gate token is REPORTED and never removed, and prevention is opt-in per module

Status: accepted (gate-token leak guard cycle, plan.md 2412ba38-51e1-4b73-895b-7b240a3c21d3).

Sibling: `docs/adr/ADR-daemon-leak-guard.md`. That guard is the shape this one is built
from, link for link, and the four places the two deliberately disagree are the reason this
entry exists rather than a comment in `tests/conftest.py`.

## Context

Five test modules left `/tmp/writ-gate-token-<sid>` files behind after every run, measured
by running 28 candidate modules together with an `ls /tmp` diff around them. Two of the
five build their session ids as `f"vp-{uuid.uuid4().hex[:8]}"` and
`f"{_TEST_SCOPE}-srv3-{...}"`, so the file count grew without bound: one new `0600` file
per test per run, forever, in a directory shared with every other process on the machine.
At the start of this cycle there were 39 such files.

A sixth module carries the same defect latently.
`tests/test_phase6hi_methodology_retrieval_and_playbook_wiring.py` mints through
`write_bound_gate_token` and removes nothing; it does not appear in a `/tmp` diff today
only because its `POST /advance-phase` consumes the token on success. Any refusal (a seed
change, a route change, a failed gate) leaves the file.

**This is a hygiene fix, not a vulnerability fix, and nothing in this cycle claims
otherwise.** The advance route reads the token at a path built from the ADVANCING session's
own id (`writ/server/routes/gate.py:80`), so a stray file named `vp-1a2b3c4d` is
unreachable unless a caller supplies that exact literal id, and real ids are
client-assigned UUIDs. No forged approval was ever possible through these files.

Six modules already carry a per-test `_no_leaked_gate_tokens` fixture
(`tests/test_gate_token_binding.py:147` and its copies). It has never been executed against
a module that really leaks: its correctness rests on narrative plus agreement with one
empirical diff.

## Decision

### 1. The guard REPORTS a leaked file and never removes it

`tests/_gate_token_leak.py` defines a session-scoped `/tmp` baseline and a module-scoped
autouse guard that re-snapshots at every module boundary, stores the new snapshot BEFORE
failing so one leak is reported once by its own module, and fails by module name. That is
the daemon guard's chain exactly.

The divergence is what it does with what it finds. The daemon guard's stated posture is
that a process the suite did not prove it started is the operator's to stop. The same
argument is STRONGER for a token file, because the file is not merely someone else's, it
may be an ACTIVE credential: a real Claude Code session running in another window mints
into the same `/tmp` directory the instant its operator types an approval, and a guard that
tidied up after itself would, on the day those two coincide, delete a human's live
approval and burn it. So the guard prints the paths, says in the failure message that it
left them, and stops.

`format_report` ANNOTATES a leaked id whose shape is a dashed uuid as probably belonging to
a live session, and never EXEMPTS it. A future test written with a bare `uuid4()` id would
wear exactly that shape, so an exemption would build a blind spot into the guard on the day
somebody adds one.

Pre-existing leftovers need no exclusion rule at all: the baseline is taken before the
first module boundary, so every file already in `/tmp` from a past run is inside it and can
never be reported. That is structural, the same argument the daemon guard makes for the
operator's own long-lived daemon, and it is why deleting the 39 leftovers is an operator
action rather than something this cycle does.

### 2. Prevention is a separate layer: a teardown sweeper keyed on the module's own prefix

A module opts in by declaring one module-level constant, `GATE_TOKEN_SESSION_PREFIX`. The
per-test autouse `_sweep_gate_tokens` fixture reads it off `request.module` and, at
TEARDOWN, removes `/tmp/writ-gate-token-<prefix>*`. A module that declares nothing is one
`getattr` and a return, so registering the fixture tree-wide costs the other ~200 modules
nothing and cannot sweep a namespace no module claimed.

Three properties make this the right shape, and each is the reason a more obvious option
was rejected:

- **It runs at teardown, so no assertion moves.** Three tests legitimately END with a token
  on disk and assert it is there (`tests/test_advance_gate_validation_parity.py:207,233`
  and `tests/test_decision_memory_capture.py:1244`). They pass unchanged; the file goes
  away afterwards. The whole change per module is ONE line, with no test body, no
  assertion, and no seeded fixture edited.
- **It can only delete inside the declaring module's namespace, and that namespace can
  never be a real session's.** A prefix must clear TWO tests, because there are two ways it
  can reach a live approval. It may not SPELL one: `prefix_is_safe` refuses a prefix built
  only from `[0-9a-f-]`, which is exactly the alphabet a uuid session id is drawn from, and
  the empty string with it, since that is the opening of every id there is. And it may not
  MATCH one: a prefix is interpolated into a glob pattern, so any glob metacharacter
  (`*`, `?`, `[`, `]`) is refused as well. The refusal happens at SETUP naming the
  constant, so an unsafe declaration fails loudly rather than quietly widening the sweep.

  The metacharacter half was missed in the first implementation and found in review. It
  matters because a metacharacter is not a hex digit, so it PASSES the first test on its
  own merits while widening the sweep: a declared prefix of `*` expands the pattern to
  `writ-gate-token-**` and matches every gate-token file on the machine. The case that
  shows it is not theoretical is `2412ba38*`, which read safe under the first test alone
  and would sweep every session whose id starts with those eight hex digits. `sweep_prefix`
  additionally escapes its own input with `glob.escape`, which is deliberate duplication
  rather than belt-and-braces: the validator guards the one fixture that consults it, and
  the escape guards the DELETION against any future caller that does not. Both halves are
  pinned by tests that were measured RED before the fix, including one that plants a
  uuid-shaped approval in a temp directory and requires it to survive a sweep attempted
  under each metacharacter.
- **It does not fail on removal.** That is the deliberate difference from
  `_no_leaked_gate_tokens`, and it is why that fixture could not simply be copied into the
  leaking modules. Sweeping is the sanctioned cleanup; FAILING is the guard's job.

### 3. The fixtures live in an importable module, not in `tests/conftest.py`

The daemon guard's fixtures sit in `tests/conftest.py` and its pure half in
`tests/_daemon_leak.py`. Here BOTH halves are in `tests/_gate_token_leak.py` and
`tests/conftest.py` carries only a one-line import, the same registration convention
`tests/fixtures/session_state.py`'s `sandbox_cwd` uses per module.

The reason is the proof. `tests/test_gate_token_leak_guard.py` proves the guard by RUNNING
it: a nested `pytest -p tests._gate_token_leak` over a synthetic module that really writes
a real file into the real `/tmp`, in four cases (a leaking module that must FAIL naming the
exact path, a cleaning module that must PASS so the failure is caused by the leak and not
the harness, a sweeper module that must PASS with the file gone afterwards, and an unsafe
prefix refused at setup). A conftest cannot be loaded with `-p`, and a synthetic module
placed under `<repo>/tests/` to reach one would load the real `tests/conftest.py`, whose
`pytest_sessionstart` wipes and replays the isolated graph and whose `pytest_sessionfinish`
stops whatever daemon answers on the suite port (the hazard `tests/test_pol5e_hook_noise.py:25-32`
records). Putting the fixtures in an importable module is what makes the proof drive the
REAL objects instead of a re-implementation of them, which is the exact gap
`_no_leaked_gate_tokens` still has.

### 4. The positive control is a sentinel file, not a self-inspection

The daemon guard proves its snapshot is not blind by requiring the process table to still
show this process's own whole command line. There is no equivalent here, so the
session-scoped fixture WRITES a file it knows the name of
(`/tmp/writ-gate-token-leakguard-sentinel-<pid>`, one line reading `not-a-token`), requires
the snapshot to come back holding it, and removes it at session teardown. `live_snapshot`
separately raises `UnmeasurableTmp` when `os.scandir` fails, so an unscannable `/tmp` FAILS
the boundary instead of reading as "no leaks". A guard that goes green because it could not
look is the failure mode this repo has already paid for.

The sentinel is harmless if a crash leaves one behind: a one-line file is refused by
`claim_gate_token`'s bound-token format, and no session is named `leakguard-sentinel-<pid>`.

### 5. The derived map is scoped to what source can actually judge

`tests/_inventory.py::gate_token_minting_modules(*, tests_dir=TESTS)` maps every collected
test module that can mint a real token FROM PYTHON to the neutralizer it carries
(`prefix-sweeper`, `leak-fixture`, `explicit-remove`, `path-patched`, `none`). A KIND rather
than a bool, for the reason `daemon_starting_hooks` gives for its own map: a module that
changes hands reads differently instead of silently staying True.

Two things it cannot see, stated in the direction they fail:

- **Shell-driven mints.** Whether a subprocess of `hooks/scripts/auto-approve-gate.sh`
  reaches its mint at line 333 or 429 is a fact about the caller's runtime path, not about
  source text. Enrolling them by source trace would mean enrolling every module that names
  the hook, which is 24 files against 2 measured leakers, so that arm would demand a
  declaration from ~22 modules that are not broken. The two shell-driven leakers are
  covered by the runtime guard and by their own prefix declarations instead.
- **Consumption.** A module whose every mint is spent by a successful advance carries no
  removal in source and reads `none` correctly, because the file survives the moment the
  advance refuses. That is the latent sixth module, and it is also why five further modules
  read `none` while leaving nothing on disk today (see Consequences).

The key is the MODULE, not the pytest node id, because after this cycle the MECHANISM is
module-scoped: the constant is a module attribute and the sweeper applies to every test in
the module. A node-id key would invent a granularity the mechanism does not have and would
go stale on every test rename. The offending TEST is still named, more precisely, by the
runtime layer: it prints the leaked paths, and the session id inside a leaked path is what
identifies the case.

## Alternatives considered

- **Copy `_no_leaked_gate_tokens` into the five leaking modules.** Rejected on reading its
  source: it globs before and after EACH test, removes anything new, and then asserts
  nothing was removed, so any test that legitimately ends with a token on disk FAILS under
  it. That is three tests across the two modules that matter most, and greening them would
  require wrapping every mint in `_mint_cleanup(sid)` inside the test bodies (14 tests in
  one module, 13 minting ids in the other): roughly 25 edits in assertion-bearing code, for
  a hygiene fix.
- **Extract the six existing copies of `_no_leaked_gate_tokens` into the shared module.**
  Rejected, and not out of squeamishness about touching working files: the new sweeper is
  NOT the same object. It runs at module-declared prefix scope, it does not fail on
  removal, and it deletes nothing outside a declared namespace, whereas the six run per
  test, fail on removal, and delete anything new. "Extracting" them would be a behaviour
  change dressed as a move. **If a later cycle does consolidate them, those three
  differences are what must be preserved, and a fail-on-removal variant has to stay for the
  six: all of them assert on token presence INSIDE `_mint_cleanup` blocks and rely on the
  strict semantics.** That sentence is the reason this alternative is recorded rather than
  dropped.
- **Have the guard delete what it finds, like the six fixtures do.** Rejected outright, and
  it is the single most important decision here. The suite cannot prove it wrote the file
  it is looking at. The six existing copies carry exactly this hazard today (they remove
  ANY `/tmp/writ-gate-token-*` file that appeared during a test, including one a real
  session minted concurrently); this cycle does not fix them, but it makes sure the new
  mechanism cannot add to it.
- **A source-only detector, with no runtime layer.** Rejected because the property is a
  RUNTIME property. Whether a file remains depends on three things source text cannot see:
  whether a shell subprocess reached one of the hook's two mint sites, whether an advance
  CONSUMED the token, and whether a path built through a local variable was written at all.
- **A runtime layer only, with no derived map.** Rejected for the reason the map's value
  shape gives: the runtime layer sees a leak only when it happens, so a module whose mints
  are currently consumed by a successful advance is invisible to it until the day it
  breaks. The map is what names that module while it is still latent.
- **Key the map on the pytest node id** so a file that leaks in one class and is clean in
  another is reported precisely. Rejected: see decision 5.

## Consequences

- Every module boundary in all four suite chunks pays one `os.scandir` of `/tmp`, and every
  test pays one `getattr`. On a run with no leak both are invisible.
- A leaked token file fails the module that leaked it, once, with the full path in the
  message and an explicit statement that nothing was removed. The next boundary does not
  repeat it, because the snapshot is stored before the failure is raised.
- **The suite can now fail for something the operator did.** If a human types an approval in
  another window while the suite is running, the new file appears mid-run and the module
  that happened to be executing is failed by name. That is the accepted cost of a guard that
  refuses to delete: the alternative is destroying the approval. The message says so, and
  `format_report` marks a uuid-shaped id as probably a live session.
- A machine whose `/tmp` cannot be scanned cannot run the suite green. That is the intended
  direction.
- **THE SWEEPER ERASES A SECONDARY REGRESSION SIGNAL, and this is a cost, not a
  neutrality.** It sweeps at EVERY teardown, pass or fail. For a module whose tokens are
  normally CONSUMED by a successful advance, the surviving file was itself evidence that
  the consumption path had broken, and after the declaration that evidence is removed
  before anyone can see it.
  `tests/test_phase6hi_methodology_retrieval_and_playbook_wiring.py` is precisely that
  module: decision 5 and the Context above both call it latent, clean today only because
  its `POST /advance-phase` spends each token. A break in that spending would previously
  have left a `/tmp/writ-gate-token-test-6i-*` file behind; now it will not.

  It NARROWS detection rather than removing it: that module's own functional assertions
  about the advance still fail on their own, and they are the primary signal, more direct
  and more specific than a file in `/tmp`. Sweeping only on a passing test was considered
  and rejected, because it leaves a file behind on every unrelated failure, which is the
  unbounded growth this whole mechanism exists to stop. Recorded here, and in
  `_sweep_gate_tokens`'s own docstring, rather than designed away.
- **The map reports five modules as `none` that leave nothing on disk today**
  (`test_hardening.py`, `test_mode_infrastructure.py`, `test_phase3_centralization.py`,
  `test_phase_advance_unified.py`, `test_pol6f_approval_workflow_extraction.py`). Measured,
  not read: running all five together moved the `/tmp` file count 39 to 39. Every one of
  them mints through `write_bound_gate_token` and removes nothing, and every one is held
  only by a successful advance consuming the token, which is precisely the latent shape
  decision 5 says the map exists to name. They are outside this cycle's planned file list,
  so they are recorded here rather than edited: the fix is one constant each, and an
  unplanned edit is invisible to the commit audit.
- The 39 leftover files from previous runs are left in place by the guard, by design. The
  ones inside a declared namespace are removed the next time their own module runs, because
  the sweeper globs the namespace rather than diffing the test; the rest are an operator
  action.
- No production source changes, so no daemon restart is needed to adopt any of this.
  `writ/session/gate_token.py`, `hooks/scripts/auto-approve-gate.sh` and the hardcoded
  `/tmp` are untouched: that hardcoding is deliberate, so the bash writer and the python
  reader can never disagree about where a token lives.
