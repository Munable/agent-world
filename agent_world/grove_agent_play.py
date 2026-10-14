"""A bounded everyday decision view over already presented Grove observations.

The host calls ``begin_request`` once with its immutable request identity, then
``present`` for SDK-delivered reads/receipts.  A model selects a ``choice_ref``
and supplies only the remaining semantic parameters.  ``resolve`` consumes that
reference and returns an ordinary SDK intent; it never writes, authorizes,
retries, navigates, or certifies a goal.  The caller still submits through its
original controller, journal, observation guards and world Command.

References are private, in-memory, single-use and scoped to this client, role
and request.  Neither a saved transcript nor a read-only menu renews authority.
"""
from __future__ import annotations

from collections import OrderedDict
from copy import deepcopy
import hashlib
import json
import secrets
from typing import TYPE_CHECKING

from jsonschema import Draft202012Validator

from .grove_agent_errors import AgentClientError
from .grove_agent_observation import agent_tool_schema

if TYPE_CHECKING:
    from .grove_agent_client import GroveAgentClient


MAX_PLAY_BYTES = 8 * 1024
MAX_PLAY_DETAIL_BYTES = 48 * 1024
MAX_PLAY_REFS = 64
_RESERVE_BYTES = 512
_MENU_FUNCTIONS = frozenset({"grove.interact", "grove.rule_action"})
_PROTOCOL_FIELDS = frozenset({"observation_id", "base_observation_id",
                              "execution_segment_id", "delegation"})
_ENVELOPE_FIELDS = ("observation_id", "agent_observation", "operation_id",
                    "function_id", "function_version", "world_id", "snapshot_seq",
                    "commit_seq", "ok", "read_only", "replayed", "intent_fingerprint",
                    "confirmed_outcome")
_HOST_FIELDS = ("execution_task", "execution_control", "task_action_summary",
                "effect_evidence_reads", "detail_reads")


def _encoded(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"),
                      sort_keys=True, allow_nan=False).encode("utf-8")


def _drop_fields(schema, names):
    """Remove client-owned root inputs; authored nested parameters are opaque."""
    result = deepcopy(schema)
    properties = result.get("properties")
    if isinstance(properties, dict):
        for name in names:
            properties.pop(name, None)
    if isinstance(result.get("required"), list):
        result["required"] = [name for name in result["required"] if name not in names]
    return result


def _host_facts(source):
    """Keep task evidence values/scope while removing repeated host explanation."""
    result = {key: deepcopy(source[key]) for key in _HOST_FIELDS if key in source}
    summary = result.get("task_action_summary")
    if isinstance(summary, dict):
        summary.pop("meaning", None)
        for group in summary.get("groups", []):
            if isinstance(group, dict) and isinstance(group.get("last_confirmed_outcome"), dict):
                group["last_confirmed_outcome"].pop("meaning", None)
    task = result.get("execution_task")
    progress = task.get("progress") if isinstance(task, dict) else None
    if isinstance(progress, dict) and isinstance(progress.get("evidence"), list):
        fields = ("function", "operation_id", "status", "commit_seq", "task_id",
                  "segment_id", "action", "action_id", "action_entity_ids", "error",
                  "execution_outcome")
        omitted = sum(isinstance(row, dict) and any(key not in fields for key in row)
                      for row in progress["evidence"])
        progress["evidence"] = [{key: deepcopy(row[key]) for key in fields if key in row}
                                if isinstance(row, dict) else deepcopy(row)
                                for row in progress["evidence"]]
        if omitted:
            projection = progress.get("evidence_projection")
            progress["evidence_projection"] = (deepcopy(projection) if isinstance(projection, dict) else {}) | {
                "scope": "historical_operation_index", "omitted_detailed_records": omitted,
                "meaning": "Original receipts remain saved; task_action_summary retains bounded confirmed evidence."}
    return result


def _entity_view(entity):
    if not isinstance(entity, dict):
        return None
    value = {key: deepcopy(entity[key]) for key in (
        "id", "name", "version", "interaction_revision", "x", "y", "location",
        "owner", "container", "physical_parent", "lifecycle", "entity_kind",
        "created_by", "created_by_name", "source", "contents_visibility") if key in entity}
    description = entity.get("authored_description")
    if isinstance(description, str):
        value["authored_description_excerpt"] = description[:1200]
        value["description_truncated"] = len(description) > 1200
    elif isinstance(entity.get("authored_description_excerpt"), str):
        value["authored_description_excerpt"] = entity["authored_description_excerpt"]
        if "description_truncated" in entity:
            value["description_truncated"] = entity["description_truncated"]
    state = entity.get("discovery_state", entity.get("state"))
    if isinstance(state, dict):
        value["state"] = {key: deepcopy(state[key]) for key in (
            "kind", "open", "resource_kind", "stock", "water", "water_remaining",
            "burning", "broken", "destroyed", "retired") if key in state}
    creative = entity.get("creative")
    if isinstance(creative, dict):
        keys = ("definition_ref", "type_id", "hidden")
        if creative.get("hidden") is not True:
            keys += ("fields", "world_tick", "automatic_field_change", "automatic_field_change_status", "contract")
        value["creative"] = {key: deepcopy(creative[key]) for key in keys if key in creative}
    return value


class GroveAgentPlay:
    """Compact facts and exact choices; all control remains with the caller.

    ``present(delivered, request_scope=...)`` returns the normal successful MCP
    envelope with compact ``result`` / ``execution_readback``.  Complete real
    menu entries gain ``choice_ref`` and ``parameter_schema``; outlines have
    only a formal ``read_request``.  ``resolve`` returns
    ``{function_id, arguments, observation_id}`` for the original write route.
    Explicitly selected action/candidate details may expand beyond the everyday
    target up to the original SDK limit; default scene/index reads do not.
    """

    def __init__(self, client: GroveAgentClient, *, max_refs=MAX_PLAY_REFS,
                 max_bytes=MAX_PLAY_BYTES):
        if type(max_refs) is not int or not 4 <= max_refs <= MAX_PLAY_REFS:
            raise ValueError("max_refs must be between 4 and 64")
        if type(max_bytes) is not int or not 2048 <= max_bytes <= 48 * 1024:
            raise ValueError("max_bytes must be between 2048 and 49152")
        self.client, self.max_refs, self.max_bytes = client, max_refs, max_bytes
        self._identity = (id(client), client.role_id, client.world_instance)
        self._scope = None
        self._choices = OrderedDict()

    def begin_request(self, request_scope):
        """Invalidate every old choice, including repeated use of a scope."""
        if not isinstance(request_scope, str) or not 1 <= len(request_scope) <= 512:
            raise AgentClientError("InvalidPlayScope", "A fixed original request identity is required.", 422)
        self._check_identity()
        self._scope = request_scope
        self._choices.clear()

    def _check_identity(self):
        if (id(self.client), self.client.role_id, self.client.world_instance) != self._identity:
            raise AgentClientError("PlayIdentityChanged", "Rediscover with a new play client for this identity.", 409)

    def _check_scope(self, request_scope):
        self._check_identity()
        if not isinstance(request_scope, str) or self._scope is None or request_scope != self._scope:
            raise AgentClientError("PlayRequestExpired", "This read or choice belongs to an ended request. Read again under the current request.", 409)

    def _fits(self, value, *, reserve=True, max_bytes=None):
        limit = self.max_bytes if max_bytes is None else max_bytes
        try:
            text = self.client.observations.serialize(value)
        except Exception as exc:
            if getattr(exc, "code", None) == "ToolOutputTooLarge":
                return False
            raise
        return len(text.encode("utf-8")) <= limit - (_RESERVE_BYTES if reserve else 0)

    def _schema(self, function, schema):
        contract = self.client.function_contract(function)
        if contract.get("access") != "write":
            raise AgentClientError("PlayFunctionUnavailable", "This action is outside the discovered Agent write contract.", 403)
        return _drop_fields(agent_tool_schema(schema, function=function,
            guard_paths=contract.get("guard_paths")), _PROTOCOL_FIELDS)

    def _fingerprint(self, function):
        return hashlib.sha256(_encoded({"contract": self.client.function_contract(function),
            "input_schema": self.client.tool_schema(function)})).hexdigest()

    @staticmethod
    def _read_request(entity_id=None, action=None):
        if entity_id is None:
            return {"function_id": "map.viewport", "arguments": {}}
        arguments = {"entity_id": entity_id}
        if action is not None:
            arguments["action"] = action
        return {"function_id": "grove.inspect", "arguments": arguments}

    def present(self, delivered, *, request_scope, source_function=None, _expanded=False):
        """Project a trusted SDK delivery; never read hidden state or grant writes.

        Pass the identity captured when starting the read, not the mutable current
        request.  A late response is rejected before producing any new choices.
        """
        self._check_scope(request_scope)
        if not isinstance(delivered, dict):
            raise AgentClientError("InvalidPlayObservation", "An SDK-delivered observation is required.", 422)
        meta, observation_id = delivered.get("agent_observation"), delivered.get("observation_id")
        if (not isinstance(meta, dict) or meta.get("role_id") != self.client.role_id or
                meta.get("world_instance") != self.client.world_instance or
                meta.get("scope") != "client_presented" or not isinstance(observation_id, str)):
            raise AgentClientError("InvalidPlayObservation", "The observation does not belong to this client and role.", 409)
        if source_function is not None and source_function != meta.get("source_function"):
            raise AgentClientError("InvalidPlayObservation", "The supplied function differs from the delivered observation.", 409)
        try:
            bundle = self.client.observations.get(observation_id)
        except Exception as exc:
            raise AgentClientError("PlayObservationUnavailable", "This observation is unavailable; read again before choosing.", 409) from exc
        result = {key: deepcopy(delivered[key]) for key in _ENVELOPE_FIELDS if key in delivered}
        result.update(_host_facts(delivered))
        source = delivered.get("result", delivered)
        readback = delivered.get("execution_readback")
        selection = source.get("action_options_projection", {}) if isinstance(source, dict) else {}
        candidate_selection = selection.get("candidate_selection") if isinstance(selection, dict) else None
        selected_detail = (meta.get("source_function") == "grove.inspect" and
            meta.get("source_kind") == "query" and isinstance(selection, dict) and
            (selection.get("scope") == "selected_declared_action" or
             isinstance(candidate_selection, dict) and isinstance(candidate_selection.get("candidate_id"), str)))
        expanded = _expanded is True and selected_detail
        budget = MAX_PLAY_DETAIL_BYTES if expanded else self.max_bytes
        needs_expanded_details = False

        def fits(value, *, reserve=True):
            return self._fits(value, reserve=reserve, max_bytes=budget)

        result["result"] = {}
        if isinstance(source, dict):
            for key, value in _host_facts(source).items():
                if key not in result:
                    result[key] = value
                elif value != result[key]:
                    result["result"][key] = value
        if isinstance(readback, dict):
            result["execution_readback"] = {}
            for key, value in _host_facts(readback).items():
                if key not in result:
                    result[key] = value
                elif value != result[key]:
                    result["execution_readback"][key] = value
        result["play_projection"] = {
            "scope": "ordinary_play_decisions", "omitted_sections": [
                "expanded physics and immutable declarations", "unrelated histories and object snapshots"],
            "omitted_rows": {}, "read_request": self._read_request(),
            "byte_budget": {"ordinary_target": self.max_bytes, "current_limit": budget,
                "hard_limit": MAX_PLAY_DETAIL_BYTES, "expanded_selected_details": expanded},
            "meaning": "Selected authorized facts only. Current fields and saved automatic changes are distinct; a saved change is history, not this action's cause. Choices confer no authority. Expand omitted facts through formal reads."}
        if expanded:
            result["play_projection"]["detail_expansion"] = {
                "scope": "complete_selected_action_inputs_and_previews",
                "reason": "Selected action/candidate details exceed the ordinary target; the original SDK hard limit still applies."}
        # A detail expansion does not widen fixed receipt/goal capacity.
        if not self._fits(result, reserve=False):
            raise AgentClientError("PlayOutputTooLarge", "The original receipt or fixed task evidence exceeds this view; retain it and use formal reads.", 413)
        new_refs = []

        def add(destination, key, value, *, path):
            destination[key] = value
            if fits(result):
                return True
            destination.pop(key)
            result["play_projection"]["omitted_rows"][path] = 1
            return False

        def append(destination, key, value, *, path, ordinary=False):
            rows = destination.setdefault(key, [])
            rows.append(value)
            limit = self.max_bytes if ordinary else budget
            if self._fits(result, max_bytes=limit):
                return True
            rows.pop()
            counts = result["play_projection"]["omitted_rows"]
            counts[path] = counts.get(path, 0) + 1
            return False

        def menu(destination, options, entity, inspection, path):
            nonlocal needs_expanded_details
            if not isinstance(entity, dict) or not isinstance(entity.get("id"), str):
                return
            for option in options if isinstance(options, list) else []:
                if not isinstance(option, dict) or not isinstance(option.get("action"), str):
                    continue
                action, function = option["action"], option.get("function_id")
                request = self._read_request(entity["id"], action)
                outline = {key: deepcopy(option[key]) for key in (
                    "action", "label", "function_id", "available", "reason", "use") if key in option}
                outline["read_request"] = request

                def append_outline():
                    outline["details_expanded"] = False
                    if not append(destination, "action_options", outline, path=path + ".action_options"):
                        return
                    candidates = option.get("candidates_index", [])
                    inputs, groups = option.get("input"), option.get("choices")
                    candidates = list(candidates) if isinstance(candidates, list) else []
                    if isinstance(inputs, dict) and isinstance(inputs.get("candidates"), list):
                        candidates.extend(inputs["candidates"])
                    if isinstance(groups, dict):
                        for name in ("items", "others"):
                            if isinstance(groups.get(name), list):
                                candidates.extend(groups[name])
                    indexed = {candidate["id"]: candidate for candidate in candidates
                               if isinstance(candidate, dict) and isinstance(candidate.get("id"), str)}
                    if not indexed:
                        return
                    add(outline, "candidate_count", len(indexed), path=path + ".action_options.candidate_count")
                    for identity, candidate in indexed.items():
                        row = {key: deepcopy(candidate[key]) for key in ("id", "name", "kind") if key in candidate}
                        row["read_request"] = {"function_id": "grove.inspect", "arguments":
                            {"entity_id": entity["id"], "action": action, "candidate_id": identity}}
                        append(outline, "candidates_index", row, path=path + ".action_options.candidates_index")

                schema, arguments = option.get("input_schema"), option.get("arguments")
                known = bundle.get("versions", {}).get("entity", {}).get(entity["id"])
                retained_entity = destination.get("entity")
                complete = (function in _MENU_FUNCTIONS and option.get("available") is True and
                    isinstance(schema, dict) and isinstance(arguments, dict) and
                    arguments.get("target_id") == entity["id"] and arguments.get("action") == action and
                    type(known) is int and known > 0 and entity.get("version") == known and
                    isinstance(retained_entity, dict) and retained_entity.get("id") == entity["id"] and
                    retained_entity.get("version") == known)
                if not complete or len(new_refs) >= self.max_refs:
                    append_outline()
                    continue
                fixed = {"target_id": entity["id"], "action": action}
                if ("target_slot" in arguments and
                        schema.get("properties", {}).get("target_slot", {}).get("const") == arguments["target_slot"]):
                    fixed["target_slot"] = deepcopy(arguments["target_slot"])
                try:
                    semantic = self._schema(function, schema)
                    fingerprint = self._fingerprint(function)
                except AgentClientError:
                    append_outline()
                    continue
                reference = "play-" + secrets.token_hex(16)
                choice = {key: deepcopy(value) for key, value in option.items()
                          if key not in {"arguments", "input_schema", "mcp_tool"}}
                # A shared condition pointer may not survive the compact shape.
                pointer = choice.pop("preconditions_ref", None)
                if pointer is not None:
                    try:
                        condition = inspection
                        if not isinstance(pointer, str) or not pointer.startswith("#/"):
                            raise ValueError()
                        for part in pointer[2:].split("/"):
                            part = part.replace("~1", "/").replace("~0", "~")
                            condition = condition[int(part)] if isinstance(condition, list) else condition[part]
                        choice["preconditions"] = deepcopy(condition)
                    except (KeyError, IndexError, TypeError, ValueError):
                        append_outline()
                        continue
                choice.update(choice_ref=reference, fixed_arguments=deepcopy(fixed),
                    parameter_schema=_drop_fields(semantic, fixed), details_expanded=True,
                    read_request=request)
                values = {key: deepcopy(value) for key, value in arguments.items()
                          if key in semantic.get("properties", {}) and key not in fixed}
                if values:
                    choice["menu_parameter_values"] = values
                    choice["parameter_values_meaning"] = "Observed menu values; never automatically submitted."
                if append(destination, "action_options", choice, path=path + ".action_options"):
                    new_refs.append((reference, {"function_id": function, "arguments": fixed,
                        "observation_id": observation_id, "schema": semantic,
                        "parameter_schema": choice["parameter_schema"], "fingerprint": fingerprint}))
                else:
                    if selected_detail and not expanded:
                        needs_expanded_details = True
                    outline["details_reason"] = "Complete inputs/previews exceed this play view; use the formal action read."
                    append_outline()

        def view(source_view, destination, path, *, menus):
            if not isinstance(source_view, dict):
                return
            for key in ("clock", "observed_at", "bounds", "inventory_total", "inventory_next",
                        "truncated", "selected_inspection_unavailable", "world_results"):
                if key in source_view:
                    add(destination, key, deepcopy(source_view[key]), path=path + "." + key)
            player = source_view.get("player")
            if isinstance(player, dict):
                add(destination, "player", {key: deepcopy(player[key]) for key in
                    ("id", "name", "version", "x", "y", "backpack_id") if key in player}, path=path + ".player")
            for key in ("changes", "action_result"):
                if key in source_view:
                    add(destination, key, deepcopy(source_view[key]), path=path + "." + key)
            entity = source_view.get("entity")
            if isinstance(entity, dict):
                if not add(destination, "entity", _entity_view(entity), path=path + ".entity"):
                    # Keep a visible binding basis when public fields are too
                    # large; never borrow a version from the omitted raw row.
                    identity = {key: deepcopy(entity[key]) for key in (
                        "id", "name", "version", "interaction_revision", "x", "y", "location", "owner", "lifecycle") if key in entity}
                    add(destination, "entity", identity, path=path + ".entity_identity")
                if entity.get("id"):
                    destination["raw_read_request"] = self._read_request(entity["id"])
                if menus:
                    menu(destination, source_view.get("action_options", entity.get("action_options")),
                         entity, source_view, path)
            selected = source_view.get("selected_inspection")
            if isinstance(selected, dict):
                destination["selected_inspection"] = {}
                view(selected.get("result", selected), destination["selected_inspection"],
                     path + ".selected_inspection", menus=menus)
            # A later action-target read is distinct from both the original
            # receipt and the initially selected object. Preserve its facts
            # before spending the remaining view budget on scene discovery.
            for target in source_view.get("action_targets", []) if isinstance(source_view.get("action_targets"), list) else []:
                if not isinstance(target, dict):
                    continue
                row = {key: deepcopy(target[key]) for key in ("entity_id", "observed_at", "unavailable") if key in target}
                if not append(destination, "action_targets", row, path=path + ".action_targets"):
                    continue
                inspection = target.get("inspection")
                if isinstance(inspection, dict):
                    row["inspection"] = {}
                    view(inspection.get("result", inspection), row["inspection"],
                         path + ".action_targets.inspection", menus=menus)
            # The SDK may retain only nearby read entries after shedding full
            # snapshots. Preserve these discoveries before optional terrain;
            # an entry without a version is a read target, never an action basis.
            for key in ("nearby_discovery", "entities", "inventory", "nearby_entities", "resource_nodes", "water_sources", "contents"):
                for row in source_view.get(key, []) if isinstance(source_view.get(key), list) else []:
                    compact = _entity_view(row)
                    if compact is not None:
                        if compact.get("id"):
                            compact["read_request"] = self._read_request(compact["id"])
                        append(destination, key, compact, path=path + "." + key, ordinary=True)
            for chunk in source_view.get("chunks", []) if isinstance(source_view.get("chunks"), list) else []:
                append(destination, "chunks", deepcopy(chunk), path=path + ".chunks", ordinary=True)
            for key in ("players", "nearby_players", "visible_player_names"):
                for player_row in source_view.get(key, []) if isinstance(source_view.get(key), list) else []:
                    if isinstance(player_row, dict):
                        compact = {field: deepcopy(player_row[field]) for field in
                            ("id", "name", "version", "x", "y", "location") if field in player_row}
                        append(destination, key, compact, path=path + "." + key, ordinary=True)

        view(source, result["result"], "result", menus=meta.get("source_kind") != "receipt")
        if isinstance(readback, dict):
            view(readback, result["execution_readback"], "execution_readback", menus=True)
        movement_source = readback if isinstance(readback, dict) else source
        player = movement_source.get("player") if isinstance(movement_source, dict) else None
        known_player = bundle.get("versions", {}).get("player", {}).get(self.client.role_id)
        if (isinstance(player, dict) and player.get("id") == self.client.role_id and
                type(known_player) is int and known_player > 0 and player.get("version") == known_player and
                self.client.function_contract("grove.move").get("access") == "write"):
            schema = self._schema("grove.move", self.client.tool_schema("grove.move"))
            for dx, dy, label in ((0, -1, "向上走一格"), (0, 1, "向下走一格"),
                                  (-1, 0, "向左走一格"), (1, 0, "向右走一格")):
                if len(new_refs) >= self.max_refs:
                    break
                arguments = {"dx": dx, "dy": dy}
                if not Draft202012Validator(schema).is_valid(arguments):
                    continue
                reference = "play-" + secrets.token_hex(16)
                choice = {"choice_ref": reference, "function_id": "grove.move", "label": label,
                    "fixed_arguments": arguments, "parameter_schema": _drop_fields(schema, arguments),
                    "availability": "rechecked_on_submission"}
                if append(result["result"], "movement_choices", choice, path="result.movement_choices", ordinary=True):
                    new_refs.append((reference, {"function_id": "grove.move", "arguments": arguments,
                        "observation_id": observation_id, "schema": schema,
                        "parameter_schema": choice["parameter_schema"], "fingerprint": self._fingerprint("grove.move")}))
        if needs_expanded_details:
            # Re-project the same authorized delivery, not another world read.
            # No observation or refs from the ordinary attempt were published.
            return self.present(delivered, request_scope=request_scope,
                source_function=source_function, _expanded=True)
        if not fits(result, reserve=False):
            raise AgentClientError("PlayOutputTooLarge", "The bounded play projection cannot preserve its receipt and omission metadata.", 413)
        # The raw SDK observation contains facts this view deliberately hid.
        # Register the final visible view from scratch before publishing refs;
        # no old bundle/base may supply unseen versions or candidate previews.
        result = self.client.present_derived_view(delivered, result)
        if not fits(result, reserve=False):
            raise AgentClientError("PlayOutputTooLarge", "The final registered play envelope exceeds this bounded view.", 413)
        for reference, record in new_refs:
            while len(self._choices) >= self.max_refs:
                self._choices.popitem(last=False)
            self._choices[reference] = record | {"observation_id": result["observation_id"]}
        return result

    def resolve(self, choice_ref, parameters=None, *, request_scope):
        """Consume one exact selection and return an intent without submitting it."""
        self._check_scope(request_scope)
        if not isinstance(choice_ref, str) or choice_ref not in self._choices:
            raise AgentClientError("PlayChoiceExpired", "This choice is unknown, consumed or expired. Read current choices before deciding another action.", 409)
        if parameters is None:
            parameters = {}
        if not isinstance(parameters, dict):
            raise AgentClientError("InvalidPlayParameters", "Provide an object matching this choice's parameter_schema.", 422)
        record = self._choices[choice_ref]
        if self._fingerprint(record["function_id"]) != record["fingerprint"]:
            raise AgentClientError("PlayContractChanged", "The discovered action contract changed; read and choose again.", 409)
        if set(parameters) & (set(record["arguments"]) | _PROTOCOL_FIELDS):
            raise AgentClientError("FixedPlayIntent", "Parameters cannot replace the observed target, action or client-owned protocol fields.", 422)
        errors = list(Draft202012Validator(record["parameter_schema"]).iter_errors(parameters))
        arguments = deepcopy(record["arguments"]) | deepcopy(parameters)
        if errors or not Draft202012Validator(record["schema"]).is_valid(arguments):
            paths = [".".join(map(str, error.absolute_path)) or "$" for error in errors[:3]]
            raise AgentClientError("InvalidPlayParameters", "Parameters do not match the selected formal schema" +
                (" at " + ", ".join(paths) if paths else "") + ". No defaults or replacement inputs were supplied.", 422)
        # Check the original observation's versions/previews now; the existing
        # submission path binds them again and owns grant/operation admission.
        self.client.bind(record["function_id"], arguments, record["observation_id"])
        self._choices.pop(choice_ref)
        return {"function_id": record["function_id"], "arguments": arguments,
                "observation_id": record["observation_id"]}


__all__ = ["GroveAgentPlay", "MAX_PLAY_BYTES", "MAX_PLAY_DETAIL_BYTES", "MAX_PLAY_REFS"]
