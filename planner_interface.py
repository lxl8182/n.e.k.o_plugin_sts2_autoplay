from __future__ import annotations

from dataclasses import dataclass, field
from collections.abc import Awaitable
from typing import Any, Protocol

# 插件在派发前自己注入的并发保护键（预期回合号 / 玩家标识 / 实体稳定 id）。它们只在
# "把动作发给 Mod"这一步有意义，模型既不该知道、也不该复述。三个边界必须区别对待：
#   * 执行边界（action_engine）：合法，放行；
#   * 模型输出边界（llm_planner）：模型吐出来一律**静默丢弃**——多半是学舌历史里的旧记录，
#     不值当为它拒掉整条动作；
#   * 历史/上下文（companion_session、latest_action_frame）：写入前剥离，否则会被回喂给
#     模型、被学舌、再被输出闸拒掉，形成"总是参数错误"的死循环（见 2026-10-12 现场日志）。
EXECUTION_ONLY_KWARGS = frozenset({
    "expected_turn",
    "expected_player_id",
    "expected_card_entity_id",
    "expected_enemy_entity_id",
})


def public_kwargs(kwargs: Any) -> dict[str, Any]:
    """剥离执行期专用键，得到可安全回喂给模型、可记入历史的 kwargs。"""
    if not isinstance(kwargs, dict):
        return {}
    return {key: value for key, value in kwargs.items() if key not in EXECUTION_ONLY_KWARGS}


@dataclass(slots=True)
class PlannedOperation:
    action_type: str
    kwargs: dict[str, Any]
    confidence: float
    source: str
    reason: str = ""
    action_id: str = ""
    category: str = "unknown"
    candidate_actions: list[str] = field(default_factory=list)
    future_plan: dict[str, Any] | None = None
    continuation: list[dict[str, Any]] = field(default_factory=list)
    speech: str = ""
    speech_scope: str = "steps"
    speech_step_ids: list[int] | None = None
    solver_used: bool = False
    solver_adopted: bool = False
    bindings: dict[str, Any] = field(default_factory=dict)
    assumptions: list[dict[str, Any]] = field(default_factory=list)
    reply: str = ""
    reply_kind: str = "chat"
    plan_update: str = "replace"
    control: str = ""
    media_requests: list[dict[str, str]] = field(default_factory=list)
    skip_zero_cost_cards: dict[str, str] = field(default_factory=dict)
    # 结束回合前未打的剩余可负担牌及逐张理由；旧字段名保留兼容，两者内容一致。
    skip_playable_cards: dict[str, str] = field(default_factory=dict)
    requirement_updates: list[dict[str, Any]] = field(default_factory=list)
    completes_requirements: list[str] = field(default_factory=list)
    learning_updates: list[dict[str, Any]] = field(default_factory=list)
    experience_uses: list[dict[str, Any]] = field(default_factory=list)
    review: dict[str, Any] = field(default_factory=dict)
    model_reason: str = ""
    # 交流诊断：本轮是否真的问了主人、是否带了情绪吐槽（服务层据此做冷却与话题去重）。
    asked: bool = False
    banter_used: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "action_type": self.action_type,
            "kwargs": dict(self.kwargs),
            "confidence": float(self.confidence),
            "source": self.source,
            "reason": self.reason,
            "model_reason": self.model_reason,
            "action_id": self.action_id,
            "category": self.category,
            "candidate_actions": list(self.candidate_actions),
            "future_plan": dict(self.future_plan) if self.future_plan is not None else None,
            "continuation": list(self.continuation),
            "speech": self.speech,
            "speech_scope": self.speech_scope,
            "speech_step_ids": list(self.speech_step_ids) if self.speech_step_ids is not None else None,
            "solver_used": self.solver_used,
            "solver_adopted": self.solver_adopted,
            "bindings": dict(self.bindings),
            "assumptions": list(self.assumptions),
            "reply": self.reply,
            "reply_kind": self.reply_kind,
            "plan_update": self.plan_update,
            "control": self.control,
            "media_requests": list(self.media_requests),
            "skip_zero_cost_cards": dict(self.skip_zero_cost_cards),
            "skip_playable_cards": dict(self.skip_playable_cards),
            "requirement_updates": list(self.requirement_updates),
            "completes_requirements": list(self.completes_requirements),
            "learning_updates": list(self.learning_updates),
            "experience_uses": list(self.experience_uses),
            "review": dict(self.review),
            "asked": self.asked,
            "banter_used": self.banter_used,
        }


class PlannerContext(Protocol):
    def get(self, key: str, default: Any = None) -> Any: ...


class Planner(Protocol):
    def plan(self, context: PlannerContext) -> PlannedOperation | None | Awaitable[PlannedOperation | None]: ...


class NullPlanner:
    def plan(self, context: PlannerContext) -> PlannedOperation | None:
        return None


__all__ = ["PlannedOperation", "PlannerContext", "Planner", "NullPlanner",
           "EXECUTION_ONLY_KWARGS", "public_kwargs"]
