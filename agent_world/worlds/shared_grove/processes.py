"""Bounded, versioned physical seconds using pure rules and a single flush."""
from __future__ import annotations

from copy import deepcopy

from agent_world import FunctionOutcome
from agent_world.errors import RuleViolation
from agent_world.world_streams import StreamEvent
from agent_world.simulation import matter as sm
from agent_world.simulation.effects import Break, Source, Transfer
from agent_world.simulation.frame import Frame

from . import model as m, commit, activity
from .activity import (MAX_WET_ENTITIES, MAX_BURNING_ENTITIES, MAX_BURNING_CONTENTS, MAX_EVENTS,
    MAX_GROWTH, MAX_PROCESSES, MAX_PROCESS_BYTES, MAX_FRAME_CHUNKS, MAX_FRAME_META, MAX_RAIN_CHUNKS,
    MAX_RAIN_INTENSITY, SPREAD_BATCH, WEATHER_BATCH, MOISTURE_BATCH, BURNING_BATCH, DRIFT_BATCH)
from .definitions import COMBUSTION_CADENCE_TICKS, REGISTRY, RULES_VERSION, registry_for, bind_material, mixture_capacity


def _new(ctx, kind, *, ticks, start_delay=0, **fields):
    clock = ctx.get_state(m.META, "clock", {"sequence": 0})
    start = clock["sequence"]+start_delay
    return activity.save_process(ctx, {"id": m.identifier("process"), "kind": kind, "status": "active", "created_at": ctx.now,
        "created_by": ctx.actor_role_id, "start_tick": start, "end_tick": start+ticks,
        "remaining_ticks": ticks, "entity_id": fields.pop("entity_id", None), "chunk_ids": fields.pop("chunk_ids", []),
        "rules_version": RULES_VERSION, "rule_version": 1, **fields})


def _admit_event(ctx, chunks):
    events = [activity.get_process(ctx, pid) for pid in activity.active_process_ids(ctx)]
    events = [p for p in events if p and p["status"] == "active" and p["kind"] in {"rain", "drift"}]
    if len(events) >= MAX_EVENTS:
        raise RuleViolation("最多同时运行4个降雨或漂流事件。")
    if any(set(chunks).intersection(p.get("moisture_chunks", p["chunk_ids"])) for p in events):
        raise RuleViolation("同一地块不能重叠降雨或漂流事件。")
    activity.physical_closure(ctx, reserve_chunks=chunks)


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
        commit.ensure_chunk(ctx, cx, cy)
        chunks.append(cid)
    chunks.sort()
    _admit_event(ctx, chunks)
    objects, _ = activity.physical_closure(ctx, reserve_chunks=chunks)
    return _new(ctx, "rain", ticks=ticks, start_delay=start_delay, chunk_ids=chunks, moisture_chunks=chunks,
                intensity=intensity, water_remaining=intensity*ticks*len(objects), reserved_ids=[])


def start_growth(ctx, node_id, ticks=6):
    node = m.get_entity(ctx, node_id)
    resource = node["components"].get("resource")
    material = REGISTRY.materials.get(resource.get("material")) if resource else None
    if ticks != 6 or not resource or not material or not material.absorption_per_piece or node["state"]["stock"] >= resource["capacity"] or node["state"].get("regrowth_tick") is not None:
        raise RuleViolation("只有未满、未再生的吸水资源节点可用水养护。")
    if sum(activity.get_process(ctx, pid)["kind"] == "growth" for pid in activity.active_process_ids(ctx)) >= MAX_GROWTH:
        raise RuleViolation("最多同时照料8个资源节点。")
    activity.physical_closure(ctx, [node])
    x, y = m.entity_location(ctx, node)
    process = _new(ctx, "growth", ticks=6, entity_id=node_id, chunk_ids=[m.chunk_id(*m.chunk_for(x, y))], reserved_ids=[node_id])
    node["state"]["regrowth_tick"] = process["end_tick"]
    resource["regrowth_tick"] = process["end_tick"]
    commit.save_entity(ctx, node)
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
        commit.ensure_chunk(ctx, cx, cy)
        chunks.add(m.chunk_id(cx, cy))
        previous = point
    if len(chunks) > 4:
        raise RuleViolation("漂流最多跨4个地块。")
    if any((p := activity.get_process(ctx, pid)) and p["kind"] == "drift" and p["entity_id"] == entity_id for pid in activity.active_process_ids(ctx)):
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
    objects, _ = activity.physical_closure(ctx, extra)
    frame = Frame(registry_for(objects), objects, spills=spills, bind_material=bind_material, mixture_capacity=mixture_capacity)
    frame.apply([Break(entity["id"])], allowed_sinks={"drain"})
    frame.apply(spill_rule(frame.snapshot()), allowed_sinks={"drain"}, allocate=True)
    commit.commit_frame(ctx, frame, chunks=(chunk["id"],) if chunk else ())
    return bool(frame.broken)


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
    with commit.candidate_batch(ctx) as batch:
        changes, affected = [], []
        processes = [p for pid in activity.active_process_ids(ctx) if (p := activity.get_process(ctx, pid)) and p["status"] == "active"]
        if any(p.get("rules_version") != RULES_VERSION or p.get("rule_version") != 1 for p in processes):
            raise RuleViolation("持久过程规则版本不匹配。请使用当前规则的新世界。")
        if not late:
            objects, chunks = activity.physical_closure(ctx)
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
            frame = Frame(registry_for(objects), objects, sequence, protected=protected, spills=spills, bind_material=bind_material, mixture_capacity=mixture_capacity)
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
            # Rain proposes its finite capture before the reducer checks actual
            # shared liquid/mixture room. Rejected capture remains a declared drain
            # and consumes the original rain budget; it cannot move to another target.
            proposed_water = {eid: sm.quantity(entity, "water", "contained") for eid, entity in frame.objects.items()}
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
                        captured = min(amount, rate, max(0, collect-proposed_water[eid])) if collect else 0
                        if captured:
                            inputs.append({"target": eid, "material": "water", "quantity": captured, "slot": "contained", "reason": "rain"})
                            proposed_water[eid] += captured
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
                activity.save_process(ctx, process)
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
                activity.save_process(ctx, process)
            # Spread never advances the newly ignited material's rate in this tick.
            batch.entities.update(deepcopy(frame.objects))
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
                        lot = next((lot for lot in current["matter"] if sm.ignitable_lot(frame.registry, current, lot, frame.objects)), None)
                        if lot:
                            lot["burning"] = True
                            sm.project(registry_for(frame.objects), current)
                            batch.entities[current["id"]] = deepcopy(current)
                            try:
                                activity.physical_closure(ctx, list(frame.objects.values()))
                            except RuleViolation:
                                lot["burning"] = False
                                sm.project(registry_for(frame.objects), current)
                                batch.entities[current["id"]] = deepcopy(current)
                                continue
                            frame.dirty.add(current["id"])
                            spread -= 1
                            changes.append(f"火蔓延到相邻的{current['name']}。")
                            if not spread:
                                break
            committed = commit.commit_frame(ctx, frame, chunks=chunks, tick=sequence)
            for eid in sorted(committed):
                entity = m.get_entity(ctx, eid)
                if m.is_ground(entity):
                    affected.append({"x": entity["x"], "y": entity["y"], "entity_id": eid})
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
                    activity.save_process(ctx, process)
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
            commit.record_change(ctx, summary, x=None, y=None, action="process")
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
    for pid in activity.active_process_ids(ctx):
        process = activity.get_process(ctx, pid)
        if not process or process["status"] != "active" or not chunks.intersection(process["chunk_ids"]):
            continue
        if process["entity_id"]:
            entity = m.get_entity(ctx, process["entity_id"], required=False)
            if not entity or entity.get("owner") not in (None, ctx.actor_role_id):
                continue
        result.append({key: process[key] for key in ("id", "kind", "status", "entity_id", "chunk_ids", "remaining_ticks", "start_tick", "end_tick", "version", "rules_version")})
    return result[:MAX_PROCESSES]
