from __future__ import annotations

import asyncio
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from plugin.plugins.sts2_autoplay import service as service_module
from plugin.plugins.sts2_autoplay.companion_session import CompanionSession
from plugin.plugins.sts2_autoplay.llm_planner import view_signature
from plugin.plugins.sts2_autoplay.planner_interface import PlannedOperation
from plugin.plugins.sts2_autoplay.service import STS2AutoplayService


def test_each_turn_waits_for_current_verified_message_and_passes_it_to_planner(monkeypatch):
    service = STS2AutoplayService(Mock(), lambda _: None)
    service._wait_for_turn_message = True
    service._state.autoplay_state = "running"
    clock = [100.0]
    monkeypatch.setattr(service_module, "time", lambda: clock[0])
    from plugin.plugins.sts2_autoplay import companion_session as session_module
    monkeypatch.setattr(session_module, "time", lambda: clock[0])
    view = {"screen": "COMBAT", "run_id": "one", "turn": 1, "actions": ["end_turn"],
            "combat": {"player": {"energy": 3}}}
    service._state.snapshot = {"agent_view": view}
    service._companion_session.observe(view)
    service._current_turn_message()
    monkeypatch.setattr(service, "refresh_state", AsyncMock())
    monkeypatch.setattr(service, "_sync_mod_gate", AsyncMock(return_value={"applied": True, "settled": True}))
    def read_memory(*, bucket_id, **kwargs):
        # 同步 SDK 读取不能占用插件事件循环；原话只在 default 时也能放行。
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            pass
        else:
            raise AssertionError("memory read blocked the event loop")
        return SimpleNamespace(items=records if bucket_id == "default" else [])
    service._sdk_bus = SimpleNamespace(memory=SimpleNamespace(get=read_memory))
    monkeypatch.setattr(service._catgirl_llm, "character_name", lambda: "猫娘")
    # 不可核实的工具转述不能放行；精确总线记录可以。
    records = []
    service._companion_session.remember("user", "旧工具转述")
    planner = AsyncMock(return_value=PlannedOperation("end_turn", {}, 1, "llm", "按要求防守"))
    monkeypatch.setattr(service._llm_planner, "plan", planner)
    monkeypatch.setattr(service, "execute_operation", AsyncMock(return_value={"executed": True, "status": "ok"}))
    assert asyncio.run(service.run_autoplay_step(force=True))["status"] == "waiting_for_message"
    planner.assert_not_awaited()
    clock[0] = 101.0
    records.append(SimpleNamespace(type="user_message", content="这轮先防守", timestamp=101.0))
    assert asyncio.run(service.run_autoplay_step())["executed"]
    assert planner.call_args.args[0]["current_turn_message"]["content"] == "这轮先防守"
    clock[0] = 102.0
    view["turn"] = 2
    service._companion_session.observe(view)
    assert asyncio.run(service.run_autoplay_step(force=True))["status"] == "waiting_for_message"
    assert planner.await_count == 1


def test_same_decision_danmaku_and_completed_speech_precede_action_and_pause_blocks_it(monkeypatch):
    service = STS2AutoplayService(Mock(), lambda _: None)
    service._danmaku_enabled = True  # 该用例验证弹幕先于发言与动作；默认关闭是生产配置。
    view = {"screen": "EVENT", "run_id": "one", "event": {"options": [{"i": 0}, {"i": 1}]}}
    service._state.snapshot = {"agent_view": view}
    service._companion_session.observe(view)
    events = []
    host = SimpleNamespace(base_url="host", set_companion_status=AsyncMock(),
                          push_danmaku=AsyncMock(side_effect=lambda *a, **kw: events.append("host_danmaku")))
    catgirl = SimpleNamespace(base_url="catgirl", set_companion_status=AsyncMock(),
                             push_danmaku=AsyncMock(side_effect=lambda *a, **kw: events.append("catgirl_danmaku")))
    service._host_client, service._catgirl_client = host, catgirl
    monkeypatch.setattr(service, "refresh_state", AsyncMock())
    monkeypatch.setattr(service, "_read_companion_dialogue", AsyncMock())
    async def speak(*args):
        events.append("speech_completed")
        return {"speech_id": "one", "audio_completed": True}
    monkeypatch.setattr(service_module, "speak_current_turn", speak)
    monkeypatch.setattr(service, "_execute_operation", AsyncMock(side_effect=lambda op: events.append("action") or {"executed": True}))
    operation = {"source": "llm", "action_type": "choose_event_option", "kwargs": {"option_index": 0},
                 "speech": "我选这个", "view_signature": view_signature(view), "control_mode": "program",
                 "session_generation": service._companion_session.generation,
                 "control_generation": 0, "guidance_generation": 0}
    assert asyncio.run(service.execute_operation(operation))["executed"]
    assert events == ["host_danmaku", "catgirl_danmaku", "speech_completed", "action"]
    async def paused_speak(*args):
        service.pause_autoplay()
        return {"speech_id": "two", "audio_completed": True}
    monkeypatch.setattr(service_module, "speak_current_turn", paused_speak)
    events.clear()
    operation = {**operation, "kwargs": {"option_index": 1}, "speech": "我换另一个"}
    operation.pop("plan_revision")
    operation.pop("step_id")
    assert not asyncio.run(service.execute_operation(operation))["executed"]
    assert "action" not in events


def test_action_history_retains_card_target_and_energy_before_and_after():
    session = CompanionSession()
    before = {"screen": "COMBAT", "run_id": "one", "turn": 1,
              "combat": {"player": {"energy": 3, "cards_played_this_turn": 0},
                         "hand": [{"i": 0, "line": "痛击 [2费]"}],
                         "enemies": [{"i": 0, "name": "海洋混混", "hp": "85/101"}]}}
    session.observe(before)
    after = deepcopy(before)
    after["combat"]["player"] = {"energy": 1, "cards_played_this_turn": 1}
    session.record_action({"action_type": "play_card", "kwargs": {"card_index": 0, "target_index": 0},
                           "reason": "按玩家要求先易伤"}, anchor=session.anchor(), before=before, after=after,
                          result={"status": "ok", "executed": True})
    session.observe(after)
    after["combat"]["player"]["energy"] = 0
    history = session.context()
    action = history["recent_actions"][0]
    assert action["card"]["line"] == "痛击 [2费]"
    assert action["target"]["name"] == "海洋混混"
    assert action["before_player"]["energy"] == 3
    assert action["outcome"]["player"]["energy"] == 1
    assert [item["player"]["energy"] for item in history["recent_observations"]] == [3, 1]


# ---- 出牌节奏开关（等发言 / 全自动）------------------------------------------------


def test_reconcile_starts_autoplay_in_full_auto_mode(monkeypatch):
    """全自动模式下，一进局就该自己开打（不等发言）；但主菜单/选人不启动，手动 paused 也不覆盖。"""
    service = STS2AutoplayService(Mock(), lambda _: None)
    service._wait_for_turn_message = False
    service._state.control_mode = "program"
    monkeypatch.setattr(service, "_sync_mod_gate", AsyncMock(return_value={"applied": True, "settled": True}))
    started = []
    monkeypatch.setattr(service._loop_runner, "start_autoplay", lambda: started.append(True))

    # 局内（地图屏）：应启动。
    service._state.snapshot = {"screen": "MAP", "run_id": "run-1"}
    service._state.autoplay_state = "idle"
    asyncio.run(service.reconcile_turn_control())
    assert started == [True], "全自动模式下进局应自动开打"

    # 主菜单/选人：玩家手动控制，不启动。
    started.clear()
    service._state.autoplay_state = "idle"
    service._state.snapshot = {"screen": "CHARACTER_SELECT", "run_id": "run-1"}
    asyncio.run(service.reconcile_turn_control())
    assert started == [], "选人界面不该自动开打"

    # 手动 paused：玩家显式暂停优先，不擅自恢复。
    service._state.snapshot = {"screen": "COMBAT", "run_id": "run-1"}
    service._state.autoplay_state = "paused"
    service._state.pause_reason = "user"
    asyncio.run(service.reconcile_turn_control())
    assert started == [], "玩家手动暂停不该被自动恢复"


def test_non_contiguous_announced_steps_do_not_trigger_false_correction():
    # 2026-10-17 20:56:11：初始发言点名 0/1/3，跳过未点名的电击+；完成第0步后
    # 承诺的 [寒流, 防御] 在计划里不再连续，被误判为计划改变而说“改一下”。
    session = CompanionSession()
    view = {"screen": "COMBAT", "run_id": "r", "turn": 1,
            "combat": {"hand": [{"i": 0, "line": "热修复"}, {"i": 1, "line": "寒流"},
                                {"i": 2, "line": "电击+"}, {"i": 3, "line": "防御"}]}}
    session.observe(view)
    operation = {"action_type": "play_card", "kwargs": {"card_index": 0},
                 "speech": "先热修复，再寒流，防御收尾", "speech_step_ids": [0, 1, 3],
                 "continuation": [{"action_type": "play_card", "kwargs": {"card_index": 1}},
                                  {"action_type": "play_card", "kwargs": {"card_index": 2}},
                                  {"action_type": "play_card", "kwargs": {"card_index": 3}}]}
    session.accept_plan(operation, view)
    session.announce_plan(operation, operation["speech"], "initial")
    session.complete_step(operation)
    assert len(session.announced_plan["remaining_steps"]) == 2
    assert session.speech_kind() == "none"


def test_plan_lifecycle_preserves_promises_and_consumes_each_step_once():
    session = CompanionSession()
    view = {"screen": "COMBAT", "run_id": "r", "turn": 1,
            "combat": {"hand": [{"i": 0, "line": "防御"}, {"i": 1, "line": "打击"}]}}
    session.observe(view)
    operation = {"action_type": "play_card", "kwargs": {"card_index": 0}, "speech": "先防御再打击",
                 "speech_step_ids": [0, 1], "continuation": [{"action_type": "play_card",
                    "kwargs": {"card_index": 0}, "expected_view": {**view,
                        "combat": {"hand": [{"i": 0, "line": "打击"}]}}}]}
    session.accept_plan(operation, view)
    revision = operation["plan_revision"]
    session.announce_plan(operation, operation["speech"], "initial")
    session.record_action(operation, anchor=session.anchor(), before=view, after=view,
                          result={"status": "ok", "executed": True})
    session.complete_step(operation)
    assert session.active_plan["revision"] == revision
    assert len(session.active_plan["remaining_steps"]) == 1
    assert len(session.active_plan["completed_steps"]) == 1
    assert session.announced_plan["remaining_steps"][0]["description"]["card"] == "打击"
    session.invalidate_plan("手牌变化")
    shifted = {**view, "combat": {"hand": [{"i": 0, "line": "打击"}]}}
    same = {"action_type": "play_card", "kwargs": {"card_index": 0}}
    session.accept_plan(same, shifted)
    assert session.speech_kind() == "none"  # 重算仍履行相同安排，不重复说话。
    session.invalidate_plan("目标已死亡")
    replacement = {"action_type": "end_turn", "kwargs": {}}
    session.accept_plan(replacement, shifted)
    assert session.speech_kind() == "correction"
    assert session.replacement_reason == "目标已死亡"
    session.announce_plan(replacement, "目标没了，我结束回合", "correction")
    assert session.speech_kind() == "none"
    session.observe({**view, "turn": 2})
    session.accept_plan({"action_type": "end_turn", "kwargs": {}}, {**view, "turn": 2})
    assert session.speech_kind() == "initial"
    session.observe({**view, "run_id": "another"})
    assert not session.announced_plan and not session.active_plan["remaining_steps"]


def test_pending_completion_and_plan_only_changes_reach_chat_context(monkeypatch):
    service = STS2AutoplayService(Mock(), lambda _: None)
    service._state.autoplay_state = "running"
    view = {"screen": "COMBAT", "run_id": "r", "turn": 1,
            "combat": {"player": {"energy": 2}, "hand": [{"i": 0, "line": "防御", "playable": True}]}}
    raw = {"in_combat": True, "turn": 1, "combat": {"hand": [{"entity_id": "c1"}],
            "player": {"cards_played_this_turn": 0}}}
    snapshot = {"agent_view": view, "raw_state": raw, "action_registry": [{"type": "play_card"}]}
    service._state.snapshot = snapshot
    service._companion_session.observe(view)
    operation = {"action_type": "play_card", "kwargs": {"card_index": 0, "expected_card_entity_id": "c1"},
                 "source": "llm", "continuation": [{"action_type": "end_turn", "kwargs": {},
                    "expected_view": {"screen": "COMBAT", "turn": 1}}]}
    service._companion_session.accept_plan(operation, view)
    client = SimpleNamespace(execute_action=AsyncMock(return_value={"status": "pending", "stable": False}))
    result = asyncio.run(service._action_engine.execute(client, snapshot, operation))
    assert result["status"] == "pending" and not result["executed"] and result["dispatched"]
    service._companion_session.record_action(operation, anchor=service._companion_session.anchor(),
        before=view, after=view, result=result)
    service._pending_action = {"operation": operation, "anchor": service._companion_session.anchor(),
                              "before": deepcopy(snapshot), "record": service._companion_session.actions[-1],
                              "submitted_at": service_module.time()}
    push = Mock()
    monkeypatch.setattr(service, "_push_host_context", push)
    service._deliver_host_context(snapshot)
    service._deliver_host_context(snapshot)
    assert push.call_count == 1 and "pending" in push.call_args.args[0]
    view["combat"]["player"]["energy"] = 1  # 仅有其他局面变化，不能重发或宣称完成。
    assert service._resolve_pending_action(snapshot)["status"] == "pending"
    assert asyncio.run(service.execute_operation(operation))["status"] == "pending"
    assert client.execute_action.await_count == 1
    raw["combat"]["hand"] = []
    raw["combat"]["player"]["cards_played_this_turn"] = 1
    view["combat"]["hand"] = []
    assert service._resolve_pending_action(snapshot)["executed"]
    assert service._companion_session.actions[-1]["status"] == "completed"
    assert service._selected_steps[0]["action_type"] == "end_turn"
    service._deliver_host_context(snapshot)
    assert "completed" in push.call_args.args[0] and "end_turn" in push.call_args.args[0]
    count = push.call_count
    service._companion_session.accept_plan({"action_type": "choose_event_option", "kwargs": {"option_index": 0}}, view)
    service._deliver_host_context(snapshot)
    assert push.call_count == count + 1  # 画面相同、只换计划也要同步。
    monkeypatch.setattr(service, "refresh_state", AsyncMock())
    monkeypatch.setattr(service, "_read_companion_dialogue", AsyncMock())
    context = asyncio.run(service.chat_companion())
    assert context["session"]["decision_state"] == service._companion_session.decision_state()
    assert context["session"]["recent_actions"][-1]["executed"]
    assert "expected_view" not in str(context["session"]["decision_state"])
    uncertain = {"action_type": "end_turn", "kwargs": {}, "source": "llm"}
    service._companion_session.record_action(uncertain, anchor=service._companion_session.anchor(),
        before=view, after=view, result={"status": "pending", "executed": False})
    service._pending_action = {"operation": uncertain, "anchor": service._companion_session.anchor(),
                              "before": deepcopy(snapshot), "record": service._companion_session.actions[-1],
                              "submitted_at": service_module.time() - 11}
    assert service._resolve_pending_action(snapshot)["status"] == "unconfirmed"
    assert service._pending_action is None
    validated, feedback = service._validate_companion_action(snapshot, uncertain)
    assert validated is None and "请选择其他操作" in feedback
    assert service._companion_session.actions[-1]["executed"] is False


def test_pending_action_registered_before_refresh_failure(monkeypatch):
    """G01：Mod 已接收动作后刷新失败，也要保留待确认记录（否则同一动作会重复派发）。"""
    service = STS2AutoplayService(Mock(), lambda _: None)
    view = {"screen": "COMBAT", "run_id": "r", "turn": 1,
            "combat": {"player": {"energy": 2}, "hand": [{"i": 0, "line": "打击", "playable": True}]}}
    snapshot = {"agent_view": view, "raw_state": {"in_combat": True, "turn": 1, "combat": {}},
                "action_registry": [{"type": "play_card"}]}
    service._state.snapshot = snapshot
    service._companion_session.observe(view)
    operation = {"action_type": "play_card", "kwargs": {"card_index": 0}, "source": "llm"}
    monkeypatch.setattr(service, "_companion_client", lambda: None)
    monkeypatch.setattr(service, "_action_engine", SimpleNamespace(execute=AsyncMock(return_value={
        "status": "pending", "executed": False, "action_id": "a1", "message": "等待现场确认完成。"})))
    monkeypatch.setattr(service, "refresh_state", AsyncMock(side_effect=RuntimeError("连接断开")))
    monkeypatch.setattr(service, "_deliver_host_context", Mock())
    result = asyncio.run(service._execute_operation(operation))
    assert result["status"] == "pending"
    assert service._pending_action is not None
    assert service._pending_action["action_id"] == "a1"
    assert service._companion_session.actions[-1]["status"] == "pending"


def test_action_engine_maps_mod_result_states_and_echoes_action_id():
    """G02：Mod 的 canceled/unknown 不能被当成成功；action_id 原样带给 Mod 并回传。"""
    engine = STS2AutoplayService(Mock(), lambda _: None)._action_engine
    engine.validate = lambda snapshot, operation: {"action_type": "play_card", "kwargs": {}, "source": "llm"}
    operation = {"action_type": "play_card", "kwargs": {}, "source": "llm", "_action_id": "abc"}
    for mod_status, expected, executed in (("canceled", "canceled", False), ("unknown", "unknown", False),
                                           ("pending", "pending", False), ("completed", "ok", True)):
        client = SimpleNamespace(execute_action=AsyncMock(return_value={"status": mod_status}))
        result = asyncio.run(engine.execute(client, {}, operation))
        assert result["status"] == expected and result["executed"] is executed
        assert client.execute_action.await_args.kwargs["action_id"] == "abc"


def test_pending_question_lifecycle_links_answer_and_expiry():
    """C03：问题可被回答/过期，回答按消息 id 关联原问题。"""
    session = CompanionSession()
    session.observe({"screen": "COMBAT", "run_id": "r", "turn": 1})
    question = session.ask_question("要不要把药水留到Boss？", required=True, window=10,
                                    operation={"action_type": "end_turn"})
    assert question["status"] == "open"
    assert session.context()["pending_question"]["text"] == "要不要把药水留到Boss？"
    session.answer_pending_question([{"id": "7"}])
    assert session.pending_question["status"] == "answered"
    assert session.pending_question["answered_by"] == ["7"]
    assert session.pending_question_context() == {}
    second = session.ask_question("先打哪只？", required=False, window=5)
    session.expire_pending_question()
    assert second["id"] == session.pending_question["id"]
    assert session.pending_question["status"] == "expired"
