"""Host-side timer driver. No Agent session or model call is needed."""
from __future__ import annotations

import argparse
import asyncio
import logging
import threading
import time
from contextlib import asynccontextmanager, closing

from .runtime_contracts import duration, integer
from .runtime_checkpoints import CHECKPOINT_BATCH_ITEMS

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
        self._maintenance_credit = {"commits": 0, "changes": 0, "stream_events": 0}
        self._maintenance_backlog = False
        self._maintenance_stream_after = None

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
        # each checkpoint batch releases its writer after a bounded opportunity.
        deadline = time.monotonic() + 0.1
        remaining = 100
        due_at = None
        for batch in range(5):
            if self._stop.is_set():
                break
            # Guarantee the first admission even after a slow selection. Its
            # fixed checkpoint page shares one FULL commit; the same deadline
            # still stops later items/batches rather than resetting on waits.
            definition = self.runtime._worlds.get(self.universe)
            checkpoint_world = definition is not None and bool(definition.checkpoints)
            if batch == 0:
                limit = min(CHECKPOINT_BATCH_ITEMS, self.batch_size) if checkpoint_world else 1
            else:
                limit = min(self.batch_size, remaining)
            if batch == 0 and checkpoint_world:
                result = self.runtime.run_due_timers(self.universe, limit=limit, deadline=deadline,
                                                    ensure_initial_work=True)
            else:
                result = (self.runtime.run_due_timers(self.universe, limit=limit) if batch == 0 else
                          self.runtime.run_due_timers(self.universe, limit=limit, deadline=deadline))
            remaining -= limit
            self._credit_checkpoint_work(result.get("checkpoint_work", {}))
            due_at = self.runtime.next_due_work(self.universe)
            if (remaining <= 0 or due_at is None or due_at > time.time()
                    or time.monotonic() >= deadline):
                break
        # Maintenance has its own short budget. It must not be postponed
        # forever by a continuously ready work queue, nor run between every
        # checkpoint batch. Maintenance never joins a world-effect batch.
        self._maintain_checkpoint_history({})
        return due_at

    def _credit_checkpoint_work(self, work):
        for name in self._maintenance_credit:
            self._maintenance_credit[name] += 2 * work.get(name, 0)

    def _maintain_checkpoint_history(self, work):
        definition = self.runtime._worlds.get(self.universe)
        if definition is None or not definition.checkpoints or self._stop.is_set():
            return
        self._credit_checkpoint_work(work)
        now = time.monotonic()
        credit = self._maintenance_credit
        periodic = now >= self._next_checkpoint_retention
        batched = credit["commits"] >= 64 or credit["changes"] >= 4096 or credit["stream_events"] >= 512
        if not periodic and not batched and not self._maintenance_backlog:
            return
        # Keep the production ledger across bounded passes. Recovery allowance
        # also makes progress after restart and when the next whole commit has
        # more changes than the residual credit. Reaching fresh policy windows
        # discharges credit: retaining those rows is not unresolved GC work.
        deadline = now + 0.025
        for _ in range(6):
            result = self.runtime.apply_work_retention(self.universe,
                stream_limit=min(5000, max(8, credit["stream_events"])),
                limit=min(256, max(8, credit["commits"])),
                change_limit=min(106496, max(4096, credit["changes"])),
                time_budget=max(0.001, min(0.025, deadline-time.monotonic())),
                stream_after=self._maintenance_stream_after)
            self._maintenance_stream_after = result["stream_after"]
            stream_backlog = result["stream_backlog"]
            checkpoint_backlog = result["checkpoint_backlog"]
            credit["stream_events"] = (max(0, credit["stream_events"]-result["stream_events_scanned"])
                                       if stream_backlog else 0)
            credit["commits"] = (max(0, credit["commits"]-result["checkpoint_commits_scanned"])
                                 if checkpoint_backlog else 0)
            credit["changes"] = (max(0, credit["changes"]-result["checkpoint_changes_removed"])
                                 if checkpoint_backlog else 0)
            self._maintenance_backlog = stream_backlog or checkpoint_backlog
            if not self._maintenance_backlog or self._stop.is_set() or time.monotonic() >= deadline:
                break
        self._next_checkpoint_retention = time.monotonic() + 30

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
    with closing(WorldRuntime(args.db)) as runtime:
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
            pass
        finally:
            worker.stop()


if __name__ == "__main__":
    main()
