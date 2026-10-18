from __future__ import annotations

import re

import httpx


_SOLVER_TERMS = re.compile(r"求解|解算|战斗搜索|搜索算法|solver|combat[_ ]?search|beam[_ ]?search|plan_source", re.IGNORECASE)


def public_companion_text(text: str) -> str:
    if not _SOLVER_TERMS.search(text):
        return text
    sentences = re.split(r"(?<=[。！？!?；;\n])", text)
    return "".join(s for s in sentences if not _SOLVER_TERMS.search(s)).strip() or "我再看看当前局面。"


async def speak_current_turn(port: int, name: str, text: str, anchor: dict) -> dict:
    """复用宿主 TTS，等待客户端实际播放完成而不是消息提交回执。"""
    text = public_companion_text(text)
    origin = f"http://127.0.0.1:{port}"
    async with httpx.AsyncClient(timeout=70.0, trust_env=False) as client:
        config = await client.get(origin + "/api/config/page_config")
        config.raise_for_status()
        response = await client.post(
            origin + "/api/game/sts2/speak",
            headers={"Origin": origin, "X-CSRF-Token": config.json()["autostart_csrf_token"]},
            json={"line": text, "lanlan_name": name, "wait_for_audio_completion": True,
                  "mirror_text": True, "event": anchor},
        )
        response.raise_for_status()
        result = response.json()
        if not result.get("ok") or result.get("audio_completed") is not True:
            raise RuntimeError("回合发言未确认播放完成，暂不派发动作：" + str(result.get("reason") or result.get("audio_completed")))
        return result


async def apply_existing_expression(port: int, text: str) -> dict:
    """把实际游戏上下文交给宿主配置的 emotion 模型，复用客户端表情映射。"""
    origin = f"http://127.0.0.1:{port}"
    async with httpx.AsyncClient(timeout=35.0, trust_env=False) as client:
        character = await client.get(origin + "/api/characters/current_catgirl")
        character.raise_for_status()
        name = character.json()["current_catgirl"]
        config = await client.get(origin + "/api/config/page_config")
        config.raise_for_status()
        response = await client.post(
            origin + "/api/emotion/analysis",
            headers={"Origin": origin, "X-CSRF-Token": config.json()["autostart_csrf_token"]},
            json={"text": text, "lanlan_name": name},
        )
        response.raise_for_status()
        result = response.json()
        if "error" in result:
            raise RuntimeError(result["error"])
        return {"character": name, "emotion": result["emotion"], "confidence": result["confidence"]}
