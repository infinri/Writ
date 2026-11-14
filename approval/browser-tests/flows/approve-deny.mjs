// Approve and deny flows of the approval page (M4-11) through the real writ-approval-server
// binary, the real HTTP listener and the production webauthn-rs verifier. User verification
// is SIMULATED by the CDP virtual authenticator (no real user, no real credential).
//
// A flow is { name, requests: [ids the fixture records as pending], expect: {id: final
// ledger status}, run: async (t) => detail string }. It throws on failure.
//
// D-044 item 3 (Z1..Z4): every page needs an authenticated read session, so every flow
// signs in first (flows/login.mjs); the read-session flows (flows/read-sessions.mjs) are
// appended to this module's list.

import { PAGE_GET } from "../lib/harness.mjs";
import { assert, httpView, signIn } from "./login.mjs";
import { flows as readSessionFlows } from "./read-sessions.mjs";

const baseFlows = [
  {
    name: "approve via page",
    requests: ["req-approve"],
    expect: { "req-approve": "approved" },
    async run(t) {
      await signIn(t, t.subjects.approver);
      const shown = await t.openRequest("req-approve");
      assert(shown.fingerprint === t.fingerprints["req-approve"], "page fingerprint differs from the stored one");
      assert(shown.escaped, "untrusted requester text was not escaped");
      const result = await t.decide("approve", t.subjects.approver);
      assert(result === "approved", `result ${JSON.stringify(result)}`);
      return "signed in; page shows the stored context; approve ceremony answered approved";
    },
  },
  {
    name: "deny via page",
    requests: ["req-deny"],
    expect: { "req-deny": "denied" },
    async run(t) {
      await signIn(t, t.subjects.approver);
      await t.openRequest("req-deny");
      const result = await t.decide("deny", t.subjects.approver);
      assert(result === "denied", `result ${JSON.stringify(result)}`);
      return "signed in; deny ceremony answered denied";
    },
  },
  {
    name: "the actor is the signed-in subject (another subject named on the page is refused)",
    requests: ["req-actor"],
    expect: { "req-actor": "pending" },
    async run(t) {
      await signIn(t, t.subjects.approver);
      await t.openRequest("req-actor");
      const result = await t.decide("approve", t.subjects.target);
      assert(result === "refused", `result ${JSON.stringify(result)}`);
      return `signed in as ${t.subjects.approver}; a begin naming ${t.subjects.target} answered refused`;
    },
  },
  {
    name: "user verification false refused (unmodified page)",
    requests: ["req-nouv"],
    expect: { "req-nouv": "pending" },
    async run(t) {
      await signIn(t, t.subjects.approver);
      await t.page.setUserVerified(false);
      try {
        await t.openRequest("req-nouv");
        const result = await t.decide("approve", t.subjects.approver);
        assert(result !== "approved", "approved without user verification");
        return `isUserVerified=false: page result ${JSON.stringify(result)}`;
      } finally {
        await t.page.setUserVerified(true);
      }
    },
  },
  {
    name: "user verification false refused by the server (client options tampered)",
    requests: ["req-nouv-server"],
    expect: { "req-nouv-server": "pending" },
    async run(t) {
      // A hostile client lowers userVerification so the authenticator signs with UV=0; the
      // server-held ceremony state still requires UV, so the verifier must refuse.
      await signIn(t, t.subjects.approver);
      await t.page.setUserVerified(false);
      const stopTamper = await t.tamperBegin((body) => {
        body.options.publicKey.userVerification = "discouraged";
        return body;
      });
      try {
        await t.openRequest("req-nouv-server");
        const result = await t.decide("approve", t.subjects.approver);
        assert(result === "ASSERTION_INVALID", `result ${JSON.stringify(result)}`);
        return "assertion with UV=0 reached the server and was refused ASSERTION_INVALID";
      } finally {
        await stopTamper();
        await t.page.setUserVerified(true);
      }
    },
  },
  {
    name: "wrong origin refused (Origin header from another port)",
    requests: ["req-origin-header"],
    expect: { "req-origin-header": "pending" },
    async run(t) {
      const s = await httpView(t, t.subjects.approver, "req-origin-header");
      const fields = { csrf: s.csrf, request_id: "req-origin-header", fingerprint: s.fingerprint, verdict: "approve", subject: t.subjects.approver };
      const wrong = await t.post("/ceremony/begin", fields, { cookie: s.cookie, origin: t.wrongOrigin });
      assert(wrong.status === 403 && JSON.parse(wrong.body).error === "origin", `wrong origin: ${wrong.status} ${wrong.body}`);
      const host = await t.post("/ceremony/begin", fields, { cookie: s.cookie, host: `127.0.0.1:${t.port}` });
      assert(host.status === 421, `wrong host: ${host.status} ${host.body}`);
      return "Origin http://localhost:<other port> answered 403 origin; Host 127.0.0.1 answered 421 (HTTP level, no browser)";
    },
  },
  {
    name: "wrong origin refused (assertion made on another port, relayed)",
    requests: ["req-origin-relay"],
    expect: { "req-origin-relay": "pending" },
    async run(t) {
      // A relay: the challenge is begun on the real origin, the browser signs it on a page
      // served from http://localhost:<other port> (same RP id, different origin).
      const s = await httpView(t, t.subjects.approver, "req-origin-relay");
      const begun = await t.post("/ceremony/begin", {
        csrf: s.csrf, request_id: "req-origin-relay", fingerprint: s.fingerprint, verdict: "approve", subject: t.subjects.approver,
      }, { cookie: s.cookie });
      assert(begun.status === 200, `begin ${begun.status} ${begun.body}`);
      const { challenge_id, options } = JSON.parse(begun.body);
      await t.page.navigate(`${t.wrongOrigin}/relay`);
      const assertion = await t.page.call(PAGE_GET, JSON.stringify(options));
      const clientData = JSON.parse(Buffer.from(JSON.parse(assertion).response.clientDataJSON, "base64url").toString());
      assert(clientData.origin === t.wrongOrigin, `assertion origin ${clientData.origin}`);
      const done = await t.post("/ceremony/finish", { csrf: s.csrf, challenge_id, response: assertion }, { cookie: s.cookie });
      assert(done.status === 403 && JSON.parse(done.body).error === "ASSERTION_INVALID", `finish ${done.status} ${done.body}`);
      return `assertion with clientData origin ${t.wrongOrigin} refused ASSERTION_INVALID`;
    },
  },
  {
    name: "stale display refused (displayed fingerprint is not the stored one)",
    requests: ["req-stale"],
    expect: { "req-stale": "pending" },
    async run(t) {
      await signIn(t, t.subjects.approver);
      await t.openRequest("req-stale");
      await t.page.evaluate(`document.getElementById("page").dataset.fingerprint = "sha256:${"0".repeat(64)}"`);
      const result = await t.decide("approve", t.subjects.approver);
      assert(result === "stale display", `result ${JSON.stringify(result)}`);
      return "begin with a fingerprint other than the displayed and stored one answered stale display";
    },
  },
  {
    name: "stale display refused (request decided after it was displayed)",
    requests: ["req-closed"],
    expect: { "req-closed": "denied" },
    async run(t) {
      await signIn(t, t.subjects.approver);
      await t.openRequest("req-closed");
      const denied = await t.decide("deny", t.subjects.approver);
      assert(denied === "denied", `deny ${JSON.stringify(denied)}`);
      // The page still displays "pending"; approving from that stale display is refused.
      const result = await t.decide("approve", t.subjects.approver);
      assert(result === "refused", `result ${JSON.stringify(result)}`);
      return "approve from a page still showing pending after the request was denied answered refused";
    },
  },
];

export const flows = [...baseFlows, ...readSessionFlows];
