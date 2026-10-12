"""Stored membership and bounded, local wakeups for authored instances.

Sleeping changes scheduling only: definitions, instances and their permissions
stay intact. This index never executes a declaration or advances a clock.
"""
from __future__ import annotations

from copy import deepcopy
from contextlib import nullcontext
import hashlib
import json

from agent_world import StateRule
from agent_world.errors import RuleViolation
from agent_world.simulation import matter as sm
from . import activity, model as m
from .definitions import instance_definition, execution_type_plan

MEMBERS = "grove.execution.members"
SOURCES = "grove.execution.sources"
CHUNKS = "grove.execution.chunks"
CONTROL = "grove.execution.control"
MAX_FRONTIER = 32
MAX_SUBSCRIBERS = 32
MAX_SOURCES = 96
MAX_MEMBER_BYTES = MAX_INDEX_BYTES = 8192
MAX_INDEX_WRITES = 32
MAX_MEMBER_WRITES = 32
MAX_QUEUE_WRITES = 32
INDEX_VERSION = 1
_DELETED = None


def _record(ctx, scope, key):
    """Internal read-only row; value getters copy and writers replace the row."""
    batch = getattr(ctx, "_grove_batch", None)
    token = (scope, key)
    if batch is not None:
        if token not in batch.execution_rows:
            batch.execution_rows[token] = deepcopy(ctx.get_state_record(scope, key))
        return batch.execution_rows[token]
    return ctx.get_state_record(scope, key)


def _get(ctx, scope, key, default=None):
    row = _record(ctx, scope, key)
    return deepcopy(row["value"] if row["exists"] else default)


def _set(ctx, scope, key, value):
    """Share one final CAS per index/queue row with the candidate batch owner."""
    previous = _record(ctx, scope, key)
    if previous["exists"] and previous["value"] == value:
        return
    batch = getattr(ctx, "_grove_batch", None)
    if batch is None:
        ctx.set_state(scope, key, value, expected_version=previous["version"])
    else:
        batch.execution_rows[scope, key] = {**previous, "exists": True, "value": deepcopy(value)}
        batch.dirty_execution.add((scope, key))


def _delete(ctx, scope, key):
    previous = _record(ctx, scope, key)
    if not previous["exists"]:
        return
    batch = getattr(ctx, "_grove_batch", None)
    if batch is None:
        ctx.delete_state(scope, key, expected_version=previous["version"])
    else:
        batch.execution_rows[scope, key] = {**previous, "exists": False, "value": _DELETED}
        batch.dirty_execution.add((scope, key))


def flush(ctx, batch):
    """Called only by commit's existing owner, still inside Runtime's transaction."""
    from . import events
    counts = {scope: sum(token[0] == scope for token in batch.dirty_execution)
              for scope in (MEMBERS, SOURCES, CHUNKS, events.PENDING, events.FACTS, CONTROL)}
    migrating = (getattr(ctx, "_grove_execution_migrating", False)
                 or getattr(ctx, "_grove_execution_migration_batch", None) is batch)
    index_limit = MAX_SOURCES * MAX_FRONTIER + 4 * MAX_FRONTIER if migrating else MAX_INDEX_WRITES
    if (counts[MEMBERS] > MAX_MEMBER_WRITES or counts[SOURCES] + counts[CHUNKS] > index_limit
            or counts[events.PENDING] > MAX_QUEUE_WRITES
            or counts[events.FACTS] > (events.MAX_EVENTS if migrating else events.MAX_FACT_WRITES)
            or counts[CONTROL] > 1):
        from .domains import CapacityPressure
        raise CapacityPressure("本步执行依赖索引写入超过预留预算；本次变化没有提交。")
    for scope, key in sorted(batch.dirty_execution):
        row = batch.execution_rows[scope, key]
        if row["exists"]:
            ctx.set_state(scope, key, row["value"], expected_version=row["version"])
        else:
            ctx.delete_state(scope, key, expected_version=row["version"])
    if getattr(ctx, "_grove_execution_migration_batch", None) is batch:
        del ctx._grove_execution_migration_batch


def get_member(ctx, entity_id):
    return _get(ctx, MEMBERS, entity_id)


def subscribers(ctx, source_id):
    return _get(ctx, SOURCES, source_id, [])


def local_subscribers(ctx, x, y):
    return _get(ctx, CHUNKS, m.chunk_id(*m.chunk_for(x, y)), [])


def _replace_index(ctx, scope, entity_id, before, after):
    for key in sorted(set(before) | set(after)):
        previous = _get(ctx, scope, key, [])
        proposed = sorted((set(previous) - {entity_id}) | ({entity_id} if key in after else set()))
        if len(proposed) > MAX_SUBSCRIBERS or sm.encoded_size(proposed) > MAX_INDEX_BYTES:
            from .domains import CapacityPressure
            raise CapacityPressure("同一来源或局部事件区域的依赖超过接纳预算；创建或绑定未提交。")
        if proposed:
            _set(ctx, scope, key, proposed)
        else:
            _delete(ctx, scope, key)


def _ancestry(ctx, entity_id):
    result, seen = [], set()
    current = entity_id
    for _ in range(sm.MAX_TREE_DEPTH + 1):
        if current in seen:
            raise RuleViolation("执行依赖的物件祖先不能循环。")
        seen.add(current)
        result.append(current)
        entity = m.get_entity(ctx, current, required=False)
        parent = entity.get("physical_parent") if entity else None
        if not parent:
            return result
        current = parent["id"]
    raise RuleViolation("执行依赖的物件祖先超过已声明界限。")


def _member_for(ctx, entity, previous=None, *, fingerprint=True):
    pack, spec = instance_definition(ctx, entity)
    if spec is None:
        return None
    binding = entity["state"]["program"]
    # Only immutable definition analysis is shared within this transaction.
    # Sources, visibility, fingerprints and capacity still read current facts.
    plan = execution_type_plan(ctx, binding["definition_ref"], binding["type_id"])
    sources = set(_ancestry(ctx, entity["id"]))
    for eid in set(plan["literal_objects"]) | set(binding.get("port_bindings", {}).values()):
        sources.update(_ancestry(ctx, eid))
    for link in binding.get("connections", []):
        sources.update(_ancestry(ctx, link["other_id"]))
    if len(sources) > MAX_SOURCES:
        from .domains import CapacityPressure
        raise CapacityPressure("实例来源及可见性祖先依赖超过接纳预算。")
    location = m.public_location(ctx, entity)
    chunks = []
    if (plan["event_driven"] or plan["recorder"]) and location is not None:
        radius = spec.get("recorder", {}).get("radius", 1)
        chunks = m.chunk_ids_around(*location, radius=radius)
    previous = previous or {}
    value = {"entity_id": entity["id"], "definition_ref": binding["definition_ref"],
             "type_id": binding["type_id"], "sources": sorted(sources), "chunks": sorted(chunks),
             "event_driven": plan["event_driven"], "recorder": plan["recorder"],
             "continuous": plan["continuous"], "sleep_safe": plan["sleep_safe"],
             "status": previous.get("status", "active"),
             "input_fingerprint": _input_fingerprint(ctx, sources) if fingerprint else previous.get("input_fingerprint", ""),
             "wake_revision": previous.get("wake_revision", 0),
             "registered_tick": previous.get("registered_tick", activity.meta_get(ctx, "clock", {}).get("sequence", 0))}
    if sm.encoded_size(value) > MAX_MEMBER_BYTES:
        from .domains import CapacityPressure
        raise CapacityPressure("实例依赖记录超过8KiB接纳预算。")
    return value


def _input_fingerprint(ctx, sources):
    """Observe actual inputs, excluding bookkeeping produced by the runner.

    Keep the digest on the consumer, not one extra record per physical source.
    Visibility ancestors and ordinary matter are included; a process/cursor
    update alone must not turn a sleeping declaration into a polling loop.
    """
    # Only reconcile_many owns this phase cache. It is discarded before any
    # candidate can change, and is never keyed by a persisted version.
    facts = getattr(ctx, "_grove_input_facts", None)
    inputs = []
    for eid in sorted(sources):
        encoded_source = facts.get(eid) if facts is not None else None
        if encoded_source is None:
            source = m.get_entity(ctx, eid, required=False)
            if source is not None:
                source.pop("version", None)
                source.pop("interaction_revision", None)
                program = source.get("state", {}).get("program", {})
                program.pop("process", None)
                program.pop("event_cursor", None)
                program.get("recorder", {}).pop("recorded_count", None)
            encoded_source = json.dumps(source, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            if facts is not None:
                facts[eid] = encoded_source
        inputs.append(json.dumps(eid, ensure_ascii=False)+":"+encoded_source)
    encoded = ("{"+",".join(inputs)+"}").encode()
    return hashlib.sha256(encoded).hexdigest()


def _index_member(ctx, entity, previous=None):
    previous = previous if previous is not None else get_member(ctx, entity["id"])
    member = _member_for(ctx, entity, previous)
    if member is None:
        return None
    _replace_index(ctx, SOURCES, entity["id"], (previous or {}).get("sources", []), member["sources"])
    _replace_index(ctx, CHUNKS, entity["id"], (previous or {}).get("chunks", []), member["chunks"])
    _set(ctx, MEMBERS, entity["id"], member)
    return member


def _closure(ctx, seeds):
    found, pending = set(), list(seeds)
    while pending:
        source = pending.pop()
        for eid in subscribers(ctx, source):
            if eid in found:
                continue
            found.add(eid)
            if len(found) > MAX_FRONTIER:
                from .domains import CapacityPressure
                raise CapacityPressure("来源变化的完整依赖唤醒超过32个局部执行参与者；创建或绑定未提交。")
            pending.append(eid)
        if get_member(ctx, source) is not None and source not in found:
            found.add(source)
            if len(found) > MAX_FRONTIER:
                from .domains import CapacityPressure
                raise CapacityPressure("完整依赖唤醒超过32个局部执行参与者。")
    return found


def _admit_dependency_graph(ctx, member):
    # New edges can increase an upstream source's transitive fan-out. Follow
    # only explicit stored predecessors, never scan the instance directory.
    ancestors, pending = set(), [member["entity_id"]]
    while pending:
        eid = pending.pop()
        if eid in ancestors:
            continue
        ancestors.add(eid)
        if len(ancestors) > MAX_SOURCES * MAX_FRONTIER:
            from .domains import CapacityPressure
            raise CapacityPressure("依赖关系接纳工作超过有界来源预算。")
        _closure(ctx, [eid])
        row = get_member(ctx, eid)
        if row:
            pending.extend(source for source in row["sources"] if source != eid)


def potential_members(ctx, entity_ids, *, source_ids=()):
    """Read the same bounded wake closure without changing scheduling state."""
    possible = set(entity_ids)
    if source_ids:
        possible.update(_closure(ctx, source_ids))
    members = []
    for eid in sorted(possible):
        entity = m.get_entity(ctx, eid, required=False)
        if (entity is not None and entity["lifecycle"] == "intact"
                and not entity["state"].get("retired") and get_member(ctx, eid) is not None):
            members.append(eid)
    if len(members) > MAX_FRONTIER:
        from .domains import CapacityPressure
        raise CapacityPressure("本步可能唤醒的局部完整成员并集超过32个；本步未推进或消费事件。")
    return members


def wake(ctx, entity_ids, *, reason=None):
    if isinstance(entity_ids, str):
        entity_ids = [entity_ids]
    from . import domains
    direct_event = reason == "public_event"
    if domains.installed(ctx):
        awakened = set(entity_ids) if direct_event else set().union(*(_closure(ctx, [eid]) for eid in entity_ids))
        groups = {}
        for eid in sorted(awakened):
            domain = domains.for_entity(ctx, eid) or domains.ensure(ctx, entity_ids=[eid])
            if domain:
                groups.setdefault(domain["id"], set()).add(eid)
        for did, ids in groups.items():
            with domains.scope(ctx, did):
                _wake_members(ctx, ids, check_capacity=not getattr(ctx, "_grove_domain_work", False))
        return awakened
    awakened = set(entity_ids) if direct_event else _closure(ctx, entity_ids)
    _wake_members(ctx, awakened)
    return awakened


def _wake_members(ctx, awakened, *, check_capacity=True):
    current = set(activity.meta_get(ctx, "creative_instances", []))
    candidates = []
    for eid in sorted(current | awakened):
        entity = m.get_entity(ctx, eid, required=False)
        if entity and entity["lifecycle"] == "intact" and not entity["state"].get("retired") and get_member(ctx, eid) is not None:
            candidates.append(eid)
    if len(candidates) > MAX_FRONTIER:
        from .domains import CapacityPressure
        raise CapacityPressure("本次变化所需活动程序超过32个局部执行参与者；本次变化未提交。")
    activity.meta_set(ctx, "creative_instances", candidates)
    if check_capacity and set(candidates) - current:
        from .creative import _check_instance_capacity
        _check_instance_capacity(ctx, candidates)
    for eid in sorted(awakened):
        member = get_member(ctx, eid)
        if member is None or eid not in candidates:
            continue
        member["wake_revision"] += 1
        member["status"] = "active"
        _set(ctx, MEMBERS, eid, member)


def register(ctx, entity):
    if not entity["state"].get("program", {}).get("definition_ref"):
        return
    member = _index_member(ctx, entity)
    _admit_dependency_graph(ctx, member)
    from . import domains
    if domains.installed(ctx):
        domains.ensure(ctx, entity_ids=[entity["id"]])
    wake(ctx, [entity["id"]], reason="registration")


def reconcile(ctx, entity, *, previous=None):
    """Called after ordinary state/ownership changes, including non-program sources."""
    return reconcile_many(ctx, {entity["id"]: entity})


def reconcile_many(ctx, entities):
    """Index the final source union once, then wake each real domain once."""
    previous = getattr(ctx, "_grove_input_facts", None)
    ctx._grove_input_facts = {}
    try:
        return _reconcile_many(ctx, entities)
    finally:
        ctx._grove_input_facts = previous


def _reconcile_many(ctx, entities):
    affected = set()
    for eid, entity in entities.items():
        affected.update(subscribers(ctx, eid))
        if get_member(ctx, eid) is not None or entity["state"].get("program", {}).get("definition_ref"):
            affected.add(eid)
    changed = []
    for eid in sorted(affected):
        current = entities.get(eid) or m.get_entity(ctx, eid, required=False)
        if current is None:
            continue
        before = get_member(ctx, eid)
        if before is not None and (current["lifecycle"] != "intact" or current["state"].get("retired")):
            _retire(ctx, eid, before)
            continue
        indexed = _index_member(ctx, current, before)
        if indexed is not None:
            if before is None or indexed["sources"] != before["sources"]:
                _admit_dependency_graph(ctx, indexed)
            if before is None or indexed["input_fingerprint"] != before.get("input_fingerprint"):
                changed.append(eid)
    if changed:
        wake(ctx, changed, reason="world_change")


def _retire(ctx, entity_id, member):
    _replace_index(ctx, SOURCES, entity_id, member["sources"], [])
    _replace_index(ctx, CHUNKS, entity_id, member["chunks"], [])
    member.update(status="retired", sources=[], chunks=[])
    _set(ctx, MEMBERS, entity_id, member)
    from . import domains
    domain = domains.for_entity(ctx, entity_id) if domains.installed(ctx) else None
    with domains.scope(ctx, domain) if domain else nullcontext():
        frontier = set(activity.meta_get(ctx, "creative_instances", []))
        frontier.discard(entity_id)
        activity.meta_set(ctx, "creative_instances", sorted(frontier))


def begin_step(ctx, entity_ids):
    ctx._grove_execution_start = {eid: (get_member(ctx, eid) or {}).get("wake_revision", 0) for eid in entity_ids}


def after_step(ctx, processed_ids):
    from . import events
    start = getattr(ctx, "_grove_execution_start", {})
    frontier = set(activity.meta_get(ctx, "creative_instances", []))
    physical = set(activity.meta_get(ctx, "physical_active", []))
    for eid in processed_ids:
        member = get_member(ctx, eid)
        entity = m.get_entity(ctx, eid, required=False)
        if member is None:
            frontier.discard(eid)
            continue
        retired = entity is None or entity["lifecycle"] != "intact" or entity["state"].get("retired")
        if retired:
            frontier.discard(eid)
            _retire(ctx, eid, member)
            continue
        program = entity["state"]["program"]
        events.consume(ctx, eid, program.get("event_cursor", 0))
        queue = events.consumer_status(ctx, eid)
        error = program["process"]["status"] == "error"
        changed_during_step = eid not in start or member["wake_revision"] != start[eid]
        visible_events = m.public_location(ctx, entity) is not None
        mechanical = program.get("mechanical", {})
        # An after-rule failure preserves this step's solved motion. One next
        # step must publish its stopped state before the declaration can sleep.
        # Contracts and recorders likewise retain only their finite deadlines.
        error_settlement = error and (
            bool(mechanical) and (mechanical.get("status") != "program_error"
                or mechanical.get("speed", 0) != 0 or mechanical.get("available_torque", 0) != 0
                or mechanical.get("enabled", False))
            or program.get("contract", {}).get("status") == "active"
            or program.get("recorder", {}).get("active", False))
        keep = (eid in physical or error_settlement or not error and
                (not member["sleep_safe"] or member["continuous"]
                 or visible_events and not queue["paused"] and queue["retained_count"]
                 or changed_during_step))
        member["status"] = "error" if error else "active" if keep else "sleeping"
        if not keep:
            frontier.discard(eid)
        _set(ctx, MEMBERS, eid, member)
    activity.meta_set(ctx, "creative_instances", sorted(frontier))


def migrate(ctx):
    """Upgrade only the known old bounded instance index; never scan history."""
    if _get(ctx, CONTROL, "index_version") == INDEX_VERSION:
        return
    from .commit import candidate_batch
    current = getattr(ctx, "_grove_batch", None)
    ctx._grove_execution_migrating = True
    try:
        with (candidate_batch(ctx) if current is None else nullcontext(current)):
            _migrate(ctx)
    finally:
        # An enclosing batch is flushed by its owner after this call. Its row
        # count, not this temporary flag, records the one-time migration plan.
        if current is not None:
            ctx._grove_execution_migration_batch = current
        del ctx._grove_execution_migrating


def _migrate(ctx):
    ids = activity.meta_get(ctx, "creative_instances", [])
    if not isinstance(ids, list) or len(ids) > MAX_FRONTIER or len(set(ids)) != len(ids):
        raise RuleViolation("旧执行参与目录无效；原物件、定义和回执保留，迁移未提交。")
    for eid in ids:
        entity = m.get_entity(ctx, eid, required=False)
        if entity is None:
            raise RuleViolation("旧执行目录引用缺少物件；迁移未提交。")
        member = _index_member(ctx, entity)
        if member is None:
            raise RuleViolation("旧执行目录引用缺少已发表定义；迁移未提交。")
    for eid in ids:
        _admit_dependency_graph(ctx, get_member(ctx, eid))
    from . import events
    events.migrate(ctx, ids)
    _set(ctx, CONTROL, "index_version", INDEX_VERSION)


def state_specs():
    from . import events
    return (*tuple(StateRule(scope, "", {}, history="metadata") for scope in (MEMBERS, SOURCES, CHUNKS, CONTROL)),
            *events.state_specs())


def reservation(ctx, entity_ids, *, max_facts=None, consumer_ids=None):
    """Reserve bounded metadata separately from matter and outcome records.

    A stable index is not rewritten just because it was read. Rows already
    planned by this command are counted once; active membership and queues
    reserve their next-step final state and referenced event-body edits.
    """
    from . import events
    if max_facts is None:
        from .generation import MAX_OUTCOMES_PER_STEP
        max_facts = MAX_OUTCOMES_PER_STEP
    max_facts = min(max_facts, events.MAX_FACT_WRITES)
    rows = {}
    batch = getattr(ctx, "_grove_batch", None)
    if batch is not None:
        rows.update({token: batch.execution_rows[token] for token in batch.dirty_execution})
    consumers, indexes, retained_facts, possible_events = {}, set(), set(), 0
    event_seeds, entity_writes, record_writes = set(), set(), set()
    for eid in entity_ids:
        member = get_member(ctx, eid)
        if member is None:
            continue
        rows.setdefault((MEMBERS, eid), _record(ctx, MEMBERS, eid))
        indexes.update((SOURCES, source) for source in member["sources"])
        indexes.update((CHUNKS, chunk) for chunk in member["chunks"])
    # Public facts also reach sleeping/error subscribers outside the frontier.
    # Their exact local IDs are supplied by the caller's physical step domain.
    for eid in consumer_ids if consumer_ids is not None else entity_ids:
        member = get_member(ctx, eid)
        entity = m.get_entity(ctx, eid, required=False)
        receives = (max_facts > 0 and entity is not None and entity["lifecycle"] == "intact"
                    and not entity["state"].get("retired") and m.public_location(ctx, entity) is not None)
        if receives and member is not None and member.get("recorder"):
            recorder = entity["state"]["program"].get("recorder", {})
            if recorder.get("active") and recorder.get("until_tick", -1) >= activity.meta_get(ctx, "clock", {}).get("sequence", 0):
                entity_writes.add(eid)
                if eid not in entity_ids:
                    record_writes.add(eid)
        if member is not None and member["event_driven"]:
            queue = events._queue(ctx, eid)
            if not queue["paused"]:
                # Pending bodies are shared. A paused queue admits no new refs
                # and preserves its original prefix without polling or GC.
                consumers[eid] = queue
                retained_facts.update(str(sequence) for sequence in queue["sequences"])
                room = (events.MAX_EVENTS-len(queue["sequences"])
                        if queue["payload_bytes"] < events.MAX_PENDING_BYTES else 0)
                # Distinct local consumers may accept different new facts.
                # Reserve their union, bounded by the step's total producers.
                possible_events = min(max_facts, possible_events + min(max_facts, room))
                rows.setdefault((events.PENDING, eid), _record(ctx, events.PENDING, eid))
                if receives:
                    # Any attempted enqueue can become an explicit overflow
                    # error, including when this consumer was sleeping/error.
                    entity_writes.add(eid)
                    if room > 0 and entity["state"]["program"]["process"]["status"] != "error":
                        event_seeds.add(eid)
    if len(consumers) > MAX_QUEUE_WRITES:
        from .domains import CapacityPressure
        raise CapacityPressure("本步局部事件消费者超过32个队列写入预算；未消费或丢弃事实。")
    # Immutable event bodies are next-frame input. Budget the direct receiver
    # writes here, not their future source graph or a foreign physics forest.
    possible_members = sorted(set(entity_ids) | event_seeds)
    for eid in possible_members:
        member = get_member(ctx, eid)
        rows.setdefault((MEMBERS, eid), _record(ctx, MEMBERS, eid))
        indexes.update((SOURCES, source) for source in member["sources"])
        indexes.update((CHUNKS, chunk) for chunk in member["chunks"])
    if sum(scope == MEMBERS for scope, _ in rows) > MAX_MEMBER_WRITES:
        from .domains import CapacityPressure
        raise CapacityPressure("本步成员写入并集超过32个预算；本步未推进或消费事件。")
    for eid in record_writes:
        rows.setdefault((events.RECORDS, eid), _record(ctx, events.RECORDS, eid))
    if sum(scope == events.PENDING for scope, _ in rows) > MAX_QUEUE_WRITES:
        from .domains import CapacityPressure
        raise CapacityPressure("本步局部事件队列写入并集超过32个预算；未消费或丢弃事实。")
    changes, size = len(rows), 0
    for (scope, key), row in rows.items():
        previous = ctx.get_state_record(scope, key)
        before = sm.encoded_size(previous["value"] if previous["exists"] else None)
        after = sm.encoded_size(row["value"] if row["exists"] else None)
        if scope == MEMBERS:
            after += 96  # status/revision growth, not a new arbitrary dependency
        elif scope == events.PENDING:
            queue = consumers.get(key)
            room = events.MAX_EVENTS-len(queue["sequences"]) if queue else 0
            after = min(events.MAX_QUEUE_BYTES, after + min(max_facts, room) * 21 + 256)
        elif scope == events.RECORDS:
            after = max(after, events.MAX_RECORD_BYTES)
        size += before + after + 256
    # Reparenting/visibility and drift may change a finite set of subscriptions.
    # Actual planned index rows above already occupy these reserved positions.
    planned_indexes = sum(scope in (SOURCES, CHUNKS) for scope, _ in rows)
    known_indexes = indexes | {token for token in rows if token[0] in (SOURCES, CHUNKS)}
    # A drift/visibility change replaces an old local chunk with a new one.
    # Explicit new bindings are already present in this command's dirty rows.
    index_budget = min(MAX_INDEX_WRITES, len(known_indexes)
                       + sum(scope == CHUNKS for scope, _ in indexes))
    missing_indexes = max(0, index_budget - planned_indexes)
    changes += missing_indexes
    # Existing subscriptions are already bounded and creation/binding stages
    # its actual changed rows before admission. Tick movement can only add
    # this frontier's actual IDs to a local destination; reserve that actual
    # fan-out instead of32 fictitious128-character IDs for every source key.
    frontier_ids = set(possible_members)
    index_sizes = []
    for scope, key in known_indexes:
        if (scope, key) in rows:
            continue
        current = _get(ctx, scope, key, [])
        proposed = sorted(set(current) | frontier_ids) if scope == CHUNKS else current
        index_sizes.append(sm.encoded_size(current) + sm.encoded_size(proposed) + 256)
    destination_chunks = sum(scope == CHUNKS for scope, _ in indexes)
    index_sizes.extend([sm.encoded_size(sorted(frontier_ids)) + 258] * destination_chunks)
    size += sum(sorted(index_sizes, reverse=True)[:missing_indexes])
    if consumers:
        planned_facts = {key for scope, key in rows if scope == events.FACTS}
        fact_sizes = []
        for sequence in retained_facts:
            record = _record(ctx, events.FACTS, sequence)
            value = record["value"] if record["exists"] else None
            if not isinstance(value, dict) or value.get("references", 0) <= 0 or "event" not in value:
                raise RuleViolation("已排队公开事件的原事实缺失；未推进消费位置。")
            if sequence in planned_facts:
                continue
            # An unplanned cached row is the persisted original: no staged
            # mutation exists, and all final FACT writes are in planned_facts.
            before = sm.encoded_size(value)
            # The original body is immutable. Only its bounded reference count
            # can change; reserving a second fictitious512-byte body would make
            # a legal unread queue stop an otherwise small world step.
            after = sm.encoded_size({**value, "references": value["references"] + MAX_SUBSCRIBERS})
            fact_sizes.append(before + max(after, sm.encoded_size(None)) + 256)
        # A new public sequence has no previous body. Its final shared record
        # contains the bounded event and a small ref-count wrapper, only once.
        fact_sizes.extend([sm.encoded_size(None) + events.MAX_EVENT_BYTES + 64 + 256] * possible_events)
        missing_facts = min(max(0, events.MAX_FACT_WRITES - len(planned_facts)), len(fact_sizes))
        changes += missing_facts
        size += sum(sorted(fact_sizes, reverse=True)[:missing_facts])
    return {"changes": changes, "size": size,
            "active_members": len({eid for eid in entity_ids if get_member(ctx, eid) is not None}),
            "event_consumers": len(consumers), "index_rows": index_budget,
            "possible_members": possible_members, "entity_writes": sorted(entity_writes)}
