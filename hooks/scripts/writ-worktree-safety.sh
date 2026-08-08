#!/usr/bin/env bash
# Phase 2: enforce worktree gitignore safety (ENF-PROC-WORKTREE-001).
#
# PreToolUse on Bash. Denies a real `git worktree add` whose target path is
# project-local and not in .gitignore. Feature-flag gated.
#
# DETECTION IS QUOTE-AWARE, and that is the whole point of the extractor below.
# This hook used to decide with a raw substring match on the entire command text
# (`case "$CMD" in *"git worktree add"*`), so it fired on any command that merely
# CONTAINED those words. Reproduced live on 2026-08-08: a Bash command that passed a
# JSON string to a subprocess, with `git worktree add .worktrees/feature-x feature-x`
# sitting inside that string, was denied outright, and the whole multi-line script was
# recorded as the `target` in the audit log. A gate that refuses commands which only
# TALK about the thing it guards teaches the agent to work around the gate.
#
# The fix reuses the approach writ-bash-write-gate.sh already uses for its redirect and
# copy-destination extraction rather than inventing a second one: shlex(posix=False)
# keeps the quote characters ON each token, so a quoted mention stays a single argument
# token and can never sit in command position; the token stream is split on control
# operators so each command is judged separately; and leading NAME=value assignments and
# the transparent wrapper prefixes (command/env/exec/nohup/time/sudo/doas) are stepped
# over so `sudo git worktree add ...` still resolves to the verb `git`.
#
# COVERAGE LIMIT, stated in the same spirit as that file's: only literal, tokenizable
# invocations are seen. `bash -c "git worktree add ..."`, an alias, a wrapper script, a
# variable-built command or a here-doc will not be detected. This is a hygiene gate that
# fires on the ordinary spelling, not a containment boundary. `-C <dir>` is stepped over
# as a git global option but is NOT used to resolve the target path: resolution stays
# relative to the hook's cwd, exactly as before.
set -euo pipefail
HOOK_DIR="$(cd "$(dirname "$0")" && pwd)"
WRIT_DIR="$(cd "$HOOK_DIR/../.." && pwd)"
source "$WRIT_DIR/bin/lib/common.sh"
hook_instrument "writ-worktree-safety"

load_hook_env
SESSION_ID="$HOOK_SESSION_ID"
[ -z "$SESSION_ID" ] && exit 0
is_work_mode "$SESSION_ID" || exit 0

CMD="$HOOK_COMMAND"
# Cheap PREFILTER only -- the tokenizer below is the detector. Deliberately LOOSER than
# the old arm: `*worktree*` also catches `git -C dir worktree add`, which the old
# `*"git worktree add"*` match missed outright, and a stray match here costs one python
# spawn rather than a false deny.
case "$CMD" in
    *worktree*) ;;
    *) exit 0 ;;
esac

# Output is one TSV row, or nothing at all when no real invocation was found:
#   deny<TAB><target><TAB><reason>      target is project-local and not gitignored
#   allow<TAB><target>                  a real invocation with a gitignored target
VERDICT=$(WRIT_WT_CMD="$CMD" python3 <<'PY' 2>/dev/null || true
import os, re, shlex, sys

cmd = os.environ.get("WRIT_WT_CMD", "")

# NEWLINE IS A COMMAND SEPARATOR, AND shlex THROWS IT AWAY. `shlex.split` treats "\n" as
# ordinary whitespace, so it never appears as a token and the "\n" entry in CONTROL below
# matched nothing -- which meant a multi-line command was flattened into ONE segment whose
# verb is whatever the FIRST line starts with. Measured on
# "set -e\necho preparing\ngit worktree add scratch/x x": verb "set", no verdict, allowed.
# Any real invocation on any line after the first was invisible to this gate, and
# multi-line Bash commands are the common shape, so the gate was failing open rather than
# closed. Splitting here, before tokenizing, keeps that fix in one place.
#
# QUOTE-AWARE, because the naive `cmd.split("\n")` reintroduces the exact false positive
# this extractor was written to remove: a newline INSIDE a quoted string is data, not a
# separator, and cutting there turns one argument into fragments that can land in command
# position. The scan tracks quote state and only replaces newlines outside it.
SEP = "\x00"          # cannot occur in a real command line, so it is unambiguous


def split_commands(text):
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


try:
    tokens = shlex.split(split_commands(cmd), comments=False, posix=False)
except ValueError:
    sys.exit(0)          # unbalanced quotes etc -> fail open, never a false deny

# A heredoc BODY is data being fed to a command, not commands being run, so the lines
# between `<<WORD` and its terminator are dropped before any segment is judged. Without
# this the newline split above would newly refuse `cat <<'EOF' ... git worktree add ...
# EOF`, which is a document ABOUT the operation -- the same false-positive class the
# quote-aware rewrite existed to remove, reintroduced through a different door.
# `<<<` (herestring) is a single-token value, not a body, so it is deliberately not matched.
HEREDOC = re.compile(r'^<<-?(?!<)\s*([A-Za-z_][A-Za-z0-9_]*|"[^"]*"|\'[^\']*\')$')


def strip_heredoc_bodies(toks):
    out, i = [], 0
    while i < len(toks):
        m = HEREDOC.match(toks[i])
        if not m:
            out.append(toks[i])
            i += 1
            continue
        terminator = m.group(1).strip('"\'')
        i += 1
        while i < len(toks) and toks[i] != terminator:
            i += 1
        i += 1                                # step over the terminator itself
    return out


tokens = strip_heredoc_bodies(tokens)

CONTROL = {"|", "||", "&&", ";", "&", SEP}
ASSIGNMENT = re.compile(r'^[A-Za-z_][A-Za-z0-9_]*=')
# Transparent prefixes that sit IN FRONT of the real verb, same list and same reason as
# writ-bash-write-gate.sh's WRAPPERS. Parsed permissively (any dash token is stepped
# over, plus the next token for the few flags that take a value), because this gate only
# has to answer "is the verb git", not "which file does it write".
WRAPPERS = {"command", "env", "exec", "nohup", "time", "sudo", "doas"}
WRAPPER_VALUE_FLAGS = {
    "-u", "--user", "-g", "--group", "-p", "--prompt", "-C", "--chdir",
    "-a", "-o", "--output", "-f", "--format", "-S", "--split-string", "--unset",
}
# git's global options that consume the NEXT token, plus `worktree add`'s own
# value-taking options. Kept in ONE set because the positional walk below only needs to
# know which tokens are values, not which layer they belong to.
GIT_VALUE_FLAGS = {
    "-C", "-c", "--git-dir", "--work-tree", "--exec-path", "--namespace",
    "--super-prefix", "--config-env",
    "-b", "-B", "--reason",
}


def dequote(t):
    if len(t) >= 2 and t[0] == t[-1] and t[0] in ("'", '"'):
        return t[1:-1]
    return t


def flat(field):
    """One TSV field. Collapses every whitespace run, so a crafted argument carrying a
    tab or a newline cannot forge an extra field or an extra row."""
    return " ".join(str(field).split())


def verb_at(seg):
    """(effective verb, index of its first argument) for one segment.

    ("", len(seg)) when the segment has no verb at all (assignments only)."""
    i = 0
    while i < len(seg):
        tok = dequote(seg[i])
        if ASSIGNMENT.match(tok):
            i += 1
            continue
        # A leading backslash only suppresses alias lookup: `\git` is still git.
        name = os.path.basename(tok[1:] if tok.startswith("\\") else tok)
        if name not in WRAPPERS:
            return name, i + 1
        i += 1
        while i < len(seg):                 # the wrapper's own assignments and flags
            nxt = dequote(seg[i])
            if ASSIGNMENT.match(nxt):
                i += 1
                continue
            if nxt == "--":                 # end of the wrapper's options
                i += 1
                break
            if not nxt.startswith("-") or nxt == "-":
                break
            i += 1
            if nxt in WRAPPER_VALUE_FLAGS:  # `--flag=value` carries its own value
                i += 1
    return "", len(seg)


def positionals(args):
    """Non-flag arguments, with the value of each value-taking flag consumed."""
    out, i = [], 0
    while i < len(args):
        a = dequote(args[i])
        if a.startswith("-") and a != "-":
            if a in GIT_VALUE_FLAGS:
                i += 1                      # `--flag value`: skip the value too
            i += 1
            continue
        out.append(a)
        i += 1
    return out


segments, cur = [], []
for t in tokens:
    if t in CONTROL:
        if cur:
            segments.append(cur)
        cur = []
    else:
        cur.append(t)
if cur:
    segments.append(cur)

target = None
for seg in segments:
    if not seg:
        continue
    verb, arg0 = verb_at(seg)
    if verb != "git":
        continue
    pos = positionals(seg[arg0:])
    # `git [globals] worktree add [opts] <path> [branch]`
    if len(pos) >= 3 and pos[0] == "worktree" and pos[1] == "add":
        target = pos[2]
        break

if target is None:
    sys.exit(0)

# Absolute paths or paths outside the repo tree are not project-local.
repo_root = os.getcwd()
abs_target = os.path.abspath(target)
if not abs_target.startswith(repo_root + os.sep) and abs_target != repo_root:
    sys.exit(0)
# Compute path relative to repo root.
rel = os.path.relpath(abs_target, repo_root)
# Check .gitignore for a matching entry.
ignore_path = os.path.join(repo_root, ".gitignore")
if not os.path.exists(ignore_path):
    print("deny\t%s\t%s" % (flat(rel), flat(
        f"ENF-PROC-WORKTREE-001: project-local worktree target '{rel}' but no .gitignore "
        f"exists. Add an entry for '{rel}' (or a parent like '.worktrees/') before "
        f"creating the worktree.")))
    sys.exit(0)
with open(ignore_path) as f:
    ignored = [line.strip() for line in f if line.strip() and not line.startswith("#")]
# Match the rel path against gitignore patterns. Simple prefix match for directories.
top = rel.split(os.sep)[0]
matched = any(
    top == p.strip("/") or p.rstrip("/") == top or p.startswith(top + "/")
    for p in ignored
)
if matched:
    print("allow\t%s" % flat(rel))
else:
    print("deny\t%s\t%s" % (flat(rel), flat(
        f"ENF-PROC-WORKTREE-001: project-local worktree target '{rel}' is not matched by "
        f"any .gitignore entry. Add '{top}/' to .gitignore before creating the "
        f"worktree.")))
PY
)

# No row at all means no real `git worktree add` in this command: nothing to record, the
# same silence the old case-arm early exit produced for a non-matching command.
[ -z "$VERDICT" ] && exit 0

IFS=$'\t' read -r WT_DECISION WT_TARGET WT_REASON <<< "$VERDICT"

# else, not fallthrough: emit_deny only PRINTS the deny JSON (it does not exit),
# so a bare trailing allow-record would fire on the deny path too.
#
# The TARGET recorded is the worktree path, not "$CMD". The old code logged the whole
# command text, which is how an entire multi-line script ended up in the target column of
# the audit log on the false-positive above.
if [ "$WT_DECISION" = "deny" ]; then
    log_gate_decision "worktree-safety" "deny" "$WT_REASON" "${WT_TARGET:-}"
    emit_deny "$WT_REASON"
else
    log_gate_decision "worktree-safety" "allow" "worktree target is gitignored" "${WT_TARGET:-}"
fi
exit 0
