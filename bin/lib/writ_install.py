#!/usr/bin/env python3
"""The one install-time module: settings/CLAUDE.md/hooks/commands, plus the HTTP shim.

WHY THIS LIVES IN bin/lib/ AND NOT IN THE `writ/` PACKAGE. Every caller runs it under
bare system `python3`, before or independent of the venv: `scripts/bootstrap.sh` and
`scripts/bootstrap-plugin.sh` call it (directly or through the two shims) while the venv
is still being built, `scripts/patch-global-config.sh` runs on a machine that may have no
venv at all, and `bin/lib/common.sh` calls its `http-*` subcommands from hooks. That is the
same constraint that put `memory_capture.py`, `manual_test_grant.py` and
`gate_advance_outcome.py` here: stdlib only, no `writ` import, callable by absolute path.

WHY THE HTTP SHIM SHARES THIS HOME. `http-get` / `http-post` are the other half of the same
job this module exists for: being a stdlib substitute for a command-line tool Writ no longer
requires (`jq`, `curl` and gettext's variable substituter are optional accelerators, never
prerequisites). Putting the urllib fallback anywhere else would create a second "what we do
without curl" home to keep in sync, which is the defect class this cycle is closing, not
opening.

BEHAVIOR IS PORTED ONE-FOR-ONE from the jq-plus-gettext implementation that used to live in
scripts/patch-global-config.sh, with exactly one deliberate change: a missing settings.json
is CREATED (parents included) instead of being a hard error, because that is the common case
on a fresh machine and was the single largest reason the install needed hand-holding. A file
that never existed gets no `.bak`, mirroring the CLAUDE.md create branch.

Preserved exactly: append-only allow/deny merge (existing entries keep their order), the
two-guard stale-entry pruner (basename must be writ/Writ AND the directory must be gone, so a
live second checkout survives), the LEGACY_ALLOW subtraction, the statusLine policy (add when
absent, refresh an existing writ-statusline.sh, never clobber a foreign one), the per-event
hooks merge that preserves a user's own hook, and the refusal to seed hooks when a loaded
plugin resolves to this install.

Usage:
  python3 bin/lib/writ_install.py settings   --target PATH [--skill-dir DIR] [--dry-run]
  python3 bin/lib/writ_install.py claude-md  --target PATH [--template PATH]
                                             [--skill-dir DIR] [--dry-run]
  python3 bin/lib/writ_install.py hooks      --target PATH [--skill-dir DIR] [--dry-run]
  python3 bin/lib/writ_install.py commands   --target DIR  [--skill-dir DIR] [--dry-run]
  python3 bin/lib/writ_install.py all        --settings-target PATH --claude-md-target PATH
                                             --commands-target DIR [--skill-dir DIR] [--dry-run]
  python3 bin/lib/writ_install.py http-get   URL [--fail] [--timeout SECONDS]
  python3 bin/lib/writ_install.py http-post  URL BODY [--fail] [--timeout SECONDS]

Exit codes (unchanged from patch-global-config.sh):
  0  patched / already up to date / dry-run success
  1  missing template, missing settings target (hooks), or the plugin-install refusal
  2  write failure (including a target that is not readable JSON)
"""

import argparse
import difflib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
from datetime import datetime, timezone

EXIT_OK = 0
EXIT_PRECONDITION = 1
EXIT_WRITE_FAILURE = 2

# curl's own exit codes for the two failures the shell arms distinguish, so a caller
# reading $? sees the same number whichever backend ran.
CURL_HTTP_ERROR = 22       # curl(1) -sf against a >= 400 response
CURL_CONNECT_ERROR = 7     # could not connect / resolve / timed out

DEFAULT_HTTP_TIMEOUT = 10.0

# The install-dir-relative statusLine hook. writ-statusline.sh self-resolves its own
# directory from $0, so an absolute-path invocation works in any context.
STATUSLINE_REL = "hooks/scripts/writ-statusline.sh"

# Cross-mode allow rules. The wildcards match both a plugin install
# (${CLAUDE_PLUGIN_ROOT}/...) and a dev/repo run path with ONE entry each.
BASE_ALLOW = (
    "Bash(python3 *writ-session.py *)",
    "Bash(bash *writ/bin/check-gates.sh*)",
    "Bash(bash *writ/bin/verify-files.sh*)",
    "Bash(bash *writ/bin/scan-deps.sh*)",
    "Bash(bash *writ/bin/run-analysis.sh*)",
    "Bash(bash *writ/bin/validate-handoff.sh*)",
    "Bash(*writ/bin/writ query *)",
    "Bash(*writ/bin/writ status*)",
    "Bash(*writ/bin/writ role-prompt *)",
    "Bash(*writ/bin/writ validate*)",
    "Bash(*writ/bin/writ analyze-friction*)",
    "Bash(*writ/bin/writ audit-session*)",
    "Bash(bash *writ/scripts/bootstrap.sh*)",
    "Bash(bash *writ/scripts/bootstrap-plugin.sh*)",
    "Bash(bash *writ/scripts/ensure-server.sh*)",
    "Bash(bash *writ/scripts/install-user-commands.sh*)",
    "Bash(bash *writ/scripts/stop-server.sh*)",
)

# Superseded location-tied entries earlier versions installed. Removed on every run so a
# moved install does not accumulate dead allow rules.
LEGACY_ALLOW = (
    "Bash(*/.claude/skills/writ/*)",
    "Bash(*/analysis/Writ/*)",
)

DENY = (
    "AskUserQuestion",
    # Gate-approval boundary: the agent must never write a .claude/gates/*.approved file
    # directly -- that would self-approve a human-oversight gate (the north star). Scoped
    # to the gates dir so an unrelated path (a test file named *gates_approved*) is not
    # collaterally blocked.
    "Bash(touch */.claude/gates/*)",
    "Bash(*>.claude/gates/*)",
    "Bash(*/.claude/gates/*approve*)",
)

# `Edit(/abs/dir/**)` / `Bash(/abs/dir/*)` style entries, from which the pruner reads the
# directory. Mirrors the sed expression the bash pruner used.
_DIR_ENTRY_RE = re.compile(r"^(?:Edit|Write|Bash)\((/[^*?]+)/\*\*?\)$")


# --------------------------------------------------------------------------- #
# Small shared helpers
# --------------------------------------------------------------------------- #


def _skill_dir(arg):
    """Resolve the install root: --skill-dir when given, else two levels up from this
    file (bin/lib/writ_install.py -> bin/lib -> bin -> <skill>), exactly as
    patch-global-config.sh's SKILL_DIR derivation does."""
    if arg:
        return os.path.abspath(arg)
    return os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))


def _timestamp():
    """UTC stamp for backups -- byte-compatible with `date -u '+%Y%m%d%H%M%S'`."""
    return datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")


def _dump(doc):
    """Serialize like `jq .` did: 2-space indent, UTF-8 as-is, trailing newline."""
    return json.dumps(doc, indent=2, ensure_ascii=False) + "\n"


def _read_text(path):
    try:
        with open(path, encoding="utf-8") as handle:
            return handle.read()
    except OSError:
        return None


def _print_diff(label, target, current, new_text):
    print("[%s] [dry-run] would write %s. Diff:" % (label, target))
    diff = difflib.unified_diff(
        current.splitlines(keepends=True),
        new_text.splitlines(keepends=True),
        fromfile=str(target),
        tofile="%s (proposed)" % target,
    )
    sys.stdout.writelines(line if line.endswith("\n") else line + "\n" for line in diff)


def _write(label, target, new_text, backup):
    """Write new_text to target, backing the previous content up first. Returns an exit code."""
    try:
        os.makedirs(os.path.dirname(os.path.abspath(target)) or ".", exist_ok=True)
    except OSError as exc:
        print("[%s] ERROR: cannot create %s: %s"
              % (label, os.path.dirname(target), exc), file=sys.stderr)
        return EXIT_WRITE_FAILURE
    backup_path = None
    if backup and os.path.isfile(target):
        backup_path = "%s.bak.%s" % (target, _timestamp())
        try:
            shutil.copy2(target, backup_path)
        except OSError as exc:
            print("[%s] ERROR: failed to create backup at %s: %s" % (label, backup_path, exc),
                  file=sys.stderr)
            return EXIT_WRITE_FAILURE
    # Atomic replace: an interrupted plain open("w") truncates first and can leave
    # ~/.claude/settings.json empty, the exact data-loss class this module replaces.
    try:
        fd, tmp = tempfile.mkstemp(
            dir=os.path.dirname(os.path.abspath(target)) or ".",
            prefix=os.path.basename(target) + ".", suffix=".tmp",
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(new_text)
            os.replace(tmp, target)
        except OSError:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
    except OSError as exc:
        print("[%s] ERROR: failed to write %s: %s" % (label, target, exc), file=sys.stderr)
        return EXIT_WRITE_FAILURE
    if backup_path:
        print("[%s] Backup: %s" % (label, backup_path))
    return EXIT_OK


def _load_settings(target):
    """(doc, existed, error_code). A target that exists but is not readable JSON is an
    error, never an empty document: the jq pipeline it replaces would have overwritten the
    file with nothing."""
    if not os.path.isfile(target):
        return {}, False, None
    raw = _read_text(target)
    if raw is None:
        print("[settings] ERROR: cannot read %s" % target, file=sys.stderr)
        return None, True, EXIT_WRITE_FAILURE
    if not raw.strip():
        return {}, True, None
    try:
        doc = json.loads(raw)
    except ValueError as exc:
        print("[settings] ERROR: %s is not valid JSON (%s); refusing to overwrite it."
              % (target, exc), file=sys.stderr)
        return None, True, EXIT_WRITE_FAILURE
    if not isinstance(doc, dict):
        print("[settings] ERROR: %s does not contain a JSON object; refusing to overwrite it."
              % target, file=sys.stderr)
        return None, True, EXIT_WRITE_FAILURE
    return doc, True, None


# --------------------------------------------------------------------------- #
# settings.json: permissions + statusLine
# --------------------------------------------------------------------------- #


def _stale_entries(allow, skill_dir):
    """Entries to drop: the legacy wildcards, plus allow rules pointing at a writ-named
    directory that is NOT this install and no longer exists (residue of a moved checkout).

    Two guards keep an unrelated user entry: the basename must be writ/Writ, and the
    directory must be absent. A live second checkout is kept.
    """
    drop = list(LEGACY_ALLOW)
    for entry in allow:
        if not isinstance(entry, str):
            continue
        match = _DIR_ENTRY_RE.match(entry)
        if not match:
            continue
        directory = match.group(1)
        if directory == skill_dir:
            continue
        if os.path.basename(directory) not in ("writ", "Writ"):
            continue
        if not os.path.isdir(directory):
            drop.append(entry)
    return drop


def _append_new(existing, incoming):
    """Append only entries not already present, preserving existing order (jq append_new)."""
    out = list(existing)
    for item in incoming:
        if item not in out:
            out.append(item)
    return out


def cmd_settings(args):
    skill_dir = _skill_dir(args.skill_dir)
    target = os.path.abspath(args.target)
    statusline_cmd = "bash %s/%s" % (skill_dir, STATUSLINE_REL)

    doc, existed, error = _load_settings(target)
    if error is not None:
        return error

    allow_entries = list(BASE_ALLOW) + [
        # Self-edit + self-run entries are DERIVED from the resolved install dir, so a Writ
        # checked out anywhere (/opt/writ, ~/src/writ, a second worktree) gets the right
        # paths. They were once hardcoded to one developer's home, which meant every other
        # install silently ran without them and prompted on each self-edit.
        "Edit(%s/**)" % skill_dir,
        "Bash(%s/*)" % skill_dir,
    ]

    permissions = doc.get("permissions")
    if not isinstance(permissions, dict):
        permissions = {}
    current_allow = permissions.get("allow")
    current_allow = list(current_allow) if isinstance(current_allow, list) else []
    current_deny = permissions.get("deny")
    current_deny = list(current_deny) if isinstance(current_deny, list) else []

    drop = _stale_entries(current_allow, skill_dir)
    kept_allow = [entry for entry in current_allow if entry not in drop]

    permissions["allow"] = _append_new(kept_allow, allow_entries)
    permissions["deny"] = _append_new(current_deny, list(DENY))
    doc["permissions"] = permissions

    # Inform (do not act) when a non-Writ statusLine is already configured: the merge
    # leaves it untouched, so point the user at the opt-in command.
    existing_sl = doc.get("statusLine")
    existing_cmd = existing_sl.get("command", "") if isinstance(existing_sl, dict) else ""
    if existing_sl is None:
        doc["statusLine"] = {"type": "command", "command": statusline_cmd}
    elif "writ-statusline.sh" in str(existing_cmd):
        doc["statusLine"] = {"type": "command", "command": statusline_cmd}
    else:
        print("[settings] An existing (non-Writ) statusLine is configured; leaving it untouched.")
        print("[settings] To use the Writ context meter, set statusLine.command to: %s"
              % statusline_cmd)

    new_text = _dump(doc)
    current = _read_text(target) if existed else ""
    if current is None:
        current = ""

    if existed and current == new_text:
        print("[settings] No changes needed: %s already contains the Writ permission "
              "+ statusLine entries." % target)
        return EXIT_OK

    if args.dry_run:
        _print_diff("settings", target, current, new_text)
        return EXIT_OK

    rc = _write("settings", target, new_text, backup=existed)
    if rc != EXIT_OK:
        return rc
    print("[settings] %s %s" % ("Patched" if existed else "Created", target))
    return EXIT_OK


# --------------------------------------------------------------------------- #
# CLAUDE.md render
# --------------------------------------------------------------------------- #

# `$HOME` and `${HOME}` only -- the exact substitution surface of the single-variable
# gettext call this replaces. Any other $VAR in the template is left alone, as it was.
_HOME_RE = re.compile(r"\$(?:HOME\b|\{HOME\})")


def _render_home(text):
    home = os.environ.get("HOME") or os.path.expanduser("~")
    return _HOME_RE.sub(lambda _m: home, text)


def cmd_claude_md(args):
    skill_dir = _skill_dir(args.skill_dir)
    template = os.path.abspath(args.template) if args.template \
        else os.path.join(skill_dir, "templates", "CLAUDE.md")
    target = os.path.abspath(args.target)

    if not os.path.isfile(template):
        print("[CLAUDE.md] ERROR: template missing: %s" % template, file=sys.stderr)
        return EXIT_PRECONDITION

    raw = _read_text(template)
    if raw is None:
        print("[CLAUDE.md] ERROR: cannot read template: %s" % template, file=sys.stderr)
        return EXIT_PRECONDITION
    rendered = _render_home(raw)

    existed = os.path.isfile(target)
    current = _read_text(target) if existed else None
    if existed and current == rendered:
        print("[CLAUDE.md] No changes needed: %s already matches the Writ template." % target)
        return EXIT_OK

    if args.dry_run:
        if existed:
            _print_diff("CLAUDE.md", target, current or "", rendered)
        else:
            print("[CLAUDE.md] [dry-run] would create %s from template (%d lines)."
                  % (target, len(rendered.splitlines())))
        return EXIT_OK

    rc = _write("CLAUDE.md", target, rendered, backup=existed)
    if rc != EXIT_OK:
        return rc
    print("[CLAUDE.md] %s %s" % ("Replaced" if existed else "Created", target))
    return EXIT_OK


# --------------------------------------------------------------------------- #
# hooks: render templates/settings.json and merge it into the target
#
# ONLY for an install nothing auto-discovers. Hooks are already global for a normal
# install: ~/.claude/skills/writ loads as the user-scope plugin writ@skills-dir, so its 12
# hooks fire in every project with no settings.json entry at all. Writ at any other path,
# without a marketplace install, is discovered by nothing -- hooks/hooks.json is never read
# and NO hooks load, so there is no gate, no rule injection, no enforcement.
#
# REFUSES when a loaded plugin resolves to this install. Two registration surfaces would
# very likely fire all 12 events twice: doubled rule injection, doubled gate evaluation,
# duplicated telemetry, and a single-use gate token one path could consume before the other
# reads it. That double-fire is not empirically proven, so this fails safe.
# --------------------------------------------------------------------------- #


def _loaded_plugin_paths():
    """Install paths of currently loaded plugins. WRIT_PLUGIN_LIST_CMD is a test injection
    seam (same pattern as WRIT_HEALTH_CMD / WRIT_SERVE_CMD in writ-server-lib.sh); it is a
    shell snippet, run through `bash -c` exactly as the bash implementation's `eval` did."""
    injected = os.environ.get("WRIT_PLUGIN_LIST_CMD", "")
    raw = ""
    try:
        if injected:
            raw = subprocess.run(["bash", "-c", injected], capture_output=True, text=True,
                                 timeout=10).stdout
        elif shutil.which("claude"):
            raw = subprocess.run(["claude", "plugin", "list", "--json"],
                                 capture_output=True, text=True, timeout=15).stdout
    except (OSError, subprocess.SubprocessError):
        return []
    try:
        listing = json.loads(raw or "[]")
    except ValueError:
        return []
    if not isinstance(listing, list):
        return []
    paths = []
    for item in listing:
        if not isinstance(item, dict) or item.get("enabled") is False:
            continue
        path = item.get("installPath")
        if path:
            paths.append(str(path))
    return paths


def _merge_event(existing, incoming):
    """jq merge_event: append only groups whose commands are all unregistered."""
    existing = list(existing or [])
    have = [hook.get("command") for group in existing for hook in (group.get("hooks") or [])]
    out = list(existing)
    for group in incoming or []:
        commands = [hook.get("command") for hook in (group.get("hooks") or [])]
        if all(command not in have for command in commands):
            out.append(group)
    return out


def cmd_hooks(args):
    skill_dir = _skill_dir(args.skill_dir)
    target = os.path.abspath(args.target)
    template = os.path.join(skill_dir, "templates", "settings.json")

    if not os.path.isfile(template):
        print("[hooks] ERROR: template not found: %s" % template, file=sys.stderr)
        print("[hooks] Generate it: python3 scripts/render-settings-template.py",
              file=sys.stderr)
        return EXIT_PRECONDITION
    if not os.path.isfile(target):
        print("[hooks] ERROR: target settings file not found: %s" % target, file=sys.stderr)
        return EXIT_PRECONDITION

    loaded = _loaded_plugin_paths()
    resolved = {os.path.abspath(path) for path in loaded} | set(loaded)
    if skill_dir in resolved:
        print("[hooks] REFUSING: this install (%s) is already loaded as a Claude Code plugin,"
              % skill_dir, file=sys.stderr)
        print("[hooks] so its 12 hooks are registered by the plugin loader and fire in every "
              "project.", file=sys.stderr)
        print("[hooks] Seeding settings.json too would register them a SECOND time.",
              file=sys.stderr)
        print("[hooks] The --hooks step is only for an install that no marketplace or skills dir",
              file=sys.stderr)
        print("[hooks] discovers. Nothing was written.", file=sys.stderr)
        return EXIT_PRECONDITION

    # ${WRIT_DIR} is the only variable in the generated template; render it to this
    # install's path (both the braced and bare forms, as the gettext call it replaces did).
    raw_template = _read_text(template)
    if raw_template is None:
        print("[hooks] ERROR: cannot read template: %s" % template, file=sys.stderr)
        return EXIT_PRECONDITION
    rendered = re.sub(r"\$(?:WRIT_DIR\b|\{WRIT_DIR\})", lambda _m: skill_dir, raw_template)
    try:
        template_doc = json.loads(rendered)
    except ValueError as exc:
        print("[hooks] ERROR: rendered template is not valid JSON: %s" % exc, file=sys.stderr)
        return EXIT_PRECONDITION
    template_hooks = template_doc.get("hooks") or {}

    doc, _existed, error = _load_settings(target)
    if error is not None:
        return error

    merged = doc.get("hooks")
    merged = dict(merged) if isinstance(merged, dict) else {}
    for event, groups in template_hooks.items():
        merged[event] = _merge_event(merged.get(event), groups)
    doc["hooks"] = merged

    new_text = _dump(doc)
    current = _read_text(target) or ""
    if current == new_text:
        print("[hooks] Already registered (%d events); no change." % len(template_hooks))
        return EXIT_OK
    if args.dry_run:
        print("[hooks] [dry-run] would merge %d hook events into %s."
              % (len(template_hooks), target))
        _print_diff("hooks", target, current, new_text)
        return EXIT_OK

    rc = _write("hooks", target, new_text, backup=True)
    if rc != EXIT_OK:
        return rc
    print("[hooks] Registered %d hook events in %s" % (len(template_hooks), target))
    return EXIT_OK


# --------------------------------------------------------------------------- #
# commands: copy templates/commands/*.md into the user commands dir
# --------------------------------------------------------------------------- #


def cmd_commands(args):
    skill_dir = _skill_dir(args.skill_dir)
    source = os.path.join(skill_dir, "templates", "commands")
    target = os.path.abspath(args.target)

    if not os.path.isdir(source):
        print("error: source directory missing: %s" % source, file=sys.stderr)
        return EXIT_PRECONDITION

    names = sorted(name for name in os.listdir(source) if name.endswith(".md"))

    if args.dry_run:
        for name in names:
            print("[dry-run] would install: %s" % os.path.join(target, name))
        print()
        print("[dry-run] %d slash command(s) would be installed to %s" % (len(names), target))
        return EXIT_OK

    try:
        os.makedirs(target, exist_ok=True)
    except OSError as exc:
        print("error: cannot create %s: %s" % (target, exc), file=sys.stderr)
        return EXIT_WRITE_FAILURE

    count = 0
    for name in names:
        destination = os.path.join(target, name)
        try:
            shutil.copyfile(os.path.join(source, name), destination)
        except OSError as exc:
            print("error: failed to install %s: %s" % (destination, exc), file=sys.stderr)
            return EXIT_WRITE_FAILURE
        print("installed: %s" % destination)
        count += 1

    if count == 0:
        print("warning: no .md files found in %s" % source, file=sys.stderr)
        return EXIT_OK

    print()
    print("%d slash command(s) installed to %s" % (count, target))
    print("Restart Claude Code (or open a new session) to pick up the changes.")
    return EXIT_OK


# --------------------------------------------------------------------------- #
# all: the three install-time writes in one call
# --------------------------------------------------------------------------- #


def cmd_all(args):
    overall = EXIT_OK
    for func, target_attr in ((cmd_settings, "settings_target"),
                              (cmd_claude_md, "claude_md_target"),
                              (cmd_commands, "commands_target")):
        sub = argparse.Namespace(
            target=getattr(args, target_attr),
            template=None,
            skill_dir=args.skill_dir,
            dry_run=args.dry_run,
        )
        rc = func(sub)
        if rc != EXIT_OK:
            overall = rc
    return overall


# --------------------------------------------------------------------------- #
# http-get / http-post: the stdlib substitute for curl
#
# Reproduces both shell semantics exactly, because callers depend on the difference:
#   default   = `curl -s`  -> print the body for ANY status, including >= 400 (the gate
#                             outcome classifier reads the error body)
#   --fail    = `curl -sf` -> print nothing and exit non-zero on >= 400
# A connection failure prints nothing and exits non-zero in both modes.
# --------------------------------------------------------------------------- #


def _http(url, body, fail, timeout):
    if body is None:
        request = urllib.request.Request(url, method="GET")
    else:
        request = urllib.request.Request(
            url, data=body.encode("utf-8"), method="POST",
            headers={"Content-Type": "application/json"},
        )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            data = response.read()
    except urllib.error.HTTPError as exc:
        if fail:
            return CURL_HTTP_ERROR
        try:
            data = exc.read()
        except Exception:
            data = b""
    except Exception:
        # URLError, socket timeout, a malformed URL: all of curl's "could not connect".
        return CURL_CONNECT_ERROR
    sys.stdout.buffer.write(data)
    sys.stdout.buffer.flush()
    return EXIT_OK


def cmd_http_get(args):
    return _http(args.url, None, args.fail, args.timeout)


def cmd_http_post(args):
    return _http(args.url, args.body, args.fail, args.timeout)


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #


def build_parser():
    parser = argparse.ArgumentParser(
        prog="writ_install.py",
        description="Writ install-time configuration writer (stdlib only).",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    def add_common(sub):
        sub.add_argument("--skill-dir", default=None,
                         help="install root (default: two levels up from this file)")
        sub.add_argument("--dry-run", action="store_true",
                         help="print what would change; write nothing")

    settings = subparsers.add_parser("settings", help="merge permissions + statusLine")
    settings.add_argument("--target", required=True)
    add_common(settings)
    settings.set_defaults(func=cmd_settings)

    claude_md = subparsers.add_parser("claude-md", help="render templates/CLAUDE.md")
    claude_md.add_argument("--target", required=True)
    claude_md.add_argument("--template", default=None,
                           help="template path (default: <skill-dir>/templates/CLAUDE.md)")
    add_common(claude_md)
    claude_md.set_defaults(func=cmd_claude_md)

    hooks = subparsers.add_parser("hooks", help="merge the generated hook registrations")
    hooks.add_argument("--target", required=True)
    add_common(hooks)
    hooks.set_defaults(func=cmd_hooks)

    commands = subparsers.add_parser("commands", help="install the slash commands")
    commands.add_argument("--target", required=True)
    add_common(commands)
    commands.set_defaults(func=cmd_commands)

    every = subparsers.add_parser("all", help="settings + claude-md + commands")
    every.add_argument("--settings-target", required=True)
    every.add_argument("--claude-md-target", required=True)
    every.add_argument("--commands-target", required=True)
    add_common(every)
    every.set_defaults(func=cmd_all)

    http_get = subparsers.add_parser("http-get", help="GET a URL (curl(1) -s substitute)")
    http_get.add_argument("url")
    http_get.add_argument("--fail", action="store_true", help="curl(1) -sf semantics")
    http_get.add_argument("--timeout", type=float, default=DEFAULT_HTTP_TIMEOUT)
    http_get.set_defaults(func=cmd_http_get)

    http_post = subparsers.add_parser("http-post", help="POST JSON to a URL")
    http_post.add_argument("url")
    http_post.add_argument("body")
    http_post.add_argument("--fail", action="store_true", help="curl(1) -sf semantics")
    http_post.add_argument("--timeout", type=float, default=DEFAULT_HTTP_TIMEOUT)
    http_post.set_defaults(func=cmd_http_post)

    return parser


def main(argv):
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
