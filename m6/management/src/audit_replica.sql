-- Read model supplied by a trusted owner. Sequence numbers are scoped,
-- positive, immutable and assigned in causal order by the source.
CREATE TABLE audit_events (
    tenant TEXT NOT NULL,
    project TEXT NOT NULL,
    sequence INTEGER NOT NULL CHECK (sequence BETWEEN 1 AND 9007199254740991),
    event_id TEXT NOT NULL,
    event_type TEXT NOT NULL,
    occurred_at TEXT NOT NULL,
    trace_id TEXT,
    request_id TEXT,
    principal_id TEXT,
    action_kind TEXT,
    status TEXT,
    policy_version TEXT,
    payload TEXT NOT NULL,
    PRIMARY KEY (tenant, project, sequence),
    UNIQUE (tenant, project, event_id)
) STRICT, WITHOUT ROWID;
CREATE INDEX audit_by_trace ON audit_events(tenant, project, trace_id, sequence);
CREATE INDEX audit_by_request ON audit_events(tenant, project, request_id, sequence);
