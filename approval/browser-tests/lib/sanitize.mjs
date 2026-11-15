// Redaction for everything the browser suite writes to its results file, its diagnostics log
// and its console (D-044 item 2): no session cookie, CSRF token, ceremony state, challenge,
// credential id, assertion or attestation bytes, key or token survives. Some over-redaction
// is accepted; the self-test (sanitize-selftest.mjs) proves the known secret shapes are
// removed and the known false positives (tool and temporary paths, a plain lowercase word as
// the value of a secret-looking name, for example `credential: ledger`) are kept (D-044
// review LOW-7).

const R = "[REDACTED]";

// Names whose value is secret wherever they appear as `name=value`, `name: value` or
// `"name": value` (JSON, headers, form bodies, HTML attributes, query strings).
const SECRET_NAME = String.raw`[A-Za-z0-9_.-]*(?:session|csrf|token|cookie|challenge|ceremony|credential|secret|password|passwd|private|key|signature|rawid|userhandle|clientdatajson|attestationobject|authenticatordata|assertion|authorization|nonce|seed)[A-Za-z0-9_.-]*`;

// A plain lowercase word (no digit, no capital, no symbol) is never a secret value: random
// tokens of the suite are base64, base64url or hex and mix character classes.
const PLAIN_WORD = /^["']?[a-z]{1,24}["']?$/;
const keepWord = (redacted) => (m, name, value) => (PLAIN_WORD.test(value) ? m : `${name}${redacted}`);

// Paths (absolute, home-relative or dot-relative, with at least two separators) whose
// segment pieces are short or single-class are kept whole: tool paths, temporary
// directories, build outputs. A secret cannot hide in one: base64url has no `/`, and a
// base64 token is a whole path only when it starts with `/`, has no `+` and is not
// followed by `=` padding, with every piece at most 16 characters.
const PATH = /(?<=^|[\s"'`(=:,\[])(?:~|\.{1,2})?\/(?:[A-Za-z0-9._@-]+\/)+[A-Za-z0-9._@-]*(?=$|[\s"'`),;:\]}>])/g;
const pathLike = (p) => p.split("/").filter(Boolean).every((seg) => seg.split(/[-_.@]/).every((piece) =>
  piece.length <= 16 || [/[a-z]/, /[A-Z]/, /[0-9]/].filter((re) => re.test(piece)).length <= 1));

const RULES = [
  // PEM blocks (keys, certificates).
  [/-----BEGIN [A-Z0-9 ]+-----[\s\S]*?-----END [A-Z0-9 ]+-----/g, `${R}-PEM`],
  // Authorization schemes.
  [/\b(Bearer|Basic|Digest)\s+[A-Za-z0-9._~+/=-]+/gi, `$1 ${R}`],
  // Cookie headers: every name=value pair after the header name.
  [/\b(set-cookie|cookie)(["']?\s*[:=]\s*["']?)[^\r\n"']*/gi, `$1$2${R}`],
  // `"name": "value"` / `"name": value` in JSON-like text.
  [new RegExp(String.raw`(["']${SECRET_NAME}["']\s*:\s*)("(?:[^"\\]|\\.)*"|'(?:[^'\\]|\\.)*'|\{[^{}]*\}|\[[^\[\]]*\]|[^\s,;}\]]+)`, "gi"), keepWord(`"${R}"`)],
  // `name="value"`, `name=value`, `name: value` (attributes, form bodies, query strings, headers).
  [new RegExp(String.raw`(\b${SECRET_NAME}\s*[:=]\s*)("(?:[^"\\]|\\.)*"|'(?:[^'\\]|\\.)*'|[^\s&;,"'}\]]+)`, "gi"), keepWord(R)],
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
  // Keep path-like tokens out of the rules, then put them back.
  const kept = [];
  s = s.replace(PATH, (p) => {
    if (!pathLike(p)) return p;
    kept.push(p);
    return `\u0000${kept.length - 1}\u0000`;
  });
  for (const [re, to] of RULES) s = s.replace(re, to);
  return s.replace(/\u0000(\d+)\u0000/g, (_, i) => kept[Number(i)]);
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
