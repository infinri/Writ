#!/usr/bin/env python3
"""Generate templates/settings.json from hooks/hooks.json.

WHY THIS IS GENERATED. `hooks/hooks.json` is the single source of truth for Writ's hook
registrations: 12 events over 41 command paths. A hand-maintained second copy in
templates/settings.json is the exact shape of the defect fixed in ea5022f, where the package's
cache-dir default moved and three bash copies kept the old value because nothing compared
them. Writ has already paid this cost once: CHANGELOG.md:215 had to warn "keep registrations
in sync between the two if you edit either". Generating the template plus a sync test means
editing hooks/hooks.json stays the ONLY action needed for a hook change, which is what
docs/install.md already promises.

WHAT THE TEMPLATE IS FOR. Hooks are already global for a normal install: `writ@skills-dir` is
a user-scope plugin, so its hooks fire in every project with no settings.json entry at all.
But Writ at a path that is neither ~/.claude/skills/* nor a marketplace install is discovered
by nothing, so hooks/hooks.json is never read and NO hooks load: no gate, no rule injection,
no enforcement. `patch-global-config.sh --hooks` seeds this template for that case only.

THE ONLY TRANSFORMATION. settings.json uses the same hooks schema as the plugin manifest, so
nothing structural changes. `${CLAUDE_PLUGIN_ROOT}` is set only for plugin-loaded hooks, so it
becomes `${WRIT_DIR}`, which the installer fills via `envsubst '$WRIT_DIR'`. Leaving the
plugin variable would expand to the empty string and point every command at /hooks/...

Usage:
  python3 scripts/render-settings-template.py            # write templates/settings.json
  python3 scripts/render-settings-template.py --stdout   # print, write nothing (CI/tests)
  python3 scripts/render-settings-template.py --check    # exit 1 if the committed file is stale
  python3 scripts/render-settings-template.py --source X # read hooks from X instead
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

SKILL_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_SOURCE = SKILL_ROOT / "hooks" / "hooks.json"
DEFAULT_TARGET = SKILL_ROOT / "templates" / "settings.json"

PLUGIN_VAR = "${CLAUDE_PLUGIN_ROOT}"
INSTALL_VAR = "${WRIT_DIR}"

# Only the hooks block is generated. permissions and statusLine are owned by
# patch-global-config.sh's own merge, so this template has exactly one job and the two steps
# cannot fight over the same keys.
_BANNER = (
    "GENERATED FILE -- do not edit. Source: hooks/hooks.json. "
    "Regenerate: python3 scripts/render-settings-template.py"
)


def render(source: Path) -> str:
    """Return the template text for the given hooks manifest."""
    doc = json.loads(source.read_text())
    hooks = doc.get("hooks")
    if not hooks:
        raise SystemExit(f"{source} has no 'hooks' block")

    rewritten = json.loads(json.dumps(hooks).replace(PLUGIN_VAR, INSTALL_VAR))
    if PLUGIN_VAR in json.dumps(rewritten):
        raise SystemExit("plugin-root variable survived the rewrite")

    out = {"_comment": _BANNER, "hooks": rewritten}
    return json.dumps(out, indent=2) + "\n"


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--source", default=str(DEFAULT_SOURCE))
    ap.add_argument("--target", default=str(DEFAULT_TARGET))
    ap.add_argument("--stdout", action="store_true", help="print instead of writing")
    ap.add_argument("--check", action="store_true",
                    help="exit 1 when the target is stale (writes nothing)")
    args = ap.parse_args(argv)

    text = render(Path(args.source))

    if args.stdout:
        sys.stdout.write(text)
        return 0

    target = Path(args.target)
    if args.check:
        current = target.read_text() if target.is_file() else ""
        if current == text:
            return 0
        print(
            f"{target} is stale. Regenerate: python3 {Path(__file__).name}",
            file=sys.stderr,
        )
        return 1

    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text)
    print(f"wrote {target} ({len(json.loads(text)['hooks'])} hook events)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
