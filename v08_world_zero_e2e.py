from __future__ import annotations

import asyncio
import json
import os
import pathlib
import sqlite3
import subprocess
import sys
import tempfile
import time

import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client


ROOT = pathlib.Path(__file__).resolve().parent
WEB_PORT = 8855
MCP_PORT = 8856
WEB = f"http://127.0.0.1:{WEB_PORT}"
MCP_URL = f"http://127.0.0.1:{MCP_PORT}/mcp"
WEB_USER = "operator"
WEB_PASSWORD = "world-zero-test"


def start_server(module: str, port: int, env: dict[str, str], log_name: str):
    log = (ROOT / log_name).open("w", encoding="utf-8")
    proc = subprocess.Popen(
        [
            sys.executable, "-m", "uvicorn", f"{module}:app",
            "--host", "127.0.0.1", "--port", str(port),
            "--log-level", "warning",
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
            response = httpx.get(url, timeout=0.25)
            if response.status_code in expected:
                return
        except Exception:
            pass
        time.sleep(0.1)
    raise AssertionError(f"server did not become ready: {url}")
def create_role_and_token(
    name: str,
    auth: httpx.BasicAuth,
) -> tuple[dict, str, str]:
    role_response = httpx.post(
        WEB + "/api/roles",
        auth=auth,
        json={"display_name": name},
        timeout=5,
    )
    role_response.raise_for_status()
    role = role_response.json()

    package_response = httpx.post(
        WEB + f"/api/roles/{role['role_id']}/join-package",
        auth=auth,
        json={"ttl_seconds": 60},
        timeout=5,
    )
    package_response.raise_for_status()
    package = package_response.json()
    prompt = package["agent_instructions"]
    assert "world.list_places" in prompt
    assert "world.observe" in prompt
    assert "world.visit" in prompt
    assert "world.leave_mark" in prompt

    ticket = prompt.split("Join ticket: ", 1)[1].splitlines()[0].strip()
    exchange = httpx.post(
        WEB + "/v1/join/exchange",
        json={"ticket": ticket},
        timeout=5,
    )
    exchange.raise_for_status()
    token = exchange.json()["identity"]["token"]
    return role, token, prompt


async def open_session(token: str):
    http_client = httpx.AsyncClient(
        headers={"Authorization": f"Bearer {token}"},
        timeout=10,
    )
    transport = streamable_http_client(MCP_URL, http_client=http_client)
    read_stream, write_stream = await transport.__aenter__()
    session = ClientSession(read_stream, write_stream)
    await session.__aenter__()
    init = await session.initialize()
    return http_client, transport, session, init


async def close_session(http_client, transport, session):
    await session.__aexit__(None, None, None)
    await transport.__aexit__(None, None, None)
    await http_client.aclose()


async def call(session: ClientSession, name: str, arguments: dict):
    result = await session.call_tool(name, arguments=arguments)
    if result.is_error:
        raise AssertionError(f"{name} failed: {result.structured_content}")
    return result.structured_content
async def main() -> None:
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        db = pathlib.Path(tmp) / "world-zero.sqlite3"
        common = os.environ.copy()
        common["WORLD_DB"] = str(db)
        common["WORLD_UNIVERSE"] = "world-zero"

        web_env = common.copy()
        web_env["WORLD_WEB_USER"] = WEB_USER
        web_env["WORLD_WEB_PASSWORD"] = WEB_PASSWORD
        web_env["WORLD_PUBLIC_BASE_URL"] = WEB
        web_env["WORLD_MCP_PUBLIC_URL"] = MCP_URL

        mcp_env = common.copy()
        mcp_env["WORLD_PROFILE"] = "world-zero"
        mcp_env["WORLD_AUTH_REQUIRED"] = "1"
        mcp_env["WORLD_HOST"] = "127.0.0.1"

        web_proc, web_log = start_server(
            "product_app", WEB_PORT, web_env, "v08_world_zero_web.log"
        )
        mcp_proc, mcp_log = start_server(
            "mcp_app", MCP_PORT, mcp_env, "v08_world_zero_mcp.log"
        )

        try:
            wait_ready(WEB + "/health", {200})
            wait_ready(MCP_URL, {401})
            auth = httpx.BasicAuth(WEB_USER, WEB_PASSWORD)

            role_a, token_a, _ = create_role_and_token("Lantern", auth)
            role_b, token_b, _ = create_role_and_token("Moss", auth)

            http_a, transport_a, session_a, init_a = await open_session(token_a)
            tools = await session_a.list_tools()
            by_name = {tool.name: tool for tool in tools.tools}
            assert len(by_name) == 9
            assert "operation_id" not in by_name["world.list_places"].input_schema["properties"]
            assert "operation_id" not in by_name["world.observe"].input_schema["properties"]
            assert "operation_id" in by_name["world.visit"].input_schema["properties"]
            assert "operation_id" in by_name["world.leave_mark"].input_schema["properties"]

            boot_a = await call(session_a, "world.bootstrap", {})
            assert boot_a["role_profile"]["display_name"] == "Lantern"
            places_a = await call(session_a, "world.list_places", {"arguments": {}})
            assert places_a["result"]["current_place_id"] == "threshold"
            observed_a0 = await call(
                session_a, "world.observe", {"arguments": {}}
            )
            assert observed_a0["result"]["place"]["place_id"] == "threshold"
            assert observed_a0["result"]["marks"] == []

            visit_a = await call(
                session_a,
                "world.visit",
                {
                    "operation_id": "lantern-visit-garden",
                    "arguments": {"place_id": "garden"},
                },
            )
            assert visit_a["result"]["place"]["place_id"] == "garden"

            mark_a = await call(
                session_a,
                "world.leave_mark",
                {
                    "operation_id": "lantern-mark-one",
                    "arguments": {
                        "mark_id": "lantern-first-light",
                        "text": "A small light was left here.",
                    },
                },
            )
            assert mark_a["result"]["place_id"] == "garden"

            replay_a = await call(
                session_a,
                "world.leave_mark",
                {
                    "operation_id": "lantern-mark-one",
                    "arguments": {
                        "mark_id": "lantern-first-light",
                        "text": "A small light was left here.",
                    },
                },
            )
            assert replay_a["replayed"] is True
            await close_session(http_a, transport_a, session_a)

            status_a = httpx.get(
                WEB + f"/api/roles/{role_a['role_id']}/status",
                auth=auth,
                timeout=5,
            ).json()
            assert status_a["entered_world"] is True
            assert status_a["latest_recipient_event_seq"] > 0
            http_b, transport_b, session_b, _ = await open_session(token_b)
            boot_b = await call(session_b, "world.bootstrap", {})
            assert boot_b["role_profile"]["display_name"] == "Moss"

            await call(
                session_b,
                "world.visit",
                {
                    "operation_id": "moss-visit-garden",
                    "arguments": {"place_id": "garden"},
                },
            )
            observed_b = await call(
                session_b,
                "world.observe",
                {"arguments": {"mark_limit": 10}},
            )
            marks_b = observed_b["result"]["marks"]
            assert len(marks_b) == 1
            assert marks_b[0]["mark_id"] == "lantern-first-light"
            assert marks_b[0]["author_display_name"] == "Lantern"

            await call(
                session_b,
                "world.leave_mark",
                {
                    "operation_id": "moss-reply-one",
                    "arguments": {
                        "mark_id": "moss-reply",
                        "text": "Someone noticed the light and stayed a moment.",
                    },
                },
            )
            await close_session(http_b, transport_b, session_b)

            # Lantern returns with no conversational continuity.
            http_a2, transport_a2, session_a2, _ = await open_session(token_a)
            boot_a2 = await call(session_a2, "world.bootstrap", {})
            assert boot_a2["role_id"] == role_a["role_id"]

            places_a2 = await call(
                session_a2, "world.list_places", {"arguments": {}}
            )
            assert places_a2["result"]["current_place_id"] == "garden"
            observed_a2 = await call(
                session_a2,
                "world.observe",
                {"arguments": {"mark_limit": 10}},
            )
            mark_ids = {m["mark_id"] for m in observed_a2["result"]["marks"]}
            assert mark_ids == {"lantern-first-light", "moss-reply"}
            await close_session(http_a2, transport_a2, session_a2)
            with sqlite3.connect(db) as conn:
                operation_count = conn.execute(
                    "SELECT COUNT(*) FROM operations WHERE universe='world-zero'"
                ).fetchone()[0]
                event_count = conn.execute(
                    "SELECT COUNT(*) FROM events WHERE universe='world-zero'"
                ).fetchone()[0]
                mark_count = conn.execute(
                    """SELECT COUNT(*) FROM world_state
                       WHERE universe='world-zero'
                         AND scope='place:garden'
                         AND state_key LIKE 'mark:%'"""
                ).fetchone()[0]
                location_count = conn.execute(
                    """SELECT COUNT(*) FROM world_state
                       WHERE universe='world-zero'
                         AND state_key='world_zero.location'"""
                ).fetchone()[0]

            assert operation_count == 4
            assert event_count == 4
            assert mark_count == 2
            assert location_count == 2

            print(json.dumps({
                "mcp_protocol": str(init_a.protocol_version),
                "tools": len(by_name),
                "roles": 2,
                "places": 3,
                "durable_marks": mark_count,
                "persistent_locations": location_count,
                "write_operations": operation_count,
                "idempotent_mark_replay": True,
                "later_agent_discovered_prior_mark": True,
                "fresh_session_kept_location": True,
                "fresh_session_discovered_reply": True,
            }, ensure_ascii=False))
        finally:
            for proc in (mcp_proc, web_proc):
                proc.terminate()
            for proc in (mcp_proc, web_proc):
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(timeout=5)
            mcp_log.close()
            web_log.close()


if __name__ == "__main__":
    asyncio.run(main())
