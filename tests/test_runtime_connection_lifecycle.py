"""Transaction isolation and host connection cleanup boundaries."""
import sqlite3
import tempfile
import threading
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch
from agent_world.runtime_core import WorldRuntime

class ConnectionLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.directory=tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.runtime=WorldRuntime(Path(self.directory.name)/'world.sqlite3')
        self.addCleanup(self.runtime.close)
        # These failure probes exercise the overflow close path. Pool reuse is
        # verified independently below; no warm handle bypasses their fake.
        self.runtime._connection_idle_limit=0
        with self.runtime._conn():pass

    def connection(self, *, fail_commit=False, fail_rollback=False, slow_close=None):
        runtime=self.runtime
        class Connection:
            in_transaction=False
            stages=[]
            def execute(self,sql):return self
            def set_authorizer(self,guard):pass
            def commit(self):
                self.stages.append(('commit',runtime._lock._is_owned()))
                if fail_commit:raise sqlite3.OperationalError('commit failed')
                self.in_transaction=False
            def rollback(self):
                self.stages.append(('rollback',runtime._lock._is_owned()))
                if fail_rollback:raise sqlite3.OperationalError('rollback failed')
                self.in_transaction=False
            def close(self):
                self.stages.append(('close',runtime._lock._is_owned()))
                self.in_transaction=False
                if slow_close:slow_close()
        return Connection()

    def test_commit_and_rollback_keep_lock_but_successful_close_releases_it(self):
        for failure in [False,True]:
            connection=self.connection(fail_commit=failure)
            with patch('agent_world.runtime_core.sqlite3.connect',return_value=connection):
                if failure:
                    with self.assertRaises(sqlite3.OperationalError):
                        with self.runtime._conn(serialized=True) as c:c.in_transaction=True
                else:
                    with self.runtime._conn(serialized=True) as c:c.in_transaction=True
            self.assertEqual(connection.stages,[('commit',True),*([('rollback',True)] if failure else []),('close',False)])

    def test_rollback_failure_closes_active_transaction_under_lock(self):
        connection=self.connection(fail_commit=True,fail_rollback=True)
        with patch('agent_world.runtime_core.sqlite3.connect',return_value=connection):
            with self.assertRaises(sqlite3.OperationalError):
                with self.runtime._conn(serialized=True) as c:c.in_transaction=True
        self.assertEqual(connection.stages,[('commit',True),('rollback',True),('close',True)])

    def test_slow_nontransaction_close_does_not_block_next_runtime_request(self):
        closing=threading.Event();release=threading.Event()
        def slow_close():closing.set();release.wait(2)
        connection=self.connection(slow_close=slow_close)
        def run():
            with self.runtime._conn(serialized=True) as c:c.in_transaction=True
        with patch('agent_world.runtime_core.sqlite3.connect',return_value=connection):
            worker=threading.Thread(target=run);worker.start()
            try:
                self.assertTrue(closing.wait(2))
                acquired=self.runtime._lock.acquire(timeout=.2)
                if acquired:self.runtime._lock.release()
                self.assertTrue(acquired)
            finally:release.set();worker.join(2)
        self.assertFalse(worker.is_alive())


class ConnectionPoolTests(unittest.TestCase):
    def setUp(self):
        directory=tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.runtime=WorldRuntime(Path(directory.name)/'world.sqlite3')
        self.addCleanup(self.runtime.close)

    def test_reuses_separate_read_write_handles_with_full_durability(self):
        with self.runtime._conn() as writer:
            self.assertEqual(writer.execute('PRAGMA synchronous').fetchone()[0],2)
            self.assertEqual(writer.execute('PRAGMA foreign_keys').fetchone()[0],1)
        with self.runtime._conn() as repeated:
            self.assertIs(repeated,writer)
        with self.runtime._conn(readonly=True) as reader:
            self.assertIsNot(reader,writer)
            with self.assertRaises(sqlite3.OperationalError):
                reader.execute('CREATE TABLE denied (n)')
        with self.runtime._conn(readonly=True) as repeated:
            self.assertIs(repeated,reader)

    def test_failed_request_rolls_back_and_resets_authorizer_before_reuse(self):
        with self.runtime._conn() as c:
            c.execute('CREATE TABLE sample (value INTEGER)')
        with self.assertRaisesRegex(ValueError,'request failed'):
            with self.runtime._conn(serialized=True) as failed:
                failed.execute('BEGIN IMMEDIATE')
                failed.execute('INSERT INTO sample VALUES (1)')
                failed.set_authorizer(lambda action,*args: sqlite3.SQLITE_DENY
                                      if action==sqlite3.SQLITE_READ else sqlite3.SQLITE_OK)
                raise ValueError('request failed')
        with self.runtime._conn() as reused:
            self.assertIs(reused,failed)
            self.assertFalse(reused.in_transaction)
            self.assertEqual(reused.execute('SELECT value FROM sample').fetchall(),[])

    def test_each_read_lease_has_its_own_fresh_snapshot(self):
        with self.runtime._conn() as c:
            c.execute('CREATE TABLE sample (value INTEGER)')
            c.execute('INSERT INTO sample VALUES (0)')
        with self.runtime._conn(readonly=True) as reader:
            reader.execute('BEGIN')
            self.assertEqual(reader.execute('SELECT value FROM sample').fetchone()[0],0)
            with self.runtime._conn(serialized=True) as writer:
                writer.execute('BEGIN IMMEDIATE')
                writer.execute('UPDATE sample SET value=1')
            self.assertEqual(reader.execute('SELECT value FROM sample').fetchone()[0],0)
        with self.runtime._conn(readonly=True) as reused:
            self.assertIs(reused,reader)
            self.assertFalse(reused.in_transaction)
            self.assertEqual(reused.execute('SELECT value FROM sample').fetchone()[0],1)

    def test_checkout_is_exclusive_and_idle_storage_is_bounded(self):
        with ExitStack() as stack:
            handles=[stack.enter_context(self.runtime._conn())
                     for _ in range(self.runtime._connection_idle_limit+2)]
            self.assertEqual(len({id(c) for c in handles}),len(handles))
        idle=self.runtime._idle_connections[False]
        self.assertEqual(len(idle),self.runtime._connection_idle_limit)
        closed=[c for c in handles if c not in idle]
        for c in closed:
            with self.assertRaises(sqlite3.ProgrammingError):c.execute('SELECT 1')

    def test_idle_handle_can_be_leased_by_another_thread(self):
        with self.runtime._conn() as original:pass
        result=[];errors=[]
        def read():
            try:
                with self.runtime._conn() as c:
                    result.append((c,c.execute('SELECT 1').fetchone()[0]))
            except BaseException as exc:errors.append(exc)
        thread=threading.Thread(target=read);thread.start();thread.join(2)
        self.assertFalse(thread.is_alive());self.assertEqual(errors,[])
        self.assertEqual(result,[(original,1)])

    def test_opening_handle_does_not_hold_pool_lock(self):
        entered=threading.Event();release=threading.Event();writer_done=threading.Event()
        original=self.runtime._open_connection;errors=[]
        def open_connection(readonly):
            if readonly:
                entered.set();release.wait(2)
            return original(readonly)
        def read():
            try:
                with self.runtime._conn(readonly=True):pass
            except BaseException as exc:errors.append(exc)
        def write():
            try:
                with self.runtime._conn():pass
                writer_done.set()
            except BaseException as exc:errors.append(exc)
        with patch.object(self.runtime,'_open_connection',side_effect=open_connection):
            reader=threading.Thread(target=read);reader.start()
            writer=None
            try:
                self.assertTrue(entered.wait(1))
                writer=threading.Thread(target=write);writer.start()
                self.assertTrue(writer_done.wait(1))
            finally:
                release.set();reader.join(2)
                if writer is not None:writer.join(2)
        self.assertEqual(errors,[])

    def test_close_drains_idle_and_closes_inflight_only_after_release(self):
        with self.runtime._conn() as inflight:
            with self.runtime._conn(readonly=True) as idle:pass
            self.runtime.close()
            self.runtime.close()
            with self.assertRaises(sqlite3.ProgrammingError):idle.execute('SELECT 1')
            self.assertEqual(inflight.execute('SELECT 1').fetchone()[0],1)
            with self.assertRaisesRegex(RuntimeError,'closed'):
                with self.runtime._conn():pass
        with self.assertRaises(sqlite3.ProgrammingError):inflight.execute('SELECT 1')
        self.assertEqual(self.runtime._idle_connections,{False:[],True:[]})

    def test_failed_schema_initialization_releases_handle(self):
        handles=[]
        def fail(runtime):
            with runtime._conn() as c:
                handles.append(c)
                c.execute('BEGIN IMMEDIATE')
                raise ValueError('bad schema')
        with patch.object(WorldRuntime,'_init_db',fail):
            with self.assertRaisesRegex(ValueError,'bad schema'):
                WorldRuntime(self.runtime.db_path)
        with self.assertRaises(sqlite3.ProgrammingError):handles[0].execute('SELECT 1')
