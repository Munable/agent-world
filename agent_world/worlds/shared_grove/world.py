"""The formal Shared Grove world: people act; ordinary rules adjudicate."""
from __future__ import annotations

import hashlib
import json
from copy import deepcopy

from agent_world import FunctionOutcome, FunctionSpec, StateRule, TimerSpec, ViewSpec, WorldDefinition
from agent_world.errors import PermissionDenied, RuleViolation, StateConflict
from agent_world.world_streams import StreamEvent, StreamSpec
from agent_world.world_timers import TimerNotFound

from . import model as m
from . import processes
from . import building
from .definitions import COMBUSTION_CADENCE_TICKS, COMPONENT_FIELDS, CONSTRUCTIONS, REGISTRY, RULES_VERSION, registry_for

EMPTY = {"type": "object", "properties": {}, "additionalProperties": False}
ID = {"type": "string", "minLength": 1, "maxLength": 128}
LOT_ID = {"type": "string", "minLength": 1, "maxLength": 180}
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
INSPECT_ARGS = {"type": "object", "properties": {"entity_id": ID,
    "action": {"type": "string", "minLength": 1, "maxLength": 80,
               "description": "Optional actual declared action name. Expand only this action's complete conditions, candidates and previews; omit to expand all actions."},
    "candidate_id": {**ID, "description": "Optional actual input.candidates or choices.items/others ID, only with action. Expand only this complete candidate; other candidates' details and versions are not provided."}},
    "required": ["entity_id"], "additionalProperties": False}
BUILD_ARGS = {"type": "object", "properties": {
    "recipe": {"enum": list(building.RECIPES)}, "x": COORD, "y": COORD,
    "expected_player_version": VERSION,
    "material_ids": {"type": "array", "minItems": 1, "maxItems": 8, "uniqueItems": True, "items": ID}},
    "required": ["recipe", "x", "y", "expected_player_version"], "additionalProperties": False}
INTERACT_ARGS = {"type": "object", "properties": {
    "action": {"enum": ["pickup", "drop", "open", "close", "store", "retrieve", "fill", "pour", "ignite",
                         "harvest", "tend", "fuel", "dismantle", "switch_mode", "attach", "detach", "launch", "transfer"]},
    "target_id": ID, "item_id": ID, "expected_player_version": VERSION,
    "expected_target_version": VERSION, "expected_item_version": VERSION,
    "expected_combination_fingerprint": {"type": "string", "minLength": 64, "maxLength": 64, "pattern": "^[0-9a-f]{64}$"},
    "amount": {"type": "integer", "minimum": 1, "maximum": 10000}, "lot_id": LOT_ID,
    "target_slot": {"enum": ["contained", "surface"]},
    "mode": {"enum": ["shelter", "collect"]}},
    "required": ["action", "expected_player_version"], "additionalProperties": False}
VIEW_ARGS = {"type": "object", "properties": {"cx": COORD, "cy": COORD,
    "radius": {"type": "integer", "minimum": 1, "maximum": 8}, "inventory_after": ID,
    "inventory_limit": {"type": "integer", "minimum": 1, "maximum": 16}}, "additionalProperties": False}
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
    "matter": {"type": "array", "maxItems": 8, "items": {"type": "object", "properties": {
        "id": LOT_ID, "material": {"enum": list(REGISTRY.materials)},
        "quantity": {"type": "integer", "minimum": 0, "maximum": 10000},
        "slot": {"enum": ["body", "contained", "surface", "absorbed"]}, "bound_to": LOT_ID,
        "role": {"enum": ["structural", "lining", "substance"]},
        "burning": {"type": "boolean"}}, "required": ["id", "material", "quantity", "slot", "role", "burning"],
        "additionalProperties": True}},
    "components": {"type": "object", "properties": {name: {"type": "object", "properties": {
        field: ({"type": "boolean"} if kind is bool else {"type": "string", "maxLength": 80} if kind is str
                else {"type": "object", "maxProperties": 8} if kind is dict
                else {"type": "integer", "minimum": minimum, "maximum": maximum})
        for field, (kind, minimum, maximum) in fields.items()}, "additionalProperties": False}
        for name, fields in COMPONENT_FIELDS.items()}, "maxProperties": 16, "additionalProperties": False},
    "composition": {"type": "object", "properties": {"material": {"enum": list(m.MATERIALS)},
        "construction": {"enum": list(CONSTRUCTIONS)}, "size": {"type": "integer", "minimum": 1, "maximum": 2},
        "lining": {"enum": ["cloth", None]}}, "required": ["material", "construction", "size"], "additionalProperties": False},
    "physical_parent": {"oneOf": [{"type": "null"}, {"type": "object", "properties": {
        "id": ID, "relation": {"enum": ["contained", "attached"]}}, "required": ["id", "relation"],
        "additionalProperties": False}]}, "lifecycle": {"enum": ["intact", "broken", "consumed"]},
    "state": {"type": "object", "properties": {
        "wetness": {"type": "integer", "minimum": 0, "maximum": 100},
        "fuel": {"type": "integer", "minimum": 0},
        "burning": {"type": "boolean"}, "destroyed": {"type": "boolean"}, "open": {"type": "boolean"},
        "capacity": {"type": "integer", "minimum": 0, "maximum": 32},
        "contents": {"type": "array", "maxItems": 32, "uniqueItems": True, "items": ID},
        "water": {"type": "integer", "minimum": 0, "maximum": 200},
        "water_capacity": {"type": "integer", "minimum": 0, "maximum": 200},
        "water_remaining": {"type": "integer", "minimum": 0, "maximum": 1000},
        "water_source_capacity": {"type": "integer", "minimum": 0, "maximum": 1000},
        "retired": {"type": "boolean"}, "retired_into": NULL_ID,
        "kind": {"type": "string", "maxLength": 40}, "resource_kind": {"enum": list(m.MATERIALS)},
        "stock": {"type": "integer", "minimum": 0, "maximum": 4}, "stock_capacity": {"const": 4},
        "regrowth_tick": {"type": ["integer", "null"], "minimum": 0},
        "recipe": {"enum": list(building.RECIPES)}, "mode": {"enum": ["shelter", "collect"]},
        "hearth_fuel": {"type": "integer", "minimum": 0},
        "lit": {"type": "boolean"}, "dismantled": {"type": "boolean"},
        "invested_materials": {"type": "array", "maxItems": 8, "items": {"type": "object", "properties": {
            "id": ID, "material": {"enum": list(m.MATERIALS)},
            "quantity": {"type": "integer", "minimum": 1},
            "lot_ids": {"type": "array", "maxItems": 8, "uniqueItems": True, "items": LOT_ID}},
            "required": ["id", "material", "quantity", "lot_ids"], "additionalProperties": False}}},
        "required": ["wetness", "fuel", "burning", "destroyed", "open", "capacity", "contents",
                     "water", "water_capacity", "water_remaining"], "additionalProperties": True}},
    "required": ["id", "name", "x", "y", "material", "capabilities", "owner", "container",
                 "state", "created_by", "created_at", "source", "authored_description", "matter", "components",
                 "physical_parent", "lifecycle"], "additionalProperties": False}
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
    "water_reservoirs": {"type": "object", "maxProperties": 256,
                         "additionalProperties": {"type": "integer", "minimum": 0, "maximum": 10000}},
    "safe_zones": {"type": "array", "maxItems": 48, "items": {"type": "object", "properties": {
        "owner": ID, "x": COORD, "y": COORD, "radius": {"const": 1}}, "required": ["owner", "x", "y", "radius"], "additionalProperties": False}}},
    "required": ["id", "cx", "cy", "seed", "generator_version", "tiles", "entity_ids", "player_ids", "safe_zones"], "additionalProperties": False}
PROCESS_SCHEMA = {"type": "object", "properties": {
    "id": ID, "kind": {"enum": ["rain", "drift", "growth"]},
    "status": {"enum": ["active", "complete"]}, "entity_id": NULL_ID,
    "chunk_ids": {"type": "array", "maxItems": 4, "uniqueItems": True, "items": ID},
    "remaining_ticks": {"type": "integer", "minimum": 0, "maximum": 32}, "start_tick": {"type": "integer", "minimum": 0},
    "created_at": {"type": "number"}, "created_by": NULL_ID,
    "rules_version": {"const": RULES_VERSION}, "rule_version": {"const": 1},
    "reserved_ids": {"type": "array", "maxItems": processes.MAX_WET_ENTITIES, "uniqueItems": True, "items": ID},
    "intensity": {"type": "integer", "minimum": 1, "maximum": 32},
    "water_remaining": {"type": "integer", "minimum": 0, "maximum": 32*32*processes.MAX_WET_ENTITIES},
    "route": {"type": "array", "minItems": 2, "maxItems": 9, "items": {
        "type": "object", "properties": {"x": COORD, "y": COORD}, "required": ["x", "y"], "additionalProperties": False}},
    "route_index": {"type": "integer", "minimum": 0, "maximum": 8}, "paused": {"type": "boolean"},
    "end_tick": {"type": "integer", "minimum": 0},
    "moisture_chunks": {"type": "array", "maxItems": 4, "uniqueItems": True, "items": ID}},
    "required": ["id", "kind", "status", "entity_id", "chunk_ids", "remaining_ticks", "start_tick", "end_tick",
                 "created_at", "created_by", "rules_version", "rule_version", "reserved_ids"],
    "additionalProperties": False}


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


def resume_clock(ctx):
    """Explicitly hand the saved clock to this reviewed world revision.

    Runtime keeps old-version timers blocked. Only the current clock task is
    replaced; state, logical sequence and contract deadlines do not advance.
    """
    clock = ctx.get_state(m.META, "clock")
    old_id = f"grove.tick.{clock['epoch']}.{clock['sequence']}"
    try:
        ctx.get_timer(old_id)
    except TimerNotFound:
        pass
    else:
        ctx.cancel_timer(old_id)
    clock["epoch"] = m.identifier("clock")
    ctx.set_state(m.META, "clock", clock)
    ctx.schedule_timer(f"grove.tick.{clock['epoch']}.{clock['sequence']}", "grove.tick",
        {"epoch": clock["epoch"], "sequence": clock["sequence"]}, due_at=ctx.now+1)


def handbook(ctx, args):
    from .generation import handbook as generation_handbook
    from .definitions import composition_catalog, PHASES, RULES_VERSION
    creation = generation_handbook(ctx, {}).result["creation"]
    from .creative import rule_catalog
    creation["programs"] = rule_catalog(ctx, {}).result
    return FunctionOutcome({
        "world": "shared-grove", "title": "溪源", "player": _public_self(m.get_player(ctx, required=False)),
        "clock": ctx.get_state(m.META, "clock"), "rules_version": RULES_VERSION, "process_order": list(PHASES),
        "combustion": {"cadence_ticks": COMBUSTION_CADENCE_TICKS,
                       "schedule": "clock.sequence multiples; quantity is consumed once per burning lot at its combustion.rate",
                       "rate_unit": "u_per_burning_lot_per_settlement",
                       "spread_cadence_ticks": COMBUSTION_CADENCE_TICKS,
                       "quenching_cadence_ticks": 1,
                       "observation": "entity.simulation.combustion in grove.inspect"},
        "process_limits": {"active_entities": processes.MAX_WET_ENTITIES, "burning_entities": processes.MAX_BURNING_ENTITIES,
                           "burning_contents": processes.MAX_BURNING_CONTENTS, "events": processes.MAX_EVENTS,
                           "active_processes": processes.MAX_PROCESSES, "hearths": building.MAX_HEARTHS, "growth": processes.MAX_GROWTH,
                           "lots_per_entity": 8, "physical_descendants": 16, "physical_depth": 8,
                           "frame_chunks": processes.MAX_FRAME_CHUNKS, "entity_bytes": 8192, "chunk_bytes": m.MAX_CHUNK_BYTES,
                           "process_bytes": processes.MAX_PROCESS_BYTES},
        "terrain": {"grass": "草地", "dirt": "泥地", "water": "可涉浅溪；实际接触水由当地有限溪水转入随身吸水材料，无水时不会凭空变湿。通路完整时免受这次水接触。", "rock": "不能通行的岩壁"},
        "materials": composition_catalog()["materials"], "compositions": composition_catalog(),
        "material_slots": {"body": "本体真实材料", "contained": "容器内可装卸的物质", "surface": "外表面接触物质", "absorbed": "实际已吸收水"},
        "material_roles": {"structural": "支撑本体，烧耗会影响结构", "lining": "衬层，耗尽不把石壳变成燃料", "substance": "装入或接触的物质"},
        "material_operations": {
            "store": "搬入完整子物件，保留其身份与自身材料；子物件的本体不变成目标的contained配料。",
            "transfer": "按来源item_id、lot_id和amount转移已有材料批次到目标target_slot；材料身份不变，来源余量保留。本人个人背包中已有的自身contained散料可转出；不取背包本体、衬层或完整子物件，不能取他人的保护内容。个人背包不接收散料。",
            "charge": "grove.rule_action只消耗定义的feedstock并等量制备包内声明材料symbol，不是转移已有批次，也不证明反应发生。"},
        "combination_preview": {"actions": ["store", "attach"], "location": "action_options[].input.candidates[].preview",
            "availability": "available/reason 表示当前行动资格，与 physics.known/overloaded 分开。",
            "physics": "before/after 为目标载荷汇总；affected 为可见或本人的受影响承载者，含其当前版本。",
            "support_limit": "support_limit_applicable=false 时 support_margin=null，表示未设置结构承载上限。",
            "submit_guard": "store/attach 可携带 expected_combination_fingerprint；组合版本变化时原子拒绝并要求重新阅读预览。",
            "assumption": "relation_change_only", "current_only": True,
            "meaning": "只假设这次关系改变，不推进时间、不扣料、不写世界；过载提示不禁止有意冒险，行动仍按当前版本结算。"},
        "capabilities": {"transport": "搬运完整物件", "container": "开关与有限容纳", "blocking": "有效时阻挡通行",
                         "water_source": "有限真实取水源", "ignition": "点燃实际可燃批次", "personal": "背包保护边界，不能公开丢弃",
                         "resource_node": "有限原材料库存", "attachment": "兼容部件的唯一附着关系", "buoyant": "按实际总载荷决定浮力"},
        "rules": ["人可亲自移动与行动，或明确一个目标委托自己的外部Agent用同一角色实际读取、计划、行动、读回与调整；亲手行动或收回会停止后续委托，已完成事实保留。",
                  "行动需当前人物版本；目标和使用物件可带当前版本，过期拒绝且无半次结果。倒水若明确amount和target_slot，则固定来源、目标、数量和作用槽，在提交时重新校验当前权限、距离、存在性、水量和容量；正常物理变化不单独阻塞这个仍合法的意图。作用槽改变时拒绝，不自动改量或改灌入位置。物件投影pour_target_slot保存该次读取的接触位置；连接器可从指定原观察绑定省略的target_slot，不能从新状态换槽。",
                  "公共地面可由附近任何人取用和修改，作者不是永久产权；别人的背包及其保护内容不能取用。放下即公开。",
                  "同一物件只有一个物理父关系，容纳和附着不能循环，关闭容器内不能装卸。",
                  "数量u是公开逻辑单位，不是SI重量；木、石、布、水真实批次都有当前余量，移动或装卸不复制。",
                  "装入水、燃料和物件都会增加载荷；超载、受湿及燃烧可能损伤结构。破损释放原物件身份，背包保护内容不凭空公开。",
                  "水只能从声明的有限来源转移；雨、接触、装水、倒水和养护均记录真实水或明确消耗。水不会因为一句描述出现。",
                  "点火消费工具次数，燃烧消费真实木布批次；石壳内有木布也可点燃，衬层燃尽不销毁石壳。",
                  "普通物件燃烧耗材与相邻蔓延在世界逻辑步每3步共同结算；每个燃烧批次按物件combustion.rate耗材，不补累计3倍。读取inspect的燃料余量、rate、cadence_ticks和ticks_until_next_consumption判断行动窗口。点火后遇最近的共同结算点就耗材，并非每次点火后保证3步。",
                  "倒水即时接触、每步水淬灭及过载损伤保持原节奏；只浇灭一个物件，邻近火源仍可能在下一蔓延结算点使已经干燥且暴露的可燃批次复燃。",
                  "外表浇水不穿透关闭容器；蒸发和燃烧进入明确环境终点，已耗物质不能在拆除时复活。",
                  "结构、材料损伤、失效、泄出及运输按同一确定过程顺序裁决，来源和物件名字不改变机制。",
                  "配方按真实材料u投入，部分用料的余量保留；拆除只取回仍存在的投资批次。",
                  "木布节点可消费本人5u水养护，6个物理逻辑步后产出一份原料；石材有限，浇水不造石。",
                  "下水后的浮力按组合及内容总负担计算，超载、吸水或失去浮材可改变漂流结果。",
                  "公共内容保存各作者贡献及真实后果；接续须读取当前锚点与版本，不复制私聊作为公共事实。",
                  "过程有公开有限容量，超过则拒绝或停止，事务、Receipt和原Operation恢复继续适用。",
                  "Agent可自动发布有界声明规则；服务器校验不可变定义与实际资源，纯文字不赋予能力。"],
        "creation": creation,
        "construction": {"recipes": building.recipes(), "radius_metric": "chebyshev",
                         "limits": {"hearths": building.MAX_HEARTHS, "growth": processes.MAX_GROWTH},
                         "build_schema": BUILD_ARGS, "interact_schema": INTERACT_ARGS},
        "entry": "本机伙伴实际回应并读世界后，由人进入；入场后点击现场读当前资源与动作。"})


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
        "capabilities":["container","wettable","personal"],"state":{},
        "components":{"container":{"slots":16,"liquid_capacity":0,"open":True},
                      "absorbent":{"capacity":32,"rate":2},"personal":{"protected":True}},
        "owner":player["id"],"source":"personal"})
    player["inventory"].append(backpack["id"])
    starters = [("水壶","stone",["transport","container","wettable"],{}),
                ("打火石","stone",["transport","ignition"],{"uses":20}),
                ("木柴","wood",["transport","flammable","wettable"],{}),
                ("干布","cloth",["transport","flammable","wettable"],{})]
    for item_name,material,caps,state in starters:
        value={"name":item_name,"material":material,"capabilities":caps,"state":state,
               "owner":player["id"],"container":backpack_id,"source":"personal"}
        if "container" in caps:
            value["components"]={"container":{"slots":2,"liquid_capacity":30,"open":True},
                                 "structure":{"integrity":100,"max_integrity":100,"support_limit":120,"overload_rate":1},
                                 "absorbent":{"capacity":0,"rate":2}}
            value["matter"]=[{"id":m.identifier("lot"),"material":"stone","quantity":40,"slot":"body","role":"structural","burning":False},
                             {"id":m.identifier("lot"),"material":"water","quantity":20,"slot":"contained","role":"substance","burning":False}]
        item = m.spawn_entity(ctx,value)
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
    from .creative import check_delegation, public_event
    check_delegation(ctx, args)
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
        contacted = 0
        for eid in player["inventory"]:
            entity = m.get_entity(ctx, eid)
            try:
                _accessible(ctx, player, entity)
            except (PermissionDenied, RuleViolation):
                continue
            if "absorbent" in entity.get("components", {}) and entity.get("lifecycle") != "consumed":
                contacted += m.contact_water(ctx, entity, 2, reason="wading")["transferred"]
        changes.append(f"涉过浅溪，从当地有限水源实际接触了{contacted}u水。")
    if previous_chunk!=m.chunk_for(x,y):
        _add_player_index(ctx,player,add=True)
        offer_generation(ctx,x,y,trigger="first_enter")
    for cid in m.chunk_ids_around(x,y,8):
        _,cx,cy=cid.split("_")
        m.ensure_chunk(ctx,int(cx),int(cy))
    public_event(ctx, action="move", x=x, y=y, kind="movement")
    return FunctionOutcome({"player":_public_self(player),"changes":changes,"viewport":viewport(ctx,{})},
        events=(StreamEvent("grove.changes","move",{"summary":f"{player['name']}走了一格。","x":x,"y":y},key="move"),))


def _accessible(ctx, player, entity, *, near=True, subject="target"):
    if entity["state"].get("retired") or entity.get("lifecycle") == "consumed":
        raise RuleViolation("物件已耗尽或投入其他用途。")
    current, seen = entity, set()
    for _ in range(17):
        if current["id"] in seen:
            raise RuleViolation("物理关系不能循环。")
        seen.add(current["id"])
        if current["owner"] not in {None, player["id"]}:
            raise PermissionDenied("别人的背包和物件不能取用。")
        parent = current.get("physical_parent")
        if not parent:
            break
        current = m.get_entity(ctx, parent["id"])
        if parent["relation"] == "contained" and not current["state"]["open"]:
            raise RuleViolation("物件在关闭的容器内，请先打开。")
    else:
        raise RuleViolation("物理关系层数超过限制。")
    x, y = m.entity_location(ctx, entity)
    if x is None or (near and current["owner"] is None and abs(x-player["x"])+abs(y-player["y"]) > 1):
        visible = x is not None and max(abs(x-player["x"]), abs(y-player["y"])) <= 8
        condition = {"subject": subject, "entity_id": entity["id"], "condition": "adjacent",
                     "known": visible, "satisfied": False, "required_max": 1, "metric": "manhattan"}
        if visible:
            condition.update(location={"x": x, "y": y}, player_location={"x": player["x"], "y": player["y"]},
                             actual_distance=abs(x-player["x"])+abs(y-player["y"]))
        else:
            condition["reason"] = "location_not_observable"
        label = {"target": "目标物件", "item": "来源物件", "other": "另一物件"}.get(subject, "物件")
        raise RuleViolation("要先走到" + label + "相邻的一格。", details={"action_precondition": condition})
    if not near and max(abs(x-player["x"]), abs(y-player["y"])) > 8:
        raise RuleViolation("物件不在附近视野内。")


def _access_observation(ctx, player, entity, *, subject="target"):
    """Public action distance after ownership, ancestry and visibility checks."""
    try:
        _accessible(ctx, player, entity, near=False, subject=subject)
    except (PermissionDenied, RuleViolation):
        return {"subject": subject, "entity_id": entity["id"], "condition": "accessible",
                "known": False, "satisfied": False}
    x, y = m.entity_location(ctx, entity)
    distance = abs(x-player["x"])+abs(y-player["y"])
    return {"subject": subject, "entity_id": entity["id"], "condition": "adjacent",
            "known": True, "location": {"x": x, "y": y}, "player_location": {"x": player["x"], "y": player["y"]},
            "actual_distance": distance, "required_max": 1, "metric": "manhattan",
            "satisfied": entity["owner"] == player["id"] or distance <= 1}


def _reason(ctx, player, entity, *, burning=False):
    try:
        _accessible(ctx, player, entity)
    except (PermissionDenied, RuleViolation) as exc:
        return str(exc)
    if not burning:
        cursor = entity
        for _ in range(17):
            if cursor["state"]["burning"]:
                return "燃烧中的物件及内容不能搬运或取用。"
            parent = cursor.get("physical_parent")
            if not parent:
                break
            cursor = m.get_entity(ctx, parent["id"])
    return None


def _candidate(entity, lots=None):
    body = [(lot["material"], lot["quantity"]) for lot in entity.get("matter", []) if lot["slot"] == "body"]
    hidden = entity["owner"] is None and not entity["state"]["open"] and "container" in entity.get("components", {})
    water = "未公开" if hidden else str(entity["state"]["water"])+"u"
    value = {"id": entity["id"], "name": entity["name"], "version": entity["version"],
             "selection_kind": "material_lot" if lots is not None else "whole_entity",
             "summary": "本体" + "、".join(f"{material}{quantity}u" for material, quantity in body) +
                        "，自身实际水"+water}
    if lots is not None:
        value["lots"] = [{"id": lot["id"], "material": lot["material"], "quantity": lot["quantity"],
                          "slot": lot["slot"], "item_id": entity["id"]} for lot in lots]
    return value


def _combination_fingerprint(player, relation_fingerprint):
    return hashlib.sha256((relation_fingerprint+":"+player["id"]+":"+str(player["version"])).encode()).hexdigest()


def _combination_candidate(ctx, player, target, item, action, reason):
    """Expose a read-only relation preview separately from action eligibility."""
    candidate = _candidate(item)
    relation = "contained" if action == "store" else "attached"
    result = m.preview_relation(ctx, target, item, relation)
    physics = result["physics"]
    visible = []
    for affected in physics["affected"]:
        try:
            _accessible(ctx, player, m.get_entity(ctx, affected["id"]), near=False)
        except (PermissionDenied, RuleViolation):
            continue
        visible.append(affected)
    # These rows describe only subjects and their visible/owned ancestors. The
    # closed-container projection already permits aggregate load, never child
    # identities or material lots; no preview adds either kind of disclosure.
    physics["affected"] = visible
    reason = reason or result["constraint_reason"]
    candidate["preview"] = {"action": action, "target_id": target["id"], "item_id": item["id"],
        "target_version": target["version"], "item_version": item["version"], "player_version": player["version"],
        "available": reason is None, "reason": reason, "physics": physics,
        "assumption": "relation_change_only", "current_only": True,
        "version_fingerprint": _combination_fingerprint(player, result["version_fingerprint"])}
    return candidate


def _input(candidates=None, *, quantity=None, choices=None, lots=False):
    value = {"kind": "entity_quantity" if candidates is not None and quantity else "entity" if candidates is not None
             else "quantity" if quantity else "choice", "argument": "item_id"}
    if candidates is not None:
        value["candidates"] = candidates
    if quantity:
        value["quantity"] = {"min": 1, "max": max(1, quantity), "default": min(5, max(1, quantity)),
                             "label": "实际数量 u", "argument": "amount"}
    if choices is not None:
        value["choices"], value["choice_argument"] = choices, "mode"
    if lots:
        value["lot_argument"] = "lot_id"
    return value


def _movable(ctx, player, entity):
    return ("transport" in entity["capabilities"] and "personal" not in entity["capabilities"]
            and not _reason(ctx, player, entity) and entity.get("lifecycle") != "consumed"
            and (not entity.get("physical_parent") or entity["physical_parent"]["relation"] != "attached"))


def _transfer_lots(ctx, player, entity):
    from .creative import guard_ordinary_action
    personal = "personal" in entity["capabilities"]
    if _reason(ctx, player, entity) or personal and entity["owner"] != player["id"]:
        return []
    try:
        guard_ordinary_action(ctx, "transfer", entity)
    except PermissionDenied:
        return []
    raw = not personal and building.material_status(ctx, player, entity)["eligible"]
    return [lot for lot in entity.get("matter", []) if lot["quantity"] > 0 and not lot.get("bound_to")
            and not lot.get("burning") and (not personal or lot.get("role") != "lining")
            and (lot["slot"] == "contained" and entity["state"]["open"] and "container" in entity.get("components", {})
            or lot["slot"] == "body" and raw)]


def _attach_reason(ctx, part, target):
    try:
        allowed = m.can_attach(ctx, part, target)
    except RuleViolation as exc:
        return str(exc)
    if isinstance(allowed, dict):
        return None if allowed.get("allowed", allowed.get("available", False)) else allowed.get("reason", "此部件不能附着到目标。")
    return None if allowed is True or allowed is None else "此部件不能附着到目标。"


def _launch_route(ctx, entity):
    """Plan at most eight adjacent steps from the actual public water tile."""
    if not m.eligible_drift(ctx, entity):
        return None
    route = [{"x": entity["x"], "y": entity["y"]}]
    visited = {(entity["x"], entity["y"])}
    for _ in range(8):
        point = route[-1]
        for dx, dy in ((0, 1), (1, 0), (-1, 0), (0, -1)):
            x, y = point["x"]+dx, point["y"]+dy
            if (x, y) in visited or max(abs(x), abs(y)) > m.WORLD_LIMIT or m.tile_at(ctx, x, y) == "rock":
                continue
            if (m.is_safe(ctx, x, y) or m.public_players_near(ctx, x, y, radius=0)
                    or m.public_entities_near(ctx, x, y, radius=0, limit=m.MAX_CHUNK_ENTITIES)):
                continue
            route.append({"x": x, "y": y})
            visited.add((x, y))
            break
        else:
            break
        if m.tile_at(ctx, route[-1]["x"], route[-1]["y"]) != "water":
            break
    return route if len(route) >= 2 else None


def _pour_target_slot(entity):
    state = entity["state"]
    return "contained" if "container" in entity["components"] and state["open"] and not state["burning"] and state["water_capacity"] > 0 else "surface"


def _action_options(ctx, player, entity):
    caps, components = set(entity["capabilities"]), entity.get("components", {})
    state, personal = entity["state"], "personal" in caps
    base = _reason(ctx, player, entity)
    own = building._owned_available(ctx, player, lambda e: e["owner"] == player["id"] and "personal" not in e["capabilities"])
    options = building.action_options(ctx, player, entity)
    labels = {"pickup": "拿起", "drop": "放下并公开", "open": "打开", "close": "关闭", "store": "装入物件", "retrieve": "取出物件",
              "fill": "装水", "pour": "倒水", "ignite": "点燃实际燃料", "harvest": "采集", "tend": "用水养护", "fuel": "装入木料",
              "dismantle": "拆回剩余材料", "switch_mode": "切换用途", "attach": "附着部件", "detach": "解除附着并放下",
              "launch": "放入水中漂流", "transfer": "转移材料批次"}
    def add(action, reason, use, input=None, **kwargs):
        option = building._option(action, reason, use, input=input, **kwargs)
        options.append(option)
    parent = entity.get("physical_parent")
    attached = bool(parent and parent["relation"] == "attached")
    if "transport" in caps and not personal:
        if entity["owner"] is None:
            reason = base or ("附着物件须先解除绑定。" if attached else None) or ("容器内的物件须从容器取出。" if parent else None)
            reason = reason or building._backpack_reason(ctx, player, 1, tree_count=1+len(m.descendants(ctx, entity)))
            add("pickup", reason, "拿到自己的背包；物件及其内容不复制。")
        else:
            add("drop", base or ("附着物件须先解除绑定。" if attached else None), "放在脚下公开，任何附近的人均可拿取使用。")
    if "container" in caps and entity.get("lifecycle") == "intact":
        add("close" if state["open"] else "open", base, "开合容器口；关闭后内容不能装卸。")
        reason = base or ("请先打开容器。" if not state["open"] else None) or ("容器物件格已满。" if len(state["contents"]) >= state["capacity"] else None)
        candidates = [_combination_candidate(ctx, player, entity, e, "store", reason)
                      for e in own if e["id"] != entity["id"] and _movable(ctx, player, e)
                      and entity["id"] not in {child["id"] for child in m.descendants(ctx, e)}]
        if candidates and not any(candidate["preview"]["available"] for candidate in candidates):
            reason = reason or candidates[0]["preview"]["reason"]
        add("store", reason or ("没有可装入的本人物件。" if not candidates else None), "搬入完整子物件，保留其身份与自身材料；本体不会变成目标的contained配料。载荷超限可能导致后续结构失效。", _input(candidates))
        children = [m.get_entity(ctx, eid) for eid in state["contents"]] if state["open"] else []
        candidates = [_candidate(e) for e in children if _movable(ctx, player, e)]
        add("retrieve", base or ("请先打开容器。" if not state["open"] else None) or ("没有可取出的物件。" if not candidates else None),
            "取出到本人背包，保留物件身份。", _input(candidates))
    sources = "water_source" in components or "water_source" in caps or "container" in components and state["water_capacity"] > 0
    water_items = [e for e in own if "container" in e["capabilities"] and e["state"]["open"] and e["state"]["water_capacity"] > e["state"]["water"]]
    if sources:
        source_quantity = state["water_remaining"] if "water_source" in caps else state["water"]
        reason = base or ("请先打开取水口。" if "container" in caps and not state["open"] else None)
        reason = reason or ("水源已空。" if source_quantity <= 0 else None)
        candidates = [_candidate(e) for e in water_items if e["id"] != entity["id"]]
        disclosed = source_quantity if state["open"] or entity["owner"] is not None else 1
        add("fill", reason or ("没有打开且未满的本人容器。" if not candidates else None), "从目标的有限水量转移到所选本人容器。", _input(candidates, quantity=disclosed))
    if ("container" in components and state["water_capacity"] > 0) or "absorbent" in components:
        wet = [e for e in building._water_items(ctx, player) if e["id"] != entity["id"]]
        candidates = [_candidate(e) for e in wet]
        room = m.water_transfer_room(entity, _pour_target_slot(entity))
        maximum = min(room, max([e["state"]["water"] for e in wet] or [0]))
        reason = _reason(ctx, player, entity, burning=True) or ("没有可用的本人水。" if not candidates else None)
        reason = reason or ("目标真实储水或吸水容量不足。" if room <= 0 else None)
        add("pour", reason, "固定数量和作用槽倒水；提交时重验当前水量、容量、距离和权限。去向变化拒绝，水不穿透关闭容器。",
            _input(candidates, quantity=maximum), target_slot=_pour_target_slot(entity))
    fuel_lots = [lot for lot in entity.get("matter", []) if registry_for(entity).materials[lot["material"]].combustible and lot["quantity"] > 0
                 and (state["open"] or lot["slot"] != "contained" and lot["role"] != "lining")]
    if fuel_lots:
        tools = [_candidate(e) for e in own if "ignition" in e["capabilities"] and e["state"].get("uses", 0) > 0]
        x, y = m.entity_location(ctx, entity)
        reason = _reason(ctx, player, entity, burning=True)
        reason = reason or ("只能点燃公共地面物件。" if not m.is_ground(entity) or personal else None)
        reason = reason or ("出生安全区不能点火。" if m.is_safe(ctx, x, y) else None)
        reason = reason or ("目标太湿。" if state["wetness"] >= 35 else None)
        reason = reason or ("目标已经燃烧。" if state["burning"] else None)
        reason = reason or ("没有可用点火工具。" if not tools else None)
        reason = reason or ("火或内容释放容量不足。" if not processes.can_ignite(ctx, entity) else None)
        add("ignite", reason, "只燃烧物件中真实可燃批次；石壳本身不会变成燃料。", _input(tools))
    if "structure" in components and entity.get("lifecycle") == "intact":
        parts = [e for e in own if _movable(ctx, player, e) and "attachment" in e.get("components", {}) and not _attach_reason(ctx, e, entity)]
        candidates = [_combination_candidate(ctx, player, entity, e, "attach", base) for e in parts]
        reason = base or ("没有兼容的本人部件。" if not parts else None)
        if candidates and not any(candidate["preview"]["available"] for candidate in candidates):
            reason = reason or candidates[0]["preview"]["reason"]
        add("attach", reason, "把部件唯一绑定到目标，载荷和作用由组件统一计算。", _input(candidates))
    if attached:
        add("detach", base or ("私人物件不能公开放下。" if personal else None), "解除绑定并放到脚下公开；不能绕过关闭的父容器。")
    # Attached parts contribute lift through the same physical load summary.
    if m.load_summary(ctx, entity)["buoyancy"] > 0:
        x, y = m.entity_location(ctx, entity)
        reason = base or ("请先把物件放在公共地面。" if not m.is_ground(entity) else None)
        reason = reason or ("只有浅溪上的物件能下水。" if x is None or m.tile_at(ctx, x, y) != "water" else None)
        reason = reason or ("实际浮力不足以支撑当前载荷。" if not m.eligible_drift(ctx, entity) else None)
        reason = reason or ("附近没有未被占用的漂流路径。" if not _launch_route(ctx, entity) else None)
        add("launch", reason, "以真实浮力和载荷漂流；进水、火和结构失效会影响途中后果。")
    if "container" in components and entity.get("lifecycle") == "intact" and not personal:
        sources = {e["id"]: e for e in building._owned_available(ctx, player, lambda e: e["owner"] == player["id"])}
        sources.update({e["id"]: e for e in m.public_entities_near(ctx,player["x"],player["y"],radius=1,limit=96)
                        if not _reason(ctx,player,e) and "container" in e.get("components", {})})
        candidates = [_candidate(e, lots) for e in sources.values() if e["id"] != entity["id"] and (lots := _transfer_lots(ctx, player, e))]
        from .creative import guard_ordinary_action
        reason = base
        try:
            guard_ordinary_action(ctx, "transfer", entity)
        except PermissionDenied as exc:
            reason = reason or str(exc)
        reason = reason or ("请先打开目的容器。" if not state["open"] else None) or ("没有可转移的本人材料批次。" if not candidates else None)
        add("transfer", reason, "选择来源物件及lot_id，按amount将已有批次转入目标contained材料槽；材料身份不变，源余量保留，增加目标载荷。本人个人背包仅可转出自身contained散料，不取本体、衬层或完整子物件；个人背包不接收散料。与搬入完整物件、制备新材料不同。",
            _input(candidates, quantity=max([lot["quantity"] for c in candidates for lot in c["lots"]] or [1]), lots=True), target_slot="contained")
    for option in options:
        action = option["action"]
        option.update(label=labels[action], arguments={"target_id": entity["id"], "expected_target_version": entity["version"]})
        option["preconditions"] = [_access_observation(ctx, player, entity)]
        if "target_slot" in option:
            option["arguments"]["target_slot"] = option["target_slot"]
        if action == "tend":
            option["input"] = _input([_candidate(e) for e in building._water_items(ctx, player, 5)])
        elif action == "fuel":
            fuels = [e for e in own if e["material"] == "wood" and building.material_status(ctx, player, e)["eligible"]]
            option["input"] = _input([_candidate(e, [lot for lot in building._body_lots(e) if lot["material"] == "wood"]) for e in fuels], quantity=option["maximum"], lots=True)
        elif action == "switch_mode":
            option["input"] = _input(choices=[{"value": value, "label": "遮雨" if value == "shelter" else "集雨"} for value in option["modes"]])
        for candidate in (option.get("input") or {}).get("candidates", []):
            item = m.get_entity(ctx, candidate["id"])
            candidate["preconditions"] = [_access_observation(ctx, player, item, subject="item")]
    return options


def available_actions(ctx, player, entity):
    return [option["action"] for option in _action_options(ctx, player, entity) if option["available"]]


def inspect(ctx, args):
    from .generation import public_content
    if "candidate_id" in args and "action" not in args:
        raise RuleViolation("candidate_id须与action同时提供。")
    player = m.get_player(ctx)
    entity = m.get_entity(ctx, args["entity_id"])
    _accessible(ctx, player, entity, near=False)
    visible = _project_entity(ctx, player, entity, detailed=True)
    if "action" in args:
        action = args["action"]
        options = visible["action_options"]
        selected = [option for option in options if option["action"] == action]
        if not selected:
            raise RuleViolation("此物件没有声明所选行动；请使用实际action名称。")
        visible["action_options"] = selected
        visible["available_actions"] = [option["action"] for option in selected if option["available"]]
        visible["action_options_projection"] = {
            "scope": "selected_declared_action", "action": action,
            "expanded_actions": [action],
            "unexpanded_actions": [{key: option[key] for key in
                ("action", "label", "function_id", "available", "reason") if key in option}
                for option in options if option["action"] != action],
            "meaning": "Only the selected action's complete arguments, conditions, candidates and previews are expanded. "
                       "Other declared actions are outlines only; read grove.inspect with their action name before using missing inputs or previews."}
        if "candidate_id" in args:
            candidate_id = args["candidate_id"]
            groups = []
            for option in selected:
                inputs = option.get("input") or {}
                if "candidates" in inputs:
                    groups.append((inputs, "candidates"))
                choices = option.get("choices", {})
                groups.extend((choices, name) for name in ("items", "others") if name in choices)
            candidates = [candidate for group, name in groups for candidate in group[name]]
            if not any(candidate["id"] == candidate_id for candidate in candidates):
                raise RuleViolation("所选行动没有这个实际候选；请使用该行动返回的候选id。")
            for group, name in groups:
                group[name] = [candidate for candidate in group[name] if candidate["id"] == candidate_id]
            visible["action_options_projection"]["candidate_selection"] = {
                "candidate_id": candidate_id, "expanded_candidate_ids": [candidate_id],
                "unexpanded_candidate_ids": list(dict.fromkeys(candidate["id"] for candidate in candidates
                                                                if candidate["id"] != candidate_id)),
                "unexpanded_details_provided": False,
                "read": {"function_id": "grove.inspect", "entity_id": entity["id"], "action": action,
                         "candidate_id_argument": "Select one actual ID from unexpanded_candidate_ids."},
                "meaning": "Only the selected candidate's complete facts, version, conditions and preview are provided. "
                           "Other candidate IDs are discovery references, without versions or previews; read the same entity/action with that candidate_id before acting on them."}
            visible["action_options_projection"]["meaning"] = (
                "Only the selected declared action and candidate are expanded; the object's actual state and creative facts remain. "
                "Other actions and candidates are explicitly unexpanded and supply no missing action inputs or previews.")
    contents = [m.get_entity(ctx, eid) for eid in entity["state"]["contents"]] if entity["state"]["open"] else []
    explanation = f"{entity['name']}：完整性{visible['simulation']['integrity']}，实际载荷{visible['simulation']['total']}，支撑{visible['simulation']['limit']}。"
    if entity["state"].get("kind") == "resource_node":
        explanation = f"{entity['name']}：公共库存{entity['state']['stock']}/{entity['state']['stock_capacity']}；"+("石材有限。" if entity['state']['resource_kind'] == 'stone' else '养护消耗本人5u水。')
    combustion = visible["simulation"].get("combustion")
    if combustion and combustion["burning_quantity"] and combustion["rate"]:
        explanation += (f" 可见燃料{combustion['fuel_quantity']}u，当前燃烧{combustion['burning_quantity']}u；"
                        f"每燃烧批次每{combustion['cadence_ticks']}逻辑步耗{combustion['rate']}u，"
                        f"距下一耗材结算{combustion['ticks_until_next_consumption']}逻辑步。")
    result = {"player": _public_self(player), "entity": visible,
        "contents": [_project_entity(ctx, player, e, include_options=False) for e in contents],
        "available_actions": visible["available_actions"], "action_options": visible["action_options"], "explanation": explanation,
        "public_content": public_content(ctx, player, entity["state"].get("generation_region"))}
    if "action_options_projection" in visible:
        result["action_options_projection"] = visible["action_options_projection"]
    return FunctionOutcome(result)


def _target(ctx, args, *, revalidate=False):
    eid = args.get("target_id") or args.get("item_id")
    if not eid:
        raise RuleViolation("此行动需要选择目标物件。")
    entity = m.get_entity(ctx, eid)
    if not revalidate and args.get("expected_target_version") is not None and args["expected_target_version"] != entity["version"]:
        raise StateConflict("目标物件已经变化，请刷新后再行动。")
    return entity


def _own_item(ctx, player, args, capability=None, *, revalidate=False):
    if not args.get("item_id"):
        raise RuleViolation("请从背包选择使用的物件。")
    item = m.get_entity(ctx, args["item_id"])
    if not revalidate and args.get("expected_item_version") is not None and args["expected_item_version"] != item["version"]:
        raise StateConflict("所选物件已经变化，请刷新后再行动。")
    _accessible(ctx, player, item, subject="item")
    if item["owner"] != player["id"] or item.get("lifecycle") == "consumed" or item["state"].get("retired") or "personal" in item["capabilities"]:
        raise RuleViolation("须使用自己持有的可用物件。")
    if capability and capability not in item["capabilities"]:
        raise RuleViolation("所选物件没有所需能力。")
    return item


def _container(ctx, player, entity):
    reason = _reason(ctx, player, entity)
    if reason:
        raise RuleViolation(reason)
    if "container" not in entity.get("components", {}) or entity.get("lifecycle") != "intact" or not entity["state"]["open"]:
        raise RuleViolation("目标必须是打开且结构完整的容器。")


def _store(ctx, player, item, target):
    _container(ctx, player, target)
    if not _movable(ctx, player, item):
        raise RuleViolation("燃烧、私有保护或附着中的物件不能装卸。")
    if item["id"] == target["id"] or target["id"] in {e["id"] for e in m.descendants(ctx, item)}:
        raise RuleViolation("容纳或绑定关系不能循环。")
    if len(target["state"]["contents"]) >= target["state"]["capacity"]:
        raise RuleViolation("容器物件格已满。")
    m.contain(ctx, target, item)


def interact(ctx, args):
    from .generation import record_content_outcome
    from .creative import check_delegation, guard_ordinary_action
    check_delegation(ctx, args)
    player = m.get_player(ctx)
    m.check_player_version(player, args["expected_player_version"])
    action = args["action"]
    # An explicit pour is a fixed intent, not a promise about a past temperature
    # or fuel amount. Current legality is rechecked inside this transaction;
    # implicit/default pours and other actions retain strict observed versions.
    revalidate_pour = action == "pour" and "amount" in args and "target_slot" in args
    target = _target(ctx, args, revalidate=revalidate_pour)
    guard_ordinary_action(ctx, action, target, m.get_entity(ctx, args["item_id"]) if args.get("item_id") else None)
    if action in {"store", "attach"} and "expected_combination_fingerprint" in args:
        if not args.get("item_id"):
            raise RuleViolation("请选择这次组合使用的本人物件。")
        item = m.get_entity(ctx, args["item_id"])
        current = m.preview_relation(ctx, target, item, "contained" if action == "store" else "attached")
        if args["expected_combination_fingerprint"] != _combination_fingerprint(player, current["version_fingerprint"]):
            raise StateConflict("组合物件或承载依据已经变化，请刷新预览后再次选择。")
    _accessible(ctx, player, target)
    option = next((o for o in _action_options(ctx, player, target) if o["action"] == action), None)
    if not option or not option["available"]:
        raise RuleViolation(option["reason"] if option else "此物件没有所选行动。")
    affected_ids = {target["id"]}
    if args.get("item_id"):
        affected_ids.add(args["item_id"])
    if action in {"harvest", "tend", "fuel", "dismantle", "switch_mode"}:
        summary = building.interact_structure(ctx, player, args, target)
    elif action == "pickup":
        _store(ctx, player, target, m.get_entity(ctx, player["backpack_id"]))
        summary = f"拿起了{target['name']}。"
    elif action in {"drop", "detach"}:
        item = _own_item(ctx, player, {**args, "item_id": target["id"]}, "transport") if action == "drop" else target
        if "blocking" in item["capabilities"] and (m.is_safe(ctx, player["x"], player["y"]) or m.blocking_at(ctx, player["x"], player["y"], ignore=item["id"])):
            raise RuleViolation("安全区或已有障碍的位置不能放障碍。")
        if "blocking" in item["capabilities"] and len(m.public_players_near(ctx, player["x"], player["y"], radius=0)) > 1:
            raise RuleViolation("不能把障碍放在其他人的位置。")
        m.detach(ctx, item, owner=None, x=player["x"], y=player["y"])
        summary = f"{'解除附着并' if action == 'detach' else ''}放下了{item['name']}，现在公开可用。"
    elif action in {"open", "close"}:
        target["components"]["container"]["open"] = action == "open"
        m.save_entity(ctx, target)
        summary = f"{'打开' if action == 'open' else '关闭'}了{target['name']}。"
    elif action == "store":
        item = _own_item(ctx, player, args, "transport")
        _store(ctx, player, item, target)
        summary = f"把{item['name']}装入{target['name']}，保留原物件身份。"
    elif action == "retrieve":
        _container(ctx, player, target)
        if args.get("item_id") not in target["state"]["contents"]:
            raise RuleViolation("所选物件不在目标容器内。")
        item = m.get_entity(ctx, args["item_id"])
        if args.get("expected_item_version") is not None and item["version"] != args["expected_item_version"]:
            raise StateConflict("所选内容已经变化。")
        if not _movable(ctx, player, item):
            raise RuleViolation("所选内容不能取出。")
        if target["id"] == player["backpack_id"]:
            m.detach(ctx, item, owner=player["id"])
        else:
            _store(ctx, player, item, m.get_entity(ctx, player["backpack_id"]))
        summary = f"取出了{item['name']}。"
    elif action == "attach":
        part = _own_item(ctx, player, args, "transport")
        reason = _attach_reason(ctx, part, target)
        if reason:
            raise RuleViolation(reason)
        m.attach(ctx, target, part)
        summary = f"将{part['name']}唯一附着到{target['name']}，实际载荷已改变。"
    elif action == "launch":
        route = _launch_route(ctx, target)
        if route is None:
            raise RuleViolation("附近没有未被占用的漂流路径。")
        processes.start_drift(ctx, target["id"], ticks=8, route=route, start_delay=0)
        summary = f"将{target['name']}放入溪中，按实际浮力和载荷漂流。"
    elif action == "fill":
        item = _own_item(ctx, player, args, "container")
        _container(ctx, player, item)
        if "water_source" not in target["capabilities"]:
            _container(ctx, player, target)
        amount = args.get("amount", min(item["state"]["water_capacity"]-item["state"]["water"], target["state"]["water_remaining"] if 'water_source' in target['capabilities'] else target['state']['water']))
        result = m.transfer_water(ctx, target, item, amount, reason="fill")
        summary = f"向{item['name']}转入{result['transferred']}u真实水。"
    elif action == "pour":
        item = _own_item(ctx, player, args, "container", revalidate=revalidate_pour)
        _container(ctx, player, item)
        slot = _pour_target_slot(target)
        if args.get("target_slot", slot) != slot:
            raise StateConflict("倒水作用位置已变化，请重新选择浇表面或灌入容器；没有自动改去向或数量。")
        amount = args.get("amount", min(5, item["state"]["water"], m.water_transfer_room(target, slot)))
        was_burning = target["state"]["burning"]
        result = m.transfer_water(ctx, item, target, amount, target_slot=slot, reason="pour")
        summary = f"向{target['name']}转移{result['transferred']}u水，{'装入容器' if slot == 'contained' else '只接触外表面'}。"
        if was_burning and not target["state"]["burning"]:
            summary += "本次燃烧已熄灭。"
    elif action == "transfer":
        if not args.get("item_id"):
            raise RuleViolation("请选择实际来源物件。")
        item = m.get_entity(ctx,args["item_id"])
        _accessible(ctx,player,item,subject="item")
        if args.get("expected_item_version") is not None and item["version"] != args["expected_item_version"]:
            raise StateConflict("来源物件已经变化，请刷新后再行动。")
        _container(ctx, player, target)
        lot = next((lot for lot in _transfer_lots(ctx, player, item) if lot["id"] == args.get("lot_id")), None)
        if not lot or not args.get("amount"):
            raise RuleViolation("请选择可转移的真实材料批次和数量。")
        result = m.transfer_material(ctx, item, target, lot["id"], args["amount"], target_slot=args.get("target_slot", "contained"), reason="human_transfer")
        summary = f"向{target['name']}转入{result['transferred']}u{lot['material']}，源余量保留。"
    elif action == "ignite":
        tool = _own_item(ctx, player, args, "ignition")
        if tool["state"].get("uses", 0) <= 0:
            raise RuleViolation("点火工具已经耗尽。")
        m.start_burning(ctx, target, args.get("lot_id"))
        tool["state"]["uses"] -= 1
        m.save_entity(ctx, tool)
        summary = f"点燃了{target['name']}中真实可燃材料。"
    else:
        raise RuleViolation("未知行动。")
    player = m.get_player(ctx)
    m.save_player(ctx, player)
    target = m.get_entity(ctx, target["id"])
    x, y = m.entity_location(ctx, target)
    m.record_change(ctx, summary, x=x, y=y, entity_id=target["id"], action=action)
    for affected_id in sorted(affected_ids):
        record_content_outcome(ctx, affected_id, action, summary)
    return FunctionOutcome({"player": _public_self(player), "entity": _project_entity(ctx, player, target),
        "changes": [summary], "viewport": viewport(ctx, {})}, events=(StreamEvent("grove.changes", "interact",
        {"summary": f"{player['name']}{summary}", "x": x, "y": y, "entity_id": target["id"], "action": action}, key="action"),))


def _process_refs(ctx, entity):
    active = tuple(processes.active_process_ids(ctx))
    cached = getattr(ctx, "_grove_projection_processes", None)
    if cached is None or cached[0] != active:
        rows = [processes.get_process(ctx, pid) for pid in active]
        cached = (active, [row for row in rows if row and row["status"] == "active"])
        ctx._grove_projection_processes = cached
    roots, cursor = {entity["id"]}, entity
    for _ in range(8):
        parent = cursor.get("physical_parent")
        if not parent:
            break
        roots.add(parent["id"])
        cursor = m.get_entity(ctx, parent["id"])
    region = m.chunk_id(*m.chunk_for(entity["x"], entity["y"])) if m.is_ground(entity) else None
    return [{"id": row["id"], "kind": row["kind"]} for row in cached[1]
            if row["entity_id"] in roots or entity["id"] in row.get("reserved_ids", [])
            or row["kind"] == "rain" and region in row["chunk_ids"]]


def _project_entity(ctx, player, entity, *, include_options=True, detailed=False):
    value = deepcopy(entity)
    hidden = entity["owner"] is None and not entity["state"]["open"] and "container" in entity["components"]
    children = [child for child in m.descendants(ctx, entity) if (child.get("physical_parent") or {}).get("id") == entity["id"]]
    if hidden:
        value["matter"] = [lot for lot in value["matter"] if lot["slot"] != "contained" and lot["role"] != "lining"]
        value["state"]["contents"] = []
        value["state"]["water"] = None
        if "hearth_fuel" in value["state"]:
            value["state"]["hearth_fuel"] = None
        value["state"]["fuel"] = sum(lot["quantity"] for lot in value["matter"] if registry_for(entity).materials[lot["material"]].combustible)
        value.get("composition", {}).pop("lining", None)
        visible_ids = {lot["id"] for lot in value["matter"]}
        for lot in value["matter"]:
            if lot.get("bound_to") not in visible_ids:
                lot.pop("bound_to", None)
        for component in value["components"].values():
            if "supports" in component:
                component["supports"] = {key: support for key,support in component["supports"].items() if key in visible_ids}
        children = [child for child in children if child["physical_parent"]["relation"] != "contained"]
    value["contents"] = list(value["state"]["contents"])
    if ("container" in value["components"] and value["state"]["water_capacity"] > 0) or "absorbent" in value["components"]:
        # A compact post-action observation can preserve where a later named
        # pour would contact this object without pretending to be a full menu.
        value["pour_target_slot"] = _pour_target_slot(entity)
    if include_options:
        from .creative import action_options
        value["action_options"] = [*_action_options(ctx, player, entity), *action_options(ctx, player, entity)]
        value["available_actions"] = [option["action"] for option in value["action_options"] if option["available"]]
    from .creative import projection
    if hidden and value["state"].get("program"):
        program = value["state"]["program"]
        value["state"]["program"] = {key: program[key] for key in ("definition_ref", "type_id") if key in program}
        value["state"]["program"]["hidden"] = True
        value["creative"] = deepcopy(value["state"]["program"])
    else:
        value["creative"] = projection(ctx, entity, detailed=detailed)
        if value["creative"] and value["state"].get("program"):
            program = value["state"]["program"]
            program["material_definitions"] = program.pop("materials", {})
            program["process"] = deepcopy(value["creative"]["process"])
    if detailed and include_options and not hidden and value["creative"]:
        from .observations import action_condition_observations
        conditions = action_condition_observations(ctx, player, entity)
        for option in value["action_options"]:
            if option.get("function_id") == "grove.rule_action":
                option["preconditions"] = [_access_observation(ctx, player, entity)]
                if option["action"] in conditions:
                    option["condition_observation"] = conditions[option["action"]]
                for name, subject in (("items", "item"), ("others", "other")):
                    for candidate in option.get("choices", {}).get(name, []):
                        if candidate.get("kind") == "entity":
                            item = m.get_entity(ctx, candidate["id"])
                            observation = _access_observation(ctx, player, item, subject=subject)
                            if subject == "other" and observation["known"]:
                                first = m.entity_location(ctx, entity)
                                second = m.entity_location(ctx, item)
                                distance = max(abs(first[0]-second[0]), abs(first[1]-second[1]))
                                observation.update(condition="interface_distance", metric="chebyshev",
                                    actual_distance=distance, required_max=2, satisfied=distance <= 2,
                                    reference_id=entity["id"], reference_location={"x": first[0], "y": first[1]})
                                observation.pop("player_location", None)
                            candidate["preconditions"] = [observation]
                            if subject == "item" and observation["known"] and option["action"] in {"charge", "fuel", "deliver"}:
                                slots = ["body", "contained", "surface", "absorbed"] if option["action"] == "deliver" else ["body", "contained", "surface"]
                                quantities = {}
                                for lot in item["matter"]:
                                    if not lot["burning"] and lot["slot"] in slots:
                                        quantities[lot["material"]] = quantities.get(lot["material"], 0)+lot["quantity"]
                                candidate["source_materials"] = {"slots": slots,
                                    "quantities": quantities, "unit": "logical_matter", "whole_children_included": False}
                if option["action"] == "charge":
                    option["preconditions"].append({"subject": "target", "entity_id": entity["id"],
                        "condition": "container_open", "known": True, "satisfied": entity["state"]["open"]})
    value["construction_material"] = building.material_status(ctx, player, entity)
    value["relations"] = {"parent": value["physical_parent"], "children": [
        {"id": child["id"], "relation": child["physical_parent"]["relation"], "name": child["name"]} for child in children]}
    x, y = m.entity_location(ctx, entity)
    value["location"] = {"x": x, "y": y}
    value["simulation"] = m.load_summary(ctx, entity)
    if hidden:
        value["simulation"]["water"] = None
        value["simulation"]["burning_lots"] = [lot["id"] for lot in value["matter"] if lot.get("burning")]
        body = {}
        for lot in value["matter"]:
            if lot["slot"] == "body":
                body[lot["material"]] = body.get(lot["material"], 0)+lot["quantity"]
        value["simulation"]["body"] = [{"material": material, "quantity": quantity} for material, quantity in body.items()]
    entity_registry = registry_for(entity)
    combustible_lots = [lot for lot in value["matter"] if entity_registry.materials[lot["material"]].combustible]
    if combustible_lots or "combustion" in value["components"]:
        tick = ctx.get_state(m.META, "clock", {"sequence": 0})["sequence"]
        burning_quantity = sum(lot["quantity"] for lot in combustible_lots if lot.get("burning"))
        rate = value["components"].get("combustion", {}).get("rate", 2)
        distance = COMBUSTION_CADENCE_TICKS-tick % COMBUSTION_CADENCE_TICKS
        value["simulation"]["combustion"] = {
            "fuel_quantity": sum(lot["quantity"] for lot in combustible_lots),
            "burning_quantity": burning_quantity, "quantity_unit": "u",
            "quantity_scope": "visible_material_lots", "hidden_contents": hidden,
            "rate": rate, "rate_unit": "u_per_burning_lot_per_settlement",
            "cadence_ticks": COMBUSTION_CADENCE_TICKS, "clock_sequence": tick,
            "next_consumption_tick": tick+distance if burning_quantity and rate else None,
            "ticks_until_next_consumption": distance if burning_quantity and rate else None,
            "spread_cadence_ticks": COMBUSTION_CADENCE_TICKS,
            "quenching_cadence_ticks": 1}
    quantities = {}
    for lot in value["matter"]:
        slots = quantities.setdefault(lot["material"], {})
        slots[lot["slot"]] = slots.get(lot["slot"], 0)+lot["quantity"]
    value["material_quantity"] = {"unit": "logical_matter", "by_material": quantities}
    value["content_summary"] = {"visible": not hidden, "item_count": len(value["contents"]) if not hidden else None,
                                "materials": [{"material": key, "quantity": slots.get("contained", 0)} for key, slots in quantities.items() if slots.get("contained")]}
    value["process_refs"] = _process_refs(ctx, entity)
    if building.is_structure(entity):
        value["construction"] = building.effect_projection(entity)
        if hidden:
            value["construction"]["water"] = None
    if not include_options:
        value.pop("action_options", None)
        value.pop("authored_description", None)
        value.pop("created_at", None)
        value.pop("created_by", None)
        value["presentation"] = {"size": value.get("composition", {}).get("size", 1)}
        value["barrier_active"] = bool(value.get("components", {}).get("barrier", {}).get("active", False))
        value.pop("composition", None)
        value.pop("components", None)
        value["state"] = {key: item for key, item in value["state"].items() if key in {
            "kind", "wetness", "fuel", "burning", "destroyed", "open", "capacity", "contents", "water",
            "water_capacity", "water_remaining", "water_source_capacity", "recipe", "mode", "lit", "hearth_fuel",
            "resource_kind", "stock", "stock_capacity", "regrowth_tick", "uses"}}
        # Views aggregate equal observable matter. Exact lot identities and
        # provenance are read from inspect before a versioned transfer.
        grouped = {}
        for lot in value["matter"]:
            key = (lot["material"], lot["slot"], lot["role"], lot["burning"])
            grouped[key] = grouped.get(key, 0)+lot["quantity"]
        value["matter"] = [{"material": key[0], "slot": key[1], "role": key[2], "burning": key[3], "quantity": amount}
                           for key, amount in grouped.items()]
    return value


def build_function(ctx,args):
    from .creative import check_delegation
    check_delegation(ctx, args)
    from .generation import record_content_outcome
    player=m.get_player(ctx)
    m.check_player_version(player,args["expected_player_version"])
    entity,summary=building.build(ctx,player,args)
    player=m.get_player(ctx)
    m.save_player(ctx,player)
    m.record_change(ctx,summary,x=entity["x"],y=entity["y"],entity_id=entity["id"],action="build")
    record_content_outcome(ctx,entity["id"],"build",summary)
    return FunctionOutcome({"player":_public_self(player),"entity":_project_entity(ctx,player,entity),
        "changes":[summary],"viewport":viewport(ctx,{})},events=(StreamEvent("grove.changes","build",
        {"summary":f"{player['name']}{summary}","x":entity["x"],"y":entity["y"],
         "entity_id":entity["id"],"action":"build","recipe":args["recipe"]},key="build"),))


def viewport(ctx,args):
    from .generation import generation_opportunities, public_content
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
    inventory_ids = list(player["inventory"])
    start = 0
    if args.get("inventory_after"):
        if args["inventory_after"] not in inventory_ids:
            raise RuleViolation("背包分页位置已变化，请重新读取第一页。")
        start = inventory_ids.index(args["inventory_after"])+1
    own=[]
    for eid in inventory_ids[start:start+args.get("inventory_limit",16)]:
        entity=m.get_entity(ctx,eid)
        if entity["owner"]!=player["id"]:
            raise RuleViolation("背包归属索引无效。")
        own.append(_project_entity(ctx,player,entity,include_options=False))
    changes=ctx.get_state(m.META,"changes",[])
    changes=[c for c in changes if c["x"] is None or max(abs(c["x"]-x),abs(c["y"]-y))<=radius][-12:]
    totals = {}
    for entity in building._owned_available(ctx,player,lambda e: building.material_status(ctx,player,e)["eligible"]):
        status = building.material_status(ctx,player,entity)
        totals[entity["material"]] = totals.get(entity["material"],0)+status["quantity_u"]
    body={"player":_public_self(player),"chunks":chunks,
          "entities":[_project_entity(ctx,player,e,include_options=False) for e in raw_entities],
          "players":m.public_players_near(ctx,x,y,radius),"inventory":own,
          "inventory_total":len(inventory_ids),"inventory_next":own[-1]["id"] if own and start+len(own)<len(inventory_ids) else None,
          "processes":processes.visible_processes(ctx,x,y,radius),
          "opportunities":generation_opportunities(ctx,{**player,"x":x,"y":y},radius),
          "changes":changes,"public_content":public_content(ctx,player),
          "bounds":{"min_x":x-radius,"min_y":y-radius,"max_x":x+radius,"max_y":y+radius},
          "truncated":len(raw_entities)>=160,"clock":ctx.get_state(m.META,"clock"),
          "construction":{"recipes":building.recipes(),"material_totals":totals,"unit":"logical_matter",
                          "nearby":building.nearby_effects(ctx,x,y,radius),
                          "radius_metric":"chebyshev","limits":{"hearths":building.MAX_HEARTHS,"growth":processes.MAX_GROWTH}}}
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
        elif len(body["inventory"])>1:
            body["inventory"].pop()
            body["inventory_next"]=body["inventory"][-1]["id"]
        else:
            raise RuleViolation("附近投影过大，请缩小视野半径。")
    return body


def viewport_function(ctx,args):
    return FunctionOutcome(viewport(ctx,args))


def map_project(ctx,args):
    body=viewport(ctx,args)
    entities={e["id"]:e for e in body.pop("entities")}
    return {"entities":entities,"meta":body}


def _registered_entity_schema():
    schema = deepcopy(ENTITY_SCHEMA)
    props = schema["properties"]
    # Immutable material snapshots are validated by the trusted world handler;
    # the static state schema must not snapshot one world's dynamic registry.
    props["matter"]["items"]["properties"]["material"] = {"type": "string", "minLength": 1, "maxLength": 180}
    props["composition"]["properties"]["construction"]["enum"] = list(CONSTRUCTIONS)
    props["components"]["properties"] = {name: {"type": "object", "properties": {
        field: ({"type": "boolean"} if kind is bool else {"type": "string", "maxLength": 80} if kind is str
                else {"type": "object", "maxProperties": 8} if kind is dict
                else {"type": "integer", "minimum": minimum, "maximum": maximum})
        for field, (kind, minimum, maximum) in definition.fields.items()}, "additionalProperties": False}
        for name, definition in REGISTRY.components.items()}
    return schema


def make_world():
    from .generation import generation_specs, generation_state_specs
    from . import creative
    def with_delegation(schema):
        schema = deepcopy(schema)
        schema["properties"]["delegation"] = creative.DELEGATION
        return schema
    return WorldDefinition("shared-grove","溪源",(
        FunctionSpec("grove.join",join,JOIN_ARGS,description="以当前身份进入或接续溪源。"),
        FunctionSpec("grove.move",move,with_delegation(MOVE_ARGS),authorize=member,description="本人或明确目标委托向四个方向之一走一格，读取失败与后果后接续。"),
        FunctionSpec("grove.inspect",inspect,INSPECT_ARGS,version=9,access="read",authorize=member,description="在同一次读取中返回本人状态、物件的自身材料槽、完整子物件、当前反应条件与缺口、历史过程、编译定义的材料身份与反应系数，及实际行动和装入/绑定候选的只读载荷预览。可选action按实际声明动作名只展开该动作；candidate_id须与action同时给，按input.candidates或choices.items/others的实际id只展开一个完整候选。其他动作和候选明确标为未展开，不提供候选版本或预览；省略筛选则保持全部动作，未知动作或候选明确拒绝。系数与历史过程不证明本次已经消耗或生产。"),
        FunctionSpec("grove.interact",interact,with_delegation(INTERACT_ARGS),version=7,authorize=member,description="按实际能力与归属操作物件。store搬入完整子物件，不把其本体变成目标配料；transfer按item_id、lot_id、amount与target_slot转移已有材料批次，材料身份不变。本人personal容器可转出自身contained且非燃烧、非绑定的散料，保护本体、衬层、完整子物件及他人内容；personal目的容器不接收散料。读取inspect的当前候选、目标/来源前置条件与版本；距离拒绝明确对应对象。pour提供固定amount与target_slot时重验当前合法性；燃料或湿度改变版本不单独阻塞，去向改变、来源不足、容量不足或权限/距离改变仍原子拒绝，不换量、来源、目标或槽。"),
        FunctionSpec("grove.build",build_function,with_delegation(BUILD_ARGS),authorize=member,description="用本人原材料搭通路、雨棚或烘干炉，原子投入唯一物件。"),
        FunctionSpec("grove.handbook",handbook,EMPTY,access="read",description="读取正式世界规则，未入场亦可读取。"),
        FunctionSpec("map.viewport",viewport_function,VIEW_ARGS,version=2,access="read",authorize=member,description="读取附近有限地图、自己的背包、自身材料槽与明确标记的历史过程摘要；详细反应条件从inspect读取。"),
        *generation_specs(), *creative.specs()),version=21,state_version=12,
        state_rules=(StateRule(m.PLAYERS,"",PLAYER_SCHEMA),StateRule(m.ENTITIES,"",_registered_entity_schema()),
            StateRule(m.CHUNKS,"",CHUNK_SCHEMA),StateRule(m.PROCESSES,"",PROCESS_SCHEMA,history="metadata"),
            StateRule(m.META,"",{},history="metadata"),*generation_state_specs(),*creative.state_specs()),
        state_authorizer=_state_authorizer,initialize=initialize,bootstrap=bootstrap,
        # All supported older revisions hand off once at the final migration.
        # Timer commands are staged until the migration transaction commits.
        migrations={6: lambda ctx: None, 7: lambda ctx: None, 8: lambda ctx: None, 9: lambda ctx: None, 10: lambda ctx: None, 11: lambda ctx: None, 12: resume_clock},
        timers=(TimerSpec("grove.tick",processes.tick,TICK_ARGS,max_attempts=3,retry_delay_seconds=1),),
        views=(ViewSpec("grove.map",map_project,VIEW_ARGS,authorize=member,renderer="shared-grove",streams=("grove.changes",)),),
        streams=(StreamSpec("grove.changes",public=True,retention_seconds=86400,max_events=4096),),
        entry_instructions="先读grove.handbook与grove.rule_catalog。人可亲自移动操作，或明确目标委托给自己的外部Agent实际读、计划、行动、读回和调整；随时收回。规则发布与实例生成分开，后者消耗真实区域资源。")
