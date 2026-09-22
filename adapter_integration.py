from __future__ import annotations

import asyncio
import json
import os
import threading
import time
from typing import Any

import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

HTTP_BASE = os.getenv("WORLD_HTTP_BASE", "http://127.0.0.1:8765")
MCP_URL = os.getenv("WORLD_MCP_URL", "http://127.0.0.1:8766/mcp")


def expect_http_ok(response: httpx.Response) -> dict[str, Any]:
    if response.status_code >= 400:
        raise AssertionError(
            f"HTTP {response.status_code}: {response.text[:1000]}"
        )
    return response.json()


async def mcp_call(
    session: ClientSession, name: str, arguments: dict[str, Any]
) -> dict[str, Any]:
    result = await session.call_tool(name, arguments=arguments)
    if getattr(result, "is_error", False):
        raise AssertionError(
            f"MCP tool {name} failed: {getattr(result, 'structured_content', None)}"
        )
    structured = getattr(result, "structured_content", None)
    if not isinstance(structured, dict):
        raise AssertionError(f"MCP tool {name} returned no structured content")
    return structured


async def main() -> None:
    observations: dict[str, Any] = {}
    suffix = str(int(time.time() * 1000))
    role_a = "A-" + suffix
    role_b = "B-" + suffix
    role_c = "C-" + suffix
    role_d = "D-" + suffix
    activity_adapter = "job-adapter-" + suffix
    activity_fence = "job-fence-" + suffix
    with httpx.Client(timeout=5.0) as http:
        health = expect_http_ok(http.get(HTTP_BASE + "/health"))
        assert health["ok"] is True

        functions = expect_http_ok(http.get(HTTP_BASE + "/v1/functions"))
        ids = {f["function_id"] for f in functions["functions"]}
        assert {"counter.increment", "activity.score.add"} <= ids
        observations["http_function_ids"] = sorted(ids)

        async with streamable_http_client(MCP_URL) as (read_stream, write_stream):
            async with ClientSession(read_stream, write_stream) as session:
                init = await session.initialize()
                observations["mcp_protocol_version"] = init.protocol_version

                tool_result = await session.list_tools()
                tools = {tool.name: tool for tool in tool_result.tools}
                assert "counter.increment" in tools
                assert "activity.score.add" in tools
                assert "world.bootstrap" in tools
                counter_schema = tools["counter.increment"].input_schema
                assert counter_schema["properties"]["arguments"]["required"] == ["amount"]
                assert (
                    tools["activity.score.add"]
                    .input_schema["required"]
                    .count("activity_claim")
                    == 1
                )
                observations["mcp_tool_count"] = len(tools)

                h1 = expect_http_ok(
                    http.post(
                        HTTP_BASE + "/v1/functions/counter.increment/invoke",
                        json={
                            "role_id": role_a,
                            "operation_id": "http-op-1-" + suffix,
                            "expected_version": 1,
                            "arguments": {"amount": 2},
                        },
                    )
                )
                assert h1["result"]["value"] == 2

                m1 = await mcp_call(
                    session,
                    "counter.increment",
                    {
                        "role_id": role_a,
                        "operation_id": "mcp-op-1-" + suffix,
                        "expected_version": 1,
                        "arguments": {"amount": 3},
                    },
                )
                assert m1["result"]["value"] == 5

                # Same logical operation retried through the other adapter.
                cross_retry = expect_http_ok(
                    http.post(
                        HTTP_BASE + "/v1/functions/counter.increment/invoke",
                        json={
                            "role_id": role_a,
                            "operation_id": "mcp-op-1-" + suffix,
                            "expected_version": 1,
                            "arguments": {"amount": 3},
                        },
                    )
                )
                assert cross_retry["replayed"] is True
                assert cross_retry["result"]["value"] == 5
                observations["cross_adapter_idempotency"] = True

                activity = await mcp_call(
                    session,
                    "world.start_activity",
                    {
                        "role_id": role_a,
                        "activity_id": activity_adapter,
                        "kind": "research",
                        "exclusive_group": "work",
                        "ttl_seconds": 5,
                    },
                )
                assert activity["activity_id"] == activity_adapter

                claim = expect_http_ok(
                    http.post(
                        HTTP_BASE + f"/v1/activities/{activity_adapter}/claim",
                        json={
                            "role_id": role_a,
                            "runtime_id": "runtime-mcp-" + suffix,
                            "lease_seconds": 2,
                        },
                    )
                )
                score = await mcp_call(
                    session,
                    "activity.score.add",
                    {
                        "role_id": role_a,
                        "operation_id": "score-1-" + suffix,
                        "expected_version": 1,
                        "arguments": {"delta": 7},
                        "activity_claim": claim,
                    },
                )
                assert score["result"]["score"] == 7
                observations["cross_adapter_claimed_function"] = True


                # Cross-process wait: correctness is required even though the
                # current prototype has no shared low-latency notifier.
                wait_result: dict[str, Any] = {}

                def waiter():
                    started = time.monotonic()
                    r = httpx.get(
                        HTTP_BASE + "/v1/changes/wait",
                        params={
                            "role_id": role_b,
                            "after": 0,
                            "timeout": 0.5,
                            "limit": 20,
                        },
                        timeout=2.0,
                    )
                    wait_result["elapsed"] = round(
                        time.monotonic() - started, 3
                    )
                    wait_result["body"] = expect_http_ok(r)

                thread = threading.Thread(target=waiter)
                thread.start()
                time.sleep(0.10)
                await mcp_call(
                    session,
                    "counter.increment",
                    {
                        "role_id": role_b,
                        "operation_id": "wake-b-" + suffix,
                        "arguments": {"amount": 1},
                    },
                )
                thread.join(timeout=2)
                assert not thread.is_alive()
                assert wait_result["body"]["events"]
                observations["cross_process_wait_elapsed"] = wait_result["elapsed"]

                # Lease expiry + takeover. Old proof must be fenced before any
                # world-state mutation.
                expect_http_ok(
                    http.post(
                        HTTP_BASE + "/v1/activities",
                        json={
                            "role_id": role_c,
                            "activity_id": activity_fence,
                            "kind": "research",
                            "exclusive_group": "work",
                            "ttl_seconds": 5,
                        },
                    )
                )
                old_claim = expect_http_ok(
                    http.post(
                        HTTP_BASE + f"/v1/activities/{activity_fence}/claim",
                        json={
                            "role_id": role_c,
                            "runtime_id": "runtime-old-" + suffix,
                            "lease_seconds": 0.15,
                        },
                    )
                )
                time.sleep(0.20)
                new_claim = await mcp_call(
                    session,
                    "world.claim_activity",
                    {
                        "role_id": role_c,
                        "activity_id": activity_fence,
                        "runtime_id": "runtime-new-" + suffix,
                        "lease_seconds": 2,
                    },
                )
                assert new_claim["claim_epoch"] == old_claim["claim_epoch"] + 1

                stale = http.post(
                    HTTP_BASE + "/v1/functions/activity.score.add/invoke",
                    json={
                        "role_id": role_c,
                        "operation_id": "stale-score-" + suffix,
                        "arguments": {"delta": 9},
                        "activity_claim": old_claim,
                    },
                )
                assert stale.status_code == 409
                assert stale.json()["error"] == "ClaimFenced"

                fresh = await mcp_call(
                    session,
                    "activity.score.add",
                    {
                        "role_id": role_c,
                        "operation_id": "fresh-score-" + suffix,
                        "arguments": {"delta": 9},
                        "activity_claim": new_claim,
                    },
                )
                assert fresh["result"]["score"] == 9
                observations["stale_claim_fenced_cross_adapter"] = True

        # Request cancellation / timeout after business commit:
        # the retry must return the original receipt rather than execute twice.
        timed_out = False
        try:
            with httpx.Client(timeout=0.2) as impatient:
                impatient.post(
                    HTTP_BASE + "/v1/functions/counter.increment/invoke",
                    headers={"X-Test-Response-Delay-Ms": "1000"},
                    json={
                        "role_id": role_d,
                        "operation_id": "timeout-after-commit-" + suffix,
                        "arguments": {"amount": 4},
                    },
                )
        except httpx.TimeoutException:
            timed_out = True
        assert timed_out
        time.sleep(1.05)

        retry = expect_http_ok(
            http.post(
                HTTP_BASE + "/v1/functions/counter.increment/invoke",
                json={
                    "role_id": role_d,
                    "operation_id": "timeout-after-commit-" + suffix,
                    "arguments": {"amount": 4},
                },
            )
        )
        assert retry["replayed"] is True
        assert retry["result"]["value"] == 4
        observations["timeout_after_commit_recovered"] = True

    print(json.dumps(observations, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
