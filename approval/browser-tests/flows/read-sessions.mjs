// Authenticated, scoped record reads (D-044 item 3, M4-INTERFACES Z1..Z4) through the real
// writ-approval-server binary, the real HTTP listener and the production webauthn-rs
// verifier. User verification is SIMULATED by the CDP virtual authenticator (no real user,
// no real credential). Flow shape: see flows/approve-deny.mjs. These flows are appended to
// the approve-deny module's list (they run before the operator pages revoke the target
// subject).
//
// Fixture state they use: `req-read` is stored in (t-browser, p-browser), `req-other-scope`
// in (t-other, p-browser) (the fixture's `req-other` prefix); the target subject holds
// `approver` in (t-browser, p-browser) only.

import {
  assert, assertRefused, assertion, get, httpLogin, post, refusalReference, signIn,
} from "./login.mjs";

/** The text of the browser's current page. */
const pageText = (t) => t.page.evaluate("document.body.innerText");

export const flows = [
  {
    name: "anonymous direct URLs are refused identically (no session, no record)",
    requests: ["req-read"],
    expect: { "req-read": "pending" },
    async run(t) {
      const reference = await refusalReference(t);
      for (const path of ["/request/req-read", "/request/req-guessed", `/signoff/${t.signoffId}`,
        "/signoff/so-guessed", "/revoke", "/role", "/enroll"]) {
        assertRefused(await get(t, path), reference, `anonymous ${path}`);
      }
      assertRefused(await get(t, "/view", { site: "same-origin" }), reference, "anonymous /view");
      const prepare = await post(t, "/prepare", { csrf: "x", operation: "role-revoke", target: "r" });
      assert(prepare.status === 403 && JSON.parse(prepare.body).error === "refused",
        `anonymous prepare ${prepare.status} ${prepare.body}`);
      // The browser without any session: the landing page offers login only; a direct URL
      // shows the same refusal page.
      await t.page.send("Network.clearBrowserCookies");
      await t.page.navigate(`${t.origin}/`);
      const landing = await t.page.evaluate(`({ login: !!document.getElementById("login"),
        csrf: document.getElementById("page").dataset.csrf || null, text: document.body.innerText })`);
      assert(landing.login && landing.csrf === null, "the landing page is not the login form");
      assert(!landing.text.includes("req-read"), "the landing page shows a record");
      await t.page.navigate(`${t.origin}/request/req-read`);
      const shown = await pageText(t);
      assert(/Not found/.test(shown) && !shown.includes("req-read"), `anonymous browser page: ${shown.slice(0, 200)}`);
      return "every page and direct URL answered the one 404 without a session; POST /prepare 403 refused; the browser saw the login page and the refusal";
    },
  },
  {
    name: "valid scoped read (approver in scope, after a passkey login)",
    requests: ["req-read"],
    expect: { "req-read": "pending" },
    async run(t) {
      await signIn(t, t.subjects.target);
      const shown = await t.openRequest("req-read");
      assert(shown.fingerprint === t.fingerprints["req-read"], "page fingerprint differs from the stored one");
      assert(shown.decidable, "a pending request in scope offers its decision");
      return `${t.subjects.target} (approver in t-browser/p-browser) signed in and read req-read`;
    },
  },
  {
    name: "cross-scope reads are refused identically",
    requests: ["req-read", "req-other-scope"],
    expect: { "req-read": "pending", "req-other-scope": "pending" },
    async run(t) {
      const reference = await refusalReference(t);
      // The approver of (t-browser, p-browser) reads a request of t-other: refused.
      const target = await httpLogin(t, t.subjects.target);
      assert((await get(t, "/request/req-read", { cookie: target.cookie })).status === 200, "in-scope read");
      assertRefused(await get(t, "/request/req-other-scope", { cookie: target.cookie }), reference, "other tenant");
      // A signoff authority reads an approval request; an approver reads a signoff; an
      // operator without approver reads a request: refused.
      const reviewer = await httpLogin(t, t.subjects.reviewer);
      assertRefused(await get(t, "/request/req-read", { cookie: reviewer.cookie }), reference, "reviewer");
      const approver = await httpLogin(t, t.subjects.approver);
      assertRefused(await get(t, `/signoff/${t.signoffId}`, { cookie: approver.cookie }), reference, "approver signoff");
      const admin = await httpLogin(t, t.subjects.admin);
      assertRefused(await get(t, "/request/req-read", { cookie: admin.cookie }), reference, "operator");
      // An approver's operator page read (prepare) is the generic refusal.
      const page = await get(t, "/role", { cookie: approver.cookie });
      const csrf = page.body.match(/data-csrf="([^"]+)"/)[1];
      const prepare = await post(t, "/prepare", {
        csrf, operation: "role-grant", target: t.subjects.target, permission: "revoker",
        tenant_id: "t-browser", project_id: "p-browser",
      }, { cookie: approver.cookie });
      assert(prepare.status === 403 && JSON.parse(prepare.body).error === "refused",
        `approver prepare ${prepare.status} ${prepare.body}`);
      // In the browser: the same refusal page.
      await signIn(t, t.subjects.target);
      await t.page.navigate(`${t.origin}/request/req-other-scope`);
      const shown = await pageText(t);
      assert(/Not found/.test(shown) && !shown.includes("t-other"), `browser page: ${shown.slice(0, 200)}`);
      return "other tenant, other permission (reviewer, approver, operator) and an approver's role prepare all answered the one refusal";
    },
  },
  {
    name: "replayed login assertion is refused",
    requests: [],
    expect: {},
    async run(t) {
      const first = await httpLogin(t, t.subjects.approver);
      // The same finish again, with the pre-authentication cookie and with the new one.
      const again = await post(t, "/login/finish", first.fields, { cookie: first.pre });
      assert(again.status === 403, `replay with the old cookie: ${again.status} ${again.body}`);
      const withNew = await post(t, "/login/finish", { ...first.fields, csrf: first.csrf }, { cookie: first.cookie });
      assert(withNew.status === 403, `replay with the new cookie: ${withNew.status} ${withNew.body}`);
      // The old assertion presented for a new login challenge: the verifier refuses it.
      const begun = await post(t, "/login/begin", { subject: t.subjects.approver });
      const b = JSON.parse(begun.body);
      const pre = (begun.headers["set-cookie"] || [])[0].split(";")[0];
      const stale = await post(t, "/login/finish", { csrf: b.csrf, challenge_id: b.challenge_id, response: first.response }, { cookie: pre });
      assert(stale.status === 403 && JSON.parse(stale.body).error === "ASSERTION_INVALID",
        `old assertion on a new challenge: ${stale.status} ${stale.body}`);
      // That pre-authentication session stays unauthenticated.
      const reference = await refusalReference(t);
      assertRefused(await get(t, "/request/req-read", { cookie: pre }), reference, "after the refused replay");
      // A login challenge is never accepted for a mutation: finishing it as a ceremony in an
      // authenticated session is refused.
      const fresh = await post(t, "/login/begin", { subject: t.subjects.approver });
      const f = JSON.parse(fresh.body);
      const signed = await assertion(t, f.options);
      const cross = await post(t, "/ceremony/finish", { csrf: first.csrf, challenge_id: f.challenge_id, response: signed }, { cookie: first.cookie });
      assert(cross.status === 403, `login challenge finished as a ceremony: ${cross.status} ${cross.body}`);
      return "the same finish (old and new cookie) answered 403; the old assertion on a new challenge ASSERTION_INVALID; a login challenge as a ceremony 403";
    },
  },
  {
    name: "an open read session is invalidated by revoking its credential",
    requests: ["req-read"],
    expect: { "req-read": "pending" },
    async run(t) {
      const reference = await refusalReference(t);
      // The target signs in and reads.
      const target = await httpLogin(t, t.subjects.target);
      assert((await get(t, "/request/req-read", { cookie: target.cookie })).status === 200, "read before revocation");
      // Its credential id (hex of the raw id the verifier stores) from its login options.
      const allowed = target.begun.options.publicKey.allowCredentials || [];
      assert(allowed.length === 1, `allowCredentials ${allowed.length}`);
      const credential = Buffer.from(allowed[0].id, "base64url").toString("hex");
      // The administrator revokes that credential through the revoke page's ceremony.
      const admin = await httpLogin(t, t.subjects.admin);
      const form = await get(t, "/revoke", { cookie: admin.cookie });
      const csrf = form.body.match(/data-csrf="([^"]+)"/)[1];
      const prepared = await post(t, "/prepare", {
        csrf, operation: "revoke", target_kind: "credential", target: credential, reason: "read-session test",
      }, { cookie: admin.cookie });
      assert(prepared.status === 200, `prepare ${prepared.status} ${prepared.body}`);
      const view = await get(t, "/view", { cookie: admin.cookie, site: "same-origin" });
      const fingerprint = view.body.match(/data-fingerprint="([^"]+)"/)[1];
      const begun = await post(t, "/ceremony/begin", { csrf, fingerprint, verdict: "confirm" }, { cookie: admin.cookie });
      assert(begun.status === 200, `begin ${begun.status} ${begun.body}`);
      const b = JSON.parse(begun.body);
      const response = await assertion(t, b.options);
      const done = await post(t, "/ceremony/finish", { csrf, challenge_id: b.challenge_id, response }, { cookie: admin.cookie });
      assert(done.status === 200 && JSON.parse(done.body).result === "revoked", `revoke ${done.status} ${done.body}`);
      // The open session reads nothing any more: the one refusal; a new login is refused.
      assertRefused(await get(t, "/request/req-read", { cookie: target.cookie }), reference, "after revocation");
      const again = await post(t, "/login/begin", { subject: t.subjects.target });
      assert(again.status === 403 && JSON.parse(again.body).error === "refused", `login after revocation ${again.status}`);
      // The browser's own session of the target (signed in by an earlier flow) too.
      await t.page.navigate(`${t.origin}/request/req-read`);
      const shown = await pageText(t);
      assert(/Not found/.test(shown), `browser page after revocation: ${shown.slice(0, 200)}`);
      return `credential ${credential.slice(0, 12)}... revoked by ${t.subjects.admin}; the open session got the one refusal; a new login was refused`;
    },
  },
];
