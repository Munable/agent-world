from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path
import os
import secrets
import socket
import subprocess
import sys
import tempfile
import time

import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

ROOT = Path(__file__).resolve().parents[1]


class LiveServer:
    def __init__(self, profile="demo"):
        self.profile = profile

    def __enter__(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = Path(self.temp.name) / "network.sqlite3"
        self.password = secrets.token_urlsafe(24)
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            self.port = sock.getsockname()[1]
        self.url = f"http://127.0.0.1:{self.port}"
        self.log_path = Path(self.temp.name) / "server.log"
        self.log = self.log_path.open("w", encoding="utf-8")
        env = os.environ.copy()
        env["WORLD_WEB_PASSWORD"] = self.password
        self.proc = subprocess.Popen(
            [
                sys.executable,
                "-u",
                "-m",
                "agent_world",
                "--world",
                self.profile,
                "--universe",
                "network",
                "--db",
                str(self.db),
                "--port",
                str(self.port),
            ],
            cwd=ROOT,
            env=env,
            stdout=self.log,
            stderr=subprocess.STDOUT,
        )
        try:
            with httpx.Client(trust_env=False, timeout=0.3) as client:
                deadline = time.monotonic() + 60
                last_probe = "not attempted"
                while time.monotonic() < deadline:
                    if self.proc.poll() is not None:
                        raise RuntimeError("test server exited: " + self.log_path.read_text(encoding="utf-8"))
                    try:
                        response = client.get(self.url + "/health")
                        last_probe = f"HTTP {response.status_code}"
                        if response.status_code == 200 and response.json().get("universe") == "network":
                            return self
                    except (httpx.HTTPError, ValueError) as exc:
                        last_probe = type(exc).__name__
                    time.sleep(0.05)
            details = self.log_path.read_text(encoding="utf-8", errors="replace")[-4000:]
            raise RuntimeError(f"test server readiness deadline exceeded; {last_probe}; log: {details}")
        except BaseException:
            self.__exit__(None, None, None)
            raise

    def __exit__(self, *_):
        if os.name == "nt" and self.proc.poll() is None:
            # Windows venv redirectors may have a Python child; stop only this owned tree.
            subprocess.run(
                ["taskkill", "/PID", str(self.proc.pid), "/T", "/F"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
            )
        elif self.proc.poll() is None:
            self.proc.terminate()
        try:
            self.proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            self.proc.wait(timeout=5)
        self.log.close()
        self.temp.cleanup()

    def role(self, name="Visitor"):
        with httpx.Client(trust_env=False, timeout=5) as client:
            response = client.post(
                self.url + "/api/roles", auth=("operator", self.password), json={"display_name": name}
            )
            response.raise_for_status()
            role = response.json()
            response = client.post(
                self.url + f"/api/roles/{role['role_id']}/join-package",
                auth=("operator", self.password),
                json={"ttl_seconds": 60},
            )
            response.raise_for_status()
            instructions = response.json()["agent_instructions"]
            ticket = instructions.split("Join ticket: ", 1)[1].splitlines()[0]
            response = client.post(self.url + "/v1/join/exchange", json={"ticket": ticket})
            response.raise_for_status()
            return role, response.json()["identity"]

    @asynccontextmanager
    async def session(self, identity, *, terminate=True):
        captured = {}

        async def observe_response(response):
            sid = response.headers.get("mcp-session-id")
            if sid:
                captured["session_id"] = sid

        async with httpx.AsyncClient(
            trust_env=False,
            timeout=5,
            headers={"Authorization": "Bearer " + identity["token"]},
            event_hooks={"response": [observe_response]},
        ) as http:
            async with streamable_http_client(
                self.url + "/mcp", http_client=http, terminate_on_close=terminate
            ) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    yield session, captured
