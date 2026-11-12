# Trusted time for the broker (risk 14): design for owner review

**Status:** PROPOSED (2026-11-12); not implemented. M3-02 provides only a `TrustedClock` interface with an injected test implementation. ADR-0008 APPROVED IN PRINCIPLE; release and production NO-GO.

**Requirement (M0 sign-off risk 14, D-018 A5):** the agent never supplies authoritative time; a skewed or rolled-back clock must not extend a grant, permit, deadline, acceptance freshness or reservation.

## Proposal

1. **Source:** the broker host's realtime clock read only by the broker process (`TrustedClock::now`), truncated to whole seconds UTC (D-022 form). The agent has no way to set it in Governed Execution (real-host enforcement is M5); no time value from a request, scenario or model is ever used as `now`.
2. **Rollback protection with a persisted high-water mark:** the ledger keeps `meta.time_high_water` (strict UTC). Every fenced state-changing transaction that uses time reads it, computes `effective_now = max(clock_now, time_high_water)`, uses `effective_now` for every expiry and freshness comparison, and writes back `effective_now` when it is later. Time therefore never moves backward across operations, restarts or owners, so a rolled-back clock cannot make an expired grant, permit or record valid again. A clock that runs ahead only makes things expire earlier (fail closed).
3. **Detection:** if `clock_now < time_high_water - tolerance` (proposed tolerance 120 s), the broker records an operator diagnostic (`clock ROLLBACK`) and refuses time-sensitive operations with `LEDGER_UNAVAILABLE`-class unavailability until the clock catches up or an operator acknowledges; small rollbacks within tolerance are absorbed by `max`.
4. **Forward jumps:** a forward jump larger than a configured bound (proposed 24 h) relative to the high-water mark is also an operator diagnostic, because accepting it would permanently advance the high-water mark and expire everything; proposed behaviour: refuse time-sensitive operations until acknowledged.
5. **Scope:** single-owner local profile only (the high-water mark lives in the owned ledger). The PostgreSQL profile (M10) needs its own design.

## Tests (planned)

Rollback after a grant expired does not revive it (same process, after restart, after an owner change); forward-skewed clock expires early; rollback beyond tolerance refuses time-sensitive operations and is reported; the agent-supplied `now` is never used.

## Questions for the owner

1. Approve the host clock as the trusted source for the local profile.
2. Approve the persisted high-water mark with `max` semantics.
3. Tolerance (120 s) and forward-jump bound (24 h), and whether exceeding them should refuse until operator acknowledgement or only report.
