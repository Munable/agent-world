"""World permission and phase views for the simulation's shared fact catalog."""
from __future__ import annotations

from copy import deepcopy
from agent_world.simulation.facts import snapshot_facts
from . import model as m
from .definitions import registry_for


def bound_ids(entity, definition):
    from .creative import _bound_ids
    return _bound_ids(definition or {}) | set(entity.get("state", {}).get("program", {}).get("port_bindings", {}).values())


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


def facts(entity, objects, *, external=False):
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
    return result
