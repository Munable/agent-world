"""Bounded evidence reads from this user's dedicated native session only.

This is a connector tool, not a world query or an observation authority. It
reads the host's existing message API; it creates no operation or history store.
"""
from __future__ import annotations

import base64
import binascii
from copy import deepcopy
import hashlib
import json
import re
import time

from jsonschema import Draft202012Validator

from .grove_agent_observation import _agent_tool_text, _agent_tool_text_fits
from .grove_connector_errors import ConnectorError


TOOL_NAME = "native_tool_history"
_PAGE_SIZE = 12
_ID = re.compile(r"(?:msg|prt)_[A-Za-z0-9]{1,100}\Z")
_MEANING = (
    "Filtered historical evidence from your own dedicated native session, not current observations. "
    "Credential and authorization handles are removed before selection and pagination. "
    "Original tool inputs and observation IDs do not grant a current action. Receipt submission "
    "and execution_readback are separate observations at their recorded times. Assistant prose "
    "and summaries are not world evidence. Filters and missing records cannot establish that an "
    "event never happened. Read current world tools before another action."
)
_LIST_MEANING = (
    "This page is a location index, not an original tool-result body. output_available only "
    "indicates that a structured output can be read; use the chosen entry.read to obtain its "
    "actual recorded quantities and state. When next_request is present, this native listing "
    "has not ended; copy that complete call unchanged, including on pages with no matches. "
    "Do not reconstruct a cursor or claim to have read all history from this index."
)
TOOL_SCHEMA = {"type": "object", "additionalProperties": False, "properties": {
    "mode": {"type": "string", "enum": ["list", "read"], "default": "list"},
    "kind": {"type": "string", "enum": ["all", "tool", "user", "summary"], "default": "all"},
    "goal_query": {"type": "string", "minLength": 1, "maxLength": 500,
                   "description": "Literal substring of the original user request; not an Agent summary."},
    "tool": {"type": "string", "minLength": 1, "maxLength": 120,
             "description": "An actual formal tool name, for example grove_interact."},
    "operation_id": {"type": "string", "minLength": 1, "maxLength": 180},
    "cursor": {"type": "string", "minLength": 1, "maxLength": 24 * 1024,
               "description": "Copy the complete next_request call unchanged to continue; its cursor already carries the original filters."},
    "limit": {"type": "integer", "minimum": 1, "maximum": _PAGE_SIZE},
    "message_id": {"type": "string", "pattern": r"^msg_[A-Za-z0-9]{1,100}$"},
    "part_id": {"type": "string", "pattern": r"^prt_[A-Za-z0-9]{1,100}$"},
    "section": {"type": "string", "enum": ["output", "input", "text"], "default": "output"},
    "pointer": {"type": "string", "maxLength": 300,
                "description": "An exact JSON Pointer within the filtered section, such as /execution_readback/entities. No query expressions or file paths."},
    "offset": {"type": "integer", "minimum": 0,
               "description": "Unicode character offset returned for an explicitly incomplete filtered JSON fragment."},
    "expected_source_sha256": {"type": "string", "pattern": r"^[a-f0-9]{64}$"},
    "expected_filtered_sha256": {"type": "string", "pattern": r"^[a-f0-9]{64}$"},
}}
TOOL_DESCRIPTION = (
    "Recover historical sources from your own same dedicated native session. kind selects tool, user, summary or all. "
    "Start with mode=list; optionally match the original user goal text, a tool, or an operation, "
    "and copy the complete next_request unchanged even when this page has no matches. "
    "A list page is only a location index, not the original result body; next_request means "
    "the listing has not ended. Entries identify original message_id/part_id and submission/readback "
    "times. Use the chosen entry.read to obtain actual recorded quantities and state. Use mode=read with those IDs to read "
    "the filtered historical output, section=input for structured tool arguments including errors, or section=text for original user/Native summary text. Large bodies return a fragment with "
    "next_read; alternatively select an exact JSON Pointer discovered in output_roots. "
    "Credential/authorization handles are removed. Source hashes identify the stored original; "
    "returned JSON and fragments are filtered evidence, not byte-exact original text. "
    "Only structured outputs actually returned by the native host are readable; missing output or "
    "placeholder text means this entry did not obtain the original body, not that storage was deleted. "
    "These are historical sources, never new observation_id or "
    "permission to act. Assistant summaries and absent records do not prove world outcomes."
)

_ENVELOPE_HANDLES = {"credential_id", "observation_id", "base_observation_id",
                     "execution_segment_id", "delegation", "grant"}


def filtered_evidence(value, *, tool_input=False):
    """Filter known protocol/control nodes; authored world fields keep their names."""
    clean = deepcopy(value)
    if not isinstance(clean, dict):
        raise ConnectorError("NativeHistoryFormatUnsupported", "原工具结果格式无法核清授权字段；没有提供未经核清的正文。", 422)

    def envelope(node):
        if isinstance(node, dict):
            for key in _ENVELOPE_HANDLES:
                node.pop(key, None)

    envelope(clean)
    if tool_input:
        return clean  # params, definitions and material fields are world content.
    clean.pop("segment_id", None)  # The local checkpoint's execution handle.
    for key in ("result", "execution_readback", "execution_control"):
        envelope(clean.get(key))
    task = clean.get("execution_task")
    if isinstance(task, dict):
        envelope(task)
        task.pop("segment_id", None)

    def protocol_metadata(node):
        if isinstance(node, list):
            for item in node:
                protocol_metadata(item)
        elif isinstance(node, dict):
            for key, item in node.items():
                # These carry authored world values, not transport authority.
                if key in {"state", "program", "params", "fields", "definition", "definitions",
                           "materials", "material_definitions"}:
                    continue
                if key in {"projection", "readback_projection", "action_readback_projection"}:
                    envelope(item)
                elif key == "execution_task" and isinstance(item, dict):
                    envelope(item)
                    item.pop("segment_id", None)
                elif key in {"execution_control", "execution_readback", "viewport", "selected_inspection"}:
                    envelope(item)
                elif key == "read_request" and isinstance(item, dict):
                    envelope(item.get("arguments"))
                elif key == "read_requests" and isinstance(item, list):
                    for request in item:
                        if isinstance(request, dict):
                            envelope(request.get("arguments"))
                protocol_metadata(item)

    protocol_metadata(clean)
    return clean


class NativeToolHistory:
    def __init__(self, *, role_id, session_id, read_page, read_message, formal_tools,
                 prompt_prefixes=()):
        self.role_id, self.session_id = role_id, session_id
        self.read_page, self.read_message = read_page, read_message
        self.tool_names = dict(formal_tools)
        self.formal_tools = set(self.tool_names.values())
        self.prompt_prefixes = tuple(prefix + "\n" for prefix in prompt_prefixes)
        self.scope = hashlib.sha256((role_id + "\0" + session_id).encode()).hexdigest()[:24]

    def _message(self, record, expected_id=None):
        info = record.get("info") if isinstance(record, dict) else None
        if (not isinstance(info, dict) or info.get("sessionID") != self.session_id or
                not isinstance(info.get("id"), str) or not _ID.fullmatch(info["id"]) or
                not info["id"].startswith("msg_") or
                expected_id is not None and info["id"] != expected_id or
                not isinstance(record.get("parts"), list)):
            raise ConnectorError("NativeHistorySourceMismatch", "原会话记录来源不一致；没有提供历史结果。", 409)
        return record

    def _completed_parts(self, record):
        if record["info"].get("role") != "assistant":
            return []
        result = []
        for part in record["parts"]:
            if not isinstance(part, dict) or part.get("type") != "tool" or part.get("tool") not in self.formal_tools | {"invalid"}:
                continue
            state = part.get("state")
            if not isinstance(state, dict) or state.get("status") not in {"completed", "error"}:
                continue
            if (part.get("sessionID") != self.session_id or part.get("messageID") != record["info"]["id"] or
                    not isinstance(part.get("id"), str) or not _ID.fullmatch(part["id"]) or
                    not part["id"].startswith("prt_")):
                raise ConnectorError("NativeHistorySourceMismatch", "原工具片段来源不一致；没有提供历史结果。", 409)
            result.append(part)
        return result

    def _goal(self, record):
        if record["info"].get("role") != "user":
            return ""
        return "\n".join(part["text"] for part in record["parts"] if isinstance(part, dict) and
            part.get("type") == "text" and isinstance(part.get("text"), str) and
            not part.get("synthetic") and not part["text"].startswith(self.prompt_prefixes))

    @staticmethod
    def _output(part):
        if part["state"].get("status") != "completed" or part.get("tool") == "invalid":
            return None, None  # Arbitrary native error text is not a safe world envelope.
        text = part["state"].get("output")
        if not isinstance(text, str):
            return None, None
        try:
            parsed = json.loads(text)
            return text, filtered_evidence(parsed) if isinstance(parsed, dict) else None
        except (ValueError, TypeError):
            return text, None

    def _source(self, record, part, output):
        info, state = record["info"], part["state"]
        raw_time = state.get("time") if isinstance(state.get("time"), dict) else {}
        source = {"kind": "native_completed_tool_part" if state.get("status") == "completed" else "native_error_tool_part", "role_id": self.role_id,
            "session_id": self.session_id, "message_id": info["id"], "part_id": part["id"],
            "tool": part["tool"], "native_part_status": state["status"], "historical_only": True,
            "native_time": {key: raw_time[key] for key in ("start", "end", "compacted")
                            if type(raw_time.get(key)) in {int, float}}}
        if isinstance(part.get("callID"), str):
            source["call_id"] = part["callID"]
        if isinstance(info.get("parentID"), str):
            source["parent_message_id"] = info["parentID"]
        if isinstance(output, dict):
            source["original_operation"] = {key: deepcopy(output[key]) for key in
                ("operation_id", "function_id", "commit_seq", "ok", "replayed") if key in output}
            receipt = output.get("result")
            viewport = receipt.get("viewport") if isinstance(receipt, dict) else None
            if isinstance(viewport, dict) and isinstance(viewport.get("clock"), dict):
                source["submission_viewport_clock"] = deepcopy(viewport["clock"])
            readback = output.get("execution_readback")
            if isinstance(readback, dict):
                source["execution_readback_time"] = {key: deepcopy(readback[key]) for key in
                    ("clock", "observed_at", "public_events_observed_at") if key in readback}
        return source

    def _text_parts(self, record):
        role = record["info"].get("role")
        kind = "user" if role == "user" else "summary" if role == "assistant" and record["info"].get("summary") is True else None
        if kind is None:
            return []
        result = []
        for part in record["parts"]:
            if (not isinstance(part, dict) or part.get("type") != "text" or
                    not isinstance(part.get("text"), str) or part.get("synthetic") or
                    role == "user" and part["text"].startswith(self.prompt_prefixes)):
                continue
            if not isinstance(part.get("id"), str) or not _ID.fullmatch(part["id"]) or not part["id"].startswith("prt_"):
                continue
            if part.get("sessionID") != self.session_id or part.get("messageID") != record["info"]["id"]:
                raise ConnectorError("NativeHistorySourceMismatch", "原文字片段不属于本人会话。", 409)
            result.append((kind, part))
        return result

    def _text_source(self, record, part, kind):
        return {"kind": "original_user_text" if kind == "user" else "native_assistant_summary",
            "role_id": self.role_id, "session_id": self.session_id,
            "message_id": record["info"]["id"], "part_id": part["id"],
            "native_role": record["info"]["role"], "historical_only": True,
            "world_evidence": False, "private_session_source": True}

    def _cursor(self, value):
        if value is None:
            return {"scope": self.scope, "before": None, "offset": 0, "messages": None, "filters": {},
                    "cutoff_ms": int(time.time() * 1000)}
        try:
            decoded = json.loads(base64.urlsafe_b64decode(value + "=" * (-len(value) % 4)))
            if (not isinstance(decoded, dict) or set(decoded) != {"scope", "before", "offset", "messages", "filters", "cutoff_ms"} or
                    decoded["scope"] != self.scope or type(decoded["offset"]) is not int or
                    not 0 <= decoded["offset"] <= 10000 or not isinstance(decoded["filters"], dict) or
                    type(decoded["cutoff_ms"]) is not int or decoded["cutoff_ms"] <= 0 or
                    set(decoded["filters"]) - {"goal_query", "tool", "operation_id", "kind"} or
                    decoded["filters"].get("kind", "all") not in {"all", "tool", "user", "summary"} or
                    any(not isinstance(item, str) or not 1 <= len(item) <=
                        {"goal_query": 500, "tool": 120, "operation_id": 180, "kind": 7}[key]
                        for key, item in decoded["filters"].items()) or
                    decoded["before"] is not None and (not isinstance(decoded["before"], str) or
                        not 1 <= len(decoded["before"]) <= 2000 or any(char.isspace() for char in decoded["before"])) or
                    decoded["messages"] is not None and (not isinstance(decoded["messages"], list) or
                        not 1 <= len(decoded["messages"]) <= _PAGE_SIZE or any(not isinstance(item, str) or
                        not item.startswith("msg_") or not _ID.fullmatch(item) for item in decoded["messages"]))):
                raise ValueError()
            return decoded
        except (ValueError, TypeError, UnicodeError, binascii.Error):
            raise ConnectorError("InvalidNativeHistoryCursor", "历史游标无效或不属于本人原会话；请从原入口重新分页。", 422) from None

    @staticmethod
    def _encode_cursor(value):
        return base64.urlsafe_b64encode(json.dumps(value, separators=(",", ":")).encode()).decode().rstrip("=")

    async def list(self, arguments):
        cursor = self._cursor(arguments.get("cursor"))
        filters = {key: arguments[key] for key in ("goal_query", "tool", "operation_id", "kind") if key in arguments}
        if arguments.get("cursor") and filters and filters != cursor["filters"]:
            raise ConnectorError("InvalidNativeHistoryCursor", "接续历史分页时请保留原过滤条件。", 422)
        filters = filters or cursor["filters"]
        tool = self.tool_names.get(filters.get("tool"), filters.get("tool"))
        if tool and tool not in self.formal_tools | {"invalid"}:
            raise ConnectorError("ToolDenied", "历史只提供本会话的正式工具结果。", 403)
        if cursor["messages"] is None:
            page = await self.read_page(_PAGE_SIZE, cursor["before"])
            if not isinstance(page, dict):
                raise ConnectorError("NativeHistoryUnavailable", "原会话历史分页未能读回；没有替代记录。", 502)
            records, before = page.get("messages"), page.get("next_cursor")
            if (not isinstance(records, list) or len(records) > _PAGE_SIZE or before is not None and
                    (not isinstance(before, str) or not 1 <= len(before) <= 2000 or any(char.isspace() for char in before))):
                raise ConnectorError("NativeHistoryUnavailable", "原会话历史分页未能读回；没有替代记录。", 502)
            records = [self._message(record) for record in records]
        else:
            records = [self._message(await self.read_message(identity), identity) for identity in cursor["messages"]]
            before = cursor["before"]
        parents = {record["info"]["id"]: record for record in records}
        entries = []
        for record in records:
            if filters.get("kind", "all") != "tool" and not tool and not filters.get("operation_id"):
                for kind, part in self._text_parts(record):
                    if filters.get("kind", "all") not in {"all", kind} or filters.get("goal_query") and filters["goal_query"] not in part["text"]:
                        continue
                    created = record["info"].get("time", {}).get("created")
                    if type(created) in {int, float} and created > cursor["cutoff_ms"]:
                        continue
                    entries.append({"source": self._text_source(record, part, kind),
                        "text_available": True, "source_text_bytes": len(part["text"].encode()),
                        "read": {"tool": TOOL_NAME, "arguments": {"mode": "read",
                            "message_id": record["info"]["id"], "part_id": part["id"], "section": "text"}}})
            parts = self._completed_parts(record) if filters.get("kind", "all") in {"all", "tool"} else []
            if not parts:
                continue
            parent_id = record["info"].get("parentID")
            if isinstance(parent_id, str) and parent_id.startswith("msg_") and _ID.fullmatch(parent_id) and parent_id not in parents:
                parents[parent_id] = self._message(await self.read_message(parent_id), parent_id)
            parent = parents.get(parent_id)
            goal = self._goal(parent) if parent else ""
            if filters.get("goal_query") and filters["goal_query"] not in goal:
                continue
            for part in parts:
                native_time = part["state"].get("time")
                ended = native_time.get("end") if isinstance(native_time, dict) else None
                if type(ended) in {int, float} and ended > cursor["cutoff_ms"]:
                    continue
                if tool and part["tool"] != tool:
                    continue
                text, output = self._output(part)
                if filters.get("operation_id") and (not isinstance(output, dict) or output.get("operation_id") != filters["operation_id"]):
                    continue
                source = self._source(record, part, output)
                input_available = part["tool"] != "invalid" and isinstance(part["state"].get("input"), dict)
                inputs = part["state"].get("input", {}) if input_available else {}
                inputs = filtered_evidence(inputs, tool_input=True) if isinstance(inputs, dict) else {}
                entry = {"source": source, "input_index": {key: deepcopy(inputs[key]) for key in
                    ("action", "entity_id", "target_id", "item_id", "other_id", "amount") if isinstance(inputs, dict) and key in inputs},
                    "input_complete": False, "input_available": input_available,
                    "input_read": {"tool": TOOL_NAME, "arguments": {"mode": "read", "message_id": source["message_id"], "part_id": source["part_id"], "section": "input"}} if input_available else None,
                    "goal_excerpt": goal[:600], "goal_excerpt_complete": len(goal) <= 600,
                    "goal_source": {"message_id": parent_id, "kind": "original_user_request"}
                        if parent and parent["info"].get("role") == "user" else None,
                    "output_available": output is not None, "output_format_supported": output is not None,
                    "output_unavailable_reason": (None if output is not None else
                        "native_error_or_invalid_not_a_world_envelope" if part["state"]["status"] == "error" or part["tool"] == "invalid" else
                        "native_output_placeholder" if text == "[Old tool result content cleared]" else
                        "stored_output_missing" if text is None else "stored_output_not_structured_json"),
                    "source_output_bytes": len(text.encode()) if text is not None else None,
                    "output_roots": ["/" + key.replace("~", "~0").replace("/", "~1") for key in output]
                        if isinstance(output, dict) else [],
                    "read": {"tool": TOOL_NAME, "arguments": {"mode": "read", "message_id": source["message_id"], "part_id": source["part_id"]}}}
                entries.append(entry)
        offset, limit = cursor["offset"], arguments.get("limit", _PAGE_SIZE)
        result = {"scope": "same_dedicated_native_session", "meaning": _MEANING,
            "content_kind": "tool_result_location_index", "original_body_delivered": False,
            "page_meaning": _LIST_MEANING,
            "native_cutoff_ms": cursor["cutoff_ms"], "messages_read": len(records),
            "match_scope": "this_native_message_page_only", "historical_completeness": "not_established",
            "matches_in_page": len(entries), "entries": []}

        def with_continuation(value):
            # Size each candidate with its actual continuation, including the
            # complete copyable call; never add cursor overhead after fitting.
            end = offset + len(value["entries"])
            if end < len(entries):
                next_page = {"scope": self.scope, "before": before, "offset": end,
                    "messages": [record["info"]["id"] for record in records], "filters": filters,
                    "cutoff_ms": cursor["cutoff_ms"]}
            elif before is not None:
                next_page = {"scope": self.scope, "before": before, "offset": 0,
                    "messages": None, "filters": filters, "cutoff_ms": cursor["cutoff_ms"]}
            else:
                next_page = None
            token = self._encode_cursor(next_page) if next_page else None
            value["next_cursor"] = token
            value["next_request"] = ({"tool": TOOL_NAME, "arguments": {
                "mode": "list", "cursor": token, "limit": limit}} if token else None)
            value["native_listing_end_reached"] = next_page is None
            return value

        result = with_continuation(result)
        for entry in entries[offset:offset + limit]:
            candidate = deepcopy(result)
            candidate["entries"].append(entry)
            candidate = with_continuation(candidate)
            if not _agent_tool_text_fits(_agent_tool_text(candidate)):
                break
            result = candidate
        if offset < len(entries) and not result["entries"]:
            raise ConnectorError("ToolOutputTooLarge", "原历史索引超出输出预算；请用更具体的正式工具或目标过滤。", 413)
        if not _agent_tool_text_fits(_agent_tool_text(result)):
            raise ConnectorError("ToolOutputTooLarge", "原历史索引超出输出预算；请缩小limit后读取。", 413)
        return result

    async def call(self, arguments):
        if not isinstance(arguments, dict) or any(Draft202012Validator(TOOL_SCHEMA).iter_errors(arguments)):
            raise ConnectorError("InvalidArguments", "历史读取参数不符合正式工具声明。", 422)
        mode = arguments.get("mode", "list")
        allowed = ({"mode", "kind", "goal_query", "tool", "operation_id", "cursor", "limit"} if mode == "list" else
                   {"mode", "message_id", "part_id", "section", "pointer", "offset", "expected_source_sha256", "expected_filtered_sha256"})
        if set(arguments) - allowed:
            raise ConnectorError("InvalidArguments", "请分别使用历史列表参数或原片段读取参数。", 422)
        return await (self.list(arguments) if mode == "list" else self.read(arguments))

    @staticmethod
    def _select(value, pointer):
        if not pointer:
            return value
        if not pointer.startswith("/") or len(pointer.split("/")) > 21 or re.search(r"~(?![01])", pointer):
            raise ConnectorError("InvalidNativeHistoryPointer", "只接受原JSON中的确切JSON Pointer，不接受路径或查询表达式。", 422)
        try:
            for key in pointer[1:].split("/"):
                key = key.replace("~1", "/").replace("~0", "~")
                if isinstance(value, list):
                    if not re.fullmatch(r"0|[1-9][0-9]*", key):
                        raise KeyError(key)
                    value = value[int(key)]
                elif isinstance(value, dict):
                    value = value[key]
                else:
                    raise KeyError(key)
            return value
        except (KeyError, IndexError):
            raise ConnectorError("NativeHistorySelectionMissing", "过滤后的历史视图未公开所选字段；不能据此推断世界事实不存在。", 404) from None

    async def read(self, arguments):
        message_id, part_id = arguments.get("message_id"), arguments.get("part_id")
        if not message_id or not part_id:
            raise ConnectorError("InvalidArguments", "读取原工具结果须提供历史列表中的message_id和part_id。", 422)
        record = self._message(await self.read_message(message_id), message_id)
        section, pointer = arguments.get("section", "output"), arguments.get("pointer", "")
        if section == "text":
            matched = next(((kind, part) for kind, part in self._text_parts(record) if part["id"] == part_id), None)
            if matched is None:
                raise ConnectorError("NativeHistoryPartUnavailable", "原消息没有可读的用户或原生摘要文字片段。", 404)
            kind, part = matched
            text, value = part["text"], part["text"]
            source = self._text_source(record, part, kind)
        else:
            part = next((part for part in self._completed_parts(record) if part["id"] == part_id), None)
            if part is None:
                raise ConnectorError("NativeHistoryPartUnavailable", "原消息没有可读的正式终态工具片段；缺记录不证明从未发生。", 404)
            text, output = self._output(part)
            source = self._source(record, part, output)
            if section == "input":
                if part["tool"] == "invalid" or not isinstance(part["state"].get("input"), dict):
                    raise ConnectorError("NativeHistoryInputUnavailable", "原生无效调用不提供任意错误正文；请重新读取正式工具的参数。", 404)
                inputs = part["state"].get("input")
                value = filtered_evidence(inputs, tool_input=True)
                text = json.dumps(inputs, ensure_ascii=False, separators=(",", ":"))
            else:
                if text is None or text == "[Old tool result content cleared]":
                    raise ConnectorError("NativeHistoryOutputUnavailable", "当前入口未取得原正文；没有用模型摘要替代。", 404)
                if output is None:
                    raise ConnectorError("NativeHistoryFormatUnsupported", "原工具结果不是可核清授权字段的JSON封套；没有提供任意原文。", 422)
                value = output
        digest = hashlib.sha256(text.encode()).hexdigest()
        if arguments.get("expected_source_sha256") not in {None, digest}:
            raise ConnectorError("NativeHistorySourceChanged", "原所选部分与续读指纹不同；没有拼接两份不同记录。", 409)
        if pointer:
            value = self._select(value, pointer)
        fragment_source = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
        filtered_digest = hashlib.sha256(fragment_source.encode()).hexdigest()
        if arguments.get("expected_filtered_sha256") not in {None, filtered_digest}:
            raise ConnectorError("NativeHistorySourceChanged", "过滤后的所选历史内容与续读指纹不同；没有拼接不同内容。", 409)
        result = {"source": source, "meaning": _MEANING,
            "source_sha256": digest,
            "source_fingerprint_meaning": "SHA256 of the stored selected section (tool input uses canonical JSON); filtered tool evidence is not byte-exact original text.",
            "filtered_value_sha256": filtered_digest,
            "section": section, "pointer": pointer,
            "filtered_output_complete": not pointer and section == "output", "selected_value_complete": True,
            "value": value}
        result["source_" + section + "_sha256"] = digest
        if not arguments.get("offset") and _agent_tool_text_fits(_agent_tool_text(result)):
            return result
        offset = arguments.get("offset", 0)
        if offset > len(fragment_source):
            raise ConnectorError("InvalidArguments", "续读偏移超出这份原输出。", 422)
        result.pop("value")
        result.update(filtered_output_complete=False, selected_value_complete=False,
            fragment_format="filtered_evidence_json",
            offset_unit="unicode_characters", offset=offset, total_characters=len(fragment_source))
        count = min(16000, len(fragment_source) - offset)
        while True:
            end = offset + count
            result["text_fragment"] = fragment_source[offset:end]
            result["next_read"] = ({"tool": TOOL_NAME, "arguments": {"mode": "read", "message_id": message_id,
                "part_id": part_id, "section": section, "pointer": pointer, "offset": end,
                "expected_source_sha256": digest, "expected_filtered_sha256": filtered_digest}} if end < len(fragment_source) else None)
            result["fragment_end"] = end
            if _agent_tool_text_fits(_agent_tool_text(result)):
                return result
            if count <= 1:
                raise ConnectorError("ToolOutputTooLarge", "这份历史来源元数据超出输出预算；未提供假完整结果。", 413)
            count //= 2
