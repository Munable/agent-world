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
from agent_world.simulation.programs import compile_program, evaluate, plan_reaction, program_material_semantics, solve_mechanical
from agent_world.world_streams import StreamEvent

from . import model as m
from .definitions import BODY_MATERIALS, MAX_CUSTOM_MATERIALS, REGISTRY, registry_for

DEFINITIONS = "grove.creative.definitions"
VERSIONS = "grove.creative.versions"
CONTROL = "grove.creative.control"
RECORDS = "grove.creative.records"
ACCOUNTING = "grove.creative.accounting"
MAX_DEFINITIONS = 32
MAX_INSTANCES = 32
MAX_LINKS = 8
MAX_RECORDS = 32
MAX_EVENTS = 256
MAX_RECORD_BYTES = 1536
MAX_EVENT_BYTES = 512
MAX_EVENT_JOURNAL_BYTES = 65536
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
        "material": {"type": "string", "minLength": 1, "maxLength": 180}, "terms_version": VERSION,
        "params": {"type": "object", "maxProperties": 16}, "delegation": DELEGATION}}


def _save(ctx, scope, key, value):
    rec = ctx.get_state_record(scope, key)
    return ctx.set_state(scope, key, value, expected_version=rec["version"])


def _bounded_record(value):
    """Retain recent public facts and reads within one reserved record size."""
    record = {"history": deepcopy(value.get("history", [])[-MAX_RECORDS:]),
              "reads": deepcopy(value.get("reads", [])[-16:])}
    while sm.encoded_size(record) > MAX_RECORD_BYTES:
        history, reads = record["history"], record["reads"]
        history_age = (history[0]["recorded_tick"], history[0]["sequence"], 0) if history else None
        read_age = (reads[0]["read_tick"], reads[0]["sequence"], 1) if reads else None
        if history and (not reads or history_age <= read_age):
            history.pop(0)
        else:
            reads.pop(0)
    return record


def _check_instance_capacity(ctx, ids):
    """Reserve a future full physical step before admitting a new instance.

    Definitions are read-only during a step. Only the admitted types' possible
    results, distinct material accounts and bounded observer histories add to
    the existing physical reservation; sharing a material account counts once.
    """
    from . import processes as physical
    from .definitions import MAX_CUSTOM_MATERIALS
    from .generation import MAX_CONTENT_BYTES, MAX_OUTCOME_BYTES

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
    burning_accounts = physical.MAX_BURNING_ENTITIES * MAX_CUSTOM_MATERIALS
    changes += physical.MAX_FRAME_CHUNKS + physical_facts + burning_accounts + 2
    journal = ctx.get_state(m.META, "creative_events", {"sequence": 0, "events": []})
    size += (physical.MAX_FRAME_CHUNKS * 2 * MAX_CONTENT_BYTES
             + physical_facts * MAX_OUTCOME_BYTES
             + burning_accounts * ACCOUNT_HISTORY_BYTES
             + max(sm.encoded_size(journal), MAX_EVENT_JOURNAL_BYTES) + MAX_EVENT_JOURNAL_BYTES
             + 2 * m.MAX_META_BYTES)
    accounts = set()
    for eid in ids:
        entity = m.get_entity(ctx, eid, required=False)
        if not entity or entity["lifecycle"] != "intact" or entity["state"].get("retired"):
            continue
        _, spec = _type(ctx, entity)
        if spec is None:
            continue
        reactions = False
        for rule in spec["rules"]:
            for effect in rule["effects"]:
                if effect["op"] != "react":
                    continue
                reactions = True
                for category, amounts in (("sinks", effect["inputs"]), ("sources", effect["outputs"])):
                    for symbol in amounts:
                        identity = _material(entity, symbol)
                        if "#" in identity:
                            accounts.add((category, identity))
        contract = int("contract" in spec)
        facts = max(int(reactions) + int("mechanical" in spec) + contract,
                    int(bool(spec["rules"]) or "barrier" in spec) + contract)
        changes += facts
        size += facts * MAX_OUTCOME_BYTES
        if "recorder" in spec:
            record = ctx.get_state(RECORDS, eid, {"history": [], "reads": []})
            changes += 1
            size += max(sm.encoded_size(record), MAX_RECORD_BYTES) + MAX_RECORD_BYTES
    changes += len(accounts)
    size += len(accounts) * ACCOUNT_HISTORY_BYTES
    if changes > MAX_COMMIT_CHANGES or size > MAX_COMMIT_BYTES:
        raise RuleViolation("声明物件组合超过完整物理步的提交容量；现有物件继续运行，本次实例创建未提交。")


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
                                     max_custom_materials=MAX_CUSTOM_MATERIALS, imported_materials=resolved)
    except ValueError as exc:
        raise RuleViolation(str(exc)) from exc
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
                                "material_semantics": semantics})
    index = ctx.get_state(m.META, "creative_definitions", [])
    if len(index) >= MAX_DEFINITIONS:
        raise RuleViolation("世界当前规则包容量为32；发布版本也占一份，不以覆盖旧版本释放。")
    _save(ctx, DEFINITIONS, ref, {"definition": definition, "author": ctx.actor_role_id, "hash": digest,
                                 "published_tick": step(ctx), "published_at": ctx.now})
    _save(ctx, VERSIONS, version_key, {"definition_ref": ref})
    _save(ctx, m.META, "creative_definitions", [*index, ref])
    return FunctionOutcome({"definition_ref": ref, "definition": definition, "already_published": False,
                            "material_semantics": semantics,
                            "instantiate": "grove.content_submit: content.objects[].definition_ref and type_id; existing opportunity and real costs apply"})


def definition_read(ctx, args):
    value = ctx.get_state(DEFINITIONS, args["definition_ref"])
    if not value:
        raise RuleViolation("规则版本不存在；先发布并使用返回的definition_ref。")
    return FunctionOutcome({"definition_ref": args["definition_ref"], **value,
        "material_semantics": program_material_semantics(value["definition"], args["definition_ref"], materials=REGISTRY.materials)})


def _type(ctx, entity):
    binding = entity["state"].get("program", {})
    if not binding.get("definition_ref"):
        return None, None
    pack = ctx.get_state(DEFINITIONS, binding["definition_ref"])
    if not pack or binding["type_id"] not in pack["definition"]["types"]:
        raise RuleViolation("物件引用的已发布规则版本缺失。")
    return pack["definition"], pack["definition"]["types"][binding["type_id"]]


def _material(entity, symbol):
    program = entity["state"]["program"]
    return symbol if symbol in REGISTRY.materials else program.get("material_aliases", {}).get(
        symbol, program["definition_ref"] + "#" + symbol)


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


def object_definition(ctx, spec):
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
    matter = [{"id": "body_" + material, "material": material, "quantity": amount * REGISTRY.materials[material].piece_quantity,
               "slot": "body", "role": "structural", "burning": False} for material, amount in kind["cost"].items() if amount]
    components = {"structure": {"max_integrity": 100, "integrity": 100, "support_limit": 1000, "overload_rate": 1}}
    capabilities = ["transport"]
    if "container_capacity" in kind or "contract" in kind or any(e["op"] == "react" for r in kind["rules"] for e in r["effects"]):
        capacity = kind.get("container_capacity", 100)
        binding["mixture_capacity"] = capacity
        components["container"] = {"slots": 4, "liquid_capacity": capacity, "open": True}
        capabilities.append("container")
    if "barrier" in kind:
        active = bool(evaluate(kind["barrier"], _context({"state": {"program": binding}, "matter": matter,
            "x": spec["x"], "y": spec["y"]}, ctx=ctx, definition=pack)))
        components["barrier"] = {"active": active}
        if active:
            capabilities.append("blocking")
    return {"name": kind["name"], "material": kind["body_material"], "capabilities": capabilities,
            "state": {"kind": "authored", "program": binding}, "matter": matter, "components": components,
            "composition": {"material": kind["body_material"], "construction": "bundle", "size": 1},
            "material_cost": deepcopy(kind["cost"]), "material_units": sum(kind["cost"].values()),
            "water_capacity": components.get("container", {}).get("liquid_capacity", 0)}


def register_instance(ctx, entity):
    if not entity["state"].get("program", {}).get("definition_ref"):
        return
    ids = ctx.get_state(m.META, "creative_instances", [])
    if entity["id"] not in ids:
        if len(ids) >= MAX_INSTANCES:
            raise RuleViolation("同时最多32个声明程序物件；已耗尽／退役物件会在下一步释放执行位。")
        proposed = [*ids, entity["id"]]
        _check_instance_capacity(ctx, proposed)
        _save(ctx, m.META, "creative_instances", proposed)
        from .processes import _physical_closure
        _physical_closure(ctx)


def _quantities(entity, *, symbolic=True):
    values = {}
    prefix = entity["state"].get("program", {}).get("definition_ref", "") + "#"
    aliases = {identity: alias for alias, identity in entity["state"].get("program", {}).get("material_aliases", {}).items()}
    for lot in entity["matter"]:
        if lot["slot"] != "contained":
            continue
        key = lot["material"]
        if symbolic:
            key = aliases.get(key, key[len(prefix):] if key.startswith(prefix) else key)
        values[key] = values.get(key, 0) + lot["quantity"]
    return values


def _bound_ids(value):
    """Literal object bindings are part of the immutable program, never a query."""
    if isinstance(value, dict):
        if set(value) == {"object"}:
            return {value["object"]["id"]}
        return set().union(*(_bound_ids(child) for child in value.values()))
    if isinstance(value, list):
        return set().union(*(_bound_ids(child) for child in value))
    return set()


def _context(entity, params=None, event=None, *, ctx=None, objects=None, definition=None):
    program = entity["state"]["program"]
    observed = {}
    if ctx is not None:
        definition = definition if definition is not None else _type(ctx, entity)[0]
        location = _public_location(ctx, entity) if entity.get("id") else (entity.get("x"), entity.get("y"))
        for eid in sorted(_bound_ids(definition or {})):
            target = objects.get(eid) if objects is not None and eid in objects else m.get_entity(ctx, eid, required=False)
            target_location = _public_location(ctx, target) if target else None
            if (location is None or None in location or target_location is None or
                    max(abs(location[0]-target_location[0]), abs(location[1]-target_location[1])) > 8):
                continue
            value = {"intact": target["lifecycle"] == "intact"}
            binding = target["state"].get("program", {})
            if "barrier" in target["components"]:
                value.update({"barrier.active": target["components"]["barrier"]["active"],
                              "barrier.opened_sequence": binding.get("barrier_opened_sequence", 0)})
            if "mechanical" in binding:
                value.update({"mechanical." + key: val if key == "status" else int(val)
                              for key, val in binding["mechanical"].items()
                              if key in {"position", "speed", "angle", "energy", "status"}})
            observed[eid] = value
    contract = program.get("contract", {})
    return {"fields": program["fields"], "params": params or {}, "quantities": _quantities(entity), "objects": observed,
            "contract": {"status": contract.get("status", "none"), "started_event_sequence": contract.get("started_event_sequence", 0),
                         "deadline_tick": contract.get("deadline_tick") or 0, "delivered": contract.get("delivered", 0), "quantity": contract.get("quantity", 0)},
            "mechanical": program.get("mechanical", {"position": 0, "angle": 0, "speed": 0, "energy": 0, "status": "none", "available_torque": 0}),
            "event": {"kind": "none", "action": "", "entity_id": "", "actor": "", "mechanical_position": 0, "passable": False, **(event or {})}}


def _take(entity, material, amount, *, slots=("contained",)):
    lots = [lot for lot in entity["matter"] if lot["material"] == material and lot["slot"] in slots and not lot["burning"]]
    if sum(lot["quantity"] for lot in lots) < amount:
        raise RuleViolation("实际可用材料不足；没有生成或扣除任何产物。")
    left = amount
    for lot in sorted(lots, key=lambda lot: lot["id"]):
        take = min(left, lot["quantity"])
        lot["quantity"] -= take
        left -= take
    entity["matter"] = [lot for lot in entity["matter"] if lot["quantity"]]


def _put(entity, material, amount):
    if not amount:
        return
    lot = next((lot for lot in entity["matter"] if lot["material"] == material and lot["slot"] == "contained" and not lot["burning"]), None)
    if lot:
        lot["quantity"] += amount
    else:
        if len(entity["matter"]) >= sm.MAX_LOTS:
            raise RuleViolation("产物将超过每物件8批次容量；该原子反应未执行。")
        entity["matter"].append({"id": m.identifier("matter"), "material": material, "quantity": amount,
                                "slot": "contained", "role": "substance", "burning": False})


def _effects(ctx, entity, kind, effects, *, params=None, event=None, channels=None, accounts=None, supply=None, objects=None):
    changed, reactions = False, []
    channels = set() if channels is None else channels
    supply = deepcopy(_quantities(entity)) if supply is None else supply
    contract = entity["state"]["program"].get("contract")
    reserved = {}
    if contract and contract["delivered"] > contract.get("released", 0):
        symbol = next((alias for alias, identity in entity["state"]["program"].get("material_aliases", {}).items()
                       if identity == contract["material"]), contract["material"].split("#", 1)[-1])
        reserved[symbol] = contract["delivered"] - contract.get("released", 0)
    def account(category, material, amount):
        if accounts is None:
            m._record_account(ctx, category, material, amount, "creative_reaction")
        else:
            accounts.append((category, material, amount, "creative_reaction"))
    for effect in effects:
        context = _context(entity, params, event, ctx=ctx, objects=objects)
        if effect["op"] == "set":
            value = evaluate(effect["value"], context)
            field = kind["fields"][effect["field"]]
            if field["type"] == "integer" and not field["min"] <= value <= field["max"]:
                raise RuleViolation("规则字段" + effect["field"] + "结果越界，请修改表达式。")
            changed |= entity["state"]["program"]["fields"][effect["field"]] != value
            entity["state"]["program"]["fields"][effect["field"]] = value
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
            usable = deepcopy(entity)
            usable["matter"] = [lot for lot in entity["matter"] if not lot["burning"]]
            usable_quantities = _quantities(usable)
            available = {key: max(0, min(amount, usable_quantities.get(key, 0)) - reserved.get(key, 0)) for key, amount in supply.items()}
            actual_capacity = entity["components"].get("container", {}).get("liquid_capacity", 0)
            # Planner supply excludes this phase's products and escrow, while
            # those real lots still occupy volume.
            capacity = actual_capacity - sum(context["quantities"].values()) + sum(available.values())
            planned = plan_reaction(effect, available, materials, max(0, capacity))
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


def _public_location(ctx, entity):
    current, seen = entity, set()
    for _ in range(9):
        if current["id"] in seen or current.get("owner"):
            return None
        seen.add(current["id"])
        parent = current.get("physical_parent")
        if not parent:
            return m.entity_location(ctx, entity) if m.is_ground(current) else None
        current = m.get_entity(ctx, parent["id"])
        if parent["relation"] == "contained" and not current["state"]["open"]:
            return None
    return None


def public_event(ctx, *, action, entity_id=None, x=None, y=None, kind="action", actor=None):
    """Append only minimal server-observed public facts, never private arguments."""
    if entity_id:
        entity = m.get_entity(ctx, entity_id, required=False)
        location = _public_location(ctx, entity) if entity else None
        if location is None:
            return
        x, y = location
    if x is None or y is None:
        return
    journal = getattr(ctx, "_grove_event_batch", None)
    if journal is None:
        journal = ctx.get_state(m.META, "creative_events", {"sequence": 0, "events": []})
    sequence = journal["sequence"] + 1
    event = {"sequence": sequence, "tick": step(ctx), "occurred_at": ctx.now, "kind": kind,
             "action": action or "", "entity_id": entity_id or "", "actor": actor or ctx.actor_role_id,
             "x": x, "y": y, "mechanical_position": 0, "passable": False}
    if entity_id and entity:
        binding = entity["state"].get("program", {})
        if "mechanical" in binding:
            event["mechanical_position"] = int(binding["mechanical"]["position"])
        if "barrier" in entity["components"]:
            event["passable"] = entity["lifecycle"] == "intact" and not entity["components"]["barrier"]["active"]
        if binding.pop("barrier_open_pending", False):
            binding["barrier_opened_sequence"] = sequence
            m.save_entity(ctx, entity)
    if sm.encoded_size(event) > MAX_EVENT_BYTES:
        raise RuleViolation("公开事件超过单条事实容量；本次行动未提交。")
    cursors = []
    for eid in ctx.get_state(m.META, "creative_instances", []):
        consumer = m.get_entity(ctx, eid, required=False)
        if not consumer or consumer["lifecycle"] != "intact" or consumer["state"]["program"]["process"]["status"] == "error":
            continue
        _, spec = _type(ctx, consumer)
        if _has_event(spec):
            cursors.append(consumer["state"]["program"].get("event_cursor", 0))
    consumed = min(cursors, default=journal["sequence"])
    pending = [old for old in journal["events"] if old["sequence"] > consumed]
    limit = MAX_EVENTS if kind == "process" else MAX_EVENTS - 164
    journal = {"sequence": sequence, "events": [*pending, event], "consumed_through": consumed}
    if len(pending) >= limit or sm.encoded_size(journal) > MAX_EVENT_JOURNAL_BYTES:
        raise StateConflict("本逻辑步公开事件处理预算已满，请待下一世界步读取结果再继续；本次行动未提交。")
    if hasattr(ctx, "_grove_event_batch"):
        ctx._grove_event_batch = journal
    else:
        _save(ctx, m.META, "creative_events", journal)
    for eid in ctx.get_state(m.META, "creative_instances", []):
        recorder = m.get_entity(ctx, eid, required=False)
        if not recorder or recorder["lifecycle"] != "intact":
            continue
        pack, spec = _type(ctx, recorder)
        state = recorder["state"]["program"].get("recorder")
        location = _public_location(ctx, recorder)
        if not state or not state["active"] or location is None or step(ctx) > state["until_tick"]:
            continue
        if max(abs(x-location[0]), abs(y-location[1])) > spec["recorder"]["radius"]:
            continue
        batch = getattr(ctx, "_grove_record_batch", None)
        record = batch.get(eid) if batch is not None else None
        if record is None:
            record = ctx.get_state(RECORDS, eid, {"history": [], "reads": []})
        record["history"] = [*record["history"], {**event, "recorded_tick": step(ctx)}][-MAX_RECORDS:]
        record = _bounded_record(record)
        if batch is not None:
            batch[eid] = record
        else:
            _save(ctx, RECORDS, eid, record)
        state["recorded_count"] += 1
        m.save_entity(ctx, recorder)


def _has_event(value):
    if isinstance(value, dict):
        return (set(value) == {"event"}) or any(_has_event(child) for child in value.values())
    return isinstance(value, list) and any(_has_event(child) for child in value)


def _phase_applications(spec, phase, events):
    rules = sorted((rule for rule in spec["rules"] if rule["phase"] == phase), key=lambda rule: rule["id"])
    # Ordinary phase rules run once; event reactions preserve causal journal
    # order across definitions, with rule id breaking ties within one event.
    for rule in rules:
        if not _has_event(rule):
            yield rule, None
    for fact in events:
        for rule in rules:
            if _has_event(rule):
                yield rule, fact


def _relevant_events(ctx, entity, since):
    location = _public_location(ctx, entity)
    if location is None:
        return []
    _, spec = _type(ctx, entity)
    radius = spec.get("recorder", {}).get("radius", 1)
    events = ctx.get_state(m.META, "creative_events", {"events": []})["events"]
    return [event for event in events if event["sequence"] > since and max(abs(event["x"]-location[0]), abs(event["y"]-location[1])) <= radius]


def _barrier(entity, spec, event=None, *, ctx=None, objects=None):
    if "barrier" in spec:
        previous = entity["components"]["barrier"]["active"]
        current = bool(evaluate(spec["barrier"], _context(entity, event=event, ctx=ctx, objects=objects)))
        entity["components"]["barrier"]["active"] = current
        if previous and not current:
            entity["state"]["program"]["barrier_open_pending"] = bool(ctx is not None and _public_location(ctx, entity) is not None)
        elif current:
            entity["state"]["program"].pop("barrier_open_pending", None)


def _delivery_reason(ctx, entity, spec):
    """Evaluate at admission, before material transfer. Missing facts fail closed."""
    try:
        if not evaluate(spec["contract"].get("delivery_when", True), _context(entity, ctx=ctx)):
            return "交付条件当前未满足；本次材料没有转移。"
    except ValueError as exc:
        return "交付条件无法读取公开事实：" + str(exc)[:140]
    return None


def physical_contract_failure(ctx, entity, *, tick=None):
    """Reconcile a real broken body's surviving escrow in its physical commit.

    Loss records an unavailable contract claim, not a new material sink. Existing
    physical rules already account for burnt, spilled or otherwise moved lots.
    Completion preserves beneficiary rights; unfinished terms cannot complete
    after the supporting object has failed. Surface lots remain recoverable.
    """
    state = entity.get("state", {}).get("program", {}).get("contract")
    if not state or entity.get("lifecycle") not in {"broken", "consumed"}:
        return False
    previous = deepcopy(state)
    current_tick = step(ctx) if tick is None else tick
    state.setdefault("physical_failure_tick", current_tick)
    if state["status"] != "complete":
        state["status"] = "failed"
        state.setdefault("finished_tick", current_tick)
        state["failure_reason"] = "physical_damage"
    outstanding = max(0, state["delivered"] - state.get("released", 0) - state.get("lost", 0))
    surviving = sum(lot["quantity"] for lot in entity.get("matter", [])
                    if lot["material"] == state["material"] and lot["slot"] in {"contained", "surface"})
    state["lost"] = state.get("lost", 0) + max(0, outstanding - surviving)
    state["escrow_remaining"] = min(outstanding, surviving)
    return state != previous


def _contract_update(ctx, entity, spec, event=None, *, objects=None):
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
        if _has_event(condition):
            pending = event if isinstance(event, list) else [event] if event else []
            for fact in pending:
                if fact["sequence"] > state.get("started_event_sequence", 0) and evaluate(condition, _context(entity, event=fact, ctx=ctx, objects=objects)):
                    state["completion_proof"] = {key: fact[key] for key in ("sequence", "tick", "action", "entity_id", "actor")}
            fulfilled = bool(state.get("completion_proof"))
        else:
            fulfilled = evaluate(condition, _context(entity, ctx=ctx, objects=objects))
        state.pop("condition_error", None)
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
    ids = ctx.get_state(m.META, "creative_instances", [])
    # Logical-step facts and recorder histories are one commit, not one state
    # write per event/observer pair. Instance admission reserves their complete
    # combination with physics; history growth cannot exceed that reservation.
    ctx._grove_event_batch = None
    ctx._grove_record_batch = {}
    objects = {eid: entity for eid in ids if (entity := m.get_entity(ctx, eid, required=False))
               and entity["lifecycle"] == "intact" and not entity["state"].get("retired")}
    if list(objects) != ids:
        _save(ctx, m.META, "creative_instances", list(objects))
    specs = {eid: _type(ctx, entity)[1] for eid, entity in objects.items()}
    original = deepcopy(objects)
    journal_sequence = ctx.get_state(m.META, "creative_events", {"sequence": 0})["sequence"]
    events, pending_accounts, phase_supply = {}, {}, {}
    outcomes = []

    def fail_instance(eid, error, preserve_mechanical=False):
        # Declarative evaluation runs on local copies. No account or object
        # write has occurred for this instance, so its entire step can stop.
        mechanical_result = deepcopy(objects[eid]["state"]["program"].get("mechanical")) if preserve_mechanical else None
        objects[eid] = deepcopy(original[eid])
        program = objects[eid]["state"]["program"]
        program["process"].update(status="error", error=str(error)[:180], last_tick=step(ctx))
        if mechanical_result is not None:
            program["mechanical"] = mechanical_result
        if "mechanical" in program:
            program["mechanical"].update(speed=0, status="program_error", enabled=False)
        pending_accounts[eid] = []
        outcomes[:] = [item for item in outcomes if item[0] != eid]
        outcomes.append((eid, "program_error", "该物件规则暂停：" + str(error)[:120]))

    for eid, entity in sorted(objects.items()):
        state = entity["state"]["program"]
        relevant = _relevant_events(ctx, entity, state.get("event_cursor", 0))
        event = relevant[-1] if relevant else None
        events[eid] = relevant
        pending_accounts[eid] = []
        phase_supply[eid] = deepcopy(_quantities(entity))
        if state["process"]["status"] == "error":
            continue
        try:
            channels, reactions = set(), []
            for rule, fact in _phase_applications(specs[eid], "reaction", relevant):
                if evaluate(rule["when"], _context(original[eid], event=fact, ctx=ctx, objects=original)):
                    _, produced = _effects(ctx, entity, specs[eid], rule["effects"], event=fact, channels=channels,
                                           accounts=pending_accounts[eid], supply=phase_supply[eid], objects=original)
                    reactions.extend(produced)
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
            if _has_event(specs[eid]):
                state["event_cursor"] = journal_sequence
            m.normalize_entity(entity)
        except (ValueError, RuleViolation) as exc:
            fail_instance(eid, exc)

    # Relations, inputs and loads come from the step's original snapshot. A
    # carried, closed, distant or asymmetrically disconnected pair is no mesh.
    nodes, edges = [], []
    for eid, entity in sorted(original.items()):
        if "mechanical" not in specs[eid] or _public_location(ctx, entity) is None or objects[eid]["state"]["program"]["process"]["status"] == "error":
            continue
        state = deepcopy(entity["state"]["program"]["mechanical"])
        mechanical = deepcopy(specs[eid]["mechanical"])
        if mechanical["role"] == "source" and not state["enabled"]:
            state["energy"] = 0
        nodes.append({"id": eid, "mechanical": mechanical, "fields": entity["state"]["program"]["fields"], "state": state})
        for link in entity["state"]["program"].get("connections", []):
            other = original.get(link["other_id"])
            if not other or eid >= other["id"] or _public_location(ctx, other) is None:
                continue
            if not any(reverse["other_id"] == eid for reverse in other["state"]["program"].get("connections", [])):
                continue
            first, second = m.entity_location(ctx, entity), m.entity_location(ctx, other)
            if max(abs(first[0]-second[0]), abs(first[1]-second[1])) <= 2:
                edges.append({"source": eid, "target": other["id"], "meshed": True})
    eligible = {node["id"] for node in nodes}
    edges = [edge for edge in edges if edge["source"] in eligible and edge["target"] in eligible]
    results = solve_mechanical(nodes, edges) if nodes else {}
    for eid, result in results.items():
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
    after_objects = deepcopy(objects)
    for entity in original.values():
        definition, _ = _type(ctx, entity)
        for bound_id in sorted(_bound_ids(definition)):
            if bound_id not in after_objects:
                after_objects[bound_id] = m.get_entity(ctx, bound_id, required=False)
    for eid, entity in sorted(objects.items()):
        spec = specs[eid]
        if entity["state"]["program"]["process"]["status"] != "error":
            try:
                phase_snapshot = after_objects[eid]
                channels = set()
                for rule, fact in _phase_applications(spec, "after", events[eid]):
                    if evaluate(rule["when"], _context(phase_snapshot, event=fact, ctx=ctx, objects=after_objects)):
                        _effects(ctx, entity, spec, rule["effects"], event=fact, channels=channels,
                                 accounts=pending_accounts[eid], supply=phase_supply[eid], objects=after_objects)
                _barrier(entity, spec, events[eid][-1] if events[eid] else None, ctx=ctx, objects=after_objects)
                m.normalize_entity(entity)
            except (ValueError, RuleViolation) as exc:
                fail_instance(eid, exc, preserve_mechanical=True)
                entity = objects[eid]
        recorder = entity["state"]["program"].get("recorder")
        if recorder and recorder["active"] and step(ctx) >= recorder["until_tick"]:
            recorder["active"] = False
        # Deadlines still advance even if this instance's authored rule stopped.
        # Cross-object contract conditions retain the step-start read view;
        # after/physical feedback does not move contract completion earlier.
        if _contract_update(ctx, entity, spec, events[eid], objects=original):
            outcomes.append((eid, "contract", "契约依据真实交付和逻辑期限形成结果。"))
    accounts = {}
    for entries in pending_accounts.values():
        for category, material, amount, reason in entries:
            key = category, material, reason
            accounts[key] = accounts.get(key, 0) + amount
    for (category, material, reason), amount in sorted(accounts.items()):
        m._record_account(ctx, category, material, amount, reason)
    for eid, entity in sorted(objects.items()):
        if entity != original[eid]:
            m.save_entity(ctx, entity)
    for eid, action, summary in outcomes:
        public_event(ctx, action=action, entity_id=eid, kind="process", actor="system:rules")
    if ctx._grove_event_batch is not None:
        _save(ctx, m.META, "creative_events", ctx._grove_event_batch)
    for eid, record in sorted(ctx._grove_record_batch.items()):
        _save(ctx, RECORDS, eid, record)
    del ctx._grove_event_batch
    del ctx._grove_record_batch
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
        m.retire_material(ctx, item, target["id"])
    else:
        m.save_entity(ctx, item)


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
        raise RuleViolation("此物件没有可执行的声明规则。")
    program, action = entity["state"]["program"], args["action"]
    summary = ""
    result_extra = {}
    _contract_update(ctx, entity, spec)
    if action == "charge":
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
        if sum(_quantities(entity).values()) + amount > entity["components"]["container"]["liquid_capacity"]:
            raise RuleViolation("实际混合物超过定义容积；来源没有消耗。")
        _take(item, definition["feedstock"], amount, slots=("body", "contained", "surface"))
        material = _material(entity, args["material"])
        _put(entity, material, amount)
        m._record_account(ctx, "sinks", definition["feedstock"], amount, "creative_charge")
        m._record_account(ctx, "sources", material, amount, "creative_charge")
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
            _take(item, "wood", amount, slots=("body", "contained", "surface"))
            state["energy"] += amount
            m._record_account(ctx, "sinks", "wood", amount, "mechanical_fuel")
            _finish_item(ctx, item, entity)
            summary = f"燃料已消耗，储存了{amount}单位有限能源。"
        else:
            state["enabled"] = action == "start"
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
        m.save_entity(ctx, other)
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
                record = _bounded_record(record)
                _save(ctx, RECORDS, entity["id"], record)
            result_extra["records"] = _bounded_record(record)
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
                    m.transfer_material(ctx, item, entity, lot["id"], take, reason="contract_delivery")
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
                        m.transfer_material(ctx, entity, item, lot["id"], take, reason="contract_release")
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
        if not evaluate(declared.get("when", True), _context(entity, params, ctx=ctx)):
            raise RuleViolation("此动作的实际前提尚未成立。")
        _effects(ctx, entity, spec, declared["effects"], params=params)
        program["process"].update(status="idle", last_tick=step(ctx))
        program["process"].pop("error", None)
        summary = "已执行已发布动作：" + declared["label"] + "。"
    if entity["lifecycle"] == "intact":
        _barrier(entity, spec, ctx=ctx)
    m.save_entity(ctx, entity)
    location = _public_location(ctx, entity)
    events = ()
    if location is not None:
        m.record_change(ctx, summary, x=location[0], y=location[1], entity_id=entity["id"], action=action)
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
        record = _bounded_record(ctx.get_state(RECORDS, entity["id"], {"history": [], "reads": []}))
        result["history"] = record["history"]
        result["reads"] = record["reads"]
    from .observations import program_observation
    result.update(program_observation(ctx, entity, detailed=detailed))
    if detailed:
        pack, _ = _type(ctx, entity)
        result["material_semantics"] = program_material_semantics(pack, binding["definition_ref"], materials=REGISTRY.materials,
                                                                 type_id=binding["type_id"])
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
    if "mechanical" in spec:
        add("connect", "连接传动", {"others": mechanicals})
        links = {item["other_id"] for item in entity["state"]["program"]["connections"]}
        add("disconnect", "断开传动", {"others": [item for item in mechanicals if item["id"] in links]})
        if spec["mechanical"]["role"] == "source":
            add("fuel", "消耗木料储能", {"items": own_items}, amount=True)
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
    definitions = []
    for ref in ctx.get_state(m.META, "creative_definitions", []):
        pack = ctx.get_state(DEFINITIONS, ref)
        definitions.append({"definition_ref": ref, "author": pack["author"], "key": pack["definition"]["key"],
                            "version": pack["definition"]["version"], "types": {key: {"name": kind["name"], "cost": kind["cost"]} for key, kind in pack["definition"]["types"].items()}})
    return FunctionOutcome({"language_version": 1, "definitions": definitions, "world_tick": step(ctx),
        "limits": {"packages": MAX_DEFINITIONS, "instances": MAX_INSTANCES, "types_per_package": 8, "materials_per_package": 4, "rules_per_type": 8,
                   "effects_per_rule": 8, "expression_depth": 8, "expression_nodes": 64, "links_per_object": MAX_LINKS, "records_per_object": MAX_RECORDS,
                   "record_bytes_per_object": MAX_RECORD_BYTES, "event_bytes": MAX_EVENT_BYTES,
                   "event_journal_bytes": MAX_EVENT_JOURNAL_BYTES,
                   "instance_admission": "types' combined future physical-step records must fit 512 changes and 2MiB before instance creation; definition publication alone consumes no execution slot"},
        "definition_shape": {"key": "lowercase_key", "name": "public name", "version": "positive integer", "materials": "local symbol:{name,feedstock:wood|stone|cloth|water,load_per_u:equal feedstock,combustible:boolean,liquid:boolean}; local and imported materials combined at most4",
            "material_imports": "optional alias:{definition_ref:actual immutable published program reference,symbol:declared or imported material symbol}. Pins original identity/physical properties; same display names do not match. References provide neither matter nor permission; consume/produce only actual lots with mass-balanced reactions. Cannot charge imported material.",
            "types": "symbol:{name,body_material:wood|stone|cloth,cost:{wood:0..4,stone:0..4,cloth:0..4},fields?:{name:{type:integer|boolean,default,min?,max?}},container_capacity?:1..200,mechanical?,recorder?,contract?,barrier?:boolean_expression,rules?:[],actions?:[]}"},
        "mechanical": {"shape": {"role": "source|gear|actuator", "teeth": "1..100", "torque": "1..100", "speed": "1..10", "energy_per_step": "1..100", "load": "0..100", "travel": "1..100", "pitch": "1..10", "load_field?": "integer field range0..100", "speed_field?": "integer field range1..10"},
            "semantics": "meshed connection ratio=-source.teeth/target.teeth; one source per network, finite wood fuel energy; reflected total load exceeds torque or contradictory cycle stops whole network retaining position; snapshot then solve then commit; no inertia/gravity/collision/stress"},
        "recorder": {"radius": "0..8", "window_ticks": "1..3600", "semantics": "after activation actual public events only; closed/private objects omitted; occurred_at provenance distinct from logical recorded_tick/read_tick. History and reads share a1536-byte retained window (also at most32 events and16 reads); oldest logical facts/reads leave first, recorded_count remains cumulative."},
        "contract": {"material": "declared, explicitly imported or builtin symbol; custom material identity is the original immutable definition_ref#symbol. Different definitions/authors may use the same original identity through local declaration or explicit import; matching names alone are not equivalent, and imports supply neither matter nor permission", "quantity": "1..100", "duration_ticks": "1..3600", "completion_when?": "boolean expression, delivery quantity still required", "delivery_when?": "boolean expression checked atomically before every delivery; false or unavailable fact refuses with no material transfer. A later change never retroactively permits an earlier refused delivery.", "semantics": "offer establishes participants/terms; each actor accepts own terms; second party actual material delivery; completion permits beneficiary release into their own open material-holding container. Personal backpacks hold whole objects only and cannot receive loose matter; expiry fails and permits actual payer recovery through release. Contract escrow release from a closed source container is a lawful exception; ordinary container loading/unloading still requires opening. No silent or prose consent"},
        "expressions": {"literals": "boolean|bounded integer|short string", "references": ["{field:name}", "{param:name}", "{quantity:material_symbol}", "{concentration:material_symbol}", "{mechanical:position|speed|angle|status|available_torque|energy}", "{event:kind|action|entity_id|actor|mechanical_position|passable}", "{contract:status|started_event_sequence|deadline_tick|delivered|quantity}", "{object:{id:actual_entity_id,field:intact|barrier.active|barrier.opened_sequence|mechanical.position|mechanical.speed|mechanical.angle|mechanical.energy|mechanical.status}}"],
            "object_bindings": "Literal IDs are immutable definition bindings, at most 8 distinct IDs. Only actual public objects within 8 tiles of this instance can supply facts; missing, private, closed-contained, distant objects or unsupported capabilities fail explicitly, never default false/zero. Mechanical numbers use integer projection as self references do. barrier.opened_sequence is the server public-event sequence of the actual last blocked-to-passable transition; initially 0. Compare it with this contract's started_event_sequence to require opening after bilateral acceptance.",
            "binary": "{op:eq|ne|lt|lte|gt|gte|add|sub|mul|div|min|max,left:expr,right:expr}", "boolean": "{op:all|any,args:[expr,...]} or {op:not,value:expr}", "concentration": "simplified volume shares percentage of real contained u, not physical chemical units"},
        "rule": "{id,phase:reaction|after,when:boolean_expression,effects:[...]}; phase predicates use snapshot; non-event rules once by id, then events by sequence and rule id within each event; products enter input supply next step, bounded execution",
        "action": "{id,label,parameters?:{name:{type,default,min?,max?}},when?:boolean_expression,effects:[...]}; server reserved charge/fuel/connect/.../accept cannot be overridden",
        "effects": ["{op:set,field:declared_field,value:expr}", "{op:react,inputs:{symbol:1..100},outputs:{symbol:1..100},rate:1..10,heat:-20..20,channel:family,mode:exclusive|independent}", "{op:emit,name:public_signal}"],
        "reaction": "sum(quantity*load_per_u) must match. Atomic maximal extent/rate/capacity; exclusive same channel substitutes, independent channels compete finite inputs; temperature field receives bounded heat; emitted signal does not prove consent/delivery",
        "steps": ["publish immutable definition with definition_publish", "claim generation_context layout; actual allowed_tiles/resources/budgets apply", "content_submit object {definition_ref,type_id,x,y,name?,description?} consumes real body materials", "inspect actual actions, source item/lot candidates and versions", "interact store moves a whole child, retaining its own matter; it does not feed the parent's contained reaction inputs", "interact transfer moves an existing lot by item_id/lot_id/amount into target_slot, preserving material identity and source remainder", "rule_action charge consumes a declared symbol's feedstock to prepare that custom material; it is not transfer or reaction evidence. fuel consumes finite wood; connect links actual objects; another identity can use the same definitions/objects"],
        "time": "all simulation windows/deadlines use clock.sequence; server downtime pauses, no wall-clock catch-up",
        "security": "no arbitrary code, owner/position/security writes or author acceptance for other actors; normal human actions do not call a model"})


def specs():
    return (
        FunctionSpec("grove.rule_catalog", rule_catalog, EMPTY, access="read", description="Discover the exact bounded program language, published immutable definitions, physical semantics and actual publication path."),
        FunctionSpec("grove.definition_read", definition_read, {"type": "object", "additionalProperties": False, "required": ["definition_ref"], "properties": {"definition_ref": REF}}, version=2, access="read", description="Read one immutable definition and its author/hash, with compiled canonical material bindings and consumption/production coefficients; existing instances pin this reference."),
        FunctionSpec("grove.definition_publish", definition_publish, {"type": "object", "additionalProperties": False, "required": ["definition"], "properties": {"definition": {"type": "object"}, "delegation": DELEGATION}}, version=3, authorize=_member, description="Compile and publish bounded immutable conditions, actions, reactions, processes and relations, returning actual canonical material bindings and consumption/production coefficients. Optional material_imports pins an existing published definition_ref/symbol's original material identity and properties; at most4 local/imported materials combined. No matter, ownership or consent created; mass-balanced reactions use actual lots."),
        FunctionSpec("grove.rule_action", rule_action, ACTION_ARGS, version=4, authorize=_member, description="Use published actions and finite resources with current inspect options, conditions and versions. charge consumes only a locally declared symbol's feedstock, never an imported material; it does not transfer an existing lot or prove a reaction. Use grove.interact transfer for actual immutable lots, store for whole children. Contract release requires your own open material-holding container; personal backpacks hold whole objects only and cannot receive loose matter. Distance refusal identifies target, item or other. Same actor human or explicitly delegated Agent."),
        FunctionSpec("grove.control_grant", control_grant, {"type": "object", "additionalProperties": False, "required": ["goal", "max_actions"], "properties": {"goal": {"type": "string", "minLength": 1, "maxLength": 2000}, "max_actions": {"type": "integer", "minimum": 1, "maximum": 64}}}, authorize=_member, description="Explicitly delegate one bounded goal on the same user identity. Replaces any prior grant; max_actions counts committed writes."),
        FunctionSpec("grove.control_revoke", control_revoke, EMPTY, authorize=_member, description="Reclaim control immediately: future unexecuted delegated writes fail; completed results and receipts remain."),
        FunctionSpec("grove.control_status", control_status, EMPTY, authorize=_member, access="read", description="Read own current goal and committed action count, without exposing grant token."),
    )


def state_specs():
    return tuple(StateRule(scope, "", {}, history="metadata") for scope in (DEFINITIONS, VERSIONS, CONTROL, RECORDS, ACCOUNTING))
