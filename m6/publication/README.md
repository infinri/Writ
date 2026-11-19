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
