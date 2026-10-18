# -*- coding: utf-8 -*-
"""跨进程「事实闸门」写入器（插件侧）。

背景：插件在运行期间会把大量游戏内容推给本体 NEKO。这些回合我们希望留在
recent.json（本体能聊当前这局），但**不能**沉淀进 facts.json —— 抽出来的全是
游戏状态（角色名、选牌、血量），跟人设无关，只会在之后污染召回。

插件侧没有 SDK 接口能关掉记忆，本体也无法只凭落盘消息把游戏回合认出来。于是
两边约定：插件在活跃期间持续刷新数据根下的

    <N.E.K.O 数据根>/memory/.fact_suppression.json

本体在 fact 抽取入口读它；文件带 updated_at + TTL，插件崩溃或没在跑时心跳停
止、TTL 自然过期，抽取自动恢复 —— 不需要任何清理步骤，标记也不会卡死。

只抑制**事实抽取**：recent、时间索引、reflection、人设卡都不受影响。

实现说明：心跳跑在守护线程里，不用 asyncio task。插件生命周期钩子不保证在持有
运行中事件循环的线程上执行（实测 create_task 路径拿不到 loop，task 静默不跑），
而这里只是每 20s 写一个小文件 —— 线程是最不依赖宿主调度假设的实现。
"""

from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path
from typing import Any

GATE_FILENAME = ".fact_suppression.json"

# 心跳间隔与宿主 TTL：宿主 TTL(90s) 是几个心跳窗口，偶发丢一拍不会中途失效，
# 而插件真的死了约 1.5 分钟内自动解禁。
REFRESH_SECONDS = 20.0


def _resolve_memory_dir() -> Path | None:
    """解析本体的 memory 目录（与宿主 memory/fact_suppression.py 同一位置）。

    优先用 SDK 的数据根解析（NEKO_STORAGE_SELECTED_ROOT / storage_policy 的
    selected_root），与本体 config_manager.memory_dir = <root>/memory 对齐。
    解析失败返回 None —— 此时闸门不写，等于不抑制（安全默认）。
    """
    root: Path | None = None
    try:
        from plugin.sdk.shared.core.base_runtime import resolve_runtime_data_root
        root = Path(resolve_runtime_data_root())
    except Exception:
        try:
            selected = os.environ.get("NEKO_STORAGE_SELECTED_ROOT", "").strip()
            if selected:
                root = Path(selected)
            else:
                anchor = os.environ.get("NEKO_STORAGE_ANCHOR_ROOT", "").strip()
                if not anchor:
                    base = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA")
                    anchor = os.path.join(base, "N.E.K.O") if base else ""
                if anchor:
                    policy = Path(anchor) / "state" / "storage_policy.json"
                    try:
                        selected = str(json.loads(policy.read_text(encoding="utf-8")).get("selected_root") or "").strip()
                    except Exception:
                        selected = ""
                    root = Path(selected) if selected else Path(anchor)
        except Exception:
            root = None
    if root is None:
        return None
    return root / "memory"


class FactSuppressionGate:
    """把「事实闸门」心跳写到共享数据根，跑在守护线程里。

    Args:
        logger: 插件日志器（可为 None）。
        refresh_seconds: 心跳间隔，默认 20s。
    """

    def __init__(self, logger: Any = None, *, refresh_seconds: float = REFRESH_SECONDS) -> None:
        self._logger = logger
        self._refresh_seconds = max(5.0, float(refresh_seconds or REFRESH_SECONDS))
        self._memory_dir = _resolve_memory_dir()
        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._last_write_ok = False

    @property
    def active(self) -> bool:
        """心跳线程是否在跑。"""
        return self._thread is not None and self._thread.is_alive()

    @property
    def path(self) -> Path | None:
        return None if self._memory_dir is None else self._memory_dir / GATE_FILENAME

    def _log(self, msg: str) -> None:
        if self._logger is None:
            return
        try:
            self._logger.info(msg)
        except Exception:
            try:
                self._logger.debug(msg)
            except Exception:
                pass

    def _write(self, *, suppress: bool) -> bool:
        path = self.path
        if path is None:
            return False
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            payload = {
                "suppress": bool(suppress),
                "source": "sts2_autoplay",
                "reason": "game_plugin_active",
                "pid": os.getpid(),
                "updated_at": time.time(),
            }
            tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
            tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            os.replace(tmp, path)
            self._last_write_ok = True
            return True
        except OSError as exc:
            self._last_write_ok = False
            if self._logger is not None:
                try:
                    self._logger.debug(f"[sts2_fact_gate] write failed: {exc}")
                except Exception:
                    pass
            return False

    def _run(self) -> None:
        while not self._stop_event.wait(self._refresh_seconds):
            self._write(suppress=True)

    def start(self) -> None:
        """启动心跳线程（幂等）。解析不到数据根时静默降级为不抑制。"""
        if self._thread is not None and self._thread.is_alive():
            return
        if self._memory_dir is None:
            self._log("[sts2_fact_gate] 无法解析本体数据根，事实闸门不启用")
            return
        self._stop_event.clear()
        # 立刻写一拍，避免"刚开局还没到首个心跳窗口"这段窗口漏抑制。
        self._write(suppress=True)
        self._thread = threading.Thread(target=self._run, name="sts2-fact-gate", daemon=True)
        self._thread.start()

    def stop(self, *, timeout: float = 2.0) -> None:
        """停止心跳并立即释放闸门（写 suppress=false）。

        写 false 而不是删文件：本体侧的 mtime+size 缓存因此必然失效、立刻读到
        关闭状态，不必等 TTL。TTL 仍是兜底（插件被强杀来不及写 false）。
        """
        self._stop_event.set()
        thread = self._thread
        self._thread = None
        if thread is not None and thread.is_alive():
            thread.join(timeout=timeout)
        if self.path is not None:
            self._write(suppress=False)


__all__ = ["FactSuppressionGate", "GATE_FILENAME", "REFRESH_SECONDS"]
