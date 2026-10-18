from __future__ import annotations

import asyncio
from copy import deepcopy
import json
from time import perf_counter, time
from unittest.mock import AsyncMock, Mock

import pytest

from plugin.plugins.sts2_autoplay.catgirl_memory import CatgirlMemoryStore
from plugin.plugins.sts2_autoplay.companion_journal import CompanionJournal
from plugin.plugins.sts2_autoplay.companion_session import CompanionSession
from plugin.plugins.sts2_autoplay.service import STS2AutoplayService
from plugin.plugins.sts2_autoplay.snapshot_normalizer import normalize_snapshot


def test_strategy_context_preserves_presets_without_run_slogans(tmp_path):
    from plugin.plugins.sts2_autoplay.strategy_parser import STS2StrategyParser
    from plugin.plugins.sts2_autoplay.strategy_repository import STS2StrategyRepository

    legacy = tmp_path / "defect"
    legacy.mkdir()
    (legacy / "base.md").write_text("球体系、低战损、先防御", encoding="utf-8")
    (tmp_path / "player_overrides.md").write_text("旧全局规则", encoding="utf-8")
    explicit = tmp_path / "user_overrides" / "ironclad"
    explicit.mkdir(parents=True)
    (explicit / "base.md").write_text("玩家明确保存的战士偏好", encoding="utf-8")
    memory = Mock()
    memory.summarize_recent.return_value = {"lessons": ["下次决策慢一点"], "preferences": ["旧频次偏好"]}
    preferences = Mock()
    preferences.list_domain.return_value = []
    repository = STS2StrategyRepository(Mock(), preferences, catgirl_memory=memory)
    repository._parser = STS2StrategyParser(Mock(), strategies_dir=tmp_path)
    context = repository.build_context({"screen": "COMBAT", "raw_state": {"run": {"character_id": "DEFECT"}}})
    assert context["strategy_name"] == "defect"
    assert "球体系、低战损、先防御" in context["strategy_prompt"]
    assert "旧全局规则" in context["state_strategy_prompt"]
    assert context["strategy_directives"]["avoid"] == []
    assert context["strategy_directives"]["prefer"] == []
    memory.summarize_recent.assert_not_called()
    assert "玩家明确保存的战士偏好" in repository._parser.load_prompt("ironclad", "combat")


def test_legacy_reports_remain_readable_without_becoming_decision_advice():
    store = CatgirlMemoryStore()
    run = store.add_run("old", character="DEFECT", outcome="defeat", floor=16, act=0,
                        decision_memory=[{"reason": "暴毙", "current_hp": 1, "max_hp": 75}])
    assert run["lessons"] == []
    store.runs[0]["lessons"] = ["下次决策慢一点"]
    store.attach_reflection("old", "旧局保守复盘口号")
    store.experiences = [{"id": "preference", "version": 1, "text": "玩家明确偏好", "kind": "preference",
                          "status": "active", "scope": "persistent", "conditions": {"character": "DEFECT"},
                          "updated_at": 1, "source": {}}]
    result = store.decision_context({"run_id": "new", "run": {"character_id": "DEFECT"}}, include_history=True)
    assert result["experiences"][0]["id"] == "preference"
    assert result["runs"][0]["run_id"] == "old"
    assert "lessons" not in result["runs"][0] and "reflection" not in result["runs"][0]
    assert store.runs[0]["lessons"] == ["下次决策慢一点"]
    assert store.runs[0]["reflection"] == "旧局保守复盘口号"


def test_decision_knowledge_uses_real_scene_and_never_borrows_another_character(tmp_path):
    from plugin.plugins.sts2_autoplay.strategy_parser import STS2StrategyParser
    from plugin.plugins.sts2_autoplay.strategy_repository import STS2StrategyRepository
    role = tmp_path / "defect"
    role.mkdir()
    (role / "base.md").write_text("## 角色总纲\n按资源选择构筑。\n## 战斗\n战斗专项。", encoding="utf-8")
    (role / "reward.md").write_text("## 选牌\n奖励专项。", encoding="utf-8")
    repository = STS2StrategyRepository(Mock(), Mock())
    repository._parser = STS2StrategyParser(Mock(), strategies_dir=tmp_path)
    view = {"screen": "CARD_SELECTION", "run": {"character_id": "DEFECT"}, "selection": {"kind": "deck_upgrade_select"}}
    upgrade = repository.decision_knowledge(view, {})
    assert upgrade["scene"] == "upgrade" and "按资源" in upgrade["base"]["text"]
    assert "奖励专项" not in upgrade["scene_rules"]["text"]
    (role / "remove.md").write_text("## 移除\n删牌专项。", encoding="utf-8")
    for screen in ("CARD_SELECTION_REMOVE", "CARD_SELECTION_DELETE", "CARD_SELECTION_DELET", "CARD_SELECTION_UNUSEFULL"):
        remove = repository.decision_knowledge({**view, "screen": screen, "selection": {"kind": "deck_card_select"}}, {})
        assert remove["scene"] == "remove" and "删牌专项" in remove["scene_rules"]["text"]
    combat = repository.decision_knowledge({**view, "screen": "COMBAT", "selection": {}}, {})
    assert combat["base"] == upgrade["base"] and "战斗专项" in combat["scene_rules"]["text"]
    missing = repository.decision_knowledge({"screen": "COMBAT", "run": {"character_id": "IRONCLAD"}}, {})
    assert not missing["base"]["text"]
    assert "missing" in repository.decision_knowledge({"screen": "COMBAT"}, {})


def test_common_scene_rules_are_loaded_on_demand(tmp_path):
    from plugin.plugins.sts2_autoplay.strategy_parser import STS2StrategyParser

    role = tmp_path / "defect"
    role.mkdir()
    (role / "base.md").write_text("## 角色总纲\n按资源选择构筑。", encoding="utf-8")
    common = tmp_path / "common"
    common.mkdir()
    (common / "map.md").write_text("## 地图流程\n先投票再等队友。", encoding="utf-8")
    (common / "combat.md").write_text("## 战斗规则\n能量每回合重置。", encoding="utf-8")
    parser = STS2StrategyParser(Mock(), strategies_dir=tmp_path)

    map_knowledge = parser.decision_knowledge("defect", "map")
    combat_knowledge = parser.decision_knowledge("defect", "combat")
    assert "先投票再等队友" in map_knowledge["scene_rules"]["text"]
    assert "common/map.md" in [source["path"] for source in map_knowledge["scene_rules"]["sources"]]
    assert "能量每回合重置" in combat_knowledge["scene_rules"]["text"]
    assert "先投票再等队友" not in combat_knowledge["scene_rules"]["text"]


def test_experiences_filter_scenes_candidates_and_keep_explicit_preference():
    from types import SimpleNamespace
    store = CatgirlMemoryStore()
    source = {"id": "m", "content": "以后在战斗稳健一点"}
    update = {"op": "add", "source_message_id": "m", "kind": "preference", "scope": "persistent",
              "text": source["content"], "conditions": {"character": "DEFECT", "scenes": ["combat"]}}
    journal = SimpleNamespace(data={"run_id": "r", "turns": []})
    store.validate_learning([update], [], {}, messages=[source], journal=journal, available=[], terminal=False, step_count=0)
    with pytest.raises(ValueError, match="scenes"):
        store.validate_learning([{**update, "conditions": {"scenes": ["bogus"]}}], [], {}, messages=[source], journal=journal, available=[], terminal=False, step_count=0)
    store.experiences = [{"id": str(i), "version": 1, "text": "条件建议", "kind": kind,
                          "status": "revoked" if i == 5 else "active", "scope": "persistent",
                          "conditions": update["conditions"], "updated_at": i, "source": {}}
                         for i, kind in enumerate(["preference", "player_correction", "player_correction", "player_correction", "model_candidate", "player_correction"])]
    assert not store.relevant_experiences({"screen": "REWARD", "run": {"character_id": "DEFECT"}}, {})
    selected = store.relevant_experiences({"screen": "COMBAT", "run": {"character_id": "DEFECT"}}, {})
    assert len(selected) == 3 and "0" in {e["id"] for e in selected}
    assert not {"4", "5"} & {e["id"] for e in selected}


def test_routine_experience_projection_is_small_and_history_stays_available():
    store = CatgirlMemoryStore()
    store.runs.append({"run_id": "old", "character": "DEFECT", "decisions": [
        {"action_type": "play_card", "delta": {"detail": "旧现场" * 1000}}]})
    store.experiences.append({"id": "e", "version": 2, "text": "战斗偏好", "kind": "preference",
        "status": "active", "scope": "persistent", "conditions": {"scenes": ["combat"]}, "updated_at": 1,
        "source": {"run_id": "old", "turn_id": "old:1", "case": {"record": {"before": {"detail": "现场" * 2000}, "actions": []}}}})
    before = deepcopy(store.runs), deepcopy(store.experiences)
    view = {"screen": "COMBAT", "run_id": "new"}
    brief = store.decision_context(view, {})
    detailed = store.decision_context(view, {}, include_history=True)
    assert not brief["runs"] and detailed["runs"][0]["run_id"] == "old"
    assert brief["experiences"][0]["source"] == {"run_id": "old", "turn_id": "old:1"}
    assert detailed["experiences"][0]["source"]["evidence"]["before"]["detail"]
    assert len(json.dumps(brief)) < len(json.dumps(detailed)) / 5
    assert (store.runs, store.experiences) == before


def test_previous_turn_review_separates_planning_from_dispatch_and_uses_focused_request(monkeypatch):
    from plugin.plugins.sts2_autoplay.llm_planner import STS2LLMPlanner
    journal = CompanionJournal()
    journal.activate("r")
    planned = {"screen": "COMBAT", "player": {"energy": 3}, "hand": [{"line": "打击：造成6点伤害。"}] * 2,
               "enemies": [{"name": "敌人", "hp": "39/123"}], "effects": {"enemies": [{"powers": ["易伤1（增伤50%）"]}]}}
    dispatch = {**deepcopy(planned), "enemies": [{"name": "敌人", "hp": "13/123"}]}
    for turn in (3, 4):
        journal.data["turns"].append({"id": f"r:1:turn:{turn}", "battle_id": "r:1", "turn": turn, "closed": True,
            "before": {"hp": "59/75"}, "after": {"hp": "58/75"}, "plans": [],
            "actions": [{"id": f"a{turn}", "action_type": "play_card", "status": "completed", "executed": True}]})
    journal.data["latest"] = {"id": "r:room:reward", "turn": None}
    journal.data["details"] = {"r:1:turn:3": {"initial": {"player": {"energy": 0}, "hand": []},
        "plans": [{"scene": planned, "reason": "原防御路线"}], "actions": {"a3": {"before": dispatch}}}}
    message = {"id": "1", "content": "你上回合能打死呀", "turn": 4}
    review = journal.review_context([message])
    focus = review["turns"][0]
    assert review["focus_turn_id"] == "r:1:turn:3"
    assert focus["planning"]["enemies"][0]["hp"] == "39/123"
    assert focus["first_dispatch"]["enemies"][0]["hp"] == "13/123"
    assert focus["first_dispatch"]["player"]["energy"] == 3
    assert len(focus["first_dispatch"]["hand"]) == 2
    planner = STS2LLMPlanner(Mock(), persona_provider=lambda: "小铃")
    planner._history_owner, planner._last_prefix = "s", "old"
    planner._history = [{"role": "assistant", "content": "之前真实对话"}]
    monkeypatch.setattr(planner, "_ensure_llm", AsyncMock(return_value=object()))
    seen = []
    async def invoke(llm, messages):
        seen.append(deepcopy(messages))
        return json.dumps({"action_type": None, "plan_update": "keep", "message_effects": {"1": "query"},
                           "reply": "13血时两张打击共18伤，应该收掉。"}, ensure_ascii=False)
    monkeypatch.setattr(planner, "_invoke_text", invoke)
    operation = asyncio.run(planner.plan({"conversation_owner": "s", "agent_view": {"screen": "REWARD", "actions": ["resolve_rewards"]},
        "new_player_messages": [message], "recent_turn_review": review}))
    assert len(seen[0]) == 2
    assert json.loads(seen[0][-1]["content"])["actions_enabled"] is False
    assert not operation.action_type and operation.reply
    assert planner._history[0]["content"] == "之前真实对话"


def test_reflection_includes_late_fatal_turn_with_string_hp_and_original_reason():
    turns = [{"id": f"r:1:turn:{i}", "battle_id": "r:1", "turn": i,
              "before": {"hp": "60/75"}, "after": {"hp": "60/75"},
              "actions": [], "plans": []} for i in range(1, 21)]
    turns[-2].update(before={"hp": "22/75"}, after={"hp": "5/75"})
    turns[-1].update(before={"hp": "5/75"}, after={"hp": "2/75"},
                     following_observation={"screen": "GAME_OVER", "hp": "0/75"},
                     actions=[{"id": "fatal-end", "action_type": "end_turn", "status": "completed"}],
                     plans=[{"model_reason": "这次挨7伤不会死", "steps": [], "solver_summary": {"win_prob": 0}}])
    cases = [{"id": f"case-{i}", "turn_id": t["id"], "action_id": "", "reason": "stale"}
             for i, t in enumerate(turns[:18])]
    turns += [{"id": f"r:1:turn:{i}", "battle_id": "r:1", "turn": i, "before": {"hp": "0/75"},
               "after": {"hp": "0/75"}, "actions": [], "plans": []} for i in (21, 22, 23)]
    result = CompanionJournal.reflection_context({"run_id": "r", "turns": turns, "cases": cases})
    selected = {t["id"]: t for t in result["key_turns"]}
    assert "r:1:turn:19" in selected and "r:1:turn:20" in selected
    assert selected["r:1:turn:20"]["following_observation"]["hp"] == "0/75"
    assert selected["r:1:turn:20"]["plans"][0]["model_reason"] == "这次挨7伤不会死"
    assert "r:1:turn:23" not in selected and len(selected) <= 12


def test_terminal_review_waits_for_feedback_and_saves_after_menu_reload(tmp_path, monkeypatch):
    from plugin.plugins.sts2_autoplay.llm_planner import STS2LLMPlanner
    from plugin.plugins.sts2_autoplay.tests.test_companion_development import prepare

    terminal = {"screen": "GAME_OVER", "run_id": "r", "actions": []}
    service = prepare(monkeypatch, terminal)
    journal = service._companion_session.journal
    journal.directory = str(tmp_path / "runs")
    turn = {"id": "r:1:turn:2", "battle_id": "r:1", "turn": 2,
            "before": {"hp": "5/75"}, "after": {"hp": "0/75"}, "closed": True,
            "actions": [{"id": "end", "action_type": "end_turn", "status": "completed", "executed": True}], "plans": []}
    journal.data["turns"] = [turn]
    store = CatgirlMemoryStore(str(tmp_path / "memory.json"))
    store.add_run("r", character="DEFECT", outcome="defeat", floor=22, act=1, decision_memory=[], process=journal.run_context())
    service._catgirl_memory = store
    planner = STS2LLMPlanner(Mock(), persona_provider=lambda: "小铃")
    monkeypatch.setattr(planner, "_ensure_llm", AsyncMock(return_value=object()))
    update = {"op": "add", "kind": "model_candidate", "scope": "persistent",
              "text": "结束回合前比较扣除格挡后的来伤与剩余血量，达到致死线先检查保命手段。",
              "conditions": {"actions": ["end_turn"]}, "turn_id": turn["id"]}
    answer = {"action_type": None, "plan_update": "wait", "reply": "5血吃7伤到了致死线。你觉得关键问题在哪？",
        "review": {"kind": "run_end", "turn_id": turn["id"], "assessment": "最后回合到了致死线。", "question": "你觉得关键问题在哪？"},
        "learning_updates": [update]}
    corrected = {**answer, "learning_updates": []}
    invoke = AsyncMock(side_effect=[json.dumps(answer, ensure_ascii=False), json.dumps(corrected, ensure_ascii=False)])
    monkeypatch.setattr(planner, "_invoke_text", invoke)
    service._llm_planner = planner
    monkeypatch.setattr(service, "_read_companion_dialogue", AsyncMock())
    monkeypatch.setattr(service, "_publish_companion_reply", AsyncMock())
    asyncio.run(service.run_autoplay_step())
    assert invoke.await_count == 2 and not store.experiences and not store.runs[0]["reflection"]
    assert store.pending_review()["turn_id"] == turn["id"]
    with pytest.raises(ValueError, match="先讨论"):
        store.validate_learning([update], [], {}, messages=[], journal=journal, available=[], terminal=True, step_count=0)

    menu = {"screen": "MAIN_MENU", "run_id": "run_unknown", "actions": []}
    service = prepare(monkeypatch, menu)
    service._catgirl_memory = CatgirlMemoryStore(store.path)
    service._companion_session.journal.directory = journal.directory
    service._companion_session.remember("user", "刚才应该先检查其他保命手段。")
    message = service._companion_session.pending_messages()[0]
    feedback = {"kind": "run_feedback", "run_id": "r", "turn_id": turn["id"],
                "feedback_message_ids": [message["id"]], "assessment": "讨论后：结束前应先核对合法保命手段，再判断是否有解。"}
    update = {**update, "kind": "player_correction", "source_message_id": message["id"]}
    answer = {"action_type": None, "plan_update": "keep", "reply": "对，先检查当时实际可用的手段，再归因。",
              "message_effects": {message["id"]: "query"}, "review": feedback, "learning_updates": [update]}
    planner = STS2LLMPlanner(Mock(), persona_provider=lambda: "小铃")
    monkeypatch.setattr(planner, "_ensure_llm", AsyncMock(return_value=object()))
    invoke = AsyncMock(return_value=json.dumps(answer, ensure_ascii=False))
    monkeypatch.setattr(planner, "_invoke_text", invoke)
    service._llm_planner = planner
    monkeypatch.setattr(service, "_read_companion_dialogue", AsyncMock())
    monkeypatch.setattr(service, "_publish_companion_reply", AsyncMock(return_value={"text_sent": True}))
    asyncio.run(service.run_autoplay_step())
    assert invoke.await_count == 1
    assert json.loads(invoke.await_args.args[1][-1]["content"])["pending_run_review"]["run_id"] == "r"
    restored = CatgirlMemoryStore(store.path)
    assert not restored.pending_review() and len(restored.experiences) == 1
    assert restored.experiences[0]["source"]["turn_id"] == turn["id"]
    assert restored.experiences[0]["source"]["message"] == message["content"]
    assert restored.runs[0]["reflection"] == feedback["assessment"]
    service.execute_operation.assert_not_awaited()


def test_low_hp_experience_matches_formatted_hp_and_numeric_facts():
    store = CatgirlMemoryStore()
    store.experiences = [{"id": "low", "version": 1, "text": "低血判断", "kind": "player_correction",
        "scope": "persistent", "status": "active", "conditions": {"character": "DEFECT", "hp_lte": 10},
        "source": {}, "updated_at": 0}]
    current = {"run": {"character": "DEFECT"}, "combat": {"player": {"hp": "3/80"}}}
    assert store.relevant_experiences(current, {})[0]["id"] == "low"
    current["combat"]["player"]["hp"] = "30/80"
    assert not store.relevant_experiences(current, {})
    assert store.relevant_experiences(current, {"collaboration": {"self": {"current_hp": 3}}})[0]["id"] == "low"
    current["combat"]["player"]["hp"] = "unknown"
    assert not store.relevant_experiences(current, {})


def view(turn=1):
    return {"run_id": "learning-run", "screen": "COMBAT", "turn": turn, "actions": ["end_turn"],
            "run": {"floor": 3, "hp": 30, "character": "DEFECT"},
            "combat": {"player": {"energy": 1, "current_hp": 30},
                       "hand": [{"i": 0, "card_id": "DEFEND", "line": "防御", "playable": True}],
                       "enemies": [{"i": 0, "name": "甲", "hp": 10, "intent": "ATTACK"}]}}


def test_journal_keeps_run_process_two_turns_and_pending_identity_after_reload(tmp_path):
    session = CompanionSession()
    session.journal = CompanionJournal(str(tmp_path))
    first_action = None
    for number in range(1, 4):
        current = view(number)
        session.observe(current, effects={"incoming_damage": 8})
        operation = {"action_type": "end_turn", "kwargs": {}, "reason": f"第{number}回合保留格挡", "continuation": []}
        session.accept_plan(operation, current)
        for i in range(12):
            op = {**operation, "record_id": f"action-{number}-{i}"}
            session.record_action(op, anchor=session.anchor(), before=current, after=current,
                                  result={"status": "pending", "executed": False})
            record = session.actions[-1]
            if first_action is None:
                first_action = record["id"]
            record.update(status="completed", executed=True)
            session.journal.confirm_action(record, current)
        if number == 1:
            case = session.journal.keep_case(session.journal.data["turns"][0]["id"], action_id=first_action)
    restored = CompanionJournal(str(tmp_path))
    restored.activate("learning-run")
    assert len(restored.data["turns"]) == 3
    assert sum(len(t["actions"]) for t in restored.data["turns"]) == 36
    assert all(a["status"] == "completed" for t in restored.data["turns"] for a in t["actions"])
    assert len(restored.data["details"]) == 2
    session.remember("user", "这场保留药水")
    pending_message = session.pending_messages()[0]
    session.resolve_messages({pending_message["id"]: "guidance"})
    session.save_input_result([pending_message], "我记下这场保留药水。", media=[], kind="chat")
    session.journal.save_checkpoint({"session": session.context(), "model_history": [{"role": "user", "content": "真实历史"}]})
    session.journal.save_checkpoint({"control_state": {"state": "paused", "pause_reason": "user"}})
    disk_checkpoint = restored.load_checkpoint()
    assert disk_checkpoint["session"]["input_results"][0]["reply"] == "我记下这场保留药水。"
    assert disk_checkpoint["model_history"][0]["content"] == "真实历史"
    assert disk_checkpoint["control_state"]["state"] == "paused"
    assert [t["turn"] for t in restored.review_context()["turns"]] == [2, 3]
    assert restored.data["cases"][0]["detail"]["initial"]["effects"]["incoming_damage"] == 8
    assert restored.data["cases"][0]["id"] == case["id"]
    assert len(restored.run_context()["turns"][0]["actions"]) == 12
    resumed = CompanionSession()
    resumed.journal = restored
    resumed.observe(view(3))
    assert resumed.anchor()["battle_id"] == "learning-run:1"
    resumed.observe({"run_id": "next", "screen": "COMBAT", "turn": 1, "run": {"floor": 1}})
    assert resumed.journal.data["run_id"] == "next" and not resumed.journal.data["cases"]
    previous = CompanionJournal(str(tmp_path))
    previous.activate("learning-run")
    assert len(previous.data["turns"]) == 3
    previous.data["telemetry"] = {"hp_trail": [{"hp": 30}], "route_log": [{"room": "甲"}]}
    pending = {"id": "pending-reload", "action_type": "end_turn", "status": "pending", "executed": False,
               "run_id": "learning-run", "battle_id": "learning-run:1", "turn": 3,
               "kwargs": {}, "outcome": {}}
    previous.data["turns"][-1]["actions"].append(pending)
    previous.save()
    recovery = STS2AutoplayService(Mock(), lambda _: None)
    recovery._catgirl_memory = CatgirlMemoryStore()
    recovery._companion_session.journal = CompanionJournal(str(tmp_path))
    recovery._companion_session.journal.activate("learning-run")
    recovery._restore_journal_pending({"agent_view": view(3), "raw_state": {"turn": 3, "combat": {}}})
    assert recovery._state.hp_trail == [{"hp": 30}]
    assert recovery._state.route_log == [{"room": "甲"}]
    assert recovery._companion_session.journal.data["turns"][-1]["actions"][-1]["status"] == "unconfirmed"
    assert recovery._unconfirmed_action[1] == "end_turn"


def test_natural_query_correction_reuse_replace_and_revoke_in_unified_session(tmp_path, monkeypatch):
    service = STS2AutoplayService(Mock(), lambda _: None)
    service._catgirl_memory = CatgirlMemoryStore(str(tmp_path / "memory.json"))
    service._companion_session.journal = CompanionJournal(str(tmp_path / "runs"))
    current = view(1)
    service._companion_session.observe(current)
    op = {"action_type": "end_turn", "kwargs": {}, "reason": "当时判断击杀足够", "continuation": []}
    service._companion_session.accept_plan(op, current)
    service._companion_session.record_action(op, anchor=service._companion_session.anchor(), before=current,
        after=current, result={"status": "ok", "executed": True})
    turn_id = service._companion_session.journal.data["turns"][0]["id"]
    action_id = service._companion_session.actions[-1]["id"]
    current = view(2)
    snapshot = normalize_snapshot({"run_id": current["run_id"], "screen": "COMBAT", "in_combat": True,
                                  "turn": 2, "agent_view": current}, {"actions": [{"name": "end_turn"}]})
    snapshot["action_registry"] = service._action_registry.build(snapshot)
    service._state.snapshot = snapshot
    service._companion_session.observe(current)
    service._state.autoplay_state = "paused"
    service._state.pause_reason = "user"
    service._state.transport_state = "connected"
    service._ingress_active = True
    monkeypatch.setattr(service, "refresh_state", AsyncMock(return_value={"snapshot": snapshot}))
    monkeypatch.setattr(service, "_read_companion_dialogue", AsyncMock())
    monkeypatch.setattr(service, "_sync_mod_gate", AsyncMock(return_value={"applied": True, "settled": True}))
    monkeypatch.setattr(service, "_sync_execution_status", AsyncMock())
    monkeypatch.setattr(service, "_write_history_view", Mock())
    monkeypatch.setattr(service, "_publish_companion_reply", AsyncMock(return_value={"text_sent": True}))
    monkeypatch.setattr(service, "execute_operation", AsyncMock())
    monkeypatch.setattr(service._llm_planner, "_ensure_llm", AsyncMock(return_value=object()))
    service._companion_session.remember("user", "刚才为什么不防御？以后这种牌面请先防住8点来伤。")
    message = service._companion_session.pending_messages()[0]
    answer = {"action_type": None, "plan_update": "keep", "message_effects": {message["id"]: "query"},
        "reply": "当时我判断能击杀；现在应先防住来伤。", "review": {"turn_id": turn_id,
        "action_id": action_id, "assessment": "应先防御", "accepted": True}, "requirement_updates": [{"op": "add",
        "source_message_id": message["id"], "text": "本场先防住来伤", "scope": "battle", "strength": "preference", "behavior": {"kind": "intent"}}], "learning_updates": [{"op": "add",
        "source_message_id": message["id"], "text": "防御牌可用且敌人甲攻击时优先防御", "kind": "player_correction",
        "scope": "persistent", "conditions": {"cards": ["DEFEND"], "enemies": ["甲"]},
        "turn_id": turn_id, "action_id": action_id}]}
    seen = []
    async def invoke(llm, messages):
        seen.append(json.loads(messages[-1]["content"]))
        return json.dumps(answer, ensure_ascii=False)
    monkeypatch.setattr(service._llm_planner, "_invoke_text", invoke)
    asyncio.run(service.run_autoplay_step())
    assert service._state.autoplay_state == "paused" and service._state.pause_reason == "user"
    service.execute_operation.assert_not_awaited()
    assert [t["turn"] for t in seen[0]["recent_turn_review"]["turns"]] == [1, 2]
    assert service._llm_planner.metrics["model_calls"] == 1
    assert len(service._companion_session.journal.data["reviews"]) == 1
    assert service._companion_session.active_requirements()[0]["scope"] == "battle"
    store = CatgirlMemoryStore(str(tmp_path / "memory.json"))
    experience = store.relevant_experiences(current, {}, detail=True)[0]
    assert experience["source"]["message"] == message["content"]
    mismatch = deepcopy(current)
    mismatch["combat"]["hand"] = []
    assert not store.relevant_experiences(mismatch, {})
    store.apply_learning(answer["learning_updates"], messages=[message], journal=service._companion_session.journal)
    assert len(store.experiences) == 1
    usage = {"id": experience["id"], "version": 1, "reason": "遵循防守纠正", "steps": [0]}
    service._learning_available = [experience]
    monkeypatch.setattr(service._llm_planner, "_invoke_text", AsyncMock(return_value=json.dumps({
        "action_type": "end_turn", "kwargs": {}, "reason": "按条件保留格挡", "experience_uses": [usage]})))
    selected = asyncio.run(service._llm_planner.plan({"agent_view": current, "past_experience": {"experiences": [experience]},
        "active_requirements": []}, validate=lambda operation: service._validate_companion_action(snapshot, operation)))
    assert selected.experience_uses == [usage] and selected.as_dict()["experience_uses"] == [usage]
    store.record_exposure([experience], "learning-run", turn_id)
    record = {"id": "adopted", "run_id": "learning-run", "battle_id": "learning-run:1", "turn": 2,
              "experience_uses": [usage], "status": "pending", "executed": False, "outcome": {}}
    store.record_experience_result(record)
    record.update(status="completed", executed=True)
    store.record_experience_result(record)
    report = store.learning_report()["experiences"][0]
    assert (report["applicable_turns"], report["reads"], report["adoptions"], report["completed"]) == (1, 1, 1, 1)
    store.record_turn_result({"run_id": "learning-run", "battle_id": "learning-run:1", "turn": 2,
                             "before": {}, "after": {}, "following_observation": {"hp": 28}})
    assert store.relevant_experiences(current, {}, detail=True)[0]["adoption_evidence"][0]["turn_outcome"]["following_observation"]["hp"] == 28
    replacement = {**answer["learning_updates"][0], "op": "replace", "id": experience["id"], "text": "修正后的条件防守"}
    store.validate_learning([replacement], [], {}, messages=[message], journal=service._companion_session.journal,
                            available=[experience], terminal=False, step_count=0)
    store.apply_learning([replacement], messages=[message], journal=service._companion_session.journal)
    assert store.experiences[0]["version"] == 2 and len(store.experiences[0]["history"]) == 1
    revoke = {"op": "revoke", "id": experience["id"], "source_message_id": message["id"]}
    store.apply_learning([revoke], messages=[message], journal=service._companion_session.journal)
    assert not CatgirlMemoryStore(str(tmp_path / "memory.json")).relevant_experiences(current, {})
    with pytest.raises(ValueError, match="本轮提供"):
        store.validate_learning([], [usage], {}, messages=[message], journal=service._companion_session.journal,
                                available=[], terminal=False, step_count=1)
    store.add_run("learning-run", character="DEFECT", outcome="victory", floor=3, act=1,
                  decision_memory=[], process=service._companion_session.journal.run_context())
    store.attach_reflection("learning-run", "现在的评价")
    assert not store.runs[0]["lessons"] and store.runs[0]["process"]["turns"]
    conditional = CatgirlMemoryStore(str(tmp_path / "conditional.json"))
    update = {**answer["learning_updates"][0], "conditions": {"cards": ["DEFEND"],
        "deck_card_counts": [{"card_type": "Power", "max": 1}]}}
    conditional.validate_learning([update], [], answer["review"], messages=[message],
        journal=service._companion_session.journal, available=[], terminal=False, step_count=0)
    conditional.apply_learning([update], messages=[message], journal=service._companion_session.journal)
    assert conditional.relevant_experiences(current, {"deck_composition": [{"card_type": "Power", "count": 1}]})
    assert not conditional.relevant_experiences(current, {"deck_composition": [{"card_type": "Power", "count": 2}]})
    assert not conditional.relevant_experiences(current, {})  # 无完整牌组事实时不匹配数量条件。
    with pytest.raises(ValueError, match="已采纳"):
        conditional.validate_learning([], [], answer["review"], messages=[message],
            journal=service._companion_session.journal, available=[], terminal=False, step_count=0)


def test_effect_report_groups_configurations_and_counts_per_call_latency(monkeypatch):
    service = STS2AutoplayService(Mock(), lambda _: None)
    service._catgirl_memory = CatgirlMemoryStore()
    service._companion_session.observe(view())
    service._latency_baseline = {"model_calls": 8, "solver_calls": 3}
    service._llm_planner.metrics["model_calls"] = 9
    service._state.decision_metrics["solver_calls"] = 4
    service._step_timing = {"model_ms": 12, "solver_ms": 8}
    service._record_latency(perf_counter(), "model", {"status": "ok"})
    journal = service._companion_session.journal
    sample = journal.data["latency"][0]
    assert sample["model_calls"] == sample["solver_calls"] == 1
    for value in range(1, 11):
        journal.latency({"configuration": "other", "path": "selected_plan", "stages": {"dispatch_ms": value},
                         "model_calls": 0, "solver_calls": 0})
    groups = journal.report()["latency"]
    assert len(groups) == 2
    other = next(g for g in groups if g["configuration"] == "other")
    assert other["timings"]["dispatch_ms"] == {"samples": 10, "median_ms": 5.5, "p90_ms": 9}
    source = {"id": "input-1", "timestamp": time() - 0.2, "content": "先防御"}
    service._record_input_timing([source], "input_to_reply_ms")
    service._record_input_timing([source], "input_to_reply_ms")
    assert sum(s["path"] == "input" for s in journal.data["latency"]) == 1
    from plugin.plugins.sts2_autoplay.history_view import build_history_payload
    payload = build_history_payload([], learning_report=service._catgirl_memory.learning_report(), current_report=journal.report())
    assert payload["effects"][0]["run_id"] == "learning-run" and "learning" in payload


def test_character_strategy_memory_uses_stable_id_across_skin_and_run(tmp_path):
    from plugin.plugins.sts2_autoplay.agent_facts import build_agent_facts
    store = CatgirlMemoryStore(str(tmp_path / "memory.json"))
    message = {"id": "6", "request_id": "character-strategy", "content": "玩这个角色时优先挡伤害，再靠持续效果输出", "run_id": "first"}
    update = {"op": "add", "source_message_id": "6", "kind": "preference", "scope": "persistent",
              "text": message["content"], "conditions": {"character": "DEFECT"}}
    journal = CompanionJournal()
    journal.data = {"run_id": "first", "turns": []}
    store.validate_learning([update], [], {}, messages=[message], journal=journal, available=[], terminal=False, step_count=0)
    store.apply_learning([update], messages=[message], journal=journal)
    restored = CatgirlMemoryStore(store.path)
    facts = build_agent_facts({"run": {"character_id": "DEFECT"}})
    assert facts["character_id"] == "DEFECT"
    assert restored.relevant_experiences({"run_id": "next", "run": {"character": "蓝色大肥鱼"}}, facts)
    assert not restored.relevant_experiences({"run_id": "next", "run": {"character": "铁甲战士"}}, {"character_id": "IRONCLAD"})
    assert restored.experiences[0]["source"]["request_id"] == message["request_id"]


def test_memory_pledge_requires_persistent_experience(monkeypatch):
    """玩家明确说"存进记忆"时，只更新本局要求不算记住：必须落 persistent 经验，否则重判。"""
    from plugin.plugins.sts2_autoplay.llm_planner import STS2LLMPlanner

    planner = STS2LLMPlanner(Mock(), persona_provider=lambda: "小铃")
    monkeypatch.setattr(planner, "_ensure_llm", AsyncMock(return_value=object()))
    pledge = {"id": "6", "content": "存进记忆里 记住尽量先防御 我说抢输出再抢输出 局面合适或者能杀时再抢输出"}
    context = {"agent_view": {"screen": "COMBAT", "run_id": "r", "turn": 5, "actions": [], "available_actions": []},
               "new_player_messages": [pledge], "active_requirements": []}
    requirement = {"op": "add", "source_message_id": "6", "text": "默认先防御挡伤害",
                   "scope": "run", "strength": "preference", "behavior": {"kind": "intent"}}
    only_requirement = {"action_type": None, "plan_update": "keep", "message_effects": {"6": "guidance"},
                        "reply": "记住了，这条我存进这局的打法偏好了。", "requirement_updates": [requirement],
                        "learning_updates": []}
    experience = {"op": "add", "source_message_id": "6", "kind": "preference", "scope": "persistent",
                  "text": "默认优先防御挡伤害；主人主动要求或能击杀时才抢输出。", "conditions": {}}
    persisted = {**only_requirement, "learning_updates": [experience],
                 "reply": "记住了：默认先防御，我会把这条存进长期记忆里。"}
    invoke = AsyncMock(side_effect=[json.dumps(only_requirement, ensure_ascii=False),
                                    json.dumps(persisted, ensure_ascii=False)])
    monkeypatch.setattr(planner, "_invoke_text", invoke)
    operation = asyncio.run(planner.plan(context, validate=lambda _: (True, "")))
    assert invoke.await_count == 2, "只给本局要求应被硬校验拦下并重判一次"
    assert [u["scope"] for u in operation.learning_updates] == ["persistent"]

    # 反例：只说"这回合"的局内要求不该被当成跨局记忆，避免误伤。
    local = {"id": "7", "content": "记住这回合先防住来伤"}
    context = {"agent_view": context["agent_view"], "new_player_messages": [local], "active_requirements": []}
    local_answer = {"action_type": None, "plan_update": "keep", "message_effects": {"7": "guidance"},
                    "reply": "好，这回合先防。", "learning_updates": [],
                    "requirement_updates": [{**requirement, "source_message_id": "7", "scope": "turn"}]}
    invoke = AsyncMock(return_value=json.dumps(local_answer, ensure_ascii=False))
    monkeypatch.setattr(planner, "_invoke_text", invoke)
    asyncio.run(planner.plan(context, validate=lambda _: (True, "")))
    assert invoke.await_count == 1, "局内要求不应触发跨局记忆校验"
