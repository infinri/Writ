#!/usr/bin/env python3
"""Validate the Writ V2 documentation tree and draft contract fixtures.

This checks documentation consistency and JSON Schema conformance, NOT any
runtime authorization, model integration, or security guarantee.
Requires: python -m pip install -r requirements-v2-docs.txt
"""
from __future__ import annotations

import json
import re
import sys
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import unquote, urlsplit

try:
    from jsonschema import Draft202012Validator, ValidationError
except ImportError:
    print("Install V2 docs dependencies: python -m pip install -r requirements-v2-docs.txt", file=sys.stderr)
    raise SystemExit(2)

ROOT = Path(__file__).resolve().parent.parent
DOCS = ROOT / "docs"
ROOT_DOCS = ["README.md", "HANDBOOK.md", "CONTRIBUTING.md",
             "SECURITY.md", "GOVERNANCE.md", "CHANGELOG.md"]
RETIRED_DIRS = ["adr", "reference", "pressure-runs", "programs", "archive",
                "marketplace", "monthly-reviews"]
REQUIRED = [
    "docs/README.md", "docs/implementation/MASTER-IMPLEMENTATION-PLAN.md",
    "docs/implementation/M0-START-HERE.md",
    "docs/security/invariants.md", "docs/security/threat-model.md",
    "docs/architecture/architecture.md",
    "docs/architecture/model-agnostic-runtime.md",
    "docs/architecture/capability-packs.md",
    "docs/architecture/agent-orchestration.md",
    "docs/architecture/index.html", "docs/architecture/models.html",
    "docs/architecture/capability-packs.html",
    "docs/architecture/tasks-and-evaluation.html",
    "docs/architecture/execution.html", "docs/architecture/policy.html",
    "docs/architecture/knowledge-graph.html",
    "docs/architecture/retrieval-pipeline.html",
    "docs/architecture/data-model.html", "docs/architecture/injection-channels.html",
    "docs/architecture/corpus-roundtrip.html", "docs/architecture/roadmap.html",
    "docs/architecture/security.html", "docs/architecture/extensions.html",
    "docs/architecture/assets/v2.css",
    "docs/decisions/ADR-0006-model-and-domain-independence.md",
    "docs/packs/README.md",
]

NEW_SCHEMAS = {
    "model-capabilities", "model-request", "model-event",
    "capability-pack", "task-spec", "artifact", "evidence", "evaluation-record"
}
EXISTING_SCHEMAS = {
    "action-request", "decision", "effect-receipt", "audit-event", "policy-manifest"
}
EXPECTED_PACKS = {"software-engineering", "research-investigation", "review-audit"}
EXPECTED_TASKS = {"code-review", "research", "document-review"}


class AssetLinks(HTMLParser):
    def __init__(self):
        super().__init__()
        self.links = []

    def handle_starttag(self, tag, attrs):
        if tag in {"a", "link", "script", "img", "source"}:
            data = dict(attrs)
            candidate = data.get("href") or data.get("src")
            if candidate:
                self.links.append(candidate)


def check_ref(source: Path, raw: str, errors: list[str]) -> None:
    raw = raw.strip().strip("<>")
    if not raw or raw.startswith(("#", "mailto:", "data:", "javascript:")):
        return
    url = urlsplit(raw)
    if url.scheme or url.netloc:
        return
    path_part = unquote(url.path)
    if not path_part:
        return
    target = (source.parent / path_part).resolve()
    try:
        target.relative_to(ROOT)
    except ValueError:
        errors.append(f"{source.relative_to(ROOT)}: escaped repository: {raw}")
        return
    if not target.exists():
        errors.append(f"{source.relative_to(ROOT)}: missing link: {raw}")


def load_json(path: Path, errors: list[str]):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (ValueError, UnicodeError) as exc:
        errors.append(f"Invalid JSON at {path.relative_to(ROOT)}: {exc}")
        return None


def validate_fixture(path: Path, schema: dict, errors: list[str], *, should_pass=True):
    data = load_json(path, errors)
    if data is None:
        return None
    try:
        Draft202012Validator(schema).validate(data)
    except ValidationError as exc:
        if should_pass:
            errors.append(f"Invalid fixture {path.relative_to(ROOT)}: {exc.message}")
    else:
        if not should_pass:
            errors.append(f"Negative fixture was incorrectly accepted: {path.relative_to(ROOT)}")
    return data


def main() -> int:
    errors = []
    for path in REQUIRED:
        if not (ROOT / path).is_file():
            errors.append(f"Missing normative document: {path}")
    for name in RETIRED_DIRS:
        if (DOCS / name).exists():
            errors.append(f"Retired V1 documentation hierarchy reintroduced: docs/{name}/")

    markdown = sorted(DOCS.rglob("*.md")) + [ROOT / p for p in ROOT_DOCS]
    html = sorted(DOCS.rglob("*.html"))
    schemas = sorted((DOCS / "contracts").glob("*.schema.json"))
    pattern = re.compile(r"!?\[[^\]]*\]\(([^)]+)\)")
    for source in markdown:
        if not source.is_file():
            errors.append(f"Missing root document: {source}")
            continue
        for match in pattern.finditer(source.read_text(encoding="utf-8")):
            raw = match.group(1).strip()
            if raw.startswith("<") and ">" in raw:
                raw = raw[1:raw.index(">")]
            elif " " in raw:
                raw = raw.split(" ", 1)[0]
            check_ref(source, raw, errors)

    for source in html:
        parser = AssetLinks()
        parser.feed(source.read_text(encoding="utf-8"))
        for raw in parser.links:
            check_ref(source, raw, errors)

    schema_by_name = {}
    for source in schemas:
        document = load_json(source, errors)
        if not isinstance(document, dict):
            continue
        try:
            Draft202012Validator.check_schema(document)
        except Exception as exc:
            errors.append(f"Invalid JSON Schema {source.relative_to(ROOT)}: {exc}")
            continue
        if not {"$schema", "$id", "title", "required", "properties"}.issubset(document):
            errors.append(f"Incomplete schema: {source.relative_to(ROOT)}")
        schema_by_name[source.name.removesuffix(".schema.json")] = document

    expected_schema_names = NEW_SCHEMAS | EXISTING_SCHEMAS
    if set(schema_by_name) != expected_schema_names:
        errors.append(f"Expected schema names {sorted(expected_schema_names)}, got {sorted(schema_by_name)}")

    packs = sorted((DOCS / "packs" / "examples").glob("*.json"))
    tasks = sorted((DOCS / "contracts" / "fixtures").glob("*.task.json"))
    negative = sorted((DOCS / "contracts" / "fixtures" / "negative").glob("*.invalid.json"))
    if {p.stem for p in packs} != EXPECTED_PACKS:
        errors.append("Missing or unexpected reference packs")
    if {p.name.removesuffix(".task.json") for p in tasks} != EXPECTED_TASKS:
        errors.append("Missing or unexpected cross-domain TaskSpec fixtures")
    if len(negative) < 2:
        errors.append("At least two negative contract fixtures are required")

    pack_by_id = {}
    if "capability-pack" in schema_by_name:
        for path in packs:
            data = validate_fixture(path, schema_by_name["capability-pack"], errors)
            if isinstance(data, dict):
                pack_by_id[data.get("pack_id")] = data
                task_types = set(data.get("task_types", []))
                for wf in data.get("workflows", []):
                    if wf.get("task_type") not in task_types:
                        errors.append(f"{path.name}: workflow type absent from task_types")
                if data.get("signature_ref") is not None:
                    errors.append(f"{path.name}: design fixture must not impersonate signed production pack")

    if "task-spec" in schema_by_name:
        for path in tasks:
            task = validate_fixture(path, schema_by_name["task-spec"], errors)
            if not isinstance(task, dict):
                continue
            pack = pack_by_id.get(task.get("pack_id"))
            if pack is None:
                errors.append(f"{path.name}: no reference pack for pack_id")
            elif task.get("task_type") not in pack.get("task_types", []):
                errors.append(f"{path.name}: task_type not declared by reference pack")
            elif not set(task.get("required_artifacts", [])).issubset(
                {a.get("artifact_type") for a in pack.get("artifacts", [])}
            ):
                errors.append(f"{path.name}: required artifact absent from reference pack")

    # Positive stub-provider capability contracts and deliberately invalid examples.
    if "model-capabilities" in schema_by_name:
        for path in sorted((DOCS / "contracts" / "fixtures").glob("model-*.capabilities.json")):
            validate_fixture(path, schema_by_name["model-capabilities"], errors)

    for path in negative:
        filename = path.name
        if filename.startswith("pack-") and "capability-pack" in schema_by_name:
            validate_fixture(path, schema_by_name["capability-pack"], errors, should_pass=False)
        elif filename.startswith("task-") and "task-spec" in schema_by_name:
            validate_fixture(path, schema_by_name["task-spec"], errors, should_pass=False)
        else:
            errors.append(f"Unrecognized negative fixture kind: {filename}")

    if len(html) < 14 or len(markdown) < 25:
        errors.append(f"Unexpected docs inventory: {len(markdown)} Markdown and {len(html)} HTML")
    if errors:
        for error in errors:
            print("ERROR:", error, file=sys.stderr)
        print(f"V2 docs check FAILED: {len(errors)} errors", file=sys.stderr)
        return 1
    print(f"V2 docs check OK: {len(markdown)} Markdown, {len(html)} HTML, "
          f"{len(schemas)} schemas, {len(packs)} packs, {len(tasks)} tasks, "
          f"{len(negative)} negative fixtures. Link and schema checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
