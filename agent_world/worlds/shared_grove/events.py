"""Bounded public fact journal and observer histories.

Read stored public facts, retain bounded event/record rows, and return observer
metadata candidates to the shared commit owner. This module never executes
program rules, changes matter or calls the process/action runners.
"""
from __future__ import annotations
from copy import deepcopy
from agent_world.errors import RuleViolation, StateConflict
from agent_world.simulation import matter as sm
from agent_world.simulation.declarations import has_event
from . import model as m, activity
from .definitions import instance_definition

RECORDS = "grove.creative.records"
MAX_RECORDS = 32
MAX_EVENTS = 256
MAX_RECORD_BYTES = 1536
MAX_EVENT_BYTES = 512
MAX_EVENT_JOURNAL_BYTES = 65536


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
    journal = getattr(ctx, "_grove_event_batch", None)
    if journal is None:
        journal = ctx.get_state(m.META, "creative_events", {"sequence": 0, "events": []})
    sequence = journal["sequence"] + 1
    event = {"sequence": sequence, "tick": ctx.get_state(m.META, "clock", {"sequence": 0})["sequence"], "occurred_at": ctx.now, "kind": kind,
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
    cursors = []
    for eid in activity.meta_get(ctx, "creative_instances", []):
        consumer = m.get_entity(ctx, eid, required=False)
        if not consumer or consumer["lifecycle"] != "intact" or consumer["state"]["program"]["process"]["status"] == "error":
            continue
        _, spec = instance_definition(ctx, consumer)
        if has_event(spec):
            cursors.append(consumer["state"]["program"].get("event_cursor", 0))
    consumed = min(cursors, default=journal["sequence"])
    pending = [old for old in journal["events"] if old["sequence"] > consumed]
    limit = MAX_EVENTS if kind == "process" else MAX_EVENTS - 164
    journal = {"sequence": sequence, "events": [*pending, event], "consumed_through": consumed}
    if len(pending) >= limit or sm.encoded_size(journal) > MAX_EVENT_JOURNAL_BYTES:
        raise StateConflict("本逻辑步公开事件处理预算已满，请待下一世界步读取结果再继续；本次行动未提交。")
    if hasattr(ctx, "_grove_event_batch"):
        ctx._grove_event_batch = journal
    else:
        _save(ctx, m.META, "creative_events", journal)
    for eid in activity.meta_get(ctx, "creative_instances", []):
        recorder = updates.get(eid) or m.get_entity(ctx, eid, required=False)
        if not recorder or recorder["lifecycle"] != "intact":
            continue
        pack, spec = instance_definition(ctx, recorder)
        state = recorder["state"]["program"].get("recorder")
        location = m.public_location(ctx, recorder)
        if not state or not state["active"] or location is None or ctx.get_state(m.META, "clock", {"sequence": 0})["sequence"] > state["until_tick"]:
            continue
        if max(abs(x-location[0]), abs(y-location[1])) > spec["recorder"]["radius"]:
            continue
        batch = getattr(ctx, "_grove_record_batch", None)
        record = batch.get(eid) if batch is not None else None
        if record is None:
            record = ctx.get_state(RECORDS, eid, {"history": [], "reads": []})
        record["history"] = [*record["history"], {**event, "recorded_tick": ctx.get_state(m.META, "clock", {"sequence": 0})["sequence"]}][-MAX_RECORDS:]
        record = bounded_record(record)
        if batch is not None:
            batch[eid] = record
        else:
            _save(ctx, RECORDS, eid, record)
        state["recorded_count"] += 1
        updates[recorder["id"]] = recorder

    return list(updates.values())
