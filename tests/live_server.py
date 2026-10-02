from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path
import asyncio
import base64
import os
import secrets
import shutil
import socket
import subprocess
import sys
import tempfile
import time

import httpx
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

ROOT = Path(__file__).resolve().parents[1]


class _OwnedDirectory:
    # A process that cannot be reaped may still have its database open. Unlike
    # TemporaryDirectory, this directory has no implicit deletion finalizer.
    def __init__(self):
        self.name = tempfile.mkdtemp(prefix="agent-world-test-")

    def cleanup(self):
        try:
            shutil.rmtree(self.name)
        except FileNotFoundError:
            pass


class LiveServer:
    def __init__(self, profile="demo"):
        self.profile = profile
        self.startup_timeout = float(os.getenv("WORLD_TEST_STARTUP_TIMEOUT", "60"))
        if not 1 <= self.startup_timeout <= 300:
            raise ValueError("test startup timeout must be between 1 and 300 seconds")

    def __enter__(self):
        self.temp = _OwnedDirectory()
        self.proc = None
        self.log = None
        try:
            self.db = Path(self.temp.name) / "network.sqlite3"
            self.password = secrets.token_urlsafe(24)
            with socket.socket() as sock:
                sock.bind(("127.0.0.1", 0))
                self.port = sock.getsockname()[1]
            self.url = f"http://127.0.0.1:{self.port}"
            self.log_path = Path(self.temp.name) / "server.log"
            self.log = self.log_path.open("w", encoding="utf-8")
            self.start()
        except BaseException as exc:
            if self.log is None or not self.log.closed:
                self.__exit__(type(exc), exc, exc.__traceback__)
            raise
        return self

    async def __aenter__(self):
        opening = asyncio.create_task(asyncio.to_thread(self.__enter__))
        try:
            return await asyncio.shield(opening)
        except asyncio.CancelledError as cancelled:
            # A cancelled await does not stop the startup thread. Reap its
            # owned process before allowing the test to leave this context.
            try:
                await opening
            except Exception as opening_failure:
                for note in getattr(opening_failure, "__notes__", ()):
                    cancelled.add_note(note)
                raise cancelled from opening_failure
            else:
                await asyncio.to_thread(self.__exit__, None, None, None)
            raise

    async def __aexit__(self, exc_type, exc, traceback):
        closing = asyncio.create_task(asyncio.to_thread(self.__exit__, exc_type, exc, traceback))
        try:
            return await asyncio.shield(closing)
        except asyncio.CancelledError:
            await closing
            raise

    def start(self):
        """Start the owned process with the same database and network address."""
        env = os.environ.copy()
        env["WORLD_WEB_PASSWORD"] = self.password
        try:
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
            with httpx.Client(trust_env=False, timeout=0.3) as client:
                deadline = time.monotonic() + self.startup_timeout
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
        except BaseException as exc:
            self.__exit__(type(exc), exc, exc.__traceback__)
            raise

    def stop(self, *, abrupt=False):
        """Stop only this fixture process, preserving its database for recovery tests."""
        if self.proc is None:
            return
        if abrupt and os.name != "nt" and self.proc.poll() is None:
            self.proc.kill()
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

    def restart(self, *, abrupt=False):
        self.stop(abrupt=abrupt)
        self.start()

    def __exit__(self, exc_type, exc, traceback):
        stop_error = None
        try:
            self.stop()
        except BaseException as failure:
            stop_error = failure
            if exc is not None:
                raise failure from exc
            raise
        finally:
            unreaped = self.proc is not None and self.proc.poll() is None
            failure = stop_error if stop_error is not None else exc
            if unreaped and failure is None:
                failure = RuntimeError("owned test server did not exit")
            if failure is not None:
                try:
                    if self.log is None:
                        details = "no server log was opened"
                    else:
                        self.log.flush()
                        details = self.log_path.read_text(encoding="utf-8", errors="replace")[-4000:]
                except (OSError, ValueError) as log_error:
                    details = f"log unavailable: {type(log_error).__name__}"
                failure.add_note("Owned test server log before cleanup:\n" + (details or "(empty)"))
                if unreaped:
                    failure.add_note(f"Owned test server PID {self.proc.pid} is still running; retained directory: {self.temp.name}")
            try:
                if self.log is not None:
                    self.log.close()
            finally:
                if not unreaped:
                    self.temp.cleanup()
            if unreaped and stop_error is None:
                raise failure

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

    def key_identity(self, private_key=None):
        private_key = private_key or Ed25519PrivateKey.generate()
        public_key = base64.urlsafe_b64encode(
            private_key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
        ).rstrip(b"=").decode("ascii")
        with httpx.Client(trust_env=False, timeout=5) as client:
            response = client.post(
                self.url + "/v1/key-identities/challenges",
                json={"public_key": public_key},
            )
            response.raise_for_status()
            challenge = response.json()
            message = base64.urlsafe_b64decode(
                challenge["message"] + "=" * (-len(challenge["message"]) % 4)
            )
            signature = base64.urlsafe_b64encode(
                private_key.sign(message)
            ).rstrip(b"=").decode("ascii")
            response = client.post(
                self.url + "/v1/key-identities/exchange",
                json={
                    "challenge_id": challenge["challenge_id"],
                    "signature": signature,
                },
            )
            response.raise_for_status()
            body = response.json()
        return body["role_profile"], body["identity"], private_key

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
