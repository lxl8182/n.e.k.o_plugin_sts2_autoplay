from __future__ import annotations

import asyncio
import hashlib
import threading
from concurrent.futures import Future
from time import time
from typing import Any

from .decision_payload import DecisionPayload
from .instructions import instruction_summary, normalize_strategy_instruction
from .llm_planner import view_signature
from .snapshot_normalizer import normalize_snapshot


class STS2LoopRunner:
    # Mod /events/stream event types that mark a real state change worth re-fetching /state for.
    _RELEVANT_EVENT_TYPES = frozenset({
        "screen_changed", "available_actions_changed", "combat_started", "combat_ended",
        "combat_turn_changed", "player_action_window_opened", "player_action_window_closed",
        "route_decision_required", "reward_decision_required", "event_state_changed", "session_started",
    })
    # 猫娘战斗弹幕只响应这三个战斗事件；仅 combat_turn_changed（回合推进）带 out 牌建议 line，
    # 其余两个（combat_started / available_actions_changed）只报基础。见 _build_combat_prompt。
    _COMBAT_DANMAKU_EVENTS = frozenset({"combat_started", "available_actions_changed", "combat_turn_changed"})
    # Coalesce a burst of SSE events into at most one /state fetch per this window.
    _EVENT_FETCH_MIN_INTERVAL_SECONDS = 0.5
    # 单次自动决策的硬上限：宿主/Mod 某个 await 不返回时，取消本轮并释放决策锁，
    # 避免整局无限停在“正在行动”。正常决策远低于该值。
    _AUTOPLAY_STEP_TIMEOUT_SECONDS = 90.0

    def __init__(self, service: Any) -> None:
        self._service = service
        self._poll_task: asyncio.Task[Any] | Future[Any] | None = None
        self._autoplay_task: asyncio.Task[Any] | Future[Any] | None = None
        self._shutdown = False
        self._owner_loop: asyncio.AbstractEventLoop | None = None
        self._owner_thread: threading.Thread | None = None
        self._owner_thread_ready = threading.Event()
        # Cache the mod /solver/plan against the combat-state signature so the mod's search is not
        # recomputed on every tick while the combat position is unchanged. See _combat_plan_signature.
        self._solver_plan_sig: Any = None
        self._solver_plan_cached: dict[str, Any] | None = None
        # ★ t11：**取回** `_solver_plan_cached` 那一刻的局面指纹（见 tick 里 `plan_fetch_signature`）。
        #   随缓存计划一起存活；planner 用它区分「取回后局面已变（漂移 ⇒ 归 A）」与
        #   「取回后局面没变但首步仍做不了（真无解 ⇒ 归 B）」。
        self._solver_plan_fetch_signature = ""
        # 计划复用/重算的日志去重键（避免每 tick 刷屏）。
        self._plan_log_key = ""
        # 最近一次 SSE 事件类型（combat_started / player_action_window_opened 等）。随
        # /events/stream 更新，供 catgirl 战斗 prompt 判断"该不该带 line"：仅轮到玩家行动的
        # player_action_window_opened 才带，其余战斗事件只报基础（怪物血/我方血/层级）。
        self._current_event_type: str = ""
        # Scene-change event streaming (mod GET /events/stream): drive a refresh on a real change
        # instead of polling /state on a timer. See _event_loop / _RELEVANT_EVENT_TYPES.
        self._event_task: asyncio.Task[Any] | Future[Any] | None = None
        self._status_task: asyncio.Task[Any] | Future[Any] | None = None
        self._last_event_refresh = 0.0
        self._event_backoff = 1.0
        self._sse_connected = False
        # ★★ t14/D4（批 1）看门狗状态：判断"业务进度"是否停滞（只看 step_count，不看连接/心跳）。
        self._fallback_last_step_count = -1
        self._fallback_last_progress_at = 0.0
        self._fallback_refresh_count = 0
        # ★★ t14/D4（批 2）业务事件静默检测的起算时刻（0.0 = 已收到过业务事件）。
        self._business_silence_since = 0.0

    async def tick(self, *, plan_combat: bool = False) -> dict[str, Any]:
        client = await self._service._ensure_companion_client()
        # ★★ D10（t26，上游修复）：`/state` 失败时**降级**到 `/actions/available`，而不是中止整个 tick。
        #   判据：两个端点是独立的，前者失败不蕴含"没有动作可做"；只有后者返回空动作集才是。
        #   ★ 不得静默：降级时打 `[sts2_d10]`，让"插件没动"与"状态取不到"在日志里可区分。
        raw_state: dict[str, Any] = {}
        state_error: Exception | None = None
        try:
            raw_state = await client.get_state()
        except Exception as exc:  # noqa: BLE001 — 任何 /state 失败都降级，不区分异常类型
            state_error = exc
        raw_actions = await client.get_available_actions()
        snapshot = normalize_snapshot(raw_state, raw_actions)
        if snapshot.get("state_unavailable"):
            self._service.logger.warning(
                "[sts2_d10] /state 不可用 ⇒ 降级用 /actions/available 的动作集 "
                "（state_error=%s；actions=%d；screen=%s）",
                type(state_error).__name__ if state_error is not None else "empty_payload",
                snapshot.get("available_action_count", 0),
                snapshot.get("screen"),
            )
        view = snapshot.get("agent_view") or {}
        epoch = self._service._companion_session.epoch
        if view:
            from .agent_facts import build_agent_facts
            self._service._companion_session.observe(view, in_combat=snapshot.get("in_combat"),
                                                     effects=build_agent_facts(raw_state))
        if self._service._companion_session.epoch != epoch:
            self._service._reset_companion_run_context()
        action_registry = self._service._action_registry.build(snapshot)
        classification = self._service._state_machine.classify(snapshot)
        snapshot_with_context = {**snapshot, "classification": classification, "polled_at": time()}
        snapshot_summary = self._service._summary_builder.build_snapshot_summary(snapshot_with_context)
        previous_summary = self._service._state.latest_snapshot_summary
        continuous_delta = self._service._summary_engine.compute_delta(
            previous_summary if isinstance(previous_summary, dict) else None,
            snapshot_summary,
            source="continuous_snapshot",
        )
        self._service._state.latest_snapshot_summary = dict(snapshot_summary)
        self._service._state.remember_continuous_delta(continuous_delta)
        summary_context = self._service._summary_builder.build(
            snapshot_with_context,
            runtime_state=self._service._state,
        )
        strategy_context = self._service._strategy_repository.build_context(
            {**snapshot, "classification": classification, "summary_context": summary_context}
        )
        tactical_signals = self._service._summary_builder.build_tactical_signals(
            snapshot_with_context,
            summary_context,
            strategy_context,
        )
        mode_info = self._service._mode_controller.describe(self._service._state.control_mode)
        if self._service._state.discussion_active:
            # 讨论暂停只关执行；仍可在停手状态计算提议。
            mode_info = {**mode_info, "allows_planner": True}
        candidate_actions = []
        instructions = list(self._service._state.pending_guidance)
        instructions.append(
            normalize_strategy_instruction(
                str(strategy_context.get("strategy_name") or "unknown"),
                dict(strategy_context.get("strategy_constraints") if isinstance(strategy_context.get("strategy_constraints"), dict) else {}),
            )
        )
        decision_payload = DecisionPayload(
            mode=self._service._state.control_mode,
            screen_type=str(classification.get("screen_class") or "unknown"),
            state_name=str(classification.get("state_name") or snapshot.get("screen") or "unknown"),
            summary_kind=str(classification.get("summary_kind") or "general"),
            state_signature=str(self._service._state.snapshot_signature or ""),
            strategy_directives={
                "strategy_name": strategy_context.get("strategy_name"),
                "strategy_prompt": strategy_context.get("strategy_prompt"),
                "constraints": strategy_context.get("strategy_constraints"),
                **(strategy_context.get("strategy_directives") if isinstance(strategy_context.get("strategy_directives"), dict) else {}),
            },
            guidance={
                "pending": list(self._service._state.pending_guidance),
                "generation": self._service._state.guidance_generation,
                "summary": instruction_summary(instructions),
            },
            instructions=instructions,
            run_state={
                "floor": snapshot.get("floor"),
                "act": snapshot.get("act"),
                "in_combat": snapshot.get("in_combat"),
                "current_hp": snapshot_summary.get("player", {}).get("current_hp") if isinstance(snapshot_summary.get("player"), dict) else None,
                "max_hp": snapshot_summary.get("player", {}).get("max_hp") if isinstance(snapshot_summary.get("player"), dict) else None,
            },
            tactical_signals=tactical_signals,
            legal_actions=[dict(action) for action in action_registry if isinstance(action, dict)],
            candidate_actions=[dict(item) for item in candidate_actions if isinstance(item, dict)],
            policy={
                "allows_planner": bool(mode_info.get("allows_planner")),
                "allows_game_llm": bool(mode_info.get("allows_game_llm")),
                "prefers_heuristic": bool(mode_info.get("prefers_heuristic")),
                "prefers_model": bool(mode_info.get("prefers_model")),
            },
        ).as_dict()
        decision_payload["recent_decision_memory"] = list(self._service._state.recent_decision_memory)
        decision_payload["run_intent"] = dict(self._service._state.run_intent)
        summary_context["decision_payload"] = decision_payload
        strategy_context["decision_payload"] = decision_payload
        # 状态刷新只读；模型调用由单独的动作循环串行进行，聊天/SSE 不等待模型。
        previous_operation = self._service._state.snapshot.get("planned_operation")
        planned_operation_dict = None
        if (isinstance(previous_operation, dict) and previous_operation.get("view_signature") == view_signature(snapshot.get("agent_view") or {})
                and previous_operation.get("session_generation") == self._service._companion_session.generation
                and previous_operation.get("control_mode") == self._service._state.control_mode):
            planned_operation_dict = previous_operation

        agent_operation = planned_operation_dict

        executable_operation = self._service._action_engine.validate({**snapshot, "action_registry": action_registry}, agent_operation)
        situation_summary = self._service._summary_engine.summarize(summary_context)
        companion_evaluation = self._service._companion_evaluator.evaluate(
            summary_context=summary_context,
            situation_summary=situation_summary,
            strategy_context=strategy_context,
            runtime_state=self._service._state,
        )
        action_frame = self._service._state.latest_action_frame if isinstance(self._service._state.latest_action_frame, dict) else {}
        if action_frame:
            companion_evaluation["action_frame"] = dict(action_frame)
        catgirl_sync = self._service._catgirl_bridge.build_sync_packet(
            {
                **snapshot,
                "classification": classification,
                "situation_summary": situation_summary,
                "companion_evaluation": companion_evaluation,
            },
            standby=self._service._state.standby,
        )
        # 带上触发这次点评的 SSE 事件类型：仅 player_action_window_opened（轮到玩家行动）时
        # catgirl 战斗 prompt 才带 line，其余战斗事件只报基础（怪物血/我方血/层级）。
        catgirl_sync["payload"]["event_type"] = self._current_event_type
        catgirl_sync["payload"]["agent_summary"] = {
            "standby": self._service._state.standby,
            "text": str(situation_summary.get("text") or ""),
            "kind": situation_summary.get("kind", "general"),
            "source": situation_summary.get("source", "snapshot"),
            "delta": dict(situation_summary.get("delta") if isinstance(situation_summary.get("delta"), dict) else {}),
            "before": dict(situation_summary.get("before") if isinstance(situation_summary.get("before"), dict) else {}),
            "after": dict(situation_summary.get("after") if isinstance(situation_summary.get("after"), dict) else {}),
            "recent_guidance": list(self._service._state.pending_guidance),
            "companion_evaluation": companion_evaluation,
        }

        return {
            "raw_state": raw_state,
            "raw_actions": raw_actions,
            "snapshot": {
                **snapshot,
                "action_registry": action_registry,
                "classification": classification,
                "summary_context": summary_context,
                "strategy_context": strategy_context,
                "mode": mode_info,
                "planned_operation": planned_operation_dict,
                "agent_operation": agent_operation,
                "executable_operation": executable_operation,
                "agent_packet": {
                    "screen": snapshot.get("screen", "unknown"),
                    "classification": classification,
                    "strategy_context": strategy_context,
                    "summary_context": summary_context,
                    "standby": self._service._state.standby,
                    "recent_guidance": list(self._service._state.pending_guidance),
                    "available_action_ids": [str(action.get("id") or "") for action in action_registry if isinstance(action, dict)],
                    "companion_evaluation": companion_evaluation,
                },
                "situation_summary": situation_summary,
                "companion_evaluation": companion_evaluation,
                "catgirl_sync": catgirl_sync,
                "polled_at": snapshot_with_context["polled_at"],
            },
        }

    def start_background(self) -> None:
        self._shutdown = False
        if self._status_task is None or self._task_done(self._status_task):
            self._ensure_owner_loop()
            self._status_task = self._create_task(self._status_loop(), name="sts2-status-heartbeat")
        # 事件流模式下完全靠 /events/stream 驱动刷新（真正的变化才拉 /state），不再另起一个定时
        # 轮询循环——否则状态没变也每几秒重推一遍状态/通报，重复刷屏污染日志。轮询循环仅作为
        # use_event_stream=false（无 SSE 的客户端）的兜底。
        self._hb_log(
            "start_background use_event_stream=%s poll_task_alive=%s event_task_alive=%s"
            % (self._service._cfg_use_event_stream(),
               not self._task_done(self._poll_task), not self._task_done(self._event_task))
        )
        if not self._service._cfg_use_event_stream() and (
            self._poll_task is None or self._task_done(self._poll_task)
        ):
            self._ensure_owner_loop()
            self._poll_task = self._create_task(self._poll_loop(), name="sts2-poll-loop")
        if self._service._cfg_use_event_stream() and (
            self._event_task is None or self._task_done(self._event_task)
        ):
            self._ensure_owner_loop()
            self._event_task = self._create_task(self._event_loop(), name="sts2-event-stream")

    def start_autoplay(self) -> None:
        self._shutdown = False
        # ★ 注意：``_autoplay_task`` 的复活是本函数的传统职责；**刷新通路**
        #   （``_event_task`` / ``_poll_task``）的复活由下方 ``start_background()`` 负责。
        self._hb_log(
            "start_autoplay autoplay_task_alive=%s event_task_alive=%s poll_task_alive=%s"
            % (not self._task_done(self._autoplay_task),
               not self._task_done(self._event_task), not self._task_done(self._poll_task))
        )
        if self._autoplay_task is None or self._task_done(self._autoplay_task):
            self._ensure_owner_loop()
            self._autoplay_task = self._create_task(self._autoplay_loop(), name="sts2-autoplay-loop")
        # ★★ t14/D4 自愈：**同时**复活已死的刷新通路。
        #   历史缺陷（实测）：`_event_loop` 静默退出后，刷新通路彻底中断
        #   （`last_success_at` 不再前进、`step_count` 冻结）；而本函数只重建
        #   `_autoplay_task` ⇒ pause → start **完全无法恢复**，只能 reload 插件。
        #   `start_background()` 内已含"只在任务已死时才重建"的幂等判断
        #   （`_task_done` 检查），故此处直接复用，不会重复起任务。
        #   ★ 必须放在 `_autoplay_task` 之后：若刷新通路复活失败，
        #     autoplay 循环本身仍应存在，以便下一轮看门狗兜底。
        self.start_background()

    async def stop_background(self) -> None:
        self.stop_background_sync()

    def is_polling(self) -> bool:
        return self._poll_task is not None and not self._task_done(self._poll_task)

    def is_autoplaying(self) -> bool:
        return self._autoplay_task is not None and not self._task_done(self._autoplay_task)

    def _ensure_owner_loop(self) -> asyncio.AbstractEventLoop | None:
        loop = self._owner_loop
        thread = self._owner_thread
        if loop is not None and thread is not None and thread.is_alive() and not loop.is_closed():
            return loop

        ready = self._owner_thread_ready
        ready.clear()
        holder: dict[str, asyncio.AbstractEventLoop] = {}

        def _run_loop() -> None:
            worker_loop = asyncio.new_event_loop()
            asyncio.set_event_loop(worker_loop)
            holder["loop"] = worker_loop
            ready.set()
            try:
                worker_loop.run_forever()
            finally:
                pending = [task for task in asyncio.all_tasks(worker_loop) if not task.done()]
                for task in pending:
                    task.cancel()
                if pending:
                    worker_loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
                worker_loop.close()

        thread = threading.Thread(target=_run_loop, name="sts2-companion-poll", daemon=True)
        thread.start()
        if not ready.wait(timeout=2.0):
            loop = holder.get("loop")
            if loop is not None and not loop.is_closed():
                try:
                    loop.call_soon_threadsafe(loop.stop)
                except RuntimeError:
                    pass
            if thread.is_alive():
                thread.join(timeout=3.0)
            return None
        self._owner_loop = holder.get("loop")
        self._owner_thread = thread
        return self._owner_loop

    def _stop_owner_loop(self) -> None:
        loop = self._owner_loop
        thread = self._owner_thread
        self._owner_loop = None
        self._owner_thread = None
        if loop is not None and not loop.is_closed():
            try:
                loop.call_soon_threadsafe(loop.stop)
            except RuntimeError:
                pass
        if thread is not None and thread.is_alive():
            thread.join(timeout=3.0)

    def _task_done(self, task: asyncio.Task[Any] | Future[Any] | None) -> bool:
        return task is None or task.done()

    # ── ★ 诊断心跳与空转观测（仅日志，不改任何决策判据）────────────────────────
    _HB_EVERY = 20

    def _hb_log(self, message: str) -> None:
        """诊断心跳（仅日志）。任何异常都被吞掉，绝不影响决策链路。"""
        try:
            self._service.logger.info("[sts2_heartbeat] %s", message)
        except Exception:
            pass

    def _note_stall_sample(self, tick: int) -> None:
        """把一个心跳样本喂给空转判据，并在判定成立时打 `[sts2_stall]` 告警。

        **纯观测**：只读 `_combat_turn_key()`、只写 runtime_state 里 stall_* 记账字段，
        绝不改任何决策字段。

        ``tick`` 是 `_autoplay_loop` 的循环计数（每轮 +1，**无论是否推进**）。
        ★ 不能用 `step_count` 顶替 heartbeat：它只在动作成功时 +1，真卡死时会冻结。
        """
        try:
            snapshot = self._service._state.snapshot
            snapshot = snapshot if isinstance(snapshot, dict) else {}
            turn_key = self._service._combat_turn_key(snapshot)
            verdict = self._service._state.note_turn_key_sample(turn_key, heartbeat=int(tick))
            if not verdict.get("should_warn"):
                return
            self._service.logger.warning(
                "[sts2_stall] ⚠ 疑似空转：%.0f 分钟内同一局面（%s）反复重算 %d 次"
                "（占比 %.2f，种类 %d，样本 %d，tick=%d，heartbeat 在涨）",
                float(verdict.get("zero_new_seconds", 0.0)) / 60.0,
                turn_key or "<empty>",
                int(verdict.get("repeats", 0)),
                float(verdict.get("top_key_share", 0.0)),
                int(verdict.get("distinct_keys", 0)),
                int(verdict.get("samples", 0)),
                int(tick),
            )
        except Exception:
            pass

    async def _poll_loop(self) -> None:
        self._hb_log("poll-loop ENTER")
        n = 0
        while not self._shutdown:
            n += 1
            try:
                await self._service.refresh_state(trigger_sync=True)
                await self._service.reconcile_turn_control()
            except Exception as exc:
                self._service._mark_loop_error(exc)
            if n % self._HB_EVERY == 0:
                self._hb_log("poll-loop N=%d OK" % n)
            await asyncio.sleep(self._poll_interval())
        self._hb_log("poll-loop EXIT(shutdown=True) n=%d" % n)

    # ★★ t14/D4 修复（批 1）：SSE 模式下的**周期兜底刷新**（no-progress 看门狗）。
    #
    # 缺陷（实机证据，2026-10-19）：
    #   `_event_loop` 阻塞在 `:565 async for ... subscribe_events()`（心跳实测 ENTER=4 / EXIT=0 /
    #   WAKE=0 / DROPPED=0 ⇒ 从未返回、从未重连），而 SSE 模式下 `start_background()` **不创建**
    #   `_poll_task`（见 :367-376，实测 `poll_task_alive=False`）。⇒ **没有任何周期刷新者**。
    #   于是某次 refresh 恰好拿到退化的 available_actions（如出牌动画瞬间只剩 save_and_quit）
    #   之后，快照被**永久固化**：陈旧 registry 里没有 end_turn ⇒ `action_engine.validate()`
    #   返回 None（实测 `executable_operation = null`）⇒ 动作发不出 ⇒ 局面不变化 ⇒
    #   再也不产生事件 ⇒ `_event_loop` 更不会被唤醒。**自洽死锁。**
    #   实机表现：step_count 卡在 13、turn 恒为 6、插件内部 names=['save_and_quit']
    #   而 mod 侧一直是 ['end_turn','save_and_quit']（陈旧 60s+ 不变）、
    #   `external_control.seconds_since_contact` 单调增长（无人向 mod 发请求）。
    #
    # 判据（与字节/事件无关，只看**业务进度**）：`step_count` 连续 `_FALLBACK_NO_PROGRESS_SECONDS`
    #   秒未增 ⇒ 强制 `refresh_state()`。★ 刻意**不用** state_version / 心跳 / 连接状态判活
    #   （这些都已被证明会掩盖真实停滞）。
    #
    # ★ 为什么这是安全网而不是"越权动作"：它**只刷新状态**，不发明任何动作；
    #   刷新后一切仍由既有判据决定（planner / validate / 窗口判定全部不变）。
    _FALLBACK_NO_PROGRESS_SECONDS = 20.0

    async def _maybe_fallback_refresh(self) -> None:
        """`step_count` 长时间不增 ⇒ 强制刷新一次，打破"陈旧快照 ⇒ 零推进"的自锁。"""
        try:
            state = self._service._state
            step_count = int(getattr(state, "step_count", 0) or 0)
            now = time()
            if step_count != self._fallback_last_step_count:
                # 有进展（或首次）⇒ 重新计时。★ 只在"真的动了"时清零。
                self._fallback_last_step_count = step_count
                self._fallback_last_progress_at = now
                return
            if now - self._fallback_last_progress_at < self._FALLBACK_NO_PROGRESS_SECONDS:
                return
            # ★ 到期仍未推进 ⇒ 强制刷新（并重新计时，避免每 tick 都刷）。
            self._fallback_last_progress_at = now
            self._fallback_refresh_count += 1
            self._hb_log(
                "no-progress-watchdog step_count=%s 停滞>=%.0fs ⇒ 强制 refresh_state（第 %d 次）"
                % (step_count, self._FALLBACK_NO_PROGRESS_SECONDS, self._fallback_refresh_count)
            )
            await self._service.refresh_state(trigger_sync=True)
        except Exception as exc:
            # 看门狗自身绝不拖垮主循环：记一次心跳日志后继续。
            self._hb_log("no-progress-watchdog 刷新失败: %r" % (exc,))

    def _poll_interval(self) -> float:
        # With the event stream active the poll loop is only a fallback (slow), so it never races the
        # event-driven refresh; without SSE it keeps the historical reactive cadence.
        if self._service._cfg_use_event_stream():
            return self._service._cfg_fallback_poll_interval()
        return self._service._cfg_poll_interval(active=self._service._state.autoplay_state == "running")

    # ★★ t14/D4（批 2）：**业务事件静默超时** —— 解决"心跳掩盖业务事件流死亡"。
    #
    # 缺陷（实机证据，2026-10-19）：
    #   mod 每 ~1s 发一行 `: heartbeat` 注释。它刷新了 `transport_client` 里 `read=60.0`
    #   的读超时 ⇒ **超时永不触发**；而注释行被 `continue` 丢弃、**不产出任何事件**。
    #   于是当**业务事件流**停了（插件自锁 ⇒ 局面不变 ⇒ mod 也不再发事件），
    #   连接看起来**完全健康**：`_event_loop` 永久阻塞在 `async for`，
    #   **不抛异常 ⇒ 不重连 ⇒ 不刷新**。实机心跳实测：
    #     `ENTER=4 / EXIT=0 / WAKE=0 / DROPPED=0`（从未返回、从未重连），
    #   而队长直连 mod `/events/stream` 仍是 HTTP 200、15 秒 16 行
    #   ⇒ ★ **保活信号掩盖了业务事件流的死亡**，这是本次死锁能永久持续的直接原因。
    #
    # 判据：**只有业务事件才算进度**。连接侧每产出一个业务事件就更新
    #   `client.last_business_event_at`（见 transport_client 的 SSE 循环）；
    #   这里若发现"距上次业务事件已超时"就**主动断开重连**，而不是干等。
    #   ★ 只用业务事件判活，绝不用心跳/连接状态 —— 后者正是本缺陷的来源。
    #   ★ 60s：正常推进时场景事件远密于此（战斗里每 1-3 秒若干条），
    #     同时远大于"长思考/结算动画"的正常停顿。
    _BUSINESS_EVENT_SILENCE_SECONDS = 60.0
    # 检查周期：无需每 tick 都看，10s 一次足够（且重连本身还有 backoff）。
    _BUSINESS_SILENCE_CHECK_EVERY = 10.0

    async def _business_silence_expired(self) -> bool:
        """返回 True 表示"业务事件已静默过久、需要断开重连"。

        ★ 刻意**不**看心跳/连接状态 —— 它们已被证明会掩盖真实停滞（见上）。
        """
        try:
            client = self._service._require_client()
            last = float(getattr(client, "last_business_event_at", 0.0) or 0.0)
            if last <= 0.0:
                # 本次连接还没产出过业务事件：从"连接建立时刻"起算，避免刚连上就误判。
                if not self._business_silence_since:
                    self._business_silence_since = time()
                silent = time() - self._business_silence_since
            else:
                self._business_silence_since = 0.0
                silent = time() - last
            if silent >= self._BUSINESS_EVENT_SILENCE_SECONDS:
                self._hb_log(
                    "event-loop 业务事件静默 %.0fs（>=%.0fs）⇒ 主动断开重连"
                    % (silent, self._BUSINESS_EVENT_SILENCE_SECONDS)
                )
                return True
        except Exception as exc:
            self._hb_log("业务静默检测异常: %r" % (exc,))
        return False

    async def _event_loop_body(self) -> None:
        """`_event_loop` 的单次订阅生命周期；返回即表示连接结束、需要重连。

        ★ 从 `_event_loop` 抽出来，是为了让外层能用 `asyncio.wait(timeout=...)`
        对"永久阻塞的 async for"做**业务静默检测**（心跳会一直维持连接，所以
        单靠 transport 的读超时永远等不到异常）。
        """
        client = self._service._require_client()
        if not hasattr(client, "subscribe_events"):
            # No SSE-capable transport; nothing to do.
            # ★★ 原本是静默 return（零日志、零重试）——现在必须留痕，否则
            #    "事件循环已死"会伪装成"局面没变化"。这里**仍保持** return 语义（仅加日志）。
            self._hb_log("event-loop EXIT(no subscribe_events on %s)" % type(client).__name__)
            return
        async for envelope in client.subscribe_events():
            event_type = envelope.get("type", "") if isinstance(envelope, dict) else ""
            if event_type == "stream_ready":
                # Connection (re)established: reset backoff and pull the current state once.
                self._event_backoff = 1.0
                self._business_silence_since = time()
                self._mark_sse_connected(True)
                self._hb_log("event-loop stream_ready (backoff reset)")
                await self._maybe_event_refresh(force=True)
                # 连接上 mod 后把其自身 LLM/弹幕交给本插件（mod 可能晚于插件启动），失败静默。
                try:
                    await client.set_config(llm_enabled=False, danmaku_enabled=False)
                    await self._service.sync_control_mode_to_mod()
                    # 每次(重)连都推一次猫娘显示名：游戏可能晚于插件启动、或中途重启，
                    # 这条 stream_ready 是必然触发的握手点，比单靠 startup/adopt 更可靠。
                    await self._service.sync_catgirl_name_to_mod()
                    await self._service.reconcile_turn_control()
                except Exception:
                    pass
                continue
            if event_type in self._RELEVANT_EVENT_TYPES:
                # 战斗弹幕只认三个战斗事件（见 _COMBAT_DANMAKU_EVENTS）；非战斗事件清空，
                # 避免 _current_event_type 把上一场战斗的事件类型串进当前画面。
                self._current_event_type = event_type if event_type in self._COMBAT_DANMAKU_EVENTS else ""
                self._hb_log("event-loop EVENT=%s -> refresh" % event_type)
                await self._maybe_event_refresh(force=False)
            # heartbeat / unknown events -> ignore.

    async def _event_loop(self) -> None:
        # Subscribe to the mod's /events/stream and re-read /state on a scene change (the mod already
        # diffs its state, so these events fire only when something actually changed). Keeps reconnecting
        # with exponential backoff; the slow _poll_loop remains the safety net.
        # ★ 注意：SSE 模式下 start_background() **不会**创建 _poll_task（见 :367-376），
        #   所以本循环退出后无人再驱动 refresh_state。该事实由心跳日志暴露（t17/D8）。
        #   ★★ t14/D4 已补两道兜底：批 1 的 no-progress 强制刷新（`_maybe_fallback_refresh`）
        #      与批 2 的业务静默重连（本函数）。
        self._hb_log("event-loop ENTER")
        wakes = 0
        while not self._shutdown:
            wakes += 1
            try:
                client = await self._service._ensure_companion_client()
                if not hasattr(client, "subscribe_events"):
                    # No SSE-capable transport; nothing to do.
                    return
            except Exception:
                await asyncio.sleep(1.0)
                continue

            if wakes % self._HB_EVERY == 0:
                self._hb_log("event-loop WAKE=%d subscribing" % wakes)

            # ★★ 把订阅包在独立任务里，以便对"永久阻塞的 async for"做超时检查。
            sub = asyncio.ensure_future(self._event_loop_body())
            try:
                while not sub.done():
                    # 每 _BUSINESS_SILENCE_CHECK_EVERY 秒醒一次看看业务事件是否已静默过久。
                    await asyncio.wait({sub}, timeout=self._BUSINESS_SILENCE_CHECK_EVERY)
                    if sub.done():
                        break
                    if await self._business_silence_expired():
                        self._hb_log("event-loop 因业务静默被强制重置")
                        sub.cancel()
                        break
                if sub.done() and not sub.cancelled():
                    # 订阅正常/异常结束 ⇒ 取出异常（若有）以按既有路径记日志并重连。
                    exc = sub.exception()
                    if exc is not None:
                        raise exc
            except Exception as exc:
                self._service.logger.warning("SSE events stream dropped: %s", exc)
                self._hb_log("event-loop DROPPED: %r" % (exc,))
            finally:
                if not sub.done():
                    sub.cancel()
                    try:
                        await sub
                    except (asyncio.CancelledError, Exception):
                        pass
                self._mark_sse_connected(False)

            # Disconnected: backoff and retry. Do not flood while the mod is briefly unavailable.
            await asyncio.sleep(self._event_backoff)
            self._event_backoff = min(self._event_backoff * 2, 30.0)
        self._hb_log("event-loop EXIT(shutdown=True) wakes=%d" % wakes)

    async def _maybe_event_refresh(self, *, force: bool) -> None:
        now = time()
        if not force and now - self._last_event_refresh < self._EVENT_FETCH_MIN_INTERVAL_SECONDS:
            return
        self._last_event_refresh = now
        try:
            await self._service.refresh_state(trigger_sync=True)
            await self._service.reconcile_turn_control()
        except Exception as exc:
            self._service._mark_loop_error(exc)

    def _mark_sse_connected(self, connected: bool) -> None:
        state = self._service._state
        # Count only a real transition (an established stream that dropped), not a clean shutdown.
        if not connected and self._sse_connected and not self._shutdown:
            state.sse_reconnect_count += 1
        self._sse_connected = connected

    async def _autoplay_loop(self) -> None:
        # ★ 诊断心跳（t17/D8，仅日志，不改任何判据）：
        #   目的 = 区分"循环死了/挂住"与"循环活着但每 tick 早退/无动作"。
        #   独立 tag [sts2_heartbeat]，绝不混进决策日志（否则破坏"决策日志空洞"判据）。
        self._hb_log("autoplay-loop ENTER")
        tick = 0
        while not self._shutdown:
            tick += 1
            try:
                await self._service._sync_companion_ingress()
                await self._service._sync_execution_status()
                has_input = bool(self._service._companion_session.pending_messages() or self._service._ingress_callbacks
                                 or self._service._decision_failure_notice)
            except Exception as exc:
                self._service._mark_loop_error(exc, autoplay=True)
                await asyncio.sleep(0.25 if self._service._decision_failure_notice
                                    else self._service._autoplay_retry_backoff())
                continue
            if self._service._state.autoplay_state != "running" or self._service._state.standby:
                # 有未处理的玩家输入时仍要走一轮（暂停/旁观只停自动决策，不吞输入）。
                if not has_input:
                    if tick % self._HB_EVERY == 0:
                        self._hb_log(
                            "autoplay-loop TICK=%d SKIP(state=%s standby=%s)"
                            % (tick, self._service._state.autoplay_state, self._service._state.standby)
                        )
                    await asyncio.sleep(0.25)
                    continue
            try:
                result = await asyncio.wait_for(
                    self._service.run_autoplay_step(),
                    timeout=self._AUTOPLAY_STEP_TIMEOUT_SECONDS,
                )
            except asyncio.TimeoutError:
                # 卡住时取消本轮等待；_run_autoplay_step 的 finally 会释放决策锁。
                self._service._mark_loop_error(
                    RuntimeError(f"自动决策超过 {self._AUTOPLAY_STEP_TIMEOUT_SECONDS:.0f} 秒未返回，已中断本轮等待"),
                    autoplay=True,
                )
                await asyncio.sleep(0.25)
                continue
                await asyncio.sleep(0.25)
                continue
            except Exception as exc:
                # 单次错误退避；连续失败达到上限后暂停并投递原因。
                self._service._mark_loop_error(exc, autoplay=True)
                await asyncio.sleep(0.25 if self._service._decision_failure_notice
                                    else self._service._autoplay_retry_backoff())
                continue
            await self._maybe_fallback_refresh()
            self._note_stall_sample(tick)
            if tick % self._HB_EVERY == 0:
                self._hb_log(
                    "autoplay-loop TICK=%d OK step_count=%s"
                    % (tick, getattr(self._service._state, "step_count", "?"))
                )
            delay = (self._service._autoplay_retry_backoff() if result.get("status") in {"error", "retrying"}
                     else 0 if result.get("executed") and self._service._selected_steps
                     else 0.1 if result.get("status") == "pending" else 0.25)
            await asyncio.sleep(delay)
        self._hb_log("autoplay-loop EXIT(shutdown=True) tick=%d" % tick)

    async def _status_loop(self) -> None:
        while not self._shutdown:
            await self._service._sync_execution_status()
            await asyncio.sleep(3)

    def _log_plan_reuse(self, step_index: int, total: int) -> None:
        """按快照继续执行时留痕（同一步只记一次）—— 用来核对"到底省了多少次求解"。"""
        key = f"reuse:{step_index}/{total}"
        if key == self._plan_log_key:
            return
        self._plan_log_key = key
        try:
            self._service.logger.info("[sts2_plan] 复用快照 step=%s/%s（局面与快照一致）", step_index + 1, total)
        except Exception:
            pass

    def _log_plan_refresh(self, reason: str) -> None:
        """重新向 mod 取计划时留痕（含原因）—— 原因是"局面不一致"还是"回合变了"要看得出来。"""
        try:
            self._service.logger.info("[sts2_plan] 重新计算计划（原因：%s）", reason)
        except Exception:
            pass

    def invalidate_combat_plan_cache(self) -> None:
        """丢弃整回合缓存的 /solver/plan（下一次 tick 重新向 mod 取）。

        场景：动作被 mod 拒收（多半是缓存计划指向的敌人已经死了）。不丢的话下一轮还会拿同一个
        过期下标去撞同一堵墙。
        """
        self._solver_plan_sig = None
        self._solver_plan_cached = None
        # ★ t11：指纹与缓存计划同生共死 —— 清缓存必须一并清指纹，
        #   否则新取回的计划会错误沿用上一份的"取回时指纹"。
        self._solver_plan_fetch_signature = ""

    def _create_task(self, coro: Any, *, name: str) -> asyncio.Task[Any] | Future[Any] | None:
        try:
            current_loop = asyncio.get_running_loop()
            if self._owner_loop is None or self._owner_loop.is_closed() or not self._owner_loop.is_running():
                self._owner_loop = current_loop
            loop = self._owner_loop
            if loop is current_loop:
                task = loop.create_task(coro, name=name)
            else:
                future = asyncio.run_coroutine_threadsafe(coro, loop)
                task = asyncio.wrap_future(future)
            task.add_done_callback(lambda t: self._log_task_done(name, t))
            return task
        except RuntimeError:
            coro.close()
            return None

    def _log_task_done(self, name: str, task: asyncio.Task[Any] | Future[Any]) -> None:
        return


    def stop_background_sync(self) -> None:
        self._shutdown = True
        for task in (self._autoplay_task, self._poll_task, self._event_task, self._status_task):
            if task is None or task.done():
                continue
            try:
                task.cancel()
            except RuntimeError:
                # 事件循环已被关闭（shutdown 撞上 NEKO 超时强杀时会出现），忽略。
                pass
        self._autoplay_task = None
        self._poll_task = None
        self._event_task = None
        self._status_task = None
        self._stop_owner_loop()


def _combat_deadlock_signature(snapshot: dict[str, Any]) -> str:
    """战斗死锁判据用的**局面指纹**（手牌 card_id 集合 / 能量 / 敌人血量）。

    ★ 为什么把它从 `heuristic_planner._combat_deadlock_signature` 提到这里：
      t11 修复需要 `loop_runner` 在**取回计划的那一瞬间**记下当时的局面指纹
      （`plan_fetch_signature`），而 planner 侧那份是实例方法、loop_runner 拿不到。
      两份实现若各写一遍，将来必然漂移 ⇒ 提取成同一个模块级纯函数，**双方共用**，
      planner 的方法改为委托给它（保持既有调用点与测试不变）。

    取材刻意与 `_combat_plan_signature` 对齐（手牌/能量/敌人血量），
    这样"计划该重取"与"死锁该计数"依据同一个局面概念，不会互相打架。
    """
    raw_state = snapshot.get("raw_state") if isinstance(snapshot.get("raw_state"), dict) else {}
    combat = raw_state.get("combat") if isinstance(raw_state.get("combat"), dict) else {}
    hand = combat.get("hand") if isinstance(combat.get("hand"), list) else []
    player = combat.get("player") if isinstance(combat.get("player"), dict) else {}
    enemies = combat.get("enemies") if isinstance(combat.get("enemies"), list) else []

    card_ids = sorted(str(c.get("card_id") or "?") for c in hand if isinstance(c, dict))
    enemy_hp = sorted(
        str(e.get("current_hp") if e.get("current_hp") is not None else e.get("hp") or "?")
        for e in enemies
        if isinstance(e, dict)
    )
    return "|".join([
        ",".join(card_ids),
        str(player.get("energy") if player.get("energy") is not None else "?"),
        ",".join(enemy_hp),
    ])


def _combat_plan_signature(snapshot: dict[str, Any]) -> str:
    """Turn-stable fingerprint for WHOLE-TURN caching: 按 回合(run_id+turn) **与存活敌人集合** 键。

    GET /solver/plan 每次调用都会重跑 CombatSolver 搜索；插件现在"回合开始取一次、整回合复用"
    （见 solver_step_index），所以签名在回合内尽量不变，进入新回合才变化从而重查。
    注意：不能把整局战斗状态放进签名——那每出一张牌就变，起不到缓存作用。

    **但存活敌人集合必须进签名**：计划里的 target_index 是**绝对下标**。回合内死掉一只敌人后
    （史莱姆成群出现，这是常态），旧计划的下标就可能越界，于是 mod 以
    ``invalid_target: target_index is out of range`` 拒收。敌人集合变化就让缓存失效、按现场重算。
    HP 刻意排除在外：血量每出一张牌都变，放进去等于取消缓存。
    """
    raw = snapshot.get("raw_state") if isinstance(snapshot.get("raw_state"), dict) else {}
    run = raw.get("run") if isinstance(raw.get("run"), dict) else {}
    run_id = str(raw.get("run_id") or run.get("run_id") or run.get("runId") or run.get("id") or "")
    turn = raw.get("turn")
    if turn is None:
        combat = raw.get("combat") if isinstance(raw.get("combat"), dict) else {}
        turn = combat.get("turn")
    return hashlib.sha256(f"{run_id}|turn:{turn}|{_alive_enemy_key(raw)}".encode("utf-8")).hexdigest()


def _alive_enemy_key(raw_state: dict[str, Any]) -> str:
    """存活敌人的 (下标, 身份) 清单——只有"谁还活着"变了才算局面换了目标空间。"""
    combat = raw_state.get("combat") if isinstance(raw_state.get("combat"), dict) else {}
    enemies = combat.get("enemies") if isinstance(combat.get("enemies"), list) else []
    parts: list[str] = []
    for enemy in enemies:
        if not isinstance(enemy, dict):
            continue
        if enemy.get("is_alive") is False:
            continue
        parts.append(f"{enemy.get('index')}:{enemy.get('enemy_id') or enemy.get('move_id') or ''}")
    return ",".join(sorted(parts))


def _current_turn_steps(solver_plan: dict[str, Any]) -> list[dict[str, Any]] | None:
    line = solver_plan.get("line") if isinstance(solver_plan.get("line"), list) else None
    if not line or not isinstance(line[0], dict):
        return None
    steps = line[0].get("steps") if isinstance(line[0].get("steps"), list) else None
    return steps if isinstance(steps, list) else None


def _reusable_plan(
    cached: dict[str, Any] | None,
    cached_sig: Any,
    sig: Any,
    step_index: int,
    snapshot: dict[str, Any],
) -> tuple[dict[str, Any] | None, str]:
    """计划快照能不能继续按它执行？返回 (可复用的计划 或 None, 不能复用时的原因)。

    抽成纯函数是为了可测：这是"算一次 → 按快照执行 → 局面不一致就重算"这条口径的**全部判据**。
    """
    if not cached or cached_sig != sig:
        return None, "回合或敌人组合变了"
    steps = _current_turn_steps(cached)
    if steps is None:
        return None, "快照里没有可执行步骤"
    if step_index >= len(steps):
        return None, "快照步骤已用尽"
    ok, why = _plan_step_is_executable(steps[step_index], snapshot)
    if not ok:
        return None, why
    return cached, ""


def _plan_step_is_executable(step: dict[str, Any], snapshot: dict[str, Any]) -> tuple[bool, str]:
    """快照里的这一步，在**当前局面**下还能照做吗？

    这就是玩家要的"每次行动后用当前局面和快照对比"：
    一致 -> 继续按快照走；不一致 -> 丢掉快照重新算。

    为什么不能只看"回合签名没变"：抽牌/弃牌/消耗会让**手牌位置漂移**，步进下标会指到别的牌上；
    敌人死了会让目标失效。这些都不会改变回合号，但足以让快照里的下一步变成非法动作。

    判据（保守：拿不准就说"能执行"，让原有的动作层兜底）：

    - `play_card`：优先按 `card_id` 找当前手牌里**可打出**的那张（位置会漂，id 不会）；
      只有 `card_index` 时校验下标在范围内且可打出；牌需要目标时校验 `target_index` 仍在
      `valid_target_indices` 里。
    - `use_potion`：该药水仍在且 `can_use`。
    - `end_turn`：`end_turn` 仍在可用动作里（否则说明时序还没轮到我们）。
    - 其它步骤（选牌等）：交给下层的选牌界面逻辑，不在这里判。
    """
    if not isinstance(step, dict):
        return True, ""
    kind = str(step.get("kind") or step.get("action") or "").strip().lower()
    raw_state = snapshot.get("raw_state") if isinstance(snapshot.get("raw_state"), dict) else {}
    combat = raw_state.get("combat") if isinstance(raw_state.get("combat"), dict) else {}
    if kind == "play_card":
        hand = [card for card in (combat.get("hand") if isinstance(combat.get("hand"), list) else []) if isinstance(card, dict)]
        card_id = str(step.get("card_id") or "")
        target = step.get("target_index")
        if card_id:
            card = next(
                (item for item in hand if str(item.get("card_id") or "") == card_id and bool(item.get("playable", True))),
                None,
            )
            if card is None:
                return False, f"手牌里已没有可打出的 {card_id}"
        else:
            index = step.get("card_index")
            if isinstance(index, int) and not isinstance(index, bool):
                if not (0 <= index < len(hand)):
                    return False, f"手牌下标 {index} 越界（现在 {len(hand)} 张）"
                if not bool(hand[index].get("playable", True)):
                    return False, f"手牌[{index}] 当前不可打出"
                card = hand[index]
            else:
                card = None
        if card is not None and target is not None:
            valid = card.get("valid_target_indices")
            if isinstance(valid, list) and valid:
                try:
                    if int(target) not in {int(item) for item in valid}:
                        return False, f"目标 {target} 已失效"
                except (TypeError, ValueError):
                    pass
        return True, ""
    if kind == "use_potion":
        potion_id = str(step.get("card_id") or step.get("potion_id") or "")
        if not potion_id:
            return True, ""
        run = raw_state.get("run") if isinstance(raw_state.get("run"), dict) else {}
        potions = run.get("potions") if isinstance(run.get("potions"), list) else []
        usable = any(
            isinstance(potion, dict)
            and str(potion.get("potion_id") or "") == potion_id
            and bool(potion.get("can_use"))
            for potion in potions
        )
        return (True, "") if usable else (False, f"药水 {potion_id} 当前不可用")
    if kind == "end_turn":
        actions = snapshot.get("available_actions") if isinstance(snapshot.get("available_actions"), list) else []
        available = {
            str(action.get("type") or action.get("name") or "")
            for action in actions
            if isinstance(action, dict)
        }
        return (True, "") if "end_turn" in available else (False, "现在不能结束回合")
    return True, ""


__all__ = ["STS2LoopRunner"]
