"""Finite River Camp continuation, composed entirely with the public SDK.

This is a separate world, not a migration of the frozen River Camp experiment.
Two fixed depot shipments and one flood salvage crate are the only new stock.
The second shipment triggers a declared story event, not real weather or a day.
Factory authority arguments initialize fresh instances only, as in river_camp.
"""
from __future__ import annotations

from dataclasses import replace

from agent_world import EventSpec, FunctionOutcome, FunctionSpec, StateRule
from agent_world.errors import RuleViolation, StateConflict
from examples import river_camp as base


SCOPE, KEY = "campaign:world", "progress"
SHIPMENTS = {"timber": {"wood": 4, "rope": 2, "stone": 0},
             "quarry": {"wood": 2, "rope": 1, "stone": 2}}
STORE_COST = {"wood": 3, "rope": 1, "stone": 1}
SALVAGE = {"wood": 2, "rope": 1, "stone": 0}
REPAIRS = {
    "quick": {"cost": {"wood": 2, "rope": 1, "stone": 1},
              "effects": {"passage": "cargo", "stability": 2, "exposure": 1}},
    "anchored": {"cost": {"wood": 1, "rope": 2, "stone": 2},
                 "effects": {"passage": "cargo", "stability": 3, "exposure": 0}},
}
GOALS = {"supply": "完成至少一趟补给", "build": "亲手建成共享仓库", "watch": "观察水势并亲手完成洪水后修复"}
BOOL = {"type": "boolean"}
NULL_ROLE = {"anyOf": [base.ROLE, {"type": "null"}]}
CONTRIBUTIONS = base.object_schema({"shipments": {"type": "integer", "minimum": 0, "maximum": 2},
                                  "built": BOOL, "observed": BOOL, "repaired": BOOL})
PERSON = base.object_schema({"choice": {"enum": [None, *GOALS]}, "contributions": CONTRIBUTIONS})
SHIPMENT = base.object_schema({"claimed": BOOL, "actor_role_id": NULL_ROLE})
ACTIVITY = base.object_schema({"actor_role_id": base.ROLE, "action": {"type": "string"},
                              "at": {"type": "number"}, "details": {"type": "object"}})
PROGRESS = base.object_schema({
    "stage": {"enum": ["bridge", "supply", "flood", "repaired"]},
    "shipments": base.object_schema({name: SHIPMENT for name in SHIPMENTS}),
    "storehouse": BOOL, "observed_by": {"type": "array", "items": base.ROLE, "maxItems": 16, "uniqueItems": True},
    "flood_applied": BOOL, "rescue_claimed": BOOL, "repair_plan": {"enum": [None, *REPAIRS]},
    "participants": {"type": "object", "additionalProperties": PERSON, "maxProperties": 16},
    "activity": {"type": "array", "items": ACTIVITY, "maxItems": 64},
})
GUARD = {"expected_campaign_version": base.VERSION}
RESOURCE_GUARD = {**GUARD, "inventory_version": base.VERSION}
ROUTE_GUARD = {**RESOURCE_GUARD, "route_version": base.VERSION}
CHOOSE_ARGS = base.object_schema({**GUARD, "choice": {"enum": list(GOALS)}})
SUPPLY_ARGS = base.object_schema({**ROUTE_GUARD, "shipment_id": {"enum": list(SHIPMENTS)}})
BUILD_ARGS = base.object_schema(RESOURCE_GUARD)
OBSERVE_ARGS = base.object_schema(GUARD)
SALVAGE_ARGS = base.object_schema(RESOURCE_GUARD)
REPAIR_ARGS = base.object_schema({**ROUTE_GUARD, "plan": {"enum": list(REPAIRS)}})


def _fresh_person():
    return {"choice": None, "contributions": {"shipments": 0, "built": False, "observed": False, "repaired": False}}


def _record(ctx, args):
    record = ctx.get_state_record(SCOPE, KEY)
    if record["version"] != args["expected_campaign_version"]:
        raise StateConflict("campaign changed; inspect and retry with its current version")
    return record


def _resource(ctx, args, key):
    record = ctx.get_state_record("camp:world", key)
    if record["version"] != args[key + "_version"]:
        raise StateConflict(key + " changed; inspect and retry with its current version")
    return record


def _person(progress, actor):
    return progress["participants"].setdefault(actor, _fresh_person())


def _affordable(stock, cost):
    return all(stock[name] >= amount for name, amount in cost.items())


def _spend(stock, cost):
    if not _affordable(stock, cost):
        raise RuleViolation("the shared stock cannot fund this action")
    return {name: stock[name] - cost[name] for name in stock}


def _completed(person):
    choice, facts = person["choice"], person["contributions"]
    return {None: False, "supply": facts["shipments"] >= 1,
            "build": facts["built"], "watch": facts["observed"] and facts["repaired"]}[choice]


def _projection(ctx, record):
    progress = record["value"]
    inventory = ctx.get_state_record("camp:world", "inventory")
    route = ctx.get_state_record("camp:world", "route")
    stock = inventory["value"]
    targets = [
        {"id": "supplies", "label": "两趟外部补给均已抵达", "done": all(item["claimed"] for item in progress["shipments"].values())},
        {"id": "storehouse", "label": "共享仓库已建成", "done": progress["storehouse"]},
        {"id": "restored_route", "label": "涨水后已恢复安全货运", "done": progress["repair_plan"] is not None and base.goal_reached(route["value"])},
    ]
    role_id = ctx.actor_role_id
    own = progress["participants"].get(role_id, _fresh_person())
    actions = []

    def action(function, label, arguments, reason=None):
        actions.append({"function": function, "label": label, "arguments": arguments,
                        "enabled": reason is None, "blocked_reason": reason})

    guard = {"expected_campaign_version": record["version"]}
    resource = {**guard, "inventory_version": inventory["version"]}
    route_guard = {**resource, "route_version": route["version"]}
    for choice, label in GOALS.items():
        action("campaign.choose_goal", "选择个人目标：" + label, {**guard, "choice": choice},
               "个人目标已选择；完成情况由实际贡献决定" if own["choice"] is not None else None)
    for name, shipment in progress["shipments"].items():
        reason = ("这批补给已领取" if shipment["claimed"] else
                  "涨水后外部补给阶段已结束" if progress["flood_applied"] else
                  "先恢复稳定度至少2、暴露度至多1的货运通行" if not base.goal_reached(route["value"]) else None)
        label = "领取木料补给（4木/2绳）" if name == "timber" else "领取石场补给（2木/1绳/2石）"
        if sum(item["claimed"] for item in progress["shipments"].values()) == 1 and not shipment["claimed"]:
            label += "；抵达将触发剧情涨水"
        action("campaign.supply", label, {**route_guard, "shipment_id": name}, reason)
    action("campaign.build_storehouse", "建共享仓库（3木/1绳/1石；防涨水损失）", resource,
           "仓库已建成" if progress["storehouse"] else
           "先领取至少一趟补给" if progress["stage"] == "bridge" else
           "库存不足3木/1绳/1石" if not _affordable(stock, STORE_COST) else None)
    action("campaign.observe_water", "观察水势与剧情涨水条件", guard,
           "你已记录水势观察" if own["contributions"]["observed"] else None)
    action("campaign.salvage_crate", "打捞外部应急箱（2木/1绳，仅一次）", resource,
           "应急箱已打捞" if progress["rescue_claimed"] else
           "应急箱只在剧情涨水后出现" if not progress["flood_applied"] else None)
    for plan, recipe in REPAIRS.items():
        cost = recipe["cost"]
        action("campaign.repair", ("快速修复" if plan == "quick" else "锚固修复") +
               f"（{cost['wood']}木/{cost['rope']}绳/{cost['stone']}石）", {**route_guard, "plan": plan},
               "洪水后修复已完成" if progress["repair_plan"] is not None else
               "仅在第二趟补给触发的剧情涨水后修复" if not progress["flood_applied"] else
               "库存不足以支付此修复方案" if not _affordable(stock, cost) else None)
    if not targets[0]["done"]:
        next_step = "恢复安全货运后领取两趟补给；第二趟之前建仓可防损失"
    elif not targets[2]["done"]:
        next_step = "打捞一次应急箱或使用现有库存修复洪水后的通行"
    elif not targets[1]["done"]:
        next_step = "通行已修复；仍需建成共享仓库，可打捞未领取的应急箱"
    else:
        next_step = "共同目标已达成；个人目标仍按各自真实贡献显示"
    personals = []
    for member in ctx.list_state("camp:members", limit=200)["items"]:
        person = progress["participants"].get(member["key"], _fresh_person())
        personals.append({"role_id": member["key"], **person, "label": GOALS.get(person["choice"], "尚未选择个人目标"),
                          "completed": _completed(person)})
    return {"version": record["version"], **{key: value for key, value in progress.items() if key != "participants"},
            "goal": {"text": "完成两趟补给、建仓，并在剧情涨水后恢复安全货运", "reached": all(item["done"] for item in targets), "targets": targets},
            "personals": personals, "available_actions": actions, "next": next_step}


def inspect(ctx, args):
    result = base.inspect(ctx, args).result
    campaign = _projection(ctx, ctx.get_state_record(SCOPE, KEY))
    return FunctionOutcome({**result, "route_goal_reached": result["goal_reached"],
                            "goal": campaign["goal"]["text"], "goal_reached": campaign["goal"]["reached"], "campaign": campaign})


def _commit(ctx, record, action, details):
    progress = record["value"]
    progress["activity"].append({"actor_role_id": ctx.actor_role_id, "action": action, "at": ctx.now, "details": details})
    progress["activity"] = progress["activity"][-64:]
    version = ctx.set_state(SCOPE, KEY, progress, expected_version=record["version"])
    result = {"campaign": _projection(ctx, {"value": progress, "version": version}),
              "inventory": ctx.get_state_record("camp:world", "inventory"), "route": ctx.get_state_record("camp:world", "route")}
    result.update(goal_reached=result["campaign"]["goal"]["reached"],
                  route_goal_reached=base.goal_reached(result["route"]["value"]))
    return FunctionOutcome(result, base._events(ctx, action, {"actor_role_id": ctx.actor_role_id, "at": ctx.now, **details}))


def accept(ctx, args):
    record = ctx.get_state_record(SCOPE, KEY)
    if record["value"]["stage"] != "bridge":
        raise RuleViolation("the first supply departure closed bridge planning; use campaign.repair after the flood")
    before_stock = ctx.get_state("camp:world", "inventory")
    outcome = base.accept(ctx, args)
    cost = {name: amount - outcome.result["inventory"][name] for name, amount in before_stock.items()}
    extra = _commit(ctx, record, "camp.accept", {"intent_id": args["intent_id"], "cost": cost, "route": outcome.result["route"]})
    result = {**outcome.result, "route_goal_reached": outcome.result["goal_reached"],
              "goal_reached": extra.result["goal_reached"], "campaign": extra.result["campaign"]}
    events = tuple(EventSpec(event.recipient_role_id, event.kind,
                            {**event.payload, "route_goal_reached": event.payload["goal_reached"], "goal_reached": result["goal_reached"]})
                   for event in outcome.events)
    return FunctionOutcome(result, events)


def _bridge_open(ctx):
    if ctx.get_state(SCOPE, KEY)["stage"] != "bridge":
        raise RuleViolation("bridge proposal stage closed; use declared campaign actions")


def propose(ctx, args):
    _bridge_open(ctx)
    return base.propose(ctx, args)


def judge(ctx, args):
    _bridge_open(ctx)
    return base.judge(ctx, args)


def choose_goal(ctx, args):
    record = _record(ctx, args)
    person = _person(record["value"], ctx.actor_role_id)
    if person["choice"] is not None:
        raise RuleViolation("a personal goal can be chosen only once")
    person["choice"] = args["choice"]
    return _commit(ctx, record, "campaign.choose_goal", {"choice": args["choice"]})


def supply(ctx, args):
    record = _record(ctx, args)
    progress = record["value"]
    inventory, route = _resource(ctx, args, "inventory"), _resource(ctx, args, "route")
    shipment = progress["shipments"][args["shipment_id"]]
    if shipment["claimed"] or progress["flood_applied"]:
        raise RuleViolation("this finite shipment cannot be collected again")
    if not base.goal_reached(route["value"]):
        raise RuleViolation("external supplies require safe cargo passage")
    shipment.update(claimed=True, actor_role_id=ctx.actor_role_id)
    _person(progress, ctx.actor_role_id)["contributions"]["shipments"] += 1
    stock = {name: inventory["value"][name] + amount for name, amount in SHIPMENTS[args["shipment_id"]].items()}
    progress["stage"] = "supply"
    details = {"shipment_id": args["shipment_id"], "source": "fixed external depot shipment", "received": SHIPMENTS[args["shipment_id"]]}
    if all(item["claimed"] for item in progress["shipments"].values()):
        loss = {"wood": 0, "rope": 0, "stone": 0} if progress["storehouse"] else {"wood": min(2, stock["wood"]), "rope": min(1, stock["rope"]), "stone": 0}
        stock = _spend(stock, loss)
        effects = {"passage": "blocked", "stability": max(0, route["value"]["stability"] - 1), "exposure": min(2, route["value"]["exposure"] + 1)}
        ctx.set_state("camp:world", "route", effects, expected_version=route["version"])
        progress.update(stage="flood", flood_applied=True)
        details.update(story_event="second-shipment high water", loss=loss, route=effects)
    ctx.set_state("camp:world", "inventory", stock, expected_version=inventory["version"])
    return _commit(ctx, record, "campaign.supply", details)


def build_storehouse(ctx, args):
    record = _record(ctx, args)
    progress = record["value"]
    inventory = _resource(ctx, args, "inventory")
    if progress["storehouse"] or progress["stage"] == "bridge":
        raise RuleViolation("build once, after at least one external shipment")
    ctx.set_state("camp:world", "inventory", _spend(inventory["value"], STORE_COST), expected_version=inventory["version"])
    progress["storehouse"] = True
    _person(progress, ctx.actor_role_id)["contributions"]["built"] = True
    return _commit(ctx, record, "campaign.build_storehouse", {"cost": STORE_COST})


def observe_water(ctx, args):
    record = _record(ctx, args)
    progress = record["value"]
    if ctx.actor_role_id in progress["observed_by"]:
        raise RuleViolation("this role already recorded its observation")
    progress["observed_by"].append(ctx.actor_role_id)
    _person(progress, ctx.actor_role_id)["contributions"]["observed"] = True
    return _commit(ctx, record, "campaign.observe_water", {
        "finding": "The second fixed shipment triggers story high water: it blocks the route and loses 2 wood/1 rope unless a storehouse already exists. This is not real weather or a day passing."})


def salvage_crate(ctx, args):
    record = _record(ctx, args)
    progress = record["value"]
    inventory = _resource(ctx, args, "inventory")
    if not progress["flood_applied"] or progress["rescue_claimed"]:
        raise RuleViolation("one independent external emergency crate is available only after the story flood")
    stock = {name: inventory["value"][name] + amount for name, amount in SALVAGE.items()}
    ctx.set_state("camp:world", "inventory", stock, expected_version=inventory["version"])
    progress["rescue_claimed"] = True
    return _commit(ctx, record, "campaign.salvage_crate", {"source": "one fixed external emergency crate, not bridge refunds", "received": SALVAGE})


def repair(ctx, args):
    record = _record(ctx, args)
    progress = record["value"]
    inventory, route = _resource(ctx, args, "inventory"), _resource(ctx, args, "route")
    if not progress["flood_applied"] or progress["repair_plan"] is not None:
        raise RuleViolation("repair once, after the second-shipment story flood")
    recipe = REPAIRS[args["plan"]]
    ctx.set_state("camp:world", "inventory", _spend(inventory["value"], recipe["cost"]), expected_version=inventory["version"])
    ctx.set_state("camp:world", "route", recipe["effects"], expected_version=route["version"])
    progress.update(stage="repaired", repair_plan=args["plan"])
    _person(progress, ctx.actor_role_id)["contributions"]["repaired"] = True
    return _commit(ctx, record, "campaign.repair", {"plan": args["plan"], **recipe})


def _state_access(ctx, scope, key, access):
    if scope == SCOPE:
        member = ctx.authorization_state("camp:members", ctx.actor_role_id) is not None
        return member and key == KEY and (access == "read" or ctx.function_id in {
            "camp.accept", "campaign.choose_goal", "campaign.supply", "campaign.build_storehouse",
            "campaign.observe_water", "campaign.salvage_crate", "campaign.repair"})
    if access == "write" and ctx.function_id.startswith("campaign."):
        if ctx.authorization_state("camp:members", ctx.actor_role_id) is None:
            return False
        keys = {"campaign.supply": {"inventory", "route"}, "campaign.build_storehouse": {"inventory"},
                "campaign.salvage_crate": {"inventory"}, "campaign.repair": {"inventory", "route"}}
        return scope == "camp:world" and key in keys.get(ctx.function_id, set())
    return base._state_access(ctx, scope, key, access)


def make_world(*, founder_role_id=None, judge_role_ids=()):
    original = base.make_world(founder_role_id=founder_role_id, judge_role_ids=judge_role_ids)

    def initialize(ctx):
        original.initialize(ctx)
        ctx.set_state(SCOPE, KEY, {"stage": "bridge", "shipments": {name: {"claimed": False, "actor_role_id": None} for name in SHIPMENTS},
                                 "storehouse": False, "observed_by": [], "flood_applied": False, "rescue_claimed": False,
                                 "repair_plan": None, "participants": {}, "activity": []}, expected_version=0)

    overrides = {"camp.inspect": (inspect, " Inspect campaign-derived common and personal goals, finite legal material sources, available action arguments and actual contributions. Cargo passage is only a route milestone; goal_reached means the whole campaign goal."),
                 "camp.accept": (accept, " Bridge planning closes permanently when the first campaign shipment departs; subsequent restoration uses campaign.repair. goal_reached means the common campaign goal, not just cargo passage."),
                 "camp.propose": (propose, " Only the initial bridge stage accepts free intentions. The first campaign shipment closes proposals; then use declared campaign actions. Historical pending intents remain visible but cannot execute."),
                 "camp.judge": (judge, " Only during initial bridge stage. After the first campaign shipment, new rulings are rejected and stored approvals cannot execute; use declared campaign actions.")}
    functions = tuple(replace(spec, handler=overrides[spec.name][0], description=spec.description + overrides[spec.name][1])
                      if spec.name in overrides else spec for spec in original.functions)
    additions = (
        ("campaign.choose_goal", choose_goal, CHOOSE_ARGS, "Choose supply, build or watch once for your own role. Completion is derived only from actual shared-world contributions; no client-supplied completed flag."),
        ("campaign.supply", supply, SUPPLY_ARGS, "Collect one named external depot shipment once, requiring safe cargo passage and current campaign/inventory/route versions. The second shipment atomically triggers declared story high water: route blocked, stability -1, exposure +1; no storehouse loses 2 wood/1 rope. Not real weather or elapsed days."),
        ("campaign.build_storehouse", build_storehouse, BUILD_ARGS, "Build once after the first shipment for 3 wood/1 rope/1 stone. If built before the second shipment it prevents flood inventory loss. Can also build after flood or repair; old bridge materials are never refunded."),
        ("campaign.observe_water", observe_water, OBSERVE_ARGS, "Record your own public water observation once and learn the second-shipment story event. This does not advance a clock, claim real weather or create materials."),
        ("campaign.salvage_crate", salvage_crate, SALVAGE_ARGS, "After story high water, collect one shared external emergency crate for 2 wood/1 rope. It is finite and independent of consumed bridge materials; inspect version before taking it."),
        ("campaign.repair", repair, REPAIR_ARGS, "Restore the flooded crossing once: quick costs 2 wood/1 rope/1 stone for cargo stability2 exposure1; anchored costs 1 wood/2 rope/2 stone for cargo stability3 exposure0. Current campaign/inventory/route versions required. Repair and common goal are separate: missing warehouse may still be built."),
    )
    return replace(original, world_id="river-camp-campaign", display_name="River Camp Campaign", initialize=initialize,
                   functions=functions + tuple(FunctionSpec(name, handler, schema, authorize=base._member, description=description)
                                               for name, handler, schema, description in additions),
                   state_rules=original.state_rules + (StateRule(SCOPE, KEY, PROGRESS),), state_authorizer=_state_access,
                   entry_instructions="Join with camp.join, then camp.inspect. Only a trusted configured or founder-authorized external judge can interpret free bridge intentions, using exact wood=2 foot/4 cargo, rope=stability+2-exposure, stone=stability-1. Only a proposer accepts their approved plan. Cargo stability>=2 exposure<=1 is a route milestone, NOT the campaign common goal. Before the first shipment, propose/judge/accept bridge plans; the first shipment closes that stage permanently. Choose a personal goal and use campaign.available_actions with current versions. Collect two one-off external depot shipments, build a storehouse and observe water. The second shipment triggers declared story high water: it blocks passage and loses 2 wood/1 rope unless a storehouse already exists. This is neither real weather nor an elapsed day. Repair once; a missing warehouse can still be built after repair. One post-flood external emergency crate supplies 2 wood/1 rope independently of spent bridge materials. Common completion requires both shipments, a warehouse and safe passage restored after the flood. Personal completion derives from your own contributions; no text or completed flag creates effects. Submitted intentions/ruling reasons/activity are public; optional private notes are owner/current member-judge only. Timed projects are separate commitments, not physical campaign work. Default WORLD grants no privileges; factory arguments initialize fresh instances only, never reset stored authority on restart. No resident model loop.")


WORLD = make_world()
