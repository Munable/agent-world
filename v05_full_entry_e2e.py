from __future__ import annotations

import asyncio
import json
import os
import pathlib
import subprocess
import sys
import tempfile
import time

import httpx
from mcp import ClientSession
from agent_world.runtime_contracts import CORE_TOOL_NAMES
from mcp.client.streamable_http import streamable_http_client


ROOT = pathlib.Path(__file__).resolve().parent
ONBOARD_PORT = 8825
MCP_PORT = 8826
ONBOARD = f"http://127.0.0.1:{ONBOARD_PORT}"
MCP_URL = f"http://127.0.0.1:{MCP_PORT}/mcp"
OPERATOR = "v05-e2e-operator"


def start_server(module: str, port: int, env: dict[str, str], log_name: str):
    log = (ROOT / log_name).open("w", encoding="utf-8")
    proc = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "uvicorn",
            f"{module}:app",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
            "--log-level",
            "warning",
        ],
        cwd=ROOT,
        env=env,
        stdout=log,
        stderr=subprocess.STDOUT,
    )
    return proc, log
def wait_ready(url: str, expected: set[int]) -> None:
    for _ in range(80):
        try:
            response = httpx.get(url, timeout=0.25, trust_env=False)
            if response.status_code in expected:
                return
        except Exception:
            pass
        time.sleep(0.1)
    raise AssertionError(f"server did not become ready: {url}")


async def mcp_phase(token: str, role_id: str) -> dict:
    async with httpx.AsyncClient(
        headers={"Authorization": f"Bearer {token}"},
        timeout=10.0,
        trust_env=False,
    ) as http_client:
        async with streamable_http_client(
            MCP_URL,
            http_client=http_client,
        ) as (read_stream, write_stream):
            async with ClientSession(read_stream, write_stream) as session:
                init = await session.initialize()
                tools = await session.list_tools()
                by_name = {tool.name: tool for tool in tools.tools}
                assert set(by_name) == CORE_TOOL_NAMES | {'activity.score.add', 'counter.increment', 'counter.get'}
                assert "role_id" not in by_name["world.bootstrap"].input_schema["properties"]
                assert "role_id" not in by_name["counter.increment"].input_schema["properties"]
                assert "operation_id" not in by_name["counter.get"].input_schema["properties"]
                boot = await session.call_tool("world.bootstrap", arguments={})
                assert not boot.is_error
                boot_body = boot.structured_content
                assert boot_body["role_id"] == role_id
                assert boot_body["role_profile"]["display_name"] == "E2E Walker"

                mutation = await session.call_tool(
                    "counter.increment",
                    arguments={
                        "operation_id": "v05-entry-op-1",
                        "arguments": {"amount": 5},
                    },
                )
                assert not mutation.is_error
                assert mutation.structured_content["result"]["value"] == 5

                changes = await session.call_tool(
                    "world.get_changes",
                    arguments={"after": 0},
                )
                assert not changes.is_error
                events = changes.structured_content["events"]
                assert events[-1]["payload"]["value"] == 5
                return {
                    "protocol": str(init.protocol_version),
                    "tool_count": len(by_name),
                    "latest_seq": events[-1]["seq"],
                }
async def recovery_phase(token: str, role_id: str, expected_seq: int) -> None:
    async with httpx.AsyncClient(
        headers={"Authorization": f"Bearer {token}"},
        timeout=10.0,
        trust_env=False,
    ) as http_client:
        async with streamable_http_client(
            MCP_URL,
            http_client=http_client,
        ) as (read_stream, write_stream):
            async with ClientSession(read_stream, write_stream) as session:
                await session.initialize()
                boot = await session.call_tool("world.bootstrap", arguments={})
                assert not boot.is_error
                assert boot.structured_content["role_id"] == role_id
                assert boot.structured_content["latest_event_seq"] >= expected_seq
                changes = await session.call_tool(
                    "world.get_changes",
                    arguments={"after": 0},
                )
                assert not changes.is_error
                events = changes.structured_content["events"]
                assert any(
                    event["seq"] == expected_seq
                    and event["payload"].get("value") == 5
                    for event in events
                )
async def main() -> None:
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        db = pathlib.Path(tmp) / "full-entry.sqlite3"
        common = os.environ.copy()
        common["WORLD_DB"] = str(db)
        common["WORLD_UNIVERSE"] = "demo"

        onboard_env = common.copy()
        onboard_env["WORLD_OPERATOR_KEY"] = OPERATOR
        onboard_env["WORLD_MCP_PUBLIC_URL"] = MCP_URL
        onboard_env["WORLD_PUBLIC_BASE_URL"] = ONBOARD

        mcp_env = common.copy()
        mcp_env["WORLD_AUTH_REQUIRED"] = "1"
        mcp_env["WORLD_HOST"] = "127.0.0.1"

        onboard_proc, onboard_log = start_server(
            "onboarding_app", ONBOARD_PORT, onboard_env, "v05_e2e_onboarding.log"
        )
        mcp_proc, mcp_log = start_server(
            "mcp_app", MCP_PORT, mcp_env, "v05_e2e_mcp.log"
        )
        try:
            wait_ready(ONBOARD + "/health", {200})
            wait_ready(MCP_URL, {401})

            op_headers = {"X-Operator-Key": OPERATOR}
            role_response = httpx.post(
                ONBOARD + "/v1/roles",
                headers=op_headers,
                json={"display_name": "E2E Walker", "avatar_ref": "avatar://e2e"},
                timeout=5,
                trust_env=False,
            )
            role_response.raise_for_status()
            role = role_response.json()
            package_response = httpx.post(
                ONBOARD + "/v1/join-tickets",
                headers=op_headers,
                json={"role_id": role["role_id"], "ttl_seconds": 60},
                timeout=5,
                trust_env=False,
            )
            package_response.raise_for_status()
            package = package_response.json()
            ticket = package["join"]["ticket"]

            # Simulate "server committed the exchange but the caller lost the reply":
            lost = httpx.post(
                ONBOARD + "/v1/join/exchange",
                json={"ticket": ticket},
                timeout=5,
                trust_env=False,
            )
            lost.raise_for_status()
            # Deliberately ignore lost.json().

            recovered = httpx.post(
                ONBOARD + "/v1/join/exchange",
                json={"ticket": ticket},
                timeout=5,
                trust_env=False,
            )
            recovered.raise_for_status()
            identity_package = recovered.json()
            assert identity_package["replayed"] is True
            token = identity_package["identity"]["token"]

            first = await mcp_phase(token, role["role_id"])
            await recovery_phase(token, role["role_id"], first["latest_seq"])

            print(
                json.dumps(
                    {
                        "role_id": role["role_id"],
                        "join_exchange_recovered": True,
                        "mcp_protocol": first["protocol"],
                        "tool_count": first["tool_count"],
                        "mutation_value": 5,
                        "fresh_session_recovery": True,
                    },
                    ensure_ascii=False,
                )
            )
        finally:
            for proc in (mcp_proc, onboard_proc):
                proc.terminate()
            for proc in (mcp_proc, onboard_proc):
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(timeout=5)
            mcp_log.close()
            onboard_log.close()


if __name__ == "__main__":
    asyncio.run(main())
