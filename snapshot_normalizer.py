from __future__ import annotations

from typing import Any, Dict, List


def normalize_actions(payload: Dict[str, Any]) -> List[Dict[str, Any]]:
    actions = payload.get("actions") if isinstance(payload.get("actions"), list) else []
    normalized: List[Dict[str, Any]] = []
    for item in actions:
        if not isinstance(item, dict):
            continue
        action_type = item.get("type") or item.get("action") or item.get("name") or "unknown"
        normalized.append(
            {
                "type": str(action_type),
                "label": str(item.get("label") or item.get("description") or item.get("name") or action_type),
                "raw": item,
            }
        )
    return normalized


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        if value is None:
            return default
        return int(value)
    except (TypeError, ValueError):
        return default


def normalize_snapshot(state_payload: Dict[str, Any], actions_payload: Dict[str, Any]) -> Dict[str, Any]:
    actions = normalize_actions(actions_payload)
    run = state_payload.get("run") if isinstance(state_payload.get("run"), dict) else {}
    view = state_payload.get("agent_view") if isinstance(state_payload.get("agent_view"), dict) else {}
    screen = state_payload.get("screen") or state_payload.get("screen_type") or "unknown"
    game_over = state_payload.get("game_over") or {}
    victory = game_over.get("is_victory")
    outcome = ("victory" if victory else "defeat") if isinstance(victory, bool) else {
        "VICTORY": "victory", "DEFEAT": "defeat"}.get(str(screen).upper())
    potions = run.get("potions") or []
    if potions and all(p.get("occupied") for p in potions):
        blocked = {r["index"] for r in (state_payload.get("reward") or {}).get("rewards") or []
                   if r.get("reward_type") == "Potion"}
        if blocked:
            reward = view.get("reward") or {}
            view = {**view, "reward": {**reward, "rewards": [
                {**r, "claimable": False, "blocked_reason": "potion_slots_full"} if r["i"] in blocked else r
                for r in reward.get("rewards") or []]}}
    return {
        "screen": screen,
        "outcome": outcome,
        # 真实数据 floor/act/character 常在 run 里（run.floor / run.act_id / run.character_id）
        "floor": _safe_int(state_payload.get("floor") or state_payload.get("act_floor") or run.get("floor")),
        "act": _safe_int(state_payload.get("act") or run.get("act") or run.get("act_id")),
        "in_combat": bool(state_payload.get("in_combat", False)),
        "run_id": state_payload.get("run_id"),
        "character": state_payload.get("character") or state_payload.get("character_id") or run.get("character_id") or "",
        "available_actions": actions,
        "available_action_count": len(actions),
        "raw_state": state_payload,
        "raw_actions": actions_payload,
        # Mod 已裁好的单侧视图；决策层只消费它，不从 /coop/state 拼接信息。
        "agent_view": view,
    }


__all__ = ["normalize_actions", "normalize_snapshot"]
