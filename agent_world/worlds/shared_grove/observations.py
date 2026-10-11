"""Bounded observations of current rules; never choose or execute an action."""
from __future__ import annotations

from copy import deepcopy

from agent_world.errors import PermissionDenied, RuleViolation
from agent_world.simulation.declarations import has_event
from agent_world.simulation.programs import evaluate, plan_reaction, MECHANICAL_DEFAULTS
from agent_world.simulation import matter as sm
from agent_world.simulation.facts import reference_semantics

from . import model as m
from .definitions import REGISTRY, registry_for, instance_definition as _type, material_identity as _material, contained_quantities as _quantities, reaction_available_quantities, mixture_capacity

MAX_REACTION_OBSERVATIONS = 8
MAX_AUTOMATIC_RULE_OBSERVATIONS = 8


def _references(value):
    if isinstance(value, dict):
        if len(value) == 1 and next(iter(value)) in {
            "field", "param", "quantity", "concentration", "mechanical", "physical", "event", "contract", "object"
        }:
            yield value
        else:
            for child in value.values():
                yield from _references(child)
    elif isinstance(value, list):
        for child in value:
            yield from _references(child)


def _visible_context(ctx, player, entity, pack, *, objects=None):
    from .creative import _context
    from . import world_facts
    from .world import _accessible

    # Resolve the definition's fixed object references once in this read view.
    # Context construction and reader visibility check the same actual entities.
    targets = objects if objects is not None else world_facts.freeze(ctx, {entity["id"]: entity})
    context = _context(entity, ctx=ctx, definition=pack, objects=targets)
    if entity.get("owner") != player["id"]:
        public = world_facts.facts(entity, targets, external=True)
        context["physical"] = {key.removeprefix("physical."): value for key, value in public.items() if key.startswith("physical.")}
    # Rule execution observes around its anchor. Presentation must also respect
    # this reader's radius, so an anchor eight tiles away cannot reveal sixteen.
    for eid in list(context["objects"]):
        target = targets[eid]
        try:
            if target is None:
                raise RuleViolation("不可观察的公共事实。")
            _accessible(ctx, player, target, near=False)
        except (PermissionDenied, RuleViolation):
            context["objects"].pop(eid, None)
    return context


def condition_observation(expression, context):
    """Use the real evaluator; unknown events are not a synthetic 'none' event."""
    references = list(_references(expression))
    if any("event" in reference for reference in references):
        return {"known": False, "satisfied": None, "reason": "event_required",
                "meaning": "须有实际事件；物件快照不能判断。"}
    facts = []
    for reference in references:
        if any(row["reference"] == reference for row in facts):
            continue
        try:
            actual = evaluate(reference, context)
            row = {"reference": deepcopy(reference), "known": True, "actual": actual}
        except ValueError:
            row = {"reference": deepcopy(reference), "known": False}
        semantics = reference_semantics(reference)
        if semantics is not None:
            row["semantics"] = semantics
        facts.append(row)
    try:
        satisfied = bool(evaluate(expression, context))
        result = {"known": True, "satisfied": satisfied}
    except ValueError:
        result = {"known": False, "satisfied": None, "reason": "unavailable_fact_or_expression"}
    checks = []

    def comparisons(value, path="when"):
        if not isinstance(value, dict):
            return
        if value.get("op") in {"eq", "ne", "lt", "lte", "gt", "gte"}:
            row = {"path": path, "operator": value["op"],
                   "left": deepcopy(value["left"]), "right": deepcopy(value["right"])}
            for side in ("left", "right"):
                semantics = reference_semantics(value[side])
                if semantics is not None and semantics.get("unit") is not None:
                    row[side + "_semantics"] = semantics
            try:
                row.update(known=True, actual_left=evaluate(value["left"], context),
                           actual_right=evaluate(value["right"], context), satisfied=bool(evaluate(value, context)))
            except ValueError:
                row.update(known=False, satisfied=None)
            checks.append(row)
        elif value.get("op") in {"all", "any"}:
            for index, child in enumerate(value["args"]):
                comparisons(child, path + ".args." + str(index))
        elif value.get("op") == "not":
            comparisons(value["value"], path + ".value")

    comparisons(expression)
    result.update(expression=deepcopy(expression), facts=facts, checks=checks,
                  checks_meaning="子比较快照；成立条件按原all/any/not组合。")
    return result


def action_condition_observations(ctx, player, entity):
    """Default-input predicates are observations, not submitted-input admission."""

    pack, spec = _type(ctx, entity)
    if not spec or not (spec["actions"] or "contract" in spec):
        return {}
    result = {}
    context = _visible_context(ctx, player, entity, pack)
    for action in spec["actions"]:
        params = {name: field["default"] for name, field in action["parameters"].items()}
        observation = condition_observation(action.get("when", True), {**context, "params": params})
        observation.update(input_scope="parameter_defaults", submitted_input_rechecked=True)
        result[action["id"]] = observation
    if "contract" in spec:
        result["deliver"] = condition_observation(spec["contract"].get("delivery_when", True), context)
    return result


def _location_objects(ctx, entity):
    """Compact deployment needs only the bounded ancestor path."""
    objects = {entity["id"]: entity}
    current = entity
    while (parent := current.get("physical_parent")) and len(objects) < sm.MAX_TREE_DEPTH:
        parent_id = parent["id"]
        if parent_id in objects or (current := m.get_entity(ctx, parent_id, required=False)) is None:
            break
        objects[parent_id] = current
    return objects


def _deployment_observation(ctx, entity, spec, objects):
    from . import activity, world_facts

    admitted = entity["id"] in activity.meta_get(ctx, "creative_instances", [])
    intact = entity["lifecycle"] == "intact" and not entity["state"].get("retired")
    errored = entity["state"]["program"]["process"]["status"] == "error"
    runnable = admitted and intact and not errored
    public = world_facts.public_location(entity, objects) is not None
    mechanical = "mechanical" in spec
    blockers = []
    for blocked, reason in ((not mechanical, "no_mechanical_definition"),
                            (not admitted, "not_admitted"), (not intact, "not_intact"),
                            (errored, "program_error"), (not public, "no_public_location")):
        if blocked:
            blockers.append(reason)
    return {"publicly_placed": public, "admitted": admitted,
            "ordinary_rules_enabled": runnable and any(not has_event(rule) for rule in spec["rules"]),
            "public_event_input_available": runnable and public,
            "mechanical_solver_eligible": runnable and public and mechanical,
            "mechanical_blockers": blockers,
            "meaning": "自身规则可随身评估；事件／机械需公开。资格非事件或运动证据。"}


def _mechanical_observation(binding, spec, deployment, observed_tick):
    if "mechanical" not in binding:
        return None
    last_solved = deepcopy(binding.get("mechanical_last_solved"))
    if last_solved is not None:
        last_solved.update(historical=True,
            meaning="最近求解输出变化的保存步；非每步记录。")
    request = deepcopy(binding.get("drive_request"))
    role = spec["mechanical"]["role"]
    result = {"role": role, "stored_state": deepcopy(binding["mechanical"]),
            "angle_unit": "logical_turns",
            "role_meaning": {"source": "动力源只提供角动力，不产生直线行程。", "gear": "齿轮只传递角动力，不产生直线行程。",
                             "actuator": "执行器把角动力转换成有限直线行程。"}[role],
            "last_solved": last_solved, "drive_request": request,
            "solver_eligible": deployment["mechanical_solver_eligible"],
            "stored_output_is_history": True, "observed_tick": observed_tick,
            "pending_next_step": request is not None and request["applies_from_step"] > observed_tick,
            "meaning": "保存状态非实时运动；空标记非未求解证据，请求生效步已到非已求解证据。"}
    if role == "actuator":
        result["travel"] = spec["mechanical"].get("travel", MECHANICAL_DEFAULTS["travel"])
        result["pitch"] = spec["mechanical"].get("pitch", MECHANICAL_DEFAULTS["pitch"])
    return result


def _automatic_rule_observations(spec, context):
    observations = []
    for rule in sorted(spec["rules"], key=lambda value: value["id"])[:MAX_AUTOMATIC_RULE_OBSERVATIONS]:
        event_required = has_event(rule)
        row = {"rule_id": rule["id"], "phase": rule["phase"],
               "input_kind": "public_event" if event_required else "logical_step",
               "actual_event_required": event_required,
               "condition": condition_observation(rule["when"], context),
               "effect_ops": list(dict.fromkeys(effect["op"] for effect in rule["effects"])),
               "snapshot_only": True}
        drive_inputs = [{"effect_index": index,
                         "observation": condition_observation(effect["enabled"], context),
                         "snapshot_only": True}
                        for index, effect in enumerate(rule["effects"]) if effect["op"] == "drive"]
        if drive_inputs:
            row["drive_inputs"] = drive_inputs
        observations.append(row)
    return observations


def program_observation(ctx, entity, *, detailed=False, player=None):
    """Return replacement public fields for creative.projection's binding copy."""

    pack, spec = _type(ctx, entity)
    if not spec:
        return {}
    player = player or m.get_player(ctx)
    binding = entity["state"]["program"]
    process = deepcopy(binding["process"])
    historical = process.pop("last_reactions", None)
    if historical is not None:
        process["last_reaction"] = {
            "source_last_tick": process.get("last_tick"), "reactions": historical,
            "historical": True, "current_production": False,
            "meaning": "上次反应历史；非当前生产或库存。"}
    process["observed_tick"] = ctx.get_state(m.META, "clock", {}).get("sequence", 0)
    # These short per-object labels travel in state, creative and public anchors.
    # Full execution policy belongs to handbook/rule_catalog, not every copy.
    process["steps_meaning"] = "累计反应步数，非目标进度。"
    process["status_meaning"] = "保存的过程标记；error为错误门禁，其他值不证明整个装置运行／停止。"
    process["last_tick_meaning"] = "成型／显式行动／反应／错误步；不是最近自动声明评估或机械求解步。"

    from . import world_facts
    # Detailed conditions and deployment share one frozen authorized forest.
    # Compact reads need only the bounded ancestor path, not descendants or inputs.
    objects = world_facts.freeze(ctx, {entity["id"]: entity}) if detailed else _location_objects(ctx, entity)
    context = _visible_context(ctx, player, entity, pack, objects=objects) if detailed else None
    deployment = _deployment_observation(ctx, entity, spec, objects)
    quantities = context["quantities"] if context is not None else _quantities(entity)
    available = reaction_available_quantities(entity)
    capacity = mixture_capacity(entity)
    capacity = sm.liquid_capacity(entity) if capacity is None else capacity
    actual_total = sum(quantities.values())
    result = {"material_definitions": deepcopy(binding.get("materials", {})), "process": process,
        "automatic_rule_count": len(spec["rules"]), "explicit_action_count": len(spec["actions"]),
        "deployment": deployment,
        "reaction_inputs": {"scope": "this_entity_contained_lots_only", "unit": "logical_matter",
            "quantities": quantities, "available_quantities": available,
            "contained_quantity": actual_total, "capacity": capacity,
            "whole_children_are_inputs": False,
            "meaning": "仅自身contained；body／整件子物件不混入，燃烧／托管不可用。"}}
    mechanical = _mechanical_observation(binding, spec, deployment, process["observed_tick"])
    if mechanical is not None:
        result["mechanical_observation"] = mechanical
    reactions = [(rule, index, effect) for rule in sorted(spec["rules"], key=lambda value: value["id"])
                 for index, effect in enumerate(rule["effects"]) if effect["op"] == "react"]
    result["reaction_observation_count"] = len(reactions)
    if not detailed:
        result["reaction_observations_projection"] = "当前contained与反应历史；inspect读取条件／缺口。"
        result["automatic_rules_projection"] = "inspect读取自动声明条件／动力输入；显式actions另提交。"
        return result

    result["automatic_rules"] = _automatic_rule_observations(spec, context)
    result["automatic_rules_truncated"] = len(spec["rules"]) > MAX_AUTOMATIC_RULE_OBSERVATIONS
    result["automatic_rules_meaning"] = "当前快照非执行／预测；事件需真实日志。drive输入是启停读数；when=false跳过，unknown保留控制。"

    material_defs = {key: {"load_per_u": value.load_per_u} for key, value in REGISTRY.materials.items()}
    material_defs.update(pack["materials"])
    for symbol, material_id in binding.get("material_aliases", {}).items():
        metadata = binding.get("materials", {}).get(material_id)
        if metadata is not None:
            material_defs[symbol] = {"load_per_u": metadata["load_per_u"]}
    registry = registry_for(entity)
    for symbol in available:
        material_id = symbol if "#" in symbol else _material(entity, symbol)
        if material_id in registry.materials:
            material_defs[symbol] = {"load_per_u": registry.materials[material_id].load_per_u}
    # The planner receives the same occupancy allowance: unusable lots still
    # occupy the vessel. Each observation is isolated, not a multi-rule solver.
    planning_capacity = sm.reaction_capacity(capacity, actual_total, sum(available.values()))
    observations = []
    for rule, index, effect in reactions[:MAX_REACTION_OBSERVATIONS]:
        condition = condition_observation(rule["when"], context)
        planned = plan_reaction(effect, available, material_defs, planning_capacity)
        inputs = [{"symbol": symbol, "material_id": _material(entity, symbol),
                   "per_extent": amount, "available": available.get(symbol, 0),
                   "missing_for_one_extent": max(0, amount - available.get(symbol, 0))}
                  for symbol, amount in effect["inputs"].items()]
        blockers = []
        if not condition["known"]:
            blockers.append("condition_unknown")
        elif not condition["satisfied"]:
            blockers.append("condition_unmet")
        if any(row["missing_for_one_extent"] for row in inputs):
            blockers.append("insufficient_inputs")
        if planned["reason"] == "capacity" and planned["extent"] == 0:
            blockers.append("capacity")
        if process["status"] == "error":
            blockers.append("program_error")
        if entity["lifecycle"] != "intact":
            blockers.append("not_intact")
        observations.append({"rule_id": rule["id"], "phase": rule["phase"], "effect_index": index,
            "condition": condition, "inputs": inputs, "outputs_per_extent": deepcopy(effect["outputs"]),
            "rate": effect["rate"], "channel": effect["channel"], "mode": effect["mode"],
            "isolated_extent": planned["extent"], "capacity_limited": planned["reason"] == "capacity",
            "capacity": capacity, "contained_quantity": actual_total,
            "blockers": blockers, "snapshot_only": True,
            "meaning": "单项快照，非执行或预测；未合算竞争与先前效果。"})
    result["reaction_observations"] = observations
    result["reaction_observations_truncated"] = len(reactions) > MAX_REACTION_OBSERVATIONS
    if result["reaction_observations_truncated"]:
        result["reaction_observations_projection"] = "最多展示8条；其余声明须读取此物件固定引用的完整定义，不表示不存在。"
    return result
