from __future__ import annotations

import copy
import json
import math
from typing import Any

from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError, SchemaError
from .runtime_errors import InvalidArguments, RegistryConflict, SchemaRejected

PROTOCOL_VERSION = "0.11"
SDK_API_VERSION = 1
CORE_TOOL_NAMES = frozenset(
    {
        "world.bootstrap",
        "world.list_views",
        "world.view_snapshot",
        "world.view_sync",
        "world.discover",
        "world.get_receipt",
        "world.start_activity",
        "world.claim_activity",
        "world.renew_claim",
        "world.finish_activity",
        "world.get_changes",
        "world.wait_changes",
    }
)
MAX_ARGUMENT_BYTES = 65536
MAX_STATE_BYTES = 262144
MAX_RESULT_BYTES = 262144
MAX_EVENTS = 128


def identifier(value: Any, label: str, maximum: int = 128) -> str:
    if (
        not isinstance(value, str)
        or not value.strip()
        or len(value) > maximum
        or any(ord(c) < 32 for c in value)
    ):
        raise InvalidArguments(f"{label} must be a nonempty string of at most {maximum} characters")
    return value


def integer(value: Any, label: str, minimum: int = 0, maximum: int = 2**63 - 1) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        raise InvalidArguments(f"{label} must be an integer in [{minimum}, {maximum}]")
    return value


def duration(value: Any, label: str, maximum: float = 86400, *, zero: bool = False) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or value > maximum
        or (value < 0 if zero else value <= 0)
    ):
        raise InvalidArguments(
            f"{label} must be a finite {'nonnegative' if zero else 'positive'} number <= {maximum}"
        )
    return float(value)


def json_text(value: Any, *, maximum: int = MAX_RESULT_BYTES) -> str:
    def check(item: Any, depth: int = 0) -> None:
        if depth > 64:
            raise InvalidArguments("JSON nesting exceeds 64 levels")
        if item is None or type(item) in (str, bool, int):
            return
        if type(item) is float and math.isfinite(item):
            return
        if isinstance(item, list):
            for child in item:
                check(child, depth + 1)
            return
        if isinstance(item, dict) and all(isinstance(key, str) for key in item):
            for child in item.values():
                check(child, depth + 1)
            return
        raise InvalidArguments("value is not finite, JSON-compatible data")

    check(value)
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    if len(encoded.encode("utf-8")) > maximum:
        raise InvalidArguments(f"JSON payload exceeds {maximum} bytes")
    return encoded


def arguments_object(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise InvalidArguments("arguments must be a JSON object")
    json_text(value, maximum=MAX_ARGUMENT_BYTES)
    return value


def check_schema(schema: dict[str, Any], *, object_root: bool = True) -> None:
    if not isinstance(schema, dict) or (object_root and schema.get("type") != "object"):
        raise RegistryConflict("function schemas must have an object root")
    json_text(schema)

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                if key in {"$ref", "$dynamicRef"} and not (
                    isinstance(value, str) and (value == "#" or value.startswith("#/"))
                ):
                    raise RegistryConflict("only local JSON Pointer schema references are supported")
                if key in {"$anchor", "$dynamicAnchor"}:
                    raise RegistryConflict("schema anchors are not supported; use local $defs pointers")
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)

    walk(schema)
    try:
        Draft202012Validator.check_schema(schema)
    except SchemaError as exc:
        raise RegistryConflict("invalid JSON Schema") from exc


def validate(
    schema: dict[str, Any], value: Any, *, maximum: int = MAX_ARGUMENT_BYTES, object_root: bool = True
) -> None:
    if object_root and not isinstance(value, dict):
        raise SchemaRejected("expected a JSON object")
    json_text(value, maximum=maximum)
    try:
        Draft202012Validator(schema).validate(value)
    except ValidationError as exc:
        path = "/".join(str(part) for part in exc.absolute_path) or "<root>"
        # Never echo rejected values: arguments may contain secrets.
        raise SchemaRejected(f"schema violation at {path}: {exc.validator}") from exc
    except Exception as exc:
        raise SchemaRejected("schema reference could not be resolved") from exc


def embedded_arguments_schema(schema: dict[str, Any], property_name: str = "arguments") -> dict[str, Any]:
    schema = copy.deepcopy(schema)

    def rewrite(node: Any) -> None:
        if isinstance(node, dict):
            node.pop("$id", None)
            for key, value in list(node.items()):
                if key in {"$ref", "$dynamicRef"} and isinstance(value, str) and value.startswith("#"):
                    node[key] = "#/properties/" + property_name + value[1:]
                else:
                    rewrite(value)
        elif isinstance(node, list):
            for value in node:
                rewrite(value)

    rewrite(schema)
    return schema
