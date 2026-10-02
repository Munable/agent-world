from __future__ import annotations

from dataclasses import replace
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import httpx

from agent_world.builtin_worlds import get_builtin_definition
from agent_world.errors import InvalidArguments, PermissionDenied, SchemaRejected
from tests.capability_harness import RuntimeCapabilityHarness
from tests.live_server import LiveServer


LISTS = (
    ("commons.post.list", "posts", "post_id", {}, 20),
    ("commons.reply.list", "replies", "reply_id", {"post_id": "p000"}, 50),
    ("commons.conversation.list", "conversations", "conversation_id", {}, 50),
    ("commons.message.list", "messages", "message_id", {"conversation_id": "c000"}, 100),
)


class CommonsPaginationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "commons.sqlite3"
        self.world = get_builtin_definition("commons")
        self.h = RuntimeCapabilityHarness(self.path, self.world, universe="commons")
        self.alice, self.bob, self.carol = self.h.spawn(3)

    def seed(self, count, text=None):
        # Equal timestamps force the key tie-breaker to carry page continuity.
        with patch("agent_world.runtime_functions.time.time", return_value=1700000000.0):
            for index in range(count):
                suffix = f"{index:03d}"
                self.h.call(self.alice, "commons.post.create",
                            {"post_id": "p" + suffix, "text": text or "public " + suffix}, "post" + suffix)
                self.h.call(self.bob, "commons.reply.create",
                            {"post_id": "p000", "reply_id": "r" + suffix, "text": text or "reply " + suffix},
                            "reply" + suffix)
                self.h.call(self.alice, "commons.conversation.open",
                            {"conversation_id": "c" + suffix, "peer_role_id": self.bob.role_id},
                            "conversation" + suffix)
                self.h.call(self.bob, "commons.message.send",
                            {"conversation_id": "c000", "message_id": "m" + suffix,
                             "text": text or "private " + suffix}, "message" + suffix)

    def test_all_lists_recover_more_than_100_items_with_timestamp_ties(self):
        self.seed(105)
        for function, field, identifier, arguments, default_limit in LISTS:
            with self.subTest(function=function):
                default = self.h.query(self.alice, function, arguments)["result"]
                self.assertEqual(len(default[field]), default_limit)
                self.assertTrue(default["has_more"])
                first = self.h.query(self.alice, function, {**arguments, "limit": 100})["result"]
                self.assertEqual(len(first[field]), 100)
                self.assertTrue(first["has_more"])
                self.assertIsInstance(first["next_cursor"], str)
                last = self.h.query(self.alice, function,
                                    {**arguments, "limit": 100, "after": first["next_cursor"]})["result"]
                self.assertEqual(len(last[field]), 5)
                self.assertFalse(last["has_more"])
                self.assertIsNone(last["next_cursor"])
                values = first[field] + last[field]
                prefix = {"post_id": "p", "reply_id": "r", "conversation_id": "c", "message_id": "m"}[identifier]
                self.assertEqual([value[identifier] for value in values],
                                 [prefix + f"{index:03d}" for index in reversed(range(105))])
                self.assertEqual(len({value[identifier] for value in values}), 105)
                self.assertEqual({value["updated_at"] for value in values}, {1700000000.0})

    def test_long_unicode_pages_shrink_without_skipping_or_repeating_history(self):
        from agent_world.runtime_contracts import MAX_RESULT_BYTES, json_text

        self.seed(55, text="界🙂" * 1000)
        for function, field, identifier, arguments, _ in LISTS:
            with self.subTest(function=function):
                values = []
                after = None
                sizes = []
                while True:
                    query = {**arguments, "limit": 100}
                    if after is not None:
                        query["after"] = after
                    response = self.h.query(self.alice, function, query)
                    self.assertLessEqual(len(json_text(response).encode("utf-8")), MAX_RESULT_BYTES)
                    page = response["result"]
                    self.assertTrue(page[field])
                    sizes.append(len(page[field]))
                    values.extend(page[field])
                    if not page["has_more"]:
                        self.assertIsNone(page["next_cursor"])
                        break
                    self.assertNotEqual(page["next_cursor"], after)
                    after = page["next_cursor"]
                    self.assertLess(len(sizes), 56)
                prefix = {"post_id": "p", "reply_id": "r", "conversation_id": "c", "message_id": "m"}[identifier]
                self.assertEqual([value[identifier] for value in values],
                                 [prefix + f"{index:03d}" for index in reversed(range(55))])
                if field != "conversations":
                    self.assertLess(sizes[0], 55)
                    self.assertEqual({value["text"] for value in values}, {"界🙂" * 1000})

    def test_cursor_scope_and_private_permissions_are_rechecked(self):
        self.seed(3)
        pages = {
            function: self.h.query(self.alice, function, {**arguments, "limit": 1})["result"]
            for function, _, _, arguments, _ in LISTS
        }
        wrong_scope = (
            ("commons.reply.list", {"post_id": "p000"}, pages["commons.post.list"]),
            ("commons.reply.list", {"post_id": "p001"}, pages["commons.reply.list"]),
            ("commons.message.list", {"conversation_id": "c001"}, pages["commons.message.list"]),
        )
        for function, arguments, page in wrong_scope:
            with self.subTest(function=function, arguments=arguments):
                with self.assertRaises(InvalidArguments):
                    self.h.query(self.alice, function, {**arguments, "after": page["next_cursor"]})
        with self.assertRaises(InvalidArguments):
            self.h.query(self.carol, "commons.conversation.list",
                         {"after": pages["commons.conversation.list"]["next_cursor"]})
        with self.assertRaises(PermissionDenied):
            self.h.query(self.carol, "commons.message.list",
                         {"conversation_id": "c000", "after": pages["commons.message.list"]["next_cursor"]})
        bob_page = self.h.query(self.bob, "commons.message.list",
                               {"conversation_id": "c000", "after": pages["commons.message.list"]["next_cursor"]})
        self.assertEqual([message["message_id"] for message in bob_page["result"]["messages"]], ["m001", "m000"])
        self.assertEqual(self.h.query(self.carol, "commons.conversation.list", {})["result"]["conversations"], [])
        other = RuntimeCapabilityHarness(self.path, self.world, universe="other")
        other_alice = other.reenter(self.alice)
        with self.assertRaises(InvalidArguments):
            other.query(other_alice, "commons.post.list", {"after": pages["commons.post.list"]["next_cursor"]})

    def test_malformed_or_oversized_cursor_is_rejected(self):
        self.seed(1)
        for function, _, _, arguments, _ in LISTS:
            with self.subTest(function=function):
                with self.assertRaises(InvalidArguments):
                    self.h.query(self.alice, function, {**arguments, "after": "invalid-cursor"})
                with self.assertRaises(SchemaRejected):
                    self.h.query(self.alice, function, {**arguments, "after": "x" * 4097})

    def test_v2_upgrade_preserves_identity_history_and_original_operation(self):
        descriptions = {
            "commons.post.list": "Read recent public posts.",
            "commons.reply.list": "Read recent public replies for one post.",
            "commons.conversation.list": "List private conversations visible to the current participant.",
            "commons.message.list": "Read recent messages from a private conversation.",
        }
        functions = []
        for spec in self.world.functions:
            if spec.name in descriptions:
                schema = {**spec.input_schema, "properties": {
                    key: value for key, value in spec.input_schema["properties"].items() if key != "after"
                }}
                spec = replace(spec, version=1, description=descriptions[spec.name], input_schema=schema)
            functions.append(spec)
        old_world = replace(
            self.world, version=2, functions=tuple(functions),
            entry_instructions=(
                "Read recent posts. Create or reply to public posts when useful. "
                "Use conversations and messages for private two-party communication. "
                "Notification events are not read receipts or acceptance."
            ),
        )
        old_path = Path(self.temp.name) / "v2.sqlite3"
        old = RuntimeCapabilityHarness(old_path, old_world, universe="commons")
        alice, bob = old.spawn(2)
        post_args = {"post_id": "before-upgrade", "text": "persistent"}
        post = old.call(alice, "commons.post.create", post_args, "original-post")
        old.call(alice, "commons.conversation.open",
                 {"conversation_id": "old-private", "peer_role_id": bob.role_id}, "original-conversation")
        old.call(bob, "commons.message.send",
                 {"conversation_id": "old-private", "message_id": "old-message", "text": "private"},
                 "original-message")
        upgraded = RuntimeCapabilityHarness(old_path, self.world, universe="commons")
        reentered = upgraded.reenter(alice)
        self.assertEqual(reentered.role_id, alice.role_id)
        self.assertFalse(reentered.created_profile)
        self.assertEqual(upgraded.runtime.get_world_manifest("commons")["version"], 3)
        self.assertEqual(upgraded.runtime.get_receipt("commons", alice.role_id, "original-post",
                                                    identity_token=alice.token), post)
        self.assertEqual(upgraded.call(reentered, "commons.post.create", post_args, "original-post"),
                         {**post, "replayed": True})
        self.assertEqual(upgraded.query(reentered, "commons.post.list", {})["result"]["posts"][0]["text"],
                         "persistent")
        messages = upgraded.query(reentered, "commons.message.list", {"conversation_id": "old-private"})
        self.assertEqual(messages["result"]["messages"][0]["message_id"], "old-message")
        self.assertEqual(upgraded.runtime.get_function("commons", "commons.post.list")["version"], 2)


class CommonsPaginationTransportTests(unittest.IsolatedAsyncioTestCase):
    async def test_all_cursors_continue_across_http_and_mcp_with_private_authorization(self):
        with LiveServer("commons") as server:
            alice, identity, _ = server.key_identity()
            bob, _, _ = server.key_identity()
            _, outsider, _ = server.key_identity()
            auth = {"Authorization": "Bearer " + identity["token"]}
            with httpx.Client(trust_env=False, timeout=5) as client:
                def http_call(function, arguments, operation_id=None):
                    envelope = {"arguments": arguments}
                    if operation_id is not None:
                        envelope["operation_id"] = operation_id
                    return client.post(server.url + f"/v1/functions/{function}/invoke",
                                       headers=auth, json=envelope)

                for index in range(3):
                    suffix = f"{index:03d}"
                    commands = (
                        ("commons.post.create", {"post_id": "p" + suffix, "text": suffix}),
                        ("commons.reply.create", {"post_id": "p000", "reply_id": "r" + suffix, "text": suffix}),
                        ("commons.conversation.open", {"conversation_id": "c" + suffix,
                                                       "peer_role_id": bob["role_id"]}),
                        ("commons.message.send", {"conversation_id": "c000", "message_id": "m" + suffix,
                                                  "text": "private " + suffix}),
                    )
                    for function, arguments in commands:
                        response = http_call(function, arguments, function + suffix)
                        self.assertEqual(response.status_code, 200, response.text)
                async with server.session(identity) as (session, _):
                    for function, field, identifier, arguments, _ in LISTS:
                        with self.subTest(function=function):
                            first_http = http_call(function, {**arguments, "limit": 2})
                            self.assertEqual(first_http.status_code, 200, first_http.text)
                            first = first_http.json()["result"]
                            last_mcp = await session.call_tool(function, arguments={"arguments": {
                                **arguments, "limit": 2, "after": first["next_cursor"],
                            }})
                            self.assertFalse(last_mcp.is_error, last_mcp.structured_content)
                            last = last_mcp.structured_content["result"]
                            self.assertEqual(len(first[field]) + len(last[field]), 3)
                            self.assertFalse(last["has_more"])
                            self.assertIsNone(last["next_cursor"])
                            self.assertEqual(len({item[identifier] for item in first[field] + last[field]}), 3)
                            first_mcp = await session.call_tool(function, arguments={"arguments": {
                                **arguments, "limit": 2,
                            }})
                            self.assertFalse(first_mcp.is_error, first_mcp.structured_content)
                            first_reverse = first_mcp.structured_content["result"]
                            last_http = http_call(function, {**arguments, "limit": 2,
                                                           "after": first_reverse["next_cursor"]})
                            self.assertEqual(last_http.status_code, 200, last_http.text)
                            self.assertEqual(last_http.json()["result"], last)
                first_private = http_call("commons.message.list", {"conversation_id": "c000", "limit": 1})
                cursor = first_private.json()["result"]["next_cursor"]
                async with server.session(outsider) as (session, _):
                    denied = await session.call_tool("commons.message.list", arguments={"arguments": {
                        "conversation_id": "c000", "after": cursor,
                    }})
                    self.assertTrue(denied.is_error)
                    self.assertEqual(denied.structured_content["error"], "PermissionDenied")


if __name__ == "__main__":
    unittest.main()
