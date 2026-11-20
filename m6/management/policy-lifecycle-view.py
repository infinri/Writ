"""Render one authorized /manage/v1 policy lifecycle page as offline, read-only HTML."""

import argparse
from html import escape
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile


HERE = Path(__file__).resolve().parent
BIN = Path(os.environ.get("WRIT_MANAGEMENT_BIN_DIR", HERE / "target/debug")).resolve()
PROVENANCE = (
    ("Subject type", "kind"), ("Profile", "profile"), ("Scope", "scope"),
    ("Policy", "policy"), ("Implementation", "implementation"),
    ("Enforcement point", "enforcement_point"), ("Positive tests", "positive_tests"),
    ("Negative tests", "negative_tests"), ("Freshness", "freshness"),
    ("Compatibility", "compatibility"), ("Reasons", "reasons"),
)
STYLE = """
:root { color-scheme: light dark; font: 100%/1.5 system-ui, sans-serif }
body { max-width: 68rem; margin: auto; padding: 1rem; overflow-wrap: anywhere }
h1, h2, h3 { line-height: 1.2 }
article, details { border-top: 1px solid GrayText; padding: .8rem 0 }
dl { display: grid; grid-template-columns: minmax(7rem, 11rem) minmax(0, 1fr); gap: .3rem 1rem }
dt { font-weight: 600 } dd, ul { margin: 0 } ul { padding-inline-start: 1.25rem }
pre { white-space: pre-wrap; font-size: .9rem } summary { cursor: pointer }
.label { display: inline-block; border: 1px solid; border-radius: .25rem; padding: .12rem .5rem }
@media (max-width: 32rem) { dl { display: block } dd { margin-bottom: .5rem } }
"""


def value(data):
    if data is None:
        return "None recorded"
    if isinstance(data, bool):
        return "Yes" if data else "No"
    if isinstance(data, list):
        if not data:
            return "None recorded"
        return "<ul>" + "".join(f"<li>{value(item)}</li>" for item in data) + "</ul>"
    if isinstance(data, dict):
        return f"<pre>{escape(json.dumps(data, indent=2, ensure_ascii=False))}</pre>"
    return escape(str(data))


def fields(record, columns):
    return "<dl>" + "".join(
        f"<dt>{title}</dt><dd>{value(record[key])}</dd>" for title, key in columns
    ) + "</dl>"


def view(response, draft_history=None):
    if response.get("ok") is not True:
        # Only a stable refusal code is visible. Never display partial success data.
        return f'<p role="status">Inspection failed. {value(response["error"]["code"])}</p>'
    result = response["result"]
    history = result["history"]
    selected = history["selected"]
    body = ["<h2>Selected publication</h2>"]
    if selected is None:
        body.append("<p>No publication selected.</p>")
    else:
        body.append(fields(selected, (
            ("Publication version", "publication_version"), ("Selection revision", "revision"),
            ("Audit ID", "audit_id"), ("Selection verified", "verified"),
        )))
    body.append("<h2>Enforcement labels</h2>")
    if not result["enforcement_labels"]:
        body.append("<p>No labeled subjects reported.</p>")
    for row in result["enforcement_labels"]:
        body.append(f'<article><h3>{value(row["subject"])}</h3>'
                    f'<strong class="label">{value(row["label"])}</strong>'
                    f'{fields(row, PROVENANCE)}</article>')
    if draft_history is not None:
        body.append('<section id="drafts"><h2>Immutable draft versions</h2>')
        if not draft_history["versions"]:
            body.append("<p>No draft versions on this page.</p>")
        for version in draft_history["versions"]:
            body.append(fields(version, (
                ("Draft ID", "id"), ("Revision", "revision"), ("Content digest", "digest"),
                ("Cloned from", "source"),
            )))
        if draft_history["next"] is not None:
            body.append(f'<p>Continue draft history with <code>--draft-after {value(draft_history["next"])}</code>.</p>')
        body.append("</section>")
    body.append('<section id="publications"><h2>Immutable publication versions on this page</h2>')
    versions = dict.fromkeys((e["publication_version"], e["publication_digest"]) for e in history["events"])
    if not versions:
        body.append("<p>No publication versions on this page.</p>")
    for version, digest in versions:
        body.append(f'<dl><dt>Publication version</dt><dd>{value(version)}</dd>'
                    f'<dt>Publication digest</dt><dd>{value(digest)}</dd></dl>')
    body.append("</section><h2>Publication history</h2>")
    if not history["events"]:
        body.append("<p>No events on this page.</p>")
    for event in history["events"]:
        body.append(f'<details><summary>{value(event["operation"])} · publication '
                    f'{value(event["publication_version"])} · audit {value(event["audit_id"])}</summary>')
        body.append(fields(event, (
            ("Revision", "revision"), ("Publication digest", "publication_digest"),
            ("Intent digest", "intent_digest"), ("Principal", "human"),
            ("Recorded at", "at"),
        )) + "</details>")
    if history["next"] is not None:
        body.append(f'<p>Read the next page with <code id="publication-cursor">--after {value(history["next"])}</code>.</p>')
    return "".join(body)


def document(body):
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline'; base-uri 'none'; form-action 'none'">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Policy lifecycle | Writ</title><style>{STYLE}</style></head>
<body><main><h1>Policy lifecycle</h1><p>Disposable fixture snapshot</p>{body}</main></body></html>"""


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("management_db")
    parser.add_argument("credential_file")
    parser.add_argument("publication_db")
    parser.add_argument("--after", type=int, default=0)
    parser.add_argument("--limit", type=int, default=50)
    parser.add_argument("--draft-id")
    parser.add_argument("--draft-after", type=int, default=0)
    parser.add_argument("--draft-limit", type=int, default=50)
    parser.add_argument("--tenant", default="m6-lab")
    parser.add_argument("--project", default="offline-proof")
    parser.add_argument("--scenario", default="current", choices=(
        "current", "removed", "stale", "mismatched", "unavailable", "revoked",
        "missing-positive", "missing-negative",
    ))
    args = parser.parse_args()
    scope = {"tenant": args.tenant, "project": args.project}
    def inspect(mode, credential, request, *owners, pass_fds=()):
        process = subprocess.run(
            [BIN / "writ-manage-fixture", mode, args.management_db, credential, *owners],
            input=json.dumps({"version": "/manage/v1", "scope": scope, **request}),
            capture_output=True, text=True, timeout=30, pass_fds=pass_fds,
        )
        response = json.loads(process.stdout)
        if response["version"] != "/manage/v1" or (process.returncode == 0) != (response["ok"] is True):
            raise ValueError("inconsistent management response")
        return response

    def read(credential, pass_fds=()):
        response = inspect("v1-policy", credential, {"operation": "policy.lifecycle.inspect",
                           "after": args.after, "limit": args.limit}, args.publication_db,
                           args.scenario, pass_fds=pass_fds)
        draft_history = None
        if response["ok"] and args.draft_id is not None:
            drafts = inspect("v1", credential, {"operation": "history", "id": args.draft_id,
                             "after": args.draft_after, "limit": args.draft_limit}, pass_fds=pass_fds)
            if not drafts["ok"]:
                return view(drafts), False
            draft_history = drafts["result"]
        return view(response, draft_history), response["ok"]

    try:
        if args.draft_id is None:
            body, success = read(args.credential_file)
        else:
            # Pin the fixture credential across both service reads in an unnamed private file.
            with tempfile.TemporaryFile() as pinned:
                try:
                    if not Path(args.credential_file).is_file():
                        raise OSError("unavailable credential")
                    with open(args.credential_file, "rb") as source:
                        pinned.write(source.read(513))  # MAX_CREDENTIAL plus one overflow byte.
                except OSError:
                    pass  # Empty credentials go through the same service authentication refusal.
                pinned.flush()
                body, success = read(f"/proc/self/fd/{pinned.fileno()}", (pinned.fileno(),))
    except (OSError, subprocess.TimeoutExpired, ValueError, KeyError, TypeError):
        body = '<p role="status">Inspection failed. UNAVAILABLE</p>'
        success = False
    print(document(body))
    return 0 if success else 1


if __name__ == "__main__":
    sys.exit(main())
