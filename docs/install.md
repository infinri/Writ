# Installing Writ

Writ runs the same way under three install paths; pick one:

- **A. Marketplace plugin** (recommended): `claude plugin install` from this repo's own marketplace.
- **B. Skills-directory checkout**: a clone at `~/.claude/skills/writ/`, auto-discovered by Claude Code as the user-scope plugin `writ@skills-dir`.
- **C. Anywhere else**: a clone at a path Claude Code does not discover; hooks must be seeded into `~/.claude/settings.json` (section 3).

In every path, hook registrations come from one place, `hooks/hooks.json` (41 registrations across 12 events over 37 scripts). Editing that file is all a hook change needs.

**Prerequisites (all paths):** Python 3.11+, Docker (Neo4j runs in a container), and `git` for the clone paths. That is the whole list. `jq` and `curl` are optional accelerators: every JSON read has a Python fallback and every HTTP call has a `urllib` fallback, so their absence changes speed, never behavior. Nothing needs `envsubst`/gettext.

## 1. Install (path A: marketplace plugin)

```bash
claude plugin marketplace add infinri/Writ
claude plugin install writ@writ
```

Now open Claude Code once in any project. Writ sees the un-bootstrapped install and prints one absolute command on its own line:

```
bash /path/it/prints/scripts/bootstrap-plugin.sh
```

Run that, restart Claude Code, and you are done: there is no install-path lookup step and no separate config patch. (If you would rather not open Claude Code first, `claude plugin list --json` carries the `installPath`; read it by eye. There is no `claude plugin path` subcommand.)

**Paths B and C (clone):**

```bash
git clone <writ-repo> ~/.claude/skills/writ     # path B
WRIT_DIR=~/.claude/skills/writ
bash "$WRIT_DIR/scripts/bootstrap.sh"
```

## 2. What the one bootstrap does

Both bootstraps are idempotent and safe to re-run. Each does the whole install:

| Step | `bootstrap-plugin.sh` (path A) | `bootstrap.sh` (paths B/C) |
| --- | --- | --- |
| Python venv | `${CLAUDE_PLUGIN_DATA:-~/.cache/writ}/.venv` (outside the plugin root, so an upgrade that rewrites the install path does not orphan it) | `$WRIT_DIR/.venv` |
| Package, ONNX model, Neo4j, corpus, daemon | yes | yes |
| `~/.claude/settings.json` + `~/.claude/CLAUDE.md` | yes | yes |
| `~/.claude/commands/` slash commands | yes | yes |
| `~/.local/bin/writ` and `~/.claude/{rules,agents}` symlinks | no (the plugin loader supplies the agents) | yes |

Each accepts `--preflight` to run only the prerequisite checks (tool presence and the Python version) and exit, which is a quick way to confirm a machine is ready before committing to a full install.

The global-config part exists because a plugin manifest cannot ship a permission allowlist, a statusLine, or `~/.claude/CLAUDE.md`. It merges the Writ allow/deny entries into `~/.claude/settings.json` (preserving your ordering and your non-Writ entries), sets the Writ statusLine (a foreign statusLine is left untouched), and renders `templates/CLAUDE.md` into `~/.claude/CLAUDE.md`, backing up anything it replaces. A missing `settings.json` is created. By default it never touches the `hooks` block: the plugin loader owns hooks.

To run either piece on its own, or to preview it:

```bash
bash "$WRIT_DIR/scripts/patch-global-config.sh"      # --dry-run to preview
bash "$WRIT_DIR/scripts/install-user-commands.sh"    # USER_COMMANDS_DIR=/path to redirect
```

## 3. Hook seeding (path C only)

If Writ lives at a path neither the plugin loader nor the skills directory discovers, `hooks/hooks.json` is never read and **no hooks load at all**: no gates, no rule injection, no enforcement. Confirm discovery first:

```bash
claude plugin list --json      # look for an entry whose installPath is your install
claude plugin details writ     # should report the hooks loaded
```

If nothing discovers it, seed the registrations globally:

```bash
bash "$WRIT_DIR/scripts/patch-global-config.sh" --hooks
```

This merges the hook events from the generated `templates/settings.json` (rendered from `hooks/hooks.json` by `scripts/render-settings-template.py`) into `~/.claude/settings.json` with your install path filled in. Idempotent, backs up the previous file, preserves your own hooks.

**Never run `--hooks` on a plugin-loaded install.** Both surfaces would register the same events and every hook would fire twice: doubled rule injection, doubled gate evaluation, duplicated telemetry. The script detects a plugin-loaded install and refuses; `writ doctor` reports the condition (`duplicate-hook-registration`) if it arises another way.

## 4. Optional: run the daemon as a systemd user service

By default the daemon starts on demand (SessionStart hook or `scripts/ensure-server.sh`, both singleton-safe via a file lock). For auto-restart on crash and clean lifecycle management:

```bash
bash "$WRIT_DIR/scripts/install-server-service.sh"
```

Installs `writ-server.service` (waits for Neo4j, `Restart=on-failure`) and the daily `writ-logs-rotate.timer`, stops any ad-hoc daemon first, and health-probes the result. To start at boot before first login: `loginctl enable-linger $USER` (needs sudo/polkit).

## Verify

```bash
"$WRIT_DIR"/bin/writ status                    # daemon health + rule count
test -f ~/.claude/commands/writ-approve.md && echo "/writ-approve installed"
"$WRIT_DIR"/bin/writ doctor                    # 13 checks; writ doctor --fix repairs 6 of them
```

For a raw `/health` read that does not depend on `curl`:

```bash
python3 "$WRIT_DIR"/bin/lib/writ_install.py http-get http://localhost:8765/health
```

`writ doctor` covers daemon liveness, orphaned-port conflicts, Neo4j connectivity, uniqueness constraints, the embedding stack, corpus drift, Bitbucket credentials, the git post-commit hook, the `writ` PATH symlink, Claude Code hook registration, duplicate registration, role symlinks, and mode/gate sanity.

Then open Claude Code in any project and type a prompt: you should see a `[Writ: ...]` status line and a `--- WRIT RULES ---` block.

## Updating

After `git pull` (or a plugin update), hook changes in `hooks/hooks.json` apply on the next session automatically. For everything else, re-run the one bootstrap: it is idempotent, and it re-applies the permissions, the statusLine, `~/.claude/CLAUDE.md` and the slash commands, and re-installs the package so a dependency change lands.

```bash
bash "$WRIT_DIR/scripts/bootstrap-plugin.sh"    # path A
bash "$WRIT_DIR/scripts/bootstrap.sh"           # paths B/C
```

Then restart Claude Code (config and command changes are read at session start).

## Restarting the daemon

Required only when code under `writ/server/` or `writ/retrieval/` changes; the running process keeps serving the old module until restarted. Routine ingest, query, or hook edits do not need it.

```bash
# If the systemd service is installed (check: systemctl --user status writ-server):
systemctl --user restart writ-server

# Ad-hoc daemon (no service):
bash "$WRIT_DIR/scripts/stop-server.sh" && bash "$WRIT_DIR/scripts/ensure-server.sh"
```

Do not use `stop-server.sh` against the systemd service; the service auto-restarts and the two will fight. `stop-server.sh` never touches Neo4j (it may be shared with other tools).

## Switching from standalone to the plugin

The standalone install keeps working; the plugin path is additive. To move over:

1. Stop the existing daemon (see "Restarting the daemon" above for the right command).
2. Remove the standalone symlinks: `rm -f ~/.claude/rules/writ-*.md ~/.claude/agents/writ-*.md`.
3. If you ever ran `--hooks` seeding, remove the Writ `hooks` entries from `~/.claude/settings.json` (back it up first); the plugin now supplies them.
4. Install via path A above. The Neo4j Docker volume (`writ-neo4j-data`) is shared between modes, so the corpus survives the switch.

## Troubleshooting

- **`Docker daemon not reachable`**: start Docker Desktop, or `sudo systemctl start docker`, then re-run the bootstrap.
- **`python3 version is 3.9; need >= 3.11`**: install a newer Python (`pyenv` works well).
- **`port 7687 already in use`**: another Neo4j is running; stop it or change the `ports:` mapping in `docker-compose.yml`.
- **`Neo4j did not become reachable within 60s`**: `docker compose logs neo4j`; the common cause is too little memory for Docker (Neo4j wants ~1 GB).
- **Daemon not healthy**: check the daemon log; the location is install-dependent: `$WRIT_LOG` if set, else `<install>/var/logs/server.log` (standalone) or `${CLAUDE_PLUGIN_DATA:-~/.cache/writ}/server.log` (plugin), or `journalctl --user -u writ-server` under systemd. Usually an import error; re-run `pip install -e .` inside the venv.
- **A GPU-discovery warning from onnxruntime at startup** on CPU-only machines is unsuppressible and harmless; CPU execution works normally.
- **Default Neo4j credentials (`neo4j/writdevpass`)**: a development default, silently used whenever `writ.toml` is missing. For any non-local use, change `NEO4J_AUTH` in `docker-compose.yml` and the `[neo4j]` section of `writ.toml`.

## Known limitations

- `patch-global-config.sh` leaves a foreign statusLine as-is; Writ's statusLine is skipped in that case.
- The user-commands installer overwrites identically named files in `~/.claude/commands/`.
- Config and command changes need a Claude Code restart to take effect in a running session.
- A plugin install patches nothing globally until the bootstrap runs; hooks still fire, but Bash permission prompts appear and the workflow instructions in `~/.claude/CLAUDE.md` are absent.
