"""Costed public structures and resource use, on the ordinary entity model."""
from __future__ import annotations

from copy import deepcopy

from agent_world.errors import RuleViolation

from . import model as m


MAX_HEARTHS = 16
HEARTH_FUEL_CAPACITY = 200
RECIPES = {
    "walkway": {"id": "walkway", "name": "干脚通路", "cost": {"wood": 2},
                "terrain": ["water"], "radius": 0,
                "use": "铺在一格浅溪上；涉水仍可直接走，这格通路免除随身物件受湿。"},
    "canopy": {"id": "canopy", "name": "雨棚", "cost": {"wood": 2, "cloth": 1},
               "terrain": ["grass", "dirt"], "radius": 1,
               "use": "遮雨模式保护周围九格的公共地面材料；集雨模式收集最多40份真实水。"},
    "hearth": {"id": "hearth", "name": "烘干炉", "cost": {"stone": 2},
               "terrain": ["grass", "dirt"], "radius": 1,
               "use": "每份木料提供40燃料；另用打火石点燃，每秒耗2燃料，附近公共材料额外干6且不蔓延。"},
}


def recipes():
    return deepcopy(list(RECIPES.values()))


def is_structure(entity):
    return entity["state"].get("recipe") in RECIPES


def _path_reason(ctx, player, entity):
    if entity["state"]["destroyed"] or entity["state"].get("retired"):
        return "物件已毁坏或已投入其他用途。"
    if entity["owner"] not in {None, player["id"]}:
        return "不能使用别人的物件。"
    cursor = entity
    for _ in range(8):
        if cursor["state"]["burning"]:
            return "燃烧中的物件及容器内容不能取用。"
        if not cursor.get("container"):
            break
        cursor = m.get_entity(ctx, cursor["container"])
        if not cursor["state"]["open"]:
            return "请先打开盛放物件的容器。"
    else:
        return "容器层数超过限制。"
    x, y = m.entity_location(ctx, entity)
    if x is None or (entity["owner"] is None and abs(x-player["x"])+abs(y-player["y"]) > 1):
        return "要先走到目标相邻的一格。"
    return None


def material_status(ctx, player, entity):
    caps = set(entity["capabilities"])
    reason = _path_reason(ctx, player, entity)
    if not reason and entity["owner"] != player["id"]:
        reason = "请先把原材料拿到自己的背包。"
    if not reason and ("transport" not in caps or caps & {"container", "ignition", "personal"}
                       or entity["state"]["contents"] or entity["state"]["water"]
                       or is_structure(entity) or entity["state"].get("kind") == "resource_node"):
        reason = "需使用单份裸材料，容器、点火工具和私人物件不能投料。"
    if not reason and entity["material"] != "stone" and entity["state"]["wetness"] >= 35:
        reason = "木料和布料湿度须低于35，请先晾干或烘干。"
    if not reason and entity["material"] != "stone" and entity["state"]["fuel"] <= 0:
        reason = "材料的可用部分已经烧尽。"
    return {"eligible": reason is None, "reason": reason, "material": entity["material"]}


def _backpack_reason(ctx, player, count):
    backpack = m.get_entity(ctx, player["backpack_id"])
    reason = _path_reason(ctx, player, backpack)
    if reason:
        return reason
    if not backpack["state"]["open"]:
        return "请先打开自己的背包。"
    if len(backpack["state"]["contents"])+count > backpack["state"]["capacity"]:
        return "背包容量不足，请先腾出位置。"
    if len(player["inventory"])+count > m.MAX_INVENTORY:
        return "背包物件过多。"
    return None


def _owned_available(ctx, player, predicate):
    result = []
    for eid in player["inventory"]:
        entity = m.get_entity(ctx, eid)
        if not _path_reason(ctx, player, entity) and predicate(entity):
            result.append(entity)
    return result


def _water_items(ctx, player, minimum=1):
    return _owned_available(ctx, player, lambda e: "container" in e["capabilities"]
                            and "personal" not in e["capabilities"] and e["state"]["open"]
                            and e["state"]["water"] >= minimum)


def _hearth_ids(ctx):
    return list(ctx.get_state(m.META, "active_hearths", []))


def _live_hearths(ctx):
    return [e for eid in _hearth_ids(ctx) if (e := m.get_entity(ctx, eid, required=False))
            and m.is_ground(e) and e["state"].get("recipe") == "hearth"]


def _set_hearths(ctx, ids):
    ids = list(dict.fromkeys(ids))
    if ids != _hearth_ids(ctx):
        ctx.set_state(m.META, "active_hearths", ids)


def _choose_materials(ctx, player, recipe, selected):
    cost = recipe["cost"]
    if selected is not None:
        if len(selected) != sum(cost.values()) or len(set(selected)) != len(selected):
            raise RuleViolation("所选原材料件数须刚好符合配方，不能重复。")
        materials = [m.get_entity(ctx, eid) for eid in selected]
    else:
        candidates = [m.get_entity(ctx, eid) for eid in player["inventory"]]
        materials = []
        for material, count in cost.items():
            eligible = [e for e in candidates if e["material"] == material
                        and material_status(ctx, player, e)["eligible"]]
            eligible.sort(key=lambda e: e["id"])
            if len(eligible) < count:
                name = {"wood": "木", "cloth": "布", "stone": "石"}[material]
                raise RuleViolation(f"缺少{count}件可用{name}原料；木布须干燥，容器和点火工具不能投料。")
            materials.extend(eligible[:count])
    actual = {}
    for entity in materials:
        status = material_status(ctx, player, entity)
        if not status["eligible"]:
            raise RuleViolation(status["reason"])
        actual[entity["material"]] = actual.get(entity["material"], 0)+1
    if actual != cost:
        raise RuleViolation("所选原材料的材质和件数不符合配方。")
    return materials


def build(ctx, player, args):
    recipe = RECIPES[args["recipe"]]
    x, y = args["x"], args["y"]
    if abs(x-player["x"])+abs(y-player["y"]) > 1:
        raise RuleViolation("只能在自己所在或相邻一格建设。")
    m.ensure_chunk(ctx, *m.chunk_for(x, y))
    if m.tile_at(ctx, x, y) not in recipe["terrain"]:
        raise RuleViolation("通路只能建在浅溪上，雨棚和烘干炉只能建在草地或泥地。")
    ground = m.public_entities_near(ctx, x, y, radius=0, limit=m.MAX_CHUNK_ENTITIES)
    if any(is_structure(e) or e["state"].get("kind") == "resource_node" for e in ground):
        raise RuleViolation("这一格已有建筑或资源节点。")
    if m.blocking_at(ctx, x, y):
        raise RuleViolation("请先移开这一格的障碍。")
    if args["recipe"] == "hearth" and m.is_safe(ctx, x, y):
        raise RuleViolation("出生安全区不能建设火炉。")
    hearths = _live_hearths(ctx)
    if args["recipe"] == "hearth" and len(hearths) >= MAX_HEARTHS:
        raise RuleViolation("世界最多保留16座可用烘干炉，请先拆除不用的炉。")
    materials = _choose_materials(ctx, player, recipe, args.get("material_ids"))
    bid = m.identifier("building")
    investment = [{"id": e["id"], "material": e["material"],
                   "fuel": e["state"]["fuel"], "wetness": e["state"]["wetness"]} for e in materials]
    structural_fuel = sum(e["fuel"] for e in investment)
    if structural_fuel > 200:
        raise RuleViolation("所选原料的燃料超过这座建筑的容量。")
    state = {"recipe": args["recipe"], "invested_materials": investment,
             "fuel": structural_fuel, "wetness": max(e["wetness"] for e in investment)}
    if args["recipe"] == "hearth":
        material, caps = "stone", ["hearth", "wettable"]
        state.update(hearth_fuel=0, lit=False, consumed_fuel_count=0)
    else:
        material, caps = "wood", [args["recipe"], "flammable", "wettable"]
        if args["recipe"] == "canopy":
            caps.append("container")
            state.update(mode="shelter", water=0, water_capacity=40, capacity=0, open=True)
    for entity in materials:
        m.retire_material(ctx, entity, bid)
    entity = m.spawn_entity(ctx, {"id": bid, "name": recipe["name"], "material": material,
        "capabilities": caps, "x": x, "y": y, "created_by": player["id"], "source": "construction", "state": state})
    if args["recipe"] == "hearth":
        _set_hearths(ctx, [e["id"] for e in hearths]+[bid])
    return entity, f"搭建了{recipe['name']}，投入的原材料已从背包移出。"


def _refund_materials(ctx, building):
    # Structural fire spends the invested biomass. Allocate the finite remaining
    # fuel in ledger order; a piece spent completely can never be restored.
    remaining = building["state"]["fuel"]
    result = []
    for invested in building["state"].get("invested_materials", []):
        entity = m.get_entity(ctx, invested["id"], required=False)
        if (not entity or entity["state"]["destroyed"] or not entity["state"].get("retired")
                or entity["state"].get("retired_into") != building["id"]):
            continue
        fuel = min(remaining, invested["fuel"])
        remaining -= fuel
        if entity["material"] == "stone" or fuel > 0:
            result.append((entity, fuel))
    return result


def _dismantle_reason(ctx, player, target):
    state = target["state"]
    if state["burning"] or state.get("lit"):
        return "燃烧中的建筑不能拆，请先灭火。"
    if state["water"] or state["contents"]:
        return "请先取出建筑里的水和物件。"
    return _backpack_reason(ctx, player, len(_refund_materials(ctx, target)))


def _option(action, reason, use, cost=None, **fields):
    value = {"action": action, "available": reason is None, "reason": reason, "use": use, **fields}
    if cost is not None:
        value["cost"] = cost
    return value


def action_options(ctx, player, target):
    """Expose precisely the useful action prerequisites without making writes."""
    state = target["state"]
    base_reason = _path_reason(ctx, player, target)
    if state.get("kind") == "resource_node":
        harvest_reason = base_reason or ("资源已采空。" if state["stock"] <= 0 else _backpack_reason(ctx, player, 1))
        options = [_option("harvest", harvest_reason, "采出一份原材料，公共库存减1。", {"stock": 1})]
        reason = base_reason
        if not reason and state["resource_kind"] == "stone":
            reason = "石材有限，浇水不能造石。"
        if not reason and state["stock"] >= state["stock_capacity"]:
            reason = "节点库存已满。"
        if not reason and state.get("regrowth_tick") is not None:
            reason = "这一份资源正在再生。"
        if not reason and not _water_items(ctx, player, 5):
            reason = "需要自己打开的容器里至少5份水。"
        if not reason:
            from . import processes
            if not processes.can_start_growth(ctx):
                reason = "最多同时养护16个资源节点。"
        options.append(_option("tend", reason, "消耗水养护，6个物理秒后库存加1。", {"water": 5}, duration_ticks=6))
        return options
    if not is_structure(target):
        return []
    options = [_option("dismantle", base_reason or _dismantle_reason(ctx, player, target),
                       "把未烧尽的投入原料退到当前拆除者背包；已耗燃料不返。")]
    if state["recipe"] == "canopy":
        reason = base_reason or ("燃烧中的棚不能切换模式。" if state["burning"] else None)
        options.append(_option("switch_mode", reason, "在遮雨与集雨之间选择，已有水保留。", modes=["shelter", "collect"]))
    elif state["recipe"] == "hearth":
        fuels = _owned_available(ctx, player, lambda e: e["material"] == "wood"
                                 and material_status(ctx, player, e)["eligible"] and e["state"]["fuel"] >= 40)
        reason = base_reason
        if not reason and state["hearth_fuel"]+40 > HEARTH_FUEL_CAPACITY:
            reason = "炉中最多存200份燃料，暂时放不下一份木料。"
        if not reason and not fuels:
            reason = "需要一份干燥、完整的裸木料。"
        options.append(_option("fuel", reason, "投入一份木料增加40燃料；还需点火才开始烘干。", {"wood": 1}))
        tools = _owned_available(ctx, player, lambda e: "ignition" in e["capabilities"]
                                 and "personal" not in e["capabilities"] and e["state"].get("uses", 20) > 0)
        reason = base_reason
        if not reason and state.get("lit"):
            reason = "炉已经点燃。"
        if not reason and state["hearth_fuel"] <= 0:
            reason = "请先投入木料。"
        if not reason and state["wetness"] >= 35:
            reason = "炉太湿，须先自然干燥到35以下。"
        if not reason and m.is_safe(ctx, target["x"], target["y"]):
            reason = "出生安全区不能点火。"
        if not reason and not tools:
            reason = "需要自己可用的点火工具。"
        options.append(_option("ignite", reason, "点燃受控火，燃料每秒减2，附近公共材料额外干6；不会蔓延。", {"uses": 1}))
    return options


def structure_actions(ctx, player, target):
    return [o["action"] for o in action_options(ctx, player, target) if o["available"]]


def _require_own(ctx, player, args):
    if not args.get("item_id"):
        raise RuleViolation("请从背包选择使用的物件。")
    entity = m.get_entity(ctx, args["item_id"])
    reason = _path_reason(ctx, player, entity)
    if reason or entity["owner"] != player["id"] or "personal" in entity["capabilities"]:
        raise RuleViolation(reason or "须使用自己持有的可用物件。")
    return entity


def interact_structure(ctx, player, args, target):
    action, state = args["action"], target["state"]
    option = next((o for o in action_options(ctx, player, target) if o["action"] == action), None)
    if option is None:
        raise RuleViolation("此目标没有所选设施或资源行动。")
    if not option["available"]:
        raise RuleViolation(option["reason"])
    if action == "harvest":
        material = state["resource_kind"]
        names = {"wood": "木料", "cloth": "芦苇布料", "stone": "石料"}
        caps = ["transport", "wettable"]
        if material != "stone":
            caps.append("flammable")
        item = m.spawn_entity(ctx, {"name": names[material], "material": material, "capabilities": caps,
            "owner": player["id"], "container": player["backpack_id"], "created_by": player["id"], "source": "harvest",
            "state": {"kind": "raw_material", "wetness": state["wetness"]}})
        player = m.get_player(ctx)
        player["inventory"].append(item["id"])
        m.save_player(ctx, player)
        state["stock"] -= 1
        m.save_entity(ctx, target)
        return f"从{target['name']}采出了一份{names[material]}，公共库存剩{state['stock']}份。"
    if action == "tend":
        item = _require_own(ctx, player, args)
        if "container" not in item["capabilities"] or not item["state"]["open"] or item["state"]["water"] < 5:
            raise RuleViolation("养护需要自己打开的容器里至少5份水。")
        from . import processes
        processes.start_growth(ctx, target["id"], ticks=6)
        item["state"]["water"] -= 5
        m.save_entity(ctx, item)
        return f"消耗5份水养护了{target['name']}，6个物理秒后增加一份资源。"
    if action == "fuel":
        item = _require_own(ctx, player, args)
        status = material_status(ctx, player, item)
        if not status["eligible"] or item["material"] != "wood" or item["state"]["fuel"] < 40:
            raise RuleViolation(status["reason"] or "须投入一份至少40燃料的完整木料。")
        m.retire_material(ctx, item, target["id"])
        item["state"].update(destroyed=True, fuel=0)
        m.save_entity(ctx, item)
        state["hearth_fuel"] += 40
        state["consumed_fuel_count"] = state.get("consumed_fuel_count", 0)+1
        m.save_entity(ctx, target)
        return f"把一份木料投入{target['name']}，增加40燃料；投入木料已消耗，不能拆回。"
    if action == "ignite":
        item = _require_own(ctx, player, args)
        if "ignition" not in item["capabilities"] or item["state"].get("uses", 20) <= 0:
            raise RuleViolation("需要自己可用的点火工具。")
        item["state"]["uses"] = item["state"].get("uses", 20)-1
        state["lit"] = True
        m.save_entity(ctx, item)
        m.save_entity(ctx, target)
        return f"点燃了{target['name']}，开始消耗燃料烘干附近公共材料。"
    if action == "switch_mode":
        mode = args.get("mode")
        if mode not in {"shelter", "collect"}:
            raise RuleViolation("请选择遮雨或集雨模式。")
        if mode == state["mode"]:
            raise RuleViolation("棚已经处于所选模式。")
        state["mode"] = mode
        m.save_entity(ctx, target)
        return f"把{target['name']}切换为{'遮雨' if mode == 'shelter' else '集雨'}模式。"
    if action == "dismantle":
        refunds = _refund_materials(ctx, target)
        refunded_ids = {e["id"] for e, _ in refunds}
        for entity, fuel in refunds:
            entity["state"]["fuel"] = fuel
            entity["state"]["wetness"] = max(entity["state"]["wetness"], state["wetness"])
            m.restore_material(ctx, entity, owner=player["id"], container=player["backpack_id"])
        for invested in state["invested_materials"]:
            if invested["id"] not in refunded_ids:
                entity = m.get_entity(ctx, invested["id"], required=False)
                if entity and entity["state"].get("retired_into") == target["id"]:
                    entity["state"].update(destroyed=True, fuel=0)
                    m.save_entity(ctx, entity)
        state.update(destroyed=True, fuel=0, hearth_fuel=0, lit=False, invested_materials=[], dismantled=True)
        m.save_entity(ctx, target)
        if state["recipe"] == "hearth":
            _set_hearths(ctx, [eid for eid in _hearth_ids(ctx) if eid != target["id"]])
        return f"拆除了{target['name']}，{len(refunds)}份未烧尽原料已退到你的背包。"
    raise RuleViolation("未知设施行动。")


def effect_projection(entity):
    state, recipe = entity["state"], RECIPES[entity["state"]["recipe"]]
    radius = recipe["radius"]
    coverage = [{"x": entity["x"]+dx, "y": entity["y"]+dy}
                for dy in range(-radius, radius+1) for dx in range(-radius, radius+1)
                if max(abs(entity["x"]+dx), abs(entity["y"]+dy)) <= m.WORLD_LIMIT]
    if state["recipe"] == "walkway":
        effects = ["这一格走过时随身物件不因涉水变湿。"]
    elif state["recipe"] == "canopy":
        effects = (["保护周围九格公共地面材料免受雨；棚自身仍受雨。"] if state["mode"] == "shelter"
                   else ["降雨时蓄水，容量40；可装入自己容器。"])
        if state["mode"] == "collect" and not state["open"]:
            effects = ["储水口已关闭，集雨和取水暂停。"]
        if state["burning"]:
            effects = ["棚正在燃烧，遮雨与集雨停止。"]
    else:
        effects = (["附近公共地面材料每秒额外干6，多炉不叠加；燃料每秒减2，不蔓延。"]
                   if state.get("lit") else ["炉未点燃，尚不产生烘干效果。"])
    if state["destroyed"]:
        effects = ["建筑已拆除或烧毁，不再提供效果。"]
        coverage = []
    return {"id": entity["id"], "recipe": state["recipe"], "x": entity["x"], "y": entity["y"],
            "radius": radius, "coverage": coverage, "effects": effects, "active": not state["destroyed"],
            **{key: state[key] for key in ("mode", "lit", "hearth_fuel", "water", "water_capacity") if key in state}}


def nearby_effects(ctx, x, y, radius=8):
    return [effect_projection(e) for e in m.public_entities_near(ctx, x, y, radius, limit=160) if is_structure(e)]


def on_walkway(ctx, x, y):
    return any(e["state"].get("recipe") == "walkway" for e in m.public_entities_near(ctx, x, y, radius=0, limit=96))


def rain_effect(ctx, entity, intensity):
    """Pure read: the weather pass applies this result once per target."""
    state = entity["state"]
    protected_by = None
    if m.is_ground(entity) and state.get("recipe") != "canopy":
        for canopy in m.public_entities_near(ctx, entity["x"], entity["y"], radius=1, limit=4*m.MAX_CHUNK_ENTITIES):
            if (canopy["id"] != entity["id"] and canopy["state"].get("recipe") == "canopy"
                    and canopy["state"]["mode"] == "shelter" and not canopy["state"]["burning"]):
                protected_by = canopy["id"]
                break
    wetness, water = state["wetness"], state["water"]
    if not protected_by:
        if "wettable" in entity["capabilities"]:
            wetness = min(100, wetness+intensity)
        if "container" in entity["capabilities"] and state["open"]:
            collected = intensity//4
            if state.get("recipe") == "canopy":
                collected = intensity if state["mode"] == "collect" and not state["burning"] else 0
            water += min(collected, state["water_capacity"]-water)
    return {"wetness": wetness, "water": water, "protected_by": protected_by}


def tick_structures(ctx, sequence, changed, entities=None):
    """Compute controlled heat for the caller's single material write pass."""
    entities = {} if entities is None else entities
    hearths = []
    for eid in _hearth_ids(ctx):
        hearth = entities.get(eid)
        if hearth is None:
            hearth = m.get_entity(ctx, eid, required=False)
        if hearth and m.is_ground(hearth) and hearth["state"].get("recipe") == "hearth":
            hearths.append(hearth)
    _set_hearths(ctx, [e["id"] for e in hearths])
    heated = set()
    for hearth in hearths:
        state = hearth["state"]
        if not state.get("lit"):
            continue
        if state["wetness"] >= 35 or state["hearth_fuel"] <= 0:
            state["lit"] = False
            entities[hearth["id"]] = hearth
            changed.append(f"{hearth['name']}熄灭了。")
            continue
        state["hearth_fuel"] = max(0, state["hearth_fuel"]-2)
        if not state["hearth_fuel"]:
            state["lit"] = False
            changed.append(f"{hearth['name']}的燃料耗尽。")
        entities[hearth["id"]] = hearth
        for entity in m.public_entities_near(ctx, hearth["x"], hearth["y"], radius=1, limit=4*m.MAX_CHUNK_ENTITIES):
            entity = entities.get(entity["id"], entity)
            if "wettable" in entity["capabilities"] and entity["state"]["wetness"] > 0:
                entities[entity["id"]] = entity
                heated.add(entity["id"])
    return heated
