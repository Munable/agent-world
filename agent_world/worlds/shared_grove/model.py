"""Shared Grove's spatial, material and ownership facts.

Indexes are authoritative state maintained in the same transaction as an entity.
Views and processes read bounded chunk/index keys, never scan world scopes.
"""
from __future__ import annotations

import hashlib
import math
import uuid

from agent_world.errors import RuleViolation, StateConflict

CHUNK_SIZE = 16
WORLD_LIMIT = 1024
SEED = "shared-grove-v1"
GENERATOR_VERSION = 1
PLAYERS = "grove.player"
ENTITIES = "grove.entity"
CHUNKS = "grove.chunk"
PROCESSES = "grove.process"
META = "grove.meta"
MATERIALS = {"wood": {"fuel": 40}, "stone": {"fuel": 0}, "cloth": {"fuel": 16}}
CAPABILITIES = {"transport", "container", "blocking", "flammable", "wettable",
                "water_source", "ignition", "floating", "personal"}
MAX_CHUNK_ENTITIES = 96
MAX_INVENTORY = 64
MAX_ACTIVE = 128


def identifier(prefix):
    return prefix + "_" + uuid.uuid4().hex


def chunk_id(cx, cy):
    return f"chunk_{cx}_{cy}"


def chunk_for(x, y):
    return x // CHUNK_SIZE, y // CHUNK_SIZE


def chunk_ids_around(x, y, radius=8):
    return [chunk_id(cx, cy)
            for cy in range((y-radius)//16, (y+radius)//16+1)
            for cx in range((x-radius)//16, (x+radius)//16+1)]


def stable_number(*parts):
    return int.from_bytes(hashlib.sha256(":".join(map(str, (SEED, *parts))).encode()).digest()[:8], "big")


def terrain(x, y):
    if abs(x) > WORLD_LIMIT or abs(y) > WORLD_LIMIT:
        return "rock"
    # A continuous, deterministic stream with accessible banks near the common.
    river_x = 6 + int(2 * math.sin(y / 11))
    if river_x <= x <= river_x + 2:
        return "water"
    if -24 <= x <= 8 and -24 <= y <= 24:
        return "grass"
    value = stable_number("tile", x, y) % 100
    return "rock" if value < 3 else "dirt" if value < 22 else "grass"


def get_chunk(ctx, cx, cy):
    cid = chunk_id(cx, cy)
    rec = ctx.get_state_record(CHUNKS, cid)
    if rec["exists"]:
        return {**rec["value"], "version": rec["version"]}
    return {"id": cid, "cx": cx, "cy": cy, "seed": SEED, "generator_version": GENERATOR_VERSION,
            "tiles": [terrain(cx*16+x, cy*16+y) for y in range(16) for x in range(16)],
            "entity_ids": [], "player_ids": [], "safe_zones": [], "version": rec["version"]}


def save_chunk(ctx, chunk):
    value = {k: v for k, v in chunk.items() if k != "version"}
    version = ctx.set_state(CHUNKS, chunk["id"], value, expected_version=chunk.get("version"))
    chunk["version"] = version
    return chunk


def ensure_chunk(ctx, cx, cy):
    chunk = get_chunk(ctx, cx, cy)
    if chunk["version"]:
        return chunk
    save_chunk(ctx, chunk)
    # Chunk creation seeds small, fixed material facts. A query never does this.
    candidates = [(4, 4, "wood", ["transport", "blocking", "flammable", "wettable"], "倒木"),
                  (2, 6, "stone", ["transport", "container", "wettable"], "石碗"),
                  (5, 5, "wood", ["transport", "container", "flammable", "wettable", "floating"], "木箱"),
                  (2, 7, "cloth", ["transport", "flammable", "wettable"], "布卷")]
    for ox, oy, material, capabilities, name in candidates:
        x, y = cx*16+ox, cy*16+oy
        if abs(x) > WORLD_LIMIT or abs(y) > WORLD_LIMIT or terrain(x, y) in {"water", "rock"}:
            continue
        spawn_entity(ctx, {"id": f"seed_{cx}_{cy}_{ox}_{oy}", "name": name, "x": x, "y": y,
                          "material": material, "capabilities": capabilities, "source": "terrain",
                          "state": {"capacity": 8 if "container" in capabilities else 0,
                                    "water_capacity": 20 if name == "石碗" else 0}})
    # Water is a finite source for each materialized chunk, not infinite inventory.
    water_tiles = [(cx*16+x, cy*16+y) for y in range(16) for x in range(16)
                   if terrain(cx*16+x, cy*16+y) == "water"]
    if water_tiles:
        x, y = water_tiles[min(1, len(water_tiles)-1)]
        spawn_entity(ctx, {"id": f"water_{cx}_{cy}", "name": "溪水", "x": x, "y": y,
                          "material": "stone", "capabilities": ["water_source"],
                          "state": {"water_remaining": 200}, "source": "terrain"})
    return get_chunk(ctx, cx, cy)


def tile_at(ctx, x, y):
    cx, cy = chunk_for(x, y)
    chunk = get_chunk(ctx, cx, cy)
    return chunk["tiles"][(y-cy*16)*16+(x-cx*16)]


def get_player(ctx, role_id=None, *, required=True):
    role_id = role_id or ctx.actor_role_id
    rec = ctx.get_state_record(PLAYERS, role_id)
    if not rec["exists"]:
        if required:
            raise RuleViolation("请先连接自己的 Agent 并进入溪源。")
        return None
    return {**rec["value"], "version": rec["version"]}


def save_player(ctx, player, expected_version=None):
    version = ctx.set_state(PLAYERS, player["id"], {k:v for k,v in player.items() if k != "version"},
                            expected_version=player.get("version") if expected_version is None else expected_version)
    player["version"] = version
    return player


def check_player_version(player, expected):
    if player["version"] != expected:
        raise StateConflict("你的位置或背包已经变化，请刷新后再行动。")


def get_entity(ctx, entity_id, *, required=True):
    rec = ctx.get_state_record(ENTITIES, entity_id)
    if not rec["exists"]:
        if required:
            raise RuleViolation("物件已经不存在。")
        return None
    return {**rec["value"], "version": rec["version"]}


def entity_location(ctx, entity):
    seen = set()
    current = entity
    for _ in range(8):
        if current["id"] in seen:
            raise RuleViolation("容器关系不能循环。")
        seen.add(current["id"])
        if current.get("owner"):
            player = get_player(ctx, current["owner"], required=False)
            return (player["x"], player["y"]) if player else (None, None)
        if current.get("container"):
            current = get_entity(ctx, current["container"])
        else:
            return current.get("x"), current.get("y")
    raise RuleViolation("容器层数超过限制。")


def is_ground(entity):
    return entity.get("owner") is None and entity.get("container") is None and entity.get("x") is not None


def descendants(ctx, entity):
    result, seen = [], {entity["id"]}
    pending = list(entity["state"].get("contents", []))
    while pending:
        eid = pending.pop(0)
        if eid in seen or len(result) >= MAX_INVENTORY:
            raise RuleViolation("容器关系无效或物件过多。")
        seen.add(eid)
        child = get_entity(ctx, eid)
        result.append(child)
        pending.extend(child["state"].get("contents", []))
    return result


def is_safe(ctx, x, y):
    if x is None or y is None:
        return False
    for cid in chunk_ids_around(x, y, 2):
        _, cx, cy = cid.split("_")
        for safe in get_chunk(ctx, int(cx), int(cy))["safe_zones"]:
            if max(abs(x-safe["x"]), abs(y-safe["y"])) <= safe["radius"]:
                return True
    return False


def public_entities_near(ctx, x, y, radius=8, limit=160):
    found = []
    for cid in chunk_ids_around(x, y, radius):
        _, cx, cy = cid.split("_")
        for eid in get_chunk(ctx, int(cx), int(cy))["entity_ids"]:
            entity = get_entity(ctx, eid, required=False)
            if entity and is_ground(entity) and max(abs(entity["x"]-x), abs(entity["y"]-y)) <= radius:
                found.append(entity)
    found.sort(key=lambda e: (abs(e["x"]-x)+abs(e["y"]-y), e["id"]))
    return found[:limit]


def public_players_near(ctx, x, y, radius=8):
    result = []
    for cid in chunk_ids_around(x, y, radius):
        _, cx, cy = cid.split("_")
        for pid in get_chunk(ctx, int(cx), int(cy))["player_ids"]:
            p = get_player(ctx, pid, required=False)
            if p and max(abs(p["x"]-x), abs(p["y"]-y)) <= radius:
                result.append({k:p[k] for k in ("id", "name", "x", "y", "version")})
    return result[:64]


def blocking_at(ctx, x, y, *, ignore=None):
    return any(e["id"] != ignore and "blocking" in e["capabilities"] and not e["state"]["destroyed"]
               for e in public_entities_near(ctx, x, y, radius=0, limit=96))


def _index_entity(ctx, entity, add):
    if not is_ground(entity):
        return
    chunk = get_chunk(ctx, *chunk_for(entity["x"], entity["y"]))
    ids = chunk["entity_ids"]
    if add and entity["id"] not in ids:
        if len(ids) >= MAX_CHUNK_ENTITIES:
            raise RuleViolation("这一片地方的物件已满。")
        ids.append(entity["id"])
    elif not add and entity["id"] in ids:
        ids.remove(entity["id"])
    else:
        return
    save_chunk(ctx, chunk)


def normalize_entity(entity):
    value = {k:v for k,v in entity.items() if k != "version"}
    value.setdefault("id", identifier("object"))
    value.setdefault("owner", None)
    value.setdefault("container", None)
    value.setdefault("x", None)
    value.setdefault("y", None)
    value.setdefault("created_by", None)
    value.setdefault("source", "interaction")
    value.setdefault("created_at", 0)
    value.setdefault("authored_description", "")
    material = value.get("material")
    caps = value.get("capabilities", [])
    if material not in MATERIALS or not isinstance(caps, list) or set(caps)-CAPABILITIES:
        raise RuleViolation("未知材质或物件能力。")
    if len(caps) != len(set(caps)):
        raise RuleViolation("能力不能重复。")
    if not isinstance(value.get("name"), str) or not 1 <= len(value["name"]) <= 80:
        raise RuleViolation("物件名称须为1至80字。")
    if not isinstance(value["authored_description"], str) or len(value["authored_description"]) > 600:
        raise RuleViolation("物件描述过长。")
    if value["owner"] is not None or value["container"] is not None:
        value["x"] = value["y"] = None
    elif type(value["x"]) is not int or type(value["y"]) is not int or max(abs(value["x"]), abs(value["y"])) > WORLD_LIMIT:
        raise RuleViolation("地面物件必须有合法坐标。")
    state = dict(value.get("state", {}))
    defaults = {"wetness": 0, "fuel": MATERIALS[material]["fuel"] if "flammable" in caps else 0,
                "burning": False, "destroyed": False, "open": True, "capacity": 0,
                "contents": [], "water": 0, "water_capacity": 0, "water_remaining": 0}
    for key, default in defaults.items():
        state.setdefault(key, default)
    for key, maximum in (("wetness",100), ("fuel",200), ("capacity",32),
                         ("water",100), ("water_capacity",100), ("water_remaining",1000)):
        if type(state[key]) is not int or not 0 <= state[key] <= maximum:
            raise RuleViolation(f"物件 {key} 数量无效。")
    for key in ("burning", "destroyed", "open"):
        if type(state[key]) is not bool:
            raise RuleViolation("物件状态必须为明确真假值。")
    if not isinstance(state["contents"], list) or len(state["contents"]) > state["capacity"] or len(set(state["contents"])) != len(state["contents"]):
        raise RuleViolation("容器容量不足或内容重复。")
    if "container" not in caps and (state["contents"] or state["water"] or state["water_capacity"]):
        raise RuleViolation("只有容器可以储物或装水。")
    if state["water"] > state["water_capacity"]:
        raise RuleViolation("水量超过容量。")
    if "flammable" not in caps and (state["fuel"] or state["burning"]):
        raise RuleViolation("该材质能力不能燃烧。")
    if state["destroyed"]:
        state["burning"] = False
    value["state"] = state
    return value


def save_entity(ctx, entity, expected_version=None):
    value = normalize_entity(entity)
    previous = get_entity(ctx, value["id"], required=False)
    expected = entity.get("version") if expected_version is None else expected_version
    if previous and expected is not None and previous["version"] != expected:
        raise StateConflict("物件已经变化，请刷新后再行动。")
    if previous and value == {k:v for k,v in previous.items() if k != "version"}:
        entity.update(previous)
        return previous
    if previous and (previous.get("x"), previous.get("y"), previous.get("owner"), previous.get("container")) != (value.get("x"), value.get("y"), value.get("owner"), value.get("container")):
        _index_entity(ctx, previous, False)
    if is_ground(value):
        _index_entity(ctx, value, True)
    version = ctx.set_state(ENTITIES, value["id"], value,
                            expected_version=expected)
    result = {**value, "version": version}
    entity.update(result)
    # Processes are registered by ability/state, not by particular object IDs.
    from .processes import reconcile_entity_processes
    reconcile_entity_processes(ctx, result)
    return result


def spawn_entity(ctx, entity):
    entity = dict(entity)
    entity.setdefault("id", identifier("object"))
    if get_entity(ctx, entity["id"], required=False):
        raise RuleViolation("物件标识已经存在。")
    entity.setdefault("created_at", ctx.now)
    value = normalize_entity(entity)
    if is_ground(value) and is_safe(ctx, value["x"], value["y"]) and ("blocking" in value["capabilities"] or value["state"]["burning"]):
        raise RuleViolation("出生安全区不能放障碍或火源。")
    if value.get("container"):
        parent = get_entity(ctx, value["container"])
        if "container" not in parent["capabilities"] or parent["state"]["destroyed"]:
            raise RuleViolation("目标不是可用容器。")
        ancestor = parent
        for _ in range(8):
            if ancestor["state"]["burning"]:
                raise RuleViolation("燃烧容器及其内部容器不能添加内容。")
            if not ancestor.get("container"):
                break
            ancestor = get_entity(ctx,ancestor["container"])
        else:
            raise RuleViolation("容器层数超过限制。")
        if value["owner"] != parent["owner"]:
            raise RuleViolation("容器与内容的归属必须一致。")
        parent["state"]["contents"].append(value["id"])
        save_entity(ctx, parent)
    return save_entity(ctx, value, expected_version=0)


def relocate_tree(ctx, entity, *, owner, container=None, x=None, y=None):
    tree = [entity, *descendants(ctx, entity)]
    old_owner = entity.get("owner")
    if old_owner:
        player = get_player(ctx, old_owner)
        player["inventory"] = [eid for eid in player["inventory"] if eid not in {e["id"] for e in tree}]
        save_player(ctx, player)
    if owner:
        player = get_player(ctx, owner)
        ids = [e["id"] for e in tree]
        if len(set(player["inventory"] + ids)) > MAX_INVENTORY:
            raise RuleViolation("背包物件过多。")
        player["inventory"] = list(dict.fromkeys(player["inventory"] + ids))
        save_player(ctx, player)
    for child in tree[1:]:
        child["owner"] = owner
        save_entity(ctx, child)
    entity.update(owner=owner, container=container, x=x, y=y)
    return save_entity(ctx, entity)


def remove_from_container(ctx, entity):
    if entity.get("container"):
        parent = get_entity(ctx, entity["container"])
        parent["state"]["contents"].remove(entity["id"])
        save_entity(ctx, parent)


def record_change(ctx, summary, *, x, y, entity_id=None, action=None):
    changes = ctx.get_state(META, "changes", [])
    changes.append({"summary": summary[:180], "x": x, "y": y, "entity_id": entity_id,
                    "action": action, "at": ctx.now})
    ctx.set_state(META, "changes", changes[-24:])
