// Revoke, role-change, enrollment and signoff pages (W-F, D-043 item 4 and Y2) through the
// real writ-approval-server binary, the real HTTP listener and the production webauthn-rs
// verifier. User verification is SIMULATED by CDP virtual authenticators (no real user, no
// real credential). Flow shape: see flows/approve-deny.mjs.

import { request, sleep } from "../lib/harness.mjs";

const assert = (cond, msg) => { if (!cond) throw new Error(msg); };

/** Wait until `expr` (evaluated in the page) is truthy; navigation in between is tolerated. */
async function until(t, expr, what) {
  for (let i = 0; i < 300; i++) {
    try {
      if (await t.page.evaluate(expr)) return;
    } catch { /* the page is navigating */ }
    await sleep(100);
  }
  throw new Error(`timeout waiting for ${what}`);
}

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

/** GET `path` over loopback HTTP: the page session's cookie and CSRF token. */
async function session(t, path) {
  const r = await request(t.port, "GET", path, { host: `localhost:${t.port}` });
  const cookie = ((r.headers["set-cookie"] || [])[0] || "").split(";")[0];
  const m = r.body.match(/data-csrf="([^"]+)"/);
  return { status: r.status, cookie, csrf: m && m[1], body: r.body, headers: r.headers };
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
      const s = await session(t, "/revoke");
      const fields = { operation: "revoke", target_kind: "subject", target: t.subjects.approver, reason: "x" };
      const bad = await t.post("/prepare", { ...fields, csrf: "0".repeat(64) }, { cookie: s.cookie });
      assert(bad.status === 403 && JSON.parse(bad.body).error === "session", `csrf: ${bad.status} ${bad.body}`);
      const wrong = await t.post("/prepare", { ...fields, csrf: s.csrf }, { cookie: s.cookie, origin: t.wrongOrigin });
      assert(wrong.status === 403 && JSON.parse(wrong.body).error === "origin", `origin: ${wrong.status} ${wrong.body}`);
      return "prepare with a wrong CSRF token answered 403 session; with Origin of another port 403 origin";
    },
  },
  {
    name: "role change via page (grant)",
    requests: [],
    expect: {},
    async run(t) {
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
      await t.page.navigate(`${t.origin}/enroll`);
      // 1. The operator's registration start: displayed, then the operator's passkey.
      const start = await prepare(t, { operator: t.subjects.admin, subject: newbie });
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
      await t.page.navigate(`${t.origin}/enroll`);
      await prepare(t, { operator: t.subjects.approver, subject: "browser-nobody" });
      const result = await t.decide("confirm", t.subjects.approver);
      assert(result === "refused", `result ${JSON.stringify(result)}`);
      return `registration start by ${t.subjects.approver} (approver, not operator) answered refused; no row created`;
    },
  },
  {
    name: "signoff page refuses a signer the ledger excludes (linked to the requester)",
    requests: [],
    expect: {},
    async run(t) {
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
      const r = await session(t, "/signoff/so-not-pending");
      assert(r.status === 404, `status ${r.status}`);
      assert(!r.headers["set-cookie"], "a session was created");
      const bad = await request(t.port, "GET", "/signoff/so%2Fx", { host: `localhost:${t.port}` });
      assert(bad.status === 400, `malformed id ${bad.status}`);
      return "GET /signoff/<unknown> answered 404 without a session; a malformed id 400";
    },
  },
];
