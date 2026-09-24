"""Where Writ keeps state that has to outlive any one copy of Writ.

Session caches (mode, approved gates, the manual-testing grant), the per-session scratch files
the pending-test and lint hooks leave, and the typed log streams all used to live under the
install directory (`<install>/var/...`), derived from each module's own `__file__`. A plugin
install path carries the version (`~/.claude/plugins/cache/writ/writ/1.8.0/`), so every upgrade
started from an empty state tree and orphaned the live sessions' modes and approvals, and every
copy of Writ on one machine (the plugin cache, a marketplace clone, a dev checkout) kept its own.

The root is the XDG state directory: `$XDG_STATE_HOME/writ`, else `~/.local/state/writ`.
XDG_STATE_HOME is honoured only when absolute, because the spec says a relative value is to be
ignored. NOT a temp directory: `/usr/lib/tmpfiles.d/tmp.conf` declares `D /tmp`, which empties it
at boot, and that wipe once destroyed every session cache (see writ/session/cache.py).

ONE definition per language. bin/lib/common.sh computes the same root in bash
(`_WRIT_STATE_ROOT`) with parameter expansion only, because common.sh is sourced by every hook
and a subshell there is paid on every tool call; tests/test_state_root.py resolves both under one
environment and compares them. os only, no pathlib: this sits under writ/session/cache.py on the
per-hook hot path, and pathlib costs about 5.6ms per spawn there.
"""

import os


def state_root() -> str:
    """`$XDG_STATE_HOME/writ` when that variable is absolute, else `~/.local/state/writ`."""
    base = os.environ.get("XDG_STATE_HOME", "")
    if not os.path.isabs(base):
        base = os.path.join(os.path.expanduser("~"), ".local", "state")
    return os.path.join(base, "writ")


def session_dir() -> str:
    """The session-cache directory: WRIT_CACHE_DIR when set and non-empty, else
    `<state_root>/session`.

    An EMPTY WRIT_CACHE_DIR counts as unset, which is what the bash resolver has always done
    (`[ -n "${WRIT_CACHE_DIR:-}" ]`). The package used to hand back the empty string, which
    resolved against the process cwd, so the two sides disagreed on exactly that input.
    """
    return os.environ.get("WRIT_CACHE_DIR") or os.path.join(state_root(), "session")


def default_log_root() -> str:
    """The typed log streams' default root, `<state_root>/logs`. WRIT_LOG_ROOT overrides it in
    writ/shared/logging.py::log_root."""
    return os.path.join(state_root(), "logs")
