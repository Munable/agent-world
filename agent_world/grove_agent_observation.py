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

AGENT_WRITE_FUNCTIONS = frozenset({"grove.move", "grove.interact", "grove.build", "grove.generation_context",
    "grove.content_submit", "grove.content_release", "grove.definition_publish", "grove.rule_action", "grove.fabricate"})
AGENT_ACTION_FUNCTIONS = frozenset({"grove.move", "grove.interact", "grove.build", "grove.rule_action", "grove.fabricate"})

_NATIVE_TOOL_MAX_BYTES = 50 * 1024
_NATIVE_TOOL_MAX_LINES = 2000
_AGENT_TOOL_MAX_BYTES = 48 * 1024
_AGENT_TOOL_MAX_LINES = 1800


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
                "entity_id", "version", "name", "x", "y", "created_by", "source",
                "authored_description_excerpt", "description_truncated") if key in anchor}
            entity = anchor.get("entity")
            if isinstance(entity, dict):
                row.update({key: deepcopy(entity[key]) for key in
                    ("name", "x", "y", "created_by", "source") if key in entity})
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
    """Retain a few authorized runtime facts without expanding declarations."""
    summary = {key: deepcopy(creative[key]) for key in
               ("definition_ref", "type_id", "hidden") if key in creative}
    if creative.get("hidden") is True:
        return summary
    for key in ("automatic_rule_count", "explicit_action_count", "automatic_rules_projection"):
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
             "last_tick_meaning") if key in process}
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
            "role", "solver_eligible", "stored_output_is_history", "pending_next_step", "observed_tick")
            if key in mechanical}
        stored = mechanical.get("stored_state")
        if isinstance(stored, dict):
            observation["stored_state"] = {key: deepcopy(stored[key]) for key in
                ("position", "angle", "speed", "energy", "enabled", "status", "available_torque")
                if key in stored}
        for key, fields in (("last_solved", ("step", "historical")),
                            ("drive_request", ("enabled", "requested_step", "applies_from_step"))):
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


def _companion_scene_index(scene):
    """An authorized discovery index; detailed physics remains an explicit read."""
    if not isinstance(scene, dict):
        return scene
    index = {key: deepcopy(scene[key]) for key in (
        "player", "players", "nearby_players", "clock", "bounds", "inventory_total",
        "inventory_next", "truncated", "region_entity_count", "selected_inspection", "observed_at",
        "world_results", "selected_inspection_unavailable") if key in scene}
    if isinstance(scene.get("chunks"), list):
        index["chunks"] = [_companion_chunk_terrain(chunk) for chunk in scene["chunks"]]
    construction = scene.get("construction")
    if isinstance(construction, dict):
        index["construction"] = {key: deepcopy(construction[key]) for key in
                                 ("material_totals", "unit", "radius_metric", "limits") if key in construction}
        if isinstance(construction.get("recipes"), list):
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
                    "id", "version", "name", "x", "y", "location", "owner", "physical_parent",
                    "entity_ref", "material", "capabilities", "container", "lifecycle", "created_by",
                    "source", "barrier_active", "contents_visibility", "content_summary", "matter_unit",
                    "pour_target_slot") if key in entity}
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
                    row["creative"] = _companion_creative_summary(creative)
                if isinstance(entity.get("id"), str):
                    row["read_request"] = {"tool": "grove_inspect", "arguments": {"entity_id": entity["id"]}}
                state = entity.get("state")
                if not isinstance(state, dict):
                    state = entity.get("discovery_state", {})
                row["discovery_state"] = {key: deepcopy(state[key]) for key in
                    ("kind", "resource_kind", "stock", "open", "recipe") if key in state}
                if (entity.get("owner") is None and "container" in entity.get("capabilities", [])
                        and not state.get("open")):
                    row["contents_visibility"] = "hidden"
                rows.append(row)
            index[field] = rows
    if "public_content" in scene:
        index["public_content"] = _companion_public_content_index(scene["public_content"])
    index["projection"] = {
        "scope": "authorized_scene_index",
        "meaning": "Actual identity/version/location index, not complete state or an action preview. "
                   "Matter rows are actual authorized slot/quantity summaries, not transferable lot IDs; "
                   "canonical material identity is distinct from its display name and instance creator. "
                   "Creative runtime summaries are read snapshots: saved mechanical output is history, "
                   "solver eligibility is not participation, and process.steps counts reactions only. "
                   "last_solved marks output changes, not every solve; a request's earliest step is not confirmation. "
                   "Omitted conditions, contents and history are not absent. Read grove_inspect(entity_id) "
                   "for current physics/actions; grove_content_history(content_id) for history; "
                   "map_viewport for the current scene. Do not infer quantities or success from this index."}
    return index


def _companion_selected_state_summary(inspection):
    """Automatic write readback reports state; explicit inspect owns previews."""
    entity = inspection.get("entity") if isinstance(inspection, dict) else None
    if not isinstance(entity, dict):
        return {"projection": {"scope": "selected_state_unexpanded",
            "meaning": "The selected object's state and action previews were not supplied; read grove_inspect."}}
    # Copy actual authorized values, including physical lots and program state.
    # A new object must not inherit inspection-local references to omitted actions.
    state = {key: deepcopy(entity[key]) for key in (
        "id", "version", "name", "x", "y", "location", "owner", "physical_parent",
        "material", "materials", "matter", "matter_unit", "material_quantity", "simulation",
        "state", "mechanical", "creative", "capabilities", "container", "lifecycle",
        "barrier_active", "contents_visibility", "content_summary", "relations", "created_by", "source",
        "pour_target_slot")
        if key in entity}
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


def companion_projection(function: str, payload: dict) -> dict:
    """Own one mutable copy of an authorized response, then compact its facts."""
    result = deepcopy(payload)
    if function in {"grove.definition_list", "grove.fabrication_options"}:
        # The list's page/cursor contract and selected recipe's complete source
        # lots are already bounded world reads, not expandable scene indexes.
        return result
    if function not in {"map.viewport", "grove.generation_context", "grove.inspect", "grove.handbook", "grove.rule_catalog"} | AGENT_WRITE_FUNCTIONS:
        return result
    value = result.get("result", result)
    if not isinstance(value, dict):
        return result
    if function == "grove.rule_catalog":
        value["definitions_projection"] = "Page actual immutable definitions with grove_definition_list, " \
            "then read the chosen grove_definition_read(definition_ref). This static language catalog is not an empty publication list."
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
        entity = value.get("entity")
        if isinstance(entity, dict):
            for field in ("action_options", "available_actions"):
                if field in entity and field in value and entity[field] == value[field]:
                    # Paths are relative to the inspection result, whether it
                    # is a tool result or this turn's selected_inspection.
                    references = entity.setdefault("field_references", {})
                    if isinstance(references, dict):
                        references[field] = "#/" + field
                        entity.pop(field)
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
            value["entity"] = compact_entity(value["entity"])
        if isinstance(value.get("entities"), list):
            value["entities"] = [compact_entity(entity) for entity in value["entities"]]
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
        "player", "clock", "bounds", "inventory", "inventory_total", "inventory_next", "truncated",
        "observed_at", "selected_inspection", "selected_inspection_unavailable", "world_results") if key in readback}
    construction = readback.get("construction")
    if isinstance(construction, dict):
        compact["construction"] = {key: construction[key] for key in ("material_totals", "unit")
                                   if key in construction}
    rows = readback.get("entities", [])
    rows = rows if isinstance(rows, list) else []
    changed = [row for row in rows if isinstance(row, dict) and
               (row.get("id") not in known or known[row["id"]] != row.get("version"))]
    discovery_limit = 8
    discovery = [row for row in changed if row.get("id") not in relevant]
    retained = [row for row in rows if isinstance(row, dict) and
                (row.get("id") in relevant or row in discovery[:discovery_limit])]
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
        "omitted_changed_or_new_entity_count": max(0, len(discovery) - discovery_limit),
        "omitted_recent_change_count": max(0, len(changes) - 6),
        "discovery_limit": discovery_limit,
        "omitted_sections": ["chunk terrain", "unchanged and excess nearby discovery rows",
                             "other players", "public content/history", "construction recipes"],
        "meaning": "Current actor, actual returned inventory page/material totals, selected state and "
                   "bounded changed/new authorized entity summaries relative to the referenced observation. "
                   "This is not a complete scene or action preview. Omission proves neither absence nor "
                   "unchanged state; retained summaries are not transferable lot choices. Recent changes "
                   "are nearby history, not attribution of this operation. Use map_viewport for discovery "
                   "and grove_inspect for exact current action inputs/previews; chain base_observation_id "
                   "to this response when extending it."}
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


def _companion_inspection_index(payload):
    """Keep actual state and discovery when expanded action inputs do not fit."""
    value = payload.get("result", payload)
    if not isinstance(value, dict) or not isinstance(value.get("entity"), dict):
        return None
    entity = value["entity"]
    projection = value.get("action_options_projection") or {}
    if (projection.get("candidate_selection") or {}).get("candidate_id"):
        return None  # A single complete candidate must never become a fake preview.
    options = value.get("action_options", entity.get("action_options", []))
    if not isinstance(options, list):
        return None
    state = _companion_selected_state_summary({"entity": entity})["entity"]
    creative = state.get("creative")
    if isinstance(creative, dict) and isinstance(creative.get("automatic_rules"), list):
        # The pinned definition is independently readable. Keep every current
        # fact/comparison, omitting only the duplicate immutable expressions.
        omitted = False
        for rule in creative["automatic_rules"]:
            conditions = [rule.get("condition"), *[item.get("observation")
                          for item in rule.get("drive_inputs", [])]]
            for condition in conditions:
                if isinstance(condition, dict) and "expression" in condition:
                    condition.pop("expression")
                    omitted = True
        if omitted:
            creative["automatic_rules_expression_projection"] = {
                "scope": "current_conditions_without_duplicate_expressions",
                "read_request": {"tool": "grove_definition_read", "arguments": {
                    "definition_ref": creative["definition_ref"]}},
                "type_id": creative["type_id"],
                "meaning": "Current verdicts, facts and checks remain. Only immutable expression trees are omitted; "
                           "read the pinned type_id/rule_id or indexed drive effect from this definition."}
    outlines = []
    for option in options:
        if not isinstance(option, dict) or not isinstance(option.get("action"), str):
            continue
        outline = {key: deepcopy(option[key]) for key in
            ("action", "label", "function_id", "available", "reason", "use", "preconditions") if key in option}
        request = {"tool": "grove_inspect", "arguments": {"entity_id": entity["id"], "action": option["action"]}}
        outline["read_request"] = request
        candidates = (option.get("input") or {}).get("candidates", [])
        choices = option.get("choices") or {}
        candidates = [*candidates, *choices.get("items", []), *choices.get("others", []),
                      *choices.get("sources", [])]
        if candidates:
            outline["candidates_index"] = [{key: deepcopy(candidate[key]) for key in
                ("id", "name", "kind") if key in candidate} | {"read_request": {
                    "tool": "grove_inspect", "arguments": request["arguments"] | {"candidate_id": candidate["id"]}}}
                for candidate in candidates if isinstance(candidate, dict) and isinstance(candidate.get("id"), str)]
        outlines.append(outline)
    index = {"player": deepcopy(value.get("player")), "entity": state,
        "action_options": outlines, "available_actions": deepcopy(value.get("available_actions", [])),
        "action_options_projection": {"scope": "authorized_action_index", "expanded_actions": [],
            "requested_projection": deepcopy(projection),
            "meaning": "Actual state and action outlines only. Expanded inputs, candidate versions, conditions and "
                       "previews were not supplied or bound; use each action/candidate read_request before relying on them."},
        "projection": {"scope": "object_state_and_action_index", "complete_inspection": False,
            "omitted_sections": ["expanded action inputs and previews", "child snapshots", "public content history"]}}
    if "explanation" in value:
        index["explanation"] = deepcopy(value["explanation"])
    if "result" not in payload:
        return index
    return {key: deepcopy(payload[key]) for key in
        ("world_id", "function_id", "function_version", "snapshot_seq") if key in payload} | {"result": index}


def _bounded_agent_projection(function, payload):
    """Run before observation binding; oversized reads never imply full inspect."""
    if _agent_tool_text_fits(_agent_tool_text(payload)):
        return payload
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
        "operation_id", "function_id", "ok", "status", "commit_seq", "actor_role_id",
        "function_version", "replayed", "execution_control") if key in payload}
    operation_result = payload.get("result")
    if isinstance(operation_result, dict):
        receipt_fields = {key: deepcopy(operation_result[key]) for key in
            ("operation_id", "function_id", "ok", "status", "commit_seq", "function_version", "replayed")
            if key in operation_result}
        if receipt_fields:
            result["result"] = receipt_fields
    readback = payload.get("execution_readback", {})
    compact = {}
    if isinstance(readback, dict):
        player = readback.get("player")
        if isinstance(player, dict):
            compact["player"] = {key: deepcopy(player[key]) for key in
                ("id", "version", "x", "y", "location") if key in player}
        selected = readback.get("selected_inspection", {})
        entity = selected.get("entity") if isinstance(selected, dict) else None
        if isinstance(entity, dict):
            identity = {key: deepcopy(entity[key]) for key in ("id", "version", "pour_target_slot") if key in entity}
            compact["selected_inspection"] = {"entity": identity,
                "projection": {"scope": "selected_identity_only", "state_and_previews_included": False}}
            if isinstance(identity.get("id"), str):
                compact["selected_inspection"]["read_request"] = {
                    "tool": "grove_inspect", "arguments": {"entity_id": identity["id"]}}
    result["execution_readback"] = compact
    result["projection"] = {"scope": "write_receipt_and_read_entries", "expanded_state_included": False,
        "world_verdict_included": False, "goal_result_included": False,
        "reason": "NativeToolOutputLimit",
        "omitted_sections": ["expanded result", "expanded execution_readback", "event sequence lists"],
        "meaning": "The original operation's receipt and control are preserved; expanded state and action previews "
                   "are unexpanded, not absent. Do not repeat the write to obtain its output. Read current state "
                   "through the declared world tools before another consequential action. Receipt ok/commit "
                   "metadata does not verify physical success or the person's natural-language goal."}
    result["read_requests"] = [{"tool": "map_viewport", "arguments": {}},
                               {"tool": "grove_control_status", "arguments": {}}]
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


def agent_tool_schema(schema, *, function=None):
    """Hide only optimistic snapshot guards from this connector's MCP input."""
    result = deepcopy(schema)

    def visit(value, path=()):
        if not isinstance(value, dict):
            return
        guarded = path in {(), ("continuation",)}
        source_row = function == "grove.fabricate" and path == ("sources", "[]")
        properties = value.get("properties", {})
        for key in list(properties):
            if (guarded and _agent_version_field(key)) or (source_row and key == "expected_entity_version"):
                properties.pop(key)
            else:
                visit(properties[key], (*path, key))
        if guarded and isinstance(value.get("required"), list):
            value["required"] = [key for key in value["required"] if not _agent_version_field(key)]
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

    def present(self, function, payload, *, base_id=None, preflight=False, action_entity_ids=()):
        """Create a new fixed bundle solely from this presented response.

        No invoke/UI/recovery path calls this. Another parallel tool's unseen
        response cannot alter the bundle selected by an already submitted write.
        """
        result = companion_projection(function, payload)
        bundle = (self.get(base_id) if base_id is not None else
                  {"versions": {kind: {} for kind in ("player", "entity", "opportunity", "content")},
                   "continuation_bases": {}, "pour_target_slots": {}})
        if not preflight:
            if function in AGENT_ACTION_FUNCTIONS:
                result = _companion_action_projection(result, base_id=base_id, versions=bundle["versions"],
                    entity_ids=action_entity_ids, source=payload)
            # This is the exact object both MCP representations will deliver.
            # User-message preflights are not native tool output; their complete
            # selected previews have a different transport/input boundary.
            result = _bounded_agent_projection(function, result)

        def remember(kind, value, id_key="id", version_key="version"):
            if isinstance(value, dict) and isinstance(value.get(id_key), str) and type(value.get(version_key)) is int and value[version_key] > 0:
                bundle["versions"][kind][value[id_key]] = value[version_key]

        def rows(value):
            return value if isinstance(value, list) else []

        def options(values, *, target=None):
            for option in values if isinstance(values, list) else []:
                if not isinstance(option, dict):
                    continue
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
                if isinstance(option.get("input_schema"), dict):
                    option["input_schema"] = agent_tool_schema(option["input_schema"], function=option.get("function_id"))
                if isinstance(option.get("arguments"), dict):
                    option["arguments"] = {key: value for key, value in option["arguments"].items()
                                           if not _agent_version_field(key)}
                    if option.get("function_id") == "grove.fabricate":
                        for source in rows(option["arguments"].get("sources")):
                            if isinstance(source, dict):
                                source.pop("expected_entity_version", None)

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

        scene(result.get("result", result))
        scene(result.get("execution_readback"))
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

    def bind(self, function, arguments, observation_id, schema):
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
