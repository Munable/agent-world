"""Published, immutable bounded programs executed by ordinary world transactions.

Definitions describe conditions and effects; they neither instantiate matter nor
grant consent. All progress uses the world's logical step, including deadlines.
"""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json
import secrets

from agent_world import FunctionOutcome, FunctionSpec, StateRule
from agent_world.errors import PermissionDenied, RuleViolation, StateConflict
from agent_world.simulation import matter as sm
from agent_world.simulation.declarations import declaration_semantics, has_event, phase_applications
from agent_world.simulation.programs import compile_program, evaluate, plan_reaction, program_material_semantics, solve_mechanical
from agent_world.simulation.facts import FactUnavailable, PUBLIC_FACTS, snapshot_facts
from agent_world.world_streams import StreamEvent

from . import model as m, commit, activity, publication
from .model import public_location as _public_location
from .commit import public_event, physical_contract_failure
from .events import bounded_record, RECORDS, MAX_RECORDS, MAX_EVENTS, MAX_RECORD_BYTES, MAX_EVENT_BYTES, MAX_EVENT_JOURNAL_BYTES
from .definitions import BODY_MATERIALS, MAX_CUSTOM_MATERIALS, REGISTRY, registry_for, DEFINITIONS, instance_definition as _type, material_identity as _material, contained_quantities as _quantities, reaction_available_quantities

VERSIONS = "grove.creative.versions"
CONTROL = "grove.creative.control"
ACCOUNTING = "grove.creative.accounting"
MAX_INSTANCES = 32
MAX_LINKS = 8
PROGRAM_COMPONENTS = ("structure", "container", "combustion", "absorbent", "buoyant", "attachment")
MAX_COMMIT_BYTES = 2 * 1024 * 1024
MAX_COMMIT_CHANGES = 512
ACCOUNT_HISTORY_BYTES = 512
ID = {"type": "string", "minLength": 1, "maxLength": 128}
REF = {"type": "string", "minLength": 1, "maxLength": 128}
VERSION = {"type": "integer", "minimum": 1}
EMPTY = {"type": "object", "additionalProperties": False}
DELEGATION = {"type": "string", "minLength": 16, "maxLength": 100}
ACTION_ARGS = {"type": "object", "additionalProperties": False,
    "required": ["action", "target_id", "expected_target_version", "expected_player_version"],
    "properties": {"action": {"type": "string", "minLength": 1, "maxLength": 80},
        "target_id": ID, "expected_target_version": VERSION, "expected_player_version": VERSION,
        "item_id": ID, "expected_item_version": VERSION, "other_id": ID, "expected_other_version": VERSION,
        "amount": {"type": "integer", "minimum": 1, "maximum": 10000},
        "material": {"type": "string", "minLength": 1, "maxLength": 180}, "terms_version": VERSION, "lot_id": ID,
        "params": {"type": "object", "maxProperties": 16}, "port": {"type": "string", "minLength": 1, "maxLength": 48}, "delegation": DELEGATION}}


def _save(ctx, scope, key, value):
    rec = ctx.get_state_record(scope, key)
    return ctx.set_state(scope, key, value, expected_version=rec["version"])




def _check_instance_capacity(ctx, ids):
    """Reserve a future full physical step before admitting a new instance.

    Definitions are read-only during a step. Only the admitted types' possible
    results, distinct material accounts and bounded observer histories add to
    the existing physical reservation; sharing a material account counts once.
    """
    from . import activity as physical
    from .definitions import MAX_CUSTOM_MATERIALS
    from .generation import MAX_CONTENT_BYTES, MAX_OUTCOME_BYTES
    from .generation import MAX_OUTCOMES_PER_STEP
    from . import contact

    changes = (sm.MAX_FRAME_OBJECTS + physical.MAX_FRAME_CHUNKS + 4
               + physical.MAX_PROCESSES + physical.MAX_FRAME_META)
    size = (sm.MAX_FRAME_OBJECTS * 2 * sm.MAX_ENTITY_BYTES
            + physical.MAX_FRAME_CHUNKS * 2 * m.MAX_CHUNK_BYTES
            + 4 * 2 * m.MAX_META_BYTES
            + physical.MAX_PROCESSES * 2 * physical.MAX_PROCESS_BYTES
            + physical.MAX_FRAME_META * 2 * m.MAX_META_BYTES)
    # Public content roots/outcomes follow the physical flush. Custom burning
    # lots have their own accounting rows rather than a shared built-in index.
    physical_facts = sm.MAX_FRAME_OBJECTS + physical.MAX_EVENTS
    total_facts = physical_facts
    burning_accounts = physical.MAX_BURNING_ENTITIES * MAX_CUSTOM_MATERIALS
    changes += physical.MAX_FRAME_CHUNKS + physical_facts + burning_accounts + 2
    journal = ctx.get_state(m.META, "creative_events", {"sequence": 0, "events": []})
    size += (physical.MAX_FRAME_CHUNKS * 2 * MAX_CONTENT_BYTES
             + physical_facts * MAX_OUTCOME_BYTES
             + burning_accounts * ACCOUNT_HISTORY_BYTES
             + max(sm.encoded_size(journal), MAX_EVENT_JOURNAL_BYTES) + MAX_EVENT_JOURNAL_BYTES
             + 2 * m.MAX_META_BYTES)
    accounts = set()
    builtin_accounts = {category: set(activity.meta_get(ctx, "matter_accounting", {}).get(category, {}))
                        for category in ("sinks", "sources")}
    authored_outputs = {}
    admitted = {}
    for eid in ids:
        entity = m.get_entity(ctx, eid, required=False)
        if not entity or entity["lifecycle"] != "intact" or entity["state"].get("retired"):
            continue
        _, spec = _type(ctx, entity)
        if spec is None:
            continue
        admitted[eid] = entity
        reactions = False
        for rule in spec["rules"]:
            for effect in rule["effects"]:
                if effect["op"] != "react":
                    continue
                reactions = True
                authored_outputs.setdefault(eid, set()).update(_material(entity, symbol) for symbol in effect["outputs"])
                for category, amounts in (("sinks", effect["inputs"]), ("sources", effect["outputs"])):
                    for symbol in amounts:
                        identity = _material(entity, symbol)
                        if "#" in identity:
                            accounts.add((category, identity, "creative_reaction"))
        contract = int("contract" in spec)
        facts = max(int(reactions) + int("mechanical" in spec) + contract,
                    int(bool(spec["rules"]) or "barrier" in spec) + contract)
        changes += facts
        total_facts += facts
        size += facts * MAX_OUTCOME_BYTES
        if "recorder" in spec:
            record = ctx.get_state(RECORDS, eid, {"history": [], "reads": []})
            changes += 1
            size += max(sm.encoded_size(record), MAX_RECORD_BYTES) + MAX_RECORD_BYTES
    objects, _ = physical.physical_closure(ctx, list(admitted.values()))
    contact_reservations = contact.reserve_forest(ctx, objects, authored_outputs=authored_outputs)
    for entity in objects.values():
        rows = contact_reservations[entity["id"]]
        if not rows:
            continue
        # One container fact carries all of its at-most-eight applications.
        total_facts += 1
        changes += 1
        size += MAX_OUTCOME_BYTES
        for row in rows:
            for category, group in (("sinks", "inputs"), ("sources", "outputs")):
                for identity in row["reaction"][group]:
                    if "#" in identity:
                        accounts.add((category, identity, "contact_reaction"))
                    else:
                        builtin_accounts[category].add(identity + ":contact_reaction")
    changes += len(accounts)
    size += len(accounts) * ACCOUNT_HISTORY_BYTES
    if (any(len(keys) > 32 for keys in builtin_accounts.values()) or total_facts > MAX_OUTCOMES_PER_STEP
            or changes > MAX_COMMIT_CHANGES or size > MAX_COMMIT_BYTES):
        raise RuleViolation("活动物件组合超过完整物理步的提交容量；现有物件继续运行，本次入料或创建未提交。")


def step(ctx):
    return ctx.get_state(m.META, "clock", {}).get("sequence", 0)


def _member(ctx, args=None):
    return m.get_player(ctx, required=False) is not None


def _control_public(value, version):
    return {**{k: deepcopy(v) for k, v in value.items() if k != "delegation"},
            "version": version, "delegation_active": value.get("status") == "active"}


def control_status(ctx, args):
    rec = ctx.get_state_record(CONTROL, ctx.actor_role_id)
    return FunctionOutcome(_control_public(rec["value"] if rec["exists"] else {"status": "idle", "goal": "", "actions_count": 0}, rec["version"]))


def control_grant(ctx, args):
    value = {"status": "active", "goal": args["goal"], "max_actions": args["max_actions"],
             "actions_count": 0, "delegation": secrets.token_urlsafe(32), "started_tick": step(ctx)}
    version = _save(ctx, CONTROL, ctx.actor_role_id, value)
    return FunctionOutcome({**_control_public(value, version), "delegation": value["delegation"]})


def control_revoke(ctx, args):
    rec = ctx.get_state_record(CONTROL, ctx.actor_role_id)
    value = deepcopy(rec["value"]) if rec["exists"] else {"goal": "", "actions_count": 0}
    value.update(status="revoked", delegation="")
    return FunctionOutcome(_control_public(value, _save(ctx, CONTROL, ctx.actor_role_id, value)))


def check_delegation(ctx, args):
    """The connector supplies a grant on every write. Manual writes reclaim.

    A bearer proves the user's identity, not that a client is human or a model.
    This is a revocable execution boundary, not a new Agent identity.
    """
    rec = ctx.get_state_record(CONTROL, ctx.actor_role_id)
    value = deepcopy(rec["value"]) if rec["exists"] else {"status": "idle"}
    token = args.get("delegation")
    if token is None:
        if value.get("status") == "active":
            value.update(status="reclaimed", delegation="")
            _save(ctx, CONTROL, ctx.actor_role_id, value)
        return
    if value.get("status") != "active" or not secrets.compare_digest(token, value.get("delegation", "")):
        raise PermissionDenied("目标委托已收回、耗尽或被替换；后续动作没有执行。")
    value["actions_count"] += 1
    if value["actions_count"] >= value["max_actions"]:
        value["status"] = "exhausted"
    _save(ctx, CONTROL, ctx.actor_role_id, value)


def definition_publish(ctx, args):
    check_delegation(ctx, args)
    try:
        proposed = deepcopy(args["definition"])
        resolved = {}
        if isinstance(proposed, dict) and isinstance(proposed.get("material_imports"), dict):
            for alias, binding in proposed["material_imports"].items():
                if not isinstance(binding, dict) or set(binding) != {"definition_ref", "symbol"}:
                    continue  # The compiler reports the exact invalid shape.
                if not isinstance(binding["definition_ref"], str) or not isinstance(binding["symbol"], str):
                    continue
                reference, symbol, material = _resolve_material(ctx, binding["definition_ref"], binding["symbol"])
                proposed["material_imports"][alias] = {"definition_ref": reference, "symbol": symbol}
                resolved[alias] = material
        definition = compile_program(proposed, materials=REGISTRY.materials, body_materials=BODY_MATERIALS,
                                     max_custom_materials=MAX_CUSTOM_MATERIALS, imported_materials=resolved,
                                     fact_catalog={key: fact.type for key, fact in PUBLIC_FACTS.items()},
                                     component_catalog={key: REGISTRY.components[key] for key in PROGRAM_COMPONENTS})
    except ValueError as exc:
        raise RuleViolation("定义未发表，世界没有改变：" + str(exc)) from exc
    encoded = json.dumps(definition, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    if len(encoded) > 24576:
        raise RuleViolation("规则包超过24KiB，请拆成更小的实际能力包。")
    digest = hashlib.sha256(encoded).hexdigest()
    ref = "program_" + hashlib.sha256((ctx.actor_role_id + ":" + digest).encode()).hexdigest()
    semantics = program_material_semantics(definition, ref, materials=REGISTRY.materials)
    version_key = hashlib.sha256((ctx.actor_role_id + ":" + definition["key"] + ":" + str(definition["version"])).encode()).hexdigest()
    previous = ctx.get_state(VERSIONS, version_key)
    if previous:
        if previous["definition_ref"] != ref:
            raise StateConflict("已发布的同作者／名称／版本不能覆盖；请递增version。旧物件仍引用原版本。")
        return FunctionOutcome({"definition_ref": ref, "definition": definition, "already_published": True,
                                "material_semantics": semantics, "declaration_semantics": declaration_semantics(definition)})
    publication.publish(ctx, ref, version_key, {"definition": definition, "author": ctx.actor_role_id, "hash": digest,
                                               "published_tick": step(ctx), "published_at": ctx.now})
    return FunctionOutcome({"definition_ref": ref, "definition": definition, "already_published": False,
                            "material_semantics": semantics, "declaration_semantics": declaration_semantics(definition),
                            "instantiate": "cost_u: grove.fabrication_options then grove.fabricate with actual lot sources; cost: grove.content_submit with existing opportunity and builtin piece costs"})


def definition_read(ctx, args):
    value = ctx.get_state(DEFINITIONS, args["definition_ref"])
    if not value:
        raise RuleViolation("规则版本不存在；先发布并使用返回的definition_ref。")
    return FunctionOutcome({"definition_ref": args["definition_ref"], **value,
        "material_semantics": program_material_semantics(value["definition"], args["definition_ref"], materials=REGISTRY.materials),
        "declaration_semantics": declaration_semantics(value["definition"])})




def _resolve_material(ctx, reference, symbol):
    """Pin imports to the original immutable definition, never a display name."""
    record = ctx.get_state(DEFINITIONS, reference)
    if not record:
        raise RuleViolation("导入材料引用不存在；使用已发表definition_ref与材料symbol。")
    pack = record["definition"]
    if symbol in pack["materials"]:
        return reference, symbol, deepcopy(pack["materials"][symbol])
    binding = pack.get("material_imports", {}).get(symbol)
    if binding:
        # Publication canonicalizes every import to its original material.
        source = ctx.get_state(DEFINITIONS, binding["definition_ref"])
        if source and binding["symbol"] in source["definition"]["materials"]:
            return binding["definition_ref"], binding["symbol"], deepcopy(source["definition"]["materials"][binding["symbol"]])
    raise RuleViolation("导入symbol不是该不可变规则包声明或导入的材料。")


def object_definition(ctx, spec, *, formation=False):
    pack = definition_read(ctx, {"definition_ref": spec["definition_ref"]}).result["definition"]
    if spec["type_id"] not in pack["types"]:
        raise RuleViolation("type_id不在指定规则版本中。")
    kind = pack["types"][spec["type_id"]]
    fields = {key: field["default"] for key, field in kind["fields"].items()}
    materials = {spec["definition_ref"] + "#" + symbol: {"name": definition["name"], "load_per_u": definition["load_per_u"],
                  "piece_quantity": 4, "combustible": definition["combustible"], "liquid": definition["liquid"]}
                 for symbol, definition in pack["materials"].items()}
    aliases = {}
    for alias, imported in pack.get("material_imports", {}).items():
        reference, symbol, material = _resolve_material(ctx, imported["definition_ref"], imported["symbol"])
        identity = reference + "#" + symbol
        aliases[alias] = identity
        materials[identity] = {"name": material["name"], "load_per_u": material["load_per_u"],
                              "piece_quantity": 4, "combustible": material["combustible"], "liquid": material["liquid"]}
    binding = {"definition_ref": spec["definition_ref"], "type_id": spec["type_id"], "fields": fields,
               "materials": materials, "event_cursor": ctx.get_state(m.META, "creative_events", {"sequence": 0})["sequence"], "connections": [], "process": {"status": "idle", "steps": 0, "last_tick": step(ctx)}}
    if aliases:
        binding["material_aliases"] = aliases
    if "ports" in kind:
        binding["port_bindings"] = {}
        binding["port_binding_version"] = 0
    if "mechanical" in kind:
        binding["mechanical"] = {"position": 0, "angle": 0, "speed": 0, "energy": 0,
                                 "enabled": False, "status": "no_source", "available_torque": 0}
    if "recorder" in kind:
        binding["recorder"] = {"active": False, "started_tick": None, "until_tick": None, "recorded_count": 0}
    if "contract" in kind:
        binding["contract"] = {"status": "unoffered", "terms_version": 0, "parties": [], "accepted": [],
                               "deadline_tick": None, "delivered": 0, "quantity": kind["contract"]["quantity"],
                               "material": _material({"state": {"program": binding}}, kind["contract"]["material"]),
                               "beneficiary": None, "released": 0}
    if "cost_u" in kind and not formation:
        raise RuleViolation("此类型按真实u批次成型；先读取grove.fabrication_options，再提交grove.fabricate。区域份数库存不能代替原材料。")
    matter = ([] if formation else [{"id": "body_" + material, "material": material,
               "quantity": amount * REGISTRY.materials[material].piece_quantity,
               "slot": "body", "role": "structural", "burning": False} for material, amount in kind["cost"].items() if amount])
    components = {"structure": {"max_integrity": 100, "integrity": 100, "support_limit": 1000, "overload_rate": 1}}
    component_defaults = {"structure": components["structure"],
        "container": {"slots": 4, "liquid_capacity": 0, "open": True},
        "absorbent": {"capacity": 0, "rate": 2}, "buoyant": {"capacity": 0},
        "combustion": {"rate": 2, "spread": True},
        "attachment": {"kind": "part", "fits": "container", "max_children": 16}}
    for name, parameters in kind.get("components", {}).items():
        components[name] = {**component_defaults[name], **deepcopy(parameters)}
    capabilities = ["transport"]
    if "container_capacity" in kind or "contract" in kind or any(e["op"] == "react" for r in kind["rules"] for e in r["effects"]):
        capacity = kind.get("container_capacity", 100)
        binding["mixture_capacity"] = capacity
        components.setdefault("container", {"slots": 4, "liquid_capacity": capacity, "open": True})
        if "container_capacity" in kind:
            components["container"]["liquid_capacity"] = capacity
        capabilities.append("container")
    elif "container" in components:
        capabilities.append("container")
        # Component-only vessels follow ordinary container policy: object
        # slots, liquid volume and bounded solid lots are separate capacities.
    if "barrier" in kind:
        try:
            active = bool(evaluate(kind["barrier"], _context({"state": {"program": binding}, "matter": matter,
                "components": components, "x": spec.get("x"), "y": spec.get("y")}, ctx=ctx, definition=pack)))
        except FactUnavailable:
            active = True
            binding["process"].update(status="waiting", blocked_reason="unavailable_fact_or_port")
        components["barrier"] = {"active": active}
        if active:
            capabilities.append("blocking")
    body = _material({"state": {"program": binding}}, kind["body_material"])
    capabilities += [tag for component, tag in (("absorbent", "wettable"), ("buoyant", "floating"), ("combustion", "flammable"))
                     if component in components and tag not in capabilities]
    return {"name": kind["name"], "material": body, "capabilities": capabilities,
            "state": {"kind": "authored", "program": binding}, "matter": matter, "components": components,
            "composition": {"material": body, "construction": "bundle", "size": 1},
            "material_cost": deepcopy(kind.get("cost", {})), "material_units": sum(kind.get("cost", {}).values()),
            "water_capacity": components.get("container", {}).get("liquid_capacity", 0)}


def register_instance(ctx, entity):
    if not entity["state"].get("program", {}).get("definition_ref"):
        return
    ids = activity.meta_get(ctx, "creative_instances", [])
    if entity["id"] not in ids:
        if len(ids) >= MAX_INSTANCES:
            raise RuleViolation("同时最多32个声明程序物件；已耗尽／退役物件会在下一步释放执行位。")
        proposed = [*ids, entity["id"]]
        _check_instance_capacity(ctx, proposed)
        activity.meta_set(ctx, "creative_instances", proposed)
        activity.physical_closure(ctx)


def _bound_ids(value):
    """Literal object bindings are part of the immutable program, never a query."""
    if isinstance(value, dict):
        if set(value) == {"object"}:
            return {value["object"]["id"]} if "id" in value["object"] else set()
        return set().union(*(_bound_ids(child) for child in value.values()))
    if isinstance(value, list):
        return set().union(*(_bound_ids(child) for child in value))
    return set()


def _context(entity, params=None, event=None, *, ctx=None, objects=None, definition=None):
    from . import world_facts
    program = entity["state"]["program"]
    observed = {}
    if ctx is not None:
        definition = definition if definition is not None else _type(ctx, entity)[0]
        if objects is None:
            anchor = entity if entity.get("id") else {"id": "candidate", **entity}
            objects = world_facts.freeze(ctx, {anchor["id"]: anchor})
        location = world_facts.public_location(entity, objects)
        for eid in sorted(world_facts.bound_ids(entity, definition)):
            target = objects.get(eid)
            target_location = world_facts.public_location(target, objects) if target else None
            if (location is None or None in location or target_location is None or
                    max(abs(location[0]-target_location[0]), abs(location[1]-target_location[1])) > 8):
                continue
            observed[eid] = world_facts.facts(target, objects, external=True)
    contract = program.get("contract", {})
    physical = world_facts.facts((objects or {}).get(entity.get("id"), entity), objects or {})
    return {"fields": program["fields"], "params": params or {}, "quantities": _quantities(entity), "objects": observed,
            "physical": {key.removeprefix("physical."): value for key, value in physical.items() if key.startswith("physical.")},
            "ports": deepcopy(program.get("port_bindings", {})),
            "contract": {"status": contract.get("status", "none"), "started_event_sequence": contract.get("started_event_sequence", 0),
                         "deadline_tick": contract.get("deadline_tick") or 0, "delivered": contract.get("delivered", 0), "quantity": contract.get("quantity", 0)},
            "mechanical": program.get("mechanical", {"position": 0, "angle": 0, "speed": 0, "energy": 0, "status": "none", "available_torque": 0}),
            "event": {"kind": "none", "action": "", "entity_id": "", "actor": "", "mechanical_position": 0, "passable": False, **(event or {})}}


def _take(entity, material, amount, *, slots=("contained",)):
    try:
        sm.take_material(entity, material, amount, slots=slots, include_burning=False)
    except ValueError as exc:
        raise RuleViolation("实际可用材料不足；没有生成或扣除任何产物。") from exc


def _put(entity, material, amount):
    try:
        sm.add_lot(entity, material, amount, "contained", merge_burning=False,
                   merge_bindings=False, id_factory=lambda: m.identifier("matter"))
    except ValueError as exc:
        raise RuleViolation("产物批次数量或容量越界；该原子反应未执行。") from exc


def _effects(ctx, entity, kind, effects, *, params=None, event=None, channels=None, accounts=None, supply=None, objects=None, drive_source=None):
    changed, reactions = False, []
    channels = set() if channels is None else channels
    supply = _quantities(entity, include_burning=False) if supply is None else supply
    def account(category, material, amount):
        if accounts is None:
            commit.record_account(ctx, category, material, amount, "creative_reaction")
        else:
            sm.accumulate_quantity(accounts, (category, material, "creative_reaction"), amount)
    for effect in effects:
        context = _context(entity, params, event, ctx=ctx, objects=objects)
        if effect["op"] == "set":
            value = evaluate(effect["value"], context)
            field = kind["fields"][effect["field"]]
            if field["type"] == "integer" and not field["min"] <= value <= field["max"]:
                raise RuleViolation("规则字段" + effect["field"] + "结果越界，请修改表达式。")
            changed |= entity["state"]["program"]["fields"][effect["field"]] != value
            entity["state"]["program"]["fields"][effect["field"]] = value
        elif effect["op"] == "drive":
            enabled = evaluate(effect["enabled"], context)
            changed |= _set_drive(ctx, entity, kind, enabled, source=drive_source)
        elif effect["op"] == "react":
            if effect["mode"] == "exclusive" and effect["channel"] in channels:
                continue
            if effect["mode"] == "exclusive":
                channels.add(effect["channel"])
            materials = {key: {"load_per_u": value.load_per_u} for key, value in REGISTRY.materials.items()}
            pack, _ = _type(ctx, entity)
            materials.update(pack["materials"])
            registry = registry_for(entity)
            materials.update({alias: {"load_per_u": registry.materials[identity].load_per_u}
                              for alias, identity in entity["state"]["program"].get("material_aliases", {}).items()})
            for symbol in supply:
                identity = symbol if "#" in symbol else _material(entity, symbol)
                if identity in registry.materials:
                    materials[symbol] = {"load_per_u": registry.materials[identity].load_per_u}
            available = reaction_available_quantities(entity, supply=supply)
            from .definitions import mixture_capacity
            actual_capacity = mixture_capacity(entity)
            actual_capacity = sm.liquid_capacity(entity) if actual_capacity is None else actual_capacity
            # Planner supply excludes this phase's products and escrow, while
            # those real lots still occupy volume.
            capacity = sm.reaction_capacity(actual_capacity, sum(context["quantities"].values()), sum(available.values()))
            planned = plan_reaction(effect, available, materials, capacity)
            if not planned["extent"]:
                continue
            for symbol, amount in planned["consume"].items():
                material = _material(entity, symbol)
                _take(entity, material, amount)
                supply[symbol] -= amount
                account("sinks", material, amount)
            for symbol, amount in planned["produce"].items():
                material = _material(entity, symbol)
                _put(entity, material, amount)
                account("sources", material, amount)
            # Heat is a bounded authored state effect, not free mechanical energy.
            if "temperature" in kind["fields"]:
                field = kind["fields"]["temperature"]
                value = entity["state"]["program"]["fields"]["temperature"] + planned["heat"]
                if not field["min"] <= value <= field["max"]:
                    raise RuleViolation("反应温度超出定义字段范围；整笔反应回滚。")
                entity["state"]["program"]["fields"]["temperature"] = value
            reactions.append(planned)
            changed = True
        else:
            # An authored emitted signal is explicitly distinct from a verified
            # physical event and cannot count as a delivery or another's consent.
            entity["state"]["program"]["last_signal"] = {"name": effect["name"], "tick": step(ctx)}
            changed = True
    return changed, reactions






def _relevant_events(ctx, entity, since):
    location = _public_location(ctx, entity)
    if location is None:
        return []
    _, spec = _type(ctx, entity)
    radius = spec.get("recorder", {}).get("radius", 1)
    events = ctx.get_state(m.META, "creative_events", {"events": []})["events"]
    return [event for event in events if event["sequence"] > since and max(abs(event["x"]-location[0]), abs(event["y"]-location[1])) <= radius]


def _set_drive(ctx, entity, spec, enabled, *, source):
    if type(enabled) is not bool or spec.get("mechanical", {}).get("role") != "source" or entity["lifecycle"] != "intact":
        raise RuleViolation("drive只能启停当前完好物件已有的动力源。")
    binding = entity["state"]["program"]
    state = binding["mechanical"]
    changed = state["enabled"] != enabled
    state["enabled"] = enabled
    if changed:
        binding["drive_request"] = {"enabled": enabled, "requested_step": step(ctx),
                                    "applies_from_step": step(ctx)+1, "source": deepcopy(source)}
    return changed


def _barrier(entity, spec, event=None, *, ctx=None, objects=None, allow_unknown=False):
    known = True
    if "barrier" in spec:
        previous = entity["components"]["barrier"]["active"]
        try:
            current = bool(evaluate(spec["barrier"], _context(entity, event=event, ctx=ctx, objects=objects)))
        except FactUnavailable:
            if not allow_unknown:
                raise
            current = True
            known = False
            process = entity["state"]["program"]["process"]
            if process["status"] != "error":
                process.update(status="waiting", barrier_blocked_reason="unavailable_fact_or_port")
        else:
            entity["state"]["program"]["process"].pop("barrier_blocked_reason", None)
        entity["components"]["barrier"]["active"] = current
        if previous and not current:
            entity["state"]["program"]["barrier_open_pending"] = bool(ctx is not None and _public_location(ctx, entity) is not None)
        elif current:
            entity["state"]["program"].pop("barrier_open_pending", None)
    return known


def _port_candidates(ctx, player, entity, spec):
    from . import world_facts
    location = _public_location(ctx, entity)
    if location is None:
        return []
    result = []
    for target in m.public_entities_near(ctx, *location, radius=8):
        if target["id"] == entity["id"] or max(abs(target["x"]-player["x"]), abs(target["y"]-player["y"])) > 8:
            continue
        forest = world_facts.freeze(ctx, {target["id"]: target})
        facts = world_facts.facts(target, forest, external=True)
        eligible = [name for name, port in spec.get("ports", {}).items() if all(field in facts for field in port["fields"])]
        if eligible:
            result.append({"id": target["id"], "name": target["name"], "version": target["version"], "kind": "entity", "eligible_ports": eligible})
    return result


def _edit_port(ctx, player, entity, spec, args):
    from .world import _accessible
    port = args.get("port")
    if port not in spec.get("ports", {}):
        raise RuleViolation("请选择此固定类型已声明的端口名。")
    program = entity["state"]["program"]
    contract = program.get("contract")
    if contract and (contract["status"] not in {"unoffered", "awaiting_acceptance"} or contract["delivered"]):
        raise RuleViolation("成立或已有结果的契约固定实际绑定；不能切换目标。")
    if contract and contract["parties"] and contract["beneficiary"] != player["id"]:
        raise PermissionDenied("只有原提议者能修改未成立条款的绑定。")
    bindings = program.setdefault("port_bindings", {})
    previous = bindings.get(port)
    if args["action"] == "bind_port":
        other = m.get_entity(ctx, args.get("other_id", ""))
        _accessible(ctx, player, other, near=False, subject="other")
        if other["version"] != args.get("expected_other_version"):
            raise StateConflict("端口目标已变化；请读取同一观察中的实际对象版本。")
        candidates = _port_candidates(ctx, player, entity, spec)
        candidate = next((row for row in candidates if row["id"] == other["id"] and port in row["eligible_ports"]), None)
        if candidate is None:
            raise RuleViolation("端口目标不在双方可读范围，或不具备端口声明的公开事实。")
        bindings[port] = other["id"]
    else:
        if previous is None:
            raise RuleViolation("该端口没有实际绑定。")
        bindings.pop(port)
    if previous == bindings.get(port):
        return {"port": port, "previous_entity_id": previous, "entity_id": previous,
                "binding_version": program["port_binding_version"]}
    program["port_binding_version"] += 1
    if contract and contract["status"] == "awaiting_acceptance":
        contract.update(status="unoffered", terms_version=contract["terms_version"]+1,
                        parties=[], accepted=[], beneficiary=None, deadline_tick=None)
        contract.pop("pinned_ports", None)
        contract.pop("port_binding_version", None)
    program["process"].pop("blocked_reason", None)
    return {"port": port, "previous_entity_id": previous, "entity_id": bindings.get(port),
            "binding_version": program["port_binding_version"]}


def _delivery_reason(ctx, entity, spec):
    """Evaluate at admission, before material transfer. Missing facts fail closed."""
    try:
        if not evaluate(spec["contract"].get("delivery_when", True), _context(entity, ctx=ctx)):
            return "交付条件当前未满足；本次材料没有转移。"
    except ValueError as exc:
        return "交付条件无法读取公开事实：" + str(exc)[:140]
    return None




def _contract_update(ctx, entity, spec, event=None, *, objects=None, defer_unknown=False):
    state = entity["state"]["program"].get("contract")
    physically_changed = physical_contract_failure(ctx, entity)
    if not state or state["status"] != "active":
        return physically_changed
    if step(ctx) >= state["deadline_tick"]:
        state["status"] = "failed"
        state["finished_tick"] = step(ctx)
        return True
    condition = spec["contract"].get("completion_when", True)
    try:
        if has_event(condition):
            pending = event if isinstance(event, list) else [event] if event else []
            for fact in pending:
                if fact["sequence"] > state.get("started_event_sequence", 0) and evaluate(condition, _context(entity, event=fact, ctx=ctx, objects=objects)):
                    state["completion_proof"] = {key: fact[key] for key in ("sequence", "tick", "action", "entity_id", "actor")}
            fulfilled = bool(state.get("completion_proof"))
        else:
            fulfilled = evaluate(condition, _context(entity, ctx=ctx, objects=objects))
        state.pop("condition_error", None)
    except FactUnavailable as exc:
        if defer_unknown and has_event(condition):
            raise
        state["condition_error"] = str(exc)[:180]
        fulfilled = False
    except ValueError as exc:
        state["condition_error"] = str(exc)[:180]
        fulfilled = False
    if state["delivered"] >= state["quantity"] and fulfilled:
        state["status"] = "complete"
        state["finished_tick"] = step(ctx)
        return True
    return False


def tick(ctx):
    """Snapshot rules/relations, bounded reaction pass, network solve, after pass.

    No wall-clock catch-up. New products feed later logical steps, not an
    unbounded same-step chain; independent reactions share one updated balance.
    """
    ids = activity.meta_get(ctx, "creative_instances", [])
    # Logical-step facts and recorder histories are one commit, not one state
    # write per event/observer pair. Instance admission reserves their complete
    # combination with physics; history growth cannot exceed that reservation.
    objects = {eid: entity for eid in ids if (entity := m.get_entity(ctx, eid, required=False))
               and entity["lifecycle"] == "intact" and not entity["state"].get("retired")}
    if list(objects) != ids:
        activity.meta_set(ctx, "creative_instances", list(objects))
    specs = {eid: _type(ctx, entity)[1] for eid, entity in objects.items()}
    persisted = deepcopy(objects)
    # A prior authored failure stops the next step before any reader freezes
    # its facts. Preserve the already solved position/energy of the failed step.
    for entity in objects.values():
        program = entity["state"]["program"]
        if program["process"]["status"] == "error" and "mechanical" in program:
            program["mechanical"].update(speed=0, available_torque=0, status="program_error", enabled=False)
    original = deepcopy(objects)
    from . import contact, world_facts
    reaction_view = world_facts.freeze(ctx, original)
    physical_objects, _ = activity.physical_closure(ctx)
    contact_original = {eid: deepcopy(entity) for eid, entity in physical_objects.items()
                        if contact.candidates(ctx, entity)}
    journal_sequence = ctx.get_state(m.META, "creative_events", {"sequence": 0})["sequence"]
    events, pending_accounts, phase_supply = {}, {}, {}
    outcomes = []
    blocked_events, solved_mechanical, solved_markers = set(), {}, {}
    blocked_rules = {eid: [] for eid in objects}

    def fail_instance(eid, error, preserve_mechanical=False):
        # Declarative evaluation runs on local copies. No account or object
        # write has occurred for this instance, so its entire step can stop.
        mechanical_result = deepcopy(solved_mechanical.get(eid)) if preserve_mechanical else None
        objects[eid] = deepcopy(original[eid])
        program = objects[eid]["state"]["program"]
        program["process"].update(status="error", error=str(error)[:180], last_tick=step(ctx))
        blocked_rules[eid] = []
        if mechanical_result is not None:
            program["mechanical"] = mechanical_result
            if solved_markers[eid] is not None:
                program["mechanical_last_solved"] = deepcopy(solved_markers[eid])
        if "mechanical" in program and mechanical_result is None:
            program["mechanical"].update(speed=0, available_torque=0, status="program_error", enabled=False)
        pending_accounts[eid] = {}
        phase_supply[eid] = _quantities(original[eid], include_burning=False)
        outcomes[:] = [item for item in outcomes if item[0] != eid or preserve_mechanical and item[1] == "mechanical"]
        outcomes.append((eid, "program_error", "该物件规则暂停：" + str(error)[:120]))

    def wait_event_batch(eid, error):
        # Event cursors advance only with the complete declarative event batch.
        # A later retry cannot replay an already-committed prefix. The separately
        # solved physical result is a fact, not a candidate control request.
        if objects[eid]["state"]["program"]["process"]["status"] == "error":
            return
        objects[eid] = deepcopy(original[eid])
        program = objects[eid]["state"]["program"]
        if eid in solved_mechanical:
            program["mechanical"] = deepcopy(solved_mechanical[eid])
            if solved_markers[eid] is not None:
                program["mechanical_last_solved"] = deepcopy(solved_markers[eid])
        program["process"].update(status="waiting", blocked_reason=str(error)[:180])
        pending_accounts[eid] = {}
        phase_supply[eid] = _quantities(original[eid], include_burning=False)
        outcomes[:] = [item for item in outcomes if item[0] != eid or item[1] == "mechanical"]
        blocked_events.add(eid)

    def apply_declaration(eid, rule, fact, view, channels, *, predicate_snapshot=None):
        candidate = objects[eid]
        checkpoint = deepcopy(candidate), deepcopy(pending_accounts[eid]), deepcopy(phase_supply[eid]), set(channels)
        try:
            if evaluate(rule["when"], _context(predicate_snapshot or view[eid], event=fact, ctx=ctx, objects=view)):
                return _effects(ctx, candidate, specs[eid], rule["effects"], event=fact, channels=channels,
                                accounts=pending_accounts[eid], supply=phase_supply[eid], objects=view,
                                drive_source={"kind": "automatic_rule", "rule_id": rule["id"], "phase": rule["phase"]})[1]
        except FactUnavailable as exc:
            if fact is not None:
                wait_event_batch(eid, exc)
            else:
                objects[eid], pending_accounts[eid], phase_supply[eid], before_channels = checkpoint
                channels.clear()
                channels.update(before_channels)
                blocked_rules[eid].append({"rule_id": rule["id"], "phase": rule["phase"], "reason": str(exc)[:120]})
        return []

    for eid, entity in sorted(objects.items()):
        state = entity["state"]["program"]
        relevant = _relevant_events(ctx, entity, state.get("event_cursor", 0))
        event = relevant[-1] if relevant else None
        events[eid] = relevant
        pending_accounts[eid] = {}
        phase_supply[eid] = _quantities(entity, include_burning=False)
        if state["process"]["status"] == "error":
            continue
        try:
            channels, reactions = set(), []
            for rule, fact in phase_applications(specs[eid], "reaction", relevant):
                reactions.extend(apply_declaration(eid, rule, fact, reaction_view, channels))
                if eid in blocked_events:
                    break
            entity = objects[eid]
            state = entity["state"]["program"]
            if eid in blocked_events:
                continue
            if reactions:
                state["process"] = {"status": "running", "steps": state["process"]["steps"]+1,
                                    "last_tick": step(ctx), "last_reactions": reactions}
                outcomes.append((eid, "reaction", "实际组分按已发布配比反应，余量仍在。"))
            elif state["process"]["status"] == "running":
                state["process"]["status"] = "waiting"
            # A cursor tracks consumed event inputs, not every public change.
            # Recorder history is updated separately by public_event; neither
            # recording alone nor reading live object facts consumes this log.
            # Advancing unused cursors would invalidate otherwise unchanged
            # action targets whenever another object is moving or speaking.
            m.normalize_entity(entity)
        except (ValueError, RuleViolation) as exc:
            fail_instance(eid, exc)

    # Relations, inputs and loads come from the step's original snapshot. A
    # carried, closed, distant or asymmetrically disconnected pair is no mesh.
    nodes, edges = [], []
    for eid, entity in sorted(original.items()):
        if "mechanical" not in specs[eid] or world_facts.public_location(entity, reaction_view) is None or objects[eid]["state"]["program"]["process"]["status"] == "error":
            continue
        state = deepcopy(entity["state"]["program"]["mechanical"])
        mechanical = deepcopy(specs[eid]["mechanical"])
        nodes.append({"id": eid, "mechanical": mechanical, "fields": entity["state"]["program"]["fields"], "state": state})
        for link in entity["state"]["program"].get("connections", []):
            other = original.get(link["other_id"])
            if not other or eid >= other["id"] or world_facts.public_location(other, reaction_view) is None:
                continue
            if not any(reverse["other_id"] == eid for reverse in other["state"]["program"].get("connections", [])):
                continue
            first, second = world_facts.public_location(entity, reaction_view), world_facts.public_location(other, reaction_view)
            if max(abs(first[0]-second[0]), abs(first[1]-second[1])) <= 2:
                edges.append({"source": eid, "target": other["id"], "meshed": True})
    eligible = {node["id"] for node in nodes}
    edges = [edge for edge in edges if edge["source"] in eligible and edge["target"] in eligible]
    results = solve_mechanical(nodes, edges) if nodes else {}
    for eid, result in results.items():
        actual = deepcopy(original[eid]["state"]["program"]["mechanical"])
        actual.update({key: value for key, value in result.items() if key != "energy_used"})
        actual["energy"] -= result["energy_used"]
        solved_mechanical[eid] = actual
        previous_program = original[eid]["state"]["program"]
        marker = previous_program.get("mechanical_last_solved")
        # Mark a real solver change, not every evaluation or a control request.
        # Carried objects retain this historical marker without new solves.
        if actual != previous_program["mechanical"]:
            marker = {"step": step(ctx)}
        solved_markers[eid] = deepcopy(marker)
        if marker is not None:
            objects[eid]["state"]["program"]["mechanical_last_solved"] = deepcopy(marker)
        state = objects[eid]["state"]["program"]["mechanical"]
        state.update({key: value for key, value in result.items() if key != "energy_used"})
        state["energy"] -= result["energy_used"]
        if state["energy"] < 0:
            raise RuleViolation("机械求解消耗超过真实能源；本步未提交。")
        if result["speed"]:
            outcomes.append((eid, "mechanical", "传动网络按齿数、方向、负载推进。"))
    # Every after rule observes the same solved network and prior barriers.
    # Its candidate writes and failures feed a later step, never another
    # instance selected only by its id. Literal non-instance bindings also
    # belong to this read view; a missing binding remains missing throughout.
    after_candidates = deepcopy(objects)
    after_objects = deepcopy(reaction_view)
    for eid, result in solved_mechanical.items():
        after_objects[eid]["state"]["program"]["mechanical"] = deepcopy(result)
    for eid, candidate in objects.items():
        if candidate["state"]["program"]["process"]["status"] == "error" and "mechanical" in candidate["state"]["program"]:
            after_objects[eid]["state"]["program"]["mechanical"] = deepcopy(candidate["state"]["program"]["mechanical"])
    for eid in sorted(original):
        entity = objects[eid]
        spec = specs[eid]
        if entity["state"]["program"]["process"]["status"] != "error" and eid not in blocked_events:
            try:
                channels = set()
                for rule, fact in phase_applications(spec, "after", events[eid]):
                    apply_declaration(eid, rule, fact, after_objects, channels, predicate_snapshot=after_candidates[eid])
                    if eid in blocked_events:
                        break
                entity = objects[eid]
                if eid not in blocked_events:
                    known = _barrier(entity, spec, events[eid][-1] if events[eid] else None, ctx=ctx, objects=after_objects, allow_unknown=True)
                    if not known and has_event(spec.get("barrier")):
                        wait_event_batch(eid, FactUnavailable("事件关联的障碍事实当前不可读。"))
                        entity = objects[eid]
                        entity["components"]["barrier"]["active"] = True
                        entity["state"]["program"].pop("barrier_open_pending", None)
                m.normalize_entity(entity)
            except (ValueError, RuleViolation) as exc:
                fail_instance(eid, exc, preserve_mechanical=True)
                entity = objects[eid]
        process = entity["state"]["program"]["process"]
        if eid in blocked_events and "barrier" in spec:
            _barrier(entity, spec, events[eid][-1] if events[eid] else None, ctx=ctx, objects=after_objects, allow_unknown=True)
        if blocked_rules[eid] and eid not in blocked_events and process["status"] != "error":
            process.update(status="waiting", blocked_rules=blocked_rules[eid], blocked_reason="unavailable_fact_or_port")
        elif eid not in blocked_events:
            process.pop("blocked_rules", None)
            if process.get("barrier_blocked_reason"):
                process["blocked_reason"] = process["barrier_blocked_reason"]
            else:
                process.pop("blocked_reason", None)
        recorder = entity["state"]["program"].get("recorder")
        if recorder and recorder["active"] and step(ctx) >= recorder["until_tick"]:
            recorder["active"] = False
        # Deadlines still advance even if this instance's authored rule stopped.
        # Cross-object contract conditions retain the step-start read view;
        # after/physical feedback does not move contract completion earlier.
        try:
            if _contract_update(ctx, entity, spec, events[eid], objects=reaction_view, defer_unknown=True):
                outcomes.append((eid, "contract", "契约依据真实交付和逻辑期限形成结果。"))
        except FactUnavailable as exc:
            wait_event_batch(eid, exc)
            entity = objects[eid]
            entity["state"]["program"]["contract"]["condition_error"] = str(exc)[:180]
            _barrier(entity, spec, events[eid][-1] if events[eid] else None, ctx=ctx, objects=after_objects, allow_unknown=True)
        if has_event(spec) and eid not in blocked_events and entity["state"]["program"]["process"]["status"] != "error":
            entity["state"]["program"]["event_cursor"] = journal_sequence
    # Existing authored stages retain their order and frozen views. Contact
    # comes last, sharing their remaining original inputs, never their products.
    for eid, snapshot in sorted(contact_original.items()):
        entity = objects.get(eid, physical_objects[eid])
        supply = ({symbol if "#" in symbol else _material(snapshot, symbol): amount for symbol, amount in phase_supply[eid].items()}
                  if eid in phase_supply else _quantities(snapshot, symbolic=False, include_burning=False))
        candidate, entries, outcome = contact.settle(ctx, entity, snapshot, supply, step(ctx))
        objects[eid] = candidate
        original.setdefault(eid, snapshot)
        persisted.setdefault(eid, snapshot)
        pending_accounts["contact:" + eid] = entries
        if outcome and _public_location(ctx, candidate) is not None and candidate["state"]["open"]:
            outcomes.append(outcome)
    accounts = {}
    for entries in pending_accounts.values():
        for key, amount in entries.items():
            sm.accumulate_quantity(accounts, key, amount)
    changed = {eid for eid, entity in objects.items() if entity != persisted[eid]}
    commit.commit_results(ctx, objects, changed, accounts=accounts, tick=step(ctx))
    for eid, action, summary in outcomes:
        public_event(ctx, action=action, entity_id=eid, kind="process", actor="system:rules")
    return outcomes


def guard_ordinary_action(ctx, action, entity, item=None):
    """Contract escrow cannot be withdrawn through the ordinary container route."""
    for obj in [entity, item] if item else [entity]:
        contract = obj["state"].get("program", {}).get("contract")
        if contract and contract.get("parties") and (contract["delivered"] > contract.get("released", 0)+contract.get("lost", 0) or contract["status"] in {"awaiting_acceptance", "active"}):
            if action not in {"open", "close"}:
                raise PermissionDenied("此物件持有契约或托管材料；请使用契约接受、交付和释放动作，不能绕过双方条件。")


def _item(ctx, player, args, *, allow_container=True):
    from .world import _accessible
    if not args.get("item_id") or not args.get("expected_item_version"):
        raise RuleViolation("请选择实际来源物件和最新版本。")
    entity = m.get_entity(ctx, args["item_id"])
    _accessible(ctx, player, entity, subject="item")
    if entity["version"] != args["expected_item_version"]:
        raise StateConflict("材料或接收物件已变化，请重新读取。")
    if entity.get("owner") != player["id"] or "personal" in entity["capabilities"] and not allow_container:
        raise PermissionDenied("必须使用本人可操作的真实物件。")
    if entity["state"]["burning"]:
        raise RuleViolation("燃烧中的材料不能投入此动作。")
    guard_ordinary_action(ctx, "transfer", entity)
    return entity


def _finish_item(ctx, item, target):
    if not sm.quantity(item) and not item["state"]["contents"] and not item["state"].get("attachments"):
        commit.retire_material(ctx, item, target["id"])
    else:
        commit.save_entity(ctx, item)


def rule_action(ctx, args):
    from .world import _accessible, _public_self, _project_entity, viewport
    check_delegation(ctx, args)
    player = m.get_player(ctx)
    m.check_player_version(player, args["expected_player_version"])
    entity = m.get_entity(ctx, args["target_id"])
    _accessible(ctx, player, entity, subject="target")
    if entity["version"] != args["expected_target_version"]:
        raise StateConflict("规则物件已变化，请读取最新版本。")
    if entity["lifecycle"] != "intact" and not (entity["lifecycle"] == "broken" and args["action"] == "release"):
        raise RuleViolation("规则物件已破损或耗尽。")
    pack, spec = _type(ctx, entity)
    if not spec:
        if args["action"] == "charge":
            raise RuleViolation("普通物件没有本地声明材料的制备规则。charge需已发表定义的实际容器实例，消耗feedstock制备其声明材料；"
                "可用generation_context的layout创建该定义中的容器类型。普通容器只对转入的已有材料批次结算接触反应，feedstock不等于新材料。")
        raise RuleViolation("此物件没有可执行的声明规则。")
    program, action = entity["state"]["program"], args["action"]
    summary = ""
    result_extra = {}
    _contract_update(ctx, entity, spec)
    if action in {"bind_port", "unbind_port"}:
        result_extra["port_binding"] = _edit_port(ctx, player, entity, spec, args)
        summary = "已明确绑定实际公开对象，下一步读取其声明事实。" if action == "bind_port" else "已解除端口；依赖事实保持未知，等待明确重新绑定。"
        if program.get("contract", {}).get("status") == "unoffered" and program.get("contract", {}).get("terms_version"):
            summary += "未成立条款的此前接受已清除，需重新提议。"
    elif action == "charge":
        if "container" not in entity["components"] or not entity["state"]["open"]:
            raise RuleViolation("配料需要打开的实际容器。")
        if args.get("material") not in pack["materials"]:
            choices = ", ".join(f"{symbol} (feedstock={value['feedstock']})" for symbol, value in pack["materials"].items())
            raise RuleViolation("charge只制备本规则包已声明材料：" + choices +
                "；消耗对应feedstock并转换材料。转移已有批次请读grove.inspect的transfer选项，使用grove.interact transfer。")
        amount = args.get("amount", 0)
        if not amount:
            raise RuleViolation("请输入投入的真实u数量。")
        item = _item(ctx, player, args, allow_container=False)
        if item["id"] == entity["id"]:
            raise RuleViolation("请选择独立来源，不能用反应物自身的承重结构无限配料。")
        definition = pack["materials"][args["material"]]
        from .definitions import mixture_capacity
        material = _material(entity, args["material"])
        if amount > sm.contained_room(registry_for(entity), entity, material, mixture_capacity=mixture_capacity(entity)):
            raise RuleViolation("实际混合物超过定义容积；来源没有消耗。")
        _take(item, definition["feedstock"], amount, slots=("body", "contained", "surface"))
        _put(entity, material, amount)
        commit.record_account(ctx, "sinks", definition["feedstock"], amount, "creative_charge")
        commit.record_account(ctx, "sources", material, amount, "creative_charge")
        _finish_item(ctx, item, entity)
        summary = f"消耗{amount}u {definition['feedstock']}，制备{amount}u {args['material']}；来源余量保留。这不是反应产物证据。"
    elif action in {"fuel", "start", "stop"}:
        if spec.get("mechanical", {}).get("role") != "source":
            raise RuleViolation("此物件不是动力源。")
        state = program["mechanical"]
        if action == "fuel":
            amount = args.get("amount", 0)
            if not amount or state["energy"] + amount > 10000:
                raise RuleViolation("请输入1..10000u燃料，存储能源不能超过10000。")
            item = _item(ctx, player, args, allow_container=False)
            if item["id"] == entity["id"]:
                raise RuleViolation("动力源不能消耗自己的结构冒充外部燃料。")
            from .definitions import select_fuel
            lot = select_fuel(ctx, player, item, "mechanical_fuel", args.get("lot_id"))
            if amount > lot["quantity"]:
                raise RuleViolation("所选燃料批次不足；没有自动改用另一批。")
            material = lot["material"]
            lot["quantity"] -= amount
            item["matter"] = sm.normalize_lots(registry_for(item), item["id"], item["matter"])
            state["energy"] += amount
            commit.record_account(ctx, "sinks", material, amount, "mechanical_fuel")
            _finish_item(ctx, item, entity)
            summary = f"燃料已消耗，储存了{amount}单位有限能源。"
        else:
            _set_drive(ctx, entity, spec, action == "start", source={"kind": "explicit_action",
                       "action_id": action, "actor_role_id": ctx.actor_role_id})
            summary = "动力输入已启用。" if state["enabled"] else "动力输入已停止，已有位移保留。"
    elif action in {"connect", "disconnect"}:
        if "mechanical" not in spec or not args.get("other_id"):
            raise RuleViolation("请选择两个实际机械接口。")
        other = m.get_entity(ctx, args["other_id"])
        _accessible(ctx, player, other, near=False, subject="other")
        _, other_spec = _type(ctx, other)
        if other["id"] == entity["id"] or not other_spec or "mechanical" not in other_spec:
            raise RuleViolation("不能自连，另一物件必须有已声明机械接口。")
        if other["version"] != args.get("expected_other_version"):
            raise StateConflict("另一机械物件已变化，请重新读取。")
        if _public_location(ctx, entity) is None or _public_location(ctx, other) is None:
            raise RuleViolation("机械网络须由公开放置的物件组成，先放下并公开。")
        first, second = m.entity_location(ctx, entity), m.entity_location(ctx, other)
        if max(abs(first[0]-second[0]), abs(first[1]-second[1])) > 2:
            raise RuleViolation("机械接口距离超过2格。")
        for left, right in ((entity, other), (other, entity)):
            links = left["state"]["program"]["connections"]
            exists = any(link["other_id"] == right["id"] for link in links)
            if action == "connect" and not exists:
                if len(links) >= MAX_LINKS:
                    raise RuleViolation("单个接口最多8个真实连接。")
                links.append({"other_id": right["id"]})
                links.sort(key=lambda link: link["other_id"])
            elif action == "disconnect":
                left["state"]["program"]["connections"] = [link for link in links if link["other_id"] != right["id"]]
        commit.save_entity(ctx, other)
        summary = "已连接实际传动接口，下一逻辑步参与求解。" if action == "connect" else "已断开传动连接，下一步不再传力。"
    elif action in {"start_recording", "stop_recording", "read_records"}:
        if "recorder" not in spec:
            raise RuleViolation("此物件没有记录能力。")
        state = program["recorder"]
        if action == "start_recording":
            if _public_location(ctx, entity) is None:
                raise RuleViolation("先公开放置记录物件；不会在私人背包内监听世界。")
            state.update(active=True, started_tick=step(ctx), until_tick=step(ctx)+spec["recorder"]["window_ticks"])
            summary = "记录已启用，只记之后范围内实际发生的公开事件。"
        elif action == "stop_recording":
            state["active"] = False
            summary = "记录已停止，原记录保留。"
        else:
            record = ctx.get_state(RECORDS, entity["id"], {"history": [], "reads": []})
            if _public_location(ctx, entity) is not None:
                record["reads"] = [*record["reads"], {"actor": player["id"], "read_tick": step(ctx),
                                   "sequence": record["history"][-1]["sequence"] if record["history"] else 0}][-16:]
                record = bounded_record(record)
                _save(ctx, RECORDS, entity["id"], record)
            result_extra["records"] = bounded_record(record)
            summary = "读取了真实记录，阅读身份另行记录，不补写过去。"
    elif action in {"offer", "accept", "deliver", "release"}:
        if "contract" not in spec:
            raise RuleViolation("此物件没有契约能力。")
        state = program["contract"]
        if action == "offer":
            if _public_location(ctx, entity) is None:
                raise RuleViolation("先把契约公开放置，双方才能读取并以自己的身份接受。")
            if state["status"] not in {"unoffered", "awaiting_acceptance"} or state["delivered"]:
                raise RuleViolation("此契约已有实际执行结果，不能重写条款或参与者。")
            if state["parties"] and state["beneficiary"] != player["id"]:
                raise PermissionDenied("只能由原提议者修改未成立条款；修改将取消此前接受。")
            other = m.get_player(ctx, args.get("other_id", ""), required=False) if args.get("other_id") else None
            if not other or other["id"] == player["id"] or other["version"] != args.get("expected_other_version"):
                raise RuleViolation("请选择另一个已进入世界的实际参与者及当前档案版本。")
            if max(abs(other["x"]-player["x"]), abs(other["y"]-player["y"])) > 8:
                raise RuleViolation("只能向当前可见参与者提出契约。")
            if spec.get("ports"):
                from . import world_facts
                forest = world_facts.freeze(ctx, {entity["id"]: entity})
                context = _context(entity, ctx=ctx, objects=forest, definition=pack)
                pinned = {}
                for port, declaration in spec["ports"].items():
                    target_id = program["port_bindings"].get(port)
                    facts = context["objects"].get(target_id, {})
                    if any(field not in facts for field in declaration["fields"]):
                        raise RuleViolation("契约提议前须明确绑定全部声明端口及可读的实际事实。")
                    pinned[port] = {"entity_id": target_id, "entity_version": forest[target_id]["version"]}
                state.update(pinned_ports=pinned, port_binding_version=program["port_binding_version"])
            state.update(status="awaiting_acceptance", terms_version=state["terms_version"]+1,
                         parties=[player["id"], other["id"]], accepted=[], beneficiary=player["id"], deadline_tick=None)
            summary = "条款已提出，双方均需以自己身份明确接受；提出本身不算接受。"
        elif action == "accept":
            if state["status"] != "awaiting_acceptance" or player["id"] not in state["parties"]:
                raise PermissionDenied("只有该条款中的真实参与者能接受尚未成立契约。")
            if args.get("terms_version") != state["terms_version"]:
                raise StateConflict("条款版本已变化，之前的意愿不适用于新条款。")
            if player["id"] not in state["accepted"]:
                state["accepted"].append(player["id"])
            if set(state["accepted"]) == set(state["parties"]):
                state.update(status="active", deadline_tick=step(ctx)+spec["contract"]["duration_ticks"],
                             started_event_sequence=ctx.get_state(m.META, "creative_events", {"sequence": 0})["sequence"])
            summary = "已保存本人的明确接受。" + ("双方接受成立，逻辑期限开始。" if state["status"] == "active" else "仍等待另一方。")
        elif action == "deliver":
            if state["status"] != "active" or player["id"] != state["parties"][1]:
                raise PermissionDenied("只有明确接受后的交付方能在有效期内履约。")
            reason = _delivery_reason(ctx, entity, spec)
            if reason:
                raise RuleViolation(reason)
            item = _item(ctx, player, args, allow_container=False)
            amount = args.get("amount", 0)
            if not amount or amount > state["quantity"] - state["delivered"]:
                raise RuleViolation("交付数必须是实际缺口内的正数。")
            if state["material"] not in registry_for([item, entity]).materials:
                raise RuleViolation("实际材料与条款不一致。")
            # Transfer actual lots, preserving origin and definitions. No text
            # declaration can supply these quantities.
            left = amount
            for lot in list(item["matter"]):
                if lot["material"] != state["material"] or lot["burning"]:
                    continue
                take = min(left, lot["quantity"])
                if take:
                    commit.transfer_material(ctx, item, entity, lot["id"], take, reason="contract_delivery")
                    left -= take
                if not left:
                    break
            if left:
                raise RuleViolation("实际交付材料不足，整笔交付回滚。")
            entity = m.get_entity(ctx, entity["id"])
            program = entity["state"]["program"]
            state = program["contract"]
            state["delivered"] += amount
            _contract_update(ctx, entity, spec)
            _finish_item(ctx, item, entity)
            summary = f"实际交付{amount}u，已交付{state['delivered']}／{state['quantity']}u。"
        else:
            recipient = state["beneficiary"] if state["status"] == "complete" else state["parties"][1] if state["status"] == "failed" and len(state["parties"]) == 2 else None
            if recipient != player["id"]:
                raise PermissionDenied("完成后仅受益方能取交付物；失败后仅交付方能取回托管物。")
            item = _item(ctx, player, args)
            if "personal" in item["capabilities"]:
                raise RuleViolation("普通背包只放整件物件，不能接收散料；请选择本人打开的可盛料容器。")
            if "container" not in item["components"] or not item["state"]["open"]:
                raise RuleViolation("请选择本人打开的可盛料容器。")
            left = max(0, state["delivered"]-state.get("released", 0)-state.get("lost", 0))
            if not left:
                raise RuleViolation(f"没有尚未释放的实际交付物；已记录损失{state.get('lost', 0)}u。")
            slots = {"contained", "surface"} if entity["lifecycle"] == "broken" else {"contained"}
            available = sum(lot["quantity"] for lot in entity["matter"]
                            if lot["material"] == state["material"] and lot["slot"] in slots)
            loss = max(0, left-available)
            lost_total = state.get("lost", 0)+loss
            state["lost"] = lost_total
            left -= loss
            released = left
            for lot in list(entity["matter"]):
                if lot["material"] == state["material"] and lot["slot"] in slots:
                    take = min(left, lot["quantity"])
                    if take:
                        commit.transfer_material(ctx, entity, item, lot["id"], take, reason="contract_release")
                        left -= take
                    if not left:
                        break
            if left:
                raise RuleViolation("托管实际余量不足，释放未提交。")
            if released:
                entity = m.get_entity(ctx, entity["id"])
            program = entity["state"]["program"]
            state = program["contract"]
            state["lost"] = lost_total
            state["released"] = state.get("released", 0)+released
            state["escrow_remaining"] = 0
            result_extra.update(released_quantity=released, lost_quantity=state.get("lost", 0))
            summary = f"按完成／失败归属释放真实余量{released}u；累计损失{state.get('lost', 0)}u已记录，不能重复领取。"
    else:
        declared = next((item for item in spec["actions"] if item["id"] == action), None)
        if declared is None:
            raise RuleViolation("动作没有在该不可变规则版本中声明。")
        params = args.get("params", {})
        if set(params)-set(declared["parameters"]):
            raise RuleViolation("动作参数中存在未声明字段。")
        params = {name: params.get(name, field["default"]) for name, field in declared["parameters"].items()}
        for name, field in declared["parameters"].items():
            value = params[name]
            if (field["type"] == "boolean" and type(value) is not bool) or (field["type"] == "integer" and (type(value) is not int or not field["min"] <= value <= field["max"])):
                raise RuleViolation("动作参数" + name + "类型或范围无效。")
        from . import world_facts
        action_view = world_facts.freeze(ctx, {entity["id"]: entity})
        try:
            if not evaluate(declared.get("when", True), _context(entity, params, ctx=ctx, objects=action_view)):
                raise RuleViolation("此动作的实际前提尚未成立。")
            _effects(ctx, entity, spec, declared["effects"], params=params, objects=action_view,
                     drive_source={"kind": "explicit_action", "action_id": action, "actor_role_id": ctx.actor_role_id})
        except FactUnavailable as exc:
            raise RuleViolation("声明事实或端口当前不可读；本次动作未执行：" + str(exc)) from exc
        program["process"].update(status="idle", last_tick=step(ctx))
        program["process"].pop("error", None)
        summary = "已执行已发布动作：" + declared["label"] + "。"
    if entity["lifecycle"] == "intact":
        _barrier(entity, spec, ctx=ctx, allow_unknown=True)
    commit.save_entity(ctx, entity)
    location = _public_location(ctx, entity)
    events = ()
    if location is not None:
        commit.record_change(ctx, summary, x=location[0], y=location[1], entity_id=entity["id"], action=action)
        from .generation import record_content_outcome
        record_content_outcome(ctx, entity["id"], action, summary)
        events = (StreamEvent("grove.changes", "rule_action", {"entity_id": entity["id"], "action": action, "summary": summary}, key="rule-action"),)
    # Re-read: immediate public event recording may update this same object.
    entity = m.get_entity(ctx, entity["id"])
    return FunctionOutcome({"player": _public_self(m.get_player(ctx)), "entity": _project_entity(ctx, player, entity),
        "changes": [summary], "viewport": viewport(ctx, {}), **result_extra}, events=events)


def projection(ctx, entity, *, detailed=False):
    binding = entity["state"].get("program")
    if not binding or not binding.get("definition_ref"):
        return None
    result = deepcopy(binding)
    result.pop("materials", None)
    result["world_tick"] = step(ctx)
    if "recorder" in binding:
        record = bounded_record(ctx.get_state(RECORDS, entity["id"], {"history": [], "reads": []}))
        result["history"] = record["history"]
        result["reads"] = record["reads"]
    from .observations import program_observation
    result.update(program_observation(ctx, entity, detailed=detailed))
    if detailed:
        pack, _ = _type(ctx, entity)
        result["material_semantics"] = program_material_semantics(pack, binding["definition_ref"], materials=REGISTRY.materials,
                                                                 type_id=binding["type_id"])
        result["declaration_semantics"] = declaration_semantics(pack, type_id=binding["type_id"])
    return result


def action_options(ctx, player, entity):
    from .world import _reason
    pack, spec = _type(ctx, entity)
    if not spec:
        return []
    base = _reason(ctx, player, entity, burning=True)
    candidates = [m.get_entity(ctx, eid) for eid in player["inventory"]]
    own_items = [{"id": item["id"], "name": item["name"], "version": item["version"], "kind": "entity"} for item in candidates
                 if item["owner"] == player["id"] and not item["state"]["burning"] and not item["state"].get("retired")]
    location = _public_location(ctx, entity)
    near = m.public_entities_near(ctx, location[0], location[1], radius=2) if location else []
    mechanicals = [{"id": item["id"], "name": item["name"], "version": item["version"], "kind": "entity"} for item in near
                   if item["id"] != entity["id"] and item["state"].get("program", {}).get("mechanical") is not None]
    players = [{"id": item["id"], "name": item["name"], "version": item["version"], "kind": "role"}
               for item in m.public_players_near(ctx, player["x"], player["y"], radius=8) if item["id"] != player["id"]]
    options = []

    def add(action, label, choices=None, params=None, arguments=None, reason=None, amount=False, use=None):
        if entity["lifecycle"] != "intact" and action != "release":
            reason = "规则物件已破损，只有实际托管物可按结果回收。"
        fields = {"action": {"const": action}, "target_id": ID, "expected_target_version": VERSION, "expected_player_version": VERSION}
        required = list(fields)
        args = {"action": action, "target_id": entity["id"], "expected_target_version": entity["version"]}
        choices = choices or {}
        for name, key in (("items", "item_id"), ("others", "other_id"), ("materials", "material")):
            if name in choices:
                fields[key] = ID
                required.append(key)
                if not choices[name]:
                    reason = reason or "附近没有符合条件的实际对象或材料。"
                if key != "material":
                    vkey = "expected_item_version" if key == "item_id" else "expected_other_version"
                    fields[vkey] = VERSION
                    required.append(vkey)
        if amount:
            fields["amount"] = ACTION_ARGS["properties"]["amount"]
            required.append("amount")
            args["amount"] = 1
        if any(item.get("lots") for item in choices.get("items", [])):
            fields["lot_id"] = ID
            required.append("lot_id")
        if arguments:
            args.update(arguments)
            for key in arguments:
                fields[key] = ACTION_ARGS["properties"].get(key, {})
        schema = {"type": "object", "additionalProperties": False, "properties": fields, "required": required}
        param_schema = {"type": "object", "additionalProperties": False, "properties": {}, "required": []}
        if params:
            args["params"] = {name: field["default"] for name, field in params.items()}
            for name, field in params.items():
                param_schema["properties"][name] = {"type": field["type"], "default": field["default"]}
                if field["type"] == "integer":
                    param_schema["properties"][name].update(minimum=field["min"], maximum=field["max"])
            fields["params"] = param_schema
        options.append({"action": action, "label": label, "function_id": "grove.rule_action", "available": not (base or reason),
                        "reason": base or reason, "arguments": args, "input_schema": schema, "params_schema": param_schema, "choices": choices})
        if use:
            options[-1]["use"] = use

    if pack["materials"] and "container" in entity["components"]:
        add("charge", "制备已声明材料", {"items": own_items, "materials": [
            {"id": key, "name": value["name"], "feedstock": value["feedstock"]} for key, value in pack["materials"].items()]}, amount=True,
            use="选材料symbol，消耗来源中的对应feedstock，等量制备该声明材料。转移已有批次用transfer，搬入整件用store；制备不是规则反应。")
    if spec.get("ports"):
        contract = entity["state"]["program"].get("contract", {})
        locked = contract.get("status", "unoffered") not in {"unoffered", "awaiting_acceptance"} or contract.get("delivered", 0)
        reason = "契约已有实际结果或已成立，绑定固定。" if locked else None
        port_names = list(spec["ports"])
        add("bind_port", "绑定公开事实", {"others": _port_candidates(ctx, player, entity, spec)},
            arguments={"port": port_names[0]}, reason=reason,
            use="先选择声明端口，再明确选择eligible_ports包含它的实际目标；端口只读，不授予远程写入，也不会自动重绑定。未成立条款修改会清空此前接受。")
        options[-1]["input_schema"]["properties"]["port"] = {"type": "string", "enum": port_names}
        options[-1]["input_schema"]["required"].append("port")
        bound = list(entity["state"]["program"].get("port_bindings", {}))
        add("unbind_port", "解除事实绑定", arguments={"port": bound[0] if bound else port_names[0]},
            reason=reason or (None if bound else "当前没有实际端口绑定。"))
        options[-1]["input_schema"]["properties"]["port"] = {"type": "string", "enum": bound or port_names}
        options[-1]["input_schema"]["required"].append("port")
    if "mechanical" in spec:
        add("connect", "连接传动", {"others": mechanicals})
        links = {item["other_id"] for item in entity["state"]["program"]["connections"]}
        add("disconnect", "断开传动", {"others": [item for item in mechanicals if item["id"] in links]})
        if spec["mechanical"]["role"] == "source":
            from .definitions import usable_fuel_lots
            fuels = [{"id": item["id"], "name": item["name"], "version": item["version"], "kind": "entity",
                      "lots": [{key: lot[key] for key in ("id", "material", "quantity", "slot")} for lot in lots]}
                     for item in candidates if (lots := usable_fuel_lots(ctx, player, item, "mechanical_fuel"))]
            add("fuel", "消耗合格燃料储能", {"items": fuels}, amount=True,
                use="按lot_id消耗实际燃料；原材料定义须声明mechanical_fuel资格。设置启停请求后，下一模拟步才参与原始输入求解。")
            add("start", "启用动力")
            add("stop", "停止动力")
    if "recorder" in spec:
        add("start_recording", "开始记录现场", reason=None if location else "先公开放置记录物件。")
        add("stop_recording", "停止记录")
        add("read_records", "阅读实际记录")
    if "contract" in spec:
        state = entity["state"]["program"]["contract"]
        add("offer", "提出双方契约", {"others": players}, reason="先公开放置契约，让双方能读取与接受。" if location is None else None if state["status"] in {"unoffered", "awaiting_acceptance"} else "该契约已成立或已有结果。")
        add("accept", "接受当前条款", arguments={"terms_version": max(1, state["terms_version"])},
            reason=None if state["status"] == "awaiting_acceptance" and player["id"] in state["parties"] else "本人不是等待接受的参与者。")
        add("deliver", "交付真实材料", {"items": own_items}, amount=True,
            reason=_delivery_reason(ctx, entity, spec) if state["status"] == "active" and player["id"] == state["parties"][1] else "契约未成立或本人不是交付方。")
        recipient = state["beneficiary"] if state["status"] == "complete" else state["parties"][1] if state["status"] == "failed" and len(state["parties"]) == 2 else None
        containers = [item for item in own_items
                      if "container" in (container := m.get_entity(ctx, item["id"]))["components"]
                      and "personal" not in container["capabilities"]]
        add("release", "释放实际托管物", {"items": containers}, reason=None if recipient == player["id"] and state["delivered"] > state.get("released", 0)+state.get("lost", 0) else "本人当前没有可领取的托管材料。",
            use="完成后受益人用release领入本人打开的可盛料容器；普通背包只放整件物件，不能接收散料。失败后交付方用同一动作取回实际余量。源契约容器关闭仍可release，这是合同托管释放的合法特例；普通容器装卸仍须先打开。")
    for action in spec["actions"]:
        defaults = {name: field["default"] for name, field in action["parameters"].items()}
        # Editable parameters can make a currently false default predicate true;
        # only the actual submitted input is a final eligibility decision.
        try:
            eligible = evaluate(action.get("when", True), _context(entity, defaults, ctx=ctx))
        except ValueError:
            eligible = False
        add(action["id"], action["label"], params=action["parameters"], reason=None if eligible or defaults else "声明前提尚未成立。")
    return options


def rule_catalog(ctx, args):
    from agent_world.simulation.programs import (
        MECHANICAL_DEFAULTS, MECHANICAL_RANGES, PROGRAM_KEYS, PROGRAM_REQUIRED_KEYS, TYPE_KEYS, TYPE_REQUIRED_KEYS)
    object_fields = "|".join(PUBLIC_FACTS)
    physical_fields = "|".join(name.removeprefix("physical.") for name in PUBLIC_FACTS if name.startswith("physical."))
    component_schema = {
        name: {"type": "object", "additionalProperties": False,
               "properties": {field: {"type": {int: "integer", bool: "boolean", str: "string"}[kind],
                                      **({"minimum": minimum} if minimum is not None else {}),
                                      **({"maximum": maximum} if maximum is not None else {})}
                              for field, (kind, minimum, maximum) in REGISTRY.components[name].fields.items()
                              if field != "supports"}}
        for name in PROGRAM_COMPONENTS}
    return FunctionOutcome({"language_version": 3, "definition_directory": {"function_id": "grove.definition_list",
        "read_function_id": "grove.definition_read", "seek_paged": True}, "world_tick": step(ctx),
        "builtin_materials": {key: {"name": value.name, "load_per_u": value.load_per_u,
            "combustible": value.combustible, "liquid": value.liquid} for key, value in REGISTRY.materials.items()},
        "phase_policy": declaration_semantics({"types": {}})["phase_policy"],
        "execution_model": {
            "automatic_rules": "Each intact admitted instance evaluates types[].rules automatically at world logical steps, including ordinary self rules while carried. There is no action button for an automatic rule and no Agent inference is needed to run it. Publishing a definition alone creates no instance.",
            "time": "This world advances in the background, not by player turns. The timer targets a roughly one-second logical-step cadence; actual service work and late-timer handling can delay advancement. Reading, model thinking and travel do not pause the world. A successful mechanical solve consumes the source's finite energy_per_step, so available fuel can be spent before the next command. Use clock.sequence and actual readback, not elapsed wall time, to determine what happened. Server downtime pauses the world without catch-up.",
            "manual_actions": "types[].actions and reserved rule_action operations run only when a person or an explicitly granted Agent invokes them. Setup, fuel, binding and a successful manual action do not prove an automatic rule ran.",
            "conditions": "when=false skips that rule; it neither undoes earlier effects nor stops an already enabled source. An unavailable rule input produces no new effect and waits, retaining existing control rather than becoming false/zero or automatically switching targets. Other independently valid rules retain their normal semantics. Error status is distinct from recoverable unknown facts.",
            "event_rules": "Rules using event references consume actual nearby public journal inputs in sequence, not a synthetic current event. A carried/private anchor receives no public events; the default event radius is1, or the declared recorder radius when present.",
            "deployment": "Self rules do not all require ground placement. Mechanical solve requires public reachable objects: carried/private nodes and closed-contained nodes do not participate. Finite qualified fuel energy, enabled source, actual reciprocal connections within2 tiles and feasible network load determine real motion.",
            "fact_ports": "A fact port reads a bound public source within8 tiles of the public anchor; binding requires both actor and anchor range. Binding supplies no motion or permission. Mechanical connect establishes a different relation within2 tiles. Missing/private/closed-contained/distant source facts are unknown; the original binding is retained.",
            "drive": "drive controls only this instance's existing source. A changed request applies from the following logical step; the current mechanical solve uses the step-start control and fuel snapshot. Neither drive nor start writes energy, speed or position. when=false is not an implicit stop request.",
            "readback": "Inspect current automatic conditions, bindings and actual fields/matter/mechanical state. A satisfied snapshot condition is not execution evidence; a saved control request records a past change, not repeated activity. Read actual state after the request's applies_from_step. process.steps counts reaction steps only, not all rule execution or goal progress. Another person may change public inputs; re-read the same instance and continue from its real shared consequences."},
        "limits": {"publication_bytes": 24576, "new_publications_per_author_step": publication.MAX_PUBLISH_PER_STEP,
                   "publication_storage": "no small lifetime package/byte cap; metered JSON payloads and actual host storage remain separate",
                   "instances": MAX_INSTANCES, "types_per_package": 8, "materials_per_package": 4, "rules_per_type": 8,
                   "effects_per_rule": 8, "expression_depth": 8, "expression_nodes": 64, "links_per_object": MAX_LINKS, "records_per_object": MAX_RECORDS,
                   "record_bytes_per_object": MAX_RECORD_BYTES, "event_bytes": MAX_EVENT_BYTES,
                   "event_journal_bytes": MAX_EVENT_JOURNAL_BYTES,
                   "contact_candidates_per_container": 8, "process_facts_per_step": 164,
                   "instance_admission": "all admitted authored and contact future records must fit 512 changes, 2MiB and164 facts before creating an instance or receiving reactive material; future snapshots/slots/results also fit4 custom materials,8 lots,8KiB"},
        "definition_fields": {"allowed": list(PROGRAM_KEYS), "required": list(PROGRAM_REQUIRED_KEYS),
            "meaning": "Exact top-level fields accepted by the compiler. version belongs here, never inside a type. materials is optional and defaults to an empty object."},
        "type_fields": {"allowed": list(TYPE_KEYS), "required": list(TYPE_REQUIRED_KEYS),
            "exactly_one": ["cost", "cost_u"],
            "meaning": "Each type requires name and body_material, plus exactly one cost path. cost_u must include a positive quantity of body_material; all input symbols must be actual eligible solid materials at formation."},
        "definition_shape": {"key": "lowercase_key", "name": "public name", "version": "positive integer; top-level only", "materials": "optional, default {}; local symbol:{name,feedstock:wood|stone|cloth|water,load_per_u:equal feedstock,combustible:boolean,liquid:boolean}; local and imported materials combined at most4. A new object name, appearance or capability does not require a new material; builtin materials can directly name body_material and cost/cost_u inputs",
            "material_imports": "optional alias:{definition_ref:actual immutable published program reference,symbol:declared or imported material symbol}. Pins original identity/physical properties; same display names do not match. References provide neither matter nor permission; consume/produce only actual lots with mass-balanced reactions. Cannot charge imported material.",
            "contact_reactions": "optional top-level [{id,when:boolean_expression,reaction:{op:react,inputs,outputs,rate?:1..10,channel?,mode?:exclusive|independent,heat?:0}}], at most8. Each consumes at least one material declared in this package's materials; conditions only use quantity/concentration of local, imported or builtin symbols. Travels with original canonical material identity into any ordinary container; only its own positive non-burning contained lots minus escrow. Personal backpack itself never reacts; closed internal containers may. Conditions freeze before authored rules; contact runs after authored stages on shared remaining inputs, products next step. Exclusive channels are per definition_ref. Outputs preserve canonical identity and mass. A name, metadata, whole child object, surface lot or imported-only rule does not activate contact.",
            "types": "symbol:{name,body_material:builtin|local|imported solid symbol, exactly one of cost:{wood:0..4,stone:0..4,cloth:0..4} or cost_u:{solid_symbol:1..200} (total<=200u, actual sources required),components?:object matching component_schema,ports?:{name:{fields:[typed public facts],label?}},fields?:{name:{type:integer|boolean,default,min?,max?}},container_capacity?:1..200,mechanical?,recorder?,contract?,barrier?:boolean_expression,rules?:[],actions?:[]}"},
        "component_schema": {"type": "object", "additionalProperties": False, "properties": component_schema,
            "meaning": "Only these components and fields are authorable. Fields are optional overrides; instance construction supplies existing defaults. supports is derived from actual formed body lots, never an authored field."},
        "public_facts": {name: {"type": fact.type, "component": fact.component, "aggregate": fact.aggregate} for name, fact in PUBLIC_FACTS.items()},
        "new_semantics": {"fuel_uses": "local materials may declare uses:{hearth_fuel?:boolean,mechanical_fuel?:boolean}; true requires combustible non-liquid. Qualification is read from the original immutable definition, not display names or combustible alone. Builtin wood retains its existing mechanical fuel admission; hearth uses actual dry qualified lots.",
            "material_publication": "Local publication defines a new canonical material identity, properties and optional carried behavior; imports pin an existing identity. Neither publication nor import creates usable inventory or reserves feedstock. A future recipe may be published, but cost_u formation requires already existing eligible lots of each exact canonical input, including body_material. Existing builtin material can carry a newly named object's declared capabilities without declaring a new material.",
            "physical": f"{{physical:{physical_fields}}}; reads real trusted solver state, never program fields or temperature. Names and types come from public_facts keys with the physical. prefix removed. Missing/inapplicable/hidden facts are unknown; total buoyancy includes attached parts even if root has no buoyant component.",
            "ports": "{object:{port:declared_port,field:typed_fact}}; combined ports/literal sources<=8 per instance. bind_port/unbind_port require actual CAS versions and public readable targets within8 tiles of anchor and actor; they are separate from mechanical connect. Unavailable reference retains identity and retries when readable; no auto-rebind. First contract offer pins IDs/binding version/source versions; editing unaccepted bindings clears prior acceptances; active/result contracts cannot switch.",
            "unknown": "ordinary live-fact rules roll back only that declaration and wait; a blocked event declaration rolls back this instance's full declarative batch and retains its cursor to avoid replaying committed prefixes, preserving independently solved mechanics. Barrier unknown conservatively blocks. Event fields are historical; physical/port facts use trusted physical input plus the phase's actual solved mechanics, never another object's uncommitted reaction candidate or event-time physics. Existing after self fields/quantities use its frozen reaction candidate; products feed physical fact reads in a later step.",
            "drive": "{op:drive,enabled:boolean_expression} only controls this object's existing source, at the next simulation step; never writes energy, speed, position, burning or other objects. Later rule failure preserves already solved mechanical results, discarding candidate controls.",
            "fabrication": "read fabrication_options for canonical cost_u and eligible actual own lots; fabricate consumes1..8 exact entity/lot/version/quantity sources in one operation, preserves origins and remainders, binds capacity to actual body lots, creates no material source. Placement inventory, then existing drop/attach actions. Liquid cannot form a rigid body; constructed bodies require a separate disassembly process and cannot be silently consumed."},
        "mechanical": {"shape": {"role": "source|gear|actuator",
            **{name: f"{minimum}..{maximum}" for name, (minimum, maximum) in MECHANICAL_RANGES.items()},
            "load_field?": f"integer field range{MECHANICAL_RANGES['load'][0]}..{MECHANICAL_RANGES['load'][1]}",
            "speed_field?": f"integer field range{MECHANICAL_RANGES['speed'][0]}..{MECHANICAL_RANGES['speed'][1]}"},
            "required": ["role"], "defaults": dict(MECHANICAL_DEFAULTS),
            "role_semantics": {
                "source": {"used_parameters": ["teeth", "torque", "speed", "speed_field", "energy_per_step", "load", "load_field"],
                    "control": "Only source accepts finite qualified fuel and self drive/start/stop. Drive sets enabled for the following logical solve; it supplies no energy. The network must contain exactly one source, enabled and sufficiently fueled; a second source blocks it even if disabled. energy_per_step is consumed once at that source per successful solve.",
                    "motion": "Source speed, or its speed_field, establishes angular turns per step. Torque bounds the total reflected network load. Its angle advances, but its own position does not perform actuator travel; travel and pitch do not apply to this role."},
                "gear": {"used_parameters": ["teeth", "load", "load_field"],
                    "control": "Passive transmission; it cannot receive fuel or self drive/start/stop and creates no power. Its configured torque, speed/speed_field and energy_per_step do not replace the network source's parameters.",
                    "motion": "Its signed speed and angle follow the network source through meshed tooth ratios. Its own position stays unchanged; travel and pitch do not apply."},
                "actuator": {"used_parameters": ["teeth", "load", "load_field", "travel", "pitch"],
                    "control": "Passive driven output, not a source; it cannot self drive or receive fuel/start/stop. Its configured torque, speed/speed_field and energy_per_step do not supply power or set network speed.",
                    "motion": "position advances by derived signed turns*pitch, clamped to0..travel; angle still follows angular turns. Reaching or trying to cross either bound reports travel_limit. That bound does not automatically stop the source or stop its energy consumption."},
                "direction_and_load": "Each meshed link contributes the signed ratio -first.teeth/second.teeth; network path ratios determine each output's direction and speed. Actuator displacement uses that signed result, so angular motion alone does not prove position changed at a bound. Each node contributes its load or load_field times the absolute relative ratio; actuator load additionally multiplies pitch. Multiple sources, contradictory cycles, insufficient source energy or excessive reflected load stall the network and preserve positions."},
            "semantics": "meshed connection ratio=-source.teeth/target.teeth; one source per network, finite qualified fuel energy; reflected total load exceeds torque or contradictory cycle stops whole network retaining position; snapshot then solve then commit; no inertia/gravity/collision/stress"},
        "recorder": {"radius": "0..8", "window_ticks": "1..3600", "semantics": "after activation actual public events only; closed/private objects omitted; occurred_at provenance distinct from logical recorded_tick/read_tick. History and reads share a1536-byte retained window (also at most32 events and16 reads); oldest logical facts/reads leave first, recorded_count remains cumulative."},
        "contract": {"material": "declared, explicitly imported or builtin symbol; custom material identity is the original immutable definition_ref#symbol. Different definitions/authors may use the same original identity through local declaration or explicit import; matching names alone are not equivalent, and imports supply neither matter nor permission", "quantity": "1..100", "duration_ticks": "1..3600", "completion_when?": "boolean expression, delivery quantity still required", "delivery_when?": "boolean expression checked atomically before every delivery; false or unavailable fact refuses with no material transfer. A later change never retroactively permits an earlier refused delivery.", "semantics": "offer establishes participants/terms; each actor accepts own terms; second party actual material delivery; completion permits beneficiary release into their own open material-holding container. Personal backpacks hold whole objects only and cannot receive loose matter; expiry fails and permits actual payer recovery through release. Contract escrow release from a closed source container is a lawful exception; ordinary container loading/unloading still requires opening. No silent or prose consent"},
        "expressions": {"literals": "boolean|bounded integer|short string", "references": ["{field:name}", "{param:name}", "{quantity:material_symbol}", "{concentration:material_symbol}", "{mechanical:position|speed|angle|status|available_torque|energy}", "{event:kind|action|entity_id|actor|mechanical_position|passable}", "{contract:status|started_event_sequence|deadline_tick|delivered|quantity}",
            f"{{physical:{physical_fields}}}", f"{{object:{{id:actual_entity_id,field:{object_fields}}}}}",
            f"{{object:{{port:declared_port,field:{object_fields}}}}}"],
            "field_catalog": "public_facts is the shared typed catalog for both object sources. Physical self references use its physical.* keys with the physical. prefix removed; they do not introduce a separate fact catalog.",
            "object_bindings": "Use exactly one id or port. Literal IDs are immutable definition bindings, at most 8 distinct IDs. Port references require a declared port and a field listed in that port's typed fields. Only actual public objects within 8 tiles of this instance can supply facts; missing, private, closed-contained, distant objects or unsupported capabilities fail explicitly, never default false/zero. Mechanical numbers use integer projection as self references do. barrier.opened_sequence is the server public-event sequence of the actual last blocked-to-passable transition; initially 0. Compare it with this contract's started_event_sequence to require opening after bilateral acceptance.",
            "binary": "{op:eq|ne|lt|lte|gt|gte|add|sub|mul|div|min|max,left:expr,right:expr}", "boolean": "{op:all|any,args:[expr,...]} or {op:not,value:expr}", "concentration": "simplified volume shares percentage of real contained u, not physical chemical units"},
        "rule": "{id,phase:reaction|after,when:boolean_expression,effects:[...]}; phase predicates use snapshot; non-event rules once by id, then events by sequence and rule id within each event; products enter input supply next step, bounded execution",
        "action": "{id,label,parameters?:{name:{type,default,min?,max?}},when?:boolean_expression,effects:[...]}; server reserved charge/fuel/connect/.../accept cannot be overridden",
        "effects": ["{op:set,field:declared_field,value:expr}", "{op:react,inputs:{symbol:1..100},outputs:{symbol:1..100},rate:1..10,heat:-20..20,channel:family,mode:exclusive|independent}", "{op:emit,name:public_signal}", "{op:drive,enabled:boolean_expression}"],
        "reaction": "sum(quantity*load_per_u) must match. Atomic maximal extent/rate/capacity; exclusive same channel substitutes, independent channels compete finite inputs; temperature field receives bounded heat; emitted signal does not prove consent/delivery",
        "steps": ["inspect actual materials first; visible custom lots carry exact material_import {definition_ref,symbol}, copy it into material_imports under a chosen alias. Builtin wood/stone/cloth use their existing symbols. Local materials create new identities, not same-named existing lots; publication creates no matter", "publish immutable definition with definition_publish; a future missing-material recipe may be published, but it cannot yet form an object. New local material production requires an independently constructible carrier, not one that already costs the missing material", "cost_u path: read fabrication_options, obtain eligible actual own lots, then fabricate with exact source entities/versions/lot IDs/u quantities; result enters inventory, use drop/attach to place it. Never use regional resources or content_submit instead", "cost path: claim generation_context layout; actual allowed_tiles/resources/budgets apply; content_submit object {definition_ref,type_id,x,y,name?,description?} pays regional material pieces", "inspect actual actions, source item/lot candidates and versions", "interact store moves a whole child, retaining its own matter; it does not feed the parent's contained reaction inputs", "interact transfer moves an existing lot by item_id/lot_id/amount into target_slot, preserving material identity and source remainder", "rule_action charge consumes a declared symbol's feedstock to prepare that local custom material; it is not transfer or reaction evidence. fuel consumes finite qualified lots; connect links actual objects; another identity can use the same definitions/objects"],
        "time": "all simulation windows/deadlines use clock.sequence; the background timer targets roughly one-second logical steps, subject to actual service delay and late-timer handling, not per player or model turn; reading/thinking/travel do not pause finite mechanical energy use; server downtime pauses, no wall-clock catch-up",
        "security": "no arbitrary code, owner/position/security writes or author acceptance for other actors; normal human actions do not call a model"})


def specs():
    from . import fabrication
    return (
        *publication.specs(),
        *fabrication.specs(),
        FunctionSpec("grove.rule_catalog", rule_catalog, EMPTY, version=4, access="read", description="Discover the bounded creation language and shared physical facts. For immutable publications page definition_list, then definition_read; this static catalog contains no full-history index."),
        FunctionSpec("grove.definition_read", definition_read, {"type": "object", "additionalProperties": False, "required": ["definition_ref"], "properties": {"definition_ref": REF}}, version=2, access="read", description="Read one immutable definition and its author/hash, with compiled canonical material bindings and consumption/production coefficients; existing instances pin this reference."),
        FunctionSpec("grove.definition_publish", definition_publish, {"type": "object", "additionalProperties": False, "required": ["definition"], "properties": {"definition": {"type": "object"}, "delegation": DELEGATION}}, version=5, authorize=_member, description="Compile immutable definitions with cost_u actual-source bodies, bounded components/ports/physical conditions and self drive control, or the existing piece cost. Optional local material uses qualifies fuel; material_imports pins original identities. Publication creates no matter, permission or consent. Discover precise shape and bounds with rule_catalog."),
        FunctionSpec("grove.rule_action", rule_action, ACTION_ARGS, version=7, authorize=_member, description="Use inspect's formal declared actions, explicit bind_port/unbind_port, qualified fuel lot selection and finite start/stop control. Bindings need same-observation target/other versions and do not grant remote writes; active/result contracts pin targets. charge prepares only locally declared material from feedstock; actual lot transfer is interact transfer. Same identity and Runtime receipts for human or delegated Agent."),
        FunctionSpec("grove.control_grant", control_grant, {"type": "object", "additionalProperties": False, "required": ["goal", "max_actions"], "properties": {"goal": {"type": "string", "minLength": 1, "maxLength": 2000}, "max_actions": {"type": "integer", "minimum": 1, "maximum": 64}}}, authorize=_member, description="Explicitly delegate one bounded goal on the same user identity. Replaces any prior grant; max_actions counts committed writes."),
        FunctionSpec("grove.control_revoke", control_revoke, EMPTY, authorize=_member, description="Reclaim control immediately: future unexecuted delegated writes fail; completed results and receipts remain."),
        FunctionSpec("grove.control_status", control_status, EMPTY, authorize=_member, access="read", description="Read own current goal and committed action count, without exposing grant token."),
    )


def state_specs():
    return (*publication.state_specs(), *tuple(StateRule(scope, "", {}, history="metadata") for scope in (DEFINITIONS, VERSIONS, CONTROL, RECORDS, ACCOUNTING)))
