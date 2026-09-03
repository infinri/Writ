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
- `coproc`, `function`: both take an optional NAME before the command, the same reason
  `timeout`, `stdbuf`, `nice`, `setsid`, `xargs` and `watch` are absent from `WRAPPERS`.

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
