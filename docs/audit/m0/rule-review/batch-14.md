# Rule review batch-14: security (25 rules)

Source: `writ-corpus.cypher` at `ebdafe5450b201339f81e717ed104e6bcfd3fe3e`. Proposed dispositions are the automated first pass (D-004), not verdicts. Record decisions in [batch-14.csv](batch-14.csv).

## SEC-INJ-LOG-001

- **Domain / severity / mandatory:** security / medium / false
- **Statement:** Log values that originate from user input must be sanitized to prevent log injection or forging: strip or escape CR/LF, structure log entries as JSON or key-value pairs (one record per line) so injected newlines cannot fake additional log entries.
- **Trigger:** When user-controlled values are written to application logs.
- **V1 enforcement text:** Code review. Custom check on print()/logger calls with f-strings containing user-attribute references.
- **V1 mechanical path:** none
- **Rule id referenced in V1 code:** 3 file(s): `bin/lib/analyzers-regex.sh`, `bin/lib/common.sh`, `writ/shared/logging.py`
- **Rule id referenced in V1 tests:** 4 file(s)
- **Related rules (conflict candidates):** CLEAN-LOG-001, SEC-DATA-PII-001, SEC-INJ-HEADER-001
- **Proposed disposition:** advisory (guidance only; no enforcement claim); pack affinity: software-engineering
- **Reviewer verdict:** _not reviewed_

## SEC-INJ-PATH-001

- **Domain / severity / mandatory:** security / critical / false
- **Statement:** User-supplied path components must be validated against path traversal: reject `..`, null bytes, and absolute paths; resolve to a canonical path; verify the result is within the intended base directory. The base directory check uses canonical paths on both sides.
- **Trigger:** When file paths are constructed from user input (uploads, downloads, includes, template loading, archive extraction).
- **V1 enforcement text:** Code review. Static analysis can flag `os.path.join` followed by `open(` with non-constant first argument.
- **V1 mechanical path:** none
- **Rule id referenced in V1 code:** 3 file(s): `bin/lib/test_paths.py`, `writ/cli.py`, `writ/shared/logging.py`
- **Rule id referenced in V1 tests:** 5 file(s)
- **Related rules (conflict candidates):** SEC-VAL-ENCODING-001, SEC-VAL-FILE-001
- **Proposed disposition:** advisory (guidance only; no enforcement claim); pack affinity: software-engineering
- **Reviewer verdict:** _not reviewed_

## SEC-INJ-REDIR-001

- **Domain / severity / mandatory:** security / high / false
- **Statement:** Redirect destinations from user input must be validated against an allowlist of internal paths or an explicit list of permitted external hosts. Open redirect is a violation even when the receiving endpoint is otherwise harmless.
- **Trigger:** When an endpoint redirects the user to a URL supplied or influenced by request parameters (login-success redirects, return-to flows, deeplink handlers).
- **V1 enforcement text:** Code review. Framework-specific redirect helpers (Django's `url_has_allowed_host_and_scheme`) provide a vetted check.
- **V1 mechanical path:** none
- **Rule id referenced in V1 code:** 0 file(s)
- **Rule id referenced in V1 tests:** 1 file(s)
- **Related rules (conflict candidates):** SEC-INJ-HEADER-001, SEC-INJ-SSRF-001, SEC-VAL-ALLOW-001
- **Proposed disposition:** advisory (guidance only; no enforcement claim); pack affinity: software-engineering
- **Reviewer verdict:** _not reviewed_

## SEC-INJ-SQL-001

- **Domain / severity / mandatory:** security / critical / true
- **Statement:** Parameterized queries only. SQL must never be built by string concatenation, format, f-string, or interpolation of any value that is not a literal constant. Every dynamic value enters through a bind parameter: a positional placeholder (`?`, `%s`) or a named placeholder (`:name`). Parameterization is the structural defense against SQL injection.
- **Trigger:** When writing SQL strings that include any variable, user input, or function return value.
- **V1 enforcement text:** Mechanically enforced by bin/run-analysis.sh::analyze_security_injection: regex scan flags f-strings, .format(), and `+`-concatenation in proximity to execute/cursor/raw-query call sites. PHPStan + PHPCS catch the equivalent PHP patterns.
- **V1 mechanical path:** bin/run-analysis.sh::analyze_security_injection (resolves at baseline: yes)
- **Rule id referenced in V1 code:** 1 file(s): `bin/lib/analyzers-regex.sh`
- **Rule id referenced in V1 tests:** 9 file(s)
- **Related rules (conflict candidates):** SEC-INJ-CMD-001, SEC-INJ-LDAP-001, SEC-INJ-SQL-002, SEC-INJ-SQL-003
- **Proposed disposition:** candidate_enforced (V1 binding resolves at baseline; must be rebound to a V2 validator); pack affinity: software-engineering
- **Reviewer verdict:** _not reviewed_

## SEC-INJ-SQL-002

- **Domain / severity / mandatory:** security / critical / false
- **Statement:** ORM raw-query methods must use the ORM's parameter binding, never string interpolation. Escape hatches bypass the ORM's automatic parameterization; bound parameters bring it back.
- **Trigger:** When using ORM raw-query escape hatches (Django `raw()`, SQLAlchemy `text()`, Doctrine `getNativeQuery`, Eloquent `DB::raw`).
- **V1 enforcement text:** Code review. Static analysis (custom ruff plugin or grep for ORM raw-method names with non-constant args) can flag the most common cases.
- **V1 mechanical path:** none
- **Rule id referenced in V1 code:** 1 file(s): `bin/lib/analyzers-regex.sh`
- **Rule id referenced in V1 tests:** 1 file(s)
- **Related rules (conflict candidates):** SEC-INJ-SQL-001, SEC-INJ-SQL-003
- **Proposed disposition:** advisory (guidance only; no enforcement claim); pack affinity: software-engineering
- **Reviewer verdict:** _not reviewed_

## SEC-INJ-SQL-003

- **Domain / severity / mandatory:** security / high / false
- **Statement:** Stored procedure invocations must pass arguments as bound parameters, never as part of the procedure call string.
- **Trigger:** When calling stored procedures from application code.
- **V1 enforcement text:** Code review. Same SQL-concat detector flags the procedure-call form.
- **V1 mechanical path:** none
- **Rule id referenced in V1 code:** 0 file(s)
- **Rule id referenced in V1 tests:** 0 file(s)
- **Related rules (conflict candidates):** SEC-INJ-SQL-001, SEC-INJ-SQL-002
- **Proposed disposition:** advisory (guidance only; no enforcement claim); pack affinity: software-engineering
- **Reviewer verdict:** _not reviewed_

## SEC-INJ-SSRF-001

- **Domain / severity / mandatory:** security / critical / true
- **Statement:** Outbound requests with user-supplied destinations must validate the resolved host against an allowlist of permitted destinations. Block all RFC1918 ranges (10/8, 172.16/12, 192.168/16), loopback (127/8, ::1), link-local (169.254/16), cloud metadata IPs (169.254.169.254), and IPv4-mapped IPv6 forms of all of the above. Resolution and connection must use the same address to prevent DNS rebinding.
- **Trigger:** When making outbound HTTP/network requests where the destination URL or host derives from user input (webhooks, URL previews, profile-image fetches, ingest jobs that follow a user-supplied link).
- **V1 enforcement text:** Mechanically enforced by bin/run-analysis.sh::analyze_security_injection: regex flags `requests.get(`/`requests.post(`/`urllib.urlopen(`/`fetch(` with non-constant URL arguments. Static analysis cannot fully prove SSRF safety; the rule still requires reviewer attention.
- **V1 mechanical path:** bin/run-analysis.sh::analyze_security_injection (resolves at baseline: yes)
- **Rule id referenced in V1 code:** 2 file(s): `writ/session/bitbucket_client.py`, `writ/session/remote_parse.py`
- **Rule id referenced in V1 tests:** 2 file(s)
- **Related rules (conflict candidates):** SEC-CRYPTO-CERT-001, SEC-INJ-REDIR-001, SEC-VAL-ALLOW-001
- **Proposed disposition:** candidate_enforced (V1 binding resolves at baseline; must be rebound to a V2 validator); pack affinity: software-engineering
- **Reviewer verdict:** _not reviewed_

## SEC-INJ-SSTI-001

- **Domain / severity / mandatory:** security / critical / false
- **Statement:** Template engines (Jinja2, Twig, Handlebars, ERB) must never receive user input as the template body. User values are arguments passed to a fixed template; the template itself is always a literal or a file path under developer control.
- **Trigger:** When user input could reach a template-rendering engine as the template string itself, not as a value passed to a fixed template.
- **V1 enforcement text:** Code review. Static analysis: flag `Template(`/`new Template(`/`compile_template(` with non-constant args.
- **V1 mechanical path:** none
- **Rule id referenced in V1 code:** 1 file(s): `bin/lib/analyzers-regex.sh`
- **Rule id referenced in V1 tests:** 2 file(s)
- **Related rules (conflict candidates):** SEC-INJ-CMD-002, SEC-INJ-DESER-001
- **Proposed disposition:** advisory (guidance only; no enforcement claim); pack affinity: software-engineering
- **Reviewer verdict:** _not reviewed_

## SEC-INJ-XSS-001

- **Domain / severity / mandatory:** security / critical / true
- **Statement:** All user-supplied content must be rendered through the framework's automatic escaping: React JSX `{value}`, Vue/Blade/Jinja2 `{{ value }}`, Twig `{{ value }}`, Angular interpolation. Never bypass framework escaping for user data.
- **Trigger:** When user-supplied content is rendered in HTML, including server-rendered templates, JSX, and Vue/Svelte/Blade templates.
- **V1 enforcement text:** Mechanically enforced by bin/run-analysis.sh::analyze_security_injection: regex scan flags `dangerouslySetInnerHTML`, `v-html`, `{!! !!}`, `innerHTML =`, `outerHTML =`, `document.write(`, and `$()`.html(` with non-constant args.
- **V1 mechanical path:** bin/run-analysis.sh::analyze_security_injection (resolves at baseline: yes)
- **Rule id referenced in V1 code:** 1 file(s): `bin/lib/analyzers-regex.sh`
- **Rule id referenced in V1 tests:** 1 file(s)
- **Related rules (conflict candidates):** SEC-HDR-CSP-001, SEC-INJ-CSRF-001, SEC-INJ-XSS-002, SEC-INJ-XSS-003, SEC-INJ-XSS-004
- **Proposed disposition:** candidate_enforced (V1 binding resolves at baseline; must be rebound to a V2 validator); pack affinity: software-engineering
- **Reviewer verdict:** _not reviewed_

## SEC-INJ-XSS-002

- **Domain / severity / mandatory:** security / critical / false
- **Statement:** Never use `dangerouslySetInnerHTML`, `v-html`, `{!! !!}`, Svelte `{@html}`, or any equivalent unsafe-render API. If raw HTML is genuinely required (rich-text editor output), the content must be sanitized through a vetted library (DOMPurify, HTMLPurifier) before reaching the API.
- **Trigger:** When writing JSX, Vue, Svelte, Blade, or other template code that touches any rendering API capable of inserting raw HTML.
- **V1 enforcement text:** Mechanically enforced by bin/run-analysis.sh::analyze_security_injection alongside SEC-INJ-XSS-001.
- **V1 mechanical path:** none
- **Rule id referenced in V1 code:** 1 file(s): `bin/lib/analyzers-regex.sh`
- **Rule id referenced in V1 tests:** 0 file(s)
- **Related rules (conflict candidates):** SEC-INJ-XSS-001, SEC-INJ-XSS-003, SEC-INJ-XSS-004
- **Proposed disposition:** advisory (guidance only; no enforcement claim); pack affinity: software-engineering
- **Reviewer verdict:** _not reviewed_

## SEC-INJ-XSS-003

- **Domain / severity / mandatory:** security / high / false
- **Statement:** Direct DOM mutation methods that interpret strings as HTML (`innerHTML`, `outerHTML`, `document.write`, `$.html()`) must never receive user data. Use `textContent`, `innerText`, or framework-rendered nodes instead.
- **Trigger:** When manipulating the DOM in vanilla JavaScript with user-controlled values, including rendering user-generated content (comments, reviews, profile text, messages) into the page. This is about DOM insertion of user text, not code comments.
- **V1 enforcement text:** Mechanically enforced by bin/run-analysis.sh::analyze_security_injection.
- **V1 mechanical path:** none
- **Rule id referenced in V1 code:** 1 file(s): `bin/lib/analyzers-regex.sh`
- **Rule id referenced in V1 tests:** 2 file(s)
- **Related rules (conflict candidates):** SEC-HDR-CSP-001, SEC-INJ-XSS-001, SEC-INJ-XSS-002, SEC-INJ-XSS-004
- **Proposed disposition:** advisory (guidance only; no enforcement claim); pack affinity: software-engineering
- **Reviewer verdict:** _not reviewed_

## SEC-INJ-XSS-004

- **Domain / severity / mandatory:** security / high / false
- **Statement:** Bypassing the framework's auto-escape (React `dangerouslySetInnerHTML`, Lit `unsafeHTML` / `.innerHTML=`, Vue `v-html`, Blade `{!! !!}`) is permitted ONLY when both hold: (1) rendering real HTML is a deliberate product requirement, not the rendering of an arbitrary user string such as a comment, name, or search term; AND (2) the content is passed through an allowlist sanitizer at the render seam that drops every non-allowlisted element, strips all `on*` event-handler attributes, and neutralizes `javascript:` / scriptable URLs, BEFORE it reaches the unsafe-render API. The escape mandate of SEC-INJ-XSS-001 continues to bind every value that does not meet both conditions. This is a narrow carve-out, not a relaxation.
- **Trigger:** When rendering raw HTML is a genuine product requirement (rich-text editor output, CMS or LLM-generated marketing HTML shown in an internal viewer), so the framework auto-escape mandated by SEC-INJ-XSS-001/002/003 would render the markup as visible text and break the feature.
- **V1 enforcement text:** Code review at the seam: the reviewer must confirm an allowlist sanitizer wraps the value before the unsafe-render call, and that the source is a genuine-HTML field. The sanctioned implementation is DOMPurify with a tight allowlist config, already established in ai-stack at `apps/shopping/src/util/sanitize-html.ts` (HIGH-1 / WEB-9206): `ALLOWED_TAGS` + `ALLOWED_ATTR` + `ALLOWED_URI_REGEXP` (drops `javascript:` / `vbscript:` / `data:`) plus an `afterSanitizeAttributes` hook that hardens anchors (`target=_blank`, `rel="noopener noreferrer nofollow"`). Reuse that pattern; do not hand-roll a scrubber when DOMPurify is available. A `DOMParser`-based allowlist (e.g. `SvgContentDisplayComponent.sanitizeSvg`) is a fallback only where a vetted library genuinely cannot be added. Originating finding: HIGH-21 (`DataFileDisplayComponent` CSV-summary XSS), the direct analog of HIGH-1.
- **V1 mechanical path:** none
- **Rule id referenced in V1 code:** 0 file(s)
- **Rule id referenced in V1 tests:** 0 file(s)
- **Related rules (conflict candidates):** SEC-INJ-XSS-001, SEC-INJ-XSS-002, SEC-INJ-XSS-003
- **Proposed disposition:** advisory (guidance only; no enforcement claim); pack affinity: software-engineering
- **Reviewer verdict:** _not reviewed_

## SEC-RATE-API-001

- **Domain / severity / mandatory:** security / high / false
- **Statement:** Public API endpoints must be rate-limited per client identity (API key, authenticated user, or source IP for unauthenticated). The limit applies at the application or gateway layer, not just at the network layer.
- **Trigger:** When exposing public API endpoints (REST, GraphQL, RPC) to internet traffic.
- **V1 enforcement text:** Middleware config review. API gateways (Kong, Tyk, AWS API Gateway) provide native support.
- **V1 mechanical path:** none
- **Rule id referenced in V1 code:** 0 file(s)
- **Rule id referenced in V1 tests:** 2 file(s)
- **Related rules (conflict candidates):** SEC-RATE-BATCH-001, SEC-RATE-LOGIN-001, SEC-RATE-QUERY-001, SEC-RATE-UPLOAD-001
- **Proposed disposition:** advisory (guidance only; no enforcement claim); pack affinity: software-engineering
- **Reviewer verdict:** _not reviewed_

## SEC-RATE-BATCH-001

- **Domain / severity / mandatory:** security / medium / false
- **Statement:** Batch endpoints cap the number of items accepted per request AND apply a per-batch rate limit. Unbounded batch APIs let a single request consume disproportionate resources.
- **Trigger:** When implementing bulk/batch endpoints that accept an array of operations (bulk create, bulk update, multi-delete).
- **V1 enforcement text:** Code review.
- **V1 mechanical path:** none
- **Rule id referenced in V1 code:** 0 file(s)
- **Rule id referenced in V1 tests:** 1 file(s)
- **Related rules (conflict candidates):** SEC-RATE-API-001, SEC-RATE-QUERY-001
- **Proposed disposition:** advisory (guidance only; no enforcement claim); pack affinity: software-engineering
- **Reviewer verdict:** _not reviewed_

## SEC-RATE-LOGIN-001

- **Domain / severity / mandatory:** security / high / false
- **Statement:** Authentication endpoints carry their own rate limit, distinct from the general API limit. Login limits are per account AND per IP (per-account stops credential stuffing of a known user; per-IP stops bulk username enumeration).
- **Trigger:** When implementing authentication endpoints (login, password reset, MFA verify, token exchange).
- **V1 enforcement text:** Code review.
- **V1 mechanical path:** none
- **Rule id referenced in V1 code:** 0 file(s)
- **Rule id referenced in V1 tests:** 0 file(s)
- **Related rules (conflict candidates):** SEC-AUTH-BRUTE-001, SEC-AUTH-ENUM-001, SEC-RATE-API-001
- **Proposed disposition:** advisory (guidance only; no enforcement claim); pack affinity: software-engineering
- **Reviewer verdict:** _not reviewed_

## SEC-RATE-QUERY-001

- **Domain / severity / mandatory:** security / medium / false
- **Statement:** Database queries triggered by user input must apply pagination with a maximum page size, and unbounded result sets are not returned. The default page size is small; the max is bounded.
- **Trigger:** When implementing endpoints that trigger database queries based on user input (list, search, filter, export, GraphQL collection fields).
- **V1 enforcement text:** Code review. GraphQL: depth-limit / cost-analysis plugins.
- **V1 mechanical path:** none
- **Rule id referenced in V1 code:** 0 file(s)
- **Rule id referenced in V1 tests:** 1 file(s)
- **Related rules (conflict candidates):** API-PAGINATION-001, PERF-QUERY-004, SEC-DATA-EXPORT-001, SEC-RATE-API-001, SEC-RATE-BATCH-001, SEC-VAL-REGEX-001
- **Proposed disposition:** advisory (guidance only; no enforcement claim); pack affinity: software-engineering
- **Reviewer verdict:** _not reviewed_

## SEC-RATE-UPLOAD-001

- **Domain / severity / mandatory:** security / high / false
- **Statement:** File upload endpoints enforce explicit size limits at the application layer: max bytes per file AND max bytes per request body. The application rejects oversized requests before reading them entirely into memory.
- **Trigger:** When implementing file upload endpoints.
- **V1 enforcement text:** Framework config review (Flask MAX_CONTENT_LENGTH, Django DATA_UPLOAD_MAX_MEMORY_SIZE, nginx client_max_body_size).
- **V1 mechanical path:** none
- **Rule id referenced in V1 code:** 0 file(s)
- **Rule id referenced in V1 tests:** 0 file(s)
- **Related rules (conflict candidates):** SEC-RATE-API-001, SEC-VAL-FILE-001
- **Proposed disposition:** advisory (guidance only; no enforcement claim); pack affinity: software-engineering
- **Reviewer verdict:** _not reviewed_

## SEC-UNI-003

- **Domain / severity / mandatory:** security / high / false
- **Statement:** API responses must explicitly select which fields to include. Never return the full entity. Internal IDs, credentials, PII, and internal state are excluded unless the endpoint specifically requires them and the caller has rights.
- **Trigger:** When an API endpoint returns entity data to a caller and the response includes the full entity object or uses `toArray()`/`getData()`/`jsonSerialize()` without field filtering.
- **V1 enforcement text:** SEC-DATA-MASK-001 (error responses must not expose internals).
- **V1 mechanical path:** none
- **Rule id referenced in V1 code:** 0 file(s)
- **Rule id referenced in V1 tests:** 6 file(s)
- **Related rules (conflict candidates):** SEC-DATA-MASK-001, SEC-DATA-PII-002
- **Proposed disposition:** advisory (guidance only; no enforcement claim); pack affinity: software-engineering
- **Reviewer verdict:** _not reviewed_

## SEC-VAL-ALLOW-001

- **Domain / severity / mandatory:** security / high / false
- **Statement:** Validation uses an allowlist (explicit set of permitted values) whenever the valid set is enumerable. Blocklists (reject these characters/values) are forbidden where allowlists are possible: blocklists are routinely incomplete and silently fail open on unanticipated inputs.
- **Trigger:** When validating inputs whose valid set is enumerable (countries, statuses, roles, file types, sort fields, redirect targets).
- **V1 enforcement text:** Code review. Look for negative-pattern checks (blocklists) on fields with an enumerable valid set.
- **V1 mechanical path:** none
- **Rule id referenced in V1 code:** 0 file(s)
- **Rule id referenced in V1 tests:** 1 file(s)
- **Related rules (conflict candidates):** SEC-INJ-REDIR-001, SEC-INJ-SSRF-001, SEC-VAL-ENCODING-001, SEC-VAL-FILE-001
- **Proposed disposition:** advisory (guidance only; no enforcement claim); pack affinity: software-engineering
- **Reviewer verdict:** _not reviewed_

## SEC-VAL-ENCODING-001

- **Domain / severity / mandatory:** security / high / false
- **Statement:** Input is decoded to canonical form (single decode, NFC unicode normalization) before validation. Double-decoding, accepting mixed encodings, or validating before decoding allows bypass through alternative encodings.
- **Trigger:** When validating any input that may have been URL-encoded, HTML-entity-encoded, or unicode-normalized differently from canonical form.
- **V1 enforcement text:** Code review. Look for validation followed by decoding/normalization rather than the reverse.
- **V1 mechanical path:** none
- **Rule id referenced in V1 code:** 0 file(s)
- **Rule id referenced in V1 tests:** 0 file(s)
- **Related rules (conflict candidates):** SEC-INJ-PATH-001, SEC-VAL-ALLOW-001, SEC-VAL-SERVER-001
- **Proposed disposition:** advisory (guidance only; no enforcement claim); pack affinity: software-engineering
- **Reviewer verdict:** _not reviewed_

## SEC-VAL-FILE-001

- **Domain / severity / mandatory:** security / critical / true
- **Statement:** File uploads must be validated by content (magic bytes / libmagic / file-type sniffing), not just by extension or client-supplied MIME type. Files are stored outside the web root, served through an application handler that sets safe Content-Type and Content-Disposition headers. Filename is sanitized or replaced with a UUID.
- **Trigger:** When accepting file uploads from users (avatars, attachments, documents, media).
- **V1 enforcement text:** Mechanically enforced by bin/run-analysis.sh::analyze_security_auth_authz: regex flags `request.files[...].save(`, `$request->file(...)->store(`, `multer({ dest: ... })`, and similar patterns to direct manual review.
- **V1 mechanical path:** bin/run-analysis.sh::analyze_security_auth_authz (resolves at baseline: yes)
- **Rule id referenced in V1 code:** 1 file(s): `bin/lib/analyzers-regex.sh`
- **Rule id referenced in V1 tests:** 1 file(s)
- **Related rules (conflict candidates):** SEC-HDR-TYPE-001, SEC-INJ-PATH-001, SEC-RATE-UPLOAD-001, SEC-VAL-ALLOW-001
- **Proposed disposition:** candidate_enforced (V1 binding resolves at baseline; must be rebound to a V2 validator); pack affinity: software-engineering
- **Reviewer verdict:** _not reviewed_

## SEC-VAL-LENGTH-001

- **Domain / severity / mandatory:** security / high / false
- **Statement:** All string inputs must have an explicit maximum length constraint. Unbounded text fields (free-form descriptions, comments, names) without a max_length are violations. The default for any new string field is bounded.
- **Trigger:** When defining schemas or accepting string-typed fields from user input.
- **V1 enforcement text:** Schema review. Look for `str`, `Optional[str]`, `TEXT`/`VARCHAR` columns without length constraints.
- **V1 mechanical path:** none
- **Rule id referenced in V1 code:** 0 file(s)
- **Rule id referenced in V1 tests:** 0 file(s)
- **Related rules (conflict candidates):** SEC-VAL-RANGE-001, SEC-VAL-TYPE-001
- **Proposed disposition:** advisory (guidance only; no enforcement claim); pack affinity: software-engineering
- **Reviewer verdict:** _not reviewed_

## SEC-VAL-RANGE-001

- **Domain / severity / mandatory:** security / medium / false
- **Statement:** Numeric inputs must be validated against the domain-appropriate range. Negative quantities, integer overflow values, and out-of-domain numbers (e.g., percentage > 100, year < 1900) are rejected at validation time.
- **Trigger:** When accepting numeric inputs (counts, IDs, prices, percentages, durations, sizes).
- **V1 enforcement text:** Schema review.
- **V1 mechanical path:** none
- **Rule id referenced in V1 code:** 0 file(s)
- **Rule id referenced in V1 tests:** 0 file(s)
- **Related rules (conflict candidates):** PROC-COMMIT-001, SEC-VAL-LENGTH-001
- **Proposed disposition:** advisory (guidance only; no enforcement claim); pack affinity: software-engineering
- **Reviewer verdict:** _not reviewed_

## SEC-VAL-REGEX-001

- **Domain / severity / mandatory:** security / medium / false
- **Statement:** User-supplied regular expressions must be rejected outright or executed under a non-backtracking engine (RE2, Hyperscan) with a time bound. The default Python `re`, Node `RegExp`, PHP `preg_match`, and Java `Pattern` engines are exposed to ReDoS.
- **Trigger:** When accepting regex patterns from end users (search, filter, advanced query, admin UI).
- **V1 enforcement text:** Code review. Look for `re.compile(user_input)` or equivalent patterns.
- **V1 mechanical path:** none
- **Rule id referenced in V1 code:** 0 file(s)
- **Rule id referenced in V1 tests:** 2 file(s)
- **Related rules (conflict candidates):** SEC-RATE-QUERY-001
- **Proposed disposition:** advisory (guidance only; no enforcement claim); pack affinity: software-engineering
- **Reviewer verdict:** _not reviewed_

## SEC-VAL-SERVER-001

- **Domain / severity / mandatory:** security / critical / true
- **Statement:** All input validation is performed server-side, before the data reaches business logic. Client-side validation exists for UX only and is never trusted. Trusting a custom HTTP header or a hidden form field as 'pre-validated' is a violation.
- **Trigger:** When accepting any data from a client (HTTP request, form, file upload, websocket message, mobile-app payload, third-party webhook).
- **V1 enforcement text:** Mechanically enforced by bin/run-analysis.sh::analyze_security_auth_authz: presence check flags handler functions that read req.body / request.json directly without an intervening schema-validation call. Detection is conservative; code review covers the rest.
- **V1 mechanical path:** bin/run-analysis.sh::analyze_security_auth_authz (resolves at baseline: yes)
- **Rule id referenced in V1 code:** 1 file(s): `bin/lib/analyzers-regex.sh`
- **Rule id referenced in V1 tests:** 1 file(s)
- **Related rules (conflict candidates):** SEC-AUTHZ-MASS-001, SEC-VAL-ENCODING-001, SEC-VAL-TYPE-001
- **Proposed disposition:** candidate_enforced (V1 binding resolves at baseline; must be rebound to a V2 validator); pack affinity: software-engineering
- **Reviewer verdict:** _not reviewed_
