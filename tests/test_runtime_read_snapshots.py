"""WAL readers retain a coherent published code/policy and database snapshot."""
from concurrent.futures import ThreadPoolExecutor, TimeoutError
from dataclasses import replace
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from agent_world import WorldRuntime, WorldDefinition, FunctionSpec, FunctionOutcome, StateRule, ViewSpec, StreamSpec, TimerSpec, EventSpec, PresentationCue
from agent_world.errors import FunctionVersionMismatch, PermissionDenied, WorldVersionMismatch

EMPTY = {"type": "object", "properties": {}, "additionalProperties": False}


class RuntimeReadSnapshotContracts(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory(prefix="world-read-snapshot-")
        self.addCleanup(directory.cleanup)
        self.runtime = WorldRuntime(Path(directory.name) / "world.sqlite3")
        self.addCleanup(lambda: getattr(self.runtime, "close", lambda: None)())
        self.role = self.runtime.create_role("reader")["role_id"]

    def fixture(self, read, *, write=None, state_authorizer=None):
        def initialize(ctx):
            ctx.set_state("shared", "number", {"value": 1})
            ctx.set_state("shared", "stable", {"value": 7})

        def project(ctx, args):
            return {"entities": {"stable": ctx.get_state("shared", "stable")}}

        functions = (FunctionSpec("number", read, EMPTY, access="read"),)
        if write:
            functions += (FunctionSpec("write", write, EMPTY),)
        return WorldDefinition("snapshot-world", "Snapshot world", functions,
            state_rules=(StateRule("shared", "", {"type": "object"}),),
            initialize=initialize, state_authorizer=state_authorizer,
            views=(ViewSpec("stable", project, public=True),),
            streams=(StreamSpec("public", public=True),))

    def test_reads_and_unchanged_view_do_not_wait_for_uncommitted_writer(self):
        entered, release = threading.Event(), threading.Event()

        def read(ctx, args):
            return FunctionOutcome(ctx.get_state("shared", "number"))

        def write(ctx, args):
            ctx.set_state("shared", "number", {"value": 2})
            entered.set()
            if not release.wait(3):
                raise RuntimeError("test writer was not released")
            return FunctionOutcome({"written": True})

        self.runtime.install_world("u", self.fixture(read, write=write))
        prior = self.runtime.view_snapshot("u", self.role, "stable")
        with ThreadPoolExecutor(max_workers=10) as pool:
            writer = pool.submit(self.runtime.call_function, "u", "write", self.role, {}, operation_id="writer")
            try:
                self.assertTrue(entered.wait(1))
                readers = [
                    pool.submit(self.runtime.query_function, "u", "number", self.role, {}),
                    pool.submit(self.runtime.list_functions, "u", actor_role_id=self.role),
                    pool.submit(self.runtime.describe_world, "u", self.role),
                    pool.submit(self.runtime.list_views, "u", self.role),
                    pool.submit(self.runtime.list_streams, "u", self.role),
                    pool.submit(self.runtime.read_stream, "u", "public", self.role),
                    pool.submit(self.runtime.view_sync, "u", self.role, prior["cursor"]),
                    pool.submit(self.runtime.wait_changes, "u", self.role, timeout=0),
                ]
                results = [future.result(timeout=1) for future in readers]
                self.assertEqual(results[0]["result"], {"value": 1})
                self.assertEqual(results[6]["cursor"], prior["cursor"])
                self.assertFalse(writer.done())
            finally:
                release.set()
            writer.result(timeout=2)
        self.assertEqual(self.runtime.query_function("u", "number", self.role, {})["result"], {"value": 2})

    def test_install_during_read_keeps_old_state_and_old_authorizer_together(self):
        entered, release = threading.Event(), threading.Event()

        def read(ctx, args):
            first = ctx.get_state("shared", "number")["value"]
            entered.set()
            if not release.wait(3):
                raise RuntimeError("test reader was not released")
            return FunctionOutcome({"first": first, "second": ctx.get_state("shared", "number")["value"]})

        initial = self.fixture(read, state_authorizer=lambda ctx, scope, key, action: True)
        self.runtime.install_world("u", initial)

        def migrate(ctx):
            ctx.set_state("shared", "number", {"value": 2})

        def deny_reads(ctx, scope, key, action):
            return action == "write"

        updated = replace(initial, version=2, state_version=2, migrations={2: migrate},
            state_authorizer=deny_reads, functions=(FunctionSpec("number", read, EMPTY, access="read", version=2),))
        with ThreadPoolExecutor(max_workers=2) as pool:
            reader = pool.submit(self.runtime.query_function, "u", "number", self.role, {})
            try:
                self.assertTrue(entered.wait(1))
                pool.submit(self.runtime.install_world, "u", updated).result(timeout=1)
                self.assertFalse(reader.done())
            finally:
                release.set()
            self.assertEqual(reader.result(timeout=2)["result"], {"first": 1, "second": 1})
        with self.assertRaises(FunctionVersionMismatch):
            self.runtime.query_function("u", "number", self.role, {}, expected_version=1)
        with self.assertRaises(PermissionDenied):
            self.runtime.query_function("u", "number", self.role, {}, expected_version=2)

    def test_old_code_bundle_cannot_read_a_newer_persisted_contract(self):
        def read(ctx, args):
            return FunctionOutcome({"read": True})

        initial = self.fixture(read)
        self.runtime.install_world("u", initial)
        old = self.runtime._capture_registry("u")
        self.runtime.install_world("u", replace(initial, version=2))
        with patch.object(self.runtime, "_capture_registry", return_value=old):
            with self.assertRaises(WorldVersionMismatch):
                self.runtime.query_function("u", "number", self.role, {})

    def test_unmanaged_bundle_cannot_bypass_newly_installed_world_authorization(self):
        def read(ctx, args):
            return FunctionOutcome({"read": True})

        self.runtime.register_function("u", "number", 1, "", EMPTY, read, access="read")
        old = self.runtime._capture_registry("u")
        self.runtime.install_world("u", self.fixture(read, state_authorizer=lambda *args: False))
        with patch.object(self.runtime, "_capture_registry", return_value=old):
            with self.assertRaises(WorldVersionMismatch):
                self.runtime.query_function("u", "number", self.role, {})

    def test_long_poll_keeps_world_actions_and_timer_worker_available(self):
        polled = threading.Event()

        def read(ctx, args):
            return FunctionOutcome(ctx.get_state("shared", "number"))

        def arm(ctx, args):
            ctx.schedule_timer("next", "finish", {}, due_at=ctx.now)
            return FunctionOutcome({"scheduled": True})

        def finish(ctx, args):
            ctx.set_state("shared", "number", {"value": 2})
            return FunctionOutcome({"finished": True}, (EventSpec(self.role, "finished", {"value": 2}),))

        definition = replace(self.fixture(read, write=arm), timers=(TimerSpec("finish", finish, EMPTY),))
        self.runtime.install_world("u", definition)
        original = self.runtime.read_changes

        def observing(*args, **kwargs):
            rows = original(*args, **kwargs)
            polled.set()
            return rows

        with patch.object(self.runtime, "read_changes", side_effect=observing), ThreadPoolExecutor(max_workers=2) as pool:
            reader = pool.submit(self.runtime.wait_changes, "u", self.role, timeout=3)
            self.assertTrue(polled.wait(1))
            pool.submit(self.runtime.call_function, "u", "write", self.role, {}, operation_id="arm").result(timeout=1)
            result = pool.submit(self.runtime.run_due_timers, "u").result(timeout=1)
            self.assertEqual(result["count"], 1)
            self.assertEqual(result["processed"][0]["status"], "completed")
            self.assertEqual(reader.result(timeout=2)[0]["payload"], {"value": 2})

    def test_timeline_rechecks_both_view_gates_after_concurrent_access_closes(self):
        for gate_kind in ("visible_to", "authorize"):
            with self.subTest(gate=gate_kind):
                entered, release = threading.Event(), threading.Event()
                pause = {"enabled": False}

                def read(ctx, args):
                    return FunctionOutcome({"read": True})

                def close(ctx, args):
                    ctx.set_state("shared", "stable", {"value": 0})
                    return FunctionOutcome({"closed": True})

                def gate(ctx, *unused):
                    return ctx.get_state("shared", "stable")["value"] == 7

                def project(ctx, args):
                    if pause["enabled"]:
                        entered.set()
                        if not release.wait(3):
                            raise RuntimeError("test timeline projection was not released")
                    return {"entities": {"notice": {"present": True}}}

                def publish(ctx, args):
                    cue = PresentationCue("private-timeline-cue", "notice", "speech", "start", "line",
                                          {"text": "must not return after the gate closes"})
                    return FunctionOutcome({}, (cue.event(ctx.actor_role_id),))

                initial = self.fixture(read, write=close)
                definition = replace(initial, functions=(*initial.functions, FunctionSpec("publish", publish, EMPTY)),
                    views=(ViewSpec("timeline", project, timeline=True, **{gate_kind: gate}),))
                universe = gate_kind
                self.runtime.install_world(universe, definition)
                before = self.runtime.view_snapshot(universe, self.role, "timeline")
                self.runtime.call_function(universe, "publish", self.role, {}, operation_id="publish")
                pause["enabled"] = True
                with ThreadPoolExecutor(max_workers=2) as pool:
                    timeline = pool.submit(self.runtime.view_timeline, universe, self.role, before["cursor"])
                    try:
                        self.assertTrue(entered.wait(1))
                        pool.submit(self.runtime.call_function, universe, "write", self.role, {},
                                    operation_id="close").result(timeout=1)
                    finally:
                        release.set()
                    with self.assertRaises(PermissionDenied):
                        timeline.result(timeout=2)
                with self.runtime._conn(readonly=True) as connection:
                    self.assertEqual(connection.execute("SELECT COUNT(*) FROM view_checkpoints WHERE universe=?",
                                                       (universe,)).fetchone()[0], 1)

    def test_same_version_callback_publication_cannot_mix_old_allow_with_new_state(self):
        captured, release_capture = threading.Event(), threading.Event()
        committed, release_publish = threading.Event(), threading.Event()
        beginning = threading.Event()
        calls = []

        def read(ctx, args):
            calls.append("old handler ran")
            return FunctionOutcome(ctx.get_state("shared", "number"))

        def write(ctx, args):
            ctx.set_state("shared", "number", {"value": 2})
            return FunctionOutcome({"written": True})

        initial = self.fixture(read, write=write)
        self.runtime.install_world("u", initial)
        # Another legitimate Runtime models a separate process. Its writes
        # can commit after the install SQL transaction but before this host's
        # callback publication; process-local writer locks cannot fence them.
        writer_runtime = WorldRuntime(self.runtime.db_path)
        self.addCleanup(lambda: getattr(writer_runtime, "close", lambda: None)())
        writer_runtime.install_world("u", initial)
        updated = replace(initial, functions=tuple(
            replace(spec, authorize=lambda ctx, args: False) if spec.name == "number" else spec
            for spec in initial.functions))
        self.assertEqual(initial.manifest(), updated.manifest())
        capture, publish, begin = (self.runtime._capture_registry,
                                  self.runtime._publish_registry, self.runtime._begin_registry_read)

        def delayed_capture(universe):
            registry = capture(universe)
            captured.set()
            if not release_capture.wait(3):
                raise RuntimeError("test registry capture was not released")
            return registry

        def delayed_publish(*args, **kwargs):
            committed.set()
            if not release_publish.wait(3):
                raise RuntimeError("test registry publication was not released")
            return publish(*args, **kwargs)

        def starting(*args):
            beginning.set()
            return begin(*args)

        with patch.object(self.runtime, "_capture_registry", side_effect=delayed_capture), \
             patch.object(self.runtime, "_publish_registry", side_effect=delayed_publish), \
             patch.object(self.runtime, "_begin_registry_read", side_effect=starting), \
             ThreadPoolExecutor(max_workers=3) as pool:
            reader = pool.submit(self.runtime.query_function, "u", "number", self.role, {})
            installer = None
            try:
                self.assertTrue(captured.wait(1))
                installer = pool.submit(self.runtime.install_world, "u", updated)
                self.assertTrue(committed.wait(1))
                pool.submit(writer_runtime.call_function, "u", "write", self.role, {},
                            operation_id="new-fact").result(timeout=1)
                release_capture.set()
                self.assertTrue(beginning.wait(1))
                with self.assertRaises(TimeoutError):
                    reader.result(timeout=.1)
                self.assertEqual(calls, [])
            finally:
                release_capture.set()
                release_publish.set()
            installer.result(timeout=2)
            with self.assertRaises(WorldVersionMismatch):
                reader.result(timeout=2)
        self.assertEqual(calls, [])
        with self.assertRaises(PermissionDenied):
            self.runtime.query_function("u", "number", self.role, {})


if __name__ == "__main__":
    unittest.main()
