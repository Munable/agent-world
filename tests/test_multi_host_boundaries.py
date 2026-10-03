"""Two scripted host processes, one key identity, and unrelated host intents.

These are public HTTP/MCP contract regressions, not autonomous model evidence.
The owner issues both credentials by key proof and uses the regular self-revoke
surface; no operator credential or direct token-table mutation is used.
"""
from __future__ import annotations

import asyncio
import base64
import json
from pathlib import Path
import sys
import unittest

import httpx
from cryptography.hazmat.primitives.serialization import Encoding, NoEncryption, PrivateFormat

from tests.live_server import LiveServer


ROOT = Path(__file__).resolve().parents[1]

# Credentials and recovery keys travel through stdin, never argv or output.
# A host owns an independent Python process, HTTP client, and initialized MCP
# session. Its only actions are the explicit, bounded requests below.
HOST_PROGRAM = r'''
import asyncio, base64, json, os, sys
import httpx
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

def encode(raw):
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")

def decode(raw):
    return base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4))

def emit(value):
    print(json.dumps(value), flush=True)

async def main():
    config = json.loads(sys.stdin.readline())
    async with httpx.AsyncClient(base_url=config["url"], trust_env=False, timeout=5) as public:
        identity = config.get("identity")
        if identity is None:
            key = Ed25519PrivateKey.from_private_bytes(decode(config["key"]))
            public_key = encode(key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw))
            challenge = await public.post("/v1/key-identities/challenges", json={"public_key": public_key})
            challenge.raise_for_status()
            challenge = challenge.json()
            entered = await public.post("/v1/key-identities/exchange", json={
                "challenge_id": challenge["challenge_id"],
                "signature": encode(key.sign(decode(challenge["message"]))),
            })
            entered.raise_for_status()
            identity = entered.json()["identity"]

    captured = {}
    async def observe(response):
        if response.headers.get("mcp-session-id"):
            captured["session_id"] = response.headers["mcp-session-id"]

    async with httpx.AsyncClient(base_url=config["url"], trust_env=False, timeout=5,
            headers={"Authorization": "Bearer " + identity["token"]},
            event_hooks={"response": [observe]}) as http:
        async with streamable_http_client(config["url"] + "/mcp", http_client=http,
                terminate_on_close=False) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                emit({"pid": os.getpid(), "role_id": identity["role_id"],
                      "token_id": identity["token_id"], **captured})
                while True:
                    line = await asyncio.to_thread(sys.stdin.readline)
                    if not line:
                        break
                    request = json.loads(line)
                    if "tool" in request:
                        result = await session.call_tool(request["tool"], arguments=request["arguments"])
                        emit({"is_error": result.is_error, "body": result.structured_content})
                    else:
                        headers = {}
                        if request.get("mcp_session"):
                            headers = {"Mcp-Session-Id": captured["session_id"],
                                       "MCP-Protocol-Version": "2025-11-25",
                                       "Accept": "application/json, text/event-stream"}
                        response = await http.request(request["method"], request["path"],
                            json=request.get("json"), headers=headers)
                        emit({"status": response.status_code, "body": response.json()})

asyncio.run(main())
'''


class ScriptedHost:
    def __init__(self, server, identity=None, key=None):
        self.config = {"url": server.url, "identity": identity}
        self.pending_readers = set()
        if key is not None:
            raw = key.private_bytes(Encoding.Raw, PrivateFormat.Raw, NoEncryption())
            self.config["key"] = base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")

    async def __aenter__(self):
        self.proc = await asyncio.create_subprocess_exec(
            sys.executable, "-u", "-c", HOST_PROGRAM, cwd=ROOT,
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            await self.send(self.config)
            self.ready = await self.receive()
            return self
        except BaseException as exc:
            await self.__aexit__(type(exc), exc, exc.__traceback__)
            raise

    async def __aexit__(self, exc_type, exc, traceback):
        closing = asyncio.create_task(self.close())
        try:
            await asyncio.shield(closing)
        except asyncio.CancelledError:
            await closing
            raise
        except Exception as failure:
            if exc is None:
                raise
            exc.add_note("Scripted host cleanup also failed: " + str(failure))

    async def close(self):
        readers = [task for task in self.pending_readers if not task.done()]
        for task in readers:
            task.cancel()
        if readers:
            await asyncio.gather(*readers, return_exceptions=True)
        self.proc.stdin.close()
        try:
            await asyncio.wait_for(self.proc.wait(), 5)
        except TimeoutError:
            self.proc.terminate()
            try:
                await asyncio.wait_for(self.proc.wait(), 5)
            except TimeoutError:
                self.proc.kill()
                await asyncio.wait_for(self.proc.wait(), 5)
            raise AssertionError("scripted host did not finish its bounded requests")
        if self.proc.returncode:
            stderr = (await self.proc.stderr.read()).decode("utf-8", errors="replace")
            raise AssertionError("scripted host failed: " + stderr[-2000:])

    async def send(self, request):
        self.proc.stdin.write((json.dumps(request) + "\n").encode("utf-8"))
        await self.proc.stdin.drain()

    async def receive(self):
        task = asyncio.current_task()
        self.pending_readers.add(task)
        try:
            line = await asyncio.wait_for(self.proc.stdout.readline(), 8)
            if not line:
                raise AssertionError("scripted host exited without a result")
            return json.loads(line)
        finally:
            self.pending_readers.discard(task)

    async def call(self, request):
        await self.send(request)
        return await self.receive()

    async def tool(self, name, arguments):
        return await self.call({"tool": name, "arguments": arguments})

    async def http(self, method, path, body=None, *, mcp_session=False):
        return await self.call({"method": method, "path": path, "json": body,
                                "mcp_session": mcp_session})


def command(post_id, text, operation):
    return {"arguments": {"post_id": post_id, "text": text}, "operation_id": operation}


class MultiHostBoundaryTests(unittest.IsolatedAsyncioTestCase):
    def ok(self, response):
        if "status" in response:
            self.assertEqual(response["status"], 200, response["body"])
        else:
            self.assertIs(response["is_error"], False, response["body"])
        return response["body"]

    async def identities(self, server):
        role, first, key = await asyncio.to_thread(server.key_identity)
        same_role, second, _ = await asyncio.to_thread(server.key_identity, key)
        self.assertEqual(same_role["role_id"], role["role_id"])
        self.assertNotEqual(first["token_id"], second["token_id"])
        return role, first, second, key

    async def test_independent_host_intents_and_principal_scoped_operation_race(self):
        async with LiveServer("commons") as server:
            role, first, second, _ = await self.identities(server)
            async with ScriptedHost(server, first) as a, ScriptedHost(server, second) as b:
                self.assertNotEqual(a.ready["pid"], b.ready["pid"])
                self.assertNotEqual(a.ready["session_id"], b.ready["session_id"])
                self.assertEqual(a.ready["role_id"], b.ready["role_id"])
                # A keeps a field journal; B publishes its own travel notice.
                # Their separate operations carry no shared goal or agreement.
                journal = command("field-journal", "My own field observation", "journal-op")
                notice = command("travel-notice", "My separate travel notice", "notice-op")
                left, right = await asyncio.gather(
                    a.tool("commons.post.create", journal),
                    b.http("POST", "/v1/functions/commons.post.create/invoke", notice),
                )
                for response, expected in ((left, journal), (right, notice)):
                    result = self.ok(response)["result"]
                    self.assertEqual(result["author_role_id"], role["role_id"])
                    self.assertEqual(result["post_id"], expected["arguments"]["post_id"])

                same_intent = command("one-retry", "One logical intent", "shared-operation")
                left, right = await asyncio.gather(
                    a.tool("commons.post.create", same_intent),
                    b.http("POST", "/v1/functions/commons.post.create/invoke", same_intent),
                )
                bodies = [self.ok(response) for response in (left, right)]
                self.assertEqual(sorted(body["replayed"] for body in bodies), [False, True])
                self.assertEqual(bodies[0]["result"], bodies[1]["result"])
                self.assertEqual(bodies[0]["commit_seq"], bodies[1]["commit_seq"])
                conflict = await b.http("POST", "/v1/functions/commons.post.create/invoke",
                    command("unrelated-collision", "A different intent", "shared-operation"))
                self.assertEqual(conflict["status"], 409)
                self.assertEqual(conflict["body"]["error"], "OperationConflict")
                posts = self.ok(await a.tool("commons.post.list", {"arguments": {}}))["result"]["posts"]
                self.assertEqual({post["post_id"] for post in posts},
                                 {"field-journal", "travel-notice", "one-retry"})
                for host in (a, b):
                    receipt = self.ok(await host.http("GET", "/v1/receipts/shared-operation"))
                    self.assertEqual(receipt["commit_seq"], bodies[0]["commit_seq"])
                    self.assertEqual(receipt["result"], bodies[0]["result"])

    async def test_self_revocation_blocks_open_session_and_pending_wait_only_for_other_host(self):
        async with LiveServer("commons") as server:
            _, first, second, _ = await self.identities(server)
            _, peer, _ = await asyncio.to_thread(server.key_identity)
            async with ScriptedHost(server, first) as a, ScriptedHost(server, second) as b:
                self.ok(await b.tool("commons.post.create", command("watched-post", "My own post", "watch-op")))
                cursor = self.ok(await a.tool("world.get_changes", {}))["next_cursor"]
                await a.send({"tool": "world.wait_changes", "arguments": {"after": cursor, "timeout": 2}})
                pending = asyncio.create_task(a.receive())
                await asyncio.sleep(0.2)
                # This proves a pending client RPC on an initialized session;
                # it does not establish a server-side admission barrier.
                self.assertFalse(pending.done(), "wait must still be pending before self-revocation")
                # This public API keeps B and revokes all of its other current
                # credentials. With exactly A/B, only A is affected.
                revoked = self.ok(await b.http("POST", "/api/me/revoke-other-credentials", {}))
                self.assertEqual(revoked["revoked_count"], 1)
                self.assertEqual(revoked["kept_token_id"], second["token_id"])
                denied_wait = await pending
                self.assertIs(denied_wait["is_error"], True)
                self.assertEqual(denied_wait["body"]["error"], "InvalidIdentityToken")
                self.assertNotIn("events", denied_wait["body"])

                attempted = command("revoked-write", "Old host cannot commit", "revoked-op")
                denied = await a.http("POST", "/mcp", {
                    "jsonrpc": "2.0", "id": 91, "method": "tools/call",
                    "params": {"name": "commons.post.create", "arguments": attempted},
                }, mcp_session=True)
                self.assertEqual(denied["status"], 401)
                self.assertEqual(denied["body"]["error"], "InvalidIdentityToken")
                missing = await b.http("GET", "/v1/receipts/revoked-op")
                self.assertEqual(missing["status"], 404)
                self.assertEqual(missing["body"]["error"], "ReceiptNotFound")
                self.ok(await b.http("POST", "/v1/functions/commons.post.create/invoke",
                                     command("still-active", "B continues its own work", "active-op")))
                await b.send({"tool": "world.wait_changes", "arguments": {"after": cursor, "timeout": 2}})
                waiting = asyncio.create_task(b.receive())
                async with httpx.AsyncClient(base_url=server.url, trust_env=False, timeout=5,
                        headers={"Authorization": "Bearer " + peer["token"]}) as peer_http:
                    reply = await peer_http.post("/v1/functions/commons.reply.create/invoke", json={
                        "operation_id": "peer-reply", "arguments": {
                            "post_id": "watched-post", "reply_id": "new-reply", "text": "A later public reply"}})
                    self.assertEqual(reply.status_code, 200, reply.json())
                woke = self.ok(await waiting)
                self.assertIs(woke["timed_out"], False)
                self.assertEqual([event["payload"]["reply_id"] for event in woke["events"]], ["new-reply"])
                blocked_read = await a.http("POST", "/mcp", {
                    "jsonrpc": "2.0", "id": 92, "method": "tools/call",
                    "params": {"name": "world.get_changes", "arguments": {"after": cursor}},
                }, mcp_session=True)
                self.assertEqual(blocked_read["status"], 401)
                self.assertNotIn("new-reply", json.dumps(blocked_read["body"]))
                posts = self.ok(await b.tool("commons.post.list", {"arguments": {}}))["result"]["posts"]
                self.assertEqual({post["post_id"] for post in posts}, {"watched-post", "still-active"})

    async def test_new_host_process_recovers_original_identity_receipt_and_operation_after_revocation(self):
        async with LiveServer("commons") as server:
            role, first, second, key = await self.identities(server)
            intent = command("saved-journal", "A's independent durable record", "original-operation")
            async with ScriptedHost(server, second) as b:
                async with ScriptedHost(server, first) as a:
                    first_pid = a.ready["pid"]
                    original = self.ok(await a.tool("commons.post.create", intent))
                    self.ok(await b.http("POST", "/v1/functions/commons.post.create/invoke",
                                         command("other-notice", "B's unrelated record", "other-operation")))
                    self.assertEqual(self.ok(await b.http("POST", "/api/me/revoke-other-credentials", {}))["revoked_count"], 1)
                async with ScriptedHost(server, key=key) as returned:
                    self.assertNotEqual(returned.ready["pid"], first_pid)
                    self.assertEqual(returned.ready["role_id"], role["role_id"])
                    self.assertNotIn(returned.ready["token_id"], {first["token_id"], second["token_id"]})
                    receipt = self.ok(await returned.tool("world.get_receipt", {"operation_id": "original-operation"}))
                    self.assertEqual(receipt["commit_seq"], original["commit_seq"])
                    self.assertEqual(receipt["result"], original["result"])
                    replay = self.ok(await returned.tool("commons.post.create", intent))
                    self.assertIs(replay["replayed"], True)
                    self.assertEqual(replay["commit_seq"], original["commit_seq"])
                    self.assertEqual(replay["result"], original["result"])
                    posts = self.ok(await returned.tool("commons.post.list", {"arguments": {}}))["result"]["posts"]
                    self.assertEqual({post["post_id"] for post in posts}, {"saved-journal", "other-notice"})
                    self.assertEqual(self.ok(await b.http("GET", "/api/me"))["role_id"], role["role_id"])


if __name__ == "__main__":
    unittest.main()
