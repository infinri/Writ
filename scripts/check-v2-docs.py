#!/usr/bin/env python3
"""Writ V2 docs-only integrity test. No external dependencies or V1 runtime needed.

Checks that the replacement Markdown/HTML site has valid internal links, that V2
contract JSON is parseable, and that no retired V1 documentation hierarchy returns.
It is not a substitute for schema validation, security review or implementation tests.
"""
from __future__ import annotations

import json
import re
import sys
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import unquote, urlsplit

ROOT = Path(__file__).resolve().parent.parent
DOCS = ROOT / "docs"
ROOT_DOCS = ["README.md", "HANDBOOK.md", "CONTRIBUTING.md",
             "SECURITY.md", "GOVERNANCE.md", "CHANGELOG.md"]
RETIRED_DIRS = ["adr", "reference", "pressure-runs", "programs", "archive",
                "marketplace", "monthly-reviews"]
REQUIRED = [
    "docs/README.md",
    "docs/implementation/MASTER-IMPLEMENTATION-PLAN.md",
    "docs/security/invariants.md",
    "docs/security/threat-model.md",
    "docs/architecture/index.html",
    "docs/architecture/execution.html",
    "docs/architecture/policy.html",
    "docs/architecture/knowledge-graph.html",
    "docs/architecture/retrieval-pipeline.html",
    "docs/architecture/data-model.html",
    "docs/architecture/injection-channels.html",
    "docs/architecture/corpus-roundtrip.html",
    "docs/architecture/roadmap.html",
    "docs/architecture/security.html",
    "docs/architecture/extensions.html",
    "docs/architecture/assets/v2.css",
]

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


def check_ref(source: Path, raw: str, errors: list[str]):
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


def main() -> int:
    errors = []
    for path in REQUIRED:
        if not (ROOT / path).is_file():
            errors.append(f"Missing normative document: {path}")
    for name in RETIRED_DIRS:
        if (DOCS / name).exists():
            errors.append(f"Retired V1 directory reintroduced: docs/{name}/")

    markdown = sorted(DOCS.rglob("*.md")) + [ROOT / x for x in ROOT_DOCS]
    html = sorted(DOCS.rglob("*.html"))
    schemas = sorted(DOCS.rglob("*.schema.json"))
    pattern = re.compile(r"!?\[[^\]]*\]\(([^)]+)\)")
    for source in markdown:
        if not source.is_file():
            errors.append(f"Missing root document: {source}")
            continue
        txt = source.read_text(encoding="utf-8")
        for m in pattern.finditer(txt):
            raw = m.group(1).strip()
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
    for source in schemas:
        try:
            document = json.loads(source.read_text(encoding="utf-8"))
        except (ValueError, UnicodeError) as e:
            errors.append(f"Invalid schema JSON {source.relative_to(ROOT)}: {e}")
            continue
        if not isinstance(document, dict) or not {"$schema", "title", "required", "properties"}.issubset(document):
            errors.append(f"Incomplete schema draft: {source.relative_to(ROOT)}")

    if len(markdown) < 20 or len(html) < 11 or len(schemas) != 5:
        errors.append(f"Unexpected document inventory: {len(markdown)} markdown, {len(html)} HTML, {len(schemas)} schemas")
    if errors:
        for e in errors:
            print("ERROR:", e, file=sys.stderr)
        print(f"V2 docs check FAILED: {len(errors)} errors", file=sys.stderr)
        return 1
    print(f"V2 docs check OK: {len(markdown)} Markdown, {len(html)} HTML, "
          f"{len(schemas)} JSON Schema files; relative links resolved")
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
