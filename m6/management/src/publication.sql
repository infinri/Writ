CREATE TABLE IF NOT EXISTS publication_intents (
    slot INTEGER PRIMARY KEY CHECK (slot BETWEEN 1 AND 8192),
    tenant TEXT NOT NULL,
    project TEXT NOT NULL,
    subject TEXT NOT NULL,
    key_digest TEXT NOT NULL,
    request_digest TEXT NOT NULL,
    intent_digest TEXT NOT NULL,
    context_digest TEXT NOT NULL,
    context TEXT NOT NULL,
    refusal TEXT CHECK (refusal IN ('conflict', 'authority', 'policy', 'request', 'not_found')),
    UNIQUE (tenant, project, subject, key_digest),
    UNIQUE (tenant, project, intent_digest)
) STRICT;
