"""Form declared bodies by one atomic transfer of actual, guarded lots."""
from __future__ import annotations

from copy import deepcopy
from agent_world import FunctionOutcome, FunctionSpec
from agent_world.errors import PermissionDenied, RuleViolation, StateConflict
from agent_world.simulation import matter as sm
from agent_world.simulation.effects import Transfer
from agent_world.simulation.frame import Frame
from . import model as m, commit
from .definitions import registry_for, bind_material, mixture_capacity, material_import

ID = {"type": "string", "minLength": 1, "maxLength": 128}
VERSION = {"type": "integer", "minimum": 1}
ARGS = {"type": "object", "additionalProperties": False,
    "required": ["definition_ref", "type_id", "expected_player_version", "placement", "sources"],
    "properties": {"definition_ref": ID, "type_id": {"type": "string", "minLength": 1, "maxLength": 48},
        "expected_player_version": VERSION,
        "placement": {"type": "object", "additionalProperties": False, "required": ["kind"],
                      "properties": {"kind": {"const": "inventory"}}},
        "sources": {"type": "array", "minItems": 1, "maxItems": 8, "items": {
            "type": "object", "additionalProperties": False,
            "required": ["entity_id", "expected_entity_version", "lot_id", "quantity_u"],
            "properties": {"entity_id": ID, "expected_entity_version": VERSION, "lot_id": ID,
                           "quantity_u": {"type": "integer", "minimum": 1, "maximum": 200}}}},
        "delegation": {"type": "string", "minLength": 16, "maxLength": 100}}}
MAX_SOURCE_DISCOVERY = 12
MAX_SOURCE_SCAN = 160


def eligible_lots(ctx, player, entity):
    from .world import _accessible, _transfer_lots
    from .creative import guard_ordinary_action
    try:
        _accessible(ctx, player, entity, subject="item")
        guard_ordinary_action(ctx, "fabricate", entity)
    except (RuleViolation, PermissionDenied):
        return []
    if (entity.get("owner") != player["id"] or entity["lifecycle"] != "intact"
            or entity["state"].get("retired") or entity["state"]["burning"]
            or "personal" in entity["capabilities"]):
        return []
    registry = registry_for(entity)
    return [lot for lot in _transfer_lots(ctx, player, entity)
            if not registry.materials[lot["material"]].liquid
            and (lot["slot"] != "body" or not entity["state"].get("program", {}).get("definition_ref")
                 and entity.get("source") != "fabrication")
            and sm.lot_moisture(entity, lot) < 35 and not sm.quantity(entity, "water", "surface")]


def _unavailable_source(ctx, player, entity, costs):
    """Explain actual current eligibility, without summing structural matter."""
    from .world import _accessible, _transfer_lots
    from .creative import guard_ordinary_action
    relevant = [lot for lot in entity["matter"] if lot["material"] in costs and lot["quantity"] > 0]
    if not relevant or any(lot["material"] in costs for lot in eligible_lots(ctx, player, entity)):
        return None
    try:
        _accessible(ctx, player, entity, subject="item")
        guard_ordinary_action(ctx, "fabricate", entity)
    except (RuleViolation, PermissionDenied) as exc:
        code, reason = "access_or_rule", str(exc)
    else:
        if entity["lifecycle"] != "intact" or entity["state"].get("retired"):
            code, reason = "not_intact", "物件已损坏或退役，不能作为成型原料。"
        elif entity["state"]["burning"]:
            code, reason = "burning", "燃烧中的材料不能用于成型。"
        elif "personal" in entity["capabilities"]:
            code, reason = "personal_structure", "背包等私人物件的结构不是可消费原料。"
        elif any(sm.lot_moisture(entity, lot) >= 35 for lot in relevant) or sm.quantity(entity, "water", "surface"):
            code, reason = "wet", "需先取得足够干燥且无表面水的实际批次。"
        elif not _transfer_lots(ctx, player, entity):
            code, reason = "not_loose_material", "只有合格裸料或打开容器内可取用的材料能投料；物件结构不能静默拆作原料。"
        else:
            code, reason = "lot_not_eligible", "相关批次被绑定、材质不符或属于已成型结构；请读取真实批次。"
    return {"id": entity["id"], "name": entity["name"], "reason_code": code, "reason": reason[:220],
            "materials": sorted({lot["material"] for lot in relevant}),
            "read_request": {"function_id": "grove.inspect", "arguments": {"entity_id": entity["id"]}}}


def _acquisition_sources(ctx, player, costs):
    """Local authorized discovery, not reserved sources or an automatic plan."""
    from .world import _accessible
    from . import building
    visible = m.public_entities_near(ctx, player["x"], player["y"], radius=8, limit=MAX_SOURCE_SCAN)
    found = []
    for entity in visible:
        try:
            _accessible(ctx, player, entity, near=False)
        except (RuleViolation, PermissionDenied):
            continue
        materials, action, reason = {}, None, None
        state = entity["state"]
        if state.get("kind") == "resource_node" and state.get("resource_kind") in costs:
            material = state["resource_kind"]
            # Actual visible node stock, not regeneration promises or body mass.
            if state.get("stock", 0) > 0 and entity["lifecycle"] == "intact":
                materials[material] = state["stock"] * building.MATERIAL_UNITS[material]
                action = "harvest"
        elif ("transport" in entity["capabilities"] and "container" not in entity["components"]
              and entity["lifecycle"] == "intact" and not state["burning"]):
            # A prospective pickup is evaluated by the same formal own-source
            # filter; this detached read candidate grants no ownership.
            prospective = deepcopy(entity)
            prospective["owner"] = player["id"]
            for lot in eligible_lots(ctx, player, prospective):
                if lot["material"] in costs:
                    materials[lot["material"]] = materials.get(lot["material"], 0)+lot["quantity"]
            if materials:
                action = "pickup"
        elif "container" in entity["components"] and entity["lifecycle"] == "intact":
            # Do not examine a closed container's contents. It may be worth
            # opening, but unknown stock is never reported as existing material.
            if not state["open"]:
                action, reason = "open", "内部材料尚不可见；打开后重新检查，不保证有需要的原料。"
            else:
                prospective = deepcopy(entity)
                prospective["owner"] = player["id"]
                for lot in eligible_lots(ctx, player, prospective):
                    if lot["slot"] == "contained" and lot["material"] in costs:
                        materials[lot["material"]] = materials.get(lot["material"], 0)+lot["quantity"]
                if materials:
                    action = "pickup" if "transport" in entity["capabilities"] else None
                    reason = ("取走整件容器不消费其结构；拿取后仍须重新检查内部材料资格。" if action
                              else "该容器不可拿起；从本人可盛料容器的转移菜单读取该来源，再实际转移材料。")
        if action is None and not materials:
            continue
        found.append({"id": entity["id"], "name": entity["name"], "location": {"x": entity["x"], "y": entity["y"]},
            "materials": [{"material_id": key, "visible_quantity_u": quantity} for key, quantity in materials.items()],
            "distance": abs(entity["x"]-player["x"])+abs(entity["y"]-player["y"]),
            "next_action": action, "reason": reason,
            "read_request": {"function_id": "grove.inspect", "arguments": {"entity_id": entity["id"],
                **({"action": action} if action else {})}},
            "permission": "Discovery only; inspect actual action conditions and revalidate before acting."})
    found.sort(key=lambda row: (not row["materials"], row["distance"], row["id"]))
    return found[:MAX_SOURCE_DISCOVERY], {"radius": 8, "radius_metric": "chebyshev",
        "scan_limit": MAX_SOURCE_SCAN, "source_limit": MAX_SOURCE_DISCOVERY,
        "truncated": len(visible) >= MAX_SOURCE_SCAN or len(found) > MAX_SOURCE_DISCOVERY,
        "scope": "authorized_nearby_public_sources", "world_scan": False,
        "meaning": "Visible possible acquisition sources, not available own stock or reserved quantities; omitted sources may exist."}


def _plan(ctx, args):
    from .creative import object_definition, definition_read, _material
    definition = definition_read(ctx, {"definition_ref": args["definition_ref"]}).result["definition"]
    spec = definition["types"].get(args["type_id"])
    if not spec or "cost_u" not in spec:
        raise RuleViolation("此类型没有真实u成型配方；旧cost类型使用区域生成路径。")
    candidate = object_definition(ctx, args, formation=True)
    costs = {}
    for symbol, quantity in spec["cost_u"].items():
        identity = _material(candidate, symbol)
        costs[identity] = costs.get(identity, 0) + quantity
    return definition, spec, candidate, costs


def fabrication_options(ctx, args):
    from .world import _public_self
    from .creative import _material
    player = m.get_player(ctx)
    definition, spec, candidate, costs = _plan(ctx, args)
    choices, unavailable, available = [], [], {identity: 0 for identity in costs}
    for eid in player["inventory"]:
        entity = m.get_entity(ctx, eid)
        lots = [lot for lot in eligible_lots(ctx, player, entity) if lot["material"] in costs]
        if lots:
            choices.append({"id": eid, "name": entity["name"], "version": entity["version"], "kind": "entity",
                "lots": [{**{key: lot[key] for key in ("id", "material", "quantity", "slot")},
                    **({"material_import": material_import(lot["material"])} if material_import(lot["material"]) else {})} for lot in lots]})
            for lot in lots:
                available[lot["material"]] += lot["quantity"]
        elif (reason := _unavailable_source(ctx, player, entity, costs)) is not None:
            unavailable.append(reason)
    registry = registry_for(candidate)
    enough = all(available[key] >= quantity for key, quantity in costs.items())
    action = {"action": "fabricate", "label": "从真实材料成型", "function_id": "grove.fabricate",
        "arguments": {"definition_ref": args["definition_ref"], "type_id": args["type_id"],
                      "expected_player_version": player["version"], "placement": {"kind": "inventory"}, "sources": []},
        "input_schema": deepcopy(ARGS), "choices": {"sources": choices}, "available": enough,
        "reason": None if enough else "本人可用材料不足；先取得配方所需的真实批次。"}
    acquisition, discovery = _acquisition_sources(ctx, player, costs) if not enough else ([], {
        "scope": "not_requested_materials_already_available", "truncated": False, "world_scan": False})
    result = {"player": _public_self(player), "definition_ref": args["definition_ref"], "type_id": args["type_id"],
        "source_scope": "Only eligible lots in this person's own inventory are counted. Lots outside that inventory, foreign-owned, too wet, burning and constructed body lots are not fabrication stock. This read neither reserves nor selects sources; fabricate revalidates the exact submitted lots and versions.",
        "materials_quantity_basis": "quantity_u, available_u and missing_u are totals per canonical material_id, as in cost_u. Rows with different aliases but the same material_id describe the same group; their quantities must not be added again.",
        "cost_u": costs, "materials": [{"symbol": symbol, "material_id": identity,
            "name": registry.materials[identity].name, "quantity_u": costs[identity],
            "available_u": available[identity], "missing_u": max(0, costs[identity]-available[identity])}
            for symbol in spec["cost_u"] for identity in [_material(candidate, symbol)]],
        "action": action, "unavailable_sources": unavailable[:MAX_SOURCE_DISCOVERY],
        "unavailable_sources_truncated": len(unavailable) > MAX_SOURCE_DISCOVERY,
        "acquisition_sources": acquisition, "source_discovery_projection": discovery}
    if sm.encoded_size(result) > 45000:
        raise RuleViolation("真实来源选项超出一次可完整呈现的读取；先整理本人材料物件，再重新读取。")
    return FunctionOutcome(result)


def _supports(entity):
    """Allocate capacities once across final origin-preserving body lot IDs."""
    body = [lot for lot in entity["matter"] if lot["slot"] == "body"]
    total = sum(lot["quantity"] for lot in body)
    entity["state"]["initial_structural_quantity"] = total
    for name in ("absorbent", "buoyant"):
        component = entity["components"].get(name)
        if component is None:
            continue
        capacity, remaining, supports = component["capacity"], component["capacity"], {}
        for index, lot in enumerate(body):
            amount = remaining if index == len(body)-1 else capacity * lot["quantity"] // total
            supports[lot["id"]] = {"quantity": lot["quantity"], "capacity": amount}
            remaining -= amount
        component["supports"] = supports


def fabricate(ctx, args):
    from .creative import check_delegation, register_instance, _finish_item, _barrier
    from .world import _public_self, _project_entity, viewport
    check_delegation(ctx, args)
    player = m.get_player(ctx)
    m.check_player_version(player, args["expected_player_version"])
    from . import domains
    domains.guard_action(ctx, {"entity_id": player["backpack_id"],
                              "material_ids": [row["entity_id"] for row in args["sources"]]}, topology=True)
    _, spec, target, costs = _plan(ctx, args)
    target = {key: target[key] for key in ("name", "material", "capabilities", "state", "matter", "components", "composition")}
    sources, taken, selected, effects = {}, {}, set(), []
    target.update(id=m.identifier("object"), owner=player["id"], created_at=ctx.now,
                  created_by=player["id"], source="fabrication", container=None)
    declared_combustion = deepcopy(target["components"].get("combustion"))
    target = m.normalize_entity(target)
    for row in args["sources"]:
        eid = row["entity_id"]
        if eid not in sources:
            sources[eid] = m.get_entity(ctx, eid)
        source = sources[eid]
        if source["version"] != row["expected_entity_version"]:
            raise StateConflict("制造来源已变化；原批次没有替换，重新读取成型选项。")
        key = eid, row["lot_id"]
        if key in selected:
            raise RuleViolation("同一实际来源批次不能重复填写。")
        selected.add(key)
        lot = next((lot for lot in eligible_lots(ctx, player, source) if lot["id"] == row["lot_id"]), None)
        if lot is None or lot["material"] not in costs or row["quantity_u"] > lot["quantity"]:
            raise RuleViolation("所选真实材料批次不可用、类型不符或余量不足；没有消耗。")
        taken[lot["material"]] = taken.get(lot["material"], 0) + row["quantity_u"]
        effects.append(Transfer(eid, lot["id"], target["id"], row["quantity_u"], "body"))
    if taken != costs:
        raise RuleViolation("实际来源必须精确满足成型配方，不能少料、多料或用同名材料替代。")
    with commit.candidate_batch(ctx):
        frame = Frame(registry_for([target, *sources.values()]), {target["id"]: target, **sources},
                      bind_material=bind_material, mixture_capacity=mixture_capacity)
        try:
            frame.apply(effects)
            formed = frame.objects[target["id"]]
            for lot in formed["matter"]:
                lot["role"] = "structural"
            if declared_combustion is not None:
                formed["components"]["combustion"] = declared_combustion
            _supports(formed)
            # Birth admission uses the real formed body, never empty-body load.
            _barrier(formed, spec, ctx=ctx, allow_unknown=True)
            frame.objects[target["id"]] = m.normalize_entity(formed)
            frame.verify_balance()
        except ValueError as exc:
            raise RuleViolation(str(exc)) from exc
        commit.commit_frame(ctx, frame)
        target = m.get_entity(ctx, target["id"])
        for eid in sources:
            _finish_item(ctx, m.get_entity(ctx, eid), target)
        backpack = m.get_entity(ctx, player["backpack_id"])
        commit.contain(ctx, backpack, target)
        target = m.get_entity(ctx, target["id"])
        register_instance(ctx, target)
        commit.record_change(ctx, "真实材料成型：" + target["name"] + "；来源余量保留。",
                             x=player["x"], y=player["y"], entity_id=target["id"], action="fabricate")
    player = m.get_player(ctx)
    return FunctionOutcome({"player": _public_self(player), "entity": _project_entity(ctx, player, m.get_entity(ctx, target["id"])),
        "sources": [{"id": eid, "version": m.get_entity(ctx, eid)["version"]} for eid in sources],
        "changes": ["按精确配方成型；真实批次来源和余量已保存。"], "viewport": viewport(ctx, {})})


def specs():
    from .creative import _member
    return (FunctionSpec("grove.fabrication_options", fabrication_options,
        {"type": "object", "additionalProperties": False, "required": ["definition_ref", "type_id"],
         "properties": {"definition_ref": ID, "type_id": ARGS["properties"]["type_id"]}},
        version=3, access="read", authorize=_member, description="Read one exact cost_u recipe, eligible own lots and missing totals, bounded rejected-source reasons and authorized nearby acquisition sources. Discovery is not own available stock or an action permission; inspect its actual action before acting. No source is reserved or automatically selected."),
        FunctionSpec("grove.fabricate", fabricate, ARGS, authorize=_member,
        description="Atomically form one published cost_u body from1..8 exact own observed lots; pass every source version, exact quantities and placement inventory. Preserves canonical identity, origins and remainders. No new material is generated; a constructed structural body is not a disassembly source."))
