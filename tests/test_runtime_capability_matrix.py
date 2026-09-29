from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import tempfile
import unittest

from agent_world.errors import InvalidIdentityToken, PermissionDenied, StateConflict
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

    def test_same_key_supports_multiple_local_credentials_and_reentry_after_revoke(self):
        first = self.p[0]
        second = self.h.reenter(first)

        self.assertFalse(second.created_profile)
        self.assertEqual(second.public_key, first.public_key)
        self.assertEqual(second.role_id, first.role_id)
        self.assertNotEqual(second.token_id, first.token_id)

        for participant in (first, second):
            aggregate = self.h.query(
                participant,
                "topology.aggregate",
                {"target_role_id": first.role_id},
            )
            self.assertEqual(aggregate["result"]["contributions"], [])

        self.h.revoke(first)
        with self.assertRaises(InvalidIdentityToken):
            self.h.query(
                first,
                "topology.aggregate",
                {"target_role_id": first.role_id},
            )

        replacement = self.h.reenter(first)
        self.assertFalse(replacement.created_profile)
        self.assertEqual(replacement.role_id, first.role_id)
        self.assertNotEqual(replacement.token_id, first.token_id)
        self.assertEqual(
            self.h.query(
                replacement,
                "topology.aggregate",
                {"target_role_id": first.role_id},
            )["result"]["contributions"],
            [],
        )

    def test_same_key_returns_to_same_profile_after_runtime_restart(self):
        first = self.p[0]
        self.h.restart()
        returned = self.h.reenter(first)

        self.assertFalse(returned.created_profile)
        self.assertEqual(returned.public_key, first.public_key)
        self.assertEqual(returned.role_id, first.role_id)
        self.assertNotEqual(returned.token_id, first.token_id)

    def test_same_key_in_independent_worlds_has_independent_profiles_and_bearers(self):
        first = self.p[0]
        other = RuntimeCapabilityHarness(
            Path(self.temp.name) / "other.sqlite3",
            WORLD,
            universe="other",
        )
        remote = other.enter(first.private_key)

        self.assertEqual(remote.public_key, first.public_key)
        self.assertNotEqual(remote.role_id, first.role_id)
        self.assertNotEqual(remote.token, first.token)
        self.assertTrue(remote.created_profile)
        with self.assertRaises(InvalidIdentityToken):
            other.runtime.resolve_identity_token(first.token)

    def test_concurrent_first_entry_of_same_key_creates_one_profile(self):
        private_key = self.p[0].private_key
        fresh = RuntimeCapabilityHarness(
            Path(self.temp.name) / "concurrent-entry.sqlite3",
            WORLD,
        )

        with ThreadPoolExecutor(max_workers=2) as pool:
            entries = list(pool.map(lambda _: fresh.enter(private_key), range(2)))

        self.assertEqual({entry.role_id for entry in entries}, {entries[0].role_id})
        self.assertEqual(len({entry.token_id for entry in entries}), 2)
        self.assertEqual(sorted(entry.created_profile for entry in entries), [False, True])

    def test_concurrent_same_object_cas_allows_exactly_one_winner(self):
        contenders = self.p[:2]

        def claim(pair):
            index, participant = pair
            try:
                result = self.h.call(
                    participant,
                    "topology.claim_slot",
                    {"slot_id": "shared", "expected_version": 0},
                    f"claim-slot-{index}",
                )
                return ("ok", participant.role_id, result)
            except StateConflict as exc:
                return ("conflict", participant.role_id, exc)

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(claim, enumerate(contenders)))

        self.assertEqual(
            sorted(result[0] for result in results),
            ["conflict", "ok"],
        )
        winner = next(result for result in results if result[0] == "ok")
        state = self.h.runtime.get_state(
            self.h.universe,
            "contest:shared",
            "slot:shared",
        )
        self.assertEqual(state["value"]["owner_role_id"], winner[1])
        self.assertEqual(state["version"], 1)

    def test_bulk_key_identity_entry_keeps_profiles_and_credentials_unique(self):
        with ThreadPoolExecutor(max_workers=8) as pool:
            participants = list(pool.map(lambda _: self.h.enter(), range(32)))

        self.assertEqual(len({p.public_key for p in participants}), 32)
        self.assertEqual(len({p.role_id for p in participants}), 32)
        self.assertEqual(len({p.token_id for p in participants}), 32)
        self.assertTrue(all(p.created_profile for p in participants))

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
