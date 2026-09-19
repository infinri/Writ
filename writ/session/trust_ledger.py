"""Trust ledger (WRIT-ROADMAP 18e): what can act on this machine, and what changed.

RECORDS AND ALARMS. It does not block a tool call and does not judge an extension
malicious. Writ relocates oversight rather than removing it, so the useful output
is an alarm a human acts on.

THE COVERAGE LIMIT IS PART OF THE FEATURE. Measured 2026-09-18 on this machine:
twelve MCP servers were connected while `mcpServers` was EMPTY for all 37 projects
in ~/.claude.json, and no readable file under ~/.claude/ enumerated them. They
arrive through the claude.ai connector layer, not local configuration. A ledger
that reported "0 MCP servers, all trusted" against twelve live ones would be a
false assurance, so the blind spot is stated in the output itself and pinned by a
test rather than left to a reader's inference.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

BASELINE_REL_PATH = "var/trust-ledger.json"

MCP_BLIND_SPOT = (
    "MCP coverage is LOCAL CONFIG ONLY. Servers attached through the claude.ai "
    "connector layer do not appear in any file on disk, so they are outside this "
    "ledger's view and an empty MCP section does NOT mean no servers are connected."
)

_KINDS = ("skill", "agent", "mcp")


def _hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()[:16]


def _hash_file(p: Path) -> str:
    try:
        return _hash(p.read_text(errors="replace"))
    except OSError:
        return "unreadable"


def collect_inventory(
    skills_dir: Path | None,
    agent_dirs: list[Path],
    claude_json: Path | None,
) -> dict[str, dict[str, str]]:
    """Hash every extension Writ can actually read. Missing roots are not errors:
    an absent skills directory is a machine without skills, not a failure."""
    inv: dict[str, dict[str, str]] = {k: {} for k in _KINDS}

    if skills_dir and skills_dir.is_dir():
        for d in sorted(skills_dir.iterdir()):
            if not d.is_dir():
                continue
            # A skill's identity is its instruction file; fall back to the directory
            # name alone so a skill with no SKILL.md is still LISTED rather than
            # silently absent from a ledger whose job is completeness.
            skill_md = d / "SKILL.md"
            inv["skill"][d.name] = _hash_file(skill_md) if skill_md.is_file() else "no-skill-md"

    # Keyed by SOURCE, not by bare name. Agents resolve from more than one directory
    # (the plugin's and ~/.claude/agents/), and on this machine the second is symlinks
    # to the first, so the hashes agree. Replace one symlink with a real file and the
    # two diverge: keying on the name alone would let the last directory scanned win
    # and show ONE entry, hiding that two different definitions answer to one name.
    # That is the precise failure a trust ledger exists to surface.
    for adir in agent_dirs:
        if not adir or not adir.is_dir():
            continue
        src_label = adir.parent.name or adir.name
        for f in sorted(adir.glob("*.md")):
            inv["agent"][f"{src_label}:{f.stem}"] = _hash_file(f)

    if claude_json and claude_json.is_file():
        try:
            data = json.loads(claude_json.read_text(errors="replace"))
        except (OSError, json.JSONDecodeError):
            data = {}
        for proj, cfg in (data.get("projects") or {}).items():
            if not isinstance(cfg, dict):
                continue
            for name in cfg.get("mcpServers") or []:
                inv["mcp"][f"{Path(proj).name}:{name}"] = "declared"

    return inv


def classify(
    inventory: dict[str, dict[str, str]],
    baseline: dict[str, dict[str, str]] | None,
) -> dict[tuple[str, str], str]:
    """Per-entry status. `baseline=None` means no baseline has ever been recorded,
    which is its OWN state: reporting a first run as wholesale drift would train
    the operator to dismiss the alarm that matters."""
    out: dict[tuple[str, str], str] = {}
    if baseline is None:
        for kind in _KINDS:
            for name in inventory.get(kind, {}):
                out[(kind, name)] = "unrecorded"
        return out

    for kind in _KINDS:
        now = inventory.get(kind, {})
        was = baseline.get(kind, {})
        for name, digest in now.items():
            if name not in was:
                out[(kind, name)] = "new"
            elif was[name] != digest:
                out[(kind, name)] = "changed"
            else:
                out[(kind, name)] = "unchanged"
        for name in was:
            if name not in now:
                out[(kind, name)] = "removed"
    return out


def render_ledger(
    inventory: dict[str, dict[str, str]],
    baseline: dict[str, dict[str, str]] | None,
) -> str:
    status = classify(inventory, baseline)
    lines = ["# Extension trust ledger", ""]
    if baseline is None:
        lines += [
            "No baseline recorded yet, so every entry below is `unrecorded` rather than",
            "drift. Run `writ trust-ledger --accept` to record the current state.",
            "",
        ]
    for kind in _KINDS:
        names = sorted(set(inventory.get(kind, {})) | set((baseline or {}).get(kind, {})))
        lines.append(f"## {kind}")
        lines.append("")
        if not names:
            lines.append("(none visible)")
        for name in names:
            s = status.get((kind, name), "unchanged")
            lines.append(f"- {name}: {s.upper() if s != 'unchanged' else s}")
        lines.append("")
    lines += [MCP_BLIND_SPOT, ""]
    return "\n".join(lines)


def load_baseline(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None


def save_baseline(path: Path, inventory: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(inventory, indent=2, sort_keys=True) + "\n")


def default_roots(plugin_root: Path) -> tuple[Path, list[Path], Path]:
    """Resolve the real machine layout. `Path.home()` is called HERE rather than taken
    as a parameter so the user-level agents directory is written the way it actually
    is: the symlink target bootstrap.sh links into, which is still correct. Callers
    that need isolation pass explicit roots to `collect_inventory` instead, and the
    doctor patches `_trust_ledger_roots`, so nothing depends on this for testability.
    """
    return (
        Path.home() / ".claude" / "skills",
        [plugin_root / "agents", Path.home() / ".claude" / "agents"],
        Path.home() / ".claude.json",
    )
