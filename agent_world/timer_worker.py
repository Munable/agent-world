"""Host-side timer driver. No Agent session or model call is needed."""
from __future__ import annotations

import argparse
import asyncio
import logging
import threading
import time
from contextlib import asynccontextmanager

from .runtime_contracts import duration, integer

logger = logging.getLogger(__name__)


class TimerWorker:
    def __init__(self, runtime, universe, *, interval=1.0, batch_size=25):
        self.runtime, self.universe = runtime, universe
        self.interval = duration(interval, "timer polling interval", 60)
        if self.interval < 0.05:
            raise ValueError("timer polling interval must be at least 0.05 seconds")
        self.batch_size = integer(batch_size, "timer batch size", 1, 100)
        self._stop = threading.Event()
        self._thread = None
        self._next_checkpoint_retention = time.monotonic() + 30

    def start(self):
        if self._thread is not None:
            raise RuntimeError("timer worker is already started")
        self._thread = threading.Thread(target=self._run, name="agent-world-timers", daemon=True)
        self._thread.start()
        return self

    def _run(self):
        while not self._stop.is_set():
            delay = min(self.interval, 0.1)
            try:
                due_at = self._run_ready_slice()
                delay = self.interval if due_at is None else max(0.01, min(self.interval, due_at - time.time()))
            except Exception as exc:
                # Do not print arbitrary rule arguments, error messages or credentials.
                logger.error("timer sweep failed (%s); pending work retained", type(exc).__name__)
            except BaseException as exc:
                self._stop.set()
                logger.critical("timer worker stopped (%s); pending work retained", type(exc).__name__)
                return
            self._stop.wait(delay)

    def _run_ready_slice(self):
        # Drain immediately ready work without another full polling interval.
        # Both count and elapsed-time bounds prevent an endless catch-up loop;
        # each Runtime task still commits independently and releases its lock.
        deadline = time.monotonic() + 0.1
        remaining = 100
        due_at = None
        for _ in range(4):
            if self._stop.is_set():
                break
            limit = min(self.batch_size, remaining)
            result = self.runtime.run_due_timers(self.universe, limit=limit, deadline=deadline)
            remaining -= limit
            self._maintain_checkpoint_history(result.get("checkpoint_work", {}))
            due_at = self.runtime.next_due_work(self.universe)
            if (remaining <= 0 or due_at is None or due_at > time.time()
                    or time.monotonic() >= deadline):
                break
        return due_at

    def _maintain_checkpoint_history(self, work):
        definition = self.runtime._worlds.get(self.universe)
        if definition is None or not definition.checkpoints:
            return
        now = time.monotonic()
        commits = work.get("commits", 0)
        if not commits and now < self._next_checkpoint_retention:
            return
        # Reuse the host worker and size cleanup from actual production. The
        # slower idle sweep catches up after restart; world handlers never GC
        # another transaction's history. Streams release references first.
        self._next_checkpoint_retention = now + 30
        stream_budget = 2 * work.get("stream_events", 0) + 8
        # A managed outcome has at most 128 events and a sweep at most 100
        # commits, so six API-sized passes cover the largest legal production.
        for _ in range(6):
            limit = min(5000, stream_budget)
            self.runtime.apply_stream_retention(self.universe, limit=limit)
            stream_budget -= limit
            if stream_budget <= 0:
                break
        self.runtime.apply_checkpoint_retention(self.universe,
            limit=min(256, 2 * commits + 8),
            change_limit=min(106496, 2 * work.get("changes", 0) + 4096))

    def stop(self):
        self._stop.set()
        if self._thread is not None:
            self._thread.join()  # Let an in-flight short rule commit/roll back before shutdown.


@asynccontextmanager
async def timer_lifespan(runtime, universe, *, enabled=True, interval=1.0):
    worker = TimerWorker(runtime, universe, interval=interval).start() if enabled else None
    try:
        yield worker
    finally:
        if worker is not None:
            await asyncio.to_thread(worker.stop)


def main():
    from .runtime_core import WorldRuntime
    from .universe_loader import get_universe_installer

    parser = argparse.ArgumentParser(description="Process durable world timers with a matching trusted world package.")
    parser.add_argument("--world", required=True)
    parser.add_argument("--universe", required=True)
    parser.add_argument("--db", required=True)
    parser.add_argument("--once", action="store_true", help="process one bounded batch and exit")
    parser.add_argument("--limit", type=int, default=25)
    parser.add_argument("--interval", type=float, default=1.0)
    args = parser.parse_args()
    runtime = WorldRuntime(args.db)
    get_universe_installer(args.world)(runtime, args.universe)
    if args.once:
        import json
        print(json.dumps(runtime.run_due_timers(args.universe, limit=args.limit)))
        return
    worker = TimerWorker(runtime, args.universe, interval=args.interval, batch_size=args.limit).start()
    try:
        while worker._thread.is_alive():
            worker._thread.join(timeout=1)
        raise RuntimeError("timer worker terminated; pending work retained")
    except KeyboardInterrupt:
        worker.stop()


if __name__ == "__main__":
    main()
