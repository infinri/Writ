# V2 repository structure and ownership

**Status:** proposed target structure; not present yet. V1 files remain on the branch only as migration reference.

~~~text
/
├── Cargo.toml or go.mod          # SELECT AT M1, not both forever
├── contracts/
│   ├── schemas/                 # versioned Action, Decision, Event, Receipt
│   └── fixtures/                # canonical golden examples and fuzz seeds
├── core/                        # pure transition and decision kernel
├── policy/
│   ├── evaluator/               # Cedar or equivalent authorization adapter
│   ├── registry/                # immutable bundles, verification, conflict checks
│   └── bundles/                 # signed development-only policy fixtures
├── broker/                      # only governed action execution boundary
├── adapters/
│   ├── claude-code/             # hook bridge, no business/policy copies
│   ├── mcp/                     # host tools proxy, conditional support
│   ├── cli/                     # trusted operator transport
│   └── sdk/                     # API clients with explicit identity
├── intelligence/
│   ├── retrieval/               # BM25/vector ranker and abstention
│   ├── context/                 # bounded graph evidence snapshots
│   └── recall/                  # scoped durable memory and provenance
├── graph/
│   ├── schema/                  # Cypher migrations and uniqueness constraints
│   └── neo4j/                  # replaceable GraphPort adapter
├── persistence/
│   ├── sqlite/                  # local transactional ledger
│   └── postgres/                # enterprise transactional ledger
├── orchestration/               # optional remote durable workflows
├── extensions/                  # optional sandboxed plugins
├── telemetry/                   # OpenTelemetry + audit verification
├── management/                  # Control Center management service and API (outside the decision path, ADR-0007)
├── control-center/              # optional web UI over the management API
├── cmd/                         # local agent, operator CLI, server
├── deployments/                 # local install and enterprise manifests
├── tests/
│   ├── contracts/               # cross-runtime behavior
│   ├── threat/                  # forgery, bypass, isolation, TOCTOU
│   ├── integration/             # real host and store adapters
│   ├── chaos/                   # crash, outage, replay
│   └── performance/             # reproducible workload harness
├── docs/                        # normative Markdown and derived HTML
└── legacy/                      # optional tracked V1 reference extraction later
~~~

## Import rules

Core imports contracts and pure domain types only. Policy evaluator imports contracts plus Cedar adapter. Broker imports contracts/core policy interface and persistence port; it does not reach into prompt retrieval. Graph and retrieval may import read-only domain types but not broker internals. Adapters translate events into contracts; they do not implement a second state machine. Core, policy evaluator and broker never import `management/` or `control-center/`; the management service talks to the registry, approval service and read-only stores through their APIs (ADR-0007).

A module may implement ports in a separate package, not use package-level mutable globals to impersonate dependency injection. No single 3,000-line CLI module or 200-KB shell enforcement script. Keep files cohesive, not arbitrarily below a fixed line threshold.

## Existing files and branch state

For now, top-level writ/, hooks/, bin/, agents/, rules/, legacy V1 CI and tests are still present. The initial docs-only commit is intentionally **not** a V2 runtime release. M0 will produce a porting/disposition map for each V1 component. M2+ builds clean packages; M12 removes/deprecates V1 paths after verified cutover. Avoid moving thousands of files only for cosmetic layout before semantic contracts exist.

## Generated artifact rule

The architecture website is a static projection of Markdown source. In M2, generate the pages from a single manifest to prevent drift. Until that generator exists, reviewers must update both in the same PR, and CI should validate page/link sets. No production data or old V1 graph-data.js payload belongs in these pages.

## Domain-independent additions

~~~text
adapters/models/             # managed-agent provider clients only (optional)
agents/runner/                # bounded generic task loop; not a security authority
packs/registry/              # signed manifest verification, dependency constraints
packs/examples/              # reference coding, research, review packs
artifacts/                   # typed outputs, provenance and versioned schemas
evaluation/                  # mechanical, evidence, model-assisted, human evaluators
contracts/task-and-model/     # provider-neutral TaskSpec/ModelEvent/Evidence/Artifact
~~~

These describe target folders, not current source. An externally managed Claude Code integration must not import AgentRunner or a model client. The kernel must not import a pack, model provider or coding-specific module. See [model](model-agnostic-runtime.md), [packs](capability-packs.md) and [M0](../implementation/M0-START-HERE.md).
