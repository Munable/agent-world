from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import tempfile
import unittest

from agent_world.errors import InvalidIdentityToken, PermissionDenied
from tests.capability_harness import RuntimeCapabilityHarness
from tests.fixtures.capability_matrix_world import WORLD


class RuntimeCapabilityMatrixTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.h = RuntimeCapabilityHarness(
            Path(self.temp.name) / "matrix.sqlite3",
            WORLD,
        )
        self.p = self.h.spawn(6)

    def test_one_to_many_broadcast_replay_and_restart_recovery(self):
        sender, recipients = self.p[0], self.p[1:]
        args = {
            "message_id": "fanout-1",
            "recipients": [p.role_id for p in recipients],
        }
        first = self.h.call(
            sender,
            "topology.broadcast",
            args,
            "fanout-op",
        )
        self.assertEqual(first["result"]["recipient_count"], 5)
        for participant in recipients:
            page = self.h.changes(participant)
            self.assertEqual(len(page["events"]), 1)
            self.assertEqual(
                page["events"][0]["payload"]["sender_role_id"],
                sender.role_id,
            )

        replay = self.h.call(
            sender,
            "topology.broadcast",
            args,
            "fanout-op",
        )
        self.assertTrue(replay["replayed"])

        self.h.restart()
        for participant in recipients:
            self.assertEqual(
                len(self.h.changes(participant)["events"]),
                1,
            )
        self.assertTrue(
            self.h.call(
                sender,
                "topology.broadcast",
                args,
                "fanout-op",
            )["replayed"]
        )

    def test_many_to_one_concurrent_fanin_is_complete_and_private(self):
        target, contributors = self.p[0], self.p[1:]

        def submit(pair):
            index, participant = pair
            return self.h.call(
                participant,
                "topology.contribute",
                {
                    "target_role_id": target.role_id,
                    "contribution_id": f"c{index}",
                    "value": index,
                },
                f"fanin-{index}",
            )

        with ThreadPoolExecutor(max_workers=len(contributors)) as pool:
            results = list(
                pool.map(
                    submit,
                    enumerate(contributors, 1),
                )
            )
        self.assertEqual(len(results), 5)

        aggregate = self.h.query(
            target,
            "topology.aggregate",
            {"target_role_id": target.role_id},
        )
        self.assertEqual(
            sorted(
                x["value"]
                for x in aggregate["result"]["contributions"]
            ),
            [1, 2, 3, 4, 5],
        )
        self.assertEqual(
            len(self.h.changes(target)["events"]),
            5,
        )
        with self.assertRaises(PermissionDenied):
            self.h.query(
                contributors[0],
                "topology.aggregate",
                {"target_role_id": target.role_id},
            )

    def test_many_to_many_and_one_revocation_does_not_stop_others(self):
        def send(participant):
            recipients = [
                p.role_id
                for p in self.p
                if p.role_id != participant.role_id
            ]
            return self.h.call(
                participant,
                "topology.broadcast",
                {
                    "message_id": "mesh-" + participant.role_id,
                    "recipients": recipients,
                },
                "mesh-op-" + participant.role_id,
            )

        with ThreadPoolExecutor(max_workers=len(self.p)) as pool:
            list(pool.map(send, self.p))

        for participant in self.p:
            senders = {
                event["payload"]["sender_role_id"]
                for event in self.h.changes(participant)["events"]
            }
            self.assertEqual(
                senders,
                {
                    p.role_id
                    for p in self.p
                    if p.role_id != participant.role_id
                },
            )

        self.h.revoke(self.p[2])
        with self.assertRaises(InvalidIdentityToken):
            send(self.p[2])

        surviving = self.h.call(
            self.p[3],
            "topology.broadcast",
            {
                "message_id": "after-revoke",
                "recipients": [self.p[0].role_id],
            },
            "after-revoke",
        )
        self.assertEqual(
            surviving["result"]["recipient_count"],
            1,
        )


if __name__ == "__main__":
    unittest.main()
