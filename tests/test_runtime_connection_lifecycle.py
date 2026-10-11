"""Transaction isolation and host connection cleanup boundaries."""
import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch
from agent_world.runtime_core import WorldRuntime

class ConnectionLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.directory=tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.runtime=WorldRuntime(Path(self.directory.name)/'world.sqlite3')

    def connection(self, *, fail_commit=False, fail_rollback=False, slow_close=None):
        runtime=self.runtime
        class Connection:
            in_transaction=False
            stages=[]
            def execute(self,sql):return self
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
