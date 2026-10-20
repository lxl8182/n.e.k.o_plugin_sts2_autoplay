from __future__ import annotations

from collections import deque
from copy import deepcopy
from time import time
from typing import Any
from uuid import uuid4

from .planner_interface import public_kwargs
from .companion_requirements import RequirementBook
from .companion_journal import CompanionJournal


class CompanionSession:
    """有界的局内对话与事实；不读取跨局策略记忆。"""

    def __init__(self) -> None:
        self.messages: deque[dict[str, Any]] = deque(maxlen=16)
        self.user_requests: deque[dict[str, Any]] = deque(maxlen=20)
        self.actions: deque[dict[str, Any]] = deque(maxlen=20)
        self.observations: deque[dict[str, Any]] = deque(maxlen=32)
        self._anchors: deque[dict[str, Any]] = deque(maxlen=128)
        self._battle_number = 0
        self._in_combat = False
        self.future_plan: dict[str, Any] = {}
        self.plan_revision = 0
        self.action_sequence = 0
        self.active_plan: dict[str, Any] = {"remaining_steps": []}
        self.announced_plan: dict[str, Any] = {}
        self.replacement_reason = ""
        self.facts: dict[str, Any] = {}
        self._run_id: str | None = None
        self.generation = 0
        self.dialogue_revision = 0
        self.progress_sequence = 0
        self._last_speech: dict[str, Any] = {}
        self.epoch = 0
        self.character_choice_made = False
        self.terminal_reply_run_id: str | None = None
        self.started_at = time()
        self._seen_records: deque[str] = deque(maxlen=128)
        self._character_name = ""
        self.requirements = RequirementBook()
        self.journal = CompanionJournal()
        self.observed_effects: dict[str, Any] = {}
        self.on_action = None
        self.input_results: list[dict[str, Any]] = []
        self.delivery_revision = 0
        # C03：当前待回答的问题（回答/取消/过期/被新话题替代）。
        self.pending_question: dict[str, Any] = {}

    def observe(self, view: dict[str, Any], *, in_combat: bool | None = None, effects: dict | None = None) -> None:
        observed_at = time()
        run_id = view.get("run_id")
        pregame = {"MAIN_MENU", "MULTIPLAYER_LOBBY", "CHARACTER_SELECT"}
        if run_id == "run_unknown" or view.get("screen") in pregame:
            run_id = None
        entering_run = run_id is not None and self._run_id is None and self.facts.get("screen") in pregame
        if run_id != self._run_id and not entering_run:
            if self.journal.data.get("run_id") and self.journal.data["run_id"] != run_id:
                for old_turn in self.journal.data["turns"]:
                    for record in list(old_turn["actions"]):
                        if record["status"] == "pending":
                            record.update(status="cancelled", executed=False, message="已离开原局，取消等待旧动作。")
                            self.journal.confirm_action(record, view)
                            if self.on_action:
                                self.on_action(record)
            self.messages.clear()
            self.user_requests.clear()
            self.input_results.clear()
            self.delivery_revision += 1
            self.facts.clear()
            self.actions.clear()
            self.observations.clear()
            self._anchors.clear()
            self.future_plan.clear()
            self.pending_question = {}
            self.active_plan = {"remaining_steps": []}
            self.announced_plan.clear()
            self._last_speech.clear()
            self.replacement_reason = ""
            self.plan_revision = 0
            self.action_sequence = 0
            self._battle_number = 0
            self._in_combat = False
            self._run_id = run_id
            self.generation += 1
            self.epoch += 1
            self.character_choice_made = False
            self.terminal_reply_run_id = None
            self.started_at = time()
            self._seen_records.clear()
            self.requirements.clear()
        self._run_id = run_id
        self.journal.activate(run_id)
        self.observed_effects = deepcopy(effects or {})
        run = view.get("run") or {}
        combat = view.get("combat") or {}
        if in_combat is None:
            in_combat = view.get("screen") == "COMBAT"
        if in_combat and not self._in_combat:
            saved = self.journal.data.get("latest") or {}
            if self._battle_number == 0 and saved.get("battle_id") and saved.get("floor") == run.get("floor"):
                self._battle_number = int(saved["battle_id"].rsplit(":", 1)[1])
            else:
                self._battle_number = max([self._battle_number, *[int(t["battle_id"].rsplit(":", 1)[1])
                    for t in self.journal.data.get("turns", []) if t.get("battle_id")]]) + 1
        self._in_combat = in_combat
        battle_id = f"{run_id}:{self._battle_number}" if in_combat else None
        turn = view.get("turn", combat.get("turn")) if in_combat else None
        anchor = {"run_id": run_id, "battle_id": battle_id, "turn": turn}
        if in_combat:
            observation = {**anchor, "player": deepcopy(combat.get("player") or {}),
                           "hand": deepcopy(combat.get("hand") or []),
                           "discard": deepcopy(combat.get("discard") or [])}
            previous = {k: v for k, v in self.observations[-1].items() if k != "timestamp"} if self.observations else None
            if observation != previous:
                self.observations.append({"timestamp": observed_at, **observation})
        if not self._anchors or any(self._anchors[-1].get(k) != v for k, v in anchor.items()):
            self._anchors.append({"timestamp": observed_at, **anchor})
        if self.future_plan and (self.future_plan.get("battle_id") != battle_id
                or (turn is not None and turn > self.future_plan["through_turn"])):
            self.future_plan.clear()
        self.facts = {
            **anchor, "screen": view.get("screen"),
            "character": run.get("character"), "floor": run.get("floor"),
            "act": run.get("act_id"), "hp": run.get("hp"), "player": combat.get("player"),
            "last_action": self.facts.get("last_action"),
            "last_outcome": self.facts.get("last_outcome"),
        }
        if (self.active_plan.get("status") == "selected"
                and self.active_plan.get("anchor") != {**anchor, "screen": view.get("screen")}):
            self.invalidate_plan("进入新的回合或界面，旧步骤取消")
        self.plan_revision = max([self.plan_revision, *[p["revision"] for t in self.journal.data.get("turns", []) for p in t["plans"]]])
        self.journal.observe(view, anchor, self.observed_effects)

    def anchor(self) -> dict[str, Any]:
        return {key: self.facts.get(key) for key in ("run_id", "battle_id", "turn")}

    @property
    def requirements_revision(self) -> int:
        return self.requirements.revision

    def active_requirements(self) -> list[dict[str, Any]]:
        return self.requirements.active()

    def requirement_states(self) -> list[dict[str, Any]]:
        return self.requirements.states()

    def waiting_requirements(self) -> list[dict[str, Any]]:
        return [r for r in self.active_requirements() if r["status"] == "waiting"]

    def apply_requirement_updates(self, updates: list, snapshot: dict[str, Any], operation: dict | None = None,
                                  *, messages: list[dict] | None = None) -> bool:
        changed = self.requirements.apply(updates, snapshot=snapshot, anchor=self.anchor(),
                                          messages=self.pending_messages() if messages is None else messages,
                                          action_sequence=self.action_sequence, operation=operation)
        if changed:
            self.generation += 1
            self.refresh_requirements(snapshot)
        return changed

    def refresh_requirements(self, snapshot: dict[str, Any]) -> bool:
        changed = self.requirements.refresh(snapshot, self.anchor(), list(self.actions))
        if changed:
            self.generation += 1
        return changed

    def requirement_blocker(self, operation: dict[str, Any], snapshot: dict[str, Any]) -> dict | None:
        self.refresh_requirements(snapshot)
        return self.requirements.blocker(operation, snapshot)

    def bind_requirement_completions(self, operation: dict[str, Any]) -> None:
        self.requirements.bind_action_completions(operation, self.anchor())

    def confirm_requirement_action(self, record: dict[str, Any]) -> bool:
        changed = self.requirements.confirm_action(record)
        if changed:
            self.generation += 1
        return changed

    def restore_requirements(self, context: dict[str, Any]) -> None:
        """现场已确认同局后恢复期限锚点，不恢复旧动作计划。"""
        previous = context.get("current_run") or {}
        if previous.get("run_id") != self.facts.get("run_id"):
            return
        self.requirements.records = deepcopy(context.get("requirements") or [])
        self.requirements.revision = context.get("requirements_revision", 0)
        self.requirements.sequence = context.get("requirement_sequence", len(self.requirements.records))
        self.action_sequence = (context.get("decision_state") or {}).get("action_sequence", self.action_sequence)
        self._battle_number = context.get("battle_number", self._battle_number)
        if self._in_combat and context.get("in_combat") and previous.get("floor") == self.facts.get("floor"):
            self.facts["battle_id"] = f"{self._run_id}:{self._battle_number}"
            if self._anchors:
                self._anchors[-1]["battle_id"] = self.facts["battle_id"]
        elif self._in_combat:
            self._battle_number += 1
            self.facts["battle_id"] = f"{self._run_id}:{self._battle_number}"

    @staticmethod
    def describe_step(operation: dict[str, Any], view: dict[str, Any]) -> dict[str, Any]:
        """把易漂移的下标附上当时的牌面/目标，供计划比较和聊天使用。"""
        kwargs = public_kwargs(operation.get("kwargs"))
        combat = view.get("combat") or {}
        card = next((item for item in combat.get("hand") or []
                     if item.get("i", item.get("index")) == kwargs.get("card_index")), {})
        target = next((item for i, item in enumerate(combat.get("enemies") or [])
                       if item.get("i", item.get("index", i)) == kwargs.get("target_index")), {})
        bound = operation.get("bindings") or {}
        if bound:
            card = bound.get("card") or {}
            target = bound.get("target") or {}
        return {"action_type": operation["action_type"], "kwargs": kwargs,
                "card_ref": (bound.get("card") or {}).get("ref"),
                "target_ref": (bound.get("target") or {}).get("ref"),
                "card": card.get("line") or card.get("name") or "",
                "target": target.get("name") or ""}

    @staticmethod
    def step_identity(step: dict[str, Any]) -> dict[str, Any]:
        description = step["description"]
        kwargs = dict(description["kwargs"])
        if description["card"] and description["action_type"] == "play_card":
            kwargs.pop("card_index", None)
        if description.get("target_ref"):
            kwargs.pop("target_index", None)
        return {**description, "kwargs": kwargs}

    def accept_plan(self, operation: dict[str, Any], view: dict[str, Any]) -> None:
        steps = []
        for index, candidate in enumerate([operation, *operation.get("continuation", [])]):
            self.bind_requirement_completions(candidate)
            expected = view if index == 0 else candidate.get("expected_view") or {}
            steps.append({"action_type": candidate["action_type"],
                          "kwargs": public_kwargs(candidate.get("kwargs")),
                          "reason": candidate.get("reason") or "",
                          "model_reason": candidate.get("model_reason") or operation.get("model_reason") or "",
                          "flow_anchor": deepcopy(candidate.get("flow_anchor") or operation.get("flow_anchor") or {}),
                          "expected_view": deepcopy(expected),
                          "bindings": deepcopy(candidate.get("bindings") or {}),
                          "assumptions": deepcopy(candidate.get("assumptions") or []),
                          "known_card_entities": list(candidate.get("known_card_entities") or []),
                          "enemy_intents": deepcopy(candidate.get("enemy_intents")),
                          "observation_guard": deepcopy(candidate.get("observation_guard")),
                          "experience_uses": deepcopy(candidate.get("experience_uses") or []),
                          "completes_requirements": list(candidate.get("completes_requirements") or []),
                          "requirement_completion_bindings": deepcopy(candidate["requirement_completion_bindings"]),
                          "description": self.describe_step(candidate, expected)})
        anchor = {**self.anchor(), "screen": view.get("screen")}
        previous = self.active_plan
        same = (previous.get("anchor") == anchor
                and [self.step_identity(s) for s in previous["remaining_steps"]]
                == [self.step_identity(s) for s in steps])
        if not same:
            self.plan_revision += 1
        for index, step in enumerate(steps):
            step["step_id"] = previous["remaining_steps"][index]["step_id"] if same else f"{self.plan_revision}:{index}"
        self.active_plan = {"revision": self.plan_revision, "anchor": anchor,
                            "status": "selected", "remaining_steps": steps,
                            "completed_steps": previous.get("completed_steps", []) if previous.get("anchor") == anchor else [],
                            "reason": operation.get("reason") or "", "updated_at": time(),
                            "model_reason": operation.get("model_reason") or operation.get("reason") or "",
                            "solver_adopted": operation.get("solver_adopted", False),
                            "solver_summary": deepcopy(operation.get("solver_summary") or {}),
                            "skip_zero_cost_cards": deepcopy(operation.get("skip_zero_cost_cards") or {}),
                            "experience_uses": deepcopy(operation.get("experience_uses") or [])}
        operation["plan_revision"] = self.plan_revision
        operation["step_id"] = steps[0]["step_id"]
        self.journal.record_plan(self.active_plan, view, self.active_requirements(), self.observed_effects)

    def invalidate_plan(self, reason: str) -> None:
        if self.active_plan.get("status") == "selected" or self.active_plan["remaining_steps"]:
            self.active_plan["status"] = "cancelled"
            self.active_plan["cancelled_steps"] = deepcopy(self.active_plan["remaining_steps"])
            self.active_plan["remaining_steps"].clear()
            self.active_plan["updated_at"] = time()
            self.replacement_reason = reason
            self.journal.cancel_plan(self.plan_revision, reason)

    def speech_kind(self) -> str:
        announced = self.announced_plan
        topic = self.speech_topic()
        if self._last_speech.get("topic") != topic:
            return "initial"
        if not announced or announced.get("anchor") != self.active_plan.get("anchor"):
            return "none"
        if not announced["remaining_steps"]:
            return "none"
        before = [self.step_identity(s) for s in announced["remaining_steps"]]
        after = [self.step_identity(s) for s in self.active_plan["remaining_steps"]]
        # 发言只点名部分步骤时，承诺的步骤在计划里本来就不连续（如点名0/1/3、跳过2）；
        # 只要这些步骤仍按原顺序保留就不改口，被替换或删除才改口。
        # 2026-10-17 20:56:11 曾因连续匹配把“寒流仍按原计划”误报成“改一下”。
        remaining = iter(after)
        matches = all(any(item == candidate for candidate in remaining) for item in before)
        if matches:
            return "none"
        if (self._last_speech.get("kind") == "correction"
                and self._last_speech.get("progress") == self.progress_sequence
                and self._last_speech.get("generation") == self.generation):
            return "none"
        return "correction"

    def speech_topic(self) -> dict[str, Any]:
        anchor = self.anchor()
        return anchor if anchor["battle_id"] else {**anchor, "screen": self.facts.get("screen"), "floor": self.facts.get("floor")}

    def awaiting_chat_reply(self) -> bool:
        user = next((item for item in reversed(self.user_requests)), None)
        return bool(user and user["kind"] not in {"guidance", "immediate"} and not any(
            item["role"] == "assistant" and item["timestamp"] >= user["timestamp"] for item in self.messages))

    def announce_plan(self, operation: dict[str, Any], text: str, kind: str) -> None:
        selected = self.active_plan["remaining_steps"]
        references = operation.get("speech_step_ids")
        steps = selected if references is None else [selected[i] for i in references if i < len(selected)]
        self.announced_plan = {"revision": operation.get("plan_revision"),
                               "anchor": deepcopy(self.active_plan.get("anchor")),
                               "text": text, "kind": kind, "scope": operation.get("speech_scope", "steps"),
                               "remaining_steps": deepcopy(steps), "spoken_at": time()}
        self._last_speech = {"topic": self.speech_topic(), "kind": kind,
                             "progress": self.progress_sequence, "generation": self.generation}

    def complete_step(self, operation: dict[str, Any]) -> None:
        if not operation.get("step_id") or operation.get("plan_revision") != self.active_plan.get("revision"):
            return
        remaining = self.active_plan["remaining_steps"]
        if not remaining or remaining[0]["step_id"] != operation.get("step_id"):
            return
        step = remaining.pop(0)
        self.progress_sequence += 1
        self.active_plan["completed_steps"].append(step)
        self.active_plan.update(status="selected" if remaining else "completed", updated_at=time())
        announced = self.announced_plan.get("remaining_steps", [])
        if announced and self.step_identity(announced[0]) == self.step_identity(step):
            announced.pop(0)

    def decision_state(self) -> dict[str, Any]:
        plans = {}
        for name, plan in (("active_plan", self.active_plan), ("announced_plan", self.announced_plan)):
            exported = dict(plan)
            for field in ("remaining_steps", "completed_steps", "cancelled_steps"):
                if field in plan:
                    exported[field] = [{k: step[k] for k in ("step_id", "description", "reason") if k in step}
                                       for step in plan[field]]
            plans[name] = exported
        return deepcopy({**plans, "replacement_reason": self.replacement_reason,
                         "action_sequence": self.action_sequence})

    def remember(self, role: str, text: str, *, timestamp: float | None = None, kind: str = "pending") -> None:
        if text.strip():
            stamp = time() if timestamp is None else timestamp
            anchor = next((a for a in reversed(self._anchors) if a["timestamp"] <= stamp), {})
            message = {"role": role, "content": text.strip()[:3000], "timestamp": stamp,
                       **{key: anchor.get(key) for key in ("run_id", "battle_id", "turn")}}
            self.messages.append(message)
            if role == "user":
                self.dialogue_revision += 1
                message.update(id=str(self.dialogue_revision), kind=kind)
                self.user_requests.append(message)
                if kind in {"guidance", "immediate"}:
                    self.generation += 1

    def pending_messages(self) -> list[dict[str, Any]]:
        return [dict(item) for item in self.user_requests if item["kind"] == "pending"]

    def resolve_messages(self, effects: dict[str, str]) -> None:
        changed = False
        for item in self.user_requests:
            if item["kind"] == "pending" and item["id"] in effects:
                item["kind"] = effects[item["id"]]
                changed |= item["kind"] in {"guidance", "immediate"}
        if changed:
            self.generation += 1

    # ---- 问题与回答（C03） ----

    def ask_question(self, text: str, *, required: bool, window: float,
                     operation: dict[str, Any] | None = None) -> dict[str, Any]:
        """记录一个待回答的问题；新问题替代旧问题（旧问题标记 superseded）。"""
        text = str(text or "").strip()
        if not text:
            return {}
        if self.pending_question.get("status") == "open":
            self.pending_question.update(status="superseded", superseded_at=time())
        self.pending_question = {
            "id": uuid4().hex[:8], "text": text[:500], "required": bool(required),
            "window": float(window or 0), "status": "open", "asked_at": time(),
            "answered_at": None, "answered_by": [], "anchor": self.anchor(),
            "action_type": str((operation or {}).get("action_type") or ""),
        }
        return deepcopy(self.pending_question)

    def answer_pending_question(self, messages: list[dict[str, Any]]) -> dict[str, Any]:
        question = self.pending_question
        if question.get("status") != "open" or not messages:
            return {}
        question.update(status="answered", answered_at=time(),
                        answered_by=[m.get("id") or m.get("request_id") for m in messages])
        return deepcopy(question)

    def expire_pending_question(self) -> None:
        if self.pending_question.get("status") == "open":
            self.pending_question.update(status="expired", expired_at=time())

    def pending_question_context(self) -> dict[str, Any]:
        question = self.pending_question
        if question.get("status") != "open":
            return {}
        return {key: deepcopy(question[key]) for key in
                ("id", "text", "required", "asked_at", "window", "action_type")}

    def record_action(self, operation: dict[str, Any], *, anchor: dict[str, Any],
                      before: dict[str, Any], after: dict[str, Any], result: dict[str, Any]) -> None:
        kwargs = operation.get("kwargs") or {}
        # 记进历史前剥离执行期专用键（expected_turn / expected_player_id / …）：这些键由插件
        # 在派发前注入，模型不应看见。原样记入会被回喂给决策模型、被学舌复述、再被输出闸拒掉，
        # 形成"总是参数错误"的死循环。出牌下标的读取用原始 kwargs，不受影响。
        recorded_kwargs = public_kwargs(kwargs)
        combat = before.get("combat") or {}
        card = next((item for item in combat.get("hand") or []
                     if item.get("i", item.get("index")) == kwargs.get("card_index")), None) if operation.get("action_type") == "play_card" else None
        target = next((item for item in combat.get("enemies") or []
                       if item.get("i", item.get("index")) == kwargs.get("target_index")), None) if kwargs.get("target_index") is not None else None
        self.actions.append({**anchor, "id": operation.setdefault("record_id", str(uuid4())),
                             "timestamp": time(), "action_type": operation.get("action_type"),
                             "actor": "self", "local_sequence": self.action_sequence + 1,
                             "completes_requirements": list(operation.get("completes_requirements") or []),
                             "requirement_completion_bindings": deepcopy(operation.get("requirement_completion_bindings") or []),
                             "plan_revision": operation.get("plan_revision"), "step_id": operation.get("step_id"),
                             "experience_uses": deepcopy(operation.get("experience_uses") or []),
                             "input_received_at": operation.get("input_received_at"),
                             "kwargs": recorded_kwargs, "reason": operation.get("reason"),
                             "model_reason": operation.get("model_reason") or operation.get("reason"),
                             "card": deepcopy(card), "target": deepcopy(target),
                             "card_entity_id": (operation.get("bindings") or {}).get("card", {}).get("entity_id"),
                             "card_id": (operation.get("bindings") or {}).get("card", {}).get("card_id"),
                             "target_entity_id": (operation.get("bindings") or {}).get("target", {}).get("entity_id"),
                             "before_player": deepcopy(combat.get("player") or {}),
                             "speech": operation.get("speech") or "",
                             "selected_plan": dict(operation.get("future_plan") or {}),
                             "status": "completed" if result.get("executed") else result.get("status"),
                             "executed": result.get("executed", False),
                             "message": result.get("message") or "",
                             "changed": before != after, "outcome": {
                                 "screen": after.get("screen"), "turn": after.get("turn"),
                                 "player": deepcopy((after.get("combat") or {}).get("player")),
                                 "hp": (after.get("run") or {}).get("hp")}})
        if operation.get("action_type") in {"select_deck_card", "confirm_selection"}:
            selection = after.get("selection")
            self.actions[-1]["outcome"].update(selection=deepcopy(selection),
                selection_submitted=bool(result.get("executed") and before.get("selection") and not selection))
        if operation.get("action_type") == "choose_event_option":
            event = after.get("event") or {}
            self.actions[-1]["outcome"].update(event=deepcopy(event),
                event_vote_registered=bool(event.get("is_shared") and not event.get("finished")
                    and event.get("local_vote") == kwargs.get("option_index")))
        self.action_sequence += 1
        self.journal.record_action(self.actions[-1], before, after, operation.get("_execution_effects", self.observed_effects))
        if self.on_action:
            self.on_action(self.actions[-1])
        if result.get("executed"):
            self.complete_step(operation)
            self.confirm_requirement_action(self.actions[-1])

    def select_plan(self, plan: dict[str, Any], *, anchor: dict[str, Any] | None = None) -> None:
        self.future_plan = {**plan, **(anchor or self.anchor()), "selected_at": time(),
                            "through_turn": max(item["turn"] for item in plan["turns"])} if plan else {}

    def context(self) -> dict[str, Any]:
        return {"recent_dialogue": list(self.messages), "current_run": dict(self.facts),
                "input_results": deepcopy(self.input_results),
                "recent_actions": list(self.actions), "future_plan": dict(self.future_plan),
                "decision_state": self.decision_state(),
                "recent_observations": list(self.observations),
                "history_scope": "recent_actions 的 completed/executed=true 是实际完成；decision_state 和历史JSON是计划。当前进度以 current_run 和最新 agent_view 为准。",
                "player_messages": [dict(item) for item in self.user_requests if item["kind"] == "guidance"],
                "local_pending_messages": [dict(item) for item in self.user_requests
                                           if item["kind"] == "pending" and not item.get("request_id")],
                "pending_inputs": [dict(item) for item in self.user_requests if item["kind"] == "pending"],
                "pending_question": self.pending_question_context(),
                "character_choice_made": self.character_choice_made,
                "requirements": deepcopy(self.requirements.records), "requirements_revision": self.requirements_revision,
                "requirement_sequence": self.requirements.sequence,
                "battle_number": self._battle_number, "in_combat": self._in_combat,
                "terminal_reply_run_id": self.terminal_reply_run_id}

    def save_input_result(self, messages: list[dict], text: str, *, media: list, kind: str) -> dict:
        ids = [m.get("request_id") or m["id"] for m in messages]
        existing = next((r for r in self.input_results if r["ids"] == ids), None)
        if existing:
            return existing
        record = {"ids": ids, "messages": deepcopy(messages), "reply": text,
                  "media": deepcopy(media), "kind": kind, "output_id": "reply:" + ":".join(ids),
                  "understood": True, "effects_saved": True, "text_sent": False,
                  "audio_completed": None, "anchor": self.anchor()}
        self.input_results.append(record)
        # 未送达的结果始终保留，已送达结果只需近期去重窗口。
        delivered = [r for r in self.input_results if r["text_sent"]][-20:]
        self.input_results = [r for r in self.input_results if not r["text_sent"]] + delivered
        self.delivery_revision += 1
        return record

    def import_dialogue(self, records: list[Any], *, character_name: str) -> None:
        if character_name != self._character_name:
            if self._character_name:
                self.messages.clear()
                self.user_requests.clear()
            self._seen_records.clear()
            self._character_name = character_name
            self.generation += 1
        for record in sorted(records, key=lambda item: item.timestamp or 0):
            if not record.timestamp or record.timestamp < self.started_at:
                continue
            if record.lanlan_name != character_name:
                continue
            # 宿主的 user/assistant 实际发言；不把插件事件提示当作玩家说过的话。
            role = {"user_message": "user", "assistant_message": "assistant",
                    "proactive_reply": "assistant"}.get(record.turn_type)
            if role is None or not record.content:
                continue
            key = f"{record.timestamp}|{record.turn_type}|{record.content}"
            if key in self._seen_records:
                continue
            self._seen_records.append(key)
            # 导入时间不能冒充发言时间；早于可核实观测的记录保留未知回合。
            self.remember(role, record.content, timestamp=record.timestamp or 0)
