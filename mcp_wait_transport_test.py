from __future__ import annotations

import asyncio
import json
import time

from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

MCP_URL = "http://127.0.0.1:8766/mcp"


def structured(result):
    value = getattr(result, "structured_content", None)
    if not isinstance(value, dict):
        raise AssertionError("missing structured content")
    if getattr(result, "is_error", False):
        raise AssertionError(value)
    return value


async def main():
    observations = {}

    async with streamable_http_client(MCP_URL) as (r1, w1):
        async with ClientSession(r1, w1) as s1:
            await s1.initialize()

            async with streamable_http_client(MCP_URL) as (r2, w2):
                async with ClientSession(r2, w2) as s2:
                    await s2.initialize()

                    started = time.monotonic()
                    waiting = asyncio.create_task(
                        s1.call_tool(
                            "world.wait_changes",
                            arguments={
                                "role_id": "MCP_WAIT_ROLE",
                                "after": 0,
                                "timeout": 5.0,
                                "limit": 20,
                            },
                            read_timeout_seconds=10,
                        )
                    )
                    await asyncio.sleep(0.75)
                    send = structured(
                        await s2.call_tool(
                            "counter.increment",
                            arguments={
                                "role_id": "MCP_WAIT_ROLE",
                                "operation_id": "mcp-wait-wake-1",
                                "arguments": {"amount": 1},
                            },
                        )
                    )
                    waited = structured(await waiting)
                    elapsed = round(time.monotonic() - started, 3)
                    assert waited["events"]
                    assert waited["events"][-1]["seq"] in send["event_seqs"]
                    assert elapsed < 2.0
                    observations["wake_wait_seconds"] = elapsed

                    cursor = waited["next_cursor"]
                    started = time.monotonic()
                    timeout_result = structured(
                        await s1.call_tool(
                            "world.wait_changes",
                            arguments={
                                "role_id": "MCP_WAIT_ROLE",
                                "after": cursor,
                                "timeout": 0.4,
                                "limit": 20,
                            },
                            read_timeout_seconds=3,
                        )
                    )
                    timeout_elapsed = round(time.monotonic() - started, 3)
                    assert timeout_result["timed_out"] is True
                    assert 0.30 <= timeout_elapsed <= 1.5
                    observations["empty_wait_seconds"] = timeout_elapsed

                    cancel_task = asyncio.create_task(
                        s1.call_tool(
                            "world.wait_changes",
                            arguments={
                                "role_id": "MCP_CANCEL_ROLE",
                                "after": 0,
                                "timeout": 5.0,
                            },
                            read_timeout_seconds=10,
                        )
                    )
                    await asyncio.sleep(0.25)
                    cancel_task.cancel()
                    cancelled = False
                    try:
                        await cancel_task
                    except asyncio.CancelledError:
                        cancelled = True
                    assert cancelled
                    await asyncio.sleep(0.15)

                    # A cancelled wait must not poison the MCP session/server.
                    boot = structured(
                        await s1.call_tool(
                            "world.bootstrap",
                            arguments={"role_id": "MCP_CANCEL_ROLE"},
                        )
                    )
                    assert boot["role_id"] == "MCP_CANCEL_ROLE"
                    observations["cancelled_wait_session_survived"] = True

    print(json.dumps(observations, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
