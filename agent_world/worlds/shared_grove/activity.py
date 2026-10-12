"""Bounded physical participation and durable process/index storage.

Registration and admission depend only on stored world facts. Neither saving an
object nor admitting a closure executes a tick or an authored declaration.
"""
from __future__ import annotations
from copy import deepcopy
from agent_world.errors import RuleViolation
from agent_world.simulation import matter as sm
from . import model as m
from .definitions import registry_for

MAX_WET_ENTITIES = sm.MAX_FRAME_OBJECTS
MAX_BURNING_ENTITIES = 8
MAX_BURNING_CONTENTS = sm.MAX_DESCENDANTS
MAX_EVENTS = 4
MAX_GROWTH = 8
MAX_PROCESSES = MAX_EVENTS+MAX_GROWTH
MAX_PROCESS_BYTES = 2048
MAX_FRAME_CHUNKS = 8
MAX_FRAME_META = 8
MAX_RAIN_CHUNKS = 4
MAX_RAIN_INTENSITY = 32
SPREAD_BATCH = 2
WEATHER_BATCH = MOISTURE_BATCH = sm.MAX_FRAME_OBJECTS
BURNING_BATCH = MAX_BURNING_ENTITIES
DRIFT_BATCH = MAX_EVENTS


def meta_get(ctx, key, default=None):
    from . import domains
    if key in domains.ACTIVITY_KEYS and domains.installed(ctx):
        return domains.activity_get(ctx, key, default)
    batch = getattr(ctx, "_grove_batch", None)
    if batch is None:
        return deepcopy(ctx.get_state(m.META, key, default))
    if key not in batch.meta:
        batch.meta[key] = deepcopy(ctx.get_state(m.META, key, default))
    return deepcopy(batch.meta[key])


def meta_set(ctx, key, value):
    from . import domains
    if key in domains.ACTIVITY_KEYS and domains.installed(ctx):
        return domains.activity_set(ctx, key, value)
    if sm.encoded_size(value) > m.MAX_META_BYTES:
        raise RuleViolation("世界过程索引超过8KiB记录界限。")
    if meta_get(ctx, key) == value:
        return
    batch = getattr(ctx, "_grove_batch", None)
    if batch is None:
        ctx.set_state(m.META, key, value)
    else:
        batch.meta[key] = deepcopy(value)
        batch.dirty_meta.add(key)


def spill_points(ctx):
    """Explicit pool resources; legacy chunk indexes are expanded only locally."""
    from . import domains
    points = set(meta_get(ctx, "spill_points", []))
    if not domains.installed(ctx):
        for cid in meta_get(ctx, "spill_chunks", []):
            _, cx, cy = cid.split("_")
            points.update(key for key, amount in m.get_chunk(ctx, int(cx), int(cy)).get("water_reservoirs", {}).items() if amount)
    return points


def read_spills(ctx, objects, *, full_chunks=()):
    """Read only this frame's contact points and fully admitted footprints."""
    from . import domains
    points = spill_points(ctx)
    points.update(f"{obj['x']},{obj['y']}" for obj in objects.values() if m.is_ground(obj))
    for cid in full_chunks:
        _, cx, cy = cid.split("_")
        points.update(m.get_chunk(ctx, int(cx), int(cy)).get("water_reservoirs", {}))
    result = {}
    domain = domains.current(ctx)
    for key in sorted(points):
        point = tuple(map(int, key.split(",")))
        held = m.get_chunk(ctx, *m.chunk_for(*point)).get("water_reservoirs", {}).get(key, 0)
        if held and domains.installed(ctx):
            for owner in domains.nearby(ctx, *point):
                if key in owner.get("spill_points", []) and (domain is None or owner["id"] != domain["id"]):
                    raise domains.CapacityPressure("接触池不属于当前已接纳作用域；本次未读取或消费其水。")
        result[key] = held
    return result


def update_spill_index(ctx, owned_points, remaining):
    """Foreign pools in the same chunk do not keep this domain awake."""
    points = spill_points(ctx) - set(owned_points)
    points.update(key for key, amount in remaining.items() if amount)
    previous = set(meta_get(ctx, "spill_points", []))
    meta_set(ctx, "spill_points", sorted(points))
    meta_set(ctx, "spill_chunks", sorted({m.chunk_id(*m.chunk_for(*map(int, key.split(",")))) for key in points}))
    if previous != points:
        from . import domains
        if domains.installed(ctx):
            domains.mark_dirty(ctx)


def get_process(ctx, process_id):
    batch = getattr(ctx, "_grove_batch", None)
    if batch is not None and process_id in batch.processes:
        return deepcopy(batch.processes[process_id])
    record = ctx.get_state_record(m.PROCESSES, process_id)
    result = {**record["value"], "version": record["version"]} if record["exists"] else None
    if batch is not None:
        batch.processes[process_id] = deepcopy(result)
        batch.process_versions[process_id] = record["version"]
    return deepcopy(result)


def active_process_ids(ctx):
    ids = meta_get(ctx, "active_processes", [])
    if len(ids) > MAX_PROCESSES:
        from .domains import CapacityPressure
        raise CapacityPressure("相互作用域活动过程索引超过已声明界限。")
    return ids


def save_process(ctx, process):
    value = {key: item for key, item in process.items() if key != "version"}
    if sm.encoded_size(value) > MAX_PROCESS_BYTES:
        raise RuleViolation("过程记录超过2KiB预算。")
    previous = get_process(ctx, process["id"])
    if previous is None or value != {key: item for key, item in previous.items() if key != "version"}:
        batch = getattr(ctx, "_grove_batch", None)
        if batch is None:
            process["version"] = ctx.set_state(m.PROCESSES, process["id"], value, expected_version=process.get("version", 0))
        else:
            process["version"] = batch.process_versions[process["id"]]
            batch.processes[process["id"]] = deepcopy(process)
            batch.dirty_processes.add(process["id"])
    ids = active_process_ids(ctx)
    if process["status"] == "active" and process["id"] not in ids:
        if len(ids) >= MAX_PROCESSES:
            from .domains import CapacityPressure
            raise CapacityPressure("相互作用域活动过程已满。")
        ids.append(process["id"])
    elif process["status"] != "active" and process["id"] in ids:
        ids.remove(process["id"])
    meta_set(ctx, "active_processes", sorted(ids))
    if previous is None or previous["status"] != process["status"]:
        from . import domains
        if domains.installed(ctx):
            domains.mark_dirty(ctx)
    return process


def needs_physics(ctx, entity, objects, registry):
    if entity["state"].get("retired") or entity["lifecycle"] == "consumed":
        return False
    contact = False
    try:
        overload = sm.load_summary(registry, entity, objects)["overload"]
    except ValueError as exc:
        raise RuleViolation(str(exc)) from exc
    x, y = m.entity_location(ctx, entity)
    if (x is not None and not entity.get("owner") and "absorbent" in entity["components"]
            and m.tile_at(ctx, x, y) == "water" and sm.absorption_targets(entity, objects)):
        _, sources = m.region_resources(ctx, *m.chunk_for(x, y))
        contact = any(sm.quantity(source, "water", "contained") for source in sources)
    from .contact import candidates
    return bool(contact or candidates(ctx, entity) or sm.quantity(entity, "water", "absorbed") or sm.quantity(entity, "water", "surface")
                or any(lot.get("burning") for lot in entity["matter"])
                or entity["components"].get("heater", {}).get("lit")
                or overload)


def physical_closure(ctx, extra=(), reserve_chunks=()):
    from . import domains
    extra = list(extra)
    domain = domains.current(ctx)
    if domains.installed(ctx) and (domain is None or set(entity["id"] for entity in extra) - set(domain["objects"])):
        domain = domains.ensure(ctx, entity_ids=[entity["id"] for entity in extra], reserve_chunks=reserve_chunks)
    active_ids = set(meta_get(ctx, "physical_active", [])) | set(meta_get(ctx, "creative_instances", []))
    if domain:
        active_ids.update(domain["objects"])
    seeds = {eid: m.get_entity(ctx, eid, required=False) for eid in active_ids}
    for entity in extra:
        seeds[entity["id"]] = entity
    reserved = set(reserve_chunks)
    for pid in active_process_ids(ctx):
        process = get_process(ctx, pid)
        if process and process["status"] == "active":
            if process["kind"] in {"rain", "drift"}:
                reserved.update(process.get("moisture_chunks", process["chunk_ids"]))
            for eid in process.get("reserved_ids", [process["entity_id"]] if process["entity_id"] else []):
                seeds.setdefault(eid, m.get_entity(ctx, eid, required=False))
    for cid in reserved:
        _, cx, cy = cid.split("_")
        chunk = m.get_chunk(ctx, int(cx), int(cy))
        for eid in chunk["entity_ids"]:
            root = m.get_entity(ctx, eid, required=False)
            if root:
                seeds.setdefault(eid, root)
    for key in spill_points(ctx):
        point = tuple(map(int, key.split(",")))
        reserved.add(m.chunk_id(*m.chunk_for(*point)))
        for root in m.public_entities_near(ctx, *point, radius=0, limit=m.MAX_CHUNK_ENTITIES):
            seeds.setdefault(root["id"], root)
    objects = {}
    for entity in list(seeds.values()):
        if entity is None or entity["state"].get("retired") or entity["lifecycle"] == "consumed":
            continue
        objects.update(m._tree_objects(ctx, entity))
    # The same forest calculation guards public capacity and spill receivers.
    # An overloaded attached pouch can release even when its ground root is
    # healthy; a contained pouch with a healthy enclosing vessel cannot.
    for root in [obj for obj in objects.values() if m.is_ground(obj)]:
        release = m.public_break_reservations(ctx, root, objects=objects)
        chunk = m.get_chunk(ctx, *m.chunk_for(root["x"], root["y"]))
        # An already indexed root without public child releases consumes no
        # new slot. Recheck its neighbors only for new placement or release.
        needs_slots = bool(release["ground_ids"]) or root["id"] not in chunk["entity_ids"]
        if needs_slots and not m.can_place_ground(ctx, root):
            raise domains.CapacityPressure("结构失效可能释放的原物件已无公共地面空间。")
        for point in release["contact_tiles"]:
            for target in m.public_entities_near(ctx, *point, radius=0, limit=m.MAX_CHUNK_ENTITIES):
                objects.update(m._tree_objects(ctx, target))
    # Ground fire retains its bounded adjacent spread participants.
    frozen_step = bool(getattr(ctx, "_grove_domain_work", False) and getattr(ctx, "_grove_step_plan", None))
    for entity in list(objects.values()):
        if frozen_step:
            # New ignition belongs to this already reserved frame. Its next
            # frontier is admitted by the following preparing phase.
            break
        if m.is_ground(entity) and entity["state"]["burning"] and entity["components"].get("combustion", {}).get("spread"):
            point = m.entity_location(ctx, entity)
            for target in m.public_entities_near(ctx, *point, radius=1, limit=4*m.MAX_CHUNK_ENTITIES):
                if abs(point[0]-target["x"])+abs(point[1]-target["y"]) <= 1:
                    objects.update(m._tree_objects(ctx, target))
    # Water contact is backed by the chunk's real finite source, not by terrain's
    # visual water label. Reserve each source record in this same physical frame.
    for entity in list(objects.values()):
        x, y = m.entity_location(ctx, entity)
        if (x is not None and not entity.get("owner") and "absorbent" in entity["components"]
                and m.tile_at(ctx, x, y) == "water" and sm.absorption_targets(entity, objects)):
            _, sources = m.region_resources(ctx, *m.chunk_for(x, y))
            for source in sources:
                if sm.quantity(source, "water", "contained"):
                    objects.update(m._tree_objects(ctx, source))
        if entity["components"].get("heater", {}).get("lit") and x is not None:
            for target in m.public_entities_near(ctx, x, y, radius=1, limit=4*m.MAX_CHUNK_ENTITIES):
                if sm.quantity(target, "water", "absorbed") or sm.quantity(target, "water", "surface"):
                    objects.update(m._tree_objects(ctx, target))
    locations = set(reserved)
    for obj in objects.values():
        point = m.entity_location(ctx, obj)
        if point[0] is not None:
            locations.add(m.chunk_id(*m.chunk_for(*point)))
    if len(objects) > sm.MAX_FRAME_OBJECTS or len(locations) > MAX_FRAME_CHUNKS:
        raise domains.CapacityPressure("活动物理闭包最多64个对象、8个地块（含内容、绑定、湿物和释放接触物）。")
    if len({obj["owner"] for obj in objects.values() if obj.get("owner")}) > 4:
        raise domains.CapacityPressure("活动物理闭包最多涉及4名背包持有者。")
    for obj in objects.values():
        if sm.encoded_size({k:v for k,v in obj.items() if k != "version"}) > sm.MAX_ENTITY_BYTES:
            raise RuleViolation("活动物件超过8KiB记录界限。")
    burning = sum(any(lot.get("burning") for lot in obj["matter"]) for obj in objects.values())
    if burning > MAX_BURNING_ENTITIES:
        raise domains.CapacityPressure("同一相互作用域最多同时燃烧8个对象的独立物质批次。")
    # Worst-case record budget:64*2*8KiB +8*2*16KiB +4*2*8KiB
    # +12*2*2KiB +8*2*8KiB =1,556,480B. Reserve the remaining
    # >500KiB for bounded outcome records and Runtime journal/event framing.
    return objects, locations


def reconcile_entity(ctx, entity):
    return reconcile_many(ctx, {entity["id"]: entity})


def reconcile_many(ctx, changed, *, skip_admission=False):
    """Maintain each affected forest once and admit the final local union."""
    from . import domains
    if not domains.installed(ctx):
        for entity in changed.values():
            _reconcile_tree(ctx, entity, skip_admission=skip_admission)
        return
    grouped = {}
    for entity in changed.values():
        domain = domains.for_entity(ctx, entity["id"])
        if domain is not None:
            grouped.setdefault(domain["id"], {})[entity["id"]] = entity
    for did, entities in grouped.items():
        with domains.scope(ctx, did):
            domain = domains.current(ctx)
            tree = {eid: obj for eid in domain["objects"] if (obj := m.get_entity(ctx, eid, required=False)) is not None}
            registry = registry_for(tree)
            ids = {eid for eid, obj in tree.items() if needs_physics(ctx, obj, tree, registry)}
            if len(ids) > sm.MAX_FRAME_OBJECTS:
                raise domains.CapacityPressure("相互作用域活动物理对象已满。")
            meta_set(ctx, "physical_active", sorted(ids))
            if ids and not skip_admission and not getattr(ctx, "_grove_step_plan", None):
                physical_closure(ctx)


def _reconcile_tree(ctx, entity, *, skip_admission=False):
    tree = m._tree_objects(ctx, entity)
    ids = set(meta_get(ctx, "physical_active", []))
    ids.difference_update(tree)
    # Recompute every member after containment, attachment, ownership or load
    # changes. A healthy outer vessel can still contain an overloaded inner one.
    try:
        registry = registry_for(tree)
    except ValueError as exc:
        raise RuleViolation(str(exc)) from exc
    needed = {eid for eid, obj in tree.items() if needs_physics(ctx, obj, tree, registry)}
    ids.update(needed)
    if len(ids) > sm.MAX_FRAME_OBJECTS:
        from .domains import CapacityPressure
        raise CapacityPressure("活动物理对象已满。")
    meta_set(ctx, "physical_active", sorted(ids))
    root = next((obj for obj in tree.values() if not obj.get("physical_parent")), entity)
    if needed or meta_get(ctx, "creative_instances", []) or any(p and p["status"] == "active" and p["kind"] in {"rain", "drift"} and m.is_ground(root)
                     and m.chunk_id(*m.chunk_for(root["x"], root["y"])) in p.get("moisture_chunks", p["chunk_ids"])
                     for p in (get_process(ctx, pid) for pid in active_process_ids(ctx))):
        if not skip_admission:
            physical_closure(ctx, list(tree.values()))


def can_ignite(ctx, entity):
    if entity["lifecycle"] == "consumed" or entity.get("owner") or "personal" in entity["components"]:
        return False
    x, y = m.entity_location(ctx, entity)
    if x is None or m.is_safe(ctx, x, y) or sm.quantity(entity, "water", "surface"):
        return False
    candidate = deepcopy(entity)
    found = False
    objects = m._tree_objects(ctx, entity)
    registry = registry_for(objects)
    for lot in candidate["matter"]:
        if sm.ignitable_lot(registry, candidate, lot, objects):
            lot["burning"] = True
            found = True
    if not found:
        return False
    candidate["state"]["burning"] = True
    try:
        if not m.can_place_ground(ctx, candidate):
            return False
        physical_closure(ctx, [candidate])
    except RuleViolation:
        return False
    return True
