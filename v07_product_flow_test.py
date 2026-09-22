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
from mcp.client.streamable_http import streamable_http_client


ROOT = pathlib.Path(__file__).resolve().parent
WEB_PORT = 8845
MCP_PORT = 8846
WEB = f"http://127.0.0.1:{WEB_PORT}"
MCP_URL = f"http://127.0.0.1:{MCP_PORT}/mcp"
WEB_USER = "operator"
WEB_PASSWORD = "product-test-password"


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
async def main() -> None:
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        db = pathlib.Path(tmp) / "product.sqlite3"
        common = os.environ.copy()
        common["WORLD_DB"] = str(db)
        common["WORLD_UNIVERSE"] = "commons"

        web_env = common.copy()
        web_env["WORLD_WEB_USER"] = WEB_USER
        web_env["WORLD_WEB_PASSWORD"] = WEB_PASSWORD
        web_env["WORLD_PUBLIC_BASE_URL"] = WEB
        web_env["WORLD_MCP_PUBLIC_URL"] = MCP_URL

        mcp_env = common.copy()
        mcp_env["WORLD_PROFILE"] = "commons"
        mcp_env["WORLD_AUTH_REQUIRED"] = "1"
        mcp_env["WORLD_HOST"] = "127.0.0.1"

        web_proc, web_log = start_server(
            "product_app", WEB_PORT, web_env, "v07_product_web.log"
        )
        mcp_proc, mcp_log = start_server(
            "mcp_app", MCP_PORT, mcp_env, "v07_product_mcp.log"
        )
        try:
            wait_ready(WEB + "/health", {200})
            wait_ready(MCP_URL, {401})

            assert httpx.get(WEB + "/", timeout=5).status_code == 401
            auth = httpx.BasicAuth(WEB_USER, WEB_PASSWORD)
            page = httpx.get(WEB + "/", auth=auth, timeout=5)
            assert page.status_code == 200
            assert "Give your agent a place to exist." in page.text
            assert "Create a role" in page.text

            script = httpx.get(WEB + "/static/app.js", timeout=5)
            assert script.status_code == 200
            assert "entered_world" in script.text
            role_response = httpx.post(
                WEB + "/api/roles",
                auth=auth,
                json={"display_name": "Lantern", "avatar_ref": "avatar://lantern"},
                timeout=5,
            )
            role_response.raise_for_status()
            role = role_response.json()

            status_before = httpx.get(
                WEB + f"/api/roles/{role['role_id']}/status",
                auth=auth,
                timeout=5,
            )
            status_before.raise_for_status()
            assert status_before.json()["identity_claimed"] is False
            assert status_before.json()["entered_world"] is False

            package_response = httpx.post(
                WEB + f"/api/roles/{role['role_id']}/join-package",
                auth=auth,
                json={"ttl_seconds": 60},
                timeout=5,
            )
            package_response.raise_for_status()
            package = package_response.json()
            prompt = package["agent_instructions"]
            assert "Join Open Agent World as this role." in prompt
            assert role["role_id"] in prompt
            assert WEB + "/v1/join/exchange" in prompt
            assert MCP_URL in prompt
            assert "awjt_" in prompt
            assert "awid_" not in prompt

            ticket = prompt.split("Join ticket: ", 1)[1].splitlines()[0].strip()
            exchange = httpx.post(
                WEB + "/v1/join/exchange",
                json={"ticket": ticket},
                timeout=5,
            )
            exchange.raise_for_status()
            identity = exchange.json()["identity"]
            token = identity["token"]
            claimed = httpx.get(
                WEB + f"/api/roles/{role['role_id']}/status",
                auth=auth,
                timeout=5,
            ).json()
            assert claimed["identity_claimed"] is True
            assert claimed["entered_world"] is False

            async with httpx.AsyncClient(
                headers={"Authorization": f"Bearer {token}"},
                timeout=10,
            ) as mcp_http:
                async with streamable_http_client(
                    MCP_URL, http_client=mcp_http
                ) as (read_stream, write_stream):
                    async with ClientSession(read_stream, write_stream) as session:
                        init = await session.initialize()
                        tools = await session.list_tools()
                        boot = await session.call_tool(
                            "world.bootstrap", arguments={}
                        )
                        assert not boot.is_error
                        assert boot.structured_content["role_id"] == role["role_id"]
                        assert boot.structured_content["role_profile"]["display_name"] == "Lantern"

            entered = httpx.get(
                WEB + f"/api/roles/{role['role_id']}/status",
                auth=auth,
                timeout=5,
            ).json()
            assert entered["identity_claimed"] is True
            assert entered["entered_world"] is True
            assert entered["presence"]["bootstrap_count"] == 1

            print(json.dumps({
                "web_auth": True,
                "role_created": True,
                "join_package_generated": True,
                "identity_claimed": True,
                "mcp_protocol": str(init.protocol_version),
                "entered_world": True,
                "bootstrap_count": 1,
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
