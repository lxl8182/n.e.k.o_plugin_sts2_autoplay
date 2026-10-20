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

    async def tick(self, *, plan_combat: bool = False) -> dict[str, Any]:
        client = await self._service._ensure_companion_client()
        raw_state = await client.get_state()
        raw_actions = await client.get_available_actions()
        snapshot = normalize_snapshot(raw_state, raw_actions)
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
        if self._autoplay_task is None or self._task_done(self._autoplay_task):
            self._ensure_owner_loop()
            self._autoplay_task = self._create_task(self._autoplay_loop(), name="sts2-autoplay-loop")

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

    async def _poll_loop(self) -> None:
        while not self._shutdown:
            try:
                await self._service.refresh_state(trigger_sync=True)
                await self._service.reconcile_turn_control()
            except Exception as exc:
                self._service._mark_loop_error(exc)
            await asyncio.sleep(self._poll_interval())

    def _poll_interval(self) -> float:
        # With the event stream active the poll loop is only a fallback (slow), so it never races the
        # event-driven refresh; without SSE it keeps the historical reactive cadence.
        if self._service._cfg_use_event_stream():
            return self._service._cfg_fallback_poll_interval()
        return self._service._cfg_poll_interval(active=self._service._state.autoplay_state == "running")

    async def _event_loop(self) -> None:
        # Subscribe to the mod's /events/stream and re-read /state on a scene change (the mod already
        # diffs its state, so these events fire only when something actually changed). Keeps reconnecting
        # with exponential backoff; the slow _poll_loop remains the safety net.
        while not self._shutdown:
            try:
                client = await self._service._ensure_companion_client()
                if not hasattr(client, "subscribe_events"):
                    # No SSE-capable transport; nothing to do.
                    return
            except Exception:
                await asyncio.sleep(1.0)
                continue

            try:
                async for envelope in client.subscribe_events():
                    event_type = envelope.get("type", "") if isinstance(envelope, dict) else ""
                    if event_type == "stream_ready":
                        # Connection (re)established: reset backoff and pull the current state once.
                        self._event_backoff = 1.0
                        self._mark_sse_connected(True)
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
                        self._current_event_type = event_type
                        await self._maybe_event_refresh(force=False)
                    # heartbeat / unknown events -> ignore.
            except Exception as exc:
                self._service.logger.warning("SSE events stream dropped: %s", exc)
            finally:
                self._mark_sse_connected(False)

            # Disconnected: backoff and retry. Do not flood while the mod is briefly unavailable.
            await asyncio.sleep(self._event_backoff)
            self._event_backoff = min(self._event_backoff * 2, 30.0)

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
        while not self._shutdown:
            try:
                await self._service._sync_companion_ingress()
                await self._service._sync_execution_status()
                has_input = bool(self._service._companion_session.pending_messages() or self._service._ingress_callbacks
                                 or self._service._decision_failure_notice)
                if (self._service._state.autoplay_state != "running" or self._service._state.standby) and not has_input:
                    await asyncio.sleep(0.25)
                    continue
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
            except Exception as exc:
                # 单次错误退避；连续失败达到上限后暂停并投递原因。
                self._service._mark_loop_error(exc, autoplay=True)
                await asyncio.sleep(0.25 if self._service._decision_failure_notice
                                    else self._service._autoplay_retry_backoff())
                continue
            delay = (self._service._autoplay_retry_backoff() if result.get("status") in {"error", "retrying"}
                     else 0 if result.get("executed") and self._service._selected_steps
                     else 0.1 if result.get("status") == "pending" else 0.25)
            await asyncio.sleep(delay)

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
