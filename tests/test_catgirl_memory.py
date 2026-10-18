from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import Mock

import pytest

from plugin.plugins.sts2_autoplay.catgirl_memory import CatgirlMemoryStore
from plugin.plugins.sts2_autoplay.service import STS2AutoplayService
from plugin.plugins.sts2_autoplay.snapshot_normalizer import normalize_snapshot


class _DummyLogger:
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


def test_store_add_save_load_roundtrip(tmp_path: Path) -> None:
    path = tmp_path / "catgirl_memory.json"
    store = CatgirlMemoryStore(str(path), logger=_DummyLogger())

    record = store.add_run(
        "r-1",
        character="IRONCLAD",
        outcome="defeat",
        floor=12,
        act=1,
        decision_memory=[{"action_type": "attack", "reason": "翻车", "current_hp": 10, "max_hp": 80}],
    )
    assert record["lessons"] == []
    assert os.path.exists(str(path))

    # 重新实例化（模拟重启）应读到同一批
    store2 = CatgirlMemoryStore(str(path), logger=_DummyLogger())
    assert len(store2.runs) == 1
    assert store2.runs[0]["run_id"] == "r-1"
    assert store2.runs[0]["lessons"] == record["lessons"]


@pytest.mark.parametrize("victory, expected", [(True, "victory"), (False, "defeat"), (None, None)])
def test_game_over_uses_result_flag_and_keeps_victory_hp_out_of_damage(victory, expected):
    service = STS2AutoplayService(Mock(), lambda _: None)
    service._catgirl_memory = CatgirlMemoryStore()
    service._state.run_reflection_enabled = False
    service._ingress_active = True
    service._state.hp_trail_run_id = "run-result"
    service._state.hp_trail = [{"hp": 1, "max_hp": 75, "screen": "EVENT", "room": "ARCHITECT"}]
    snapshot = normalize_snapshot({"screen": "GAME_OVER", "run_id": "run-result",
        "game_over": {"is_victory": victory}, "run": {"current_hp": 0, "max_hp": 75}}, {})
    snapshot["classification"] = {"screen_class": "terminal"}
    service._write_history_view = Mock()
    service._remember_hp_sample(snapshot)
    service._maybe_record_run_memory(snapshot)
    if expected is None:
        assert not service._catgirl_memory.runs
    else:
        record = service._catgirl_memory.runs[0]
        assert record["outcome"] == expected
        assert record["hp"]["final_hp"] == (1 if victory else 0)
        assert record["hp"]["total_lost"] == (0 if victory else 1)


def test_recording_same_run_after_reload_preserves_stats_and_reflection(tmp_path):
    path = tmp_path / "memory.json"
    store = CatgirlMemoryStore(str(path))
    store.add_run("run-1", character="DEFECT", outcome="victory", floor=45, act=2,
        decision_memory=[{"action_type": "play_card", "reason": "先防住来伤"}],
        hp_report={"total_lost": 50, "final_hp": 1}, route=[{"room": "boss"}])
    original = store.attach_reflection("run-1", "一血通关，格挡撑到了最后。")
    restored = CatgirlMemoryStore(str(path))
    replay = restored.add_run("run-1", character="DEFECT", outcome="victory", floor=45, act=2,
        decision_memory=[], hp_report={"total_lost": 0, "final_hp": 0}, route=[])
    assert replay == original
    assert CatgirlMemoryStore(str(path)).runs == [original]


def test_result_correction_removes_wrong_defeat_conclusions_and_keeps_evidence():
    store = CatgirlMemoryStore()
    store.add_run("run-1", character="DEFECT", outcome="defeat", floor=45, act=2,
        decision_memory=[{"action_type": "choose_event_option", "reason": "通关事件继续"}],
        hp_report={"final_hp": 1}, route=[{"room": "ARCHITECT"}])
    previous = store.attach_reflection("run-1", "可能是即死机制。")
    corrected = store.add_run("run-1", character="DEFECT", outcome="victory", floor=45, act=2,
        decision_memory=[], hp_report={}, route=[])
    assert corrected["outcome"] == "victory"
    assert corrected["reflection"] == "" and corrected["lessons"] == []
    for field in ("hp", "route", "decisions"):
        assert corrected[field] == previous[field]


