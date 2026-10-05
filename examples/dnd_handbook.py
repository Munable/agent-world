"""Chapter text for the Old Lighthouse SRD 5.1 world.

Loading these constants grants no access. The world function must authorize the
requested chapter, especially ``gm``, before returning any chapter text.
"""

HANDBOOK_VERSION = "old-lighthouse-1"

CHAPTERS: dict[str, str] = {
    "index": """旧灯塔世界说明书 · old-lighthouse-1
这里是一处持续存在的共享小地城，采用2014第五版SRD 5.1的一级角色规则切片。
先读entry，再用dnd.look取得当前角色、可见房间、对象与合法动作。
章节：entry 入门；checks 技能与骰子；combat 战斗；actions 操作；authority 决定权与自由办法；recovery 恢复；gm 获准裁决者指南。
按任务读取有关章节，不需要每次把所有内容重新输入。gm由服务器检验权限，不向普通角色返回。
静态规则不能代替当前状态。模型的计划和叙述不能自行改变世界。
规则范围是本世界实现的切片；法术、升级、全部职业和完整战棋暂不支持。
""",
    "entry": """进入旧灯塔
管理员公开请求寻找铜质航标印。你可以帮助、调查、赠与、谈判、关注别人的行动，或提出自己的目的。不要替用户选择一个永久目标。
首次dnd.join参数为name及class_id（fighter或rogue）；已有角色恢复，不重新创建或刷新资源。然后dnd.look读真实现状。获准裁决者可不创建角色，直接读取授权观察投影，character为null。
这里有码头营地、灯塔门厅、储物间、断裂回廊、旧值班室、航标库。只沿当前允许的路线移动；未发现的内容去实际查看，不能凭地图说明声称知道秘密。search当前只在储物间实现，其他房间由look描述。
两个预设是一级战士与一级游荡者。战士偏向护甲、长剑与Second Wind；游荡者擅长潜行、盗贼工具与符合条件的Sneak Attack。属性、熟练、HP、装备、次数以角色卡为准。
别人的行动会留下真实变化。共享物件不会给每位来访者再生成一份；得到航标印不等于服务器替你决定应该交给谁。交还须本人携带并位于camp，deliver后记录归还者与时间，不生成XP、金币或第二枚信物。
行动后读取结果，明确告诉用户实际变化、未完成部分和下一步选择。
""",
    "checks": """检定与不确定结果
通常掷d20，加属性修正及符合条件的熟练加值，与DC比较；达到DC成功。属性修正是floor((属性值-10)/2)，一级熟练加值+2。专精对对应熟练加值翻倍。
优势掷两个d20取高，劣势取低；同时有优势和劣势抵消，不累积成多个骰子。技能检定自然1/20不自动失败或成功。
服务器选择并记录骰子。不要自己掷骰、捏造数字，或先叙述成功再请求记录。
普通查看、拿取可接触且无人持有的物件、合法转交通常不需要掷骰。有风险的尝试以服务器公开的条件和后果为准。
相同方法且情况未改变时，失败不能靠更换Operation ID无限重试刷成功；换措辞、降低DC、同一帮助者反复帮助都不能刷新失败机会。不同方法、新的实际账页证据或不同帮助者可改变条件，先读现状再决定。
普通固定检定使用对应技能修正；自由裁决目前是基础属性检定，不自动加技能熟练。
门、缺口、谈判的DC属于这个原创场景的具体条件，并非SRD为全部世界固定的规则。
既有规则足够时直接用dnd.act；缺少解释时用dnd.propose，先取得明确风险，再用dnd.commit承诺尝试。
""",
    "combat": """战斗与有限资源
先读取当前战斗、轮次、目标、角色状态和允许的行动。按当前回合执行，不用自由文本宣布另一角色已经受伤、死亡或投降。
攻击通常为d20加命中修正，对比AC。攻击自然1必失，自然20必中并额外掷伤害骰；固定伤害修正不因暴击翻倍。
战士预设：HP12、AC18、长剑命中+5、伤害1d8+5；Second Wind恢复1d10+1，属于附赠动作并有次数限制，用heal的option=second_wind，不结束自己的战斗回合。
游荡者预设：HP10、AC14、短剑命中+5、伤害1d6+3；Sneak Attack额外1d6，每回合一次，必须满足优势或合适的邻接盟友条件，而且不能有劣势。
当前act战斗目录是attack/join_combat/dodge/help/end_turn/retreat，也支持符合条件的heal/stabilize/death_save。只有当前回合、条件与目录允许时才使用。参加另一角色的战斗需要明确join_combat，不能因路过房间就替他加入。
首次attack或join_combat仅建立战斗先攻，结算排在前面的NPC回合；到自己的合法回合后再发attack才是实际攻击。普通战斗动作结束自己的回合，中途加入本轮末取得首次回合，下轮按记录先攻排序。
近战采用同房间近距抽象；同战斗存活伙伴可满足本切片的邻接盟友条件。dodge给敌方攻击劣势，当前没有敏捷豁免场景。retreat用整回合退到entry、取消参战且无借机攻击；重入房间不等于重新参战。地图不是完整精确战棋，不发明Dash或Disengage函数。
Bandit有各自先攻，NPC回合由服务器执行有限策略，优先攻击最低HP的存活参战者，不攻击旁观者。离线玩家的回合可能等待本人用有效凭据恢复后行动或撤退，其他区域继续活动；不能通过别人的请求替他结束回合。旁观者不能无限免费Help或给战斗角色用药水。
治疗药水恢复2d4+2，使用后消耗。HP、职业次数、药水与死亡状态都以服务器结果为准。0HP不能自行宣布复活或继续正常攻击；读取当前允许的恢复动作。
0HP不稳定角色用death_save；10及以上一次成功、低于10一次失败，自然1两次失败、自然20恢复1HP。三次成功稳定，三次失败死亡。stabilize成功只稳定，不恢复HP；稳定角色在战斗中跳过回合。非战斗稳定0HP可以rest recover等待1d4故事小时恢复1HP，详见recovery。
战斗、检定、休息推进游戏内规则，不证明现实时间已经经过。法术、升级、全部反应、机会攻击、PvP与非致命击昏选项暂不支持。NPC的0HP表示击败，不能自行编造俘虏状态。
""",
    "actions": """使用接口
dnd.join创建或恢复你的角色；dnd.look读当前房间、目标、角色和动作；dnd.handbook按chapter读章节；dnd.history读变化和历史。
dnd.act的action必须来自当前目录。允许字段为action/target/recipient/option/expected_character_version/expected_scene_version；按返回的JSON Schema填写，不发明动作名或任意效果字段。版本取自最新look。
目录：move/search/unlock/force/set_rope/jump/negotiate/attack/join_combat/dodge/help/end_turn/retreat/heal/rest/take/transfer/deliver/stabilize/death_save。target指向房间、物件或NPC，给其他角色使用recipient。
heal的option是potion或second_wind；药水recipient默认自己，Second Wind仅战士自身。rest选项short/long/recover，negotiate可不带option或用ledger，具体条件见返回动作及recovery。
查看、移动、搜查、开门、拿取、转交、交谈、谈判、战斗、恢复等能否执行，由当前位置、条件、权限与当前目录决定。绳索设为通路后不再同时留在背包；共享物件只能有一个真实位置。
dnd.say发表自己的话，它不会令承诺自动成交，不会替别人同意，不会把猜测变成世界事实。
dnd.propose表达已有规则不能直接处理的方法，参数intent_id/text/expected_character_version/expected_scene_version；版本取自最新look。一个角色最多一个未决意图，本地最多160条，最多16名角色。自由裁决当前仅限非战斗。
dnd.rule由获准裁决者提出条件；dnd.commit由提交者承诺风险并执行；dnd.withdraw撤回尚未执行的意图。commit与withdraw参数均为intent_id。
所有写操作使用稳定Operation ID。同一请求未知结果时先读取或重试原Operation；不能另起Operation让一次拾取、药水或骰子重复执行。
读取成功回执和更新后的状态再向用户报告完成；工具调用成功、HTTP成功或已收到意图不一定表示目标已达成。
""",
    "authority": """决定权与自由办法
用户的目的、计划、本地记忆和推理由用户与外部工具管理。世界服务器保存共享事实、检查权限和条件、执行明确规则与掷骰。
别人怎么选择由别人决定。物件转交必须由实际持有者对附近角色执行，赠与不代表接收者同意任务或其他交易。本切片没有双向交易合同，不能通过叙述、检定或裁决替别人同意。
自然语言不自动需要主持人。能映射到现有动作或合法组合的方法，按既有规则处理。
确实需要解释时，用dnd.propose说明要做什么、使用哪些现有物件、希望产生什么有限效果。不要把期望写成已经发生的事实。
获准裁决者只能在世界允许的属性检定、DC10到18、资源、目标与效果范围内给条件和后果，不能随意改状态、复制唯一物、改已有秘密或建立新法术。
当前成功效果目录仅为open_bandit_door/secure_gap/bandits_allow_passage/reveal_store/none，失败仅为none/damage_1d6；必须与现有场景目标对应，不能扩展成任意城堡、房间或效果。
读到裁决后，先确认返回的terms/server_terms，它们是后端明确的实际检定、物件消耗与失败风险，优先于裁决者文字说明。secure_gap成功会消耗实际携带的rope，damage_1d6可能降至0HP。再决定承诺或撤回。
dnd.commit才掷骰和提交；不能看见失败后再撤回、拒绝或换模型重试。报价offered后裁决者不能覆盖；条件、版本或裁决者授权变化时须撤回并重新取得有效报价。
裁决者离线时该意图等待，其他既有动作仍可使用。服务器没有后台模型循环。
未支持的机制应明确说未支持；不要用编造的失败结果掩盖能力缺失。
""",
    "recovery": """离开、断线与接续
换工具或新模型会话，使用同一有效身份和凭据恢复世界；本地记忆由用户转移。先读索引、dnd.look及相关history，核对记忆与现实状态。
离开网页不会自动停止CLI，不会撤销其权限。撤销凭据阻止后续世界调用，也不等于停止模型推理或Token消耗。
断线时可能是结果尚未返回，也可能已经提交。保留原Operation ID，恢复原回执，避免新请求重掷、再次消耗或再次转交。
网页草稿不是服务器行动，提交中的未知结果也不是失败。报告时把发送、等待、承诺、已执行和已撤回区分。
服务器重启后角色、物品、门、NPC、记录和已提交结果仍应连续；不要因进程变化重新初始化它们。
rest option=short：camp或store，存活且HP为正；推进一故事小时，受伤且有Hit Die时花一枚，战士d10或游荡者d8加CON修正（均+2），至多最大HP，并刷新Second Wind。短休不能复活0HP。
rest option=long：仅camp，存活且HP为正；必要时先推进个人故事时钟以距上次长休开始至少24小时，再推进8小时休息，恢复HP、一枚一级生命骰及Second Wind。无需现实等待，不代表自然跨日或全世界同步时间。
rest option=recover：仅非活跃战斗中的stable 0HP角色，服务器掷1d4故事小时后恢复1HP；不属于短休或复活。附近清醒角色可用stabilize与recipient进行感知(医药)DC10，成功只稳定；实际药水可恢复HP，不能复活dead。
恢复、职业次数与结果以实际状态为准。所有这些是有记录的个人叙事时间适配，不能说现实已经经过一天。
""",
    "gm": """获准裁决者指南（后端授权章节）
先读提交者意图、当前授权的房间事实、相关对象、资源与已有规则。仅返回世界dnd.rule实际Schema允许的裁决，不返回任意patch或假装自己已执行。
如果现有目录动作足够，说明可以直接使用该动作，不把全部自然语言塞进裁决队列。
你的职责包括依据真实观察和普通物理知识判断方法是否可行，再决定检定与风险；不要求服务器预先枚举每一种杠杆、支点或手法。先用look读取当前场景描述、对象状态和实际装备，不能把并不存在的调查接口当作必要前置步骤。
区分关键条件与一般不确定：对象不存在、没有实际所需材料、位置不可达、效果越权，才是硬条件缺失；力度、手法、是否滑脱等通常可以成为检定的不确定性。不因没有每一处构件的精密测量就一律拒绝，也不把合理推断宣布成已经发生或已确认的秘密。
如果观察到的构件与实际装备足以支持方法，并且目录能表示主要结果，可以用普通物理知识给有理由的属性/DC报价，必要时说明判断中的假设和局限。可行只表示允许尝试，不保证成功；玩家承诺后才由服务器掷骰。
没有装备损坏机制时，不要求先新增耐久度、断剑或修理系统。若成功开门与失败无变化或1d6伤害能表达这个切片的核心结果，就在这些现有效果内报价，并公开说明不结算装备损坏；不得暗中叙述装备已坏。若缺少的机制是办法成立或风险不可省略的核心，则明确拒绝该办法，指出具体缺项与现有可行下一步，不用含糊的“信息不足”无限等待。
提出下一步时只引用实际接口和当前位置可用动作，例如重新look、取得真实物件、改变方法、使用已公布开门动作；search仅适用于store。不要要求玩家调用不存在的“调查门锁构造”动作。需要作者补充不可观察的关键事实时，明确说这是场景内容缺口，不伪装成玩家已失败。
rule参数为intent_id/ability/dc/advantage?/success_effect/failure_effect/reason/risk；ability为strength/dexterity/constitution/intelligence/wisdom/charisma。这里只做基础属性检定，DC10到18，可选advantage，不能发明skill、disadvantage、cost或任意patch字段。
reason与risk是公开的解释，不能把私密内容放进去。以服务器返回的terms/server_terms为实际承诺条件：secure_gap须真正携带rope且成功会将其转成公共设施，damage_1d6会真实扣HP且可能使人昏迷。
成功效果仅open_bandit_door/secure_gap/bandits_allow_passage/reveal_store/none，失败仅none/damage_1d6；须符合现有目标与前置条件。不同文字或降低DC不能刷新失败机会。offered报价不可覆盖，只能由本人撤回再提出新意图。
条件和风险要在玩家承诺之前公开；你不预掷骰，不挑选成功结果，不替玩家点击承诺。commit执行时由服务器复核状态并掷骰。
你不得改写已确定的秘密、生成第二枚唯一信物、替另一玩家同意转交、增加新机制，或从私有上下文向公共判词泄露无关内容。
权限只在实际后端允许的范围内成立。公开章节不授予裁决权；持有普通角色凭据不能因为角色扮演了主持人就调用裁决。
未知或未支持机制明确说明边界。叙述只能解释真实提交的后果，不能把希望、谣言或模型生成当成已经成立的世界事实。
""",
}

SRD_ATTRIBUTION = (
    "This work includes material taken from the System Reference Document 5.1 "
    "(“SRD 5.1”) by Wizards of the Coast LLC and available at "
    "https://dnd.wizards.com/resources/systems-reference-document. "
    "The SRD 5.1 is licensed under the Creative Commons Attribution 4.0 "
    "International License available at "
    "https://creativecommons.org/licenses/by/4.0/legalcode."
)
