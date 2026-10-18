from __future__ import annotations

import asyncio
import copy

from plugin.plugins.sts2_autoplay.transport_client import STS2TransportClient


def test_describe_error_includes_action_screen_and_code() -> None:
    """mod 端多个动作共用同一句 message，靠 error.details 才能定位是哪个动作挂的。"""
    payload = {
        "ok": False,
        "error": {
            "code": "invalid_action",
            "message": "Action is not available in the current state.",
            "details": {"action": "host_multiplayer_lobby", "screen": "MAIN_MENU"},
        },
    }

    text = STS2TransportClient._describe_error(payload, 409)

    assert "Action is not available in the current state." in text
    assert "action=host_multiplayer_lobby" in text
    assert "screen=MAIN_MENU" in text
    assert "code=invalid_action" in text


def test_legacy_candidates_use_cached_game_catalog_and_keep_live_effects():
    from plugin.plugins.sts2_autoplay.agent_facts import build_agent_facts

    async def run():
        client = STS2TransportClient("http://game")
        state = {"chest": {"relic_options": [{"index": 1, "relic_id": "HELMET", "name": "头盔"}]},
                 "shop": {"relics": [{"index": 0, "relic_id": "OTHER", "name": "头盔"}],
                          "potions": [{"index": 2, "potion_id": "POTION", "name": "药水"}]}}
        calls = []

        async def request(method, path):
            calls.append(path)
            if path == "/state":
                return copy.deepcopy(state)
            if path == "/data/relics":
                return {"value": [{"id": "HELMET", "description": "获得{Block}点格挡。"},
                                  {"id": "OTHER", "description": "抽1张牌。"}]}
            assert path == "/data/potions"
            return {"value": [{"id": "POTION", "description": "回复5点生命。"}]}

        client._request = request
        for _ in range(2):
            observed = await client.get_state()
            choices = build_agent_facts(observed)["choice_effects"]
            assert choices["chest_relics"][0]["effect_unresolved"] is True
            assert choices["shop_relics"][0]["effect"] == "抽1张牌。"
            assert choices["shop_potions"][0]["effect"] == "回复5点生命。"
        state["chest"]["relic_options"][0]["description"] = "现场动态效果9。"
        observed = await client.get_state()
        assert observed["chest"]["relic_options"][0]["description"] == "现场动态效果9。"
        assert calls.count("/data/relics") == calls.count("/data/potions") == 1
        assert "description" not in state["shop"]["relics"][0]

    asyncio.run(run())
