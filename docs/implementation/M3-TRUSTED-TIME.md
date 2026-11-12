# Trusted time for the broker (risk 14): revised design for owner review

**Status:** REVISION 2 (2026-11-12), direction approved by the owner (D-033 item 3), mechanism revised per the owner's corrections; **not implemented**. M3-02 provides only a `TrustedClock` interface with an injected test implementation. ADR-0008 APPROVED IN PRINCIPLE; release and production NO-GO.

**Requirement:** the agent never supplies authoritative time; no clock fault (rollback, forward jump, skew, suspend, restart, owner change) may extend the lifetime of a grant, permit, deadline, reservation or acceptance record. Where trustworthy expiry cannot be established, time-sensitive operations fail closed.

## Why revision 1 was insufficient

`effective_now = max(clock_now, high_water)` freezes effective time while the wall clock is behind the watermark: during a rollback every lifetime stops advancing, which **extends** it. Comparing a forward jump only against the persisted watermark also misreads ordinary downtime (a broker stopped for two days) as a jump.

## Mechanism

1. **Effective time comes from elapsed time, not from the wall clock.** At start the broker fixes a base `B` (rule 3) and a monotonic anchor; while it runs, `effective_now = B + elapsed`, where `elapsed` is read from a monotonic clock that **keeps counting during system suspend** (Linux `CLOCK_BOOTTIME`). Lifetimes therefore keep running through wall-clock rollbacks and through suspend; the wall clock never moves effective time backward or forward while the broker runs.
2. **The wall clock is only a detector while running:** `drift = wall_now - effective_now`. `drift < -120 s` is a rollback diagnostic; `drift > +24 h` is a forward-jump diagnostic (measured against the elapsed-time baseline, not the watermark, so downtime is never involved while running). Diagnostics are operator records; neither changes `effective_now`, so a detected rollback cannot pause or extend anything. 120 s and 24 h are diagnostic thresholds, never allowances.
3. **Startup and restart** (no elapsed baseline exists across downtime): read the persisted high-water mark `H` and the wall clock `W0`.
   - `W0 >= H`: `B = W0` (downtime of any length is normal; a wall clock that ran ahead during downtime only expires things earlier, fail closed).
   - `H - 120 s <= W0 < H`: `B = H` (small rollback across downtime absorbed; lifetimes then run from `H`).
   - `W0 < H - 120 s`: **fail closed** for every time-sensitive operation until an authenticated, authorized, audited operator acknowledgement; the acknowledgement starts the broker with `B = H` (never lower), so it cannot revive anything that had expired at `H`.
4. **Persisted floor:** the effective time used to decide an expiry (or any time-sensitive refusal) is written to the high-water mark in its **own fenced transaction before** the governed operation's result is final, so it persists even when the governed operation is refused or rolled back. The mark is also written periodically while running and is never lowered (fenced, owner-checked, D-029 and M3-01 ownership).
5. **Owner changes:** a new owner reads the same persisted `H` (single-owner ledger), so rule 3 applies unchanged.
6. **Acknowledgement:** an operator action through an authenticated, authorized channel (not the agent, not a model, not a request field), recorded in the audit log; it can only allow starting from `H`; it never lowers `H`, never revives expired or revoked credentials, and there is no silent reset.

## Residual risk (documented, not solved here)

Across downtime there is no elapsed baseline: if the host clock is moved backward during downtime by less than the downtime itself, the restart sees `W0 >= H` and cannot detect it, so lifetimes that should have ended during downtime can appear to end later than intended by up to that amount. Security therefore still depends on host-clock integrity across downtime (trusted time synchronization, agent unable to set the clock); real-host clock security is M5 and is not resolved by this design.

## Decision needed before implementation

**Suspend-aware monotonic clock.** Rule 1 needs `CLOCK_BOOTTIME`. `std::time::Instant` on Linux is not documented to count suspend and has changed clock sources across releases, so it cannot be relied on. Options:

1. add `rustix` (safe `clock_gettime(ClockId::Boottime)`) to the broker under the dependency policy (review of version, licence, transitive closure);
2. use `libc` (already locked for other targets) with a documented, single-function exception to `unsafe_code = "forbid"`;
3. treat suspend as unsupported: detect it (`wall` advanced far more than `elapsed`) and fail closed until acknowledgement.

Recommendation: option 1, with option 3's detection kept as a defence.

## Tests (planned)

Rollback while running does not pause or extend a grant, permit or reservation lifetime; expiry refusal persists the floor, then clock rollback, then restart, refuses the same item; owner change after a rollback; startup with `W0 < H - 120 s` fails closed and an acknowledgement starts from `H` without reviving expired items; long normal downtime is accepted; forward jump while running is reported and does not change effective time; agent-supplied time never used; acknowledgement unauthenticated or by an agent refused.
