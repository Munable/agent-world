"""Seek-paged immutable publication directory, separate from execution seats."""
from __future__ import annotations

import hashlib
import re
from agent_world import FunctionOutcome, FunctionSpec, StateRule
from agent_world.errors import RuleViolation
from agent_world.simulation import matter as sm
from . import model as m
from .definitions import DEFINITIONS

VERSIONS = "grove.creative.versions"
DIRECTORY = "grove.creative.directory"
AUTHORS = "grove.creative.author."
SERIES = "grove.creative.series."
PUBLISHERS = "grove.creative.publishers"
LEDGER_KEY = "publication_storage"
MAX_PUBLISH_PER_STEP = 8
_KEY = re.compile(r"[a-z][a-z0-9_.-]{0,79}\Z")


def _save(ctx, scope, key, value):
    previous = ctx.get_state_record(scope, key)
    ctx.set_state(scope, key, value, expected_version=previous["version"])


def _scope(author=None, key=None):
    if key is not None and author is None:
        raise RuleViolation("查询规则系列需要同时指定作者。")
    if key is not None and (not isinstance(key, str) or not _KEY.fullmatch(key)):
        raise RuleViolation("规则系列key格式无效。")
    if author is None:
        return DIRECTORY
    digest = hashlib.sha256(author.encode()).hexdigest()
    return AUTHORS + digest if key is None else SERIES + hashlib.sha256((author + ":" + key).encode()).hexdigest()


def _index(ctx, reference, record, sequence):
    definition = record["definition"]
    summary = {"definition_ref": reference, "author": record["author"], "key": definition["key"],
               "name": definition["name"], "version": definition["version"], "hash": record["hash"],
               "published_tick": record["published_tick"], "type_ids": list(definition["types"])}
    key = f"{(1 << 63) - 1 - sequence:019d}:{reference}"
    sizes = 0
    for scope in (DIRECTORY, _scope(record["author"]), _scope(record["author"], definition["key"])):
        _save(ctx, scope, key, summary)
        sizes += sm.encoded_size(summary)
    return sizes


def migrate_catalog(ctx):
    """Only the known, previously bounded old index; never scan old history."""
    if ctx.get_state(m.META, LEDGER_KEY) is not None:
        return
    references = ctx.get_state(m.META, "creative_definitions", [])
    if not isinstance(references, list) or len(references) > 32 or len(set(references)) != len(references):
        raise RuleViolation("旧发表目录无效；原定义与实例保留，迁移未提交。")
    ledger = {"sequence": 0, "definitions_count": 0, "payload_bytes": 0}
    for reference in references:
        record = ctx.get_state(DEFINITIONS, reference)
        if not record:
            raise RuleViolation("旧目录引用缺少原定义，迁移未提交。")
        definition = record["definition"]
        version_key = hashlib.sha256((record["author"] + ":" + definition["key"] + ":" + str(definition["version"])).encode()).hexdigest()
        version = ctx.get_state(VERSIONS, version_key)
        if version != {"definition_ref": reference}:
            raise RuleViolation("旧定义版本映射不一致，迁移未提交。")
        ledger["sequence"] += 1
        ledger["definitions_count"] += 1
        ledger["payload_bytes"] += sm.encoded_size(record) + sm.encoded_size(version) + _index(ctx, reference, record, ledger["sequence"])
    _save(ctx, m.META, LEDGER_KEY, ledger)
    old = ctx.get_state_record(m.META, "creative_definitions")
    if old["exists"]:
        ctx.delete_state(m.META, "creative_definitions", expected_version=old["version"])


def publish(ctx, reference, version_key, record):
    migrate_catalog(ctx)
    ledger = ctx.get_state(m.META, LEDGER_KEY, {"sequence": 0, "definitions_count": 0, "payload_bytes": 0})
    tick = ctx.get_state(m.META, "clock", {"sequence": 0})["sequence"]
    rate = ctx.get_state(PUBLISHERS, ctx.actor_role_id, {"tick": tick, "used": 0})
    used = rate["used"] if rate["tick"] == tick else 0
    if used >= MAX_PUBLISH_PER_STEP:
        raise RuleViolation("本作者当前逻辑步的新发表预算已用完；原定义和物件继续可用。")
    version = {"definition_ref": reference}
    _save(ctx, DEFINITIONS, reference, record)
    _save(ctx, VERSIONS, version_key, version)
    ledger["sequence"] += 1
    ledger["definitions_count"] += 1
    ledger["payload_bytes"] += sm.encoded_size(record) + sm.encoded_size(version) + _index(ctx, reference, record, ledger["sequence"])
    _save(ctx, m.META, LEDGER_KEY, ledger)
    _save(ctx, PUBLISHERS, ctx.actor_role_id, {"tick": tick, "used": used + 1})


def definition_list(ctx, args):
    scope = _scope(args.get("author"), args.get("key"))
    page = ctx.list_state(scope, limit=args.get("limit", 8), after=args.get("after"), order="key")
    return FunctionOutcome({"definitions": [item["value"] for item in page["items"]],
        "has_more": page["has_more"], "next_cursor": page["next_cursor"],
        "filters": {key: args[key] for key in ("author", "key") if key in args},
        "order": "publication_sequence_desc", "storage": ctx.get_state(m.META, LEDGER_KEY, {}),
        "complete": not page["has_more"], "last_page": not page["has_more"], "scope": "one_query_page",
        "storage_scope": "whole_world_publications",
        "storage_meaning": "累计定义、版本映射及三类目录摘要的JSON负载字节；不含key、SQLite、回执、journal、WAL或备份，不是磁盘占用或终生配额。"})


def specs():
    return (FunctionSpec("grove.definition_list", definition_list,
        {"type": "object", "additionalProperties": False, "properties": {
            "author": {"type": "string", "minLength": 1, "maxLength": 128},
            "key": {"type": "string", "minLength": 1, "maxLength": 80},
            "limit": {"type": "integer", "minimum": 1, "maximum": 16},
            "after": {"type": "string", "minLength": 1, "maxLength": 4096}}}, access="read",
        description="Page immutable published definitions, optionally by author or author+key; use next_cursor with identical filters. Read selected definition_ref directly with definition_read; no full-history scan."),)


def state_specs():
    return tuple(StateRule(scope, "", {}, history="metadata") for scope in (DIRECTORY, AUTHORS, SERIES, PUBLISHERS))
