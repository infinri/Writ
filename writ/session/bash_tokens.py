"""Bash control-operator splitting, shared by the hooks that tokenize Bash text.

SINGLE SOURCE. `shlex.split(cmd, posix=False)` forces whitespace_split=True, so an
unquoted run of non-whitespace is ONE token whatever punctuation it carries: a control
operator written with no space in front of it stays glued to the token before it.

    shlex.split('foo > src/log.txt; bar', posix=False)
    -> ['foo', '>', 'src/log.txt;', 'bar']

Every consumer of those tokens then reads a corrupted value (`src/log.txt;`), and the
operator never becomes a token, so the command AFTER it is judged as a trailing argument
of the command before it and never reaches command position. Measured: a credential write
spelled `echo x > .env; ls` was ALLOWED, because `.env;` is not a credential BASENAME,
while `echo x > .env ; ls` was denied.

Both hooks that tokenize Bash text share this: hooks/scripts/writ-bash-write-gate.sh
(write targets, credential classification, egress destinations) and
hooks/scripts/writ-worktree-safety.sh (`git worktree add` detection). Each MIRRORS the
marker-delimited block below verbatim as an inline fallback, because the hooks run under
the system python3 and reach this package only through `sys.path.insert(0, WRIT_DIR)`; a
failed import must degrade to the same behavior, never silently back to the defect.
tests/test_bash_control_operator_split.py asserts the copies are textually identical AND
that they compute identical token streams, so the mirror cannot drift.

This module imports nothing on purpose: it has to load under `python -S` and it has to be
safe to paste inline.
"""

# MIRROR BEGIN split_control_operators
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
# MIRROR END split_control_operators
