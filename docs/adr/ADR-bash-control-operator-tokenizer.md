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

**Newly gated for ordinary writes, and STILL A MISS FOR SECRETS.** Splitting inside a
token also splits the body of an unquoted command substitution or a paren group: `(cd x; cp
a src/y.py)` tokenizes as `['(cd', 'x;', 'cp', 'a', 'src/y.py)']` and now segments, so the
`cp` destination is extracted as `src/y.py)` with a stray paren, where before the fix it
produced no target, no gate call and no audit row. For an ordinary path that is a net gain
with a cosmetically wrong character.

CALLING IT COSMETIC IS FALSE FOR A CREDENTIAL PATH, and the measurement says so. Group
constructs defeat the credential guard by TWO separate mechanisms, both present before this
cycle and both still present after it (verified by reverting):

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

Not corrected here, deliberately. Trailing-punctuation stripping is the per-consumer
patching this design replaced, and hardening the classifier against malformed input would
treat the symptom rather than the tokenizer that produced it. The group-construct blind
spot is its own cycle, and it is a SECURITY item rather than a tidiness one: the honest
statement is that the credential guard is defeated by a paren today.

**Residue that stays open, so the fix is not read as wider than it is.**

- `foo>src/x.py`, the operator glued to the verb in front of it.
- A shell KEYWORD in verb position: `then`, `do`, `else` and `{` are not stepped over the
  way the `WRAPPERS` prefixes are, so in `if true; then cp a src/x.py; fi` the segment's
  verb resolves to `then` and the write is not seen, even though the `;` now splits.
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
