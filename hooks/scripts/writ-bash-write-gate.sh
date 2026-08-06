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
# prompts. sudo/doas are parsed STRICTLY: an option the classifier cannot place bails to
# no-detection rather than risk naming the wrong verb, so `sudo -u deploy scp f h:/p` is
# covered while an exotic option shape stays where it was before this change.
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
    *) exit 0 ;;
esac

# Extract write targets AND egress destinations in ONE python spawn.
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
    "-v", "--validate", "-V", "--version", "-e", "--edit", "-h", "--help",
})
DOAS_VALUE_FLAGS = frozenset({"-u", "-a", "-C"})
DOAS_NOVALUE_FLAGS = frozenset({"-L", "-n", "-s"})
# Wrapper -> (flags whose value is the NEXT token, flags known to take no value).
#
# A None second element means PERMISSIVE: any dash token is skipped. Safe for these
# five -- their option sets are tiny and stable and they take no positional arguments
# of their own, so the token after the options IS the verb.
#
# A set means STRICT: an unclassifiable dash token BAILS to no-detection. sudo's option
# grammar is large enough that guessing wrong would swallow the real verb and could
# raise a prompt naming the WRONG command, which is worse than a miss. Bailing is never
# worse than the pre-fix behavior (`sudo <anything>` was undetected outright), and it
# still closes the everyday shapes (`sudo curl -d @x URL`, `sudo -u deploy scp f h:/p`).
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
            base = nxt.split("=", 1)[0] if nxt.startswith("--") else nxt
            if base in novalue_flags:
                i += 1
                continue
            if base in value_flags:
                i += 1
                if "=" not in nxt:            # `--user=x` carries its value, `-u x` does not
                    i += 1
                continue
            if (not nxt.startswith("--") and len(nxt) > 2
                    and all("-" + c in novalue_flags for c in nxt[1:])):
                i += 1     # a bundle of value-less short flags consumes nothing after it
                continue
            return "", len(seg), []           # unclassifiable option: bail, detect nothing
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
