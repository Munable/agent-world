"""Public connector errors shared by its narrow state components."""
from __future__ import annotations


def public_world_error_details(value):
    """Allow only the world's observable action facts, never upstream diagnostics."""
    if not isinstance(value, dict):
        return None
    source = value.get("action_precondition")
    if not isinstance(source, dict):
        source = {}
    safe = {}
    for key in ("subject", "entity_id", "condition", "metric", "reason"):
        if isinstance(source.get(key), str) and len(source[key]) <= 180:
            safe[key] = source[key]
    for key in ("known", "satisfied"):
        if type(source.get(key)) is bool:
            safe[key] = source[key]
    for key in ("required_max", "actual_distance"):
        if type(source.get(key)) is int and 0 <= source[key] <= 1_000_000:
            safe[key] = source[key]
    if safe.get("known") is True:
        for key in ("location", "player_location"):
            location = source.get(key)
            if (isinstance(location, dict) and set(location) == {"x", "y"} and
                    all(type(location[axis]) is int and abs(location[axis]) <= 1_000_000 for axis in ("x", "y"))):
                safe[key] = dict(location)
    else:
        safe.pop("actual_distance", None)
    result = {"action_precondition": safe} if safe else {}
    transfer = value.get("water_transfer")
    if isinstance(transfer, dict):
        water = {}
        for key in ("source_id", "target_id", "meaning"):
            if isinstance(transfer.get(key), str) and len(transfer[key]) <= 180:
                water[key] = transfer[key]
        if transfer.get("target_slot") in ("contained", "surface", "absorbed"):
            water["target_slot"] = transfer["target_slot"]
        for key in ("requested", "source_remaining", "target_remaining_capacity", "maximum"):
            if type(transfer.get(key)) is int and 0 <= transfer[key] <= 1_000_000:
                water[key] = transfer[key]
        if transfer.get("unit") == "u":
            water["unit"] = "u"
        if water:
            result["water_transfer"] = water
    return result or None


class ConnectorError(Exception):
    def __init__(self, code: str, message: str, status: int = 400, *, world_details=None, execution_outcome=None):
        self.code, self.message, self.status = code, message, status
        self.world_details = public_world_error_details(world_details)
        # This is adapter-owned stage evidence, never a substitute world result.
        self.execution_outcome = None
        if isinstance(execution_outcome, dict):
            safe = {key: value for key, value in execution_outcome.items()
                    if key in {"stage", "operation_id", "function_id", "task_id", "segment_id", "checkpoint_status", "readback_error"}
                    and isinstance(value, str) and len(value) <= 180}
            safe.update({key: value for key, value in execution_outcome.items()
                         if key in {"world_action_committed", "receipt_confirmed", "checkpoint_recorded", "checkpoint_persisted", "segment_finished", "current_facts_available"}
                         and type(value) is bool})
            if type(execution_outcome.get("commit_seq")) is int and execution_outcome["commit_seq"] >= 0:
                safe["commit_seq"] = execution_outcome["commit_seq"]
            self.execution_outcome = safe or None
        super().__init__(message)
