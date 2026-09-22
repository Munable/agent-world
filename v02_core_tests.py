from __future__ import annotations

import concurrent.futures
import json
import pathlib
import threading
import time
import traceback

from demo_universe import install_demo_universe
from runtime_core import (
    ActivityConflict,
    ClaimBusy,
    ClaimFenced,
    CursorExpired,
    EventSpec,
    FunctionContext,
    FunctionOutcome,
    FunctionVersionMismatch,
    OperationConflict,
    RegistryConflict,
    SchemaRejected,
    WorldRuntime,
)

ROOT = pathlib.Path(__file__).resolve().parent
RESULTS = ROOT / "v02_core_results.json"

tests = []


def test(name):
    def deco(fn):
        tests.append((name, fn))
        return fn
    return deco


def fresh(name: str, universe: str = "demo"):
    path = ROOT / f"v02_{name}.sqlite3"
    for suffix in ("", "-wal", "-shm"):
        p = pathlib.Path(str(path) + suffix)
        if p.exists():
            p.unlink()
    runtime = WorldRuntime(path)
    install_demo_universe(runtime, universe)
    return runtime, path


@test("registry_exposes_concrete_function_schemas")
def _():
    w, _ = fresh("01")
    funcs = {f["function_id"]: f for f in w.list_functions("demo")}
    assert set(funcs) == {
        "counter.increment",
        "counter.get",
        "activity.score.add",
    }
    assert funcs["counter.increment"]["input_schema"]["required"] == ["amount"]
    assert funcs["counter.increment"]["access"] == "write"
    assert funcs["counter.get"]["access"] == "read"
    assert funcs["counter.get"]["input_schema"]["properties"] == {}
    assert funcs["activity.score.add"]["requires_activity_claim"] is True


@test("schema_rejection_has_no_side_effect")
def _():
    w, _ = fresh("02")
    try:
        w.invoke_function(
            "demo", "counter.increment", "A", {"amount": 999},
            operation_id="bad-schema",
        )
    except SchemaRejected:
        pass
    else:
        raise AssertionError("invalid arguments were accepted")
    assert w.get_state("demo", "role:A", "counter", 0)["value"] == 0
    assert w.read_changes("demo", "A", 0) == []


@test("explicit_idempotency_executes_once")
def _():
    w, _ = fresh("03")
    first = w.invoke_function(
        "demo", "counter.increment", "A", {"amount": 2},
        operation_id="same-op",
    )
    second = w.invoke_function(
        "demo", "counter.increment", "A", {"amount": 2},
        operation_id="same-op",
    )
    assert first["result"]["value"] == 2
    assert second["result"]["value"] == 2 and second["replayed"] is True
    assert w.get_state("demo", "role:A", "counter")["value"] == 2
    assert len(w.read_changes("demo", "A", 0)) == 1


@test("same_arguments_new_operation_is_new_action")
def _():
    w, _ = fresh("04")
    w.invoke_function("demo", "counter.increment", "A", {"amount": 2}, operation_id="one")
    w.invoke_function("demo", "counter.increment", "A", {"amount": 2}, operation_id="two")
    assert w.get_state("demo", "role:A", "counter")["value"] == 4


@test("same_operation_id_different_arguments_conflicts")
def _():
    w, _ = fresh("05")
    w.invoke_function("demo", "counter.increment", "A", {"amount": 1}, operation_id="x")
    try:
        w.invoke_function("demo", "counter.increment", "A", {"amount": 2}, operation_id="x")
    except OperationConflict:
        pass
    else:
        raise AssertionError("conflicting idempotency reuse was accepted")
    assert w.get_state("demo", "role:A", "counter")["value"] == 1


@test("idempotency_scope_is_role_and_universe_local")
def _():
    w, _ = fresh("06", "u1")
    install_demo_universe(w, "u2")
    w.invoke_function("u1", "counter.increment", "A", {"amount": 1}, operation_id="local")
    w.invoke_function("u1", "counter.increment", "B", {"amount": 1}, operation_id="local")
    w.invoke_function("u2", "counter.increment", "A", {"amount": 1}, operation_id="local")
    assert w.get_state("u1", "role:A", "counter")["value"] == 1
    assert w.get_state("u1", "role:B", "counter")["value"] == 1
    assert w.get_state("u2", "role:A", "counter")["value"] == 1


@test("descriptor_change_requires_version_bump")
def _():
    w, _ = fresh("07")

    def handler(ctx: FunctionContext, args: dict) -> FunctionOutcome:
        return FunctionOutcome(result={"ok": True})

    try:
        w.register_function(
            "demo",
            "counter.increment",
            1,
            "Changed description without version bump.",
            {"type": "object", "properties": {}, "additionalProperties": False},
            handler,
        )
    except RegistryConflict:
        pass
    else:
        raise AssertionError("schema drift without version bump was accepted")


@test("new_call_can_pin_function_version")
def _():
    w, _ = fresh("08")

    def v2(ctx: FunctionContext, args: dict) -> FunctionOutcome:
        return FunctionOutcome(result={"version": 2})

    w.register_function(
        "demo",
        "counter.increment",
        2,
        "Version two counter contract.",
        {
            "type": "object",
            "properties": {"amount": {"type": "integer"}},
            "required": ["amount"],
            "additionalProperties": False,
        },
        v2,
    )
    try:
        w.invoke_function(
            "demo", "counter.increment", "A", {"amount": 1},
            operation_id="new-after-upgrade", expected_version=1,
        )
    except FunctionVersionMismatch:
        pass
    else:
        raise AssertionError("stale expected version was silently upgraded")


@test("completed_retry_survives_registry_upgrade")
def _():
    w, _ = fresh("09")
    first = w.invoke_function(
        "demo", "counter.increment", "A", {"amount": 1},
        operation_id="before-upgrade", expected_version=1,
    )

    def v2(ctx: FunctionContext, args: dict) -> FunctionOutcome:
        return FunctionOutcome(result={"version": 2})

    w.register_function(
        "demo",
        "counter.increment",
        2,
        "Version two counter contract.",
        {
            "type": "object",
            "properties": {"amount": {"type": "integer"}},
            "required": ["amount"],
            "additionalProperties": False,
        },
        v2,
    )
    replay = w.invoke_function(
        "demo", "counter.increment", "A", {"amount": 1},
        operation_id="before-upgrade", expected_version=1,
    )
    assert first["function_version"] == 1
    assert replay["function_version"] == 1 and replay["replayed"] is True
    assert w.get_state("demo", "role:A", "counter")["value"] == 1


@test("claim_required_function_rejects_missing_proof")
def _():
    w, _ = fresh("10")
    w.start_activity("demo", "job-1", "A", "research", exclusive_group="work", ttl_seconds=5)
    try:
        w.invoke_function(
            "demo", "activity.score.add", "A", {"delta": 1},
            operation_id="no-claim",
        )
    except ClaimFenced:
        pass
    else:
        raise AssertionError("claimed function ran without claim")
    assert w.get_state("demo", "activity:job-1", "score", 0)["value"] == 0


@test("same_runtime_claim_replay_is_stable")
def _():
    w, _ = fresh("11")
    w.start_activity("demo", "job-1", "A", "research", ttl_seconds=5)
    one = w.claim_activity("demo", "job-1", "A", "runtime-A", lease_seconds=2)
    two = w.claim_activity("demo", "job-1", "A", "runtime-A", lease_seconds=2)
    assert one["claim_epoch"] == two["claim_epoch"] == 1
    assert one["claim_token"] == two["claim_token"]
    assert two["replayed"] is True


@test("different_runtime_cannot_steal_live_claim")
def _():
    w, _ = fresh("12")
    w.start_activity("demo", "job-1", "A", "research", ttl_seconds=5)
    w.claim_activity("demo", "job-1", "A", "runtime-A", lease_seconds=2)
    try:
        w.claim_activity("demo", "job-1", "A", "runtime-B", lease_seconds=2)
    except ClaimBusy:
        pass
    else:
        raise AssertionError("live claim was stolen")



@test("expired_claim_can_be_taken_over_with_higher_epoch")
def _():
    w, _ = fresh("13")
    w.start_activity("demo", "job-1", "A", "research", ttl_seconds=5)
    old = w.claim_activity("demo", "job-1", "A", "runtime-A", lease_seconds=0.08)
    time.sleep(0.11)
    new = w.claim_activity("demo", "job-1", "A", "runtime-B", lease_seconds=2)
    assert old["claim_epoch"] == 1
    assert new["claim_epoch"] == 2


@test("stale_claim_is_fenced_before_world_state_change")
def _():
    w, _ = fresh("14")
    w.start_activity("demo", "job-1", "A", "research", ttl_seconds=5)
    old = w.claim_activity("demo", "job-1", "A", "runtime-A", lease_seconds=0.08)
    time.sleep(0.11)
    new = w.claim_activity("demo", "job-1", "A", "runtime-B", lease_seconds=2)

    try:
        w.invoke_function(
            "demo",
            "activity.score.add",
            "A",
            {"delta": 7},
            operation_id="stale-submit",
            activity_claim=old,
        )
    except ClaimFenced:
        pass
    else:
        raise AssertionError("stale runtime mutated world state")

    assert w.get_state("demo", "activity:job-1", "score", 0)["value"] == 0
    assert w.read_changes("demo", "A", 0) == []

    ok = w.invoke_function(
        "demo",
        "activity.score.add",
        "A",
        {"delta": 7},
        operation_id="fresh-submit",
        activity_claim=new,
    )
    assert ok["result"]["score"] == 7
    assert w.get_state("demo", "activity:job-1", "score")["value"] == 7


@test("concurrent_claim_has_exactly_one_winner")
def _():
    w, _ = fresh("15")
    w.start_activity("demo", "job-1", "A", "research", ttl_seconds=5)
    barrier = threading.Barrier(9)

    def contender(i):
        barrier.wait(timeout=3)
        try:
            return w.claim_activity(
                "demo", "job-1", "A", f"runtime-{i}", lease_seconds=1
            )
        except ClaimBusy:
            return None

    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        futures = [pool.submit(contender, i) for i in range(8)]
        barrier.wait(timeout=3)
        results = [f.result(timeout=3) for f in futures]

    winners = [x for x in results if x is not None]
    assert len(winners) == 1
    assert winners[0]["claim_epoch"] == 1


@test("claim_proof_survives_runtime_process_restart")
def _():
    w1, path = fresh("16")
    w1.start_activity("demo", "job-1", "A", "research", ttl_seconds=5)
    proof = w1.claim_activity(
        "demo", "job-1", "A", "runtime-A", lease_seconds=2
    )

    w2 = WorldRuntime(path)
    install_demo_universe(w2, "demo")
    replay = w2.claim_activity(
        "demo", "job-1", "A", "runtime-A", lease_seconds=2
    )
    assert replay["claim_epoch"] == proof["claim_epoch"]
    assert replay["claim_token"] == proof["claim_token"]


@test("claim_is_universe_scoped")
def _():
    w, _ = fresh("17", "u1")
    install_demo_universe(w, "u2")
    w.start_activity("u1", "job", "A", "research", ttl_seconds=5)
    w.start_activity("u2", "job", "A", "research", ttl_seconds=5)
    a = w.claim_activity("u1", "job", "A", "runtime-A", lease_seconds=2)
    b = w.claim_activity("u2", "job", "A", "runtime-B", lease_seconds=2)
    assert a["claim_epoch"] == b["claim_epoch"] == 1


@test("handler_failure_rolls_back_state_event_and_receipt")
def _():
    w, _ = fresh("18")

    def broken(ctx: FunctionContext, args: dict) -> FunctionOutcome:
        ctx.set_state(f"role:{ctx.actor_role_id}", "should_not_exist", 123)
        raise RuntimeError("forced handler failure")

    w.register_function(
        "demo",
        "test.broken",
        1,
        "Write then fail to verify transaction rollback.",
        {
            "type": "object",
            "properties": {},
            "additionalProperties": False,
        },
        broken,
    )
    try:
        w.invoke_function(
            "demo", "test.broken", "A", {}, operation_id="broken-op"
        )
    except RuntimeError:
        pass
    else:
        raise AssertionError("broken handler unexpectedly succeeded")

    assert w.get_state(
        "demo", "role:A", "should_not_exist", None
    )["version"] == 0
    assert w.read_changes("demo", "A", 0) == []

    # Same key can be retried because the failed transaction did not persist a receipt.
    try:
        w.invoke_function(
            "demo", "test.broken", "A", {}, operation_id="broken-op"
        )
    except RuntimeError:
        pass
    else:
        raise AssertionError("failed operation was incorrectly cached as success")


@test("event_cursor_and_cleanup_gap_remain_explicit")
def _():
    w, _ = fresh("19")
    first = w.invoke_function(
        "demo", "counter.increment", "A", {"amount": 1},
        operation_id="one",
    )
    events = w.read_changes("demo", "A", 0)
    assert len(events) == 1
    seq = events[0]["seq"]
    assert first["event_seqs"] == [seq]

    w.cleanup_events("demo", seq)
    try:
        w.read_changes("demo", "A", 0)
    except CursorExpired:
        pass
    else:
        raise AssertionError("stale cursor gap was hidden")


@test("activity_exclusivity_still_does_not_create_global_busy")
def _():
    w, _ = fresh("20")
    w.start_activity(
        "demo", "raid-1", "A", "raid",
        exclusive_group="combat", ttl_seconds=5,
    )
    try:
        w.start_activity(
            "demo", "raid-2", "A", "raid",
            exclusive_group="combat", ttl_seconds=5,
        )
    except ActivityConflict:
        pass
    else:
        raise AssertionError("second exclusive combat activity was accepted")

    # An unrelated world function remains usable.
    result = w.invoke_function(
        "demo", "counter.increment", "A", {"amount": 1},
        operation_id="nonexclusive-action",
    )
    assert result["result"]["value"] == 1


@test("start_activity_retry_replays_same_activity")
def _():
    w, _ = fresh("21")
    first = w.start_activity(
        "demo", "same-job", "A", "research",
        exclusive_group="work", ttl_seconds=5,
    )
    retry = w.start_activity(
        "demo", "same-job", "A", "research",
        exclusive_group="work", ttl_seconds=999,
    )
    assert first["replayed"] is False
    assert retry["replayed"] is True
    assert retry["expires_at"] == first["expires_at"]


@test("start_activity_same_id_different_shape_conflicts")
def _():
    w, _ = fresh("22")
    w.start_activity("demo", "same-job", "A", "research", ttl_seconds=5)
    try:
        w.start_activity("demo", "same-job", "A", "combat", ttl_seconds=5)
    except ActivityConflict:
        pass
    else:
        raise AssertionError("same activity_id was reused for a different activity")


@test("tampered_claim_token_is_fenced")
def _():
    w, _ = fresh("23")
    w.start_activity("demo", "job", "A", "research", ttl_seconds=5)
    proof = w.claim_activity("demo", "job", "A", "runtime-A", lease_seconds=2)
    proof["claim_token"] += "tampered"
    try:
        w.invoke_function(
            "demo", "activity.score.add", "A", {"delta": 3},
            operation_id="tampered", activity_claim=proof,
        )
    except ClaimFenced:
        pass
    else:
        raise AssertionError("tampered claim token was accepted")
    assert w.get_state("demo", "activity:job", "score", 0)["value"] == 0


@test("claim_from_wrong_role_is_fenced")
def _():
    w, _ = fresh("24")
    w.start_activity("demo", "job", "A", "research", ttl_seconds=5)
    proof = w.claim_activity("demo", "job", "A", "runtime-A", lease_seconds=2)
    try:
        w.invoke_function(
            "demo", "activity.score.add", "B", {"delta": 3},
            operation_id="wrong-role", activity_claim=proof,
        )
    except ClaimFenced:
        pass
    else:
        raise AssertionError("another role used A's claim")
    assert w.get_state("demo", "activity:job", "score", 0)["value"] == 0


@test("activity_expiry_fences_claim_before_mutation")
def _():
    w, _ = fresh("25")
    w.start_activity("demo", "job", "A", "research", ttl_seconds=0.08)
    proof = w.claim_activity("demo", "job", "A", "runtime-A", lease_seconds=2)
    time.sleep(0.11)
    try:
        w.invoke_function(
            "demo", "activity.score.add", "A", {"delta": 5},
            operation_id="after-activity-expired", activity_claim=proof,
        )
    except ClaimFenced:
        pass
    else:
        raise AssertionError("expired activity accepted a mutation")
    assert w.get_state("demo", "activity:job", "score", 0)["value"] == 0


@test("failed_stale_attempt_can_retry_same_operation_with_fresh_claim")
def _():
    w, _ = fresh("26")
    w.start_activity("demo", "job", "A", "research", ttl_seconds=5)
    old = w.claim_activity("demo", "job", "A", "old", lease_seconds=0.08)
    time.sleep(0.11)
    new_claim = w.claim_activity("demo", "job", "A", "new", lease_seconds=2)
    try:
        w.invoke_function(
            "demo", "activity.score.add", "A", {"delta": 4},
            operation_id="retry-me", activity_claim=old,
        )
    except ClaimFenced:
        pass
    else:
        raise AssertionError("stale attempt unexpectedly succeeded")
    result = w.invoke_function(
        "demo", "activity.score.add", "A", {"delta": 4},
        operation_id="retry-me", activity_claim=new_claim,
    )
    assert result["result"]["score"] == 4


@test("completed_claimed_operation_replays_after_claim_expiry")
def _():
    w, _ = fresh("27")
    w.start_activity("demo", "job", "A", "research", ttl_seconds=5)
    proof = w.claim_activity("demo", "job", "A", "runtime-A", lease_seconds=0.08)
    first = w.invoke_function(
        "demo", "activity.score.add", "A", {"delta": 6},
        operation_id="done-before-expiry", activity_claim=proof,
    )
    time.sleep(0.11)
    replay = w.invoke_function(
        "demo", "activity.score.add", "A", {"delta": 6},
        operation_id="done-before-expiry", activity_claim=proof,
    )
    assert replay["replayed"] is True
    assert replay["result"] == first["result"]
    assert w.get_state("demo", "activity:job", "score")["value"] == 6


@test("concurrent_duplicate_invoke_across_runtime_instances_executes_once")
def _():
    w1, path = fresh("28")
    w2 = WorldRuntime(path)
    install_demo_universe(w2, "demo")
    barrier = threading.Barrier(21)

    def run(i):
        barrier.wait(timeout=3)
        w = w1 if i % 2 == 0 else w2
        return w.invoke_function(
            "demo", "counter.increment", "A", {"amount": 1},
            operation_id="shared-duplicate",
        )

    with concurrent.futures.ThreadPoolExecutor(max_workers=20) as pool:
        futures = [pool.submit(run, i) for i in range(20)]
        barrier.wait(timeout=3)
        rows = [f.result(timeout=5) for f in futures]

    assert w1.get_state("demo", "role:A", "counter")["value"] == 1
    assert sum(1 for row in rows if row["replayed"] is False) == 1


@test("concurrent_distinct_invokes_across_runtime_instances_preserve_updates")
def _():
    w1, path = fresh("29")
    w2 = WorldRuntime(path)
    install_demo_universe(w2, "demo")

    def run(i):
        w = w1 if i % 2 == 0 else w2
        return w.invoke_function(
            "demo", "counter.increment", "A", {"amount": 1},
            operation_id=f"distinct-{i}",
        )

    with concurrent.futures.ThreadPoolExecutor(max_workers=16) as pool:
        list(pool.map(run, range(50)))
    assert w1.get_state("demo", "role:A", "counter")["value"] == 50


@test("persisted_descriptor_without_handler_is_not_advertised")
def _():
    w1, path = fresh("30")

    def extra(ctx: FunctionContext, args: dict) -> FunctionOutcome:
        return FunctionOutcome(result={"ok": True})

    w1.register_function(
        "demo", "temporary.extra", 1, "Temporary function.",
        {"type": "object", "properties": {}, "additionalProperties": False},
        extra,
    )
    assert "temporary.extra" in {
        x["function_id"] for x in w1.list_functions("demo")
    }

    w2 = WorldRuntime(path)
    install_demo_universe(w2, "demo")
    assert "temporary.extra" not in {
        x["function_id"] for x in w2.list_functions("demo")
    }


@test("bootstrap_hides_time_expired_activity_without_external_sweep")
def _():
    w, _ = fresh("31")
    w.start_activity("demo", "short-job", "A", "research", ttl_seconds=0.08)
    time.sleep(0.11)
    boot = w.bootstrap("demo", "A")
    assert boot["active_activities"] == []


def main():
    rows = []
    for name, fn in tests:
        started = time.monotonic()
        try:
            fn()
            rows.append({
                "name": name,
                "passed": True,
                "seconds": round(time.monotonic() - started, 3),
            })
            print("PASS", name, flush=True)
        except Exception as exc:
            rows.append({
                "name": name,
                "passed": False,
                "seconds": round(time.monotonic() - started, 3),
                "error": f"{type(exc).__name__}: {exc}",
                "traceback": traceback.format_exc(),
            })
            print("FAIL", name, type(exc).__name__, str(exc), flush=True)

    summary = {
        "passed": sum(1 for r in rows if r["passed"]),
        "failed": sum(1 for r in rows if not r["passed"]),
        "total": len(rows),
        "tests": rows,
    }
    RESULTS.write_text(
        json.dumps(summary, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(json.dumps({
        "passed": summary["passed"],
        "failed": summary["failed"],
        "total": summary["total"],
    }))
    return 0 if summary["failed"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
