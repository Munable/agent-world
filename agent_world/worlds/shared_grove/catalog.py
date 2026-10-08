"""Finite creation vocabulary; authored prose never supplies mechanics."""
from __future__ import annotations

from copy import deepcopy

from .definitions import composition_catalog, composition_definition
from agent_world.errors import RuleViolation


_NAMES = {"wood_crate": "木箱", "wood_bucket": "木桶", "stone_bowl": "石碗",
          "cloth_roll": "布卷", "wood_stick": "木枝", "stone": "卵石", "wood_log": "原木"}

_COMPOSITIONS = {
    "wood_crate": {"material": "wood", "construction": "vessel", "size": 2},
    "wood_bucket": {"material": "wood", "construction": "vessel", "size": 1},
    "stone_bowl": {"material": "stone", "construction": "vessel", "size": 1},
    "cloth_roll": {"material": "cloth", "construction": "bundle", "size": 1},
    "wood_stick": {"material": "wood", "construction": "bundle", "size": 1},
    "stone": {"material": "stone", "construction": "bundle", "size": 1},
    "wood_log": {"material": "wood", "construction": "bundle", "size": 2},
}


def _shortcut(name):
    value = composition_definition(_COMPOSITIONS[name])
    value["name"] = _NAMES[name]
    if name == "wood_crate":
        value["state"]["open"] = False
        value["components"]["container"]["open"] = False
    return value


CATALOG = {name: _shortcut(name) for name in _COMPOSITIONS}

CONTENT_TEMPLATES = ("cloth_roll", "wood_stick", "stone")
DRIFT_TEMPLATES = ("wood_crate", "wood_bucket")
RAIN_COVERAGE = ("local_region", "river_pair")
RAIN_TICKS = (4, 8, 12)
RAIN_INTENSITIES = (10, 20, 30)
EVENT_START_DELAYS = (2, 4, 6)
DRIFT_TICKS = (8, 16, 24)


def template(name):
    return _shortcut(name)


def object_definition(spec, ctx=None):
    """Resolve only registered constructions; prose cannot add mechanics."""
    if sum(key in spec for key in ("template", "composition", "definition_ref")) != 1:
        raise RuleViolation("choose exactly one template, composition, or published definition_ref/type_id")
    if "definition_ref" in spec:
        if ctx is None:
            raise RuleViolation("published definitions must be resolved in their own world")
        from .creative import object_definition as authored_definition
        return authored_definition(ctx, spec)
    if "composition" in spec:
        try:
            return composition_definition(spec["composition"])
        except (ValueError, KeyError) as exc:
            raise RuleViolation(str(exc)) from exc
    if spec["template"] not in CATALOG:
        raise RuleViolation("unknown object shortcut")
    return template(spec["template"])


def composition_options():
    registry = composition_catalog()
    options = []
    for construction in registry["constructions"]:
        for material in construction["materials"]:
            for size in construction["sizes"]:
                spec = {"material": material, "construction": construction["id"], "size": size}
                options.append({"composition": spec, **composition_definition(spec)})
                for lining in construction.get("lining", []):
                    lined = {**spec, "lining": lining}
                    options.append({"composition": lined, **composition_definition(lined)})
    return options


def content_catalog():
    """A bounded public description of actual creation authority."""
    return {
        "objects": [{"template": key, **template(key)} for key in CATALOG],
        "composition_registry": composition_catalog(),
        "compositions": composition_options(),
        "contents_templates": list(CATALOG),
        "rain": {"coverage_templates": list(RAIN_COVERAGE), "duration_ticks": list(RAIN_TICKS),
                 "intensities": list(RAIN_INTENSITIES), "start_delay_ticks": list(EVENT_START_DELAYS)},
        "drift": {"container_templates": list(DRIFT_TEMPLATES), "route_templates": ["local_bank"],
                  "duration_ticks": list(DRIFT_TICKS), "start_delay_ticks": list(EVENT_START_DELAYS)},
        "authored_description": "Saved public author text. It grants no ability, resource, ownership, or reward.",
        "resource_cost": "Every body, lining and contained object pays public regional node stock. Each paid piece supplies its material's registered piece_quantity of real u, and spawning consumes that finite allocation exactly once. Initial water transfers actual source u. Active material_units are a separate occupancy budget and are never matter or refundable stock.",
    }


TEXT = {"type": "string", "minLength": 1, "maxLength": 600}
NAME = {"type": "string", "minLength": 1, "maxLength": 80}
COORD = {"type": "integer", "minimum": -1000000, "maximum": 1000000}
COMPOSITION_SCHEMA = {
    "type": "object", "additionalProperties": False,
    "required": ["material", "construction", "size"],
    "properties": {"material": {"enum": sorted({material for item in composition_catalog()["constructions"] for material in item["materials"]})},
                   "construction": {"enum": [item["id"] for item in composition_catalog()["constructions"]]},
                   "size": {"enum": sorted({size for item in composition_catalog()["constructions"] for size in item["sizes"]})},
                   "lining": {"enum": sorted({material for item in composition_catalog()["constructions"] for material in item.get("lining", [])})}},
}
DEFINITION_CHOICE = [{"required": ["template"], "properties": {"composition": False, "definition_ref": False, "type_id": False}},
                     {"required": ["composition"], "properties": {"template": False, "definition_ref": False, "type_id": False}},
                     {"required": ["definition_ref", "type_id"], "properties": {"template": False, "composition": False}}]
PROGRAM_PROPERTIES = {"definition_ref": {"type": "string", "minLength": 1, "maxLength": 128},
                      "type_id": {"type": "string", "minLength": 1, "maxLength": 48}}
CONTENT_ITEM_SCHEMA = {
    "type": "object", "additionalProperties": False, "oneOf": DEFINITION_CHOICE,
    "properties": {**PROGRAM_PROPERTIES, "template": {"enum": list(CATALOG)}, "composition": COMPOSITION_SCHEMA,
                   "name": NAME, "description": TEXT,
                   "water": {"type": "integer", "minimum": 0, "maximum": 12}},
}
OBJECT_PROPERTIES = {
    **PROGRAM_PROPERTIES,
    "template": {"enum": list(CATALOG)}, "composition": COMPOSITION_SCHEMA,
    "name": NAME, "description": TEXT,
    "water": {"type": "integer", "minimum": 0, "maximum": 12},
    "contents": {"type": "array", "maxItems": 8, "items": CONTENT_ITEM_SCHEMA},
}
OBJECT_SCHEMA = {
    "type": "object", "additionalProperties": False, "required": ["x", "y"], "oneOf": DEFINITION_CHOICE,
    "properties": {**OBJECT_PROPERTIES, "x": COORD, "y": COORD},
}
CONTENT_SCHEMA = {
    "oneOf": [
        {"type": "object", "additionalProperties": False, "required": ["kind", "objects"],
         "properties": {"kind": {"const": "layout"},
                        "objects": {"type": "array", "minItems": 1, "maxItems": 4, "items": OBJECT_SCHEMA}}},
        {"type": "object", "additionalProperties": False, "required": ["kind", "rain"],
         "properties": {"kind": {"const": "rain"}, "rain": {
             "type": "object", "additionalProperties": False,
             "required": ["coverage_template", "duration_ticks", "intensity", "start_delay_ticks"],
             "properties": {"coverage_template": {"enum": list(RAIN_COVERAGE)},
                            "duration_ticks": {"enum": list(RAIN_TICKS)},
                            "intensity": {"enum": list(RAIN_INTENSITIES)},
                            "start_delay_ticks": {"enum": list(EVENT_START_DELAYS)},
                            "description": TEXT}}}},
        {"type": "object", "additionalProperties": False, "required": ["kind", "drift"],
         "properties": {"kind": {"const": "drift"}, "drift": {
             "type": "object", "additionalProperties": False,
             "required": ["route_template", "duration_ticks", "start_delay_ticks"], "oneOf": DEFINITION_CHOICE,
             "properties": {**OBJECT_PROPERTIES, "template": {"enum": list(DRIFT_TEMPLATES)},
                            "contents": {"type": "array", "maxItems": 4, "items": CONTENT_ITEM_SCHEMA},
                            "route_template": {"const": "local_bank"},
                            "duration_ticks": {"enum": list(DRIFT_TICKS)},
                            "start_delay_ticks": {"enum": list(EVENT_START_DELAYS)}}}}},
    ],
}


def content_schema():
    """Build public enums from the trusted registry when a world is assembled."""
    schema = deepcopy(CONTENT_SCHEMA)
    constructions = composition_catalog()["constructions"]
    names = [item["id"] for item in constructions]
    materials = sorted({material for item in constructions for material in item["materials"]})
    sizes = sorted({size for item in constructions for size in item["sizes"]})
    linings = sorted({material for item in constructions for material in item.get("lining", [])})

    def refresh(value):
        if isinstance(value, dict):
            properties = value.get("properties", {})
            if "material" in properties and "construction" in properties:
                properties["material"]["enum"] = list(materials)
                properties["construction"]["enum"] = list(names)
                properties["size"]["enum"] = list(sizes)
                properties["lining"]["enum"] = list(linings)
            for child in value.values():
                refresh(child)
        elif isinstance(value, list):
            for child in value:
                refresh(child)

    refresh(schema)
    return schema
