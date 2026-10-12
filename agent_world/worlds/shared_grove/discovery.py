"""Seek-paged public work references; current entities decide every result.

Only a creation's first public placement or public metadata change updates the
directory. Locations, inventory ownership and ticking fields are never cached.
References may outlive public placement; an empty candidate page is not absence.
"""
from __future__ import annotations

import base64
import hashlib
import json
import unicodedata

from agent_world import FunctionOutcome, FunctionSpec, StateRule
from agent_world.errors import PermissionDenied, RuleViolation
from agent_world.simulation.matter import encoded_size
from agent_world.world_checkpoints import CheckpointSpec, CheckpointNotFound
from . import model as m

ENTRIES = "grove.public_works.entries"
DIRECTORY = "grove.public_works.directory"
BY_AUTHOR = "grove.public_works.author."
BY_NAME = "grove.public_works.name."
AUTHORS = "grove.public_works.authors"
AUTHOR_NAMES = "grove.public_works.author_name."
CONTROL = "grove.public_works.control"
BUILD_KEY = "grove.public_works.build"
CANDIDATE_LIMIT = 32
BUILD_BATCH = 16
EMPTY = {"type": "object", "additionalProperties": False}


def _normalize(text):
    return unicodedata.normalize("NFKC", text).strip().casefold()


def _scope(prefix, text):
    return prefix + hashlib.sha256(text.encode()).hexdigest()


def _put(ctx, scope, key, value):
    record = ctx.get_state_record(scope, key)
    if not record["exists"] or record["value"] != value:
        ctx.set_state(scope, key, value, expected_version=record["version"])


def _delete(ctx, scope, key):
    record = ctx.get_state_record(scope, key)
    if record["exists"]:
        ctx.delete_state(scope, key, expected_version=record["version"])


def _eligible(entity):
    return (entity is not None and m.is_ground(entity)
            and isinstance(entity.get("created_by"), str) and bool(entity["created_by"])
            and entity.get("source") not in {"terrain", "personal"}
            and type(entity.get("x")) is int and type(entity.get("y")) is int)


def sync_author_name(ctx, player):
    """Update only known public creators; movement never writes this index."""
    author = player["id"]
    previous = ctx.get_state(AUTHORS, author)
    if previous is None:
        return
    name = player.get("name")
    normalized = _normalize(name) if isinstance(name, str) and name else None
    if previous["name"] == normalized:
        return
    if previous["name"]:
        _delete(ctx, _scope(AUTHOR_NAMES, previous["name"]), author)
    if normalized:
        _put(ctx, _scope(AUTHOR_NAMES, normalized), author, {"author": author})
    _put(ctx, AUTHORS, author, {"name": normalized})


def _register_author(ctx, author):
    if ctx.get_state(AUTHORS, author) is None:
        _put(ctx, AUTHORS, author, {"name": None})
    sync_author_name(ctx, {"id": author, "name": m.public_player_name(ctx, author)})


def _index_rows(entry):
    return ((DIRECTORY, entry["key"]),
            (_scope(BY_AUTHOR, entry["author"]), entry["key"]),
            (_scope(BY_NAME, entry["name"]), entry["key"]))


def sync_many(ctx, entities):
    """Called once after an admitted save; never executes or schedules rules."""
    authors = set()
    for entity in entities.values():
        if not _eligible(entity):
            continue
        # Trusted constructors set finite server timestamps. Microsecond ties
        # use stable identity; no shared world ordinal or ticking counter.
        stamp = int(entity.get("created_at", 0) * 1_000_000)
        key = f"{(1 << 63) - 1 - stamp:019d}:{entity['id']}"
        entry = {"entity_id": entity["id"], "author": entity["created_by"],
                 "name": _normalize(entity["name"]), "key": key}
        previous = ctx.get_state(ENTRIES, entity["id"])
        if previous == entry:
            continue
        old_rows = set(_index_rows(previous)) if previous else set()
        rows = set(_index_rows(entry))
        for scope, old_key in old_rows - rows:
            _delete(ctx, scope, old_key)
        for scope, new_key in rows - old_rows:
            _put(ctx, scope, new_key, {"entity_id": entity["id"]})
        _put(ctx, ENTRIES, entity["id"], entry)
        authors.add(entry["author"])
    for author in sorted(authors):
        _register_author(ctx, author)


def _cursor(filters, scope, after, kind="works"):
    if after is None:
        return None
    value = {"filters": filters, "scope": scope, "after": after, "kind": kind}
    return base64.urlsafe_b64encode(json.dumps(value, separators=(",", ":")).encode()).decode()


def _decode_cursor(value, filters):
    if value is None:
        return None
    try:
        cursor = json.loads(base64.urlsafe_b64decode(value.encode()).decode())
        if (not isinstance(cursor, dict) or set(cursor) != {"filters", "scope", "after", "kind"}
                or cursor["filters"] != filters or cursor["kind"] not in {"works", "authors"}
                or not isinstance(cursor["scope"], str) or not isinstance(cursor["after"], str)):
            raise ValueError()
        return cursor
    except (ValueError, KeyError, TypeError, UnicodeError) as exc:
        raise RuleViolation("作品目录游标无效或查询条件已改变；从当前条件的第一页重新读取。") from exc


def _read_cursor(value, filters, scope, kind):
    cursor = _decode_cursor(value, filters)
    if cursor is None:
        return None
    if cursor["scope"] != scope or cursor["kind"] != kind:
        raise RuleViolation("作品目录游标无效或查询条件已改变；从当前条件的第一页重新读取。")
    return cursor["after"]


def _author_matches(ctx, scope, name, after, limit):
    matches, more = [], False
    for _ in range(CANDIDATE_LIMIT):
        page = ctx.list_state(scope, limit=1, after=after, order="key")
        if not page["items"]:
            break
        author = page["items"][0]["value"]["author"]
        current_name = m.public_player_name(ctx, author)
        more, after = page["has_more"], page["next_cursor"]
        if current_name is not None and _normalize(current_name) == name:
            matches.append({"author": author, "name": current_name})
        if len(matches) >= limit or not more:
            break
    return matches, more, after


def _row(ctx, player, entity, author_name):
    program = entity.get("state", {}).get("program", {})
    hidden = "container" in entity.get("components", {}) and not entity["state"]["open"]
    fields = {} if hidden else program.get("fields", {})
    visible_fields = {}
    for key, value in fields.items():
        if len(visible_fields) < 8 and encoded_size({**visible_fields, key: value}) <= 512:
            visible_fields[key] = value
    nearby = max(abs(entity["x"] - player["x"]), abs(entity["y"] - player["y"])) <= 8
    result = {"entity_id": entity["id"], "name": entity["name"],
              "created_by": entity["created_by"], "created_by_name": author_name,
              "location": {"x": entity["x"], "y": entity["y"]},
              "created_at": entity.get("created_at"), "lifecycle": entity.get("lifecycle"),
              "fields": visible_fields, "fields_omitted_count": len(fields) - len(visible_fields),
              "nearby": nearby,
              "read_request": {"tool": "grove_inspect", "arguments": {"entity_id": entity["id"]},
                               "requires": "within_current_nearby_view"}}
    if hidden:
        result["contents_visibility"] = "hidden"
    for key in ("definition_ref", "type_id"):
        if key in program:
            result[key] = program[key]
    return result


def public_works(ctx, args):
    player = m.get_player(ctx, required=False)
    if player is None:
        raise PermissionDenied("请先进入溪源，再读取公开作品目录。")
    if "author" in args and "author_name" in args:
        raise RuleViolation("作者ID与作者姓名择一查询；姓名不是唯一身份。")
    filters = {key: (_normalize(args[key]) if key != "author" else args[key])
               for key in ("author", "author_name", "name") if key in args}
    if any(not value for value in filters.values()):
        raise RuleViolation("作品名称与作者线索不能为空。")
    cursor = _decode_cursor(args.get("after"), filters)
    base = {"filters": filters, "scope": "current_public_top_level_creations",
            "index_build_complete": bool((ctx.get_state(CONTROL, "build") or {}).get("complete")),
            "candidate_limit": CANDIDATE_LIMIT,
            "meaning": "曾公开的稳定引用按当前实体重新检查。只返回当前公开顶层创作，不含背包、关闭容器内容、附属子件或自动地形物件。"
                "作者姓名是同世界当前公开名，重名不合并身份，不表示作者在线。位置不是通路或远程行动许可；近处inspect后再操作。"
                "fields是实际保存值，不由过程标记推断运行／暂停。空页、有后页或索引未建立完不证明作品不存在。"}
    author = filters.get("author")
    if "name" in filters:
        scope = _scope(BY_NAME, filters["name"])
    elif author:
        scope = _scope(BY_AUTHOR, author)
    elif "author_name" in filters:
        scope = _scope(AUTHOR_NAMES, filters["author_name"])
        # A previous author-choice page remains a choice page, even if its
        # final page contains only one name. Never silently select that author.
        is_choice = cursor is not None and cursor["kind"] == "authors"
        after = _read_cursor(args.get("after"), filters, scope, "authors") if is_choice else None
        matches, more, after = _author_matches(ctx, scope, filters["author_name"], after, args.get("limit", 6))
        if len(matches) != 1 or more or is_choice:
            if args.get("after") and not is_choice:
                raise RuleViolation("作者查询已改变；重新读取第一页，不沿旧作品游标换作者。")
            return FunctionOutcome({**base, "works": [], "author_matches": matches,
                "author_identity_required": len(matches) > 1 or more or is_choice,
                "has_more": more,
                "next_cursor": _cursor(filters, scope, after, "authors") if more else None})
        author = matches[0]["author"]
        scope = _scope(BY_AUTHOR, author)
    else:
        scope = DIRECTORY
    after = _read_cursor(args.get("after"), filters, scope, "works")
    works, more, next_cursor = [], False, None
    # One candidate at a time keeps the exact last examined seek cursor when
    # the returned-row limit is reached; skipped private refs reveal no IDs.
    for _ in range(CANDIDATE_LIMIT):
        page = ctx.list_state(scope, limit=1, after=after, order="key")
        if not page["items"]:
            break
        entity = m.get_entity(ctx, page["items"][0]["value"]["entity_id"], required=False)
        more, after = page["has_more"], page["next_cursor"]
        if _eligible(entity):
            name = m.public_player_name(ctx, entity["created_by"])
            if ((not author or entity["created_by"] == author)
                    and ("author_name" not in filters or name is not None and _normalize(name) == filters["author_name"])
                    and ("name" not in filters or _normalize(entity["name"]) == filters["name"])):
                works.append(_row(ctx, player, entity, name))
        if len(works) >= args.get("limit", 6) or not more:
            break
    if more:
        next_cursor = _cursor(filters, scope, after)
    return FunctionOutcome({**base, "works": works, "has_more": more, "next_cursor": next_cursor,
                            "author_identity_required": "author_name" in filters and len({row["created_by"] for row in works}) > 1,
                            "order": "server_creation_time_desc_then_entity_id"})


def begin(ctx):
    """Start one bounded traversal of existing spatial keys, not old history."""
    if ctx.get_state(CONTROL, "build") is None:
        _put(ctx, CONTROL, "build", {"after": None, "chunk": None, "entity_after": None, "complete": False})
    resume(ctx)


def resume(ctx):
    work = ctx.get_state(CONTROL, "build")
    if work is None:
        begin(ctx)
        return
    if work["complete"]:
        return
    if getattr(ctx, "_grove_discovery_scheduled", False):
        return
    try:
        old = ctx.get_checkpoint(BUILD_KEY)
    except CheckpointNotFound:
        old = None
    ctx.schedule_checkpoint(BUILD_KEY, "grove.public_works.build", {}, due_at=ctx.now + 1,
                            expected_generation=old["generation"] if old else 0)
    ctx._grove_discovery_scheduled = True


def backfill(ctx, args):
    work = dict(ctx.get_state(CONTROL, "build"))
    if work["complete"]:
        return FunctionOutcome({"complete": True})
    if work["chunk"] is None:
        page = ctx.list_state(m.CHUNKS, limit=1, after=work["after"], order="key")
        if not page["items"]:
            work["complete"] = True
        else:
            work.update(chunk=page["items"][0]["key"], next_after=page["next_cursor"],
                        last_chunk=not page["has_more"], entity_after=None)
    if not work["complete"]:
        chunk = ctx.get_state(m.CHUNKS, work["chunk"], {})
        ids = sorted(eid for eid in chunk.get("entity_ids", [])
                     if work["entity_after"] is None or eid > work["entity_after"])
        chosen = ids[:BUILD_BATCH]
        entities = {eid: entity for eid in chosen if (entity := m.get_entity(ctx, eid, required=False)) is not None}
        sync_many(ctx, entities)
        if len(ids) > BUILD_BATCH:
            work["entity_after"] = chosen[-1]
        else:
            work.update(after=work.pop("next_after"), chunk=None, entity_after=None,
                        complete=work.pop("last_chunk"))
    _put(ctx, CONTROL, "build", work)
    resume(ctx)
    return FunctionOutcome({"complete": work["complete"]})


def specs():
    return (FunctionSpec("grove.public_works", public_works,
        {"type": "object", "additionalProperties": False, "properties": {
            "author": {"type": "string", "minLength": 1, "maxLength": 128},
            "author_name": {"type": "string", "minLength": 1, "maxLength": 40},
            "name": {"type": "string", "minLength": 1, "maxLength": 80},
            "limit": {"type": "integer", "minimum": 1, "maximum": 12},
            "after": {"type": "string", "minLength": 1, "maxLength": 16384}}}, access="read",
        description="Find actual public top-level creations by exact current author_name, stable author ID or exact work name. "
            "Names normalize width/case, never fuzzy-match; combine work name with one author filter. Omit filters for recent public creations. "
            "Page next_cursor using identical filters. Returned coordinates and saved fields are current read-only directory facts, "
            "not nearby observations or action permission. Move normally and inspect nearby before acting. "
            "Equal author names require choosing a real author ID or specifying the work name; no online-player list."),)


def state_specs():
    return tuple(StateRule(scope, "", {}, history="metadata") for scope in
                 (ENTRIES, DIRECTORY, BY_AUTHOR, BY_NAME, AUTHORS, AUTHOR_NAMES, CONTROL))


def checkpoint_specs():
    return (CheckpointSpec("grove.public_works.build", backfill, EMPTY),)
