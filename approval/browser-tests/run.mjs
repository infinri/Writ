// End-to-end browser suite of the approval channel (D-043 item 4; approval-server/README.md).
//
// Real path: headless Chrome (disposable --user-data-dir in a temporary directory this
// script creates and removes) -> the real `writ-approval-server` binary (production
// webauthn-rs verifier, loopback listener, SQLite ledger) on a temporary ledger.
// User verification is SIMULATED: the authenticator is a CDP WebAuthn virtual
// authenticator (hasUserVerification true, isUserVerified true unless a flow turns it off).
// No real user is enrolled and nothing listens beyond 127.0.0.1.
//
//   node run.mjs --server <writ-approval-server> --fixture <browser_fixture> [--chrome <path>]
//                [--only <substring>]
//
// Flows live in flows/*.mjs (see flows/approve-deny.mjs for the shape); add a module to
// FLOW_MODULES to cover further pages.

import fs from "node:fs";
import os from "node:os";
import path from "node:path";

import { Cdp, Page } from "./lib/cdp.mjs";
import {
  closeServer, driveFixture, form, freePort, launchChrome, request, sleep, spawnLines,
  staticServer, stop, waitExit,
} from "./lib/harness.mjs";

const FLOW_MODULES = ["./flows/approve-deny.mjs", "./flows/operator-pages.mjs"];
const FLOW_TIMEOUT_MS = 60000;
const LABEL = "CDP virtual authenticator, simulated UV";
const ISSUER = "writ-local";
const SUBJECTS = { admin: "browser-admin", approver: "browser-approver", target: "browser-target" };

function args() {
  const out = { chrome: "/usr/bin/google-chrome" };
  const a = process.argv.slice(2);
  for (let i = 0; i < a.length; i += 2) {
    if (!a[i].startsWith("--") || a[i + 1] === undefined) throw new Error(`bad argument ${a[i]}`);
    out[a[i].slice(2)] = a[i + 1];
  }
  for (const k of ["server", "fixture"]) if (!out[k]) throw new Error(`missing --${k}`);
  return out;
}

const log = (line) => console.log(`browser suite: ${line}`);

async function main() {
  const opt = args();
  const flows = [];
  for (const m of FLOW_MODULES) flows.push(...(await import(m)).flows);
  const selected = flows.filter((f) => !opt.only || f.name.includes(opt.only));
  const requestIds = selected.flatMap((f) => f.requests);
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "writ-browser-suite-"));
  const ledger = path.join(dir, "ledger.db");
  const children = {};
  const servers = {};
  let cdp;
  const results = [];
  let cleaned = false;
  const cleanup = async () => {
    if (cleaned) return;
    cleaned = true;
    if (cdp) {
      // Ask the browser to exit (all its processes) before its profile is removed.
      try { await cdp.send("Browser.close", {}, undefined, 5000); } catch { /* already gone */ }
      cdp.close();
    }
    if (children.chrome) await waitExit(children.chrome, 10000);
    for (const s of Object.values(servers)) await closeServer(s);
    for (const c of Object.values(children)) await stop(c);
    fs.rmSync(dir, { recursive: true, force: true, maxRetries: 10, retryDelay: 200 });
    log(`removed temporary directory ${dir}`);
  };
  for (const sig of ["SIGINT", "SIGTERM"]) {
    process.once(sig, () => { log(`${sig}: cleaning up`); cleanup().finally(() => process.exit(1)); });
  }
  try {
    const port = await freePort();
    let wrongPort = await freePort();
    while (wrongPort === port) wrongPort = await freePort();
    const origin = `http://localhost:${port}`;
    const wrongOrigin = `http://localhost:${wrongPort}`;
    log(`temporary directory ${dir}; server origin ${origin}; other origin ${wrongOrigin}`);

    const chrome = await launchChrome(opt.chrome, path.join(dir, "chrome-profile"), dir);
    children.chrome = chrome.child;
    cdp = await Cdp.connect(chrome.wsUrl);
    const page = await Page.open(cdp);
    await page.addVirtualAuthenticator();
    log(`virtual authenticator ${page.authenticatorId} (${LABEL})`);

    // Setup: the fixture bootstraps the administrator and enrolls the approver with
    // credentials created in this browser, on a placeholder page at the server's origin.
    servers.setup = await staticServer(port, "writ browser suite setup");
    await page.navigate(`${origin}/setup`);
    const fixture = spawnLines(opt.fixture, [
      "setup", "--ledger", ledger, "--origin", origin, "--issuer", ISSUER,
      "--admin", SUBJECTS.admin, "--approver", SUBJECTS.approver, "--target", SUBJECTS.target,
      "--requests", requestIds.join(","),
    ], (e) => process.stderr.write(`fixture: ${e}`));
    children.fixture = fixture;
    const done = await driveFixture(fixture, page, log);
    if ((await waitExit(fixture)) !== 0) throw new Error("fixture setup failed");
    await closeServer(servers.setup);
    servers.setup = null;
    log(`ledger bootstrapped (administrator ${SUBJECTS.admin}) and approver ${SUBJECTS.approver} enrolled; ${(await page.credentials()).length} credentials in the virtual authenticator`);

    // The real production binary on the prepared ledger.
    const server = spawnLines(opt.server, [
      "--ledger", ledger, "--port", String(port), "--rp-id", "localhost", "--origin", origin,
      "--channel", "approval-listener", "--subject-issuer", ISSUER,
    ], (e) => process.stderr.write(`server: ${e}`));
    children.server = server;
    const ready = await server.nextLine(20000);
    if (!ready.includes(`serving ${origin}`)) throw new Error(`server: ${ready}`);
    log(ready);
    servers.wrong = await staticServer(wrongPort, "writ browser suite other origin");

    const t = {
      page, port, origin, wrongPort, wrongOrigin, fingerprints: done.requests,
      subjects: { approver: SUBJECTS.approver, admin: SUBJECTS.admin, target: SUBJECTS.target },
      async openRequest(id) {
        await page.navigate(`${origin}/request/${encodeURIComponent(id)}`);
        return page.evaluate(`(() => {
          const p = document.getElementById("page");
          return { fingerprint: p && p.dataset.fingerprint, decidable: !!document.getElementById("decide"),
            escaped: document.body.innerHTML.includes("&lt;b&gt;requester&lt;/b&gt;") && !document.querySelector(".context b") };
        })()`);
      },
      async decide(verdict, subject) {
        await page.evaluate(`(() => {
          const out = document.getElementById("result");
          out.textContent = "";
          document.querySelector("#decide input[name=subject]").value = ${JSON.stringify(subject)};
          document.querySelector('#decide button[value="${verdict}"]').click();
        })()`);
        for (let i = 0; i < 300; i++) {
          const text = await page.evaluate(`document.getElementById("result").textContent`);
          if (text) return text;
          await sleep(100);
        }
        throw new Error("no result on the page");
      },
      async view(id) {
        const r = await request(port, "GET", `/request/${encodeURIComponent(id)}`, { host: `localhost:${port}` });
        const cookie = (r.headers["set-cookie"] || [])[0].split(";")[0];
        const csrf = r.body.match(/data-csrf="([^"]+)"/)[1];
        const fingerprint = r.body.match(/data-fingerprint="([^"]+)"/)[1];
        return { cookie, csrf, fingerprint };
      },
      post(p, fields, { cookie, origin: o = origin, host = `localhost:${port}` } = {}) {
        const body = form(fields);
        return request(port, "POST", p, {
          host, origin: o, cookie, "content-type": "application/x-www-form-urlencoded",
          "content-length": Buffer.byteLength(body),
        }, body);
      },
      async tamperBegin(edit) {
        await page.send("Fetch.enable", { patterns: [{ urlPattern: "*/ceremony/begin", requestStage: "Response" }] });
        const off = cdp.on(async (msg) => {
          if (msg.method !== "Fetch.requestPaused" || msg.sessionId !== page.sessionId) return;
          const ev = msg.params;
          const { body, base64Encoded } = await page.send("Fetch.getResponseBody", { requestId: ev.requestId });
          const text = base64Encoded ? Buffer.from(body, "base64").toString() : body;
          let out = text;
          try { out = JSON.stringify(edit(JSON.parse(text))); } catch { /* not a challenge: unchanged */ }
          await page.send("Fetch.fulfillRequest", {
            requestId: ev.requestId, responseCode: ev.responseStatusCode,
            responseHeaders: (ev.responseHeaders || []).filter((h) => h.name.toLowerCase() !== "content-length"),
            body: Buffer.from(out).toString("base64"),
          });
        });
        return async () => { off(); await page.send("Fetch.disable"); };
      },
    };

    for (const flow of selected) {
      const started = Date.now();
      let timer;
      try {
        const detail = await Promise.race([
          flow.run(t),
          new Promise((_, reject) => { timer = setTimeout(() => reject(new Error(`flow timeout ${FLOW_TIMEOUT_MS} ms`)), FLOW_TIMEOUT_MS); }),
        ]);
        results.push({ name: flow.name, ok: true, detail });
      } catch (e) {
        results.push({ name: flow.name, ok: false, detail: e.message });
      } finally {
        clearTimeout(timer);
      }
      const r = results[results.length - 1];
      log(`flow ${r.ok ? "passed" : "FAILED"} in ${Date.now() - started} ms: ${flow.name}`);
    }

    // Final ledger state, read after the server released the ledger.
    await stop(server);
    const status = spawnLines(opt.fixture, ["status", "--ledger", ledger, "--requests", requestIds.join(",")],
      (e) => process.stderr.write(`fixture: ${e}`));
    const final = JSON.parse(await status.nextLine()).status;
    await waitExit(status);
    for (const flow of selected) {
      for (const [id, want] of Object.entries(flow.expect)) {
        const got = final[id];
        results.push({ name: `${flow.name}: ledger status of ${id}`, ok: got === want, detail: `${got} (expected ${want})` });
      }
    }
  } finally {
    await cleanup();
  }
  let failed = 0;
  for (const r of results) {
    if (!r.ok) failed++;
    console.log(`${r.ok ? "PASS" : "FAIL"} ${r.name} [${LABEL}]: ${r.detail}`);
  }
  log(`${results.length - failed} passed, ${failed} failed (${LABEL})`);
  return failed === 0 && results.length > 0 ? 0 : 1;
}

main().then((code) => process.exit(code), (e) => {
  console.error(`browser suite: ERROR ${e.stack || e.message}`);
  process.exit(1);
});
