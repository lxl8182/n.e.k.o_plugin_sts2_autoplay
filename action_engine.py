from __future__ import annotations

import logging
from typing import Any

from .planner_interface import PlannedOperation

logger = logging.getLogger(__name__)


class STS2ActionEngine:
    def __init__(self, i18n: Any = None) -> None:
        self._i18n = i18n
        # 最近一次 _normalize_kwargs 是否修正过越界目标（诊断用，见 _repair_combat_target）。
        self._last_target_repair: dict[str, Any] = {}

    def t(self, key: str, *, default: str = "", **params: Any) -> str:
        if self._i18n is not None:
            return self._i18n.t(key, default=default, **params)
        return default.format(**params) if params and default else (default or key)

    def validate(self, snapshot: dict[str, Any], operation: dict[str, Any] | PlannedOperation | None) -> dict[str, Any] | None:
        if operation is None:
            return None
        planned = operation.as_dict() if hasattr(operation, "as_dict") else dict(operation)
        action_type = str(planned.get("action_type") or "").strip()
        if not action_type:
            return None

        registry = snapshot.get("action_registry") if isinstance(snapshot.get("action_registry"), list) else []
        action = self._find_action(registry, snapshot, action_type=action_type, action_id=planned.get("action_id"))
        if action is None:
            return None

        kwargs = planned.get("kwargs") if isinstance(planned.get("kwargs"), dict) else {}
        if planned.get("source") == "llm":
            try:
                self._validate_llm_kwargs(snapshot, action, kwargs)
            except ValueError:
                return None
            self._last_target_repair = {}
            normalized_kwargs = dict(kwargs)
        else:
            normalized_kwargs = self._normalize_kwargs(action, kwargs, snapshot)
        return {
            "action_id": action.get("id") or "",
            "action_type": action_type,
            "category": action.get("category") or "general",
            "kwargs": normalized_kwargs,
            "source": planned.get("source") or "unknown",
            "confidence": planned.get("confidence"),
            "reason": planned.get("reason") or "",
            "candidate_actions": list(planned.get("candidate_actions") or []),
            "action": action,
            "target_repair": dict(self._last_target_repair),
        }

    def validate_with_feedback(self, snapshot: dict[str, Any], operation: dict[str, Any] | PlannedOperation | None) -> tuple[dict[str, Any] | None, str]:
        if operation is None:
            return None, "missing operation"
        planned = operation.as_dict() if hasattr(operation, "as_dict") else dict(operation)
        if planned.get("source") == "llm":
            validated = self.validate(snapshot, operation)
            if validated is not None:
                return validated, ""
            action = self._find_action(snapshot.get("action_registry") or [], snapshot,
                                       action_type=str(planned.get("action_type") or ""), action_id=None)
            if action is None:
                return None, "动作不在当前合法动作中"
            try:
                self._validate_llm_kwargs(snapshot, action, planned.get("kwargs") or {})
            except ValueError as exc:
                return None, str(exc)
            return None, "动作不合法"
        action_type = str(planned.get("action_type") or "").strip()
        if not action_type:
            return None, "missing action_type"
        registry = snapshot.get("action_registry") if isinstance(snapshot.get("action_registry"), list) else []
        action = self._find_action(registry, snapshot, action_type=action_type, action_id=planned.get("action_id"))
        if action is None:
            return None, f"illegal action: {action_type}"
        kwargs = planned.get("kwargs") if isinstance(planned.get("kwargs"), dict) else {}
        normalized_kwargs = self._normalize_kwargs(action, kwargs, snapshot)
        validated = {
            "action_id": action.get("id") or "",
            "action_type": action_type,
            "category": action.get("category") or "general",
            "kwargs": normalized_kwargs,
            "source": planned.get("source") or "unknown",
            "confidence": planned.get("confidence"),
            "reason": planned.get("reason") or "",
            "candidate_actions": list(planned.get("candidate_actions") or []),
            "action": action,
            "target_repair": dict(self._last_target_repair),
        }
        return validated, ""

    def _validate_llm_kwargs(self, snapshot: dict[str, Any], action: dict[str, Any], kwargs: dict[str, Any]) -> None:
        # 模型输出是系统边界。只接受明确的合法参数，不默认选牌、不修复目标。
        allowed = {"card_index", "option_index", "target_index", "player_index", "command",
                   "expected_turn", "expected_player_id", "expected_card_entity_id", "expected_enemy_entity_id"}
        if set(kwargs) - allowed:
            raise ValueError("含不支持的参数")
        for key in ("card_index", "option_index", "target_index", "player_index", "expected_turn"):
            if key in kwargs and (type(kwargs[key]) is not int or kwargs[key] < 0
                                  and not (key == "option_index" and kwargs[key] == -1 and action.get("type") == "resolve_rewards")):
                raise ValueError(f"{key} 必须为非负整数")
        view = snapshot.get("agent_view") or (snapshot.get("raw_state") or {}).get("agent_view") or {}
        kind = action.get("type")
        reward = view.get("reward") or {}
        if kind in {"resolve_rewards", "collect_rewards_and_proceed"} and not (kind == "resolve_rewards" and kwargs.get("option_index") == -1) and (
                reward.get("pending_card_choice") or any(item.get("claimable") for item in reward.get("rewards") or [])):
            raise ValueError("奖励尚未逐项处理，无选牌参数的批量动作会由 Mod 默认选牌；领取用当前合法的 claim_reward，选牌或跳牌只用当前选牌页的合法动作；明确跳过卡牌可用 resolve_rewards，kwargs={\"option_index\":-1}。")
        combat = view.get("combat") or {}
        if kind == "play_card":
            card = next((item for item in combat.get("hand") or [] if item.get("i") == kwargs.get("card_index")), None)
            if not card or not card.get("playable"):
                raise ValueError(f"card_index={kwargs.get('card_index')} 不在当前可打出的 hand 中")
            if card.get("target") and kwargs.get("target_index") not in (card.get("targets") or []):
                raise ValueError(f"该牌 target_index 必须属于 {card.get('targets') or []}")
        # 无 index 也合法的批量/离场动作：Mod 的 resolve_rewards 处理器明确写了
        # "absent = auto (first card)"，"collect_rewards_and_proceed" 更是纯离场。但 Mod 的
        # 动作描述符给 resolve_rewards 标了 requires_index=true（与自身处理器不一致），插件照
        # 描述符执行就会把"无 index 离开奖励屏"这类合法动作误拒。这里对这两个动作豁免
        # requires_index；奖励必须逐项处理的闸仍在上面，不放松任何真实选择。
        index_optional = kind in {"resolve_rewards", "collect_rewards_and_proceed"}
        if action.get("requires_target") and "target_index" not in kwargs:
            raise ValueError(f"{kind} 需要 target_index")
        paths = {
            "choose_map_node": ("map", "options"), "choose_event_option": ("event", "options"),
            "choose_rest_option": ("rest", "options"), "choose_reward_card": ("reward", "cards"),
            "claim_reward": ("reward", "rewards"), "select_deck_card": ("selection", "cards"),
            "select_character": ("character_select", "characters"), "choose_timeline_epoch": ("timeline", "slots"),
            "choose_treasure_relic": ("chest", "relics"),
            "buy_card": ("shop", "cards"), "buy_relic": ("shop", "relics"), "buy_potion": ("shop", "potions"),
            "use_potion": ("run", "potions"), "discard_potion": ("run", "potions"),
        }
        options = ((view.get(paths[kind][0]) or {}).get(paths[kind][1]) or []) if kind in paths else []
        valid_indices = [item.get("i") for item in options if not item.get("locked") and not item.get("selected")]
        if action.get("requires_index") and kind != "play_card" and not index_optional \
                and "option_index" not in kwargs:
            raise ValueError(f'{kind} 需要 option_index；使用kwargs={{"option_index":当前选项下标}}，可选下标={valid_indices}')
        if kind in paths:
            section, field = paths[kind]
            option = next((item for item in (view.get(section) or {}).get(field) or []
                           if item.get("i") == kwargs.get("option_index")), None)
            if not option or option.get("locked"):
                raise ValueError(f"{kind}的option_index={kwargs.get('option_index')} 不在 {section}.{field} 可选下标中或已锁定；"
                                 f'使用kwargs={{"option_index":当前选项下标}}，可选下标={valid_indices}')
            required_flag = {"choose_rest_option": "enabled", "claim_reward": "claimable",
                             "buy_card": "affordable", "buy_relic": "affordable", "buy_potion": "affordable",
                             "use_potion": "usable", "discard_potion": "discard",
                             "choose_timeline_epoch": "actionable"}.get(kind)
            if required_flag and not option.get(required_flag):
                raise ValueError(f"该选项 {required_flag}=false，不可执行")
            if kind in {"buy_card", "buy_relic", "buy_potion"} and option.get("stocked") is False:
                raise ValueError("商品已售出")
            targets = option.get("targets") or option.get("valid_target_indices") or []
            if (option.get("requires_target") or (kind == "use_potion" and targets)) and kwargs.get("target_index") not in targets:
                raise ValueError(f"target_index 必须属于 {targets}")
        if kind == "choose_bundle" and kwargs.get("option_index") not in {item.get("i") for item in view.get("bundles") or []}:
            raise ValueError("option_index 不在 bundles 下标中")

    async def execute(self, client: Any, snapshot: dict[str, Any], operation: dict[str, Any] | PlannedOperation | None) -> dict[str, Any]:
        validated = self.validate(snapshot, operation)
        if validated is None:
            message = self.t("action_engine.no_legal_action", default="当前没有可执行的合法动作。")
            return {
                "status": "idle",
                "message": message,
                "summary": message,
                "executed": False,
            }
        action_request_id = str(operation.get("_action_id") or "") if isinstance(operation, dict) else ""
        result = await client.execute_action(validated["action_type"], action_id=action_request_id,
                                             **validated["kwargs"])
        raw_status = str(result.get("status") or "")
        if raw_status in {"canceled", "cancelled"}:
            status = "canceled"
        elif raw_status in {"rejected", "unknown"}:
            status = raw_status
        elif raw_status == "pending":
            status = "pending"
        else:
            status = "ok"
        message = self.t("action_engine.executed_action", default="已执行动作: {action_type}", action_type=validated["action_type"])
        if status == "pending":
            message = "动作已派发，等待现场确认完成。"
        elif status == "canceled":
            message = "动作被游戏取消，按新现场重新判断。"
        elif status == "rejected":
            message = "动作被拒绝，按新现场重新判断。"
        elif status == "unknown":
            message = "动作结果未知，继续查询现场后判断。"
        return {
            "status": status,
            "message": message,
            "summary": message,
            "executed": status == "ok",
            "dispatched": True,
            "operation": validated,
            "result": result,
            "action_id": action_request_id,
        }

    def _find_action(self, registry: list[dict[str, Any]], snapshot: dict[str, Any], *, action_type: str, action_id: Any) -> dict[str, Any] | None:
        normalized_id = str(action_id or "").strip()
        if normalized_id:
            for action in registry:
                if str(action.get("id") or "") == normalized_id:
                    return action
        target = str(action_type or "").strip().lower()
        for action in registry:
            if str(action.get("type") or "").strip().lower() == target:
                return action

        actions = snapshot.get("available_actions") if isinstance(snapshot.get("available_actions"), list) else []
        for action in actions:
            if not isinstance(action, dict):
                continue
            raw = action.get("raw") if isinstance(action.get("raw"), dict) else {}
            candidate = str(action.get("type") or raw.get("name") or raw.get("action") or "").strip().lower()
            if candidate == target:
                return action
        return None

    def _normalize_kwargs(self, action: dict[str, Any], kwargs: dict[str, Any], snapshot: dict[str, Any] | None = None) -> dict[str, Any]:
        self._last_target_repair = {}
        raw = action.get("raw") if isinstance(action.get("raw"), dict) else {}
        default_kwargs = action.get("default_kwargs") if isinstance(action.get("default_kwargs"), dict) else {}
        action_type = str(action.get("type") or raw.get("name") or raw.get("action") or "").strip().lower()
        normalized = dict(default_kwargs)
        normalized.update(kwargs)
        if action_type in {"choose_event_option", "choose_map_node", "choose_rest_option", "choose_reward_card", "select_deck_card", "claim_reward", "buy_card", "buy_relic", "buy_potion", "use_potion", "discard_potion"}:
            if "option_index" not in normalized and "index" in raw:
                normalized["option_index"] = raw["index"]
        if action_type == "play_card" and "card_index" not in normalized and "card_index" in raw:
            normalized["card_index"] = raw["card_index"]
        if action_type == "play_card":
            target_index = normalized.get("target_index") if "target_index" in normalized else raw.get("target_index")
            if target_index is None:
                fallback_target = self._fallback_target_index(action, snapshot, normalized)
                if fallback_target is not None:
                    normalized["target_index"] = fallback_target
            elif "target_index" not in normalized:
                normalized["target_index"] = target_index
            self._repair_combat_target(action, normalized, snapshot)
        return normalized

    def _hand_card(self, snapshot: dict[str, Any] | None, card_index: Any) -> dict[str, Any]:
        """从**现场**（raw_state.combat.hand）取出即将打出的那张牌。

        目标合法集只有这里最权威：mod 的 /actions/available 对 play_card 只报
        requires_index，具体某张牌能不能指向某个敌人要看 hand[i].valid_target_indices。
        """
        if not isinstance(snapshot, dict):
            return {}
        raw_state = snapshot.get("raw_state") if isinstance(snapshot.get("raw_state"), dict) else {}
        combat = raw_state.get("combat") if isinstance(raw_state.get("combat"), dict) else {}
        hand = combat.get("hand") if isinstance(combat.get("hand"), list) else []
        try:
            wanted = int(card_index)
        except (TypeError, ValueError):
            return {}
        for card in hand:
            if not isinstance(card, dict):
                continue
            try:
                if int(card.get("index")) == wanted:
                    return card
            except (TypeError, ValueError):
                continue
        return {}

    def _valid_target_indices(self, action: dict[str, Any], snapshot: dict[str, Any] | None, kwargs: dict[str, Any]) -> list[int]:
        raw = action.get("raw") if isinstance(action.get("raw"), dict) else {}
        card = raw.get("card") if isinstance(raw.get("card"), dict) else {}
        declared = card.get("valid_target_indices") if isinstance(card.get("valid_target_indices"), list) else []
        if not declared:
            live = self._hand_card(snapshot, kwargs.get("card_index"))
            declared = live.get("valid_target_indices") if isinstance(live.get("valid_target_indices"), list) else []
        indices: list[int] = []
        for item in declared:
            try:
                indices.append(int(item))
            except (TypeError, ValueError):
                continue
        return indices

    def _repair_combat_target(self, action: dict[str, Any], kwargs: dict[str, Any], snapshot: dict[str, Any] | None) -> None:
        """把越界的 target_index 就地改成现场合法值（最后一次兜底）。

        为什么需要这一层：战斗计划是**整回合缓存**的，而目标索引是**绝对下标**。回合内死掉一只敌人
        后，缓存里那一步的 target_index 就可能指向已不存在的位置，mod 会以
        ``invalid_target: target_index is out of range for combat.enemies[]`` 拒收——而一次被拒
        曾经足以让自动玩永久停工（见 service._mark_loop_error）。与其指望上游永远不发旧下标，
        不如在**即将上线的那一刻**用现场合法集校正一次。
        """
        valid = self._valid_target_indices(action, snapshot, kwargs)
        if not valid:
            # 拿不到合法集（旧 mod / 非战斗）就不猜，保持原样。
            return
        current = kwargs.get("target_index")
        if current is None:
            return
        try:
            current_int = int(current)
        except (TypeError, ValueError):
            current_int = None
        if current_int is not None and current_int in valid:
            return
        repaired = valid[0]
        self._last_target_repair = {
            "card_index": kwargs.get("card_index"),
            "from": current,
            "to": repaired,
            "valid": list(valid),
        }
        kwargs["target_index"] = repaired
        logger.info(
            "[sts2_target_repair] 越界目标已按现场合法集修正 card_index=%s %s -> %s (valid=%s)",
            kwargs.get("card_index"),
            current,
            repaired,
            valid,
        )

    def _fallback_target_index(self, action: dict[str, Any], snapshot: dict[str, Any] | None = None, kwargs: dict[str, Any] | None = None) -> int | None:
        raw = action.get("raw") if isinstance(action.get("raw"), dict) else {}
        card = raw.get("card") if isinstance(raw.get("card"), dict) else {}
        valid_target_indices = card.get("valid_target_indices") if isinstance(card.get("valid_target_indices"), list) else []
        if not valid_target_indices:
            valid_target_indices = self._valid_target_indices(action, snapshot, kwargs or {})
        if not valid_target_indices:
            return None
        try:
            return int(valid_target_indices[0])
        except Exception:
            return None


__all__ = ["STS2ActionEngine"]
