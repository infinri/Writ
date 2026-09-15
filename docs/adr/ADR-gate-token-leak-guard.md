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
  advance refuses. That is the latent sixth module, and it was also why five further
  modules read `none` while leaving nothing on disk on most runs. Decision 7 wires all
  five, so no member of the map reads `none` any more; the gap this bullet describes is
  unchanged, and it is what a sixth such module would land in.

The key is the MODULE, not the pytest node id, because after this cycle the MECHANISM is
module-scoped: the constant is a module attribute and the sweeper applies to every test in
the module. A node-id key would invent a granularity the mechanism does not have and would
go stale on every test rename. The offending TEST is still named, more precisely, by the
runtime layer: it prints the leaked paths, and the session id inside a leaked path is what
identifies the case.

### 6. The per-test deleters are CONFINED: only the DELETION narrows, never the detection

Added by the second cycle on this mechanism (plan.md
2412ba38-51e1-4b73-895b-7b240a3c21d3), which closes the hazard the first cycle recorded and
deliberately left open in the third bullet of "Alternatives considered" below.

Five test modules run a per-test autouse `_no_leaked_gate_tokens`: four define it
(`tests/test_gate_token_binding.py`, `tests/test_approval_evidence.py`,
`tests/test_replan_reopen_planning.py`, `tests/test_review_promote_authority.py`) and a
fifth registers it by importing it (`tests/test_phase_machine_reset.py:37`). It listed the
whole shared `/tmp/writ-gate-token-*` namespace before and after every test and deleted
everything that appeared in between, which is precisely the thing decision 1 refuses to do:
between the two snapshots a real Claude Code session in another window can mint an
approval, and the fixture removed it and failed a test that had done nothing wrong.

**The deletion now discriminates on the SHAPE of the session id.**
`tests/_gate_token_leak.py::confined_leak_sweep` removes only the files whose session id
could NOT belong to a live session, returns them as `removed` (the caller still fails its
own test by name, exactly as before), and returns the rest as `left_alone`, untouched on
disk. `could_be_a_live_session` is the rule: an id that is non-empty and drawn only from
`[0-9a-f-]` could BE a real client-assigned uuid, so a test may not delete the file
carrying it. That alphabet is `_UUID_ALPHABET`, the same frozenset `prefix_is_safe` already
used, so the shape rule has ONE definition across the mechanism rather than five copies.

`could_be_a_live_session` is a second function rather than `not prefix_is_safe(...)`
because the two answer different questions. `prefix_is_safe` also refuses a glob
metacharacter, since a DECLARED prefix is interpolated into a sweep pattern; a DISCOVERED
filename is never interpolated, it is handed to `os.remove` whole. Negating it would
classify a leaked file named `writ-gate-token-vp-*` as probably-live and leave it, which is
false and would quietly stop the suite catching a real leak. Both are False for the empty
string, and for OPPOSITE reasons: an empty PREFIX is the opening of every id there is,
while an empty COMPLETE id cannot be a uuid, so `/tmp/writ-gate-token-` is this suite's own
leak and is removed like any other. Both halves are pinned.

**The hand-off is what makes this safe: detection MOVES, it does not disappear.** A
uuid-shaped file that appears mid-test is no longer deleted, and it is no longer this
test's business to judge, but `_gate_token_leak_guard` (decision 1) still snapshots at the
next module boundary, still reports the path, and still FAILS the module, with
`format_report` naming it as probably a live session's own approval. So a non-uuid-shaped
leak fails its own test by name as it always did, and a uuid-shaped leak fails one module
later with the path named. Per-test failure was the wrong granularity for that class, not
an acceptable cost: it blames a test for a file a human minted in another window, which is
a false positive by construction.

`left_alone` is a RETURNED, NAMED value rather than a filter applied inside the sweep, so
the decision to skip a shape class is visible at all five call sites instead of buried in
one. A future fixture that ignores the second element of the tuple is visibly ignoring it.
The per-test report channel is `warnings.warn(..., ForeignGateTokenWarning)`: a `print` on
a PASSING test is swallowed by pytest's capture exactly when it matters, and a failure is
the false positive above, while a warning lands in the end-of-run summary attributed to the
test that saw it, survives `-q`, and can be escalated by anyone who wants
`-W error::tests._gate_token_leak.ForeignGateTokenWarning`.

**Why the warning is raised by `warn_about_left_alone` instead of inline in each fixture,
measured rather than styled.** When one of these fixtures fails its `assert not removed`,
pytest's teardown traceback echoes the fixture's own source from its `def` line down to the
failing statement. A `warnings.warn(..., ForeignGateTokenWarning, ...)` written inline
would therefore put that class name in the OUTPUT of a run where nothing was left alone and
no warning was raised, and the anti-vacuity test that proves the confined fixture still
deletes and still fails reads exactly that string. Keeping the class name out of the
fixture body keeps a report a report.

The extraction is a strict superset of what each copy already did (same directory, same
prefix match, same `os.remove` tolerating `OSError`, same `assert not <removed>` message
shape) plus one further change this module's stated philosophy demands: the before-snapshot
goes through `live_snapshot()`, which raises `UnmeasurableTmp` on an unscannable `/tmp`
instead of silently returning an empty set and letting the teardown call every surviving
file a leak.

`tests/_inventory.py::gate_token_directory_deleters(*, tests_dir=TESTS)` holds the
population, keyed on the MECHANISM rather than on a fixture name, so a sixth copy calling
itself anything else still fails by name. A function enters through arm A by doing all
three of naming the token namespace, LISTING a directory and calling a remover, or by
delegating to `confined_leak_sweep`; a module enters through arm B by IMPORTING an arm-A
function, which is the only way `tests/test_phase_machine_reset.py` can be covered without
editing it. `tests/_gate_token_leak.py::sweep_prefix` lists and removes in one function and
would be arm A, but the walk collects `test_*.py` only, so it is out. That is deliberate,
not luck: it deletes only inside a namespace a module DECLARED and `prefix_is_safe`
validated, which is the sanctioned form, and it is recorded here so the omission is not
read later as an oversight.

**The road not taken, first: extract the whole FIXTURE into one tree-wide autouse.**
Rejected for the reason the third bullet below already gives for the sweeper: the per-test
delete-and-fail fixture is not the same object as the module-scoped guard, and registering
it tree-wide would fail every test in the other ~200 modules that legitimately ends holding
a token (`tests/test_advance_gate_validation_parity.py:207,233` and
`tests/test_decision_memory_capture.py:1244` are exactly those). Five modules opted into
strict per-test cleanup; that opt-in stays theirs. Only the DECISION is shared.

**The road not taken, second: delete the five fixtures and give each module a
`GATE_TOKEN_SESSION_PREFIX` instead.** This looks like the cheapest fix, because the
sweeper in decision 2 is already namespace-scoped, already escapes glob metacharacters and
already validates its prefix, and all five modules use distinguishable ids. It is blocked,
measured rather than guessed: `bin/lib/analyzers-regex.sh:292-295` refuses any ALL-CAPS
identifier containing `TOKEN` assigned a string literal of eight or more characters, and of
the five prefixes needed, `evidence-`, `reviewpromote-` and `advance-from-complete-` are
refused at write time (the first cycle hit this same wall and deferred two modules for it).
The four ways to make it land (renaming the constant, shortening the value, adding an
allowlist entry, building the value by concatenation) are all evasions of a guard whose
stated purpose this change does not satisfy, and narrowing that scanner is its own cycle.
It would not be equivalent even if it landed: the sweeper does not FAIL on removal, so
these five modules would lose the per-test leak detection they deliberately opted into.

### 7. The last five `none` modules are wired, each by the mechanism its own id shapes allow

Added by the third cycle on this mechanism (plan.md
2412ba38-51e1-4b73-895b-7b240a3c21d3). The first cycle recorded five modules that the map
rated `none` and left them outside its file list; this one wires all five, so
`gate_token_minting_modules()` now maps no module to `none` and the strict xfail that
recorded the deferral is deleted. Two of the five were measured LEAKING, not latent:
`tests/test_mode_infrastructure.py::TestModeAdvancePhase::test_advance_denied_for_nonwork_mode`
and `tests/test_phase3_centralization.py::TestAdvancePhase::test_advance_fails_without_plan_md`
each end on a REFUSED advance, so nothing consumes the token, and each left a real file
behind when run alone. A later test on the same path consumed it, which is why a
full-module run looked clean and the single-node run did not.

**The identical-id collision is fixed UPSTREAM, in the shared fixture, not labelled
downstream with a shared prefix.** Those two modules did not merely share a prefix: both
import `session_id` from `tests/fixtures/session_state.py`, neither parametrizes it, and
its default was one literal, so both wrote the same 37-byte path
`/tmp/writ-gate-token-test-session`. The sweeper is keyed per module and its docstring
promises to remove "this module's OWN gate-token files", so giving both the same declared
prefix would have made that promise false for both, with no way for the next reader to tell
which module owns the file. Instead the fixture's DEFAULT is now derived from the requesting
module (`module_session_id(request.module.__name__)`), and each of the two declares
`GATE_TOKEN_SESSION_PREFIX = module_session_id(__name__)` from the same function, so the
declared namespace and the id actually minted have ONE definition and cannot drift.
`request.param` is still consulted first, so a consumer overriding the id by indirect
parametrization is unaffected; none does today.

The blast radius is the import edge, and it is DERIVED rather than assumed:
`tests/_inventory.py::shared_session_id_importers()` walks the tree for a module-level
`from tests.fixtures.session_state import ... session_id ...`, which is four modules today,
two of which (`tests/test_mode_switch_midsession.py`, `tests/test_gate_artifact_cleanup.py`)
mint no token at all and would never appear in the other map. Module level is the mechanism,
not a style rule: pytest registers a fixture from the name bound in the module's own
namespace, so `tests/test_w5_fixture_dedup_c.py`'s function-body import registers nothing
and shares no default. A fifth importer gets its own id automatically, because the id is a
function of the module name; the two residual overlap shapes (two modules with the same stem
in different subdirectories, and a stem that EXTENDS another's) are pinned by a capability
asserting no derived id equals or is a string PREFIX of another, since a prefix is what a
glob-based sweep actually cares about.

**`tests/test_phase_advance_unified.py` is WRAPPED, not prefixed, and that is the second
decision here.** Its five mint sites use `parity-pathb-<8hex>`, `parity-patha-<8hex>`,
`root-mismatch-<8hex>`, `write-fail-<8hex>` and the bare literal `".."`. No string prefixes
all five, so any single declared constant would have moved the module out of `none` while
four shapes stayed bare: it would read as fixed and would not be. The module imports
`_mint_cleanup` from `tests.test_gate_token_binding` instead and wraps each site through its
final assertion, so a refused advance and a failed assertion both still clear the file. The
helper is imported from where it lives rather than copied, because
`tests/test_phase_machine_reset.py:37` already takes that exact edge and because
`_gtm_carries_leak_fixture` recognizes a DEFINITION as readily as an IMPORT, so a sixth copy
would classify identically while adding drift. Consolidating the five existing copies is
still the deferred item the second bullet under "Alternatives considered" describes, with
the three semantic differences that must be preserved.

Two things that import deliberately does NOT bring. It does not bind
`_no_leaked_gate_tokens` beside it: that sibling is an autouse delete-and-fail fixture, and
binding its name would register it for every test in that module and change the pass/fail
semantics of tests this cycle has no business touching. And it does not enrol the module in
`gate_token_directory_deleters()`: arm B resolves an import through the imported module's
stem AND requires the imported NAME to be one of that module's arm-A functions, and
`definers["test_gate_token_binding"]` holds `_no_leaked_gate_tokens` only, because
`_mint_cleanup` removes a path it NAMED and lists no directory. Both halves are pinned, the
second one positively: the import edge now exists and arm B is genuinely evaluated and
refused on the name, where before the edge was absent and the same assertion held vacuously.

"The module now imports a cleanup helper" is not evidence that all five sites are covered,
so that claim is derived too:
`tests/_inventory.py::gate_token_mint_wrapping(module_path)` returns the mint line numbers
split into wrapped and unwrapped, and the assertion requires a NON-EMPTY wrapped list beside
the empty unwrapped one, so a derivation that found no mint at all cannot read clean. It
keys on `_mint_cleanup` specifically rather than on any `with` block, pinned by a synthetic
module that mints inside an unrelated context manager and must still read unwrapped.

The remaining three modules take a plain declared prefix, each naming a namespace its own
ids already sit inside: `test-hardening-` for `tests/test_hardening.py` (whose module-local
fixture returns `test-hardening-session`) and `aw-` for
`tests/test_pol6f_approval_workflow_extraction.py` (whose ids are `f"aw-{uuid4().hex[:8]}"`).
`aw-` is three characters, so `bin/lib/analyzers-regex.sh` IDENT_ASSIGN never reaches it;
`test-hardening-` is reached and exempt because NAMESPACE_PREFIX forgives a lowercase value
ending in a bare separator, which is also why `module_session_id` terminates its value with a
hyphen. That termination is for single-source-of-truth with the sweep pattern first and the
scanner second, not an evasion of it.

**The replacement for the deleted xfail is a POSITIVE population assertion.** The strict
marker said in its own reason string that it would XPASS once the five were wired, so
leaving it would have reddened the suite. It is gone, together with the `UNWIRED_*`
constants, and the test under it is an ordinary unmarked assertion that no member of the map
reads `none`. It cannot pass vacuously: the derivation is asserted non-empty against the real
tree beside it. What a future author reads when a sixth module lands unwired is built by
`unwired_module_report()` and pinned on synthetic input, so the refusal exists and is
readable without waiting for a red run. It names the offenders, all three sanctioned
neutralizers in `_gtm_kind` precedence order, the file each one lives in, and the map to
update, because a refusal that names no action is a deadlock.

**What this cycle does NOT do, stated so it is not read later as an oversight.** The file
`/tmp/writ-gate-token-test-session` on the build machine is left in place. After this change
no module mints the id `test-session` at all, so no declared prefix covers that exact name,
and it is a pre-existing leftover sitting inside the session baseline where it can never fail
anything. Removing it is an operator action, exactly as the bullet below says of the other
leftovers.

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
  it is looking at. The existing per-test copies carried exactly this hazard (they removed
  ANY `/tmp/writ-gate-token-*` file that appeared during a test, including one a real
  session minted concurrently); the first cycle did not fix them, it only made sure the new
  mechanism could not add to it. **Decision 6 closes that, and this bullet is the reason it
  exists:** the copies now delete only what cannot be a live session's own approval.
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
- **The five modules that once read `none` are wired, and the earlier reading of them as
  "latent" was WRONG for two of them.** The first cycle recorded `test_hardening.py`,
  `test_mode_infrastructure.py`, `test_phase3_centralization.py`,
  `test_phase_advance_unified.py` and `test_pol6f_approval_workflow_extraction.py` as
  minting but leaving nothing on disk, measured by running all five together and watching
  the `/tmp` count hold at 39. That measurement was true and the conclusion drawn from it
  was not: a MODULE run masks this class of leak, because a later test on the same path
  consumes the token the earlier refusal left behind. Run alone,
  `test_mode_infrastructure.py::TestModeAdvancePhase::test_advance_denied_for_nonwork_mode`
  and `test_phase3_centralization.py::TestAdvancePhase::test_advance_fails_without_plan_md`
  each left a real 37-byte file, and because both took the shared fixture's single literal
  default it was the SAME file. Decision 7 wires all five; the map now maps no module to
  `none`, and the two nodes above are the operational check, run one at a time with the
  `/tmp` listing compared across each.
- The 39 leftover files from previous runs are left in place by the guard, by design. The
  ones inside a declared namespace are removed the next time their own module runs, because
  the sweeper globs the namespace rather than diffing the test; the rest are an operator
  action.
- **A test in the five confined modules can no longer delete a live approval, and the cost
  is one module of latency on one shape class.** A uuid-shaped file that appears during one
  of those tests survives, the test passes carrying a `ForeignGateTokenWarning` that names
  the path, and the module-scoped guard fails the module at its next boundary with the same
  path. A test that genuinely leaks a uuid-shaped token of its own (a future test written
  with a bare `uuid4()` id) is therefore reported one level coarser than before, by module
  instead of by test. That is the accepted cost of never deleting a human's approval, and
  it is bounded: the leak gets LOUDER (a failed module) rather than quieter.
  `tests/test_gate_token_deleter_confinement.py` calls the four modules' own `_sid` helpers
  live and requires each result to be rejected by `could_be_a_live_session`, so the day one
  of them is rewritten as a bare `uuid4()` that test says so.
- No production source changes, so no daemon restart is needed to adopt any of this.
  `writ/session/gate_token.py`, `hooks/scripts/auto-approve-gate.sh` and the hardcoded
  `/tmp` are untouched: that hardcoding is deliberate, so the bash writer and the python
  reader can never disagree about where a token lives.
