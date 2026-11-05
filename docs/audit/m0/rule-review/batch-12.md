# Rule review batch-12: security (25 rules)

Source: `writ-corpus.cypher` at `ebdafe5450b201339f81e717ed104e6bcfd3fe3e`. Proposed dispositions are the automated first pass (D-004), not verdicts. Record decisions in [batch-12.csv](batch-12.csv).

## ENF-SEC-001

- **Domain / severity / mandatory:** security / critical / true
- **Statement:** Every endpoint must have a written Access Boundary Declaration presented and approved before implementation code is produced. The declaration must answer four questions: who can call it, how the caller is authenticated, what data ownership rules apply, and what happens when unauthorized.
- **Trigger:** When generating code for any externally accessible endpoint (REST route, GraphQL resolver, admin controller, storefront controller, CLI command).
- **V1 enforcement text:** ENF-GATE-007 test skeletons must include unauthorized caller test, ownership violation test, and valid caller test. > **Framework-specific guidance**: See `bible/frameworks/magento/runtime-constraints.md` for Magento 2 patterns (`webapi.xml`, `$context->getUserId()`, `_isAllowed()`, `GraphQlAuthorizationException`).
- **V1 mechanical path:** bin/run-analysis.sh:78 (PHPStan ownership check) (resolves at baseline: yes)
- **Rule id referenced in V1 code:** 0 file(s)
- **Rule id referenced in V1 tests:** 3 file(s)
- **Related rules (conflict candidates):** ENF-GATE-007, FW-M2-RT-004, PY-PYDANTIC-001
- **Proposed disposition:** candidate_enforced (V1 binding resolves at baseline; must be rebound to a V2 validator); pack affinity: software-engineering
- **Reviewer verdict:** _not reviewed_

## SEC-AUTH-BRUTE-001

- **Domain / severity / mandatory:** security / high / false
- **Statement:** Failed authentication attempts must be rate-limited per account and per source IP. Acceptable mechanisms: progressive lockout (lock for N minutes after K fails), exponential backoff, CAPTCHA after threshold. The limit applies even when the username does not exist.
- **Trigger:** When implementing any authentication endpoint (login, password verify, MFA verify, API token issuance).
- **V1 enforcement text:** Code review. Look for auth endpoints without rate-limit decorators or middleware. Frameworks: Flask-Limiter, django-ratelimit, express-rate-limit.
- **V1 mechanical path:** none
- **Rule id referenced in V1 code:** 0 file(s)
- **Rule id referenced in V1 tests:** 1 file(s)
- **Related rules (conflict candidates):** SEC-AUTH-ENUM-001, SEC-RATE-LOGIN-001
- **Proposed disposition:** advisory (guidance only; no enforcement claim); pack affinity: software-engineering
- **Reviewer verdict:** _not reviewed_

## SEC-AUTH-ENUM-001

- **Domain / severity / mandatory:** security / medium / false
- **Statement:** Login and password-reset endpoints must return identical responses (same status, same body, same timing) for valid and invalid accounts. Discoverability of registered usernames or emails is a violation.
- **Trigger:** When implementing login, password reset, signup, or any endpoint that distinguishes between 'this account exists' and 'this account does not exist'.
- **V1 enforcement text:** Code review. Look for branched error messages or distinct status codes on auth failure paths.
- **V1 mechanical path:** none
- **Rule id referenced in V1 code:** 0 file(s)
- **Rule id referenced in V1 tests:** 2 file(s)
- **Related rules (conflict candidates):** SEC-AUTH-BRUTE-001, SEC-AUTH-TIMING-001, SEC-RATE-LOGIN-001
- **Proposed disposition:** advisory (guidance only; no enforcement claim); pack affinity: software-engineering
- **Reviewer verdict:** _not reviewed_

## SEC-AUTH-HASH-001

- **Domain / severity / mandatory:** security / critical / true
- **Statement:** Passwords must be hashed with bcrypt, argon2, or scrypt. Single-pass MD5, SHA-1, SHA-256, or SHA-512 are never acceptable for password storage. The work factor must match current industry guidance (bcrypt cost >= 12, argon2id memory >= 64 MiB).
- **Trigger:** When persisting user passwords, password-equivalent secrets, or any credential that must withstand offline cracking.
- **V1 enforcement text:** Mechanically enforced by bin/run-analysis.sh::analyze_security_auth_authz: regex flags hashlib.md5/sha1/sha256/sha512 (Python), md5()/sha1() (PHP), crypto.createHash('md5'|'sha1'|'sha256') (Node) in proximity to the identifier 'password'/'passwd'/'pwd' or assignment to a *.password field.
- **V1 mechanical path:** bin/run-analysis.sh::analyze_security_auth_authz (resolves at baseline: yes)
- **Rule id referenced in V1 code:** 1 file(s): `bin/lib/analyzers-regex.sh`
- **Rule id referenced in V1 tests:** 2 file(s)
- **Related rules (conflict candidates):** SEC-AUTH-HASH-002
- **Proposed disposition:** candidate_enforced (V1 binding resolves at baseline; must be rebound to a V2 validator); pack affinity: software-engineering
- **Reviewer verdict:** _not reviewed_

## SEC-AUTH-HASH-002

- **Domain / severity / mandatory:** security / high / false
- **Statement:** Salts must be unique per user and generated by the hashing library's gensalt helper. Never reuse a global salt, derive the salt from the username, or hand-roll salt generation from a non-CSPRNG.
- **Trigger:** When generating or storing the salt that accompanies a password hash.
- **V1 enforcement text:** Code review. Static checks for module-level salt constants or salt = '<literal>' patterns help.
- **V1 mechanical path:** none
- **Rule id referenced in V1 code:** 0 file(s)
- **Rule id referenced in V1 tests:** 2 file(s)
- **Related rules (conflict candidates):** SEC-AUTH-HASH-001, SEC-AUTH-TOKEN-001, SEC-CRYPTO-RAND-001
- **Proposed disposition:** advisory (guidance only; no enforcement claim); pack affinity: software-engineering
- **Reviewer verdict:** _not reviewed_

## SEC-AUTH-LOGOUT-001

- **Domain / severity / mandatory:** security / medium / false
- **Statement:** Logout must invalidate the server-side session: delete the session row, revoke the JWT (if a denylist is maintained), or rotate the session key. Clearing the cookie client-side without server-side invalidation is a violation.
- **Trigger:** When implementing logout for any session-bearing authentication system.
- **V1 enforcement text:** Code review.
- **V1 mechanical path:** none
- **Rule id referenced in V1 code:** 0 file(s)
- **Rule id referenced in V1 tests:** 2 file(s)
- **Related rules (conflict candidates):** SCALE-STATELESS-001, SEC-AUTH-RESET-001, SEC-AUTH-TOKEN-002
- **Proposed disposition:** advisory (guidance only; no enforcement claim); pack affinity: software-engineering
- **Reviewer verdict:** _not reviewed_

## SEC-AUTH-MFA-001

- **Domain / severity / mandatory:** security / medium / false
- **Statement:** MFA shared secrets must be encrypted at rest (database-level or field-level), never stored in plaintext. TOTP verification accepts only the current 30-second period plus exactly one adjacent period for clock skew. Wider windows are violations.
- **Trigger:** When implementing TOTP-based multi-factor authentication.
- **V1 enforcement text:** Schema review (encrypted column type), code review (valid_window argument).
- **V1 mechanical path:** none
- **Rule id referenced in V1 code:** 0 file(s)
- **Rule id referenced in V1 tests:** 3 file(s)
- **Related rules (conflict candidates):** SEC-AUTHZ-PRIV-001, SEC-DATA-ENCRYPT-001
- **Proposed disposition:** advisory (guidance only; no enforcement claim); pack affinity: software-engineering
- **Reviewer verdict:** _not reviewed_

## SEC-AUTH-RESET-001

- **Domain / severity / mandatory:** security / high / false
- **Statement:** Reset tokens must be single-use (invalidated on first successful use), time-limited to one hour or less, and invalidated when the underlying credential changes by any other path. Tokens are CSPRNG-generated per SEC-AUTH-TOKEN-001.
- **Trigger:** When implementing password reset, email change confirmation, or any single-use token-bearing link sent to a user.
- **V1 enforcement text:** Schema review (expiry column, consumed flag), code review (consumption logic).
- **V1 mechanical path:** none
- **Rule id referenced in V1 code:** 0 file(s)
- **Rule id referenced in V1 tests:** 0 file(s)
- **Related rules (conflict candidates):** SEC-AUTH-LOGOUT-001, SEC-AUTH-TOKEN-001, SEC-HDR-REFERRER-001
- **Proposed disposition:** advisory (guidance only; no enforcement claim); pack affinity: software-engineering
- **Reviewer verdict:** _not reviewed_

## SEC-AUTH-TIMING-001

- **Domain / severity / mandatory:** security / high / false
- **Statement:** Secret comparisons must use constant-time helpers: Python `hmac.compare_digest`, Node `crypto.timingSafeEqual`, PHP `hash_equals`, Java `MessageDigest.isEqual`. Equality operators (`==`, `===`) leak timing information.
- **Trigger:** When comparing two byte/string values where one side is attacker-influenced and the other is a secret (session token, API key, password hash, HMAC tag, reset token).
- **V1 enforcement text:** Code review. Grep for ` == ` or ` === ` adjacent to identifiers containing 'token', 'secret', 'hmac', 'signature', 'mac', 'tag'.
- **V1 mechanical path:** none
- **Rule id referenced in V1 code:** 0 file(s)
- **Rule id referenced in V1 tests:** 2 file(s)
- **Related rules (conflict candidates):** SEC-AUTH-ENUM-001, SEC-AUTH-TOKEN-001
- **Proposed disposition:** advisory (guidance only; no enforcement claim); pack affinity: software-engineering
- **Reviewer verdict:** _not reviewed_

## SEC-AUTH-TOKEN-001

- **Domain / severity / mandatory:** security / critical / true
- **Statement:** Security tokens must be produced by a cryptographically secure pseudo-random number generator: Python `secrets`, Node `crypto.randomBytes`, PHP `random_bytes`, Java `SecureRandom`. The token must be at least 128 bits of entropy (16 random bytes hex-encoded or 22 base64url characters).
- **Trigger:** When generating session tokens, password-reset tokens, email-verification tokens, API keys, OAuth state values, CSRF tokens, or any other security-sensitive random value.
- **V1 enforcement text:** Mechanically enforced by bin/run-analysis.sh::analyze_security_auth_authz: regex flags Math.random(), random.random(), random.choices(), random.randint(), random.choice(), rand(), mt_rand(), srand() within proximity to identifiers 'token', 'session', 'secret', 'csrf', 'key', 'nonce', 'reset', 'verify', 'api_key', 'sessid', 'salt'.
- **V1 mechanical path:** bin/run-analysis.sh::analyze_security_auth_authz (resolves at baseline: yes)
- **Rule id referenced in V1 code:** 1 file(s): `bin/lib/analyzers-regex.sh`
- **Rule id referenced in V1 tests:** 2 file(s)
- **Related rules (conflict candidates):** SEC-AUTH-HASH-002, SEC-AUTH-RESET-001, SEC-AUTH-TIMING-001, SEC-AUTH-TOKEN-002, SEC-CRYPTO-RAND-001, SEC-INJ-CSRF-001
- **Proposed disposition:** candidate_enforced (V1 binding resolves at baseline; must be rebound to a V2 validator); pack affinity: software-engineering
- **Reviewer verdict:** _not reviewed_

## SEC-AUTH-TOKEN-002

- **Domain / severity / mandatory:** security / high / false
- **Statement:** Session cookies must be set with Secure (HTTPS only), HttpOnly (no JS access), and SameSite=Lax or Strict. Authorization-header bearer tokens are transmitted only to HTTPS endpoints. Plain HTTP transmission of any session credential is forbidden.
- **Trigger:** When issuing session tokens (cookies, Authorization headers) from the application server.
- **V1 enforcement text:** Framework config review (Django SESSION_COOKIE_SECURE/HTTPONLY/SAMESITE, Flask SESSION_COOKIE_SECURE, Express express-session cookie.secure/httpOnly/sameSite). Static analysis flags `set_cookie` without these flags.
- **V1 mechanical path:** none
- **Rule id referenced in V1 code:** 0 file(s)
- **Rule id referenced in V1 tests:** 0 file(s)
- **Related rules (conflict candidates):** SEC-AUTH-LOGOUT-001, SEC-AUTH-TOKEN-001, SEC-HDR-CORS-001, SEC-HDR-HSTS-001, SEC-INJ-CSRF-001
- **Proposed disposition:** advisory (guidance only; no enforcement claim); pack affinity: software-engineering
- **Reviewer verdict:** _not reviewed_

## SEC-AUTHZ-DEFAULT-001

- **Domain / severity / mandatory:** security / critical / true
- **Statement:** New endpoints default to deny: they must opt in to a permission policy. The framework or application middleware must reject requests to endpoints without an explicit policy. 'No decorator means public' is a violation.
- **Trigger:** When adding a new endpoint, GraphQL field, server action, or admin tool to the application.
- **V1 enforcement text:** Mechanically enforced by bin/run-analysis.sh::analyze_security_auth_authz: presence check rejects handlers without an authorization decorator or middleware-tag. Phase 1B implementation flags suspect handlers; full coverage requires framework-aware deny-by-default middleware.
- **V1 mechanical path:** bin/run-analysis.sh::analyze_security_auth_authz (resolves at baseline: yes)
- **Rule id referenced in V1 code:** 1 file(s): `bin/lib/analyzers-regex.sh`
- **Rule id referenced in V1 tests:** 2 file(s)
- **Related rules (conflict candidates):** SEC-AUTHZ-ENFORCE-001, SEC-AUTHZ-FUNC-001
- **Proposed disposition:** candidate_enforced (V1 binding resolves at baseline; must be rebound to a V2 validator); pack affinity: software-engineering
- **Reviewer verdict:** _not reviewed_

## SEC-AUTHZ-ENFORCE-001

- **Domain / severity / mandatory:** security / critical / true
- **Statement:** Every endpoint must declare an explicit authorization check: framework-native auth decorator, dependency, middleware, or in-function check before any business logic runs. 'Open by default' or 'forgot to add auth' is a violation. Public endpoints must be explicitly marked.
- **Trigger:** When defining any HTTP route, RPC handler, GraphQL resolver, or server action.
- **V1 enforcement text:** Mechanically enforced by bin/run-analysis.sh::analyze_security_auth_authz: presence check flags route definitions (@app.route, @app.get/post/put/delete, FastAPI @router.*, Django path()/url()) without an auth decorator or Depends(auth) call in the same handler. Frameworks: Flask-Login, django.contrib.auth, FastAPI Security().
- **V1 mechanical path:** bin/run-analysis.sh::analyze_security_auth_authz (resolves at baseline: yes)
- **Rule id referenced in V1 code:** 1 file(s): `bin/lib/analyzers-regex.sh`
- **Rule id referenced in V1 tests:** 2 file(s)
- **Related rules (conflict candidates):** SEC-AUTHZ-DEFAULT-001, SEC-AUTHZ-FUNC-001, SEC-AUTHZ-IDOR-001, SEC-AUTHZ-RBAC-001
- **Proposed disposition:** candidate_enforced (V1 binding resolves at baseline; must be rebound to a V2 validator); pack affinity: software-engineering
- **Reviewer verdict:** _not reviewed_

## SEC-AUTHZ-FUNC-001

- **Domain / severity / mandatory:** security / high / false
- **Statement:** Administrative endpoints must be separated by path prefix and middleware (e.g., `/admin/*` behind `require_admin` middleware), not only by UI visibility. Hiding admin controls in the client is not access control.
- **Trigger:** When implementing administrative functionality alongside regular user functionality in the same application.
- **V1 enforcement text:** Code review of route registration, middleware coverage.
- **V1 mechanical path:** none
- **Rule id referenced in V1 code:** 0 file(s)
- **Rule id referenced in V1 tests:** 0 file(s)
- **Related rules (conflict candidates):** SEC-AUTHZ-DEFAULT-001, SEC-AUTHZ-ENFORCE-001, SEC-AUTHZ-MASS-001, SEC-AUTHZ-RBAC-001
- **Proposed disposition:** advisory (guidance only; no enforcement claim); pack affinity: software-engineering
- **Reviewer verdict:** _not reviewed_

## SEC-AUTHZ-IDOR-001

- **Domain / severity / mandatory:** security / critical / true
- **Statement:** Object access must be verified against the authenticated user's permissions, not just the existence of the record. Loading `Order.find(params[:id])` without checking `order.user_id == current_user.id` (or equivalent permission predicate) is a violation. This applies to read, update, delete, and side-channel exposure (filenames, redirect targets).
- **Trigger:** When loading or mutating any record where the record ID is supplied by the request (URL path param, query string, body field).
- **V1 enforcement text:** Code review. Look for `.get(pk=...)`, `Model.find(...)`, `.objects.get(id=...)` where the ID source is request data and no follow-up ownership/permission check is present.
- **V1 mechanical path:** bin/run-analysis.sh::analyze_security_auth_authz (resolves at baseline: yes)
- **Rule id referenced in V1 code:** 0 file(s)
- **Rule id referenced in V1 tests:** 1 file(s)
- **Related rules (conflict candidates):** SEC-AUTHZ-ENFORCE-001, SEC-AUTHZ-RBAC-001, SEC-AUTHZ-TENANT-001
- **Proposed disposition:** candidate_enforced (V1 binding resolves at baseline; must be rebound to a V2 validator); pack affinity: software-engineering
- **Reviewer verdict:** _not reviewed_

## SEC-AUTHZ-MASS-001

- **Domain / severity / mandatory:** security / critical / true
- **Statement:** Model fields populated from user input must use an explicit allowlist (Pydantic schema with declared fields, Rails strong parameters `permit(...)`, Django ModelForm `fields = [...]`, ActiveRecord `params.require(...).permit(...)`). Passing the raw request body to a constructor or update call is a mass assignment violation: the client chooses which fields to set, including ones it should never touch.
- **Trigger:** When constructing or updating a model instance from a request body (REST POST/PATCH, GraphQL mutation, form submission).
- **V1 enforcement text:** Mechanically enforced by bin/run-analysis.sh::analyze_security_auth_authz: regex flags `Model(**request.json|request.body|req.body)`, `Object.assign(model, req.body)`, `update_attributes(params[`, `Model.create(**request.`. The detector encodes the common mass-assignment idioms across Python, Ruby, and Node.
- **V1 mechanical path:** bin/run-analysis.sh::analyze_security_auth_authz (resolves at baseline: yes)
- **Rule id referenced in V1 code:** 1 file(s): `bin/lib/analyzers-regex.sh`
- **Rule id referenced in V1 tests:** 2 file(s)
- **Related rules (conflict candidates):** SEC-AUTHZ-FUNC-001, SEC-AUTHZ-PRIV-001, SEC-DATA-PII-002, SEC-VAL-SERVER-001, SEC-VAL-TYPE-001
- **Proposed disposition:** candidate_enforced (V1 binding resolves at baseline; must be rebound to a V2 validator); pack affinity: software-engineering
- **Reviewer verdict:** _not reviewed_

## SEC-AUTHZ-PRIV-001

- **Domain / severity / mandatory:** security / high / false
- **Statement:** Privilege-escalation paths must require re-authentication: a fresh password prompt, MFA challenge, or 'sudo mode' session within the last few minutes. A long-lived session is not sufficient.
- **Trigger:** When implementing endpoints that change a user's role, grant admin status, modify another user's permissions, or alter security-relevant account state (email, MFA settings, recovery keys).
- **V1 enforcement text:** Code review. Look for role/permission mutations without reauth gating.
- **V1 mechanical path:** none
- **Rule id referenced in V1 code:** 0 file(s)
- **Rule id referenced in V1 tests:** 2 file(s)
- **Related rules (conflict candidates):** SEC-AUTH-MFA-001, SEC-AUTHZ-MASS-001
- **Proposed disposition:** advisory (guidance only; no enforcement claim); pack affinity: software-engineering
- **Reviewer verdict:** _not reviewed_

## SEC-AUTHZ-RBAC-001

- **Domain / severity / mandatory:** security / medium / false
- **Statement:** Role checks must happen at the service/controller layer, not only at the route layer. A function that performs a privileged action must reject callers who lack the role, even when invoked from internal code paths.
- **Trigger:** When the application has roles, permissions, or capabilities that gate behavior.
- **V1 enforcement text:** Code review. Look for privileged operations whose only guard is a route decorator.
- **V1 mechanical path:** none
- **Rule id referenced in V1 code:** 0 file(s)
- **Rule id referenced in V1 tests:** 2 file(s)
- **Related rules (conflict candidates):** SEC-AUTHZ-ENFORCE-001, SEC-AUTHZ-FUNC-001, SEC-AUTHZ-IDOR-001, SEC-AUTHZ-TENANT-001
- **Proposed disposition:** advisory (guidance only; no enforcement claim); pack affinity: software-engineering
- **Reviewer verdict:** _not reviewed_

## SEC-AUTHZ-SCOPE-001

- **Domain / severity / mandatory:** security / high / false
- **Statement:** Tokens must carry the minimum scopes required for their use case. Wildcard scopes (`*`, `admin`, `all`) are forbidden except for explicit administrative tooling. Scopes are enforced at the resource server, not assumed by the client.
- **Trigger:** When issuing API tokens, OAuth grants, machine-to-machine credentials, or service accounts.
- **V1 enforcement text:** Token issuance code review, scope-enforcement middleware review.
- **V1 mechanical path:** none
- **Rule id referenced in V1 code:** 0 file(s)
- **Rule id referenced in V1 tests:** 0 file(s)
- **Related rules (conflict candidates):** SEC-DATA-EXPORT-001
- **Proposed disposition:** advisory (guidance only; no enforcement claim); pack affinity: software-engineering
- **Reviewer verdict:** _not reviewed_

## SEC-AUTHZ-TENANT-001

- **Domain / severity / mandatory:** security / critical / false
- **Statement:** Multi-tenant queries must filter by tenant ID at the data layer (repository method, ORM scope, query helper), not only at the API layer. Tenant scoping enforced only in controllers leaks data through internal callers, background jobs, or any code path that bypasses the controller.
- **Trigger:** When the application has multiple tenants (organizations, workspaces, accounts) sharing the same database or storage.
- **V1 enforcement text:** Code review of repositories and ORM scopes. Static analysis can flag raw `Model.query.all()` outside of tenant-scoped wrappers.
- **V1 mechanical path:** none
- **Rule id referenced in V1 code:** 0 file(s)
- **Rule id referenced in V1 tests:** 1 file(s)
- **Related rules (conflict candidates):** DRY-QUERY-001, PERF-CACHE-001, PROC-PLAN-001, SEC-AUTHZ-IDOR-001, SEC-AUTHZ-RBAC-001
- **Proposed disposition:** advisory (guidance only; no enforcement claim); pack affinity: software-engineering
- **Reviewer verdict:** _not reviewed_

## SEC-CRYPTO-ALGO-001

- **Domain / severity / mandatory:** security / critical / false
- **Statement:** Symmetric encryption must use AES-256-GCM, ChaCha20-Poly1305, or AES-256-SIV. ECB mode is forbidden. DES, 3DES, RC4, Blowfish are forbidden. Modes without authentication (CBC, CTR alone) require an explicit HMAC.
- **Trigger:** When implementing symmetric encryption for data at rest, message authentication, or session-token sealing.
- **V1 enforcement text:** Code review. Static analysis flags AES.MODE_ECB, AES.MODE_CBC without an HMAC, DES, 3DES, RC4 usage.
- **V1 mechanical path:** none
- **Rule id referenced in V1 code:** 1 file(s): `bin/lib/analyzers-regex.sh`
- **Rule id referenced in V1 tests:** 1 file(s)
- **Related rules (conflict candidates):** SEC-CRYPTO-ALGO-002, SEC-CRYPTO-IV-001, SEC-DATA-ENCRYPT-001
- **Proposed disposition:** advisory (guidance only; no enforcement claim); pack affinity: software-engineering
- **Reviewer verdict:** _not reviewed_

## SEC-CRYPTO-ALGO-002

- **Domain / severity / mandatory:** security / high / false
- **Statement:** RSA keys are minimum 2048 bits; new keys should be 3072 or 4096 bits. EC keys use P-256 or stronger, or Ed25519. RSA-1024, DSA-1024, and short-curve EC keys are forbidden.
- **Trigger:** When generating asymmetric key pairs (RSA, EC, Ed25519) for signing, encryption, or TLS.
- **V1 enforcement text:** Code review of key-generation call sites. Audit of stored key material for legacy short-key remnants.
- **V1 mechanical path:** none
- **Rule id referenced in V1 code:** 0 file(s)
- **Rule id referenced in V1 tests:** 2 file(s)
- **Related rules (conflict candidates):** SEC-CRYPTO-ALGO-001, SEC-CRYPTO-TLS-001
- **Proposed disposition:** advisory (guidance only; no enforcement claim); pack affinity: software-engineering
- **Reviewer verdict:** _not reviewed_

## SEC-CRYPTO-CERT-001

- **Domain / severity / mandatory:** security / medium / false
- **Statement:** Certificate validation must not be disabled outside of clearly-scoped local development paths. `verify=False`, `rejectUnauthorized: false`, `InsecureSkipVerify: true`, `-k`/`--insecure` are violations in any code path that runs in production.
- **Trigger:** When configuring an HTTPS client (requests, axios, http.Client, curl, custom).
- **V1 enforcement text:** Static analysis. Linter rules: ruff S501 (Python), eslint-plugin-security (Node), gosec (Go), CodeQL (multi-language).
- **V1 mechanical path:** none
- **Rule id referenced in V1 code:** 1 file(s): `bin/lib/analyzers-regex.sh`
- **Rule id referenced in V1 tests:** 1 file(s)
- **Related rules (conflict candidates):** SEC-CRYPTO-TLS-001, SEC-INJ-SSRF-001
- **Proposed disposition:** advisory (guidance only; no enforcement claim); pack affinity: software-engineering
- **Reviewer verdict:** _not reviewed_

## SEC-CRYPTO-IV-001

- **Domain / severity / mandatory:** security / high / false
- **Statement:** IVs and nonces must be unique per encryption operation, generated by a CSPRNG, never reused with the same key. For GCM, nonce reuse with the same key is catastrophic (allows full plaintext recovery of both messages).
- **Trigger:** When performing symmetric encryption that requires an IV or nonce (AES-GCM, AES-CBC, ChaCha20-Poly1305).
- **V1 enforcement text:** Code review. Look for module-level IV/nonce constants or fixed-byte assignments.
- **V1 mechanical path:** none
- **Rule id referenced in V1 code:** 0 file(s)
- **Rule id referenced in V1 tests:** 1 file(s)
- **Related rules (conflict candidates):** SEC-CRYPTO-ALGO-001, SEC-CRYPTO-RAND-001
- **Proposed disposition:** advisory (guidance only; no enforcement claim); pack affinity: software-engineering
- **Reviewer verdict:** _not reviewed_

## SEC-CRYPTO-KEY-001

- **Domain / severity / mandatory:** security / critical / true
- **Statement:** No hardcoded secrets, API keys, passwords, or private keys may appear in source code or committed configuration files. Secrets are loaded at runtime from environment variables or a secret-management service.
- **Trigger:** When any string literal in source code matches the shape of a credential: API keys, passwords, private keys, OAuth client secrets, bearer tokens, database passwords, signing secrets.
- **V1 enforcement text:** Mechanically enforced by bin/run-analysis.sh::analyze_security_crypto_headers: regex flags string literals matching `sk_live_*`, `AKIA*`, `xoxb-*`, `ghp_*`, `pk-` patterns; identifier names containing 'api_key', 'secret', 'token', 'password' assigned to a string literal; PEM block headers in source. CI: git pre-commit secret scanning (gitleaks, truffleHog).
- **V1 mechanical path:** bin/run-analysis.sh::analyze_security_crypto_headers (resolves at baseline: yes)
- **Rule id referenced in V1 code:** 1 file(s): `bin/lib/analyzers-regex.sh`
- **Rule id referenced in V1 tests:** 3 file(s)
- **Related rules (conflict candidates):** PROC-ENV-001, SEC-CRYPTO-KEY-002
- **Proposed disposition:** candidate_enforced (V1 binding resolves at baseline; must be rebound to a V2 validator); pack affinity: software-engineering
- **Reviewer verdict:** _not reviewed_
