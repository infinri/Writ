"""Auto-memory parsing: the one reader behind both memory writers.

Claude Code writes each memory to `~/.claude/projects/<encoded-project>/memory/<name>.md`
with a small YAML frontmatter block and a markdown body. Two Writ writers mirror
those files into the graph -- the PostToolUse hook (writ-memory-capture.sh, one file
at a time) and `writ memory backfill` (every file, every project) -- so the parse and
the payload shape live HERE and both bind to it. Two independent parsers would drift,
and a drifted parser writes silently-wrong graph nodes.

stdlib only, like bin/lib/manual_test_grant.py: this runs from a hook where no
virtualenv is guaranteed, so PyYAML is not available. The frontmatter subset Claude
Code emits (flat `key: value` plus a one-level `metadata:` block) is parsed directly.

Fail-closed at every boundary. A parse that cannot be trusted returns None rather
than a half-filled dict, so the caller SKIPS the mirror instead of writing a node
with a guessed identity. The `MEMORY.md` index is excluded here, once, rather than
in each caller: it is a table of contents, not a memory.
"""

import json
import os
import re
import sys
from datetime import datetime, timezone

# The index file, excluded by exact basename. A note merely CONTAINING "memory" in
# its name (memory_notes.md) is a real memory and must still mirror.
INDEX_BASENAME = 'MEMORY.md'

# [[wikilink]] targets: a bare memory name, no directory, no .md. The alias form
# [[target|label]] keeps only the target.
_LINK_RE = re.compile(r'\[\[([^\]\[|]+)(?:\|[^\]\[]*)?\]\]')

# CLI exit codes. The hook branches on 2 alone: an unreadable file means a memory
# write that already landed on disk was NOT mirrored, which is worth recording,
# while 1 means there was nothing to mirror in the first place.
EXIT_OK = 0
EXIT_NOTHING = 1
EXIT_UNREADABLE = 2
EXIT_USAGE = 3


def _now_iso():
    return datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


def _clean_scalar(raw):
    """Strip surrounding whitespace and one layer of matched quotes."""
    value = (raw or '').strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in ('"', "'"):
        value = value[1:-1]
    return value.strip()


def _split_frontmatter(content):
    """Return (frontmatter_text, body) or None when there is no leading '---' block."""
    text = (content or '').lstrip('\ufeff')
    lines = text.split('\n')
    if not lines or lines[0].strip() != '---':
        return None
    for index in range(1, len(lines)):
        if lines[index].strip() in ('---', '...'):
            return '\n'.join(lines[1:index]), '\n'.join(lines[index + 1:])
    return None


def _parse_frontmatter(text):
    """Parse the flat + one-level-nested subset Claude Code emits.

    Returns (top_level, nested), where nested maps a parent key (`metadata`) to its
    indented children. Anything richer than that (lists, deeper nesting, multi-line
    scalars) is ignored rather than guessed at.
    """
    top = {}
    nested = {}
    parent = None
    for raw in text.split('\n'):
        if not raw.strip() or raw.lstrip().startswith('#'):
            continue
        indented = raw[:1].isspace()
        key, separator, value = raw.strip().partition(':')
        if not separator:
            continue
        key = key.strip()
        value = _clean_scalar(value)
        if indented:
            if parent:
                nested.setdefault(parent, {})[key] = value
            continue
        if value:
            top[key] = value
            parent = None
        else:
            parent = key
    return top, nested


def _extract_links(body):
    """Deduped [[wikilink]] targets in body order."""
    seen = []
    for target in _LINK_RE.findall(body or ''):
        target = target.strip()
        if target and target not in seen:
            seen.append(target)
    return seen


def parse_memory_markdown(content):
    """Parse one memory file's text into its graph fields, or None.

    None means "nothing parseable here" (no frontmatter block at all), which the
    callers treat as a silent skip. `type` comes from the NESTED metadata.type field
    Claude Code writes; a top-level `type` is accepted as a fallback. Missing
    description/type default to '' rather than raising, and an empty body parses to
    '' -- a memory with only frontmatter is still a memory.
    """
    try:
        split = _split_frontmatter(content)
        if split is None:
            return None
        frontmatter_text, body = split
        top, nested = _parse_frontmatter(frontmatter_text)
        metadata = nested.get('metadata', {})
        return {
            'name': top.get('name', ''),
            'description': top.get('description', ''),
            'type': metadata.get('type', top.get('type', '')),
            'body': (body or '').strip(),
            'links': _extract_links(body),
            'origin_session_id': metadata.get('originSessionId', ''),
        }
    except Exception:
        return None


def is_memory_index_file(path):
    """True for the MEMORY.md index only (exact basename), never for a real memory."""
    try:
        return os.path.basename(path or '') == INDEX_BASENAME
    except Exception:
        return False


def derive_project_from_memory_path(path):
    """The project scope of a memory path: the directory CONTAINING its memory dir.

    `.../<encoded-project>/memory/<name>.md` -> `<encoded-project>`. Keyed on the
    `memory` parent segment rather than on a `.claude/projects` prefix so a
    `--projects-root` pointed anywhere (backfill, tests) resolves the same scope the
    hook does. Returns None when the path is not a memory-directory file.
    """
    try:
        parts = [part for part in (path or '').replace('\\', '/').split('/') if part]
        if len(parts) < 3 or parts[-2] != 'memory':
            return None
        return parts[-3]
    except Exception:
        return None


def build_memory_payload(path, content, session_id=''):
    """Assemble the /memory-record upsert payload for one memory file, or None.

    None on: the MEMORY.md index, unparsable content, a frontmatter without a `name`
    (nothing to key the node on), or a path outside a memory directory. Every None
    case is a skip, never an error -- the memory itself already exists on disk.

    The payload keys ARE the create_memory kwargs, so the hook, the route, and the
    CLI cannot disagree about the node's shape. `session_id` is the writing session
    when the caller knows it (the hook), else the frontmatter's originSessionId (the
    backfill, which reconstructs history it did not witness).
    """
    try:
        if is_memory_index_file(path):
            return None
        parsed = parse_memory_markdown(content)
        if not parsed or not parsed.get('name'):
            return None
        project = derive_project_from_memory_path(path)
        if not project:
            return None
        return {
            'name': parsed['name'],
            'project': project,
            'description': parsed['description'],
            'type': parsed['type'],
            'body': parsed['body'],
            'links': parsed['links'],
            'path': path,
            'session_id': session_id or parsed.get('origin_session_id', ''),
            'updated_at': _now_iso(),
            'status': 'live',
        }
    except Exception:
        return None


def _cli():
    """payload <path> [session_id] -- print one memory's upsert payload as JSON.

    Exit 0 with the payload on stdout; 1 when there is nothing to mirror (index
    file, no frontmatter, no name); 2 when the file cannot be read; 3 on usage.
    The hook branches on the status alone, so it needs no inline python.
    """
    argv = sys.argv[1:]
    if len(argv) < 2 or argv[0] != 'payload':
        return EXIT_USAGE

    path = argv[1]
    session_id = argv[2] if len(argv) > 2 else ''
    if is_memory_index_file(path):
        return EXIT_NOTHING

    try:
        with open(path, encoding='utf-8', errors='replace') as handle:
            content = handle.read()
    except OSError:
        return EXIT_UNREADABLE

    payload = build_memory_payload(path, content, session_id=session_id)
    if payload is None:
        return EXIT_NOTHING
    print(json.dumps(payload))
    return EXIT_OK


if __name__ == '__main__':
    sys.exit(_cli())
