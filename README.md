# 插件部分（Python · NEKO 插件）

**目录**：仓库根目录（Python 包），`plugin.toml` 声明入口 `STS2AutoplayPlugin`。

本插件协调尖塔2同伴的统一聊天、计划与动作会话：宿主提供最终主人格、背景、文字/语音队列和播放，插件调用同一agent模型回答及决策，Mod提供局面、求解和执行。当前事实取猫娘进程同一/state响应的agent_view、agent_facts与action_refs，统一入口的局末复盘进入当前会话，每局只自动表达一次。通过Mod HTTP API（`/state`、`/events/stream`、`/action`、`/danmaku`）交互；统一模型按角色读取总纲并按实际场景读取策略，条件经验负责纠正与偏好；旧局口号不再转成策略指令。

游戏期间保留本体启用的主动话题来源，经后台获取后进入统一同伴会话；点歌和搜表情包用独立媒体请求交给现有搜索/播放器，不等待搜索完成才出牌。搜索、派发和播放器真实状态分开记入上下文，普通闲聊和点歌保留有效游戏计划。情绪气泡可在桌宠设置的「情绪表情包」入口按角色上传多张图片，触发时随机选择并避免连续重复；空项使用默认猫图。

## 当前交付与验收（2026-10-15）

N1过程记录、N2最近回合自然追问、N3条件经验沉淀与采用已实现；102项相关Python检查及历史页JavaScript语法检查通过，机制验收完成。9个运行文件已同步实际安装目录并核对SHA256；上次交付时宿主未运行，正常启动加载新版。

你可以在现有聊天直接问“刚才为什么没防御/上回合为什么这样出”，不需要打开历史选中操作。实玩确认包括：最近回合解释是否准确、纠正后是否采用及修改/撤销是否符合原话、局末是否根据整局过程复盘，以及打法/配合/速度基线和原有升级取消。清单统一见[开发计划第2节](../nekospire-companion-plan.md)。

N4具体打法修正、N5进一步性能优化按记录中的实际问题继续选题。原历史页“经验采用与速度记录”提供采用统计和耗时样本数、中位数/P90，用于实玩对比。

## 入口 actions（`__init__.py`）

- **状态**：`sts2_health_check`、`sts2_get_status`、`sts2_read_state`
- **自动玩**：`sts2_start_autoplay`、`sts2_pause_autoplay`、`sts2_resume_autoplay`、`sts2_stop_autoplay`、`sts2_set_standby`
- **陪玩**：`sts2_enable_companion_mode`、`sts2_disable_companion_mode`
- **弹幕**：默认关闭；`sts2_set_danmaku`（`enabled=true/false`，真正的总开关，关掉同时通知 mod 停止渲染并清屏）
- **存档**：`sts2_continue_run`（回主菜单点「继续」回到存档；不可用时回报当前界面与可用动作）
- **co-op**：`sts2_open_coop_room`
- **策略**：`sts2_apply_user_override`
- **规划**：`sts2_get_planned_operation`、`sts2_execute_planned_operation`

## 核心模块（按职责）

| 模块 | 职责 |
|---|---|
| `service.py` | 总控：事件循环接线、决策/点评/记忆入口 |
| `loop_runner.py` | 轮询 + SSE 只读刷新；串行触发纯 LLM 自主动作循环 |
| `transport_client.py` | Mod HTTP 客户端（`/state`、`/action`、`/solver/plan`、`/danmaku`、SSE） |
| `neko_interface.py` | NEKO SDK 接口（只读/控制 SD 工具入口） |
| `llm_planner.py` | 战斗重判先求解一次，同一主人格模型选择求解器/自定计划、回答与发言 |
| `action_engine.py` | 决策边界：严格校验 LLM 动作，不替换非法选择 |
| `action_identity.py` | 卡牌/目标稳定实体绑定、现场下标解析、有限战术前提和取消诊断 |
| `companion_session.py` | 局内记忆：真实对话、带时间/战斗/回合的行动结果、未来回合意图，跨局重置 |
| `heuristic_planner.py` / `companion_evaluator.py` | 旧评估/策略兼容代码；不再作为自主决策来源 |
| `catgirl_llm.py` | 宿主 agent 模型客户端与当前角色主人格读取；旧点评接口仅保留兼容 |
| `play_style.py` | 游玩模式（认真/捣蛋/笨蛋）的档位常量、别名与"从有序候选里挑一个"的纯函数 |
| `catgirl_memory.py` | 战报、条件经验、版本/修改撤销、相关读取与实际采用结果 |
| `companion_journal.py` | 整局紧凑过程、两回合详细依据、重点案例与效果统计 |
| `strategy_repository.py` / `strategy_parser.py` | 兼容层加载角色/场景文档和玩家覆盖；旧局教训不自动注入 |
| `dispatcher.py` | 状态反馈 / 陪玩通知的 NEKO 输出边界 |

## 决策与表达

- **决策**：整局由本体 agent 模型主导。当前事实使用猫娘 `agent_view`，另附局内上下文与有来源的跨局经验；不接 `/coop/state`。战斗可行动且需要重判时，先调用一次自己的 `GET /solver/plan?mode=companion`，结果与 `solver_steps` 确定前缀交同一模型，默认选择 `plan_source=solver`；要求冲突或覆盖不足时模型可选 `custom`。程序原样采用被选中的求解器动作，不让模型重抄牌序。陪玩短搜索4秒、Mod总期限6秒，HTTP等待8秒、外围含刷新等待9秒；通常一次模型调用，格式修正最多再一次，总预算60秒。不暴露求解function tool，`function_calling_available=false`表示本路径未启用工具往返，不影响预求解。
- **选择**：普通事件和收益取舍交给模型。模型选定继续占卜后，自定义界面按原有接口暴露的合法下标推进，记录无效尝试；保留原有 Mod 信息，不修改或重编 Mod。接口未给出占卜选项时会报告缺项，不猜造下标。
- **表达**：文字/语音与游戏事件进入同一planner会话，单轮输出reply、plan_update和动作，沿用完整宿主人格，并把宿主生效主人/当前角色档案放入固定system，按档案称呼玩家。自动行动及纯游戏要求只保留一份绑定的speech，闲聊/询问用reply；数字步号清除。Flash使用简短、直接的任务prompt，每个可行动战斗回合首次出手用1–2短句表达主要目标、取舍与自然感受；同回合可继续交流，不设发言次数或计划节流，输出ID去重仍保留。采用求解器也保留模型的回合意图，speech_step_ids关联实际步骤，牌名/目标可用`{card:0}`/`{target:0}`绑定；安排改变时简短改口。必需发言缺失或绑定错误时沿用既有一次格式修正，仍失败则记录sts2_speech_deferred并继续校验、执行合法动作；已分类且更新动作的游戏要求也不因文案失败阻塞，闲聊/询问仍须回答。执行层不再补首牌模板；自然牌数、未选中的牌/目标及未经验证的收益数字只记sts2_speech_advisory，保留发言且不重试；显式引用仍校验。用药独立说明药水、对象及用途，已明确说过同一步时不重复，静音续步由实际药水与目标补句。speech与reply独立校验，战术语音播放完刷新校验，普通闲聊/回顾不挡无关续步。
- **等待与恢复**：局面/对话不变时复用等待决定，正常等待队友不报卡住；无进展会记录并报告一次，只有现场变化才清除。单次决策/派发失败退避重试，间隔最长8秒；连续3轮失败暂停行动，直接通知具体原因；文字真正送达后才清除，断线保留、重连补送，同连接最多3次投递，不调用模型。旧输入与事件不再触发重试，新输入仍可有限回应，明确继续/启动/切回program后恢复；checkpoint保留失败暂停及未送达通知；人物设置重建会话时重新接管当前连接。药水满栏时选择丢旧药再领取或离开，避免无效领取。缺少对话总线时跳过导入并显示说明；缺少 `agent_view` 不发动作并报告。
- **记忆与规划**：即时会话保留最近16条真实发言和20条行动；另由`companion_journal.py`按局保存整局紧凑过程、最近两回合详细依据及重点案例，重载后保留。玩家可直接追问当前/上一回合，原理由、实际结果和事后评价分别保存。结构化经验按角色、牌/遗物、遭遇和资源条件最多读取3条，附来源及近期真实采用结果，支持修改/撤销；旧战报仍按角色和时间保留兼容读取。局末自由反思作为评价独立保存。当前事实和有效要求优先；结算按is_victory，胜利结算归零不计实际损血。
- **计划复用与耗时**：主动作与最多4个续步按card_ref/target_ref绑定entity_id，派发时解析现场下标，模型不预测删牌后的手牌line。固定集火、击杀血量范围、血量顺序/差及防守前提按模型所选条件校验；队友削血但前提成立可继续，目标缺失或新信息停止依赖尾部。规划时保存enemy_intents，存活敌人意图变化报enemy_intent_changed并重判，忽略血量与位置变化。agent_facts明确是否攻击及已知来伤，击杀后按最新意图重算防守。有效续步去掉1.5秒空等，自己ready_to_end_turn完成后等待队友。decision_metrics、latency_samples和取消诊断用于下一局比较实际效果。
- **求解器续步**：只采用当前回合、当前手牌可绑定的前缀；同种相同牌面分配不同实体，不把重复初始下标当成同一张牌反复使用。不同升级/临时牌面无法确定身份时停止前缀，sts2_solver_prefix记录停止原因；药水缺稳定引用时交模型按现场处理。抽牌、选牌、未知卡牌及结束回合处停止尾部。`solver_assumptions`按步骤附玩家打法的必要现场前提。有效续步不再求解、不调用模型；结束续步还有可打0费实体时统一复核一次，end_turn须逐实体说明不出的取舍，允许遗物格挡等理由。日志分开记录`solver_used`（有成功参考结果）和`solver_adopted`（采用牌序），sts2_solver_line记录原始首回合牌序。
- **取消诊断**：`sts2_observation_changed`记录最多6个变更字段路径及前后值，与revision/step关联；保留自身资源、界面和敌人效果的派发校验，不能将真实变化统称为续步模型错误。
- **API缓存**：固定同一会话system（工具集合为空），完整请求历史追加，动态局面与求解结果放末尾并稳定序列化；超过80,000 prompt token后按完整轮次成块裁剪。剔除run.piles与求解结果重复的现场/引用；重载可恢复checkpoint。缓存字段按`prompt_cache_hit_tokens`、`cache_read_input_tokens`、标准`cached_tokens`优先读取，日志记录source与完整usage。请求复用宿主astream，include_usage开启，完整JSON收齐后再校验动作；sts2_stream_complete记录首字与总耗时。HTTP追踪记录实际stream、system_hash、parameters_hash、共同消息数/字符数和请求ID，随机关联tag仅在header，正文和凭据不记日志。当前只排查命中率，计价暂缓。
- **输入分类**：模型仅使用短消息id填写message_effects；已知传输request_id可转回对应id，旧消息附加项过滤，当前新消息分类必须完整。格式修正反馈列出实际所需id，避免笼统报错反复卡住合法动作。
- **战斗就绪**：卡牌离手能证明已执行，但动画与Mod的200ms稳定闸还可能阻止下一步；此时保留续步并等待动作重新可用，有新输入仍可回答。ready_to_end_turn仅在COMBAT有效，奖励页不沿用旧战斗的结束标记。
- **暂停讨论**：Mod 自主驱动 hold、插件执行权限、宿主聊天彼此独立。讨论结束保留原控制模式和玩家运行意图，期间玩家主动暂停仍有效。

## 弹幕/点评

本期交流由统一planner决定，宿主负责渲染与播放；旧独立点评接口仅保留兼容。游戏内弹幕开关与动作执行、宿主聊天分别控制。

### 怎么真正关掉弹幕（v0.3.1 起）

`catgirl_llm_enabled=false` **不是**关弹幕——它只是把 LLM 点评降级成启发式文本，弹幕照旧。要关弹幕用：

| 方式 | 说明 |
|---|---|
| `sts2_set_danmaku(enabled=false)` | 插件侧总开关：`_push_catgirl` 直接早退（不生成、不拉头像、不推），并 `POST /config {danmaku_push_enabled:false}` + `POST /danmaku/clear` 清屏 |
| `plugin.toml [sts2] danmaku_enabled = false` | 同上，随插件启动生效 |
| 游戏内 Mod 设置「允许游戏内弹幕渲染」取消勾选 | 关闭 mod 侧的 `danmaku_push_enabled`：任何本地调用者推的弹幕都不再渲染，并立即清屏 |

三个开关分工：`danmaku_enabled`（插件侧推送）、`danmaku_push_enabled`（mod 侧 overlay 渲染，唯一收口在 `DanmakuService.PushAsync`）、`catgirl_llm_enabled`（点评用 LLM 还是启发式文本）。自动游玩与状态读取都不受影响。

## 地图投票与存档（co-op 行为）

插件接管猫娘后持续保持 Mod `hold`，等待正在执行的 Mod 动作结束再调用模型，避免两个驱动同时操作。猫娘自己按当前地图视图投票，玩家投票和聊天可以影响模型判断；已经投票时等待队友，不靠保守评分或自动跟票替代模型。

`sts2_set_route` 将路线讨论写入局内对话，不直接投票。`map_policy` 和带锚点的 guidance 一起进入模型上下文，由 LLM 判断；不使用启发式替换模型投票。

- **回到存档**：主菜单有可继续存档时，插件主菜单决策**优先 `continue_run`**（以前优先 `open_character_select`，会抢在存档前面开新局）；猫娘进程则**完全不碰存档**（两个进程共用 `user://`，谁点「继续」谁就把存档吃掉），只保留重新加入房间的能力。`sts2_continue_run` 是显式的补救入口；`continue_run` 现在会先收干净主菜单子菜单（子菜单开着时「继续」按钮会被藏起来，以前会直接 409）。
- **防呆**：主菜单存在可继续存档时，`start_coop_session` / `start_multiplayer_host` 会返回 `409 active_run_present` 而不是默默另开一局（`POST /config {coop_yield_to_saved_run:false}` 可关闭这道守卫）。
- **已知限制**：游戏本体看起来没有「客户端加入进行中局」（join-in-progress）。即使主机点「继续」读回 co-op 存档，AI 队友也可能回不到那一局——这是游戏侧能力边界，不是插件能绕过的。

## 控制档位与游玩风格

| 档位 | 行为 |
|---|---|
| `program` | 自主判断并执行自己的合法动作 |
| `advise` | 所有界面只判断，不自动执行 |
| `standby` | 停止自主动作与规划，仍可只读查询和聊天 |

`sts2_start_discussion` / `sts2_end_discussion` 用于暂停与恢复动作，讨论期间不要求逐步批准。`sts2_pause_autoplay` 同样停止动作，聊天继续。`sts2_execute_planned_operation` 可显式执行一步，但不能绕过讨论暂停。

插件接管期间各档位都向 Mod 推 `hold`，执行权限由插件单独决定。暂停不会把控制权交回 Mod。

`serious` / `mischievous` / `silly` 作为风格输入交给模型理解；插件不再按概率替换模型选择，也不按候选评分扰动动作。

## 插件驱动的是哪个进程（host / catgirl）

这是 coop 里最容易搞混的一件事：**端口不区分"模式"，区分"哪个游戏进程"**。

| 端口 | 是谁 |
|---|---|
| **18080**（`HttpServer.DefaultPort`，`STS2_API_PORT` 可覆盖） | 单人局里那唯一的实例；**coop 里是主机（真人玩的那个）** |
| **18081**（`NekoConfig.coop_client_port`） | coop 里由 mod 用 `STS2_API_PORT=18081` 拉起的**猫娘进程** |

所以插件有"活动实例"的概念，`sts2_set_instance` 可以查看与覆盖：

| 值 | 含义 |
|---|---|
| `host` | 配置里的 `base_url`（默认 18080）。**开房必须发给它**——只有主机能拉起猫娘进程 |
| `catgirl` | 猫娘进程。插件驱动游玩时读状态、下动作都对着它 |

**默认是自动的**：`sts2_open_coop_room(host=True)` 成功后，插件会向主机读 `GET /config` 的 `coop_client_port`，据此推导猫娘地址，**轮询等它冷启动**（最长 45 秒），起来就切过去接管。

接管时给猫娘实例推 `coop_control_mode="hold"`，避免 Mod 与插件同时行动。开房后 Mod 负责拉起进程、进入房间并停在角色选择界面；由你手动选择猫娘角色并确认 ready/embark，插件和 Mod 不代点。新版插件会校验手动选人能力回执。Mod hold 在当前进程内保持，插件重新连接后同步；仅切换面板管理实例不改变控制状态。

同伴认知、查询与动作固定使用猫娘进程。面板活动实例只影响管理操作；猫娘连接中断时明确报错，不能改去操作真人主机。

注意 `GET /config` 里的 `coop_client_port` 是 **mod 0.1.4** 起才有的只读字段。老版本 mod 没有它时插件**不猜端口**，会停在主机实例并如实说明——单人流程不受影响。

## 数据流

```
宿主文字/语音队列 + 最终人格/背景 ─┐
猫娘/state → agent_view/facts/refs ─┴→ 插件统一调度与CompanionSession
  → 战斗重判先求解一次，绑定当前回合确定前缀
  → 同一LLM会话：回复 + 保留/更新/等待计划 + 选择solver/custom
  → 绑定实体及具体发言 → 宿主output镜像/TTS
  → 刷新，检查新输入/控制/前提，按实体解析当前下标 → 校验 → /action
  → 确认真实结果 → session/checkpoint → 有效续步直接执行，否则重新判断
```

宿主接线位于NEKO源码main_routers/sts2_companion_router.py及web_app注册；需源码后端。
接口为/api/sts2-companion/poll、output、release，默认unified_session_enabled=true。
角色+实例+90秒租约限定所有权，重载保留未确认输入及会话，旧动作计划不直接重放。
完整部署与待验收状态见[AGENTS.md](AGENTS.md)和[开发方案](../nekospire-companion-plan.md)。

## 使用教程

### 获取MOD

1. 订阅并启用前置模组 **STS2-RitsuLib**（创意工坊 `3747602295`；`mod_id.json` 建议声明 `min_version`）。
2. 订阅**猫娘尖塔 NekoSpire** [创意工坊](https://steamcommunity.com/sharedfiles/filedetails/?id=3794941932)。

### 启动插件

1. 在 NEKO 里启用猫爪 + 本插件。插件连接时会通过 `POST /config` 关掉 Mod 自己的 LLM/弹幕，把解说交给 NEKO 侧猫娘。
2. 用插件的 `sts2_start_autoplay` 等入口控制自动玩，`sts2_open_coop_room` 打开 co-op 房间。


# Mod 部分（C# · 游戏内 Godot Mod）

**目录**：`game_mod/nekospire/`，编译成 `nekospire.dll` 随游戏加载。默认监听 **18080**（环境变量 `STS2_API_PORT` 可改）。

本 Mod（NekoSpire）是尖塔2 的猫娘陪玩桥：内嵌 **Combat Solver** 的战斗求解脑，通过 HTTP 把游戏状态与战斗路线暴露给 NEKO 插件，并自带游戏内弹幕渲染与 co-op 自动玩的底层驱动。它只提供**可执行**能力（出牌/状态采集、求解决策、弹幕渲染、本地自动玩），**不读取**策略文档；插件兼容层读取角色策略及玩家覆盖。

## 主要功能

**跨回合战斗求解（`GET /solver/plan`）**：内嵌 **CombatSolver 搜索/模拟核心**，不只算眼前一回合——继续预测抽牌、洗牌、敌人行动与后续资源。它在 game 线程捕获**实时** Play 阶段状态（当前回合真实手牌），把预测路线按回合分组输出，当前回合的 `card_index` 直接落到真实手牌位置。

**co-op 与独立玩家驱动（`NekoAutoplayDriver`）**：猫娘作为独立玩家 = 双进程，各进程 Mod 驱动自己本地 player。战斗用 `/solver/plan` 的下一步；地图/奖励/选牌/事件四屏先问 LLM，LLM 不可用或返回非法动作则退回各屏启发式。每个候选都用游戏自己的 `available_actions` 把关，绝不发出非法动作；非客户端/非 co-op 进程保持安静，宿主不受打扰。

**游戏内弹幕 overlay（`POST /danmaku`）**：猫娘解说直接在游戏内渲染（内置 Godot 节点 + `ProcessFrame` 信号，非 `_Process`），可配置字号与头像；弹幕与决策 LLM 解耦，`danmaku_enabled` 独立开关。所有来源都收口在 `DanmakuService.PushAsync`，由 `danmaku_push_enabled` 运行时门控（本地任何调用者推的弹幕都受控），`POST /danmaku/clear` 负责清屏。

**HTTP 状态桥（`/state`、`/events/stream`）**：一次性导出完整局面快照；屏幕切换 + 战斗状态变化经 SSE 实时推送，由真实游戏切换驱动，而非纯轮询。

## 独立运行：co-op 启动流程

不带 NEKO 插件、由 Mod 自配 LLM 独立运行时，猫娘作为独立玩家的 co-op 由 Mod 自己驱动。**双进程模型**：一份是 host（真人玩家手动玩），另一份是猫娘 client（自动玩）。下面以「游戏内一键开房」为主流程，另附两种启动脚本/Steam 路径。

### 方式一：游戏内一键开房（推荐 · 生产 UI）

1. **配 LLM**。主界面 → 设置 → 管理MOD → 选择 NekoSpire → 打开 NekoSpire 设置（游戏 Mod 详情页 NekoSpire 设置按钮）→ 填 `Base URL` / `API Key` / `Model`，勾「启用 LLM 决策（地图/奖励/事件/卡组由 LLM 决定）」，按需勾「启用猫娘弹幕点评」→ 保存。这是猫娘做决策与弹幕解说用的。
2. **主菜单开房**。点「开始 co-op 房间」。Mod 依次：
   - 置 `coop_enabled = true` 并存盘（`user://NekoSpire/settings.json`）；
   - 关掉主菜单子菜单后 `open_multiplayer_menu` → `start_multiplayer_host`（ENet `33771`）；
   - **拉起第二个游戏进程**（猫娘 client），环境变量 `STS2_API_PORT=<coop_client_port·默认18081>`、`STS2_ENABLE_DEBUG_ACTIONS=1`（同一 exe，**不带** `+connect_lobby`）。
3. **猫娘进程自动接棒**。它读到同一份 `settings.json`，本进程端口 == `coop_client_port` → `IsClientByPort()` 判定自己是猫娘，`NekoAutoplayDriver` 启动：主菜单 → `open_multiplayer_menu` → `join_multiplayer_direct`（连 host `127.0.0.1:33771`）→ 进入大厅后停住，等待你选择角色并手动 ready/开局；确认后才开始地图互投。
4. **host 手动玩，猫娘自动跟随**。战斗用 `/solver/plan` 的下一步；地图/奖励/选牌/事件四屏先问 LLM（第 1 步配的那个），LLM 不可用或返回非法动作则退回各屏启发式；每个候选都以游戏自己的 `available_actions` 把关，绝不发非法动作。
5. **弹幕解说只在 host 进程**渲染（`NekoDanmakuDriver` 用 `IsCatgirlProcess()` 在猫娘进程跳过），所以猫娘进程专心地当队友、不刷弹幕。

### 方式二：启动器脚本 `Start-NekoCoop.ps1`（调试 / 自动化）

复用 `start-game-session.ps1` 拉起 host + client 两个实例（不同 API 端口、均 debug），再通过 HTTP `/action` 引导会话。默认进「multiplayer test」调试场景；`-ClientAutoplays` 让猫娘 client 自己 join/select/ready/vote；`-EnterCombat` 让两边投同一节点进第一场战斗并等 `COMBAT`。

```powershell
scripts\Start-NekoCoop.ps1 -ClientAutoplays -EnterCombat              # 全自动
scripts\Start-NekoCoop.ps1 -SkipHostLaunch -ClientAutoplays           # host 你已从 Steam 手动开好
```

### 双进程要点

- **谁算猫娘**：本进程 `STS2_API_PORT`（或 `STS2_COOP_PORT` 覆盖）== `NekoConfig.coop_client_port`（默认 18081）；或设了 `STS2_CONNECT_LOBBY`。其余实例一律视为玩家，保持手动、安静。
- **共享配置**：host 与猫娘是同一安装、同一 `user://NekoSpire/settings.json`，host 开的 `coop_enabled` 猫娘也读得到。
- **不劫持宿主**：运行中发现 `multiplayer_lobby.is_host` 或 `net_game_type == "host"` 时，自动玩立即禁用并告警（防线接错）。


## 依赖与兼容性

- 需要 **RitsuLib 0.5.18+**；当前适配《杀戮尖塔 2》0.111.0（net9.0 / C# 13）。
- 搜索受 Short/Deep 时间预算与分支上限约束，展示的是预算内找到的最佳路线，不承诺数学意义全局最优；**不修改游戏 RNG**。
- 卡效推断依赖 RitsuLib（消除 `unsupported` 卡模）；对游戏私有成员的访问用 **GameRef 反射**（不依赖编译期/运行时 publicize，无 Krafs.Publicizer）。

## 性能说明

`/solver/plan` 是短预算搜索（默认 Short 8s / Deep 120s；Deep 仅在 Short 无法解决时触发），求解在 worker 线程后台运行并周期性让出 CPU。自建 `Game/Sim/` 确定性结算器仅当 `SolverEnabled=false` 时作为回退；求解器不可用时自动玩退回「第一张可打出的牌」，避免卡死。

## 有界重试与失败暂停

自动玩**不会因为一次错误就停手**，这一条是被真实的"猫娘站着不动"逼出来的：

- 单次错误保留运行意图并退避；连续3轮决策/派发失败则明确设为paused，pause_reason=decision_failure。每轮模型最多一次格式修正，合法动作不因附加文案持续失败而阻塞。暂停原因直接通过已有输出/TTS通道投递，不再要求失败中的planner生成解释。读状态成功不清空决策错误；新输入可获得有限回应，动作等待明确恢复，重载继承失败暂停。
- **动作被拒 = 状态漂移，不是致命错**：`invalid_target`（例如整回合缓存的战斗计划里
  `target_index` 指向已经死掉的敌人）这类拒绝会自动作废缓存的 `/solver/plan`、强制重刷 `/state`
  后按现场重算；退避上限8秒，连续3轮仍失败则暂停等待指令。
- **战斗计划缓存签名含"存活敌人集合"**：回合内死一只敌人就重取计划，避免继续用过期下标。
- **发牌前最后一道兜底**：`action_engine` 会用现场 `hand[i].valid_target_indices` 校正越界的
  `target_index`（拿不到合法集就不猜，保持原样）。
- **选牌屏光标只由"真的按下去了"推动**（`note_selection_option_pressed`）。`plan()` 是每 tick 都跑的
  （事件流约 1 次/秒），若让规划自己推进光标，几个候选会在几秒内被白白耗光、屏幕却一张都没选中，
  随后永久返回 `None` —— 猫娘就永久停在那一屏。整屏按遍仍不推进时先让一次 `None`（留确认/离开的
  出口），再重置光标重按，保证不会死锁。
- **点评 LLM 只在四道闸门都过了之后才调用**：否则每次同步都白烧一次生成再丢掉结果
  （实测日志里约 1 次/秒的 `result=None` 就是这么来的，接真 LLM 时白花 token）。
- **商店不会反复开关货架**：`open_shop_inventory` 是**开关**，而开关两态的 `screen` 都叫 `SHOP`，
  只看"动作在不在册"就会 开→没值得买的就关→又看见开→再开，无限来回。现在一次进店只开一次货架
  （用只在开着时出现的 `close_shop_inventory` 判断"已经看过货了"），逛完就 `proceed` 走人。
- **选人界面会开局，不是一直重按角色**：`_selected_character_index` 取的是 `selected_character_id`
  的下标 —— 也就是**当前已经选中**的那个角色，所以 `select_character(那个下标)` 是 no-op：
  屏幕不推进 → 下一 tick 又发 → 无限重按，猫娘永远停在选人界面（死后重开一局必踩）。
  现在角色已选好就直接 `embark`。同一类错误（**幂等动作被当成推进手段反复发**）在商店也出现过，
  排查时可作为一条通用线索。

## 旧启发式地图选路（历史实现）

以下描述仅供旧代码维护参考，本期自主循环不使用这些评分。

选路不是"只看眼前一步"，而是**从每个候选节点递归到 boss，评估整条路线**。

### 为什么之前不是

旧代码也写了"前瞻"（`_score_map_node` 里累加 `_collect_route_descendants`），但它是**假前瞻**，
两个原因叠在一起让前瞻恒等于 0：

1. 交给规划器的 `available_nodes` **没有 `children` 字段**；就算读全图 `map.nodes`，它的
   `children/parents` 是 **`{row,col}` 坐标对、不带 `node_type`**，而旧代码取的是
   `descendant.get("node_type")` —— 空串，权重 0。
2. `summary_context_builder` 读的 `game_map.future_nodes`，**mod 从来没下发过这个字段**。

### 还有一条更要命的类型不匹配

mod 下发的 `node_type` 是**游戏枚举名**（`Point.PointType.ToString()`）：
`Monster / Elite / Shop / RestSite / Treasure / Unknown / Event`（+ `Ancient`、`Boss`）。
而权重表的键是 `rest / shop / treasure / event / question / monster / elite` ——
**`RestSite` 与 `rest` 拼不上**，于是休息点权重恒为 0。再叠加 `_preferred_map_index` 过去那条
"分数必须 > 0 才采纳"的判断，结果就是：**怪物（3 分）永远压过营地（0 分），猫娘从不主动回血。**
现在用 `_MAP_NODE_TYPE_ALIASES` 做显式映射，并有单测逐一钉住这些取值。

### 评估口径

- 用 `(row, col) -> node` 索引把 `children` 坐标解成真正的图；从每个候选递归到 boss。
- **分支点取 max**：到那一层我们还会再选一次，所以是"沿途都选最好"，不是求和。
- **逐层折扣** `ROUTE_DISCOUNT_PER_ROW = 0.8`：远处的不确定性更大。
- **同类饱和** `ROUTE_REPEAT_DECAY = 0.5`：两个休息点 ≠ 两倍休息。
- **临门加权** `ROUTE_BOSS_PREP_MULTIPLIER = 2.0`：距 boss ≤ 2 层的休息/商店价值远高于中途
  （补血、把金币花掉）；金币不足 `ROUTE_BOSS_PREP_GOLD` 时临门商店不加权。
- **精英随血量缓和** `ROUTE_ELITE_RELIEF_MAX = 0.5`：满血时惩罚打对折（精英给遗物+金币，未必是坏事），
  但**不清零**；血少时用低血表，惩罚反而更重。
- **低血替换对自动选路也生效了**：过去只有玩家手点的 `sts2_set_route mode=safe` 才带
  `conditions.low_hp`，自动选路拿到的是空策略，血再低也不会整体转向休息。
- 深度：`route_policy.lookahead_depth`（缺省 `ROUTE_LOOKAHEAD_ROWS_FALLBACK = 6` 层）。
- **拿不到图就退回原来的单步评分**，行为与旧版一致（旧 mod / 非地图 payload 不会因此变差）。
- 每次决策用 info 级记一条 `[sts2_map_route] 金币=… 血比=… 候选评分=… -> 选 option_index=…`，
  同一局面只记一次 —— 选路是玩家最容易质疑的决策，必须留痕。
  （注意：本文件里的 `_debug` 是个**空函数**，所有 `[sts2_*]` 调试行其实都没有输出。）

## 同一族故障：动作"成功"但状态没变 → 无限重发

实测踩到四次，形态完全一样：**某个 `screen` 需要一个规划器不认识的中间动作，或者某个动作被 mod
接受却不产生状态变化**，于是每 tick 重发同一步，界面永不推进 —— 而插件看起来一切正常
（`autoplay_state=running`、租约 attached、`step_count` 还在涨）。认这条线可以省很多时间。

| 界面 | 病因 | 修法 |
|---|---|---|
| `SHOP` | `open_shop_inventory` 是**开关**，开关两态的 screen 都叫 SHOP，关了又开 | 一次进店只开一次货架 |
| `CHARACTER_SELECT` | 只发 `select_character`，而下标取自"当前已选中"的角色 → no-op | 角色已选好就 `embark` |
| `CHEST` | 流程是 `open_chest` → `choose_treasure_relic` → `proceed`，旧分支不认第一步 | 先开箱（实测卡 6 分钟以上）|
| `REWARD` | 药水栏满时"领药水奖励"发不出去，mod 照样回 `completed` → 重领同一个下标 | 药水排最后 + "已真的发过"的下标不再重发，都领不动就推进 |

**排查手法**：手工 `POST /action` 一次怀疑的动作，看返回里的 `state` 有没有真的变
（`/state` 的 `screen`/`selection`/`reward.rewards` 是否变化）。一变就说明"该发的是这个动作，
只是插件没发/发错了"；不变就说明动作本身在这条件下是空转。

**两条通用护栏**（都是被这些坑逼出来的）：

- 凡是"记住已经试过哪一项"的状态，**必须由"真的执行了"推动，不能由"规划了"推动** ——
  `plan()` 每 tick 都跑（事件流约 1 次/秒），执行约 2.5 秒一次，用规划推进会在几秒内把候选白白耗光。
- 候选试遍仍不推进时要有**重置/推进出口**（选牌屏重置重按、奖励屏改发 `collect_rewards_and_proceed`），
  绝不允许"规划不出动作"成为稳定终态。

## 弹幕双线：即时短句 + 高风险详解（v0.6.2 起）

**问题**：点评是等 LLM 生成完才推的，而主系统用**推理模型**。实测每条生成间隔：

| 场景 | 平均 | 最大 |
|---|---|---|
| combat | 5.0s | 39s |
| reward | 7.9s | 27s |
| map | 7.1s | 20s |
| selection | 6.2s | 23s |
| event | 9.6s | 24s |

5~10 秒足够换场景，所以玩家看到的是"上一个场景的弹幕"。**调 prompt 解决不了**，只能改架构：

1. **即时短句**（`_emit_instant_catgirl_line`）：完全不调 LLM，用手里已有的信息拼一句话立刻推。
   按 `场景|层|回合` 去重，事件一变就发。战斗里报"出牌建议 + 敌人意图 + 我方血量"
   （出牌建议只读 `_solver_plan_cached`，**绝不在这里等 HTTP**）。
2. **高风险长讲解**（`_should_emit_long_commentary`）：战斗里只在血比 < 50%、
   或敌人这一下的意图总伤害 ≥ 当前生命的一半时才再让本体生成；地图/商店/奖励等非战斗界面照旧讲
   （它们本来就被 `should_comment` + 指纹去重挡着，而且"讲解局势"主要就发生在那里）。
3. **迟到即丢弃**：生成完对比 `last_instant_line_key`，画面已换场景/回合就整条丢掉
   （日志 `讲解迟到已丢弃`），不再让上一场景的内容冒出来。

**两个容易忽略的细节**：

- 即时短句推弹幕时**不等待拉头像**（`fetch_avatar=False`）：拉头像是一次最长 3 秒的 HTTP，
  等它回来"即时"就没了；即时线只用已缓存的头像，没有就纯文本先发。
- 「讲解由谁生成」：SDK 只有单向 `push_message`，**没有读回本体回复的接口**，
  所以讲解文本由插件用**本体的模型 + 本体人设**生成（`catgirl_llm._persona_system`）；
  同时用 `push_message(ai_behavior="read")` 把**简化后的局势速报**喂给本体，让它自己也知道猫娘在打什么。

## 局末总结与反思（v0.6.1 起）

正常游玩时持续记录整局紧凑过程和最近两回合详细依据，重点案例另行保留。过程保存在`catgirl_memory.json`旁的`run_process/`中；即时会话的最近20条动作不再承担整局归档。

- **战报事实：** 胜负、角色、进度、逐房间选择与收获、生命观测和实际动作。重载保留过程，采样间隔保留标记；联机前后变化按观测区间记录。
- **局末复盘：** 统一模型沿用当前会话，读取覆盖整局的房间汇总、最多12个重点回合、3个重点案例和6条最近问答，自动表达一次；玩家新问题照常回答。
- **评价与经验：** 反思文字独立保存；可复用结论经`learning_updates`提炼为带条件、来源和版本的经验，模型自评标记为候选。整段反思不再直接追加为长期策略。
- **玩家纠正：** 明确纠正和讨论结论无需每次说“记住”，按原话范围区分当前要求、案例纠正与长期偏好；支持修改/撤销，后续相关局面少量读取并记录实际采用及回合结果。

### 损血归因按**房间标识**，不是层数（v0.7.0 起）

玩家指出的问题：复盘说"第 27 层掉了 40 血"**没有用** —— 尖塔地图是随机的，同一层在不同局
根本不是同一个房间，这种记法无法跨局复用。

现在每条生命采样都带一个**房间标识**（`_room_identity`）：

| 房间 | 标识来源 |
|---|---|
| 战斗 | `combat.enemies[].enemy_id` 的组合，如 `LEAF_SLIME_M+LEAF_SLIME_S` |
| 事件 | `event.event_id` |
| 篝火/商店/宝箱 | mod **没有 id**，退化为 `room:REST` 这类房间类型 |

两个实现细节：

- mod **没有** encounter/room id，所以战斗用"敌人组合"当遭遇标识（足够区分"普通史莱姆群"和"精英"）。
- 一场战斗的标识在**遭遇开始时就钉住**（`_state.combat_room_id`，离开战斗清零）。战斗中敌人会死，
  若每帧重取，"同一场战斗"会被拆成好几个房间，归因就散了。

战报与历史页的"最痛的几处"现在都显示房间标识，层数只作为整体进度出现（`打到第 N 层`）。

### 路线图：逐房间的损血与收获（v0.7.1 起）

玩家口径：「需要路线图，对应每个房间的损血和收获」。每局记录里多一条按访问顺序的 `route`，
每个房间写清：**掉了多少血**（进房间的血 → 过程中最低血）、**拿到什么**（金币差 + 新增卡/遗物/药水，
按 id 差集算）。历史页把它渲染成"一行式序列 + 逐房间表格"。

两个判定要点：

- **奖励类界面并入上一个房间**：奖励是"这个房间打完之后"发的（战斗 → REWARD → 选牌 → REWARD → MAP），
  另起一段就看不出"这个房间值不值"。合并时**保留原房间的身份**，否则这段会被记成 `room:REWARD`，
  路线图上就看不出是打哪一场拿到的。
- **地图走一格不算房间**：没有损血也没有收获的 `MAP` 过渡段直接丢弃，否则路线图全是噪声。

顺带一提：`route`（路线图）与 `hp.heaviest`（最痛几处）是两套互补视角 ——
前者是"整条路怎么走的"，后者是"哪里最疼"。

### 篝火：默认优先锻造，而不是回血（v0.7.0 起）

玩家指出的问题：插件偏爱休息回血。锻造能**升级卡牌**（升数值、降费用），对战力提升很大，
应该优先考虑。

旧实现是 `_preferred_route_option(preferences) or 0` —— 要么用**路由**偏好（与篝火选项无关），
要么直接取下标 0，也就是"列表第一项"，而第一项通常就是回血，所以事实上永远在回血。

新口径（`_preferred_rest_option`）：

1. 玩家显式配过的偏好优先（`records[].value.preferred_option_index`）；
2. 血比 < `REST_HEAL_HP_RATIO`（0.6）→ 回血优先（快死了还去锻造就是送）；
3. 否则**锻造/升级优先**（关键词 `smith/upgrade/forge/锻造/升级/淬炼`）；
4. 认不出类型就取第一个**可用**选项（不看 `is_enabled=False` 的）。

类型判断读的是 `option_id` / `title` / `description` —— mod 的 `RestOptionPayload` 发的是
`option_id`，而 `summary_context_builder` 原来读 `type`/`name`，于是 `rest_option_types`
**一直是空的**（又一处形状错配）。现在也一并修了。每次决定用 info 级记一条
`[sts2_rest] 选项=[...] 血=…/… -> 选 option_index=…`，方便玩家核对"为什么又回血了"。

## 角色：玩家手动选择

进房后停在选人界面。你手动选择猫娘角色并确认 ready/embark；插件只读取确认后的角色，不替你操作。

## 自动游玩历史页（v0.6.1 起）

插件 UI 新增面板「自动游玩历史」（`static/history.html`），每局展示：结局/角色/层级、损血合计与
**最痛几处**、教训与复盘、以及**逐条选择**（步数/界面/动作/理由）。新增“经验采用与速度记录”，展示匹配回合、读取、关联动作/完成，以及按局、配置和处理路径分组的耗时样本数、中位数/P90。

数据通路：前端页面是插件服务器按静态文件发的（`GET /plugin/{id}/ui/<path>`），**没有直读插件内存的通道**，
所以启动、局末及复盘纠正后把战报、经验与效果报告投影成 `static/autoplay_history.json`（`history_view.py`），
页面 `fetch` 同一个静态目录下的它。权威数据是 `catgirl_memory.json` 和相邻的 `run_process/` 按局记录，快照只是视图缓存
（升级插件被覆盖也无所谓，下次局末会重写）。

## 多语言的一个大坑（v0.6.4 起）

奖励屏弹幕实测念出英文 `Prefer rewards that patch the weaknesses of the current deck.`。
根因不是缺译文：SDK 的 `PluginI18n.t()` 在**不传 locale** 时只按 `default_locale` 查表，
而 `default_locale` 的默认值是 **`"en"`**（`plugin/sdk/shared/i18n.py` 的 `DEFAULT_LOCALE`），
于是**凡是 `en.json` 里存在的键，无论玩家用什么语言都说英文**；只有 en 里没有的键才会退回代码里
写的中文 `default` —— 所以同一屏里会半中半英（地图那条是中文、奖励那条是英文，就是这个原因）。

插件侧拿不到运行时语区（SDK context 不暴露 locale），因此在 `plugin.toml` 里显式钉住：

```toml
[plugin.i18n]
default_locale = "zh-CN"
locales_dir = "i18n"
```

并补齐了 7 种语言缺的 `companion.suggestion.reward_default`，避免英文漏到玩家眼前。

## 求解器请求期限

陪玩重判请求 `/solver/plan?mode=companion`：短搜索与Smart药水审计共享4秒预算，总期限6秒，取消token贯通后台搜索。求解请求串行，排队计入期限；插件HTTP等待8秒、外围含刷新等待9秒。成功或失败结果按现场与要求版本缓存，格式重试不重复启动搜索。有效续步直接执行。未带mode的独立求解仍使用原有8秒Short/120秒Deep预算。

## 复盘期间暂停自动游玩（v0.7.2 起）

统一入口启用时，局末结构化摘要只投递一次，由planner沿用当前会话表达并存入复盘；不启动旧的独立生成与暂停/恢复链。自动结算输出使用`run_end:<run_id>`，checkpoint保留已发言局号，界面或控制变化不再重复生成；玩家的新输入仍可正常回答。下面的暂停/恢复机制仅用于未接管统一会话的兼容路径。

玩家口径："复盘的时候暂停自动游玩，复盘完再继续"——否则猫娘会在复盘还没讲完时就冲进主菜单开下一局。

- 局末写完记录后 `_pause_for_reflection()`（`pause_reason="run_reflection"`），复盘任务结束后
  `_resume_after_reflection()`。
- **一定要恢复**：`_reflect_run_end` 用 `finally` 恢复，连 LLM 没产出、抛异常也恢复 ——
  卡在"暂停"上等于猫娘永久停手，那比不暂停糟得多。
- **不擅自恢复**：只在"确实是我们为了复盘暂停的、且玩家没在这期间动过档位"时才恢复。
  玩家手动暂停或切到 `standby` 之后被我们偷偷恢复，比不恢复更糟。

## 两个「跟本体说话」的开关（v0.7.3 起）

| 开关 | 配置键 | 运行时动作 | 关掉之后 |
|---|---|---|---|
| **尖塔速报** | `host_digest_enabled` | `sts2_set_host_digest`（llm_tool 同名） | 不再把局势悄悄交给本体，本体也就不主动讲局势 |
| **尖塔复盘** | `run_reflection_enabled` | `sts2_set_run_reflection`（llm_tool 同名） | 局末不生成反思、不推给本体，也**不做"暂停-恢复"**那一步 |

两者都**只影响"跟本体说话"这一路**，这是刻意的：

- **游戏内弹幕**不受影响（那是 `danmaku_enabled` / `sts2_set_danmaku` 管的）；
- **自动游玩**不受影响；
- **结构化记录照旧写** —— 损血归因、路线图、历史页在复盘关掉时依然完整（复盘只多加一段
  "让模型复盘"的话）。所以复盘开关关掉后，历史页仍然能看到"哪个房间掉了多少血、拿到什么"。

两个入口都用静默推送（`ai_behavior="blind"`）：开关切换不该污染聊天，也不该被 LLM 复述。

## 架构与 HTTP 接口

- **入口** `ModEntry.cs`：依次启动 `GameThread`、`GameEventService` + `ScreenEventBridge`、`HttpServer`/`Router`、`CombatSolverRuntime`、`NekoConfig`、`NekoDanmakuDriver`、`NekoAutoplayDriver`。
- **HTTP 路由**（`Server/Router.cs`）：

| 路由 | 作用 |
|---|---|
| `/health` | 握手 / 版本 |
| `/state` | 完整局面快照 |
| `/coop/state` | co-op 读视图（每 player 行动阶段 + 手牌 + 共享敌人） |
| `/action` / `/actions/available` | 执行动作 / 可用动作 |
| `/config` / `/config/open` | 配置读写（插件连上后据此关掉 Mod 的 LLM/弹幕） |
| `/danmaku` | 游戏内弹幕 POST（受 `danmaku_push_enabled` 收口） |
| `/danmaku/clear` | 清空屏幕上的弹幕并卸掉 overlay（关弹幕时用） |
| `/events/stream` | SSE 场景事件流 |
| `/solver/plan` | 战斗求解决策（权威来源） |

- **战斗求解器**（`Solver/`）：内嵌 **vendored CombatSolver 0.50.1 引擎**（基线 `v0.28.0`），卡效推断用 **RitsuLib**，私有成员用 **GameRef 反射**（已去掉 Krafs.Publicizer）。`Game/Sim/` 是一套独立重实现的确定性结算器，仅作回退。
  - vendor 边界、适配规则、再同步流程与流水线脚本见 [`game_mod/nekospire/Solver/VENDORING.md`](game_mod/nekospire/Solver/VENDORING.md) 与 `game_mod/scripts/apply-vendor-sync.py`；只读同步报告：`game_mod\scripts\sync-vendor.ps1`。
  - **行为验证状态**：本次同步只验证到"Release 编译零错误 + SimTests 全通过"。求解器在真实对局中的行为**尚未验证**，见 `VENDORING.md` 的"验证边界"一节。

## 问题反馈

遇到错误路线、意外重算或执行异常，把游戏运行日志与 NEKO 运行日志发给作者邮箱（文末「联系人」一节）。

## 设置mod端口

mod默认监听端口为18080，虽然是冷门端口，但依然可能会被占用。

可以打开环境变量，在全局变量处新建变量 STS2_API_PORT ，值可以选择一些无占用端口

样例 变量名："STS2_API_PORT" 变量值："38080"

## 第三方致谢

本 Mod 的战斗求解器核心 **Vendoring 并深度适配**了以下第三方项目，特此致谢：

- **[Combat Solver](https://github.com/Torch1230/CombatSolver)**（作者：**Torch / Torch1230**）—— [创意工坊页面](https://steamcommunity.com/sharedfiles/filedetails/?id=3790899961)。战斗路线求解器。其 **搜索/模拟核心**被本 Mod 内嵌并适配，用于 `GET /solver/plan` 的战斗决策建议。已获作者授权使用。**上游现为 MIT 许可证**（见下）。
- **[Random Foreseer](https://github.com/hotwords123/StS2.RandomForeseer)**（作者：**hotwords123**）—— [创意工坊页面](https://steamcommunity.com/sharedfiles/filedetails/?id=3747531952)。Combat Solver 的战斗模拟引擎核（战斗状态、牌堆、RNG、Fork、历史、Mirror）部分来自其实现，并在获得 hotwords123 书面许可后进行改造。本 Mod 仅 Vendoring 引擎源码，**不加载/分发 Random Foreseer 程序集作为运行时依赖**。

> **许可边界**：Combat Solver 上游采用 **MIT 许可证**，版权声明为
> `Copyright (c) 2026 Torch1230 and contributors`，以及
> `Copyright (c) 2026 hotwords123 (portions derived from Random Foreseer)`。
> MIT 允许使用、修改、再分发与再许可，**条件是保留版权声明与许可证全文**。
> 本仓库内嵌的是其搜索/模拟核心的一个子集（并做了反射适配与 Godot 去耦），
> 相关来源与同步机制见 [`game_mod/nekospire/Solver/VENDORING.md`](game_mod/nekospire/Solver/VENDORING.md)。
> 上述第三方来源代码不受本仓库 AGPL-3.0-only 自动覆盖，各自遵循原作者的 MIT 条款。

本 Mod 的MCP模块参考：

- **[STS2-Agent](https://github.com/CharTyr/STS2-Agent)**（作者：**杉茶 / CharTyr**）—— AI Agent Mod。在可视化窗口里配置模型端点、对话、按模型调整思考强度、让模型自动游玩。其具体数据来源本 MOD 并未过多修改。

本 Mod 的弹幕模块参考：

- **[danmuai](https://github.com/PEPETII/danmuai)**（作者：**timerome / PEPETII**）—— 让屏幕内容拥有自己的 AI 弹幕。看到这个仓库才诞生的加入猫娘弹幕的功能，故写致谢里。

- **[弹幕尖塔 DanmakuSpire]**（作者：**Icnsis**） —— [创意工坊页面](https://steamcommunity.com/sharedfiles/filedetails/?id=3779807977)。本 MOD 视觉样式刻意照着 DanmakuSpire 调，代码本身是独立写的。他的样式也很好看，故写致谢里。

**感谢开源**我嘞个缝合怪。

## 联系人

有任何问题请把游戏运行日志和NEKO运行日志发送邮件到 zhaijiunknown@outlook.com

游戏运行日志
```text
%AppData%\SlayTheSpire2\logs
```

NEKO运行日志
```text
您的用户文件夹\AppData\Local\N.E.K.O\logs
```

## License

This project is licensed under the GNU Affero General Public License v3.0 only (AGPL-3.0-only).
