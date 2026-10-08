"""Canonical lots and derived projections. No world reads or writes here."""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json

MAX_LOTS = 8
MAX_ENTITY_BYTES = 8192
MAX_QUANTITY = 10000
MAX_TREE_DEPTH = 8
MAX_DESCENDANTS = 16
MAX_FRAME_OBJECTS = 64


def encoded_size(value):
    return len(json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8"))


def lot_identifier(entity_id, key):
    return entity_id+":"+hashlib.sha256(str(key).encode()).hexdigest()[:20]


def normalize_lots(registry, entity_id, lots):
    if not isinstance(lots, list) or len(lots) > MAX_LOTS:
        raise ValueError("每个物件最多8个独立物质批次。")
    ids = {lot.get("id", str(i)): lot.get("id") if str(lot.get("id", "")).startswith(entity_id+":")
           else lot_identifier(entity_id, lot.get("id", i)) for i, lot in enumerate(lots) if isinstance(lot, dict)}
    result, seen = [], set()
    for i, current in enumerate(lots):
        if not isinstance(current, dict) or set(current)-{"id", "material", "quantity", "slot", "role", "bound_to", "burning", "origin_object", "origin_lot"}:
            raise ValueError("物质批次结构无效。")
        lot = deepcopy(current)
        lot["id"] = ids.get(current.get("id", str(i)), lot_identifier(entity_id, i))
        if lot["id"] in seen or lot.get("material") not in registry.materials:
            raise ValueError("物质批次标识或材料无效。")
        seen.add(lot["id"])
        if type(lot.get("quantity")) is not int or not 0 <= lot["quantity"] <= MAX_QUANTITY:
            raise ValueError("物质量必须是0至10000的整数逻辑u。")
        if lot.get("slot") not in {"body", "contained", "surface", "absorbed"}:
            raise ValueError("物质槽无效。")
        lot.setdefault("burning", False)
        if type(lot["burning"]) is not bool or (lot["burning"] and not registry.materials[lot["material"]].combustible):
            raise ValueError("只有可燃物质批次能燃烧。")
        if lot.get("bound_to") in ids:
            lot["bound_to"] = ids[lot["bound_to"]]
        lot.setdefault("role", "structural" if lot["slot"] == "body" else "substance")
        if lot["role"] not in ("structural", "lining", "substance"):
            raise ValueError("物质角色无效。")
        for key in ("origin_object", "origin_lot", "bound_to"):
            if key in lot and (not isinstance(lot[key], str) or len(lot[key]) > 180):
                raise ValueError("物质批次来源或依附标识无效。")
        if lot["quantity"]:
            result.append(lot)
    return result


def validate_components(registry, components):
    if not isinstance(components, dict) or set(components)-set(registry.components):
        raise ValueError("物件只能采用已注册部件。")
    result = deepcopy(components)
    for key, value in result.items():
        registry.components[key].validate(value)
    structure = result.get("structure")
    if structure and (structure.get("max_integrity", 0) < 1 or structure.get("integrity", 0) > structure["max_integrity"]):
        raise ValueError("承载结构完整度无效。")
    if result.get("collector", {}).get("mode") not in (None, "shelter", "collect"):
        raise ValueError("收集部件模式无效。")
    return result


def quantity(entity, material=None, slot=None):
    return sum(lot["quantity"] for lot in entity.get("matter", [])
               if (material is None or lot["material"] == material) and (slot is None or lot["slot"] == slot))


def component_capacity(entity, component):
    definition = entity.get("components", {}).get(component, {})
    if "supports" not in definition:
        return definition.get("capacity", 0)
    lots = {lot["id"]: lot for lot in entity.get("matter", [])}
    return sum(portion["capacity"]*lots[key]["quantity"]//portion["quantity"] for key, portion in definition["supports"].items()
               if key in lots and portion["quantity"] > 0)


def absorption_targets(entity, objects):
    component = entity.get("components", {}).get("absorbent", {})
    lots = {lot["id"]: lot for lot in entity.get("matter", [])}
    result = []
    for key, portion in component.get("supports", {}).items():
        lot = lots.get(key)
        if lot and exposed(entity, lot, objects):
            capacity = portion["capacity"]*lot["quantity"]//portion["quantity"]
            held = sum(water["quantity"] for water in entity["matter"] if water["material"] == "water" and water["slot"] == "absorbed" and water.get("bound_to") == key)
            if capacity > held:
                result.append((key, capacity-held))
    return result


def moisture(entity):
    capacity = component_capacity(entity, "absorbent")
    held = quantity(entity, "water", "absorbed")
    return min(100, held*100//capacity) if capacity else 0


def lot_moisture(entity, lot):
    """Water acts on its actual material binding or the shared liquid slot."""
    if (lot["slot"] == "contained" or lot.get("role") == "lining") and quantity(entity, "water", "contained"):
        return 100
    portion = entity.get("components", {}).get("absorbent", {}).get("supports", {}).get(lot["id"])
    if not portion:
        return 0
    capacity = portion["capacity"]*lot["quantity"]//portion["quantity"]
    held = sum(water["quantity"] for water in entity["matter"] if water["material"] == "water" and water["slot"] == "absorbed" and water.get("bound_to") == lot["id"])
    return min(100, held*100//capacity) if capacity else 0


def exposed(entity, lot, objects):
    """Lids protect internal matter, while exterior body/surface stays exposed."""
    if lot["slot"] == "contained" and not entity.get("components", {}).get("container", {}).get("open", True):
        return False
    if lot.get("role") == "lining" and not entity.get("components", {}).get("container", {}).get("open", True):
        return False
    current, seen = entity, set()
    for _ in range(MAX_TREE_DEPTH):
        if current["id"] in seen:
            raise ValueError("物理关系不能循环。")
        seen.add(current["id"])
        parent = current.get("physical_parent")
        if not parent:
            return True
        current = objects.get(parent["id"])
        if current is None:
            return False
        if parent["relation"] == "contained" and not current.get("components", {}).get("container", {}).get("open", True):
            return False
    return False


def children(entity, objects):
    return [obj for obj in objects.values() if (obj.get("physical_parent") or {}).get("id") == entity["id"]]


def load_summary(registry, entity, objects):
    seen = set()
    def total(current, depth):
        if depth >= MAX_TREE_DEPTH or current["id"] in seen:
            raise ValueError("物理关系无效或过深。")
        seen.add(current["id"])
        local = sum(l["quantity"]*registry.materials[l["material"]].load_per_u for l in current.get("matter", []))
        own_body = sum(l["quantity"]*registry.materials[l["material"]].load_per_u for l in current.get("matter", []) if l["slot"] == "body")
        descendants = [total(child, depth+1) for child in children(current, objects)]
        buoyancy = component_capacity(current, "buoyant")
        buoyancy += sum(value[3] for value in descendants if value[4] == "attached")
        relation = (current.get("physical_parent") or {}).get("relation")
        return local+sum(value[0] for value in descendants), local, local-own_body+sum(value[0] for value in descendants), buoyancy, relation
    carried, local, supported, buoyancy, _ = total(entity, 0)
    structure = entity.get("components", {}).get("structure", {})
    maximum, integrity = structure.get("max_integrity", 100), structure.get("integrity", 100)
    if entity.get("lifecycle", "intact") != "intact":
        maximum = entity.get("state", {}).get("failed_max_integrity", maximum)
        integrity = 0
    limit = structure.get("support_limit", 0)*integrity//max(1, maximum)
    return {"unit": "logical_load", "local": local, "total": carried, "supported": supported,
            "limit": limit, "overload": max(0, supported-limit) if structure else 0,
            "integrity": integrity, "max_integrity": maximum, "buoyancy": buoyancy,
            "can_float": entity.get("lifecycle", "intact") == "intact" and buoyancy > 0 and carried <= buoyancy,
            "wetness": moisture(entity), "water": quantity(entity, "water", "contained"),
            "body": [{"material": lot["material"], "quantity": lot["quantity"]} for lot in entity.get("matter", []) if lot["slot"] == "body"],
            "burning_lots": [lot["id"] for lot in entity.get("matter", []) if lot.get("burning") and lot["quantity"]]}


def project(registry, entity):
    state, components = entity.setdefault("state", {}), entity.get("components", {})
    state["water"] = quantity(entity, "water", "contained") if "water_source" not in components else 0
    state["water_remaining"] = quantity(entity, "water", "contained") if "water_source" in components else 0
    state["liquid_quantity"] = sum(lot["quantity"] for lot in entity.get("matter", [])
                                   if lot["slot"] == "contained" and registry.materials[lot["material"]].liquid)
    state["liquid_capacity"] = components.get("water_source", {}).get("capacity", components.get("container", {}).get("liquid_capacity", components.get("collector", {}).get("capacity", 0)))
    state["water_capacity"] = components.get("container", {}).get("liquid_capacity", components.get("collector", {}).get("capacity", 0))
    state["water_source_capacity"] = components.get("water_source", {}).get("capacity", 200)
    state["wetness"] = moisture(entity)
    state["fuel"] = sum(lot["quantity"] for lot in entity.get("matter", []) if registry.materials[lot["material"]].combustible)
    state["burning"] = any(lot.get("burning") for lot in entity.get("matter", []))
    state["capacity"] = components.get("container", {}).get("slots", 0)
    state["open"] = components.get("container", {}).get("open", state.get("open", True))
    state["destroyed"] = entity.get("lifecycle") == "consumed"
    state["broken"] = entity.get("lifecycle") == "broken"
    if "heater" in components:
        state["lit"] = components["heater"].get("lit", False)
        state["hearth_fuel"] = sum(lot["quantity"] for lot in entity.get("matter", []) if lot["slot"] == "contained" and registry.materials[lot["material"]].combustible)
    if "collector" in components:
        state["mode"] = components["collector"].get("mode", "shelter")
    if "resource" in components:
        resource = components["resource"]
        state["resource_kind"] = resource["material"]
        state["stock_capacity"] = resource["capacity"]
        state["stock"] = quantity(entity, resource["material"], "contained")//registry.materials[resource["material"]].piece_quantity
    state["rules_version"] = registry.version
    return entity
