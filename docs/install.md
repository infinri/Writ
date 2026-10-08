# Installing Writ

Writ runs the same way under two install paths; pick one:

- **A. Marketplace plugin** (recommended): `claude plugin install` from this repo's own marketplace.
- **B. Clone anywhere**: a git clone at any path you like. If Claude Code discovers it as a plugin, its hooks load from `hooks/hooks.json`; if nothing discovers it, seed them into `~/.claude/settings.json` (section 3).

In every path, hook registrations come from one place, `hooks/hooks.json`. The current counts are generated from that file into [`reference/hooks.md`](reference/hooks.md). Editing that file is all a hook change needs.

The daemon, in this guide and everywhere else in the docs, is Writ's local server process (`writ serve`, on `http://localhost:8765` and a private unix socket) that the hooks call for retrieval, session state and gate decisions.

**Where state lives (both paths):** session caches, approvals and the pending-test and lint scratch files under `$XDG_STATE_HOME/writ` (default `~/.local/state/writ`), typed logs under its `logs/`. None of it is inside the install, so an upgrade or a second copy of Writ sees the same sessions. The first session after upgrading from 1.8.0 or earlier copies each old `<install>/var/session/writ-session-*.json` across once, never overwriting.

## Prerequisites

Linux or macOS (the hooks are bash; Windows is not supported). Python 3.11+. Docker with its daemon running (Neo4j runs in a container and wants about 1 GB of memory). `git` for the clone path. Network access once, for Python packages and the embedding model. `jq` and `curl` are optional accelerators: every JSON read has a Python fallback and every HTTP call has a `urllib` fallback, so their absence changes speed, never behavior. Nothing needs `envsubst`/gettext.

### Python dependencies

Both bootstraps install the package with its `dev` extra into their venv; you do not install these by hand.

| Group | What it holds | When it is installed |
|---|---|---|
| Required | `fastapi`, `starlette`, `uvicorn`, `neo4j`, `tantivy`, `hnswlib`, `httpx`, `pydantic`, `typer`, `rich`, `onnxruntime` | Always |
| `dev` | Test and lint tools, and `optimum`, which exports the ONNX embedding model during bootstrap | By both bootstraps |
| `fallback` | `sentence-transformers` and `scikit-learn` (about 5 GB with their dependencies) | Only by hand, for `WRIT_ALLOW_EMBEDDING_FALLBACK=1`, `writ compress` and the redundancy check in `writ validate` |
| `benchmark` | Benchmark tooling | Maintainers only |

## 1. Install (path A: marketplace plugin)

```bash
claude plugin marketplace add infinri/Writ
claude plugin install writ@writ
```

Now open Claude Code once in any project. Writ sees the un-bootstrapped install and prints one absolute command on its own line:

```
bash /path/it/prints/scripts/bootstrap-plugin.sh
```

Run that, restart Claude Code, and you are done: there is no install-path lookup step and no separate config patch. (If you would rather not open Claude Code first, `claude plugin list --json` carries the `installPath`; read it by eye. There is no `claude plugin path` subcommand.) Add `--preflight` to the command to run only the prerequisite checks.

When the bootstrap finishes it prints a banner headed `Writ plugin is ready`, with the lines `Plugin root`, `Venv`, `Neo4j`, `Writ daemon`, `Rules loaded`, `Daemon log`, `Global config` and `Verify`, and ends with `! Restart Claude Code for the hooks to take effect.` Keep the `Plugin root` path; the Verify section below uses it.

### Let Claude Code run the install

You already have an agent that reads instructions and runs commands. Point it at this page:

> Install Writ from https://github.com/infinri/Writ. Verify that Python 3.11+ and Docker are available, follow the plugin installation instructions, run the bootstrap command Writ prints after Claude Code starts, restart Claude Code, and verify that the Writ service is healthy. Stop and explain anything that requires me to install or approve it manually.

Claude Code can do the project setup. It cannot install Docker or Python for you, so if either is missing you will be asked to handle that part yourself.

**Path B (clone):**

```bash
git clone https://github.com/infinri/Writ /any/path/you/like/writ
WRIT_DIR=/any/path/you/like/writ
bash "$WRIT_DIR/scripts/bootstrap.sh"
```

It ends with a banner headed `Writ is ready`. The clone path also links `~/.local/bin/writ` (and warns when `~/.local/bin` is not on your PATH); the plugin path does not, and its CLI is `<plugin root>/bin/writ` (inside Claude Code the plugin's `bin/` is on the Bash tool's PATH).

## 2. What the one bootstrap does

Both bootstraps are idempotent and safe to re-run. Each does the whole install:

| Step | `bootstrap-plugin.sh` (path A) | `bootstrap.sh` (path B) |
| --- | --- | --- |
| Python venv | `$CLAUDE_PLUGIN_DATA/.venv`, i.e. `~/.claude/plugins/data/<plugin>-<marketplace>/.venv` (outside the plugin root, so an upgrade that rewrites the install path does not orphan it; found from the install path when the variable is not exported). Each SessionStart points its editable `writ` at the running version | `$WRIT_DIR/.venv` |
| Package, ONNX model, Neo4j, corpus, daemon | yes | yes |
| Private Neo4j password (`writ neo4j set-password`, once; an install that already has one keeps it) | yes | yes |
| `~/.claude/settings.json` + `~/.claude/CLAUDE.md` | yes | yes |
| `~/.claude/commands/` slash commands | yes | yes |
| `~/.local/bin/writ` and `~/.claude/{rules,agents}` symlinks | no (the plugin loader supplies the agents) | yes |

Neo4j's ports are published on 127.0.0.1 only. Before anything else connects, the bootstrap replaces the development password: it generates a random one, changes it in the running database, saves it to `writ.toml` with mode 0600, and re-creates the container so its healthcheck uses it. The password is never printed; `writ neo4j password` prints it when you ask.

Both honor `WRIT_VENV`. The full lookup order is in `bin/lib/writ-venv.sh`; see `reference/configuration.md`.

Each accepts `--preflight` to run only the prerequisite checks (tool presence and the Python version) and exit, which is a quick way to confirm a machine is ready before committing to a full install.

The global-config part exists because a plugin manifest cannot ship a permission allowlist, a statusLine, a top-level settings value, or `~/.claude/CLAUDE.md`. It merges the Writ allow/deny entries into `~/.claude/settings.json` (preserving your ordering and your non-Writ entries), sets the Writ statusLine (a foreign statusLine is left untouched), writes the settings keys Writ ships a default for (`outputStyle: "Concise"` and `effortLevel: "high"` today, and a value you already set is left untouched, so a machine that already carries `effortLevel: "xhigh"` keeps `xhigh` and the shipped default reaches only a file where the key is absent), and renders `templates/CLAUDE.md` into `~/.claude/CLAUDE.md`, backing up anything it replaces. A missing `settings.json` is created. By default it never touches the `hooks` block: the plugin loader owns hooks.

To run either piece on its own, or to preview it:

```bash
bash "$WRIT_DIR/scripts/patch-global-config.sh"      # --dry-run to preview
bash "$WRIT_DIR/scripts/install-user-commands.sh"    # USER_COMMANDS_DIR=/path to redirect
```

## 3. Hook seeding (path B, when nothing discovers the clone)

If your clone lives at a path the plugin loader does not discover, `hooks/hooks.json` is never read and **no hooks load at all**: no gates, no rule injection, no enforcement. Confirm discovery first:

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

First set `WRIT_DIR`. Path A: the `Plugin root` line of the bootstrap banner. Path B: your clone.

```bash
WRIT_DIR="<plugin root, or your clone>"
"$WRIT_DIR"/bin/writ status                    # daemon health + rule count
test -f ~/.claude/commands/writ-approve.md && echo "/writ-approve installed"
"$WRIT_DIR"/bin/writ doctor                    # run it for the current check list; --fix repairs 7 of them
```

For a raw `/health` read that does not depend on `curl`:

```bash
python3 "$WRIT_DIR"/bin/lib/writ_install.py http-get http://localhost:8765/health
```

`writ doctor` covers daemon liveness, orphaned-port conflicts, Neo4j connectivity, the Neo4j password, uniqueness constraints, duplicate records, index degeneracy, the daemon socket, the embedding stack, corpus drift, Bitbucket credential presence, the git post-commit hook, the `writ` PATH symlink, Claude Code hook registration and duplicate registration, hook telemetry coverage, stranded telemetry, sub-agent role coverage and declared write scope, the sub-agent governance census, role symlinks, mode and gate sanity, gate refusal liveness, and the extension trust ledger. Run the command for the authoritative list rather than relying on this sentence staying complete.

### What success looks like

- `writ status` prints the daemon's `/health` JSON with `"status": "healthy"`, `"index_state": "warm"` and a non-zero `"rule_count"` (with `"mandatory_count"` beside it). `"status": "degraded"` means the index is warm but the database reports zero rules; re-run the bootstrap.
- `Service not running. Start with: writ serve` (exit 1) means the daemon is down. See "Restarting the daemon" below.
- `writ doctor` prints a `STATUS  NAME  DETAIL` table whose statuses are `ok`, `warn` or `fail`, and exits non-zero when any row is `fail`.
- In Claude Code, the status bar shows `Writ ctx <n>%`.
- A prompt carries a `--- WRIT RULES (<n> rules, <mode> mode) ---` block when retrieval matched, or a line beginning `[Writ: no matching rules found for this task.` when it did not. The first prompt of a session also carries the `=== ALWAYS-ACTIVE RULES ===` block.

### Next

- Set a mode, or let auto-route set one: `writ mode set <conversation|debug|investigate|review|work> <session_id>`.
- Make a repository's docs, ADRs and READMEs retrievable with `writ docs ingest --repo <repo root>`. The repository must be a registered project first; approving a plan in it, or a captured commit, registers it.
- `writ git-hooks install --repo <repo root>` installs the post-commit hook that records decisions. Writ also installs it automatically on the first Work-mode entry into a repository.

## Updating

After `git pull` (or a plugin update), hook changes in `hooks/hooks.json` apply on the next session automatically. For everything else, re-run the one bootstrap: it is idempotent, and it re-applies the permissions, the statusLine, `~/.claude/CLAUDE.md` and the slash commands, and re-installs the package so a dependency change lands.

```bash
bash "$WRIT_DIR/scripts/bootstrap-plugin.sh"    # path A
bash "$WRIT_DIR/scripts/bootstrap.sh"           # path B
```

Then restart Claude Code (config and command changes are read at session start).

After a plugin update, the first session's SessionStart notices that the shared venv still imports the previous version and reinstalls the package from the new one; it prints one line saying so, and a daemon that was already running needs one restart (see "Restarting the daemon" below).

### Upgrading from 1.8.0 or earlier: the Neo4j container

Nothing is required. A container created by 1.8.0 carries the Compose project `180`, because Compose used to name the project after the install directory. Every start path now runs `docker start writ-neo4j` when that container exists, so it keeps working under any later version.

Re-homing it under the project `writ` is optional; it only tidies `docker compose ls`. The graph lives in the named volume `writ-neo4j-data`, which removing the container does not touch. Nothing does this automatically. To do it by hand:

```bash
docker inspect -f '{{index .Config.Labels "com.docker.compose.project"}}' writ-neo4j   # the old project, e.g. 180
OLD=~/.claude/plugins/cache/writ/writ/1.8.0        # the install dir that created it
docker compose -p 180 -f "$OLD/docker-compose.yml" stop neo4j
docker compose -p 180 -f "$OLD/docker-compose.yml" rm -f neo4j
docker compose -f ~/.claude/plugins/cache/writ/writ/1.9.0/docker-compose.yml up -d neo4j   # the NEW install dir
```

If the old install dir is already gone, `docker stop writ-neo4j && docker rm writ-neo4j` replaces the two middle commands. Compose may warn that the volume `writ-neo4j-data` already exists and was created for another project; that is expected, and it reuses the volume.

### Securing an existing install (Neo4j password and loopback ports)

Writ refuses the published development password: `writ serve`, every command that opens the graph and `writ doctor` stop with a message naming `writ neo4j set-password`, and SessionStart prints that message instead of starting a daemon that would refuse. Re-running the bootstrap does the whole migration. By hand, once:

```bash
writ neo4j set-password                               # changes it in Neo4j, saves writ.toml (0600), prints nothing secret
docker inspect -f '{{range .Mounts}}{{.Name}} {{end}}' writ-neo4j                     # must print writ-neo4j-data
docker inspect -f '{{index .Config.Labels "com.docker.compose.project"}}' writ-neo4j   # writ, or an older project
WRIT_NEO4J_PASSWORD="$(writ neo4j password)" docker compose -f "$WRIT_DIR/docker-compose.yml" up -d neo4j   # re-creates it on 127.0.0.1 with the new healthcheck
systemctl --user restart writ-server                  # or scripts/stop-server.sh, then scripts/ensure-server.sh
writ doctor                                           # neo4j-password and neo4j-connectivity report ok
```

If the project is not `writ`, Compose refuses the container name. Remove the old container first (`docker stop writ-neo4j && docker rm writ-neo4j`), then run the compose line. The graph lives in the named volume `writ-neo4j-data`, which removing the container does not touch; the first `docker inspect` line is how you confirm that before removing anything. If you use the systemd service, re-run `scripts/install-server-service.sh` once so a refused start is not retried every few seconds.

The variable is set inline, for that one command, on purpose: an `export` stays in the shell and, because `WRIT_NEO4J_PASSWORD` overrides `writ.toml`, would send a stale password after the next rotation.

Re-running the bootstrap is safe at any point. It finishes a migration that stopped halfway: when the password is already private but `writ-neo4j` still publishes a port beyond 127.0.0.1, or its healthcheck reports unhealthy, it re-creates the container. Concurrent runs are safe too. `writ neo4j set-password` holds an exclusive lock on `.writ.toml.lock` beside `writ.toml` for the whole change and re-reads the file under it, so two sessions or bootstraps take turns instead of both authenticating with the same old password; a run that waits longer than `--lock-timeout` seconds (default 60) exits 1 and changes nothing. The bootstraps pass `--if-default`, which makes a run that finds a private password already in place change nothing, so two concurrent bootstraps rotate the password once.

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

## Why was I blocked?

Every refusal starts with a tag in brackets. Find it here. "Blocks" means the tool call is refused, "Asks" means Claude Code asks you to confirm, and "Reports" means the message is shown and nothing is refused.

| Message starts with | Kind | What it means | What to do |
|---|---|---|---|
| `[ENF-GATE-MODE]` | Blocks | The session has no mode, so no write is allowed | Run the `writ mode set` command the message prints; it carries your session id. If a sub-agent was refused, the main session sets the mode and dispatches it again. |
| `[ENF-GATE-PLAN] ALL writes blocked` | Blocks | Work mode, and the plan is not approved | Read the plan, and reply `approved` after the agent asks for it. |
| `[ENF-GATE-PLAN] plan.md cannot be modified` | Blocks | The implementation phase freezes `plan.md` | Reply `replan approved`. The session returns to planning and both gates are cleared. |
| `[ENF-GATE-TEST]` | Blocks | The plan is approved and the test skeletons are not | The agent writes a test file with a test signature and asks; reply `approved`. With no runnable tests, reply `manual test approved` (a grant that lasts 30 minutes), then `approved` when asked. |
| `[ENF-GATE-DRIFT]` | Blocks | `plan.md` changed after you approved it | Review the change, and reply `approved` when the agent asks. |
| `[ENF-PROJECT-BOUNDARY]` | Blocks | The path is outside the project root | Declare the absolute path in the plan's `## Files`, then approve again. A sub-agent cannot do this; it reports to the main session. |
| `[ENF-ROLE-SCOPE]` or `[ENF-GATE-SUBAGENT]` | Blocks | A sub-agent wrote outside its role's declared scope, or where its orchestrator would be refused | A role scope changes only when a human edits the role. For gate inheritance, resolve the main session's pending gate. |
| `[SEC-CREDENTIAL-WRITE]` | Blocks | The path is a credential or secret file | Write the file yourself. Templates named `.env.example`, `.env.sample` or `*.pub` are allowed. |
| `[DEBUG-GATE-ROOT-CAUSE]` or `[DEBUG-EVIDENCE-FIRST]` | Blocks (debug mode) | Source edits before a root cause is written, or source reads before evidence is recorded | Fill `## Root cause` (for edits), or `## Evidence` and `## Narrowing` (for reads), in the session's `debug.md` (`.claude/debug/<session_id>/debug.md`). |
| `ENF-PROC-TDD-001:` | Blocks (Work mode) | A Write of a source file under `src/`, `lib/` or `app/` with no conventional test file carrying assertion markers | Add the test file the message lists, with assertions; or, for code with no runnable harness, reply `manual test approved`. |
| `[ENF-POST-007]` or `[ENF-POST-008]` | Blocks | Pre-write checks found an error in the proposed content, or a shell file with a syntax error or with all its code commented out | Fix the flagged content. |
| `[ENF-IRREVERSIBLE]` | Blocks | A destructive command (for example a hard reset, a force push, or a destructive database statement) | If you intend it, run it yourself with a leading `!` in Claude Code. |
| `[ENF-GATE-STATE]` | Blocks | The command or write targets Writ's own approval state | Nothing to do: approvals come from your replies. To write prose that names gate state in a commit, use `git commit -F <file>`. |
| `[ENF-TOOL-BUDGET]` | Blocks | The identical call already failed three times in a row | Change the input. For a shell command you want repeated, run it yourself with `!`. |
| `[ENF-STRICT-001]` | Blocks | `WRIT_STRICT=1` is set and no verdict could be obtained | Start the daemon, or unset `WRIT_STRICT`. |
| `ENF-PROC-WORKTREE-001:` | Blocks, or asks when the target cannot be resolved | `git worktree add` into a path inside the project that `.gitignore` does not cover | Add the directory to `.gitignore`, or create the worktree outside the project. |
| `[SEC-BASH-EGRESS]` | Asks | A shell command appears to send local data to a host not on your allowlist | Confirm or decline. Allowlist hosts in `writ.toml` (`[egress] allow_hosts`) or `WRIT_EGRESS_ALLOW_HOSTS`. |
| `[ENF-BASH-WRITE-UNRESOLVED]` | Asks | A shell write whose target Writ cannot resolve, such as an unset variable | Confirm or decline; spelling the path literally avoids the question. |
| `[Writ] The reviewer left` | Asks | `git commit` while CRITICAL reviewer findings stand | Fix the findings and re-run `writ-reviewer`, or confirm to commit anyway. |
| `[ENF-DECIDER-INCOMPLETE]` or `[WRIT CRITICAL]` | Asks, or reports | A Writ decision helper did not finish, so the call was not judged | Run `writ doctor`. |
| `[WRIT-READ-SIZE]` or `[WRIT-READ-JUNK]` | Blocks only with `WRIT_READ_JUNK_GATE=enforce`; by default the check only records | A whole-file read of a large or generated file | Read with offset and limit, or grep for what you need. |
| `[ENF-COMMS-OUTPUT-001]`, `[ENF-TEST-001]` or `ENF-PROC-VERIFY-001` at the end of a turn | Reports | The reply used forbidden punctuation, a marked test failed, or a quality self-score was under 3 | Read it and fix what it names. These exit 1, which Claude Code treats as a non-blocking hook error, so the turn is not held. |

Two more things can look like a block. Once any gate (drift, plan or test skeletons) has refused twice in a session, the Write and Edit hook asks you instead of refusing, for every refusal on that hook except credential paths, which always stay refused, prefixed `[Writ: repeated gate violation #N]`; declining keeps the refusal. The counts clear when that gate advances. And an approval can be refused by the gate's validator:

```text
[Writ: planning gate REJECTED, not advanced] plan.md validation failed: ...
Fix the issue above in one edit; the rejection spent the prior approval, so the user must approve again.
```

Fix what the message names (the plan's sections, its `## Files` lines, rule ids it cites that were never injected), then reply `approved` again when asked. When the gate could not evaluate the artifact at all, the second line reads `Your approval was NOT consumed: fix the cause above and retry; you do not need to approve again.` instead.

If the daemon is down when you approve, nothing advances and Writ prints:

```text
[Writ: <gate> gate NOT advanced -- the Writ daemon did not answer]
Your approval was not consumed. Start the daemon and try again:
  systemctl --user restart writ-server
Then reply "approved" once more; a duplicate advance on an already-advanced gate is a
no-op, so retrying is safe.
```

Without the systemd service, start it with `bash "$WRIT_DIR/scripts/ensure-server.sh"`, then reply `approved` again.

## Troubleshooting

- **`Docker daemon not reachable`**: start Docker Desktop, or `sudo systemctl start docker`, then re-run the bootstrap.
- **`python3 version is 3.9; need >= 3.11`**: install a newer Python (`pyenv` works well).
- **`port 7687 already in use`**: another Neo4j is running; stop it or change the `ports:` mapping in `docker-compose.yml`.
- **`Neo4j did not become reachable within 60s`**: `docker compose logs neo4j`; the common cause is too little memory for Docker (Neo4j wants ~1 GB).
- **Daemon not healthy**: check the daemon log; the location is install-dependent: `$WRIT_LOG` if set, else `$XDG_STATE_HOME/writ/logs/server.log` (default `~/.local/state/writ/logs/server.log`, clone) or `${CLAUDE_PLUGIN_DATA:-~/.cache/writ}/server.log` (plugin), or `journalctl --user -u writ-server` under systemd. Usually an import error; re-run `pip install -e .` inside the venv.
- **A GPU-discovery warning from onnxruntime at startup** on CPU-only machines is unsuppressible and harmless; CPU execution works normally.
- **`Refusing to use Neo4j with the built-in development password`**: run `writ neo4j set-password` once, then the steps it prints (see "Securing an existing install"). `WRIT_ALLOW_DEV_PASSWORD=1` opts out, for a throwaway instance or CI only.
- **`The configured Neo4j password does not authenticate`**: `writ.toml` and the database disagree. If you know the password the database uses, run `WRIT_NEO4J_PASSWORD='<that password>' writ neo4j set-password`. After a plugin upgrade, `writ.toml` is carried forward from the previous version directory automatically while that directory still exists; copy it by hand otherwise.
- **A `.writ.toml.XXXX` file beside `writ.toml`**: a `set-password` run was killed after it changed the database and before it saved `writ.toml`. That file (mode 0600) holds the new password the database now uses; rename it over `writ.toml` to recover, or delete it if `writ neo4j check` already passes.
- **The password is lost entirely**: reset it with Neo4j's own recovery mode (authentication off, no published ports), then store a fresh one:

  ```bash
  docker stop writ-neo4j
  docker run -d --rm --name writ-neo4j-recover -e NEO4J_AUTH=none -v writ-neo4j-data:/data neo4j:5
  sleep 30
  TMP_PW="$(python3 -c 'import secrets; print(secrets.token_hex(16))')"
  docker exec writ-neo4j-recover cypher-shell -d system "ALTER USER neo4j SET PASSWORD '$TMP_PW' CHANGE NOT REQUIRED"
  docker stop writ-neo4j-recover
  docker start writ-neo4j
  WRIT_NEO4J_PASSWORD="$TMP_PW" writ neo4j set-password
  ```

- **`writ-neo4j` reports `unhealthy` after a password change**: its healthcheck still carries the old password; re-create it with the inline `WRIT_NEO4J_PASSWORD=... docker compose` line above, or re-run the bootstrap.

- **`writ: venv python not found at ...`**: run the bootstrap it names; the path it prints is where the venv belongs. Set `WRIT_VENV` to use a venv elsewhere.
- **`[Writ] ... imports writ from ..., not this install; reinstalling it`** at session start: expected once after an upgrade, or when the venv was bootstrapped from another copy of Writ. Restart the daemon afterwards. If it reports it could not repoint, run the `bootstrap-plugin.sh` command it prints.
- **`Conflict. The container name "/writ-neo4j" is already in use`**: an older Writ is starting Neo4j with `docker compose up`. Current versions run `docker start writ-neo4j` instead; start it by hand with that command.

## Known limitations

- `patch-global-config.sh` leaves a foreign statusLine as-is; Writ's statusLine is skipped in that case.
- The same never-clobber rule applies to the settings keys Writ ships a default for: if you have already chosen an `outputStyle`, the patcher keeps your value, prints both values, and writes nothing. Writ will never restore its own default over your choice, so the way back to `Concise` is `/config` or editing `~/.claude/settings.json` yourself. `writ doctor` reports a differing value as information rather than as a problem, which is why it does not offer a fix for it.
- The user-commands installer overwrites identically named files in `~/.claude/commands/`.
- Config and command changes need a Claude Code restart to take effect in a running session.
- A plugin install patches nothing globally until the bootstrap runs; hooks still fire, but Bash permission prompts appear and the workflow instructions in `~/.claude/CLAUDE.md` are absent.
