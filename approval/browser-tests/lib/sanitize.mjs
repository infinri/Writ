// Redaction for everything the browser suite writes to its results file, its diagnostics log
// and its console (D-044 item 2): no session cookie, CSRF token, ceremony state, challenge,
// credential id, assertion or attestation bytes, key or token survives. Over-redaction is
// accepted; the self-test (sanitize-selftest.mjs) proves the known secret shapes are removed.

const R = "[REDACTED]";

// Names whose value is secret wherever they appear as `name=value`, `name: value` or
// `"name": value` (JSON, headers, form bodies, HTML attributes, query strings).
const SECRET_NAME = String.raw`[A-Za-z0-9_.-]*(?:session|csrf|token|cookie|challenge|ceremony|credential|secret|password|passwd|private|key|signature|rawid|userhandle|clientdatajson|attestationobject|authenticatordata|assertion|authorization|nonce|seed)[A-Za-z0-9_.-]*`;

const RULES = [
  // PEM blocks (keys, certificates).
  [/-----BEGIN [A-Z0-9 ]+-----[\s\S]*?-----END [A-Z0-9 ]+-----/g, `${R}-PEM`],
  // Authorization schemes.
  [/\b(Bearer|Basic|Digest)\s+[A-Za-z0-9._~+/=-]+/gi, `$1 ${R}`],
  // Cookie headers: every name=value pair after the header name.
  [/\b(set-cookie|cookie)(["']?\s*[:=]\s*["']?)[^\r\n"']*/gi, `$1$2${R}`],
  // `"name": "value"` / `"name": value` in JSON-like text.
  [new RegExp(String.raw`(["']${SECRET_NAME}["']\s*:\s*)("(?:[^"\\]|\\.)*"|'(?:[^'\\]|\\.)*'|\{[^{}]*\}|\[[^\[\]]*\]|[^\s,;}\]]+)`, "gi"), `$1"${R}"`],
  // `name="value"`, `name=value`, `name: value` (attributes, form bodies, query strings, headers).
  [new RegExp(String.raw`(\b${SECRET_NAME}\s*[:=]\s*)("(?:[^"\\]|\\.)*"|'(?:[^'\\]|\\.)*'|[^\s&;,"'}\]]+)`, "gi"), `$1${R}`],
  // Backstop: long base64, base64url or hex runs that mix character classes (random bytes,
  // fingerprints, credential ids, challenges). Plain lowercase words and paths are kept.
  [/[A-Za-z0-9+/_=-]{20,}/g, (m) => {
    const classes = [/[a-z]/, /[A-Z]/, /[0-9]/].filter((re) => re.test(m)).length;
    return classes >= 2 ? R : m;
  }],
];

/** Sanitized copy of `text` (non-strings are stringified first). */
export function sanitize(text) {
  let s = typeof text === "string" ? text : String(text);
  for (const [re, to] of RULES) s = s.replace(re, to);
  return s;
}

/** Deep copy of a JSON-like value with every string sanitized (keys are kept). */
export function sanitizeDeep(value) {
  if (typeof value === "string") return sanitize(value);
  if (Array.isArray(value)) return value.map(sanitizeDeep);
  if (value && typeof value === "object") {
    return Object.fromEntries(Object.entries(value).map(([k, v]) => [k, sanitizeDeep(v)]));
  }
  return value;
}
