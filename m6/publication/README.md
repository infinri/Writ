# M6 disposable signed policy publication

[D-051](../../docs/implementation/DECISION-LOG.md#d-051-m6-slice-2-signed-policy-publication-and-selection-owner-2026-11-19) exercises validated publication, Ed25519 verification, coherent selection and explicit rollback in disposable SQLite stores. Run from the repository root with pinned Rust and D-023 SQLite.

```sh
m1/tools/install-sqlite.sh /tmp/writ-sqlite-download
. m1/tools/env.sh
m3/tools/run-with-clean-fds.sh cargo run --locked --manifest-path m6/publication/Cargo.toml --example disposable
m3/tools/run-with-clean-fds.sh cargo test --locked --manifest-path m6/publication/Cargo.toml
python3 -m unittest discover -s m6/publication/contracts
```

The demonstration starts in a new private temporary directory. It publishes and selects a validated pair, executes one controlled synthetic effect, publishes a pair with a hard denial, then rolls back with a fresh human fixture approval. Reopen verifies the selected pair using public keys. The previous permit stays stale after rollback, and audit shows publication versions 1, 2 and 1 at activation revisions 1, 2 and 3. The example cleans up its own directory on exit.

The lab uses an injected time source, simulated human publisher evidence and fixture keys. Commit and effect tests exercise current verification, exact grant binding, activation races, durable audit and atomic recovery. The example uses the real broker and the existing SQLite test effect adapter for the synthetic, non-transition action. A separate kernel test exercises acceptance-record binding. Current public-key state and monotonic trusted time remain responsibilities of the configured trust source across restart. Production ingress, activation, storage ownership, human publisher credentials and authorized effect execution remain subject to M5 and later gate review.

[Wire and lifecycle contract](CONTRACT.md) · [Dependency and licence review](DEPENDENCIES.md)

## Enforcement evidence in the disposable profile

The publication owner reads its verified activation and a sequence-indexed audit page in one database snapshot. The selected signed publication, compiler proof and current trust guard identify the rules in that activation. Each rule applies only within its signed resource prefix. The `synthetic.inspect` capability and controlled effect span several prefixes. Every completed effect consumes a permit. Only a matching approval rule also requires an authenticated grant. The approval fixture executes allowed requests on the broader capability alongside requests that must obtain a grant on the restricted prefix.

The broker result record in `fixtures/mediation-result.json` names the passing owner tests and the digest of their local source and toolchain manifest. The same registration is checked after human approval inside the final store transaction. A removed point, expired registration, revoked key, incompatible implementation or missing test evidence invalidates the corresponding claim. The fixture evidence window runs from November 19, 2026 through November 18, 2027 UTC. New source identities need a new passing test record. Build-time `sha256sum` reads only listed repository files.

The host-contract result record covers the schema-fixture host at fixture-1.0. The permission prompt observation identifies the Write workspace-file variant, fs.write capability and interceptor. The subagent_start coverage record reports no observation or prevention. The advisory rule comes from the exact DOC-ONBOARD-001 rule in the exported Writ corpus. Live-host enforcement requires separate M5 evidence.

For the immutable host-contract and corpus fixtures, “current” means that the exact fixture version, source hashes and required coverage or membership record still match. It makes no statement about elapsed time or a live host. A missing or changed link yields UNVERIFIED. For renewal of the time-limited broker record, rerun the named publication effect, grant, expiry and replay tests with the pinned toolchain against the new source manifest. Review their results before updating the seal, passed test record and validity window. The host fixture requires the named Rust portability and Python schema tests against its independently pinned sources.

The fixture CLI opens a read-only registry for lifecycle inspection. Within the inspection snapshot it checks the SQLite page budget (32 MiB) and probes each publication, audit and management-intent primary key for a 257th row before running integrity checks or replaying history. A larger registry returns UNAVAILABLE for this fixture view. Publication, rollback and ordinary administrative recovery retain their existing capacity. Once open, the view fetches at most 51 audit rows for a requested page of 50.
