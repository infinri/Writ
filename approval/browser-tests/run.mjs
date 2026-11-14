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
//                [--only <substring>] [--results <file.json>] [--diagnostics <file.log>]
//
// CI mode (WRIT_BROWSER_CI=1, required CI, D-044 item 2): refuses `--only`, requires
// --results and --diagnostics, checks Node and Chrome against ci-manifest.json before any
// setup, and fails unless every flow of FLOW_MODULES (exactly expected_flow_count) ran and
// passed. Results, diagnostics and console output pass through lib/sanitize.mjs.
//
// Flows live in flows/*.mjs (see flows/approve-deny.mjs for the shape); add a module to
// FLOW_MODULES to cover further pages.

import { execFileSync } from "node:child_process";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";

import { Cdp, Page } from "./lib/cdp.mjs";
import {
  closeServer, driveFixture, form, freePort, launchChrome, request, sleep, spawnLines,
  staticServer, stop, waitExit,
} from "./lib/harness.mjs";
import { sanitize, sanitizeDeep } from "./lib/sanitize.mjs";

const FLOW_MODULES = ["./flows/approve-deny.mjs", "./flows/operator-pages.mjs"];
const FLOW_TIMEOUT_MS = 60000;
const LABEL = "CDP virtual authenticator, simulated UV";
const ISSUER = "writ-local";
const SUBJECTS = {
  admin: "browser-admin", approver: "browser-approver", target: "browser-target",
  reviewer: "browser-reviewer", excluded: "browser-excluded",
};
const SIGNOFF_ID = "so-browser";
const CI = process.env.WRIT_BROWSER_CI === "1";
const MANIFEST = new URL("./ci-manifest.json", import.meta.url);

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

// Diagnostics (harness log, server and fixture stderr, Chrome console), sanitized as captured.
const diagnostics = [];
const diag = (source, text) => {
  for (const line of sanitize(text).split("\n")) if (line.trim()) diagnostics.push(`[${source}] ${line}`);
};
const log = (line) => { diag("harness", line); console.log(`browser suite: ${sanitize(line)}`); };
const stderrTo = (source) => (e) => { diag(source, e); process.stderr.write(`${source}: ${sanitize(e)}`); };

/** Resolved tool versions; in CI mode a missing or wrong tool is an error before any setup. */
function tooling(opt, manifest) {
  const tools = { node: process.versions.node, node_path: process.execPath, chrome_path: opt.chrome };
  try {
    tools.chrome = execFileSync(opt.chrome, ["--version"], { encoding: "utf8", timeout: 20000 }).trim();
  } catch (e) {
    tools.chrome = null;
    tools.chrome_error = e.message.split("\n")[0];
  }
  log(`tooling: Node ${tools.node} (${tools.node_path}); Chrome ${tools.chrome || `unavailable: ${tools.chrome_error}`} (${opt.chrome})`);
  if (CI) {
    const nodeMajor = Number(tools.node.split(".")[0]);
    if (nodeMajor !== manifest.node_major) throw new Error(`CI: Node ${tools.node} is not major ${manifest.node_major}`);
    if (!tools.chrome) throw new Error(`CI: Chrome unavailable at ${opt.chrome}: ${tools.chrome_error}`);
    const m = tools.chrome.match(/(\d+)\.\d+\.\d+\.\d+/);
    if (!m || Number(m[1]) < manifest.chrome_min_major) {
      throw new Error(`CI: Chrome "${tools.chrome}" is not a supported version (major >= ${manifest.chrome_min_major})`);
    }
  }
  return tools;
}

const report = {
  suite: "approval browser suite", label: LABEL, ci_mode: CI, started_at: new Date().toISOString(),
  tools: {}, manifest: null, flow_modules: FLOW_MODULES, loaded_flows: [], executed_flows: [],
  results: [], passed: 0, failed: 0, ok: false, error: null,
};

/** Write the sanitized results and diagnostics files (when asked for). */
function writeOutputs(opt) {
  if (opt.results) fs.writeFileSync(opt.results, JSON.stringify(sanitizeDeep(report), null, 1) + "\n");
  if (opt.diagnostics) fs.writeFileSync(opt.diagnostics, diagnostics.map(sanitize).join("\n") + "\n");
}

async function main(opt) {
  const manifest = JSON.parse(fs.readFileSync(MANIFEST, "utf8"));
  report.manifest = { expected_flow_count: manifest.expected_flow_count, node_major: manifest.node_major,
    chrome_min_major: manifest.chrome_min_major };
  if (CI) {
    if (opt.only !== undefined) throw new Error("CI: --only is refused; required CI runs the complete suite");
    const unknown = Object.keys(opt).filter((k) => !["server", "fixture", "chrome", "results", "diagnostics"].includes(k));
    if (unknown.length) throw new Error(`CI: argument(s) refused: ${unknown.map((k) => `--${k}`).join(" ")}`);
    if (!opt.results || !opt.diagnostics) throw new Error("CI: --results and --diagnostics are required");
  }
  report.tools = tooling(opt, manifest);
  const flows = [];
  for (const m of FLOW_MODULES) {
    const mod = (await import(m)).flows;
    if (!Array.isArray(mod) || mod.length === 0) throw new Error(`flow module ${m} exports no flows`);
    flows.push(...mod);
  }
  report.loaded_flows = flows.map((f) => f.name);
  if (new Set(report.loaded_flows).size !== flows.length) throw new Error("duplicate flow names");
  if (CI && flows.length !== manifest.expected_flow_count) {
    throw new Error(`CI: ${flows.length} flows loaded from FLOW_MODULES, ci-manifest.json expects ${manifest.expected_flow_count}`);
  }
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
    report.tools.chrome_cdp = (await cdp.send("Browser.getVersion")).product;
    log(`Chrome over CDP: ${report.tools.chrome_cdp}`);
    // Chrome console messages and page exceptions of every attached page (sanitized).
    cdp.on((msg) => {
      if (msg.method === "Runtime.consoleAPICalled") {
        diag(`chrome console ${msg.params.type}`, msg.params.args.map((a) => (a.value !== undefined ? String(a.value) : a.description || a.type)).join(" "));
      } else if (msg.method === "Runtime.exceptionThrown") {
        const d = msg.params.exceptionDetails;
        diag("chrome exception", (d.exception && d.exception.description) || d.text);
      } else if (msg.method === "Log.entryAdded") {
        diag(`chrome log ${msg.params.entry.level}`, `${msg.params.entry.text} ${msg.params.entry.url || ""}`);
      }
    });
    const page = await Page.open(cdp);
    await page.send("Log.enable");
    await page.addVirtualAuthenticator();
    log(`virtual authenticator ${page.authenticatorId} (${LABEL})`);

    // Setup: the fixture bootstraps the administrator and enrolls the approver with
    // credentials created in this browser, on a placeholder page at the server's origin.
    servers.setup = await staticServer(port, "writ browser suite setup");
    await page.navigate(`${origin}/setup`);
    const fixture = spawnLines(opt.fixture, [
      "setup", "--ledger", ledger, "--origin", origin, "--issuer", ISSUER,
      "--admin", SUBJECTS.admin, "--approver", SUBJECTS.approver, "--target", SUBJECTS.target,
      "--signoff", SIGNOFF_ID, "--reviewer", SUBJECTS.reviewer, "--excluded", SUBJECTS.excluded,
      "--requests", requestIds.join(","),
    ], stderrTo("fixture"));
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
    ], stderrTo("server"));
    children.server = server;
    const ready = await server.nextLine(20000);
    if (!ready.includes(`serving ${origin}`)) throw new Error(`server: ${ready}`);
    log(ready);
    servers.wrong = await staticServer(wrongPort, "writ browser suite other origin");

    const t = {
      page, port, origin, wrongPort, wrongOrigin, fingerprints: done.requests,
      subjects: { ...SUBJECTS },
      signoffId: SIGNOFF_ID,
      // Flows record what the ledger must hold for a signoff ({ subject, expires_at }).
      signoffExpect: {},
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
        // Raw HTTP is not a browser: it states the Fetch Metadata of a direct navigation.
        const r = await request(port, "GET", `/request/${encodeURIComponent(id)}`, { host: `localhost:${port}`, "sec-fetch-site": "none" });
        const cookie = (r.headers["set-cookie"] || [])[0].split(";")[0];
        const csrf = r.body.match(/data-csrf="([^"]+)"/)[1];
        const fingerprint = r.body.match(/data-fingerprint="([^"]+)"/)[1];
        return { cookie, csrf, fingerprint };
      },
      post(p, fields, { cookie, origin: o = origin, host = `localhost:${port}`, site = "same-origin" } = {}) {
        const body = form(fields);
        // Raw HTTP is not a browser: it states the Fetch Metadata of this origin's page script.
        return request(port, "POST", p, {
          host, origin: o, cookie, "sec-fetch-site": site, "content-type": "application/x-www-form-urlencoded",
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
      report.executed_flows.push(flow.name);
      let timer;
      try {
        const detail = await Promise.race([
          flow.run(t),
          new Promise((_, reject) => { timer = setTimeout(() => reject(new Error(`flow timeout ${FLOW_TIMEOUT_MS} ms`)), FLOW_TIMEOUT_MS); }),
        ]);
        results.push({ name: flow.name, kind: "flow", ok: true, detail });
      } catch (e) {
        results.push({ name: flow.name, kind: "flow", ok: false, detail: e.message });
      } finally {
        clearTimeout(timer);
      }
      const r = results[results.length - 1];
      log(`flow ${r.ok ? "passed" : "FAILED"} in ${Date.now() - started} ms: ${flow.name}`);
    }

    // Final ledger state, read after the server released the ledger.
    await stop(server);
    const status = spawnLines(opt.fixture, ["status", "--ledger", ledger, "--requests", requestIds.join(","),
      "--signoffs", SIGNOFF_ID],
      stderrTo("fixture"));
    const finalAll = JSON.parse(await status.nextLine());
    const final = finalAll.status;
    await waitExit(status);
    for (const flow of selected) {
      for (const [id, want] of Object.entries(flow.expect)) {
        const got = final[id];
        results.push({ name: `${flow.name}: ledger status of ${id}`, kind: "ledger-status", ok: got === want,
          expected: want, actual: got === undefined ? null : got, detail: `${got} (expected ${want})` });
      }
    }
    for (const [id, want] of Object.entries(t.signoffExpect)) {
      const got = finalAll.signoffs[id];
      const ok = !!got && got.subject === want.subject && got.expires_at === want.expires_at;
      results.push({ name: `stored signoff ${id}`, kind: "stored-signoff", ok,
        expected: "signer and displayed expiry", actual: ok ? "match" : "mismatch",
        // Outcome only: the stored record is ledger content and stays out of logs and results.
        detail: !got ? "no stored signoff" : `stored signer ${got.subject === want.subject ? "matches" : "differs from"} the expected signer; `
          + `stored expiry ${got.expires_at === want.expires_at ? "equals" : "differs from"} the displayed expiry` });
    }
  } finally {
    await cleanup();
  }
  let failed = 0;
  for (const r of results) {
    if (!r.ok) failed++;
    const line = `${r.ok ? "PASS" : "FAIL"} ${r.name} [${LABEL}]: ${r.detail}`;
    diag("result", line);
    console.log(sanitize(line));
    // The results file keeps names, outcomes and expected/actual statuses only: no flow
    // detail text (it may quote page or ledger content); a failed flow keeps its sanitized reason.
    const entry = { name: r.name, kind: r.kind, ok: r.ok };
    if (r.expected !== undefined) Object.assign(entry, { expected: r.expected, actual: r.actual });
    if (!r.ok && r.kind === "flow") entry.failure = r.detail;
    report.results.push(entry);
  }
  report.passed = results.length - failed;
  report.failed = failed;
  // Completeness: every selected flow ran in order and produced its result and its ledger
  // checks; in CI mode the selection is the complete FLOW_MODULES list of the manifest's size.
  const want = selected.map((f) => f.name);
  const same = (a, b) => JSON.stringify(a) === JSON.stringify(b);
  const ledgerChecks = selected.reduce((n, f) => n + Object.keys(f.expect).length, 0);
  const complete = same(report.executed_flows, want)
    && same(results.filter((r) => r.kind === "flow").map((r) => r.name), want)
    && results.filter((r) => r.kind === "ledger-status").length === ledgerChecks
    && (!CI || (same(want, report.loaded_flows) && want.length === manifest.expected_flow_count));
  if (!complete) {
    report.error = `incomplete run: ${report.executed_flows.length} of ${want.length} selected flows executed `
      + `(${report.loaded_flows.length} loaded; manifest expects ${manifest.expected_flow_count})`;
    log(report.error);
  }
  log(`${results.length - failed} passed, ${failed} failed; ${report.executed_flows.length} of ${report.loaded_flows.length} flows executed (${LABEL})`);
  report.ok = complete && failed === 0 && results.length > 0;
  return report.ok ? 0 : 1;
}

let opt = {};
try {
  opt = args();
} catch (e) {
  console.error(`browser suite: ERROR ${e.message}`);
  process.exit(1);
}
main(opt).catch((e) => {
  report.error = e.message;
  report.ok = false;
  diag("harness", `ERROR ${e.stack || e.message}`);
  console.error(`browser suite: ERROR ${sanitize(e.stack || e.message)}`);
  return 1;
}).then((code) => {
  report.finished_at = new Date().toISOString();
  try {
    writeOutputs(opt);
    if (opt.results) console.log(`browser suite: sanitized results ${opt.results}, diagnostics ${opt.diagnostics || "(not written)"}`);
  } catch (e) {
    console.error(`browser suite: ERROR writing results: ${e.message}`);
    code = 1;
  }
  process.exit(code);
});
