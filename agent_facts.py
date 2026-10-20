"""补齐 ``agent_view`` 被裁掉的决策事实。

Mod 的 ``GET /state`` 同时返回两份视图：给 AI 用的精简 ``agent_view``，以及完整的原始
``state``（插件侧 ``snapshot["raw_state"]``）。精简视图有意去掉了敌人意图、双方 buff 层数、
遗物与药水的效果文本，只留名字/招式 id。这几类正是 LLM 判断「该防还是该抢输出」「这瓶药值不值得喝」
「已有遗物怎么配合」所必需的，缺了就会误判。

这里不改 Mod、也不改 ``agent_view``（它参与 ``view_signature``、续步校验与失效判断，动它会连带
影响缓存命中）。做法只从 ``raw_state`` 读被裁掉的那几类事实，整理成一个独立字段并进决策上下文，
让模型同时看到「现状视图 + 补充事实」。

只输出**增量**：``agent_view`` 已有的（牌面规则文字、血量、金币、牌堆构成）不再重复，避免把
上下文撑大。所有文本做 BBCode/占位符清洗，坏数据一律降级为空，绝不抛异常打断决策。
"""

from __future__ import annotations

import re
from typing import Any

# Mod 的效果文本带 BBCode（``[green]{Heal}[/green]``）与运行期才填的占位符（``{Damage}``）。
_TAG_RE = re.compile(r"\[/?[a-zA-Z][^\]]*\]")
# 旧版 payload 可能保留动态数值占位符。整段删掉会留下"回复点生命"这种
# 缺主语的残句，所以保留占位符名并加括号，让模型知道这里是个待定数值，而不是我们臆造一个数字。
_PLACEHOLDER_RE = re.compile(r"\{([^}]*)\}")

# 敌人意图类型 -> 人话。未知类型保留原文（多半已是可读的英文枚举）。
_INTENT_TYPE_LABELS = {
    "attack": "攻击",
    "deathblow": "致命一击",
    "defend": "防御",
    "block": "防御",
    "buff": "强化",
    "debuff": "弱化",
    "statuscard": "塞牌",
    "sleep": "睡眠",
    "stun": "眩晕",
    "escape": "逃跑",
    "unknown": "意图不明",
}
# 这些是「还没定 / 没数据」的哨兵，别当事实报给模型。
_UNKNOWN_INTENTS = frozenset({"", "unset", "unsetmove", "none", "null", "unknown", "unknownmove"})


def clean_effect_text(text: Any) -> str:
    """去掉 BBCode，并把 ``{X}`` 占位符标成待定数值，返回可读效果文本。失败返回空串。"""
    if not isinstance(text, str):
        return ""
    def icon_text(match: re.Match) -> str:
        icon = match.group(0).rsplit("/", 1)[-1].rsplit(".", 1)[0]
        if icon.endswith("energy_icon"):
            return "能量"
        return {"star_icon": "星星", "gold_icon": "金币", "card_icon": "卡牌",
                "potion_icon": "药水", "chest_icon": "宝箱"}.get(icon, icon.replace("_icon", "").replace("_", " "))

    cleaned = re.sub(r"res://[\w/.-]+\.(?:png|svg|webp)", icon_text, text)
    cleaned = _TAG_RE.sub("", cleaned)
    cleaned = re.sub(r"(?:能量){2,}", lambda m: f"{len(m.group(0)) // 2}能量", cleaned)
    cleaned = _PLACEHOLDER_RE.sub(lambda m: f"（{m.group(1).strip()}）" if m.group(1).strip() else "", cleaned)
    return " ".join(cleaned.split()).strip()


def end_turn_danger(combat: dict[str, Any]) -> str:
    risks = [risk for risk in combat.get("lethal_risks") or [] if risk.get("will_kill_player")]
    # Mod 的致死口径只比"意图总伤害 vs 现有格挡"；结算里的球被动、提前击杀、毒等已由 unmodeled 标出。
    # 有未计入项时不能反推"这回合安全"，也要提醒待结算没有定论。
    open_items = sorted({item for risk in combat.get("lethal_risks") or []
                         for item in risk.get("unmodeled") or [] if isinstance(item, str)})
    qualifier = (f"（本口径未计入：{'、'.join(open_items)}，结算后以实际结果为准）" if open_items else "")
    if not risks and not combat.get("end_turn_will_kill_player"):
        # 没有致死风险就不占用发言位置；未计入项仍留在 payload 里供模型自己核对。
        return ""
    for risk in risks:
        hp, block, incoming = (risk.get(key) for key in ("player_hp", "player_block", "incoming_damage"))
        if all(type(value) in {int, float} for value in (hp, block, incoming)):
            damage = max(0, incoming - max(0, block))
            if damage >= hp:
                return (f"当前{hp:g}血，来伤{incoming:g}，{block:g}格挡后还剩{damage:g}点伤害，已达到致死线，"
                        f"结束回合有致死风险。{qualifier}")
    return f"当前结束回合存在致死风险。{qualifier}"


def _as_int(value: Any) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return int(value)
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def _normalize_token(raw: Any) -> str:
    token = str(raw or "").strip().lower()
    for char in ("_", "-", " "):
        token = token.replace(char, "")
    return token


def _format_intent(intents: Any, fallback: Any = None) -> str:
    """把结构化 ``intents[]`` 说成人话；拿不到就退回招式 id 的可读化写法。

    与 ``companion_evaluator._enemy_intent_label`` 同源思路：优先权威结构化数据，
    哨兵值直接不猜。返回空串表示「不知道，别说」。
    """
    if isinstance(intents, list) and intents:
        labels: list[str] = []
        for item in intents:
            if not isinstance(item, dict):
                continue
            key = _normalize_token(item.get("intent_type"))
            if not key or key in _UNKNOWN_INTENTS:
                continue
            label = _INTENT_TYPE_LABELS.get(key, str(item.get("intent_type") or "").strip())
            total = _as_int(item.get("total_damage"))
            damage = _as_int(item.get("damage"))
            hits = _as_int(item.get("hits"))
            if label in {"攻击", "致命一击"} and total:
                labels.append(f"{label} {damage}×{hits}（合计{total}）" if damage is not None and hits and hits > 1
                              else f"{label} 合计{total}（{hits}次）" if hits and hits > 1 else f"{label} {total}")
            elif label:
                labels.append(label)
        if labels:
            return " + ".join(labels)
        return ""
    raw = str(fallback or "").strip()
    if _normalize_token(raw) in _UNKNOWN_INTENTS:
        return ""
    return raw


def _powers(powers: Any) -> list[str]:
    """``powers[]`` -> ``["力量3", "易伤2（受到的攻击伤害提高 50%）"]``。坏数据跳过。

    效果文本来自 Mod 的 ``description``（游戏权威悬停说明，如易伤 ×1.5）。此前只有"名字+层数"，
    模型拿不到倍率只能猜（易伤是 ×1.5 还是 ×2）——所以描述有就一并带上。
    """
    result: list[str] = []
    if not isinstance(powers, list):
        return result
    for power in powers:
        if not isinstance(power, dict):
            continue
        name = str(power.get("name") or power.get("power_id") or "").strip()
        if not name:
            continue
        amount = _as_int(power.get("amount"))
        label = f"{name}{amount}" if amount is not None else name
        display_amount = _as_int(power.get("display_amount"))
        if display_amount is not None and display_amount != amount:
            label += f"〔显示值{display_amount}〕"
        description = clean_effect_text(power.get("description"))
        if description:
            label = f"{label}（{description}）"
        elif power.get("description_source") == "missing":
            label += "（当前效果说明缺失）"
        result.append(label)
    return result


def _relic_effects(relics: Any, *, id_key: str = "relic_id") -> list[dict[str, Any]]:
    """遗物效果文本。``agent_view`` 只给了名字，这里补 description。"""
    result: list[dict[str, Any]] = []
    if not isinstance(relics, list):
        return result
    for relic in relics:
        if isinstance(relic, dict):
            name = str(relic.get("name") or relic.get(id_key) or "").strip()
            desc = clean_effect_text(relic.get("description"))
        elif isinstance(relic, str):
            name, desc = relic.strip(), ""
        else:
            continue
        if not name:
            continue
        entry = {"name": name}
        if isinstance(relic, dict) and relic.get(id_key):
            entry[id_key] = relic[id_key]
        if desc:
            entry["effect"] = desc
            if isinstance(relic, dict) and "{" in str(relic.get("description") or ""):
                entry["effect_unresolved"] = True
        else:
            entry["effect_missing"] = True
        if isinstance(relic, dict) and relic.get("is_melted"):
            entry["melted"] = True
        if isinstance(relic, dict) and relic.get("related_effects"):
            entry["related_effects"] = [dict(projected, kind=effect["kind"]) for effect in relic["related_effects"]
                                       for projected in _relic_effects([effect], id_key="id")]
        result.append(entry)
    return result


def _potion_info(potions: Any, combat: dict[str, Any]) -> list[dict[str, Any]]:
    """药水效果与可用性。``agent_view`` 只给了名字与使用场景枚举。"""
    result: list[dict[str, Any]] = []
    if not isinstance(potions, list):
        return result
    for potion in potions:
        if not isinstance(potion, dict) or not potion.get("occupied"):
            continue
        name = str(potion.get("name") or potion.get("potion_id") or "").strip()
        if not name:
            continue
        entry = _relic_effects([potion], id_key="potion_id")[0]
        if potion.get("can_use"):
            entry["usable"] = True
        entry["option_index"] = potion["index"]
        space = potion.get("target_index_space")
        entry["target_index_space"] = space
        entry["targets"] = [{"target_index": i,
                             "role": "self" if combat[space][i].get("is_local") else "master" if space == "players" else "enemy",
                             "name": combat[space][i].get("name") or combat[space][i].get("character_name")}
                            for i in potion.get("valid_target_indices") or []]
        result.append(entry)
    return result


def _ascension_effects(effects: Any) -> list[str]:
    result: list[str] = []
    if not isinstance(effects, list):
        return result
    for effect in effects:
        if isinstance(effect, dict):
            name = str(effect.get("name") or "").strip()
            desc = clean_effect_text(effect.get("description"))
            text = f"{name}：{desc}" if name and desc else (name or desc)
        else:
            text = str(effect or "").strip()
        if text:
            result.append(text)
    return result


def _collaboration_facts(raw: dict[str, Any], combat: dict[str, Any]) -> dict[str, Any]:
    """可核对的协作字段；不复制队友手牌或完整游戏对象。"""
    result: dict[str, Any] = {}
    def subject(entity: dict[str, Any]) -> dict[str, Any]:
        data = {key: entity[key] for key in ("entity_id", "player_id", "character_name", "current_hp", "max_hp", "block", "energy") if key in entity}
        if "powers" in entity:
            data["powers"] = [{key: power[key] for key in ("power_id", "name", "amount") if key in power}
                              for power in entity["powers"] if isinstance(power, dict)]
        return data
    if combat.get("player"):
        result["self"] = subject(combat["player"])
    master = next((p for p in combat.get("players") or [] if isinstance(p, dict) and p.get("is_local") is False), None)
    if master is not None:
        result["master"] = subject(master)
    enemies = [subject(e) for e in combat.get("enemies") or [] if isinstance(e, dict) and e.get("entity_id") and e.get("is_alive", True)]
    if enemies:
        result["enemies"] = enemies
    if "action_results" in raw:
        result["action_results"] = [{key: event[key] for key in ("sequence", "actor", "player_id", "action_type", "card_entity_id", "card_id", "target_entity_id", "status", "executed", "kwargs") if key in event}
                                    for event in raw["action_results"] if isinstance(event, dict)]
        result["action_results_epoch"] = raw.get("action_results_epoch")
    return result


def build_agent_facts(raw_state: Any) -> dict[str, Any]:
    """从原始 ``/state`` 抽取 ``agent_view`` 缺失的决策事实。

    返回空 dict 表示当前无需补充（非战斗且没有可补内容，或数据不可用）。
    """
    if not isinstance(raw_state, dict):
        return {}
    combat = raw_state.get("combat") if isinstance(raw_state.get("combat"), dict) else {}
    run = raw_state.get("run") if isinstance(raw_state.get("run"), dict) else {}

    facts: dict[str, Any] = {}
    if run.get("character_id"):
        facts["character_id"] = run["character_id"]
    # act_id 是0起算内部值；幕数明确投影为1起的 act_number，避免"第二幕说成第一层"（Q08）。
    act_id = run.get("act_id")
    if type(act_id) is int:
        facts["act_number"] = act_id + 1
        facts["act_id"] = act_id
    collaboration = _collaboration_facts(raw_state, combat)
    if collaboration:
        facts["collaboration"] = collaboration

    # 自身 buff/减益层数：agent_view 的 combat.player 完全没有 powers。
    player_powers = _powers((combat.get("player") or {}).get("powers") if isinstance(combat.get("player"), dict) else None)
    if player_powers:
        facts["player_powers"] = player_powers

    # 充能球效果：agent_view 只给"名字 被动3/激发8"，没给这个球做什么（如电球=每回合被动打3、激发打8）。
    player_combat = combat.get("player") if isinstance(combat.get("player"), dict) else {}
    orbs_out: list[dict[str, Any]] = []
    for orb in (player_combat.get("orbs") or []):
        if not isinstance(orb, dict):
            continue
        name = str(orb.get("name") or orb.get("orb_id") or "").strip()
        if not name:
            continue
        entry: dict[str, Any] = {"name": name, "orb_id": orb.get("orb_id"),
                                 "slot_index": orb.get("slot_index"), "is_front": orb.get("is_front"),
                                 "passive_value": orb.get("passive_value"), "evoke_value": orb.get("evoke_value")}
        description = clean_effect_text(orb.get("description"))
        if description:
            entry["effect"] = description
        orbs_out.append(entry)
    if orbs_out:
        facts["orbs"] = orbs_out
    # 球位上限：agent_view 只给当前球列表，模型会按 Defect 默认 3 球位猜；
    # 实际容量可能被遗物/能力改小（如 orb_capacity=2），满位再产球会立即激发最右球。
    capacity = player_combat.get("orb_capacity")
    if type(capacity) is int:
        facts["orb_capacity"] = capacity
        empty_slots = player_combat.get("empty_orb_slots")
        facts["empty_orb_slots"] = empty_slots if type(empty_slots) is int else max(0, capacity - len(orbs_out))
    base_slots = player_combat.get("base_orb_slots")
    if type(base_slots) is int:
        facts["base_orb_slots"] = base_slots

    # 敌人：意图 + 自身 buff 层数（agent_view 的 enemies 只有 intent=move_id）。
    enemies_out: list[dict[str, Any]] = []
    for enemy in combat.get("enemies") or []:
        if not isinstance(enemy, dict) or not enemy.get("is_alive", True):
            continue
        entry: dict[str, Any] = {"index": enemy.get("index", enemy.get("i"))}
        if enemy.get("entity_id") is not None:
            entry.update(entity_id=str(enemy["entity_id"]), ref="e" + str(enemy["entity_id"]).rsplit("-", 1)[-1],
                         hp=enemy.get("current_hp"), move_id=enemy.get("move_id") or enemy.get("intent"))
        if enemy.get("max_hp") is not None:
            # 斩杀视野用：30血或30%最大生命（2026-10-17 主人口径）。
            entry["max_hp"] = enemy["max_hp"]
        if enemy.get("block") is not None:
            # 敌方当前格挡（含覆甲在其回合结束获得的格挡）：判断"打不打得穿"要按合计伤害比它，
            # 不能只看覆甲层数，也不能因为单张伤害小于格挡就认为全部白打。
            entry["block"] = enemy["block"]
        if enemy.get("name"):
            entry["name"] = enemy["name"]
        intent = _format_intent(enemy.get("intents"), enemy.get("intent") or enemy.get("move_id"))
        if intent:
            entry["intent"] = intent
        intents = enemy.get("intents") or []
        types = [_normalize_token(item.get("intent_type")) for item in intents]
        if types and all(kind in _INTENT_TYPE_LABELS and kind not in _UNKNOWN_INTENTS for kind in types):
            entry["attacking"] = any(kind in {"attack", "deathblow"} for kind in types)
            totals = [_as_int(item.get("total_damage")) for item in intents
                      if _normalize_token(item.get("intent_type")) in {"attack", "deathblow"}]
            if all(total is not None for total in totals):
                entry["intent_damage"] = sum(totals)
        powers = _powers(enemy.get("powers"))
        if powers:
            entry["powers"] = powers
        enemies_out.append(entry)
    if enemies_out:
        facts["enemies"] = enemies_out
        if all("intent_damage" in enemy for enemy in enemies_out):
            facts["incoming_damage"] = sum(enemy["intent_damage"] for enemy in enemies_out)

    if run.get("deck") is not None:
        composition = {}
        for card in run["deck"]:
            key = (card.get("card_id"), card.get("card_type"))
            composition[key] = composition.get(key, 0) + 1
        facts["deck_composition"] = [{"card_id": key[0], "card_type": key[1], "count": count}
                                      for key, count in composition.items()]

    relics = _relic_effects(run.get("relics"))
    if relics:
        facts["relics"] = relics

    choices = {}
    for name, items, id_key in (
        ("chest_relics", (raw_state.get("chest") or {}).get("relic_options"), "relic_id"),
        ("shop_relics", (raw_state.get("shop") or {}).get("relics"), "relic_id"),
        ("shop_potions", (raw_state.get("shop") or {}).get("potions"), "potion_id"),
        ("event_relics", [{**option["relic_preview"], "index": option["index"]}
                          for option in (raw_state.get("event") or {}).get("options") or []
                          if option.get("relic_preview")], "relic_id"),
    ):
        projected = []
        for item in items or []:
            effects = _relic_effects([item], id_key=id_key)
            if effects:
                projected.append({**effects[0], "option_index": item["index"],
                                  **{key: item[key] for key in ("price", "is_stocked", "enough_gold") if key in item}})
        if projected:
            choices[name] = projected
    if choices:
        facts["choice_effects"] = choices
    event_options = []
    for option in (raw_state.get("event") or {}).get("options") or []:
        entry = {"option_index": option["index"]}
        if re.search(r"\{[^{}]+\}", option.get("description") or ""):
            entry["effect_unresolved"] = True
        if option.get("related_effects"):
            entry["related_effects"] = [dict(projected, kind=effect["kind"]) for effect in option["related_effects"]
                                       for projected in _relic_effects([effect], id_key="id")]
        if len(entry) > 1:
            event_options.append(entry)
    if event_options:
        facts.setdefault("choice_effects", {})["event_options"] = event_options

    potions = _potion_info(run.get("potions"), combat)
    if potions:
        facts["potions"] = potions

    ascension = _ascension_effects(run.get("ascension_effects"))
    if ascension:
        facts["ascension_effects"] = ascension

    return facts


__all__ = ["build_agent_facts", "clean_effect_text"]
