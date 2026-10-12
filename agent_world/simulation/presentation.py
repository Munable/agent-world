"""Finite field-to-display bindings; no expressions, world reads or writes.

Authored displays describe authored fields. They cannot grant a capability,
change an object's footprint, or replace trusted physics and motion overlays.
"""
from __future__ import annotations

from copy import deepcopy
import re
import unicodedata

SHAPES = ("box", "bowl", "cloth", "beam", "stone", "raft", "part")
POSES = ("normal", "folded")
TARGETS = ("color", "glyph", "pose", "badge")
MAX_CASES = 8
MAX_BADGE_LABEL = 12
_COLOR = re.compile(r"#[0-9a-fA-F]{6}\Z")


def _fail(path, message):
    raise ValueError(f"{path}: {message}")


def _object(value, path, allowed, required=()):
    if not isinstance(value, dict) or any(not isinstance(key, str) for key in value):
        _fail(path, "must be an object with string keys")
    extra, missing = set(value) - set(allowed), set(required) - set(value)
    if extra:
        _fail(path, "unknown keys: " + ", ".join(sorted(extra)))
    if missing:
        _fail(path, "missing keys: " + ", ".join(sorted(missing)))
    return value


def _text(value, path, maximum):
    if (not isinstance(value, str) or not value.strip() or len(value) > maximum
            or any(unicodedata.category(char).startswith("C") for char in value)):
        _fail(path, f"must be printable text of 1..{maximum} characters")
    return value


def _display(value, target, path):
    if target == "color":
        if not isinstance(value, str) or not _COLOR.fullmatch(value):
            _fail(path, "must be a #RRGGBB color")
        return value.lower()
    if target == "glyph":
        return _text(value, path, 3)
    if target in {"shape", "pose"}:
        choices = SHAPES if target == "shape" else POSES
        if not isinstance(value, str) or value not in choices:
            _fail(path, "must be " + "|".join(choices))
        return value
    _fail(path, "unsupported display target")


def _field_value(value, field):
    if field["type"] == "boolean":
        return type(value) is bool
    return type(value) is int and field["min"] <= value <= field["max"]


def compile_presentation(value, fields, path="presentation"):
    """Validate only an explicitly supplied display, preserving old hashes."""
    _object(value, path, {"shape", "color", "glyph", "bindings"})
    result = {key: _display(item, key, path + "." + key)
              for key, item in value.items() if key != "bindings"}
    if "bindings" not in value:
        return result
    bindings = _object(value["bindings"], path + ".bindings", TARGETS)
    result["bindings"] = {}
    for target, binding in bindings.items():
        at = path + ".bindings." + target
        allowed = {"field", "label"} if target == "badge" else {"field", "cases"}
        required = {"field"} if target == "badge" else {"field", "cases"}
        _object(binding, at, allowed, required)
        name = binding["field"]
        if not isinstance(name, str) or name not in fields:
            _fail(at + ".field", "must name this type's declared integer or boolean field")
        field = fields[name]
        current = {"field": name}
        if target == "badge":
            if "label" in binding:
                current["label"] = _text(binding["label"], at + ".label", MAX_BADGE_LABEL)
        else:
            cases = binding["cases"]
            if not isinstance(cases, list) or not 1 <= len(cases) <= MAX_CASES:
                _fail(at + ".cases", f"requires 1..{MAX_CASES} exact value mappings")
            current["cases"], seen = [], set()
            for index, case in enumerate(cases):
                cp = at + f".cases[{index}]"
                _object(case, cp, {"value", "display"}, {"value", "display"})
                actual = case["value"]
                if not _field_value(actual, field):
                    _fail(cp + ".value", "must match the declared field's type and range; boolean is not integer")
                if actual in seen:
                    _fail(cp + ".value", "duplicate exact value mapping")
                seen.add(actual)
                current["cases"].append({"value": actual,
                    "display": _display(case["display"], target, cp + ".display")})
        result["bindings"][target] = current
    return result


def resolve_presentation(spec, fields, actual, *, size=1, visible=True, detailed=False, tick=None):
    """Project one current authorized snapshot; never retain a prior display.

    The caller supplies only this object's already authorized field values. Hidden
    or invalid state is unknown, never a default field value or a previous result.
    """
    result = {"size": 2 if size == 2 else 1}
    if spec is None:
        return result
    result.update({key: deepcopy(value) for key, value in spec.items() if key != "bindings"})
    observations, statuses = [], []
    for target, binding in spec.get("bindings", {}).items():
        name = binding["field"]
        field = fields[name]
        value = actual.get(name) if isinstance(actual, dict) else None
        known = visible and _field_value(value, field)
        row = {"target": target, "field": name, "known": known}
        if known:
            row["value"] = value
        else:
            row["reason"] = "hidden" if not visible else "unavailable_field"
        if target == "badge":
            result["badge"] = {"label": binding.get("label", ""), "value": value if known else None,
                               "known": known}
            row["matched"] = known
        else:
            if target == "pose":
                result["pose"] = "normal"
            case = next((case for case in binding["cases"]
                         if known and type(case["value"]) is type(value) and case["value"] == value), None)
            row["matched"] = case is not None
            if case is not None:
                result[target] = case["display"]
        row["status"] = "unavailable" if not known else "resolved" if row["matched"] else "unmapped"
        observations.append(row)
        statuses.append(row["status"])
    if statuses:
        result["binding_status"] = ("unavailable" if "unavailable" in statuses else
                                    "unmapped" if "unmapped" in statuses else "resolved")
        if detailed:
            result["observation"] = {"tick": tick, "bindings": observations,
                "meaning": "Current authored field displays; no physical capability or execution claim."}
    return result


def presentation_catalog():
    """One small authoring vocabulary shared by validation and discovery."""
    return {"static": {"shape": list(SHAPES), "color": "#RRGGBB", "glyph": "printable text, 1..3 characters"},
        "bindings": {"color|glyph|pose": "{field:declared_field,cases:[{value:exact_integer_or_boolean,display:allowed_value}]}",
                     "badge": "{field:declared_field,label?:printable_text_up_to12_characters}",
                     "pose": list(POSES), "targets": list(TARGETS), "cases_per_binding": MAX_CASES},
        "source": "Only this type's declared integer/boolean fields. Existing rules read real physical/port conditions and change those fields. No direct object, physical, event, expression, script or asset source.",
        "meaning": "The server resolves actual current fields for the map and inspection. An unmapped value retains static base appearance and is marked unmapped; hidden/missing fields are unknown, not false/zero. A badge shows the actual scalar or ?. Pose affects drawing only; size, location, collision, material, fire, water and mechanical motion remain world facts. Field names never imply a visual or physical mechanism.",
        "compatibility": "Optional type.presentation; omit it to keep prior canonical definitions and default appearance. Old immutable definitions and instances are never retrofitted."}
