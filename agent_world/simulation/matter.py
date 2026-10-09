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
_ALL_SUPPORTS = object()


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


def liquid_quantity(registry, entity, slot="contained"):
    return sum(lot["quantity"] for lot in entity.get("matter", [])
               if registry.materials[lot["material"]].liquid and (slot is None or lot["slot"] == slot))


def liquid_capacity(entity):
    components = entity.get("components", {})
    return components.get("water_source", {}).get("capacity", components.get("container", {}).get(
        "liquid_capacity", components.get("collector", {}).get("capacity", 0)))


def liquid_room(registry, entity):
    return max(0, liquid_capacity(entity)-liquid_quantity(registry, entity))


def checked_mixture_capacity(capacity):
    """Validate the supplied policy independently of its current occupancy."""
    if capacity is None:
        return None
    if type(capacity) is not int or not 0 <= capacity <= MAX_QUANTITY:
        raise ValueError("混合物容量必须是有界的非负整数逻辑u。")
    return capacity


def mixture_room(entity, capacity):
    """A supplied total-content policy; material simulation does not own it."""
    capacity = checked_mixture_capacity(capacity)
    if capacity is None:
        return None
    return max(0, capacity-quantity(entity, slot="contained"))


def contained_room(registry, entity, material, *, mixture_capacity=None):
    """Liquid contents share volume; every content shares a supplied mixture cap."""
    free = (liquid_room(registry, entity) if registry.materials[material].liquid
            else MAX_QUANTITY-quantity(entity, material, "contained"))
    mixture = mixture_room(entity, mixture_capacity)
    return min(free, mixture) if mixture is not None else free


def reaction_capacity(capacity, held, available):
    """Keep occupied but unavailable products/escrow outside the planner's supply."""
    return max(0, capacity-held+available)


def validate_contents(registry, entity, *, mixture_capacity=None):
    # Use actual quantities, rather than clamped room, to reject an overfull final state.
    mixture_capacity = checked_mixture_capacity(mixture_capacity)
    if mixture_capacity is not None and quantity(entity, slot="contained") > mixture_capacity:
        raise ValueError("真实混合物总量超过物件共用容量。")
    liquid = liquid_quantity(registry, entity)
    if liquid > liquid_capacity(entity):
        raise ValueError("真实液体总量超过物件共用容量。")
    return liquid


def withdraw_lot(lot, amount):
    """One batch withdrawal. Selection, escrow and partial allocation are callers' policy."""
    if type(amount) is not int or amount < 0 or amount > lot["quantity"]:
        raise ValueError("实际可用材料不足或扣减数量无效。")
    lot["quantity"] -= amount


def take_material(entity, material, amount, *, slots=("contained",), include_burning=False, remove_empty=True):
    """Strict withdrawal in stable batch order, checking the whole amount first."""
    lots = [lot for lot in entity["matter"] if lot["material"] == material and lot["slot"] in slots
            and (include_burning or not lot.get("burning", False))]
    if type(amount) is not int or amount < 0 or sum(lot["quantity"] for lot in lots) < amount:
        raise ValueError("实际可用材料不足或扣减数量无效。")
    left = amount
    for lot in sorted(lots, key=lambda lot: lot["id"]):
        current = min(left, lot["quantity"])
        withdraw_lot(lot, current)
        left -= current
    if remove_empty:
        entity["matter"] = [lot for lot in entity["matter"] if lot["quantity"]]


def add_lot(entity, material, amount, slot, *, bound_to=None, source_lot=None,
            merge_burning=None, merge_bindings=True, preserve_body_provenance=True, id_factory=None):
    """Add with shared quantity/count bounds and an explicit merge policy.

    ``merge_burning=None`` preserves transfer reduction's existing merge with a
    burning batch. Reactions pass False, keeping their output out of that batch.
    ``merge_bindings=False`` preserves authored output's material/slot-only merge.
    ``id_factory`` is a zero-argument factory; otherwise IDs derive from provenance.
    """
    if type(amount) is not int or not 0 <= amount <= MAX_QUANTITY:
        raise ValueError("物质数量达到上限或添加数量无效。")
    if not amount:
        return None
    origin_object = (source_lot or {}).get("origin_object") if slot == "body" and preserve_body_provenance else None
    origin_lot = (source_lot or {}).get("origin_lot") if origin_object else None
    key = (material, slot, bound_to, origin_object, origin_lot)
    match = next((lot for lot in entity["matter"]
                  if (lot["material"], lot["slot"]) == (material, slot)
                  and (not merge_bindings or (lot.get("bound_to"), lot.get("origin_object"), lot.get("origin_lot")) == key[2:])
                  and (merge_burning is None or lot.get("burning", False) == merge_burning)), None)
    if match:
        if match["quantity"]+amount > MAX_QUANTITY:
            raise ValueError("物质数量达到上限。")
        match["quantity"] += amount
        return match
    if len(entity["matter"]) >= MAX_LOTS:
        raise ValueError("物件的8个物质批次已满。")
    lot = {"id": id_factory() if id_factory is not None else lot_identifier(entity["id"], key),
           "material": material, "quantity": amount, "slot": slot,
           "role": (source_lot or {}).get("role", "structural") if slot == "body" else "substance", "burning": False}
    if bound_to:
        lot["bound_to"] = bound_to
    if origin_object:
        lot.update(origin_object=origin_object, origin_lot=origin_lot or (source_lot or {})["id"])
    entity["matter"].append(lot)
    return lot


def accumulate_quantity(accounts, key, amount, *, limit=10**18):
    """Bounded accumulation shared by physical and authored source/sink accounts."""
    if type(amount) is not int or not 0 <= amount <= limit:
        raise ValueError("物质账数量无效。")
    if not amount:
        return
    current = accounts.get(key, 0)
    if type(current) is not int or not 0 <= current <= limit or current+amount > limit:
        raise ValueError("物质账的有界计数已满。")
    accounts[key] = current+amount


def _supported_capacity(lot, portion):
    return portion["capacity"]*lot["quantity"]//portion["quantity"] if lot and portion and portion["quantity"] > 0 else 0


def component_capacity(entity, component):
    definition = entity.get("components", {}).get(component, {})
    if "supports" not in definition:
        return definition.get("capacity", 0)
    lots = {lot["id"]: lot for lot in entity.get("matter", [])}
    return sum(_supported_capacity(lots.get(key), portion) for key, portion in definition["supports"].items())


def absorption_capacity(entity, bound_to=_ALL_SUPPORTS):
    if bound_to is _ALL_SUPPORTS:
        return component_capacity(entity, "absorbent")
    portion = entity.get("components", {}).get("absorbent", {}).get("supports", {}).get(bound_to)
    support = next((lot for lot in entity.get("matter", []) if lot["id"] == bound_to), None)
    return _supported_capacity(support, portion)


def absorption_room(entity, bound_to=_ALL_SUPPORTS):
    held = sum(lot["quantity"] for lot in entity.get("matter", [])
               if lot["material"] == "water" and lot["slot"] == "absorbed"
               and (bound_to is _ALL_SUPPORTS or lot.get("bound_to") == bound_to))
    return max(0, absorption_capacity(entity, bound_to)-held)


def absorption_targets(entity, objects):
    component = entity.get("components", {}).get("absorbent", {})
    lots = {lot["id"]: lot for lot in entity.get("matter", [])}
    result = []
    for key in component.get("supports", {}):
        lot = lots.get(key)
        if lot and exposed(entity, lot, objects):
            room = absorption_room(entity, key)
            if room:
                result.append((key, room))
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
    capacity = absorption_capacity(entity, lot["id"])
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


def ignitable_lot(registry, entity, lot, objects):
    """Physical ignition condition, without world permission, safety or budgets."""
    return (registry.materials[lot["material"]].combustible and lot_moisture(entity, lot) < 35
            and not quantity(entity, "water", "surface") and exposed(entity, lot, objects))


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
    state["liquid_quantity"] = liquid_quantity(registry, entity)
    state["liquid_capacity"] = liquid_capacity(entity)
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
