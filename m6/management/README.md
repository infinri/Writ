# M6 slice 5A policy draft workshop

[D-054](../../docs/implementation/DECISION-LOG.md#d-054-m6-slice-5a-headless-policy-draft-management-owner-2026-11-19) adds a disposable in-process [`/manage/v1` contract](../../docs/contracts/management/draft-request.schema.json) and opt-in CLI. Both use the same application service. The [response contract](../../docs/contracts/management/draft-response.schema.json) references the core Decision schema for every simulated case. This batch has no network listener or production authentication integration.

## Run the fixture

From the repository root, with the pinned Rust and SQLite toolchains configured, build the fixture and allocate a private disposable directory.

```sh
. m1/tools/env.sh
cargo build --locked --features fixtures --manifest-path m6/management/Cargo.toml
lab=$(mktemp -d)
cli=m6/management/target/debug/writ-manage-fixture
printf '%s' fixture-author > "$lab/credential"
"$cli" init "$lab/drafts.sqlite"
python3 - <<'PY' | "$cli" v1 "$lab/drafts.sqlite" "$lab/credential"
import json
from pathlib import Path
print(json.dumps({
    "version": "/manage/v1",
    "operation": "create",
    "scope": {"tenant": "m6-lab", "project": "offline-proof"},
    "id": "inventory",
    "expected_revision": 0,
    "idempotency_key": "first",
    "preview": False,
    "candidate": json.loads(Path("m6/compiler/fixtures/permit.input.json").read_text()),
}))
PY
"$cli" v1 "$lab/drafts.sqlite" "$lab/credential" <<'JSON'
{"version":"/manage/v1","operation":"simulate","scope":{"tenant":"m6-lab","project":"offline-proof"},"draft":{"id":"inventory","revision":1},"corpus":[{"kind":"synthetic.inspect","resource":"synthetic:m6-lab/offline-proof/inventory/report"}]}
JSON
```

The fixture CLI reads one v1 request from stdin and writes a JSON response to stdout. Its status is 0 when `ok` is true. Public fixture credentials are `fixture-author`, `fixture-viewer`, `fixture-agent` and `fixture-adapter` as exact file bytes. The last two are refusal controls. The library defaults to zero fixture credentials. An embedding host must supply an `Authenticator` that resolves the management realm, stable subject and grants from a credential. Only the host selects the store path. It must own and control the store directory and provide a bounded authenticator. Protect the disposable database and SQLite sidecars with filesystem permissions.

## Behavior

- Policy authors create, replace, clone, validate, simulate and compare. Authors and Viewers read revision metadata. Draft lookup and grants are scoped by the exact tenant and project. Role or scope denial returns the same `NOT_FOUND` as an absent draft. The candidate's owner field is compiler input, never authority.
- Create and clone expect target revision 0. Edit expects the current positive revision and writes a new immutable revision. Clone names an exact source revision in the same scope. Get and history return content digests and clone lineage for each revision. Authors provide full candidate JSON on edits. Start history at `after: 0` and pass returned `next` as the next cursor until it is null.
- A mutation key is scoped to the verified subject, tenant and project. The durable binding digests the entire strict canonical v1 request. A replay returns the original receipt, including its scoped audit ID, even after an intervening edit. A changed request with that key refuses. Preview computes a prospective revision with a null audit ID and does not reserve a key. A single SQLite transaction couples every committed version with its audit event. Opening a marked disposable store can recover a hot journal left by an interrupted writer.
- Validation reports the authorized draft's compiler diagnostics and whether pinned offline proof exists. A supported unpinned edit can run in the synthetic simulator. Publication still requires the existing compiler proof and signed lifecycle. All draft analysis is read-only. Fixture cases construct synthetic Requests locally for the given scope and call the pure kernel. Comparison returns structural change locations and every complete before and after Decision with its changed JSON pointers, including metadata differences when the status stays the same. All analysis has zero runtime authority.
- The request cap is 40 KiB, the canonical candidate cap 32 KiB, and evaluation allows 16 manifests, companions and corpus rows per group. Synthetic resource IDs are limited to 256 bytes. History is limited to 50 rows per page. Storage caps committed versions at 1,024 per scope and 8,192 per database, and bounds SQLite row length, busy waiting and VM work. Evaluation uses bounded work units with a five-second dispatch budget check. Fixture stdin requires EOF. Host file I/O and the embedding authenticator are outside the in-process evaluation budget.
