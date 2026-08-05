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
#   2. WORK-GATE (project-local targets): feed the abspath to the same server
#      gate the Write tool uses (POST /session/<id>/can-write -> _can_write_check),
#      so a Bash write to project source is plan-gated like a Write. Targets
#      outside the repo (e.g. /tmp scratch) are not work-gated -- mirrors
#      writ-worktree-safety, which also acts only on project-local paths.
#
# Redirect detection is QUOTE-AWARE: shlex(posix=False) keeps quote chars on
# tokens, so a quoted '>' (e.g. `grep '>' app.pem`) is NOT treated as an operator;
# and `[[ ]]` / `[ ]` / `test` / `(( ))` comparison/arith spans are suppressed.
#
# COVERAGE LIMIT (no silent caps): only common LITERAL vectors are detected
# (`>`/`>>`/`2>`/`&>`/`>|`, `tee`, `dd of=`, `cp`/`mv`/`install` dest incl. -t,
# `sed -i`). Obfuscated writes -- var-indirection, eval/base64, here-docs,
# `python -c "open(...,'w')"`, glued `foo>bar` -- WILL evade. This narrows the
# hole, it does not seal it.
#
# Hook type: PreToolUse (matcher: Bash). Exit: always 0 (deny via emit_deny JSON).
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

# Gate state is protected by a blanket path check BEFORE the write-verb early exit
# below. The extractor only understands shell write forms (>, cp, mv, tee, sed -i),
# so an interpreter one-liner -- python3 -c "json.dump(...open(p,'w'))", node -e,
# perl -e -- reaches the file without ever matching one. Naming the path is
# therefore refused unless the command is provably read-only (below).
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
case "$CMD" in
    *"$STATE_DIR_GUARD"* | *"/tmp/writ-current-session"* | *"writ-session-"* \
    | *"writ-manual-test-grant"* | *"manual_test_grant"* | *"writ-grant-"*)
        if _readonly_inspection "$CMD"; then
            log_gate_decision "bash-write" "allow" "read-only inspection naming gate state" ""
        else
            GUARD_REASON="[ENF-GATE-STATE] Refusing this Bash command: it names Writ gate state ('$STATE_DIR_GUARD'). Mode, approvals and the manual-testing grant live there, and a gate the agent can edit is not a gate, so a command that could execute, expand, or write is refused in any mode. Plain read-only inspection (grep/cat/ls pipelines with no redirects, substitution, or control operators) is allowed, and the Read tool covers the rest. A manual-testing bypass is minted only from the user's own words: ask the user to reply \"manual testing approved\"."
            log_gate_decision "bash-write" "deny" "$GUARD_REASON" "$STATE_DIR_GUARD"
            emit_deny "$GUARD_REASON"
            exit 0
        fi
        ;;
esac

# Cheap hot-path early-exit: a command with no write operator (the vast majority --
# ls, git, grep, test runs) never spawns the python extractor. Loose on purpose
# (a stray match only costs one spawn, never a false deny -- the extractor decides).
case "$CMD" in
    *">"* | *"tee "* | *"dd "* | *"cp "* | *"mv "* | *"install "* \
    | *"sed -i"* | *"sed --in-place"* | *"--in-place"* | *"--target-directory"* ) ;;
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
                printf '%s\n' "$SWAP"
            fi
        fi
        exit 0
        ;;
    *) exit 0 ;;
esac

# Extract write targets. Output lines: "<kind>\t<path>" where kind is `cred`
# (credential, deny everywhere) or `local` (project-local abspath, work-gate it).
TARGETS=$(WRIT_BASH_CMD="$CMD" WRIT_CWD="$(pwd)" WRIT_DIR="$WRIT_DIR" python3 <<'PY' 2>/dev/null || true
import os, re, shlex, sys

cmd = os.environ.get("WRIT_BASH_CMD", "")
cwd = os.environ.get("WRIT_CWD", "") or os.getcwd()

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
CONTROL = {"|", "||", "&&", ";", "&", "\n"}
REDIR = re.compile(r'^(?:&|[0-9]*)>>?')

def dequote(t):
    if len(t) >= 2 and t[0] == t[-1] and t[0] in ("'", '"'):
        return t[1:-1]
    return t

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

try:
    tokens = shlex.split(cmd, comments=False, posix=False)
except ValueError:
    sys.exit(0)   # unbalanced quotes etc -> fail open (no false deny)

# Segment on control operators so each command's dest logic is scoped.
segments, cur = [], []
for t in tokens:
    if t in CONTROL:
        if cur:
            segments.append(cur); cur = []
    else:
        cur.append(t)
if cur:
    segments.append(cur)

raw_targets = []
for seg in segments:
    if not seg:
        continue
    # 1. redirects -- suppressed inside [[ ]] / [ ] / test / (( )) comparison+arith spans.
    arith = 0
    test_ctx = 0
    cmd0 = dequote(os.path.basename(seg[0]))
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
            raw_targets.append(dequote(tgt))
            if tgt == nxt:
                skip_next = True
    # 2. command-specific write destinations (skip pure test/arith commands).
    if seg_is_test:
        continue
    args = [dequote(a) for a in seg[1:]]
    if cmd0 == "tee":
        for a in args:
            if a.startswith("-"):
                continue
            if a.startswith(("<", ">")) or REDIR.match(a):  # redirect / process-sub ends the list
                break
            raw_targets.append(a)
    elif cmd0 == "dd":
        raw_targets += [a[3:] for a in args if a.startswith("of=") and a[3:]]
    elif cmd0 in ("cp", "mv", "install"):
        tdir = None
        skip = False
        for j, a in enumerate(args):
            if skip:
                skip = False; continue
            if a in ("-t", "--target-directory"):
                if j + 1 < len(args):
                    tdir = args[j + 1]; skip = True
            elif a.startswith("--target-directory="):
                tdir = a.split("=", 1)[1]
            elif a.startswith("-t") and len(a) > 2:
                tdir = a[2:]
        if tdir is not None:
            raw_targets.append(tdir)        # -t DIR: the dir IS the write dest
        else:
            cand = [a for a in args if not a.startswith("-") and REDIR.match(a) is None]
            if len(cand) >= 2:              # last positional is the destination
                raw_targets.append(cand[-1])
    elif cmd0 == "sed":
        if any(a == "-i" or a.startswith("-i") or a == "--in-place" or a.startswith("--in-place") for a in args):
            files = [a for a in args if not a.startswith("-") and REDIR.match(a) is None]
            if files:                       # the edited file is the LAST positional (skip the script)
                raw_targets.append(files[-1])

seen = set()
for t in raw_targets:
    if t in NONFILE or t in seen:
        continue
    seen.add(t)
    if is_cred(t):
        print(f"cred\t{t}")
        continue
    if is_gate_state(t):
        print(f"state\t{t}")
        continue
    ap = t if os.path.isabs(t) else os.path.normpath(os.path.join(cwd, t))
    # Work-gate only project-local targets. Scratch writes outside the repo are not plan-gated.
    if ap == cwd or ap.startswith(cwd + os.sep):
        print(f"local\t{ap}")
PY
)

[ -z "$TARGETS" ] && exit 0

# 1. Credential targets: deny in any mode, no server needed (org boundary).
CRED_HIT=$(printf '%s\n' "$TARGETS" | awk -F'\t' '$1=="cred"{print $2; exit}')
if [ -n "$CRED_HIT" ]; then
    emit_deny "[SEC-CREDENTIAL-WRITE] Refusing this Bash command: it writes to a credential/secret path ('$CRED_HIT'). Secret material must not be written or overwritten by the agent. Name non-secret templates .env.example / .env.sample / *.pub."
    exit 0
fi

# 1b. Writ gate state: deny in any mode. A gate the agent can edit is not a gate.
STATE_HIT=$(printf '%s\n' "$TARGETS" | awk -F'\t' '$1=="state"{print $2; exit}')
if [ -n "$STATE_HIT" ]; then
    STATE_REASON="[ENF-GATE-STATE] Refusing this Bash command: it writes to Writ gate state ('$STATE_HIT'). Mode, approvals and the manual-testing grant live there. A manual-testing bypass is minted only from the user's own words -- ask the user to reply \"manual testing approved\"."
    log_gate_decision "bash-write" "deny" "$STATE_REASON" "$STATE_HIT"
    emit_deny "$STATE_REASON"
    exit 0
fi

# 2. Project-local targets: run the SAME write gate the Write tool uses.
SKILL_DIR="$WRIT_DIR"
while IFS=$'\t' read -r kind path; do
    [ "$kind" = "local" ] || continue
    [ -z "$path" ] && continue
    BODY=$(WRIT_AP="$path" WRIT_SD="$SKILL_DIR" python3 -c "
import os, json
print(json.dumps({'tool_input': {'file_path': os.environ['WRIT_AP']}, 'skill_dir': os.environ['WRIT_SD']}))" 2>/dev/null) || continue
    RESP=$(curl -sf --connect-timeout 0.2 --max-time 1 \
        -X POST "${WRIT_SESSION_BASE}/session/${SESSION_ID}/can-write" \
        -H "Content-Type: application/json" -d "$BODY" 2>/dev/null) || true
    if [ -z "$RESP" ]; then
        # Daemon unreachable: fall back to the same local subprocess the Write
        # gate uses ({"decision": allow|deny} shape), so an outage does not
        # ungate Bash writes. Only a NO-ANSWER (fallback also failed) is left
        # to policy: fail open by default, fail closed under WRIT_STRICT=1.
        RESP=$(printf '%s' "$BODY" | _writ_session can-write "$SESSION_ID" --skill-dir "$SKILL_DIR" 2>/dev/null | python3 -c "
import sys, json
try:
    r = json.load(sys.stdin)
except Exception:
    raise SystemExit
print(json.dumps({'can_write': r.get('decision', 'allow') != 'deny', 'reason': r.get('reason')}))" 2>/dev/null) || true
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
    DENY_REASON=$(printf '%s' "$RESP" | WRIT_TGT="$path" python3 -c "
import sys, json, os
try:
    r = json.load(sys.stdin)
except Exception:
    raise SystemExit
if not r.get('can_write', True):
    reason = r.get('reason') or 'Write blocked by a Writ gate.'
    print(f\"[Bash write to {os.path.basename(os.environ['WRIT_TGT'])}] {reason}\")" 2>/dev/null) || true
    if [ -n "$DENY_REASON" ]; then
        log_gate_decision "bash-write" "deny" "$DENY_REASON" "$path"
        emit_deny "$DENY_REASON"
        exit 0
    fi
    log_gate_decision "bash-write" "allow" "no gate objection" "$path"
done <<< "$TARGETS"

exit 0
