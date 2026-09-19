#!/usr/bin/env bash
# writ-statusline.sh
#
# POL-5a: Claude Code statusLine command. Replaces the per-tool-call
# writ-context-watcher.sh. Runs OUTSIDE the tool-call loop (never blocks the
# agent), debounced by the harness, after each assistant message + /compact.
#
# Reads the statusLine stdin envelope, takes the harness-native
# context_window.used_percentage (computed against the REAL window, unlike the
# old 200k-hardcoded estimate), and:
#   1. renders a context meter + a user-facing "run /compact" note at the
#      50% / 75% bands to stdout (the status bar) -- never stderr.
#   2. best-effort POSTs context_percent to /session/{id}/context-percent so
#      cmd_should_skip keeps its context-pressure gate (re-sourced; the server
#      does a serialized read-modify-write, so no lost-update clobber).
#
# Single Python process: program via heredoc, envelope via STATUSLINE_STDIN so
# the interpreter's stdin stays free. Server-down / malformed input degrade to
# a clean render with no note and no error. Always exits 0.

set -euo pipefail

HOOK_DIR="$(cd "$(dirname "$0")" && pwd)"
WRIT_DIR="$(cd "$HOOK_DIR/../.." && pwd)"
source "$WRIT_DIR/bin/lib/common.sh"

STATUSLINE_STDIN="$(cat || true)"

# WRIT_DIR joins this env chain EXPLICITLY. It is a shell variable (line 24), not an
# exported one, so the python block below read it as unset and fell back to ".", the
# PROJECT directory for every project but this repo. Importing the shared daemon
# client by path would then fail and the bare `except` would swallow it, silently
# dropping the context signal cmd_should_skip's pressure gate reads.
#
# THE COMMENT SITS ABOVE THE CHAIN, not inside it. Placing it between two
# continuation lines detached the assignments from python3, so STATUSLINE_STDIN never
# arrived and the bar rendered "Writ ctx --" with no POST at all. `bash -n` accepts
# that happily: it is valid syntax and the wrong program.
STATUSLINE_STDIN="$STATUSLINE_STDIN" \
WRIT_SESSION_BASE="$WRIT_SESSION_BASE" \
WRIT_DIR="$WRIT_DIR" \
python3 - <<'PYEOF' || true
import os
import sys

raw = os.environ.get("STATUSLINE_STDIN", "")


def _render(text):
    sys.stdout.write(text)
    sys.stdout.flush()


try:
    import json

    try:
        data = json.loads(raw) if raw.strip() else {}
    except Exception:
        data = {}
    if not isinstance(data, dict):
        data = {}

    cw = data.get("context_window") or {}
    used = cw.get("used_percentage") if isinstance(cw, dict) else None
    sid = (data.get("session_id") or "").strip()

    pct = None
    if isinstance(used, bool):
        pct = None
    elif isinstance(used, (int, float)):
        pct = max(0, min(100, int(used)))

    # 1. Render the bar (stdout). The band note labels which threshold was
    #    crossed so the user knows the severity at a glance.
    if pct is None:
        _render("Writ ctx --")
    elif pct >= 75:
        _render(f"Writ ctx {pct}% | >=75% run /compact now")
    elif pct >= 50:
        _render(f"Writ ctx {pct}% | >=50% run /compact at a pause")
    else:
        _render(f"Writ ctx {pct}%")

    # 2. Best-effort re-source context_percent for should-skip. Render already
    #    happened; a slow/down server never blocks the bar.
    if sid and pct is not None:
        # Through the shared client (bin/lib/writ_daemon_client.py), which prefers the
        # daemon's unix socket. This block was the transport census's dominant source:
        # 65 of the first 71 state-touching TCP writes, invisible to E2a's curl-side
        # transport because it is python inside a shell script.
        try:
            sys.path.insert(0, os.path.join(os.environ.get("WRIT_DIR", "."), "bin", "lib"))
            import writ_daemon_client

            writ_daemon_client.post_json(
                f"/session/{sid}/context-percent", {"context_percent": pct}
            )
        except Exception:
            pass
except Exception:
    # Never crash the status bar.
    pass
PYEOF

exit 0
