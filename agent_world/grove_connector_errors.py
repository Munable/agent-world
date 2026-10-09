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
    def __init__(self, code: str, message: str, status: int = 400, *, world_details=None):
        self.code, self.message, self.status = code, message, status
        self.world_details = public_world_error_details(world_details)
        super().__init__(message)
