"use strict";
const form = document.querySelector("form");
const result = document.querySelector("[role=status]");
const fields = document.querySelector("#fields");
const draftChanges = ["create", "edit", "clone"];
const publicationChanges = ["publish", "rollback", "retire", "deactivate", "remove_optional"];
const registryChanges = ["contracts.publish", "contracts.deprecate", "contracts.retire",
  "packs.install", "packs.enable", "packs.disable", "packs.upgrade"];
const definitions = {
  create: ["Create draft", "id expected_revision idempotency_key candidate"],
  edit: ["Edit draft", "id expected_revision idempotency_key candidate"],
  clone: ["Clone draft", "id expected_revision idempotency_key source"],
  get: ["Inspect draft version", "draft"],
  history: ["Draft version history", "id after limit"],
  validate: ["Validate draft", "draft"],
  simulate: ["Simulate draft", "draft corpus"],
  compare: ["Compare drafts", "before after_draft corpus"],
  compare_active: ["Compare draft to active bundle", "draft corpus"],
  compare_recorded: ["Compare recorded transitions to draft", "draft record_ids"],
  publish: ["Publish and activate signed pair", "draft expected_revision publication_version publication idempotency_key acknowledge_self"],
  rollback: ["Roll back selection", "expected_revision publication_version idempotency_key acknowledge_self"],
  retire: ["Retire inactive version", "expected_revision publication_version confirmation idempotency_key acknowledge_self"],
  deactivate: ["Check direct policy deactivation", "expected_revision publication_version policy_id confirmation"],
  remove_optional: ["Deactivate optional policy with signed replacement", "draft expected_revision publication_version publication policy_id confirmation idempotency_key acknowledge_self"],
  "contracts.browse": ["Browse contract catalog", ""],
  "contracts.inspect": ["Inspect contract versions and dependencies", "contract_id"],
  "contracts.validate-schema": ["Validate contract schema", "schema"],
  "contracts.validate-example": ["Validate contract example", "contract_id contract_version example"],
  "contracts.create": ["Create custom contract draft", "contract_id contract_version schema expected_revision idempotency_key"],
  "contracts.publish": ["Publish custom contract", "contract_id contract_version expected_revision idempotency_key acknowledge_self_publication"],
  "contracts.deprecate": ["Deprecate contract version", "contract_id contract_version expected_revision replacement_version end_date idempotency_key confirmation"],
  "contracts.retire": ["Retire contract version", "contract_id contract_version expected_revision idempotency_key confirmation"],
  "contracts.recover": ["Recover contract receipt", "idempotency_key"],
  "packs.inspect": ["Inspect pack manifest and dependencies", "pack_id pack_version"],
  "packs.verify": ["Verify pack trust", "pack_id pack_version"],
  "packs.history": ["Pack selection and history", "pack_id"],
  "packs.install": ["Install signed pack", "pack_id pack_version expected_revision envelope idempotency_key"],
  "packs.enable": ["Enable pack", "pack_id pack_version expected_revision idempotency_key"],
  "packs.disable": ["Disable pack", "pack_id pack_version expected_revision idempotency_key confirmation"],
  "packs.upgrade": ["Upgrade pack", "pack_id pack_version expected_revision idempotency_key confirmation"],
  "packs.recover": ["Recover pack receipt", "idempotency_key"],
  publication_history: ["Publication versions and selection", "after limit"],
};
const descriptions = {
  pack_id: "Pack ID", pack_version: "Pack version", envelope: "Signed pack envelope (JSON)",
  contract_id: "Contract ID", contract_version: "Contract version", schema: "Contract schema (JSON)",
  example: "Example (JSON)", replacement_version: "Replacement version", end_date: "Deprecation end date",
  acknowledge_self_publication: "I acknowledge self-publication",
  id: "Draft ID", expected_revision: "Expected current revision", idempotency_key: "Idempotency key",
  candidate: "Policy candidate (JSON)", source: "Source draft", draft: "Draft",
  before: "Baseline draft", after_draft: "Candidate draft", corpus: "Sample requests (JSON array)",
  after: "After cursor", limit: "Page size", publication_version: "Publication version",
  publication: "Signed policy and acceptance pair (JSON)", confirmation: "Exact confirmation",
  record_ids: "Recorded transition IDs (JSON array)", policy_id: "Policy ID", acknowledge_self: "I acknowledge self-publication",
};
const refs = ["source", "draft", "before", "after_draft"];
const jsonFields = ["candidate", "corpus", "publication", "record_ids", "schema", "example", "envelope"];
const numberFields = ["expected_revision", "publication_version", "after", "limit"];
for (const [prefix, title] of [["", "Policy and acceptance"], ["contracts", "Contracts"], ["packs", "Capability packs"]]) {
  const group = document.createElement("optgroup");
  group.label = title;
  for (const [op, [label]] of Object.entries(definitions)) {
    if (prefix ? op.startsWith(prefix + ".") : !op.includes(".")) group.append(new Option(label, op));
  }
  form.operation.append(group);
}

function addField(name, labelText, type, initial = "") {
  const label = document.createElement("label");
  label.textContent = labelText;
  const input = document.createElement(type === "json" ? "textarea" : "input");
  if (type === "json") input.rows = 5;
  else input.type = type;
  if (type === "number") input.min = 0;
  input.name = name;
  input.required = type !== "checkbox";
  input.value = initial;
  label.append(input);
  fields.append(label);
}

function render() {
  fields.replaceChildren();
  result.textContent = "";
  form.intent_digest.value = form.presentation.value = "";
  const op = form.operation.value;
  const guidance = {
    create: 'Use a policy candidate, or a pair with "authorization" and "acceptance" JSON members.',
    edit: 'Submit the replacement candidate and current revision. The owner retains the prior version.',
    remove_optional: 'Supply the signed replacement and confirm with "deactivate <policy ID>".',
    deactivate: 'Direct deactivation checks the selected policy ID. Optional removal requires a signed replacement.',
    retire: 'Confirm with "retire <publication version>".',
    compare_recorded: 'Enter the IDs of transitions in the host-configured capture for this project.',
  };
  document.querySelector("#guidance").textContent = guidance[op] || "";
  for (const name of definitions[op][1].split(" ").filter(Boolean)) {
    if (refs.includes(name)) {
      addField(name + "_id", descriptions[name] + " ID", "text");
      addField(name + "_revision", descriptions[name] + " revision", "number", "1");
    } else {
      const type = jsonFields.includes(name) ? "json" : numberFields.includes(name) ? "number"
        : name.startsWith("acknowledge_self") ? "checkbox" : "text";
      addField(name, descriptions[name], type, {limit: "50", after: "0", expected_revision: op === "edit" ? "1" : "0"}[name]);
    }
  }
  const mutable = draftChanges.includes(op) || publicationChanges.includes(op) || registryChanges.includes(op);
  document.querySelector("#preview").hidden = !mutable;
  document.querySelector("#authority").hidden = !(publicationChanges.includes(op) || registryChanges.includes(op)) || op === "deactivate";
  document.querySelector("#apply").textContent = mutable || op === "contracts.create" ? "Apply" : "Inspect";
}
form.operation.onchange = render;
render();

form.onsubmit = async event => {
  event.preventDefault();
  const operation = form.operation.value;
  const preview = event.submitter?.id === "preview";
  if (!event.submitter) return;
  const op = operation === "remove_optional" ? "deactivate" : operation;
  const sensitive = publicationChanges.includes(operation);
  const request = { version: "/manage/v1", scope: {tenant: form.tenant.value, project: form.project.value},
    operation: sensitive && preview ? "preview_" + op : op };
  try {
    for (const name of definitions[operation][1].split(" ").filter(Boolean)) {
      if (refs.includes(name)) {
        request[name === "after_draft" ? "after" : name] = {
          id: form.elements[name + "_id"].value, revision: form.elements[name + "_revision"].valueAsNumber,
        };
      } else {
        const field = form.elements[name];
        request[name] = jsonFields.includes(name) ? JSON.parse(field.value) : numberFields.includes(name)
          ? field.valueAsNumber : field.type === "checkbox" ? field.checked : field.value;
      }
    }
    if (registryChanges.includes(op) && preview) {
      const [registry, change] = op.split(".");
      request.operation = registry + ".preview";
      request[registry === "packs" ? "pack_operation" : "change"] = change;
      delete request.confirmation;
      delete request.idempotency_key;
    }
    if (draftChanges.includes(operation)) request.preview = preview;
    if ((sensitive || registryChanges.includes(op)) && !preview && operation !== "deactivate") {
      request.presentation = form.presentation.value;
      request.intent_digest = form.intent_digest.value;
    }
    const credential = form.credential.value;
    form.credential.value = form.presentation.value = "";
    Array.from(form.elements).forEach(control => control.disabled = true);
    const endpoint = op.startsWith("contracts.") ? "/manage/v1/contracts" :
      op.startsWith("packs.") ? "/manage/v1/packs" : "/manage/v1";
    const response = await fetch(endpoint, {method: "POST", cache: "no-store", credentials: "omit",
      headers: {"Content-Type": "application/json", Authorization: "Bearer " + credential}, body: JSON.stringify(request)});
    result.textContent = JSON.stringify(await response.json(), null, 2);
  } catch {
    result.textContent = "Request unavailable. Check the JSON fields and the management service.";
  } finally {
    Array.from(form.elements).forEach(control => control.disabled = false);
  }
};
