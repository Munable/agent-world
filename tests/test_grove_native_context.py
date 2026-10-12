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
        self.assertGreater(actual["current_tool_input_bytes"], actual["historical_tool_history_budget_bytes"])
        for invalid in (-1, True, "120000"):
            self.loaded(self.proof(projection={**projection, "omitted_historical_tool_input_bytes": invalid}))
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


if __name__ == "__main__":
    unittest.main()
