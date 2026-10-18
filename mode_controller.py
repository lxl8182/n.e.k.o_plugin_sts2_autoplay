from __future__ import annotations

from typing import Any


class STS2ModeController:
    """插件执行权限：program 自主执行，advise 只判断，standby 停手。

    Mod 自主驱动始终由 hold 仲裁；这些权限不抑制 N.E.K.O 本体聊天。
    """

    _ALIASES = {
        "program": "program",
        "full-program": "program",
        "full_program": "program",
        "half-program": "program",
        "half_program": "program",
        "full-model": "program",
        "full_model": "program",
        "model": "program",
        "auto": "program",
        "全程序": "program",
        "半程序": "program",
        "全模型": "program",
        "自己玩": "program",
        "你自己玩": "program",
        "advise": "advise",
        "advise-only": "advise",
        "advise_only": "advise",
        "listen": "advise",
        "听安排": "advise",
        "听我安排": "advise",
        "你听我安排": "advise",
        "等我发话": "advise",
        "standby": "standby",
        "待机": "standby",
        "停手": "standby",
    }

    def __init__(self, mode: str = "program") -> None:
        self._default_mode = self.normalize(mode)

    def normalize(self, mode: Any) -> str:
        raw = str(mode or self._default_mode or "program").strip().lower().replace(" ", "_")
        return self._ALIASES.get(raw, "program")

    def is_standby(self, mode: Any) -> bool:
        return self.normalize(mode) == "standby"

    def is_advise(self, mode: Any) -> bool:
        return self.normalize(mode) == "advise"

    def describe(self, mode: Any) -> dict[str, Any]:
        normalized = self.normalize(mode)
        return {
            "mode": normalized,
            "standby": self.is_standby(normalized),
            "advise": self.is_advise(normalized),
            # advise 仍然要算：玩家问"你打算怎么走"时得答得出来，所以规划器照跑。
            "allows_planner": normalized in {"program", "advise"},
            # 所有屏幕使用相同的插件执行权限。
            "allows_execute": normalized == "program",
            "allows_game_llm": False,
            "prefers_heuristic": False,
            "prefers_model": normalized in {"program", "advise"},
        }


__all__ = ["STS2ModeController"]
