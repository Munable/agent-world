"""Bounded observations of current rules; never choose or execute an action."""
from __future__ import annotations

from copy import deepcopy

from agent_world.errors import PermissionDenied, RuleViolation
from agent_world.simulation.programs import evaluate, plan_reaction
from agent_world.simulation import matter as sm

from . import model as m
from .definitions import REGISTRY, registry_for, instance_definition as _type, material_identity as _material, contained_quantities as _quantities, reaction_available_quantities

MAX_REACTION_OBSERVATIONS = 8


def _references(value):
    if isinstance(value, dict):
        if len(value) == 1 and next(iter(value)) in {
            "field", "param", "quantity", "concentration", "mechanical", "event", "contract", "object"
        }:
            yield value
        else:
            for child in value.values():
                yield from _references(child)
    elif isinstance(value, list):
        for child in value:
            yield from _references(child)


def _visible_context(ctx, player, entity, pack, *, params=None):
    from .creative import _context
    from .world import _accessible

    context = _context(entity, params=params, ctx=ctx, definition=pack)
    # Rule execution observes around its anchor. Presentation must also respect
    # this reader's radius, so an anchor eight tiles away cannot reveal sixteen.
    for eid in list(context["objects"]):
        target = m.get_entity(ctx, eid, required=False)
        try:
            if target is None:
                raise RuleViolation("不可观察的公共事实。")
            _accessible(ctx, player, target, near=False)
            if target["owner"] is None and not target["state"]["open"] and "container" in target["components"]:
                raise RuleViolation("关闭公共容器的内部规则状态未公开。")
        except (PermissionDenied, RuleViolation):
            context["objects"].pop(eid, None)
    return context


def condition_observation(expression, context):
    """Use the real evaluator; unknown events are not a synthetic 'none' event."""
    references = list(_references(expression))
    if any("event" in reference for reference in references):
        return {"known": False, "satisfied": None, "reason": "event_required",
                "meaning": "事件条件须有实际事件输入；当前物件快照不能判断。"}
    facts = []
    for reference in references:
        if any(row["reference"] == reference for row in facts):
            continue
        try:
            actual = evaluate(reference, context)
            row = {"reference": deepcopy(reference), "known": True, "actual": actual}
        except ValueError:
            row = {"reference": deepcopy(reference), "known": False}
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
                  checks_meaning="子比较各自的当前快照；整体结果遵循原表达式的all/any/not组合，不是逐项必须满足的清单。")
    return result


def action_condition_observations(ctx, player, entity):
    """Default-input predicates are observations, not submitted-input admission."""

    pack, spec = _type(ctx, entity)
    if not spec:
        return {}
    result = {}
    for action in spec["actions"]:
        params = {name: field["default"] for name, field in action["parameters"].items()}
        context = _visible_context(ctx, player, entity, pack, params=params)
        observation = condition_observation(action.get("when", True), context)
        observation.update(input_scope="parameter_defaults", submitted_input_rechecked=True)
        result[action["id"]] = observation
    if "contract" in spec:
        context = _visible_context(ctx, player, entity, pack)
        result["deliver"] = condition_observation(spec["contract"].get("delivery_when", True), context)
    return result


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
            "meaning": "保存的上次反应结果；last_tick是保存的过程字段，不证明当前生产或当前仍有产物。"}
    process["observed_tick"] = ctx.get_state(m.META, "clock", {}).get("sequence", 0)
    process["steps_meaning"] = "累计发生反应的逻辑步数，不是本次目标进度。"

    quantities = _quantities(entity)
    available = reaction_available_quantities(entity)
    capacity = sm.liquid_capacity(entity)
    actual_total = sm.quantity(entity, slot="contained")
    result = {"material_definitions": deepcopy(binding.get("materials", {})), "process": process,
        "reaction_inputs": {"scope": "this_entity_contained_lots_only", "unit": "logical_matter",
            "quantities": quantities, "available_quantities": available,
            "contained_quantity": actual_total, "capacity": capacity,
            "whole_children_are_inputs": False,
            "meaning": "仅本物件contained材料槽；自身body和完整子物件的材料不计入。可用量排除燃烧与托管材料。"}}
    reactions = [(rule, index, effect) for rule in sorted(spec["rules"], key=lambda value: value["id"])
                 for index, effect in enumerate(rule["effects"]) if effect["op"] == "react"]
    result["reaction_observation_count"] = len(reactions)
    if not detailed:
        result["reaction_observations_projection"] = "当前材料与历史过程摘要；inspect才返回当前反应条件和材料缺口。"
        return result

    context = _visible_context(ctx, player, entity, pack)
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
            "meaning": "单条声明基于当前可观察快照的材料与条件；不是已执行反应，不预测通道竞争、先前效果或下一逻辑步结果。"})
    result["reaction_observations"] = observations
    result["reaction_observations_truncated"] = len(reactions) > MAX_REACTION_OBSERVATIONS
    if result["reaction_observations_truncated"]:
        result["reaction_observations_projection"] = "最多展示8条；其余声明须读取此物件固定引用的完整定义，不表示不存在。"
    return result
