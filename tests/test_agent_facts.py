"""agent_facts 补充事实：从原始 /state 补齐 agent_view 裁掉的决策信息。

背景：Mod 的 GET /state 同时给精简 agent_view 和完整原始 state。agent_view 有意去掉了
敌人意图、双方 buff 层数、遗物/药水效果文本，只留名字或招式 id，导致 LLM 看不到
「敌人要做什么 / 我现在有多少力量 / 这瓶药干什么」。这里只从 raw_state 抽取增量事实，
不改 agent_view（它参与 view_signature 与续步校验）。
"""

from __future__ import annotations

from plugin.plugins.sts2_autoplay.agent_facts import build_agent_facts


def _raw_state(*, player_powers=None, enemies=None, relics=None, potions=None, ascension=None):
    return {
        "combat": {"player": {"powers": player_powers or []}, "enemies": enemies or []},
        "run": {
            "relics": relics or [],
            "potions": potions or [],
            "ascension_effects": ascension or [],
        },
    }


def test_enemy_intent_from_structured_intents():
    # 实盘形状：intents[0]=攻击10，intents[1]=强化
    facts = build_agent_facts(_raw_state(enemies=[{
        "index": 0, "name": "下水道蚌", "is_alive": True,
        "intent": "RAM_MOVE", "move_id": "RAM_MOVE",
        "intents": [
            {"index": 0, "intent_type": "Attack", "label": "10", "damage": 10, "hits": 1, "total_damage": 10},
            {"index": 1, "intent_type": "Buff", "label": None, "damage": None, "hits": None, "total_damage": None},
        ],
        "powers": [{"name": "易伤", "amount": 2, "is_debuff": True}],
    }]))
    enemy = facts["enemies"][0]
    assert enemy["intent"] == "攻击 10 + 强化"
    assert enemy["powers"] == ["易伤2"]


def test_power_runtime_description_keeps_amount_and_display_distinct():
    facts = build_agent_facts(_raw_state(player_powers=[
        {"name": "硬化外壳", "amount": 44, "display_amount": 29,
         "description_source": "runtime_hover_tip", "description": "每回合失去的生命值不会超过44点。"},
        {"name": "未知效果", "amount": 2, "description_source": "missing", "description": ""}]))
    assert facts["player_powers"] == ["硬化外壳44〔显示值29〕（每回合失去的生命值不会超过44点。）",
                                      "未知效果2（当前效果说明缺失）"]


def test_enemy_intent_multi_hit_and_fallback():
    from plugin.plugins.sts2_autoplay.companion_evaluator import _enemy_intent_label

    facts = build_agent_facts(_raw_state(enemies=[
        {"index": 0, "is_alive": True, "intents": [
            {"intent_type": "Attack", "damage": 2, "total_damage": 8, "hits": 4}]},
        # 无结构化 intents：退回招式 id（可读化保留原文）
        {"index": 1, "is_alive": True, "intent": "JET_MOVE"},
        # 哨兵值：不猜
        {"index": 2, "is_alive": True, "intent": "UNSET_MOVE"},
        {"index": 3, "is_alive": True, "intents": [{"intent_type": "Attack", "total_damage": 8, "hits": 3}]},
    ]))
    assert facts["enemies"][0]["intent"] == "攻击 2×4（合计8）"
    assert facts["enemies"][1]["intent"] == "JET_MOVE"
    assert "intent" not in facts["enemies"][2]
    assert facts["enemies"][3]["intent"] == "攻击 合计8（3次）"
    assert _enemy_intent_label({"intents": [{"intent_type": "Attack", "damage": 2, "hits": 4, "total_damage": 8}]}) == "攻击 2×4（合计8）"


def test_dead_enemy_excluded_and_player_powers():
    facts = build_agent_facts(_raw_state(
        player_powers=[{"name": "无惧疼痛", "amount": 3}, {"name": "力量", "amount": 1}],
        enemies=[{"index": 0, "is_alive": False, "intents": [{"intent_type": "Attack", "total_damage": 5}]}],
    ))
    assert facts["player_powers"] == ["无惧疼痛3", "力量1"]
    assert not facts.get("enemies")


def test_orb_evoke_target_preserves_identity_effect_and_values():
    raw = _raw_state()
    raw["combat"]["player"]["orbs"] = [
        {"slot_index": 0, "orb_id": "FROST_ORB", "name": "千问", "is_front": True,
         "passive_value": 2, "evoke_value": 5, "description": "AI：获得格挡。"},
        {"slot_index": 1, "orb_id": "LIGHTNING_ORB", "name": "Claude", "is_front": False,
         "passive_value": 3, "evoke_value": 8, "description": "AI：对随机敌人造成伤害。"},
        {"slot_index": 2, "orb_id": "LIGHTNING_ORB", "name": "Claude", "is_front": False,
         "passive_value": 3, "evoke_value": 8, "description": "AI：对随机敌人造成伤害。"},
    ]
    orbs = build_agent_facts(raw)["orbs"]
    front, = (orb for orb in orbs if orb["is_front"])
    assert (front["slot_index"], front["orb_id"], front["name"]) == (0, "FROST_ORB", "千问")
    assert (front["passive_value"], front["evoke_value"], front["effect"]) == (2, 5, "AI：获得格挡。")
    assert orbs[-1]["name"] == "Claude" and orbs[-1]["evoke_value"] == 8
    assert "伤害" in orbs[-1]["effect"]


def test_orb_capacity_and_empty_slots_are_exposed():
    """球位上限要按现场 orb_capacity，而不是 Defect 默认 3 球位（2026-10-20 01:09 现场）。"""
    raw = _raw_state()
    raw["combat"]["player"].update({
        "orb_capacity": 2, "base_orb_slots": 3, "empty_orb_slots": 0,
        "orbs": [
            {"slot_index": 0, "orb_id": "FROST_ORB", "name": "冰霜", "is_front": True,
             "passive_value": 2, "evoke_value": 5},
            {"slot_index": 1, "orb_id": "LIGHTNING_ORB", "name": "闪电", "is_front": False,
             "passive_value": 3, "evoke_value": 8},
        ],
    })
    facts = build_agent_facts(raw)
    assert facts["orb_capacity"] == 2
    assert facts["base_orb_slots"] == 3
    assert facts["empty_orb_slots"] == 0


def test_relic_and_potion_effects_added():
    facts = build_agent_facts(_raw_state(
        relics=[{"relic_id": "BURNING_BLOOD", "name": "燃烧之血",
                 "description": "在战斗结束时，回复[green]{Heal}[/green]点生命。", "is_melted": False},
                {"name": "熔毁的玩意", "description": "x", "is_melted": True}],
        potions=[{"index": 1, "name": "药水形状的石头", "description": "[blue]{Damage}[/blue]点伤害。",
                  "occupied": True, "can_use": True},
                 {"index": 2, "occupied": False, "name": None}],
    ))
    assert facts["relics"][0] == {"name": "燃烧之血", "relic_id": "BURNING_BLOOD",
                                  "effect": "在战斗结束时，回复（Heal）点生命。", "effect_unresolved": True}
    assert facts["relics"][1].get("melted") is True
    assert facts["potions"] == [{"name": "药水形状的石头", "effect": "（Damage）点伤害。", "effect_unresolved": True, "usable": True,
                                "option_index": 1, "target_index_space": None, "targets": []}]
    raw = _raw_state(potions=[{"index": 2, "name": "格挡药水", "occupied": True, "can_use": True,
                              "target_index_space": "players", "valid_target_indices": [0, 1]}])
    raw["combat"]["players"] = [{"is_local": False}, {"is_local": True}]
    potion = build_agent_facts(raw)["potions"][0]
    assert potion["option_index"] == 2 and potion["target_index_space"] == "players"
    assert [t["role"] for t in potion["targets"]] == ["master", "self"]


def test_candidate_effects_keep_identity_index_and_missing_values():
    raw = _raw_state()
    raw["chest"] = {"relic_options": [
        {"index": 1, "relic_id": "INTIMIDATING_HELMET", "name": "骇人头盔",
         "description": "每当你打出一张耗能大于等于" +
                        "[img]res://images/packed/sprite_fonts/colorless_energy_icon.png[/img]" * 2 +
                        "的牌，获得[blue]4[/blue]点格挡。"},
        {"index": 0, "relic_id": "LANTERN", "name": "灯笼"}]}
    raw["shop"] = {"relics": [{"index": 2, "relic_id": "OTHER", "name": "同名物品",
                              "description": "获得{Block}点格挡。", "price": 150, "enough_gold": False}],
                   "potions": [{"index": 3, "potion_id": "POTION", "name": "同名物品", "description": "抽2张牌。"}]}
    raw["event"] = {"options": [{"index": 4, "relic_preview": raw["chest"]["relic_options"][0]}]}
    choices = build_agent_facts(raw)["choice_effects"]
    helmet = choices["chest_relics"][0]
    assert helmet == {"option_index": 1, "relic_id": "INTIMIDATING_HELMET", "name": "骇人头盔",
                      "effect": "每当你打出一张耗能大于等于2能量的牌，获得4点格挡。"}
    assert choices["chest_relics"][1]["effect_missing"] is True
    assert choices["shop_relics"][0]["effect_unresolved"] is True
    assert choices["shop_relics"][0]["enough_gold"] is False
    assert choices["shop_potions"][0]["potion_id"] == "POTION"
    assert choices["event_relics"][0] == {**helmet, "option_index": 4}
    card = {"kind": "card", "id": "BRIGHTEST_FLAME", "name": "至亮之焰", "description": "造成[blue]10[/blue]伤害。"}
    raw["chest"]["relic_options"][0]["related_effects"] = [card]
    raw["event"]["options"].append({"index": 8, "description": "附魔：{Enchantment}", "related_effects": [
        {"kind": "enchantment", "id": "SOWN", "name": "播种", "description": "获得{Amount}点格挡。"}]})
    choices = build_agent_facts(raw)["choice_effects"]
    assert choices["chest_relics"][0]["related_effects"][0] == {
        "kind": "card", "id": "BRIGHTEST_FLAME", "name": "至亮之焰", "effect": "造成10伤害。"}
    assert choices["event_options"][0]["option_index"] == 8
    assert choices["event_options"][0]["effect_unresolved"]
    assert choices["event_options"][0]["related_effects"][0]["effect_unresolved"]


def test_enemy_current_block_projected_for_armor_comparison():
    """敌方当前格挡要进事实，否则无法按“合计伤害 vs 格挡”判断打不打得穿（覆甲场景）。"""
    facts = build_agent_facts(_raw_state(enemies=[
        {"index": 0, "entity_id": "e100", "name": "下水道蚌", "current_hp": 40, "block": 12,
         "is_alive": True, "intents": [{"intent_type": "Buff"}]}]))
    assert facts["enemies"][0]["block"] == 12


