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
GENERATOR_VERSION = 2
PLAYERS = "grove.player"
ENTITIES = "grove.entity"
CHUNKS = "grove.chunk"
PROCESSES = "grove.process"
META = "grove.meta"
MATERIALS = {"wood": {"fuel": 40}, "stone": {"fuel": 0}, "cloth": {"fuel": 16}}
CAPABILITIES = {"transport", "container", "blocking", "flammable", "wettable",
                "water_source", "ignition", "floating", "personal", "resource_node",
                "walkway", "canopy", "hearth"}
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


def chunk_landscape(cx, cy):
    if (cx, cy) == (0, 0):
        return "common_bank"
    return ("willow_grove", "reed_marsh", "stone_ridge", "open_bank")[stable_number("landscape", cx, cy) % 4]


def terrain(x, y):
    if abs(x) > WORLD_LIMIT or abs(y) > WORLD_LIMIT:
        return "rock"
    # A continuous, deterministic stream with accessible banks near the common.
    river_x = 6 + int(2 * math.sin(y / 11))
    if river_x <= x <= river_x + 2:
        return "water"
    if -24 <= x <= 8 and -24 <= y <= 24:
        return "grass"
    style = chunk_landscape(*chunk_for(x, y))
    ox, oy = x % 16, y % 16
    # Local pools and ridges make places differ without introducing fluid flow
    # or an impassable river gate. Every water tile remains directly wadeable.
    if style == "reed_marsh" and 7 <= ox <= 10 and 4 <= oy <= 10:
        return "water"
    value = stable_number("tile", x, y) % 100
    rock_limit = 13 if style == "stone_ridge" else 3
    return "rock" if value < rock_limit else "dirt" if value < 34 else "grass"


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
    style = chunk_landscape(cx, cy)
    resources = {"common_bank": ("wood", "cloth", "stone", "wood"),
                 "willow_grove": ("wood", "wood", "wood", "cloth"),
                 "reed_marsh": ("cloth", "cloth", "wood"),
                 "stone_ridge": ("stone", "stone", "wood"),
                 "open_bank": ("wood", "cloth", "stone")}[style]
    preferred = [(2, 2), (4, 2), (2, 5), (4, 5)] if style == "common_bank" else []
    land = preferred + [(x, y) for y in range(2, 15) for x in range(2, 15)
                        if (x, y) not in preferred]
    land = [(x, y) for x, y in land
            if max(abs(cx*16+x), abs(cy*16+y)) <= WORLD_LIMIT
            and terrain(cx*16+x, cy*16+y) not in {"water", "rock"}]
    if style != "common_bank":
        land.sort(key=lambda p: stable_number("node_position", cx, cy, *p))
    names = {"wood": "柳枝丛", "cloth": "芦苇丛", "stone": "卵石堆"}
    for i, material in enumerate(resources):
        if i >= len(land):
            break
        ox, oy = land[i]
        spawn_entity(ctx, {"id": f"node_{cx}_{cy}_{i}", "name": names[material],
                          "x": cx*16+ox, "y": cy*16+oy, "material": material,
                          "capabilities": ["resource_node", "wettable"], "source": "terrain",
                          "state": {"kind": "resource_node", "resource_kind": material,
                                    "stock": 4, "stock_capacity": 4, "regrowth_tick": None}})
    log = ("倒木", "wood", ["transport", "blocking", "flammable", "wettable"], {"kind": "raw_material"})
    bowl = ("石碗", "stone", ["transport", "container", "wettable"], {"capacity": 8, "water_capacity": 20})
    crate = ("木箱", "wood", ["transport", "container", "flammable", "wettable", "floating"], {"capacity": 8})
    cloth = ("布卷", "cloth", ["transport", "flammable", "wettable"], {"kind": "raw_material"})
    bucket = ("溪边木桶", "wood", ["transport", "container", "flammable", "wettable", "floating"],
              {"capacity": 4, "water_capacity": 12})
    stone = ("散落卵石", "stone", ["transport", "wettable"], {"kind": "raw_material"})
    clutter = {"common_bank": [log, crate, bowl, cloth], "willow_grove": [log, crate],
               "reed_marsh": [bucket, cloth], "stone_ridge": [bowl, stone],
               "open_bank": [crate, bowl, cloth]}[style]
    places = [(4, 4), (5, 5), (2, 6), (2, 7)] if style == "common_bank" else land[len(resources):]
    for i, (name, material, caps, state) in enumerate(clutter):
        if i >= len(places):
            break
        ox, oy = places[i]
        spawn_entity(ctx, {"id": f"seed_{cx}_{cy}_{ox}_{oy}", "name": name, "material": material,
                          "capabilities": caps, "state": state, "x": cx*16+ox, "y": cy*16+oy,
                          "source": "terrain"})
    # Water is a finite source for each materialized chunk, not infinite inventory.
    water_tiles = [(cx*16+x, cy*16+y) for y in range(16) for x in range(16)
                   if terrain(cx*16+x, cy*16+y) == "water"]
    if water_tiles:
        x, y = water_tiles[min(1, len(water_tiles)-1)]
        spawn_entity(ctx, {"id": f"water_{cx}_{cy}", "name": "溪水", "x": x, "y": y,
                          "material": "stone", "capabilities": ["water_source", "wettable"],
                          "state": {"water_remaining": 200, "water_source_capacity": 200}, "source": "terrain"})
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
    if entity["state"].get("retired"):
        return None, None
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
    return (not entity.get("state", {}).get("retired") and not entity.get("state", {}).get("destroyed") and entity.get("owner") is None
            and entity.get("container") is None and entity.get("x") is not None)


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


def can_place_ground(ctx, entity):
    """Keep room for the contents of every burning public container to fall."""
    if not is_ground(entity):
        return True
    chunk = get_chunk(ctx, *chunk_for(entity["x"], entity["y"]))
    roots = [entity]
    for eid in chunk["entity_ids"]:
        if eid != entity["id"]:
            root = get_entity(ctx, eid, required=False)
            if root and is_ground(root):
                roots.append(root)
    occupied = len(roots)
    for root in roots:
        if root["state"]["burning"] and "container" in root["capabilities"]:
            occupied += len(descendants(ctx, root))
    return occupied <= MAX_CHUNK_ENTITIES


def _index_entity(ctx, entity, add):
    if (entity.get("owner") is not None or entity.get("container") is not None
            or entity.get("x") is None or (add and not is_ground(entity))):
        return
    chunk = get_chunk(ctx, *chunk_for(entity["x"], entity["y"]))
    ids = chunk["entity_ids"]
    if add and not can_place_ground(ctx, entity):
        raise RuleViolation("这一片地方的物件或燃烧容器的内容释放空间已满。")
    if add and entity["id"] not in ids:
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
    if value["owner"] is not None or value["container"] is not None or value.get("state", {}).get("retired"):
        value["x"] = value["y"] = None
    elif type(value["x"]) is not int or type(value["y"]) is not int or max(abs(value["x"]), abs(value["y"])) > WORLD_LIMIT:
        raise RuleViolation("地面物件必须有合法坐标。")
    state = dict(value.get("state", {}))
    defaults = {"wetness": 0, "fuel": MATERIALS[material]["fuel"] if "flammable" in caps else 0,
                "burning": False, "destroyed": False, "open": True, "capacity": 0,
                "contents": [], "water": 0, "water_capacity": 0, "water_remaining": 0,
                "water_source_capacity": max(200, state.get("water_remaining", 0)) if "water_source" in caps else 0,
                "retired": False, "retired_into": None, "generation_allocation_retired": False}
    for key, default in defaults.items():
        state.setdefault(key, default)
    for key, maximum in (("wetness",100), ("fuel",200), ("capacity",32),
                         ("water",100), ("water_capacity",100), ("water_remaining",1000),
                         ("water_source_capacity",1000)):
        if type(state[key]) is not int or not 0 <= state[key] <= maximum:
            raise RuleViolation(f"物件 {key} 数量无效。")
    for key in ("burning", "destroyed", "open", "retired", "generation_allocation_retired"):
        if type(state[key]) is not bool:
            raise RuleViolation("物件状态必须为明确真假值。")
    if not isinstance(state["contents"], list) or len(state["contents"]) > state["capacity"] or len(set(state["contents"])) != len(state["contents"]):
        raise RuleViolation("容器容量不足或内容重复。")
    if "container" not in caps and (state["contents"] or state["water"] or state["water_capacity"]):
        raise RuleViolation("只有容器可以储物或装水。")
    if state["water"] > state["water_capacity"]:
        raise RuleViolation("水量超过容量。")
    if state["water_remaining"] > state["water_source_capacity"]:
        raise RuleViolation("水源超过有限容量。")
    if state.get("kind") == "resource_node":
        if state.get("resource_kind") not in MATERIALS or state.get("stock_capacity") != 4:
            raise RuleViolation("资源节点种类或容量无效。")
        if type(state.get("stock")) is not int or not 0 <= state["stock"] <= 4:
            raise RuleViolation("资源节点库存无效。")
        if state.get("regrowth_tick") is not None and (type(state["regrowth_tick"]) is not int or state["regrowth_tick"] < 0):
            raise RuleViolation("资源节点再生时刻无效。")
        if state["resource_kind"] == "stone" and state.get("regrowth_tick") is not None:
            raise RuleViolation("石头不能由浇水再生。")
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
    if previous and ((previous.get("x"), previous.get("y"), previous.get("owner"), previous.get("container")) != (value.get("x"), value.get("y"), value.get("owner"), value.get("container"))
                     or (is_ground(previous) and not is_ground(value))):
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


def retire_material(ctx, entity, building_id):
    if (entity["state"]["destroyed"] or entity["state"].get("retired") or entity["state"]["contents"]
            or entity["state"]["burning"] or "personal" in entity["capabilities"]):
        raise RuleViolation("只能投入未燃烧、未退役的单份可用材料。")
    remove_from_container(ctx, entity)
    if entity.get("owner"):
        player = get_player(ctx, entity["owner"])
        player["inventory"] = [eid for eid in player["inventory"] if eid != entity["id"]]
        save_player(ctx, player)
    entity.update(container=None, x=None, y=None)
    entity["state"].update(retired=True, retired_into=building_id)
    if entity["state"].get("generation_region"):
        entity["state"]["generation_allocation_retired"] = True
    return save_entity(ctx, entity)


def restore_material(ctx, entity, *, owner=None, container=None, x=None, y=None):
    if not entity["state"].get("retired") or entity["state"]["destroyed"]:
        raise RuleViolation("原材料不能重复返还。")
    if container:
        parent = get_entity(ctx, container)
        if (parent["owner"] != owner or "container" not in parent["capabilities"]
                or not parent["state"]["open"] or parent["state"]["destroyed"]
                or parent["state"]["burning"] or len(parent["state"]["contents"]) >= parent["state"]["capacity"]):
            raise RuleViolation("返还容器不可用或容量不足。")
        parent["state"]["contents"].append(entity["id"])
        save_entity(ctx, parent)
    entity["state"].update(retired=False, retired_into=None)
    return relocate_tree(ctx, entity, owner=owner, container=container, x=x, y=y)


def region_resources(ctx, cx, cy):
    nodes, sources = [], []
    for eid in get_chunk(ctx, cx, cy)["entity_ids"]:
        entity = get_entity(ctx, eid, required=False)
        if not entity or not is_ground(entity) or entity["state"]["destroyed"]:
            continue
        state = entity["state"]
        if state.get("kind") == "resource_node":
            nodes.append(entity)
        if "water_source" in entity["capabilities"]:
            sources.append(entity)
    return sorted(nodes, key=lambda e: e["id"]), sorted(sources, key=lambda e: e["id"])


def resource_stock(ctx, cx, cy):
    nodes, sources = region_resources(ctx, cx, cy)
    stock = {key: 0 for key in MATERIALS}
    for node in nodes:
        stock[node["state"]["resource_kind"]] += node["state"]["stock"]
    stock["water"] = sum(source["state"]["water_remaining"] for source in sources)
    return stock


def consume_resources(ctx, cx, cy, cost):
    """Spend current public node stock in the caller's publication transaction."""
    stock = resource_stock(ctx, cx, cy)
    if any(type(amount) is not int or amount < 0 or stock.get(kind, 0) < amount for kind, amount in cost.items()):
        raise StateConflict("本区域真实公共材料或水库存不足；不能凭空发表赠物。")
    nodes, sources = region_resources(ctx, cx, cy)
    consumed = []
    for kind, total in cost.items():
        remaining = total
        for source in sources if kind == "water" else nodes:
            if kind != "water" and source["state"]["resource_kind"] != kind:
                continue
            field = "water_remaining" if kind == "water" else "stock"
            amount = min(remaining, source["state"][field])
            if amount:
                source["state"][field] -= amount
                save_entity(ctx, source)
                consumed.append({"id": source["id"], "resource_kind": kind, "amount": amount, "version": source["version"]})
                remaining -= amount
            if not remaining:
                break
    return consumed


def record_change(ctx, summary, *, x, y, entity_id=None, action=None):
    changes = ctx.get_state(META, "changes", [])
    changes.append({"summary": summary[:180], "x": x, "y": y, "entity_id": entity_id,
                    "action": action, "at": ctx.now})
    ctx.set_state(META, "changes", changes[-24:])
