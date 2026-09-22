from __future__ import annotations

import pathlib
import sqlite3
import tempfile

from runtime_core import WorldRuntime, WorldRuntimeError
from world_zero_universe import install_world_zero


def fresh():
    tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
    db = pathlib.Path(tmp.name) / "world-zero.sqlite3"
    runtime = WorldRuntime(db)
    install_world_zero(runtime, "world-zero")
    return tmp, db, runtime


def test_initial_observe_is_read_only():
    tmp, db, runtime = fresh()
    try:
        role = runtime.create_role("First")
        places = runtime.query_function(
            "world-zero", "world.list_places", role["role_id"], {}
        )
        assert places["result"]["current_place_id"] == "threshold"
        assert {p["place_id"] for p in places["result"]["places"]} == {
            "threshold", "garden", "observatory"
        }

        observed = runtime.query_function(
            "world-zero", "world.observe", role["role_id"], {}
        )
        assert observed["result"]["place"]["place_id"] == "threshold"
        assert observed["result"]["marks"] == []

        with sqlite3.connect(db) as conn:
            assert conn.execute("SELECT COUNT(*) FROM operations").fetchone()[0] == 0
            assert conn.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 0
        print("PASS initial_observe_is_read_only")
    finally:
        tmp.cleanup()
def test_visit_and_mark_persist():
    tmp, _, runtime = fresh()
    try:
        role = runtime.create_role("Lantern")
        visit = runtime.invoke_function(
            "world-zero",
            "world.visit",
            role["role_id"],
            {"place_id": "garden"},
            operation_id="visit-garden-1",
        )
        assert visit["result"]["from_place_id"] == "threshold"
        assert visit["result"]["place"]["place_id"] == "garden"

        mark = runtime.invoke_function(
            "world-zero",
            "world.leave_mark",
            role["role_id"],
            {"mark_id": "lantern-arrived", "text": "Lantern arrived quietly."},
            operation_id="mark-1",
        )
        assert mark["result"]["place_id"] == "garden"

        observed = runtime.query_function(
            "world-zero", "world.observe", role["role_id"], {}
        )
        assert observed["result"]["place"]["place_id"] == "garden"
        assert observed["result"]["marks"][0]["mark_id"] == "lantern-arrived"
        assert observed["result"]["marks"][0]["author_display_name"] == "Lantern"

        replay = runtime.invoke_function(
            "world-zero",
            "world.leave_mark",
            role["role_id"],
            {"mark_id": "lantern-arrived", "text": "Lantern arrived quietly."},
            operation_id="mark-1",
        )
        assert replay["replayed"] is True

        try:
            runtime.invoke_function(
                "world-zero",
                "world.leave_mark",
                role["role_id"],
                {"mark_id": "lantern-arrived", "text": "Overwrite attempt"},
                operation_id="mark-2",
            )
        except WorldRuntimeError:
            pass
        else:
            raise AssertionError("existing mark_id was overwritten")
        print("PASS visit_and_mark_persist")
    finally:
        tmp.cleanup()
def test_second_role_can_discover_mark():
    tmp, _, runtime = fresh()
    try:
        first = runtime.create_role("First")
        second = runtime.create_role("Second")

        runtime.invoke_function(
            "world-zero",
            "world.visit",
            first["role_id"],
            {"place_id": "observatory"},
            operation_id="first-visit",
        )
        runtime.invoke_function(
            "world-zero",
            "world.leave_mark",
            first["role_id"],
            {"mark_id": "signal-one", "text": "A small signal remains."},
            operation_id="first-mark",
        )

        before = runtime.query_function(
            "world-zero", "world.observe", second["role_id"], {}
        )
        assert before["result"]["place"]["place_id"] == "threshold"
        assert before["result"]["marks"] == []

        runtime.invoke_function(
            "world-zero",
            "world.visit",
            second["role_id"],
            {"place_id": "observatory"},
            operation_id="second-visit",
        )
        after = runtime.query_function(
            "world-zero", "world.observe", second["role_id"], {}
        )
        marks = after["result"]["marks"]
        assert len(marks) == 1
        assert marks[0]["mark_id"] == "signal-one"
        assert marks[0]["author_role_id"] == first["role_id"]
        print("PASS second_role_can_discover_mark")
    finally:
        tmp.cleanup()


def main():
    test_initial_observe_is_read_only()
    test_visit_and_mark_persist()
    test_second_role_can_discover_mark()
    print("V08 WORLD ZERO CORE: 3/3 passed")


if __name__ == "__main__":
    main()
