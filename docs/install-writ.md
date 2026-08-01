# Installing Writ

Writ is distributed as a Claude Code plugin. The skill lives at
`~/.claude/skills/writ/` and Claude Code auto-loads it as the plugin
`writ@skills-dir`, so its hooks and slash commands work from any project
directory. Hook registrations come from one place: `hooks/hooks.json`
(the plugin manifest). Editing `hooks/hooks.json` is all that is needed
for a hook change.

For a standard install there is no `~/.claude/settings.json` hook seeding
step: the plugin is user-scoped, so its hooks already fire in every project.
The one exception is an install at a path Claude Code does not discover, which
is covered in "Installing outside `~/.claude/skills`" below.

## 1. Bootstrap runtime prerequisites

One-time setup of the Python venv, Neo4j, the ONNX embedding model, and
the rule corpus:

```bash
bash ~/.claude/skills/writ/scripts/bootstrap-plugin.sh
```

(When working inside the repo as a developer, `scripts/bootstrap.sh` is
the equivalent that uses the repo-local `.venv`.)

## 2. Patch global config (permissions, statusLine, CLAUDE.md)

A plugin manifest cannot ship the permission allowlist or the main
statusLine, and the plugin lifecycle does not write `~/.claude/CLAUDE.md`.
This idempotent, non-destructive patch delivers all three:

```bash
bash ~/.claude/skills/writ/scripts/patch-global-config.sh
```

It merges the Writ allow/deny entries into `~/.claude/settings.json`
(preserving your ordering and any non-Writ entries), sets the Writ
statusLine (leaving a foreign statusLine untouched), and renders
`templates/CLAUDE.md` into `~/.claude/CLAUDE.md` (backing up any
pre-existing file). By default it never touches the `hooks` block: the
plugin owns hooks. See the next section for the one case where it should.

## 2b. Installing outside `~/.claude/skills` (hook seeding)

Skip this unless Writ lives somewhere Claude Code does not discover.

Hooks load because `~/.claude/skills/writ` is auto-discovered as the
user-scope plugin `writ@skills-dir`. Confirm your install is discovered:

```bash
claude plugin list --json     # look for an entry whose installPath is your install
claude plugin details writ    # should report Hooks (12)
```

If Writ is at some other path and was not installed through a marketplace,
nothing discovers it: `hooks/hooks.json` is never read and **no hooks load
at all**, so there is no gate, no rule injection, and no enforcement. In
that case register the hooks globally instead:

```bash
bash /path/to/writ/scripts/patch-global-config.sh --hooks
```

This merges the 12 hook events from `templates/settings.json` into
`~/.claude/settings.json`, with the install path filled in. It is
idempotent, backs up the previous file, and preserves your own hooks and
unrelated settings.

**Do not run it when Writ is plugin-loaded.** Both surfaces would register
the same 12 events, so every hook would fire twice: doubled rule injection,
doubled gate evaluation, and duplicated telemetry. The step detects this and
refuses, and `writ doctor` reports the condition (`duplicate-hook-registration`)
if it arises another way, such as seeding an install and later moving it
under `~/.claude/skills`.

`templates/settings.json` is generated from `hooks/hooks.json` by
`scripts/render-settings-template.py`; a test keeps the two in sync, so
`hooks/hooks.json` remains the only file to edit for a hook change.

## 3. Install user-level slash commands

Claude Code discovers slash commands from `~/.claude/commands/` (user
level). The skill's own `.claude/commands/` is only discovered when the
session cwd is the skill itself, so `/writ-approve` would not work from
your normal project directories without this step:

```bash
bash ~/.claude/skills/writ/scripts/install-user-commands.sh
```

Idempotent: it copies every `.md` from `templates/commands/` to
`~/.claude/commands/`. Re-run after pulling updates that add a command.

After running, restart Claude Code (or open a new session) to pick up
the commands.

## Verify install

```bash
test -f ~/.claude/commands/writ-approve.md && echo "/writ-approve installed"
curl -sf http://localhost:8765/health >/dev/null && echo "writ server healthy"
```

Both should print confirmation lines. If the server line does not, the
SessionStart hook starts the daemon on the next Claude session; check
`~/.cache/writ/server.log`.

## Update path

After `git pull` in `~/.claude/skills/writ/`, the plugin picks up hook
changes from `hooks/hooks.json` automatically on the next session. Re-run
the two idempotent patches when permissions, CLAUDE.md, or commands changed:

```bash
bash ~/.claude/skills/writ/scripts/patch-global-config.sh
bash ~/.claude/skills/writ/scripts/install-user-commands.sh
```

## Restart the writ daemon (when server.py changes)

When `writ/server.py` changes (new endpoints, modified routes), the
running uvicorn process keeps serving the old module until restarted.
PSR-005 caught a `/dashboard` 404 traced to exactly this: the route was
wired in code but the daemon predated the change.

```bash
bash ~/.claude/skills/writ/scripts/stop-server.sh
bash ~/.claude/skills/writ/scripts/ensure-server.sh
```

Verify with `curl -sf http://localhost:8765/health`. Restart is only
required when `server.py` (or a FastAPI route module it imports) changes;
routine ingest, query, or hook updates do not need it.

## Known limitations

- `patch-global-config.sh` merges into `~/.claude/settings.json`
  non-destructively, but if you keep a foreign statusLine it is left
  as-is (Writ's statusLine is not installed in that case).
- The user-commands installer overwrites identically-named files in
  `~/.claude/commands/`. A non-Writ `writ-approve.md` there gets replaced.
- Restarting Claude Code is required after config changes for the
  settings/commands to take effect in a running session.
