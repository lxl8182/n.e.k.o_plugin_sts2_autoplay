"""「听安排」档位（advise）：只算不做，等玩家发话。

三条硬约束都是"会让游戏卡死"的地方，所以单独钉住：

* **战斗不停手** —— 玩家替不了猫娘出牌，停手会把回合永久卡住；
* **主菜单 / 组队大厅 / 选角色不停手** —— 那是猫娘加入房间的必经流程，停手就根本进不了局；
* **认不出的界面不停手** —— 宁可没听成安排，也不能让 mod 意外卡死。

另外 ``hold``（插件的 standby）语义不同：除上述"进局流程"外**全部**停手，**包括战斗**——
回合会停住，这是该档位的预期行为，不是 bug。
"""

from __future__ import annotations

from unittest.mock import AsyncMock

from plugin.plugins.sts2_autoplay.mode_controller import STS2ModeController
from plugin.plugins.sts2_autoplay.planner_interface import PlannedOperation
from plugin.plugins.sts2_autoplay.service import STS2AutoplayService


class DummyLogger:
    def debug(self, *a, **k):
        return None

    def info(self, *a, **k):
        return None

    def warning(self, *a, **k):
        return None

    def exception(self, *a, **k):
        return None


def _service() -> STS2AutoplayService:
    service = STS2AutoplayService(DummyLogger(), lambda p: None, lambda **k: None)
    service.refresh_state = AsyncMock()
    service._read_companion_dialogue = AsyncMock()
    service._sync_mod_gate = AsyncMock(return_value={"applied": True, "settled": True})
    service._llm_planner.plan = AsyncMock(return_value=PlannedOperation("choose_map_node", {"option_index": 1}, 1, "llm"))
    return service


# ---- 档位别名与能力 -----------------------------------------------------------------


def test_advise_still_plans_but_must_not_execute() -> None:
    info = STS2ModeController().describe("advise")
    assert info["advise"] is True
    # 还要算：玩家问"你打算怎么走"得答得出来。
    assert info["allows_planner"] is True
    # 但不许自己动手。
    assert info["allows_execute"] is False


def test_standby_stops_planning_too() -> None:
    info = STS2ModeController().describe("standby")
    assert info["allows_planner"] is False
    assert info["allows_execute"] is False


# ---- 执行闸门 -----------------------------------------------------------------------


def test_switching_mode_interrupts_the_pending_decision() -> None:
    """切档必须打断已算好的那一步，否则切到"听安排"却仍然走了一步。"""
    service = _service()
    service._state.interrupt_requested = False
    service._apply_control_mode("advise")
    assert service._state.interrupt_requested is True
    assert service._state.control_mode == "advise"
