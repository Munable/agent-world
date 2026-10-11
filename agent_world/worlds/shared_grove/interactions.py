"""Explicit local installation of immutable, bounded contact declarations.

Installation changes a container's rule references, never the definition or its
material balance. Ordinary transfers and ticks still own actual consequences.
"""
from __future__ import annotations

from agent_world.errors import PermissionDenied, RuleViolation, StateConflict
from . import model as m, commit, contact
from .definitions import DEFINITIONS, execution_semantics

ACTIONS = frozenset({"install_interaction", "uninstall_interaction"})


def _eligible(ctx, player, entity):
    from .world import _accessible
    from .creative import guard_ordinary_action
    _accessible(ctx, player, entity)
    guard_ordinary_action(ctx, "install_interaction", entity)
    if entity["lifecycle"] != "intact" or "container" not in entity["components"]:
        raise RuleViolation("交互定义只能安装或卸载于完好的实际容器。")
    if "personal" in entity["components"] or "personal" in entity["capabilities"]:
        raise RuleViolation("个人保护背包本身不能安装交互定义。")
    if not entity["state"]["open"]:
        raise RuleViolation("先打开容器，再明确安装或卸载交互定义。")
    if entity["state"]["burning"]:
        raise RuleViolation("燃烧容器不能安装或卸载交互定义。")
    x, y = m.entity_location(ctx, entity)
    if entity.get("owner") is None and m.is_safe(ctx, x, y):
        raise RuleViolation("出生安全区的公共物件不能安装或卸载交互定义。")


def _definition(ctx, reference):
    if not isinstance(reference, str) or not 1 <= len(reference) <= 128:
        raise RuleViolation("请选择明确的不可变definition_ref。")
    record = ctx.get_state(DEFINITIONS, reference)
    if not record:
        raise RuleViolation("交互定义不存在；先发表或读取真实definition_ref。")
    execution_semantics(record)
    rows = [row for row in record["definition"].get("contact_reactions", [])
            if row.get("activation", "material") == "installed"]
    if not rows:
        raise RuleViolation("此定义没有activation=installed的局部交互；材料携带规则不能借安装改变激活范围。")
    return record, rows


def apply(ctx, player, entity, args):
    """Recheck actual authority and CAS, then commit only local references."""
    if args["action"] not in ACTIONS:
        raise RuleViolation("未知的交互安装动作。")
    m.check_player_version(m.get_player(ctx, player["id"]), args["expected_player_version"])
    current = m.get_entity(ctx, entity["id"])
    if current["version"] != args.get("expected_target_version"):
        raise StateConflict("安装目标已变化；读取真实容器后重新选择。")
    _eligible(ctx, player, current)
    references = list(contact.installed_references(current))
    reference = args.get("definition_ref")
    if args["action"] == "install_interaction":
        _definition(ctx, reference)
        if reference in references:
            return "此交互定义已安装，未重复创建规则或材料。", {
                "action": args["action"], "definition_ref": reference,
                "installed_definitions": references, "already_installed": True}
        if len(references) >= contact.MAX_INSTALLED_DEFINITIONS:
            raise RuleViolation("此容器最多安装2份交互定义；先明确卸载一份。")
        references.append(reference)
        references.sort()
        current["state"]["interaction_definitions"] = references
        contact.installed_candidates(ctx, current)
        # Admission follows the same reachable-output/slot/full-step path as
        # actual incoming matter; no zero lot or free input is installed.
        contact.reserve(ctx, current)
        summary = "已安装局部交互定义；仅当前容器内的实际材料在后续逻辑步参与。"
    else:
        if reference not in references:
            raise RuleViolation("该交互定义不在此容器的真实安装列表中。")
        references.remove(reference)
        if references:
            current["state"]["interaction_definitions"] = references
        else:
            current["state"].pop("interaction_definitions", None)
        summary = "已卸载局部交互定义；已形成材料与其他实际后果保持。"
    commit.save_entity(ctx, current)
    return summary, {"action": args["action"], "definition_ref": reference,
                     "installed_definitions": references, "quantity_changed": False}


def options(ctx, player, entity):
    """Describe installation without eagerly reading the publication directory."""
    from .world import _access_observation, INTERACT_ARGS
    from copy import deepcopy
    if "container" not in entity["components"] or "personal" in entity["components"]:
        return []
    try:
        _eligible(ctx, player, entity)
        reason = None
    except (PermissionDenied, RuleViolation) as exc:
        reason = str(exc)
    installed = list(contact.installed_references(entity))
    choices = [{"definition_ref": ref,
                "name": (ctx.get_state(DEFINITIONS, ref) or {}).get("definition", {}).get("name", ref)}
               for ref in installed]
    rows = []
    for action, label, blocked in (
            ("install_interaction", "安装交互定义",
             "此容器的2份交互安装位已满。" if len(installed) >= contact.MAX_INSTALLED_DEFINITIONS else None),
            ("uninstall_interaction", "卸载交互定义", "此容器尚未安装交互定义。" if not installed else None)):
        unavailable = reason or blocked
        properties = {key: deepcopy(INTERACT_ARGS["properties"][key]) for key in
                      ("action", "target_id", "expected_player_version", "expected_target_version", "definition_ref")}
        properties["action"] = {"const": action}
        rows.append({"action": action, "label": label, "function_id": "grove.interact",
                     "available": unavailable is None, "reason": unavailable,
                     "required": ["definition_ref", "expected_target_version"],
                     "arguments": {"action": action, "target_id": entity["id"],
                                   "expected_target_version": entity["version"]},
                     "input_schema": {"type": "object", "properties": properties,
                                      "required": list(properties), "additionalProperties": False},
                     "preconditions": [_access_observation(ctx, player, entity)],
                     "use": "明确安装只影响此容器的真实contained材料；卸载不回滚已经形成的后果，不改变已发表规则。",
                     "installed_definitions": installed,
                     **({"choices": {"interactions": choices}} if action == "uninstall_interaction" else
                        {"discovery": {"list_function": "grove.definition_list", "limit": 8,
                                       "read_function": "grove.definition_read", "required_activation": "installed"}})})
    return rows
