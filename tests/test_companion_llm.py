from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from plugin.plugins.sts2_autoplay.action_engine import STS2ActionEngine
from plugin.plugins.sts2_autoplay.action_registry import STS2ActionRegistry
from plugin.plugins.sts2_autoplay.companion_session import CompanionSession
from plugin.plugins.sts2_autoplay.llm_planner import STS2LLMPlanner, view_signature
from plugin.plugins.sts2_autoplay.service import STS2AutoplayService
from plugin.plugins.sts2_autoplay.snapshot_normalizer import normalize_snapshot


@pytest.mark.parametrize("text,expected", [
    ("求解器这次没出方案，我手动来。先垫飞跃，再电击。", "先垫飞跃，再电击。"),
    ("先防御。CombatSolver报错！solver_steps为空；再打击。", "先防御。再打击。"),
    ("解算失败，我换个思路。", "我再看看当前局面。"),
    ("先防御，再打击。音乐搜索完成了。", "先防御，再打击。音乐搜索完成了。"),
])
def test_public_companion_text_filters_solver_sentences_only(text, expected):
    from plugin.plugins.sts2_autoplay.companion_feedback import public_companion_text

    assert public_companion_text(text) == expected


@pytest.mark.parametrize("channel", ["reply", "tactical", "legacy_reply", "danmaku"])
def test_solver_text_is_filtered_at_output_boundaries(monkeypatch, channel):
    service = STS2AutoplayService(Mock(), lambda _: None)
    service._companion_session.observe({"screen": "COMBAT", "run_id": "r"})
    text, expected = "SOLVER暂不可用。先防御。", "先防御。"
    if channel == "danmaku":
        client = SimpleNamespace(base_url="http://local", push_danmaku=AsyncMock(return_value={}))
        service._danmaku_enabled = True
        service._host_client = client
        monkeypatch.setattr(service, "_companion_client", lambda: client)
        monkeypatch.setattr(service, "_cached_avatar_base64", lambda: "")
        asyncio.run(service._push_catgirl(text, fetch_avatar=False))
        assert client.push_danmaku.call_args.args[0] == expected
    elif channel == "legacy_reply":
        from plugin.plugins.sts2_autoplay import service as service_module

        service._ingress_enabled = False
        service._ingress_active = False
        monkeypatch.setattr(service, "_push_catgirl", AsyncMock())
        speak = AsyncMock(return_value={"ok": True, "text_sent": True, "audio_completed": True})
        monkeypatch.setattr(service_module, "speak_current_turn", speak)
        asyncio.run(service._publish_companion_reply(text, []))
        assert speak.call_args.args[2] == expected
        assert service._push_catgirl.call_args.args[0] == expected
    else:
        service._ingress_enabled = service._ingress_active = True
        request = AsyncMock(return_value={"ok": True, "text_sent": True, "audio_completed": True})
        monkeypatch.setattr(service, "_companion_host_request", request)
        asyncio.run(service._publish_companion_reply(text, [], wait_audio=channel == "tactical"))
        assert request.call_args.args[1]["line"] == expected


@pytest.mark.parametrize("old_update", [False, True])
def test_history_messages_cannot_create_new_requirements_without_new_input(monkeypatch, old_update):
    planner = STS2LLMPlanner(Mock(), persona_provider=lambda: "小铃")
    monkeypatch.setattr(planner, "_ensure_llm", AsyncMock(return_value=object()))
    update = {"op": "add", "source_message_id": "1", "text": "整局走左边", "scope": "run", "behavior": {"kind": "intent"}}
    answer = {"action_type": "choose_map_node", "kwargs": {"option_index": 0},
              "message_effects": {"1": "guidance"}, "requirement_updates": [update] if old_update else []}
    requests = []
    async def invoke(llm, messages):
        requests.append(messages)
        return json.dumps(answer if len(requests) == 1 else {**answer, "requirement_updates": []}, ensure_ascii=False)
    monkeypatch.setattr(planner, "_invoke_text", invoke)
    routed = Mock()
    operation = asyncio.run(planner.plan({"agent_view": {"screen": "MAP", "actions": ["choose_map_node"]},
        "active_requirements": [], "session": {"player_messages": [{"id": "1", "content": "走左边吧", "kind": "guidance"}]}},
        route_messages=routed))
    assert operation.action_type == "choose_map_node" and operation.requirement_updates == []
    assert planner.metrics["model_calls"] == (2 if old_update else 1)
    routed.assert_not_called()


def test_lethal_end_turn_uses_numeric_risk_instead_of_survival_claim(monkeypatch):
    from plugin.plugins.sts2_autoplay.agent_facts import end_turn_danger

    planner = STS2LLMPlanner(Mock(), persona_provider=lambda: "小铃")
    monkeypatch.setattr(planner, "_ensure_llm", AsyncMock(return_value=object()))
    monkeypatch.setattr(planner, "_invoke_text", AsyncMock(return_value=json.dumps({
        "action_type": "end_turn", "kwargs": {}, "reason": "剩5格挡，过回合",
        "speech": "我垫了5格挡，这次挨7伤不会死，撑住了。"}, ensure_ascii=False)))
    combat = {"end_turn_will_kill_player": True, "lethal_risks": [{
        "will_kill_player": True, "player_hp": 5, "player_block": 5,
        "incoming_damage": 12, "damage_after_block": 7}]}
    operation = asyncio.run(planner.plan({"agent_view": {"screen": "COMBAT", "run_id": "r",
        "combat": combat, "actions": ["end_turn"]}}))
    assert "当前5血" in operation.speech and "剩7点伤害" in operation.speech
    assert "致死线" in operation.speech and "不会死" not in operation.speech
    assert operation.model_reason == "剩5格挡，过回合"
    assert end_turn_danger({"end_turn_will_kill_player": False, "lethal_risks": []}) == ""
    combat["lethal_risks"][0].update(player_hp=7)
    assert "致死线" in end_turn_danger(combat)


@pytest.mark.parametrize("mode", ["paused", "wait", "action"])
def test_requirement_updates_survive_action_and_no_action_responses(monkeypatch, mode):
    updates = [{"op": "add", "source_message_id": "r1", "text": "等右边易伤再攻击",
                "scope": "battle", "strength": "hard", "behavior": {"kind": "wait"},
                "conditions": [{"kind": "power", "subject": "enemy", "target_ref": "e1",
                                "power_id": "VULNERABLE_POWER", "op": "gte", "value": 1}]}]
    active = [{"id": "old", "text": "这场留着药水", "status": "active", "behavior": {"kind": "reserve"}},
              {"id": "intent", "text": "这次结束回合", "status": "active", "behavior": {"kind": "intent"}},
              {"id": "future", "text": "下个营火愈合", "status": "pending", "behavior": {"kind": "intent"}}]
    status = {"kind": "paused", "reason": "玩家暂停"}
    answer = {"action_type": "end_turn" if mode == "action" else None,
              "kwargs": {}, "plan_update": "wait" if mode == "wait" else "keep",
              "message_effects": {"r1": "guidance"}, "reply": "等右边易伤后再攻击。",
              "speech": "等右边易伤后再攻击。", "reason": "等待右边易伤",
              "requirement_updates": updates, "completes_requirements": ["intent"] if mode == "action" else []}
    planner = STS2LLMPlanner(Mock(), persona_provider=lambda: "小铃")
    monkeypatch.setattr(planner, "_ensure_llm", AsyncMock(return_value=object()))
    seen = []
    async def invoke(llm, messages):
        seen.append(messages)
        output = dict(answer)
        if len(seen) == 1:
            if mode == "action":
                output["completes_requirements"] = ["future", "old"]
            elif mode == "paused":
                output["completes_requirements"] = ["intent"]
            else:
                output.pop("requirement_updates")
        return json.dumps(output, ensure_ascii=False)
    monkeypatch.setattr(planner, "_invoke_text", invoke)
    routed = Mock()
    validator = Mock(side_effect=lambda op: (op, ""))
    operation = asyncio.run(planner.plan({
        "agent_view": {"screen": "COMBAT", "turn": 1, "actions": ["end_turn"]},
        "actions_enabled": mode != "paused", "active_requirements": active,
        "execution_status": status, "new_player_messages": [{"id": "r1", "content": "等右边易伤再攻击"}],
    }, route_messages=routed, validate=validator))
    assert operation.requirement_updates == operation.as_dict()["requirement_updates"] == updates
    assert operation.completes_requirements == operation.as_dict()["completes_requirements"] == answer["completes_requirements"]
    assert (operation.model_reason == "等待右边易伤" and operation.reason == "本次选择：结束回合") if mode == "action" else operation.reason == "等待右边易伤"
    assert bool(operation.action_type) == (mode == "action")
    payload = json.loads(seen[0][1]["content"])
    assert payload["active_requirements"] == active and payload["execution_status"] == status
    assert planner.metrics["model_calls"] == 2
    routed.assert_called_once_with({"r1": "guidance"})
    validator.assert_called_once_with(operation)


@pytest.mark.parametrize("scenario", ["missing", "unbound", "future"])
def test_guidance_response_does_not_discard_valid_actions_or_future_ack(monkeypatch, scenario):
    future = scenario == "future"
    update = {"op": "add", "source_message_id": "1", "text": "后续战斗先防住伤害" if future else "这次选第二张",
              "scope": "run" if future else "action", "strength": "preference", "behavior": {"kind": "intent"}}
    answer = {"action_type": "choose_reward_card", "kwargs": {"option_index": 1}, "plan_update": "replace",
              "message_effects": {"1": "guidance"}, "requirement_updates": [update],
              "speech": "先把奖励收了。" if future else "{card:9}" if scenario == "unbound" else "",
              "reply": "后续先稳住血量再输出，保留药水的req-2照旧。" if future else ""}
    planner = STS2LLMPlanner(Mock(), persona_provider=lambda: "小铃")
    monkeypatch.setattr(planner, "_ensure_llm", AsyncMock(return_value=object()))
    monkeypatch.setattr(planner, "_invoke_text", AsyncMock(return_value=json.dumps(answer, ensure_ascii=False)))
    routed = Mock()
    operation = asyncio.run(planner.plan({"agent_view": {"screen": "CARD_SELECTION_REWARD", "actions": ["choose_reward_card"]},
        "new_player_messages": [{"id": "1", "content": update["text"]}], "active_requirements": [
            {"id": "req-2", "text": "保留药水", "status": "active", "behavior": {"kind": "reserve"}}]},
        route_messages=routed, validate=lambda op: (op, "")))
    assert operation.action_type == "choose_reward_card" and operation.requirement_updates == [update]
    assert planner.metrics["model_calls"] == (1 if future else 2)
    assert ("后续先稳住血量" in operation.reply and "req-2" not in operation.reply) if future else not operation.reply
    assert not operation.speech
    routed.assert_called_once_with({"1": "guidance"})


def test_future_guidance_received_without_actions_survives_until_rest(monkeypatch):
    session = CompanionSession()
    session.observe({"screen": "REWARD", "run_id": "run-1"})
    session.remember("user", "下个火堆要愈合帮我回血啦 不然打不过boss")
    message = session.pending_messages()[0]
    seen = []
    answers = iter([
        {"action_type": None, "message_effects": {message["id"]: "guidance"},
         "reply": "到下个营火选愈合给你回血。", "plan_update": "keep"},
        {"action_type": "choose_rest_option", "kwargs": {"option_index": 2},
         "speech": "按主人的要求选愈合。"},
    ])
    planner = STS2LLMPlanner(Mock(), persona_provider=lambda: "Neko")
    monkeypatch.setattr(planner, "_ensure_llm", AsyncMock(return_value=object()))
    async def invoke(llm, messages):
        seen.append(json.loads(messages[-1]["content"]))
        return json.dumps(next(answers), ensure_ascii=False)
    monkeypatch.setattr(planner, "_invoke_text", invoke)
    asyncio.run(planner.plan({"agent_view": {"screen": "REWARD", "actions": []},
        "actions_enabled": False, "new_player_messages": [message], "session": session.context()},
        route_messages=session.resolve_messages))
    assert not session.pending_messages()
    session.observe({"screen": "REST_SITE", "run_id": "run-1"})
    operation = asyncio.run(planner.plan({"agent_view": {"screen": "REST_SITE", "actions": ["choose_rest_option"]},
        "session": session.context(), "new_player_messages": []}))
    assert operation.action_type == "choose_rest_option" and operation.kwargs == {"option_index": 2}
    assert seen[-1]["session"]["player_messages"][0]["content"] == message["content"]
    assert seen[-1]["new_player_messages"] == []


def test_prompt_update_keeps_same_session_history_instead_of_replaying_seed(monkeypatch):
    planner = STS2LLMPlanner(Mock(), persona_provider=lambda: "更新后的人格提示")
    planner._history_owner = "session-1"
    planner._last_prefix = "旧提示"
    history = [{"role": "assistant", "content": "主人要求已回应，当前到了地图。"}]
    planner._history = history.copy()
    seen = []
    monkeypatch.setattr(planner, "_ensure_llm", AsyncMock(return_value=object()))
    async def invoke(llm, messages):
        seen.extend(messages)
        return '{"action_type":"choose_map_node","kwargs":{"option_index":0},"speech":"继续走当前路线。"}'
    monkeypatch.setattr(planner, "_invoke_text", invoke)
    asyncio.run(planner.plan({"agent_view": {"screen": "MAP", "actions": ["choose_map_node"]},
        "conversation_owner": "session-1", "conversation_seed": [{"role": "user", "content": "我要磨刀石"}]}))
    assert seen[1] == history[0]
    assert not any(item["content"] == "我要磨刀石" for item in seen)


def test_strategy_base_is_stable_and_scene_rules_reach_the_actual_request(monkeypatch):
    planner = STS2LLMPlanner(Mock(), persona_provider=lambda: "小铃")
    monkeypatch.setattr(planner, "_ensure_llm", AsyncMock(return_value=object()))
    seen = []
    async def invoke(llm, messages):
        seen.append(messages)
        return '{"action_type":"choose_map_node","kwargs":{"option_index":0},"speech":"继续当前路线。"}'
    monkeypatch.setattr(planner, "_invoke_text", invoke)
    for scene in ("map", "event"):
        asyncio.run(planner.plan({"agent_view": {"screen": "MAP", "actions": ["choose_map_node"]},
            "conversation_owner": "session", "strategy_knowledge": {"scene": scene,
                "base": {"text": "按实际资源构筑", "sources": [{"path": "defect/base.md", "hash": "v1"}]},
                "scene_rules": {"text": scene + "当前专项"}}, "active_requirements": [{"id": "req-1", "text": "按主人安排"}]}))
    assert seen[0][0]["content"] == seen[1][0]["content"]
    assert "按实际资源构筑" in seen[0][0]["content"]
    payload = json.loads(seen[1][-1]["content"])
    assert "base" not in payload["strategy_knowledge"]
    assert payload["strategy_knowledge"]["scene_rules"]["text"] == "event当前专项"
    assert payload["active_requirements"][0]["text"] == "按主人安排"


def test_model_projection_limits_receipts_without_changing_execution_source(monkeypatch):
    planner = STS2LLMPlanner(Mock(), persona_provider=lambda: "小铃")
    monkeypatch.setattr(planner, "_ensure_llm", AsyncMock(return_value=object()))
    seen = []
    async def invoke(llm, messages):
        seen.append(json.loads(messages[-1]["content"]))
        return '{"action_type":"choose_map_node","kwargs":{"option_index":0},"speech":"继续当前路线。"}'
    monkeypatch.setattr(planner, "_invoke_text", invoke)
    actions = ["choose_map_node"]
    context = {"agent_view": {"screen": "MAP", "actions": actions, "available_actions": actions,
                                "run": {"piles": {"deck": ["重复副本"]}}},
               "agent_facts": {"collaboration": {"action_results": [{"sequence": i, "status": "completed"} for i in range(30)]}},
               "conversation_owner": "s", "session": {"player_messages": [{"content": "旧要求"}]},
               "active_requirements": [{"id": "req-1", "text": "保留药水"}]}
    asyncio.run(planner.plan(context))
    assert len(seen[0]["agent_facts"]["collaboration"]["action_results"]) == 4
    assert len(context["agent_facts"]["collaboration"]["action_results"]) == 30
    assert "available_actions" not in seen[0]["agent_view"] and "piles" not in seen[0]["agent_view"]["run"]
    assert "available_actions" in context["agent_view"]
    assert seen[0]["active_requirements"] == context["active_requirements"]


def test_reload_preserves_handled_request_ids_and_does_not_receive_them_again(monkeypatch):
    service = STS2AutoplayService(Mock(), lambda _: None)
    view = {"screen": "MAP", "run_id": "run-1"}
    service._state.snapshot = {"agent_view": view}
    service._companion_session.observe(view)
    service._ingress_enabled = True
    monkeypatch.setattr(service, "_companion_mode_active", lambda: True)
    monkeypatch.setattr(service._catgirl_llm, "character_name", lambda: "Neko")
    old = {"id": "60", "role": "user", "kind": "guidance", "timestamp": 1,
           "content": "下个营火愈合", "request_id": "handled-60"}
    service._companion_session.remember("user", "这回合先防守")
    local = service._companion_session.context()["local_pending_messages"][0]
    service._companion_session.user_requests.clear()
    service._companion_session.messages.clear()
    monkeypatch.setattr(service, "_companion_host_request", AsyncMock(return_value={
        "persona": "Neko", "checkpoint": {"session": {"current_run": {"run_id": "run-1"},
            "player_messages": [old], "recent_dialogue": [old], "local_pending_messages": [local]}},
        "inputs": [old, {"request_id": "fresh-61", "content": "现在怎么样", "timestamp": 2}],
    }))
    asyncio.run(service._sync_companion_ingress())
    assert [(m["id"], m["kind"]) for m in service._companion_session.user_requests] == [("60", "guidance"), ("1", "pending"), ("61", "pending")]
    assert service._companion_session.pending_messages()[-1]["request_id"] == "fresh-61"
    assert "handled-60" in service._ingress_ack
    assert sum(m["content"] == old["content"] for m in service._companion_session.messages) == 1


def test_transport_ids_and_old_effects_do_not_block_current_message_routing(monkeypatch):
    planner = STS2LLMPlanner(Mock(), persona_provider=lambda: "小铃")
    monkeypatch.setattr(planner, "_ensure_llm", AsyncMock(return_value=object()))
    seen = []
    async def invoke(llm, messages):
        seen.append(json.loads(messages[-1]["content"]))
        return '{"action_type":"end_turn","kwargs":{},"message_effects":{"transport-17":"guidance","old":"chat"},"speech":"收到，结束这回合。"}'
    monkeypatch.setattr(planner, "_invoke_text", invoke)
    routed = Mock()
    context = {"agent_view": {"screen": "COMBAT", "turn": 1, "actions": ["end_turn"]},
               "new_player_messages": [{"id": "17", "request_id": "transport-17", "content": "结束这回合"}]}
    operation = asyncio.run(planner.plan(context, route_messages=routed))
    assert operation.action_type == "end_turn" and planner.metrics["model_calls"] == 1
    routed.assert_called_once_with({"17": "guidance"})
    assert seen[0]["new_player_messages"] == [{"id": "17", "content": "结束这回合"}]


def test_planner_uses_host_persona_and_corrects_illegal_action(monkeypatch):
    from plugin.plugins.sts2_autoplay import catgirl_llm
    monkeypatch.setattr(catgirl_llm, "create_chat_llm_async", Mock())
    monkeypatch.setattr(catgirl_llm, "get_config_manager", Mock())
    view = {"screen": "COMBAT", "combat": {"hand": [
        {"i": 0, "playable": True, "target": "enemies", "targets": [1]},
    ]}, "actions": ["play_card"], "available_actions": ["play_card"]}
    snapshot = normalize_snapshot({"agent_view": view}, {"actions": [{"name": "play_card", "requires_index": True}]})
    snapshot["action_registry"] = STS2ActionRegistry().build(snapshot)
    engine = STS2ActionEngine()
    responses = iter([
        '{"action_type":"play_card","kwargs":{"card_index":0,"target_index":0}}',
        '{"action_type":"play_card","kwargs":{"card_index":0,"target_index":1},"reason":"当前目标合法"}',
    ])
    seen = []
    planner = STS2LLMPlanner(Mock(), persona_provider=lambda: "我是 N.E.K.O 当前角色，有自己的身份与关系。")

    async def ensure():
        return object()

    async def invoke(llm, messages):
        seen.append([dict(item) for item in messages])
        return next(responses)

    monkeypatch.setattr(planner, "_ensure_llm", ensure)
    monkeypatch.setattr(planner, "_invoke_text", invoke)
    operation = asyncio.run(planner.plan(
        {"agent_view": view, "session": {"recent_dialogue": []}},
        validate=lambda op: engine.validate_with_feedback(snapshot, op),
    ))
    assert operation.source == "llm"
    assert operation.kwargs == {"card_index": 0, "target_index": 1}
    assert seen[0][0]["content"].startswith("我是 N.E.K.O 当前角色")
    # 请求体按约定省略与 actions 完全相同的 available_actions，其余现场原样保留。
    assert json.loads(seen[0][1]["content"])["agent_view"] == {k: v for k, v in view.items() if k != "available_actions"}
    assert len(seen) == 2  # 非法目标交还模型，未静默替换成 1。
    reward_snapshot = {"agent_view": {"reward": {"rewards": [{"i": 0, "claimable": True}]}},
                       "action_registry": [{"type": "collect_rewards_and_proceed"}]}
    batch = {"source": "llm", "action_type": "collect_rewards_and_proceed", "kwargs": {}}
    assert engine.validate(reward_snapshot, batch) is None
    assert "默认选牌" in engine.validate_with_feedback(reward_snapshot, batch)[1]
    reward_snapshot["agent_view"]["reward"]["rewards"] = []
    assert engine.validate(reward_snapshot, batch) is not None
    planner._persona_provider = lambda: ""
    with pytest.raises(RuntimeError, match="主人格"):
        asyncio.run(planner.plan({"agent_view": view}))


def test_companion_identity_stale_execution_and_paused_chat(monkeypatch):
    service = STS2AutoplayService(Mock(), lambda _: None)
    view = {"screen": "MAP", "run_id": "run-1", "map": {"options": [{"i": 0}]},
            "actions": [{"name": "choose_map_node", "requires_index": True}]}
    snapshot = normalize_snapshot({"agent_view": view}, {"actions": view["actions"]})
    snapshot["action_registry"] = service._action_registry.build(snapshot)
    service._state.snapshot = snapshot
    service._companion_session.observe(view)
    host, companion = Mock(), Mock()
    host.set_companion_status = AsyncMock()
    service._host_client = host
    service._client = host
    service._catgirl_client = companion
    companion.base_url = "http://127.0.0.1:18081"
    assert service._companion_client() is companion

    async def refresh(**kwargs):
        return {"snapshot": service._state.snapshot}

    async def read_dialogue():
        return None

    dispatched = []

    async def execute(operation):
        dispatched.append(operation)
        return {"status": "ok", "executed": True}

    monkeypatch.setattr(service, "refresh_state", refresh)
    monkeypatch.setattr(service, "_read_companion_dialogue", read_dialogue)
    monkeypatch.setattr(service, "_execute_operation", execute)
    operation = {"source": "llm", "action_type": "choose_map_node", "kwargs": {"option_index": 0},
                 "view_signature": view_signature(view), "control_mode": service._state.control_mode,
                 "session_generation": service._companion_session.generation}
    service._state.snapshot = normalize_snapshot({"agent_view": {**view, "screen": "EVENT"}}, {})
    assert asyncio.run(service.execute_operation(operation))["status"] == "stale"
    assert dispatched == []
    service._state.snapshot = snapshot
    assert asyncio.run(service.execute_operation(operation))["executed"]
    service._state.discussion_active = True
    assert not asyncio.run(service.execute_operation(operation))["executed"]
    service._llm_planner.plan = Mock(side_effect=AssertionError("聊天不能启动动作决策"))
    result = asyncio.run(service.chat_companion("你现在怎么样？"))
    assert result["agent_view"] == view
    assert result["executed"] is False
    assert len(dispatched) == 1
    service._state.discussion_active = False
    service._state.control_mode = "program"

    async def gate(*args, **kwargs):
        return {"applied": True, "settled": True}

    from plugin.plugins.sts2_autoplay.planner_interface import PlannedOperation
    calls = []

    async def plan(*args, **kwargs):
        calls.append(args)
        return PlannedOperation("choose_map_node", {"option_index": 0}, 1.0, "llm", "投票")

    monkeypatch.setattr(service, "_sync_mod_gate", gate)
    monkeypatch.setattr(service._llm_planner, "plan", plan)
    assert asyncio.run(service.run_autoplay_step())["executed"]
    assert not asyncio.run(service.run_autoplay_step())["executed"]
    assert len(calls) == 1  # 已投票且局面未变时等待队友，不重复调用模型或改票。


def test_session_consumes_actual_host_dialogue_without_duplicates_or_cross_run_memory():
    session = CompanionSession()
    session.observe({"run_id": "one", "run": {"character": "故障机器人", "floor": 3}})
    stamp = session.started_at
    records = [
        SimpleNamespace(timestamp=stamp + 1, lanlan_name="当前角色", turn_type="user_message", content="刚才太危险了"),
        SimpleNamespace(timestamp=stamp + 2, lanlan_name="当前角色", turn_type="assistant_message", content="我记得，下一步会考虑。"),
        SimpleNamespace(timestamp=stamp + 3, lanlan_name="其他角色", turn_type="user_message", content="无关会话"),
        SimpleNamespace(timestamp=stamp + 4, lanlan_name="当前角色", turn_type="proactive_instruction", content="内部推送提示"),
    ]
    session.import_dialogue(records, character_name="当前角色")
    generation = session.generation
    session.import_dialogue(records, character_name="当前角色")
    assert session.generation == generation
    assert [item["role"] for item in session.messages] == ["user", "assistant"]
    assert session.facts["floor"] == 3
    for i in range(30):
        session.remember("assistant", str(i))
    assert len(session.messages) == 16
    session.observe({"run_id": "two", "run": {"floor": 1}})
    assert list(session.messages) == []
    assert session.facts["floor"] == 1


def test_planner_forwards_agent_facts_supplement(monkeypatch):
    """决策请求必须带上 agent_facts 补齐字段：敌人意图、双方 buff、遗物/药水效果。

    背景：agent_view 有意裁掉了这些（只留招式 id / 名字），LLM 靠它判断"该防还是该抢输出"
    会缺信息。service 从同一个 raw_state 抽取补进 planner context，这里钉死它确实进模型请求。
    """
    from plugin.plugins.sts2_autoplay.agent_facts import build_agent_facts
    view = {"screen": "COMBAT", "turn": 3, "run_id": "r1", "available_actions": ["end_turn"]}
    raw_state = {
        "combat": {
            "player": {"powers": [{"name": "力量", "amount": 2}]},
            "enemies": [{"index": 0, "name": "下水道蚌", "is_alive": True, "intent": "RAM_MOVE",
                         "intents": [{"intent_type": "Attack", "total_damage": 10, "hits": 1}]}],
        },
        "run": {
            "relics": [{"name": "燃烧之血", "description": "战斗结束时回复[green]{Heal}[/green]点生命。"}],
            "potions": [{"index": 0, "name": "幸运补剂", "description": "获得[blue]{BufferPower}[/blue]层缓冲。",
                         "occupied": True, "can_use": True}],
        },
    }
    seen = []
    planner = STS2LLMPlanner(Mock(), persona_provider=lambda: "人格")
    planner._available = True

    async def ensure():
        return object()

    async def invoke(llm, messages):
        seen.append(json.loads(messages[1]["content"]))
        return '{"action_type":"end_turn","kwargs":{},"reason":"先防"}'

    monkeypatch.setattr(planner, "_ensure_llm", ensure)
    monkeypatch.setattr(planner, "_invoke_text", invoke)
    asyncio.run(planner.plan({"agent_view": view, "agent_facts": build_agent_facts(raw_state),
                              "session": {"recent_dialogue": []}}))
    sent = seen[0]["agent_facts"]
    assert sent["player_powers"] == ["力量2"]
    assert sent["enemies"][0]["intent"] == "攻击 10"
    assert sent["relics"][0]["effect"] == "战斗结束时回复（Heal）点生命。"
    assert sent["potions"][0]["effect"] == "获得（BufferPower）层缓冲。"


def test_bad_future_plan_is_dropped_without_rejecting_the_action(monkeypatch):
    """模型附带的 future_plan 格式不合格时只丢弃这份计划，绝不拒掉本次合法动作。

    2026-10-12 现场：resolve_rewards / choose_map_node / play_card 本来就合法，却因为模型顺手
    给的 future_plan 回合号对不上，被反复 raise 拒掉（单日 11 次 [sts2_llm_invalid]）。奖励屏
    因此推不动、一路卡到 [sts2_stuck] repeat=8。future_plan 与 continuation 同属"可选锦上添花"，
    与动作能否执行无关 —— 格式不对丢计划、保动作。
    """
    from plugin.plugins.sts2_autoplay import catgirl_llm
    monkeypatch.setattr(catgirl_llm, "create_chat_llm_async", Mock())
    monkeypatch.setattr(catgirl_llm, "get_config_manager", Mock())
    view = {"screen": "REWARD", "reward": {"rewards": [], "pending_card_choice": False},
            "actions": ["resolve_rewards"], "available_actions": ["resolve_rewards"]}
    seen = []
    planner = STS2LLMPlanner(Mock(), persona_provider=lambda: "人格")
    planner._available = True

    async def ensure():
        return object()

    # current_run.turn 缺失（非战斗屏）：按旧代码这是致命的，现在必须只是丢弃 future_plan。
    async def invoke(llm, messages):
        seen.append(len(messages))
        return ('{"action_type":"resolve_rewards","kwargs":{},"reason":"离开奖励",'
                '"future_plan":{"turns":[{"turn":1,"intent":"打左"}]}}')

    monkeypatch.setattr(planner, "_ensure_llm", ensure)
    monkeypatch.setattr(planner, "_invoke_text", invoke)
    operation = asyncio.run(planner.plan(
        {"agent_view": view, "session": {"recent_dialogue": []}},
        validate=lambda op: ({"ok": True}, ""),
    ))
    assert operation is not None and operation.action_type == "resolve_rewards"
    assert operation.future_plan is None, "非法 future_plan 必须被丢弃，而不是让整条动作作废"
    assert len(seen) == 1, "不该因附带的坏计划白烧第二次模型往返"


def test_engine_allows_indexless_reward_batch_actions():
    """奖励已处理完后，无 index 的 resolve_rewards / collect_rewards_and_proceed 应放行。

    Mod 的 resolve_rewards 处理器写着 "absent = auto (first card)"、"collect...proceed"
    是纯离场，但 Mod 描述符给 resolve_rewards 标了 requires_index=true（与自身实现不一致）。
    插件据此误拒合法动作 —— 这里豁免 requires_index。奖励未处理完的闸仍生效。
    """
    engine = STS2ActionEngine()
    snapshot = {"agent_view": {"reward": {"rewards": [], "pending_card_choice": None}},
                "action_registry": [{"type": "resolve_rewards", "requires_index": True}]}
    assert engine.validate(snapshot, {"source": "llm", "action_type": "resolve_rewards",
                                      "kwargs": {}}) is not None
    # 奖励未处理完：仍必须逐项处理（不因豁免而放开）。
    pending = {"agent_view": {"reward": {"rewards": [{"i": 0, "claimable": True}]}},
               "action_registry": [{"type": "resolve_rewards", "requires_index": True}]}
    assert engine.validate(pending, {"source": "llm", "action_type": "resolve_rewards",
                                     "kwargs": {}}) is None
    assert "默认选牌" in engine.validate_with_feedback(
        pending, {"source": "llm", "action_type": "resolve_rewards", "kwargs": {}})[1]
    service = STS2AutoplayService(Mock(), lambda _: None)
    potion_reward = {"raw_state": {"run": {"potions": [{"occupied": True}, {"occupied": True}]},
        "reward": {"rewards": [{"index": 0, "reward_type": "Potion"}]}},
        "agent_view": {"reward": {"rewards": [{"i": 0, "line": "Potion: 固化药水", "claimable": True}]}},
        "action_registry": [{"type": "claim_reward"}, {"type": "resolve_rewards"}]}
    claim = {"source": "llm", "action_type": "claim_reward", "kwargs": {"option_index": 0}}
    assert "先discard_potion" in service._validate_companion_action(potion_reward, claim)[1]
    normalized = normalize_snapshot({**potion_reward["raw_state"], "agent_view": potion_reward["agent_view"]}, {})
    normalized["action_registry"] = potion_reward["action_registry"]
    assert not normalized["agent_view"]["reward"]["rewards"][0]["claimable"]
    assert potion_reward["agent_view"]["reward"]["rewards"][0]["claimable"]  # 原始现场仍保留。
    assert service._validate_companion_action(normalized, {"source": "llm", "action_type": "resolve_rewards", "kwargs": {}})[0]
    potion_reward["raw_state"]["run"]["potions"][0]["occupied"] = False
    assert service._validate_companion_action(potion_reward, claim)[0]


def test_explicit_card_skip_resolves_reward_overview_without_reopening_cards(monkeypatch):
    snapshot = {"agent_view": {"screen": "REWARD", "actions": ["resolve_rewards", "claim_reward"],
        "reward": {"pending_card_choice": False, "rewards": [{"i": 1, "claimable": True, "line": "Card"}]}},
        "action_registry": [{"type": "resolve_rewards"}, {"type": "claim_reward", "requires_index": True}]}
    engine = STS2ActionEngine()
    planner = STS2LLMPlanner(Mock(), persona_provider=lambda: "小铃")
    monkeypatch.setattr(planner, "_ensure_llm", AsyncMock(return_value=object()))
    answers = [{"action_type": "resolve_rewards", "kwargs": {}},
               {"action_type": "resolve_rewards", "kwargs": {"option_index": -1}, "reason": "刚才已明确跳过三选一，离开奖励。"}]
    invoke = AsyncMock(side_effect=[json.dumps(a, ensure_ascii=False) for a in answers])
    monkeypatch.setattr(planner, "_invoke_text", invoke)
    operation = asyncio.run(planner.plan({"agent_view": snapshot["agent_view"]},
        validate=lambda op: engine.validate_with_feedback(snapshot, op)))
    assert operation.action_type == "resolve_rewards" and operation.kwargs == {"option_index": -1}
    assert invoke.await_count == 2
    assert '"option_index":-1' in invoke.await_args_list[1].args[1][-1]["content"]
    assert engine.validate(snapshot, {"source": "llm", "action_type": "claim_reward", "kwargs": {"option_index": -1}}) is None
    assert engine.validate(snapshot, {"source": "llm", "action_type": "resolve_rewards", "kwargs": {"option_index": -2}}) is None


def test_music_request_keeps_game_plan_and_returns_separate_media_operation(monkeypatch):
    session = CompanionSession()
    session.observe({'screen': 'COMBAT', 'run_id': 'r', 'turn': 1})
    session.remember('user', '放一首周杰伦的晴天')
    message = session.pending_messages()[0]
    planner = STS2LLMPlanner(Mock(), persona_provider=lambda: '当前人格')
    monkeypatch.setattr(planner, '_ensure_llm', AsyncMock(return_value=object()))
    answer = {'action_type': None, 'reply': '我去找这首歌。', 'message_effects': {message['id']: 'chat'},
              'plan_update': 'keep', 'media_requests': [{'kind': 'music', 'query': '晴天', 'artist': '周杰伦'}]}
    monkeypatch.setattr(planner, '_invoke_text', AsyncMock(return_value=json.dumps(answer, ensure_ascii=False)))
    classified = []
    solver = AsyncMock()
    operation = asyncio.run(planner.plan({'agent_view': {'screen': 'COMBAT', 'actions': ['play_card']},
        'new_player_messages': [message], 'actions_enabled': True, 'selected_plan_valid': True},
        route_messages=classified.append, solver=solver))
    solver.assert_not_awaited()
    assert operation.plan_update == 'keep' and not operation.action_type
    assert operation.media_requests == answer['media_requests']
    assert classified == [{message['id']: 'chat'}]
    assert operation.reply == '我去找这首歌。'
    assert operation.reply_kind == 'chat'


def test_tactical_query_marks_reply_without_changing_game_plan(monkeypatch):
    session = CompanionSession()
    session.observe({'screen': 'COMBAT', 'run_id': 'r', 'turn': 1})
    session.remember('user', '这回合怎么出牌？')
    message = session.pending_messages()[0]
    planner = STS2LLMPlanner(Mock(), persona_provider=lambda: '当前人格')
    monkeypatch.setattr(planner, '_ensure_llm', AsyncMock(return_value=object()))
    answer = {'action_type': None, 'reply': '这回合先防守，等下一轮输出。', 'reply_kind': 'tactical',
              'message_effects': {message['id']: 'query'}, 'plan_update': 'keep'}
    monkeypatch.setattr(planner, '_invoke_text', AsyncMock(return_value=json.dumps(answer, ensure_ascii=False)))
    operation = asyncio.run(planner.plan({'agent_view': {'screen': 'COMBAT', 'actions': []},
        'new_player_messages': [message], 'actions_enabled': False}))
    assert operation.reply_kind == 'tactical' and operation.plan_update == 'keep' and not operation.action_type


@pytest.mark.parametrize("form", ["plain", "preface", "fence", "truncated", "nondecision", "trailing"])
def test_complete_final_decision_json_preserves_plan_without_accepting_partial_output(form):
    from plugin.plugins.sts2_autoplay.llm_planner import _decision_json
    answer = {"action_type": "play_card", "kwargs": {"card_ref": "c9"},
        "speech": "先防御，再出打击。", "continuation": [{"action_type": "play_card", "kwargs": {"card_ref": "c7", "target_ref": "e1"}}, {"action_type": "end_turn"}]}
    body = json.dumps(answer, ensure_ascii=False)
    if form == "preface":
        body = '先按当前现场安排这一轮。\n\n' + body
    elif form == "fence":
        body = '先按现场安排。\n```json\n' + body + '\n```'
    elif form == "truncated":
        body = '说明\n' + body[:-1]
    elif form == "nondecision":
        body = '说明\n' + '{"scene":{"action_type":"end_turn"}}'
    elif form == "trailing":
        body = '说明\n' + body + '\n还有其他不完整输出'
    if form in {"truncated", "nondecision", "trailing"}:
        with pytest.raises(json.JSONDecodeError):
            _decision_json(body)
    else:
        assert _decision_json(body) == answer
