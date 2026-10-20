"""按当前场面按需注入卡牌/能力关键词定义。

关键词表是数据文件（``strategies/common/keywords.json``），只有当前手牌/候选牌/
powers 等文本里实际出现的词条才会进入 ``agent_facts.keyword_effects``；没出现就
完全不发，避免把整份关键词表常驻进每轮 prompt。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

_REGISTRY_PATH = Path(__file__).with_name("strategies") / "common" / "keywords.json"
_CACHE: dict[str, Any] = {"path": None, "stamp": None, "entries": []}


def _load_entries(path: Path) -> list[dict[str, Any]]:
    try:
        stat = path.stat()
        stamp = (stat.st_mtime_ns, stat.st_size)
        if _CACHE["path"] == path and _CACHE["stamp"] == stamp:
            return _CACHE["entries"]
        payload = json.loads(path.read_text(encoding="utf-8"))
        raw = payload.get("keywords") if isinstance(payload, dict) else payload
        entries: list[dict[str, Any]] = []
        for entry in raw or []:
            if not isinstance(entry, dict):
                continue
            names = [str(name).strip() for name in entry.get("names") or [] if str(name).strip()]
            text = str(entry.get("text") or "").strip()
            if names and text:
                entries.append({**entry, "names": names, "text": text})
        _CACHE.update(path=path, stamp=stamp, entries=entries)
        return entries
    except Exception:
        return []


def _texts(items: Any) -> list[str]:
    result: list[str] = []
    for item in items or []:
        if not isinstance(item, dict):
            continue
        for key in ("line", "resolved_rules_text", "name", "description"):
            value = item.get(key)
            if isinstance(value, str) and value:
                result.append(value)
    return result


def _candidate_texts(view: dict[str, Any]) -> list[str]:
    result: list[str] = []
    for section_name in ("selection", "reward", "shop"):
        section = view.get(section_name)
        if not isinstance(section, dict):
            continue
        for field in ("cards", "rewards", "items", "options"):
            result.extend(_texts(section.get(field)))
    return result


def _haystacks(raw_state: dict[str, Any], view: dict[str, Any]) -> dict[str, str]:
    combat = raw_state.get("combat") if isinstance(raw_state.get("combat"), dict) else {}
    run = raw_state.get("run") if isinstance(raw_state.get("run"), dict) else {}
    view_combat = view.get("combat") if isinstance(view.get("combat"), dict) else {}
    player = combat.get("player") if isinstance(combat.get("player"), dict) else {}
    enemies = [enemy for enemy in combat.get("enemies") or [] if isinstance(enemy, dict)]
    return {
        "hand": "\n".join(_texts(view_combat.get("hand")) + _texts(combat.get("hand"))),
        "player_powers": "\n".join(_texts(player.get("powers"))),
        "enemy_powers": "\n".join(text for enemy in enemies for text in _texts(enemy.get("powers"))),
        "relics": "\n".join(_texts(run.get("relics"))),
        "potions": "\n".join(_texts(run.get("potions"))),
        "candidates": "\n".join(_candidate_texts(view)),
    }


def build_keyword_effects(raw_state: Any, view: Any, *, registry_path: Path | None = None) -> dict[str, str]:
    """返回当前场面命中的关键词定义；没有命中时返回空 dict。"""
    if not isinstance(raw_state, dict) or not isinstance(view, dict):
        return {}
    scene = str(view.get("screen") or "").strip().lower()
    if not scene:
        return {}
    entries = _load_entries(registry_path or _REGISTRY_PATH)
    if not entries:
        return {}
    stacks = _haystacks(raw_state, view)
    effects: dict[str, str] = {}
    for entry in entries:
        scenes = [str(item).strip().lower() for item in entry.get("scenes") or []]
        if scenes and scene not in scenes:
            continue
        sources = entry.get("sources") or ["hand"]
        haystack = "\n".join(stacks.get(str(source), "") for source in sources)
        if not haystack:
            continue
        folded = haystack.casefold()
        if not any(name.casefold() in folded for name in entry["names"]):
            continue
        hint = str(entry.get("hint") or "").strip()
        effects[entry["names"][0]] = entry["text"] + (" " + hint if hint else "")
        if len(effects) >= 6:
            break
    return effects


def reset_cache() -> None:
    _CACHE.update(path=None, stamp=None, entries=[])
