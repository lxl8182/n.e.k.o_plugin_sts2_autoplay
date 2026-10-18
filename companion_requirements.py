from __future__ import annotations

from copy import deepcopy
from time import time
from typing import Any

from .action_identity import action_refs
from .planner_interface import public_kwargs


LIVE = {"pending", "active", "waiting"}
SCOPES = {"action", "turn", "battle", "scene", "run"}
# 场景内的子页面（营火→选牌/升级、宝箱→奖励、看牌页）不结束场景要求：
# 02:35:06 第15层营火进入升级选牌页即把 req-4 判为过期，随后第23层又拿旧约定当依据。
_CHILD_SCREENS = frozenset({"CARD_SELECTION", "CARD_SELECTION_REMOVE", "CARD_SELECTION_UPGRADE",
                            "CARD_SELECTION_REWARD", "CARDS_VIEW", "REWARD", "BUNDLE"})


def _scene(screen: str | None) -> str | None:
    return {"REST_SITE": "REST"}.get(screen, screen)


def _events(snapshot: dict) -> list[dict]:
    raw = snapshot.get("raw_state") or {}
    return raw.get("action_results") or (raw.get("combat") or {}).get("action_results") or []


def _subject(condition: dict, snapshot: dict) -> dict | None:
    combat = (snapshot.get("raw_state") or {}).get("combat") or {}
    subject = condition.get("subject", "self")
    if subject == "enemy":
        return next((e for e in combat.get("enemies") or []
                     if str(e.get("entity_id")) == condition.get("target_entity_id")
                     and e.get("is_alive", True)), None)
    if subject == "master":
        return next((p for p in combat.get("players") or [] if p.get("is_local") is False), None)
    return combat.get("player")


def condition_result(condition: dict, snapshot: dict, actions: list[dict]) -> tuple[bool, dict]:
    """核对有限现场事实；动作完成只读取明确完成记录。"""
    kind = condition["kind"]
    if kind == "screen":
        actual = (snapshot.get("agent_view") or {}).get("screen", snapshot.get("screen"))
        return _scene(actual) == _scene(condition["value"]), {"kind": kind, "actual": actual}
    if kind == "action_result":
        records = _events(snapshot) if condition.get("actor", "self") == "master" else [*_events(snapshot), *actions]
        for record in reversed(records):
            if record.get("actor", "self") != condition.get("actor", "self"):
                continue
            sequence_key = "local_sequence" if record.get("local_sequence") is not None else "sequence"
            baseline = condition.get("after_local_sequence", 0) if sequence_key == "local_sequence" else condition.get("after_sequence", 0)
            if record.get(sequence_key, 0) <= baseline:
                continue
            if record.get("status") != "completed" or record.get("executed") is False:
                continue
            if any(record.get(key) != condition[key] for key in ("action_type", "card_entity_id", "card_id", "target_entity_id") if key in condition):
                continue
            if any((record.get("kwargs") or {}).get(key) != value for key, value in (condition.get("kwargs") or {}).items()):
                continue
            return True, {"kind": kind, "sequence": record.get(sequence_key), "record": deepcopy(record)}
        return False, {"kind": kind, "reason": "等待新的实际完成记录"}
    if kind == "card":
        combat = (snapshot.get("raw_state") or {}).get("combat") or {}
        zone = condition["zone"]
        if zone not in combat:
            return False, {"kind": kind, "missing": zone}
        present = any(str(c.get("entity_id")) == condition["card_entity_id"] for c in combat[zone])
        return present == condition.get("present", True), {"kind": kind, "present": present}
    entity = _subject(condition, snapshot)
    if entity is None:
        return False, {"kind": kind, "missing": "等待对象的现场事实"}
    if kind == "power":
        if "powers" not in entity:
            return False, {"kind": kind, "missing": "powers"}
        power = next((p for p in entity["powers"] if p.get("power_id") == condition["power_id"]), None)
        actual = power.get("amount") if power else 0
    else:
        actual = entity.get(condition["field"])
    if actual is None:
        return False, {"kind": kind, "missing": condition.get("field", "amount")}
    op, value = condition.get("op", "gte"), condition["value"]
    result = actual == value if op == "eq" else actual >= value if op == "gte" else actual <= value
    return result, {"kind": kind, "actual": actual, "op": op, "value": value}


class RequirementBook:
    """独立于聊天窗口的局内要求；程序只维护期限、实体和可核对结果。"""

    def __init__(self) -> None:
        self.records: list[dict] = []
        self.revision = 0
        self.sequence = 0

    def clear(self) -> None:
        self.records.clear()
        self.sequence = 0
        self.revision += 1

    def active(self) -> list[dict]:
        return deepcopy([r for r in self.records if r["status"] in LIVE])

    def states(self, limit: int = 12) -> list[dict[str, Any]]:
        """给历史摘要的紧凑状态：旧原话可追问，但已结束的要求不再有约束力。"""
        return [{"id": r["id"], "text": r["text"][:60], "scope": r["scope"],
                 "strength": r["strength"], "status": r["status"]} for r in self.records[-limit:]]

    def bind_action_completions(self, operation: dict, anchor: dict) -> None:
        ids = operation.get("completes_requirements") or []
        if not isinstance(ids, list) or any(not isinstance(identifier, str) for identifier in ids) or len(set(ids)) != len(ids):
            raise ValueError("completes_requirements 必须为不重复的要求ID数组")
        ids = [*ids, *(r["id"] for r in self.records if r["id"] not in ids and r["status"] == "active"
                      and r.get("action_completion") == {"action_type": operation.get("action_type"),
                                                         "kwargs": public_kwargs(operation.get("kwargs"))})]
        operation["completes_requirements"] = ids
        bindings = []
        for identifier in ids:
            record = next((r for r in self.records if r["id"] == identifier), None)
            if (record is None or record["status"] != "active" or record["behavior"]["kind"] != "intent"
                    or record["anchor"].get("run_id") != anchor.get("run_id") or not operation.get("action_type")):
                raise ValueError("completes_requirements 只能绑定本局当前生效的intent要求")
            bindings.append({"id": identifier, "created_at": record["created_at"], "run_id": anchor.get("run_id"),
                             "action_type": operation["action_type"], "kwargs": public_kwargs(operation.get("kwargs"))})
        operation["requirement_completion_bindings"] = bindings

    def confirm_action(self, action: dict) -> bool:
        """只用已绑定且实际完成的动作结束意图；过渡到期不抢走完成回执。"""
        if action.get("status") != "completed" or action.get("executed") is not True:
            return False
        changed = False
        ids = action.get("completes_requirements") or []
        for binding in action.get("requirement_completion_bindings") or []:
            if binding["id"] not in ids or binding["run_id"] != action.get("run_id"):
                continue
            if binding["action_type"] != action.get("action_type") or binding["kwargs"] != public_kwargs(action.get("kwargs")):
                continue
            record = next((r for r in self.records if r["id"] == binding["id"] and r["created_at"] == binding["created_at"]), None)
            if (record is None or record["behavior"]["kind"] != "intent" or record["anchor"].get("run_id") != action.get("run_id")):
                continue
            expired_by_transition = record["status"] == "expired" and record.get("evidence", {}).get("kind") in {"scene_left", "turn_ended", "battle_ended"}
            if record["status"] not in LIVE and not expired_by_transition:
                continue
            record.update(status="completed", updated_at=time(), evidence={"kind": "confirmed_action",
                          "action_type": action["action_type"], "kwargs": deepcopy(action.get("kwargs") or {}),
                          "local_sequence": action.get("local_sequence"), "step_id": action.get("step_id")})
            changed = True
        if changed:
            self.revision += 1
        return changed

    @staticmethod
    def _bind(item: dict, refs: dict) -> None:
        for field, collection, output in (("card_ref", "cards", "card_entity_id"), ("target_ref", "enemies", "target_entity_id")):
            if field not in item:
                continue
            entity = next((entry for entry in refs[collection] if entry["ref"] == item[field]), None)
            if entity is None:
                raise ValueError(f"requirement {field} 引用了未知实体")
            item[output] = entity["entity_id"]

    @classmethod
    def _condition(cls, item: dict, refs: dict, snapshot: dict, action_sequence: int) -> dict:
        if not isinstance(item, dict) or item.get("kind") not in {"power", "player", "screen", "action_result", "card"}:
            raise ValueError("不支持的 requirement condition")
        item = deepcopy(item)
        cls._bind(item, refs)
        kind = item["kind"]
        if item.get("subject", "self") not in {"enemy", "self", "master"}:
            raise ValueError("condition.subject 必须为 enemy/self/master")
        if kind in {"power", "player"}:
            if item.get("op", "gte") not in {"gte", "eq", "lte"} or type(item.get("value")) not in {int, float}:
                raise ValueError("condition 需要数值 value 和 gte/eq/lte")
            if kind == "power" and not isinstance(item.get("power_id"), str):
                raise ValueError("power condition 需要现场 power_id")
            if item.get("subject") == "enemy" and not item.get("target_entity_id"):
                raise ValueError("enemy condition 需要 target_ref")
            if kind == "player" and item.get("field") not in {"energy", "block", "current_hp", "max_hp"}:
                raise ValueError("player condition.field 不支持")
        elif kind == "screen" and not isinstance(item.get("value"), str):
            raise ValueError("screen condition 需要 value")
        elif kind == "action_result":
            if item.get("actor", "self") not in {"self", "master"} or not item.get("action_type"):
                raise ValueError("action_result 需要 self/master actor 和 action_type")
            if not isinstance(item.get("kwargs", {}), dict):
                raise ValueError("action_result.kwargs 必须为对象")
            item["after_sequence"] = max((e.get("sequence", 0) for e in _events(snapshot)), default=0)
            item["after_local_sequence"] = action_sequence
            item["events_epoch"] = (snapshot.get("raw_state") or {}).get("action_results_epoch")
        elif kind == "card":
            if item.get("subject", "self") != "self" or item.get("zone") not in {"hand", "discard", "exhaust"} or not item.get("card_entity_id"):
                raise ValueError("card condition 需要自身实体及 hand/discard/exhaust zone")
            if type(item.get("present", True)) is not bool:
                raise ValueError("card.present 必须为布尔值")
        return item

    def apply(self, updates: list, *, snapshot: dict, anchor: dict, messages: list[dict], action_sequence: int,
              operation: dict | None = None) -> bool:
        if not isinstance(updates, list) or len(updates) > 16:
            raise ValueError("requirement_updates 必须为最多16项的数组")
        if not updates:
            return False
        # 整批先校验，防止第二条失败后留下半份更新。
        records, sequence = deepcopy(self.records), self.sequence
        refs = action_refs(snapshot)
        for update in updates:
            if not isinstance(update, dict) or update.get("op") not in {"add", "cancel", "replace"}:
                raise ValueError("requirement update.op 必须为 add/cancel/replace")
            update = deepcopy(update)
            behavior_fields = update.get("behavior") or {}
            if not isinstance(behavior_fields, dict):
                raise ValueError("requirement behavior 必须为对象")
            # 模型会把条件写在 behavior 内；在输入边界归一位置，保留同一套语义校验。
            for field in ("conditions", "completion", "release_by_player"):
                if field in behavior_fields:
                    value = behavior_fields.pop(field)
                    if field in update and update[field] != value:
                        raise ValueError(f"requirement {field} 在顶层与behavior中冲突")
                    update[field] = value
            source = next((m for m in messages if m.get("id") == str(update.get("source_message_id"))), None)
            if source is None:
                raise ValueError("requirement update 需要有效 source_message_id")
            previous = None
            if update["op"] in {"cancel", "replace"}:
                previous = next((r for r in records if r["id"] == update.get("id") and r["status"] in LIVE), None)
                if previous is None:
                    raise ValueError("requirement update.id 不是有效要求")
            if update["op"] == "replace":
                for field in ("behavior", "scope", "strength", "scene", "activate_turn", "through_turn", "release_by_player"):
                    if field not in update and field in previous:
                        update[field] = deepcopy(previous[field])
                if not isinstance(update["behavior"], dict) or not update["behavior"].get("kind"):
                    raise ValueError("replace.behavior 需明确kind及对应约束，省略则保留原行为")
            new = None
            if update["op"] != "cancel":
                scope, behavior = update.get("scope", "run"), deepcopy(update.get("behavior") or {"kind": "intent"})
                if scope not in SCOPES or not isinstance(update.get("text"), str) or not update["text"].strip():
                    raise ValueError("requirement 需要 text 和有效 scope")
                strength = {"must": "hard", "prefer": "preference"}.get(update.get("strength"), update.get("strength", "hard"))
                if strength not in {"hard", "preference"} or behavior.get("kind") not in {"intent", "avoid_target", "only_target", "reserve", "wait"}:
                    raise ValueError("requirement strength/behavior.kind 不支持")
                self._bind(behavior, refs)
                if any(not isinstance(behavior.get(k, []), list) or any(not isinstance(v, str) for v in behavior.get(k, [])) for k in ("action_types", "card_types")):
                    raise ValueError("behavior action_types/card_types 必须为字符串数组")
                if behavior["kind"] in {"avoid_target", "only_target"} and not behavior.get("target_entity_id"):
                    raise ValueError("目标分工需要 target_ref")
                if behavior["kind"] == "reserve":
                    if not any(k in behavior for k in ("card_entity_id", "potion_index", "resource")):
                        raise ValueError("reserve 需要牌、药水或资源")
                    if "potion_index" in behavior:
                        potion = next((p for p in (snapshot.get("raw_state") or {}).get("run", {}).get("potions", []) if p.get("index") == behavior["potion_index"] and p.get("occupied")), None)
                        if potion is None:
                            raise ValueError("reserve 药水槽中没有药水")
                        behavior["potion_id"] = potion.get("potion_id")
                    if "resource" in behavior and (behavior["resource"] != "energy" or type(behavior.get("min")) not in {int, float} or behavior["min"] < 0):
                        raise ValueError("reserve resource 支持 energy 和非负 min")
                if previous is not None and update["op"] == "replace" and "conditions" not in update:
                    conditions = deepcopy(previous["conditions"])
                else:
                    conditions = update.get("conditions") or []
                    if not isinstance(conditions, list) or len(conditions) > 8:
                        raise ValueError("requirement.conditions 必须为最多8项的数组")
                    conditions = [self._condition(c, refs, snapshot, action_sequence) for c in conditions]
                if behavior["kind"] == "wait" and not conditions and update.get("release_by_player") is not True:
                    raise ValueError("wait 需要可观察条件或 release_by_player:true")
                fixed_anchor = {**anchor, **{k: source[k] for k in ("run_id", "battle_id", "turn") if source.get(k) is not None}}
                activate = update.get("activate_turn", fixed_anchor.get("turn"))
                through = update.get("through_turn", activate if scope == "turn" else None)
                if any(v is not None and (type(v) is not int or v < 0) for v in (activate, through)):
                    raise ValueError("requirement turn 必须为非负整数")
                if scope in {"turn", "battle"} and not fixed_anchor.get("battle_id"):
                    raise ValueError("turn/battle requirement 需要当前战斗锚点")
                if scope in {"turn", "battle"} and (snapshot.get("agent_view") or {}).get("screen") != "COMBAT":
                    raise ValueError("当前战斗已结束；后续战斗也适用的偏好请用scope=run，不能绑定已结束战斗")
                if scope == "scene" and not isinstance(update.get("scene"), str):
                    raise ValueError("scene requirement 需要 scene")
                sequence += 1
                new = {"id": f"req-{sequence}", "text": update["text"].strip(), "source_message_id": source["id"],
                       "source_text": source["content"], "created_at": time(), "anchor": fixed_anchor, "scope": scope,
                       "strength": strength, "activate_turn": activate, "through_turn": through, "scene": update.get("scene"),
                       "behavior": behavior, "conditions": conditions, "release_by_player": update.get("release_by_player", False),
                       "status": "pending", "evidence": {"kind": "user_message", "id": source["id"]}}
                if scope == "action":
                    view = snapshot.get("agent_view") or {}
                    new["action_scene"] = {"screen": view.get("screen"), "floor": (view.get("run") or {}).get("floor", view.get("floor"))}
                    if behavior["kind"] == "intent" and not update.get("completion") and operation and operation.get("action_type"):
                        new["action_completion"] = {"action_type": operation["action_type"], "kwargs": public_kwargs(operation.get("kwargs"))}
                if update.get("completion") is not None:
                    new["completion"] = self._condition(update["completion"], refs, snapshot, action_sequence)
                elif previous is not None and "completion" not in update and "completion" in previous:
                    new["completion"] = deepcopy(previous["completion"])
            if previous is not None:
                previous.update(status="cancelled" if update["op"] == "cancel" else "replaced",
                                evidence={"kind": "user_message", "id": source["id"], "text": source["content"]}, updated_at=time())
            if new is not None:
                records.append(new)
        self.records, self.sequence = records, sequence
        self.revision += 1
        return True

    def refresh(self, snapshot: dict, anchor: dict, actions: list[dict]) -> bool:
        before = deepcopy(self.records)
        view = snapshot.get("agent_view") or {}
        raw = snapshot.get("raw_state") or {}
        combat = raw.get("combat") or {}
        for record in self.records:
            if record["status"] not in LIVE:
                continue
            # 游戏进程重启会重建历史；先锚定新进程，旧记录不充当新完成。
            for condition in [*record["conditions"], *([record["completion"]] if record.get("completion") else [])]:
                if condition["kind"] == "action_result" and condition.get("events_epoch") != raw.get("action_results_epoch"):
                    condition["events_epoch"] = raw.get("action_results_epoch")
                    condition["after_sequence"] = max((e.get("sequence", 0) for e in _events(snapshot)), default=0)
            scope, original = record["scope"], record["anchor"]
            status, evidence = "active", {}
            turn = anchor.get("turn")
            floor = (view.get("run") or {}).get("floor", view.get("floor"))
            # 父场景的子页面（营火→选牌/升级、宝箱→奖励）仍是同一流程：同层内换页不结束要求。
            scene_child = bool(scope == "scene" and record.get("scene_entered")
                               and record.get("scene_floor") is not None
                               and floor == record["scene_floor"]
                               and str(view.get("screen") or "").upper() in _CHILD_SCREENS
                               and _scene(view.get("screen")) != _scene(record["scene"]))
            completion = record.get("completion")
            complete, completion_evidence = condition_result(completion, snapshot, actions) if completion else (False, {})
            if original.get("run_id") != anchor.get("run_id"):
                status, evidence = "expired", {"kind": "run_changed"}
            elif complete and not (scope == "turn" and turn is not None and record.get("activate_turn") is not None and turn < record["activate_turn"]):
                status, evidence = "completed", completion_evidence
            elif scope in {"turn", "battle"} and original.get("battle_id") != anchor.get("battle_id"):
                status, evidence = "expired", {"kind": "battle_ended", "anchor": anchor}
            elif scope == "turn" and turn is not None and record.get("through_turn") is not None and turn > record["through_turn"]:
                status, evidence = "expired", {"kind": "turn_ended", "turn": turn}
            elif scope == "action" and record.get("action_scene") and record["action_scene"] != {
                    "screen": view.get("screen"), "floor": (view.get("run") or {}).get("floor", view.get("floor"))}:
                status, evidence = "expired", {"kind": "scene_left"}
            elif scope == "scene" and scene_child:
                status = "active" if record.get("scene_entered") else "pending"
                evidence = {"kind": "scene_child", "screen": view.get("screen"), "floor": floor,
                            "scene": record["scene"]}
            elif scope == "scene" and _scene(view.get("screen")) != _scene(record["scene"]):
                status = "expired" if record.get("scene_entered") else "pending"
                evidence = {"kind": "scene_left" if record.get("scene_entered") else "await_scene", "screen": view.get("screen")}
            elif scope in {"turn", "battle"} and turn is not None and record.get("activate_turn") is not None and turn < record["activate_turn"]:
                status, evidence = "pending", {"kind": "await_turn", "turn": record["activate_turn"]}
            if status == "active":
                if scope == "scene":
                    record["scene_entered"] = True
                    if record.get("scene_floor") is None and _scene(view.get("screen")) == _scene(record["scene"]):
                        record["scene_floor"] = floor
                if not complete:
                    behavior = record["behavior"]
                    identities = [behavior, *record["conditions"]]
                    missing_target = next((b["target_entity_id"] for b in identities if b.get("target_entity_id") and not any(
                        str(e.get("entity_id")) == b["target_entity_id"] and e.get("is_alive", True) for e in combat.get("enemies") or [])), None)
                    if missing_target and view.get("screen") == "COMBAT":
                        status, evidence = "unavailable", {"kind": "target_missing", "entity_id": missing_target}
                    elif behavior["kind"] == "reserve" and "potion_index" in behavior:
                        potion = next((p for p in (raw.get("run") or {}).get("potions") or [] if p.get("index") == behavior["potion_index"]), {})
                        if not potion.get("occupied") or potion.get("potion_id") != behavior.get("potion_id"):
                            status, evidence = "unavailable", {"kind": "potion_missing", "index": behavior["potion_index"]}
                    elif behavior["kind"] == "reserve" and behavior.get("card_entity_id"):
                        zones = [combat.get(zone) for zone in ("hand", "discard", "draw", "exhaust")]
                        if all(zone is not None for zone in zones) and not any(str(c.get("entity_id")) == behavior["card_entity_id"] for zone in zones for c in zone):
                            status, evidence = "unavailable", {"kind": "card_missing", "entity_id": behavior["card_entity_id"]}
                    if status == "active" and behavior["kind"] == "wait":
                        checks = [condition_result(c, snapshot, actions) for c in record["conditions"]]
                        if not checks or not all(ok for ok, _ in checks):
                            status = "waiting"
                        evidence = {"kind": "conditions", "checks": [result for _, result in checks], "satisfied": status == "active"}
            if record["status"] != status or record.get("evidence") != evidence:
                record.update(status=status, evidence=evidence, updated_at=time())
        changed = before != self.records
        if changed:
            self.revision += 1
        return changed

    def blocker(self, operation: dict, snapshot: dict) -> dict | None:
        refs = action_refs(snapshot)
        kwargs = operation.get("kwargs") or {}
        bound = operation.get("bindings") or {}
        card = bound.get("card") or next((c for c in refs["cards"] if c["index"] == kwargs.get("card_index")), {})
        target = bound.get("target") or next((e for e in refs["enemies"] if e["index"] == kwargs.get("target_index")), {})
        action_type = operation.get("action_type")
        combat = (snapshot.get("raw_state") or {}).get("combat") or {}
        actual_card = next((c for c in combat.get("hand") or [] if str(c.get("entity_id")) == card.get("entity_id")), {})
        all_enemies = action_type == "play_card" and actual_card.get("target_type") == "AllEnemies"
        if action_type == "use_potion":
            potion = next((p for p in ((snapshot.get("raw_state") or {}).get("run") or {}).get("potions") or []
                           if p.get("index") == kwargs.get("option_index")), {})
            if potion.get("target_index_space") == "players":
                target = {}
            all_enemies = potion.get("target_type") == "AllEnemies"
        for record in self.records:
            if record["status"] not in {"active", "waiting"} or record["strength"] != "hard":
                continue
            behavior = record["behavior"]
            kind = behavior["kind"]
            if behavior.get("action_types") and action_type not in behavior["action_types"]:
                continue
            types = behavior.get("card_types")
            if types and (action_type != "play_card" or (card.get("card_type") and card["card_type"].casefold() not in {t.casefold() for t in types})):
                continue
            same_card = bool(behavior.get("card_entity_id") and behavior["card_entity_id"] == card.get("entity_id"))
            same_target = bool(behavior.get("target_entity_id") and behavior["target_entity_id"] == target.get("entity_id"))
            blocked = False
            if kind == "wait":
                if behavior.get("card_entity_id") and not same_card:
                    continue
                if behavior.get("target_entity_id") and not (same_target or all_enemies):
                    continue
                blocked = record["status"] == "waiting"
            elif kind == "avoid_target":
                blocked = same_target or all_enemies
            elif kind == "only_target":
                blocked = all_enemies or (action_type in {"play_card", "use_potion"} and bool(target) and not same_target)
            elif kind == "reserve":
                blocked = (same_card and action_type == "play_card") or ("potion_index" in behavior and
                           action_type in {"use_potion", "discard_potion"} and kwargs.get("option_index") == behavior["potion_index"])
                if behavior.get("resource") == "energy" and action_type == "play_card":
                    cost = actual_card.get("energy_cost")
                    energy = (combat.get("player") or {}).get("energy")
                    if actual_card.get("costs_x"):
                        cost = energy
                    blocked = cost is None or energy is None or energy - max(0, cost) < behavior["min"]
            if blocked:
                return {"code": "requirement_wait" if kind == "wait" else "requirement_constraint", "requirement_id": record["id"],
                        "reason": record["text"], "status": record["status"], "evidence": deepcopy(record["evidence"])}
        return None
