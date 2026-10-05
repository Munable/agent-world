"""Original six-room scenario using the CC BY 4.0 SRD 5.1 rules.

This data is trusted world code, never model-authored state patches. The room
graph is deliberately small; the rules and consequences belong to this world,
not to the neutral Agent World Runtime.
"""

WORLD_TITLE = "旧灯塔 · 航标失踪之夜"
ROOMS = {
    "camp": {"title": "码头营地", "description": "港口守灯人正在等候遗失的航标印。每个人可以自行决定是否帮忙、记录真相，或与其他来客交谈。这里能安全休息。", "exits": ["entry"]},
    "entry": {"title": "灯塔门厅", "description": "盐风从破窗穿过。左侧是储物间，右侧回廊的地板断裂，正前方的木门通向有人占据的旧值班室。门厅这一侧能看见两道外露铁铰链：表面已被盐风腐蚀，固定螺钉周围的木材有裂纹。门底与石门槛之间留着约两指宽的窄缝；平直、牢固的石门槛边缘露在门外。门上有旧撞击痕迹，锁具仍在门上。", "exits": ["camp", "store", "gallery", "bandit_room"]},
    "store": {"title": "储物间", "description": "落灰的架子和旧账册仍在原处。搜索可以确认有哪些实际遗留物；这里没有刷新物资的机关。", "exits": ["entry"]},
    "gallery": {"title": "断裂回廊", "description": "回廊有一道十尺深的缺口。对面通往航标库。用绳索可以建立安全通路，也可以接受坠落风险尝试跳跃。", "exits": ["entry", "vault"]},
    "bandit_room": {"title": "旧值班室", "description": "两个武装拾荒者守着通向航标库的门。他们声称只是在取回自己的旧物。可以谈判，也可以主动发起战斗；没有玩家会因站在同一房间就被强制加入战斗。", "exits": ["entry", "vault"]},
    "vault": {"title": "航标库", "description": "旧式铜制航标座立在窗前。原物只有一份，谁取走、交给谁、是否归还，都会留下持续的记录。", "exits": ["gallery", "bandit_room"]},
}

ITEMS = {
    "rope": {"name": "麻绳", "description": "一捆结实的五十尺麻绳。架设缺口安全通路后留在现场，不能同时继续随身携带。"},
    "healing_potion": {"name": "治疗药水", "description": "唯一一瓶，使用后消耗。恢复 2d4+2 生命值，不能复活已死亡者。"},
    "ledger_page": {"name": "旧账册页", "description": "页上记载拾荒者获准取回旧工具；航标印尚留在库中。指出这些记录能改变谈判的实际证据。"},
    "beacon_seal": {"name": "航标印", "description": "港口遗失的唯一铜印。可以携带、交接，或在码头交还守灯人；归还后不再刷新。"},
}

NPCS = {
    "bandit_1": {"name": "拾荒者阿栎", "ac": 12, "max_hp": 11, "attack_bonus": 3, "damage_die": 6, "damage_bonus": 1, "initiative_bonus": 1},
    "bandit_2": {"name": "拾荒者伊苇", "ac": 12, "max_hp": 11, "attack_bonus": 3, "damage_die": 6, "damage_bonus": 1, "initiative_bonus": 1},
}

CLASS_PRESETS = {
    "fighter": {
        "class_name": "战士", "abilities": {"strength": 16, "dexterity": 14, "constitution": 15, "intelligence": 9, "wisdom": 13, "charisma": 11},
        "max_hp": 12, "ac": 18, "hit_die": 10,
        "skills": {"athletics": 5, "perception": 3}, "saving_throws": {"strength": 5, "constitution": 4},
        "weapon": {"id": "longsword", "name": "长剑", "attack_bonus": 5, "damage_die": 8, "damage_bonus": 5},
        "equipment": ["长剑", "链甲", "盾牌"],
        "features": ["决斗：单手长剑伤害已包含 +2", "回气：附赠动作恢复 1d10+1 HP；短休或长休恢复使用次数"],
    },
    "rogue": {
        "class_name": "游荡者", "abilities": {"strength": 9, "dexterity": 16, "constitution": 15, "intelligence": 14, "wisdom": 13, "charisma": 11},
        "max_hp": 10, "ac": 14, "hit_die": 8,
        "skills": {"acrobatics": 5, "investigation": 4, "perception": 3, "persuasion": 2, "stealth": 7, "thieves_tools": 7},
        "saving_throws": {"dexterity": 5, "intelligence": 4},
        "weapon": {"id": "shortsword", "name": "短剑", "attack_bonus": 5, "damage_die": 6, "damage_bonus": 3},
        "equipment": ["短剑", "皮甲", "盗贼工具"],
        "features": ["专精：隐匿与盗贼工具已包含双倍熟练", "偷袭：每回合一次额外 1d6；有优势，或另一名存活参战伙伴邻近目标且自身没有劣势"],
    },
}

ABILITY_LABELS = {"strength": "力量", "dexterity": "敏捷", "constitution": "体质", "intelligence": "智力", "wisdom": "感知", "charisma": "魅力"}
