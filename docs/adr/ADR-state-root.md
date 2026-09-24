# ADR: Writ's durable state lives in the XDG state root, never inside the install

Status: accepted
Date: 2026-09-24
Plan: `.claude/plans/6bd39d6d-6668-46eb-9a23-b2eb8edede83/plan.md`
Touches: `writ/shared/state_root.py` (new), `writ/session/cache.py`, `writ/shared/logging.py`,
`bin/lib/common.sh`, `bin/lib/manual_test_grant.py`, `bin/lib/pointer_session.py`,
`bin/lib/writ-flush-events.py`, `bin/lib/writ_state_migrate.py` (new),
`hooks/scripts/session-start-bootstrap.sh`, `hooks/scripts/writ-mark-pending-test.sh`,
`hooks/scripts/writ-run-pending-tests.sh`, `hooks/scripts/validate-file.sh`,
`hooks/scripts/writ-state-write-gate.sh`, `hooks/scripts/writ-bash-write-gate.sh`,
`scripts/lib/writ-server-lib.sh`, `scripts/install-server-service.sh`

## Context

Every piece of durable Writ state was located relative to the module that wrote it. The
session cache (`writ/session/cache.py`), the manual-testing grant, the commit hook's
session lookup, the event-buffer drain and the typed logs each walked up from their own
`__file__` to the install directory and appended `var/session` or `var/logs`. The bash side
did the same from `bin/lib/common.sh`, and three hooks put their scratch files in the
install's `cache/`.

A plugin install path carries the version: `~/.claude/plugins/cache/writ/writ/1.8.0/`. So
every upgrade began from an empty `var/session`, and every live session lost its declared
mode and its approved gates on the first turn after the upgrade. A machine with more than
one copy of Writ (the plugin cache, a marketplace clone, a dev checkout) kept one state tree
per copy, and which one a given hook read depended on which copy that hook shipped in.

The same investigation found a session that set work mode on two OTHER sessions' ids, read
from `/tmp/writ-current-session`, got `set: work` both times, and left its own mode null.
Nothing in the output could show the mistake. That is addressed by a warning in
`writ/session/cli_dispatch.py`, and it bears on this record because unifying state means
those foreign ids now have caches, so a check for "no cache exists" alone would go silent on
exactly that misuse.

## Decision

One root, `$XDG_STATE_HOME/writ` when that variable is absolute, else `~/.local/state/writ`.
A relative XDG_STATE_HOME is ignored, as the XDG spec requires. Logs are XDG "state" by the
spec's own definition, so they move too. Never a temp directory: systemd's tmpfiles.d
declares `D /tmp`, which empties it at boot, and that wipe once destroyed every session
cache.

The root has exactly one definition per language: `writ/shared/state_root.py` (stdlib `os`
only, resolved at call time) and `_WRIT_STATE_ROOT` in `bin/lib/common.sh` (a `case` and
parameter expansion, no subshell, because common.sh is sourced by every hook).
`tests/test_state_root.py` resolves both under one environment and compares them.

| Site | Before | After |
|---|---|---|
| `writ/session/cache.py` | `<install>/var/session` | `<root>/session` via `state_root.session_dir()` |
| `bin/lib/common.sh` `writ_session_cache_dir` | `<skill>/var/session` | `$_WRIT_STATE_ROOT/session` |
| `bin/lib/manual_test_grant.py` | own copy of `<install>/var/session` | `state_root.session_dir()` |
| `bin/lib/pointer_session.py` | `$WRIT_DIR/var/session`, else its own location | `state_root.session_dir()` |
| `bin/lib/writ-flush-events.py` | own copy of `<install>/var/session` | `state_root.session_dir()`, so the drain reads the directory the bash appender writes |
| pending-test markers and lint logs | `${WRIT_CACHE_DIR:-<install>/cache}` | `${WRIT_CACHE_DIR:-$_WRIT_STATE_ROOT/cache}`, a sibling of `session/` and outside the gated state dir |
| `writ/shared/logging.py` `log_root()` | `<install>/var/logs` | `<root>/logs` |
| standalone daemon log | `${WRIT_DIR:-~/.claude/skills/writ}/var/logs/server.log` | `<root>/logs/server.log`; the plugin branch keeps `$CLAUDE_PLUGIN_DATA` |
| state gates | `${WRIT_CACHE_DIR:-<install>/var/session}` | the new dir AND the legacy dir (see Gates) |

Precedence is unchanged: WRIT_CACHE_DIR still wins for the session dir and the scratch
files, WRIT_LOG_ROOT still wins for the logs. One semantic is tightened on purpose: an EMPTY
WRIT_CACHE_DIR now counts as unset on both sides. The package used to return the empty
string, which resolved against the process cwd, while bash already treated empty as unset.

A daemon started by a hook is pinned by the hook's export of `WRIT_CACHE_DIR`, so it agrees
with no change. A systemd daemon inherits the user manager's environment, which usually
lacks the login shell's XDG_STATE_HOME, so `scripts/install-server-service.sh` pins
WRIT_CACHE_DIR and WRIT_LOG_ROOT in the unit to the values resolved at install time.

## Migration

A copy at SessionStart, `bin/lib/writ_state_migrate.py`, run from
`session-start-bootstrap.sh` before its CLAUDE_PLUGIN_ROOT exit and before the venv probe.
Chosen over a lazy copy inside `_read_cache` because the bash readers (the direct mode read
and the auto-route classifier's jq read) never call `_read_cache`, so a lazy copy would
leave them blind on exactly the first turn that decides routing.

Sources: this install's `var/session`; when the install sits inside a plugin cache, every
sibling version directory's `var/session` (the new version's own directory is empty, the
live state is in the previous version's); and the two home locations earlier releases were
installed at, `~/.claude/plugins/marketplaces/writ/var/session` and
`~/.claude/skills/writ/var/session`. Siblings of a checkout that is NOT in a plugin cache
are never scanned, because a clone in a workspace directory sits beside other projects whose
`var/session` can hold PHP session files by the hundred thousand.

Guarantees: only regular `writ-session-*.json` files are carried (a grant expires in 30
minutes, an event buffer is telemetry), never a symlink; the newest mtime wins among copies
of one id; a destination that already exists is never touched, and the final step is
`os.link`, which fails atomically if a hook of the same session created the file
meanwhile; `copystat` keeps the mtime so rotation's newest-by-mtime logic stays meaningful.
A set WRIT_CACHE_DIR disables the migration entirely, which is also what keeps the test
suite from reading a real legacy directory.

Accepted case: a session that keeps running on the OLD version after its cache was carried
keeps writing the old directory, and those later writes are not carried again (no clobber).
That session resumes from its carried snapshot.

## Gates

ENF-GATE-STATE protects the new session dir, and it keeps protecting this install's legacy
`var/session`, because the migration reads from it: a cache forged there would be carried
in. The Write/Edit gate also denies any target whose basename starts `writ-session-` or
`writ-grant-`, in any directory, which covers sibling plugin version dirs without a path
list (`bin/lib/writ-session.py` does not match, because its name continues with `.py`). The
Bash gate adds the legacy dir and the literal `state/writ/session` (the `~` and `$HOME`
spellings the absolute path text cannot catch) to its name list, and passes the resolved
guard to its target extractor so both directories are checked by path. The irreversibility
arm gains the artifact `state/writ`, so `rm -rf ~/.local/state/writ/logs` is ENF-IRREVERSIBLE.

## Not done

- Log history is not migrated. Live typed streams can be hundreds of MB with gzipped
  archives per project. The new root starts empty; `WRIT_LOG_ROOT=<old>/var/logs` reads the
  old history, and the CHANGELOG gives the one `cp -a` that carries it by hand.
- Venv discovery for the `writ` subcommands other than `mode`. Only `writ mode` runs under
  the system python3 ahead of the venv check; the rest still expect `<install>/.venv`.
- The `/tmp` session pointer and gate token files. They are not install-derived, and both
  are deliberately ephemeral.

## Consequences

- A daemon started before the upgrade keeps reading the old directory until it is
  restarted. `_writ_start_locked` now reads `/health.cache_dir` when it finds a daemon up
  and prints a one-line warning naming both directories and the restart command. It does
  not restart the daemon, because systemd owns restarts.
- A systemd user must re-run `scripts/install-server-service.sh` once so the unit pins the
  new root.
- Doctor checks that read recent logs report thin data until the new streams fill, which is
  accurate.
