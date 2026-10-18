from __future__ import annotations

from copy import deepcopy
from hashlib import sha1
import json
import re
from typing import Any


def action_refs(snapshot: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    """同一响应的原始实体与精简牌面；下标只供接口派发。"""
    raw = (snapshot.get("raw_state") or {}).get("combat") or {}
    view = (snapshot.get("agent_view") or {}).get("combat") or {}
    result = {"cards": [], "enemies": []}
    for field, prefix, output in (("hand", "c", "cards"), ("enemies", "e", "enemies")):
        for i, item in enumerate(raw.get(field) or []):
            entity = item.get("entity_id")
            if entity is None:
                continue
            index = item.get("index", i)
            face = next((v for j, v in enumerate(view.get(field) or [])
                         if v.get("i", v.get("index", j)) == index), {})
            result[output].append({"ref": prefix + str(entity).rsplit("-", 1)[-1], "entity_id": str(entity),
                                   "index": index, "name": item.get("name") or face.get("name") or "",
                                   "line": face.get("line") or item.get("name") or "",
                                   "hp": item.get("current_hp", face.get("hp")),
                                   "block": item.get("block", face.get("block")),
                                   "alive": item.get("is_alive", True),
                                   "target_space": item.get("target_index_space")})
            if output == "enemies":
                result[output][-1].update(move_id=item.get("move_id") or item.get("intent"),
                    intents=deepcopy(item.get("intents") or []), powers=deepcopy(item.get("powers") or []))
            if output == "cards":
                result[output][-1]["card_id"] = item.get("card_id")
                result[output][-1]["card_type"] = item.get("card_type")
    state = snapshot.get("raw_state") or {}
    selection = state.get("selection") or {}
    selection_view = (snapshot.get("agent_view") or {}).get("selection") or {}
    if selection.get("cards"):
        anchor = {"run_id": (snapshot.get("agent_view") or {}).get("run_id", state.get("run_id")),
                  "screen": (snapshot.get("agent_view") or {}).get("screen", state.get("screen")),
                  "floor": (state.get("run") or {}).get("floor", ((snapshot.get("agent_view") or {}).get("run") or {}).get("floor")),
                  "selection_id": selection.get("selection_id"), "kind": selection.get("kind")}
        result["selection_cards"] = []
        for card in selection["cards"]:
            if not card.get("entity_id"):
                continue
            face = next((c for c in selection_view.get("cards") or [] if c.get("i") == card["index"]), {})
            result["selection_cards"].append({"ref": "selection-" + str(card["entity_id"]),
                "entity_id": str(card["entity_id"]), "index": card["index"], "name": card.get("name") or face.get("line") or "",
                "selected": card.get("selected", False), "anchor": anchor})
    if (state.get("run") or {}).get("potions"):
        result["potions"] = [{"ref": f"potion-{p.get('index', i)}", "entity_id": f"{p.get('index', i)}:{p.get('potion_id', p.get('name'))}",
            "index": p.get("index", i), "name": p.get("name") or p.get("potion_id") or "药水",
            "potion_id": p.get("potion_id"),
            "target_space": p.get("target_index_space")}
            for i, p in enumerate(state["run"]["potions"]) if p.get("occupied", True)]
    rewards = (state.get("reward") or {}).get("rewards") or []
    if rewards:
        result["rewards"] = []
        anchor = {"run_id": state.get("run_id", (snapshot.get("agent_view") or {}).get("run_id")),
                  "floor": (state.get("run") or {}).get("floor"), "screen": state.get("screen")}
        for i, reward in enumerate(rewards):
            identity = {k: v for k, v in reward.items() if k not in {"index", "i", "claimable", "blocked_reason"}}
            fingerprint = sha1(json.dumps(identity, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
            result["rewards"].append({"ref": "reward-" + fingerprint, "entity_id": fingerprint,
                "index": reward.get("index", i), "name": reward.get("description") or reward.get("reward_type") or "奖励",
                "anchor": anchor, "claimable": reward.get("claimable", True)})
    return result


def target_label(target: dict, enemies: list[dict], *, spoken: bool = False) -> str:
    same = [e for e in enemies if e["name"] == target["name"]]
    if len(same) <= 1:
        return target["name"]
    label = (f"第{target['index'] + 1}只" if len(same) > 1 else "") + target["name"]
    if spoken:
        return label
    facts = []
    if target.get("hp") is not None:
        facts.append(f"{target['hp']}血")
    intents = [str(i.get("label") or i.get("intent_type") or "") for i in target.get("intents") or []]
    if any(intents):
        facts.append("当前" + "/".join(intents))
    powers = [f"{p.get('name') or p.get('power_id')} {p.get('amount', p.get('stacks', ''))}" for p in target.get("powers") or []]
    if powers:
        facts.append("已有" + "、".join(powers))
    return label + ("（" + "，".join(facts) + "）" if facts else "")


def bind_action(action_type: str, kwargs: dict[str, Any], refs: dict[str, Any],
                assumptions: list[dict[str, Any]] | None = None) -> tuple[dict, dict, list]:
    """模型边界：引用绑定一次；绝不按牌名替换。"""
    kwargs = dict(kwargs)
    bindings = {}
    if action_type == "select_deck_card":
        cards = refs.get("selection_cards", [])
        valid = [card["index"] for card in cards if not card.get("selected")]
        help_text = f"select_deck_card使用kwargs={{\"option_index\":当前选牌页下标}}，可选下标={valid}；card_ref仅引用action_refs.selection_cards"
        chosen = None
        if "card_ref" in kwargs:
            ref = kwargs.pop("card_ref")
            chosen = next((card for card in cards if card["ref"] == ref), None)
            # 兼容模型把本页i写成字符串引用；只在当前页有真实实体时绑定。
            if chosen is None and isinstance(ref, str) and re.fullmatch(r"(?:i=)?\d+", ref):
                chosen = next((card for card in cards if card["index"] == int(ref.removeprefix("i="))), None)
            if chosen is None:
                raise ValueError(help_text)
        if "card_index" in kwargs:
            index = kwargs.pop("card_index")
            alias = next((card for card in cards if type(index) is int and card["index"] == index), None)
            if alias is None or chosen is not None and alias["entity_id"] != chosen["entity_id"]:
                raise ValueError("选牌参数冲突或无当前实体；" + help_text)
            chosen = alias
        if chosen is not None:
            if "option_index" in kwargs and (type(kwargs["option_index"]) is not int or kwargs["option_index"] != chosen["index"]):
                raise ValueError("选牌参数冲突；" + help_text)
            kwargs["option_index"] = chosen["index"]
        elif cards:
            chosen = next((card for card in cards if type(kwargs.get("option_index")) is int and card["index"] == kwargs["option_index"]), None)
            if chosen is None:
                raise ValueError(help_text)
        if chosen is not None:
            if chosen.get("selected"):
                raise ValueError("目标牌已选中，不重复点击；" + help_text)
            bindings["selection_card"] = deepcopy(chosen)
    if action_type in {"use_potion", "discard_potion"} and refs.get("potions"):
        potion = next((p for p in refs["potions"] if p["index"] == kwargs.get("option_index")), None)
        if potion is None:
            raise ValueError("药水槽位不存在")
        bindings["potion"] = deepcopy(potion)
        if action_type == "use_potion" and potion["target_space"] == "enemies" and "target_ref" not in kwargs:
            target = next((e for e in refs.get("enemies", []) if e["index"] == kwargs.get("target_index")), None)
            if target is None:
                raise ValueError("药水的敌人目标必须使用target_ref")
            kwargs["target_ref"] = target["ref"]
    if action_type == "claim_reward" and refs.get("rewards"):
        reward_ref = kwargs.pop("reward_ref", None)
        reward = next((r for r in refs["rewards"] if r["ref"] == reward_ref), None) if reward_ref is not None else next(
            (r for r in refs["rewards"] if r["index"] == kwargs.get("option_index")), None)
        if reward is None:
            raise ValueError("claim_reward需引用当前奖励的reward_ref或option_index")
        bindings["reward"] = deepcopy(reward)
        kwargs["option_index"] = reward["index"]
    if action_type == "play_card" and refs.get("cards"):
        ref = kwargs.pop("card_ref", None)
        card = next((c for c in refs["cards"] if c["ref"] == ref), None)
        if card is None:
            raise ValueError("play_card 必须使用 action_refs.cards 中的 card_ref")
        bindings["card"] = deepcopy(card)
        kwargs["card_index"] = card["index"]
        if card["target_space"] == "enemies" and "target_ref" not in kwargs:
            target = next((e for e in refs.get("enemies", []) if e["index"] == kwargs.get("target_index")), None)
            if target is None:
                raise ValueError("敌人目标必须使用 target_ref")
            kwargs["target_ref"] = target["ref"]
    target_ref = kwargs.pop("target_ref", None)
    if target_ref is not None:
        target = next((e for e in refs.get("enemies", []) if e["ref"] == target_ref), None)
        if target is None:
            raise ValueError("target_ref 不在 action_refs.enemies 中")
        bindings["target"] = deepcopy(target)
        kwargs["target_index"] = target["index"]
    conditions = deepcopy(assumptions or [])
    if not isinstance(conditions, list) or len(conditions) > 8:
        raise ValueError(f"assumptions 必须是最多8项的条件对象数组；收到{type(conditions).__name__}"
                         + (f"，共{len(conditions)}项" if isinstance(conditions, list) else "")
                         + "。保留有效条件，按kind/ref/数值界限返回；无战术前提才用[]")
    enemy_refs = {e["ref"] for e in refs.get("enemies", [])}
    for condition in conditions:
        if not isinstance(condition, dict):
            raise ValueError("assumptions 条件必须是对象")
        kind = condition.get("kind")
        if kind in {"enemy_hp", "hp_order"}:
            if condition.get("ref") not in enemy_refs:
                raise ValueError("assumptions 引用了未知敌人")
            if kind == "hp_order" and condition.get("order") not in {"lowest", "highest"}:
                raise ValueError("hp_order.order 必须为 lowest/highest")
        elif kind == "hp_spread":
            if not isinstance(condition.get("refs"), list) or len(condition["refs"]) < 2 or any(
                    r not in enemy_refs for r in condition["refs"]):
                raise ValueError("hp_spread.refs 必须引用至少两个现场敌人")
        elif kind != "player":
            raise ValueError("不支持的计划条件")
        bounds = {"enemy_hp": ("min", "max"), "hp_spread": ("min_gap", "max_gap"),
                  "player": ("hp_min", "block_min", "incoming_max")}.get(kind, ())
        if bounds and not any(key in condition for key in bounds):
            raise ValueError("计划条件缺少数值界限")
        if any(type(condition[key]) not in {int, float} or condition[key] < 0
               for key in bounds if key in condition):
            raise ValueError("计划条件界限必须为非负数")
    return kwargs, bindings, conditions


def resolve_action(operation: dict, snapshot: dict) -> tuple[dict | None, dict | None]:
    """每次刷新后按实体重定位，返回可诊断的失败原因。"""
    resolved = deepcopy(operation)
    refs = action_refs(snapshot)
    known = operation.get("known_card_entities")
    if known and any(c["entity_id"] not in known for c in refs["cards"]):
        return None, {"code": "new_hand_information"}
    kwargs = dict(resolved.get("kwargs") or {})
    selection_card = (operation.get("bindings") or {}).get("selection_card")
    if selection_card:
        current = next((card for card in refs.get("selection_cards", [])
                        if card["entity_id"] == selection_card["entity_id"] and card["anchor"] == selection_card["anchor"]), None)
        if current is None or current.get("selected"):
            return None, {"code": "selection_card_changed", "ref": selection_card["ref"]}
        kwargs["option_index"] = current["index"]
    potion = (operation.get("bindings") or {}).get("potion")
    if potion and not any(p["entity_id"] == potion["entity_id"] for p in refs.get("potions", [])):
        return None, {"code": "potion_missing", "ref": potion["ref"]}
    reward = (operation.get("bindings") or {}).get("reward")
    if reward:
        current = next((r for r in refs.get("rewards", []) if r["ref"] == reward["ref"] and r["anchor"] == reward["anchor"]), None)
        if current is None:
            return None, {"code": "reward_missing", "ref": reward["ref"]}
        if not current["claimable"]:
            return None, {"code": "reward_unclaimable", "ref": reward["ref"]}
        kwargs["option_index"] = current["index"]
    for field, output, index_key, expected_key in (
            ("card", "cards", "card_index", "expected_card_entity_id"),
            ("target", "enemies", "target_index", "expected_enemy_entity_id")):
        bound = (operation.get("bindings") or {}).get(field)
        if not bound:
            continue
        current = next((item for item in refs[output] if item["entity_id"] == bound["entity_id"]), None)
        if current is None or current.get("alive") is False:
            return None, {"code": field + "_missing", "ref": bound["ref"], "name": bound["name"]}
        kwargs[index_key] = current["index"]
        kwargs[expected_key] = bound["entity_id"]
    planned_intents = operation.get("enemy_intents")
    if planned_intents is not None:
        current_intents = enemy_intents(snapshot)
        for entity, intent in current_intents.items():
            planned = planned_intents.get(entity)
            # 只比招式与次数：伤害数字会因虚弱/力量重算，属计划内效果（2026-10-17 19:50:06、
            # 20:01:13 等 5 次同一 move_id 仅伤害变化却取消整条计划）。
            if planned is None or (planned.get("move_id") != intent.get("move_id")
                                   or _intent_identity(planned.get("intents")) != _intent_identity(intent.get("intents"))):
                return None, {"code": "enemy_intent_changed", "entity_id": entity,
                              "before": planned, "after": intent}
    enemies = {e["ref"]: e for e in refs["enemies"] if e["alive"]}
    player = ((snapshot.get("raw_state") or {}).get("combat") or {}).get("player") or {}
    for condition in operation.get("assumptions") or []:
        kind = condition["kind"]
        ok = True
        actual = None
        if kind in {"enemy_hp", "hp_order"}:
            enemy = enemies.get(condition["ref"])
            # 被移出活体列表的已绑定敌人视为0血；击杀后转火的前提仍成立。
            actual = enemy.get("hp") if enemy is not None else 0 if kind == "enemy_hp" else None
            ok = actual is not None
            if ok and kind == "enemy_hp":
                ok = condition.get("min", 0) <= actual <= condition.get("max", float("inf"))
            if ok and kind == "hp_order":
                values = [e["hp"] for e in enemies.values() if e["hp"] is not None]
                ok = actual == (min(values) if condition["order"] == "lowest" else max(values))
        elif kind == "hp_spread":
            values = [(enemies.get(r) or {}).get("hp") for r in condition["refs"]]
            ok = all(v is not None for v in values)
            if ok:
                actual = max(values) - min(values)
                ok = condition.get("min_gap", 0) <= actual <= condition.get("max_gap", float("inf"))
        elif kind == "player":
            actual = {"hp_min": player.get("current_hp"), "block_min": player.get("block")}
            actual["incoming_max"] = sum(intent.get("total_damage", 0) or 0
                for e in ((snapshot.get("raw_state") or {}).get("combat") or {}).get("enemies") or []
                if e.get("is_alive", True) for intent in e.get("intents") or [])
            ok = all(actual.get(key) is not None and
                     (actual[key] <= value if key == "incoming_max" else actual[key] >= value)
                     for key, value in condition.items() if key != "kind")
        if not ok:
            return None, {"code": "assumption_changed", "condition": condition, "actual": actual}
    resolved["kwargs"] = kwargs
    return resolved, None


def enemy_intents(snapshot: dict) -> dict:
    """计划时的存活敌人意图；不含血量、位置或随出牌变化的效果层数。"""
    combat = (snapshot.get("raw_state") or {}).get("combat") or {}
    return {str(e["entity_id"]): {"move_id": e.get("move_id") or e.get("intent"),
                                "intents": deepcopy(e.get("intents") or [])}
            for e in combat.get("enemies") or [] if e.get("entity_id") is not None and e.get("is_alive", True)}


_PILE_KEYS = ("draw", "discard", "exhaust", "draw_cards")


def _pile_identity(cards: Any) -> list[dict[str, Any]]:
    """牌堆只比内容与数量：洗牌/抽牌的展示顺序变化不是新事实。

    旧实现直接比较整个视图，牌堆数组顺序一变就整份不等，导致同一计划被反复取消
    （2026-10-17 02:43:55、02:48:42 因 combat.draw/draw_cards 变化取消计划）。
    """
    if not isinstance(cards, list):
        return []
    identity = []
    for card in cards:
        if not isinstance(card, dict):
            identity.append({"value": str(card)[:60]})
            continue
        identity.append({
            "entity_id": str(card.get("entity_id") or card.get("id") or card.get("i") or card.get("index") or ""),
            "line": card.get("line") or card.get("name") or "",
            "upgrade": card.get("upgrade") or card.get("upgrade_level"),
        })
    return sorted(identity, key=lambda item: json.dumps(item, sort_keys=True))


def _intent_identity(intents: Any) -> list[dict[str, Any]]:
    """敌人意图只比招式、次数与附加状态牌数，不比重算后的伤害数值。

    label 本身就是伤害数字（"7"→"5"），连它一起比等于仍在比伤害；自身或队友施加虚弱、
    力量变化后意图伤害会跟着重算，那是计划内效果，不是现场出现新招式。
    招式本身变化看 move_id/intent_type，强度变化另由 powers 反映。
    """
    if not isinstance(intents, list):
        return []
    identity = [{"intent_type": item.get("intent_type"), "hits": item.get("hits"),
                 "status_card_count": item.get("status_card_count")}
                for item in intents if isinstance(item, dict)]
    return sorted(identity, key=lambda item: json.dumps(item, sort_keys=True))


def observation_guard(snapshot: dict) -> dict:
    """决策期间自身资源/界面不得变；敌方削血、位置移动与计划内效果交给计划条件。"""
    view = deepcopy(snapshot.get("agent_view") or {})
    for key in ("actions", "available_actions"):
        if key in view:
            view[key] = sorted(view[key], key=lambda a: json.dumps(a, sort_keys=True))
    if view.get("screen") == "MAP":
        map_view = view.get("map") or {}
        for option in map_view.get("options") or []:
            if "line" in option:
                option["line"] = re.sub(r" \[votes:\d+\]$", "", option["line"])
    if view.get("screen") != "COMBAT":
        return view
    combat = view.get("combat") or {}
    combat.pop("enemies", None)
    for card in combat.get("hand") or []:
        card.pop("targets", None)  # 目标下标空间变化由稳定目标身份和现场合法性检查。
    for key in _PILE_KEYS:
        if key in combat:
            combat[key] = _pile_identity(combat[key])
    raw = (snapshot.get("raw_state") or {}).get("combat") or {}
    view["enemy_effects"] = sorted(
        [{"entity_id": str(e.get("entity_id")), "powers": e.get("powers"),
          "intents": _intent_identity(e.get("intents"))}
         for e in raw.get("enemies") or [] if e.get("is_alive", True)], key=lambda e: e["entity_id"])
    return view
