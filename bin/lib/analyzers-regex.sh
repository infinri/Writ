# analyzers-regex.sh -- the cross-language regex scanner for run-analysis.sh.
#
# Sourced by bin/run-analysis.sh (which owns `set -euo pipefail`); do NOT declare
# set -e here. analyze_all_regex_scanners is pure (file + lang argv -> stdout JSON)
# and self-contained (one quoted python3 heredoc).

# ── Cross-language regex scanners (merged, PART 2b) ─────────────────────────
# One python3 process runs all 6 SEC-INJ-*/SEC-AUTH*/SEC-CRYPTO-*/SEC-DATA-*/
# PERF-QUERY-001/SCALE-STATELESS-001 regex passes over a single file read.
analyze_all_regex_scanners() {
  local file="$1"
  local lang="$2"

  # PART 2b (PERF-IO-001 / DRY-DUP-003): the 6 cross-language regex
  # scanners (injection / auth-authz / crypto / data / perf N+1 / scaling)
  # run in ONE python3 process that reads the file ONCE. Each pass emits
  # its own findings under its own rule/tool prefix exactly as before.
  python3 - <<'PYEOF' "$file" "$lang"
import json, os, re, sys

file_path = sys.argv[1]
lang = sys.argv[2]
# Seed scripts contain literal example code from the public rulebook as
# documentation (rule.violation / rule.pass_example strings). Skip them.
basename = os.path.basename(file_path)
if basename.startswith("seed_") and basename.endswith(".py"):
    sys.exit(0)
try:
    with open(file_path, encoding="utf-8", errors="replace") as f:
        src = f.read()
except OSError:
    sys.exit(0)

lines = src.splitlines()

# ── Pass 1: injection (writ-injection-scan/) ───────────
def emit(line_no, rule, tool, message, severity="error"):
    print(json.dumps({
        "file": file_path,
        "line": line_no,
        "severity": severity,
        "rule": rule,
        "tool": "writ-injection-scan/" + tool,
        "message": message,
    }))

PATTERNS = [
    # SEC-INJ-SQL-001: SQL string concatenation / interpolation near execute()
    (r"\.execute\s*\(\s*f[\"']", "SEC-INJ-SQL-001", "sql-fstring",
     "f-string SQL in .execute(): use bound parameters instead", "error"),
    (r"\.execute\s*\([^)]*\+\s*['\"]?\s*\w", "SEC-INJ-SQL-001", "sql-concat",
     "string concatenation in .execute(): use bound parameters instead", "error"),
    (r"\.raw\s*\(\s*f[\"']", "SEC-INJ-SQL-002", "orm-raw-fstring",
     "f-string in ORM .raw(): pass parameters as a list/dict", "error"),
    # SEC-INJ-XSS-001/002: dangerous render APIs
    (r"dangerouslySetInnerHTML", "SEC-INJ-XSS-002", "react-unsafe-html",
     "dangerouslySetInnerHTML: render as text or sanitize via DOMPurify", "error"),
    (r"\bv-html\s*=", "SEC-INJ-XSS-002", "vue-unsafe-html",
     "v-html: render as text or sanitize", "error"),
    (r"\{!![^!]+!!\}", "SEC-INJ-XSS-002", "blade-unsafe",
     "Blade {!! !!} raw output: use {{ }} or sanitize", "error"),
    (r"\{@html\b", "SEC-INJ-XSS-002", "svelte-unsafe-html",
     "Svelte {@html}: sanitize before rendering", "error"),
    # SEC-INJ-XSS-003: vanilla DOM mutation with HTML
    (r"\.innerHTML\s*=", "SEC-INJ-XSS-003", "dom-innerhtml",
     "innerHTML assignment: use textContent or framework-rendered nodes", "error"),
    (r"\.outerHTML\s*=", "SEC-INJ-XSS-003", "dom-outerhtml",
     "outerHTML assignment: replace via createElement instead", "error"),
    (r"document\.write\s*\(", "SEC-INJ-XSS-003", "document-write",
     "document.write(): use DOM API instead", "error"),
    # SEC-INJ-CMD-001: shell command construction
    (r"subprocess\.(run|Popen|call|check_output|check_call)\s*\([^)]*shell\s*=\s*True", "SEC-INJ-CMD-001", "subprocess-shell",
     "subprocess with shell=True: pass argument list instead", "error"),
    (r"\bos\.system\s*\(", "SEC-INJ-CMD-001", "os-system",
     "os.system(): use subprocess with argument list", "error"),
    (r"\bos\.popen\s*\(", "SEC-INJ-CMD-001", "os-popen",
     "os.popen(): use subprocess.run with argument list", "error"),
    (r"child_process\.exec(?:Sync)?\s*\(", "SEC-INJ-CMD-001", "node-exec",
     "child_process.exec(): use execFile or spawn with argument list", "error"),
    # PHP shell exec functions
    (r"\bshell_exec\s*\(", "SEC-INJ-CMD-001", "php-shell-exec",
     "shell_exec(): use escapeshellarg or argument-list invocation", "error"),
    (r"\bpassthru\s*\(", "SEC-INJ-CMD-001", "php-passthru",
     "passthru(): pass constant command, escape arguments", "error"),
    # SEC-INJ-CMD-002: eval / dynamic-code evaluators
    (r"\beval\s*\(", "SEC-INJ-CMD-002", "eval",
     "eval(): use a lookup table or dispatch dict instead", "error"),
    (r"new\s+Function\s*\(", "SEC-INJ-CMD-002", "new-function",
     "new Function(): replace with a lookup table or registry", "error"),
    # SEC-INJ-DESER-001: insecure deserialization
    (r"\bpickle\.loads?\s*\(", "SEC-INJ-DESER-001", "pickle",
     "pickle.loads(): never deserialize untrusted data; use JSON or typed schema", "error"),
    (r"\byaml\.load\s*\((?![^)]*Loader\s*=\s*\w*SafeLoader)", "SEC-INJ-DESER-001", "yaml-unsafe",
     "yaml.load() without SafeLoader: use yaml.safe_load() instead", "error"),
    (r"\bunserialize\s*\(", "SEC-INJ-DESER-001", "php-unserialize",
     "PHP unserialize(): use json_decode or typed schema", "error"),
    (r"ObjectInputStream", "SEC-INJ-DESER-001", "java-objectinputstream",
     "Java ObjectInputStream: use a typed schema (JSON/Protobuf)", "error"),
    # SEC-INJ-SSTI-001: server-side template injection
    (r"\bTemplate\s*\(\s*(?!['\"])", "SEC-INJ-SSTI-001", "template-dynamic",
     "Template() with non-literal argument: never pass user input as template body", "error"),
    # SEC-INJ-LOG-001: log injection
    # (advisory severity in the source; flagged but not error)
    (r"(logger|logging)\.[a-z]+\s*\(\s*f[\"'][^\"']*\{[a-zA-Z_]", "SEC-INJ-LOG-001", "log-fstring",
     "f-string log with embedded values: use structured logger fields", "warning"),
]

for line_no, line in enumerate(lines, start=1):
    # Skip lines that look like comments to reduce false positives on
    # documentation examples within the rules themselves. Comment markers:
    # Python #, JS/Java/PHP //, /* ... */ ranges are not tracked here.
    stripped = line.lstrip()
    if stripped.startswith(("#", "//", "*")):
        continue
    for pat, rule, tool, msg, severity in PATTERNS:
        if re.search(pat, line):
            emit(line_no, rule, tool, msg, severity)

# ── Pass 2: auth/authz/validation (writ-auth-scan/) ────
def emit(line_no, rule, tool, message, severity="error"):
    print(json.dumps({
        "file": file_path,
        "line": line_no,
        "severity": severity,
        "rule": rule,
        "tool": "writ-auth-scan/" + tool,
        "message": message,
    }))

# Proximity helper: does the rest of the file mention a marker within
# `window` lines of `line_no` (1-indexed)? Used for password/token context.
def near(line_no, marker_re, window=2):
    lo = max(0, line_no - 1 - window)
    hi = min(len(lines), line_no - 1 + window + 1)
    return any(re.search(marker_re, lines[i]) for i in range(lo, hi))

PASSWORD_CTX = re.compile(r"\b(password|passwd|pwd|passhash)\b", re.IGNORECASE)
TOKEN_CTX = re.compile(
    r"\b(token|session|secret|csrf|nonce|reset|verify|api_?key|sessid|salt|seed)\b",
    re.IGNORECASE,
)

# Direct-match patterns -- no proximity needed.
DIRECT = [
    # SEC-AUTHZ-MASS-001: mass assignment from request body
    (r"\b\w+\s*\(\s*\*\*\s*request\.(?:json|body|POST|data|args)", "SEC-AUTHZ-MASS-001", "mass-assign-py",
     "Mass assignment from request body: validate through a schema or permit/allowlist first", "error"),
    (r"Object\.assign\s*\(\s*\w+\s*,\s*req\.body\b", "SEC-AUTHZ-MASS-001", "mass-assign-node",
     "Object.assign(model, req.body): use an allowlist or schema validator", "error"),
    (r"\.update_attributes\s*\(\s*params\b", "SEC-AUTHZ-MASS-001", "mass-assign-rails",
     "update_attributes(params): use strong parameters (.permit(...))", "error"),
    (r"new\s+\w+\s*\(\s*req\.body\s*\)", "SEC-AUTHZ-MASS-001", "mass-assign-node-ctor",
     "new Model(req.body): validate through a schema first", "error"),
    # SEC-VAL-FILE-001: file upload to web root or without content check
    (r"request\.files\s*\[[^\]]+\]\.save\s*\(", "SEC-VAL-FILE-001", "upload-save-flask",
     "request.files[...].save(): verify by magic bytes + store outside web root + use sanitized filename", "error"),
    (r"\$request\s*->\s*file\s*\([^)]*\)\s*->\s*(?:store|move|save)\s*\(", "SEC-VAL-FILE-001", "upload-save-laravel",
     "$request->file(...)->store/move: verify by magic bytes and use random storage name", "error"),
    (r"multer\s*\(\s*\{[^}]*dest\s*:", "SEC-VAL-FILE-001", "upload-multer",
     "multer({ dest: ... }): also verify mime/magic bytes server-side", "error"),
]

# Context-sensitive patterns -- only flag when accompanying identifier is nearby.
CONTEXT = [
    # SEC-AUTH-HASH-001: weak password hash
    (r"\bhashlib\.(md5|sha1|sha256|sha512)\s*\(", "SEC-AUTH-HASH-001", "weak-hash-py",
     PASSWORD_CTX, 2,
     "Weak hash on password: use bcrypt, argon2, or scrypt", "error"),
    (r"\bcrypto\.createHash\s*\(\s*['\"](md5|sha1|sha256|sha512)['\"]", "SEC-AUTH-HASH-001", "weak-hash-node",
     PASSWORD_CTX, 2,
     "Weak hash on password: use bcrypt, argon2, or scrypt", "error"),
    (r"\b(md5|sha1|hash)\s*\(\s*\$\w*pass", "SEC-AUTH-HASH-001", "weak-hash-php",
     PASSWORD_CTX, 0,
     "Weak hash on password: use password_hash() with PASSWORD_BCRYPT or PASSWORD_ARGON2ID", "error"),
    # SEC-AUTH-TOKEN-001: non-CSPRNG for security-sensitive value
    (r"\bMath\.random\s*\(\s*\)", "SEC-AUTH-TOKEN-001", "weak-rng-node",
     TOKEN_CTX, 2,
     "Math.random() for security-sensitive value: use crypto.randomBytes()", "error"),
    (r"\brandom\.(random|choice|choices|randint|randrange|uniform)\s*\(", "SEC-AUTH-TOKEN-001", "weak-rng-py",
     TOKEN_CTX, 2,
     "random.* for security-sensitive value: use secrets.token_urlsafe() or secrets.token_bytes()", "error"),
    (r"\b(mt_rand|rand|srand)\s*\(", "SEC-AUTH-TOKEN-001", "weak-rng-php",
     TOKEN_CTX, 2,
     "mt_rand/rand for security-sensitive value: use random_bytes() or random_int()", "error"),
]

# Heuristic structural checks (file-level rather than per-line).
# SEC-AUTHZ-ENFORCE-001 / SEC-AUTHZ-DEFAULT-001: route handlers without an
# explicit authorization decorator/dependency.
#
# Auditable opt-out: a module-level "# writ-auth-scan: internal-service"
# marker suppresses the missing-auth finding for the WHOLE file. It exists
# for services that intentionally run without per-route auth because they
# are not externally reachable (e.g. the Writ session daemon bound to
# localhost:8765, whose endpoints are called by local hooks). The marker is
# self-documenting and greppable, so applying it to an externally-exposed
# service is visible in review. Every other security analyzer (secrets,
# weak hash/RNG, mass-assignment, unvalidated upload/body) still runs on the
# file -- only the route-auth heuristic is suppressed.
AUTH_INTERNAL_MARKER = re.compile(
    r"#\s*writ-auth-scan:\s*internal-service", re.IGNORECASE,
)
ROUTE_DECORATOR = re.compile(
    r"@(?:app|router|bp|blueprint)\.(?:route|get|post|put|delete|patch)\s*\("
    r"|@router\.(?:get|post|put|delete|patch)\s*\("
    r"|@app\.(?:get|post|put|delete|patch)\s*\("
)
AUTH_GUARD = re.compile(
    r"@(?:login_required|admin_required|permission_required|authentication_required|authorize|policy)"
    r"|Depends\s*\(\s*(?:get_current_user|get_current_active_user|require_admin|require_auth|require_role|verify_token|oauth2_scheme)"
    r"|permission_classes\s*=\s*\[",
    re.IGNORECASE,
)
_auth_scan_suppressed = bool(AUTH_INTERNAL_MARKER.search(src))
for i, line in enumerate(lines):
    if ROUTE_DECORATOR.search(line) and not _auth_scan_suppressed:
        window = "\n".join(lines[max(0, i - 1): min(len(lines), i + 6)])
        if not AUTH_GUARD.search(window):
            emit(i + 1, "SEC-AUTHZ-ENFORCE-001", "missing-auth-decorator",
                 "Route handler without explicit auth check (login_required / Depends(auth) / permission_classes): "
                 "endpoints default to deny per SEC-AUTHZ-DEFAULT-001", "error")

# SEC-VAL-SERVER-001: handler reads request body without an intervening
# schema validation call. Conservative: flag handlers that read
# request.json/body and never call .validate( or a *Schema/*Model constructor.
HANDLER_HEADER = re.compile(r"def\s+\w+\s*\(.*\)\s*:\s*$")
BODY_ACCESS = re.compile(r"request\.(?:json|body|POST|data|args)|req\.body")
SCHEMA_CALL = re.compile(
    r"\.validate\s*\(|"
    r"\b[A-Z]\w+(?:Create|Update|Schema|Model|Payload|Request|Input|Dto)\s*\(|"
    r"BaseModel|pydantic|Marshmallow|joi\.|Joi\.|zod\.|z\.object\b"
)
i = 0
while i < len(lines):
    if HANDLER_HEADER.search(lines[i]):
        body_end = i + 1
        while body_end < len(lines):
            stripped = lines[body_end]
            if stripped and not stripped.startswith((" ", "\t")) and not stripped.startswith(("#", "//")):
                break
            body_end += 1
        body_block = "\n".join(lines[i: body_end])
        if BODY_ACCESS.search(body_block) and not SCHEMA_CALL.search(body_block):
            emit(i + 1, "SEC-VAL-SERVER-001", "unvalidated-body",
                 "Handler reads request body without schema validation: validate through a typed schema "
                 "(Pydantic/Marshmallow/Zod/Joi) before business logic", "warning")
        i = body_end
    else:
        i += 1

# Apply per-line patterns.
for line_no, line in enumerate(lines, start=1):
    stripped = line.lstrip()
    if stripped.startswith(("#", "//", "*")):
        continue
    for pat, rule, tool, msg, severity in DIRECT:
        if re.search(pat, line):
            emit(line_no, rule, tool, msg, severity)
    for pat, rule, tool, ctx_re, window, msg, severity in CONTEXT:
        if re.search(pat, line) and near(line_no, ctx_re, window):
            emit(line_no, rule, tool, msg, severity)

# ── Pass 3: crypto/headers (writ-crypto-scan/) ─────────
def emit(line_no, rule, tool, message, severity="error"):
    print(json.dumps({
        "file": file_path,
        "line": line_no,
        "severity": severity,
        "rule": rule,
        "tool": "writ-crypto-scan/" + tool,
        "message": message,
    }))

def near(line_no, marker_re, window=2):
    lo = max(0, line_no - 1 - window)
    hi = min(len(lines), line_no - 1 + window + 1)
    return any(re.search(marker_re, lines[i]) for i in range(lo, hi))

# SEC-CRYPTO-KEY-001: hardcoded secret patterns.
SECRET_PATTERNS = [
    (r"['\"](sk_live_[A-Za-z0-9]{16,})['\"]", "stripe-live", "Stripe live secret in source"),
    (r"['\"](sk_test_[A-Za-z0-9]{16,})['\"]", "stripe-test", "Stripe test secret in source (rotate before production)"),
    (r"['\"](AKIA[0-9A-Z]{16})['\"]", "aws-access-key", "AWS access key ID in source"),
    (r"['\"](xox[baprs]-[A-Za-z0-9-]{10,})['\"]", "slack-token", "Slack token in source"),
    (r"['\"](ghp_[A-Za-z0-9]{20,})['\"]", "github-pat", "GitHub personal access token in source"),
    (r"['\"](gho_[A-Za-z0-9]{20,})['\"]", "github-oauth", "GitHub OAuth token in source"),
    (r"-----BEGIN (RSA |DSA |EC |OPENSSH |PGP )?PRIVATE KEY-----", "pem-private-key",
     "PEM-encoded private key in source"),
]

# Identifier-based: API_KEY = "...", SECRET = "...", PASSWORD = "...", etc.
#
# THE PREFIX IS PART OF THE LITERAL. Requiring the quote immediately after the
# assignment let every prefixed string through with a real secret inside it:
# `API_TOKEN = f"<secret>"`, `r"<secret>"`, `rb"<secret>"` and C#'s `@"<secret>"`
# all read as no-match while the bare form was caught. At most two characters,
# letters/underscore/$/@ only: `(` and `[` are excluded on purpose, because
# accepting them would match `API_TOKEN = os.environ.get("MY_SERVICE_TOKEN")`,
# where the captured value is an env var NAME, and make every correct credential
# lookup a finding.
IDENT_ASSIGN = re.compile(
    r"\b(?:[A-Z_]*(?:API_?KEY|SECRET|PASSWORD|TOKEN|PRIVATE_?KEY|ACCESS_?KEY|AUTH_?KEY)[A-Z_]*)"
    r"\s*[:=]\s*[A-Za-z_$@]{0,2}['\"]([^'\"]{8,})['\"]"
)
# Lowercase variant (Python attribute / dict key assignment).
IDENT_ASSIGN_LOWER = re.compile(
    r"\b(?:api_key|secret|password|token|private_key|access_key|auth_key)\b"
    r"\s*[:=]\s*[A-Za-z_$@]{0,2}['\"]([^'\"]{8,})['\"]",
    re.IGNORECASE,
)

# Allowlist: obvious placeholders the linter should not flag.
#
# ANCHORED AT BOTH ENDS, and that is the point of this shape. This used to be a
# `^`-anchored pattern consulted with `.match()`, which anchors only the START of
# the captured value, so six characters of decoy laundered an arbitrary real
# secret: `API_TOKEN = "{name}<real secret>"` and the `<placeholder><real secret>`
# form were both ADMITTED, at any length, any case, any entropy, while the same
# secret bare was flagged. A placeholder has to be a placeholder ALL THE WAY
# THROUGH, not a placeholder glued to a payload. Consulted with `fullmatch`.
#
# TAIL is the alphabet a placeholder may continue in: lowercase, digits, `.`,
# `_`, `-`. Deliberately NO uppercase, which is what refuses
# `"test" + <mixed-case secret>`, and that is why the words below are made
# case-insensitive through a SCOPED `(?i:...)` group instead of a global
# re.IGNORECASE flag -- a global flag makes `[a-z]` match uppercase too and
# re-opens the hole it is here to close.
#
# TWO SHAPES, not interchangeable:
#
#  * WORD entries stay PREFIX matches followed by TAIL, because `"test-mode-engine"`
#    and `"your-api-key-here"` are real admitted values in this tree.
#  * BRACKET entries are TEMPLATE shapes. A value is a template when it is made of
#    `<...>` / `{...}` interpolations and TAIL glue and nothing else, with at least
#    one interpolation present: `"{session_cache}"` (bin/lib/test_paths.py:150) and
#    `f"{_TEST_SCOPE}-"` (tests/test_decision_memory_capture.py:86) qualify;
#    `"{name}<real secret>"` does not, because the payload sits outside both the
#    braces and TAIL. A plain `fullmatch` of `\{.+\}` would have refused
#    `{_TEST_SCOPE}-` and broken an already-wired module, which is why the template
#    form is a segment grammar rather than one greedy `.+`. The INSIDE of an
#    interpolation is unrestricted on purpose: it is a variable name the source
#    already contains, never the literal's payload.
_PLACEHOLDER_TAIL = r"[a-z0-9._-]"
_PLACEHOLDER_WORD = (
    r"(?i:your[-_]?|example|placeholder|change[-_]?me|fake|dummy|test|sample|todo|xxx+|\.\.\.)"
)
_TEMPLATE_SEGMENT = r"(?:<[^<>]*>|\{[^{}]*\})"
PLACEHOLDER = re.compile(
    "(?:"
    + _PLACEHOLDER_WORD + _PLACEHOLDER_TAIL + "*"
    + "|"
    + _PLACEHOLDER_TAIL + "*" + _TEMPLATE_SEGMENT
    + "(?:" + _TEMPLATE_SEGMENT + "|" + _PLACEHOLDER_TAIL + ")*"
    + ")"
)

# A NAMESPACE PREFIX, not a credential. It BEGINS with a lowercase letter, so a
# value opening on a digit (`3f9a2b7c...-`) is still caught; the rest is lowercase
# letters, digits, hyphen and underscore; and it ENDS in a bare separator, which is
# the property that does the most work: a generated secret never ends in a trailing
# `-` or `_`. Measured against
# 200,000 freshly generated samples of each of six realistic secret shapes (hex32,
# base64url, mixed password, AWS access key id, AWS 40-char secret, uuid), 1.2M in
# total: zero matches. It admits `phase3b-`, `approve-`, `evidence-`,
# `reviewpromote-` and `advance-from-complete-`, the namespace prefixes this repo's
# own test modules declare, and it still flags every weak-but-real password,
# including `aaaaaaaa` and `password123456`, because none of those ends in a
# separator.
#
# ENTROPY WAS TRIED AND IS DISPROVEN, do not reintroduce it: `password123456` and
# `advance-from-complete-` tie exactly at 3.664 bits/char, and `aaaaaaaa` (0.000)
# sits below every prefix here, so the populations overlap in both directions.
#
# THE DISCLOSED BLIND SPOT: a real secret hand-crafted as `word-word-` is exempted
# by this. See docs/adr/ADR-credential-literal-value-shape.md.
#
# `fullmatch`, not `match` with a `$`: `$` also matches before a trailing newline,
# and the whole value has to satisfy the shape for the judgment to hold.
NAMESPACE_PREFIX = re.compile(r"[a-z][a-z0-9_-]*[-_]")

# SEC-CRYPTO-RAND-001: non-CSPRNG near crypto-specific identifiers.
CRYPTO_RNG = [
    (r"\bMath\.random\s*\(\s*\)", "weak-rng-crypto-node",
     "Math.random() near crypto identifier: use crypto.randomBytes()"),
    (r"\brandom\.(random|choice|choices|randint|randrange|uniform|getrandbits)\s*\(",
     "weak-rng-crypto-py",
     "random.* near crypto identifier: use secrets.token_bytes() or os.urandom()"),
    (r"\b(mt_rand|rand|srand)\s*\(", "weak-rng-crypto-php",
     "mt_rand/rand near crypto identifier: use random_bytes() or random_int()"),
]
CRYPTO_CTX = re.compile(
    r"\b(iv|nonce|salt|aes|gcm|cbc|aead|encrypt|cipher|hmac|signing[_-]?key)\b",
    re.IGNORECASE,
)

# SEC-CRYPTO-CERT-001: disabled cert verification.
CERT_DISABLE = [
    (r"\bverify\s*=\s*False\b", "verify-false-py",
     "verify=False disables TLS cert validation -- forbidden outside scoped local-dev paths"),
    (r"rejectUnauthorized\s*:\s*false", "reject-unauth-node",
     "rejectUnauthorized: false disables TLS cert validation"),
    (r"InsecureSkipVerify\s*:\s*true", "insecure-skip-go",
     "InsecureSkipVerify: true disables TLS cert validation"),
    (r"CURLOPT_SSL_VERIFYPEER\s*,\s*(false|0|FALSE)", "curl-verifypeer-php",
     "CURLOPT_SSL_VERIFYPEER false disables TLS cert validation"),
]

# SEC-CRYPTO-ALGO-001: forbidden symmetric algorithms and modes.
WEAK_CIPHER = [
    (r"AES\.MODE_ECB\b", "aes-ecb",
     "AES ECB mode: pattern-leaking; use AES-GCM or ChaCha20-Poly1305"),
    (r"\b(?:DES|TripleDES|3DES)\.new\s*\(", "des-cipher",
     "DES/3DES symmetric cipher: forbidden; use AES-256-GCM"),
    (r"\bARC4\.new\s*\(", "rc4-cipher", "RC4 cipher: forbidden"),
]

for line_no, line in enumerate(lines, start=1):
    stripped = line.lstrip()
    if stripped.startswith(("#", "//", "*")):
        continue

    # Hardcoded-secret patterns.
    for pat, tool, msg in SECRET_PATTERNS:
        if re.search(pat, line):
            emit(line_no, "SEC-CRYPTO-KEY-001", tool, msg, "error")

    # Identifier-based credential assignment.
    for ident_re in (IDENT_ASSIGN, IDENT_ASSIGN_LOWER):
        m = ident_re.search(line)
        if m:
            literal = m.group(1)
            if not PLACEHOLDER.fullmatch(literal) and not NAMESPACE_PREFIX.fullmatch(literal):
                emit(line_no, "SEC-CRYPTO-KEY-001", "credential-literal-assign",
                     f"Credential assigned to string literal in source (load from env or secrets manager)",
                     "error")

    # CSPRNG context: only flag when crypto-specific identifier is nearby.
    for pat, tool, msg in CRYPTO_RNG:
        if re.search(pat, line) and near(line_no, CRYPTO_CTX, window=2):
            emit(line_no, "SEC-CRYPTO-RAND-001", tool, msg, "error")

    # Cert validation disable.
    for pat, tool, msg in CERT_DISABLE:
        if re.search(pat, line):
            emit(line_no, "SEC-CRYPTO-CERT-001", tool, msg, "error")

    # Weak symmetric algorithms.
    for pat, tool, msg in WEAK_CIPHER:
        if re.search(pat, line):
            emit(line_no, "SEC-CRYPTO-ALGO-001", tool, msg, "error")

# ── Pass 4: data-protection (writ-data-scan/) ──────────
def emit(line_no, rule, tool, message, severity="error"):
    print(json.dumps({
        "file": file_path,
        "line": line_no,
        "severity": severity,
        "rule": rule,
        "tool": "writ-data-scan/" + tool,
        "message": message,
    }))

# Logger call shapes across languages.
LOGGER_CALL = re.compile(
    r"\b(?:logger|logging|log|console|Log)\.(?:debug|info|warn(?:ing)?|error|fatal|trace|exception)\s*\("
    r"|\bprint\s*\("
    r"|\bSystem\.out\.println\s*\("
    r"|\berror_log\s*\("
)
# PII-shaped identifiers / keys.
PII_IDENT = re.compile(
    r"\b(?:e?mail(?:_?address)?|phone(?:_?number)?|ssn|sin|nin|social_?security|"
    r"dob|date_?of_?birth|street_?address|home_?address|"
    r"credit_?card(?:_?number)?|cc_?number|card_?pan|cvv|"
    r"passport(?:_?number)?|drivers_?license|tax_?id|national_?id)\b",
    re.IGNORECASE,
)
# Allowlist: hashed/redacted/masked variants are safe.
SAFE_SUFFIX = re.compile(r"_(hash|hashed|digest|redacted|masked|fingerprint|token)\b", re.IGNORECASE)

for line_no, line in enumerate(lines, start=1):
    stripped = line.lstrip()
    if stripped.startswith(("#", "//", "*")):
        continue
    if LOGGER_CALL.search(line):
        for m in PII_IDENT.finditer(line):
            start = m.start()
            tail = line[start: start + 60]
            if SAFE_SUFFIX.search(tail):
                continue
            emit(line_no, "SEC-DATA-PII-001", "pii-in-log",
                 f"Logger call references PII identifier '{m.group(0)}': redact, hash, or omit before logging",
                 "error")
            break  # one finding per line

# ── Pass 5: N+1 performance (writ-perf-scan/) ──────────
def emit(line_no, rule, tool, message, severity="warning"):
    print(json.dumps({
        "file": file_path,
        "line": line_no,
        "severity": severity,
        "rule": rule,
        "tool": "writ-perf-scan/" + tool,
        "message": message,
    }))

# Loop headers across languages.
FOR_LOOP = re.compile(
    r"\bfor\s+(\w+)\s+in\s+|"
    r"\bforeach\s*\(\s*\$\w+\s+as\s+\$(\w+)\s*\)|"
    r"\.forEach\s*\(\s*(?:\(?\s*(\w+)|function\s*\(\s*(\w+))|"
    r"\.map\s*\(\s*(?:\(?\s*(\w+)|function\s*\(\s*(\w+))"
)
# ORM/DB access methods that suggest a query.
DB_ACCESS = re.compile(
    r"\.(query|filter|filter_by|get|first|find|where|raw|execute|fetch|fetchall|fetchone|"
    r"objects\.get|objects\.filter|select|update|find_one|find_by_id|"
    r"findOne|findById|findOneBy)\s*\("
)

for i, line in enumerate(lines):
    stripped = line.lstrip()
    if stripped.startswith(("#", "//", "*")):
        continue
    m = FOR_LOOP.search(line)
    if not m:
        continue
    loop_var = next((g for g in m.groups() if g), None)
    if not loop_var:
        continue
    # Inspect next 10 lines for DB access that references the loop variable.
    indent = len(line) - len(line.lstrip())
    for j in range(i + 1, min(len(lines), i + 12)):
        body_line = lines[j]
        if not body_line.strip():
            continue
        # Stop if dedented out of the loop body.
        body_indent = len(body_line) - len(body_line.lstrip())
        if body_indent <= indent and body_line.strip():
            break
        if DB_ACCESS.search(body_line) and re.search(rf"\b{re.escape(loop_var)}\b", body_line):
            emit(j + 1, "PERF-QUERY-001", "loop-with-db-access",
                 f"Possible N+1: loop variable '{loop_var}' used in DB call inside loop body. "
                 f"Consider joins, eager loading, or batch fetching.",
                 "warning")
            break

# ── Pass 6: scaling / stateless (writ-scale-scan/) ─────
def emit(line_no, rule, tool, message, severity="warning"):
    print(json.dumps({
        "file": file_path,
        "line": line_no,
        "severity": severity,
        "rule": rule,
        "tool": "writ-scale-scan/" + tool,
        "message": message,
    }))

# Module-level (indent == 0) mutable global with a state-suggesting name.
STATE_NAME = re.compile(
    r"^([A-Z_]*(?:USER|SESSION|CART|LOGIN|TOKEN|AUTH|CACHE|STORE|STATE)[A-Z_]*"
    r"|_?(?:user_?cache|session_?store|user_?carts|sessions|carts|active_users|user_state))\s*"
    r"(?::\s*[^=]+)?\s*=\s*(\{|\[|set\(\)|dict\(\)|list\(\)|defaultdict|OrderedDict|deque)",
    re.IGNORECASE,
)

for line_no, line in enumerate(lines, start=1):
    # Module-level only -- no leading whitespace.
    if line[:1] in (" ", "\t"):
        continue
    stripped = line.lstrip()
    if stripped.startswith(("#", "//", "*")):
        continue
    m = STATE_NAME.match(line)
    if m:
        emit(line_no, "SCALE-STATELESS-001", "module-state-global",
             f"Module-level mutable global '{m.group(1)}' suggests in-process user/session state; "
             f"move to external store (Redis, DB) for horizontal scaling.",
             "warning")

PYEOF
}
