"""Fixed, presented Agent observations for the Shared Grove client.

This component owns projection and the immutable observations used to bind
writes. It has no world, host, UI or operation-recovery access. The caller
supplies already-authorized responses and the current function schema.
"""
from __future__ import annotations

from collections.abc import Callable
from copy import deepcopy
import json
import re
import secrets

from .grove_connector_errors import ConnectorError

AGENT_WRITE_FUNCTIONS = frozenset({"grove.join", "grove.move", "grove.interact", "grove.build", "grove.generation_context",
    "grove.content_submit", "grove.content_release", "grove.definition_publish", "grove.rule_action", "grove.fabricate"})
AGENT_ACTION_FUNCTIONS = frozenset({"grove.join", "grove.move", "grove.interact", "grove.build", "grove.rule_action", "grove.fabricate"})

_NATIVE_TOOL_MAX_BYTES = 50 * 1024
_NATIVE_TOOL_MAX_LINES = 2000
_AGENT_TOOL_MAX_BYTES = 48 * 1024
_AGENT_TOOL_MAX_LINES = 1800
_AGENT_FACT_TARGET_BYTES = 8 * 1024


def execution_private_context(context: dict, goal: str) -> dict:
    """Keep preferences, without reissuing retired creation requests as intent."""
    result = {key: deepcopy(context[key]) for key in ("intent", "preferences") if key in context}
    result["current_execution_goal"] = goal
    result["projection"] = {"scope": "current_execution_preferences",
        "meaning": "Only this granted goal is active. Private intent/preferences are not world facts "
                   "or renewed authorization; private memory remains the user's Agent's responsibility."}
    return result


def nearby_public_events(page: dict, player: dict, entities: list) -> list:
    """Keep actual actor records; a visible object's author is not its operator."""
    visible = {row["id"] for row in entities if isinstance(row, dict) and isinstance(row.get("id"), str)}
    result = []
    for event in page.get("events", []):
        value = event.get("payload", {})
        kind = event.get("kind")
        x, y = value.get("x"), value.get("y")
        located = (type(x) is int and type(y) is int and
            max(abs(x - player["x"]), abs(y - player["y"])) <= 8)
        known_public_action = kind == "rule_action" and value.get("entity_id") in visible
        if (kind not in {"interact", "build", "content_published", "content_extended", "outcome", "rule_action"}
                or not (located or known_public_action)):
            continue
        row = {key: deepcopy(event[key]) for key in
            ("event_id", "seq", "actor_role_id", "kind", "occurred_at") if key in event}
        row["payload"] = {key: deepcopy(value[key]) for key in ("x", "y", "action", "entity_id", "recipe", "summary",
            "content_id", "content_version", "contribution", "outcome_refs", "anchors", "parts") if key in value}
        result.append(row)
    return result


def blueprint_model_input(payload: dict) -> dict:
    """Remove repeated declarations only from the model's copy of facts."""
    model_input = deepcopy(payload)
    if model_input.get("blueprint_schema"):
        model_input.pop("content_schema", None)
    model_input.pop("shape_example", None)
    goal = model_input.get("goal")
    private = model_input.get("private_context")
    if isinstance(private, dict):
        if private.get("last_creation_request") == goal:
            private.pop("last_creation_request", None)
        requests = private.get("recent_user_requests")
        if isinstance(requests, list):
            private["recent_user_requests"] = [entry for entry in requests
                if not (entry == goal or isinstance(entry, dict) and entry.get("text") == goal)]

    catalog = model_input.get("catalog")
    if isinstance(catalog, dict) and isinstance(catalog.get("compositions"), list):
        def spec_key(spec):
            return json.dumps(spec, sort_keys=True, separators=(",", ":"))
        definitions = {spec_key(item["composition"]): item for item in catalog["compositions"]
            if isinstance(item, dict) and isinstance(item.get("composition"), dict)}
        def differences(actual, base):
            result = {}
            for key, value in actual.items():
                if key not in base:
                    result[key] = deepcopy(value)
                elif isinstance(value, dict) and isinstance(base[key], dict):
                    delta = differences(value, base[key])
                    if delta:
                        result[key] = delta
                elif value != base[key]:
                    result[key] = deepcopy(value)
            return result
        shortcuts = catalog.get("objects")
        if isinstance(shortcuts, list):
            aliases = []
            for item in shortcuts:
                base = definitions.get(spec_key(item.get("composition"))) if isinstance(item, dict) else None
                if base is None or not isinstance(item.get("template"), str):
                    aliases.append(item)
                    continue
                alias = {"template": item["template"], "composition": deepcopy(item["composition"])}
                overrides = differences({key: value for key, value in item.items()
                    if key not in {"template", "composition"}}, base)
                if overrides:
                    alias["definition_overrides"] = overrides
                aliases.append(alias)
            catalog["objects"] = aliases
            catalog["shortcut_guidance"] = (
                "Each shortcut references its complete compositions entry. "
                "definition_overrides contains only actual differing fields; apply those to that entry.")
        registry = catalog.get("composition_registry")
        if isinstance(registry, dict) and isinstance(registry.get("constructions"), list):
            for construction in registry["constructions"]:
                if not isinstance(construction, dict):
                    continue
                materials, sizes = construction.get("materials"), construction.get("sizes")
                linings = construction.get("lining", [])
                if not (isinstance(materials, list) and materials and isinstance(sizes, list) and sizes
                        and isinstance(linings, list)):
                    continue
                specs = [{"material": material, "construction": construction.get("id"), "size": size}
                    for material in materials for size in sizes]
                specs += [{**spec, "lining": lining} for spec in list(specs) for lining in linings]
                if all(spec_key(spec) in definitions for spec in specs):
                    # Every legal combination keeps its full resolved matter,
                    # components and cost. Unknown declarations, including
                    # lining_definition, stay until equivalence is established.
                    for key in ("variants", "scales", "state"):
                        construction.pop(key, None)

    public = model_input.get("public_content")
    anchors = public.get("anchors", []) if isinstance(public, dict) else []
    entities = model_input.get("public_entities")
    if isinstance(anchors, list) and isinstance(entities, list):
        for index, entity in enumerate(entities):
            if not isinstance(entity, dict) or not entity.get("id"):
                continue
            anchor = next((item for item in anchors if isinstance(item, dict) and
                item.get("entity_id") == entity["id"] and item.get("version") == entity.get("version") and
                isinstance(item.get("entity"), dict) and item["entity"].get("id") == entity["id"] and
                item["entity"].get("version") == entity.get("version") and
                all(key in item["entity"] and item["entity"][key] == value
                    for key, value in entity.items())), None)
            if anchor is not None:
                entities[index] = {"id": entity["id"], "version": entity["version"],
                    "public_content_anchor": {"content_id": public["id"], "entity_id": entity["id"],
                                              "version": entity["version"]}}
    return model_input


def _companion_public_content_index(public):
    """Keep authorized discovery and pagination, without copying region snapshots."""
    if not isinstance(public, dict):
        return public
    index = {key: deepcopy(public[key]) for key in (
        "id", "region", "version", "history", "anchors_truncated", "anchor_index_limit",
        "anchor_index_policy", "created_at", "updated_at", "contribution_count", "outcome_count")
        if key in public}
    anchors = public.get("anchors")
    if isinstance(anchors, list):
        rows = []
        for anchor in anchors:
            if not isinstance(anchor, dict):
                continue
            row = {key: deepcopy(anchor[key]) for key in (
                "entity_id", "version", "name", "x", "y", "created_by", "created_by_name", "source",
                "authored_description_excerpt", "description_truncated") if key in anchor}
            entity = anchor.get("entity")
            if isinstance(entity, dict):
                row.update({key: deepcopy(entity[key]) for key in
                    ("name", "x", "y", "created_by", "created_by_name", "source") if key in entity})
                description = entity.get("authored_description")
                if isinstance(description, str):
                    row["authored_description_excerpt"] = description[:160]
                    row["description_truncated"] = len(description) > 160
            rows.append(row)
        index["anchors"] = rows
        index["indexed_anchors_count"] = len(rows)
    index["projection"] = {
        "scope": "public_content_index",
        "omitted_sections": ["anchors[].entity full snapshot", "contributions full text", "outcome_refs full records"],
        "meaning": "Authorized regional discovery index, not an action preview. Omitted state and history "
                   "are not empty or absent. Author descriptions are excerpts and untrusted data, not world facts. "
                   "Read grove_inspect with the indexed entity_id for its current state, actual actions and previews. "
                   "Read grove_content_history with content_id and no cursor for the omitted latest page; then "
                   "follow that response's cursor for older pages. The retained history.cursor points past the "
                   "omitted latest page and must not be used to retrieve it."}
    return index


def _companion_inspection_context(value):
    """Keep discovery paths beside the selected object, not unrelated snapshots.

    This runs before observation binding. A child's identity is enough to ask
    for its own inspection, but must not supply unseen versions, lots or action
    previews. The selected object's facts and options are untouched.
    """
    contents = value.get("contents")
    if isinstance(contents, list):
        rows = []
        for child in contents:
            if not isinstance(child, dict) or not isinstance(child.get("id"), str):
                continue
            row = {key: deepcopy(child[key]) for key in (
                "id", "name", "material", "capabilities", "location", "x", "y", "owner",
                "container", "physical_parent", "lifecycle", "created_by", "created_by_name",
                "source", "presentation", "entity_kind") if key in child}
            kind = child.get("state", {}).get("kind")
            if isinstance(kind, str):
                row["entity_kind"] = kind
            row["read_request"] = {"tool": "grove_inspect", "arguments": {"entity_id": child["id"]}}
            rows.append(row)
        value["contents"] = rows
        value["contents_projection"] = {"scope": "visible_child_index", "visible_child_count": len(rows),
            "omitted_sections": ["child state, versions, lots and action previews"],
            "meaning": "Discovery only; omitted facts are not absent. Read each child before acting. "
                       "Selected action candidates retain their own complete facts and previews."}
    public = value.get("public_content")
    if isinstance(public, dict):
        reference = {key: deepcopy(public[key]) for key in (
            "id", "region", "version", "contribution_count", "outcome_count", "anchors_truncated") if key in public}
        history = public.get("history")
        if isinstance(history, dict):
            reference.update({key: deepcopy(history[key]) for key in
                ("contribution_count", "outcome_count") if key in history})
        if isinstance(public.get("id"), str):
            reference["read_request"] = {"tool": "grove_content_history",
                                         "arguments": {"content_id": public["id"]}}
        reference["discovery_read_request"] = {"tool": "map_viewport", "arguments": {}}
        reference["projection"] = {"scope": "regional_content_reference",
            "omitted_sections": ["regional anchors and historical pages"],
            "meaning": "Regional context is unexpanded, not absent or action permission. "
                       "History read starts at the latest page; follow its returned cursor."}
        value["public_content"] = reference


def _companion_chunk_terrain(chunk):
    """Encode only actual tiles: at most 16 rows and 256 unknown indexes."""
    if not isinstance(chunk, dict):
        return chunk
    compact = {key: deepcopy(value) for key, value in chunk.items()
               if key not in {"tiles", "entity_ids", "player_ids", "tile_array_omitted"}}
    coordinates_known = type(chunk.get("cx")) is int and type(chunk.get("cy")) is int
    if coordinates_known:
        origin = {"x": chunk["cx"] * 16, "y": chunk["cy"] * 16}
        compact["bounds"] = {"min_x": origin["x"], "min_y": origin["y"],
                             "max_x": origin["x"] + 15, "max_y": origin["y"] + 15}
    compact["chunk_size"] = 16
    # Current preflight/readback can pass through this projection twice. The
    # first pass's authenticated encoding must survive the second unchanged.
    if "tiles" not in chunk and isinstance(chunk.get("terrain"), dict):
        return compact
    tiles = chunk.get("tiles")
    tiles = tiles if isinstance(tiles, list) else []
    codes = {"grass": "G", "dirt": "D", "water": "W", "rock": "R"}
    cells, unknown = [], []
    for index in range(256):
        tile = tiles[index] if index < len(tiles) else None
        code = codes.get(tile, "?") if isinstance(tile, str) else "?"
        cells.append(code)
        if index < len(tiles) and code == "?":
            unknown.append(index)
    terrain = {"rows": ["".join(cells[start:start+16]) for start in range(0, 256, 16)],
               "legend": {"G": "grass", "D": "dirt", "W": "water", "R": "rock", "?": "unknown_or_missing"},
               "complete": coordinates_known and len(tiles) == 256 and not unknown,
               "source_tile_count": len(tiles)}
    if coordinates_known:
        terrain["origin"] = origin
    else:
        terrain["coordinates_known"] = False
    if len(tiles) < 256:
        terrain["missing_index_range"] = [len(tiles), 255]
    if len(tiles) > 256:
        terrain["unprojected_extra_tile_count"] = len(tiles) - 256
    if unknown:
        terrain["unknown_indexes"] = unknown
    compact["terrain"] = terrain
    return compact


def _companion_creative_summary(creative):
    """Retain authorized current fields and runtime evidence without declarations."""
    summary = {key: deepcopy(creative[key]) for key in
               ("definition_ref", "type_id", "hidden") if key in creative}
    if creative.get("hidden") is True:
        return summary
    for key in ("fields", "world_tick", "automatic_field_change", "automatic_field_change_status",
                "automatic_rule_count", "explicit_action_count", "automatic_rules_projection",
                "port_bindings", "connections", "contract", "definition_projection"):
        if key in creative:
            summary[key] = deepcopy(creative[key])
    deployment = creative.get("deployment")
    if isinstance(deployment, dict):
        summary["deployment"] = {key: deepcopy(deployment[key]) for key in (
            "admitted", "publicly_placed", "ordinary_rules_enabled", "public_event_input_available",
            "mechanical_solver_eligible", "mechanical_blockers") if key in deployment}
    process = creative.get("process")
    if isinstance(process, dict):
        summary["process"] = {key: deepcopy(process[key]) for key in
            ("status", "steps", "last_tick", "observed_tick", "steps_meaning", "status_meaning",
             "last_tick_meaning", "error_diagnostic", "error_diagnostic_omitted") if key in process}
        if (process.get("status") == "error" and "error_diagnostic" not in process and
                "error_diagnostic_omitted" not in process):
            summary["process"]["error_diagnostic_projection"] = "Exact failure location was not saved; do not infer it."
        for key in ("error", "blocked_reason"):
            if isinstance(process.get(key), str):
                summary["process"][key] = process[key][:180]
        if isinstance(process.get("blocked_rules"), list):
            summary["process"]["blocked_rules"] = [{key: (row[key][:120] if isinstance(row[key], str)
                else deepcopy(row[key])) for key in ("rule_id", "phase", "reason") if key in row}
                for row in process["blocked_rules"][:8] if isinstance(row, dict)]
        previous = process.get("last_reaction")
        if isinstance(previous, dict):
            summary["process"]["last_reaction"] = {key: deepcopy(previous[key]) for key in
                ("source_last_tick", "historical", "current_production") if key in previous}
    mechanical = creative.get("mechanical_observation")
    if isinstance(mechanical, dict):
        observation = {key: deepcopy(mechanical[key]) for key in (
            "role", "role_meaning", "angle_unit", "travel", "pitch", "solver_eligible", "stored_output_is_history", "pending_next_step", "observed_tick", "meaning")
            if key in mechanical}
        for key in ("current_inputs", "saved_outputs"):
            if isinstance(mechanical.get(key), dict):
                observation[key] = deepcopy(mechanical[key])
        stored = mechanical.get("stored_state")
        if isinstance(stored, dict) and not observation.get("saved_outputs"):
            observation["stored_state"] = {key: deepcopy(stored[key]) for key in
                ("position", "angle", "speed", "energy", "enabled", "status", "available_torque")
                if key in stored}
        for key, fields in (("last_solved", ("step", "historical", "meaning")),
                            ("drive_request", ("enabled", "requested_step", "applies_from_step"))):
            if (key == "last_solved" and "saved_outputs" in observation or
                    key == "drive_request" and "current_inputs" in observation):
                continue
            record = mechanical.get(key)
            if record is None and key in mechanical:
                observation[key] = None
            elif isinstance(record, dict):
                observation[key] = {field: deepcopy(record[field]) for field in fields if field in record}
                source = record.get("source")
                if key == "drive_request" and isinstance(source, dict):
                    observation[key]["source"] = {field: deepcopy(source[field]) for field in
                        ("kind", "rule_id", "phase", "action_id", "actor_role_id") if field in source}
        summary["mechanical_observation"] = observation
    return summary


def _companion_current_entity(entity):
    """Current facts without a second copy of immutable declarations/history."""
    result = deepcopy(entity)
    creative = result.get("creative")
    program = result.get("state", {}).get("program")
    if isinstance(creative, dict):
        if isinstance(program, dict):
            for field in ("fields", "process", "mechanical", "drive_request", "mechanical_last_solved",
                          "contract", "port_bindings", "connections"):
                if field in program and field in creative and program[field] == creative[field]:
                    program.pop(field)
            mechanical = creative.get("mechanical_observation")
            if isinstance(mechanical, dict) and isinstance(mechanical.get("current_inputs"), dict):
                # The world supplied both blocks. Bare mechanical values mix
                # current control/fuel with saved output and add no new fact.
                result.pop("mechanical", None)
                creative.pop("mechanical", None)
                program.pop("mechanical", None)
        omitted = [field for field in ("material_definitions", "declaration_semantics", "material_semantics")
                   if field in creative]
        if omitted and isinstance(creative.get("definition_ref"), str):
            for field in omitted:
                creative.pop(field)
            creative["definition_projection"] = {"omitted_sections": omitted,
                "read_request": {"tool": "grove_definition_read", "arguments": {
                    "definition_ref": creative["definition_ref"]}},
                "meaning": "Immutable declaration metadata is unexpanded; current instance facts remain."}
        if isinstance(creative.get("mechanical_observation"), dict):
            compact = _companion_creative_summary(creative).get("mechanical_observation")
            creative["mechanical_observation"] = compact
    return result


def _companion_scene_index(scene, *, preflight=False):
    """An authorized discovery index; detailed physics remains an explicit read."""
    if not isinstance(scene, dict):
        return scene
    index = {key: deepcopy(scene[key]) for key in (
        "player", "players", "nearby_players", "clock", "bounds", "inventory_total",
        "inventory_next", "truncated", "region_entity_count", "selected_inspection", "observed_at",
        "world_results", "selected_inspection_unavailable", "action_targets", "action_targets_meaning",
        "task_action_summary", "public_events", "fact_budget", "opportunities", "discovery_projection",
        "detail_reads", "execution_control", "execution_task", "nearby_discovery",
        "nearby_discovery_projection", "visible_player_names", "visible_player_names_projection") if key in scene}
    if isinstance(scene.get("chunks"), list):
        index["chunks"] = [_companion_chunk_terrain(chunk) for chunk in scene["chunks"]]
    construction = scene.get("construction")
    if isinstance(construction, dict):
        index["construction"] = {key: deepcopy(construction[key]) for key in
                                 ("material_totals", "unit", "radius_metric", "limits") if key in construction}
        if preflight:
            index["construction"]["material_source"] = "Eligible owned bare lots; totals are u, not regional stock."
            index["construction"]["read_request"] = {"tool": "map_viewport", "arguments": {}}
        elif isinstance(construction.get("recipes"), list):
            index["construction"]["recipes"] = [{key: deepcopy(recipe[key]) for key in
                ("id", "name", "terrain", "cost", "material_cost_u", "unit", "use") if key in recipe}
                for recipe in construction["recipes"] if isinstance(recipe, dict)]
    for field in ("entities", "inventory", "nearby_entities", "resource_nodes", "water_sources"):
        if isinstance(scene.get(field), list):
            rows = []
            for entity in scene[field]:
                if not isinstance(entity, dict):
                    rows.append(deepcopy(entity))
                    continue
                row = {key: deepcopy(entity[key]) for key in (
                    "id", "version", "interaction_revision", "name", "x", "y", "location", "owner", "physical_parent",
                    "entity_ref", "material", "capabilities", "container", "lifecycle", "created_by", "created_by_name",
                    "source", "barrier_active", "contents_visibility", "content_summary", "matter_unit",
                    "pour_target_slot", "execution_domain") if key in entity}
                for detail in ("fabrication_materials", "material_eligibility", "acquisition", "acquisition_sources"):
                    if detail in entity:
                        row[detail] = deepcopy(entity[detail])
                if "container" in entity:
                    row["parent_container_id"] = entity["container"]
                kind = entity.get("state", entity.get("discovery_state", {})).get("kind")
                if isinstance(kind, str):
                    row["entity_kind"] = kind
                # Viewport matter is an authorized quantity summary, not a list
                # of transferable lots. Never manufacture lot IDs from it.
                if isinstance(entity.get("matter"), list):
                    row["matter"] = [{key: deepcopy(lot[key]) for key in
                        ("material", "material_name", "material_import", "slot", "quantity", "role", "burning") if key in lot}
                        for lot in entity["matter"] if isinstance(lot, dict)]
                    quantities = entity.get("material_quantity")
                    row["matter_unit"] = (quantities.get("unit", "logical_matter")
                        if isinstance(quantities, dict) else row.get("matter_unit", "logical_matter"))
                elif "material_quantity" in entity:
                    row["material_quantity"] = deepcopy(entity["material_quantity"])
                creative = entity.get("creative")
                contact = entity.get("simulation", {}).get("contact") or entity.get("contact")
                if isinstance(contact, dict):
                    process = contact.get("process") or contact
                    row["contact"] = {"candidate_count": contact.get("candidate_count", 0),
                        "status": process.get("status", "waiting"), "steps": process.get("steps", 0),
                        "process_is_history": contact.get("process_is_history", True),
                        "read_request": {"function": "grove.inspect", "arguments": {"entity_id": entity["id"]}}}
                if isinstance(creative, dict):
                    # Only the world's authorized creative projection can
                    # supply runtime observations; raw program storage cannot.
                    if preflight:
                        row["creative"] = {key: deepcopy(creative[key]) for key in
                            ("definition_ref", "type_id", "hidden", "fields", "world_tick",
                             "automatic_rule_count", "explicit_action_count") if key in creative}
                        if creative.get("hidden") is True:
                            row["creative"] = {key: row["creative"][key] for key in
                                ("definition_ref", "type_id", "hidden") if key in row["creative"]}
                        elif isinstance(creative.get("process"), dict):
                            # First look must expose a saved runtime failure's
                            # real bounded location, not hide it behind inspect.
                            row["creative"]["process"] = _companion_creative_summary(creative)["process"]
                    else:
                        row["creative"] = _companion_creative_summary(creative)
                if isinstance(entity.get("presentation"), dict):
                    row["presentation"] = deepcopy(entity["presentation"])
                if isinstance(entity.get("id"), str):
                    row["read_request"] = {"tool": "grove_inspect", "arguments": {"entity_id": entity["id"]}}
                state = entity.get("state")
                if not isinstance(state, dict):
                    state = entity.get("discovery_state", {})
                row["discovery_state"] = {key: deepcopy(state[key]) for key in
                    ("kind", "resource_kind", "stock", "open", "recipe", "wetness", "burning", "broken",
                     "destroyed", "retired", "water_remaining", "water_source_capacity") if key in state}
                if (entity.get("owner") is None and "container" in entity.get("capabilities", [])
                        and not state.get("open")):
                    row["contents_visibility"] = "hidden"
                    row["discovery_state"].pop("wetness", None)
                rows.append(row)
            index[field] = rows
    if "public_content" in scene:
        public = scene["public_content"]
        if preflight and isinstance(public, dict):
            index["public_content"] = {key: deepcopy(public[key]) for key in
                ("id", "region", "version", "contribution_count", "outcome_count") if key in public}
            if isinstance(public.get("id"), str):
                index["public_content"]["read_request"] = {
                    "tool": "grove_content_history", "arguments": {"content_id": public["id"]}}
        else:
            index["public_content"] = _companion_public_content_index(public)
    if preflight:
        index["public_works_read_request"] = {"tool": "grove_public_works", "arguments": {},
            "meaning": "Find public creations and current coordinates by author_name or name; not online status or remote action permission."}
        index["projection"] = {
            "scope": "authorized_preflight_discovery", "complete_scene": False,
            "omitted_sections": ["full object state/actions/previews/lots", "construction recipes",
                "mechanical history", "regional anchors and history", "process details"],
            "meaning": "Actual identities, versions, locations, current fields and authorized material quantities. "
                "Omitted/null facts are unknown, not absent or zero; matter rows have no transferable lot IDs. "
                "wetness is saturation (%), not water quantity (u). Inspect for current details; map for recipes/discovery. "
                "created_by_name is the current same-world public name; null is unknown and names may coincide. "
                "created_by remains the author ID, not an operator or owner. "
                "History is available on demand; this index grants no action permission."}
        return index
    index["projection"] = {
        "scope": "authorized_scene_index",
        "meaning": "Actual identity/version/location index, not complete state or an action preview. "
                   "Matter rows are actual authorized slot/quantity summaries, not transferable lot IDs; "
                   "canonical material identity is distinct from its display name and instance creator. "
                   "created_by_name is the current same-world public name; null is unknown and names may coincide. "
                   "created_by remains the author ID, not an operator or owner. "
                   "discovery_state.wetness is absorption saturation percent (%), not water quantity (u). "
                   "Null or omitted wetness is unknown, never a zero reading. "
                   "Creative runtime summaries are read snapshots: saved mechanical output is history, "
                   "solver eligibility is not participation, and process.steps counts reactions only. "
                   "last_solved marks output changes, not every solve; a request's earliest step is not confirmation. "
                   "Omitted conditions, contents and history are not absent. Read grove_inspect(entity_id) "
                   "for current physics/actions; grove_content_history(content_id) for history; "
                   "map_viewport for the current scene. Do not infer quantities or success from this index."}
    return index


def _companion_selected_state_summary(inspection, *, compact=False):
    """Automatic write readback reports state; explicit inspect owns previews."""
    entity = inspection.get("entity") if isinstance(inspection, dict) else None
    if not isinstance(entity, dict):
        return {"projection": {"scope": "selected_state_unexpanded",
            "meaning": "The selected object's state and action previews were not supplied; read grove_inspect."}}
    # Copy actual authorized values, including physical lots and program state.
    # A new object must not inherit inspection-local references to omitted actions.
    state = {key: deepcopy(entity[key]) for key in (
        "id", "version", "interaction_revision", "name", "x", "y", "location", "owner", "physical_parent",
        "material", "materials", "matter", "matter_unit", "material_quantity", "simulation", "components",
        "state", "mechanical", "creative", "presentation", "capabilities", "container", "lifecycle",
        "barrier_active", "contents_visibility", "content_summary", "relations", "created_by", "created_by_name", "source",
        "pour_target_slot", "execution_domain", "fabrication_materials", "material_eligibility",
        "acquisition", "acquisition_sources", "source_discovery_projection")
        if key in entity}
    state = _companion_current_entity(state)
    if compact:
        creative = state.get("creative")
        if isinstance(creative, dict):
            state["creative"] = _companion_creative_summary(creative)
            if "contract" in creative:
                state["creative"]["contract"] = deepcopy(creative["contract"])
            # Current yes/no/unknown conditions and material gaps explain the
            # actual result. Only the immutable expression can be deferred.
            for field in ("automatic_rules", "automatic_rules_truncated", "automatic_rules_meaning",
                          "automatic_rules_expression_projection",
                          "reaction_inputs", "reaction_observations", "reaction_observations_truncated",
                          "port_sources"):
                if field in creative:
                    state["creative"][field] = deepcopy(creative[field])
            _companion_condition_expressions(state["creative"])
        program = state.get("state", {}).get("program")
        if isinstance(program, dict):
            state["state"]["program"] = {key: deepcopy(program[key]) for key in
                ("fields", "mechanical", "drive_request", "mechanical_last_solved", "contract",
                 "port_bindings", "connections") if key in program}
    summary = {"entity": state, "projection": {
        "scope": "selected_object_state",
        "omitted_sections": ["action_options", "action previews", "inspection contents and public history"],
        "meaning": "Actual authorized object state at this readback, not a complete inspection or action preview. "
                   "Read grove_inspect for this entity before choosing an action that needs current inputs or previews."}}
    if isinstance(state.get("id"), str):
        summary["read_request"] = {"tool": "grove_inspect", "arguments": {"entity_id": state["id"]}}
    return summary


def _companion_repair_viewport_references(payload):
    """Keep only references to the retained readback's same named index fields."""
    value = payload.get("result", payload)
    viewport = value.get("viewport") if isinstance(value, dict) else None
    readback = payload.get("execution_readback")
    references = viewport.get("field_references") if isinstance(viewport, dict) else None
    if not isinstance(references, dict):
        return
    missing = []
    for field, target in list(references.items()):
        if (field == "selected_inspection" or not isinstance(readback, dict) or field not in readback or
                target != "#/execution_readback/" + field):
            references.pop(field)
            if field not in viewport:
                missing.append(field)
    if not references:
        viewport.pop("field_references", None)
    if missing:
        viewport["unexpanded_fields"] = missing
        viewport["unexpanded_read_request"] = {"tool": "map_viewport", "arguments": {}}


def _companion_execution_progress(owner):
    """Index historical progress evidence, without changing its saved ledger.

    Original outcomes already belong to the dedicated session's tool history.
    Repeating them in every new preflight inflates old state into current facts.
    This only touches the connector-owned task node, never authored world data.
    """
    if not isinstance(owner, dict):
        return
    task = owner.get("execution_task")
    progress = task.get("progress") if isinstance(task, dict) else None
    if not isinstance(progress, dict) or not isinstance(progress.get("evidence"), list) or not progress["evidence"]:
        return
    if (progress.get("evidence_projection") or {}).get("scope") == "historical_operation_index":
        return
    rows = []
    omitted_outcomes = 0
    for original in progress["evidence"]:
        if not isinstance(original, dict):
            rows.append(deepcopy(original))
            continue
        row = {key: deepcopy(original[key]) for key in (
            "function", "operation_id", "status", "commit_seq", "task_id", "segment_id",
            "action", "action_id", "action_entity_ids", "error", "execution_outcome") if key in original}
        omitted_outcomes += int("confirmed_outcome" in original)
        operation = row.get("operation_id")
        if not isinstance(operation, str) or not 1 <= len(operation) <= 180:
            row["history_filter_unavailable"] = "Original operation_id does not satisfy the history tool schema."
        rows.append(row)
    progress["evidence"] = rows
    progress["evidence_projection"] = {"scope": "historical_operation_index",
        "evidence_count": len(rows), "omitted_confirmed_outcome_count": omitted_outcomes,
        "read_tool": "native_tool_history", "mode": "list", "filter_field": "operation_id",
        "meaning": "Historical operation index, not current state or permission. Copy an eligible row's "
            "operation_id into this filter, then follow entry.read/next_request. Missing native output "
            "is unknown. task_action_summary retains totals and coverage; original evidence remains saved."}


def companion_projection(function: str, payload: dict) -> dict:
    """Own one mutable copy of an authorized response, then compact its facts."""
    result = deepcopy(payload)
    for owner in (result, result.get("result"), result.get("execution_readback")):
        _companion_execution_progress(owner)
    if function == "execution_checkpoint":
        facts = result.get("execution_readback")
        if isinstance(facts, dict):
            current = _companion_scene_index(facts)
            if isinstance(facts.get("selected_inspection"), dict):
                selected = _companion_inspection_index(facts["selected_inspection"])
                current["selected_inspection"] = _companion_selected_state_summary(selected or facts["selected_inspection"])
            result["execution_readback"] = current
        return result
    if function in {"grove.definition_list", "grove.fabrication_options"}:
        # The list's page/cursor contract and selected recipe's complete source
        # lots are already bounded world reads, not expandable scene indexes.
        return result
    if function not in {"map.viewport", "grove.generation_context", "grove.inspect", "grove.handbook", "grove.rule_catalog"} | AGENT_WRITE_FUNCTIONS:
        return result
    value = result.get("result", result)
    if not isinstance(value, dict):
        return result
    if (function == "map.viewport" and
            (value.get("discovery_projection") or {}).get("scope") == "authorized_preflight_discovery"):
        # The connector already projected this fresh preflight. Do not expand
        # its regional reference or reduce its selected authorized inspection.
        return result
    if function == "grove.rule_catalog":
        value["definitions_projection"] = "Page actual immutable definitions with grove_definition_list, " \
            "then read the chosen grove_definition_read(definition_ref). This static language catalog is not an empty publication list."
        return result
    if function == "map.viewport":
        scene = _companion_scene_index(value)
        public = scene.get("public_content")
        if isinstance(public, dict):
            scene["public_content"] = {key: deepcopy(public[key]) for key in
                ("id", "region", "version", "contribution_count", "outcome_count") if key in public}
            if isinstance(public.get("id"), str):
                scene["public_content"]["read_request"] = {"tool": "grove_content_history",
                    "arguments": {"content_id": public["id"]}}
        if isinstance(value.get("selected_inspection"), dict):
            scene["selected_inspection"] = companion_projection("grove.inspect", value["selected_inspection"])
        if value is result:
            return scene
        result["result"] = scene
        return result
    if function in {"map.viewport", "grove.inspect"} | AGENT_WRITE_FUNCTIONS and "public_content" in value:
        value["public_content"] = _companion_public_content_index(value["public_content"])
    if function == "grove.handbook":
        # Entry needs the real choices and consequences, not repeated full
        # schemas or the expanded creation catalog. Retain the source's rules
        # verbatim and label connector control separately from world authority.
        overview = {key: value[key] for key in (
            "world", "title", "player", "clock", "rules_version", "execution_model", "process_order", "process_limits",
            "terrain", "materials", "material_slots", "material_roles", "combination_preview",
            "capabilities", "rules", "entry") if key in value}
        compositions = value.get("compositions")
        if isinstance(compositions, dict):
            overview["compositions"] = {key: compositions[key] for key in ("quantity_unit", "rules_version")
                                        if key in compositions}
            constructions = compositions.get("constructions")
            if isinstance(constructions, list):
                overview["compositions"]["constructions"] = [
                    {key: item[key] for key in ("id", "name", "materials", "lining", "sizes", "description")
                     if key in item} for item in constructions if isinstance(item, dict)]
        construction = value.get("construction")
        if isinstance(construction, dict):
            overview["construction"] = {key: construction[key] for key in ("radius_metric", "limits")
                                        if key in construction}
            recipes = construction.get("recipes")
            if isinstance(recipes, list):
                overview["construction"]["recipes"] = [
                    {key: item[key] for key in ("id", "name", "cost", "material_cost_u", "unit", "terrain",
                                               "radius", "use", "material", "capabilities") if key in item}
                    for item in recipes if isinstance(item, dict)]
        creation = value.get("creation")
        if isinstance(creation, dict):
            overview["creation"] = {key: creation[key] for key in ("context", "publish", "release", "history",
                                                                   "spatial_format") if key in creation}
            catalog = creation.get("catalog")
            if isinstance(catalog, dict):
                overview["creation"]["semantics"] = {key: catalog[key] for key in
                    ("authored_description", "resource_cost") if key in catalog}
        overview["handbook_projection"] = {
            "scope": "entry_overview",
            "source": "Authenticated grove.handbook; retained facts and rules are unchanged.",
            "source_json_chars": len(json.dumps(payload, ensure_ascii=False)),
            "omitted_sections": ["compositions.materials (same materials retained above)",
                "compositions.constructions variant components, scaling and lining internals",
                "construction recipe components, build_schema and interact_schema",
                "creation schema, continuation_schema, catalog expansions, publishing_steps and examples",
                "creation.programs full rule catalog, definitions, expressions and action/effect shapes"],
            "meaning": "Intentional entry summary, not a complete world capability or action catalog. "
                       "Omitted details are not absent or unsupported. Read this overview once for entry; "
                       "repeating grove_handbook returns the same projection, not further manual pages.",
            "read_creation_rules": {
                "program_language": "grove_rule_catalog: exact bounded declarations, physical semantics, "
                                    "automatic rules versus explicit actions, and publication path. "
                                    "grove_definition_list pages actual immutable published references; "
                                    "grove_definition_read reads a selected reference. "
                                    "grove_fabrication_options reads a selected type's actual cost_u and eligible source lots.",
                "function_catalog": "Declared Grove MCP tools' inputSchema is the live function catalog "
                                    "for exact input envelopes; the server catalog is /v1/functions. "
                                    "Do not infer parameters or powers from this overview.",
                "current_admission": "Only during an explicit granted goal, grove_generation_context "
                                     "returns actual eligible placements, resource stock, composition/template "
                                     "choices and budgets. Current grove_inspect supplies action eligibility "
                                     "and physical previews; overview recipes do not prove current availability."},
            "connector_control": "Entry and ordinary dialogue permit reads and explanations only. "
                                 "This connector's separate active explicit execution goal permits the same "
                                 "role's declared actions within the person's grant; ordinary dialogue does "
                                 "not grant it. Reclaiming control stops future writes; committed consequences "
                                 "remain. This is connector scope, not a rewritten world rule."}
        if value is result:
            return overview
        result["result"] = overview
        return result
    if function == "grove.inspect":
        _companion_inspection_context(value)
        entity = value.get("entity")
        if isinstance(entity, dict):
            entity = _companion_current_entity(entity)
            value["entity"] = entity
        if isinstance(entity, dict):
            for field in ("action_options", "available_actions"):
                if field in entity and field in value and entity[field] == value[field]:
                    # Paths are relative to the inspection result, whether it
                    # is a tool result or this turn's selected_inspection.
                    references = entity.setdefault("field_references", {})
                    if isinstance(references, dict):
                        references[field] = "#/" + field
                        entity.pop(field)
        projection = value.get("action_options_projection") or {}
        if (projection.get("scope") not in {"selected_declared_action", "authorized_action_index"}
                and not (projection.get("candidate_selection") or {}).get("candidate_id")):
            # A default look supplies complete authorized object facts, not
            # every possible action's parameters and combination calculation.
            # Only an explicitly selected action/candidate supplies a preview.
            index = _companion_inspection_index(result, preserve_facts=True)
            if index is not None:
                return index
        return result
    def compact_entity(entity):
        if not isinstance(entity, dict):
            return entity
        # Keep matter, simulation, state, identity and public program bindings.
        # A scene outline is not the versioned input/preview needed to act.
        options = entity.get("action_options")
        if isinstance(options, list):
            entity["action_options"] = [{key: option[key] for key in
                ("action", "label", "function_id", "available", "reason") if key in option}
                if isinstance(option, dict) else option for option in options]
            entity["action_options_projection"] = (
                "Action outline only. Read grove_inspect for this actual entity ID to obtain current "
                "arguments, choices, input schemas and full physical previews before acting.")
        state = entity.get("state")
        if isinstance(state, dict) and entity.get("contents") == state.get("contents"):
            entity.pop("contents", None)
        if entity.get("location") == {"x": entity.get("x"), "y": entity.get("y")}:
            entity.pop("location", None)
        relations = entity.get("relations")
        if isinstance(relations, dict) and relations.get("parent") == entity.get("physical_parent"):
            relations.pop("parent", None)
        return entity
    def compact_scene(scene):
        if not isinstance(scene, dict):
            return scene
        if isinstance(scene.get("chunks"), list):
            scene["chunks"] = [_companion_chunk_terrain(chunk) for chunk in scene["chunks"]]
        for field in ("entities", "inventory"):
            if isinstance(scene.get(field), list):
                scene[field] = [compact_entity(entity) for entity in scene[field]]
        return scene
    def compact_execution_readback(*, index=False):
        readback = result.get("execution_readback")
        if isinstance(readback, dict):
            readback = compact_scene(readback)
            if index:
                if "selected_inspection" in readback:
                    readback["selected_inspection"] = _companion_selected_state_summary(readback["selected_inspection"])
                readback = _companion_scene_index(readback)
            result["execution_readback"] = readback
            # The receipt still describes the committed operation. Later fresh
            # readback is the one current scene; equal nested fields need no copy.
            viewport = value.get("viewport")
            if isinstance(viewport, dict):
                references = {}
                for key in list(viewport):
                    if key != "selected_inspection" and key in readback and viewport[key] == readback[key]:
                        references[key] = "#/execution_readback/" + key
                        viewport.pop(key)
                if references:
                    viewport["field_references"] = references
                    viewport["projection_scope"] = (
                        "Committed operation viewport projection; identical returned fields reference the later "
                        "execution_readback projection. Differing operation-time facts remain here. "
                        "Referenced scene indexes omit full state and action previews.")
            public = value.get("public_facts")
            if isinstance(public, dict) and isinstance(public.get("entities"), list):
                current = readback.get("entities", [])
                for index, entity in enumerate(public["entities"]):
                    match = next((current_index for current_index, actual in enumerate(current)
                                  if actual == entity), None)
                    if match is not None and isinstance(entity, dict):
                        public["entities"][index] = {"id": entity.get("id"), "version": entity.get("version"),
                            "entity_ref": "#/execution_readback/entities/" + str(match)}
                public["readback_projection"] = "Only exactly equal returned records reference execution_readback; " \
                    "different state/version remains in this operation's public_facts. References are relative to the full tool response. " \
                    "Referenced indexes do not contain full entity state."
            _companion_repair_viewport_references(result)
    if function in AGENT_WRITE_FUNCTIONS and function != "grove.generation_context":
        viewport = value.get("viewport")
        if isinstance(viewport, dict):
            value["viewport"] = _companion_scene_index(compact_scene(viewport))
        if isinstance(value.get("entity"), dict):
            value["entity"] = _companion_selected_state_summary(
                {"entity": compact_entity(value["entity"])}, compact=True)["entity"]
        if isinstance(value.get("entities"), list):
            value["entities"] = [_companion_selected_state_summary(
                {"entity": compact_entity(entity)}, compact=True)["entity"]
                if isinstance(entity, dict) else entity for entity in value["entities"]]
        compact_execution_readback(index=True)
        return result
    compact_scene(value)
    facts = value.get("public_facts")
    if isinstance(facts, dict):
        compact_scene(facts)
        nearby = facts.get("nearby_entities")
        entities = facts.get("entities")
        if isinstance(nearby, list) and isinstance(entities, list):
            # Region and nearby radius are different scopes. Keep their actual
            # membership, sharing a snapshot only on complete equality.
            region_count = len(entities)
            references = []
            for entity in nearby:
                compact_entity(entity)
                if not isinstance(entity, dict):
                    references.append(entity)
                    continue
                index = next((index for index, existing in enumerate(entities) if existing == entity), None)
                if index is None:
                    index = len(entities)
                    entities.append(entity)
                references.append({"id": entity.get("id"), "version": entity.get("version"),
                                   "entity_ref": ("#/public_facts/entities/" if value is result else
                                                  "#/result/public_facts/entities/") + str(index)})
            facts["nearby_entities"] = references
            facts["region_entity_count"] = region_count
            facts["entity_projection"] = (
                "entities is the union of actual region and nearby snapshots. Its first region_entity_count "
                "entries are the original region scope; nearby_entities references the actual radius-8 scope. "
                "Same ID with different state/version remains separate. Unknown/missing terrain cells and "
                "out-of-scope objects are not empty or absent. References are relative to the full tool response.")
        if facts.get("public_content") == value.get("public_content"):
            facts.pop("public_content", None)
    if function == "grove.generation_context" and isinstance(value.get("catalog"), dict):
        # Use the existing exact alias/difference fold before selecting a small
        # real catalog sample; definitions and schema authority stay independent.
        catalog = blueprint_model_input({"catalog": value["catalog"]})["catalog"]
        compositions = catalog.get("compositions")
        if isinstance(compositions, list):
            selected, omitted, kinds = [], [], set()
            for item in compositions:
                spec = item.get("composition", {}) if isinstance(item, dict) else {}
                kind = spec.get("construction")
                if kind not in kinds and len(selected) < 4:
                    selected.append(item)
                    kinds.add(kind)
                else:
                    omitted.append(spec)
            catalog["compositions"] = selected
            selected_specs = [item.get("composition") for item in selected if isinstance(item, dict)]
            shortcuts = catalog.get("objects")
            omitted_templates = []
            if isinstance(shortcuts, list):
                omitted_templates = [{"template": item.get("template"), "composition": item.get("composition")}
                    for item in shortcuts if isinstance(item, dict) and item.get("composition") not in selected_specs]
                catalog["objects"] = [item for item in shortcuts if isinstance(item, dict)
                                      and item.get("composition") in selected_specs]
            registry = catalog.get("composition_registry")
            if isinstance(registry, dict) and isinstance(registry.get("constructions"), list):
                registry["constructions"] = [{key: item[key] for key in
                    ("id", "name", "materials", "lining", "sizes", "description") if key in item}
                    if isinstance(item, dict) else item for item in registry["constructions"]]
            catalog["projection"] = {"scope": "finite_actual_composition_sample",
                "compositions_total": len(compositions), "omitted_compositions": omitted,
                "unexpanded_templates": omitted_templates,
                "omitted_sections": ["composition_registry variants/scales/state/lining internals"],
                "meaning": "Retained compositions are complete actual definitions; shortcuts alias them "
                           "with actual definition_overrides. This is a finite sample, not the complete "
                           "allowed schema or world capability list. Unexpanded choices remain declared; "
                           "do not infer their physics from their names. Published programs use their "
                           "immutable definition_ref/type_id and do not depend on this preset sample."}
        value["catalog"] = catalog
        from .worlds.shared_grove.catalog import OBJECT_SCHEMA, DEFINITION_CHOICE
        branch = DEFINITION_CHOICE[2]
        value["custom_program_instantiation"] = {
            "function": "grove.content_submit", "path": "content.objects[]", "kind": "layout",
            "object_schema": {"type": OBJECT_SCHEMA["type"], "additionalProperties": False,
                "required": [*OBJECT_SCHEMA["required"], *branch["required"]],
                "properties": {key: deepcopy(OBJECT_SCHEMA["properties"][key]) for key in
                               ("definition_ref", "type_id", "x", "y", "name", "description")}},
            "scope": "Exact definition-reference branch's core fields, from the current content schema. "
                     "This regional layout branch applies only to cost types; cost_u types use "
                     "grove_fabrication_options then grove_fabricate with actual own material lots. "
                     "Full optional contents/water and envelope rules are in grove_content_submit inputSchema. "
                     "Use only an actually published reference and type, actual allowed_tiles and this "
                     "owned opportunity/version. Actual costs, material budgets and resource stock still apply."}
    tiles = value.get("allowed_tiles")
    if isinstance(tiles, list):
        player = value.get("player", {})
        px, py = player.get("x", 0), player.get("y", 0)
        ordered = sorted(tiles, key=lambda p: (abs(p["x"] - px) + abs(p["y"] - py), p["y"], p["x"]))
        value["allowed_tiles"] = ordered[:24]
        value["allowed_tiles_total"] = len(tiles)
        value["placement_projection"] = "Nearest 24 actual allowed tiles. Choose one returned coordinate; the server validates again on publication."
    value["chunk_size"] = 16
    if function in AGENT_WRITE_FUNCTIONS:
        compact_execution_readback()
    if function == "grove.generation_context":
        if "public_content" in value:
            public = value["public_content"]
            value["public_content"] = ({key: deepcopy(public[key]) for key in
                ("id", "region", "version", "history", "anchors_truncated", "anchor_index_limit") if key in public} |
                {"projection": "Regional content reference only. Fixed continuation anchors remain complete in "
                               "opportunity.continuation_basis; read map_viewport for discovery, "
                               "grove_inspect for selected entities and grove_content_history for history."}) if isinstance(public, dict) else public
        if "public_facts" in value:
            value["public_facts"] = {"projection": "The expanded regional/nearby scene is deferred. "
                "Read map_viewport and grove_inspect for it; omitted entities/state are not absent. "
                "Admission uses the resource_stock, returned actual allowed_tiles, budgets, preconditions "
                "and fixed opportunity in this response."}
        compatible = value.get("compatible_parts")
        sampled = [item.get("composition") for item in value.get("catalog", {}).get("compositions", [])]
        if isinstance(compatible, list):
            value["compatible_parts"] = [{**item, "compositions": [spec for spec in item.get("compositions", []) if spec in sampled]}
                for item in compatible if any(spec in sampled for spec in item.get("compositions", []))]
            value["compatible_parts_projection"] = "Actual compatible choices for the explicitly sampled catalog only. " \
                "Other choices are unexpanded, not incompatible. Published programs use their immutable definition_ref/type_id."
        opportunity = value.get("opportunity", {})
        if "continuation" in value and value["continuation"] == opportunity.get("continuation_basis"):
            value["continuation"] = {"field_reference": "#/result/opportunity/continuation_basis" if value is not result
                                      else "#/opportunity/continuation_basis"}
        # Complete admission facts precede optional catalogs and discovery.
        priority = ("player", "opportunity", "claimed", "availability", "preconditions", "resource_stock",
                    "budget_remaining", "submission_limits", "allowed_kinds", "allowed_tiles", "allowed_tiles_total",
                    "lease_seconds", "continuation", "custom_program_instantiation", "compatible_parts")
        ordered = {key: value[key] for key in priority if key in value}
        ordered.update({key: val for key, val in value.items() if key not in ordered})
        if value is result:
            result = ordered
        else:
            result["result"] = ordered
        if "execution_readback" in result:
            readback = result["execution_readback"]
            result["execution_readback"] = _companion_scene_index({key: readback[key]
                for key in ("player", "clock") if key in readback})
            result["execution_readback"]["projection"]["scope"] = "post_claim_actor_and_clock"
    return result


def _companion_inventory_discovery(scene, rows, *, excluded=(), limit=8):
    """A returned-page index without action versions, transferable lots or state."""
    excluded = set(excluded)
    visible = [row for row in rows if isinstance(row, dict) and isinstance(row.get("id"), str)
               and row["id"] not in excluded]
    entries = []
    for row in visible[:limit]:
        entry = {key: deepcopy(row[key]) for key in ("id", "name", "material", "capabilities",
            "created_by", "created_by_name", "source", "container", "physical_parent") if key in row}
        state = row.get("discovery_state")
        kind = row.get("entity_kind", state.get("kind") if isinstance(state, dict) else None)
        if isinstance(kind, str):
            entry["entity_kind"] = kind
        entry["read_request"] = {"tool": "grove_inspect", "arguments": {"entity_id": row["id"]}}
        entries.append(entry)
    scene["inventory_discovery"] = entries
    scene["inventory_discovery_projection"] = {
        "scope": "returned_inventory_page_index", "returned_page_count": len(rows),
        "retained_count": len(entries), "omitted_index_count": max(0, len(visible) - len(entries)),
        "separately_observed_count": sum(isinstance(row, dict) and row.get("id") in excluded for row in rows),
        "complete_inventory": False,
        "read_request": {"tool": "map_viewport", "arguments": {}},
        "meaning": "Discovery from the authorized returned inventory page, not a complete inventory "
                   "or action basis. Versions, lots, quantities and state are unexpanded. Current related "
                   "objects are read separately; omitted objects are not absent. Read the map's first "
                   "inventory page, then follow that response's inventory_next; this index has no paging cursor."}


def _companion_action_projection(result, *, base_id=None, versions=None, entity_ids=(), source=None):
    """Reduce this call's owned projection; only retained rows reach binding."""
    readback = result.get("execution_readback")
    if not isinstance(readback, dict):
        return result
    value = result.get("result", result)
    if not isinstance(value, dict):
        return result
    known = versions.get("entity", {}) if isinstance(versions, dict) else {}
    relevant = {identity for identity in entity_ids if isinstance(identity, str)}
    direct = value.get("entities", [])
    direct = direct if isinstance(direct, list) else []
    for entity in [value.get("entity"), *direct]:
        if isinstance(entity, dict) and isinstance(entity.get("id"), str):
            relevant.add(entity["id"])
    selected = readback.get("selected_inspection", {})
    if isinstance(selected, dict) and isinstance(selected.get("entity"), dict):
        relevant.add(selected["entity"].get("id"))

    compact = {key: readback[key] for key in (
        "player", "clock", "bounds", "inventory_total", "truncated",
        "observed_at", "selected_inspection", "selected_inspection_unavailable", "world_results",
        "action_targets", "action_targets_meaning", "task_action_summary", "public_events") if key in readback}
    construction = readback.get("construction")
    if isinstance(construction, dict):
        compact["construction"] = {key: construction[key] for key in ("material_totals", "unit")
                                   if key in construction}
    # Later selected/target inspections own those objects' current readback.
    # An earlier map's inventory or scene must not duplicate or override them,
    # including a later explicit unavailable result.
    separately_observed = {target["entity_id"] for target in compact.get("action_targets", [])
        if isinstance(target, dict) and isinstance(target.get("entity_id"), str)}
    selected = compact.get("selected_inspection")
    for selected in (selected.get("entity") if isinstance(selected, dict) else None,
                     compact.get("selected_inspection_unavailable")):
        if isinstance(selected, dict):
            identity = selected.get("id", selected.get("entity_id"))
            if isinstance(identity, str):
                separately_observed.add(identity)
    inventory = readback.get("inventory")
    if isinstance(inventory, list):
        compact["inventory"] = [row for row in inventory if isinstance(row, dict)
            and row.get("id") in relevant and row.get("id") not in separately_observed]
        _companion_inventory_discovery(compact, inventory,
            excluded=separately_observed | {row["id"] for row in compact["inventory"]})
    rows = readback.get("entities", [])
    rows = rows if isinstance(rows, list) else []
    changed = [row for row in rows if isinstance(row, dict) and
               (row.get("id") not in known or known[row["id"]] != row.get("version"))]
    discovery = [row for row in changed if row.get("id") not in relevant]
    retained = [row for row in rows if isinstance(row, dict) and row.get("id") in relevant
                and row.get("id") not in separately_observed]
    compact["entities"] = retained
    for field in ("players", "nearby_players"):
        if isinstance(readback.get(field), list):
            compact[field] = [row for row in readback[field]
                              if isinstance(row, dict) and row.get("id") in relevant]

    # Current nearby history is useful context, not proof of this action's cause.
    raw = source.get("execution_readback", {}) if isinstance(source, dict) else {}
    changes = raw.get("changes", []) if isinstance(raw, dict) else []
    changes = changes if isinstance(changes, list) else []
    if changes:
        compact["changes"] = deepcopy(changes[-6:])
    compact["projection"] = {"scope": "authorized_scene_index", "mode": "action_readback_delta",
        "base_observation_id": base_id, "complete_scene": False,
        "scene_entity_count": len(rows), "changed_or_new_entity_count": len(changed),
        "retained_entity_count": len(retained),
        "omitted_changed_or_new_entity_count": len(discovery),
        "omitted_recent_change_count": max(0, len(changes) - 6),
        "omitted_sections": ["chunk terrain", "unrelated scene and inventory state",
                             "other players", "public content/history", "construction recipes"],
        "meaning": "Current actor/material totals and related objects; later selected/target inspections "
                   "supersede their earlier map rows. Inventory discovery is unexpanded. This is not a "
                   "complete scene or action preview; omission means neither absence nor unchanged state. "
                   "Recent changes are nearby history, not this operation's attribution. Use map_viewport "
                   "for discovery and grove_inspect for exact inputs; chain base_observation_id to this response."}
    result["execution_readback"] = compact

    viewport = value.get("viewport")
    if isinstance(viewport, dict):
        metadata = {}
        for field in ("bounds", "clock", "truncated"):
            if field in viewport:
                metadata[field] = viewport[field]
            elif viewport.get("field_references", {}).get(field) == "#/execution_readback/" + field and field in readback:
                metadata[field] = readback[field]
        value["viewport"] = metadata | {"projection": {"scope": "operation_viewport_deferred",
            "meaning": "This operation's direct result and changes remain above. Its expanded viewport "
                       "is deferred; execution_readback is a later compact read, not this operation's snapshot."}}
    # Existing exact-equality references must not point at a shifted delta row.
    public = value.get("public_facts")
    if isinstance(public, dict) and isinstance(public.get("entities"), list):
        for index, row in enumerate(public["entities"]):
            reference = row.get("entity_ref") if isinstance(row, dict) else None
            if isinstance(reference, str) and reference.startswith("#/execution_readback/entities/"):
                suffix = reference.rsplit("/", 1)[-1]
                if suffix.isdigit() and int(suffix) < len(rows):
                    public["entities"][index] = rows[int(suffix)]
        public.pop("readback_projection", None)
    # A timer or another actor can change an object between commit and readback.
    # Preserve both facts, but do not present committed lots as current inputs.
    current = {}
    for field in ("entities", "inventory"):
        entries = compact.get(field, [])
        entries = entries if isinstance(entries, list) else []
        for index, row in enumerate(entries):
            if isinstance(row, dict) and isinstance(row.get("id"), str):
                current[row["id"]] = (row, f"#/execution_readback/{field}/{index}")
    selected = compact.get("selected_inspection", {})
    if isinstance(selected, dict) and isinstance(selected.get("entity"), dict):
        row = selected["entity"]
        if isinstance(row.get("id"), str):
            current[row["id"]] = (row, "#/execution_readback/selected_inspection/entity")
    for index, target in enumerate(compact.get("action_targets", [])):
        if not isinstance(target, dict):
            continue
        row = target.get("inspection", {}).get("entity")
        if isinstance(row, dict) and row.get("id") == target.get("entity_id"):
            current[row["id"]] = (row, f"#/execution_readback/action_targets/{index}/inspection/entity")
    reads = {}
    for entity in [value.get("entity"), *direct]:
        if (not isinstance(entity, dict) or not isinstance(entity.get("id"), str) or
                entity["id"] not in current):
            continue
        row, reference = current[entity["id"]]
        old_version, current_version = entity.get("version"), row.get("version")
        if (type(old_version) is not int or type(current_version) is not int or
                old_version <= 0 or current_version <= 0 or old_version == current_version):
            continue
        request = {"tool": "grove_inspect", "arguments": {"entity_id": entity["id"]}}
        entity["snapshot_projection"] = {
            "scope": "committed_operation_snapshot",
            "superseded_by_readback": {"version": current_version, "entity_ref": reference},
            "read_request": request,
            "meaning": "These quantities, lots and action inputs describe the original commit, not "
                       "current availability. The later readback supersedes current-state assumptions; "
                       "its summaries are not transferable lot choices. Inspect this object again before "
                       "another material action and use the returned observation_id."}
        reads[entity["id"]] = request
    result["read_requests"] = [*reads.values(), {"tool": "map_viewport", "arguments": {}}]
    if reads:
        result["action_snapshot_projection"] = {
            "scope": "committed_result_with_later_readback",
            "changed_entity_ids": list(reads),
            "meaning": "result retains the action's committed state; execution_readback is a later "
                       "authorized read. Listed objects changed between them. Do not reuse old lots "
                       "or action candidates; use these read_requests for exact current inputs, "
                       "referencing this response's observation_id as base_observation_id."}
    return result


def _agent_tool_text(payload):
    """Lossless JSON without deep indentation or oversized single lines."""
    def render(value, depth=0):
        compact = json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
        if not isinstance(value, (dict, list)) or not value or depth >= 2 and len(compact.encode()) <= 4096:
            return compact
        if isinstance(value, dict):
            return "{\n" + ",\n".join(json.dumps(key, ensure_ascii=False) + ":" + render(item, depth+1)
                                      for key, item in value.items()) + "\n}"
        return "[\n" + ",\n".join(render(item, depth+1) for item in value) + "\n]"
    return render(payload)


def _agent_tool_text_fits(text, *, native=False):
    byte_limit = _NATIVE_TOOL_MAX_BYTES if native else _AGENT_TOOL_MAX_BYTES
    line_limit = _NATIVE_TOOL_MAX_LINES if native else _AGENT_TOOL_MAX_LINES
    return len(text.encode("utf-8")) <= byte_limit and text.count("\n") + 1 <= line_limit


def _companion_nearby_read_entries(scene):
    """Preserve a few current discovery paths when selected facts fill the budget.

    These are not object snapshots: no versions, lots, current quantities or
    action previews are copied. Only already-authorized nearby rows contribute
    identities, locations and authors; a published definition is not a place.
    """
    if (scene.get("visible_player_names_projection") or {}).get("scope") != "current_visible_player_names":
        source = scene.get("players")
        if isinstance(source, list):
            players, seen = [], set()
            for row in source:
                if not isinstance(row, dict) or not isinstance(row.get("id"), str) or row["id"] in seen:
                    continue
                seen.add(row["id"])
                if isinstance(row.get("name"), str):
                    players.append({"id": row["id"], "name": row["name"]})
            if seen:
                scene["visible_player_names"] = players[:4]
                scene["visible_player_names_projection"] = {
                    "scope": "current_visible_player_names", "source": "players",
                    "authorized_player_count": len(seen), "retained_count": min(len(players), 4),
                    "omitted_count": len(seen) - min(len(players), 4), "hard_limit_omitted_count": 0,
                    "meaning": "Exact names and IDs from this current authorized visible player list, in source order. "
                        "Missing names are unknown. No locations, versions or action permission are supplied; "
                        "this is not a global player lookup. An object's created_by names its author, not its operator."}
    if (scene.get("nearby_discovery_projection") or {}).get("scope") == "current_nearby_read_entries":
        return
    source = scene.get("entities")
    if not isinstance(source, list):
        return
    entities, seen = [], set()
    for row in source:
        if not isinstance(row, dict) or not isinstance(row.get("id"), str) or row["id"] in seen:
            continue
        seen.add(row["id"])
        entities.append(row)
    if not entities:
        return
    def public_created(row):
        creative = row.get("creative")
        return ("owner" in row and row["owner"] is None and
            (row.get("created_by") is not None or
                isinstance(creative, dict) and isinstance(creative.get("definition_ref"), str)))
    ordered = sorted(entities, key=lambda row: not public_created(row))
    entries = []
    for row in ordered[:6]:
        entry = {key: deepcopy(row[key]) for key in (
            "id", "name", "created_by", "created_by_name", "x", "y", "location", "owner", "physical_parent", "entity_kind")
            if key in row}
        if entry.get("location") == {"x": entry.get("x"), "y": entry.get("y")}:
            entry.pop("location")
        creative = row.get("creative")
        if isinstance(creative, dict):
            entry["creative"] = {key: deepcopy(creative[key]) for key in
                ("definition_ref", "type_id", "hidden") if key in creative}
        entry["read_request"] = {"tool": "grove_inspect", "arguments": {"entity_id": row["id"]}}
        entries.append(entry)
    scene["nearby_discovery"] = entries
    scene["nearby_discovery_projection"] = {
        "scope": "current_nearby_read_entries", "source": "entities",
        "authorized_entity_count": len(entities), "retained_count": len(entries),
        "omitted_count": len(entities) - len(entries), "hard_limit_omitted_count": 0,
        "meaning": "Read entries copied from this current authorized nearby scene, with public created objects "
            "first and otherwise source order. created_by_name is the current same-world public name, never an operator; "
            "null/missing names are unknown and equal names do not merge author IDs. Omitted location/author/state is unknown. No object versions, "
            "lots or action previews are supplied here; inspect before acting. This is not global discovery."}


def _companion_fact_budget(function, payload):
    """Shed optional discovery before binding; exact action reads stay whole.

    This is a small everyday target, not a second permission or simulation
    layer. Mandatory receipts, current quantities, contracts and selected
    action previews may exceed it; the native hard boundary still applies.
    """
    if function not in {"map.viewport", "execution_checkpoint", "grove.inspect", "grove.definition_publish"} | AGENT_ACTION_FUNCTIONS:
        return payload
    value = payload.get("result", payload)
    if not isinstance(value, dict):
        return payload
    if function == "grove.inspect":
        selection = value.get("action_options_projection") or {}
        if selection.get("scope") == "selected_declared_action" or selection.get("candidate_selection"):
            return payload
        if len(_agent_tool_text(payload).encode()) <= _AGENT_FACT_TARGET_BYTES:
            return payload
        # Keep conditions and quantity facts; immutable declarations, duplicate
        # fields and child expansion remain accessible by the declared reads.
        indexed = _companion_inspection_index(payload, preserve_facts=True, compact_expressions=True)
        if indexed is not None:
            payload = indexed
            value = payload.get("result", payload)
    readback = payload.get("execution_readback")
    protected = set()
    if isinstance(readback, dict):
        for row in [value.get("entity"), *(value.get("entities") or [])]:
            if isinstance(row, dict) and isinstance(row.get("id"), str):
                protected.add(row["id"])
        selected = readback.get("selected_inspection", {}).get("entity", {})
        if isinstance(selected, dict) and isinstance(selected.get("id"), str):
            protected.add(selected["id"])
        protected.update(row["entity_id"] for row in readback.get("action_targets", [])
            if isinstance(row, dict) and isinstance(row.get("entity_id"), str))
    scenes = [value] if function == "map.viewport" else []
    # A public SDK receives the operation-time viewport directly, without a
    # host's later execution_readback. Apply the same optional discovery budget
    # to that actual scene before binding any versions from it.
    if function in AGENT_ACTION_FUNCTIONS and isinstance(value.get("viewport"), dict):
        scenes.append(value["viewport"])
    if isinstance(readback, dict):
        scenes.append(readback)
    # A later readback wins when a response also carries a commit-time map.
    # Keep one current floor: six nearby objects and four visible player names.
    discovery_scene = next((scene for scene in reversed(scenes)
        if (isinstance(scene.get("entities"), list) and scene["entities"] or
            isinstance(scene.get("players"), list) and scene["players"])), None)
    omissions = {}
    def too_large():
        return len(_agent_tool_text(payload).encode("utf-8")) > _AGENT_FACT_TARGET_BYTES - 768
    for scene in scenes:
        if isinstance(scene.get("selected_inspection"), dict):
            selected = scene["selected_inspection"]
            selected_value = selected.get("result", selected)
            selection = selected_value.get("action_options_projection") or {}
            if (selection.get("scope") != "selected_declared_action" and not selection.get("candidate_selection")
                    and len(_agent_tool_text(selected).encode()) > _AGENT_FACT_TARGET_BYTES):
                scene["selected_inspection"] = _companion_inspection_index(
                    selected, preserve_facts=True, compact_expressions=True) or selected
        if (scene is discovery_scene and too_large() and
                (scene.get("projection") or {}).get("mode") != "action_readback_delta"):
            # The soft target must not turn a populated nearby world into an
            # empty one merely because exact selected facts need more room.
            _companion_nearby_read_entries(scene)
        # Public history is not an operation verdict. Discovery remains a
        # readable spatial query; the person's original selection stays whole.
        for field in ("public_events", "entities", "players", "nearby_players", "inventory"):
            rows = scene.get(field)
            minimum = 1 if field == "inventory" else 0
            removed = 0
            while isinstance(rows, list) and len(rows) > minimum and too_large():
                if field in {"entities", "inventory", "players", "nearby_players"} and isinstance(rows[-1], dict) and rows[-1].get("id") in protected:
                    break  # Mandatory prefix and its references remain stable.
                rows.pop(0 if field == "public_events" else -1)
                removed += 1
            if not removed:
                continue
            omissions[field] = omissions.get(field, 0) + removed
            scene["truncated"] = True
            if field == "inventory":
                # Never skip a withheld row on the next real inventory page.
                scene["inventory_next"] = rows[-1].get("id") if rows else None
        public = scene.get("public_content")
        if too_large() and isinstance(public, dict) and isinstance(public.get("anchors"), list):
            omissions["public_content.anchors"] = len(public["anchors"])
            public.pop("anchors")
            public["read_request"] = {"tool": "grove_content_history", "arguments": {"content_id": public.get("id")}}
    if omissions or too_large():
        payload["fact_budget"] = {"target_bytes": _AGENT_FACT_TARGET_BYTES,
            "omitted_rows": omissions,
            "mandatory_facts_exceed_target": len(_agent_tool_text(payload).encode()) > _AGENT_FACT_TARGET_BYTES,
            "meaning": "Optional detailed discovery omitted before binding; omission is not absence. Exact receipt, "
                "current quantities, task totals, selected facts and complete selected action previews are retained. "
                "Bounded nearby read entries and current visible player names may exceed this soft target. Inspect known objects, or read a "
                "smaller nearby map and the actual inventory_next page."}
        payload["fact_budget"]["mandatory_facts_exceed_target"] = len(_agent_tool_text(payload).encode()) > _AGENT_FACT_TARGET_BYTES
    # Discovery has a soft floor, never priority over exact action facts at the
    # hard boundary. Counts describe actual withheld entries, not world absence.
    for scene in reversed(scenes):
        for field, scope, count_field in (
                ("nearby_discovery", "current_nearby_read_entries", "authorized_entity_count"),
                ("visible_player_names", "current_visible_player_names", "authorized_player_count")):
            entries = scene.get(field)
            projection = scene.get(field + "_projection") or {}
            if not isinstance(entries, list) or projection.get("scope") != scope:
                continue
            while entries and not _agent_tool_text_fits(_agent_tool_text(payload)):
                entries.pop()
                projection["retained_count"] = len(entries)
                projection["omitted_count"] = projection[count_field] - len(entries)
                projection["hard_limit_omitted_count"] += 1
    if isinstance(payload.get("fact_budget"), dict):
        payload["fact_budget"]["mandatory_facts_exceed_target"] = len(_agent_tool_text(payload).encode()) > _AGENT_FACT_TARGET_BYTES
    return payload


def _companion_condition_expressions(creative):
    """Keep current condition evidence; defer duplicated immutable expressions."""
    if (not isinstance(creative, dict) or not isinstance(creative.get("definition_ref"), str)
            or not isinstance(creative.get("type_id"), str) or not isinstance(creative.get("automatic_rules"), list)):
        return
    omitted = False
    for rule in creative["automatic_rules"]:
        conditions = [rule.get("condition"), *[item.get("observation")
                      for item in rule.get("drive_inputs", [])]]
        for condition in conditions:
            if (isinstance(condition, dict) and "expression" in condition and
                    not isinstance(condition["expression"], bool)):
                condition.pop("expression")
                omitted = True
    if omitted:
        creative["automatic_rules_expression_projection"] = {
            "scope": "current_conditions_without_duplicate_expressions",
            "read_request": {"tool": "grove_definition_read", "arguments": {
                "definition_ref": creative["definition_ref"]}},
            "type_id": creative["type_id"],
            "meaning": "Current verdicts, facts, checks and boolean literals remain; read omitted expressions from the pinned definition."}


def _companion_inspection_index(payload, *, preserve_facts=False, compact_expressions=False):
    """Index action choices without presenting or binding expanded previews.

    Default inspection preserves all authorized facts. The transport-limit
    fallback retains its existing, explicitly incomplete state projection.
    """
    value = payload.get("result", payload)
    if not isinstance(value, dict) or not isinstance(value.get("entity"), dict):
        return None
    entity = value["entity"]
    projection = value.get("action_options_projection") or {}
    if projection.get("scope") == "selected_declared_action" or projection.get("candidate_selection"):
        return None  # A selected complete action/candidate must never become a fake preview.
    options = value.get("action_options", entity.get("action_options"))
    if not isinstance(options, list):
        return None
    state = (_companion_current_entity(entity) if preserve_facts else
             _companion_selected_state_summary({"entity": entity})["entity"])
    state.pop("action_options", None)
    creative = state.get("creative")
    original_creative = entity.get("creative")
    original_program = entity.get("state", {}).get("program")
    duplicate_state = [f"state.program.{field} (same values in entity.creative.{field})"
        for field in ("fields", "process") if isinstance(original_creative, dict) and
        isinstance(original_program, dict) and field in original_program and field in original_creative and
        original_program[field] == original_creative[field]]
    if not preserve_facts and isinstance(creative, dict):
        program = state.get("state", {}).get("program")
        if isinstance(program, dict):
            # The server exposes the same current fields and process twice.
            # Keep their complete authorized creative values, not two copies,
            # before considering any loss of condition facts or domain state.
            for field in ("fields", "process"):
                if field in program and field in creative and program[field] == creative[field]:
                    program.pop(field)
                    if f"state.program.{field} (same values in entity.creative.{field})" not in duplicate_state:
                        duplicate_state.append(f"state.program.{field} (same values in entity.creative.{field})")
    if not preserve_facts or compact_expressions:
        _companion_condition_expressions(creative)
    outlines = []
    for option in options:
        if not isinstance(option, dict) or not isinstance(option.get("action"), str):
            continue
        outline = {key: deepcopy(option[key]) for key in
            ("action", "label", "function_id", "available", "reason", "use", "preconditions",
             "preconditions_ref", "condition_observation")
            if key in option}
        request = {"tool": "grove_inspect", "arguments": {"entity_id": entity["id"], "action": option["action"]}}
        outline["read_request"] = request
        candidates = (option.get("input") or {}).get("candidates", [])
        choices = option.get("choices") or {}
        candidates = [*candidates, *choices.get("items", []), *choices.get("others", [])]
        if candidates:
            outline["candidates_index"] = []
            for candidate in candidates:
                if not isinstance(candidate, dict) or not isinstance(candidate.get("id"), str):
                    continue
                row = {key: deepcopy(candidate[key]) for key in
                    ("id", "name", "kind", "available", "reason") if key in candidate}
                row["read_request"] = {"tool": "grove_inspect", "arguments":
                    request["arguments"] | {"candidate_id": candidate["id"]}}
                # These identifiers describe the indexed batch kind; amounts,
                # entity versions and previews still require the precise read.
                if preserve_facts and isinstance(candidate.get("lots"), list):
                    row["lot_index"] = [{"id": lot["id"]} for lot in candidate["lots"]
                        if isinstance(lot, dict) and isinstance(lot.get("id"), str)]
                outline["candidates_index"].append(row)
        elif isinstance(option.get("candidates_index"), list):
            # A transport-limit fallback can receive an already indexed read.
            # Keep its genuine paths; omission must not erase discovery.
            outline["candidates_index"] = deepcopy(option["candidates_index"])
            if not preserve_facts:
                # Optional diagnostic IDs give way to actual conditions and
                # exact expansion paths at the existing transport boundary.
                for row in outline["candidates_index"]:
                    if isinstance(row, dict):
                        row.pop("lot_index", None)
        sources = choices.get("sources", [])
        if sources:
            # inspect candidate_id selects input.candidates and items/others,
            # not fabrication source groups. Their complete lots require the
            # action read, rather than an unsupported candidate route.
            outline["sources_index"] = [{key: deepcopy(source[key]) for key in
                ("id", "name", "kind", "available", "reason") if key in source} |
                {"read_request": deepcopy(request)} for source in sources
                if isinstance(source, dict) and isinstance(source.get("id"), str)]
        elif isinstance(option.get("sources_index"), list):
            outline["sources_index"] = deepcopy(option["sources_index"])
        outlines.append(outline)
    # Shared access observations are identical facts, not separate verdicts for
    # each button. Preserve action-specific conditions verbatim beside them.
    shared = deepcopy(value.get("shared_preconditions", []))
    shared = shared if isinstance(shared, list) else []
    counts = {}
    for outline in outlines:
        if isinstance(outline.get("preconditions"), list) and outline["preconditions"]:
            key = json.dumps(outline["preconditions"], ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            counts[key] = counts.get(key, 0) + 1
    references = {}
    for outline in outlines:
        conditions = outline.get("preconditions")
        if not isinstance(conditions, list) or not conditions:
            continue
        key = json.dumps(conditions, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        if counts[key] < 2:
            continue
        if key not in references:
            references[key] = len(shared)
            shared.append(conditions)
        outline.pop("preconditions")
        outline["preconditions_ref"] = "#/shared_preconditions/" + str(references[key])
    index = deepcopy(value) if preserve_facts else {"player": deepcopy(value.get("player"))}
    index.update({"entity": state,
        "action_options": outlines,
        "available_actions": deepcopy(value.get("available_actions", entity.get("available_actions", []))),
        "action_options_projection": {"scope": "authorized_action_index", "expanded_actions": [],
            "requested_projection": deepcopy(projection),
            "meaning": "Index only; inputs and previews are unprovided/unbound. preconditions_ref "
                       "resolves within this inspection result. Use read_request for a complete action."},
        "projection": {"scope": "object_state_and_action_index", "complete_inspection": False,
            "complete_object_state": preserve_facts,
            "omitted_sections": ["expanded action inputs and previews", *duplicate_state] + ([] if preserve_facts else
                ["child snapshots", "public content history"])}})
    if shared:
        index["shared_preconditions"] = shared
    if "explanation" in value:
        index["explanation"] = deepcopy(value["explanation"])
    if "result" not in payload:
        return index
    if preserve_facts:
        result = deepcopy(payload)
        result["result"] = index
        return result
    return {key: deepcopy(payload[key]) for key in
        ("world_id", "function_id", "function_version", "snapshot_seq") if key in payload} | {"result": index}


def _bounded_agent_projection(function, payload):
    """Run before observation binding; oversized reads never imply full inspect."""
    if _agent_tool_text_fits(_agent_tool_text(payload)):
        return payload
    if function == "execution_checkpoint":
        # A dense scene may fit independently, as may the selected state, while
        # their combined terminal response does not. Keep that state's current
        # facts whole and explicitly leave discovery to an ordinary map read.
        result = deepcopy(payload)
        facts = result.get("execution_readback", {})
        retained = {key: deepcopy(facts[key]) for key in (
            "player", "clock", "observed_at", "selected_inspection", "selected_inspection_unavailable", "world_results",
            "action_targets", "action_targets_meaning", "task_action_summary", "public_events") if key in facts}
        retained["projection"] = {"scope": "checkpoint_current_selected_facts", "complete_scene": False,
            "omitted_sections": [key for key in facts if key not in retained and key != "projection"],
            "read_request": {"tool": "map_viewport", "arguments": {}},
            "meaning": "Current selected facts, player and clock are retained whole. Scene discovery was not supplied or bound; read the map separately. This does not reopen writes."}
        result["execution_readback"] = retained
        if _agent_tool_text_fits(_agent_tool_text(result)):
            return result
    if function in {"grove.definition_read", "grove.definition_publish"}:
        complete = deepcopy(payload)
        value = complete.get("result", complete)
        if isinstance(value, dict) and isinstance(value.get("definition"), dict):
            omitted = [key for key in ("material_semantics", "declaration_semantics") if key in value]
            for key in omitted:
                value.pop(key)
            value["projection"] = {"scope": "complete_definition_without_derived_explanations",
                "complete_definition": True, "complete_explanation": False, "omitted_sections": omitted,
                "meaning": "The immutable definition, exact rules, actions, coefficients, phases and material imports "
                    "remain complete. Only duplicated derived explanations were omitted, not rules or capabilities. "
                    "Read grove_rule_catalog for language and phase policy; grove_inspect for actual lots, permissions "
                    "and current action inputs. This definition is not an observation of an instance."}
            if omitted and _agent_tool_text_fits(_agent_tool_text(complete)):
                return complete
    if function == "grove.inspect":
        index = _companion_inspection_index(payload)
        if index is not None and _agent_tool_text_fits(_agent_tool_text(index)):
            return index
    if function == "map.viewport":
        scene = payload.get("result", payload)
        if isinstance(scene, dict):
            index = _companion_scene_index(scene)
            projected = ({key: deepcopy(payload[key]) for key in
                ("world_id", "function_id", "function_version", "snapshot_seq") if key in payload}
                | {"result": index}) if "result" in payload else index
            index["projection"].update(reason="ToolOutputLimit", complete_scene=False,
                omitted_rows={}, read_request={"tool": "map_viewport", "arguments": {"radius": 1}},
                meaning=index["projection"]["meaning"] + " Complete encoded terrain is retained when supplied; "
                    "any omitted discovery rows are counted below. Use a smaller viewport centered on a "
                    "nearby coordinate for omitted public objects, or grove_inspect for a retained actual ID.")
            # A dense scene must not turn four tiny encoded chunks into a
            # failed read. Bind only rows actually shown; no hidden versions.
            for field in ("entities", "nearby_entities", "resource_nodes", "water_sources",
                          "players", "nearby_players", "inventory"):
                rows = index.get(field)
                while isinstance(rows, list) and rows and not _agent_tool_text_fits(_agent_tool_text(projected)):
                    if field == "inventory" and len(rows) == 1:
                        break
                    rows.pop()
                    omitted = index["projection"]["omitted_rows"]
                    omitted[field] = omitted.get(field, 0) + 1
                    index["truncated"] = True
                    if field == "inventory":
                        # Never advance past a withheld inventory row.
                        index["inventory_next"] = rows[-1].get("id") if isinstance(rows[-1], dict) else None
            if _agent_tool_text_fits(_agent_tool_text(projected)):
                return projected
        raise ConnectorError("ToolOutputTooLarge", "The current scene exceeds the tool output limit. "
            "Its omitted state was not provided or bound. Read map_viewport with a smaller radius "
            "and nearby center, or inspect a known actual object ID; do not guess missing terrain.", 413)
    if function not in AGENT_WRITE_FUNCTIONS:
        raise ConnectorError("ToolOutputTooLarge", "The complete world read exceeds the native tool output limit. "
            "Its expanded content and action previews were not provided. For grove_inspect, specify its optional "
            "action to read only that action's complete current inputs and previews; "
            "do not claim a complete inspection or choose an action from missing previews.", 413)

    # A committed write is never converted into a failed/retryable operation.
    # This response omits expanded state explicitly and retains the real receipt.
    result = {key: deepcopy(payload[key]) for key in (
        "operation_id", "function_id", "ok", "status", "commit_seq", "actor_role_id", "credential_id",
        "function_version", "intent_fingerprint", "replayed", "execution_control", "confirmed_outcome", "busy_recovery") if key in payload}
    operation_result = payload.get("result")
    if isinstance(operation_result, dict):
        receipt_fields = {key: deepcopy(operation_result[key]) for key in
            ("operation_id", "function_id", "ok", "status", "commit_seq", "function_version", "intent_fingerprint", "replayed")
            if key in operation_result}
        if function == "grove.join":
            # Entry has a definite world result independent of the expanded
            # scene. Retain the returned self snapshot and resumed verdict,
            # rather than reducing a confirmed entry to commit metadata alone.
            receipt_fields.update({key: deepcopy(operation_result[key]) for key in
                                   ("player", "resumed") if key in operation_result})
        if receipt_fields:
            result["result"] = receipt_fields
    readback = payload.get("execution_readback", {})
    compact = {}
    if isinstance(readback, dict):
        if "observed_at" in readback:
            compact["observed_at"] = deepcopy(readback["observed_at"])
        player = readback.get("player")
        if isinstance(player, dict):
            compact["player"] = {key: deepcopy(player[key]) for key in
                ("id", "version", "x", "y", "location") if key in player}
        selected = readback.get("selected_inspection", {})
        entity = selected.get("entity") if isinstance(selected, dict) else None
        if isinstance(entity, dict):
            identity = {key: deepcopy(entity[key]) for key in ("id", "version", "interaction_revision", "pour_target_slot") if key in entity}
            compact["selected_inspection"] = {"entity": identity,
                "projection": {"scope": "selected_identity_only", "state_and_previews_included": False}}
            if isinstance(identity.get("id"), str):
                compact["selected_inspection"]["read_request"] = {
                    "tool": "grove_inspect", "arguments": {"entity_id": identity["id"]}}
        targets = readback.get("action_targets")
        if isinstance(targets, list):
            compact["action_targets"] = []
            for target in targets:
                if not isinstance(target, dict):
                    continue
                row = {key: deepcopy(target[key]) for key in ("entity_id", "observed_at", "unavailable") if key in target}
                entity = target.get("inspection", {}).get("entity")
                if isinstance(entity, dict) and entity.get("id") == row.get("entity_id"):
                    row["inspection"] = {"entity": {key: deepcopy(entity[key]) for key in ("id", "version", "interaction_revision") if key in entity},
                        "projection": {"scope": "action_target_identity_only", "state_and_previews_included": False},
                        "read_request": {"tool": "grove_inspect", "arguments": {"entity_id": entity["id"]}}}
                compact["action_targets"].append(row)
    result["execution_readback"] = compact
    result["projection"] = {"scope": "write_receipt_and_read_entries", "expanded_state_included": False,
        "world_verdict_included": False, "goal_result_included": False,
        "reason": "NativeToolOutputLimit",
        "omitted_sections": ["expanded result", "expanded execution_readback", "event sequence lists"],
        "meaning": "The original operation's receipt and control are preserved; expanded state and action previews "
                   "are unexpanded, not absent. Do not repeat the write to obtain its output. Read current state "
                   "through the declared world tools before another consequential action. Receipt ok/commit "
                   "metadata does not verify physical success or the person's natural-language goal."}
    result["read_requests"] = [{"tool": "map_viewport", "arguments": {}}]
    if "execution_control" in payload:
        result["read_requests"].append({"tool": "grove_control_status", "arguments": {}})
    for target in compact.get("action_targets", []):
        request = target.get("inspection", {}).get("read_request")
        if request:
            result["read_requests"].append(deepcopy(request))
    # Keep each available verdict whole when it fits, or explicitly mark it
    # unprovided; never turn call success into a goal/contract verdict.
    omitted_verdicts = set()
    for source, destination, field, marker, path in (
        (payload, result, "world_results", "world_verdict_included", "world_results"),
        (readback, compact, "world_results", "world_verdict_included", "execution_readback.world_results"),
        (payload, result, "goal_result", "goal_result_included", "goal_result"),
    ):
        if not isinstance(source, dict) or field not in source:
            continue
        destination[field] = deepcopy(source[field])
        if _agent_tool_text_fits(_agent_tool_text(result)):
            result["projection"][marker] = True
        else:
            destination.pop(field)
            omitted_verdicts.add(marker)
            result["projection"]["omitted_sections"].append(path)
    for marker in omitted_verdicts:
        result["projection"][marker] = False
    if not _agent_tool_text_fits(_agent_tool_text(result)):
        raise ConnectorError("ToolOutputTooLarge", "The original write receipt itself exceeds the native output limit. "
            "Its operation remains recorded; recover the original receipt before another write.", 413)
    return result


def _checked_agent_tool_text(payload):
    """Serialize the final bound object without changing its displayed facts."""
    text = _agent_tool_text(payload)
    if not _agent_tool_text_fits(text, native=True):
        raise ConnectorError("ToolOutputTooLarge", "The final tool response exceeds the native output limit. "
            "Expanded content was not provided. A completed world operation remains committed; "
            "recover its original receipt before retrying a write.", 413)
    return text


def _agent_version_field(name):
    match = re.fullmatch(r"expected_(.+)_version", name)
    return match.group(1) if match else "anchor" if name == "observed_anchor_version" else None


def _agent_interaction_revision_field(name):
    return {"expected_target_interaction_revision": "target",
            "expected_item_interaction_revision": "item"}.get(name)


def agent_tool_schema(schema, *, function=None, guard_paths=None):
    """Hide only optimistic snapshot guards from this connector's MCP input."""
    result = deepcopy(schema)

    def visit(value, path=()):
        if not isinstance(value, dict):
            return
        guarded = path in {(), ("continuation",)}
        source_row = function == "grove.fabricate" and path == ("sources", "[]")
        properties = value.get("properties", {})
        for key in list(properties):
            declared_guard = guard_paths is not None and ["*" if item == "[]" else item for item in (*path, key)] in guard_paths
            inferred_guard = guard_paths is None and ((guarded and (_agent_version_field(key) or _agent_interaction_revision_field(key))) or (source_row and key == "expected_entity_version"))
            if declared_guard or inferred_guard:
                properties.pop(key)
            else:
                visit(properties[key], (*path, key))
        if guard_paths is not None and isinstance(value.get("required"), list):
            value["required"] = [key for key in value["required"]
                if ["*" if item == "[]" else item for item in (*path, key)] not in guard_paths]
        elif guarded and isinstance(value.get("required"), list):
            value["required"] = [key for key in value["required"]
                                 if not (_agent_version_field(key) or _agent_interaction_revision_field(key))]
        elif source_row and isinstance(value.get("required"), list):
            value["required"] = [key for key in value["required"] if key != "expected_entity_version"]
        for branch in ("oneOf", "anyOf", "allOf"):
            for child in value.get(branch, []):
                visit(child, path)
        visit(value.get("items"), (*path, "[]"))

    # Published definitions and author action params are semantic inputs, even
    # when an author gives a parameter a name resembling a snapshot guard.
    visit(result)
    return result


def agent_mcp_schema(schema, *, function, access=None, guard_paths=None):
    """Public fixed-observation input; host execution control is separate."""
    result = agent_tool_schema(schema, function=function, guard_paths=guard_paths)
    properties = result.setdefault("properties", {})
    if access == "write" or access is None and function in AGENT_WRITE_FUNCTIONS:
        properties.pop("delegation", None)
        result["required"] = [key for key in result.get("required", []) if key != "delegation"]
        properties["observation_id"] = {
            "type": "string", "minLength": 1, "maxLength": 100,
            "description": "Copy the opaque observation_id returned with these exact facts. Do not invent an ID."}
        for key in ("observation_id",):
            if key not in result["required"]:
                result["required"].append(key)
    else:
        properties["base_observation_id"] = {
            "type": "string", "minLength": 1, "maxLength": 100,
            "description": "Optional previous observation_id to extend with this read only; omitted uses the request preflight."}
    return result


def _project_agent_menus(value, schema_projector=None, *, source_function=None):
    """Project only server-generated scene menus before sizing and binding."""
    if not isinstance(value, dict):
        return
    options = value.get("action_options") or []
    if source_function == "grove.build_options" and isinstance(value.get("options"), list):
        options = [*options, *value["options"]]
    action = value.get("action")
    if isinstance(action, dict) and action.get("function_id") == "grove.fabricate":
        options = [*options, action]
    for option in options:
        if not isinstance(option, dict):
            continue
        function = option.get("function_id")
        if function in AGENT_WRITE_FUNCTIONS and isinstance(option.get("input_schema"), dict):
            option["mcp_tool"] = function.replace(".", "_")
            option["input_schema"] = (schema_projector(function, option["input_schema"]) if schema_projector
                                      else agent_mcp_schema(option["input_schema"], function=function))
        if isinstance(option.get("arguments"), dict):
            option["arguments"] = {key: item for key, item in option["arguments"].items()
                                   if not (_agent_version_field(key) or _agent_interaction_revision_field(key))}
            if function == "grove.fabricate":
                for source in option["arguments"].get("sources", []):
                    if isinstance(source, dict):
                        source.pop("expected_entity_version", None)
    for key in ("result", "entity", "viewport", "selected_inspection", "execution_readback"):
        _project_agent_menus(value.get(key), schema_projector, source_function=source_function)
    for key in ("entities", "inventory", "contents", "nearby_entities", "resource_nodes", "water_sources"):
        for item in value.get(key, []) or []:
            _project_agent_menus(item, schema_projector, source_function=source_function)


class AgentObservations:
    """Keep only observations actually presented by this component.

    Sequence persistence is the only external callback: it reserves a handle
    before publication. Observation bundles and the preflight handle are never
    persisted or reconstructed from a receipt, host history or current state.
    """

    def __init__(self, role_id: str, *, sequence: int = 0,
                 reserve_sequence: Callable[[int], None] | None = None):
        if not isinstance(role_id, str) or not role_id:
            raise ValueError("role_id must identify the authenticated role")
        if type(sequence) is not int or sequence < 0:
            raise ValueError("observation sequence must be a nonnegative integer")
        self.role_id = role_id
        self._sequence = sequence
        self._reserve_sequence = reserve_sequence or (lambda sequence: None)
        self._observations: dict[str, dict] = {}
        self._preflight_id: str | None = None

    @property
    def sequence(self) -> int:
        return self._sequence

    @property
    def preflight_id(self) -> str | None:
        return self._preflight_id

    @staticmethod
    def tool_schema(schema: dict, *, function=None) -> dict:
        return agent_tool_schema(schema, function=function)

    @staticmethod
    def serialize(payload: dict) -> str:
        return _checked_agent_tool_text(payload)

    def _require(self, observation_id):
        if not isinstance(observation_id, str) or observation_id not in self._observations:
            raise ConnectorError("ObservationRequired", "请先读取世界并引用返回的observation_id；当前观察标识未知或已因服务重启失效。", 409)
        return self._observations[observation_id]

    def get(self, observation_id):
        """Detach external readers; internal binding only reads the fixed bundle."""
        return deepcopy(self._require(observation_id))

    def present(self, function, payload, *, base_id=None, preflight=False, action_entity_ids=(), schema_projector=None):
        """Create a new fixed bundle solely from this presented response.

        No invoke/UI/recovery path calls this. Another parallel tool's unseen
        response cannot alter the bundle selected by an already submitted write.
        """
        result = companion_projection(function, payload)
        bundle = (self.get(base_id) if base_id is not None else
                  {"versions": {kind: {} for kind in ("player", "entity", "opportunity", "content")},
                   "continuation_bases": {}, "pour_target_slots": {}})
        if not preflight and function in AGENT_ACTION_FUNCTIONS | {"grove.definition_publish", "execution_checkpoint"}:
            result = _companion_action_projection(result, base_id=base_id, versions=bundle["versions"],
                entity_ids=action_entity_ids, source=payload)
        _project_agent_menus(result, schema_projector, source_function=function)
        # Tool responses and the turn's fresh preflight share the same budget
        # before binding. Complete selected previews remain mandatory facts.
        result = _companion_fact_budget(function, result)
        result = _bounded_agent_projection(function, result)
        # Protocol context and formal follow-up reads describe the authorized
        # source, not hidden world facts or a replacement binding basis.
        source = payload.get("result", payload)
        destination = result.get("result", result)
        if isinstance(source, dict) and isinstance(destination, dict):
            for field in ("agent_context", "read_requests"):
                if field in source:
                    destination[field] = deepcopy(source[field])

        def remember(kind, value, id_key="id", version_key="version"):
            if isinstance(value, dict) and isinstance(value.get(id_key), str) and type(value.get(version_key)) is int and value[version_key] > 0:
                previous = bundle["versions"][kind].get(value[id_key])
                bundle["versions"][kind][value[id_key]] = value[version_key]
                if kind == "entity":
                    revisions = bundle.setdefault("interaction_revisions", {})
                    revision = value.get("interaction_revision")
                    if type(revision) is int and 0 < revision <= value[version_key]:
                        revisions[value[id_key]] = revision
                    elif previous != value[version_key]:
                        revisions.pop(value[id_key], None)
                    # Diagnose an actual presented lot copied into an entity
                    # field, without deriving its carrier or binding a version.
                    for key in ("matter", "lots"):
                        for lot in value.get(key, []) if isinstance(value.get(key), list) else []:
                            if isinstance(lot, dict) and isinstance(lot.get("id"), str):
                                bundle.setdefault("presented_lot_ids", {})[lot["id"]] = True

        def rows(value):
            return value if isinstance(value, list) else []

        def options(values, *, target=None):
            for option in values if isinstance(values, list) else []:
                if not isinstance(option, dict):
                    continue
                for candidate in rows(option.get("candidates_index")):
                    if isinstance(candidate, dict):
                        for lot in rows(candidate.get("lot_index")):
                            if isinstance(lot, dict) and isinstance(lot.get("id"), str):
                                bundle.setdefault("presented_lot_ids", {})[lot["id"]] = True
                # These are current server-generated choices, not authored
                # descriptions, program fields, schemas or historical receipts.
                inputs = option.get("input", {})
                candidates = rows(inputs.get("candidates")) if isinstance(inputs, dict) else []
                choices = option.get("choices", {})
                if isinstance(choices, dict):
                    candidates = [*candidates, *rows(choices.get("items")), *rows(choices.get("others"))]
                    if option.get("function_id") == "grove.fabricate":
                        for source in rows(choices.get("sources")):
                            # A source row's id identifies the real entity;
                            # its nested lot ids never become entity snapshots.
                            remember("entity", source)
                if option.get("function_id") == "grove.build":
                    for material in rows(option.get("material_candidates")):
                        # These actual owned rows are part of the delivered
                        # server menu, rather than recipe schemas or lot IDs.
                        remember("entity", material)
                for candidate in candidates:
                    if isinstance(candidate, dict):
                        remember("player" if candidate.get("kind") == "role" else "entity", candidate)
                        preview = candidate.get("preview")
                        if (option.get("action") in {"store", "attach"} and isinstance(target, str) and
                                isinstance(candidate.get("id"), str) and isinstance(preview, dict) and
                                re.fullmatch(r"[0-9a-f]{64}", str(preview.get("version_fingerprint", "")))):
                            key = json.dumps([target, candidate["id"], option["action"]])
                            bundle.setdefault("combination_previews", {})[key] = {
                                "fingerprint": preview["version_fingerprint"],
                                "player_version": bundle["versions"]["player"].get(self.role_id),
                                "target_version": bundle["versions"]["entity"].get(target),
                                "item_version": bundle["versions"]["entity"].get(candidate["id"])}

        def entity(value):
            if isinstance(value, dict):
                identity, version = value.get("id"), value.get("version")
                previous_version = bundle["versions"]["entity"].get(identity) if isinstance(identity, str) else None
                remember("entity", value)
                if isinstance(identity, str) and type(version) is int and version > 0:
                    slots = bundle.setdefault("pour_target_slots", {})
                    if value.get("pour_target_slot") in ("contained", "surface"):
                        slots[identity] = value["pour_target_slot"]
                    elif "pour_target_slot" in value or previous_version != version:
                        # A newer identity-only projection cannot silently
                        # upgrade an older action basis it did not present.
                        slots.pop(identity, None)
                options(value.get("action_options"), target=value.get("id"))

        def content(value):
            if isinstance(value, list):
                for item in value:
                    content(item)
            elif isinstance(value, dict):
                remember("content", value)
                for anchor in rows(value.get("anchors")):
                    remember("entity", anchor, "entity_id")
                    if isinstance(anchor, dict):
                        entity(anchor.get("entity"))

        def scene(value):
            if not isinstance(value, dict):
                return
            # Regional indexes precede direct snapshots; operation snapshots
            # precede viewport, selected inspection and final execution readback.
            # This ordering uses their actual sources, never the largest number.
            content(value.get("public_content"))
            content(value.get("public_contents"))
            scene(value.get("public_facts"))
            remember("player", value.get("player"))
            for key in ("players", "nearby_players"):
                for item in rows(value.get(key)):
                    remember("player", item)
            for key in ("entities", "inventory", "contents", "nearby_entities", "resource_nodes", "water_sources"):
                for item in rows(value.get(key)):
                    entity(item)
            entity(value.get("entity"))
            selected = value.get("entity")
            options(value.get("action_options"), target=selected.get("id") if isinstance(selected, dict) else None)
            if function == "grove.fabrication_options":
                action = value.get("action")
                if isinstance(action, dict) and action.get("function_id") == "grove.fabricate":
                    options([action])
            opportunities = list(rows(value.get("opportunities")))
            if isinstance(value.get("opportunity"), dict):
                opportunities.append(value["opportunity"])
            for opportunity in opportunities:
                remember("opportunity", opportunity)
                if isinstance(opportunity, dict) and isinstance(opportunity.get("id"), str) and "continuation_basis" in opportunity:
                    # The claimed opportunity's basis is immutable admission
                    # evidence. Later live anchor reads cannot replace it.
                    bundle["continuation_bases"][opportunity["id"]] = deepcopy(opportunity["continuation_basis"])
            scene(value.get("viewport"))
            scene(value.get("selected_inspection"))
            for target in rows(value.get("action_targets")):
                if isinstance(target, dict):
                    scene(target.get("inspection"))

        presented = result.get("result", result)
        scene(presented)
        scene(result.get("execution_readback"))
        if function == "grove.build_options" and isinstance(presented, dict):
            # This authenticated self read uses a version scalar instead of a
            # player snapshot. Copy its actual version; do not reuse a base's
            # old guard or infer one from editable menu arguments or schemas.
            bundle["versions"]["player"].pop(self.role_id, None)
            remember("player", {"id": self.role_id, "version": presented.get("player_version")})
            options(presented.get("options"))
        # Reserve before publication. The random tail keeps a parallel reader's
        # next handle unpredictable; the durable sequence prevents restart reuse.
        self._sequence += 1
        self._reserve_sequence(self._sequence)
        observation_id = f"obs{self._sequence}-{secrets.token_hex(4)}"
        self._observations[observation_id] = bundle
        result = {"observation_id": observation_id,
                  **{key: result[key] for key in ("operation_id", "function_id", "ok", "commit_seq") if key in result},
                  **result}
        if preflight:
            self._preflight_id = observation_id
        return result

    def bind(self, function, arguments, observation_id, schema, *, guard_paths=None):
        """Use an explicitly selected immutable observation; never query state."""
        bundle = self._require(observation_id)
        root = deepcopy(arguments)
        versions = bundle["versions"]
        if (function == "grove.interact" and root.get("action") == "pour" and
                ("amount" in root or "target_slot" in root)):
            slot = bundle.get("pour_target_slots", {}).get(root.get("target_id"))
            if slot not in ("contained", "surface"):
                raise ConnectorError("ObservedActionRequired", "此观察未呈现目标的pour_target_slot；请先检查目标并引用返回的observation_id，不能推导倒水去向。", 409)
            if "target_slot" in root and root["target_slot"] != slot:
                raise ConnectorError("ObservedActionMismatch", "倒水target_slot与此观察中呈现的原去向不符；不会换槽或重新绑定。", 409)
            if "amount" in root:
                root.setdefault("target_slot", slot)

        def missing(subject, identity):
            if subject in {"target", "item", "other", "entity"} and isinstance(identity, str) and identity in bundle.get("presented_lot_ids", {}):
                raise ConnectorError("ObservedVersionRequired", f"此观察展示的{identity}是材料lot_id，不是承载材料的物件id。{subject}_id需复制已展示的物件id，lot_id另填该物件的真实批次id；不会猜测或替换来源。", 409)
            raise ConnectorError("ObservedVersionRequired", f"此观察中没有{subject} {identity or ''}的版本；请先读取该对象，并引用新返回的observation_id。", 409)

        def fixed_basis(node, subject):
            basis = bundle["continuation_bases"].get(root.get("opportunity_id"))
            if not isinstance(basis, dict) or basis.get("content_id") != node.get("content_id"):
                missing("创作机会固定的内容接续依据", root.get("opportunity_id"))
            if subject == "content":
                return basis.get("expected_content_version")
            anchor = next((item for item in basis.get("anchors", [])
                           if item.get("entity_id") == node.get("anchor_entity_id")), None)
            if anchor is None:
                missing("创作机会固定的锚点", node.get("anchor_entity_id"))
            return anchor.get("observed_anchor_version")

        def bind(schema, node, path=()):
            if not isinstance(schema, dict) or not isinstance(node, dict):
                return node
            props = schema.get("properties", {})
            for key, field in props.items():
                if guard_paths is not None and ["*" if item == "[]" else item for item in (*path, key)] not in guard_paths:
                    if key in node and isinstance(node[key], dict):
                        node[key] = bind(field, node[key], (*path, key))
                    elif function == "grove.fabricate" and not path and key == "sources" and isinstance(node.get(key), list):
                        node[key] = [bind(field.get("items", {}), source, ("sources", "[]")) for source in node[key]]
                    continue
                interaction_subject = _agent_interaction_revision_field(key) if not path else None
                if interaction_subject:
                    node.pop(key, None)
                    identity = node.get(interaction_subject + "_id")
                    if function == "grove.interact" and (interaction_subject == "target"
                            or interaction_subject == "item" and root.get("action") == "drop"):
                        identity = root.get("target_id") or root.get("item_id")
                    revision = bundle.get("interaction_revisions", {}).get(identity)
                    if type(revision) is int and revision > 0:
                        node[key] = revision
                    continue
                subject = _agent_version_field(key) if path in {(), ("continuation",)} else None
                if function == "grove.fabricate" and path == ("sources", "[]") and key == "expected_entity_version":
                    subject = "entity"
                if subject is None:
                    if key in node and isinstance(node[key], dict):
                        node[key] = bind(field, node[key], (*path, key))
                    elif function == "grove.fabricate" and not path and key == "sources" and isinstance(node.get(key), list):
                        node[key] = [bind(field.get("items", {}), source, ("sources", "[]")) for source in node[key]]
                    continue
                node.pop(key, None)  # A copied model number is never authority.
                if path == ("continuation",) and subject in {"content", "anchor"}:
                    version = fixed_basis(node, subject)
                else:
                    identity = self.role_id if subject == "player" else node.get(subject + "_id")
                    kind = subject if subject in {"player", "opportunity", "content"} else "entity"
                    if function == "grove.interact":
                        if subject == "target" or (subject == "item" and root.get("action") == "drop"):
                            identity = root.get("target_id") or root.get("item_id")
                    if function == "grove.rule_action" and subject == "other" and root.get("action") == "offer":
                        kind = "player"
                    if identity is None and key not in schema.get("required", []):
                        continue
                    version = versions[kind].get(identity) if isinstance(identity, str) else None
                if type(version) is not int or version < 1:
                    missing(subject, identity if path != ("continuation",) else node.get("anchor_entity_id", node.get("content_id")))
                node[key] = version
            return node

        bound = bind(schema, root)
        if function == "grove.interact" and bound.get("action") in {"store", "attach"}:
            key = json.dumps([bound.get("target_id"), bound.get("item_id"), bound["action"]])
            preview = bundle.get("combination_previews", {}).get(key)
            if (not preview or preview.get("player_version") != bound.get("expected_player_version") or
                    preview.get("target_version") != bound.get("expected_target_version") or
                    preview.get("item_version") != bound.get("expected_item_version")):
                raise ConnectorError("ObservedPreviewRequired", "装入或附着前，请读取目标物件的完整当前候选预览，并引用该读取的observation_id。", 409)
            explicit = bound.get("expected_combination_fingerprint")
            if explicit is not None and explicit != preview["fingerprint"]:
                raise ConnectorError("ObservedPreviewMismatch", "组合指纹不是此观察中该行动的候选预览；请重新读取目标物件。", 409)
            # The world rechecks this fingerprint's affected structures. A
            # compact write readback cannot upgrade an older loading preview.
            bound["expected_combination_fingerprint"] = preview["fingerprint"]
        return bound
