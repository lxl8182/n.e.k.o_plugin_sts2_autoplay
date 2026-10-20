# -*- coding: utf-8 -*-
"""猫娘跨局战报、复盘评价和可修正的条件经验。

战报保存整局紧凑过程；反思文字独立保存。统一模型同次输出的结构化经验
带来源、条件和版本，按当前局面匹配少量条目，并关联真实动作及回合结果。
已有旧战报和启发式教训继续可读，新过程不从动作频次推断玩家偏好。
"""

from __future__ import annotations

import json
import os
import shutil
from datetime import datetime
import uuid
from time import time
from typing import Any
from copy import deepcopy
from uuid import uuid4
from .strategy_repository import SCENES, strategy_scene

# 跨局记忆条数上限（避免无限膨胀）
_RUN_CAP = 50

#: 兜底教训文本（"没有任何可执行信息"的那一条）。
#: ★实测它是**最高频**的教训（41/53 = 77.36%），且 `_lesson_tokens()` 对它返回 `[]`
#: ⇒ 它既挤占注入窗口，又无法影响任何决策。集中成常量，便于识别与治理。
_FALLBACK_LESSON = "这局输得不明不白，下次决策慢一点、多留容错"

#: ★R5：真实 `decisions[].reason` 全是**英文标签**（660 条 / 25 种，中文关键词命中 0/660）。
#: 这张表把"英文标签"翻译成**可执行的行为教训**。
#: 键是标签前缀/全称的小写形式，值是对应的教训文本（含中文行为词，供 `_lesson_tokens` 抽取）。
_REASON_TO_LESSON: tuple[tuple[tuple[str, ...], str], ...] = (
    (("mod_solver_end_turn",),
     "这局有好几回合打不出更优解只能直接结束回合，下次每回合先垫够格挡再输出"),
    (("combat_preference_or_default",),
     "这局战斗里好几步都只能走默认兜底，下次别光着急出攻击牌，先垫格挡再输出"),
    (("map_preference_or_default",),
     "这局选路线时只能按默认偏好走，下次提前绕开精英、少走危险的房间"),
    (("reward_",),
     "这局选牌奖励拿得比较随意，下次优先拿格挡牌、别贪输出牌"),
    (("shop_",),
     "这局商店花钱没什么章法，下次攒钱买关键的，别乱买"),
)


def _is_mistake_reason(reason: str) -> bool:
    """该 reason 是否代表一次"值得复盘的具体失误"。"""
    text = str(reason or "").strip().lower()
    if not text:
        return False
    return any(text.startswith(prefix) for prefix, _lesson in _REASON_TO_LESSON)


def _lesson_for_reason(reason: str) -> str:
    """把英文 reason 标签映射成一条**可执行**的教训文本。"""
    text = str(reason or "").strip().lower()
    for prefixes, lesson in _REASON_TO_LESSON:
        if any(text.startswith(prefix) for prefix in prefixes):
            return lesson
    return _FALLBACK_LESSON


#: ★I1 作用域推断：根据教训文本里的行为词判断它该作用在哪一屏。
#: 顺序敏感 —— 先问最具体的（战斗行为），再问路线，最后才是 general。
#: 这与 `heuristic_planner._LESSON_KEYWORD_MAP` 的 token 体系是**两套独立**的语义：
#: 这张表只用于**标注**（可观测性 / 度量），不参与降权决策，避免与 token 体系互相干扰。
_DOMAIN_HINTS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("combat", ("格挡", "垫格挡", "输出", "攻击牌", "回合", "叠甲", "保命", "斩杀", "抢血")),
    ("map", ("路线", "绕开", "精英", "房间", "休息", "篝火", "商店")),
    ("reward", ("拿牌", "选牌", "奖励", "能力牌", "卡牌")),
    ("event", ("事件", "选项", "生命上限", "最大生命")),
)


def _infer_lesson_domain(text: str, declared: str = "") -> str:
    """推断一条教训的作用域（combat / map / reward / event / general）。"""
    if declared and declared != "general":
        return declared
    lowered = str(text or "")
    for domain, hints in _DOMAIN_HINTS:
        if any(hint in lowered for hint in hints):
            return domain
    return "general"



def _default_memory_path() -> str | None:
    """解析插件数据目录下的记忆文件路径。

    优先用环境变量（NEKO 插件数据根），回退到 LOCALAPPDATA/N.E.K.O 下的插件私有子目录。
    返回 None 表示环境无法确定（此时 store 退化为仅内存、不落盘）。
    """
    base = os.environ.get("NEKO_PLUGIN_DATA_DIR") or ""
    if base:
        return os.path.join(base, "sts2_autoplay", "catgirl_memory.json")
    try:
        local = os.environ.get("LOCALAPPDATA") or ""
        if local:
            return os.path.join(local, "N.E.K.O", "sts2_autoplay", "catgirl_memory.json")
    except Exception:
        pass
    return None


class CatgirlMemoryStore:
    """猫娘跨局记忆 store：落盘读写 + 启发式/LLM 总结 + 跨局聚合。

    Args:
        path: 记忆文件路径（默认从环境解析；None 时仅内存，不落盘）。
        engine: "heuristic" | "llm"（llm 需要传入可用 `llm` 实例）。
        logger: 用于告警日志。
    """

    def __init__(self, path: str | None = None, *, engine: str = "heuristic", logger: Any = None) -> None:
        # path=None -> 纯内存（不落盘、不读盘），用于测试；生产由调用方显式传 _default_memory_path()。
        self._path = path
        self._engine = engine
        self._logger = logger
        self._runs: list[dict[str, Any]] = []
        self.experiences: list[dict] = []
        self.learning_events: list[dict] = []
        self.applied_updates: list[str] = []
        self.last_error = ""
        self.load_failed = False
        self.load()

    @property
    def path(self) -> str | None:
        return self._path

    @property
    def runs(self) -> list[dict[str, Any]]:
        return self._runs

    def load(self) -> None:
        """从磁盘读入最近记忆；**损坏时抢救式解析，绝不静默清空**。

        读取失败时保留原文件并置 ``load_failed``；在显式 ``recover()`` 之前，
        ``save()`` 不会用这份空状态覆盖原文件。

        ★t16 抢救式解析（上游）：正常 ``json.load`` 失败（尾随残片等）时，用
        ``JSONDecoder().raw_decode()`` 取**第一个完整对象**，抢救出其中的数据并显式
        记告警日志（含抢救出的局数与丢弃的残留字符数）。**绝不清空。**
        连第一个完整对象都取不到（完全非 JSON / 空文件）才回落空，且必须告警。

        向前兼容：文件不存在 / 空文件 / 完全非 JSON，均不抛异常（见 t16 D4）。
        """
        self._runs = []
        self.load_failed = False
        if not self._path or not os.path.exists(self._path):
            return
        try:
            with open(self._path, "r", encoding="utf-8") as f:
                raw = f.read()
        except OSError:
            self.load_failed = True
            self.last_error = f"经验读取失败：{self._path}"
            self._log(f"catgirl_memory load failed (IO): {self._path}")
            return
        if not raw.strip():
            return

        # --- 分支 1：正常解析 ---
        try:
            data = json.loads(raw)
            self._adopt_runs(data)
            return
        except ValueError:
            pass

        # --- 分支 2：抢救式解析（取第一个完整对象）★不得清空 ---
        try:
            data, end = json.JSONDecoder().raw_decode(raw)
        except ValueError:
            # --- 分支 3：连第一个对象都取不到 ⇒ 回落空，但**置 load_failed 阻止覆盖** ---
            self.load_failed = True
            self.last_error = f"经验读取失败：{self._path}"
            self._log(
                f"catgirl_memory load: unparsable, starting empty: {self._path} "
                f"(bytes={len(raw)})"
            )
            return
        self._adopt_runs(data)
        trailing = len(raw) - end
        self._log(
            f"catgirl_memory load: RECOVERED partial file {self._path} "
            f"(salvaged_runs={len(self._runs)} trailing_residue_chars={trailing})"
        )

    def _adopt_runs(self, data: Any) -> None:
        """把解析出的文档里的 runs 与学习状态收进来。"""
        records = data.get("runs") if isinstance(data, dict) else None
        if isinstance(records, list):
            self._runs = [r for r in records if isinstance(r, dict)]
            self._runs = self._runs[-_RUN_CAP:]
        if isinstance(data, dict):
            self.experiences = data.get("experiences", [])
            self.learning_events = data.get("learning_events", [])
            self.applied_updates = data.get("applied_updates", [])

    def save(self) -> bool:
        """把当前记忆写回磁盘（幂等；path 为 None 时静默跳过）。

        读取失败且尚未 ``recover()`` 时拒绝覆盖原文件。返回是否真正落盘成功。

        ★t16 加固（上游）：唯一 tmp 名（含 pid 与 uuid4）避免两个写入者共用 tmp；
        写入后 ``flush()`` + ``os.fsync()`` 再 ``os.replace``。
        """
        if not self._path:
            return False
        if self.load_failed:
            self.last_error = f"经验读取失败，已阻止覆盖：{self._path}"
            self._log("catgirl_memory save blocked after failed load")
            return False
        tmp = "%s.%d.%s.tmp" % (self._path, os.getpid(), uuid.uuid4().hex[:8])
        try:
            directory = os.path.dirname(self._path)
            if directory:
                os.makedirs(directory, exist_ok=True)
            payload = {"runs": self._runs[-_RUN_CAP:], "updated_at": time(),
                       "experiences": self.experiences, "learning_events": self.learning_events,
                       "applied_updates": self.applied_updates}
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False, indent=2)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, self._path)
            self.last_error = ""
            return True
        except (OSError, TypeError, ValueError):
            self.last_error = f"经验保存失败：{self._path}"
            self._log(f"catgirl_memory save failed: {self._path}")
            try:
                if os.path.exists(tmp):
                    os.remove(tmp)
            except OSError:
                pass
            return False

    def recover(self) -> bool:
        """备份读取失败的原文件并清除 ``load_failed``，之后 ``save()`` 恢复正常。

        备份文件名为 ``<path>.corrupt-<yyyyMMdd-HHmmss>``，原文件保留。
        没有读取失败时直接返回 True。
        """
        if not self.load_failed:
            return True
        if self._path:
            try:
                stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
                backup = f"{self._path}.corrupt-{stamp}"
                if os.path.exists(self._path):
                    shutil.copy2(self._path, backup)
            except OSError:
                self.last_error = f"经验备份失败：{self._path}"
                self._log(f"catgirl_memory recover backup failed: {self._path}")
                return False
        self.load_failed = False
        self.last_error = ""
        return True

    def add_run(self, run_id: str, *, character: str, outcome: str, floor: int, act: int,
                decision_memory: list[dict[str, Any]], llm: Any = None,
                hp_report: dict[str, Any] | None = None,
                route: list[dict[str, Any]] | None = None, process: dict | None = None,
                deck: list[dict[str, Any]] | None = None,
                extra_lessons: list[str] | None = None) -> dict[str, Any]:
        """总结一局并追加到记忆；同 run_id 保留已有记录，结果纠正时清除旧结论。

        ``llm`` 参数保留但**这里不再使用**：本方法必须是同步、确定、立刻落盘的（terminal 屏会反复
        出现，记录不能等模型）。自然语言的**反思**改由 service 事后异步生成，再经
        ``attach_reflection`` 贴回同一条记录。

        旧代码在这里同步调用 ``llm.generate(...)`` —— 那是个 ``async`` 方法，拿到的是协程，
        随后 ``.splitlines()`` 直接抛错被 ``except`` 吞掉；而且还有 ``self._engine == "llm"`` 这道
        默认关闭的闸门。**那条 LLM 总结路径从来没有生效过**，一直是纯启发式。

        ``deck``（2026-10-19 新增）：死亡那一刻的**完整牌组快照**（mod 的
        `DeckCardPayload`，带 card_type / upgraded / energy_cost / dynamic_values /
        resolved_rules_text）。它是「六维缺口」教训的唯一数据来源 ——
        没有它就只能反思"死因"，有了它才能反思"**牌组缺哪一维**"。

        ``extra_lessons``（2026-10-19 新增）：由**调用方**（service）算好的额外教训。
        为什么由调用方给、而不是在这里算：六维量化要用
        `STS2HeuristicPlanner._deck_gap_vector`，而 planner 实例住在 service
        （`service.py:99`），memory store 拿不到它。
        ⚠️ 刻意**不**在这里 new 一个 planner：那会形成第二套判据来源，
           与"决策与复盘共用同一把尺"的设计冲突。
        """
        existing = next((run for run in self._runs if run.get("run_id") == str(run_id or "")), None)
        if existing is not None:
            if existing.get("outcome") != outcome:
                existing["outcome"] = outcome
                existing["reflection"] = ""
                existing["lessons"] = []
                self.save()
            return dict(existing)
        run = {
            "run_id": str(run_id or ""),
            "character": str(character or ""),
            "ended_at": time(),
            "outcome": str(outcome or ""),
            "floor": int(floor or 0),
            "act": int(act or 0),
            "lessons": [],
            "preferences": [],
            "hp": dict(hp_report or {}),
            "reflection": "",
            "process": deepcopy(process or {}),
            # 逐房间的路线图：每个房间掉了多少血、拿到什么（供历史页展示与复盘参考）。
            "route": [dict(item) for item in (route or []) if isinstance(item, dict)],
            # ★ 局末牌组快照：供「六维缺口」反思量化牌组用。
            #   只留量化需要的字段，避免把整个 deck 塞进记忆文件（50 局 × 30 张太大）。
            "deck": [self._slim_deck_card(item) for item in (deck or []) if isinstance(item, dict)],
            # 本局的关键决策（截断保留），供历史页展示 + 局末复盘当上下文。
            "decisions": [
                {
                    "screen": str(item.get("screen") or ""),
                    "action_type": str(item.get("action_type") or ""),
                    "reason": str(item.get("decision_reason") or item.get("reason") or "")[:80],
                    "step_count": item.get("step_count"),
                    "battle_id": item.get("battle_id"), "turn": item.get("turn"),
                    "delta": dict(item.get("delta") or {}),
                }
                for item in (decision_memory or [])[-60:]
                if isinstance(item, dict)
            ],
        }
        run["extra_lessons"] = [
            str(text).strip()
            for text in (extra_lessons or [])
            if isinstance(text, (str, int)) and str(text).strip()
        ]
        summary = {"lessons": [], "preferences": []} if process and process.get("turns") else self.summarize_run(
            decision_memory=decision_memory,
            run_meta=run,
        )
        run["lessons"] = summary.get("lessons", [])
        run["preferences"] = summary.get("preferences", [])
        # ★I1：同时落盘结构化记录（新键）。老键 `lessons` 保留不动 ⇒ 老读者零影响。
        run["lesson_records"] = [
            self.build_lesson_record(str(text), source="heuristic")
            for text in run["lessons"]
            if isinstance(text, (str, int)) and str(text).strip()
        ]
        # 同 run_id 覆盖（去重），再追加
        self._runs = [r for r in self._runs if r.get("run_id") != run.get("run_id")]
        self._runs.append(run)
        self._runs = self._runs[-_RUN_CAP:]
        self.save()
        return dict(run)

    # ---- 总结 ----

    #: ★I1 结构化教训 schema 的字段与默认值。
    #: 设计目标：**向前兼容** —— 老记忆文件里 `lessons` 只是纯字符串列表，
    #: 读进来时按这里补默认值即可，**零迁移、零报错**（真实 705KB 文件 44 条记录已实测）。
    LESSON_RECORD_DEFAULTS: dict[str, Any] = {
        "text": "",           # 原始自由文本（保留原文，永不再生成）
        "domain": "general",  # 作用域：combat / map / reward / event / general
        "trigger": {},        # 触发条件（如 {"floor_min": 24}）
        "action": {},         # 建议动作（如 {"prefer": "block_first"}）
        "strength": 1.0,      # 强度 [0,1]，供后续加权
        "evidence": {},       # 证据（哪一局哪一步，取自 hp.heaviest / decisions）
        "confidence": 1.0,    # 置信度 [0,1]
        "hit_count": 0,       # 命中次数（由消费侧累加上报）
        "source": "heuristic",  # heuristic / llm
    }

    @classmethod
    def build_lesson_record(cls, text: str, **overrides: Any) -> dict[str, Any]:
        """把一条自由文本教训包装成结构化记录（缺字段给默认）。"""
        record = dict(cls.LESSON_RECORD_DEFAULTS)
        record["text"] = str(text or "").strip()
        for key, value in overrides.items():
            if key in cls.LESSON_RECORD_DEFAULTS:
                record[key] = value
        record["domain"] = _infer_lesson_domain(record["text"], str(record.get("domain") or ""))
        return record

    @classmethod
    def normalize_lesson_record(cls, item: Any) -> dict[str, Any]:
        """把「老格式（纯字符串）或新格式（dict）」统一成结构化记录。

        ★这是向前兼容的**唯一**入口：任何读记忆的地方都该走它，
        这样老文件（`lessons: ["..."]`）与新文件（`lesson_records: [{...}]`）都能用。
        """
        if isinstance(item, dict):
            record = dict(cls.LESSON_RECORD_DEFAULTS)
            for key in cls.LESSON_RECORD_DEFAULTS:
                if key in item:
                    record[key] = item[key]
            record["text"] = str(record.get("text") or "").strip()
            if not record["text"]:
                return {}
            record["domain"] = _infer_lesson_domain(record["text"], str(record.get("domain") or ""))
            return record
        text = str(item or "").strip() if isinstance(item, (str, int)) else ""
        if not text:
            return {}
        return cls.build_lesson_record(text)

    def lesson_records(self, runs: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
        """把若干局里的教训读成结构化记录（老格式自动升级）。

        优先读 `lesson_records`（新键），同时并入 `lessons`（老键）——
        两者同时存在时**合并**，保证新写入的记录不会让老记录消失。
        """
        source_runs = self._runs if runs is None else runs
        records: list[dict[str, Any]] = []
        seen: set[str] = set()
        for run in source_runs:
            if not isinstance(run, dict):
                continue
            structured = run.get("lesson_records") if isinstance(run.get("lesson_records"), list) else []
            legacy = run.get("lessons") if isinstance(run.get("lessons"), list) else []
            for item in [*structured, *legacy]:
                record = self.normalize_lesson_record(item)
                if not record:
                    continue
                if record["text"] in seen:
                    continue
                seen.add(record["text"])
                records.append(record)
        return records

    def summarize_run(self, *, decision_memory: list[dict[str, Any]], run_meta: dict[str, Any],
                      llm: Any = None) -> dict[str, Any]:
        """保存兼容局报摘要；可复用经验统一通过 learning_updates 保存。"""
        lessons: list[str] = []
        preferences = self._heuristic_preferences(decision_memory)
        lessons, preferences = self._dedup(lessons), self._dedup(preferences)
        if llm is not None and self._llm_available(llm):
            llm_out = self._summarize_with_llm(llm, decision_memory, run_meta)
            if llm_out:
                lessons = self._dedup(lessons + llm_out.get("lessons", []))
                preferences = self._dedup(preferences + llm_out.get("preferences", []))
        return {"lessons": lessons, "preferences": preferences}

    def attach_reflection(self, run_id: str, reflection: str, *, as_lesson: bool = False) -> dict[str, Any] | None:
        """保存局末评价；默认与长期经验分开，旧调用可显式请求附入lessons。"""
        text = str(reflection or "").strip()
        if not text:
            return None
        for run in self._runs:
            if str(run.get("run_id") or "") != str(run_id or ""):
                continue
            run["reflection"] = text
            if as_lesson:
                lessons = run.get("lessons") if isinstance(run.get("lessons"), list) else []
                if text not in lessons:
                    lessons.append(text)
                run["lessons"] = lessons
                # ★I1：LLM 反思同样要进结构化层（source=llm），否则它永远读不出 domain/触发条件。
                records = run.get("lesson_records") if isinstance(run.get("lesson_records"), list) else []
                if not any(
                    isinstance(record, dict) and str(record.get("text") or "") == text for record in records
                ):
                    records.append(self.build_lesson_record(text, source="llm"))
                run["lesson_records"] = records
            self.save()
            return dict(run)
        return None

    def summarize_recent(self, limit: int = 5) -> dict[str, list[str]]:
        """聚合最近 N 局的教训/偏好（去重），供注入 strategy_directives。

        ★**闸2（注入窗口）**：此前"最近 N 局"是按**局数**截断的，而真实记忆里连续 41 局
        的 lessons 全是同一条兜底模板句（`_heuristic_lessons` 的 defeat 兜底）。
        于是 `limit=5` 只能捞到那一条**抽不出 token** 的句子 —— 10 条真实 LLM 反思
        （能抽出 `skill`/`attack` 等 token）全部被锁在窗外。

        实测拐点（`.dsh-artifacts/reflection-metrics-baseline.json` → `runtime.gate2_window_sweep`）：
        `limit=5→1 条`、`10→1`、`20→7`、`44→12`。

        修法：**先按局数取窗口，再补足"有信息量"的教训**（默认开启，见 `include_older_lessons`）。
        这样既不改变 `limit` 的语义（调用方仍可说"看最近 5 局"），
        又不会让高频模板句把仅有的几条真教训挤掉。补足是**追加**，不重排、不去重以外的改写。
        """
        return self.summarize_recent_detailed(limit=limit)["summary"]

    #: 兜底模板句的**特征子串**。用于识别"这一条不携带任何可执行信息"。
    #: 必须是子串匹配（不是等值），因为历史记录里可能带标点差异。
    _FALLBACK_LESSON_MARKERS: tuple[str, ...] = ("输得不明不白",)

    @classmethod
    def _is_fallback_lesson(cls, text: str) -> bool:
        """这条教训是不是那条"没有信息量"的兜底模板句。"""
        return any(marker in str(text) for marker in cls._FALLBACK_LESSON_MARKERS)

    def summarize_recent_detailed(
        self,
        limit: int = 5,
        *,
        include_older_lessons: bool = True,
        max_older_lessons: int = 12,
    ) -> dict[str, Any]:
        """`summarize_recent` 的详细版：额外给出窗口诊断信息（供度量脚手架与日志用）。

        返回 ``{"summary": {...}, "window": {...}}``，其中 `summary` 与 `summarize_recent`
        的返回值**完全一致**（同样的键、同样的顺序语义）。

        Args:
            include_older_lessons: 是否用"窗口外的、非兜底的"教训补足窗口。
                关掉它可精确复现改动前的行为（t10 基线口径）。
        """
        window_runs = self._runs[-limit:] if limit > 0 else []
        lessons: list[str] = []
        preferences: list[str] = []
        for run in window_runs:
            lessons.extend(str(item) for item in run.get("lessons", []) if isinstance(item, (str, int)))
            preferences.extend(str(item) for item in run.get("preferences", []) if isinstance(item, (str, int)))
        lessons, preferences = self._dedup(lessons), self._dedup(preferences)

        window_lessons = list(lessons)
        added: list[str] = []
        if include_older_lessons:
            older_runs = self._runs[: len(self._runs) - len(window_runs)]
            for run in reversed(older_runs):  # 由新到旧
                if len(added) >= max_older_lessons:
                    break
                for item in run.get("lessons", []):
                    if not isinstance(item, (str, int)):
                        continue
                    text = str(item).strip()
                    if not text or self._is_fallback_lesson(text):
                        continue
                    if text in lessons or text in added:
                        continue
                    added.append(text)
                    lessons.append(text)
                    if len(added) >= max_older_lessons:
                        break

        return {
            "summary": {"lessons": lessons, "preferences": preferences},
            "window": {
                "limit": limit,
                "runs_in_window": len(window_runs),
                "window_lessons": window_lessons,
                "window_lesson_count": len(window_lessons),
                "window_all_fallback": bool(window_lessons) and all(
                    self._is_fallback_lesson(item) for item in window_lessons
                ),
                "older_lessons_added": added,
                "older_lessons_added_count": len(added),
                "total_lessons": len(lessons),
            },
        }

    def pending_review(self) -> dict:
        for run in reversed(self._runs):
            draft = run.get("review_discussion") or {}
            if draft.get("status") == "awaiting_feedback":
                return {"run_id": run["run_id"], **deepcopy(draft)}
        return {}

    def stage_review(self, review: dict, reply: str, journal: Any) -> None:
        run = next(r for r in self._runs if r["run_id"] == journal.data["run_id"])
        run["review_discussion"] = {"status": "awaiting_feedback", "turn_id": review["turn_id"],
                                    "assessment": review["assessment"], "question": review["question"], "reply": reply}
        self.save()

    def finish_review(self, review: dict, messages: list[dict]) -> None:
        run = next(r for r in self._runs if r["run_id"] == review["run_id"])
        run["review_discussion"].update(status="discussed", assessment=review["assessment"],
                                         feedback=[{"id": m["id"], "content": m["content"]} for m in messages])
        self.attach_reflection(run["run_id"], review["assessment"])

    def decision_context(self, view: dict[str, Any], facts: dict | None = None, *, include_history: bool = False) -> dict[str, Any]:
        """保留教训的来源与实际依据；不把启发式或反思提升为当前事实。"""
        character = (view.get("run") or {}).get("character")
        candidates = [run for run in self._runs if run.get("run_id") != view.get("run_id")]
        candidates.sort(key=lambda run: (run.get("character") == character, run.get("ended_at", 0)), reverse=True)
        return {"interpretation": "经验按场景和条件匹配，候选与旧反思只留在讨论/历史中；当前事实和有效要求优先，明确偏好保留。",
                "experiences": self.relevant_experiences(view, facts or {}, detail=include_history),
                "runs": [{"run_id": run.get("run_id"), "character": run.get("character"),
                          "outcome": run.get("outcome"), "floor": run.get("floor"),
                          "evidence": {"hp": {key: (run.get("hp") or {}).get(key)
                                               for key in ("total_lost", "min_hp", "final_hp", "loss_events")},
                                       "decisions": [{"screen": item.get("screen"), "action_type": item.get("action_type"),
                                                      "reason": str(item.get("reason") or "")[:80],
                                                      "battle_id": item.get("battle_id"), "turn": item.get("turn"),
                                                      "delta": json.dumps(item.get("delta") or {}, ensure_ascii=False)[:1200]}
                                                     for item in (run.get("decisions") or [])[-5:]]}}
                         for run in candidates[:3] if include_history]}

    def relevant_experiences(self, view: dict, facts: dict, *, detail: bool = False) -> list[dict]:
        combat, run = view.get("combat") or {}, view.get("run") or {}
        def tokens(items):
            return {str(v).casefold() for item in items if isinstance(item, dict)
                    for v in item.values() if isinstance(v, (str, int))}
        pools = {"cards": tokens(combat.get("hand") or []),
                 "enemies": tokens(combat.get("enemies") or []) | tokens(facts.get("enemies") or []),
                 "relics": tokens(run.get("relics") or []) | tokens(facts.get("relics") or []),
                 "actions": {str(a if isinstance(a, str) else a.get("name")) for a in view.get("actions") or []}}
        matched = []
        for item in self.experiences:
            if item["status"] != "active" or item["scope"] != "persistent" or item["kind"] == "model_candidate":
                continue
            c = item["conditions"]
            if c.get("scenes") and strategy_scene(view) not in c["scenes"]:
                continue
            if c.get("selection_kinds") and (view.get("selection") or {}).get("kind") not in c["selection_kinds"]:
                continue
            deck = facts.get("deck_composition")
            if deck is None and run.get("deck") is not None and all("card_type" in card for card in run["deck"]):
                deck = run["deck"]
            rules = c.get("deck_card_counts") or []
            if rules and (deck is None or any(not rule.get("min", 0) <= sum(
                    card.get("count", 1) * int((not rule.get("card_type") or card.get("card_type") == rule["card_type"]) and
                    (not rule.get("card_id") or card.get("card_id") == rule["card_id"]))
                    for card in deck) <= rule.get("max", float("inf")) for rule in rules)):
                continue
            if c.get("character") and c["character"] not in {run.get("character"), run.get("character_id"), facts.get("character_id")}:
                continue
            if any(c.get(k) and not any(str(v).casefold() in pools[k] or
                   any(str(v).casefold() in token for token in pools[k]) for v in c[k]) for k in pools):
                continue
            player = dict(combat.get("player") or {})
            hp = (facts.get("collaboration") or {}).get("self", {}).get("current_hp")
            if hp is None:
                hp = player.get("current_hp", player.get("hp", run.get("current_hp", run.get("hp"))))
            if isinstance(hp, str):
                try:
                    hp = float(hp.split("/", 1)[0])
                except ValueError:
                    hp = None
            player["current_hp"] = hp
            if any(k in c and (not isinstance(player.get(field), (int, float)) or player[field] > c[k])
                   for k, field in (("hp_lte", "current_hp"), ("energy_lte", "energy"))):
                continue
            matched.append(item)
        matched.sort(key=lambda x: (x["kind"] == "player_correction",
                                   sum(k not in {"character", "scenes"} for k in x["conditions"]),
                                   bool(x["conditions"].get("scenes")), x["updated_at"]), reverse=True)
        selected = matched[:3]
        preference = next((item for item in matched if item["kind"] == "preference"), None)
        if preference is not None and preference not in selected:
            selected[-1] = preference
        if self._logger is not None:
            self._logger.info("[sts2_experience_match] scene=%s matched=%s selected=%s", strategy_scene(view),
                              len(matched), [(e["id"], e["version"]) for e in selected])
        result = []
        for item in selected:
            entry = {k: deepcopy(item[k]) for k in ("id", "version", "text", "kind", "conditions", "status")}
            source = item["source"]
            if not detail:
                entry["source"] = {k: source.get(k) for k in ("run_id", "turn_id", "action_id", "request_id") if source.get(k)}
                result.append(entry)
                continue
            evidence = (source.get("case") or {}).get("record") or {}
            entry["source"] = {k: deepcopy(v) for k, v in source.items() if k != "case"}
            entry["source"]["evidence"] = {k: deepcopy(evidence.get(k)) for k in ("before", "after")}
            entry["source"]["actions"] = [{k: deepcopy(a.get(k)) for k in ("id", "action_type", "reason", "status", "card", "target")}
                for a in evidence.get("actions", []) if not source.get("action_id") or a["id"] == source["action_id"]][-5:]
            entry["adoption_evidence"] = [deepcopy(event) for event in self.learning_events
                                          if event["kind"] == "use" and event["experience_id"] == item["id"]][-3:]
            result.append(entry)
        return result

    def experience_catalog(self, messages: list[dict]) -> list[dict]:
        text = " ".join(m["content"] for m in messages).casefold()
        pairs = {text[i:i + 2] for i in range(len(text) - 1)}
        ranked = sorted(self.experiences, key=lambda e: (sum(p in e["text"].casefold() for p in pairs), e["updated_at"]), reverse=True)
        return [{k: deepcopy(e[k]) for k in ("id", "version", "text", "kind", "scope", "conditions", "status")}
                for e in ranked[:12]]

    def validate_learning(self, updates: list, uses: list, review: dict, *, messages: list,
                          journal: Any, available: list, terminal: bool, step_count: int) -> None:
        if not isinstance(updates, list) or len(updates) > 4:
            raise ValueError("learning_updates最多4条")
        sources = {m["id"]: m for m in messages}
        existing = {e["id"]: e for e in self.experiences}
        turns = {t["id"]: t for t in journal.data.get("turns", [])}
        if terminal and not messages and updates:
            raise ValueError("局末先讨论复盘并询问玩家意见，收到本轮玩家反馈后再写入经验；learning_updates填[]")
        if review.get("kind") == "run_feedback":
            pending = self.pending_review()
            feedback_ids = review.get("feedback_message_ids")
            if (not messages or review.get("run_id") != pending.get("run_id")
                    or review.get("turn_id") not in turns
                    or not isinstance(feedback_ids, list) or not feedback_ids
                    or any(not isinstance(mid, str) or mid not in sources for mid in feedback_ids)):
                raise ValueError("复盘讨论须引用pending_run_review的run_id、已有turn_id及本轮反馈消息id")
        for update in updates:
            if not isinstance(update, dict) or update.get("op") not in {"add", "replace", "revoke"}:
                raise ValueError("learning_updates使用add/replace/revoke")
            if update["op"] != "add" and update.get("id") not in existing:
                raise ValueError("修改或撤销需引用已有经验id")
            source = update.get("source_message_id")
            if source not in sources and not (terminal and update.get("kind") == "model_candidate" and update["op"] == "add"):
                raise ValueError("经验更新需引用本轮玩家消息；局末仅允许新增model_candidate")
            if update["op"] == "revoke":
                continue
            if update.get("kind") not in {"player_correction", "preference", "model_candidate"}:
                raise ValueError("经验类型为player_correction/preference/model_candidate")
            if not isinstance(update.get("text"), str) or not 1 <= len(update["text"].strip()) <= 800:
                raise ValueError("经验text需1到800字")
            if update.get("scope") not in {"case", "persistent"}:
                raise ValueError("经验scope为case或persistent，局内要求不写长期经验")
            conditions = update.get("conditions")
            allowed = {"character", "scenes", "selection_kinds", "cards", "enemies", "relics", "actions", "hp_lte", "energy_lte", "deck_card_counts"}
            if not isinstance(conditions, dict) or set(conditions) - allowed:
                raise ValueError("经验conditions使用character/scenes/selection_kinds/cards/enemies/relics/actions/hp_lte/energy_lte/deck_card_counts")
            for k, v in conditions.items():
                if k == "scenes" and (not isinstance(v, list) or not v or any(not isinstance(s, str) or s not in SCENES for s in v)):
                    raise ValueError("scenes使用combat/event/map/reward/remove/exhaust/upgrade/shop/rest/chest/selection")
                if k == "deck_card_counts":
                    if not isinstance(v, list) or not 1 <= len(v) <= 4 or any(
                        not isinstance(rule, dict) or set(rule) - {"card_type", "card_id", "min", "max"}
                        or not (rule.get("card_type") or rule.get("card_id"))
                        or not any(bound in rule for bound in ("min", "max"))
                        or any(type(rule[b]) is not int or rule[b] < 0 for b in ("min", "max") if b in rule)
                        for rule in v):
                        raise ValueError("deck_card_counts需按card_type/card_id给非负min/max，最多4项")
                if k in {"cards", "enemies", "relics", "actions", "selection_kinds"} and (not isinstance(v, list) or not v or len(v) > 8 or any(not isinstance(s, str) or not s.strip() or len(s) > 120 for s in v)):
                    raise ValueError("经验匹配列表需1到8个非空字符串")
                if k == "character" and (not isinstance(v, str) or not v.strip()):
                    raise ValueError("经验角色需非空字符串")
                if k in {"hp_lte", "energy_lte"} and (type(v) not in {int, float} or v < 0):
                    raise ValueError("经验资源阈值需非负数")
            if update["scope"] == "persistent" and update["kind"] != "preference" and not conditions:
                raise ValueError("长期打法经验需填写适用条件")
            turn_id = update.get("turn_id")
            if turn_id not in turns and update.get("kind") != "preference":
                raise ValueError("纠正或模型经验需关联已记录turn_id")
            if update.get("action_id") and (turn_id not in turns or not any(a["id"] == update["action_id"] for a in turns[turn_id]["actions"])):
                raise ValueError("经验action_id需属于来源回合")
        exposed = {e["id"]: e for e in available}
        if not isinstance(uses, list) or len(uses) > 3:
            raise ValueError("experience_uses最多3条")
        for use in uses:
            if not isinstance(use, dict) or use.get("id") not in exposed or use.get("version") != exposed[use["id"]]["version"]:
                raise ValueError("采用经验需引用本轮提供的id与version")
            if any(u.get("id") == use["id"] and u["op"] in {"replace", "revoke"} for u in updates):
                raise ValueError("本轮修改/撤销的经验不再按旧版本采用")
            if not isinstance(use.get("reason"), str) or not 1 <= len(use["reason"]) <= 400:
                raise ValueError("采用经验需简短reason")
            if not isinstance(use.get("steps"), list) or any(type(i) is not int or not 0 <= i < step_count for i in use["steps"]):
                raise ValueError("experience_uses.steps需引用保留的实际步骤序号，无动作填[]")
        if review:
            if not isinstance(review, dict) or review.get("turn_id") not in turns:
                raise ValueError("review需引用已有turn_id")
            run_end_review = terminal and review.get("kind") == "run_end"
            if (not messages and not run_end_review) or not isinstance(review.get("assessment"), str) or not 1 <= len(review["assessment"]) <= 1200:
                raise ValueError("review仅回应本轮玩家，assessment需1到1200字")
            if run_end_review and review.get("accepted") is True:
                raise ValueError("局末模型复盘不能标记采纳了玩家纠正")
            if review.get("action_id") and not any(a["id"] == review["action_id"] for a in turns[review["turn_id"]]["actions"]):
                raise ValueError("review.action_id需属于该回合")
            if review.get("accepted") is True and not any(u.get("kind") == "player_correction" and
                    u.get("turn_id") == review["turn_id"] for u in updates):
                raise ValueError("已采纳的回顾纠正需同次learning_updates保存原回合case或条件经验")

    def apply_learning(self, updates: list, *, messages: list, journal: Any) -> None:
        sources = {m["id"]: m for m in messages}
        for index, update in enumerate(updates):
            source = sources.get(update.get("source_message_id"), {})
            origin = source.get("request_id") or (f"{source.get('run_id')}:{source.get('id')}:{source.get('timestamp')}"
                                                   if source else f"{journal.data.get('run_id')}:terminal")
            key = f"{origin}:{index}:{json.dumps(update, ensure_ascii=False, sort_keys=True)}"
            if key in self.applied_updates:
                continue
            existing = next((e for e in self.experiences if e["id"] == update.get("id")), None)
            case = journal.keep_case(update["turn_id"], action_id=update.get("action_id", ""), reason="learning") if update.get("turn_id") else None
            if existing:
                existing["history"].append({k: deepcopy(v) for k, v in existing.items() if k != "history"})
                existing["version"] += 1
            else:
                existing = {"id": str(uuid4()), "version": 1, "history": []}
                self.experiences.append(existing)
            existing.update(status="revoked" if update["op"] == "revoke" else "active", updated_at=time())
            if update["op"] != "revoke":
                existing.update({k: deepcopy(update[k]) for k in ("text", "kind", "scope", "conditions")})
                existing["source"] = {"run_id": journal.data.get("run_id") if case else source.get("run_id"), "turn_id": update.get("turn_id"),
                    "action_id": update.get("action_id"), "case": deepcopy(case),
                    "message": source.get("content", ""), "request_id": source.get("request_id") or source.get("id")}
            self.applied_updates.append(key)
        if updates:
            self.save()
            if self.last_error:
                raise RuntimeError(self.last_error)

    def record_exposure(self, experiences: list, run_id: str, turn_id: str) -> None:
        for entry in experiences:
            self.learning_events.append({"kind": "read", "experience_id": entry["id"], "version": entry["version"],
                                         "run_id": run_id, "turn_id": turn_id, "timestamp": time()})
        if experiences:
            self.save()

    def record_experience_result(self, action: dict) -> None:
        for use in action.get("experience_uses") or []:
            event = next((e for e in self.learning_events if e["kind"] == "use" and e.get("action_id") == action["id"] and e["experience_id"] == use["id"]), None)
            if event is None:
                event = {"kind": "use", "experience_id": use["id"], "version": use["version"],
                         "run_id": action.get("run_id"), "turn": action.get("turn"), "battle_id": action.get("battle_id"),
                         "action_id": action["id"], "reason": use["reason"]}
                self.learning_events.append(event)
            event.update(status=action["status"], executed=action["executed"], outcome=deepcopy(action.get("outcome")))
        if action.get("experience_uses"):
            self.save()

    def learning_report(self) -> dict:
        return {"experiences": [{"id": e["id"], "version": e["version"], "text": e["text"],
                 "kind": e["kind"], "status": e["status"], "conditions": e["conditions"],
                 "reads": sum(x["kind"] == "read" and x["experience_id"] == e["id"] for x in self.learning_events),
                 "applicable_turns": len({(x.get("run_id"), x.get("turn_id")) for x in self.learning_events
                                          if x["kind"] == "read" and x["experience_id"] == e["id"]}),
                 "adoptions": sum(x["kind"] == "use" and x["experience_id"] == e["id"] for x in self.learning_events),
                 "completed": sum(x["kind"] == "use" and x["experience_id"] == e["id"] and x.get("executed", False) for x in self.learning_events),
                 "source": {k: v for k, v in e["source"].items() if k != "case"}}
                for e in self.experiences], "events": deepcopy(self.learning_events[-30:]),
                "storage_error": self.last_error,
                "interpretation": "applicable_turns为发生决策且匹配的回合数，reads为提供给模型次数，adoptions为关联动作，completed为实际完成；打法改善结合来源案例和结果比较。"}

    def record_turn_result(self, turn: dict) -> None:
        changed = False
        for event in self.learning_events:
            if event["kind"] == "use" and all(event.get(k) == turn.get(k) for k in ("run_id", "battle_id", "turn")):
                event["turn_outcome"] = {k: deepcopy(turn.get(k)) for k in ("before", "after", "following_observation")}
                changed = True
        if changed:
            self.save()

    def attach_process(self, run_id: str, process: dict) -> None:
        for run in self._runs:
            if run["run_id"] == run_id:
                run["process"] = deepcopy(process)
                self.save()
                return

    # ---- 启发式规则 ----

    #: ★ 牌组快照里**保留**的字段 —— 恰好是 `_deck_gap_vector` 量化六维要读的那几个。
    #:
    #: 为什么不整张存：50 局 × 30 张 × (rules_text + dynamic_values + upgrade) 能把
    #: `catgirl_memory.json` 撑到几十 MB，而历史页/策略注入根本不看那些。
    #:
    #: ⚠️ `resolved_rules_text` **必须留** —— 六维量化里的
    #:    抽牌/群伤/状态判定全靠它做关键词匹配（`_DECK_DRAW_TOKENS` 等）。
    #:    同时 `_card_damage` / `_card_block` 在 `dynamic_values` 缺失时
    #:    **也要**回落读它（`BUG-card-damage-unit-confusion` 修完之后是按单位正则匹配）。
    _DECK_SNAPSHOT_FIELDS: tuple[str, ...] = (
        "card_id", "name", "card_type", "type",
        "upgraded", "energy_cost", "costs_x",
        "dynamic_values",
        "resolved_rules_text", "rules_text",
        "damage", "block",
    )

    @classmethod
    def _slim_deck_card(cls, card: dict[str, Any]) -> dict[str, Any]:
        """把一张 deck 卡压成"六维量化够用"的最小字段集。

        保留 `dynamic_values` 里的 `name`/`current_value` 两项即可 ——
        `_card_damage` 只按 `name == "damage"` 找，`_card_block` 只按 `name == "block"` 找。
        """
        slim: dict[str, Any] = {}
        for key in cls._DECK_SNAPSHOT_FIELDS:
            if key not in card:
                continue
            value = card[key]
            if key == "dynamic_values" and isinstance(value, list):
                value = [
                    {
                        "name": str(item.get("name") or ""),
                        "current_value": item.get("current_value"),
                    }
                    for item in value
                    if isinstance(item, dict)
                ]
            elif isinstance(value, str):
                value = value[:400]
            slim[key] = value
        return slim

    #: ★R5：真实 `decisions[].reason` 是**全英文标签**（实测 660 条 / 去重 25 种，
    #: 中文关键词命中 **0/660**）。这里列出"代表一次值得复盘的失误"的英文标签。
    #: 只收语义明确的；宁可漏判，也不要因为模糊匹配产生噪声教训。
    _MISTAKE_REASON_TOKENS: tuple[str, ...] = (
        # 战斗：被 solver 判定只能结束回合，通常意味着这回合打不出更优解
        "mod_solver_end_turn",
        # 战斗：兜底/默认路径，说明没有找到任何"有把握"的序列
        "combat_preference_or_default",
        # 路线/选牌：偏好与默认混用，说明当时没有明确的更优选项
        "map_preference_or_default",
        "reward_",
        "shop_",
    )

    @staticmethod
    def _heuristic_lessons(decision_memory: list[dict[str, Any]], run_meta: dict[str, Any]) -> list[str]:
        lessons: list[str] = []
        saw_low_hp = False
        for item in decision_memory:
            if not isinstance(item, dict):
                continue
            why = str(item.get("reason") or item.get("note") or item.get("summary") or "").lower()
            hp = item.get("current_hp")
            max_hp = item.get("max_hp")
            # 生命跌到危险（<35%）的决策 -> 提示别太激进。
            # 只要本局出现过一次低血决策就记一条；跨局聚合（summarize_recent）自会去重。
            if isinstance(hp, (int, float)) and isinstance(max_hp, (int, float)) and max_hp > 0:
                ratio = float(hp) / float(max_hp)
                if ratio < 0.35:
                    saw_low_hp = True
            if any(k in why for k in ("翻车", "失误", "暴毙", "秒杀", "战败", "贪", "上头")):
                lessons.append("这个位置容易翻车，提前留防御/留退路")
            elif _is_mistake_reason(why):
                # ★R5：真实数据是英文标签，中文关键词**永远命中不了**（实测 0/660）。
                # 补上英文分支后，这些局才第一次能产出"指向具体行为"的教训。
                lessons.append(_lesson_for_reason(why))

        # ★★ 死亡归因：从 `route[]` 的损血轨迹推出**具体死因**，产出指向维度的教训。
        #
        # 为什么必须加（实测）：本函数此前**只读 `decision_memory` 的 reason 文本**，
        # 从不读牌组、不读分层损血、不读敌人类型 ⇒ 50 局真实记忆里三条死亡教训
        # **全是同一句兜底句**（"这局输得不明不白…"），抽不出任何 token，
        # 于是复盘对下一局**毫无影响**。
        #
        # 玩家的口径：「教训是需要反思整个链路的……遇到群怪就死就反思有没有拿 aoe 卡，
        # 遇到 boss 和精英就死需要反思选卡的数值和强化卡选择」。
        # ⇒ 死因（vantom/精英/群怪/被磨死）**决定该反思哪一维**。
        #
        # 实测效果：50 局里 **49 局**能产出具体死因教训（此前 3/50 全是兜底句）。
        try:
            from .death_attribution import attribution_to_lessons, build_death_attribution

            attribution = build_death_attribution(run_meta)
            attribution["final_hp"] = run_meta.get("final_hp") if isinstance(run_meta, dict) else None
            lessons.extend(attribution_to_lessons(attribution))
        except Exception:
            # 归因失败**不得**影响原有链路 —— 宁可少一条教训，也不要让复盘炸掉。
            pass

        if saw_low_hp:
            lessons.append("血量过低时别贪输出，优先保命叠甲")

        # ★★ 六维缺口教训：由 service 用 `_deck_gap_vector` 量化本局牌组算出
        # （`deck_gap_reflection.build_gap_lessons`），经 `extra_lessons` 传进来。
        #
        # 为什么放在最后：这些是**最有信息量**的一条（指向具体维度 + 具体缺多少），
        # 而上面的都是"迹象类"教训。放在后面能让 `lessons[:N]` 这类截断
        # 优先保留迹象类（更稳），而完整列表里两者都在。
        #
        # ⚠️ 它们**不参与** `if not lessons` 的兜底判定 —— 那是"这局没抓到任何教训"的意思，
        #    六维缺口算不上"抓到了迹象"，不该阻止兜底句。
        lessons.extend(
            str(text)
            for text in (run_meta.get("extra_lessons") or [])
            if isinstance(text, (str, int)) and str(text).strip()
        )

        # 极端结局兜底：如果整局叠到低血但没抓到教训
        if not lessons and str(run_meta.get("outcome") or "") == "defeat":
            lessons.append(_FALLBACK_LESSON)
        return lessons

    @staticmethod
    def _heuristic_preferences(decision_memory: list[dict[str, Any]]) -> list[str]:
        # 同上型行动反复出现 -> 偏好；简单统计 action_type 频次
        preferences: list[str] = []
        seen = [str(item.get("action_type") or item.get("kind") or "") for item in decision_memory if isinstance(item, dict)]
        # 只保留出现 >=2 次的类型，避免把偶发当偏好
        for action_type in set(seen):
            if action_type and seen.count(action_type) >= 2:
                preferences.append(f"偏好这类选择：{action_type}")
        return preferences

    @staticmethod
    def _dedup(items: list[str]) -> list[str]:
        seen: set[str] = set()
        out: list[str] = []
        for item in items:
            normalized = str(item).strip()
            if normalized and normalized not in seen:
                seen.add(normalized)
                out.append(normalized)
        return out

    # ---- LLM 可选扩展点 ----

    @staticmethod
    def _llm_available(llm: Any) -> bool:
        try:
            return bool(getattr(llm, "available", False))
        except Exception:
            return False

    def _summarize_with_llm(self, llm: Any, decision_memory: list[dict[str, Any]], run_meta: dict[str, Any]) -> dict[str, Any]:
        """LLM 自然语言归纳一局教训/偏好。失败/解析不出任何东西返回空 dict（调用方回退启发式）。"""
        try:
            memory_line = "；".join(
                f"{str(item.get('action_type') or item.get('kind') or '')}:{str(item.get('reason') or item.get('summary') or '')}"
                for item in decision_memory[:15] if isinstance(item, dict)
            ) or "（本局无关键决策记录）"
            outcome = str(run_meta.get("outcome") or "?")
            text = llm.generate(
                summary_text=f"本局结局:{outcome}。关键决策:{memory_line}",
                summary_kind="memory_summary",
                payload={},
            )
            if not text:
                return {}
            # 简单按分隔符切出 lessons / preferences 两条；LLM 可能自由发挥，取不到就都算教训
            lines = [ln.strip() for ln in str(text).splitlines() if ln.strip()]
            lessons = [ln for ln in lines if ln.startswith(("教训", "lesson"))]
            preferences = [ln for ln in lines if ln.startswith(("偏好", "prefer"))]
            if not lessons and not preferences:
                lessons = lines[:1] or []
            return {"lessons": lessons, "preferences": preferences}
        except Exception:
            return {}

    def _log(self, msg: str) -> None:
        try:
            if self._logger is not None:
                self._logger.info(msg)
            else:
                print(msg)
        except Exception:
            pass


__all__ = ["CatgirlMemoryStore", "_default_memory_path"]
