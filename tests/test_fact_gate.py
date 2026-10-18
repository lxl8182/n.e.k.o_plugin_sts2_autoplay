"""事实闸门（插件侧）：运行期间抑制本体 fact 抽取。

玩家口径：「你直接在尖塔插件运行的时候所有对话都不写 fact」。

闸门协议：插件活跃时持续刷新数据根下的 memory/.fact_suppression.json 心跳
（带 updated_at + TTL），本体在 fact 抽取入口读它并跳过抽取。这里只测插件侧的
写入/释放与降级行为；本体侧读取语义由 memory/fact_suppression.py 自己保证。
"""

from __future__ import annotations

import json
import time

from plugin.plugins.sts2_autoplay.fact_gate import (
    GATE_FILENAME,
    FactSuppressionGate,
)


class DummyLogger:
    def info(self, *a, **k):
        return None

    def debug(self, *a, **k):
        return None

    def warning(self, *a, **k):
        return None


def _gate(tmp_path, *, refresh=5.0):
    gate = FactSuppressionGate(DummyLogger(), refresh_seconds=refresh)
    gate._memory_dir = tmp_path  # 直接钉住目录，绕开对真实数据根的解析
    return gate


def test_start_writes_suppress_true_immediately(tmp_path) -> None:
    gate = _gate(tmp_path)
    gate.start()
    try:
        payload = json.loads((tmp_path / GATE_FILENAME).read_text(encoding="utf-8"))
        assert payload["suppress"] is True
        assert payload["source"] == "sts2_autoplay"
        assert payload["reason"] == "game_plugin_active"
        assert abs(time.time() - payload["updated_at"]) < 5
        assert gate.active is True
    finally:
        gate.stop()


def test_stop_releases_the_gate_with_suppress_false(tmp_path) -> None:
    """停止时写 false 而不是删文件：本体侧的 mtime+size 缓存因此立刻失效。"""
    gate = _gate(tmp_path)
    gate.start()
    gate.stop()
    payload = json.loads((tmp_path / GATE_FILENAME).read_text(encoding="utf-8"))
    assert payload["suppress"] is False
    assert gate.active is False


