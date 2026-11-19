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

## Run signed publication and rollback

[D-055](../../docs/implementation/DECISION-LOG.md#d-055-m6-slice-5b-headless-signed-policy-lifecycle-owner-2026-11-19) adds the disposable signed lifecycle to the shared service. From the repository root:

```sh
. m1/tools/env.sh
cargo build --locked --features fixtures --manifest-path m6/management/Cargo.toml --bins
python3 m6/management/run-publication-lab.py
```

The lab creates and validates two pinned drafts, obtains a pre-signed pair for each from the separate fixture signer, previews each selection, obtains a distinct human fixture presentation and commits each pair. It inspects the correlated audit, rolls back to version 1 with fresh authorization, then reopens both stores in a new CLI process. The verified history selects versions 1, 2 and 1 at revisions 1, 2 and 3.

- `writ-manage-fixture init-publication REGISTRY` initializes the separate registry. `writ-manage-fixture v1 DRAFT_DB CREDENTIAL_FILE REGISTRY` enables publication alongside draft operations. Keep both files in your own disposable directory.
- `writ-publication-authority-fixture sign-publication VERSION` reads candidate JSON from stdin and emits a signed pair with prerequisites. `writ-publication-authority-fixture authorize-publication STEP_UP_FILE CEREMONY_ID` reads a complete successful preview response and emits an opaque, 60-second presentation. The fixture uses public test credentials `fixture-publisher` and `fixture-publisher-step-up` as exact file bytes. The keys for signing and ceremony issuance are only in the separate authority fixture binary.
- `preview_publish` and `preview_rollback` take the expected activation revision, publication version, idempotency key and explicit acknowledgment boolean. Publish preview also takes the exact draft reference and signed publication. The result displays the source draft, proof, publisher, fingerprint and proposed selection. Submit the same fields with `publish` or `rollback`, the preview's intent digest and the opaque presentation. Preview makes no durable writes and reserves no ceremony or idempotency key. Publish selects immediately in the disposable registry, and rollback targets an earlier immutable publication with attributable managed draft history. Standalone D-051 history is visible and remains eligible for its existing direct rollback path.
- The publisher role is scoped to a human management principal and requires separate D-051 step-up. The default service requires a different author and publisher. The opt-in local fixture allows `fixture-author-publisher` to use both roles with `acknowledge_self: true` when that subject contributed to the draft or its clone lineage. The acknowledgment is fingerprinted and visible in audit. Existing ordinary approvals and human signoff keep their own separation rules.
- `publication_history` accepts `after` and `limit`, returning immutable selection events with the exact management context correlated to the registry's audit ID. A coherent selected record that fails current trust is marked `verified: false`. Consumers must use the registry's verified selection path.
- The management SQLite store records an intent before dispatch. A live witness and draft write fence order changes against the registry's own transaction. A retry with the original key and exact body reads the authoritative receipt by intent identity. If there is no receipt and the outcome is uncertain, it returns `UNAVAILABLE`; fetch and verify history before starting a new preview and fresh ceremony with a new key. The registry transaction alone commits immutable publication, selection and audit. The two SQLite stores have separate transactions.

## Run signed policy retirement

Build the opt-in binaries and run the disposable retirement scenario from the repository root.

```sh
. m1/tools/env.sh
cargo build --locked --manifest-path m6/management/Cargo.toml --features fixtures --bins
python3 m6/management/run-retirement-lab.py
```

The scenario selects a verified signed replacement, retires the original inactive publication and reopens both stores. It verifies the exact retirement receipt, the unchanged replacement selection and refusal of attempts to publish or roll back to the retired publication.

- `preview_retire` takes the same fields as `preview_rollback` plus the exact text `confirmation: "retire <publication_version>"`. Send the same fields to `retire` with the preview's `intent_digest` and a fresh presentation from the separate fixture authority. The preview and receipt report the expected activation revision because retirement does not advance it. `publication_history` includes the retirement and its correlated context. Retry with the exact original body and key to recover the receipt.
- The registry checks current selection and complete owning dependency state before retiring the target. An active dependent at preflight returns `ACTIVE_DEPENDENTS`. If a dependent appears after preflight, the write and its exact retry return `REVISION_CONFLICT`. Unavailable or incomplete dependency state returns `UNAVAILABLE`. The fixture operates in its closed disposable scope.
- `preview_deactivate` and `deactivate` take `expected_revision`, `publication_version`, `policy_id` and `confirmation` with the exact text `deactivate <policy_id>`. The owner returns `INVALID_POLICY` for mandatory acceptance content and `UNSUPPORTED` for synthetic authorization rule removal. This compiler profile issues no deactivation ceremony.

## Run evaluator and waiver management

Build the opt-in binaries and run the disposable workflow. Every CLI operation opens the stores in a fresh process.

```bash
source m1/tools/env.sh
cargo build --locked --manifest-path m6/management/Cargo.toml --features fixtures --bins
python3 m6/management/run-lifecycle-lab.py
```

The CLI and embedding API send the same `/manage/v1` JSON to `execute_v1_with_service`. Embedders supply the appropriate owner adapter with `Service::with_evaluator` or `Service::with_waiver`. Lifecycle writes require a configured management authenticator that pins the current identity and scope until the owning transaction returns. The fixture CLI selects its local owner stores with `v1-evaluator MANAGEMENT_DB CREDENTIAL_FILE LEDGER_DB` or `v1-waiver MANAGEMENT_DB CREDENTIAL_FILE LEDGER_DB POLICY_DB`. The scenario builds new stores and provisions only synthetic fixture state.

| Operation | Required fields beyond version, operation and scope |
|---|---|
| `evaluator_inspect` | `evaluator_id`, `after`, `limit` |
| `evaluator_preview_publish` | `expected_revision`, `entry` |
| `evaluator_preview_revoke` | `expected_revision`, `evaluator_id` |
| `evaluator_publish`, `evaluator_revoke` | Corresponding preview fields plus `intent_digest`, `idempotency_key`, `presentation` |
| `evaluator_recover` | `idempotency_key` |
| `waiver_request` | `transition_ref`, `workflow_id`, `from_state`, `to_state`, `expected_revision`, `criterion_id`, `reason` |
| `waiver_begin` | `transition_ref`, `waiver_id`, `approver_issuer`, `approver_subject`, `idempotency_key` |
| `waiver_finish` | `transition_ref`, `waiver_id`, `challenge_id`, `idempotency_key`, `dispatch_key`, `presentation` |
| `waiver_inspect` | `waiver_id` |
| `waiver_validate` | `transition_ref`, `waiver_id` |
| `waiver_recover` | `dispatch_key` |

A scoped management Publisher can preview an evaluator change. The separate evaluator fixture authority turns the exact preview into a purpose-specific human presentation. The owner rechecks the scope, revision, live management grant, human authorization and implementation and key bindings in the fenced registry operation. The owner returns its immutable sequence and scoped revision. Inspection includes current entry and key status and paginates immutable receipt metadata and entry digests by scoped revision. The evaluator fixture uses a fixed test clock. The owner’s freshness and revocation checks are exercised with expired and revoked test authorities.

A scoped Waiver Operator can submit a request and relay the human challenge and assertion. The configured ingress resolves the transition reference. The broker and ledger derive the authoritative workflow, policy, artifact and excluded parties. The external fixture authority supplies the human assertion. The ledger checks current authority and binds its single consumption to recording `waived`. Finish uses the ceremony key from begin and a separate dispatch key for management recovery.

Use inspect to read a waiver's displayed context and any historical receipt. Use validate to recheck the waiver against current state. A current waiver retains the `waived` outcome, and a mandatory criterion stays incomplete. Recover returns a historical receipt scoped to the original dispatch and caller. If that exact receipt cannot be found, management returns `UNAVAILABLE` and leaves the owner mutation untried on recovery. A deliberately new attempt has a new dispatch key and must pass all current owner checks.

[Request schema](../../docs/contracts/management/draft-request.schema.json) · [Response schema](../../docs/contracts/management/draft-response.schema.json)

## Run custom-contract administration

[D-058](../../docs/implementation/DECISION-LOG.md#d-058-m6-slice-5e-headless-custom-contract-administration-owner-2026-11-19) uses disposable stores, the existing pinned SQLite and Rust toolchains, and the same `jsonschema==4.23.0` previously reviewed for V2 repository schemas. Install it from the existing requirements file in an isolated Python environment.

```sh
. m1/tools/env.sh
lab=$(mktemp -d)
python3 -m venv "$lab/python"
"$lab/python/bin/python3" -m pip install -r requirements-v2-docs.txt
export WRIT_CONTRACT_PYTHON="$lab/python/bin/python3"
cargo build --locked --manifest-path m6/management/Cargo.toml --features fixtures --bins
"$WRIT_CONTRACT_PYTHON" m6/management/run-contract-lab.py
```

`writ-manage-fixture init-contracts CONTRACT_DB` initializes the separate owner store. `writ-manage-fixture v1-contracts MANAGEMENT_DB CREDENTIAL_FILE CONTRACT_DB` sends the standard v1 envelope to the same service as an embedder. The scope in the lab is tenant `m6-lab`, project `offline-proof`. The owner recognizes `tenant:m6-lab:<slug>` and `pack:sample-pack:<slug>`. Each store binds exactly one tenant and project. Pack membership comes from owner configuration. Core names and normalized aliases remain reserved in every namespace. Core browsing catalogs source schemas under the read-only fixture version `1.0.0`. Inspect displays the source schema with its original identifiers and field descriptions.

| `/manage/v1` operation | Fields in addition to the envelope |
|---|---|
| `contracts.browse` | None |
| `contracts.inspect` | `contract_id` |
| `contracts.validate-schema` | `schema` |
| `contracts.validate-example` | `contract_id`, `contract_version`, `example` |
| `contracts.create` | `contract_id`, `contract_version`, `schema`, `expected_revision`, `idempotency_key` |
| `contracts.preview` | `change` (`publish`, `deprecate` or `retire`), `contract_id`, `contract_version`, `expected_revision`. For deprecation include `replacement_version` and UTC `end_date` (`YYYY-MM-DD`). |
| `contracts.publish`, `contracts.deprecate`, `contracts.retire` | Corresponding preview fields except `change`, plus `intent_digest`, `presentation` and `idempotency_key`. Deprecation and retirement also require the exact previewed `confirmation`. |
| `contracts.recover` | Original `idempotency_key` |

A human Policy Author creates a validated draft. Each version stores immutable schema bytes from creation forward. A human Publisher previews the proposed change without writing either store. Send the full successful preview to `writ-contract-authority-fixture STEP_UP_FILE CEREMONY_ID`. The fixture step-up credential is the exact file content `fixture-contract-publisher-step-up`. The separate authority emits a presentation for the contract purpose and exact intent. The same author and Publisher need the fixture's explicit `acknowledge_self_publication: true` on publish preview and commit. Type the previewed destructive confirmation as `deprecate <id>@<version>` or `retire <id>@<version>`.

Each successful mutation advances the scope revision and writes its audit, idempotency binding and authoritative receipt in the owning SQLite transaction. Drafts, published, deprecated and retired versions are visible in history. The owner reports active fixture pack, policy and integration references, plus live links from deprecated versions. Retirement requires complete owner inventory, an available replacement and the recorded UTC end date to have arrived. Pass the original body and key to replay or use `contracts.recover` under the original subject to retrieve the committed receipt after an uncertain result. Changed replay bodies are refused. The management database and registry database have independent transactions. If an interrupted process leaves a hot owner journal, run the host-only `writ-manage-fixture recover-contract-storage CONTRACT_DB` before retrying inspection or receipt recovery. Ordinary contract preview opens both stores read-only.

### Validation profile

Custom schemas must explicitly declare `https://json-schema.org/draft/2020-12/schema`. The profile supports `type`, `properties`, `required`, boolean `additionalProperties`, schema-valued `items`, `minLength`, `maxLength`, `minimum`, `maximum`, `minItems`, `maxItems`, `enum` and `const`, plus `title` and `description` annotations. It accepts sibling-free `$ref` entries to exact named root `$defs` in the submitted schema. Definitions are a closed catalog. The validator rejects nested definitions, unresolved names, cycles and every other keyword, including patterns, format, applicator combinations and dynamic refs. It performs no network or path retrieval. The existing core schemas keep their own Draft 2020-12 assertions, with formats treated as annotations as in the repository validator.

Writ's canonical JSON restricts numeric input to safe integers. Schemas and examples are each limited to 16 KiB, 32 path levels and 2,048 JSON values. The local catalog allows 32 definitions. Expansion allows 256 schema nodes and 40 KiB of normalized JSON, within the document traversal limits. Example diagnostics are sorted by RFC 6901 pointer and code. Missing fields and extra properties name the affected path. More than 32 unique errors returns `DIAGNOSTIC_LIMIT`. An invalid or over-limit document never produces a successful validation result. The pinned validator runs in an isolated Python child with a cleared environment, three CPU seconds and 256 MiB of address space. Its absolute executable and package environment are owner configuration. Missing or failed tooling refuses validation.

### Compatibility profile

The owner compares with the highest previously published version of the same contract, including historical deprecations and retirements. Publication versions must increase. The comparison expands the bounded catalog and ignores description and title changes. Field removal, newly required fields, narrower types, tighter bounds, newly constrained array items or open-object fields, and closing an open object are breaking. Optional additions to an already closed object and proven widenings of existing constraints are compatible. Changed `enum` and `const` assertions are outside the comparison profile and are refused regardless of the version. A known breaking change requires a higher major version. The preview and authoritative publication receipt report the comparison. Comparisons that would exceed the 16 KiB result budget are refused before preparation completes. Custom contracts do not participate in kernel Decisions, permission grants, evaluator enrollment or runtime enforcement.

## Run Capability Pack administration

[D-059](../../docs/implementation/DECISION-LOG.md#d-059-m6-slice-5f-headless-capability-pack-administration-owner-2026-11-19) provisions only disposable stores and test authorities. With the pinned toolchain and `WRIT_CONTRACT_PYTHON` set as above, run the scenario from the repository root.

```sh
cargo test --locked --manifest-path m6/management/Cargo.toml --features fixtures \
  --test pack_workflow complete_pack_lifecycle -- --exact --nocapture
```

The scenario publishes a custom contract and a scoped evaluator through their owners. It signs, installs and verifies two immutable pack versions, enables version 1, upgrades to version 2, disables the pack and recovers the original receipts after reopening. The contract owner refuses retirement while either version is selected and accepts the same authorized retirement after disable. The scenario compares complete kernel Decisions across all four selection states using the golden and permit, approval and deny corpora.

- `writ-manage-fixture init-packs CONTRACT_DB EVALUATOR_DB` provisions the pack-enabled D-058 store with the identity of an existing disposable D-052 evaluator ledger. Use a management store created by the current fixture so the human intent can bind its durable identity. `writ-manage-fixture v1-packs MANAGEMENT_DB CREDENTIAL_FILE CONTRACT_DB EVALUATOR_DB` invokes the same service as an embedder using `Service::with_packs`.
- `writ-pack-authority-fixture sign SIGNER_CREDENTIAL_FILE` takes the canonical payload described by the [pack envelope](../../docs/contracts/management/pack-envelope.schema.json). It emits a pack-purpose Ed25519 signature. The fixture signer credential is the exact file content `fixture-pack-build-signer`. The trusted verifier is scoped to the configured tenant, project and permitted pack IDs.
- `writ-pack-authority-fixture approve STEP_UP_FILE CEREMONY_ID` takes a complete successful preview response. The step-up credential is the exact file content `fixture-pack-publisher-step-up`. It emits a presentation under the distinct human pack lifecycle purpose. The fixture uses a fixed test clock and separate keys for the two purposes.
- The manifest uses the existing CapabilityPack schema. This profile recognizes exact core compatibility `2.0`, local artifact references of the form `id@version`, external-host review workflow declarations, `fs.read` as a symbolic requirement, and ephemeral memory with empty read and write types. It refuses blocking evaluator declarations. Exact D-052 rubric bindings and authenticated evaluator and key state are checked at verification and commit. The selected metadata gives no permission, trust registration, effect, approval, signoff, evidence, waiver, tool access or memory access.

| Operation | Fields in addition to the v1 envelope |
|---|---|
| `packs.verify`, `packs.inspect` | `pack_id`, `pack_version` |
| `packs.history` | `pack_id` |
| `packs.preview` | `pack_operation`, `pack_id`, `pack_version`, `expected_revision`, plus `envelope` for install |
| `packs.install`, `packs.enable`, `packs.disable`, `packs.upgrade` | Preview fields except `pack_operation`, plus `intent_digest`, `presentation` and `idempotency_key`. Disable and upgrade also require the exact previewed `confirmation`. |
| `packs.recover` | Original `idempotency_key` |

Preview reserves neither an idempotency key nor a ceremony. The pack write path reserves the evaluator writer transaction first, then the D-058 owner transaction. Contract retirement consults active pack dependencies in the same owner transaction as its retirement audit. Disable can remove an active dependency even if the pack publisher or evaluator has since been revoked. The pack selection, immutable version and audit use one owner transaction. A separate management transaction records the attempt and consumes the presentation for dispatch. The human intent binds both stores so another management database or a direct owner call cannot replay a pending attempt. Exact replay or recover reads the original receipt. An attempt with durable management dispatch identity but no owner receipt returns `UNAVAILABLE` and is never redispatched. Before starting a fresh attempt, check history and obtain a new preview, key and human presentation. After process interruption, the owner can run the host-only `recover-management-storage MANAGEMENT_DB` and `recover-contract-storage CONTRACT_DB` commands before inspecting or recovering the attempt. The normal preview and recovery queries do not repair journals.
