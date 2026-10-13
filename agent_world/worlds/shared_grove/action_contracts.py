"""Shared read-only preconditions and failures for the Grove action surfaces."""
from __future__ import annotations

from copy import deepcopy

from agent_world.errors import PermissionDenied, RuleViolation

from . import model as m


class Reason(str):
    """A display sentence carrying machine facts, never inferred from wording."""

    def __new__(cls, message, failure, *, retryable=False, retry_after_seconds=None):
        value = super().__new__(cls, message)
        value.failure = failure
        value.details = {"code": failure["code"], "failure": failure,
                         "action_precondition": failure["precondition"],
                         "read_requests": failure["read_requests"]}
        value.recovery = failure["recovery"]["strategy"]
        value.retryable = retryable
        value.retry_after_seconds = retry_after_seconds
        return value

    def __deepcopy__(self, memo):
        return Reason(str(self), deepcopy(self.failure, memo), retryable=self.retryable,
                      retry_after_seconds=self.retry_after_seconds)


def read_entity(entity_id):
    return {"function_id": "grove.inspect", "arguments": {"entity_id": entity_id}}


def condition(name, *, subject="target", entity_id=None, tile=None, known=True,
              satisfied=False, actual=None, required=None, **facts):
    reference = {"kind": "entity", "id": entity_id} if entity_id else (
        {"kind": "tile", **tile} if tile is not None else {"kind": "player", "role": "self"})
    value = {"condition": name, "subject": subject, "subject_ref": reference,
             "known": known, "satisfied": satisfied, "actual": actual if known else None,
             "required": required, **facts}
    if entity_id:
        value["entity_id"] = entity_id
    return value


def failure(code, message, name, *, subject="target", entity_id=None, tile=None,
            known=True, actual=None, required=None, reads=None,
            recovery="refresh_then_choose_action", instruction="读取当前事实，满足条件后重新选择行动。",
            retryable=False, retry_after_seconds=None, **facts):
    precondition = condition(name, subject=subject, entity_id=entity_id, tile=tile,
                             known=known, actual=actual, required=required, **facts)
    if reads is None:
        reads = [read_entity(entity_id)] if entity_id and known else [
            {"function_id": "map.viewport", "arguments": {}}]
    return Reason(message, {"code": code, "condition": name, "precondition": precondition,
        "subjects": [precondition["subject_ref"]], "read_requests": reads,
        "recovery": {"strategy": recovery, "instruction": instruction}},
        retryable=retryable, retry_after_seconds=retry_after_seconds)


def from_error(exc, *, subject="target", entity_id=None, tile=None, reads=None):
    details = getattr(exc, "details", None) or {}
    if "failure" in details:
        reason = Reason(str(exc), deepcopy(details["failure"]), retryable=exc.retryable,
                        retry_after_seconds=exc.retry_after_seconds)
        reason.details.update({key: deepcopy(value) for key, value in details.items()
                               if key not in reason.details})
        return reason
    # Unknown failures remain explicitly unknown; no Chinese text is parsed.
    denied = isinstance(exc, PermissionDenied)
    code = "entity_not_accessible" if denied else "action_precondition_failed"
    name = "accessible" if denied else "declared_action_preconditions"
    return failure(code, str(exc), name, subject=subject, entity_id=entity_id, tile=tile,
        known=False, reads=reads, recovery=getattr(exc, "recovery", None) or "refresh_then_choose_action",
        retryable=getattr(exc, "retryable", False),
        retry_after_seconds=getattr(exc, "retry_after_seconds", None))


def reject(reason, *, exception=RuleViolation):
    if isinstance(reason, Reason):
        raise exception(str(reason), details=reason.details, recovery=reason.recovery,
                        retryable=reason.retryable, retry_after_seconds=reason.retry_after_seconds)
    raise exception(str(reason))


def option_reason(option):
    """Recover the declared failure carried by a JSON action-menu row."""
    return Reason(option["reason"], option["failure"]) if option.get("failure") else option.get("reason")


def accessible(ctx, player, entity, *, near=True, subject="target"):
    """Authorize before exposing ancestry, lifecycle, or location facts."""
    current, seen = entity, set()
    for _ in range(17):
        if current["owner"] not in {None, player["id"]}:
            reject(failure("entity_not_accessible", "别人的背包和物件不能取用。", "accessible",
                subject=subject, entity_id=entity["id"], known=False), exception=PermissionDenied)
        if current["id"] in seen:
            reject(failure("physical_relation_cycle", "物理关系不能循环。", "acyclic_relation",
                subject=subject, entity_id=entity["id"], actual=False, required=True))
        seen.add(current["id"])
        if current["state"].get("retired") or current.get("lifecycle") == "consumed":
            reject(failure("entity_consumed", "物件已耗尽或投入其他用途。", "usable_lifecycle",
                subject=subject, entity_id=entity["id"], actual="consumed", required="usable"))
        parent = current.get("physical_parent")
        if not parent:
            break
        current = m.get_entity(ctx, parent["id"])
        if current["owner"] not in {None, player["id"]}:
            continue
        if parent["relation"] == "contained" and not current["state"]["open"]:
            reject(failure("ancestor_container_closed", "物件在关闭的容器内，请先打开。", "ancestor_container_open",
                subject=subject, entity_id=entity["id"], actual=False, required=True,
                reads=[read_entity(current["id"])],
                instruction="读取盛放物件的容器，打开后重新读取目标。"))
    else:
        reject(failure("physical_relation_depth", "物理关系层数超过限制。", "bounded_relation_depth",
            subject=subject, entity_id=entity["id"], actual=17, required={"maximum": 16}))
    x, y = m.entity_location(ctx, entity)
    distance = None if x is None else abs(x-player["x"])+abs(y-player["y"])
    visible = x is not None and max(abs(x-player["x"]), abs(y-player["y"])) <= 8
    if x is None or near and current["owner"] is None and distance > 1:
        facts = {"required_max": 1, "metric": "manhattan"}
        if visible:
            facts.update(location={"x": x, "y": y}, player_location={"x": player["x"], "y": player["y"]},
                         actual_distance=distance)
        else:
            facts["reason"] = "location_not_observable"
        label = {"target": "目标物件", "item": "来源物件", "other": "另一物件"}.get(subject, "物件")
        reject(failure("entity_not_adjacent", "要先走到"+label+"相邻的一格。", "adjacent",
            subject=subject, entity_id=entity["id"], known=visible, actual=distance, required={"maximum": 1},
            reads=[{"function_id": "map.viewport", "arguments": {}}],
            instruction="读取本人和目标当前位置，移动到相邻一格后重新读取目标。", **facts))
    if not near and not visible:
        reject(failure("entity_not_observable", "物件不在附近视野内。", "observable",
            subject=subject, entity_id=entity["id"], known=False, required=True))


def access_reason(ctx, player, entity, *, burning=False, subject="target"):
    try:
        accessible(ctx, player, entity, subject=subject)
    except (PermissionDenied, RuleViolation) as exc:
        return from_error(exc, subject=subject, entity_id=entity["id"])
    if not burning:
        cursor = entity
        for _ in range(17):
            if cursor["state"]["burning"]:
                return failure("entity_burning", "燃烧中的物件及内容不能搬运或取用。", "not_burning",
                    subject=subject, entity_id=entity["id"], actual=True, required=False,
                    instruction="用真实水灭火或等待燃烧结束，再读取物件。")
            parent = cursor.get("physical_parent")
            if not parent:
                break
            cursor = m.get_entity(ctx, parent["id"])
    return None


def container_reason(ctx, player, entity, *, subject="target", capacity=False):
    reason = access_reason(ctx, player, entity, subject=subject)
    if reason:
        return reason
    if "container" not in entity.get("components", {}):
        return failure("container_missing", "目标不是容器。", "container_component",
            subject=subject, entity_id=entity["id"], actual=False, required=True,
            instruction="选择拥有容器组件的实际目标。")
    if entity.get("lifecycle") != "intact":
        return failure("container_broken", "容器结构已损坏，不能装卸。", "container_intact",
            subject=subject, entity_id=entity["id"], actual=entity.get("lifecycle"), required="intact",
            instruction="选择结构完整的容器；已损坏物件的剩余材料仍保留。")
    if not entity["state"]["open"]:
        return failure("container_closed", "请先打开容器。", "container_open",
            subject=subject, entity_id=entity["id"], actual=False, required=True,
            instruction="从容器行动菜单选择open，成功后重新读取。")
    if capacity and len(entity["state"]["contents"]) >= entity["state"]["capacity"]:
        return failure("container_full", "容器物件格已满。", "container_slots",
            subject=subject, entity_id=entity["id"], actual=len(entity["state"]["contents"]),
            required={"maximum_before_store": entity["state"]["capacity"]-1},
            instruction="从容器取出已有完整物件或选择另一容器，再重新读取。")
    return None
