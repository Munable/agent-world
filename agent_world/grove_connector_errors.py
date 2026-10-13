"""Public connector errors shared by its narrow state components."""
from __future__ import annotations

import math
import re


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
    settlement = value.get("settlement")
    if (isinstance(settlement, dict) and settlement.get("state") == "calculating"
            and settlement.get("operation_committed") is False
            and type(settlement.get("target_tick")) is int and settlement["target_tick"] >= 0):
        result["settlement"] = {"state": "calculating", "target_tick": settlement["target_tick"],
                                "operation_committed": False}
    return result or None


class ConnectorError(Exception):
    def __init__(self, code: str, message: str, status: int = 400, *, world_details=None, execution_outcome=None,
                 retryable=None, recovery=None, retry_after_seconds=None):
        self.code, self.message, self.status = code, message, status
        self.world_details = public_world_error_details(world_details)
        self.retryable = retryable if type(retryable) is bool else None
        self.recovery = recovery if isinstance(recovery, str) and 0 < len(recovery) <= 128 else None
        self.retry_after_seconds = (retry_after_seconds if type(retry_after_seconds) in (int, float)
            and (type(retry_after_seconds) is int or math.isfinite(retry_after_seconds))
            and retry_after_seconds >= 0 else None)
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
            for key in ("attempts", "busy_rejections"):
                if type(execution_outcome.get(key)) is int and 0 <= execution_outcome[key] <= 16:
                    safe[key] = execution_outcome[key]
            if (type(execution_outcome.get("wait_seconds")) in (int, float)
                    and math.isfinite(execution_outcome["wait_seconds"]) and execution_outcome["wait_seconds"] >= 0):
                safe["wait_seconds"] = execution_outcome["wait_seconds"]
            # Preserve the verified native yield across this public projection.
            # This only carries evidence; the native proof verifier and executor
            # still require its exact request owner, terminal tools and budget.
            if code == "ExecutionSegmentYield" and execution_outcome.get("stage") == "native_segment_yield":
                if type(execution_outcome.get("safe")) is bool:
                    safe["safe"] = execution_outcome["safe"]
                if execution_outcome.get("reason") in ("request_headroom", "request_over_budget"):
                    safe["reason"] = execution_outcome["reason"]
                request_id = execution_outcome.get("request_id")
                if isinstance(request_id, str) and re.fullmatch(r"grove-request-[a-f0-9]{32}", request_id):
                    safe["request_id"] = request_id
                for key in ("body_bytes", "current_terminal_tool_parts", "current_unfinished_tool_parts"):
                    if type(execution_outcome.get(key)) is int and 0 <= execution_outcome[key] <= 2**63 - 1:
                        safe[key] = execution_outcome[key]
            self.execution_outcome = safe or None
        super().__init__(message)

    def public_payload(self):
        payload = {"error": self.code, "message": self.message,
                   "retryable": self.retryable if self.retryable is not None else self.status >= 500}
        if self.recovery is not None:
            payload["recovery"] = self.recovery
        if self.retry_after_seconds is not None:
            payload["retry_after_seconds"] = self.retry_after_seconds
        if self.world_details is not None:
            payload["details"] = self.world_details
        if self.execution_outcome is not None:
            payload["execution_outcome"] = self.execution_outcome
        return payload
