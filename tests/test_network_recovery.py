"""Real loopback socket faults, not cross-machine or autonomous-Agent evidence."""
from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import socket
import threading
import unittest

import httpx

from tests.live_server import LiveServer


class ResponseLossProxy:
    """A test-only HTTP forwarder which can lose a response after upstream commit."""

    def __init__(self, upstream):
        self.upstream = upstream
        self.drop_path = None
        self.partitioned = False
        self.dropped_status = None

    def __enter__(self):
        proxy = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def do_GET(self):
                self.forward()

            def do_POST(self):
                self.forward()

            def forward(self):
                if proxy.partitioned:
                    self.connection.shutdown(socket.SHUT_RDWR)
                    self.connection.close()
                    return
                body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
                with httpx.Client(trust_env=False, timeout=5) as client:
                    response = client.request(
                        self.command,
                        proxy.upstream + self.path,
                        headers={k: v for k, v in self.headers.items()
                                 if k.lower() not in {"host", "connection", "content-length"}},
                        content=body,
                    )
                if self.path == proxy.drop_path:
                    proxy.drop_path = None
                    proxy.dropped_status = response.status_code
                    self.connection.shutdown(socket.SHUT_RDWR)
                    self.connection.close()
                    return
                self.send_response(response.status_code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(response.content)))
                self.end_headers()
                self.wfile.write(response.content)

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        self.url = f"http://127.0.0.1:{self.httpd.server_port}"
        return self

    def __exit__(self, *_):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=5)


class NetworkRecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_lost_response_partition_crash_and_cross_transport_replay(self):
        with LiveServer("commons") as server, ResponseLossProxy(server.url) as proxy:
            alice, alice_identity, alice_key = server.key_identity()
            bob, bob_identity, _ = server.key_identity()
            _, outsider_identity, _ = server.key_identity()
            alice_auth = {"Authorization": "Bearer " + alice_identity["token"]}
            bob_auth = {"Authorization": "Bearer " + bob_identity["token"]}
            invoke = "/v1/functions/commons.message.send/invoke"
            operation = {
                "operation_id": "lost-message",
                "arguments": {"conversation_id": "c1", "message_id": "m1", "text": "durable"},
            }
            with httpx.Client(trust_env=False, timeout=5) as client:
                opened = client.post(
                    proxy.url + "/v1/functions/commons.conversation.open/invoke",
                    headers=alice_auth,
                    json={"operation_id": "open", "arguments": {
                        "conversation_id": "c1", "peer_role_id": bob["role_id"]}},
                )
                self.assertEqual(opened.status_code, 200, opened.text)
                before = client.get(proxy.url + "/v1/changes", headers=bob_auth).json()
                cursor = before["next_cursor"]
                proxy.drop_path = invoke
                with self.assertRaises(httpx.RemoteProtocolError):
                    client.post(proxy.url + invoke, headers=alice_auth, json=operation)
                self.assertEqual(proxy.dropped_status, 200)
                proxy.partitioned = True
                with self.assertRaises(httpx.RemoteProtocolError):
                    client.get(proxy.url + "/v1/receipts/lost-message", headers=alice_auth)

            # The acknowledged upstream commit survives an ungraceful server death.
            server.restart(abrupt=True)
            proxy.partitioned = False
            returned, new_identity, _ = server.key_identity(alice_key)
            self.assertEqual(returned["role_id"], alice["role_id"])
            async with server.session(new_identity) as (session, _):
                replay = await session.call_tool("commons.message.send", arguments=operation)
                self.assertFalse(replay.is_error, replay.structured_content)
                self.assertTrue(replay.structured_content["replayed"])

            with httpx.Client(trust_env=False, timeout=5) as client:
                after = client.get(proxy.url + "/v1/changes", headers=bob_auth,
                                   params={"after": cursor}).json()
                self.assertEqual([e["kind"] for e in after["events"]], ["commons.message.created"])
                self.assertEqual(after["events"][0]["payload"]["message_id"], "m1")
                receipt = client.get(proxy.url + "/v1/receipts/lost-message", headers=alice_auth)
                self.assertEqual(receipt.status_code, 200, receipt.text)
                messages = client.post(
                    proxy.url + "/v1/functions/commons.message.list/invoke",
                    headers=bob_auth, json={"arguments": {"conversation_id": "c1", "limit": 100}},
                )
                self.assertEqual(messages.status_code, 200, messages.text)
                self.assertEqual([m["message_id"] for m in messages.json()["result"]["messages"]], ["m1"])
                denied = client.post(
                    proxy.url + "/v1/functions/commons.message.list/invoke",
                    headers={"Authorization": "Bearer " + outsider_identity["token"]},
                    json={"arguments": {"conversation_id": "c1"}},
                )
                self.assertEqual(denied.status_code, 403, denied.text)
                self.assertEqual(denied.json()["error"], "PermissionDenied")


if __name__ == "__main__":
    unittest.main()
