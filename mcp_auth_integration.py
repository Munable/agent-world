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

from agent_world.runtime_core import WorldRuntime


ROOT = pathlib.Path(__file__).resolve().parent
PORT = 8776
URL = f"http://127.0.0.1:{PORT}/mcp"


async def call(session: ClientSession, name: str, arguments: dict):
    result = await session.call_tool(name, arguments=arguments)
    return result, getattr(result, "structured_content", None)


async def main() -> None:
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        db = pathlib.Path(tmp) / "mcp-auth.sqlite3"
        runtime = WorldRuntime(db)
        issued = runtime.issue_identity_token("demo", "MCP-ROLE-A", ttl_seconds=60)
        token = issued["token"]

        env = os.environ.copy()
        env["WORLD_DB"] = str(db)
        env["WORLD_UNIVERSE"] = "demo"
        env["WORLD_AUTH_REQUIRED"] = "1"
        env["WORLD_HOST"] = "127.0.0.1"
        log_path = ROOT / "mcp_auth_server.log"
        log = log_path.open("w", encoding="utf-8")
        proc = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "uvicorn",
                "mcp_app:app",
                "--host",
                "127.0.0.1",
                "--port",
                str(PORT),
                "--log-level",
                "warning",
            ],
            cwd=ROOT,
            env=env,
            stdout=log,
            stderr=subprocess.STDOUT,
        )
        try:
            ready = False
            for _ in range(60):
                try:
                    r = httpx.get(URL, timeout=0.2, trust_env=False)
                    if r.status_code == 401:
                        ready = True
                        break
                except Exception:
                    pass
                time.sleep(0.1)
            if not ready:
                details = log_path.read_text(encoding="utf-8", errors="replace")[-4000:]
                raise AssertionError(f"authenticated MCP server did not become ready; exit={proc.poll()}; log: {details}")

            async with httpx.AsyncClient(
                trust_env=False,
                headers={"Authorization": f"Bearer {token}"},
                timeout=5.0,
            ) as http_client:
                async with streamable_http_client(
                    URL, http_client=http_client
                ) as (read_stream, write_stream):
                    async with ClientSession(read_stream, write_stream) as session:
                        init = await session.initialize()
                        tools = await session.list_tools()
                        names = {tool.name for tool in tools.tools}
                        assert "world.bootstrap" in names
                        assert "counter.increment" in names
                        assert "counter.get" in names
                        by_name = {tool.name: tool for tool in tools.tools}
                        assert set(by_name) == CORE_TOOL_NAMES | {'activity.score.add', 'counter.increment', 'counter.get'}
                        assert "role_id" not in by_name["world.bootstrap"].input_schema["properties"]
                        assert "role_id" not in by_name["counter.increment"].input_schema["properties"]
                        assert "role_id" not in by_name["counter.get"].input_schema["properties"]
                        assert "operation_id" in by_name["counter.increment"].input_schema["properties"]
                        assert "operation_id" not in by_name["counter.get"].input_schema["properties"]

                        bootstrap, body = await call(
                            session,
                            "world.bootstrap",
                            {},
                        )
                        assert not bootstrap.is_error, body
                        assert body["role_id"] == "MCP-ROLE-A"

                        spoof, spoof_body = await call(
                            session,
                            "world.bootstrap",
                            {"role_id": "MCP-ROLE-B"},
                        )
                        assert spoof.is_error
                        assert spoof_body["error"] == "IdentityScopeMismatch"

                        invoke, invoke_body = await call(
                            session,
                            "counter.increment",
                            {
                                "operation_id": "mcp-auth-op-1",
                                "arguments": {"amount": 4},
                            },
                        )
                        assert not invoke.is_error, invoke_body
                        assert invoke_body["result"]["value"] == 4

                        read_result, read_body = await call(
                            session,
                            "counter.get",
                            {"arguments": {}},
                        )
                        assert not read_result.is_error, read_body
                        assert read_body["result"]["value"] == 4
                        assert read_body["read_only"] is True

                        changes, changes_body = await call(
                            session,
                            "world.get_changes",
                            {"after": 0},
                        )
                        assert not changes.is_error, changes_body
                        assert changes_body["events"]
                        assert changes_body["events"][-1]["payload"]["value"] == 4

                        print(
                            json.dumps(
                                {
                                    "protocol_version": str(init.protocol_version),
                                    "tool_count": len(tools.tools),
                                    "role_bound": True,
                                    "role_omitted_from_auth_tool_schema": True,
                                    "read_omits_operation_id": True,
                                    "spoof_rejected": True,
                                    "mutation_value": 4,
                                    "read_value": 4,
                                },
                                ensure_ascii=False,
                            )
                        )
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=5)
            log.close()


if __name__ == "__main__":
    asyncio.run(main())
