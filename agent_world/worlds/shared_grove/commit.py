"""Commit trusted Grove results and their shared lifecycle consequences.

Action reducers and logical-step reducers produce candidates. This module owns
normalization, spatial/inventory indexes, physical admission, material accounts,
spills and public change publication inside the Runtime's existing transaction.
Storage and simulation never call back into the action or authored-rule runner.
"""
from __future__ import annotations

from copy import deepcopy
from contextlib import contextmanager, nullcontext
import hashlib

from agent_world.errors import RuleViolation, StateConflict
from agent_world.simulation import matter as sm
from agent_world.simulation.effects import Sink, Transfer
from agent_world.simulation.frame import Frame
from . import model as m, activity, events
from .definitions import RULES_VERSION, PIECE_QUANTITIES, registry_for, bind_material, mixture_capacity


def save_entity(ctx, entity, expected_version=None):
    from .contact import prepare_materials
    prepare_materials(ctx, entity)
    result, changed = m.store_entity(ctx, entity, expected_version=expected_version)
    if changed:
        from .contact import reserve, may_contact, authored_outputs
        contacts = reserve(ctx, result)
        activity.reconcile_entity(ctx, result)
        # Reparenting can create a future spill recipient even when this subject
        # is only a dry outer vessel. A bounded forest has at most17 members.
        if not contacts:
            contacts = any(may_contact(ctx, obj, authored_outputs(ctx, obj)) for obj in m._tree_objects(ctx, result).values())
        batch = getattr(ctx, "_grove_batch", None)
        if batch is not None and contacts:
            batch.contact_admission = True
        elif contacts:
            from .creative import _check_instance_capacity
            _check_instance_capacity(ctx, activity.meta_get(ctx, "creative_instances", []))
    return result


def ensure_chunk(ctx, cx, cy):
    chunk = m.get_chunk(ctx, cx, cy)
    if chunk["version"]:
        return chunk
    m.save_chunk(ctx, chunk)
    style = m.chunk_landscape(cx, cy)
    resources = {"common_bank": ("wood", "cloth", "stone", "wood"),
                 "willow_grove": ("wood", "wood", "wood", "cloth"),
                 "reed_marsh": ("cloth", "cloth", "wood"),
                 "stone_ridge": ("stone", "stone", "wood"),
                 "open_bank": ("wood", "cloth", "stone")}[style]
    preferred = [(2, 2), (4, 2), (2, 5), (4, 5)] if style == "common_bank" else []
    land = preferred + [(x, y) for y in range(2, 15) for x in range(2, 15)
                        if (x, y) not in preferred]
    land = [(x, y) for x, y in land
            if max(abs(cx*16+x), abs(cy*16+y)) <= m.WORLD_LIMIT
            and m.terrain(cx*16+x, cy*16+y) not in {"water", "rock"}]
    if style != "common_bank":
        land.sort(key=lambda p: m.stable_number("node_position", cx, cy, *p))
    names = {"wood": "柳枝丛", "cloth": "芦苇丛", "stone": "卵石堆"}
    for i, material in enumerate(resources):
        if i >= len(land):
            break
        ox, oy = land[i]
        spawn_entity(ctx, {"id": f"node_{cx}_{cy}_{i}", "name": names[material],
                          "x": cx*16+ox, "y": cy*16+oy, "material": material,
                          "capabilities": ["resource_node", "wettable"], "source": "terrain",
                          "state": {"kind": "resource_node", "resource_kind": material,
                                    "stock": 4, "stock_capacity": 4, "regrowth_tick": None}})
    log = ("倒木", "wood", ["transport", "blocking", "flammable", "wettable"], {"kind": "raw_material"})
    bowl = ("石碗", "stone", ["transport", "container", "wettable"], {"capacity": 8, "water_capacity": 20})
    crate = ("木箱", "wood", ["transport", "container", "flammable", "wettable", "floating"], {"capacity": 8})
    cloth = ("布卷", "cloth", ["transport", "flammable", "wettable"], {"kind": "raw_material"})
    bucket = ("溪边木桶", "wood", ["transport", "container", "flammable", "wettable", "floating"],
              {"capacity": 4, "water_capacity": 12})
    stone = ("散落卵石", "stone", ["transport", "wettable"], {"kind": "raw_material"})
    clutter = {"common_bank": [log, crate, bowl, cloth], "willow_grove": [log, crate],
               "reed_marsh": [bucket, cloth], "stone_ridge": [bowl, stone],
               "open_bank": [crate, bowl, cloth]}[style]
    places = [(4, 4), (5, 5), (2, 6), (2, 7)] if style == "common_bank" else land[len(resources):]
    for i, (name, material, caps, state) in enumerate(clutter):
        if i >= len(places):
            break
        ox, oy = places[i]
        spawn_entity(ctx, {"id": f"seed_{cx}_{cy}_{ox}_{oy}", "name": name, "material": material,
                          "capabilities": caps, "state": state, "x": cx*16+ox, "y": cy*16+oy,
                          "source": "terrain"})
    # Water is a finite source for each materialized chunk, not infinite inventory.
    water_tiles = [(cx*16+x, cy*16+y) for y in range(16) for x in range(16)
                   if m.terrain(cx*16+x, cy*16+y) == "water"]
    if water_tiles:
        x, y = water_tiles[min(1, len(water_tiles)-1)]
        spawn_entity(ctx, {"id": f"water_{cx}_{cy}", "name": "溪水", "x": x, "y": y,
                          "material": "stone", "capabilities": ["water_source", "wettable"],
                          "state": {"water_remaining": 200, "water_source_capacity": 200}, "source": "terrain"})
    return m.get_chunk(ctx, cx, cy)


def spawn_entity(ctx, entity):
    entity = dict(entity)
    entity.setdefault("id", m.identifier("object"))
    if m.get_entity(ctx, entity["id"], required=False):
        raise RuleViolation("物件标识已经存在。")
    entity.setdefault("created_at", ctx.now)
    value = m.normalize_entity(entity)
    registry = registry_for(value)
    pool = getattr(ctx, "_grove_material_allocation", None)
    if value.get("source") == "agent":
        if pool is None:
            raise RuleViolation("Agent物件必须从真实区域材料转移而来。")
        needed = {material: sm.quantity(value, material) for material in registry.materials}
        if any(needed[k] > pool.get(k, 0) for k in needed):
            raise RuleViolation("生成物件的实际物质超过已投入库存。")
        for material, amount in needed.items():
            pool[material] = pool.get(material, 0)-amount
    else:
        for material in registry.materials:
            amount = sm.quantity(value, material)
            if amount:
                record_account(ctx, "sources", material, amount, "seed")
    if m.is_ground(value) and m.is_safe(ctx, value["x"], value["y"]) and ("blocking" in value["capabilities"] or value["state"]["burning"]):
        raise RuleViolation("出生安全区不能放障碍或火源。")
    if value.get("physical_parent"):
        parent = m.get_entity(ctx, value["physical_parent"]["id"])
        if "container" not in parent["capabilities"] or parent["state"]["destroyed"]:
            raise RuleViolation("目标不是可用容器。")
        ancestor = parent
        for _ in range(8):
            if ancestor["state"]["burning"]:
                raise RuleViolation("燃烧容器及其内部容器不能添加内容。")
            if not ancestor.get("physical_parent"):
                break
            ancestor = m.get_entity(ctx,ancestor["physical_parent"]["id"])
        else:
            raise RuleViolation("容器层数超过限制。")
        if value["owner"] != parent["owner"]:
            raise RuleViolation("容器与内容的归属必须一致。")
        relation = value["physical_parent"]["relation"]
        result = save_entity(ctx, value, expected_version=0)
        parent["state"]["contents" if relation == "contained" else "attachments"].append(value["id"])
        save_entity(ctx, parent)
        return m.get_entity(ctx, result["id"])
    return save_entity(ctx, value, expected_version=0)


def relocate_tree(ctx, entity, *, owner, container=None, x=None, y=None):
    tree = [entity, *m.descendants(ctx, entity)]
    old_owner = entity.get("owner")
    if old_owner:
        player = m.get_player(ctx, old_owner)
        player["inventory"] = [eid for eid in player["inventory"] if eid not in {e["id"] for e in tree}]
        m.save_player(ctx, player)
    if owner:
        player = m.get_player(ctx, owner)
        ids = [e["id"] for e in tree]
        if len(set(player["inventory"] + ids)) > m.MAX_INVENTORY:
            raise RuleViolation("背包物件过多。")
        player["inventory"] = list(dict.fromkeys(player["inventory"] + ids))
        m.save_player(ctx, player)
    for child in tree[1:]:
        child["owner"] = owner
        save_entity(ctx, child)
    entity.update(owner=owner, container=container, physical_parent={"id": container, "relation": "contained"} if container else None, x=x, y=y)
    return save_entity(ctx, entity)


def remove_from_container(ctx, entity):
    if entity.get("physical_parent"):
        relation = entity["physical_parent"]
        parent = m.get_entity(ctx, relation["id"])
        field = "contents" if relation["relation"] == "contained" else "attachments"
        parent["state"][field].remove(entity["id"])
        save_entity(ctx, parent)


def retire_material(ctx, entity, building_id):
    if (entity["state"]["destroyed"] or entity["state"].get("retired") or entity["state"]["contents"]
            or entity["state"]["burning"] or "personal" in entity["capabilities"]):
        raise RuleViolation("只能投入未燃烧、未退役的单份可用材料。")
    if sm.quantity(entity):
        raise RuleViolation("原料的实际物质须先转入施工或燃料槽，不能复制退役。")
    remove_from_container(ctx, entity)
    if entity.get("owner"):
        player = m.get_player(ctx, entity["owner"])
        player["inventory"] = [eid for eid in player["inventory"] if eid != entity["id"]]
        m.save_player(ctx, player)
    entity.update(container=None, physical_parent=None, x=None, y=None)
    entity["state"].update(retired=True, retired_into=building_id)
    if entity["state"].get("generation_region"):
        entity["state"]["generation_allocation_retired"] = True
    return save_entity(ctx, entity)


def restore_material(ctx, entity, *, owner=None, container=None, x=None, y=None):
    if not entity["state"].get("retired") or not sm.quantity(entity, slot="body"):
        raise RuleViolation("原材料不能重复返还。")
    entity["lifecycle"] = "intact"
    if container:
        parent = m.get_entity(ctx, container)
        if (parent["owner"] != owner or "container" not in parent["capabilities"]
                or not parent["state"]["open"] or parent["state"]["destroyed"]
                or parent["state"]["burning"] or len(parent["state"]["contents"]) >= parent["state"]["capacity"]):
            raise RuleViolation("返还容器不可用或容量不足。")
    entity["state"].update(retired=False, retired_into=None)
    result = relocate_tree(ctx, entity, owner=owner, container=container, x=x, y=y)
    if container:
        parent = m.get_entity(ctx, container)
        parent["state"]["contents"].append(entity["id"])
        save_entity(ctx, parent)
    return result


def consume_resources(ctx, cx, cy, cost):
    """Spend current public node stock in the caller's publication transaction."""
    stock = m.resource_stock(ctx, cx, cy)
    if any(type(amount) is not int or amount < 0 or stock.get(kind, 0) < amount for kind, amount in cost.items()):
        raise StateConflict("本区域真实公共材料或水库存不足；不能凭空发表赠物。")
    nodes, sources = m.region_resources(ctx, cx, cy)
    consumed = []
    pool = getattr(ctx, "_grove_material_allocation", None)
    if pool is None:
        pool = ctx._grove_material_allocation = {}
    for kind, total in cost.items():
        remaining = total
        for source in sources if kind == "water" else nodes:
            if kind != "water" and source["state"]["resource_kind"] != kind:
                continue
            field = "water_remaining" if kind == "water" else "stock"
            amount = min(remaining, source["state"][field])
            if amount:
                units = amount if kind == "water" else amount*PIECE_QUANTITIES[kind]
                try:
                    sm.take_material(source, kind, units, include_burning=True, remove_empty=False)
                except ValueError as exc:
                    raise StateConflict("资源节点的真实物质已经变化。") from exc
                pool[kind] = pool.get(kind, 0)+units
                save_entity(ctx, source)
                consumed.append({"id": source["id"], "resource_kind": kind, "amount": amount, "version": source["version"]})
                remaining -= amount
            if not remaining:
                break
    return consumed


def finish_resource_allocation(ctx):
    if any(getattr(ctx, "_grove_material_allocation", {}).values()):
        raise RuleViolation("生成提交未完全转移投入的真实材料。")
    ctx._grove_material_allocation = None


def record_account(ctx, category, material, amount, reason):
    if not amount:
        return
    if category not in {"sources", "sinks"} or type(amount) is not int or not 0 < amount <= 10**18:
        raise RuleViolation("物质账分类或数量无效。")
    if "#" in material:
        if reason not in m.CREATIVE_ACCOUNT_REASONS:
            raise RuleViolation("注册材料的物质账必须使用已声明的固定原因。")
        key = hashlib.sha256("\0".join((category, material, reason)).encode()).hexdigest()
        record = ctx.get_state_record(m.CREATIVE_ACCOUNTING, key)
        expected = {"category": category, "material": material, "reason": reason}
        previous_quantity = 0
        if record["exists"]:
            previous = record["value"]
            if (not isinstance(previous, dict) or set(previous) != set(expected) | {"quantity"}
                    or any(previous[field] != value for field, value in expected.items())
                    or type(previous["quantity"]) is not int or not 0 <= previous["quantity"] <= 10**18):
                raise RuleViolation("注册材料的实际物质账无效。")
            previous_quantity = previous["quantity"]
        quantities = {"quantity": previous_quantity}
        try:
            sm.accumulate_quantity(quantities, "quantity", amount)
        except ValueError as exc:
            raise RuleViolation("物质账的有界计数已满。") from exc
        ctx.set_state(m.CREATIVE_ACCOUNTING, key, {**expected, **quantities}, expected_version=record["version"])
        return
    account = activity.meta_get(ctx, "matter_accounting", {"unit": "u", "rules_version": RULES_VERSION, "sources": {}, "sinks": {}})
    key = material+":"+reason
    values = account[category]
    try:
        sm.accumulate_quantity(values, key, amount)
    except ValueError as exc:
        raise RuleViolation("物质账的有界计数已满。") from exc
    if len(values) > 32:
        raise RuleViolation("物质账的有界计数已满。")
    activity.meta_set(ctx, "matter_accounting", account)


def apply_matter(ctx, objects, effects, *, sources=(), sinks=()):
    from .contact import bind_incoming
    try:
        frame = Frame(registry_for(objects), {obj["id"]: obj for obj in objects},
                      bind_material=lambda entity, definition: bind_incoming(ctx, entity, definition), mixture_capacity=mixture_capacity)
        frame.apply(effects, allowed_sources=sources, allowed_sinks=sinks)
    except ValueError as exc:
        raise RuleViolation(str(exc)) from exc
    commit_frame(ctx, frame)
    for obj in objects:
        if obj["id"] in frame.dirty:
            obj.update(m.get_entity(ctx, obj["id"]))
    return frame


def transfer_material(ctx, source, target, lot_id, amount, *, target_slot="contained", bound_to=None, reason="transfer"):
    source = m.get_entity(ctx, source) if isinstance(source, str) else source
    target = m.get_entity(ctx, target) if isinstance(target, str) else target
    if type(amount) is not int or amount <= 0 or target_slot not in {"body", "contained", "surface", "absorbed"}:
        raise RuleViolation("物质转移参数无效。")
    lot = next((lot for lot in source["matter"] if lot["id"] == lot_id), None)
    if not lot or lot["quantity"] < amount:
        raise RuleViolation("来源物质批次余额不足。")
    if source["id"] == target["id"] and lot["slot"] == target_slot:
        raise RuleViolation("物质已位于这个槽。")
    if target_slot == "absorbed" and lot["material"] != "water":
        raise RuleViolation("吸附槽只支持已注册的水吸附机制。")
    apply_matter(ctx, [source] if source["id"] == target["id"] else [source, target],
                  [Transfer(source["id"], lot_id, target["id"], amount, target_slot, bound_to)])
    matches = [current for current in target["matter"] if current["material"] == lot["material"] and current["slot"] == target_slot
               and current.get("bound_to") == bound_to and (target_slot != "body" or current.get("origin_object") == lot.get("origin_object", source["id"]))]
    return {"transferred": amount, "quantity": amount, "source": source["id"], "target": target["id"],
            "lot_id": matches[-1]["id"] if matches else None, "source_version": source["version"], "target_version": target["version"]}


def transfer_water(ctx, source, target, amount, *, target_slot="contained", reason="pour"):
    source = m.get_entity(ctx, source) if isinstance(source, str) else source
    target = m.get_entity(ctx, target) if isinstance(target, str) else target
    if type(amount) is not int or amount <= 0:
        raise RuleViolation("转水量必须是正整数u。")
    lots = [lot for lot in source["matter"] if lot["material"] == "water" and lot["slot"] == "contained"]
    available = sum(lot["quantity"] for lot in lots)
    room = m.water_transfer_room(target, target_slot)
    current_limits = {"water_transfer": {"source_id": source["id"], "target_id": target["id"],
        "target_slot": target_slot, "requested": amount, "source_remaining": available,
        "target_remaining_capacity": room, "maximum": min(available, room), "unit": "u",
        "meaning": "Current rejection facts only; no amount was changed or water moved."}}
    if available < amount:
        raise RuleViolation("真实储水余额不足。", details=current_limits)
    if amount > room:
        raise RuleViolation("目标真实储水或吸水容量不足。", details=current_limits)
    effects, remaining = [], amount
    targets = [[bound, available] for bound, available in sm.absorption_targets(target, m._tree_objects(ctx, target))] if target_slot == "absorbed" else [[None, amount]]
    if sum(available for _, available in targets) < amount:
        raise RuleViolation("当前暴露的吸水材料容量不足。")
    for lot in lots:
        available = lot["quantity"]
        for destination in targets:
            take = min(remaining, available, destination[1])
            if take:
                effects.append(Transfer(source["id"], lot["id"], target["id"], take, target_slot, destination[0]))
                remaining -= take
                available -= take
                destination[1] -= take
    apply_matter(ctx, [source] if source["id"] == target["id"] else [source, target], effects)
    if target_slot in {"absorbed", "surface"}:
        extinguish(ctx, target)
    return {"transferred": amount, "amount": amount, "source": source["id"], "target": target["id"],
            "source_id": source["id"], "target_id": target["id"], "source_version": source["version"], "target_version": target["version"]}


def add_water_from_source(ctx, entity, amount, source):
    return transfer_water(ctx, source, entity, amount, reason="fill")


def contact_water(ctx, target, amount, *, source=None, reason="contact"):
    target = m.get_entity(ctx, target) if isinstance(target, str) else target
    amount = min(amount, sum(available for _, available in sm.absorption_targets(target, m._tree_objects(ctx, target))))
    if amount <= 0:
        return {"transferred": 0, "target": target["id"]}
    if source is None:
        x, y = m.entity_location(ctx, target)
        if x is None or m.tile_at(ctx, x, y) != "water":
            return {"transferred": 0, "target": target["id"]}
        _, sources = m.region_resources(ctx, *m.chunk_for(x, y))
        source = next((obj for obj in sources if obj["state"]["water_remaining"]), None)
        if source is None:
            return {"transferred": 0, "target": target["id"]}
    source = m.get_entity(ctx, source) if isinstance(source, str) else source
    amount = min(amount, sm.quantity(source, "water", "contained"))
    return transfer_water(ctx, source, target, amount, target_slot="absorbed", reason=reason) if amount else {"transferred": 0, "target": target["id"]}


def consume_water(ctx, entity, amount, reason="tend"):
    entity = m.get_entity(ctx, entity) if isinstance(entity, str) else entity
    if type(amount) is not int or amount <= 0 or sm.quantity(entity, "water", "contained") < amount:
        raise RuleViolation("真实储水量不足。")
    remaining, effects = amount, []
    for lot in entity["matter"]:
        if lot["material"] == "water" and lot["slot"] == "contained":
            take = min(remaining, lot["quantity"])
            if take:
                effects.append(Sink(entity["id"], lot["id"], take, reason))
                remaining -= take
    apply_matter(ctx, [entity], effects, sinks={reason})
    return {"consumed": amount, "reason": reason, "entity_id": entity["id"]}


def start_burning(ctx, entity, lot_id=None):
    entity = m.get_entity(ctx, entity) if isinstance(entity, str) else entity
    objects = m._tree_objects(ctx, entity)
    try:
        registry = registry_for(objects)
    except ValueError as exc:
        raise RuleViolation(str(exc)) from exc
    if sm.quantity(entity, "water", "surface") or not activity.can_ignite(ctx, entity):
        raise RuleViolation("当前湿度、保护或物理预算不允许点燃。")
    selected = [lot for lot in entity["matter"] if (lot_id is None or lot["id"] == lot_id) and sm.ignitable_lot(registry, entity, lot, objects)]
    if not selected:
        raise RuleViolation("没有可暴露的可燃物质批次。")
    for lot in selected:
        lot["burning"] = True
    from .definitions import fuel_lots
    qualified = {lot["id"] for lot in fuel_lots(ctx, entity, "hearth_fuel", include_burning=True)}
    if "heater" in entity["components"] and any(lot["slot"] == "contained" and lot["id"] in qualified for lot in selected):
        entity["components"]["heater"]["lit"] = True
    return save_entity(ctx, entity)


def extinguish(ctx, entity):
    entity = m.get_entity(ctx, entity) if isinstance(entity, str) else entity
    for lot in entity["matter"]:
        lot["burning"] = False
    if "heater" in entity["components"]:
        entity["components"]["heater"]["lit"] = False
    return save_entity(ctx, entity)


def _parent_relation(ctx, parent, child, relation):
    if child["id"] == parent["id"] or parent["id"] in {entity["id"] for entity in m.descendants(ctx, child)}:
        raise RuleViolation("物理父关系不能循环。")
    # Count the receiving root's complete forest, not only a nested container.
    # Moving its existing identities keeps their slots; an outside subtree adds
    # each identity once, matching the read-only hypothetical relation preview.
    parent_tree = m._tree_objects(ctx, parent)
    root_id = next(eid for eid, entity in parent_tree.items() if not entity.get("physical_parent"))
    final_descendants = ((set(parent_tree)-{root_id})
                         | {entity["id"] for entity in m.descendants(ctx, child)} | {child["id"]})
    if len(final_descendants) > sm.MAX_DESCENDANTS:
        raise RuleViolation("物理树最多16个后代。")
    if relation == "attached":
        result = m.can_attach(ctx, child, parent)
        if not result["allowed"]:
            raise RuleViolation(result["reason"])
    elif "container" not in parent["components"] or not parent["components"]["container"]["open"] or len(parent["state"]["contents"]) >= parent["components"]["container"]["slots"]:
        raise RuleViolation("目标容器关闭、破损或物件槽已满。")
    for ancestor in m._tree_objects(ctx, parent).values():
        if ancestor["state"]["burning"]:
            raise RuleViolation("燃烧容器及其内部不能装卸。")
    remove_from_container(ctx, child)
    root = parent
    while root.get("physical_parent"):
        root = m.get_entity(ctx, root["physical_parent"]["id"])
    relocate_tree(ctx, child, owner=root.get("owner"), container=parent["id"])
    child["physical_parent"] = {"id": parent["id"], "relation": relation}
    child["x"] = child["y"] = None
    parent = m.get_entity(ctx, parent["id"])
    parent["state"]["contents" if relation == "contained" else "attachments"].append(child["id"])
    save_entity(ctx, child)
    save_entity(ctx, parent)
    return child


def contain(ctx, parent, child):
    return _parent_relation(ctx, parent, child, "contained")


def attach(ctx, parent, child):
    return _parent_relation(ctx, parent, child, "attached")


def detach(ctx, child, *, owner=None, container=None, x=None, y=None):
    if x is None and owner is None and container is None:
        x, y = m.entity_location(ctx, child)
        owner = child.get("owner")
    remove_from_container(ctx, child)
    if container:
        return contain(ctx, m.get_entity(ctx, container), child)
    return relocate_tree(ctx, child, owner=owner, x=None if owner else x, y=None if owner else y)


def record_change(ctx, summary, *, x, y, entity_id=None, action=None):
    changes = activity.meta_get(ctx, "changes", [])
    changes.append({"summary": summary[:180], "x": x, "y": y, "entity_id": entity_id,
                    "action": action, "at": ctx.now})
    activity.meta_set(ctx, "changes", changes[-8:])
    public_event(ctx, action=action, entity_id=entity_id, x=x, y=y)


def physical_contract_failure(ctx, entity, *, tick=None):
    """Reconcile a real broken body's surviving escrow in its physical commit.

    Loss records an unavailable contract claim, not a new material sink. Existing
    physical rules already account for burnt, spilled or otherwise moved lots.
    Completion preserves beneficiary rights; unfinished terms cannot complete
    after the supporting object has failed. Surface lots remain recoverable.
    """
    state = entity.get("state", {}).get("program", {}).get("contract")
    if not state or entity.get("lifecycle") not in {"broken", "consumed"}:
        return False
    previous = deepcopy(state)
    current_tick = ctx.get_state(m.META, "clock", {"sequence": 0})["sequence"] if tick is None else tick
    state.setdefault("physical_failure_tick", current_tick)
    if state["status"] != "complete":
        state["status"] = "failed"
        state.setdefault("finished_tick", current_tick)
        state["failure_reason"] = "physical_damage"
    outstanding = max(0, state["delivered"] - state.get("released", 0) - state.get("lost", 0))
    surviving = sum(lot["quantity"] for lot in entity.get("matter", [])
                    if lot["material"] == state["material"] and lot["slot"] in {"contained", "surface"})
    state["lost"] = state.get("lost", 0) + max(0, outstanding - surviving)
    state["escrow_remaining"] = min(outstanding, surviving)
    return state != previous


def public_event(ctx, **fact):
    for entity in events.record_event(ctx, **fact) or ():
        save_entity(ctx, entity)


@contextmanager
def candidate_batch(ctx):
    """Own one candidate view: flush on success, discard it on every exit.

    This is not a transaction. Runtime still owns atomic state, effects,
    receipts and rollback. Reducers join this view but never close it; a failed
    normalization, capacity check, event admission or CAS escapes to Runtime.
    """
    if getattr(ctx, "_grove_batch", None) is not None:
        raise RuntimeError("A Grove candidate batch already has an owner.")
    batch = m.CandidateBatch()
    ctx._grove_batch = batch
    try:
        yield batch
        _flush_batch(ctx, batch)
    finally:
        del ctx._grove_batch


def _flush_batch(ctx, batch):
    """Write this owner's admitted state and public facts through existing CAS."""
    if (len(batch.dirty_processes) > activity.MAX_PROCESSES
            or len(batch.dirty_meta) > activity.MAX_FRAME_META):
        raise RuleViolation("过程及索引写入超过预留记录预算。")
    from .contact import prepare_materials
    for eid in batch.dirty_entities:
        entity = batch.entities[eid]
        prepare_materials(ctx, entity)
        version = entity.get("version")
        batch.entities[eid] = m.normalize_entity(entity)
        m.project_world_uses(ctx, batch.entities[eid])
        if version is not None:
            batch.entities[eid]["version"] = version
    if batch.contact_admission:
        from .creative import _check_instance_capacity
        _check_instance_capacity(ctx, activity.meta_get(ctx, "creative_instances", []))
    m.write_candidates(ctx, batch)
    for pid in sorted(batch.dirty_processes):
        process = batch.processes[pid]
        value = {k:v for k,v in process.items() if k != "version"}
        if sm.encoded_size(value) > activity.MAX_PROCESS_BYTES:
            raise RuleViolation("过程记录超过准入预算。")
        ctx.set_state(m.PROCESSES, pid, value, expected_version=batch.process_versions[pid])
    for key in sorted(batch.dirty_meta):
        ctx.set_state(m.META, key, batch.meta[key])
    if batch.event_journal is not None:
        events._save(ctx, m.META, "creative_events", batch.event_journal)
    for eid, record in sorted(batch.records.items()):
        events._save(ctx, events.RECORDS, eid, record)


def commit_results(ctx, objects, dirty, *, accounts=None, spills=None, chunks=(), tick=None):
    """Commit one trusted candidate set; reducers still own mechanism/order policy.

    Seed the complete read view before reconciling any member, then normalize
    and index each dirty object once. Broken/consumed contract claims, consumed
    inventory membership, finite spilled water and accounts are common effects
    of action and tick results, never separately reimplemented by those runners.
    """
    current = getattr(ctx, "_grove_batch", None)
    # Ordinary actions own their small batch; physics and authored rules join
    # their logical step's existing owner. Only that owner flushes or discards.
    with (candidate_batch(ctx) if current is None else nullcontext(current)) as batch:
        candidates = deepcopy(objects)
        changed = set(dirty)
        for eid, entity in candidates.items():
            if physical_contract_failure(ctx, entity, tick=tick):
                changed.add(eid)
        batch.entities.update(candidates)
        batch.dirty_entities.update(changed)
        for eid in sorted(changed):
            entity = candidates[eid]
            save_entity(ctx, entity)
            if entity["lifecycle"] == "consumed" and entity.get("owner"):
                player = m.get_player(ctx, entity["owner"])
                if eid in player["inventory"]:
                    player["inventory"].remove(eid)
                    m.save_player(ctx, player)
        if spills is not None:
            covered = set(chunks)
            indexed = set(activity.meta_get(ctx, "spill_chunks", []))-covered
            # Partition this frame's final reservoirs once, rather than
            # parsing every spilled coordinate again for each covered chunk.
            by_point = {}
            if covered:
                for key, amount in spills.items():
                    if amount:
                        point = m.chunk_for(*map(int, key.split(",")))
                        by_point.setdefault(point, {})[key] = amount
            for cid in sorted(covered):
                _, cx, cy = cid.split("_")
                point = int(cx), int(cy)
                chunk = m.get_chunk(ctx, *point)
                held = by_point.get(point, {})
                if held != chunk.get("water_reservoirs", {}):
                    chunk["water_reservoirs"] = held
                    m.save_chunk(ctx, chunk)
                if held:
                    indexed.add(cid)
            activity.meta_set(ctx, "spill_chunks", sorted(indexed))
        for (category, material, reason), amount in sorted((accounts or {}).items()):
            record_account(ctx, category, material, amount, reason)
        return changed


def commit_frame(ctx, frame, *, chunks=(), tick=None):
    """Frame adapter into the same candidate/ledger/lifecycle submission."""
    try:
        frame.verify_balance()
    except ValueError as exc:
        raise RuleViolation(str(exc)) from exc
    accounts = {}
    for category, values in (("sources", frame.sources), ("sinks", frame.sinks)):
        for (material, reason), amount in values.items():
            sm.accumulate_quantity(accounts, (category, material, reason), amount)
    changed = commit_results(ctx, frame.objects, frame.dirty, accounts=accounts,
                             spills=frame.spills if chunks else None, chunks=chunks, tick=tick)
    frame.dirty.update(changed)
    return changed
