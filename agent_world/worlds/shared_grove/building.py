"""Costed construction on the world's common material and relation model."""
from __future__ import annotations

from copy import deepcopy

from agent_world.errors import RuleViolation, StateConflict

from . import model as m
from .definitions import PIECE_QUANTITIES, REGISTRY


MAX_HEARTHS = 16
HEARTH_FUEL_CAPACITY = 200
MATERIAL_UNITS = {key: amount for key, amount in PIECE_QUANTITIES.items() if key != "water"}
RECIPES = {
    "walkway": {"id": "walkway", "name": "干脚通路", "cost": {"wood": 2}, "terrain": ["water"], "radius": 0,
                "use": "在浅溪上支撑通行；结构仍会受湿、燃烧和载荷影响。",
                "material": "wood", "capabilities": ["walkway", "wettable", "flammable"],
                "components": {"structure": {"max_integrity": 100, "integrity": 100, "support_limit": 240, "overload_rate": 1},
                               "absorbent": {"capacity": 24, "rate": 2}, "combustion": {"rate": 2, "spread": True}}},
    "canopy": {"id": "canopy", "name": "雨棚", "cost": {"wood": 2, "cloth": 1}, "terrain": ["grass", "dirt"], "radius": 1,
               "use": "遮雨或把实际降雨收入40u水容量；载水会增加结构负担。",
               "material": "wood", "capabilities": ["canopy", "container", "wettable", "flammable"],
               "components": {"container": {"slots": 0, "liquid_capacity": 40, "open": True},
                              "structure": {"max_integrity": 100, "integrity": 100, "support_limit": 240, "overload_rate": 1},
                              "absorbent": {"capacity": 24, "rate": 2}, "collector": {"capacity": 40, "mode": "shelter", "radius": 1},
                              "combustion": {"rate": 2, "spread": True}}},
    "hearth": {"id": "hearth", "name": "烘干炉", "cost": {"stone": 2}, "terrain": ["grass", "dirt"], "radius": 1,
               "use": "装入真实木料后点火；每秒烧2u木料，附近物件加快蒸发，不额外造燃料。",
               "material": "stone", "capabilities": ["hearth", "container", "wettable"],
               "components": {"container": {"slots": 0, "liquid_capacity": 0, "open": True},
                              "structure": {"max_integrity": 100, "integrity": 100, "support_limit": 480, "overload_rate": 1},
                              "absorbent": {"capacity": 20, "rate": 2}, "heater": {"rate": 6, "radius": 1, "lit": False},
                              "combustion": {"rate": 2, "spread": False}}},
}


def recipes():
    result = deepcopy(list(RECIPES.values()))
    for recipe in result:
        recipe["material_cost_u"] = {key: value*MATERIAL_UNITS[key] for key, value in recipe["cost"].items()}
        recipe["unit"] = "logical_matter"
    return result


def is_structure(entity):
    return entity["state"].get("recipe") in RECIPES


def _path_reason(ctx, player, entity):
    if entity["state"].get("retired") or entity.get("lifecycle") == "consumed":
        return "物件已耗尽或已投入其他用途。"
    if entity["owner"] not in {None, player["id"]}:
        return "不能使用别人的物件。"
    cursor, seen = entity, set()
    for _ in range(17):
        if cursor["id"] in seen:
            return "物理关系不能循环。"
        seen.add(cursor["id"])
        if cursor["state"]["burning"]:
            return "燃烧中的物件及其内容不能搬运或取用。"
        parent = cursor.get("physical_parent")
        if not parent:
            break
        cursor = m.get_entity(ctx, parent["id"])
        if parent["relation"] == "contained" and not cursor["state"]["open"]:
            return "请先打开盛放物件的容器。"
    else:
        return "物理关系层数超过限制。"
    x, y = m.entity_location(ctx, entity)
    if x is None or (entity["owner"] is None and abs(x-player["x"])+abs(y-player["y"]) > 1):
        return "要先走到目标相邻的一格。"
    return None


def _body_lots(entity):
    return [lot for lot in entity.get("matter", []) if lot["slot"] == "body" and lot["quantity"] > 0]


def material_status(ctx, player, entity):
    caps = set(entity["capabilities"])
    reason = _path_reason(ctx, player, entity)
    if not reason and entity["owner"] != player["id"]:
        reason = "请先把原材料拿到自己的背包。"
    if not reason and ("transport" not in caps or caps & {"container", "ignition", "personal"}
                       or entity["state"]["contents"] or entity["state"]["water"]
                       or entity.get("components", {}).get("attachment") or is_structure(entity)
                       or entity["state"].get("kind") == "resource_node"):
        reason = "需使用裸材料，容器、组合部件和私人物件不能投料。"
    if not reason and entity["material"] != "stone" and entity["state"]["wetness"] >= 35:
        reason = "木料和布料须先晾干或烘干到湿度35以下。"
    quantity = sum(lot["quantity"] for lot in _body_lots(entity) if lot["material"] == entity["material"])
    if not reason and quantity <= 0:
        reason = "材料的可用部分已经耗尽。"
    return {"eligible": reason is None, "reason": reason, "material": entity["material"],
            "quantity_u": quantity, "unit": "logical_matter", "units_per_piece": MATERIAL_UNITS.get(entity["material"])}


def _backpack_reason(ctx, player, count, *, tree_count=None):
    backpack = m.get_entity(ctx, player["backpack_id"])
    reason = _path_reason(ctx, player, backpack)
    if reason:
        return reason
    if not backpack["state"]["open"]:
        return "请先打开自己的背包。"
    if len(backpack["state"]["contents"])+count > backpack["state"]["capacity"]:
        return "背包容量不足，请先腾出位置。"
    added = count if tree_count is None else tree_count
    if len(m.descendants(ctx, backpack))+added > 16:
        return "背包的容纳组合最多16个物件，请先取出或放下部分内容。"
    if len(player["inventory"])+added > m.MAX_INVENTORY:
        return "背包物件过多。"
    return None


def _owned_available(ctx, player, predicate):
    key = (player["id"], player["version"])
    cached = getattr(ctx, "_grove_owned_candidates", None)
    if cached is None or cached[0] != key:
        values = [m.get_entity(ctx, eid) for eid in player["inventory"]]
        ctx._grove_owned_candidates = (key, values)
    else:
        values = cached[1]
    return [entity for entity in values if not _path_reason(ctx, player, entity) and predicate(entity)]


def _water_items(ctx, player, minimum=1):
    return _owned_available(ctx, player, lambda e: "container" in e["capabilities"]
                            and "personal" not in e["capabilities"] and e["state"]["open"]
                            and e["state"]["water"] >= minimum)


def _hearth_ids(ctx):
    return list(ctx.get_state(m.META, "active_hearths", []))


def _live_hearths(ctx):
    return [e for eid in _hearth_ids(ctx) if (e := m.get_entity(ctx, eid, required=False))
            and m.is_ground(e) and "heater" in e.get("components", {}) and e.get("lifecycle") == "intact"]


def _set_hearths(ctx, ids):
    ids = list(dict.fromkeys(ids))
    if ids != _hearth_ids(ctx):
        ctx.set_state(m.META, "active_hearths", ids)


def _choose_materials(ctx, player, recipe, selected):
    required = {key: value*MATERIAL_UNITS[key] for key, value in recipe["cost"].items()}
    if selected is not None and len(set(selected)) != len(selected):
        raise RuleViolation("所选原材料不能重复。")
    candidates = [m.get_entity(ctx, eid) for eid in (selected if selected is not None else player["inventory"])]
    if selected is not None:
        for entity in candidates:
            status = material_status(ctx, player, entity)
            if not status["eligible"]:
                raise RuleViolation(status["reason"])
            if entity["material"] not in required:
                raise RuleViolation("所选原料包含配方不需要的材质。")
    result = []
    for material, amount in required.items():
        remaining = amount
        lots = [(entity["id"], lot) for entity in candidates if entity["material"] == material
                and material_status(ctx, player, entity)["eligible"] for lot in _body_lots(entity)
                if lot["material"] == material]
        # Largest real lots first minimizes the bounded investment record;
        # small scraps must not hide an otherwise legal eight-lot build.
        lots.sort(key=lambda item: (-item[1]["quantity"], item[0], item[1]["id"]))
        for entity_id, lot in lots:
            quantity = min(remaining, lot["quantity"])
            if quantity:
                result.append((entity_id, lot["id"], quantity))
                remaining -= quantity
            if remaining == 0:
                break
        if remaining:
            raise RuleViolation(f"缺少{remaining}u可用{material}原材料，配方需要{amount}u。")
    if len(result)>8:
        raise RuleViolation("所需材料过于分散；同一建筑最多接受8个真实批次。")
    return result


def build(ctx, player, args):
    recipe = RECIPES[args["recipe"]]
    x, y = args["x"], args["y"]
    if abs(x-player["x"])+abs(y-player["y"]) > 1:
        raise RuleViolation("只能在自己所在或相邻一格建设。")
    m.ensure_chunk(ctx, *m.chunk_for(x, y))
    if m.tile_at(ctx, x, y) not in recipe["terrain"]:
        raise RuleViolation("这一格地形不适合所选配方。")
    ground = m.public_entities_near(ctx, x, y, radius=0, limit=m.MAX_CHUNK_ENTITIES)
    if any(is_structure(e) or e["state"].get("kind") == "resource_node" for e in ground):
        raise RuleViolation("这一格已有建筑或资源节点。")
    if m.blocking_at(ctx, x, y):
        raise RuleViolation("请先移开这一格的障碍。")
    if "heater" in recipe["components"] and m.is_safe(ctx, x, y):
        raise RuleViolation("出生安全区不能建设火炉。")
    hearths = _live_hearths(ctx)
    if "heater" in recipe["components"] and len(hearths) >= MAX_HEARTHS:
        raise RuleViolation("世界最多保留16座可用烘干炉，请先拆除不用的炉。")
    materials = _choose_materials(ctx, player, recipe, args.get("material_ids"))
    region = f"{x//m.CHUNK_SIZE}:{y//m.CHUNK_SIZE}"
    content_region = next((m.get_entity(ctx,source_id)["state"].get("generation_region")
                           for source_id,_,_ in materials if m.get_entity(ctx,source_id)["state"].get("generation_region") == region),None)
    bid = m.identifier("building")
    entity = m.spawn_entity(ctx, {"id": bid, "name": recipe["name"], "material": recipe["material"],
        "capabilities": recipe["capabilities"], "components": deepcopy(recipe["components"]), "matter": [],
        "x": x, "y": y, "created_by": player["id"], "source": "construction",
        "state": {"recipe": args["recipe"], "invested_materials": []}})
    investments = {}
    for source_id, lot_id, quantity in materials:
        source = m.get_entity(ctx, source_id)
        target = m.get_entity(ctx, bid)
        before = {lot["id"]: lot["quantity"] for lot in target["matter"]}
        m.transfer_material(ctx, source, target, lot_id, quantity, target_slot="body", reason="construction")
        target = m.get_entity(ctx, bid)
        transferred = [lot["id"] for lot in target["matter"] if lot["quantity"] > before.get(lot["id"], 0)]
        entry = investments.setdefault(source_id, {"id": source_id, "material": source["material"], "quantity": 0, "lot_ids": []})
        entry["quantity"] += quantity
        entry["lot_ids"] = list(dict.fromkeys(entry["lot_ids"]+transferred))
    for source_id in investments:
        source = m.get_entity(ctx, source_id)
        if not any(lot["quantity"] for lot in source["matter"]):
            m.retire_material(ctx, source, bid)
    entity = m.get_entity(ctx, bid)
    entity["state"]["invested_materials"] = list(investments.values())
    entity["state"]["initial_structural_quantity"] = sum(lot["quantity"] for lot in entity["matter"] if lot["role"] == "structural")
    if content_region:
        entity["state"]["generation_region"] = content_region
    _bind_absorption(entity)
    m.save_entity(ctx, entity)
    if m.load_summary(ctx, entity)["overload"]:
        raise RuleViolation("所选原材料超过建筑的支撑载荷。")
    if "heater" in recipe["components"]:
        _set_hearths(ctx, [e["id"] for e in hearths]+[bid])
    return entity, f"搭建了{recipe['name']}，投入真实材料；剩余原料保留在背包。"


def _refund_materials(ctx, target):
    """Partition each remaining body lot once, capped by actual investments."""
    result = []
    body = {lot["id"]: lot for lot in target["matter"] if lot["slot"] == "body" and lot["quantity"] > 0}
    remaining = {lot_id: lot["quantity"] for lot_id, lot in body.items()}
    for invested in target["state"].get("invested_materials", []):
        allowance, lots = invested["quantity"], []
        for lot_id in invested["lot_ids"]:
            lot = body.get(lot_id)
            if not lot or lot["material"] != invested["material"]:
                continue
            quantity = min(allowance, remaining[lot_id])
            if quantity:
                lots.append({**deepcopy(lot), "quantity": quantity})
                remaining[lot_id] -= quantity
                allowance -= quantity
            if allowance == 0:
                break
        if lots:
            result.append((m.get_entity(ctx, invested["id"], required=False), lots))
    return result


def _bind_absorption(entity, *, raw=False):
    """Bind declared absorption to actual construction/returned material."""
    component = entity["components"].get("absorbent")
    lots = [lot for lot in entity["matter"] if lot["slot"] == "body" and lot["quantity"] > 0]
    if not component or not lots:
        return
    total = sum(lot["quantity"] for lot in lots)
    capacity = component["capacity"]
    if raw:
        definition = REGISTRY.materials[entity["material"]]
        capacity = definition.absorption_per_piece*total//definition.piece_quantity
        component["capacity"] = capacity
    supports, remaining = {}, capacity
    for index,lot in enumerate(lots):
        portion = remaining if index == len(lots)-1 else capacity*lot["quantity"]//total
        supports[lot["id"]] = {"quantity": lot["quantity"], "capacity": portion}
        remaining -= portion
    component["supports"] = supports


def _dismantle_reason(ctx, player, target):
    if target["state"]["burning"]:
        return "燃烧中的建筑不能拆，请先灭火。"
    if "container" in target["components"] and not target["state"]["open"]:
        return "请先打开建筑的容器口，再确认真实内容。"
    if target["state"]["water"] or target["state"]["contents"] or any(
            lot["quantity"] for lot in target["matter"] if lot["slot"] == "contained"):
        return "请先取出建筑里的物件、水和燃料。"
    if any((e.get("physical_parent") or {}).get("relation") == "attached" for e in m.descendants(ctx, target)):
        return "请先解除建筑的附着部件。"
    refunds = _refund_materials(ctx, target)
    if not refunds:
        return "已没有可拆回的投入材料。"
    return _backpack_reason(ctx, player, len(refunds))


def _option(action, reason, use, cost=None, **fields):
    value = {"action": action, "available": reason is None, "reason": reason, "use": use, **fields}
    if cost is not None:
        value["cost"] = cost
    return value


def action_options(ctx, player, target):
    state = target["state"]
    base = _path_reason(ctx, player, target)
    if state.get("kind") == "resource_node":
        options = [_option("harvest", base or ("资源已采空。" if state["stock"] <= 0 else _backpack_reason(ctx, player, 1)),
                           "采出一份真实原材料，公共库存减1。", {"stock": 1})]
        reason = base
        if not reason and state["resource_kind"] == "stone":
            reason = "石材有限，浇水不能造石。"
        if not reason and state["stock"] >= state["stock_capacity"]:
            reason = "节点库存已满。"
        if not reason and state.get("regrowth_tick") is not None:
            reason = "这一份资源正在再生。"
        if not reason and not _water_items(ctx, player, 5):
            reason = "需要自己打开的容器里至少5u水。"
        if not reason:
            from . import processes
            growing = sum(bool(process and process["kind"] == "growth") for process in
                          (processes.get_process(ctx, pid) for pid in processes.active_process_ids(ctx)))
            if growing >= processes.MAX_GROWTH:
                reason = f"最多同时养护{processes.MAX_GROWTH}个资源节点。"
        options.append(_option("tend", reason, "消耗5u真实水养护，6个物理秒后库存加1。", {"water": 5}, duration_ticks=6))
        return options
    options = []
    if is_structure(target):
        options.append(_option("dismantle", base or _dismantle_reason(ctx, player, target), "取回仍存在的材料批次；已耗材料不返。"))
    components = target.get("components", {})
    if "collector" in components:
        options.append(_option("switch_mode", base, "选择遮雨或集雨；原有真实水保留。", modes=["shelter", "collect"]))
    if "heater" in components:
        fuel = sum(lot["quantity"] for lot in target["matter"] if lot["slot"] == "contained" and lot["material"] == "wood")
        fuels = _owned_available(ctx, player, lambda e: e["material"] == "wood" and material_status(ctx, player, e)["eligible"])
        reason = base or ("请先打开炉口。" if not state["open"] else None) or ("炉中燃料已满。" if fuel >= HEARTH_FUEL_CAPACITY else None) or ("需要自己干燥的裸木料。" if not fuels else None)
        maximum = HEARTH_FUEL_CAPACITY if target["owner"] is None and not state["open"] else HEARTH_FUEL_CAPACITY-fuel
        options.append(_option("fuel", reason, "从原材料转入所选木料数量；剩余材料不丢失。", {"unit": "logical_matter"}, maximum=maximum))
    return options


def structure_actions(ctx, player, target):
    return [option["action"] for option in action_options(ctx, player, target) if option["available"]]


def _require_own(ctx, player, args):
    if not args.get("item_id"):
        raise RuleViolation("请从背包选择使用的物件。")
    entity = m.get_entity(ctx, args["item_id"])
    if args.get("expected_item_version") is not None and args["expected_item_version"] != entity["version"]:
        raise StateConflict("所选物件已经变化，请刷新后再行动。")
    reason = _path_reason(ctx, player, entity)
    if reason or entity["owner"] != player["id"] or "personal" in entity["capabilities"]:
        raise RuleViolation(reason or "须使用自己持有的可用物件。")
    return entity


def interact_structure(ctx, player, args, target):
    action, state = args["action"], target["state"]
    option = next((o for o in action_options(ctx, player, target) if o["action"] == action), None)
    if not option or not option["available"]:
        raise RuleViolation(option["reason"] if option else "此目标没有所选行动。")
    if action == "harvest":
        material = state["resource_kind"]
        names = {"wood": "木料", "cloth": "芦苇布料", "stone": "石料"}
        caps = ["transport", "wettable"]+(["flammable"] if material != "stone" else [])
        components = {}
        if REGISTRY.materials[material].combustible:
            components["combustion"] = {"rate": 2, "spread": True}
        if REGISTRY.materials[material].absorption_per_piece:
            components["absorbent"] = {"capacity": REGISTRY.materials[material].absorption_per_piece, "rate": 2}
        item = m.spawn_entity(ctx, {"name": names[material], "material": material, "capabilities": caps,
            "matter": [], "components": components,
            "owner": player["id"], "container": player["backpack_id"], "created_by": player["id"], "source": "harvest",
            "state": {"kind": "raw_material"}})
        remaining = MATERIAL_UNITS[material]
        for lot in list(target["matter"]):
            if lot["slot"] != "contained" or lot["material"] != material or not remaining:
                continue
            quantity = min(remaining, lot["quantity"])
            m.transfer_material(ctx, m.get_entity(ctx, target["id"]), m.get_entity(ctx, item["id"]),
                                lot["id"], quantity, target_slot="body", reason="harvest")
            remaining -= quantity
        if remaining:
            raise RuleViolation("节点实际材料不足，未采出任何原料。")
        item = m.get_entity(ctx, item["id"])
        for lot in item["matter"]:
            if lot["slot"] == "body":
                lot["role"] = "structural"
        item["state"]["initial_structural_quantity"] = MATERIAL_UNITS[material]
        _bind_absorption(item,raw=True)
        m.save_entity(ctx, item)
        player = m.get_player(ctx)
        if item["id"] not in player["inventory"]:
            player["inventory"].append(item["id"])
            m.save_player(ctx, player)
        target = m.get_entity(ctx, target["id"])
        return f"从{target['name']}采出{MATERIAL_UNITS[material]}u{names[material]}，公共库存剩{target['state']['stock']}份。"
    if action == "tend":
        item = _require_own(ctx, player, args)
        if "container" not in item["capabilities"] or not item["state"]["open"] or item["state"]["water"] < 5:
            raise RuleViolation("养护需要自己打开的容器里至少5u水。")
        from . import processes
        processes.start_growth(ctx, target["id"], ticks=6)
        m.consume_water(ctx, item, 5, reason="growth")
        return f"消耗5u水养护了{target['name']}，6个物理秒后增加一份资源。"
    if action == "fuel":
        item = _require_own(ctx, player, args)
        status = material_status(ctx, player, item)
        if not status["eligible"] or item["material"] != "wood":
            raise RuleViolation(status["reason"] or "须投入自己可用的裸木料。")
        lot = next((lot for lot in _body_lots(item) if lot["material"] == "wood" and (not args.get("lot_id") or lot["id"] == args["lot_id"])), None)
        if not lot:
            raise RuleViolation("所选木料批次已经不存在。")
        quantity = args.get("amount", min(40, lot["quantity"], option["maximum"]))
        if quantity > lot["quantity"] or quantity > option["maximum"]:
            raise RuleViolation("木料不足或炉中容量不足，未转移任何材料。")
        m.transfer_material(ctx, item, target, lot["id"], quantity, reason="fuel")
        item = m.get_entity(ctx, item["id"])
        if not any(lot["quantity"] for lot in item["matter"]):
            m.retire_material(ctx, item, target["id"])
        return f"向{target['name']}投入{quantity}u木料，原物件的剩余材料保留。"
    if action == "switch_mode":
        mode = args.get("mode")
        collector = target["components"]["collector"]
        if mode not in {"shelter", "collect"} or mode == collector["mode"]:
            raise RuleViolation("请选择另一种遮雨或集雨模式。")
        collector["mode"] = mode
        m.save_entity(ctx, target)
        return f"把{target['name']}切换为{'遮雨' if mode == 'shelter' else '集雨'}模式。"
    if action == "dismantle":
        refunds = _refund_materials(ctx, target)
        for source, lots in refunds:
            if source is None or not source["state"].get("retired"):
                material = lots[0]["material"]
                components = {}
                definition = REGISTRY.materials[material]
                if definition.combustible:
                    components["combustion"] = {"rate": 2, "spread": True}
                if definition.absorption_per_piece:
                    components["absorbent"] = {"capacity": definition.absorption_per_piece, "rate": 2}
                source = m.spawn_entity(ctx, {"name": "拆回的"+definition.name+"料", "material": material,
                    "capabilities": ["transport"], "components": components, "matter": [],
                    "state": {"kind": "raw_material"}, "owner": player["id"], "container": player["backpack_id"], "source": "dismantle"})
                player = m.get_player(ctx)
                if source["id"] not in player["inventory"]:
                    player["inventory"].append(source["id"])
                    m.save_player(ctx, player)
            for lot in lots:
                m.transfer_material(ctx, m.get_entity(ctx, target["id"]), m.get_entity(ctx, source["id"]),
                                    lot["id"], lot["quantity"], target_slot="body", reason="dismantle")
            source = m.get_entity(ctx, source["id"])
            _bind_absorption(source,raw=True)
            m.save_entity(ctx,source)
            if source["state"].get("retired"):
                m.restore_material(ctx, source, owner=player["id"], container=player["backpack_id"])
            elif not source["state"].get("initial_structural_quantity"):
                source["state"]["initial_structural_quantity"] = sum(lot["quantity"] for lot in source["matter"] if lot["role"] == "structural")
                m.save_entity(ctx, source)
        target = m.get_entity(ctx, target["id"])
        target["state"].update(invested_materials=[], dismantled=True)
        target["lifecycle"] = "consumed" if not target["matter"] else "broken"
        m.save_entity(ctx, target)
        _set_hearths(ctx, [eid for eid in _hearth_ids(ctx) if eid != target["id"]])
        return f"拆除了{target['name']}，取回{sum(lot['quantity'] for _, lots in refunds for lot in lots)}u仍存在的材料。"
    raise RuleViolation("未知设施行动。")


def effect_projection(entity):
    recipe = RECIPES[entity["state"]["recipe"]]
    radius = recipe["radius"]
    components = entity.get("components", {})
    active = entity.get("lifecycle") == "intact" and components.get("structure", {}).get("integrity", 0) > 0
    coverage = [{"x": entity["x"]+dx, "y": entity["y"]+dy}
                for dy in range(-radius, radius+1) for dx in range(-radius, radius+1)
                if max(abs(entity["x"]+dx), abs(entity["y"]+dy)) <= m.WORLD_LIMIT] if active and entity["x"] is not None else []
    effects = [recipe["use"]] if active else ["结构已失效，不再提供原效果；剩余物质仍在现场。"]
    collector = components.get("collector")
    heater = components.get("heater")
    if collector and active and (entity["state"]["burning"] or collector["mode"] == "collect" and not entity["state"]["open"]):
        effects = ["燃烧或关闭的集雨口使当前作用停止。"]
        active, coverage = False, []
    if collector and active:
        effects = ["遮雨范围内的公共物件减少雨水接触。"] if collector["mode"] == "shelter" else ["把实际降雨转入同一容器；水增加载荷。"]
    if heater and active:
        effects = ["真实木料正在燃烧，附近公共材料加快蒸发。"] if heater["lit"] else ["尚未点燃；须装入实际木料再点火。"]
    return {"id": entity["id"], "recipe": recipe["id"], "x": entity["x"], "y": entity["y"],
            "radius": radius, "coverage": coverage, "effects": effects, "active": active,
            **({"mode": collector["mode"]} if collector else {}), **({"lit": heater["lit"]} if heater else {}),
            "water": entity["state"]["water"] if entity["state"]["open"] or entity["owner"] is not None else None,
            "water_capacity": entity["state"]["water_capacity"]}


def nearby_effects(ctx, x, y, radius=8):
    return [effect_projection(e) for e in m.public_entities_near(ctx, x, y, radius, limit=160) if is_structure(e)]


def on_walkway(ctx, x, y):
    return any("walkway" in e["capabilities"] and e.get("lifecycle") == "intact"
               and e.get("components", {}).get("structure", {}).get("integrity", 0) > 0
               for e in m.public_entities_near(ctx, x, y, radius=0, limit=96))


def rain_effect(ctx, entity, intensity):
    protected_by = None
    for part in m.descendants(ctx, entity):
        relation = part.get("physical_parent") or {}
        if (relation.get("id") == entity["id"] and relation.get("relation") == "attached"
                and "covering" in part.get("components", {}) and part.get("lifecycle") == "intact"
                and not part["state"]["burning"]):
            protected_by = part["id"]
            break
    if m.is_ground(entity):
        for root in m.public_entities_near(ctx, entity["x"], entity["y"], radius=1, limit=4*m.MAX_CHUNK_ENTITIES):
            collector = root.get("components", {}).get("collector")
            if (collector and root["id"] != entity["id"] and root.get("lifecycle") == "intact"
                    and collector["mode"] == "shelter" and not root["state"]["burning"]):
                protected_by = root["id"]
                break
    components = entity.get("components", {})
    collect_capacity = collect_rate = 0
    if not protected_by and entity.get("lifecycle") == "intact" and entity["state"]["open"]:
        collector = components.get("collector")
        collect_rate = intensity if collector and collector["mode"] == "collect" else intensity//4 if not collector else 0
        collect_capacity = entity["state"]["water_capacity"] if collect_rate else 0
    return {"protected_by": protected_by, "collect_capacity": collect_capacity, "collect_rate": collect_rate,
            "wettable": not protected_by and "absorbent" in components}


def tick_structures(ctx, sequence, changed, entities=None, *, dirty=None):
    """Choose controlled heat; the common burning phase spends each lot once."""
    if entities is None:
        entities = {root["id"]:root for root in _live_hearths(ctx)}
    # The physics owner has already admitted a bounded closure. Reading an
    # unrelated dry furnace must never add it or its neighbours to that frame.
    hearths = [root for root in list(entities.values()) if m.is_ground(root)
               and "heater" in root.get("components", {}) and root.get("lifecycle") == "intact"]
    kept = []
    for eid in _hearth_ids(ctx):
        root = entities.get(eid) or m.get_entity(ctx,eid,required=False)
        if root and m.is_ground(root) and "heater" in root.get("components", {}) and root.get("lifecycle") == "intact":
            kept.append(eid)
    _set_hearths(ctx, kept)
    heated = set()
    for root in hearths:
        heater = root["components"]["heater"]
        before = (heater["lit"], tuple((lot["id"],lot["burning"]) for lot in root["matter"]))
        lots = [lot for lot in root["matter"] if lot["slot"] == "contained" and lot["material"] == "wood" and lot["quantity"] > 0]
        if heater["lit"] and (root["state"]["wetness"] >= 35 or not root["state"]["open"] or not lots):
            heater["lit"] = False
            for lot in lots:
                lot["burning"] = False
            changed.append(f"{root['name']}熄灭了。")
        if heater["lit"]:
            if not any(lot["burning"] for lot in lots):
                lots[0]["burning"] = True
            for entity in m.public_entities_near(ctx, root["x"], root["y"], radius=heater["radius"], limit=4*m.MAX_CHUNK_ENTITIES):
                entity = entities.get(entity["id"])
                if entity and any(lot["material"] == "water" and lot["slot"] in {"absorbed", "surface"}
                                  and lot["quantity"] > 0 for lot in entity["matter"]):
                    heated.add(entity["id"])
        after = (heater["lit"], tuple((lot["id"],lot["burning"]) for lot in root["matter"]))
        if dirty is not None and before != after:
            dirty.add(root["id"])
    return heated
