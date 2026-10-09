"""Bounded trusted compositions. Quantities and load are game units, not SI."""
from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
import re

from agent_world.errors import RuleViolation
from agent_world.simulation.registry import ComponentDef, MaterialDef, Registry
from agent_world.simulation.compiler import compile_composition

RULES_VERSION = "grove-matter-1"
COMBUSTION_CADENCE_TICKS = 3
PHASES = ("water", "damage", "failure", "spill", "transport")
REGISTRY = Registry(RULES_VERSION, PHASES)
for material in (MaterialDef("wood", "木", 1, 40, True, 20),
                 MaterialDef("stone", "石", 3, 40),
                 MaterialDef("cloth", "布", 1, 16, True, 32),
                 MaterialDef("water", "水", 2, 1, False, 0, True)):
    REGISTRY.register_material(material)
PIECE_QUANTITIES = {key: value.piece_quantity for key, value in REGISTRY.materials.items()}
BODY_MATERIALS = ("wood", "stone", "cloth")
MAX_CUSTOM_MATERIALS = 4
_MATERIAL_SYMBOL = re.compile(r"[a-z][a-z0-9_]{0,47}\Z")


def _custom_material_id(material_id):
    if (not isinstance(material_id, str) or not 3 <= len(material_id) <= 180
            or material_id.count("#") != 1):
        raise ValueError("注册材料标识须为定义引用#材料符号。")
    prefix, symbol = material_id.split("#")
    if (not prefix or any(not char.isprintable() or char.isspace() for char in prefix)
            or not _MATERIAL_SYMBOL.fullmatch(symbol)):
        raise ValueError("注册材料标识须为定义引用#材料符号。")
    if material_id in REGISTRY.materials:
        raise ValueError("不能覆盖内置材料定义。")


def bind_material(entity, definition):
    """Carry a transferred material's facts using Grove's identity and budget policy."""
    if definition.id in REGISTRY.materials:
        return
    _custom_material_id(definition.id)
    snapshot = definition.snapshot()
    program = entity.setdefault("state", {}).get("program", {})
    if not isinstance(program, dict):
        raise ValueError("接收物件的程序材料快照无效。")
    materials = program.get("materials", {})
    if not isinstance(materials, dict) or len(materials) > MAX_CUSTOM_MATERIALS:
        raise ValueError("接收物件最多携带4种注册材料。")
    previous = materials.get(definition.id)
    if previous is not None:
        if not definition.matches_snapshot(previous):
            raise ValueError("相同注册材料标识的物理定义不一致，不能覆盖。")
        return
    if len(materials) >= MAX_CUSTOM_MATERIALS:
        raise ValueError("接收物件的4种注册材料已满。")
    entity["state"].setdefault("program", program)
    program.setdefault("materials", materials)
    materials[definition.id] = snapshot


def registry_for(entity_or_list):
    """Build a local registry from the participating entities' trusted snapshots.

    Material metadata travels with lots, including ordinary receiving vessels.
    It never changes the shared built-in registry or enables executable rules.
    """
    registry = Registry(REGISTRY.version, REGISTRY.phases)
    registry.materials = dict(REGISTRY.materials)
    registry.components = dict(REGISTRY.components)
    registry.rules = dict(REGISTRY.rules)
    if entity_or_list is None:
        entities = ()
    elif isinstance(entity_or_list, Mapping):
        entities = ((entity_or_list,) if any(key in entity_or_list for key in ("id", "material", "state", "matter"))
                    else entity_or_list.values())
    else:
        entities = entity_or_list
    for entity in entities:
        if not isinstance(entity, Mapping):
            raise ValueError("材料注册须来自参与物件。")
        state = entity.get("state", {})
        if not isinstance(state, Mapping):
            raise ValueError("物件状态无效。")
        program = state.get("program", {})
        if not isinstance(program, Mapping):
            raise ValueError("物件程序材料快照无效。")
        materials = program.get("materials", {})
        if not isinstance(materials, Mapping) or len(materials) > MAX_CUSTOM_MATERIALS:
            raise ValueError("每个物件最多携带4种注册材料。")
        for material_id, snapshot in materials.items():
            _custom_material_id(material_id)
            definition = MaterialDef.from_snapshot(material_id, snapshot)
            previous = registry.materials.get(material_id)
            if previous is not None and previous != definition:
                raise ValueError("相同注册材料标识的物理定义不一致。")
            registry.materials[material_id] = definition
    return registry


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
register_material_rules(REGISTRY, combustion_cadence=COMBUSTION_CADENCE_TICKS)


DEFINITIONS = "grove.creative.definitions"


def instance_definition(ctx, entity):
    binding = entity["state"].get("program", {})
    if not binding.get("definition_ref"):
        return None, None
    pack = ctx.get_state(DEFINITIONS, binding["definition_ref"])
    if not pack or binding["type_id"] not in pack["definition"]["types"]:
        raise RuleViolation("物件引用的已发布规则版本缺失。")
    return pack["definition"], pack["definition"]["types"][binding["type_id"]]


def mixture_capacity(entity):
    """Grove's optional contained-mixture policy, shared by preview and reducers."""
    program = entity.get("state", {}).get("program", {})
    if "mixture_capacity" not in program:
        return None
    capacity = program["mixture_capacity"]
    if type(capacity) is not int or not 1 <= capacity <= 200:
        raise ValueError("混合物容量须为1至200的整数逻辑u。")
    return capacity


def material_identity(entity, symbol):
    program = entity["state"]["program"]
    return symbol if symbol in REGISTRY.materials else program.get("material_aliases", {}).get(
        symbol, program["definition_ref"] + "#" + symbol)


def contained_quantities(entity, *, symbolic=True):
    values = {}
    prefix = entity["state"].get("program", {}).get("definition_ref", "") + "#"
    aliases = {identity: alias for alias, identity in entity["state"].get("program", {}).get("material_aliases", {}).items()}
    for lot in entity["matter"]:
        if lot["slot"] != "contained":
            continue
        key = lot["material"]
        if symbolic:
            key = aliases.get(key, key[len(prefix):] if key.startswith(prefix) else key)
        values[key] = values.get(key, 0) + lot["quantity"]
    return values



def reaction_available_quantities(entity, *, supply=None):
    """Actual non-burning contents minus escrow, optionally limited to phase input.

    A phase supplies its frozen input balance, excluding same-step products.
    An observation omits it to describe current inputs without granting execution.
    """
    usable = {**entity, "matter": [lot for lot in entity["matter"] if not lot.get("burning", False)]}
    actual = contained_quantities(usable)
    offered = actual if supply is None else supply
    available = {key: min(amount, actual.get(key, 0)) for key, amount in offered.items()}
    contract = entity["state"].get("program", {}).get("contract", {})
    reserved = max(0, contract.get("delivered", 0)-contract.get("released", 0))
    if reserved:
        for symbol in available:
            if material_identity(entity, symbol) == contract.get("material"):
                available[symbol] = max(0, available[symbol]-reserved)
    return available
