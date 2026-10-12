"""One public logical frame, paged dispatch and independent atomic domains.

The coordinator never computes physics. Domain receipts close its bounded
current page; failures do not leave a frame waiting for a missing callback.
"""
from __future__ import annotations

from agent_world import FunctionOutcome
from agent_world.errors import CheckpointNotFound, RuleViolation
from . import domains, model as m

FRAME_KEY = "grove.frame"
DOMAIN_PREFIX = "grove.domain."
PAGE_SIZE = 8
CHECKPOINT_ARGS = {"type": "object", "properties": {}, "additionalProperties": False}
DOMAIN_ARGS = {"type": "object", "properties": {
    "domain_id": {"type": "string", "minLength": 1, "maxLength": 128},
    "topology_revision": {"type": "integer", "minimum": 1},
    "target_tick": {"type": "integer", "minimum": 1},
    "frame_id": {"type": "string", "minLength": 1, "maxLength": 128}},
    "required": ["domain_id", "topology_revision", "target_tick", "frame_id"], "additionalProperties": False}
DOMAIN_ARGS["properties"]["phase"] = {"enum": ["prepare", "advance"]}
DOMAIN_ARGS["required"].append("phase")


def _checkpoint(ctx, key):
    try:
        return ctx.get_checkpoint(key)
    except CheckpointNotFound:
        return None


def _schedule_frame(ctx, due_at):
    old = _checkpoint(ctx, FRAME_KEY)
    return ctx.schedule_checkpoint(FRAME_KEY, "grove.frame", {}, due_at=due_at,
                                   expected_generation=old["generation"] if old else 0)


def _wake_frame(ctx):
    """Coalesce child completions once an immediate coordinator is pending."""
    old = _checkpoint(ctx, FRAME_KEY)
    if (old is not None and old["status"] == "pending"
            and old["due_at"] <= ctx.now and old["available_at"] <= ctx.now):
        return old["generation"]
    return _schedule_frame(ctx, ctx.now)


def resume(ctx):
    """Resume saved progress; downtime never generates missed physical ticks."""
    domains.migrate(ctx)
    clock = ctx.get_state(m.META, "clock", {"sequence": 0})
    work = domains.frame(ctx)
    clock.setdefault("completed_sequence", clock["sequence"] if not work or work["status"] != "open" else work["target_tick"]-1)
    clock["last_tick_at"] = ctx.now
    ctx.set_state(m.META, "clock", clock)
    if work and work["status"] in {"preparing", "open"}:
        # Pause the cadence across downtime without rewriting the original
        # opening fact or replaying any already applied domain.
        work["cadence_anchor_at"] = ctx.now
        for entry in work["pending"]:
            domain = domains.get(ctx, entry["domain_id"])
            if (domain and domain["status"] != "retired"
                    and domain["last_tick"] < work["target_tick"]
                    and not (work["status"] == "preparing"
                             and domain.get("prepared_frame") == work["id"])):
                checkpoint = _checkpoint(ctx, DOMAIN_PREFIX+domain["id"])
                if (checkpoint is not None and checkpoint["status"] not in {"pending", "cancelled"}
                        and not _version_blocked(checkpoint)):
                    # A terminal receipt must reach the coordinator unchanged.
                    # Restarting before that observation cannot erase a real
                    # rejection/failure or retry its rolled-back rule handler.
                    continue
                if (checkpoint is not None and checkpoint["status"] == "pending"
                        and checkpoint.get("attempts", 0) > 0):
                    # Keep an actual retry's attempt limit and availability.
                    # If its revision changed, Runtime will first fence it;
                    # the coordinator can then reissue that exact old target.
                    continue
                # Installation also resumes when only the world revision
                # changes. Pending tasks still belong to the old revision;
                # reissue the same unfinished frame rather than wait for its
                # version fence to turn an uncomputed domain into a fault.
                entry["generation"] = _schedule_domain(ctx, domain, work)
        domains._set(ctx, domains.CONTROL, "frame", work)
    recovery = domains._get(ctx, domains.CONTROL, "checkpoint_recovery")
    if not recovery or recovery["world_revision"] != ctx.function_version:
        domains._set(ctx, domains.CONTROL, "checkpoint_recovery", {
            "world_revision": ctx.function_version, "cursor": None, "complete": False})
    _schedule_frame(ctx, ctx.now if work and work["status"] in {"preparing", "open"} else ctx.now+1)
    ctx.world_resumed = True


def _schedule_domain(ctx, domain, work):
    key = DOMAIN_PREFIX+domain["id"]
    old = _checkpoint(ctx, key)
    return ctx.schedule_checkpoint(key, "grove.domain", {
        "domain_id": domain["id"], "topology_revision": domain["topology_revision"],
        "target_tick": work["target_tick"], "frame_id": work["id"],
        "phase": "prepare" if work["status"] == "preparing" else "advance"}, due_at=ctx.now,
        expected_generation=old["generation"] if old else 0)


def _open(ctx):
    clock = ctx.get_state(m.META, "clock")
    target = clock["sequence"]+1
    work = {"id": f"{clock['epoch']}.{target}", "status": "preparing", "target_tick": target,
            "high_water": domains._get(ctx, domains.CONTROL, "last_ordinal", 0),
            "event_cutoff": ctx.get_state(m.META, "creative_events", {"sequence": 0})["sequence"],
            "cursor": None, "finished_listing": False, "pending": [],
            "settled": 0, "prepared": 0, "pressure": 0, "faults": 0, "opened_at": ctx.now,
            "cadence_anchor_at": ctx.now}
    # sequence denotes the public target; each object exposes its own applied
    # sequence. Publishing the target never claims its domains already settled.
    clock.update(completed_sequence=clock.get("completed_sequence", target-1),
                 frame_id=work["id"], frame_status="preparing")
    ctx.set_state(m.META, "clock", clock)
    domains._set(ctx, domains.CONTROL, "frame", work)
    return work


def _settle_page(ctx, work):
    pending = []
    for entry in work["pending"]:
        domain = domains.get(ctx, entry["domain_id"])
        if domain is None or domain["status"] == "retired":
            # Topology mutation of a selected domain is fenced. This is an
            # explicit unexpected state, never a silently recalculated group.
            if work["status"] == "open":
                work["faults"] += 1
            continue
        if work["status"] == "preparing" and domain.get("prepared_frame") == work["id"]:
            work["prepared"] += 1
            continue
        if domain["last_tick"] >= work["target_tick"]:
            work["settled"] += 1
            continue
        checkpoint = _checkpoint(ctx, DOMAIN_PREFIX+domain["id"])
        if checkpoint is None or checkpoint["generation"] != entry["generation"]:
            code = "DomainCheckpointChanged"
        elif checkpoint["status"] == "pending":
            pending.append(entry)
            continue
        elif checkpoint["status"] == "completed":
            code = "DomainSettlementMissing"
        elif _version_blocked(checkpoint):
            # This receipt proves that no rule handler ran. Keep the exact
            # frame, logical target and event cutoff; settled neighbors remain
            # untouched and the successor is fenced by its own generation.
            entry["generation"] = _schedule_domain(ctx, domain, work)
            pending.append(entry)
            continue
        else:
            code = checkpoint["last_error_code"] or "DomainCheckpoint"+checkpoint["status"].title()
        domains.mark_failure(ctx, domain, work["target_tick"], code)
        work["pressure" if code == "CapacityPressure" else "faults"] += 1
    work["pending"] = pending


def _version_blocked(checkpoint):
    return (checkpoint is not None and checkpoint["status"] == "blocked"
            and checkpoint["last_error_code"] == "CheckpointVersionMismatch")


def _recover_version_faults(ctx):
    """Bounded restart repair, never replay an already closed physical frame."""
    recovery = domains._get(ctx, domains.CONTROL, "checkpoint_recovery")
    if not recovery or recovery["complete"]:
        return
    page = ctx.list_state(domains.DOMAINS, limit=PAGE_SIZE, after=recovery["cursor"])
    for item in page["items"]:
        domain = item["value"]
        if (domain.get("status") == "fault"
                and domain.get("failure", {}).get("code") == "CheckpointVersionMismatch"):
            checkpoint = _checkpoint(ctx, DOMAIN_PREFIX+domain["id"])
            if _version_blocked(checkpoint):
                domains.recover_checkpoint_version(ctx, domain, checkpoint)
    recovery.update(cursor=page["next_cursor"], complete=not page["has_more"])
    domains._set(ctx, domains.CONTROL, "checkpoint_recovery", recovery)


def _dispatch_page(ctx, work):
    preparing = work["status"] == "preparing"
    page = ctx.list_state(domains.DIRTY if preparing else domains.READY, limit=PAGE_SIZE, after=work["cursor"])
    exhausted = not page["has_more"]
    for item in page["items"]:
        if int(item["key"]) > work["high_water"]:
            exhausted = True
            break
        domain = domains.get(ctx, item["value"]["domain_id"])
        ready = domains._get(ctx, domains.READY, item["key"])
        if (domain is None or domain["status"] != "ready" or ready is None
                or ready["eligible_tick"] > work["target_tick"]
                or domain["last_tick"] >= work["target_tick"]):
            continue
        generation = _schedule_domain(ctx, domain, work)
        work["pending"].append({"domain_id": domain["id"], "generation": generation,
                                "topology_revision": domain["topology_revision"]})
    work["cursor"] = page["next_cursor"]
    work["finished_listing"] = exhausted


def _close(ctx, work):
    work["status"] = "closed"
    work["closed_at"] = ctx.now
    domains._set(ctx, domains.CONTROL, "frame", work)
    clock = ctx.get_state(m.META, "clock")
    clock.update(completed_sequence=work["target_tick"], frame_status="closed", last_tick_at=ctx.now)
    ctx.set_state(m.META, "clock", clock)
    # One successor only. A slow frame starts its next successor promptly;
    # elapsed wall time never creates extra physics steps to catch up.
    anchor = work.get("cadence_anchor_at", work["opened_at"])
    _schedule_frame(ctx, max(ctx.now, anchor+1))


def tick(ctx, args):
    """Open/resume one frame and dispatch at most eight independent groups."""
    if args and "epoch" in args:
        clock = ctx.get_state(m.META, "clock")
        if args["epoch"] != clock["epoch"] or args["sequence"] != clock["sequence"]:
            return FunctionOutcome({"advanced": False, "superseded": True, "tick": clock["sequence"]})
    domains.migrate(ctx)
    _recover_version_faults(ctx)
    work = domains.frame(ctx)
    if work is None or work["status"] == "closed":
        work = _open(ctx)
    _settle_page(ctx, work)
    if not work["pending"] and not work["finished_listing"]:
        _dispatch_page(ctx, work)
    if work["status"] == "preparing" and not work["pending"] and work["finished_listing"]:
        work.update(status="open", cursor=None, finished_listing=False,
                    high_water=domains._get(ctx, domains.CONTROL, "last_ordinal", 0))
        clock = ctx.get_state(m.META, "clock")
        clock.update(sequence=work["target_tick"], frame_status="open")
        ctx.set_state(m.META, "clock", clock)
        domains._set(ctx, domains.CONTROL, "frame", work)
        _dispatch_page(ctx, work)
    closed = not work["pending"] and work["finished_listing"]
    if closed:
        _close(ctx, work)
    else:
        domains._set(ctx, domains.CONTROL, "frame", work)
        # Children wake this checkpoint on a successful atomic settlement.
        # A bounded delayed poll also discovers rejected/failed child receipts.
        _schedule_frame(ctx, ctx.now+0.1)
    return FunctionOutcome({"advanced": closed, "tick": work["target_tick"], "frame_id": work["id"],
                            "frame_status": work["status"], "settled": work["settled"],
                            "pressure": work["pressure"], "faults": work["faults"]})


def advance_domain(ctx, args):
    work = domains.frame(ctx)
    domain = domains.get(ctx, args["domain_id"])
    if not work or work["status"] not in {"preparing", "open"} or work["id"] != args["frame_id"]:
        return FunctionOutcome({"advanced": False, "superseded": True, "tick": args["target_tick"]})
    if args["phase"] == "prepare":
        if work["status"] != "preparing":
            return FunctionOutcome({"advanced": False, "superseded": True, "tick": args["target_tick"]})
        if domain is None or domain["status"] == "retired":
            return FunctionOutcome({"advanced": False, "merged": True, "tick": args["target_tick"]})
        if args["target_tick"] != work["target_tick"]:
            raise RuleViolation("准备任务不属于当前世界逻辑轮。")
        if domain.get("prepared_frame") == work["id"]:
            return FunctionOutcome({"advanced": False, "already_prepared": True, "tick": args["target_tick"]})
        domains.prepare(ctx, domain, work["id"], work["target_tick"])
        _wake_frame(ctx)
        return FunctionOutcome({"advanced": False, "prepared": True, "tick": args["target_tick"], "domain_id": domain["id"]})
    if work["status"] != "open":
        return FunctionOutcome({"advanced": False, "superseded": True, "tick": args["target_tick"]})
    if domain is None or domain["status"] == "retired" or domain["topology_revision"] != args["topology_revision"]:
        raise RuleViolation("域任务的真实拓扑已改变；旧任务未计算。")
    if domain["last_tick"] >= args["target_tick"]:
        return FunctionOutcome({"advanced": False, "already_settled": True, "tick": args["target_tick"]})
    if args["target_tick"] != work["target_tick"] or not domains.pending_in_frame(ctx, domain):
        raise RuleViolation("域任务不属于当前世界逻辑轮。")
    from .processes import advance_domain as compute
    old_cutoff = getattr(ctx, "_grove_event_cutoff", None)
    ctx._grove_event_cutoff = work["event_cutoff"]
    ctx._grove_domain_work = True
    try:
        with domains.scope(ctx, domain):
            outcome = compute(ctx, args["target_tick"], domain)
            domain = domains.current(ctx)
            domain["last_tick"] = domain["last_applied_tick"] = args["target_tick"]
            domains.refresh_ready(ctx, domain)
    finally:
        ctx._grove_domain_work = False
        ctx._grove_event_cutoff = old_cutoff
    _wake_frame(ctx)
    return outcome


def state_specs():
    # Frame coordination uses the domain control scope's existing CAS owner.
    return ()
