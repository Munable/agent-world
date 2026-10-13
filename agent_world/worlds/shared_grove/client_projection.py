"""Versioned, authorized data for replaceable Shared Grove clients.

The world owns facts, eligibility and intent bindings. This projection never
executes client code and never exports the persisted entity/program structure.
The existing Agent observations and authoritative action handlers stay separate.
"""
from __future__ import annotations

from copy import deepcopy
import json

from agent_world import FunctionOutcome, FunctionSpec
from agent_world.errors import RuleViolation
from . import model as m

VERSION = 1
LABELS = {
    "fields": "声明字段", "mechanical_observation": "机械观察", "automatic_rules": "自动规则条件",
    "reaction_observations": "材料反应条件", "contract": "当前契约", "history": "已记录历史",
    "reads": "实际读取", "port_sources": "关联事实来源", "published_exports": "公开字段",
    "process": "保存过程", "deployment": "实际部署资格", "automatic_field_change": "最近自动字段变化",
    "mechanical": "保存机械值", "connections": "当前连接", "recorder": "记录器",
    "energy": "储能", "position": "位置", "angle": "累计圈数", "speed": "每步圈数", "status": "状态",
    "enabled": "启用", "known": "已读到", "actual": "实际值", "satisfied": "条件满足",
    "quantity": "数量", "material": "材料", "name": "名称", "label": "名称", "unit": "单位",
    "available": "当前可用", "capacity": "容量", "delivered": "已交付", "accepted": "已接受身份",
    "parties": "双方身份", "deadline_tick": "截止世界时刻", "terms_version": "条款版本",
    "last_tick": "保存世界时刻", "observed_tick": "观察世界时刻", "last_applied_tick": "最近完成结算时刻",
    "pending_target_tick": "等待结算时刻", "stored_state": "保存输出", "current_inputs": "本次输入",
    "saved_outputs": "历史保存输出", "last_solved": "最近输出变化", "drive_request": "保存控制请求",
    "meaning": "含义", "reason": "原因", "error": "错误", "blockers": "阻塞原因", "steps": "累计反应步数",
    "rule_id": "声明名称", "phase": "阶段", "condition": "当前条件", "tick": "世界时刻",
    "inputs": "实际投入", "outputs": "实际产出", "input_kind": "触发来源", "historical": "历史事实",
    "read_tick": "读取世界时刻", "recorded_tick": "记录世界时刻", "sequence": "事件记录",
    "available_torque": "可用扭矩", "solver_eligible": "本次求解资格", "source_matches": "来源类型符合",
    "bound_entity_name": "已绑定物件", "port": "关联槽位", "facts": "读取事实",
    "item_id": "来源物件", "other_id": "关联对象", "material": "材料", "lot_id": "材料批次",
    "amount": "数量（u）", "mode": "方式", "definition_ref": "固定规则版本",
    "local": "本件载荷", "total": "总载荷", "supported": "承受载荷", "limit": "承载上限",
    "overload": "超出载荷", "integrity": "完整度", "max_integrity": "完整度上限",
    "buoyancy": "浮力", "can_float": "下水资格", "wetness": "吸水饱和度",
    "event_consumer": "实际事件消费", "automatic_field_change_status": "自动变化记录范围",
    "fact_semantics": "公开事实含义", "material_semantics": "材料身份与数量含义", "declaration_semantics": "声明解释含义",
}
VALUES = {"intact": "完整", "broken": "破损，残材保留", "consumed": "材料已耗尽",
          "settled": "已结算", "sleeping": "休眠", "calculating": "正在结算", "pressure": "容量受阻",
          "error": "运行受阻", "running": "运行中", "waiting": "等待材料或条件", "idle": "闲置",
          "complete": "已完成", "failed": "已失败", "active": "进行中", "stopped": "已停止",
          "awaiting_acceptance": "等待双方接受", "unoffered": "尚未提出", "no_source": "未接入动力",
          "source": "动力源", "gear": "传动件", "actuator": "执行器", "public": "公共", "private": "私人"}
SLOTS = {"body": "本体", "contained": "容纳材料", "surface": "表面", "absorbed": "吸附水"}
PROTECTED = {"action", "target_id", "expected_player_version", "expected_target_version",
             "expected_target_interaction_revision", "expected_item_version", "expected_item_interaction_revision",
             "expected_other_version", "expected_combination_fingerprint", "terms_version", "target_slot", "delegation"}

# These schemas are published through normal function discovery and validate the
# actual replies. Clients need neither this Python module nor storage schemas.
_TEXT = {"type": "string"}
_OBJECT = {"type": "object"}
_ARRAY = {"type": "array"}
_POINT = {"type": "object", "properties": {"x": {"type": ["integer", "null"]}, "y": {"type": ["integer", "null"]}}, "required": ["x", "y"], "additionalProperties": False}
_CLOCK = {"type": "object", "properties": {"epoch": _TEXT, "sequence": {"type": "integer", "minimum": 0}, "last_tick_at": {"type": "number"},
          "completed_sequence": {"type": "integer", "minimum": 0}, "frame_id": _TEXT, "frame_status": {"enum": ["preparing", "open", "closed"]}},
          "required": ["epoch", "sequence", "last_tick_at"], "additionalProperties": False}
_PLAYER_PROPERTIES = {"id": _TEXT, "name": _TEXT, "x": {"type": "integer"}, "y": {"type": "integer"}, "version": {"type": "integer"}}
_PUBLIC_PLAYER = {"type": "object", "properties": _PLAYER_PROPERTIES, "required": list(_PLAYER_PROPERTIES), "additionalProperties": False}
_PLAYER = {"type": "object", "properties": {**_PLAYER_PROPERTIES, "backpack_id": _TEXT, "spawn": _POINT},
           "required": [*list(_PLAYER_PROPERTIES), "backpack_id", "spawn"], "additionalProperties": False}
_FACT = {"type": "object", "properties": {
    "id": _TEXT, "label": _TEXT, "availability": {"enum": ["known", "unknown"]},
    "type": {"enum": ["group", "boolean", "number", "text"]},
    "value": {"type": ["null", "boolean", "number", "string"]}, "display_value": _TEXT,
    "unit": _TEXT, "temporal_scope": {"enum": ["current", "historical", "published"]}, "source": _TEXT,
    "rows": {"type": "array", "items": {"$ref": "#/$defs/fact"}}},
    "required": ["id", "label", "availability", "type", "temporal_scope", "source"], "additionalProperties": False}
_SECTION = {"type": "object", "properties": {"id": _TEXT, "label": _TEXT,
    "rows": {"type": "array", "items": {"$ref": "#/$defs/fact"}}}, "required": ["id", "label", "rows"], "additionalProperties": False}
_REQUEST = {"type": "object", "properties": {"function_id": _TEXT, "arguments": _OBJECT},
            "required": ["function_id", "arguments"], "additionalProperties": False}
_NUMBER_OR_UNKNOWN = {"type": ["number", "null"]}
_APPEARANCE = {"type": "object", "properties": {
    "size": {"enum": [1, 2]}, "shape": {"enum": ["box", "bowl", "cloth", "beam", "stone", "raft", "part"]},
    "color": {"type": "string", "pattern": "^#[0-9a-fA-F]{6}$"}, "glyph": _TEXT, "pose": {"enum": ["normal", "folded"]},
    "binding_status": {"enum": ["unavailable", "unmapped", "resolved"]},
    "badge": {"type": "object", "properties": {"label": _TEXT, "known": {"type": "boolean"},
        "value": {"type": ["integer", "boolean", "null"]}}, "required": ["label", "known", "value"], "additionalProperties": False},
    "container_open": {"type": ["boolean", "null"]},
    "overlays": {"type": "object", "properties": {"broken": {"type": "boolean"}, "consumed": {"type": "boolean"},
        "burning": {"type": "boolean"}, "wetness": _NUMBER_OR_UNKNOWN, "water_quantity": _NUMBER_OR_UNKNOWN},
        "required": ["broken", "consumed", "burning", "wetness", "water_quantity"], "additionalProperties": False},
    "motion": {"type": "object", "properties": {"kind": {"enum": ["linear_actuator", "rotation"]}, "angle": {"type": "number"},
        "position": _NUMBER_OR_UNKNOWN, "travel": _NUMBER_OR_UNKNOWN, "observed_tick": {"type": ["integer", "null"]},
        "replay_allowed": {"type": "boolean"}}, "required": ["kind", "angle", "position", "travel", "observed_tick", "replay_allowed"], "additionalProperties": False}},
    "required": ["size", "shape", "color", "glyph", "pose", "container_open", "overlays"], "additionalProperties": False}
_CHOICE = {"type": "object", "properties": {"value": {"type": ["string", "number", "boolean", "null"]},
    "label": _TEXT, "arguments_patch": _OBJECT, "when": {"type": "object", "additionalProperties": {"type": "array"}},
    "maximum": {"type": "number"}, "preview": {"type": "object", "properties": {
        "rows": {"type": "array", "items": {"$ref": "#/$defs/fact"}}, "available": {"type": ["boolean", "null"]},
        "reason": {"type": ["string", "null"]}}, "required": ["rows", "available", "reason"], "additionalProperties": False}},
    "required": ["value", "label", "arguments_patch"], "additionalProperties": False}
_CONTEXT = {"type": "object", "properties": {
    "projection_version": {"const": VERSION}, "world_instance": _TEXT, "world_id": {"const": "shared-grove"},
    "world_version": {"type": "integer", "minimum": 1}, "viewer_role_id": _TEXT,
    "snapshot_seq": {"type": "integer", "minimum": 0}, "observation_clock": _CLOCK,
    "watermark_source": {"const": "query_envelope.snapshot_seq"}},
    "required": ["projection_version", "world_instance", "world_id", "world_version", "viewer_role_id", "snapshot_seq", "observation_clock", "watermark_source"], "additionalProperties": False}
_CONTROL = {"type": "object", "properties": {"id": _TEXT, "path": {"type": "array", "items": _TEXT},
    "label": _TEXT, "kind": {"enum": ["choice", "multi_choice", "integer", "number", "boolean", "string", "array"]},
    "required": {"type": "boolean"}, "schema": _OBJECT, "default": {}, "choices": {"type": "array", "items": _CHOICE},
    "discovery_request": _REQUEST, "maximum_from_control_id": _TEXT}, "required": ["id", "path", "label", "kind", "required", "schema"], "additionalProperties": False}
_ACTION = {"type": "object", "properties": {"id": _TEXT, "action": _TEXT, "function_id": _TEXT, "label": _TEXT,
    "description": _TEXT, "available": {"type": "boolean"}, "reason": {"type": ["string", "null"]},
    "expanded": {"type": "boolean"}, "fixed_arguments": _OBJECT, "input_schema": _OBJECT,
    "controls": {"type": "array", "items": {"$ref": "#/$defs/control"}}, "confirmation_keys": {"type": "array", "items": _TEXT},
    "preconditions": {"type": "array", "items": {"$ref": "#/$defs/fact"}}, "read_request": _REQUEST,
    "cost_facts": {"type": "array", "items": {"$ref": "#/$defs/fact"}},
    "candidate_requests": {"type": "array", "items": {"type": "object", "properties": {"id": _TEXT, "label": _TEXT,
        "read_request": _REQUEST}, "required": ["id", "label", "read_request"], "additionalProperties": False}}},
    "required": ["id", "action", "function_id", "label", "description", "available", "reason", "expanded", "read_request"], "additionalProperties": False}
_ENTITY = {"type": "object", "properties": {"id": _TEXT, "name": _TEXT, "version": {"type": "integer"},
    "interaction_revision": {"type": ["integer", "null"]}, "x": {"type": ["integer", "null"]}, "y": {"type": ["integer", "null"]},
    "location": _POINT, "custody": {"type": "object", "properties": {"scope": {"enum": ["private", "public"]},
        "holder_id": {"type": ["string", "null"]}, "container_id": {"type": ["string", "null"]}},
        "required": ["scope", "holder_id", "container_id"], "additionalProperties": False}, "appearance": _APPEARANCE,
    "summary_facts": {"type": "array", "items": {"$ref": "#/$defs/fact"}},
    "fact_sections": {"type": "array", "items": {"$ref": "#/$defs/section"}}, "materials": {"type": "array", "items": {
        "type": "object", "properties": {"id": {"type": ["string", "null"]}, "material_id": _TEXT, "material_name": _TEXT,
            "quantity": {"type": "integer"}, "unit": {"const": "u"}, "slot": _TEXT, "slot_label": _TEXT, "burning": {"type": "boolean"}},
        "required": ["id", "material_id", "material_name", "quantity", "unit", "slot", "slot_label", "burning"], "additionalProperties": False}},
    "relations": _OBJECT, "navigation": {"type": "object", "properties": {"blocks": {"type": "boolean"}}, "required": ["blocks"], "additionalProperties": False},
    "read_request": _REQUEST, "authored_description": {"type": ["string", "null"]},
    "created_by": {"type": ["string", "null"]}, "created_by_name": {"type": ["string", "null"]},
    "definition_request": {"oneOf": [{"type": "null"}, _REQUEST]},
    "actions": {"type": "array", "items": {"$ref": "#/$defs/action"}}, "summary": _TEXT},
    "required": ["id", "name", "version", "location", "custody", "appearance", "summary_facts", "fact_sections", "actions", "summary", "navigation", "read_request"], "additionalProperties": False}


def output_schema(surface):
    properties = {"context": {"$ref": "#/$defs/context"}, "player": _PLAYER}
    if surface == "viewport":
        properties.update({key: _ARRAY for key in ("changes", "opportunities")})
        properties["players"] = {"type": "array", "items": _PUBLIC_PLAYER}
        chunk_props = {"id": _TEXT, "cx": {"type": "integer"}, "cy": {"type": "integer"}, "seed": _TEXT,
            "generator_version": {"type": "integer"}, "tiles": {"type": "array", "items": _TEXT}, "version": {"type": "integer"}}
        properties["chunks"] = {"type": "array", "items": {"type": "object", "properties": chunk_props, "required": list(chunk_props), "additionalProperties": False}}
        properties["coverage"] = {"type": "array", "items": {"type": "object", "properties": {"entity_id": _TEXT, "active": {"type": "boolean"}, "color": _TEXT,
            "cells": {"type": "array", "items": _POINT}, "description": {"type": "array", "items": _TEXT}}, "required": ["entity_id", "active", "color", "cells", "description"], "additionalProperties": False}}
        properties["processes"] = {"type": "array", "items": {"type": "object", "properties": {"id": _TEXT, "label": _TEXT, "status": _TEXT,
            "entity_id": {"type": ["string", "null"]}, "participants": {"type": "array", "items": _TEXT}}, "required": ["id", "label", "status"], "additionalProperties": False}}
        properties.update({key: {"type": "array", "items": {"$ref": "#/$defs/entity"}} for key in ("entities", "inventory")})
        properties.update({"inventory_total": {"type": "integer"}, "inventory_next": {"type": ["string", "null"]},
            "bounds": {"type": "object", "properties": {key: {"type": "integer"} for key in ("min_x", "min_y", "max_x", "max_y")},
                "required": ["min_x", "min_y", "max_x", "max_y"], "additionalProperties": False}, "truncated": {"type": "boolean"}, "clock": _CLOCK,
            "navigation": {"type": "object", "properties": {"cells": {"type": "array", "items": {"type": "object", "properties": {
                "x": {"type": "integer"}, "y": {"type": "integer"}, "passable": {"type": ["boolean", "null"]}}, "required": ["x", "y", "passable"], "additionalProperties": False}},
                "planning_only": {"const": True}, "omitted_cells": {"const": "unknown"}}, "required": ["cells", "planning_only", "omitted_cells"], "additionalProperties": False},
            "weather": {"type": "object", "properties": {"label": _TEXT, "rain_chunks": {"type": "array", "items": _TEXT}}, "required": ["label", "rain_chunks"], "additionalProperties": False},
            "public_content": {"type": ["object", "null"]}, "projection": _OBJECT})
    elif surface == "inspect":
        properties.update({"entity": {"$ref": "#/$defs/entity"}, "contents": {"type": "array", "items": {"$ref": "#/$defs/entity"}},
            "explanation": {"type": ["string", "null"]}, "projection": _OBJECT, "tile": _OBJECT,
            "entities": {"type": "array", "items": {"$ref": "#/$defs/entity"}}, "actions": {"type": "array", "items": {"$ref": "#/$defs/action"}}})
    else:
        return definitions_output_schema()
    required_fields = list(properties) if surface == "viewport" else ["context", "player"]
    result = {"type": "object", "properties": properties, "required": required_fields, "additionalProperties": False,
            "$defs": {"context": _CONTEXT, "fact": _FACT, "section": _SECTION, "entity": _ENTITY, "action": _ACTION, "control": _CONTROL}}
    if surface == "inspect":
        result["oneOf"] = [{"required": ["entity", "contents", "explanation", "projection"], "not": {"required": ["tile"]}},
                           {"required": ["tile", "entities", "actions"], "not": {"required": ["entity"]}}]
    return result


def definitions_output_schema():
    """Mode-specific, discoverable contracts for the public blueprint browser."""
    def shape(mode, fields):
        props = {"context": {"$ref": "#/$defs/context"}, "mode": {"const": mode}, **fields}
        return {"type": "object", "properties": props, "required": list(props), "additionalProperties": False}
    nullable_text = {"type": ["string", "null"]}
    summary = {"type": "object", "properties": {"definition_ref": _TEXT, "author": _TEXT, "author_name": nullable_text,
        "key": _TEXT, "name": _TEXT, "version": {"type": "integer"}, "hash": _TEXT, "published_tick": {"type": "integer"},
        "type_ids": {"type": "array", "items": _TEXT}, "read_request": _REQUEST},
        "required": ["definition_ref", "author", "author_name", "key", "name", "version", "hash", "published_tick", "type_ids", "read_request"], "additionalProperties": False}
    storage = {"type": "object", "properties": {"sequence": {"type": "integer"}, "definitions_count": {"type": "integer"}, "payload_bytes": {"type": "integer"}}, "additionalProperties": False}
    author_storage = {"type": ["object", "null"], "properties": {"definitions_count": {"type": "integer"}, "payload_bytes": {"type": "integer"}, "complete": {"type": "boolean"}}, "additionalProperties": False}
    budget = {"type": "object", "properties": {"payload_bytes": {"type": "integer"}, "definitions": {"type": "integer"},
        **{key: _TEXT for key in ("scope", "charge", "excludes", "on_exhaustion")}},
        "required": ["payload_bytes", "definitions", "scope", "charge", "excludes", "on_exhaustion"], "additionalProperties": False}
    list_shape = shape("list", {"definitions": {"type": "array", "maxItems": 16, "items": summary}, "has_more": {"type": "boolean"},
        "next_cursor": nullable_text, "filters": {"type": "object", "properties": {"author": _TEXT, "key": _TEXT}, "additionalProperties": False},
        "order": {"const": "publication_sequence_desc"}, "complete": {"type": "boolean"}, "last_page": {"type": "boolean"},
        "scope": {"const": "one_query_page"}, "filter_scope": {"enum": ["selected_page_only", "all_definitions"]},
        "author_name_meaning": _TEXT, "storage": storage, "storage_scope": {"const": "whole_world_publications"},
        "storage_meaning": _TEXT, "author_storage": author_storage, "publication_budget": budget})
    type_choice = {"type": "object", "properties": {"id": _TEXT, "label": _TEXT, "can_fabricate": {"type": "boolean"},
        "creation_request": {"oneOf": [{"type": "null"}, _REQUEST]}}, "required": ["id", "label", "can_fabricate", "creation_request"], "additionalProperties": False}
    detail_shape = shape("detail", {"definition_ref": _TEXT, "name": _TEXT, "author": _TEXT, "author_name": nullable_text,
        "version": {"type": "integer"}, "types": {"type": "array", "items": type_choice}, "installable": {"type": "boolean"},
        "fact_sections": {"type": "array", "items": {"$ref": "#/$defs/section"}}, "projection": _OBJECT})
    lot = {"type": "object", "properties": {"id": _TEXT, "material": _TEXT, "quantity": {"type": "integer"}, "slot": _TEXT,
        "material_import": {"type": "object", "properties": {"definition_ref": _TEXT, "symbol": _TEXT}, "required": ["definition_ref", "symbol"], "additionalProperties": False}},
        "required": ["id", "material", "quantity", "slot"], "additionalProperties": False}
    source = {"type": "object", "properties": {"id": _TEXT, "name": _TEXT, "version": {"type": "integer"}, "kind": {"const": "entity"},
        "lots": {"type": "array", "items": lot}}, "required": ["id", "name", "version", "kind", "lots"], "additionalProperties": False}
    sources = {"type": "array", "items": source}
    source_choices = {"type": "object", "properties": {"sources": sources}, "required": ["sources"], "additionalProperties": False}
    inputs = {"type": "object", "properties": {"path": {"const": ["sources"]}, "schema": _OBJECT, "choices": source_choices}, "required": ["path", "schema", "choices"], "additionalProperties": False}
    intent = {"type": "object", "properties": {"function_id": {"const": "grove.fabricate"}, "fixed_arguments": _OBJECT,
        "input_schema": _OBJECT, "selection_path": {"const": ["sources"]}, "guard_keys": {"type": "array", "items": _TEXT}},
        "required": ["function_id", "fixed_arguments", "input_schema", "selection_path", "guard_keys"], "additionalProperties": False}
    fab_action = {"type": "object", "properties": {"action": _TEXT, "label": _TEXT, "function_id": _TEXT,
        "arguments": _OBJECT, "input_schema": _OBJECT, "choices": source_choices, "available": {"type": "boolean"}, "reason": nullable_text},
        "required": ["action", "label", "function_id", "arguments", "input_schema", "choices", "available", "reason"], "additionalProperties": False}
    material = {"type": "object", "properties": {"symbol": _TEXT, "material_id": _TEXT, "name": _TEXT,
        **{key: {"type": "integer", "minimum": 0} for key in ("quantity_u", "available_u", "missing_u")}},
        "required": ["symbol", "material_id", "name", "quantity_u", "available_u", "missing_u"], "additionalProperties": False}
    unavailable = {"type": "object", "properties": {"id": _TEXT, "name": _TEXT, "reason_code": _TEXT, "reason": _TEXT,
        "materials": {"type": "array", "items": _TEXT}, "read_request": _REQUEST},
        "required": ["id", "name", "reason_code", "reason", "materials", "read_request"], "additionalProperties": False}
    acquisition = {"type": "object", "properties": {"id": _TEXT, "name": _TEXT, "location": _OBJECT,
        "materials": {"type": "array", "items": {"type": "object", "properties": {"material_id": _TEXT, "visible_quantity_u": {"type": "integer"}},
            "required": ["material_id", "visible_quantity_u"], "additionalProperties": False}}, "distance": {"type": "integer"},
        "next_action": nullable_text, "reason": nullable_text, "read_request": _REQUEST, "permission": _TEXT},
        "required": ["id", "name", "location", "materials", "distance", "next_action", "reason", "read_request", "permission"], "additionalProperties": False}
    fabrication_shape = shape("fabrication", {"player": _PLAYER, "definition_ref": _TEXT, "type_id": _TEXT, "source_scope": _TEXT,
        "materials_quantity_basis": _TEXT, "cost_u": {"type": "object", "additionalProperties": {"type": "integer", "minimum": 1}},
        "materials": {"type": "array", "items": material}, "action": fab_action, "inputs": inputs, "intent": intent,
        "unavailable_sources": {"type": "array", "items": unavailable}, "unavailable_sources_truncated": {"type": "boolean"},
        "acquisition_sources": {"type": "array", "items": acquisition}, "source_discovery_projection": _OBJECT})
    return {"type": "object", "oneOf": [list_shape, detail_shape, fabrication_shape], "$defs": {"context": _CONTEXT, "fact": _FACT, "section": _SECTION}}


def clock_projection(value):
    """Expose the finite client clock, independent of stored scheduler metadata."""
    return {key: deepcopy(value[key]) for key in _CLOCK["properties"] if key in value}


def context(ctx):
    from .world import WORLD_VERSION
    return {"projection_version": VERSION, "world_instance": ctx.universe,
            "world_id": "shared-grove", "world_version": WORLD_VERSION,
            "viewer_role_id": ctx.actor_role_id, "observation_clock": clock_projection(ctx.get_state(m.META, "clock", {})),
            "snapshot_seq": ctx.read_snapshot_seq, "watermark_source": "query_envelope.snapshot_seq"}


def _label(key):
    return LABELS.get(key, str(key).replace("_", " "))


def fact(key, value, *, label=None, unit=None, temporal="current", source="world"):
    row = {"id": str(key), "label": label or _label(key), "availability": "unknown" if value is None else "known",
           "type": "boolean" if type(value) is bool else "number" if isinstance(value, (int, float)) else "text",
           "value": value, "temporal_scope": temporal, "source": source}
    if unit:
        row["unit"] = unit
    if isinstance(value, str):
        row["display_value"] = VALUES.get(value, value)
    return row


def fact_tree(value, *, path="", depth=0, temporal="current", source="world"):
    """Bounded typed rows; labels and scopes are server facts, not client guesses."""
    if depth >= 6:
        return [fact(path, "更深的内容请读取详细做法。", temporal=temporal, source=source)]
    entries = list(enumerate(value)) if isinstance(value, list) else list(value.items()) if isinstance(value, dict) else [(path, value)]
    rows = []
    for key, item in entries[:40]:
        row_id = f"{path}.{key}" if path else str(key)
        label = f"记录 {key + 1}" if isinstance(key, int) else _label(key)
        item_temporal = "historical" if key in {"saved_outputs", "stored_state", "last_solved", "last_reaction", "history", "reads", "automatic_field_change"} else temporal
        if isinstance(item, (dict, list)):
            rows.append({"id": row_id, "label": label, "type": "group", "availability": "known",
                         "temporal_scope": item_temporal, "source": source,
                         "rows": fact_tree(item, path=row_id, depth=depth + 1, temporal=item_temporal, source=source)})
        else:
            rows.append(fact(row_id, item, label=label, temporal=item_temporal, source=source))
    if len(entries) > 40:
        rows.append(fact(path + ".omitted", f"当前展开40项，共{len(entries)}项；省略不表示不存在。", temporal=temporal, source=source))
    return rows


def _material_name(entity, identity):
    from .definitions import registry_for
    for lot in entity.get("matter", []):
        if lot.get("material") == identity and lot.get("material_name"):
            return lot["material_name"]
    definitions = registry_for(entity).materials
    material = definitions.get(identity)
    return material.name if material else str(identity)


def _candidate(candidate, field, entity):
    result = {"value": candidate.get("id", candidate.get("value", candidate.get("definition_ref", candidate.get("symbol")))),
              "label": candidate.get("name", candidate.get("label", "未命名对象")), "arguments_patch": {}}
    if "version" in candidate:
        version_key = {"item_id": "expected_item_version", "other_id": "expected_other_version"}.get(field)
        if version_key:
            result["arguments_patch"][version_key] = candidate["version"]
    if candidate.get("preview"):
        result["preview"] = {"rows": fact_tree(candidate["preview"], path="preview"),
                             "available": candidate["preview"].get("available"), "reason": candidate["preview"].get("reason")}
        fingerprint = candidate["preview"].get("version_fingerprint")
        if fingerprint:
            result["arguments_patch"]["expected_combination_fingerprint"] = fingerprint
    if candidate.get("eligible_ports") is not None:
        result["when"] = {"port": candidate["eligible_ports"]}
    return result


def action(ctx, option, player, entity):
    """Normalize existing authoritative menus into finite form-control data."""
    schema = deepcopy(option.get("input_schema", {}))
    arguments = deepcopy(option.get("arguments", {}))
    arguments["expected_player_version"] = player["version"]
    descriptors = {"id": option.get("function_id", "grove.interact") + ":" + option["action"],
        "action": option["action"], "function_id": option.get("function_id", "grove.interact"),
        "label": option.get("label", option["action"]), "description": option.get("use", ""),
        "available": option.get("available", False), "reason": option.get("reason"),
        "fixed_arguments": {key: value for key, value in arguments.items() if key in PROTECTED},
        "input_schema": schema, "controls": [], "expanded": True, "confirmation_keys": [],
        "preconditions": fact_tree(option.get("preconditions", []), path="preconditions"),
        "read_request": {"function_id": "grove.client_inspect", "arguments": {"entity_id": entity["id"], "action": option["action"]}}}
    if option.get("condition_observation"):
        descriptors["preconditions"].extend(fact_tree(option["condition_observation"], path="condition"))
    # This is the world's existing write-handler policy, made explicit for the
    # client. It changes no guard and never substitutes a new intent. Whole-item
    # moves and eligible field actions tolerate automatic output revisions;
    # explicit pours revalidate the original amount/source/slot at submission.
    confirmation_keys = set(schema.get("properties", {})) - {"expected_player_version", "delegation"}
    if "expected_target_interaction_revision" in arguments:
        confirmation_keys.discard("expected_target_version")
    if (descriptors["function_id"] == "grove.interact" and option["action"] == "pour"
            and "amount" in schema.get("properties", {}) and "target_slot" in arguments):
        confirmation_keys.difference_update({"expected_target_version", "expected_item_version"})
    descriptors["confirmation_keys"] = sorted(confirmation_keys)
    inputs = option.get("input") or {}
    groups = option.get("choices") or {}
    candidate_groups = {"item_id": inputs.get("candidates", groups.get("items", [])),
                        "other_id": groups.get("others", []), "material": groups.get("materials", [])}
    properties = schema.get("properties", {})
    required = set(schema.get("required", []))

    def add_control(name, prop, prefix=(), defaults=None, required_names=required):
        if name in PROTECTED or name in {"params"}:
            return
        path = [*prefix, name]
        values = candidate_groups.get(name) if not prefix else None
        control = {"id": ".".join(path), "path": path, "label": prop.get("title", _label(name)),
                   "required": name in required_names, "schema": deepcopy(prop),
                   "default": (defaults or {}).get(name, prop.get("default"))}
        if values:
            control.update(kind="choice", choices=[_candidate(row, name, entity) for row in values])
        elif name == "lot_id":
            lots = []
            for source in candidate_groups.get("item_id", []):
                source_entity = m.get_entity(ctx, source["id"])
                for lot in source.get("lots", []):
                    lots.append({"value": lot["id"], "label": f"{_material_name(source_entity, lot['material'])} · {SLOTS.get(lot['slot'], lot['slot'])} · {lot['quantity']}u",
                                 "when": {"item_id": [source["id"]]}, "maximum": lot["quantity"], "arguments_patch": {}})
            control.update(kind="choice", choices=lots)
        elif name == "definition_ref":
            choices = option.get("choices", {}).get("interactions", [])
            control.update(kind="choice", choices=[_candidate(row, name, entity) for row in choices])
            if option.get("discovery"):
                control["discovery_request"] = {"function_id": "grove.client_definitions", "arguments": {"mode": "list", "installable_only": True, "limit": 8}}
        elif prop.get("enum"):
            control.update(kind="choice", choices=[{"value": value, "label": VALUES.get(value, str(value)), "arguments_patch": {}} for value in prop["enum"]])
        elif name == "mode" and inputs.get("choices"):
            control.update(kind="choice", choices=[{"value": row["value"], "label": row.get("label", row["value"]), "arguments_patch": {}} for row in inputs["choices"]])
        elif prop.get("type") == "object":
            for child, spec in prop.get("properties", {}).items():
                add_control(child, spec, path, (defaults or {}).get(name, {}), set(prop.get("required", [])))
            return
        else:
            kind = prop.get("type")
            if not isinstance(kind, str) or kind not in {"integer", "number", "boolean", "string", "array"}:
                # An unknown input grammar is not an invitation to guess a form.
                descriptors.update(available=False, reason="此参数结构尚无有限客户端控件；请使用正式schema客户端读取后提交。")
                return
            control["kind"] = kind
        if name == "amount" and inputs.get("quantity"):
            limits = inputs["quantity"]
            control["schema"].update(minimum=limits.get("min", 1), maximum=limits.get("max", prop.get("maximum", 10000)))
            control["default"] = limits.get("default", control["default"])
        descriptors["controls"].append(control)

    for name, prop in properties.items():
        add_control(name, prop, defaults=arguments)
    params = option.get("params_schema") or properties.get("params", {})
    for name, prop in params.get("properties", {}).items():
        add_control(name, prop, ["params"], arguments.get("params", {}), set(params.get("required", [])))
    # Empty candidate sets stay controls; an available menu may never turn an ID
    # parameter into a free-form guess merely because the candidate list is empty.
    for control in descriptors["controls"]:
        name = control["path"][0]
        if name in candidate_groups and (name in groups or name in {"item_id", "other_id", "material"}):
            control["kind"] = "choice"
            control.setdefault("choices", [])
        if control["id"] == "amount" and any(row["id"] == "lot_id" for row in descriptors["controls"]):
            control["maximum_from_control_id"] = "lot_id"
    descriptors["controls"].sort(key=lambda row: (row["id"] != "port", row["id"] == "lot_id"))
    return descriptors


def entity_projection(ctx, player, value, *, detailed=False):
    state, simulation = value.get("state", {}), value.get("simulation", {})
    authored = value.get("creative") or {}
    presentation = deepcopy(value.get("presentation", {}))
    presentation.setdefault("size", 1)
    presentation.setdefault("pose", "normal")
    presentation.setdefault("shape", "box" if "container" in value.get("capabilities", []) else "part")
    presentation.setdefault("glyph", "▣" if "container" in value.get("capabilities", []) else "◆")
    presentation.setdefault("color", {"wood": "#a27c46", "stone": "#9daba6", "cloth": "#e3c793"}.get(value.get("material"), "#aaac8c"))
    presentation["overlays"] = {"broken": value.get("lifecycle") == "broken", "consumed": value.get("lifecycle") == "consumed",
        "burning": bool(simulation.get("burning_lots") or state.get("burning") or state.get("lit")),
        "wetness": simulation.get("wetness"), "water_quantity": simulation.get("water")}
    presentation["container_open"] = state.get("open") if "container" in value.get("capabilities", []) else None
    observation = authored.get("mechanical_observation") or {}
    if observation.get("solver_eligible") and value.get("lifecycle") == "intact" and observation.get("angle_unit") == "logical_turns":
        saved = observation.get("stored_state", {})
        if isinstance(saved.get("angle"), (int, float)):
            presentation["motion"] = {"kind": "linear_actuator" if observation.get("role") == "actuator" else "rotation",
                "angle": saved["angle"], "position": saved.get("position"), "travel": observation.get("travel"),
                "observed_tick": observation.get("observed_tick"), "replay_allowed": saved.get("status") in {"running", "travel_limit", "insufficient_energy"}}
    summary = [fact("lifecycle", value.get("lifecycle"), label="结构与残材")]
    for key in ("total", "limit", "integrity", "wetness"):
        if key in simulation:
            summary.append(fact(key, simulation[key], unit="%" if key == "wetness" else None))
    if state.get("water_capacity", 0) > 0:
        summary.extend([fact("water", state.get("water"), label="存水", unit="u"), fact("water_capacity", state["water_capacity"], label="存水上限", unit="u")])
    if state.get("water_source_capacity", 0) > 0:
        summary.extend([fact("water_remaining", state.get("water_remaining"), label="可取水量", unit="u"),
                        fact("water_source_capacity", state["water_source_capacity"], label="水源上限", unit="u")])
    for key, label in (("uses", "余下点火次数"), ("stock", "公共原料份数")):
        if key in state:
            summary.append(fact(key, state[key], label=label))
    lots = [{"id": lot.get("id"), "material_id": lot["material"], "material_name": lot.get("material_name", _material_name(value, lot["material"])),
             "quantity": lot["quantity"], "unit": "u", "slot": lot["slot"], "slot_label": SLOTS.get(lot["slot"], lot["slot"]), "burning": lot.get("burning", False)} for lot in value.get("matter", [])]
    sections = [{"id": "materials", "label": "当前材料", "rows": [fact("lot." + str(index), f"{lot['slot_label']} · {lot['material_name']} {lot['quantity']}u" + (" · 燃烧中" if lot["burning"] else "")) for index, lot in enumerate(lots)]}]
    if detailed:
        sections.append({"id": "current_summary", "label": "当前数量与状态", "rows": deepcopy(summary)})
        sections.append({"id": "physics", "label": "当前物理事实", "rows": [fact(key, simulation.get(key)) for key in ("local", "total", "supported", "limit", "overload", "integrity", "max_integrity", "buoyancy", "can_float", "wetness") if key in simulation]})
        sections.append({"id": "execution", "label": "自动结算", "rows": fact_tree(value.get("execution_domain", {}))})
        for key in ("fields", "automatic_field_change", "automatic_field_change_status", "deployment", "automatic_rules", "reaction_observations", "mechanical_observation", "contract", "published_exports", "port_sources", "event_consumer", "fact_semantics", "material_semantics", "declaration_semantics", "history", "reads", "process"):
            if key in authored:
                sections.append({"id": key, "label": _label(key), "rows": fact_tree(authored[key], path=key,
                    temporal="historical" if key in {"history", "reads", "process", "automatic_field_change"} else "published" if key.endswith("_semantics") else "current", source="declared_field" if key == "fields" else "world")})
        if simulation.get("contact"):
            sections.append({"id": "contact", "label": "本容器材料接触", "rows": fact_tree(simulation["contact"], path="contact")})
        if presentation.get("observation"):
            sections.append({"id": "display", "label": "外观绑定观察", "rows": fact_tree(presentation.pop("observation"), path="display", source="declared_field")})
    relation = deepcopy(value.get("relations", {"parent": None, "children": []}))
    location = deepcopy(value.get("location", {"x": value.get("x"), "y": value.get("y")}))
    result = {"id": value["id"], "name": value["name"], "version": value["version"],
        "interaction_revision": value.get("interaction_revision"), "x": value.get("x"), "y": value.get("y"), "location": location,
        "custody": {"scope": "private" if value.get("owner") else "public", "holder_id": value.get("owner"), "container_id": value.get("container")},
        "appearance": presentation, "summary_facts": summary, "fact_sections": sections, "materials": lots, "relations": relation,
        "navigation": {"blocks": bool(value.get("barrier_active", value.get("components", {}).get("barrier", {}).get("active", False))) and value.get("lifecycle") == "intact"},
        "read_request": {"function_id": "grove.client_inspect", "arguments": {"entity_id": value["id"]}},
        "authored_description": value.get("authored_description"), "created_by": value.get("created_by"), "created_by_name": value.get("created_by_name"),
        "definition_request": {"function_id": "grove.client_definitions", "arguments": {"mode": "detail", "definition_ref": authored["definition_ref"]}} if authored.get("definition_ref") else None,
        "actions": [action(ctx, option, player, value) for option in value.get("action_options", [])]}
    result["summary"] = " · ".join(f"{row['label']}：{row.get('display_value', row['value']) if row['availability'] == 'known' else '未公开或不可读'}" + (row.get("unit") or "") for row in summary[:6])
    return result


def client_viewport(ctx, args):
    from . import world
    value = world.viewport(ctx, args)
    player = value["player"]
    body = {key: deepcopy(value[key]) for key in ("player", "chunks", "players", "inventory_total", "inventory_next", "bounds", "truncated", "clock", "changes", "opportunities")}
    body["clock"] = clock_projection(value["clock"])
    body.update(context=context(ctx), entities=[entity_projection(ctx, player, row) for row in value["entities"]], inventory=[entity_projection(ctx, player, row) for row in value["inventory"]])
    blocked = {(row["x"], row["y"]) for row in body["entities"] if row["navigation"]["blocks"]}
    bounds = value["bounds"]
    body["navigation"] = {"cells": [{"x": chunk["cx"] * 16 + index % 16, "y": chunk["cy"] * 16 + index // 16,
        "passable": (tile != "rock" and (chunk["cx"] * 16 + index % 16, chunk["cy"] * 16 + index // 16) not in blocked) if not value["truncated"] else None} for chunk in value["chunks"] for index, tile in enumerate(chunk["tiles"])
        if bounds["min_x"] <= chunk["cx"] * 16 + index % 16 <= bounds["max_x"] and bounds["min_y"] <= chunk["cy"] * 16 + index // 16 <= bounds["max_y"]],
        "planning_only": True, "omitted_cells": "unknown"}
    body["coverage"] = []
    for structure in value.get("construction", {}).get("nearby", []):
        entity = next((row for row in value["entities"] if row["id"] == structure["id"]), {})
        state = entity.get("state", {})
        active = structure.get("active") and not state.get("burning") and (structure.get("recipe") == "canopy" and structure.get("mode") == "shelter" or structure.get("recipe") == "hearth" and structure.get("lit"))
        body["coverage"].append({"entity_id": structure["id"], "active": bool(active), "color": "#efa457" if structure.get("recipe") == "hearth" else "#86c9da" if structure.get("mode") == "collect" else "#e3d89b", "cells": deepcopy(structure.get("coverage", [])), "description": deepcopy(structure.get("effects", []))})
    rains = [row for row in value.get("processes", []) if row["kind"] == "rain" and row["status"] == "active" and row["start_tick"] <= value["clock"]["sequence"]]
    body["weather"] = {"label": "附近有雨" if rains else "附近无雨", "rain_chunks": list(dict.fromkeys(cid for row in rains for cid in row["chunk_ids"]))}
    body["processes"] = [{"id": row["id"], "label": {"rain": "降雨", "drift": "漂流", "growth": "生长"}.get(row["kind"], "世界过程"),
        "status": row["status"], "entity_id": row.get("entity_id")} for row in value.get("processes", [])]
    content = deepcopy(value.get("public_content"))
    if content:
        for anchor in content.get("anchors", []):
            if anchor.get("entity"):
                anchor["entity"] = entity_projection(ctx, player, anchor["entity"])
            anchor["read_request"] = {"function_id": "grove.client_inspect", "arguments": {"entity_id": anchor["entity_id"]}}
    body["public_content"] = content
    body["projection"] = {"scope": "authorized_nearby_map", "detailed_entities": False, "actions_expanded": False, "truncated": value["truncated"], "omitted_sections": ["entity action candidates", "entity detailed facts"]}
    while len(json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode()) > 110000:
        if body.get("public_content") and any(anchor.get("entity") for anchor in body["public_content"].get("anchors", [])):
            from .world import _content_anchor_index
            # Identity-only anchor indexes still preserve every formal read link.
            body["public_content"] = _content_anchor_index(body["public_content"])
            for anchor in body["public_content"]["anchors"]:
                anchor["read_request"] = {"function_id": "grove.client_inspect", "arguments": {"entity_id": anchor["entity_id"]}}
            body["public_content"]["projection"]["meaning"] = "Authorized anchor identities and excerpts; read client_inspect for current facts. History paging is unchanged."
            body["projection"]["omitted_sections"].append("public anchor entity details")
        elif body["entities"]:
            body["entities"].pop()
            body["truncated"] = body["projection"]["truncated"] = True
            for cell in body["navigation"]["cells"]:
                cell["passable"] = None
        else:
            raise RuleViolation("客户端地图超出一次读取预算，请缩小视野。")
    return FunctionOutcome(body)


def _tile(ctx, args):
    from . import world, building, domains
    player = m.get_player(ctx)
    x, y = args["tile"]["x"], args["tile"]["y"]
    if max(abs(x - player["x"]), abs(y - player["y"])) > 8:
        raise RuleViolation("只能读取当前附近地图中的位置。")
    rows = m.public_entities_near(ctx, x, y, radius=0, limit=m.MAX_CHUNK_ENTITIES)
    terrain = m.tile_at(ctx, x, y)
    choices = []
    for recipe in building.recipes():
        reason = None
        try:
            if abs(x - player["x"]) + abs(y - player["y"]) > 1:
                raise RuleViolation("只能在自己所在或相邻一格建设。")
            if terrain not in recipe["terrain"]:
                raise RuleViolation("这一格地形不适合所选配方。")
            if any(building.is_structure(row) or row["state"].get("kind") == "resource_node" for row in rows):
                raise RuleViolation("这一格已有建筑或资源节点。")
            if m.blocking_at(ctx, x, y):
                raise RuleViolation("请先移开这一格的障碍。")
            if "heater" in recipe["components"] and m.is_safe(ctx, x, y):
                raise RuleViolation("出生安全区不能建设火炉。")
            materials = building._choose_materials(ctx, player, recipe, None)
            domains.guard_action(ctx, {"material_ids": list({eid for eid, _, _ in materials})}, topology=True)
            domains.guard_tiles(ctx, [(x, y)])
        except RuleViolation as exc:
            reason = str(exc)
        option = {"id": "grove.build:" + recipe["id"], "action": recipe["id"], "function_id": "grove.build",
            "label": "搭建 " + recipe["name"], "description": recipe["use"], "available": reason is None, "reason": reason,
            "fixed_arguments": {"recipe": recipe["id"], "x": x, "y": y, "expected_player_version": player["version"]},
            "input_schema": deepcopy(world.BUILD_ARGS), "controls": [], "expanded": True, "confirmation_keys": [], "preconditions": [],
            "read_request": {"function_id": "grove.client_inspect", "arguments": {"tile": {"x": x, "y": y}}},
            "cost_facts": [fact(material, count, label=_material_name(rows[0], material) if rows else material, unit="u") for material, count in recipe["material_cost_u"].items()]}
        candidates = []
        for eid in player["inventory"]:
            raw = m.get_entity(ctx, eid)
            status = building.material_status(ctx, player, raw)
            if status["eligible"] and status["material"] in recipe["cost"]:
                candidates.append({"value": eid, "label": raw["name"] + " · " + str(status["quantity_u"]) + "u", "arguments_patch": {}})
        option["controls"] = [{"id": "material_ids", "path": ["material_ids"], "label": "可选：指定投入的物件", "kind": "multi_choice", "required": False, "schema": deepcopy(world.BUILD_ARGS["properties"]["material_ids"]), "choices": candidates}]
        choices.append(option)
    return {"context": context(ctx), "player": world._public_self(player), "tile": {"x": x, "y": y, "terrain": terrain, "name": {"grass": "草地", "dirt": "泥地", "water": "浅溪", "rock": "岩壁"}[terrain], "passable": terrain != "rock" and not m.blocking_at(ctx, x, y)},
            "entities": [entity_projection(ctx, player, world._project_entity(ctx, player, row, include_options=False)) for row in rows], "actions": choices}


def client_inspect(ctx, args):
    from . import world
    if "tile" in args:
        return FunctionOutcome(_tile(ctx, args))
    outcome = world.inspect(ctx, {key: value for key, value in args.items() if key != "tile"}).result
    player = outcome["player"]
    entity = entity_projection(ctx, player, outcome["entity"], detailed=True)
    original_projection = outcome.get("action_options_projection", {})
    projection = {"scope": original_projection.get("scope", "complete_entity_inspection"), "actions_expanded": True}
    for option in entity["actions"]:
        if args.get("action") == option["action"]:
            option["read_request"]["arguments"] = {key: deepcopy(item) for key, item in args.items() if key != "tile"}
    for option in original_projection.get("unexpanded_actions", []):
        entity["actions"].append({"id": option.get("function_id", "grove.interact") + ":" + option["action"],
            "action": option["action"], "function_id": option.get("function_id", "grove.interact"),
            "label": option.get("label", option["action"]), "description": "", "available": option.get("available", False), "reason": option.get("reason"),
            "expanded": False, "read_request": {"function_id": "grove.client_inspect", "arguments": {"entity_id": entity["id"], "action": option["action"]}}})
    selection = original_projection.get("candidate_selection")
    if selection:
        projection["candidate_selection"] = {key: deepcopy(selection[key]) for key in ("candidate_id", "expanded_candidate_ids", "unexpanded_candidate_ids", "unexpanded_details_provided")}
        selected = next(option for option in entity["actions"] if option["action"] == args["action"])
        selected["candidate_requests"] = [{"id": identity, "label": "另一个实际候选 · " + identity,
            "read_request": {"function_id": "grove.client_inspect", "arguments": {"entity_id": entity["id"], "action": args["action"], "candidate_id": identity}}}
            for identity in selection["unexpanded_candidate_ids"]]
    result = {"context": context(ctx), "player": player, "entity": entity,
              "contents": [entity_projection(ctx, player, row) for row in outcome.get("contents", [])],
              "explanation": outcome.get("explanation"), "projection": projection}
    if len(json.dumps(result, ensure_ascii=False, separators=(",", ":")).encode()) > 110000:
        outlines = []
        for row in entity["actions"]:
            outline = {**{key: row[key] for key in ("id", "action", "function_id", "label", "description", "available", "reason", "read_request")}, "expanded": False}
            candidates = {choice["value"]: {"id": choice["value"], "label": choice["label"],
                "read_request": {"function_id": "grove.client_inspect", "arguments": {"entity_id": entity["id"], "action": row["action"], "candidate_id": choice["value"]}}}
                for control in row.get("controls", []) if control["path"] in [["item_id"], ["other_id"]]
                for choice in control.get("choices", []) if isinstance(choice.get("value"), str)}
            candidates.update({request["id"]: request for request in row.get("candidate_requests", [])})
            if candidates:
                outline["candidate_requests"] = list(candidates.values())
            outlines.append(outline)
        entity["actions"] = outlines
        result["projection"].update(scope="entity_facts_and_action_outlines", actions_expanded=False,
            meaning="Action outlines provide no parameter/guard/candidate authority. Read the supplied request before choosing inputs.")
        if len(json.dumps(result, ensure_ascii=False, separators=(",", ":")).encode()) > 110000:
            raise RuleViolation("所选对象的客户端事实超出一次读取预算；原数据未改变。")
    return FunctionOutcome(result)


def client_definitions(ctx, args):
    from . import creative, publication, fabrication
    mode = args.get("mode", "list")
    if mode == "list":
        value = publication.definition_list(ctx, {key: value for key, value in args.items() if key in {"author", "key", "limit", "after"}}).result
        definitions = []
        for row in value["definitions"]:
            if args.get("installable_only"):
                pack = creative.definition_read(ctx, {"definition_ref": row["definition_ref"]}).result["definition"]
                if not any(rule.get("activation") == "installed" for rule in pack.get("contact_reactions", [])):
                    continue
            definitions.append({**deepcopy(row), "read_request": {"function_id": "grove.client_definitions", "arguments": {"mode": "detail", "definition_ref": row["definition_ref"]}}})
        return FunctionOutcome({**value, "definitions": definitions, "context": context(ctx), "mode": mode,
                                "filter_scope": "selected_page_only" if args.get("installable_only") else "all_definitions"})
    if not args.get("definition_ref"):
        raise RuleViolation("请指定实际固定规则版本。")
    if mode == "fabrication":
        if not args.get("type_id"):
            raise RuleViolation("请指定所选成型类型。")
        value = fabrication.fabrication_options(ctx, {"definition_ref": args["definition_ref"], "type_id": args["type_id"]}).result
        for row in value["unavailable_sources"] + value["acquisition_sources"]:
            row["read_request"]["function_id"] = "grove.client_inspect"
        option = value["action"]
        value["inputs"] = {"path": ["sources"], "schema": deepcopy(option["input_schema"]["properties"]["sources"]), "choices": deepcopy(option["choices"])}
        value["intent"] = {"function_id": option["function_id"], "fixed_arguments": {key: deepcopy(item) for key, item in option["arguments"].items() if key != "sources"},
            "input_schema": deepcopy(option["input_schema"]), "selection_path": ["sources"], "guard_keys": ["expected_player_version", "expected_entity_version"]}
        return FunctionOutcome({**value, "context": context(ctx), "mode": mode})
    value = creative.definition_read(ctx, {"definition_ref": args["definition_ref"]}).result
    pack = value["definition"]
    types = []
    for type_id, spec in pack.get("types", {}).items():
        types.append({"id": type_id, "label": spec.get("name", spec.get("label", type_id)), "can_fabricate": bool(spec.get("cost_u")),
            "creation_request": {"function_id": "grove.client_definitions", "arguments": {"mode": "fabrication", "definition_ref": args["definition_ref"], "type_id": type_id}} if spec.get("cost_u") else None})
    return FunctionOutcome({"context": context(ctx), "mode": "detail", "definition_ref": args["definition_ref"], "name": pack.get("name", "规则做法"),
        "author": value["author"], "author_name": value.get("author_name"), "version": pack["version"], "types": types,
        "installable": any(rule.get("activation") == "installed" for rule in pack.get("contact_reactions", [])),
        "fact_sections": [{"id": "definition", "label": "所选不可变版本的声明说明", "rows": fact_tree(pack, path="definition", temporal="published", source="author")},
                          {"id": "diagnostics", "label": "服务器诊断", "rows": fact_tree(value.get("diagnostics", {}))}],
        "projection": {"scope": "immutable_definition_display", "fact_max_depth": 6, "fact_max_children": 40,
            "meaning": "Typed published declaration display, not current instance facts or executable client rules; omitted rows are explicit."}})


def specs():
    from . import world
    inspect_schema = deepcopy(world.INSPECT_ARGS)
    inspect_schema["required"] = []
    inspect_schema["properties"]["tile"] = {"type": "object", "properties": {"x": world.COORD, "y": world.COORD}, "required": ["x", "y"], "additionalProperties": False}
    inspect_schema["oneOf"] = [{"required": ["entity_id"], "not": {"required": ["tile"]}}, {"required": ["tile"], "not": {"required": ["entity_id"]}}]
    definitions_schema = {"type": "object", "additionalProperties": False, "properties": {
        "mode": {"enum": ["list", "detail", "fabrication"]}, "definition_ref": world.ID, "type_id": {"type": "string", "minLength": 1, "maxLength": 80},
        "author": {"type": "string", "minLength": 1, "maxLength": 128}, "key": {"type": "string", "minLength": 1, "maxLength": 80},
        "limit": {"type": "integer", "minimum": 1, "maximum": 16}, "after": {"type": "string", "minLength": 1, "maxLength": 4096}, "installable_only": {"type": "boolean"}}}
    return (FunctionSpec("grove.client_viewport", client_viewport, world.VIEW_ARGS, access="read", version=1, output_schema=output_schema("viewport"), authorize=world.member, description="Version1 authorized client map DTO: appearance, typed facts and navigation. No storage entity/program shapes; inspect separately before acting."),
            FunctionSpec("grove.client_inspect", client_inspect, inspect_schema, access="read", version=1, output_schema=output_schema("inspect"), authorize=world.member, description="Version1 authorized client entity or tile inspection: typed facts and finite controls with fixed intent guards. Submit through existing authoritative actions and original Operation/Receipt."),
            FunctionSpec("grove.client_definitions", client_definitions, definitions_schema, access="read", version=1, output_schema=output_schema("definitions"), authorize=world.member, description="Version1 paged immutable blueprint choices, server installation eligibility and actual fabrication source menus. Publication creates no material or operation permission."))
