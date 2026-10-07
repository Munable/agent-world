"""The formal Shared Grove world: people act; ordinary rules adjudicate."""
from __future__ import annotations

import json

from agent_world import FunctionOutcome, FunctionSpec, StateRule, TimerSpec, ViewSpec, WorldDefinition
from agent_world.errors import PermissionDenied, RuleViolation, StateConflict, TimerNotFound
from agent_world.world_streams import StreamEvent, StreamSpec

from . import model as m
from . import processes
from . import building

EMPTY = {"type": "object", "properties": {}, "additionalProperties": False}
ID = {"type": "string", "minLength": 1, "maxLength": 128}
VERSION = {"type": "integer", "minimum": 1}
COORD = {"type": "integer", "minimum": -m.WORLD_LIMIT, "maximum": m.WORLD_LIMIT}
NULL_ID = {"type": ["string", "null"], "maxLength": 128}
NULL_COORD = {"type": ["integer", "null"], "minimum": -m.WORLD_LIMIT, "maximum": m.WORLD_LIMIT}
JOIN_ARGS = {"type": "object", "properties": {"name": {"type": "string", "minLength": 1, "maxLength": 40}},
             "required": ["name"], "additionalProperties": False}
MOVE_ARGS = {"type": "object", "properties": {
    "dx": {"type": "integer", "minimum": -1, "maximum": 1},
    "dy": {"type": "integer", "minimum": -1, "maximum": 1},
    "expected_player_version": VERSION}, "required": ["dx", "dy", "expected_player_version"], "additionalProperties": False}
INSPECT_ARGS = {"type": "object", "properties": {"entity_id": ID}, "required": ["entity_id"], "additionalProperties": False}
BUILD_ARGS = {"type": "object", "properties": {
    "recipe": {"enum": list(building.RECIPES)}, "x": COORD, "y": COORD,
    "expected_player_version": VERSION,
    "material_ids": {"type": "array", "minItems": 2, "maxItems": 3, "uniqueItems": True, "items": ID}},
    "required": ["recipe", "x", "y", "expected_player_version"], "additionalProperties": False}
INTERACT_ARGS = {"type": "object", "properties": {
    "action": {"enum": ["pickup", "drop", "open", "close", "store", "retrieve", "fill", "pour", "ignite",
                         "harvest", "tend", "fuel", "dismantle", "switch_mode"]},
    "target_id": ID, "item_id": ID, "expected_player_version": VERSION,
    "expected_target_version": VERSION, "amount": {"type": "integer", "minimum": 1, "maximum": 100},
    "mode": {"enum": ["shelter", "collect"]}},
    "required": ["action", "expected_player_version"], "additionalProperties": False}
VIEW_ARGS = {"type": "object", "properties": {"cx": COORD, "cy": COORD,
    "radius": {"type": "integer", "minimum": 1, "maximum": 8}}, "additionalProperties": False}
TICK_ARGS = {"type": "object", "properties": {"epoch": ID, "sequence": {"type": "integer", "minimum": 0}},
             "required": ["epoch", "sequence"], "additionalProperties": False}

ENTITY_SCHEMA = {"type": "object", "properties": {
    "id": ID, "name": {"type": "string", "minLength": 1, "maxLength": 80},
    "x": NULL_COORD, "y": NULL_COORD, "material": {"enum": list(m.MATERIALS)},
    "capabilities": {"type": "array", "maxItems": len(m.CAPABILITIES), "uniqueItems": True,
                     "items": {"enum": sorted(m.CAPABILITIES)}},
    "owner": NULL_ID, "container": NULL_ID, "created_by": NULL_ID,
    "created_at": {"type": "number"}, "source": {"type": "string", "maxLength": 40},
    "authored_description": {"type": "string", "maxLength": 600},
    "state": {"type": "object", "properties": {
        "wetness": {"type": "integer", "minimum": 0, "maximum": 100},
        "fuel": {"type": "integer", "minimum": 0, "maximum": 200},
        "burning": {"type": "boolean"}, "destroyed": {"type": "boolean"}, "open": {"type": "boolean"},
        "capacity": {"type": "integer", "minimum": 0, "maximum": 32},
        "contents": {"type": "array", "maxItems": 32, "uniqueItems": True, "items": ID},
        "water": {"type": "integer", "minimum": 0, "maximum": 100},
        "water_capacity": {"type": "integer", "minimum": 0, "maximum": 100},
        "water_remaining": {"type": "integer", "minimum": 0, "maximum": 1000},
        "water_source_capacity": {"type": "integer", "minimum": 0, "maximum": 1000},
        "retired": {"type": "boolean"}, "retired_into": NULL_ID,
        "kind": {"type": "string", "maxLength": 40}, "resource_kind": {"enum": list(m.MATERIALS)},
        "stock": {"type": "integer", "minimum": 0, "maximum": 4}, "stock_capacity": {"const": 4},
        "regrowth_tick": {"type": ["integer", "null"], "minimum": 0},
        "recipe": {"enum": list(building.RECIPES)}, "mode": {"enum": ["shelter", "collect"]},
        "hearth_fuel": {"type": "integer", "minimum": 0, "maximum": building.HEARTH_FUEL_CAPACITY},
        "lit": {"type": "boolean"}, "dismantled": {"type": "boolean"},
        "invested_materials": {"type": "array", "maxItems": 3, "items": {"type": "object", "properties": {
            "id": ID, "material": {"enum": list(m.MATERIALS)},
            "fuel": {"type": "integer", "minimum": 0, "maximum": 200},
            "wetness": {"type": "integer", "minimum": 0, "maximum": 100}},
            "required": ["id", "material", "fuel", "wetness"], "additionalProperties": False}}},
        "required": ["wetness", "fuel", "burning", "destroyed", "open", "capacity", "contents",
                     "water", "water_capacity", "water_remaining"], "additionalProperties": True}},
    "required": ["id", "name", "x", "y", "material", "capabilities", "owner", "container",
                 "state", "created_by", "created_at", "source", "authored_description"], "additionalProperties": False}
PLAYER_SCHEMA = {"type": "object", "properties": {
    "id": ID, "name": {"type": "string", "minLength": 1, "maxLength": 40}, "x": COORD, "y": COORD,
    "backpack_id": ID, "inventory": {"type": "array", "uniqueItems": True, "maxItems": m.MAX_INVENTORY, "items": ID},
    "spawn": {"type": "object", "properties": {"x": COORD, "y": COORD}, "required": ["x", "y"], "additionalProperties": False},
    "created_at": {"type": "number"}},
    "required": ["id", "name", "x", "y", "backpack_id", "inventory", "spawn", "created_at"], "additionalProperties": False}
CHUNK_SCHEMA = {"type": "object", "properties": {
    "id": ID, "cx": {"type": "integer"}, "cy": {"type": "integer"},
    "seed": {"const": m.SEED}, "generator_version": {"const": m.GENERATOR_VERSION},
    "tiles": {"type": "array", "minItems": 256, "maxItems": 256, "items": {"enum": ["grass", "dirt", "water", "rock"]}},
    "entity_ids": {"type": "array", "uniqueItems": True, "maxItems": m.MAX_CHUNK_ENTITIES, "items": ID},
    "player_ids": {"type": "array", "uniqueItems": True, "maxItems": 48, "items": ID},
    "safe_zones": {"type": "array", "maxItems": 48, "items": {"type": "object", "properties": {
        "owner": ID, "x": COORD, "y": COORD, "radius": {"const": 1}}, "required": ["owner", "x", "y", "radius"], "additionalProperties": False}}},
    "required": ["id", "cx", "cy", "seed", "generator_version", "tiles", "entity_ids", "player_ids", "safe_zones"], "additionalProperties": False}
PROCESS_SCHEMA = {"type": "object", "properties": {
    "id": ID, "kind": {"enum": ["rain", "moisture", "burning", "drift", "growth"]},
    "status": {"enum": ["active", "complete"]}, "entity_id": NULL_ID,
    "chunk_ids": {"type": "array", "maxItems": 4, "items": ID},
    "remaining_ticks": {"type": "integer", "minimum": 0}, "start_tick": {"type": "integer", "minimum": 0},
    "created_at": {"type": "number"}, "created_by": NULL_ID,
    "entity_ids": {"type": "array", "maxItems": m.MAX_CHUNK_ENTITIES, "uniqueItems": True, "items": ID},
    "group_key": {"type": "string", "maxLength": 256}, "owner": NULL_ID,
    "member_limit": {"type": "integer", "minimum": 1, "maximum": m.MAX_CHUNK_ENTITIES},
    "reservations": {"type": "array", "maxItems": m.MAX_ACTIVE, "uniqueItems": True, "items": ID},
    "reservation_members": {"type": "object", "additionalProperties": {
        "type": "array", "maxItems": m.MAX_CHUNK_ENTITIES, "uniqueItems": True, "items": ID}},
    "end_tick": {"type": "integer", "minimum": 0},
    "moisture_chunks": {"type": "array", "maxItems": 4, "uniqueItems": True, "items": ID}},
    "required": ["id", "kind", "status", "entity_id", "chunk_ids", "remaining_ticks", "start_tick", "created_at", "created_by"],
    "additionalProperties": True}


def member(ctx, args=None):
    return m.get_player(ctx, required=False) is not None


def _state_authorizer(ctx, scope, key, access):
    # Trusted handlers project facts themselves. Generic state access must not
    # disclose another backpack or expose process/lease control internals.
    if ctx.actor_role_id in {"system:timer", "system:installer"}:
        return True
    if ctx.function_id.startswith("grove.") or ctx.function_id in {"map.viewport", "view:grove.map", "world.bootstrap"}:
        return True
    if access == "write":
        return False
    if scope == m.PLAYERS:
        return key == ctx.actor_role_id
    if scope == m.ENTITIES:
        value = ctx.authorization_state(scope, key)
        return value is not None and value.get("owner") == ctx.actor_role_id
    return scope == m.META and key in {"clock", "seed", "changes"}


def initialize(ctx):
    epoch = m.identifier("clock")
    ctx.set_state(m.META, "seed", m.SEED)
    ctx.set_state(m.META, "clock", {"epoch": epoch, "sequence": 0, "last_tick_at": ctx.now})
    ctx.set_state(m.META, "active_processes", [])
    ctx.set_state(m.META, "active_hearths", [])
    ctx.set_state(m.META, "join_count", 0)
    ctx.set_state(m.META, "changes", [])
    m.ensure_chunk(ctx, 0, 0)
    m.ensure_chunk(ctx, -1, 0)
    ctx.schedule_timer(f"grove.tick.{epoch}.0", "grove.tick", {"epoch": epoch, "sequence": 0}, due_at=ctx.now+1)


def migrate_batched_processes(ctx):
    # Runtime correctly refuses timers created under an older world contract.
    # Retain all material/player facts and replace only the scheduler instance.
    clock = ctx.get_state(m.META,"clock")
    old_timer = f"grove.tick.{clock['epoch']}.{clock['sequence']}"
    try:
        ctx.get_timer(old_timer)
    except TimerNotFound:
        pass
    else:
        ctx.cancel_timer(old_timer)
    clock.update(epoch=m.identifier("clock"),last_tick_at=ctx.now)
    ctx.set_state(m.META,"clock",clock)
    ctx.schedule_timer(f"grove.tick.{clock['epoch']}.{clock['sequence']}","grove.tick",
                       {"epoch":clock["epoch"],"sequence":clock["sequence"]},due_at=ctx.now+1)


def handbook(ctx, args):
    from .generation import handbook as generation_handbook
    creation = generation_handbook(ctx,{}).result["creation"]
    return FunctionOutcome({
        "world": "shared-grove", "title": "溪源", "player": _public_self(m.get_player(ctx,required=False)),
        "clock": ctx.get_state(m.META,"clock"), "process_order": ["weather", "moisture", "hearth", "burning", "drift", "growth"],
        "process_limits": {"wet_or_reserved_entities": processes.MAX_WET_ENTITIES,
                           "burning_entities": processes.MAX_BURNING_ENTITIES,
                           "burning_container_contents": processes.MAX_BURNING_CONTENTS,
                           "events": processes.MAX_EVENTS, "active_processes": m.MAX_ACTIVE,
                           "hearths": building.MAX_HEARTHS, "growth": 16},
        "terrain": {"grass": "草地", "dirt": "泥地", "water": "可涉浅溪，走入时随身可湿物件湿度加10；干脚通路免除此代价", "rock": "不能通行的岩壁"},
        "materials": {"wood": "木材可组合搬运、容器、障碍、可燃和受湿能力", "stone": "石材不燃烧", "cloth": "布燃料较少，容易受湿"},
        "capabilities": {"transport": "拿取与放下", "container": "开关、储取与有限装水", "blocking": "未毁坏时阻挡一步移动",
                         "flammable": "消耗有限燃料，湿度35及以上熄火", "wettable": "可以浇湿和干燥",
                         "water_source": "有限取水源", "ignition": "点火工具", "floating": "可在水上漂流",
                         "personal": "私人物件，不能丢弃、公开存入或点燃",
                         "resource_node": "有限库存；木布可花水养护，石材有限",
                         "walkway": "一格水面干脚通路", "canopy": "遮雨或有限集雨", "hearth": "真实消耗木料的受控烘干炉"},
        "rules": ["人控制四向一步移动和物件行动；自己的外部Agent读取、生成内容与解释。",
                  "行动需最新人物版本，物件归属只在服务器原子提交后改变。",
                  "公共物件需相邻一格才可互动，不能取用别人背包。",
                  "火只蔓延到相邻可燃的公共地面物件；出生安全区禁止火与障碍。",
                  "雨、受湿、燃烧、漂流按每秒顺序处理，服务停机时暂停，不补算缺席时间。",
                  "雨与漂流先按实际物件预留容量：受湿或已预留物件最多128件，燃烧对象最多16件，燃烧容器全部内容合计最多16件，活动过程索引最多128组。雨/漂流事件最多4个，同地块只能1个。超限的新动作会拒绝，新火蔓延暂缓。",
                  "每秒所有已接受物件都处理一次：非雨中湿度减2、燃料减2。降雨按固定逻辑结束tick结束，物件密度不改变这些速率；服务停机不推进逻辑tick。",
                  "配方成本按裸材料实体件数计；木布湿度须低于35，石材不受湿度限制。容器、点火工具和私人物件不能投料，原木障碍可以拿取后作料。",
                  "通路只铺一格浅溪；雨棚和烘干炉在草地或泥地建设，每格最多一座，出生安全区禁止火炉。",
                  "棚的遮雨模式保护半径1的九格公共地面材料，棚自身仍受雨；集雨模式取消遮雨，把雨存为同一容器余额，容量40。已有水保留，可装入自己的容器。",
                  "炉每份完整木料增加40燃料，上限200；另用打火石点火，每秒耗2燃料，半径1公共地面材料额外干6，多炉不叠加。受湿35及以上立即熄炉，余料保留，受控炉火不蔓延。",
                  "建筑可由附近任何人拆除，未烧尽的原材料只退还一次到拆除者背包。燃烧、有水或物品时不能拆；木建筑烧毁后投资不返，投炉燃料永不返。",
                  "采集资源节点立即消耗1份公共库存；木和布最多存4份，花本人容器5份水养护，6个逻辑秒后恢复1份，最多同时养护16处。石材有限，浇水不能造石。",
                  "容器烧毁后全部内容只释放一次；烧毁障碍可以通行。",
                  "生成须领取公共区域机会，目录与资源预算由服务器校验，文字描述不授予能力。"],
        "creation": creation,
        "construction": {"recipes": building.recipes(), "radius_metric": "chebyshev",
                         "limits": {"hearths": building.MAX_HEARTHS, "growth": 16},
                         "build_schema": BUILD_ARGS, "interact_schema": INTERACT_ARGS},
        "entry": "在本地连接器中让自己的OpenCode Agent实际回应并读取世界后，由人进入。"})


def bootstrap(ctx):
    value = handbook(ctx,{}).result
    return {"world": value["world"], "title": value["title"], "player": value["player"],
            "clock": value["clock"], "rules": value["rules"], "next_function": "grove.handbook"}


def _public_self(player):
    if player is None:
        return None
    return {k:player[k] for k in ("id","name","x","y","backpack_id","spawn","version")}


def _add_player_index(ctx, player, *, add):
    chunk = m.get_chunk(ctx,*m.chunk_for(player["x"],player["y"]))
    if add and player["id"] not in chunk["player_ids"]:
        if len(chunk["player_ids"]) >= 48:
            raise RuleViolation("这一片地方的人已满。")
        chunk["player_ids"].append(player["id"])
    elif not add and player["id"] in chunk["player_ids"]:
        chunk["player_ids"].remove(player["id"])
    else:
        return
    m.save_chunk(ctx,chunk)


def join(ctx,args):
    from .generation import offer_generation
    current = m.get_player(ctx, required=False)
    if current:
        return FunctionOutcome({"resumed": True, "player": _public_self(current), "viewport": viewport(ctx,{})})
    name = args["name"].strip()
    if not name:
        raise RuleViolation("名字不能为空。")
    slot = ctx.get_state(m.META,"join_count",0)
    if slot >= 128:
        raise RuleViolation("本地世界已有128个身份。")
    first_spawns = ((0,0),(0,3),(3,0),(0,-3))
    preferred = first_spawns[slot] if slot < len(first_spawns) else (-((slot-4)%8)*4-4,-((slot-4)//8)*4-8)
    position = None
    for distance in range(12):
        offsets = [(0,0)] if distance == 0 else [(distance,0),(-distance,0),(0,distance),(0,-distance)]
        for dx,dy in offsets:
            x,y = preferred[0]+dx,preferred[1]+dy
            m.ensure_chunk(ctx,*m.chunk_for(x,y))
            footprint = [(x+ox,y+oy) for oy in (-1,0,1) for ox in (-1,0,1)]
            if (m.tile_at(ctx,x,y) in {"grass","dirt"}
                and all(not m.is_safe(ctx,px,py) and m.tile_at(ctx,px,py)!="rock" and not m.blocking_at(ctx,px,py)
                        and not any(e["state"]["burning"] for e in m.public_entities_near(ctx,px,py,radius=0))
                        for px,py in footprint)
                and not m.public_players_near(ctx,x,y,radius=1)):
                position = x,y
                break
        if position:
            break
    if position is None:
        raise RuleViolation("附近没有安全出生点。")
    x,y = position
    backpack_id = m.identifier("backpack")
    player = {"id":ctx.actor_role_id,"name":name,"x":x,"y":y,"backpack_id":backpack_id,
              "inventory":[],"spawn":{"x":x,"y":y},"created_at":ctx.now,"version":0}
    m.save_player(ctx,player)
    _add_player_index(ctx,player,add=True)
    chunk = m.get_chunk(ctx,*m.chunk_for(x,y))
    chunk["safe_zones"].append({"owner":player["id"],"x":x,"y":y,"radius":1})
    m.save_chunk(ctx,chunk)
    backpack = m.spawn_entity(ctx,{"id":backpack_id,"name":"随身背包","material":"cloth",
        "capabilities":["container","wettable","personal"],"state":{"capacity":16,"open":True},
        "owner":player["id"],"source":"personal"})
    player["inventory"].append(backpack["id"])
    starters = [("水壶","stone",["transport","container","wettable"],{"water":20,"water_capacity":30,"capacity":2}),
                ("打火石","stone",["transport","ignition"],{"uses":20}),
                ("木柴","wood",["transport","flammable","wettable"],{}),
                ("干布","cloth",["transport","flammable","wettable"],{})]
    for item_name,material,caps,state in starters:
        item = m.spawn_entity(ctx,{"name":item_name,"material":material,"capabilities":caps,"state":state,
                 "owner":player["id"],"container":backpack_id,"source":"personal"})
        player["inventory"].append(item["id"])
    m.save_player(ctx,player)
    ctx.set_state(m.META,"join_count",slot+1)
    for cid in m.chunk_ids_around(x,y,8):
        _,cx,cy=cid.split("_")
        m.ensure_chunk(ctx,int(cx),int(cy))
    offer_generation(ctx,x,y,trigger="first_enter")
    m.record_change(ctx,f"{name}进入溪源。",x=x,y=y,action="join")
    return FunctionOutcome({"resumed":False,"player":_public_self(player),"viewport":viewport(ctx,{})},
            events=(StreamEvent("grove.changes","join",{"summary":f"{name}进入溪源。","x":x,"y":y},key="join"),))


def move(ctx,args):
    from .generation import offer_generation
    player=m.get_player(ctx)
    m.check_player_version(player,args["expected_player_version"])
    dx,dy=args["dx"],args["dy"]
    if abs(dx)+abs(dy)!=1:
        raise RuleViolation("每次只能向上、下、左、右走一格。")
    x,y=player["x"]+dx,player["y"]+dy
    if max(abs(x),abs(y))>m.WORLD_LIMIT:
        raise RuleViolation("这里是世界边界。")
    m.ensure_chunk(ctx,*m.chunk_for(x,y))
    if m.tile_at(ctx,x,y)=="rock" or m.blocking_at(ctx,x,y):
        raise RuleViolation("这一格被岩壁或未毁坏的物件挡住。")
    previous_chunk=m.chunk_for(player["x"],player["y"])
    if previous_chunk!=m.chunk_for(x,y):
        _add_player_index(ctx,player,add=False)
    player.update(x=x,y=y)
    m.save_player(ctx,player)
    changes = ["走了一格。"]
    if m.tile_at(ctx, x, y) == "water" and not building.on_walkway(ctx, x, y):
        for eid in player["inventory"]:
            entity = m.get_entity(ctx, eid)
            if ("wettable" in entity["capabilities"] and not entity["state"]["destroyed"]
                    and not entity["state"].get("retired")):
                entity["state"]["wetness"] = min(100, entity["state"]["wetness"]+10)
                m.save_entity(ctx, entity)
        changes.append("涉过浅溪，随身可湿物件湿度增加10。")
    if previous_chunk!=m.chunk_for(x,y):
        _add_player_index(ctx,player,add=True)
        offer_generation(ctx,x,y,trigger="first_enter")
    for cid in m.chunk_ids_around(x,y,8):
        _,cx,cy=cid.split("_")
        m.ensure_chunk(ctx,int(cx),int(cy))
    return FunctionOutcome({"player":_public_self(player),"changes":changes,"viewport":viewport(ctx,{})},
        events=(StreamEvent("grove.changes","move",{"summary":f"{player['name']}走了一格。","x":x,"y":y},key="move"),))


def _accessible(ctx,player,entity,*,near=True):
    if entity["state"].get("retired"):
        raise RuleViolation("物件已经投入其他用途。")
    if entity["owner"] is not None and entity["owner"]!=player["id"]:
        raise PermissionDenied("别人的背包和物件不能取用。")
    current=entity
    seen=set()
    while current.get("container"):
        if current["id"] in seen:
            raise RuleViolation("容器关系循环。")
        seen.add(current["id"])
        parent=m.get_entity(ctx,current["container"])
        if not parent["state"]["open"]:
            raise RuleViolation("物件在关闭的容器内，请先打开。")
        current=parent
    x,y=m.entity_location(ctx,entity)
    if x is None or (near and entity["owner"] is None and abs(x-player["x"])+abs(y-player["y"])>1):
        raise RuleViolation("要先走到物件相邻的一格。")
    if not near and max(abs(x-player["x"]),abs(y-player["y"]))>8:
        raise RuleViolation("物件不在附近视野内。")


def available_actions(ctx,player,entity):
    if entity["state"]["destroyed"] or entity["state"].get("retired") or entity["owner"] not in {None,player["id"]}:
        return []
    x,y=m.entity_location(ctx,entity)
    if entity["owner"] is None and (x is None or abs(x-player["x"])+abs(y-player["y"])>1):
        return []
    caps=set(entity["capabilities"])
    actions=[]
    personal="personal" in caps
    burning_path=False
    ancestor=entity
    for _ in range(8):
        burning_path=burning_path or ancestor["state"]["burning"]
        if not ancestor.get("container"):
            break
        ancestor=m.get_entity(ctx,ancestor["container"])
        if not ancestor["state"]["open"]:
            return []
    else:
        return []
    if "transport" in caps and not personal and not entity["state"]["burning"] and (entity["owner"] is not None or entity["container"] is None):
        actions.append("pickup" if entity["owner"] is None else "drop")
    if "container" in caps:
        actions.append("close" if entity["state"]["open"] else "open")
        if entity["state"]["open"] and not burning_path:
            if len(entity["state"]["contents"])<entity["state"]["capacity"]:
                actions.append("store")
            if entity["state"]["contents"]:
                actions.append("retrieve")
    if (("water_source" in caps and entity["state"]["water_remaining"]>0)
        or ("container" in caps and entity["state"]["open"] and not burning_path
            and entity["owner"] is None and entity["state"]["water"]>0)):
        if entity["state"].get("recipe") != "canopy" or building._owned_available(ctx, player,
                lambda e: "container" in e["capabilities"] and "personal" not in e["capabilities"]
                and e["state"]["open"] and e["state"]["water"] < e["state"]["water_capacity"]):
            actions.append("fill")
    receives_water=("container" in caps and entity["state"]["water_capacity"]>0
                    and entity["state"]["open"] and not entity["state"]["burning"])
    if receives_water and not burning_path and entity["state"]["water"]<entity["state"]["water_capacity"]:
        actions.append("pour")
    elif not receives_water and "wettable" in caps and not personal:
        actions.append("pour")
    if building.is_structure(entity) and "pour" in actions and not building._water_items(ctx, player):
        actions.remove("pour")
    if ("flammable" in caps and m.is_ground(entity) and not personal and not entity["state"]["burning"]
        and entity["state"]["wetness"]<35 and entity["state"]["fuel"]>0 and not m.is_safe(ctx,x,y)
        and processes.can_ignite(ctx,entity)):
        actions.append("ignite")
    actions.extend(building.structure_actions(ctx,player,entity))
    return list(dict.fromkeys(actions))


def _action_options(ctx, player, entity):
    options = building.action_options(ctx, player, entity)
    if building.is_structure(entity) and entity["state"]["recipe"] == "canopy":
        actions = available_actions(ctx, player, entity)
        reason = building._path_reason(ctx, player, entity)
        if not reason and not entity["state"]["open"]:
            reason = "请先打开棚的储水口。"
        if not reason and entity["state"]["water"] <= 0:
            reason = "棚里没有储水，可切到集雨模式等待实际降雨。"
        receptacles = building._owned_available(ctx, player, lambda e: "container" in e["capabilities"]
            and "personal" not in e["capabilities"] and e["state"]["open"]
            and e["state"]["water"] < e["state"]["water_capacity"])
        if not reason and not receptacles:
            reason = "需要自己打开且未满的装水容器。"
        options.append(building._option("fill", reason, "从棚的同一储水余额装入自己的容器。", "按实际装入份数消耗棚中储水。"))
        for action in ("open", "close"):
            if action in actions:
                options.append(building._option(action, None,
                    "开合储水口；关上会暂停集雨与取水。"))
    if building.is_structure(entity):
        reason = building._path_reason(ctx, player, entity)
        # Water can suppress a burning wooden structure, so burning the target
        # itself does not prevent pouring on public ground.
        if entity["state"]["burning"] and m.is_ground(entity):
            x, y = m.entity_location(ctx, entity)
            reason = "要先走到目标相邻的一格。" if abs(x-player["x"])+abs(y-player["y"])>1 else None
        if not reason and not building._water_items(ctx, player):
            reason = "需要自己打开的容器里至少1份水。"
        if not reason and ("container" in entity["capabilities"] and entity["state"]["open"]
                and not entity["state"]["burning"] and entity["state"]["water"] >= entity["state"]["water_capacity"]):
            reason = "目标储水已满。"
        options.append(building._option("pour", reason,
            "把实际水加入棚的储水，或浇湿建筑灭火；每份水使湿度加8。", "按实际倒出份数消耗自己的水。"))
    return options


def inspect(ctx,args):
    player=m.get_player(ctx)
    entity=m.get_entity(ctx,args["entity_id"])
    _accessible(ctx,player,entity,near=False)
    contents=[]
    if "container" in entity["capabilities"] and entity["state"]["open"]:
        contents=[m.get_entity(ctx,eid) for eid in entity["state"]["contents"]]
    visible=_project_entity(ctx,player,entity)
    explanation=f"{entity['name']}：{entity['material']}，湿度{entity['state']['wetness']}，燃料{entity['state']['fuel']}。"
    if entity["state"].get("kind") == "resource_node":
        explanation=f"{entity['name']}：公共库存{entity['state']['stock']}/{entity['state']['stock_capacity']}；"+(
            "石材有限，不能浇水再生。" if entity["state"]["resource_kind"] == "stone" else "可消耗自己的5份水养护，6个物理秒后恢复1份。")
    elif building.is_structure(entity):
        explanation=" ".join(building.effect_projection(entity)["effects"])
    return FunctionOutcome({"entity":visible,"contents":[_project_entity(ctx,player,e) for e in contents],
        "available_actions":available_actions(ctx,player,entity),
        "action_options":_action_options(ctx,player,entity),"explanation":explanation})


def _target(ctx,args):
    eid=args.get("target_id") or args.get("item_id")
    if not eid:
        raise RuleViolation("此行动需要选择目标物件。")
    entity=m.get_entity(ctx,eid)
    if "expected_target_version" in args and args["expected_target_version"]!=entity["version"]:
        raise StateConflict("目标物件已经变化，请刷新后再行动。")
    return entity


def _own_item(ctx,player,args,capability=None):
    if not args.get("item_id"):
        raise RuleViolation("请从背包选择使用的物件。")
    item=m.get_entity(ctx,args["item_id"])
    _accessible(ctx,player,item)
    if item["owner"]!=player["id"] or item["state"]["destroyed"] or item["state"].get("retired") or "personal" in item["capabilities"]:
        raise RuleViolation("须使用自己持有的可用物件。")
    if capability and capability not in item["capabilities"]:
        raise RuleViolation("所选物件没有所需能力。")
    return item


def _container(ctx,player,entity):
    _accessible(ctx,player,entity)
    if "container" not in entity["capabilities"] or entity["state"]["destroyed"] or not entity["state"]["open"]:
        raise RuleViolation("目标必须是打开且未毁坏的容器。")
    ancestor=entity
    while True:
        if ancestor["state"]["burning"]:
            raise RuleViolation("燃烧中的容器及其内部容器不能存取，请先灭火。")
        if not ancestor.get("container"):
            break
        ancestor=m.get_entity(ctx,ancestor["container"])


def _store(ctx,player,item,target):
    _container(ctx,player,target)
    if item["id"]==target["id"] or target["id"] in {e["id"] for e in m.descendants(ctx,item)}:
        raise RuleViolation("容器不能装入自己或自己的内容。")
    # Containment depth stays finite and small for both interaction and projection.
    depth=1
    cursor=target
    while cursor.get("container"):
        depth+=1
        cursor=m.get_entity(ctx,cursor["container"])
    if depth>=4 or len(target["state"]["contents"])>=target["state"]["capacity"]:
        raise RuleViolation("容器容量不足或嵌套过深。")
    if item["state"]["burning"] or "personal" in item["capabilities"]:
        raise RuleViolation("燃烧或私人物件不能存入。")
    if item["container"]==target["id"]:
        raise RuleViolation("物件已在这个容器内。")
    m.remove_from_container(ctx,item)
    # Source removal can change the target (own backpack), so re-read it.
    target=m.get_entity(ctx,target["id"])
    target["state"]["contents"].append(item["id"])
    m.save_entity(ctx,target)
    m.relocate_tree(ctx,item,owner=target["owner"],container=target["id"])


def interact(ctx,args):
    player=m.get_player(ctx)
    m.check_player_version(player,args["expected_player_version"])
    action=args["action"]
    target=_target(ctx,args)
    _accessible(ctx,player,target)
    if target["state"]["destroyed"]:
        raise RuleViolation("毁坏的物件不能再互动。")
    item=None
    if action in {"harvest","tend","fuel","dismantle","switch_mode"} or (
            action == "ignite" and target["state"].get("recipe") == "hearth"):
        summary=building.interact_structure(ctx,player,args,target)
        target=m.get_entity(ctx,target["id"])
    elif action=="pickup":
        if "transport" not in target["capabilities"] or target["owner"] is not None or target.get("container"):
            raise RuleViolation("只能拿起公共地面可搬运物件；容器内物件请取出。")
        if target["state"]["burning"] or "personal" in target["capabilities"]:
            raise RuleViolation("燃烧或私人物件不能拿取。")
        backpack=m.get_entity(ctx,player["backpack_id"])
        _store(ctx,player,target,backpack)
        summary=f"拿起了{target['name']}。"
    elif action=="drop":
        item=_own_item(ctx,player,args,"transport")
        if item["state"]["burning"]:
            raise RuleViolation("燃烧物件不能搬运。")
        if "blocking" in item["capabilities"]:
            if m.is_safe(ctx,player["x"],player["y"]) or m.blocking_at(ctx,player["x"],player["y"]):
                raise RuleViolation("安全区或已有障碍的位置不能放障碍。")
            if len(m.public_players_near(ctx,player["x"],player["y"],radius=0))>1:
                raise RuleViolation("不能把障碍放在其他人的位置。")
        m.remove_from_container(ctx,item)
        m.relocate_tree(ctx,item,owner=None,x=player["x"],y=player["y"])
        target=item
        summary=f"放下了{item['name']}。"
    elif action in {"open","close"}:
        if "container" not in target["capabilities"]:
            raise RuleViolation("目标没有容器能力。")
        wanted=action=="open"
        if target["state"]["open"]==wanted:
            raise RuleViolation("容器已经处于这一状态。")
        target["state"]["open"]=wanted
        m.save_entity(ctx,target)
        summary=f"{'打开' if wanted else '关闭'}了{target['name']}。"
    elif action=="store":
        item=_own_item(ctx,player,args,"transport")
        _store(ctx,player,item,target)
        summary=f"把{item['name']}存入{target['name']}。"
    elif action=="retrieve":
        _container(ctx,player,target)
        if not args.get("item_id") or args["item_id"] not in target["state"]["contents"]:
            raise RuleViolation("所选物件不在目标容器内。")
        item=m.get_entity(ctx,args["item_id"])
        _accessible(ctx,player,item)
        if "transport" not in item["capabilities"] or item["state"]["burning"] or "personal" in item["capabilities"]:
            raise RuleViolation("不能取出燃烧或受保护物件。")
        backpack=m.get_entity(ctx,player["backpack_id"])
        if target["id"]==backpack["id"]:
            m.remove_from_container(ctx,item)
            m.relocate_tree(ctx,item,owner=player["id"])
        else:
            _store(ctx,player,item,backpack)
        summary=f"取出了{item['name']}。"
    elif action=="fill":
        item=_own_item(ctx,player,args,"container")
        _container(ctx,player,item)
        source_key="water_remaining"
        if "water_source" not in target["capabilities"]:
            _container(ctx,player,target)
            if target["owner"] is not None or target["state"]["water_capacity"]<=0:
                raise RuleViolation("目标必须是公共水源或打开的公共储水容器。")
            source_key="water"
        if target["id"]==item["id"]:
            raise RuleViolation("不能从同一容器给自己装水。")
        amount=min(args.get("amount",item["state"]["water_capacity"]),
                   item["state"]["water_capacity"]-item["state"]["water"],target["state"][source_key])
        if amount<=0:
            raise RuleViolation("容器已满或水源耗尽。")
        target["state"][source_key]-=amount
        item["state"]["water"]+=amount
        m.save_entity(ctx,target)
        m.save_entity(ctx,item)
        summary=f"向{item['name']}装入{amount}份水。"
    elif action=="pour":
        item=_own_item(ctx,player,args,"container")
        _container(ctx,player,item)
        if item["id"]==target["id"]:
            raise RuleViolation("不能向自己倒水。")
        amount=min(args.get("amount",5),item["state"]["water"])
        if amount<=0:
            raise RuleViolation("所选容器没有水。")
        if ("container" in target["capabilities"] and target["state"]["water_capacity"]>0
            and target["state"]["open"] and not target["state"]["burning"]):
            _container(ctx,player,target)
            amount=min(amount,target["state"]["water_capacity"]-target["state"]["water"])
            if amount<=0:
                raise RuleViolation("目标容器已满。")
            target["state"]["water"]+=amount
        elif "wettable" in target["capabilities"] and "personal" not in target["capabilities"]:
            target["state"]["wetness"]=min(100,target["state"]["wetness"]+amount*8)
            if target["state"]["wetness"]>=35:
                target["state"]["burning"]=False
                if target["state"].get("recipe") == "hearth":
                    target["state"]["lit"]=False
        else:
            raise RuleViolation("目标既不能装水也不能浇湿。")
        item["state"]["water"]-=amount
        m.save_entity(ctx,item)
        m.save_entity(ctx,target)
        summary=f"向{target['name']}倒了{amount}份水。"
    elif action=="ignite":
        item=_own_item(ctx,player,args,"ignition")
        state=target["state"]
        if (target["owner"] is not None or not m.is_ground(target) or "flammable" not in target["capabilities"]
            or "personal" in target["capabilities"] or state["wetness"]>=35 or state["fuel"]<=0 or state["burning"]
            or m.is_safe(ctx,target["x"],target["y"])):
            raise RuleViolation("只能点燃安全区外干燥、有燃料且未着火的公共地面物件。")
        if item["state"].get("uses",20)<=0:
            raise RuleViolation("点火工具已耗尽。")
        if not processes.can_ignite(ctx,target):
            raise RuleViolation("火或内容过程容量不足，或这一片没有容器烧毁后释放内容的地面空间。")
        item["state"]["uses"]=item["state"].get("uses",20)-1
        state["burning"]=True
        m.save_entity(ctx,item)
        m.save_entity(ctx,target)
        summary=f"点燃了{target['name']}。"
    else:
        raise RuleViolation("未知行动。")
    # Ownership helpers can update player inventory. Re-read before advancing the
    # player version, so no candidate write restores a stale inventory.
    player=m.get_player(ctx)
    m.save_player(ctx,player)
    x,y=m.entity_location(ctx,target)
    m.record_change(ctx,summary,x=x,y=y,entity_id=target["id"],action=action)
    return FunctionOutcome({"player":_public_self(player),"entity":_project_entity(ctx,player,m.get_entity(ctx,target["id"])),
        "changes":[summary],"viewport":viewport(ctx,{})},events=(StreamEvent("grove.changes","interact",
            {"summary":f"{player['name']}{summary}","x":x,"y":y,"entity_id":target["id"],"action":action},key="action"),))


def _project_entity(ctx,player,entity):
    value={k:v for k,v in entity.items()}
    value["state"]=dict(entity["state"])
    if entity["owner"] is None and not entity["state"]["open"]:
        value["state"]["contents"]=[]
    value["contents"]=list(value["state"]["contents"])
    value["available_actions"]=available_actions(ctx,player,entity)
    value["action_options"]=_action_options(ctx,player,entity)
    value["construction_material"]=building.material_status(ctx,player,entity)
    if building.is_structure(entity):
        value["construction"]=building.effect_projection(entity)
    return value


def build_function(ctx,args):
    player=m.get_player(ctx)
    m.check_player_version(player,args["expected_player_version"])
    entity,summary=building.build(ctx,player,args)
    player=m.get_player(ctx)
    m.save_player(ctx,player)
    m.record_change(ctx,summary,x=entity["x"],y=entity["y"],entity_id=entity["id"],action="build")
    return FunctionOutcome({"player":_public_self(player),"entity":_project_entity(ctx,player,entity),
        "changes":[summary],"viewport":viewport(ctx,{})},events=(StreamEvent("grove.changes","build",
        {"summary":f"{player['name']}{summary}","x":entity["x"],"y":entity["y"],
         "entity_id":entity["id"],"action":"build","recipe":args["recipe"]},key="build"),))


def viewport(ctx,args):
    from .generation import generation_opportunities
    player=m.get_player(ctx)
    x,y=args.get("cx",player["x"]),args.get("cy",player["y"])
    radius=args.get("radius",8)
    if max(abs(x-player["x"]),abs(y-player["y"]))>8:
        raise RuleViolation("只能查看自己附近的地块。")
    chunks=[]
    for cid in m.chunk_ids_around(x,y,radius):
        _,cx,cy=cid.split("_")
        chunk=m.get_chunk(ctx,int(cx),int(cy))
        chunks.append({k:chunk[k] for k in ("id","cx","cy","seed","generator_version","tiles","version")})
    raw_entities=m.public_entities_near(ctx,x,y,radius,limit=160)
    own=[]
    for eid in player["inventory"]:
        entity=m.get_entity(ctx,eid)
        if entity["owner"]!=player["id"]:
            raise RuleViolation("背包归属索引无效。")
        own.append(_project_entity(ctx,player,entity))
    changes=ctx.get_state(m.META,"changes",[])
    changes=[c for c in changes if c["x"] is None or max(abs(c["x"]-x),abs(c["y"]-y))<=radius][-12:]
    body={"player":_public_self(player),"chunks":chunks,
          "entities":[_project_entity(ctx,player,e) for e in raw_entities],
          "players":m.public_players_near(ctx,x,y,radius),"inventory":own,
          "processes":processes.visible_processes(ctx,x,y,radius),
          "opportunities":generation_opportunities(ctx,{**player,"x":x,"y":y},radius),
          "changes":changes,"bounds":{"min_x":x-radius,"min_y":y-radius,"max_x":x+radius,"max_y":y+radius},
          "truncated":len(raw_entities)>=160,"clock":ctx.get_state(m.META,"clock"),
          "construction":{"recipes":building.recipes(),"nearby":building.nearby_effects(ctx,x,y,radius),
                          "radius_metric":"chebyshev","limits":{"hearths":building.MAX_HEARTHS,"growth":16}}}
    # Keep both query and SDK projection comfortably below the96KiB view limit.
    while len(json.dumps(body,ensure_ascii=False,separators=(",",":")).encode())>90000:
        if body["entities"]:
            body["entities"].pop()
            body["truncated"]=True
        elif body["changes"]:
            body["changes"].pop(0)
        elif body["construction"]["nearby"]:
            body["construction"]["nearby"].pop()
            body["truncated"]=True
        else:
            raise RuleViolation("附近投影过大，请缩小视野半径。")
    return body


def viewport_function(ctx,args):
    return FunctionOutcome(viewport(ctx,args))


def map_project(ctx,args):
    body=viewport(ctx,args)
    entities={e["id"]:e for e in body.pop("entities")}
    return {"entities":entities,"meta":body}


def make_world():
    from .generation import generation_specs, generation_state_specs
    return WorldDefinition("shared-grove","溪源",(
        FunctionSpec("grove.join",join,JOIN_ARGS,description="以当前身份进入或接续溪源。"),
        FunctionSpec("grove.move",move,MOVE_ARGS,authorize=member,description="由人向四个方向之一走一格。"),
        FunctionSpec("grove.inspect",inspect,INSPECT_ARGS,access="read",authorize=member,description="检查附近物件、状态与实际可用行动。"),
        FunctionSpec("grove.interact",interact,INTERACT_ARGS,authorize=member,description="由人操作真实物件，按材质能力与归属结算。"),
        FunctionSpec("grove.build",build_function,BUILD_ARGS,authorize=member,description="由人用本人原材料搭通路、雨棚或烘干炉，原子投入唯一物件。"),
        FunctionSpec("grove.handbook",handbook,EMPTY,access="read",description="读取正式世界规则，未入场亦可读取。"),
        FunctionSpec("map.viewport",viewport_function,VIEW_ARGS,access="read",authorize=member,description="读取附近有限地图与自己的背包。"),
        *generation_specs()),version=4,state_version=3,
        state_rules=(StateRule(m.PLAYERS,"",PLAYER_SCHEMA),StateRule(m.ENTITIES,"",ENTITY_SCHEMA),
            StateRule(m.CHUNKS,"",CHUNK_SCHEMA),StateRule(m.PROCESSES,"",PROCESS_SCHEMA,history="metadata"),
            StateRule(m.META,"",{},history="metadata"),*generation_state_specs()),
        state_authorizer=_state_authorizer,initialize=initialize,bootstrap=bootstrap,
        migrations={2:migrate_batched_processes,3:migrate_batched_processes},
        timers=(TimerSpec("grove.tick",processes.tick,TICK_ARGS,max_attempts=3,retry_delay_seconds=1),),
        views=(ViewSpec("grove.map",map_project,VIEW_ARGS,authorize=member,renderer="shared-grove",streams=("grove.changes",)),),
        streams=(StreamSpec("grove.changes",public=True,retention_seconds=86400,max_events=4096),),
        entry_instructions="先用grove.handbook读取规则。人连接自己的外部Agent后入场，四向移动与物件行动由人控制；Agent读取事实、按公共机会创作与解释。")
