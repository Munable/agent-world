"""Disposable SQLite recovery drills, not a production backup service or soak test."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
import json
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

from agent_world import (
    EventSpec, FunctionOutcome, FunctionSpec, StateRule, StreamEvent, StreamSpec,
    TimerSpec, WorldDefinition, WorldRuntime,
)
from agent_world.errors import ReceiptNotFound, WorldVersionMismatch
from tests.capability_harness import RuntimeCapabilityHarness

ROOT = Path(__file__).resolve().parents[1]
EMPTY = {"type": "object", "properties": {}, "additionalProperties": False}
ARGS = {
    "type": "object",
    "properties": {"item_id": {"type": "string"}, "recipient": {"type": "string"}},
    "required": ["item_id", "recipient"],
    "additionalProperties": False,
}


def store_item(ctx, args):
    value = {"item_id": args["item_id"], "owner": ctx.actor_role_id, "finished": False}
    ctx.set_state("items", args["item_id"], value)
    ctx.schedule_timer(args["item_id"], "finish", args, due_at=0)
    return FunctionOutcome(value, (
        EventSpec(args["recipient"], "item.stored", value),
        StreamEvent("items", "item.stored", value),
    ))


def finish_item(ctx, args):
    value = ctx.get_state("items", args["item_id"])
    value["finished"] = True
    ctx.set_state("items", args["item_id"], value)
    return FunctionOutcome(value, (EventSpec(args["recipient"], "item.finished", value),))


RECOVERY_WORLD = WorldDefinition(
    "storage-recovery", "Storage recovery fixture",
    (FunctionSpec("item.store", store_item, ARGS),),
    state_rules=(StateRule("items", "", {"type": "object"}),),
    streams=(StreamSpec("items", public=True),),
    timers=(TimerSpec("finish", finish_item, ARGS),),
)


def migration_world(version=1):
    def initialize(ctx):
        ctx.set_state("migration", "count", 1)

    def second(ctx):
        ctx.set_state("migration", "count", ctx.get_state("migration", "count") + 10)
        ctx.schedule_timer("migration-done", "noop", {}, due_at=0)

    def third(ctx):
        ctx.set_state("migration", "count", ctx.get_state("migration", "count") * 2)

    def read(ctx, args):
        return FunctionOutcome({"count": ctx.get_state("migration", "count")})

    return WorldDefinition(
        "migration-recovery", "Migration recovery fixture",
        (FunctionSpec("count.read", read, EMPTY, access="read", version=version),),
        version=version, state_version=version,
        initialize=initialize, migrations={2: second, 3: third},
        state_rules=(StateRule("migration", "count", {"type": "integer"}),),
        timers=(TimerSpec("noop", lambda c, a: FunctionOutcome({}), EMPTY),),
    )


def inspect_restored_backup(path, data):
    """Run only in the fresh test subprocess; all credentials are synthetic."""
    w = WorldRuntime(path)
    w.install_world("u", RECOVERY_WORLD)
    receipt = w.get_receipt("u", data["actor"], "committed", identity_token=data["token"])
    replay = w.call_function("u", "item.store", data["actor"], data["args"],
                             operation_id="committed", identity_token=data["token"])
    events = w.read_changes_page("u", data["recipient"], identity_token=data["recipient_token"])
    stream = w.read_stream("u", "items", cursor=data["stream_cursor"])
    before = w.get_state("u", "items", "committed")
    absent = w.get_state("u", "items", "after-backup")
    catchup = w.run_due_timers("u")
    again = w.run_due_timers("u")
    resumed = w.read_changes_page("u", data["recipient"], events["next_cursor"],
                                  identity_token=data["recipient_token"])
    return {"receipt": receipt, "replay": replay, "events": events, "stream": stream,
            "before": before, "absent": absent, "catchup": catchup, "again": again,
            "after": w.get_state("u", "items", "committed"), "resumed": resumed,
            "timer_receipt": w.get_timer_receipt("u", "committed")}


class StorageRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "source.sqlite3"

    def run_worker(self, code, path=None, payload=None, expected_code=0):
        result = subprocess.run(
            [sys.executable, "-c", code, str(path or self.path)],
            input=json.dumps(payload) if payload is not None else None,
            cwd=ROOT, text=True, capture_output=True, timeout=30,
        )
        self.assertEqual(result.returncode, expected_code, result.stdout + result.stderr)
        return result.stdout

    def backup(self, source, destination):
        # SQLite's online backup API includes committed WAL pages. Copying the
        # live main file alone does not establish the same recovery point.
        with closing(sqlite3.connect(Path(source).as_uri() + "?mode=ro", uri=True)) as src:
            with closing(sqlite3.connect(destination)) as dst:
                src.backup(dst)
                self.assertEqual(dst.execute("PRAGMA integrity_check").fetchall(), [("ok",)])
                self.assertEqual(dst.execute("PRAGMA foreign_key_check").fetchall(), [])

    def database_dump(self, path=None):
        with closing(sqlite3.connect(path or self.path)) as c:
            return c.execute("PRAGMA user_version").fetchone()[0], list(c.iterdump())

    def make_harness(self):
        h = RuntimeCapabilityHarness(self.path, RECOVERY_WORLD, universe="u")
        alice, bob = h.spawn(2)
        return h, alice, bob

    def test_online_wal_backup_restores_identity_receipts_events_stream_and_due_timer(self):
        """A self-contained backup must work without the source WAL or process."""
        h, alice, bob = self.make_harness()
        stream_cursor = h.runtime.read_stream("u", "items")["cursor"]
        args = {"item_id": "committed", "recipient": bob.role_id}
        backup = Path(self.temp.name) / "backup.sqlite3"
        restored = Path(self.temp.name) / "restored.sqlite3"
        # Pin an earlier read snapshot so committed pages remain in the WAL.
        with closing(sqlite3.connect(self.path, isolation_level=None)) as reader:
            reader.execute("BEGIN")
            reader.execute("SELECT COUNT(*) FROM operations").fetchone()
            receipt = h.call(alice, "item.store", args, "committed")
            self.assertGreater(Path(str(self.path) + "-wal").stat().st_size, 32)
            self.backup(self.path, backup)
            h.call(alice, "item.store", {"item_id": "after-backup", "recipient": bob.role_id},
                   "after-backup")
        shutil.copyfile(backup, restored)
        result = json.loads(self.run_worker(
            "import json,sys; from tests.test_storage_recovery import inspect_restored_backup; "
            "print(json.dumps(inspect_restored_backup(sys.argv[1], json.load(sys.stdin))))",
            restored, {"actor": alice.role_id, "token": alice.token, "recipient": bob.role_id,
                       "recipient_token": bob.token, "args": args, "stream_cursor": stream_cursor},
        ))
        self.assertEqual(result["receipt"], receipt)
        self.assertEqual(result["replay"], {**receipt, "replayed": True})
        self.assertEqual([e["seq"] for e in result["events"]["events"]], receipt["event_seqs"])
        self.assertEqual([e["event_id"] for e in result["stream"]["events"]], receipt["stream_event_ids"])
        self.assertFalse(result["before"]["value"]["finished"])
        self.assertFalse(result["absent"]["exists"])
        self.assertEqual(result["catchup"]["count"], 1)
        self.assertEqual(result["again"]["count"], 0)
        self.assertTrue(result["after"]["value"]["finished"])
        self.assertEqual([e["kind"] for e in result["resumed"]["events"]], ["item.finished"])
        self.assertEqual(result["timer_receipt"]["timer_id"], "committed")
        restored_h = RuntimeCapabilityHarness(restored, RECOVERY_WORLD, universe="u")
        reentered = restored_h.reenter(alice)
        self.assertEqual(reentered.role_id, alice.role_id)
        self.assertFalse(reentered.created_profile)
        self.assertEqual(h.runtime.get_timer("u", "committed")["status"], "pending")
        self.assertFalse(h.runtime.get_state("u", "items", "committed")["value"]["finished"])

    def test_online_backup_excludes_a_fully_staged_uncommitted_command(self):
        """No partial state, receipt, event, stream or timer leaks into a backup."""
        h, alice, bob = self.make_harness()
        baseline = self.database_dump()
        backup = Path(self.temp.name) / "during-write.sqlite3"
        staged, release = threading.Event(), threading.Event()
        original = h.runtime._commit_outcome_tx

        def pause_before_commit(*args, **kwargs):
            receipt = original(*args, **kwargs)
            staged.set()
            if not release.wait(10):
                raise RuntimeError("test did not release staged command")
            return receipt

        with patch.object(h.runtime, "_commit_outcome_tx", side_effect=pause_before_commit):
            with ThreadPoolExecutor(1) as pool:
                future = pool.submit(h.call, alice, "item.store",
                                     {"item_id": "pending", "recipient": bob.role_id}, "pending")
                try:
                    self.assertTrue(staged.wait(10), "writer never reached the pre-commit boundary")
                    self.backup(self.path, backup)
                    self.assertEqual(self.database_dump(backup), baseline)
                finally:
                    release.set()
                self.assertTrue(future.result(timeout=10)["ok"])
        restored = WorldRuntime(backup)
        restored.install_world("u", RECOVERY_WORLD)
        self.assertFalse(restored.get_state("u", "items", "pending")["exists"])
        with self.assertRaises(ReceiptNotFound):
            restored.get_receipt("u", alice.role_id, "pending", identity_token=alice.token)
        self.assertEqual(restored.run_due_timers("u")["count"], 0)
        self.assertEqual(restored.read_stream("u", "items")["events"], [])
        self.assertEqual(restored.read_changes("u", bob.role_id, identity_token=bob.token), [])
        self.assertTrue(h.runtime.get_state("u", "items", "pending")["exists"])

    def test_process_exit_before_migration_commit_preserves_old_world_and_allows_retry(self):
        """All ordered migration steps and the target install share one commit."""
        w = WorldRuntime(self.path)
        w.install_world("u", migration_world())
        role = w.create_role("Migration reader")["role_id"]
        baseline = self.database_dump()
        output = self.run_worker(
            "import os,sys; from agent_world import WorldRuntime; "
            "from tests.test_storage_recovery import migration_world; "
            "w=WorldRuntime(sys.argv[1]); original=w._record_commit_tx\n"
            "def crash(*args,**kwargs):\n"
            " original(*args,**kwargs)\n"
            " print('STAGED_MIGRATION_COMMIT', flush=True)\n"
            " os._exit(73)\n"
            "w._record_commit_tx=crash\n"
            "w.install_world('u', migration_world(3))\n",
            expected_code=73,
        )
        self.assertIn("STAGED_MIGRATION_COMMIT", output)
        self.assertEqual(self.database_dump(), baseline)
        self.assertEqual(w.query_function("u", "count.read", role, {})["result"], {"count": 1})
        recovered = WorldRuntime(self.path)
        recovered.install_world("u", migration_world())
        recovered.install_world("u", migration_world(3))
        self.assertEqual(recovered.query_function("u", "count.read", role, {})["result"], {"count": 22})
        self.assertEqual(recovered.get_world_manifest("u")["state_version"], 3)
        self.assertEqual(recovered.get_function("u", "count.read")["version"], 3)
        with self.assertRaises(WorldVersionMismatch):
            w.query_function("u", "count.read", role, {})
        recovered.install_world("u", migration_world(3))
        with closing(sqlite3.connect(self.path)) as c:
            self.assertEqual(c.execute("SELECT COUNT(*) FROM world_commits WHERE source='migration'").fetchone()[0], 1)
            self.assertEqual(c.execute("SELECT COUNT(*) FROM timer_transitions").fetchone()[0], 1)
        self.assertEqual(recovered.run_due_timers("u")["count"], 1)
        self.assertEqual(recovered.run_due_timers("u")["count"], 0)

    def test_process_exit_during_schema_creation_rolls_back_ddl_and_can_reinitialize(self):
        """A process exit must not publish half a schema or rotate persisted secrets."""
        output = self.run_worker(
            "import os,sys; from agent_world import WorldRuntime\n"
            "class CrashingRuntime(WorldRuntime):\n"
            " def _init_streams_tx(self,c):\n"
            "  super()._init_streams_tx(c)\n"
            "  print('STAGED_SCHEMA', flush=True)\n"
            "  os._exit(74)\n"
            "CrashingRuntime(sys.argv[1])\n",
            expected_code=74,
        )
        self.assertIn("STAGED_SCHEMA", output)
        with closing(sqlite3.connect(self.path)) as c:
            self.assertEqual(c.execute("PRAGMA user_version").fetchone()[0], 0)
            self.assertEqual(c.execute("SELECT name FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'").fetchall(), [])
        w = WorldRuntime(self.path)
        w.install_world("u", migration_world())
        baseline = self.database_dump()
        WorldRuntime(self.path)
        self.assertEqual(self.database_dump(), baseline)
        with closing(sqlite3.connect(self.path)) as c:
            self.assertEqual(c.execute("PRAGMA integrity_check").fetchall(), [("ok",)])
            self.assertEqual(c.execute("PRAGMA foreign_key_check").fetchall(), [])


if __name__ == "__main__":
    unittest.main()
