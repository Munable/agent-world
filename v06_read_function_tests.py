from __future__ import annotations

import pathlib
import sqlite3
import tempfile

from demo_universe import install_demo_universe
from runtime_core import (
    EventSpec,
    FunctionContext,
    FunctionOutcome,
    RegistryConflict,
    WorldRuntime,
    WorldRuntimeError,
)


def fresh():
    tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
    db = pathlib.Path(tmp.name) / "read.sqlite3"
    runtime = WorldRuntime(db)
    install_demo_universe(runtime, "demo")
    return tmp, db, runtime


def test_read_query_has_no_operation_or_event_side_effect():
    tmp, db, runtime = fresh()
    try:
        write = runtime.invoke_function(
            "demo",
            "counter.increment",
            "A",
            {"amount": 3},
            operation_id="write-1",
        )
        assert write["result"]["value"] == 3

        read = runtime.query_function("demo", "counter.get", "A", {})
        assert read["result"]["value"] == 3
        assert read["read_only"] is True

        with sqlite3.connect(db) as conn:
            operations = conn.execute("SELECT COUNT(*) FROM operations").fetchone()[0]
            events = conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
        assert operations == 1
        assert events == 1
        print("PASS read_query_has_no_operation_or_event_side_effect")
    finally:
        tmp.cleanup()
def test_read_handler_cannot_use_context_set_state():
    tmp, _, runtime = fresh()
    try:
        def bad(ctx: FunctionContext, arguments: dict) -> FunctionOutcome:
            ctx.set_state("global", "bad", 1)
            return FunctionOutcome(result={"ok": True})

        runtime.register_function(
            "demo",
            "bad.read.set_state",
            1,
            "Bad read.",
            {"type": "object", "properties": {}, "additionalProperties": False},
            bad,
            access="read",
        )
        try:
            runtime.query_function("demo", "bad.read.set_state", "A", {})
        except WorldRuntimeError as exc:
            assert "read function cannot modify" in str(exc)
        else:
            raise AssertionError("read handler mutated through set_state")
        print("PASS read_handler_cannot_use_context_set_state")
    finally:
        tmp.cleanup()


def test_read_handler_cannot_write_sql_directly():
    tmp, _, runtime = fresh()
    try:
        def bad(ctx: FunctionContext, arguments: dict) -> FunctionOutcome:
            ctx.conn.execute(
                """INSERT INTO world_state(
                     universe,scope,state_key,value_json,version,updated_at
                   ) VALUES('demo','global','bad','1',1,0)"""
            )
            return FunctionOutcome(result={"ok": True})
        runtime.register_function(
            "demo",
            "bad.read.sql",
            1,
            "Bad direct SQL read.",
            {"type": "object", "properties": {}, "additionalProperties": False},
            bad,
            access="read",
        )
        try:
            runtime.query_function("demo", "bad.read.sql", "A", {})
        except WorldRuntimeError as exc:
            assert "database write" in str(exc)
        else:
            raise AssertionError("query_only did not block direct SQL write")
        print("PASS read_handler_cannot_write_sql_directly")
    finally:
        tmp.cleanup()


def test_read_handler_cannot_emit_event():
    tmp, _, runtime = fresh()
    try:
        def bad(ctx: FunctionContext, arguments: dict) -> FunctionOutcome:
            return FunctionOutcome(
                result={"ok": True},
                events=(
                    EventSpec(
                        recipient_role_id=ctx.actor_role_id,
                        kind="bad",
                        payload={"bad": True},
                    ),
                ),
            )

        runtime.register_function(
            "demo",
            "bad.read.event",
            1,
            "Bad event read.",
            {"type": "object", "properties": {}, "additionalProperties": False},
            bad,
            access="read",
        )
        try:
            runtime.query_function("demo", "bad.read.event", "A", {})
        except WorldRuntimeError as exc:
            assert "cannot emit durable events" in str(exc)
        else:
            raise AssertionError("read handler emitted an event")
        print("PASS read_handler_cannot_emit_event")
    finally:
        tmp.cleanup()
def test_registry_rejects_invalid_read_contracts():
    tmp, _, runtime = fresh()
    try:
        def ok(ctx: FunctionContext, arguments: dict) -> FunctionOutcome:
            return FunctionOutcome(result={})

        try:
            runtime.register_function(
                "demo",
                "bad.access",
                1,
                "Bad access.",
                {"type": "object", "properties": {}, "additionalProperties": False},
                ok,
                access="banana",
            )
        except RegistryConflict:
            pass
        else:
            raise AssertionError("invalid access was registered")

        try:
            runtime.register_function(
                "demo",
                "bad.read.claim",
                1,
                "Read claim.",
                {"type": "object", "properties": {}, "additionalProperties": False},
                ok,
                access="read",
                requires_activity_claim=True,
            )
        except RegistryConflict:
            pass
        else:
            raise AssertionError("read function accepted activity claim requirement")
        print("PASS registry_rejects_invalid_read_contracts")
    finally:
        tmp.cleanup()


def main():
    test_read_query_has_no_operation_or_event_side_effect()
    test_read_handler_cannot_use_context_set_state()
    test_read_handler_cannot_write_sql_directly()
    test_read_handler_cannot_emit_event()
    test_registry_rejects_invalid_read_contracts()
    print("V06 READ CORE: 5/5 passed")


if __name__ == "__main__":
    main()
