"""World permission and phase views for the simulation's shared fact catalog."""
from __future__ import annotations

from copy import deepcopy
from agent_world.simulation.facts import snapshot_facts
from agent_world.errors import RuleViolation
from . import model as m
from .definitions import registry_for, instance_definition, execution_semantics, read_definition


def interface_schema(ctx, source):
    """Read one exact immutable public interface, never a name-based lookup."""
    from agent_world.simulation.programs import exported_interface
    if not isinstance(source, dict) or set(source) != {"definition_ref", "type_id"}:
        raise RuleViolation("公开接口来源须为明确的definition_ref与type_id。")
    record = read_definition(ctx, source["definition_ref"])
    if not record:
        raise RuleViolation("公开接口的不可变定义不存在。")
    execution_semantics(record)
    spec = record["definition"]["types"].get(source["type_id"])
    if spec is None:
        raise RuleViolation("公开接口的type_id不在指定不可变定义中。")
    return exported_interface(spec)


def validate_port_source(ctx, target, declaration):
    """A typed declaration binds only its exact source type and exports.

    Visibility and distance remain the caller's snapshot permission checks.
    This check grants no reading or writing authority by itself.
    """
    source = declaration.get("source")
    if source is None:
        return not any(name.startswith("authored.") for name in declaration["fields"])
    binding = target.get("state", {}).get("program", {})
    if any(binding.get(key) != source.get(key) for key in ("definition_ref", "type_id")):
        return False
    schema = interface_schema(ctx, source)
    return all(not name.startswith("authored.") or name in schema for name in declaration["fields"])


def interface_facts(ctx, entity):
    """Project actual explicitly exported fields; never defaults or hidden text."""
    from agent_world.simulation.programs import exported_interface
    _, spec = instance_definition(ctx, entity)
    if spec is None:
        return {}
    fields = entity["state"]["program"].get("fields", {})
    result = {}
    for name, declaration in exported_interface(spec).items():
        value = fields.get(name.removeprefix("authored."))
        kind = declaration["type"]
        if type(value) is not {"integer": int, "boolean": bool}[kind]:
            continue
        if kind == "integer" and not declaration["min"] <= value <= declaration["max"]:
            continue
        result[name] = value
    return result


def bound_ids(entity, definition):
    from .creative import _bound_ids
    program = entity.get("state", {}).get("program", {})
    spec = (definition or {}).get("types", {}).get(program.get("type_id"), {})
    return _bound_ids(spec) | set(program.get("port_bindings", {}).values())


def freeze(ctx, anchors):
    """Point-read only the admitted roots and their declared bounded sources."""
    from .definitions import instance_definition
    objects = {}
    for entity in anchors.values():
        if entity is None:
            continue
        objects.update(m._tree_objects(ctx, entity))
        definition, _ = instance_definition(ctx, entity)
        for eid in sorted(bound_ids(entity, definition)):
            target = m.get_entity(ctx, eid, required=False)
            if target is not None and m.public_location(ctx, target) is not None:
                objects.update(m._tree_objects(ctx, target))
    objects.update(anchors)
    return deepcopy(objects)


def public_location(entity, objects):
    """Resolve visibility and location entirely within a frozen forest."""
    current, seen = entity, set()
    while current is not None:
        if (current.get("owner") is not None or current.get("state", {}).get("retired")
                or current.get("state", {}).get("destroyed")):
            return None
        eid = current.get("id")
        if eid in seen or len(seen) >= 8:
            return None
        seen.add(eid)
        relation = current.get("physical_parent")
        if not relation:
            return (current["x"], current["y"]) if current.get("x") is not None and current.get("y") is not None else None
        parent = objects.get(relation["id"])
        if parent is None or relation["relation"] == "contained" and not parent.get("state", {}).get("open", True):
            return None
        current = parent
    return None


def facts(entity, objects, *, external=False, ctx=None):
    entity = {"id": "candidate", **entity}
    tree = {entity["id"]: entity}
    known = True
    pending, seen = [entity], set()
    while pending:
        current = pending.pop()
        if current["id"] in seen:
            known = False
            continue
        seen.add(current["id"])
        if external and (current.get("owner") is not None or
                         "container" in current.get("components", {}) and not current.get("state", {}).get("open", True)):
            known = False
            continue
        ids = current.get("state", {}).get("contents", []) + current.get("state", {}).get("attachments", [])
        for eid in ids:
            child = objects.get(eid)
            if child is None or (child.get("physical_parent") or {}).get("id") != current["id"]:
                known = False
            else:
                tree[eid] = child
                pending.append(child)
    result = snapshot_facts(registry_for(tree), entity, tree, aggregate_known=known)
    if external and "container" in entity.get("components", {}) and not entity.get("state", {}).get("open", True):
        for key in ("physical.contained_water", "physical.absorbed_water", "physical.wetness",
                    "physical.burning", "physical.load.local"):
            result.pop(key, None)
    if ctx is not None and (not external or public_location(entity, objects) is not None):
        # A closed container's own public interface is hidden just like its
        # contained state; callers cannot bypass that policy through exports.
        if not external or not ("container" in entity.get("components", {}) and not entity.get("state", {}).get("open", True)):
            result.update(interface_facts(ctx, entity))
    return result
