from __future__ import annotations

from typing import Any

import pytest
from plugin.plugins.sts2_autoplay.service import STS2AutoplayService


class DummyLogger:
    def info(self, *args, **kwargs):
        return None

    def debug(self, *args, **kwargs):
        return None

    def warning(self, *args, **kwargs):
        return None

    def error(self, *args, **kwargs):
        return None

    def exception(self, *args, **kwargs):
        return None


# 真机主菜单：open_multiplayer_menu 与 start_coop_session 都在（外加普通主菜单项）。
MAIN_MENU_ACTIONS: set[str] = {
    "open_character_select",
    "open_timeline",
    "open_multiplayer_menu",
    "start_coop_session",
}
# NMultiplayerSubmenu 打开后，第二步的正式动作才出现。
SUBMENU_OPEN: set[str] = {"start_multiplayer_host", "join_multiplayer_direct"}


class ScriptedClient:
    """按顺序吐出 (screen, 可用动作) 帧，并记录真正发出的动作（含 /config 写入）。

    帧只在**执行动作**时前进（/config 写入不动界面）；帧用完后一直重复最后一帧，
    用来模拟「动作发出去了但界面没变」。
    """

    def __init__(self, *frames: tuple[str, set[str]]) -> None:
        self._frames = list(frames)
        self._actions = 0
        self.calls: list[str] = []

    async def get_available_actions(self) -> dict[str, Any]:
        screen, names = self._frames[min(self._actions, len(self._frames) - 1)]
        return {"screen": screen, "actions": [{"type": name} for name in sorted(names)]}

    async def execute_action(self, action: str, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(action)
        self._actions += 1
        return {}

    async def set_config(self, **fields: Any) -> dict[str, Any]:
        # 选人控制在启动猫娘前交给插件；coop_enabled 仍由 Mod 开房动作设置。
        rendered = ", ".join(f"{key}={value}" for key, value in sorted(fields.items()))
        self.calls.append(f"set_config({rendered})")
        return {"coop_manual_character_selection": fields.get("coop_manual_character_selection")}


def build_service(*frames: tuple[str, set[str]]) -> tuple[STS2AutoplayService, ScriptedClient]:
    service = STS2AutoplayService(DummyLogger(), lambda payload: None)
    client = ScriptedClient(*frames)
    service._client = client  # type: ignore[assignment]

    async def fake_refresh_state(**kwargs: Any) -> dict[str, Any]:
        # 动作之后的状态：两种路径都不是「主菜单」了，交给调用方看的是这份。
        service._state.snapshot = {
            "screen": "CHARACTER_SELECT",
            "available_actions": [{"type": "embark"}, {"type": "select_character"}],
        }
        return {}

    service.refresh_state = fake_refresh_state  # type: ignore[method-assign]
    return service, client


@pytest.mark.asyncio
async def test_open_coop_room_host_delegates_to_mod_session_action() -> None:
    """host 只发一个 start_coop_session，编排（含拉起猫娘进程）归 mod。

    回归：这里曾经自己拼 open_multiplayer_menu + host_multiplayer_lobby（调试测试场景那套，
    跟在 open_multiplayer_menu 后面必然 409），后来又自己拼 open_multiplayer_menu +
    start_multiplayer_host —— 但那样拉不起猫娘进程，因为 LaunchCatgirlProcess 只暴露在
    mod 的 start_coop_session 后面。
    """
    service, client = build_service(("MAIN_MENU", MAIN_MENU_ACTIONS))

    result = await service.open_coop_room(host=True)

    assert client.calls == ["set_config(coop_manual_character_selection=True)", "start_coop_session"]
    assert result["status"] == "opened"
    assert result["action"] == "start_coop_session"
    assert result["host"] is True


@pytest.mark.asyncio
async def test_open_coop_room_client_uses_direct_connect() -> None:
    """加入方没有猫娘要拉，仍自己走 菜单 -> 直连。"""
    service, client = build_service(("MAIN_MENU", MAIN_MENU_ACTIONS), ("MAIN_MENU", SUBMENU_OPEN))

    result = await service.open_coop_room(host=False)

    assert client.calls == ["open_multiplayer_menu", "join_multiplayer_direct"]
    assert result["action"] == "join_multiplayer_direct"
    assert result["host"] is False


