"""Snapshot, effect reduction and single dirty overlay for a physical step."""
from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping
from copy import deepcopy
from dataclasses import dataclass, replace
from types import MappingProxyType

from .effects import Break, Damage, Move, SetBurning, Sink, Source, SpillSink, SpillTransfer, Transfer
from .matter import (MAX_FRAME_OBJECTS, MAX_LOTS, MAX_QUANTITY, children, lot_identifier,
                     normalize_lots, project)


def _freeze(value):
    if isinstance(value, dict):
        return MappingProxyType({key: _freeze(item) for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_freeze(item) for item in value)
    if isinstance(value, set):
        return frozenset(value)
    return value


@dataclass(frozen=True)
class Snapshot:
    registry: object
    tick: int
    objects: object
    protected: frozenset
    inputs: object
    heated: frozenset
    wet_by_rain: frozenset
    moves: object
    spills: object


class Frame:
    def __init__(self, registry, objects, tick=0, *, protected=(), inputs=(), heated=(), wet_by_rain=(), moves=(), spills=None):
        if len(objects) > MAX_FRAME_OBJECTS:
            raise ValueError("活动物理闭包最多64个物件。")
        self.registry, self.tick = registry, tick
        self.objects = deepcopy(objects)
        self.before = deepcopy(objects)
        self.protected = frozenset(protected)
        self.inputs, self.heated, self.wet_by_rain = tuple(inputs), frozenset(heated), frozenset(wet_by_rain)
        self.moves = tuple(moves)
        self.spills = deepcopy(spills or {})
        self.spills_before = deepcopy(self.spills)
        self.sources, self.sinks = defaultdict(int), defaultdict(int)
        self.dirty, self.broken = set(), []

    def snapshot(self):
        return Snapshot(self.registry, self.tick, _freeze(deepcopy(self.objects)), self.protected,
                        _freeze(self.inputs), self.heated, self.wet_by_rain, _freeze(self.moves), _freeze(self.spills))

    def _lot(self, entity_id, lot_id):
        return next((lot for lot in self.objects[entity_id]["matter"] if lot["id"] == lot_id), None)

    def _liquid_quantity(self, entity):
        return sum(lot["quantity"] for lot in entity["matter"]
                   if lot["slot"] == "contained" and self.registry.materials[lot["material"]].liquid)

    def _mixture_room(self, entity):
        program = entity.get("state", {}).get("program", {})
        if not isinstance(program, Mapping):
            raise ValueError("物件的程序绑定无效。")
        if "mixture_capacity" not in program:
            return None
        capacity = program["mixture_capacity"]
        if type(capacity) is not int or not 1 <= capacity <= 200:
            raise ValueError("混合物容量须为1至200的整数逻辑u。")
        return max(0, capacity-sum(lot["quantity"] for lot in entity["matter"]
                                  if lot["slot"] == "contained"))

    def _inherit_material(self, entity, material):
        if material in {"wood", "stone", "cloth", "water"}:
            return
        definition = self.registry.materials.get(material)
        if definition is None:
            raise ValueError("转移物质未注册。")
        if (not isinstance(definition.name, str) or not 1 <= len(definition.name) <= 80
                or not definition.name.strip() or not definition.name.isprintable()
                or type(definition.piece_quantity) is not int or not 1 <= definition.piece_quantity <= 10000
                or type(definition.load_per_u) is not int or not 1 <= definition.load_per_u <= 8
                or type(definition.combustible) is not bool or type(definition.liquid) is not bool):
            raise ValueError("注册材料名称或份量无效。")
        snapshot = {"name": definition.name, "load_per_u": definition.load_per_u,
                    "piece_quantity": definition.piece_quantity,
                    "combustible": definition.combustible, "liquid": definition.liquid}
        program = entity.setdefault("state", {}).get("program", {})
        if not isinstance(program, dict):
            raise ValueError("接收物件的程序材料快照无效。")
        materials = program.get("materials", {})
        if not isinstance(materials, dict) or len(materials) > 4:
            raise ValueError("接收物件最多携带4种注册材料。")
        previous = materials.get(material)
        if previous is not None:
            if not isinstance(previous, Mapping):
                raise ValueError("接收物件的材料定义无效。")
            comparable = dict(previous)
            comparable.setdefault("piece_quantity", 4)
            if comparable != snapshot:
                raise ValueError("相同注册材料标识的物理定义不一致，不能覆盖。")
            return
        if len(materials) >= 4:
            raise ValueError("接收物件的4种注册材料已满。")
        entity["state"].setdefault("program", program)
        program.setdefault("materials", materials)
        materials[material] = snapshot

    def _add(self, entity_id, material, amount, slot, *, source_lot=None, bound_to=None):
        if amount == 0:
            return
        entity = self.objects[entity_id]
        if material not in self.registry.materials:
            raise ValueError("转移物质未注册。")
        if slot == "absorbed" and material != "water":
            raise ValueError("吸附槽只支持实际水，其他物质不能冒用吸水机制。")
        if slot == "contained":
            mixture_room = self._mixture_room(entity)
            if mixture_room is not None and amount > mixture_room:
                raise ValueError("真实混合物总量超过物件共用容量。")
        self._inherit_material(entity, material)
        origin_object = (source_lot or {}).get("origin_object")
        origin_lot = (source_lot or {}).get("origin_lot")
        # Body provenance is kept for one-time dismantling. Unrefundable stored
        # substances can merge, bounding repeated refuelling without a growing log.
        if slot != "body":
            origin_object = origin_lot = None
        match = next((lot for lot in entity["matter"] if (lot["material"], lot["slot"], lot.get("bound_to"), lot.get("origin_object"), lot.get("origin_lot"))
                      == (material, slot, bound_to, origin_object, origin_lot)), None)
        if match:
            if match["quantity"]+amount > MAX_QUANTITY:
                raise ValueError("物质数量达到上限。")
            match["quantity"] += amount
        else:
            if len(entity["matter"]) >= MAX_LOTS:
                raise ValueError("物件的8个物质批次已满。")
            key = (material, slot, bound_to, origin_object, origin_lot)
            lot = {"id": lot_identifier(entity_id, key), "material": material, "quantity": amount, "slot": slot,
                   "role": (source_lot or {}).get("role", "structural") if slot == "body" else "substance", "burning": False}
            if bound_to:
                lot["bound_to"] = bound_to
            if origin_object:
                lot.update(origin_object=origin_object, origin_lot=origin_lot or (source_lot or {})["id"])
            entity["matter"].append(lot)
        self.dirty.add(entity_id)

    def apply(self, effects, *, allowed_sources=(), allowed_sinks=(), allocate=False):
        effects = tuple(effects)
        rooms = {}
        def room(effect):
            source = None if isinstance(effect, (Source, SpillTransfer)) else self._lot(effect.source, effect.lot_id)
            if not isinstance(effect, (Source, SpillTransfer)) and source is None:
                raise ValueError("转移来源物质批次不存在。")
            material = effect.material if isinstance(effect, Source) else "water" if isinstance(effect, SpillTransfer) else source["material"]
            if material not in self.registry.materials:
                raise ValueError("转移物质未注册。")
            if effect.slot == "absorbed" and material != "water":
                raise ValueError("吸附槽只支持实际水，其他物质不能冒用吸水机制。")
            liquid = self.registry.materials[material].liquid and effect.slot == "contained"
            # All contained liquids reserve the same room. Material-specific
            # keys would let simultaneous liquids each allocate the full vessel.
            key = (effect.target, "@liquid" if liquid else material, effect.slot,
                   None if liquid else effect.bound_to)
            target = self.objects[key[0]]
            if key not in rooms:
                slot, bound = key[2:]
                if material == "water" and slot == "absorbed":
                    portion = target["components"].get("absorbent", {}).get("supports", {}).get(bound)
                    support = self._lot(key[0], bound)
                    capacity = portion["capacity"]*support["quantity"]//portion["quantity"] if portion and support else 0
                    held = sum(lot["quantity"] for lot in target["matter"] if lot["material"] == material and lot["slot"] == slot and lot.get("bound_to") == bound)
                    rooms[key] = max(0, capacity-held)
                elif liquid:
                    components = target["components"]
                    capacity = components.get("water_source", {}).get("capacity", components.get("container", {}).get("liquid_capacity", components.get("collector", {}).get("capacity", 0)))
                    rooms[key] = max(0, capacity-self._liquid_quantity(target))
                else:
                    rooms[key] = MAX_QUANTITY-sum(lot["quantity"] for lot in target["matter"] if (lot["material"], lot["slot"], lot.get("bound_to")) == (material, slot, bound))
            keys = [key]
            if effect.slot == "contained":
                mixture_room = self._mixture_room(target)
                if mixture_room is not None:
                    mixture_key = (effect.target, "@mixture", "contained", None)
                    rooms.setdefault(mixture_key, mixture_room)
                    keys.append(mixture_key)
            return tuple(keys)

        def reserve(destination, amount, strict=False):
            free = min(rooms[key] for key in destination)
            if strict and amount > free:
                raise ValueError("目的物质槽的实际容量不足。")
            amount = min(amount, free)
            for key in destination:
                rooms[key] -= amount
            return amount
        if allocate:
            # Automatic consumers receive a stable allocation in registered rule
            # and entity order. Commands use strict transfers and reject shortages.
            available, planned = {}, []
            for effect in effects:
                if isinstance(effect, (Transfer, Sink)):
                    key = (effect.source, effect.lot_id)
                    if key not in available:
                        lot = self._lot(*key)
                        available[key] = lot["quantity"] if lot else 0
                    amount = min(available[key], effect.quantity)
                    if isinstance(effect, Transfer) and amount:
                        destination = room(effect)
                        amount = reserve(destination, amount)
                    available[key] -= amount
                    if amount:
                        planned.append(replace(effect, quantity=amount))
                elif isinstance(effect, Source):
                    destination = room(effect)
                    amount = reserve(destination, effect.quantity)
                    rejected = effect.quantity-amount
                    if rejected:
                        if effect.reason not in allowed_sources or "drain" not in allowed_sinks:
                            raise ValueError("自动source目的容量不足且未声明余量sink。")
                        self.sources[(effect.material, effect.reason)] += rejected
                        self.sinks[(effect.material, "drain")] += rejected
                    if amount:
                        planned.append(replace(effect, quantity=amount))
                elif isinstance(effect, (SpillTransfer, SpillSink)):
                    key = ("spill", effect.reservoir)
                    available.setdefault(key, self.spills.get(effect.reservoir, 0))
                    amount = min(available[key], effect.quantity)
                    if isinstance(effect, SpillTransfer) and amount:
                        destination = room(effect)
                        amount = reserve(destination, amount)
                    available[key] -= amount
                    if amount:
                        planned.append(replace(effect, quantity=amount))
                else:
                    planned.append(effect)
            effects = tuple(planned)
        else:
            for effect in effects:
                if isinstance(effect, (Transfer, Source, SpillTransfer)):
                    destination = room(effect)
                    reserve(destination, effect.quantity, strict=True)
        withdrawals = defaultdict(int)
        setters, damage = {}, defaultdict(int)
        for effect in effects:
            if isinstance(effect, (Transfer, Sink)):
                if type(effect.quantity) is not int or effect.quantity < 0:
                    raise ValueError("转移数量无效。")
                withdrawals[(effect.source, effect.lot_id)] += effect.quantity
            elif isinstance(effect, Source):
                if effect.reason not in allowed_sources or effect.material not in self.registry.materials or type(effect.quantity) is not int or effect.quantity < 0:
                    raise ValueError("未授权的物质source。")
            elif isinstance(effect, (SpillTransfer, SpillSink)):
                if type(effect.quantity) is not int or effect.quantity < 0:
                    raise ValueError("泄水转移数量无效。")
                if isinstance(effect, SpillSink) and effect.reason not in allowed_sinks:
                    raise ValueError("未授权的泄水sink。")
                key = ("spill", effect.reservoir)
                withdrawals[key] += effect.quantity
            elif isinstance(effect, SetBurning):
                key = (effect.target, effect.lot_id)
                if key in setters and setters[key] != effect.burning:
                    raise ValueError("同阶段独占writer冲突。")
                setters[key] = effect.burning
            elif isinstance(effect, Damage):
                if type(effect.amount) is not int or effect.amount < 0:
                    raise ValueError("损伤效果无效。")
                damage[effect.target] += effect.amount
        for (entity_id, lot_id), amount in withdrawals.items():
            if entity_id == "spill":
                if amount > self.spills.get(lot_id, 0):
                    raise ValueError("泄水储池被重复支用。")
                continue
            lot = self._lot(entity_id, lot_id)
            if not lot or amount > lot["quantity"]:
                raise ValueError("同阶段转移争用了不足的来源物质。")
        for effect in effects:
            if isinstance(effect, Transfer):
                source = self._lot(effect.source, effect.lot_id)
                copy = deepcopy(source)
                copy.setdefault("origin_object", effect.source)
                copy.setdefault("origin_lot", source["id"])
                source["quantity"] -= effect.quantity
                self._add(effect.target, source["material"], effect.quantity, effect.slot, source_lot=copy, bound_to=effect.bound_to)
                self.dirty.add(effect.source)
            elif isinstance(effect, Sink):
                if effect.reason not in allowed_sinks:
                    raise ValueError("未授权的物质sink。")
                source = self._lot(effect.source, effect.lot_id)
                source["quantity"] -= effect.quantity
                self.sinks[(source["material"], effect.reason)] += effect.quantity
                self.dirty.add(effect.source)
            elif isinstance(effect, Source):
                self._add(effect.target, effect.material, effect.quantity, effect.slot, bound_to=effect.bound_to)
                self.sources[(effect.material, effect.reason)] += effect.quantity
            elif isinstance(effect, Move):
                entity = self.objects[effect.target]
                entity.update(x=effect.x, y=effect.y)
                self.dirty.add(effect.target)
            elif isinstance(effect, SpillTransfer):
                self.spills[effect.reservoir] -= effect.quantity
                self._add(effect.target, "water", effect.quantity, effect.slot, bound_to=effect.bound_to)
            elif isinstance(effect, SpillSink):
                self.spills[effect.reservoir] -= effect.quantity
                self.sinks[("water", effect.reason)] += effect.quantity
        for (entity_id, lot_id), burning in setters.items():
            lot = self._lot(entity_id, lot_id)
            if lot and lot["quantity"]:
                if burning and not self.registry.materials[lot["material"]].combustible:
                    raise ValueError("该物质不能燃烧。")
                lot["burning"] = burning
                self.dirty.add(entity_id)
        for entity_id, amount in damage.items():
            structure = self.objects[entity_id].get("components", {}).get("structure")
            if structure:
                structure["integrity"] = max(0, structure["integrity"]-amount)
                self.dirty.add(entity_id)
        breaking = {effect.target for effect in effects if isinstance(effect, Break)}
        for entity_id in sorted(breaking):
            self._break(entity_id, breaking)
        for entity_id in self.dirty:
            entity = self.objects[entity_id]
            entity["matter"] = [lot for lot in entity["matter"] if lot["quantity"]]
            project(self.registry, entity)

    def _consume(self, entity):
        root, seen = entity, set()
        while root.get("physical_parent"):
            if root["id"] in seen:
                raise ValueError("物理关系不能循环。")
            seen.add(root["id"])
            root = self.objects[root["physical_parent"]["id"]]
        parent = entity.get("physical_parent")
        if parent:
            previous = self.objects[parent["id"]]
            for key in ("contents", "attachments"):
                previous["state"][key] = [eid for eid in previous["state"].get(key, []) if eid != entity["id"]]
            self.dirty.add(previous["id"])
        entity.update(lifecycle="consumed", physical_parent=None, container=None,
                      x=None if root.get("owner") else root.get("x"), y=None if root.get("owner") else root.get("y"))
        self.dirty.add(entity["id"])

    def _break(self, entity_id, breaking=()):
        entity = self.objects[entity_id]
        if entity.get("lifecycle") == "consumed":
            return
        if entity.get("lifecycle") == "broken":
            if not any(lot["quantity"] for lot in entity["matter"]) and not children(entity, self.objects):
                self._consume(entity)
            return
        root, seen = entity, set()
        recipient = None
        while root.get("physical_parent"):
            if root["id"] in seen:
                raise ValueError("物理关系不能循环。")
            seen.add(root["id"])
            relationship = root["physical_parent"]
            parent = self.objects[relationship["id"]]
            if recipient is None and relationship["relation"] == "contained" and parent["id"] not in breaking and parent.get("lifecycle") == "intact" and "container" in parent["components"]:
                recipient = parent
            root = parent
        owner, x, y = root.get("owner"), root.get("x"), root.get("y")
        entity["lifecycle"] = "broken"
        if "structure" in entity["components"]:
            entity["state"]["failed_max_integrity"] = entity["components"]["structure"]["max_integrity"]
        old_children = children(entity, self.objects)
        entity["state"]["contents"] = []
        entity["state"]["attachments"] = []
        for child in old_children:
            if recipient:
                contents = recipient["state"]["contents"]
                if len(contents) < recipient["components"]["container"]["slots"]:
                    child.update(physical_parent={"id": recipient["id"], "relation": "contained"}, container=recipient["id"], owner=owner, x=None, y=None)
                    contents.append(child["id"])
                    self.dirty.add(recipient["id"])
                else:
                    child.update(physical_parent={"id": entity_id, "relation": "attached"}, container=None, owner=owner, x=None, y=None)
                    entity["state"]["attachments"].append(child["id"])
            else:
                child.update(physical_parent=None, container=None, owner=owner, x=None if owner else x, y=None if owner else y)
            self.dirty.add(child["id"])
        for key in ("container", "structure", "barrier", "covering", "collector", "heater", "attachment"):
            entity["components"].pop(key, None)
        for lot in entity["matter"]:
            if lot["slot"] == "contained":
                if recipient and self.registry.materials[lot["material"]].liquid:
                    free = recipient["components"]["container"]["liquid_capacity"]-self._liquid_quantity(recipient)
                    mixture_room = self._mixture_room(recipient)
                    if mixture_room is not None:
                        free = min(free, mixture_room)
                    amount = min(max(0, free), lot["quantity"])
                    self._add(recipient["id"], lot["material"], amount, "contained")
                    lot["quantity"] -= amount
                    lot["slot"] = "surface"
                elif owner:
                    # Private breakage cannot publish protected substances.
                    lot["slot"] = "surface"
                elif lot["material"] == "water":
                    key = f"{x},{y}"
                    available = MAX_QUANTITY-self.spills.get(key, 0)
                    held = min(available, lot["quantity"])
                    self.spills[key] = self.spills.get(key, 0)+held
                    self.sinks[("water", "drain")] += lot["quantity"]-held
                    lot["quantity"] = 0
                else:
                    # Solid/fuel substances remain on the broken body, with
                    # their own material and quantity; no material is erased.
                    lot["slot"] = "surface"
        if not any(lot["quantity"] for lot in entity["matter"]) and not children(entity, self.objects):
            self._consume(entity)
        self.dirty.add(entity_id)
        self.broken.append(entity_id)

    def run(self):
        ordered = self.registry.ordered_rules()
        for phase in self.registry.phases:
            effects, sources, sinks = [], set(), set()
            snapshot = self.snapshot()
            for rule in ordered:
                if rule.phase != phase:
                    continue
                if len(self.objects) > rule.max_participants:
                    raise ValueError("物理规则参与闭包超限。")
                proposed = tuple(rule.evaluate(snapshot))
                if len(proposed) > rule.max_effects:
                    raise ValueError("物理规则效果超出声明预算。")
                for effect in proposed:
                    required = ({"matter"} if isinstance(effect, (Transfer, Source, Sink, SpillTransfer, SpillSink)) else
                                {"structure.integrity"} if isinstance(effect, Damage) else
                                {"matter.burning"} if isinstance(effect, SetBurning) else
                                {"position"} if isinstance(effect, Move) else
                                {"lifecycle", "relationships", "matter", "components"} if isinstance(effect, Break) else None)
                    if required is None or not required.issubset(rule.writes):
                        raise ValueError("规则效果超出此规则声明的writer。")
                    if isinstance(effect, Source) and effect.reason not in rule.sources:
                        raise ValueError("此规则没有声明所用source。")
                    if isinstance(effect, (Sink, SpillSink)) and effect.reason not in rule.sinks:
                        raise ValueError("此规则没有声明所用sink。")
                    if isinstance(effect, Break) and "drain" not in rule.sinks:
                        raise ValueError("结构失效规则没有声明泄水drain。")
                effects.extend(proposed)
                sources.update(rule.sources)
                sinks.update(rule.sinks)
            if len(effects) > 1024:
                raise ValueError("同阶段物理效果超限。")
            self.apply(effects, allowed_sources=sources, allowed_sinks=sinks, allocate=True)
        return self

    def verify_balance(self):
        before, after = defaultdict(int), defaultdict(int)
        for entity in self.before.values():
            for lot in entity["matter"]:
                before[lot["material"]] += lot["quantity"]
        for entity in self.objects.values():
            for lot in entity["matter"]:
                after[lot["material"]] += lot["quantity"]
        before["water"] += sum(self.spills_before.values())
        after["water"] += sum(self.spills.values())
        for (material, _), amount in self.sources.items():
            before[material] += amount
        for (material, _), amount in self.sinks.items():
            after[material] += amount
        if any(before[key] != after[key] for key in set(before).union(after)):
            raise ValueError("物质转移未守恒。")
        return True
