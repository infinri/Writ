// Revoke, role-change, enrollment and signoff pages (W-F, D-043 item 4 and Y2) through the
// real writ-approval-server binary, the real HTTP listener and the production webauthn-rs
// verifier. User verification is SIMULATED by CDP virtual authenticators (no real user, no
// real credential). Flow shape: see flows/approve-deny.mjs. Every flow signs in first
// (D-044 item 3: every page needs an authenticated read session; flows/login.mjs).

import { request } from "../lib/harness.mjs";
import { assert, get, httpLogin, refusalReference, assertRefused, signIn, until } from "./login.mjs";

/** Fill the page's `form#prepare` with `fields`, submit it and wait for the `/view` page. */
async function prepare(t, fields) {
  await t.page.evaluate(`(() => {
    window.__wfPrepared = true; // gone once the new /view document loads
    const f = document.getElementById("prepare");
    for (const [k, v] of Object.entries(${JSON.stringify(fields)})) { f.elements[k].value = v; }
    f.requestSubmit();
  })()`);
  await until(t, `!window.__wfPrepared && location.pathname === "/view" && document.readyState === "complete" && !!document.getElementById("decide")`, "/view");
  return t.page.evaluate(`({ title: document.querySelector("h1").textContent,
    fingerprint: document.getElementById("page").dataset.fingerprint,
    text: document.querySelector(".context").textContent })`);
}

/** Sign in as `subject` over loopback HTTP, then GET `path`: the session's cookie and CSRF. */
async function session(t, subject, path) {
  const s = await httpLogin(t, subject);
  const r = await get(t, path, { cookie: s.cookie });
  const m = r.body.match(/data-csrf="([^"]+)"/);
  return { status: r.status, cookie: s.cookie, csrf: m && m[1], body: r.body, headers: r.headers };
}

/** Open the pending signoff page; the displayed exclusions, delegation chain and expiry. */
async function openSignoff(t) {
  await t.page.navigate(`${t.origin}/signoff/${encodeURIComponent(t.signoffId)}`);
  return t.page.evaluate(`(() => {
    const top = document.querySelector(".context > dl");
    if (!top) return { missing: document.body.innerText.slice(0, 300) };
    const member = (dl, name) => {
      for (const dt of dl.children) {
        if (dt.tagName === "DT" && dt.textContent === name) return dt.nextElementSibling;
      }
      return null;
    };
    const excluded = member(top, "excluded").querySelector("dl");
    return {
      expires: member(top, "expires_at").textContent,
      delegation: member(excluded, "delegation_chain").textContent,
      requesters: member(excluded, "requesters").textContent,
      linked: member(excluded, "linked_subjects").textContent,
      fingerprint: document.getElementById("page").dataset.fingerprint,
    };
  })()`).then((shown) => {
    if (shown.missing !== undefined) throw new Error(`no signoff context displayed: ${shown.missing}`);
    return shown;
  });
}

export const flows = [
  {
    name: "revoke via page (subject target)",
    requests: [],
    expect: {},
    async run(t) {
      await signIn(t, t.subjects.admin);
      await t.page.navigate(`${t.origin}/revoke`);
      const shown = await prepare(t, {
        target_kind: "subject", target: t.subjects.target, reason: "browser suite <revocation>",
      });
      assert(shown.title === "Revocation", `title ${shown.title}`);
      assert(shown.text.includes(t.subjects.target) && shown.text.includes("browser suite <revocation>"),
        "the displayed context lacks the target or the reason");
      const result = await t.decide("confirm", t.subjects.admin);
      assert(result === "revoked", `result ${JSON.stringify(result)}`);
      return `revocation of subject ${t.subjects.target} displayed then confirmed by ${t.subjects.admin}: revoked`;
    },
  },
  {
    name: "revoke page refuses a wrong CSRF token and a wrong origin",
    requests: [],
    expect: {},
    async run(t) {
      const s = await session(t, t.subjects.admin, "/revoke");
      assert(s.status === 200 && s.csrf, `revoke page ${s.status}`);
      const fields = { operation: "revoke", target_kind: "subject", target: t.subjects.approver, reason: "x" };
      // A wrong CSRF token is the one generic refusal of a read (D-044 Z3).
      const bad = await t.post("/prepare", { ...fields, csrf: "0".repeat(64) }, { cookie: s.cookie });
      assert(bad.status === 403 && JSON.parse(bad.body).error === "refused", `csrf: ${bad.status} ${bad.body}`);
      const wrong = await t.post("/prepare", { ...fields, csrf: s.csrf }, { cookie: s.cookie, origin: t.wrongOrigin });
      assert(wrong.status === 403 && JSON.parse(wrong.body).error === "origin", `origin: ${wrong.status} ${wrong.body}`);
      // LOW-1: the session's cookie and CSRF token used from another localhost port
      // (Sec-Fetch-Site same-site) are refused.
      const planted = await t.post("/prepare", { ...fields, csrf: s.csrf }, { cookie: s.cookie, site: "same-site" });
      assert(planted.status === 403 && JSON.parse(planted.body).error === "fetch site", `fetch site: ${planted.status} ${planted.body}`);
      return "prepare with a wrong CSRF token answered 403 refused; with Origin of another port 403 origin; with Sec-Fetch-Site same-site 403 fetch site";
    },
  },
  {
    name: "role change via page (grant)",
    requests: [],
    expect: {},
    async run(t) {
      await signIn(t, t.subjects.admin);
      await t.page.navigate(`${t.origin}/role`);
      const shown = await prepare(t, {
        operation: "role-grant", target: t.subjects.approver, permission: "signoff_authority",
        tenant_id: "t-browser", project_id: "p-browser",
      });
      assert(shown.title === "Role change", `title ${shown.title}`);
      assert(shown.text.includes("signoff_authority"), "the displayed context lacks the permission");
      const result = await t.decide("confirm", t.subjects.admin);
      assert(result === "role changed", `result ${JSON.stringify(result)}`);
      return `grant of signoff_authority to ${t.subjects.approver} displayed then confirmed: role changed`;
    },
  },
  {
    name: "role change page refuses a stale display",
    requests: [],
    expect: {},
    async run(t) {
      await signIn(t, t.subjects.admin);
      await t.page.navigate(`${t.origin}/role`);
      await prepare(t, {
        operation: "role-grant", target: t.subjects.approver, permission: "revoker",
        tenant_id: "t-browser", project_id: "p-browser",
      });
      await t.page.evaluate(`document.getElementById("page").dataset.fingerprint = "sha256:${"0".repeat(64)}"`);
      const result = await t.decide("confirm", t.subjects.admin);
      assert(result === "stale display", `result ${JSON.stringify(result)}`);
      return "confirm with a fingerprint other than the displayed one answered stale display";
    },
  },
  {
    name: "enrollment via page (registration start, new person's passkey on a second authenticator, Enroll)",
    requests: [],
    expect: {},
    async run(t) {
      const newbie = "browser-newbie";
      await signIn(t, t.subjects.admin);
      await t.page.navigate(`${t.origin}/enroll`);
      // 1. The operator's registration start (the operator is the signed-in subject):
      // displayed, then the operator's passkey.
      const start = await prepare(t, { subject: newbie });
      assert(start.title === "Registration start", `title ${start.title}`);
      assert(start.text.includes(newbie) && start.text.includes("localhost"), "start context incomplete");
      const started = await t.decide("confirm", t.subjects.admin);
      assert(started === "registration started", `start ${JSON.stringify(started)}`);
      await until(t, `!document.getElementById("register-new").hidden`, "the registration button");
      // 2. The new person's passkey, on a second virtual authenticator (simulated UV): the
      // operator's authenticator stops answering for the duration.
      const { authenticatorId: second } = await t.page.send("WebAuthn.addVirtualAuthenticator", {
        options: {
          protocol: "ctap2", ctap2Version: "ctap2_1", transport: "usb", hasResidentKey: true,
          hasUserVerification: true, isUserVerified: true, automaticPresenceSimulation: true,
        },
      });
      await t.page.send("WebAuthn.setAutomaticPresenceSimulation", { authenticatorId: t.page.authenticatorId, enabled: false });
      let registered;
      let created;
      try {
        await t.page.evaluate(`(() => { document.getElementById("result").textContent = "";
          document.getElementById("register-new").click(); })()`);
        await until(t, `document.getElementById("result").textContent !== ""`, "the registration result");
        registered = await t.page.evaluate(`document.getElementById("result").textContent`);
        created = (await t.page.send("WebAuthn.getCredentials", { authenticatorId: second })).credentials.length;
      } finally {
        await t.page.send("WebAuthn.setAutomaticPresenceSimulation", { authenticatorId: t.page.authenticatorId, enabled: true });
        await t.page.send("WebAuthn.removeVirtualAuthenticator", { authenticatorId: second });
      }
      assert(registered === "registered", `registration ${JSON.stringify(registered)}`);
      assert(created === 1, `second authenticator holds ${created} credentials`);
      // 3. The Enroll ceremony of that credential by the same operator in the same session.
      await until(t, `!document.getElementById("prepare").hidden`, "the enrollment form");
      const enroll = await prepare(t, { roles: "approver t-browser p-browser", linked: "" });
      assert(enroll.title === "Enrollment" && enroll.text.includes(newbie), `enrollment page ${enroll.title}`);
      const result = await t.decide("confirm", t.subjects.admin);
      assert(result === "enrolled", `enroll ${JSON.stringify(result)}`);
      return `start ceremony by ${t.subjects.admin}, ${newbie}'s passkey created on a second authenticator, Enroll ceremony: enrolled`;
    },
  },
  {
    name: "enrollment page refuses a registration start by a non-operator",
    requests: [],
    expect: {},
    async run(t) {
      // D-044 Z3: the registration-start context is an operator read; an approver's
      // prepare is refused before anything is displayed or begun.
      await signIn(t, t.subjects.approver);
      await t.page.navigate(`${t.origin}/enroll`);
      await t.page.evaluate(`(() => {
        document.getElementById("result").textContent = "";
        const f = document.getElementById("prepare");
        f.elements.subject.value = "browser-nobody";
        f.requestSubmit();
      })()`);
      await until(t, `document.getElementById("result").textContent !== ""`, "the prepare result");
      const result = await t.page.evaluate(`document.getElementById("result").textContent`);
      assert(result === "refused", `result ${JSON.stringify(result)}`);
      const path = await t.page.evaluate("location.pathname");
      assert(path === "/enroll", `navigated to ${path}`);
      return `registration start prepared by ${t.subjects.approver} (approver, not operator) answered refused; nothing displayed, no row created`;
    },
  },
  {
    name: "signoff page refuses a signer the ledger excludes (linked to the requester)",
    requests: [],
    expect: {},
    async run(t) {
      await signIn(t, t.subjects.excluded);
      const shown = await openSignoff(t);
      assert(shown.linked.includes(t.subjects.excluded), `exclusions ${shown.linked}`);
      const result = await t.decide("confirm", t.subjects.excluded);
      assert(result === "SEPARATION_VIOLATED" || result === "refused", `result ${JSON.stringify(result)}`);
      return `${t.subjects.excluded} is listed in the displayed exclusions (linked subjects); its confirmation answered ${result}`;
    },
  },
  {
    name: "signoff via page (exclusions, delegation unavailable, effective expiry displayed; real verifier)",
    requests: [],
    expect: {},
    async run(t) {
      await signIn(t, t.subjects.reviewer);
      const shown = await openSignoff(t);
      assert(shown.requesters.includes("agent:session-1"), `requesters ${shown.requesters}`);
      assert(shown.delegation === "unavailable", `delegation_chain ${shown.delegation}`);
      assert(/^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(\.\d+)?Z$/.test(shown.expires), `expires_at ${shown.expires}`);
      const result = await t.decide("confirm", t.subjects.reviewer);
      assert(result === "signed off", `result ${JSON.stringify(result)}`);
      // The ledger must store exactly the displayed effective expiry (checked after the run).
      t.signoffExpect[t.signoffId] = { subject: t.subjects.reviewer, expires_at: shown.expires };
      return `page showed exclusions, delegation_chain unavailable and expires_at ${shown.expires}; ${t.subjects.reviewer} confirmed: signed off`;
    },
  },
  {
    name: "signoff page refuses an id with no pending signoff (generic, no session)",
    requests: [],
    expect: {},
    async run(t) {
      const reference = await refusalReference(t);
      const r = await session(t, t.subjects.reviewer, "/signoff/so-not-pending");
      assertRefused(r, reference, "unknown signoff id");
      // The signed signoff is no longer pending: the same refusal.
      assertRefused(await session(t, t.subjects.reviewer, `/signoff/${t.signoffId}`), reference, "signed signoff");
      const s = await httpLogin(t, t.subjects.reviewer);
      const bad = await request(t.port, "GET", "/signoff/so%2Fx", { host: `localhost:${t.port}`, "sec-fetch-site": "none", cookie: s.cookie });
      assert(bad.status === 400, `malformed id ${bad.status}`);
      return "GET /signoff/<unknown> and /signoff/<signed> answered the one 404 without a new session; a malformed id 400";
    },
  },
];
