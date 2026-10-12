"""Read existing native evidence without world writes or observation authority."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, Mock, patch

import httpx
from jsonschema import Draft202012Validator

from agent_world.grove_agent_observation import _agent_tool_text, _agent_tool_text_fits
from agent_world.grove_connector import ConnectorManager, GroveSession, MAX_WORLD_CALLS
from agent_world.grove_connector_errors import ConnectorError
from agent_world.grove_native_history import NativeToolHistory, TOOL_NAME, TOOL_SCHEMA


SESSION = "ses_owned"
FORMAL = {"grove_interact": "grove_world_grove_interact",
          "grove_inspect": "grove_world_grove_inspect",
          "execution_checkpoint": "grove_world_execution_checkpoint"}


def user_message(identity="msg_user", goal="看看它怎样变化，然后继续探索"):
    return {"info": {"id": identity, "sessionID": SESSION, "role": "user"}, "parts": [
        {"type": "text", "text": goal},
        {"type": "text", "text": "CURRENT FACTS\n不是用户目标"},
        {"type": "text", "text": "PRIVATE CONTEXT\n不是用户目标"},
        {"type": "text", "text": "合成说明", "synthetic": True}]}


def tool_message(identity="msg_action", part_id="prt_action", output=None,
                 *, tool="grove_world_grove_interact", inputs=None, status="completed", ended=20):
    output = output if output is not None else {"ok": True, "operation_id": "op_source", "commit_seq": 14,
        "result": {"entity": {"id": "cloth", "version": 2, "matter": [
            {"material": "water", "slot": "surface", "quantity_u": 6}]},
            "viewport": {"clock": {"sequence": 10}}},
        "execution_readback": {"clock": {"sequence": 11}, "observed_at": 30,
            "entities": [{"id": "cloth", "version": 3, "matter": [
                {"material": "water", "slot": "surface", "quantity_u": 2},
                {"material": "water", "slot": "absorbed", "quantity_u": 2}]}]}}
    return {"info": {"id": identity, "sessionID": SESSION, "role": "assistant", "parentID": "msg_user"},
        "parts": [{"id": part_id, "sessionID": SESSION, "messageID": identity, "type": "tool",
                   "tool": tool, "callID": "call_source", "state": {
                       "status": status, "input": inputs if inputs is not None else {"action": "pour", "amount": 6},
                       "output": output if isinstance(output, str) else json.dumps(output, ensure_ascii=False),
                       "time": {"start": 10, "end": ended}}},
                  {"type": "text", "text": "模型声称从来没有吸过水"}]}


class NativeHistoryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.user = user_message()
        self.action = tool_message()
        self.messages = {row["info"]["id"]: row for row in [self.user, self.action]}
        self.page = AsyncMock(return_value={"messages": [self.action], "next_cursor": None})
        self.message = AsyncMock(side_effect=lambda identity: deepcopy(self.messages[identity]))
        self.reader = NativeToolHistory(role_id="owner", session_id=SESSION,
            read_page=self.page, read_message=self.message, formal_tools=FORMAL,
            prompt_prefixes=("CURRENT FACTS", "PRIVATE CONTEXT"))

    async def read(self, **arguments):
        return await self.reader.call({"mode": "read", "message_id": "msg_action", "part_id": "prt_action", **arguments})

    async def test_prefixed_completed_tool_and_original_user_goal_are_discoverable(self):
        result = await self.reader.call({"goal_query": "怎样变化", "tool": "grove_interact"})
        self.assertEqual(len(result["entries"]), 1)
        entry = result["entries"][0]
        self.assertEqual(entry["source"]["tool"], FORMAL["grove_interact"])
        self.assertEqual(entry["source"]["original_operation"]["commit_seq"], 14)
        self.assertEqual(entry["goal_excerpt"], self.user["parts"][0]["text"])
        self.assertFalse(entry["input_complete"])
        self.assertEqual((await self.reader.call({"tool": FORMAL["grove_interact"]}))["entries"],
                         (await self.reader.call({"tool": "grove_interact"}))["entries"])
        for excluded in ("模型声称", "不是用户目标", "合成说明"):
            self.assertEqual((await self.reader.call({"goal_query": excluded}))["entries"], [])

    async def test_builtin_running_and_foreign_sources_are_never_evidence(self):
        builtin = tool_message("msg_builtin", "prt_builtin", tool="bash")
        running = tool_message("msg_running", "prt_running", status="running")
        self.page.return_value = {"messages": [builtin, running, self.action], "next_cursor": None}
        self.assertEqual(len((await self.reader.call({}))["entries"]), 1)
        foreign = deepcopy(self.action)
        foreign["info"]["sessionID"] = "ses_other"
        self.page.return_value = {"messages": [foreign], "next_cursor": None}
        with self.assertRaises(ConnectorError) as rejected:
            await self.reader.call({})
        self.assertEqual(rejected.exception.code, "NativeHistorySourceMismatch")
        self.action["parts"][0]["sessionID"] = "ses_other"
        with self.assertRaises(ConnectorError) as rejected:
            await self.read()
        self.assertEqual(rejected.exception.code, "NativeHistorySourceMismatch")

    async def test_error_and_non_json_outputs_do_not_block_safe_input_recovery(self):
        self.action["parts"][0]["state"].update(status="error", output=None,
            error="upstream headers contain private data", input={"observation_id": "secret", "action": "pour", "amount": 6})
        page = await self.reader.call({"kind": "tool"})
        self.assertEqual(page["entries"][0]["source"]["native_part_status"], "error")
        self.assertFalse(page["entries"][0]["output_available"])
        result = await self.read(section="input")
        self.assertEqual(result["value"], {"action": "pour", "amount": 6})
        self.assertNotIn("headers", json.dumps(result))
        with self.assertRaises(ConnectorError):
            await self.read()
        self.action["parts"][0]["state"].update(status="completed", output="not a JSON envelope")
        self.assertEqual((await self.read(section="input"))["value"]["amount"], 6)
        with self.assertRaises(ConnectorError):
            await self.read()

    async def test_original_user_and_native_summary_text_have_private_source_reads(self):
        part = self.user["parts"][0]
        part.update(id="prt_usertext", sessionID=SESSION, messageID="msg_user")
        summary = {"info": {"id": "msg_summary", "sessionID": SESSION, "role": "assistant", "summary": True},
            "parts": [{"id": "prt_summary", "sessionID": SESSION, "messageID": "msg_summary",
                "type": "text", "text": "模型的历史摘要，不是已发生的事实"}]}
        self.messages["msg_summary"] = summary
        self.page.return_value = {"messages": [self.user, summary], "next_cursor": None}
        entries = (await self.reader.call({"kind": "all"}))["entries"]
        self.assertEqual(len(entries), 2)
        values = [await self.reader.call(entry["read"]["arguments"]) for entry in entries]
        self.assertEqual(values[0]["value"], part["text"])
        self.assertFalse(values[1]["source"]["world_evidence"])
        self.assertTrue(all(value["source"]["private_session_source"] for value in values))
        self.assertEqual(len((await self.reader.call({"kind": "user"}))["entries"]), 1)

    async def test_invalid_tool_error_is_only_a_source_locator(self):
        self.action["parts"][0].update(tool="invalid")
        self.action["parts"][0]["state"].update(status="error",
            input={"tool": "grove_interact", "error": "observation_id=private-token"})
        entry = (await self.reader.call({"kind": "tool"}))["entries"][0]
        self.assertEqual(entry["source"]["tool"], "invalid")
        self.assertFalse(entry["input_available"])
        self.assertIsNone(entry["input_read"])
        self.assertEqual(entry["input_index"], {})
        self.assertNotIn("private-token", json.dumps(entry))
        with self.assertRaises(ConnectorError) as rejected:
            await self.read(section="input")
        self.assertEqual(rejected.exception.code, "NativeHistoryInputUnavailable")

    async def test_empty_filtered_page_keeps_the_official_before_cursor(self):
        self.page.side_effect = [
            {"messages": [self.user], "next_cursor": "opaque_native_before"},
            {"messages": [self.action], "next_cursor": None}]
        first = await self.reader.call({"goal_query": "怎样变化", "tool": "grove_interact"})
        self.assertEqual(first["matches_in_page"], 0)
        self.assertEqual(first["historical_completeness"], "not_established")
        self.assertIsNotNone(first["next_cursor"])
        self.assertFalse(first["original_body_delivered"])
        self.assertEqual(first["content_kind"], "tool_result_location_index")
        self.assertFalse(first["native_listing_end_reached"])
        self.assertEqual(first["next_request"], {"tool": TOOL_NAME, "arguments": {
            "mode": "list", "cursor": first["next_cursor"], "limit": 12}})
        self.assertTrue(_agent_tool_text_fits(_agent_tool_text(first)))
        next_page = await self.reader.call(first["next_request"]["arguments"])
        self.assertEqual(len(next_page["entries"]), 1)
        self.assertFalse(next_page["original_body_delivered"])
        self.assertEqual(next_page["entries"][0]["read"]["arguments"]["mode"], "read")
        self.page.assert_any_await(12, "opaque_native_before")
        self.assertIsNone(next_page["next_cursor"])
        self.assertIsNone(next_page["next_request"])
        self.assertTrue(next_page["native_listing_end_reached"])

    async def test_returned_long_cursor_call_matches_published_schema_and_can_continue(self):
        goal = "界" * 500
        before = "n" * 2000
        self.user["parts"][0]["text"] = goal
        self.page.side_effect = [
            {"messages": [self.user], "next_cursor": before},
            {"messages": [self.action], "next_cursor": None}]
        first = await self.reader.call({"goal_query": goal})
        arguments = first["next_request"]["arguments"]
        self.assertGreater(len(arguments["cursor"]), 5000)
        self.assertEqual(list(Draft202012Validator(TOOL_SCHEMA).iter_errors(arguments)), [])
        self.assertTrue(_agent_tool_text_fits(_agent_tool_text(first)))
        next_page = await self.reader.call(arguments)
        self.assertEqual(len(next_page["entries"]), 1)
        self.assertEqual(next_page["entries"][0]["goal_excerpt"], goal)
        self.page.assert_any_await(12, before)
        self.assertIsNone(next_page["next_request"])

    async def test_within_page_cursor_is_repeatable_and_does_not_admit_later_parts(self):
        second = tool_message(part_id="prt_second")
        self.action["parts"].insert(1, second["parts"][0])
        with patch("agent_world.grove_native_history.time.time", return_value=0.100), \
                patch("agent_world.grove_native_history._agent_tool_text_fits", wraps=_agent_tool_text_fits) as fit:
            first = await self.reader.call({"limit": 1})
        for checked in fit.call_args_list:
            candidate = json.loads(checked.args[0])
            self.assertIn("next_cursor", candidate)
            self.assertIn("next_request", candidate)
        self.assertEqual(first["next_request"], {"tool": TOOL_NAME, "arguments": {
            "mode": "list", "cursor": first["next_cursor"], "limit": 1}})
        self.assertTrue(_agent_tool_text_fits(_agent_tool_text(first)))
        self.action["parts"].append(tool_message(part_id="prt_later", ended=101)["parts"][0])
        follow = first["next_request"]["arguments"]
        repeated = await self.reader.call(follow)
        self.assertEqual(repeated, await self.reader.call(follow))
        self.assertEqual(repeated["entries"][0]["source"]["part_id"], "prt_second")
        self.assertIsNone(repeated["next_cursor"])
        self.assertIsNone(repeated["next_request"])
        self.assertTrue(repeated["native_listing_end_reached"])
        self.assertFalse(repeated["original_body_delivered"])
        self.page.assert_awaited_once_with(12, None)
        with self.assertRaises(ConnectorError):
            await self.reader.call({**follow, "tool": "grove_inspect"})
        other = NativeToolHistory(role_id="another_owner", session_id=SESSION,
            read_page=self.page, read_message=self.message, formal_tools=FORMAL)
        with self.assertRaises(ConnectorError) as rejected:
            await other.call(follow)
        self.assertEqual(rejected.exception.code, "InvalidNativeHistoryCursor")

    async def test_receipt_submission_and_later_readback_remain_separate_facts(self):
        result = await self.read()
        self.assertEqual(result["value"]["result"]["entity"]["matter"][0]["quantity_u"], 6)
        self.assertEqual(result["value"]["execution_readback"]["entities"][0]["matter"][1],
            {"material": "water", "slot": "absorbed", "quantity_u": 2})
        self.assertEqual(result["source"]["submission_viewport_clock"], {"sequence": 10})
        self.assertEqual(result["source"]["execution_readback_time"]["clock"], {"sequence": 11})
        self.assertEqual(result["source"]["execution_readback_time"]["observed_at"], 30)
        self.assertTrue(result["source"]["historical_only"])
        self.assertNotIn("模型声称", json.dumps(result, ensure_ascii=False))

    def install_authority_fixture(self, *, large=False):
        ordinary = {"token": "world-token", "grant": "world-grant", "delegation": "world-delegation",
                    "observation_id": "authored-world-field"}
        output = json.loads(self.action["parts"][0]["state"]["output"])
        output.update(credential_id="secret-credential", observation_id="secret-observation",
                      delegation="secret-delegation", grant="secret-grant", segment_id="secret-segment",
                      execution_task={"segment_id": "secret-task-segment", "grant_version": 7,
                                      "delegation_active": False})
        output["execution_readback"]["projection"] = {"base_observation_id": "secret-base-observation",
                                                       "clock": {"sequence": 11}}
        output["result"]["entity"]["state"] = deepcopy(ordinary)
        output["result"]["entity"]["program"] = deepcopy(ordinary)
        output["result"]["entity"]["matter"][0]["token"] = "world-material-token"
        output["result"]["action_options"] = [{"read_request": {"arguments": {
            "base_observation_id": "secret-option-observation", "entity_id": "cloth"}},
            "input_schema": {"properties": {"observation_id": {"type": "string"}}}}]
        if large:
            output["result"]["entity"]["state"]["description"] = "可续读世界事实" * 16000
        self.action["parts"][0]["state"].update(output=json.dumps(output, ensure_ascii=False), input={
            "action": "pour", "observation_id": "secret-input-observation", "base_observation_id": "secret-input-base",
            "execution_segment_id": "secret-input-segment", "delegation": "secret-input-delegation",
            "params": deepcopy(ordinary), "definition": deepcopy(ordinary)})

    async def test_authority_removed_before_full_read_pointer_input_and_index(self):
        self.install_authority_fixture()
        result = await self.read()
        encoded = json.dumps(result)
        self.assertNotIn("secret-", encoded)
        self.assertEqual(result["value"]["execution_task"], {"grant_version": 7, "delegation_active": False})
        self.assertEqual(result["value"]["result"]["entity"]["state"]["token"], "world-token")
        self.assertEqual(result["value"]["result"]["entity"]["program"]["delegation"], "world-delegation")
        self.assertIn("observation_id", result["value"]["result"]["action_options"][0]["input_schema"]["properties"])
        inputs = await self.read(section="input")
        self.assertNotIn("secret-", json.dumps(inputs))
        self.assertEqual(inputs["value"]["params"]["grant"], "world-grant")
        index = await self.reader.call({})
        self.assertNotIn("secret-", json.dumps(index))
        self.assertNotIn("/observation_id", index["entries"][0]["output_roots"])
        for section, pointer in (("output", "/credential_id"), ("output", "/execution_readback/projection/base_observation_id"),
                                 ("input", "/observation_id")):
            with self.assertRaises(ConnectorError) as rejected:
                await self.read(section=section, pointer=pointer)
            self.assertEqual(rejected.exception.code, "NativeHistorySelectionMissing")
            self.assertIn("过滤后的历史视图", rejected.exception.message)

    async def test_large_fragments_are_filtered_bounded_and_reconstruct_the_selected_value(self):
        self.install_authority_fixture(large=True)
        first = await self.read()
        self.assertFalse(first["filtered_output_complete"])
        self.assertEqual(first["fragment_format"], "filtered_evidence_json")
        fragments, result = [], first
        while True:
            self.assertTrue(_agent_tool_text_fits(_agent_tool_text(result)))
            self.assertNotIn("secret-", json.dumps(result))
            fragments.append(result["text_fragment"])
            if result["next_read"] is None:
                break
            result = await self.reader.call(result["next_read"]["arguments"])
        filtered_text = "".join(fragments)
        filtered = json.loads(filtered_text)
        self.assertEqual(first["filtered_value_sha256"], hashlib.sha256(filtered_text.encode()).hexdigest())
        self.assertNotIn("credential_id", filtered)
        self.assertEqual(filtered["result"]["entity"]["state"]["token"], "world-token")
        original = self.action["parts"][0]["state"]["output"]
        self.assertEqual(first["source_output_sha256"], hashlib.sha256(original.encode()).hexdigest())
        subtree = await self.read(pointer="/execution_readback/entities/0/matter")
        self.assertTrue(subtree["selected_value_complete"])
        self.assertFalse(subtree["filtered_output_complete"])
        for check in ("expected_source_sha256", "expected_filtered_sha256"):
            with self.assertRaises(ConnectorError) as rejected:
                await self.read(**{check: "0" * 64})
            self.assertEqual(rejected.exception.code, "NativeHistorySourceChanged")

    async def test_missing_placeholder_and_unknown_format_are_explicit_without_raw_text(self):
        for text, code, reason in ((None, "NativeHistoryOutputUnavailable", "stored_output_missing"),
                ("[Old tool result content cleared]", "NativeHistoryOutputUnavailable", "native_output_placeholder"),
                ("unknown format with secret-raw-content", "NativeHistoryFormatUnsupported", "stored_output_not_structured_json"),
                ("[]", "NativeHistoryFormatUnsupported", "stored_output_not_structured_json")):
            self.action["parts"][0]["state"]["output"] = text
            entry = (await self.reader.call({}))["entries"][0]
            self.assertFalse(entry["output_available"])
            self.assertEqual(entry["output_unavailable_reason"], reason)
            with self.assertRaises(ConnectorError) as rejected:
                await self.read()
            self.assertEqual(rejected.exception.code, code)
            self.assertNotIn("secret-raw", rejected.exception.message)
            if reason == "native_output_placeholder":
                self.assertIn("当前入口未取得原正文", rejected.exception.message)
                self.assertNotIn("已清除", rejected.exception.message)
            if reason == "stored_output_missing":
                self.assertIn("当前入口未取得原正文", rejected.exception.message)
                self.assertNotIn("未保存", rejected.exception.message)

    async def test_no_host_session_file_path_or_query_expression_is_accepted(self):
        for extra in ({"session_id": "ses_other"}, {"host": "http://remote"}, {"path": "/private/file"},
                      {"tool": "bash"}, {"mode": "read", "cursor": "mixed"}):
            with self.assertRaises(ConnectorError):
                await self.reader.call(extra)
        self.page.assert_not_awaited()
        self.message.assert_not_awaited()
        for pointer in ("$.result", "/../secret", "/result/entity/~2", "/execution_readback/entities/-1"):
            with self.assertRaises(ConnectorError):
                await self.read(pointer=pointer)


class ConnectorNativeHistoryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory(prefix="grove-native-history-")
        self.addCleanup(directory.cleanup)
        manager = ConnectorManager(world_url="http://127.0.0.1:8890", local_url="http://127.0.0.1:8891",
            storage=Path(directory.name), binary=Path("unused-opencode"), wrapper=None)
        self.session = GroveSession(manager, {"role_id": "owner", "access_mode": "control"},
                                    "unused-private-bearer", "测试玩家", "provider/model")
        self.session.session_id, self.session.host_url = SESSION, "http://127.0.0.1:9000"
        self.session.status = "thinking"
        self.session.catalog = {"grove.interact": {"access": "write", "input_schema": {"type": "object"}}}

    async def test_registration_owner_scope_and_repeated_read_do_not_create_world_authority(self):
        self.assertIn(TOOL_NAME, [tool["name"] for tool in self.session.tools()])
        self.assertIn({"permission": "grove_world_*", "pattern": "*", "action": "allow"}, self.session.permissions())
        action, user = tool_message(), user_message()
        async def native(method, path, body=None, **kwargs):
            self.assertEqual(method, "GET")
            self.assertTrue(path.startswith("/session/" + SESSION + "/message"))
            if "?" in path:
                self.assertTrue(kwargs["include_page_cursor"])
                return {"messages": [action], "next_cursor": None}
            return deepcopy(user if path.endswith("msg_user") else action)
        self.session.host = AsyncMock(side_effect=native)
        self.session.invoke, self.session.world = AsyncMock(), AsyncMock()
        self.session.present_agent_result, self.session.remember = Mock(), Mock()
        before = self.session.observations.sequence
        listing = await self.session.tool_call(TOOL_NAME, {"tool": "grove_interact"})
        read = listing["entries"][0]["read"]["arguments"]
        self.assertEqual(await self.session.tool_call(TOOL_NAME, read), await self.session.tool_call(TOOL_NAME, read))
        self.assertEqual(self.session.world_call_count, 3)
        self.assertEqual(self.session.observations.sequence, before)
        self.session.world.assert_not_awaited()
        self.session.invoke.assert_not_awaited()
        self.session.present_agent_result.assert_not_called()
        self.session.remember.assert_not_called()

    async def test_existing_active_turn_blueprint_and_budget_gates_cover_history(self):
        self.session.native_tool_history = AsyncMock(return_value={})
        self.session.status = "idle"
        with self.assertRaises(ConnectorError) as rejected:
            await self.session.tool_call(TOOL_NAME, {})
        self.assertEqual(rejected.exception.code, "NoActiveTurn")
        self.session.status, self.session.blueprint_active = "thinking", True
        with self.assertRaises(ConnectorError) as rejected:
            await self.session.tool_call(TOOL_NAME, {})
        self.assertEqual(rejected.exception.code, "BlueprintToolsDenied")
        self.session.blueprint_active, self.session.world_call_count = False, MAX_WORLD_CALLS
        with self.assertRaises(ConnectorError) as rejected:
            await self.session.tool_call(TOOL_NAME, {})
        self.assertEqual(rejected.exception.code, "TurnBudget")
        self.session.native_tool_history.assert_not_awaited()

    async def test_host_page_adapter_only_exposes_the_official_cursor_header(self):
        response = httpx.Response(200, json=[], headers={"X-Next-Cursor": "official_before",
            "Link": "https://private-host/path?secret-header", "X-Private": "secret-header"})
        with patch("agent_world.grove_connector.httpx.AsyncClient.request", AsyncMock(return_value=response)):
            result = await self.session.host("GET", "/session/" + SESSION + "/message?limit=12", include_page_cursor=True)
        self.assertEqual(result, {"messages": [], "next_cursor": "official_before"})
        self.assertNotIn("secret-header", json.dumps(result))
