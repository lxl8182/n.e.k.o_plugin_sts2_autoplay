"""拿不到可执行动作时**不许静默空转**。

**实盘证据（2026-10-07）**：mod 认不出的界面（水晶球事件的"占卜"子界面）只暴露 `save_and_quit`，
插件循环每 0.5 秒空转一次、`step_count` 冻在 2395，**整整 2 小时 40 分**，而插件日志里
一句都没有 —— 玩家只能靠自己发现"它不动了"。

三件事必须同时成立：

1. 同一个「卡住指纹」只记**一次** WARNING（带未识别界面类型名，方便精确加 switch 分支）；
2. 写进状态（`stuck_reason`），状态查询/前端看得到；
3. 按「尖塔速报」开关推**一次**给本体；而且**绝不改 autoplay 意图** —— 玩家没喊停就不许自己停。
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from plugin.plugins.sts2_autoplay.service import STS2AutoplayService
from plugin.plugins.sts2_autoplay.planner_interface import PlannedOperation

_STUCK_TYPE = "MegaCrit.Sts2.Core.Nodes.Screens.CrystalSphereScreen"


class _Logger:
    def __init__(self) -> None:
        self.warnings: list[str] = []

    def info(self, *args: Any, **kwargs: Any) -> None: ...
    def error(self, *args: Any, **kwargs: Any) -> None: ...
    def debug(self, *args: Any, **kwargs: Any) -> None: ...

    def warning(self, *args: Any, **kwargs: Any) -> None:
        self.warnings.append(str(args[0]) % tuple(args[1:]) if len(args) > 1 else str(args[0]))


def _service() -> tuple[STS2AutoplayService, _Logger]:
    logger = _Logger()
    return STS2AutoplayService(logger, lambda payload: None, lambda **kwargs: None), logger


def _stuck_snapshot(*, screen: str = "UNKNOWN", unknown: str = _STUCK_TYPE, actions: tuple[str, ...] = ("save_and_quit",)) -> dict[str, Any]:
    return {
        "screen": screen,
        "raw_state": {
            "screen": screen,
            "unknown_screen_type": unknown,
            "available_actions": list(actions),
        },
    }


def _prepare(monkeypatch: pytest.MonkeyPatch, service: STS2AutoplayService) -> dict[str, list[Any]]:
    """把一步自动玩里所有会碰网络/事件循环的东西摁掉，只留"拿不到动作"这条路。"""
    seen: dict[str, list[Any]] = {"host": [], "status": []}

    async def _noop_refresh(**kwargs: Any) -> None:
        return None

    async def _fast_sleep(*args: Any, **kwargs: Any) -> None:
        return None

    service._state.transport_state = "connected"
    monkeypatch.setattr(service, "_should_hold_for_orders", lambda snapshot: False)
    monkeypatch.setattr(service, "_should_rebuild_operation", lambda snapshot: False)
    monkeypatch.setattr(service, "refresh_state", _noop_refresh)
    monkeypatch.setattr(service, "_read_companion_dialogue", _noop_refresh)
    async def _gate(*args: Any, **kwargs: Any) -> dict[str, Any]:
        return {"applied": True, "settled": True}

    async def _plan(*args: Any, **kwargs: Any) -> PlannedOperation | None:
        op = service._test_operation
        return PlannedOperation(op["action_type"], op["kwargs"], 1, "llm") if op else None

    monkeypatch.setattr(service, "_sync_mod_gate", _gate)
    monkeypatch.setattr(service._llm_planner, "plan", _plan)
    monkeypatch.setattr(service, "_emit_status", lambda: seen["status"].append(1))
    monkeypatch.setattr(service, "_push_proactive_to_host", lambda text, **kwargs: seen["host"].append(text))
    monkeypatch.setattr(asyncio, "sleep", _fast_sleep)
    return seen


def _step(service: STS2AutoplayService, snapshot: dict[str, Any], *, force: bool = False) -> dict[str, Any]:
    if "agent_view" not in snapshot:
        snapshot["agent_view"] = {"screen": snapshot.get("screen", ""), "actions": [],
                                  "test_position": snapshot.get("state_signature", snapshot.get("screen", ""))}
    service._test_operation = snapshot.get("agent_operation")
    service._state.snapshot = snapshot
    return asyncio.run(service.run_autoplay_step(force=force))


def test_persistent_stall_is_reported_once(monkeypatch: pytest.MonkeyPatch) -> None:
    """实盘回归：持续卡住必须**报一次**，而且带未识别界面类型名。"""
    service, logger = _service()
    seen = _prepare(monkeypatch, service)
    snapshot = _stuck_snapshot()

    _step(service, snapshot)
    service._state.stuck_since -= 30  # 假装已经等了 30 秒
    _step(service, snapshot)

    assert len(logger.warnings) == 1, logger.warnings
    assert "sts2_stuck" in logger.warnings[0]
    assert "UNKNOWN" in logger.warnings[0]
    assert _STUCK_TYPE in logger.warnings[0], "要报出到底是哪个界面类型认不出来"
    assert "save_and_quit" in logger.warnings[0]
    assert _STUCK_TYPE in service._state.stuck_reason
    assert seen["host"], "要让本体开口问玩家一次"
    assert len(seen["status"]) == 1, "状态要刷一次，前端才看得到"

    _step(service, snapshot)
    _step(service, snapshot)
    assert len(logger.warnings) == 1, "同一处卡住只报一次，别刷屏"
    assert len(seen["host"]) == 1


# ---- 第二道闸：动作发出去了、mod 也回 ok，但局面没变（动作其实没生效） ----------------------


def _moving_snapshot(signature: str = "sig-1", *, screen: str = "EVENT") -> dict[str, Any]:
    return {
        "screen": screen,
        "state_signature": signature,
        "agent_operation": {"action_type": "choose_event_option", "kwargs": {"option_index": 0}},
    }


def _succeed_always(monkeypatch: pytest.MonkeyPatch, service: STS2AutoplayService) -> list[dict[str, Any]]:
    executed = []

    async def _ok(operation: dict[str, Any]) -> dict[str, Any]:
        executed.append(operation)
        return {"status": "ok", "executed": True}

    monkeypatch.setattr(service, "execute_operation", _ok)
    return executed


def test_repeated_action_without_state_change_is_reported(monkeypatch: pytest.MonkeyPatch) -> None:
    """实盘回归：水晶球事件屏上 `choose_event_option` 重复 10 次而局面一字未变（step 83→93）。"""
    service, logger = _service()
    seen = _prepare(monkeypatch, service)
    executed = _succeed_always(monkeypatch, service)
    service._state.autoplay_state = "running"

    for _ in range(service.NO_PROGRESS_REPEAT_LIMIT):
        _step(service, _moving_snapshot(), force=True)

    assert len(executed) == service.NO_PROGRESS_REPEAT_LIMIT
    assert len(logger.warnings) == 1, logger.warnings
    assert "无进展" in logger.warnings[0]
    assert "choose_event_option" in logger.warnings[0]
    assert "choose_event_option" in service._state.stuck_reason
    assert seen["host"], "要让本体开口问玩家一次"
    assert service._state.autoplay_state == "running", "只报告，不自己把自动玩停掉"


