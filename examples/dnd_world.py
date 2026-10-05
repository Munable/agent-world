"""A persistent, bounded fifth-edition SRD 5.1 adventure world.

The Runtime remains neutral. This world alone owns characters, dice, room
rules, combat and the finite adjudication vocabulary. No server model or Agent
loop runs here. An externally configured referee may *offer* a novel check;
only its owner can accept the disclosed risk and produce a recorded roll.
"""
from __future__ import annotations

from copy import deepcopy

from agent_world import EventSpec, FunctionOutcome, FunctionSpec, StateRule, WorldDefinition
from agent_world.errors import PermissionDenied, RuleViolation, StateConflict

from .dnd_data import ABILITY_LABELS, CLASS_PRESETS, ITEMS, NPCS, ROOMS, WORLD_TITLE

ID = {"type": "string", "minLength": 1, "maxLength": 128}
VERSION = {"type": "integer", "minimum": 1}
TEXT = {"type": "string", "minLength": 1, "maxLength": 3000}
EMPTY = {"type": "object", "properties": {}, "additionalProperties": False}
OBJECT = {"type": "object"}
CHARACTERS = "dnd:characters"
SCENES = "dnd:scenes"
INTENTS = "dnd:intents"
HISTORY = "dnd:history"
WORLD_STATE = "dnd:world"
SUCCESS_EFFECTS = ("open_bandit_door", "secure_gap", "bandits_allow_passage", "reveal_store", "none")
FAILURE_EFFECTS = ("none", "damage_1d6")
ACTIONS = ("move", "search", "unlock", "force", "set_rope", "jump", "negotiate", "attack", "join_combat", "dodge", "help", "end_turn", "retreat", "heal", "rest", "take", "transfer", "deliver", "stabilize", "death_save")
CHAPTER_IDS = ("index", "entry", "play", "agent", "together", "checks", "combat", "actions", "authority", "recovery", "gm")


def _schema(properties, required=()):
    return {"type": "object", "properties": properties, "required": list(required), "additionalProperties": False}


JOIN_ARGS = _schema({"name": {"type": "string", "minLength": 1, "maxLength": 40}, "class_id": {"enum": list(CLASS_PRESETS)}}, ("name", "class_id"))
ACT_ARGS = _schema({"action": {"enum": list(ACTIONS)}, "target": ID, "recipient": ID, "option": {"type": "string", "maxLength": 100}, "expected_character_version": VERSION, "expected_scene_version": VERSION}, ("action",))
PROPOSE_ARGS = _schema({"intent_id": ID, "text": TEXT, "expected_character_version": VERSION, "expected_scene_version": VERSION}, ("intent_id", "text", "expected_character_version", "expected_scene_version"))
RULE_ARGS = _schema({"intent_id": ID, "ability": {"enum": list(ABILITY_LABELS)}, "dc": {"type": "integer", "minimum": 10, "maximum": 18}, "advantage": {"type": "boolean"}, "success_effect": {"enum": list(SUCCESS_EFFECTS)}, "failure_effect": {"enum": list(FAILURE_EFFECTS)}, "reason": TEXT, "risk": TEXT}, ("intent_id", "ability", "dc", "success_effect", "failure_effect", "reason", "risk"))
INTENT_ARGS = _schema({"intent_id": ID}, ("intent_id",))
SAY_ARGS = _schema({"text": {"type": "string", "minLength": 1, "maxLength": 1200}}, ("text",))
HISTORY_ARGS = _schema({"after": {"type": "integer", "minimum": 0}, "limit": {"type": "integer", "minimum": 1, "maximum": 50}})
HANDBOOK_ARGS = _schema({"chapter": {"enum": list(CHAPTER_IDS)}}, ("chapter",))


def _authority(ctx):
    return ctx.get_state(WORLD_STATE, "authority", {"judges": [], "epoch": 1})


def _member(ctx, args=None):
    return ctx.get_state(CHARACTERS, ctx.actor_role_id) is not None


def _judge(ctx, args=None):
    return ctx.actor_role_id in _authority(ctx)["judges"]


def _viewer(ctx, args=None):
    return _member(ctx) or _judge(ctx)


def _state_access(ctx, scope, key, access):
    # Raw Runtime state inspection is intentionally narrower than a trusted
    # world handler: room secrets and other people's pending snapshots are
    # exposed only through the world's controlled projections.
    if ctx.actor_role_id == "system:installer":
        return access == "write"
    member = ctx.authorization_state(CHARACTERS, ctx.actor_role_id)
    authority = ctx.authorization_state(WORLD_STATE, "authority", {"judges": []})
    judge = ctx.actor_role_id in authority["judges"]
    world_handler = ctx.function_id.startswith("dnd.") or ctx.function_id == "world.bootstrap"
    if access == "read":
        if scope == CHARACTERS and key == ctx.actor_role_id:
            return True
        if scope == WORLD_STATE and key == "authority" and world_handler:
            return True
        if world_handler and (member is not None or judge or ctx.function_id == "dnd.join"):
            return scope in {WORLD_STATE, CHARACTERS, SCENES, INTENTS, HISTORY}
        return False
    if ctx.function_id == "dnd.join":
        return ((scope == CHARACTERS and key == ctx.actor_role_id)
                or scope in {HISTORY, WORLD_STATE} and member is not None)
    if ctx.function_id == "dnd.rule":
        return judge and scope in {INTENTS, HISTORY, WORLD_STATE}
    if member is None:
        return False
    if ctx.function_id in {"dnd.propose", "dnd.withdraw"}:
        value = ctx.authorization_state(INTENTS, key) if scope == INTENTS else None
        return (scope == INTENTS and (value is None or value["owner"] == ctx.actor_role_id)
                or scope in {HISTORY, WORLD_STATE})
    if ctx.function_id == "dnd.say":
        return scope in {HISTORY, WORLD_STATE}
    if ctx.function_id == "dnd.commit":
        if scope == INTENTS:
            value = ctx.authorization_state(INTENTS, key)
            return value is not None and value["owner"] == ctx.actor_role_id
        return scope in {SCENES, HISTORY, WORLD_STATE} or scope == CHARACTERS and key == ctx.actor_role_id
    if ctx.function_id == "dnd.act":
        return scope in {CHARACTERS, SCENES, HISTORY, WORLD_STATE}
    return False


def _char_record(ctx, role_id=None):
    role_id = ctx.actor_role_id if role_id is None else role_id
    record = ctx.get_state_record(CHARACTERS, role_id)
    if not record["exists"]:
        raise RuleViolation("character does not exist; call dnd.join first")
    return record


def _scene_record(ctx, scene_id):
    if scene_id not in ROOMS:
        raise RuleViolation("unknown scene")
    return ctx.get_state_record(SCENES, scene_id)


def _check_version(record, expected, label):
    if expected is not None and record["version"] != expected:
        raise StateConflict(f"{label} changed; call dnd.look and use its new versions")


def _save_char(ctx, role_id, value, version):
    return ctx.set_state(CHARACTERS, role_id, value, expected_version=version)


def _save_scene(ctx, scene_id, value, version):
    return ctx.set_state(SCENES, scene_id, value, expected_version=version)


def _record(ctx, kind, text, payload=None, *, scene_id=None):
    record = ctx.get_state_record(WORLD_STATE, "chronicle")
    meta = record["value"]
    seq = meta["last_seq"] + 1
    entry = {"seq": seq, "kind": kind, "text": text, "actor": ctx.actor_role_id,
             "scene_id": scene_id, "occurred_at": ctx.now, "payload": payload or {}}
    ctx.set_state(HISTORY, f"{seq:012d}", entry, expected_version=0)
    meta["last_seq"] = seq
    ctx.set_state(WORLD_STATE, "chronicle", meta, expected_version=record["version"])
    return entry


def _events(ctx, entry):
    return tuple(EventSpec(item["key"], "dnd." + entry["kind"], {"seq": entry["seq"], "text": entry["text"], "scene_id": entry["scene_id"]})
                 for item in ctx.list_state(CHARACTERS, limit=200)["items"])


def _outcome(ctx, result, entry=None):
    return FunctionOutcome(result, _events(ctx, entry) if entry else ())


def _mod(score):
    return (score - 10) // 2


def _roll(ctx, die, count=1):
    return [ctx.random_int(1, die) for _ in range(count)]


def _check(ctx, char, ability, dc, *, skill=None, advantage=False, disadvantage=False):
    if advantage and disadvantage:
        advantage = disadvantage = False
    rolls = _roll(ctx, 20, 2 if advantage or disadvantage else 1)
    natural = max(rolls) if advantage else min(rolls) if disadvantage else rolls[0]
    modifier = char["skills"].get(skill, _mod(char["abilities"][ability])) if skill else _mod(char["abilities"][ability])
    total = natural + modifier
    return {"ability": ability, "skill": skill, "rolls": rolls, "natural": natural, "modifier": modifier,
            "total": total, "dc": dc, "success": total >= dc, "advantage": advantage, "disadvantage": disadvantage}


def _alive(char):
    if char["status"] != "alive" or char["hp"] <= 0:
        raise RuleViolation("this character cannot act while unconscious or dead; use death_save, stabilize or healing as appropriate")


def _damage(char, amount, *, critical=False):
    if char["status"] == "dead" or amount <= 0:
        return
    old_hp = char["hp"]
    if amount - old_hp >= char["max_hp"]:
        char["hp"] = 0
        char["status"] = "dead"
        char["death_saves"] = {"successes": 0, "failures": 3}
        return
    char["hp"] = max(0, old_hp - amount)
    if old_hp == 0 and amount:
        # Damage breaks stabilization, starting a fresh unstable sequence.
        # An already-unstable creature retains previously earned successes;
        # injury adds failures rather than erasing the successful saves.
        if char["status"] == "stable":
            char["death_saves"] = {"successes": 0, "failures": 0}
        char["death_saves"]["failures"] += 2 if critical else 1
        char["status"] = "dead" if char["death_saves"]["failures"] >= 3 else "unconscious"
    elif char["hp"] == 0:
        char["status"] = "unconscious"
        char["death_saves"] = {"successes": 0, "failures": 0}


def _heal(char, amount):
    if char["status"] == "dead":
        raise RuleViolation("ordinary healing cannot revive a dead character")
    char["hp"] = min(char["max_hp"], char["hp"] + amount)
    if char["hp"] > 0:
        char["status"] = "alive"
        char["death_saves"] = {"successes": 0, "failures": 0}


def _all_characters(ctx):
    return ctx.list_state(CHARACTERS, limit=200)["items"]


def _scene_combat(ctx, scene_id):
    if scene_id != "bandit_room":
        return None
    return _scene_record(ctx, "bandit_room")["value"]["combat"]


def _in_combat(ctx, char):
    combat = _scene_combat(ctx, char["scene_id"])
    return bool(combat and combat["active"] and ctx.actor_role_id in combat["participants"])


def _attempt(ctx, char, key, *, evidence="ordinary"):
    # A fresh operation ID is transport identity, not new fictional evidence.
    # Another character, a new method or newly consumed Help can change a check.
    token = evidence
    helper = char.get("help")
    if helper and helper["scene_id"] == char["scene_id"]:
        token += ":help:" + helper["by"]
    seen = char["attempts"].get(key, [])
    if token in seen:
        raise RuleViolation("this method already failed under the same conditions; change the method or obtain real new evidence/Help")
    # Keep *all* failed-condition tokens: alternating ordinary/ledger/Help must
    # not resurrect an opportunity already exhausted in that same situation.
    char["attempts"][key] = [*seen, token]
    advantage = bool(helper and helper["scene_id"] == char["scene_id"])
    if advantage:
        char["help"] = None
    return advantage


def _public_char(item, *, own=False):
    value = deepcopy(item["value"])
    value["version"] = item["version"]
    if not own:
        for key in ("attempts", "help", "visited", "fictional_hours", "last_long_rest_hour"):
            value.pop(key, None)
    return value


def _history_items(ctx, after=0, limit=50):
    last = ctx.get_state(WORLD_STATE, "chronicle")["last_seq"]
    entries = [ctx.get_state(HISTORY, f"{seq:012d}") for seq in range(after + 1, min(last, after + limit) + 1)]
    return {"items": [entry for entry in entries if entry is not None], "next_after": min(last, after + limit),
            "has_more": after + limit < last, "last_seq": last}


def _exit_available(ctx, char, destination):
    scene_id = char["scene_id"]
    if destination not in ROOMS[scene_id]["exits"]:
        return False
    if {scene_id, destination} == {"entry", "bandit_room"}:
        return not _scene_record(ctx, "entry")["value"]["locked"]
    if {scene_id, destination} == {"gallery", "vault"}:
        return _scene_record(ctx, "gallery")["value"]["gap_secured"]
    if {scene_id, destination} == {"bandit_room", "vault"}:
        room = _scene_record(ctx, "bandit_room")["value"]
        return room["allow_passage"] or all(npc["hp"] == 0 for npc in room["npcs"].values())
    return True


def _combat_turn(combat):
    if not combat or not combat["active"] or not combat["order"]:
        return None
    return combat["order"][combat["index"]]["id"]


def _advance_index(combat):
    combat["index"] += 1
    if combat["index"] >= len(combat["order"]):
        combat["index"] = 0
        combat["round"] += 1


def _combat_end_if_needed(ctx, room, chars):
    combat = room["combat"]
    if not combat or not combat["active"]:
        return
    if all(npc["hp"] <= 0 for npc in room["npcs"].values()):
        combat["active"] = False
        combat["outcome"] = "bandits_defeated"
        room["allow_passage"] = True
    elif not any(role in chars and chars[role]["scene_id"] == "bandit_room" and chars[role]["status"] == "alive"
                 for role in combat["participants"]):
        combat["active"] = False
        combat["outcome"] = "participants_withdrew_or_fell"


def _run_npc_turns(ctx, room, chars):
    """Resolve only NPC/removed-character slots; stop before any user's turn."""
    combat = room["combat"]
    rolls = []
    _combat_end_if_needed(ctx, room, chars)
    if not combat or not combat["active"]:
        return rolls
    for _ in range(len(combat["order"]) + 1):
        turn = _combat_turn(combat)
        if turn in chars:
            char = chars[turn]
            if char["scene_id"] == "bandit_room" and char["status"] not in {"dead", "stable"}:
                # An unconscious participant takes their own death-save turn.
                char["dodging"] = False
                return rolls
            _advance_index(combat)
            continue
        npc = room["npcs"].get(turn)
        if npc is None or npc["hp"] <= 0:
            _advance_index(combat)
            continue
        candidates = [entry["id"] for entry in combat["order"]
                      if entry["id"] in chars and entry["id"] in combat["participants"]
                      and chars[entry["id"]]["scene_id"] == "bandit_room"
                      and chars[entry["id"]]["status"] == "alive"]
        if not candidates:
            combat["active"] = False
            combat["outcome"] = "participants_withdrew_or_fell"
            return rolls
        # Finite transparent strategy: attack the lowest-HP living participant,
        # stable role-ID tie break; no server AI and no attack on bystanders.
        target_id = min(candidates, key=lambda role: (chars[role]["hp"], role))
        target = chars[target_id]
        dice = _roll(ctx, 20, 2 if target["dodging"] else 1)
        natural = min(dice) if target["dodging"] else dice[0]
        hit = natural == 20 or natural != 1 and natural + npc["attack_bonus"] >= target["ac"]
        damage_dice = _roll(ctx, npc["damage_die"], 2 if natural == 20 else 1) if hit else []
        damage = sum(damage_dice) + npc["damage_bonus"] if hit else 0
        _damage(target, damage, critical=natural == 20)
        rolls.append({"attacker": turn, "target": target_id, "rolls": dice, "total": natural + npc["attack_bonus"], "hit": hit,
                      "critical": natural == 20, "damage_rolls": damage_dice, "damage": damage, "target_hp": target["hp"]})
        _advance_index(combat)
        _combat_end_if_needed(ctx, room, chars)
        if not combat["active"]:
            return rolls
    raise RuleViolation("combat has no valid turn holder")


def _combat_chars(ctx, room):
    combat = room["combat"]
    roles = list(combat["participants"]) if combat else []
    values = {}
    records = {}
    for role in roles:
        record = _char_record(ctx, role)
        records[role] = record
        values[role] = record["value"]
    return values, records


def _start_combat(ctx, char, room):
    alive_npcs = [npc_id for npc_id, npc in room["npcs"].items() if npc["hp"] > 0]
    if not alive_npcs:
        raise RuleViolation("there is no living hostile target")
    entries = [{"id": ctx.actor_role_id, "initiative": ctx.random_int(1, 20) + _mod(char["abilities"]["dexterity"]), "kind": "character"}]
    for npc_id in alive_npcs:
        entries.append({"id": npc_id, "initiative": ctx.random_int(1, 20) + room["npcs"][npc_id]["initiative_bonus"], "kind": "npc"})
    entries.sort(key=lambda item: (-item["initiative"], item["id"]))
    room["combat"] = {"active": True, "round": 1, "index": 0, "order": entries,
                      "participants": [ctx.actor_role_id], "enemy_ids": alive_npcs, "outcome": None}


def join(ctx, args):
    name = args["name"].strip()
    if not name:
        raise RuleViolation("enter a nonblank character name")
    record = ctx.get_state_record(CHARACTERS, ctx.actor_role_id)
    if record["exists"]:
        return _outcome(ctx, {"joined": True, "restored": True, "character": _public_char({"value": record["value"], "version": record["version"]}, own=True)})
    if len(_all_characters(ctx)) >= 16:
        raise RuleViolation("this official local adventure admits at most 16 characters")
    preset = deepcopy(CLASS_PRESETS[args["class_id"]])
    value = {"role_id": ctx.actor_role_id, "name": name, "class_id": args["class_id"], "class_name": preset["class_name"],
             "level": 1, "race": "human", "abilities": preset["abilities"], "skills": preset["skills"], "saving_throws": preset["saving_throws"],
             "hp": preset["max_hp"], "max_hp": preset["max_hp"], "ac": preset["ac"], "speed": 30,
             "weapon": preset["weapon"], "equipment": preset["equipment"], "features": preset["features"],
             "hit_die": preset["hit_die"], "hit_dice_remaining": 1, "second_wind_available": args["class_id"] == "fighter",
             "scene_id": "camp", "inventory": [], "visited": ["camp"], "status": "alive", "death_saves": {"successes": 0, "failures": 0},
             "attempts": {}, "help": None, "dodging": False, "fictional_hours": 0, "last_long_rest_hour": None,
             "joined_at": ctx.now}
    version = _save_char(ctx, ctx.actor_role_id, value, 0)
    entry = _record(ctx, "joined", f"{name}以{preset['class_name']}身份来到码头。", {"role_id": ctx.actor_role_id, "class_id": args["class_id"]}, scene_id="camp")
    return _outcome(ctx, {"joined": True, "restored": False, "character": {**value, "version": version}}, entry)


def _available_actions(ctx, char, room):
    actions = []
    def add(action, label, *, target=None, option=None, recipient=None, risk=None):
        args = {"action": action}
        if target is not None:
            args["target"] = target
        if option is not None:
            args["option"] = option
        if recipient is not None:
            args["recipient"] = recipient
        actions.append({"id": action + (":" + target if target else "") + (":" + option if option else ""), "label": label, "args": args, "risk": risk})
    if char["status"] == "dead":
        return actions
    combat = room.get("combat")
    active = combat and combat["active"]
    participating = active and ctx.actor_role_id in combat["participants"]
    if char["status"] == "unconscious":
        if not participating or _combat_turn(combat) == ctx.actor_role_id:
            add("death_save", "进行死亡豁免", risk="三次失败会死亡；骰子结果立即入账。")
        return actions
    if char["status"] == "stable":
        if not participating:
            add("rest", "等待稳定伤者苏醒（1d4故事小时）", option="recover", risk="稳定伤者1d4故事小时后恢复1HP；不是现实时间持续观察。")
        return actions
    if active and not participating:
        add("join_combat", "主动加入战斗", risk="入战后可能被攻击，按独立先攻顺序等待自己的回合。")
    if participating:
        if _combat_turn(combat) != ctx.actor_role_id:
            return actions
        for npc_id, npc in room["npcs"].items():
            if npc["hp"] > 0:
                add("attack", "攻击" + npc["name"], target=npc_id, risk="消耗本回合动作并结束回合；随后可能发生NPC攻击。")
        add("dodge", "采取闪避并结束回合")
        add("end_turn", "跳过本回合")
        add("retreat", "撤回门厅", risk="撤离作为本世界脱离近战的整回合动作；不会额外掷借机攻击。")
    else:
        for destination in ROOMS[char["scene_id"]]["exits"]:
            if _exit_available(ctx, char, destination):
                add("move", "前往" + ROOMS[destination]["title"], target=destination)
        if char["scene_id"] == "store" and not room["searched"]:
            add("search", "搜索储物架与旧账册")
        if char["scene_id"] == "entry" and room["locked"]:
            if char["class_id"] == "rogue":
                add("unlock", "用盗贼工具开锁", risk="敏捷(盗贼工具) DC12；同条件失败不能重新刷骰。")
            add("force", "撞开旧值班室木门", risk="力量(运动) DC15；失败留下警觉声响，同条件不能刷骰。")
        if char["scene_id"] == "gallery" and not room["gap_secured"]:
            if "rope" in char["inventory"]:
                add("set_rope", "用麻绳架设安全通路")
            add("jump", "冒险跳向航标库", target="vault", risk="力量(运动) DC12；失败坠落1d6伤害并留在回廊，同条件不能刷骰。")
        if char["scene_id"] == "vault" and not _scene_record(ctx, "gallery")["value"]["gap_secured"]:
            add("jump", "跳回回廊", target="gallery", risk="力量(运动) DC12；失败坠落1d6伤害并留在航标库。")
        if not active and char["scene_id"] == "bandit_room" and not room["allow_passage"] and any(npc["hp"] > 0 for npc in room["npcs"].values()):
            add("negotiate", "请求拾荒者让路", risk="魅力(游说) DC13；失败不会替他人同意，也不能重复同一说辞刷骰。")
            if "ledger_page" in char["inventory"]:
                add("negotiate", "出示账册澄清误会", option="ledger", risk="账册是新证据：魅力(游说) DC10。成功只获得通行，不强占他人物品。")
            for npc_id, npc in room["npcs"].items():
                if npc["hp"] > 0:
                    add("attack", "主动攻击" + npc["name"], target=npc_id, risk="主动进入真实先攻战斗；若NPC先行动，会先承受其攻击。本请求先建立战斗，随后在自己回合攻击。")
        if room["searched"] or char["scene_id"] == "vault":
            for item_id in room["items"]:
                add("take", "拿取" + ITEMS[item_id]["name"], target=item_id)
        if char["scene_id"] in {"camp", "store"}:
            add("rest", "短休：消耗生命骰／恢复回气", option="short")
            if char["scene_id"] == "camp":
                add("rest", "在营地长休", option="long", risk="叙事休息与等待时间立即写入记录；不表示现实中已过去八小时或一天。")
        if char["scene_id"] == "camp" and "beacon_seal" in char["inventory"]:
            add("deliver", "把航标印交还守灯人", target="beacon_seal")
    if char["class_id"] == "fighter" and char["second_wind_available"]:
        add("heal", "回气：恢复1d10+1 HP", option="second_wind")
    nearby = [item for item in _all_characters(ctx) if item["key"] != ctx.actor_role_id and item["value"]["scene_id"] == char["scene_id"]]
    if not active or participating and _combat_turn(combat) == ctx.actor_role_id:
        for item in nearby:
            other = item["value"]
            if other["status"] == "alive":
                add("help", "协助" + other["name"] + "的下一次行动", recipient=item["key"])
            if other["status"] == "unconscious":
                add("stabilize", "稳定" + other["name"] + "的伤势", recipient=item["key"], risk="感知(医药) DC10；此动作不恢复HP。")
        if "healing_potion" in char["inventory"]:
            add("heal", "饮用治疗药水", option="potion")
            for item in nearby:
                if item["value"]["status"] != "dead" and item["value"]["hp"] < item["value"]["max_hp"]:
                    add("heal", "给" + item["value"]["name"] + "使用治疗药水", option="potion", recipient=item["key"])
    if not active:
        for item in nearby:
            for item_id in char["inventory"]:
                add("transfer", "把" + ITEMS[item_id]["name"] + "交给" + item["value"]["name"], target=item_id, recipient=item["key"])
    return actions


def _description(ctx, char, room):
    scene_id = char["scene_id"]
    description = ROOMS[scene_id]["description"]
    if scene_id == "entry":
        description += " 当前木门仍锁着。" if room["locked"] else " 当前木门已打开。"
    elif scene_id == "gallery":
        description += " 当前麻绳已固定，通往航标库的安全通路向所有人开放。" if room["gap_secured"] else " 当前尚未建立安全通路。"
    elif scene_id == "bandit_room":
        if all(npc["hp"] <= 0 for npc in room["npcs"].values()):
            description = "两名拾荒者已经死亡。通向航标库的路已不再被阻拦。后果不会因新来客进入而重置。"
        elif room["combat"] and room["combat"]["active"]:
            description += " 当前正在进行战斗，参战者按已记录的先攻顺序行动。"
        elif room["allow_passage"]:
            description += " 当前双方已有让路约定，无须再次谈判或攻击。"
    elif scene_id == "vault" and "beacon_seal" not in room["items"]:
        description += " 当前航标印已被取走；这里不会再次生成一份。"
    elif scene_id == "camp" and ctx.get_state(WORLD_STATE, "progress")["seal_returned"]:
        description += " 当前航标印已归还，真实归还者记在世界进展中。"
    return description


def _intent_view(ctx, item):
    intent = {"intent_id": item["key"], **item["value"], "version": item["version"]}
    if not _judge(ctx):
        return intent
    owner_record = _char_record(ctx, intent["owner"])
    owner = owner_record["value"]
    room_record = _scene_record(ctx, owner["scene_id"])
    room = room_record["value"]
    effects = []
    if owner["status"] == "alive" and owner["scene_id"] == intent["scene_id"]:
        for effect in SUCCESS_EFFECTS:
            try:
                _effect_allowed(ctx, owner, effect)
            except RuleViolation:
                continue
            effects.append(effect)
    intent["ruling_context"] = {
        "character": {key: deepcopy(owner[key]) for key in ("role_id", "name", "status", "scene_id", "inventory", "equipment", "abilities", "skills")},
        "scene": {"id": owner["scene_id"], "version": room_record["version"], "description": _description(ctx, owner, room),
                  "locked": room["locked"], "gap_secured": room["gap_secured"], "searched": room["searched"], "allow_passage": room["allow_passage"],
                  "objects": [{"id": item_id, **ITEMS[item_id]} for item_id in room["items"] if room["searched"] or owner["scene_id"] == "vault"],
                  "npcs": [{"id": npc_id, "name": npc["name"], "hp": npc["hp"], "ac": npc["ac"]} for npc_id, npc in room["npcs"].items()],
                  "combat_active": bool(room["combat"] and room["combat"]["active"])},
        "allowed_success_effects": effects,
    }
    intent["ruling_context"]["character"]["version"] = owner_record["version"]
    return intent


def look(ctx, args):
    own = ctx.get_state_record(CHARACTERS, ctx.actor_role_id)
    if not own["exists"]:
        # Refereeing is a configured world authority, not a forced adventurer
        # identity/class. The observer receives public data and pending methods.
        if not _judge(ctx):
            raise RuleViolation("join a character before entering the adventure")
        last = ctx.get_state(WORLD_STATE, "chronicle")["last_seq"]
        bandits = _scene_record(ctx, "bandit_room")["value"]
        combat = deepcopy(bandits["combat"])
        if combat:
            combat["turn"] = _combat_turn(combat)
        intents = [_intent_view(ctx, item)
                   for item in ctx.list_state(INTENTS, limit=200)["items"] if item["value"]["status"] in {"pending", "offered"}]
        return _outcome(ctx, {"world_title": WORLD_TITLE, "character": None,
                             "scene": {"id": "camp", "title": "裁决观察席", "description": "你是明确配置的外部裁决者，可以读取公开进展并对待决方法提出有限条件；你没有冒险角色，不能替玩家行动。", "exits": [], "objects": [], "npcs": [], "version": _scene_record(ctx, "camp")["version"]},
                             "map": [{"id": scene_id, "title": data["title"], "visited": False, "current": False} for scene_id, data in ROOMS.items()],
                             "characters": [_public_char(item) for item in _all_characters(ctx)], "progress": ctx.get_state(WORLD_STATE, "progress"),
                             "combat": combat, "pending_intents": intents, "available_actions": [],
                             "history": _history_items(ctx, max(0, last - 16), 16)["items"],
                             "authority": {"can_rule": True, "epoch": _authority(ctx)["epoch"], "configured_judges": len(_authority(ctx)["judges"])},
                             "rules_version": "SRD 5.1 / official-slice 1", "recovery": {"inference_hosted_here": False}})
    char = own["value"]
    room_record = _scene_record(ctx, char["scene_id"])
    room = room_record["value"]
    combat = deepcopy(room.get("combat"))
    if combat:
        combat["turn"] = _combat_turn(combat)
    objects = [{"id": item_id, **ITEMS[item_id], "available": True} for item_id in room["items"]
               if room["searched"] or char["scene_id"] == "vault"]
    npcs = [{"id": npc_id, "name": npc["name"], "hp": npc["hp"], "max_hp": npc["max_hp"], "ac": npc["ac"], "status": "dead" if npc["hp"] <= 0 else "alive"}
            for npc_id, npc in room["npcs"].items()]
    intents = [_intent_view(ctx, item)
               for item in ctx.list_state(INTENTS, limit=200)["items"]
               if item["value"]["status"] in {"pending", "offered"}
               and (item["value"]["owner"] == ctx.actor_role_id or _judge(ctx) or item["value"]["scene_id"] == char["scene_id"])]
    progress = ctx.get_state(WORLD_STATE, "progress")
    last = ctx.get_state(WORLD_STATE, "chronicle")["last_seq"]
    characters = [_public_char(item, own=item["key"] == ctx.actor_role_id) for item in _all_characters(ctx)]
    return _outcome(ctx, {"world_title": WORLD_TITLE, "character": _public_char({"value": char, "version": own["version"]}, own=True),
                         "scene": {"id": char["scene_id"], "title": ROOMS[char["scene_id"]]["title"], "description": _description(ctx, char, room),
                                   "exits": [{"id": destination, "label": ROOMS[destination]["title"], "available": _exit_available(ctx, char, destination)} for destination in ROOMS[char["scene_id"]]["exits"]],
                                   "objects": objects, "npcs": npcs, "version": room_record["version"], "searched": room["searched"],
                                   "locked": room["locked"], "gap_secured": room["gap_secured"], "allow_passage": room["allow_passage"], "alert": room["alert"]},
                         "map": [{"id": scene_id, "title": data["title"], "visited": scene_id in char["visited"], "current": scene_id == char["scene_id"]} for scene_id, data in ROOMS.items()],
                         "characters": characters, "progress": progress, "combat": combat, "pending_intents": intents,
                         "available_actions": _available_actions(ctx, char, room), "history": _history_items(ctx, max(0, last - 16), 16)["items"],
                         "authority": {"can_rule": _judge(ctx), "epoch": _authority(ctx)["epoch"], "configured_judges": len(_authority(ctx)["judges"])},
                         "rules_version": "SRD 5.1 / official-slice 1", "recovery": {"credential_identity": "Your existing signing key and local Agent context continue this same character.", "inference_hosted_here": False}})


def handbook(ctx, args):
    if args["chapter"] == "gm" and not _judge(ctx):
        raise PermissionDenied("the referee chapter is available only to explicitly configured referee identities")
    from .dnd_handbook import CHAPTERS, HANDBOOK_REVISION, HANDBOOK_VERSION
    return _outcome(ctx, {"chapter": args["chapter"], "text": CHAPTERS[args["chapter"]], "version": HANDBOOK_REVISION,
                         "handbook_id": HANDBOOK_VERSION,
                         "rules": "2014 fifth edition SRD 5.1", "chapters": [key for key in CHAPTER_IDS if key != "gm" or _judge(ctx)]})


def history(ctx, args):
    return _outcome(ctx, _history_items(ctx, args.get("after", 0), args.get("limit", 50)))


def say(ctx, args):
    text = args["text"].strip()
    if not text:
        raise RuleViolation("message must not be blank")
    char = _char_record(ctx)["value"]
    entry = _record(ctx, "said", f"{char['name']}：{text}", {"text": text, "name": char["name"]}, scene_id=char["scene_id"])
    return _outcome(ctx, {"said": True, "entry": entry}, entry)


def _npc_attack(ctx, char, room, target_id):
    target = room["npcs"].get(target_id)
    if target is None or target["hp"] <= 0:
        raise RuleViolation("choose a living NPC in this room")
    helper = char.get("help")
    advantage = bool(helper and helper["scene_id"] == char["scene_id"])
    if advantage:
        char["help"] = None
    dice = _roll(ctx, 20, 2 if advantage else 1)
    natural = max(dice) if advantage else dice[0]
    weapon = char["weapon"]
    hit = natural == 20 or natural != 1 and natural + weapon["attack_bonus"] >= target["ac"]
    damage_dice = _roll(ctx, weapon["damage_die"], 2 if natural == 20 else 1) if hit else []
    damage = sum(damage_dice) + weapon["damage_bonus"] if hit else 0
    allies = []
    if room["combat"]:
        for role in room["combat"]["participants"]:
            if role == ctx.actor_role_id:
                continue
            ally = _char_record(ctx, role)["value"]
            if ally["scene_id"] == "bandit_room" and ally["status"] == "alive":
                allies.append(role)
    sneak = char["class_id"] == "rogue" and hit and (advantage or bool(allies))
    sneak_dice = _roll(ctx, 6, 2 if natural == 20 else 1) if sneak else []
    damage += sum(sneak_dice)
    target["hp"] = max(0, target["hp"] - damage)
    return {"attacker": ctx.actor_role_id, "target": target_id, "rolls": dice, "natural": natural,
            "total": natural + weapon["attack_bonus"], "hit": hit, "critical": natural == 20, "advantage": advantage,
            "damage_rolls": damage_dice, "sneak_attack_rolls": sneak_dice, "damage": damage, "target_hp": target["hp"]}


def _death_save(ctx, char):
    if char["status"] != "unconscious":
        raise RuleViolation("only an unconscious unstable character makes a death saving throw")
    natural = ctx.random_int(1, 20)
    if natural == 20:
        _heal(char, 1)
    else:
        if natural == 1:
            char["death_saves"]["failures"] += 2
        elif natural >= 10:
            char["death_saves"]["successes"] += 1
        else:
            char["death_saves"]["failures"] += 1
        if char["death_saves"]["failures"] >= 3:
            char["status"] = "dead"
        elif char["death_saves"]["successes"] >= 3:
            char["status"] = "stable"
            char["death_saves"] = {"successes": 0, "failures": 0}
    return {"natural": natural, "status": char["status"], "hp": char["hp"], "death_saves": deepcopy(char["death_saves"])}


def act(ctx, args):
    action = args["action"]
    own_record = _char_record(ctx)
    char = own_record["value"]
    original_char = deepcopy(char)
    scene_id = char["scene_id"]
    room_record = _scene_record(ctx, scene_id)
    room = room_record["value"]
    original_room = deepcopy(room)
    _check_version(own_record, args.get("expected_character_version"), "character")
    _check_version(room_record, args.get("expected_scene_version"), "scene")
    target = args.get("target")
    recipient = args.get("recipient")
    option = args.get("option")
    result = {"action": action, "actor": ctx.actor_role_id, "scene_id": scene_id}
    text = ""
    other_changes = {}
    combat = room.get("combat")
    participating = bool(combat and combat["active"] and ctx.actor_role_id in combat["participants"])
    combat_action = bool(participating and not (action == "heal" and option == "second_wind"))
    if participating and _combat_turn(combat) != ctx.actor_role_id:
        raise RuleViolation("it is another participant's combat turn; do not act for them")
    if participating and action not in {"attack", "dodge", "help", "end_turn", "retreat", "heal", "stabilize", "death_save"}:
        raise RuleViolation("this action is not available during combat; finish or retreat from the current turn")
    if combat and combat["active"] and not participating and action in {"help", "stabilize"}:
        raise RuleViolation("join_combat before providing a combat action; spectators cannot contribute unlimited free turns")
    if combat and combat["active"] and not participating and action == "heal" and option == "potion" and recipient in combat["participants"]:
        raise RuleViolation("join_combat before giving a potion to an active combat participant")
    recovering_stable = action == "rest" and option == "recover" and char["status"] == "stable"
    if action != "death_save" and not recovering_stable:
        _alive(char)

    if action == "move":
        if not target or not _exit_available(ctx, char, target):
            raise RuleViolation("no currently available direct passage to that destination; inspect the obstacle and available actions")
        char["scene_id"] = target
        if target not in char["visited"]:
            char["visited"].append(target)
        char["help"] = None
        text = f"{char['name']}从{ROOMS[scene_id]['title']}来到{ROOMS[target]['title']}。"
        result["destination"] = target

    elif action == "search":
        if scene_id != "store":
            raise RuleViolation("search is implemented for the storage room; look already describes the other public rooms")
        if room["searched"]:
            return _outcome(ctx, {**result, "changed": False, "message": "储物间已搜索；没有生成新的物品。", "objects": [{"id": key, **ITEMS[key]} for key in room["items"]]})
        room["searched"] = True
        result["objects"] = [{"id": key, **ITEMS[key]} for key in room["items"]]
        text = f"{char['name']}搜索储物间，确认了麻绳、药水和旧账册的实际存量。"

    elif action in {"unlock", "force"}:
        if scene_id != "entry" or not room["locked"]:
            raise RuleViolation("the locked bandit-room door is not an unresolved obstacle here")
        if action == "unlock" and char["class_id"] != "rogue":
            raise RuleViolation("this character does not have the equipped thieves' tools proficiency; use another supported method")
        advantage = _attempt(ctx, char, "door:" + action)
        check = _check(ctx, char, "dexterity" if action == "unlock" else "strength", 12 if action == "unlock" else 15,
                       skill="thieves_tools" if action == "unlock" else "athletics", advantage=advantage)
        result["check"] = check
        if check["success"]:
            room["locked"] = False
            text = f"{char['name']}打开了旧值班室的木门。"
        else:
            if action == "force":
                room["alert"] = True
            text = f"{char['name']}的{'开锁' if action == 'unlock' else '撞门'}尝试失败，同条件不能反复刷新骰子。"

    elif action == "set_rope":
        if scene_id != "gallery" or room["gap_secured"]:
            raise RuleViolation("there is no unsecured gallery gap here")
        if "rope" not in char["inventory"]:
            raise RuleViolation("take the actual rope from the store, or receive it from its current carrier")
        char["inventory"].remove("rope")
        room["gap_secured"] = True
        text = f"{char['name']}把唯一的麻绳固定在缺口上，建立了所有来客均可使用的安全通路。"
        result["consumed_item"] = "rope"

    elif action == "jump":
        if {scene_id, target} != {"gallery", "vault"} or _scene_record(ctx, "gallery")["value"]["gap_secured"]:
            raise RuleViolation("jump is only the unresolved gallery/vault gap; use move once a safe route exists")
        advantage = _attempt(ctx, char, "jump:" + scene_id + ":" + target)
        check = _check(ctx, char, "strength", 12, skill="athletics", advantage=advantage)
        result["check"] = check
        if check["success"]:
            char["attempts"].pop("jump:" + scene_id + ":" + target, None)
            char["scene_id"] = target
            if target not in char["visited"]:
                char["visited"].append(target)
            char["help"] = None
            text = f"{char['name']}成功跃过回廊缺口，来到{ROOMS[target]['title']}。"
        else:
            damage_rolls = _roll(ctx, 6)
            _damage(char, sum(damage_rolls))
            result.update(damage_rolls=damage_rolls, damage=sum(damage_rolls), hp=char["hp"])
            text = f"{char['name']}跃过缺口失败，坠落受到{sum(damage_rolls)}点伤害，仍在{ROOMS[scene_id]['title']}。"

    elif action == "negotiate":
        if scene_id != "bandit_room" or room["allow_passage"] or all(npc["hp"] <= 0 for npc in room["npcs"].values()):
            raise RuleViolation("there is no unresolved living bandit negotiation here")
        if room["combat"] and room["combat"]["active"]:
            raise RuleViolation("the current fight must end before negotiation")
        use_ledger = option == "ledger"
        if option not in {None, "ledger"}:
            raise RuleViolation("unsupported negotiation option")
        if use_ledger and "ledger_page" not in char["inventory"]:
            raise RuleViolation("you must possess the actual ledger page to offer it as evidence")
        advantage = _attempt(ctx, char, "negotiate", evidence="ledger" if use_ledger else "ordinary")
        check = _check(ctx, char, "charisma", 10 if use_ledger else 13, skill="persuasion", advantage=advantage)
        result["check"] = check
        if check["success"]:
            room["allow_passage"] = True
            text = f"{char['name']}与拾荒者达成让路约定；他们仍保有自己的物品与选择。"
        else:
            text = f"{char['name']}的谈判未获同意；拾荒者继续阻拦通路，同一说辞不能重刷。"

    elif action == "take":
        if target not in ITEMS or target not in room["items"] or not (room["searched"] or scene_id == "vault"):
            raise RuleViolation("the requested item is not visibly present in this room")
        room["items"].remove(target)
        char["inventory"].append(target)
        if target == "beacon_seal":
            progress_record = ctx.get_state_record(WORLD_STATE, "progress")
            progress = progress_record["value"]
            progress["seal_holder"] = ctx.actor_role_id
            ctx.set_state(WORLD_STATE, "progress", progress, expected_version=progress_record["version"])
        text = f"{char['name']}取走了{ITEMS[target]['name']}；原处不再有第二份。"
        result["item"] = target

    elif action == "transfer":
        if target not in char["inventory"] or recipient is None or recipient == ctx.actor_role_id:
            raise RuleViolation("transfer requires an actually held item and another nearby character")
        record = _char_record(ctx, recipient)
        other = record["value"]
        if other["scene_id"] != scene_id or other["status"] == "dead":
            raise RuleViolation("the recipient must be present and not dead")
        if combat and combat["active"]:
            raise RuleViolation("inventory transfers are not an implemented combat action")
        char["inventory"].remove(target)
        other["inventory"].append(target)
        other_changes[recipient] = (other, record["version"])
        if target == "beacon_seal":
            progress_record = ctx.get_state_record(WORLD_STATE, "progress")
            progress = progress_record["value"]
            progress["seal_holder"] = recipient
            ctx.set_state(WORLD_STATE, "progress", progress, expected_version=progress_record["version"])
        text = f"{char['name']}把{ITEMS[target]['name']}交给{other['name']}；没有替对方许下任务承诺。"
        result.update(item=target, recipient=recipient)

    elif action == "deliver":
        if scene_id != "camp" or target not in {None, "beacon_seal"} or "beacon_seal" not in char["inventory"]:
            raise RuleViolation("deliver the unique beacon seal while you carry it in camp")
        progress_record = ctx.get_state_record(WORLD_STATE, "progress")
        progress = progress_record["value"]
        if progress["seal_returned"]:
            raise RuleViolation("the seal has already been returned; the reward cannot be duplicated")
        char["inventory"].remove("beacon_seal")
        progress.update(seal_returned=True, seal_holder=None, returned_by=ctx.actor_role_id, returned_at=ctx.now)
        ctx.set_state(WORLD_STATE, "progress", progress, expected_version=progress_record["version"])
        text = f"{char['name']}将唯一航标印交还守灯人。港口记下了这次归还；世界继续存在。"
        result["progress"] = progress

    elif action == "rest":
        if option == "recover":
            if char["status"] != "stable" or room.get("combat") and room["combat"]["active"]:
                raise RuleViolation("only a stabilized noncombat character can wait to regain consciousness")
            hours = ctx.random_int(1, 4)
            _heal(char, 1)
            char["fictional_hours"] += hours
            result.update(fictional_hours=hours, real_time_elapsed=False, hp=char["hp"])
            text = f"{char['name']}稳定后等待{hours}个故事小时，恢复意识和1点HP。这不是现实跨日观察。"
        elif scene_id not in {"camp", "store"}:
            raise RuleViolation("safe rests are supported in camp and the store only")
        kind = option or "short"
        if kind == "recover":
            pass
        elif kind == "short":
            rolls = _roll(ctx, char["hit_die"]) if char["hp"] < char["max_hp"] and char["hit_dice_remaining"] > 0 else []
            if rolls:
                char["hit_dice_remaining"] -= 1
                _heal(char, max(0, sum(rolls) + _mod(char["abilities"]["constitution"])))
            char["second_wind_available"] = char["class_id"] == "fighter"
            char["fictional_hours"] += 1
            result.update(healing_rolls=rolls, fictional_hours=1, real_time_elapsed=False)
            text = f"{char['name']}完成一次叙事短休（故事中一小时），恢复{sum(rolls) + 2 if rolls else 0}点HP并刷新可短休恢复的能力。"
        elif kind == "long" and scene_id == "camp":
            now_hour = char["fictional_hours"]
            last_hour = char["last_long_rest_hour"]
            wait_hours = 0 if last_hour is None else max(0, last_hour + 24 - now_hour)
            char["fictional_hours"] += wait_hours + 8
            char["last_long_rest_hour"] = now_hour + wait_hours
            char["hp"] = char["max_hp"]
            char["hit_dice_remaining"] = 1
            char["second_wind_available"] = char["class_id"] == "fighter"
            result.update(fictional_hours=wait_hours + 8, waiting_hours=wait_hours, real_time_elapsed=False)
            text = f"{char['name']}在营地完成八小时叙事长休" + (f"（先等待{wait_hours}个故事小时以满足每日休息限制）" if wait_hours else "") + "，恢复HP与一级角色的一枚生命骰。这不是跨日实测。"
        else:
            raise RuleViolation("use rest option short, or long while in camp")

    elif action == "heal":
        if option == "second_wind":
            if char["class_id"] != "fighter" or not char["second_wind_available"] or recipient not in {None, ctx.actor_role_id}:
                raise RuleViolation("Second Wind belongs to its fighter and is unavailable until a rest restores it")
            rolls = _roll(ctx, 10)
            char["second_wind_available"] = False
            _heal(char, sum(rolls) + 1)
            result.update(healing_rolls=rolls, healing=sum(rolls) + 1, hp=char["hp"], ends_turn=False)
            combat_action = False
            text = f"{char['name']}使用回气，恢复至{char['hp']}点HP。"
        elif option == "potion":
            if "healing_potion" not in char["inventory"]:
                raise RuleViolation("you do not carry the actual healing potion")
            if recipient is None or recipient == ctx.actor_role_id:
                other = char
                record = own_record
            else:
                record = _char_record(ctx, recipient)
                other = record["value"]
                if other["scene_id"] != scene_id:
                    raise RuleViolation("the potion recipient must be in this room")
            if other["status"] == "dead":
                raise RuleViolation("a healing potion cannot revive the dead")
            rolls = _roll(ctx, 4, 2)
            char["inventory"].remove("healing_potion")
            _heal(other, sum(rolls) + 2)
            if other is not char:
                other_changes[recipient] = (other, record["version"])
            result.update(healing_rolls=rolls, healing=sum(rolls) + 2, recipient=other["role_id"], hp=other["hp"])
            text = f"{char['name']}消耗唯一药水，使{other['name']}恢复至{other['hp']}点HP。"
        else:
            raise RuleViolation("choose heal option second_wind or potion")

    elif action == "help":
        if recipient is None or recipient == ctx.actor_role_id:
            raise RuleViolation("Help requires a different willing nearby actor's next check; you cannot Help yourself")
        record = _char_record(ctx, recipient)
        other = record["value"]
        if other["scene_id"] != scene_id or other["status"] != "alive":
            raise RuleViolation("the assisted character must be present and conscious")
        if participating and recipient not in combat["participants"]:
            raise RuleViolation("combat Help requires a voluntarily enrolled participant")
        other["help"] = {"by": ctx.actor_role_id, "scene_id": scene_id, "operation_id": ctx.operation_id}
        other_changes[recipient] = (other, record["version"])
        text = f"{char['name']}协助{other['name']}，其下一次本房间检定或攻击可获得优势。"
        result["recipient"] = recipient

    elif action == "stabilize":
        if recipient is None or recipient == ctx.actor_role_id:
            raise RuleViolation("stabilize requires another unconscious character")
        record = _char_record(ctx, recipient)
        other = record["value"]
        if other["scene_id"] != scene_id or other["status"] != "unconscious":
            raise RuleViolation("the recipient must be an unconscious unstable character in this room")
        advantage = _attempt(ctx, char, "stabilize:" + recipient, evidence=str(record["version"]))
        check = _check(ctx, char, "wisdom", 10, skill="medicine", advantage=advantage)
        if check["success"]:
            other["status"] = "stable"
            other["death_saves"] = {"successes": 0, "failures": 0}
            other_changes[recipient] = (other, record["version"])
        result["check"] = check
        text = f"{char['name']}的急救{'成功稳定了' if check['success'] else '未能稳定'}{other['name']}；没有恢复生命值。"

    elif action == "death_save":
        result["death_save"] = _death_save(ctx, char)
        text = f"{char['name']}进行死亡豁免，掷出{result['death_save']['natural']}，目前状态：{char['status']}。"

    elif action in {"attack", "join_combat", "dodge", "end_turn", "retreat"}:
        if scene_id != "bandit_room":
            raise RuleViolation("this six-room slice has combat only with the bandits; it does not implement PvP")
        if action == "attack" and target not in room["npcs"]:
            raise RuleViolation("attack a named living bandit; attacking other users is outside this world's contract")
        if action in {"attack", "join_combat"} and not (room["combat"] and room["combat"]["active"]):
            _start_combat(ctx, char, room)
            chars = {ctx.actor_role_id: char}
            result["initiative"] = deepcopy(room["combat"]["order"])
            result["npc_turns"] = _run_npc_turns(ctx, room, chars)
            # Starting a fight is a commitment, not an attack secretly outside
            # initiative. A second request performs the attack on a legal turn.
            text = f"{char['name']}主动发起战斗，先攻顺序已固定。"
            result["combat_started"] = True
            combat_action = False
        elif action in {"attack", "join_combat"} and ctx.actor_role_id not in room["combat"]["participants"]:
            if action != "join_combat":
                raise RuleViolation("join_combat explicitly before attacking in an existing fight")
            initiative = ctx.random_int(1, 20) + _mod(char["abilities"]["dexterity"])
            current_turn = _combat_turn(room["combat"])
            room["combat"]["order"] = [entry for entry in room["combat"]["order"] if entry["id"] != ctx.actor_role_id]
            room["combat"]["index"] = next(index for index, entry in enumerate(room["combat"]["order"]) if entry["id"] == current_turn)
            room["combat"]["participants"].append(ctx.actor_role_id)
            # Mid-round entrants are appended; initiative is recorded and the
            # complete order is sorted once the next round begins (see below).
            room["combat"]["order"].append({"id": ctx.actor_role_id, "initiative": initiative, "kind": "character"})
            result["initiative"] = initiative
            text = f"{char['name']}自愿加入战斗，将在本轮剩余参与者之后取得第一个回合。"
            combat_action = False
        elif action == "join_combat":
            raise RuleViolation("you are already participating")
        elif not participating:
            raise RuleViolation("there is no active combat turn for this character")
        elif action == "attack":
            result["attack"] = _npc_attack(ctx, char, room, target)
            hit = result["attack"]["hit"]
            text = f"{char['name']}攻击{room['npcs'][target]['name']}，" + (f"造成{result['attack']['damage']}点伤害。" if hit else "未命中。")
        elif action == "dodge":
            char["dodging"] = True
            text = f"{char['name']}采取闪避直到自己的下一个回合。"
        elif action == "end_turn":
            text = f"{char['name']}结束自己的回合。"
        elif action == "retreat":
            char["scene_id"] = "entry"
            char["help"] = None
            room["combat"]["participants"].remove(ctx.actor_role_id)
            text = f"{char['name']}以整回合脱离战斗，退回门厅。"

    else:
        raise RuleViolation("unsupported action; read dnd.handbook(actions) or submit an explicitly pending proposal")

    # Finite NPC reactions follow *completed* player turns. No request may
    # manufacture another human player's move or skip their decision.
    if participating and combat_action:
        values, records = _combat_chars(ctx, room)
        snapshots = {role: {**record, "value": deepcopy(record["value"])} for role, record in records.items()}
        values[ctx.actor_role_id] = char
        for role, (other, version) in other_changes.items():
            if role in values:
                values[role] = other
        _advance_index(room["combat"])
        # A new round establishes initiative for newly enrolled entrants.
        if room["combat"]["index"] == 0:
            room["combat"]["order"].sort(key=lambda item: (-item["initiative"], item["id"]))
        result["npc_turns"] = _run_npc_turns(ctx, room, values)
        for role, value in values.items():
            if role != ctx.actor_role_id and value != snapshots[role]["value"]:
                other_changes[role] = (value, snapshots[role]["version"])
        result["ends_turn"] = True

    # A quotation about this room must expire when its actual conditions change.
    # Another person's movement or rest changes their character, not the door,
    # objects or combat. The owner's version separately protects their position,
    # inventory, Help and injury; public history still records every action.
    if room != original_room:
        result["scene_version"] = _save_scene(ctx, scene_id, room, room_record["version"])
    else:
        result["scene_version"] = room_record["version"]
    if char != original_char:
        result["character_version"] = _save_char(ctx, ctx.actor_role_id, char, own_record["version"])
    else:
        result["character_version"] = own_record["version"]
    for role, (other, version) in other_changes.items():
        _save_char(ctx, role, other, version)
    result.update(message=text, hp=char["hp"], status=char["status"], inventory=list(char["inventory"]), destination=char["scene_id"])
    if room.get("combat"):
        result["combat"] = {**deepcopy(room["combat"]), "turn": _combat_turn(room["combat"])}
    entry = _record(ctx, "acted", text, result, scene_id=scene_id)
    result["seq"] = entry["seq"]
    return _outcome(ctx, result, entry)


def propose(ctx, args):
    record = _char_record(ctx)
    char = record["value"]
    _alive(char)
    if _in_combat(ctx, char):
        raise RuleViolation("novel actions are adjudicated outside combat in this slice; use its published combat actions")
    scene = _scene_record(ctx, char["scene_id"])
    _check_version(record, args["expected_character_version"], "character")
    _check_version(scene, args["expected_scene_version"], "scene")
    text = args["text"].strip()
    if not text:
        raise RuleViolation("describe your intended method")
    if ctx.get_state(INTENTS, args["intent_id"]) is not None:
        raise RuleViolation("intent_id already exists; reuse its original operation_id for a transport retry")
    intents = ctx.list_state(INTENTS, limit=200)["items"]
    if len(intents) >= 160:
        raise RuleViolation("this finite trial allows at most 160 recorded free-form intentions")
    if any(item["value"]["owner"] == ctx.actor_role_id and item["value"]["status"] in {"pending", "offered"} for item in intents):
        raise RuleViolation("finish or withdraw your existing pending proposal first")
    value = {"owner": ctx.actor_role_id, "text": text, "scene_id": char["scene_id"], "status": "pending", "created_at": ctx.now,
             "character_version": record["version"], "scene_version": scene["version"], "ruling": None, "result": None}
    version = ctx.set_state(INTENTS, args["intent_id"], value, expected_version=0)
    entry = _record(ctx, "proposed", f"{char['name']}提出待裁决方法：{text}", {"intent_id": args["intent_id"]}, scene_id=char["scene_id"])
    return _outcome(ctx, {"intent_id": args["intent_id"], "status": "pending", "version": version,
                          "message": "已记录提议，尚未产生世界效果。既有行动仍可直接执行。", "judge_available": bool(_authority(ctx)["judges"])}, entry)


def _intent_record(ctx, intent_id):
    record = ctx.get_state_record(INTENTS, intent_id)
    if not record["exists"]:
        raise RuleViolation("intent does not exist")
    return record


def _effect_allowed(ctx, char, effect):
    if effect == "none":
        return
    if effect == "open_bandit_door":
        if char["scene_id"] != "entry" or not _scene_record(ctx, "entry")["value"]["locked"]:
            raise RuleViolation("open_bandit_door requires the unresolved door at entry")
    elif effect == "secure_gap":
        if char["scene_id"] != "gallery" or _scene_record(ctx, "gallery")["value"]["gap_secured"] or "rope" not in char["inventory"]:
            raise RuleViolation("secure_gap requires the actual carried rope and unresolved gallery gap; prose cannot invent materials")
    elif effect == "bandits_allow_passage":
        room = _scene_record(ctx, "bandit_room")["value"]
        if char["scene_id"] != "bandit_room" or room["allow_passage"] or room["combat"] and room["combat"]["active"] or all(npc["hp"] <= 0 for npc in room["npcs"].values()):
            raise RuleViolation("bandits_allow_passage requires living bandits in a noncombat unresolved negotiation")
    elif effect == "reveal_store":
        if char["scene_id"] != "store" or _scene_record(ctx, "store")["value"]["searched"]:
            raise RuleViolation("reveal_store requires the not-yet-searched store")
    else:
        raise RuleViolation("unsupported finite effect")


def rule(ctx, args):
    record = _intent_record(ctx, args["intent_id"])
    intent = record["value"]
    if intent["status"] != "pending":
        raise RuleViolation("a quotation cannot be replaced after it is offered; the owner must withdraw and make a fresh proposal")
    char_record = _char_record(ctx, intent["owner"])
    char = char_record["value"]
    _alive(char)
    if char["scene_id"] != intent["scene_id"] or _scene_combat(ctx, char["scene_id"]) and _scene_combat(ctx, char["scene_id"])["active"]:
        raise StateConflict("the proposer moved or combat began; ask them for a new proposal")
    _effect_allowed(ctx, char, args["success_effect"])
    scene = _scene_record(ctx, char["scene_id"])
    reason = args["reason"].strip()
    risk = args["risk"].strip()
    if not reason or not risk:
        raise RuleViolation("offer an explicit public reason and risk before any roll")
    authority = _authority(ctx)
    intent["ruling"] = {"judge": ctx.actor_role_id, "authority_epoch": authority["epoch"], "ability": args["ability"], "dc": args["dc"],
                        "advantage": args.get("advantage", False), "success_effect": args["success_effect"], "failure_effect": args["failure_effect"],
                        "reason": reason, "risk": risk, "character_version": char_record["version"], "scene_version": scene["version"], "offered_at": ctx.now}
    effect_text = {
        "open_bandit_door": "成功后木门永久打开；不移动或支配其他玩家。",
        "secure_gap": "成功后把背包中的唯一麻绳转为公共固定设施；背包减少一根绳，所有人得到安全通路。",
        "bandits_allow_passage": "成功后拾荒者允许通行；不转移他们的财产，不替其他玩家作承诺。",
        "reveal_store": "成功后公开储物间已有物品；不创造任何新物资。",
        "none": "成功后仅记录这次检定；不产生额外物理效果。",
    }
    failure_text = "失败后承受服务器掷出的1d6伤害，可能降至0HP；无权事后撤回。" if args["failure_effect"] == "damage_1d6" else "失败后不额外损失HP或物品，但同方法同条件不能重复掷骰。"
    # These authoritative mechanical terms cannot be overridden by a model's
    # prose risk explanation. They are returned *before* the owner commits.
    intent["ruling"]["terms"] = {
        "success": {"effect": args["success_effect"], "description": effect_text[args["success_effect"]],
                    "cost_items": ["rope"] if args["success_effect"] == "secure_gap" else []},
        "failure": {"effect": args["failure_effect"], "description": failure_text,
                    "damage": "1d6" if args["failure_effect"] == "damage_1d6" else None},
        "check": {"ability": args["ability"], "modifier": _mod(char["abilities"][args["ability"]]), "dc": args["dc"],
                  "advantage": bool(args.get("advantage", False) or char.get("help") and char["help"]["scene_id"] == char["scene_id"])},
        "owner_must_commit": True,
    }
    intent["ruling"]["server_terms"] = effect_text[args["success_effect"]] + " " + failure_text
    intent["status"] = "offered"
    version = ctx.set_state(INTENTS, args["intent_id"], intent, expected_version=record["version"])
    entry = _record(ctx, "ruled", f"裁决者提出条件：{reason} 风险：{risk}；等待原提议者承诺，尚未掷骰。", {"intent_id": args["intent_id"], "ruling": intent["ruling"]}, scene_id=intent["scene_id"])
    return _outcome(ctx, {"intent_id": args["intent_id"], "status": "offered", "version": version, "ruling": intent["ruling"]}, entry)


def commit(ctx, args):
    record = _intent_record(ctx, args["intent_id"])
    intent = record["value"]
    if intent["owner"] != ctx.actor_role_id:
        raise PermissionDenied("only the proposer can commit their disclosed risk")
    if intent["status"] != "offered":
        raise RuleViolation("only an offered, uncommitted proposal can be committed")
    ruling = intent["ruling"]
    authority = _authority(ctx)
    if ruling["judge"] not in authority["judges"] or ruling["authority_epoch"] != authority["epoch"] or ctx.get_role(ruling["judge"])["status"] != "active":
        raise StateConflict("referee authorization changed; withdraw and request fresh terms")
    char_record = _char_record(ctx)
    char = char_record["value"]
    _alive(char)
    scene_record = _scene_record(ctx, char["scene_id"])
    room = scene_record["value"]
    if char["scene_id"] != intent["scene_id"]:
        raise StateConflict("character left the quoted scene")
    _check_version(char_record, ruling["character_version"], "quoted character")
    _check_version(scene_record, ruling["scene_version"], "quoted scene")
    _effect_allowed(ctx, char, ruling["success_effect"])
    # Rephrasing prose or lowering DC does not create a new mechanical method.
    # The limited adapter distinguishes the declared ability/effect, not words.
    attempt_key = "novel:" + intent["scene_id"] + ":" + ruling["success_effect"] + ":" + ruling["ability"]
    evidence = "ordinary"
    if ruling["success_effect"] == "open_bandit_door" and ruling["ability"] in {"strength", "dexterity"}:
        attempt_key = "door:" + ("force" if ruling["ability"] == "strength" else "unlock")
    elif ruling["success_effect"] == "bandits_allow_passage" and ruling["ability"] == "charisma":
        attempt_key = "negotiate"
        evidence = "ledger" if "ledger_page" in char["inventory"] else "ordinary"
    advantage = _attempt(ctx, char, attempt_key, evidence=evidence)
    check = _check(ctx, char, ruling["ability"], ruling["dc"], advantage=advantage or ruling["advantage"])
    if check["success"]:
        char["attempts"].pop(attempt_key, None)
    effect = ruling["success_effect"] if check["success"] else ruling["failure_effect"]
    damage_rolls = []
    if effect == "open_bandit_door":
        room["locked"] = False
    elif effect == "secure_gap":
        char["inventory"].remove("rope")
        room["gap_secured"] = True
    elif effect == "bandits_allow_passage":
        room["allow_passage"] = True
    elif effect == "reveal_store":
        room["searched"] = True
    elif effect == "damage_1d6":
        damage_rolls = _roll(ctx, 6)
        _damage(char, sum(damage_rolls))
    char_version = _save_char(ctx, ctx.actor_role_id, char, char_record["version"])
    scene_version = _save_scene(ctx, char["scene_id"], room, scene_record["version"])
    intent["status"] = "executed"
    intent["result"] = {"check": check, "effect": effect, "damage_rolls": damage_rolls, "hp": char["hp"], "committed_at": ctx.now}
    version = ctx.set_state(INTENTS, args["intent_id"], intent, expected_version=record["version"])
    text = f"{char['name']}承诺先前公开风险后执行方法，检定{'成功' if check['success'] else '失败'}，已结算效果：{effect}。"
    entry = _record(ctx, "committed", text, {"intent_id": args["intent_id"], **intent["result"]}, scene_id=char["scene_id"])
    return _outcome(ctx, {"intent_id": args["intent_id"], "status": "executed", "version": version,
                          "character_version": char_version, "scene_version": scene_version, **intent["result"]}, entry)


def withdraw(ctx, args):
    record = _intent_record(ctx, args["intent_id"])
    intent = record["value"]
    if intent["owner"] != ctx.actor_role_id:
        raise PermissionDenied("only the original proposer can withdraw")
    if intent["status"] not in {"pending", "offered"}:
        raise RuleViolation("a committed outcome cannot be withdrawn after seeing its roll")
    intent["status"] = "withdrawn"
    version = ctx.set_state(INTENTS, args["intent_id"], intent, expected_version=record["version"])
    entry = _record(ctx, "withdrawn", "提议者在承诺风险前撤回了方法。", {"intent_id": args["intent_id"]}, scene_id=intent["scene_id"])
    return _outcome(ctx, {"intent_id": args["intent_id"], "status": "withdrawn", "version": version}, entry)


def bootstrap(ctx):
    own = ctx.get_state_record(CHARACTERS, ctx.actor_role_id)
    return {"joined": own["exists"], "next": "dnd.look" if own["exists"] or _judge(ctx) else "dnd.join", "can_rule": _judge(ctx),
            "guide": "dnd.handbook", "rules_version": "SRD 5.1 / official-slice 1", "inference_hosted_here": False}


def make_world(*, judge_role_ids=()):
    judges = tuple(judge_role_ids)
    if len(set(judges)) != len(judges) or len(judges) > 8:
        raise ValueError("configure at most eight distinct referee identities")

    def initialize(ctx):
        for role_id in judges:
            if ctx.get_role(role_id)["status"] != "active":
                raise RuleViolation("configured referees must already be active world-local identities")
        ctx.set_state(WORLD_STATE, "authority", {"judges": list(judges), "epoch": 1}, expected_version=0)
        ctx.set_state(WORLD_STATE, "chronicle", {"last_seq": 0}, expected_version=0)
        ctx.set_state(WORLD_STATE, "progress", {"seal_returned": False, "seal_holder": None, "returned_by": None, "returned_at": None}, expected_version=0)
        for scene_id in ROOMS:
            items = ["rope", "healing_potion", "ledger_page"] if scene_id == "store" else ["beacon_seal"] if scene_id == "vault" else []
            room = {"id": scene_id, "items": items, "searched": False, "locked": scene_id == "entry", "gap_secured": False,
                    "allow_passage": False, "alert": False, "npcs": {npc_id: {**deepcopy(npc), "hp": npc["max_hp"]} for npc_id, npc in NPCS.items()} if scene_id == "bandit_room" else {},
                    "combat": None}
            ctx.set_state(SCENES, scene_id, room, expected_version=0)

    functions = (
        FunctionSpec("dnd.join", join, JOIN_ARGS, description="Create one ordinary level-one human fighter or rogue for your existing identity. A repeat restores that same immutable character; it grants no referee privilege."),
        FunctionSpec("dnd.look", look, EMPTY, access="read", authorize=_viewer, description="Read your current character and scene versions, actual available actions, shared characters, progress, combat initiative and pending public intentions. Configured referee without a character receives a read-only observer projection (character=null). Use this before choosing or recovering an action."),
        FunctionSpec("dnd.handbook", handbook, HANDBOOK_ARGS, version=2, access="read", description="Read a versioned world chapter. Start with index/entry; AI assistants read agent, players read play, and shared participation uses together. Read actions/checks/combat when relevant. The gm chapter is restricted to configured referees."),
        FunctionSpec("dnd.act", act, ACT_ARGS, authorize=_member, description="Commit a published deterministic game action, with optional current character/scene versions. Known paths require no AI referee. Check disclosed risks before checks/combat. Initial attack establishes initiative; attack again only on your legal turn. Same failed method without changed evidence/Help cannot reroll. Unknown actions are unsupported, not fictional failures. Transport retries MUST reuse the same operation_id."),
        FunctionSpec("dnd.propose", propose, PROPOSE_ARGS, authorize=_member, description="Record a public novel method pending an externally configured referee, outside combat. It changes no world fact. Include current character and scene versions; only one unresolved proposal per owner."),
        FunctionSpec("dnd.rule", rule, RULE_ARGS, authorize=_judge, visible_to=lambda ctx: _judge(ctx), description="Configured external referee offers an ability/DC10..18 check, explicit reason/risk and finite listed effects. No dice, no arbitrary state patch, no other player's consent. An offered quotation cannot be replaced. secure_gap requires the actual rope. All reasoning fields are public."),
        FunctionSpec("dnd.commit", commit, INTENT_ARGS, authorize=_member, description="Only the original proposer accepts an offered risk: read authoritative ruling.terms/server_terms (cost and failure damage) as well as the external referee's explanation, then atomically roll once and commit success or failure. Authority and quoted character/scene versions must still match. A result cannot be rejected after seeing its dice. Reuse operation_id for transport retry."),
        FunctionSpec("dnd.withdraw", withdraw, INTENT_ARGS, authorize=_member, description="Only the owner withdraws a pending/offered proposal before commitment. Committed results are durable and cannot be withdrawn."),
        FunctionSpec("dnd.say", say, SAY_ARGS, authorize=_member, description="Write a public in-world utterance in your current scene. Text is expression, never a material fact, NPC ruling or another character's consent. Chat does not invalidate game quotations."),
        FunctionSpec("dnd.history", history, HISTORY_ARGS, access="read", authorize=_viewer, description="Read durable public chronicle after a numeric sequence, at most 50 entries per page. next_after/has_more provide real older-history continuation; look only shows the latest 16. Configured referees need no adventurer character."),
    )
    entry = ("Welcome to the original Old Lighthouse adventure, a small shared world using classic 2014 fifth-edition SRD 5.1. "
             "Your human user's goals and local memory remain in your own AI tool; this server does not run your inference. "
             "Read dnd.handbook(index), then entry; assistants also read agent and participants read together when interacting. Players can read play for their first decision. "
             "Call dnd.join(name,class_id=fighter|rogue) before an ordinary unjoined player's dnd.look; existing credentials resume the same character across clients. "
             "The optional shared objective is returning the unique beacon_seal to camp. Other users need not share your goal. "
             "Choose actual available_actions[].args with the current character.version and scene.version. Known actions work without a referee. "
             "Rope, potion, ledger and seal are finite persistent objects. Dice are server-recorded; retries reuse operation_id. "
             "Read checks before risky attempts and combat before fighting. Initial attack commits initiative; it is not a free attack before NPC turns. "
             "Novel methods: propose with current versions; only configured external referee can offer a bounded ruling; owner reads risk then commits or withdraws before dice. "
             "Never claim prose, a plan or an uncommitted offer is an executed event. Offline referees do not prevent normal actions. "
             "On a new model context, compaction, reconnection or conflict, reload bootstrap/handbook index and look, reconcile receipts/history, and continue from actual state. "
             "Fictional rest hours are explicitly a story-time simplification, never evidence of real cross-day persistence. This slice excludes PvP, spells, leveling, grid movement and full campaign scheduling.")
    return WorldDefinition("old-lighthouse-dnd", WORLD_TITLE, functions, version=2, initialize=initialize, bootstrap=bootstrap,
                           state_authorizer=_state_access,
                           state_rules=(StateRule(WORLD_STATE, "authority", _schema({"judges": {"type": "array", "items": ID, "maxItems": 8, "uniqueItems": True}, "epoch": VERSION}, ("judges", "epoch"))),
                                        StateRule(WORLD_STATE, "chronicle", _schema({"last_seq": {"type": "integer", "minimum": 0}}, ("last_seq",))),
                                        StateRule(WORLD_STATE, "progress", OBJECT), StateRule(CHARACTERS, "", OBJECT), StateRule(SCENES, "", OBJECT),
                                        StateRule(INTENTS, "", OBJECT), StateRule(HISTORY, "", OBJECT)),
                           entry_instructions=entry)


WORLD = make_world()
