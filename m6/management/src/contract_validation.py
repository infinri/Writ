"""Preflight a closed schema profile and hand all example assertions to jsonschema."""

import json
import re
import resource
import sys

resource.setrlimit(resource.RLIMIT_CPU, (3, 3))
resource.setrlimit(resource.RLIMIT_AS, (256 * 1024 * 1024,) * 2)

from importlib.metadata import version

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError
from referencing import Registry

DIALECT = "https://json-schema.org/draft/2020-12/schema"
KEYWORDS = {
    "title", "description", "type", "properties", "required", "additionalProperties",
    "items", "minLength", "maxLength", "minimum", "maximum", "minItems", "maxItems",
    "enum", "const",
}
ANNOTATIONS = {"title", "description"}
NAME = re.compile(r"[A-Za-z][A-Za-z0-9_-]{0,63}")
MAX_DEPTH = 32
MAX_VALUES = 2048
MAX_DEFINITIONS = 32
MAX_EXPANDED_SCHEMAS = 256
MAX_NORMALIZED = 40 * 1024
MAX_DIAGNOSTICS = 32


class Refusal(Exception):
    def __init__(self, code, path):
        self.code = code
        self.path = path


def pointer(path):
    return "".join("/" + str(part).replace("~", "~0").replace("/", "~1") for part in path)


def check_size(document):
    pending = [(document, ())]
    count = 0
    while pending:
        value, path = pending.pop()
        count += 1
        if count > MAX_VALUES or len(path) > MAX_DEPTH:
            raise Refusal("DOCUMENT_LIMIT", path)
        if isinstance(value, dict):
            pending.extend((child, (*path, key)) for key, child in reversed(value.items()))
        if isinstance(value, list):
            pending.extend((child, (*path, i)) for i, child in reversed(list(enumerate(value))))


def normalize(schema):
    if not isinstance(schema, dict):
        raise Refusal("INVALID_SCHEMA", ())
    if schema.get("$schema") != DIALECT:
        raise Refusal("UNSUPPORTED_DIALECT", ("$schema",))
    definitions = schema.get("$defs", {})
    if not isinstance(definitions, dict):
        raise Refusal("INVALID_SCHEMA", ("$defs",))
    if len(definitions) > MAX_DEFINITIONS:
        raise Refusal("SCHEMA_LIMIT", ("$defs",))

    expanded = 0

    def visit(node, path, stack, depth):
        nonlocal expanded
        expanded += 1
        if expanded > MAX_EXPANDED_SCHEMAS or depth > MAX_DEPTH:
            raise Refusal("SCHEMA_LIMIT", path)
        if not isinstance(node, dict):
            raise Refusal("INVALID_SCHEMA", path)
        if "$ref" in node:
            if len(node) != 1:
                raise Refusal("UNSUPPORTED_KEYWORD", (*path, "$ref"))
            ref = node["$ref"]
            prefix = "#/$defs/"
            if not isinstance(ref, str) or not ref.startswith(prefix):
                raise Refusal("UNAPPROVED_REFERENCE", (*path, "$ref"))
            name = ref[len(prefix):]
            if not NAME.fullmatch(name) or name not in definitions:
                raise Refusal("UNAPPROVED_REFERENCE", (*path, "$ref"))
            if name in stack:
                raise Refusal("REFERENCE_CYCLE", (*path, "$ref"))
            return visit(definitions[name], ("$defs", name), (*stack, name), depth + 1)

        result = {}
        for keyword, value in node.items():
            key_path = (*path, keyword)
            if keyword not in KEYWORDS:
                raise Refusal("UNSUPPORTED_KEYWORD", key_path)
            if keyword == "properties":
                if not isinstance(value, dict):
                    raise Refusal("INVALID_SCHEMA", key_path)
                value = {
                    name: visit(child, (*key_path, name), stack, depth + 1)
                    for name, child in value.items()
                }
            elif keyword == "items":
                value = visit(value, key_path, stack, depth + 1)
            elif keyword == "additionalProperties" and not isinstance(value, bool):
                raise Refusal("UNSUPPORTED_KEYWORD", key_path)
            if keyword not in ANNOTATIONS:
                result[keyword] = value
        return result

    for name, definition in definitions.items():
        if not NAME.fullmatch(name):
            raise Refusal("UNAPPROVED_REFERENCE", ("$defs", name))
        visit(definition, ("$defs", name), (name,), 0)

    root = {key: value for key, value in schema.items() if key not in {"$schema", "$defs"}}
    normalized = visit(root, (), (), 0)
    try:
        Draft202012Validator.check_schema(schema)
    except SchemaError as error:
        raise Refusal("INVALID_SCHEMA", tuple(error.absolute_path)) from None
    try:
        check_size(normalized)
    except Refusal as error:
        raise Refusal("SCHEMA_LIMIT", error.path) from None
    if len(encode(normalized)) > MAX_NORMALIZED:
        raise Refusal("SCHEMA_LIMIT", ())
    return normalized


def example_diagnostics(schema, document):
    check_size(document)
    diagnostics = set()
    # The normalized custom schemas contain no references. Core schemas are
    # source-owned and the registry has no retrieval callback.
    for error in Draft202012Validator(schema, registry=Registry()).iter_errors(document):
        path = tuple(error.absolute_path)
        if error.validator == "required":
            paths = [(*path, name) for name in error.validator_value if name not in error.instance]
        elif error.validator == "additionalProperties":
            paths = [
                (*path, name) for name in error.instance
                if name not in error.schema.get("properties", {})
                and not any(re.search(pattern, name) for pattern in error.schema.get("patternProperties", {}))
            ]
        else:
            paths = [path]
        kind = str(error.validator)
        code = re.sub(r"([a-z])([A-Z])", r"\1_\2", kind).upper()
        diagnostics.update((pointer(item), "EXAMPLE_" + code) for item in paths)
        if len(diagnostics) > MAX_DIAGNOSTICS:
            return [{"code": "DIAGNOSTIC_LIMIT", "pointer": ""}]
    return [{"code": code, "pointer": path} for path, code in sorted(diagnostics)]


def encode(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode()


def evaluate(body):
    result = {"diagnostics": [], "normalized": None}
    try:
        schema = body["schema"]
        if not body["core"]:
            check_size(schema)
            schema = normalize(schema)
        if "example" in body:
            result["diagnostics"] = example_diagnostics(schema, body["example"])
        else:
            result["normalized"] = schema
    except Refusal as error:
        result["diagnostics"] = [{"code": error.code, "pointer": pointer(error.path)}]
    return result


if __name__ == "__main__":
    if version("jsonschema") != "4.23.0":
        sys.exit(1)
    raw = sys.stdin.buffer.read(40 * 1024 + 1)
    if len(raw) > 40 * 1024:
        sys.exit(1)
    sys.stdout.buffer.write(encode(evaluate(json.loads(raw))))
