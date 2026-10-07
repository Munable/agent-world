"""Finite creation vocabulary; authored prose never supplies mechanics."""
from __future__ import annotations

from copy import deepcopy


CATALOG = {
    "wood_crate": {
        "name": "木箱", "material": "wood", "material_units": 6, "material_cost": {"wood": 6},
        "capabilities": ["transport", "container", "flammable", "wettable", "floating"],
        "state": {"capacity": 8, "contents": [], "open": False, "floating": False},
    },
    "wood_bucket": {
        "name": "木桶", "material": "wood", "material_units": 5, "material_cost": {"wood": 5},
        "capabilities": ["transport", "container", "flammable", "wettable", "floating"],
        "state": {"capacity": 4, "contents": [], "open": True, "water": 0,
                  "water_capacity": 12, "floating": False},
    },
    "stone_bowl": {
        "name": "石碗", "material": "stone", "material_units": 3, "material_cost": {"stone": 3},
        "capabilities": ["transport", "container", "wettable"],
        "state": {"capacity": 4, "contents": [], "open": True, "water": 0, "water_capacity": 8},
    },
    "cloth_roll": {
        "name": "布卷", "material": "cloth", "material_units": 2, "material_cost": {"cloth": 1},
        "capabilities": ["transport", "flammable", "wettable"], "state": {"kind": "raw_material"},
    },
    "wood_stick": {
        "name": "木枝", "material": "wood", "material_units": 2, "material_cost": {"wood": 1},
        "capabilities": ["transport", "flammable", "wettable", "floating"],
        "state": {"floating": False, "kind": "raw_material"},
    },
    "stone": {
        "name": "卵石", "material": "stone", "material_units": 2, "material_cost": {"stone": 1},
        "capabilities": ["transport", "wettable"], "state": {"kind": "raw_material"},
    },
    "wood_log": {
        "name": "原木", "material": "wood", "material_units": 2, "material_cost": {"wood": 1},
        "capabilities": ["transport", "flammable", "wettable", "floating"],
        "state": {"kind": "raw_material", "floating": False},
    },
}

CONTENT_TEMPLATES = ("cloth_roll", "wood_stick", "stone")
DRIFT_TEMPLATES = ("wood_crate", "wood_bucket")
RAIN_COVERAGE = ("local_region", "river_pair")
RAIN_TICKS = (4, 8, 12)
RAIN_INTENSITIES = (10, 20, 30)
EVENT_START_DELAYS = (2, 4, 6)
DRIFT_TICKS = (8, 16, 24)


def template(name):
    return deepcopy(CATALOG[name])


def content_catalog():
    """A bounded public description of actual creation authority."""
    return {
        "objects": [{"template": key, **deepcopy(value)} for key, value in CATALOG.items()],
        "contents_templates": list(CONTENT_TEMPLATES),
        "rain": {"coverage_templates": list(RAIN_COVERAGE), "duration_ticks": list(RAIN_TICKS),
                 "intensities": list(RAIN_INTENSITIES), "start_delay_ticks": list(EVENT_START_DELAYS)},
        "drift": {"container_templates": list(DRIFT_TEMPLATES), "route_templates": ["local_bank"],
                  "duration_ticks": list(DRIFT_TICKS), "start_delay_ticks": list(EVENT_START_DELAYS)},
        "authored_description": "Saved public author text. It grants no ability, resource, ownership, or reward.",
        "resource_cost": "Every object and contained item spends current public resource-node stock in its claimed chunk. Initial water spends the same chunk's finite public water sources. Material costs count harvested pieces, separately from active-entity budget units.",
    }


TEXT = {"type": "string", "minLength": 1, "maxLength": 600}
NAME = {"type": "string", "minLength": 1, "maxLength": 80}
COORD = {"type": "integer", "minimum": -1000000, "maximum": 1000000}
CONTENT_ITEM_SCHEMA = {
    "type": "object", "additionalProperties": False, "required": ["template"],
    "properties": {"template": {"enum": list(CONTENT_TEMPLATES)}, "name": NAME, "description": TEXT},
}
OBJECT_PROPERTIES = {
    "template": {"enum": list(CATALOG)}, "name": NAME, "description": TEXT,
    "water": {"type": "integer", "minimum": 0, "maximum": 12},
    "contents": {"type": "array", "maxItems": 8, "items": CONTENT_ITEM_SCHEMA},
}
OBJECT_SCHEMA = {
    "type": "object", "additionalProperties": False, "required": ["template", "x", "y"],
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
             "required": ["template", "route_template", "duration_ticks", "start_delay_ticks"],
             "properties": {**OBJECT_PROPERTIES, "template": {"enum": list(DRIFT_TEMPLATES)},
                            "contents": {"type": "array", "maxItems": 4, "items": CONTENT_ITEM_SCHEMA},
                            "route_template": {"const": "local_bank"},
                            "duration_ticks": {"enum": list(DRIFT_TICKS)},
                            "start_delay_ticks": {"enum": list(EVENT_START_DELAYS)}}}}},
    ],
}
