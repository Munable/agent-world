"""Bounded public fact journal and observer histories.

Read stored public facts, retain bounded event/record rows, and return observer
metadata candidates to the shared commit owner. This module never executes
program rules, changes matter or calls the process/action runners.
"""
from __future__ import annotations
from copy import deepcopy
from agent_world.errors import RuleViolation
from agent_world.simulation import matter as sm
from agent_world.simulation.declarations import has_event
from . import model as m, execution
from .definitions import instance_definition

RECORDS = "grove.creative.records"
MAX_RECORDS = 32
MAX_EVENTS = 256
MAX_RECORD_BYTES = 1536
MAX_EVENT_BYTES = 512
MAX_EVENT_JOURNAL_BYTES = 65536
PENDING = "grove.execution.pending"
FACTS = "grove.execution.event_facts"
MAX_PENDING_BYTES = MAX_EVENT_JOURNAL_BYTES
MAX_QUEUE_BYTES = 8192
MAX_FACT_WRITES = 164


def _save(ctx, scope, key, value):
    rec = ctx.get_state_record(scope, key)
    return ctx.set_state(scope, key, value, expected_version=rec["version"])


def bounded_record(value):
    """Retain recent public facts and reads within one reserved record size."""
    record = {"history": deepcopy(value.get("history", [])[-MAX_RECORDS:]),
              "reads": deepcopy(value.get("reads", [])[-16:])}
    while sm.encoded_size(record) > MAX_RECORD_BYTES:
        history, reads = record["history"], record["reads"]
        history_age = (history[0]["recorded_tick"], history[0]["sequence"], 0) if history else None
        read_age = (reads[0]["read_tick"], reads[0]["sequence"], 1) if reads else None
        if history and (not reads or history_age <= read_age):
            history.pop(0)
        else:
            reads.pop(0)
    return record


def _queue(ctx, entity_id):
    return execution._get(ctx, PENDING, entity_id, {
        "sequences": [], "payload_bytes": 0, "consumed_through": 0,
        "paused": False, "gap": None})


def consumer_status(ctx, entity_id):
    queue = _queue(ctx, entity_id)
    return {"pending_count": sum(sequence > queue["consumed_through"] for sequence in queue["sequences"]),
            "pending_bytes": queue["payload_bytes"], "paused": queue["paused"],
            "gap": deepcopy(queue["gap"]), "consumed_through": queue["consumed_through"],
            "retained_count": len(queue["sequences"])}


def _fact_rows(ctx):
    batch = getattr(ctx, "_grove_batch", None)
    if batch is not None:
        return {key for scope, key in batch.dirty_execution if scope == FACTS}
    return getattr(ctx, "_grove_event_fact_writes", set())


def _write_fact(ctx, sequence, value):
    key = str(sequence)
    touched = _fact_rows(ctx)
    if key not in touched and len(touched) >= MAX_FACT_WRITES:
        return False
    if value is None:
        execution._delete(ctx, FACTS, key)
    else:
        execution._set(ctx, FACTS, key, value)
    if getattr(ctx, "_grove_batch", None) is None:
        ctx._grove_event_fact_writes = touched | {key}
    return True


def _pause(ctx, entity, queue, sequence, reason):
    """Isolate one full consumer, retaining its exact pending prefix and cursor."""
    queue.update(paused=True, gap={"from_sequence": sequence, "reason": reason,
                                  "last_consumed_sequence": queue["consumed_through"]})
    execution._set(ctx, PENDING, entity["id"], queue)
    entity["state"]["program"]["process"].update(
        status="error", error="公开事件订阅已暂停：" + reason,
        event_gap=deepcopy(queue["gap"]))
    return entity


def _enqueue(ctx, entity, event, *, migrating=False):
    queue = _queue(ctx, entity["id"])
    sequence = event["sequence"]
    if queue["paused"] or sequence <= queue["consumed_through"] or sequence in queue["sequences"]:
        return None
    size = sm.encoded_size(event)
    proposed = {**queue, "sequences": [*queue["sequences"], sequence],
                "payload_bytes": queue["payload_bytes"] + size}
    if (len(proposed["sequences"]) > MAX_EVENTS or proposed["payload_bytes"] > MAX_PENDING_BYTES
            or sm.encoded_size(proposed) > MAX_QUEUE_BYTES):
        if migrating:
            raise RuleViolation("原未消费事件超过已声明队列界限；迁移未提交，原事实保留。")
        return _pause(ctx, entity, queue, sequence, "pending_capacity")
    record = execution._get(ctx, FACTS, str(sequence))
    if record is not None and record["event"] != event:
        raise RuleViolation("同一公开事件sequence不能绑定不同事实。")
    value = {"event": deepcopy(event), "references": (record or {}).get("references", 0) + 1}
    if not _write_fact(ctx, sequence, value):
        if migrating:
            # Migration retains the old bounded journal (up to256), and coalesces
            # each fact once; its owner admits that one-time plan independently.
            execution._set(ctx, FACTS, str(sequence), value)
        else:
            return _pause(ctx, entity, queue, sequence, "event_step_budget")
    execution._set(ctx, PENDING, entity["id"], proposed)
    return None


def pending_events(ctx, entity_id, since=0):
    """Exact queued causal inputs; unavailable storage is an error, never a gap guess."""
    entity = m.get_entity(ctx, entity_id, required=False)
    # A carried or closed-contained observer has no current public event view.
    # Its real unread prefix remains stored until visibility is restored.
    if entity is None or m.public_location(ctx, entity) is None:
        return []
    queue = _queue(ctx, entity_id)
    if queue["paused"]:
        return []
    result = []
    for sequence in queue["sequences"]:
        if sequence <= max(since, queue["consumed_through"]):
            continue
        fact = execution._get(ctx, FACTS, str(sequence))
        if fact is None or fact["references"] <= 0:
            raise RuleViolation("已排队公开事件的原事实缺失；未推进消费位置。")
        result.append(deepcopy(fact["event"]))
    return result


def consume(ctx, entity_id, through):
    """Ack only the actual saved cursor; bounded GC does not replay an acked prefix."""
    queue = _queue(ctx, entity_id)
    if queue["paused"]:
        return
    queue["consumed_through"] = max(queue["consumed_through"], through)
    kept, size = [], queue["payload_bytes"]
    for sequence in queue["sequences"]:
        if sequence > through:
            kept.append(sequence)
            continue
        record = execution._get(ctx, FACTS, str(sequence))
        if record is None or record["references"] <= 0:
            raise RuleViolation("待回收公开事件引用缺失；本步没有提交。")
        value = {**record, "references": record["references"] - 1}
        if not _write_fact(ctx, sequence, value if value["references"] else None):
            kept.append(sequence)
            continue
        size -= sm.encoded_size(record["event"])
    queue.update(sequences=kept, payload_bytes=size)
    if execution._get(ctx, PENDING, entity_id) is not None or kept:
        execution._set(ctx, PENDING, entity_id, queue)


def migrate(ctx, entity_ids):
    """Share original pending event bodies, preserving each original consumer cursor."""
    journal = ctx.get_state(m.META, "creative_events", {"sequence": 0, "events": []})
    for eid in entity_ids:
        entity = m.get_entity(ctx, eid, required=False)
        if entity is None:
            continue
        _, spec = instance_definition(ctx, entity)
        if spec is None or not has_event(spec):
            continue
        cursor = entity["state"]["program"].get("event_cursor", 0)
        queue = _queue(ctx, eid)
        queue["consumed_through"] = cursor
        execution._set(ctx, PENDING, eid, queue)
        location = m.public_location(ctx, entity)
        if location is None:
            continue
        radius = spec.get("recorder", {}).get("radius", 1)
        for event in journal["events"]:
            if event["sequence"] > cursor and max(abs(event["x"]-location[0]), abs(event["y"]-location[1])) <= radius:
                _enqueue(ctx, entity, event, migrating=True)


def state_specs():
    from agent_world import StateRule
    return tuple(StateRule(scope, "", {}, history="metadata") for scope in (PENDING, FACTS))


def record_event(ctx, *, action, entity_id=None, x=None, y=None, kind="action", actor=None):
    """Append only minimal server-observed public facts, never private arguments."""
    updates = {}
    if entity_id:
        entity = m.get_entity(ctx, entity_id, required=False)
        location = m.public_location(ctx, entity) if entity else None
        if location is None:
            return
        x, y = location
    if x is None or y is None:
        return
    batch = getattr(ctx, "_grove_batch", None)
    journal = batch.event_journal if batch is not None else None
    if journal is None:
        journal = ctx.get_state(m.META, "creative_events", {"sequence": 0, "events": []})
    sequence = journal["sequence"] + 1
    tick = ctx.get_state(m.META, "clock", {"sequence": 0})["sequence"]
    event = {"sequence": sequence, "tick": tick, "occurred_at": ctx.now, "kind": kind,
             "action": action or "", "entity_id": entity_id or "", "actor": actor or ctx.actor_role_id,
             "x": x, "y": y, "mechanical_position": 0, "passable": False}
    if entity_id and entity:
        binding = entity["state"].get("program", {})
        if "mechanical" in binding:
            event["mechanical_position"] = int(binding["mechanical"]["position"])
        if "barrier" in entity["components"]:
            event["passable"] = entity["lifecycle"] == "intact" and not entity["components"]["barrier"]["active"]
        if binding.pop("barrier_open_pending", False):
            binding["barrier_opened_sequence"] = sequence
            updates[entity["id"]] = entity
    if sm.encoded_size(event) > MAX_EVENT_BYTES:
        raise RuleViolation("公开事件超过单条事实容量；本次行动未提交。")
    # Recent source history is a bounded projection. Consumers retain their own
    # shared-fact refs, so one unknown input cannot hold this global window open.
    recent = [*journal["events"], event][-MAX_EVENTS:]
    journal = {"sequence": sequence, "events": recent,
               "consumed_through": journal.get("consumed_through", sequence - 1)}
    while sm.encoded_size(journal) > MAX_EVENT_JOURNAL_BYTES:
        journal["events"].pop(0)
    if batch is not None:
        batch.event_journal = journal
    else:
        _save(ctx, m.META, "creative_events", journal)
    instances, wakeups = [], []
    for eid in execution.local_subscribers(ctx, x, y):
        consumer = m.get_entity(ctx, eid, required=False)
        if not consumer or consumer["lifecycle"] != "intact":
            continue
        _, spec = instance_definition(ctx, consumer)
        location = m.public_location(ctx, consumer)
        if spec is None or location is None or _queue(ctx, eid)["paused"]:
            continue
        radius = spec.get("recorder", {}).get("radius", 1)
        if max(abs(x-location[0]), abs(y-location[1])) > radius:
            continue
        instances.append((consumer, spec))
        if has_event(spec):
            paused = _enqueue(ctx, consumer, event)
            if paused is not None:
                updates[eid] = paused
            elif consumer["state"]["program"]["process"]["status"] != "error":
                wakeups.append(eid)
    if wakeups:
        execution.wake(ctx, wakeups, reason="public_event")
    for current, spec in instances:
        eid = current["id"]
        recorder = updates.get(eid) or current
        state = recorder["state"]["program"].get("recorder")
        location = m.public_location(ctx, recorder)
        if not state or not state["active"] or location is None or tick > state["until_tick"]:
            continue
        if max(abs(x-location[0]), abs(y-location[1])) > spec["recorder"]["radius"]:
            continue
        record = batch.records.get(eid) if batch is not None else None
        if record is None:
            record = ctx.get_state(RECORDS, eid, {"history": [], "reads": []})
        record["history"] = [*record["history"], {**event, "recorded_tick": tick}][-MAX_RECORDS:]
        record = bounded_record(record)
        if batch is not None:
            batch.records[eid] = record
        else:
            _save(ctx, RECORDS, eid, record)
        state["recorded_count"] += 1
        updates[recorder["id"]] = recorder

    return list(updates.values())
