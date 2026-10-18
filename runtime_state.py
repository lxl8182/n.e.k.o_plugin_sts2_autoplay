from __future__ import annotations

from dataclasses import dataclass, field
from time import time
from typing import Any


@dataclass(slots=True)
class STS2RuntimeState:
    base_url: str = "http://127.0.0.1:18080"
    # 当前活动实例：插件驱动的是"哪一个游戏进程"。
    #   "host"    —— 配置里的 base_url（默认 18080）。单人局就是它；coop 里它是主机。
    #   "catgirl" —— coop 里被 mod 拉起的猫娘进程（地址由主机 /config.coop_client_port 推导）。
    # 见 service.switch_active_instance / adopt_coop_catgirl。
    active_instance: str = "host"
    catgirl_base_url: str = ""
    transport_state: str = "disconnected"
    last_error: str = ""
    snapshot: dict[str, Any] = field(default_factory=dict)
    raw_state: dict[str, Any] = field(default_factory=dict)
    raw_actions: dict[str, Any] = field(default_factory=dict)
    control_mode: str = "program"
    # 游玩模式：serious（默认，零影响）/ mischievous / silly。见 play_style.py。
    play_style: str = "serious"
    autoplay_state: str = "idle"
    # 进 standby（待机）之前的**意图**，退出待机时还回去。
    # 以前这里没有记忆，退出待机直接写死 `idle` —— 而自动玩循环只认 `running`，
    # 于是"取消待机"等于把自动游玩永久停摆（2026-10-07 实测 step 冻住不再增长）。
    autoplay_state_before_standby: str = ""
    # 「卡住」检测：拿不到可执行动作时**别每 0.5 秒静默空转**。
    # 2026-10-07 实测空转了 2 小时 40 分、日志里一个字都没有（水晶球事件的占卜子界面，mod 只暴露
    # save_and_quit）。`stuck_signature` 变了就重新计时，`stuck_reported` 保证同一处只报一次。
    stuck_signature: str = ""
    stuck_since: float = 0.0
    stuck_reported: bool = False
    stuck_reason: str = ""
    # 「无进展」检测：动作发出去了、mod 也回 ok，但**局面指纹没变** = 这个动作其实没生效。
    # 2026-10-07 实测：水晶球事件屏上插件每 2-3 秒重发一次 choose_event_option（step 83→93），
    # 金币/选项/局面一字未变。这种情况"拿不到动作"那道闸拦不住，得有第二道闸。
    progress_action_key: str = ""
    progress_signature: str = ""
    progress_repeat: int = 0
    progress_reported: bool = False
    # 自动占卜/揭图块的游标：自定义事件屏（水晶球）上**每次派发都推进下标**，扫遍所有图块。
    # 2026-10-07 实测：死盯一个下标会空点（点 [3] 无变化、点 [4] 才 completed）。
    event_option_cursor: int = 0
    event_option_screen: str = ""
    stop_reason: str = ""
    pause_reason: str = ""
    step_count: int = 0
    consecutive_errors: int = 0
    decision_errors: int = 0
    decision_error: str = ""
    decision_failed_revision: int = -1
    input_warning: str = ""
    retry_at: float = 0.0
    control_generation: int = 0
    decision_metrics: dict[str, Any] = field(default_factory=dict)
    latency_samples: list[dict[str, Any]] = field(default_factory=list)
    discussion_previous: dict[str, Any] = field(default_factory=dict)
    event_invalid_indices: list[int] = field(default_factory=list)
    # Incremented each time the scene-change event stream (GET /events/stream) drops/reconnects.
    sse_reconnect_count: int = 0
    last_poll_at: float = 0.0
    last_action_at: float = 0.0
    last_sync_at: float = 0.0
    last_success_at: float = 0.0
    last_summary: str = ""
    last_decision_source: str = ""
    last_decision_reason: str = ""
    last_planner_type: str = ""
    # /solver/plan 当前一步若是需要"选一张牌"的动作（消耗/拉弃牌堆/生成到手/变换等），solver 选定的
    # 那张 card_id；在随后出现的选牌界面（combat_hand_select 等）据此选牌，避免卡住或用启发式乱猜。
    # 无选择动作为 None。承载的是"下一步选择"的选中牌，不限于消耗。
    pending_card_choice_id: str | None = None
    # 战斗自动玩：当前回合缓存的 /solver/plan line[0].steps 里下一个要执行的步骤下标。
    # 回合开始取一次计划后整回合复用；每执行一个战斗步（play_card/use_potion/end_turn）自增，
    # 选牌子动作（select_deck_card）不步进；接近回合末尾/耗尽后重查下一回合并归零。
    solver_step_index: int = 0
    # 战斗中用药水的记账（规格 docs/shop_scoring.md 「八」，玩家口径 2026-10-07：
    # 「还有每次对战如果损血大于8就鼓励使用药水」）：
    #   potion_used_turn_key —— 本回合是否已经喝过一瓶（"同一回合只喝一瓶"，避免连灌）。
    #     键是"本局|层|房间|回合"：只看回合号不够，两场战斗的回合 1 会互相顶掉。
    #   potion_decision_log_key —— 上一句 [sts2_potion] 决策日志的去重键。自动玩每 2~3 秒一步、
    #     规划每秒一次，不去重就是刷屏（"不喝"的那些行尤其烦）。
    potion_used_turn_key: str = ""
    potion_decision_log_key: str = ""
    latest_sync_packet: dict[str, Any] = field(default_factory=dict)
    latest_report_packet: dict[str, Any] = field(default_factory=dict)
    latest_snapshot_summary: dict[str, Any] = field(default_factory=dict)
    latest_continuous_delta: dict[str, Any] = field(default_factory=dict)
    latest_action_frame: dict[str, Any] = field(default_factory=dict)
    latest_player_operation_observation: dict[str, Any] = field(default_factory=dict)
    recent_action_frames: list[dict[str, Any]] = field(default_factory=list)
    recent_continuous_deltas: list[dict[str, Any]] = field(default_factory=list)
    recent_decision_memory: list[dict[str, Any]] = field(default_factory=list)
    recent_companion_player_ops: list[dict[str, Any]] = field(default_factory=list)
    run_intent: dict[str, Any] = field(default_factory=dict)
    recent_deliveries: list[dict[str, Any]] = field(default_factory=list)
    last_companion_turn_key: str = ""
    last_companion_scene_key: str = ""
    last_companion_evaluation_key: str = ""
    last_companion_combat_comment_key: str = ""
    last_companion_player_op_fingerprint: str = ""
    last_companion_player_op_at: float = 0.0
    last_plugin_action_fingerprint: str = ""
    snapshot_signature: str = ""
    pending_guidance: list[dict[str, Any]] = field(default_factory=list)
    guidance_generation: int = 0
    last_consumed_guidance_generation: int = 0
    last_decision_generation: int = 0
    interrupt_requested: bool = False
    interrupt_reason: str = ""
    # 上一次自动玩动作被 mod 拒收（状态漂移类）时置位：说明手里的现场/计划已经过期，
    # 下一轮必须先强制 refresh_state 重算再动手。用完即清（见 service.run_autoplay_step）。
    force_replan_requested: bool = False
    last_sync_fingerprint: str = ""
    last_sync_screen: str = ""
    last_sync_summary_kind: str = ""
    last_sync_reason: str = ""
    last_push_scene_key: str = ""
    last_push_reason: str = ""
    last_push_step_count: int = -1
    last_push_at: float = 0.0
    sync_repeat_count: int = 0
    # 弹幕双线的记账：即时短句按"场景|层|回合"去重；给本体的局势摘要按时间节流。
    last_instant_line_key: str = ""
    last_host_digest_at: float = 0.0
    # 最近一次交给本体的局势速报对应的场景键（同一局面只讲一次）。
    last_host_digest_key: str = ""
    # 自动行动期间喂给本体的**被动局面上下文**（ai_behavior="read"，只进 LLM 上下文、
    # 不触发开口）去重键。自动行动时本体的点评轨被压住，若不单独喂局面，本体对
    # "现在什么情况"只能拿很久以前的旧画面回答 —— 这就是"事件界面旧快照"的成因。
    last_host_context_key: str = ""
    # 本局生命轨迹（每次刷新追加一条），局末总结时用来做"哪里损血"的归因。
    hp_trail: list[dict[str, Any]] = field(default_factory=list)
    hp_trail_run_id: str = ""
    # 当前这场战斗的"房间标识"（遭遇开始时的敌人组合）。战斗结束清零。
    combat_room_id: str = ""
    # 本局**路线图**：按访问顺序记每个房间的损血与收获。
    #   route_room_key  当前房间的键（层|界面|房间标识），换房间时收尾上一段
    #   route_start     进入房间时的基线（血量/金币/牌组/遗物/药水），用来算"收获"
    #   route_last      该房间的最新观测值
    route_log: list[dict[str, Any]] = field(default_factory=list)
    route_room_key: str = ""
    route_start: dict[str, Any] = field(default_factory=dict)
    route_last: dict[str, Any] = field(default_factory=dict)
    # 本局已经公告过的角色（开局后报一次"我玩的谁"）。
    announced_character_run_id: str = ""
    # 本局已记录的历史条目 id（避免同一局重复写历史）。
    last_history_run_id: str = ""
    # 是否正因"局末复盘"而暂停自动游玩（复盘完要恢复；玩家自己动过就不擅自恢复）。
    reflection_paused: bool = False
    # 战斗讨论模式：插件与 Mod 停手；允许确认单步或当前回合战术计划。
    discussion_active: bool = False
    discussion_status: str = "inactive"
    # 仅本场/本回合有效的即时目标与顺序约束，不写入长期偏好。
    # 两个"跟本体说话"的开关（玩家口径：给尖塔速报和尖塔复盘各一个开关）。
    #   host_digest_enabled     尖塔速报：把局势悄悄交给本体，让它自己主动讲一句
    #   run_reflection_enabled  尖塔复盘：局末生成反思并推给本体
    # 关掉只影响"跟本体说话"这一路；结构化记录（损血归因/路线图/历史页）照旧写。
    host_digest_enabled: bool = True
    run_reflection_enabled: bool = True
    # 上次已把哪一局写入猫娘记忆（避免 terminal 屏反复触发时重复总结同一局）。
    last_memory_run_id: str = ""

    @property
    def standby(self) -> bool:
        return self.control_mode == "standby"

    @property
    def guidance_queue_size(self) -> int:
        return len(self.pending_guidance)

    def touch_poll(self) -> None:
        self.last_poll_at = time()

    def touch_action(self) -> None:
        self.last_action_at = time()

    def touch_sync(self) -> None:
        self.last_sync_at = time()

    def touch_success(self) -> None:
        self.last_success_at = time()
        self.consecutive_errors = 0

    def remember_delivery(self, delivery: dict[str, Any]) -> None:
        self.recent_deliveries.append(dict(delivery))
        self.recent_deliveries = self.recent_deliveries[-20:]

    def remember_action_frame(self, frame: dict[str, Any]) -> None:
        self.latest_action_frame = dict(frame)
        self.recent_action_frames.append(dict(frame))
        self.recent_action_frames = self.recent_action_frames[-20:]

    def remember_continuous_delta(self, delta: dict[str, Any]) -> None:
        self.latest_continuous_delta = dict(delta)
        self.recent_continuous_deltas.append(dict(delta))
        self.recent_continuous_deltas = self.recent_continuous_deltas[-20:]

    def remember_decision_memory(self, item: dict[str, Any]) -> None:
        self.recent_decision_memory.append(dict(item))
        self.recent_decision_memory = self.recent_decision_memory[-20:]

    def remember_companion_player_op(self, observation: dict[str, Any]) -> None:
        self.latest_player_operation_observation = dict(observation)
        self.last_companion_player_op_fingerprint = str(observation.get("fingerprint") or "")
        self.last_companion_player_op_at = time()
        self.recent_companion_player_ops.append(dict(observation))
        self.recent_companion_player_ops = self.recent_companion_player_ops[-20:]

    def note_companion_player_op_comment(self, fingerprint: str) -> None:
        self.last_companion_player_op_fingerprint = str(fingerprint or "")
        self.last_companion_player_op_at = time()


__all__ = ["STS2RuntimeState"]
