"""Optional host lifecycle maintenance, independent of timer execution."""
from __future__ import annotations
import argparse
import asyncio
from contextlib import asynccontextmanager
import logging
import threading

logger = logging.getLogger(__name__)


@asynccontextmanager
async def retention_lifespan(runtime, universe):
    definition = runtime._worlds.get(universe)
    stop = threading.Event()
    thread = None
    if definition is not None and (definition.retention is not None or definition.streams):
        def run():
            while not stop.is_set():
                try:
                    runtime.apply_retention(universe)
                    runtime.apply_stream_retention(universe)
                except Exception as exc:
                    logger.error("retention sweep failed (%s)", type(exc).__name__)
                stop.wait(30)
        thread = threading.Thread(target=run, name="agent-world-retention", daemon=True)
        thread.start()
    try:
        yield
    finally:
        stop.set()
        if thread is not None:
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
    runtime = WorldRuntime(args.db)
    get_universe_installer(args.world)(runtime, args.universe)
    print(json.dumps(runtime.apply_retention(args.universe, limit=args.limit)))


if __name__ == "__main__":
    main()
