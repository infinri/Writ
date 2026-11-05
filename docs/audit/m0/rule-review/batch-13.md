# Rule review batch-13: security (25 rules)

Source: `writ-corpus.cypher` at `ebdafe5450b201339f81e717ed104e6bcfd3fe3e`. Proposed dispositions are the automated first pass (D-004), not verdicts. Record decisions in [batch-13.csv](batch-13.csv).

## SEC-CRYPTO-KEY-002

- **Domain / severity / mandatory:** security / high / false
- **Statement:** Secrets must be loaded from environment variables or a secret-management service (Vault, AWS Secrets Manager, GCP Secret Manager, Azure Key Vault). Loading from a config file committed to VCS is a violation, even when the file is in a 'secrets' directory.
- **Trigger:** When loading secrets, API keys, or credentials at application startup.
- **V1 enforcement text:** CI check that no config file matching common secret-file names is tracked in git. Code review.
- **V1 mechanical path:** none
- **Rule id referenced in V1 code:** 0 file(s)
- **Rule id referenced in V1 tests:** 0 file(s)
- **Related rules (conflict candidates):** PROC-ENV-001, SCALE-CONFIG-001, SEC-CRYPTO-KEY-001, SEC-DATA-ENCRYPT-001
- **Proposed disposition:** advisory (guidance only; no enforcement claim); pack affinity: software-engineering
- **Reviewer verdict:** _not reviewed_

## SEC-CRYPTO-RAND-001

- **Domain / severity / mandatory:** security / critical / true
- **Statement:** Cryptographic operations must use a CSPRNG. Python `secrets` / `os.urandom`, Node `crypto.randomBytes`, Java `SecureRandom`, PHP `random_bytes` / `random_int`. Non-cryptographic generators (`random`, `Math.random`, `rand`, `mt_rand`) are forbidden for security-sensitive values.
- **Trigger:** When generating any value used for security: encryption keys, IVs, nonces, salts, signing keys, password-reset tokens, MFA codes, session IDs.
- **V1 enforcement text:** Mechanically enforced by bin/run-analysis.sh::analyze_security_crypto_headers: regex flags Math.random, random.random/randint/choices, rand/mt_rand in proximity to identifiers 'iv', 'nonce', 'salt', 'key', 'token', 'secret'. Overlaps with SEC-AUTH-TOKEN-001 by design (auth tokens are a subset of security-sensitive values).
- **V1 mechanical path:** bin/run-analysis.sh::analyze_security_crypto_headers (resolves at baseline: yes)
- **Rule id referenced in V1 code:** 1 file(s): `bin/lib/analyzers-regex.sh`
- **Rule id referenced in V1 tests:** 0 file(s)
- **Related rules (conflict candidates):** SEC-AUTH-HASH-002, SEC-AUTH-TOKEN-001, SEC-CRYPTO-IV-001
- **Proposed disposition:** candidate_enforced (V1 binding resolves at baseline; must be rebound to a V2 validator); pack affinity: software-engineering
- **Reviewer verdict:** _not reviewed_

## SEC-CRYPTO-TLS-001

- **Domain / severity / mandatory:** security / high / false
- **Statement:** TLS 1.2 is the minimum; TLS 1.0 and 1.1 are disabled. The cipher suite excludes RC4, DES, 3DES, MD5-based MACs, and export-grade ciphers. Server configuration explicitly sets the floor.
- **Trigger:** When configuring TLS servers, HTTPS clients, or any code that establishes encrypted transport.
- **V1 enforcement text:** Server config review (nginx ssl_protocols, Apache SSLProtocol, Go tls.Config MinVersion, Java sslContext.getProtocol). Mozilla SSL Configuration Generator as reference.
- **V1 mechanical path:** none
- **Rule id referenced in V1 code:** 0 file(s)
- **Rule id referenced in V1 tests:** 1 file(s)
- **Related rules (conflict candidates):** SEC-CRYPTO-ALGO-002, SEC-CRYPTO-CERT-001, SEC-HDR-HSTS-001
- **Proposed disposition:** advisory (guidance only; no enforcement claim); pack affinity: software-engineering
- **Reviewer verdict:** _not reviewed_

## SEC-DATA-ENCRYPT-001

- **Domain / severity / mandatory:** security / high / false
- **Statement:** Sensitive data must be encrypted at rest. Acceptable mechanisms: database-level transparent encryption (TDE), field-level encryption with a KMS-managed key, application-level encryption with envelope-encrypted keys. Storing such data in plaintext columns is a violation.
- **Trigger:** When storing sensitive data: government IDs, financial account numbers, health records, credentials, biometric data, encryption keys, recovery codes.
- **V1 enforcement text:** Schema review. Database-level encryption (PostgreSQL pgcrypto, MySQL transparent encryption) verifies at the platform layer.
- **V1 mechanical path:** none
- **Rule id referenced in V1 code:** 0 file(s)
- **Rule id referenced in V1 tests:** 0 file(s)
- **Related rules (conflict candidates):** SEC-AUTH-MFA-001, SEC-CRYPTO-ALGO-001, SEC-CRYPTO-KEY-002, SEC-DATA-RETAIN-001
- **Proposed disposition:** advisory (guidance only; no enforcement claim); pack affinity: software-engineering
- **Reviewer verdict:** _not reviewed_

## SEC-DATA-EXPORT-001

- **Domain / severity / mandatory:** security / medium / false
- **Statement:** Data export endpoints must implement access controls (the same authz as the underlying records, plus an explicit 'can-export' permission) and audit logging (who exported what, when). Per-request rate limits cap exfil volume.
- **Trigger:** When implementing endpoints that return bulk data exports (CSV, JSON dump, ZIP archive, third-party data sync).
- **V1 enforcement text:** Code review.
- **V1 mechanical path:** none
- **Rule id referenced in V1 code:** 0 file(s)
- **Rule id referenced in V1 tests:** 0 file(s)
- **Related rules (conflict candidates):** SEC-AUTHZ-SCOPE-001, SEC-DATA-PII-002, SEC-RATE-QUERY-001
- **Proposed disposition:** advisory (guidance only; no enforcement claim); pack affinity: software-engineering
- **Reviewer verdict:** _not reviewed_

## SEC-DATA-MASK-001

- **Domain / severity / mandatory:** security / medium / false
- **Statement:** Error responses must never expose internal paths, query text, schema details, library versions, stack traces, or env-var content to end users. Production handlers return a generic message with a correlation ID; the full detail goes to logs.
- **Trigger:** When error handlers, exception middleware, or stack-trace renderers may return error details to clients.
- **V1 enforcement text:** Framework config review (Flask DEBUG=False in prod, Django DEBUG=False, FastAPI custom exception_handlers, Express custom error middleware). E2E test that production error responses never include `traceback`, file paths, or SQL text.
- **V1 mechanical path:** none
- **Rule id referenced in V1 code:** 3 file(s): `hooks/scripts/writ-bash-write-gate.sh`, `writ/analysis/instrumentation.py`, `writ/config.py`
- **Rule id referenced in V1 tests:** 2 file(s)
- **Related rules (conflict candidates):** PHP-ERR-002, SEC-UNI-003
- **Proposed disposition:** advisory (guidance only; no enforcement claim); pack affinity: software-engineering
- **Reviewer verdict:** _not reviewed_

## SEC-DATA-PII-001

- **Domain / severity / mandatory:** security / critical / true
- **Statement:** PII must never be written to plaintext logs. Either omit the value, hash it deterministically (for join-key utility), or redact to a token (`<redacted-email>`). The application is responsible for the redaction; relying on log-pipeline scrubbers downstream is a violation.
- **Trigger:** When logging any value that may contain personally identifiable information: emails, phone numbers, addresses, SSNs, government IDs, credit-card numbers, dates of birth, full names paired with other identifiers.
- **V1 enforcement text:** Mechanically enforced by bin/run-analysis.sh::analyze_security_data_protection: regex flags logger calls that pass identifiers matching email/phone/ssn/dob/address/credit_card/passport/license patterns. False positives accepted; reviewer confirms.
- **V1 mechanical path:** bin/run-analysis.sh::analyze_security_data_protection (resolves at baseline: yes)
- **Rule id referenced in V1 code:** 1 file(s): `bin/lib/analyzers-regex.sh`
- **Rule id referenced in V1 tests:** 2 file(s)
- **Related rules (conflict candidates):** SEC-DATA-PII-002, SEC-INJ-LOG-001
- **Proposed disposition:** candidate_enforced (V1 binding resolves at baseline; must be rebound to a V2 validator); pack affinity: software-engineering
- **Reviewer verdict:** _not reviewed_

## SEC-DATA-PII-002

- **Domain / severity / mandatory:** security / high / false
- **Statement:** API responses explicitly select which fields to include via a response schema or serializer. Returning the full entity (`return user`, `return model.to_dict()`) leaks PII and internal state to clients; this is a violation. The serializer enforces the field allowlist; fields the requester is not authorized to see are excluded at the data layer, not redacted in middleware.
- **Trigger:** When designing REST or GraphQL API response shapes for endpoints that return user, customer, account, or other domain-entity data.
- **V1 enforcement text:** Code review. Look for `to_dict()`, `model_dump()`, `__dict__` returned directly from a handler. Pydantic + response_model parameter (FastAPI) makes the allowlist explicit and enforced.
- **V1 mechanical path:** none
- **Rule id referenced in V1 code:** 0 file(s)
- **Rule id referenced in V1 tests:** 4 file(s)
- **Related rules (conflict candidates):** SEC-AUTHZ-MASS-001, SEC-DATA-EXPORT-001, SEC-DATA-PII-001, SEC-UNI-003, SEC-VAL-TYPE-001
- **Proposed disposition:** advisory (guidance only; no enforcement claim); pack affinity: software-engineering
- **Reviewer verdict:** _not reviewed_

## SEC-DATA-RETAIN-001

- **Domain / severity / mandatory:** security / medium / false
- **Statement:** Data retention must follow a documented policy with explicit lifetimes. Indefinite storage of user-generated content or analytics data requires a documented justification (legal obligation, contractual). A retention sweep job purges data past lifetime.
- **Trigger:** When designing schemas, batch jobs, or background processes that retain user data.
- **V1 enforcement text:** Schema review documenting retention per table. Audit job logs deletion counts. Related: SEC-DATA-ENCRYPT-001.
- **V1 mechanical path:** none
- **Rule id referenced in V1 code:** 0 file(s)
- **Rule id referenced in V1 tests:** 1 file(s)
- **Related rules (conflict candidates):** SEC-DATA-ENCRYPT-001
- **Proposed disposition:** advisory (guidance only; no enforcement claim); pack affinity: software-engineering
- **Reviewer verdict:** _not reviewed_

## SEC-DEP-AUDIT-001

- **Domain / severity / mandatory:** security / high / false
- **Statement:** No direct dependency may have a known critical-or-high-severity CVE. Acceptable mechanisms: npm audit, pip-audit, cargo audit, bundler-audit, composer audit, GitHub Dependabot, Snyk. The check runs in CI and gates merge.
- **Trigger:** When adding, updating, or pinning a dependency in package.json, requirements.txt, pyproject.toml, Gemfile, composer.json, go.mod, Cargo.toml.
- **V1 enforcement text:** CI gate (npm audit, pip-audit, cargo audit). Dependabot/Renovate auto-PRs for vulnerable upgrades. Code review on bypass justifications.
- **V1 mechanical path:** none
- **Rule id referenced in V1 code:** 0 file(s)
- **Rule id referenced in V1 tests:** 1 file(s)
- **Related rules (conflict candidates):** SEC-DEP-LOCK-001, SEC-DEP-PIN-001, SEC-DEP-REVIEW-001
- **Proposed disposition:** advisory (guidance only; no enforcement claim); pack affinity: software-engineering
- **Reviewer verdict:** _not reviewed_

## SEC-DEP-LOCK-001

- **Domain / severity / mandatory:** security / medium / false
- **Statement:** Lockfiles (package-lock.json, yarn.lock, poetry.lock, Cargo.lock, Gemfile.lock, composer.lock) are committed and used for installs in CI and production. `npm install` without an existing lockfile, `pip install` without `--require-hashes`, or production builds that resolve loose versions are violations.
- **Trigger:** When the project has a package manager that supports lockfiles.
- **V1 enforcement text:** CI/build config review.
- **V1 mechanical path:** none
- **Rule id referenced in V1 code:** 0 file(s)
- **Rule id referenced in V1 tests:** 0 file(s)
- **Related rules (conflict candidates):** SEC-DEP-AUDIT-001, SEC-DEP-PIN-001
- **Proposed disposition:** advisory (guidance only; no enforcement claim); pack affinity: software-engineering
- **Reviewer verdict:** _not reviewed_

## SEC-DEP-PIN-001

- **Domain / severity / mandatory:** security / medium / false
- **Statement:** Dependencies are pinned to an exact version or a narrow range (`~1.2.3`, `>=1.2,<1.3`), not open-ended (`*`, `^1.0.0` on critical libs, `latest`). Lockfiles capture exact resolution; manifests bound which resolutions are acceptable.
- **Trigger:** When declaring dependency versions in manifest files.
- **V1 enforcement text:** Manifest review.
- **V1 mechanical path:** none
- **Rule id referenced in V1 code:** 0 file(s)
- **Rule id referenced in V1 tests:** 0 file(s)
- **Related rules (conflict candidates):** SEC-DEP-AUDIT-001, SEC-DEP-LOCK-001, SEC-DEP-REVIEW-001
- **Proposed disposition:** advisory (guidance only; no enforcement claim); pack affinity: software-engineering
- **Reviewer verdict:** _not reviewed_

## SEC-DEP-REVIEW-001

- **Domain / severity / mandatory:** security / low / false
- **Statement:** New dependency additions are documented in the PR description: what library, what it does, why this one over alternatives, license, maintainer activity (commits in last 6 months, weekly downloads). A reviewer signs off on the addition specifically.
- **Trigger:** When opening a pull request that adds a new dependency to a manifest file.
- **V1 enforcement text:** PR template + reviewer checklist. CODEOWNERS for manifest files routes adds to security-aware reviewers.
- **V1 mechanical path:** none
- **Rule id referenced in V1 code:** 0 file(s)
- **Rule id referenced in V1 tests:** 0 file(s)
- **Related rules (conflict candidates):** SEC-DEP-AUDIT-001, SEC-DEP-PIN-001
- **Proposed disposition:** advisory (guidance only; no enforcement claim); pack affinity: software-engineering
- **Reviewer verdict:** _not reviewed_

## SEC-HDR-CORS-001

- **Domain / severity / mandatory:** security / critical / false
- **Statement:** Access-Control-Allow-Origin must never be `*` for credentialed requests (Access-Control-Allow-Credentials: true). Origins are listed explicitly or validated against an allowlist of internal origins.
- **Trigger:** When configuring CORS middleware for an API that uses cookie-based or token-based authentication.
- **V1 enforcement text:** Middleware config review.
- **V1 mechanical path:** none
- **Rule id referenced in V1 code:** 0 file(s)
- **Rule id referenced in V1 tests:** 1 file(s)
- **Related rules (conflict candidates):** SEC-AUTH-TOKEN-002, SEC-INJ-CSRF-001
- **Proposed disposition:** advisory (guidance only; no enforcement claim); pack affinity: software-engineering
- **Reviewer verdict:** _not reviewed_

## SEC-HDR-CSP-001

- **Domain / severity / mandatory:** security / high / false
- **Statement:** Content-Security-Policy header must be set on HTML responses. `unsafe-inline` and `unsafe-eval` are forbidden unless documented with the specific framework constraint that requires them. Default-src defaults to 'self'.
- **Trigger:** When configuring an application that serves HTML responses to browsers.
- **V1 enforcement text:** Middleware review. Browsers expose CSP violations via report-uri; track those in monitoring.
- **V1 mechanical path:** none
- **Rule id referenced in V1 code:** 0 file(s)
- **Rule id referenced in V1 tests:** 1 file(s)
- **Related rules (conflict candidates):** SEC-HDR-FRAME-001, SEC-INJ-XSS-001, SEC-INJ-XSS-003
- **Proposed disposition:** advisory (guidance only; no enforcement claim); pack affinity: software-engineering
- **Reviewer verdict:** _not reviewed_

## SEC-HDR-FRAME-001

- **Domain / severity / mandatory:** security / medium / false
- **Statement:** X-Frame-Options: DENY (or SAMEORIGIN) or `frame-ancestors` in CSP must be set. Pages reachable from a browser session are not embeddable in third-party frames unless explicitly permitted.
- **Trigger:** When serving HTML responses that include any state-changing or sensitive UI.
- **V1 enforcement text:** Middleware config review.
- **V1 mechanical path:** none
- **Rule id referenced in V1 code:** 0 file(s)
- **Rule id referenced in V1 tests:** 0 file(s)
- **Related rules (conflict candidates):** SEC-HDR-CSP-001
- **Proposed disposition:** advisory (guidance only; no enforcement claim); pack affinity: software-engineering
- **Reviewer verdict:** _not reviewed_

## SEC-HDR-HSTS-001

- **Domain / severity / mandatory:** security / high / false
- **Statement:** Strict-Transport-Security header is set with max-age at least 31536000 seconds (1 year). includeSubDomains is added once subdomains are HTTPS-ready. preload submitted for production-stable domains.
- **Trigger:** When serving any HTTPS endpoint from a production domain.
- **V1 enforcement text:** Middleware config review. Verify with securityheaders.com.
- **V1 mechanical path:** none
- **Rule id referenced in V1 code:** 0 file(s)
- **Rule id referenced in V1 tests:** 1 file(s)
- **Related rules (conflict candidates):** SEC-AUTH-TOKEN-002, SEC-CRYPTO-TLS-001
- **Proposed disposition:** advisory (guidance only; no enforcement claim); pack affinity: software-engineering
- **Reviewer verdict:** _not reviewed_

## SEC-HDR-REFERRER-001

- **Domain / severity / mandatory:** security / low / false
- **Statement:** Referrer-Policy is set to `no-referrer` or `strict-origin-when-cross-origin`. Default-leak of the full URL (which may contain tokens or PII) to third-party hosts is prevented.
- **Trigger:** When serving HTML responses.
- **V1 enforcement text:** Middleware config review.
- **V1 mechanical path:** none
- **Rule id referenced in V1 code:** 0 file(s)
- **Rule id referenced in V1 tests:** 0 file(s)
- **Related rules (conflict candidates):** SEC-AUTH-RESET-001
- **Proposed disposition:** advisory (guidance only; no enforcement claim); pack affinity: software-engineering
- **Reviewer verdict:** _not reviewed_

## SEC-HDR-TYPE-001

- **Domain / severity / mandatory:** security / medium / false
- **Statement:** X-Content-Type-Options: nosniff is set on all responses. Browsers must respect the declared Content-Type and not infer it from the body.
- **Trigger:** When serving any response from the application server.
- **V1 enforcement text:** Middleware config review.
- **V1 mechanical path:** none
- **Rule id referenced in V1 code:** 0 file(s)
- **Rule id referenced in V1 tests:** 0 file(s)
- **Related rules (conflict candidates):** SEC-VAL-FILE-001
- **Proposed disposition:** advisory (guidance only; no enforcement claim); pack affinity: software-engineering
- **Reviewer verdict:** _not reviewed_

## SEC-INJ-CMD-001

- **Domain / severity / mandatory:** security / critical / true
- **Statement:** Shell commands must be invoked with an argument list, not a single string interpreted by a shell. Python: `subprocess.run([cmd, arg1, arg2])` (no `shell=True`). Node: `child_process.execFile`/`spawn` (not `exec`). PHP: explicit array arguments, never `shell_exec` with concatenated input.
- **Trigger:** When invoking shell commands from application code, especially when any argument could derive from user input.
- **V1 enforcement text:** Mechanically enforced by bin/run-analysis.sh::analyze_security_injection: regex flags `subprocess.run(`/`subprocess.Popen(` with `shell=True`, bare `os.system(`/`os.popen(`, `child_process.exec(`/`execSync(`, PHP `shell_exec`/`exec(`/`system(`/`passthru(` with non-constant args.
- **V1 mechanical path:** bin/run-analysis.sh::analyze_security_injection (resolves at baseline: yes)
- **Rule id referenced in V1 code:** 6 file(s): `bin/check-gates.sh`, `bin/lib/analyzers-regex.sh`, `hooks/git/post-commit`, `hooks/scripts/auto-approve-gate.sh`, `hooks/scripts/writ-manual-test-grant.sh` ...
- **Rule id referenced in V1 tests:** 3 file(s)
- **Related rules (conflict candidates):** SEC-INJ-CMD-002, SEC-INJ-LDAP-001, SEC-INJ-SQL-001
- **Proposed disposition:** candidate_enforced (V1 binding resolves at baseline; must be rebound to a V2 validator); pack affinity: software-engineering
- **Reviewer verdict:** _not reviewed_

## SEC-INJ-CMD-002

- **Domain / severity / mandatory:** security / high / false
- **Statement:** Dynamic code-string evaluation is prohibited when any input to the code string is not a literal constant. If dynamic dispatch is required, use a lookup table, dispatch dict, or registry pattern instead.
- **Trigger:** When using language-level eval, exec, or dynamic code-string evaluators (Python `eval`/`exec`, JavaScript `eval`/`new Function`, PHP `eval`).
- **V1 enforcement text:** Mechanically enforced by bin/run-analysis.sh::analyze_security_injection: regex flags `eval(`, `exec(`, `new Function(`, PHP `eval(` and Python `compile(`+`exec(` patterns.
- **V1 mechanical path:** none
- **Rule id referenced in V1 code:** 2 file(s): `bin/lib/analyzers-regex.sh`, `bin/lib/common.sh`
- **Rule id referenced in V1 tests:** 3 file(s)
- **Related rules (conflict candidates):** SEC-INJ-CMD-001, SEC-INJ-DESER-001, SEC-INJ-SSTI-001
- **Proposed disposition:** advisory (guidance only; no enforcement claim); pack affinity: software-engineering
- **Reviewer verdict:** _not reviewed_

## SEC-INJ-CSRF-001

- **Domain / severity / mandatory:** security / critical / true
- **Statement:** State-changing requests must be validated against an anti-CSRF token: synchronizer token, double-submit cookie, SameSite=Strict cookies plus origin check, or the framework's built-in CSRF middleware. Cross-origin state changes without a token are violations.
- **Trigger:** When implementing any HTTP endpoint that mutates server state (POST, PUT, PATCH, DELETE) and is reachable from a browser session.
- **V1 enforcement text:** Mechanically enforced by bin/run-analysis.sh::analyze_security_injection: framework-aware presence check (Django middleware enabled, Flask-WTF CSRFProtect initialized, etc.). Where framework support exists, opting out (`@csrf_exempt`) requires a documented justification.
- **V1 mechanical path:** bin/run-analysis.sh::analyze_security_injection (resolves at baseline: yes)
- **Rule id referenced in V1 code:** 0 file(s)
- **Rule id referenced in V1 tests:** 1 file(s)
- **Related rules (conflict candidates):** SEC-AUTH-TOKEN-001, SEC-AUTH-TOKEN-002, SEC-HDR-CORS-001, SEC-INJ-XSS-001
- **Proposed disposition:** candidate_enforced (V1 binding resolves at baseline; must be rebound to a V2 validator); pack affinity: software-engineering
- **Reviewer verdict:** _not reviewed_

## SEC-INJ-DESER-001

- **Domain / severity / mandatory:** security / critical / true
- **Statement:** Untrusted data must never be deserialized with formats that allow code execution: Python `pickle`, PHP `unserialize`, Java `ObjectInputStream`, YAML `yaml.load` without `SafeLoader`, Ruby Marshal. Use JSON, MessagePack, or a typed schema (Protobuf, JSON Schema, Pydantic) instead.
- **Trigger:** When deserializing data received from any external or untrusted source (cookies, request bodies, files, queues, databases populated by other tenants).
- **V1 enforcement text:** Mechanically enforced by bin/run-analysis.sh::analyze_security_injection: regex flags `pickle.loads(`, `pickle.load(`, `yaml.load(` without `SafeLoader=`, PHP `unserialize(`, Java `ObjectInputStream`.
- **V1 mechanical path:** bin/run-analysis.sh::analyze_security_injection (resolves at baseline: yes)
- **Rule id referenced in V1 code:** 1 file(s): `bin/lib/analyzers-regex.sh`
- **Rule id referenced in V1 tests:** 1 file(s)
- **Related rules (conflict candidates):** SEC-INJ-CMD-002, SEC-INJ-SSTI-001, SEC-VAL-TYPE-001
- **Proposed disposition:** candidate_enforced (V1 binding resolves at baseline; must be rebound to a V2 validator); pack affinity: software-engineering
- **Reviewer verdict:** _not reviewed_

## SEC-INJ-HEADER-001

- **Domain / severity / mandatory:** security / high / false
- **Statement:** Response header values constructed from user input must reject or escape CR (`\r`, `%0d`) and LF (`\n`, `%0a`). Most frameworks reject newlines in header values automatically; custom header writers must do the same.
- **Trigger:** When setting HTTP response headers whose values derive from user input (redirects with user-supplied URL, content-disposition with user filename, custom headers echoing request data).
- **V1 enforcement text:** Code review. Custom check: response.headers[X] = Y where Y derives from request input without explicit newline rejection.
- **V1 mechanical path:** none
- **Rule id referenced in V1 code:** 0 file(s)
- **Rule id referenced in V1 tests:** 0 file(s)
- **Related rules (conflict candidates):** SEC-INJ-LOG-001, SEC-INJ-REDIR-001
- **Proposed disposition:** advisory (guidance only; no enforcement claim); pack affinity: software-engineering
- **Reviewer verdict:** _not reviewed_

## SEC-INJ-LDAP-001

- **Domain / severity / mandatory:** security / high / false
- **Statement:** LDAP filter and DN construction must use the bind/parameter APIs provided by the LDAP client library, never string formatting. Special LDAP characters (`*`, `(`, `)`, `\`, NUL) in user input must be escaped before they reach the filter.
- **Trigger:** When constructing LDAP search filters or DNs from any non-constant value.
- **V1 enforcement text:** Code review. Custom ruff plugin or grep for LDAP search calls with non-constant filter strings.
- **V1 mechanical path:** none
- **Rule id referenced in V1 code:** 0 file(s)
- **Rule id referenced in V1 tests:** 0 file(s)
- **Related rules (conflict candidates):** SEC-INJ-CMD-001, SEC-INJ-SQL-001
- **Proposed disposition:** advisory (guidance only; no enforcement claim); pack affinity: software-engineering
- **Reviewer verdict:** _not reviewed_
