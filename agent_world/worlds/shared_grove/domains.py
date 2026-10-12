"""Durable, bounded groups of objects that can change one another in a step.

Spatial indexes find candidates; sharing a chunk is never an interaction edge.
The directory contains saved groups, while READY contains only actual work.
All writes use the existing candidate owner's CAS overlay when one is present.
"""
from __future__ import annotations

from contextlib import contextmanager
from copy import deepcopy
import hashlib

from agent_world import StateRule
from agent_world.errors import RuleViolation
from agent_world.simulation import matter as sm
from . import model as m

DOMAINS = "grove.domains"
OWNERS = "grove.domain.entities"
REGIONS = "grove.domain.regions"
READY = "grove.domain.ready"
DIRTY = "grove.domain.dirty"
CONTROL = "grove.domain.control"
ACTIVITY_KEYS = frozenset({"creative_instances", "physical_active", "active_processes", "active_hearths", "spill_chunks", "spill_points"})
INDEX_VERSION = 1
MAX_DOMAIN_BYTES = 32768
MAX_REGION_DOMAINS = m.MAX_CHUNK_ENTITIES + 12


class CapacityPressure(RuleViolation):
    """A known finite work budget, rather than an invalid world fact."""


class DomainBusy(RuleViolation):
    """This candidate rolled back while its related frame was unsettled."""

    retryable = True
    recovery = "wait_then_retry_same_operation"


def _get(ctx, scope_name, key, default=None):
    from .execution import _get as read
    return read(ctx, scope_name, key, default)


def _set(ctx, scope_name, key, value):
    from .execution import _set as write
    return write(ctx, scope_name, key, value)


def _delete(ctx, scope_name, key):
    from .execution import _delete as delete
    return delete(ctx, scope_name, key)


def installed(ctx):
    return _get(ctx, CONTROL, "index_version") == INDEX_VERSION


def get(ctx, domain_id):
    return _get(ctx, DOMAINS, domain_id)


def current(ctx):
    domain_id = getattr(ctx, "_grove_domain", None)
    if isinstance(domain_id, dict):
        return deepcopy(domain_id)
    return get(ctx, domain_id) if domain_id else None


@contextmanager
def scope(ctx, domain):
    previous = getattr(ctx, "_grove_domain", None)
    ctx._grove_domain = domain if isinstance(domain, dict) and domain.get("preview") else domain["id"] if isinstance(domain, dict) else domain
    try:
        yield current(ctx)
    finally:
        ctx._grove_domain = previous


def for_entity(ctx, entity_id):
    owner = _get(ctx, OWNERS, entity_id)
    return get(ctx, owner["domain_id"]) if owner else None


def ready_key(domain):
    return f"{domain['ordinal']:020d}"


def frame(ctx):
    return _get(ctx, CONTROL, "frame")


def _save(ctx, domain):
    if sm.encoded_size(domain) > MAX_DOMAIN_BYTES:
        raise CapacityPressure("相互作用域记录超过已预留的32KiB预算。")
    _set(ctx, DOMAINS, domain["id"], domain)


def _active(domain):
    return any(domain.get(key) for key in ("creative_instances", "physical_active", "active_processes", "spill_chunks"))


def refresh_ready(ctx, domain):
    """Keep a stable seek key. Waking during a frame never joins that frame."""
    old = _get(ctx, READY, ready_key(domain))
    if domain["status"] in {"retired", "pressure", "fault"} or not _active(domain):
        _delete(ctx, READY, ready_key(domain))
        _delete(ctx, DIRTY, ready_key(domain))
        if domain["status"] not in {"retired", "pressure", "fault"}:
            domain["status"] = "sleeping"
    else:
        domain["status"] = "ready"
        if old is None:
            work = frame(ctx)
            clock = ctx.get_state(m.META, "clock", {"sequence": 0})
            eligible = work["target_tick"] + 1 if work and work["status"] in {"preparing", "open"} else clock["sequence"] + 1
            _set(ctx, READY, ready_key(domain), {"domain_id": domain["id"], "eligible_tick": eligible})
        if domain.get("topology_dirty"):
            _set(ctx, DIRTY, ready_key(domain), {"domain_id": domain["id"]})
    _save(ctx, domain)


def activity_get(ctx, key, default=None):
    domain = current(ctx)
    if domain is None:
        return deepcopy(default)
    return deepcopy(domain.get(key, default))


def activity_set(ctx, key, value):
    domain = current(ctx)
    if domain is None:
        if value:
            raise RuleViolation("活动状态写入缺少相互作用域。")
        return
    if domain.get(key) == value:
        return
    domain[key] = deepcopy(value)
    if key in {"active_processes", "spill_points"}:
        _index_regions(ctx, domain, domain["chunks"])
    # A genuine new input unlocks a pressure/fault domain. Merely advancing its
    # queue or polling it does not; sync_entities owns that distinction.
    refresh_ready(ctx, domain)


def _region_ids(ctx, chunk_id):
    return _get(ctx, REGIONS, chunk_id, [])


def nearby(ctx, x, y, radius=0):
    ids = set()
    for cid in m.chunk_ids_around(x, y, radius=radius):
        ids.update(_region_ids(ctx, cid))
    return [domain for did in sorted(ids) if (domain := get(ctx, did)) and domain["status"] != "retired"]


def _index_regions(ctx, domain, before):
    for cid in sorted(set(before) | set(domain["chunks"])):
        ids = set(_region_ids(ctx, cid)) - {domain["id"]}
        if (cid in domain["chunks"] and domain["status"] != "retired"
                and (domain["objects"] or domain["active_processes"] or domain.get("spill_points"))):
            ids.add(domain["id"])
        if len(ids) > MAX_REGION_DOMAINS:
            raise CapacityPressure("局部作用域索引超过真实空间及事件预算。")
        if ids:
            _set(ctx, REGIONS, cid, sorted(ids))
        else:
            _delete(ctx, REGIONS, cid)


def _live(entity):
    return entity is not None and entity["lifecycle"] != "consumed" and not entity["state"].get("retired")


def _processes(ctx, domain_ids):
    from .activity import get_process
    result = {}
    for did in sorted(domain_ids):
        domain = get(ctx, did)
        if domain:
            for pid in domain["active_processes"]:
                process = get_process(ctx, pid)
                if process and process["status"] == "active":
                    result[pid] = process
    return result


def _roots_in(ctx, cid):
    _, cx, cy = cid.split("_")
    chunk = m.get_chunk(ctx, int(cx), int(cy))
    return [entity for eid in chunk["entity_ids"] if _live(entity := m.get_entity(ctx, eid, required=False))]


def closure(ctx, entity_ids=(), *, reserve_chunks=(), process_ids=(), spill_points=(), include_owned=False):
    """Point-read the complete mutable dependency graph, bounded as it grows.

    Read-only mutable sources are edges too: solving their producer first in a
    separate transaction would change same-phase frozen reads. Public journal
    subscribers are deliberately absent; their immutable bodies use a cutoff.
    """
    from . import activity, execution
    pending = list(entity_ids)
    objects, found_domains, processes = {}, set(), {}
    reserved = set(reserve_chunks)
    scanned_chunks, checked_processes = set(), set()
    selected_spills, checked_spills = set(), set()

    def include_spill(key):
        if key in checked_spills:
            return
        checked_spills.add(key)
        point = tuple(map(int, key.split(",")))
        chunk = m.get_chunk(ctx, *m.chunk_for(*point))
        if not chunk.get("water_reservoirs", {}).get(key):
            return
        selected_spills.add(key)
        pending.extend(obj["id"] for obj in m.public_entities_near(ctx, *point, radius=0, limit=m.MAX_CHUNK_ENTITIES))
        for did in _region_ids(ctx, chunk["id"]):
            owned = get(ctx, did)
            covering = _processes(ctx, [did]) if owned else {}
            covered = any(p["kind"] in {"rain", "drift"} and chunk["id"] in p.get("moisture_chunks", p["chunk_ids"])
                          for p in covering.values())
            if owned and (key in owned.get("spill_points", []) or owned["anchor"] == "spill:"+key or covered):
                found_domains.add(did)
                processes.update(covering)
                if include_owned:
                    pending.extend(owned["objects"])
                    for other in owned.get("spill_points", []):
                        include_spill(other)

    for key in spill_points:
        include_spill(key)
    for pid in process_ids:
        process = activity.get_process(ctx, pid)
        if process and process["status"] == "active":
            processes[pid] = process
    # Weather/process scopes have a shared finite budget. They must include
    # all receivers, even if none needs physics before the rain starts.
    for cid in reserved:
        pending.extend(entity["id"] for entity in _roots_in(ctx, cid))
        found_domains.update(_region_ids(ctx, cid))
        _, cx, cy = cid.split("_")
        for key in m.get_chunk(ctx, int(cx), int(cy)).get("water_reservoirs", {}):
            include_spill(key)
    processes.update(_processes(ctx, found_domains))
    while pending or set(processes) - checked_processes:
        if not pending:
            pid = min(set(processes) - checked_processes)
            checked_processes.add(pid)
            process = processes[pid]
            pending.extend(process.get("reserved_ids", []))
            if process.get("entity_id"):
                pending.append(process["entity_id"])
            if process["kind"] in {"rain", "drift"}:
                covered = set(process.get("moisture_chunks", process["chunk_ids"]))
                reserved.update(covered)
                for cid in covered - scanned_chunks:
                    scanned_chunks.add(cid)
                    pending.extend(entity["id"] for entity in _roots_in(ctx, cid))
                    _, cx, cy = cid.split("_")
                    for key in m.get_chunk(ctx, int(cx), int(cy)).get("water_reservoirs", {}):
                        include_spill(key)
                    if process["kind"] == "rain":
                        for receiver in _roots_in(ctx, cid):
                            for shelter in m.public_entities_near(ctx, receiver["x"], receiver["y"], radius=1, limit=4*m.MAX_CHUNK_ENTITIES):
                                if shelter["components"].get("collector", {}).get("mode") == "shelter":
                                    pending.append(shelter["id"])
                    for did in _region_ids(ctx, cid):
                        candidate = get(ctx, did)
                        # Shared rain/drift admission is a true interaction.
                        # Static objects in the same ordinary chunk are not.
                        if candidate:
                            for other_pid, other in _processes(ctx, [did]).items():
                                if covered.intersection(other.get("moisture_chunks", other["chunk_ids"])):
                                    processes[other_pid] = other
            continue
        eid = pending.pop()
        if eid in objects:
            continue
        entity = m.get_entity(ctx, eid, required=False)
        if not _live(entity):
            continue
        tree = m._tree_objects(ctx, entity)
        new = set(tree) - set(objects)
        objects.update(tree)
        if len(objects) > sm.MAX_FRAME_OBJECTS:
            raise CapacityPressure("完整相互作用域超过64个物件；关系变化没有提交。")
        for oid in sorted(new):
            obj = objects[oid]
            owner = _get(ctx, OWNERS, oid)
            if owner:
                did = owner["domain_id"]
                if did not in found_domains:
                    found_domains.add(did)
                    processes.update(_processes(ctx, [did]))
                    if include_owned:
                        domain = get(ctx, did)
                        if domain:
                            pending.extend(domain["objects"])
                            for key in domain.get("spill_points", []):
                                include_spill(key)
            member = execution._member_for(ctx, obj, execution.get_member(ctx, oid), fingerprint=False) if obj["state"].get("program", {}).get("definition_ref") else None
            if member:
                pending.extend(member["sources"])
            # Validate the actual candidate edge; the source index can still
            # describe the before-state of a binding changed in this batch.
            for subscriber in execution.subscribers(ctx, oid):
                subject = m.get_entity(ctx, subscriber, required=False)
                if _live(subject):
                    actual = execution._member_for(ctx, subject, execution.get_member(ctx, subscriber), fingerprint=False)
                    if actual and oid in actual["sources"]:
                        pending.append(subscriber)
            point = m.entity_location(ctx, obj)
            for pid, process in getattr(ctx, "_grove_migrating_processes", {}).items():
                if oid == process.get("entity_id") or oid in process.get("reserved_ids", []):
                    processes[pid] = process
            if point[0] is None:
                continue
            cid = m.chunk_id(*m.chunk_for(*point))
            for pid, process in getattr(ctx, "_grove_migrating_processes", {}).items():
                if process["kind"] in {"rain", "drift"} and cid in process.get("moisture_chunks", process["chunk_ids"]):
                    processes[pid] = process
            # A new object inside active weather becomes part of its finite
            # scope before it can consume that process's budget.
            for did in _region_ids(ctx, cid):
                for pid, process in _processes(ctx, [did]).items():
                    if process["kind"] in {"rain", "drift"} and cid in process.get("moisture_chunks", process["chunk_ids"]):
                        processes[pid] = process
            if m.is_ground(obj):
                release = m.public_break_reservations(ctx, obj, objects=tree)
                for tile in release["contact_tiles"]:
                    pending.extend(target["id"] for target in m.public_entities_near(ctx, *tile, radius=0, limit=m.MAX_CHUNK_ENTITIES))
                neighbors = m.public_entities_near(ctx, *point, radius=1, limit=4*m.MAX_CHUNK_ENTITIES)
                burning = bool(obj["state"]["burning"] and obj["components"].get("combustion", {}).get("spread"))
                lit = bool(obj["components"].get("heater", {}).get("lit"))
                wet = bool(sm.quantity(obj, "water", "surface") or sm.quantity(obj, "water", "absorbed"))
                for target in neighbors:
                    target_burns = bool(target["state"]["burning"] and target["components"].get("combustion", {}).get("spread"))
                    target_lit = bool(target["components"].get("heater", {}).get("lit"))
                    target_wet = bool(sm.quantity(target, "water", "surface") or sm.quantity(target, "water", "absorbed"))
                    if ((burning or target_burns) and abs(point[0]-target["x"])+abs(point[1]-target["y"]) <= 1
                            or lit and target_wet or target_lit and wet):
                        pending.append(target["id"])
                include_spill(f"{point[0]},{point[1]}")
            # Exposed descendants use their public root's position too. The
            # finite source and every actual receiver share one mutable budget;
            # an empty source or a vessel with no absorption room is no edge.
            contact = (not obj.get("owner") and "absorbent" in obj["components"]
                       and m.tile_at(ctx, *point) == "water" and sm.absorption_targets(obj, tree))
            source = "water_source" in obj["capabilities"] and sm.quantity(obj, "water", "contained")
            if contact or source:
                _, sources = m.region_resources(ctx, *m.chunk_for(*point))
                available = [value for value in sources if sm.quantity(value, "water", "contained")]
                pending.extend(value["id"] for value in available)
                for root in _roots_in(ctx, cid) if available else []:
                    forest = m._tree_objects(ctx, root)
                    if any(not receiver.get("owner") and "absorbent" in receiver["components"]
                           and m.tile_at(ctx, *m.entity_location(ctx, receiver)) == "water"
                           and sm.absorption_targets(receiver, forest) for receiver in forest.values()):
                        pending.append(root["id"])
    members = [eid for eid, obj in objects.items() if obj["lifecycle"] == "intact" and obj["state"].get("program", {}).get("definition_ref")]
    chunks = set(reserved)
    chunks.update(m.chunk_id(*m.chunk_for(*map(int, key.split(",")))) for key in selected_spills)
    for obj in objects.values():
        point = m.entity_location(ctx, obj)
        if point[0] is not None:
            chunks.add(m.chunk_id(*m.chunk_for(*point)))
    if len(members) > execution.MAX_FRONTIER or len(chunks) > activity.MAX_FRAME_CHUNKS:
        raise CapacityPressure("完整相互作用域最多32个声明参与者、8个地块；本次变化未提交。")
    return {"objects": objects, "members": sorted(members), "chunks": sorted(chunks),
            "domains": found_domains, "processes": processes, "spill_points": sorted(selected_spills)}


def _anchor(graph):
    if graph["objects"]:
        return min(graph["objects"])
    if graph.get("spill_points"):
        return "spill:"+min(graph["spill_points"])
    return "region:"+min(graph["chunks"], default="empty")


def _new_domain(ctx, anchor):
    # A disconnected component can reactivate its old anchor identity. Repeated
    # connect/disconnect does not create a permanent checkpoint per attempt.
    did = "domain_" + hashlib.sha256(anchor.encode()).hexdigest()[:32]
    old = get(ctx, did)
    if old:
        old["topology_revision"] += 1
        old["status"] = "sleeping"
        old["last_tick"] = ctx.get_state(m.META, "clock", {"sequence": 0})["sequence"]
        return old
    ordinal = _get(ctx, CONTROL, "last_ordinal", 0) + 1
    _set(ctx, CONTROL, "last_ordinal", ordinal)
    return {"id": did, "anchor": anchor, "ordinal": ordinal, "topology_revision": 1, "status": "sleeping",
            "last_tick": ctx.get_state(m.META, "clock", {"sequence": 0})["sequence"],
            "last_applied_tick": ctx.get_state(m.META, "clock", {"sequence": 0})["sequence"],
            "objects": [], "chunks": [], **{key: [] for key in ACTIVITY_KEYS}}


def _retire(ctx, domain):
    before = domain["chunks"]
    for eid in domain["objects"]:
        owner = _get(ctx, OWNERS, eid)
        if owner and owner["domain_id"] == domain["id"]:
            _delete(ctx, OWNERS, eid)
    domain.update(status="retired", objects=[], chunks=[])
    for key in ACTIVITY_KEYS:
        domain[key] = []
    domain["topology_revision"] += 1
    _delete(ctx, READY, ready_key(domain))
    _delete(ctx, DIRTY, ready_key(domain))
    _index_regions(ctx, domain, before)
    _save(ctx, domain)


def _persist_component(ctx, graph, preferred=None):
    old = [domain for did in sorted(graph["domains"]) if (domain := get(ctx, did)) and domain["status"] != "retired"]
    inherited = graph.get("inherited", old)
    anchor = _anchor(graph)
    anchored = [row for row in old if row["anchor"] in graph["objects"] or row["anchor"] == anchor]
    domain = deepcopy(preferred or (min(anchored, key=lambda row: row["ordinal"]) if anchored else _new_domain(ctx, anchor)))
    before_chunks, before_objects = domain["chunks"], domain["objects"]
    topology_changed = (set(before_objects) != set(graph["objects"]) or set(before_chunks) != set(graph["chunks"])
                        or set(domain.get("spill_points", [])) != set(graph["spill_points"]))
    if old and (topology_changed or any(row["id"] != domain["id"] for row in old)):
        assert_boundary(ctx, old, topology=len(old) > 1)
    if topology_changed and before_objects:
        domain["topology_revision"] += 1
    objects = set(graph["objects"])
    for key in ACTIVITY_KEYS:
        previous = set(domain.get(key, []))
        for row in inherited:
            previous.update(row.get(key, []))
        if key in {"physical_active", "creative_instances", "active_hearths"}:
            previous.intersection_update(objects)
        elif key == "active_processes":
            previous.intersection_update(graph["processes"])
        elif key == "spill_points":
            previous.intersection_update(graph["spill_points"])
        else:
            previous.intersection_update(graph["chunks"])
        domain[key] = sorted(previous)
    domain["active_processes"] = sorted(graph["processes"])
    domain["spill_points"] = graph["spill_points"]
    domain["spill_chunks"] = sorted({m.chunk_id(*m.chunk_for(*map(int, key.split(",")))) for key in graph["spill_points"]})
    if inherited:
        domain["last_tick"] = max(row["last_tick"] for row in inherited)
        domain["last_applied_tick"] = min(row.get("last_applied_tick", row["last_tick"]) for row in inherited)
        paused = next((row for row in inherited if row["status"] in {"pressure", "fault"}), None)
        if paused:
            domain["status"] = paused["status"]
            domain["failure"] = deepcopy(paused.get("failure"))
    domain["objects"], domain["chunks"] = sorted(objects), graph["chunks"]
    domain["topology_dirty"] = False
    _delete(ctx, DIRTY, ready_key(domain))
    for row in old:
        if row["id"] != domain["id"]:
            _retire(ctx, row)
    for eid in objects:
        _set(ctx, OWNERS, eid, {"domain_id": domain["id"]})
    for eid in set(before_objects) - objects:
        owner = _get(ctx, OWNERS, eid)
        if owner and owner["domain_id"] == domain["id"]:
            _delete(ctx, OWNERS, eid)
    _index_regions(ctx, domain, before_chunks)
    refresh_ready(ctx, domain)
    return domain


def ensure(ctx, entity_ids=(), reserve_chunks=(), process_ids=(), *, spill_points=(), create=True):
    graph = closure(ctx, entity_ids, reserve_chunks=reserve_chunks, process_ids=process_ids,
                    spill_points=spill_points, include_owned=True)
    if not graph["objects"] and not graph["chunks"] and not graph["processes"]:
        return current(ctx)
    if not create and not graph["domains"]:
        return None
    if ctx.access == "read":
        domain = {"id": "preview", "preview": True, "objects": sorted(graph["objects"]), "chunks": graph["chunks"],
                  **{key: [] for key in ACTIVITY_KEYS}}
        for did in graph["domains"]:
            saved = get(ctx, did)
            if saved:
                for key in ACTIVITY_KEYS:
                    domain[key] = sorted(set(domain[key]) | set(saved.get(key, [])))
        domain["active_processes"] = sorted(graph["processes"])
        domain["spill_points"] = graph["spill_points"]
        domain["spill_chunks"] = sorted({m.chunk_id(*m.chunk_for(*map(int, key.split(",")))) for key in graph["spill_points"]})
        ctx._grove_domain = domain
        return domain
    domain = _persist_component(ctx, graph)
    ctx._grove_domain = domain["id"]
    return domain


def _needs_domain(ctx, entity):
    if not _live(entity):
        return False
    if entity["state"].get("program", {}).get("definition_ref") or for_entity(ctx, entity["id"]):
        return True
    if m.is_ground(entity):
        chunk = m.get_chunk(ctx, *m.chunk_for(entity["x"], entity["y"]))
        if chunk.get("water_reservoirs", {}).get(f"{entity['x']},{entity['y']}"):
            return True
    from .activity import needs_physics
    from .definitions import registry_for
    tree = m._tree_objects(ctx, entity)
    return any(needs_physics(ctx, obj, tree, registry_for(tree)) for obj in tree.values())


def sync_entities(ctx, changed):
    """Rebuild only affected saved groups; no directory or history scan.

    In a running domain, its pre-admitted graph remains the atomic scope until
    the frame closes. Relationship removal can then split it on the next real
    action. Output-only updates do not rebuild topology at all.
    """
    if not installed(ctx) or not changed:
        return []
    current_work = getattr(ctx, "_grove_domain_work", False)
    domain = current(ctx)
    if current_work:
        if domain is None:
            raise RuleViolation("域任务缺少已接纳作用范围。")
        batch = getattr(ctx, "_grove_batch", None)
        for eid in set(changed) - set(domain["objects"]):
            record = batch.entity_records.get(eid) if batch else ctx.get_state_record(m.ENTITIES, eid)
            before = record["value"] if record and record["exists"] else None
            if not _tail_metadata_only(before, changed[eid]):
                raise CapacityPressure("本步变化超出已冻结相互作用域；本步没有提交。")
        for eid in set(changed) & set(domain["objects"]):
            record = batch.entity_records.get(eid) if batch else ctx.get_state_record(m.ENTITIES, eid)
            before = record["value"] if record and record["exists"] else None
            if before is None or m.physical_shape(before) != m.physical_shape(changed[eid]):
                mark_dirty(ctx, domain)
                break
        return [domain]
    seeds, old_domains = set(), {}
    for eid, entity in changed.items():
        old = for_entity(ctx, eid)
        if old:
            old_domains[old["id"]] = old
            seeds.update(old["objects"])
        if _needs_domain(ctx, entity):
            seeds.add(eid)
    # Point readers of changed inputs have to join before any frontier changes.
    from . import execution
    for eid in changed:
        seeds.update(execution.subscribers(ctx, eid))
    if not seeds:
        return []
    components, unassigned = [], set(seeds)
    while unassigned:
        eid = min(unassigned)
        graph = closure(ctx, [eid])
        for did in graph["domains"]:
            if did not in old_domains:
                old = get(ctx, did)
                if old:
                    old_domains[did] = old
                    unassigned.update(old["objects"])
        unassigned.difference_update(graph["objects"])
        unassigned.discard(eid)
        if graph["objects"]:
            components.append(graph)
    # A process with no material receiver still owns its finite footprint.
    assigned_processes = {pid for graph in components for pid in graph["processes"]}
    for old in old_domains.values():
        remaining = set(old["active_processes"]) - assigned_processes
        if remaining:
            graph = closure(ctx, process_ids=remaining)
            components.append(graph)
    # A pool can remain after its last object is removed. It is still a real
    # local resource, not a reason to annex every other root in its chunk.
    assigned_spills = {key for graph in components for key in graph["spill_points"]}
    remaining_spills = {key for old in old_domains.values() for key in old.get("spill_points", [])} - assigned_spills
    while remaining_spills:
        key = min(remaining_spills)
        graph = closure(ctx, spill_points=[key])
        remaining_spills.discard(key)
        remaining_spills.difference_update(graph["spill_points"])
        if graph["spill_points"]:
            components.append(graph)
    result, reused = [], set()
    for graph in components:
        graph["inherited"] = [old_domains[did] for did in graph["domains"] if did in old_domains]
        candidates = [get(ctx, did) for did in graph["domains"] - reused]
        candidates = [row for row in candidates if row and row["status"] != "retired"]
        anchor = _anchor(graph)
        anchored = [row for row in candidates if row["anchor"] in graph["objects"] or row["anchor"] == anchor]
        preferred = min(anchored, key=lambda row: row["ordinal"]) if anchored else _new_domain(ctx, anchor)
        # During a split the first component cannot retire rows still supplying
        # the next component. Only the chosen row is owned by this component.
        graph["domains"] = {row["id"] for row in candidates}
        result.append(_persist_component(ctx, graph, preferred))
        reused.update(graph["domains"])
    for old in old_domains.values():
        if old["id"] not in reused and get(ctx, old["id"])["status"] != "retired":
            _retire(ctx, old)
    # Changes to actual inputs unlock a domain after a known budget failure.
    for domain in result:
        latest = get(ctx, domain["id"])
        if latest["status"] in {"pressure", "fault"} and not getattr(ctx, "_grove_domain_preparing", False):
            latest["status"] = "sleeping"
            latest.pop("failure", None)
            refresh_ready(ctx, latest)
    if len(changed) == 1:
        focus = for_entity(ctx, next(iter(changed)))
        ctx._grove_domain = focus["id"] if focus else None
    return result


def mark_dirty(ctx, domain=None):
    domain = domain or current(ctx)
    if domain and not domain.get("preview"):
        domain["topology_dirty"] = True
        if domain["status"] == "ready":
            _set(ctx, DIRTY, ready_key(domain), {"domain_id": domain["id"]})
        _save(ctx, domain)


def prepare(ctx, domain, frame_id, target_tick):
    """Rebuild one dirty group while no domain has begun the new frame."""
    previous = getattr(ctx, "_grove_domain_preparing", False)
    ctx._grove_domain_preparing = True
    try:
        with scope(ctx, domain):
            entities = {eid: obj for eid in domain["objects"] if (obj := m.get_entity(ctx, eid, required=False)) is not None}
            if entities:
                result = sync_entities(ctx, entities)
            else:
                graph = closure(ctx, process_ids=domain["active_processes"], spill_points=domain.get("spill_points", []))
                graph["domains"].add(domain["id"])
                result = [_persist_component(ctx, graph, domain)]
            for updated in result:
                updated = get(ctx, updated["id"])
                updated["prepared_frame"] = frame_id
                updated["topology_dirty"] = False
                _delete(ctx, DIRTY, ready_key(updated))
                _save(ctx, updated)
                if _active(updated) and updated["status"] == "ready":
                    # A split changes bookkeeping, not its physical cadence.
                    _set(ctx, READY, ready_key(updated), {"domain_id": updated["id"], "eligible_tick": target_tick})
            return result
    finally:
        ctx._grove_domain_preparing = previous


def _tail_metadata_only(before, after):
    if before is None:
        return False
    copies = [deepcopy(before), deepcopy(after)]
    for value in copies:
        value.pop("version", None)
        value.pop("interaction_revision", None)
        program = value.get("state", {}).get("program", {})
        process = program.get("process", {})
        for key in ("status", "error", "event_gap"):
            process.pop(key, None)
        program.get("recorder", {}).pop("recorded_count", None)
    return copies[0] == copies[1]


def pending_in_frame(ctx, domain):
    work = frame(ctx)
    if not work or work["status"] not in {"preparing", "open"} or domain["last_tick"] >= work["target_tick"]:
        return False
    ready = _get(ctx, READY, ready_key(domain))
    return bool(ready and domain["ordinal"] <= work["high_water"] and ready["eligible_tick"] <= work["target_tick"])


def assert_boundary(ctx, domain_rows, *, topology=False):
    rows = {row["id"]: row for row in domain_rows if row and row["status"] != "retired"}
    work = frame(ctx)
    if work and work["status"] == "preparing" and getattr(ctx, "_grove_domain_preparing", False):
        return
    if any(pending_in_frame(ctx, row) for row in rows.values()):
        raise DomainBusy("相关世界作用域正在结算；请在当前轮结束后重试，原动作没有执行。",
            retry_after_seconds=0.1, details={"settlement": {
                "state": "calculating", "target_tick": work["target_tick"], "operation_committed": False}})
    # Settled or newly woken groups already share a local frame boundary. A
    # distant unfinished domain must not stop their construction or connection.


def guard_tiles(ctx, points):
    """Fence exact participants and process footprints, not ordinary chunks."""
    for x, y in points:
        if x is None or y is None:
            continue
        cid = m.chunk_id(*m.chunk_for(x, y))
        for domain in nearby(ctx, x, y, radius=1):
            if not pending_in_frame(ctx, domain):
                continue
            if f"{x},{y}" in domain.get("spill_points", []):
                assert_boundary(ctx, [domain])
            processes = _processes(ctx, [domain["id"]])
            if any(p["kind"] == "rain" and cid in p.get("moisture_chunks", p["chunk_ids"])
                   or p["kind"] == "drift" and {"x": x, "y": y} in p.get("route", []) for p in processes.values()):
                assert_boundary(ctx, [domain])
            for eid in domain["objects"]:
                entity = m.get_entity(ctx, eid, required=False)
                if _live(entity) and m.entity_location(ctx, entity) == (x, y):
                    assert_boundary(ctx, [domain])
                # A new neighbor of an admitted fire/heater would otherwise
                # bypass the pre-frame physical closure.
                if _live(entity) and m.is_ground(entity):
                    if (abs(entity["x"]-x)+abs(entity["y"]-y) <= 1 and entity["state"]["burning"] and entity["components"].get("combustion", {}).get("spread")
                            or max(abs(entity["x"]-x), abs(entity["y"]-y)) <= 1 and entity["components"].get("heater", {}).get("lit")):
                        assert_boundary(ctx, [domain])


def guard_action(ctx, args, *, topology=False):
    ids = set()
    for key in ("target_id", "item_id", "entity_id", "other_id", "source_id"):
        if isinstance(args.get(key), str):
            ids.add(args[key])
    ids.update(args.get("material_ids", []))
    ids.update(value for value in args.get("port_bindings", {}).values() if isinstance(value, str))
    rows, points = {}, []
    for eid in ids:
        entity = m.get_entity(ctx, eid, required=False)
        if entity:
            points.append(m.entity_location(ctx, entity))
            tree = m._tree_objects(ctx, entity)
        else:
            tree = {eid: None}
        from .execution import subscribers
        related = set(tree)
        for source in tree:
            related.update(subscribers(ctx, source))
        for source in related:
            domain = for_entity(ctx, source)
            if domain:
                rows[domain["id"]] = domain
    assert_boundary(ctx, rows.values(), topology=topology)
    guard_tiles(ctx, points)
    if len(rows) == 1:
        ctx._grove_domain = next(iter(rows))
    return list(rows.values())


def mark_failure(ctx, domain, target_tick, code):
    domain["last_tick"] = target_tick
    domain["status"] = "pressure" if code == "CapacityPressure" else "fault"
    domain["failure"] = {"tick": target_tick, "code": code, "computed": False}
    refresh_ready(ctx, domain)


def recover_checkpoint_version(ctx, domain, checkpoint):
    """Resume only a scheduler-fenced, demonstrably uncomputed old task.

    The failed logical step remains a saved fact. A closed frame cannot be
    recomputed against today's inputs, so recovery joins the next legal frame
    through the ordinary readiness index, without advancing physical state.
    """
    failure = domain.get("failure", {})
    arguments = checkpoint.get("arguments", {})
    tick = failure.get("tick")
    if (domain.get("status") != "fault" or failure.get("computed") is not False
            or failure.get("code") != "CheckpointVersionMismatch"
            or type(tick) is not int or domain.get("last_tick") != tick
            or domain.get("last_applied_tick", tick) >= tick
            or checkpoint.get("status") != "blocked"
            or checkpoint.get("last_error_code") != "CheckpointVersionMismatch"
            or checkpoint.get("handler") != "grove.domain"
            or arguments.get("domain_id") != domain["id"]
            or arguments.get("target_tick") != tick
            or arguments.get("topology_revision") != domain["topology_revision"]
            or arguments.get("phase") != "advance"):
        return False
    restored = deepcopy(domain)
    restored["status"] = "sleeping"
    restored["recovered_failure"] = restored.pop("failure")
    if sm.encoded_size(restored) > MAX_DOMAIN_BYTES:
        return False
    refresh_ready(ctx, restored)
    return True


def observation(ctx, entity_id):
    domain = for_entity(ctx, entity_id)
    if domain is None:
        return {"state": "sleeping", "last_applied_tick": None, "pending_target_tick": None}
    work = frame(ctx)
    pending = pending_in_frame(ctx, domain)
    state = "calculating" if pending else {"fault": "error", "ready": "settled"}.get(domain["status"], domain["status"])
    result = {"domain_id": domain["id"], "state": state,
            "last_applied_tick": domain.get("last_applied_tick", domain["last_tick"]),
            "pending_target_tick": work["target_tick"] if pending else None,
            "failure": deepcopy(domain.get("failure")),
            "counts": {"objects": len(domain["objects"]), "rules": len(domain["creative_instances"]),
                       "processes": len(domain["active_processes"]), "chunks": len(domain["chunks"])}}
    if "recovered_failure" in domain:
        result["recovered_failure"] = deepcopy(domain["recovered_failure"])
    return result


def migrate(ctx):
    """Consume the old bounded active indexes once, without scanning objects."""
    if installed(ctx):
        return
    legacy = {key: ctx.get_state(m.META, key, []) for key in ACTIVITY_KEYS}
    from .activity import get_process
    ctx._grove_migrating_processes = {pid: process for pid in legacy["active_processes"]
                                    if (process := get_process(ctx, pid)) and process["status"] == "active"}
    _set(ctx, CONTROL, "last_ordinal", 0)
    seeds = set(legacy["creative_instances"]) | set(legacy["physical_active"]) | set(legacy["active_hearths"])
    # Existing processes are at most12; their real receiver sets are bounded.
    process_graphs = [closure(ctx, process_ids=[pid]) for pid in legacy["active_processes"]]
    graphs = []
    for graph in process_graphs:
        seeds.update(graph["objects"])
    while seeds:
        seed = min(seeds)
        seeds.remove(seed)
        graph = closure(ctx, [seed])
        seeds.difference_update(graph["objects"])
        if graph["objects"]:
            graphs.append(graph)
    represented = {pid for graph in graphs for pid in graph["processes"]}
    graphs.extend(graph for graph in process_graphs if set(graph["processes"]) - represented and not graph["objects"])
    represented_spills = {key for graph in graphs for key in graph["spill_points"]}
    for cid in legacy["spill_chunks"]:
        _, cx, cy = cid.split("_")
        for key, amount in m.get_chunk(ctx, int(cx), int(cy)).get("water_reservoirs", {}).items():
            if amount and key not in represented_spills:
                graph = closure(ctx, spill_points=[key])
                represented_spills.update(graph["spill_points"])
                graphs.append(graph)
    for graph in graphs:
        domain = _persist_component(ctx, graph)
        for key in ACTIVITY_KEYS:
            if key in {"spill_points", "spill_chunks"}:
                continue
            domain[key] = sorted(set(legacy[key]) & (set(graph["processes"]) if key == "active_processes" else set(graph["chunks"]) if key == "spill_chunks" else set(graph["objects"])))
        refresh_ready(ctx, domain)
    _set(ctx, CONTROL, "index_version", INDEX_VERSION)
    del ctx._grove_migrating_processes
    # The old lists are no longer an authoritative union. Keep their empty
    # identities so no saved entity, definition, receipt or legacy row is lost.
    for key in ACTIVITY_KEYS:
        ctx.set_state(m.META, key, [])


def state_specs():
    return tuple(StateRule(name, "", {}, history="metadata") for name in (DOMAINS, OWNERS, REGIONS, READY, DIRTY, CONTROL))
