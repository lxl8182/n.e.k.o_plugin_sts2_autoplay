from __future__ import annotations

import asyncio
from copy import deepcopy
import json
from unittest.mock import AsyncMock, Mock
import pytest

from plugin.plugins.sts2_autoplay.action_identity import action_refs, bind_action, resolve_action, observation_guard
from plugin.plugins.sts2_autoplay.llm_planner import STS2LLMPlanner
from plugin.plugins.sts2_autoplay.service import STS2AutoplayService


def scene():
    return {"agent_view": {"screen": "COMBAT", "run_id": "r", "turn": 1,
            "actions": ["play_card", "end_turn"], "combat": {"player": {"energy": 3}, "hand": [
                {"i": 0, "line": "打击", "playable": True},
                {"i": 1, "line": "防御", "playable": True},
                {"i": 2, "line": "打击", "playable": True}]}},
            "raw_state": {"in_combat": True, "turn": 1, "combat": {"hand": [
                {"index": 0, "entity_id": "s-card-1", "name": "打击", "target_index_space": "enemies"},
                {"index": 1, "entity_id": "s-card-2", "name": "防御"},
                {"index": 2, "entity_id": "s-card-3", "name": "打击", "target_index_space": "enemies"}],
                "enemies": [{"index": 0, "entity_id": "s-enemy-4", "name": "甲", "current_hp": 16},
                            {"index": 1, "entity_id": "s-enemy-5", "name": "乙", "current_hp": 20}],
                "players": [{"is_local": True, "player_id": "1", "is_action_phase": True}]}}}


@pytest.mark.parametrize("kind", ["immediate", "guidance"])
def test_immediate_flow_request_does_not_drop_persistent_guidance(monkeypatch, kind):
    from plugin.plugins.sts2_autoplay.tests.test_companion_development import prepare
    service = prepare(monkeypatch)
    session = service._companion_session
    session.remember("user", "我玩战士，你快选啦" if kind == "immediate" else "下个火堆休息")
    message = session.pending_messages()[0]
    answer = {"action_type": "choose_event_option", "kwargs": {"option_index": 5}, "plan_update": "replace",
              "reply": "收到。", "speech": "这就接着往下走。", "speech_scope": "intent",
              "message_effects": {message["id"]: kind}, "requirement_updates": []}
    fixed = {**answer, "requirement_updates": [{"op": "add", "source_message_id": message["id"],
             "text": "下个火堆休息", "scope": "scene", "scene": "REST"}]}
    answers = iter([answer, fixed])
    requests = []
    planner = service._llm_planner
    monkeypatch.setattr(planner, "_ensure_llm", AsyncMock(return_value=object()))
    async def invoke(llm, messages):
        requests.append(deepcopy(messages))
        return json.dumps(next(answers), ensure_ascii=False)
    monkeypatch.setattr(planner, "_invoke_text", invoke)
    operation = asyncio.run(planner.plan({"agent_view": service._state.snapshot["agent_view"],
        "action_refs": action_refs(service._state.snapshot), "active_requirements": [], "new_player_messages": [message]},
        validate=lambda op: service._validate_companion_action(service._state.snapshot, op), route_messages=session.resolve_messages))
    assert operation.action_type == "choose_event_option" and not session.pending_messages()
    assert session.user_requests[-1]["kind"] == kind and session.generation > 0
    if kind == "immediate":
        assert len(requests) == 1 and not operation.requirement_updates and not session.requirements.records
        assert not session.context()["player_messages"]
    else:
        assert len(requests) == 2 and "immediate" in requests[1][-1]["content"]
        session.apply_requirement_updates(operation.requirement_updates, service._state.snapshot, messages=[message])
        assert session.active_requirements()[0]["scene"] == "REST"


@pytest.mark.parametrize("kwargs", [{"option_index": 8}, {"card_index": 8}, {"card_ref": "8"},
                                   {"card_ref": "i=8"}, {"card_ref": "selection-choice-b"}])
def test_selection_aliases_bind_current_page_identity_and_stop_on_page_change(monkeypatch, kwargs):
    snapshot = {"agent_view": {"screen": "CARD_SELECTION", "run_id": "r", "run": {"floor": 1},
        "actions": ["select_deck_card"], "selection": {"cards": [{"i": 0, "line": "打击"}, {"i": 8, "line": "电击"}]}},
        "raw_state": {"run": {"floor": 1}, "selection": {"selection_id": "page-1", "kind": "deck_upgrade_select", "cards": [
            {"index": 0, "entity_id": "choice-a", "name": "打击"}, {"index": 8, "entity_id": "choice-b", "name": "电击"}]}}}
    snapshot["action_registry"] = [{"type": "select_deck_card", "requires_index": True}]
    from plugin.plugins.sts2_autoplay.action_engine import STS2ActionEngine
    engine = STS2ActionEngine()
    planner = STS2LLMPlanner(Mock(), persona_provider=lambda: "小铃")
    monkeypatch.setattr(planner, "_ensure_llm", AsyncMock(return_value=object()))
    requests = []
    async def invoke(llm, messages):
        requests.append(deepcopy(messages))
        return json.dumps({"action_type": "select_deck_card", "kwargs": kwargs, "speech": "升级电击。"}, ensure_ascii=False)
    monkeypatch.setattr(planner, "_invoke_text", invoke)
    operation = asyncio.run(planner.plan({"conversation_owner": "selection-test", "agent_view": snapshot["agent_view"], "action_refs": action_refs(snapshot)},
        validate=lambda op: engine.validate_with_feedback(snapshot, op)))
    assert len(requests) == 1 and operation.kwargs == {"option_index": 8}
    assert operation.bindings["selection_card"]["entity_id"] == "choice-b"
    payload = json.loads(requests[0][-1]["content"])
    assert payload["action_contracts"]["select_deck_card"]["valid_option_indices"] == [0, 8]
    assert json.loads(planner._history[-1]["content"])["kwargs"] == {"option_index": 8}
    after = deepcopy(snapshot)
    after["raw_state"]["selection"]["cards"][1]["index"] = 3
    assert resolve_action(operation.as_dict(), after)[0]["kwargs"] == {"option_index": 3}
    after["raw_state"]["selection"]["selection_id"] = "page-2"
    assert resolve_action(operation.as_dict(), after)[1]["code"] == "selection_card_changed"
    for invalid in ({"card_ref": "c8"}, {"card_ref": "8", "option_index": 0}, {"card_index": 99}, {"option_index": True}):
        with pytest.raises(ValueError, match="option_index"):
            bind_action("select_deck_card", invalid, action_refs(snapshot))


def test_requirement_repair_lists_source_and_scene_in_one_round(monkeypatch):
    from plugin.plugins.sts2_autoplay.tests.test_companion_development import prepare
    service = prepare(monkeypatch)
    session = service._companion_session
    session.remember("user", "下个火堆休息")
    message = session.pending_messages()[0]
    update = {"op": "add", "scope": "scene", "text": "下个火堆休息"}
    answer = {"action_type": None, "plan_update": "keep", "reply": "下个火堆我会休息。",
              "message_effects": {message["id"]: "guidance"}, "requirement_updates": [update]}
    fixed = {**answer, "requirement_updates": [{**update, "source_message_id": message["id"], "scene": "REST"}]}
    answers, requests = iter([answer, fixed]), []
    planner = service._llm_planner
    monkeypatch.setattr(planner, "_ensure_llm", AsyncMock(return_value=object()))
    async def invoke(llm, messages):
        requests.append(deepcopy(messages))
        return json.dumps(next(answers), ensure_ascii=False)
    monkeypatch.setattr(planner, "_invoke_text", invoke)
    operation = asyncio.run(planner.plan({"agent_view": service._state.snapshot["agent_view"],
        "active_requirements": [], "new_player_messages": [message]},
        validate=lambda op: service._validate_companion_action(service._state.snapshot, op), route_messages=session.resolve_messages))
    feedback = requests[1][-1]["content"]
    assert len(requests) == 2 and all(field in feedback for field in ("source_message_id", "scene", "REST"))
    assert operation.requirement_updates[0]["scene"] == "REST" and "scene" not in update


def test_natural_speech_preserves_upgraded_card_explanation_without_appending_plan(monkeypatch):
    snapshot = scene()
    snapshot["raw_state"]["combat"]["hand"][0]["name"] = "打击+"
    snapshot["agent_view"]["combat"]["hand"][0]["line"] = "打击+"
    snapshot["raw_state"]["combat"]["enemies"][0]["name"] = "小啃兽"
    speech = "开场先垫防御再打，小啃兽这回合打12，防御够挡，中间用打击+压一下血。"
    answer = {"action_type": "play_card", "kwargs": {"card_ref": "c2"},
              "speech": speech, "speech_scope": "steps", "speech_step_ids": [0, 1, 2],
              "continuation": [{"action_type": "play_card", "kwargs": {"card_ref": "c1", "target_ref": "e4"}},
                               {"action_type": "end_turn", "kwargs": {}}]}
    planner = STS2LLMPlanner(Mock(), persona_provider=lambda: "小铃")
    monkeypatch.setattr(planner, "_ensure_llm", AsyncMock(return_value=object()))
    monkeypatch.setattr(planner, "_invoke_text", AsyncMock(return_value=json.dumps(answer, ensure_ascii=False)))
    operation = asyncio.run(planner.plan({"agent_view": snapshot["agent_view"], "action_refs": action_refs(snapshot),
                                         "turn_speech_required": True}))
    assert operation.speech == speech and len(operation.continuation) == 2
    assert planner.metrics["model_calls"] == 1


@pytest.mark.parametrize("case", ["repair", "keep_valid", "wait", "paused"])
def test_solver_keep_without_existing_plan_requires_explicit_replacement(monkeypatch, case):
    snapshot = scene()
    for card, card_id in zip(snapshot["raw_state"]["combat"]["hand"], ("STRIKE", "DEFEND", "STRIKE")):
        card["card_id"] = card_id
    answer = {"plan_source": "solver", "plan_update": "wait" if case == "wait" else "keep",
              "action_type": None, "reason": "采用求解器的完整打线", "speech": "两张打击打甲，再结束回合。"}
    planner = STS2LLMPlanner(Mock(), persona_provider=lambda: "小铃")
    monkeypatch.setattr(planner, "_ensure_llm", AsyncMock(return_value=object()))
    requests = []
    async def invoke(llm, messages):
        requests.append(deepcopy(messages))
        return json.dumps({**answer, **({"plan_update": "replace"} if len(requests) == 2 else {})}, ensure_ascii=False)
    monkeypatch.setattr(planner, "_invoke_text", invoke)
    solver = AsyncMock(return_value={"status": "ok", "solver_result": {"line": [{"turn": 1, "steps": [
        {"kind": "play_card", "card_id": "STRIKE", "card_index": 0, "target_index": 0},
        {"kind": "play_card", "card_id": "STRIKE", "card_index": 0, "target_index": 0},
        {"kind": "end_turn"}]}]}})
    operation = asyncio.run(planner.plan({"agent_view": snapshot["agent_view"], "action_refs": action_refs(snapshot),
        "selected_plan_valid": case == "keep_valid", "actions_enabled": case != "paused"}, solver=solver))
    if case == "repair":
        assert len(requests) == 2 and "plan_update=replace" in requests[1][-1]["content"]
        assert operation.solver_adopted and operation.plan_update == "replace"
        assert operation.bindings["card"]["ref"] == "c1"
        assert operation.continuation[0]["bindings"]["card"]["ref"] == "c3"
        assert operation.continuation[1]["action_type"] == "end_turn"
    else:
        assert len(requests) == 1 and not operation.action_type
        assert operation.plan_update == ("wait" if case == "wait" else "keep")
    assert solver.await_count == (0 if case == "paused" else 1)


def test_identity_and_conditions_survive_coop_damage_and_index_changes():
    before = scene()
    refs = action_refs(before)
    for invalid in ("优先最低血量", [{"kind": "player", "hp_min": 1}] * 9):
        with pytest.raises(ValueError, match="assumptions.*条件对象数组"):
            bind_action("play_card", {"card_ref": "c3", "target_ref": "e5"}, refs, invalid)
    kwargs, bindings, assumptions = bind_action("play_card", {"card_ref": "c3", "target_ref": "e5"}, refs)
    step = {"action_type": "play_card", "kwargs": kwargs, "bindings": bindings, "assumptions": assumptions}
    after = deepcopy(before)
    after["raw_state"]["combat"]["hand"].pop(0)
    after["raw_state"]["combat"]["enemies"].pop(0)
    for i, c in enumerate(after["raw_state"]["combat"]["hand"]):
        c["index"] = i
    after["raw_state"]["combat"]["enemies"][0].update(index=0, current_hp=8)
    resolved, error = resolve_action(step, after)
    assert error is None
    assert resolved["kwargs"] == {"card_index": 1, "target_index": 0,
                                  "expected_card_entity_id": "s-card-3", "expected_enemy_entity_id": "s-enemy-5"}
    # 同名牌身份不同，目标死亡不能替换成另一只怪。
    after["raw_state"]["combat"]["hand"][1]["entity_id"] = "different-card"
    assert resolve_action(step, after)[1]["code"] == "card_missing"
    after = scene()
    after["raw_state"]["combat"]["enemies"][1]["current_hp"] = 10
    for condition, valid in (({"kind": "enemy_hp", "ref": "e5", "max": 12}, True),
                             ({"kind": "enemy_hp", "ref": "e5", "min": 12}, False),
                             ({"kind": "hp_order", "ref": "e5", "order": "lowest"}, True),
                             ({"kind": "hp_spread", "refs": ["e4", "e5"], "max_gap": 4}, False)):
        _, _, conditions = bind_action("play_card", {"card_ref": "c3", "target_ref": "e5"}, refs, [condition])
        assert (resolve_action({**step, "assumptions": conditions}, after)[1] is None) is valid
    _, _, conditions = bind_action("play_card", {"card_ref": "c3", "target_ref": "e5"}, refs,
                                   [{"kind": "enemy_hp", "ref": "e4", "max": 0}])
    after["raw_state"]["combat"]["enemies"].pop(0)
    assert resolve_action({**step, "assumptions": conditions}, after)[1] is None
    # 模型思考/发言期间队友削血不废弃仍可执行的动作。
    service = STS2AutoplayService(Mock(), lambda _: None)
    service._state.snapshot = after
    assert service._resolve_dispatch({**step, "observation_guard": observation_guard(before)})[1] == ""
    guarded = scene()
    guarded["agent_view"]["combat"]["player"]["block"] = 3
    service._state.snapshot = deepcopy(guarded)
    service._state.snapshot["agent_view"]["combat"]["player"]["block"] = 6
    service._state.snapshot["agent_view"]["actions"].reverse()
    service._state.snapshot["raw_state"]["combat"]["enemies"][0]["powers"] = [{"name": "易伤", "amount": 2}]
    assert service._resolve_dispatch({**step, "observation_guard": observation_guard(guarded)})[1] == ""
    service._state.snapshot["raw_state"]["combat"]["enemies"][1]["powers"] = [{"name": "力量", "amount": 2}]
    assert service._resolve_dispatch({**step, "observation_guard": observation_guard(guarded)})[0] is None
    service._companion_session.observe(before["agent_view"])
    record = {}
    service._pending_action = {"operation": {"action_type": "end_turn", "kwargs": {}},
                              "before": before, "anchor": service._companion_session.anchor(), "record": record}
    after["raw_state"]["combat"]["players"][0]["ready_to_end_turn"] = True
    assert service._resolve_pending_action(after)["executed"]
    assert record["status"] == "completed"
    # 队友补票只改变票数展示；自身已投或可选节点变化仍须重新检查。
    map_before = {"agent_view": {"screen": "MAP", "map": {"local_vote": None, "votes": [],
        "options": [{"i": 0, "line": "Monster (2,0) [votes:0]"}]}}}
    service._state.snapshot = deepcopy(map_before)
    service._state.snapshot["agent_view"]["map"]["votes"] = [{"player_id": "master", "coord": "2,0"}]
    service._state.snapshot["agent_view"]["map"]["options"][0]["line"] = "Monster (2,0) [votes:1]"
    vote = {"action_type": "choose_map_node", "kwargs": {"option_index": 0}, "observation_guard": observation_guard(map_before)}
    assert service._resolve_dispatch(vote)[1] == ""
    service._state.snapshot["agent_view"]["map"]["votes"][0]["coord"] = "2,1"
    assert service._resolve_dispatch(vote)[0] is None
    service._state.snapshot["agent_view"]["map"]["votes"][0]["coord"] = "2,0"
    service._state.snapshot["agent_view"]["map"]["options"][0]["line"] = "Monster (3,0)"
    assert service._resolve_dispatch(vote)[0] is None
    service._state.snapshot["agent_view"]["map"]["options"][0]["line"] = "Monster (2,0)"
    service._state.snapshot["agent_view"]["map"]["local_vote"] = {"row": 2, "col": 0}
    assert service._resolve_dispatch(vote)[0] is None


@pytest.mark.parametrize("types", [("Potion", "Potion"), ("Gold", "Card")])
def test_reward_continuation_resolves_content_after_reindexing(types):
    before = {"raw_state": {"run_id": "r", "screen": "REWARD", "run": {"floor": 7}, "reward": {"rewards": [
        {"index": 0, "reward_type": types[0], "description": "第一份", "claimable": True},
        {"index": 1, "reward_type": types[1], "description": "第二份", "claimable": True}]}},
        "agent_view": {"run_id": "r", "screen": "REWARD"}}
    kwargs, bindings, assumptions = bind_action("claim_reward", {"option_index": 1}, action_refs(before))
    step = {"action_type": "claim_reward", "kwargs": kwargs, "bindings": bindings, "assumptions": assumptions}
    after = deepcopy(before)
    after["raw_state"]["reward"]["rewards"].pop(0)
    after["raw_state"]["reward"]["rewards"][0]["index"] = 0
    resolved, invalid = resolve_action(step, after)
    assert invalid is None and resolved["kwargs"]["option_index"] == 0
    after["raw_state"]["reward"]["rewards"][0]["claimable"] = False
    assert resolve_action(step, after)[1]["code"] == "reward_unclaimable"
    after["raw_state"]["run"]["floor"] += 1
    assert resolve_action(step, after)[1]["code"] == "reward_missing"


def test_input_reply_survives_cancel_disconnect_and_reload(monkeypatch, tmp_path):
    from plugin.plugins.sts2_autoplay.tests.test_companion_development import prepare
    service = prepare(monkeypatch)
    service._ingress_active = True
    service._companion_session.remember("user", "刚才的选择不如另一项")
    message = service._companion_session.pending_messages()[0]
    message["request_id"] = "correction-7"
    service._companion_session.resolve_messages({message["id"]: "query"})
    record = service._companion_session.save_input_result([message], "我采纳这次纠正，已保存原回合案例。", media=[], kind="chat")
    service._companion_session.invalidate_plan("目标现场改变")
    failed = AsyncMock(return_value={"ok": False, "text_sent": False, "reason": "frontend_disconnected"})
    monkeypatch.setattr(service, "_companion_host_request", failed)
    assert not asyncio.run(service._flush_companion_replies())
    checkpoint = {"session": service._companion_session.context(), "character_name": "小铃"}
    service._companion_session.journal.directory = str(tmp_path)
    service._companion_session.journal.save_checkpoint(checkpoint)
    assert not record["text_sent"] and record["effects_saved"]

    restored = prepare(monkeypatch)
    restored._companion_session.journal.directory = str(tmp_path)
    restored._ingress_enabled = True
    restored._ingress_active = False
    monkeypatch.setattr(restored._catgirl_llm, "character_name", lambda: "小铃")
    monkeypatch.setattr(restored, "_companion_mode_active", lambda: True)
    host = AsyncMock(return_value={"ok": True, "persona": "小铃", "inputs": [
        {"request_id": "correction-7", "content": message["content"], "timestamp": message["timestamp"]}],
        "checkpoint": {}})
    monkeypatch.setattr(restored, "_companion_host_request", host)
    asyncio.run(restored._sync_companion_ingress())
    assert not restored._companion_session.pending_messages()
    restored._companion_session.input_results[0]["retry_at"] = 0
    host.return_value = {"ok": False, "text_sent": True, "audio_completed": None, "reason": "tts_unavailable"}
    assert asyncio.run(restored._flush_companion_replies())
    assert asyncio.run(restored._flush_companion_replies())
    assert host.await_count == 2  # poll与一次文本投递；TTS失败不再重答或重学。
    assert host.await_args.args[1]["output_id"] == "reply:correction-7"
    assert restored._companion_session.input_results[0]["text_sent"]


@pytest.mark.parametrize("screen", ["COMBAT", "CARD_SELECTION_REWARD"])
def test_audio_failure_keeps_choice_and_noncombat_does_not_wait(monkeypatch, screen):
    from plugin.plugins.sts2_autoplay.tests.test_companion_development import prepare
    service = prepare(monkeypatch)
    snapshot = scene() if screen == "COMBAT" else {"agent_view": {"screen": screen, "run_id": "r",
        "actions": ["choose_reward_card"], "reward": {"cards": [{"i": 0, "line": "打碎"}]}}, "raw_state": {"screen": screen}}
    action = "play_card" if screen == "COMBAT" else "choose_reward_card"
    kwargs = {"card_ref": "c2"} if screen == "COMBAT" else {"option_index": 0}
    kwargs, bindings, assumptions = bind_action(action, kwargs, action_refs(snapshot))
    snapshot["action_registry"] = [{"type": action}]
    service._state.snapshot = snapshot
    service._companion_session.observe(snapshot["agent_view"])
    service._ingress_active = True
    service._state.host_digest_enabled = True
    monkeypatch.setattr(service, "_read_companion_dialogue", AsyncMock())
    monkeypatch.setattr(service, "_push_catgirl", AsyncMock())
    monkeypatch.setattr(service, "_sync_execution_status", AsyncMock())
    dispatch = AsyncMock(return_value={"status": "ok", "executed": True})
    monkeypatch.setattr(service, "_execute_operation", dispatch)
    monkeypatch.setattr(service._llm_planner, "plan", AsyncMock(side_effect=AssertionError("不应重新规划")))
    host = AsyncMock(return_value={"ok": False, "text_sent": True, "audio_completed": False,
                                  "reason": "audio_completion_timeout"})
    monkeypatch.setattr(service, "_companion_host_request", host)
    operation = {"action_type": action, "kwargs": kwargs, "bindings": bindings, "assumptions": assumptions,
                 "source": "llm", "speech": "这次用防御稳住。" if screen == "COMBAT" else "选打碎吧。"}
    service._companion_session.accept_plan(operation, snapshot["agent_view"])
    service._selected_generation = (service._companion_session.generation, service._state.control_generation, service._state.guidance_generation)
    service._last_dialogue_generation = service._companion_session.generation
    operation.update(observation_guard=observation_guard(snapshot), session_generation=service._companion_session.generation,
        control_mode=service._state.control_mode, control_generation=service._state.control_generation,
        guidance_generation=service._state.guidance_generation)
    result = asyncio.run(STS2AutoplayService.execute_operation(service, operation))
    if screen != "COMBAT":
        assert result["executed"] and host.await_args.args[1]["wait_audio"] is False
        return
    assert result["status"] == "audio_timeout" and not dispatch.called
    assert service._step_timing["speech_ms"] >= 0
    from time import perf_counter
    service._record_latency(perf_counter(), "model", result)
    assert service._state.latency_samples[-1]["outcome"] == "audio_timeout"
    first_id = host.await_args.args[1]["output_id"]
    service._state.retry_at = 0
    host.return_value = {"ok": True, "text_sent": True, "audio_completed": True}
    monkeypatch.setattr(service, "execute_operation", STS2AutoplayService.execute_operation.__get__(service))
    result = asyncio.run(service.run_autoplay_step())
    assert result["executed"] and dispatch.await_count == 1
    assert host.await_args.args[1]["output_id"] == first_id
    assert service._state.latency_samples[-1]["path"] == "selected_plan"


def test_one_conversation_keeps_cached_prefix_and_binds_announced_steps(monkeypatch):
    from plugin.plugins.sts2_autoplay import catgirl_llm
    monkeypatch.setattr(catgirl_llm, "create_chat_llm_async", Mock())
    monkeypatch.setattr(catgirl_llm, "get_config_manager", Mock())
    snapshot = scene()
    requests = []
    answers = iter([
        {"action_type": "play_card", "kwargs": {"card_ref": "c1", "target_ref": "e4"},
         "speech": "我先〔步骤〕，喵。", "speech_step_ids": [0, 1], "continuation": [
             {"action_type": "play_card", "kwargs": {"card_ref": "c3", "target_ref": "e4"}, "assumptions": []}]},
        {"action_type": None, "reply": "我陪着你呢。", "plan_update": "keep", "message_effects": {"1": "chat"}},
    ])
    class Client:
        async def ainvoke_raw(self, messages, **kwargs):
            requests.append(deepcopy(messages))
            return {"choices": [{"message": {"content": json.dumps(next(answers), ensure_ascii=False)}}],
                    "usage": {"prompt_tokens": 2000, "prompt_tokens_details": {"cached_tokens": 1500}}}
    planner = STS2LLMPlanner(Mock(), persona_provider=lambda: "我是小铃，主人。")
    monkeypatch.setattr(planner, "_ensure_llm", AsyncMock(return_value=Client()))
    context = {"conversation_owner": "same-session", "agent_view": snapshot["agent_view"],
               "action_refs": action_refs(snapshot), "session": {}}
    async def run():
        plan = await planner.plan(context)
        assert plan.speech == "我先2次打击打甲，喵。"
        assert plan.bindings["card"]["entity_id"] != plan.continuation[0]["bindings"]["card"]["entity_id"]
        reply = await planner.plan({**context, "agent_view": {"screen": "COMBAT", "actions": []},
                                   "new_player_messages": [{"id": "1", "content": "今天有点累"}]})
        assert reply.reply == "我陪着你呢。" and reply.plan_update == "keep"
    asyncio.run(run())
    # 历史现场按简版追加：system 与前序历史不变，只有刚写入的那轮变成 compact 简版。
    assert requests[0][0]["content"] == requests[1][0]["content"]
    assert requests[1][1]["role"] == "user" and json.loads(requests[1][1]["content"])["compact"] == 1
    assert planner.metrics["model_calls"] == 2 and planner.metrics["cached_tokens"] == 3000
    prefix = planner._last_prefix
    old_rounds = []
    for i in range(8):
        old_rounds.extend([{"role": "user", "content": json.dumps({"new_player_messages": [{"id": str(i), "content": f"主人原话{i}"}],
            "agent_view": {"run_id": "r", "turn": i}, "scene": "动态现场" * 1000}, ensure_ascii=False)},
            {"role": "assistant", "content": json.dumps({"reply": f"真实回答{i}"}, ensure_ascii=False)}])
    latest = {"role": "user", "content": '{"new_player_messages":[]}' }
    for token_count in (81_000, 120_000):
        planner._last_prompt_tokens = token_count
        planner._commit_history([{"role": "system", "content": prefix}, *old_rounds, latest], {"content": '{}'})
        # 未到阈值不裁剪；历史现场按简版写入（compact 标记），不再整份回灌。
        assert len(planner._history) == len(old_rounds) + 2
        assert all(json.loads(m["content"]).get("compact") for m in planner._history[:-1] if m["role"] == "user")
        assert planner._history[-1] == {"role": "assistant", "content": '{}'}
    planner._last_prompt_tokens = 120_001
    planner._commit_history([{"role": "system", "content": prefix}, *old_rounds, latest], {"content": '{}'})
    assert planner._history[-2]["role"] == "user" and planner._last_prefix == prefix
    summary = json.loads(planner._history[0]["content"])["companion_history_summary"]
    assert summary[0]["messages"][0]["content"] == "主人原话0" and summary[0]["reply"] == "真实回答0"


@pytest.mark.parametrize("case", ["natural", "missing"])
def test_required_turn_intent_uses_selected_plan_and_repairs_missing_expression(monkeypatch, case):
    planner = STS2LLMPlanner(Mock(), persona_provider=lambda: "小铃")
    monkeypatch.setattr(planner, "_ensure_llm", AsyncMock(return_value=object()))
    snapshot = scene()
    good = {"action_type": "play_card", "kwargs": {"card_ref": "c1", "target_ref": "e4"},
            "speech": "这回合集中打甲，先压低它的血线，终于能还手了。", "speech_scope": "intent"}
    bad = {**good, "speech": "" if case == "missing" else "这回合先集中打乙。"}
    replies = [good] if case == "natural" else [bad]
    invoke = AsyncMock(side_effect=[{"content": json.dumps(a, ensure_ascii=False)} for a in replies])
    monkeypatch.setattr(planner, "_invoke_decision", invoke)
    operation = asyncio.run(planner.plan({"agent_view": snapshot["agent_view"],
        "action_refs": action_refs(snapshot), "turn_speech_required": True, "speech_allowed": True}))
    assert operation.speech == ("我先打击打甲。" if case == "missing" else good["speech"]) and operation.speech_step_ids == [0]
    assert invoke.await_count == len(replies)
    if case == "missing":
        assert invoke.await_args_list[-1].args[1][-1]["role"] == "user"


def test_missing_turn_speech_uses_model_speech_intent_instead_of_template(monkeypatch):
    # 2026-10-17 20:45:33：模型自然发言引用了被抽牌边界裁掉的续步而被清空，
    # 兜底不应退化成“我先X打Y。”；模型给了 speech_intent 时优先用它。
    planner = STS2LLMPlanner(Mock(), persona_provider=lambda: "小铃")
    monkeypatch.setattr(planner, "_ensure_llm", AsyncMock(return_value=object()))
    snapshot = scene()
    answer = {"action_type": "play_card", "kwargs": {"card_ref": "c1", "target_ref": "e4"},
              "speech": "", "speech_intent": "它这回合不打人，我先把输出铺开。"}
    invoke = AsyncMock(return_value={"content": json.dumps(answer, ensure_ascii=False)})
    monkeypatch.setattr(planner, "_invoke_decision", invoke)
    operation = asyncio.run(planner.plan({"agent_view": snapshot["agent_view"],
        "action_refs": action_refs(snapshot), "turn_speech_required": True, "speech_allowed": True}))
    assert operation.speech == "它这回合不打人，我先把输出铺开。"
    assert operation.speech_scope == "intent" and operation.speech_step_ids == [0]


def test_speech_window_wait_keeps_plan_and_resumes_without_model_or_repeating_speech(monkeypatch):
    from plugin.plugins.sts2_autoplay.tests.test_companion_development import prepare
    service = prepare(monkeypatch)
    snapshot = scene()
    snapshot["action_registry"] = [{"type": "play_card"}]
    service._state.snapshot = snapshot
    service._companion_session.observe(snapshot["agent_view"])
    service._ingress_active = True
    service._state.host_digest_enabled = True
    monkeypatch.setattr(service, "_read_companion_dialogue", AsyncMock())
    monkeypatch.setattr(service, "_push_catgirl", AsyncMock())
    monkeypatch.setattr(service, "_sync_execution_status", AsyncMock())
    dispatch = AsyncMock(return_value={"status": "ok", "executed": True})
    monkeypatch.setattr(service, "_execute_operation", dispatch)
    planner = AsyncMock(side_effect=AssertionError("窗口恢复不得重新调用模型"))
    monkeypatch.setattr(service._llm_planner, "plan", planner)
    async def host(*args, **kwargs):
        snapshot["agent_view"]["actions"] = []
        return {"ok": True, "text_sent": True, "audio_completed": True}
    voice = AsyncMock(side_effect=host)
    monkeypatch.setattr(service, "_companion_host_request", voice)
    kwargs, bindings, assumptions = bind_action("play_card", {"card_ref": "c2"}, action_refs(snapshot))
    operation = {"action_type": "play_card", "kwargs": kwargs, "bindings": bindings, "assumptions": assumptions,
                 "source": "llm", "speech": "先用防御稳住。", "observation_guard": observation_guard(snapshot),
                 "session_generation": service._companion_session.generation, "control_mode": service._state.control_mode,
                 "control_generation": service._state.control_generation, "guidance_generation": service._state.guidance_generation}
    service._companion_session.accept_plan(operation, snapshot["agent_view"])
    service._selected_generation = (service._companion_session.generation, service._state.control_generation, service._state.guidance_generation)
    service._last_dialogue_generation = service._companion_session.generation
    monkeypatch.setattr(service, "execute_operation", STS2AutoplayService.execute_operation.__get__(service))
    result = asyncio.run(service.execute_operation(operation))
    assert result["status"] == "waiting_for_actions" and dispatch.await_count == 0
    assert len(service._selected_steps) == 1 and service._companion_session.announced_plan
    snapshot["agent_view"]["actions"] = ["play_card", "end_turn"]
    result = asyncio.run(service.run_autoplay_step())
    assert result["executed"] and dispatch.await_count == 1 and voice.await_count == 1
    assert planner.await_count == 0


@pytest.mark.parametrize("change", ["energy", "intent", "input"])
def test_closed_window_still_rejects_changed_resources_intents_or_input(monkeypatch, change):
    from plugin.plugins.sts2_autoplay.tests.test_companion_development import prepare
    service = prepare(monkeypatch)
    snapshot = scene()
    service._state.snapshot = snapshot
    service._companion_session.observe(snapshot["agent_view"])
    monkeypatch.setattr(service, "_read_companion_dialogue", AsyncMock())
    dispatch = AsyncMock()
    monkeypatch.setattr(service, "_execute_operation", dispatch)
    kwargs, bindings, assumptions = bind_action("play_card", {"card_ref": "c2"}, action_refs(snapshot))
    operation = {"action_type": "play_card", "source": "llm", "kwargs": kwargs, "bindings": bindings,
                 "assumptions": assumptions, "observation_guard": observation_guard(snapshot),
                 "dialogue_revision": service._companion_session.dialogue_revision,
                 "session_generation": service._companion_session.generation, "control_mode": service._state.control_mode}
    snapshot["agent_view"]["actions"] = []
    if change == "energy":
        snapshot["agent_view"]["combat"]["player"]["energy"] = 1
    elif change == "intent":
        snapshot["raw_state"]["combat"]["enemies"][0]["intents"] = [{"intent_type": "Attack", "total_damage": 20}]
    else:
        service._companion_session.remember("user", "先别打这张")
    result = asyncio.run(STS2AutoplayService.execute_operation(service, operation))
    assert result["status"] == "stale" and dispatch.await_count == 0


@pytest.mark.parametrize("during_speech", [False, True])
def test_adopted_solver_plan_keeps_ally_damage_without_extra_calls(monkeypatch, during_speech):
    from plugin.plugins.sts2_autoplay.tests.test_companion_development import prepare
    service = prepare(monkeypatch)
    snapshot = scene()
    snapshot["raw_state"]["combat"]["enemies"][0]["current_hp"] = 39
    service._state.snapshot = snapshot
    service._companion_session.observe(snapshot["agent_view"])
    monkeypatch.setattr(service, "_read_companion_dialogue", AsyncMock())
    monkeypatch.setattr(service, "_push_catgirl", AsyncMock())
    monkeypatch.setattr(service, "_sync_execution_status", AsyncMock())
    solver = AsyncMock(side_effect=AssertionError("队友削血不额外求解"))
    monkeypatch.setattr(service, "_solve_for_companion", solver)
    planner = AsyncMock(side_effect=AssertionError("队友削血不额外调用模型"))
    monkeypatch.setattr(service._llm_planner, "plan", planner)
    dispatch = AsyncMock(return_value={"status": "ok", "executed": True})
    monkeypatch.setattr(service, "_execute_operation", dispatch)
    async def host(*args, **kwargs):
        if during_speech:
            snapshot["raw_state"]["combat"]["enemies"][0]["current_hp"] = 13
        return {"ok": True, "text_sent": True, "audio_completed": True}
    monkeypatch.setattr(service, "_companion_host_request", AsyncMock(side_effect=host))
    monkeypatch.setattr("plugin.plugins.sts2_autoplay.service.speak_current_turn", AsyncMock(side_effect=host))
    kwargs, bindings, assumptions = bind_action("play_card", {"card_ref": "c2"}, action_refs(snapshot))
    operation = {"action_type": "play_card", "kwargs": kwargs, "bindings": bindings, "assumptions": assumptions,
                 "source": "llm", "solver_adopted": True, "speech": "先用防御稳住。",
                 "observation_guard": observation_guard(snapshot), "session_generation": service._companion_session.generation,
                 "control_mode": service._state.control_mode, "control_generation": service._state.control_generation,
                 "guidance_generation": service._state.guidance_generation}
    service._companion_session.accept_plan(operation, snapshot["agent_view"])
    if not during_speech:
        snapshot["raw_state"]["combat"]["enemies"][0]["current_hp"] = 13
    result = asyncio.run(STS2AutoplayService.execute_operation(service, operation))
    assert result["executed"] and dispatch.await_count == 1
    solver.assert_not_awaited()
    planner.assert_not_awaited()


@pytest.mark.parametrize("scenario", ["duplicates", "different_faces", "draw"])
def test_solver_prefix_binds_distinct_entities_and_stops_at_unknown_information(scenario):
    snapshot = scene()
    hand = snapshot["raw_state"]["combat"]["hand"]
    for card, card_id in zip(hand, ("STRIKE", "DEFEND", "STRIKE")):
        card["card_id"] = card_id
    if scenario == "different_faces":
        snapshot["agent_view"]["combat"]["hand"][2]["line"] = "打击+"
    if scenario == "draw":
        snapshot["agent_view"]["combat"]["hand"][0]["line"] = snapshot["agent_view"]["combat"]["hand"][2]["line"] = "打击：抽1张牌。"
    refs = action_refs(snapshot)
    solver_line = [{"kind": "play_card", "card_index": 0, "card_id": "STRIKE", "target_index": 0},
                   {"kind": "play_card", "card_index": 0, "card_id": "STRIKE", "target_index": 0},
                   {"kind": "end_turn"}]
    if scenario == "unknown_card":
        solver_line[1]["card_id"] = "NOT_IN_HAND"
    result = {"status": "ok", "solver_refs": refs, "solver_result": {"line": [
        {"turn": 2 if scenario == "future" else 1, "steps": solver_line}]}}
    planner = STS2LLMPlanner(Mock(), persona_provider=lambda: "小铃")
    steps = planner._solver_steps(result, {"agent_view": snapshot["agent_view"], "action_refs": refs})
    if scenario in {"future", "different_faces"}:
        assert steps == []
    elif scenario in {"draw", "unknown_card"}:
        assert len(steps) == 1
    else:
        assert [s["kwargs"].get("card_ref") for s in steps] == ["c1", "c3", None]
        assert steps[0]["kwargs"]["target_ref"] == steps[1]["kwargs"]["target_ref"] == "e4"


@pytest.mark.parametrize("scenario", ["solver", "solver_natural", "solver_assumptions", "custom", "draw_continuation", "custom_attributes", "legacy_continuation", "flat", "flat_conflict", "main_flat"])
def test_selected_solver_actions_and_spoken_line_share_one_plan(monkeypatch, scenario):
    from plugin.plugins.sts2_autoplay import catgirl_llm
    monkeypatch.setattr(catgirl_llm, "create_chat_llm_async", Mock())
    monkeypatch.setattr(catgirl_llm, "get_config_manager", Mock())
    snapshot = scene()
    for card, card_id in zip(snapshot["raw_state"]["combat"]["hand"], ("STRIKE", "DEFEND", "STRIKE")):
        card["card_id"] = card_id
    if scenario == "draw_main":
        snapshot["agent_view"]["combat"]["hand"][1]["line"] = "防御：抽1张牌。"
    if scenario == "draw_continuation":
        snapshot["agent_view"]["combat"]["hand"][0]["line"] = "打击：抽1张牌。"
    adopts_solver = scenario in {"solver", "solver_natural", "solver_assumptions"}
    answer = {"plan_source": "solver" if adopts_solver else "custom", "plan_update": "replace",
        "action_type": "play_card", "kwargs": {"card_ref": "c2"}, "reply": "先防御，再打乙。",
        "speech": "先防御凑够14点，我先〔步骤〕。", "speech_scope": "steps", "speech_step_ids": [0],
        # 本用例验证发言与步骤绑定；guidance会按"有指令reply时取消计划speech"整条压掉发言。
        "message_effects": {"1": "chat"}, "continuation": [
            {"action_type": "play_card", "kwargs": {"card_ref": "c2" if scenario == "duplicate" else "c1", "target_ref": "e4"}},
            {"action_type": "end_turn"}]}
    if scenario == "custom_attributes":
        for enemy in snapshot["raw_state"]["combat"]["enemies"]:
            enemy["name"] = "鳗鱼"
        snapshot["raw_state"]["combat"]["enemies"][0].update(current_hp=31, intent="ENLARGE_MOVE", intents=[{"label": "强化", "intent_type": "Buff"}])
        snapshot["raw_state"]["combat"]["enemies"][1].update(current_hp=35, intent="BITE_MOVE", intents=[{"label": "攻击7", "intent_type": "Attack"}], powers=[{"name": "力量", "amount": 2}])
        answer.update(kwargs={"card_ref": "c1", "target_ref": "e5"}, reason="血最低且正在强化的e5", speech="全砸血最低且正在强化的鳗鱼。", continuation=[])
    if scenario == "legacy_continuation":
        answer["continuation"][0]["kwargs"] = {"card_ref": "c1", "target_index": 1}
    if scenario in {"flat", "flat_conflict"}:
        step = answer["continuation"][0]
        step.update(step.pop("kwargs"))
        if scenario == "flat_conflict":
            step["kwargs"] = {"card_ref": "c3", "target_ref": "e4"}
    if scenario == "main_flat":
        answer.update(answer.pop("kwargs"))
    if scenario == "solver_assumptions":
        answer["solver_assumptions"] = [[{"kind": "hp_order", "ref": "e4", "order": "lowest"}],
                                       [{"kind": "enemy_hp", "ref": "e4", "min": 1}]]
    if scenario == "solver_natural":
        answer.update(speech="两张打击送给甲，终于轮到我还手了。", speech_step_ids=[0, 1])
    requests = []
    class Client:
        async def ainvoke_raw(self, messages, **kwargs):
            requests.append(deepcopy(messages))
            assert "tools" not in kwargs
            return {"choices": [{"message": {"content": json.dumps(answer, ensure_ascii=False)}}]}
    solver = AsyncMock(return_value={"status": "ok", "solver_result": {"line": [{"turn": 1, "steps": [
        {"kind": "play_card", "card_id": "STRIKE", "card_index": 0, "target_index": 0},
        {"kind": "play_card", "card_id": "STRIKE", "card_index": 0, "target_index": 0}, {"kind": "end_turn"}]}]}})
    planner = STS2LLMPlanner(Mock(), persona_provider=lambda: "小铃")
    monkeypatch.setattr(planner, "_ensure_llm", AsyncMock(return_value=Client()))
    operation = asyncio.run(planner.plan({"conversation_owner": "same", "agent_view": snapshot["agent_view"],
        "action_refs": action_refs(snapshot), "new_player_messages": [{"id": "1", "content": "打甲"}]}, solver=solver))
    assert solver.await_count == 1 and planner.metrics["model_calls"] == 1
    assert operation.solver_used and operation.solver_adopted == adopts_solver
    if scenario == "custom_attributes":
        assert operation.speech == answer["speech"]
        assert "35血，当前攻击7，已有力量 2" in operation.reason
        assert "血最低" not in operation.reason and "当前强化" not in operation.reason
        assert operation.model_reason == "血最低且正在强化的e5"
        assert operation.bindings["target"]["ref"] == "e5"
        return
    if scenario == "legacy_continuation":
        assert operation.continuation[0]["bindings"]["target"]["ref"] == "e5"
    if scenario in {"flat", "main_flat"}:
        assert operation.bindings["card"]["ref"] == "c2"
        assert operation.continuation[0]["bindings"]["card"]["ref"] == "c1"
        assert operation.continuation[0]["bindings"]["target"]["ref"] == "e4"
        assert operation.continuation[1]["action_type"] == "end_turn"
    elif scenario == "flat_conflict":
        assert operation.continuation == []
        return
    if adopts_solver:
        assert operation.bindings["card"]["ref"] == "c1"
        assert operation.continuation[0]["bindings"]["card"]["ref"] == "c3"
        assert operation.speech == answer["speech"].replace("〔步骤〕", "打击打甲")
        assert operation.reply == answer["reply"]
        assert "14" not in operation.reason and operation.model_reason == ""
        saved = json.loads(planner._history[-1]["content"])
        assert saved["speech"] == operation.speech and saved["kwargs"]["card_ref"] == "c1"
        if scenario == "solver_assumptions":
            assert operation.assumptions == answer["solver_assumptions"][0]
            assert operation.continuation[0]["assumptions"] == answer["solver_assumptions"][1]
            snapshot["raw_state"]["combat"]["enemies"][1]["current_hp"] = 5
            assert resolve_action(operation.as_dict(), snapshot)[1]["code"] == "assumption_changed"
    elif scenario in {"duplicate", "draw_main"}:
        assert operation.continuation == []
    elif scenario == "draw_continuation":
        assert len(operation.continuation) == 1
    else:
        assert operation.bindings["card"]["ref"] == "c2"
        assert operation.continuation[-1]["action_type"] == "end_turn"


@pytest.mark.parametrize("speech,refs", [("先补格挡，再集中输出。", [0, 1]),
                                        ("先防御凑够14点。", [0]),
                                        ("这回合稳住血量，我也缓口气。", None)])
def test_intent_is_bound_to_retained_steps_without_rejecting_action(monkeypatch, speech, refs):
    from plugin.plugins.sts2_autoplay import catgirl_llm
    monkeypatch.setattr(catgirl_llm, "create_chat_llm_async", Mock())
    monkeypatch.setattr(catgirl_llm, "get_config_manager", Mock())
    snapshot = scene()
    planner = STS2LLMPlanner(Mock(), persona_provider=lambda: "小铃")
    answer = {"action_type": "play_card", "kwargs": {"card_ref": "c2"}, "speech": speech,
              "speech_scope": "intent", "continuation": [{"action_type": "play_card", "kwargs": {"card_ref": "missing"}}]}
    if refs is not None:
        answer["speech_step_ids"] = refs
    monkeypatch.setattr(planner, "_ensure_llm", AsyncMock(return_value=object()))
    monkeypatch.setattr(planner, "_invoke_text", AsyncMock(return_value=json.dumps(answer, ensure_ascii=False)))
    operation = asyncio.run(planner.plan({"agent_view": snapshot["agent_view"], "action_refs": action_refs(snapshot)}))
    assert operation.action_type == "play_card" and operation.continuation == []
    if "集中输出" in speech:
        assert operation.speech == ""
    else:
        assert operation.speech == speech
    if refs is None:
        assert operation.speech_step_ids == [0]
    assert planner.metrics["model_calls"] == 1


@pytest.mark.parametrize("kind", ["automatic", "guidance", "chat"])
def test_tactical_reply_is_bound_once_and_step_numbers_do_not_leak(monkeypatch, kind):
    from plugin.plugins.sts2_autoplay import catgirl_llm
    monkeypatch.setattr(catgirl_llm, "create_chat_llm_async", Mock())
    monkeypatch.setattr(catgirl_llm, "get_config_manager", Mock())
    snapshot = scene()
    context = {"agent_view": snapshot["agent_view"], "action_refs": action_refs(snapshot)}
    answer = {"action_type": "play_card", "kwargs": {"card_ref": "c1", "target_ref": "e4"},
              "reply": "先压它的血，争取尽快收场。", "speech": "先〔1〕打击打甲，争取尽快收场。"}
    if kind in {"guidance", "chat"}:
        context["new_player_messages"] = [{"id": "1", "content": "集火甲" if kind == "guidance" else "今天有点累"}]
        answer["message_effects"] = {"1": kind}
        context["speech_allowed"] = kind != "guidance"
    if kind == "chat":
        answer["reply"] = "主人，今天工作辛苦了。"
    elif kind == "reply_only_unbound":
        answer.update(speech="", reply="先用防御。")
    elif kind == "silent":
        context["speech_allowed"] = False
    planner = STS2LLMPlanner(Mock(), persona_provider=lambda: "小铃")
    monkeypatch.setattr(planner, "_ensure_llm", AsyncMock(return_value=object()))
    monkeypatch.setattr(planner, "_invoke_text", AsyncMock(return_value=json.dumps(answer, ensure_ascii=False)))
    operation = asyncio.run(planner.plan(context))
    assert operation.action_type == "play_card" and planner.metrics["model_calls"] == 1
    assert "〔1〕" not in operation.speech + operation.reply
    assert operation.reply == (answer["reply"] if kind in {"chat", "guidance"} else "")
    assert operation.speech == ("" if kind in {"silent", "guidance"} else "先用防御。" if kind == "reply_only_unbound" else "先打击打甲，争取尽快收场。")


@pytest.mark.parametrize("danger", [False, True])
def test_instruction_reply_covers_opening_and_preserves_end_turn_warning(monkeypatch, danger):
    snapshot = scene()
    context = {"agent_view": snapshot["agent_view"], "action_refs": action_refs(snapshot),
               "turn_speech_required": True, "speech_allowed": True,
               "new_player_messages": [{"id": "1", "content": "结束回合" if danger else "打甲"}]}
    answer = {"action_type": "end_turn" if danger else "play_card",
              "kwargs": {} if danger else {"card_ref": "c1", "target_ref": "e4"},
              "reply": "好，按你的安排。", "speech": "", "message_effects": {"1": "guidance"},
              "requirement_updates": [{"op": "add", "source_message_id": "1", "text": context["new_player_messages"][0]["content"],
                  "scope": "action", "behavior": {"kind": "intent"}}]}
    if danger:
        context["agent_view"]["combat"]["lethal_risks"] = [{"will_kill_player": True, "player_hp": 5, "player_block": 0, "incoming_damage": 8}]
        context["turn_end_check"] = {"playable_zero_cost_cards": [{"card_ref": "c1", "name": "打击"}]}
        answer["skip_zero_cost_cards"] = {"c1": "保留回合末格挡"}
    planner = STS2LLMPlanner(Mock(), persona_provider=lambda: "小铃")
    monkeypatch.setattr(planner, "_ensure_llm", AsyncMock(return_value=object()))
    invoke = AsyncMock(return_value=json.dumps(answer, ensure_ascii=False))
    monkeypatch.setattr(planner, "_invoke_text", invoke)
    operation = asyncio.run(planner.plan(context))
    assert not operation.speech and invoke.await_count == 1
    assert operation.reply.startswith(answer["reply"])
    if danger:
        assert "致死" in operation.reply
        # 逐张理由只进结构化字段，不再拼进回复（2026-10-17 21:30 播报概括）。
        assert "保留回合末格挡" not in operation.reply


@pytest.mark.parametrize("case", ["action", "no_action", "invalid_reply", "failure"])
def test_chat_requires_reply_before_consuming_input(monkeypatch, case):
    from plugin.plugins.sts2_autoplay.companion_session import CompanionSession
    snapshot = scene()
    session = CompanionSession()
    session.remember("user", "主人要死啦 只能打点输出了你加油")
    message = session.pending_messages()[0]
    answer = {"action_type": None if case == "no_action" else "play_card",
              "kwargs": {"card_ref": "c1", "target_ref": "e4"},
              "plan_update": "keep", "speech": "先过牌找输出。",
              "message_effects": {message["id"]: "chat"}}
    if case == "invalid_reply":
        answer["reply"] = "我先用{card:99}。"
    repaired = {**answer, "reply": "听到啦，主人，你先撑住，我尽量补足伤害。"}
    routed = Mock(side_effect=session.resolve_messages)
    replies = iter([answer, answer if case == "failure" else repaired])
    planner = STS2LLMPlanner(Mock(), persona_provider=lambda: "小铃")
    monkeypatch.setattr(planner, "_ensure_llm", AsyncMock(return_value=object()))
    async def invoke(*args):
        assert session.pending_messages() and not routed.called
        return json.dumps(next(replies), ensure_ascii=False)
    monkeypatch.setattr(planner, "_invoke_text", invoke)
    context = {"agent_view": snapshot["agent_view"], "action_refs": action_refs(snapshot),
               "new_player_messages": [message]}
    if case == "failure":
        with pytest.raises(RuntimeError, match="需要非空reply"):
            asyncio.run(planner.plan(context, route_messages=routed))
        assert session.pending_messages() and not routed.called
    else:
        operation = asyncio.run(planner.plan(context, route_messages=routed))
        assert operation.reply == repaired["reply"] and operation.plan_update == "keep"
        routed.assert_called_once_with({message["id"]: "chat"})
        assert not session.pending_messages()
    assert planner.metrics["model_calls"] == 2


@pytest.mark.parametrize("invalid", ["", "我先用{card:99}。"])
def test_guidance_requires_speech_before_consuming_input(monkeypatch, invalid):
    from plugin.plugins.sts2_autoplay.companion_session import CompanionSession
    snapshot = scene()
    session = CompanionSession()
    session.remember("user", "打右边的")
    message = session.pending_messages()[0]
    answer = {"action_type": "play_card", "kwargs": {"card_ref": "c1", "target_ref": "e5"},
              "speech": invalid, "message_effects": {message["id"]: "guidance"}}
    repaired = {**answer, "speech": "听到了，这次集火右边的乙。"}
    routed = Mock(side_effect=session.resolve_messages)
    replies = iter([answer, repaired])
    planner = STS2LLMPlanner(Mock(), persona_provider=lambda: "小铃")
    monkeypatch.setattr(planner, "_ensure_llm", AsyncMock(return_value=object()))
    async def invoke(*args):
        assert session.pending_messages() and not routed.called
        return json.dumps(next(replies), ensure_ascii=False)
    monkeypatch.setattr(planner, "_invoke_text", invoke)
    operation = asyncio.run(planner.plan({"agent_view": snapshot["agent_view"], "action_refs": action_refs(snapshot),
        "new_player_messages": [message], "speech_allowed": False}, route_messages=routed))
    assert operation.speech == repaired["speech"] and not operation.reply
    assert operation.kwargs["target_index"] == 1
    assert planner.metrics["model_calls"] == 2
    routed.assert_called_once_with({message["id"]: "guidance"})
    assert not session.pending_messages()


@pytest.mark.parametrize("kind", ["guidance", "zero_cost_skip", "ordinary"])
def test_same_turn_input_and_zero_cost_choice_are_spoken(monkeypatch, kind):
    from plugin.plugins.sts2_autoplay import service as service_module
    snapshot = scene()
    snapshot["action_registry"] = [{"type": "end_turn"}]
    service = STS2AutoplayService(Mock(), lambda _: None)
    service._state.snapshot = snapshot
    service._state.autoplay_state = "running"
    service._state.host_digest_enabled = True
    service._companion_session.observe(snapshot["agent_view"])
    service._communicated_turn = service._companion_session.anchor()
    service._companion_session._last_speech = {"topic": service._companion_session.speech_topic()}
    context = {"agent_view": snapshot["agent_view"], "speech_allowed": False}
    answer = {"action_type": "end_turn", "kwargs": {}}
    if kind == "guidance":
        context["new_player_messages"] = [{"id": "1", "content": "打右边的"}]
        answer.update(message_effects={"1": "guidance"}, speech="听到了，右边继续集火；这回合已经没有攻击牌，下一回合接着打它。")
    elif kind == "zero_cost_skip":
        context["turn_end_check"] = {"playable_zero_cost_cards": [{"card_ref": "c1", "name": "螺旋钻击"}]}
        answer["skip_zero_cost_cards"] = {"c1": "保留波纹水盆的回合末格挡"}
    else:
        context["speech_allowed"] = True
        answer["speech"] = "这回合先收手，留好格挡等下一轮。"
    planner = STS2LLMPlanner(Mock(), persona_provider=lambda: "小铃")
    monkeypatch.setattr(planner, "_ensure_llm", AsyncMock(return_value=object()))
    monkeypatch.setattr(planner, "_invoke_text", AsyncMock(return_value=json.dumps(answer, ensure_ascii=False)))
    operation = asyncio.run(planner.plan(context)).as_dict()
    operation.update(observation_guard=observation_guard(snapshot), session_generation=service._companion_session.generation,
        control_mode=service._state.control_mode, control_generation=service._state.control_generation,
        guidance_generation=service._state.guidance_generation, input_ids=["request-1"] if kind == "guidance" else [])
    monkeypatch.setattr(service, "refresh_state", AsyncMock())
    monkeypatch.setattr(service, "_read_companion_dialogue", AsyncMock())
    monkeypatch.setattr(service, "_push_catgirl", AsyncMock())
    dispatch = AsyncMock(return_value={"status": "ok", "executed": True})
    monkeypatch.setattr(service, "_execute_operation", dispatch)
    monkeypatch.setattr(service._catgirl_llm, "character_name", Mock(return_value="小铃"))
    speaker = AsyncMock(return_value={"speech_id": "test", "audio_completed": True})
    monkeypatch.setattr(service_module, "speak_current_turn", speaker)
    assert service._companion_session.speech_kind() == "none"
    result = asyncio.run(service.execute_operation(operation))
    assert result["executed"], result
    if kind == "zero_cost_skip":
        # 逐张理由不再拼进发言；speech_allowed=False 时本轮无话可说（2026-10-17 21:30 概括要求）。
        speaker.assert_not_awaited()
        assert not operation["speech"]
    else:
        speaker.assert_awaited_once()
        assert speaker.await_args.args[2] == operation["speech"]
    dispatch.assert_awaited_once()


def test_chat_reply_is_published_while_existing_plan_continues(monkeypatch):
    from plugin.plugins.sts2_autoplay.tests.test_companion_development import prepare
    snapshot = scene()
    snapshot["action_registry"] = [{"type": "play_card"}]
    service = prepare(monkeypatch)
    service._state.snapshot = snapshot
    service._ingress_active = True
    service._companion_session.observe(snapshot["agent_view"])
    kwargs, bindings, assumptions = bind_action("play_card", {"card_ref": "c2"}, action_refs(snapshot))
    service._companion_session.accept_plan({"action_type": "play_card", "kwargs": kwargs,
                                            "bindings": bindings, "assumptions": assumptions}, snapshot["agent_view"])
    revision = service._companion_session.plan_revision
    service._selected_generation = (service._companion_session.generation, service._state.control_generation,
                                    service._state.guidance_generation)
    service._last_dialogue_generation = service._companion_session.generation
    service._companion_session.remember("user", "主人要死啦 只能打点输出了你加油")
    message = service._companion_session.pending_messages()[0]
    reply = "听到啦，主人，你先撑住，我来争取收掉它。"
    answer = {"action_type": "play_card", "kwargs": {"card_ref": "c1", "target_ref": "e4"},
              "plan_update": "keep", "reply": reply, "speech": "先打击。", "message_effects": {message["id"]: "chat"}}
    planner = STS2LLMPlanner(Mock(), persona_provider=lambda: "小铃")
    monkeypatch.setattr(planner, "_ensure_llm", AsyncMock(return_value=object()))
    monkeypatch.setattr(planner, "_invoke_text", AsyncMock(return_value=json.dumps(answer, ensure_ascii=False)))
    monkeypatch.setattr(service, "_sync_companion_ingress", AsyncMock())
    monkeypatch.setattr(service, "_read_companion_dialogue", AsyncMock())
    publish = AsyncMock(return_value={"text_sent": True, "audio_completed": None})
    monkeypatch.setattr(service, "_publish_companion_reply", publish)
    async def plan(context, **kwargs):
        return await planner.plan(context, validate=kwargs["validate"], route_messages=kwargs["route_messages"])
    monkeypatch.setattr(service._llm_planner, "plan", plan)
    result = asyncio.run(service.run_autoplay_step())
    assert result["executed"]
    publish.assert_awaited_once()
    assert publish.await_args.args[0] == reply
    dispatched = service.execute_operation.await_args.args[0]
    assert dispatched["bindings"]["card"]["ref"] == "c2" and dispatched["plan_revision"] == revision
    assert not service._companion_session.pending_messages()


@pytest.mark.parametrize("finish_reason", ["length", None])
def test_reasoning_budget_exhaustion_is_logged_without_identical_retry(monkeypatch, finish_reason):
    from types import SimpleNamespace
    from plugin.plugins.sts2_autoplay import catgirl_llm
    monkeypatch.setattr(catgirl_llm, "create_chat_llm_async", Mock())
    monkeypatch.setattr(catgirl_llm, "get_config_manager", Mock())
    calls = []
    class Client:
        async def ainvoke_raw(self, messages, **kwargs):
            raise AssertionError("使用流式接口")
        async def astream(self, messages, **kwargs):
            calls.append(kwargs)
            yield SimpleNamespace(content="", reasoning_content="思考中", tool_call_deltas=None,
                                  usage_metadata=None, finish_reason=finish_reason)
            yield SimpleNamespace(content="", tool_call_deltas=None, usage_metadata={
                "prompt_tokens": 2000, "completion_tokens": 8192,
                "completion_tokens_details": {"reasoning_tokens": 8192}})
    logger = Mock()
    planner = STS2LLMPlanner(logger, persona_provider=lambda: "猫娘")
    monkeypatch.setattr(planner, "_ensure_llm", AsyncMock(return_value=Client()))
    with pytest.raises(RuntimeError, match="输出预算耗尽"):
        asyncio.run(planner.plan({"agent_view": {"screen": "COMBAT", "turn": 1, "actions": ["end_turn"]}}))
    # 预算耗尽不再原样重试：改用"收窄提问 + 去掉历史现场"的一次降级重试，第二次仍是 low。
    assert [c["reasoning_effort"] for c in calls] == ["low", "low"] and planner._max_tokens == 8192
    assert planner._max_tokens == 8192 and not planner._history
    invalid = [c.args for c in logger.info.call_args_list if c.args[0].startswith("[sts2_llm_invalid]")]
    assert invalid[0][-2:] == ("budget_exhausted", finish_reason)
    assert any(c.args[0].startswith("[sts2_generation]") for c in logger.info.call_args_list)


def test_streaming_preserves_cache_usage_and_fixed_effective_profiles(monkeypatch):
    import httpx
    from types import SimpleNamespace
    from plugin.plugins.sts2_autoplay import catgirl_llm
    monkeypatch.setattr(catgirl_llm, "create_chat_llm_async", Mock())
    config = Mock()
    card = {"种族": "鲸鱼娘", "对主人的称呼": "主人", "说话风格": "认真又亲近", "_reserved": {"voice_id": "private"}}
    config.get_character_data.return_value = ("猫寻铃", "小铃", {"档案名": "猫寻铃", "昵称": "主人"}, {"小铃": card})
    monkeypatch.setattr(catgirl_llm, "get_config_manager", Mock(return_value=config))
    service = STS2AutoplayService(Mock(), lambda _: None)
    service._host_persona = "小铃"
    logger = Mock()
    planner = STS2LLMPlanner(logger, persona_provider=service._companion_persona)
    requests = []
    usage = {"prompt_tokens": 2000, "prompt_cache_hit_tokens": 1800, "prompt_tokens_details": {"cached_tokens": 0}}
    answer = json.dumps({"action_type": "end_turn", "kwargs": {}, "speech": "这回合先收手，等新手牌。"}, ensure_ascii=False)
    def handle(request):
        requests.append(json.loads(request.content))
        return httpx.Response(200, headers={"x-request-id": "stream-gateway"}, json={})
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http_client:
            class Client:
                _aclient = SimpleNamespace(_client=http_client)
                async def ainvoke_raw(self, messages, **kwargs):
                    raise AssertionError("应调用流式接口")
                async def astream(self, messages, **kwargs):
                    await http_client.post("https://gateway.example/v1/chat/completions", headers=kwargs.pop("extra_headers"),
                        json={"messages": messages, "model": "flash", "stream": True,
                              "stream_options": {"include_usage": True}, **kwargs})
                    for text in ("", answer[:25], answer[25:]):
                        yield SimpleNamespace(content=text, tool_call_deltas=None, usage_metadata=None)
                    yield SimpleNamespace(content="", tool_call_deltas=None, usage_metadata=usage)
            monkeypatch.setattr(planner, "_ensure_llm", AsyncMock(return_value=Client()))
            context = {"conversation_owner": "one", "agent_view": {"screen": "COMBAT", "turn": 1, "actions": ["end_turn"]}}
            first = await planner.plan(context)
            config.get_character_data.return_value = ("猫寻铃", "小铃", {"昵称": "主人", "档案名": "猫寻铃"}, {"小铃": dict(reversed(list(card.items())))})
            second = await planner.plan(context)
            assert first.speech == second.speech == "这回合先收手，等新手牌。"
    asyncio.run(run())
    assert all(r["stream"] and r["stream_options"]["include_usage"] and r["reasoning_effort"] == "low" for r in requests)
    # 同一会话 system 固定；上一轮的完整现场以简版进入历史，之后不再改写。
    assert requests[1]["messages"][0] == requests[0]["messages"][0]
    assert json.loads(requests[1]["messages"][1]["content"])["compact"] == 1
    system = requests[0]["messages"][0]["content"]
    assert "猫寻铃" in system and "对主人的称呼" in system and "鲸鱼娘" in system
    assert "_reserved" not in system and "voice_id" not in system
    assert planner.metrics["cached_tokens"] == 3600
    wire = [json.loads(c.args[1]) for c in logger.info.call_args_list if c.args[0] == "[sts2_http_request] %s"]
    assert wire[0]["parameters_hash"] == wire[1]["parameters_hash"]
    assert sum(c.args[0].startswith("[sts2_stream_complete]") for c in logger.info.call_args_list) == 2


def test_continuation_rechecks_surviving_enemy_intents_and_exposes_zero_incoming():
    from plugin.plugins.sts2_autoplay.action_identity import enemy_intents
    from plugin.plugins.sts2_autoplay.agent_facts import build_agent_facts
    snapshot = scene()
    enemies = snapshot["raw_state"]["combat"]["enemies"]
    for enemy in enemies:
        enemy.update(intent="ATTACK_MOVE", intents=[{"intent_type": "Attack", "total_damage": 8}])
    kwargs, bindings, conditions = bind_action("play_card", {"card_ref": "c2"}, action_refs(snapshot))
    step = {"action_type": "play_card", "kwargs": kwargs, "bindings": bindings,
            "assumptions": conditions, "enemy_intents": enemy_intents(snapshot)}
    service = STS2AutoplayService(Mock(), lambda _: None)
    operation = {**step, "continuation": [deepcopy(step)]}
    service._companion_session.observe(snapshot["agent_view"])
    service._companion_session.accept_plan(operation, snapshot["agent_view"])
    saved = service._selected_steps[0]
    after = deepcopy(snapshot)
    after["raw_state"]["combat"]["enemies"].pop(0)
    survivor = after["raw_state"]["combat"]["enemies"][0]
    survivor.update(index=0, current_hp=10)
    assert resolve_action(saved, after)[1] is None
    survivor.update(intent="VULNERABLE_SPORES_MOVE", intents=[{"intent_type": "Debuff", "total_damage": None}])
    error = resolve_action(saved, after)[1]
    assert error["code"] == "enemy_intent_changed" and error["entity_id"] == survivor["entity_id"]
    assert error["before"]["move_id"] == "ATTACK_MOVE"
    facts = build_agent_facts(after["raw_state"])
    assert facts["incoming_damage"] == 0 and facts["enemies"][0]["attacking"] is False
    assert facts["enemies"][0]["intent_damage"] == 0
    survivor["intents"] = [{"intent_type": "Unknown"}]
    facts = build_agent_facts(after["raw_state"])
    assert "incoming_damage" not in facts and "attacking" not in facts["enemies"][0]


def test_observation_guard_ignores_pile_order_and_intent_recalculation():
    """牌堆顺序与意图伤害重算不算新事实；招式本身变化仍然算（Q03）。"""
    snapshot = {"agent_view": {"screen": "COMBAT", "run_id": "r", "turn": 1, "actions": ["play_card"],
                               "combat": {"player": {"energy": 3}, "hand": [{"i": 0, "line": "打击", "playable": True}],
                                          "draw": [{"i": 0, "line": "防御"}, {"i": 1, "line": "打击"}]}},
                "raw_state": {"combat": {"enemies": [{"entity_id": "e1", "is_alive": True,
                    "intents": [{"intent_type": "Attack", "damage": 18, "hits": 1}]}]}}}
    first = observation_guard(snapshot)
    reordered = deepcopy(snapshot)
    reordered["agent_view"]["combat"]["draw"].reverse()
    reordered["raw_state"]["combat"]["enemies"][0]["intents"][0]["damage"] = 13
    assert observation_guard(reordered) == first
    changed = deepcopy(snapshot)
    changed["raw_state"]["combat"]["enemies"][0]["intents"][0]["intent_type"] = "Defend"
    assert observation_guard(changed) != first


def test_end_turn_requires_reasons_for_affordable_cards(monkeypatch):
    """结束回合前还有付得起的牌时必须逐张说明取舍，不再只看0费牌（Q06）。"""
    snapshot = scene()
    context = {"agent_view": snapshot["agent_view"], "action_refs": action_refs(snapshot),
               "turn_end_check": {"energy": 1, "playable_cards": [
                   {"card_ref": "c2", "name": "充电+", "line": "[1费]获得7点格挡。在下个回合获得能量。", "card_type": "Skill",
                    "energy_cost": 1}]}}
    planner = STS2LLMPlanner(Mock(), persona_provider=lambda: "小铃")
    monkeypatch.setattr(planner, "_ensure_llm", AsyncMock(return_value=object()))
    end = {"action_type": "end_turn", "kwargs": {}}
    fixed = {**end, "skip_playable_cards": {"c2": "零来伤，7点格挡用不上"}}
    invoke = AsyncMock(side_effect=[json.dumps(end), json.dumps(fixed)])
    monkeypatch.setattr(planner, "_invoke_text", invoke)
    operation = asyncio.run(planner.plan(context))
    assert invoke.await_count == 2 and operation.action_type == "end_turn"
    assert operation.as_dict()["skip_playable_cards"] == {"c2": "零来伤，7点格挡用不上"}
    # 理由只用于核对，不再由插件逐张拼进发言（2026-10-17 21:30 现场：主人要求概括即可）。
    assert "零来伤，7点格挡用不上" not in (operation.speech or "")


def test_ask_field_is_delivered_with_turn_speech(monkeypatch):
    """模型用独立ask字段提问时随本轮发言送出（只靠提示词时自主决策长期0次提问）。"""
    snapshot = scene()
    context = {"agent_view": snapshot["agent_view"], "action_refs": action_refs(snapshot)}
    planner = STS2LLMPlanner(Mock(), persona_provider=lambda: "小铃")
    monkeypatch.setattr(planner, "_ensure_llm", AsyncMock(return_value=object()))
    answer = {"plan_source": "custom", "action_type": "play_card",
              "kwargs": {"card_ref": "c1", "target_ref": "e4"},
              "speech": "先打这张。", "ask": "要不要把药水留到Boss"}
    monkeypatch.setattr(planner, "_invoke_text", AsyncMock(return_value=json.dumps(answer, ensure_ascii=False)))
    operation = asyncio.run(planner.plan(context))
    assert operation.action_type == "play_card"
    assert operation.speech.startswith("先打这张。")
    assert "要不要把药水留到Boss？" in operation.speech
    # asked 必须进 as_dict：服务层要靠它决定询问窗口（说完等回答再派发）。
    assert operation.as_dict()["asked"] is True


def test_banter_appends_to_speech_and_respects_cooldown(monkeypatch):
    """情绪吐槽由模型自判、接在发言后面；banter_allowed=false 时丢弃（服务层冷却）。"""
    snapshot = scene()
    answer = {"plan_source": "custom", "action_type": "play_card",
              "kwargs": {"card_ref": "c1", "target_ref": "e4"},
              "speech": "先打这张。", "banter": "这牌堆真会挑时候恶心人"}
    planner = STS2LLMPlanner(Mock(), persona_provider=lambda: "小铃")
    monkeypatch.setattr(planner, "_ensure_llm", AsyncMock(return_value=object()))
    monkeypatch.setattr(planner, "_invoke_text", AsyncMock(return_value=json.dumps(answer, ensure_ascii=False)))
    context = {"agent_view": snapshot["agent_view"], "action_refs": action_refs(snapshot), "banter_allowed": True}
    operation = asyncio.run(planner.plan(context))
    assert "这牌堆真会挑时候恶心人" in operation.speech and operation.banter_used
    cooldown = STS2LLMPlanner(Mock(), persona_provider=lambda: "小铃")
    monkeypatch.setattr(cooldown, "_ensure_llm", AsyncMock(return_value=object()))
    monkeypatch.setattr(cooldown, "_invoke_text", AsyncMock(return_value=json.dumps(answer, ensure_ascii=False)))
    quiet = asyncio.run(cooldown.plan({**context, "banter_allowed": False}))
    assert "这牌堆真会挑时候恶心人" not in quiet.speech and not quiet.banter_used


def test_media_request_allowed_when_speech_explains_it(monkeypatch):
    """自主行动没有reply时，媒体请求只要speech里有说明就应放行（此前被reply必填挡住）。"""
    snapshot = scene()
    answer = {"plan_source": "custom", "action_type": "play_card",
              "kwargs": {"card_ref": "c1", "target_ref": "e4"},
              "speech": "这波先稳一下，给你放首《Silver Scrapes》。",
              "media_requests": [{"kind": "music", "query": "Silver Scrapes"}]}
    planner = STS2LLMPlanner(Mock(), persona_provider=lambda: "小铃")
    monkeypatch.setattr(planner, "_ensure_llm", AsyncMock(return_value=object()))
    monkeypatch.setattr(planner, "_invoke_text", AsyncMock(return_value=json.dumps(answer, ensure_ascii=False)))
    operation = asyncio.run(planner.plan({"agent_view": snapshot["agent_view"], "action_refs": action_refs(snapshot)}))
    assert operation.media_requests and operation.media_requests[0]["kind"] == "music"


def test_end_turn_skips_obvious_zero_value_cards_without_explanation(monkeypatch):
    """零来伤时的纯防御牌是明显零收益：不要求理由、也不写进发言；有来伤时必须解释（Q16）。"""
    snapshot = scene()
    defense = {"card_ref": "c2", "name": "防御", "line": "防御 [1费]：获得5点格挡。",
               "card_type": "Skill", "energy_cost": 1}
    context = {"agent_view": snapshot["agent_view"], "action_refs": action_refs(snapshot),
               "turn_end_check": {"energy": 1, "playable_cards": [defense]},
               "agent_facts": {"incoming_damage": 0}}
    planner = STS2LLMPlanner(Mock(), persona_provider=lambda: "小铃")
    monkeypatch.setattr(planner, "_ensure_llm", AsyncMock(return_value=object()))
    end = {"action_type": "end_turn", "kwargs": {}, "reason": "零来伤，防御不跨回合"}
    invoke = AsyncMock(return_value=json.dumps(end, ensure_ascii=False))
    monkeypatch.setattr(planner, "_invoke_text", invoke)
    operation = asyncio.run(planner.plan(context))
    assert invoke.await_count == 1 and operation.action_type == "end_turn"
    assert "先不打" not in (operation.speech or "")
    context["agent_facts"] = {"incoming_damage": 5}
    retry = AsyncMock(side_effect=[json.dumps(end, ensure_ascii=False),
                                   json.dumps({**end, "skip_playable_cards": {"c2": "5点来伤，1费格挡能挡"}}, ensure_ascii=False)])
    monkeypatch.setattr(planner, "_invoke_text", retry)
    operation = asyncio.run(planner.plan(context))
    assert retry.await_count == 2 and operation.skip_playable_cards["c2"]


@pytest.mark.parametrize("choice", ["solver_repair", "solver_repair_skip"])
def test_end_turn_checks_playable_zero_cost_cards_and_records_choices(monkeypatch, choice):
    snapshot = scene()
    context = {"agent_view": snapshot["agent_view"], "action_refs": action_refs(snapshot),
               "turn_end_check": {"energy": 0, "playable_zero_cost_cards": [{"card_ref": "c1", "name": "螺旋钻击"}]}}
    repair = choice in {"repair", "solver_repair", "solver_repair_skip"}
    end = {"action_type": "end_turn", "kwargs": {}, "plan_source": "solver" if choice.startswith("solver") else "custom"}
    if not repair:
        end["skip_zero_cost_cards"] = {"c1": "保留波纹水盆的回合末格挡"}
    attack = {"plan_source": "custom", "action_type": "play_card", "kwargs": {"card_ref": "c1", "target_ref": "e4"}}
    fixed = {**end, "skip_zero_cost_cards": {"c1": "保留波纹水盆的回合末格挡"}} if choice == "solver_repair_skip" else attack
    planner = STS2LLMPlanner(Mock(), persona_provider=lambda: "小铃")
    monkeypatch.setattr(planner, "_ensure_llm", AsyncMock(return_value=object()))
    invoke = AsyncMock(side_effect=[json.dumps(a, ensure_ascii=False) for a in ([end, fixed] if repair else [end])])
    monkeypatch.setattr(planner, "_invoke_text", invoke)
    solver = AsyncMock(return_value={"status": "ok", "solver_result": {"line": [{"turn": 1, "steps": [{"kind": "end_turn"}]}]}})
    operation = asyncio.run(planner.plan(context, solver=solver if choice.startswith("solver") else None))
    assert operation.action_type == ("play_card" if repair and choice != "solver_repair_skip" else "end_turn")
    assert operation.solver_adopted == (choice in {"solver_skip", "solver_repair_skip"})
    assert invoke.await_count == (2 if repair else 1)
    if repair:
        feedback = invoke.await_args_list[1].args[1][-1]["content"]
        assert 'plan_source="custom"' in feedback and '"skip_zero_cost_cards":{"c1":' in feedback
    if operation.action_type == "end_turn":
        assert any(c.args[0].startswith("[sts2_end_turn_choice]") for c in planner._logger.info.call_args_list)
        assert "保留波纹水盆的回合末格挡" not in (operation.speech or "")
        assert operation.as_dict()["skip_zero_cost_cards"] == {"c1": "保留波纹水盆的回合末格挡"}


@pytest.mark.parametrize("playable,cost,review", [(True, 0, True), (False, 0, False), (True, 1, False)])
def test_end_turn_continuation_reviews_zero_cost_without_looping(monkeypatch, playable, cost, review):
    from plugin.plugins.sts2_autoplay.tests.test_companion_development import prepare
    from plugin.plugins.sts2_autoplay.planner_interface import PlannedOperation
    snapshot = scene()
    snapshot["agent_view"]["combat"]["player"]["energy"] = 0
    card = snapshot["raw_state"]["combat"]["hand"][0]
    card.update(energy_cost=cost, playable=playable, card_type="Power")
    card.pop("index")  # 原始无index时与实体引用使用相同的枚举下标。
    snapshot["action_registry"] = [{"type": "play_card"}, {"type": "end_turn"}]
    service = prepare(monkeypatch)
    service._state.snapshot = snapshot
    service._companion_session.observe(snapshot["agent_view"])
    operation = {"action_type": "end_turn", "kwargs": {}}
    service._communicated_turn = service._companion_session.anchor()
    service._state.host_digest_enabled = True
    service._companion_session.accept_plan(operation, snapshot["agent_view"])
    service._selected_generation = (service._companion_session.generation, service._state.control_generation, service._state.guidance_generation)
    service._last_dialogue_generation = service._companion_session.generation
    planner = AsyncMock(return_value=PlannedOperation("end_turn", {}, 1, "llm", "保留遗物格挡"))
    monkeypatch.setattr(service._llm_planner, "plan", planner)
    assert asyncio.run(service.run_autoplay_step())["executed"]
    assert planner.await_count == int(review)
    if review:
        check = planner.await_args.args[0]["turn_end_check"]
        assert planner.await_args.args[0]["speech_allowed"] is True
        assert check["energy"] == 0 and check["playable_zero_cost_cards"][0]["card_ref"] == "c1"
        assert check["playable_zero_cost_cards"][0]["card_type"] == "Power"
    asyncio.run(service.run_autoplay_step())
    assert planner.await_count == int(review)


@pytest.mark.parametrize("space,target,mode", [("players", 0, "fallback"), ("players", 1, "fallback"),
    ("enemies", 0, "fallback"), (None, None, "fallback"), ("players", 0, "model"), ("players", 0, "announced")])
def test_potion_speech_bypasses_turn_throttle_and_uses_actual_target(monkeypatch, space, target, mode):
    from plugin.plugins.sts2_autoplay import service as service_module
    service = STS2AutoplayService(Mock(), lambda _: None)
    snapshot = scene()
    snapshot["raw_state"]["combat"]["players"] = [{"is_local": False, "player_id": "human"}, {"is_local": True, "player_id": "1"}]
    snapshot["raw_state"]["run"] = {"potions": [{"index": 0, "name": "格挡药水", "occupied": True,
        "can_use": True, "target_index_space": space, "valid_target_indices": [] if target is None else [target]}]}
    snapshot["agent_view"]["run"] = {"potions": [{"i": 0, "line": "格挡药水", "usable": True, "targets": [] if target is None else [target]}]}
    snapshot["action_registry"] = [{"type": "use_potion"}]
    service._state.snapshot = snapshot
    service._state.autoplay_state = "running"
    service._state.host_digest_enabled = True
    service._companion_session.observe(snapshot["agent_view"])
    service._communicated_turn = service._companion_session.anchor()
    service._companion_session._last_speech = {"topic": service._companion_session.speech_topic()}
    monkeypatch.setattr(service, "refresh_state", AsyncMock())
    monkeypatch.setattr(service, "_read_companion_dialogue", AsyncMock())
    monkeypatch.setattr(service, "_push_catgirl", AsyncMock())
    dispatch = AsyncMock(return_value={"status": "ok", "executed": True})
    monkeypatch.setattr(service, "_execute_operation", dispatch)
    monkeypatch.setattr(service._catgirl_llm, "character_name", Mock(return_value="小铃"))
    speaker = AsyncMock(return_value={"speech_id": "p", "audio_completed": True})
    monkeypatch.setattr(service_module, "speak_current_turn", speaker)
    kwargs = {"option_index": 0, **({"target_index": target} if target is not None else {})}
    operation = {"action_type": "use_potion", "kwargs": kwargs, "source": "llm"}
    if mode == "model":
        planner = STS2LLMPlanner(Mock(), persona_provider=lambda: "小铃")
        monkeypatch.setattr(planner, "_ensure_llm", AsyncMock(return_value=object()))
        monkeypatch.setattr(planner, "_invoke_text", AsyncMock(return_value=json.dumps({**operation, "speech": "这瓶格挡药水给你用，先撑住。"}, ensure_ascii=False)))
        snapshot["agent_view"]["actions"] = ["use_potion"]
        operation = asyncio.run(planner.plan({"agent_view": snapshot["agent_view"], "speech_allowed": False,
                                             "potion_speech_allowed": True})).as_dict()
        assert operation["speech"] == "这瓶格挡药水给你用，先撑住。"
    operation.update(observation_guard=observation_guard(snapshot), session_generation=service._companion_session.generation,
                     control_mode=service._state.control_mode, control_generation=service._state.control_generation,
                     guidance_generation=service._state.guidance_generation)
    if mode == "announced":
        service._companion_session.accept_plan(operation, snapshot["agent_view"])
        service._companion_session.announce_plan(operation, "这瓶格挡药水给你用。", "potion")
    assert asyncio.run(service.execute_operation(operation))["executed"]
    assert speaker.await_count == int(mode != "announced") and dispatch.await_count == 1
    if mode != "announced":
        text = speaker.await_args.args[2]
        assert "格挡药水" in text
        assert ("给你" in text) == (space == "players" and target == 0)
        assert ("甲身上" in text) == (space == "enemies")


@pytest.fixture
def enemy_potion_context():
    snapshot = scene()
    snapshot["agent_view"]["actions"] += ["use_potion"]
    snapshot["raw_state"]["run"] = {"potions": [{"index": 0, "potion_id": "WEAK_POTION",
        "name": "虚弱药水", "occupied": True, "target_index_space": "enemies"}]}
    return {"agent_view": snapshot["agent_view"], "action_refs": action_refs(snapshot), "agent_facts": {
        "potions": [{"option_index": 0, "usable": True, "effect": "给予3层虚弱"}],
        "enemies": [{"ref": "e4", "attacking": True, "intent_damage": 10},
                    {"ref": "e5", "attacking": False, "intent_damage": 0}]}}


@pytest.mark.parametrize("source", ["solver", "custom"])
@pytest.mark.parametrize("error", ["missing", "wrong_ref", "not_attacking"])
def test_potion_current_damage_check_retries_without_silently_changing_target(monkeypatch, enemy_potion_context, source, error):
    planner = STS2LLMPlanner(Mock(), persona_provider=lambda: "小铃")
    monkeypatch.setattr(planner, "_ensure_llm", AsyncMock(return_value=object()))
    check = {"target_ref": "e4" if error == "wrong_ref" else "e5",
             "purpose": "reduce_incoming", "reason": "降低本轮10点来伤"}
    bad = {"plan_source": source, "action_type": "use_potion", "kwargs": {"option_index": 0, "target_ref": "e5"},
           "potion_checks": {} if error == "missing" else {"0": check}}
    speech = "给正在攻击的甲挂虚弱，减少本轮伤害。"
    good = {**bad, "plan_source": "custom", "kwargs": {"option_index": 0, "target_ref": "e4"},
            "potion_checks": {"0": {**check, "target_ref": "e4"}}, "speech": speech}
    invoke = AsyncMock(side_effect=[json.dumps(bad, ensure_ascii=False), json.dumps(good, ensure_ascii=False)])
    monkeypatch.setattr(planner, "_invoke_text", invoke)
    solver = AsyncMock(return_value={"status": "ok", "solver_result": {"line": [{"turn": 1, "steps": [
        {"kind": "use_potion", "card_id": "WEAK_POTION", "target_index": 1}]}]}})
    operation = asyncio.run(planner.plan(enemy_potion_context, solver=solver if source == "solver" else None))
    assert invoke.await_count == 2
    feedback = invoke.await_args_list[1].args[1][-1]["content"]
    assert ("不是有来伤" if error == "not_attacking" else "potion_checks") in feedback
    assert operation.bindings["target"]["ref"] == "e4" and operation.kwargs["target_index"] == 0
    assert operation.speech == speech and not operation.solver_adopted
    assert solver.await_count == int(source == "solver")


@pytest.mark.parametrize("source", ["solver", "custom"])
def test_potion_setup_allows_nonattacking_target(monkeypatch, enemy_potion_context, source):
    planner = STS2LLMPlanner(Mock(), persona_provider=lambda: "小铃")
    monkeypatch.setattr(planner, "_ensure_llm", AsyncMock(return_value=object()))
    answer = {"plan_source": source, "action_type": "use_potion", "kwargs": {"option_index": 0, "target_ref": "e5"},
        "potion_checks": {"0": {"target_ref": "e5", "purpose": "setup", "reason": "提前压制下回合攻击，不减少本轮来伤"}}}
    invoke = AsyncMock(return_value=json.dumps(answer, ensure_ascii=False))
    monkeypatch.setattr(planner, "_invoke_text", invoke)
    solver = AsyncMock(return_value={"status": "ok", "solver_result": {"line": [{"turn": 1, "steps": [
        {"kind": "use_potion", "card_id": "WEAK_POTION", "target_index": 1}]}]}})
    operation = asyncio.run(planner.plan(enemy_potion_context, solver=solver if source == "solver" else None))
    assert invoke.await_count == 1 and operation.bindings["target"]["ref"] == "e5"
    assert operation.solver_adopted == (source == "solver")


def test_unchecked_potion_continuation_keeps_main_action_and_drops_tail(monkeypatch, enemy_potion_context):
    planner = STS2LLMPlanner(Mock(), persona_provider=lambda: "小铃")
    monkeypatch.setattr(planner, "_ensure_llm", AsyncMock(return_value=object()))
    answer = {"action_type": "play_card", "kwargs": {"card_ref": "c2"}, "continuation": [
        {"action_type": "use_potion", "kwargs": {"option_index": 0, "target_ref": "e5"}},
        {"action_type": "end_turn", "kwargs": {}}]}
    invoke = AsyncMock(return_value=json.dumps(answer, ensure_ascii=False))
    monkeypatch.setattr(planner, "_invoke_text", invoke)
    operation = asyncio.run(planner.plan(enemy_potion_context))
    assert operation.bindings["card"]["ref"] == "c2" and operation.continuation == []
    assert invoke.await_count == 1


@pytest.mark.parametrize("case", ["self", "enemy", "missing", "ambiguous", "draw", "unavailable"])
def test_solver_potion_prefix_binds_effect_owner_and_preserves_remaining_cards(case):
    snapshot = scene()
    potion = {"ref": "potion-0", "entity_id": "0:STRENGTH_POTION", "index": 0,
              "name": "力量药水", "potion_id": "STRENGTH_POTION", "target_space": "enemies" if case == "enemy" else "players"}
    refs = action_refs(snapshot)
    refs["potions"] = [potion] if case != "missing" else []
    if case == "ambiguous":
        refs["potions"].append({**potion, "ref": "potion-1", "entity_id": "1:STRENGTH_POTION", "index": 1})
    for card in refs["cards"]:
        card["card_id"] = "STRIKE" if card["name"] == "打击" else "DEFEND"
    line = [{"kind": "use_potion", "card_id": "STRENGTH_POTION", "target_index": 0 if case == "enemy" else None},
            {"kind": "play_card", "card_id": "STRIKE", "card_index": 0, "target_index": 0}, {"kind": "end_turn"}]
    context = {"agent_view": snapshot["agent_view"], "action_refs": refs, "agent_facts": {"potions": [
        {"option_index": 0, "usable": case != "unavailable", "effect": "抽2张牌" if case == "draw" else "获得力量",
         "targets": [{"role": "master", "target_index": 0}, {"role": "self", "target_index": 1}]}]}}
    planner = STS2LLMPlanner(Mock(), persona_provider=lambda: "小铃")
    result = {"status": "ok", "solver_refs": {kind: [{k: v for k, v in item.items() if k != "entity_id"} for item in items]
        for kind, items in refs.items()}, "solver_result": {"line": [{"turn": 1, "steps": line}]}}
    steps = planner._solver_steps(result, context)
    if case in {"missing", "ambiguous", "unavailable"}:
        assert steps == []
        return
    assert steps[0]["kwargs"] == ({"option_index": 0, "target_ref": "e4"} if case == "enemy" else {"option_index": 0, "target_index": 1})
    assert len(steps) == (1 if case == "draw" else 3)
    if case != "draw":
        assert steps[1]["kwargs"] == {"card_ref": "c1", "target_ref": "e4"}
        assert steps[2]["action_type"] == "end_turn"


@pytest.mark.parametrize("fail_audio", [False, True])
def test_user_reply_waits_for_audio_and_retries_without_repeating_text(monkeypatch, fail_audio):
    from plugin.plugins.sts2_autoplay.tests.test_companion_development import prepare
    service = prepare(monkeypatch)
    service._ingress_active = True
    service._companion_session.remember("user", "以后按这个打法")
    message = service._companion_session.pending_messages()[0]
    message["request_id"] = "reply-with-voice"
    service._companion_session.resolve_messages({message["id"]: "guidance"})
    record = service._companion_session.save_input_result([message], "明白了，已保存角色打法偏好。", media=[], kind="tactical")
    calls = []
    async def host(path, payload):
        calls.append(deepcopy(payload))
        assert payload["wait_audio"] is True
        return {"ok": not (fail_audio and len(calls) == 1), "text_sent": True,
                "audio_completed": not (fail_audio and len(calls) == 1), "audio_queued": True,
                "reason": "audio_completion_timeout" if fail_audio and len(calls) == 1 else ""}
    monkeypatch.setattr(service, "_companion_host_request", host)
    assert asyncio.run(service._flush_companion_replies())
    assert record["text_sent"]
    if fail_audio:
        assert record["audio_retry"] and record["audio_attempts"] == 1
        assert asyncio.run(service._flush_companion_replies())
        assert len(calls) == 1
        record["retry_at"] = 0
        assert asyncio.run(service._flush_companion_replies())
        assert calls[0]["output_id"] == calls[1]["output_id"]
    assert record["audio_completed"] is True and not record["audio_retry"]
    assert sum(m["role"] == "assistant" for m in service._companion_session.messages) == 1
    assert asyncio.run(service._flush_companion_replies())
    assert len(calls) == (2 if fail_audio else 1)


def test_continuation_ignores_recalculated_intent_damage():
    """同一招式只改伤害（虚弱/力量重算）不算新意图，不再取消计划（Q03）。"""
    from plugin.plugins.sts2_autoplay.action_identity import enemy_intents
    snapshot = scene()
    for enemy in snapshot["raw_state"]["combat"]["enemies"]:
        enemy.update(move_id="ATTACK_MOVE",
                     intents=[{"intent_type": "Attack", "hits": 1, "label": "14", "damage": 14}])
    kwargs, bindings, assumptions = bind_action("play_card", {"card_ref": "c2"}, action_refs(snapshot))
    step = {"action_type": "play_card", "kwargs": kwargs, "bindings": bindings,
            "assumptions": assumptions, "enemy_intents": enemy_intents(snapshot)}
    after = deepcopy(snapshot)
    for enemy in after["raw_state"]["combat"]["enemies"]:
        enemy["intents"] = [{"intent_type": "Attack", "hits": 1, "label": "10", "damage": 10}]
    assert resolve_action(step, after)[1] is None
    after["raw_state"]["combat"]["enemies"][0]["intents"] = [
        {"intent_type": "Attack", "hits": 2, "label": "10×2", "damage": 10}]
    assert resolve_action(step, after)[1]["code"] == "enemy_intent_changed"
