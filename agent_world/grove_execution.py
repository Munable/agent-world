"""Owned execution state for a person's explicitly delegated Grove goal.

The connector supplies narrow ports. This component owns task admission,
request/action budgets, segment authority, the receipt ledger and the local
stop boundary; it never receives a connector session or native host client.
Native conversation, observation binding and HTTP recovery journals remain at
their existing boundaries.
"""
from __future__ import annotations

import asyncio
import contextlib
from copy import deepcopy
from dataclasses import dataclass
import hashlib
import json
import secrets
import time
from typing import Awaitable, Callable

from jsonschema import Draft202012Validator

from .grove_agent_observation import AGENT_WRITE_FUNCTIONS
from .grove_connector_errors import ConnectorError


MAX_ACTION_TARGETS = 4
MAX_ACTION_SUMMARY_BYTES = 8 * 1024
MAX_EFFECT_READS = 16
MAX_EFFECT_READ_BYTES = 4 * 1024
MAX_EFFECT_READ_LEDGER_BYTES = 16 * 1024
RECEIPT_HISTORY_SCOPE = "formal_agent_writes"
MAX_TASK_REQUESTS = 32
DEFAULT_TASK_REQUESTS = 16
_PAGED_PREPARATION_READS = {
    "grove.definition_list": {"definitions": "definition_ref"},
    "grove.public_works": {"works": "entity_id", "author_matches": "author"},
    "grove.content_history": {"contributions": "id", "outcome_refs": "id"},
}
_PREPARATION_METADATA = {
    "response": {"observation_id", "base_observation_id", "snapshot_seq", "commit_seq", "observed_at",
                 "ok", "world_id", "function_id", "function_version", "clock", "world_tick", "cursor",
                 "next_cursor", "next", "after", "offset", "has_more", "execution_control", "execution_task",
                 "task_action_summary", "agent_context", "read_requests", "observed_step", "effect_evidence_reads"},
    "entity": {"version", "interaction_revision", "updated_at"},
    "creative": {"world_tick"}, "program": {"event_cursor"}, "process": {"last_tick", "observed_tick"},
    "mechanical_observation": {"observed_tick"}, "mechanical_values": {"observed_tick"},
    "automatic_field_change": {"tick"},
    "combustion": {"clock_sequence", "next_consumption_tick", "ticks_until_next_consumption"},
    "presentation_observation": {"tick"},
    "execution_domain": {"last_applied_tick", "pending_target_tick"},
}
_PREPARATION_CHILDREN = {
    "response": {"result": "response", "viewport": "response", "selected_inspection": "response",
        "inspection": "response", "entity": "entity", "player": "entity", "entities": "entity",
        "players": "entity", "nearby_entities": "entity", "nearby_players": "entity", "inventory": "entity",
        "contents": "entity", "resource_nodes": "entity", "water_sources": "entity"},
    "entity": {"creative": "creative", "state": "state", "simulation": "simulation",
               "presentation": "presentation", "execution_domain": "execution_domain"},
    "simulation": {"combustion": "combustion"},
    "presentation": {"observation": "presentation_observation"},
    "state": {"program": "program"}, "program": {"process": "process"},
    "creative": {"process": "process", "mechanical_observation": "mechanical_observation",
                 "automatic_field_change": "automatic_field_change"},
    "mechanical_observation": {"current_inputs": "mechanical_values", "saved_outputs": "mechanical_values"},
}


def _preparation_read_signature(function: str, arguments: dict):
    """Identify a semantic query, independent of read handles and cursors."""
    if function not in {"map.viewport", "grove.inspect", "grove.rule_catalog",
            "grove.definition_read", "grove.definition_list", "grove.definition_validate",
            "grove.fabrication_options", "grove.public_works", "grove.content_history",
            "grove.effect_evidence"} or not isinstance(arguments, dict):
        return None
    semantic = {key: value for key, value in arguments.items()
                if key not in {"base_observation_id", "limit", "cursor", "after", "offset"}}
    try:
        encoded = json.dumps([function, semantic], ensure_ascii=False, sort_keys=True,
                             separators=(",", ":"), allow_nan=False).encode("utf-8")
    except (TypeError, ValueError):
        return None
    return hashlib.sha256(encoded).hexdigest()


def _preparation_facts(value, *, kind="response"):
    """Hash delivered facts, excluding only known envelope/runtime metadata.

    Authored fields, conditions and definitions are opaque here: a user field
    called ``version`` or ``tick`` is still a fact. These fingerprints never
    provide write guards or certify a natural-language goal.
    """
    if isinstance(value, list):
        return [_preparation_facts(row, kind=kind) for row in value]
    if not isinstance(value, dict):
        return value
    omitted = _PREPARATION_METADATA.get(kind, ())
    children = _PREPARATION_CHILDREN.get(kind, {})
    # The shape, not arbitrary descendant field names, identifies each known
    # runtime block. Everything else remains opaque and unchanged.
    result = {}
    for key, item in value.items():
        if key in omitted:
            continue
        child_kind = children.get(key)
        if child_kind:
            result[key] = _preparation_facts(item, kind=child_kind)
        else:
            result[key] = item
    return result


def _preparation_digest(value):
    try:
        encoded = json.dumps(value, ensure_ascii=False, sort_keys=True,
                             separators=(",", ":"), allow_nan=False).encode("utf-8")
    except (TypeError, ValueError):
        return None
    return hashlib.sha256(encoded).hexdigest()


def _effect_read_record(arguments, delivered):
    """Retain exact bounded public evidence sections, never an Agent verdict.

    The full response was already delivered to the model. A large section is
    omitted whole from this local reference, with explicit coverage; retaining
    a smaller array must not turn it into an apparently complete result.
    """
    if not isinstance(arguments, dict) or not isinstance(delivered, dict) or delivered.get("ok") is False:
        return None
    observation_id = delivered.get("observation_id")
    source = delivered.get("agent_observation")
    value = delivered.get("result", delivered)
    if (not isinstance(observation_id, str) or not 1 <= len(observation_id) <= 100 or
            not isinstance(source, dict) or source.get("source_function") != "grove.effect_evidence" or
            source.get("source_kind") != "query" or source.get("scope") != "client_presented" or
            not isinstance(source.get("world_instance"), str) or not source["world_instance"] or
            not isinstance(value, dict) or value.get("ok") is False or
            not isinstance(value.get("entity_id"), str) or not isinstance(value.get("definition_ref"), str) or
            value.get("entity_id") != arguments.get("entity_id") or
            value.get("definition_ref") != arguments.get("definition_ref")):
        return None
    digest = _preparation_digest(value)
    if digest is None:
        return None
    record = {"observation_id": observation_id, "function_id": "grove.effect_evidence",
        "world_instance": source.get("world_instance"), "role_id": source.get("role_id"),
        "entity_id": value["entity_id"], "definition_ref": value["definition_ref"],
        "snapshot_seq": delivered.get("snapshot_seq"),
        "result_sha256": digest, "summary": {key: deepcopy(value[key]) for key in
            ("entity_id", "definition_ref", "type_id", "entity_version", "observed_step") if key in value},
        "summary_omitted_sections": [],
        "meaning": "Actually delivered public read at its observed_step, with exact requested identity. "
            "Current predicates and historical effect witnesses retain their separate meanings; "
            "omitted sections are unknown here. This is not goal verification or a write binding."}
    # Prefer predicate results and the history's declared coverage to catalogs
    # or full outcomes. Kept branches stay exact, including false/unknown.
    sections = [("query", {key: deepcopy(arguments[key]) for key in
        ("entity_id", "definition_ref", "predicates", "after_step", "outcome_before") if key in arguments}),
        *[(key, value[key]) for key in ("predicates", "history", "stages", "process") if key in value]]
    for key, section in sections:
        container = record if key == "query" else record["summary"]
        container[key] = deepcopy(section)
        if len(json.dumps(record, ensure_ascii=False, indent=2).encode("utf-8")) > MAX_EFFECT_READ_BYTES - 512:
            container.pop(key)
            record["summary_omitted_sections"].append(key)
    record["summary_omitted_sections"].extend(key for key in value if key not in
        {"entity_id", "definition_ref", "type_id", "entity_version", "observed_step", "predicates", "history", "stages", "process"})
    return record


def _preparation_fingerprints(function: str, query_key: str, delivered: dict):
    """Only delivered facts count; a changed row retains its stable identity."""
    if not isinstance(delivered, dict) or delivered.get("ok") is False:
        return None
    value = delivered.get("result", delivered)
    if not isinstance(value, dict) or value.get("ok") is False or not value:
        return None
    if function not in _PAGED_PREPARATION_READS:
        facts = _preparation_facts(value)
        if not facts:
            return None
        digest = _preparation_digest(facts)
        return {query_key: digest} if digest is not None else None
    pages = [(name, value[name], identity) for name, identity in _PAGED_PREPARATION_READS[function].items()
             if isinstance(value.get(name), list)]
    if not pages:
        return None
    # A successful empty result is useful once, but changing its cursor is not.
    result = {query_key: "page_read"}
    for name, page, identity in pages:
        for row in page:
            if not isinstance(row, dict) or not isinstance(row.get(identity), str) or not row[identity]:
                return None
            key = _preparation_digest([query_key, name, row[identity]])
            digest = _preparation_digest(_preparation_facts(row, kind="entity" if name == "works" else "response"))
            if key is None or digest is None:
                return None
            result[key] = digest
    return result


def _summary_outcome(outcome):
    """Keep original commit evidence, without repeating a whole object snapshot."""
    if not isinstance(outcome, dict):
        return None
    value = {"kind": "server_receipt"}
    if isinstance(outcome.get("changes"), list):
        value["changes"] = [text[:160] for text in outcome["changes"][:2] if isinstance(text, str)]
    entity = outcome.get("entity")
    if isinstance(entity, dict):
        value["entity"] = {key: deepcopy(entity[key]) for key in
            ("id", "name", "version", "x", "y", "owner", "container", "physical_parent", "lifecycle")
            if key in entity}
    action = outcome.get("action_result")
    if isinstance(action, dict):
        # Authored recordings and full object lists remain in the original
        # receipt. Their absence here must never look like an empty result.
        keep = ("action", "target_id", "target_name", "stored_energy_after", "costs", "transfer")
        value["action_result"] = {key: deepcopy(action[key]) for key in keep if key in action}
        omitted = sorted(set(action) - set(keep))
        if omitted:
            value["action_result_omitted_fields"] = omitted
    value["meaning"] = "Selected facts at the original commit; omitted fields remain in that operation's original receipt. Not current state or a goal verdict."
    return value


def _receipt_costs(outcome):
    """Only typed costs explicitly reported by the server, never quantity inference."""
    action = outcome.get("action_result") if isinstance(outcome, dict) else None
    if not isinstance(action, dict) or "costs" not in action:
        return None
    rows = action["costs"]
    if not isinstance(rows, list):
        return False
    costs = {}
    for row in rows:
        if (not isinstance(row, dict) or not isinstance(row.get("material"), str) or
                not row["material"] or type(row.get("quantity_u")) is not int or row["quantity_u"] < 0):
            return False
        costs[row["material"]] = costs.get(row["material"], 0) + row["quantity_u"]
    return costs


def action_entity_ids(arguments, payload=None):
    """Only formal action/receipt identity fields, never authored prose or rules."""
    arguments = arguments if isinstance(arguments, dict) else {}
    result = payload.get("result", {}) if isinstance(payload, dict) else {}
    result = result if isinstance(result, dict) else {}
    candidates = [arguments.get(key) for key in ("target_id", "entity_id", "other_id", "item_id")]
    entities = result.get("entities", [])
    candidates += [entity.get("id") for entity in [result.get("entity"),
        *(entities[:MAX_ACTION_TARGETS] if isinstance(entities, list) else [])] if isinstance(entity, dict)]
    return list(dict.fromkeys(identity for identity in candidates
        if isinstance(identity, str) and 1 <= len(identity) <= 256))[:MAX_ACTION_TARGETS]


def confirmed_outcome(payload):
    """A bounded projection of the original authorized server receipt only."""
    result = payload.get("result") if isinstance(payload, dict) else None
    if not isinstance(result, dict):
        return None
    facts = {"kind": "server_receipt"}
    changes = result.get("changes")
    if isinstance(changes, list):
        facts["changes"] = [value[:600] for value in changes[:4] if isinstance(value, str)]
    player = result.get("player")
    if isinstance(player, dict):
        facts["player"] = {key: deepcopy(player[key]) for key in ("id", "x", "y") if key in player}
    entity = result.get("entity")
    if isinstance(entity, dict):
        facts["entity"] = {key: deepcopy(entity[key]) for key in
            ("id", "name", "version", "x", "y", "owner", "container", "physical_parent", "lifecycle",
             "material_quantity", "mechanical", "execution_domain") if key in entity}
        if isinstance(entity.get("matter"), list):
            facts["entity"]["matter"] = [{key: deepcopy(lot[key]) for key in
                ("material", "slot", "quantity", "role", "burning") if key in lot}
                for lot in entity["matter"][:8] if isinstance(lot, dict)]
            facts["entity"]["matter_total"] = len(entity["matter"])
        state = entity.get("state")
        if isinstance(state, dict):
            facts["entity"]["state"] = {key: deepcopy(value) for key, value in state.items()
                if value is None or isinstance(value, (str, bool, int, float))}
            program = state.get("program")
            if isinstance(program, dict):
                facts["entity"]["state"]["program"] = {key: deepcopy(program[key]) for key in
                    ("fields", "mechanical", "drive_request", "mechanical_last_solved") if key in program}
    action = result.get("action_result")
    if isinstance(action, dict):
        facts["action_result"] = deepcopy(action)
    viewport = result.get("viewport", {})
    if isinstance(viewport, dict) and isinstance(player, dict):
        facts["same_position_public_objects"] = [{key: deepcopy(item[key]) for key in ("id", "name") if key in item}
            for item in viewport.get("entities", []) if isinstance(item, dict) and item.get("owner") is None
            and item.get("x") == player.get("x") and item.get("y") == player.get("y")][:8]
    return facts if len(facts) > 1 else None


@dataclass(frozen=True)
class ExecutionPorts:
    """Narrow adapters; closures may resolve the current transport at call time.

    read accepts kind="receipt"/"facts" and operation_id/context_entity_id,
    plus bounded action_entity_ids for independent exact current target reads.
    native_segment accepts kind="execute"/"history", a goal and an
    optional context_entity_id. recover settles original operations without
    replay; unknown_operations exposes the transport's original journal.
    settle confirms the saved native request and admitted-write drain.
    """

    invoke: Callable[..., Awaitable[dict]]
    read: Callable[..., Awaitable[dict]]
    recover: Callable[..., Awaitable[None]]
    unknown_operations: Callable[[], list[dict]]
    native_segment: Callable[..., Awaitable[None]]
    settle: Callable[..., Awaitable[bool]]
    persist: Callable[[], None]


class ExecutionController:
    def __init__(self, role_id: str, *, max_requests: int, max_world_calls: int,
                 max_seconds: int, ports: ExecutionPorts | None = None):
        self.role_id = role_id
        self.request_batch_limit, self.request_limit = max_requests, MAX_TASK_REQUESTS
        self.world_call_limit = max_world_calls
        self.ports = ports
        self.state = {"status": "idle", "goal": "", "max_actions": 0, "actions_count": 0,
            "control_status": "idle", "delegation_active": False,
            "grant_version": None, "grant_id": None, "receipts": [], "receipt_history_complete": True,
            "receipt_history_scope": RECEIPT_HISTORY_SCOPE, "readback": None,
            "readback_actual": False, "error": None,
            "task_id": "", "context_entity_id": None, "requests_used": 0, "max_requests": DEFAULT_TASK_REQUESTS,
            "request_status": "idle", "stop_reason": "", "resume_allowed": False,
            "grant_action_offset": 0, "pending_grant_action_offset": None, "grant_operation_id": None,
            "readback_actions_count": None, "no_progress_segments": 0,
            "preparation_count": 0, "preparation_fingerprints": {},
            "effect_evidence_reads": [],
            "host_request": {"status": "idle", "purpose": None, "generation": 0},
            "persistence_failure": None,
            "segment_id": "", "checkpoint": None, "goal_result": None, "segments": [], "phase_timings": [],
            "context_result_reference": None, "context_reference_attempted": True, "world_results": [],
            "settlement": None, "action_entity_ids": [],
            "history_recovery": {"requested": False, "status": "not_started", "attempts": 0},
            "budget_extensions": [],
            "progress": {"summary": "", "next_intent": "", "evidence": []},
            "budget": {"max_steps": 8, "max_tool_calls": max_world_calls, "max_seconds": max_seconds}}
        self.archive: list[dict] = []
        self.delegation = ""
        # These boundaries differ: a checkpoint closes segment writes while the
        # task may continue; reclaiming spans native/write cleanup; stopped
        # remains set after cleanup until a newly admitted request starts.
        self.active = False
        self.stopped = False
        self.reclaiming = False
        self._reclaim_stop_reason = "你已收回后续行动；已成立结果保留。"
        self._preserve_reclaim_reason = False

    @property
    def unresolved(self) -> bool:
        """The adapter's original journal is the only unknown-write fact."""
        return bool(self.ports and self.ports.unknown_operations())

    @property
    def remaining_actions(self) -> int:
        return max(0, self.state["max_actions"] - self.state["actions_count"])

    def _persist(self):
        if self.ports is not None:
            try:
                self.ports.persist()
            except Exception as exc:
                self.persistence_failed(exc)
                raise ConnectorError("ExecutionPersistenceFailed", "本机执行记录未能保存；原行动保留，恢复原会话与回执后才能接续。", 503) from exc

    def persistence_failed(self, error: BaseException):
        """A failed audit save closes local admission independently of model text."""
        self.active = False
        self.state["resume_allowed"] = False
        if not self.state.get("persistence_failure"):
            self.state["persistence_failure"] = {"code": "ExecutionPersistenceFailed", "error_type": type(error).__name__}

    def ensure_persistence_ready(self):
        if self.state.get("persistence_failure"):
            raise ConnectorError("ExecutionPersistenceFailed", "本机执行记录未能保存；原行动保留，恢复原会话与回执后才能接续。", 503)

    def snapshot(self) -> dict:
        """Adapters observe owned task state; nested records are never write ports."""
        return deepcopy(self.state) | {"remaining_actions": self.remaining_actions,
            "action_summary": self.action_summary()}

    def action_summary(self) -> dict:
        """Count all formal Agent writes across segments, not host control work."""
        task = self.state.get("task_id")
        groups, segments, materials = {}, {}, {}
        costed, invalid_costs, missing_outcomes = 0, 0, 0
        # Recovery normally updates the same row, but old persisted duplicates
        # must not turn one operation into two confirmed actions or costs.
        receipts = {row["operation_id"]: row for row in self.state.get("receipts", [])
            if task and row.get("task_id") == task and isinstance(row.get("operation_id"), str)
            and row.get("function") in AGENT_WRITE_FUNCTIONS}
        confirmed = 0
        for receipt in receipts.values():
            if receipt.get("status") != "confirmed":
                continue
            targets = tuple(receipt.get("action_entity_ids", []))
            key = (receipt["function"], receipt.get("action"), receipt.get("action_id"), targets)
            group = groups.pop(key, {"function": receipt["function"],
                "action_entity_ids": list(targets), "count": 0})
            group.update(count=group["count"] + 1, last_operation_id=receipt["operation_id"],
                last_commit_seq=receipt.get("commit_seq"))
            for field in ("action", "action_id"):
                if field in receipt:
                    group[field] = receipt[field]
            # An older group's outcome cannot stand in for a missing last one.
            group.pop("last_confirmed_outcome", None)
            outcome = receipt.get("confirmed_outcome")
            if (compact := _summary_outcome(outcome)) is not None:
                group["last_confirmed_outcome"] = compact
            else:
                missing_outcomes += 1
            costs = _receipt_costs(outcome)
            if costs is False:
                invalid_costs += 1
            elif costs is not None:
                costed += 1
                for material, quantity in costs.items():
                    materials[material] = materials.get(material, 0) + quantity
            groups[key] = group
            confirmed += 1
            segment = receipt.get("segment_id") or None
            segments[segment] = segments.get(segment, 0) + 1
        summary = {"task_id": task, "scope": "current_task_retained_confirmed_actions",
            "confirmed_count": confirmed, "total_groups": len(groups),
            "omitted_groups": max(0, len(groups) - 8), "groups": list(groups.values())[-8:],
            "segments": [{"segment_id": identity, "confirmed_count": count} for identity, count in segments.items()],
            "material_costs": [{"material": identity, "quantity_u": quantity} for identity, quantity in sorted(materials.items())],
            "coverage": {"receipt_history_complete": self.state.get("receipt_history_complete") is True,
                "receipt_history_scope": self.state.get("receipt_history_scope", "legacy_write_scope_unverified"),
                "structured_cost_operations": costed, "invalid_cost_operations": invalid_costs,
                "operations_without_structured_costs": confirmed - costed - invalid_costs,
                "confirmed_operations_without_outcome": missing_outcomes,
                "committed_unconfirmed_count": sum(row.get("status") == "committed" for row in receipts.values()),
                "unknown_count": sum(row.get("status") == "unknown" for row in receipts.values()),
                "omitted_material_cost_rows": 0, "omitted_segment_rows": 0,
                "cost_scope": "Sum of explicitly reported costs in retained confirmed receipts only; not all material transfers, automatic consumption or other people's actions."},
            "meaning": "All this person's retained confirmed formal Agent writes across ALL segments of this SAME task's bounded receipt ledger. "
                "Opportunity claims/releases, definition publications, content submissions and object operations each count as their own confirmed writes. "
                "A claimed opportunity is not published content; a published definition does not create an object or matter. Host control grants/revocations are excluded. "
                "material_costs sums actual cost rows, never count times the last cost. "
                "Missing outcome/cost rows and incomplete receipt history prevent claiming all task spending. "
                "receipt_history_scope=legacy_write_scope_unverified means the saved completeness flag covered an older scope; full formal-write completeness is unknown. "
                "Groups show selected original-commit evidence, not current state or goal verification. "
                "Omitted groups or receipts do not mean an action never happened."}
        # Aggregate facts survive detail omission. Never truncate an outcome
        # into something that looks like a complete but smaller original.
        while summary["groups"] and len(json.dumps(summary, ensure_ascii=False, indent=2).encode("utf-8")) > MAX_ACTION_SUMMARY_BYTES:
            summary["groups"].pop(0)
            summary["omitted_groups"] += 1
        while summary["material_costs"] and len(json.dumps(summary, ensure_ascii=False, indent=2).encode("utf-8")) > MAX_ACTION_SUMMARY_BYTES:
            summary["material_costs"].pop()
            summary["coverage"]["omitted_material_cost_rows"] += 1
        while summary["segments"] and len(json.dumps(summary, ensure_ascii=False, indent=2).encode("utf-8")) > MAX_ACTION_SUMMARY_BYTES:
            summary["segments"].pop(0)
            summary["coverage"]["omitted_segment_rows"] += 1
        return summary

    def value(self, field: str, default=None):
        """Read one current fact without copying the whole saved world readback."""
        if field == "remaining_actions":
            return self.remaining_actions
        if field == "action_summary":
            return self.action_summary()
        return deepcopy(self.state.get(field, default))

    def archived_snapshots(self) -> list[dict]:
        return deepcopy(self.archive)

    def restore(self, saved: dict | None, *, archive=None, delegation=""):
        """Restore the same task/session authority, without reopening a goal."""
        if isinstance(saved, dict):
            self.state.update(deepcopy(saved))
            # Old completeness flags covered only physical object operations.
            # Broader retained writes remain valid, but their whole history
            # cannot be inferred from that old flag or a saved summary cache.
            if saved.get("task_id"):
                if "receipt_history_complete" not in saved or saved.get("receipt_history_scope") != RECEIPT_HISTORY_SCOPE:
                    self.state["receipt_history_complete"] = False
                if saved.get("receipt_history_scope") != RECEIPT_HISTORY_SCOPE:
                    self.state["receipt_history_scope"] = "legacy_write_scope_unverified"
            self.state.pop("remaining_actions", None)
            self.state.pop("action_summary", None)
            # Old query-only hashes cannot describe observed content changes.
            # Keep consumed progress/budget, not the obsolete local cache.
            self.state.pop("preparation_keys", None)
        self.archive = deepcopy(archive[-8:]) if isinstance(archive, list) else []
        if not isinstance(delegation, str):
            raise ValueError("invalid execution delegation")
        self.delegation = delegation
        self.active = False
        if self.state.get("status") in {"starting", "running"}:
            self.state.update(status="paused", request_status="unsettled", resume_allowed=False,
                stop_reason="连接器已重启；先恢复原回执和现场，再明确接续。")
        if (self.state.get("history_recovery") or {}).get("status") == "running":
            self.state["history_recovery"]["status"] = "uncertain"
        host = self.state.get("host_request")
        if not isinstance(host, dict):
            self.state["host_request"] = {"status": "unsettled", "purpose": None, "generation": 0}
        elif host.get("status") not in {"idle", "idle_confirmed"}:
            host["status"] = "unsettled"
        return self.normalize_progress()

    def ensure_admission(self, *, disconnecting=False):
        # World revocation may be ready before native abort finishes. That
        # short-stage result permits manual control, never a new partner task.
        if disconnecting or self.reclaiming:
            raise ConnectorError("ReclaimBusy", "原请求正在收尾；世界行动权已按当前状态处理，先等原收尾完成再启动伙伴任务。", 409)

    def ensure_native_ready(self):
        self.ensure_admission()
        self.ensure_persistence_ready()
        if not self.native_ready():
            raise ConnectorError("OriginalRequestUnsettled", "原请求尚未确认停止；先恢复原会话和回执，再启动伙伴请求。", 409)

    def native_ready(self) -> bool:
        never_started = self.state.get("request_status") == "not_started" and not self.state.get("requests_used")
        host_ready = self.state.get("host_request", {}).get("status") in {"idle", "idle_confirmed"}
        return bool(host_ready and (never_started or self.state.get("request_status") in {"idle", "idle_confirmed"}))

    def admission(self, *, entered: bool, native_connected: bool,
                  worker_busy: bool, catalog: dict, publication_pending=False,
                  disconnecting=False) -> dict:
        """One inference admission policy used by HTTP entry points and the UI.

        Transport reports connection/worker/publication facts. Task authority,
        original-request settlement and continuation eligibility remain here.
        Human world operations do not consume this projection.
        """
        def decision(code="", reason=""):
            return {"allowed": not code, "code": code, "reason": reason}

        if disconnecting or self.reclaiming:
            common = decision("ReclaimBusy", "原请求正在收尾；先等原收尾完成再启动伙伴任务。")
        elif self.state.get("persistence_failure"):
            common = decision("ExecutionPersistenceFailed", "本机执行记录未能保存；先恢复原会话与回执。")
        elif self.unresolved:
            common = decision("UnknownWrite", "先核对原操作结果，再启动伙伴请求。")
        elif not entered or not native_connected:
            common = decision("EntryRequired", "请先连接伙伴并进入现场。")
        elif worker_busy:
            common = decision("TurnBusy", "伙伴正在回应；请先收回或等待当前回应。")
        elif not self.native_ready():
            common = decision("OriginalRequestUnsettled", "原请求尚未确认停止；先恢复原会话和回执。")
        else:
            common = decision()
        result = {kind: dict(common) for kind in ("conversation", "creation", "goal", "resume", "extend")}
        if not common["allowed"]:
            return result
        if publication_pending:
            result["creation"] = decision("RecoverPublication", "先核对已发表内容的原回执与地图，不能生成替代内容。")
        if self.delegation or self.state.get("delegation_active"):
            result["goal"] = decision("ReclaimRequired", "请先收回原目标的控制，再授予新目标。")
        elif (self.state.get("history_recovery") or {}).get("status") in {"running", "uncertain", "failed"}:
            result["goal"] = decision("HistoryRecoveryUnconfirmed", "先恢复并核对原摘要结果，不能替换原目标。")
        elif not {"grove.control_grant", "grove.control_revoke", "grove.control_status"} <= catalog.keys():
            result["goal"] = decision("ExecutionUnavailable", "这个世界尚未提供目标执行。")
        if not self.state.get("resume_allowed") or not self.can_resume():
            result["resume"] = decision("ResumeUnavailable", "这个任务已经完成、额度用完或原请求尚未安全停止。")
        if not self.can_resume(additional_requests=1):
            result["extend"] = decision("ResumeUnavailable", "这个原任务不能追加请求后接续。")
        return result

    def require_admission(self, kind: str, **facts):
        result = self.admission(**facts)[kind]
        if not result["allowed"]:
            raise ConnectorError(result["code"], result["reason"], 409)

    def begin_native_work(self):
        """A newly admitted ordinary turn clears a previous local stop boundary."""
        self.ensure_native_ready()
        self.stopped = False

    def confirm_no_native_request(self):
        """Adapter confirmed there is no saved native session after worker drain."""
        self.state["request_status"] = "idle"
        self.state["host_request"] = {"status": "idle", "purpose": None,
            "generation": self.state.get("host_request", {}).get("generation", 0)}
        self._persist()

    def start(self, body: dict, *, entered: bool, native_connected: bool,
              worker_busy: bool, catalog: dict) -> dict:
        self.ensure_admission()
        self.ensure_persistence_ready()
        if not isinstance(body, dict) or set(body) - {"goal", "max_actions", "max_requests", "context_entity_id", "compact_history"}:
            raise ConnectorError("InvalidArguments", "Expected an explicit goal and bounded action grant.")
        goal, maximum = body.get("goal"), body.get("max_actions", 32)
        if not isinstance(goal, str) or not 1 <= len(goal.strip()) <= 4000:
            raise ConnectorError("InvalidGoal", "执行目标需要 1–4000 字。")
        if type(maximum) is not int or not 1 <= maximum <= 64:
            raise ConnectorError("InvalidActionBudget", "每次目标最多授权 1–64 个世界动作。")
        request_budget = body.get("max_requests", DEFAULT_TASK_REQUESTS)
        if type(request_budget) is not int or not 1 <= request_budget <= self.request_limit:
            raise ConnectorError("InvalidRequestBudget", "每个目标最多授权 1–32 次模型请求；在原额度内自动分段。")
        compact = body.get("compact_history", False)
        if type(compact) is not bool:
            raise ConnectorError("InvalidArguments", "compact_history 需要布尔值。")
        if compact and request_budget < 2:
            raise ConnectorError("HistoryRecoveryBudget", "历史整理和接续执行共需至少两次模型请求。")
        selected = body.get("context_entity_id")
        if selected is not None and (not isinstance(selected, str) or not 1 <= len(selected) <= 256 or not selected.strip()):
            raise ConnectorError("InvalidArguments", "context_entity_id 需要真实已选物件的标识。")
        self.require_admission("goal", entered=entered, native_connected=native_connected,
            worker_busy=worker_busy, catalog=catalog)
        if not Draft202012Validator(catalog["grove.control_grant"]["input_schema"]).is_valid(
                {"goal": goal.strip(), "max_actions": maximum}):
            raise ConnectorError("InvalidGoal", "执行目标或动作额度不符合这个世界当前的授权规则。")
        if self.state.get("task_id"):
            self.archive = (self.archive + [self.snapshot()])[-8:]
        # Preserve the public state shape and the fixed per-request budget.
        self.state.update(status="starting", goal=goal.strip(), max_actions=maximum, actions_count=0,
            delegation_active=False, grant_version=None, grant_id=None, control_status=None,
            receipts=[], receipt_history_complete=True, receipt_history_scope=RECEIPT_HISTORY_SCOPE,
            readback=None, readback_actual=False, error=None, action_entity_ids=[],
            task_id="grove-task-" + secrets.token_hex(16), context_entity_id=selected,
            requests_used=0, max_requests=request_budget, request_status="idle", stop_reason="", resume_allowed=False,
            grant_action_offset=0, pending_grant_action_offset=None, grant_operation_id=None,
            readback_actions_count=None, no_progress_segments=0, preparation_count=0, preparation_fingerprints={},
            effect_evidence_reads=[],
            segment_id="", checkpoint=None, goal_result=None, segments=[], phase_timings=[], settlement=None,
            context_result_reference=None, context_reference_attempted=False, world_results=[],
            history_recovery={"requested": compact, "status": "not_started", "attempts": 0},
            budget_extensions=[], progress={"summary": "", "next_intent": "", "evidence": []})
        self.active, self.stopped = False, False
        self._preserve_reclaim_reason = False
        self._persist()
        return {"goal": self.state["goal"], "context_entity_id": selected, "mode": "execute"}

    def resume(self, body: dict, *, entered: bool, native_connected: bool,
               worker_busy: bool) -> dict:
        self.ensure_admission()
        self.ensure_persistence_ready()
        if (not isinstance(body, dict) or "task_id" not in body or set(body) - {"task_id", "compact_history", "additional_requests"} or
                not isinstance(body["task_id"], str) or body["task_id"] != self.state.get("task_id")):
            raise ConnectorError("InvalidTask", "接续需要当前原任务标识；可明确追加模型请求，不接受替换目标或动作额度。")
        compact, additional = body.get("compact_history", False), body.get("additional_requests", 0)
        if type(compact) is not bool:
            raise ConnectorError("InvalidArguments", "compact_history 需要布尔值。")
        if (type(additional) is not int or not 0 <= additional <= self.request_batch_limit or
                self.state.get("max_requests", 0) + additional > self.request_limit):
            raise ConnectorError("InvalidRequestBudget", "每次明确接续最多追加 8 次请求；原目标累计总额度不能超过 32 次。")
        self.require_admission("conversation", entered=entered, native_connected=native_connected,
            worker_busy=worker_busy, catalog={})
        if (self.state.get("status") == "completed" or
                (not additional and not self.state.get("resume_allowed")) or
                not self.can_resume(additional_requests=additional)):
            raise ConnectorError("ResumeUnavailable", "这个任务已经完成、额度用完或原请求尚未安全停止。", 409)
        recovery = self.state["history_recovery"]
        if compact and recovery.get("status") != "completed":
            if recovery.get("attempts", 0):
                raise ConnectorError("HistoryRecoveryUnavailable", "原目标已尝试历史整理，不会重复调用。", 409)
            if self.state["max_requests"] + additional - self.state["requests_used"] < 2:
                raise ConnectorError("HistoryRecoveryBudget", "历史整理和接续执行共需至少两次剩余模型请求。", 409)
            recovery["requested"] = True
        if additional:
            previous = self.state["max_requests"]
            self.state["max_requests"] += additional
            self.state["budget_extensions"] = (self.state.get("budget_extensions", []) + [{
                "additional_requests": additional, "previous_max_requests": previous,
                "max_requests": self.state["max_requests"], "requests_used": self.state["requests_used"],
                "actions_count": self.state["actions_count"]}])[-self.request_limit:]
        self.active, self.stopped = False, False
        self._preserve_reclaim_reason = False
        self.state.update(status="starting", error=None, stop_reason="", resume_allowed=False)
        self._persist()
        return {"goal": self.state["goal"], "context_entity_id": self.state.get("context_entity_id"), "mode": "execute-resume"}

    def normalize_progress(self):
        if not self.state.get("task_id") or self.state.get("grant_operation_id") or self.state.get("receipts") or self.unresolved:
            return False
        changed = self.state.get("actions_count", 0) != 0 or self.state.get("grant_action_offset", 0) != 0
        self.state.update(actions_count=0, grant_action_offset=0, grant_version=None, grant_id=None,
            delegation_active=False)
        if self.state.get("readback_actions_count") is not None:
            self.state["readback_actions_count"] = 0
        return changed

    def can_resume(self, *, additional_requests=0):
        return bool(not self.reclaiming and not self.state.get("persistence_failure") and
            self.state.get("task_id") and self.state.get("status") in {"paused", "error"} and
            (self.state.get("history_recovery") or {}).get("status") not in {"running", "uncertain", "failed"} and
            not self.unresolved and not self.state.get("delegation_active") and not self.delegation and
            self.state.get("actions_count", 0) < self.state.get("max_actions", 0) and
            self.state.get("max_requests", 0) + additional_requests <= self.request_limit and
            self.state.get("requests_used", 0) < self.state.get("max_requests", 0) + additional_requests and
            self.state.get("request_status") == "idle_confirmed" and self.native_ready())

    def operation_owner(self, function: str, arguments: dict, *, model_write=False) -> dict:
        if model_write:
            return {"execution_write": True, "task_id": self.state.get("task_id"),
                "segment_id": self.state.get("segment_id")}
        if function == "grove.control_grant" and self.state.get("task_id") and arguments.get("goal") == self.state["goal"]:
            return {"task_id": self.state["task_id"], "segment_id": self.state.get("segment_id", "")}
        return {}

    def owns_operation(self, record: dict) -> bool:
        return bool(self.state.get("task_id") and record.get("task_id") == self.state["task_id"])

    def receipt(self, function: str, operation: str, payload: dict | None = None, *, status: str,
                operation_record: dict | None = None, task_id: str | None = None, segment_id: str | None = None):
        # Recovery must pass the original journal record. Defaults are only for
        # a direct current-task readback and preserve older persisted ledgers.
        if operation_record is not None:
            task_id, segment_id = operation_record.get("task_id"), operation_record.get("segment_id")
        task_id = self.state.get("task_id") if task_id is None and operation_record is None else task_id
        owner = self.state if task_id == self.state.get("task_id") else next(
            (item for item in reversed(self.archive) if task_id and item.get("task_id") == task_id), None)
        if owner is None:
            return None
        receipts = owner.setdefault("receipts", [])
        existing = next((item for item in receipts if item.get("operation_id") == operation), None)
        if segment_id is None:
            segment_id = (existing or {}).get("segment_id", owner.get("segment_id", ""))
        value = {"function": function, "operation_id": operation, "status": status,
            "commit_seq": (payload or {}).get("commit_seq"), "task_id": task_id, "segment_id": segment_id}
        arguments = (operation_record or {}).get("envelope", {}).get("arguments", {})
        for field in ("action", "action_id"):
            if isinstance(arguments.get(field), str):
                value[field] = arguments[field]
        if status in {"committed", "confirmed"} and (outcome := confirmed_outcome(payload)) is not None:
            value["confirmed_outcome"] = outcome
        if status in {"committed", "confirmed"}:
            targets = ((existing or {}).get("action_entity_ids", []) if operation_record is None else [])
            targets = targets or action_entity_ids(arguments, payload)
            if targets:
                value["action_entity_ids"] = targets
                self._remember_action_targets(owner, targets)
        if existing is not None:
            existing.update(value)
        else:
            receipts.append(value)
        limit = self.request_limit * self.world_call_limit
        if any(row.get("function") in AGENT_WRITE_FUNCTIONS and row.get("status") != "rejected"
               for row in receipts[:-limit]):
            owner["receipt_history_complete"] = False
        owner["receipts"] = receipts[-limit:]
        if owner is self.state and status == "unknown":
            self.state.update(status="unknown", resume_allowed=False)
        self._persist()
        return value

    @staticmethod
    def _remember_action_targets(owner, targets):
        # Most recently touched real objects remain readable at checkpoints and
        # recovery. The user's original selected contract is a separate fact.
        owner["action_entity_ids"] = list(dict.fromkeys(
            [*targets, *owner.get("action_entity_ids", [])]))[:MAX_ACTION_TARGETS]

    def preparation_read_delivered(self, function: str, arguments: dict, *, owner: dict, delivered: dict) -> bool:
        """Record a new delivered preparation query; never an action or receipt.

        The adapter has already checked successful projection and encoding.
        Ownership is captured before its read awaits; reads
        completing after a segment closes do not extend execution progress.
        """
        self.ensure_persistence_ready()
        if (not isinstance(owner, dict) or owner.get("purpose") != "execution" or
                not self.state.get("task_id") or not self.state.get("segment_id") or
                owner.get("task_id") != self.state["task_id"] or
                owner.get("segment_id") != self.state["segment_id"] or
                not self.active or self.state.get("status") != "running" or
                self.stopped or self.reclaiming or self.state.get("checkpoint") or self.unresolved):
            return False
        key = _preparation_read_signature(function, arguments)
        if key is None:
            return False
        delivered_facts = _preparation_fingerprints(function, key, delivered)
        if delivered_facts is None:
            return False
        fingerprints = self.state.setdefault("preparation_fingerprints", {})
        changes = {identity: digest for identity, digest in delivered_facts.items()
                   if fingerprints.get(identity) != digest}
        if not changes:
            return False
        # Keep bounded local hashes, not snapshots or an Agent memory service.
        # Never evict an old identity merely to let repeated pages count again.
        limit = self.request_limit * self.world_call_limit
        available = max(0, limit - len(fingerprints))
        accepted = {}
        for identity, digest in changes.items():
            if identity in fingerprints:
                accepted[identity] = digest
            elif available:
                accepted[identity] = digest
                available -= 1
        if not accepted:
            return False
        previous_fingerprints = dict(fingerprints)
        previous_count = self.state.get("preparation_count", 0)
        fingerprints.update(accepted)
        self.state["preparation_count"] = self.state.get("preparation_count", 0) + 1
        try:
            self._persist()
        except Exception:
            self.state.update(preparation_fingerprints=previous_fingerprints, preparation_count=previous_count)
            raise
        return True

    def effect_read_delivered(self, arguments: dict, *, owner: dict, delivered: dict) -> bool:
        """Bind a real read to its admitted task; model reports provide no facts.

        A restarted host preserves these historical references, but observation
        bundles are still owned by the SDK and cannot be rebuilt from them.
        """
        self.ensure_persistence_ready()
        if (not isinstance(owner, dict) or owner.get("purpose") != "execution" or
                not self.state.get("task_id") or not self.state.get("segment_id") or
                owner.get("task_id") != self.state["task_id"] or
                owner.get("segment_id") != self.state["segment_id"] or
                not self.active or self.state.get("status") != "running" or
                self.stopped or self.reclaiming or self.state.get("checkpoint") or self.unresolved):
            return False
        record = _effect_read_record(arguments, delivered)
        if record is None or record.get("role_id") != self.role_id:
            return False
        record.update(task_id=self.state["task_id"], segment_id=self.state["segment_id"],
            observed_before_action_count=self.state["actions_count"])
        previous = self.state.get("effect_evidence_reads", [])
        if any(row.get("observation_id") == record["observation_id"] for row in previous if isinstance(row, dict)):
            return False
        records = [*previous, record][-MAX_EFFECT_READS:]
        while len(records) > 1 and len(json.dumps(records, ensure_ascii=False, indent=2).encode("utf-8")) > MAX_EFFECT_READ_LEDGER_BYTES:
            records.pop(0)
        self.state["effect_evidence_reads"] = records
        try:
            self._persist()
        except Exception:
            self.state["effect_evidence_reads"] = previous
            raise
        return True

    def effect_read_references(self):
        """Small current-task index; kept observations remain past reads."""
        return [{key: deepcopy(row[key]) for key in
            ("observation_id", "entity_id", "definition_ref", "segment_id", "observed_before_action_count") if key in row}
            | {"observed_step": row.get("summary", {}).get("observed_step")}
            for row in self.state.get("effect_evidence_reads", [])
            if isinstance(row, dict) and row.get("task_id") == self.state.get("task_id")]

    def phase(self, phase: str, started: float, *, status="confirmed", error=None):
        value = {"phase": phase, "request": self.state.get("requests_used", 0),
            "elapsed_seconds": round(time.monotonic() - started, 3), "status": status}
        if error:
            value["error_type"] = type(error).__name__
            if isinstance(error, ConnectorError):
                value["error_code"] = error.code
        host = self.state["host_request"]
        ordinary = phase.startswith("settle_") and host.get("settlement_scope") in {"entry", "explain"}
        target = host if ordinary else self.state
        if ordinary:
            value.pop("request")
            value["generation"] = host.get("generation", 0)
        target["phase_timings"] = (target.get("phase_timings", []) + [value])[-32:]
        self._persist()

    def request_started(self, purpose: str) -> dict:
        self.ensure_persistence_ready()
        if purpose in {"entry", "explain"}:
            self.ensure_native_ready()
            if self.stopped:
                raise asyncio.CancelledError()
            # These use the same saved main native session, but do not spend
            # an explicit goal's grant or manufacture task/segment ownership.
            # The adapter persists this status with the native inference entry.
            host = self.state["host_request"]
            self.state["host_request"] = {"purpose": purpose, "status": "running", "generation": host.get("generation", 0) + 1}
            return {}
        if purpose not in {"execution", "execution_summary"}:
            return {}
        if self.stopped or self.reclaiming:
            raise asyncio.CancelledError()
        if self.state["requests_used"] >= self.state["max_requests"]:
            raise ConnectorError("ExecutionRequestBudget", "原目标的模型请求额度已用完。", 409)
        if self.state["host_request"].get("status") not in {"idle", "idle_confirmed"}:
            raise ConnectorError("OriginalRequestUnsettled", "原请求尚未确认停止；先结算原会话，再开始执行段。", 409)
        self.state["requests_used"] += 1
        self.state["request_status"] = "summarizing" if purpose == "execution_summary" else "running"
        host = self.state["host_request"]
        self.state["host_request"] = {"purpose": purpose, "status": self.state["request_status"],
            "generation": host.get("generation", 0) + 1}
        # The adapter persists this charge with its matching native inference
        # entry. A separate write here would split that original audit record.
        return {"task_id": self.state.get("task_id"), "segment_id": self.state.get("segment_id"),
            "request_number": self.state["requests_used"], "host_generation": self.state["host_request"]["generation"]}

    def request_returned(self, purpose: str) -> bool:
        """Adapter verified a complete terminal response from the main session.

        A returned ordinary response is distinct from a later native metadata
        idle confirmation. Interrupted responses must use the settlement port.
        """
        if purpose not in {"entry", "explain"} or self.stopped or self.reclaiming:
            return False
        host = self.state["host_request"]
        if host.get("purpose") != purpose or host.get("status") != "running":
            return False
        host["status"] = "idle"
        self._persist()
        return True

    def operation_unresolved(self, record: dict):
        """The transport could not find the receipt for this exact admitted write."""
        if record.get("execution_write") and self.owns_operation(record):
            self.state.update(status="unknown", resume_allowed=False)

    def settlement_started(self, stage: str, *, native_owner: dict | None = None) -> dict:
        host = self.state["host_request"]
        purpose = (native_owner or {}).get("purpose")
        ordinary = purpose in {"entry", "explain"} and not host.get("task_cleanup_pending")
        owner = {"task_id": self.state.get("task_id"), "segment_id": self.state.get("segment_id"),
            "native_purpose": purpose if ordinary else "execution", "host_generation": host.get("generation", 0),
            "reclaimed_request": self.state.get("request_status") == "unsettled" and
                self.state.get("control_status") == "revoked"}
        host["settlement_scope"] = owner["native_purpose"]
        if ordinary:
            host.update(status="settling", settlement={"stage": stage, "status": "checking", "elapsed_seconds": 0})
            self._persist()
            return owner
        self.active = False
        self.state.update(request_status="settling", resume_allowed=False,
            settlement={"stage": stage, "status": "checking", "elapsed_seconds": 0})
        self._persist()
        return owner

    def _current_event(self, owner: dict | None) -> bool:
        return owner is None or (owner.get("task_id") == self.state.get("task_id") and
            owner.get("segment_id") == self.state.get("segment_id") and
            ("host_generation" not in owner or owner["host_generation"] == self.state["host_request"].get("generation", 0)))

    def native_status_observed(self, native_status: str, *, owner: dict):
        if self._current_event(owner):
            target = self.state["host_request"] if owner.get("native_purpose") in {"entry", "explain"} else self.state
            target["settlement"]["native_status"] = native_status

    def settlement_finished(self, *, stage: str, elapsed_seconds: float, owner: dict) -> dict | None:
        """Adapter verified original session identity, native idle and write drain."""
        if not self._current_event(owner):
            return None
        if owner.get("native_purpose") in {"entry", "explain"}:
            host = self.state["host_request"]
            host["status"] = "idle_confirmed"
            host["settlement"].update(stage=stage, status="idle_confirmed", elapsed_seconds=elapsed_seconds)
            host.pop("error", None)
            if self.reclaiming:
                # An explicit reclaim closes the delegated task too. Ordinary
                # conversation cleanup alone never changes task settlement.
                self.state["request_status"] = "idle_confirmed"
            self._persist()
            return {"reclaimed": False}
        self.state["request_status"] = "idle_confirmed"
        self.state["host_request"]["status"] = "idle_confirmed"
        self.state["host_request"].pop("task_cleanup_pending", None)
        self.state["settlement"].update(stage=stage, status="idle_confirmed", elapsed_seconds=elapsed_seconds)
        reclaimed = bool(owner.get("reclaimed_request") and not self.unresolved and
            not self.delegation and not self.state.get("delegation_active"))
        if reclaimed and (self.state.get("error") or {}).get("code") in {"UnsettledHost", "NativeSessionBusy"}:
            self.state["error"] = None
        self._persist()
        return {"reclaimed": reclaimed}

    def settlement_failed(self, *, stage: str, elapsed_seconds: float, error: BaseException,
                          owner: dict) -> dict | None:
        if not self._current_event(owner):
            return None
        ordinary = owner.get("native_purpose") in {"entry", "explain"}
        target = self.state["host_request"] if ordinary else self.state
        native_status = (target.get("settlement") or {}).get("native_status")
        diagnostic = {"stage": stage, "status": "unconfirmed", "elapsed_seconds": elapsed_seconds,
            "error_type": type(error).__name__}
        if isinstance(error, ConnectorError):
            diagnostic["error_code"] = error.code
        if native_status:
            diagnostic["native_status"] = native_status
        labels = {"abort": "停止原请求", "session_identity": "读取原会话",
            "session_status": "读取执行状态", "write_drain": "核对已接纳行动"}
        message = ("原会话仍在运行；没有打开后续动作。" if native_status in {"busy", "retry"} else
            labels[stage] + "时状态未确认（" + type(error).__name__ + "）；没有打开后续动作。")
        failure = {"code": error.code if isinstance(error, ConnectorError) else "UnsettledHost", "message": message}
        if ordinary:
            target.update(settlement=diagnostic, status="unsettled", error=failure)
        else:
            self.active = False
            self.state["host_request"]["status"] = "unsettled"
            self.state.update(settlement=diagnostic, request_status="unsettled", resume_allowed=False, error=failure)
        self._persist()
        return deepcopy(failure)

    def native_response_observed(self, purpose: str, *, owner: dict | None = None):
        if purpose == "execution" and self._current_event(owner) and not self.stopped and not self.reclaiming:
            self.state["request_status"] = "returned"
            self.state["host_request"]["status"] = "returned"
            self._persist()

    def native_interrupted(self, purpose: str, *, owner: dict | None = None):
        if not self._current_event(owner):
            return
        if purpose in {"entry", "explain"}:
            self.state["host_request"]["status"] = "unsettled"
            self._persist()
            return
        self.active = False
        self.state.update(request_status="timed_out" if purpose == "execution" else "unsettled",
            resume_allowed=False)
        self.state["host_request"]["status"] = "unsettled"
        self._persist()

    def recovered_world_authority(self) -> bool:
        """Reconnection recovers originals, never starts inference or an old goal."""
        terminal = self.state.get("status") == "completed"
        if self.state.get("goal") and not terminal:
            self.state.update(status="unknown" if self.unresolved else "paused", resume_allowed=False)
        return bool(self.state.get("goal") and not terminal and not self.unresolved)

    def recovery_finished(self):
        if self.state.get("persistence_failure"):
            safe = (self.native_ready() and not self.unresolved and not self.delegation and
                not self.state.get("delegation_active") and not self.active and
                (not self.state.get("task_id") or self.state.get("readback_actual")))
            if safe:
                self.state["persistence_failure"] = None
                self._persist()
        self.state["resume_allowed"] = self.can_resume()
        self._persist()

    def ensure_history_recovery(self):
        recovery = self.state["history_recovery"]
        if recovery.get("attempts", 0):
            raise ConnectorError("HistoryRecoveryUnavailable", "本目标已使用一次历史整理；不会自动重复调用。", 409)
        if self.unresolved or self.delegation or self.state.get("delegation_active") or self.active:
            raise ConnectorError("HistoryRecoveryUnsafe", "先停止原授权并核对原操作，再整理历史。", 409)
        if self.state["max_requests"] - self.state["requests_used"] < 2:
            raise ConnectorError("HistoryRecoveryBudget", "历史整理和接续执行共需至少两次剩余模型请求。", 409)

    def history_recovery_started(self, *, session_id: str, before_message_ids: list[str]):
        self.ensure_history_recovery()
        if self.stopped or self.reclaiming:
            raise asyncio.CancelledError()
        self.state["history_recovery"].update(status="running", attempts=1, session_id=session_id,
            before_message_ids=list(before_message_ids))

    def summary_request_recorded(self, inference_number: int):
        # Persisted by the adapter with the exact native inference journal.
        self.state["history_recovery"]["inference_number"] = inference_number

    def history_recovery_verified(self, *, compaction_user_id: str, message_id: str,
                                  summary_sha256: str, summary_characters: int):
        self.state["history_recovery"].update(status="completed", compaction_user_id=compaction_user_id,
            message_id=message_id, summary_sha256=summary_sha256, summary_characters=summary_characters, error=None)
        # The adapter saves this verdict with the corresponding verified native
        # inference response, rather than splitting the original audit record.

    def history_recovery_interrupted(self, error: BaseException) -> dict:
        diagnostic = {"code": getattr(error, "code", "HistoryRecoveryInterrupted"),
            "message": "历史整理未确认完整成功；原记录保留，没有开始执行。"}
        self.state["history_recovery"].update(status="uncertain", error=diagnostic)
        self.state["resume_allowed"] = False
        return deepcopy(diagnostic)

    def history_recovery_unconfirmed(self, error: ConnectorError, *, original_request_idle: bool):
        self.state["history_recovery"].update(
            status="failed" if original_request_idle and error.code == "HistoryRecoveryInvalid" else "uncertain",
            error={"code": error.code, "message": error.message})
        self.state["resume_allowed"] = False
        self._persist()

    def selection_unavailable(self, context_entity_id: str | None, code: str):
        if (context_entity_id is not None and context_entity_id == self.state.get("context_entity_id") and
                not self.state.get("context_reference_attempted", True) and
                code in {"RuleViolation", "PermissionDenied", "ResourceNotFound"}):
            self.state["context_reference_attempted"] = True
            self._persist()

    def preflight_observed(self, facts: dict):
        self.state["world_results"] = self.world_results(facts, establish_reference=True)
        self.state.update(readback=deepcopy(facts), readback_actual=True)
        self._persist()

    def reclaim_interrupted(self, error: dict, *, persistence_failed=False):
        self.active = False
        if self.state.get("goal") or persistence_failed:
            self.state.update(request_status="unsettled", resume_allowed=False)
        if self.state.get("goal"):
            self.state.update(status="unknown" if self.unresolved or self.delegation or self.state.get("delegation_active") else "paused",
                error=deepcopy(error))

    def update_control(self, value: dict):
        self.normalize_progress()
        grant_id = self.state.get("grant_id")
        task_control = (isinstance(grant_id, str) and bool(grant_id) and
            value.get("grant_id") == grant_id and value.get("goal") == self.state["goal"] and
            bool(self.state.get("grant_operation_id")))
        if self.state.get("task_id"):
            if task_control and type(value.get("actions_count")) is int and value["actions_count"] >= 0:
                self.state["actions_count"] = max(self.state["actions_count"],
                    self.state.get("grant_action_offset", 0) + value["actions_count"])
        else:
            self.state.update({key: value[key] for key in ("goal", "max_actions", "actions_count") if key in value})
        if not self.state.get("task_id") or task_control:
            self.state["grant_version"] = value.get("version", self.state.get("grant_version"))
        self.state["control_status"] = value.get("status")
        self.state["delegation_active"] = value.get("delegation_active", value.get("status") == "active") is True
        if self.state.get("task_id") and not task_control:
            self.state["delegation_active"] = False

    def observe_control(self, function: str, payload: dict, *, operation_record=None):
        value = payload.get("result", payload)
        if function == "grove.control_grant":
            self.delegation = value.get("delegation", "")
            owned = operation_record is not None and self.owns_operation(operation_record)
            if owned:
                self.state["grant_operation_id"] = operation_record["envelope"]["operation_id"]
                # Bind authority from this task's original grant receipt. An
                # old receipt without a stable grant identity stays unknown.
                identity = value.get("grant_id")
                self.state["grant_id"] = identity if isinstance(identity, str) and identity else None
                if self.state.get("pending_grant_action_offset") is not None:
                    self.state["grant_action_offset"] = self.state.pop("pending_grant_action_offset")
                self.update_control(value)
            elif self.state.get("task_id"):
                # A late old grant still needs revocation, but cannot authorize
                # or change counters in a different current task.
                self.state["delegation_active"] = False
            else:
                self.update_control(value)
        elif function == "grove.control_revoke":
            self.update_control(value)
            self.delegation = ""

    def pause(self, reason: str, *, error=None):
        self.active = False
        completed = (self.state.get("goal_result") or {}).get("status") == "completed"
        self.state.update(status="unknown" if self.unresolved else "completed" if completed else "paused",
            stop_reason=reason, error=error, resume_allowed=False)

    def begin_reclaim(self, *, reason: str | None = None):
        # Technical shutdown still revokes authority, but does not rewrite an
        # already-paused task as a new deliberate user withdrawal.
        self._reclaim_stop_reason = reason or "你已收回后续行动；已成立结果保留。"
        self._preserve_reclaim_reason = bool(reason and self.state.get("status") in {"paused", "error"}
                                            and self.state.get("stop_reason"))
        self.reclaiming, self.stopped, self.active = True, True, False
        self.state["host_request"]["task_cleanup_pending"] = True
        self.state.update(request_status="settling", resume_allowed=False)
        self._persist()

    def record_reclaimed(self, revoke_error=None):
        if self.state.get("goal") and self.state.get("status") != "completed":
            self.state["status"] = "unknown" if self.unresolved or revoke_error else "paused"
            if not self._preserve_reclaim_reason:
                self.state["stop_reason"] = self._reclaim_stop_reason
            if revoke_error:
                self.state["error"] = {"code": revoke_error.code,
                    "message": "收回或原操作结果尚未确认；后续本机动作已停止，请恢复后核对。"}
        # Recording the world revoke never settles the native request. Only
        # finish_reclaim may expose continuation after the whole cleanup.
        self.state["resume_allowed"] = False
        self._persist()

    def finish_reclaim(self, *, error=None):
        self.reclaiming, self.active = False, False
        if error:
            self.state.update(status="unknown" if self.unresolved or self.delegation or self.state.get("delegation_active") else "paused",
                error=error, request_status="unsettled", resume_allowed=False)
        else:
            self.state["resume_allowed"] = self.can_resume()
        self._persist()

    def admit_write(self, segment_id: str | None):
        self.ensure_persistence_ready()
        if self.stopped or self.reclaiming:
            raise ConnectorError("DelegationRequired", "The person reclaimed control; stop writing.", 403)
        if self.state.get("status") == "idle" and not self.state.get("task_id"):
            raise ConnectorError("DelegationRequired", "No active action grant remains; stop writing.", 403)
        if not self.active or self.state.get("checkpoint"):
            raise ConnectorError("ExecutionSegmentFinished", "这一段已结束；可以读取当前事实，不能继续写入。", 409)
        if self.state.get("status") != "running":
            raise ConnectorError("ExecutionNotRunning", "当前执行受阻或尚未运行；先读取事实及核对原回执。", 409)
        if not self.delegation or not self.state.get("delegation_active"):
            raise ConnectorError("DelegationRequired", "No active action grant remains; stop writing.", 403)
        if not segment_id:
            raise ConnectorError("InvalidToolArguments", "缺少execution_segment_id；请原样使用当前执行段标识。", 422)
        if segment_id != self.state.get("segment_id"):
            raise ConnectorError("StaleExecutionSegment", "执行段标识不匹配；此调用没有执行，请读取当前执行段状态。", 409)

    async def readback(self, *, context_entity_id=None, action_entity_ids=None):
        started = time.monotonic()
        # A previous successful read is history while this new read is pending.
        self.state["readback_actual"] = False
        for item in list(self.state["receipts"]):
            if item.get("status") == "committed":
                receipt = await self.ports.read("receipt", operation_id=item["operation_id"])
                self.receipt(item["function"], item["operation_id"], receipt, status="confirmed",
                    task_id=item.get("task_id", self.state["task_id"]), segment_id=item.get("segment_id"))
        control = await self.ports.invoke("grove.control_status", {})
        self.update_control(control.get("result", control))
        selected = context_entity_id if context_entity_id is not None else self.state.get("context_entity_id")
        targets = self.state.get("action_entity_ids", []) if action_entity_ids is None else action_entity_ids
        arguments = {"context_entity_id": selected}
        if targets:
            arguments["action_entity_ids"] = targets
        facts = await self.ports.read("facts", **arguments)
        facts["task_action_summary"] = self.action_summary()
        unavailable = facts.get("selected_inspection_unavailable", {})
        self.selection_unavailable(unavailable.get("entity_id"), unavailable.get("code"))
        self.state["world_results"] = self.world_results(facts)
        self.state.update(readback=facts, readback_actual=True, readback_actions_count=self.state["actions_count"])
        self.phase("receipt_readback", started)
        self._persist()
        return facts

    async def revoke(self):
        if any(record.get("function_id") == "grove.control_revoke"
                for record in self.ports.unknown_operations()):
            raise ConnectorError("UnknownRevoke", "收回控制的原操作尚待核对；不会发起替代操作。", 409)
        if not (self.delegation or self.state.get("delegation_active") or self.state.get("status") in {"starting", "running"}):
            return
        try:
            await self.ports.invoke("grove.control_revoke", {})
        except ConnectorError as exc:
            self.state.update(status="unknown", error={"code": exc.code,
                "message": "收回控制的结果尚未确认；已停止本机后续动作，请恢复后核对原回执。"})
            self._persist()
            raise
        self._persist()

    async def write(self, function: str, arguments: dict, *, segment_id: str | None):
        self.admit_write(segment_id)
        started = time.monotonic()
        try:
            result = await self.ports.invoke(function, deepcopy(arguments) | {"delegation": self.delegation}, model_write=True)
        except BaseException as exc:
            if isinstance(exc, ConnectorError) and (exc.execution_outcome or {}).get("world_action_committed") is True:
                self.state.update(status="error", readback_actual=False,
                    error={"code": exc.code, "message": "原操作已提交；先核对原记录及现场，不能继续写入。"})
            try:
                self.phase("mcp_write", started, status="failed_or_unknown")
            finally:
                raise exc
        try:
            self.phase("mcp_write", started)
            targets = action_entity_ids(arguments, result)
            self._remember_action_targets(self.state, targets)
            facts = await self.readback(action_entity_ids=targets)
            projection = deepcopy(result)
            if (outcome := confirmed_outcome(result)) is not None:
                projection["confirmed_outcome"] = outcome
                projection["confirmed_outcome"]["commit_seq"] = result.get("commit_seq")
                projection["confirmed_outcome"]["meaning"] = "Facts at this original operation's commit; not a new observation, current scene, or free-text goal verdict."
            projection["execution_readback"] = facts
            projection["execution_control"] = {key: self.value(key) for key in
                ("actions_count", "max_actions", "remaining_actions", "delegation_active", "grant_version")}
        except Exception as exc:
            code = exc.code if isinstance(exc, ConnectorError) else type(exc).__name__
            self.state.update(status="error", readback_actual=False,
                error={"code": code, "message": "动作已提交，但当前结果未能核对；请恢复后查看。"})
            operation = result.get("operation_id")
            error = ConnectorError("ReadbackPending", "动作已经提交；后续现场读取失败，不代表动作未发生。先读取当前事实并核对原操作，不要重做。", 409,
                execution_outcome={"stage": "committed_readback_pending", "operation_id": operation,
                    "function_id": function, "commit_seq": result.get("commit_seq"),
                    "world_action_committed": True,
                    "receipt_confirmed": any(row.get("operation_id") == operation and row.get("status") == "confirmed"
                                             for row in self.state["receipts"]),
                    "current_facts_available": False, "readback_error": code})
            try:
                self._persist()
            finally:
                raise error from exc
        return projection

    async def execute_goal(self, goal: str, *, context_entity_id=None, resume=False):
        self.active = False
        try:
            self.ensure_persistence_ready()
            if not self.state.get("task_id") or goal != self.state.get("goal"):
                raise ConnectorError("InvalidTask", "执行只能使用本人明确授权的当前原目标。", 409)
            if self.stopped or self.reclaiming:
                raise asyncio.CancelledError()
            if not await self.ports.settle(abort=resume):
                state = "原模型会话仍在运行" if (self.state.get("settlement") or {}).get("native_status") in {"busy", "retry"} else "原模型会话状态未确认"
                self.pause(state + "；没有授予新动作或调用模型。", error=self.state.get("error"))
                return
            if resume:
                await self.ports.recover()
                if self.unresolved:
                    self.pause("原操作结果待核对；没有开始替代执行。")
                    return
                await self.readback(context_entity_id=context_entity_id)
                if self.state.get("delegation_active"):
                    await self.revoke()
                self.state["pending_grant_action_offset"] = self.state["actions_count"]
            recovery = self.state.get("history_recovery") or {}
            if recovery.get("requested") and recovery.get("status") != "completed":
                await self.ports.native_segment("history", goal, context_entity_id=context_entity_id)
                await self.readback(context_entity_id=context_entity_id)
            if self.stopped or self.reclaiming:
                raise asyncio.CancelledError()
            remaining = self.state["max_actions"] - self.state["actions_count"]
            if self.state["requests_used"] >= self.state["max_requests"]:
                self.pause("原任务的模型请求额度已用完；目标仍可未完成，已成立结果保留。")
                return
            if remaining <= 0:
                self.pause("原任务的世界动作额度已用完；已成立结果保留。")
                return
            await self.ports.invoke("grove.control_grant", {"goal": goal, "max_actions": remaining})
            if self.stopped or self.reclaiming:
                raise asyncio.CancelledError()
            if not self.delegation or not self.state["delegation_active"]:
                raise ConnectorError("DelegationRequired", "目标执行授权未能确认；没有调用模型。", 409)
            self.state.update(status="running", stop_reason="", error=None, resume_allowed=False)
            self._persist()
            while not self.stopped and not self.reclaiming:
                self.ensure_persistence_ready()
                if self.state["requests_used"] >= self.state["max_requests"]:
                    self.pause("模型请求额度已用完；目标未宣称完成。")
                    break
                if self.remaining_actions <= 0:
                    self.pause("原任务的世界动作额度已用完；已成立结果保留。")
                    break
                if not self.state["delegation_active"]:
                    self.pause("世界行动授权已结束；没有继续行动。")
                    break
                before = self.state["actions_count"]
                preparation_before = self.state.get("preparation_count", 0)
                self.state.update(checkpoint=None, request_status="preparing", resume_allowed=False,
                    segment_id="grove-segment-" + secrets.token_hex(16))
                self.active = True
                self._persist()
                failure = None
                try:
                    await self.ports.native_segment("execute", goal, context_entity_id=context_entity_id)
                except ConnectorError as exc:
                    failure = exc
                finally:
                    self.active = False
                if not await self.ports.settle(abort=failure is not None):
                    self.pause("上一段模型执行未确认停止；保留现场与原回执。", error=self.state.get("error"))
                    break
                await self.ports.recover()
                if self.unresolved:
                    self.pause("原行动结果待核对；没有发起新操作。")
                    break
                self.ensure_persistence_ready()
                if (self.state.get("checkpoint") or {}).get("status") != "completed":
                    await self.readback(context_entity_id=context_entity_id)
                action_progress = self.state["actions_count"] > before
                preparation_progress = self.state.get("preparation_count", 0) > preparation_before
                progress = action_progress or preparation_progress
                outcome = getattr(failure, "execution_outcome", None)
                yielded = (failure is not None and failure.code == "ExecutionSegmentYield" and
                    isinstance(outcome, dict) and outcome.get("stage") == "native_segment_yield" and
                    outcome.get("task_id") == self.state["task_id"] and
                    outcome.get("segment_id") == self.state["segment_id"] and outcome.get("safe") is True and
                    outcome.get("reason") in {"request_headroom", "request_over_budget"} and
                    type(outcome.get("current_terminal_tool_parts")) is int and outcome["current_terminal_tool_parts"] >= 1 and
                    type(outcome.get("current_unfinished_tool_parts")) is int and outcome["current_unfinished_tool_parts"] == 0 and
                    type(outcome.get("body_bytes")) is int and outcome["body_bytes"] >= 96 * 1024)
                checkpoint = self.state.get("checkpoint") or {}
                self.state["no_progress_segments"] = 0 if progress else self.state["no_progress_segments"] + 1
                self.state["segments"] = (self.state.get("segments", []) + [{
                    "request": self.state["requests_used"], "actions_before": before,
                    "actions_after": self.state["actions_count"], "progress": progress,
                    "preparation_before": preparation_before, "preparation_after": self.state.get("preparation_count", 0),
                    "task_id": self.state["task_id"], "segment_id": self.state["segment_id"],
                    "outcome": "context_yield" if yielded else "request_failure" if failure else "returned",
                    "yield": deepcopy(outcome) if yielded else None,
                    "checkpoint": checkpoint.get("status"), "error": failure.code if failure and not yielded else None}])[-self.request_limit:]
                self._persist()
                if self.stopped or self.reclaiming:
                    raise asyncio.CancelledError()
                if checkpoint.get("status") == "completed":
                    self.state.update(status="completed", stop_reason="Agent 已提交完成声明并结束本次委托。", error=None, resume_allowed=False)
                    break
                if checkpoint.get("status") == "blocked":
                    self.pause(checkpoint.get("summary") or "Agent 根据现场报告无法安全推进。")
                    break
                if not self.state["delegation_active"]:
                    self.pause("原任务的世界动作额度已用完；已成立结果保留。" if self.remaining_actions <= 0
                               else "世界行动授权已结束；没有继续行动。")
                    break
                if failure is not None and not yielded and (not action_progress or
                        failure.code in {"InternalContextResponse", "ExecutionSegmentYield"}):
                    self.pause(failure.message, error={"code": failure.code, "message": failure.message})
                    break
                if failure is not None and failure.code in {"ModelUnavailable", "HostAuthenticationFailed", "SavedSessionMissing"}:
                    self.pause("模型或原会话不可用；保留已确认进展，先解决连接阻塞。", error={"code": failure.code, "message": failure.message})
                    break
                if self.state["no_progress_segments"] >= 2:
                    self.pause("连续两段没有已确认的新行动或有效准备读取；停止重复推理。")
                    break
                self.state["status"] = "running"
                if action_progress and not checkpoint:
                    self.state["progress"] = {"summary": "这一段的实际行动已通过原回执与现场核对。", "next_intent": "依据最新现场继续原目标。",
                        "evidence": [deepcopy(item) for item in self.state["receipts"] if item.get("status") == "confirmed"][-8:]}
        except asyncio.CancelledError:
            if self.state["status"] not in {"unknown", "completed"}:
                self.pause(self.state["stop_reason"] if self._preserve_reclaim_reason else
                           self._reclaim_stop_reason if self.reclaiming else
                           "本机执行已中止；先核对原请求，已成立结果保留。")
            raise
        except ConnectorError as exc:
            completed = (self.state.get("goal_result") or {}).get("status") == "completed"
            self.state.update(status="unknown" if self.unresolved else "completed" if completed else "error",
                error={"code": exc.code, "message": exc.message})
            raise
        finally:
            self.active = False
            with contextlib.suppress(ConnectorError):
                await self.revoke()
            self.state["resume_allowed"] = self.can_resume()
            self._persist()

    def world_results(self, facts: dict | None, *, establish_reference=False):
        """The selected world's verdict; a free-text goal remains unverified."""
        may_establish = establish_reference and self.state.get("context_reference_attempted", True) is False
        if may_establish:
            self.state["context_reference_attempted"] = True
            self._persist()
        if not isinstance(facts, dict):
            return []
        selected = facts.get("selected_inspection")
        entity = selected.get("entity") if isinstance(selected, dict) else None
        identity = self.state.get("context_entity_id")
        if not identity or not isinstance(entity, dict) or entity.get("id") != identity or type(entity.get("version")) is not int or entity["version"] < 1:
            return []
        binding = entity.get("creative")
        contract = binding.get("contract") if isinstance(binding, dict) else None
        if (not isinstance(contract, dict) or not isinstance(binding.get("definition_ref"), str) or
                not isinstance(binding.get("type_id"), str) or not isinstance(contract.get("material"), str) or
                type(contract.get("terms_version")) is not int or contract["terms_version"] < 0 or
                type(contract.get("quantity")) is not int or contract["quantity"] < 1 or
                type(contract.get("delivered")) is not int or contract["delivered"] < 0):
            return []
        current_reference = {"entity_id": identity, "definition_ref": binding["definition_ref"],
            "type_id": binding["type_id"], "terms_version": contract["terms_version"],
            "material_id": contract["material"], "quantity_u": contract["quantity"]}
        reference = self.state.get("context_result_reference")
        if reference is None and may_establish:
            reference = deepcopy(current_reference)
            self.state["context_result_reference"] = reference
            self._persist()
        mismatches = [key for key, value in current_reference.items() if reference.get(key) != value] if isinstance(reference, dict) else []
        verification = "unbound" if not isinstance(reference, dict) else "reference_mismatch" if mismatches else "verified_complete" if contract.get("status") == "complete" else "not_complete"
        result = {"kind": "world_contract", "entity_id": identity, "entity_version": entity["version"],
            "entity_name": entity.get("name"), "definition_ref": binding["definition_ref"],
            "type_id": binding["type_id"], "terms_version": contract["terms_version"],
            "material_id": contract["material"], "quantity_u": contract["quantity"],
            "delivered_u": contract["delivered"], "server_status": contract.get("status"),
            "verification": {"status": verification, "scope": "bound_world_contract", "source": "authenticated_selected_inspection"},
            "reference": deepcopy(reference), "reference_mismatches": mismatches,
            "world_tick": binding.get("world_tick"), "observed_at": facts.get("observed_at")}
        for key in ("parties", "accepted", "beneficiary", "deadline_tick", "finished_tick", "completion_proof",
                    "released", "lost", "escrow_remaining", "physical_failure_tick", "failure_reason", "condition_error"):
            if key in contract:
                result[key] = deepcopy(contract[key])
        return [result]

    async def checkpoint(self, arguments: dict, *, schema: dict, write_busy=False):
        self.ensure_persistence_ready()
        if not Draft202012Validator(schema).is_valid(arguments):
            raise ConnectorError("InvalidCheckpoint", "执行检查点需要本任务标识、状态、真实发现及原回执引用。")
        # Diagnose the actual stop boundary before mismatched report IDs. An
        # ID error alone neither means control was reclaimed nor permits a
        # replacement ID, new grant or automatic resubmission.
        if self.reclaiming:
            raise ConnectorError("NoActiveExecution", "本人正在收回控制；这份报告没有记录为检查点，停止后续提交。已成立结果保留。", 409)
        if self.stopped:
            raise ConnectorError("NoActiveExecution", "本次执行已停止；这份报告没有记录为检查点，不能靠修改编号恢复执行。已成立结果保留。", 409)
        if not self.active or self.state.get("status") != "running":
            reason = "这一执行段已封闭" if self.state.get("checkpoint") else "当前执行段未在活动执行状态"
            raise ConnectorError("NoActiveExecution", reason + "；这份报告没有记录为检查点，不要重复提交或靠修改编号继续执行。", 409)
        if arguments["task_id"] != self.state.get("task_id"):
            raise ConnectorError("NoActiveExecution", "task_id与当前获准任务不一致；核对本次请求的execution_task.task_id，不沿用历史任务编号。报告未记录；编号不符不表示控制被收回。", 409)
        if arguments["execution_segment_id"] != self.state.get("segment_id"):
            raise ConnectorError("NoActiveExecution", "execution_segment_id与当前活动段不一致；核对本次请求的execution_task.segment_id，不沿用历史段编号。报告未记录；编号不符不表示控制被收回。", 409)
        if write_busy or self.unresolved:
            raise ConnectorError("UnconfirmedAction", "先核对原行动结果，再报告这一段结果。", 409)
        confirmed = {item["operation_id"]: item for item in self.state["receipts"] if item.get("status") == "confirmed" and
            item.get("task_id", self.state["task_id"]) == self.state["task_id"]}
        evidence = arguments["evidence_operation_ids"]
        if any(operation not in confirmed for operation in evidence):
            raise ConnectorError("UnconfirmedEvidence", "报告引用必须属于本任务实际已确认的原操作。", 409)
        read_ids = arguments.get("evidence_observation_ids", [])
        retained_reads = {row["observation_id"]: row for row in self.state.get("effect_evidence_reads", [])
            if isinstance(row, dict) and isinstance(row.get("observation_id"), str) and
            row.get("task_id") == self.state["task_id"] and row.get("role_id") == self.role_id and
            row.get("function_id") == "grove.effect_evidence"}
        if any(identity not in retained_reads for identity in read_ids):
            raise ConnectorError("UnconfirmedEvidenceRead", "观察引用必须是本任务实际已交付且仍保留的公开效果读取；旧任务、普通观察或未知标识不能作为这份读证据。", 409)
        evidence_reads = [deepcopy(retained_reads[identity]) for identity in read_ids]
        if arguments["status"] == "completed":
            action_evidence = any(confirmed[operation].get("function") in AGENT_WRITE_FUNCTIONS for operation in evidence)
            if not action_evidence and self.state.get("preparation_count", 0) <= 0:
                raise ConnectorError("ReadbackRequired", "先实际读取本任务需要的世界事实；没有行动或有效读取，不能仅凭口头提交完成声明。", 409)
        # All terminal reports need the same current facts. The adapter holds
        # the existing write lock while this read and segment closure complete.
        try:
            facts = await self.readback(context_entity_id=self.state.get("context_entity_id"))
        except Exception as exc:
            raise ConnectorError("CheckpointReadbackPending", "检查点尚未记录；最新现场未能读取。先核对当前事实及原回执，不要把旧读回当作本次结果。", 409,
                execution_outcome={"stage": "checkpoint_readback_pending", "task_id": self.state["task_id"],
                    "segment_id": arguments["execution_segment_id"], "checkpoint_recorded": False,
                    "segment_finished": False, "current_facts_available": False,
                    "readback_error": exc.code if isinstance(exc, ConnectorError) else type(exc).__name__}) from exc
        if self.reclaiming:
            raise ConnectorError("NoActiveExecution", "读取期间本人开始收回控制；这份迟到报告没有记录为检查点，停止后续提交。", 409)
        if self.stopped:
            raise ConnectorError("NoActiveExecution", "读取期间执行已停止；这份迟到报告没有记录为检查点，不能靠修改编号恢复执行。", 409)
        if arguments["status"] == "completed":
            if not self.state.get("readback_actual") or self.state.get("readback_actions_count") != self.state["actions_count"] or self.state.get("control_status") not in {"active", "exhausted"}:
                raise ConnectorError("ReadbackRequired", "最新行动结果尚未核对，不能提交完成声明。", 409)
        world_results = self.world_results(self.state.get("readback"))
        for record in evidence_reads:
            record["newer_task_actions_since_read"] = max(0,
                self.state["actions_count"] - record.get("observed_before_action_count", 0))
            record["scope"] = "current_task_delivered_public_effect_read"
        previous_report = {key: deepcopy(self.state.get(key)) for key in ("checkpoint", "progress", "goal_result", "status", "world_results")}
        self.state["world_results"] = world_results
        self.state["checkpoint"] = deepcopy(arguments)
        self.state["progress"] = {"summary": arguments["summary"], "next_intent": arguments["next_intent"],
            "evidence": [deepcopy(confirmed[operation]) for operation in evidence],
            "evidence_observation_ids": deepcopy(read_ids), "evidence_reads": deepcopy(evidence_reads)}
        if arguments["status"] == "completed":
            self.state["goal_result"] = {"status": "completed", "summary": arguments["summary"],
                "completion_source": "agent_claim", "verification": {"status": "unverified", "scope": "natural_language_goal"},
                "evidence_operation_ids": evidence, "actions_count": self.state["actions_count"],
                "evidence_observation_ids": deepcopy(read_ids), "evidence_reads": deepcopy(evidence_reads),
                "readback_actual": True, "world_results": deepcopy(world_results),
                "source": "Agent completion claim after confirmed operations or delivered preparation reads and fresh world readback; no natural-language adjudication"}
            self.state["status"] = "completed"
        self.active = False
        try:
            self._persist()
        except Exception as exc:
            self.state.update(previous_report)
            raise ConnectorError("CheckpointFeedbackUnavailable", "本机已经封闭这一段写入；检查点保存失败。恢复后核对原记录，可以读取现场，不能重做行动。", 409,
                execution_outcome={"stage": "checkpoint_persistence_failed", "task_id": self.state["task_id"],
                    "segment_id": arguments["execution_segment_id"], "checkpoint_status": arguments["status"],
                    "checkpoint_recorded": False, "checkpoint_persisted": False,
                    "segment_finished": True, "current_facts_available": False,
                    "world_action_committed": False, "readback_error": type(exc).__name__}) from exc
        result = {"task_id": self.state["task_id"], "status": arguments["status"], "segment_finished": True,
            "world_facts_changed": False, "world_results": deepcopy(world_results),
            "world_facts_changed_meaning": "This checkpoint writes no world facts. The readback can include changes since prior observations.",
            "execution_readback": deepcopy(facts),
            "report_meaning": "summary is the Agent's claim; execution_readback is the latest authorized read. Use its observed tick and values in your final response. Future effects remain conditional, not observed completion."}
        if arguments["status"] == "completed":
            result["goal_result"] = deepcopy(self.state["goal_result"])
        else:
            result["evidence_reads"] = deepcopy(evidence_reads)
        return result
