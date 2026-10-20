from __future__ import annotations

import asyncio
import json
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from plugin.plugins.sts2_autoplay.catgirl_memory import CatgirlMemoryStore
from plugin.plugins.sts2_autoplay.llm_planner import DecisionContextChanged, STS2LLMPlanner
from plugin.plugins.sts2_autoplay.planner_interface import PlannedOperation
from plugin.plugins.sts2_autoplay.service import STS2AutoplayService
from plugin.plugins.sts2_autoplay.snapshot_normalizer import normalize_snapshot
from plugin.plugins.sts2_autoplay.transport_client import STS2ActionRejected, STS2TransportError
from plugin.plugins.sts2_autoplay import STS2AutoplayPlugin


@pytest.mark.asyncio
async def test_status_poll_returns_data_without_host_notification():
    plugin = object.__new__(STS2AutoplayPlugin)
    plugin._service = SimpleNamespace(get_status=AsyncMock(return_value={"summary": "等待开局"}))
    plugin._dispatcher = Mock()
    result = await plugin.sts2_get_status()
    assert result.unwrap() == {"summary": "等待开局"}
    plugin._service.get_status.assert_awaited_once()
    plugin._dispatcher.push_status_feedback.assert_not_called()
    reports = []
    service = STS2AutoplayService(Mock(), reports.append)
    service._emit_status()
    service._emit_status()
    assert len(reports) == 1
    service._state.decision_errors = 1
    service._emit_status()
    assert len(reports) == 2 and reports[-1]["decision_errors"] == 1
    service._reported_status_at -= 4
    service._emit_status()
    assert len(reports) == 3


def prepare(monkeypatch, view=None):
    service = STS2AutoplayService(Mock(), lambda _: None)
    service._catgirl_memory = CatgirlMemoryStore()
    service._state.autoplay_state = "running"
    service._state.transport_state = "connected"
    service._client = Mock()
    view = view or {"screen": "EVENT", "run_id": "one", "turn": None,
                    "actions": ["choose_event_option"], "event": {"options": [{"i": 5}]}}
    snapshot = normalize_snapshot({"screen": view["screen"], "run_id": view["run_id"], "agent_view": view},
                                  {"actions": [{"name": name, "requires_index": name != "end_turn"}
                                               for name in view["actions"]]})
    snapshot["action_registry"] = service._action_registry.build(snapshot)
    service._state.snapshot = snapshot
    service._companion_session.observe(view)

    async def refresh(**kwargs):
        # 状态读成功不应清空决策健康计数。
        service._state.touch_success()
        service._companion_session.observe(service._state.snapshot["agent_view"])
        return {"snapshot": service._state.snapshot}

    monkeypatch.setattr(service, "refresh_state", refresh)
    monkeypatch.setattr(service, "_sync_mod_gate", AsyncMock(return_value={"applied": True, "settled": True}))
    history = Mock()
    history.json.return_value = ""
    monkeypatch.setattr("plugin.plugins.sts2_autoplay.service.httpx.AsyncClient.get", AsyncMock(return_value=history))
    monkeypatch.setattr(service, "_push_proactive_to_host", Mock())
    monkeypatch.setattr(service, "_sync_background_polling", Mock())
    monkeypatch.setattr(service._loop_runner, "start_autoplay", Mock())
    monkeypatch.setattr(service, "execute_operation", AsyncMock(return_value={"status": "ok", "executed": True}))
    return service


@pytest.mark.parametrize("old_failure", [False])
@pytest.mark.parametrize("transport_state", ["error", "disconnected"])
def test_game_disconnect_keeps_pending_chat_and_does_not_dispatch(monkeypatch, old_failure, transport_state):
    service = prepare(monkeypatch)
    service._ingress_active = True
    service.pause_autoplay(reason="decision_failure")
    service._companion_session.remember("user", "在吗")
    message = service._companion_session.pending_messages()[0]
    message["request_id"] = "offline-chat"
    service._state.transport_state = transport_state
    service._state.last_error = "游戏连接失败"
    if old_failure:
        service._state.decision_error = service._state.last_error
        service._state.decision_failed_revision = service._companion_session.dialogue_revision
        service._state.decision_errors = 3
    monkeypatch.setattr(service, "refresh_state", AsyncMock(side_effect=STS2TransportError("游戏连接失败")))
    monkeypatch.setattr(service, "_read_companion_dialogue", AsyncMock())
    monkeypatch.setattr(service, "_publish_companion_reply", AsyncMock(return_value={"text_sent": True}))
    contexts = []
    async def plan(context, **kwargs):
        contexts.append(context)
        kwargs["route_messages"]({message["id"]: "chat"})
        return PlannedOperation("", {}, 1, "llm", reply="在的，游戏连接断了，我还在听你说话。", plan_update="keep")
    monkeypatch.setattr(service._llm_planner, "plan", plan)
    result = asyncio.run(service.run_autoplay_step())
    assert not result["executed"] and not service._companion_session.pending_messages()
    assert contexts[0]["game_connection"] == {"connected": False, "error": "游戏连接失败", "observation": "last_known"}
    assert not contexts[0]["actions_enabled"] and not contexts[0]["agent_view"]["actions"]
    assert service._state.autoplay_state == "paused" and service._state.transport_state == transport_state
    service._sync_mod_gate.assert_not_awaited()
    service.execute_operation.assert_not_awaited()
    service._publish_companion_reply.assert_awaited_once()


def test_reload_without_game_recovers_outstanding_input_from_checkpoint(monkeypatch, tmp_path):
    service = prepare(monkeypatch)
    session = service._companion_session
    session.remember("user", "在吗")
    session.user_requests[-1]["request_id"] = "not-answered"
    session.journal.directory = str(tmp_path)
    session.journal.save_checkpoint({"character_name": "小铃", "session": session.context(),
        "control_state": {"state": "paused", "pause_reason": "decision_failure"}})
    service._state.snapshot = {}
    session.facts.clear()
    session.user_requests.clear()
    session.journal.data = {}
    service._ingress_enabled = True
    monkeypatch.setattr(service._catgirl_llm, "character_name", lambda: "小铃")
    monkeypatch.setattr(service, "_companion_host_request", AsyncMock(return_value={"ok": True, "persona": "小铃", "inputs": [], "checkpoint": {}}))
    asyncio.run(service._sync_companion_ingress())
    assert service._ingress_active and session.pending_messages()[0]["request_id"] == "not-answered"
    assert session.facts["run_id"] == "one" and not service._state.snapshot["agent_view"]["actions"]
    assert service._state.autoplay_state == "paused" and not service._selected_steps


def test_main_menu_release_waits_for_undelivered_reply(monkeypatch):
    # 2026-10-17 20:24 卡死：局末复盘已讨论完，主菜单下先release会清掉输出所有权，
    # 已生成但未送达的回复永远等不到所有权。有未送达回复时先保持接管，送达后再交回。
    service = prepare(monkeypatch, view={"screen": "MAIN_MENU", "run_id": "run_unknown",
                                         "turn": None, "actions": []})
    service._ingress_enabled = True
    service._ingress_active = True
    monkeypatch.setattr(service._catgirl_llm, "character_name", lambda: "小铃")
    monkeypatch.setattr(service, "_companion_mode_active", lambda: True)
    release = AsyncMock()
    monkeypatch.setattr(service, "_release_companion_ingress", release)
    monkeypatch.setattr(service, "_companion_host_request",
                        AsyncMock(return_value={"ok": True, "persona": "小铃", "inputs": []}))

    service._companion_session.input_results.append({"ids": ["req-1"], "reply": "明白", "text_sent": False})
    asyncio.run(service._sync_companion_ingress())
    release.assert_not_awaited()
    assert service._ingress_active

    service._companion_session.input_results.clear()
    asyncio.run(service._sync_companion_ingress())
    release.assert_awaited_once()


def test_status_heartbeat_continues_during_long_decision(monkeypatch):
    service = prepare(monkeypatch)
    runner = service._loop_runner
    calls = []
    async def sync():
        calls.append(service.execution_status()["state"])
        if len(calls) == 3:
            runner._shutdown = True
    monkeypatch.setattr(service, "_sync_execution_status", sync)
    monkeypatch.setattr("plugin.plugins.sts2_autoplay.loop_runner.asyncio.sleep", AsyncMock())
    service._execution_phase = {"state": "deciding", "text": "正在决策", "detail": ""}
    async def run():
        blocked = asyncio.create_task(asyncio.Event().wait())
        try:
            await runner._status_loop()
            assert not blocked.done()
        finally:
            blocked.cancel()
    asyncio.run(run())
    assert calls == ["deciding"] * 3


def test_condition_request_controls_dispatch_and_shared_status(monkeypatch):
    view = {"screen": "COMBAT", "run_id": "one", "turn": 1, "actions": ["end_turn"],
            "combat": {"player": {"energy": 0}, "hand": []}}
    service = prepare(monkeypatch, view)
    monkeypatch.setattr(service, "_read_companion_dialogue", AsyncMock())
    raw = service._state.snapshot["raw_state"]
    raw.update(in_combat=True, turn=1, combat={"player": {"energy": 0}, "hand": [],
        "players": [{"is_local": True, "ready_to_end_turn": False}, {"is_local": False, "block": 0}]})
    service._companion_session.remember("user", "等我有5点格挡再结束回合")
    message = service._companion_session.pending_messages()[0]
    operation = PlannedOperation("end_turn", {}, 1, "llm", speech="等你有5点格挡后我再结束。",
        requirement_updates=[{"op": "add", "source_message_id": message["id"],
            "text": message["content"], "scope": "turn", "strength": "hard",
            "behavior": {"kind": "wait", "action_types": ["end_turn"]},
            "conditions": [{"kind": "player", "subject": "master", "field": "block", "op": "gte", "value": 5}]}])

    async def plan(*args, **kwargs):
        service._companion_session.resolve_messages({message["id"]: "guidance"})
        return operation

    monkeypatch.setattr(service._llm_planner, "plan", plan)
    result = asyncio.run(service.run_autoplay_step())
    assert result["status"] == "waiting_for_condition"
    service.execute_operation.assert_not_awaited()
    assert service.execution_status()["state"] == "waiting_for_condition"
    service._state.transport_state = "disconnected"
    assert service.execution_status()["state"] == "disconnected"
    service._state.transport_state = "connected"
    service.pause_autoplay()
    assert service.execution_status()["state"] == "paused"
    raw["combat"]["players"][1]["block"] = 5
    assert service._companion_session.refresh_requirements(service._state.snapshot)
    assert service.execution_status()["state"] == "paused"
    service.resume_autoplay()
    operation = PlannedOperation("end_turn", {}, 1, "llm", speech="现在结束回合。")
    assert asyncio.run(service.run_autoplay_step())["executed"]
    service.execute_operation.assert_awaited_once()
    assert service._companion_session.active_requirements()[0]["status"] == "active"
    assert service.execution_status()["state"] != "waiting_for_condition"
    raw["combat"]["players"][1]["block"] = 0
    service._companion_session.refresh_requirements(service._state.snapshot)
    service._companion_session.remember("user", "不用等我了，现在结束回合")
    cancel = PlannedOperation("end_turn", {}, 1, "llm", requirement_updates=[{
        "op": "cancel", "id": service._companion_session.active_requirements()[0]["id"],
        "source_message_id": service._companion_session.pending_messages()[-1]["id"]}])
    assert service._validate_companion_action(service._state.snapshot, cancel)[0] is not None
    assert service._companion_session.active_requirements()[0]["status"] == "waiting"
    service._companion_session.apply_requirement_updates(cancel.requirement_updates, service._state.snapshot)
    assert not service._companion_session.active_requirements()


def test_stale_solver_decisions_preserve_pending_input_without_failure_pause(monkeypatch):
    service = prepare(monkeypatch)
    service._state.decision_errors = 2
    service._companion_session.remember("user", "先打小怪")
    planner = AsyncMock(side_effect=DecisionContextChanged("求解期间现场或要求改变，刷新后重判"))
    monkeypatch.setattr(service._llm_planner, "plan", planner)
    monkeypatch.setattr(service, "_read_companion_dialogue", AsyncMock())
    for _ in range(3):
        result = asyncio.run(service.run_autoplay_step())
        assert result["status"] == "stale" and not result["executed"]
        assert service._state.autoplay_state == "running" and service._state.decision_errors == 2
        assert service._companion_session.pending_messages()[0]["content"] == "先打小怪"
    service.execute_operation.assert_not_awaited()
    assert planner.await_count == 3


def test_decision_failures_backoff_recover_and_respect_pause(monkeypatch):
    service = prepare(monkeypatch)
    planner = AsyncMock(side_effect=RuntimeError("模型超时"))
    monkeypatch.setattr(service._llm_planner, "plan", planner)
    notice = AsyncMock(return_value={"text_sent": True})
    monkeypatch.setattr(service, "_publish_companion_reply", notice)
    for count in range(1, 4):
        service._state.retry_at = 0
        result = asyncio.run(service.run_autoplay_step())
        assert result["status"] == ("paused" if count == 3 else "error")
        assert service._state.autoplay_state == ("paused" if count == 3 else "running")
        assert service._state.decision_errors == count
        assert asyncio.run(service.run_autoplay_step())["status"] == ("paused" if count == 3 else "retrying")
    assert notice.await_count == 1 and "模型超时" in notice.await_args.args[0]
    assert "连续3轮" in notice.await_args.args[0]
    assert "conversations" in service._state.input_warning
    assert planner.await_count == 3
    asyncio.run(service.run_autoplay_step())
    assert planner.await_count == 3 and notice.await_count == 1
    service.resume_autoplay()
    planner.side_effect = None
    planner.return_value = PlannedOperation("choose_event_option", {"option_index": 5}, 1, "llm")
    assert asyncio.run(service.run_autoplay_step())["executed"]
    assert service._state.decision_errors == 0
    service.pause_autoplay()
    assert not asyncio.run(service.run_autoplay_step())["executed"]
    assert planner.await_count == 4
    service._mark_loop_error(STS2ActionRejected("过期", code="invalid_action"))
    assert service._state.autoplay_state == "paused"
    assert service._last_decision_key == ""
    assert service._state.force_replan_requested


def test_failure_pause_holds_old_inputs_but_new_instruction_can_resume(monkeypatch):
    service = prepare(monkeypatch)
    service._ingress_active = True
    service._companion_session.remember("user", "先保血量")
    service._ingress_callbacks.append({"kind": "combat_turn", "content": "战斗事件"})
    planner = AsyncMock(side_effect=RuntimeError("输出缺少消息分类"))
    monkeypatch.setattr(service._llm_planner, "plan", planner)
    notice = AsyncMock(return_value={"text_sent": True})
    monkeypatch.setattr(service, "_publish_companion_reply", notice)
    monkeypatch.setattr(service, "_sync_companion_ingress", AsyncMock())
    for _ in range(3):
        service._state.retry_at = 0
        asyncio.run(service.run_autoplay_step())
    for _ in range(4):
        service._state.snapshot["agent_view"]["event"]["options"].append({"i": 9})
        assert asyncio.run(service.run_autoplay_step())["status"] == "paused"
        asyncio.run(service.reconcile_turn_control())
    assert planner.await_count == 3 and notice.await_count == 1
    assert service._state.autoplay_state == "paused"
    service._companion_session.remember("user", "继续")
    planner.side_effect = None
    planner.return_value = PlannedOperation("", {}, 1, "llm", reply="继续打。", plan_update="wait", control="resume")
    assert not asyncio.run(service.run_autoplay_step())["executed"]
    assert planner.await_count == 4 and service._state.autoplay_state == "running"
    assert service._state.decision_errors == 0


@pytest.mark.parametrize("text,control", [("继续 直接打", "resume"), ("不要继续", "")])
def test_explicit_resume_is_applied_before_reply_and_map_dispatch(monkeypatch, text, control):
    view = {"screen": "MAP", "run_id": "one", "actions": ["choose_map_node"],
            "map": {"local_vote": None, "options": [{"i": 0, "line": "Unknown (10,3) [votes:1]"}]}}
    service = prepare(monkeypatch, view)
    service._ingress_active = True
    monkeypatch.setattr(service, "_sync_companion_ingress", AsyncMock())
    service.pause_autoplay(reason="decision_failure")
    service._companion_session.remember("user", text)
    message = service._companion_session.pending_messages()[0]
    silent_control = {"action_type": None, "reply": "我保持暂停。" if not control else "我恢复自动行动，再跟你的地图票。",
                      "plan_update": "wait", "message_effects": {message["id"]: "guidance" if control else "chat"}}
    corrected = {**silent_control, "control": control}
    choose = {"action_type": "choose_map_node", "kwargs": {"option_index": 0}, "plan_update": "wait", "speech": "我跟着你选10,3。"}
    answers = iter([silent_control, corrected, choose] if control else [silent_control])
    requests = []
    planner = STS2LLMPlanner(Mock(), persona_provider=lambda: "小铃")
    monkeypatch.setattr(planner, "_ensure_llm", AsyncMock(return_value=object()))
    async def invoke(llm, messages):
        requests.append(deepcopy(messages))
        return json.dumps(next(answers), ensure_ascii=False)
    monkeypatch.setattr(planner, "_invoke_text", invoke)
    async def plan(context, **kwargs):
        return await planner.plan(context, validate=kwargs["validate"], route_messages=kwargs["route_messages"])
    monkeypatch.setattr(service._llm_planner, "plan", plan)
    async def publish(*args, **kwargs):
        assert service._state.autoplay_state == ("running" if control else "paused")
        return {"text_sent": True, "audio_completed": None}
    monkeypatch.setattr(service, "_publish_companion_reply", AsyncMock(side_effect=publish))
    assert not asyncio.run(service.run_autoplay_step())["executed"]
    service._publish_companion_reply.assert_awaited_once()
    payload = json.loads(requests[0][-1]["content"])
    assert payload["control_state"]["autoplay"] == "paused" and payload["control_request"] == control
    assert not service._companion_session.pending_messages()
    if control:
        assert len(requests) == 2 and 'control填"resume"' in requests[1][-1]["content"]
        assert asyncio.run(service.run_autoplay_step())["executed"]
        service.execute_operation.assert_awaited_once()
        operation = service.execute_operation.await_args.args[0]
        assert operation["action_type"] == "choose_map_node" and operation["kwargs"] == {"option_index": 0}
    else:
        service.execute_operation.assert_not_awaited()


def test_failure_pause_checkpoint_survives_reload_after_chat(monkeypatch):
    service = prepare(monkeypatch)
    service._ingress_enabled = service._ingress_active = True
    monkeypatch.setattr(service._catgirl_llm, "character_name", lambda: "小铃")
    service._companion_session.remember("user", "先保血")
    service._companion_session.user_requests[-1]["request_id"] = "old-input"
    service._state.decision_errors = 3
    service._state.decision_error = "动作格式无效"
    service._pause_after_decision_failures()
    # 暂停时问话已回答，仍要保留停手状态。
    service._note_decision_success()
    host = AsyncMock(return_value={"persona": "小铃", "inputs": []})
    monkeypatch.setattr(service, "_companion_host_request", host)
    asyncio.run(service._sync_companion_ingress())
    checkpoint = host.await_args.args[1]["checkpoint"]
    assert checkpoint["failure_pause"] is not None
    assert checkpoint["failure_pause"]["notice"]
    restored = prepare(monkeypatch)
    restored._ingress_enabled = True
    monkeypatch.setattr(restored._catgirl_llm, "character_name", lambda: "小铃")
    monkeypatch.setattr(restored, "_companion_host_request", AsyncMock(return_value={
        "persona": "小铃", "inputs": [{"request_id": "old-input", "content": "先保血", "timestamp": 1}],
        "checkpoint": checkpoint}))
    asyncio.run(restored._sync_companion_ingress())
    assert restored._state.autoplay_state == "paused"
    assert restored._state.pause_reason == "decision_failure"
    assert restored._state.decision_failed_revision == restored._companion_session.dialogue_revision
    assert restored._decision_failure_notice == checkpoint["failure_pause"]["notice"]
    monkeypatch.setattr(restored, "_publish_companion_reply", AsyncMock(return_value={"text_sent": True}))
    assert asyncio.run(restored.run_autoplay_step())["status"] == "paused"


def test_cached_success_without_progress_reports_once_but_votes_wait(monkeypatch):
    service = prepare(monkeypatch)
    monkeypatch.setattr(service._llm_planner, "plan", AsyncMock(
        return_value=PlannedOperation("choose_event_option", {"option_index": 5}, 1, "llm")))
    assert asyncio.run(service.run_autoplay_step())["executed"]
    for _ in range(service.NO_PROGRESS_REPEAT_LIMIT + 3):
        asyncio.run(service.run_autoplay_step())
    assert service._state.progress_repeat == 1 and not service._state.stuck_reason
    service._state.stuck_since -= service.NO_OPERATION_STUCK_SECONDS + 1
    asyncio.run(service.run_autoplay_step())
    assert "EVENT" in service._state.stuck_reason
    assert service._push_proactive_to_host.call_count == 1
    service._state.snapshot["agent_view"]["event"]["options"].append({"i": 8})
    asyncio.run(service.run_autoplay_step())
    assert not service._state.stuck_reason
    service._state.snapshot["agent_view"] = {"screen": "MAP", "run_id": "one",
                                              "actions": ["choose_map_node"], "map": {"local_vote": "1,2"}}
    for _ in range(service.NO_PROGRESS_REPEAT_LIMIT + 3):
        service._note_step_progress(service._state.snapshot, {"action_type": "choose_map_node"})
        service._note_no_operation(service._state.snapshot)
    assert not service._state.stuck_reason


@pytest.mark.parametrize("stage", ["waiting", "finished"])
def test_pending_event_choice_uses_event_progress_as_completion(monkeypatch, stage):
    service = prepare(monkeypatch)
    before = deepcopy(service._state.snapshot)
    event = {"event_id": "TINKER_TIME", "is_finished": False,
             "options": [{"index": 0, "title": "护具"}, {"index": 1, "title": "装置"}]}
    before["raw_state"]["event"] = event
    before["agent_view"]["event"] = event
    after = deepcopy(before)
    if stage == "next_options":
        after["raw_state"]["event"]["options"] = [{"index": 0, "title": "好奇"}, {"index": 1, "title": "专长"}]
        after["agent_view"]["event"] = deepcopy(after["raw_state"]["event"])
    elif stage == "finished":
        after["raw_state"]["event"].update(is_finished=True, options=[{"index": 0, "title": "继续"}])
        after["agent_view"]["event"] = deepcopy(after["raw_state"]["event"])
    operation = {"action_type": "choose_event_option", "kwargs": {"option_index": 1}}
    record = {}
    service._pending_action = {"operation": operation, "before": before, "record": record,
                               "anchor": service._companion_session.anchor(), "submitted_at": 0}
    result = service._resolve_pending_action(after)
    assert result["executed"] == (stage != "waiting")
    if stage == "next_options":
        assert service._validate_companion_action(after, operation)[0] is not None
    elif stage == "waiting":
        assert service._validate_companion_action(after, operation)[0] is None


@pytest.mark.parametrize("local_vote", [0, 1])
def test_shared_event_wait_keeps_chat_and_resumes_on_next_page(monkeypatch, local_vote):
    event = {"id": "shared", "is_shared": True, "page_index": 0, "finished": False,
             "local_vote": local_vote, "player_votes": [{"is_local": True, "option_index": local_vote}],
             "options": [{"i": 0}, {"i": 1}]}
    service = prepare(monkeypatch, {"screen": "EVENT", "run_id": "one",
                                   "actions": ["choose_event_option"], "event": event})
    monkeypatch.setattr(service, "_read_companion_dialogue", AsyncMock())
    monkeypatch.setattr(service, "_publish_companion_reply", AsyncMock(return_value={"text_sent": True}))
    async def plan(context, **kwargs):
        messages = service._companion_session.pending_messages()
        if messages:
            kwargs["route_messages"]({m["id"]: "chat" for m in messages})
            return PlannedOperation("", {}, 1, "llm", reply="我的票已经投好了，等你的选择。", plan_update="wait")
        return PlannedOperation("choose_event_option", {"option_index": 0}, 1, "llm")
    planner = AsyncMock(side_effect=plan)
    monkeypatch.setattr(service._llm_planner, "plan", planner)
    for _ in range(4):
        assert asyncio.run(service.run_autoplay_step())["status"] == "waiting_for_teammate"
        service._note_no_operation(service._state.snapshot)
    assert not service._state.stuck_reported
    assert "共享事件投票已确认" in service.execution_status()["detail"]
    planner.assert_not_awaited()
    service._companion_session.remember("user", "你在等什么")
    asyncio.run(service.run_autoplay_step())
    assert planner.await_count == 1 and not service._companion_session.pending_messages()
    service.execute_operation.assert_not_awaited()
    assert asyncio.run(service.run_autoplay_step())["status"] == "waiting_for_teammate"
    event.update(page_index=1, local_vote=None, player_votes=[], options=[{"i": 0}])
    assert not service._waiting_for_teammate(service._state.snapshot)
    assert asyncio.run(service.run_autoplay_step())["executed"]
    assert planner.await_count == 2


@pytest.mark.parametrize("own_vote", [False, True])
def test_pending_shared_event_confirms_own_vote_without_claiming_resolution(monkeypatch, own_vote):
    from plugin.plugins.sts2_autoplay import service as service_module
    monkeypatch.setattr(service_module, "time", lambda: 1.0)
    service = prepare(monkeypatch)
    before = deepcopy(service._state.snapshot)
    before["raw_state"]["event"] = {"event_id": "shared", "is_shared": True, "is_finished": False,
        "page_index": 0, "local_vote": None, "player_votes": [], "options": [{"index": 0}, {"index": 1}]}
    after = deepcopy(before)
    after["raw_state"]["event"].update(local_vote=1 if own_vote else None,
        player_votes=[{"is_local": own_vote, "option_index": 1}])
    operation = {"action_type": "choose_event_option", "kwargs": {"option_index": 1}}
    record = {}
    service._pending_action = {"operation": operation, "before": before, "record": record,
        "anchor": service._companion_session.anchor(), "submitted_at": 0}
    result = service._resolve_pending_action(after)
    assert result["executed"] == own_vote
    if own_vote:
        assert record["outcome"]["event_vote_registered"]
        assert not record["outcome"]["event"]["is_finished"]
        after_view = deepcopy(before["agent_view"])
        after_view["event"] = {"id": "shared", "is_shared": True, "finished": False, "local_vote": 1}
        service._companion_session.record_action(operation, anchor=service._companion_session.anchor(),
            before=before["agent_view"], after=after_view, result={"status": "ok", "executed": True})
        assert service._companion_session.actions[-1]["outcome"]["event_vote_registered"]
    else:
        assert result["status"] == "pending" and service._pending_action is not None


def test_stuck_recheck_is_once_until_scene_or_player_input_changes(monkeypatch):
    service = prepare(monkeypatch)
    monkeypatch.setattr(service, "_read_companion_dialogue", AsyncMock())
    monkeypatch.setattr(service, "_publish_companion_reply", AsyncMock(return_value={"text_sent": True}))
    async def plan(context, **kwargs):
        kwargs["route_messages"]({m["id"]: "chat" for m in service._companion_session.pending_messages()})
        return PlannedOperation("", {}, 1, "llm", plan_update="wait", reply="等现场更新。")
    planner = AsyncMock(side_effect=plan)
    monkeypatch.setattr(service._llm_planner, "plan", planner)
    asyncio.run(service.run_autoplay_step())
    asyncio.run(service.run_autoplay_step())
    service._state.stuck_since -= 30
    for _ in range(8):
        asyncio.run(service.run_autoplay_step())
    assert service._state.stuck_reported and planner.await_count == 2
    service._companion_session.remember("user", "现在怎么样")
    asyncio.run(service.run_autoplay_step())
    assert planner.await_count == 3
    service._state.snapshot["agent_view"]["event"]["options"] = [{"i": 6}]
    asyncio.run(service.run_autoplay_step())
    assert planner.await_count == 4


@pytest.mark.parametrize("stale", [False, True])
def test_instruction_reply_replaces_plan_speech_and_survives_cancelled_action(monkeypatch, stale):
    view = {"screen": "MAP", "run_id": "one", "actions": ["choose_map_node"], "run": {"floor": 1},
            "map": {"local_vote": "1,3", "options": [{"i": 0, "line": "左边"}, {"i": 1, "line": "右边"}]}}
    service = prepare(monkeypatch, view)
    monkeypatch.setattr(service, "_read_companion_dialogue", AsyncMock())
    monkeypatch.setattr(service, "_push_catgirl", AsyncMock())
    monkeypatch.setattr(service, "_sync_execution_status", AsyncMock())
    delivered = AsyncMock(return_value={"ok": True, "text_sent": True, "audio_completed": True})
    monkeypatch.setattr(service, "_publish_companion_reply", delivered)
    previous = {"action_type": "choose_map_node", "kwargs": {"option_index": 1}, "speech_step_ids": [0]}
    service._companion_session.accept_plan(previous, view)
    service._companion_session.announce_plan(previous, "走右边。", "initial")
    service._companion_session.remember("user", "走左边吧")
    message = service._companion_session.pending_messages()[0]
    answer = {"action_type": "choose_map_node", "kwargs": {"option_index": 0}, "plan_update": "replace",
              "reply": "好，改走左边。", "speech": "改走左边那条，听你的。", "speech_step_ids": [0],
              "message_effects": {message["id"]: "guidance"}, "requirement_updates": [
                  {"op": "add", "source_message_id": message["id"], "text": "走左边", "scope": "action",
                   "behavior": {"kind": "intent"}}]}
    monkeypatch.setattr(service._llm_planner, "_ensure_llm", AsyncMock(return_value=object()))
    invoke = AsyncMock(return_value=json.dumps(answer, ensure_ascii=False))
    monkeypatch.setattr(service._llm_planner, "_invoke_text", invoke)
    monkeypatch.setattr(service, "execute_operation", STS2AutoplayService.execute_operation.__get__(service))
    dispatch = AsyncMock(return_value={"status": "stale" if stale else "ok", "executed": not stale,
                                       "message": "局面变化" if stale else "已执行"})
    monkeypatch.setattr(service, "_execute_operation", dispatch)
    result = asyncio.run(service.run_autoplay_step())
    assert result["executed"] == (not stale)
    assert invoke.await_count == delivered.await_count == dispatch.await_count == 1
    assert delivered.await_args.args[0] == "好，改走左边。"
    assert not service._companion_session.pending_messages()
    assert service._companion_session.input_results[-1]["text_sent"]
    assert dispatch.await_args.args[0]["speech"] == ""


@pytest.mark.parametrize("scenario", ["ok", "timeout"])
def test_combat_solver_is_prefetched_once_without_tool_round_trips(monkeypatch, scenario):
    from plugin.plugins.sts2_autoplay import catgirl_llm
    monkeypatch.setattr(catgirl_llm, "create_chat_llm_async", Mock())
    monkeypatch.setattr(catgirl_llm, "get_config_manager", Mock())
    seen = []
    replies = [
        {"content": '{"action_type":"end_turn","kwargs":{},"reason":"由我选定","future_plan":{"turns":[{"turn":2,"intent":"防守"},{"turn":3,"intent":"爆发"}]}}'},
    ]

    class Client:
        async def ainvoke_raw(self, messages, **kwargs):
            assert "tools" not in kwargs
            seen.append(deepcopy(messages))
            return {"choices": [{"message": replies.pop(0)}]}

    if scenario == "loop":
        replies = [{"tool_calls": [{"id": "solve-1", "type": "function", "function": {"name": "solve_combat", "arguments": "{}"}}]} for _ in range(2)]
    elif scenario == "unsupported":
        async def plain_invoke(self, messages):
            seen.append(deepcopy(messages))
            return {"choices": [{"message": replies.pop(0)}]}
        Client.ainvoke_raw = plain_invoke

    planner = STS2LLMPlanner(Mock(), persona_provider=lambda: "猫娘")
    monkeypatch.setattr(planner, "_ensure_llm", AsyncMock(return_value=Client()))
    result = {"status": "ok", "solver_result": {"score": 12, "confidence": 0.7,
              "line": [{"turn": 2, "steps": [{"kind": "end_turn"}]}]}}
    if scenario == "stale":
        result = {"status": "stale"}
    solver = AsyncMock(side_effect=TimeoutError("求解超时")) if scenario == "timeout" else AsyncMock(return_value=result)
    context = {"agent_view": {"screen": "EVENT" if scenario == "noncombat" else "COMBAT", "turn": 2, "actions": ["end_turn"], "combat": {}},
               "actions_enabled": scenario != "paused",
               "session": {"current_run": {"turn": 2}}}
    if scenario == "loop":
        with pytest.raises(RuntimeError, match="不调用工具"):
            asyncio.run(planner.plan(context, solver=solver))
        assert solver.await_count == 1
        assert planner.metrics["model_calls"] == 2
        return
    if scenario == "stale":
        with pytest.raises(DecisionContextChanged, match="刷新后重判"):
            asyncio.run(planner.plan(context, solver=solver))
        assert solver.await_count == 1 and planner.metrics["model_calls"] == 0
        return
    operation = asyncio.run(planner.plan(context, solver=solver))
    if scenario == "paused":
        assert operation is None
    else:
        assert operation.action_type == "end_turn"
        if scenario != "noncombat":
            assert operation.future_plan["turns"][1]["turn"] == 3
    if scenario in {"noncombat", "paused"}:
        assert solver.await_count == 0
        return
    assert solver.await_count == 1
    tool_result = json.loads(seen[0][-1]["content"])["combat_solver"]
    assert tool_result["status"] == ("error" if scenario == "timeout" else "ok")
    assert planner.metrics["model_calls"] == 1
    assert not planner.metrics["tool_round_trips"]


def test_selected_plan_saves_model_call_and_stops_on_new_request(monkeypatch):
    service = prepare(monkeypatch)
    view = deepcopy(service._state.snapshot["agent_view"])
    expected = deepcopy(view)
    expected["event"]["options"] = [{"i": 8}]
    operation = PlannedOperation("choose_event_option", {"option_index": 5}, 1, "llm", continuation=[{
        "action_type": "choose_event_option", "kwargs": {"option_index": 8}, "expected_view": expected}])
    planner = AsyncMock(return_value=operation)
    monkeypatch.setattr(service._llm_planner, "plan", planner)

    async def execute(op):
        service._state.snapshot["agent_view"] = deepcopy(expected)
        return {"status": "ok", "executed": True}

    monkeypatch.setattr(service, "execute_operation", AsyncMock(side_effect=execute))
    asyncio.run(service.run_autoplay_step())
    asyncio.run(service.run_autoplay_step())
    assert planner.await_count == 1
    assert [s["path"] for s in service._state.latency_samples] == ["model", "selected_plan"]
    service._selected_steps = operation.continuation.copy()
    asyncio.run(service.apply_user_override_safely("下一回合先防守"))
    assert not service._selected_steps
    assert service._companion_session.messages[-1]["content"] == "下一回合先防守"
    planner.return_value = PlannedOperation("choose_event_option", {"option_index": 8}, 1, "llm")
    asyncio.run(service.run_autoplay_step())
    assert planner.await_count == 2


def test_new_run_clears_game_context_and_rejects_old_host_dialogue(monkeypatch):
    from plugin.plugins.sts2_autoplay import companion_session
    now = [10.0]
    monkeypatch.setattr(companion_session, "time", lambda: now[0])
    service = STS2AutoplayService(Mock(), lambda _: None)
    states = iter([
        {"screen": "COMBAT", "run_id": "same-seed", "in_combat": True,
         "agent_view": {"screen": "COMBAT", "run_id": "same-seed", "turn": 2}},
        {"screen": "CHARACTER_SELECT", "run_id": "same-seed",
         "agent_view": {"screen": "CHARACTER_SELECT", "run_id": "same-seed"}},
        {"screen": "MAP", "run_id": "same-seed",
         "agent_view": {"screen": "MAP", "run_id": "same-seed"}},
    ])
    client = SimpleNamespace(get_state=AsyncMock(side_effect=lambda: next(states)),
                             get_available_actions=AsyncMock(return_value={"actions": []}))
    monkeypatch.setattr(service, "_ensure_companion_client", AsyncMock(return_value=client))
    asyncio.run(service._loop_runner.tick())
    session = service._companion_session
    session.remember("user", "上一局要求")
    old_stamp = session.messages[-1]["timestamp"]
    session.actions.append({"reason": "上一局行动"})
    session.character_choice_made = True
    service._state.pending_guidance.append({"content": "上一局指示"})
    service._state.recent_decision_memory.append({"reason": "上一局战报"})
    service._selected_steps.append({"action_type": "end_turn"})
    now[0] = 20.0
    asyncio.run(service._loop_runner.tick())
    assert not session.messages and not session.actions and not session.character_choice_made
    assert not service._state.pending_guidance and not service._state.recent_decision_memory
    assert not service._selected_steps
    session.import_dialogue([
        SimpleNamespace(timestamp=old_stamp, lanlan_name="猫娘", turn_type="user_message", content="上一局要求"),
        SimpleNamespace(timestamp=None, lanlan_name="猫娘", turn_type="assistant_message", content="未知时间旧回复"),
        SimpleNamespace(timestamp=session.started_at + 1, lanlan_name="猫娘", turn_type="user_message", content="这局选猎人"),
    ], character_name="猫娘")
    assert [item["content"] for item in session.messages] == ["这局选猎人"]
    session.character_choice_made = True
    asyncio.run(service._loop_runner.tick())
    assert session.facts["run_id"] == "same-seed"
    assert session.character_choice_made
    assert [item["content"] for item in session.messages] == ["这局选猎人"]


def test_lobby_waits_for_manual_character_selection(monkeypatch):
    for screen in ("CHARACTER_SELECT", "MULTIPLAYER_LOBBY"):
        service = prepare(monkeypatch, {"screen": screen, "run_id": "run_unknown",
            "actions": ["select_character", "ready_multiplayer_lobby", "embark"]})
        planner = AsyncMock()
        monkeypatch.setattr(service._llm_planner, "plan", planner)
        result = asyncio.run(service.run_autoplay_step())
        assert not result["executed"] and "手动确认" in result["message"]
        planner.assert_not_awaited()
        service.execute_operation.assert_not_awaited()


@pytest.mark.parametrize("unified", [False, True])
def test_dialogue_arriving_during_decision_invalidates_action_before_dispatch(monkeypatch, unified):
    service = prepare(monkeypatch)
    monkeypatch.delattr(service, "execute_operation")
    dispatch = AsyncMock(return_value={"status": "ok", "executed": True})
    monkeypatch.setattr(service, "_execute_operation", dispatch)
    records = []
    service._sdk_bus = SimpleNamespace(conversations=SimpleNamespace(
        get=lambda **kwargs: SimpleNamespace(items=list(records))))
    monkeypatch.setattr(service._catgirl_llm, "character_name", lambda: "猫娘")
    if unified:
        service._ingress_enabled = service._ingress_active = True
        async def host(path, payload):
            assert path == "poll"
            return {"ok": True, "persona": "猫娘", "inputs": [
                {"request_id": "interrupt", "content": item.content, "timestamp": item.timestamp}
                for item in records]}
        monkeypatch.setattr(service, "_companion_host_request", host)

    async def scenario():
        deciding, finish = asyncio.Event(), asyncio.Event()

        async def plan(*args, **kwargs):
            deciding.set()
            await finish.wait()
            return PlannedOperation("choose_event_option", {"option_index": 5}, 1, "llm")

        monkeypatch.setattr(service._llm_planner, "plan", plan)
        task = asyncio.create_task(service.run_autoplay_step())
        await deciding.wait()
        records.append(SimpleNamespace(timestamp=service._companion_session.started_at + 1,
            lanlan_name="猫娘", turn_type="user_message", content="刚才的选择改一下"))
        if not unified:
            finish.set()
        result = await asyncio.wait_for(task, timeout=3)
        assert result["status"] == "stale" and not result["executed"]
        if unified:
            assert not finish.is_set() and service._state.decision_errors == 0
            assert service._companion_session.pending_messages()[0]["request_id"] == "interrupt"

    asyncio.run(scenario())
    dispatch.assert_not_awaited()
    assert service._state.autoplay_state == "running"  # 新发言使旧动作失效，不另加自动暂停规则。
    assert service._companion_session.messages[-1]["content"] == "刚才的选择改一下"


def test_event_placeholders_wait_without_model_then_resume_when_ready(monkeypatch):
    service = prepare(monkeypatch)
    monkeypatch.setattr(service, "_read_companion_dialogue", AsyncMock())
    event = service._state.snapshot["agent_view"]["event"]
    event["options"][0]["line"] = "吃下 | 回复{Heal}生命。"
    planner = AsyncMock(return_value=PlannedOperation("choose_event_option", {"option_index": 5}, 1, "llm"))
    monkeypatch.setattr(service._llm_planner, "plan", planner)
    assert asyncio.run(service.run_autoplay_step())["status"] == "waiting_for_data"
    service._event_data_wait_at -= 11
    assert "持续未解析" in asyncio.run(service.run_autoplay_step())["message"]
    assert service.execution_status()["state"] == "waiting_for_data"
    planner.assert_not_awaited()
    service.execute_operation.assert_not_awaited()
    event["options"][0]["line"] = "吃下 | 回复9生命。"
    assert asyncio.run(service.run_autoplay_step())["executed"]
    planner.assert_awaited_once()


@pytest.mark.parametrize("recovered", [True, False])
def test_output_ownership_recovery_keeps_output_id_and_failure_count(monkeypatch, recovered):
    import httpx
    service = prepare(monkeypatch)
    service._ingress_csrf = "test-csrf"
    service._ingress_active = True
    requests = []
    def handle(request):
        requests.append(json.loads(request.content))
        if len(requests) == 1:
            return httpx.Response(409, json={"detail": "Companion instance expired or replaced"})
        return httpx.Response(200, json={"ok": True, "text_sent": True})
    factory = httpx.AsyncClient
    monkeypatch.setattr("plugin.plugins.sts2_autoplay.service.httpx.AsyncClient",
                        lambda **kwargs: factory(transport=httpx.MockTransport(handle), **kwargs))
    async def sync(*, restore_state=True):
        assert restore_state is False
        service._ingress_active = recovered
    monkeypatch.setattr(service, "_sync_companion_ingress", sync)
    async def scenario():
        payload = {"line": "新局开始了", "output_id": "stable-output"}
        if recovered:
            assert (await service._companion_host_request("output", payload))["text_sent"]
            assert requests[0] == requests[1]
        else:
            with pytest.raises(DecisionContextChanged, match="所有权"):
                await service._companion_host_request("output", payload)
            assert len(requests) == 1
        assert service._state.decision_errors == 0
    asyncio.run(scenario())


def _combat_view(hand_lines, *, turn=2, energy=3):
    return {"screen": "COMBAT", "run_id": "run1", "turn": turn,
            "actions": ["play_card", "end_turn"],
            "combat": {"player": {"hp": "40/80", "energy": energy, "cards_played_this_turn": 0},
                       "hand": [{"i": i, "line": line, "playable": True} for i, line in enumerate(hand_lines)],
                       "enemies": [{"i": 0, "enemy_id": "e1", "name": "史莱姆"}]}}


def _combat_snapshot(service, view):
    snapshot = normalize_snapshot({"screen": view["screen"], "run_id": view["run_id"], "agent_view": view},
                                  {"actions": [{"name": name} for name in view["actions"]]})
    snapshot["action_registry"] = service._action_registry.build(snapshot)
    return snapshot


@pytest.mark.parametrize("scenario", ["animation", "reward", "map_wait"])
def test_action_readiness_wait_is_scoped_to_combat(monkeypatch, scenario):
    service = prepare(monkeypatch)
    if scenario == "wait_format":
        monkeypatch.setattr(service._llm_planner, "_ensure_llm", AsyncMock(return_value=object()))
        monkeypatch.setattr(service._llm_planner, "_invoke_text", AsyncMock(return_value='{"plan_update":"wait","reason":"等队友投票后继续"}'))
        operation = asyncio.run(service._llm_planner.plan({"agent_view": {"screen": "MAP", "actions": ["choose_map_node"]}}))
        assert not operation.action_type and service._llm_planner.metrics["model_calls"] == 1
        return
    if scenario in {"event_followup", "reward_followup", "map_wait", "map_follow"}:
        follow_event = scenario == "event_followup"
        waiting = scenario == "map_wait"
        map_follow = scenario == "map_follow"
        action = "advance_event_dialogue" if follow_event else "choose_map_node" if waiting or map_follow else "collect_rewards_and_proceed"
        current = {"screen": "EVENT" if follow_event else "MAP" if waiting or map_follow else "REWARD", "run_id": "one",
            "run": {"floor": 4}, "actions": [action], "event": {"id": "event-1"},
            "map": {"local_vote": "3,2"} if waiting else {}, "reward": {"rewards": []}}
        service._state.snapshot = _combat_snapshot(service, current)
        if map_follow:
            service._map_policy = "follow"
            current["map"] = {"local_vote": None, "options": [{"i": 0, "line": "Monster (3,2)"}]}
            service._state.snapshot["summary_context"] = {"payload": {"other_player_vote_index": 0}}
        service._companion_session.observe(current)
        service._last_operation = {"action_type": "choose_event_option" if follow_event else "choose_reward_card",
            "flow_anchor": {"run_id": "one", "floor": 4, "event_id": "event-1"}}
        planner = AsyncMock(side_effect=AssertionError("无取舍流程不应调用模型"))
        monkeypatch.setattr(service._llm_planner, "plan", planner)
        result = asyncio.run(service.run_autoplay_step())
        assert result["status"] == "waiting_for_teammate" if waiting else result["executed"]
        planner.assert_not_awaited()
        if not waiting:
            assert service.execute_operation.await_args.args[0]["action_type"] == action
        return
    if scenario == "reward":
        view = {"screen": "REWARD", "run_id": "one", "actions": ["claim_reward"],
                "reward": {"rewards": [{"i": 0, "claimable": True}]}}
        service._state.snapshot = _combat_snapshot(service, view)
        service._state.snapshot["raw_state"]["combat"] = {"players": [{"is_local": True, "ready_to_end_turn": True}]}
        planner = AsyncMock(return_value=PlannedOperation("claim_reward", {"option_index": 0}, 1, "llm"))
        monkeypatch.setattr(service._llm_planner, "plan", planner)
        assert asyncio.run(service.run_autoplay_step())["executed"]
        assert planner.await_args.args[0]["actions_enabled"]
        return
    view = _combat_view(["防御 [1费]：获得5点格挡。"])
    service._state.snapshot = _combat_snapshot(service, view)
    service._companion_session.observe(view)
    planner = AsyncMock(return_value=PlannedOperation("play_card", {"card_index": 0}, 1, "llm", continuation=[
        {"action_type": "play_card", "kwargs": {"card_index": 0}, "expected_view": view.copy()}]))
    monkeypatch.setattr(service._llm_planner, "plan", planner)
    asyncio.run(service.run_autoplay_step())
    saved_plan = deepcopy(service._selected_steps)
    view["actions"] = []
    service._state.snapshot = _combat_snapshot(service, view)
    result = asyncio.run(service.run_autoplay_step())
    assert result["status"] == "waiting_for_actions"
    assert service._selected_steps == saved_plan and planner.await_count == 1
    service._companion_session.remember("user", "今天有点累")
    planner.return_value = PlannedOperation("", {}, 1, "llm", reply="休息一下也好。", plan_update="keep")
    monkeypatch.setattr(service, "_publish_companion_reply", AsyncMock(return_value={"text_sent": True}))
    asyncio.run(service.run_autoplay_step())
    assert not planner.await_args.args[0]["actions_enabled"]
    service._publish_companion_reply.assert_awaited_once()
    service._companion_session.resolve_messages({"1": "chat"})
    view["actions"] = ["play_card", "end_turn"]
    service._state.snapshot = _combat_snapshot(service, view)
    assert asyncio.run(service.run_autoplay_step())["executed"]
    assert planner.await_count == 2



@pytest.mark.parametrize("phase", ["selected", "submitted"])
def test_grid_selection_confirms_target_step_without_toggling_it(monkeypatch, phase):
    from plugin.plugins.sts2_autoplay import service as service_module
    clock = [1.0]
    monkeypatch.setattr(service_module, "time", lambda: clock[0])
    service = prepare(monkeypatch, {"screen": "CARD_SELECTION_REMOVE", "run_id": "one", "actions": ["select_deck_card", "confirm_selection"]})
    before = deepcopy(service._state.snapshot)
    before["raw_state"]["selection"] = {"kind": "deck_card_select", "min_select": 2, "max_select": 2,
        "selected_count": 0, "can_confirm": False, "cards": [
            {"index": 0, "entity_id": "first", "card_id": "STRIKE", "selected": False},
            {"index": 1, "entity_id": "second", "card_id": "STRIKE", "selected": False}]}
    before["raw_state"]["run"] = {"deck": [{"card_id": "STRIKE"}, {"card_id": "STRIKE"}]}
    after = deepcopy(before)
    operation = {"action_type": "select_deck_card", "kwargs": {"option_index": 0}}
    record = {}
    service._pending_action = {"operation": operation, "before": before, "record": record,
        "anchor": service._companion_session.anchor(), "submitted_at": 0}
    if phase == "delayed":
        assert service._resolve_pending_action(after)["status"] == "pending"
    if phase in {"selected", "delayed"}:
        after["raw_state"]["selection"]["cards"][0]["selected"] = True
        after["raw_state"]["selection"]["selected_count"] = 1
    if phase in {"submitted", "cancelled"}:
        after["raw_state"]["selection"] = None
        after["agent_view"]["screen"] = "EVENT"
        if phase == "submitted":
            after["raw_state"]["run"]["deck"] = []
    clock[0] = 11.0
    result = service._resolve_pending_action(after)
    assert result["executed"] == (phase in {"selected", "delayed", "submitted"})
    if phase in {"selected", "delayed"}:
        assert not record["outcome"]["selection_submitted"]
        assert service._validate_companion_action(after, operation)[0] is None
        assert service._validate_companion_action(after, {"action_type": "select_deck_card", "kwargs": {"option_index": 1}})[0] is not None
        after["agent_view"]["selection"] = {"selected": 1, "max": 2, "cards": [{"i": 0, "selected": True}, {"i": 1, "selected": False}]}
        service._companion_session.record_action(operation, anchor=service._companion_session.anchor(),
            before={"selection": {}}, after=after["agent_view"], result={"executed": True, "status": "ok"})
        assert not service._companion_session.actions[-1]["outcome"]["selection_submitted"]
    elif phase == "waiting":
        assert service._validate_companion_action(after, operation)[0] is None
        after["agent_view"]["run"] = {"gold": 42}
        assert service._validate_companion_action(after, operation)[0] is None
        after["raw_state"]["selection"]["cards"][1]["selected"] = True
        after["raw_state"]["selection"]["selected_count"] = 1
        assert service._validate_companion_action(after, operation)[0] is not None
    elif phase == "submitted":
        assert record["outcome"]["selection_submitted"]
    else:
        assert not record["executed"]


@pytest.mark.parametrize("scope", ["new", "manual"])
def test_failure_pause_belongs_to_original_run_and_preserves_user_control(monkeypatch, scope):
    service = prepare(monkeypatch)
    service._state.decision_errors = 3
    service._state.decision_error = "旧局连接失败"
    service._pause_after_decision_failures()
    if scope == "manual":
        service.pause_autoplay(reason="user")
    elif scope == "stop":
        service._state.autoplay_state = "idle"
        service._state.pause_reason = ""
        service._state.stop_reason = "user"
    if scope != "same":
        view = {"screen": "CHARACTER_SELECT" if scope == "pregame" else "EVENT", "run_id": "run_unknown" if scope == "pregame" else "two", "actions": []}
        service._companion_session.observe(view)
        service._state.snapshot["agent_view"] = view
    service._reset_companion_run_context()
    monkeypatch.setattr(service, "sync_control_mode_to_mod", AsyncMock(return_value={"applied": True, "settled": True}))
    monkeypatch.setattr(service, "start_autoplay", Mock(side_effect=lambda: setattr(service._state, "autoplay_state", "running")))
    asyncio.run(service.reconcile_turn_control())
    if scope in {"same", "pregame"}:
        assert service._state.autoplay_state == "paused" and service._state.decision_errors == 3
        assert service._decision_failure_notice
        if scope == "pregame":
            # 选人进入实局时session epoch可保持，仍按真实新局号清理旧故障。
            view = {"screen": "EVENT", "run_id": "two", "actions": []}
            service._companion_session.observe(view)
            service._state.snapshot["agent_view"] = view
            asyncio.run(service.reconcile_turn_control())
            assert service._state.autoplay_state == "running" and not service._decision_failure_notice
    elif scope == "new":
        assert service._state.autoplay_state == "running" and service._state.decision_errors == 0
        assert not service._decision_failure_notice
    elif scope == "manual":
        assert service._state.autoplay_state == "paused" and service._state.pause_reason == "user"
    else:
        assert service._state.autoplay_state == "idle" and service._state.stop_reason == "user"


def test_single_map_option_finishes_own_vote_after_resume_and_cached_wait(monkeypatch):
    view = {"screen": "MAP", "run_id": "one", "actions": ["choose_map_node"],
        "map": {"current": "4,0", "local_vote": None, "options": [{"i": 0, "line": "Monster (5,1) [votes:1]"}],
                "votes": [{"coord": "5,1", "local": False}]}}
    service = prepare(monkeypatch, view)
    service._state.snapshot["raw_state"]["combat"] = {"players": [{"is_local": True, "ready_to_end_turn": True}]}
    assert not service._waiting_for_teammate(service._state.snapshot)
    monkeypatch.setattr(service, "_read_companion_dialogue", AsyncMock())
    monkeypatch.setattr(service, "_publish_companion_reply", AsyncMock(return_value={"text_sent": True}))
    service._companion_session.remember("user", "继续")
    message = service._companion_session.pending_messages()[0]
    async def plan(context, **kwargs):
        kwargs["route_messages"]({message["id"]: "guidance"})
        return PlannedOperation("", {}, 1, "llm", reply="继续，等你的票生效。", plan_update="wait", control="resume")
    planner = AsyncMock(side_effect=plan)
    monkeypatch.setattr(service._llm_planner, "plan", planner)
    assert not asyncio.run(service.run_autoplay_step())["executed"]
    assert service._last_decision_key
    assert asyncio.run(service.run_autoplay_step())["executed"]
    chosen = service.execute_operation.await_args.args[0]
    assert chosen["action_type"] == "choose_map_node" and chosen["kwargs"] == {"option_index": 0}
    assert planner.await_count == 1
    service._state.snapshot["agent_view"]["map"]["local_vote"] = "5,1"
    assert asyncio.run(service.run_autoplay_step())["status"] == "waiting_for_teammate"
    assert service.execute_operation.await_count == 1


def test_branching_map_rejects_empty_wait_without_choosing_route_for_model(monkeypatch):
    view = {"screen": "MAP", "run_id": "one", "actions": ["choose_map_node"],
        "map": {"local_vote": None, "options": [{"i": 0, "line": "Monster"}, {"i": 1, "line": "Rest"}],
                "votes": [{"coord": "5,1", "local": False}]}}
    service = prepare(monkeypatch, view)
    monkeypatch.setattr(service, "_read_companion_dialogue", AsyncMock())
    assert service._flow_followup(service._state.snapshot) is None
    planner = STS2LLMPlanner(Mock(), persona_provider=lambda: "小铃")
    monkeypatch.setattr(planner, "_ensure_llm", AsyncMock(return_value=object()))
    requests = []
    answers = iter([{"action_type": None, "plan_update": "wait", "reason": "等主人的票生效"},
                    {"action_type": "choose_map_node", "kwargs": {"option_index": 1}}])
    async def invoke(llm, messages):
        requests.append(deepcopy(messages))
        return json.dumps(next(answers), ensure_ascii=False)
    monkeypatch.setattr(planner, "_invoke_text", invoke)
    service._llm_planner = planner
    assert asyncio.run(service.run_autoplay_step())["executed"]
    assert len(requests) == 2 and "自己的地图票尚未投出" in requests[1][-1]["content"]
    assert service.execute_operation.await_args.args[0]["kwargs"] == {"option_index": 1}


def test_map_vote_completion_preserves_pause_and_explicit_player_wait(monkeypatch):
    view = {"screen": "MAP", "run_id": "one", "actions": ["choose_map_node"],
            "map": {"local_vote": None, "options": [{"i": 0, "line": "Monster"}]}}
    service = prepare(monkeypatch, view)
    monkeypatch.setattr(service, "_read_companion_dialogue", AsyncMock())
    service.pause_autoplay(reason="user")
    assert not asyncio.run(service.run_autoplay_step())["executed"]
    service.execute_operation.assert_not_awaited()
    service.resume_autoplay()
    session = service._companion_session
    session.remember("user", "先别投票，等我说")
    message = session.pending_messages()[0]
    session.apply_requirement_updates([{"op": "add", "source_message_id": message["id"], "text": message["content"],
        "scope": "scene", "scene": "MAP", "strength": "hard", "release_by_player": True,
        "behavior": {"kind": "wait", "action_types": ["choose_map_node"]}}], service._state.snapshot)
    session.resolve_messages({message["id"]: "guidance"})
    assert service._map_vote_candidates(service._state.snapshot) == []
    assert service._flow_followup(service._state.snapshot) is None
    waiting = PlannedOperation("", {}, 1, "llm", plan_update="wait")
    assert service._validate_companion_action(service._state.snapshot, waiting) == (waiting, "")


def test_map_wait_consumes_material_updates_without_replanning(monkeypatch):
    view = {"screen": "MAP", "run_id": "one", "actions": ["choose_map_node"],
        "map": {"current": "5,4", "local_vote": "6,4", "options": [{"i": 0, "line": "Unknown (6,4) [local vote]"}],
                "votes": [{"coord": "6,4", "local": True}]}}
    service = prepare(monkeypatch, view)
    service._ingress_enabled = True
    service._ingress_active = True
    service._ingress_character = "小铃"
    monkeypatch.setattr(service._catgirl_llm, "character_name", lambda: "小铃")
    monkeypatch.setattr(service, "_read_companion_dialogue", AsyncMock())
    contexts = []
    async def plan(context, **kwargs):
        contexts.append(context)
        kwargs["route_messages"]({m["id"]: "chat" for m in service._companion_session.pending_messages()})
        return PlannedOperation("", {}, 1, "llm", plan_update="wait", reply="还在等你的票。" if context["new_player_messages"] else "")
    monkeypatch.setattr(service._llm_planner, "plan", plan)
    monkeypatch.setattr(service, "_publish_companion_reply", AsyncMock(return_value={"text_sent": True}))
    for i in range(4):
        material = {"kind": "proactive_material", "timestamp": i, "materials": [{"mode": "news", "text": str(i)}]}
        monkeypatch.setattr(service, "_companion_host_request", AsyncMock(return_value={"persona": "小铃", "inputs": [], "callbacks": [material]}))
        asyncio.run(service._sync_companion_ingress())
        assert asyncio.run(service.run_autoplay_step())["status"] == "waiting_for_teammate"
    assert not contexts and not service._ingress_callbacks and service._ingress_material["timestamp"] == 3
    service._state.snapshot["agent_view"]["map"]["votes"].append({"coord": "6,3", "local": False})
    asyncio.run(service.run_autoplay_step())
    assert len(contexts) == 1 and contexts[0]["host_callbacks"] == [material]
    asyncio.run(service.run_autoplay_step())
    assert len(contexts) == 1
    service._companion_session.remember("user", "你在等什么")
    asyncio.run(service.run_autoplay_step())
    assert len(contexts) == 2
    callback = {"kind": "media_result", "result": {"status": "playing"}}
    service._ingress_callbacks.append(callback)
    asyncio.run(service.run_autoplay_step())
    asyncio.run(service.run_autoplay_step())
    assert len(contexts) == 3 and contexts[-1]["host_callbacks"] == [callback]
    service.execute_operation.assert_not_awaited()


def test_flat_combat_plan_executes_cards_and_end_turn_with_one_model_call(monkeypatch):
    view = {"screen": "COMBAT", "run_id": "one", "turn": 1, "actions": ["play_card", "end_turn"],
        "combat": {"player": {"energy": 3, "block": 0}, "hand": [
            {"i": 0, "line": "打击 [1费]：造成6点伤害。", "playable": True, "target": "enemies", "targets": [0]},
            {"i": 1, "line": "打击 [1费]：造成6点伤害。", "playable": True, "target": "enemies", "targets": [0]},
            {"i": 2, "line": "防御 [1费]：获得5点格挡。", "playable": True}],
            "enemies": [{"i": 0, "name": "小啃兽", "alive": True, "hp": "30/30", "intent": "ATTACK"}]}}
    service = prepare(monkeypatch, view)
    raw = service._state.snapshot["raw_state"]
    raw.update(in_combat=True, turn=1, combat={"player": {"energy": 3}, "players": [{"is_local": True}], "hand": [
        {"index": 0, "entity_id": "card-7", "card_id": "STRIKE", "name": "打击", "energy_cost": 1, "target_index_space": "enemies"},
        {"index": 1, "entity_id": "card-8", "card_id": "STRIKE", "name": "打击", "energy_cost": 1, "target_index_space": "enemies"},
        {"index": 2, "entity_id": "card-9", "card_id": "DEFEND", "name": "防御", "energy_cost": 1}],
        "enemies": [{"index": 0, "entity_id": "enemy-1", "name": "小啃兽", "current_hp": 30, "is_alive": True, "intent": "ATTACK"}]})
    answer = {"action_type": "play_card", "kwargs": {"card_ref": "c9"}, "plan_source": "custom", "plan_update": "replace",
        "speech": "先垫张防御稳住，再两张打击削它，接着结束回合。", "speech_scope": "intent", "speech_step_ids": [0, 1, 2, 3],
        "continuation": [{"action_type": "play_card", "card_ref": "c7", "target_ref": "e1"},
                         {"action_type": "play_card", "card_ref": "c8", "target_ref": "e1"}, {"action_type": "end_turn"}]}
    monkeypatch.setattr(service._llm_planner, "_ensure_llm", AsyncMock(return_value=object()))
    invoke = AsyncMock(return_value=json.dumps(answer, ensure_ascii=False))
    monkeypatch.setattr(service._llm_planner, "_invoke_text", invoke)
    solver = AsyncMock(return_value={"status": "ok", "solver_result": {"line": [{"turn": 1, "steps": [
        {"kind": "play_card", "card_id": "DEFEND", "card_index": 2},
        {"kind": "play_card", "card_id": "STRIKE", "card_index": 0, "target_index": 0},
        {"kind": "play_card", "card_id": "STRIKE", "card_index": 0, "target_index": 0}, {"kind": "end_turn"}]}]}})
    monkeypatch.setattr(service, "_solve_for_companion", solver)
    monkeypatch.setattr(service, "_read_companion_dialogue", AsyncMock())
    operations = []
    async def execute(op):
        operations.append(deepcopy(op))
        if op["action_type"] == "play_card":
            index = op["kwargs"]["card_index"]
            raw["combat"]["hand"].pop(index)
            view["combat"]["hand"].pop(index)
            for i, (card, face) in enumerate(zip(raw["combat"]["hand"], view["combat"]["hand"])):
                card["index"] = face["i"] = i
            raw["combat"]["player"]["energy"] -= 1
            view["combat"]["player"]["energy"] -= 1
        return {"status": "ok", "executed": True}
    monkeypatch.setattr(service, "execute_operation", AsyncMock(side_effect=execute))
    for _ in range(4):
        assert asyncio.run(service.run_autoplay_step())["executed"]
    assert invoke.await_count == solver.await_count == 1
    assert [op["action_type"] for op in operations] == ["play_card", "play_card", "play_card", "end_turn"]
    assert [op.get("bindings", {}).get("card", {}).get("ref") for op in operations[:3]] == ["c9", "c7", "c8"]
    assert sum(bool(op.get("speech")) for op in operations) == 1
    assert [s["path"] for s in service._state.latency_samples] == ["model", "selected_plan", "selected_plan", "selected_plan"]
