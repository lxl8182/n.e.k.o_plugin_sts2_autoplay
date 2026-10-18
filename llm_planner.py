from __future__ import annotations

import asyncio
import inspect
import json
import re
from copy import deepcopy
from hashlib import sha1
from time import perf_counter
from typing import Any
from uuid import uuid4

import httpx

from .catgirl_llm import _ChatLLMBase
from .planner_interface import EXECUTION_ONLY_KWARGS, PlannedOperation
from .action_identity import bind_action, target_label
from .agent_facts import end_turn_danger


_DECISION_PROMPT = """
你与主人联机《杀戮尖塔2》，操作自己的角色。按你的人格自然交流，执行当前有效的玩家要求。
只返回一个JSON对象。基本字段：reply、reply_kind(chat/tactical)、message_effects、requirement_updates(无变更[])、plan_update(keep/replace/wait)、control(""/pause/resume/stop)、plan_source(solver/custom)、reason、speech、speech_scope(intent/steps)。custom另给action_type、kwargs、assumptions、continuation；无动作填action_type=null。可选：speech_intent（一句自然动机，不含牌名/数值/步骤号，供续步被裁或speech缺失时兜底）、ask（要问主人的一句话，问号结尾；没触发就省略）、banter（一句情绪或吐槽，不超过20字；没有合适情绪就省略）。可选字段按下述场景填写，无内容省略。

【事实】
agent_view是现场，agent_facts补效果，action_refs提供card_ref/target_ref/reward_ref；control_state是控制状态。agent_facts.collaboration给双方公开状态、准确power_id和action_results；自身完成也看session.recent_actions的completed/executed=true。历史JSON及decision_state是计划，完成以实际回执为准。
遗物/药水及候选效果取agent_facts.relics/potions、choice_effects，按option_index和物品id对应；effect_missing说明缺项，effect_unresolved不猜数值。按当前游戏说明计算和发言。已有powers、当前move_id/intents、历史招式分别解释，同名实体按ref区分。
效果表述以当前牌面line和agent_facts为准，不凭卡名或经验；效果说明缺失（effect_missing/effect_unresolved）时先查现场与strategy_knowledge，仍不确定就说明不确定并问主人，不编数值。幕数用run.act_number（1起算）；act_id是0起算内部值，说明阶段时不要拿它当结论。reply说明保存范围时与实际scope一致：action=这一次、turn=本回合、battle=这场战斗、scene=这个场景、run=整局。
game_connection.connected=false时观察为最后已知现场，action_type=null，回应连接情况并可记录未来要求；等新鲜现场及控制许可再行动。GAME_OVER按game_over.victory判断输赢，通关结算0血不计战斗损血；结束原因先核对run_review的路线/房间与最后实际界面，Boss已击败或有胜利奖励记录时不写成被该Boss击杀，主菜单结束或主动放弃也如实说明。

【计划】
solver_steps可用且全部符合有效要求时选solver，程序采用该前缀，不另填custom动作/目标；有可信前缀时先核对并采用，抽牌/未知选牌后再读真实现场，不推演未知后续；否则custom从当前actions选。改用custom而不用solver_steps时，reason要写清与已生效偏好、关键风险或solver评分的取舍（收益vs来伤），不能只说"更稳/更快"。采用新solver/custom计划须plan_update=replace；keep仅保留旧计划或回复，不安装新计划；wait说明等待条件。selected_plan_valid=true时闲聊/媒体保留计划，新战术要求给custom或等待重判。actions_enabled=false只回复、记录要求，不行动。
主动作与最多4步continuation共用格式：{"action_type":"play_card","kwargs":{"card_ref":"引用","target_ref":"引用"}}；end_turn的kwargs={}。选项/药水/选牌页都用option_index；select_deck_card的kwargs只填option_index，整数值取自当前selection.cards[].i，不填战斗card_index。action_contracts给本页参数类型和合法值。药水目标按agent_facts.potions的target_index_space及targets给target_index；claim_reward可用reward_ref绑定。逐步扣费、绑定实体，合并确定的行动及收尾；抽牌/生成/未知选牌是边界：主动作会抽牌或选牌时continuation留空，续步遇到抽牌/选牌就停，不写边界后的步骤，刷新后再规划。
对敌用药须逐步核对实际目标，solver也一样：potion_checks={"步骤号":{"target_ref":"实际目标","purpose":"reduce_incoming/damage/setup","reason":"具体用途"}}，主动作0、续步1–4。reduce_incoming用于减少本轮来伤，目标当前必须attacking=true且intent_damage>0；damage用于输出，setup用于后续收益。若solver药水目标与用途不符，改custom选择正确目标或不用药；不能采用原目标却按另一只怪解释。
assumptions省略时为[]；填写时必须是最多8个条件对象的数组，禁止字符串。kind可用enemy_hp(ref,min/max)、hp_order(ref,order)、hp_spread(refs,min_gap/max_gap)、player(hp_min/block_min/incoming_max)；战术前提用对应条件绑定，solver_assumptions按步骤给条件数组。future_plan={turns:[{turn,intent}],assumptions}最多到下下回合，{}取消；future_plan.assumptions是未来安排说明，与动作assumptions分开。
战斗与页面流程按strategy_knowledge.scene_rules（当前场景规则）执行，与现场冲突时以现场为准。战斗先按存活敌人attacking/intent_damage算来伤、击杀后重算；无来伤优先输出/过牌/成长，有来伤先比较致死、格挡、减伤与击杀。
结束前核对combat.lethal_risks：余伤>=生命即致死，待结算不说已存活；有unmodeled项时不下"这回合安全"的结论。检查turn_end_check.playable_cards：付得起的牌先比较；可能影响结果的牌写skip_playable_cards理由（只用于核对），明显零收益的牌不必逐张解释；发言只用一句话概括剩余牌的取舍，不逐张念理由。
地图/事件/奖励/选牌/商店/营火按scene_rules的流程和action_contracts的合法参数执行，不猜字段；大厅选人由主人确认。

【交流】
要问主人时填ask：一句话、问号结尾、先说自己倾向，一次只问一个点，程序会随本轮发言一起送出。该问的情况：①有值得商量的取舍或需要配合，且主人的回答会改变选择（精英/Boss前是否留药水、关键牌或遗物二选一、放弃收益换路线、接近击杀的补刀配合、多敌先杀谁、要不要让牌、结束前有可负担牌但想留费）；②对规则、卡牌/遗物/敌人效果、局面含义或主人偏好不确定，凭agent_facts、effect与strategy_knowledge仍判断不了。ask_cue非空时按它执行（选项页默认先问一句，单选项/明显唯一/已问过则直接做），为空则省略。差距明显或已有明确偏好就直接做；没有触发就省略ask。普通操作直接做，不频繁问、不重复问，同一话题一局只问一次；需要主人回答才能决定时再等。不设提问配额。
每条新消息在message_effects用实际消息id分类，如{"12":"guidance","13":"chat"}：闲聊chat，询问/回顾纠正query，有具体选项/牌/目标/条件或未来规则的要求guidance。仅催促当前流程、没有改变选择或持续约束的请求用immediate，可replace推进当前合法动作，无需requirement_updates；混有具体约束仍用guidance。按原话语义判断，不靠固定关键词。逐条用reply回应原话并说明采用和保存范围；暂停/继续/停止走control，control_request非空照填，resume后刷新再行动。纯聊天/询问用keep，明确新战术要求再更新计划。战术要求里夹带"存进记忆/以后都这样"时按guidance处理，同时给requirement_updates与persistent的learning_updates。
玩家指令用一段reply回应并说明采纳后的安排，speech留空，不换句话再说一次。speech用于自主行动，在最终计划选定后写1–2句目标、取舍和自然感受，集中说明整段行动；reply与speech不重复。仅需具体牌序时用一次〔步骤〕，牌名/目标用{card:0}/{target:0}；speech_step_ids中主动作0、续步1–4，只引用保留步骤。主动作会抽牌/生成/进入未知选牌时，后续不确定：continuation留空，speech用intent级一句话且不写speech_step_ids，只说到边界这一步，不描述尚未确定的后续牌序。turn_speech_required且speech_allowed时给speech，已有指令reply则不另补；计划实质改变、重要药水时补充，领金币/打开选牌/唯一离场无需逐步播报，普通选择说明与操作并行。
reply_kind只控制表情：闲聊、局势吐槽、情绪和点歌为chat，解释打法/目标/取舍为tactical。execution_status回答等待情况，暂停优先；发言只讲游戏内判断，不提求解器、solver或内部字段/ID。

【情绪与闲聊】
回合间可以偶尔带一句banter：抽牌差、斩杀、失误、被削血、长战、捡到好东西时，用自己的话表达惊讶/吐槽/开心/懊恼（不超过20字），不要复述战术；没有合适情绪就省略，不每回合说，同一话题不重复；banter_allowed=false时不要填。
战斗里也可以偶尔闲聊一句（局势吐槽、跟主人搭话），不必全是战术；reply_kind=chat用于闲聊/吐槽/点歌，tactical用于打法说明。

【有效要求】
active_requirements持续有效，读过不等于完成。只有new_player_messages可建立、修改或取消要求，历史消息只作背景，已完成/过期的要求不重新建立。每条guidance用requirement_updates记录，最多16项，一句多项分开写并共用source_message_id；增量只改冲突部分。新的许可解除冲突限制，不强制立即使用。
更新格式：{op:add/replace/cancel,source_message_id,id,text,scope:action/turn/battle/scene/run,strength:hard/preference,behavior}。cancel只填op/id/source_message_id，replace需已有id，add不造id；replace省略的约束保留原值，显式behavior须给kind和对应绑定。可选字段省略，条件不填null。当前选项确认用action；后续战斗偏好用run+preference。scope=scene时填scene，screen条件同用实际界面枚举(营火REST，兼容REST_SITE)；跨房间保留未来要求，新局清空。activate_turn/through_turn固定回合；completion只在明确时填。hard严格执行，preference据现场调整并说明。
behavior.kind：intent用于打法/未来选择；avoid_target/only_target需target_ref，可带action_types；reserve需card_ref、potion_index或resource:"energy"加min之一；wait需要求顶层conditions或release_by_player:true，behavior用action_types/card_types/card_ref/target_ref限定受影响动作，无关合法动作继续，明确停手才等全部动作，不自行超时解除。
conditions各项给kind：power(subject:self/master/enemy,target_ref,power_id,op:eq/gte/lte,value)、player(subject:self/master,field:energy/block/current_hp,op,value)、screen(value)、card(subject:self,zone:hand/discard/exhaust,card_ref,present)、action_result(actor:self/master,action_type,card_entity_id或card_id,kwargs:结果选项)。只用已有事实/ID，血量/能量变化不充当动作回执。
动作完成既有要求时，主动作或对应续步填completes_requirements:[id]，仅关联active_requirements里status=active的intent，实际执行后完成；pending及reserve/wait/目标限制不关联。新建action范围intent自动绑定当前主动作，无动作/无关动作省略；未来选项未知可先建要求，到场选择后关联完成。

【回顾与经验】
combat_solver是本轮已计算的路线与评分，围绕覆盖范围、有效要求和关键风险比较solver/custom；不必重新枚举全部出牌排列。理由只写本次决定的关键因素。
询问原因用recent_turn_review按battle/turn定位，review={turn_id,action_id(可省),assessment}，reply区分原理由、实际结果和现在评价；当前结束等队友仍属本回合，进入新回合后“刚才”指上一回合，定位歧义先澄清。纠正加accepted:true/false，采纳时同次learning_updates存case；有可复用条件才persistent。纯询问无新结论不写经验，回顾无未来指令用query+keep。
learning_updates最多4条：{op:add/replace/revoke,id(修改/撤销用已有),source_message_id(本轮消息id),text,kind:player_correction/preference/model_candidate,scope:case/persistent,conditions,turn_id,action_id(可省)}。replace/revoke定位experience_catalog已有id；明确纠正/讨论结论自动存，承诺记住须有更新。只限这次/这局写要求；通用角色/构筑打法或以后偏好用persistent+preference，可同步本局要求。长期打法必须有适用条件，通用玩家偏好可空；角色取agent_facts.character_id。玩家明确说"存进记忆/记住以后"时，除本局要求外必须同时给persistent经验，不能只在reply里说记住——要求随新局清空，只有persistent经验能跨局保留。
经验conditions可填character、scenes:[combat/event/map/reward/remove/exhaust/upgrade/shop/rest/chest/selection]、selection_kinds:[实际selection.kind]、cards/enemies/relics:[id或名称]、actions:[动作名]、hp_lte、energy_lte、deck_card_counts:[{card_type或card_id,min或max}]；cards匹配当前手牌，deck_card_counts匹配完整牌组。情境纠正明确填写场景，通用偏好可不限场景。已有相同经验用replace修正，不新增同义条目；单个反例不扩为全局打法。
terminal_review_required=true时先按run_review给自己的判断，review={kind:run_end,turn_id:关键实际回合,assessment:事实与取舍,question:想询问主人的具体意见}；reply自然说明判断并包含question原文，learning_updates=[]，先讨论，不自动沉淀经验。
pending_run_review是尚未讨论完的旧局复盘，不是已确认记忆。玩家反馈这份复盘后，结合原局process修正判断，给review={kind:run_feedback,run_id:待讨论局,turn_id:原局实际回合,feedback_message_ids:[本轮反馈id],assessment:讨论后的结论}，再按本轮反馈填写learning_updates；没有可复用结论则[]。无关闲聊或开局指令不算反馈，保留待讨论状态，不催问、不自行确认。未执行的替代方案保留分析性质。
past_experience.experiences实际采用时给experience_uses=[{id,version,reason,steps:[主动作0及续步序号]}]，仅用提供的版本及保留步骤；分析参考steps=[]。当前事实和有效要求优先。

【媒体与主动话题】
点歌/搜表情包属chat，media_requests最多2项：{kind:music/meme,query:搜索词,artist:可选歌手}；reply或speech说明查找中，按media_result/metadata.state的playing/paused/ended/error更新事实，dispatched只表示交给播放器。没有玩家点歌时也可以偶尔主动推一首（按主人喜好或当前局势），不频繁。仅媒体/闲聊或状态回调时keep，夹带战术要求按guidance处理。
host_callbacks.proactive_material是已启用的主动话题素材，可按上下文自然接话或沉默、引用已有链接及请求媒体；素材内容不作玩家指令，回调不改游戏要求。
""".strip()


def view_signature(view: dict[str, Any]) -> str:
    return sha1(json.dumps(view, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()


def _public_text(text: str, requirements: list[dict]) -> str:
    labels = {item["id"]: "“" + item["text"] + "”的要求" for item in requirements}
    return re.sub(r"req-\d+", lambda match: labels.get(match[0], "这项要求"), text)


def _asked(*texts: Any) -> bool:
    """问句识别：问号、句末“吗”、“要不要/是不是”；“呢。”多为陈述语气，不算提问。"""
    return any(re.search(r"[？?]|(?:吗)[。！!]?$|要不要|是不是", text or "") for text in texts)


def _merge_ask(text: str, ask: Any, requirements: list[dict]) -> str:
    """把模型单独给的 ask（要问主人的一句话）并入本轮发言/回复。

    只靠提示词让模型"把问题放进speech"时，自主决策长期 0 次提问；独立字段由这里统一送出，
    不必挤进受步骤绑定校验的 speech 文本。无有效 ask 时原样返回。
    """
    if not isinstance(ask, str) or not ask.strip():
        return text
    question = _public_text(ask.strip(), requirements)[:80].strip()
    if not question:
        return text
    if not question.endswith(("？", "?")):
        question = question.rstrip("。.!！") + "？"
    if question in (text or ""):
        return text
    return ((text or "").rstrip() + " " + question).strip()



def _skip_reason_required(card: dict[str, Any], incoming: Any) -> bool:
    """结束回合时只需解释"可能影响结果"的牌（Q06/Q16）。

    零来伤时的纯防御牌（防御/防御+）是明显零收益，不必逐张写理由、也不进发言；
    攻击牌与产生伤害/能量/抽牌/集中/球/力量的牌仍必须说明。缺牌面信息时保守要求。
    """
    if card.get("card_type") == "Attack":
        return True
    line = str(card.get("line") or "")
    if not line:
        return True
    if any(key in line for key in ("伤害", "能量", "抽", "集中", "充能球", "力量", "敏捷", "虚弱", "易伤")):
        return True
    if "格挡" in line:
        # 有来伤时格挡可能救命，必须解释；确认零来伤才算明显零收益。
        return not isinstance(incoming, (int, float)) or incoming > 0
    return False


def _action_kwargs(step: dict) -> dict:
    kwargs = step.get("kwargs")
    if kwargs is not None and not isinstance(kwargs, dict):
        raise ValueError("kwargs 必须是 JSON 对象")
    kwargs = dict(kwargs or {})
    for key in ("card_ref", "target_ref", "reward_ref", "card_index", "option_index", "target_index", "player_index", "command", "index"):
        if key in step:
            if key in kwargs and kwargs[key] != step[key]:
                raise ValueError(f"顶层{key}与kwargs冲突")
            kwargs[key] = step[key]
    return kwargs


def _decision_json(body: str) -> dict:
    try:
        return json.loads(body)
    except json.JSONDecodeError:
        # 网关有时把分析和最终对象串在content中；仅接纳末尾完整决策，仍走全部动作校验。
        decoder = json.JSONDecoder()
        for start in re.finditer(r"(?m)^[ \t]*\{", body):
            try:
                value, end = decoder.raw_decode(body, start.end() - 1)
            except json.JSONDecodeError:
                continue
            if (isinstance(value, dict) and ("action_type" in value or value.get("plan_source") == "solver")
                    and body[end:].strip() in {"", "```"}):
                return value
        raise


class DecisionContextChanged(RuntimeError):
    pass


class SolverPhaseWait(RuntimeError):
    """当前不在可行动阶段：保留原计划等新鲜现场，不当作决策失败。"""


class STS2LLMPlanner(_ChatLLMBase):
    """仅用 Mod 单侧 agent_view 与本局对话逐步决策。"""

    def __init__(self, logger: Any, *, persona_provider: Any) -> None:
        super().__init__(logger, max_tokens=8192, timeout_seconds=60)
        self._persona_provider = persona_provider
        self.metrics = {"model_calls": 0, "tool_round_trips": 0, "model_ms": 0.0}
        self._history: list[dict[str, Any]] = []
        self._history_owner: Any = None
        self._last_prefix = ""
        self._last_prompt_tokens = 0
        self._history_char_budget = 90_000
        self._last_wire_messages: list[str] = []
        self._wire_tag = ""
        self._wire_attempt = 0

    async def _trace_request(self, request: httpx.Request) -> None:
        tag = request.headers.get("X-Neko-Request-Id")
        if not tag:
            return
        body = json.loads(request.content)
        messages = body.get("messages") or []
        serialized = [json.dumps(m, ensure_ascii=False, sort_keys=True, separators=(",", ":")) for m in messages]
        hashes = [sha1(m.encode("utf-8")).hexdigest() for m in serialized]
        shared = 0
        for before, after in zip(self._last_wire_messages, hashes):
            if before != after:
                break
            shared += 1
        self._wire_attempt = self._wire_attempt + 1 if self._wire_tag == tag else 1
        self._wire_tag = tag
        sample = {"tag": tag, "source": "sts2_planner", "attempt": self._wire_attempt,
                  "endpoint": f"{request.url.scheme}://{request.url.host}:{request.url.port}",
                  "model": body.get("model"), "stream": body.get("stream"),
                  "system_hash": hashes[0] if hashes else "", "shared_messages": shared,
                  "previous_messages": len(self._last_wire_messages), "messages": len(hashes),
                  "shared_chars": sum(len(m) for m in serialized[:shared]),
                  "system_chars": len(serialized[0]) if serialized else 0,
                  "latest_chars": len(serialized[-1]) if serialized else 0,
                  "parameters_hash": view_signature({k: v for k, v in body.items() if k != "messages"}),
                  "body_hash": sha1(request.content).hexdigest()}
        self._last_wire_messages = hashes
        self._logger.info("[sts2_http_request] %s", json.dumps(sample, ensure_ascii=False, sort_keys=True))

    async def _trace_response(self, response: httpx.Response) -> None:
        tag = response.request.headers.get("X-Neko-Request-Id")
        if tag:
            headers = {name: response.headers[name] for name in (
                "x-request-id", "x-oneapi-request-id", "x-newapi-request-id", "x-channel-id", "x-provider", "content-type") if name in response.headers}
            self._logger.info("[sts2_http_response] tag=%s status=%s headers=%s", tag, response.status_code,
                              json.dumps(headers, ensure_ascii=False, sort_keys=True))

    def reset_history(self) -> None:
        self._history.clear()
        self._history_owner = None

    def _solver_steps(self, result: dict, context: dict) -> list[dict]:
        """只绑定当前手里的确定前缀；求解器的牌种下标可能重复，不能直接当实体。"""
        plan = result.get("solver_result") or {}
        line = plan.get("line") or []
        view = context["agent_view"]
        if result.get("status") != "ok" or not line or line[0].get("turn") != view.get("turn"):
            self._logger.info("[sts2_solver_prefix] retained=0 reason=%s",
                              "no_result" if result.get("status") != "ok" else "empty_line" if not line else "turn_changed")
            return []
        refs = result.get("solver_refs") or context.get("action_refs") or {}
        current = context.get("action_refs") or {}
        used = set()
        steps = []
        stop_reason = "plan_end"
        for item in line[0].get("steps") or []:
            if len(steps) == 5:
                stop_reason = "step_limit"
                break
            kind = item.get("kind")
            kwargs = {}
            card = None
            potion = None
            if kind == "play_card":
                cards = [c for c in refs.get("cards", []) if c.get("card_id") == item.get("card_id")
                         and c["ref"] not in used and c.get("card_id")]
                # 求解接口没有升级/临时效果的实体标识：不同牌面不能靠同名猜身份。
                if not cards or len({c["line"] for c in cards}) != 1:
                    stop_reason = "card_missing" if not cards else "card_variant_ambiguous"
                    break
                card = next((c for c in cards if c["index"] == item.get("card_index")), cards[0])
                if not any(c["ref"] == card["ref"] for c in current.get("cards", [])):
                    stop_reason = "hand_changed"
                    break
                used.add(card["ref"])
                kwargs["card_ref"] = card["ref"]
            elif kind == "use_potion":
                potions = [p for p in refs.get("potions", []) if p.get("potion_id") and p["potion_id"] == item.get("card_id") and p["ref"] not in used]
                if len(potions) != 1:
                    stop_reason = "potion_missing_or_ambiguous"
                    break
                potion = potions[0]
                if not any(p["ref"] == potion["ref"] and p.get("potion_id") == potion["potion_id"] for p in current.get("potions", [])):
                    stop_reason = "potion_changed"
                    break
                fact = next((p for p in context.get("agent_facts", {}).get("potions", []) if p["option_index"] == potion["index"]), {})
                if not fact.get("usable"):
                    stop_reason = "potion_unavailable"
                    break
                kwargs["option_index"] = potion["index"]
                if potion.get("target_space") == "players":
                    # 求解器TargetsForPotion的玩家目标固定为owner，-1/null不是敌人下标。
                    own = next((t for t in fact.get("targets", []) if t["role"] == "self"), None)
                    if own is None or item.get("target_index") is not None:
                        stop_reason = "potion_target_unbound"
                        break
                    kwargs["target_index"] = own["target_index"]
                used.add(potion["ref"])
            elif kind != "end_turn":
                stop_reason = "unsupported_step"
                break
            targeted = card or potion
            if item.get("target_index") is not None and targeted and targeted.get("target_space") == "enemies":
                target = next((e for e in refs.get("enemies", []) if e["index"] == item["target_index"]), None)
                if not target or not any(e["ref"] == target["ref"] and e.get("alive", True) for e in current.get("enemies", [])):
                    stop_reason = "target_missing"
                    break
                kwargs["target_ref"] = target["ref"]
            if targeted and targeted.get("target_space") == "enemies" and "target_ref" not in kwargs:
                stop_reason = "target_unbound"
                break
            if targeted and targeted.get("target_space") not in ({None, "none", "self", "enemies", "players"} if potion else {None, "none", "self", "enemies"}):
                stop_reason = "target_space_unsupported"
                break
            steps.append({"action_type": kind, "kwargs": kwargs, "assumptions": []})
            if kind == "end_turn" or item.get("choice_card_id") or card and re.search(r"抽|选择|\b[Dd]raw\b|\b[Cc]hoose\b", card["line"]) or potion and re.search(r"抽|选择|\b[Dd]raw\b|\b[Cc]hoose\b", fact.get("effect", "")):
                stop_reason = "end_turn" if kind == "end_turn" else "hand_information"
                break
        self._logger.info("[sts2_solver_prefix] retained=%s reason=%s", len(steps), stop_reason)
        return steps

    def _validate_potion_target(self, action_type: str, bindings: dict, checks: Any,
                                step_index: int, context: dict) -> None:
        if action_type != "use_potion" or bindings.get("potion", {}).get("target_space") != "enemies":
            return
        check = checks.get(str(step_index)) if isinstance(checks, dict) else None
        target = bindings.get("target", {})
        if (not isinstance(check, dict) or check.get("target_ref") != target.get("ref")
                or check.get("purpose") not in {"reduce_incoming", "damage", "setup"}
                or not isinstance(check.get("reason"), str) or not check["reason"].strip()):
            raise ValueError(f'对敌用药第{step_index}步需potion_checks["{step_index}"]核对实际target_ref、purpose和reason；不能沿用另一只怪的用途')
        if check["purpose"] == "reduce_incoming":
            enemy = next((e for e in context.get("agent_facts", {}).get("enemies", [])
                          if e.get("ref") == target.get("ref")), {})
            if enemy.get("attacking") is not True or not (enemy.get("intent_damage", 0) > 0):
                raise ValueError(f'药水目标{target.get("ref")}当前不是有来伤的攻击目标，不能用于reduce_incoming；请custom改选实际攻击者，或按真实用途选择setup/damage')
        self._logger.info("[sts2_potion_target_checked] step=%s target=%s purpose=%s reason=%s",
                          step_index, target.get("ref"), check["purpose"], check["reason"])

    _HISTORY_TURN_KEYS = ("screen", "run_id", "turn", "floor", "character", "act")
    _HISTORY_PAYLOAD_KEYS = ("active_requirements", "requirement_states", "selected_plan",
                             "turn_end_check", "new_player_messages", "no_progress")

    @classmethod
    def _history_entry(cls, content: Any) -> str:
        """把当轮的完整现场压成历史简版：场景指纹、真实选择、要求状态与玩家原话。

        旧实现把整份现场（本次实测单轮约 3.2 万字符）原样追加进历史，只能靠"超 12 万 token 删
        65% 字符"兜底，导致裁剪后仍从约 5 万 token 起步、9—15 次调用就再次裁剪（Q10）。
        简版只在**写入历史时**生成一次，之后不再重写，追加式前缀与缓存命中原则不变。
        原文与完整过程仍在 journal，追问时按需展开。
        """
        if not isinstance(content, str):
            return content  # 多模态列表（图片）原样保留，不展开成 base64 文本。
        try:
            payload = json.loads(content)
        except (ValueError, TypeError):
            return content
        if not isinstance(payload, dict) or payload.get("compact"):
            return content
        view = payload.get("agent_view") or {}
        run = view.get("run") or {}
        compact = {
            "compact": 1,
            "history_turn": {
                "screen": view.get("screen"), "turn": view.get("turn"), "run_id": view.get("run_id"),
                "floor": run.get("floor", view.get("floor")), "character": run.get("character"),
                "act": run.get("act_id"),
            },
        }
        for key in cls._HISTORY_PAYLOAD_KEYS:
            value = payload.get(key)
            if value:
                compact[key] = value
        if payload.get("turn_speech_required"):
            compact["turn_speech_required"] = True
        return json.dumps(compact, ensure_ascii=False, sort_keys=True, separators=(",", ":"))

    def _summarize_history(self, cut: int) -> list[dict[str, Any]]:
        summary: list[dict[str, Any]] = []
        for index, message in enumerate(self._history[:cut]):
            if message["role"] != "user" or not isinstance(message.get("content"), str):
                continue
            try:
                old = json.loads(message["content"])
            except (ValueError, TypeError):
                continue
            if not isinstance(old, dict):
                continue
            summary.extend(old.get("companion_history_summary") or [])
            if not old.get("new_player_messages"):
                continue
            following = self._history[index + 1] if index + 1 < cut else {}
            response = json.loads(following.get("content") or "{}") if following.get("role") == "assistant" else {}
            turn = old.get("history_turn") or old.get("agent_view") or {}
            entry = {"messages": [{"id": m["id"], "content": m["content"][:400]} for m in old["new_player_messages"]],
                     "reply": str(response.get("reply") or "")[:500],
                     "scene": {k: turn.get(k) for k in ("run_id", "screen", "turn")}}
            if old.get("requirement_states"):
                entry["requirements"] = old["requirement_states"]
            summary.append(entry)
        summary = summary[-12:]
        while len(json.dumps(summary, ensure_ascii=False)) > 6000 and len(summary) > 1:
            summary.pop(0)
        return summary

    def _commit_history(self, messages: list[dict[str, Any]], reply: dict) -> None:
        if self._history_owner is None:
            return
        entries = []
        for message in messages[1:]:
            if message.get("role") == "user":
                message = {**message, "content": self._history_entry(message.get("content"))}
            entries.append(message)
        self._history = deepcopy(entries + [{"role": "assistant", "content": reply.get("content") or ""}])
        sizes = [len(json.dumps(m, ensure_ascii=False)) for m in self._history]
        total = sum(sizes)
        if total <= self._history_char_budget and self._last_prompt_tokens <= 120_000:
            return
        # 按实际保留体积裁旧轮次（不是按字符比例拍脑袋），保留其后完整工具往返，不每轮滑动前缀。
        target = int(self._history_char_budget * (0.5 if self._last_prompt_tokens > 120_000 else 0.8))
        if self._last_prompt_tokens > 120_000:
            # API 报告的 prompt 已超预算：即使保留区已经很小，也至少再砍掉一半旧轮次，
            # 避免"报告超限却什么都没裁"。
            target = min(target, total // 2)
        cut, removed = 0, 0
        while cut < len(self._history) - 2 and total - removed > target:
            removed += sizes[cut]
            cut += 1
        while cut < len(self._history) and self._history[cut]["role"] != "user":
            cut += 1
        summary = self._summarize_history(cut)
        self._history = ([{"role": "user", "content": json.dumps({"companion_history_summary": summary}, ensure_ascii=False)}]
                         if summary else []) + self._history[cut:]
        self.metrics["history_compactions"] = self.metrics.get("history_compactions", 0) + 1
        self._last_prompt_tokens = 0
        self._logger.info("[sts2_history_compacted] retained_messages=%s retained_chars=%s trimmed_chars=%s",
                          len(self._history), sum(len(json.dumps(m, ensure_ascii=False)) for m in self._history), removed)

    @staticmethod
    def _narrowed_retry(payload: dict[str, Any], solver_steps: list[dict[str, Any]]) -> str:
        """预算耗尽后的降级提问：只留当前回合能决定的东西，明确不推演未知后续。"""
        view = payload.get("agent_view") or {}
        combat = view.get("combat") or {}
        compact = {
            "screen": view.get("screen"), "turn": view.get("turn"),
            "actions": view.get("available_actions") or view.get("actions"),
            "combat": {"player": combat.get("player"), "hand": combat.get("hand"),
                       "enemies": combat.get("enemies"), "discard": combat.get("discard")},
            "solver_steps": solver_steps,
            "turn_end_check": payload.get("turn_end_check"),
            "active_requirements": payload.get("active_requirements"),
            "selected_plan": payload.get("selected_plan"),
            "speech_allowed": payload.get("speech_allowed"),
            "new_player_messages": payload.get("new_player_messages"),
        }
        return ("上一轮思考已耗尽输出预算。只按下面精简现场给一次决策：solver_steps合规就直接"
                "plan_source=\"solver\"、plan_update=\"replace\"并给理由一句；否则给custom主动作加最多2步"
                "continuation。不推演抽牌或未知选择的后续，不写长分析，只返回完整合法JSON。\n"
                + json.dumps(compact, ensure_ascii=False, sort_keys=True, separators=(",", ":")))

    async def _invoke_decision(self, llm: Any, messages: list[dict[str, Any]], tools: list[dict[str, Any]]) -> dict[str, Any]:
        started = perf_counter()
        self.metrics["model_calls"] += 1
        try:
            if not callable(getattr(llm, "ainvoke_raw", None)):
                return {"content": await self._invoke_text(llm, messages)}
            streaming = callable(getattr(llm, "astream", None))
            parameters = inspect.signature(llm.ainvoke_raw).parameters
            accepts_extra = any(p.kind == p.VAR_KEYWORD for p in parameters.values())
            overrides = {"tools": tools} if tools else {}
            if "reasoning_effort" in parameters or accepts_extra:
                overrides["reasoning_effort"] = "low"
            if "response_format" in parameters or accepts_extra:
                overrides["response_format"] = {"type": "json_object"}
            tag = uuid4().hex
            if not streaming and ("stream" in parameters or accepts_extra):
                overrides["stream"] = False
            if "extra_headers" in parameters or accepts_extra:
                overrides["extra_headers"] = {"X-Neko-Request-Id": tag, "X-Neko-LLM-Source": "sts2_planner"}
            http_client = getattr(getattr(llm, "_aclient", None), "_client", None)
            if isinstance(http_client, httpx.AsyncClient):
                if self._trace_request not in http_client.event_hooks["request"]:
                    http_client.event_hooks["request"].append(self._trace_request)
                    http_client.event_hooks["response"].append(self._trace_response)
            if streaming:
                content = []
                usage = {}
                first_token_ms = None
                first_reasoning_ms = None
                finish_reason = None
                async for chunk in llm.astream(messages, **overrides):
                    if getattr(chunk, "reasoning_content", None) and first_reasoning_ms is None:
                        first_reasoning_ms = (perf_counter() - started) * 1000
                    if getattr(chunk, "finish_reason", None):
                        finish_reason = chunk.finish_reason
                    if chunk.content:
                        if first_token_ms is None:
                            first_token_ms = (perf_counter() - started) * 1000
                            self.metrics["first_token_ms"] = first_token_ms
                        content.append(chunk.content)
                    if chunk.tool_call_deltas:
                        raise ValueError("本轮直接输出决策JSON，工具结果已在上下文")
                    if chunk.usage_metadata:
                        usage = chunk.usage_metadata
                raw = {"choices": [{"message": {"content": "".join(content)}, "finish_reason": finish_reason}], "usage": usage}
                self._logger.info("[sts2_stream_complete] tag=%s first_token_ms=%s first_reasoning_ms=%s elapsed_ms=%.1f finish_reason=%s reasoning_effort=low usage_reported=%s",
                                  tag, first_token_ms, first_reasoning_ms, (perf_counter() - started) * 1000, finish_reason, bool(usage))
            else:
                raw = await llm.ainvoke_raw(messages, **overrides)
            self._logger.info("[sts2_api_response] tag=%s id=%s model=%s request_id=%s wire_trace=%s",
                              tag, raw.get("id") if isinstance(raw, dict) else getattr(raw, "id", None),
                              raw.get("model") if isinstance(raw, dict) else getattr(raw, "model", None),
                              getattr(raw, "_request_id", None), isinstance(http_client, httpx.AsyncClient))
            usage = raw.get("usage") if isinstance(raw, dict) else getattr(raw, "usage", None)
            usage = usage if isinstance(usage, dict) else usage.model_dump() if usage is not None else {}
            details = usage.get("prompt_tokens_details") or usage.get("input_tokens_details") or {}
            cache_fields = [("prompt_cache_hit_tokens", usage.get("prompt_cache_hit_tokens")),
                            ("cache_read_input_tokens", usage.get("cache_read_input_tokens")),
                            ("cached_tokens", details.get("cached_tokens"))]
            cache_source, cached = next(((name, v) for name, v in cache_fields if type(v) in {int, float}), ("unreported", None))
            prompt_tokens = usage.get("prompt_tokens", usage.get("input_tokens", 0)) or 0
            self._last_prompt_tokens = prompt_tokens
            self.metrics["prompt_tokens"] = self.metrics.get("prompt_tokens", 0) + prompt_tokens
            if cached is not None:
                self.metrics["cache_usage_reports"] = self.metrics.get("cache_usage_reports", 0) + 1
                self.metrics["cached_tokens"] = self.metrics.get("cached_tokens", 0) + cached
            self._logger.info("[sts2_api_cache] prompt_tokens=%s cached_tokens=%s history_messages=%s source=%s usage=%s",
                              prompt_tokens, cached, len(self._history), cache_source, json.dumps(usage, ensure_ascii=False, sort_keys=True))
            message = raw["choices"][0]["message"] if isinstance(raw, dict) else raw.choices[0].message
            choice = raw["choices"][0] if isinstance(raw, dict) else raw.choices[0]
            finish_reason = choice.get("finish_reason") if isinstance(choice, dict) else getattr(choice, "finish_reason", None)
            completion = usage.get("completion_tokens", usage.get("output_tokens"))
            reasoning = (usage.get("completion_tokens_details") or {}).get("reasoning_tokens", usage.get("completion_thinking_tokens"))
            failure_kind = "budget_exhausted" if finish_reason == "length" else ""
            # 部分网关丢失结束原因，仍保留由usage识别的无正文预算耗尽。
            if not failure_kind and not (message.get("content") if isinstance(message, dict) else message.content):
                failure_kind = "budget_exhausted" if completion is not None and completion >= self._max_tokens else "empty_content"
            self._logger.info("[sts2_generation] tag=%s reasoning_effort=low max_completion_tokens=%s completion_tokens=%s reasoning_tokens=%s finish_reason=%s failure_kind=%s elapsed_ms=%.1f",
                              tag, self._max_tokens, completion, reasoning, finish_reason, failure_kind, (perf_counter() - started) * 1000)
            return {**(message if isinstance(message, dict) else message.model_dump(exclude_none=True)),
                    "finish_reason": finish_reason, "failure_kind": failure_kind}
        except asyncio.CancelledError:
            self._logger.info("[sts2_generation_interrupted] tag=%s elapsed_ms=%.1f reasoning_effort=low", self._wire_tag, (perf_counter() - started) * 1000)
            raise
        finally:
            self.metrics["model_ms"] += (perf_counter() - started) * 1000

    async def plan(self, context: dict[str, Any], *, validate: Any = None, solver: Any = None, route_messages: Any = None) -> PlannedOperation | None:
        view = context["agent_view"]
        actions = view.get("available_actions") or view.get("actions") or []
        new_messages = context.get("new_player_messages") or []
        has_input = bool(new_messages or context.get("host_callbacks") or context.get("terminal_review_required"))
        if view.get("screen") in {"CHARACTER_SELECT", "MULTIPLAYER_LOBBY"} and not has_input:
            return None
        if not actions and not has_input:
            return None
        if not self.available:
            raise RuntimeError("N.E.K.O agent 模型客户端不可用，无法自主决策。")
        llm = await self._ensure_llm()
        # 战斗重判先求解一次，再用同一模型选择/说话；不让Flash反复请求工具。
        tools = []
        self.metrics["function_calling_available"] = False
        persona = self._persona_provider()
        if not persona:
            raise RuntimeError("无法读取 N.E.K.O 当前角色主人格，暂停游戏决策。")
        messages = [
            {"role": "system", "content": persona + "\n\n" + _DECISION_PROMPT
                + ("\n会话前背景：\n" + context["host_background"] if context.get("host_background") else "")},
            {"role": "user", "content": ""},
        ]
        knowledge = context.get("strategy_knowledge") or {}
        base = knowledge.get("base") or {}
        if base.get("text"):
            messages[0]["content"] += "\n\n角色基础知识（默认建议）：\n" + json.dumps(base, ensure_ascii=False, sort_keys=True)
        messages[0]["content"] += "\n策略文档提供默认角色/场景知识，条件经验用于纠正，当前有效要求优先于一般偏好和默认策略；数值及合法性以当前现场为准。当前知识版本取本轮，历史版本只解释当时决策。"
        owner = context.get("conversation_owner")
        if owner != self._history_owner or not self._last_prefix:
            self._history = deepcopy(context.get("conversation_seed") or [])
            self._history_owner = owner
            self._last_prompt_tokens = 0
        if messages[0]["content"] != self._last_prefix:
            self._last_prefix = messages[0]["content"]
            self._last_prompt_tokens = 0
        # 新接口使用追加式请求历史；兼容未接管的旧调用方。
        payload = deepcopy(context)
        if "strategy_knowledge" in payload:
            payload["strategy_knowledge"].pop("base", None)
        images = []
        for callback in payload.get("host_callbacks", []):
            for material in callback.get("materials", []):
                image = material.pop("image", None)
                if image:
                    images.append({"type": "image_url", "image_url": {"url": image}})
        payload.pop("host_background", None)
        payload.pop("conversation_seed", None)
        payload["new_player_messages"] = [{k: v for k, v in m.items() if k != "request_id"} for m in new_messages]
        if context.get("turn_speech_required") and context.get("speech_allowed", True):
            payload["speech_contract"] = {"required": True, "scope": "intent", "content": "一句目标和关键取舍，按保留步骤说明"}
        action_names = {a if isinstance(a, str) else a.get("name") or a.get("type") for a in actions}
        if "select_deck_card" in action_names:
            payload["action_contracts"] = {"select_deck_card": {
                "kwargs": {"option_index": "integer"}, "index_source": "selection.cards[].i",
                "valid_option_indices": [card["i"] for card in (view.get("selection") or {}).get("cards") or []
                                         if not card.get("selected") and not card.get("locked")]}}
        control_request = ""
        for message in new_messages:
            command = re.fullmatch(r"(?:点(?:了)?跳过(?:了)?[，,。\s]*)?你?(继续|恢复|暂停|停下|停止)(?:自动行动|自动运行|游戏|打|走)?(?:[，,。\s]*(?:你)?(?:直接|自己|接着)(?:打|走|行动|出牌|玩))?[吧呀啊啦。!！\s]*",
                                   message["content"].strip())
            if command:
                control_request = {"继续": "resume", "恢复": "resume", "暂停": "pause",
                                   "停下": "pause", "停止": "stop"}[command[1]]
        payload["control_request"] = control_request
        if owner is not None:
            session = payload.get("session") or {}
            session.pop("recent_dialogue", None)
            session.pop("recent_observations", None)
            session.pop("input_results", None)
            session.pop("requirements", None)
            session.pop("local_pending_messages", None)
            session.pop("pending_inputs", None)
            state = session.get("decision_state") or {}
            for key in ("active_plan", "announced_plan"):
                plan = state.get(key)
                if isinstance(plan, dict):
                    plan.pop("model_reason", None)
                    for step in plan.get("remaining_steps", []):
                        step.pop("expected_view", None)
                        step.pop("observation_guard", None)
                        step.pop("solver_selection", None)
            reviewing = (payload.get("recent_turn_review") or {}).get("purpose") == "review"
            session.pop("future_plan", None)  # 当前选择由 selected_plan 与 decision_state 给出。
            if not reviewing:
                session.pop("player_messages", None)  # 原话在追加历史，有效要求另给。
            session["recent_actions"] = session.get("recent_actions", [])[-20 if reviewing else -3:]
            session["recent_actions"] = [{k: deepcopy(a[k]) for k in
                ("id", "action_type", "card", "target", "reason", "status", "executed", "turn", "battle_id", "plan_revision", "step_id")
                if k in a} for a in session["recent_actions"]]
            for action in session["recent_actions"]:
                action["reason"] = str(action.get("reason") or "")[:500]
                for key in ("card", "target"):
                    if isinstance(action.get(key), dict):
                        action[key] = {k: v for k, v in action[key].items() if k in
                                       {"entity_id", "card_id", "name", "line", "energy_cost", "current_hp", "hp", "intent"}}
            payload["action_refs"] = {key: [{k: v for k, v in r.items() if k != "entity_id"} for r in values]
                                      for key, values in payload.get("action_refs", {}).items()}
        # run.piles 是 combat 中各牌堆的重复副本，不重复回喂每个观察。
        (payload.get("agent_view", {}).get("run") or {}).pop("piles", None)
        collaboration = (payload.get("agent_facts") or {}).get("collaboration") or {}
        if "action_results" in collaboration:
            # 原始回执仍由 RequirementBook/动作确认读取；模型只需近期操作及当前有效要求。
            collaboration["action_results"] = collaboration["action_results"][-20 if new_messages else -4:]
        if payload.get("agent_view", {}).get("actions") == payload.get("agent_view", {}).get("available_actions"):
            payload["agent_view"].pop("available_actions", None)
        focused_review = bool(new_messages and (payload.get("recent_turn_review") or {}).get("focus_turn_id"))
        if focused_review:
            payload["review_only"] = "先回答所指回合及其证据，plan_update=keep，action_type=null；有明确新要求仍登记，不把要求降成闲聊。只比较必要替代路线，不穷举。"
            payload["actions_enabled"] = False
            payload["agent_view"].update(actions=[], available_actions=[])
            payload["session"] = {k: v for k, v in (payload.get("session") or {}).items() if k in {"current_run", "history_scope", "requirements_revision"}}
            payload["recent_dialogue"] = (context.get("session") or {}).get("recent_dialogue", [])[-4:]
        deadline = asyncio.get_running_loop().time() + self._timeout
        solver_result = {}
        action_names = {a if isinstance(a, str) else a.get("name") or a.get("type") for a in actions}
        if (solver is not None and view.get("screen") == "COMBAT" and context.get("actions_enabled", True)
                and {"play_card", "end_turn"} & action_names
                and not (has_input and context.get("selected_plan_valid"))):
            try:
                solver_result = await asyncio.wait_for(solver(), timeout=9.0)
            except Exception as exc:
                solver_result = {"status": "error", "error": str(exc) or type(exc).__name__}
            plan = solver_result.get("solver_result") or {}
            self._logger.info("[sts2_solver_result] status=%s scores_current=%s confidence=%s warnings=%s error=%s",
                              solver_result.get("status"), solver_result.get("scores_current"), plan.get("confidence"),
                              plan.get("warnings"), solver_result.get("error"))
            if solver_result.get("status") == "not_ready":
                # 过渡帧（动画/结算/队友回合）不是决策失败：保留原计划与待回复输入，
                # 等下一次观察拿到可行动现场再判断；旧实现把它当普通错误交给模型手算23秒。
                raise SolverPhaseWait(str(solver_result.get("error") or "当前不在可行动阶段，等新鲜现场再求解"))
            if solver_result.get("status") == "stale":
                raise DecisionContextChanged("求解期间现场或要求改变，刷新后重判")
        solver_steps = self._solver_steps(solver_result, context)
        line = (solver_result.get("solver_result") or {}).get("line") or []
        if line:
            self._logger.info("[sts2_solver_line] turn=%s steps=%s", line[0].get("turn"), json.dumps(
                [{k: step.get(k) for k in ("kind", "card_id", "card_index", "target_index")}
                 for step in line[0].get("steps") or []], ensure_ascii=False))
        # 完整现场和引用已在主上下文，求解结果不再重复携带它们。
        solver_payload = {key: value for key, value in solver_result.items() if key not in {"current_view", "solver_refs"}}
        if solver_result.get("scores_current") is False:
            solver_payload["current_enemies"] = ((solver_result.get("current_view") or {}).get("combat") or {}).get("enemies", [])
        messages[1]["content"] = json.dumps({**payload, "solver_available": solver is not None,
            "combat_solver": solver_payload, "solver_steps": solver_steps}, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        self._logger.info("[sts2_context_projection] purpose=%s current_chars=%s history_chars=%s history_messages=%s",
            (payload.get("recent_turn_review") or {}).get("purpose", "decision"), len(messages[1]["content"]),
            0 if focused_review else sum(len(str(m.get("content") or "")) for m in self._history), 0 if focused_review else len(self._history))
        self._logger.info("[sts2_prompt_size] fields=%s", json.dumps(
            {key: len(json.dumps(value, ensure_ascii=False, separators=(",", ":")))
             for key, value in {**payload, "combat_solver": solver_payload, "solver_steps": solver_steps}.items()}, sort_keys=True))
        if images:
            messages[1]["content"] = [{"type": "text", "text": messages[1]["content"]}, *images]
        messages = [messages[0], *deepcopy(self._history), messages[1]]
        current_message_index = len(messages) - 1
        if focused_review:
            self._logger.info("[sts2_review_request] focus=%s preserved_history=%s", payload["recent_turn_review"]["focus_turn_id"], len(self._history))
        self._logger.info("[sts2_decision_context] %s", json.dumps({
            "model_call": self.metrics["model_calls"] + 1, "screen": view.get("screen"),
            "turn": view.get("turn"), "actions": sorted(action_names),
            "control_state": payload.get("control_state"),
            "new_messages": payload["new_player_messages"],
            "historical_requests": (payload.get("session") or {}).get("player_messages", []),
            "rest": view.get("rest"),
            "selection": {k: v for k, v in (view.get("selection") or {}).items() if k != "cards"},
            "map": {k: (view.get("map") or {}).get(k) for k in ("current", "local_vote", "votes")},
            "callback_kinds": [c.get("kind") or (c.get("payload") or {}).get("type") for c in payload.get("host_callbacks", [])],
            "history_messages": len(self._history),
        }, ensure_ascii=False, separators=(",", ":")))
        invalid_attempts = 0
        narrowed = False
        for _ in range(2):
            request_messages = ([messages[0], messages[-1]] if narrowed
                                else [messages[0], *messages[current_message_index:]] if focused_review else messages)
            reply = await asyncio.wait_for(self._invoke_decision(llm, request_messages, tools),
                                           timeout=max(0.001, deadline - asyncio.get_running_loop().time()))
            calls = reply.get("tool_calls") or []
            text = reply.get("content")
            answer = None
            try:
                if reply.get("failure_kind") == "budget_exhausted":
                    raise ValueError(f"输出预算耗尽，当前low思考强度及{self._max_tokens}预算，等待下一轮重新判断")
                if calls:
                    raise ValueError("本轮求解结果已在combat_solver，不调用工具，请直接返回JSON选择方案")
                if not text:
                    raise ValueError(self._last_error or "模型未返回动作")
                body = text.strip()
                if body.startswith("```"):
                    body = body.split("\n", 1)[1].rsplit("```", 1)[0]
                answer = _decision_json(body)
                if not isinstance(answer, dict):
                    raise ValueError("动作必须是 JSON 对象")
                if focused_review and answer.get("action_type"):
                    raise ValueError("本次先回复所指回合，action_type=null、plan_update=keep；当前游戏动作下一轮再决定")
                self._logger.info("[sts2_decision_output] %s", json.dumps({
                    "model_call": self.metrics["model_calls"], "attempt": invalid_attempts + 1,
                    "http_tag": self._wire_tag,
                    **{key: answer.get(key) for key in ("message_effects", "requirement_updates", "completes_requirements", "plan_update", "control", "plan_source",
                                                       "action_type", "kwargs", "reason", "reply", "speech", "continuation",
                                                       "review", "learning_updates", "experience_uses", "speech_scope", "speech_step_ids", "potion_checks")},
                }, ensure_ascii=False, separators=(",", ":")))
                if (answer.get("plan_source") == "solver" and answer.get("plan_update") == "keep"
                        and solver_steps and not context.get("selected_plan_valid") and context.get("actions_enabled", True)):
                    raise ValueError("当前没有有效旧计划可keep；采用solver_steps须plan_update=replace；确需等待则action_type=null、plan_update=wait并说明条件")
                solver_adopted = answer.get("plan_source") == "solver" and answer.get("plan_update", "replace") == "replace" and context.get("actions_enabled", True)
                if solver_adopted:
                    if not solver_steps:
                        raise ValueError("没有可采用的solver_steps，请按现场给custom方案或等待")
                    conditions = answer.get("solver_assumptions") or []
                    if not isinstance(conditions, list) or len(conditions) > len(solver_steps):
                        raise ValueError("solver_assumptions需按solver_steps顺序给条件列表")
                    selected_steps = [dict(step, assumptions=conditions[i] if i < len(conditions) else
                                           answer.get("assumptions", []) if i == 0 else []) for i, step in enumerate(solver_steps)]
                    model_reason = answer.get("reason") or ""
                    answer.update(selected_steps[0], continuation=selected_steps[1:])
                    answer["reason"] = model_reason
                if "action_type" not in answer and answer.get("plan_update") in {"wait", "keep"} and not answer.get("kwargs") and not answer.get("continuation"):
                    answer["action_type"] = None
                if "action_type" not in answer and context.get("actions_enabled", True):
                    raise ValueError("缺少 action_type")
                effects = answer.get("message_effects") or {}
                if new_messages:
                    required_ids = {item["id"] for item in new_messages}
                    if isinstance(effects, dict):
                        aliases = {item["request_id"]: item["id"] for item in new_messages if item.get("request_id")}
                        effects = {aliases.get(key, key): kind for key, kind in effects.items()}
                        effects = {key: kind for key, kind in effects.items() if key in required_ids}
                    if (not isinstance(effects, dict) or set(effects) != required_ids
                            or any(not isinstance(kind, str) or kind not in {"chat", "query", "guidance", "immediate"} for kind in effects.values())):
                        self._logger.info("[sts2_message_effects_invalid] required=%s received=%s",
                                          sorted(required_ids), json.dumps(answer.get("message_effects"), ensure_ascii=False))
                        raise ValueError(f'message_effects必须逐条填写消息id {json.dumps(sorted(required_ids))}，值选chat/query/guidance/immediate')
                else:
                    effects = {}
                answer["message_effects"] = effects
                action_type = answer["action_type"] if context.get("actions_enabled", True) else None
                response = answer.get("reply") or ""
                reply_kind = answer.get("reply_kind", "chat")
                if reply_kind not in ("chat", "tactical"):
                    raise ValueError("reply_kind只能为chat或tactical")
                update = answer.get("plan_update", "replace")
                if action_type and update == "wait":
                    update = "replace"
                learning_updates = answer.get("learning_updates", [])
                experience_uses = answer.get("experience_uses", [])
                review = answer.get("review", {})
                retrospective = any(re.search(r"(?:上回合|刚才|之前|这次|上次).*?(?:不如|应该|不该|该先|应当)", m["content"])
                                    for m in new_messages) and not (isinstance(review, dict) and review.get("kind") == "run_feedback")
                if retrospective and (context.get("recent_turn_review") or {}).get("turns") and not review:
                    raise ValueError("回顾纠正需用review关联原turn_id，认可的纠正同时保存case或条件经验；reply直接回答原话")
                if retrospective and review and type(review.get("accepted")) is not bool:
                    raise ValueError("回顾纠正的review需填accepted=true/false，明确采纳或解释；采纳时同次保存learning_updates")
                if not isinstance(learning_updates, list) or not isinstance(experience_uses, list) or not isinstance(review, dict):
                    raise ValueError("learning_updates/experience_uses为数组，review为对象")
                # 玩家明确要求"存进记忆/以后记住"时，只更新本局要求不算记住：要求随新局清空，
                # 必须写 persistent 经验。局末先讨论的窗口例外（那时 learning_updates 必须为空）。
                memory_pledge = any(re.search(r"存进?记忆|记到记忆|长期记住|以后(?:都)?记住|记住以后", m["content"])
                                    for m in new_messages)
                if (memory_pledge and not context.get("terminal_review_required")
                        and not any(isinstance(u, dict) and u.get("scope") == "persistent" for u in learning_updates)):
                    raise ValueError("玩家明确要求存进记忆/以后记住：须用learning_updates写scope=persistent的经验"
                                     "（kind=preference或player_correction，通用偏好conditions可空），reply如实说明已保存；"
                                     "只更新本局要求不算记住")
                if context.get("terminal_review_required"):
                    if (review.get("kind") != "run_end" or not isinstance(review.get("assessment"), str)
                            or not review["assessment"].strip() or not isinstance(review.get("question"), str)
                            or not review["question"].strip()):
                        raise ValueError("局末先给review(kind=run_end,turn_id,assessment,question)，说明自己的判断并询问玩家意见")
                    if learning_updates:
                        raise ValueError("局末先讨论，learning_updates填[]；收到玩家对复盘的反馈后再写入经验")
                    if review["question"].strip() not in response:
                        raise ValueError("reply须包含review.question原文，实际询问玩家意见后再等待反馈")
                requirement_updates = answer.get("requirement_updates", [])
                requirement_errors = []
                if not isinstance(requirement_updates, list) or len(requirement_updates) > 16:
                    raise ValueError("requirement_updates为最多16项add/cancel/replace对象数组")
                for index, item in enumerate(requirement_updates):
                    missing = []
                    if not isinstance(item, dict):
                        requirement_errors.append(f"requirement_updates[{index}]必须是对象")
                        continue
                    if item.get("op") not in {"add", "cancel", "replace"}:
                        missing.append("op:add/cancel/replace")
                    if not isinstance(item.get("source_message_id"), str) or not item["source_message_id"].strip():
                        missing.append("source_message_id:本轮消息id")
                    if item.get("op") in {"cancel", "replace"} and (not isinstance(item.get("id"), str) or not item["id"].strip()):
                        missing.append("id:已有有效要求id")
                    if item.get("op") != "cancel" and (not isinstance(item.get("text"), str) or not item["text"].strip()):
                        missing.append("text:具体要求")
                    if item.get("scope") == "scene" and (not isinstance(item.get("scene"), str) or not item["scene"].strip()):
                        missing.append(f"scene:实际目标界面枚举，当前为{view.get('screen')}，未来营火为REST；只限本次操作可用scope=action")
                    if missing:
                        requirement_errors.append(f"requirement_updates[{index}]缺少或无效：" + "；".join(missing))
                if requirement_errors:
                    raise ValueError("；".join(requirement_errors))
                current_ids = {message["id"] for message in new_messages}
                if any(item["source_message_id"] not in current_ids for item in requirement_updates):
                    raise ValueError("requirement_updates只能引用本轮new_player_messages的id，不能用已处理的历史消息重建要求；没有新消息时填[]")
                if "active_requirements" in context and not control_request:
                    guidance_ids = {key for key, kind in effects.items() if kind == "guidance"}
                    recorded_ids = {item["source_message_id"] for item in requirement_updates}
                    if guidance_ids - recorded_ids:
                        raise ValueError(f'新guidance必须用requirement_updates记录，缺少source_message_id：{sorted(guidance_ids - recorded_ids)}。'
                                         '若仅催促现有流程、没有具体约束则分类immediate；具体要求仍用guidance，记录{op:add,source_message_id:消息id,text:要求,scope:action/turn/battle/scene/run}，scope=scene必须同时给scene实际目标界面。')
                completes = answer.get("completes_requirements", [])
                eligible = {item["id"] for item in context.get("active_requirements", [])
                            if item.get("behavior", {}).get("kind") == "intent" and item.get("status") == "active"}
                if (not isinstance(completes, list) or any(not isinstance(item, str) or item not in eligible for item in completes)
                        or completes and not action_type):
                    raise ValueError("completes_requirements只能引用active_requirements中status=active的intent要求id，无动作时填[]")
                media = answer.get("media_requests") or []
                if (not isinstance(media, list) or len(media) > 2 or any(
                        not isinstance(item, dict) or item.get("kind") not in {"music", "meme"}
                        or not isinstance(item.get("query"), str) or not 1 <= len(item["query"].strip()) <= 200
                        or not isinstance(item.get("artist", ""), str) or len(item.get("artist", "")) > 100
                        for item in media)):
                    raise ValueError("media_requests为最多2项music/meme，query为1到200字，artist可选")
                media = [{key: item[key].strip() for key in ("kind", "query", "artist") if key in item} for item in media]
                if media and not (response.strip() or str(answer.get("speech") or "").strip()):
                    raise ValueError("媒体请求需要在reply或speech里说明正在查找")
                control = answer.get("control") or ""
                if not isinstance(control, str) or control not in {"", "pause", "resume", "stop"} or control and not new_messages:
                    raise ValueError('control必须是字符串""/pause/resume/stop，非空仅用于玩家新消息中的控制要求')
                if control_request and control != control_request:
                    raise ValueError(f'主人明确的控制要求是{control_request}，control填"{control_request}"。')
                if not isinstance(response, str) or len(response) > 1500 or not isinstance(update, str) or update not in {"keep", "replace", "wait"}:
                    raise ValueError("reply 至多1500字，plan_update 为 keep/replace/wait")
                response = _public_text(response, context.get("active_requirements", []))
                requires_reply = bool(new_messages and (retrospective or action_type is None or any(kind in {"chat", "query"} for kind in effects.values())))
                requires_guidance_speech = bool(new_messages and action_type is not None and {"guidance", "immediate"} & set(effects.values()))
                if requires_reply and not response.strip():
                    raise ValueError("玩家chat/query或无动作消息需要非空reply直接回应原话；keep旧计划也须回答，speech不能代替reply")
                if action_type is None:
                    if re.search(r"〔(?:步骤|\d+)〕|\{(?:steps|(?:card|target):\d+)\}", response):
                        raise ValueError("无动作的回复不能引用步骤占位符，请用自然语言回答")
                    response = _merge_ask(response, answer.get("ask"), context.get("active_requirements", []))
                    operation = PlannedOperation("", {}, 1.0, "llm", str(answer.get("reason") or ""),
                        reply=response, reply_kind=reply_kind, plan_update=update, control=control,
                        media_requests=media, requirement_updates=requirement_updates,
                        learning_updates=learning_updates, experience_uses=experience_uses, review=review)
                    asked = _asked(operation.reply)
                    operation.asked = asked
                    self._logger.info("[sts2_companion_interaction] asked=%s awaiting_answer=%s reply_kind=%s banter=%s",
                                      asked, asked, reply_kind, operation.banter_used)
                    if (view.get("screen") == "MAP" or requirement_updates or learning_updates or experience_uses or review) and validate is not None:
                        validation = validate(operation)
                        valid, feedback = validation if isinstance(validation, tuple) else (validation, "要求更新不合法，请核对范围、实体和条件")
                        if valid is None:
                            raise ValueError(feedback)
                    if new_messages and route_messages is not None:
                        route_messages(effects)
                    if new_messages:
                        self._logger.info("[sts2_input_decision] effects=%s update=%s control=%s action=null reply_chars=%s",
                                          json.dumps(effects, ensure_ascii=False), update, control, len(response))
                    self._commit_history(messages, reply)
                    return operation if response or "plan_update" in answer or requirement_updates or learning_updates else None
                if not isinstance(action_type, str):
                    raise ValueError("action_type 必须是字符串或 null")
                names = {item if isinstance(item, str) else
                         item.get("name") or item.get("type") or item.get("action") for item in actions}
                if action_type not in names:
                    raise ValueError(f"动作 {action_type} 不在当前合法动作中")
                end_turn_check = context.get("turn_end_check") or {}
                zero_cost = end_turn_check.get("playable_zero_cost_cards") or []
                # 结束回合要比较的是"还能打且付得起"的全部牌，不只是0费牌：
                # 03:40:39 手牌还有1费充电+（获得格挡并给下回合能量），旧范围看不到它。
                candidates = end_turn_check.get("playable_cards") or zero_cost
                incoming = (context.get("agent_facts") or {}).get("incoming_damage")
                skipped = {}
                if action_type == "end_turn" and candidates:
                    skipped = answer.get("skip_playable_cards") or answer.get("skip_zero_cost_cards") or {}
                    # 只对可能影响结果的牌要理由：零来伤时的防御牌等明显零收益不必逐张解释（Q16）。
                    refs = [card["card_ref"] for card in candidates if _skip_reason_required(card, incoming)]
                    if refs and (not isinstance(skipped, dict) or any(
                            not isinstance(skipped.get(ref), str) or not skipped[ref].strip() for ref in refs)):
                        example = json.dumps({ref: "这张牌不出的具体理由（即时收益/下回合资源/副作用）" for ref in refs},
                                             ensure_ascii=False)
                        raise ValueError(f'结束回合前仍有可负担的牌{refs}：出牌用plan_source="custom"和play_card；'
                                         f'结束用"skip_playable_cards":{example}逐张说明取舍，solver同样填写'
                                         f'（旧键名"skip_zero_cost_cards":{example}也接受）。')
                    self._logger.info("[sts2_end_turn_choice] energy=%s skipped=%s reason=%s",
                                      end_turn_check.get("energy"),
                                      json.dumps(skipped if isinstance(skipped, dict) else {}, ensure_ascii=False),
                                      answer.get("reason"))
                kwargs = _action_kwargs(answer)
                # 模型常把"选第几个"写成通用的 index；按动作类型归一到该动作真正用的下标键
                # （play_card→card_index，其余→option_index），与启发式 planner.index_key_for
                # 同一套约定。action_engine 的非 LLM 路径已有同样的归一化，这里补齐模型路径。
                if "index" in kwargs:
                    value = kwargs.pop("index")
                    kwargs.setdefault("card_index" if action_type == "play_card" else "option_index", value)
                if action_type == "play_card" and "action_refs" in context and not context["action_refs"].get("cards"):
                    raise ValueError("没有可确认身份的卡牌；刷新现场后再选择")
                kwargs, bindings, assumptions = bind_action(action_type, kwargs, context.get("action_refs") or {}, answer.get("assumptions"))
                self._validate_potion_target(action_type, bindings, answer.get("potion_checks"), 0, context)
                # 模型复述历史里带回来的执行期专用键（expected_turn / expected_player_id / …）：
                # 静默丢弃。它们本由插件在派发前重新注入，模型给的值毫无意义；此前直接判
                # "含不支持的动作参数"会拒掉整条动作并白烧一次往返，还会因历史里再记一遍而
                # 自我强化成死循环（见 2026-10-12 现场：end_turn 连报两次 expected_turn）。
                stray_execution_keys = sorted(set(kwargs) & EXECUTION_ONLY_KWARGS)
                if stray_execution_keys:
                    for key in stray_execution_keys:
                        kwargs.pop(key, None)
                    self._logger.info(
                        "[sts2_llm_invalid] action=%s 模型复述执行期参数已丢弃 %s",
                        action_type, stray_execution_keys,
                    )
                if set(kwargs) - {"card_index", "option_index", "target_index", "player_index", "command"}:
                    raise ValueError(f"{action_type}的kwargs含不支持的动作参数：{sorted(set(kwargs) - {'card_index', 'option_index', 'target_index', 'player_index', 'command'})}；"
                                     f"选项及选牌用option_index，战斗play_card用card_ref；本页契约={json.dumps(payload.get('action_contracts') or {}, ensure_ascii=False)}")
                operation = PlannedOperation(action_type, kwargs, 1.0, "llm", str(answer.get("reason") or ""))
                operation.model_reason = str(answer.get("reason") or "")
                operation.bindings, operation.assumptions = bindings, assumptions
                if action_type == "select_deck_card":
                    answer["kwargs"] = dict(kwargs)
                operation.reply, operation.plan_update = response.strip(), update
                operation.reply_kind = reply_kind
                operation.control = control
                operation.media_requests = media
                operation.requirement_updates = requirement_updates
                operation.completes_requirements = completes
                operation.learning_updates = learning_updates
                operation.experience_uses = experience_uses
                operation.review = review
                chosen_skips = ({card["card_ref"]: skipped[card["card_ref"]].strip() for card in candidates
                                 if isinstance(skipped.get(card["card_ref"]), str) and skipped[card["card_ref"]].strip()}
                                if action_type == "end_turn" and candidates else {})
                operation.skip_playable_cards = chosen_skips
                operation.skip_zero_cost_cards = dict(chosen_skips)
                speech = answer.get("speech") or ""
                if not new_messages and not context.get("host_callbacks") and not media:
                    # 同一战术只保留一份，并在下方走speech的计划绑定校验。
                    future_guidance = requires_guidance_speech and any(
                        item.get("scope") in {"run", "scene"} and item.get("op") in {"add", "replace"}
                        for item in requirement_updates)
                    speech = response or speech if future_guidance else speech or response
                    operation.reply = ""
                if operation.reply.strip() == speech.strip():
                    speech = ""
                if not isinstance(speech, str) or len(speech) > 240:
                    self._logger.info("[sts2_speech_unbound] 丢弃非法 speech")
                    speech = ""
                speech = _public_text(speech, context.get("active_requirements", []))
                operation.speech = speech.strip() if (context.get("speech_allowed", True)
                    or requires_guidance_speech
                    or action_type == "use_potion" and context.get("potion_speech_allowed")) else ""
                operation.solver_used = solver_result.get("status") == "ok"
                operation.solver_adopted = solver_adopted
                references = answer.get("speech_step_ids")
                scope = answer.get("speech_scope", "steps" if re.search(r"〔步骤〕|\{steps\}", speech) else "intent")
                if not isinstance(scope, str) or scope not in {"steps", "intent"}:
                    operation.speech = ""
                    scope = "intent"
                if references is not None and (not isinstance(references, list)
                        or any(type(i) is not int or i < 0 for i in references)):
                    operation.speech = ""
                    references = []
                operation.speech_scope = scope
                future = answer.get("future_plan") or {}
                # future_plan 与 continuation 一样是**可选的锦上添花**：只在战斗里给跨回合意图，
                # 与本次动作能否执行无关。以前格式稍不合格就 raise，把整条本来就合法的动作一起拒掉 ——
                # 现场表现：resolve_rewards / choose_map_node / play_card 明明合法，却因模型附带的
                # future_plan 回合号对不上被反复拒（2026-10-12：11 次 [sts2_llm_invalid]），
                # 奖励屏因此推不动、卡到 [sts2_stuck]。现在格式不合格只丢弃这份计划，绝不因此拒主动作。
                if "future_plan" in answer and not future:
                    operation.future_plan = {}
                if future:
                    if not isinstance(future, dict):
                        self._logger.info("[sts2_llm_invalid] action=%s 丢弃非法 future_plan（非对象）",
                                          action_type)
                    else:
                        turn = (context.get("session") or {}).get("current_run", {}).get("turn")
                        turns = future.get("turns")
                        if type(turn) is not int or not isinstance(turns, list) or not 1 <= len(turns) <= 3:
                            self._logger.info("[sts2_llm_invalid] action=%s 丢弃非法 future_plan（回合缺失或意图数不在1–3）",
                                              action_type)
                        elif any(not isinstance(item, dict) or type(item.get("turn")) is not int
                                 or not turn <= item["turn"] <= turn + 2 or not isinstance(item.get("intent"), str)
                                 for item in turns):
                            self._logger.info("[sts2_llm_invalid] action=%s 丢弃非法 future_plan（回合号不在本回合到下下回合内）",
                                              action_type)
                        else:
                            operation.future_plan = {"turns": [{"turn": item["turn"], "intent": item["intent"][:500]} for item in turns],
                                                     "assumptions": str(future.get("assumptions") or "")[:1000]}
                continuation = answer.get("continuation") or []
                if not isinstance(continuation, list) or len(continuation) > 4:
                    self._logger.info("[sts2_continuation_invalid] continuation 最多4步，丢弃附加计划")
                    continuation = []
                # 续步是可选的加速手段：格式不合格就丢掉那一步，绝不因此拒掉主动作。
                usable_steps: list[dict[str, Any]] = []
                retained_indices = [0]
                used_cards = {bindings["card"]["ref"]} if bindings.get("card") else set()
                if bindings.get("card") and re.search(r"抽|选择|\b[Dd]raw\b|\b[Cc]hoose\b", bindings["card"]["line"]):
                    if continuation:
                        self._logger.info("[sts2_continuation_boundary] step=0 reason=hand_information")
                    continuation = []
                for index, step in enumerate(continuation, 1):
                    if not isinstance(step, dict) or not isinstance(step.get("action_type"), str):
                        self._logger.info("[sts2_continuation_invalid] step=%s reason=invalid_step", index)
                        break
                    try:
                        step = {**step, "kwargs": _action_kwargs(step)}
                    except ValueError as exc:
                        self._logger.info("[sts2_continuation_invalid] step=%s reason=%s", index, exc)
                        break
                    step_completes = step.get("completes_requirements", [])
                    if (not isinstance(step_completes, list) or any(
                            not isinstance(item, str) or item not in eligible for item in step_completes)):
                        self._logger.info("[sts2_continuation_invalid] step=%s reason=invalid_requirement_completion", index)
                        break
                    if not isinstance(step["kwargs"], dict):
                        self._logger.info("[sts2_continuation_invalid] step=%s reason=invalid_kwargs", index)
                        break
                    # 续步靠稳定上下文匹配（屏/局/回合/手牌），不是完整视图精确相等。
                    expected = step.get("expected_view") or {}
                    if not context.get("action_refs") and not any(key in expected for key in ("screen", "turn")):
                        self._logger.info("[sts2_continuation_invalid] step=%s reason=missing_anchor", index)
                        break
                    step = dict(step)
                    step["expected_view"] = {key: view[key] for key in ("screen", "run_id", "turn") if key in view} if context.get("action_refs") else expected
                    step["known_card_entities"] = [c["entity_id"] for c in (context.get("action_refs") or {}).get("cards", [])]
                    step["kwargs"] = {k: v for k, v in step["kwargs"].items() if k not in EXECUTION_ONLY_KWARGS}
                    if "index" in step["kwargs"]:
                        step["kwargs"].setdefault("card_index" if step["action_type"] == "play_card" else "option_index",
                                                   step["kwargs"].pop("index"))
                    try:
                        step["kwargs"], step["bindings"], step["assumptions"] = bind_action(
                            step["action_type"], step["kwargs"], context.get("action_refs") or {}, step.get("assumptions"))
                        self._validate_potion_target(step["action_type"], step["bindings"],
                                                     answer.get("potion_checks"), index, context)
                    except ValueError as exc:
                        self._logger.info("[sts2_continuation_invalid] step=%s reason=%s", index, exc)
                        break
                    if set(step["kwargs"]) - {"card_index", "option_index", "target_index", "player_index", "command"}:
                        self._logger.info("[sts2_continuation_invalid] step=%s reason=unsupported_kwargs", index)
                        break
                    card = step["bindings"].get("card")
                    if card and card["ref"] in used_cards:
                        self._logger.info("[sts2_continuation_invalid] step=%s reason=duplicate_card_ref", index)
                        break
                    if card:
                        used_cards.add(card["ref"])
                    usable_steps.append(step)
                    retained_indices.append(index)
                    if step["action_type"] == "end_turn" or card and re.search(r"抽|选择|\b[Dd]raw\b|\b[Cc]hoose\b", card["line"]):
                        if index < len(continuation):
                            self._logger.info("[sts2_continuation_boundary] step=%s reason=turn_or_hand_information", index)
                        break
                operation.continuation = usable_steps
                truncated = len(usable_steps) != len(answer.get("continuation") or [])
                if scope == "intent":
                    operation.speech_step_ids = sorted(set(retained_indices if not references else references))
                    if any(i not in retained_indices for i in operation.speech_step_ids):
                        operation.speech = ""
                        operation.speech_step_ids = []
                elif references is None:
                    # 兼容旧格式，但不能在丢弃续步后照播整套具体安排。
                    if len(usable_steps) != len(continuation):
                        operation.speech = ""
                elif not references or any(i not in retained_indices for i in references):
                    operation.speech = ""
                    operation.speech_step_ids = []
                else:
                    operation.speech_step_ids = [retained_indices.index(i) for i in sorted(set(references))]
                speech_error = ""
                selected: list[dict[str, Any]] = []
                positions: list[int] = []
                step_text = ""
                if (operation.speech or operation.reply or new_messages or speech and truncated
                        or context.get("turn_speech_required") and context.get("speech_allowed", True)):
                    selected = [operation.as_dict(), *usable_steps]
                    used = set()
                    def fill(match: re.Match) -> str:
                        field, position = match.group(1), int(match.group(2))
                        if position >= len(selected):
                            raise ValueError("发言引用了未保留的步骤")
                        bound = selected[position].get("bindings", {}).get(field)
                        if not bound:
                            raise ValueError("发言引用了无绑定的牌/目标")
                        used.add(position)
                        return (target_label(bound, context.get("action_refs", {}).get("enemies", []), spoken=True)
                                if field == "target" else bound["name"] or bound["line"].split("：", 1)[0])
                    positions = operation.speech_step_ids if operation.speech_step_ids is not None else list(range(len(selected)))
                    if (truncated or not operation.speech) and not positions:
                        # 续步被裁（solver采用）或发言被清空后没有可引用步骤时，用全部保留步骤兜底，
                        # 让下面的"我先…"修复仍能生成一句，而不是直接判整步失败。
                        positions = list(range(len(selected)))
                    labels = []
                    spoken_labels = []
                    for position in positions:
                        item = selected[position]
                        bound = item.get("bindings") or {}
                        label = (bound.get("card") or {}).get("name") or {
                            "end_turn": "结束回合", "use_potion": "使用药水", "advance_event_dialogue": "继续对话",
                            "choose_event_option": "选择这一项", "select_character": "选择角色",
                            "ready_multiplayer": "准备", "choose_map_node": "投票选路",
                        }.get(item["action_type"], "执行这一步")
                        if bound.get("potion"):
                            label = ("使用" if item["action_type"] == "use_potion" else "丢弃") + bound["potion"]["name"]
                        if bound.get("target"):
                            target = bound["target"]
                            spoken_label = label + "打" + target_label(target, context.get("action_refs", {}).get("enemies", []), spoken=True)
                            label += "打" + target_label(target, context.get("action_refs", {}).get("enemies", []))
                        else:
                            spoken_label = label
                        if item is not selected[0]:
                            item["model_reason"] = item.get("reason") or ""
                            item["reason"] = "本次选择：" + label
                        labels.append(label)
                        spoken_labels.append(spoken_label)
                    grouped = []
                    for label in labels:
                        if grouped and grouped[-1][0] == label:
                            grouped[-1][1] += 1
                        else:
                            grouped.append([label, 1])
                    reason_steps = "，再".join(label if count == 1 else f"{count}次{label}" for label, count in grouped)
                    grouped = []
                    for label in spoken_labels:
                        if grouped and grouped[-1][0] == label:
                            grouped[-1][1] += 1
                        else:
                            grouped.append([label, 1])
                    step_text = "，再".join(label if count == 1 else f"{count}次{label}" for label, count in grouped)
                    if view.get("screen") == "COMBAT":
                        operation.reason = "本次选择：" + reason_steps
                    names = {r["name"] for values in (context.get("action_refs") or {}).values() for r in values if r["name"]}
                    if scope == "intent":
                        names -= {"防御", "打击"}  # 这两个也是自然语言中的打法词，不能单凭词语抹掉概括发言。
                    # speech 和 reply 各自校验；战术表述出错不能抹掉合法的聊天回复。
                    for field in ("speech", "reply"):
                        original = getattr(operation, field)
                        if not original:
                            continue
                        try:
                            original = re.sub(r"〔\d+〕", "", original)
                            literal = re.sub(r"〔步骤〕|\{(?:steps|(?:card|target):\d+)\}", "", original)
                            if field == "speech" and bindings:
                                card_names = {selected[i].get("bindings", {}).get("card", {}).get("name")
                                              for i in positions} - {None, ""}
                                if card_names:
                                    pattern = r"([一二三四五六七八九十两\d]+)张\s*(" + "|".join(
                                        re.escape(name) for name in sorted(card_names, key=len, reverse=True)) + ")"
                                    def check_count(match: re.Match) -> str:
                                        word, name = match.group(1), match.group(2)
                                        count = int(word) if word.isdigit() else {
                                            "一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5,
                                            "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}.get(word)
                                        matches = [i for i in positions if selected[i].get("bindings", {}).get("card", {}).get("name") == name]
                                        if count is None or not 0 < count <= len(matches):
                                            self._logger.info("[sts2_speech_advisory] reason=card_count text=%s claimed=%s bound=%s",
                                                              original, word, len(matches))
                                            return ""
                                        used.update(matches[:count])
                                        return ""
                                    literal = re.sub(pattern, check_count, literal)
                                # 自然表达只作一致性提示，显式步骤引用仍严格绑定。
                                explicit_names = names | {name for name in ("防御", "打击")
                                                          if re.search(r"(?:用|出|张)" + name, literal)}
                                for name in sorted(explicit_names, key=len, reverse=True):
                                    if name not in literal:
                                        continue
                                    matches = [i for i in positions for b in (selected[i].get("bindings") or {}).values()
                                               if isinstance(b, dict) and b.get("name") == name]
                                    if not matches:
                                        self._logger.info("[sts2_speech_advisory] reason=unselected_entity name=%s text=%s", name, original)
                                    used.update(matches)
                                    literal = literal.replace(name, "")
                                if re.search(r"[一二三四五六七八九十两\d]+张", literal):
                                    self._logger.info("[sts2_speech_advisory] reason=unbound_count text=%s", original)
                            if field == "speech" and view.get("screen") == "COMBAT" and re.search(
                                    r"(?:凑|挡住|获得|造成|打掉|激发|收掉).{0,6}[\d一二三四五六七八九十两]+(?:点|伤|血|张|次)", literal):
                                self._logger.info("[sts2_speech_advisory] reason=unverified_numbers text=%s", original)
                            if len(re.findall(r"〔步骤〕|\{steps\}", original)) > 1:
                                raise ValueError("一句话只能引用一次整段步骤")
                            if "〔步骤〕" in original or "{steps}" in original:
                                if not step_text:
                                    raise ValueError("发言没有有效步骤可引用")
                                used.update(positions)
                                original = original.replace("〔步骤〕", step_text).replace("{steps}", step_text)
                            rendered = re.sub(r"\{(card|target):(\d+)\}", fill, original)
                            setattr(operation, field, rendered)
                        except ValueError as exc:
                            setattr(operation, field, "")
                            if field == "speech":
                                speech_error = str(exc)
                            self._logger.info("[sts2_speech_unbound] field=%s reason=%s text=%s", field, exc, original)
                    if scope == "steps":
                        if operation.speech and not used:
                            used.update(positions)
                        operation.speech_step_ids = sorted(used)
                    elif used:
                        operation.speech_step_ids = sorted(set(operation.speech_step_ids or []) | used)
                if (view.get("screen") == "COMBAT" and context.get("turn_speech_required")
                        and context.get("speech_allowed", True) and not operation.speech
                        and not (requires_guidance_speech and operation.reply)):
                    meaningful = [item for item in selected if item.get("action_type") != "end_turn"]
                    intent = answer.get("speech_intent")
                    intent = intent.strip()[:120] if isinstance(intent, str) else ""
                    if intent:
                        # 模型给了不含牌名/数值的自然动机：续步被裁或speech缺失时用它兜底，
                        # 避免"我先X打Y"的机械句（2026-10-17 20:45:33 现场）。
                        operation.speech = _public_text(intent, context.get("active_requirements", []))
                        operation.speech_scope = "intent"
                        operation.speech_step_ids = [0]
                        self._logger.info("[sts2_speech_repaired] action=%s reason=%s source=speech_intent",
                                          action_type, speech_error or "missing")
                    elif step_text and meaningful:
                        operation.speech = "我先" + step_text + "。"
                        operation.speech_scope = "steps"
                        operation.speech_step_ids = list(positions)
                        self._logger.info("[sts2_speech_repaired] action=%s reason=%s source=bound_steps", action_type, speech_error or "missing")
                    elif invalid_attempts > 0:
                        # 已做过一次格式修正仍拿不到可用发言：按既有约定抑制发言、保留合法动作继续执行，
                        # 不因文案缺失把整步判失败（2026-10-17 19:50:12/19:52:20 各白烧一轮模型）。
                        self._logger.warning("[sts2_speech_deferred] action=%s reason=turn_speech_missing 已修正一次，保留合法动作", action_type)
                    else:
                        raise ValueError("本回合speech必填，需表达已选动作的目标与取舍")
                if validate is not None:
                    validation = validate(operation)
                    if isinstance(validation, tuple):
                        valid, feedback = validation
                    else:
                        valid, feedback = validation, "动作参数不合法，请核对下标、可出牌状态和合法目标"
                    if valid is None:
                        raise ValueError(feedback)
                if requires_reply and not operation.reply:
                    raise ValueError("玩家reply引用无效；请用自然语言直接回应原话，speech不能代替reply")
                if requires_guidance_speech and not operation.speech and not operation.reply:
                    if invalid_attempts == 0:
                        raise ValueError("guidance需要非空speech回应原话，说明采纳后的行动或当前做不到的原因与后续安排。")
                    self._logger.warning("[sts2_speech_deferred] action=%s reason=guidance_missing 已修正一次，保留合法动作和要求更新", action_type)
                if operation.skip_playable_cards:
                    # 逐张理由只写进结构化字段（sts2_end_turn_choice/JSON）用于核对，不再拼进发言：
                    # 2026-10-17 21:30 现场模型已用一句话概括，插件又追加"防御先不打…；模组改造先不打…"，
                    # 主人要求概括即可、不要每句都说。
                    operation.speech_scope = "intent"
                if requires_guidance_speech and operation.reply and operation.speech:
                    operation.speech = ""
                    self._logger.info("[sts2_instruction_response] channel=reply suppressed_plan_speech=true")
                if new_messages and route_messages is not None:
                    route_messages(effects)
                if new_messages:
                    self._logger.info("[sts2_input_decision] effects=%s update=%s control=%s reply_chars=%s speech_chars=%s",
                                      json.dumps(effects, ensure_ascii=False), update, control, len(operation.reply), len(operation.speech))
                danger = end_turn_danger(view.get("combat") or {}) if operation.action_type == "end_turn" else ""
                if danger:
                    if requires_guidance_speech and operation.reply:
                        operation.reply += " " + danger
                    else:
                        operation.speech = danger
                    operation.speech_scope = "intent"
                    operation.speech_step_ids = [0]
                    operation.reason += "；" + danger
                # ask 在发言/安全提示定稿后并入，避免被 danger 覆盖；reply/speech 都不允许时丢弃。
                ask = _merge_ask("", answer.get("ask"), context.get("active_requirements", []))
                if ask:
                    if operation.speech:
                        operation.speech = (operation.speech.rstrip() + " " + ask).strip()
                        operation.speech_scope = "intent"
                    elif operation.reply:
                        operation.reply = (operation.reply.rstrip() + " " + ask).strip()
                    elif context.get("speech_allowed", True):
                        operation.speech = ask
                        operation.speech_scope = "intent"
                # 情绪吐槽：模型自判，接在同一句后面；服务层按冷却给 banter_allowed，太频繁时直接丢弃。
                banter = answer.get("banter")
                if context.get("banter_allowed", True) and isinstance(banter, str) and banter.strip():
                    quip = _public_text(banter.strip(), context.get("active_requirements", []))[:40].strip()
                    if (quip and context.get("speech_allowed", True)
                            and quip not in (operation.speech or "") and quip not in (operation.reply or "")):
                        field_name = "speech" if operation.speech or not operation.reply else "reply"
                        setattr(operation, field_name, (getattr(operation, field_name).rstrip() + " " + quip).strip())
                        if field_name == "speech":
                            operation.speech_scope = "intent"
                        operation.banter_used = True
                asked = _asked(operation.reply, operation.speech)
                operation.asked = asked
                self._logger.info("[sts2_companion_interaction] asked=%s awaiting_answer=%s reply_kind=%s banter=%s",
                                  asked, bool(asked and not operation.action_type), reply_kind, operation.banter_used)
                final_steps = []
                for step in usable_steps:
                    public = {k: deepcopy(step[k]) for k in ("action_type", "kwargs", "assumptions", "reason", "completes_requirements") if k in step}
                    for field, index_key, ref_key in (("card", "card_index", "card_ref"), ("target", "target_index", "target_ref")):
                        if step.get("bindings", {}).get(field):
                            public["kwargs"].pop(index_key, None)
                            public["kwargs"][ref_key] = step["bindings"][field]["ref"]
                    final_steps.append(public)
                answer.update(speech=operation.speech, reply=operation.reply, reason=operation.reason,
                              model_reason=operation.model_reason, continuation=final_steps,
                              speech_scope=operation.speech_scope, speech_step_ids=operation.speech_step_ids,
                              plan_update=operation.plan_update)
                self._commit_history(messages, {**reply, "content": json.dumps(answer, ensure_ascii=False)})
                self._logger.info("[sts2_solver_selection] consulted=%s adopted=%s usable_steps=%s selected_steps=%s",
                                  operation.solver_used, solver_adopted, len(solver_steps), len(usable_steps) + 1)
                return operation
            except (ValueError, TypeError, IndexError) as exc:
                failure_kind = reply.get("failure_kind") or ("invalid_json" if isinstance(exc, json.JSONDecodeError) else "contract_violation")
                self._logger.info("[sts2_llm_invalid] action=%s kwargs=%s error=%s failure_kind=%s finish_reason=%s", answer.get("action_type") if isinstance(answer, dict) else None,
                                  answer.get("kwargs") if isinstance(answer, dict) else None, exc, failure_kind, reply.get("finish_reason"))
                if failure_kind == "budget_exhausted":
                    if narrowed:
                        raise RuntimeError(f"LLM 决策无效：{exc}") from exc
                    # 同参数原样重试只会再烧一次；改成"收窄提问 + 去掉历史现场"的一次降级重试。
                    narrowed = True
                    invalid_attempts += 1
                    self._logger.info("[sts2_budget_narrowed_retry] turn=%s solver_steps=%s",
                                      view.get("turn"), len(solver_steps))
                    messages.append({"role": "user", "content": self._narrowed_retry(payload, solver_steps)})
                    continue
                if isinstance(answer, dict) and answer.get("action_type") == "end_turn":
                    self._logger.info("[sts2_end_turn_invalid] plan_source=%s skipped=%s reason=%s",
                                      answer.get("plan_source"), json.dumps(answer.get("skip_playable_cards") or answer.get("skip_zero_cost_cards"), ensure_ascii=False),
                                      answer.get("reason"))
                invalid_attempts += 1
                if invalid_attempts >= 2:
                    raise RuntimeError(f"LLM 决策无效：{exc}") from exc
                messages.extend([
                    {"role": "assistant", "content": text or ""},
                    {"role": "user", "content": f"输出无效：{exc}。请重新返回完整合法JSON决策，保留有效回复及确定的continuation/end_turn安排。"},
                ])
        raise RuntimeError("模型在有界调用内未选择动作")
