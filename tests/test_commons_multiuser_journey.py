"""Deterministic external clients continue a shared plan through real socket faults.

This falsifies loss of identity, attribution, private authorization or durable
history across client/process/backup boundaries. Scripted role decisions do not
prove autonomous model reasoning, cross-machine networking or a cross-day soak.
"""
from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import secrets
import socket
import sqlite3
import tempfile
import threading
import unittest

import httpx

from agent_world import WorldRuntime
from tests.live_server import LiveServer
from tests.test_network_recovery import ResponseLossProxy


class JourneyServer(LiveServer):
    """Reuse the transport fixture with a caller-selected disposable directory."""

    def __enter__(self):
        root = os.getenv("WORLD_TEST_WORKDIR")
        if root is not None and not Path(root).is_dir():
            raise ValueError("WORLD_TEST_WORKDIR must be an existing directory")
        self.temp = tempfile.TemporaryDirectory(prefix="commons-journey-", dir=root)
        self.db = Path(self.temp.name) / "network.sqlite3"
        self.password = secrets.token_urlsafe(24)
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            self.port = sock.getsockname()[1]
        self.url = f"http://127.0.0.1:{self.port}"
        self.log_path = Path(self.temp.name) / "server.log"
        self.log = self.log_path.open("w", encoding="utf-8")
        self.start()
        return self


def text_record(record_type, **values):
    return json.dumps({"record_type": record_type, **values}, ensure_ascii=False, sort_keys=True)


class CommonsMultiuserJourneyTests(unittest.IsolatedAsyncioTestCase):
    def auth(self, identity):
        return {"Authorization": "Bearer " + identity["token"]}

    def invoke(self, client, server, identity, function, arguments, operation=None):
        body = {"arguments": arguments}
        if operation is not None:
            body["operation_id"] = operation
        response = client.post(
            server.url + f"/v1/functions/{function}/invoke",
            headers=self.auth(identity), json=body,
        )
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    async def mcp_invoke(self, server, identity, function, arguments, operation=None):
        body = {"arguments": arguments}
        if operation is not None:
            body["operation_id"] = operation
        async with server.session(identity) as (session, _):
            result = await session.call_tool(function, arguments=body)
            self.assertFalse(result.is_error, result.structured_content)
            return result.structured_content

    def history(self, client, server, identity, function, collection, **arguments):
        """A fresh client reconstructs all retained history using only the API."""
        rows, cursors = [], set()
        after = None
        for _ in range(10):
            page_args = {**arguments, "limit": 100}
            if after is not None:
                page_args["after"] = after
            page = self.invoke(client, server, identity, function, page_args)["result"]
            self.assertIn("has_more", page)
            self.assertIn("next_cursor", page)
            rows.extend(page[collection])
            if not page["has_more"]:
                return rows, len(cursors) + 1
            after = page["next_cursor"]
            self.assertTrue(after)
            self.assertNotIn(after, cursors, "history must make forward progress")
            cursors.add(after)
        self.fail("history never reached its retained beginning")

    def assert_private_boundary(self, client, server, identity):
        conversations = self.invoke(
            client, server, identity, "commons.conversation.list", {"limit": 100},
        )["result"]["conversations"]
        self.assertEqual(conversations, [])
        for function, arguments, operation in (
            ("commons.message.list", {"conversation_id": "supplier-budget"}, None),
            ("commons.message.send", {"conversation_id": "supplier-budget",
                                      "message_id": "intrusion", "text": "not a participant"},
             "outsider-send"),
        ):
            body = {"arguments": arguments}
            if operation:
                body["operation_id"] = operation
            denied = client.post(
                server.url + f"/v1/functions/{function}/invoke",
                headers=self.auth(identity), json=body,
            )
            self.assertEqual(denied.status_code, 403, denied.text)
            self.assertEqual(denied.json()["error"], "PermissionDenied")
            self.assertNotIn("PRIVATE-QUOTE-HK1080-ONLY", denied.text)
        events = client.get(server.url + "/v1/changes", headers=self.auth(identity))
        self.assertEqual(events.status_code, 200, events.text)
        self.assertEqual(events.json()["events"], [])

    async def test_three_role_plan_survives_retry_reentry_history_restart_and_backup(self):
        started = datetime.now(timezone.utc).isoformat()
        with JourneyServer("commons") as server, ResponseLossProxy(server.url) as proxy:
            planner, planner_id, planner_key = server.key_identity()
            budget, budget_id, budget_key = server.key_identity()
            access, access_id, _ = server.key_identity()
            outsider, outsider_id, _ = server.key_identity()
            roles = {planner["role_id"], budget["role_id"], access["role_id"], outsider["role_id"]}
            self.assertEqual(len(roles), 4)
            for identity in (planner_id, budget_id, access_id, outsider_id):
                self.assertAlmostEqual(identity["expires_at"] - identity["created_at"], 86400, delta=1)

            with httpx.Client(trust_env=False, timeout=10) as client:
                for identity, name in ((planner_id, "Event planner"), (budget_id, "Budget reviewer"),
                                       (access_id, "Accessibility reviewer"), (outsider_id, "Observer")):
                    profile = client.patch(server.url + "/api/me", headers=self.auth(identity),
                                           json={"display_name": name})
                    self.assertEqual(profile.status_code, 200, profile.text)

                # A regular user's bearer is sufficient for external MCP discovery.
                async with server.session(planner_id) as (session, _):
                    discovered = await session.list_tools()
                    self.assertIn("commons.reply.list", {item.name for item in discovered.tools})
                    for name in ("world.bootstrap", "world.describe"):
                        result = await session.call_tool(name, arguments={})
                        self.assertFalse(result.is_error, result.structured_content)

                invalid = client.get(server.url + "/v1/bootstrap",
                                     headers={"Authorization": "Bearer awid_invalid"})
                self.assertEqual(invalid.status_code, 401, invalid.text)
                draft = {"record_type": "activity_constraints", "participants": 24,
                         "budget_cap_hkd": 1200, "date": "2026-10-15", "start": "14:00",
                         "end": "16:00", "wheelchair_guests": 2,
                         "candidate_venue": "Upper-floor room with stairs"}
                self.invoke(client, server, planner_id, "commons.post.create",
                            {"post_id": "activity-plan", "text": json.dumps(draft)}, "publish-draft")
                budget_review = {"record_type": "budget_review", "venue_hkd": 400,
                                 "snacks_hkd": 360, "transport_hkd": 200,
                                 "contingency_hkd": 120, "total_hkd": 1080}
                await self.mcp_invoke(
                    server, budget_id, "commons.reply.create",
                    {"post_id": "activity-plan", "reply_id": "budget-review",
                     "text": json.dumps(budget_review)}, "publish-budget-review",
                )
                access_review = {"record_type": "accessibility_review", "reject_stairs": True,
                                 "venue": "Ground-floor community hall", "step_free": True,
                                 "accessible_toilet": True, "transport": "Step-free shuttle",
                                 "arrival": "13:45", "wheelchair_spaces": 2}
                self.invoke(client, server, access_id, "commons.reply.create",
                            {"post_id": "activity-plan", "reply_id": "accessibility-review",
                             "text": json.dumps(access_review)}, "publish-access-review")
                self.invoke(client, server, planner_id, "commons.reply.create",
                            {"post_id": "activity-plan", "reply_id": "revision-2",
                             "text": text_record("plan_revision", revision=2,
                                                 venue=access_review["venue"], total_hkd=1080)},
                            "publish-revision-2")

                self.invoke(client, server, planner_id, "commons.conversation.open",
                            {"conversation_id": "supplier-budget", "peer_role_id": budget["role_id"]},
                            "open-private-budget")
                self.invoke(client, server, planner_id, "commons.message.send",
                            {"conversation_id": "supplier-budget", "message_id": "supplier-quote",
                             "text": text_record("supplier_quote", private_reference="PRIVATE-QUOTE-HK1080-ONLY",
                                                 total_hkd=1080)}, "send-supplier-quote")
                before = client.get(server.url + "/v1/changes", headers=self.auth(planner_id))
                self.assertEqual(before.status_code, 200, before.text)
                planner_cursor = before.json()["next_cursor"]

                # The proxy sees a committed upstream result but the client sees only
                # a real connection failure. The original operation must be retained.
                lost = {"operation_id": "private-budget-lost", "arguments": {
                    "conversation_id": "supplier-budget", "message_id": "verified-quote",
                    "text": text_record("quote_check", total_hkd=1080, within_cap=True)}}
                invoke_path = "/v1/functions/commons.message.send/invoke"
                proxy.drop_path = invoke_path
                with self.assertRaises(httpx.RemoteProtocolError):
                    client.post(proxy.url + invoke_path, headers=self.auth(budget_id), json=lost)
                self.assertEqual(proxy.dropped_status, 200)
                proxy.partitioned = True
                with self.assertRaises(httpx.RemoteProtocolError):
                    client.get(proxy.url + "/v1/receipts/private-budget-lost", headers=self.auth(budget_id))

            previous_pid = server.proc.pid
            server.restart(abrupt=True)
            self.assertNotEqual(server.proc.pid, previous_pid)
            proxy.partitioned = False
            returned, new_budget_id, _ = server.key_identity(budget_key)
            self.assertEqual(returned["role_id"], budget["role_id"])
            replay = await self.mcp_invoke(server, new_budget_id, "commons.message.send",
                                           lost["arguments"], lost["operation_id"])
            self.assertTrue(replay["replayed"])
            budget_id = new_budget_id

            with httpx.Client(trust_env=False, timeout=10) as client:
                resumed = client.get(server.url + "/v1/changes", headers=self.auth(planner_id),
                                     params={"after": planner_cursor})
                self.assertEqual(resumed.status_code, 200, resumed.text)
                self.assertEqual([event["payload"]["message_id"] for event in resumed.json()["events"]],
                                 ["verified-quote"])
                receipt = client.get(server.url + "/v1/receipts/private-budget-lost",
                                     headers=self.auth(budget_id))
                self.assertEqual(receipt.status_code, 200, receipt.text)
                self.assertEqual(receipt.json()["result"]["message_id"], "verified-quote")
                changed_intent = {**lost, "arguments": {**lost["arguments"], "text": "different intent"}}
                conflict = client.post(server.url + invoke_path, headers=self.auth(budget_id), json=changed_intent)
                self.assertEqual(conflict.status_code, 409, conflict.text)
                self.assertEqual(conflict.json()["error"], "OperationConflict")

                start_line = threading.Barrier(2)

                def concurrent_send(data):
                    identity, message_id, operation = data
                    with httpx.Client(trust_env=False, timeout=10) as connection:
                        start_line.wait(timeout=10)
                        return self.invoke(connection, server, identity, "commons.message.send", {
                            "conversation_id": "supplier-budget", "message_id": message_id,
                            "text": text_record("planning_exchange", message_id=message_id,
                                                total_hkd=1080)}, operation)

                with ThreadPoolExecutor(2) as pool:
                    both = list(pool.map(concurrent_send, ((planner_id, "planner-concurrent", "planner-concurrent"),
                                                          (budget_id, "budget-concurrent", "budget-concurrent"))))
                    start_line = threading.Barrier(2)
                    same = list(pool.map(concurrent_send, ((budget_id, "duplicate-concurrent", "duplicate-concurrent"),
                                                          (budget_id, "duplicate-concurrent", "duplicate-concurrent"))))
                self.assertTrue(all(result["ok"] for result in both))
                self.assertEqual(sorted(result["replayed"] for result in same), [False, True])

                # Checklists retain the original constraints and review sources past
                # a single page. They are purposeful synthetic records, not model turns.
                checklist = ("seat", "snack", "step_free_route", "shuttle")
                for index in range(104):
                    reviewer = budget_id if index % 2 == 0 else access_id
                    self.invoke(client, server, reviewer, "commons.reply.create", {
                        "post_id": "activity-plan", "reply_id": f"check-{index:03d}",
                        "text": text_record("planning_check", sequence=index,
                                            guest=index // 4 + 1 if index < 96 else None,
                                            check=checklist[index % 4] if index < 96 else "shared_logistics",
                                            revision=2, passed=True)}, f"public-check-{index}")
                    speaker = planner_id if index % 2 == 0 else budget_id
                    self.invoke(client, server, speaker, "commons.message.send", {
                        "conversation_id": "supplier-budget", "message_id": f"quote-check-{index:03d}",
                        "text": text_record("budget_audit", sequence=index, revision=2,
                                            total_hkd=1080, remaining_hkd=120)}, f"private-check-{index}")

                for identity in (access_id, outsider_id):
                    self.assert_private_boundary(client, server, identity)
                async with server.session(outsider_id) as (session, _):
                    denied = await session.call_tool("commons.message.list", arguments={
                        "arguments": {"conversation_id": "supplier-budget", "limit": 100}})
                    self.assertTrue(denied.is_error)
                    self.assertEqual(denied.structured_content["error"], "PermissionDenied")

                runtime = WorldRuntime(server.db)
                runtime.revoke_identity_token(planner_id["token_id"])
                denied = client.get(server.url + "/v1/bootstrap", headers=self.auth(planner_id))
                self.assertEqual(denied.status_code, 401, denied.text)

            # No preceding private client/model memory is passed to the returning host.
            returned, planner_id, _ = server.key_identity(planner_key)
            self.assertEqual(returned["role_id"], planner["role_id"])
            with httpx.Client(trust_env=False, timeout=10) as fresh:
                replies, reply_pages = self.history(fresh, server, planner_id, "commons.reply.list", "replies",
                                                    post_id="activity-plan")
                messages, message_pages = self.history(fresh, server, planner_id, "commons.message.list", "messages",
                                                        conversation_id="supplier-budget")
                self.assertEqual(len(replies), 107)
                self.assertEqual(len(messages), 109)
                self.assertEqual(len({row["reply_id"] for row in replies}), len(replies))
                self.assertEqual(len({row["message_id"] for row in messages}), len(messages))
                self.assertEqual(reply_pages, 2)
                self.assertEqual(message_pages, 2)
                by_reply = {row["reply_id"]: row for row in replies}
                by_message = {row["message_id"]: row for row in messages}
                self.assertEqual(by_reply["budget-review"]["author_role_id"], budget["role_id"])
                self.assertEqual(by_reply["accessibility-review"]["author_role_id"], access["role_id"])
                self.assertEqual(by_message["verified-quote"]["sender_role_id"], budget["role_id"])
                original = self.invoke(fresh, server, planner_id, "commons.post.get",
                                       {"post_id": "activity-plan"})["result"]["post"]
                constraints = json.loads(original["text"])
                cost = json.loads(by_reply["budget-review"]["text"])
                accessibility = json.loads(by_reply["accessibility-review"]["text"])
                quote = json.loads(by_message["verified-quote"]["text"])
                line_items = {field: cost[field] for field in ("venue_hkd", "snacks_hkd", "transport_hkd", "contingency_hkd")}
                self.assertEqual(sum(line_items.values()), cost["total_hkd"])
                self.assertEqual(cost["snacks_hkd"], constraints["participants"] * 15)
                self.assertEqual(cost["total_hkd"], quote["total_hkd"])
                self.assertLessEqual(cost["total_hkd"], constraints["budget_cap_hkd"])
                self.assertTrue(accessibility["reject_stairs"])
                self.assertTrue(accessibility["step_free"] and accessibility["accessible_toilet"])
                self.assertGreaterEqual(accessibility["wheelchair_spaces"], constraints["wheelchair_guests"])
                final = {"record_type": "final_activity_plan", "source_post_id": "activity-plan",
                         "review_reply_ids": ["budget-review", "accessibility-review"],
                         "date": constraints["date"], "start": constraints["start"], "end": constraints["end"],
                         "participants": constraints["participants"], "venue": accessibility["venue"],
                         "arrival": accessibility["arrival"], "transport": accessibility["transport"],
                         "step_free": True, "accessible_toilet": True, "wheelchair_spaces": 2,
                         "budget_cap_hkd": constraints["budget_cap_hkd"], "line_items_hkd": line_items,
                         "total_hkd": cost["total_hkd"], "remaining_hkd": constraints["budget_cap_hkd"] - cost["total_hkd"]}
                final_args = {"post_id": "final-activity-plan", "text": json.dumps(final, sort_keys=True)}
                committed_final = self.invoke(fresh, server, planner_id, "commons.post.create", final_args,
                                              "publish-final-plan")
                self.assertNotIn("PRIVATE-QUOTE-HK1080-ONLY", final_args["text"])
                for identity, reply_id, role in ((budget_id, "final-budget-check", "budget"),
                                                (access_id, "final-access-check", "accessibility")):
                    self.invoke(fresh, server, identity, "commons.reply.create", {
                        "post_id": "final-activity-plan", "reply_id": reply_id,
                        "text": text_record("final_review", reviewer=role,
                                            source_post_id="activity-plan", total_hkd=1080, passed=True)}, reply_id)

                # Online backup includes committed WAL pages while the source process
                # stays live. Restore only the backup, never copy the live main file.
                source_db = server.db
                backup_db = Path(server.temp.name) / "online-backup.sqlite3"
                with closing(sqlite3.connect(source_db, isolation_level=None)) as pinned:
                    pinned.execute("BEGIN")
                    pinned.execute("SELECT COUNT(*) FROM operations").fetchone()
                    self.invoke(fresh, server, planner_id, "commons.reply.create", {
                        "post_id": "final-activity-plan", "reply_id": "backup-checkpoint",
                        "text": "Final plan and role reviews are in the online recovery point."}, "backup-checkpoint")
                    self.assertGreater(Path(str(source_db) + "-wal").stat().st_size, 32)
                    with closing(sqlite3.connect(source_db.as_uri() + "?mode=ro", uri=True)) as source:
                        with closing(sqlite3.connect(backup_db)) as target:
                            source.backup(target)
                            self.assertEqual(target.execute("PRAGMA integrity_check").fetchall(), [("ok",)])
                            self.assertEqual(target.execute("PRAGMA foreign_key_check").fetchall(), [])
                            counts = {"public_replies": len(replies), "private_messages": len(messages),
                                      "operation_rows": target.execute("SELECT COUNT(*) FROM operations").fetchone()[0],
                                      "state_rows": target.execute("SELECT COUNT(*) FROM world_state").fetchone()[0]}
                            self.assertEqual(target.execute(
                                "SELECT COUNT(*) FROM operations WHERE idempotency_key='private-budget-lost'"
                            ).fetchone()[0], 1)
                            self.assertEqual(target.execute(
                                "SELECT COUNT(*) FROM operations WHERE idempotency_key='duplicate-concurrent'"
                            ).fetchone()[0], 1)
                            self.assertEqual(target.execute(
                                "SELECT COUNT(*) FROM operations WHERE idempotency_key='outsider-send'"
                            ).fetchone()[0], 0)
                            saved_plan = target.execute(
                                "SELECT value_json FROM world_state WHERE scope='commons:posts' AND state_key='post:final-activity-plan'"
                            ).fetchone()[0]
                            self.assertEqual(json.loads(json.loads(saved_plan)["text"]), final)
                self.invoke(fresh, server, planner_id, "commons.reply.create", {
                    "post_id": "final-activity-plan", "reply_id": "after-backup",
                    "text": "A later edit must not appear in the earlier backup."}, "after-backup")

            server.stop()
            server.db = backup_db
            server.start()
            returned, restored_planner_id, _ = server.key_identity(planner_key)
            self.assertEqual(returned["role_id"], planner["role_id"])
            returned_budget, restored_budget_id, _ = server.key_identity(budget_key)
            self.assertEqual(returned_budget["role_id"], budget["role_id"])
            with httpx.Client(trust_env=False, timeout=10) as restored:
                visible_plan = self.invoke(restored, server, outsider_id, "commons.post.get",
                                           {"post_id": "final-activity-plan"})["result"]["post"]
                self.assertEqual(json.loads(visible_plan["text"]), final)
                self.assertEqual(visible_plan["author_role_id"], planner["role_id"])
                reviews, _ = self.history(restored, server, restored_planner_id, "commons.reply.list", "replies",
                                           post_id="final-activity-plan")
                self.assertEqual({row["reply_id"] for row in reviews},
                                 {"final-budget-check", "final-access-check", "backup-checkpoint"})
                by_review = {row["reply_id"]: row for row in reviews}
                for reply_id, author, role in (("final-budget-check", budget, "budget"),
                                               ("final-access-check", access, "accessibility")):
                    self.assertEqual(by_review[reply_id]["author_role_id"], author["role_id"])
                    self.assertEqual(json.loads(by_review[reply_id]["text"]), {
                        "record_type": "final_review", "reviewer": role, "source_post_id": "activity-plan",
                        "total_hkd": 1080, "passed": True})
                restored_messages, _ = self.history(restored, server, restored_budget_id, "commons.message.list", "messages",
                                                     conversation_id="supplier-budget")
                self.assertEqual({row["message_id"]: row for row in restored_messages}, by_message)
                for identity in (access_id, outsider_id):
                    self.assert_private_boundary(restored, server, identity)
                replay_final = await self.mcp_invoke(server, restored_planner_id, "commons.post.create", final_args,
                                                     "publish-final-plan")
                self.assertEqual(replay_final, {**committed_final, "replayed": True})

            evidence_path = os.getenv("WORLD_JOURNEY_EVIDENCE")
            if evidence_path:
                evidence = {"evidence_kind": "deterministic-loopback-contract-acceptance",
                            "started_at_utc": started, "finished_at_utc": datetime.now(timezone.utc).isoformat(),
                            "participants": ["Event planner", "Budget reviewer", "Accessibility reviewer", "Observer"],
                            "final_activity_plan": final, "counts": counts,
                            "history_pages": {"replies": reply_pages, "messages": message_pages},
                            "verified": ["regular key entry and same-key reentry", "Bearer-authenticated HTTP and MCP",
                                         "three-role public plan and two-person private collaboration",
                                         "fourth-user private denial before and after restore",
                                         "lost HTTP response after upstream commit", "same-operation replay and intent conflict",
                                         "concurrent messages and same-operation race", "offline event cursor continuation",
                                         "real service process death and restart", "credential revoke and fresh proof",
                                         "retained history beyond 100 records", "live WAL online backup and fresh process restore",
                                         "SQLite integrity, unique receipts and attributable final plan"],
                            "does_not_prove": ["autonomous model reasoning", "cross-day observation",
                                               "cross-machine networking", "production backup operations"]}
                Path(evidence_path).write_text(json.dumps(evidence, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    unittest.main()
