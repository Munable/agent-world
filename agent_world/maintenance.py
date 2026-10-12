"""Optional host lifecycle maintenance, independent of timer execution."""
from __future__ import annotations
import argparse
import asyncio
from contextlib import asynccontextmanager, closing
import logging
import threading
import time

logger = logging.getLogger(__name__)


@asynccontextmanager
async def retention_lifespan(runtime, universe):
    stop = threading.Event()
    with runtime.managed_wal_checkpoints():
        def run():
            next_retention = 0
            try:
                while not stop.is_set():
                    try:
                        runtime.passive_wal_checkpoint()
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
