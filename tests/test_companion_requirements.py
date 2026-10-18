from copy import deepcopy
import pytest

from plugin.plugins.sts2_autoplay.action_identity import action_refs, bind_action
from plugin.plugins.sts2_autoplay.agent_facts import build_agent_facts
from plugin.plugins.sts2_autoplay.companion_session import CompanionSession


@pytest.mark.parametrize("kind", ["only_target", "reserve", "wait"])
def test_partial_replacement_preserves_constraints_and_condition_anchors(kind):
    snapshot = {"agent_view": {"screen": "COMBAT", "run_id": "r", "turn": 1},
                "raw_state": {"combat": {"hand": [{"index": 0, "entity_id": "card-1", "name": "打击"}],
                    "enemies": [{"index": 0, "entity_id": "enemy-2", "name": "小怪", "current_hp": 6},
                                {"index": 1, "entity_id": "enemy-3", "name": "Boss", "current_hp": 100}]}}}
    session = CompanionSession()
    session.observe(snapshot["agent_view"])
    session.remember("user", "本场保留这个限制")
    behavior = {"kind": kind, "action_types": ["play_card"]}
    if kind == "only_target":
        behavior["target_ref"] = "e2"
    elif kind == "reserve":
        behavior["card_ref"] = "c1"
    update = {"op": "add", "source_message_id": "1", "text": "原限制", "scope": "battle",
              "strength": "hard", "behavior": behavior, "activate_turn": 1, "through_turn": 3,
              "conditions": [{"kind": "action_result", "actor": "master", "action_type": "play_card", "card_id": "SETUP"}]}
    session.apply_requirement_updates([update], snapshot)
    original = deepcopy(session.active_requirements()[0])
    snapshot["agent_view"]["turn"] = 2
    session.observe(snapshot["agent_view"])
    session.remember("user", "按刚才的继续")
    replacement = {"op": "replace", "id": original["id"], "source_message_id": "2", "text": "继续原限制"}
    session.apply_requirement_updates([replacement], snapshot)
    current = session.active_requirements()[0]
    for field in ("behavior", "scope", "strength", "conditions", "activate_turn", "through_turn"):
        assert current[field] == original[field]
    kwargs, bindings, _ = bind_action("play_card", {"card_ref": "c1", "target_ref": "e3"}, action_refs(snapshot))
    assert session.requirement_blocker({"action_type": "play_card", "kwargs": kwargs, "bindings": bindings}, snapshot)
    before = deepcopy(session.requirements.records)
    with pytest.raises(ValueError, match="replace.behavior"):
        session.apply_requirement_updates([{**replacement, "id": current["id"], "behavior": {}}], snapshot)
    assert session.requirements.records == before
    if kind == "only_target":
        with pytest.raises(ValueError, match="目标分工需要 target_ref"):
            session.apply_requirement_updates([{**replacement, "id": current["id"], "behavior": {"kind": "only_target"}}], snapshot)
        assert session.requirements.records == before
    session.apply_requirement_updates([{**replacement, "id": current["id"], "behavior": {"kind": "intent"}, "conditions": []}], snapshot)
    assert session.requirement_blocker({"action_type": "play_card", "kwargs": kwargs, "bindings": bindings}, snapshot) is None


def test_scene_requirement_survives_child_page_and_does_not_revive():
    """营火要求在其选牌子页仍属同一流程；离开后结束，第二个营火不复活（Q01）。"""
    session = CompanionSession()
    snapshot = {"agent_view": {"screen": "REST", "run_id": "r", "run": {"floor": 15}}, "raw_state": {}}
    session.observe(snapshot["agent_view"])
    session.remember("user", "你下个火堆不用回血，我可以给你回血，你敲牌就行")
    session.apply_requirement_updates([{"op": "add", "source_message_id": "1", "text": "下个火堆不休息，去敲牌",
        "scope": "scene", "scene": "REST", "behavior": {"kind": "intent"}}], snapshot)
    assert session.requirements.records[0]["status"] == "active"
    # 同层进入营火的升级选牌页：仍是同一流程，要求不失效。
    snapshot["agent_view"].update(screen="CARD_SELECTION")
    session.refresh_requirements(snapshot)
    record = session.requirements.records[0]
    assert record["status"] == "active" and record["evidence"]["kind"] == "scene_child"
    # 离开到下一层：结束，且第二个营火不会让旧约定复活。
    snapshot["agent_view"].update(screen="MAP", run={"floor": 16})
    session.refresh_requirements(snapshot)
    assert session.requirements.records[0]["status"] == "expired"
    snapshot["agent_view"].update(screen="REST", run={"floor": 23})
    session.refresh_requirements(snapshot)
    assert not session.active_requirements()


def test_processed_message_cannot_recreate_expired_requirement_after_restore():
    session = CompanionSession()
    snapshot = {"agent_view": {"screen": "MAP", "run_id": "r", "run": {"floor": 1}}, "raw_state": {}}
    session.observe(snapshot["agent_view"])
    session.remember("user", "走左边吧")
    update = {"op": "add", "source_message_id": "1", "text": "这次走左边", "scope": "scene",
              "scene": "MAP", "behavior": {"kind": "intent"}}
    session.apply_requirement_updates([update], snapshot)
    session.resolve_messages({"1": "guidance"})
    snapshot["agent_view"].update(screen="EVENT")
    session.observe(snapshot["agent_view"])
    session.refresh_requirements(snapshot)
    restored = CompanionSession()
    restored.observe(snapshot["agent_view"])
    restored.restore_requirements(session.context())
    assert restored.requirements.records[0]["status"] == "expired"
    with pytest.raises(ValueError, match="source_message_id"):
        restored.apply_requirement_updates([{**update, "scope": "run"}], snapshot)
    assert not restored.active_requirements()
    restored.remember("user", "以后这局每次走左边")
    current_messages = restored.pending_messages()
    current_id = current_messages[0]["id"]
    restored.resolve_messages({current_id: "guidance"})
    restored.apply_requirement_updates([{**update, "scope": "run", "source_message_id": current_id}], snapshot,
                                       messages=current_messages)
    assert restored.active_requirements()[0]["source_message_id"] == current_id


def test_one_choice_completes_on_actual_result_and_later_preferences_use_live_scope():
    session = CompanionSession()
    view = {"run_id": "r", "screen": "CARD_SELECTION_REWARD", "run": {"floor": 3}}
    snapshot = {"agent_view": view, "raw_state": {}}
    session.observe(view)
    session.remember("user", "选第二张")
    update = {"op": "add", "source_message_id": "1", "text": "这次选第二张", "scope": "action", "behavior": {"kind": "intent"}}
    operation = {"action_type": "choose_reward_card", "kwargs": {"option_index": 1}}
    session.apply_requirement_updates([update], snapshot, operation)
    session.bind_requirement_completions(operation)
    session.record_action(operation, anchor=session.anchor(), before=view, after=view, result={"status": "pending", "executed": False})
    session.refresh_requirements(snapshot)
    assert session.active_requirements()
    view["screen"] = "REWARD"
    session.observe(view)
    session.refresh_requirements(snapshot)
    assert session.requirements.records[-1]["status"] == "expired"
    session.actions[-1].update(status="completed", executed=True)
    session.confirm_requirement_action(session.actions[-1])
    session.refresh_requirements(snapshot)
    assert not session.active_requirements() and session.requirements.records[-1]["status"] == "completed"
    view["screen"] = "CARD_SELECTION_REWARD"
    view["run"]["floor"] = 5
    session.observe(view)
    session.refresh_requirements(snapshot)
    assert not session.active_requirements()
    session.apply_requirement_updates([update], snapshot)
    view["screen"] = "MAP"
    session.observe(view)
    session.refresh_requirements(snapshot)
    assert not session.active_requirements()  # 没有执行结果也只在原来的选择界面有效。
    session.remember("user", "后面先防守")
    preference = {"op": "add", "source_message_id": "2", "text": "后续战斗优先防守", "scope": "battle", "behavior": {"kind": "intent"}}
    with pytest.raises(ValueError, match="战斗"):
        session.apply_requirement_updates([preference], snapshot)
    preference["scope"] = "run"
    session.apply_requirement_updates([preference], snapshot)
    view["screen"] = "COMBAT"
    session.observe(view)
    session.refresh_requirements(snapshot)
    assert session.active_requirements()[0]["text"] == "后续战斗优先防守"


def test_cooperation_requirement_survives_history_reload_and_completes_from_actual_result():
    snapshot = {"agent_view": {"run_id": "r", "screen": "COMBAT", "turn": 1, "run": {"floor": 4}},
                "raw_state": {"action_results_epoch": "process-a", "action_results": [
                    {"sequence": 1, "actor": "master", "action_type": "play_card", "card_id": "SETUP", "status": "completed"}],
                    "combat": {"player": {"energy": 3, "powers": []},
                        "players": [{"is_local": False, "player_id": "human", "energy": 3, "powers": []}],
                        "hand": [{"index": 0, "entity_id": "card-1", "card_id": "STRIKE", "card_type": "Attack", "name": "打击"}],
                        "enemies": [{"index": 0, "entity_id": "enemy-2", "name": "右边", "is_alive": True,
                                     "powers": [{"power_id": "VULNERABLE", "amount": 0}]}]}}}
    session = CompanionSession()
    session.observe(snapshot["agent_view"])
    # 已在第三场战斗，重载必须保住此锚点。
    session._battle_number = 3
    session.facts["battle_id"] = "r:3"
    session._anchors[-1]["battle_id"] = "r:3"
    session.remember("user", "等我打完配合牌，右边易伤到两层后再用这张打击")
    source = session.user_requests[-1]["id"]
    session.apply_requirement_updates([{"op": "add", "source_message_id": source,
        "text": "等配合牌完成和右边易伤两层后打击", "scope": "battle", "strength": "hard",
        "behavior": {"kind": "wait", "action_types": ["play_card"], "card_types": ["Attack"], "target_ref": "e2"},
        "conditions": [{"kind": "power", "subject": "enemy", "target_ref": "e2", "power_id": "VULNERABLE", "value": 2},
                       {"kind": "action_result", "actor": "master", "action_type": "play_card", "card_id": "SETUP"}],
        "completion": {"kind": "action_result", "actor": "self", "action_type": "play_card", "card_ref": "c1"}}], snapshot)
    kwargs, bindings, _ = bind_action("play_card", {"card_ref": "c1", "target_ref": "e2"}, action_refs(snapshot))
    operation = {"action_type": "play_card", "kwargs": kwargs, "bindings": bindings}
    assert session.requirement_blocker(operation, snapshot)["code"] == "requirement_wait"
    assert session.requirement_blocker({"action_type": "end_turn", "kwargs": {}}, snapshot) is None
    session.resolve_messages({source: "guidance"})
    for i in range(25):
        session.remember("user", f"闲聊{i}", kind="chat")
    assert not any(m["id"] == source for m in session.user_requests)
    assert session.active_requirements()[0]["source_text"].startswith("等我打完")

    restored = CompanionSession()
    restored.observe(snapshot["agent_view"])
    restored.restore_requirements(session.context())
    assert restored.anchor()["battle_id"] == "r:3"
    assert not restored.refresh_requirements(snapshot)
    snapshot["raw_state"]["combat"]["enemies"][0]["powers"][0]["amount"] = 2
    snapshot["raw_state"]["combat"]["player"]["energy"] = 1
    assert restored.refresh_requirements(snapshot)
    assert restored.waiting_requirements()  # 能量波动和旧完成记录都不能充当这次队友动作完成。
    snapshot["raw_state"]["action_results"].append({"sequence": 2, "actor": "master", "action_type": "play_card",
                                                   "card_id": "SETUP", "status": "completed", "executed": True})
    assert restored.refresh_requirements(snapshot)
    assert restored.requirement_blocker(operation, snapshot) is None
    assert restored.active_requirements()[0]["status"] == "active"  # 解锁不等于完成。
    snapshot["raw_state"]["combat"]["enemies"][0]["powers"][0]["amount"] = 0
    assert restored.requirement_blocker(operation, snapshot)["code"] == "requirement_wait"
    snapshot["raw_state"]["combat"]["enemies"][0]["powers"][0]["amount"] = 2
    restored.record_action(operation, anchor=restored.anchor(), before=snapshot["agent_view"],
                           after=snapshot["agent_view"], result={"status": "completed", "executed": True})
    assert restored.refresh_requirements(snapshot)
    assert not restored.active_requirements()
    assert restored.requirements.records[0]["status"] == "completed"
    facts = build_agent_facts(snapshot["raw_state"])["collaboration"]
    assert facts["enemies"][0]["powers"][0]["power_id"] == "VULNERABLE"
    assert "hand" not in facts["master"]
    next_run = deepcopy(snapshot["agent_view"])
    next_run["run_id"] = "new"
    restored.observe(next_run)
    assert restored.requirements.records == []

    # 未来意图到场后才知道具体动作；选择时绑定，pending确认前不能自宣完成。
    next_run.update(screen="MAP")
    snapshot["agent_view"] = next_run
    restored.observe(next_run)
    restored.remember("user", "下个火堆愈合，继续留着这瓶药")
    source = restored.user_requests[-1]["id"]
    snapshot["raw_state"]["run"] = {"potions": [{"index": 0, "occupied": True, "potion_id": "FIRE"}]}
    restored.apply_requirement_updates([
        {"op": "add", "source_message_id": source, "text": "下个火堆愈合", "scope": "scene", "scene": "REST_SITE", "behavior": {"kind": "intent"}},
        {"op": "add", "source_message_id": source, "text": "本局保留药水", "scope": "run", "behavior": {"kind": "reserve", "potion_index": 0}}], snapshot)
    next_run["screen"] = "REST"
    restored.observe(next_run)
    restored.refresh_requirements(snapshot)
    intent, reserve = restored.active_requirements()
    with pytest.raises(ValueError, match="intent"):
        restored.bind_requirement_completions({"action_type": "use_potion", "kwargs": {"option_index": 0}, "completes_requirements": [reserve["id"]]})
    rest = {"action_type": "choose_rest_option", "kwargs": {"option_index": 0}, "completes_requirements": [intent["id"]]}
    restored.accept_plan(rest, next_run)
    restored.record_action(rest, anchor=restored.anchor(), before=next_run, after=next_run,
                           result={"status": "pending", "executed": False})
    record = restored.actions[-1]
    assert not restored.confirm_requirement_action(record)
    next_run["screen"] = "MAP"
    restored.observe(next_run)
    restored.refresh_requirements(snapshot)
    assert restored.requirements.records[0]["status"] == "expired"
    record.update(status="completed", executed=True)
    assert restored.confirm_requirement_action(record)
    assert restored.requirements.records[0]["status"] == "completed"
    assert restored.active_requirements()[0]["id"] == reserve["id"]
    assert not restored.confirm_requirement_action(record)
