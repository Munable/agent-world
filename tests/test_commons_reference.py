from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import tempfile
import unittest

from agent_world.builtin_worlds import get_builtin_definition
from agent_world.errors import PermissionDenied
from tests.capability_harness import RuntimeCapabilityHarness


class CommonsReferenceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.h = RuntimeCapabilityHarness(
            Path(self.temp.name) / "commons.sqlite3",
            get_builtin_definition("commons"),
            universe="commons-reference",
        )
        self.alice, self.bob, self.carol = self.h.spawn(3, prefix="Commons")

    def test_public_post_reply_replay_and_restart_recovery(self):
        created = self.h.call(
            self.alice,
            "commons.post.create",
            {"post_id": "p1", "text": "hello world"},
            "post-p1",
        )
        self.assertEqual(created["result"]["author_role_id"], self.alice.role_id)

        replied = self.h.call(
            self.bob,
            "commons.reply.create",
            {"post_id": "p1", "reply_id": "r1", "text": "reply"},
            "reply-r1",
        )
        self.assertEqual(replied["result"]["author_role_id"], self.bob.role_id)

        posts = self.h.query(self.carol, "commons.post.list", {"limit": 10})
        self.assertEqual([p["post_id"] for p in posts["result"]["posts"]], ["p1"])
        replies = self.h.query(
            self.carol,
            "commons.reply.list",
            {"post_id": "p1", "limit": 10},
        )
        self.assertEqual([r["reply_id"] for r in replies["result"]["replies"]], ["r1"])

        notifications = self.h.changes(self.alice)["events"]
        self.assertEqual(len(notifications), 1)
        self.assertEqual(notifications[0]["kind"], "commons.reply.created")
        self.assertEqual(notifications[0]["payload"]["reply_id"], "r1")

        replay = self.h.call(
            self.bob,
            "commons.reply.create",
            {"post_id": "p1", "reply_id": "r1", "text": "reply"},
            "reply-r1",
        )
        self.assertTrue(replay["replayed"])
        self.assertEqual(len(self.h.changes(self.alice)["events"]), 1)

        self.h.restart()
        posts_after = self.h.query(self.alice, "commons.post.list", {"limit": 10})
        replies_after = self.h.query(
            self.alice,
            "commons.reply.list",
            {"post_id": "p1", "limit": 10},
        )
        self.assertEqual(posts_after["result"]["posts"][0]["text"], "hello world")
        self.assertEqual(replies_after["result"]["replies"][0]["text"], "reply")

    def test_private_conversation_message_and_offline_recovery(self):
        opened = self.h.call(
            self.alice,
            "commons.conversation.open",
            {
                "conversation_id": "c1",
                "peer_role_id": self.bob.role_id,
            },
            "open-c1",
        )
        self.assertEqual(
            set(opened["result"]["participant_role_ids"]),
            {self.alice.role_id, self.bob.role_id},
        )

        alice_conversations = self.h.query(
            self.alice,
            "commons.conversation.list",
            {"limit": 10},
        )
        bob_conversations = self.h.query(
            self.bob,
            "commons.conversation.list",
            {"limit": 10},
        )
        carol_conversations = self.h.query(
            self.carol,
            "commons.conversation.list",
            {"limit": 10},
        )
        self.assertEqual(
            [x["conversation_id"] for x in alice_conversations["result"]["conversations"]],
            ["c1"],
        )
        self.assertEqual(
            [x["conversation_id"] for x in bob_conversations["result"]["conversations"]],
            ["c1"],
        )
        self.assertEqual(carol_conversations["result"]["conversations"], [])

        sent = self.h.call(
            self.alice,
            "commons.message.send",
            {
                "conversation_id": "c1",
                "message_id": "m1",
                "text": "private hello",
            },
            "message-m1",
        )
        self.assertEqual(sent["result"]["sender_role_id"], self.alice.role_id)
        self.assertNotIn("read_at", sent["result"])
        self.assertNotIn("accepted", sent["result"])
        self.assertNotIn("delivered_to", sent["result"])

        with self.assertRaises(PermissionDenied):
            self.h.query(
                self.carol,
                "commons.message.list",
                {"conversation_id": "c1", "limit": 10},
            )
        with self.assertRaises(PermissionDenied):
            self.h.call(
                self.carol,
                "commons.message.send",
                {
                    "conversation_id": "c1",
                    "message_id": "intrusion",
                    "text": "nope",
                },
                "intrusion-op",
            )

        self.h.restart()

        bob_events = self.h.changes(self.bob)["events"]
        self.assertEqual(
            [event["kind"] for event in bob_events],
            ["commons.conversation.opened", "commons.message.created"],
        )
        messages = self.h.query(
            self.bob,
            "commons.message.list",
            {"conversation_id": "c1", "limit": 10},
        )
        self.assertEqual([m["message_id"] for m in messages["result"]["messages"]], ["m1"])
        self.assertEqual(messages["result"]["messages"][0]["text"], "private hello")

        replay = self.h.call(
            self.alice,
            "commons.message.send",
            {
                "conversation_id": "c1",
                "message_id": "m1",
                "text": "private hello",
            },
            "message-m1",
        )
        self.assertTrue(replay["replayed"])
        self.assertEqual(len(self.h.changes(self.bob)["events"]), 2)

    def test_two_participants_can_write_same_conversation_concurrently(self):
        self.h.call(
            self.alice,
            "commons.conversation.open",
            {
                "conversation_id": "c2",
                "peer_role_id": self.bob.role_id,
            },
            "open-c2",
        )

        def send(pair):
            index, participant = pair
            return self.h.call(
                participant,
                "commons.message.send",
                {
                    "conversation_id": "c2",
                    "message_id": f"m{index}",
                    "text": f"message-{index}",
                },
                f"send-{index}",
            )

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(send, enumerate((self.alice, self.bob), 1)))

        self.assertEqual(len(results), 2)
        messages = self.h.query(
            self.alice,
            "commons.message.list",
            {"conversation_id": "c2", "limit": 10},
        )
        self.assertEqual(
            {m["message_id"] for m in messages["result"]["messages"]},
            {"m1", "m2"},
        )


if __name__ == "__main__":
    unittest.main()
