"""Bounded trusted compositions. Quantities and load are game units, not SI."""
from __future__ import annotations

from copy import deepcopy

from agent_world.simulation.registry import ComponentDef, MaterialDef, Registry
from agent_world.simulation.compiler import compile_composition

RULES_VERSION = "grove-matter-1"
PHASES = ("water", "damage", "failure", "spill", "transport")
REGISTRY = Registry(RULES_VERSION, PHASES)
for material in (MaterialDef("wood", "木", 1, 40, True, 20),
                 MaterialDef("stone", "石", 3, 40),
                 MaterialDef("cloth", "布", 1, 16, True, 32),
                 MaterialDef("water", "水", 2, 1, False, 0, True)):
    REGISTRY.register_material(material)
PIECE_QUANTITIES = {key: value.piece_quantity for key, value in REGISTRY.materials.items()}


def _integer(maximum=10000):
    return (int, 0, maximum)


COMPONENT_FIELDS = {
    "container": {"slots": _integer(16), "liquid_capacity": _integer(200), "open": (bool, None, None)},
    "structure": {"max_integrity": _integer(100), "integrity": _integer(100), "support_limit": _integer(1000), "overload_rate": _integer(10)},
    "absorbent": {"capacity": _integer(200), "rate": _integer(12), "supports": (dict, None, None)},
    "buoyant": {"capacity": _integer(1000), "supports": (dict, None, None)},
    "combustion": {"rate": _integer(8), "spread": (bool, None, None)},
    "attachment": {"kind": (str, None, None), "max_children": _integer(16), "fits": (str, None, None)},
    "covering": {"radius": _integer(1)},
    "collector": {"capacity": _integer(40), "mode": (str, None, None), "radius": _integer(1)},
    "heater": {"rate": _integer(6), "radius": _integer(1), "lit": (bool, None, None)},
    "barrier": {"active": (bool, None, None)},
    "resource": {"material": (str, None, None), "capacity": _integer(4), "regrowth_tick": _integer(1000000000)},
    "water_source": {"capacity": _integer(200)},
    "personal": {"protected": (bool, None, None)},
    "ignition": {"enabled": (bool, None, None)},
    "walkway": {"dry": (bool, None, None)},
}
for key, fields in COMPONENT_FIELDS.items():
    REGISTRY.register_component(ComponentDef(key, fields))

def _base(material):
    definition = REGISTRY.materials[material]
    components = {}
    if definition.combustible:
        components["combustion"] = {"rate": 2, "spread": True}
    if definition.absorption_per_piece:
        components["absorbent"] = {"capacity": definition.absorption_per_piece, "rate": 2}
    return {"components": components, "quantity": definition.piece_quantity, "material_cost": {material: 1}}


CONSTRUCTIONS = {}
for key, name, materials, description in (
    ("vessel", "器皿", ("wood", "stone"), "容纳物件和水；载荷超过承载会损伤。"),
    ("bundle", "材料束", ("wood", "stone", "cloth"), "可搬运的裸材料。"),
    ("panel", "遮片", ("wood", "cloth"), "可绑定到器皿形成外部遮盖。"),
    ("float", "浮材", ("wood",), "可绑定到器皿增加逻辑浮力，同时增加自身载荷。")):
    CONSTRUCTIONS[key] = {"name": name, "materials": list(materials), "lining": ["cloth"] if key == "vessel" else [],
                          "sizes": [1, 2], "description": description, "variants": {m: _base(m) for m in materials},
                          "scales": ["quantity", "components.absorbent.capacity", "components.container.slots", "components.container.liquid_capacity",
                                     "components.structure.support_limit", "components.buoyant.capacity"],
                          "state": {"kind": "raw_material" if key == "bundle" else "composed"},
                          "lining_definition": {"cloth": {"quantity": 16, "components": {"absorbent": {"capacity": 32, "rate": 2}}, "material_cost": {"cloth": 1}}}}
for variant in CONSTRUCTIONS["vessel"]["variants"].values():
    variant["components"].update(container={"slots": 4, "liquid_capacity": 12, "open": True},
                                  structure={"integrity": 100, "max_integrity": 100, "support_limit": 48, "overload_rate": 1})
CONSTRUCTIONS["vessel"]["variants"]["wood"]["components"]["buoyant"] = {"capacity": 96}
for variant in CONSTRUCTIONS["panel"]["variants"].values():
    variant["components"].update(attachment={"kind": "cover", "fits": "container", "max_children": 16}, covering={"radius": 1})
for variant in CONSTRUCTIONS["float"]["variants"].values():
    variant["components"].update(attachment={"kind": "buoyancy", "fits": "container", "max_children": 16}, buoyant={"capacity": 120})

CONSTRUCTIONS["sling"] = {
    "name": "运物布兜", "materials": ["cloth"], "lining": [], "sizes": [1],
    "description": "容纳物件的开放布兜；载荷超过承载会损伤，不能储水，自身不提供浮力。",
    "variants": {"cloth": {
        "quantity": 16, "material_cost": {"cloth": 1},
        "components": {
            "container": {"slots": 4, "liquid_capacity": 0, "open": True},
            "structure": {"max_integrity": 100, "integrity": 100, "support_limit": 96, "overload_rate": 1},
            "absorbent": {"capacity": 32, "rate": 2},
            "combustion": {"rate": 2, "spread": True},
        },
    }},
    "scales": ["quantity", "components.absorbent.capacity", "components.container.slots",
               "components.container.liquid_capacity", "components.structure.support_limit"],
    "state": {"kind": "composed"}, "lining_definition": {},
}

CONSTRUCTIONS["side_pouch"] = {
    "name": "附接布袋", "materials": ["cloth"], "lining": [], "sizes": [1],
    "description": "可附接到器皿的开放布袋；自身容纳物件并承受实际载荷，不能储水。",
    "variants": {"cloth": {
        "quantity": 16, "material_cost": {"cloth": 1},
        "components": {
            "container": {"slots": 2, "liquid_capacity": 0, "open": True},
            "structure": {"max_integrity": 100, "integrity": 100, "support_limit": 32, "overload_rate": 1},
            "absorbent": {"capacity": 32, "rate": 2},
            "combustion": {"rate": 2, "spread": True},
            "attachment": {"kind": "cargo", "fits": "container", "max_children": 16},
        },
    }},
    "scales": ["quantity", "components.absorbent.capacity", "components.container.slots",
               "components.container.liquid_capacity", "components.structure.support_limit"],
    "state": {"kind": "composed"}, "lining_definition": {},
}


def composition_definition(spec):
    return compile_composition(REGISTRY, CONSTRUCTIONS, spec)


def composition_catalog():
    return {"quantity_unit": "u", "rules_version": RULES_VERSION,
            "materials": [{"id": m.id, "name": m.name, "load_per_u": m.load_per_u,
                           "piece_quantity": m.piece_quantity, "absorbency": m.absorption_per_piece,
                           "combustible": m.combustible} for m in REGISTRY.materials.values() if m.id != "water"],
            "constructions": [{"id": key, **deepcopy(value)} for key, value in CONSTRUCTIONS.items()]}


from agent_world.simulation.rules import register_material_rules
register_material_rules(REGISTRY)
