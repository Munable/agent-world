"""Independent SDK consumer contracts; no OpenCode or private world imports."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

import httpx
from jsonschema import Draft202012Validator

from agent_world.grove_agent_client import (GroveAgentClient, AgentClientError, HttpGroveTransport,
    McpGroveTransport, JsonOperationJournal, MemoryOperationJournal)


EMPTY = {"type": "object", "properties": {}, "additionalProperties": False}
JOIN = {"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"], "additionalProperties": False}
READ = {"type": "object", "properties": {"entity_id": {"type": "string"}}, "required": ["entity_id"], "additionalProperties": False}
WRITE = {"type": "object", "properties": {"target_id": {"type": "string"}, "amount": {"type": "integer", "minimum": 1},
    "expected_player_version": {"type": "integer"}, "expected_target_version": {"type": "integer"}},
    "required": ["target_id", "amount", "expected_player_version", "expected_target_version"], "additionalProperties": False}


def intent_proof(function, arguments):
    canonical = json.dumps({"function_id": function, "arguments": arguments}, ensure_ascii=False,
                           sort_keys=True, separators=(",", ":"), allow_nan=False)
    return {"algorithm": "sha256-runtime-canonical-json-v1",
            "digest": hashlib.sha256(canonical.encode("utf-8")).hexdigest()}


class PublicWorldPort:
    """Only the published HTTP/MCP shapes, deliberately no world implementation."""
    def __init__(self):
        self.role = "participant"
        self.universe = "same-world"
        self.world_code = "shared-grove"
        self.world_version = 1
        self.snapshot = 7
        self.joined = False
        self.version = 2
        self.receipts = {}
        self.sent = []
        self.lose_reply = False
        self.lose_conflict_reply = False
        self.descriptors = [{"function_id": name, "access": access, "version": 1,
            "description": "Declared world function", "input_schema": schema} for name, access, schema in (
                ("grove.agent_contract", "read", EMPTY), ("grove.join", "write", JOIN),
                ("grove.inspect", "read", READ), ("grove.rule_action", "write", WRITE))]
        self.contract = {"contract_version": 1, "world_id": "shared-grove", "functions": [
            {"function_id": "grove.join", "access": "write", "guard_paths": []},
            {"function_id": "grove.inspect", "access": "read"},
            {"function_id": "grove.rule_action", "access": "write",
             "binding_policy": "grove.fixed-observation.v1", "guard_paths": [
                ["expected_player_version"], ["expected_target_version"]]}]}

    def bootstrap(self):
        return {"protocol_version": "0.15", "universe": self.universe, "role_id": self.role,
            "role_profile": {}, "world": {"id": self.world_code, "version": self.world_version},
            "world_entry_state": {"view": {"player": None,
                "agent_contract_request": {"function_id": "grove.agent_contract", "arguments": {}}}},
            "world_guide": {}, "latest_event_seq": 0, "snapshot_cursor": 0}

    def invoke(self, function, envelope):
        if function == "grove.agent_contract":
            value = self.contract
        elif function == "grove.inspect":
            if not self.joined:
                raise AgentClientError("PermissionDenied", "Enter first.", 403)
            value = {"player": {"id": self.role, "version": 3},
                "entity": {"id": envelope["arguments"]["entity_id"], "version": self.version},
                "agent_context": {"world_instance": self.universe, "world_id": self.world_code, "world_version": self.world_version,
                    "viewer_role_id": self.role, "snapshot_seq": self.snapshot}, "read_requests": []}
        else:
            identity = envelope["operation_id"]
            if identity in self.receipts:
                if self.receipts[identity]["intent_fingerprint"] != intent_proof(function, envelope["arguments"]):
                    if self.lose_conflict_reply:
                        self.lose_conflict_reply = False
                        raise AgentClientError("WorldUnavailable", "Original refusal reply was lost.", 503)
                    raise AgentClientError("OperationConflict", "ID belongs to another intent.", 409)
                return deepcopy(self.receipts[identity]) | {"replayed": True}
            self.sent.append((function, deepcopy(envelope)))
            if function == "grove.join":
                self.joined = True
            receipt = {"ok": True, "operation_id": identity, "function_id": function,
                "function_version": 1, "actor_role_id": self.role, "credential_id": "credential", "commit_seq": 8,
                "intent_fingerprint": intent_proof(function, envelope["arguments"]),
                "result": {"player": {"id": self.role, "version": 3}, "changed": True}}
            self.receipts[identity] = receipt
            if self.lose_reply:
                self.lose_reply = False
                raise AgentClientError("WorldUnavailable", "Reply was lost after commit.", 503)
            return deepcopy(receipt)
        return {"ok": True, "read_only": True, "function_id": function,
                "function_version": 1, "snapshot_seq": self.snapshot, "result": deepcopy(value)}

    def http(self, request):
        self.assert_header(request)
        if request.url.path == "/v1/bootstrap":
            return httpx.Response(200, json=self.bootstrap())
        if request.url.path == "/v1/discover":
            return httpx.Response(200, json={"functions": self.descriptors, "has_more": False, "next_cursor": None})
        if request.url.path.startswith("/v1/receipts/"):
            identity = request.url.path.rsplit("/", 1)[1]
            if identity not in self.receipts:
                return httpx.Response(404, json={"error": "ReceiptNotFound", "message": "Not committed at lookup.", "retryable": False})
            return httpx.Response(200, json=self.receipts[identity])
        function = request.url.path.split("/")[-2]
        try:
            payload = self.invoke(function, json.loads(request.content))
        except AgentClientError as exc:
            return httpx.Response(exc.status, json=exc.public_payload())
        return httpx.Response(200, json=payload)

    @staticmethod
    def assert_header(request):
        if request.headers.get("authorization") != "Bearer private-test-credential":
            raise AssertionError("HTTP transport did not use its configured credential")

    async def mcp(self, name, arguments):
        if name == "world.get_receipt":
            receipt = self.receipts.get(arguments["operation_id"])
            if receipt is None:
                return {"isError": True, "structuredContent": {"error": "ReceiptNotFound",
                    "message": "Not committed at lookup.", "retryable": False}}
            return {"isError": False, "structuredContent": deepcopy(receipt)}
        if name == "world.bootstrap":
            value = self.bootstrap()
        elif name == "world.discover":
            value = {"functions": self.descriptors, "has_more": False, "next_cursor": None}
        else:
            value = self.invoke(name, arguments)
        return {"isError": False, "structuredContent": value}


class GroveAgentClientTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.world = PublicWorldPort()
        original = httpx.AsyncClient
        self.patch = patch("agent_world.grove_agent_ports.httpx.AsyncClient",
            side_effect=lambda **kwargs: original(**kwargs, transport=httpx.MockTransport(self.world.http)))
        self.patch.start()
        self.addCleanup(self.patch.stop)
        self.client = GroveAgentClient(HttpGroveTransport("https://world.example", "private-test-credential"),
            self.world.role, "configured-world-endpoint", journal=MemoryOperationJournal())

    async def enter(self):
        found = await self.client.discover()
        await self.client.write("grove.join", {"name": "Reader"}, observation_id=found["observation_id"])
        return found

    async def test_http_discovery_first_entry_and_public_output_schema(self):
        found = await self.client.discover()
        self.assertFalse(self.world.joined)
        self.assertEqual(found["bootstrap_observation"]["world_entry_state"], found["bootstrap"]["world_entry_state"])
        joined = await self.client.write("grove.join", {"name": "Reader"}, observation_id=found["observation_id"])
        self.assertTrue(self.world.joined)
        tool = next(row for row in self.client.tools() if row["name"] == "grove_join")
        self.assertNotIn("execution_segment_id", tool["inputSchema"]["properties"])
        self.assertTrue(Draft202012Validator(tool["outputSchema"]).is_valid(joined))
        self.assertNotIn("private-test-credential", json.dumps(joined))

    async def test_fixed_observation_cannot_upgrade_from_parallel_read(self):
        await self.enter()
        old = await self.client.read("grove.inspect", {"entity_id": "object"})
        self.world.version = 99
        await self.client.read("grove.inspect", {"entity_id": "object"})
        await self.client.write("grove.rule_action", {"target_id": "object", "amount": 1}, observation_id=old["observation_id"])
        self.assertEqual(self.world.sent[-1][1]["arguments"]["expected_target_version"], 2)

    async def test_committed_presentation_failure_is_not_unknown_and_is_never_replayed(self):
        found = await self.client.discover()
        with patch.object(self.client, "present", side_effect=AgentClientError("ToolOutputTooLarge", "Feedback unavailable.", 413)):
            with self.assertRaises(AgentClientError) as failure:
                await self.client.write("grove.join", {"name": "Reader"}, observation_id=found["observation_id"],
                                        operation_id="confirmed-entry")
        self.assertEqual(failure.exception.code, "ReadbackPending")
        self.assertTrue(failure.exception.world_details["operation_committed"])
        self.assertEqual(self.client.pending_operations, [])
        self.assertEqual(self.client.journal.load(), [])
        self.assertEqual(await self.client.recover(replay=True), [])
        receipt = await self.client.transport.receipt("confirmed-entry")
        self.assertEqual(receipt["function_id"], "grove.join")
        self.assertEqual(len(self.world.sent), 1)

    async def test_host_false_refuses_original_new_write_and_replay(self):
        found = await self.client.discover()
        self.client.before_write = lambda record: False
        with self.assertRaises(AgentClientError) as rejected:
            await self.client.write("grove.join", {"name": "Reader"}, observation_id=found["observation_id"])
        self.assertEqual(rejected.exception.code, "WriteDenied")
        self.assertFalse(self.world.sent)
        self.assertFalse(self.client.pending_operations)
        self.client.before_write = None
        original = self.client.transport.invoke
        async def unavailable(function, envelope):
            raise AgentClientError("WorldUnavailable", "No result.", 503)
        self.client.transport.invoke = unavailable
        with self.assertRaises(AgentClientError):
            await self.client.write("grove.join", {"name": "Reader"}, observation_id=found["observation_id"])
        pending = self.client.pending_operations
        self.client.transport.invoke = original
        async def refuse(record):
            return False
        self.client.before_write = refuse
        with self.assertRaises(AgentClientError) as replay_rejected:
            await self.client.recover(replay=True)
        self.assertEqual(replay_rejected.exception.code, "WriteDenied")
        self.assertEqual(self.client.pending_operations, pending)
        self.assertFalse(self.world.sent)

    async def test_recovery_checks_original_authenticated_world_scope_before_receipt(self):
        await self.enter()
        observed = await self.client.read("grove.inspect", {"entity_id": "object"})
        self.world.lose_reply = True
        with self.assertRaises(AgentClientError):
            await self.client.write("grove.rule_action", {"target_id": "object", "amount": 2},
                                    observation_id=observed["observation_id"], operation_id="scoped-original")
        pending = self.client.pending_operations
        self.world.universe = "other-world"
        restarted = GroveAgentClient(McpGroveTransport(self.world.mcp), self.world.role,
            "configured-world-endpoint", journal=self.client.journal)
        with self.assertRaises(AgentClientError) as mismatch:
            await restarted.recover(replay=True)
        self.assertEqual(mismatch.exception.code, "IdentityScopeMismatch")
        self.assertEqual(restarted.pending_operations, pending)
        self.assertEqual(len(self.world.sent), 2)
        self.world.universe = "same-world"
        self.world.world_version = 2
        self.assertEqual((await restarted.recover())[0]["status"], "committed")

    async def test_world_code_change_closes_new_writes_until_rediscovery(self):
        await self.enter()
        observed = await self.client.read("grove.inspect", {"entity_id": "object"})
        sent = len(self.world.sent)
        self.world.world_version = 2
        with self.assertRaises(AgentClientError) as changed:
            await self.client.read("grove.inspect", {"entity_id": "object"})
        self.assertEqual(changed.exception.code, "WorldVersionMismatch")
        with self.assertRaises(AgentClientError) as closed:
            await self.client.write("grove.rule_action", {"target_id": "object", "amount": 1},
                                    observation_id=observed["observation_id"])
        self.assertEqual(closed.exception.code, "AgentDiscoveryRequired")
        self.assertEqual(len(self.world.sent), sent)
        await self.client.discover()
        fresh = await self.client.read("grove.inspect", {"entity_id": "object"})
        await self.client.write("grove.rule_action", {"target_id": "object", "amount": 1},
                                observation_id=fresh["observation_id"])
        self.world.world_code = "different-code"
        with self.assertRaises(AgentClientError) as before_send:
            await self.client.write("grove.rule_action", {"target_id": "object", "amount": 1},
                                    observation_id=fresh["observation_id"])
        self.assertEqual(before_send.exception.code, "WorldVersionMismatch")

    async def test_older_query_never_replaces_newer_fixed_observation(self):
        await self.enter()
        self.world.snapshot = 11
        latest = await self.client.read("grove.inspect", {"entity_id": "object"})
        bundle = self.client.observations.get(latest["observation_id"])
        self.world.snapshot = 10
        self.world.version = 99
        with self.assertRaises(AgentClientError) as older:
            await self.client.read("grove.inspect", {"entity_id": "object"}, base_observation_id=latest["observation_id"])
        self.assertEqual(older.exception.code, "StaleObservation")
        self.assertEqual(self.client.observations.get(latest["observation_id"]), bundle)
        self.assertEqual(self.client.bind("grove.rule_action", {"target_id": "object", "amount": 1},
                                        latest["observation_id"])["expected_target_version"], 2)

    async def test_persistent_unknown_blocks_new_write_and_recovers_receipt_over_mcp(self):
        temporary = tempfile.TemporaryDirectory(prefix="grove-sdk-")
        self.addCleanup(temporary.cleanup)
        journal = JsonOperationJournal(Path(temporary.name) / "operations.json")
        self.client.journal = journal
        await self.enter()
        observed = await self.client.read("grove.inspect", {"entity_id": "object"})
        self.world.lose_reply = True
        with self.assertRaises(AgentClientError):
            await self.client.write("grove.rule_action", {"target_id": "object", "amount": 2},
                                    observation_id=observed["observation_id"], operation_id="original")
        original = deepcopy(self.client.pending_operations[0])
        with self.assertRaises(AgentClientError) as blocked:
            await self.client.write("grove.rule_action", {"target_id": "object", "amount": 3},
                                    observation_id=observed["observation_id"])
        self.assertEqual(blocked.exception.code, "UnknownWrite")
        restarted = GroveAgentClient(McpGroveTransport(self.world.mcp), self.world.role,
            "configured-world-endpoint", journal=journal)
        recovered = await restarted.recover()
        self.assertEqual(recovered[0]["receipt"]["operation_id"], original["envelope"]["operation_id"])
        self.assertEqual(len([row for row in self.world.sent if row[0] == "grove.rule_action"]), 1)
        self.assertEqual(journal.load(), [])
        with self.assertRaises(Exception) as missing:
            restarted.observations.get(observed["observation_id"])
        self.assertEqual(missing.exception.code, "ObservationRequired")

    async def test_missing_receipt_preserves_unknown_until_explicit_original_replay(self):
        await self.enter()
        observed = await self.client.read("grove.inspect", {"entity_id": "object"})
        original = self.client.transport.invoke
        async def unavailable(function, envelope):
            raise AgentClientError("WorldUnavailable", "No result.", 503)
        self.client.transport.invoke = unavailable
        with self.assertRaises(AgentClientError):
            await self.client.write("grove.rule_action", {"target_id": "object", "amount": 4},
                                    observation_id=observed["observation_id"], operation_id="unresolved")
        pending = self.client.pending_operations
        self.client.transport.invoke = original
        self.assertEqual((await self.client.recover())[0]["status"], "unknown")
        self.assertEqual(self.client.pending_operations, pending)
        self.assertEqual((await self.client.recover(replay=True))[0]["status"], "committed")
        self.assertEqual(self.world.sent[-1][1], pending[0]["envelope"])

    async def test_journal_failure_prevents_send_and_keeps_original_for_recovery(self):
        await self.enter()
        observed = await self.client.read("grove.inspect", {"entity_id": "object"})
        save = self.client.journal.save
        self.client.journal.save = lambda records: (_ for _ in ()).throw(OSError("unavailable"))
        sent = len(self.world.sent)
        with self.assertRaises(AgentClientError) as failure:
            await self.client.write("grove.rule_action", {"target_id": "object", "amount": 1}, observation_id=observed["observation_id"])
        self.assertEqual(failure.exception.code, "JournalUnavailable")
        self.assertEqual(len(self.world.sent), sent)
        self.assertEqual(len(self.client.pending_operations), 1)
        self.client.journal.save = save
        self.assertEqual((await self.client.recover())[0]["status"], "unknown")

    async def test_modified_original_arguments_fail_journal_reopen(self):
        await self.enter()
        observed = await self.client.read("grove.inspect", {"entity_id": "object"})
        async def unavailable(function, envelope):
            raise AgentClientError("WorldUnavailable", "No result.", 503)
        self.client.transport.invoke = unavailable
        with self.assertRaises(AgentClientError):
            await self.client.write("grove.rule_action", {"target_id": "object", "amount": 4}, observation_id=observed["observation_id"])
        records = self.client.journal.load()
        records[0]["envelope"]["arguments"]["amount"] = 9
        self.client.journal.save(records)
        with self.assertRaises(AgentClientError) as corrupt:
            GroveAgentClient(McpGroveTransport(self.world.mcp), self.world.role,
                "configured-world-endpoint", journal=self.client.journal)
        self.assertEqual(corrupt.exception.code, "CorruptJournal")

    async def test_lost_operation_conflict_is_rejected_by_receipt_without_write_authorization(self):
        await self.enter()
        observed = await self.client.read("grove.inspect", {"entity_id": "object"})
        await self.client.write("grove.rule_action", {"target_id": "object", "amount": 1},
                                observation_id=observed["observation_id"], operation_id="occupied")
        self.world.lose_conflict_reply = True
        with self.assertRaises(AgentClientError) as lost:
            await self.client.write("grove.rule_action", {"target_id": "object", "amount": 2},
                                    observation_id=observed["observation_id"], operation_id="occupied")
        self.assertEqual(lost.exception.code, "WorldUnavailable")
        pending = self.client.pending_operations
        self.assertEqual(pending[0]["envelope"]["arguments"]["amount"], 2)
        sent = deepcopy(self.world.sent)
        self.client.before_write = lambda record: False
        self.client.transport.invoke = AsyncMock(side_effect=AssertionError("Recovery must remain receipt-only"))
        result = await self.client.recover()
        self.assertEqual(result[0]["status"], "rejected")
        self.assertEqual(result[0]["error"]["error"], "OperationConflict")
        self.assertEqual(self.client.journal.load(), [])
        self.assertEqual(self.world.sent, sent)
        self.client.transport.invoke.assert_not_awaited()

    async def test_missing_intent_evidence_preserves_unknown_and_explicit_replay_obeys_host(self):
        await self.enter()
        observed = await self.client.read("grove.inspect", {"entity_id": "object"})
        self.world.lose_reply = True
        with self.assertRaises(AgentClientError):
            await self.client.write("grove.rule_action", {"target_id": "object", "amount": 1},
                                    observation_id=observed["observation_id"], operation_id="legacy")
        pending = self.client.pending_operations
        receipt = deepcopy(self.world.receipts["legacy"])
        receipt.pop("intent_fingerprint")
        self.client.transport.receipt = AsyncMock(return_value=receipt)
        with self.assertRaises(AgentClientError) as unproven:
            await self.client.recover()
        self.assertEqual(unproven.exception.code, "ReceiptMismatch")
        self.assertEqual(self.client.pending_operations, pending)
        self.client.before_write = lambda record: False
        with self.assertRaises(AgentClientError) as stopped:
            await self.client.recover(replay=True)
        self.assertEqual(stopped.exception.code, "WriteDenied")
        self.assertEqual(self.client.pending_operations, pending)
        self.client.before_write = None
        original = self.client.transport.invoke
        self.client.transport.invoke = AsyncMock(wraps=original)
        self.assertEqual((await self.client.recover(replay=True))[0]["status"], "committed")
        self.client.transport.invoke.assert_awaited_once_with("grove.rule_action", pending[0]["envelope"])

    async def test_untrusted_receipt_identity_cannot_turn_a_different_fingerprint_into_rejection(self):
        await self.enter()
        observed = await self.client.read("grove.inspect", {"entity_id": "object"})
        self.world.lose_reply = True
        with self.assertRaises(AgentClientError):
            await self.client.write("grove.rule_action", {"target_id": "object", "amount": 2},
                                    observation_id=observed["observation_id"], operation_id="identity")
        pending = self.client.pending_operations
        receipt = deepcopy(self.world.receipts["identity"])
        receipt["actor_role_id"] = "another-role"
        receipt["intent_fingerprint"] = intent_proof("grove.rule_action", {"amount": 9})
        self.client.transport.receipt = AsyncMock(return_value=receipt)
        with self.assertRaises(AgentClientError) as invalid:
            await self.client.recover()
        self.assertEqual(invalid.exception.code, "ReceiptMismatch")
        self.assertEqual(self.client.pending_operations, pending)
