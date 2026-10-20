# AGENTS.md — NekoSpire 工作指南

本文件给后续 agent/开发者一份**够用即可动手**的地图，避免重复调查。覆盖三部分：
**N.E.K.O 本体**、**sts2_autoplay 插件**、**nekospire 游戏 Mod**。命令与路径按本机现状写死，
换机器时改对应变量。

更新：2026-10-17。需求与当前能力见[需求文档](../nekospire-companion-requirements.md)，当前问题与修复顺序见[问题文档](../nekospire-companion-issues.md)，历史实施依据见[旧计划](../nekospire-companion-plan.md)。后续问题及实施结果更新问题文档，不再扩写旧计划。

---

## 0. 一句话架构

```
Slay the Spire 2（两个游戏进程：host 真人 / catgirl 猫娘，由 Mod 拉起）
        ▲ 注入                                │ 注入
        │                                      │
nekospire Mod (C#/.NET9)  ── HTTP ──►  插件 sts2_autoplay (Python)
  host:18080  catgirl:18081            读 /state、决策、POST /action
  /state /action /solver/plan           │
  /events/stream(SSE) /danmaku          │ N.E.K.O 插件 SDK / IPC
                                        ▼
                              N.E.K.O 本体（人格 + 对话 + 记忆 + agent 模型 + TTS）
                              48911 main / 48912 memory / 48915 agent / 48916 plugin-server
```

**职责边界**：
- **Mod = 眼睛和手**：把游戏局面打包成 `/state`，接收 `/action` 执行。不决定策略。
- **插件 = 大脑皮层**：读局面 → 拼上下文 → 调 LLM → 校验 → 派动作；并负责弹幕/发言/记忆接线。
- **本体 = 人格与算力**：提供人格卡、对话历史、长期记忆、agent LLM 客户端、语音/表情通路。
- **LLM 用人格做整局决策**；插件不自己决策策略（旧 `heuristic_planner` 仅兼容保留）。

---

## 1. 关键路径（本机现状）

| 用途 | 路径 |
| --- | --- |
| **数据根（唯一真源）** | `F:\game\N.E.K.O` |
| 数据根配置 | `%LOCALAPPDATA%\N.E.K.O\state\storage_policy.json` → `selected_root` |
| NEKO 源码（代码目录） | `F:\game\NEKO-src`（Python 3.11.x，`uv sync`） |
| NEKO 源码 venv | `F:\game\NEKO-src\.venv\Scripts\python.exe` |
| 插件开发树（本仓库） | 本仓库根目录（Python 包，`plugin.toml` 入口 `STS2AutoplayPlugin`） |
| **插件实际加载目录** | `F:\game\N.E.K.O\.neko-plugin-installations\plugins\sts2_autoplay` |
| 插件配置 | `F:\game\N.E.K.O\plugins\sts2_autoplay\config\plugin.toml` |
| Steam 桌宠壳 | `F:\steam\steamapps\common\N.E.K.O\N.E.K.O.exe`（闭源 Electron） |
| 游戏根 | `F:\steam\steamapps\common\Slay the Spire 2` |
| 游戏 API dll | `<游戏根>\data_sts2_windows_x86_64\sts2.dll` |
| **Mod 加载目录（创意工坊）** | `F:\steam\steamapps\workshop\content\2868840\3794941932`（`nekospire.dll`/`.pck`/`mod_id.json`） |
| Mod 用户数据 | `%APPDATA%\SlayTheSpire2\NekoSpire\settings.json` |
| 插件日志 | `F:\game\N.E.K.O\logs\plugin\N.E.K.O_Plugin_sts2_autoplay_2026*.log` |
| 宿主日志 | `F:\game\N.E.K.O\logs\N.E.K.O_{Main,Memory,Agent}_*.log` |

> **代码目录 ≠ 数据目录**：源码在 `NEKO-src`，但 `memory/`、`characters.json`、插件安装目录
> 都在数据根 `F:\game\N.E.K.O`。改插件要改**安装目录**（或从本仓库部署过去）。

---

## 2. N.E.K.O 本体

### 2.1 形态
- **源码版**：`F:\game\NEKO-src`，`uv sync`（**Python 必须 3.11.***），前端需构建。
  入口 `launcher.py`。后端端口 48911/48912/48915/48916。
- **Steam 版**：内置 `projectneko_server.exe`（宿主 Python 已编译，`memory/`、`app/` 等**无 .py 可改**），
  桌宠壳为闭源；宿主补丁应改源码版并由源码后端运行。
- **结论**：宿主侧改动（如 fact 闸门）**只能跑源码版**，Steam 版不可部署宿主补丁。

### 2.2 桌面桌宠
桌宠 = 闭源 Electron 壳（`lanlan_frd`），**只在 Steam 包**。源码仓库没有桌宠界面。
**源码后端 + Steam 壳** 已实测可行：先起源码后端 → 运行 `N.E.K.O.exe`
→ 壳扫到 48911/48912/48915 上已有的后端（`app=="N.E.K.O"` 且 `instance_id` 一致）→ `attach_existing` 复用，
不再另起宿主进程。详见 §5 一键启动脚本。

### 2.3 数据根 / 人格 / 记忆
- 切换数据根：`storage_policy.json` 的 `selected_root`（本机 = `F:\game\N.E.K.O`）。
- 人格卡：`<数据根>\config\characters.json`（如 `主人.昵称`、`猫娘.YUI/小铃`）。**改完需重启 NEKO**。
- 记忆：`<数据根>\memory\<角色名>\{time_indexed.db, recent.json, settings.json, facts.json}`。
  跨局 fact 存 `facts.json`；对话原文/近期在 `recent.json`。
- **删除记忆**：删 `<数据根>\memory\<角色名>\` 下的 `time_indexed.db` / `facts.json` / `recent.json`。

### 2.4 插件系统
- 插件服务器在 48916。重载：`POST http://127.0.0.1:48916/plugin/sts2_autoplay/reload`（返回 `success:true`）。
- 状态：`GET http://127.0.0.1:48916/plugin/status?plugin_id=sts2_autoplay`。
- 插件只应从**安装目录**加载；改完本仓库要同步过去再 reload。

### 2.5 宿主侧事实闸门（本项目加的补丁，仅源码版）
- `F:\game\NEKO-src\memory\fact_suppression.py`（读闸门）+ `memory\facts.py`（4 个入口接线）。
- 协议：插件活跃时持续写 `<数据根>\memory\.fact_suppression.json` 心跳（`suppress:true` + TTL 90s）；
  宿主读它则跳过 fact 抽取。插件停止 → 心跳停 → TTL 过期自动恢复。
- **不写 fact ≠ 不写记忆**：游戏回合仍在 `recent.json`，本体能聊当前局；只是不沉淀进 `facts.json`。

### 2.6 出牌节奏（等发言与全自动）
- 开关 `wait_for_turn_message`：**默认 false（全自动）**。true = 每个战斗回合等主人发言才出牌。
- 两处改默认：`config.example.toml` 与 `service.startup` 的 `cfg.get(..., False)`。
- **运行时切换（推荐）**：工具 `sts2_set_wait_for_turn(enabled=...)` + 入口 `sts2_set_wait_for_turn`，
  玩家说"你自己打就行"→false，"每回合等我"→true。
- **全自动的启动靠 `reconcile_turn_control`**：`wait=false` 时一进局（`_in_active_run()`：
  有 run_id 且非主菜单/大厅/选人）就把 `autoplay_state=idle` 提为 `running`。**不覆盖**玩家手动
  `paused`/`stop`，也不在控制档位非 `program` 时启动。
- 全自动仍接受主人要求。统一入口默认启用（`unified_session_enabled=true`）：文字/语音直接进宿主队列，
  插件轮询后在同一模型调用中分流、回复并保留/更新计划。派发前和战术语音后检查新输入；
  闲聊/询问可保留有效计划，游戏要求经 `pending_guidance` + `interrupt_requested` 重判。
  输入接收与模型理解有轮询和调用延迟，不宣称输入瞬间无延迟打断。连续3轮决策/派发失败设paused、pause_reason=decision_failure，直接投递一次原因；旧输入/事件和现场刷新不再驱动重试。新输入仍可有限回应，明确继续/启动/切回program后恢复；失败暂停存入checkpoint，重载继承。
- 已确认动作有有效续步时立即串行推进，取消固定1.5秒空等；pending轮询0.1秒、idle轮询0.25秒，错误退避。
  自己 `ready_to_end_turn=true` 记结束完成，等待队友不再重复结束或误报未确认。
- ⚠️ 生效配置是 `<数据根>\plugins\sts2_autoplay\config\plugin.toml` 的 `[sts2]` 段
  （`config.dump()` 读它），不是安装目录的 `config.example.toml`；三处 toml 都要改才一致。

---

## 3. sts2_autoplay 插件（Python，本仓库根目录）

### 3.1 部署 / 测试
```powershell
# 部署到 NEKO：只复制本次改动文件到 §1 的实际加载目录，核对哈希，再单独 reload
# 不运行 deploy-all.ps1；本轮热重载无需删除缓存

# 重载
Invoke-RestMethod -Method Post -Uri 'http://127.0.0.1:48916/plugin/sts2_autoplay/reload'

# 按改动选择相关测试；缺 pytest 时复用宿主 venv 和已有 _verify/run_tests.py
& 'F:/game/NEKO-src/.venv/Scripts/python.exe' ../_verify/run_tests.py tests/test_companion_llm.py -q
```

### 3.2 数据流
1. **输入与所有权**：宿主 `main_routers/sts2_companion_router.py` 复用ExternalRouteKind/TakeoverMixin，
   接收文字、ASR及realtime transcript，原话按request_id入队并去重。插件经 `/api/sts2-companion/poll`
   取得原话、最终主人格、会话前背景和checkpoint；角色+UUID实例+90秒租约确定所有权。
   输入采用后ack；重载继承未确认输入与上下文，旧实例不能输出或派发。暂停仍处理聊天。
2. **观察**：SSE/轮询刷新猫娘 `GET /state` + `GET /actions/available` → `normalize_snapshot()`。
   同一响应保留 `raw_state`、`agent_view`，另提取 `agent_facts` 和 `action_refs`（紧凑引用绑定完整entity_id）。
3. **统一决策**：`service.run_autoplay_step()` → `STS2LLMPlanner.plan`，合并输入、局面、有效要求、
   实际行动与计划。同一轮输出 `message_effects`、`reply`、`plan_update=keep/replace/wait`、control及动作/续步。
   可行动战斗重判先求解一次，结果与绑定的solver_steps回同一模型选择solver/custom；不再暴露求解function tool。
   无动作、暂停或大厅选人时有输入也可回答，不调用求解器；有效续步直接执行。
   宿主普通聊天不另起一轮战术回答；`dialogue_revision`跟踪原话，`generation`因战术变化推进。
4. **表达与绑定**：主动作及最多4个续步用card_ref/target_ref绑定实体；玩家回复保留reply，独立于动作有效性保存和投递，自动行动的兼容reply才并入speech。两通路去重，数字步号〔1〕等清除；具体战术发言的`〔步骤〕`
   按speech_step_ids展开真实牌名、目标与顺序。回复/发言走 `/api/sts2-companion/output` 和既有镜像/TTS，
   output_id去重，镜像标记阻止宿主二次分析。战斗行动提醒等audio_completed后刷新再执行；普通选择说明与结束回合收尾只等文本投递和音频入队，不等播放完成。战术音频失败保留有效选择与输出ID单独重试。
5. **校验执行**：每次派发前按entity_id解析当前下标，检查锚点、前提、新输入、控制与所有权，
   再经action_engine及Mod expected_*最终校验 → `POST /action`。仅队友削血、无关敌人死亡或位置变化，
   关键前提仍成立则复用；目标死亡、新手牌或关键前提改变停止尾部并重判。续步保存规划时enemy_intents，存活敌人的招式/意图改变报enemy_intent_changed；不因单纯削血或下标变化停止。
6. **结果闭环**：pending只观测不重发；完成回执、自身出牌证据或ready_to_end_turn证明完成。
   无证据超10秒记unconfirmed并拦同局面重发。CompanionSession记录真实行动与结果，完成有效续步立即推进。
   checkpoint仅变化时写回宿主；退出以真实对话/行动摘要交回普通聊天，不重放旧动作计划。

**API缓存前缀**：同一会话固定system（宿主最终人格+生效主人/角色档案+会话前背景+契约）及工具schema，完整请求历史追加，
动态局面放末尾并稳定序列化，不每轮滑窗替换前缀；API报告prompt用量超过120,000 token才按完整轮次成块裁旧历史。
角色/局内epoch变化时重建所属会话；同会话仅system更新时保留真实请求历史，不退回旧seed。例行动作仅附最近3条真实行动，有输入时附20条。
记录prompt_tokens、cached_tokens、history_compactions，以及缓存字段source与完整usage。优先prompt_cache_hit_tokens，再cache_read_input_tokens，再标准details.cached_tokens；显式0也有优先权。模型请求复用宿主astream并携带include_usage，完整JSON收齐后再校验/执行。HTTP边界记录stream、system_hash、parameters_hash、shared_messages/shared_chars和网关请求ID，以核对实际前缀稳定性；当前按用户要求只排查命中率，计价暂缓。
当前按API报告prompt用量超过120,000 token才成块裁剪（此前80,000 token及更早100,000字符门槛已替换），记录sts2_history_compacted；去掉动态view.run.piles重复副本。system/工具schema及历史追加原则不变。
命中还取决于提供商门槛与策略，不为凑token填充提示词。

### 3.3 核心模块
| 模块 | 职责 |
| --- | --- |
| `service.py` | 总控：接线、决策/点评/记忆入口、上下文组装 |
| `loop_runner.py` | 轮询 + SSE 只读刷新；串行自主动作循环 |
| `llm_planner.py` | 战斗重判先求解一次，同一主人格模型选择solver/custom并回答，输出动作/续步 |
| `planner_interface.py` | 动作/发言字段契约与 public_kwargs，隔离执行期内部参数 |
| `action_identity.py` | 卡牌/目标引用与实体绑定、现场重定位、有限战术前提与取消诊断 |
| `agent_facts.py` | **本项目新增**：从 `raw_state` 补齐 `agent_view` 裁掉的敌人意图/双方 buff/遗物药水效果/升华 |
| `action_engine.py` | 决策边界：严格校验，不替换非法选择 |
| `transport_client.py` | Mod HTTP 客户端（`/state` `/action` `/solver/plan` `/danmaku` SSE） |
| `companion_session.py` | 局内对话、行动结果、计划版本/步骤、已说安排与未来意图，跨局重置 |
| `companion_requirements.py` | 独立有效要求、期限与实体绑定、条件等待、真实动作完成关联；不依赖聊天窗口保存 |
| `catgirl_llm.py` | 宿主 agent 模型客户端 + 主人格读取 |
| `catgirl_memory.py` | 跨局战报、条件经验及版本/修改撤销、相关读取与实际采用结果 |
| `companion_journal.py` | 按局紧凑过程、两回合详细依据、重点案例、复盘与耗时报告 |
| `fact_gate.py` | 插件侧写事实闸门心跳 |
| `snapshot_normalizer.py` | 状态归一化（保留 `raw_state` 与 `agent_view`） |

### 3.4 关键设计决策（踩过的坑）
- **信息来源**：决策使用**猫娘进程**的 `agent_view`、同一响应 `raw_state` 提取的 `agent_facts`、
  session 等；不接 `/coop/state`，不拼接两个进程的全知视图。聊天查询能读取 Mod 返回的双方状态，
  自动决策对队友信息的利用仍待完善。占卜/求解器保留原有信息范围。
- **`agent_view` 是有意裁剪的**：只留敌人 `intent=move_id`、遗物/药水只给名字。
  原始 `/state`（`raw_state`）里有结构化 `intents`（含 `label`/`damage`/`hits`）、`powers`（名称+层数）、
  遗物/药水 `description`。`agent_facts.py` 负责把这些补进决策上下文，**不改 `agent_view`**
  （它参与观察版本与求解缓存；动作身份和前提通过独立action_refs检查）。
- **唯一决策入口**：`_llm_planner.plan`，`source=llm`。Mod 被 hold，插件是唯一动作驱动。
- **玩家手动开局**：Mod 负责进房，角色选择、ready、embark 由玩家完成；之后模型判断整局。
- **计划与发言**：`plan_revision`/`step_id` 关联计划、发言与结果；`speech_scope`/`speech_step_ids`
  区分概括目标与具体步骤。被引用续步丢弃时抑制对应发言；已说安排改变可简短改口。
  同回合次数、计划及无进展发言节流已取消，模型可继续交流；输出ID去重和步骤绑定保留。用药是独立重要动作：未明确说过该药水时允许一句说明，续步静音时从实际药水/目标补句；已说过同一步不重复。agent_facts.potions带option_index、target_index_space及self/master/enemy目标，玩家下标与敌人下标分开。
- **有效续步**：不让模型预测删牌后的下标/手牌line，保存实体绑定、局/屏/回合锚点和known_card_entities，
  每步按当前实体重定位并校验。有限前提为enemy_hp、hp_order、hp_spread、player，由模型按打法选择；
  固定集火不自动附加最低血规则。非法续步丢弃依赖它的剩余尾部及对应具体发言，不拒合法主动作。
- **求解选择与表达**：`solver_used`表示成功参考，`solver_adopted`表示采用确定前缀。可行动战斗重判默认先求解一次（mode=companion：4秒Short/6秒总期限，HTTP 8秒、外围9秒），通常一次模型调用、格式错误最多再一次。模型选择solver后采用最终动作/续步；具体说明按保留步骤绑定展开，原自由分析单列model_reason。通过speech_intent保留不含实体属性和数值的自然动机；speech_step_ids关联实际保留步骤，剩余安排改变时可简短改口。模型可附solver_assumptions逐步校验玩家要求，或因要求冲突/覆盖不足选择custom。同种相同牌面分配不同实体；不同升级/临时牌面无法辨别时停止自动前缀。抽牌/选牌后停尾部重判，非法格式、重复实体及信息边界各有诊断。
- **每回合一句**：`turn_speech_required`要求初次出手简述；缺失或绑定错误时沿用既有一次格式修正，仍失败则记录sts2_speech_deferred并继续校验、执行合法动作，不由执行层生成首牌模板；同回合可继续发言。自然表达保留既有advisory诊断；solver替换、续步截断、未选中敌人及最低血量/强化等属性描述按最终绑定修正，不额外调用模型。显式引用仍严格校验。已分类为guidance且更新动作的输入不因文案修正失败阻塞；闲聊/询问仍须回答。
- **结束回合复核**：turn_end_check列当前playable且实际energy_cost=0的实体；end_turn续步遇未复核的可打0费牌回到统一planner一次。直接end_turn须在skip_zero_cost_cards逐实体写取舍理由，允许遗物格挡/负面效果等取舍。sts2_solver_line、sts2_end_turn_review、sts2_end_turn_choice区分求解遗漏与模型选择。
- **药水奖励与失败通知**：对敌用药须在potion_checks按步骤号核对实际target_ref、purpose(reduce_incoming/damage/setup)及理由，solver/custom一致；reduce_incoming目标当前必须attacking=true且intent_damage>0，其他合法用途保留，不自动换目标或改文案。主动作失败走既有一次纠正，非法续步截断。满栏药水不可直接领取，可discard_potion→刷新→claim_reward，或resolve_rewards离开；Mod在奖励页开放丢药，满栏claimable=false，执行边界拒绝直接领取。旧DLL由snapshot_normalizer修正决策视图，不改原始现场。失败通知只有text_sent=true才清除，稳定output_id去重；断线不派发、同一连接最多3次投递、重连恢复，pending文本进checkpoint，动作与模型仍暂停。人物设置重建manager时宿主poll重新接管当前manager，保留队列与checkpoint。
- **观察与求解**：派发不再整份agent_view精确相等，队友削血交给目标身份/前提判断。动作集合按含义比较；自身格挡增加及非目标的power变化允许保留，目标效果、敌人意图、关键资源和界面变化继续校验。战斗临时不可行动保留计划等新现场。
  完整求解缓存按实际观测版本；求解时仅敌人削血则附最新view及scores_current，旧评分不冒充当前评分。
- **地图投票等待**：仅自身map.local_vote确认已投票才等队友，上一场combat.players的ready_to_end_turn不用于地图等待。自身未投且仅一个选项时经合法动作及有效要求检查后补齐投票，不被缓存的null/wait拦住；有分支仍由模型选路，地图无动作结果也走校验，不能用队友已投票作为空等依据。暂停、明确等待要求及已有本地票保留。
- **参数与记账**：`expected_*` 等执行期内部键由插件注入，模型回传时剥离，写入历史前也剥离。
  `resolve_rewards`/`collect_rewards_and_proceed` 的合法无 index 离场不被误拒。
- **局末交回聊天**：主菜单/局末交回普通聊天前先送完未送达回复（`input_results` 中 `text_sent=false`），否则 `_publish_companion_reply` 会卡在“等待统一会话重新取得输出所有权”无限重试（2026-10-17 20:24 现场）。
- **场景规则文档**：通用页面流程与游戏规则在 `strategies/common/<scene>.md`，按当前场景随 `strategy_knowledge.scene_rules` 加载，五角色共用；`_DECISION_PROMPT` 只留输出契约与安全线。交流为条件式主动询问：取舍会改变选择，或对规则/效果/局面/偏好不确定且事实与文档不足时问一句。
- **聊天同步**：统一会话直接共享实际动作、剩余计划和已说安排，不常态靠记忆补读发现输入，
  不反复向普通聊天塞动态摘要。事件进同一调度器；退出才交回紧凑实际摘要。只读查询保留兼容。
- **事实闸门**跨进程靠心跳文件，插件强杀靠 TTL(90s) 自愈。

### 主动话题、媒体功能与情绪气泡

本体`main_logic/proactive_chat/companion.py`在接管期间复用启用的素材源，后台获取后投递host_callbacks；同伴实例或局号改变时停止旧投递。统一模型输出media_requests（music/meme），`/api/sts2-companion/output`推给前端`app-companion-media.js`异步执行；`media-result`按输出ID去重，播放器真实状态通过现有music_playback和takeover sink回到同伴。点歌属于chat，保留有效计划。

情绪气泡配置入口：桌宠设置「情绪表情包」，或`/api/emotion/bubbles/page`。本体`system_router/emotion_bubbles.py`按角色UID存入数据根`avatar_tools/emotion_bubbles`；支持多张PNG/GIF/JPEG/WebP（每张8MB），随机选择且避免紧邻重复。`emotion-bubble-images.js`在新发言/窗口回焦时读取配置，图片为空沿用默认猫图。

气泡现有17类：原五类及亲昵、害羞、害怕、疑惑、疲倦、得意、调皮、鼓励、思考、好奇、嫌弃、馋嘴。同一次emotion模型调用额外返回bubble_emotion，人物模型仍用原五类emotion；细类未配置图片时回用基础情绪图片。当前角色小铃已导入`F:\game\N.E.K.O\live2d\emotion`的74个GIF，共196个分类关联，可跨情绪重复使用。

显示策略：平静/普通思考按每次回复15%概率显示，明确的基础或细分情绪直接显示。动作绑定的战术发言及reply_kind=tactical的打法说明，通过mirror.event.speech_kind=tactical标记，按30%概率显示，带情绪的战术说明也遵循该概率；局势吐槽属于chat，按其实际情绪显示。两种概率同一回复只抽一次，开始、语音和结果阶段共用决定；人物表情分析照常。等待分析的fallback不提前显示平静表情包。
普通会话也注入内置点歌说明，通过[play_music:]复用前端搜索和播放器，不依赖Agent插件。气泡前端通过/api/emotion/bubbles/event记录实际显示、默认图及概率跳过，宿主日志标记为[EmotionBubble]；播放器确认状态记录为[MusicPlayback]。
### 3.5 弹幕三个开关（别混）

- `danmaku_enabled`（插件侧推送，默认关闭，运行时 `sts2_set_danmaku`）
- `danmaku_push_enabled`（Mod 侧 overlay 渲染，默认关闭）
- `catgirl_llm_enabled`（点评用 LLM 还是启发式文本）——**关不掉弹幕**

---

## 4. nekospire 游戏 Mod（C#/.NET 9，`game_mod/`）

### 4.1 构建 / 部署
```powershell
# 需要 .NET SDK 9（本机已装 9.0.318，dotnet 在 "C:\Program Files\dotnet\dotnet.exe"）
# 环境变量 + SteamRoot 必须给对，否则 CS0246 找不到 STS2RitsuLib（RitsuLib 引用来自
#   <SteamRoot>/steamapps/workshop/content/2868840/3747602295/RitsuLib.References.props）：
$env:STS2_DATA_DIR = 'F:/steam/steamapps/common/Slay the Spire 2/data_sts2_windows_x86_64'
& 'C:/Program Files/dotnet/dotnet.exe' build game_mod/nekospire/nekospire.csproj -c Release -p:SteamRoot=F:/steam --no-restore

# 部署：DLL 是纯代码改动，直接拷进创意工坊订阅夹即可（pck 只有 640B，一般不用重打）
# 两个游戏进程退出后复制；替换前保留旧 DLL，替换后核对源/目标哈希
Copy-Item -LiteralPath 'game_mod/nekospire/bin/Release/net9.0/nekospire.dll' -Destination 'F:/steam/steamapps/workshop/content/2868840/3794941932/nekospire.dll'
```
- 脚本版：`build-mod.ps1`（`-Configuration`，拷到 `<GameRoot>/mods`）与 `deploy-workshop.ps1`
  （拷到创意工坊夹）——**两者默认 `SteamRoot=D:/Steam`、`Sts2DataDir` 指向 D 盘，本机要覆盖**：
  `-SteamRoot F:/steam` 并先设 `STS2_DATA_DIR`。`deploy-workshop.ps1` 还会调用 Godot 打包器
  （`Godot_v4.5.1-stable_win64.exe/…`，**本仓库内不存在**），只改 DLL 时直接复制即可。
- csproj 关键属性：`net9.0`，引用 `$(Sts2DataDir)/sts2.dll`、`0Harmony.dll`、`GodotSharp.dll`、
  `Steamworks.NET.dll`；`Sts2DataDir` 默认 `<SteamRoot>/steamapps/common/Slay the Spire 2/data_sts2_windows_x86_64`。
- **Mod 加载目录是创意工坊夹**：`F:\steam\steamapps\workshop\content\2868840\3794941932`（item id 3794941932）。
  改完 dll 要拷这里。
- 最新Release构建：**0 errors、22项既有warnings**；本期已退出两个游戏进程、替换工坊DLL并核对哈希，重启后固定状态接口及显示有效。升级页取消修复已包含，取消返回行为仍待游戏验收。

### 4.2 结构（`game_mod/nekospire/`）
| 目录 | 内容 |
| --- | --- |
| `ModEntry.cs` | 入口：`GameThread` / `GameEventService` / `ScreenEventBridge` / `HttpServer` / Solver / 配置 / 弹幕 / 自动驱动 |
| `Server/` | `HttpServer` + `Router`（HTTP 路由）、`GameEventService`（SSE 源）、`ScreenEventBridge` |
| `Game/` | `GameStateService`（**局面打包核心，含 `agent_view` 构造**）、`GameActionService`（动作执行）、`NekoConfig`、`NekoAutoplayDriver`、`NekoDanmakuDriver`、`GameThread` |
| `Game/Sim/` | 战斗模拟/求解器（`Sim*`） |
| `Solver/` | 求解器引擎（beam search 等） |

### 4.3 HTTP 路由（`Server/Router.cs`）
`GET /health`、`GET /state`（原始全量 + `agent_view`）、`GET /coop/state`、`GET /actions/available`、
`GET /solver/plan`、`GET /data/<...>`、`GET /events/stream`(SSE)、`POST /action`、
`POST /danmaku` / `/danmaku/clear`、`GET/POST /config`。

### 4.4 双进程 & 角色判定（`NekoAutoplayDriver`）
- host = 玩家进程（默认端口 18080）；catgirl = co-op 客户端（`coop_client_port`，默认 18081，由 host 拉起）。
- 判定猫娘：`IsCatgirlProcess()` = 有 `STS2_CONNECT_LOBBY`（Steam 加入）**或** 自身端口 == `coop_client_port`。
- 拉起猫娘：`NekoConfigWindow.LaunchCatgirlProcess()`（同一 exe，设 `STS2_API_PORT` 与 `STS2_API_DEBUG`）。
  **只有 `start_coop_session` 动作能触发它**（外部够不着）。

### 4.5 玩家名字（coop 显示名）——**已实现**
- 名字来自游戏 `MegaCrit.Sts2.Core.Platform.PlatformUtil.GetPlayerNameRaw(PlatformType, UInt64)`。
  这是**唯一漏斗**（IL 已证实 `GetPlayerName`、`Creature.Name`、大厅名牌 `NRemoteLobbyPlayer._Ready`、
  战斗 `NMultiplayerPlayerState` 全汇入它）。`playerId` 是对局 netId，名字本地解析、**不走网络同步**。
- 真人主机与猫娘跑在**同一 Steam 账号**上 → 名牌默认同名、分不清谁是谁。这就是要改名的原因。
- **关键事实**：mod 的联机流程（多人测试场景 / ENet 直连）里，游戏给名牌造的是**占位名**
  （`mp_names.json` → `"Test Host"` / `"Test Client N"`，或未映射时直接显示 netId），**从不查 Steam**。
  所以「宿主显示 Steam 人格名」不能靠游戏自己，必须由本补丁直接读 `Steamworks.SteamFriends.GetPersonaName()`。
- **判据（实测过的坑）**：选人/大厅阶段 `LocalContext.NetId` 与 `RunManager.NetService.NetId` **都还是 null**
  （对局未开始），此时只有大厅对象知道本地 id。本地 netId 解析按 `GameStateService.BuildMultiplayerPayload`
  的顺序回退：`LocalContext.NetId` → `RunManager.NetService.NetId` → 多人测试大厅 `StartRunLobby.LocalPlayer.id`
  → 选人界面 `NCharacterSelectScreen.Lobby.LocalPlayer.id`。
  角色判定优先看网络角色 `NetGameType`（Client=猫娘），只有在拿不到时才回落到环境变量
  `NekoAutoplayDriver.IsCatgirlProcess()`——启动路径没设那些环境变量时，两边都会自认宿主、改错牌。
- **实现（`Game/PlayerNamePatch.cs`）**：Harmony postfix 这个漏斗，两块名牌各自改写，无需跨进程通信：
  - 猫娘名牌 → `coop_catgirl_name`（插件下发）；猫娘进程改 `playerId == myNetId`，宿主进程改 `playerId != myNetId`
  - 真人/宿主名牌 → `coop_host_name`（显式指定时），否则 `SteamFriends.GetPersonaName()`（缓存一次）
  - 两块都只有在开启了改名（猫娘名已配）或显式给了宿主名时才动，否则完全保留游戏原行为
  - 任何一环取不到（名字空 / netId null / 角色未知）→ no-op，回落原值。**未泛化到 3+ 人局**
    （宿主侧"另一个玩家"假设只有两人）。
- 名字来源：`NekoConfig.coop_catgirl_name`（插件 `POST /config` 推给两个进程；插件配置项
  `coop_catgirl_name` 缺省=本体角色名，运行时入口 `sts2_set_catgirl_name`）。
  宿主名 `coop_host_name` 由 mod 侧直接读 `settings.json`（不由插件下发），空=读 Steam 人格名。
- **实测结论**（2026-10-12，两窗口截图 + godot.log）：猫娘进程日志
  `host name='猫寻铃' playerId=1 myNetId=20484 catgirlProc=True` 与
  `catgirl name='小铃' playerId=20484 ...`，两个窗口左上名牌分别显示 **猫寻铃**（真人）和 **小铃**（猫娘）。
- 生效时机：Mod DLL 改动**需重启游戏**（老进程仍用启动时加载的旧 DLL；`/config` 写入会成功，
  但旧 DLL 的回读里没有该字段）。改 `coop_host_name` 后同样要重启。

### 4.6 配置（`NekoConfig`，存 `%APPDATA%\SlayTheSpire2\NekoSpire\settings.json`）
`coop_enabled`、`coop_client_port`(18081)、`coop_control_mode`(auto/hold，运行时)、`coop_manual_character_selection`、
`coop_catgirl_name`（联机里猫娘的显示名，空=不改）、`coop_host_name`（真人名牌的显示名，空=读 Steam 人格名）、
`danmaku_*`、`coop_map_follow_player`/`coop_map_vote_wait_seconds`、
`llm_*`（Mod 自带独立 LLM，插件接管时置 `llm_enabled=false`）。

---

## 5. 一键启动：源码后端 + Steam 桌宠壳

脚本：`scripts/start-neko-source-shell.cmd`（相对本仓库根目录）。

**Steam 启动项**（N.E.K.O → 属性 → 启动选项）：
```
cmd /c "E:\job\code\projectcodex\temp\nekospire\scripts\start-neko-source-shell.cmd" %command%
```
之后点「开始游戏」= 先起源码后端 → 校验 `/api/sts2-companion/poll` 路由 → 再开 Steam 桌宠壳（壳自动 attach 复用后端）。

**加固行为**：源码后端在 `NEKO_WAIT_SECONDS` 内未就绪、启动日志出现 `Startup timeout`/`Startup aborted`，或已有 48911 后端缺少同伴会话路由时，脚本直接报错退出，**不会启动壳**。避免壳回落到 Steam 内置后端（`projectneko_server.exe`）导致 `/api/sts2-companion/*` 404（表现为同伴会话连接异常）。
常见失败原因：长时间未启动后的冷启动，Main Server import 可能超过 launcher 的 90s 上限（`F:\game\NEKO-src\launcher_core\runtime.py:3328`），可调到 180s。冷启动实测可达 ~94s，热启动约 8s；延迟发生在 `import main_server` 内部，未见网络阻塞，优先怀疑冷文件缓存 + Defender 实时扫描。

**可覆盖环境变量**：`NEKO_SRC`、`NEKO_SHELL`、`NEKO_HEALTH`、`NEKO_WAIT_SECONDS`（默认 180）、`NEKO_COMPANION_PROBE`、`NEKO_COMPANION_ROUTE`。
直接双击脚本亦可（用默认路径）。

> 注意：必须**先起源码后端再开壳**；壳经 Steam 启动时会带 `%command%`，脚本把它们透传给 exe。

---

## 6. 常用运维命令

```powershell
# —— 端口健康 ——
Invoke-RestMethod 'http://127.0.0.1:48911/health'   # 本体 main
Invoke-RestMethod 'http://127.0.0.1:48915/health'   # 本体 agent
Invoke-RestMethod 'http://127.0.0.1:18080/health'   # Mod host
Invoke-RestMethod 'http://127.0.0.1:18081/health'   # Mod catgirl

# —— Mod 局面（原始全量，含 intents/powers/description）——
Invoke-RestMethod 'http://127.0.0.1:18081/state'

# —— 插件重载 / 状态 ——
Invoke-RestMethod -Method Post -Uri 'http://127.0.0.1:48916/plugin/sts2_autoplay/reload'
Invoke-RestMethod 'http://127.0.0.1:48916/plugin/status?plugin_id=sts2_autoplay'

# —— 事实闸门心跳 ——
Get-Content -LiteralPath 'F:/game/N.E.K.O/memory/.fact_suppression.json'

# —— 日志跟踪 ——
Get-Content -Path 'F:/game/N.E.K.O/logs/plugin/N.E.K.O_Plugin_sts2_autoplay_2026*.log' -Tail 50 -Wait
```

### Co-op 自动化（Mod 侧）
`game_mod/scripts/Start-NekoCoop.ps1`：拉起 host+catgirl、建/加房间、选角、ready、可选进战斗。
`-ClientAutoplays` 有自动 join/select/ready/vote 能力；常规同伴游玩按当前需求停在选人，角色与开局确认交给玩家，不能照搬脚本的自动选人流程。

---

## 7. 工作约定

- 默认中文，先说明结论与依据；围绕当前目标做最小改动，不顺手修无关问题或重构。
- 当前Flash决策请求使用reasoning_effort=low，保留思考能力；输出预算为8192，按真实延迟及打法继续观察。提示词短而直接，以事实、工具结果和输出契约为主，主人格保留。
- 优先复用现有实现与标准库；只在系统边界校验，异常仅在可处理时捕获，保留已有边界与安全处理。
- **允许为当前任务自动更新主项目和工坊**，部署使用 §1 实际路径；不运行 `deploy-all.ps1`，不改 Steam 内置插件目录。宿主源码改动需重启后端，插件改动复制、核对哈希后单独 reload，Mod DLL 替换需两个游戏进程退出后再操作。
- **不输出模型 Key，不打印 `core_config.json`**。已有未提交工作保留，不用还原文件的方式确认测试基线。
- 仅为关键路径和非平凡逻辑新增行为测试；单次默认最多3项，不新增测试基础设施。运行相关必要检查，小改动不跑全量；文档更新不跑代码测试。
- 文档只更新因本次改动失真的内容，需求/状态写入需求文档，下一阶段方案写入后续计划，不再追加交接流水账。
- 用户已授权本期持续要求、固定状态弹幕及协作行为开发，并新增表情包显示修复；允许重载、重启及操作测试现场。N1–N3开发及代码验收已完成，游戏效果由用户正常游玩验收；N4/N5按具体反馈选题。本轮不主动操作测试游戏。

## 8. 当前修复与验证状态（2026-10-16）

2026-10-17指令一次回应见计划16节：有指令reply时取消额外计划speech，主动作记为已说明，首回合不重复补句；动作取消保留回复，必要结束风险/跳过理由合入reply。defect/combat.md补球被动/激发不吃易伤。70HZ8MT6BJYM第2回合防御改口定位为新增易伤取消旧计划后，模型舍弃仍含防御的solver，改custom全输出吃6伤；防御偏好已注入，此批不改攻防策略。定向检查通过，3个运行文件已同步核对hash并reload成功，未操作游戏。

2026-10-17少量交流与削血恢复见计划15节：球的回合结束被动/激发/球位知识已移入defect/combat.md，真实decision_knowledge路径确认战斗读取；通用交流仅加一句偶尔补刀配合/重要取舍交流。撤除第13节新增削血求解复核与元数据，队友仅削血继续合法计划；资源/意图/新输入校验保留。定向检查通过，4个运行文件已同步核对hash并单独reload成功。

2026-10-17共享事件修复见计划14节：MORPHIC_GROVE实为本方投票已到主机、等主机投票，被误判unconfirmed；event补共享/页号/双方投票，本方登记即确认、等待时不重判，聊天与翻页正常。卡住重判按现场键只一次，过程区分投票登记与收益结算。3个新增行为测试及既有事件推进定向通过，Mod编译无新增警告；用户关闭游戏后2个Python文件及工坊DLL已同步核对hash，reload成功，游戏保持关闭。队友削血的宽泛复核本次不改，待讨论稀少机会触发。

2026-10-17本次现场收尾：新代码已恢复MX8361RD1MSV到第3层战斗、错误0。原纠正在旧代码01:25:42已回复，但混淆39血规划与13血派发现场；其误写的52a7a351…长期经验已修正为v2/case，原三条长期经验保留，历史原文不重放。聚焦复盘依据消息turn锚点，非战斗latest.turn为空时仍能定位所指回合。后续说明以计划13节最终证据为准。

2026-10-17新局MX8361RD1MSV现场修复见计划13节：CARD_SELECTION_REMOVE映射已补；“上回合能打死”两次8192纯思考耗尽，改用所指回合聚焦复盘，规划39血与实际派发13血分开给证据，保留历史/待回复原话。已采纳求解路线在队友削血后快速复核，相同路线沿用、改变才交模型重选。扩展映射及两个关键新用例定向通过，5个Python文件已定点同步hash并启动插件，原局保留；现场回复恢复继续核对。不关闭游戏，不扩大整批测试。

融合实施D已完成并部署：语音前后动作窗口暂闭均保留计划等待，原guard/敌人意图和已说安排保留；恢复不重判/不重说，资源/意图/新输入变化仍拒绝。缺首回合speech从已绑定合法步骤补一句，不整轮重跑，已有自然发言保留。2个关键测试及原缺发言检查共6场景通过，3个运行文件已同步hash/reload。A–D全部完成，当前事实、策略知识、条件经验、有效要求同次决策；游戏关闭，待用户正常开局观察。

融合实施C已完成并部署：例行经验简版+按需历史展开，模型近期回执4条/有输入20条，原始执行回执保持完整；去session重复计划/已处理原话和重复actions投影，当前事实复用同次构建。新增2个检查及2个相关既有检查通过，catgirl_memory/service/llm_planner已同步hash并reload；人格、要求、120k裁剪、low/8192保留。D继续。

融合实施B已完成并部署：统一模型现已实际读取角色总纲与当前场景策略，base固定前缀、scene当前请求，路径/hash留日志。41份文档保留，故障机器人3份仅修改绝对倾向；经验补scenes/selection_kinds，候选不例行注入，匹配纠正与明确偏好分配名额。3条有效经验仅迁移combat条件及版本，原文/来源/撤销状态保留。3个定向检查通过，8个运行文件/文档已同步hash并启动；备份_verify/fusion-stage-b-before。C–D继续。

融合实施A已完成：Power当前实例同ID动态说明优先，amount/display_amount/description_source分离，缺说明显式标记，Orb惰性路径保留。Python定向用例和编译后Mod提示身份/Orb路径检查通过，构建0错误22项既有警告。用户关闭双方游戏后已同步DLL与agent_facts并核对hash、reload；游戏保持关闭，下次启动加载。B–D继续按计划12节实施，每步即时更新。

用户要求将待实施问题、策略文档与记忆融合为整体方案，见计划第12节。推荐A事实修复→B文档/经验接入→C输入减量→D执行复用；五角色文档保留，明确默认知识、当前要求、长期偏好和条件经验各自职责。用户随后授权实施，A–D现已完成并部署；当前统一决策读取角色总纲/场景知识，记忆按场景匹配。

新增K13–K17见计划第11节：效果描述应取当前实例动态提示并区分Amount/DisplayAmount；经验补场景范围；上下文减量；临时动作窗口变化保留计划，减少无效重判。用户本轮要求整理方案并清理旧策略/口号，K13–K16已按融合A–D实现并部署。K17按用户纠正已恢复五角色策略文档及原加载路径；仅阻断旧lessons/频次偏好注入、删除启发式口号生成，例行决策不再带旧反思；历史操作和玩家明确的3条有效经验保留。新增2个定向行为测试及2个既有用例通过；不跑整批回归。部署状态见计划K17。

19:19:02“把打人的打死就不用防”当秒收到，但low请求在19:19:29再次耗尽4096（reasoning_tokens=completion_tokens=4096，finish_reason=length），下一轮才正常回复。按用户要求将输出上限提升至8192，low及60秒调用期限保留；耗尽提示使用实际预算值，继续区分length/空正文/格式/契约。仅运行预算耗尽的定向用例，llm_planner.py已同步核对哈希并热重载，宿主和游戏保留。用户明确要求排查与小改动采用轻量检查，不再整批运行相关测试。

新局E90ND82T829M的K9–K11见计划第10节末：纯流程催促由统一模型分类immediate，无具体约束时不强制登记要求，具体牌/目标/未来规则仍guidance；要求纠正一次列全source_message_id/scene等缺项。选牌给动态参数契约并按本页真实卡牌实体、页面ID绑定，兼容同页card_ref/card_index别名，冲突或换页拒绝，历史回写option_index。新增3项关键行为测试，190项相关检查通过。5个Python文件service/llm_planner/action_identity/action_engine/companion_session已同步核对哈希、热重载，宿主和游戏保留。仅核对后恢复本次选牌故障暂停，19:15:34一次模型调用完成选牌，现场ZAP为电击+、0费，随后进入第2层战斗；low及4096预算不变。

K1–K8本批实现见计划第10节：状态相同每3秒保活、变化立即推送；输出409按原所有权规则恢复并沿用output_id，不计模型失败；求解/模型期间0.25秒接收输入，新原话取消旧决策、不提前ack。assumptions保留严格条件校验并加强诊断。事件文本用变量副本解析，仍有占位符时等待刷新、继续聊天；HoverTips补关联附魔与授予卡牌效果。决策改low，4096预算暂不变；记录思考首段/正文首字/总耗时/usage/finish_reason及失败分类，预算耗尽不原样格式重试。K5原“代理排队”的归因已按同次请求对齐纠正为思考生成等待。176项相关Python检查通过，Mod编译0错误、22项既有警告；实际代理usage已确认low及非空正文。

本批5个Python文件service/llm_planner/action_identity/agent_facts/transport_client与工坊DLL已定点同步、逐个核对SHA256并保留旧运行文件。用户关闭双方游戏后替换Mod，18:54插件重载成功；通过真实sts2_get_status入口核对为disconnected、idle，等待游戏连接。/plugin/status仍返回18:32的缓存，当前运行状态以entry回执为准。游戏保持关闭，新DLL随下次启动加载。

致死判断及局末复盘修复见计划8.16：结束回合使用现有lethal_risks的具体数值，不将待结算说成已存活。局末先给结构化assessment/question并询问玩家，learning_updates=[]；catgirl_memory的review_discussion仅存待讨论草稿，不作为reflection或经验回喂。玩家反馈后用run_feedback关联原局、原回合及本轮feedback_message_ids，再保存讨论结论与经验；主菜单及重载后仍能讨论，原局要求和动作不恢复。重点回合优先真实死亡、最后有实际动作回合及大额损血；hp_lte匹配自身结构化current_hp并兼容HP字符串。此前SGTKBSWTZD53的玩家纠正经验保留。

遗物效果数据修复见计划8.15：宝箱/商店候选及事件遗物预览补游戏说明，agent_facts按ID/选项下标投影效果；旧Mod缺字段时按同游戏数据接口补齐并缓存。物品文本优先GetFormattedText，连续能量图标保留数量，缺说明/未解析数值显式标记。骇人头盔现场核对为耗能至少2时获得4格挡。43项相关Python及编译后Mod关键路径检查通过，DLL与3个Python运行文件定点部署、核对哈希并reload；SGTKBSWTZD53已在第14层结算，游戏正常关闭后经Steam重开，恢复同伴选人界面，选角与开局确认由玩家决定。

- **重复建房与弹幕默认值**：直连主机连接由GameActionService保留，重建前立即断开并Dispose底层ENetConnection；NetHostGameService在IsConnected=false时会跳过StopHost，单独Disconnect可能留下33771占用。游戏设置窗口复用start_coop_session，拦截重复点击并展示失败原因。滚动弹幕在插件及Mod默认关闭，固定同伴状态仍由独立开关控制。sts2_get_status只返回数据，功能页轮询不再向宿主投递status_feedback。

当前项目已完成一局联机通关，本期持续要求、固定状态弹幕及协作机制已实现，代码测试通过项按用户决定验收完成。完整需求和状态以[需求文档](../nekospire-companion-requirements.md)为准，剩余工作见后续计划。

- **已热部署Python**：统一输入与会话、实体绑定续步、预求解期限、发言绑定、解除同回合节流；最新补球位事实、未来要求guidance提示、明确继续先resume、重载消息ID去重与同会话真实历史保留。
- **结算/记忆修复**：按game_over.is_victory判结果，未知不猜失败；胜利结算0血不计损血；同局记录不被空数据覆盖，结果纠正清旧反思，不重复复盘。已删除用户确认的调试放弃记录；旧第16层失败归因证据不足，第45层通关仅恢复16条执行决策，完整路线与损血轨迹缺失。
- **Mod已替换并重启加载**：新DLL含固定状态弹幕、/companion/status、独立显示开关及action_results/action_results_epoch；同时包含既有升级页取消修复。Release构建0错误、22项既有警告；固定状态显示已现场检查，升级取消操作仍待游戏验收。
- **本期Python**：有效要求与scope/实体/条件、同次更新预览校验、completes_requirements实际完成关联、控制checkpoint和同源execution_status已部署；“下个火堆愈合”与保留药水已成功记录，要求及玩家暂停跨重载保留。新增有限协作事实位于agent_facts.collaboration，含双方公开资源/power_id层数及真实完成记录，不暴露队友隐藏手牌。
- **代码收尾已部署**：条件归一、本地pending输入checkpoint及B1–B5修复通过87项相关Python检查。六个文件llm_planner.py、loop_runner.py、action_identity.py、companion_requirements.py、companion_session.py、service.py已同步实际安装目录并核对哈希，插件reload成功。新增3个行为测试，其他场景扩展既有用例；不跑全量。
- **表情包已改宿主源码**：F:\game\NEKO-src下static/avatar/emotion-bubble-images.js、static/avatar/avatar-reaction-bubble.js、main_routers/system_router/emotion_bubbles.py及既有static/emotion-bubble-images.test.cjs。图片加载成功起至少5秒、最多20秒，晚到分类恢复、后续回复延后自己的展示而不提前清前图；保留15%/30%概率。3项JS检查、1项API检查及真实GIF可见时序通过；源码main及Steam桌宠已重启，新版config_failed诊断接口200；13:07:48真实开场问候记录具体GIF的displayed，偶发缺图仍按实际turn追踪。独立静态脚本无需构建bundle。
- **相关检查**：输入/历史73项、通关/记忆19项通过，最后日志调整语法校验通过；文档和删除记忆不跑代码测试。
- **诊断入口**：sts2_input_received → sts2_decision_context → sts2_decision_output → 派发/确认日志；sts2_session_restored记录恢复，sts2_decision_observation含球位和意图，sts2_run_result记录结算依据。
- **验收安排（2026-10-15）**：用户已明确要求代码测试成功即视为验收完成。新局/输入、要求生命周期与恢复、暂停聊天、地图/实体校验、条件协作机制、独立心跳及文案校验等通过项直接收尾。旧阶段游戏观察继续保留；最新G1–G5实玩清单见开发计划第2节，含最近回合追问、经验效果、局末复盘和速度。表情包缺图再现时按事件处理。

求解器已有真实接口与采用证据；如再报STS2-RitsuLib.Runtime缺失，检查工坊依赖，不将旧依赖故障当作当前能力缺失。缓存优先读prompt_cache_hit_tokens，保留完整usage；120k token裁剪会造成冷启动，计价仍暂缓。

## 9. N1–N3实现与用户验收（2026-10-15）

N1过程记录、N2直接追问最近两回合、N3经验沉淀和相关采用已实现，102项相关Python检查及历史页JS语法检查通过；实际游戏效果留给用户验证。取消历史选中操作和新增问答按钮。详细方案以[开发计划第3–7节](../nekospire-companion-plan.md)为准。

最近两回合保存详细依据，整局按局保存紧凑过程，纠正/异常/经验采用保留案例。原理由、实际执行、事后评价分开；pending完成更新同一记录；重载保留，新局隔离。复用统一模型和现有存储，无额外逐回合模型调用。长期经验有来源/条件/版本，支持修改撤销，按相关性少量读取并关联真实采用。

功能已按102项相关Python检查和历史页JS语法检查验收。用户实玩按开发计划G1–G5：升级取消、打法/配合/速度、最近回合追问、经验采用/修改撤销、局末复盘与展示；不操作测试游戏。新局清临时要求，保留Flash、主人格、唯一模型入口和占卜既有约定。

实现入口：companion_journal.py＋CompanionSession保存过程，llm_planner.py/planner_interface.py提供review、learning_updates、experience_uses，service.py接统一模型、执行确认和耗时，catgirl_memory.py保存经验及版本和采用结果。过程放在原catgirl_memory.json旁的run_process/，最近两回合细节滚动，完整紧凑过程与重点案例保留。局末模型按全局汇总和有界重点案例读取，原自由反思仅作评价。

原历史页static/history.html新增经验采用和速度报告，无历史选中问答入口。报告包含匹配/读取/关联动作/完成、按局/配置/处理路径的中位数/P90与样本数；get_status/get_history_payload也返回报告。模型/求解调用按本次增量记账，候选经验结合实际动作及后续回合观测读取。

8个Python文件和static/history.html已同步实际安装目录并核对9个SHA256。上次交付时宿主48911/插件48916未运行，未热重载；正常启动加载新版。不为验收启动或操作游戏。

## 10. L1–L14及新局修复状态（2026-10-16）

L1–L14按计划第8节完成实现；6个相关插件测试文件108项通过，宿主文本回执、输出去重及镜像语音9项通过。新增3个关键行为测试，其余扩展既有用例，未运行全量。8个Python运行文件已定点同步并核对SHA256：service、llm_planner、companion_session、companion_journal、action_identity、planner_interface、catgirl_memory、agent_facts。宿主改动位于NEKO-src/main_logic/core/turn.py及main_routers/sts2_companion_router.py。用户正常重启宿主后新版已加载，21:33核验目标局VKPR2V4NV1BR仍在第13层地图、48/75血、running、decision_errors=0，9条要求状态恢复，等待队友，无待送达回复；两个游戏进程保留。

reply按request_id/output_id独立保存处理结果和待送达状态；要求/学习先保存，输入ack前原子保存同局checkpoint。宿主以真实WebSocket文本回执更新text_sent，TTS失败与文本送达分开，音频重试不重复文本。checkpoint存run_process同名`.checkpoint.json`，含角色、会话、控制及真实模型历史；同局同角色恢复，宿主重启可从本地恢复较新版本，新局隔离。

最终动作reason与speech从保留步骤绑定展开，原模型分析单独保存model_reason。solver替换、续步截断和属性描述冲突时修正具体说明；同名目标带位置、规划时血量、当前意图与已有状态。主动作和续步共用绑定入口，旧target_index只在当前明确实体空间内转换。奖励按类型/内容及局、楼层、页面绑定，派发时重定位；满栏、消失和界面变化仍校验。

回顾纠正给review定位原回合并填写accepted；采纳时同次保存case或有条件的长期经验，未来要求独立处理。新增deck_card_counts条件及公开牌组构成事实，防止将触发收益纠正变成全局输出偏好。原有经验版本、撤销与采用结果关联保留。

模型默认只读两回合紧凑索引，回顾/纠正再读有界原理由及初始手牌，效果报告按用途提供；整局复盘的重点材料也去重。完整过程继续落盘，120k usage裁剪按完整轮次并保存真实交流摘要。目标局保存材料中，简单指令的回合材料16337→1162字符，纠正材料43518→16622字符；原早期详情已滚动，以当前保存材料对照。

复用奖励续步、已选事件的对话/唯一离场及明确跟票策略；地图已投票直接等队友。动作+wait归一为执行，省略action_type的纯wait/keep归一为null。战术音频超时记独立失败及speech_ms并复用选择，普通奖励说明不再阻塞播放；保留新输入、控制、所有权、实体和未确认动作防重发检查。G1–G5游戏效果观察仍随正常游玩进行。

新局9742XE3H7K4R的5项增量修复已部署，详见计划8.7–8.12：多选页真实min/max/已选实体与单次选择完成、故障暂停按局清理、保留自然发言并局部修正、地图票数等待及素材回调缓存、卡面图标资源路径还原语义。122项相关Python检查、3项实际Mod文本渲染检查通过；Release构建0错误、22项既有警告。4个Python文件service/llm_planner/action_identity/companion_session与工坊DLL已同步并核对SHA256，插件reload成功；当前游戏关闭，DLL随下次启动加载。当前idle、等待游戏连接、无待处理输入或待送达回复。21:39崩溃定位到Spine载入defect_orbs坏头，鲸鱼PCK在运行中被改写，更可能为旧资源索引失配；未改皮肤包，重开后新局正常运行到第6层且无新转储。断线仍回应玩家并禁止动作，离线重载可恢复原话；21:40“在吗”的回复已取得text_sent=true及语音入队回执。

SGTKBSWTZD53在01:08暴露的逐张播报已修：顶层续步card_ref/target_ref此前未并入kwargs，导致尾部被丢；主动作和续步现共用归一并拒绝冲突。求解前缀支持按身份绑定药水，玩家目标null按求解器owner语义映射自己，未知/歧义及信息变化边界继续停止。3个相关文件125项检查通过，4步链一次求解/模型调用、只首步说明；2个Python运行文件同步并核对哈希，插件热重载，游戏进程保留。详见计划8.13。

01:17:37的故障机器人通用打法原仅run偏好，现补存persistent/preference、conditions.character=DEFECT及原request_id；本局要求保留。agent_facts给稳定角色ID，经验匹配兼容皮肤显示名；通用打法与本局指令的保存契约已区分。01:18用户回复入队后同秒战术语音切sid导致旧TTS被取消，现先完成用户回复音频送达，再进入后续战术语音；文本/音频失败分开重试，稳定output_id、最多3次，不重复文本。现场确认音频audio_completed=true后恢复动作。148项相关Python检查通过，4个运行文件同步哈希并reload，游戏/宿主进程保留；详见计划8.14。此前8.13补完整末尾JSON提取，减少前置说明引发的整轮重答，131项检查通过。
