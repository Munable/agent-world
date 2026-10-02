from __future__ import annotations

import unittest

import httpx

from tests.live_server import LiveServer


class CommonsTransportTests(unittest.IsolatedAsyncioTestCase):
    async def test_missing_post_is_a_public_resource_error_over_http_and_mcp(self):
        with LiveServer("commons") as server:
            _, identity, _ = server.key_identity()
            auth = {"Authorization": "Bearer " + identity["token"]}
            cases = (
                ("commons.post.get", {"arguments": {"post_id": "missing-post"}}),
                ("commons.reply.list", {"arguments": {"post_id": "missing-post"}}),
                ("commons.reply.create", {
                    "operation_id": "missing-post-reply",
                    "arguments": {"post_id": "missing-post", "reply_id": "reply", "text": "test"},
                }),
            )
            expected = {"error": "ResourceNotFound", "message": "post not found", "retryable": False}
            async with server.session(identity) as (session, _):
                with httpx.Client(trust_env=False, timeout=5) as client:
                    for tool, arguments in cases:
                        with self.subTest(tool=tool):
                            response = client.post(
                                server.url + f"/v1/functions/{tool}/invoke",
                                headers=auth, json=arguments,
                            )
                            self.assertEqual(response.status_code, 404, response.text)
                            self.assertEqual(response.json(), expected)
                            result = await session.call_tool(tool, arguments=arguments)
                            self.assertTrue(result.is_error)
                            self.assertEqual(result.structured_content, {**expected, "tool": tool})

    async def test_reference_slice_works_across_http_and_mcp(self):
        with LiveServer("commons") as server:
            alice, alice_identity, _ = server.key_identity()
            bob, bob_identity, _ = server.key_identity()
            _, carol_identity, _ = server.key_identity()
            alice_auth = {"Authorization": "Bearer " + alice_identity["token"]}
            bob_auth = {"Authorization": "Bearer " + bob_identity["token"]}

            with httpx.Client(trust_env=False, timeout=5) as client:
                post = client.post(
                    server.url + "/v1/functions/commons.post.create/invoke",
                    headers=alice_auth,
                    json={
                        "operation_id": "net-post",
                        "arguments": {"post_id": "p1", "text": "hello"},
                    },
                )
                self.assertEqual(post.status_code, 200, post.text)

            async with server.session(bob_identity) as (session, _):
                reply = await session.call_tool(
                    "commons.reply.create",
                    arguments={
                        "operation_id": "net-reply",
                        "arguments": {
                            "post_id": "p1",
                            "reply_id": "r1",
                            "text": "reply",
                        },
                    },
                )
                self.assertFalse(reply.is_error, reply.structured_content)

            async with server.session(alice_identity) as (session, _):
                replies = await session.call_tool(
                    "commons.reply.list",
                    arguments={"arguments": {"post_id": "p1", "limit": 10}},
                )
                self.assertFalse(replies.is_error, replies.structured_content)
                self.assertEqual(
                    replies.structured_content["result"]["replies"][0]["reply_id"],
                    "r1",
                )
                opened = await session.call_tool(
                    "commons.conversation.open",
                    arguments={
                        "operation_id": "net-open",
                        "arguments": {
                            "conversation_id": "c1",
                            "peer_role_id": bob["role_id"],
                        },
                    },
                )
                self.assertFalse(opened.is_error, opened.structured_content)

            with httpx.Client(trust_env=False, timeout=5) as client:
                sent = client.post(
                    server.url + "/v1/functions/commons.message.send/invoke",
                    headers=bob_auth,
                    json={
                        "operation_id": "net-message",
                        "arguments": {
                            "conversation_id": "c1",
                            "message_id": "m1",
                            "text": "private",
                        },
                    },
                )
                self.assertEqual(sent.status_code, 200, sent.text)

            async with server.session(bob_identity) as (session, _):
                messages = await session.call_tool(
                    "commons.message.list",
                    arguments={
                        "arguments": {
                            "conversation_id": "c1",
                            "limit": 10,
                        }
                    },
                )
                self.assertFalse(messages.is_error, messages.structured_content)
                self.assertEqual(
                    messages.structured_content["result"]["messages"][0]["message_id"],
                    "m1",
                )

            async with server.session(carol_identity) as (session, _):
                denied = await session.call_tool(
                    "commons.message.list",
                    arguments={
                        "arguments": {
                            "conversation_id": "c1",
                            "limit": 10,
                        }
                    },
                )
                self.assertTrue(denied.is_error)
                self.assertEqual(
                    denied.structured_content["error"],
                    "PermissionDenied",
                )


if __name__ == "__main__":
    unittest.main()
