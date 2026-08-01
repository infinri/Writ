#!/usr/bin/env python3
"""Render the generated reference pages from their sources.

Four pages under docs/reference/ are GENERATED, never hand-edited:

  cli.md       <- the Typer app (writ.cli) + the writ-session.py dispatch tables
  http-api.md  <- the FastAPI route table (writ.server.app)
  hooks.md     <- hooks/hooks.json
  rulebook.md  <- writ-corpus.cypher

Run `make docs` after changing any source; `make docs-check` diffs without
writing. Deliberately NOT invoked from pytest: documentation is not a test
surface. Output is deterministic (no timestamps) so diffs stay clean.

Requires the venv interpreter (imports writ.cli and writ.server).
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
OUT = REPO / "docs" / "reference"

BANNER = (
    "<!-- GENERATED FILE - do not edit. Source: {source}. "
    "Regenerate with `make docs` (scripts/render-docs.py). -->\n\n"
)


def _first_doc_line(obj) -> str:
    doc = (getattr(obj, "__doc__", None) or "").strip()
    return doc.splitlines()[0].rstrip(".") if doc else ""


def render_cli() -> str:
    from writ.cli import app
    from writ.session.cli_dispatch import _COMPLEX_COMMANDS, _SIMPLE_COMMANDS

    lines = [BANNER.format(source="writ/cli.py + writ/session/cli_dispatch.py")]
    lines.append("# CLI reference\n")
    lines.append(
        "Every `writ` command, generated from the Typer app. "
        "Run `writ <command> --help` for flags and arguments.\n"
    )
    lines.append("| Command | Description |")
    lines.append("|---|---|")

    def command_rows(prefix: str, typer_app) -> list[tuple[str, str]]:
        rows = []
        for cmd in typer_app.registered_commands:
            name = cmd.name or cmd.callback.__name__.replace("_", "-")
            rows.append((f"{prefix}{name}", _first_doc_line(cmd.callback)))
        for group in typer_app.registered_groups:
            rows.extend(command_rows(f"{prefix}{group.name} ", group.typer_instance))
        return rows

    for name, desc in sorted(command_rows("writ ", app)):
        lines.append(f"| `{name}` | {desc} |")

    lines.append("")
    lines.append("## Session CLI (`bin/lib/writ-session.py`)\n")
    lines.append(
        "The hook-facing dispatcher; hooks call it when the daemon is "
        "unreachable. Simple commands take exactly `<session_id>`; complex "
        "commands parse their own flags.\n"
    )
    lines.append("| Kind | Subcommands |")
    lines.append("|---|---|")
    lines.append(
        "| simple | " + ", ".join(f"`{c}`" for c in sorted(_SIMPLE_COMMANDS)) + " |"
    )
    lines.append(
        "| complex | " + ", ".join(f"`{c}`" for c in sorted(_COMPLEX_COMMANDS)) + " |"
    )
    lines.append("")
    return "\n".join(lines)


def render_http_api() -> str:
    from fastapi.routing import APIRoute

    from writ.server import app

    rows = []
    for route in app.routes:
        if not isinstance(route, APIRoute):
            continue
        methods = ",".join(sorted(m for m in route.methods if m != "HEAD"))
        module = route.endpoint.__module__.rsplit(".", 1)[-1]
        rows.append((module, route.path, methods, _first_doc_line(route.endpoint)))

    lines = [BANNER.format(source="writ/server routes")]
    lines.append("# HTTP API reference\n")
    lines.append(
        f"All {len(rows)} endpoints on `http://localhost:8765`, generated from "
        "the FastAPI route table. JSON bodies; no auth (binds localhost only). "
        "Logical failures return HTTP 200 with an `error` key; 422 is request "
        "validation.\n"
    )
    for module in sorted({r[0] for r in rows}):
        lines.append(f"## {module}\n")
        lines.append("| Method | Path | Purpose |")
        lines.append("|---|---|---|")
        for mod, path, methods, doc in sorted(rows):
            if mod == module:
                lines.append(f"| {methods} | `{path}` | {doc} |")
        lines.append("")
    return "\n".join(lines)


def render_hooks() -> str:
    data = json.loads((REPO / "hooks" / "hooks.json").read_text())["hooks"]

    lines = [BANNER.format(source="hooks/hooks.json")]
    lines.append("# Hook registration matrix\n")
    scripts = set()
    total = 0
    body = []
    for event, entries in data.items():
        body.append(f"## {event}\n")
        body.append("| Matcher | Script |")
        body.append("|---|---|")
        for entry in entries:
            matcher = entry.get("matcher", "") or "(all)"
            for hook in entry["hooks"]:
                script = hook["command"].split("/")[-1]
                scripts.add(script)
                total += 1
                body.append(f"| `{matcher}` | `{script}` |")
        body.append("")
    lines.append(
        f"{total} registrations across {len(data)} events wiring "
        f"{len(scripts)} scripts under `hooks/scripts/`, generated from "
        "`hooks/hooks.json` (the single source; `templates/settings.json` is "
        "rendered from the same file). `writ-statusline.sh` is wired through "
        "the settings `statusLine` channel, not a hook event. Behavior and "
        "blocking semantics: `HANDBOOK.md` section 14.\n"
    )
    lines.extend(body)
    return "\n".join(lines)


_RULE_LINE = re.compile(r"^CREATE \(:Rule\b")


def _prop(line: str, key: str) -> str:
    m = re.search(rf"\b{key}: '((?:[^'\\]|\\.)*)'", line)
    return m.group(1) if m else ""


def render_rulebook() -> str:
    rules = []
    for line in (REPO / "writ-corpus.cypher").read_text().splitlines():
        if not _RULE_LINE.match(line):
            continue
        rules.append(
            {
                "id": _prop(line, "rule_id"),
                "domain": _prop(line, "domain").lower() or "(none)",
                "severity": _prop(line, "severity"),
                "mandatory": "mandatory: true" in line,
                "always_on": "always_on: true" in line,
            }
        )

    by_domain: dict[str, list[dict]] = {}
    for r in rules:
        by_domain.setdefault(r["domain"], []).append(r)

    lines = [BANNER.format(source="writ-corpus.cypher")]
    lines.append("# Rulebook inventory\n")
    lines.append(
        f"{len(rules)} rules in the shipped corpus dump, "
        f"{sum(r['mandatory'] for r in rules)} mandatory, "
        f"{sum(r['always_on'] for r in rules)} always-on. Generated from "
        "`writ-corpus.cypher`; the live graph may differ if rules were "
        "authored since the last `writ export-cypher`. Full rule text: "
        "`writ query`, `GET /rule/{id}`, or `writ export`.\n"
    )
    lines.append("| Domain | Rules | Mandatory |")
    lines.append("|---|---:|---:|")
    for domain in sorted(by_domain):
        rs = by_domain[domain]
        lines.append(f"| {domain} | {len(rs)} | {sum(r['mandatory'] for r in rs)} |")
    lines.append("")
    for domain in sorted(by_domain):
        lines.append(f"## {domain}\n")
        lines.append("| Rule | Severity | Flags |")
        lines.append("|---|---|---|")
        for r in sorted(by_domain[domain], key=lambda r: r["id"]):
            flags = ", ".join(
                f
                for f in (
                    "mandatory" if r["mandatory"] else "",
                    "always-on" if r["always_on"] else "",
                )
                if f
            )
            lines.append(f"| `{r['id']}` | {r['severity']} | {flags} |")
        lines.append("")
    return "\n".join(lines)


PAGES = {
    "cli.md": render_cli,
    "http-api.md": render_http_api,
    "hooks.md": render_hooks,
    "rulebook.md": render_rulebook,
}


def main() -> int:
    check = "--check" in sys.argv
    drifted = []
    for name, render in PAGES.items():
        content = render()
        path = OUT / name
        current = path.read_text() if path.exists() else None
        if check:
            if current != content:
                drifted.append(name)
        elif current != content:
            path.write_text(content)
            print(f"wrote {path.relative_to(REPO)}")
        else:
            print(f"unchanged {path.relative_to(REPO)}")
    if check and drifted:
        print("stale (run `make docs`): " + ", ".join(drifted), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
