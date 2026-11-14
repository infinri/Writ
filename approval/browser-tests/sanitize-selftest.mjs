// Self-test of lib/sanitize.mjs (D-044 item 2): every known secret shape the suite can meet
// is redacted, and the words the results need (flow names, statuses, versions) are kept.
//   node approval/browser-tests/sanitize-selftest.mjs     (exit 0 only if every case holds)

import crypto from "node:crypto";

import { sanitize, sanitizeDeep } from "./lib/sanitize.mjs";

const b64u = (n) => crypto.randomBytes(n).toString("base64url");
const b64 = (n) => crypto.randomBytes(n).toString("base64");
const hex = (n) => crypto.randomBytes(n).toString("hex");

// Each case: [label, text containing the secret, the secret value(s) that must disappear].
const secrets = () => {
  const cookie = b64u(32), csrf = b64u(32), challenge = b64u(32), credId = b64u(16), sig = b64u(70),
    cdj = b64u(120), att = b64u(200), auth = b64u(37), uh = b64u(16), ceremony = hex(16), fp = hex(32),
    bearer = b64u(24), pem = b64(64), rawB64 = b64(32), rawHex = hex(20), sess = hex(16);
  return [
    ["session cookie header", `cookie: writ_session=${cookie}`, [cookie]],
    ["Set-Cookie header", `set-cookie: writ_session=${cookie}; Path=/; HttpOnly; SameSite=Strict`, [cookie]],
    ["cookie in a JS object dump", `{"set-cookie":["writ_session=${cookie}; Path=/"]}`, [cookie]],
    ["bare session cookie pair", `sent writ_session=${cookie} to /view`, [cookie]],
    ["CSRF attribute", `<main id="page" data-csrf="${csrf}" data-fingerprint="${fp}">`, [csrf, fp]],
    ["CSRF form field", `verdict=approve&csrf=${csrf}&subject=browser-approver`, [csrf]],
    ["CSRF JSON field", `{"csrf_token":"${csrf}"}`, [csrf]],
    ["challenge JSON", `{"publicKey":{"challenge":"${challenge}","rpId":"localhost","userVerification":"required"}}`, [challenge]],
    ["challenge unquoted", `challenge: ${challenge}`, [challenge]],
    ["ceremony id", `{"ceremony":"${ceremony}","options":{}}`, [ceremony]],
    ["ceremony state field", `ceremony_state=${ceremony}`, [ceremony]],
    ["assertion credential", JSON.stringify({ id: credId, rawId: credId, type: "public-key",
      response: { authenticatorData: auth, clientDataJSON: cdj, signature: sig, userHandle: uh } }), [credId, auth, cdj, sig, uh]],
    ["attestation credential", JSON.stringify({ id: credId, rawId: credId,
      response: { attestationObject: att, clientDataJSON: cdj } }), [credId, att, cdj]],
    ["credential id field", `credential_id=${credId}`, [credId]],
    ["CDP credential listing", JSON.stringify({ credentialId: credId, privateKey: b64(90).slice(0, 40) + rawB64, signCount: 2 }), [credId, rawB64]],
    ["PEM key", `-----BEGIN PRIVATE KEY-----\n${pem}\n-----END PRIVATE KEY-----`, [pem]],
    ["bearer token", `authorization: Bearer ${bearer}`, [bearer]],
    ["token in a query string", `GET /view?token=${bearer}&x=1`, [bearer]],
    ["session id field", `"session_id": "${sess}"`, [sess]],
    ["bare random base64url", `relay ${challenge} to the other port`, [challenge]],
    ["bare random base64", `bytes ${rawB64}`, [rawB64]],
    ["bare hex fingerprint", `fingerprint mismatch ${fp} vs stored`, [fp]],
    ["bare hex id", `request ${rawHex}`, [rawHex]],
  ];
};

const kept = [
  "approve via page",
  "user verification false refused by the server (client options tampered)",
  "signoff page refuses an id with no pending signoff (generic, no session)",
  "approve-deny: ledger status of req-approve",
  "Google Chrome 150.0.7871.46",
  "v22.22.3",
  "webauthn-authenticator-rs",
  "approved", "denied", "pending", "ASSERTION_INVALID",
  "target/release/examples/browser_fixture",
];

let failures = 0;
const fail = (msg) => { failures++; console.error(`sanitize self-test: FAIL ${msg}`); };
let cases = 0;
for (let round = 0; round < 25; round++) {
  for (const [label, text, values] of secrets()) {
    cases++;
    const out = sanitize(text);
    for (const v of values) if (out.includes(v)) fail(`${label}: secret survived in ${JSON.stringify(out)}`);
    if (!out.includes("[REDACTED]")) fail(`${label}: no redaction marker in ${JSON.stringify(out)}`);
    const deep = JSON.stringify(sanitizeDeep({ nested: [{ text }] }));
    for (const v of values) if (deep.includes(v)) fail(`${label}: secret survived sanitizeDeep`);
  }
}
for (const k of kept) {
  cases++;
  if (sanitize(k) !== k) fail(`kept text changed: ${JSON.stringify(k)} -> ${JSON.stringify(sanitize(k))}`);
}
console.log(`sanitize self-test: ${cases - failures} of ${cases} cases held`);
process.exit(failures === 0 ? 0 : 1);
