"""Output compression (WRIT-ROADMAP 18a): drop the redundant bulk, keep the shape.

MEASURED, not assumed. Across 2,367 PostToolUse envelopes in the blackbox corpus,
Edit and Write are 95% of all tool-response bytes (Edit alone: 1,007 calls, 32.9 MB,
19,689-char median). Bash, the tool everyone expects to be the offender, has a
742-char median and is deliberately NOT a target. Within an Edit response the
concentration is sharper: `originalFile` is 96% of it, the entire pre-edit file
echoed back, while `structuredPatch` carries the actual change in ~348 chars.

THE KEY IS REPLACED, NEVER REMOVED. The hook contract says the replacement must
match the tool's expected response shape, so the failure that would break a user's
edit is a missing key, not a poor ratio.
"""

from __future__ import annotations

# Below this the replacement marker is not obviously cheaper than the content, and
# a hook that rewrites a small payload buys nothing while still being a rewrite.
MIN_COMPRESSIBLE = 4000

# Keyed by tool: the one field that carries the redundant bulk. Held as a map so a
# tool added here cannot silently inherit another tool's field name.
_BULK_FIELD = {
    "Edit": "originalFile",
    "Write": "originalFile",
}


def compress_tool_response(tool_name: str, response: object) -> dict | None:
    """Return a shape-preserving replacement, or None when there is nothing to do.

    None means "emit no updatedToolOutput at all", which leaves the envelope exactly
    as the tool produced it.
    """
    field = _BULK_FIELD.get(tool_name)
    if field is None or not isinstance(response, dict):
        return None

    bulk = response.get(field)
    if not isinstance(bulk, str) or len(bulk) <= MIN_COMPRESSIBLE:
        return None

    out = dict(response)
    # Names the field and the REAL dropped size, so a transcript reader can see that
    # something was elided and how much, rather than receiving a quietly different
    # reality than the tool produced.
    out[field] = (
        f"[writ: {field} elided, {len(bulk)} chars. The file is on disk and the "
        f"change is in structuredPatch.]"
    )
    return out
