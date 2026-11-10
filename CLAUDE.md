# Writ: project instructions for Claude Code

## Branch and scope

- Work only on `v2/architecture-rebuild`. Never modify `main`. Confirm before any push.
- V2 is being built here. The existing `writ/`, `hooks/`, `bin/`, `agents/`, `rules/`, `scripts/` (except `check-v2-docs.py`) and `tests/` trees are **V1 reference material only**: mine them for evidence, do not reproduce their structure.
- Do not change `README.md` or its media in `docs/assets/`; V2 architecture lives in `V2-ARCHITECTURE.md` and `docs/`.

## Recover state first (every new session and after every compaction)

**Start here: read [SESSION-HANDOFF](docs/implementation/SESSION-HANDOFF.md) at the start of every development session and after every compaction, before changing any code. It is the authoritative recovery entry point; resume the task its section F names (currently D-027) instead of restarting work.**

1. `git branch --show-current`, `git status`, `git log --oneline -10`, `git fetch origin && git rev-parse HEAD origin/v2/architecture-rebuild`.
2. Read [SESSION-HANDOFF](docs/implementation/SESSION-HANDOFF.md), then [WORK-STATE](docs/implementation/WORK-STATE.md), then active rows in [TASKS](docs/implementation/TASKS.md) and [DECISION-LOG](docs/implementation/DECISION-LOG.md).
3. If the docs and the repository disagree, reconcile before writing code. The repository is the source of truth, not the previous conversation.

**Continuity rule.** Before context compaction, milestone completion, or ending a substantive development session, update the authoritative session handoff with verified repository state, implemented versus unimplemented features, test evidence, open risks, decisions, and an executable next-task plan. After context compaction or a new session, read CLAUDE.md and the authoritative session handoff before changing code. Verify git state and resume the next unfinished authorized task without repeating completed work.

## Authoritative architecture

[V2-ARCHITECTURE](V2-ARCHITECTURE.md), [docs index](docs/README.md), [master plan](docs/implementation/MASTER-IMPLEMENTATION-PLAN.md), [M0 checklist](docs/implementation/M0-START-HERE.md), [security invariants](docs/security/invariants.md), [threat model](docs/security/threat-model.md), [ADRs](docs/decisions/), [contracts](docs/contracts/).

Non-negotiables: model output is untrusted; packs never grant permissions; approvals come only from authenticated humans; hard deny never becomes allow; kernel and broker import no model SDK; Rust vs Go stays OPEN until the M1 bake-off (ADR-0002).

## Working standards

- Every change maps to a task ID in TASKS.md and at least one invariant or requirement.
- Tests first for state machines, policy precedence, approvals, canonicalization and contracts.
- Never report a test as passing unless it ran in this session; record results in [TEST-EVIDENCE](docs/implementation/TEST-EVIDENCE.md). Mark work planned, implemented or verified, never blur them.
- `m0/` is a Python reference harness (decision D-001), not the production kernel.

## Commands

- M1 (pinned toolchains, user space): `source m1/tools/env.sh` (install: `m1/tools/install-toolchains.sh` and `m1/tools/install-sqlite.sh`, the shared SQLite both prototypes link, D-023). Rust: `cd m1/rust && cargo build --release --locked && cargo test --release --locked`. Go (tags required): `cd m1/go && T="libsqlite3 sqlite_omit_load_extension" && go vet -tags "$T" ./... && go build -trimpath -tags "$T" -o proto ./cmd/proto && go test -race -count=1 -tags "$T" ./...`. SQLite-ledger fixtures: `WRIT_PROTO=<binary> python3 m1/driver/parity.py --proto m1/tools/sqlite-ledger-proto.sh --manifest m1/fixtures/manifest.json`. Fixtures: `python3 m1/driver/parity.py --proto <binary> --manifest m1/fixtures/manifest.json --repeat 2`. Properties: `python3 m1/props/run.py --profile ci --seed 20261107 --rust m1/rust/target/release/proto --go m1/go/proto --out <report.json>`. Fixture drift: `python3 m1/fixtures/export_m0_fixtures.py --check`. Driver and fixture self-tests: `python3 -m unittest discover -s m1/driver/tests`.
- M0 tests: `cd m0 && .venv/bin/python -m pytest` (create venv: `uv venv m0/.venv -p 3.12 && uv pip install -p m0/.venv/bin/python -r m0/requirements.txt`)
- Audit artifacts: `cd m0 && .venv/bin/python -m writ_m0.audit generate|check`
- Docs integrity: `python3 scripts/check-v2-docs.py`
- Do not run V1 `make test` or graph migrations against non-disposable Neo4j.
