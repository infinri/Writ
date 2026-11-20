// Exercise the offline lifecycle view using real fixture CLI reads and the reviewed CDP client.
import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import { mkdtempSync, readFileSync, writeFileSync } from "node:fs";
import { rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import path from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";
import { Cdp, Page } from "../../../approval/browser-tests/lib/cdp.mjs";
import { launchChrome, stop, waitExit } from "../../../approval/browser-tests/lib/harness.mjs";

const management = fileURLToPath(new URL("../", import.meta.url));
const chromePath = process.env.CHROME || "/usr/bin/google-chrome";
const browserPolicy = JSON.parse(readFileSync(new URL(
  "../../../approval/browser-tests/ci-manifest.json", import.meta.url)));
const view = path.join(management, "policy-lifecycle-view.py");
const cli = path.resolve(process.env.WRIT_MANAGEMENT_BIN_DIR || path.join(management, "target/debug"),
  "writ-manage-fixture");
const lab = mkdtempSync(path.join(tmpdir(), "writ-policy-view-"));
const drafts = path.join(lab, "drafts.sqlite"), registry = path.join(lab, "registry.sqlite");
const viewer = path.join(lab, "viewer"), snapshot = path.join(lab, "view.html");
const scope = { tenant: "m6-lab", project: "offline-proof" };
let chrome, cdp;

function run(program, args, input, expectedStatus = 0) {
  const r = spawnSync(program, args, { input, encoding: "utf8", timeout: 60000 });
  if (r.error) throw r.error;
  assert.equal(r.status, expectedStatus, `${path.basename(program)} unexpectedly exited ${r.status}`);
  return r.stdout;
}

try {
  assert.equal(Number(process.versions.node.split(".")[0]), browserPolicy.node_major);
  const chromeVersion = run(chromePath, ["--version"]).match(/(\d+)\.\d+\.\d+\.\d+/);
  assert.ok(chromeVersion && Number(chromeVersion[1]) >= browserPolicy.chrome_min_major);
  run("python3", ["-c",
    'import runpy, sys; from pathlib import Path; runpy.run_path(sys.argv[1])["run"](Path(sys.argv[2]))',
    path.join(management, "run-publication-lab.py"), lab]);
  writeFileSync(viewer, "fixture-viewer");
  chrome = await launchChrome(chromePath, path.join(lab, "profile"), lab);
  cdp = await Cdp.connect(chrome.wsUrl);
  const page = await Page.open(cdp);
  const requests = [];
  cdp.on(msg => {
    if (msg.sessionId === page.sessionId && msg.method === "Network.requestWillBeSent") {
      requests.push(msg.params.request.url);
    }
  });
  await page.send("Network.enable");

  async function load(html) {
    writeFileSync(snapshot, html);
    await page.navigate(pathToFileURL(snapshot).href);
  }

  function read(scenario, credential, tenant, project, after, limit, status,
    managementDb = drafts, publicationDb = registry) {
    return JSON.parse(run(cli, ["v1-policy", managementDb, credential, publicationDb, scenario],
      JSON.stringify({ version: "/manage/v1", operation: "policy.lifecycle.inspect",
        scope: {tenant, project}, after, limit }), status));
  }

  async function inspect({scenario = "current", credential = viewer, tenant = scope.tenant,
    project = scope.project, after = 0, limit = 50, managementDb = drafts, publicationDb = registry, draftId, draftAfter = 0, draftLimit = 50, draftError, error} = {}) {
    const status = error ? 1 : 0;
    const response = read(scenario, credential, tenant, project, after, limit, status, managementDb, publicationDb);
    await load(run("python3", [view, managementDb, credential, publicationDb, "--scenario", scenario,
      "--tenant", tenant, "--project", project, "--after", String(after), "--limit", String(limit),
      ...(draftId ? ["--draft-id", draftId, "--draft-after", String(draftAfter), "--draft-limit", String(draftLimit)] : [])],
      undefined, draftError ? 1 : status));
    if (error || draftError) {
      if (error) assert.equal(response.error.code, error);
      assert.equal(await page.evaluate('document.querySelector("[role=status]").innerText'),
        `Inspection failed. ${error || draftError}`);
      assert.equal(await page.evaluate('document.querySelectorAll("article, details, dl").length'), 0);
      const text = await page.evaluate("document.body.innerText");
      assert.ok(!text.includes("m6.synthetic") && !text.includes(scope.project));
      if (draftId) assert.ok(!text.includes(draftId));
      return response;
    }

    const {enforcement_labels: labels, history} = response.result;
    const rows = await page.evaluate(`Array.from(document.querySelectorAll("article"), row => ({
      subject: row.querySelector("h3").innerText, labels: [...row.querySelectorAll(".label")].map(x => x.innerText),
      fields: Array.from(row.querySelectorAll("dd"), dd => dd.querySelector("ul")
        ? Array.from(dd.querySelectorAll("li"), li => li.textContent) : dd.textContent)
    }))`);
    assert.deepEqual(rows.map(r => [r.subject, r.labels]), labels.map(r => [r.subject, [r.label]]));
    function visible(x) {
      if (x === null || (Array.isArray(x) && !x.length)) return "None recorded";
      if (Array.isArray(x)) return x;
      return typeof x === "object" ? JSON.stringify(x, null, 2) : String(x);
    }
    for (const [i, row] of labels.entries()) {
      assert.deepEqual(rows[i].fields, ["kind", "profile", "scope", "policy", "implementation",
        "enforcement_point", "positive_tests", "negative_tests", "freshness", "compatibility",
        "reasons"].map(key => visible(row[key])));
    }
    const selected = history.selected;
    assert.deepEqual(await page.evaluate(
      'Array.from(document.querySelectorAll("main > dl > dd"), dd => dd.innerText)'),
      [String(selected.publication_version), String(selected.revision), String(selected.audit_id),
        selected.verified ? "Yes" : "No"]);
    const events = await page.evaluate(`Array.from(document.querySelectorAll("details"), el => {
      el.open = true;
      return el.innerText;
    })`);
    assert.equal(events.length, history.events.length);
    for (const [i, event] of history.events.entries()) {
      assert.ok(events[i].includes(event.publication_digest) && events[i].includes(event.intent_digest));
    }
    const cursor = await page.evaluate('document.querySelector("#publication-cursor")?.innerText || null');
    assert.equal(cursor, history.next === null ? null : `--after ${history.next}`);
    const inventory = await page.evaluate('Array.from(document.querySelectorAll("#publications dl"), '
      + 'dl => Array.from(dl.querySelectorAll("dd"), dd => dd.innerText))');
    assert.deepEqual(inventory, Array.from(new Map(history.events.map(e =>
      [String(e.publication_version), e.publication_digest])).entries()));
    if (draftId) {
      const result = JSON.parse(run(cli, ["v1", managementDb, credential],
        JSON.stringify({version: "/manage/v1", scope: {tenant, project}, operation: "history",
          id: draftId, after: draftAfter, limit: draftLimit}))).result;
      const versions = await page.evaluate('Array.from(document.querySelectorAll("#drafts dl"), '
        + 'dl => Array.from(dl.querySelectorAll("dd"), dd => dd.textContent))');
      assert.deepEqual(versions, result.versions.map(v => [v.id, String(v.revision), v.digest, visible(v.source)]));
      assert.equal(await page.evaluate('document.querySelector("#drafts code")?.innerText || null'),
        result.next === null ? null : `--draft-after ${result.next}`);
    }
    return labels;
  }

  const draftId = "inventory-clone";
  manage("clone", "author", {id: draftId, expected_revision: 0, source: {id: "permit", revision: 1},
    idempotency_key: "browser-inspect-clone", preview: false});
  manage("edit", "author", {id: draftId, expected_revision: 1,
    candidate: JSON.parse(readFileSync(path.join(management, "../compiler/fixtures/deny.input.json"))),
    idempotency_key: "browser-inspect-edit", preview: false});
  const current = await inspect({limit: 1, draftId, draftLimit: 1});
  await inspect({after: 1, draftId, draftAfter: 1});
  await inspect({draftId: "absent-draft", draftError: "NOT_FOUND"});
  await inspect({after: 1});
  const baselineLabels = new Set(current.map(row => row.label));
  for (const expected of ["ENFORCED", "HOOK-INTEGRATED (ADVISORY)", "ADVISORY",
    "UNMEDIATED", "UNVERIFIED"]) assert.ok(baselineLabels.has(expected), expected);
  const proven = current.find(r => r.subject === "m6.synthetic.permit" && r.label === "ENFORCED");
  assert.ok(proven, "selected permit has recorded synthetic enforcement proof");
  const faults = {
    removed: "ENFORCEMENT_POINT_MISSING", stale: "STALE_EVIDENCE", mismatched: "PROFILE_MISMATCH",
    unavailable: "EVIDENCE_UNAVAILABLE", revoked: "EVIDENCE_REVOKED",
    "missing-positive": "POSITIVE_TEST_MISSING", "missing-negative": "NEGATIVE_TEST_MISSING",
  };
  for (const [scenario, reason] of Object.entries(faults)) {
    const row = (await inspect({scenario})).find(r => r.subject === proven.subject);
    assert.equal(row.label, "UNVERIFIED", scenario);
    assert.ok(row.reasons.includes(reason), scenario);
  }
  await inspect({tenant: "another-tenant", error: "NOT_FOUND"});
  await inspect({project: "another-project", error: "NOT_FOUND"});
  for (const [name, error] of [["fixture-agent", "UNAUTHORIZED"], ["fixture-adapter", "UNAUTHORIZED"],
    ["fixture-service", "NOT_FOUND"], ["fixture-waiver-operator", "NOT_FOUND"], ["unknown", "UNAUTHORIZED"]]) {
    const credential = path.join(lab, name);
    writeFileSync(credential, name);
    await inspect({credential, error});
  }

  const absentRegistry = path.join(lab, "absent-registry.sqlite");
  const absentManagement = path.join(lab, "absent-management.sqlite");
  for (const refusal of [{credential: path.join(lab, "unknown"), error: "UNAUTHORIZED"},
    {tenant: "another-tenant", error: "NOT_FOUND"}, {project: "another-project", error: "NOT_FOUND"}]) {
    const healthy = await inspect({...refusal, draftId});
    for (const missing of [{managementDb: absentManagement}, {publicationDb: absentRegistry}]) {
      assert.deepEqual(await inspect({...refusal, ...missing, draftId}), healthy);
      const request = {version: "/manage/v1", operation: "policy.lifecycle.inspect", after: 0, limit: 50,
        scope: {tenant: refusal.tenant || scope.tenant, project: refusal.project || scope.project}};
      assert.deepEqual(JSON.parse(run(cli, ["v1", missing.managementDb || drafts, refusal.credential || viewer,
        missing.publicationDb || registry], JSON.stringify(request), 1)), healthy);
    }
  }
  for (const invalid of [{operation: "publication_history"}, {caller_role: "Publisher"}, {limit: 51}]) {
    const request = {version: "/manage/v1", scope, after: 0, limit: 1,
      operation: "policy.lifecycle.inspect", ...invalid};
    const response = JSON.parse(run(cli, ["v1-policy", absentManagement, viewer, absentRegistry, "current"],
      JSON.stringify(request), 1));
    assert.equal(response.error.code, "INVALID_REQUEST");
  }
  await inspect({publicationDb: absentRegistry, draftId, error: "UNAVAILABLE"});
  await inspect({managementDb: absentManagement, draftId, error: "UNAVAILABLE"});

  // Switch the selected signed policy through the same reviewed publication lifecycle.
  const candidate = JSON.parse(readFileSync(path.join(management, "../compiler/fixtures/approval.input.json")));
  const signer = path.join(path.dirname(cli), "writ-publication-authority-fixture");
  function manage(operation, credential, fields) {
    return JSON.parse(run(cli, ["v1", drafts, path.join(lab, credential), registry],
      JSON.stringify({version: "/manage/v1", scope, operation, ...fields})));
  }
  const draft = {id: "approval", revision: 1};
  manage("create", "author", {id: draft.id, expected_revision: 0,
    idempotency_key: "browser-approval-draft", preview: false, candidate});
  const publication = JSON.parse(run(signer, ["sign-publication", "3"], JSON.stringify(candidate)));
  const proposal = {draft, publication, expected_revision: 3, publication_version: 3,
    acknowledge_self: false, idempotency_key: "browser-approval-selection"};
  const preview = manage("preview_publish", "publisher", proposal);
  const {presentation} = JSON.parse(run(signer, ["authorize-publication", path.join(lab, "step-up"),
    "browser-approval-selection"], JSON.stringify(preview)));
  manage("publish", "publisher", {...proposal, intent_digest: preview.result.intent_digest, presentation});
  const approvalRows = await inspect();
  assert.ok(approvalRows.some(row => row.subject === "m6.synthetic.approval" &&
    row.label === "APPROVAL REQUIRED"));
  assert.notEqual(approvalRows.find(row => row.subject === proven.subject).policy, proven.policy);

  // Only this escaping control alters presentation input. All label cases above use the owner.
  const malicious = read("current", viewer, scope.tenant, scope.project, 0, 1, 0);
  const marker = '<img src=x onerror="document.title=42">';
  malicious.result.enforcement_labels[0].reasons = [marker];
  await load(run("python3", ["-c", 'import json, runpy, sys; m=runpy.run_path(sys.argv[1]); '
    + 'print(m["document"](m["view"](json.load(sys.stdin))))', view], JSON.stringify(malicious)));
  assert.ok((await page.evaluate("document.body.innerText")).includes(marker));
  assert.equal(await page.evaluate('document.querySelectorAll("img, script, iframe, a, form").length'), 0);
  assert.ok(requests.every(url => url === pathToFileURL(snapshot).href), "page requested a subresource");
  console.log("Policy lifecycle browser passed label, provenance, pagination, isolation and text-safety checks.");
} finally {
  if (cdp) {
    try {
      await cdp.send("Browser.close", {}, undefined, 5000);
    } catch {
      await stop(chrome?.child);
    }
    cdp.close();
  } else {
    await stop(chrome?.child);
  }
  if (chrome) await waitExit(chrome.child);
  await rm(lab, {recursive: true, force: true, maxRetries: 10, retryDelay: 150});
}
