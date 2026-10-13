"""Public connector errors shared by its narrow state components."""
from __future__ import annotations

import math
import re
import json


_MAX_WORLD_DETAILS_BYTES = 8192
_MISSING = object()
_READ_ARGUMENTS = frozenset({"entity_id", "candidate_id", "action", "definition_ref", "type_id",
    "source_definition_ref", "recipe", "content_id", "author", "author_name", "name", "key",
    "x", "y", "radius", "limit", "before", "after", "inventory_after", "cursor"})
_REQUIRED_FACT_KEYS = frozenset({"minimum", "maximum", "below", "above", "equals", "maximum_radius",
                               "maximum_before_store", "maximum_before_build"})


def _text(value, maximum=180):
    return value if isinstance(value, str) and len(value) <= maximum else _MISSING


def _number(value):
    if type(value) is int and abs(value) <= 2**63 - 1:
        return value
    if type(value) is float and math.isfinite(value) and abs(value) <= 2**63 - 1:
        return value
    return _MISSING


def _fact(value):
    """Small declared quantities/conditions, never arbitrary upstream objects."""
    if value is None or type(value) is bool:
        return value
    if isinstance(value, str):
        return _text(value)
    numeric = _number(value)
    if numeric is not _MISSING:
        return numeric
    if isinstance(value, list) and len(value) <= 32:
        items = [_fact(item) if not isinstance(item, (dict, list)) else _MISSING for item in value]
        return items if all(item is not _MISSING for item in items) else _MISSING
    if isinstance(value, dict) and value and set(value) <= _REQUIRED_FACT_KEYS:
        items = {key: _fact(item) if not isinstance(item, (dict, list)) else _MISSING
                 for key, item in value.items()}
        return items if all(item is not _MISSING for item in items.values()) else _MISSING
    return _MISSING


def _location(value):
    if (isinstance(value, dict) and set(value) == {"x", "y"}
            and all(type(value[axis]) is int and abs(value[axis]) <= 1_000_000 for axis in ("x", "y"))):
        return dict(value)
    return None


def _subject_ref(value):
    if not isinstance(value, dict):
        return None
    if value.get("kind") == "entity" and _text(value.get("id")) is not _MISSING:
        return {"kind": "entity", "id": value["id"]}
    if value.get("kind") == "player" and value.get("role") == "self":
        return {"kind": "player", "role": "self"}
    if value.get("kind") == "tile":
        location = _location({key: value.get(key) for key in ("x", "y")})
        if location is not None:
            return {"kind": "tile", **location}
    return None


def _precondition(source):
    if not isinstance(source, dict):
        return None
    safe = {}
    for key in ("subject", "entity_id", "condition", "metric", "reason", "material", "unit"):
        if _text(source.get(key)) is not _MISSING:
            safe[key] = source[key]
    for key in ("known", "satisfied"):
        if type(source.get(key)) is bool:
            safe[key] = source[key]
    reference = _subject_ref(source.get("subject_ref"))
    if reference is not None:
        safe["subject_ref"] = reference
    if "required" in source:
        required = _fact(source["required"])
        if required is not _MISSING:
            safe["required"] = required
    for key in ("required_max", "actual_distance"):
        if type(source.get(key)) is int and 0 <= source[key] <= 1_000_000:
            safe[key] = source[key]
    if safe.get("known") is True:
        if "actual" in source:
            actual = _fact(source["actual"])
            if actual is not _MISSING:
                safe["actual"] = actual
        for key in ("location", "player_location"):
            location = _location(source.get(key))
            if location is not None:
                safe[key] = location
    else:
        safe.pop("actual_distance", None)
        if "actual" in source:
            safe["actual"] = None
        if "satisfied" in safe:
            safe["satisfied"] = False
    return safe or None


def _read_requests(source):
    if not isinstance(source, list) or len(source) > 24:
        return None
    requests = []
    for row in source:
        if not isinstance(row, dict) or not isinstance(row.get("function_id"), str):
            continue
        function, arguments = row["function_id"], row.get("arguments")
        if (not re.fullmatch(r"(?:grove|map)\.[a-z_]{1,100}", function)
                or not isinstance(arguments, dict) or len(arguments) > 16
                or not set(arguments) <= _READ_ARGUMENTS):
            continue
        safe = {}
        for key, value in arguments.items():
            if key == "cursor" and isinstance(value, dict):
                if (set(value) <= {"contribution_before", "outcome_before"}
                        and all(type(item) is int and 0 <= item <= 2**63 - 1 for item in value.values())):
                    safe[key] = dict(value)
            else:
                item = _fact(value) if not isinstance(value, (dict, list)) else _MISSING
                if item is not _MISSING:
                    safe[key] = item
        # Do not silently turn a filtered query into a different query.
        if len(safe) != len(arguments):
            continue
        request = {"function_id": function, "arguments": safe}
        if type(row.get("requires_membership")) is bool:
            request["requires_membership"] = row["requires_membership"]
        requests.append(request)
    return requests


def _recovery(value):
    if not isinstance(value, dict):
        return None
    safe = {key: value[key] for key, maximum in (("strategy", 128), ("instruction", 2048))
            if _text(value.get(key), maximum) is not _MISSING}
    return safe if "strategy" in safe else None


def _failure(value):
    if not isinstance(value, dict):
        return None
    code, condition = _text(value.get("code")), _text(value.get("condition"))
    precondition, reads, recovery = (_precondition(value.get("precondition")),
        _read_requests(value.get("read_requests")), _recovery(value.get("recovery")))
    if code is _MISSING or condition is _MISSING or precondition is None:
        return None
    result = {"code": code, "condition": condition, "precondition": precondition}
    subjects = value.get("subjects")
    if isinstance(subjects, list) and len(subjects) <= 16:
        result["subjects"] = [reference for row in subjects if (reference := _subject_ref(row)) is not None]
    if reads is not None:
        result["read_requests"] = reads
    if recovery is not None:
        result["recovery"] = recovery
    return result


def _program_diagnostic(value, *, compiler=False):
    codes = ({"invalid_definition", "division_by_zero_possible", "field_range_possible"} if compiler else
             {"division_by_zero", "expression_error", "field_range", "effect_error"})
    if not isinstance(value, dict) or not isinstance(value.get("code"), str) or value["code"] not in codes:
        return None
    safe = {"code": value["code"]}
    for key, pattern in (("rule_id", r"[a-z][a-z0-9_.-]{0,79}"), ("action_id", r"[a-z][a-z0-9_.-]{0,79}"),
                         ("field", r"[a-z][a-z0-9_]{0,47}"), ("type_id", r"[a-z][a-z0-9_]{0,47}")):
        if key == "type_id" and not compiler:
            continue
        if isinstance(value.get(key), str) and re.fullmatch(pattern, value[key]):
            safe[key] = value[key]
    if value.get("phase") in ("manual", "reaction", "after"):
        safe["phase"] = value["phase"]
    path = value.get("path")
    if isinstance(path, str) and len(path) <= 256 and re.fullmatch(r"program(?:\.[a-zA-Z0-9_.\[\]-]+)?", path):
        safe["path"] = path
    if value.get("detail_omitted") == "diagnostic_byte_limit":
        safe["detail_omitted"] = value["detail_omitted"]
    if compiler and value.get("severity") in ("warning", "error"):
        safe["severity"] = value["severity"]
    for key in ("effect_index", "logical_step", "attempted_value", "min", "max", "inferred_min", "inferred_max"):
        if (type(value.get(key)) is int and abs(value[key]) <= 2**63 - 1
                and (key not in ("effect_index", "logical_step") or value[key] >= 0)):
            safe[key] = value[key]
    # Compiler exception text may include arbitrary submitted keys. The public
    # machine location and bounds suffice; do not relay raw message/scope text.
    return safe


def _compiler_diagnostics(value):
    if not isinstance(value, dict) or not isinstance(value.get("items"), list) or len(value["items"]) > 16:
        return None
    items = [_program_diagnostic(row, compiler=True) for row in value["items"]]
    if any(item is None for item in items):
        return None
    result = {"items": items}
    if type(value.get("truncated")) is bool:
        result["truncated"] = value["truncated"]
    if value.get("safety_proven") is False:
        result["safety_proven"] = False
    if type(value.get("omitted_count")) is int and 0 <= value["omitted_count"] <= 2**63 - 1:
        result["omitted_count"] = value["omitted_count"]
    return result


def _include_bounded(result, key, value):
    if value is None:
        return
    try:
        size = len(json.dumps({**result, key: value}, ensure_ascii=False,
                             separators=(",", ":"), allow_nan=False).encode())
    except (TypeError, ValueError, UnicodeError):
        return
    if size <= _MAX_WORLD_DETAILS_BYTES:
        result[key] = value


def public_world_error_details(value):
    """Keep formal public failures and diagnostics, never arbitrary upstream data."""
    if not isinstance(value, dict):
        return None
    safe = _precondition(value.get("action_precondition"))
    result = {}
    _include_bounded(result, "action_precondition", safe)
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
            _include_bounded(result, "water_transfer", water)
    settlement = value.get("settlement")
    if (isinstance(settlement, dict) and settlement.get("state") == "calculating"
            and settlement.get("operation_committed") is False
            and type(settlement.get("target_tick")) is int and settlement["target_tick"] >= 0):
        _include_bounded(result, "settlement", {"state": "calculating", "target_tick": settlement["target_tick"],
                                              "operation_committed": False})
    if _text(value.get("code")) is not _MISSING:
        _include_bounded(result, "code", value["code"])
    _include_bounded(result, "failure", _failure(value.get("failure")))
    _include_bounded(result, "read_requests", _read_requests(value.get("read_requests")))
    _include_bounded(result, "recovery", _recovery(value.get("recovery")))
    _include_bounded(result, "program_diagnostic", _program_diagnostic(value.get("program_diagnostic")))
    _include_bounded(result, "diagnostics", _compiler_diagnostics(value.get("diagnostics")))
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
