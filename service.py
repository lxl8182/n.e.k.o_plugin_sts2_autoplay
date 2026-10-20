from __future__ import annotations

import asyncio
import json
import os
import inspect
import re
from copy import deepcopy, copy
from datetime import datetime
from threading import Lock
from hashlib import sha1
from random import Random, random
from time import time, perf_counter, time_ns
from uuid import uuid4
from pathlib import Path
from typing import Any, Callable

import httpx

from .action_engine import STS2ActionEngine
from .action_registry import STS2ActionRegistry
from .agent_facts import build_agent_facts, end_turn_danger
from .keyword_registry import build_keyword_effects
from .action_identity import action_refs, resolve_action, observation_guard, enemy_intents
from .catgirl_bridge import STS2CatgirlBridge
from .catgirl_llm import CatgirlCommentGenerator
from .companion_session import CompanionSession
from .companion_journal import CompanionJournal
from .catgirl_memory import CatgirlMemoryStore, _default_memory_path
from .companion_evaluator import STS2CompanionEvaluator, _enemy_intent_label
from .companion_feedback import apply_existing_expression, public_companion_text, speak_current_turn
from .fact_gate import FactSuppressionGate
from .history_view import write_history_view
from .heuristic_planner import (
    MAP_POLICY_AUTO,
    MAP_POLICY_FOLLOW,
    MAP_POLICY_WAIT,
    STS2HeuristicPlanner,
    normalize_map_policy,
)
from .instructions import normalize_guidance_instruction
from .loop_runner import STS2LoopRunner, _current_turn_steps
from .llm_planner import DecisionContextChanged, SolverPhaseWait, STS2LLMPlanner, view_signature
from .mode_controller import STS2ModeController
from .neko_interface import STS2NekoInterface
from .planner_interface import public_kwargs
from .play_style import (
    PLAY_STYLE_SERIOUS,
    choose_candidate,
    normalize_play_style,
)
from .preference_extractors import STS2PreferenceExtractor
from .preference_store import STS2PreferenceStore
from .runtime_state import STS2RuntimeState
from .situation_summary_engine import STS2SituationSummaryEngine
from .snapshot_normalizer import normalize_actions
from .state_machine import STS2StateMachine, should_hold_for_orders
from .strategy_repository import STS2StrategyRepository
from .summary_context_builder import STS2SummaryContextBuilder
from .transport_client import STS2ActionRejected, STS2TransportClient, STS2TransportError

# 「当前游戏信息状态」面板：状态推送最小间隔（防刷爆 SSE 有界队列）/ 心跳间隔（空闲时也定期刷新）
STATUS_PUSH_MIN_INTERVAL = 2.0
STATUS_PUSH_HEARTBEAT = 5.0



class CompanionAudioPending(RuntimeError):
    def __init__(self, receipt: dict):
        super().__init__(receipt.get("reason") or "audio_incomplete")
        self.receipt = receipt


class STS2AutoplayService:
    def __init__(self, logger: Any, status_reporter: Callable[[dict[str, Any]], None], frontend_notifier: Callable[..., Any] | None = None, *, sdk_bus: Any = None, sdk_ctx: Any = None, i18n: Any = None, play_style_random: Any = None) -> None:
        self.logger = logger
        self._report_status = status_reporter
        self._frontend_notifier = frontend_notifier
        self._sdk_bus = sdk_bus
        self._sdk_ctx = sdk_ctx
        self._i18n = i18n
        # 猫娘 LLM 点评生成（catgirl 弹幕真内容来源）
        self._catgirl_llm = CatgirlCommentGenerator(self.logger)
        self._catgirl_llm_enabled = True
        # 游戏内弹幕总开关。与 _catgirl_llm_enabled 语义不同：后者只管"点评用 LLM 还是启发式文本"，
        # 关掉它仍会走 _push_catgirl_fallback 推弹幕；本开关是唯一的真闸门（_push_catgirl 收口）。
        self._danmaku_enabled = False
        # 地图投票策略（startup 用插件配置覆盖）。默认 wait：多人局里彻底等待，不投票也不跟票，
        # 直到玩家通过对 NEKO 说话下令（sts2_set_route）。详见 heuristic_planner.MAP_POLICY_*。
        self._map_policy = MAP_POLICY_WAIT
        self._map_vote_wait_seconds = 20.0
        # 猫娘头像(base64)缓存:从 N.E.K.O 主 server card-drop 拉一次,TTL 300s
        self._avatar_b64: str | None = None
        self._avatar_at: float = 0.0
        # 交流节奏：情绪吐槽冷却（按决策次数）与战斗 ask 话题去重（每场战斗每类一次）。
        self._decision_seq = 0
        self._banter_last_seq = -999
        self._combat_ask_state: dict[str, Any] = {}
        # 询问窗口：说完 ask 后先等主人回答再派发动作（战斗 10 秒 / 选项页 25 秒）。
        self._ask_window_combat = 10.0
        self._ask_window_page = 25.0
        # 游玩模式（认真/捣蛋/笨蛋）的随机源。可替换，便于测试确定性地验证概率与落点。
        self._play_style_random = play_style_random if play_style_random is not None else Random()
        self._client: STS2TransportClient | None = None
        # 活动实例相关（见 switch_active_instance）。默认值放在这里而不是只放 startup()：
        # 测试桩直接构造 service、不走 startup()，缺这些字段会让它们全部炸掉。
        self._host_base_url: str = ""
        self._host_client: STS2TransportClient | None = None
        self._catgirl_client: STS2TransportClient | None = None
        self._catgirl_base_url: str = ""
        self._instance_failures: int = 0
        self._cfg: dict[str, Any] = {}
        self._feedback_observation: dict[str, Any] = {}
        self._feedback_last_event = ""
        self._feedback_voice_at = 0.0
        self._feedback_risk_announced = False
        self._feedback_result: dict[str, Any] = {}
        self._feedback_lock = Lock()
        self._state = STS2RuntimeState()
        self._state_machine = STS2StateMachine()
        self._summary_builder = STS2SummaryContextBuilder()
        self._summary_engine = STS2SituationSummaryEngine(self._i18n)
        self._companion_evaluator = STS2CompanionEvaluator(self._i18n)
        self._catgirl_bridge = STS2CatgirlBridge(i18n=self._i18n, source_id="sts2_autoplay")
        self._preference_store = STS2PreferenceStore()
        self._preference_extractor = STS2PreferenceExtractor()
        # 事实闸门：插件活跃期间向本体数据根写心跳，本体据此跳过 fact 抽取。
        # 游戏回合仍留在 recent.json（本体能聊当前局），但不沉淀进 facts.json。
        self._fact_gate = FactSuppressionGate(self.logger)
        # 跨局经验按条件匹配；旧局报和自由反思保留为历史记录。
        self._catgirl_memory = CatgirlMemoryStore(path=_default_memory_path(), logger=self.logger)
        self._strategy_repository = STS2StrategyRepository(
            logger,
            self._preference_store,
            catgirl_memory=self._catgirl_memory,
            default_strategy="defect",
        )
        self._mode_controller = STS2ModeController("program")
        self._planner = STS2HeuristicPlanner(logger)
        self._host_persona = ""
        self._host_background = ""
        self._conversation_seed: list[dict] = []
        self._llm_planner = STS2LLMPlanner(logger, persona_provider=self._companion_persona)
        self._companion_session = CompanionSession()
        self._learning_available: list[dict] = []
        self._latency_baseline: dict[str, Any] = {}
        self._action_window: dict = {}
        self._last_confirmed: dict = {}
        self._telemetry_gap = False
        self._companion_session.on_action = self._record_action_learning
        self._companion_session.journal.on_turn = self._record_turn_learning
        self._action_registry = STS2ActionRegistry()
        self._action_engine = STS2ActionEngine(self._i18n)
        self._loop_runner = STS2LoopRunner(self)
        self.neko = STS2NekoInterface(self)
        # 自动循环可能运行在自己的事件线程；用线程锁隔离派发，不能共享 asyncio.Lock。
        self._discussion_confirm_lock = Lock()
        self._action_dispatch_lock = Lock()
        self._decision_lock = Lock()
        self._last_decision_key = ""
        self._stuck_recheck_key = ""
        self._map_wait_key = ""
        self._decision_failure_run_id = None
        self._companion_push_keys: dict[str, tuple[str, float]] = {}
        self._selected_steps: list[dict[str, Any]] = []
        self._selected_generation: tuple[int, int, int] | None = None
        self._solver_cache: tuple[str, dict[str, Any]] | None = None
        self._last_observed_view = ""
        self._last_operation: dict[str, Any] | None = None
        self._last_dialogue_generation = -1
        self._proactive_observation: dict[str, Any] = {}
        self._communicated_turn: dict[str, Any] = {}
        self._decision_failure_notice = ""
        self._failure_notice_retry_at = 0.0
        self._failure_notice_attempts = 0
        self._notice_delivery_ready = True
        self._notice_connection = ""
        self._end_turn_review_key = ""
        self._speech_state: dict[str, Any] = {}
        self._step_timing: dict[str, Any] = {}
        self._pending_action: dict[str, Any] | None = None
        self._unconfirmed_action: tuple[str, str, dict[str, Any]] | None = None
        self._mod_gate: dict[str, Any] = {}
        self._dialogue_record_types: dict[str, int] = {}
        self._wait_for_turn_message = False
        self._turn_message_anchor: dict[str, Any] = {}
        self._turn_message_since = time()
        self._turn_message: dict[str, Any] | None = None
        self._verified_user_messages: list[tuple[float, str]] = []
        self._ingress_instance = str(uuid4())
        self._ingress_enabled = False
        self._ingress_active = False
        self._ingress_ack: list[str] = []
        self._ingress_seen: set[str] = set()
        self._ingress_callbacks: list[dict] = []
        self._ingress_material: dict = {}
        self._ingress_csrf = ""
        self._ingress_lock = Lock()
        self._mod_gate_key: Any = None
        self._ingress_claim = True
        self._ingress_checkpoint_key: Any = None
        self._ingress_character = ""
        self._execution_phase: dict[str, str] = {}
        self._last_step_result: dict[str, Any] = {}
        self._status_push_key = ""
        self._status_push_at = 0.0
        self._status_revision = time_ns()
        self._status_push_lock = Lock()
        self._reported_status_key = ""
        self._reported_status_at = 0.0
        self._event_data_wait_key = ""
        self._event_data_wait_at = 0.0

    def t(self, key: str, *, default: str = "", **params: Any) -> str:
        if self._i18n is not None:
            return self._i18n.t(key, default=default, **params)
        return default.format(**params) if params and default else (default or key)

    def _companion_persona(self) -> str:
        persona = self._host_persona or self._catgirl_llm._persona_system()
        return persona + self._catgirl_llm._persona_profiles() if persona else ""

    @property
    def _selected_steps(self) -> list[dict[str, Any]]:
        return self._companion_session.active_plan["remaining_steps"]

    @_selected_steps.setter
    def _selected_steps(self, steps: list[dict[str, Any]]) -> None:
        self._companion_session.active_plan["remaining_steps"] = steps

    async def startup(self, cfg: dict[str, Any]) -> dict[str, Any]:
        self._ingress_enabled = cfg.get("unified_session_enabled", True)
        try:
            self.logger.info("[sts2_code_version] 20260521_companion_eval_debug")
        except Exception:
            pass
        self._cfg = dict(cfg)
        if self._catgirl_memory.path:
            self._companion_session.journal.directory = os.path.join(os.path.dirname(self._catgirl_memory.path), "run_process")
            self._companion_session.journal.logger = self.logger
            self._companion_session.on_action = self._record_action_learning
            self._companion_session.journal.on_turn = self._record_turn_learning
        # 默认 false（全自动）：配置缺失时也不该退化成"每回合干等发言"——那会让玩家以为插件卡了。
        # 想要陪玩节奏就显式设 true（或运行时调 sts2_set_wait_for_turn）。
        self._wait_for_turn_message = bool(self._cfg.get("wait_for_turn_message", False))
        # 两个"跟本体说话"的开关（默认开）。关掉只影响速报/复盘这两路，不影响自动玩与记录。
        self._state.host_digest_enabled = bool(self._cfg.get("host_digest_enabled", True))
        self._state.run_reflection_enabled = bool(self._cfg.get("run_reflection_enabled", True))
        # 启动就把历史快照刷一遍：快照原本只在"局末"写，于是插件刚起、还没打完一局时，
        # 历史页会显示"读不到历史" —— 而 catgirl_memory 里其实已经躺着几十局记录。
        self._write_history_view()
        # 猫娘 LLM 点评：开关（生成间隔节流已不启用，防刷屏由外层 _should_deliver_sync 兜底）
        self._catgirl_llm_enabled = bool(self._cfg.get("catgirl_llm_enabled", True))
        # 游戏内弹幕默认关闭；关闭后 _push_catgirl 直接早退，既不生成也不推。
        self._danmaku_enabled = bool(self._cfg.get("danmaku_enabled", False))
        # co-op 显示名：真人主机与猫娘跑在同一 Steam 账号上，名牌本来同名、分不清谁是谁。
        # 这里给猫娘一个专属显示名（默认取本体的角色名，可由 coop_catgirl_name 覆盖），
        # 插件把名字推给两个游戏进程的 mod（POST /config coop_catgirl_name），
        # mod 侧 PlayerNamePatch 只在"猫娘那个玩家"的名牌上替换。空串=关，全部回落到 Steam 名。
        self._catgirl_display_name = str(self._cfg.get("coop_catgirl_name") or "").strip()
        # 事实闸门：插件活着就一直抑制本体的事实抽取（recent 照常保留）。
        # 默认开；只有显式配置 false 才不抑制。解析不到本体数据根时静默降级。
        if bool(self._cfg.get("suppress_host_facts", True)):
            self._fact_gate.start()
        # 地图投票：跟随玩家（co-op 队友先投就跟着投），以及无人投票时的等待秒数。
        # 地图投票策略：wait（默认，彻底等玩家发话）/ follow（自动跟票）/ auto（自己选）。
        # 兼容旧配置：只有 map_follow_player 布尔而没有 map_policy 时，true->follow、false->auto。
        if "map_policy" in self._cfg:
            self._map_policy = normalize_map_policy(self._cfg.get("map_policy"))
        elif "map_follow_player" in self._cfg:
            self._map_policy = MAP_POLICY_FOLLOW if self._cfg.get("map_follow_player") else MAP_POLICY_AUTO
        else:
            self._map_policy = MAP_POLICY_WAIT
        try:
            self._map_vote_wait_seconds = max(0.0, float(self._cfg.get("map_vote_wait_seconds", 20) or 20))
        except (TypeError, ValueError):
            self._map_vote_wait_seconds = 20.0
        self._planner.configure_map_policy(
            policy=self._map_policy,
            vote_wait_seconds=self._map_vote_wait_seconds,
        )
        base_url = self._resolve_base_url()
        self._state.base_url = base_url
        self._apply_control_mode("program")
        self._state.play_style = normalize_play_style(self._cfg.get("play_style"))
        self._client = self._build_client(base_url)
        # 主机实例永远是这一个（配置里的 base_url）。开房必须发给它——只有主机能拉起猫娘进程。
        self._host_base_url = base_url
        self._host_client: STS2TransportClient | None = self._client
        # 猫娘实例：coop 里被拉起的那个进程，地址由主机的 /config.coop_client_port 推导。
        # 插件接管游玩时，读状态与驱动动作都对着它。
        self._catgirl_client: STS2TransportClient | None = None
        self._catgirl_base_url = ""
        self._instance_failures = 0
        startup_result = {"connected": False, "companion_mode_enabled": False}

        try:
            # 启动不阻塞、不等 mod：尽力 health 一次(限时)，成功则标记 connected + 顺手禁用 mod 自身
            # LLM/弹幕；失败**不提前 return**，由后台事件循环在 mod 上线时自动重连并禁用其 LLM/弹幕。
            try:
                await asyncio.wait_for(self.health_check(), timeout=3.0)
                startup_result["connected"] = True
                if self._client is not None:
                    try:
                        await self._client.set_config(llm_enabled=False, danmaku_enabled=False)
                    except Exception as _llm_exc:
                        self.logger.debug(f"[sts2] disabling mod LLM/danmaku on connect failed (non-fatal): {_llm_exc}")
            except Exception:
                pass
            companion_enabled = bool(self._cfg.get("companion_mode_enabled", self._cfg.get("neko_commentary_enabled", True)))
            if companion_enabled:
                self.set_companion_mode(True, emit_status=False, push_message=False)
                startup_result["companion_mode_enabled"] = True
            else:
                self._sync_background_polling()
            # 插件已启动就明确接管；idle/paused 表示等待玩家启动，不能让 Mod 偷跑。
            startup_result["mod_sync"] = await self.sync_control_mode_to_mod()
            if bool(self._cfg.get("autoplay_on_start", False)) and not self._state.standby:
                self.start_autoplay()
        except Exception as exc:
            self._state.transport_state = "disconnected"
            self._state.last_error = str(exc)
            self._state.consecutive_errors += 1
        return startup_result

    def _resolve_base_url(self) -> str:
        env_base_url = str(os.environ.get("STS2_API_BASE_URL") or "").strip()
        if env_base_url:
            return env_base_url.rstrip("/")

        env_port = str(os.environ.get("STS2_API_PORT") or "").strip()
        if env_port:
            try:
                port = int(env_port)
            except ValueError:
                port = 0
            if 0 < port <= 65535:
                return f"http://127.0.0.1:{port}"

        configured = str(self._cfg.get("base_url") or self._state.base_url).strip()
        return configured.rstrip("/") if configured else self._state.base_url.rstrip("/")

    async def shutdown(self) -> None:
        await self._sync_companion_ingress()
        await self._release_companion_ingress()
        self._fact_gate.stop()
        await self._loop_runner.stop_background()
        if self._client is not None:
            await self._client.close()
            self._client = None
        await self._llm_planner.shutdown()
        if self._catgirl_llm is not None:
            await self._catgirl_llm.shutdown()
        self._state.transport_state = "disconnected"
        self._state.autoplay_state = "idle"
        self._emit_status()

    async def health_check(self) -> dict[str, Any]:
        client = self._require_client()
        health = await client.health()
        self._state.transport_state = "connected"
        self._state.last_error = ""
        self._state.touch_success()
        # 连上 mod 就把地图等待策略推过去：插件与 mod 各有一套决策循环，策略不一致会出现
        # "插件在等、mod 照投"这种自相矛盾的局面。失败是良性的（mod 自己的默认也是 wait）。
        map_sync = await self.sync_map_policy_to_mod()
        control_sync = await self.sync_control_mode_to_mod()
        name_sync = await self.sync_catgirl_name_to_mod()
        self._emit_status()
        message = self.t("status.connected", default="STS2-Agent 已连接: {base_url}", base_url=self._state.base_url)
        return {
            "status": "connected",
            "message": message,
            "summary": message,
            "health": health,
            "map_policy_sync": map_sync,
            "control_mode_sync": control_sync,
            "catgirl_name_sync": name_sync,
        }

    # 正式联机流程（与 mod 自身一致）：
    #   host  → mod 的 start_coop_session 一步做完（coop_enabled 落盘 -> 关主菜单子菜单 ->
    #           open_multiplayer_menu -> start_multiplayer_host(ENet 33771) -> 拉起猫娘进程）。
    #   join  → open_multiplayer_menu（仅 currentScreen 就是 NMainMenu 时可用）-> join_multiplayer_direct。
    # 不要用调试场景那套 host_multiplayer_lobby / join_multiplayer_lobby：它们要求当前场景是
    # NMultiplayerTest，且 mod 明确记录该路径会让装了第三方 mod 的第二个游戏实例崩溃
    # （见 game_mod/nekospire/Game/NekoAutoplayDriver.cs 的 DecideMainMenu 注释）。
    COOP_SESSION_ACTION = "start_coop_session"
    COOP_MENU_ACTION = "open_multiplayer_menu"
    COOP_CLOSE_SUBMENU_ACTION = "close_main_menu_submenu"
    COOP_JOIN_ACTION = "join_multiplayer_direct"

    # ---- 活动实例：插件驱动的是"哪一个游戏进程" ------------------------------------------

    INSTANCE_HOST = "host"
    INSTANCE_CATGIRL = "catgirl"

    @staticmethod
    def _normalize_instance(value: Any) -> str:
        text = str(value or "").strip().lower()
        if text in {"catgirl", "coop", "client", "coop_client", "猫娘", "coop猫娘"}:
            return "catgirl"
        return "host"

    async def resolve_catgirl_base_url(self) -> str:
        """向**主机**问出猫娘进程的端口，拼出它的地址；问不到返回空串。

        `GET /config` 的 `coop_client_port` 是 mod 0.1.4 起才有的只读字段（默认 18081）。
        老版本 mod 没有这个字段时**不猜端口**——猜错会连到别的进程上去。
        """
        if self._catgirl_base_url:
            return self._catgirl_base_url
        host = self._host_client or self._client
        if host is None:
            return ""
        try:
            payload = await host.get_config()
        except Exception as exc:
            self.logger.debug(f"[sts2] reading host /config failed (non-fatal): {exc}")
            return ""
        data = payload.get("data") if isinstance(payload.get("data"), dict) else payload
        port = data.get("coop_client_port") if isinstance(data, dict) else None
        try:
            port_int = int(port)
        except (TypeError, ValueError):
            return ""
        if not 0 < port_int <= 65535:
            return ""
        self._catgirl_base_url = f"http://127.0.0.1:{port_int}"
        return self._catgirl_base_url

    async def switch_active_instance(self, target: str, *, reason: str = "") -> dict[str, Any]:
        """只切换管理对象；同伴的认知与执行始终绑定猫娘，切换本身不改控制状态。"""
        wanted = self._normalize_instance(target)
        if wanted == self.INSTANCE_CATGIRL:
            if self._catgirl_client is None:
                address = await self.resolve_catgirl_base_url()
                if not address:
                    return {"switched": False, "instance": self._state.active_instance, "reason": "catgirl_address_unknown"}
                self._catgirl_client = self._build_client(address)
            self._client = self._catgirl_client
            standdown = {"applied": False}
        else:
            if self._host_client is None:
                self._host_client = self._build_client(self._host_base_url)
            self._client = self._host_client
            standdown = {"applied": False}
        self._state.active_instance = wanted
        self._state.catgirl_base_url = self._catgirl_base_url
        self._instance_failures = 0
        self.logger.info(f"[sts2] active instance -> {wanted} ({reason or 'manual'})")
        self._emit_status()
        return {
            "switched": True,
            "instance": wanted,
            "base_url": self._state.base_url if wanted == self.INSTANCE_HOST else self._catgirl_base_url,
            "mod_standdown": standdown,
        }

    def _companion_client(self) -> STS2TransportClient:
        """猫娘是固定的决策、查询和执行对象；单人局才使用配置中的唯一进程。"""
        if self._catgirl_client is not None:
            return self._catgirl_client
        return self._require_client()

    async def _ensure_companion_client(self) -> STS2TransportClient:
        if self._catgirl_client is not None:
            return self._catgirl_client
        host = self._host_client or self._require_client()
        config = await host.get_config()
        if config.get("coop_enabled"):
            address = await self.resolve_catgirl_base_url()
            if not address:
                raise STS2TransportError("联机已启用，但未取得猫娘进程地址；不操作主机。")
            self._catgirl_client = self._build_client(address)
            self._state.catgirl_base_url = address
            # 只读发现客户端不改变驱动归属；startup/动作循环负责明确接管。
            return self._catgirl_client
        return host

    async def _read_companion_dialogue(self) -> None:
        if self._ingress_enabled:
            await self._sync_companion_ingress()
            if self._ingress_active:
                return
        records: list[Any] = []
        # memory 总线保留真人原话和精确时间；分钟级近期转录不能放行战斗回合。
        if self._sdk_bus is not None and hasattr(self._sdk_bus, "memory"):
            memory_client = self._sdk_bus.memory
            character_name = self._catgirl_llm.character_name()
            for bucket_id in dict.fromkeys((character_name, "default")):
                try:
                    # 当前 SDK 的 get_async() 仍在事件循环内执行同步 IPC。
                    # 在线程池读取，避免消息读取阻塞状态刷新和当前回合控制。
                    result = await asyncio.to_thread(
                        memory_client.get, bucket_id=bucket_id, limit=32, timeout=5.0,
                    )
                    if inspect.isawaitable(result):
                        result = await result
                    if hasattr(result, "unwrap"):
                        result = result.unwrap()
                    for item in result.items:
                        if item.type == "user_message" and item.content and item.timestamp:
                            records.append(type("DialogueRecord", (), {"timestamp": item.timestamp,
                                "lanlan_name": character_name, "turn_type": "user_message",
                                "content": item.content})())
                except Exception as exc:
                    self.logger.warning("[sts2_dialogue] 用户原话读取失败 bucket=%s：%s", bucket_id, exc)
        if self._sdk_bus is not None and hasattr(self._sdk_bus, "conversations"):
            try:
                result = self._sdk_bus.conversations.get(
                    max_count=32, since_ts=self._companion_session.started_at, timeout=5.0,
                )
                if inspect.isawaitable(result):
                    result = await result
                if hasattr(result, "unwrap"):
                    result = result.unwrap()
                records.extend(result.items)
            except Exception as exc:
                self.logger.debug("[sts2_dialogue] conversations unavailable: %s", exc)
        verified_messages = [(item.timestamp, item.content.strip()[:3000]) for item in records
                             if item.turn_type == "user_message" and item.timestamp and item.content]
        self._verified_user_messages = list(dict.fromkeys(sorted(verified_messages, key=lambda item: item[0])))
        # 运行版 conversations 目前只回 assistant。官方近期转录仍含带时间的 user 行；
        # 只在缺少 user 记录时补读，避免把导入时刻冒充发言时刻。
        if not any(getattr(item, "turn_type", "") == "user_message" for item in records):
            try:
                port = self._main_server_port()
                async with httpx.AsyncClient(timeout=5.0, trust_env=False) as client:
                    response = await client.get(f"http://127.0.0.1:{port}/get_recent_history/{self._catgirl_llm.character_name()}")
                    response.raise_for_status()
                    history = response.json()
                if isinstance(history, str):
                    pattern = re.compile(r"^([^|]+) \|(?: \[(\d{8}) \w+ (\d{2}:\d{2})\])?\s*(.*)$")
                    for line in history.splitlines():
                        match = pattern.match(line.strip())
                        if not match or not match.group(4) or not match.group(2):
                            continue
                        prefix, date, clock, content = match.groups()
                        stamp = datetime.strptime(f"{date} {clock}", "%Y%m%d %H:%M").timestamp()
                        role = "assistant_message" if prefix.strip() == self._catgirl_llm.character_name() else "user_message"
                        records.append(type("DialogueRecord", (), {
                            "timestamp": stamp, "lanlan_name": self._catgirl_llm.character_name(),
                            "turn_type": role, "content": content,
                        })())
            except Exception as exc:
                self.logger.debug("[sts2_dialogue] recent history unavailable: %s", exc)
        if not records:
            warning = "宿主 conversations/近期转录未提供可读取的对话记录；显式聊天仍进入决策上下文。"
            if self._state.input_warning != warning:
                self.logger.warning("[sts2_dialogue] %s", warning)
            self._state.input_warning = warning
            return
        self._state.input_warning = ""
        self._dialogue_record_types = {}
        for record in records:
            kind = str(record.turn_type)
            self._dialogue_record_types[kind] = self._dialogue_record_types.get(kind, 0) + 1
        self._companion_session.import_dialogue(
            records, character_name=self._catgirl_llm.character_name(),
        )

    async def _companion_host_request(self, path: str, payload: dict) -> dict:
        origin = f"http://127.0.0.1:{self._main_server_port()}"
        async with httpx.AsyncClient(timeout=70 if path == "output" else 5, trust_env=False) as client:
            if not self._ingress_csrf:
                response = await client.get(origin + "/api/config/page_config")
                response.raise_for_status()
                self._ingress_csrf = response.json()["autostart_csrf_token"]
            body = {"name": self._ingress_character or self._catgirl_llm.character_name(), "instance": self._ingress_instance, **payload}
            headers = {"Origin": origin, "X-CSRF-Token": self._ingress_csrf}
            response = await client.post(origin + "/api/sts2-companion/" + path, headers=headers, json=body)
            if response.status_code == 409 and path == "output":
                self.logger.warning("[sts2_ownership_lost] instance=%s run=%s output_id=%s reason=%s",
                                    self._ingress_instance, self._companion_session.facts.get("run_id"),
                                    payload.get("output_id"), response.json().get("detail"))
                self._ingress_active = False
                self._ingress_checkpoint_key = None
                # poll恢复既有租约规则；不强占别的实例。输出ID不变，已送文本仍去重。
                await self._sync_companion_ingress(restore_state=False)
                if not self._ingress_active:
                    raise DecisionContextChanged("同伴输出所有权已失效，等待重新接管后投递")
                response = await client.post(origin + "/api/sts2-companion/" + path, headers=headers, json=body)
                if response.status_code == 409:
                    self._ingress_active = False
                    raise DecisionContextChanged("同伴输出所有权再次变化，等待重新接管后投递")
            if response.status_code == 403:
                self._ingress_csrf = ""
            response.raise_for_status()
            result = response.json()
            if not result.get("ok") and path != "output":
                raise RuntimeError("同伴会话接口失败：" + str(result.get("reason")))
            return result

    async def _sync_companion_ingress(self, *, receive_only: bool = False, restore_state: bool = True) -> None:
        if not self._ingress_enabled:
            return
        if receive_only and not self._ingress_active:
            return
        name = self._catgirl_llm.character_name()
        if self._ingress_character and self._ingress_character != name:
            await self._release_companion_ingress()
            self._ingress_instance = str(uuid4())
            self._ingress_claim = True
            self._ingress_checkpoint_key = None
            self._ingress_seen.clear()
            self._ingress_ack.clear()
            self._ingress_callbacks.clear()
            self._host_background = self._host_persona = ""
            self._conversation_seed.clear()
            journal = self._companion_session.journal
            self._companion_session = CompanionSession()
            self._companion_session.journal.directory = journal.directory
            self._companion_session.journal.logger = self.logger
            self._companion_session.on_action = self._record_action_learning
            self._companion_session.journal.on_turn = self._record_turn_learning
            self._companion_session.observe(self._agent_view(self._state.snapshot))
            self._state.pending_guidance.clear()
            self._invalidate_companion_plan("切换角色，重新建立同伴会话")
            self._llm_planner.reset_history()
        self._ingress_character = name
        view = self._agent_view(self._state.snapshot)
        if not view and self._companion_session.journal.directory:
            # 断线期间重载：只为尚未处理/送达的原输入恢复最后观察，不恢复动作。
            files = sorted(Path(self._companion_session.journal.directory).glob("*.checkpoint.json"),
                           key=lambda p: p.stat().st_mtime, reverse=True)
            if files:
                saved = json.loads(files[0].read_text(encoding="utf-8"))
                previous = saved.get("session") or {}
                facts = previous.get("current_run") or {}
                outstanding = previous.get("pending_inputs") or any(not r["text_sent"] for r in previous.get("input_results", []))
                if saved.get("character_name") == name and facts.get("run_id") and outstanding:
                    self._companion_session.journal.activate(facts["run_id"])
                    self._companion_session.facts = deepcopy(facts)
                    self._companion_session._run_id = facts["run_id"]
                    self._companion_session.epoch += 1
                    view = {k: facts.get(k) for k in ("screen", "run_id", "turn")}
                    view.update(actions=[], available_actions=[], run={"floor": facts.get("floor"), "hp": facts.get("hp"), "character": facts.get("character")})
                    self._state.snapshot = {"agent_view": view, "raw_state": {}}
                    self.pause_autoplay(reason="decision_failure")
                    self._decision_failure_run_id = facts["run_id"]
        # 主菜单/局末可以交回普通聊天，但必须先把已生成、尚未送达的回复发完；
        # 否则先release会让_publish_companion_reply永远拿不到输出所有权（2026-10-17 20:24 卡死）。
        pending_reply = any(not record.get("text_sent") for record in self._companion_session.input_results)
        if not self._companion_mode_active() or (view.get("screen") in {None, "MAIN_MENU"}
                                                and not self._catgirl_memory.pending_review()
                                                and not pending_reply):
            await self._release_companion_ingress(reset_run=view.get("screen") == "MAIN_MENU")
            return
        if not self._ingress_lock.acquire(blocking=False):
            return
        try:
            checkpoint_key = (self._companion_session.epoch, self._companion_session.action_sequence,
                              self._companion_session.dialogue_revision, self._companion_session.generation,
                               self._companion_session.requirements_revision,
                               self._state.autoplay_state, self._state.control_generation,
                               self._state.pause_reason, self._state.decision_failed_revision,
                              self._llm_planner.metrics.get("model_calls", 0), self._decision_failure_notice,
                              self._companion_session.delivery_revision)
            sent_ack = [] if receive_only else list(self._ingress_ack)
            payload = {"anchor": self._companion_session.anchor(), "ack": sent_ack,
                       "claim": self._ingress_claim, "restore": not self._ingress_active and restore_state}
            if not receive_only and self._ingress_active and checkpoint_key != self._ingress_checkpoint_key:
                payload["checkpoint"] = {"session": self._companion_session.context(), "character_name": name, "saved_at": time()}
                payload["checkpoint"]["control_state"] = {
                    "state": self._state.autoplay_state, "pause_reason": self._state.pause_reason,
                    "stop_reason": self._state.stop_reason}
                payload["checkpoint"]["failure_pause"] = {
                    "revision": self._state.decision_failed_revision if self._state.decision_failed_revision >= 0
                        else self._companion_session.dialogue_revision,
                    "errors": self._state.decision_errors, "reason": self._state.decision_error,
                    "notice": self._decision_failure_notice,
                    "input_ids": [m["request_id"] for m in self._companion_session.user_requests if m.get("request_id")]
                } if self._state.decision_failed_revision >= 0 or self._state.pause_reason == "decision_failure" else None
                if self._ingress_checkpoint_key is None or checkpoint_key[-3] != self._ingress_checkpoint_key[-3]:
                    payload["checkpoint"].update(model_history=self._llm_planner._history, prefix=self._llm_planner._last_prefix)
                # 与过程记录同局存储，输入ack前先原子落盘；宿主重启仍能恢复已处理结果。
                self._companion_session.journal.save_checkpoint(payload["checkpoint"])
            result = await self._companion_host_request("poll", payload)
            restoring = not self._ingress_active and restore_state
            self._ingress_active = True
            self._ingress_claim = False
            self._ingress_ack = [i for i in self._ingress_ack if i not in sent_ack]
            if not receive_only:
                self._ingress_checkpoint_key = checkpoint_key
            self._notice_delivery_ready = result.get("frontend_connected", True)
            connection = result.get("frontend_connection", "")
            if connection != self._notice_connection:
                self._notice_connection = connection
                self._failure_notice_attempts = 0
                self._failure_notice_retry_at = 0.0
            self._host_persona = result["persona"]
            self._host_background = result.get("background") or ""
            if restoring:
                self._conversation_seed = result.get("dialogue_seed") or []
            saved = result.get("checkpoint") or {} if restoring else {}
            if restoring:
                local_checkpoint = self._companion_session.journal.load_checkpoint()
                if (local_checkpoint.get("character_name") == name
                        and (local_checkpoint.get("session", {}).get("current_run") or {}).get("run_id") == self._companion_session.facts.get("run_id")
                        and (not saved or local_checkpoint.get("saved_at", 0) > saved.get("saved_at", 0))):
                    saved = local_checkpoint
            previous = saved.get("session") or {}
            if restoring and (previous.get("current_run") or {}).get("run_id") == self._companion_session.facts.get("run_id"):
                self._companion_session.terminal_reply_run_id = previous.get("terminal_reply_run_id")
                self._companion_session.input_results = deepcopy(previous.get("input_results") or [])
                self._companion_session.delivery_revision += 1
                self._companion_session.messages.extend(previous.get("recent_dialogue") or [])
                restored_requests = [*(previous.get("player_messages") or []), *(previous.get("pending_inputs", previous.get("local_pending_messages")) or [])]
                self._companion_session.user_requests.extend(deepcopy(restored_requests))
                self._ingress_seen.update(m["request_id"] for m in restored_requests if m.get("request_id"))
                for record in self._companion_session.input_results:
                    for message in record["messages"]:
                        request_id = message.get("request_id")
                        if request_id:
                            self._ingress_seen.add(request_id)
                            self._ingress_ack.append(request_id)
                self._companion_session.generation += len(restored_requests)
                self._companion_session.dialogue_revision = max(
                    [int(m.get("id") or 0) for m in [*(previous.get("recent_dialogue") or []), *restored_requests,
                     *[m for r in self._companion_session.input_results for m in r["messages"]]]] or [0])
                self._companion_session.actions.extend(previous.get("recent_actions") or [])
                saved_actions = {a["id"]: a for t in self._companion_session.journal.data.get("turns", []) for a in t["actions"]}
                for action in self._companion_session.actions:
                    if action.get("id") in saved_actions:
                        action.update(deepcopy(saved_actions[action["id"]]))
                self._companion_session.restore_requirements(previous)
                self._companion_session.refresh_requirements(self._state.snapshot)
                control = saved.get("control_state") or {}
                if control.get("state") == "paused" and control.get("pause_reason") != "decision_failure":
                    self.pause_autoplay(reason=control.get("pause_reason") or "user")
                elif control.get("state") == "idle" and control.get("stop_reason"):
                    self.stop_autoplay(reason=control["stop_reason"])
                for message in [*(previous.get("recent_dialogue") or []), *restored_requests]:
                    request_id = message.get("request_id")
                    if request_id and message.get("kind") in {"chat", "query", "guidance", "immediate"}:
                        self._ingress_seen.add(request_id)
                        if request_id not in self._ingress_ack:
                            self._ingress_ack.append(request_id)
                self.logger.info("[sts2_session_restored] requests=%s dialogue_revision=%s history_messages=%s control=%s requirements=%s",
                                 [m["id"] for m in restored_requests], self._companion_session.dialogue_revision,
                                 len(saved.get("model_history") or []), control, self._companion_session.requirements_revision)
                self._llm_planner._history = saved.get("model_history") or []
                self._llm_planner._last_prefix = saved.get("prefix") or ""
                self._llm_planner._history_owner = f"{self._ingress_instance}:{self._companion_session.epoch}"
                failure_pause = saved.get("failure_pause")
                if failure_pause:
                    self.pause_autoplay(reason="decision_failure")
                    self._decision_failure_run_id = self._companion_session.facts.get("run_id")
                    self._state.decision_failed_revision = failure_pause["revision"]
                    self._state.decision_errors = failure_pause["errors"]
                    self._state.decision_error = failure_pause["reason"]
                    self._decision_failure_notice = failure_pause.get("notice", "")
            self._state.input_warning = ""
            for callback in result.get("callbacks") or []:
                if callback.get("kind") == "proactive_material":
                    # 素材更新供下次交流使用，不单独触发一次游戏重判。
                    self._ingress_material = callback
                else:
                    self._ingress_callbacks.append(callback)
            restored_new_input = False
            for item in result["inputs"]:
                request_id = item["request_id"]
                if request_id in self._ingress_seen:
                    continue
                if len(self._companion_session.pending_messages()) >= 20:
                    break  # 未处理输入留在宿主队列，不因本地有界历史淘汰而丢失。
                self._companion_session.remember("user", item["content"], timestamp=item["timestamp"])
                message = self._companion_session.user_requests[-1]
                message.update(request_id=request_id, **item.get("anchor", {}))
                self.logger.info("[sts2_input_received] %s", json.dumps({
                    "id": message["id"], "request_id": request_id, "timestamp": message["timestamp"],
                    "screen": view.get("screen"), "anchor": item.get("anchor"), "content": message["content"],
                }, ensure_ascii=False, separators=(",", ":")))
                self._ingress_seen.add(request_id)
                self._verified_user_messages.append((message["timestamp"], message["content"]))
                if restoring and saved.get("failure_pause") and request_id not in saved["failure_pause"]["input_ids"]:
                    restored_new_input = True
            if restoring and saved.get("failure_pause") and not restored_new_input:
                # 未ack的旧消息恢复时会分配本地新id，不能因此被当作新的恢复指令。
                self._state.decision_failed_revision = self._companion_session.dialogue_revision
        except (httpx.HTTPError, RuntimeError, OSError, ValueError) as exc:
            warning = "统一会话入口连接失败：" + str(exc)
            if warning != self._state.input_warning:
                self.logger.warning("[sts2_ingress] %s", warning)
            self._state.input_warning = warning
            self._ingress_active = False
        finally:
            self._ingress_lock.release()

    async def _plan_with_input(self, context: dict, **kwargs: Any) -> Any:
        generation = (self._companion_session.generation, self._state.control_generation,
                      self._state.guidance_generation, self._ingress_instance, self._companion_session.dialogue_revision,
                      self._companion_session.epoch)
        task = asyncio.create_task(self._llm_planner.plan(context, **kwargs))
        try:
            while True:
                done, _ = await asyncio.wait({task}, timeout=0.25)
                if done:
                    return task.result()
                await self._sync_companion_ingress(receive_only=True)
                current = (self._companion_session.generation, self._state.control_generation,
                           self._state.guidance_generation, self._ingress_instance, self._companion_session.dialogue_revision,
                           self._companion_session.epoch)
                if generation == current:
                    continue
                # 决策收尾时 route_messages 会把本轮消息从pending改成guidance/immediate并推进generation；
                # 这是本次决策自己产生的变化，不能反过来丢掉它刚算好的回复和要求
                # （2026-10-17 21:34:03 现场：营火不回血指令被误判stale，回复、要求、长期经验一起丢失）。
                if (generation[0] != current[0] and generation[1:] == current[1:]
                        and not self._companion_session.pending_messages()):
                    await asyncio.wait({task}, timeout=1.0)
                    if task.done():
                        return task.result()
                self.logger.info("[sts2_input_interrupt] pending=%s，取消旧决策并保留输入", len(self._companion_session.pending_messages()))
                raise DecisionContextChanged("决策期间收到新输入或控制变化，刷新后重判")
        finally:
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)

    async def _release_companion_ingress(self, *, reset_run: bool = False) -> None:
        if not self._ingress_active:
            return
        try:
            await self._companion_host_request("release", {"reset_run": reset_run})
        except (httpx.HTTPError, RuntimeError) as exc:
            self.logger.warning("[sts2_ingress_release] %s", exc)
        self._ingress_active = False
        self._push_host_context("刚才同伴会话的实际交流与行动：" + json.dumps({
            "dialogue": [{"role": m["role"], "text": m["content"][:120]} for m in list(self._companion_session.messages)[-4:]],
            "actions": [{"action": a["action_type"], "card": ((a.get("card") or {}).get("name") or (a.get("card") or {}).get("line") or "").split("：", 1)[0][:30],
                         "target": (a.get("target") or {}).get("name", "")[:30], "status": a["status"]}
                        for a in list(self._companion_session.actions)[-4:]]}, ensure_ascii=False),
            coalesce_key="sts2:conversation_handoff")

    async def _publish_companion_reply(self, text: str, messages: list[dict], *, wait_audio: bool = False,
                                       anchor: dict | None = None, system_notice: bool = False,
                                       media_requests: list[dict] | None = None, speech_kind: str = "chat",
                                       output_id: str | None = None, remember_reply: bool = True) -> dict:
        text = public_companion_text(text)
        terminal = (self._companion_session.facts.get("screen") in {"GAME_OVER", "VICTORY", "DEFEAT"}
                    and not messages and not wait_audio and not system_notice)
        run_id = self._companion_session.facts.get("run_id")
        if terminal and self._companion_session.terminal_reply_run_id == run_id:
            return {"status": "already_published"}
        if self._ingress_enabled and not self._ingress_active:
            raise RuntimeError("等待统一会话重新取得输出所有权")
        if not self._ingress_active:
            await self._push_catgirl(text, fetch_avatar=False)
            result = await speak_current_turn(self._main_server_port(), self._catgirl_llm.character_name(),
                                               text, {**(anchor or self._companion_session.anchor()),
                                                      "speech_kind": speech_kind})
        else:
            ids = [m.get("request_id") for m in messages if m.get("request_id")]
            result = await self._companion_host_request("output", {"line": text, "wait_audio": wait_audio,
                "media_requests": media_requests or [],
                "media_explicit": bool(messages),
                "system_notice": system_notice,
                "output_id": output_id or (":".join(ids) + ":" + sha1(text.encode()).hexdigest() if ids else
                    f"failure:{run_id}:" + sha1(text.encode()).hexdigest() if system_notice else f"run_end:{run_id}" if terminal else str(uuid4())),
                "anchor": {**(anchor or self._companion_session.anchor()),
                           "speech_kind": speech_kind}})
            if result.get("text_sent") is not True:
                raise RuntimeError("回复尚未送达聊天框：" + str(result.get("reason") or "text_not_sent"))
            if media_requests and result.get("reason") == "frontend_disconnected":
                raise RuntimeError("聊天窗口未连接，保留媒体请求等待重试")
            if wait_audio and result.get("audio_completed") is not True:
                raise CompanionAudioPending(result)
            self._ingress_ack.extend(ids)
        if remember_reply:
            self._companion_session.remember("assistant", text)
        self._record_input_timing(messages, "input_to_reply_ms")
        if terminal:
            self._companion_session.terminal_reply_run_id = run_id
            self._catgirl_memory.attach_process(str(run_id), self._companion_session.journal.run_context())
            self._write_history_view()
        return result

    async def _flush_companion_replies(self) -> bool:
        session = self._companion_session
        for record in session.input_results:
            audio_retry = record.get("audio_retry", False) and record.get("audio_attempts", 0) < 3
            if (record["text_sent"] and not audio_retry) or not record["reply"]:
                continue
            if time() < record.get("retry_at", 0):
                if record["text_sent"]:
                    continue
                return False
            had_text = record["text_sent"]
            try:
                # 入队就开始下一段会使TTS切换sid、取消尚未合成的回复；先送完用户回复音频。
                receipt = await self._publish_companion_reply(record["reply"], record["messages"],
                    media_requests=record["media"], speech_kind=record["kind"], wait_audio=True,
                    output_id=record["output_id"], anchor=record["anchor"], remember_reply=not had_text)
            except CompanionAudioPending as exc:
                receipt = exc.receipt
                if not had_text:
                    session.remember("assistant", record["reply"])
                record.update(text_sent=True, receipt=deepcopy(receipt), audio_completed=False,
                    audio_retry=True, audio_attempts=record.get("audio_attempts", 0) + 1,
                    retry_at=time() + self._autoplay_retry_backoff(), delivered_at=time())
                session.delivery_revision += 1
                self.logger.warning("[sts2_reply_audio_pending] output_id=%s attempts=%s reason=%s", record["output_id"], record["audio_attempts"], receipt.get("reason"))
                continue
            except (httpx.HTTPError, RuntimeError) as exc:
                record["delivery_error"] = str(exc)
                record["retry_at"] = time() + self._autoplay_retry_backoff()
                session.delivery_revision += 1
                self.logger.warning("[sts2_reply_pending] output_id=%s error=%s", record["output_id"], exc)
                return False
            record.update(text_sent=True, receipt=deepcopy(receipt),
                          audio_completed=receipt.get("audio_completed"), audio_retry=False, delivered_at=time())
            session.delivery_revision += 1
            self.logger.info("[sts2_reply_delivered] output_id=%s receipt=%s", record["output_id"], receipt)
        return True

    def _resolve_dispatch(self, operation: dict) -> tuple[dict | None, str]:
        snapshot = self._state.snapshot
        resolved, invalid = resolve_action(operation, snapshot)
        if invalid:
            return None, json.dumps(invalid, ensure_ascii=False)
        expected = operation.get("observation_guard")
        if expected is None:
            changed = view_signature(self._agent_view(snapshot)) != operation.get("view_signature")
        else:
            current = observation_guard(snapshot)
            # 队友击杀未选中的敌人不影响自身资源；目标存活由身份检查。
            expected = deepcopy(expected)
            if operation.get("action_type") == "choose_map_node" and current.get("screen") == expected.get("screen") == "MAP":
                planned_map, current_map = expected.get("map") or {}, current.get("map") or {}
                option = next((item for item in planned_map.get("options") or []
                               if item.get("i") == resolved["kwargs"].get("option_index")), {})
                coord = re.search(r"\((\d+),(\d+)\)", option.get("line", ""))
                votes = current_map.get("votes") or []
                if coord and votes and all(vote.get("coord") == coord[1] + "," + coord[2] for vote in votes):
                    planned_map["votes"] = deepcopy(votes)
            if "enemy_effects" in expected:
                alive = {e["entity_id"] for e in current.get("enemy_effects", [])}
                expected["enemy_effects"] = [e for e in expected["enemy_effects"] if e["entity_id"] in alive]
                target_id = ((operation.get("bindings") or {}).get("target") or {}).get("entity_id")
                for effects in (expected.get("enemy_effects", []), current.get("enemy_effects", [])):
                    for enemy in effects:
                        if enemy["entity_id"] != target_id:
                            enemy.pop("powers", None)
                before_player = (expected.get("combat") or {}).get("player") or {}
                after_player = (current.get("combat") or {}).get("player") or {}
                if (type(before_player.get("block")) in {int, float} and type(after_player.get("block")) in {int, float}
                        and after_player["block"] >= before_player["block"]):
                    before_player["block"] = after_player["block"]
                if current.get("screen") == expected.get("screen") == "COMBAT":
                    for key in ("actions", "available_actions"):
                        if key in current:
                            expected[key] = deepcopy(current[key])
            changed = current != expected
        if changed:
            changes = []
            if expected is not None:
                def compare(before, after, path):
                    if before == after or len(changes) >= 6:
                        return
                    if isinstance(before, dict) and isinstance(after, dict):
                        for key in sorted(before.keys() | after.keys()):
                            compare(before.get(key), after.get(key), f"{path}.{key}" if path else key)
                    elif isinstance(before, list) and isinstance(after, list):
                        for i in range(max(len(before), len(after))):
                            compare(before[i] if i < len(before) else None, after[i] if i < len(after) else None, f"{path}[{i}]")
                    else:
                        changes.append({"field": path, "before": str(before)[:120], "after": str(after)[:120]})
                compare(expected, current, "")
            self.logger.info("[sts2_observation_changed] revision=%s step=%s changes=%s",
                             operation.get("plan_revision"), operation.get("step_id"), json.dumps(changes, ensure_ascii=False))
            fields = ", ".join(change["field"] for change in changes)
            return None, "观察中的自身资源、界面或敌人效果已变化" + (f"：{fields}" if fields else "")
        return resolved, ""

    def _combat_window_closed(self, operation: dict) -> bool:
        view = self._agent_view(self._state.snapshot)
        names = {a if isinstance(a, str) else a.get("name") or a.get("type") for a in
                 view.get("available_actions", view.get("actions", []))}
        return (view.get("screen") == "COMBAT" and operation["action_type"] in {"play_card", "end_turn"}
                and not names.intersection({"play_card", "end_turn"}))

    def _current_turn_message(self) -> dict[str, Any] | None:
        anchor = self._companion_session.anchor()
        if anchor != self._turn_message_anchor:
            self._turn_message_anchor = dict(anchor)
            self._turn_message_since = time()
            self._turn_message = None
        for item in self._companion_session.user_requests:
            if (all(item.get(key) == value for key, value in anchor.items())
                    and item["timestamp"] >= self._turn_message_since
                    and (item["timestamp"], item["content"]) in self._verified_user_messages):
                self._turn_message = dict(item)
        return self._turn_message

    def _agent_view(self, snapshot: dict[str, Any]) -> dict[str, Any]:
        view = snapshot.get("agent_view") if isinstance(snapshot.get("agent_view"), dict) else {}
        return view

    async def _push_driver_standdown(self, client: STS2TransportClient) -> dict[str, Any]:
        """接管时停住 Mod 自主驱动；动作循环仍须检查确认回执。"""
        try:
            response = await client.set_config(coop_control_mode="hold", coop_manual_character_selection=True)
            echoed = str(response.get("coop_control_mode") or "").strip().lower()
            return {"applied": echoed == "hold", "mode": "hold", "echoed_mode": echoed or None}
        except Exception as exc:
            self.logger.debug(f"[sts2] pushing driver stand-down failed (non-fatal): {exc}")
            return {"applied": False}

    async def adopt_coop_catgirl(self, *, wait_seconds: float = 45.0) -> dict[str, Any]:
        """开房成功后接管猫娘：等它那个进程起来，再切成活动实例。

        猫娘是 mod 拉起的**另一个游戏进程**，要冷启动十几秒，所以这里轮询等待而不是一次探测。
        等不到就停在主机实例上（单人流程不受影响），并如实说明原因。
        """
        address = await self.resolve_catgirl_base_url()
        if not address:
            return {"adopted": False, "reason": "catgirl_address_unknown"}
        probe = self._catgirl_client or self._build_client(address)
        deadline = time() + max(0.0, float(wait_seconds))
        last_error = ""
        while time() < deadline:
            try:
                await probe.health()
            except Exception as exc:
                last_error = str(exc)
                await asyncio.sleep(1.5)
                continue
            self._catgirl_client = probe
            standdown = await self._push_driver_standdown(probe)
            result = await self.switch_active_instance(self.INSTANCE_CATGIRL, reason="coop_room_opened")
            result["mod_standdown"] = standdown
            # 猫娘进程刚起来："真人 vs 猫娘"两个客户端现在都在了，把显示名推给两边。
            result["name_sync"] = await self.sync_catgirl_name_to_mod()
            return {"adopted": bool(result.get("switched")), "base_url": address, "wait_error": last_error, **result}
        return {"adopted": False, "reason": "catgirl_not_reachable", "base_url": address, "last_error": last_error}

    async def _note_transport_failure(self) -> None:
        self._mod_gate_key = None
        # 猫娘断线时等待重连，绝不把决策和动作转给真人主机。
        self._instance_failures += 1

    async def set_instance(self, instance: str) -> dict[str, Any]:
        """显式切活动实例（host / catgirl），供玩家或排障用。"""
        result = await self.switch_active_instance(instance, reason="explicit")
        message = (
            "好，我现在对着猫娘那个进程。" if result.get("instance") == self.INSTANCE_CATGIRL
            else "好，我现在对着主机那个进程。"
        ) if result.get("switched") else "没切成功——多半是问不到猫娘进程的端口（mod 版本旧了？）。"
        return {"status": "ok" if result.get("switched") else "error", "message": message, "summary": message, **result}

    async def open_coop_room(self, host: bool = True) -> dict[str, Any]:
        """打开联机房间。

        host=True 只发一个 start_coop_session，整套编排交给 mod —— 包括拉起猫娘进程，那正是插件
        够不着的部分（LaunchCatgirlProcess 是私有的，只暴露在这一个动作后面）。插件因此不再自己拼
        动作序列，也不再自己写 coop_enabled：mod 那个动作内部会落盘。

        host=False 是加入别人的房间，没有猫娘要拉，仍自己走 open_multiplayer_menu ->
        join_multiplayer_direct（连 host 的 127.0.0.1:33771）。
        """
        client = self._require_client()
        if host:
            screen, available = await self._coop_probe(client)
            self._require_coop_action(
                self.COOP_SESSION_ACTION,
                screen=screen,
                available=available,
                key="coop.session_unavailable",
                default="{action} 不可用（screen={screen}，可用动作: {actions}）—— mod 可能没重启到含该动作的版本。",
            )
            config = await client.set_config(coop_manual_character_selection=True)
            if config.get("coop_manual_character_selection") is not True:
                raise STS2TransportError("当前 Mod 不支持手动选人确认，请更新 Mod 后再开房。")
            await client.execute_action(self.COOP_SESSION_ACTION)
            action = self.COOP_SESSION_ACTION
        else:
            action = await self._join_coop_room(client)

        self.logger.info(f"[sts2] opened co-op room ({action})")
        result = await self._coop_opened_result(host=host, action=action)
        if host:
            # 房间是交给主机开的，而**要玩的是猫娘**。主机把猫娘进程拉起来之后，插件切过去接管，
            # 并让那个实例上 mod 自己的驱动停手（否则两个驱动抢着下同一个动作）。
            # 等不到就停在主机实例上，不影响单人流程。
            result["catgirl_adoption"] = await self.adopt_coop_catgirl()
            if result["catgirl_adoption"].get("adopted"):
                result["autoplay"] = self.start_autoplay()
        return result

    async def _join_coop_room(self, client: STS2TransportClient) -> str:
        """加入方路径：必要时先回主菜单，再开多人菜单并直连 host。返回实际发出的动作名。"""
        screen, available = await self._coop_probe(client)

        # 子菜单已经开着（上一轮留下的、或 mods 子菜单）时不用再开菜单，直接发第二步。
        if self.COOP_JOIN_ACTION not in available:
            # open_multiplayer_menu 要求 currentScreen 就是 NMainMenu，主菜单上开着任何子菜单都会 409，
            # 所以先按 mod 自己的做法（NekoConfigWindow.CloseMainMenuSubmenusAsync）把子菜单收干净。
            await self._close_main_menu_submenus(client)
            screen, available = await self._coop_probe(client)
            self._require_coop_action(
                self.COOP_MENU_ACTION,
                screen=screen,
                available=available,
                key="coop.menu_unavailable",
                default="当前界面无法打开多人菜单（screen={screen}，可用动作: {actions}）。",
            )
            await client.execute_action(self.COOP_MENU_ACTION)
            screen, available = await self._coop_probe(client)

        # open_multiplayer_menu 返回 200 不代表 NMultiplayerSubmenu 真的留在栈上，所以发第二步前
        # 校验一次；否则这里会退化成 mod 那句无法定位的 "Action is not available in the current state."。
        self._require_coop_action(
            self.COOP_JOIN_ACTION,
            screen=screen,
            available=available,
            key="coop.lobby_action_unavailable",
            default="多人菜单没有打开，{action} 不可用（screen={screen}，可用动作: {actions}）。",
        )
        await client.execute_action(self.COOP_JOIN_ACTION)
        return self.COOP_JOIN_ACTION

    def _require_coop_action(
        self,
        action: str,
        *,
        screen: str,
        available: set[str],
        key: str,
        default: str,
    ) -> None:
        """动作不在 /actions/available 里就抛带 screen 与可用动作的错，而不是发出去等 mod 回 409。"""
        if action in available:
            return
        raise STS2TransportError(
            self.t(
                key,
                default=default,
                action=action,
                screen=screen,
                actions=", ".join(sorted(available)) or "无",
            )
        )

    async def _coop_opened_result(self, *, host: bool, action: str) -> dict[str, Any]:
        """刷新状态后组装返回值；screen 与可用动作都取动作之后的状态，跟调用方看到的一致。"""
        await self.refresh_state()
        snapshot = self._state.snapshot if isinstance(self._state.snapshot, dict) else {}
        screen = str(snapshot.get("screen") or "unknown")
        available = sorted(
            str(item.get("type"))
            for item in (snapshot.get("available_actions") or [])
            if isinstance(item, dict) and item.get("type")
        )
        message = self.t(
            "coop.opened",
            default="已打开联机房间：{action}，当前界面 {screen}。",
            action=action,
            screen=screen,
        )
        return {
            "status": "opened",
            "host": host,
            "action": action,
            "screen": screen,
            "available_actions": available,
            "message": message,
            "summary": message,
        }

    async def _coop_probe(self, client: STS2TransportClient) -> tuple[str, set[str]]:
        """读 /actions/available：返回 (screen, 动作名集合)。"""
        payload = await client.get_available_actions()
        names = {str(item.get("type")) for item in normalize_actions(payload) if item.get("type")}
        return str(payload.get("screen") or "unknown"), names

    async def _close_main_menu_submenus(self, client: STS2TransportClient) -> None:
        """把主菜单子菜单栈收回 NMainMenu（同 NekoConfigWindow.CloseMainMenuSubmenusAsync，防嵌套）。"""
        for _ in range(6):
            _, available = await self._coop_probe(client)
            if self.COOP_CLOSE_SUBMENU_ACTION not in available:
                return
            await client.execute_action(self.COOP_CLOSE_SUBMENU_ACTION)
            await asyncio.sleep(0.15)

    def _build_client(self, base_url: str) -> STS2TransportClient:
        """按配置里的超时造一个客户端。换活动实例时也走这里，保证两边超时一致。"""
        return STS2TransportClient(
            base_url=base_url.rstrip("/"),
            connect_timeout=float(self._cfg.get("connect_timeout_seconds", 5) or 5),
            request_timeout=float(self._cfg.get("request_timeout_seconds", 15) or 15),
        )

    async def refresh_state(self, *, trigger_sync: bool = False, plan_combat: bool = False) -> dict[str, Any]:
        previous_snapshot = self._state.snapshot if isinstance(self._state.snapshot, dict) else {}
        try:
            tick_result = await self._loop_runner.tick() if plan_combat else await self._loop_runner.tick(plan_combat=False)
        except Exception:
            # 记录猫娘读失败；不会切回真人主机接着出牌。
            await self._note_transport_failure()
            raise
        self._instance_failures = 0
        self._state.raw_state = tick_result["raw_state"]
        self._state.raw_actions = tick_result["raw_actions"]
        self._state.snapshot = tick_result["snapshot"]
        self._restore_journal_pending(self._state.snapshot)
        if self._companion_session.refresh_requirements(self._state.snapshot):
            self._last_decision_key = ""
            self._last_step_result = {}
        if self._wait_for_turn_message:
            self._current_turn_message()
        player_operation_observation = self._observe_companion_player_operation(previous_snapshot, self._state.snapshot)
        if player_operation_observation:
            self._state.latest_player_operation_observation = dict(player_operation_observation)
            self._state.remember_companion_player_op(player_operation_observation)
        else:
            self._state.latest_player_operation_observation = {}
        self._cfg["character_strategy"] = self._strategy_repository.strategy_for_snapshot(self._state.snapshot)
        self._state.transport_state = "connected"
        self._state.last_error = ""
        self._state.touch_poll()
        self._state.touch_success()
        self._remember_snapshot_metadata()
        if self._pending_action is not None:
            self._resolve_pending_action(self._state.snapshot)
        self._update_run_intent()
        await self._update_companion_feedback(self._state.snapshot)
        if trigger_sync:
            self._deliver_catgirl_sync(self._state.snapshot)
        self._emit_status()
        await self._sync_execution_status()
        message = self.t("status.refreshed", default="已刷新状态，screen={screen}", screen=self._state.snapshot.get("screen", "unknown"))
        return {
            "status": "ok",
            "message": message,
            "summary": message,
            "snapshot": self._state.snapshot,
        }

    def _rebuild_companion_snapshot(self, snapshot: dict[str, Any]) -> None:
        if not isinstance(snapshot, dict):
            return
        classification = snapshot.get("classification") if isinstance(snapshot.get("classification"), dict) else self._state_machine.classify(snapshot)
        snapshot["classification"] = classification
        summary_context = self._summary_builder.build(snapshot, runtime_state=self._state)
        snapshot["summary_context"] = summary_context
        situation_summary = self._summary_engine.summarize(summary_context)
        snapshot["situation_summary"] = situation_summary
        strategy_context = snapshot.get("strategy_context") if isinstance(snapshot.get("strategy_context"), dict) else {}
        companion_evaluation = self._companion_evaluator.evaluate(
            summary_context=summary_context,
            situation_summary=situation_summary,
            strategy_context=strategy_context,
            runtime_state=self._state,
        )
        action_frame = self._state.latest_action_frame if isinstance(self._state.latest_action_frame, dict) else {}
        if action_frame:
            companion_evaluation["action_frame"] = dict(action_frame)
        snapshot["companion_evaluation"] = companion_evaluation
        try:
            self.logger.info(
                "[sts2_companion_eval] trigger=%s should_comment=%s turn_key=%s scene_key=%s eval_key=%s summary_kind=%s",
                companion_evaluation.get("trigger"),
                companion_evaluation.get("should_comment"),
                companion_evaluation.get("turn_key"),
                companion_evaluation.get("scene_key"),
                companion_evaluation.get("evaluation_key"),
                companion_evaluation.get("summary_kind"),
            )
        except Exception:
            pass
        snapshot["catgirl_sync"] = self._catgirl_bridge.build_sync_packet(snapshot, standby=self._state.standby)

    async def run_autoplay_step(self, *, force: bool = False) -> dict[str, Any]:
        result = await self._run_autoplay_step(force=force)
        if result.get("status") != "busy":
            self._last_step_result = result
        await self._sync_execution_status()
        return result

    async def _run_autoplay_step(self, *, force: bool = False) -> dict[str, Any]:
        if (self._decision_failure_notice and self._notice_delivery_ready
                and self._failure_notice_attempts < 3 and time() >= self._failure_notice_retry_at):
            self._failure_notice_attempts += 1
            try:
                await self._publish_companion_reply(self._decision_failure_notice, [], system_notice=True)
            except (httpx.HTTPError, RuntimeError) as exc:
                self._failure_notice_retry_at = time() + 5
                self.logger.warning("[sts2_failure_notice] 原因通知投递失败，暂停状态保留：%s", exc)
            else:
                self._decision_failure_notice = ""
                self._failure_notice_retry_at = 0.0
                self.logger.info("[sts2_failure_notice] 暂停原因已送达")
        if self._state.decision_failed_revision >= 0:
            transport_blocked_input = (self._state.transport_state in {"error", "disconnected"} and self._state.decision_error == self._state.last_error
                                       and bool(self._companion_session.pending_messages()))
            if self._companion_session.dialogue_revision <= self._state.decision_failed_revision and not transport_blocked_input:
                return {"status": "paused", "executed": False, "message": self._state.decision_error}
            # 新输入可以获得有限次数的回应；动作仍等明确恢复指令。
            self._note_decision_success()
        if not await self._flush_companion_replies():
            return {"status": "reply_pending", "executed": False, "message": "等待回复送达后继续。"}
        has_input = bool(self._companion_session.pending_messages() or self._ingress_callbacks)
        actions_paused = (self._state.standby or self._state.discussion_active or self._state.autoplay_state == "paused"
                          or self._ingress_active and self._state.autoplay_state != "running" and not force)
        if actions_paused and not has_input:
            return {"status": "idle", "executed": False, "message": "已暂停动作，仍可聊天。"}
        question = self._companion_session.pending_question
        if (question.get("status") == "open" and question.get("required")
                and not self._companion_session.pending_messages() and not force):
            # 明确要求等回答的问题：在主人回答前不重新决策，也不让模型自己“等不及”行动。
            return {"status": "waiting_for_answer", "executed": False,
                    "message": "已提问，等待主人回答。"}
        if time() < self._state.retry_at:
            return {"status": "retrying", "executed": False, "message": self._state.decision_error or self._state.last_error,
                    "retry_at": self._state.retry_at}
        if not self._decision_lock.acquire(blocking=False):
            return {"status": "busy", "executed": False, "message": "正在判断当前局面。"}
        started = perf_counter()
        self._step_timing = {}
        metrics_before = deepcopy({**self._state.decision_metrics, **self._llm_planner.metrics})
        self._latency_baseline = dict(metrics_before)
        path = "model"
        operation = None
        action_anchor = self._companion_session.anchor()
        view = self._agent_view(self._state.snapshot)
        try:
            phase = perf_counter()
            game_connection = {"connected": True}
            try:
                await self.refresh_state(plan_combat=False)
            except STS2TransportError as exc:
                if not has_input or not self._ingress_active:
                    raise
                game_connection = {"connected": False, "error": str(exc), "observation": "last_known"}
                actions_paused = True
                self.logger.info("[sts2_disconnected_chat] pending=%s error=%s", len(self._companion_session.pending_messages()), exc)
            self._step_timing["refresh_ms"] = (perf_counter() - phase) * 1000
            snapshot = self._state.snapshot
            view = self._agent_view(snapshot)
            if self._pending_action is not None and game_connection["connected"]:
                return self._resolve_pending_action(snapshot)
            if not view:
                self._note_no_operation(snapshot)
                raise RuntimeError("猫娘 /state 未返回 agent_view；等待有效视图后重试，不派发动作。")
            signature = view_signature(view)
            if signature != self._last_observed_view:
                self._clear_stuck()
                self._state.progress_repeat = 0
                self._state.progress_reported = False
                self._last_observed_view = signature
            if self._state.force_replan_requested or self._state.interrupt_requested:
                self._invalidate_companion_plan(self._state.interrupt_reason or "玩家要求重新判断")
                self._state.force_replan_requested = False
                self._state.interrupt_requested = False
                self._state.interrupt_reason = ""
            if self._companion_session.facts.get("run_id") is not None:
                self._state.pending_guidance = [item for item in self._state.pending_guidance
                    if (item.get("anchor") or {}).get("run_id") in {None, self._companion_session.facts["run_id"]}]
            raw = snapshot.get("raw_state") or {}
            multiplayer = raw.get("multiplayer") or {}
            if multiplayer.get("is_multiplayer") and str(multiplayer.get("net_game_type") or "").lower() == "host":
                return {"status": "idle", "executed": False, "message": "尚未连接猫娘进程，不操作真人主机。"}
            gate = await self._sync_mod_gate("hold", discussion=bool(actions_paused)) if game_connection["connected"] else {}
            if not actions_paused and not (gate.get("applied") and gate.get("settled")):
                return {"status": "idle", "executed": False, "message": "等待 Mod 驱动停手确认。", "mod_sync": gate}
            if view.get("screen") == "MAIN_MENU" and self._catgirl_client is not None and not has_input:
                return {"status": "idle", "executed": False, "message": "等待 Mod 完成进房。"}
            if view.get("screen") == "MAIN_MENU":
                actions_paused = True
            if view.get("screen") in {"CHARACTER_SELECT", "MULTIPLAYER_LOBBY"} and not has_input:
                return {"status": "idle", "executed": False, "message": "等待玩家选择角色并手动确认开局。"}
            phase = perf_counter()
            await self._read_companion_dialogue()
            self._step_timing["input_ms"] = (perf_counter() - phase) * 1000
            unresolved_options = [option.get("i") for option in (view.get("event") or {}).get("options") or []
                                  if not option.get("locked") and not option.get("proceed")
                                  and re.search(r"\{[^{}]+\}", option.get("line") or "")]
            if view.get("screen") == "EVENT" and unresolved_options:
                wait_key = json.dumps([view.get("run_id"), (view.get("event") or {}).get("id"), unresolved_options])
                if self._event_data_wait_key != wait_key:
                    self._event_data_wait_key, self._event_data_wait_at = wait_key, time()
                    self.logger.info("[sts2_event_data_wait] options=%s，等待事件动态值就绪", unresolved_options)
                actions_paused = True
                if not (self._companion_session.pending_messages() or self._ingress_callbacks):
                    elapsed = time() - self._event_data_wait_at
                    return {"status": "waiting_for_data", "executed": False,
                            "message": "事件选项动态值尚未就绪，继续刷新；仍可聊天。" if elapsed < 10 else
                                       "事件选项动态值持续未解析，暂缓选择并继续刷新；仍可聊天。"}
            else:
                self._event_data_wait_key = ""
            if self._ingress_enabled and not self._ingress_active:
                return {"status": "idle", "executed": False, "message": self._state.input_warning or "等待统一同伴会话建立。"}
            generation = self._companion_session.generation
            if generation != self._last_dialogue_generation:
                self._invalidate_companion_plan("收到新的玩家发言")
                self._last_dialogue_generation = generation
            mode = self._state.control_mode
            control_generation = self._state.control_generation
            guidance_generation = self._state.guidance_generation
            action_anchor = self._companion_session.anchor()
            turn_message = self._current_turn_message() if self._wait_for_turn_message else None
            if (self._wait_for_turn_message and action_anchor.get("battle_id") and turn_message is None
                    and (not self._ingress_active or not self._companion_session.pending_messages())):
                self._invalidate_companion_plan()
                return {"status": "waiting_for_message", "executed": False,
                        "message": "本回合等待你说一句话，再根据这句话决策。", "anchor": action_anchor}
            plan_generation = (generation, control_generation, guidance_generation)
            dialogue_revision = self._companion_session.dialogue_revision
            new_messages = self._companion_session.pending_messages()
            if new_messages and self._companion_session.pending_question.get("status") == "open":
                # 新输入就是对上一问的回应（或已改变话题）：先解除 required 闸门。
                # 否则问句的 action_type 与后续动作相同，动作会一直被“等待回答”挡住
                # （2026-10-20 02:07 现场：回答“先打火炬头”后 play_card 仍被拦）。
                self._companion_session.answer_pending_question(new_messages)
            if (view.get("screen") in {"GAME_OVER", "VICTORY", "DEFEAT"} and not new_messages
                    and self._companion_session.terminal_reply_run_id == view.get("run_id")):
                self._ingress_callbacks.clear()
                return {"status": "idle", "executed": False, "message": "本局结算已说过，等待玩家。"}
            local = next((p for p in (raw.get("combat") or {}).get("players") or [] if p.get("is_local")), {}) if view.get("screen") == "COMBAT" else {}
            if local.get("ready_to_end_turn") is True and not new_messages:
                return {"status": "waiting_for_teammate", "executed": False, "message": "我已结束回合，等待队友。"}
            event = view.get("event") or {}
            if (view.get("screen") == "EVENT" and event.get("is_shared") and not event.get("finished")
                    and event.get("local_vote") is not None
                    and not new_messages and not self._ingress_callbacks and not force):
                self._clear_stuck()
                return {"status": "waiting_for_teammate", "executed": False,
                        "message": "共享事件投票已确认，等待队友投票或事件结算。"}
            if view.get("screen") == "MAP" and (view.get("map") or {}).get("local_vote") is not None:
                wait_key = view_signature({"run": view.get("run_id"), "floor": (view.get("run") or {}).get("floor"),
                    "map": view["map"], "requirements": self._companion_session.requirements_revision,
                    "control": self._state.control_generation})
                unchanged_wait = self._map_wait_key in {"", wait_key}
                self._map_wait_key = wait_key
                if unchanged_wait and not new_messages and not self._ingress_callbacks and not force:
                    return {"status": "waiting_for_teammate", "executed": False,
                            "message": "地图投票已确认，等待队友投票或地图变化。"}
            else:
                self._map_wait_key = ""
            action_names = {a if isinstance(a, str) else a.get("name") or a.get("type") for a in view.get("available_actions") or view.get("actions") or []}
            combat_ready = view.get("screen") != "COMBAT" or bool({"play_card", "end_turn"} & action_names)
            if combat_ready and not actions_paused and not self._requirement_wait():
                window_key = (view.get("run_id"), action_anchor.get("battle_id"), action_anchor.get("turn"))
                if self._action_window.get("key") != window_key:
                    self._action_window = {"key": window_key, "at": time(), "confirmed": False}
            if not combat_ready and not new_messages and not self._ingress_callbacks:
                # 手牌移走已能确认出牌，但动画和200ms稳定闸尚未结束；保留续步，等待下次观察。
                return {"status": "waiting_for_actions", "executed": False, "message": "等待战斗动作就绪。"}
            decision_key = view_signature({"view": view, "dialogue_generation": generation,
                                           "play_style": self._state.play_style, "mode": mode,
                                           "control_generation": control_generation, "guidance_generation": guidance_generation})
            raw_hand = {card.get("index", i): card for i, card in enumerate((raw.get("combat") or {}).get("hand") or [])}
            energy = ((view.get("combat") or {}).get("player") or {}).get("energy")

            def _affordable(card: dict) -> bool:
                source = raw_hand.get(card["index"]) or {}
                cost = source.get("energy_cost")
                if not source.get("playable") or source.get("blocked_by_hook"):
                    return False
                if cost == 0:
                    return True
                if type(cost) not in {int, float} or type(energy) not in {int, float}:
                    return False
                return cost <= energy

            # 结束回合前要比较的是"还能打且付得起"的全部牌；旧范围只有0费牌，漏掉1费充电+（Q06）。
            playable = [{"card_ref": card["ref"], "name": card["name"], "line": card["line"],
                         "card_type": card["card_type"], "energy_cost": (raw_hand.get(card["index"]) or {}).get("energy_cost")}
                        for card in action_refs(snapshot)["cards"] if _affordable(card)]
            zero_cost = [card for card in playable if card["energy_cost"] == 0]
            turn_end_check = {"energy": energy, "playable_cards": playable, "playable_zero_cost_cards": zero_cost}
            end_turn_key = view_signature({"anchor": action_anchor, "progress": self._companion_session.progress_sequence,
                                           "check": turn_end_check})
            if (self._selected_steps and self._selected_steps[0]["action_type"] == "end_turn"
                    and playable and self._end_turn_review_key != end_turn_key):
                self.logger.info("[sts2_end_turn_review] check=%s", json.dumps(turn_end_check, ensure_ascii=False))
                self._invalidate_companion_plan("结束回合前复核剩余可负担的牌")
            operation = None
            if (self._last_operation and self._last_operation.get("auto_divination")
                    and not new_messages
                    and self._selected_generation == plan_generation
                    and self._state.event_option_screen == self._event_screen_key(snapshot)):
                operation = {"action_type": "choose_event_option", "kwargs": dict(self._last_operation["kwargs"]),
                             "source": "llm", "reason": "继续已选占卜的图块操作"}
                path = "divination"
            if self._selected_steps and not new_messages and combat_ready:
                step = self._selected_steps[0]
                resolved, invalid = resolve_action(step, snapshot)
                if resolved is not None:
                    step = resolved
                if self._selected_generation == plan_generation and self._continuation_step_matches(step, view):
                    candidate = {**step, "action_type": step["action_type"], "kwargs": step["kwargs"],
                                 "source": "llm", "reason": step.get("reason") or "继续已选计划",
                                 "plan_revision": self._companion_session.active_plan.get("revision"),
                                 "step_id": step.get("step_id")}
                    if invalid is None and candidate.get("observation_guard"):
                        _, guard_error = self._resolve_dispatch(candidate)
                        if guard_error:
                            invalid = {"code": "observation_changed", "message": guard_error}
                    if (invalid is None and self._action_engine.validate(snapshot, candidate) is not None
                            and self._companion_session.requirement_blocker(candidate, snapshot) is None):
                        operation = candidate
                        path = "selected_plan"
                        self._state.decision_metrics["continuation_reused"] = self._state.decision_metrics.get("continuation_reused", 0) + 1
                        self.logger.info("[sts2_continuation] 复用续步 action=%s kwargs=%s 剩余=%s",
                                         step.get("action_type"), step.get("kwargs"), len(self._selected_steps))
                    else:
                        self.logger.info("[sts2_continuation] 续步已被现场校验拒绝 action=%s kwargs=%s",
                                         step.get("action_type"), step.get("kwargs"))
                if operation is None:
                    invalid = invalid or {"code": "context_changed" if self._selected_generation != plan_generation
                                         or not self._continuation_step_matches(step, view) else "action_illegal",
                                         "feedback": self._action_engine.validate_with_feedback(snapshot, {**step, "source": "llm"})[1]}
                    metrics = self._state.decision_metrics
                    metrics["continuation_cancelled"] = metrics.get("continuation_cancelled", 0) + 1
                    causes = metrics.setdefault("continuation_cancel_causes", {})
                    causes[invalid["code"]] = causes.get(invalid["code"], 0) + 1
                    self._invalidate_companion_plan(json.dumps(invalid, ensure_ascii=False))
            if operation is None and not new_messages and not self._ingress_callbacks and not actions_paused:
                operation = self._flow_followup(snapshot)
                if operation is not None:
                    path = "flow_followup"
            if decision_key == self._last_decision_key and not force and operation is None and not new_messages and not self._ingress_callbacks:
                if not self._requirement_wait():
                    self._note_no_operation(snapshot)
                if (self._state.stuck_reported and self._stuck_recheck_key != decision_key
                        and not self._waiting_for_teammate(snapshot)):
                    # 卡住后给模型一次含无进展事实的重判机会，不无限复用 null 决策。
                    self._stuck_recheck_key = decision_key
                    self._last_decision_key = ""
                return {"status": "idle", "executed": False, "message": "等待局面或对话变化。"}
            if operation is None:
                if self._state.stuck_reported:
                    self._stuck_recheck_key = decision_key
                conversation_plan_valid = False
                if self._selected_steps and self._selected_generation == plan_generation:
                    existing, invalid = resolve_action(self._selected_steps[0], snapshot)
                    if existing is not None and invalid is None and self._continuation_step_matches(existing, view):
                        conversation_plan_valid = self._action_engine.validate(snapshot, {**existing, "source": "llm"}) is not None
                if view.get("screen") == "COMBAT":
                    facts = build_agent_facts(snapshot.get("raw_state"))
                    self.logger.info("[sts2_decision_observation] run=%s turn=%s enemies=%s facts=%s incoming=%s orbs=%s",
                                     view.get("run_id"), view.get("turn"),
                                     json.dumps([{"entity_id": e.get("entity_id"), "name": e.get("name"),
                                                  "hp": e.get("current_hp"), "intent": e.get("intent"),
                                                  "intents": e.get("intents")} for e in
                                                 ((snapshot.get("raw_state") or {}).get("combat") or {}).get("enemies") or []
                                                 if e.get("is_alive", True)], ensure_ascii=False),
                                     json.dumps(facts.get("enemies", []), ensure_ascii=False), facts.get("incoming_damage"),
                                     json.dumps(facts.get("orbs", []), ensure_ascii=False))
                self._execution_phase = {"state": "deciding", "text": "正在决策", "detail": "结合当前局面与有效要求选择行动"}
                await self._sync_execution_status()
                decision_facts = build_agent_facts(snapshot.get("raw_state"))
                keyword_effects = build_keyword_effects(snapshot.get("raw_state"), view)
                if keyword_effects:
                    decision_facts["keyword_effects"] = keyword_effects
                    self.logger.info("[sts2_keyword_effects] matched=%s", list(keyword_effects))
                historical_question = any(re.search(r"上(?:一)?局|以前|那局|跨局|历史|经验|记忆|记住|依据|来源|哪来|怎么.*(?:总结|学|记)", m["content"]) for m in new_messages)
                experience_context = self._catgirl_memory.decision_context(view, decision_facts, include_history=historical_question)
                strategy_knowledge = self._strategy_repository.decision_knowledge(view, decision_facts)
                report_requested = any(re.search(r"效果|统计|速度|耗时|采用次数|学到了", m["content"]) for m in new_messages)
                effect_report = self._companion_session.journal.report() if report_requested else {}
                if effect_report:
                    effect_report.pop("outcomes", None)
                    effect_report["latency"] = effect_report["latency"][-8:]
                learning_report = self._catgirl_memory.learning_report() if report_requested else {}
                pending_review = self._catgirl_memory.pending_review()
                if pending_review and new_messages:
                    pending_review["process"] = CompanionJournal.reflection_context(
                        self._review_journal({"kind": "run_feedback", "run_id": pending_review["run_id"]}).data)
                if learning_report:
                    learning_report["experiences"] = learning_report["experiences"][:6]
                    learning_report.pop("events", None)
                self._learning_available = experience_context.get("experiences", [])
                self._latency_baseline = dict(metrics_before)
                # 交流提示：选项页是"问一句"最有价值的节点；战斗里按主人要求覆盖两类：
                # 多敌先问集火、怪接近斩杀/要不要用药水。每场战斗每类只提示一次，避免频繁问。
                screen = view.get("screen")
                self._decision_seq += 1
                if screen in {"REWARD", "SELECTION", "SHOP", "REST", "EVENT", "CHEST"}:
                    ask_cue = ("本页有多个可选：默认先用ask问一句自己的倾向再定（一次只问一个点）；"
                               "单选项、明显唯一或同一话题已问过就直接做。")
                elif screen == "MAP":
                    ask_cue = "地图分支：路线取舍值得商量时（精英/Boss/营火/商店）用ask问一句；明显最优就直接走。"
                elif screen == "COMBAT":
                    battle = str(action_anchor.get("battle_id") or view.get("battle_id") or view.get("run_id") or "")
                    state = self._combat_ask_state
                    if state.get("battle") != battle:
                        state = {"battle": battle, "focus": False, "lethal": False}
                        self._combat_ask_state = state
                    enemies = [item for item in (decision_facts.get("enemies") or [])
                               if isinstance(item.get("hp"), (int, float)) and item["hp"] > 0]
                    cues = []
                    if len(enemies) >= 2 and not state.get("focus"):
                        cues.append("多敌：先问主人一句先集火谁（用ask），一次只问一个点。")
                    # 斩杀判断按全攻差额，不按固定血量门槛：能确定击杀不问，差得远也不问。
                    if enemies and not state.get("lethal"):
                        cues.append("斩杀判断：先算这回合全攻的确定伤害D和敌人有效血量H（当前血量+格挡，按牌面修正）；"
                                    "D≥H是必杀，直接打，不要问；只有0<H-D小到随机/抽牌可能补上时，"
                                    "才用ask问主人要不要放弃防御全攻赌杀；差得远就正常防御，不必问。"
                                    "参考差额：第1层约10、第2层约20、第3层约30，实际会上下浮动，自己判断。"
                                    "无实体期间伤害封1，不按斩杀算；有复活/阶段转换的敌人，击杀不等于结束。")
                    ask_cue = "".join(cues)
                else:
                    ask_cue = ""
                # 情绪吐槽按决策次数冷却，避免每回合都来一句。
                banter_allowed = (self._decision_seq - self._banter_last_seq) >= 5
                operation = await self._plan_with_input(
                    {"agent_view": {**view, "available_actions": [], "actions": []} if actions_paused or not combat_ready or local.get("ready_to_end_turn") else view,
                     "actions_enabled": not actions_paused and combat_ready and not local.get("ready_to_end_turn", False),
                     "game_connection": game_connection,
                     "control_state": {"autoplay": self._state.autoplay_state, "mode": mode,
                                       "pause_reason": self._state.pause_reason},
                     "action_refs": action_refs(snapshot),
                     "turn_end_check": turn_end_check,
                     "conversation_owner": f"{self._ingress_instance}:{self._companion_session.epoch}" if self._ingress_active else None,
                     "host_callbacks": deepcopy(self._ingress_callbacks + ([self._ingress_material] if self._ingress_material else [])),
                     "host_background": self._host_background,
                     "conversation_seed": self._conversation_seed,
                     "selected_plan_valid": conversation_plan_valid,
                     "session": self._companion_session.context(),
                     "new_player_messages": new_messages, "selected_plan": [
                         {key: step[key] for key in ("action_type", "kwargs", "reason", "description", "assumptions") if key in step} for step in self._selected_steps],
                     "play_style": self._state.play_style, "map_policy": self._map_policy,
                     "player_requests": list(self._state.pending_guidance),
                      "active_requirements": self._companion_session.active_requirements(),
                     "requirement_states": self._companion_session.requirement_states(),
                      "execution_status": self.execution_status(),
                     "ask_cue": ask_cue,
                     "banter_allowed": banter_allowed,
                     "current_turn_message": turn_message,
                     "turn_speech_required": view.get("screen") == "COMBAT" and self._communicated_turn != action_anchor,
                     "potion_speech_allowed": (self._ingress_active or not self._companion_session.awaiting_chat_reply()) and (self._state.host_digest_enabled or self._danmaku_enabled),
                     "speech_allowed": (self._ingress_active or not self._companion_session.awaiting_chat_reply()) and (self._state.host_digest_enabled or self._danmaku_enabled),
                     "past_experience": experience_context,
                     "strategy_knowledge": strategy_knowledge,
                     "experience_catalog": self._catgirl_memory.experience_catalog(new_messages) if new_messages else [],
                     "recent_turn_review": self._companion_session.journal.review_context(new_messages) if new_messages else {},
                     "run_review": self._companion_session.journal.reflection_context(self._companion_session.journal.data) if view.get("screen") in {"GAME_OVER", "VICTORY", "DEFEAT"} else {},
                     "pending_run_review": pending_review,
                     "terminal_review_required": bool(self._state.run_reflection_enabled
                         and view.get("screen") in {"GAME_OVER", "VICTORY", "DEFEAT"}
                         and any(t.get("battle_id") and t.get("actions") for t in self._companion_session.journal.data.get("turns", []))
                         and not any(r.get("kind") == "run_end" for r in self._companion_session.journal.data.get("reviews", []))),
                     "learning_report": learning_report,
                     "effect_report": effect_report,
                     # 补齐 agent_view 被裁掉的意图/buff/遗物药水效果；由 raw_state 抽取，随局内事实一起给模型。
                     "agent_facts": decision_facts,
                     "no_progress": self._state.stuck_reason,
                     "divination_invalid_indices": list(self._state.event_invalid_indices)},
                    validate=lambda op: self._validate_companion_action(snapshot, op),
                    solver=lambda: self._solve_for_companion(view, plan_generation),
                    route_messages=self._companion_session.resolve_messages,
                )
                self._ingress_callbacks.clear()
                self._execution_phase = {}
                self._ingress_material = {}
                if operation is not None:
                    if getattr(operation, "banter_used", False):
                        self._banter_last_seq = self._decision_seq
                    if screen == "COMBAT" and getattr(operation, "asked", False):
                        state = self._combat_ask_state
                        if "集火" in ask_cue:
                            state["focus"] = True
                        if "斩杀" in ask_cue:
                            state["lethal"] = True
                decision = operation
                if decision is not None:
                    journal = self._review_journal(decision.review)
                    self._catgirl_memory.record_exposure(self._learning_available, view.get("run_id"),
                                                        (journal.data.get("latest") or {}).get("id", ""))
                    try:
                        self._catgirl_memory.apply_learning(decision.learning_updates, messages=new_messages, journal=journal)
                    except RuntimeError as exc:
                        # G04：记忆读取/保存失败时明确记录，不把“已记住”当成功，也不中断本局决策。
                        self.logger.warning("[sts2_memory_save_failed] %s", exc)
                    revised = {u["id"] for u in decision.learning_updates if u["op"] in {"replace", "revoke"}}
                    if any(u["id"] in revised for step in self._selected_steps for u in step.get("experience_uses") or []):
                        self._invalidate_companion_plan("采用的经验已修改或撤销，重新判断剩余步骤")
                    if decision.review:
                        journal.review(decision.review, decision.reply, new_messages)
                        if decision.review.get("kind") == "run_end":
                            self._catgirl_memory.stage_review(decision.review, decision.reply, journal)
                        elif decision.review.get("kind") == "run_feedback":
                            self._catgirl_memory.finish_review(decision.review, new_messages)
                            self._catgirl_memory.attach_process(journal.data["run_id"], journal.run_context())
                    if decision.learning_updates or decision.review:
                        self._write_history_view()
                if decision is not None and getattr(decision, "requirement_updates", None):
                    self._companion_session.apply_requirement_updates(decision.requirement_updates, snapshot, decision.as_dict(),
                                                                     messages=new_messages)
                    self._record_input_timing(new_messages, "input_to_requirement_applied_ms")
                    self._last_decision_key = ""
                    self._invalidate_companion_plan("玩家要求已更新")
                    self.logger.info("[sts2_requirements_updated] revision=%s active=%s",
                                     self._companion_session.requirements_revision,
                                     json.dumps(self._companion_session.active_requirements(), ensure_ascii=False))
                update = getattr(decision, "plan_update", "replace")
                reply = getattr(decision, "reply", "")
                control = getattr(decision, "control", "")
                if control == "pause":
                    self.pause_autoplay(reason="user")
                    actions_paused = True
                elif control == "stop":
                    self.stop_autoplay(reason="user")
                    actions_paused = True
                elif control == "resume":
                    self.resume_autoplay()
                    # 本轮仍用暂停时的观察回答；下一轮刷新后才恢复动作。
                if reply:
                    if new_messages:
                        self._companion_session.save_input_result(new_messages, reply,
                            media=decision.media_requests, kind=decision.reply_kind)
                        # 先保存处理结果和待送达回复，输入随后可ack；动作取消不重做学习。
                        self._ingress_ack.extend(m["request_id"] for m in new_messages if m.get("request_id"))
                        await self._read_companion_dialogue()
                        if not await self._flush_companion_replies():
                            return {"status": "reply_pending", "executed": False, "message": "回复已保存，等待送达。"}
                    else:
                        await self._publish_companion_reply(reply, new_messages, media_requests=decision.media_requests,
                                                            speech_kind=decision.reply_kind)
                elif new_messages and self._ingress_active and not getattr(decision, "speech", ""):
                    kinds = {m["id"]: m["kind"] for m in self._companion_session.user_requests}
                    if not (getattr(decision, "action_type", "") and update == "replace"
                            and all(kinds.get(m["id"]) in {"guidance", "immediate"} for m in new_messages)):
                        raise RuntimeError("统一会话未回答新消息；保留输入等待重试")
                    self.logger.warning("[sts2_guidance_speech_deferred] 游戏要求已分类并更新动作；发言修正失败不阻塞执行")
                # C03：reply 与 speech 使用同一套等待规则；只有 question 才等回答。
                if decision is not None and not getattr(decision, "action_type", ""):
                    ask_window = self._ask_window_seconds(decision.as_dict())
                    if ask_window > 0:
                        answered, _timed_out = await self._wait_for_answer(decision.as_dict(), window=ask_window)
                        if answered:
                            return {"status": "waiting_for_input", "executed": False,
                                    "message": "已问主人，收到回答后重新判断。"}
                        if getattr(decision, "answer_required", False):
                            return {"status": "waiting_for_answer", "executed": False,
                                    "message": "已提问，等待主人回答。"}
                if decision is not None and not decision.action_type:
                    operation = None
                if update == "wait" or actions_paused or not combat_ready:
                    self._note_decision_success()
                    self._last_decision_key = decision_key
                    return {"status": "idle", "executed": False, "message": reply or "暂时等待，仍可聊天。"}
                updated_generation = self._companion_session.generation
                if updated_generation != generation:
                    self._invalidate_companion_plan("收到新的游戏要求")
                    generation = updated_generation
                    self._last_dialogue_generation = generation
                    plan_generation = (generation, control_generation, guidance_generation)
                elif new_messages and (update == "keep" or not self._ingress_active) and self._selected_steps and self._selected_generation == plan_generation:
                    step = self._selected_steps[0]
                    step, invalid = resolve_action(step, snapshot)
                    if step is not None and self._continuation_step_matches(step, view):
                        candidate = {**step, "action_type": step["action_type"], "kwargs": step["kwargs"], "source": "llm",
                                     "reason": step.get("reason") or "继续已选计划", "plan_revision": self._companion_session.active_plan.get("revision"),
                                     "step_id": step["step_id"]}
                        if self._action_engine.validate(snapshot, candidate) is not None:
                            operation, path = candidate, "selected_plan"
                if operation is not None:
                    if path != "selected_plan":
                        operation = operation.as_dict()
            if operation is None:
                self._note_decision_success()
                self._last_decision_key = decision_key
                if not self._requirement_wait():
                    self._note_no_operation(snapshot)
                return {"status": "idle", "executed": False, "message": "等待当前界面可行动。"}
            blocker = self._companion_session.requirement_blocker(operation, snapshot)
            if blocker:
                self._invalidate_companion_plan(blocker["reason"])
                self._note_decision_success()
                self._last_decision_key = ""
                return {"status": "waiting_for_condition", "executed": False, "message": blocker["reason"],
                        "requirement_id": blocker.get("requirement_id")}
            if operation["action_type"] == "end_turn" and playable and path != "selected_plan":
                self._end_turn_review_key = end_turn_key
            operation = self._advance_custom_event_cursor(snapshot, operation)
            operation["view_signature"] = signature
            operation["observation_guard"] = observation_guard(snapshot)
            operation["session_generation"] = generation
            operation["dialogue_revision"] = dialogue_revision
            operation["control_mode"] = mode
            operation["control_generation"] = control_generation
            operation["guidance_generation"] = guidance_generation
            operation["consumed_guidance_generation"] = guidance_generation
            if path != "selected_plan":
                operation["flow_anchor"] = {"run_id": view.get("run_id"), "floor": (view.get("run") or {}).get("floor"),
                                            "event_id": (view.get("event") or {}).get("id")}
                guidance_ids = {m["id"] for m in self._companion_session.user_requests if m.get("kind") in {"guidance", "immediate"}}
                guidance_messages = [m for m in new_messages if m["id"] in guidance_ids]
                if guidance_messages:
                    operation["input_received_at"] = min(m["timestamp"] for m in guidance_messages)
                uses = list(operation.get("experience_uses") or [])
                for i, candidate in enumerate([operation, *operation.get("continuation", [])]):
                    candidate["experience_uses"] = [deepcopy(u) for u in uses if i in u.get("steps", [])]
                cached_solver = self._solver_cache[1] if self._solver_cache else {}
                solver_result = cached_solver.get("solver_result") or {}
                operation["solver_summary"] = {k: deepcopy(v) for k, v in solver_result.items()
                                               if k in {"score", "scores", "coverage", "warnings", "confidence", "win_prob", "mode"}}
                planned_intents = enemy_intents(snapshot)
                operation["enemy_intents"] = planned_intents
                for step in operation.get("continuation") or []:
                    step["enemy_intents"] = planned_intents
                self._companion_session.accept_plan(operation, view)
                if guidance_messages and reply and not operation.get("speech"):
                    self._companion_session.announce_plan({**operation, "speech_step_ids": [0]}, reply, "guidance")
                operation["input_ids"] = [m.get("request_id") for m in new_messages if m.get("request_id")]
                self._selected_generation = plan_generation
                self.logger.info("[sts2_plan_selected] revision=%s step=%s solver_used=%s solver_adopted=%s reason=%s remaining=%s",
                                 operation["plan_revision"], operation["step_id"], operation.get("solver_used", False),
                                 operation.get("solver_adopted", False), operation.get("reason"), [s["description"] for s in self._selected_steps])
            self._deliver_host_context(snapshot)
            snapshot["planned_operation"] = operation
            snapshot["agent_operation"] = operation
            snapshot["executable_operation"] = self._action_engine.validate(snapshot, operation)
            # 没有执行权限也可以思考；仅 program 自动执行，force 仍不能绕过讨论暂停。
            if mode != "program" and not force:
                self._note_decision_success()
                self._last_decision_key = decision_key
                return {"status": "idle", "executed": False, "planned_operation": operation,
                        "message": operation.get("reason") or "已判断，当前暂停执行。"}
            phase = perf_counter()
            result = await self.execute_operation(operation)
            self._step_timing["dispatch_and_confirmation_ms"] = (perf_counter() - phase) * 1000
            self.logger.info("[sts2_llm_action] action=%s%s status=%s executed=%s message=%s revision=%s step=%s",
                             operation["action_type"], self._action_card_hint(operation, view),
                             result.get("status"), result.get("executed"), result.get("message"),
                             operation.get("plan_revision"), operation.get("step_id"))
            if result.get("executed"):
                self._note_decision_success()
                self._last_decision_key = decision_key
                self._state.step_count += 1
                self._last_operation = operation
                plan_still_current = plan_generation == (self._companion_session.generation,
                    self._state.control_generation, self._state.guidance_generation)
                if plan_still_current:
                    self._companion_session.complete_step(operation)
                if path != "selected_plan" and plan_still_current:
                    if self._selected_steps:
                        self.logger.info("[sts2_continuation] 模型返回续步 n=%s，下次可省一次模型调用", len(self._selected_steps))
                    else:
                        self.logger.info("[sts2_continuation] 模型未返回续步，本步仍需单独决策")
                if path == "model" and plan_still_current and operation.get("future_plan") is not None:
                    # 执行前锚点，不能把 end_turn 后的新回合当成计划起点。
                    future = operation.get("future_plan") or {}
                    if self._companion_session.facts.get("battle_id") == action_anchor.get("battle_id"):
                        self._companion_session.select_plan(future, anchor=action_anchor)
                if self._agent_view(self._state.snapshot) != view:
                    self._clear_stuck()
                    self._state.progress_repeat = 0
                elif operation.get("auto_divination"):
                    index = (operation.get("kwargs") or {}).get("option_index")
                    if index not in self._state.event_invalid_indices:
                        self._state.event_invalid_indices.append(index)
                    self.logger.info("[sts2_divination] 无变化选项 index=%s", index)
                    self._last_decision_key = ""
                else:
                    self._note_step_progress(snapshot, operation)
            elif result.get("status") == "error":
                raise RuntimeError(result.get("message") or "动作未被接收")
            elif result.get("status") == "waiting_for_actions":
                if self._selected_steps and self._selected_steps[0].get("step_id") == operation.get("step_id"):
                    self._selected_steps[0]["observation_guard"] = deepcopy(operation.get("observation_guard"))
                self._execution_phase = {"state": "waiting", "text": "等待界面就绪", "detail": "保留已选计划，等待动作窗口恢复"}
                self.logger.info("[sts2_plan_waiting] revision=%s step=%s", operation.get("plan_revision"), operation.get("step_id"))
            elif result.get("status") == "waiting_for_input":
                # 已问主人、窗口内收到回答：丢弃当前动作，保留回答输入等下一轮重判。
                self._invalidate_companion_plan("已问主人，收到回答后重新判断")
                self._note_decision_success()
                self._execution_phase = {"state": "waiting", "text": "等主人回答", "detail": "已提出问题，收到回答后重新判断"}
                self.logger.info("[sts2_ask_window] 保留回答输入，等待重判 revision=%s", operation.get("plan_revision"))
            elif result.get("status") in {"stale", "unconfirmed"}:
                self._companion_session.record_action(operation, anchor=action_anchor, before=view,
                    after=self._agent_view(self._state.snapshot), result=result)
                dialogue_only = (result.get("status") == "stale" and self._companion_session.pending_messages()
                    and self._companion_session.generation == generation
                    and view_signature(self._agent_view(self._state.snapshot)) == signature
                    and self._state.control_generation == control_generation
                    and self._state.guidance_generation == guidance_generation)
                if not dialogue_only:
                    self._invalidate_companion_plan(result.get("message") or "动作未确认")
            elif result.get("status") == "idle":
                self._note_no_operation(snapshot)
            self._deliver_host_context(self._state.snapshot)
            return result
        except SolverPhaseWait as exc:
            # 过渡帧不是失败：保留原计划、不记错误、不重判，等下一次观察拿到可行动现场。
            self.logger.info("[sts2_solver_phase_wait] %s", exc)
            return {"status": "waiting_for_actions", "executed": False, "message": str(exc)}
        except DecisionContextChanged as exc:
            self._invalidate_companion_plan(str(exc))
            self._last_decision_key = ""
            result = {"status": "stale", "executed": False, "message": str(exc)}
            self.logger.info("[sts2_decision_stale] %s", exc)
            return result
        except STS2ActionRejected as exc:
            result = {"status": "rejected", "executed": False, "message": str(exc)}
            if operation is not None:
                self._companion_session.record_action(operation, anchor=action_anchor, before=view,
                    after=self._agent_view(self._state.snapshot),
                    result={"status": "rejected", "executed": False, "message": str(exc)})
            await self.refresh_state(plan_combat=False)
            fresh = self._agent_view(self._state.snapshot)
            names = {a if isinstance(a, str) else a.get("name") or a.get("type") for a in
                     fresh.get("available_actions", fresh.get("actions", []))}
            if (operation and view.get("screen") == fresh.get("screen") == "COMBAT"
                    and view.get("run_id") == fresh.get("run_id") and view.get("turn") == fresh.get("turn")
                    and operation["action_type"] not in names):
                result = {"status": "waiting_for_actions", "executed": False, "message": "动作窗口暂时关闭，等待新现场恢复。"}
                return result
            self._invalidate_companion_plan(str(exc))
            self._deliver_host_context(self._state.snapshot)
            raise
        except STS2TransportError:
            result = {"status": "transport_error", "executed": False}
            raise
        except Exception as exc:
            result = {"status": "error", "executed": False, "message": str(exc) or type(exc).__name__}
            for message in locals().get("new_messages", []):
                if any((message.get("request_id") or message["id"]) in r["ids"]
                       for r in self._companion_session.input_results):
                    continue
                if message.get("request_id") and message["request_id"] not in self._ingress_ack:
                    for pending in self._companion_session.user_requests:
                        if pending["id"] == message["id"]:
                            pending["kind"] = "pending"
            self._invalidate_companion_plan()
            self._state.decision_error = str(exc) or "LLM 决策超时"
            self._state.decision_errors += 1
            paused = self._pause_after_decision_failures()
            if not paused:
                self._state.retry_at = time() + self._autoplay_retry_backoff()
            self.logger.warning("[sts2_llm_decision] 自主决策失败 count=%s paused=%s：%s",
                                self._state.decision_errors, paused, self._state.decision_error)
            self._emit_status()
            return {"status": "paused" if paused else "error", "executed": False,
                    "message": self._state.decision_error, "retry_at": self._state.retry_at}
        finally:
            self._step_timing["model_ms"] = self._llm_planner.metrics.get("model_ms", 0) - metrics_before.get("model_ms", 0)
            self._step_timing["solver_ms"] = self._state.decision_metrics.get("solver_ms", 0) - metrics_before.get("solver_ms", 0)
            if operation is not None or self._llm_planner.metrics.get("model_calls", 0) != metrics_before.get("model_calls", 0):
                self._record_latency(started, path, locals().get("result", {}))
            self._state.decision_metrics.update(self._llm_planner.metrics)
            if locals().get("result", {}).get("status") in {"waiting_for_condition", "waiting_for_teammate", "waiting_for_message", "waiting_for_actions", "paused"}:
                self._action_window = {}
                self._last_confirmed = {}
            self._execution_phase = {}
            self._decision_lock.release()

    def _note_decision_success(self) -> None:
        self._state.decision_errors = 0
        self._state.decision_error = ""
        self._state.retry_at = 0.0
        self._state.decision_failed_revision = -1

    def _pause_after_decision_failures(self) -> bool:
        if self._state.decision_errors < 3:
            return False
        if self._state.decision_failed_revision >= 0:
            return True
        self._state.decision_failed_revision = self._companion_session.dialogue_revision
        self._decision_failure_run_id = self._companion_session.facts.get("run_id")
        self._state.retry_at = 0.0
        if self._state.autoplay_state == "running":
            self.pause_autoplay(reason="decision_failure")
        self._decision_failure_notice = (
            f"连续{self._state.decision_errors}轮失败，已暂停自动行动。原因：{self._state.decision_error}。"
            "等主人说继续，或排查后再恢复。")
        self._failure_notice_retry_at = 0.0
        self._failure_notice_attempts = 0
        self.logger.warning("[sts2_decision_paused] count=%s reason=%s",
                            self._state.decision_errors, self._state.decision_error)
        return True

    def _invalidate_companion_plan(self, reason: str = "局面或控制状态改变，重新判断") -> None:
        self._last_decision_key = ""
        if self._selected_steps:
            self.logger.info("[sts2_plan_cancelled] revision=%s reason=%s",
                             self._companion_session.active_plan.get("revision"), reason)
        self._companion_session.invalidate_plan(reason)
        self._selected_generation = None
        self._companion_session.future_plan.clear()
        for key in ("planned_operation", "agent_operation", "executable_operation"):
            self._state.snapshot.pop(key, None)

    def _clear_previous_run_failure(self) -> None:
        run_id = self._companion_session.facts.get("run_id")
        if run_id and run_id != "run_unknown" and self._decision_failure_run_id and run_id != self._decision_failure_run_id:
            if self._state.pause_reason == "decision_failure":
                self._state.autoplay_state = "idle"
                self._state.pause_reason = ""
            self._note_decision_success()
            self._decision_failure_notice = ""
            self._failure_notice_attempts = 0
            self._failure_notice_retry_at = 0.0
            self._decision_failure_run_id = None

    def _reset_companion_run_context(self) -> None:
        self._clear_previous_run_failure()
        self._map_wait_key = ""
        self._solver_cache = None
        self._llm_planner.reset_history()
        self._conversation_seed.clear()
        self._ingress_callbacks.clear()
        self._ingress_seen.clear()
        self._invalidate_companion_plan()
        self._last_operation = None
        self._last_observed_view = ""
        self._companion_push_keys.clear()
        self._proactive_observation = {}
        self._communicated_turn = {}
        self._speech_state = {}
        self._execution_phase = {}
        self._last_step_result = {}
        self._pending_action = None
        self._unconfirmed_action = None
        self._feedback_observation = {}
        self._state.pending_guidance.clear()
        self._state.latest_action_frame = {}
        self._state.recent_action_frames.clear()
        self._state.latest_snapshot_summary = {}
        self._state.latest_continuous_delta = {}
        self._state.recent_continuous_deltas.clear()
        self._state.latest_player_operation_observation = {}
        self._state.last_host_context_key = ""
        self._state.recent_companion_player_ops.clear()
        self._state.recent_decision_memory.clear()
        self._state.hp_trail.clear()
        self._state.hp_trail_run_id = ""
        self._state.route_log.clear()
        self._state.route_room_key = ""
        self._state.route_start = {}
        self._state.route_last = {}
        self._state.event_option_screen = ""
        self._state.event_invalid_indices.clear()
        self._state.event_option_cursor = 0
        self._state.progress_repeat = 0
        self._state.progress_reported = False
        self._clear_stuck()

    async def _solve_for_companion(self, view: dict[str, Any], generation: tuple[int, int, int]) -> dict[str, Any]:
        if view.get("screen") != "COMBAT":
            return {"status": "unavailable", "error": "当前不是战斗界面，请直接处理现场合法选项"}
        combat = ((self._state.snapshot or {}).get("raw_state") or {}).get("combat") or {}
        local = next((p for p in combat.get("players") or [] if p.get("is_local")), {})
        if local.get("is_action_phase") is False or local.get("ready_to_end_turn") is True:
            # 出牌动画/结算/已按结束等待期间调用必然失败；旧实现把它当普通错误交给模型，模型转手算23秒。
            return {"status": "not_ready", "error": "当前不在玩家可行动阶段，等新鲜现场后再求解"}
        client = await self._ensure_companion_client()
        observed = await client.get_state()
        fresh = observed.get("agent_view") or {}
        current_generation = (self._companion_session.generation, self._state.control_generation, self._state.guidance_generation)
        if (observation_guard({"agent_view": fresh}) != observation_guard({"agent_view": view})
                or current_generation != generation or self._state.discussion_active or self._state.standby):
            return {"status": "stale", "error": "局面、对话或控制状态改变，请重新判断"}
        key = view_signature({"view": fresh, "generation": generation})
        metrics = self._state.decision_metrics
        if self._solver_cache and self._solver_cache[0] == key:
            metrics["solver_cache_hits"] = metrics.get("solver_cache_hits", 0) + 1
            return self._solver_cache[1]
        started = perf_counter()
        metrics["solver_calls"] = metrics.get("solver_calls", 0) + 1
        try:
            plan = await client.get_combat_plan(companion=True)
        except STS2TransportError as exc:
            result = {"status": "error", "error": str(exc)}
            self._solver_cache = (key, result)
            return result
        finally:
            metrics["solver_ms"] = metrics.get("solver_ms", 0.0) + (perf_counter() - started) * 1000
        after = (await client.get_state()).get("agent_view") or {}
        if (observation_guard({"agent_view": after}) != observation_guard({"agent_view": fresh})
                or current_generation != (self._companion_session.generation, self._state.control_generation, self._state.guidance_generation)):
            return {"status": "stale", "error": "求解期间局面或要求改变，结果作废"}
        if plan.get("search_status") == "phase_not_ready":
            return {"status": "not_ready", "error": "求解器判定尚未进入玩家出牌阶段，等新鲜现场后再求解"}
        if not plan.get("in_combat") or not plan.get("line"):
            result = {"status": "error", "error": "现有求解器未产出战斗方案", "solver_result": plan}
            self._solver_cache = (key, result) if fresh == after else None
            return result
        result = {"status": "ok", "solver_result": plan, "observation_version": view_signature(fresh),
                  "scores_current": fresh == after, "current_view": after,
                  "solver_refs": {k: [{key: value for key, value in r.items() if key != "entity_id"} for r in values]
                                  for k, values in action_refs({"agent_view": fresh, "raw_state": observed}).items()},
                  "interpretation": "自己进程原有模拟输出；保留原信息。未来抽牌/行为为预测；win_prob 是启发式代理值，confidence/coverage/warnings 表示能力限制。无参数，不支持用户约束。"}
        self._solver_cache = (key, result) if fresh == after else None
        return result

    def _latency_configuration(self) -> str:
        key = getattr(self._llm_planner, "_llm_key", None) or [None, None]
        return sha1(json.dumps({"endpoint": key[0], "model": key[1], "limit": self._llm_planner._max_tokens,
            "style": self._state.play_style, "speech": self._state.host_digest_enabled,
            "wait_for_turn": self._wait_for_turn_message, "persona": self._host_persona,
            "character": self._ingress_character, "danmaku": self._danmaku_enabled},
            sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:12]

    def _record_input_timing(self, messages: list[dict], measure: str) -> None:
        journal = self._companion_session.journal
        if not journal.data:
            return
        recorded = journal.data.setdefault("input_timings", {})
        for item in messages:
            source = next((m for m in self._companion_session.user_requests
                           if item.get("request_id") and item.get("request_id") == m.get("request_id")
                           or item.get("id") and item["id"] == m["id"]), item)
            if not source.get("timestamp"):
                continue
            key = f"{source.get('request_id') or source['id']}:{measure}"
            if key not in recorded:
                value = max(0, (time() - source["timestamp"]) * 1000)
                recorded[key] = value
                journal.latency({"timestamp": time(), "configuration": self._latency_configuration(),
                    "path": "input", "measures": {measure: value}, "model_calls": 0, "solver_calls": 0})

    def _record_action_learning(self, record: dict) -> None:
        record.setdefault("id", str(uuid4()))
        self._catgirl_memory.record_experience_result(record)
        if not record.get("executed"):
            return
        now = time()
        key = tuple(record.get(k) for k in ("run_id", "battle_id", "turn"))
        measures = {}
        if self._action_window.get("key") == key and not self._action_window.get("confirmed"):
            measures["first_action_confirmation_ms"] = (now - self._action_window["at"]) * 1000
            self._action_window["confirmed"] = True
        if self._last_confirmed.get("key") == key and self._last_confirmed.get("id") != record["id"]:
            measures["continuation_interval_ms"] = (now - self._last_confirmed["at"]) * 1000
        self._last_confirmed = {"key": key, "at": now, "id": record["id"]}
        if record.get("input_received_at"):
            measures["input_to_action_ms"] = (now - record["input_received_at"]) * 1000
        self._companion_session.journal.latency({"timestamp": now, "configuration": self._latency_configuration(),
            "path": "action_confirmed", "measures": measures, "model_calls": 0, "solver_calls": 0})

    def _record_turn_learning(self, turn: dict) -> None:
        self._catgirl_memory.record_turn_result(turn)

    def _restore_journal_pending(self, snapshot: dict) -> None:
        journal = self._companion_session.journal
        if not journal.restored:
            return
        journal.restored = False
        telemetry = journal.data.get("telemetry") or {}
        for name in ("hp_trail", "combat_room_id", "route_log", "route_room_key", "route_start", "route_last"):
            if name in telemetry:
                setattr(self._state, name, deepcopy(telemetry[name]))
        if telemetry:
            self._state.hp_trail_run_id = journal.data["run_id"]
            self._telemetry_gap = True
        raw, view = snapshot.get("raw_state") or {}, self._agent_view(snapshot)
        combat = raw.get("combat") or {}
        for turn in journal.data.get("turns", []):
            for record in list(turn["actions"]):
                if record["status"] != "pending":
                    continue
                same_turn = raw.get("turn") == record.get("turn")
                local = next((p for p in combat.get("players") or [] if p.get("is_local")), {})
                completed = record["action_type"] == "end_turn" and (
                    local.get("ready_to_end_turn") is True or raw.get("turn") is not None
                    and record.get("turn") is not None and raw["turn"] > record["turn"])
                if record["action_type"] == "play_card" and same_turn and record.get("card_entity_id"):
                    completed = isinstance(combat.get("hand"), list) and all(c.get("entity_id") != record["card_entity_id"] for c in combat["hand"])
                record.update(status="completed" if completed else "unconfirmed", executed=bool(completed),
                    message="重载后现场确认完成。" if completed else "重载后保留未确认结果，停止原动作重发。",
                    outcome={"screen": view.get("screen"), "turn": view.get("turn"),
                             "player": deepcopy((view.get("combat") or {}).get("player")), "hp": (view.get("run") or {}).get("hp")})
                journal.confirm_action(record, view)
                self._catgirl_memory.record_experience_result(record)
                if not completed:
                    self._unconfirmed_action = (view_signature(view), record["action_type"], public_kwargs(record.get("kwargs")))

    def _record_latency(self, started: float, path: str, result: dict | None = None) -> None:
        metrics = {**self._state.decision_metrics, **self._llm_planner.metrics}
        self._state.decision_metrics = metrics
        sample = {"timestamp": time(), "path": path, "perception_to_action_ms": (perf_counter() - started) * 1000,
                  "stages": dict(getattr(self, "_step_timing", {})),
                  "model_calls": metrics.get("model_calls", 0), "solver_calls": metrics.get("solver_calls", 0)}
        sample["model_calls"] -= getattr(self, "_latency_baseline", {}).get("model_calls", sample["model_calls"])
        sample["solver_calls"] -= getattr(self, "_latency_baseline", {}).get("solver_calls", sample["solver_calls"])
        sample["configuration"] = self._latency_configuration()
        sample["outcome"] = (result or {}).get("status", "reply_or_wait")
        sample["measures"] = {"decision_step_ms": sample["perception_to_action_ms"]}
        baseline = getattr(self, "_latency_baseline", {})
        sample["counts"] = {key: max(0, metrics.get(key, 0) - baseline.get(key, 0)) for key in
                            ("continuation_reused", "continuation_cancelled", "solver_cache_hits", "prompt_tokens", "cached_tokens")}
        causes = metrics.get("continuation_cancel_causes", {})
        sample["cancellation_causes"] = {key: value - baseline.get("continuation_cancel_causes", {}).get(key, 0)
                                         for key, value in causes.items() if value > baseline.get("continuation_cancel_causes", {}).get(key, 0)}
        self._state.latency_samples.append(sample)
        self._state.latency_samples = self._state.latency_samples[-20:]
        self.logger.info("[sts2_latency] %s", json.dumps(sample, ensure_ascii=False))
        self._companion_session.journal.latency(sample)

    def _map_vote_candidates(self, snapshot: dict, session: Any = None) -> list[dict]:
        view = self._agent_view(snapshot)
        route = view.get("map") or {}
        if view.get("screen") != "MAP" or "local_vote" not in route or route["local_vote"] is not None:
            return []
        session = session or self._companion_session
        candidates = []
        for option in route.get("options") or []:
            candidate = {"action_type": "choose_map_node", "kwargs": {"option_index": option["i"]},
                         "source": "llm", "reason": "完成自己的地图投票"}
            if (self._action_engine.validate(snapshot, candidate) is not None
                    and session.requirement_blocker(candidate, snapshot) is None):
                candidates.append(candidate)
        return candidates

    def _flow_followup(self, snapshot: dict) -> dict | None:
        """仅推进已完成选择后的无取舍流程，仍经过同一派发校验。"""
        view = self._agent_view(snapshot)
        if (len((view.get("map") or {}).get("options") or []) == 1
                and (candidates := self._map_vote_candidates(snapshot))):
            return {**candidates[0], "reason": "地图只有一个合法选项，完成自己的投票"}
        if (view.get("screen") == "MAP" and self._map_policy == MAP_POLICY_FOLLOW
                and not self._companion_session.active_requirements() and (view.get("map") or {}).get("local_vote") is None):
            index = self._planner._other_player_vote_index(snapshot.get("summary_context") or {})
            if index is not None:
                candidate = {"action_type": "choose_map_node", "kwargs": {"option_index": index},
                             "source": "llm", "reason": "按已设定的跟票方式投票"}
                if self._action_engine.validate(snapshot, candidate) is not None:
                    return candidate
        previous = self._last_operation or {}
        anchor = previous.get("flow_anchor") or {}
        if (not anchor or anchor.get("run_id") != view.get("run_id")
                or anchor.get("floor") != (view.get("run") or {}).get("floor")):
            return None
        names = {a if isinstance(a, str) else a.get("name") or a.get("type") for a in
                 view.get("available_actions", view.get("actions", []))}
        kind = previous.get("action_type")
        action = None
        if (view.get("screen") == "EVENT" and kind in {"choose_event_option", "advance_event_dialogue"}
                and anchor.get("event_id") == (view.get("event") or {}).get("id")):
            action = "advance_event_dialogue" if "advance_event_dialogue" in names else "proceed" if "proceed" in names and "choose_event_option" not in names else None
        elif kind in {"claim_reward", "choose_reward_card", "skip_reward_cards", "resolve_rewards", "collect_rewards_and_proceed", "choose_rest_option", "choose_treasure_relic"}:
            reward = view.get("reward") or {}
            if not any(r.get("claimable") for r in reward.get("rewards") or []) and not reward.get("pending_card_choice"):
                action = next((a for a in ("collect_rewards_and_proceed", "proceed") if a in names), None)
        if action is None:
            return None
        candidate = {"action_type": action, "kwargs": {}, "source": "llm", "reason": "继续已选方案的界面收尾"}
        return candidate if self._action_engine.validate(snapshot, candidate) is not None else None

    def _event_screen_key(self, snapshot: dict[str, Any]) -> str:
        event = self._agent_view(snapshot).get("event") or (snapshot.get("raw_state") or {}).get("event") or {}
        return f"{snapshot.get('run_id')}|{snapshot.get('screen')}|{snapshot.get('floor')}|{event.get('id', event.get('event_id'))}"

    # 续步匹配只看这些稳定字段：同一屏、同一局、同一回合。
    # 战斗里 hand/energy/cards_played_this_turn/牌堆每打一张就变，模型无法预测出完整视图逐字段相等，
    # 旧判据 step["expected_view"] == view 因此永远命中不了（现场 51 条延迟样本 path 全为 model）。
    # 现在只比模型**明确给出**的稳定字段，其余交给 self._action_engine.validate 按现场兜底，
    # 与求解器复用的 _plan_step_is_executable 同一套保守思路。
    _CONTINUATION_CONTEXT_KEYS = ("screen", "run_id", "turn")

    def _continuation_context(self, view: dict[str, Any], *, keys: tuple[str, ...]) -> dict[str, Any]:
        if not isinstance(view, dict):
            return {}
        return {key: view.get(key) for key in keys if key in view}

    def _continuation_step_matches(self, step: dict[str, Any], view: dict[str, Any]) -> bool:
        """续步在当前现场还该不该走。

        判据（与求解器一致，保守：拿不准就走，让动作层拒绝）：
        - 模型在 expected_view 里明确给出的稳定字段（screen/run_id/turn/actions/敌人）必须与现场一致；
          没给的字段不参与比较（不同回合号、不同屏幕一定作废）。
        - play_card 额外按下标比对手牌 `line`：出牌会让手牌位置漂移，下标可能指到别的牌上。
        """
        expected = step.get("expected_view") if isinstance(step.get("expected_view"), dict) else {}
        if expected:
            keys = [key for key in self._CONTINUATION_CONTEXT_KEYS if key in expected]
            if keys and self._continuation_context(expected, keys=tuple(keys)) != \
                    self._continuation_context(view, keys=tuple(keys)):
                return False
        if str(step.get("action_type") or "") == "play_card":
            return self._play_card_continuation_intact(step, view)
        return True

    def _play_card_continuation_intact(self, step: dict[str, Any], view: dict[str, Any]) -> bool:
        """续步要打的牌，下标处仍是同一张牌吗？按 `line` 比对（位置会漂，牌面不会）。"""
        if step.get("bindings"):
            return True  # 实体和条件已在 resolve_action 检查。
        kwargs = step.get("kwargs") if isinstance(step.get("kwargs"), dict) else {}
        index = kwargs.get("card_index")
        if type(index) is not int or index < 0:
            return False
        combat = view.get("combat") if isinstance(view.get("combat"), dict) else {}
        hand = combat.get("hand") if isinstance(combat.get("hand"), list) else []
        if index >= len(hand) or not isinstance(hand[index], dict):
            return False
        expected_view = step.get("expected_view") if isinstance(step.get("expected_view"), dict) else {}
        expected_hand = (expected_view.get("combat") or {}).get("hand") if isinstance(expected_view.get("combat"), dict) else None
        if not isinstance(expected_hand, list) or index >= len(expected_hand) or not isinstance(expected_hand[index], dict):
            # 没有可对比的预期牌面：交给动作引擎按现场校验，不在这里否决。
            return True
        return str(hand[index].get("line") or "") == str(expected_hand[index].get("line") or "")

    def _advance_custom_event_cursor(self, snapshot: dict[str, Any], operation: dict[str, Any]) -> dict[str, Any]:
        """模型选择继续自定义占卜后，仅推进接口实际暴露的合法图块下标。"""
        if str(operation.get("action_type") or "") != "choose_event_option":
            return operation
        raw = snapshot.get("raw_state") if isinstance(snapshot.get("raw_state"), dict) else {}
        event = self._agent_view(snapshot).get("event") or raw.get("event") or {}
        progress = event.get("screen_progress") or (raw.get("event") or {}).get("screen_progress")
        # 只有自定义事件屏才有 screen_progress；常规事件屏（NEventRoom）照旧听规划器的。
        if not progress:
            self._state.event_option_screen = ""
            self._state.event_invalid_indices.clear()
            return operation
        options = event.get("options") if isinstance(event.get("options"), list) else []
        chosen = (operation.get("kwargs") or {}).get("option_index")
        if any(item.get("i", item.get("index")) == chosen and item.get("proceed", item.get("is_proceed")) for item in options):
            return operation
        indices = [item.get("i", item.get("index")) for item in options
                   if not item.get("locked", item.get("is_locked", False))
                   and not item.get("proceed", item.get("is_proceed", False))
                   and not item.get("revealed", False) and type(item.get("i", item.get("index"))) is int]
        if not indices:
            return operation
        screen_key = self._event_screen_key(snapshot)
        if screen_key != self._state.event_option_screen:
            self._state.event_option_screen = screen_key
            self._state.event_option_cursor = 0
            self._state.event_invalid_indices.clear()
        candidates = [index for index in indices if index not in self._state.event_invalid_indices]
        if not candidates:
            raise RuntimeError("占卜当前合法选项均已尝试且未推进；等待界面或玩家操作变化")
        index = candidates[self._state.event_option_cursor % len(candidates)]
        self._state.event_option_cursor += 1
        kwargs = dict(operation.get("kwargs")) if isinstance(operation.get("kwargs"), dict) else {}
        kwargs["option_index"] = index
        advanced = dict(operation)
        advanced["kwargs"] = kwargs
        advanced["auto_divination"] = True
        return advanced

    def _note_step_progress(self, snapshot: dict[str, Any], operation: dict[str, Any]) -> None:
        """动作发出去、mod 也回 ok，但**局面指纹没变** —— 那就是这个动作其实没生效。

        2026-10-07 实测：水晶球事件屏上插件每 2-3 秒重发一次 `choose_event_option`
        （`step_count` 83→93），而金币、事件选项、局面**一字未变**，mod 每次都回 `completed`。
        "拿不到动作"那道闸（:meth:`_note_no_operation`）拦不住这种 —— 所以要有第二道闸：
        同一个动作 + 同一个局面指纹，重复 :data:`NO_PROGRESS_REPEAT_LIMIT` 次就按"卡住"上报
        （同样**不动 autoplay 意图**：玩家没喊停就不许自己停）。
        """
        if self._waiting_for_teammate(snapshot):
            return
        action_key = f"{operation.get('action_type')}|{sorted((operation.get('kwargs') or {}).items())}"
        state_key = view_signature(self._agent_view(snapshot)) if self._agent_view(snapshot) else str(snapshot.get("state_signature") or "")
        if not state_key:
            state_key = f"{snapshot.get('screen')}|{snapshot.get('floor')}|{snapshot.get('turn')}"
        if action_key != self._state.progress_action_key or state_key != self._state.progress_signature:
            self._state.progress_action_key = action_key
            self._state.progress_signature = state_key
            self._state.progress_repeat = 1
            self._state.progress_reported = False
            return
        self._state.progress_repeat += 1
        if self._state.progress_reported or self._state.progress_repeat < self.NO_PROGRESS_REPEAT_LIMIT:
            return
        self._state.progress_reported = True
        action_type = str(operation.get("action_type") or "?")
        screen = str(snapshot.get("screen") or "unknown")
        detail = f"{screen} 上重复执行 {action_type} {self._state.progress_repeat} 次而局面没有任何变化"
        self._emit_stuck_report(
            reason=detail,
            log_line=(
                "[sts2_stuck] 动作没生效（无进展）：%s action=%s repeat=%s state=%s（autoplay 意图保持 %s）"
                % (screen, action_type, self._state.progress_repeat, state_key, self._state.autoplay_state)
            ),
            host_text=f"我卡住了：在{detail}，我推不动，你手动推一下。",
        )

    def _no_operation_signature(self, snapshot: dict[str, Any]) -> str:
        """「卡在哪」的指纹：界面 + 未识别的节点类型 + 可用动作集合。"""
        raw = snapshot.get("raw_state") if isinstance(snapshot.get("raw_state"), dict) else {}
        screen = str(snapshot.get("screen") or raw.get("screen") or "")
        unknown = str(raw.get("unknown_screen_type") or "")
        actions = raw.get("available_actions") if isinstance(raw.get("available_actions"), list) else []
        return f"{screen}|{unknown}|{view_signature(self._agent_view(snapshot))}|{','.join(sorted(str(action) for action in actions))}"

    def _waiting_for_teammate(self, snapshot: dict[str, Any]) -> bool:
        view = self._agent_view(snapshot)
        local = next((p for p in ((snapshot.get("raw_state") or {}).get("combat") or {}).get("players") or []
                      if p.get("is_local")), {})
        if view.get("screen") == "COMBAT" and local.get("ready_to_end_turn") is True:
            return True
        if view.get("screen") == "MAP" and (view.get("map") or {}).get("local_vote") is not None:
            return True
        event = view.get("event") or {}
        if (view.get("screen") == "EVENT" and event.get("is_shared") and not event.get("finished")
                and event.get("local_vote") is not None):
            return True
        raw = snapshot.get("raw_state") or {}
        combat = raw.get("combat") or {}
        actions = view.get("available_actions") or view.get("actions") or []
        names = {item if isinstance(item, str) else item.get("name") or item.get("type") for item in actions}
        return bool(snapshot.get("in_combat") or view.get("screen") == "COMBAT") and (combat.get("is_player_turn") is False
                or not names.intersection({"play_card", "end_turn", "use_potion", "select_deck_card"}))

    def _clear_stuck(self) -> None:
        self._stuck_recheck_key = ""
        if self._state.stuck_signature or self._state.stuck_reason or self._state.stuck_reported:
            self._state.stuck_signature = ""
            self._state.stuck_since = 0.0
            self._state.stuck_reported = False
            self._state.stuck_reason = ""

    def _note_no_operation(self, snapshot: dict[str, Any]) -> None:
        """拿不到可执行动作时**别静默空转**（而且**绝不改 autoplay 意图**：玩家没喊停就不许自己停）。

        2026-10-07 实测：mod 认不出的界面（水晶球事件的"占卜"子界面）只暴露 `save_and_quit`，
        循环每 0.5 秒空转一次、`step_count` 冻在 2395 整整 **2 小时 40 分**，日志里一句都没有 ——
        玩家只能靠自己发现"它不动了"。所以这里补三件事：

        1. 同一个「卡住指纹」只记一次 WARNING（带未识别界面类型名，方便精确定位到该加哪个 switch 分支）；
        2. 写进状态（`stuck_reason`，状态查询与前端看得到）；
        3. 按「尖塔速报」开关推**一次**给本体，让它开口问玩家。

        跨屏切换时短暂拿不到动作是正常的，所以要等 :data:`NO_OPERATION_STUCK_SECONDS` 才报。
        **只在真的连着游戏时报**：游戏没开时快照是空的，"没有可执行动作"是必然的，
        那种情况已经由 ``last_error`` 表达，不该再让猫娘去问玩家"我卡住了"。
        """
        raw = snapshot.get("raw_state") if isinstance(snapshot.get("raw_state"), dict) else {}
        if self._waiting_for_teammate(snapshot):
            return
        screen = str(snapshot.get("screen") or raw.get("screen") or "")
        if not screen or self._state.transport_state != "connected":
            self._clear_stuck()
            return
        signature = self._no_operation_signature(snapshot)
        now = time()
        if signature != self._state.stuck_signature:
            self._state.stuck_signature = signature
            self._state.stuck_since = now
            self._state.stuck_reported = False
            self._state.stuck_reason = ""
            return
        if self._state.stuck_reported or (now - self._state.stuck_since) < self.NO_OPERATION_STUCK_SECONDS:
            return
        self._state.stuck_reported = True
        raw = snapshot.get("raw_state") if isinstance(snapshot.get("raw_state"), dict) else {}
        screen = str(snapshot.get("screen") or raw.get("screen") or "unknown")
        unknown = str(raw.get("unknown_screen_type") or "")
        actions = raw.get("available_actions") if isinstance(raw.get("available_actions"), list) else []
        waited = int(now - self._state.stuck_since)
        detail = screen + (f"（未识别界面类型 {unknown}）" if unknown else "")
        has_actions = bool(self._agent_view(snapshot).get("actions") or self._agent_view(snapshot).get("available_actions"))
        activity = "尚未选出下一步操作" if has_actions else "界面里没有可执行动作"
        self._emit_stuck_report(
            reason=f"{detail} {activity}，已等 {waited} 秒",
            log_line=(
                "[sts2_stuck] %s：screen=%s unknown=%s actions=%s 已等 %s 秒（autoplay 意图保持 %s）"
                % (activity, screen, unknown or "-", sorted(str(action) for action in actions), waited, self._state.autoplay_state)
            ),
            host_text="当前界面仍可操作，请结合现场与玩家消息重新选择下一步；不要复述计时或系统调度提示。" if has_actions else f"我卡在{detail}这个界面上了，这里没有我能做的事，你手动推一下。",
        )

    def _emit_stuck_report(self, *, reason: str, log_line: str, host_text: str) -> None:
        """把"卡住"写到三个地方：日志一次、状态一份、本体一次（按「尖塔速报」开关）。

        两道闸（拿不到动作 / 动作没生效）共用这一份出口，避免两处各写一遍还写歪。
        """
        if self._state.stuck_reason:
            return
        self._state.stuck_signature = self._no_operation_signature(self._state.snapshot)
        self._state.stuck_reported = True
        self._state.stuck_reason = reason
        try:
            self.logger.warning(log_line)
        except Exception:
            pass
        self._emit_status()
        if not getattr(self._state, "host_digest_enabled", True):
            return
        self._push_proactive_to_host(host_text, kind="stuck_alert", priority=8)

    # ---- 游玩模式（认真/捣蛋/笨蛋）--------------------------------------------------------

    def _apply_play_style(self, snapshot: dict[str, Any], operation: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
        """按游玩模式把规划器选出的**最优**操作换成次优/最差。

        ``serious``（默认）**直接原样返回**，连候选排序都不算——这是"默认档零影响"的实现方式：
        不走任何扰动代码路径，规划器的结果逐字传出。

        非 serious 档也只是"尽量换"：候选全部来自规划器给出的合法下标集合，换完还要再通过
        ``action_engine`` 校验；任何一步不成立就放弃本次扰动、退回最优。宁可不动，不可发非法动作。
        """
        style = normalize_play_style(getattr(self._state, "play_style", PLAY_STYLE_SERIOUS))
        if style == PLAY_STYLE_SERIOUS:
            return operation, {}

        info: dict[str, Any] = {"style": style, "applied": False}
        context = {
            "snapshot": snapshot,
            "classification": snapshot.get("classification") if isinstance(snapshot.get("classification"), dict) else {},
            "summary_context": snapshot.get("summary_context") if isinstance(snapshot.get("summary_context"), dict) else {},
            "strategy_context": snapshot.get("strategy_context") if isinstance(snapshot.get("strategy_context"), dict) else {},
        }
        try:
            ordered = self._planner.ordered_candidates(context, operation)
        except Exception as exc:
            self.logger.debug(f"[sts2_play_style] candidate ranking failed (non-fatal): {exc}")
            ordered = []
        info["candidate_count"] = len(ordered)
        if len(ordered) < 2:
            # 没有合法替代（或该动作本来就不带下标）：放弃扰动。
            info["reason"] = "no_alternative"
            return operation, info

        candidate, reason = choose_candidate(ordered, style=style, roll=self._play_style_random.random())
        info["reason"] = reason
        if candidate is None or reason in {"kept_best", "serious", "no_alternative", "no_candidates"}:
            return operation, info

        try:
            perturbed = self._planner.build_perturbed_operation(context, operation, int(candidate))
        except Exception as exc:
            self.logger.debug(f"[sts2_play_style] perturbation build failed (non-fatal): {exc}")
            perturbed = None
        if perturbed is None:
            info["reason"] = "not_buildable"
            return operation, info
        if self._action_engine.validate(snapshot, perturbed) is None:
            # 换了之后动作类型都不在册了：不冒险。
            info["reason"] = "illegal"
            return operation, info

        info.update(applied=True, index=int(candidate), previous_index=self._operation_index(operation))
        self.logger.info(
            "[sts2_play_style] style=%s %s -> %s (%s)",
            style,
            info.get("previous_index"),
            candidate,
            reason,
        )
        return perturbed, info

    def _operation_index(self, operation: dict[str, Any]) -> Any:
        kwargs = operation.get("kwargs") if isinstance(operation.get("kwargs"), dict) else {}
        action_type = str(operation.get("action_type") or "")
        return kwargs.get(self._planner.index_key_for(action_type))

    def set_play_style(self, style: str) -> dict[str, Any]:
        self._state.control_generation += 1
        self._invalidate_companion_plan()
        """切换游玩模式：认真（默认）/ 捣蛋 / 笨蛋。"""
        normalized = normalize_play_style(style)
        self._state.play_style = normalized
        self._cfg["play_style"] = normalized
        self._emit_status()
        message = {
            "serious": "好，我认真打。",
            "mischievous": "好，那我就偶尔闹一下，别怪我。",
            "silly": "好，我摆烂了，你多担待。",
        }[normalized]
        return {"status": "ok", "message": message, "summary": message, "play_style": normalized}


    async def execute_operation(self, operation: dict[str, Any] | None) -> dict[str, Any]:
        if not self._action_dispatch_lock.acquire(blocking=False):
            return {"status": "busy", "message": "已有动作正在完成，未发送新的动作。", "summary": "动作正在完成。", "executed": False}
        try:
            if self._state.discussion_active or self._state.standby or self._state.autoplay_state == "paused":
                return {"status": "idle", "message": "暂停期间不执行动作，仍可聊天。", "executed": False}
            if self._pending_action is not None:
                return {"status": "pending", "executed": False, "message": "上一动作尚未确认完成，不重复派发。"}
            if operation and operation.get("source") == "llm":
                await self.refresh_state(plan_combat=False)
                # 模型判断期间可能有新发言；派发前重新导入，不按旧要求继续出牌。
                await self._read_companion_dialogue()
                if self._ingress_enabled and not self._ingress_active:
                    return {"status": "stale", "executed": False, "message": "统一会话所有权已失效，停止旧实例动作。"}
                resolved, invalid = self._resolve_dispatch(operation)
                if (invalid
                        or self._companion_session.generation != operation.get("session_generation")
                        or self._companion_session.dialogue_revision != operation.get("dialogue_revision", self._companion_session.dialogue_revision)
                        or self._state.control_mode != operation.get("control_mode")
                        or self._state.control_generation != operation.get("control_generation", self._state.control_generation)
                        or self._state.guidance_generation != operation.get("guidance_generation", self._state.guidance_generation)):
                    return {"status": "stale", "executed": False, "message": invalid or "对话或控制已变化，重新判断。"}
                operation.update(resolved)
                active = self._companion_session.active_plan
                if operation.get("plan_revision") is not None and (
                        operation["plan_revision"] != active.get("revision") or active.get("status") != "selected"):
                    return {"status": "stale", "executed": False, "message": "该版本计划已失效，重新判断。"}
                if self._combat_window_closed(operation):
                    return {"status": "waiting_for_actions", "executed": False, "message": "等待战斗动作窗口恢复。"}
                if self._state.discussion_active or self._state.standby or self._state.autoplay_state == "paused":
                    return {"status": "idle", "executed": False, "message": "已暂停动作。"}
                anchor = self._companion_session.anchor()
                if self._wait_for_turn_message and anchor.get("battle_id") and self._current_turn_message() is None:
                    return {"status": "waiting_for_message", "executed": False, "message": "新回合尚未收到你的话。"}
                if operation.get("action_type") == "use_potion":
                    # 药水被游戏侧取消后允许直接重发，避免同一局面永久拦死。
                    self._unconfirmed_action = None
                elif self._unconfirmed_action == (view_signature(self._agent_view(self._state.snapshot)),
                                                 operation["action_type"], public_kwargs(operation.get("kwargs"))):
                    return {"status": "unconfirmed", "executed": False, "message": "同一局面下该动作的上次结果未确认，不重复派发。"}
                if operation.get("plan_revision") is None:
                    self._companion_session.accept_plan(operation, self._agent_view(self._state.snapshot))
                kind = self._companion_session.speech_kind()
                speech = operation.get("speech") or ""
                if operation["action_type"] == "end_turn":
                    danger = end_turn_danger((self._state.snapshot.get("raw_state") or {}).get("combat") or {})
                    if danger and danger not in (operation.get("reply") or ""):
                        speech = danger
                        operation.update(speech=speech, speech_scope="intent", speech_step_ids=[0])
                if operation["action_type"] == "use_potion":
                    potion = next(p for p in (self._state.snapshot["raw_state"].get("run") or {}).get("potions") or []
                                  if p["index"] == operation["kwargs"]["option_index"])
                    announced = self._companion_session.announced_plan
                    already_said = (potion["name"] in announced.get("text", "") and any(
                        step["action_type"] == "use_potion" and step["kwargs"] == public_kwargs(operation["kwargs"])
                        for step in announced.get("remaining_steps", [])))
                    if not already_said or speech:
                        kind = "potion"
                        if not speech:
                            combat = self._state.snapshot["raw_state"].get("combat") or {}
                            target_index = operation["kwargs"].get("target_index")
                            if potion.get("target_index_space") == "players":
                                target = combat["players"][target_index]
                                speech = f"我用这瓶{potion['name']}。" if target["is_local"] else f"这瓶{potion['name']}给你用。"
                            elif potion.get("target_index_space") == "enemies":
                                speech = f"这瓶{potion['name']}用在{combat['enemies'][target_index]['name']}身上。"
                            else:
                                speech = f"我用这瓶{potion['name']}。"
                            operation.update(speech=speech, speech_scope="steps", speech_step_ids=[0])
                    else:
                        speech = ""
                    self.logger.info("[sts2_potion_speech] potion=%s target_space=%s target_index=%s already_said=%s text=%s",
                                     potion["name"], potion.get("target_index_space"), operation["kwargs"].get("target_index"), already_said, speech)
                if kind == "correction" and not speech:
                    step = self._selected_steps[0]["description"]
                    label = step["card"] or {"end_turn": "结束回合", "use_potion": "使用药水", "discard_potion": "丢弃药水",
                                              "select_deck_card": "重新选牌", "confirm_selection": "确认选牌"}.get(step["action_type"], "调整下一步")
                    speech = f"改一下，我先{label.split('：', 1)[0][:70]}。"
                    operation.update(speech=speech, speech_scope="steps", speech_step_ids=[0])
                explicit_reply = bool(operation.get("input_ids"))
                speech = public_companion_text(speech)
                allowed = explicit_reply or ((self._ingress_active or not self._companion_session.awaiting_chat_reply())
                                             and (self._state.host_digest_enabled or self._danmaku_enabled))
                if speech and allowed:
                    speech_started = perf_counter()
                    self._speech_state = {**anchor, "plan_revision": operation["plan_revision"], "kind": kind,
                                          "text": speech, "state": "speaking", "started_at": time()}
                    await self._sync_execution_status()
                    self.logger.info("[sts2_turn_speech] start anchor=%s revision=%s kind=%s text=%s",
                                     anchor, operation["plan_revision"], kind, speech)
                    await self._push_catgirl(speech, fetch_avatar=False)
                    wait_audio = self._agent_view(self._state.snapshot).get("screen") == "COMBAT" and operation["action_type"] != "end_turn"
                    if self._state.host_digest_enabled or self._ingress_active and explicit_reply:
                        if self._ingress_active:
                            try:
                                spoken = await self._publish_companion_reply(speech,
                                    [{"request_id": i} for i in operation.get("input_ids") or []], wait_audio=wait_audio,
                                    media_requests=operation.get("media_requests") or [],
                                    output_id=f"speech:{anchor.get('run_id')}:{operation['step_id']}",
                                    speech_kind="tactical",
                                    anchor={**anchor, "plan_revision": operation["plan_revision"], "kind": kind})
                            except CompanionAudioPending as exc:
                                self._speech_state.update(state="audio_failed", receipt=exc.receipt)
                                self._step_timing["speech_ms"] = (perf_counter() - speech_started) * 1000
                                self._selected_steps[0].update(speech=speech, speech_scope=operation.get("speech_scope"),
                                    speech_step_ids=operation.get("speech_step_ids"), input_ids=operation.get("input_ids", []))
                                self._state.retry_at = time() + self._autoplay_retry_backoff()
                                return {"status": "audio_timeout" if exc.receipt.get("reason") == "audio_completion_timeout" else "audio_failed",
                                        "executed": False, "message": str(exc), "receipt": exc.receipt}
                        else:
                            spoken = await speak_current_turn(self._main_server_port(), self._catgirl_llm.character_name(),
                                                              speech, {**anchor, "plan_revision": operation["plan_revision"], "kind": kind, "speech_kind": "tactical"})
                        self._speech_state["speech_id"] = spoken.get("speech_id")
                    self._speech_state.update(state="completed" if wait_audio else "queued",
                        **({"completed_at": time()} if wait_audio else {"queued_at": time()}))
                    if hasattr(self, "_step_timing"):
                        self._step_timing["speech_ms"] = (perf_counter() - speech_started) * 1000
                    self._companion_session.announce_plan(operation, speech, kind)
                    if not self._ingress_active:
                        self._companion_session.remember("assistant", speech)
                    self._communicated_turn = anchor
                    self.logger.info("[sts2_turn_speech] completed anchor=%s speech_id=%s revision=%s kind=%s",
                                     anchor, self._speech_state.get("speech_id"), operation["plan_revision"], kind)
                    # 发言过程中可以暂停、收到新要求或进入另一回合；任何一种都不得派发旧动作。
                    await self.refresh_state(plan_combat=False)
                    await self._read_companion_dialogue()
                    if self._ingress_enabled and not self._ingress_active:
                        return {"status": "stale", "executed": False, "message": "发言后统一会话所有权已失效。"}
                    resolved, invalid = self._resolve_dispatch(operation)
                    if (invalid
                            or self._companion_session.generation != operation.get("session_generation")
                            or self._companion_session.dialogue_revision != operation.get("dialogue_revision", self._companion_session.dialogue_revision)
                            or self._state.control_generation != operation.get("control_generation")
                            or self._state.guidance_generation != operation.get("guidance_generation")):
                        return {"status": "stale", "executed": False, "message": invalid or "发言后要求已变化，重新判断。"}
                    operation.update(resolved)
                    if self._combat_window_closed(operation):
                        return {"status": "waiting_for_actions", "executed": False, "message": "发言已完成，保留计划等待战斗动作窗口恢复。"}
                else:
                    operation["speech"] = ""
                # C03：reply 与 speech 使用同一套等待规则；只有 question 才阻塞动作。
                window = self._ask_window_seconds(operation)
                if window > 0:
                    answered, _timed_out = await self._wait_for_answer(operation, window=window)
                    if answered:
                        return {"status": "waiting_for_input", "executed": False,
                                "message": "已问主人，收到回答后重新判断。"}
                    if operation.get("answer_required"):
                        return {"status": "waiting_for_answer", "executed": False,
                                "message": "已提问，等待主人回答。"}
                if self._state.discussion_active or self._state.standby or self._state.autoplay_state == "paused":
                    return {"status": "idle", "executed": False, "message": "已暂停动作。"}
                if self._agent_view(self._state.snapshot).get("screen") == "COMBAT":
                    self._communicated_turn = anchor
            snapshot = self._state.snapshot
            raw = snapshot.get("raw_state") or {}
            multiplayer = raw.get("multiplayer") or {}
            if raw.get("in_combat") and multiplayer.get("is_multiplayer") and str(multiplayer.get("net_game_type") or "").lower() == "host":
                return {"status": "idle", "message": "这是真人主机的战斗界面，未代替你出牌。", "summary": "需要猫娘实例。", "executed": False}
            if operation and operation.get("source") == "llm" and raw.get("in_combat"):
                operation = dict(operation)
                kwargs = dict(operation.get("kwargs") or {})
                combat = raw.get("combat") or {}
                player = next((item for item in combat.get("players") or [] if item.get("is_local")), None)
                player_id = (player or combat.get("player") or {}).get("player_id")
                if raw.get("turn") is None or player_id is None:
                    return {"status": "unconfirmed", "executed": False, "message": "无法确认自己的战斗回合和玩家标识。"}
                kwargs["expected_turn"] = raw["turn"]
                kwargs["expected_player_id"] = str(player_id)
                if operation.get("action_type") == "play_card":
                    card = next((item for item in combat.get("hand") or [] if item.get("index") == kwargs.get("card_index")), None)
                    if not card or not card.get("entity_id"):
                        return {"status": "unconfirmed", "executed": False, "message": "无法确认待出牌的稳定标识。"}
                    kwargs["expected_card_entity_id"] = card["entity_id"]
                    if card.get("target_index_space") == "enemies" or card.get("target_type") == "AnyEnemy":
                        enemy = next((item for i, item in enumerate(combat.get("enemies") or [])
                                      if item.get("index", i) == kwargs.get("target_index")), None)
                        if not enemy or not enemy.get("entity_id"):
                            return {"status": "unconfirmed", "executed": False, "message": "无法确认目标敌人的稳定标识。"}
                        kwargs["expected_enemy_entity_id"] = enemy["entity_id"]
                operation["kwargs"] = kwargs
            return await self._execute_operation(operation)
        finally:
            if "speech_started" in locals() and self._speech_state.get("state") == "speaking":
                self._step_timing["speech_ms"] = (perf_counter() - speech_started) * 1000
                self._speech_state.update(state="failed", failed_at=time())
            self._action_dispatch_lock.release()

    def _ask_window_seconds(self, operation: dict[str, Any]) -> float:
        """本轮真的要等主人回答时才返回等待窗口；感叹/玩笑/反问不阻塞动作。"""
        if not isinstance(operation, dict):
            return 0.0
        kind = str(operation.get("interaction_kind") or ("question" if operation.get("asked") else ""))
        if kind != "question":
            return 0.0
        screen = self._agent_view(self._state.snapshot).get("screen")
        return self._ask_window_combat if screen == "COMBAT" else self._ask_window_page

    async def _wait_for_answer(self, operation: dict[str, Any], *, window: float) -> tuple[bool, bool]:
        """等待主人回答本轮提问，返回 (answered, timed_out)。

        问题记进会话：新问题替代旧问题，回答按 request_id 关联原问题；
        answer_required=true 时超时不自动解除等待（用户明确要求“等我回答”的情况）。
        """
        session = self._companion_session
        question = session.pending_question
        if question.get("status") == "answered":
            # 上一问的回答已经处理过：只有本次操作问的仍是同一个问题才直接复用。
            # 否则清掉旧记录，为本次新问题重新登记；旧实现无条件返回 True，
            # 新问题没登记就继续决策（2026-10-20 00:34:19 现场：提问后 5 秒自己行动）。
            if operation.get("question") and question.get("text") == operation.get("question"):
                session.pending_question = {}
                return True, False
            session.pending_question = {}
        if (question.get("status") != "open" or not question.get("text")
                or (operation.get("question") and question.get("text") != operation.get("question"))):
            question = session.ask_question(
                operation.get("question") or "", required=bool(operation.get("answer_required")),
                window=window, operation=operation)
            if not question:
                return False, True
        deadline = time() + window
        while time() < deadline:
            await asyncio.sleep(0.5)
            await self._read_companion_dialogue()
            messages = session.pending_messages()
            if messages:
                session.answer_pending_question(messages)
                self.logger.info("[sts2_ask_window] answered id=%s by=%s",
                                 question.get("id"), [m.get("id") for m in messages])
                return True, False
        if operation.get("answer_required"):
            self.logger.info("[sts2_ask_window] still waiting required answer id=%s", question.get("id"))
        else:
            session.expire_pending_question()
            self.logger.info("[sts2_ask_window] timeout window=%.0fs，按自己倾向继续", window)
        return False, True

    async def _execute_operation(self, operation: dict[str, Any] | None) -> dict[str, Any]:
        snapshot = self._state.snapshot if isinstance(self._state.snapshot, dict) else {}
        blocker = self._companion_session.requirement_blocker(operation, snapshot) if operation else None
        if blocker:
            self._invalidate_companion_plan(blocker["reason"])
            return {"status": "waiting_for_condition", "executed": False, "message": blocker["reason"],
                    "requirement_id": blocker.get("requirement_id")}
        question = self._companion_session.pending_question
        if (operation and question.get("status") == "open" and question.get("required")
                and operation.get("action_type")
                and operation.get("action_type") == question.get("action_type")):
            # C03：明确要求等回答时，回答前不执行依赖该问题的动作。
            return {"status": "waiting_for_answer", "executed": False,
                    "message": "已提问，等待主人回答后再执行该动作。"}
        before_view = self._agent_view(snapshot)
        if operation:
            self._companion_session.bind_requirement_completions(operation)
            operation["_execution_effects"] = build_agent_facts(snapshot.get("raw_state"))
        action_anchor = self._companion_session.anchor()
        before_summary = self._summary_builder.build_snapshot_summary(snapshot) if snapshot else {}
        client = self._companion_client()
        dispatch_started = perf_counter()
        if isinstance(operation, dict):
            operation["_action_id"] = uuid4().hex
        result = await self._action_engine.execute(client, snapshot, operation)
        accepted_at = time()
        if hasattr(self, "_step_timing"):
            self._step_timing["dispatch_ms"] = (perf_counter() - dispatch_started) * 1000
        result_status = str(result.get("status") or "")
        if result_status in {"canceled", "rejected", "unknown"}:
            # G02：Mod 明确给出非完成结果时不再当成 pending 死等；记录真实结果并按新现场重判。
            self._remember_plugin_action_marker(operation, snapshot)
            self._companion_session.record_action(operation, anchor=action_anchor, before=before_view,
                after=before_view, result=result)
            self._invalidate_companion_plan(f"动作结果：{result_status}")
            response = result.get("result") if isinstance(result.get("result"), dict) else {}
            self.logger.info("[sts2_action_result] status=%s action=%s action_id=%s reason=%s",
                             result_status, operation.get("action_type"), result.get("action_id"),
                             str(response.get("reason") or "")[:200])
            return result
        if result.get("status") == "pending":
            # G01：Mod 已接收动作，先登记待确认状态再做任何网络等待；刷新失败只丢观察，不丢账。
            self._remember_plugin_action_marker(operation, snapshot)
            refreshed = False
            try:
                await self.refresh_state(plan_combat=False)
                refreshed = True
            except Exception as exc:
                self.logger.warning("[sts2_pending_refresh_failed] 动作已接收，保留待确认记录：%s", exc)
            after_view = self._agent_view(self._state.snapshot) if refreshed else before_view
            self._companion_session.record_action(operation, anchor=action_anchor, before=before_view,
                after=after_view, result=result)
            self._pending_action = {"operation": deepcopy(operation), "anchor": action_anchor,
                                    "before": deepcopy(snapshot), "record": self._companion_session.actions[-1],
                                    "submitted_at": time(), "accepted_at": accepted_at,
                                    "action_id": str(result.get("action_id") or operation.get("_action_id") or ""),
                                    "response": dict(result)}
            if refreshed:
                self._resolve_pending_action(self._state.snapshot)
            self._deliver_host_context(self._state.snapshot)
            return result
        if result.get("status") == "ok":
            self._state.touch_action()
            self._companion_session.facts["last_action"] = {
                "action_type": operation.get("action_type"), "reason": operation.get("reason"),
            }
            op = result.get("operation") if isinstance(result.get("operation"), dict) else {}
            if op.get("action_type") == "select_deck_card":
                # 选牌光标由"这一下真的按下去了"推动。plan() 是每 tick 都跑的（事件流约 1 次/秒），
                # 若让它自己推进光标，四个候选会在几秒内被规划耗光、屏幕却一张都没选中，
                # 随后永久返回 None —— 猫娘就永久停在这一屏（玩家看到的"卡住"）。
                kwargs = op.get("kwargs") if isinstance(op.get("kwargs"), dict) else {}
                self._planner.note_selection_option_pressed(kwargs.get("option_index"))
            if op.get("action_type") == "claim_reward":
                # 同理：领奖励的记账也必须由执行推动。药水栏满时"领药水"发不出去而 mod 照旧回
                # completed，靠这张账才能跳过那一条而不是无限重领同一个下标。
                kwargs = op.get("kwargs") if isinstance(op.get("kwargs"), dict) else {}
                self._planner.note_reward_claim_dispatched(kwargs.get("option_index"))
            if op.get("action_type") == "select_character":
                # 选人界面的「随机」同样只按一次：按过就转入 embark，避免"选中后再选一次"的死循环。
                kwargs = op.get("kwargs") if isinstance(op.get("kwargs"), dict) else {}
                self._planner.note_character_select_dispatched(kwargs.get("option_index"))
                self._companion_session.character_choice_made = True
                self._companion_session.generation += 1
            if op.get("action_type") == "use_potion":
                # 战斗用药水的记账："同一回合只喝一瓶"（规格「八、节流」）。只在**真的喝下去之后**记，
                # 与选牌光标/奖励领取同一个道理：规划每 tick 都跑，用规划记账会把机会白白耗光。
                self._state.potion_used_turn_key = self._combat_turn_key(snapshot)
            # 战斗整回合缓存：仅战斗步(出牌/用药/结束回合)推进 line[0].steps 索引；选牌子动作不推进。
            if op.get("action_type") in {"play_card", "use_potion", "end_turn"}:
                self._state.solver_step_index = (self._state.solver_step_index if isinstance(self._state.solver_step_index, int) else 0) + 1
            self._state.last_decision_source = str(op.get("source") or "")
            self._state.last_decision_reason = str(op.get("reason") or "")
            self._consume_guidance(op)
            self._remember_plugin_action_marker(op, snapshot)
            action_seed = {
                "action_type": str(op.get("action_type") or ""),
                # 剥离执行期专用键（expected_turn 等）：latest_action_frame 会经行动反馈
                # 上下文喂给宿主 LLM，带上这些内部键会被复述回来。
                "action_kwargs": public_kwargs(op.get("kwargs")),
                "decision_source": self._state.last_decision_source,
                "decision_reason": self._state.last_decision_reason,
                "created_at": time(),
            }
            self._state.latest_action_frame = dict(action_seed)
            self._companion_session.complete_step(operation)
            await self.refresh_state(trigger_sync=True)
            after_snapshot = self._state.snapshot if isinstance(self._state.snapshot, dict) else {}
            after_summary = self._summary_builder.build_snapshot_summary(after_snapshot) if after_snapshot else {}
            action_delta = self._summary_engine.compute_delta(before_summary, after_summary, source="action_paired")
            after_view = self._agent_view(after_snapshot)
            self._companion_session.facts["last_outcome"] = {
                "screen": after_view.get("screen"), "turn": after_view.get("turn"),
                "hp": (after_view.get("run") or {}).get("hp"),
                "player": (after_view.get("combat") or {}).get("player"),
            }
            self._companion_session.record_action(operation, anchor=action_anchor, before=before_view,
                                                  after=after_view, result=result)
            action_frame = {
                "before": dict(before_summary),
                "after": dict(after_summary),
                "delta": dict(action_delta),
                **action_seed,
                "step_count": self._state.step_count + 1,
            }
            self._state.remember_action_frame(action_frame)
            self._state.remember_decision_memory(
                {
                    "screen": str(after_snapshot.get("screen") or before_summary.get("screen") or "unknown"),
                    "action_type": action_frame["action_type"],
                    "decision_reason": action_frame["decision_reason"],
                    "decision_source": action_frame["decision_source"],
                    "delta": dict(action_delta),
                    "step_count": action_frame["step_count"],
                    **action_anchor,
                    "created_at": action_seed["created_at"],
                    "current_hp": (after_summary.get("player") or {}).get("current_hp"),
                    "max_hp": (after_summary.get("player") or {}).get("max_hp"),
                }
            )
            summary_context = self._summary_builder.build(after_snapshot, runtime_state=self._state)
            self._state.snapshot["summary_context"] = summary_context
            self._state.snapshot["situation_summary"] = self._summary_engine.summarize(summary_context)
        return result

    def _resolve_pending_action(self, snapshot: dict[str, Any]) -> dict[str, Any]:
        pending = self._pending_action
        operation = pending["operation"]
        before = pending["before"]
        raw = snapshot.get("raw_state") or {}
        old_raw = before.get("raw_state") or {}
        view = self._agent_view(snapshot)
        kwargs = operation.get("kwargs") or {}
        action_type = operation["action_type"]
        record = pending["record"]
        if view.get("run_id") != pending["anchor"].get("run_id"):
            self._pending_action = None
            return {"status": "cancelled", "executed": False, "message": "已进入另一局，停止等待旧动作。"}
        combat = raw.get("combat") or {}
        old_combat = old_raw.get("combat") or {}
        completed = False
        if action_type == "play_card":
            entity = kwargs.get("expected_card_entity_id")
            completed = bool(entity and raw.get("in_combat") and isinstance(combat.get("hand"), list)
                             and all(c.get("entity_id") != entity for c in combat["hand"]))
            played = (combat.get("player") or {}).get("cards_played_this_turn")
            old_played = (old_combat.get("player") or {}).get("cards_played_this_turn")
            completed = completed or (raw.get("turn") == old_raw.get("turn")
                and type(played) is int and type(old_played) is int and played > old_played)
        elif action_type == "end_turn":
            local = next((p for p in combat.get("players") or [] if p.get("is_local")), {})
            completed = (raw.get("in_combat") is False
                         or (type(raw.get("turn")) is int and raw["turn"] != old_raw.get("turn"))
                         or local.get("is_action_phase") is False or local.get("ready_to_end_turn") is True)
        elif action_type in {"use_potion", "discard_potion"}:
            old_potions = (old_raw.get("run") or {}).get("potions")
            potions = (raw.get("run") or {}).get("potions")
            completed = old_potions is not None and potions is not None and old_potions != potions
        elif action_type == "choose_event_option":
            old_event = old_raw.get("event") or self._agent_view(before).get("event")
            event = raw.get("event") or view.get("event")
            same_page = bool(old_event and event
                and (old_event.get("event_id") or old_event.get("id")) == (event.get("event_id") or event.get("id"))
                and old_event.get("page_index") == event.get("page_index"))
            vote_registered = bool(same_page and event.get("is_shared")
                and event.get("local_vote") == kwargs.get("option_index"))
            event_changed = bool(old_event and event and
                {k: v for k, v in event.items() if k not in {"local_vote", "player_votes"}} !=
                {k: v for k, v in old_event.items() if k not in {"local_vote", "player_votes"}})
            completed = bool(old_event and (view.get("screen") != "EVENT" or vote_registered or event_changed))
        elif action_type in {"select_deck_card", "confirm_selection"}:
            old_selection = old_raw.get("selection") or {}
            selection = raw.get("selection") or {}
            old_card = next((c for c in old_selection.get("cards", []) if c["index"] == kwargs.get("option_index")), {})
            card = next((c for c in selection.get("cards", []) if old_card.get("entity_id")
                         and c.get("entity_id") == old_card["entity_id"]), {})
            selected = action_type == "select_deck_card" and not old_card.get("selected") and card.get("selected") is True
            deck_changed = (old_raw.get("run") or {}).get("deck") is not None and (raw.get("run") or {}).get("deck") is not None and old_raw["run"]["deck"] != raw["run"]["deck"]
            hand_changed = old_combat.get("hand") is not None and combat.get("hand") is not None and old_combat["hand"] != combat["hand"]
            submitted = bool(old_selection and not selection and (deck_changed or hand_changed))
            completed = bool(selected or submitted)
        changed = view != self._agent_view(before)
        waiting_in_combat = (action_type in {"play_card", "end_turn", "use_potion", "discard_potion"}
                             and raw.get("in_combat") and raw.get("turn") == old_raw.get("turn"))
        selection_action = action_type in {"select_deck_card", "confirm_selection"}
        if not completed and (not changed or waiting_in_combat or selection_action or action_type == "choose_event_option") and time() - pending["submitted_at"] < 10:
            return {"status": "pending", "executed": False, "message": "动作仍在等待确认，不重复派发。"}
        # 局面变化不足以证明任意动作已完成；没有上述动作证据时保留未知结果并重判。
        status = "completed" if completed else "unconfirmed"
        message = "现场确认动作完成。" if completed else "原动作结果未确认，重新判断并选择其他操作。"
        record.update(status=status, executed=completed, changed=changed,
                      message=message,
                      outcome={"screen": view.get("screen"), "turn": view.get("turn"),
                               "player": deepcopy((view.get("combat") or {}).get("player")),
                               **({"selection": deepcopy(raw.get("selection")), "selection_submitted": submitted} if selection_action else {}),
                               "hp": (view.get("run") or {}).get("hp")})
        if action_type == "choose_event_option":
            record["outcome"]["event"] = deepcopy(event)
            record["outcome"]["event_vote_registered"] = vote_registered
        self._companion_session.action_sequence += 1
        self._companion_session.journal.confirm_action(record, view)
        self._record_action_learning(record)
        accepted_at = pending.get("accepted_at", pending.get("submitted_at"))
        if accepted_at is not None:
            self._companion_session.journal.latency({"timestamp": time(), "configuration": self._latency_configuration(),
                "path": "confirmation", "outcome": status,
                "measures": {"confirmation_wait_ms": (time() - accepted_at) * 1000},
                "model_calls": 0, "solver_calls": 0})
        self._pending_action = None
        if completed:
            self._companion_session.complete_step(operation)
            self._companion_session.confirm_requirement_action(record)
            self._companion_session.refresh_requirements(snapshot)
            self._state.step_count += 1
            self._last_operation = operation
            self._consume_guidance(operation)
            self._note_decision_success()
        else:
            self._unconfirmed_action = self._action_confirmation_key(snapshot, action_type, kwargs)
            self._invalidate_companion_plan("动作已派发，但未观察到明确完成结果")
        self.logger.info("[sts2_action_confirmation] revision=%s step=%s status=%s",
                         operation.get("plan_revision"), operation.get("step_id"), status)
        return {"status": "ok" if completed else "unconfirmed", "executed": completed,
                "message": message}

    def _action_confirmation_key(self, snapshot: dict, action_type: str, kwargs: dict) -> tuple:
        view = self._agent_view(snapshot)
        if action_type in {"select_deck_card", "confirm_selection"}:
            raw = snapshot.get("raw_state") or {}
            selection = raw.get("selection") or {}
            evidence = {k: selection.get(k) for k in ("selection_id", "kind", "min_select", "max_select", "selected_count", "can_confirm")}
            evidence["cards"] = [{k: c.get(k) for k in ("index", "entity_id", "card_id", "selected")} for c in selection.get("cards", [])]
            evidence.update(run_id=view.get("run_id"), screen=view.get("screen"), floor=(view.get("run") or {}).get("floor"))
            return view_signature(evidence), action_type, public_kwargs(kwargs)
        return view_signature(view), action_type, public_kwargs(kwargs)

    def _review_journal(self, review: dict) -> CompanionJournal:
        current = self._companion_session.journal
        if review.get("kind") != "run_feedback":
            return current
        pending = self._catgirl_memory.pending_review()
        if not pending or review.get("run_id") != pending["run_id"]:
            raise ValueError("复盘反馈必须关联当前待讨论的局")
        if current.data.get("run_id") == pending["run_id"]:
            return current
        journal = CompanionJournal(current.directory, self.logger)
        journal.activate(pending["run_id"])
        return journal

    def _validate_companion_action(self, snapshot: dict[str, Any], operation: Any) -> tuple[Any, str]:
        planned = operation.as_dict() if hasattr(operation, "as_dict") else operation
        session = self._companion_session
        if planned.get("learning_updates") or planned.get("experience_uses") or planned.get("review"):
            try:
                self._catgirl_memory.validate_learning(planned.get("learning_updates", []), planned.get("experience_uses", []),
                    planned.get("review", {}), messages=session.pending_messages(), journal=self._review_journal(planned.get("review", {})),
                    available=self._learning_available,
                    terminal=self._agent_view(snapshot).get("screen") in {"GAME_OVER", "VICTORY", "DEFEAT"},
                    step_count=1 + len(planned.get("continuation") or []) if planned.get("action_type") else 0)
            except ValueError as exc:
                return None, str(exc)
        if planned.get("requirement_updates"):
            session = copy(session)
            session.requirements = deepcopy(session.requirements)
            session.apply_requirement_updates(planned["requirement_updates"], snapshot, planned)
        if not planned.get("action_type"):
            if (self._state.autoplay_state == "running" and self._state.control_mode == "program"
                    and not self._state.discussion_active and planned.get("control") not in {"pause", "stop"}
                    and not self._selected_steps and not session.pending_messages()
                    and self._map_vote_candidates(snapshot, session)):
                return None, "自己的地图票尚未投出，当前有合法选项；队友的票不等于自己的票，请choose_map_node选择路线，不能空等投票生效。"
            return operation, ""
        if planned["action_type"] == "select_deck_card":
            cards = ((snapshot.get("raw_state") or {}).get("selection") or {}).get("cards", [])
            if any(c.get("selected") for c in cards if c["index"] == planned["kwargs"].get("option_index")):
                return None, "这张牌已经选中；继续选择其余未选牌，或选够后confirm_selection，勿再次点击反选。"
        blocker = session.requirement_blocker(planned, snapshot)
        if blocker:
            return None, blocker["reason"] + "；请选择不受限制的合法动作，或action_type=null、plan_update=wait并回复当前等待原因。"
        if planned["action_type"] == "claim_reward":
            raw = snapshot.get("raw_state") or {}
            reward = next((r for r in (raw.get("reward") or {}).get("rewards") or []
                           if r["index"] == planned["kwargs"].get("option_index")), {})
            potions = (raw.get("run") or {}).get("potions") or []
            if reward.get("reward_type") == "Potion" and potions and all(p.get("occupied") for p in potions):
                return None, "药水栏已满，不能直接领取；比较新旧药水，先discard_potion再领取，或resolve_rewards离开。"
        if (planned.get("action_type") != "use_potion"
                and self._unconfirmed_action == self._action_confirmation_key(snapshot, planned["action_type"], planned.get("kwargs") or {})):
            return None, "该动作已派发但结果未确认，局面尚未改变；请选择其他操作或等待。"
        return self._action_engine.validate_with_feedback(snapshot, operation)

    def _requirement_wait(self) -> list[dict[str, Any]]:
        return [item for item in self._companion_session.active_requirements() if item.get("status") == "waiting"]

    def execution_status(self) -> dict[str, Any]:
        snapshot = self._state.snapshot or {}
        view = self._agent_view(snapshot)
        state, text, detail = "running", "正在行动", ""
        waiting = self._requirement_wait()
        if self._state.autoplay_state == "paused":
            state, text = "paused", "已暂停"
            detail = (self._state.decision_error or self._state.last_error) if self._state.pause_reason == "decision_failure" else "按你的要求暂停，仍可聊天"
        elif self._state.standby or self._state.control_mode != "program":
            state, text, detail = "standby", "自主行动已关闭", "仍可聊天与查看当前要求"
        elif self._state.autoplay_state != "running" and self._state.stop_reason:
            state, text, detail = "stopped", "已停止", "等待你恢复自主行动"
        elif self._state.transport_state != "connected":
            state, text, detail = "disconnected", "等待游戏连接", self._state.last_error or "正在恢复局面观测"
        elif self._state.input_warning:
            state, text, detail = "disconnected", "同伴会话连接异常", self._state.input_warning
        elif self._ingress_enabled and not self._ingress_active:
            state, text, detail = "restoring", "正在恢复会话", "读取有效要求与运行状态"
        elif self._state.decision_error and time() < self._state.retry_at:
            state, text, detail = "error", "行动遇到问题", self._state.decision_error
        elif self._speech_state.get("state") == "speaking" and self._decision_lock.locked():
            state, text, detail = "speaking", "正在说明打法", self._speech_state.get("text", "")
        elif self._execution_phase:
            state, text, detail = (self._execution_phase[k] for k in ("state", "text", "detail"))
        elif self._pending_action:
            state, text, detail = "confirming", "等待动作完成", "已派发，正在确认实际结果"
        elif view.get("screen") in {"MAIN_MENU", "CHARACTER_SELECT", "MULTIPLAYER_LOBBY"}:
            state, text, detail = "waiting_for_game", "等待开局", "请选择角色并确认开始"
        elif self._waiting_for_teammate(snapshot):
            state, text, detail = "waiting_for_teammate", "等待队友", "等待你结束回合或完成当前界面"
            if view.get("screen") == "EVENT":
                detail = "共享事件投票已确认，等待队友投票或事件结算"
        elif waiting:
            state, text = "waiting_for_condition", "等待配合"
            detail = "；".join(item.get("wait_reason") or item.get("text", "") for item in waiting)
            missing = [check["missing"] for item in waiting for check in (item.get("evidence") or {}).get("checks", [])
                       if check.get("missing")]
            if missing:
                state, text, detail = "observation_missing", "协作条件缺少现场信息", "等待恢复观测：" + "、".join(dict.fromkeys(missing))
        elif self._last_step_result.get("status") in {"waiting_for_message", "waiting_for_actions", "waiting_for_condition", "waiting_for_data"}:
            state = self._last_step_result["status"]
            text = {"waiting_for_message": "等待你的回合要求", "waiting_for_actions": "等待界面就绪",
                    "waiting_for_condition": "等待配合", "waiting_for_data": "等待事件信息"}[state]
            detail = self._last_step_result.get("message", "")
        elif self._state.autoplay_state != "running":
            state, text = "idle", "等待开始"
        elif self._last_step_result.get("status") == "idle" and self._last_step_result.get("message"):
            state, text, detail = "waiting", "等待下一步", self._last_step_result["message"]
        return {"state": state, "text": text, "detail": detail[:500],
                "session_id": f"{self._ingress_instance}:{self._companion_session.epoch}",
                "requirement_ids": [item["id"] for item in waiting],
                "requirements_revision": self._companion_session.requirements_revision}

    async def _sync_execution_status(self) -> None:
        client = self._host_client
        if client is None or not self._status_push_lock.acquire(blocking=False):
            return
        try:
            status = self.execution_status()
            key = json.dumps(status, ensure_ascii=False, sort_keys=True)
            if key == self._status_push_key and time() - self._status_push_at < 3:
                return
            self._status_revision = max(time_ns(), self._status_revision + 1)
            payload = {**status, "revision": self._status_revision, "ttl_seconds": 15}
            await client.set_companion_status(payload)
            self._status_push_key, self._status_push_at = key, time()
        except STS2TransportError as exc:
            # 展示失败不重发游戏动作；下一次状态心跳重试。
            if time() - self._status_push_at >= 3:
                self.logger.debug("[sts2_status_push] %s", exc)
                self._status_push_at = time()
        finally:
            self._status_push_lock.release()

    async def get_status(self) -> dict[str, Any]:
        snapshot = self._state.snapshot if isinstance(self._state.snapshot, dict) else {}
        classification = snapshot.get("classification") if isinstance(snapshot.get("classification"), dict) else {}
        summary_context = snapshot.get("summary_context") if isinstance(snapshot.get("summary_context"), dict) else {}
        strategy_context = snapshot.get("strategy_context") if isinstance(snapshot.get("strategy_context"), dict) else {}
        mode = snapshot.get("mode") if isinstance(snapshot.get("mode"), dict) else self._mode_controller.describe(self._state.control_mode)
        planned_operation = snapshot.get("planned_operation") if isinstance(snapshot.get("planned_operation"), dict) else None
        agent_operation = snapshot.get("agent_operation") if isinstance(snapshot.get("agent_operation"), dict) else None
        executable_operation = snapshot.get("executable_operation") if isinstance(snapshot.get("executable_operation"), dict) else None
        agent_packet = snapshot.get("agent_packet") if isinstance(snapshot.get("agent_packet"), dict) else {}
        situation_summary = snapshot.get("situation_summary") if isinstance(snapshot.get("situation_summary"), dict) else {}
        catgirl_sync = snapshot.get("catgirl_sync") if isinstance(snapshot.get("catgirl_sync"), dict) else {}
        summary = (
            f"transport={self._state.transport_state} | "
            f"screen={snapshot.get('screen', 'unknown')} | "
            f"class={classification.get('screen_class', 'unknown')} | "
            f"mode={mode.get('mode', 'unknown')} | "
            f"autoplay={self._state.autoplay_state}"
        )
        return {
            "summary": summary,
            "execution_status": self.execution_status(),
            "active_requirements": self._companion_session.active_requirements(),
            "message": summary,
            "server": {
                "state": self._state.transport_state,
                "base_url": self._state.base_url,
                "last_error": self._state.decision_error or self._state.last_error,
                "last_success_at": self._state.last_success_at,
                "last_poll_at": self._state.last_poll_at,
                "consecutive_errors": self._state.consecutive_errors,
                "decision_errors": self._state.decision_errors,
                "decision_error": self._state.decision_error,
                "input_warning": self._state.input_warning,
                "dialogue_record_types": dict(self._dialogue_record_types),
                "retry_at": self._state.retry_at,
            },
            "run": {
                "screen": snapshot.get("screen", "unknown"),
                "floor": snapshot.get("floor", 0),
                "act": snapshot.get("act", 0),
                "in_combat": snapshot.get("in_combat", False),
                "step_count": self._state.step_count,
                "snapshot_signature": self._state.snapshot_signature,
            },
            "autoplay": {
                "state": self._state.autoplay_state,
                "standby": self._state.standby,
                "control_mode": self._state.control_mode,
                "pause_reason": self._state.pause_reason,
                "stop_reason": self._state.stop_reason,
                "is_polling": self._loop_runner.is_polling(),
                "is_autoplaying": self._loop_runner.is_autoplaying(),
                "sse_connected": self._loop_runner._sse_connected,
                "mod_gate": dict(self._mod_gate),
                "speech": dict(self._speech_state),
                "wait_for_turn_message": self._wait_for_turn_message,
                "turn_message_anchor": dict(self._turn_message_anchor),
                "current_turn_message": self._turn_message,
                "waiting_for_message": bool(self._wait_for_turn_message and self._companion_session.anchor().get("battle_id") and self._current_turn_message() is None),
                "last_action_at": self._state.last_action_at,
                "guidance_generation": self._state.guidance_generation,
                "pending_guidance": list(self._state.pending_guidance),
                "interrupt_requested": self._state.interrupt_requested,
                "interrupt_reason": self._state.interrupt_reason,
            },
            "discussion": {
                "active": self._state.discussion_active,
                "status": self._state.discussion_status,
            },
            "companion_session": self._companion_session.context(),
            "decision_metrics": {**self._state.decision_metrics, **self._llm_planner.metrics},
            "latency_samples": list(self._state.latency_samples),
            "effect_report": self._companion_session.journal.report(),
            "learning_report": self._catgirl_memory.learning_report(),
            "decision_source": "llm",
            "companion_feedback": dict(self._feedback_result),
            "companion_mode": {
                "enabled": bool(self._cfg.get("companion_mode_enabled", self._cfg.get("neko_commentary_enabled", True))),
                "reporting_enabled": bool(self._cfg.get("neko_reporting_enabled", True)),
                "commentary_enabled": bool(self._cfg.get("neko_commentary_enabled", True)),
                "commentary_probability": float(self._cfg.get("neko_commentary_probability", 0.65) or 0.65),
                "critical_commentary_always": bool(self._cfg.get("neko_critical_commentary_always", True)),
                "latest_player_operation_observation": dict(self._state.latest_player_operation_observation),
            },
            "danmaku": {
                # 弹幕总开关（唯一真闸门）。catgirl_llm_enabled 只决定点评走 LLM 还是启发式文本。
                "enabled": self._danmaku_enabled,
                "catgirl_llm_enabled": self._catgirl_llm_enabled,
                "llm_available": bool(self._catgirl_llm is not None and self._catgirl_llm.available),
            },
            "instance": {
                # 插件当前对着哪个游戏进程：host（配置里的 base_url，单人局就是它；coop 里是主机）
                # / catgirl（coop 里被拉起的猫娘进程）。见 switch_active_instance。
                "active": self._state.active_instance,
                "host_base_url": self._host_base_url or self._state.base_url,
                "catgirl_base_url": self._catgirl_base_url,
            },
            "map_policy": {
                # wait = 彻底等玩家发话（不投票、也不跟票）；follow = 自动跟票；auto = 自己选。
                "policy": self._map_policy,
                "vote_wait_seconds": self._map_vote_wait_seconds,
            },
            "play_style": {
                # serious（默认，零影响）/ mischievous（20% 换成次优）/ silly（60% 换成最差）。
                "style": normalize_play_style(getattr(self._state, "play_style", PLAY_STYLE_SERIOUS)),
            },
            "classification": classification,
            "summary_context": summary_context,
            "strategy_context": strategy_context,
            "mode": mode,
            "planned_operation": planned_operation,
            "agent_operation": agent_operation,
            "executable_operation": executable_operation,
            "agent_packet": agent_packet,
            "situation_summary": situation_summary,
            "catgirl_sync": catgirl_sync,
            "preference_domains": list(self._preference_store.export_all().keys()),
            "recent_deliveries": list(self._state.recent_deliveries),
            "latest_sync_packet": dict(self._state.latest_sync_packet),
        }

    async def get_snapshot(self) -> dict[str, Any]:
        if not self._state.snapshot:
            await self.refresh_state(trigger_sync=True)
        snapshot = self._state.snapshot
        screen = snapshot.get("screen", "unknown") if isinstance(snapshot, dict) else "unknown"
        message = self.t("status.snapshot", default="当前快照：screen={screen}", screen=screen)
        return {"status": "ok", "message": message, "summary": message, "snapshot": snapshot}

    @staticmethod
    def _combat_fingerprint(snapshot: dict[str, Any]) -> str:
        raw = snapshot.get("raw_state") or {}
        payload = {
            "run_id": raw.get("run_id"), "floor": snapshot.get("floor"),
            "screen": snapshot.get("screen"), "session": raw.get("session"),
            "turn": raw.get("turn"), "in_combat": raw.get("in_combat"),
            "multiplayer": raw.get("multiplayer"), "combat": raw.get("combat"),
            "selection": raw.get("selection"), "potions": (raw.get("run") or {}).get("potions"),
            "actions": snapshot.get("available_actions"),
        }
        return sha1(json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")).hexdigest()[:16]

    @staticmethod
    def _combat_power(power: dict[str, Any]) -> dict[str, Any]:
        from .agent_facts import clean_effect_text

        # 效果文本（易伤=受到的攻击伤害提高50% 等）随 buff 一起给，模型据此理解实际
        # 作用，不靠 buff 名猜数值。Mod 端已解析占位符，这里只做清理。
        description = clean_effect_text(power.get("description"))
        result = {
            "id": power.get("power_id") or power.get("id") or "",
            "name": power.get("name") or power.get("power_id") or "",
            "amount": power.get("amount"), "is_debuff": bool(power.get("is_debuff", False)),
        }
        if description:
            result["effect"] = description
        return result

    @staticmethod
    def _action_card_hint(operation: dict[str, Any], view: dict[str, Any]) -> str:
        """动作日志里附上"打的是哪张牌/选的是哪个选项"。

        此前 `[sts2_llm_action] action=play_card` 只记动作名，玩家反馈"说的和打的不一样"
        时无法从日志判定到底出了哪张。取 agent_view 里对应下标的 `line`（含牌名）。
        """
        try:
            kwargs = operation.get("kwargs") if isinstance(operation.get("kwargs"), dict) else {}
            action_type = str(operation.get("action_type") or "")
            combat = view.get("combat") if isinstance(view.get("combat"), dict) else {}
            if action_type == "play_card":
                hand = combat.get("hand") if isinstance(combat.get("hand"), list) else []
                index = kwargs.get("card_index")
                card = next((c for c in hand if isinstance(c, dict)
                             and c.get("i", c.get("index")) == index), None)
                if isinstance(card, dict):
                    target = kwargs.get("target_index")
                    return f" card={card.get('line') or card.get('name')}" + (
                        f" target={target}" if target is not None else "")
                return f" card_index={index}"
            if action_type in {"use_potion", "discard_potion"}:
                return f" option_index={kwargs.get('option_index')}"
            return ""
        except Exception:
            return ""

    @staticmethod
    def _combat_card(card: dict[str, Any]) -> dict[str, Any]:
        text = str(card.get("resolved_rules_text") or card.get("rules_text") or "")
        dynamic = card.get("dynamic_values") or []
        effects = {str(item.get("name")): item.get("current_value") for item in dynamic}
        # 效果来自 Mod 的动态数值和规则文本，不以牌名猜测；条件仍保留在 rules_text 中。
        weak = any(name.lower() in {"weak", "weakpower"} and float(value or 0) > 0 for name, value in effects.items())
        vulnerable = any(name.lower() in {"vulnerable", "vulnerablepower"} and float(value or 0) > 0 for name, value in effects.items())
        return {
            "index": card.get("index"), "id": card.get("card_id"), "entity_id": card.get("entity_id"),
            "name": card.get("name") or card.get("card_id"), "card_type": card.get("card_type"),
            "cost": card.get("energy_cost"), "star_cost": card.get("star_cost"),
            "playable": bool(card.get("playable")), "unplayable_reason": card.get("unplayable_reason"),
            "rules_text": text, "effects": effects,
            "requires_target": bool(card.get("requires_target") or card.get("target_type") in {"AnyEnemy", "AnyAlly"}),
            "target_type": card.get("target_type"),
            "target_index_space": card.get("target_index_space") or ({"AnyEnemy": "enemies", "AnyAlly": "players"}.get(card.get("target_type"))),
            "legal_target_indices": list(card.get("valid_target_indices") or []),
            "can_apply_weak": weak, "can_apply_vulnerable": vulnerable,
        }

    async def get_combat_query(self, question: str = "", *, plan_combat: bool = False) -> dict[str, Any]:
        """只读查询；hand 永远属于 local_player，真人与猫娘不靠‘我方’代称。"""
        if self._client is None and self._catgirl_client is None:
            return {"status": "unavailable", "message": "战斗接口尚未连接，无法确认。", "summary": "当前未连接。", "connected": False}
        try:
            refreshed = await self.refresh_state(trigger_sync=False, plan_combat=plan_combat)
        except STS2TransportError as exc:
            message = f"当前无法读取尖塔战斗状态：{exc}"
            return {"status": "unavailable", "message": message, "summary": message, "connected": False}
        snapshot = refreshed["snapshot"]
        raw = snapshot.get("raw_state") or {}
        combat = raw.get("combat") or {}
        if not raw.get("in_combat") or not combat:
            message = f"当前不在战斗中（screen={snapshot.get('screen', 'unknown')}），无法确认战斗事实。"
            return {"status": "idle", "message": message, "summary": message, "connected": True}
        multiplayer = raw.get("multiplayer") or {}
        net_type = str(multiplayer.get("net_game_type") or "").lower()
        players = combat.get("players") or []
        local_stats = combat.get("player") or {}
        cards = [self._combat_card(card) for card in combat.get("hand") or []]
        player_views = []
        for item in players:
            view = {key: item.get(key) for key in ("player_id", "slot_index", "is_local", "character_id", "character_name", "current_hp", "max_hp", "block", "energy", "is_alive")}
            # 角色映射只用于已知的双人主机/客户端。未知多人布局不推断谁是真人。
            role = "unknown"
            if len(players) == 2 and net_type in {"host", "client"}:
                role = "human" if bool(item.get("is_local")) == (net_type == "host") else "companion"
            elif not multiplayer.get("is_multiplayer") and item.get("is_local"):
                role = "controlled_player"
            view["role"] = role
            powers = local_stats.get("powers") if item.get("is_local") else item.get("powers")
            view["powers"] = [self._combat_power(power) for power in powers] if powers is not None else None
            view["powers_known"] = powers is not None
            other_hand = item.get("hand")
            view["hand"] = cards if item.get("is_local") else ([self._combat_card(card) for card in other_hand] if other_hand is not None else None)
            player_views.append(view)
        local = next((item for item in player_views if item.get("is_local")), None)
        if local is None:
            local = {**local_stats, "is_local": True, "role": "controlled_player", "hand": cards,
                     "character_id": (raw.get("run") or {}).get("character_id")}
            player_views.append(local)
        enemies = []
        for index, enemy in enumerate(combat.get("enemies") or []):
            entity_id = enemy.get("entity_id")
            enemies.append({
                "index": enemy.get("index", index), "stable_id": entity_id, "identity_known": bool(entity_id),
                "id": enemy.get("enemy_id"), "name": enemy.get("name") or enemy.get("enemy_id"),
                "current_hp": enemy.get("current_hp"), "max_hp": enemy.get("max_hp"), "block": enemy.get("block"),
                "alive": bool(enemy.get("is_alive", True)), "hittable": bool(enemy.get("is_hittable", True)),
                "intent": enemy.get("intent"), "intents": list(enemy.get("intents") or []),
                "powers": [self._combat_power(power) for power in enemy.get("powers") or []],
            })
        snapshot_id = self._combat_fingerprint(snapshot)
        identity = {"active_instance": "catgirl" if self._catgirl_client is not None else "singleplayer", "base_url": self._companion_client().base_url,
                    "local_player_id": local.get("player_id"), "hand_owner_id": local.get("player_id"), "hand_owner_role": local.get("role"),
                    "note": "本地是同伴自己的游戏进程；hand 属于同伴。其他玩家数据仅来自该进程 Mod 实际输出。"}
        message = f"当前读取{local.get('character_name') or local.get('character_id')}（{local.get('role')}）的操作界面，HP {local.get('current_hp')}/{local.get('max_hp')}，能量 {local.get('energy')}；手牌 {len(cards)} 张。"
        return {
            "status": "ok", "message": message, "summary": message, "connected": True,
            "snapshot_id": snapshot_id, "captured_at": snapshot.get("polled_at", time()), "question": question,
            "turn": raw.get("turn"), "turn_key": self._combat_turn_key(snapshot), "identity": identity,
            "local_player": local, "players": player_views,
            "human_player": next((item for item in player_views if item["role"] == "human"), None),
            "companion_player": next((item for item in player_views if item["role"] == "companion"), None),
            "enemies": enemies, "hand": cards,
            "agent_facts": build_agent_facts(raw), "session": self._companion_session.context(),
            "cards_played_this_turn": local_stats.get("cards_played_this_turn"),
            "available_actions": [item.get("type") for item in snapshot.get("available_actions") or []],
            "effect_matches": {"weak_cards": [card["index"] for card in cards if card["can_apply_weak"] and card["playable"]],
                               "vulnerable_cards": [card["index"] for card in cards if card["can_apply_vulnerable"] and card["playable"]]},
            "discussion": {"active": self._state.discussion_active, "status": self._state.discussion_status},
        }

    async def start_discussion(self, reason: str = "user") -> dict[str, Any]:
        if not self._state.discussion_active:
            self._state.discussion_previous = {"mode": self._state.control_mode, "autoplay": self._state.autoplay_state,
                                               "pause_reason": self._state.pause_reason}
        self._state.control_generation += 1
        self._invalidate_companion_plan()
        self._state.discussion_active = True
        self._state.discussion_status = "pausing"
        self._state.pause_reason = reason
        sync = await self._sync_mod_gate("hold", discussion=True)
        settled = sync.get("applied") and sync.get("settled") and not self._action_dispatch_lock.locked()
        self._state.discussion_status = "paused" if settled else "pausing"
        self._sync_background_polling()
        self._emit_status()
        return {"status": "ok" if settled else "unconfirmed", "mod_sync": sync,
                "message": "已暂停新动作，可以继续聊；已提交的动作会完成。",
                "discussion": {"active": True, "status": self._state.discussion_status}}

    async def end_discussion(self) -> dict[str, Any]:
        if not self._state.discussion_active:
            return {"status": "idle", "message": "当前未暂停讨论。", "executed": False}
        sync = await self._sync_mod_gate("hold", discussion=False)
        if not (sync.get("applied") and sync.get("settled")):
            return {"status": "unconfirmed", "message": "控制回执尚未确认，继续暂停动作。", "mod_sync": sync}
        self._state.discussion_active = False
        self._state.discussion_status = "inactive"
        previous = self._state.discussion_previous
        if self._state.pause_reason in {"user", "discussion"} or self._state.autoplay_state == previous.get("autoplay"):
            self._state.pause_reason = str(previous.get("pause_reason") or "")
        self._state.discussion_previous = {}
        self._state.control_generation += 1
        self._invalidate_companion_plan()
        self._sync_background_polling()
        if self._state.autoplay_state == "running":
            self._loop_runner.start_autoplay()
        self._emit_status()
        return {"status": "ok", "message": "已结束讨论，恢复自主行动。", "mod_sync": sync}

    async def execute_planned_operation(self) -> dict[str, Any]:
        # 显式授权：玩家说了"就这个"，所以绕开听安排档位的停手闸门。
        return await self.run_autoplay_step(force=True)

    def start_autoplay(self) -> dict[str, Any]:
        if self._state.standby:
            self._state.autoplay_state = "standby"
            message = self.t("autoplay.start_blocked_standby", default="当前处于 standby 模式，无法启动自动运行。")
            return {"status": "idle", "message": message, "summary": message}
        # "让它自己玩起来" 同时也意味着退出"听安排"：否则启动了自动玩却仍然每步等发话，很反直觉。
        self._apply_control_mode("program")
        self._note_decision_success()
        self._decision_failure_notice = ""
        self._last_decision_key = ""
        self._state.autoplay_state = "running"
        self._state.pause_reason = ""
        self._state.stop_reason = ""
        self._sync_background_polling()
        self._loop_runner.start_autoplay()
        self._emit_status()
        message = self.t("autoplay.started", default="已启动尖塔自动运行。")
        return {"status": "ok", "message": message, "summary": message}

    def pause_autoplay(self, reason: str = "user") -> dict[str, Any]:
        self._action_window = {}
        self._last_confirmed = {}
        self._state.control_generation += 1
        self._invalidate_companion_plan()
        self._state.autoplay_state = "paused"
        self._state.pause_reason = reason
        self._sync_background_polling()
        self._emit_status()
        message = self.t("autoplay.paused", default="已暂停尖塔自动运行。")
        return {"status": "ok", "message": message, "summary": message, "pause_reason": reason}

    def resume_autoplay(self) -> dict[str, Any]:
        if self._state.standby:
            message = self.t("autoplay.resume_blocked_standby", default="当前处于 standby 模式，不能恢复自动运行。")
            return {"status": "idle", "message": message, "summary": message}
        self._note_decision_success()
        self._decision_failure_notice = ""
        self._state.autoplay_state = "running"
        self._state.control_generation += 1
        self._last_decision_key = ""
        self._state.pause_reason = ""
        self._sync_background_polling()
        self._loop_runner.start_autoplay()
        self._emit_status()
        message = self.t("autoplay.resumed", default="已恢复尖塔自动运行。")
        return {"status": "ok", "message": message, "summary": message}

    def stop_autoplay(self, reason: str = "manual") -> dict[str, Any]:
        self._state.control_generation += 1
        self._invalidate_companion_plan()
        self._state.autoplay_state = "standby" if self._state.standby else "idle"
        if self._state.standby:
            # 待机期间玩家明确喊停：这条"停"要活过取消待机（否则一取消待机又自己玩起来了）。
            self._state.autoplay_state_before_standby = "idle"
        self._state.stop_reason = reason
        self._state.pause_reason = ""
        self._sync_background_polling()
        self._emit_status()
        message = self.t("autoplay.stopped", default="已停止尖塔自动运行。")
        return {"status": "ok", "message": message, "summary": message, "stop_reason": reason}

    def set_host_digest_enabled(self, enabled: bool) -> dict[str, Any]:
        """开关「尖塔速报」：是否把局势悄悄交给本体，让它自己主动跟观众讲一句。

        关掉只影响这一路（不可见、不进记忆的主动投递）；**游戏内弹幕、自动游玩、历史记录都不受影响**。
        """
        self._state.host_digest_enabled = bool(enabled)
        self._cfg["host_digest_enabled"] = bool(enabled)
        self._emit_status()
        message = "好，局势速报开着，我会自己找话说。" if enabled else "好，我不主动报局势了，等你说。"
        return {"status": "ok", "enabled": bool(enabled), "message": message, "summary": message}

    def set_run_reflection_enabled(self, enabled: bool) -> dict[str, Any]:
        """开关「尖塔复盘」：局末是否生成反思并推给本体。

        关掉后**结构化记录照旧写**（损血归因、路线图、历史页都能看），只是不再让模型复盘、
        也不再加"暂停-恢复"这一步。
        """
        self._state.run_reflection_enabled = bool(enabled)
        self._cfg["run_reflection_enabled"] = bool(enabled)
        self._emit_status()
        message = "好，打完我会复盘一局。" if enabled else "好，我不复盘了，只记数据。"
        return {"status": "ok", "enabled": bool(enabled), "message": message, "summary": message}

    def set_wait_for_turn_message(self, enabled: bool) -> dict[str, Any]:
        """开关「每回合等主人发言再出牌」。

        - ``enabled=True``（陪玩节奏）：战斗里每个回合**强制**等主人说一句话才决策，没有发言
          就停手等待。互动感强，但慢。
        - ``enabled=False``（全自动）：不等发言，自己连续打。**指挥不受影响**——主人中途说的
          话仍会转成指引塞进下一次决策并立即打断重算（见 `queue_guidance`）。

        关掉后如果本来停在"等待发言"状态，这里顺手把它踢回自主行动，否则要等下一次别的触发
        才会动，表现为"答应了却不出牌"。
        """
        self._wait_for_turn_message = bool(enabled)
        self._cfg["wait_for_turn_message"] = bool(enabled)
        if not enabled and not self._state.standby and not self._state.discussion_active \
                and self._state.control_mode == "program" \
                and self._state.autoplay_state in {"idle", "paused"} \
                and self._state.pause_reason in {"", "waiting_for_message"}:
            self._state.autoplay_state = "running"
            self._state.pause_reason = ""
            self._state.stop_reason = ""
            self._sync_background_polling()
            self._loop_runner.start_autoplay()
        self._emit_status()
        message = ("好，每回合我等你一句话再出牌。" if enabled
                   else "好，我自己连着打，你随时插话我都听。")
        return {"status": "ok", "enabled": bool(enabled), "message": message, "summary": message}

    def set_standby(self, standby: bool) -> dict[str, Any]:
        normalized = self._apply_control_mode("standby" if standby else "program")
        return self._mode_controller.describe(normalized) | {"mode": normalized}

    def set_danmaku_enabled(self, enabled: bool) -> dict[str, Any]:
        """游戏内弹幕总开关（插件侧真闸门）。

        关闭后 _push_catgirl 直接早退：既不生成 LLM 点评，也不拉头像，更不会 POST /danmaku。
        同时把 mod 侧的 /danmaku 收口（danmaku_push_enabled）也一起写掉并清屏，避免重连窗口期
        或其它本地调用者把弹幕漏出来。与 catgirl_llm_enabled 无关——后者只切换点评文本来源。
        """
        self._danmaku_enabled = bool(enabled)
        self._cfg["danmaku_enabled"] = self._danmaku_enabled
        # 开关变化后清掉同步去重状态，重新打开时第一条点评不会被旧指纹吃掉。
        self._state.last_sync_fingerprint = ""
        self._state.last_push_scene_key = ""
        self._state.last_push_reason = ""
        self._emit_status()
        message = (
            self.t("danmaku.enabled", default="已打开游戏内猫娘弹幕。")
            if self._danmaku_enabled
            else self.t("danmaku.disabled", default="已关闭游戏内猫娘弹幕（自动游玩不受影响）。")
        )
        return {
            "status": "ok",
            "message": message,
            "summary": message,
            "enabled": self._danmaku_enabled,
        }

    async def sync_danmaku_gate_to_mod(self) -> dict[str, Any]:
        """把弹幕开关写到 mod（POST /config danmaku_push_enabled），关闭时顺带清屏。

        失败是良性的：插件侧的 _push_catgirl 已经挡住了推送，这里只是纵深防御 + 立刻清掉屏幕上
        还在飘的旧弹幕。
        """
        result: dict[str, Any] = {"applied": False, "cleared": False}
        try:
            client = self._require_client()
        except Exception:
            return result
        try:
            await client.set_config(danmaku_push_enabled=self._danmaku_enabled)
            result["applied"] = True
        except Exception as exc:
            self.logger.debug(f"[sts2] sync danmaku gate to mod failed (non-fatal): {exc}")
        if not self._danmaku_enabled:
            try:
                await client.clear_danmaku()
                result["cleared"] = True
            except Exception as exc:
                self.logger.debug(f"[sts2] clear danmaku overlay failed (non-fatal): {exc}")
        return result

    # ---- 对话式地图协作（sts2_set_route）-----------------------------------------------

    def set_map_policy(self, policy: str) -> dict[str, Any]:
        """切换地图投票策略。默认 wait：不投票也不跟票，等玩家发话。"""
        self._map_policy = normalize_map_policy(policy)
        self._cfg["map_policy"] = self._map_policy
        self._planner.configure_map_policy(
            policy=self._map_policy,
            vote_wait_seconds=self._map_vote_wait_seconds,
        )
        self._emit_status()
        message = {
            MAP_POLICY_WAIT: "地图我等你发话，你不说我不投。",
            MAP_POLICY_FOLLOW: "地图我会自动跟你投同一个节点。",
            MAP_POLICY_AUTO: "地图我自己按稳妥的挑。",
        }[self._map_policy]
        return {"status": "ok", "message": message, "summary": message, "policy": self._map_policy}

    def set_catgirl_name(self, name: str) -> dict[str, Any]:
        """设置猫娘在联机里的显示名（空串=不改名，回落到 Steam 名）。"""
        self._catgirl_display_name = str(name or "").strip()
        self._cfg["coop_catgirl_name"] = self._catgirl_display_name
        self._emit_status()
        resolved = self.resolve_catgirl_display_name()
        message = (
            f"好，联机里我就叫「{resolved}」。"
            if resolved else "好，联机名字改回跟 Steam 一样。"
        )
        return {"status": "ok", "message": message, "summary": message, "name": self._catgirl_display_name}

    async def sync_map_policy_to_mod(self) -> dict[str, Any]:
        """把等待策略写给 mod，让 mod 自己的自动玩循环也别抢路线。

        插件与 mod 各有一套决策循环，只改一边会出现"插件在等、mod 照投"。
        失败是良性的：插件侧 planner 在 wait 下本来就会返回 None 不投票。
        """
        try:
            client = self._require_client()
        except Exception:
            return {"applied": False, "policy": self._map_policy}
        try:
            await client.set_config(coop_map_policy=self._map_policy)
            return {"applied": True, "policy": self._map_policy}
        except Exception as exc:
            self.logger.debug(f"[sts2] sync map policy to mod failed (non-fatal): {exc}")
            return {"applied": False, "policy": self._map_policy}

    def resolve_catgirl_display_name(self) -> str:
        """猫娘在联机里的显示名：显式配置优先，否则用本体当前角色名。解析不到就返回空串（=不改名）。"""
        if self._catgirl_display_name:
            return self._catgirl_display_name
        try:
            return str(self._catgirl_llm.character_name() or "").strip()
        except Exception:
            return ""

    async def sync_catgirl_name_to_mod(self) -> dict[str, Any]:
        """把猫娘的显示名推给两个游戏进程的 mod，让联机名牌能区分"猫娘"和"真人"。

        两个进程跑在同一 Steam 账号上，名牌默认同名。mod 侧 PlayerNamePatch 只在猫娘玩家的名牌上
        用这个名字（猫娘进程认"自己"、宿主进程认"另一个玩家"），所以两个客户端都要写。
        失败是良性的：不写就回落到游戏原生的 Steam 名。
        """
        name = self.resolve_catgirl_display_name()
        result: dict[str, Any] = {"applied": False, "name": name, "host": False, "catgirl": False}
        targets: list[tuple[str, Any]] = []
        if self._host_client is not None:
            targets.append(("host", self._host_client))
        if self._catgirl_client is not None:
            targets.append(("catgirl", self._catgirl_client))
        if not targets and self._client is not None:
            targets.append(("host", self._client))
        for label, client in targets:
            try:
                await client.set_config(coop_catgirl_name=name)
                result[label] = True
                result["applied"] = True
            except Exception as exc:
                self.logger.debug(f"[sts2] sync catgirl name to mod ({label}) failed (non-fatal): {exc}")
        return result

    async def set_route(self, *, mode: str = "safe", pick: Any = None, index: Any = None) -> dict[str, Any]:
        # 旧入口仅传递本局的路线讨论，不再用启发式替本体投票。
        instruction = f"路线讨论：mode={mode}, pick={pick}, index={index}"
        self._queue_guidance(instruction, source="route")
        context = await self.chat_companion(instruction)
        context["message"] = "已读取路线局面，由当前角色自主判断，未直接投票。"
        context["summary"] = context["message"]
        return context

    def set_companion_mode(self, enabled: bool, *, emit_status: bool = True, push_message: bool = True) -> dict[str, Any]:
        if not enabled and self._loop_runner.is_polling() and not self._loop_runner.is_autoplaying() and hasattr(self.logger, "info"):
            try:
                self.logger.info("[sts2_companion] disabling companion while polling active; keeping state refresh path quiet during teardown")
            except Exception:
                pass
        self._cfg["companion_mode_enabled"] = bool(enabled)
        self._cfg["neko_reporting_enabled"] = bool(enabled)
        self._cfg["neko_commentary_enabled"] = bool(enabled)
        if not enabled:
            self._state.latest_player_operation_observation = {}
            self._state.last_companion_scene_key = ""
            self._state.last_companion_turn_key = ""
            self._state.last_companion_evaluation_key = ""
            self._state.last_companion_combat_comment_key = ""
            self._state.last_companion_player_op_fingerprint = ""
            self._state.latest_sync_packet = {}
        message = self.t("companion.enabled", default="已开启陪玩模式。") if enabled else self.t("companion.disabled", default="已关闭陪玩模式。")
        if emit_status:
            self._emit_status()
        if enabled:
            self._sync_background_polling()
            if push_message:
                self._push_companion_message()
        else:
            self._sync_background_polling()
        return {
            "status": "ok",
            "message": message,
            "summary": message,
            "enabled": bool(enabled),
            "reporting_enabled": bool(self._cfg.get("neko_reporting_enabled", False)),
            "commentary_enabled": bool(self._cfg.get("neko_commentary_enabled", False)),
        }

    async def apply_user_override_safely(self, instruction: str, *, source: str = "user") -> dict[str, Any]:
        self._companion_session.remember("user", instruction, kind="pending" if self._ingress_enabled else "guidance")
        self._queue_guidance(instruction, source=source)
        return await self.chat_companion(instruction)

    async def chat_companion(self, message: str = "") -> dict[str, Any]:
        # 响应式聊天不经过任何动作/节流闸门，也不调用规划器。
        await self.refresh_state(plan_combat=False)
        view = self._agent_view(self._state.snapshot)
        await self._read_companion_dialogue()
        # 将游戏上下文交回现有 N.E.K.O 会话，由主人格回应；这里不另调模型生成聊天。
        return {"status": "ok", "message": "已读取自己进程的游戏状态。", "summary": "已读取当前游戏上下文。",
                "agent_view": view, "agent_facts": build_agent_facts(self._state.snapshot.get("raw_state")),
                "session": self._companion_session.context(),
                "question": message,
                "context_scope": "当前局面以本次查询为准；回答新的局面问题时重新查询。过去行动依据 session 中的执行记录。",
                "identity": "catgirl" if self._catgirl_client is not None else "singleplayer",
                "executed": False}

    def _require_client(self) -> STS2TransportClient:
        if self._client is None:
            raise RuntimeError(self.t("errors.client_not_started", default="STS2 client 未启动"))
        return self._client

    def _cfg_poll_interval(self, *, active: bool) -> float:
        key = "poll_interval_active_seconds" if active else "poll_interval_idle_seconds"
        return float(self._cfg.get(key, 1 if active else 3) or (1 if active else 3))

    def _cfg_use_event_stream(self) -> bool:
        try:
            return bool(self._cfg.get("use_event_stream", True))
        except Exception:
            return True

    def _cfg_fallback_poll_interval(self) -> float:
        try:
            return max(5.0, float(self._cfg.get("poll_fallback_seconds", 5.0) or 5.0))
        except (TypeError, ValueError):
            return 5.0

    def _cfg_action_interval(self) -> float:
        return float(self._cfg.get("action_interval_seconds", 1.5) or 1.5)

    def _cfg_companion_push_probability(self) -> float:
        try:
            value = float(self._cfg.get("companion_push_probability", 0.2) or 0.2)
        except Exception:
            return 0.2
        return min(1.0, max(0.0, value))

    def _cfg_autoplay_push_probability(self) -> float:
        try:
            value = float(self._cfg.get("autoplay_push_probability", 0.35) or 0.35)
        except Exception:
            return 0.35
        return min(1.0, max(0.0, value))

    def _should_allow_push_by_probability(self, *, companion_mode: bool) -> bool:
        probability = self._cfg_companion_push_probability() if companion_mode else self._cfg_autoplay_push_probability()
        if probability >= 1.0:
            return True
        if probability <= 0.0:
            return False
        return random() < probability

    def _apply_control_mode(self, mode: str) -> str:
        normalized = self._mode_controller.normalize(mode)
        previous_standby = self._state.standby
        previous_mode = self._state.control_mode
        self._state.control_mode = normalized
        if self._state.standby:
            if not previous_standby:
                # 记下"进待机之前想干什么"：待机是**临时停手**，取消时要回到原来的意图。
                self._state.autoplay_state_before_standby = self._state.autoplay_state
            self._state.autoplay_state = "standby"
        elif previous_standby and self._state.autoplay_state == "standby":
            # 退出待机：还回进入前的意图。以前这里写死 "idle"，而 `_autoplay_loop` 只认 "running"，
            # 结果"取消待机"会把自动游玩永久停摆（实测 step 冻住）。空记忆时按开机意图兜底。
            remembered = self._state.autoplay_state_before_standby
            if not remembered:
                remembered = "running" if bool(self._cfg.get("autoplay_on_start", False)) else "idle"
            self._state.autoplay_state = remembered
            self._state.autoplay_state_before_standby = ""
            # 待机期间轮询可能被停过，恢复意图后必须把轮询一起拉起来（幂等）。
            self._sync_background_polling()
        # 任何档位切换都要打断当前决策：否则 program<->advise 切换后，循环还会按旧档位把
        # 已经算好的那一步执行掉（切到"听安排"却仍然走了一步）。
        if previous_mode != normalized:
            self._state.control_generation += 1
            self._invalidate_companion_plan()
            self._state.interrupt_requested = True
            self._state.interrupt_reason = "mode_change"
        return normalized

    async def set_control_mode(self, mode: str) -> dict[str, Any]:
        """切换猫娘的控制档位。

        - ``program``（自己玩）：照常决策并执行。默认。
        - ``advise``：所有界面只判断不自动执行，包括战斗。
        - ``standby``（停手）：连计划都不算，并且会让 mod 一起停手。
        """
        normalized = self._apply_control_mode(mode)
        # 玩家说“自己玩/继续打”时，切回 program 也必须解除此前的暂停。
        # 仅更新 control_mode 会留下 autoplay_state=paused，表现为已经答应但永远不出牌。
        if (normalized == "program" and not self._state.standby
                and self._state.autoplay_state in {"paused", "idle"}
                and self._state.pause_reason != "run_reflection"):
            self._note_decision_success()
            self._decision_failure_notice = ""
            self._state.autoplay_state = "running"
            self._state.pause_reason = ""
            self._state.stop_reason = ""
            self._sync_background_polling()
            self._loop_runner.start_autoplay()
        sync = await self.sync_control_mode_to_mod()
        self._emit_status()
        message = {
            "program": "好，我自己玩，你不用一直盯着。",
            "advise": "我先判断局面，暂停自动执行；我们可以继续聊。",
            "standby": "好，我彻底停手了。",
        }.get(normalized, "好，收到。")
        return {
            "status": "ok",
            "message": message,
            "summary": message,
            "mode": normalized,
            "mod_sync": sync,
        }

    async def sync_control_mode_to_mod(self) -> dict[str, Any]:
        # Mod 只负责进房；插件作为唯一决策驱动，始终让 Mod 自主驱动 hold。
        return await self._sync_mod_gate("hold")

    def _in_active_run(self) -> bool:
        """当前是否已在**局内**（有 run_id 且不在主菜单/大厅/选人这些玩家手动控制的界面）。"""
        snapshot = self._state.snapshot if isinstance(self._state.snapshot, dict) else {}
        run_id = str(snapshot.get("run_id") or "")
        if not run_id or run_id == "run_unknown":
            return False
        screen = str(snapshot.get("screen") or "").upper()
        return screen not in {"", "UNKNOWN", "MAIN_MENU", "MULTIPLAYER_LOBBY", "CHARACTER_SELECT"}

    async def reconcile_turn_control(self) -> None:
        """新游戏进程也必须停住 Mod；再按节奏决定何时启动插件决策循环。

        两种节奏：
        - 等发言（``wait_for_turn_message=True``）：本回合主人说了话才启动自主运行。
        - 全自动（``False``）：一进局（非主菜单/大厅/选人）就自己开打，不等发言；主人中途插话
          仍会转成指引立即被采纳。**只在 ``idle`` 时启动**，不碰玩家手动 `paused`/`stop` 的状态。
        """
        self._clear_previous_run_failure()
        sync = await self.sync_control_mode_to_mod()
        if not (sync.get("applied") and sync.get("settled")):
            self.logger.warning("[sts2_driver] 等待 Mod 停手确认：%s", sync)
            return
        if not self._wait_for_turn_message:
            if (self._in_active_run() and self._state.autoplay_state == "idle"
                    and self._state.control_mode == "program"
                    and not self._state.stop_reason and not self._state.discussion_active
                    and not self._state.standby):
                self.start_autoplay()
            return
        await self._read_companion_dialogue()
        if (self._companion_session.anchor().get("battle_id") and self._current_turn_message()
                and self._state.autoplay_state == "idle" and self._state.control_mode == "program"
                and not self._state.stop_reason and not self._state.discussion_active and not self._state.standby):
            self.start_autoplay()

    async def _sync_mod_gate(self, mapped: str, *, discussion: bool | None = None) -> dict[str, Any]:
        try:
            client = await self._ensure_companion_client()
        except Exception:
            return {"applied": False, "mode": mapped}
        try:
            paused = self._state.discussion_active if discussion is None else discussion
            key = (client.base_url, mapped, paused, self._danmaku_enabled, self._state.control_generation)
            if key == self._mod_gate_key and self._mod_gate.get("applied") and self._mod_gate.get("settled"):
                return self._mod_gate
            response = await client.set_config(coop_control_mode=mapped, coop_discussion_paused=paused,
                                               llm_enabled=False, coop_manual_character_selection=True,
                                               danmaku_enabled=False, danmaku_push_enabled=self._danmaku_enabled)
            echoed = str(response.get("coop_control_mode") or "").strip().lower()
            # 新版 Mod 回显实际持久化的模式；旧版只返回 updated，不能把它误报成两侧已暂停。
            applied = echoed == mapped and response.get("coop_discussion_paused") is paused
            self._mod_gate = {"applied": applied, "mode": mapped, "echoed_mode": echoed or None,
                    "manual_character_selection": response.get("coop_manual_character_selection") is True,
                    "settled": response.get("autoplay_action_in_flight") is False,
                    "persistent": response.get("coop_hold_persistent") is True}
            self._mod_gate_key = key if applied else None
            return self._mod_gate
        except Exception as exc:
            self.logger.debug(f"[sts2] sync control mode to mod failed (non-fatal): {exc}")
            return {"applied": False, "mode": mapped}

    def _queue_guidance(self, content: str, *, source: str = "neko") -> dict[str, Any]:
        self._state.guidance_generation += 1
        guidance = normalize_guidance_instruction(
            content,
            source=source,
            guidance_type="soft_guidance",
        )
        guidance["id"] = f"guidance-{self._state.guidance_generation}"
        guidance["generation"] = self._state.guidance_generation
        guidance["origin"]["generation"] = self._state.guidance_generation
        guidance["anchor"] = {**self._companion_session.anchor(), "timestamp": time()}
        self._state.pending_guidance.append(guidance)
        self._state.pending_guidance = self._state.pending_guidance[-20:]
        self._state.interrupt_requested = True
        self._state.interrupt_reason = "guidance"
        self._invalidate_companion_plan()
        return guidance

    def _consume_guidance(self, operation: dict[str, Any] | None) -> None:
        if not isinstance(operation, dict):
            return
        consumed_generation = int(operation.get("consumed_guidance_generation") or 0)
        consumed_ids = {
            str(item)
            for item in (operation.get("consumed_guidance_ids") if isinstance(operation.get("consumed_guidance_ids"), list) else [])
            if str(item)
        }
        if consumed_generation <= 0 and not consumed_ids:
            return
        self._state.last_consumed_guidance_generation = max(self._state.last_consumed_guidance_generation, consumed_generation)
        self._state.pending_guidance = [
            item for item in self._state.pending_guidance
            if int(item.get("generation") or 0) > self._state.last_consumed_guidance_generation and str(item.get("id") or "") not in consumed_ids
        ]
        self._state.interrupt_requested = False
        self._state.interrupt_reason = ""

    def _should_rebuild_operation(self, snapshot: dict[str, Any]) -> bool:
        if self._state.force_replan_requested:
            # 上次动作被拒 = 手里的现场/计划过期了，必须重算，别拿旧现场再撞一次。
            return True
        if self._state.interrupt_requested:
            return True
        operation = snapshot.get("agent_operation") if isinstance(snapshot.get("agent_operation"), dict) else None
        if operation is None:
            return True
        decision_epoch = int(operation.get("decision_epoch") or 0)
        return decision_epoch < self._state.guidance_generation

    @staticmethod
    def _snapshot_state_name(snapshot: dict[str, Any]) -> str:
        classification = snapshot.get("classification") if isinstance(snapshot.get("classification"), dict) else {}
        return str(classification.get("state_name") or "").strip().lower()

    def _should_hold_for_orders(self, snapshot: dict[str, Any]) -> bool:
        """"听安排"档位下是否该停手等玩家发话。

        战斗不在停手清单里（玩家替不了猫娘出牌），主菜单/组队大厅/选角色也不在
        （那是猫娘加入房间的必经流程）。判定细节见 state_machine.should_hold_for_orders。
        """
        if not self._mode_controller.describe(self._state.control_mode).get("advise"):
            return False
        return should_hold_for_orders(self._snapshot_state_name(snapshot), mode="advise")

    def _should_deliver_sync(self, catgirl_sync: dict[str, Any]) -> bool:
        fingerprint = str(catgirl_sync.get("fingerprint") or "")
        min_interval = float(catgirl_sync.get("min_interval_seconds") or 0.0)
        force = bool(catgirl_sync.get("force"))
        payload = catgirl_sync.get("payload") if isinstance(catgirl_sync.get("payload"), dict) else {}
        screen = str(payload.get("screen") or "")
        summary_kind = str(payload.get("summary_kind") or "")
        if force:
            try:
                self.logger.info(
                    "[sts2_push_debug] should_deliver_sync allow: force=true fingerprint=%s screen=%s summary_kind=%s",
                    fingerprint,
                    screen,
                    summary_kind,
                )
            except Exception:
                pass
            return True
        if self._state.interrupt_requested:
            try:
                self.logger.info(
                    "[sts2_push_debug] should_deliver_sync allow: interrupt_requested=true reason=%s fingerprint=%s",
                    self._state.interrupt_reason,
                    fingerprint,
                )
            except Exception:
                pass
            return True
        if fingerprint and fingerprint == self._state.last_sync_fingerprint:
            now = time()
            elapsed = now - self._state.last_sync_at if self._state.last_sync_at else None
            if self._state.last_sync_at and elapsed < min_interval:
                self._state.sync_repeat_count += 1
                try:
                    self.logger.info(
                        "[sts2_push_debug] should_deliver_sync deny: duplicate fingerprint=%s elapsed=%.3f min_interval=%.3f repeat_count=%s last_screen=%s last_summary_kind=%s",
                        fingerprint,
                        elapsed,
                        min_interval,
                        self._state.sync_repeat_count,
                        self._state.last_sync_screen,
                        self._state.last_sync_summary_kind,
                    )
                except Exception:
                    pass
                return False
            try:
                self.logger.info(
                    "[sts2_push_debug] should_deliver_sync allow: duplicate fingerprint outside interval fingerprint=%s elapsed=%s min_interval=%.3f",
                    fingerprint,
                    f"{elapsed:.3f}" if elapsed is not None else "none",
                    min_interval,
                )
            except Exception:
                pass
        if screen != self._state.last_sync_screen or summary_kind != self._state.last_sync_summary_kind:
            try:
                self.logger.info(
                    "[sts2_push_debug] should_deliver_sync allow: scene_changed fingerprint=%s screen=%s->%s summary_kind=%s->%s",
                    fingerprint,
                    self._state.last_sync_screen,
                    screen,
                    self._state.last_sync_summary_kind,
                    summary_kind,
                )
            except Exception:
                pass
            return True
        try:
            self.logger.info(
                "[sts2_push_debug] should_deliver_sync allow: default fingerprint=%s screen=%s summary_kind=%s last_fingerprint=%s",
                fingerprint,
                screen,
                summary_kind,
                self._state.last_sync_fingerprint,
            )
        except Exception:
            pass
        return True

    def _mark_loop_error(self, exc: Exception, *, autoplay: bool = False) -> None:
        """记录健康状态；自动决策/派发连续失败3轮后明确暂停，后台观察保持轮询。"""
        self._state.transport_state = "error"
        self._state.last_error = str(exc)
        self._state.consecutive_errors += 1
        if autoplay or isinstance(exc, STS2ActionRejected):
            self._state.decision_errors += 1
            self._state.decision_error = str(exc)
            if not self._pause_after_decision_failures():
                self._state.retry_at = time() + self._autoplay_retry_backoff()
        if isinstance(exc, STS2ActionRejected):
            # 状态漂移类拒绝：被拒的那一步没有落地，而缓存计划与手里的现场都是按"旧局面"算的 ——
            # 一并作废，下一轮先强制重刷 /state 再按现场重算，通常这一步就能过。
            if exc.retryable:
                self._invalidate_combat_plan_cache(reason=f"rejected:{exc.code}")
            self._invalidate_companion_plan()
            self._state.force_replan_requested = exc.retryable
            self.logger.info(
                "[sts2_recover] 动作被拒，已丢弃缓存的战斗计划: code=%s action=%s screen=%s",
                exc.code,
                exc.action_name,
                exc.screen,
            )
        self._emit_status()

    def _invalidate_combat_plan_cache(self, *, reason: str = "") -> None:
        """丢弃整回合缓存的 /solver/plan，让下一轮按现场重新取计划。

        ``solver_step_index`` 一并归零：新计划是**从当前局面**算出来的，下标自然从头开始。
        注意只归零、不清状态——被拒的那一步本来就没执行。
        """
        try:
            self._loop_runner.invalidate_combat_plan_cache()
        except Exception as exc:
            self.logger.debug(f"[sts2_recover] invalidate combat plan cache failed (non-fatal): {exc}")
        if isinstance(self._state.solver_step_index, int) and self._state.solver_step_index != 0:
            self._state.solver_step_index = 0
        if reason:
            self.logger.info("[sts2_recover] 战斗计划缓存作废 (%s)", reason)

    def _autoplay_retry_backoff(self) -> float:
        """自动玩出错后的退避：连错越多等越久，封顶 8 秒。

        与 ``_cfg_action_interval`` 同基准，保证"偶尔被拒"几乎无感（约 1.5 秒后重试），
        而 mod 真的不在时也不会每秒空转刷日志。
        """
        try:
            base = max(0.5, float(self._cfg_action_interval()))
        except (TypeError, ValueError):
            base = 1.5
        errors = max(1, self._state.consecutive_errors, self._state.decision_errors)
        return min(base * (2 ** min(errors - 1, 4)), 8.0)

    def _companion_mode_active(self) -> bool:
        return bool(self._cfg.get("companion_mode_enabled", self._cfg.get("neko_commentary_enabled", True))) and bool(self._cfg.get("neko_commentary_enabled", True))

    def _should_keep_polling(self) -> bool:
        # "error" 也在名单里：即使旧版本把自动玩留在了 error 态，也**必须继续轮询** ——
        # 否则外部控制租约一过期，mod 就以为插件走了，游戏彻底没人驱动（曾是"卡住"的最后一环）。
        return self._state.discussion_active or bool(self._cfg.get("companion_mode_enabled", False)) or self._state.autoplay_state in {"running", "paused", "error"}

    def _sync_background_polling(self) -> None:
        if self._should_keep_polling():
            self._loop_runner.start_background()
            if self._ingress_enabled:
                self._loop_runner.start_autoplay()
            return
        self._loop_runner.stop_background_sync()

    def _remember_plugin_action_marker(self, operation: dict[str, Any], snapshot: dict[str, Any]) -> None:
        action_type = str(operation.get("action_type") or "")
        if not action_type:
            self._state.last_plugin_action_fingerprint = ""
            return
        screen = str(snapshot.get("screen") or "unknown")
        floor = snapshot.get("floor")
        act = snapshot.get("act")
        turn = self._snapshot_turn(snapshot)
        action_kwargs = operation.get("kwargs") if isinstance(operation.get("kwargs"), dict) else {}
        payload = f"{action_type}|{screen}|{floor}|{act}|{turn}|{sorted(action_kwargs.items())}"
        self._state.last_plugin_action_fingerprint = sha1(payload.encode("utf-8")).hexdigest()[:16]

    def _observe_companion_player_operation(self, previous_snapshot: dict[str, Any], current_snapshot: dict[str, Any]) -> dict[str, Any] | None:
        if not self._companion_mode_active():
            return None
        if not isinstance(previous_snapshot, dict) or not previous_snapshot:
            return None
        if not isinstance(current_snapshot, dict) or not current_snapshot:
            return None

        previous_summary = self._summary_builder.build_snapshot_summary(previous_snapshot)
        current_summary = self._summary_builder.build_snapshot_summary(current_snapshot)
        delta = self._summary_engine.compute_delta(previous_summary, current_summary, source="player_operation")
        event_type = self._classify_player_operation(previous_snapshot, current_snapshot, previous_summary, current_summary, delta)
        if not event_type:
            return None

        observation = self._build_player_operation_observation(
            event_type=event_type,
            previous_snapshot=previous_snapshot,
            current_snapshot=current_snapshot,
            previous_summary=previous_summary,
            current_summary=current_summary,
            delta=delta,
        )
        if observation is None:
            return None
        if self._is_recent_plugin_action(observation):
            return None
        if not self._should_emit_player_operation(observation):
            return None
        return observation

    def _classify_player_operation(
        self,
        previous_snapshot: dict[str, Any],
        current_snapshot: dict[str, Any],
        previous_summary: dict[str, Any],
        current_summary: dict[str, Any],
        delta: dict[str, Any],
    ) -> str:
        previous_screen = str(previous_snapshot.get("screen") or "unknown")
        current_screen = str(current_snapshot.get("screen") or "unknown")
        previous_floor = self._safe_int(previous_snapshot.get("floor") if previous_snapshot.get("floor") is not None else previous_summary.get("floor"))
        current_floor = self._safe_int(current_snapshot.get("floor") if current_snapshot.get("floor") is not None else current_summary.get("floor"))
        previous_act = self._safe_int(previous_snapshot.get("act") if previous_snapshot.get("act") is not None else previous_summary.get("act"))
        current_act = self._safe_int(current_snapshot.get("act") if current_snapshot.get("act") is not None else current_summary.get("act"))
        previous_turn = self._safe_int(previous_summary.get("turn"))
        current_turn = self._safe_int(current_summary.get("turn"))
        previous_in_combat = bool(previous_summary.get("in_combat"))
        current_in_combat = bool(current_summary.get("in_combat"))

        if current_floor > previous_floor or current_act > previous_act:
            return "run_progressed"
        if not previous_in_combat and current_in_combat:
            return "combat_started"
        if previous_in_combat and not current_in_combat:
            return "combat_ended"
        if previous_screen != current_screen:
            target_class = str(current_snapshot.get("classification", {}).get("screen_class") if isinstance(current_snapshot.get("classification"), dict) else "")
            if target_class in {"reward", "selection", "shop", "rest"} or current_screen in {"event", "map", "shop", "rest", "reward"}:
                return "choice_committed"
            return "screen_transition"
        if current_in_combat and current_turn > previous_turn:
            return "combat_turn_advanced"
        if current_in_combat and self._combat_state_changed(delta):
            return "player_card_or_action_committed"
        if self._choice_state_changed(previous_snapshot, current_snapshot):
            return "choice_committed"
        return ""

    def _build_player_operation_observation(
        self,
        *,
        event_type: str,
        previous_snapshot: dict[str, Any],
        current_snapshot: dict[str, Any],
        previous_summary: dict[str, Any],
        current_summary: dict[str, Any],
        delta: dict[str, Any],
    ) -> dict[str, Any] | None:
        current_screen = str(current_snapshot.get("screen") or "unknown")
        floor = self._safe_int(current_snapshot.get("floor") if current_snapshot.get("floor") is not None else current_summary.get("floor"))
        act = self._safe_int(current_snapshot.get("act") if current_snapshot.get("act") is not None else current_summary.get("act"))
        turn = self._safe_int(current_summary.get("turn"))
        scene_key = f"{act}:{floor}:{current_screen}:{turn if event_type.startswith('combat') else 0}"
        summary = self._render_player_operation_summary(event_type, previous_snapshot, current_snapshot, delta)
        if not summary:
            return None
        delta_text = str(delta.get("text") or "").strip()
        fingerprint_payload = f"{event_type}|{current_screen}|{floor}|{act}|{turn}|{summary}|{delta_text}"
        fingerprint = sha1(fingerprint_payload.encode("utf-8")).hexdigest()[:16]
        return {
            "event_type": event_type,
            "screen": current_screen,
            "floor": floor,
            "act": act,
            "turn": turn,
            "scene_key": scene_key,
            "summary": summary,
            "delta_text": delta_text,
            "fingerprint": fingerprint,
            "captured_at": time(),
            "should_comment": True,
            "source": "state_observer",
        }

    def _render_player_operation_summary(self, event_type: str, previous_snapshot: dict[str, Any], current_snapshot: dict[str, Any], delta: dict[str, Any]) -> str:
        previous_screen = str(previous_snapshot.get("screen") or "unknown")
        current_screen = str(current_snapshot.get("screen") or "unknown")
        delta_text = str(delta.get("text") or "").strip()
        if event_type == "combat_started":
            return self.t("companion.player_operation.combat_started", default="玩家进入了新的战斗。")
        if event_type == "combat_ended":
            return self.t("companion.player_operation.combat_ended", default="玩家刚结束这场战斗，局面已经切到后续结算。")
        if event_type == "combat_turn_advanced":
            return self.t("companion.player_operation.combat_turn_advanced", default="玩家已经推进到新的战斗回合。")
        if event_type == "player_card_or_action_committed":
            return delta_text or self.t("companion.player_operation.action_committed", default="玩家刚在战斗中完成了一步操作。")
        if event_type == "choice_committed":
            return self.t("companion.player_operation.choice_committed", default="玩家刚完成了一个关键选择，画面从 {previous_screen} 进入 {current_screen}。", previous_screen=previous_screen, current_screen=current_screen)
        if event_type == "run_progressed":
            return self.t("companion.player_operation.run_progressed", default="玩家推进了当前流程，楼层或章节发生了变化。")
        if event_type == "screen_transition":
            return self.t("companion.player_operation.screen_transition", default="玩家把画面从 {previous_screen} 切换到了 {current_screen}。", previous_screen=previous_screen, current_screen=current_screen)
        return delta_text

    def _is_recent_plugin_action(self, observation: dict[str, Any]) -> bool:
        if not self._state.last_action_at:
            return False
        if time() - self._state.last_action_at > 2.5:
            return False
        last_action_frame = self._state.latest_action_frame if isinstance(self._state.latest_action_frame, dict) else {}
        before = last_action_frame.get("before") if isinstance(last_action_frame.get("before"), dict) else {}
        after = last_action_frame.get("after") if isinstance(last_action_frame.get("after"), dict) else {}
        action_type = str(last_action_frame.get("action_type") or "")
        screen = str(observation.get("screen") or "")
        turn = self._safe_int(observation.get("turn"))
        observation_event = str(observation.get("event_type") or "")
        if observation_event == "player_card_or_action_committed" and action_type == "play_card":
            return False
        if screen and screen in {str(before.get("screen") or ""), str(after.get("screen") or "")}:
            if turn and turn in {self._safe_int(before.get("turn")), self._safe_int(after.get("turn"))}:
                return True
            if observation_event in {"combat_ended", "choice_committed", "screen_transition"}:
                return True
        return False

    def _should_emit_player_operation(self, observation: dict[str, Any]) -> bool:
        fingerprint = str(observation.get("fingerprint") or "")
        if not fingerprint:
            return False
        if fingerprint == self._state.last_companion_player_op_fingerprint:
            min_interval = 0.0 if str(observation.get("event_type") or "") in {"combat_ended", "choice_committed", "run_progressed"} else 4.0
            if self._state.last_companion_player_op_at and time() - self._state.last_companion_player_op_at < min_interval:
                return False
        return True

    def _combat_state_changed(self, delta: dict[str, Any]) -> bool:
        player_changes = delta.get("player_changes") if isinstance(delta.get("player_changes"), dict) else {}
        enemy_changes = delta.get("enemy_changes") if isinstance(delta.get("enemy_changes"), dict) else {}
        hand_changes = delta.get("hand_changes") if isinstance(delta.get("hand_changes"), dict) else {}
        return any(
            [
                self._safe_int(player_changes.get("energy_delta")) != 0,
                self._safe_int(player_changes.get("block_delta")) != 0,
                self._safe_int(enemy_changes.get("enemy_total_hp_delta")) != 0,
                self._safe_int(hand_changes.get("hand_count_delta")) != 0,
                bool(hand_changes.get("left_cards")),
            ]
        )

    def _choice_state_changed(self, previous_snapshot: dict[str, Any], current_snapshot: dict[str, Any]) -> bool:
        previous_actions = previous_snapshot.get("available_actions") if isinstance(previous_snapshot.get("available_actions"), list) else []
        current_actions = current_snapshot.get("available_actions") if isinstance(current_snapshot.get("available_actions"), list) else []
        previous_names = {str(action.get("type") or "") for action in previous_actions if isinstance(action, dict)}
        current_names = {str(action.get("type") or "") for action in current_actions if isinstance(action, dict)}
        if previous_names == current_names:
            return False
        interesting = {"choose_reward_card", "claim_reward", "choose_event_option", "choose_map_node", "choose_rest_option", "buy_card", "buy_relic", "buy_potion"}
        return bool(previous_names & interesting or current_names & interesting)

    def _snapshot_turn(self, snapshot: dict[str, Any]) -> int:
        raw_state = snapshot.get("raw_state") if isinstance(snapshot.get("raw_state"), dict) else {}
        combat = raw_state.get("combat") if isinstance(raw_state.get("combat"), dict) else {}
        return self._safe_int(raw_state.get("turn") if raw_state.get("turn") is not None else combat.get("turn"))

    def _safe_int(self, value: Any) -> int:
        try:
            if value is None:
                return 0
            return int(value)
        except Exception:
            return 0

    def _update_run_intent(self) -> None:
        snapshot = self._state.snapshot if isinstance(self._state.snapshot, dict) else {}
        summary_context = snapshot.get("summary_context") if isinstance(snapshot.get("summary_context"), dict) else {}
        payload = summary_context.get("payload") if isinstance(summary_context.get("payload"), dict) else {}
        strategy_context = snapshot.get("strategy_context") if isinstance(snapshot.get("strategy_context"), dict) else {}
        hp = payload.get("current_hp")
        max_hp = payload.get("max_hp")
        hp_ratio = 0.0
        try:
            if max_hp not in (None, 0):
                hp_ratio = float(hp or 0) / float(max_hp)
        except Exception:
            hp_ratio = 0.0
        run_intent = {
            "strategy_name": strategy_context.get("strategy_name"),
            "screen": snapshot.get("screen"),
            "screen_class": snapshot.get("classification", {}).get("screen_class") if isinstance(snapshot.get("classification"), dict) else None,
            "hp_ratio": hp_ratio,
            "risk_posture": "preserve_hp" if hp_ratio < 0.45 else "growth",
            "floor": snapshot.get("floor"),
            "act": snapshot.get("act"),
        }
        self._state.run_intent = run_intent
        self._remember_hp_sample(snapshot)
        self._remember_route_step(snapshot)
        journal = self._companion_session.journal
        if journal.data.get("run_id") == snapshot.get("run_id"):
            telemetry = {name: deepcopy(getattr(self._state, name)) for name in
                         ("hp_trail", "combat_room_id", "route_log", "route_room_key", "route_start", "route_last")}
            samples = journal.data.setdefault("hp_samples", [])
            latest = (telemetry["hp_trail"] or [None])[-1]
            if latest and (not samples or latest != samples[-1]):
                samples.append(deepcopy(latest))
            if journal.data.get("telemetry") != telemetry:
                journal.data["telemetry"] = telemetry
                journal.save()
        self._maybe_announce_character(snapshot)
        self._maybe_record_run_memory(snapshot)

    @staticmethod
    def _room_identity(screen: str, raw_state: dict[str, Any]) -> str:
        """这一屏的**稳定标识**，用于跨局复盘。

        玩家口径：「尖塔的地图是随机的，同样的层可能是不同的怪，所以（只记层数）完全没有用，
        应该是记对应房间的 id」。mod 实际能给的是：

        - 战斗：`combat.enemies[].enemy_id`（这一场的敌人组合，如 `LEAF_SLIME_M+LEAF_SLIME_S`）
        - 事件：`event.event_id`
        - 篝火/商店/宝箱：**没有 id**，只能用房间类型兜底（`room:REST` 之类）

        注意 mod 没有 encounter/room id，所以战斗用"敌人组合"当遭遇标识 —— 这已经足够区分
        "同一层遇到的第 3 层史莱姆"和"第 3 层的精英"。
        """
        combat = raw_state.get("combat") if isinstance(raw_state.get("combat"), dict) else {}
        enemies = combat.get("enemies") if isinstance(combat.get("enemies"), list) else []
        ids = sorted(
            {
                str(enemy.get("enemy_id") or "")
                for enemy in enemies
                if isinstance(enemy, dict) and enemy.get("enemy_id") and enemy.get("is_alive", True)
            }
        )
        if ids:
            return "+".join(ids)
        event = raw_state.get("event") if isinstance(raw_state.get("event"), dict) else {}
        event_id = str(event.get("event_id") or "")
        if event_id:
            return event_id
        return f"room:{screen or 'unknown'}" if screen else ""

    def _remember_hp_sample(self, snapshot: dict[str, Any]) -> None:
        """记一条生命采样，供局末做"哪里损血"的归因（HP 或界面没变就不重复记）。"""
        if snapshot.get("outcome") == "victory":
            return
        try:
            raw_state = snapshot.get("raw_state") if isinstance(snapshot.get("raw_state"), dict) else {}
            combat = raw_state.get("combat") if isinstance(raw_state.get("combat"), dict) else {}
            player = combat.get("player") if isinstance(combat.get("player"), dict) else {}
            run = raw_state.get("run") if isinstance(raw_state.get("run"), dict) else {}
            hp = player.get("current_hp")
            if not isinstance(hp, (int, float)):
                hp = run.get("current_hp")
            if not isinstance(hp, (int, float)):
                return
            max_hp = player.get("max_hp")
            if not isinstance(max_hp, (int, float)):
                max_hp = run.get("max_hp")
            run_id = str(snapshot.get("run_id") or "")
            if run_id and run_id != self._state.hp_trail_run_id:
                # 换局了：轨迹清零重新记（否则上一局的损血会算到这一局头上）。
                self._state.hp_trail_run_id = run_id
                self._state.hp_trail = []
                self._state.combat_room_id = ""
            screen = str(snapshot.get("screen") or "")
            in_combat = bool(snapshot.get("in_combat"))
            if in_combat:
                # 一场战斗的标识取**遭遇开始时**的敌人组合：战斗中敌人会死，若每帧重取，
                # 同一场战斗会被拆成好几个"房间"，归因就散了。
                if not self._state.combat_room_id:
                    self._state.combat_room_id = self._room_identity(screen, raw_state)
                room = self._state.combat_room_id
            else:
                self._state.combat_room_id = ""
                room = self._room_identity(screen, raw_state)
            sample = {
                "hp": int(hp),
                "max_hp": int(max_hp or 0),
                "screen": screen,
                "room": room,
                "floor": self._safe_int(snapshot.get("floor")) or 0,
                "step_count": int(self._state.step_count),
                "at": time(),
                "sampling_gap": self._telemetry_gap,
            }
            trail = self._state.hp_trail
            if trail and trail[-1].get("hp") == sample["hp"] and trail[-1].get("room") == sample["room"] and not self._telemetry_gap:
                return
            trail.append(sample)
            self._telemetry_gap = False
            if len(trail) > 400:
                del trail[: len(trail) - 400]
        except Exception:
            pass

    # 奖励类界面属于**上一个房间**（战斗/事件打完才发奖），不另起一段路线。
    _REWARD_LIKE_SCREENS = frozenset({"REWARD", "CARD_SELECTION_REWARD", "CARDS_VIEW"})
    # 路线图最多记多少个房间（防极端情况膨胀）。
    _ROUTE_MAX_ROOMS = 80

    def _run_snapshot(self, raw_state: dict[str, Any]) -> dict[str, Any]:
        """把"这一个房间的进入/离开状态"压缩成可比较的一组值（算收获用）。"""
        run = raw_state.get("run") if isinstance(raw_state.get("run"), dict) else {}
        combat = raw_state.get("combat") if isinstance(raw_state.get("combat"), dict) else {}
        player = combat.get("player") if isinstance(combat.get("player"), dict) else {}
        hp = player.get("current_hp")
        if not isinstance(hp, (int, float)):
            hp = run.get("current_hp")
        deck = run.get("deck") if isinstance(run.get("deck"), list) else []
        relics = run.get("relics") if isinstance(run.get("relics"), list) else []
        potions = run.get("potions") if isinstance(run.get("potions"), list) else []
        return {
            "hp": int(hp) if isinstance(hp, (int, float)) else None,
            "gold": self._safe_int(run.get("gold")) or 0,
            "deck": sorted(str(card.get("card_id") or "") for card in deck if isinstance(card, dict)),
            "relics": sorted(str(relic.get("relic_id") or relic.get("name") or "") for relic in relics if isinstance(relic, dict)),
            "potions": sorted(
                str(potion.get("potion_id") or "")
                for potion in potions
                if isinstance(potion, dict) and potion.get("potion_id")
            ),
        }

    def _remember_route_step(self, snapshot: dict[str, Any]) -> None:
        """逐房间记路线图：**每个房间掉多少血、拿到什么**。

        玩家口径：「需要路线图，对应每个房间的损血和收获」。判定与记法：

        - 房间键 = `层|界面|房间标识`；奖励类界面（REWARD / CARD_SELECTION_REWARD）不另起一段 ——
          奖励是"这个房间打完之后"发的，算进同一个房间才看得懂"这房间值不值"。
        - 损血 = 进房间时的血 - 记录过程中最低的血；收获 = 金币差 + 新增卡/遗物/药水（按 id 差集）。
        - 换房间、换局、局末时收尾。
        """
        if snapshot.get("outcome") == "victory":
            return
        try:
            raw_state = snapshot.get("raw_state") if isinstance(snapshot.get("raw_state"), dict) else {}
            screen = str(snapshot.get("screen") or "")
            if not screen or screen.upper() in {"MAIN_MENU", "UNKNOWN"}:
                return
            run_id = str(snapshot.get("run_id") or "")
            if run_id and run_id != self._state.hp_trail_run_id and self._state.hp_trail_run_id:
                # 换局（hp_trail 已先一步重置）：路线图也重来。
                self._state.route_log = []
                self._state.route_room_key = ""
                self._state.route_start = {}
            current = self._run_snapshot(raw_state)
            room = self._room_identity(screen, raw_state)
            floor = self._safe_int(snapshot.get("floor")) or 0
            previous = self._state.route_last if isinstance(self._state.route_last, dict) else {}
            merging = screen.upper() in self._REWARD_LIKE_SCREENS and bool(self._state.route_room_key)
            if merging:
                # 奖励是"这个房间打完之后"发的：并入上一个房间，且**保留原房间的身份**
                # （否则这段会被记成 room:REWARD，路线图上就看不出是打哪一场拿到的）。
                key = self._state.route_room_key
                current["room"] = previous.get("room") or room
                current["screen"] = previous.get("screen") or screen
                current["floor"] = previous.get("floor", floor)
            else:
                key = f"{floor}|{screen}|{room}"
                current["room"] = room
                current["screen"] = screen
                current["floor"] = floor
            if key != self._state.route_room_key:
                self._close_route_room(reason="next_room")
                self._state.route_room_key = key
                self._state.route_start = dict(current)
            hp = current.get("hp")
            lowest = current.get("lowest_hp") if "lowest_hp" in current else previous.get("lowest_hp")
            if isinstance(hp, (int, float)):
                lowest = int(hp) if not isinstance(lowest, (int, float)) else min(int(lowest), int(hp))
            current["lowest_hp"] = lowest
            self._state.route_last = current
        except Exception as exc:
            self.logger.info("[sts2_route] step failed (non-fatal): %s", exc)

    def _close_route_room(self, *, reason: str) -> None:
        """收尾当前房间，把损血与收获写进路线图。"""
        start = self._state.route_start if isinstance(self._state.route_start, dict) else {}
        last = self._state.route_last if isinstance(self._state.route_last, dict) else {}
        if not start or not last:
            return
        start_hp = start.get("hp")
        lowest = last.get("lowest_hp")
        hp_lost = 0
        if isinstance(start_hp, (int, float)) and isinstance(lowest, (int, float)):
            hp_lost = max(0, int(start_hp) - int(lowest))
        new_cards = sorted(set(last.get("deck") or []) - set(start.get("deck") or []))
        new_relics = sorted(set(last.get("relics") or []) - set(start.get("relics") or []))
        new_potions = sorted(set(last.get("potions") or []) - set(start.get("potions") or []))
        entry = {
            "room": str(last.get("room") or ""),
            "screen": str(last.get("screen") or ""),
            "floor": last.get("floor"),
            "hp_start": start_hp,
            "hp_end": last.get("hp"),
            "lowest_hp": lowest,
            "hp_lost": hp_lost,
            "gold_gained": max(0, int(last.get("gold") or 0) - int(start.get("gold") or 0)),
            "cards_gained": new_cards,
            "relics_gained": new_relics,
            "potions_gained": new_potions,
            "ended_by": reason,
        }
        # 什么都没发生的空段（例如地图上走一格）不记，免得路线图全是噪声；
        # 其它房间即使零损血零收获也保留，这样路线是完整的。
        if hp_lost == 0 and entry["gold_gained"] == 0 and not (new_cards or new_relics or new_potions):
            if str(last.get("screen") or "") == "MAP":
                return
        self._state.route_log.append(entry)
        if len(self._state.route_log) > self._ROUTE_MAX_ROOMS:
            del self._state.route_log[: len(self._state.route_log) - self._ROUTE_MAX_ROOMS]

    def _hp_loss_report(self) -> dict[str, Any]:
        """把本局生命轨迹整理成"哪里损血"的归因表（局末总结用）。"""
        archived = self._companion_session.journal.data.get("hp_samples") or self._state.hp_trail
        trail = [s for s in archived if isinstance(s, dict)]
        if not trail:
            return {}
        losses: list[dict[str, Any]] = []
        for prev, cur in zip(trail, trail[1:]):
            try:
                delta = int(cur.get("hp") or 0) - int(prev.get("hp") or 0)
            except (TypeError, ValueError):
                continue
            if delta < 0:
                losses.append(
                    {
                        # 归因按**房间标识**（敌人组合 / 事件 id），不是层数 —— 尖塔地图随机，
                        # "第 27 层"在不同局根本不是同一个房间，那样的复盘没法复用。
                        "room": str(cur.get("room") or ""),
                        "screen": str(cur.get("screen") or ""),
                        "floor": cur.get("floor"),
                        "hp_before": prev.get("hp"),
                        "hp_after": cur.get("hp"),
                        "lost": -delta,
                        "at_step": cur.get("step_count"),
                        "sampling_gap": cur.get("sampling_gap", False),
                        "attribution": "same_room_interval" if prev.get("room") == cur.get("room") else "cross_room_interval",
                    }
                )
        hp_values = []
        for sample in trail:
            try:
                hp_values.append(int(sample.get("hp") or 0))
            except (TypeError, ValueError):
                continue
        max_hp = 0
        for sample in trail:
            try:
                max_hp = max(max_hp, int(sample.get("max_hp") or 0))
            except (TypeError, ValueError):
                continue
        return {
            "samples": len(trail),
            "max_hp": max_hp,
            "min_hp": min(hp_values) if hp_values else 0,
            "final_hp": hp_values[-1] if hp_values else 0,
            "total_lost": sum(int(item["lost"]) for item in losses),
            "loss_events": len(losses),
            "heaviest": sorted(losses, key=lambda item: -int(item["lost"]))[:3],
        }

    def _maybe_announce_character(self, snapshot: dict[str, Any]) -> None:
        """把本局使用的游戏角色交给本体，由本体决定是否开口。"""
        try:
            run_id = str(snapshot.get("run_id") or "")
            if not run_id or run_id in {"run_unknown"}:
                return
            if self._state.announced_character_run_id == run_id:
                return
            raw_state = snapshot.get("raw_state") if isinstance(snapshot.get("raw_state"), dict) else {}
            run = raw_state.get("run") if isinstance(raw_state.get("run"), dict) else {}
            name = str(run.get("character_name") or "")
            character_id = str(run.get("character_id") or "")
            display = name or character_id
            if not display:
                return
            self._state.announced_character_run_id = run_id
            if getattr(self._state, "host_digest_enabled", True):
                self._push_proactive_to_host(
                    f"《杀戮尖塔2》新的一局开始了，你使用的游戏角色是「{display}」。"
                    "要不要就此说一句由你判断。",
                    kind="character_announcement",
                    priority=6,
                )
            self.logger.info("[sts2_character] 本局角色=%s run_id=%s", display, run_id)
        except Exception as exc:
            self.logger.info("[sts2_character] announce failed (non-fatal): %s", exc)

    def _maybe_record_run_memory(self, snapshot: dict[str, Any]) -> None:
        """run 结束（死亡/通关）时，把本局决策痕迹总结成教训/偏好写进猫娘记忆。

        terminal 屏会反复出现在快照里，因此只在 run_id 与上次写入不同时才总结（幂等）。
        """
        try:
            classification = snapshot.get("classification") if isinstance(snapshot.get("classification"), dict) else {}
            if classification.get("screen_class") != "terminal":
                return
            screen = str(snapshot.get("screen") or "").strip().lower()
            # 仅认这三种终点屏；其余 modal 等误报 terminal 的跳过
            if screen not in {"game_over", "victory", "defeat"}:
                return
            run_id = str(snapshot.get("run_id") or "")
            if not run_id or run_id == self._state.last_memory_run_id:
                return
            outcome = snapshot.get("outcome") or {"victory": "victory", "defeat": "defeat"}.get(screen)
            if outcome is None:
                return
            self._state.last_memory_run_id = run_id
            self.logger.info("[sts2_run_result] run_id=%s screen=%s outcome=%s raw_victory=%s terminal_hp=%s",
                             run_id, screen, outcome,
                             (snapshot.get("raw_state", {}).get("game_over") or {}).get("is_victory"),
                             (snapshot.get("raw_state", {}).get("run") or {}).get("current_hp"))
            saved_record = next((r for r in self._catgirl_memory.runs if r.get("run_id") == run_id), None)
            if saved_record and saved_record.get("outcome") == outcome:
                self.logger.info("[sts2_memory] retained existing run_id=%s outcome=%s decisions=%s", run_id,
                                 outcome, len(saved_record.get("decisions") or []))
                self._write_history_view()
                return
            raw_state = snapshot.get("raw_state") if isinstance(snapshot.get("raw_state"), dict) else {}
            run = raw_state.get("run") if isinstance(raw_state.get("run"), dict) else {}
            character = str(snapshot.get("character") or raw_state.get("character_id") or run.get("character_id") or "")
            floor = int(snapshot.get("floor") or 0)
            act = int(snapshot.get("act") or 0)
            # 事实部分立刻落盘（同步、确定）；自然语言反思稍后异步补（见 _reflect_run_end）。
            hp_report = self._hp_loss_report()
            # 收尾最后一个房间，把路线图一起写进本局记录。
            self._close_route_room(reason="run_end")
            route = [dict(item) for item in self._state.route_log if isinstance(item, dict)]
            if self._companion_session.journal.data:
                self._companion_session.journal.data.setdefault("telemetry", {})["route_log"] = deepcopy(route)
                self._companion_session.journal.save()
            record = self._catgirl_memory.add_run(
                run_id,
                character=character,
                outcome=outcome,
                floor=floor,
                act=act,
                decision_memory=self._state.recent_decision_memory,
                hp_report=hp_report,
                route=route,
                process=self._companion_session.journal.run_context(),
            )
            hp_report = record.get("hp") or {}
            self._state.route_log = []
            self._state.route_room_key = ""
            self._state.route_start = {}
            self._state.route_last = {}
            self.logger.info(
                "[sts2_memory] recorded run end run_id=%s outcome=%s floor=%s character=%s 总损血=%s(%s次) 最低血=%s lesson_count=%s pref_count=%s",
                run_id,
                outcome,
                floor,
                character,
                hp_report.get("total_lost"),
                hp_report.get("loss_events"),
                hp_report.get("min_hp"),
                len(record.get("lessons", [])),
                len(record.get("preferences", [])),
            )
            self._write_history_view()
            if self._ingress_active:
                if self._state.run_reflection_enabled:
                    self._push_proactive_to_host(self._run_summary_digest({k: v for k, v in record.items() if k != "process"}, hp_report),
                                                 kind="run_reflection", priority=7)
                return
            # 复盘开关关掉时：只留结构化记录，既不暂停也不生成反思（连"暂停再恢复"这套都不做）。
            if not getattr(self._state, "run_reflection_enabled", True):
                self.logger.info("[sts2_reflect] 复盘开关关闭，只记录不反思")
                return
            # 复盘期间**暂停自动游玩**（玩家口径："复盘的时候暂停自动游玩，复盘完再继续"）——
            # 否则猫娘会在复盘还没讲完时就冲进主菜单开下一局。
            self._pause_for_reflection()
            # 异步：让**本体**（同一模型 + 本体人设）做一次复盘，落盘并说给玩家听。
            try:
                loop = asyncio.get_running_loop()
                task = loop.create_task(self._reflect_run_end(dict(record), hp_report))
                task.add_done_callback(lambda t: t.exception() if not t.cancelled() else None)
            except RuntimeError:
                self._resume_after_reflection()
        except Exception as exc:
            self.logger.info("[sts2_memory] record run end failed (non-fatal): %s", exc)

    def _write_history_view(self) -> None:
        """把历史投影成插件 static 目录下的 JSON，供前端"自动游玩历史"页读取。

        前端页面是插件服务器按静态文件发的，没有直读插件内存的通道，所以走"落一份视图快照"。
        权威数据仍在 catgirl_memory.json；这里失败不影响任何决策。
        """
        try:
            path = write_history_view(self._catgirl_memory.runs, learning_report=self._catgirl_memory.learning_report(),
                                      current_report=self._companion_session.journal.report())
            if path:
                self.logger.info("[sts2_history] 历史快照已更新: %s", path)
        except Exception as exc:
            self.logger.info("[sts2_history] 写历史快照失败 (non-fatal): %s", exc)

    def get_history_payload(self) -> dict[str, Any]:
        """历史页/入口用的历史数据（直接从猫娘记忆投影，不读快照文件）。"""
        try:
            from .history_view import build_history_payload

            return build_history_payload(self._catgirl_memory.runs, learning_report=self._catgirl_memory.learning_report(),
                                         current_report=self._companion_session.journal.report())
        except Exception:
            return {"summary": {}, "runs": []}

    def _run_summary_digest(self, record: dict[str, Any], hp_report: dict[str, Any]) -> str:
        """局末战报（**简化后的提示词**）：结局 + 角色 + 层级 + 损血归因 + 关键决策。

        损血归因就是玩家要的"看看哪些地方损血导致失败"：把单次掉血最多的三处列出来，
        带上是哪个界面、第几层、掉了多少。
        """
        parts = [
            f"结局:{record.get('outcome') or '?'}",
            f"角色:{record.get('character') or '?'}",
            f"打到第{record.get('floor') or 0}层",
        ]
        if hp_report:
            parts.append(f"共损血{hp_report.get('total_lost')}点({hp_report.get('loss_events')}次)")
            parts.append(f"最低血{hp_report.get('min_hp')}/{hp_report.get('max_hp')}")
            for item in hp_report.get("heaviest") or []:
                parts.append(
                    f"最痛:「{item.get('room') or item.get('screen')}」-{item.get('lost')}"
                    f"({item.get('hp_before')}->{item.get('hp_after')})"
                )
        decisions = [d for d in (record.get("decisions") or []) if isinstance(d, dict)]
        if decisions:
            recent = decisions[-8:]
            parts.append(
                "近况:"
                + "；".join(f"{str(d.get('screen') or '')}:{str(d.get('action_type') or '')}" for d in recent)
            )
        process = record.get("process") or {}
        if process.get("turns"):
            return json.dumps({"result": parts, "process": self._companion_session.journal.reflection_context(process)},
                              ensure_ascii=False, separators=(",", ":"))
        return "｜".join(parts)[:600]

    def _pause_for_reflection(self) -> None:
        """复盘期间暂停自动游玩，并记下"是我们暂停的"（复盘完才恢复）。"""
        try:
            if self._state.autoplay_state != "running":
                return
            self.pause_autoplay(reason="run_reflection")
            self._state.reflection_paused = True
            self.logger.info("[sts2_reflect] 复盘期间已暂停自动游玩")
        except Exception as exc:
            self.logger.info("[sts2_reflect] pause failed (non-fatal): %s", exc)

    def _resume_after_reflection(self) -> None:
        """复盘结束后恢复自动游玩。

        只在"确实是我们为了复盘暂停的、且玩家没在这期间动过档位"时才恢复 —— 玩家手动暂停/切到
        standby 之后被我们擅自恢复，比不恢复更糟。
        """
        if not getattr(self._state, "reflection_paused", False):
            return
        self._state.reflection_paused = False
        try:
            if self._state.standby:
                return
            if self._state.autoplay_state != "paused" or self._state.pause_reason != "run_reflection":
                return
            self.resume_autoplay()
            self.logger.info("[sts2_reflect] 复盘结束，已恢复自动游玩")
        except Exception as exc:
            self.logger.info("[sts2_reflect] resume failed (non-fatal): %s", exc)

    async def _reflect_run_end(self, record: dict[str, Any], hp_report: dict[str, Any]) -> None:
        """局末复盘：让**本体**（同一模型 + 本体人设）指出损血/失败的关键并给出下次的改法。

        产物有三处去处：① 作为局末评价保存；结构化经验走统一决策的learning_updates；
        ② 作为弹幕说给玩家听；③ 推给本体 NEKO 的对话（`ai_behavior="respond"`，让它用自己口吻再说一遍）。
        """
        try:
            if not getattr(self._state, "run_reflection_enabled", True):
                return
            digest = self._run_summary_digest(record, hp_report)
            self.logger.info("[sts2_reflect] digest=%s", digest)
            if not getattr(self._catgirl_llm, "available", False):
                self.logger.info("[sts2_reflect] 主系统 LLM 不可用，保留启发式总结")
                return
            text = await self._catgirl_llm.generate(summary_text=digest, summary_kind="run_summary", payload={})
            if not text:
                self.logger.info("[sts2_reflect] 反思未产出（保留启发式总结）")
                return
            journal = self._companion_session.journal
            source = next((t for t in reversed(journal.data.get("turns", [])) if t.get("actions")), None)
            if source:
                self._catgirl_memory.stage_review({"turn_id": source["id"], "assessment": text, "question": text}, text, journal)
            self.logger.info("[sts2_reflect] 反思=%s", text)
            self._write_history_view()  # 让历史页也能看到这条反思
            self._schedule_catgirl_push(text)
            head = (
                f"这一局结束了（{record.get('outcome') or '?'}，第{record.get('floor') or 0}层）。"
                f"复盘要点：{text}"
                "用你自己的口吻先给判断，再询问主人意见，等反馈后再总结经验。"
            )
            self._push_proactive_to_host(head, kind="run_reflection", priority=7)
        except Exception as exc:
            self.logger.info("[sts2_reflect] failed (non-fatal): %s", exc)
        finally:
            # 无论复盘成功与否都要恢复 —— 卡在"暂停"上等于猫娘永久停手。
            self._resume_after_reflection()

    async def _update_companion_feedback(self, snapshot: dict[str, Any]) -> None:
        if self._feedback_lock.locked():
            return
        raw = snapshot.get("raw_state") or {}
        multiplayer = raw.get("multiplayer") or {}
        if multiplayer.get("is_multiplayer") and str(multiplayer.get("net_game_type") or "").lower() == "host":
            self._feedback_observation = {}
            return
        combat = raw.get("combat") or {}
        player = combat.get("player") or {}
        hp = player.get("current_hp")
        enemies = {e.get("entity_id"): e.get("current_hp") for e in combat.get("enemies", []) if e.get("is_alive", True)}
        observation = {"run": raw.get("run_id"), "floor": snapshot.get("floor"), "owner": multiplayer.get("local_player_id"), "combat": bool(raw.get("in_combat")),
                       "hp": hp, "enemies": enemies, "risk": bool(raw.get("in_combat")) and self._is_high_risk_combat(snapshot, {"summary_kind": "combat", "player": player})}
        previous = self._feedback_observation
        self._feedback_observation = observation
        same_room = previous.get("run") == observation["run"] and previous.get("floor") == observation["floor"] and previous.get("owner") == observation["owner"]
        if not same_room or not observation["risk"]:
            self._feedback_risk_announced = False
        event, detail = "", ""
        if same_room and previous.get("combat") and isinstance(previous.get("hp"), (int, float)) and isinstance(hp, (int, float)) and hp < previous["hp"]:
            event, detail = "hurt", f"猫娘角色刚损失 {previous['hp'] - hp} 点生命，目前 {hp} 点。"
        elif same_room and previous.get("combat") and (not observation["combat"] or set(previous.get("enemies", {})) - set(enemies)):
            event, detail = "success", "战斗结束或有敌人被击败。"
        elif observation["risk"] and not self._feedback_risk_announced:
            event, detail = "danger", f"当前生命 {hp}；敌人意图或低生命触发危险提醒。"
            self._feedback_risk_announced = True
        elif previous.get("risk") and not observation["risk"]:
            event, detail = "thinking", "危险已缓解，继续观察局面。"
        if event:
            await self._emit_companion_feedback(event, detail)

    async def _emit_companion_feedback(self, event: str, detail: str, *, speak: bool = True) -> None:
        if self._sdk_ctx is None and self._frontend_notifier is None:
            return
        if not self._feedback_lock.acquire(blocking=False):
            return
        try:
            if self._feedback_last_event == event and time() - self._feedback_result.get("at", 0) < 8:
                return
            self._feedback_last_event = event
            context = {
                "persona": self._catgirl_llm._persona_system(),
                "event_fact": detail,
                "agent_view": self._agent_view(self._state.snapshot),
                "session": self._companion_session.context(),
                "recent_action": self._state.latest_action_frame,
            }
            analysis_text = (
                "结合当前角色人格、最近对话、行动理由和实际得失判断情绪；没有明显情绪时保持平静。\n"
                + json.dumps(context, ensure_ascii=False)
            )
            self._feedback_result = {"event": event, "detail": detail, "at": time(), "delivery": "pending"}
            try:
                result = await apply_existing_expression(self._main_server_port(), analysis_text)
            except Exception as exc:
                # 表情服务不可用时保持游戏控制，明确记录失败，语音仍可解释事件。
                self._feedback_result.update(delivery="failed", error=str(exc))
                self.logger.warning("[sts2_emotion] event=%s failed=%s", event, exc)
            else:
                self._feedback_result.update(result, delivery="host_emotion_queued")
                self.logger.info("[sts2_emotion] event=%s character=%s emotion=%s", event, result["character"], result["emotion"])
            if (speak and event != "thinking" and self._state.host_digest_enabled
                    and not (self._ingress_active and self._companion_session.facts.get("screen") in {"GAME_OVER", "VICTORY", "DEFEAT"})
                    and self._state.autoplay_state != "running" and not self._wait_for_turn_message):
                self._push_proactive_to_host(
                    detail if self._ingress_active else "游戏经历上下文：" + json.dumps(context, ensure_ascii=False)
                    + "\n按实际感受决定是否回应。",
                    kind="combat_feedback", priority=7,
                )
                self._feedback_voice_at = time()
                self._state.last_host_digest_at = time()
                self._feedback_result["voice"] = "submitted"
            else:
                self._feedback_result["voice"] = "current_dialogue" if self._state.discussion_active else "silent"
        finally:
            self._feedback_lock.release()

    def _push_proactive_to_host(self, text: str, *, kind: str, priority: int = 5,
                                prompt_ephemeral: bool = False,
                                coalesce_key: str | None = None) -> None:
        """交回 N.E.K.O 本体表达；回复沿用主人格，保留会话历史。"""
        body = str(text or "").strip()
        if not body:
            return
        if self._ingress_active:
            self._ingress_callbacks.append({"kind": kind, "content": body, "timestamp": time()})
            return
        meta = {"kind": kind, "prompt_ephemeral": prompt_ephemeral, "delivery_semantics": "proactive"}
        self.logger.info(
            "[sts2_host_push] kind=%s visibility=[] ai_behavior=respond ephemeral=%s chars=%s",
            kind,
            prompt_ephemeral,
            len(body),
        )
        ctx = self._sdk_ctx
        if ctx is not None and hasattr(ctx, "push_message"):
            try:
                ctx.push_message(
                    source="sts2_autoplay",
                    visibility=[],
                    ai_behavior="respond",
                    parts=[{"type": "text", "text": body}],
                    metadata=dict(meta),
                    priority=priority,
                    **({"coalesce_key": coalesce_key} if coalesce_key else
                       ({"coalesce_key": "sts2:combat_feedback"} if kind == "combat_feedback" else {})),
                )
                return
            except Exception as exc:
                self.logger.info("[sts2_host_push] sdk push failed (non-fatal): %s", exc)
        notifier = self._frontend_notifier
        if notifier is not None:
            try:
                notifier(
                    content=body,
                    description=body,
                    metadata=dict(meta),
                    priority=priority,
                    message_type=f"sts2_{kind}",
                    visibility=[],
                    ai_behavior="respond",
                )
            except Exception:
                pass

    def _push_host_context(self, text: str, *, coalesce_key: str) -> None:
        """把**被动局面上下文**交给本体：只进 LLM 上下文，绝不触发它开口。

        `ai_behavior="read"` 在宿主映射为 `delivery_mode="passive"`——只入队，等本体
        下一个自然轮次（用户开口）才 drain 进上下文，不打断、不抢话。这正是自动行动
        期间缺的那一路：`_deliver_catgirl_sync` 的说话轨在 catgirl 自主行动时被压住，
        若不单独喂局面，本体对"现在什么情况"只能拿旧画面作答。
        """
        body = str(text or "").strip()
        if not body:
            return
        meta = {"kind": "host_context", "delivery_semantics": "passive"}
        self.logger.info(
            "[sts2_host_push] kind=host_context ai_behavior=read coalesce_key=%s chars=%s",
            coalesce_key,
            len(body),
        )
        ctx = self._sdk_ctx
        if ctx is not None and hasattr(ctx, "push_message"):
            try:
                ctx.push_message(
                    source="sts2_autoplay",
                    visibility=[],
                    ai_behavior="read",
                    parts=[{"type": "text", "text": body}],
                    metadata=dict(meta),
                    priority=3,
                    coalesce_key=coalesce_key,
                )
                return
            except Exception as exc:
                self.logger.info("[sts2_host_push] sdk context push failed (non-fatal): %s", exc)
        notifier = self._frontend_notifier
        if notifier is not None:
            try:
                notifier(
                    content=body,
                    description=body,
                    metadata=dict(meta),
                    priority=3,
                    message_type="sts2_host_context",
                    visibility=[],
                    ai_behavior="read",
                )
            except Exception:
                pass

    def _deliver_host_context(self, snapshot: dict[str, Any]) -> None:
        if self._ingress_active:
            return  # 同伴决策直接读取当前事实，无需给普通聊天反复塞动态摘要。
        """自动行动期间把**当前局面**被动喂给本体，修正"本体还在按旧画面说话"。

        说话轨（`_deliver_catgirl_sync`）在 `autoplay_state == "running"` 时整段早退，
        等待本回合用户话语时也早退——两处都是为了"别抢话"。但连同局面上下文一起
        断掉就错了：玩家在自动行动中途开口问"现在什么情况"，本体手里还是上一屏的
        快照（实测差着好几分钟、还停在 EVENT 界面）。这里补上被动上下文轨：
        同一 `agent_view` 只喂一次（指纹去重），宿主侧同 key 合并为最新一条，最坏
        只是本体多一份新上下文，绝不会因此开口。
        """
        if not getattr(self._state, "host_digest_enabled", True):
            return
        # 只在说话轨被压住的两个窗口补位，避免和说话轨重复喂：
        #   ① 自动行动中（autoplay_state == "running"）；
        #   ② 战斗中正在等本回合用户话语（_wait_for_turn_message + battle anchor）。
        suppressed = self._state.autoplay_state == "running" or bool(
            self._wait_for_turn_message and self._companion_session.anchor().get("battle_id")
        )
        if not suppressed:
            return  # 说话轨本身会带上下文，不重复喂。
        view = self._agent_view(snapshot)
        if not view:
            return
        decision = self._companion_session.decision_state()
        active = decision["active_plan"]
        key = view_signature({"view": view, "revision": active.get("revision"), "status": active.get("status"),
                              "remaining": [s.get("step_id") for s in active["remaining_steps"]],
                              "action_sequence": decision["action_sequence"],
                              "announced": decision["announced_plan"], "replacement_reason": decision["replacement_reason"]})
        if key == self._state.last_host_context_key:
            return
        text = self._host_context_text(snapshot, view)
        if not text:
            return
        self._state.last_host_context_key = key
        self._push_host_context(text, coalesce_key="sts2:host_context")

    def _host_context_text(self, snapshot: dict[str, Any], view: dict[str, Any]) -> str:
        """把当前局面压成**紧凑速报**（宿主单条被动回调上限 1000 token）。

        不能原样塞 agent_view/agent_facts/session：整包实测 28KB、峰值 48KB，而宿主
        `_normalize_context_text_for_source` 会把一条 callback 截到 1000 token——
        截在 JSON 中间既浪费带宽，本体读到的还是半截。这里只挑当下问题的答案：
        在哪一屏、哪一层、轮到谁、血量、敌人意图/buff、自己的 buff 与充能球（含效果文本）。
        """
        bits: list[str] = []
        screen = str(view.get("screen") or snapshot.get("screen") or "unknown")
        floor = snapshot.get("floor") or view.get("floor")
        act = snapshot.get("act") or view.get("act")
        if act:
            bits.append(f"第{act}幕")
        if floor:
            bits.append(f"第{floor}层")
        bits.append(f"界面={screen}")
        if screen == "COMBAT":
            turn = snapshot.get("turn") or view.get("turn")
            if turn is not None:
                bits.append(f"回合{turn}")
            combat = view.get("combat") if isinstance(view.get("combat"), dict) else {}
            player = combat.get("player") if isinstance(combat.get("player"), dict) else {}
            hp, max_hp = player.get("current_hp"), player.get("max_hp")
            if isinstance(hp, (int, float)):
                bits.append(f"我方生命{int(hp)}/{int(max_hp or 0)}")
            if player.get("block"):
                bits.append(f"格挡{player['block']}")
            if isinstance(player.get("energy"), (int, float)):
                bits.append(f"能量{player['energy']}")
        head = "，".join(bits)
        facts = build_agent_facts(snapshot.get("raw_state"))
        details: list[str] = []
        if facts.get("player_powers"):
            details.append("我方效果：" + "、".join(str(item) for item in facts["player_powers"]))
        if facts.get("orbs"):
            orbs = []
            for orb in facts["orbs"]:
                effect = orb.get("effect")
                orbs.append(f"{orb.get('name')}（{effect}）" if effect else str(orb.get("name")))
            details.append("充能球：" + "、".join(orbs))
        for enemy in facts.get("enemies") or []:
            name = str(enemy.get("name") or f"敌人{enemy.get('index')}")
            part = name
            if enemy.get("intent"):
                part += f" 意图={enemy['intent']}"
            if enemy.get("powers"):
                part += " 效果=" + "、".join(str(item) for item in enemy["powers"])
            details.append(part)
        if facts.get("potions"):
            usable = [str(p.get("name")) for p in facts["potions"] if p.get("usable")]
            if usable:
                details.append("可用药水：" + "、".join(usable))
        body = (
            "游戏任务上下文（被动同步，仅供你了解当前局面，不必回应）："
            "你正在操作自己的联机进程，以下是此刻的局势速报，宿主旧聊天只属历史。\n"
            f"{head}"
        )
        decision = self._companion_session.decision_state()
        plan = decision["active_plan"]
        if plan.get("revision"):
            labels = []
            for step in plan["remaining_steps"][:5]:
                d = step["description"]
                label = d["card"].split("：", 1)[0][:45] or d["action_type"] + str(d["kwargs"])
                if d["target"]:
                    label += "→" + d["target"][:20]
                labels.append(label)
            body += f"\n计划版本={plan['revision']}，状态={plan['status']}，剩余打算=" + ("；".join(labels) or "无")
        recent = [a for a in self._companion_session.actions
                  if a.get("run_id") == view.get("run_id") and a.get("turn") == view.get("turn")][-3:]
        if recent:
            labels = []
            for action in recent:
                card = action.get("card") or {}
                label = str(card.get("line") or card.get("name") or action["action_type"]).split("：", 1)[0][:45]
                labels.append(f"{label}（{action['status']}）")
            body += "\n最近动作：" + "；".join(labels)
        announced = decision["announced_plan"]
        if announced:
            body += f"\n已说出的计划版本={announced['revision']}：{announced['text'][:120]}"
        if decision["replacement_reason"]:
            body += "\n旧计划取消原因：" + decision["replacement_reason"][:120]
        body += "\n剩余计划是打算；pending 是等待完成，completed 才是完成。"
        if details:
            body += "\n" + "；".join(details)
        return body

    def _schedule_current_proactive_push(self, snapshot: dict[str, Any], body: str, *,
                                         kind: str, priority: int) -> None:
        """只把仍对应当前 agent_view 的主动观测交给宿主。"""
        try:
            loop = asyncio.get_running_loop()
            task = loop.create_task(self._push_current_proactive(snapshot, body, kind=kind, priority=priority))
            task.add_done_callback(lambda t: t.exception() if not t.cancelled() else None)
        except RuntimeError:
            return

    async def _push_current_proactive(self, snapshot: dict[str, Any], body: str, *,
                                      kind: str, priority: int) -> None:
        await asyncio.sleep(0.7)
        if self._state.autoplay_state == "running":
            return
        try:
            client = await self._ensure_companion_client()
            current = await client.get_state()
            expected_view = self._agent_view(snapshot)
            current_view = self._agent_view(current)
            if not expected_view or not current_view or view_signature(expected_view) != view_signature(current_view):
                self.logger.info(
                    "[sts2_host_push] drop stale proactive kind=%s expected=%s current=%s",
                    kind, view_signature(expected_view), view_signature(current_view),
                )
                return
            self._push_proactive_to_host(
                body, kind=kind, priority=priority, prompt_ephemeral=True,
                coalesce_key=f"sts2:proactive:{kind}",
            )
        except Exception as exc:
            self.logger.info("[sts2_host_push] current-state recheck failed kind=%s: %s", kind, exc)

    def _remember_snapshot_metadata(self) -> None:
        snapshot = self._state.snapshot if isinstance(self._state.snapshot, dict) else {}
        payload = f"{snapshot.get('screen')}|{snapshot.get('floor')}|{snapshot.get('act')}|{snapshot.get('in_combat')}|{snapshot.get('available_action_count')}"
        self._state.snapshot_signature = sha1(payload.encode("utf-8")).hexdigest()[:12]
        situation_summary = snapshot.get("situation_summary") if isinstance(snapshot.get("situation_summary"), dict) else {}
        self._state.last_summary = str(situation_summary.get("text") or "")
        planned = snapshot.get("planned_operation") if isinstance(snapshot.get("planned_operation"), dict) else {}
        self._state.last_planner_type = str(planned.get("source") or "")
        agent_operation = snapshot.get("agent_operation") if isinstance(snapshot.get("agent_operation"), dict) else {}
        self._state.last_decision_source = str(agent_operation.get("source") or self._state.last_decision_source)
        self._state.last_decision_reason = str(agent_operation.get("reason") or self._state.last_decision_reason)
        self._state.last_decision_generation = int(agent_operation.get("decision_epoch") or self._state.last_decision_generation)

    # ---- 弹幕双线参数 -------------------------------------------------------------------
    # 高风险判据：血比低于此值，或敌人这一下的意图总伤害 >= 当前生命的一半。
    HIGH_RISK_HP_RATIO = 0.5
    # 拿不到可执行动作持续多久才算"卡住"（要记日志 + 告诉本体）。跨屏切换时短暂拿不到动作是正常的：
    # 实测 2026-10-07 那次是 mod 认不出界面（只剩 save_and_quit），一卡就是两小时四十分。
    NO_OPERATION_STUCK_SECONDS = 20.0
    # 同一个动作 + 同一个局面指纹，重复多少次算"动作没生效"（见 service._note_step_progress）。
    # 取 8 是留足余地：正常动作（打牌/选路）总会改变局面指纹，重复到 8 次一定是不生效了。
    NO_PROGRESS_REPEAT_LIMIT = 8

    def _raw_combat_enemies(self, snapshot: dict[str, Any]) -> list[Any]:
        """`raw_state.combat.enemies`（mod 的权威来源），用来取结构化的 `intents[]`。

        payload 里的 `enemies[].intent` 只有招式 id（念出来是 `STICKY_SHOT`），结构化意图
        （`intent_type` + 伤害）只在 raw_state 里。两边按下标对应：payload 就是按 raw 的顺序
        截前 3 个生成的（`catgirl_bridge`），所以尾部多出来的敌人不影响对齐。
        """
        raw_state = snapshot.get("raw_state") if isinstance(snapshot.get("raw_state"), dict) else {}
        combat = raw_state.get("combat") if isinstance(raw_state.get("combat"), dict) else {}
        enemies = combat.get("enemies")
        return list(enemies) if isinstance(enemies, list) else []

    def _instant_line_key(self, snapshot: dict[str, Any], payload: dict[str, Any]) -> str:
        screen = str(payload.get("screen") or snapshot.get("screen") or "")
        return f"{screen}|{snapshot.get('floor')}|{snapshot.get('turn')}"

    def _schedule_catgirl_push(self, text: str, *, fetch_avatar: bool = True) -> None:
        """把一条文本异步推到游戏内弹幕（fire-and-forget，异常自取）。

        ``fetch_avatar=False`` 用于**即时短句**：拉头像是一次 HTTP（最长 3 秒超时），
        等它回来"即时"就没了 —— 所以即时线只用已缓存的头像，没有就纯文本先发。
        """
        if not text or not self._danmaku_enabled:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        try:
            task = loop.create_task(self._push_catgirl(text, fetch_avatar=fetch_avatar))
            task.add_done_callback(lambda t: t.exception() if not t.cancelled() else None)
        except Exception:
            pass

    def _emit_instant_catgirl_line(self, snapshot: dict[str, Any], companion_evaluation: dict[str, Any], payload: dict[str, Any]) -> None:
        """**即时短句**：不调 LLM，用手里已有的信息拼一句立刻推。

        这是"弹幕跟不上画面"的解药：点评生成平均要 5~10 秒，等它出来早就换场景了；
        即时短句走的是"事件一变就发"，所以它才是玩家看到的主要弹幕。
        """
        try:
            key = self._instant_line_key(snapshot, payload)
        except Exception:
            return
        if not key or key == self._state.last_instant_line_key:
            return
        text = self._instant_line_text(snapshot, companion_evaluation, payload)
        if not text:
            return
        self._state.last_instant_line_key = key
        # 即时线不等待拉头像（那是一次最长 3 秒的 HTTP），只用缓存里的。
        self._schedule_catgirl_push(text, fetch_avatar=False)
        self.logger.info("[sts2_instant_line] key=%s text=%s", key, text[:60])

    def _instant_line_text(self, snapshot: dict[str, Any], companion_evaluation: dict[str, Any], payload: dict[str, Any]) -> str:
        """短句内容：战斗报局势 + 出牌建议（**只读缓存计划**，绝不在这里等 HTTP）；其余界面用启发式文本。"""
        kind = str(payload.get("summary_kind") or "")
        if kind != "combat":
            return str(companion_evaluation.get("primary_message") or payload.get("message") or "").strip()
        bits: list[str] = []
        try:
            plan_line = self._format_plan_line(getattr(self._loop_runner, "_solver_plan_cached", None))
        except Exception:
            plan_line = ""
        if plan_line:
            bits.append(plan_line)
        enemies = payload.get("enemies") if isinstance(payload.get("enemies"), list) else []
        raw_enemies = self._raw_combat_enemies(snapshot)
        enemy_bits = []
        for index, enemy in enumerate(enemies[:3]):
            if not isinstance(enemy, dict):
                continue
            name = str(enemy.get("name") or "敌人")
            # 说人话的意图（`攻击 9`），而不是 mod 的招式 id（`STICKY_SHOT`）——见 `_enemy_intent_label`。
            raw_enemy = raw_enemies[index] if index < len(raw_enemies) else None
            intent = _enemy_intent_label(enemy, raw_enemy)
            enemy_bits.append(f"{name}/{intent}" if intent else name)
        if enemy_bits:
            bits.append("敌:" + "、".join(enemy_bits))
        player = payload.get("player") if isinstance(payload.get("player"), dict) else {}
        if isinstance(player.get("current_hp"), (int, float)):
            bits.append(f"我{int(player['current_hp'])}/{int(player.get('max_hp') or 0)}")
        if not bits:
            return ""
        return "｜".join(bits)

    def _should_emit_long_commentary(self, snapshot: dict[str, Any], payload: dict[str, Any]) -> bool:
        """要不要再让**本体**生成一条长讲解（即时短句之外的第二条线）。

        用户口径："先做高风险，其余短句"。落到实现上是：

        - **战斗**：只在 `_is_high_risk_combat` 为真时讲（血低 / 这一下要挨掉半条命）。
          战斗回合密集，每回合都让推理模型讲一遍既慢又刷屏。
        - **非战斗**（地图/商店/奖励/事件/选牌/营地）：照旧讲。这些界面本来就被
          `should_comment` + 指纹去重挡着，频率很低；而且"讲解局势"主要就发生在这里
          （选哪条路、买什么、拿哪张牌），砍掉的话猫娘就"完全不会讲解了"。
        """
        if str(payload.get("summary_kind") or "") == "combat":
            return self._is_high_risk_combat(snapshot, payload)
        return True

    def _is_high_risk_combat(self, snapshot: dict[str, Any], payload: dict[str, Any]) -> bool:
        """要不要额外出长讲解。用户口径："先做高风险，其余短句"。

        判据：血比 < HIGH_RISK_HP_RATIO，或敌人这一下的意图总伤害 >= 当前生命的一半
        （意图伤害从 raw_state.combat.enemies[].intents[].total_damage 取，这是 mod 的权威来源）。
        """
        if str(payload.get("summary_kind") or "") != "combat":
            return False
        player = payload.get("player") if isinstance(payload.get("player"), dict) else {}
        hp_value = player.get("current_hp")
        max_value = player.get("max_hp")
        if not isinstance(hp_value, (int, float)):
            # 连血量都不知道 -> 判不出风险，那就**讲**。玩家要的是"猫娘会讲解局势"，
            # 这种"拿不准"的场合静默反而更糟（实测 payload 里 player 一直都在，所以线上很少走这里）。
            return True
        ratio = 1.0
        try:
            if float(max_value or 0) > 0:
                ratio = float(hp_value) / float(max_value)
        except (TypeError, ValueError):
            ratio = 1.0
        if ratio < self.HIGH_RISK_HP_RATIO:
            return True
        try:
            raw_state = snapshot.get("raw_state") if isinstance(snapshot.get("raw_state"), dict) else {}
            combat = raw_state.get("combat") if isinstance(raw_state.get("combat"), dict) else {}
            incoming = 0.0
            for enemy in combat.get("enemies") if isinstance(combat.get("enemies"), list) else []:
                if not isinstance(enemy, dict):
                    continue
                for intent in enemy.get("intents") if isinstance(enemy.get("intents"), list) else []:
                    if isinstance(intent, dict):
                        incoming += float(intent.get("total_damage") or 0)
            current_hp = float(hp_value or 0)
            return current_hp > 0 and incoming >= current_hp * 0.5
        except (TypeError, ValueError):
            return False

    # ---- 战斗中用药水（规格 docs/shop_scoring.md 「八」，玩家口径 2026-10-07）----------------
    #
    # 玩家原话：「还有每次对战如果损血大于8就鼓励使用药水」。
    # 决策本身是纯函数（`planner.decide_combat_potion`）；这里负责三件只有服务层能做的事：
    #   1. 只在这一步真的能派发 `use_potion` 时才抢在战斗计划前面（否则白抢一步，还挡掉出牌）；
    #   2. "同一回合只喝一瓶"的记账（runtime_state.potion_used_turn_key，由**真的喝下去**推动）；
    #   3. 一句 [sts2_potion] 日志（去重），让玩家事后能核对"为什么喝/为什么没喝"。

    def _combat_turn_key(self, snapshot: dict[str, Any]) -> str:
        """"本回合"的键：`本局|层|房间|回合`。

        为什么不能只用回合号：两场战斗都从回合 1 开始，上一场喝过药的记账会把这一场也一起挡掉
        （"同一回合只喝一瓶"于是变成"同一场战斗只喝一瓶"）。层 + 房间标识（遭遇开始时的敌人组合）
        一起用就分得开了；读不到就是空串（调用方据此退化成"不记账"，也就是不拦）。
        """
        raw_state = snapshot.get("raw_state") if isinstance(snapshot.get("raw_state"), dict) else {}
        combat = raw_state.get("combat") if isinstance(raw_state.get("combat"), dict) else {}
        turn = raw_state.get("turn")
        if turn is None:
            turn = combat.get("turn")
        if turn is None:
            return ""
        run_id = str(snapshot.get("run_id") or raw_state.get("run_id") or "")
        floor = self._safe_int(snapshot.get("floor")) or 0
        room = str(getattr(self._state, "combat_room_id", "") or "")
        return f"{run_id}|{floor}|{room}|{turn}"

    def _combat_potion_override(self, snapshot: dict[str, Any]) -> dict[str, Any] | None:
        """要不要抢在战斗计划前面先喝一瓶药水；不需要就返回 None（一切照旧）。

        只在**战斗中**、且 `use_potion` 真的在册（= 手里有能用的药水）时才算 —— 否则抢一步只会换来
        "没有合法动作"，还可能把本来要出的那张牌吞掉。注意 mod 的 `use_potion` 对
        `PotionUsage.AnyTime` 的药水**在战斗外也在册**（`IsPotionUsable`），所以必须自己带 `in_combat` 闸门。
        """
        if not bool(snapshot.get("in_combat")):
            return None
        actions = snapshot.get("available_actions") if isinstance(snapshot.get("available_actions"), list) else []
        if not any(str(action.get("type") or "").strip().lower() == "use_potion" for action in actions if isinstance(action, dict)):
            return None
        turn_key = self._combat_turn_key(snapshot)
        already_used = bool(turn_key) and turn_key == str(getattr(self._state, "potion_used_turn_key", "") or "")
        try:
            decision = self._planner.decide_combat_potion(snapshot, already_used_this_turn=already_used)
        except Exception as exc:  # 规则本身出问题绝不该把自动玩拖停
            self.logger.debug(f"[sts2_potion] 用药水决策异常（忽略）: {exc}")
            return None
        self._log_potion_decision(snapshot, turn_key, decision)
        if not decision.get("use"):
            return None
        kwargs: dict[str, Any] = {"option_index": decision.get("option_index")}
        if decision.get("target_index") is not None:
            kwargs["target_index"] = decision.get("target_index")
        return {
            "action_type": "use_potion",
            "kwargs": kwargs,
            "confidence": 0.86,
            "source": "heuristic",
            "reason": "potion_lethal_save" if decision.get("lethal") else "potion_incoming_damage",
        }

    def _log_potion_decision(self, snapshot: dict[str, Any], turn_key: str, decision: dict[str, Any]) -> None:
        """把这一 tick 的用药水判断用 info 级记一次（同一局面同一结论只记一次，避免刷屏）。

        规格「八」要求日志能读出"喝 / 不喝以及原因"：`[sts2_potion] 预计损血=12 > 8 → 用〈减伤药水〉(评分 88)`，
        或 `预计损血=12 但不喝（原因：已在本回合喝过）`。
        """
        text = str(decision.get("log") or "")
        if not text:
            return
        key = f"{turn_key}|{text}"
        if key == str(getattr(self._state, "potion_decision_log_key", "") or ""):
            return
        self._state.potion_decision_log_key = key
        try:
            self.logger.info("[sts2_potion] %s", text)
        except Exception:
            pass

    def _situation_digest_text(self, snapshot: dict[str, Any], payload: dict[str, Any]) -> str:
        """给本体的**局势速报**（它据此主动跟观众讲一句）。

        内容给足才有"互动味"：战斗里报敌人+意图+我方血量，其余界面报界面名与层级。
        以前这里刻意压到 30 字以内 —— 因为 dispatcher 会截断；现在优先走 SDK 的
        `ctx.push_message`（不截断），所以可以写完整。
        """
        kind = str(payload.get("summary_kind") or "")
        screen = str(payload.get("screen") or snapshot.get("screen") or "?")
        floor = snapshot.get("floor")
        head = f"第{floor}层 " if floor else ""
        if kind == "combat":
            player = payload.get("player") if isinstance(payload.get("player"), dict) else {}
            enemies = payload.get("enemies") if isinstance(payload.get("enemies"), list) else []
            bits: list[str] = []
            raw_enemies = self._raw_combat_enemies(snapshot)
            for index, enemy in enumerate(enemies[:3]):
                if not isinstance(enemy, dict):
                    continue
                name = str(enemy.get("name") or "敌人")
                raw_enemy = raw_enemies[index] if index < len(raw_enemies) else None
                intent = _enemy_intent_label(enemy, raw_enemy)
                bits.append(f"{name}({intent})" if intent else name)
            hp = ""
            if isinstance(player.get("current_hp"), (int, float)):
                hp = f"；我方生命{int(player['current_hp'])}/{int(player.get('max_hp') or 0)}"
            return f"{head}战斗中，敌人：" + ("、".join(bits) if bits else "未识别") + hp
        return f"{head}{screen}界面"

    def _push_host_situation_digest(self, snapshot: dict[str, Any], payload: dict[str, Any]) -> None:
        """兼容旧调用入口，统一使用事件驱动的主动交流。"""
        self._deliver_catgirl_sync(snapshot)

    def _maybe_emit_catgirl_llm(self, snapshot: dict[str, Any], companion_evaluation: dict[str, Any], payload: dict[str, Any]) -> None:
        """catgirl 轨道：猫娘 LLM 生成点评（优先）或启发式 primary_message 兜底。

        - 启用 + LLM 可用 + 非生成中 + 距上次生成 ≥ 间隔 → 调度 LLM 生成（生成后推弹幕）
        - 否则推启发式文本（保持即时性，避免无弹幕）
        """
        kind = str(payload.get("summary_kind") or "")
        # 弹幕总闸门：关掉时连 LLM 生成与头像 HTTP 都不做（省一次 N.E.K.O 主 server 请求）。
        # _catgirl_llm_enabled=false 只是把 LLM 点评降级为启发式文本，不是关弹幕——见 danmaku_enabled。
        if not self._danmaku_enabled:
            self.logger.info("[sts2_catgirl_llm] suppressed danmaku disabled kind=%s screen=%s", kind, payload.get("screen"))
            return
        # 弹幕高密度：不再用 should_comment / LLM 生成间隔节流拦截。防刷屏统一交给外层
        # _deliver_catgirl_sync 里的 _should_deliver_sync（指纹+场景/间隔去重）。
        enabled = self._catgirl_llm_enabled
        llm_ok = self._catgirl_llm is not None and self._catgirl_llm.available
        if enabled and llm_ok:
            self.logger.info("[sts2_catgirl_llm] schedule kind=%s screen=%s", kind, payload.get("screen"))
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                self._push_catgirl_fallback(companion_evaluation, payload)
                return
            loop.create_task(self._catgirl_llm_async(snapshot, payload, companion_evaluation, self._instant_line_key(snapshot, payload)))
            return
        self.logger.info("[sts2_catgirl_llm] fallback enabled=%s llm_ok=%s kind=%s", enabled, llm_ok, kind)
        self._push_catgirl_fallback(companion_evaluation, payload)

    @staticmethod
    def _main_server_port() -> int:
        """解析 N.E.K.O 主 server 端口（环境变量优先，默认 48911）。"""
        for key in ("NEKO_MAIN_SERVER_PORT", "MAIN_SERVER_PORT"):
            raw = os.environ.get(key)
            if raw:
                try:
                    port = int(raw)
                    if 0 < port <= 65535:
                        return port
                except ValueError:
                    pass
        return 48911

    def _cached_avatar_base64(self) -> str | None:
        """只用**已缓存**的头像（TTL 内）。即时短句用它，避免为头像等一次 HTTP 而拖慢弹幕。"""
        try:
            if self._avatar_b64 is not None and (time() - self._avatar_at) < 300:
                return self._avatar_b64
        except Exception:
            pass
        return None

    async def _catgirl_avatar_base64(self) -> str | None:
        """取猫娘头像 base64（N.E.K.O 主 server card-drop），TTL 300s 缓存；失败返回上次缓存或 None。"""
        now = time()
        if self._avatar_b64 is not None and now - self._avatar_at < 300:
            return self._avatar_b64
        try:
            import httpx

            url = f"http://127.0.0.1:{self._main_server_port()}/api/card-drop/active-character?include_avatar=true"
            async with httpx.AsyncClient(timeout=3.0) as client:
                resp = await client.get(url)
            payload = resp.json()
            # 48911 card-drop 顶层直接返回 {name, dataUrl}；头像在 dataUrl 字段（可能带 data:...;base64, 前缀），
            # 不是 data 包装里的 avatar。Mod 端 DecodeAvatar.StripDataPrefix 会去掉前缀，直接传即可。
            avatar = payload.get("dataUrl") if isinstance(payload, dict) else None
            if avatar:
                self._avatar_b64 = str(avatar)
                self._avatar_at = now
        except Exception:
            return self._avatar_b64
        return self._avatar_b64

    async def _push_catgirl(self, text: str, *, fetch_avatar: bool = True) -> None:
        """推一条猫娘点评到游戏内弹幕（mod /danmaku），带可选头像。

        这是插件侧唯一的弹幕出口（LLM 分支与启发式兜底都汇到这里），所以 danmaku_enabled 的闸门
        放在这一处即可覆盖全部来源。

        ``fetch_avatar=False``：只用**已缓存**的头像，绝不为它等 HTTP —— 即时短句靠这个保住延迟。
        """
        if not self._danmaku_enabled:
            return
        text = public_companion_text(text)
        if not text:
            return
        clients = {client.base_url: client for client in (self._host_client, self._companion_client()) if client is not None}
        avatar = await self._catgirl_avatar_base64() if fetch_avatar else self._cached_avatar_base64()
        for client in clients.values():
            try:
                response = await client.push_danmaku(text, style="catgirl", avatar=avatar)
                self.logger.info("[sts2_catgirl_llm] danmaku endpoint=%s result=%s", client.base_url, response)
            except Exception as exc:
                self.logger.info("[sts2_catgirl_llm] push failed endpoint=%s: %s", client.base_url, exc)

    async def _catgirl_llm_async(self, snapshot: dict[str, Any], payload: dict[str, Any], companion_evaluation: dict[str, Any] | None = None, scene_key: str = "") -> None:
        """异步生成猫娘点评并推 catgirl 弹幕；**LLM 没产出就退回启发式文本**（不能噤声）。

        战斗时给 LLM 的 prompt 精简为三样：当前回合完整 line + 怪物行动意图 + 我方生命，
        其余界面保持原 summary_text（太长会导致 LLM 抓不住重点、输出变唠叨）。
        """
        kind = str(payload.get("summary_kind") or "")
        try:
            situation = snapshot.get("situation_summary") if isinstance(snapshot.get("situation_summary"), dict) else {}
            summary_text = str(payload.get("message") or situation.get("text") or "").strip()
            if kind == "combat":
                summary_text = await self._build_combat_prompt(snapshot, payload)
            self.logger.info("[sts2_catgirl_llm] generate kind=%s text=%s", kind, summary_text[:80])
            text = await self._catgirl_llm.generate(
                summary_text=summary_text,
                summary_kind=kind,
                payload=payload,
            )
            # 迟到丢弃：生成花了几秒，若画面已经换了场景/回合，这条讲解就是"上一个场景的弹幕"
            # —— 玩家抱怨的正是这个。即时短句（_emit_instant_catgirl_line）已经把该说的说过了。
            stale = bool(scene_key) and bool(self._state.last_instant_line_key) and self._state.last_instant_line_key != scene_key
            if text:
                if stale:
                    self.logger.info(
                        "[sts2_catgirl_llm] 讲解迟到已丢弃 kind=%s %s -> %s",
                        kind,
                        scene_key,
                        self._state.last_instant_line_key,
                    )
                    return
                self.logger.info("[sts2_catgirl_llm] result=%s", text)
                await self._push_catgirl(text)
                return
            # LLM **可用但这次没产出**：必须退回启发式文本。
            # 旧代码这里什么都不推，而 _maybe_emit_catgirl_llm 又因为 llm_ok=True 不会走它自己的
            # 兜底分支 —— 于是猫娘在游戏里彻底噤声。实测：战斗场景 208 次调用全部 result=None，
            # 一条弹幕都没有，而商店/地图/奖励场景正常出声。
            if stale:
                self.logger.info("[sts2_catgirl_llm] 兜底也迟到已丢弃 kind=%s", kind)
                return
            self.logger.info("[sts2_catgirl_llm] result=None kind=%s -> 退回启发式文本", kind)
            self._push_catgirl_fallback(dict(companion_evaluation or {}), payload)
        except Exception as exc:
            self.logger.info("[sts2_catgirl_llm] exception %s kind=%s", exc, kind)

    async def _build_combat_prompt(self, snapshot: dict[str, Any], payload: dict[str, Any]) -> str:
        """旧弹幕接口保留局面描述，不调用求解器生成出牌建议。"""
        steps_text = ""
        # 怪物血量+意图 + 我方生命 + 当前层级。敌人带当前血量：payload["enemies"] 只有 name/intent，
        # 血量要从 snapshot.situation_summary.payload.enemies 取。
        enemies_pool = payload.get("enemies") if isinstance(payload.get("enemies"), list) else []
        situation = snapshot.get("situation_summary") if isinstance(snapshot.get("situation_summary"), dict) else {}
        sum_payload = situation.get("payload") if isinstance(situation.get("payload"), dict) else {}
        full_enemies = sum_payload.get("enemies") if isinstance(sum_payload.get("enemies"), list) else []
        enemy_bits = []
        for idx in range(min(len(enemies_pool), 3)):
            base_e = enemies_pool[idx]
            if not isinstance(base_e, dict):
                continue
            name = str(base_e.get("name") or "敌人")
            intent = str(base_e.get("intent") or "")
            full = full_enemies[idx] if idx < len(full_enemies) and isinstance(full_enemies[idx], dict) else {}
            chp = full.get("current_hp") if full.get("current_hp") is not None else base_e.get("hp")
            mhp = full.get("max_hp")
            hp_text = ""
            if isinstance(chp, (int, float)):
                hp_text = f"{int(chp)}/{int(mhp or 0)}"
            enemy_bits.append(f"{name}({hp_text}，意图:{intent})" if hp_text else (f"{name}(意图:{intent})" if intent else name))
        player = payload.get("player") if isinstance(payload.get("player"), dict) else {}
        hp = ""
        if isinstance(player.get("current_hp"), (int, float)):
            hp = f" 我方生命{int(player['current_hp'])}/{int(player.get('max_hp') or 0)}"
        floor = ""
        try:
            fl = self._safe_int(snapshot.get("floor"))
            if fl:
                floor = f" 层级{fl}"
        except Exception:
            floor = ""
        enemies_text = " 敌人:" + "、".join(enemy_bits) if enemy_bits else ""
        base = steps_text or ""
        return f"本轮: {base}{enemies_text}{hp}{floor}".strip()

    @staticmethod
    def _format_plan_line(plan: dict[str, Any] | None) -> str:
        """把 /solver/plan 的当前回合 line（line[0].steps）拼成一句人类可读指令。

        每个动作一步：打<卡名>[→目标N] / 用药水<名> / 结束回合。取不到返回 ""。
        """
        if not plan:
            return ""
        try:
            steps = _current_turn_steps(plan)
        except Exception:
            return ""
        if not steps:
            return ""
        parts: list[str] = []
        for s in steps:
            if not isinstance(s, dict):
                continue
            k = str(s.get("kind") or "")
            if k == "play_card":
                name = str(s.get("card_name") or s.get("card_id") or "打牌")
                target = ""
                ti = s.get("target_index")
                if isinstance(ti, int):
                    target = f"→{ti + 1}"
                parts.append(f"打{name}{target}")
            elif k == "use_potion":
                parts.append(f"用药水{str(s.get('card_name') or s.get('card_id') or '')}")
            elif k == "end_turn":
                parts.append("结束回合")
        return "；".join(parts)

    def _push_catgirl_fallback(self, companion_evaluation: dict[str, Any], payload: dict[str, Any]) -> None:
        """兜底：推启发式 primary_message 到游戏内弹幕（原 catgirl 轨道文本来源）。"""
        client = self._companion_client()
        if client is None:
            return
        catgirl_text = str(
            companion_evaluation.get("primary_message") or payload.get("message") or ""
        ).strip()
        if catgirl_text:
            try:
                task = asyncio.get_running_loop().create_task(self._push_catgirl(catgirl_text))
                # Fire-and-forget: retrieve any exception so asyncio doesn't warn about an unretrieved task error.
                task.add_done_callback(lambda t: t.exception() if not t.cancelled() else None)
            except Exception:
                self.logger.info("[sts2_catgirl_llm] fallback push failed")

    def _deliver_catgirl_sync(self, snapshot: dict[str, Any]) -> None:
        # 被动局面上下文先喂——说话轨在自动行动/等待用户话语时会早退，那是"别抢话"
        # 的正当理由，但不该连"本体知道现在什么情况"一起断掉（见 _deliver_host_context）。
        self._deliver_host_context(snapshot)
        # 自动行动的发言由同一次决策选择并等待播放；不再异步生成另一份旧局面回复。
        if self._state.autoplay_state == "running":
            return
        if self._danmaku_enabled:
            packet = snapshot.get("catgirl_sync") or {}
            self._emit_instant_catgirl_line(snapshot, snapshot.get("companion_evaluation") or {}, packet.get("payload") or {})
        # 等待本回合用户话语时，禁止异步生成与接下来决策无关的战斗回复。
        if self._wait_for_turn_message and self._companion_session.anchor().get("battle_id"):
            return
        view = self._agent_view(snapshot)
        if not view or not self._state.host_digest_enabled:
            return
        screen = str(view.get("screen") or "")
        combat = view.get("combat") or {}
        anchor = self._companion_session.anchor()
        living = [enemy.get("i", enemy.get("index")) for enemy in combat.get("enemies") or []
                  if enemy.get("alive", enemy.get("is_alive", True))]
        previous = self._proactive_observation
        observation = {**anchor, "screen": screen, "living": living}
        self._proactive_observation = observation
        result_changed = previous.get("battle_id") == anchor.get("battle_id") and len(living) < len(previous.get("living") or [])
        if screen == "COMBAT":
            if not (combat.get("end_turn_will_kill_player") or any(
                risk.get("will_kill_player") for risk in combat.get("lethal_risks") or []
            )):
                if result_changed:
                    kind = "combat_result"
                elif previous.get("battle_id") != anchor.get("battle_id") or previous.get("turn") != anchor.get("turn"):
                    kind = "combat_turn"
                else:
                    return
            else:
                kind = "danger"
        elif screen in {"MAIN_MENU", "MULTIPLAYER_LOBBY", "CHARACTER_SELECT"}:
            return
        else:
            kind = "decision:" + screen
        if self._companion_session.facts.get("last_outcome"):
            outcome = self._companion_session.facts["last_outcome"]
        else:
            outcome = {}
        key = (view_signature({"anchor": anchor, "kind": kind, "living": living})
               if kind in {"combat_turn", "combat_result", "danger"}
               else view_signature({"run_id": view.get("run_id"), "screen": screen,
                                    "choice": view.get({"REWARD": "reward", "MAP": "map", "EVENT": "event",
                                                        "SHOP": "shop", "REST_SITE": "rest"}.get(screen, "selection"))}))
        last_key, last_at = self._companion_push_keys.get(kind, ("", 0.0))
        now = time()
        if key == last_key or (kind not in {"danger", "combat_result", "combat_turn"} and now - last_at < 8):
            return
        self._companion_push_keys[kind] = (key, now)
        body = (
            "联机现场：agent_view 是本次观测，session 记录本局对话和执行结果。"
            "按人格和现场感受决定是否简短回应。此消息用于交流，游戏动作由自动行动循环执行。\n"
            + json.dumps({"agent_view": view, "agent_facts": build_agent_facts(snapshot.get("raw_state")),
                          "session": self._companion_session.context(),
                          "last_action_outcome": outcome}, ensure_ascii=False)
        )
        # 状态推送和宿主回复是异步的。选牌/奖励/事件等决策界面经常只存在一个轮询周期，
        # 延迟一个短窗口并重新读取 agent_view，避免界面已经回到战斗后宿主还按旧画面回复。
        if kind.startswith("decision:"):
            self._schedule_current_proactive_push(
                snapshot, body, kind=kind, priority=7 if kind == "danger" else 4,
            )
        else:
            self._push_proactive_to_host(body, kind=kind, priority=7 if kind == "danger" else 4)

    def _host_reply_text(self, text: str, *, limit: int = 30) -> str:
        normalized = " ".join(str(text or "").split())
        if len(normalized) <= limit:
            return normalized
        return normalized[: limit - 3].rstrip() + "..."

    def _push_companion_message(self) -> None:
        if not bool(self._cfg.get("companion_mode_enabled", False)):
            return
        notifier = self._frontend_notifier
        snapshot = self._state.snapshot if isinstance(self._state.snapshot, dict) else {}
        companion_evaluation = snapshot.get("companion_evaluation") if isinstance(snapshot.get("companion_evaluation"), dict) else {}
        if companion_evaluation and not bool(companion_evaluation.get("should_comment", True)):
            return
        strategy_context = snapshot.get("strategy_context") if isinstance(snapshot.get("strategy_context"), dict) else {}
        strategy_name = str(strategy_context.get("strategy_name") or companion_evaluation.get("strategy_name") or self.t("companion.current_strategy", default="当前策略"))
        commentary = str(companion_evaluation.get("commentary") or "").strip()
        if not commentary:
            commentary = self.t("companion.enabled_default_commentary", default="陪玩已开启，我会给出简短建议。")
        content = self._host_reply_text(
            self.t("companion.enabled_announcement", default="陪玩模式已开启。{strategy_name}：{commentary}", strategy_name=strategy_name, commentary=commentary)
        )
        if notifier is not None:
            notifier(
                content=content,
                description="STS2 companion mode enabled",
                metadata={
                    "kind": "companion_mode_enabled",
                    "delivery_semantics": "passive",
                    "strategy_name": strategy_name,
                    "companion_evaluation": companion_evaluation,
                },
                priority=5,
                message_type="sts2_companion_mode_enabled",
                visibility=["chat"],
                ai_behavior="respond",
            )

    def _emit_status(self) -> None:
        try:
            snap = self._state.snapshot if isinstance(self._state.snapshot, dict) else {}
            # 只推轻量摘要，不塞整包 /state（snapshot 里含完整 raw_state，几十 MB；塞进状态/IPC 会触发
            # NEKO 端 ZMQ 反序列化错误 / 超时）。需要全量就单独走 refresh_state / get_snapshot。
            light_keys = ("screen", "floor", "act", "in_combat", "run_id", "character", "turn", "available_action_count", "summary_kind", "state_signature")
            payload = {
                "source": "sts2_autoplay",
                "transport_state": self._state.transport_state,
                "last_error": self._state.decision_error or self._state.last_error,
                "decision_errors": self._state.decision_errors,
                "input_warning": self._state.input_warning,
                "retry_at": self._state.retry_at,
                "snapshot": {k: snap.get(k) for k in light_keys if k in snap},
                "standby": self._state.standby,
                "autoplay_state": self._state.autoplay_state,
                "step_count": self._state.step_count,
                # 卡住时这里非空（"UNKNOWN（未识别界面类型 X）界面里没有可执行动作，已等 N 秒"）：
                # 让"它不动了"变成看得见的事实，而不是要玩家自己盯出来。
                "stuck_reason": self._state.stuck_reason,
                "sse_reconnect_count": getattr(self._state, "sse_reconnect_count", 0),
            }
            key = json.dumps(payload, ensure_ascii=False, sort_keys=True)
            if key == self._reported_status_key and time() - self._reported_status_at < 3:
                return
            self._report_status(payload)
            self._reported_status_key, self._reported_status_at = key, time()
        except Exception:
            pass
