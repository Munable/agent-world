"""Managed native hook proof gates; no host, model or world service is launched."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock

from agent_world.grove_connector import (
    ConnectorError, ConnectorManager, GroveSession, NATIVE_CONTEXT_HOOK, NATIVE_CONTEXT_VERSION, save_json,
)


class GroveNativeContextTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory(prefix="grove-context-")
        self.addCleanup(directory.cleanup)
        manager = ConnectorManager(world_url="http://127.0.0.1:8890", local_url="http://127.0.0.1:8891",
            storage=Path(directory.name), binary=Path("unused"), wrapper=None)
        self.session = GroveSession(manager, {"role_id": "same-user", "access_mode": "control"},
            "private-world-bearer", "User", "provider/model")
        self.session.session_id = "same-session"
        self.plugin = self.session.configure_native_context()

    def proof(self, **changes):
        options = self.plugin[1]
        guard = {"status": "installed", "runtime": "ai_sdk", "provider_id": "provider", "model_id": "model",
            "historical_message_budget_bytes": 65536, "request_budget_bytes": 131072, "native_llm_allowed": False,
            "installed_at_ms": 123, "attempt_count": 0, "sent_count": 0, "blocked_count": 0}
        projection = changes.get("projection")
        if isinstance(projection, dict):
            body = json.dumps({"messages": [{"role": "user", "content": "fixture"}], "system": [], "tools": []})
            guard.update(status="sent", session_id=projection.get("session_id"),
                current_request_message_id=projection.get("current_request_message_id"), observed_at_ms=300,
                request_sequence=1, attempt_count=1, sent_count=1, body_bytes=len(body.encode()),
                body_sha256=hashlib.sha256(body.encode()).hexdigest(), system_bytes=2, messages_bytes=40, tools_bytes=2,
                rejection=None)
        return {"status": "loaded", "version": NATIVE_CONTEXT_VERSION, "hook": NATIVE_CONTEXT_HOOK,
            "nonce": options["nonce"], "role_id": options["role_id"], "source_sha256": options["source_sha256"],
            "process_id": 42, "loaded_at_ms": 123, "sdk_guard": guard, **changes}

    def loaded(self, proof=None):
        save_json(Path(self.plugin[1]["diagnostic_path"]), proof or self.proof())
        self.session.host = AsyncMock(side_effect=lambda method, path, **kwargs:
            {"plugin": [deepcopy(self.plugin)]} if path == "/config" else ["read", "edit"])

    def projection(self, **changes):
        return {"session_id": "same-session", "current_request_message_id": "this-request", "observed_at_ms": 300,
            "omitted_assistant_parts": 2, "omitted_assistant_characters": 20, "reduced_tool_parts": 1,
            "historical_tool_characters_before": 50000, "historical_tool_characters_after": 1200,
            "omitted_machine_snapshot_parts": 1, "omitted_machine_snapshot_characters": 800,
            "stored_originals_changed": False, "character_unit": "utf16_code_units", **changes}

    async def test_configuration_without_actual_native_initialization_proof_does_not_admit_a_model(self):
        self.session.host = AsyncMock(side_effect=lambda method, path, **kwargs:
            {"plugin": [deepcopy(self.plugin)]} if path == "/config" else ["read"])
        with self.assertRaises(ConnectorError) as rejected:
            await self.session.verify_native_context(initialize=True)
        self.assertEqual(rejected.exception.code, "NativeContextUnavailable")
        self.assertEqual(self.session.inference_count, 0)
        self.assertEqual(self.session.session_id, "same-session")
        self.assertEqual([call.args for call in self.session.host.await_args_list],
            [("GET", "/config"), ("GET", "/experimental/tool/ids")])

    async def test_valid_current_native_proof_is_private_and_does_not_replace_session_or_history(self):
        self.loaded()
        self.session.messages = [{"role": "user", "text": "原话"}]
        before = deepcopy(self.session.messages)
        await self.session.verify_native_context(initialize=True)
        self.assertEqual(self.session.native_context["status"], "loaded")
        self.assertEqual(self.session.messages, before)
        self.assertEqual((self.session.inference_count, self.session.session_id), (0, "same-session"))
        public = json.dumps(self.session.state())
        self.assertNotIn(self.plugin[1]["nonce"], public)
        self.assertNotIn("private-world-bearer", public)
        self.assertNotIn(str(self.session.profile), json.dumps(self.session.native_context))

    async def test_stale_nonce_wrong_source_foreign_projection_and_an_extra_plugin_are_rejected(self):
        for changes in ({"nonce": "0" * 32}, {"source_sha256": "0" * 64},
                        {"role_id": "other-user"}, {"process_id": False},
                        {"projection": {"session_id": "other-session"}}):
            with self.subTest(changes=changes):
                self.loaded(self.proof(**changes))
                with self.assertRaises(ConnectorError):
                    await self.session.verify_native_context()
        self.loaded()
        self.session.host = AsyncMock(return_value={"plugin": [self.plugin, "an-unapproved-plugin"]})
        with self.assertRaises(ConnectorError):
            await self.session.verify_native_context()

    async def test_returned_request_requires_its_own_fresh_transform_proof(self):
        for projection in (None, self.projection(current_request_message_id="previous-request"),
                           self.projection(observed_at_ms=199)):
            with self.subTest(projection=projection):
                self.loaded(self.proof(**({"projection": projection} if projection is not None else {})))
                with self.assertRaises(ConnectorError):
                    await self.session.verify_native_context(request_message_id="this-request", request_started_at_ms=200)
        self.loaded(self.proof(projection=self.projection()))
        await self.session.verify_native_context(request_message_id="this-request", request_started_at_ms=200)
        self.assertEqual(self.session.native_context["projection"], self.projection())
        self.assertEqual(self.session.inference_count, 0)

    async def test_current_request_rejects_missing_foreign_and_stale_sdk_guards(self):
        valid = self.proof(projection=self.projection())
        for guard in (None, {**valid["sdk_guard"], "model_id": "other-model"},
                      {**valid["sdk_guard"], "current_request_message_id": "previous-request"},
                      {**valid["sdk_guard"], "observed_at_ms": 199}):
            with self.subTest(guard=guard):
                self.loaded({**valid, "sdk_guard": guard})
                with self.assertRaises(ConnectorError) as rejected:
                    await self.session.verify_native_context(request_message_id="this-request", request_started_at_ms=200)
                self.assertEqual(rejected.exception.code, "NativeContextUnavailable")
                self.assertEqual(self.session.inference_count, 0)

    async def test_blocked_sdk_request_pauses_and_preserves_its_safe_diagnostic(self):
        proof = self.proof(projection=self.projection())
        proof["sdk_guard"].update(status="blocked", sent_count=0, blocked_count=1,
            rejection="historical_messages_over_budget")
        self.loaded(proof)
        with self.assertRaises(ConnectorError) as rejected:
            await self.session.verify_native_context(request_message_id="this-request", request_started_at_ms=200)
        self.assertEqual(rejected.exception.code, "NativeContextBudgetExceeded")
        self.assertEqual(self.session.native_context["sdk_guard"]["status"], "blocked")
        self.assertEqual(self.session.native_context["sdk_guard"]["body_sha256"], proof["sdk_guard"]["body_sha256"])
        self.assertEqual(self.session.native_context["projection"], self.projection())
        self.assertEqual((self.session.inference_count, self.session.session_id), (0, "same-session"))

    async def test_deployed_file_tampering_and_linked_proof_are_rejected(self):
        self.loaded()
        target = self.session.profile / "grove-native-context.mjs"
        original = target.read_bytes()
        target.write_bytes(original + b"\n// changed\n")
        with self.assertRaises(ConnectorError):
            await self.session.verify_native_context()
        self.assertEqual(self.session.host.await_count, 0)
        target.write_bytes(original)
        diagnostic = Path(self.plugin[1]["diagnostic_path"])
        kept = diagnostic.with_name("kept-original-proof.json")
        diagnostic.rename(kept)
        diagnostic.symlink_to(kept)
        with self.assertRaises(ConnectorError):
            await self.session.verify_native_context()

    async def test_history_budget_counters_keep_current_input_separate_and_reject_invalid_bytes(self):
        projection = self.projection(historical_tool_history_budget_bytes=65536,
            historical_tool_history_bytes_after=18000, omitted_historical_tool_parts=12,
            omitted_historical_tool_input_bytes=120000, omitted_historical_tool_output_bytes=2200,
            current_tool_input_bytes=80000, protected_message_bytes=80500,
            managed_message_bytes_after=98500, historical_message_budget_bytes=65536,
            historical_message_allowance_bytes=17804, fixed_request_reserve_bytes=32768, byte_unit="utf8",
            request_projection_target_bytes=131072, sdk_fixed_request_bytes=30000, sdk_message_expansion_bytes=100,
            sdk_envelope_source="previous_matching_sdk_body",
            budget_scope="historical_native_inference_projection")
        self.loaded(self.proof(projection=projection))
        await self.session.verify_native_context(request_message_id="this-request", request_started_at_ms=200)
        actual = self.session.native_context["projection"]
        for key in ("historical_tool_history_budget_bytes", "historical_tool_history_bytes_after",
                    "omitted_historical_tool_parts", "omitted_historical_tool_input_bytes",
                    "omitted_historical_tool_output_bytes", "current_tool_input_bytes", "protected_message_bytes",
                    "managed_message_bytes_after", "historical_message_budget_bytes", "historical_message_allowance_bytes",
                    "fixed_request_reserve_bytes", "byte_unit", "budget_scope"):
            self.assertEqual(actual[key], projection[key])
        for key in ("request_projection_target_bytes", "sdk_fixed_request_bytes", "sdk_message_expansion_bytes",
                    "sdk_envelope_source"):
            self.assertEqual(actual[key], projection[key])
        self.assertGreater(actual["current_tool_input_bytes"], actual["historical_tool_history_budget_bytes"])
        for invalid in (-1, True, "120000"):
            self.loaded(self.proof(projection={**projection, "omitted_historical_tool_input_bytes": invalid}))
            with self.assertRaises(ConnectorError):
                await self.session.verify_native_context()
        for key in ("request_projection_target_bytes", "sdk_fixed_request_bytes", "sdk_message_expansion_bytes"):
            for invalid in (-1, True, "30000"):
                self.loaded(self.proof(projection={**projection, key: invalid}))
                with self.assertRaises(ConnectorError):
                    await self.session.verify_native_context()

    def test_owned_source_is_exact_bundled_code_and_existing_memory_is_unchanged(self):
        self.session.remember()
        original_memory = self.session.memory_file.read_bytes()
        another = self.session.configure_native_context()
        target = self.session.profile / "grove-native-context.mjs"
        self.assertEqual(hashlib.sha256(target.read_bytes()).hexdigest(), another[1]["source_sha256"])
        self.assertEqual(target.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.session.memory_file.read_bytes(), original_memory)
        self.assertEqual(another[1]["session_id"], "same-session")
        self.assertTrue(another[0].startswith("file://"))

    def yielded_proof(self):
        owner = {"purpose": "execution", "request_id": "grove-request-" + "a" * 32,
            "task_id": "grove-task-" + "b" * 32, "segment_id": "grove-segment-" + "c" * 32}
        projection = self.projection(agent="shared-grove-executor", request_purpose="execution",
            world_write_authorized=True, request_id=owner["request_id"], execution_task_id=owner["task_id"],
            execution_segment_id=owner["segment_id"], current_terminal_tool_parts=1, current_unfinished_tool_parts=0)
        proof = self.proof(projection=projection)
        body = json.dumps({"messages": [{"role": "user", "content": "x" * 100000}]})
        proof["sdk_guard"].update(status="yielded", agent="shared-grove-executor", body_bytes=len(body.encode()),
            body_sha256=hashlib.sha256(body.encode()).hexdigest(), rejection="execution_segment_budget", sent_count=0,
            segment_yield={"safe": True, "reason": "request_headroom", "request_id": owner["request_id"],
                "task_id": owner["task_id"], "segment_id": owner["segment_id"], "body_bytes": len(body.encode()),
                "current_terminal_tool_parts": 1, "current_unfinished_tool_parts": 0})
        return proof, owner

    async def test_verified_owned_yield_is_typed_and_never_a_model_completion(self):
        proof, owner = self.yielded_proof()
        self.loaded(proof)
        with self.assertRaises(ConnectorError) as yielded:
            await self.session.verify_native_context(request_message_id="this-request", request_started_at_ms=200,
                inference_owner=owner)
        self.assertEqual(yielded.exception.code, "ExecutionSegmentYield")
        self.assertEqual(yielded.exception.execution_outcome,
            {"stage": "native_segment_yield", **proof["sdk_guard"]["segment_yield"]})
        self.assertEqual((self.session.inference_count, self.session.completion_count, self.session.text), (0, 0, ""))
        self.assertEqual(self.session.session_id, "same-session")

    async def test_verified_yield_allows_loading_checks_but_cannot_authorize_the_next_request(self):
        proof, owner = self.yielded_proof()
        self.loaded(proof)
        self.session.execution_controller.state.update(task_id=owner["task_id"], segment_id=owner["segment_id"],
            goal="Continue the original goal", status="paused", requests_used=3, max_requests=4,
            actions_count=1, max_actions=4)
        original_execution = deepcopy(self.session.execution)
        diagnostic = Path(self.plugin[1]["diagnostic_path"])
        original_diagnostic = diagnostic.read_bytes()
        with self.assertRaises(ConnectorError) as yielded:
            await self.session.verify_native_context(request_message_id="this-request", request_started_at_ms=200,
                inference_owner=owner)
        self.assertEqual(yielded.exception.code, "ExecutionSegmentYield")
        for initialize in (False, True):
            await self.session.verify_native_context(initialize=initialize)
            self.assertEqual(self.session.native_context["status"], "loaded")
            self.assertEqual(self.session.native_context["sdk_guard"]["status"], "yielded")
            self.assertNotIn("segment_yield", self.session.native_context["sdk_guard"])
        self.assertEqual(diagnostic.read_bytes(), original_diagnostic)
        next_owner = {**owner, "request_id": "grove-request-" + "d" * 32,
            "segment_id": "grove-segment-" + "e" * 32}
        for request_id, started_at, request_owner in (
                ("next-request", 400, next_owner), ("this-request", 301, owner),
                ("this-request", 200, next_owner)):
            with self.subTest(request_id=request_id, started_at=started_at, request_owner=request_owner):
                with self.assertRaises(ConnectorError) as stale:
                    await self.session.verify_native_context(request_message_id=request_id,
                        request_started_at_ms=started_at, inference_owner=request_owner)
                self.assertEqual(stale.exception.code, "NativeContextUnavailable")
        next_projection = self.projection(current_request_message_id="next-request", observed_at_ms=450,
            agent="shared-grove-executor", request_purpose="execution", world_write_authorized=True,
            request_id=next_owner["request_id"], execution_task_id=next_owner["task_id"],
            execution_segment_id=next_owner["segment_id"])
        next_proof = self.proof(projection=next_projection)
        next_proof["sdk_guard"].update(observed_at_ms=450, request_sequence=2)
        self.loaded(next_proof)
        await self.session.verify_native_context(request_message_id="next-request", request_started_at_ms=400,
            inference_owner=next_owner)
        self.assertEqual(self.session.native_context["sdk_guard"]["status"], "sent")
        self.assertEqual(self.session.native_context["projection"]["current_request_message_id"], "next-request")
        self.assertEqual(self.session.execution, original_execution)
        self.assertEqual((self.session.session_id, self.session.inference_count, self.session.completion_count,
            self.session.text), ("same-session", 0, 0, ""))

    def test_yield_error_projection_keeps_only_typed_bounded_evidence(self):
        proof, _ = self.yielded_proof()
        outcome = {"stage": "native_segment_yield", **proof["sdk_guard"]["segment_yield"],
            "upstream_body": "private-provider-content", "provider_credential": "private-token"}
        error = ConnectorError("ExecutionSegmentYield", "Safe native boundary.", 409, execution_outcome=outcome)
        self.assertEqual(error.public_payload()["execution_outcome"],
            {"stage": "native_segment_yield", **proof["sdk_guard"]["segment_yield"]})
        self.assertNotIn("private-provider-content", json.dumps(error.public_payload()))
        ordinary = ConnectorError("ModelRequestFailed", "Provider failure.", 502, execution_outcome=outcome)
        for key in ("safe", "reason", "request_id", "body_bytes", "current_terminal_tool_parts",
                    "current_unfinished_tool_parts"):
            self.assertNotIn(key, ordinary.execution_outcome)
        invalid = ConnectorError("ExecutionSegmentYield", "Invalid evidence.", 409,
            execution_outcome={**outcome, "safe": "true", "reason": "arbitrary-upstream-text",
                "request_id": "another-request", "body_bytes": True,
                "current_terminal_tool_parts": -1, "current_unfinished_tool_parts": False})
        for key in ("safe", "reason", "request_id", "body_bytes", "current_terminal_tool_parts",
                    "current_unfinished_tool_parts"):
            self.assertNotIn(key, invalid.execution_outcome)

    async def test_yield_requires_exact_current_owner_and_terminal_tool_proof(self):
        mutations = [lambda proof, owner: owner.update(purpose="explain"),
            lambda proof, owner: owner.update(task_id="another-task"),
            lambda proof, owner: owner.update(request_id="another-request"),
            lambda proof, owner: proof["projection"].update(request_purpose="explain"),
            lambda proof, owner: proof["projection"].update(world_write_authorized=False),
            lambda proof, owner: proof["sdk_guard"]["segment_yield"].update(current_terminal_tool_parts=0),
            lambda proof, owner: proof["sdk_guard"]["segment_yield"].update(current_unfinished_tool_parts=1),
            lambda proof, owner: proof["sdk_guard"]["segment_yield"].update(current_unfinished_tool_parts=False),
            lambda proof, owner: proof["sdk_guard"]["segment_yield"].update(safe=False),
            lambda proof, owner: proof["sdk_guard"].update(agent="shared-grove")]
        for mutation in mutations:
            proof, owner = self.yielded_proof()
            mutation(proof, owner)
            self.loaded(proof)
            with self.assertRaises(ConnectorError) as rejected:
                await self.session.verify_native_context(request_message_id="this-request", request_started_at_ms=200,
                    inference_owner=owner)
            self.assertEqual(rejected.exception.code, "NativeContextUnavailable")
            self.assertEqual(self.session.completion_count, 0)


if __name__ == "__main__":
    unittest.main()
