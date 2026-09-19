"""The value a real shell resolves a WORD to: the single source for `expand_word`.

WHY THIS IS ITS OWN MODULE and not another function in `writ/session/bash_tokens.py`:
that module's docstring states a zero-import contract ("This module imports nothing on
purpose"), which `tests/_inventory.py` cites when it explains why the MIRROR span is not
excluded from its shlex guard. `expand_word` needs `os`, `re` and `pwd`, so putting it
there would falsify a contract other guards lean on. The dependency runs ONE direction
only: this module imports `strip_unbalanced_close` from `bash_tokens`, never the reverse.

WHY IT IS ALSO PASTED INLINE IN TWO HOOKS. Both Bash gates
(`hooks/scripts/writ-bash-write-gate.sh`, `hooks/scripts/writ-worktree-safety.sh`) run
under the SYSTEM python3 and reach this package only through
`sys.path.insert(0, WRIT_DIR)`. A failed import must degrade to the SAME behavior, never
silently back to the defect each gate exists to close, so each hook carries the
marker-delimited block below verbatim and then REBINDS the name from here. Three copies,
one derived identity ratchet: `tests/_inventory.py::expand_word_copy_sites` finds every
file in the tree carrying the EXPAND markers by scanning for them, so a fourth pasted copy
fails by name instead of waiting for someone to update a hardcoded tuple.

WHY A SEPARATE MARKER FAMILY from `MIRROR BEGIN/END`. The mirror block holds
`split_control_operators` and its siblings to a stricter contract than this text can meet:
`tests/test_bash_control_operator_split.py` execs it in an EMPTY namespace and rejects any
import statement between the markers. This block carries no import either, but it needs
`os`, `re`, `pwd` and `strip_unbalanced_close` supplied by its caller, so it gets its own
markers and its own exec namespace rather than loosening the mirror's contract.

WHAT IT ANSWERS. `expand_word(raw)` returns a 2-TUPLE `(value, unresolved)`: the string
bash would produce for this word under tilde expansion, simple-name parameter expansion,
quote removal and backslash escapes, and the NAME of the first simple-name parameter the
CALLER's environment could not resolve ("" when everything resolved). The name rather than
a bare bool, because the confirmation this drives has to say WHICH variable could not be
seen. The forms it deliberately leaves LITERAL (the three directory-stack tildes, command
and process substitution, arithmetic and brace expansion, globbing, the
parameter-expansion operator forms, positional and special parameters) are disclosed in
each caller's own unexpanded-forms header block, because what a caller does about them is
the caller's decision: the write gate classifies them in-project, the worktree hook asks.
"""

import os
import re

from writ.session.bash_tokens import strip_unbalanced_close

# `~name` resolves through the password database (see expand_word below), so an
# unavailable module must leave that spelling LITERAL rather than raise and take the
# importing program down with it. Same guard, same reason, as the two inline hook copies.
try:
    import pwd
except Exception:
    pwd = None


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
