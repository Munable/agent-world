"""Reusable material/relationship rules; no names, templates or world IDs."""
from .effects import Break, Damage, Move, SetBurning, Sink, Source, SpillSink, SpillTransfer, Transfer
from .matter import absorption_targets, component_capacity, exposed, load_summary, lot_moisture, quantity
from .registry import RuleSpec


def water_rule(snapshot):
    effects = []
    for entry in snapshot.inputs:
        if entry["quantity"]:
            effects.append(Source(entry["target"], entry["material"], entry["quantity"], entry["slot"], entry["reason"], entry.get("bound_to")))
    for entity_id, entity in sorted(snapshot.objects.items()):
        if entity.get("lifecycle") == "consumed" or entity["state"].get("retired"):
            continue
        remaining = (0 if entity_id in snapshot.wet_by_rain else 2)+(6 if entity_id in snapshot.heated else 0)
        absorption = entity["components"].get("absorbent", {}).get("rate", 0)
        targets = [[key, capacity] for key, capacity in absorption_targets(entity, snapshot.objects)]
        for target in targets:
            target[1] = max(0, target[1]-sum(entry["quantity"] for entry in snapshot.inputs if entry["target"] == entity_id and entry["slot"] == "absorbed" and entry.get("bound_to") == target[0]))
        for lot in entity["matter"]:
            if lot["material"] != "water" or lot["slot"] not in ("absorbed", "surface"):
                continue
            left = lot["quantity"]
            if lot["slot"] == "surface":
                for target in targets:
                    amount = min(left, absorption, target[1])
                    if amount:
                        effects.append(Transfer(entity_id, lot["id"], entity_id, amount, "absorbed", target[0]))
                        target[1] -= amount
                        left -= amount
                        absorption -= amount
            if remaining and left:
                amount = min(remaining, left)
                effects.append(Sink(entity_id, lot["id"], amount, "evaporation"))
                remaining -= amount
    return effects


def damage_rule(snapshot):
    effects = []
    for entity_id, entity in sorted(snapshot.objects.items()):
        if entity.get("lifecycle") == "consumed" or entity["state"].get("retired"):
            continue
        structure = entity["components"].get("structure")
        if structure:
            summary = load_summary(snapshot.registry, entity, snapshot.objects)
            if summary["overload"]:
                effects.append(Damage(entity_id, min(100, summary["overload"]*structure.get("overload_rate", 1))))
        for lot in entity["matter"]:
            if not lot.get("burning"):
                continue
            if entity_id in snapshot.protected or lot_moisture(entity, lot) >= 35 or quantity(entity, "water", "surface") or not exposed(entity, lot, snapshot.objects):
                effects.append(SetBurning(entity_id, lot["id"], False))
                continue
            amount = min(lot["quantity"], entity["components"].get("combustion", {}).get("rate", 2))
            effects.append(Sink(entity_id, lot["id"], amount, "combustion"))
            if structure and lot.get("role") == "structural":
                # Only the load-bearing body damages structural integrity.
                initial = entity["state"].get("initial_structural_quantity", sum(l["quantity"] for l in entity["matter"] if l.get("role") == "structural"))
                effects.append(Damage(entity_id, max(1, (structure["max_integrity"]*amount+max(1, initial)-1)//max(1, initial))))
    return effects


def failure_rule(snapshot):
    effects = []
    for entity_id, entity in sorted(snapshot.objects.items()):
        if entity.get("lifecycle") == "consumed" or entity["state"].get("retired"):
            continue
        structure = entity["components"].get("structure")
        supports = entity["components"].get("absorbent", {}).get("supports", {})
        body = {lot["id"]: lot for lot in entity["matter"]}
        remaining = {key: portion["capacity"]*body[key]["quantity"]//portion["quantity"] if key in body else 0 for key, portion in supports.items()}
        for lot in entity["matter"]:
            if lot["material"] == "water" and lot["slot"] == "absorbed":
                bound = lot.get("bound_to")
                keep = min(lot["quantity"], remaining.get(bound, 0))
                remaining[bound] = remaining.get(bound, 0)-keep
                if lot["quantity"] > keep:
                    # Lost absorbent material releases its actual bound water;
                    # it cannot preserve fictitious absorption or buoyancy.
                    effects.append(Transfer(entity_id, lot["id"], entity_id, lot["quantity"]-keep, "surface"))
        if structure and (structure["integrity"] == 0 or not any(lot["role"] == "structural" for lot in entity["matter"]) and entity["state"].get("initial_structural_quantity", 0)):
            effects.append(Break(entity_id))
        elif not entity["matter"] and not any((obj.get("physical_parent") or {}).get("id") == entity_id for obj in snapshot.objects.values()):
            effects.append(Break(entity_id))
    return effects


def spill_rule(snapshot):
    effects, available = [], dict(snapshot.spills)
    for entity_id, entity in sorted(snapshot.objects.items()):
        if quantity(entity, "water", "surface"):
            effects.extend(SetBurning(entity_id, lot["id"], False) for lot in entity["matter"] if lot.get("burning"))
        if entity.get("owner") or entity.get("physical_parent") or entity.get("x") is None or entity.get("lifecycle") == "consumed":
            continue
        key = f"{entity['x']},{entity['y']}"
        if available.get(key, 0) <= 0:
            continue
        amount = 0
        for bound, capacity in absorption_targets(entity, snapshot.objects):
            take = min(available[key], capacity)
            if take:
                effects.append(SpillTransfer(key, entity_id, take, "absorbed", bound))
                available[key] -= take
                amount += take
        if not amount and any(lot.get("burning") for lot in entity["matter"]):
            # Nonporous burning contents are quenched by actual surface water.
            amount = min(available[key], 1)
            effects.append(SpillTransfer(key, entity_id, amount, "surface"))
            available[key] -= amount
        if amount:
            for lot in entity["matter"]:
                if lot.get("burning"):
                    effects.append(SetBurning(entity_id, lot["id"], False))
    # This world models contact pools, not fluid flow. Uncaptured public spill
    # leaves through an explicit drain after same-step contact and quenching.
    effects.extend(SpillSink(key, amount) for key, amount in sorted(available.items()) if amount)
    return effects


def transport_rule(snapshot):
    effects = []
    for move in snapshot.moves:
        entity = snapshot.objects.get(move["target"])
        if entity and load_summary(snapshot.registry, entity, snapshot.objects)["can_float"] and entity.get("lifecycle") == "intact":
            effects.append(Move(move["target"], move["x"], move["y"]))
    return effects


def register_material_rules(registry):
    registry.register_rule(RuleSpec("material.water", 1, "water", water_rule,
        reads=frozenset({"matter", "heater", "exposure"}), writes=frozenset({"matter"}), reducers={"matter": "transfer"},
        sources=frozenset({"rain", "growth", "seed"}), sinks=frozenset({"evaporation", "drain"})))
    registry.register_rule(RuleSpec("material.damage", 1, "damage", damage_rule,
        reads=frozenset({"matter", "relationships", "structure"}), writes=frozenset({"matter", "structure.integrity", "matter.burning"}),
        reducers={"matter": "transfer", "structure.integrity": "sum", "matter.burning": "exclusive"}, sinks=frozenset({"combustion"})))
    registry.register_rule(RuleSpec("material.failure", 1, "failure", failure_rule,
        reads=frozenset({"structure.integrity", "matter"}), writes=frozenset({"lifecycle", "relationships", "matter", "components"}),
        reducers={"matter": "transfer"}, sinks=frozenset({"drain"})))
    registry.register_rule(RuleSpec("material.spill", 1, "spill", spill_rule,
        reads=frozenset({"matter", "relationships"}), writes=frozenset({"matter", "matter.burning"}),
        reducers={"matter": "transfer"}, sinks=frozenset({"drain"})))
    registry.register_rule(RuleSpec("material.transport", 1, "transport", transport_rule,
        reads=frozenset({"matter", "relationships", "buoyant"}), writes=frozenset({"position"})))
    registry.ordered_rules()
