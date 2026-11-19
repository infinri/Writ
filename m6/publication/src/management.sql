CREATE TABLE management_intents (
    sequence INTEGER PRIMARY KEY REFERENCES publication_audit(sequence),
    digest TEXT NOT NULL UNIQUE
) STRICT;
