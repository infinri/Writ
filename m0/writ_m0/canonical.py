"""Canonical JSON encoding and digests for operation and target identity.

Digests bind approvals, permits and receipts to an exact operation, so two
implementations (Rust and Go in M1) must produce byte-identical canonical forms.
To keep that achievable without a shared float formatter, the canonical form
deliberately admits a narrow value domain (decision D-006):

- objects with string keys, sorted by Unicode code point; arrays; strings;
  integers within the IEEE-754 safe range; true, false, null
- no floats (encode quantities as integers or decimal strings)
- no lone surrogates; strings are not Unicode-normalized, since normalization
  can change the meaning of identifiers and paths

Encoding: UTF-8, no insignificant whitespace, minimal JSON string escaping
(control characters, quote and backslash only).
"""
from __future__ import annotations

import hashlib
import json
from typing import Any

MAX_SAFE_INTEGER = 2**53 - 1
DIGEST_PREFIX = "sha256:"
MAX_DEPTH = 64


class CanonicalizationError(ValueError):
    pass


def _check(value: Any, depth: int) -> None:
    if depth > MAX_DEPTH:
        raise CanonicalizationError("nesting exceeds canonical depth limit")
    if value is None or isinstance(value, bool):
        return
    if isinstance(value, int):
        if abs(value) > MAX_SAFE_INTEGER:
            raise CanonicalizationError(f"integer outside safe range: {value}")
        return
    if isinstance(value, float):
        raise CanonicalizationError("floats are not canonicalizable; use integers or strings")
    if isinstance(value, str):
        try:
            value.encode("utf-8")
        except UnicodeEncodeError as exc:
            raise CanonicalizationError("string contains a lone surrogate") from exc
        return
    if isinstance(value, list):
        for item in value:
            _check(item, depth + 1)
        return
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                raise CanonicalizationError(f"non-string object key: {key!r}")
            _check(key, depth + 1)
            _check(item, depth + 1)
        return
    raise CanonicalizationError(f"unsupported type: {type(value).__name__}")


def canonical_bytes(value: Any) -> bytes:
    _check(value, 0)
    text = json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True,
                      allow_nan=False)
    return text.encode("utf-8")


def digest(value: Any) -> str:
    return DIGEST_PREFIX + hashlib.sha256(canonical_bytes(value)).hexdigest()
