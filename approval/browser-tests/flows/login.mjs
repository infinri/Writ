// Read-session login helpers of the browser flows (D-044 item 3, M4-INTERFACES Z1..Z4).
// Every page of the approval listener needs an authenticated read session: the landing page
// `GET /` offers the login form; `POST /login/begin` and `POST /login/finish` run a
// purpose-separated WebAuthn ceremony (purpose `read`) and rotate the session token. User
// verification is SIMULATED by the CDP virtual authenticator (no real user, no real
// credential).

import { PAGE_GET, form, request, sleep } from "../lib/harness.mjs";

export const assert = (cond, msg) => { if (!cond) throw new Error(msg); };

/** Pause and attempts for a login begin answered `rate` (refill is 2 tokens per second). */
const RATE_WAIT_MS = 600;
const RATE_RETRIES = 40;

/** Wait until `expr` (evaluated in the page) is truthy; navigation in between is tolerated. */
export async function until(t, expr, what) {
  for (let i = 0; i < 300; i++) {
    try {
      if (await t.page.evaluate(expr)) return;
    } catch { /* the page is navigating */ }
    await sleep(100);
  }
  throw new Error(`timeout waiting for ${what}`);
}

/**
 * Sign the browser in as `subject` on the landing page (the page's own script: login
 * begin, the virtual authenticator's assertion, login finish). Returns the page's result
 * text ("signed in" on success).
 */
export async function login(t, subject) {
  // The listener rate-limits login begins process-wide (D-044 review M2); like a person,
  // the flow waits and tries again while the answer is `rate`.
  for (let attempt = 0; ; attempt++) {
    await t.page.navigate(`${t.origin}/`);
    await t.page.evaluate(`(() => {
      document.getElementById("result").textContent = "";
      const f = document.getElementById("login");
      f.elements.subject.value = ${JSON.stringify(subject)};
      f.requestSubmit();
    })()`);
    await until(t, `document.getElementById("result").textContent !== ""`, "the sign-in result");
    const result = await t.page.evaluate(`document.getElementById("result").textContent`);
    if (result !== "rate" || attempt >= RATE_RETRIES) return result;
    await sleep(RATE_WAIT_MS);
  }
}

/** `login`, failing the flow unless it signed in. */
export async function signIn(t, subject) {
  const result = await login(t, subject);
  assert(result === "signed in", `sign-in of ${subject}: ${JSON.stringify(result)}`);
}

const cookieOf = (r) => ((r.headers["set-cookie"] || [])[0] || "").split(";")[0];

/**
 * A raw loopback POST as this origin's page script (Fetch Metadata `same-origin`), with the
 * session cookie only when one is given (raw HTTP is not a browser: it states the headers a
 * browser would send).
 */
export async function post(t, path, fields, { cookie, origin = t.origin, site = "same-origin" } = {}) {
  const body = form(fields);
  const headers = {
    host: `localhost:${t.port}`, origin, "sec-fetch-site": site,
    "content-type": "application/x-www-form-urlencoded", "content-length": Buffer.byteLength(body),
  };
  if (cookie) headers.cookie = cookie;
  // A login begin answered `rate` (the process-wide login rate limit, D-044 review M2) is
  // retried after a pause, as a person would.
  for (let attempt = 0; ; attempt++) {
    const r = await request(t.port, "POST", path, headers, body);
    const limited = path === "/login/begin" && r.status === 429 && r.body.includes("\"rate\"");
    if (!limited || attempt >= RATE_RETRIES) return r;
    await sleep(RATE_WAIT_MS);
  }
}

/** A raw loopback GET as a direct navigation (`Sec-Fetch-Site: none`), with `cookie` if given. */
export function get(t, path, { cookie, site = "none" } = {}) {
  const headers = { host: `localhost:${t.port}`, "sec-fetch-site": site };
  if (cookie) headers.cookie = cookie;
  return request(t.port, "GET", path, headers);
}

/** Sign `options` (a begin's verifier options) in the browser at this server's origin. */
export async function assertion(t, options) {
  const here = await t.page.evaluate("location.origin").catch(() => "");
  if (here !== t.origin) await t.page.navigate(`${t.origin}/`);
  return t.page.call(PAGE_GET, JSON.stringify(options));
}

/**
 * Sign in as `subject` over raw HTTP, the assertion made by the browser's virtual
 * authenticator at this origin. Returns the authenticated session (cookie, CSRF token) and
 * what a replay needs (the pre-authentication cookie, the finish fields, the begin).
 */
export async function httpLogin(t, subject) {
  const begun = await post(t, "/login/begin", { subject });
  assert(begun.status === 200, `login begin of ${subject}: ${begun.status} ${begun.body}`);
  const b = JSON.parse(begun.body);
  const pre = cookieOf(begun);
  const response = await assertion(t, b.options);
  const fields = { csrf: b.csrf, challenge_id: b.challenge_id, response };
  const done = await post(t, "/login/finish", fields, { cookie: pre });
  assert(done.status === 200, `login finish of ${subject}: ${done.status} ${done.body}`);
  const d = JSON.parse(done.body);
  return { cookie: cookieOf(done), csrf: d.csrf, pre, fields, begun: b, response };
}

/** `httpLogin` as `subject`, then GET `/request/<id>`: { cookie, csrf, fingerprint }. */
export async function httpView(t, subject, id) {
  const s = await httpLogin(t, subject);
  const r = await get(t, `/request/${encodeURIComponent(id)}`, { cookie: s.cookie });
  assert(r.status === 200, `view ${id} as ${subject}: ${r.status}`);
  return {
    cookie: s.cookie,
    csrf: r.body.match(/data-csrf="([^"]+)"/)[1],
    fingerprint: r.body.match(/data-fingerprint="([^"]+)"/)[1],
  };
}

/** The one refusal of every unavailable page (D-044 Z3), as the anonymous reference. */
export async function refusalReference(t) {
  const r = await get(t, "/request/req-never-stored");
  assert(r.status === 404 && !r.headers["set-cookie"], `reference refusal ${r.status}`);
  return r.body;
}

/** `r` is exactly the one refusal: same status and body, no session created. */
export function assertRefused(r, reference, what) {
  assert(r.status === 404, `${what}: status ${r.status}`);
  assert(r.body === reference, `${what}: body differs from the anonymous refusal`);
  assert(!r.headers["set-cookie"], `${what}: a session was created`);
}
