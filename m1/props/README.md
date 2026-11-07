# M1-05 property, metamorphic, concurrency and differential harness

Task M1-05 (authorized by D-020). Python 3.12 standard library only. It drives the Rust and Go prototypes through the
same JSON Lines protocol as `m1/driver/parity.py` (`proto <command>`, `{"id","input"|"input_text"}` in,
`{"id","output"}` out). No oracle here calls or imports either prototype or the M0 harness. It is a test harness,
not a third implementation.

```text
python3 m1/props/run.py --campaign <name|all> --seed N --cases K --repeats R \
    --rust m1/rust/target/release/proto --go m1/go/proto --out report.json
python3 m1/props/run.py --profile ci --rust ... --go ... --out ci.json      # all campaigns, about 10 s
python3 m1/props/mutants/smoke.py --real <proto> --seed 7 --cases 600 --out smoke.json
python3 m1/driver/parity.py --proto <proto> --manifest m1/props/regressions/manifest.json
```

The exit status is 0 only when every check passes and there are no differential mismatches. The report records, for
each campaign: the seed and its derived RNG seed string (`"<seed>:<campaign>"`, `random.Random`), the number of cases,
pass and fail counts per invariant per prototype, up to 5 violating cases per invariant, mismatch clusters (grouped by
which output fields differ) with one example each, minimized findings, protocol problems, concurrency outcome
distributions and the sha256 of each prototype binary. Copy the binaries somewhere stable before a long campaign:
the report names the file it ran.

## Campaigns and oracles

Each oracle is taken from a spec passage. Where the spec does not fix a result, the input is either kept out of the
oracle (differential only) or listed under "Ambiguous" in the report. It is never decided by asking a prototype.

| Campaign | Command(s) | Generators | Oracles |
|---|---|---|---|
| `json` | `canonical` (plus the same text sent to `check-acceptance`, `transition-scenario`, `decide`, `broker-scenario`) | random values (astral, controls, U+2028, BOM-like characters, empty keys, numbers up to ±(2^53-1)) written with random key order, whitespace and escape spellings; one injected fault per case: duplicate keys at random depth (same or escape-equivalent spelling), alternate or out-of-range numbers (`1.0`, `1e0`, `-0`, `01`, `-01`, `+1`, 2^53, 2^64+k), NaN/Infinity literals, lone or reversed surrogates (in keys and values), chains of 58-67 nested arrays and objects with scalar, empty or member leaves, syntax errors, large inputs; malformed and large envelopes | D-006 canonical bytes of the generated value and sha256 digest; the same value in any spelling gives the same output; canonical output fed back is a fixed point; one fault gives its spec code; depth computed from the D-018 A1 rule; the same malformed text gives the same strict-parse code in every command and `invalid`/`REQUEST_JSON_INVALID` in `decide`; a bad envelope line does not stop the neighbouring lines from being answered |
| `decide` | `decide` | M0 reference bundle, M1 transition bundle and random bundles (statements with tenant, role, prefix, `from_states`/`to_states` conditions; random protected states and edges); clean requests plus 0 to 3 injected faults | evaluation order (schema, requested-capabilities redundancy, deadline, availability, bundle rejected at load, pin, issuer, state revision, capability knowledge, canonical form, scope, transition support, resource shape, attributes, composite, protection): status and reason of the earliest fault, no matched rules; bundle-load defects labelled by a small spec rule (missing or invalid protection, duplicate or self edges, unscoped permit, bypass route found by BFS); metamorphic: adding a matching forbid, removing covering statements, cross-tenant and cross-project resources (existing and nonexistent tenant give the same output), statement order, key order and whitespace, pinning the bundle's own sha, extra roles against a matched forbid, approval request id differs when `from_state` or `to_state` differs |
| `acceptance` | `check-acceptance` | random valid (record, current, requirements) triples; 0 to 4 independent mutations, each tagged with the check it alone fails | valid triple gives `ok`; one mutation of a bound field gives exactly its code; several give the earliest check (criteria by `criterion_id`, then sub-check); unbound fields (advisory criteria, record id when not consumed, criterion order, unrelated revocations, `issued_at == now`) keep `ok`; a bound mutation never gives `ok`; text spelling and key order never change the result |
| `refusal` | `external-refusal` | every pass-through and security code, near misses (case, whitespace, NUL, zero-width space, look-alike and full-width letters, prefixes, suffixes), random unknown codes, escaped text, malformed inputs | exactly the 12 pass-through codes pass; everything else is `PROMOTION_REFUSED`; an `external` value is never anything else |
| `transition` | `transition-scenario` | (a) one promote with 0 to 2 faults tagged by step; (b) random sequences of promote, retry, effect (with faults), reconcile, update_artifact, activate_policy, activate_authorization_bundle, revoke_evaluator, advance_time and small concurrent groups; (c) concurrency, below; probes | (a) exact result from the step order; (b) refusals counted, receipts = distinct committed keys, revision accounting, record never consumed twice, protected commit always had a record, replay repeats the original, uncertain effect never re-dispatched, final effects match observed dispatches; inserting a guaranteed refusal changes nothing else; key-order twins; probes must be answered without a crash |
| `broker` | `broker-scenario` | random sequences of approve (correct, wrong credential, untrusted channel, self, denied, TTL), execute (valid, absent, forged, foreign-adapter, unregistered and altered seals; grants; all faults), retries, present_permit (tampered), apply_without_permit, reconcile, restart_broker, advance_time, activate_bundle, set_dependency, concurrent groups; 1 to 3 principals and sessions; seals are HMAC-SHA256 under the fixture ingress key over the D-006 bytes (BROKER.md "Seal"); probes | per-operation codes independent of history (ingress, dependencies, deny class, approval required, human authentication, permit missing, permit presentation order); grant uses at most 1 and equal to first reservations under that grant; effects applied = dispatches <= consumed permits; receipts = reserved keys; refusals counted; uncertain effect never re-dispatched; restart_broker inserted anywhere changes nothing (D-020); inserted refusal changes nothing else |

## Concurrency

The prototypes run the operations of a `concurrent` group (and `promote_concurrent`) on real OS threads (Rust) or
goroutines (Go) inside one process. That in-process contention is real and is what the concurrency campaigns
exercise: groups of 16, 32 and 64 operations with shared single-use grants, shared or conflicting idempotency keys,
several principals and sessions, repeated `--repeats` times (default 10). The distribution of observed outcomes per
group is in the report.

Oracles never require one particular interleaving. They require that the observed outcome equals the outcome of
some serial order:

- `promote_concurrent`: exactly one commit, all others `REVISION_CONFLICT` (or all refused with the same code when
  the promote is invalid);
- symmetric promote groups and effect groups: closed form (the first serialized operation wins; others replay,
  conflict or are refused by compare-and-set; effect keys are independent);
- small mixed groups (2 to 5 operations): every serial order is run through the same prototype as an ordinary
  sequence, and the concurrent result must equal one of them (the serial results are also compared Rust against Go);
- broker groups: a small abstract model of BROKER.md execute steps 4 to 9 (fixed decision class, grants with
  `max_uses` 1, receipts by key) and an exhaustive memoized search over serial orders that must reproduce the
  observed outcome counts and final counters. The search is exact, not sampled; if its budget runs out the case is
  reported as undetermined, not as passed.

## Minimization and regressions

Differential mismatches are shrunk by delta debugging (character chunks for raw text; removal of members and
elements and subtree promotion for JSON values; strict-valid text is converted to a value with member order kept),
keeping a change only while the two prototypes still disagree on the same output fields. Minimized inputs are in the
report.

A case is written to `regressions/<campaign>.jsonl` (driver format, `hard_gate: false` in
`regressions/manifest.json`) only when its oracle gives an exact spec-derived expectation and the prototypes
disagree with exactly one of them matching it; the original input is written, because shrinking can change the
expected result. Two cases are hand-minimized from frozen fixtures by a semantics-preserving change (member order).
Everything else is a finding in the report, not a fixture. Nothing is written to `m1/fixtures/`.

## Mutation smoke test

`mutants/mutant.py` proxies to a real prototype and breaks one behavior (accepts duplicate keys, ignores the
artifact binding, double-counts grant uses, re-dispatches uncertain effects, passes every refusal code through,
allows cross-scope requests, ignores forbids, commits replayed records). `mutants/smoke.py` runs the relevant campaign
with the real binary as "rust" and the mutant as "go" and lists the oracles that failed on the mutant but not on the
real binary.

## Simulated and not tested

Tested here: the specified logic of each command, determinism, input-spelling independence, and in-process
concurrency (threads and goroutines on shared in-memory state).

Simulated by the prototypes and therefore NOT tested: persistent storage and its atomicity and durability; process
isolation (everything runs in one process); crash recovery of a real store (`fault` values and `restart_broker` are
in-memory simulations, D-020); real signatures and key management (seals and permit MACs are HMAC with fixture keys
in scenario state; no key rotation or registration); authentic evaluators and record issuance (boundary 2);
artifact storage and immutability (boundary 4); real host enforcement and adapters; trusted clocks (time is injected);
multi-process locking. No performance is measured; timings in reports are informational only.

## Layout

`run.py` CLI; `wprops/common.py` canonical form, seals, prototype I/O, bookkeeping; `wprops/c_*.py` one module per
campaign (generators and oracles); `wprops/minimize.py` shrinking and regression writing; `mutants/` mutation smoke
test; `regressions/` generated regression cases.
