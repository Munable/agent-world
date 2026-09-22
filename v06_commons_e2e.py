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
ONBOARD_PORT = 8835
MCP_PORT = 8836
ONBOARD = f"http://127.0.0.1:{ONBOARD_PORT}"
MCP_URL = f"http://127.0.0.1:{MCP_PORT}/mcp"
OPERATOR = "commons-e2e-operator"


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
            response = httpx.get(url, timeout=0.25)
            if response.status_code in expected:
                return
        except Exception:
            pass
        time.sleep(0.1)
    raise AssertionError(f"server did not become ready: {url}")


def create_role_and_token(name: str, operator_headers: dict[str, str]):
    role_response = httpx.post(
        ONBOARD + "/v1/roles",
        headers=operator_headers,
        json={"display_name": name},
        timeout=5,
    )
    role_response.raise_for_status()
    role = role_response.json()

    ticket_response = httpx.post(
        ONBOARD + "/v1/join-tickets",
        headers=operator_headers,
        json={"role_id": role["role_id"], "ttl_seconds": 60},
        timeout=5,
    )
    ticket_response.raise_for_status()
    ticket = ticket_response.json()["join"]["ticket"]

    exchange = httpx.post(
        ONBOARD + "/v1/join/exchange",
        json={"ticket": ticket},
        timeout=5,
    )
    exchange.raise_for_status()
    token = exchange.json()["identity"]["token"]
    return role, token
async def open_session(token: str):
    http_client = httpx.AsyncClient(
        headers={"Authorization": f"Bearer {token}"},
        timeout=10.0,
    )
    transport = streamable_http_client(MCP_URL, http_client=http_client)
    read_stream, write_stream = await transport.__aenter__()
    session = ClientSession(read_stream, write_stream)
    await session.__aenter__()
    await session.initialize()
    return http_client, transport, session


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
        db = pathlib.Path(tmp) / "commons.sqlite3"
        common = os.environ.copy()
        common["WORLD_DB"] = str(db)
        common["WORLD_UNIVERSE"] = "commons"

        onboard_env = common.copy()
        onboard_env["WORLD_OPERATOR_KEY"] = OPERATOR
        onboard_env["WORLD_MCP_PUBLIC_URL"] = MCP_URL
        onboard_env["WORLD_PUBLIC_BASE_URL"] = ONBOARD

        mcp_env = common.copy()
        mcp_env["WORLD_PROFILE"] = "commons"
        mcp_env["WORLD_AUTH_REQUIRED"] = "1"
        mcp_env["WORLD_HOST"] = "127.0.0.1"
        onboard_proc, onboard_log = start_server(
            "onboarding_app", ONBOARD_PORT, onboard_env, "v06_commons_onboarding.log"
        )
        mcp_proc, mcp_log = start_server(
            "mcp_app", MCP_PORT, mcp_env, "v06_commons_mcp.log"
        )
        try:
            wait_ready(ONBOARD + "/health", {200})
            wait_ready(MCP_URL, {401})
            op_headers = {"X-Operator-Key": OPERATOR}

            role_a, token_a = create_role_and_token("Commons A", op_headers)
            role_b, token_b = create_role_and_token("Commons B", op_headers)

            http_a, transport_a, session_a = await open_session(token_a)
            tools = await session_a.list_tools()
            by_name = {tool.name: tool for tool in tools.tools}
            assert len(by_name) == 8
            assert {
                "commons.board.post",
                "commons.board.list",
                "commons.note.send",
            } <= set(by_name)
            assert "operation_id" in by_name["commons.board.post"].input_schema["properties"]
            assert "operation_id" not in by_name["commons.board.list"].input_schema["properties"]

            boot_a = await call(session_a, "world.bootstrap", {})
            assert boot_a["role_profile"]["display_name"] == "Commons A"

            post_a = await call(
                session_a,
                "commons.board.post",
                {
                    "operation_id": "commons-a-post-1",
                    "arguments": {
                        "post_id": "hello-a",
                        "text": "Hello from role A",
                    },
                },
            )
            assert post_a["result"]["author_role_id"] == role_a["role_id"]
            replay_a = await call(
                session_a,
                "commons.board.post",
                {
                    "operation_id": "commons-a-post-1",
                    "arguments": {
                        "post_id": "hello-a",
                        "text": "Hello from role A",
                    },
                },
            )
            assert replay_a["replayed"] is True

            board_a = await call(
                session_a,
                "commons.board.list",
                {"arguments": {"limit": 10}},
            )
            assert [p["post_id"] for p in board_a["result"]["posts"]] == ["hello-a"]

            await call(
                session_a,
                "commons.note.send",
                {
                    "operation_id": "commons-a-note-b",
                    "arguments": {
                        "recipient_role_id": role_b["role_id"],
                        "text": "A says hello to B",
                    },
                },
            )
            await close_session(http_a, transport_a, session_a)

            http_b, transport_b, session_b = await open_session(token_b)
            boot_b = await call(session_b, "world.bootstrap", {})
            assert boot_b["role_profile"]["display_name"] == "Commons B"
            board_b = await call(
                session_b,
                "commons.board.list",
                {"arguments": {"limit": 10}},
            )
            assert board_b["result"]["posts"][0]["post_id"] == "hello-a"

            changes_b = await call(
                session_b,
                "world.get_changes",
                {"after": 0},
            )
            assert any(
                event["kind"] == "commons_note"
                and event["payload"]["from_role_id"] == role_a["role_id"]
                for event in changes_b["events"]
            )
            await call(
                session_b,
                "commons.board.post",
                {
                    "operation_id": "commons-b-post-1",
                    "arguments": {
                        "post_id": "reply-b",
                        "text": "Role B was here",
                    },
                },
            )
            note_back = await call(
                session_b,
                "commons.note.send",
                {
                    "operation_id": "commons-b-note-a",
                    "arguments": {
                        "recipient_role_id": role_a["role_id"],
                        "text": "B replies to A",
                    },
                },
            )
            assert note_back["result"]["delivered_to"] == role_a["role_id"]
            await close_session(http_b, transport_b, session_b)

            # A returns in a completely fresh MCP session.
            http_a2, transport_a2, session_a2 = await open_session(token_a)
            board_a2 = await call(
                session_a2,
                "commons.board.list",
                {"arguments": {"limit": 10}},
            )
            ids = {post["post_id"] for post in board_a2["result"]["posts"]}
            assert ids == {"hello-a", "reply-b"}

            changes_a2 = await call(
                session_a2,
                "world.get_changes",
                {"after": 0},
            )
            assert any(
                event["kind"] == "commons_note"
                and event["payload"]["from_role_id"] == role_b["role_id"]
                and event["payload"]["text"] == "B replies to A"
                for event in changes_a2["events"]
            )
            await close_session(http_a2, transport_a2, session_a2)
            with sqlite3.connect(db) as conn:
                operation_count = conn.execute(
                    "SELECT COUNT(*) FROM operations WHERE universe='commons'"
                ).fetchone()[0]
                public_posts = conn.execute(
                    """SELECT COUNT(*) FROM world_state
                       WHERE universe='commons'
                         AND scope='commons:board'
                         AND state_key LIKE 'post:%'"""
                ).fetchone()[0]
                event_count = conn.execute(
                    "SELECT COUNT(*) FROM events WHERE universe='commons'"
                ).fetchone()[0]

            assert operation_count == 4
            assert public_posts == 2
            assert event_count == 4

            print(json.dumps({
                "roles": 2,
                "mcp_tools": len(by_name),
                "public_posts": public_posts,
                "durable_events": event_count,
                "write_operations": operation_count,
                "idempotent_post_replay": True,
                "cross_role_shared_fact": True,
                "cross_role_durable_note": True,
                "fresh_session_recovery": True,
            }, ensure_ascii=False))
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
