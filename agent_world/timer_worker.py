"""Host-side timer driver. No Agent session or model call is needed."""
from __future__ import annotations

import argparse
import asyncio
import logging
import threading
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

    def start(self):
        if self._thread is not None:
            raise RuntimeError("timer worker is already started")
        self._thread = threading.Thread(target=self._run, name="agent-world-timers", daemon=True)
        self._thread.start()
        return self

    def _run(self):
        while not self._stop.is_set():
            try:
                self.runtime.run_due_timers(self.universe, limit=self.batch_size)
            except Exception as exc:
                # Do not print arbitrary rule arguments, error messages or credentials.
                logger.error("timer sweep failed (%s); pending work retained", type(exc).__name__)
            except BaseException as exc:
                self._stop.set()
                logger.critical("timer worker stopped (%s); pending work retained", type(exc).__name__)
                return
            self._stop.wait(self.interval)

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
