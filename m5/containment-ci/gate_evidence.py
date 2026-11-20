"""Bounded host/guest validation shared by the inactive M5a-12 draft."""
import hashlib
import json
import re


def unique(fields):
    value = {}
    for key, item in fields:
        if key in value:
            raise ValueError("duplicate field")
        value[key] = item
    return value


def parse(raw):
    if len(raw) > 65536:
        raise ValueError("record too large")
    try:
        return json.loads(raw.decode("ascii"), object_pairs_hook=unique)
    except RecursionError as exc:
        raise ValueError("record too deeply nested") from exc


def check_binding(binding):
    hashes = {"source_sha": 40, "source_archive_sha256": 64, "image_sha256": 64,
              "manifest_sha256": 64, "challenge": 64}
    if type(binding) is not dict or set(binding) != set(hashes) | {"run_id", "attempt"}:
        raise ValueError("invalid attempt binding")
    for key, length in hashes.items():
        if type(binding[key]) is not str or not re.fullmatch("[0-9a-f]{%d}" % length, binding[key]):
            raise ValueError("invalid digest or challenge")
    for key in ("run_id", "attempt"):
        if type(binding[key]) is not int or not 1 <= binding[key] <= 2**63 - 1:
            raise ValueError("invalid attempt identity")


def required(manifest, binding):
    check_binding(binding)
    if hashlib.sha256(manifest).hexdigest() != binding["manifest_sha256"]:
        raise ValueError("manifest digest differs from approval")
    ids = parse(manifest)
    if type(ids) is not list or not ids or any(
        type(item) is not str or not re.fullmatch(r"[A-Z][A-Z0-9-]{1,79}", item) for item in ids
    ) or len(set(ids)) != len(ids):
        raise ValueError("invalid required-result manifest")
    return ids


def validate(raw_bytes, binding, manifest_bytes):
    ids = required(manifest_bytes, binding)
    record = parse(raw_bytes)
    if type(record) is not dict or set(record) != set(binding) | {"results"}:
        raise ValueError("invalid evidence fields")
    observed = {key: record[key] for key in binding}
    check_binding(observed)
    if observed != binding:
        raise ValueError("evidence from different attempt")
    results = record["results"]
    if type(results) is not dict or set(results) != set(ids):
        raise ValueError("incomplete or unexpected results")
    for result in results.values():
        if type(result) is not dict or result != {"outcome": "pass", "exit_code": 0} or type(result["exit_code"]) is not int:
            raise ValueError("required check did not pass")
