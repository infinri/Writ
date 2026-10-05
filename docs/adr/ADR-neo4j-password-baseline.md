# ADR: Neo4j password and port baseline (program item 3)

Status: accepted
Date: 2026-10-05
Plan: `.claude/plans/1c1f801f-c493-4e74-aa60-76c1e69ea30e/plan.md` (program item 3,
`docs/programs/knowledge-engine-program.md`)
Touches: `docker-compose.yml`, `writ/config.py`, `writ/graph/db/__init__.py`,
`writ/neo4j_password.py`, `writ/cli.py`, `writ/session/doctor.py`,
`scripts/lib/writ-server-lib.sh`, `scripts/bootstrap.sh`, `scripts/bootstrap-plugin.sh`,
`scripts/install-server-service.sh`, `bin/lib/writ-venv.sh`, `.github/workflows/pr.yml`

## Context

`docker-compose.yml` published Neo4j's HTTP and Bolt ports on every interface and hard-coded
the development password in `NEO4J_AUTH` and the healthcheck. The same password is the built-in
default in `writ/config.py`, used silently whenever `writ.toml` has none, and it is in this
public repository. An install that never configured a password therefore ran a graph that
anyone on its network could open with a password anyone can read.

## Decisions

### 1. Loopback-only ports

Both ports are published on 127.0.0.1. Writ's daemon and the browser that opens the Neo4j
Browser run on the same machine, so nothing legitimate needed the other interfaces. An existing
container keeps its bindings until it is re-created, which the migration does.

### 2. Compose reads WRIT_NEO4J_PASSWORD and falls back to the development default

`NEO4J_AUTH` and the healthcheck read `${WRIT_NEO4J_PASSWORD:-<default>}`. Rejected: a strict
`${WRIT_NEO4J_PASSWORD:?}`. The hook start paths compose without the variable, and a fresh
install has no password yet: `writ neo4j set-password` replaces the password with
`ALTER CURRENT USER ... FROM old`, which needs a database already running on a known old one.
The fallback cannot become a quiet production state, because the ports are loopback only and
every connection refuses that password. Compose interpolates once, at creation, so the container
is re-created with the variable set after every change; the bootstraps do this and wait for the
healthcheck to pass on the new password. The documented form sets it inline for the one compose
command: an exported value would outlive it and, because the variable overrides `writ.toml`,
send a stale password after the next rotation.

### 3. One refusal point, an exact opt-in, exit 78

Every connection Writ makes is constructed by `Neo4jConnection`, so its constructor refuses the
development password unless `WRIT_ALLOW_DEV_PASSWORD` is exactly `1`. Rejected: raising in
`get_neo4j_password` (it would break the getter's contract and the command that must read the
old password to replace it); checks at each call site (seventeen of them, and the next one
forgets). `writ serve` checks before binding anything and exits 78 (EX_CONFIG); the systemd unit
lists 78 in `RestartPreventExitStatus`; `writ doctor` gains a `neo4j-password` check that names
the state, never the value. The isolated test graph uses its own password and is unaffected; CI's
throwaway services keep the development password and opt in at workflow level.

### 4. writ neo4j set-password

It generates 32 letters and digits with `secrets` (about 190 bits, and no character that needs
quoting in TOML or in the healthcheck's shell). Everything that can fail without the database
is checked first: the resolved instance is the one `writ.toml` names, the new file is rendered
and verified in memory, and the directory is writable. Then the configured password is proven
to authenticate, `ALTER CURRENT USER SET PASSWORD FROM $old TO $new` runs on the system
database with parameters, and `writ.toml` is replaced atomically with mode 0600. The new file is
staged before the ALTER (a 0600 temp file beside `writ.toml`, written and fsynced) and renamed
over it after, then the directory is fsynced. A failure before the ALTER changes nothing. Any
exception between the ALTER and the rename, an interruption included, changes the database back;
only if that also fails is the new password printed, with two recovery routes, because it then
exists nowhere else. A kill that runs no code leaves the staged file, so the password is not
lost even then; every other failure removes it. The file edit is line based and verified by parsing both versions, so comments and
other sections survive; a form it cannot express is refused. Rejected: re-serialising with a
TOML writer (drops comments and order); writing the file before the ALTER (a failed ALTER would
leave a file that does not authenticate).

The password is never printed on success, because scrollback and logs keep what is printed.
`writ neo4j password` prints the stored value on request and reads the file, not the
environment, so a stale export cannot echo itself back. A rerun authenticates with the current
password and rotates it; the bootstraps call `writ neo4j check` first so they do not rotate on
every run.

Concurrent runs (several sessions, or two bootstraps) are serialised by an exclusive `flock` on
`.writ.toml.lock` beside `writ.toml`, held for the whole change, with the file re-read inside it:
without it both runs authenticate with the same old password and the second ALTER fails, or
rotates twice. The wait is bounded (`--lock-timeout`, default 60s); a timeout exits 1 and changes
nothing. The lock file stays after a run, because unlinking a flock file lets two runs lock
different inodes. `--if-default` makes a run that finds anything but the development default,
read under the lock, change nothing; the bootstraps pass it, so two of them rotate once. Readers
(`writ neo4j password`, `check`) take no lock: the rename makes every read see a whole file, old
or new, and a shared lock would block them for as long as a run waits for a booting Neo4j.

### 5. Install and migration

Both bootstraps run the command after Neo4j is up and before the corpus import, re-create a
container owned by the `writ` Compose project and wait for it to report healthy. A re-run checks
the container as well as the password: one that still publishes a port beyond 127.0.0.1, or
reports unhealthy, was not re-created after the change and is re-created now, so a migration that
stopped halfway finishes; a container
from an older project is left for the documented manual re-homing. The shared start routine
prints a one-time notice naming the command instead of launching a daemon that would exit 78.
Because `writ.toml` lives in the install directory and a plugin upgrade creates a new one, the
venv repoint at SessionStart and the plugin bootstrap carry `writ.toml` forward, privately, when
the new directory has none.

## Consequences

- A fresh install never serves on the published password, and Neo4j is not reachable off the
  machine.
- An existing install's daemon stops starting after the upgrade until `writ neo4j set-password`
  runs (or the bootstrap is re-run); SessionStart, `writ serve` and `writ doctor` all say so.
- The password appears in the container's environment and healthcheck (`docker inspect`) and in
  the container's process list during a healthcheck, as with any environment-configured image;
  never on the host process list. Access to the container runtime is already root-equivalent.
- A lost password needs Neo4j's recovery mode; docs/install.md has the steps.
- The isolated test graph still publishes its ports on every interface with its published test
  password; it holds only disposable data, and binding it to loopback is left to a follow-up.
