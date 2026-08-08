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
# var-indirection, eval/base64, a path assembled from pieces, glued `foo>bar` -- WILL
# evade. This narrows the hole, it does not seal it.
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
# wrapper prefixes (command / env / exec / nohup / time, and sudo / doas) with their own
# flags, and a leading backslash all sit in FRONT of the real command. One helper serves
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
# variable-indirected URLs and hosts, glued forms the tokenizer does not split, and any
# verb not named above (ftp, aws s3 cp, rclone, git remotes over http). Still-uncovered
# prefixes, because each takes non-flag positional arguments of its own before the
# command and a naive skip would mis-read the verb: timeout, stdbuf, nice, setsid,
# xargs, watch. Still-uncovered destination overrides, because they are not command
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
            GUARD_REASON="[ENF-GATE-STATE] Refusing this Bash command: it names Writ gate state ('$STATE_DIR_GUARD'). Mode, approvals, the manual-testing grant and recorded review verdicts live there, and a gate the agent can edit is not a gate, so a command that could execute, expand, or write is refused in any mode. Plain read-only inspection (grep/cat/ls pipelines with no redirects, substitution, or control operators) is allowed, and the Read tool covers the rest. A manual-testing bypass is minted only from the user's own words: ask the user to reply \"manual testing approved\". A CRITICAL review verdict is cleared only by fixing the findings and re-running writ-reviewer, never by writing the record directly."
            log_gate_decision "bash-write" "deny" "$GUARD_REASON" "$STATE_DIR_GUARD"
            emit_deny "$GUARD_REASON"
            exit 0
        fi
        ;;
esac

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

_GIT_COMMIT_RE='(^|[;&|]|&&|\|\|)[[:space:]]*([A-Za-z_][A-Za-z0-9_]*=[^[:space:]]*[[:space:]]+)*(env[[:space:]]+|command[[:space:]]+|sudo[[:space:]]+|nohup[[:space:]]+)*git([[:space:]]+-[^[:space:]]+([[:space:]]+[^[:space:]-][^[:space:]]*)?)*[[:space:]]+commit([[:space:]]|$)'

case "$CMD" in
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
case "$CMD" in
    *">"* | *"tee "* | *"dd "* | *"cp "* | *"mv "* | *"install "* \
    | *"sed -i"* | *"sed --in-place"* | *"--in-place"* | *"--target-directory"* \
    | *"wget "* | *"scp "* | *"rsync "* | *"sftp "* | *"gist "* \
    | *"nc "* | *"ncat "* | *"netcat "* | *"telnet "* | *"curl "* ) ;;
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
        case "$CMD" in
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
# `state` (Writ gate state, deny everywhere) or `local` (project-local abspath,
# work-gate it); plus "egress\t<host>\t<detail>" per non-allowlisted destination.
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
}
# Assignments that silently move the real destination. `no_proxy` is excluded: it
# DISABLES proxying, it does not redirect. Compared lowercased, so HTTPS_PROXY counts.
PROXY_ASSIGN_NAMES = {"http_proxy", "https_proxy", "all_proxy"}


def verb_at(seg):
    """(effective verb, index of its first argument, leading NAME=value assignments).

    ("", len(seg), ...) when the segment has no verb at all (assignments only) or when a
    STRICT wrapper's options could not be classified. SINGLE SOURCE: both the write
    extractor's cmd0 and the egress pass resolve through this, so the two vectors cannot
    drift on what "the command" is.
    """
    assigns = []
    i = 0
    while i < len(seg):
        tok = dequote(seg[i])
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
    return "", len(seg), assigns


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
# Also the STDIN forms -- a bare `-`, a heredoc (`<<'PY'`), an input redirect
# (`python3 < script.py`) and an ARGUMENT-FREE interpreter fed by a pipe
# (`printf '...' | python3`, which runs its stdin with no marker on the command) --
# for which the WHOLE command text is scanned instead, since the source arrives from
# another segment or from a heredoc body that is not an argument.
# NOT COVERED, knowingly: `python -m MODULE` (module execution, not inline code -- a
# module that itself writes, like py_compile, is not seen; the check bails there
# because everything after `-m` is the module's own arguments, `-c`/`-p` included);
# `sh -c` / `bash -c` (their body is shell, already parsed by the vectors above, and
# gating every `bash -c` would gate most tooling); awk/sed program text; an
# interpreter reached through a variable, an alias or a wrapper script; a path built
# by concatenation or held in a variable (`open(P,'w')`); base64/eval-obfuscated
# source; and a path whose basename carries no recognized extension and no `/`, `./`
# or `~/` sigil (`open('scratch','w')`).
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
    """Project-path candidates carried by these tokens."""
    out = []
    for tok in toks:
        for lit in token_literals(tok):
            out += [c for c in PATH_CAND.findall(lit) if looks_like_path(c)]
    return out

try:
    tokens = shlex.split(cmd, comments=False, posix=False)
except ValueError:
    sys.exit(0)   # unbalanced quotes etc -> fail open (no false deny)

# Segment on control operators so each command's dest logic is scoped. Each segment
# carries one extra bit -- whether the control token BEFORE it was a pipe -- which the
# egress pass needs to know that local data feeds the segment. Write extraction below
# unpacks the pair and is otherwise untouched.
segments, cur, piped = [], [], False
for t in tokens:
    if t in CONTROL:
        if cur:
            segments.append((cur, piped))
        cur = []
        piped = t == "|"
    else:
        cur.append(t)
if cur:
    segments.append((cur, piped))

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
            raw_targets.append(dequote(tgt))
            if tgt == nxt:
                skip_next = True
    # 2. command-specific write destinations (skip pure test/arith commands).
    if seg_is_test:
        continue
    args = [dequote(a) for a in seg[cmd_arg0:]]
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

# Inline-interpreter pass: same segments, third question -- does this command hand an
# interpreter source code that NAMES a project file? Its hits join raw_targets, so
# every path below is classified and gated identically whether it came from a redirect
# or from `python3 -c`. Runs on the RAW segment tokens: shlex(posix=False) keeps the
# quote characters, and PATH_CAND excludes them, so quotes act as delimiters.
stdin_interpreter = False
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
        raw_targets += scan_tokens(args)
    elif form == "stdin":
        stdin_interpreter = True
if stdin_interpreter:
    # The source is not in this segment's arguments: it arrives over a pipe, from a
    # heredoc body, or from an input redirect. Every token of the command is the only
    # place it can be, so paths named by ANY segment count -- `cat notes.md | python3 -`
    # is gated on notes.md. Coarser than the flag form, deliberately: a stdin-fed
    # interpreter is itself the strong signal, and the answer to "the code is somewhere
    # in here" must not be silence.
    raw_targets += scan_tokens(tokens)

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
    RESP=$(curl -sf --connect-timeout 0.2 --max-time 1 \
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

# 3. Egress destinations: ASK the user. Placed LAST on purpose -- every deny above
# outranks a confirmation, so this is reached only by a command with nothing stronger
# against it. No mode is read and no server is called here: a pure-egress command has
# no `local` target, so the loop above never ran.
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
