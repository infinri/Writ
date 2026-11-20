CREATE TABLE quorum_promotions (
    tenant_id TEXT NOT NULL,
    project_id TEXT NOT NULL,
    workflow_id TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    record_id TEXT NOT NULL,
    attempt_id TEXT NOT NULL,
    member_ids TEXT NOT NULL,
    record_sha TEXT NOT NULL,
    member_digests TEXT NOT NULL,
    policy_sha TEXT NOT NULL,
    PRIMARY KEY (tenant_id, idempotency_key),
    UNIQUE (tenant_id, project_id, workflow_id, attempt_id)
) STRICT;
