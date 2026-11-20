PRAGMA application_id=1464421681;
PRAGMA user_version=1;
CREATE TABLE principals (
 subject TEXT PRIMARY KEY NOT NULL,
 realm TEXT NOT NULL CHECK(realm IN ('management','management-service','agent','adapter')),
 active INTEGER NOT NULL CHECK(active IN (0,1))
) STRICT;
CREATE TABLE credentials (
 fingerprint TEXT PRIMARY KEY NOT NULL,
 subject TEXT NOT NULL REFERENCES principals(subject),
 issued INTEGER NOT NULL,
 expires INTEGER NOT NULL CHECK(expires > issued),
 revoked INTEGER NOT NULL DEFAULT 0 CHECK(revoked IN (0,1))
) STRICT;
CREATE TABLE scoped_grants (
 subject TEXT NOT NULL REFERENCES principals(subject), tenant TEXT NOT NULL, project TEXT NOT NULL,
 role TEXT NOT NULL CHECK(role IN ('viewer','policy-author','publisher','waiver-operator','security-auditor')),
 PRIMARY KEY(subject,tenant,project,role)
) STRICT;
CREATE TABLE credential_audit (
 seq INTEGER PRIMARY KEY, happened INTEGER NOT NULL, change TEXT NOT NULL,
 subject TEXT NOT NULL, fingerprint TEXT, tenant TEXT, project TEXT, role TEXT, realm TEXT, active INTEGER
) STRICT;
CREATE TRIGGER principal_added AFTER INSERT ON principals BEGIN
 INSERT INTO credential_audit(happened,change,subject,realm,active) VALUES(unixepoch(),'principal.created',NEW.subject,NEW.realm,NEW.active);
END;
CREATE TRIGGER principal_status AFTER UPDATE OF active ON principals BEGIN
 INSERT INTO credential_audit(happened,change,subject,realm,active) VALUES(unixepoch(),'principal.status',NEW.subject,NEW.realm,NEW.active);
END;
CREATE TRIGGER principal_identity BEFORE UPDATE OF subject,realm ON principals BEGIN SELECT RAISE(ABORT,'immutable principal'); END;
CREATE TRIGGER principal_remove BEFORE DELETE ON principals BEGIN SELECT RAISE(ABORT,'disable principal'); END;
CREATE TRIGGER credential_issued AFTER INSERT ON credentials BEGIN
 INSERT INTO credential_audit(happened,change,subject,fingerprint) VALUES(unixepoch(),'credential.issued',NEW.subject,NEW.fingerprint);
END;
CREATE TRIGGER credential_revoke BEFORE UPDATE ON credentials WHEN
 NEW.fingerprint!=OLD.fingerprint OR NEW.subject!=OLD.subject OR NEW.issued!=OLD.issued OR NEW.expires!=OLD.expires OR OLD.revoked!=0 OR NEW.revoked!=1
 BEGIN SELECT RAISE(ABORT,'immutable credential'); END;
CREATE TRIGGER credential_revoked AFTER UPDATE ON credentials BEGIN
 INSERT INTO credential_audit(happened,change,subject,fingerprint) VALUES(unixepoch(),'credential.revoked',NEW.subject,NEW.fingerprint);
END;
CREATE TRIGGER credential_remove BEFORE DELETE ON credentials BEGIN SELECT RAISE(ABORT,'revoke credential'); END;
CREATE TRIGGER grant_add AFTER INSERT ON scoped_grants BEGIN
 INSERT INTO credential_audit(happened,change,subject,tenant,project,role)
 VALUES(unixepoch(),'grant.added',NEW.subject,NEW.tenant,NEW.project,NEW.role);
END;
CREATE TRIGGER grant_remove AFTER DELETE ON scoped_grants BEGIN
 INSERT INTO credential_audit(happened,change,subject,tenant,project,role)
 VALUES(unixepoch(),'grant.removed',OLD.subject,OLD.tenant,OLD.project,OLD.role);
END;
CREATE TRIGGER grant_update BEFORE UPDATE ON scoped_grants BEGIN SELECT RAISE(ABORT,'replace grant'); END;
CREATE TRIGGER audit_update BEFORE UPDATE ON credential_audit BEGIN SELECT RAISE(ABORT,'immutable audit'); END;
CREATE TRIGGER audit_delete BEFORE DELETE ON credential_audit BEGIN SELECT RAISE(ABORT,'immutable audit'); END;

CREATE TRIGGER principal_unique BEFORE INSERT ON principals WHEN EXISTS(SELECT 1 FROM principals WHERE subject=NEW.subject)
 BEGIN SELECT RAISE(ABORT,'existing principal'); END;
CREATE TRIGGER credential_unique BEFORE INSERT ON credentials WHEN EXISTS(SELECT 1 FROM credentials WHERE fingerprint=NEW.fingerprint)
 BEGIN SELECT RAISE(ABORT,'existing fingerprint'); END;
