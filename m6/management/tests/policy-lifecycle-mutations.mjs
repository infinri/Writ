import assert from "node:assert/strict";
import path from "node:path";
import { readFileSync } from "node:fs";
import { randomBytes } from "node:crypto";
import { spawnLines, stop } from "../../../approval/browser-tests/lib/harness.mjs";

export async function checkMutationForm({page, management, drafts, registry, signer, run, lab, manage, cli}) {
  const contracts = path.join(lab, "browser-contracts.sqlite"), evaluator = path.join(lab, "browser-evaluator.sqlite");
  run(cli, ["init-evaluator", evaluator]);
  run(cli, ["init-packs", contracts, evaluator]);
  const catalogPath = path.join(lab, "ui-identities.sqlite");
  const tokens = Object.fromEntries(["publisher", "author", "viewer", "agent"].map(role =>
    ["fixture-" + role, randomBytes(32).toString("hex")]));
  run("python3", ["-c", `
import hashlib, json, sqlite3, sys, time
from pathlib import Path
with sqlite3.connect(sys.argv[1]) as db:
    db.executescript(Path(sys.argv[2]).read_text())
    for subject, token in json.load(sys.stdin).items():
        role = subject.removeprefix("fixture-")
        realm = "agent" if role == "agent" else "management"
        db.execute("INSERT INTO principals VALUES(?,?,1)", (subject, realm))
        fingerprint = "sha256:" + hashlib.sha256(token.encode()).hexdigest()
        now = int(time.time())
        db.execute("INSERT INTO credentials VALUES(?,?,?,?,0)", (fingerprint, subject, now-60, now+600))
        db.execute("INSERT INTO scoped_grants VALUES(?,?,?,?)", (subject, "m6-lab", "offline-proof",
                   {"author":"policy-author","agent":"publisher"}.get(role, role)))
`, catalogPath, path.join(management, "src/identity.sql")], JSON.stringify(tokens));
  const host = spawnLines("env", ["WRIT_MANAGEMENT_IDENTITY_DB=" + catalogPath, "python3", path.join(management, "lifecycle-ui.py"), drafts, registry,
    "--contract-db", contracts, "--evaluator-db", evaluator]);
  try {
    const url = await host.nextLine();
    const history = () => manage("publication_history", "publisher", {after: 0, limit: 50}).result;
    const baseline = history();
    await page.navigate(url);
    assert.deepEqual(history(), baseline);
    assert.equal((await fetch(new URL("/manage/v1", url), {method: "POST", headers: {Origin: "http://other.test"}})).status, 403);

    async function submit(op, fields, button, credential = "fixture-publisher") {
      return page.call(async (operation, values, buttonId, cred) => {
        const form = document.querySelector("form");
        form.operation.value = operation;
        form.operation.dispatchEvent(new Event("change"));
        form.project.value = "offline-proof";
        form.credential.value = cred;
        for (const [name, value] of Object.entries(values)) form.elements[name].value = value;
        const result = document.querySelector("[role=status]");
        await new Promise((resolve, reject) => {
          const observer = new MutationObserver(() => {
            clearTimeout(timer);
            observer.disconnect();
            resolve();
          });
          const timer = setTimeout(() => { observer.disconnect(); reject(new Error("No UI response")); }, 25000);
          observer.observe(result, {childList: true});
          form.querySelector("#" + buttonId).click();
        });
        if (form.credential.value || form.presentation.value) throw new Error("Credential retained");
        return JSON.parse(result.textContent);
      }, op, fields, button, tokens[credential] || credential);
    }

    const catalog = await submit("contracts.browse", {}, "apply", "fixture-viewer");
    assert.ok(catalog.result.contracts.some(row => row.contract_id === "decision"));
    assert.ok((await submit("contracts.inspect", {contract_id: "decision"}, "apply", "fixture-viewer"))
      .result.versions[0].schema.properties);
    assert.equal((await submit("contracts.create", {contract_id: "decision", contract_version: "1.0.0",
      expected_revision: 0, schema: JSON.stringify({type: "object"}), idempotency_key: "ui-core"},
      "apply", "fixture-author")).error.code, "PROTECTED_CONTRACT");
    assert.equal((await submit("packs.inspect", {pack_id: "absent", pack_version: "1.0.0"}, "apply",
      "fixture-agent")).error.code, "UNAUTHORIZED");

    const expected_revision = baseline.selected.revision;
    const mandatory = {expected_revision, publication_version: baseline.selected.publication_version,
      policy_id: "review", confirmation: "deactivate review"};
    for (const button of ["preview", "apply"]) {
      assert.equal((await submit("deactivate", mandatory, button)).error.code, "INVALID_POLICY");
      assert.deepEqual(history(), baseline);
    }
    assert.equal((await submit("deactivate", mandatory, "apply", "fixture-agent")).error.code, "UNAUTHORIZED");
    assert.equal((await submit("deactivate", {...mandatory, project: "outside"}, "apply")).error.code, "NOT_FOUND");

    const clone = {id: "browser-form", expected_revision: 0, idempotency_key: "browser-form-create",
      source_id: "permit", source_revision: 1};
    assert.ok((await submit("clone", clone, "preview", "fixture-author")).ok);
    assert.equal((await submit("get", {draft_id: clone.id, draft_revision: 1}, "apply", "fixture-author")).error.code,
      "NOT_FOUND");
    assert.ok((await submit("clone", clone, "apply", "fixture-author")).ok);
    assert.ok((await submit("validate", {draft_id: clone.id, draft_revision: 1}, "apply", "fixture-author")).ok);

    const comparison = await submit("compare_active", {draft_id: clone.id, draft_revision: 1,
      corpus: JSON.stringify([{kind: "synthetic.inspect", resource: "synthetic:m6-lab/offline-proof/inventory"}])},
      "apply", "fixture-author");
    assert.equal(comparison.result.runtime_authority, false);
    assert.equal(comparison.result.publication_version, baseline.selected.publication_version);
    assert.equal(comparison.result.cases.length, 1);

    const fields = {expected_revision, publication_version: 1, idempotency_key: "ui-select-permit"};
    const preview = await submit("rollback", fields, "preview");
    assert.ok(preview.ok);
    assert.deepEqual(history(), baseline);
    assert.equal((await submit("rollback", fields, "apply")).error.code, "INVALID_REQUEST");
    const {presentation} = JSON.parse(run(signer, ["authorize-publication", path.join(lab, "step-up"),
      fields.idempotency_key], JSON.stringify(preview)));
    const approved = {...fields, intent_digest: preview.result.intent_digest, presentation};
    assert.equal((await submit("rollback", {...approved, idempotency_key: "altered-ui-selection"},
      "apply")).ok, false);
    assert.deepEqual(history(), baseline);
    const committed = await submit("rollback", approved, "apply");
    assert.ok(committed.ok);
    const final = history();
    assert.equal(final.selected.publication_version, fields.publication_version);
    assert.equal(final.selected.audit_id, committed.result.audit_id);
    assert.equal(final.events.length, baseline.events.length + 1);
    assert.deepEqual((await submit("rollback", approved, "apply")).result, committed.result);
    assert.deepEqual(history(), final);
    const authorization = JSON.parse(readFileSync(path.join(management, "../compiler/fixtures/permit.input.json")));
    const review = {criterion_id: "review", required_class: "human"};
    const evidence = {criterion_id: "evidence", required_class: "mechanical"};
    async function proposePair(id, version, revision, criteria) {
      const candidate = {authorization, acceptance: {version: "writ-acceptance-policy/1",
        policy_id: "m6-acceptance", policy_version: String(version), criteria}};
      assert.ok((await submit("create", {id, expected_revision: 0, idempotency_key: "create-" + id,
        candidate: JSON.stringify(candidate)}, "apply", "fixture-author")).ok);
      return {draft_id: id, draft_revision: 1, expected_revision: revision, publication_version: version,
        publication: run(signer, ["sign-publication", String(version)], JSON.stringify(candidate)),
        idempotency_key: id};
    }
    const pair = await proposePair("ui-add-required", 4, final.selected.revision, [review, evidence]);
    const display = await submit("publish", pair, "preview");
    assert.ok(display.ok);
    const step = JSON.parse(run(signer, ["authorize-publication", path.join(lab, "step-up"),
      pair.idempotency_key], JSON.stringify(display)));
    const pairReceipt = await submit("publish", {...pair, ...step, intent_digest: display.result.intent_digest}, "apply");
    assert.ok(pairReceipt.ok);
    const protectedHistory = history();
    for (const [change, criteria] of [["remove", [review]], ["edit", [review, {...evidence, required_class: "human"}]],
      ["reorder", [evidence, review]]]) {
      const forbidden = await proposePair("ui-forbidden-" + change, 5, protectedHistory.selected.revision, criteria);
      assert.equal((await submit("publish", forbidden, "preview")).error.code, "INVALID_POLICY");
      assert.equal((await submit("publish", {...forbidden, ...step, intent_digest: display.result.intent_digest},
        "apply")).ok, false);
      assert.deepEqual(history(), protectedHistory);
    }
    run("python3", ["-c", 'import sqlite3, sys; db=sqlite3.connect(sys.argv[1]); '
      + 'db.execute("UPDATE credentials SET revoked=1 WHERE subject=?", ("fixture-publisher",)); db.commit()',
      catalogPath]);
    assert.equal((await submit("rollback", approved, "apply")).error.code, "UNAUTHORIZED");
    assert.deepEqual(history(), protectedHistory);
  } finally {
    await stop(host);
  }
}
