# -*- coding: utf-8 -*-
"""猫娘跨局战报、复盘评价和可修正的条件经验。

战报保存整局紧凑过程；反思文字独立保存。统一模型同次输出的结构化经验
带来源、条件和版本，按当前局面匹配少量条目，并关联真实动作及回合结果。
已有旧战报和启发式教训继续可读，新过程不从动作频次推断玩家偏好。
"""

from __future__ import annotations

import json
import os
from time import time
from typing import Any
from copy import deepcopy
from uuid import uuid4
from .strategy_repository import SCENES, strategy_scene

# 跨局记忆条数上限（避免无限膨胀）
_RUN_CAP = 50


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
        self.load()

    @property
    def path(self) -> str | None:
        return self._path

    @property
    def runs(self) -> list[dict[str, Any]]:
        return self._runs

    def load(self) -> None:
        """从磁盘读入最近记忆；文件不存在/损坏时空载（不中断）。"""
        self._runs = []
        if not self._path or not os.path.exists(self._path):
            return
        try:
            with open(self._path, "r", encoding="utf-8") as f:
                data = json.load(f)
            data = data if isinstance(data, dict) else {}
            records = data.get("runs") if isinstance(data, dict) else None
            self.experiences = data.get("experiences", [])
            self.learning_events = data.get("learning_events", [])
            self.applied_updates = data.get("applied_updates", [])
            if isinstance(records, list):
                self._runs = [r for r in records if isinstance(r, dict)]
                self._runs = self._runs[-_RUN_CAP:]
        except (OSError, ValueError):
            self._runs = []

    def save(self) -> None:
        """把当前记忆写回磁盘（幂等；path 为 None 时静默跳过）。"""
        if not self._path:
            return
        try:
            os.makedirs(os.path.dirname(self._path), exist_ok=True)
            payload = {"runs": self._runs[-_RUN_CAP:], "updated_at": time(),
                       "experiences": self.experiences, "learning_events": self.learning_events,
                       "applied_updates": self.applied_updates}
            tmp = self._path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False, indent=2)
            os.replace(tmp, self._path)
            self.last_error = ""
        except OSError:
            self.last_error = f"经验保存失败：{self._path}"
            self._log(f"catgirl_memory save failed: {self._path}")

    def add_run(self, run_id: str, *, character: str, outcome: str, floor: int, act: int,
                decision_memory: list[dict[str, Any]], llm: Any = None,
                hp_report: dict[str, Any] | None = None,
                route: list[dict[str, Any]] | None = None, process: dict | None = None) -> dict[str, Any]:
        """总结一局并追加到记忆；同 run_id 保留已有记录，结果纠正时清除旧结论。

        ``llm`` 参数保留但**这里不再使用**：本方法必须是同步、确定、立刻落盘的（terminal 屏会反复
        出现，记录不能等模型）。自然语言的**反思**改由 service 事后异步生成，再经
        ``attach_reflection`` 贴回同一条记录。

        旧代码在这里同步调用 ``llm.generate(...)`` —— 那是个 ``async`` 方法，拿到的是协程，
        随后 ``.splitlines()`` 直接抛错被 ``except`` 吞掉；而且还有 ``self._engine == "llm"`` 这道
        默认关闭的闸门。**那条 LLM 总结路径从来没有生效过**，一直是纯启发式。
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
        summary = {"lessons": [], "preferences": []} if process and process.get("turns") else self.summarize_run(
            decision_memory=decision_memory,
            run_meta=run,
        )
        run["lessons"] = summary.get("lessons", [])
        run["preferences"] = summary.get("preferences", [])
        self._runs.append(run)
        self._runs = self._runs[-_RUN_CAP:]
        self.save()
        return dict(run)

    # ---- 总结 ----

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
            self.save()
            return dict(run)
        return None

    def summarize_recent(self, limit: int = 5) -> dict[str, list[str]]:
        """聚合旧局报供历史查询，不作为决策指令。"""
        lessons: list[str] = []
        preferences: list[str] = []
        for run in self._runs[-limit:]:
            lessons.extend(str(item) for item in run.get("lessons", []) if isinstance(item, (str, int)))
            preferences.extend(str(item) for item in run.get("preferences", []) if isinstance(item, (str, int)))
        return {"lessons": self._dedup(lessons), "preferences": self._dedup(preferences)}

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
