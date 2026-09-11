"""Bash tokenization helpers shared by the hooks that parse Bash text.

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

A SECOND SPLITTING DEFECT lives in the same call, pointing the other way. `posix=False`
also ends a token at the closing quote of a span that STARTED that token, so ONE shell word
arrives as TWO tokens whenever an unquoted fragment is glued after a leading quoted span.

    shlex.split('cp x "$HOME"/y', comments=False, posix=False)
    -> ['cp', 'x', '"$HOME"', '/y']        one bash word, TWO tokens

A token that starts UNQUOTED absorbs the quoted spans that follow instead, which is why
only one of the two spellings was ever broken. Three DIFFERENT consequences were measured
through the write gate's own extractor, and whether the leftover fragment starts with `/`
decides the direction: `cp x "$HOME"escape.txt` read as project-local while bash wrote
OUTSIDE the project (a boundary ESCAPE, more permissive); `cp x "src"/escape.txt` read as
`/escape.txt` outside the project while bash wrote `src/escape.txt` (an over-block); and
`echo hi > "$HOME"/escape.txt` kept the quoted head and dropped the FILENAME.
rejoin_glued_words repairs it, and the dequote beside it is PART of that repair rather
than a tidy-up: merging moves quote characters INTO tokens other consumers dequote, so
landing the rejoin alone would trade one defect for two.

Both hooks that tokenize Bash text share this: hooks/scripts/writ-bash-write-gate.sh
(write targets, credential classification, egress destinations) and
hooks/scripts/writ-worktree-safety.sh (`git worktree add` detection). Each MIRRORS the
marker-delimited block below verbatim as an inline fallback, because the hooks run under
the system python3 and reach this package only through `sys.path.insert(0, WRIT_DIR)`; a
failed import must degrade to the same behavior, never silently back to the defect.
tests/test_bash_control_operator_split.py asserts the copies are textually identical AND
that they compute identical token streams, so the mirror cannot drift.

NEWLINE PRE-SPLIT AND HEREDOC-BODY STRIPPING live here too, as of cycle R. They were
authored in hooks/scripts/writ-worktree-safety.sh in 1.7.0 and were NOT merely moved: the
stripper was EXECUTED and found wrong twice over (it swallowed the opener line's own
tokens, and it dropped the opener token that is the only marker of the `python3 <<'PY'`
stdin form). Both defects are recorded in
docs/adr/ADR-bash-control-operator-tokenizer.md, because the next person to reach for that
function needs to know the original was defective.

The zero-import rule below is what forced the opener test to be string operations rather
than the `re.compile` pattern it used to be. That is a CONTRACT, not a style choice: this
module has to load under `python -S`, it has to be safe to paste inline, and the mirror
test execs the block in an EMPTY namespace.

This module imports nothing on purpose: it has to load under `python -S` and it has to be
safe to paste inline.
"""

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
