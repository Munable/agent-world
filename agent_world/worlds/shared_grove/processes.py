"""Bounded, versioned physical seconds using pure rules and a single flush."""
from __future__ import annotations

from copy import deepcopy

from agent_world import FunctionOutcome
from agent_world.errors import RuleViolation
from agent_world.world_streams import StreamEvent
from agent_world.simulation import matter as sm
from agent_world.simulation.effects import Break, Sink, Source, Transfer
from agent_world.simulation.frame import Frame

from . import model as m
from .definitions import COMBUSTION_CADENCE_TICKS, REGISTRY, RULES_VERSION, registry_for

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


def _meta_get(ctx, key, default=None):
    cache = getattr(ctx, "_grove_meta_cache", None)
    if cache is None:
        return deepcopy(ctx.get_state(m.META, key, default))
    if key not in cache:
        cache[key] = deepcopy(ctx.get_state(m.META, key, default))
    return deepcopy(cache[key])


def _meta_set(ctx, key, value):
    if sm.encoded_size(value) > m.MAX_META_BYTES:
        raise RuleViolation("世界过程索引超过8KiB记录界限。")
    if _meta_get(ctx, key) == value:
        return
    cache = getattr(ctx, "_grove_meta_cache", None)
    if cache is None:
        ctx.set_state(m.META, key, value)
    else:
        cache[key] = deepcopy(value)
        ctx._grove_pending_meta.add(key)


def get_process(ctx, process_id):
    cache = getattr(ctx, "_grove_process_cache", None)
    if cache is not None and process_id in cache:
        return deepcopy(cache[process_id])
    record = ctx.get_state_record(m.PROCESSES, process_id)
    result = {**record["value"], "version": record["version"]} if record["exists"] else None
    if cache is not None:
        cache[process_id] = deepcopy(result)
        ctx._grove_process_versions[process_id] = record["version"]
    return deepcopy(result)


def active_process_ids(ctx):
    ids = _meta_get(ctx, "active_processes", [])
    if len(ids) > MAX_PROCESSES:
        raise RuleViolation("活动过程索引超过已声明界限。")
    return ids


def _save(ctx, process):
    value = {key: item for key, item in process.items() if key != "version"}
    if sm.encoded_size(value) > MAX_PROCESS_BYTES:
        raise RuleViolation("过程记录超过2KiB预算。")
    previous = get_process(ctx, process["id"])
    if previous is None or value != {key: item for key, item in previous.items() if key != "version"}:
        cache = getattr(ctx, "_grove_process_cache", None)
        if cache is None:
            process["version"] = ctx.set_state(m.PROCESSES, process["id"], value, expected_version=process.get("version", 0))
        else:
            process["version"] = ctx._grove_process_versions[process["id"]]
            cache[process["id"]] = deepcopy(process)
            ctx._grove_pending_processes.add(process["id"])
    ids = active_process_ids(ctx)
    if process["status"] == "active" and process["id"] not in ids:
        if len(ids) >= MAX_PROCESSES:
            raise RuleViolation("世界活动过程已满。")
        ids.append(process["id"])
    elif process["status"] != "active" and process["id"] in ids:
        ids.remove(process["id"])
    _meta_set(ctx, "active_processes", sorted(ids))
    return process


def _new(ctx, kind, *, ticks, start_delay=0, **fields):
    clock = ctx.get_state(m.META, "clock", {"sequence": 0})
    start = clock["sequence"]+start_delay
    return _save(ctx, {"id": m.identifier("process"), "kind": kind, "status": "active", "created_at": ctx.now,
        "created_by": ctx.actor_role_id, "start_tick": start, "end_tick": start+ticks,
        "remaining_ticks": ticks, "entity_id": fields.pop("entity_id", None), "chunk_ids": fields.pop("chunk_ids", []),
        "rules_version": RULES_VERSION, "rule_version": 1, **fields})


def _needs_physics(ctx, entity, objects=None):
    if entity["state"].get("retired") or entity["lifecycle"] == "consumed":
        return False
    contact = False
    objects = m._tree_objects(ctx, entity) if objects is None else objects
    try:
        overload = sm.load_summary(registry_for(objects), entity, objects)["overload"]
    except ValueError as exc:
        raise RuleViolation(str(exc)) from exc
    if m.is_ground(entity) and "absorbent" in entity["components"] and m.tile_at(ctx, entity["x"], entity["y"]) == "water" and sm.absorption_targets(entity, objects):
        _, sources = m.region_resources(ctx, *m.chunk_for(entity["x"], entity["y"]))
        contact = any(sm.quantity(source, "water", "contained") for source in sources)
    return bool(contact or sm.quantity(entity, "water", "absorbed") or sm.quantity(entity, "water", "surface")
                or any(lot.get("burning") for lot in entity["matter"])
                or entity["components"].get("heater", {}).get("lit")
                or overload)


def _physical_closure(ctx, extra=(), reserve_chunks=()):
    active_ids = set(_meta_get(ctx, "physical_active", [])) | set(_meta_get(ctx, "creative_instances", []))
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
    for cid in _meta_get(ctx, "spill_chunks", []):
        _, cx, cy = cid.split("_")
        chunk = m.get_chunk(ctx, int(cx), int(cy))
        reserved.add(cid)
        for key, amount in chunk.get("water_reservoirs", {}).items():
            if amount:
                for root in m.public_entities_near(ctx, *map(int, key.split(",")), radius=0, limit=m.MAX_CHUNK_ENTITIES):
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
        if not m.can_place_ground(ctx, root):
            raise RuleViolation("结构失效可能释放的原物件已无公共地面空间。")
        for point in release["contact_tiles"]:
            for target in m.public_entities_near(ctx, *point, radius=0, limit=m.MAX_CHUNK_ENTITIES):
                objects.update(m._tree_objects(ctx, target))
    # Ground fire retains its bounded adjacent spread participants.
    for entity in list(objects.values()):
        if m.is_ground(entity) and entity["state"]["burning"] and entity["components"].get("combustion", {}).get("spread"):
            point = m.entity_location(ctx, entity)
            for target in m.public_entities_near(ctx, *point, radius=1, limit=4*m.MAX_CHUNK_ENTITIES):
                objects.update(m._tree_objects(ctx, target))
    # Water contact is backed by the chunk's real finite source, not by terrain's
    # visual water label. Reserve each source record in this same physical frame.
    for entity in list(objects.values()):
        x, y = m.entity_location(ctx, entity)
        if x is not None and "absorbent" in entity["components"] and m.tile_at(ctx, x, y) == "water":
            _, sources = m.region_resources(ctx, *m.chunk_for(x, y))
            for source in sources:
                objects.setdefault(source["id"], source)
        if entity["components"].get("heater", {}).get("lit") and x is not None:
            for target in m.public_entities_near(ctx, x, y, radius=1, limit=4*m.MAX_CHUNK_ENTITIES):
                if sm.quantity(target, "water", "absorbed") or sm.quantity(target, "water", "surface"):
                    objects.update(m._tree_objects(ctx, target))
    locations = {m.chunk_id(*m.chunk_for(*m.entity_location(ctx, obj))) for obj in objects.values() if m.entity_location(ctx, obj)[0] is not None}
    locations.update(reserved)
    if len(objects) > sm.MAX_FRAME_OBJECTS or len(locations) > MAX_FRAME_CHUNKS:
        raise RuleViolation("活动物理闭包最多64个对象、8个地块（含内容、绑定、湿物和释放接触物）。")
    if len({obj["owner"] for obj in objects.values() if obj.get("owner")}) > 4:
        raise RuleViolation("活动物理闭包最多涉及4名背包持有者。")
    for obj in objects.values():
        if sm.encoded_size({k:v for k,v in obj.items() if k != "version"}) > sm.MAX_ENTITY_BYTES:
            raise RuleViolation("活动物件超过8KiB记录界限。")
    burning = sum(any(lot.get("burning") for lot in obj["matter"]) for obj in objects.values())
    if burning > MAX_BURNING_ENTITIES:
        raise RuleViolation("最多同时燃烧8个对象的独立物质批次。")
    # Worst-case record budget:64*2*8KiB +8*2*16KiB +4*2*8KiB
    # +12*2*2KiB +8*2*8KiB =1,556,480B. Reserve the remaining
    # >500KiB for bounded outcome records and Runtime journal/event framing.
    return objects, locations


def reconcile_entity_processes(ctx, entity):
    tree = m._tree_objects(ctx, entity)
    ids = set(_meta_get(ctx, "physical_active", []))
    ids.difference_update(tree)
    # Recompute every member after containment, attachment, ownership or load
    # changes. A healthy outer vessel can still contain an overloaded inner one.
    needed = {eid for eid, obj in tree.items() if _needs_physics(ctx, obj, tree)}
    ids.update(needed)
    if len(ids) > sm.MAX_FRAME_OBJECTS:
        raise RuleViolation("活动物理对象已满。")
    _meta_set(ctx, "physical_active", sorted(ids))
    root = next((obj for obj in tree.values() if not obj.get("physical_parent")), entity)
    if needed or _meta_get(ctx, "creative_instances", []) or any(p and p["status"] == "active" and p["kind"] in {"rain", "drift"} and m.is_ground(root)
                     and m.chunk_id(*m.chunk_for(root["x"], root["y"])) in p.get("moisture_chunks", p["chunk_ids"])
                     for p in (get_process(ctx, pid) for pid in active_process_ids(ctx))):
        _physical_closure(ctx, list(tree.values()))


def can_ignite(ctx, entity):
    if entity["lifecycle"] == "consumed" or entity.get("owner") or "personal" in entity["components"]:
        return False
    x, y = m.entity_location(ctx, entity)
    if x is None or m.is_safe(ctx, x, y) or sm.quantity(entity, "water", "surface"):
        return False
    candidate = deepcopy(entity)
    found = False
    objects = m._tree_objects(ctx, entity)
    for lot in candidate["matter"]:
        if registry_for(candidate).materials[lot["material"]].combustible and sm.lot_moisture(candidate, lot) < 35 and sm.exposed(candidate, lot, objects):
            lot["burning"] = True
            found = True
    if not found:
        return False
    candidate["state"]["burning"] = True
    try:
        if not m.can_place_ground(ctx, candidate):
            return False
        _physical_closure(ctx, [candidate])
    except RuleViolation:
        return False
    return True


def _admit_event(ctx, chunks):
    events = [get_process(ctx, pid) for pid in active_process_ids(ctx)]
    events = [p for p in events if p and p["status"] == "active" and p["kind"] in {"rain", "drift"}]
    if len(events) >= MAX_EVENTS:
        raise RuleViolation("最多同时运行4个降雨或漂流事件。")
    if any(set(chunks).intersection(p.get("moisture_chunks", p["chunk_ids"])) for p in events):
        raise RuleViolation("同一地块不能重叠降雨或漂流事件。")
    _physical_closure(ctx, reserve_chunks=chunks)


def start_rain(ctx, chunk_ids, *, ticks=8, intensity=10, start_delay=2):
    if type(ticks) is not int or not 1 <= ticks <= 32 or type(intensity) is not int or not 1 <= intensity <= MAX_RAIN_INTENSITY or type(start_delay) is not int or not 0 <= start_delay <= 30:
        raise RuleViolation("降雨时长、强度或延迟无效。")
    if not isinstance(chunk_ids, list) or not 1 <= len(chunk_ids) <= MAX_RAIN_CHUNKS or any(not isinstance(cid, str) for cid in chunk_ids) or len(set(chunk_ids)) != len(chunk_ids):
        raise RuleViolation("降雨须指定1至4个不重复的地块标识。")
    chunks = []
    for cid in chunk_ids:
        try:
            prefix, x, y = cid.split("_")
            cx, cy = int(x), int(y)
        except ValueError:
            raise RuleViolation("降雨地块标识无效。") from None
        if prefix != "chunk" or cid != m.chunk_id(cx, cy) or max(abs(cx*16), abs(cy*16)) > m.WORLD_LIMIT:
            raise RuleViolation("降雨地块标识或范围无效。")
        m.ensure_chunk(ctx, cx, cy)
        chunks.append(cid)
    chunks.sort()
    _admit_event(ctx, chunks)
    objects, _ = _physical_closure(ctx, reserve_chunks=chunks)
    return _new(ctx, "rain", ticks=ticks, start_delay=start_delay, chunk_ids=chunks, moisture_chunks=chunks,
                intensity=intensity, water_remaining=intensity*ticks*len(objects), reserved_ids=[])


def start_growth(ctx, node_id, ticks=6):
    node = m.get_entity(ctx, node_id)
    resource = node["components"].get("resource")
    material = REGISTRY.materials.get(resource.get("material")) if resource else None
    if ticks != 6 or not resource or not material or not material.absorption_per_piece or node["state"]["stock"] >= resource["capacity"] or node["state"].get("regrowth_tick") is not None:
        raise RuleViolation("只有未满、未再生的吸水资源节点可用水养护。")
    if sum(get_process(ctx, pid)["kind"] == "growth" for pid in active_process_ids(ctx)) >= MAX_GROWTH:
        raise RuleViolation("最多同时照料8个资源节点。")
    _physical_closure(ctx, [node])
    x, y = m.entity_location(ctx, node)
    process = _new(ctx, "growth", ticks=6, entity_id=node_id, chunk_ids=[m.chunk_id(*m.chunk_for(x, y))], reserved_ids=[node_id])
    node["state"]["regrowth_tick"] = process["end_tick"]
    resource["regrowth_tick"] = process["end_tick"]
    m.save_entity(ctx, node)
    return process


def start_drift(ctx, entity_id, ticks=16, route=None, start_delay=2):
    entity = m.get_entity(ctx, entity_id)
    if not m.eligible_drift(ctx, entity):
        raise RuleViolation("须是公共水面上完整且总载荷不超过浮力的物件。")
    if type(ticks) is not int or not 1 <= ticks <= 32 or type(start_delay) is not int or not 0 <= start_delay <= 30:
        raise RuleViolation("漂流时长或延迟无效。")
    route = route or [{"x": entity["x"], "y": entity["y"]}, {"x": entity["x"], "y": entity["y"]+1}]
    if not isinstance(route, list) or not 2 <= len(route) <= 9:
        raise RuleViolation("漂流路径必须为1至8个相邻步。")
    previous, chunks = {"x": entity["x"], "y": entity["y"]}, set()
    for index, point in enumerate(route):
        if not isinstance(point, dict) or set(point) != {"x", "y"} or any(type(point[k]) is not int for k in point):
            raise RuleViolation("漂流路径坐标无效。")
        distance = abs(point["x"]-previous["x"])+abs(point["y"]-previous["y"])
        if (index == 0 and point != previous) or (index and distance != 1):
            raise RuleViolation("漂流必须从当前位置逐格行进。")
        if max(abs(point["x"]), abs(point["y"])) > m.WORLD_LIMIT or m.tile_at(ctx, point["x"], point["y"]) == "rock" or (index < len(route)-1 and m.tile_at(ctx, point["x"], point["y"]) != "water"):
            raise RuleViolation("漂流只能沿水面到最后一格岸边。")
        cx, cy = m.chunk_for(point["x"], point["y"])
        m.ensure_chunk(ctx, cx, cy)
        chunks.add(m.chunk_id(cx, cy))
        previous = point
    if len(chunks) > 4:
        raise RuleViolation("漂流最多跨4个地块。")
    if any((p := get_process(ctx, pid)) and p["kind"] == "drift" and p["entity_id"] == entity_id for pid in active_process_ids(ctx)):
        raise RuleViolation("该物件已经有漂流过程。")
    _admit_event(ctx, chunks)
    return _new(ctx, "drift", ticks=ticks, start_delay=start_delay, entity_id=entity_id,
                route=route, route_index=0, paused=False, chunk_ids=[m.chunk_id(*m.chunk_for(entity["x"], entity["y"]))],
                moisture_chunks=sorted(chunks), reserved_ids=[entity_id])


def destroy_entity(ctx, entity):
    """Explicit trusted break uses the same reducer; no clearing a water balance."""
    from agent_world.simulation.rules import spill_rule
    extra = [entity]
    x, y = m.entity_location(ctx, entity)
    spills, chunk = {}, None
    if x is not None and not entity.get("owner"):
        chunk = m.get_chunk(ctx, *m.chunk_for(x, y))
        spills = chunk.get("water_reservoirs", {})
        extra.extend(m.public_entities_near(ctx, x, y, radius=0, limit=m.MAX_CHUNK_ENTITIES))
    objects, _ = _physical_closure(ctx, extra)
    frame = Frame(registry_for(objects), objects, spills=spills)
    frame.apply([Break(entity["id"])], allowed_sinks={"drain"})
    frame.apply(spill_rule(frame.snapshot()), allowed_sinks={"drain"}, allocate=True)
    frame.verify_balance()
    from .creative import physical_contract_failure
    for current in frame.objects.values():
        if physical_contract_failure(ctx, current):
            frame.dirty.add(current["id"])
    own_batch = not hasattr(ctx, "_grove_entities")
    if own_batch:
        _begin_batch(ctx)
    ctx._grove_entities.update(deepcopy(frame.objects))
    ctx._grove_dirty_entities.update(frame.dirty)
    for eid in sorted(frame.dirty):
        m.save_entity(ctx, frame.objects[eid])
        current = frame.objects[eid]
        if current["lifecycle"] == "consumed" and current.get("owner"):
            player = m.get_player(ctx, current["owner"])
            if eid in player["inventory"]:
                player["inventory"].remove(eid)
                m.save_player(ctx, player)
    held = {key: amount for key, amount in frame.spills.items() if amount}
    if chunk and held != spills:
        chunk["water_reservoirs"] = held
        m.save_chunk(ctx, chunk)
        spill_chunks = set(_meta_get(ctx, "spill_chunks", []))
        (spill_chunks.add if held else spill_chunks.discard)(chunk["id"])
        _meta_set(ctx, "spill_chunks", sorted(spill_chunks))
    for (material, reason), amount in frame.sinks.items():
        m._record_account(ctx, "sinks", material, amount, reason)
    if own_batch:
        _flush_batch(ctx)
    return bool(frame.broken)


def _begin_batch(ctx):
    m.begin_batch(ctx)
    ctx._grove_process_cache, ctx._grove_process_versions = {}, {}
    ctx._grove_pending_processes = set()
    ctx._grove_meta_cache, ctx._grove_pending_meta = {}, set()


def _flush_batch(ctx):
    if len(ctx._grove_pending_processes) > MAX_PROCESSES or len(ctx._grove_pending_meta) > MAX_FRAME_META:
        raise RuleViolation("过程及索引写入超过预留记录预算。")
    m.flush_batch(ctx)
    for pid in sorted(ctx._grove_pending_processes):
        process = ctx._grove_process_cache[pid]
        value = {k:v for k,v in process.items() if k != "version"}
        if sm.encoded_size(value) > MAX_PROCESS_BYTES:
            raise RuleViolation("过程记录超过准入预算。")
        ctx.set_state(m.PROCESSES, pid, value, expected_version=ctx._grove_process_versions[pid])
    for key in sorted(ctx._grove_pending_meta):
        ctx.set_state(m.META, key, ctx._grove_meta_cache[key])


def _finished_opportunity(ctx, process):
    from .generation import offer_generation
    if process["kind"] == "rain":
        for cid in process["chunk_ids"]:
            _, cx, cy = cid.split("_")
            x = min(m.WORLD_LIMIT, int(cx)*16+8)
            y = min(m.WORLD_LIMIT, int(cy)*16+8)
            offer_generation(ctx, x, y, trigger="environment_change")
    else:
        entity = m.get_entity(ctx, process["entity_id"], required=False)
        if entity and m.is_ground(entity):
            offer_generation(ctx, entity["x"], entity["y"], trigger="environment_change")


def tick(ctx, args):
    from . import building
    clock = ctx.get_state(m.META, "clock")
    if args["epoch"] != clock["epoch"] or args["sequence"] != clock["sequence"]:
        return FunctionOutcome({"advanced": False, "reason": "superseded"})
    if clock.get("rules_version", RULES_VERSION) != RULES_VERSION:
        raise RuleViolation("规则版本已变化，旧过程不能被新规则静默重解释。")
    late = ctx.timer is not None and ctx.now-ctx.timer.due_at > 2
    sequence = clock["sequence"] if late else clock["sequence"]+1
    if late:
        clock["epoch"] = m.identifier("clock")
    clock.update(sequence=sequence, last_tick_at=ctx.now, rules_version=RULES_VERSION)
    _begin_batch(ctx)
    changes, affected = [], []
    processes = [p for pid in active_process_ids(ctx) if (p := get_process(ctx, pid)) and p["status"] == "active"]
    if any(p.get("rules_version") != RULES_VERSION or p.get("rule_version") != 1 for p in processes):
        raise RuleViolation("持久过程规则版本不匹配。请使用当前规则的新世界。")
    if not late:
        objects, chunks = _physical_closure(ctx)
        original = deepcopy(objects)
        protected = set()
        for eid, entity in objects.items():
            x, y = m.entity_location(ctx, entity)
            if entity.get("owner") or "personal" in entity["components"] or m.is_safe(ctx, x, y):
                protected.add(eid)
        spills = {}
        for cid in chunks:
            _, cx, cy = cid.split("_")
            spills.update(m.get_chunk(ctx, int(cx), int(cy)).get("water_reservoirs", {}))
        frame = Frame(registry_for(objects), objects, sequence, protected=protected, spills=spills)
        # Finite water contact spends one source lot with a deterministic budget.
        contact_effects, source_available, wet_by_rain = [], {}, set()
        for eid, entity in sorted(frame.objects.items()):
            x, y = m.entity_location(ctx, entity)
            if entity.get("owner") or x is None or "absorbent" not in entity["components"] or m.tile_at(ctx, x, y) != "water":
                continue
            targets = [[bound, capacity] for bound, capacity in sm.absorption_targets(entity, frame.objects)]
            if not targets:
                continue
            _, sources = m.region_resources(ctx, *m.chunk_for(x, y))
            amount = min(entity["components"]["absorbent"]["rate"], sum(target[1] for target in targets))
            for source in sources:
                if source["id"] not in frame.objects:
                    continue
                for lot in source["matter"]:
                    if lot["material"] != "water" or lot["slot"] != "contained":
                        continue
                    key = (source["id"], lot["id"])
                    available = source_available.setdefault(key, lot["quantity"])
                    for target in targets:
                        take = min(amount, available, target[1])
                        if take:
                            contact_effects.append(Transfer(source["id"], lot["id"], eid, take, "absorbed", target[0]))
                            available -= take
                            source_available[key] -= take
                            amount -= take
                            target[1] -= take
                            wet_by_rain.add(eid)
        frame.apply(contact_effects)
        inputs = []
        absorption = {eid: [[bound, capacity] for bound, capacity in sm.absorption_targets(entity, frame.objects)] for eid, entity in frame.objects.items()}
        stored = {eid: sm.quantity(entity, "water", "contained") for eid, entity in frame.objects.items()}
        for process in sorted(processes, key=lambda p:p["id"]):
            if process["kind"] != "rain" or sequence <= process["start_tick"]:
                continue
            if sequence <= process["end_tick"]:
                for eid, entity in sorted(frame.objects.items()):
                    if not m.is_ground(entity) or m.chunk_id(*m.chunk_for(entity["x"], entity["y"])) not in process["chunk_ids"]:
                        continue
                    effect = building.rain_effect(ctx, entity, process["intensity"])
                    if effect.get("protected_by"):
                        continue
                    amount = min(process["intensity"], process["water_remaining"])
                    collect = effect.get("collect_capacity", 0)
                    rate = effect.get("collect_rate", 0)
                    if "water_source" in entity["components"]:
                        collect = entity["components"]["water_source"]["capacity"]
                        rate = process["intensity"]
                    captured = min(amount, rate, max(0, collect-stored[eid]))
                    if captured:
                        inputs.append({"target": eid, "material": "water", "quantity": captured, "slot": "contained", "reason": "rain"})
                        stored[eid] += captured
                        amount -= captured
                        process["water_remaining"] -= captured
                    for target in absorption[eid]:
                        taken = min(amount, target[1])
                        if taken:
                            inputs.append({"target": eid, "material": "water", "quantity": taken, "slot": "absorbed", "reason": "rain", "bound_to": target[0]})
                            target[1] -= taken
                            amount -= taken
                            process["water_remaining"] -= taken
                            wet_by_rain.add(eid)
            process["remaining_ticks"] = max(0, process["end_tick"]-sequence)
            if sequence >= process["end_tick"]:
                process["status"] = "complete"
                changes.append("短雨结束。")
            _save(ctx, process)
        # Existing structure helper only enables the declared fuel lots and
        # identifies deduplicated dry targets; all quantity rates stay in rules.
        heated = building.tick_structures(ctx, sequence, changes, frame.objects, dirty=frame.dirty)
        frame.inputs, frame.heated, frame.wet_by_rain = tuple(inputs), frozenset(heated), frozenset(wet_by_rain)
        moves, drift_by_entity, destinations = [], {}, set()
        for process in sorted(processes, key=lambda p:p["id"]):
            if process["kind"] != "drift" or sequence <= process["start_tick"]:
                continue
            entity = frame.objects.get(process["entity_id"])
            index = process["route_index"]+1
            if entity is None or not m.is_ground(entity) or index >= len(process["route"]):
                process["status"] = "complete"
            else:
                point = process["route"][index]
                key = point["x"], point["y"]
                occupants = m.public_entities_near(ctx, *key, radius=0, limit=m.MAX_CHUNK_ENTITIES)
                if (key in destinations or m.is_safe(ctx, *key) or m.public_players_near(ctx, *key, radius=0)
                        or any(obj["id"] != entity["id"] for obj in occupants)):
                    process["paused"] = True
                else:
                    moves.append({"target": entity["id"], **point})
                    drift_by_entity[entity["id"]] = process
                    destinations.add(key)
            process["remaining_ticks"] = max(0, process["end_tick"]-sequence)
            if sequence >= process["end_tick"]:
                process["status"] = "complete"
        frame.moves = tuple(moves)
        try:
            frame.run()
            frame.verify_balance()
        except ValueError as exc:
            raise RuleViolation(str(exc)) from exc
        # Growth is an explicit biological source, paid by the tend water sink.
        for process in processes:
            if process["kind"] != "growth":
                continue
            process["remaining_ticks"] = max(0, process["end_tick"]-sequence)
            node = frame.objects.get(process["entity_id"])
            if not node or node["lifecycle"] != "intact":
                process["status"] = "complete"
            elif sequence >= process["end_tick"]:
                resource = node["components"]["resource"]
                quantity = min(REGISTRY.materials[resource["material"]].piece_quantity,
                               resource["capacity"]*REGISTRY.materials[resource["material"]].piece_quantity-sm.quantity(node, resource["material"], "contained"))
                frame.apply([Source(node["id"], resource["material"], quantity, "contained", "growth")], allowed_sources={"growth"})
                node["state"]["regrowth_tick"] = None
                resource.pop("regrowth_tick", None)
                process["status"] = "complete"
                changes.append(f"{node['name']}经照料再生了一份材料。")
            _save(ctx, process)
        # Spread never advances the newly ignited material's rate in this tick.
        ctx._grove_entities.update(deepcopy(frame.objects))
        if sequence % COMBUSTION_CADENCE_TICKS == 0:
            spread = SPREAD_BATCH
            for entity in sorted(frame.objects.values(), key=lambda obj:obj["id"]):
                if not spread or not m.is_ground(entity) or not entity["components"].get("combustion", {}).get("spread") or not any(lot.get("burning") for lot in entity["matter"]):
                    continue
                for target in m.public_entities_near(ctx, entity["x"], entity["y"], radius=1, limit=m.MAX_CHUNK_ENTITIES):
                    if abs(target["x"]-entity["x"])+abs(target["y"]-entity["y"]) != 1 or target["id"] not in frame.objects or target["id"] in protected:
                        continue
                    current = frame.objects[target["id"]]
                    if any(lot.get("burning") for lot in current["matter"]) or sm.quantity(current, "water", "surface"):
                        continue
                    burning_count = sum(any(lot.get("burning") for lot in obj["matter"]) for obj in frame.objects.values())
                    if burning_count >= MAX_BURNING_ENTITIES:
                        break
                    lot = next((lot for lot in current["matter"] if registry_for(frame.objects).materials[lot["material"]].combustible and sm.lot_moisture(current, lot) < 35 and sm.exposed(current, lot, frame.objects)), None)
                    if lot:
                        lot["burning"] = True
                        sm.project(registry_for(frame.objects), current)
                        ctx._grove_entities[current["id"]] = deepcopy(current)
                        try:
                            _physical_closure(ctx, list(frame.objects.values()))
                        except RuleViolation:
                            lot["burning"] = False
                            sm.project(registry_for(frame.objects), current)
                            ctx._grove_entities[current["id"]] = deepcopy(current)
                            continue
                        frame.dirty.add(current["id"])
                        spread -= 1
                        changes.append(f"火蔓延到相邻的{current['name']}。")
                        if not spread:
                            break
        frame.verify_balance()
        from .creative import physical_contract_failure
        for current in frame.objects.values():
            if physical_contract_failure(ctx, current, tick=sequence):
                frame.dirty.add(current["id"])
        ctx._grove_entities.update(deepcopy(frame.objects))
        ctx._grove_dirty_entities.update(frame.dirty)
        for eid in sorted(frame.dirty):
            m.save_entity(ctx, frame.objects[eid])
            entity = frame.objects[eid]
            if entity["lifecycle"] == "consumed" and entity.get("owner"):
                player = m.get_player(ctx, entity["owner"])
                if eid in player["inventory"]:
                    player["inventory"].remove(eid)
                    m.save_player(ctx, player)
            if m.is_ground(entity):
                affected.append({"x": entity["x"], "y": entity["y"], "entity_id": eid})
        for cid in chunks:
            _, cx, cy = cid.split("_")
            chunk = m.get_chunk(ctx, int(cx), int(cy))
            own_spills = {key: amount for key, amount in frame.spills.items() if amount and m.chunk_for(*map(int, key.split(","))) == (int(cx), int(cy))}
            if own_spills != chunk.get("water_reservoirs", {}):
                chunk["water_reservoirs"] = own_spills
                m.save_chunk(ctx, chunk)
        spill_chunks = [cid for cid in chunks if m.get_chunk(ctx, *map(int, cid.split("_")[1:])).get("water_reservoirs")]
        _meta_set(ctx, "spill_chunks", sorted(spill_chunks))
        for category, values in (("sources", frame.sources), ("sinks", frame.sinks)):
            for (material, reason), amount in values.items():
                m._record_account(ctx, category, material, amount, reason)
        outcomes = []
        for eid in frame.broken:
            if frame.objects[eid].get("owner") or frame.objects[eid].get("physical_parent"):
                continue
            changes.append(f"{frame.objects[eid]['name']}结构失效，剩余材料和原内容留在现场。")
            outcomes.append((eid, "broken", changes[-1], None))
        for process in processes:
            if process["kind"] == "drift":
                entity = frame.objects.get(process["entity_id"])
                before = original.get(process["entity_id"])
                if entity and before and (entity.get("x"), entity.get("y")) != (before.get("x"), before.get("y")):
                    process["route_index"] += 1
                    process["paused"] = False
                    process["chunk_ids"] = [m.chunk_id(*m.chunk_for(entity["x"], entity["y"]))]
                    if m.tile_at(ctx, entity["x"], entity["y"]) != "water":
                        process["status"] = "complete"
                        changes.append(f"{entity['name']}漂到了岸边。")
                        outcomes.append((entity["id"], "shore", changes[-1], process["id"]))
                elif entity and entity["lifecycle"] != "intact":
                    process["status"] = "complete"
                else:
                    process["paused"] = True
                _save(ctx, process)
            if process["status"] == "complete":
                _finished_opportunity(ctx, process)
        # Each wet/burning/overloaded object's state has already advanced fully.
        # No density-dependent scan delays, query materialization or catch-up.
    ctx.set_state(m.META, "clock", clock)
    if not late:
        from .creative import tick as creative_tick
        creative_outcomes = creative_tick(ctx)
        outcomes.extend((eid, action, summary, None) for eid, action, summary in creative_outcomes)
        changes.extend(summary for _, _, summary in creative_outcomes)
    for summary in changes[:8]:
        m.record_change(ctx, summary, x=None, y=None, action="process")
    _flush_batch(ctx)
    from .generation import record_content_outcomes
    record_content_outcomes(ctx, [{"entity_id": entity_id, "action": action, "summary": summary, "process_id": process_id}
                                 for entity_id, action, summary, process_id in (outcomes if not late else [])])
    ctx.schedule_timer(f"grove.tick.{clock['epoch']}.{sequence}", "grove.tick",
                       {"epoch": clock["epoch"], "sequence": sequence}, due_at=ctx.now+1)
    events = (StreamEvent("grove.changes", "process", {"summary": " ".join(changes[:8]), "tick": sequence,
               "affected": affected[:64]}, key=f"tick-{sequence}"),) if changes else ()
    return FunctionOutcome({"advanced": not late, "paused_resume": late, "tick": sequence, "changes": changes[:16],
        "activity_limits": {"wet_or_reserved": MAX_WET_ENTITIES, "burning": MAX_BURNING_ENTITIES,
                            "burning_contents": MAX_BURNING_CONTENTS, "events": MAX_EVENTS, "growth": MAX_GROWTH,
                            "hearths": building.MAX_HEARTHS, "objects": sm.MAX_FRAME_OBJECTS, "chunks": MAX_FRAME_CHUNKS,
                            "lots_per_object": sm.MAX_LOTS, "rules_version": RULES_VERSION}}, events=events)


def visible_processes(ctx, x, y, radius=8):
    chunks = set(m.chunk_ids_around(x, y, radius))
    result = []
    for pid in active_process_ids(ctx):
        process = get_process(ctx, pid)
        if not process or process["status"] != "active" or not chunks.intersection(process["chunk_ids"]):
            continue
        if process["entity_id"]:
            entity = m.get_entity(ctx, process["entity_id"], required=False)
            if not entity or entity.get("owner") not in (None, ctx.actor_role_id):
                continue
        result.append({key: process[key] for key in ("id", "kind", "status", "entity_id", "chunk_ids", "remaining_ticks", "start_tick", "end_tick", "version", "rules_version")})
    return result[:MAX_PROCESSES]
