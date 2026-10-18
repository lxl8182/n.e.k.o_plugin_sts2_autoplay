from __future__ import annotations

from copy import deepcopy
from hashlib import sha256
import json
import re
from pathlib import Path
from statistics import median
from time import time
from typing import Any
from uuid import uuid4

from .planner_interface import public_kwargs


def model_action(record: dict, *, detailed: bool = True) -> dict:
    result = {k: deepcopy(record.get(k)) for k in ("id", "action_type", "status", "executed", "plan_revision", "step_id")}
    for key in ("card", "target"):
        value = record.get(key)
        result[key] = {k: deepcopy(value[k]) for k in ("entity_id", "card_id", "name", "line", "energy_cost", "current_hp", "hp", "intent") if k in value} if isinstance(value, dict) else value
    if detailed:
        result.update(reason=str(record.get("reason") or "")[:600], outcome=deepcopy(record.get("outcome") or {}))
        if record.get("model_reason") and record["model_reason"] != record.get("reason"):
            result["model_reason"] = str(record["model_reason"])[:1000]
    return result


def scene(view: dict, facts: dict | None = None, *, detailed: bool = False) -> dict:
    """同侧公开事实；状态差保留为观测，不归因给单张牌。"""
    combat, run = view.get("combat") or {}, view.get("run") or {}
    result = {"screen": view.get("screen"), "floor": run.get("floor"),
              "character": run.get("character"), "hp": run.get("hp"),
              "player": deepcopy(combat.get("player") or {}),
              "enemies": deepcopy(combat.get("enemies") or [])}
    if detailed:
        result.update(hand=deepcopy(combat.get("hand") or []),
                      effects=deepcopy(facts or {}))
        for key in ("event", "reward", "rest", "map", "selection"):
            if key in view:
                result[key] = deepcopy(view[key])
    return result


class CompanionJournal:
    """按局保存紧凑过程，最近两个战斗回合另存详细依据。"""

    def __init__(self, directory: str | None = None, logger: Any = None) -> None:
        self.directory = directory
        self.logger = logger
        self.data: dict = {}
        self.error = ""
        self.revision = 0
        self.restored = False
        self.on_turn = None
        self._checkpoint: dict = {}
        self._checkpoint_loaded = False

    def _path(self, run_id: str) -> Path:
        return Path(self.directory) / (sha256(run_id.encode()).hexdigest() + ".json")

    def activate(self, run_id: str | None) -> None:
        if not run_id or run_id == "run_unknown":
            self.data = {}
            self.restored = False
            return
        if self.data.get("run_id") == run_id:
            return
        self._checkpoint = {}
        self._checkpoint_loaded = False
        loaded = {}
        if self.directory and self._path(run_id).exists():
            try:
                loaded = json.loads(self._path(run_id).read_text(encoding="utf-8"))
            except (OSError, ValueError) as exc:
                self.error = f"读取局内记录失败：{exc}"
                if self.logger:
                    self.logger.warning("[sts2_journal] %s", self.error)
                # 损坏文件保持原状，等待明确处理，不用空记录覆盖。
                raise RuntimeError(self.error) from exc
        self.data = loaded or {"version": 1, "run_id": run_id, "turns": [], "details": {},
                               "cases": [], "reviews": [], "latency": [], "started_at": time()}
        self.restored = bool(loaded)

    def load_checkpoint(self) -> dict:
        if not self._checkpoint_loaded:
            if self.directory and self.data.get("run_id"):
                path = self._path(self.data["run_id"]).with_suffix(".checkpoint.json")
                if path.exists():
                    self._checkpoint = json.loads(path.read_text(encoding="utf-8"))
            self._checkpoint_loaded = True
        return deepcopy(self._checkpoint)

    def save_checkpoint(self, checkpoint: dict) -> None:
        self.load_checkpoint()
        self._checkpoint.update(deepcopy(checkpoint))
        if self.directory and self.data.get("run_id"):
            path = self._path(self.data["run_id"]).with_suffix(".checkpoint.json")
            path.parent.mkdir(parents=True, exist_ok=True)
            temporary = path.with_suffix(".tmp")
            temporary.write_text(json.dumps(self._checkpoint, ensure_ascii=False), encoding="utf-8")
            temporary.replace(path)

    def save(self) -> None:
        self.revision += 1
        if not self.directory or not self.data:
            return
        try:
            path = self._path(self.data["run_id"])
            path.parent.mkdir(parents=True, exist_ok=True)
            temp = path.with_suffix(".tmp")
            temp.write_text(json.dumps(self.data, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
            temp.replace(path)
            self.error = ""
        except OSError as exc:
            self.error = f"保存局内记录失败：{exc}"
            if self.logger:
                self.logger.warning("[sts2_journal] %s", self.error)

    @staticmethod
    def turn_id(anchor: dict, view: dict) -> str:
        if anchor.get("battle_id"):
            return f"{anchor['battle_id']}:turn:{anchor.get('turn')}"
        return f"{anchor.get('run_id')}:room:{(view.get('run') or {}).get('floor')}:{view.get('screen')}"

    def observe(self, view: dict, anchor: dict, facts: dict | None = None) -> None:
        self.activate(anchor.get("run_id"))
        if not anchor.get("run_id") or self.data.get("run_id") != anchor["run_id"]:
            return
        key = self.turn_id(anchor, view)
        turn = next((t for t in self.data["turns"] if t["id"] == key), None)
        current = scene(view)
        if facts:
            current["effects"] = {k: deepcopy(facts[k]) for k in ("incoming_damage", "orbs", "enemies", "player") if k in facts}
        if turn is None:
            turn = {"id": key, **anchor, "started_at": time(), "before": current,
                    "after": current, "actions": [], "plans": [], "closed": False}
            if self.data["turns"]:
                previous = self.data["turns"][-1]
                previous["closed"] = True
                previous["following_observation"] = current
                for case in self.data["cases"]:
                    if case["turn_id"] == previous["id"]:
                        case["record"] = deepcopy(previous)
                if self.on_turn and previous.get("battle_id"):
                    self.on_turn(previous)
            self.data["turns"].append(turn)
            if anchor.get("battle_id"):
                self.data["details"][key] = {"initial": scene(view, facts, detailed=True), "plans": [], "actions": {}}
                combat_ids = [t["id"] for t in self.data["turns"] if t.get("battle_id")][-2:]
                self.data["details"] = {k: d for k, d in self.data["details"].items() if k in combat_ids}
        changed = turn["after"] != current or self.data.get("latest", {}).get("id") != key
        turn["after"] = current
        self.data["latest"] = {"id": key, **anchor, "screen": view.get("screen"), "floor": current["floor"]}
        # 自动观察可补齐回合结算；只在事实变化时写盘。
        if changed or "saved_at" not in turn:
            turn["saved_at"] = time()
            self.save()

    def record_plan(self, plan: dict, view: dict, requirements: list, facts: dict) -> None:
        key = self.turn_id(plan["anchor"], view)
        turn = next((t for t in self.data.get("turns", []) if t["id"] == key), None)
        if turn is None:
            return
        plan_id = f"{key}:plan:{plan['revision']}"
        if any(p["id"] == plan_id for p in turn["plans"]):
            return
        summary = {"id": plan_id, "revision": plan["revision"], "reason": plan["reason"],
                   "model_reason": plan.get("model_reason", plan["reason"]),
                   "requirements": deepcopy(requirements), "solver_adopted": plan.get("solver_adopted", False),
                   "steps": [{"step_id": s["step_id"], "description": s["description"], "reason": s["reason"]}
                             for s in plan["remaining_steps"]],
                   "experience_uses": deepcopy(plan.get("experience_uses") or [])}
        summary["solver_summary"] = deepcopy(plan.get("solver_summary") or {})
        summary["skip_zero_cost_cards"] = deepcopy(plan.get("skip_zero_cost_cards") or {})
        turn["plans"].append(summary)
        if key in self.data["details"]:
            self.data["details"][key]["plans"].append({**deepcopy(summary), "scene": scene(view, facts, detailed=True)})
        self.save()

    def cancel_plan(self, revision: int, reason: str) -> None:
        for turn in reversed(self.data.get("turns", [])):
            for plan in turn["plans"]:
                if plan["revision"] == revision:
                    plan.update(cancel_reason=reason)
                    self.save()
                    return

    def record_action(self, record: dict, before: dict, after: dict, facts: dict) -> None:
        key = self.turn_id(record, before)
        turn = next((t for t in self.data.get("turns", []) if t["id"] == key), None)
        if turn is None:
            return
        compact = {k: deepcopy(v) for k, v in record.items() if k not in {"before_player", "kwargs"}}
        compact["kwargs"] = public_kwargs(record.get("kwargs"))
        compact["before"] = scene(before)
        compact["after"] = scene(after)
        found = next((a for a in turn["actions"] if a["id"] == record["id"]), None)
        if found is None:
            turn["actions"].append(compact)
        else:
            found.update(compact)
        if key in self.data["details"]:
            self.data["details"][key]["actions"][record["id"]] = {
                "before": scene(before, facts, detailed=True), "after": scene(after, detailed=True)}
        if record["status"] in {"rejected", "unconfirmed", "cancelled", "stale"}:
            self.keep_case(key, action_id=record["id"], reason=record["status"])
        elif record.get("experience_uses"):
            self.keep_case(key, action_id=record["id"], reason="experience_use")
        self.save()

    def confirm_action(self, record: dict, after: dict) -> None:
        for turn in self.data.get("turns", []):
            action = next((a for a in turn["actions"] if a["id"] == record["id"]), None)
            if action:
                action.update({k: deepcopy(record[k]) for k in ("status", "executed", "message", "outcome")})
                action["after"] = scene(after)
                action["confirmed_at"] = time()
                if turn["id"] in self.data["details"]:
                    detail = self.data["details"][turn["id"]]["actions"].get(record["id"])
                    if detail:
                        detail["after"] = scene(after, detailed=True)
                if not record["executed"]:
                    self.keep_case(turn["id"], action_id=record["id"], reason=record["status"])
                elif record.get("experience_uses"):
                    self.keep_case(turn["id"], action_id=record["id"], reason="experience_use")
                self.save()
                return

    def keep_case(self, turn_id: str, *, action_id: str = "", reason: str = "review") -> dict:
        turn = next((t for t in self.data.get("turns", []) if t["id"] == turn_id), None)
        if turn is None:
            raise ValueError("复盘来源回合不存在")
        if action_id and not any(a["id"] == action_id for a in turn["actions"]):
            raise ValueError("复盘来源动作不属于该回合")
        existing = next((c for c in self.data["cases"] if c["turn_id"] == turn_id and c["action_id"] == action_id), None)
        if existing:
            existing.update(record=deepcopy(turn), detail=deepcopy(self.data["details"].get(turn_id, existing["detail"])))
            self.save()
            return existing
        case = {"id": str(uuid4()), "turn_id": turn_id, "action_id": action_id, "reason": reason,
                "record": deepcopy(turn), "detail": deepcopy(self.data["details"].get(turn_id, {}))}
        self.data["cases"].append(case)
        self.save()
        return case

    def review_context(self, messages: list[dict] | None = None) -> dict:
        turns = [t for t in self.data.get("turns", []) if t.get("battle_id")][-2:]
        text = " ".join(m["content"] for m in messages or [])
        detailed = messages is None or any(word in text for word in ("刚才", "回合", "为什么", "不如", "应该", "复盘", "之前", "刚刚"))
        projected = []
        for turn in turns:
            item = {k: deepcopy(turn.get(k)) for k in ("id", "battle_id", "turn", "closed")}
            actions = turn["actions"]
            item["action_count"] = len(actions)
            item["actions"] = [model_action(a, detailed=detailed) for a in actions[-20:]]
            if detailed:
                item.update(before=deepcopy(turn["before"]), after=deepcopy(turn["after"]))
                detail = self.data["details"].get(turn["id"], {})
                item["initial"] = deepcopy(detail.get("initial") or {})
                # 每份现场只读一次，不再附每次计划/动作的重复scene和selected_plan。
                item["plans"] = [{k: deepcopy(p.get(k)) for k in ("id", "revision", "reason", "steps", "cancel_reason")}
                                 for p in turn["plans"][-4:]]
            projected.append(item)
        result = {"current": deepcopy(self.data.get("latest")), "turns": projected,
            "purpose": "review" if detailed else "index",
            "interpretation": "按battle_id和turn解释当前/上一回合；当时reason、实际结果、现在评价分开。结束等待队友仍属刚结束回合。状态差为联机观测。"}
        if messages and re.search(r"上(?:一|个)?回合|上一轮|刚才|刚刚|为什么|复盘|之前", text):
            current_turn = next((m["turn"] for m in reversed(messages) if m.get("turn") is not None),
                                (self.data.get("latest") or {}).get("turn"))
            previous = bool(re.search(r"上(?:一|个)?回合|上一轮", text))
            eligible = [t for t in turns if t.get("actions") and (not previous or t.get("turn") != current_turn)]
            focus = eligible[-1] if eligible else turns[-1] if turns else None
            if focus:
                result["focus_turn_id"] = focus["id"]
                result["interpretation"] += " 本轮先回答focus_turn_id。规划现场与实际派发现场分别给出；比较少数可行替代路线即可，联机削血不归因给自己的牌，不推演无关未来回合。"
                def evidence_scene(value):
                    compact = {k: deepcopy(value[k]) for k in ("screen", "hp", "player", "enemies", "hand") if k in value}
                    effects = value.get("effects") or {}
                    compact["effects"] = {k: deepcopy(effects[k]) for k in ("player_powers", "enemies", "orbs", "incoming_damage") if k in effects}
                    compact["effects"]["relics"] = [{k: relic[k] for k in ("relic_id", "name", "effect") if k in relic} for relic in effects.get("relics", [])]
                    return compact
                for item in projected:
                    turn = next(t for t in turns if t["id"] == item["id"])
                    item["actions"] = [model_action(a, detailed=False) for a in turn["actions"][-20:]]
                    item.pop("initial", None)
                    item.pop("plans", None)
                    if turn["id"] == focus["id"]:
                        detail = self.data.get("details", {}).get(turn["id"], {})
                        plans = detail.get("plans") or []
                        item["planning"] = evidence_scene((plans[0].get("scene") if plans else None) or detail.get("initial") or turn["before"])
                        item["original_reason"] = str(plans[0].get("model_reason") or plans[0].get("reason") or "") if plans else ""
                        first_id = turn["actions"][0]["id"] if turn["actions"] else ""
                        first = (detail.get("actions") or {}).get(first_id, {})
                        item["first_dispatch"] = evidence_scene(first.get("before") or turn["before"])
        # 有界投影；完整原记录仍按turn/action id读取并保存在本地过程文件。
        for item in projected:
            if len(json.dumps(result, ensure_ascii=False)) > 24000:
                item.pop("plans", None)
                initial = item.get("initial") or {}
                initial.pop("effects", None)
        for item in projected:
            if len(json.dumps(result, ensure_ascii=False)) > 24000:
                item.pop("before", None)
                item.pop("after", None)
                item["actions"] = item["actions"][-8:]
        if len(json.dumps(result, ensure_ascii=False)) > 24000:
            for item in projected:
                item.pop("initial", None)
                item.pop("plans", None)
        return result

    def review(self, value: dict, reply: str, messages: list[dict]) -> None:
        case = self.keep_case(value["turn_id"], action_id=value.get("action_id", ""))
        request_ids = [m.get("request_id") or m["id"] for m in messages]
        if any(r["request_ids"] == request_ids and r["case_id"] == case["id"] for r in self.data["reviews"]):
            return
        self.data["reviews"].append({"case_id": case["id"], "request_ids": request_ids,
            "kind": value.get("kind", "player_review"),
            "question": value.get("question", ""),
            "no_learning_reason": value.get("no_learning_reason", ""),
            "questions": [m["content"] for m in messages], "reply": reply, "assessment": value["assessment"], "timestamp": time()})
        self.save()

    def run_context(self) -> dict:
        """整局小条目加重点案例，避免仅把末尾动作交给反思。"""
        return {"run_id": self.data.get("run_id"), "turns": [
            {k: deepcopy(t[k]) for k in ("id", "battle_id", "turn", "before", "after", "closed", "plans")}
            | {"following_observation": deepcopy(t.get("following_observation"))}
            | {"actions": [{k: deepcopy(a.get(k)) for k in ("id", "action_type", "card", "target", "reason", "status", "experience_uses")}
                           for a in t["actions"]]} for t in self.data.get("turns", [])],
            "cases": deepcopy(self.data.get("cases", [])), "reviews": deepcopy(self.data.get("reviews", [])),
            "effect_report": self.report(), "hp_samples": deepcopy(self.data.get("hp_samples", [])),
            "rooms": deepcopy((self.data.get("telemetry") or {}).get("route_log", []))}

    def latency(self, sample: dict) -> None:
        if self.data:
            self.data["latency"].append(deepcopy(sample))
            self.save()

    @staticmethod
    def reflection_context(process: dict) -> dict:
        """全局覆盖按战斗/房间汇总，重点回合有界读取；原过程仍完整落盘。"""
        turns = process.get("turns", [])
        rooms: dict[str, dict] = {}
        cases = process.get("cases", [])
        case_turns = {c["turn_id"] for c in cases}
        def hp(state):
            player = state.get("player") or {}
            value = player.get("current_hp", player.get("hp", state.get("hp")))
            if isinstance(value, str):
                try:
                    return float(value.split("/", 1)[0])
                except ValueError:
                    return None
            return value if isinstance(value, (int, float)) else None
        def loss(turn):
            before = hp(turn["before"])
            after = hp(turn.get("following_observation") or turn["after"])
            return max(0, before - after) if before is not None and after is not None else 0
        last_combat = next((t["id"] for t in reversed(turns) if t.get("battle_id") and t.get("actions")), None)
        fatal = {t["id"] for t in turns if (hp(t["before"]) or 0) > 0
                 and hp(t.get("following_observation") or t["after"]) == 0}
        selected = sorted(turns, key=lambda t: (t["id"] in fatal, t["id"] == last_combat, loss(t),
            t["id"] in case_turns,
            any(a["status"] in {"rejected", "unconfirmed", "stale"} or a.get("experience_uses") for a in t["actions"])), reverse=True)[:12]
        selected_ids = {t["id"] for t in selected}
        for turn in turns:
            key = turn.get("battle_id") or turn["id"]
            room = rooms.setdefault(key, {"id": key, "floor": turn["before"].get("floor"),
                "before": deepcopy(turn["before"]), "turn_count": 0, "action_count": 0, "action_statuses": {}})
            room["after"] = deepcopy(turn["after"])
            room["turn_count"] += 1
            room["action_count"] += len(turn["actions"])
            for action in turn["actions"]:
                status = action["status"]
                room["action_statuses"][status] = room["action_statuses"].get(status, 0) + 1
        result = {"run_id": process.get("run_id"), "rooms": list(rooms.values()),
            "key_turns": [{k: deepcopy(t[k]) for k in ("id", "battle_id", "turn", "before", "after")}
                | {"following_observation": deepcopy(t.get("following_observation")),
                   "plans": [{k: deepcopy(p.get(k)) for k in ("revision", "model_reason", "steps")}
                             | {"solver_summary": {k: deepcopy((p.get("solver_summary") or {}).get(k))
                                                   for k in ("score", "win_prob", "confidence")}}
                             for p in t.get("plans", [])[-2:]],
                   "actions": [model_action(a) for a in t["actions"][-12:]]} for t in turns if t["id"] in selected_ids],
            "cases": [{k: deepcopy(c[k]) for k in ("id", "turn_id", "action_id", "reason")}
                | {"initial": deepcopy((c.get("detail") or {}).get("initial") or {}),
                   "actions": [model_action(a) for a in (c.get("record") or {}).get("actions", [])
                               if not c["action_id"] or a["id"] == c["action_id"]][-4:]}
                for c in sorted(cases, key=lambda c: c["reason"] in {"learning", "review"}, reverse=True)[:3]],
            "reviews": deepcopy(process.get("reviews", [])[-6:]),
            "interpretation": "rooms覆盖整局，key_turns为重点回合。前后生命变化是观测区间净变化，联机动作贡献按实际记录区分。"}
        if len(json.dumps(result, ensure_ascii=False)) > 48000:
            for turn in result["key_turns"]:
                turn["actions"] = turn["actions"][-4:]
            for case in result["cases"]:
                case["initial"].pop("effects", None)
        return result

    def report(self) -> dict:
        groups: dict[tuple, list] = {}
        for sample in self.data.get("latency", []):
            groups.setdefault((sample["configuration"], sample.get("path", ""), sample.get("outcome", "")), []).append(sample)
        output = []
        for (config, path, outcome), samples in groups.items():
            measurements: dict[str, list] = {}
            for sample in samples:
                for key, value in {**sample.get("stages", {}), **sample.get("measures", {})}.items():
                    if key in {"model_ms", "solver_ms"} and sample.get("model_calls" if key == "model_ms" else "solver_calls", 0) == 0:
                        continue
                    if isinstance(value, (int, float)):
                        measurements.setdefault(key, []).append(value)
            def stats(values):
                values = sorted(values)
                return {"samples": len(values), "median_ms": round(median(values), 1),
                        "p90_ms": round(values[max(0, (9 * len(values) + 9) // 10 - 1)], 1)}
            output.append({"configuration": config, "path": path, "outcome": outcome, "samples": len(samples),
                           "timings": {key: stats(values) for key, values in measurements.items()},
                           "paths": {path: sum(s.get("path") == path for s in samples) for path in sorted({s.get("path", "") for s in samples})},
                           "model_calls": sum(s.get("model_calls", 0) for s in samples),
                           "solver_calls": sum(s.get("solver_calls", 0) for s in samples),
                           "counts": {key: sum(s.get("counts", {}).get(key, 0) for s in samples)
                                      for key in ("continuation_reused", "continuation_cancelled", "solver_cache_hits", "prompt_tokens", "cached_tokens")},
                           "cancellation_causes": {cause: sum(s.get("cancellation_causes", {}).get(cause, 0) for s in samples)
                               for cause in {k for s in samples for k in s.get("cancellation_causes", {})}}})
        return {"run_id": self.data.get("run_id"), "turns": len(self.data.get("turns", [])),
                "cases": len(self.data.get("cases", [])), "reviews": len(self.data.get("reviews", [])),
                "action_statuses": {status: sum(a["status"] == status for t in self.data.get("turns", []) for a in t["actions"])
                                    for status in sorted({a["status"] for t in self.data.get("turns", []) for a in t["actions"]})},
                "outcomes": {"before": deepcopy((self.data.get("turns") or [{}])[0].get("before")),
                             "after": deepcopy((self.data.get("turns") or [{}])[-1].get("after"))},
                "latency": output, "storage_error": self.error}
