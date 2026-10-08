"""Shared Grove's spatial, material and ownership facts.

Indexes are authoritative state maintained in the same transaction as an entity.
Views and processes read bounded chunk/index keys, never scan world scopes.
"""
from __future__ import annotations

import hashlib
import math
import uuid
from copy import deepcopy

from agent_world.simulation import matter as sm
from agent_world.simulation.effects import Sink, Source, Transfer
from agent_world.simulation.frame import Frame
from .definitions import REGISTRY, RULES_VERSION, PIECE_QUANTITIES, registry_for

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
CREATIVE_ACCOUNTING = "grove.creative.accounting"
CREATIVE_ACCOUNT_REASONS = {"seed", "combustion", "creative_reaction", "creative_charge", "mechanical_fuel"}
MATERIALS = {key: {"fuel": definition.piece_quantity if definition.combustible else 0}
             for key, definition in REGISTRY.materials.items() if key != "water"}
CAPABILITIES = {"transport", "container", "blocking", "flammable", "wettable",
                "water_source", "ignition", "floating", "personal", "resource_node",
                "walkway", "canopy", "hearth"}
CAPABILITIES.update({"attachment", "covering", "buoyant", "broken"})
MAX_CHUNK_ENTITIES = 96
MAX_INVENTORY = 64
MAX_ACTIVE = sm.MAX_FRAME_OBJECTS
MAX_CHUNK_BYTES = 16384
MAX_META_BYTES = 8192


def liquid_quantity(entity, slot="contained", registry=None):
    registry = registry_for(entity) if registry is None else registry
    return sum(lot["quantity"] for lot in entity.get("matter", [])
               if registry.materials[lot["material"]].liquid and (slot is None or lot["slot"] == slot))


def liquid_capacity(entity):
    components = entity.get("components", {})
    return components.get("water_source", {}).get("capacity", components.get("container", {}).get(
        "liquid_capacity", components.get("collector", {}).get("capacity", 0)))


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
    if hasattr(ctx, "_grove_chunks") and cid in ctx._grove_chunks:
        return ctx._grove_chunks[cid]
    rec = ctx.get_state_record(CHUNKS, cid)
    if rec["exists"]:
        return {**rec["value"], "version": rec["version"]}
    return {"id": cid, "cx": cx, "cy": cy, "seed": SEED, "generator_version": GENERATOR_VERSION,
            "tiles": [terrain(cx*16+x, cy*16+y) for y in range(16) for x in range(16)],
            "entity_ids": [], "player_ids": [], "safe_zones": [], "version": rec["version"]}


def save_chunk(ctx, chunk):
    value = {k: v for k, v in chunk.items() if k != "version"}
    reservoirs = value.get("water_reservoirs", {})
    if not isinstance(reservoirs, dict) or len(reservoirs) > 256:
        raise RuleViolation("地块泄水坐标超过256格。")
    for key, amount in reservoirs.items():
        try:
            point = tuple(map(int, key.split(",")))
        except (ValueError, AttributeError):
            raise RuleViolation("泄水坐标无效。") from None
        if len(point) != 2 or chunk_for(*point) != (chunk["cx"], chunk["cy"]) or type(amount) is not int or not 0 <= amount <= sm.MAX_QUANTITY:
            raise RuleViolation("泄水坐标或真实数量超过地块界限。")
    if sm.encoded_size(value) > MAX_CHUNK_BYTES:
        raise RuleViolation("地块记录超过16KiB空间预算。")
    if hasattr(ctx, "_grove_chunks"):
        ctx._grove_chunks[chunk["id"]] = chunk
        ctx._grove_dirty_chunks.add(chunk["id"])
        return chunk
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
    if hasattr(ctx, "_grove_players") and role_id in ctx._grove_players:
        return deepcopy(ctx._grove_players[role_id])
    rec = ctx.get_state_record(PLAYERS, role_id)
    if not rec["exists"]:
        if required:
            raise RuleViolation("请先连接自己的 Agent 并进入溪源。")
        return None
    return {**rec["value"], "version": rec["version"]}


def save_player(ctx, player, expected_version=None):
    if len(player.get("inventory", [])) > MAX_INVENTORY or sm.encoded_size(player) > MAX_META_BYTES:
        raise RuleViolation("背包记录超过受保护空间预算。")
    if hasattr(ctx, "_grove_players"):
        ctx._grove_players[player["id"]] = player
        ctx._grove_dirty_players.add(player["id"])
        return player
    version = ctx.set_state(PLAYERS, player["id"], {k:v for k,v in player.items() if k != "version"},
                            expected_version=player.get("version") if expected_version is None else expected_version)
    player["version"] = version
    return player


def check_player_version(player, expected):
    if player["version"] != expected:
        raise StateConflict("你的位置或背包已经变化，请刷新后再行动。")


def get_entity(ctx, entity_id, *, required=True):
    if hasattr(ctx, "_grove_entities") and entity_id in ctx._grove_entities:
        return deepcopy(ctx._grove_entities[entity_id])
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
        if current.get("physical_parent"):
            current = get_entity(ctx, current["physical_parent"]["id"])
        elif current.get("owner"):
            player = get_player(ctx, current["owner"], required=False)
            return (player["x"], player["y"]) if player else (None, None)
        else:
            return current.get("x"), current.get("y")
    raise RuleViolation("容器层数超过限制。")


def is_ground(entity):
    return (not entity.get("state", {}).get("retired") and not entity.get("state", {}).get("destroyed") and entity.get("owner") is None
            and not entity.get("physical_parent") and entity.get("container") is None and entity.get("x") is not None)


def descendants(ctx, entity):
    result, seen = [], {entity["id"]}
    pending = list(entity["state"].get("contents", []))+list(entity["state"].get("attachments", []))
    while pending:
        eid = pending.pop(0)
        if eid in seen or len(result) >= sm.MAX_DESCENDANTS:
            raise RuleViolation("容器关系无效或物件过多。")
        seen.add(eid)
        child = get_entity(ctx, eid)
        result.append(child)
        pending.extend(child["state"].get("contents", []))
        pending.extend(child["state"].get("attachments", []))
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
    return any(e["id"] != ignore and e.get("lifecycle", "intact") == "intact"
               and e.get("components", {}).get("barrier", {}).get("active", False) is True
               and not e["state"]["destroyed"]
               for e in public_entities_near(ctx, x, y, radius=0, limit=96))


def public_break_reservations(ctx, entity, *, objects=None):
    """Bound possible public releases using the reducer's containment policy.

    An attached vessel is outside its parent's lid. A contained vessel can
    instead release into the nearest intact containing ancestor, including its
    protected remnant when that ancestor's object slots are full.
    """
    objects = _tree_objects(ctx, entity) if objects is None else objects
    root = objects.get(entity["id"], entity)
    seen = set()
    while root.get("physical_parent"):
        if root["id"] in seen or len(seen) >= sm.MAX_TREE_DEPTH:
            raise RuleViolation("物理关系不能循环或超过8层。")
        seen.add(root["id"])
        root = objects[root["physical_parent"]["id"]]
    if not is_ground(root):
        # A private root keeps released identities and water under its current
        # holder. Provenance never grants permanent private ownership.
        return {"ground_ids": set(), "contact_tiles": set()}
    members, pending, seen = {}, [root], set()
    while pending:
        member = pending.pop()
        if member["id"] in seen or len(members) >= sm.MAX_DESCENDANTS+1:
            raise RuleViolation("物理树不能循环或超过16个后代。")
        seen.add(member["id"])
        members[member["id"]] = member
        pending.extend(sm.children(member, objects))
    at_risk = set()
    try:
        registry = registry_for(members)
    except ValueError as exc:
        raise RuleViolation(str(exc)) from exc
    for eid, member in members.items():
        if member.get("lifecycle", "intact") != "intact" or member["state"].get("retired"):
            continue
        try:
            overloaded = bool(member["components"].get("structure") and sm.load_summary(registry, member, members)["overload"])
        except ValueError as exc:
            raise RuleViolation(str(exc)) from exc
        if overloaded or any(lot.get("burning") for lot in member["matter"]):
            at_risk.add(eid)
    released, contact = set(), set()
    for eid in at_risk:
        member, recipient = members[eid], None
        current = member
        while current.get("physical_parent"):
            relation = current["physical_parent"]
            parent = members[relation["id"]]
            if (relation["relation"] == "contained" and parent["id"] not in at_risk
                    and parent.get("lifecycle", "intact") == "intact" and "container" in parent["components"]):
                recipient = parent
                break
            current = parent
        if recipient is None:
            released.update(child["id"] for child in sm.children(member, members)
                            if child.get("lifecycle", "intact") != "consumed" and not child["state"].get("retired"))
            # Reserve receivers even before there is stored water: water can
            # arrive in this admitted frame before damage and structural failure.
            point = entity_location(ctx, member)
            if point[0] is not None:
                contact.add(point)
    return {"ground_ids": released, "contact_tiles": contact}


def can_place_ground(ctx, entity):
    """Keep public slots for every at-risk member of the bounded forests."""
    if not is_ground(entity):
        return True
    chunk = get_chunk(ctx, *chunk_for(entity["x"], entity["y"]))
    roots = [entity]
    for eid in chunk["entity_ids"]:
        if eid != entity["id"]:
            root = get_entity(ctx, eid, required=False)
            if root and is_ground(root) and chunk_for(root["x"], root["y"]) == (chunk["cx"], chunk["cy"]):
                roots.append(root)
    occupied = {root["id"] for root in roots}
    for root in roots:
        occupied.update(public_break_reservations(ctx, root)["ground_ids"])
    return len(occupied) <= MAX_CHUNK_ENTITIES


def _index_entity(ctx, entity, add):
    if (entity.get("owner") is not None or entity.get("physical_parent") or entity.get("container") is not None
            or entity.get("x") is None or (add and not is_ground(entity))):
        return
    chunk = get_chunk(ctx, *chunk_for(entity["x"], entity["y"]))
    ids = chunk["entity_ids"]
    if add and not can_place_ground(ctx, entity):
        raise RuleViolation("这一片地方的物件或结构失效内容释放空间已满。")
    if add and entity["id"] not in ids:
        ids.append(entity["id"])
    elif not add and entity["id"] in ids:
        ids.remove(entity["id"])
    else:
        return
    save_chunk(ctx, chunk)


def normalize_entity(entity):
    value = deepcopy({k:v for k,v in entity.items() if k != "version"})
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
    value.setdefault("physical_parent", {"id": value["container"], "relation": "contained"} if value["container"] else None)
    parent = value["physical_parent"]
    if parent is not None and (not isinstance(parent, dict) or set(parent) != {"id", "relation"} or parent["relation"] not in ("contained", "attached") or parent["id"] == value["id"]):
        raise RuleViolation("物件只能有一个合法物理父关系。")
    value["container"] = parent["id"] if parent and parent["relation"] == "contained" else None
    if value["owner"] is not None or parent or value.get("state", {}).get("retired"):
        value["x"] = value["y"] = None
    elif type(value["x"]) is not int or type(value["y"]) is not int or max(abs(value["x"]), abs(value["y"])) > WORLD_LIMIT:
        raise RuleViolation("地面物件必须有合法坐标。")
    state = dict(value.get("state", {}))
    defaults = {"open": True, "contents": [], "attachments": [], "retired": False, "retired_into": None,
                "generation_allocation_retired": False}
    for key, default in defaults.items():
        state.setdefault(key, default)
    for key in ("open", "retired", "generation_allocation_retired"):
        if type(state[key]) is not bool:
            raise RuleViolation("物件状态必须为明确真假值。")
    value["state"] = state
    try:
        registry = registry_for(value)
    except ValueError as exc:
        raise RuleViolation(str(exc)) from exc
    # These defaults are a trusted new-object constructor, not a migration of
    # old percentage/fuel facts. Once matter exists, those projections are ignored.
    if "matter" not in value:
        value["matter"] = [{"id": "body", "material": material, "quantity": PIECE_QUANTITIES[material], "slot": "body", "role": "structural", "burning": False}]
        if "water_source" in caps:
            value["matter"].append({"id": "source", "material": "water", "quantity": state.get("water_remaining", 200), "slot": "contained", "role": "substance", "burning": False})
        if "resource_node" in caps:
            resource = state.get("resource_kind", material)
            value["matter"].append({"id": "stock", "material": resource, "quantity": state.get("stock", 4)*PIECE_QUANTITIES[resource], "slot": "contained", "role": "substance", "burning": False})
    try:
        value["matter"] = sm.normalize_lots(registry, value["id"], value["matter"])
        if any(lot["slot"] == "absorbed" and lot["material"] != "water" for lot in value["matter"]):
            raise ValueError("吸附槽只支持已注册的水吸附机制。")
    except ValueError as exc:
        raise RuleViolation(str(exc)) from exc
    if "components" not in value:
        components = {}
        if "container" in caps:
            components["container"] = {"slots": min(16, state.get("capacity", 4)), "liquid_capacity": state.get("water_capacity", 0), "open": state["open"]}
            components["structure"] = {"integrity": 100, "max_integrity": 100, "support_limit": 48, "overload_rate": 1}
        absorption = registry.materials[material].absorption_per_piece
        if "wettable" in caps and absorption:
            components["absorbent"] = {"capacity": absorption, "rate": 2}
        if any(registry.materials[lot["material"]].combustible for lot in value["matter"]):
            components["combustion"] = {"rate": 2, "spread": True}
        if "floating" in caps:
            components["buoyant"] = {"capacity": 96}
        if "blocking" in caps:
            components["barrier"] = {"active": True}
            components.setdefault("structure", {"integrity": 100, "max_integrity": 100, "support_limit": 0, "overload_rate": 1})
        if "personal" in caps:
            components["personal"] = {"protected": True}
        if "ignition" in caps:
            components["ignition"] = {"enabled": True}
        if "water_source" in caps:
            components["water_source"] = {"capacity": state.get("water_source_capacity", 200)}
        if "resource_node" in caps:
            components["resource"] = {"material": state.get("resource_kind", material), "capacity": 4}
        value["components"] = components
    try:
        value["components"] = sm.validate_components(registry, value["components"])
        if any(registry.materials[lot["material"]].combustible for lot in value["matter"]):
            value["components"].setdefault("combustion", {"rate": 2, "spread": "heater" not in value["components"]})
        else:
            value["components"].pop("combustion", None)
        for component in ("absorbent", "buoyant"):
            current = value["components"].get(component)
            if current:
                supports = current.setdefault("supports", {lot["id"]: {"quantity": lot["quantity"], "capacity": current["capacity"]}
                                                            for lot in value["matter"][:1] if lot["slot"] == "body"})
                converted = {}
                for key, portion in supports.items():
                    global_id = key if key.startswith(value["id"]+":") else sm.lot_identifier(value["id"], key)
                    if not isinstance(portion, dict) or set(portion) != {"quantity", "capacity"} or type(portion["quantity"]) is not int or not 1 <= portion["quantity"] <= 10000 or type(portion["capacity"]) is not int or not 0 <= portion["capacity"] <= 1000:
                        raise ValueError("部件支撑材料参数无效。")
                    converted[global_id] = portion
                if len(converted) > sm.MAX_LOTS:
                    raise ValueError("部件支撑批次过多。")
                current["supports"] = converted
                body_lots = [lot for lot in value["matter"] if lot["slot"] == "body"]
                live_ids = {lot["id"] for lot in body_lots}
                for lot in body_lots:
                    if lot["id"] in converted:
                        continue
                    original = lot.get("origin_lot")
                    if original in converted and original not in live_ids:
                        converted[lot["id"]] = converted.pop(original)
                    elif component == "absorbent":
                        definition = registry.materials[lot["material"]]
                        capacity = definition.absorption_per_piece*lot["quantity"]//definition.piece_quantity
                        available = max(0, current["capacity"]-sum(portion["capacity"] for portion in converted.values()))
                        if capacity and available:
                            converted[lot["id"]] = {"quantity": lot["quantity"], "capacity": min(capacity, available)}
    except ValueError as exc:
        raise RuleViolation(str(exc)) from exc
    value.setdefault("lifecycle", "intact")
    if value["lifecycle"] not in ("intact", "broken", "consumed"):
        raise RuleViolation("物件生命周期无效。")
    for key in ("contents", "attachments"):
        if not isinstance(state[key], list) or len(state[key]) > sm.MAX_DESCENDANTS or len(set(state[key])) != len(state[key]):
            raise RuleViolation("物件关系超过16个后代界限或重复。")
    if set(state["contents"]).intersection(state["attachments"]):
        raise RuleViolation("同一物件不能同时容纳和绑定。")
    state.setdefault("initial_structural_quantity", sum(lot["quantity"] for lot in value["matter"] if lot["role"] == "structural"))
    sm.project(registry, value)
    state["liquid_quantity"] = liquid_quantity(value, registry=registry)
    state["liquid_capacity"] = liquid_capacity(value)
    components = value["components"]
    if state["contents"] and "container" not in components and value["lifecycle"] != "broken":
        raise RuleViolation("只有容器或受保护破损根可以容纳物件。")
    if "container" in components and len(state["contents"]) > components["container"]["slots"]:
        raise RuleViolation("容器物件槽不足。")
    program = state.get("program", {})
    if "mixture_capacity" in program:
        capacity = program["mixture_capacity"]
        if type(capacity) is not int or not 1 <= capacity <= 200:
            raise RuleViolation("混合物容量须为1至200的整数逻辑u。")
        if sm.quantity(value, slot="contained") > capacity:
            raise RuleViolation("真实混合物总量超过物件共用容量。")
    if state["liquid_quantity"] > state["liquid_capacity"]:
        raise RuleViolation("真实液体总量超过物件共用容量。")
    dynamic = {"container", "flammable", "wettable", "floating", "blocking", "attachment", "covering", "buoyant", "broken"}
    caps = [cap for cap in caps if cap not in dynamic]
    for component, tag in (("container", "container"), ("combustion", "flammable"), ("absorbent", "wettable"),
                           ("buoyant", "floating"), ("barrier", "blocking"), ("attachment", "attachment"), ("covering", "covering")):
        if component == "barrier" and components.get("barrier", {}).get("active", False) is not True:
            continue
        if component in components and (value["lifecycle"] == "intact" or (value["lifecycle"] == "broken" and component in {"combustion", "absorbent", "buoyant"})):
            caps.append(tag)
    if value["lifecycle"] == "broken":
        caps.append("broken")
    value["capabilities"] = sorted(set(caps))
    future = set()
    if components.get("container", {}).get("liquid_capacity", 0) or "collector" in components or "water_source" in components:
        future.add(("contained", None))
    for lot_id, portion in components.get("absorbent", {}).get("supports", {}).items():
        if portion["capacity"] and any(lot["id"] == lot_id for lot in value["matter"]):
            future.add(("absorbed", lot_id))
    if "absorbent" in components or any(registry.materials[lot["material"]].combustible for lot in value["matter"]):
        future.add(("surface", None))
    held = {(lot["slot"], lot.get("bound_to")) for lot in value["matter"] if lot["material"] == "water"}
    missing = future-held
    if sum(lot["quantity"] for lot in value["matter"] if lot["material"] == "water" and lot["slot"] in {"surface", "absorbed"}) > sm.MAX_QUANTITY:
        raise RuleViolation("表面和受湿水合计不能超过10000u。")
    if len(value["matter"])+len(missing) > sm.MAX_LOTS:
        raise RuleViolation("8个物质槽还须为真实吸水、储水和表面接触预留位置。")
    reserved_bytes = 64
    for slot, bound in missing:
        reserved = {"id": sm.lot_identifier(value["id"], ("water", slot, bound, None, None)), "material": "water",
                    "quantity": sm.MAX_QUANTITY, "slot": slot, "role": "substance", "burning": False}
        if bound:
            reserved["bound_to"] = bound
        reserved_bytes += sm.encoded_size(reserved)+1
    if sm.encoded_size(value)+reserved_bytes > sm.MAX_ENTITY_BYTES:
        raise RuleViolation("物件记录超过8KiB物理提交预算。")
    return value


def save_entity(ctx, entity, expected_version=None):
    value = normalize_entity(entity)
    if hasattr(ctx, "_grove_entities"):
        record = ctx.get_state_record(ENTITIES, value["id"])
        previous = {**record["value"], "version": record["version"]} if record["exists"] else None
    else:
        previous = get_entity(ctx, value["id"], required=False)
    expected = entity.get("version") if expected_version is None else expected_version
    if previous and expected is not None and previous["version"] != expected:
        raise StateConflict("物件已经变化，请刷新后再行动。")
    if previous and value == {k:v for k,v in previous.items() if k != "version"} and not (hasattr(ctx, "_grove_dirty_entities") and value["id"] in ctx._grove_dirty_entities):
        entity.update(previous)
        return previous
    if previous and ((previous.get("x"), previous.get("y"), previous.get("owner"), previous.get("container")) != (value.get("x"), value.get("y"), value.get("owner"), value.get("container"))
                     or (is_ground(previous) and not is_ground(value))):
        _index_entity(ctx, previous, False)
    if is_ground(value):
        _index_entity(ctx, value, True)
    if hasattr(ctx, "_grove_entities"):
        result = {**value, "version": previous["version"] if previous else 0}
        ctx._grove_entities[value["id"]] = result
        ctx._grove_dirty_entities.add(value["id"])
        entity.update(result)
        from .processes import reconcile_entity_processes
        reconcile_entity_processes(ctx, result)
        return result
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
    registry = registry_for(value)
    pool = getattr(ctx, "_grove_material_allocation", None)
    if value.get("source") == "agent":
        if pool is None:
            raise RuleViolation("Agent物件必须从真实区域材料转移而来。")
        needed = {material: sm.quantity(value, material) for material in registry.materials}
        if any(needed[k] > pool.get(k, 0) for k in needed):
            raise RuleViolation("生成物件的实际物质超过已投入库存。")
        for material, amount in needed.items():
            pool[material] = pool.get(material, 0)-amount
    else:
        for material in registry.materials:
            amount = sm.quantity(value, material)
            if amount:
                _record_account(ctx, "sources", material, amount, "seed")
    if is_ground(value) and is_safe(ctx, value["x"], value["y"]) and ("blocking" in value["capabilities"] or value["state"]["burning"]):
        raise RuleViolation("出生安全区不能放障碍或火源。")
    if value.get("physical_parent"):
        parent = get_entity(ctx, value["physical_parent"]["id"])
        if "container" not in parent["capabilities"] or parent["state"]["destroyed"]:
            raise RuleViolation("目标不是可用容器。")
        ancestor = parent
        for _ in range(8):
            if ancestor["state"]["burning"]:
                raise RuleViolation("燃烧容器及其内部容器不能添加内容。")
            if not ancestor.get("physical_parent"):
                break
            ancestor = get_entity(ctx,ancestor["physical_parent"]["id"])
        else:
            raise RuleViolation("容器层数超过限制。")
        if value["owner"] != parent["owner"]:
            raise RuleViolation("容器与内容的归属必须一致。")
        relation = value["physical_parent"]["relation"]
        result = save_entity(ctx, value, expected_version=0)
        parent["state"]["contents" if relation == "contained" else "attachments"].append(value["id"])
        save_entity(ctx, parent)
        return get_entity(ctx, result["id"])
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
    entity.update(owner=owner, container=container, physical_parent={"id": container, "relation": "contained"} if container else None, x=x, y=y)
    result = save_entity(ctx, entity)
    from .processes import reconcile_entity_processes
    reconcile_entity_processes(ctx, result)
    return result


def remove_from_container(ctx, entity):
    if entity.get("physical_parent"):
        relation = entity["physical_parent"]
        parent = get_entity(ctx, relation["id"])
        field = "contents" if relation["relation"] == "contained" else "attachments"
        parent["state"][field].remove(entity["id"])
        save_entity(ctx, parent)


def retire_material(ctx, entity, building_id):
    if (entity["state"]["destroyed"] or entity["state"].get("retired") or entity["state"]["contents"]
            or entity["state"]["burning"] or "personal" in entity["capabilities"]):
        raise RuleViolation("只能投入未燃烧、未退役的单份可用材料。")
    if sm.quantity(entity):
        raise RuleViolation("原料的实际物质须先转入施工或燃料槽，不能复制退役。")
    remove_from_container(ctx, entity)
    if entity.get("owner"):
        player = get_player(ctx, entity["owner"])
        player["inventory"] = [eid for eid in player["inventory"] if eid != entity["id"]]
        save_player(ctx, player)
    entity.update(container=None, physical_parent=None, x=None, y=None)
    entity["state"].update(retired=True, retired_into=building_id)
    if entity["state"].get("generation_region"):
        entity["state"]["generation_allocation_retired"] = True
    return save_entity(ctx, entity)


def restore_material(ctx, entity, *, owner=None, container=None, x=None, y=None):
    if not entity["state"].get("retired") or not sm.quantity(entity, slot="body"):
        raise RuleViolation("原材料不能重复返还。")
    entity["lifecycle"] = "intact"
    if container:
        parent = get_entity(ctx, container)
        if (parent["owner"] != owner or "container" not in parent["capabilities"]
                or not parent["state"]["open"] or parent["state"]["destroyed"]
                or parent["state"]["burning"] or len(parent["state"]["contents"]) >= parent["state"]["capacity"]):
            raise RuleViolation("返还容器不可用或容量不足。")
    entity["state"].update(retired=False, retired_into=None)
    result = relocate_tree(ctx, entity, owner=owner, container=container, x=x, y=y)
    if container:
        parent = get_entity(ctx, container)
        parent["state"]["contents"].append(entity["id"])
        save_entity(ctx, parent)
    return result


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
    pool = getattr(ctx, "_grove_material_allocation", None)
    if pool is None:
        pool = ctx._grove_material_allocation = {}
    for kind, total in cost.items():
        remaining = total
        for source in sources if kind == "water" else nodes:
            if kind != "water" and source["state"]["resource_kind"] != kind:
                continue
            field = "water_remaining" if kind == "water" else "stock"
            amount = min(remaining, source["state"][field])
            if amount:
                units = amount if kind == "water" else amount*PIECE_QUANTITIES[kind]
                left = units
                for lot in source["matter"]:
                    if lot["material"] == kind and lot["slot"] == "contained":
                        take = min(left, lot["quantity"])
                        lot["quantity"] -= take
                        left -= take
                if left:
                    raise StateConflict("资源节点的真实物质已经变化。")
                pool[kind] = pool.get(kind, 0)+units
                save_entity(ctx, source)
                consumed.append({"id": source["id"], "resource_kind": kind, "amount": amount, "version": source["version"]})
                remaining -= amount
            if not remaining:
                break
    return consumed


def finish_resource_allocation(ctx):
    if any(getattr(ctx, "_grove_material_allocation", {}).values()):
        raise RuleViolation("生成提交未完全转移投入的真实材料。")
    ctx._grove_material_allocation = None


def _record_account(ctx, category, material, amount, reason):
    if not amount:
        return
    if category not in {"sources", "sinks"} or type(amount) is not int or not 0 < amount <= 10**18:
        raise RuleViolation("物质账分类或数量无效。")
    if "#" in material:
        if reason not in CREATIVE_ACCOUNT_REASONS:
            raise RuleViolation("注册材料的物质账必须使用已声明的固定原因。")
        key = hashlib.sha256("\0".join((category, material, reason)).encode()).hexdigest()
        record = ctx.get_state_record(CREATIVE_ACCOUNTING, key)
        expected = {"category": category, "material": material, "reason": reason}
        if record["exists"]:
            previous = record["value"]
            if (not isinstance(previous, dict) or set(previous) != set(expected) | {"quantity"}
                    or any(previous[field] != value for field, value in expected.items())
                    or type(previous["quantity"]) is not int or not 0 <= previous["quantity"] <= 10**18):
                raise RuleViolation("注册材料的实际物质账无效。")
            amount += previous["quantity"]
        if amount > 10**18:
            raise RuleViolation("物质账的有界计数已满。")
        ctx.set_state(CREATIVE_ACCOUNTING, key, {**expected, "quantity": amount}, expected_version=record["version"])
        return
    from .processes import _meta_get, _meta_set
    account = _meta_get(ctx, "matter_accounting", {"unit": "u", "rules_version": RULES_VERSION, "sources": {}, "sinks": {}})
    key = material+":"+reason
    values = account[category]
    values[key] = values.get(key, 0)+amount
    if values[key] > 10**18 or len(values) > 32:
        raise RuleViolation("物质账的有界计数已满。")
    _meta_set(ctx, "matter_accounting", account)


def _tree_objects(ctx, entity):
    root, seen = entity, set()
    while root.get("physical_parent"):
        if root["id"] in seen or len(seen) >= sm.MAX_TREE_DEPTH:
            raise RuleViolation("物理关系不能循环或超过8层。")
        seen.add(root["id"])
        root = get_entity(ctx, root["physical_parent"]["id"])
    objects = {obj["id"]: obj for obj in [root, *descendants(ctx, root)]}
    # A relation change can precede updating its parent's inverse index in the
    # same transaction. The subject's complete subtree is still authoritative
    # for its own load and must not disappear from this bounded forest read.
    objects.update({obj["id"]: obj for obj in [entity, *descendants(ctx, entity)]})
    if len(objects) > sm.MAX_DESCENDANTS+1:
        raise RuleViolation("同一物理树最多16个后代。")
    return objects


def load_summary(ctx, entity):
    try:
        objects = _tree_objects(ctx, entity)
        return sm.load_summary(registry_for(objects), entity, objects)
    except ValueError as exc:
        raise RuleViolation(str(exc)) from exc


def preview_relation(ctx, target, item, relation):
    """Read one hypothetical reparenting without saving or running physics.

    Callers authorize the two visible subjects and filter affected identities.
    Numeric summaries deliberately omit material lots and descendant identities;
    closed public containers already disclose these same aggregate load fields.
    """
    if relation not in {"contained", "attached"}:
        raise RuleViolation("组合预览只支持容纳或绑定关系。")
    target = get_entity(ctx, target) if isinstance(target, str) else target
    item = get_entity(ctx, item) if isinstance(item, str) else item
    original = {**_tree_objects(ctx, target), **_tree_objects(ctx, item)}
    original[target["id"]], original[item["id"]] = target, item
    versions = ";".join(f"{eid}:{obj.get('version', 0)}" for eid, obj in sorted(original.items()))
    fingerprint = hashlib.sha256((RULES_VERSION+":"+relation+":"+target["id"]+":"+item["id"]+":"+versions).encode()).hexdigest()
    fields = ("unit", "local", "total", "supported", "limit", "overload", "integrity", "max_integrity", "buoyancy", "can_float")
    def summary(objects, eid):
        actual = sm.load_summary(registry_for(objects), objects[eid], objects)
        return {key: actual[key] for key in fields}
    def ancestry(objects, eid):
        result = set()
        for _ in range(sm.MAX_TREE_DEPTH):
            if eid in result:
                raise ValueError("物理关系不能循环。")
            result.add(eid)
            parent = objects[eid].get("physical_parent")
            if not parent:
                return result
            eid = parent["id"]
        raise ValueError("物理关系层数超过限制。")
    physics = {"known": False, "reason": None,
               "subject": {"id": target["id"], "name": target["name"], "version": target["version"]},
               "before": None, "after": None, "support_margin": None,
               "support_limit_applicable": "structure" in target.get("components", {}),
               "overloaded": None, "can_float": None, "affected": []}
    constraint = None
    try:
        physics["before"] = summary(original, target["id"])
        changed = ancestry(original, target["id"]) | ancestry(original, item["id"])
        if item["id"] in ancestry(original, target["id"]):
            raise ValueError("容纳或绑定关系不能循环。")
        hypothetical = deepcopy(original)
        moving = hypothetical[item["id"]]
        old_parent = moving.get("physical_parent")
        if old_parent:
            old = hypothetical[old_parent["id"]]
            for key in ("contents", "attachments"):
                old["state"][key] = [eid for eid in old["state"].get(key, []) if eid != moving["id"]]
        destination = hypothetical[target["id"]]
        owner = hypothetical[next(eid for eid in ancestry(hypothetical, target["id"])
                                  if not hypothetical[eid].get("physical_parent"))].get("owner")
        pending = [moving]
        while pending:
            obj = pending.pop()
            obj["owner"], obj["x"], obj["y"] = owner, None, None
            pending.extend(sm.children(obj, hypothetical))
        moving.update(physical_parent={"id": target["id"], "relation": relation},
                      container=target["id"] if relation == "contained" else None)
        key = "contents" if relation == "contained" else "attachments"
        destination["state"][key].append(item["id"])
        changed.update(ancestry(hypothetical, item["id"]))
        for eid in sorted(changed):
            before, after = summary(original, eid), summary(hypothetical, eid)
            obj = original[eid]
            limited = "structure" in hypothetical[eid].get("components", {})
            physics["affected"].append({"id": eid, "name": obj["name"], "version": obj["version"],
                "before": before, "after": after, "support_limit_applicable": limited,
                "support_margin": after["limit"]-after["supported"] if limited else None,
                "overloaded": after["overload"] > 0, "can_float": after["can_float"]})
        physics["after"] = summary(hypothetical, target["id"])
        physics.update(known=True, support_margin=physics["after"]["limit"]-physics["after"]["supported"] if physics["support_limit_applicable"] else None,
                       overloaded=physics["after"]["overload"] > 0, can_float=physics["after"]["can_float"])
        # Tree size is an action constraint, not an overload prohibition. Keep
        # useful numeric previews even when this finite relation bound is full.
        for eid in changed:
            if hypothetical[eid].get("physical_parent"):
                continue
            pending, count = [hypothetical[eid]], 0
            while pending:
                obj = pending.pop()
                count += 1
                pending.extend(sm.children(obj, hypothetical))
            if count > sm.MAX_DESCENDANTS+1:
                constraint = "同一物理树最多16个后代。"
    except (ValueError, KeyError) as exc:
        physics.update(known=False, reason=str(exc) if isinstance(exc, ValueError) else "相关物理关系不完整，无法预览。",
                       after=None, support_margin=None, overloaded=None, can_float=None, affected=[])
        constraint = physics["reason"]
    return {"physics": physics, "constraint_reason": constraint, "version_fingerprint": fingerprint}


def eligible_drift(ctx, entity):
    return bool(is_ground(entity) and entity["lifecycle"] == "intact" and not is_safe(ctx, entity["x"], entity["y"])
                and tile_at(ctx, entity["x"], entity["y"]) == "water" and load_summary(ctx, entity)["can_float"])


def _apply_matter(ctx, objects, effects, *, sources=(), sinks=()):
    try:
        frame = Frame(registry_for(objects), {obj["id"]: obj for obj in objects})
        frame.apply(effects, allowed_sources=sources, allowed_sinks=sinks)
        frame.verify_balance()
    except ValueError as exc:
        raise RuleViolation(str(exc)) from exc
    for category, values in (("sources", frame.sources), ("sinks", frame.sinks)):
        for (material, reason), amount in values.items():
            _record_account(ctx, category, material, amount, reason)
    for entity_id in sorted(frame.dirty):
        updated = save_entity(ctx, frame.objects[entity_id])
        for obj in objects:
            if obj["id"] == entity_id:
                obj.update(updated)
    return frame


def transfer_material(ctx, source, target, lot_id, amount, *, target_slot="contained", bound_to=None, reason="transfer"):
    source = get_entity(ctx, source) if isinstance(source, str) else source
    target = get_entity(ctx, target) if isinstance(target, str) else target
    if type(amount) is not int or amount <= 0 or target_slot not in {"body", "contained", "surface", "absorbed"}:
        raise RuleViolation("物质转移参数无效。")
    lot = next((lot for lot in source["matter"] if lot["id"] == lot_id), None)
    if not lot or lot["quantity"] < amount:
        raise RuleViolation("来源物质批次余额不足。")
    if source["id"] == target["id"] and lot["slot"] == target_slot:
        raise RuleViolation("物质已位于这个槽。")
    if target_slot == "absorbed" and lot["material"] != "water":
        raise RuleViolation("吸附槽只支持已注册的水吸附机制。")
    _apply_matter(ctx, [source] if source["id"] == target["id"] else [source, target],
                  [Transfer(source["id"], lot_id, target["id"], amount, target_slot, bound_to)])
    matches = [current for current in target["matter"] if current["material"] == lot["material"] and current["slot"] == target_slot
               and current.get("bound_to") == bound_to and (target_slot != "body" or current.get("origin_object") == lot.get("origin_object", source["id"]))]
    return {"transferred": amount, "quantity": amount, "source": source["id"], "target": target["id"],
            "lot_id": matches[-1]["id"] if matches else None, "source_version": source["version"], "target_version": target["version"]}


def transfer_water(ctx, source, target, amount, *, target_slot="contained", reason="pour"):
    source = get_entity(ctx, source) if isinstance(source, str) else source
    target = get_entity(ctx, target) if isinstance(target, str) else target
    if type(amount) is not int or amount <= 0:
        raise RuleViolation("转水量必须是正整数u。")
    lots = [lot for lot in source["matter"] if lot["material"] == "water" and lot["slot"] == "contained"]
    if sum(lot["quantity"] for lot in lots) < amount:
        raise RuleViolation("真实储水余额不足。")
    capacity = liquid_capacity(target) if target_slot == "contained" else sm.component_capacity(target, "absorbent")
    if target_slot == "surface":
        capacity = max(8, capacity)
    held = (liquid_quantity(target) if target_slot == "contained" else sm.quantity(target, "water", target_slot))
    if held+amount > capacity:
        raise RuleViolation("目标真实储水或吸水容量不足。")
    effects, remaining = [], amount
    targets = [[bound, available] for bound, available in sm.absorption_targets(target, _tree_objects(ctx, target))] if target_slot == "absorbed" else [[None, amount]]
    if sum(available for _, available in targets) < amount:
        raise RuleViolation("当前暴露的吸水材料容量不足。")
    for lot in lots:
        available = lot["quantity"]
        for destination in targets:
            take = min(remaining, available, destination[1])
            if take:
                effects.append(Transfer(source["id"], lot["id"], target["id"], take, target_slot, destination[0]))
                remaining -= take
                available -= take
                destination[1] -= take
    _apply_matter(ctx, [source] if source["id"] == target["id"] else [source, target], effects)
    if target_slot in {"absorbed", "surface"}:
        extinguish(ctx, target)
    return {"transferred": amount, "amount": amount, "source": source["id"], "target": target["id"],
            "source_id": source["id"], "target_id": target["id"], "source_version": source["version"], "target_version": target["version"]}


def add_water_from_source(ctx, entity, amount, source):
    return transfer_water(ctx, source, entity, amount, reason="fill")


def contact_water(ctx, target, amount, *, source=None, reason="contact"):
    target = get_entity(ctx, target) if isinstance(target, str) else target
    amount = min(amount, sum(available for _, available in sm.absorption_targets(target, _tree_objects(ctx, target))))
    if amount <= 0:
        return {"transferred": 0, "target": target["id"]}
    if source is None:
        x, y = entity_location(ctx, target)
        if x is None or tile_at(ctx, x, y) != "water":
            return {"transferred": 0, "target": target["id"]}
        _, sources = region_resources(ctx, *chunk_for(x, y))
        source = next((obj for obj in sources if obj["state"]["water_remaining"]), None)
        if source is None:
            return {"transferred": 0, "target": target["id"]}
    source = get_entity(ctx, source) if isinstance(source, str) else source
    amount = min(amount, sm.quantity(source, "water", "contained"))
    return transfer_water(ctx, source, target, amount, target_slot="absorbed", reason=reason) if amount else {"transferred": 0, "target": target["id"]}


def consume_water(ctx, entity, amount, reason="tend"):
    entity = get_entity(ctx, entity) if isinstance(entity, str) else entity
    if type(amount) is not int or amount <= 0 or sm.quantity(entity, "water", "contained") < amount:
        raise RuleViolation("真实储水量不足。")
    remaining, effects = amount, []
    for lot in entity["matter"]:
        if lot["material"] == "water" and lot["slot"] == "contained":
            take = min(remaining, lot["quantity"])
            if take:
                effects.append(Sink(entity["id"], lot["id"], take, reason))
                remaining -= take
    _apply_matter(ctx, [entity], effects, sinks={reason})
    return {"consumed": amount, "reason": reason, "entity_id": entity["id"]}


def start_burning(ctx, entity, lot_id=None):
    from .processes import can_ignite
    entity = get_entity(ctx, entity) if isinstance(entity, str) else entity
    objects = _tree_objects(ctx, entity)
    try:
        registry = registry_for(objects)
    except ValueError as exc:
        raise RuleViolation(str(exc)) from exc
    if sm.quantity(entity, "water", "surface") or not can_ignite(ctx, entity):
        raise RuleViolation("当前湿度、保护或物理预算不允许点燃。")
    selected = [lot for lot in entity["matter"] if (lot_id is None or lot["id"] == lot_id) and registry.materials[lot["material"]].combustible and sm.lot_moisture(entity, lot) < 35 and sm.exposed(entity, lot, objects)]
    if not selected:
        raise RuleViolation("没有可暴露的可燃物质批次。")
    for lot in selected:
        lot["burning"] = True
    if "heater" in entity["components"] and any(lot["slot"] == "contained" for lot in selected):
        entity["components"]["heater"]["lit"] = True
    return save_entity(ctx, entity)


def extinguish(ctx, entity):
    entity = get_entity(ctx, entity) if isinstance(entity, str) else entity
    for lot in entity["matter"]:
        lot["burning"] = False
    if "heater" in entity["components"]:
        entity["components"]["heater"]["lit"] = False
    return save_entity(ctx, entity)


def can_attach(ctx, part, target):
    attachment = part.get("components", {}).get("attachment")
    if not attachment or attachment.get("fits") not in target.get("components", {}):
        return {"allowed": False, "reason": "这份部件没有适合目标的已注册绑定关系。"}
    if target.get("lifecycle", "intact") != "intact" or part.get("lifecycle", "intact") != "intact":
        return {"allowed": False, "reason": "破损物件不能绑定。"}
    if target.get("owner") not in (None, ctx.actor_role_id) or part.get("owner") not in (None, ctx.actor_role_id):
        return {"allowed": False, "reason": "不能操作他人的背包物件。"}
    if part["id"] == target["id"]:
        return {"allowed": False, "reason": "不能绑定自己。"}
    try:
        tree = descendants(ctx, target)
        if part["id"] in {obj["id"] for obj in tree} or target["id"] in {obj["id"] for obj in descendants(ctx, part)}:
            return {"allowed": False, "reason": "绑定不能重复或循环。"}
        if len(tree)+len(descendants(ctx, part))+1 > sm.MAX_DESCENDANTS:
            return {"allowed": False, "reason": "同一物理树最多16个后代。"}
    except RuleViolation as exc:
        return {"allowed": False, "reason": str(exc)}
    return {"allowed": True, "reason": None}


def _parent_relation(ctx, parent, child, relation):
    if child["id"] == parent["id"] or parent["id"] in {entity["id"] for entity in descendants(ctx, child)}:
        raise RuleViolation("物理父关系不能循环。")
    # Count the receiving root's complete forest, not only a nested container.
    # Moving its existing identities keeps their slots; an outside subtree adds
    # each identity once, matching the read-only hypothetical relation preview.
    parent_tree = _tree_objects(ctx, parent)
    root_id = next(eid for eid, entity in parent_tree.items() if not entity.get("physical_parent"))
    final_descendants = ((set(parent_tree)-{root_id})
                         | {entity["id"] for entity in descendants(ctx, child)} | {child["id"]})
    if len(final_descendants) > sm.MAX_DESCENDANTS:
        raise RuleViolation("物理树最多16个后代。")
    if relation == "attached":
        result = can_attach(ctx, child, parent)
        if not result["allowed"]:
            raise RuleViolation(result["reason"])
    elif "container" not in parent["components"] or not parent["components"]["container"]["open"] or len(parent["state"]["contents"]) >= parent["components"]["container"]["slots"]:
        raise RuleViolation("目标容器关闭、破损或物件槽已满。")
    for ancestor in _tree_objects(ctx, parent).values():
        if ancestor["state"]["burning"]:
            raise RuleViolation("燃烧容器及其内部不能装卸。")
    remove_from_container(ctx, child)
    root = parent
    while root.get("physical_parent"):
        root = get_entity(ctx, root["physical_parent"]["id"])
    relocate_tree(ctx, child, owner=root.get("owner"), container=parent["id"])
    child["physical_parent"] = {"id": parent["id"], "relation": relation}
    child["x"] = child["y"] = None
    parent = get_entity(ctx, parent["id"])
    parent["state"]["contents" if relation == "contained" else "attachments"].append(child["id"])
    save_entity(ctx, child)
    save_entity(ctx, parent)
    from .processes import reconcile_entity_processes
    reconcile_entity_processes(ctx, get_entity(ctx, parent["id"]))
    return child


def contain(ctx, parent, child):
    return _parent_relation(ctx, parent, child, "contained")


def attach(ctx, parent, child):
    return _parent_relation(ctx, parent, child, "attached")


def detach(ctx, child, *, owner=None, container=None, x=None, y=None):
    if x is None and owner is None and container is None:
        x, y = entity_location(ctx, child)
        owner = child.get("owner")
    remove_from_container(ctx, child)
    if container:
        return contain(ctx, get_entity(ctx, container), child)
    return relocate_tree(ctx, child, owner=owner, x=None if owner else x, y=None if owner else y)


def begin_batch(ctx):
    ctx._grove_entities, ctx._grove_chunks, ctx._grove_players = {}, {}, {}
    ctx._grove_dirty_entities, ctx._grove_dirty_chunks, ctx._grove_dirty_players = set(), set(), set()


def flush_batch(ctx):
    """Each dirty object/index/player is written exactly once, within hard caps."""
    if len(ctx._grove_dirty_entities) > sm.MAX_FRAME_OBJECTS or len(ctx._grove_dirty_chunks) > 8 or len(ctx._grove_dirty_players) > 4:
        raise RuleViolation("本步物理提交超过已预留的记录预算。")
    estimate = 0
    for scope, cache, dirty in ((ENTITIES, ctx._grove_entities, ctx._grove_dirty_entities),
                              (CHUNKS, ctx._grove_chunks, ctx._grove_dirty_chunks),
                              (PLAYERS, ctx._grove_players, ctx._grove_dirty_players)):
        for key in sorted(dirty):
            rec = ctx.get_state_record(scope, key)
            value = {k:v for k,v in cache[key].items() if k != "version"}
            estimate += sm.encoded_size(value)+sm.encoded_size(rec["value"] if rec["exists"] else None)+256
    # 600KiB covers bounded process/meta/outcome rows and Runtime journal framing.
    if estimate > 1400000:
        raise RuleViolation("本步物理提交超过2MiB中预留给对象和空间索引的预算。")
    for scope, cache, dirty in ((ENTITIES, ctx._grove_entities, ctx._grove_dirty_entities),
                              (CHUNKS, ctx._grove_chunks, ctx._grove_dirty_chunks),
                              (PLAYERS, ctx._grove_players, ctx._grove_dirty_players)):
        for key in sorted(dirty):
            rec = ctx.get_state_record(scope, key)
            value = {k:v for k,v in cache[key].items() if k != "version"}
            cache[key]["version"] = ctx.set_state(scope, key, value, expected_version=rec["version"])


def record_change(ctx, summary, *, x, y, entity_id=None, action=None):
    from .processes import _meta_get
    changes = _meta_get(ctx, "changes", [])
    changes.append({"summary": summary[:180], "x": x, "y": y, "entity_id": entity_id,
                    "action": action, "at": ctx.now})
    from .processes import _meta_set
    _meta_set(ctx, "changes", changes[-8:])
    from .creative import public_event
    public_event(ctx, action=action, entity_id=entity_id, x=x, y=y)
