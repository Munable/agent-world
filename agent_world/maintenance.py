"""Optional host lifecycle maintenance, independent of timer execution."""
from __future__ import annotations
import argparse
import asyncio
from contextlib import asynccontextmanager, closing
import logging
import threading
import time

logger = logging.getLogger(__name__)


class _WalBackfillSchedule:
    """Yield to queued writes, but retry pinned/continuously busy WALs.

    The deadline grants a maintenance opportunity; it cannot bound the IO
    duration of PASSIVE or force a reader to release its snapshot.
    """
    def __init__(self, max_defer=5.0):
        self.max_defer = max_defer
        self.pending_since = None
        self.last_status = None
        self.last_attempt = None

    def ready(self, status, *, writer_busy, now):
        if status is None:
            # SQLite hosts without NOOP retain the existing maintenance path.
            return True
        if not status["pending_frames"]:
            self.pending_since = None
            return False
        if self.pending_since is None:
            self.pending_since = now
        signature = (status["wal_frames"], status["checkpointed_frames"])
        if (signature == self.last_status and self.last_attempt is not None
                and now - self.last_attempt < self.max_defer):
            return False
        return not writer_busy or now - self.pending_since >= self.max_defer

    def completed(self, status, *, now):
        self.last_status = (status["wal_frames"], status["checkpointed_frames"])
        self.last_attempt = now
        self.pending_since = now if status["pending_frames"] else None


@asynccontextmanager
async def retention_lifespan(runtime, universe):
    stop = threading.Event()
    with runtime.managed_wal_checkpoints():
        def run():
            next_retention = 0
            backfill = _WalBackfillSchedule()
            try:
                while not stop.is_set():
                    try:
                        status = runtime.wal_checkpoint_status()
                        writer = runtime._lock.snapshot()
                        if (not stop.is_set() and backfill.ready(status,
                                writer_busy=writer["active"] or bool(writer["waiting"]), now=time.monotonic())):
                            result = runtime.passive_wal_checkpoint()
                            backfill.completed(result, now=time.monotonic())
                    except Exception as exc:
                        logger.error("WAL backfill failed (%s); durable WAL retained", type(exc).__name__)
                    if time.monotonic() >= next_retention and not stop.is_set():
                        try:
                            definition = runtime._worlds.get(universe)
                            if definition is not None and definition.retention is not None:
                                runtime.apply_retention(universe)
                            if definition is not None and definition.streams:
                                runtime.apply_stream_retention(universe)
                        except Exception as exc:
                            logger.error("retention sweep failed (%s)", type(exc).__name__)
                        next_retention = time.monotonic() + 30
                    stop.wait(1)
            except BaseException as exc:
                logger.critical("host maintenance stopped (%s); restoring SQLite automatic checkpoints", type(exc).__name__)
            finally:
                runtime.restore_auto_wal_checkpoints()
        # Reuse the existing host-owned maintenance thread. PASSIVE may take
        # real disk time, but neither request commits nor timers execute it.
        thread = threading.Thread(target=run, name="agent-world-maintenance", daemon=True)
        thread.start()
        try:
            yield
        finally:
            stop.set()
            await asyncio.to_thread(thread.join)


def main():
    import json
    from .runtime_core import WorldRuntime
    from .universe_loader import get_universe_installer
    parser = argparse.ArgumentParser(description="Apply one bounded world retention sweep")
    parser.add_argument("--world", required=True)
    parser.add_argument("--universe", required=True)
    parser.add_argument("--db", required=True)
    parser.add_argument("--limit", type=int, default=500)
    args = parser.parse_args()
    with closing(WorldRuntime(args.db)) as runtime:
        get_universe_installer(args.world)(runtime, args.universe)
        print(json.dumps(runtime.apply_retention(args.universe, limit=args.limit)))


if __name__ == "__main__":
    main()
