"""Persistent one-second material processes, in a fixed causal order."""
from __future__ import annotations

from copy import deepcopy

from agent_world import FunctionOutcome
from agent_world.errors import RuleViolation
from agent_world.world_streams import StreamEvent

from . import model as m

# Admission reserves bounded work, so every admitted material advances every
# logical second. These limits include all members released by burning boxes.
MAX_WET_ENTITIES = 128
MAX_BURNING_ENTITIES = 16
MAX_BURNING_CONTENTS = 16
SPREAD_BATCH = MAX_BURNING_ENTITIES
WEATHER_BATCH = MAX_WET_ENTITIES
MOISTURE_BATCH = MAX_WET_ENTITIES
BURNING_BATCH = MAX_BURNING_ENTITIES
DRIFT_BATCH = 4
MAX_EVENTS = 4
MAX_GROWTH = 16


def _admit_event(ctx,chunks=None):
    events = [p for pid in active_process_ids(ctx) if (p:=get_process(ctx,pid))
              and p["status"]=="active" and p["kind"] in {"rain","drift"}]
    if len(events)>=MAX_EVENTS:
        raise RuleViolation("最多同时运行4个降雨或漂流事件。")
    if chunks and any(set(chunks).intersection(p.get("moisture_chunks",p["chunk_ids"])) for p in events):
        raise RuleViolation("同一地块不能同时运行两个降雨或漂流事件。")


def _wet_slots(ctx):
    ids = set()
    for pid in active_process_ids(ctx):
        process = get_process(ctx,pid)
        if process and process["kind"]=="moisture" and process["status"]=="active":
            ids.update(process.get("entity_ids",[process["entity_id"]] if process["entity_id"] else []))
            for members in process.get("reservation_members",{}).values():
                ids.update(members)
    return ids


def _reserve_wet_members(ctx,group,reservation,members):
    proposed = _wet_slots(ctx).union(members)
    if len(proposed)>MAX_WET_ENTITIES:
        raise RuleViolation("世界最多同时处理128件受湿或已预留降雨/漂流的物件。")
    current = group.setdefault("reservation_members",{}).setdefault(reservation,[])
    for eid in members:
        if eid not in current:
            current.append(eid)
    _save(ctx,group)


def can_ignite(ctx,entity):
    candidate = {**entity, "state": {**entity["state"], "burning": True}}
    if not m.can_place_ground(ctx, candidate):
        return False
    burning = []
    contents = 0
    for pid in active_process_ids(ctx):
        p = get_process(ctx,pid)
        if not p or p["kind"]!="burning" or p["status"]!="active":
            continue
        current = m.get_entity(ctx,p["entity_id"],required=False)
        if current and current["state"]["burning"]:
            burning.append(current["id"])
            contents += len(m.descendants(ctx,current))
    return (len(burning)<MAX_BURNING_ENTITIES and
            contents+len(m.descendants(ctx,entity))<=MAX_BURNING_CONTENTS and
            len(active_process_ids(ctx))<m.MAX_ACTIVE)


def _meta_get(ctx,key,default=None):
    cache = getattr(ctx,"_grove_meta_cache",None)
    if cache is None:
        return ctx.get_state(m.META,key,default)
    if key not in cache:
        cache[key] = ctx.get_state(m.META,key,default)
    return deepcopy(cache[key])


def _meta_set(ctx,key,value):
    current = _meta_get(ctx,key)
    if current == value:
        return
    cache = getattr(ctx,"_grove_meta_cache",None)
    if cache is None:
        ctx.set_state(m.META,key,value)
    else:
        cache[key] = deepcopy(value)
        ctx._grove_pending_meta.add(key)


def get_process(ctx, process_id):
    cache = getattr(ctx,"_grove_process_cache",None)
    if cache is not None and process_id in cache:
        return deepcopy(cache[process_id])
    rec = ctx.get_state_record(m.PROCESSES, process_id)
    value = {**rec["value"], "version": rec["version"]} if rec["exists"] else None
    if cache is not None:
        cache[process_id] = value
        ctx._grove_process_versions[process_id] = rec["version"]
    return deepcopy(value)


def active_process_ids(ctx):
    return _meta_get(ctx,"active_processes",[])


def _save(ctx, process):
    previous = get_process(ctx,process["id"])
    value = {k:v for k,v in process.items() if k != "version"}
    if previous is None or value != {k:v for k,v in previous.items() if k != "version"}:
        cache = getattr(ctx,"_grove_process_cache",None)
        if cache is None:
            process["version"] = ctx.set_state(m.PROCESSES,process["id"],value,
                                               expected_version=process.get("version"))
        else:
            process["version"] = ctx._grove_process_versions[process["id"]]
            cache[process["id"]] = deepcopy(process)
            ctx._grove_pending_processes.add(process["id"])
    elif previous is not None:
        process["version"] = previous["version"]
    ids = active_process_ids(ctx)
    changed = False
    if process["status"] == "active" and process["id"] not in ids:
        if len(ids) >= m.MAX_ACTIVE:
            raise RuleViolation("当前环境过程已满，请稍后再试。")
        ids.append(process["id"])
        changed = True
    elif process["status"] != "active" and process["id"] in ids:
        ids.remove(process["id"])
        changed = True
    if changed:
        _meta_set(ctx,"active_processes",ids)
    return process


def _new(ctx, kind, **fields):
    clock = ctx.get_state(m.META, "clock", {"sequence": 0})
    delay = fields.pop("start_delay", 0)
    return _save(ctx, {"id": m.identifier("process"), "kind": kind, "status": "active",
                       "created_at": ctx.now, "created_by": ctx.actor_role_id,
                       "start_tick": clock["sequence"] + delay,
                       "remaining_ticks": fields.pop("remaining_ticks", 0),
                       "entity_id": fields.pop("entity_id", None),
                       "chunk_ids": fields.pop("chunk_ids", []), **fields})


def _moisture_key(ctx, entity):
    if entity["owner"]:
        return "owner:" + entity["owner"], [], entity["owner"], m.MAX_INVENTORY
    if entity["container"]:
        root = entity
        seen = set()
        while root["container"]:
            if root["id"] in seen:
                raise RuleViolation("容器关系不能循环。")
            seen.add(root["id"])
            root = m.get_entity(ctx, root["container"])
        x,y = m.entity_location(ctx,root)
        return "container:" + root["id"], [m.chunk_id(*m.chunk_for(x,y))], None, m.MAX_INVENTORY
    cid = m.chunk_id(*m.chunk_for(entity["x"],entity["y"]))
    return "chunk:" + cid, [cid], None, m.MAX_CHUNK_ENTITIES


def _moisture_group(ctx, group_key, chunks, owner=None, limit=m.MAX_CHUNK_ENTITIES, *, reservation=None):
    """One indexed process per ground chunk, owner, or public container tree."""
    key = "moisture_group:" + group_key
    pid = _meta_get(ctx,key)
    current = get_process(ctx,pid) if pid else None
    if current is None or current["status"] != "active":
        current = _new(ctx,"moisture",group_key=group_key,entity_ids=[],
                       member_limit=limit,owner=owner,chunk_ids=chunks,
                       reservations=[reservation] if reservation else [],reservation_members={})
        _meta_set(ctx,key,current["id"])
    elif reservation and reservation not in current.get("reservations",[]):
        current.setdefault("reservations",[]).append(reservation)
        _save(ctx,current)
    return current


def _remove_moisture_member(ctx, process, entity_id):
    # Old per-entity records are still bounded and can be retired on touch.
    if "entity_ids" not in process:
        process["status"] = "complete"
        _save(ctx,process)
        return
    if entity_id in process["entity_ids"]:
        process["entity_ids"].remove(entity_id)
    if not process["entity_ids"] and not process.get("reservations"):
        process["status"] = "complete"
    _save(ctx,process)


def _release_moisture_reservations(ctx, process):
    for cid in process.get("moisture_chunks",process.get("chunk_ids",[])):
        pid = _meta_get(ctx,"moisture_group:chunk:"+cid)
        group = get_process(ctx,pid) if pid else None
        if group and process["id"] in group.get("reservations",[]):
            group["reservations"].remove(process["id"])
            group.get("reservation_members",{}).pop(process["id"],None)
            if not group["entity_ids"] and not group["reservations"]:
                group["status"] = "complete"
            _save(ctx,group)


def reconcile_entity_processes(ctx, entity):
    if m.is_ground(entity) and "wettable" in entity["capabilities"] and not entity["state"]["destroyed"]:
        cid = m.chunk_id(*m.chunk_for(entity["x"],entity["y"]))
        pid = _meta_get(ctx,"moisture_group:chunk:"+cid)
        group = get_process(ctx,pid) if pid else None
        if group and group["status"]=="active":
            for rid in group.get("reservations",[]):
                event = get_process(ctx,rid)
                if event and event["kind"]=="rain" and event["status"]=="active":
                    _reserve_wet_members(ctx,group,rid,[entity["id"]])
    key = "processes:" + entity["id"]
    links = _meta_get(ctx,key,{})
    wet = (entity["state"]["wetness"] > 0 and not entity["state"]["destroyed"]
           and not entity["state"].get("retired"))
    current = get_process(ctx,links["moisture"]) if links.get("moisture") else None
    changed = False
    if wet:
        if len(_wet_slots(ctx).union({entity["id"]}))>MAX_WET_ENTITIES:
            raise RuleViolation("世界最多同时处理128件受湿或已预留物件。")
        group_key,chunks,owner,limit = _moisture_key(ctx,entity)
        if current is not None and (current["status"] != "active" or current.get("group_key") != group_key):
            if current["status"] == "active":
                _remove_moisture_member(ctx,current,entity["id"])
            current = None
        if current is None:
            current = _moisture_group(ctx,group_key,chunks,owner,limit)
        if entity["id"] not in current["entity_ids"]:
            if len(current["entity_ids"]) >= current["member_limit"]:
                raise RuleViolation("当前受湿物件索引已满。")
            current["entity_ids"].append(entity["id"])
            _save(ctx,current)
        if links.get("moisture") != current["id"]:
            links["moisture"] = current["id"]
            changed = True
    elif current is not None and current["status"] == "active":
        _remove_moisture_member(ctx,current,entity["id"])
    wanted = {"burning": entity["state"]["burning"] and not entity["state"]["destroyed"]
              and not entity["state"].get("retired")}
    for kind, needed in wanted.items():
        current = get_process(ctx, links[kind]) if kind in links else None
        if needed and (current is None or current["status"] != "active"):
            if kind=="burning" and not can_ignite(ctx,entity):
                raise RuleViolation("最多同时燃烧16件物件，燃烧容器的全部内容总数最多16件。")
            p = _new(ctx, kind, entity_id=entity["id"])
            links[kind] = p["id"]
            changed = True
        elif not needed and current and current["status"] == "active":
            current["status"] = "complete"
            _save(ctx, current)
    if changed:
        _meta_set(ctx,key,links)


def start_rain(ctx, chunk_ids, ticks=8, intensity=20, start_delay=2):
    _admit_event(ctx,chunk_ids)
    if not isinstance(chunk_ids, list) or not 1 <= len(set(chunk_ids)) <= 4:
        raise RuleViolation("降雨区域须为1至4个地块。")
    if type(ticks) is not int or not 1 <= ticks <= 24 or type(intensity) is not int or not 1 <= intensity <= 30:
        raise RuleViolation("降雨必须有有限时长与水量。")
    if type(start_delay) is not int or not 0 <= start_delay <= 30:
        raise RuleViolation("降雨延迟无效。")
    centers = []
    rain_id = m.identifier("process")
    for cid in chunk_ids:
        try:
            _, cx, cy = cid.split("_")
            cx, cy = int(cx), int(cy)
        except (TypeError, ValueError):
            raise RuleViolation("降雨地块无效。") from None
        m.ensure_chunk(ctx, cx, cy)
        # Reserve the one process needed for this chunk before accepting rain.
        # Every future ground member fits the same96-slot chunk index.
        group = _moisture_group(ctx,"chunk:"+cid,[cid],reservation=rain_id)
        wettable = [eid for eid in m.get_chunk(ctx,cx,cy)["entity_ids"]
                    if (entity:=m.get_entity(ctx,eid)) and m.is_ground(entity)
                    and not entity["state"]["destroyed"] and "wettable" in entity["capabilities"]]
        _reserve_wet_members(ctx,group,rain_id,wettable)
        centers.append({"x": cx*16+8, "y": cy*16+8})
    clock = ctx.get_state(m.META,"clock")
    start_tick = clock["sequence"]+start_delay
    return _new(ctx, "rain", id=rain_id, chunk_ids=list(dict.fromkeys(chunk_ids)), remaining_ticks=ticks,
                intensity=intensity, water_remaining=ticks*intensity*96*len(chunk_ids),
                start_delay=start_delay, end_tick=start_tick+ticks, centers=centers)


def can_start_growth(ctx):
    count = sum(1 for pid in active_process_ids(ctx) if (p := get_process(ctx, pid))
                and p["status"] == "active" and p["kind"] == "growth")
    return count < MAX_GROWTH and len(active_process_ids(ctx)) < m.MAX_ACTIVE


def start_growth(ctx, node_id, ticks=6):
    node = m.get_entity(ctx, node_id)
    state = node["state"]
    if (not m.is_ground(node) or state.get("kind") != "resource_node"
            or state.get("resource_kind") not in {"wood", "cloth"}
            or state["stock"] >= state["stock_capacity"] or state.get("regrowth_tick") is not None):
        raise RuleViolation("只有库存未满且尚未再生的木材或芦苇节点可以照料。")
    if ticks != 6:
        raise RuleViolation("人工照料每次固定6个逻辑秒再生一份。")
    if not can_start_growth(ctx):
        raise RuleViolation("最多同时照料16个资源节点。")
    sequence = ctx.get_state(m.META, "clock")["sequence"]
    process = _new(ctx, "growth", entity_id=node_id,
                   chunk_ids=[m.chunk_id(*m.chunk_for(node["x"], node["y"]))],
                   remaining_ticks=6, end_tick=sequence+6)
    state["regrowth_tick"] = sequence+6
    m.save_entity(ctx, node)
    return process


def start_drift(ctx, entity_id, ticks=16, route=None, start_delay=2):
    _admit_event(ctx)
    entity = m.get_entity(ctx, entity_id)
    if not m.is_ground(entity) or "floating" not in entity["capabilities"] or entity["state"]["destroyed"]:
        raise RuleViolation("只有公共地面的可漂浮物件能漂流。")
    if m.tile_at(ctx, entity["x"], entity["y"]) != "water":
        raise RuleViolation("漂流须从水面开始。")
    if type(ticks) is not int or not 1 <= ticks <= 32 or type(start_delay) is not int or not 0 <= start_delay <= 30:
        raise RuleViolation("漂流时长或延迟无效。")
    if route is None:
        route = [{"x": entity["x"], "y": entity["y"]}]
        for _ in range(ticks):
            last = route[-1]
            next_tile = {"x": last["x"], "y": last["y"]+1}
            if m.tile_at(ctx, next_tile["x"], next_tile["y"]) == "rock":
                break
            route.append(next_tile)
            if m.tile_at(ctx, next_tile["x"], next_tile["y"]) != "water":
                break
    if not isinstance(route, list) or not 1 <= len(route) <= 33:
        raise RuleViolation("漂流路径长度无效。")
    points = []
    last = {"x": entity["x"], "y": entity["y"]}
    for i, point in enumerate(route):
        if not isinstance(point, dict) or set(point) != {"x", "y"} or any(type(point[k]) is not int for k in point):
            raise RuleViolation("漂流路径坐标无效。")
        if i == 0 and point == last:
            points.append(point)
            continue
        if abs(point["x"]-last["x"])+abs(point["y"]-last["y"]) != 1:
            raise RuleViolation("漂流每步须为相邻一格。")
        if max(abs(point["x"]), abs(point["y"])) > m.WORLD_LIMIT or m.tile_at(ctx, point["x"], point["y"]) == "rock":
            raise RuleViolation("漂流不能穿越岩壁或边界。")
        if i < len(route)-1 and m.tile_at(ctx, point["x"], point["y"]) != "water":
            raise RuleViolation("只有最后一格可以上岸。")
        points.append(point)
        last = point
    links = _meta_get(ctx,"processes:"+entity_id,{})
    current = get_process(ctx, links.get("drift", "missing"))
    if current and current["status"] == "active":
        raise RuleViolation("该物件已在漂流。")
    moisture_chunks = list(dict.fromkeys(m.chunk_id(*m.chunk_for(point["x"],point["y"])) for point in points))
    _admit_event(ctx,moisture_chunks)
    if len(moisture_chunks)>4:
        raise RuleViolation("漂流路径最多跨越4个地块。")
    drift_id = m.identifier("process")
    for cid in moisture_chunks:
        _,cx,cy=cid.split("_")
        m.ensure_chunk(ctx,int(cx),int(cy))
        group = _moisture_group(ctx,"chunk:"+cid,[cid],reservation=drift_id)
        _reserve_wet_members(ctx,group,drift_id,[entity_id])
    p = _new(ctx, "drift", id=drift_id, entity_id=entity_id, remaining_ticks=ticks, route=points,
             moisture_chunks=moisture_chunks,
             route_index=0, chunk_ids=[m.chunk_id(*m.chunk_for(entity["x"], entity["y"]))], start_delay=start_delay)
    links["drift"] = p["id"]
    _meta_set(ctx,"processes:"+entity_id,links)
    return p


def destroy_entity(ctx, entity):
    """Persist destruction and release each direct child exactly once atomically."""
    if entity["state"]["destroyed"]:
        return False
    x, y = m.entity_location(ctx, entity)
    contents = list(entity["state"]["contents"])
    entity["state"].update(destroyed=True, burning=False, contents=[], water=0, open=True)
    m.save_entity(ctx, entity)
    for eid in contents:
        child = m.get_entity(ctx, eid)
        if child["container"] != entity["id"]:
            raise RuleViolation("容器内容的归属已变化。")
        m.relocate_tree(ctx, child, owner=None, container=None, x=x, y=y)
    m.record_change(ctx, f"{entity['name']}烧毁，内容落在地面，原来的障碍消失。", x=x, y=y,
                    entity_id=entity["id"], action="destroyed")
    return True


def _finished_opportunity(ctx, process):
    from .generation import offer_generation
    if process["kind"] == "rain":
        for point in process.get("centers", [])[:4]:
            offer_generation(ctx, point["x"], point["y"], trigger="environment_change")
    elif process["kind"] == "drift":
        entity = m.get_entity(ctx, process["entity_id"], required=False)
        if entity and m.is_ground(entity):
            offer_generation(ctx, entity["x"], entity["y"], trigger="environment_change")


def _begin_batch(ctx):
    ctx._grove_process_cache = {}
    ctx._grove_process_versions = {}
    ctx._grove_pending_processes = set()
    ctx._grove_meta_cache = {}
    ctx._grove_pending_meta = set()


def _flush_batch(ctx):
    # Each process/member group and each per-entity link is persisted once even
    # when weather, moisture and fire all touch it during the ordered frame.
    for pid in sorted(ctx._grove_pending_processes):
        process = ctx._grove_process_cache[pid]
        value = {k:v for k,v in process.items() if k != "version"}
        ctx.set_state(m.PROCESSES,pid,value,expected_version=ctx._grove_process_versions[pid])
    for key in sorted(ctx._grove_pending_meta):
        ctx.set_state(m.META,key,ctx._grove_meta_cache[key])


def _rain_members(ctx, process):
    result = []
    for cid in process["chunk_ids"]:
        _,cx,cy = cid.split("_")
        for eid in m.get_chunk(ctx,int(cx),int(cy))["entity_ids"]:
            entity = m.get_entity(ctx,eid)
            if m.is_ground(entity) and not entity["state"]["destroyed"] and "wettable" in entity["capabilities"]:
                result.append(eid)
    return sorted(set(result))


def tick(ctx, args):
    from . import building
    clock = ctx.get_state(m.META,"clock")
    if args["epoch"] != clock["epoch"] or args["sequence"] != clock["sequence"]:
        return FunctionOutcome({"advanced":False,"reason":"superseded"})
    late = ctx.timer is not None and ctx.now-ctx.timer.due_at > 2
    sequence = clock["sequence"] if late else clock["sequence"]+1
    if late:
        clock["epoch"] = m.identifier("clock")
    clock["sequence"] = sequence
    clock["last_tick_at"] = ctx.now
    changed, wet_by_rain, wet_updates = [], set(), {}
    affected, affected_keys = [], set()
    def affect(entity=None, points=None):
        entries = points or ([{"x": entity.get("x"), "y": entity.get("y"), "entity_id": entity["id"]}]
                             if entity and entity.get("owner") is None and not entity.get("container") else [])
        for entry in entries:
            key = (entry.get("x"), entry.get("y"))
            if all(type(n) is int for n in key) and key not in affected_keys and len(affected) < 80:
                affected.append(entry)
                affected_keys.add(key)
    _begin_batch(ctx)
    indexed = [p for pid in active_process_ids(ctx) if (p:=get_process(ctx,pid)) and p["status"]=="active"]
    if not late:
        # 1. Admitted events cover at most128 uniquely reserved objects.
        # Every member receives this logical second's rain, irrespective of
        # density; the fixed end_tick never counts scans or physical passes.
        rains = [p for p in indexed if p["kind"]=="rain" and sequence>p["start_tick"]]
        for process in rains:
            process.pop("rain_pending",None)
            if "end_tick" not in process:
                process["end_tick"] = sequence+process["remaining_ticks"]-1
            if sequence<=process["end_tick"]:
                for eid in _rain_members(ctx,process):
                    entity = wet_updates.setdefault(eid, m.get_entity(ctx,eid))
                    water = min(process["intensity"],process["water_remaining"])
                    process["water_remaining"] -= water
                    effect = building.rain_effect(ctx, entity, water)
                    entity["state"]["wetness"] = effect["wetness"]
                    entity["state"]["water"] = effect["water"]
                    if entity["state"]["wetness"]>=35:
                        entity["state"]["burning"] = False
                        if entity["state"].get("recipe") == "hearth":
                            entity["state"]["lit"] = False
                    if "water_source" in entity["capabilities"] and not effect.get("protected_by"):
                        entity["state"]["water_remaining"] += min(water,
                            entity["state"]["water_source_capacity"]-entity["state"]["water_remaining"])
                    if not effect.get("protected_by"):
                        wet_by_rain.add(eid)
            process["remaining_ticks"] = max(0,process["end_tick"]-sequence)
            if sequence>=process["end_tick"] or process["water_remaining"]<=0:
                process["status"] = "complete"
                _release_moisture_reservations(ctx,process)
                _finished_opportunity(ctx,process)
                changed.append("短雨结束。")
                affect(points=process.get("centers", []))
            _save(ctx,process)

        # 2. Admission bounds the entire wet index, so every member dries by
        # exactly two units each non-rain logical second. No entity is deferred.
        moisture_tasks = []
        for pid in active_process_ids(ctx):
            process = get_process(ctx,pid)
            if process and process["kind"]=="moisture" and process["status"]=="active":
                members = process.get("entity_ids",[process["entity_id"]] if process["entity_id"] else [])
                moisture_tasks.extend((pid,eid) for eid in members)
        for pid,eid in sorted(moisture_tasks):
            entity = wet_updates.get(eid) or m.get_entity(ctx,eid,required=False)
            if not entity or entity["state"]["destroyed"] or entity["state"].get("retired"):
                _remove_moisture_member(ctx,get_process(ctx,pid),eid)
                continue
            wet_updates[eid] = entity
        for eid,entity in sorted(wet_updates.items()):
            if entity["state"]["wetness"]>=35 and entity["state"]["burning"]:
                entity["state"]["burning"] = False
                changed.append(f"{entity['name']}被水熄灭。")
                affect(entity)
            if eid not in wet_by_rain:
                entity["state"]["wetness"] = max(0,entity["state"]["wetness"]-2)

        # Installed stone hearths add bounded controlled heat. Their targets are
        # deduplicated by the building helper; overlapping hearths do not stack.
        before_heat = len(changed)
        heated = building.tick_structures(ctx, sequence, changed, wet_updates)
        for eid in sorted(heated):
            entity = wet_updates[eid]
            entity["state"]["wetness"] = max(0, entity["state"]["wetness"]-6)
        # One entity write for weather, natural drying, and controlled heat.
        # New rain members reconcile their existing reserved moisture group here.
        for eid,entity in sorted(wet_updates.items()):
            m.save_entity(ctx,entity)
        if len(changed) != before_heat:
            for eid in _meta_get(ctx, "active_hearths", []):
                affect(m.get_entity(ctx, eid, required=False))

        # 3. Every admitted fire burns at the same two-unit-per-second rate.
        # Fire and total burning-container contents are reserved at admission.
        burning = [pid for pid in active_process_ids(ctx) if (p:=get_process(ctx,pid))
                   and p["kind"]=="burning" and p["status"]=="active"]
        spread_remaining = SPREAD_BATCH
        for pid in sorted(burning):
            process = get_process(ctx,pid)
            entity = m.get_entity(ctx,process["entity_id"],required=False)
            if not entity or not entity["state"]["burning"]:
                process["status"] = "complete"
                _save(ctx,process)
                continue
            if not m.is_ground(entity) or m.is_safe(ctx,entity["x"],entity["y"]) or entity["state"]["wetness"]>=35:
                entity["state"]["burning"] = False
                m.save_entity(ctx,entity)
                continue
            next_fuel = max(0,entity["state"]["fuel"]-2)
            entity["state"]["fuel"] = next_fuel
            if not next_fuel:
                destroy_entity(ctx,entity)
                changed.append(f"{entity['name']}烧毁。")
                affect(entity)
                continue
            m.save_entity(ctx,entity)
            if sequence%3==0 and spread_remaining:
                for target in m.public_entities_near(ctx,entity["x"],entity["y"],radius=1,limit=96):
                    if not spread_remaining:
                        break
                    if abs(target["x"]-entity["x"])+abs(target["y"]-entity["y"])!=1:
                        continue
                    state = target["state"]
                    if ("flammable" in target["capabilities"] and not state["destroyed"] and not state["burning"]
                        and state["fuel"]>0 and state["wetness"]<35 and not m.is_safe(ctx,target["x"],target["y"])):
                        if not can_ignite(ctx,target):
                            if "环境过程已满，新的火蔓延暂缓。" not in changed:
                                changed.append("环境过程已满，新的火蔓延暂缓。")
                            continue
                        state["burning"] = True
                        m.save_entity(ctx,target)
                        spread_remaining -= 1
                        changed.append(f"火蔓延到相邻的{target['name']}。")
                        affect(target)

        # 4. Drift routes were materialized and moisture capacity reserved at
        # admission; the tick performs only bounded one-step movement.
        drifts = [p for p in indexed if p["kind"]=="drift" and sequence>=p["start_tick"]]
        for process in sorted(drifts,key=lambda p:p["id"]):
            entity = m.get_entity(ctx,process["entity_id"],required=False)
            if not entity or not m.is_ground(entity) or entity["state"]["destroyed"]:
                process["status"] = "complete"
            else:
                next_index = process["route_index"]+1
                if next_index>=len(process["route"]):
                    process["status"] = "complete"
                else:
                    point = process["route"][next_index]
                    occupants = m.public_entities_near(ctx,point["x"],point["y"],radius=0,limit=96)
                    if (m.is_safe(ctx,point["x"],point["y"])
                        or m.public_players_near(ctx,point["x"],point["y"],radius=0)
                        or any(e["id"]!=entity["id"] and not e["state"]["destroyed"] for e in occupants)):
                        process["status"] = "complete"
                    else:
                        entity["x"],entity["y"] = point["x"],point["y"]
                        if "wettable" in entity["capabilities"]:
                            entity["state"]["wetness"] = min(100,entity["state"]["wetness"]+12)
                        m.save_entity(ctx,entity)
                        process["route_index"] = next_index
                        process["chunk_ids"] = [m.chunk_id(*m.chunk_for(entity["x"],entity["y"]))]
                        if m.tile_at(ctx,entity["x"],entity["y"])!="water":
                            process["status"] = "complete"
                            changed.append(f"{entity['name']}漂到了岸边。")
                            affect(entity)
                process["remaining_ticks"] -= 1
                if process["remaining_ticks"]<=0:
                    process["status"] = "complete"
            _save(ctx,process)
            if process["status"]=="complete":
                _release_moisture_reservations(ctx,process)
                _finished_opportunity(ctx,process)
        # A paid manual tending advances once, then completes. Stock is never
        # reset to capacity and no autonomous repeated harvest is scheduled.
        growth = [p for p in indexed if p["kind"] == "growth"]
        for process in sorted(growth, key=lambda p: p["id"]):
            node = m.get_entity(ctx, process["entity_id"], required=False)
            process["remaining_ticks"] = max(0, process["end_tick"]-sequence)
            if not node or not m.is_ground(node) or node["state"].get("kind") != "resource_node":
                process["status"] = "complete"
            elif sequence >= process["end_tick"]:
                state = node["state"]
                state["stock"] = min(state["stock_capacity"], state["stock"]+1)
                state["regrowth_tick"] = None
                m.save_entity(ctx, node)
                process["status"] = "complete"
                _finished_opportunity(ctx, process)
                changed.append(f"{node['name']}经照料再生了一份材料。")
                affect(node)
            _save(ctx, process)
    _flush_batch(ctx)
    ctx.set_state(m.META,"clock",clock)
    ctx.schedule_timer(f"grove.tick.{clock['epoch']}.{sequence}","grove.tick",
                       {"epoch":clock["epoch"],"sequence":sequence},due_at=ctx.now+1)
    events = ()
    if changed:
        for summary in changed[:8]:
            m.record_change(ctx,summary,x=None,y=None,action="process")
        events = (StreamEvent("grove.changes","process",{"summary":" ".join(changed[:8]),"tick":sequence,
                              "affected":affected},key=f"tick-{sequence}"),)
    return FunctionOutcome({"advanced":not late,"paused_resume":late,"tick":sequence,
                            "changes":changed[:16],"activity_limits":{"wet_or_reserved":MAX_WET_ENTITIES,
                            "burning":MAX_BURNING_ENTITIES,"burning_contents":MAX_BURNING_CONTENTS,
                            "events":MAX_EVENTS,"growth":MAX_GROWTH,"hearths":building.MAX_HEARTHS}},events=events)


def visible_processes(ctx, x, y, radius=8):
    chunks = set(m.chunk_ids_around(x, y, radius))
    result = []
    for pid in active_process_ids(ctx):
        p = get_process(ctx, pid)
        if p is None or p["status"] != "active":
            continue
        visible = bool(chunks.intersection(p["chunk_ids"]))
        if p.get("owner"):
            visible = p["owner"] == ctx.actor_role_id
        if p.get("entity_id"):
            entity = m.get_entity(ctx,p["entity_id"],required=False)
            if entity and (entity["owner"] == ctx.actor_role_id or m.is_ground(entity)):
                ex,ey = m.entity_location(ctx,entity)
                visible = ex is not None and max(abs(ex-x),abs(ey-y)) <= radius
        if visible:
            value = {k:p[k] for k in ("id","kind","status","entity_id","chunk_ids","remaining_ticks","start_tick","version")}
            if "end_tick" in p:
                value["end_tick"] = p["end_tick"]
            result.append(value)
    return result[:64]
