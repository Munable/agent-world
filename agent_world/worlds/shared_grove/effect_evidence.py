"""Bounded read evidence for an exact immutable definition and live instance.

Current predicates, publication, action receipts and retained transitions have
different meanings. This reader never evaluates a private goal or reconstructs
missing history, and never attributes a transition to a submitted operation.
"""
from __future__ import annotations

from copy import deepcopy
import json
import operator

from agent_world import FunctionOutcome
from agent_world.errors import RuleViolation
from agent_world.runtime_errors import MAX_ERROR_DETAILS_BYTES
from agent_world.simulation.facts import PUBLIC_FACTS
from agent_world.simulation.programs import exported_interface
from . import action_contracts, definitions, model as m, observations, world_facts


MAX_PREDICATES = 8
MAX_OUTCOMES = 12
DESCRIPTION = ("Read typed current facts, retained transitions, public outcome watermarks and process blockers "
               "for one exact entity_id and immutable definition_ref. A satisfied current predicate is not "
               "effect history. after_step bounds witnessed logical transitions; missing history is unknown. "
               "No task completion or attribution to an operation is inferred.")
ID = {"type": "string", "minLength": 1, "maxLength": 128}
INTEGER = {"type": "integer", "minimum": 0}
SCALAR = {"type": ["integer", "boolean", "string"], "maxLength": 80}
PREDICATE = {"type": "object", "properties": {
    "fact": {"type": "string", "minLength": 1, "maxLength": 80},
    "operator": {"enum": ["eq", "ne", "lt", "lte", "gt", "gte"]}, "value": SCALAR},
    "required": ["fact", "operator", "value"], "additionalProperties": False}
ARGS = {"type": "object", "properties": {
    "entity_id": ID, "definition_ref": ID,
    "predicates": {"type": "array", "maxItems": MAX_PREDICATES, "items": PREDICATE},
    "after_step": INTEGER, "outcome_before": INTEGER, "operation_id": ID},
    "required": ["entity_id", "definition_ref"], "additionalProperties": False}


def _object(properties, required=()):
    return {"type": "object", "properties": properties, "required": list(required), "additionalProperties": False}


def _array(items, maximum):
    return {"type": "array", "maxItems": maximum, "items": items}


CURRENT = _object({"known": {"type": "boolean"}, "actual": SCALAR,
                   "satisfied": {"type": ["boolean", "null"]}, "reason": ID}, ("known", "satisfied"))
WITNESS = _object({"kind": {"const": "automatic_field_change"}, "step": INTEGER,
    "entity_id": ID, "definition_ref": ID, "fact": ID, "before": SCALAR, "after": SCALAR,
    "causal_attribution": {"const": "not_established"}},
    ("kind", "step", "entity_id", "definition_ref", "fact", "before", "after", "causal_attribution"))
VERDICT = _object({"status": {"enum": ["observed", "unknown"]}, "reason": ID,
                  "witnesses": _array(WITNESS, 1)}, ("status", "reason", "witnesses"))
RESULT_PREDICATE = _object({**PREDICATE["properties"], "current": CURRENT, "effect_observed": VERDICT},
                          ("fact", "operator", "value", "current", "effect_observed"))
FACT_SCHEMA = _object({"type": {"enum": ["integer", "boolean", "string"]},
    "unit": {"type": "string", "maxLength": 16}, "scope": ID,
    "min": {"type": "integer"}, "max": {"type": "integer"}}, ("type", "scope"))
FACT_MAP = {"type": "object", "maxProperties": 40, "additionalProperties": SCALAR}
TRANSITION = _object({"kind": {"const": "automatic_field_change"}, "step": INTEGER,
    "entity_id": ID, "definition_ref": ID, "before": FACT_MAP, "after": FACT_MAP},
    ("kind", "step", "entity_id", "definition_ref", "before", "after"))
OUTCOME = _object({"id": ID, "entity_id": ID, "entity_version": INTEGER,
    "action": {"type": "string", "maxLength": 80}, "at": {"type": "number", "minimum": 0}},
    ("id", "entity_id", "entity_version", "action", "at"))
OUTCOME_COVERAGE = _object({"status": {"enum": ["partial", "unavailable"]}, "reason": ID,
    "content_id": ID, "content_version": INTEGER, "latest_sequence": INTEGER,
    "requested_before": INTEGER, "scanned_count": INTEGER, "next_before": INTEGER,
    "has_more": {"type": "boolean"}, "missing_sequences": _array(INTEGER, MAX_OUTCOMES)}, ("status", "reason"))
CONTEXT = _object({"world_instance": ID, "world_id": {"const": "shared-grove"},
    "world_version": {"type": "integer", "minimum": 1}, "viewer_role_id": ID, "snapshot_seq": INTEGER},
    ("world_instance", "world_id", "world_version", "viewer_role_id", "snapshot_seq"))
STAGE = _object({"status": ID, "meaning": {"type": "string", "maxLength": 240},
                 "published_step": INTEGER, "reason": ID}, ("status", "meaning"))
REQUEST = _object({"function_id": ID, "arguments": {"type": "object", "maxProperties": 4}},
                  ("function_id", "arguments"))
DEPLOYMENT = _object({"publicly_placed": {"type": "boolean"}, "admitted": {"type": "boolean"},
    "activity_state": ID, "active_this_step": {"type": "boolean"},
    "ordinary_rules_enabled": {"type": "boolean"}, "public_event_input_available": {"type": "boolean"},
    "mechanical_solver_eligible": {"type": "boolean"}, "mechanical_blockers": _array(ID, 8)})
OUTPUT_SCHEMA = _object({"entity_id": ID, "definition_ref": ID, "type_id": ID,
    "entity_version": {"type": "integer", "minimum": 1}, "observed_step": INTEGER, "agent_context": CONTEXT,
    "stages": _object({name: STAGE for name in ("authored_intent", "publication", "instance_creation", "action_commit")},
                      ("authored_intent", "publication", "instance_creation", "action_commit")),
    "fact_catalog": {"type": "object", "maxProperties": 40, "additionalProperties": FACT_SCHEMA},
    "predicates": _array(RESULT_PREDICATE, MAX_PREDICATES),
    "history": _object({"scope": {"const": "last_change_and_bounded_public_outcomes"}, "complete": {"const": False},
        "after_step": {"type": ["integer", "null"], "minimum": 0},
        "coverage": _object({"automatic_field_change": {"const": "last_saved_change_only"},
            "transition_status": ID, "outcomes": OUTCOME_COVERAGE}, ("automatic_field_change", "transition_status", "outcomes")),
        "outcomes": _array(OUTCOME, MAX_OUTCOMES), "transition_records": _array(TRANSITION, 1),
        "mechanical_change_step": INTEGER, "last_reaction_step": INTEGER},
        ("scope", "complete", "after_step", "coverage", "outcomes", "transition_records")),
    "process": _object({"status": ID, "steps": INTEGER, "last_tick": INTEGER,
        "blocked": {"type": ["boolean", "null"]}, "blocker_codes": _array(ID, 8), "deployment": DEPLOYMENT},
        ("status", "blocked", "blocker_codes", "deployment")),
    "read_requests": _array(REQUEST, 4)},
    ("entity_id", "definition_ref", "type_id", "entity_version", "observed_step", "agent_context",
     "stages", "fact_catalog", "predicates", "history", "process", "read_requests"))

_COMPARISONS = {"eq": operator.eq, "ne": operator.ne, "lt": operator.lt,
                "lte": operator.le, "gt": operator.gt, "gte": operator.ge}
_TYPES = {"integer": int, "boolean": bool, "string": str}
ERROR_CODES = ("unknown_fact", "predicate_type_mismatch", "predicate_range_mismatch",
               "predicate_operator_mismatch", "definition_reference_mismatch")
ERROR_DIAGNOSTIC_SCHEMA = _object({"code": {"enum": list(ERROR_CODES)},
    "path": {"type": "string", "maxLength": 40,
             "pattern": r"^(?:definition_ref|predicates\[[0-7]\]\.(?:fact|value|operator))$"},
    "predicate_index": {"type": "integer", "minimum": 0, "maximum": MAX_PREDICATES - 1},
    "supplied_fact": PREDICATE["properties"]["fact"],
    "supplied_operator": PREDICATE["properties"]["operator"],
    "supplied_type": {"enum": list(_TYPES)}, "expected_type": {"enum": list(_TYPES)},
    "min": {"type": "integer"}, "max": {"type": "integer"},
    "allowed_operators": _array(PREDICATE["properties"]["operator"], 6)}, ("code", "path"))


def _error(code, message, *, path, entity_id, reference=None, index=None,
           predicate=None, schema=None, catalog=None):
    """Return declarations and discovery requests, never current fact answers."""
    diagnostic = {"code": code, "path": path}
    if index is not None:
        diagnostic["predicate_index"] = index
    if predicate is not None:
        diagnostic.update(supplied_fact=predicate["fact"], supplied_operator=predicate["operator"])
    if schema is not None:
        diagnostic.update(expected_type=schema["type"],
                          supplied_type=next(name for name, kind in _TYPES.items()
                                             if type(predicate["value"]) is kind),
                          allowed_operators=list(_COMPARISONS) if schema["type"] == "integer" else ["eq", "ne"])
        diagnostic.update({name: schema[name] for name in ("min", "max") if name in schema})
    reads = [{"function_id": "grove.inspect", "arguments": {"entity_id": entity_id}}]
    if reference is not None:
        reads = [{"function_id": "grove.definition_read", "arguments": {"definition_ref": reference}},
                 # predicates is optional and defaults to []; do not repeat any
                 # rejected predicate or manufacture a replacement expression.
                 {"function_id": "grove.effect_evidence",
                  "arguments": {"entity_id": entity_id, "definition_ref": reference}}]
    details = {"code": code, "effect_evidence_diagnostic": diagnostic, "read_requests": reads}
    if catalog is not None:
        candidate = {**details, "fact_catalog": deepcopy(catalog)}
        if len(json.dumps(candidate, ensure_ascii=False, separators=(",", ":")).encode()) <= MAX_ERROR_DETAILS_BYTES:
            details = candidate
    raise RuleViolation(message, details=details, recovery="fix_request")


def _catalog(spec):
    result = {name: {"type": declaration.type, "scope": "registered_current_fact",
                     **({"unit": declaration.unit} if declaration.unit is not None else {})}
              for name, declaration in PUBLIC_FACTS.items()}
    for name, declaration in exported_interface(spec).items():
        result[name] = {**deepcopy(declaration), "scope": "explicit_authored_export"}
    for name in result:
        if name in {"mechanical.position", "mechanical.speed", "mechanical.angle", "mechanical.status", "mechanical.available_torque"}:
            result[name]["scope"] = "stored_mechanical_output_not_live_motion"
    return result


def _typed(value, schema):
    return (type(value) is _TYPES[schema["type"]]
            and (schema["type"] != "string" or len(value) <= 80)
            and (schema["type"] != "integer" or schema.get("min", value) <= value <= schema.get("max", value)))


def _transition(ctx, entity, reference, catalog, observed_step, hidden):
    if hidden:
        return None, "program_state_hidden"
    record = ctx.get_state("grove.creative.field_changes", entity["id"])
    if record is None:
        return None, "no_retained_transition"
    step = record.get("step") if isinstance(record, dict) else None
    if type(step) is not int or not 0 <= step <= observed_step:
        return None, "transition_step_unverified"
    before, after = record.get("before"), record.get("after")
    if not isinstance(before, dict) or not isinstance(after, dict):
        return None, "transition_shape_unverified"
    old, new = {}, {}
    for field in before.keys() & after.keys():
        name = "authored." + field
        schema = catalog.get(name)
        if schema and _typed(before[field], schema) and _typed(after[field], schema) and before[field] != after[field]:
            old[name], new[name] = before[field], after[field]
    if not old:
        return None, "no_typed_export_transition"
    return {"kind": "automatic_field_change", "step": step, "entity_id": entity["id"],
            "definition_ref": reference, "before": old, "after": new}, "partial"


def _outcomes(ctx, player, entity, before, hidden):
    from . import generation
    unavailable = {"status": "unavailable", "reason": "no_current_public_region_history"}
    if hidden:
        return [], {**unavailable, "reason": "program_state_hidden"}, None
    location = m.public_location(ctx, entity)
    region = generation._region_key(player["x"], player["y"])
    if location is None or generation._region_key(*location) != region:
        return [], unavailable, None
    root = ctx.get_state_record(generation.CONTENTS, region)
    if not root["exists"]:
        return [], unavailable, None
    content = root["value"]
    latest = content["outcome_count"]
    first = min(latest, latest if before is None else before)
    stop = max(0, first - MAX_OUTCOMES)
    rows, missing = [], []
    for sequence in range(first, stop, -1):
        row = ctx.get_state(generation.OUTCOMES, f"{content['id']}:outcome:{sequence}")
        if row is None:
            missing.append(sequence)
        elif row.get("entity_id") == entity["id"]:
            rows.append({key: deepcopy(row[key]) for key in ("id", "entity_id", "entity_version", "action", "at")})
    coverage = {"status": "partial", "reason": "bounded_public_action_records_not_typed_effect_proof",
        "content_id": content["id"], "content_version": root["version"], "latest_sequence": latest,
        "requested_before": first, "scanned_count": first - stop, "next_before": stop,
        "has_more": stop > 0, "missing_sequences": missing}
    request = {"function_id": "grove.content_history", "arguments": {"content_id": content["id"],
        "cursor": {"outcome_before": stop}}}
    return list(reversed(rows)), coverage, request


def _predicate(predicate, catalog, values, transition, after_step, hidden, *, index, entity_id, reference):
    name, comparison, expected = predicate["fact"], predicate["operator"], predicate["value"]
    schema = catalog.get(name)
    if schema is None:
        _error("unknown_fact", "证据谓词只接受注册事实或此固定类型显式导出的authored字段。",
               path=f"predicates[{index}].fact", index=index, predicate=predicate,
               entity_id=entity_id, reference=reference, catalog=catalog)
    if type(expected) is not _TYPES[schema["type"]]:
        _error("predicate_type_mismatch", "证据谓词值须匹配声明类型；布尔值不能代替整数。",
               path=f"predicates[{index}].value", index=index, predicate=predicate, schema=schema,
               entity_id=entity_id, reference=reference)
    if not _typed(expected, schema):
        _error("predicate_range_mismatch", "证据谓词值须位于声明范围内。",
               path=f"predicates[{index}].value", index=index, predicate=predicate, schema=schema,
               entity_id=entity_id, reference=reference)
    if comparison not in {"eq", "ne"} and schema["type"] != "integer":
        _error("predicate_operator_mismatch", "此声明类型只接受eq或ne比较。",
               path=f"predicates[{index}].operator", index=index, predicate=predicate, schema=schema,
               entity_id=entity_id, reference=reference)
    current = {"known": False, "satisfied": None,
               "reason": "program_state_hidden" if hidden else "fact_not_currently_available"}
    if name in values and _typed(values[name], schema):
        current = {"known": True, "actual": values[name], "satisfied": _COMPARISONS[comparison](values[name], expected)}
    verdict = {"status": "unknown", "reason": "no_matching_retained_transition", "witnesses": []}
    if after_step is None:
        verdict["reason"] = "after_step_required_for_effect_window"
    elif hidden:
        verdict["reason"] = "program_state_hidden"
    elif transition is not None and after_step < transition["step"] and name in transition["after"]:
        actual = transition["after"][name]
        if _COMPARISONS[comparison](actual, expected):
            verdict = {"status": "observed", "reason": "typed_transition_in_requested_window", "witnesses": [{
                "kind": transition["kind"], "step": transition["step"], "entity_id": transition["entity_id"],
                "definition_ref": transition["definition_ref"], "fact": name,
                "before": transition["before"][name], "after": actual, "causal_attribution": "not_established"}]}
    return {**deepcopy(predicate), "current": current, "effect_observed": verdict}


def read(ctx, args, *, world_version):
    """Read one authorized snapshot; world_version is bound by installed code."""
    player = m.get_player(ctx)
    entity = m.get_entity(ctx, args["entity_id"])
    action_contracts.accessible(ctx, player, entity, near=False)
    binding = entity.get("state", {}).get("program", {})
    reference = args["definition_ref"]
    if binding.get("definition_ref") != reference:
        _error("definition_reference_mismatch", "证据读取须匹配同一物件固定的definition_ref；名称和其他实例不能替代。",
               path="definition_ref", entity_id=entity["id"])
    _, spec = definitions.instance_definition(ctx, entity)
    publication = definitions.read_definition(ctx, reference)
    observed_step = ctx.get_state(m.META, "clock", {}).get("sequence", 0)
    hidden = entity.get("owner") != player["id"] and "container" in entity.get("components", {}) and not entity["state"].get("open", True)
    catalog = _catalog(spec)
    objects = world_facts.freeze(ctx, {entity["id"]: entity})
    values = world_facts.facts(entity, objects, external=entity.get("owner") != player["id"], ctx=ctx)
    if hidden:
        # Existing public physics may remain visible; no authored export,
        # process state or retained private field value crosses a closed vessel.
        values = {name: value for name, value in values.items() if not name.startswith(("authored.", "mechanical."))}
    transition, transition_status = _transition(ctx, entity, reference, catalog, observed_step, hidden)
    after_step = args.get("after_step")
    predicates = [_predicate(item, catalog, values, transition, after_step, hidden,
                             index=index, entity_id=entity["id"], reference=reference)
                  for index, item in enumerate(args.get("predicates", []))]
    outcomes, coverage, history_request = _outcomes(ctx, player, entity, args.get("outcome_before"), hidden)
    history = {"scope": "last_change_and_bounded_public_outcomes", "complete": False, "after_step": after_step,
        "coverage": {"automatic_field_change": "last_saved_change_only", "transition_status": transition_status, "outcomes": coverage},
        "outcomes": outcomes, "transition_records": [transition] if transition is not None else []}
    process = {"status": "unknown", "blocked": None, "blocker_codes": [], "deployment": {}}
    if not hidden:
        stored = binding.get("process", {})
        codes = []
        if stored.get("status") == "error":
            codes.append("program_error")
        if stored.get("blocked_reason") or stored.get("barrier_blocked_reason") or stored.get("blocked_rules"):
            codes.append("unavailable_fact_or_port")
        if stored.get("event_gap"):
            codes.append("public_event_gap")
        process.update(status=stored.get("status", "unknown"), blocked=bool(codes), blocker_codes=codes)
        for key in ("steps", "last_tick"):
            if type(stored.get(key)) is int and stored[key] >= 0:
                process[key] = stored[key]
        deployment = observations.program_observation(ctx, entity, player=player).get("deployment", {})
        process["deployment"] = {name: deepcopy(value) for name, value in deployment.items() if name in DEPLOYMENT["properties"]}
        marker = binding.get("mechanical_last_solved", {})
        if isinstance(marker, dict) and type(marker.get("step")) is int and 0 <= marker["step"] <= observed_step:
            history["mechanical_change_step"] = marker["step"]
        if stored.get("last_reactions") and type(stored.get("last_tick")) is int and 0 <= stored["last_tick"] <= observed_step:
            history["last_reaction_step"] = stored["last_tick"]
    requests = [{"function_id": "grove.inspect", "arguments": {"entity_id": entity["id"]}},
                {"function_id": "grove.definition_read", "arguments": {"definition_ref": reference}}]
    if history_request is not None:
        requests.append(history_request)
    if "operation_id" in args:
        requests.append({"function_id": "world.get_receipt", "arguments": {"operation_id": args["operation_id"]}})
    stages = {
        "authored_intent": {"status": "declared", "meaning": "An immutable authored declaration, not a private goal or observed physical effect."},
        "publication": {"status": "published", "meaning": "This exact immutable definition exists; publication creates no physical instance or effect."},
        "instance_creation": {"status": "exists", "meaning": "This current instance pins this exact definition; no creation operation or goal verdict is inferred."},
        "action_commit": {"status": "unknown", "reason": "receipt_is_separate_evidence", "meaning": "Read your original Receipt separately; a committed action is not proof of a later effect."}}
    published_step = publication.get("published_tick")
    if type(published_step) is int and published_step >= 0:
        stages["publication"]["published_step"] = published_step
    return FunctionOutcome({"entity_id": entity["id"], "definition_ref": reference, "type_id": binding["type_id"],
        "entity_version": entity["version"], "observed_step": observed_step,
        "agent_context": {"world_instance": ctx.universe, "world_id": "shared-grove", "world_version": world_version,
                          "viewer_role_id": ctx.actor_role_id, "snapshot_seq": ctx.read_snapshot_seq},
        "stages": stages, "fact_catalog": catalog, "predicates": predicates, "history": history,
        "process": process, "read_requests": requests})


def make_handler(world_version):
    if type(world_version) is not int or world_version < 1:
        raise ValueError("world_version must identify the installed world code")
    def handler(ctx, args):
        return read(ctx, args, world_version=world_version)
    return handler
