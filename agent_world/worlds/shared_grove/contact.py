"""Material-carried, bounded local reactions; no object-name or global rule pool."""
from copy import deepcopy

from agent_world.errors import RuleViolation
from agent_world.simulation import matter as sm
from agent_world.simulation.programs import evaluate, plan_reaction
from agent_world.simulation.registry import MaterialDef
from . import model as m
from .definitions import (DEFINITIONS, REGISTRY, bind_material, registry_for, retain_material_snapshots,
                          contained_quantities, reaction_available_quantities, mixture_capacity)

MAX_CANDIDATES = 8
PROCESS_RESERVE_BYTES = 2048


def _identity(pack, reference, symbol):
    if symbol in REGISTRY.materials:
        return symbol
    binding = pack.get("material_imports", {}).get(symbol)
    return (binding["definition_ref"] + "#" + binding["symbol"] if binding else reference + "#" + symbol)


def _material(ctx, identity):
    if identity in REGISTRY.materials:
        return REGISTRY.materials[identity]
    reference, symbol = identity.split("#", 1)
    source = ctx.get_state(DEFINITIONS, reference)
    if not source or symbol not in source["definition"]["materials"]:
        raise RuleViolation("接触反应引用的原始材料定义缺失。")
    spec = source["definition"]["materials"][symbol]
    return MaterialDef(identity, spec["name"], spec["load_per_u"], 4,
                       spec["combustible"], liquid=spec["liquid"])


def _quantities(row, entity):
    aliases = {identity: symbol for symbol, identity in row["bindings"].items()}
    return {aliases.get(identity, identity): amount for identity, amount in
            contained_quantities(entity, symbolic=False).items()}


def authored_outputs(ctx, entity):
    from .definitions import instance_definition
    pack, spec = instance_definition(ctx, entity)
    if not spec:
        return set()
    reference = entity["state"]["program"]["definition_ref"]
    return {_identity(pack, reference, symbol) for rule in spec["rules"] for effect in rule["effects"]
            if effect["op"] == "react" for symbol in effect["outputs"]}


def may_contact(ctx, entity, outputs=()):
    possible = {lot["material"] for lot in entity["matter"] if lot["slot"] == "contained" and lot["quantity"]}
    possible.update(outputs)
    return bool(candidates(ctx, entity, possible=possible))


def candidates(ctx, entity, *, possible=None):
    if (entity["lifecycle"] != "intact" or entity["state"].get("retired")
            or "container" not in entity["components"] or "personal" in entity["components"]):
        return []
    held = possible if possible is not None else {key for key, amount in
        reaction_available_quantities(entity, symbolic=False).items() if amount > 0}
    found = {}
    # Resolve only references carried by real local materials (or their bounded
    # prospective outputs during admission), never all world definitions.
    for identity in sorted(held):
        if "#" not in identity:
            continue
        reference, symbol = identity.split("#", 1)
        record = ctx.get_state(DEFINITIONS, reference)
        if not record or symbol not in record["definition"]["materials"]:
            continue
        pack = record["definition"]
        for rule in pack.get("contact_reactions", []):
            if symbol not in rule["reaction"]["inputs"]:
                continue
            bindings = {key: _identity(pack, reference, key) for key in
                        set(REGISTRY.materials) | set(pack["materials"]) | set(pack.get("material_imports", {}))}
            effect = {**rule["reaction"], **{group: {bindings[key]: amount for key, amount in
                       rule["reaction"][group].items()} for group in ("inputs", "outputs")}}
            found[reference, rule["id"]] = {"definition_ref": reference, "rule_id": rule["id"],
                "when": rule["when"], "bindings": bindings, "reaction": effect}
    if len(found) > MAX_CANDIDATES:
        raise RuleViolation("同槽材料的接触候选超过8条；本次入料未提交。")
    return [found[key] for key in sorted(found)]


def reserve(ctx, entity, *, incoming=()):
    """Check reachable outputs without installing dormant rules or zero lots."""
    possible = {lot["material"] for lot in entity["matter"] if lot["slot"] == "contained"
                and lot["quantity"] > 0}
    possible.update(incoming)
    all_rules = {}
    while True:
        rows = candidates(ctx, entity, possible=possible)
        previous = len(possible)
        for row in rows:
            all_rules[row["definition_ref"], row["rule_id"]] = row
            possible.update(row["reaction"]["outputs"])
        if len(all_rules) > MAX_CANDIDATES:
            raise RuleViolation("含后续产物的接触候选超过8条；本次入料未提交。")
        if len(possible) == previous:
            break
    if not all_rules and not incoming:
        return []
    probe = deepcopy(entity)
    retain_material_snapshots(probe, possible)
    for identity in incoming:
        bind_material(probe, _material(ctx, identity))
    for row in all_rules.values():
        for identity in row["reaction"]["outputs"]:
            bind_material(probe, _material(ctx, identity))
    m.normalize_entity(probe)
    # The existing normalizer reserves water. Count contact slots in that same
    # reservation rather than pretending they are real quantities or capacity.
    registry = registry_for(probe)
    future = {(key, "contained", None) for row in all_rules.values() for key in row["reaction"]["outputs"]}
    future.update((key, "contained", None) for key in incoming)
    future.update(m.future_water_slots(probe, registry))
    held = {(lot["material"], lot["slot"], lot.get("bound_to")) for lot in probe["matter"] if not lot.get("burning")}
    missing = future - held
    if len(probe["matter"]) + len(missing) > sm.MAX_LOTS:
        raise RuleViolation("8个物质槽不足以预留接触产物及真实水接触；本次入料未提交。")
    process_bytes = sm.encoded_size(probe["state"].get("program", {}).get("contact_process", {}))
    size = sm.encoded_size({k: v for k, v in probe.items() if k != "version"}) + PROCESS_RESERVE_BYTES - process_bytes + 64
    for identity, slot, bound in missing:
        lot = {"id": sm.lot_identifier(entity["id"], (identity, slot, bound, None, None)),
               "material": identity, "quantity": sm.MAX_QUANTITY, "slot": slot,
               "role": "substance", "burning": False}
        if bound:
            lot["bound_to"] = bound
        size += sm.encoded_size(lot) + 1
    if size > sm.MAX_ENTITY_BYTES:
        raise RuleViolation("物件无足够8KiB空间预留完整接触结果；本次入料未提交。")
    return list(all_rules.values())


def prepare_materials(ctx, entity, *, incoming=()):
    rows = reserve(ctx, entity, incoming=incoming)
    needed = set(incoming)
    needed.update(identity for row in rows for identity in row["reaction"]["outputs"])
    retain_material_snapshots(entity, needed)


def bind_incoming(ctx, entity, definition):
    # Metadata follows the real transfer. Final candidate admission checks the
    # actual destination slot and reserves reachable contact products there.
    retain_material_snapshots(entity, (definition.id,))
    bind_material(entity, definition)


def reserve_forest(ctx, objects, *, authored_outputs=None):
    """Reserve liquids a future child break can deliver to each ancestor.

    This is a conservative presence/slot union, not an invented transfer or
    changed spill policy. Any contained edge can make its intact container the
    recipient if a closer container breaks in the same physical phase.
    """
    authored_outputs = authored_outputs or {}
    roots = {}
    for eid, entity in objects.items():
        current = entity
        for _ in range(sm.MAX_TREE_DEPTH):
            parent = current.get("physical_parent")
            if not parent or parent["id"] not in objects:
                break
            current = objects[parent["id"]]
        roots[eid] = current["id"]
    active_roots = {roots[eid] for eid, entity in objects.items()
                    if may_contact(ctx, entity, authored_outputs.get(eid, ()))}
    active = {eid for eid in objects if roots[eid] in active_roots}
    own = {eid: reserve(ctx, entity, incoming=authored_outputs.get(eid, ())) if eid in active else []
           for eid, entity in objects.items()}
    incoming = {eid: set(identities) for eid, identities in authored_outputs.items() if eid in active}
    while True:
        changed = False
        for eid, entity in objects.items():
            if eid not in active:
                continue
            registry = registry_for(entity)
            liquids = {lot["material"] for lot in entity["matter"] if lot["slot"] == "contained"
                       and lot["quantity"] and registry.materials[lot["material"]].liquid}
            liquids.update(identity for identity in incoming.get(eid, ()) if _material(ctx, identity).liquid)
            liquids.update(identity for row in own[eid] for identity in row["reaction"]["outputs"]
                           if _material(ctx, identity).liquid)
            current = entity
            for _ in range(sm.MAX_TREE_DEPTH):
                relation = current.get("physical_parent")
                if not relation or relation["id"] not in objects:
                    break
                current = objects[relation["id"]]
                if (relation["relation"] == "contained" and current["lifecycle"] == "intact"
                        and "container" in current["components"] and sm.liquid_capacity(current)):
                    held = incoming.setdefault(current["id"], set())
                    before = len(held)
                    held.update(liquids)
                    changed |= len(held) != before
        if not changed:
            break
        # Monotonic bounded identity sets; each reserve enforces four snapshots
        # and eight reachable declarations before another propagation pass.
        for eid, identities in incoming.items():
            if identities:
                own[eid] = reserve(ctx, objects[eid], incoming=identities)
    return own


def settle(ctx, entity, snapshot, supply, tick):
    """Return one complete container candidate and accounts, or roll it back."""
    result, available_supply, accounts = deepcopy(entity), deepcopy(supply), {}
    previous = entity["state"].get("program", {}).get("contact_process", {})
    applications, channels = [], set()
    try:
        rows = candidates(ctx, snapshot)
        if not rows:
            return entity, {}, None
        for row in rows:
            context = {"quantities": _quantities(row, snapshot)}
            if not evaluate(row["when"], context):
                continue
            effect = row["reaction"]
            channel = row["definition_ref"], effect["channel"]
            if effect["mode"] == "exclusive" and channel in channels:
                continue
            if effect["mode"] == "exclusive":
                channels.add(channel)
            registry = registry_for(result)
            for identity in set(effect["inputs"]) | set(effect["outputs"]):
                registry.materials[identity] = _material(ctx, identity)
            available = reaction_available_quantities(result, supply=available_supply, symbolic=False)
            capacity = mixture_capacity(result)
            if capacity is not None:
                capacity = sm.reaction_capacity(capacity, sm.quantity(result, slot="contained"), sum(available.values()))
            planned = plan_reaction(effect, available, registry.materials, capacity,
                liquid_capacity=sm.liquid_capacity(result), liquid_held=sm.liquid_quantity(registry, result))
            if not planned["extent"]:
                continue
            for identity, amount in planned["consume"].items():
                sm.take_material(result, identity, amount)
                available_supply[identity] = available_supply.get(identity, 0) - amount
                sm.accumulate_quantity(accounts, ("sinks", identity, "contact_reaction"), amount)
            retain_material_snapshots(result, planned["produce"])
            for identity, amount in planned["produce"].items():
                bind_material(result, registry.materials[identity])
                sm.add_lot(result, identity, amount, "contained", id_factory=lambda: m.identifier("lot"),
                           merge_burning=False, merge_bindings=False)
                sm.accumulate_quantity(accounts, ("sources", identity, "contact_reaction"), amount)
            applications.append({"definition_ref": row["definition_ref"], "rule_id": row["rule_id"], "extent": planned["extent"]})
        if applications:
            process = {"status": "running" if candidates(ctx, result) else "waiting", "steps": previous.get("steps", 0) + 1,
                       "last_tick": tick, "last_applications": applications}
            if sm.encoded_size(process) > PROCESS_RESERVE_BYTES:
                raise RuleViolation("接触结果超过完整记录预算。")
            result["state"].setdefault("program", {})["contact_process"] = process
            m.normalize_entity(result)
            reserve(ctx, result)
            return result, accounts, (entity["id"], "contact_reaction", "同槽真实材料按原始不可变声明发生接触反应。")
        if previous.get("status") == "running":
            result["state"]["program"]["contact_process"]["status"] = "waiting"
        return result, {}, None
    except (ValueError, RuleViolation) as exc:
        result = deepcopy(entity)
        error = str(exc)[:180]
        result["state"].setdefault("program", {})["contact_process"] = {
            **previous, "status": "error", "error": error}
        changed = previous.get("status") != "error" or previous.get("error") != error
        return result, {}, (entity["id"], "contact_error", "该容器接触反应未成立，材料保留。") if changed else None


def projection(ctx, entity, *, detailed=False):
    rows = candidates(ctx, entity)
    process = deepcopy(entity["state"].get("program", {}).get("contact_process"))
    if not rows and not process:
        return None
    result = {"scope": "own_contained_only", "candidate_count": len(rows), "process": process,
              "products_available": "next_logical_step", "unit": "u",
              "process_is_history": True}
    if detailed:
        available = reaction_available_quantities(entity, symbolic=False)
        reactions = []
        for row in rows:
            effect = row["reaction"]
            context = {"quantities": _quantities(row, entity)}
            try:
                condition = {"known": True, "satisfied": bool(evaluate(row["when"], context))}
            except ValueError as exc:
                condition = {"known": False, "satisfied": None, "error": str(exc)[:180]}
            reactions.append({"definition_ref": row["definition_ref"], "rule_id": row["rule_id"],
                "reaction_id": row["definition_ref"] + "#" + row["rule_id"], "channel_scope": "definition_ref",
                "when": deepcopy(row["when"]), "condition": condition,
                "material_bindings": {symbol: {"material_id": identity, "name": _material(ctx, identity).name}
                                      for symbol, identity in row["bindings"].items()},
                "inputs_per_extent": deepcopy(effect["inputs"]), "outputs_per_extent": deepcopy(effect["outputs"]),
                "observed_quantities": context["quantities"],
                "observed_concentrations": {symbol: amount * 100 // max(1, sum(context["quantities"].values()))
                                            for symbol, amount in context["quantities"].items()},
                "available": {identity: available.get(identity, 0) for identity in effect["inputs"]},
                "missing_for_one_extent": {identity: max(0, amount - available.get(identity, 0))
                                           for identity, amount in effect["inputs"].items()},
                "max_extent": effect["rate"], "channel": effect["channel"], "mode": effect["mode"]})
        result["reactions"] = reactions
        history = []
        for application in (process or {}).get("last_applications", []):
            record = ctx.get_state(DEFINITIONS, application["definition_ref"])
            rule = next(row for row in record["definition"]["contact_reactions"] if row["id"] == application["rule_id"])
            effect = rule["reaction"]
            history.append({**application, "material_names": {_identity(record["definition"], application["definition_ref"], key):
                _material(ctx, _identity(record["definition"], application["definition_ref"], key)).name
                for group in ("inputs", "outputs") for key in effect[group]}, **{group: {_identity(record["definition"], application["definition_ref"], key):
                amount * application["extent"] for key, amount in effect[group].items()} for group in ("inputs", "outputs")}})
        result["last_actual_reactions"] = history
    return result
