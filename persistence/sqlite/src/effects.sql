-- Writ controlled effect adapter store, schema writ-effects-1 (M3-04).
-- The local test adapter's own store, separate from the broker ledger: the broker reaches
-- it only through the adapter interface (writ_broker::effects::EffectAdapter); tests read
-- it directly. Its effects are records, not side effects on an external system.
-- Applied once to a new database inside one transaction.

CREATE TABLE meta (
  key   TEXT PRIMARY KEY,
  value TEXT NOT NULL
) STRICT;

-- Every permit id the adapter accepted, recorded in the same transaction as its effect.
CREATE TABLE consumed_permits (
  permit_id TEXT PRIMARY KEY,
  result    TEXT NOT NULL CHECK (result IN ('applied', 'failed'))
) STRICT;

-- Applied effects: at most one per permit, never without a consumed permit.
CREATE TABLE effects (
  seq                 INTEGER PRIMARY KEY AUTOINCREMENT,
  permit_id           TEXT NOT NULL UNIQUE REFERENCES consumed_permits (permit_id),
  tenant_id           TEXT NOT NULL,
  idempotency_key     TEXT NOT NULL,
  request_fingerprint TEXT NOT NULL
) STRICT;

INSERT INTO meta (key, value) VALUES ('schema_version', 'writ-effects-1');
