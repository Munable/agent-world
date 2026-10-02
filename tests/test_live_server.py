from __future__ import annotations

import asyncio
import gc
from pathlib import Path
import threading
import subprocess
import unittest
from unittest.mock import Mock, patch

from tests.live_server import LiveServer


class PausedServer(LiveServer):
    """Pause lifecycle work without opening a service or using shared data."""
    def __init__(self):
        super().__init__()
        self.starting = threading.Event()
        self.release_start = threading.Event()
        self.stopping = threading.Event()
        self.release_stop = threading.Event()

    def start(self):
        self.starting.set()
        if not self.release_start.wait(5):
            raise RuntimeError("test did not release startup")
        self.log.write("synthetic fixture startup\n")

    def stop(self, **kwargs):
        self.stopping.set()
        if not self.release_stop.wait(5):
            raise RuntimeError("test did not release shutdown")


class LiveServerLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def test_async_lifecycle_keeps_loop_responsive_and_cleans_owned_files(self):
        server = PausedServer()

        async def lifecycle():
            async with server:
                self.assertTrue(server.log_path.exists())

        task = asyncio.create_task(lifecycle())
        try:
            self.assertTrue(await asyncio.to_thread(server.starting.wait, 5))
            self.assertFalse(task.done())
            server.release_start.set()
            self.assertTrue(await asyncio.to_thread(server.stopping.wait, 5))
            self.assertFalse(task.done())
            server.release_stop.set()
            await task
        finally:
            server.release_start.set()
            server.release_stop.set()
            await asyncio.gather(task, return_exceptions=True)
        self.assertTrue(server.log.closed)
        self.assertFalse(Path(server.temp.name).exists())

    async def test_cancelled_startup_waits_for_owned_cleanup(self):
        server = PausedServer()
        server.release_stop.set()
        task = asyncio.create_task(server.__aenter__())
        try:
            self.assertTrue(await asyncio.to_thread(server.starting.wait, 5))
            task.cancel()
            await asyncio.sleep(0)
            self.assertFalse(task.done())
            server.release_start.set()
            with self.assertRaises(asyncio.CancelledError):
                await task
        finally:
            server.release_start.set()
            await asyncio.gather(task, return_exceptions=True)
        self.assertTrue(server.log.closed)
        self.assertFalse(Path(server.temp.name).exists())

    async def test_cancelled_failed_startup_keeps_unreaped_process_diagnostics(self):
        server = PausedServer()
        original = OSError("synthetic startup failure")
        cleanup_failure = subprocess.TimeoutExpired("synthetic process", 5)
        proc = Mock(pid=12346)
        proc.poll.return_value = None
        proc.wait.side_effect = cleanup_failure

        def failed_start():
            server.proc = proc
            server.starting.set()
            if not server.release_start.wait(5):
                raise RuntimeError("test did not release startup")
            server.log.write("synthetic failed startup\n")
            raise original

        with patch.object(server, "start", side_effect=failed_start), \
                patch.object(server, "stop", side_effect=lambda: LiveServer.stop(server)), \
                patch("tests.live_server.subprocess.run"):
            task = asyncio.create_task(server.__aenter__())
            try:
                self.assertTrue(await asyncio.to_thread(server.starting.wait, 5))
                task.cancel("caller stopped startup")
                await asyncio.sleep(0)
                server.release_start.set()
                with self.assertRaises(asyncio.CancelledError) as caught:
                    await task
                self.assertTrue(task.cancelled())
                self.assertEqual(caught.exception.args, ("caller stopped startup",))
                self.assertIs(caught.exception.__cause__, cleanup_failure)
                self.assertIs(cleanup_failure.__cause__, original)
                notes = "\n".join(caught.exception.__notes__)
                self.assertIn("synthetic failed startup", notes)
                self.assertIn("12346", notes)
                self.assertIn(server.temp.name, notes)
                self.assertTrue(server.log_path.exists())
                self.assertEqual([call.kwargs for call in proc.wait.call_args_list], [{"timeout": 5}, {"timeout": 5}])
            finally:
                server.release_start.set()
                await asyncio.gather(task, return_exceptions=True)
                # The PID is synthetic; there is no process using these files.
                server.temp.cleanup()

    async def test_body_failure_keeps_server_log_on_original_exception(self):
        server = PausedServer()
        server.release_start.set()
        server.release_stop.set()
        failure = RuntimeError("synthetic transport failure")
        with self.assertRaises(RuntimeError) as caught:
            async with server:
                raise failure
        self.assertIs(caught.exception, failure)
        self.assertIn("synthetic fixture startup", "\n".join(failure.__notes__))
        self.assertFalse(Path(server.temp.name).exists())

    async def test_launch_failure_preserves_cause_and_cleans_owned_files(self):
        server = LiveServer()
        failure = OSError("synthetic process launch failure")
        with patch("tests.live_server.subprocess.Popen", side_effect=failure):
            with self.assertRaises(OSError) as caught:
                async with server:
                    self.fail("failed launch entered the context")
        self.assertIs(caught.exception, failure)
        self.assertIn("Owned test server log", "\n".join(failure.__notes__))
        self.assertTrue(server.log.closed)
        self.assertFalse(Path(server.temp.name).exists())

    async def test_port_reservation_failure_cleans_directory_before_launch(self):
        server = LiveServer()
        failure = OSError("synthetic bind failure")
        with patch("tests.live_server.socket.socket") as sock:
            sock.return_value.__enter__.return_value.bind.side_effect = failure
            with self.assertRaises(OSError) as caught:
                async with server:
                    self.fail("failed port reservation entered the context")
        self.assertIs(caught.exception, failure)
        self.assertIsNone(server.proc)
        self.assertIsNone(server.log)
        self.assertFalse(Path(server.temp.name).exists())

    async def test_unreaped_process_keeps_directory_without_a_deletion_finalizer(self):
        server = PausedServer()
        server.release_start.set()
        await server.__aenter__()
        failure = subprocess.TimeoutExpired("synthetic process", 5)
        proc = Mock(pid=12345)
        proc.poll.return_value = None
        proc.wait.side_effect = failure
        server.proc = proc
        directory = server.temp.name
        original = RuntimeError("synthetic body failure")
        try:
            with patch.object(server, "stop", side_effect=lambda: LiveServer.stop(server)), \
                    patch("tests.live_server.subprocess.run"):
                with self.assertRaises(subprocess.TimeoutExpired) as caught:
                    await server.__aexit__(type(original), original, None)
            self.assertIs(caught.exception, failure)
            self.assertIs(failure.__cause__, original)
            self.assertEqual(proc.wait.call_count, 2)
            self.assertEqual([call.kwargs for call in proc.wait.call_args_list], [{"timeout": 5}, {"timeout": 5}])
            self.assertTrue(server.log_path.exists())
            self.assertIn(server.temp.name, "\n".join(failure.__notes__))
            self.assertIn("12345", "\n".join(failure.__notes__))
            owned = server.temp
            server.temp = None
            del server
            del owned
            gc.collect()
            self.assertTrue(Path(directory).exists())
        finally:
            # This fixture has no actual process; the test owns this exact path.
            from shutil import rmtree
            rmtree(directory)


if __name__ == "__main__":
    unittest.main()
