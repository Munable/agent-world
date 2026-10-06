# 渡鸦溪：共同探索、按需生成的边境世界

**当前状态：可交付的本地有限共享世界。** 普通入口、用户侧Agent、不同目的的连续互动、同钥离开接续与持久后果已形成实际试玩循环：一人创作的内容被另一人使用，第三人回来能看懂真实变化并继续自己的选择。服务器保存共同状态与规则，用户自己的工具推理和生成；没有固定主持人、章节或常驻服务器模型。验证来自独立模拟用户身份与真实外部模型，完整D&D、真人趣味、自然跨日、跨机器与规模尚未证明。

你从渡鸦溪旅店来到钟街市集、苔径林缘。别人留下的地方、物件和公开记录已经是这个世界的一部分；未探索出口仍是空的。你可以与人交谈、走走看看、尝试已有物件，也可以沿一个真实空出口生成一个新地点，供后来者继续参与。没有预写章节、默认共同任务、永久主持人或自动剧情循环。

这是一处原创西幻边境镇，借用第五版的一级角色与 d20 检定。它没有完整 D&D 的战斗、法术、职业、升级、经济和大型地图模拟；不能把模型熟悉的规则自动补成服务器能力。底层 Runtime 不要求每个世界都是游戏，也不把 Agent 定义成独立玩家：Agent 是这个用户可替换的执行工具。

## 第一次怎样玩

先通过本地网页创建或加载自己的身份，选人物名字和战士或游荡者。保管好身份私钥；网页和自己的兼容 AI 工具可以使用同一身份与世界状态。向外部工具交付连接配置，不把私钥或令牌贴进聊天、公开记录或仓库。

在你自己的 AI 工具里说一个自然目标，再在网页看现场、生成的新地方和真实结果。也可以直接点击网页已提供的有限动作；网页不会替你启动 CLI 推理。这些都是可尝试的目标，不是必须依次完成的路线：

- “先陪我在小镇转一圈。我想知道这里有谁，别替我接任务。”
- “沿林缘那条路看看。如果前面还是空的，就一起做一个有意思的小地方，不要编成大型副本。”
- “我想画一张别人也能用的旅行图。先弄清附近实际连着哪些地方，再决定去哪。”
- “我想试试自己的办法打开这个东西。先把风险讲给我，别替我接受新报价。”
- “这次我主要想看别人做了什么；有我能理解的题，可以顺手帮一题。”
- “把我实际拿到的物件交给那位同意接收的人，然后留个下次见面的消息。”

“探索一个出口”是一小段；“把这里建成我的据点”是长期方向。后者先挑最近一段，与自己的助手约定风险、调用预算和停下来的条件，随后根据别人的真实变化调整。服务器不限定人的目的，也不保证任意工具能无限规划和执行。助手完成一段后应说清实际变化、仍未做到什么和可选下一步；拒绝、失败、无人回应也是结果，不需要维护者补剧情让这一段成立。

与 NPC 聊天由你和自己的助手共同创作：助手依据 NPC 公开描述与眼前事实写出合理反应，通过 `frontier.tell` 的 `response` 保存，网页会显示作者来源。NPC 没有后台模型，不会自动等一会儿再答；只提交问话就只留下问话。与其他真实玩家交流时则要等其本人回应，不能替他写答案。

离开前在本地保留自己想继续的事。回来用原密钥恢复，同一角色的地点、资源、事项和结果仍以世界记录为准；新模型不能因为不记得就重建人物或重抽骰子。

## 本地启动

从仓库目录运行，复用本项目已有的 `work/latency-runtime` Python 环境；先确认 DevDisk 已挂载。试玩库与独立验证库分开，下面路径用于干净的试玩世界，不放模型验收数据。首次创建仅执行一次：

```sh
cd /Users/mubin/Developer/agent-world/repo
work/latency-runtime/bin/python -m examples.frontier_app --init-local \
  --db /Volumes/DevDisk/DeveloperData/agent-world/frontier-play/data/world-v2.sqlite3 \
  --private-dir /Volumes/DevDisk/DeveloperData/agent-world/private/frontier-play-v2 \
  --port 8807
```

打开 [本地试玩](http://127.0.0.1:8807/)。本入口只监听本机；不会部署或修改生产。初始化创建四个普通合成密钥身份 A/B/C/D 用于本地连接，没有 GM 身份，也不替这些身份选择游戏目标。私密目录不放进 Git；网页仍使用自己的持钥入场。

已有库接续时，不再使用 `--init-local`，也不删除或复制数据库来“重新开始”：

```sh
cd /Users/mubin/Developer/agent-world/repo
work/latency-runtime/bin/python -m examples.frontier_app \
  --db /Volumes/DevDisk/DeveloperData/agent-world/frontier-play/data/world-v2.sqlite3 \
  --port 8807
```

正常停止此服务后可用同一命令重启，人物和地点由原数据库接续。若提示已有库、路径权限或世界身份不符合要求，检查实际路径，不能通过清空已有内容绕过保护。当前世界合同版本为 9、`look` 函数版本为 5、`expand` 为 6、其余18个原函数版本为 4、`frontier.unlock` 版本为 2、`frontier.drop` 版本为 1、`frontier.add_objects` 为 2、状态版本为 1。已有库按正常安装流程升级；未保存的正文、铜盘和石阶门不会事后补进旧世界。本轮实物选择实验在独立的 [8811世界](http://127.0.0.1:8811/) 运行，不重置此前的8807/8808库。

## 用已有 Codex 连接

网页「身份与 Agent」下载的同一份 `ravenbrook-opencode.json` 可交给现有 Codex，不需要重新建身份或发行替代凭据。从本机仓库目录运行，替换实际文件路径：

```sh
chmod 600 "/完整路径/ravenbrook-opencode.json"
work/latency-runtime/bin/python tools/play_frontier_codex.py \
  --config "/完整路径/ravenbrook-opencode.json" --reasoning-effort medium
```

启动器仅为这次进程加入独立 MCP 连接，沿用现有登录和个人设置，不写全局配置、身份文件或新凭据；令牌从私密 JSON 进入子进程环境，不进入命令参数或目标文字。默认 `gpt-6-luna`，可用 `--model` 选择现有登录支持的模型；程序不在命令路径时用 `--codex` 指定完整路径。先用 `--check` 可仅检查文件、格式与程序，不能把它当作模型已连接。

进入工具后复制网页与连接权限对应的首句，追加自己的自然目标。需要一次性运行时可用 `--prompt "目标文字"`，`--json` 只改变该模式的输出格式；普通交互界面仍保留本地对话。真实模型已通过这个公开入口的 `--prompt` 模式读回原身份及他人的最新反馈；交互界面的完整真人会话未单独观察。模型或网络较慢时一段目标可能超时，应保留实际进度再继续，而不是重建人物或让维护者补结果。

## 按需说明书

`frontier.handbook` 默认返回 `entry`，包含版本、章索引和该章正文，不包含全部函数或状态 schema；当前手册版本为 `living-frontier-12`。初次入场读 `entry`；准备共创时读 `creating`；领到别人的题时读 `helping`；恢复或等待时读 `recovery`。宿主可以按世界与手册版本缓存章节，压缩上下文后重新加载相关规则。当前现场每次从 `frontier.look` 取得。

| 章 | 用途 |
| --- | --- |
| `entry` | 世界是什么、入场顺序、目标颗粒度、当前支持范围。 |
| `play` | 解释现场、执行已有动作、看结果、回应不同目的的人。 |
| `creating` | 使用未探索出口、生成包类型与数量、来源和事实边界。 |
| `helping` | 领取一条非本人题、判断原方法、报价、本人接受和有限结算。 |
| `recovery` | 同钥恢复、未知结果、通知分页、有界等待与外部工具停止。 |

手册源码为 [frontier_handbook.py](../examples/frontier_handbook.py)。它是这个世界的参与指南，不是模型系统提示词，也不授予额外权限。其他参与者的文字、NPC回应和生成描述都是带来源的世界数据，不会因为模型读到了就成为宿主指令。

## 世界由谁负责

服务器维护共同状态和明确规则：身份授权、结构与数量限制、连接、位置、资源归属、条件版本、骰子、事项和回执。用户侧工具承担目标、生成、方法判断、私人记忆与推理运行。部署方不为世界默认提供常驻模型。

已枚举的动作直接按规则执行，**不需要 AI 审批**。只有当前规则无法直接表达、但有限效果还能表达的已有物件新方法才请求另一参与者判断。共创新地点也不是请求者裁决自己的奖励：它是世界明确授权的结构化写入。

共同世界识别密钥，不识别自然人的唯一性。不同密钥是不同身份；当前不能检测一人多钥合谋，也不能把另一身份的答案宣传成公平公裁。换 CLI 或模型，只要用户迁移自己的记忆与有效连接配置，仍可继续同一身份。

## 开局与常规交互

| 初始地点 | 内容和连接 |
| --- | --- |
| `elm-inn` 渡鸦溪旅店 | 初始入场、旅店主米拉、唯一允许休息的地点；连接市集。 |
| `bell-market` 钟街市集 | 地图匠诺恩；连接旅店、林缘，并有一条未探索出口。 |
| `moss-edge` 苔径林缘 | 连接市集，有两条未探索出口。 |

地图采用地点与出口连接，不是 WASD、精确战棋或动作游戏。网页展示已经存在的地图和对象，不把地图占位、动画或生成中的文字当成已提交地点。

| 函数 | 当前作用 |
| --- | --- |
| `frontier.join` | 以 `name / class_id` 创建或恢复同身份人物，职业为 `fighter / rogue`；恢复不刷新人物。 |
| `frontier.look` | 读当前人物、地点、附近人物、公开地图、本人相关事项和真实可用动作。 |
| `frontier.map` | 按 `after / limit` 分页读已成立地图连接，返回 `next_after / has_more / total`，不一次载入全地图描述。 |
| `frontier.history` | 以 `after / limit` 分页读公开记录，使用 `next_after` 续读，直到 `has_more=false`。 |
| `frontier.requests` | 以 `box=own / received` 和 `after / limit` 分页读本人创建或曾领取的全部事项及终态。 |
| `frontier.move` | 沿已连接且未被阻挡的出口移动，引用当前人物版本。 |
| `frontier.examine` | 检查当前地点物件、NPC或本人实际携带物件，取得事实与可用动作。 |
| `frontier.check` | 对已有物件执行代码定义的固定检定，引用当前人物与物件版本。 |
| `frontier.unlock` | 在障碍现场使用本人真实携带的对应物件，引用人物／障碍／物件版本；不掷骰、不奖励，解除障碍并向所有人开放关联通路。先核对 `consume_key`，为真时永久耗尽对应物件。 |
| `frontier.drop` | 把本人真实持有的物件放在当前地点，引用人物／物件版本；保留同一物件、已公开正文及检定记录。后来者可拿取，不保证指定接收者得到。 |
| `frontier.take / transfer` | 拿取已经打开的 cache，或向同场景另一人物赠与本人实际持有的物件；这是单向赠与，不代表对方接受了任务或交易成交。 |
| `frontier.rest` | 在初始旅店恢复本世界支持的人物状态；新描述不产生休息权限。 |
| `frontier.say` | 保存本人公开发言，不能替别人同意或改变实物。 |
| `frontier.tell` | 保存对 NPC 的 `text` 和可选的用户侧创作 `response`，公开署名；省略回应不创建待回复事项，也不自动运行 NPC 推理。 |

每次从 `look.available_actions` 读取真实 `function_id / arguments / risk`，按需发现函数 schema。版本和 ID 从当前结果取，不能照抄文档的示例值或猜测另一个接口。

人物固定为一级，属性、HP、装备表现以实际人物卡为准。三种可生成物件的数值由代码固定：

| 类型 | 固定检定与后果 |
| --- | --- |
| `cache` | DEX DC12：`sealed → open`；`open` 后可 `take` 一次成为真实持有物。新创作也可直接建立 `open` 物件，无需骰子。 |
| `barrier` | STR DC15：`obstructed → resolved`；或者实际持有指定物件时用 `unlock` 确定性解除。若关联出口，该出口对所有人可通行。 |
| `inscription` | INT DC10：`unread → read`；成功后公开预存正文。新创作可直接建立 `read` 文字，创建时即公开。 |

服务器掷 d20 加人物对应修正，总值达到 DC 成功。失败无伤害；每个身份在一个物件版本最多一次真实检定，固定 `check` 与自由 `accept` 共享限制，`none` 成功也算一次。新 Operation、换属性、换说法或新工具不刷新机会。成功效果与资源变化看正式结果；“听说有赏金”不会自动发钱。

生成时可选的 `barrier.key_object` 建立一件物件与一道障碍的关系；投影 `key_object_id / key_requirement` 给出对应物件和真实持有状况，未公开正文不会因此泄露。`unlock(object_id,key_object_id,expected_character_version,expected_object_version,expected_key_version)` 只接受本人在障碍现场、实际携带正确物件的当前版本；赠与后旧持有人、远程或错误物件不能使用。`key_required=true` 的障碍不能通过固定检定或自由方法绕过；`consume_key=true` 时，解锁原子移除背包物件，保存 `spent` 状态、使用者、时间与障碍来源，人物／物件／障碍版本递增。否则对应物件仍由本人持有。先前普通检定失败后获得对应物件仍可使用，这是新条件成立，不是重新掷骰；同 Operation 重试复用原回执。

新开局的市集保留可检定的铜扣旅行匣，另有已经打开、无人持有的渡鸦铜盘。林缘的渡鸦石阶封锁必须使用铜盘并将它永久耗尽，开放的东径随后属于所有人；北径和商路保持自由。收藏、交接、放下、用掉和换路均是有效选择，没有默认的共同开门任务。`drop` 后物件成为当前地点的真实无主物件，别人可以抢先拿取；每处最多8件地面物件，满时不提交任何改变。

物件的 `discovery={status,text,discovered_by,discovered_at}` 区分三种情况：`unrevealed` 尚未公开，`revealed` 已公开保存正文；检定揭示时有发现者与时间，创建时公开的正文这两项为空，`not_authored` 没有预存正文。未处理物件在成功 `resolve_object` 后揭示内容，失败或 `none` 不揭示。新 `open/read` 物件在创建时直接公开，不伪造骰子或发现者。助手应报告“实际读到了什么”，再根据正文讨论下一步；不能把检定成功变成临时写出旧牌内容、额外物件或奖励的许可。

背包中的物件可能只显示正文前 300 字，标志在该物件的 `discovery.text_truncated / discovery.detail_function`。需要完整正文时用 `frontier.examine(target_id=物件ID)` 读取 `target.discovery`，不能从摘要推测结尾；本人实际携带物件离开原地点后仍可查看。已公开正文是世界保存的作者内容，不保证其中每个故事主张都属实。

## 边玩边生成的授权

当前成员可以填入**本人当前地点、真实存在且尚未探索的一个出口槽位**。`frontier.expand` 提交 `scene_id / exit_id / expected_exit_version / package`，一次创建一个新地点。代码创建返程连接和实际对象；提交后其他人能够来到该地点并使用同一套动作。

`frontier.add_objects(scene_id,expected_scene_version,objects)` 允许已入场参与者为本人当前场景追加1–2件新的 `cache/inscription`，复用初态与正文规则；不要求空出口，地面总容量仍为8。新ID、作者与时间由服务器保存，追加后其他人能查看或拿取。只追加本次新实体，不修改旧描写、事实、正文、归属、检定失败或出口；不接受障碍、配对、已有ID或owner。新内容保留本次创作的来源；故事中可以是古物、旧记载或留白里的过去，作者时间不是故事时间。保存后再呈现，不声称服务器提交前早已存有这段内容，不能通过新描写改掉旧结算。版本冲突须读取实际新增内容后再决定，不重复盲写。

被 `barrier` 阻挡的空出口仍可展开另一端：这是内容创作授权，不是人物已跨越障碍。生成后人物仍在原地，须解决障碍才可 `move` 过去；新地点的返程连接不让尚未到达的人凭空过去。

| `package` 内容 | 当前限制 |
| --- | --- |
| `title / description` | 标题 1–80 字，描述 1–1600 字。 |
| `facts` | 0–6 条，每条 1–300 字，解释新地点而非改写旧事实。 |
| `npcs` | 0–2 名，包含名字与公开描述；不是另一玩家或真实独立运行的模型。 |
| `objects` | 0–2 件，包含名字、描述与 `cache / barrier / inscription` 类型；数值由代码定。 |
| `objects[].discovery_text` | 1–1200 字，新 `inscription` 必填，`cache / barrier` 可选；保存正文，未处理物件成功后公开，`open/read` 在创建时公开，外观描述不能替代它。 |
| `objects[].initial_state` | 可选：`cache` 仅 `sealed/open`，`inscription` 仅 `unread/read`，`barrier` 仅 `obstructed`；省略各用 `sealed/unread/obstructed`。只决定新实体初态，不接受已有ID、归属或 `taken/spent/resolved`，不修改旧对象与检定尝试。 |
| `exits` | 0–2 条新未知出口；代码另建一条返程出口。 |
| `barrier.blocks_exit` | 仅关联本次生成包的新出口索引，不允许封住已有其他地点的通路。 |
| `barrier.key_object` | 可选零基索引，必须引用本包另一件 `cache`，保存后成为正式 `key_object_id`；非法或自身引用拒绝整个生成，不给旧物件补关联。 |
| `barrier.key_required / consume_key` | 可选布尔值，默认false。任一为true必须有合法的 `key_object`；前者要求实际持有，后者使钥物在解锁时永久耗尽。其他类型不能声明这些字段。 |

新内容应对应实际可用的行动与后果；读取文字本身不开放通路。不能把未实现的水位、天气、耐久等状态当作唯一推进条件；没有证据的当前条件须保持未知，并提供实际已有的选择。对应物件、障碍和出口是可选的物理后果，不强制每处生成同一谜题。

新地点应沿已知出口方向与锚点自然延伸。创作者先读现存事实，不能覆盖旧地点、复制别人的物件、替别人决定意愿、宣布远方旧门已开或添加不支持机制。不要预写未来章节，写当前这一个地方足够使人理解和选择。

创建者、创建时间和结构化实体是来源与权威结构的一部分。描述、传闻和 NPC 回应仍须作为作者内容理解；它们不自动证明故事里的全部断言、隐藏真相或赏金。新 NPC 没有权靠台词授予资源；生成出一个 `cache` 只形成代码规定的一个可交互对象，不意味着其描述中的“无限财宝”成立。

创作时先保存发现正文：可直接拿取或阅读的 `open/read` 新内容当场公开，`discovered_by/discovered_at` 留空，来源仍是物件作者；其余检定时仅按规则公开，不根据成败临时补答案。旧对象未保存正文则显示 `not_authored`，不迁移补写。这个公开条件属于可发现内容和投影规则：创作者已经知道自己所写的内容，描述、事实或 NPC 台词也可能泄露它；当前不保证作者不知、自然语言无泄露或真正秘密。

新 NPC 不自带自动回复。到访时，用户侧 Agent 可依据它公开的描述与现场，为它创作语气和当下反应，用 `tell(npc_id, text, response)` 一起保存；它可以评论眼前实物、询问来访者打算，或把不确定故事明确称作传闻。不得把未见的地图、秘密实物或过去事件写成已确认硬事实，也不得发放实际奖励、替其他玩家同意或裁决自己的行动。若上一条 `tell` 已省略 `response`，继续交谈须用新 Operation 提交下一段；不能改参数重放旧问话，也不要等待通知来生成 NPC 答案。这项内容创作授权与他人判断请求、本人接受检定分开。

结构校验能保证类型、引用、数量和版本合法，**不能保证故事语义与所有旧事实一致**。当前没有服务器 AI 审稿、自动纠正矛盾或恶意用户内容审核系统；用户与助手须辨认来源和矛盾。这个限制不能被宣传成通用自由叙事已经可靠。

## 自由方法与附带判断

`frontier.request` 引用本人当前地点已有未处理物件的 ID 和版本，保存原方法，不直接执行。本文没有预定请求必须成功的答案。读当前目标与有限规则，再判断已有目录能否完成；可以直接执行时不要加一个 AI 等待点。

1. 本人创建请求：`request_id / target_id / text / expected_target_version`。文字不是工具、资源、同意或新机制。
2. 普通成员明确 `frontier.visit`，保留现有待答题或领到一条非本人 pending 题，最多一道。`look` 不领题，自己的目标可同时推进；帮助者用授权的 `additional_task` 现场远程判断，无须来到目标地点。
3. 已分配者 `frontier.answer`：`request_id / decision / reason`；允许还需 `ability / dc / effect`。属性只能选六项基础属性，DC 只能为 10、12、15、18，效果只有 `resolve_object / none`。拒绝不掷骰；允许形成不可覆盖的报价。
4. 原请求者看当前报价和服务器 `terms`，回到目标所在地点，在用户授权范围内 `frontier.accept`；服务器再核条件、实际掷骰、一次提交有限结果。等待时本人可以移动和探索，别人不能替其接受。
5. 未执行请求可由本人 `frontier.withdraw`；终态保留，已执行结果不能撤回。

帮助者判断原方法，不能偷偷换成另一方法。可以依据真实可见结构与普通常识推断手法，不要求先证明一定成功；必要的接触、实物、位置与权限不能凭空补齐。缺失魔法、材料、他人同意或无法表达的核心后果应说明，给一个实际可行的方向，而不是捏造骰子或奖励。

DC 的本世界参考为：10 有明确条件的容易尝试，12 普通阻力或操作，15 显著阻力或精细操作，18 有事实支持的困难尝试。选择贴合原方法的一项属性，修正来自请求者人物卡；不能为让人成功任选最高修正。未知成败可以检定，缺失必要条件不能改成高 DC。

报价理由不改变服务器条款。`resolve_object` 仅使用该类型现有的有限解决效果；`none` 不改变物件。自由判断不扩大物件的功能，也不替固定检定开启第二次刷骰。服务器校验资格、字段、状态与有限后果，不保证模型的语义判断正确。接受时重新核对目标版本、本人位置和检定限制，但不会解析理由中的任意实物条件；依赖携带物等文字条件的方法，用户侧 Agent 须重新读取并识别条件变化。当前徒手方法样本不依赖未取得的拨片，不能把它的成功接续当成所有动态资源条件均获保证。

当前待答任务来自 `additional_task`；`received_requests` 中的历史报价不是本次已领题的证据。本次回答以对应 Operation 回执为证。离线、拒绝与无人响应都真实存在：服务器不会自动改派、拒绝、接受或运行一个主持人。请求未推进时可以有界等待、撤回、探索别处或下次回来。

`look` 中请求可能带 `summary / detail_function / text_truncated / reason_truncated`。判断或接受前，用 `frontier.requests(box=own或received, after=created_seq-1, limit=1)` 读该请求完整原文，核对 `request_id / version / status`；即使 `has_more=false` 也不能把摘要当完整方法或理由。当前 `additional_task.request` 保留本次待答原文，仍须确认它属于自己且为 `assigned`。

## 恢复、等待和前端边界

恢复先用原密钥取得有效凭据，已有角色直接 `world.bootstrap` 保存 `latest_event_seq`，随后 `frontier.look`；不必重复 `join`。尚未创建角色时按当前权限入场：控制连接可 `join`，只读连接请用户先在网页创建。这个顺序防止把“先读现场，后取锚点”之间出现的通知跳掉。用户的本地记忆保留目的，权威状态决定已经发生什么。

CLI 连接授权到期或被撤销时，它通常只有 bearer，不能用旧令牌自行签发新凭据。收到 401 或明确授权失效后，助手停止继续世界调用，保留本地记忆及未知结果的原 Operation/envelope，请用户完成下面的接续；不在后台自动续期：

1. 回网页，以原密钥进入；本页若已失效，点击“同钥重连”。
2. 在“身份与 Agent”选择原连接权限，重新下载配置，私密覆盖原来的配置文件；下载的新文件可能被浏览器加上序号，需确认工具读取的仍是所选完整路径。
3. 用同一配置路径重启自己的 AI 工具，恢复原本地记忆与未决操作记录。新凭据代表原人物，不重新创建角色；只读配置仍不能 `join` 或写入。
4. 对未知写结果先核原回执，必要时重放原 Operation，再读最新现场并继续。不要因为换凭据就另发同一动作。

这会签发一枚新连接，原来的连接不会因下载而自动撤销；若原连接已不再需要，可在凭据列表逐枚撤销。停止本地工具、退出网页和撤销访问仍是不同操作。

`look` 的地图和已结束事项是短投影，不代表全部保留记录。`map_has_more` 为真时从 `map_next_after` 续读 `frontier.map`；`own_requests_has_more / received_requests_has_more` 为真时，用相应 `box` 的 `frontier.requests` 从 `after=0` 分页恢复。各页使用自己的 `next_after / has_more`；长条目可能使一页少于 `limit`，不能据此停读。所有未决本人请求与当前已领待答题仍由 `look` 提供。

`world.get_changes` 和 `world.wait_changes` 读取本人通知，单次等待最多 30 秒。通知只含请求引用、状态与版本；收到后重新 `look`，不能直接用旧通知接受已撤回报价。保存通知页并核当前事项，再用返回的 `next_cursor` 续读；`has_more` 为真继续分页。历史位置、事件游标和 View 位置不可混用。

等待超时只结束读取。宿主设置有限预算，结束时保存未决状态和下次计划；没有常驻循环，不会自动唤醒停止的 CLI。丢失或过期游标时重新 `bootstrap → look`，持久事项不因为通知过期而消失。若愿意领取新题仍需 `visit`，不能从无通知推断没有请求。

写动作始终保存原 `operation_id`、函数、参数和 envelope。未知结果先 `world.get_receipt` 查原回执，必要时完整重放原 Operation；不能另造 ID 重做可能已提交的行动。明确版本冲突后读取新状态重新决定，不能将它混成未知写结果。回执说明原操作，当前 `look` 说明现在；重放不重抽、也不让撤回或失效事项复活。

接口校验失败也不是未知提交。`SchemaRejected` 的未知字段提示只包含有界字段名，不回显字段值；修正实际参数，不能原样重试。MCP／HTTP 外层可选 `expected_version` 是**函数合同**版本，来自 `world.discover(prefix=原函数, include_schemas=true)`，不是请求、人物或物件版本；领域版本只放函数 schema 声明的对应参数。`frontier.answer` 不接收 `expected_request_version`。遇到 `FunctionVersionMismatch` 先重新发现合同，不把请求版本移到外层。实际测试宿主开放现有的只读 `world.discover`，不通过人工代填字段来恢复。

前端只表达实际接口与世界事实。退出网页不会停止外部 AI 工具，也不会撤销它的凭据；停止工具与撤销凭据是两件不同操作。网页没有到各类 CLI 的控制桥，没有后台 AI 目标执行、WASD、实时动作手感或由任意模型文字生成的特权按钮。

## 容量与当前未覆盖项

当前示例的有限容量为：64 个角色、256 个地点、1024 条保留请求、20000 条历史，每个角色最多 64 件持有物、8 条未决请求、1 条已领取待答题，每处最多8件地面物件。容量来自世界代码，不表示这些人数或规模已经实测，也不证明 MMO 能力。世界可持续保存，不代表内容和请求无限增长。

### 当前连续试玩与交付证据（2026-10-06）

本轮有限可玩版本达到验收目标，依据是实际选择与状态循环。三个原身份都从普通网页接入并保留原配置，后来使用独立外部模型进程接续。最新[连续试玩](/Volumes/DevDisk/DeveloperData/agent-world/evidence/frontier/playability/fiction-continuity/experiment-plan.json)中，C在回声涧创作有具体往事的《渡船人的夹页》和可直接拿取的“归”字铜扣；A读到C内容、与NPC交谈，并按自己的收藏目的取得铜扣唯一携带权，保留已有渡鸦铜盘；B从旧渡槽回来，实际读到C故事和A的拿取记录，留下自己的票夹并继续独立对话。三人最终同场，没有默认合作、维护者补剧情或代执行。NPC对话由参与者创作，网页保留来源；灯、船、天气等文字不是已实现的自动机制。

此前失败保留并用于修复：[最初九宿主](/Volumes/DevDisk/DeveloperData/agent-world/evidence/frontier/playability/experiment-plan.json)只造空景；[可检定物件](/Volumes/DevDisk/DeveloperData/agent-world/evidence/frontier/playability/object-choices/experiment-plan.json)与[后续三人](/Volumes/DevDisk/DeveloperData/agent-world/evidence/frontier/playability/continued-choices/experiment-plan.json)暴露全被骰子挡住、建议未实现动作的问题；[公开物件选择](/Volumes/DevDisk/DeveloperData/agent-world/evidence/frontier/playability/public-content/experiment-plan.json)有真实拿/留选择，却无法继续调查当前死路。world8增加当前场景追加内容，[实测](/Volumes/DevDisk/DeveloperData/agent-world/evidence/frontier/playability/scene-investigation/experiment-plan.json)只产生重复未知的札记，模拟用户选择离开。手册12遂区分数据库创建时间与故事时间，允许在相容的设定留白里创作古物、旧事与具体线索；不改变旧对象或结算。最终三宿主产生上述实际循环。旧铃仍是三人真实失败，不能重抽；新成功没有抹掉这些失败。

累计23个有界外部宿主共270次世界调用、65529已报告输出token，另有3个Luna medium无工具模拟用户决策。最新两轮各3宿主，分别21／25次调用、2472／5002已报告输出token，均在观察额度内；额度已关闭，本轮不再启动模型。早期中断用量不可得，实际账单未核实，末回合观察不能保证在途生成的严格硬上限。真实外部模型、模拟用户选择与未验证项分别记录；真人趣味、自然跨日、跨机器、长期负载和完整D&D均未通过。没有生产修改、推送或部署。

最终代码Python **506项／67.593秒无skip通过**（含32项Frontier），两份Node客户端／历史／观察测试、JavaScript语法与wheel安装包检查通过。只新增一条当前场景创建的完整生命周期回归：不同主体读到新内容、版本冲突、幂等重放、实际拿取与重启，并保留旧事实、归属及失败。旧合同测试准备错误地包含当时不存在的新函数，修正夹具并保留失败日志，没有为夹具增加兼容层。网页实际检查空白不提交、与真实模型写入并发时旧版本被拒绝、关闭→取回草稿→读取→重开、刷新同身份和草稿、取消、前进后退；375px页面和343px创作弹窗均无横向溢出。目录、地图和长记录仍可折叠；未显示摘要不推进观察位置。

[隔离恢复记录](/Volumes/DevDisk/DeveloperData/agent-world/evidence/frontier/playability/fiction-continuity/backup-restore.json)核对149条状态摘要一致，A/B/C原凭据读回同一人物、三次铃失败、新夹页与A同时持有铜盘和铜扣，B/C没有取得铜扣；三人位置与96条历史保持。当前唯一备份更新在私密目录，临时恢复副本已清理，没有恢复生产。两次正常升级前后分别137／140条状态摘要一致；此前快照仅证明各自旧状态。真实截图：[三人同场与可执行动作](/Volumes/DevDisk/DeveloperData/agent-world/evidence/frontier/playability/07-story-shared-world.png)、[实际新故事](/Volumes/DevDisk/DeveloperData/agent-world/evidence/frontier/playability/08-new-story-and-possession.png)、[375px同场页面](/Volumes/DevDisk/DeveloperData/agent-world/evidence/frontier/playability/09-narrow-shared-world.png)。有用来源、失败、提示词与最终证据保留；重复成功日志、空模型目录及恢复临时文件按同目录cleanup记录清理，运行环境与其他预览保留。

可直接[打开8811世界](http://127.0.0.1:8811/)，正常重启复用原库：

```sh
cd /Users/mubin/Developer/agent-world/repo
work/latency-runtime/bin/python -m examples.frontier_app \
  --db /Volumes/DevDisk/DeveloperData/agent-world/test-work/frontier-playability/data/world.sqlite3 \
  --port 8811
```

下面是此前不同快照的技术证据；它们不代替上面的实际用户路径，也不扩展当前交付范围。

## 已保留的技术流程验收（2026-10-06）

证据统一保存在 [frontier 验收目录](/Volumes/DevDisk/DeveloperData/agent-world/evidence/frontier/)，逐项判断见 [acceptance.json](/Volumes/DevDisk/DeveloperData/agent-world/evidence/frontier/acceptance.json)。模型、确定性测试与人工操作网页是三种不同证据。

| 范围 | 实际结果及边界 |
| --- | --- |
| 独立身份与共同生成 | 四个普通合成密钥保留各自目的与可见记忆。真实模型沿空出口创建地点，别人实际到访、拿取唯一铜筒并赠与同场景A；A从真实背包纠正旧记忆。当前模型世界有11个地点、100条历史；A/B/C/D为原四密钥，E为网页新建的第五身份。组织者没有填写地点、代判或补剧情；E入场由模拟普通用户点击网页完成。宿主不是新的用户身份。 |
| 活跃等待与本人决定 | D-wait/B-help 已验证原宿主等待收到新回答、重读现场、本人接受和结算；C只收到领取通知的旧轮次不算通过。最新C原徒手方法由B报价，C同钥新宿主恢复后接受一次，真实10+3=13/DC15失败保留；别人不能替本人接受。 |
| 封存的前期模型范围 | 原12宿主／136次MCP、发现正文4宿主／37次调用、物件4宿主／34次调用分别封存。共享发现正文已被B/C读到；缺失当前水位仍使当轮目标停住。原B/C开匣失败及旧牌没有正文的缺口保留，不补答案、不把后来成功改写成旧轮通过。 |
| 接口误用与自行恢复 | [peer-followup](/Volumes/DevDisk/DeveloperData/agent-world/evidence/frontier/peer-followup/acceptance.json)：4宿主／48次调用／7680已报告输出token。B先后遇到未知字段SchemaRejected与错误函数版本FunctionVersionMismatch，未形成报价；L6诊断与发现入口修复后，B新宿主自行发现正确合同，提交原方法报价，C恢复并执行上述真实失败。 |
| 真实物件解锁 | [visitor-followup](/Volumes/DevDisk/DeveloperData/agent-world/evidence/frontier/visitor-followup/acceptance.json)：2个Luna medium宿主／29次调用／4212已报告输出token。D开匣真实4+3=7/DC12失败；A同钥返回后真实10+3=13/DC12成功，拿取正确物件，不掷骰解锁，并生成、进入内岸折石台。 |
| 另一身份利用共同后果 | [continuation-followup](/Volumes/DevDisk/DeveloperData/agent-world/evidence/frontier/continuation-followup/acceptance.json)：2个Luna medium宿主／15次调用／2468已报告输出token。D不持钥、不重抽地通过已打开通路，继续创作带NPC的分汊石望地；B重启后同钥通过，向同地点身份发言。B曾把真实参与者ID交给NPC专用tell，被RuleViolation拒绝后自行改用say。新地点已保存，本轮没有检验其NPC后续对话。三轮共8宿主均已结束，额度封存。 |
| 模型与费用边界 | 原C缺姓名／职业未入场、提供方容量失败与B超时／报告输出超预算均保留失败。上述报告token与宿主结束不证明账单或任务成功；实际账单未核实，没有充值、改路由、隐私设置或额外订阅，也没有无限模型循环。 |
| 此前代码回归（c6d7705） | Python **501项／82.295秒无skip通过**；Frontier27项覆盖并发、幂等、权限、分页、发现、实际钥物交接／解锁与旧库／旧回执升级，新增接口回归覆盖诊断、发现正确合同与同Operation纠正后提交。8份Node测试与wheel构建、独立安装、网页资源、生成地点和第三方SDK probe通过。最终wheel为315447字节，SHA256为`d31f09a8494a1ebe92b38c092d3479d2d96b36fb8fbb13cff9fa1a13afd348c0`；测试总数不证明整体产品完成。 |
| 实际网页与权限 | 入场、导入时姓名职业、同钥恢复、失败反馈、取消保留草稿、重复点击只创建一次、前进后退与375px窄屏已检查。隔离网页完成真实物件赠与、解锁、另一身份通行；远程帮助者能看对应物件、ID和持有人。下载的只读连接读取200、写入403，撤销后401；网页离开不撤销外部权限。 |
| 读取断连与未知写结果 | 最新源码在模型世界实际停止／重启服务后，网页显示旧现场与读取中断提示，恢复读取后提示自动消失，A原地点和背包保留，135条状态没有改变。读取恢复不清除行动错误是当前代码行为，未据此声明所有未知写结果DOM组合已复测。此前隔离注入已提交写入丢响应，原Operation重试仅一个事件；45条真实模型历史从最近40条和缺口补全45条。 |
| 重启与备份恢复 | 最新两次模型世界正常重启前后分别119、133条状态摘要一致。[当前唯一备份恢复](/Volumes/DevDisk/DeveloperData/agent-world/evidence/frontier/browser-crossclient-followup/backup-restore.json)含157条状态，经隔离恢复和同A/B/C/D/E密钥正式接入读回：11地点、100条历史、各身份位置／物品／失败尝试／请求及已解锁关联与新石片正文保留。旧对象仍为not_authored；原81、109、135、144条恢复记录只证明各自旧快照。临时恢复环境已停止、清理，没有恢复生产。 |

[网页接入实验](/Volumes/DevDisk/DeveloperData/agent-world/evidence/frontier/browser-entry/acceptance.json)实际新建E“折风”，下载恢复文件与OpenCode配置，文件逐字节核对后移到私密目录；模型的 `OPENCODE_CONFIG` 直接读取原文件，未调用身份接入助手。两个有界LongCat宿主共14次MCP／1339已报告输出token，CLI记录成本0，实际账单未核实。首宿主在真正行动前报告length，4次读取后停止，原目标失败保留；第二宿主携本人可见记录同连接返回，使用8192的单步生成上限（原4096），20调用／240秒／4096已报告输出的观察边界不变。它按缩小的一段已有道路目标实际移动7次，到达A创作的柳背听水台；网页离开期间外部连接继续行动，同钥重进后实际看见同一人物的新地点，无维护者代操作或补剧情。本机启动说明补清找不到程序时用安装程序完整路径，未安装或配置新模型账户。公开资料的两次直连超时先保留，系统既有代理随后成功读取同一公开价格来源，模型路由和隐私设置不变。

[原目标接续](/Volumes/DevDisk/DeveloperData/agent-world/evidence/frontier/browser-crossclient-followup/acceptance.json)：后续两个 LongCat 宿主各用完240秒，合计11次调用，只完成3次真实移动，未生成新地点，原目标仍失败并封存。换成 Codex Luna medium 后继续使用 E 的原网页连接、原目标与本人可见记录：首轮在世界调用前报错，顶层诊断漏记，未推断错误原因；第二轮10次调用／92.015秒／1838已报告输出token，正式生成苇音石隙、进入并留话。没有缩小原共创目标或给模型填写生成包。

B 使用自己在独立网页 origin 导入身份后实际下载的原配置，按不同的到访目标自行找路，16次调用／68.422秒／1903已报告输出token；真实15−1=14/DC10成功，公开 E 创作时预存的回声石片正文，并当面留下反馈。B 未拿物、未开新路、未替 E 作答。E 的原网页接续与刷新后仍显示同一角色、地点作者、揭示者和反馈，[真实截图](/Volumes/DevDisk/DeveloperData/agent-world/evidence/frontier/browser/24-crossclient-revealed-stone.png)已保留。

[公开启动器的真实回访](/Volumes/DevDisk/DeveloperData/agent-world/evidence/frontier/public-codex-return/acceptance.json)：E 新模型进程经上方入口读回 B 的到访、揭示正文和感谢，5次调用／37.036秒／701已报告输出token，世界状态摘要不变。三个成功宿主没有世界工具错误；此前失败不会改成通过，CLI结束与报告token也不代表已核实账单。验证脚本曾误载旧安装 helper、误用历史分页参数，修正后五身份恢复通过；这些准备错误保留，不归为世界服务缺陷。

会话到期通过本地注入验证恢复，不等于自然跨日；五个合成身份不等于真人趣味验证。跨机器、长期负载、规模化内容矛盾处理、公平裁决、完整D&D均未通过。真实失败、等待或无人回应仍是世界状态，不能由维护者补结果。上述证据说明技术流程可用，没有证明完整持续游玩的体验成立；此前据此结束目标的判断已撤回。

## 规则来源

第五版数字规则切片和许可来源沿用 [DND_WORLD：规则来源与许可](DND_WORLD.md#规则来源与许可) 中已核实的 SRD 5.1／2014 第五版资料与归属声明。渡鸦溪的地点、对象类型、固定 DC、内容生成与参与式判断是本项目的原创世界适配，不引用官方商业冒险，也不宣称完整实现 D&D。
