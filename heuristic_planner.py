from __future__ import annotations

import re
from time import monotonic
from typing import Any

from .planner_interface import PlannedOperation


def _first_line_step(solver_plan: dict[str, Any]) -> dict[str, Any] | None:
    """Return the plan's first executable move (line[0].steps[0]) when /solver/plan is turn-grouped."""
    line = solver_plan.get("line") if isinstance(solver_plan.get("line"), list) else None
    if not line or not isinstance(line[0], dict):
        return None
    steps = line[0].get("steps") if isinstance(line[0].get("steps"), list) else None
    if not steps or not isinstance(steps[0], dict):
        return None
    return steps[0]


def _line_step_at(solver_plan: dict[str, Any], index: int | None) -> dict[str, Any] | None:
    """Return line[0].steps[index] (当前回合第 index 步)，越界时夹到首/末步。整回合缓存时按步进取。"""
    line = solver_plan.get("line") if isinstance(solver_plan.get("line"), list) else None
    if not line or not isinstance(line[0], dict):
        return None
    steps = line[0].get("steps") if isinstance(line[0].get("steps"), list) else None
    if not steps or not steps:
        return None
    i = index if isinstance(index, int) else 0
    if i < 0:
        i = 0
    if i >= len(steps):
        i = len(steps) - 1
    step = steps[i]
    return step if isinstance(step, dict) else None


MAP_POLICY_WAIT = "wait"
MAP_POLICY_FOLLOW = "follow"
MAP_POLICY_AUTO = "auto"
MAP_POLICIES = (MAP_POLICY_WAIT, MAP_POLICY_FOLLOW, MAP_POLICY_AUTO)

# "定个路线"（sts2_set_route mode=safe）用的内置权重：躲精英、血少优先休息。玩家自己配过 route_policy
# 时以玩家的权重为准（见 _safe_route_preferences），这里只是没配过时的保守默认。
SAFE_MAP_WEIGHTS: dict[str, int] = {
    "rest": 16,
    "shop": 8,
    "treasure": 6,
    "event": 6,
    "question": 5,
    "monster": 2,
    "elite": -18,
}
SAFE_MAP_LOW_HP_WEIGHTS: dict[str, int] = {
    "rest": 26,
    "shop": 12,
    "elite": -40,
    "monster": -2,
    "event": 4,
    "question": 3,
    "treasure": 2,
}
SAFE_MAP_HP_RATIO_BELOW = 0.6

# ---- 路线链式递推（lookahead）------------------------------------------------------------
#
# mod 的 /state `map.nodes` 给了**整幕的 DAG**：每个节点带 row/col/node_type，且 children/parents
# 是 `{row,col}` **坐标对**。旧代码也有"前瞻"（`_score_map_node` 里累加 `_collect_route_descendants`），
# 但它是**假前瞻**：只读节点自身的 children，而交给规划器的 available_nodes 根本没这个字段；
# 就算读全图，children 里是坐标、没有 node_type，取出来是空串 -> 权重 0 -> 前瞻恒等于 0。
#
# 现在改成真正的链式递推：用坐标建索引，从每个候选节点递归到 boss，按"饱和收益 + 逐层折扣 +
# 临门加权"评估**整条路线**，而不是只看眼前那一步。
ROUTE_DISCOUNT_PER_ROW = 0.8      # 每远一层，未来收益打几折（越远越不确定）
ROUTE_REPEAT_DECAY = 0.5          # 同类节点第 2、3 次出现按此递减：两个休息点 ≠ 两倍休息
ROUTE_BOSS_PREP_ROWS = 2          # 距 boss 不超过这几层算"临门一脚"
ROUTE_BOSS_PREP_MULTIPLIER = 2.0  # 临门休息/商店的倍率（补血、把金币花掉）
ROUTE_BOSS_PREP_GOLD = 120        # 金币不到这个数，"临门商店"就没那么香
ROUTE_LOOKAHEAD_ROWS_FALLBACK = 6 # 没配 lookahead_depth 时向前看几层（幕内层数一般 10~17）

# 篝火选项的语义关键词（mod 发的是 option_id / title / description）。
# 玩家口径：默认该**优先锻造升级**（升数值、降费用，战力涨得比几十点血值），血低才回血。
_REST_SMITH_TOKENS = ("smith", "upgrade", "forge", "temper", "锻造", "升级", "淬炼", "打磨")
_REST_HEAL_TOKENS = ("rest", "heal", "recover", "mend", "sleep", "休息", "回血", "治疗", "恢复")
# 血比低于这个值才把"回血"排在"锻造"前面。
REST_HEAL_HP_RATIO = 0.6
# 精英的机会成本随血量缓和：满血时惩罚打对折（精英给遗物+金币，未必是坏事），
# 血少时保持原样。**只打折、不清零**，否则满血会让精英变成免费。
ROUTE_ELITE_RELIEF_MAX = 0.5

# mod 下发的 node_type 是**游戏枚举名**（`Point.PointType.ToString()`），不是权重表的短键：
# 取值见 mod 自己的脚本 game_mod/scripts/test-natural-room-chain.ps1 与实测：
#   Monster / Elite / Shop / RestSite / Treasure / Unknown / Event（+ Ancient 起始点、Boss / SecondBoss）
# 关键的一条：`RestSite` 与权重键 `rest` **拼不上**，于是休息点权重恒为 0 —— 而 `_preferred_map_index`
# 过去还要求"分数 > 0 才采纳"，结果怪物（3 分）永远压过营地（0 分），**猫娘从不主动回血**。
_MAP_NODE_TYPE_ALIASES: dict[str, str] = {
    "monster": "monster",
    "enemy": "monster",
    "fight": "monster",
    "elite": "elite",
    "shop": "shop",
    "merchant": "shop",
    "restsite": "rest",
    "rest": "rest",
    "campfire": "rest",
    "treasure": "treasure",
    "chest": "treasure",
    "unknown": "question",
    "question": "question",
    "event": "event",
    "boss": "boss",
    "secondboss": "boss",
    "ancient": "ancient",
}

# 方位词 -> 取候选里的哪一项。按"先长词后短词"的顺序匹配，避免"最右边"被"右"提前吃掉。
_MAP_PICK_FIRST_TOKENS = ("最左", "第一个", "第一", "左边", "左", "first", "left")
_MAP_PICK_LAST_TOKENS = ("最右", "最后一个", "最后", "右边", "最右", "右", "last", "right")
_MAP_PICK_MIDDLE_TOKENS = ("中间", "正中", "当中", "中间那个", "middle", "center", "centre", "mid")
_MAP_PICK_CN_DIGITS = {
    "一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5,
    "六": 6, "七": 7, "八": 8, "九": 9, "十": 10,
}


def normalize_map_policy(value: Any) -> str:
    """未知取值一律退化为 wait：宁可不投，也不去抢玩家已经定好的路线。"""
    text = str(value or "").strip().lower()
    return text if text in MAP_POLICIES else MAP_POLICY_WAIT


def parse_map_pick(token: Any, count: int) -> int | None:
    """把"中间 / 第二个 / 最左 / 最右 / 3"这类说法解析成候选位置（0 起算）。

    数字按**序数**理解（2 = 第二个 = 下标 1），与口语一致；要按 0 起算的明确下标请用 index 参数。
    解析不出返回 None，由调用方决定是报错还是退回安全路线。
    """
    if count <= 0:
        return None
    if isinstance(token, bool):
        return None
    if isinstance(token, int):
        return token - 1 if 1 <= token <= count else None
    text = str(token or "").strip().lower()
    if not text:
        return None
    for probe in _MAP_PICK_MIDDLE_TOKENS:
        if probe in text:
            return (count - 1) // 2
    for probe in _MAP_PICK_FIRST_TOKENS:
        if probe in text:
            return 0
    for probe in _MAP_PICK_LAST_TOKENS:
        if probe in text:
            return count - 1
    digits = re.findall(r"\d+", text)
    if digits:
        ordinal = int(digits[0])
        return ordinal - 1 if 1 <= ordinal <= count else None
    for char in text:
        if char in _MAP_PICK_CN_DIGITS:
            ordinal = _MAP_PICK_CN_DIGITS[char]
            return ordinal - 1 if 1 <= ordinal <= count else None
    return None


# ---- 商店统一评分 × 战斗用药水（规格 docs/shop_scoring.md，玩家口径 2026-10-07）----------------
#
# 玩家原话：
#   「商店好像很喜欢买遗物？这里最好评估一下那些是真正需要的。如果没有需要的就留到下一个商店」
#   「还可以买药，买两瓶药可以冲精英」
#   「这一块需要量化牌组和商店的可购买项，买评分最高的，都不高就不买」
#   「还有每次对战如果损血大于8就鼓励使用药水」
#
# 这套常量就是那份规格里的「五、配置项」（默认按保守策略）。玩家自己配过 shop_policy 记录时，
# 用同一批键名覆盖（见 _shop_policy），与 route_policy / deck_policy 一个路子。
SHOP_SCORE_THRESHOLD = 40.0          # shop_score_threshold：统一门槛
SHOP_ELITE_POTION_BOOST = 1.4        # shop_elite_potion_boost：冲精英时药水加权
SHOP_GOLD_FLOOR = 120                # shop_gold_floor：留给下一个商店的底钱
SHOP_GOLD_RICH = 300                 # 金币多于它 -> 门槛略降（避免"攒着不花"）
SHOP_GOLD_POOR = 100                 # 金币少于它 -> 门槛升高（优先留钱）
SHOP_THRESHOLD_RICH_DELTA = -8.0     # 金币很多时的门槛偏移
SHOP_THRESHOLD_POOR_DELTA = 12.0     # 金币很少时的门槛偏移
SHOP_ELITE_THRESHOLD_DELTA = -12.0   # 前方有精英 -> 门槛下调（药水更容易买）
SHOP_MATURE_THRESHOLD_DELTA = 10.0   # 遗物覆盖度整体很高（构筑成型）-> 门槛上调，倾向留钱
SHOP_COVERAGE_MATURE_RATIO = 0.5     # 缺口被已有遗物覆盖掉一半以上 = "构筑已成型"
SHOP_THRESHOLD_MIN = 10.0
SHOP_THRESHOLD_MAX = 90.0
SHOP_POTION_PRICE_FALLBACK = 60      # 读不到商店药水标价时，"补满栏位要多少钱"按这个估
SHOP_CARD_FIT_SCALE = 60.0           # _score_reward_card 原始分 -> 补缺分 0..1 的折算（见 _score_shop_card）
SHOP_UNKNOWN_RELIC_GAP_RATIO = 0.5   # 读不出效果类别的遗物：按"最缺那一维"打折算（保守先验）
SHOP_RELIC_RARITY_BONUS = {          # 稀有度加分（点数，直接加在 0..100 的补缺分上）
    "rare": 18.0,
    "uncommon": 10.0,
    "common": 4.0,
    "boss": 22.0,
    "ancient": 22.0,
    "event": 12.0,
    "shop": 8.0,
    "starter": 0.0,
}
SHOP_RELIC_KEYWORD_BONUS = 6.0       # 命中一个"正面关键词组"加多少
SHOP_RELIC_KEYWORD_BONUS_CAP = 12.0  # 关键词加分上限（不让稀有度+关键词把"没缺口"顶过门槛）
SHOP_RELIC_CONDITION_PENALTY = 10.0  # 条件苛刻/负面遗物的降权
SHOP_COST_PENALTY_SCALE = 50.0       # 代价惩罚 = 价格/金币 × 这个系数
SHOP_COST_PENALTY_HALF_BONUS = 30.0  # 花掉一半以上的钱：重罚
SHOP_POTION_NEEDED_FIT = 0.85        # 命中"还缺的那一类"药水的补缺分
SHOP_POTION_COVERED_FIT = 0.20       # 已经有的那一类：低分（药水栏位有限，重复买同一类不值）
SHOP_POTION_BASELINE_FIT = {         # 没有"冲精英"情境时的基础救急价值
    "defense": 0.60,
    "burst": 0.55,
    "aoe": 0.50,
    "heal": 0.50,
    "cleanse": 0.45,
    "draw": 0.30,
    "other": 0.20,
}
# "冲精英需要的那几类"：爆发 + 群伤 + 减伤 + 回血（规格「一之二」给的例子是"还缺爆发类"）。
SHOP_ELITE_POTION_NEEDS = ("defense", "burst", "aoe", "heal")

# 牌组缺口 / 遗物覆盖 / 药水用途共用的六维（规格「一、牌组量化」）。
SHOP_DIM_OUTPUT = "output"
SHOP_DIM_DEFENSE = "defense"
SHOP_DIM_DRAW = "draw"
SHOP_DIM_ENERGY = "energy"
SHOP_DIM_AOE = "aoe"
SHOP_DIM_QUALITY = "quality"
SHOP_DIMENSIONS = (
    SHOP_DIM_OUTPUT,
    SHOP_DIM_DEFENSE,
    SHOP_DIM_DRAW,
    SHOP_DIM_ENERGY,
    SHOP_DIM_AOE,
    SHOP_DIM_QUALITY,
)
SHOP_DIM_LABELS = {
    SHOP_DIM_OUTPUT: "输出",
    SHOP_DIM_DEFENSE: "格挡",
    SHOP_DIM_DRAW: "抽牌",
    SHOP_DIM_ENERGY: "费用",
    SHOP_DIM_AOE: "群伤",
    SHOP_DIM_QUALITY: "质量",
}
# 每回合大概能打出几张牌（估算"一回合能打多少伤害/多少格挡"）。牌组量化只用得到它的量级。
SHOP_CARDS_PER_TURN = 3
# 每层期望：输出/格挡的目标线随层数抬高（规格：「低于每层期望 → 缺口高」）。
SHOP_TARGET_DAMAGE_BASE = 10.0
SHOP_TARGET_DAMAGE_PER_FLOOR = 0.8
SHOP_TARGET_BLOCK_BASE = 6.0
SHOP_TARGET_BLOCK_PER_FLOOR = 0.6
# 牌组大于这么多张才开始嫌"循环不动"；没抽牌手段时缺口按比例放大。
SHOP_DRAW_SIZE_BASE = 14
SHOP_DRAW_SIZE_SPAN = 10
SHOP_DRAW_CARD_CREDIT = 2          # 每 2 张抽牌牌就把"循环缺口"抹平
SHOP_AOE_TARGET_UNITS = 3.0        # 群伤/状态牌的目标张数（群伤算 1 张，单体状态算半张）
SHOP_STATUS_CARD_CREDIT = 0.5
# "牌组质量"缺口：未升级的打击/防御 与 诅咒/状态牌。
SHOP_QUALITY_BASIC_TARGET = 6.0
SHOP_QUALITY_CURSE_TARGET = 2.0

# 牌组量化用的词表（mod 下发的 card_type 是 C# 枚举名：Attack/Skill/Power/Status/Curse；
# 卡名/规则文本可能是中文，所以中英都留一份）。
_DECK_ATTACK_TYPE_TOKENS = ("attack", "攻击")
_DECK_BASIC_CARD_TOKENS = ("strike", "defend", "打击", "防御")
_DECK_CURSE_TYPE_TOKENS = ("curse", "诅咒", "status", "状态")
_DECK_DRAW_TOKENS = ("draw", "抽", "replay", "重放", "exhaust", "消耗", "scry", "预见", "discard", "弃牌")
_DECK_AOE_TOKENS = ("all enemies", "all enemy", "所有敌人", "全体敌人", "全部敌人", "each enemy", "每个敌人", "aoe")
_DECK_STATUS_TOKENS = ("vulnerable", "易伤", "weak", "虚弱", "poison", "中毒", "burn", "燃烧", "slow", "迟缓", "debuff", "负面")
# 药水类别之间的优先级（同样是"还缺这一类"时，减伤 > 爆发 > 群伤 > 回血 > 清状态）。
_POTION_CATEGORY_PRIORITY = {
    "defense": 1.0,
    "burst": 0.95,
    "aoe": 0.9,
    "heal": 0.85,
    "cleanse": 0.8,
    "draw": 0.5,
    "other": 0.3,
}

# 遗物效果类别 -> 缺口维度（规格「一之二」的类别表）。一张表同时给"已有遗物覆盖度"和"商店遗物补缺"用。
_RELIC_EFFECT_DIM_TOKENS: dict[str, tuple[str, ...]] = {
    SHOP_DIM_OUTPUT: ("力量", "strength", "伤害", "damage", "攻击", "attack"),
    SHOP_DIM_DEFENSE: ("格挡", "block", "敏捷", "dexterity", "减伤", "护盾", "护甲", "plated", "shield"),
    SHOP_DIM_ENERGY: ("能量", "energy", "费用", "cost", "energy gain"),
    SHOP_DIM_DRAW: ("抽牌", "draw", "手牌", "hand", "弃牌", "discard"),
}
# 生存/回复也算"格挡/生存"这一维（规格里牌组量化的维度就叫「格挡/生存」）。
_RELIC_SURVIVAL_TOKENS = ("治疗", "heal", "回复", "restore", "regenerate", "再生", "最大生命", "max hp", "复活", "revive")
# 经济类没有对应的牌组缺口维度：钱够时它几乎没用，只在钱少时略微值一点（见 _score_shop_relic）。
_RELIC_ECON_TOKENS = ("金币", "gold", "商店", "shop", "价格", "price")
_RELIC_COMBAT_START_TOKENS = ("战斗开始时", "at the start of combat", "每回合开始时", "at the start of each turn")
_RELIC_STRONG_TOKENS = ("力量", "strength", "敏捷", "dexterity", "能量", "energy", "格挡", "block", "抽牌", "draw", "治疗", "heal")
# 条件苛刻 / 有代价的遗物要降权（规格：「条件苛刻或负面的降权」）。
_RELIC_HARSH_TOKENS = (
    "生命值低于", "血量低于", "hp is below", "低于 50", "失去", "lose",
    "诅咒", "curse", "无法", "cannot", "随机", "random", "只在", "only if",
)

# 药水用途 -> 类别（规格「一之二」「二」：单体爆发 / 群伤 / 减伤 / 回血 / 清状态 / 抽牌 / 其它）。
# 判定顺序很重要：EXPLOSIVE_POTION（对所有敌人造成伤害）必须落到"群伤"，不能被"伤害"先判成爆发。
_POTION_AOE_TOKENS = ("all enemies", "all enemy", "所有敌人", "全体敌人", "全部敌人", "each enemy", "每个敌人", "aoe", "炸弹", "bomb", "explosive", "爆炸")
_POTION_DEFENSE_TOKENS = ("block", "格挡", "护盾", "shield", "dexterity", "敏捷", "dodge", "闪避", "intangible", "无形", "ghost", "幽灵", "weak", "虚弱", "减伤", "damage reduction", "armor", "护甲")
_POTION_HEAL_TOKENS = ("heal", "回复", "治疗", "regen", "再生", "restore", "恢复", "blood", "血", "fruit", "max hp", "最大生命")
_POTION_CLEANSE_TOKENS = ("remove", "清除", "cure", "净化", "purify", "debuff", "负面", "artifact", "神器", "免疫", "dispel", "驱散")
_POTION_BURST_TOKENS = ("strength", "力量", "fire", "火焰", "damage", "伤害", "rage", "狂暴", "duplicate", "复制", "fear", "恐惧", "poison", "中毒")
# "抽牌"这一类按规格的命名收口，装的是**资源型**药水（抽牌 / 找牌 / 加能量）：它们都能让你多打几张，
# 但都不是"能把这一下伤害压下去"的保命手段，所以在战斗里一样评 0 分（规格「八、用哪一瓶」第 5 条）。
_POTION_DRAW_TOKENS = ("draw", "抽牌", "hand", "手牌", "discard", "弃牌", "liquid memories", "记忆", "insight", "洞察", "replay", "重放", "card", "卡牌", "能量", "energy")

_POTION_CATEGORY_LABELS = {
    "burst": "单体爆发",
    "aoe": "群伤",
    "defense": "减伤",
    "heal": "回血",
    "cleanse": "清状态",
    "draw": "抽牌",
    "other": "其它",
}

# ---- 战斗用药水（规格「八」）---------------------------------------------------------------
COMBAT_POTION_HP_LOSS_THRESHOLD = 8.0     # potion_hp_loss_threshold：预计损血 > 8 就鼓励用药水
COMBAT_POTION_TRASH_THRESHOLD_MULTIPLIER = 1.25  # 已知是普通小怪战：阈值上调一点（更省）
COMBAT_POTION_ELITE_THRESHOLD_MULTIPLIER = 1.0   # 精英/Boss：不动阈值（更愿意喝）
COMBAT_POTION_LOW_HP_RATIO = 0.3          # 血量低于三成：回血类也值得喝
# 「能不能把这 8 点以上损血压下去」的评分（规格「用哪一瓶」的 1~5 条）。
COMBAT_POTION_SCORES = {
    "defense": 88.0,   # 1. 减伤/闪避/格挡：最高（直接抵掉这一下）
    "cleanse": 86.0,   # 4. 清状态：与减伤同档的备选
    "aoe": 72.0,       # 2. 群伤/清场：次高（把还在打你的怪清掉）
    "burst": 55.0,     # 单体爆发：能秒掉一只，但压不下这一下
    "heal": 0.0,       # 3. 回血：只在会死 / 血量 < 30% 时才算数（见 _combat_potion_score）
    "draw": 0.0,       # 5. 抽牌等：战斗里不当保命手段，不因这条规则触发
    "other": 0.0,
}
COMBAT_POTION_LETHAL_HEAL_SCORE = 86.0    # 预计会死时的回血类评分
COMBAT_POTION_LOW_HP_HEAL_SCORE = 80.0    # 血量低于三成时的回血类评分


class STS2HeuristicPlanner:
    def __init__(self, logger: Any | None = None) -> None:
        self._logger = logger
        # 地图投票策略，由 service.startup 用插件配置覆盖：
        #   wait（默认）——多人局里彻底等待：不投票、也不自动跟票，直到玩家通过对 NEKO 说话下令
        #                 （见 sts2_set_route）。单机局没有队友可等，仍按自己的偏好选，否则会卡死。
        #   follow     ——玩家已投票就镜像跟随；无人投票满 map_vote_wait_seconds 后自己选。
        #   auto       ——不等，立即按自己的偏好选。
        self._map_policy = MAP_POLICY_WAIT
        self._map_vote_wait_seconds = 20.0
        self._map_wait_key = ""
        self._map_wait_started_at = 0.0
        # 选牌屏的推进游标：签名 + 这一屏已按过的 option_index 集合。同一屏上永远不重复按同一张卡，
        # 否则需要选 2 张的界面会一直重按第 0 张、max_select 永远到不了（"选两张卡卡住"）。
        # 注意 _selection_pressed 只由**真的按键**推进（note_selection_option_pressed），
        # 不由规划推进——plan() 每 tick 都跑，用规划推进会把候选白白耗光并导致永久卡屏。
        self._selection_cursor_key = ""
        self._selection_pressed: set[int] = set()
        # 同一屏连续"候选按遍仍返回 None"的次数，达到 _SELECTION_EXHAUSTED_RETRIES 就重置光标重按，
        # 保证这一屏永远不会陷入"再也规划不出动作"的死锁。
        self._selection_exhausted = 0
        # 商店"这次进店看过货了没"：货架开过一次、又关掉之后**不能再开**。
        # mod 的 open_shop_inventory 是**开关**，而开关两态的 screen 都叫 SHOP，
        # 只看 open_shop_inventory 在不在 available_actions 里就会开->关->开无限来回（玩家看到的死循环）。
        self._shop_visit_key = ""
        self._shop_viewed = False
        # 商店统一评分的日志去重键：plan() 每 tick 都跑一次（约 1 次/秒），不去重就是刷屏。
        # 键里带"最高分/门槛/决定"，所以调参后参数一变就会重新记一行。
        self._shop_log_key = ""
        # 最近一次路线决定留下的"前方有没有精英"：**商店界面拿不到地图**（mod 的 map payload 只在
        # 地图屏下发，BuildMapPayload 一离开地图就返回 null），所以只能在选路那一刻把结论记下来，
        # 进店后照着用（规格「路线/精英信息：evaluate_map_route 那一套已经在算精英压力，复用它」）。
        self._route_elite_ahead = False
        self._last_route_node_type = ""
        # 最近一次记录过的地图路线评分（同一局面只记一次，防止每 tick 刷屏）。
        self._last_map_route_log_key = ""
        # 奖励领取游标：当前奖励清单的签名 + 已经**真的领过**的下标。
        # 药水栏满时"领药水奖励"发不下去，而 mod 照样回 completed —— 不记账就会无限重领同一个。
        self._reward_claim_key = ""
        self._reward_claim_tried: set[int] = set()
        # 选人界面：「随机」只按一次（按键由执行推动），按过就转入开局。
        self._character_pick_key = ""
        self._character_pick_done = False
        self._character_choice_log_key = ""
        self._rest_choice_log_key = ""
        # 同类的"别重复按同一个按钮"状态：bundle 选过没、capstone 扫到第几个。
        self._bundle_signature = ""
        self._bundle_chosen = False
        self._capstone_signature = ""
        self._capstone_last_option = -1

    _MAX_CAPSTONE_SWEEP = 8
    # 选牌屏：候选按遍后允许返回 None 的次数，超过就重置光标重按（防死锁，见 _next_selection_option）。
    _SELECTION_EXHAUSTED_RETRIES = 2

    def configure_map_policy(self, *, policy: str, vote_wait_seconds: float) -> None:
        self._map_policy = normalize_map_policy(policy)
        self._map_vote_wait_seconds = max(0.0, float(vote_wait_seconds or 0))
        self._map_wait_key = ""
        self._map_wait_started_at = 0.0

    def _debug(self, message: str, *args: Any) -> None:
        return

    def _log_map_route(self, scored: list[tuple[int, float]], chosen: int, *, hp_ratio: float, gold: int) -> None:
        """把这一 tick 的路线评估结果用 info 级记一次（同一局面只记一次，避免刷屏）。

        为什么要单独写：`_debug` 在本文件里是个**空函数**，所有 [sts2_*] 调试行其实都没输出，
        线上没法核对"到底按什么选的"。选路是玩家最容易质疑的决策，必须留痕。
        """
        if self._logger is None:
            return
        try:
            key = "|".join(f"{index}:{score:.2f}" for index, score in scored)
            if key == self._last_map_route_log_key:
                return
            self._last_map_route_log_key = key
            self._logger.info(
                "[sts2_map_route] 金币=%s 血比=%.2f 候选评分=%s -> 选 option_index=%s",
                gold,
                hp_ratio,
                ", ".join(f"{index}={score:.2f}" for index, score in scored),
                chosen,
            )
        except Exception:
            pass

    @staticmethod
    def _available_actions_signature(available_actions: list[dict[str, Any]]) -> str:
        return ",".join(sorted(str(action.get("type") or "") for action in available_actions if isinstance(action, dict)))

    def _next_capstone_option(self, available_actions: list[dict[str, Any]]) -> int | None:
        """capstone 选项游标：逐项往前，扫过 _MAX_CAPSTONE_SWEEP 项后返回 None（等界面自己变）。"""
        signature = self._available_actions_signature(available_actions)
        if signature != self._capstone_signature:
            self._capstone_signature = signature
            self._capstone_last_option = -1
        option = self._capstone_last_option + 1
        if option >= self._MAX_CAPSTONE_SWEEP:
            return None
        self._capstone_last_option = option
        return option

    # ---- 地图跟随玩家投票 ----------------------------------------------------------------

    def _other_player_vote_index(self, summary_context: dict[str, Any]) -> int | None:
        """别人已投节点位置索引（没人投 / 只有自己投 -> None）。summary_context_builder 已算好。"""
        payload = summary_context.get("payload") if isinstance(summary_context.get("payload"), dict) else {}
        index = payload.get("other_player_vote_index")
        if isinstance(index, int) and index >= 0:
            return index
        return None

    @staticmethod
    def _is_multiplayer_map(summary_context: dict[str, Any]) -> bool:
        """是否多人局。单机地图不该等玩家投票（等也等不到，只会拖慢自动玩）。"""
        payload = summary_context.get("payload") if isinstance(summary_context.get("payload"), dict) else {}
        return bool(payload.get("is_multiplayer"))

    def _map_wait_pending(self, summary_context: dict[str, Any]) -> bool:
        """没人投票时是否还要继续等玩家。

        以地图身份（层级 + 当前节点 + 候选节点数）为键：换图/换层就重新开始计时，不会把上一张图的
        等待时间带过来。等满 _map_vote_wait_seconds 后返回 False，让插件自己选，避免整局卡死。
        """
        if self._map_vote_wait_seconds <= 0:
            return False
        payload = summary_context.get("payload") if isinstance(summary_context.get("payload"), dict) else {}
        nodes = payload.get("available_nodes") if isinstance(payload.get("available_nodes"), list) else []
        current = payload.get("current_node") if isinstance(payload.get("current_node"), dict) else {}
        key = f"{payload.get('floor')}|{current.get('row')},{current.get('col')}|{len(nodes)}"
        now = monotonic()
        if key != self._map_wait_key or self._map_wait_started_at <= 0:
            self._map_wait_key = key
            self._map_wait_started_at = now
            return True
        return (now - self._map_wait_started_at) < self._map_vote_wait_seconds

    # ---- 对话式地图协作（sts2_set_route）--------------------------------------------------

    @staticmethod
    def _map_nodes(summary_context: dict[str, Any]) -> list[dict[str, Any]]:
        """候选节点列表。所谓"位置"就是这个列表的下标，与 mod 侧 available_nodes 的顺序一致。"""
        payload = summary_context.get("payload") if isinstance(summary_context.get("payload"), dict) else {}
        nodes = payload.get("travelable_nodes")
        if not isinstance(nodes, list) or not nodes:
            nodes = payload.get("available_nodes")
        return [node for node in (nodes if isinstance(nodes, list) else []) if isinstance(node, dict)]

    @staticmethod
    def _map_option_index(nodes: list[dict[str, Any]], position: int) -> int:
        """位置 -> choose_map_node 的 option_index。mod 侧就是用这个下标取 available_nodes。"""
        node = nodes[position] if 0 <= position < len(nodes) and isinstance(nodes[position], dict) else {}
        raw = node.get("index")
        return raw if isinstance(raw, int) and not isinstance(raw, bool) else position

    @staticmethod
    def _map_node_type(node: dict[str, Any]) -> str:
        return str(node.get("type") or node.get("node_type") or "?").strip() or "?"

    def _map_decision(self, summary_context: dict[str, Any], preferences: dict[str, Any], snapshot: dict[str, Any] | None = None) -> tuple[int, str] | None:
        """按 map_policy 决定这一 tick 要不要投票、投哪个；返回 None 表示什么都不做。

        wait 在多人局里永远返回 None：不投票、也不跟票，路线完全留给玩家。等玩家发话之后才动，
        那三种说法走 sts2_set_route（safe/mirror/pick），不经过这里。单机局没有队友可等，
        仍然自己选，否则每张图都会白等一轮。
        """
        multiplayer = self._is_multiplayer_map(summary_context)
        policy = self._map_policy
        if policy == MAP_POLICY_WAIT and multiplayer:
            return None
        if policy == MAP_POLICY_FOLLOW:
            follow_index = self._other_player_vote_index(summary_context)
            if follow_index is not None:
                return follow_index, "map_follow_player_vote"
            if multiplayer and self._map_wait_pending(summary_context):
                return None
        return self._map_own_pick(summary_context, preferences, snapshot)

    def _map_own_pick(self, summary_context: dict[str, Any], preferences: dict[str, Any], snapshot: dict[str, Any] | None = None) -> tuple[int, str]:
        node_index = self._preferred_map_index(summary_context, preferences, snapshot)
        return (node_index if isinstance(node_index, int) else 0), "map_preference_or_default"

    def _safe_route_preferences(self, preferences: dict[str, Any] | None) -> dict[str, Any]:
        """"定个路线"用的路由权重：内置保守权重打底，玩家自己配过的权重覆盖其上。"""
        configured = self._route_policy(preferences or {})
        weights = dict(SAFE_MAP_WEIGHTS)
        user_weights = configured.get("weights") if isinstance(configured.get("weights"), dict) else {}
        for key, value in user_weights.items():
            try:
                weights[str(key).strip().lower()] = int(value)
            except (TypeError, ValueError):
                continue
        policy = {
            "instruction_type": "route_policy",
            "weights": weights,
            "conditions": {
                "low_hp": {
                    "hp_ratio_below": SAFE_MAP_HP_RATIO_BELOW,
                    "weights": dict(SAFE_MAP_LOW_HP_WEIGHTS),
                }
            },
            "lookahead_depth": configured.get("lookahead_depth") or 4,
        }
        return {"records": [{"value": policy}]}

    def resolve_route(
        self,
        summary_context: dict[str, Any],
        *,
        mode: str,
        pick: Any = None,
        index: Any = None,
        preferences: dict[str, Any] | None = None,
        snapshot: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """把"定个路线 / 跟你一样 / 选中间"解析成一个具体的 choose_map_node 选项。

        message 是给猫娘直接说出口的中文；option_index 为 None 时调用方不应执行动作。
        """
        mode_text = str(mode or "").strip().lower()
        if mode_text not in {"safe", "mirror", "pick"}:
            mode_text = "safe"
        nodes = self._map_nodes(summary_context)
        catalog = [
            {
                "position": position + 1,
                "option_index": self._map_option_index(nodes, position),
                "node_type": self._map_node_type(node),
            }
            for position, node in enumerate(nodes)
        ]
        result: dict[str, Any] = {
            "ok": False,
            "mode": mode_text,
            "option_index": None,
            "node_type": None,
            "position": None,
            "node_count": len(nodes),
            "nodes": catalog,
            "message": "",
            "error": None,
        }
        if not nodes:
            result["error"] = "not_on_map"
            result["message"] = "现在不是选路线的界面，没办法投地图票。"
            return result

        position: int | None = None
        if mode_text == "mirror":
            mirror = self._other_player_vote_index(summary_context)
            if mirror is None:
                result["error"] = "no_player_vote"
                result["message"] = "你还没在地图上投票呢，我先等着；你选好之后说一声，我就跟你投同一个。"
                return result
            if not 0 <= mirror < len(nodes):
                result["error"] = "no_player_vote"
                result["message"] = "我读到的你的票不在当前候选里，等我刷新一下再说。"
                return result
            position = mirror
        elif mode_text == "pick":
            if isinstance(index, int) and not isinstance(index, bool):
                if 0 <= index < len(nodes):
                    position = index
                else:
                    result["error"] = "index_out_of_range"
                    result["message"] = f"下标 {index} 越界了，现在只有 {len(nodes)} 个可选节点。"
                    return result
            else:
                parsed = parse_map_pick(pick, len(nodes))
                if parsed is None:
                    result["error"] = "pick_unparsed"
                    result["message"] = (
                        f"我没听清要选哪个；说“中间”“第几个”“最左”“最右”都行，现在有 {len(nodes)} 个候选。"
                    )
                    return result
                position = parsed
        else:
            chosen = self._preferred_map_index(summary_context, self._safe_route_preferences(preferences), snapshot)
            if isinstance(chosen, int):
                position = chosen if 0 <= chosen < len(nodes) else None
                if position is None:
                    for position, item in enumerate(catalog):
                        if item["option_index"] == chosen:
                            break
                    else:
                        position = None

        if position is None:
            result["error"] = "no_safe_route"
            result["message"] = "这张图上我没找到更稳妥的节点，等你定我再跟着投。"
            return result

        node_type = self._map_node_type(nodes[position])
        result.update(
            ok=True,
            option_index=self._map_option_index(nodes, position),
            node_type=node_type,
            position=position + 1,
        )
        payload = summary_context.get("payload") if isinstance(summary_context.get("payload"), dict) else {}
        if mode_text == "mirror":
            result["message"] = f"好，我跟你投第 {position + 1} 个（{node_type}）。"
        elif mode_text == "pick":
            result["message"] = f"好，我投第 {position + 1} 个（{node_type}）。"
        else:
            result["message"] = self._safe_route_message(payload, position, node_type)
        return result

    def _safe_route_message(self, payload: dict[str, Any], position: int, node_type: str) -> str:
        current_hp = self._safe_int(payload.get("current_hp")) or 0
        max_hp = self._safe_int(payload.get("max_hp")) or 0
        hp_ratio = (current_hp / max_hp) if max_hp > 0 else 0.0
        if hp_ratio and hp_ratio < SAFE_MAP_HP_RATIO_BELOW:
            return f"我投第 {position + 1} 个（{node_type}）：血量只剩 {int(hp_ratio * 100)}%，先挑稳一点的。"
        return f"我投第 {position + 1} 个（{node_type}）：这条看着最稳妥。"

    def plan(self, context: dict[str, Any]) -> PlannedOperation | None:
        classification = context.get("classification") if isinstance(context.get("classification"), dict) else {}
        summary_context = context.get("summary_context") if isinstance(context.get("summary_context"), dict) else {}
        strategy_context = context.get("strategy_context") if isinstance(context.get("strategy_context"), dict) else {}
        snapshot = context.get("snapshot") if isinstance(context.get("snapshot"), dict) else {}
        state_name = str(classification.get("state_name") or snapshot.get("screen") or "").strip().lower()
        available_actions = snapshot.get("available_actions") if isinstance(snapshot.get("available_actions"), list) else []
        preferences = strategy_context.get("preferences") if isinstance(strategy_context.get("preferences"), dict) else {}
        guidance_override = self._guidance_override(context)

        if state_name == "main_menu":
            # 主菜单优先级：**先「继续」**。原来 open_character_select 排在前面，而主菜单只要单人按钮可用
            # 就会广告 open_character_select（GameStateService.CanOpenCharacterSelect），于是插件会去点
            # 「单人/新建局」，抢在存档前面 —— 这正是「和 AI 组队中途退出后回不到存档」的成因。
            # 只有在没有可继续存档时才允许开新局/看时间线。
            action = (
                self._find_action(available_actions, "continue_run")
                or self._find_action(available_actions, "open_character_select")
                or self._find_action(available_actions, "open_timeline")
            )
            if action is None:
                # 子菜单压着主菜单时「继续」按钮会被藏起来（CanContinueRun 要求 SubmenusOpen == false），
                # 先收干净子菜单，下一 tick 就能看到 continue_run。
                action = self._find_action(available_actions, "close_main_menu_submenu")
                if action is not None:
                    return PlannedOperation(action_type=action["type"], kwargs={}, confidence=0.9, source="heuristic", reason="main_menu_close_submenu")
                action = self._find_action(available_actions, "choose_timeline_epoch")
            if action is not None:
                kwargs = self._main_menu_kwargs(action, summary_context)
                reason = "main_menu_continue_run" if str(action.get("type") or "").lower() == "continue_run" else "main_menu_default"
                return PlannedOperation(action_type=action["type"], kwargs=kwargs, confidence=0.95, source="heuristic", reason=reason)

        if state_name == "multiplayer_lobby":
            # co-op 大厅：选角 -> ready。猫娘侧由 mod 的 NekoAutoplayDriver 驱动，这里是插件驱动的那一份。
            action = self._find_action(available_actions, "ready_multiplayer_lobby")
            if action is not None:
                return PlannedOperation(action_type=action["type"], kwargs={}, confidence=0.9, source="heuristic", reason="multiplayer_lobby_ready")
            action = self._find_action(available_actions, "select_character")
            if action is not None:
                selected_index = self._selected_character_index(summary_context)
                kwargs = {"option_index": selected_index} if selected_index is not None else {}
                return PlannedOperation(action_type=action["type"], kwargs=kwargs, confidence=0.85, source="heuristic", reason="multiplayer_lobby_select_character")
            action = self._find_action(available_actions, "join_multiplayer_lobby")
            if action is not None:
                return PlannedOperation(action_type=action["type"], kwargs={}, confidence=0.8, source="heuristic", reason="multiplayer_lobby_join")

        if state_name == "character_select":
            action = self._find_action(available_actions, "select_character")
            embark_action = self._find_action(available_actions, "embark")
            # 玩家口径：**每局角色随机**。用游戏自带的「随机」候选（is_random），而不是插件自己摇 ——
            # 那样连"随机"这个语义都交给游戏，公告时再读它实际抽到谁（见 service._maybe_announce_character）。
            #
            # 老毛病提醒：`_selected_character_index` 取的是"当前已选中"的角色下标，所以再发一次
            # select_character(那个下标) 是 no-op —— 屏幕不推进、下一 tick 又发，无限重按。
            # 这里靠"按过一次就不再按"（记账由执行推动，见 note_character_select_dispatched）保证推进。
            signature = self._character_list_signature(summary_context)
            if signature != self._character_pick_key:
                self._character_pick_key = signature
                self._character_pick_done = False
            random_index = self._random_character_index(summary_context)
            already_random = self._selected_character_is_random(summary_context)
            if action is not None and random_index is not None and not already_random and not self._character_pick_done:
                self._log_character_choice("random", random_index)
                return PlannedOperation(
                    action_type=action["type"],
                    kwargs={"option_index": random_index},
                    confidence=0.9,
                    source="heuristic",
                    reason="character_select_random",
                )
            if embark_action is not None:
                # 角色已定（随机项或上一次玩的）就该开局。
                self._log_character_choice("embark", None)
                return PlannedOperation(action_type=embark_action["type"], kwargs={}, confidence=0.9, source="heuristic", reason="character_select_embark")
            if action is not None:
                selected_index = self._selected_character_index(summary_context)
                kwargs = {"option_index": selected_index} if selected_index is not None else {}
                return PlannedOperation(action_type=action["type"], kwargs=kwargs, confidence=0.9, source="heuristic", reason="character_select_default")

        if state_name == "event":
            action = self._find_action(available_actions, "choose_event_option")
            if action is not None:
                # 事件房是纯启发式的：插件不再有任何 LLM 决策（2026-10 摘除 EventAdviceGenerator）。
                preferred_option = self._preferred_event_option(summary_context, preferences)
                self._debug(
                    "[sts2_event_plan] options=%s chosen=%s payload=%s",
                    summary_context.get("payload", {}).get("event_options") if isinstance(summary_context.get("payload"), dict) else [],
                    preferred_option,
                    summary_context.get("payload") if isinstance(summary_context.get("payload"), dict) else {},
                )
                kwargs = {"option_index": preferred_option if preferred_option is not None else 0}
                return PlannedOperation(action_type=action["type"], kwargs=kwargs, confidence=0.8, source="heuristic", reason="event_preference_or_default")

        if state_name == "map":
            action = self._find_action(available_actions, "choose_map_node")
            if action is not None:
                decision = self._map_decision(summary_context, preferences, snapshot)
                if decision is None:
                    # wait 策略下的多人局：这一 tick 什么都不做，把定路线的权力完全留给玩家。
                    # 自动玩循环会在下一 tick 重来，直到玩家通过 sts2_set_route 下令。
                    self._debug("[sts2_map_plan] policy=%s waiting for the player's instruction", self._map_policy)
                    return None
                node_index, reason = decision
                self._debug(
                    "[sts2_map_plan] policy=%s nodes=%s chosen=%s reason=%s payload=%s",
                    self._map_policy,
                    summary_context.get("payload", {}).get("travelable_nodes") if isinstance(summary_context.get("payload"), dict) else [],
                    node_index,
                    reason,
                    summary_context.get("payload") if isinstance(summary_context.get("payload"), dict) else {},
                )
                return PlannedOperation(
                    action_type=action["type"],
                    kwargs={"option_index": node_index},
                    confidence=0.95 if reason == "map_follow_player_vote" else 0.82,
                    source="heuristic",
                    reason=reason,
                )

        if state_name in {"reward", "card_selection_reward", "card_selection"}:
            payload = summary_context.get("payload") if isinstance(summary_context.get("payload"), dict) else {}
            reward = payload.get("reward") if isinstance(payload.get("reward"), dict) else {}
            reward_list = reward.get("rewards") if isinstance(reward.get("rewards"), list) else []
            card_options = reward.get("card_options") if isinstance(reward.get("card_options"), list) else []
            # 两段是**分开**的，动作清单天然区分：
            #   领奖励段：claim_reward 在册（另有 resolve_rewards / collect_rewards_and_proceed）
            #   选卡段  ：领出一张卡奖励后 claim_reward 消失，换成 choose_reward_card / skip_reward_cards
            card_action = self._find_action(available_actions, "choose_reward_card")
            reward_candidates = self._reward_candidates(summary_context)
            if card_action is not None and reward_candidates:
                preferred_option = self._preferred_reward_option(summary_context, preferences)
                kwargs = self._reward_kwargs(summary_context, card_action, preferred_option)
                return PlannedOperation(action_type=card_action["type"], kwargs=kwargs, confidence=0.72, source="heuristic", reason="reward_pick_card")
            claim_action = self._find_action(available_actions, "claim_reward")
            if claim_action is not None:
                # 逐个领；**药水排最后**，且"已经真的发过的下标"不再重发。
                # 药水栏满时领药水发不出去，而 mod 照样回 completed -> 奖励清单不变 ->
                # 旧代码每 tick 重发同一个 claim_reward(0)（正好是药水），无限重领（实测卡 5 分钟以上）。
                claim_index = self._next_reward_claim_index(summary_context)
                if claim_index is None and not self._reward_claimable_list(summary_context):
                    # payload 里没有 mod 的 rewards[] 形状（旧口径 / 别的来源）：退回原来的偏好下标，
                    # 不要因为"读不到新形状"就把动作整个吞掉。
                    preferred_option = self._preferred_reward_option(summary_context, preferences)
                    claim_index = preferred_option if preferred_option is not None else 0
                if claim_index is not None:
                    return PlannedOperation(
                        action_type=claim_action["type"],
                        kwargs={"option_index": claim_index},
                        confidence=0.72,
                        source="heuristic",
                        reason="reward_claim",
                    )
            # 奖励领完 / 都领不动了 -> 推进到下一屏。
            proceed = (self._find_action(available_actions, "collect_rewards_and_proceed")
                       or self._find_action(available_actions, "resolve_rewards")
                       or self._find_action(available_actions, "skip_reward_cards")
                       or self._find_action(available_actions, "proceed"))
            if proceed is not None:
                return PlannedOperation(action_type=proceed["type"], kwargs={}, confidence=0.85, source="heuristic", reason="reward_proceed_to_map")

        # plain "card_selection" 覆盖战斗内选牌（如「坚毅」烧牌 / combat_hand_select）与菜单卡牌选择；
        # reward 类走上面的 reward 分支（那里找 choose_reward_card/claim_reward，这里没有才落到 select_deck_card）。
        if state_name in {"card_selection", "card_selection_unusefull", "card_selection_delet", "card_selection_transform", "card_selection_remove"}:
            # 1) 能确认就先确认。老代码把 select_deck_card 排在 confirm_selection 前面，于是永远不确认；
            #    需要选 2 张的界面（min_select=2）在没选够时 confirm 不可用，就会一直重按同一张卡而卡死。
            confirm_action = self._find_action(available_actions, "confirm_selection")
            if confirm_action is not None and self._selection_can_confirm(summary_context):
                return PlannedOperation(action_type=confirm_action["type"], kwargs={}, confidence=0.95, source="heuristic", reason="selection_confirm")
            action = self._find_action(available_actions, "select_deck_card")
            if action is not None:
                preferred_option = self._next_selection_option(summary_context, preferences, context)
                self._debug(
                    "[sts2_remove_plan] cards=%s chosen=%s payload=%s",
                    summary_context.get("payload", {}).get("selection_cards") if isinstance(summary_context.get("payload"), dict) else [],
                    preferred_option,
                    summary_context.get("payload") if isinstance(summary_context.get("payload"), dict) else {},
                )
                # 这一屏的候选都按过了就不再发 select_deck_card（发一个没有 option_index 的选牌动作只会
                # 让动作引擎去猜/报错），落到下面的确认或离开分支。
                if preferred_option is not None:
                    return PlannedOperation(action_type=action["type"], kwargs={"option_index": preferred_option}, confidence=0.7, source="heuristic", reason="remove_preference_or_default")
            # 选牌屏没有可用的选牌动作时，先尝试确认，其次离开（关闭牌视图 / 继续）。
            if confirm_action is not None:
                return PlannedOperation(action_type=confirm_action["type"], kwargs={}, confidence=0.9, source="heuristic", reason="selection_confirm")
            for fallback in ("close_cards_view", "proceed"):
                fallback_action = self._find_action(available_actions, fallback)
                if fallback_action is not None:
                    return PlannedOperation(action_type=fallback_action["type"], kwargs={}, confidence=0.8, source="heuristic", reason=f"selection_{fallback}")

        # 商店：**统一评分池**（规格 docs/shop_scoring.md）。卡 / 遗物 / 药水 / 删牌进同一个池子比总分，
        # 取最高分；全部低于门槛就一分不花，把钱留到下一个商店。
        #
        # 玩家口径（2026-10-07）：「商店好像很喜欢买遗物？这里最好评估一下那些是真正需要的。
        # 如果没有需要的就留到下一个商店」「买评分最高的，都不高就不买」。老实现是**死板的类别优先**
        # （遗物 → 高价值卡 → 药水 → 删牌 → 其他卡），遗物只要买得起就买、reason 却写着
        # `shop_high_value_relic` —— 这就是"商店很喜欢买遗物"的根因，也把买药冲精英的钱先花掉了。
        if state_name in {"shop", "shop_show"}:
            # 一次进店只开一次货架。open_shop_inventory 是**开关**：开 -> 没值得买的就关 ->
            # 下一 tick 又看见 open_shop_inventory 就又开，于是 open/close 无限来回（玩家报的"商店卡住"）。
            # "已经看过货"用 close_shop_inventory 是否在册判定——它只在货架**开着**时出现。
            visit_key = self._shop_visit_signature(summary_context)
            if visit_key != self._shop_visit_key:
                self._shop_visit_key = visit_key
                self._shop_viewed = False
            if self._find_action(available_actions, "close_shop_inventory") is not None:
                self._shop_viewed = True
            if not self._shop_viewed:
                open_action = self._find_action(available_actions, "open_shop_inventory")
                if open_action is not None:
                    return PlannedOperation(action_type="open_shop_inventory", kwargs={}, confidence=0.72, source="heuristic", reason="shop_open_inventory")

            decision = self._shop_preferred_purchase(summary_context, preferences, snapshot, available_actions)
            if decision is not None and decision.get("action_type"):
                return PlannedOperation(
                    action_type=str(decision["action_type"]),
                    kwargs=dict(decision.get("kwargs") or {}),
                    confidence=float(decision.get("confidence") or 0.7),
                    source="heuristic",
                    reason="shop_scored_buy",
                )

            # 一分不花：日志里已经写了"最高分 vs 门槛"，这里把 reason 也带上，方便事后查。
            reason = str((decision or {}).get("reason") or "shop_close")
            if self._find_action(available_actions, "close_shop_inventory"):
                return PlannedOperation(action_type="close_shop_inventory", kwargs={}, confidence=0.8, source="heuristic", reason=reason)
            if self._find_action(available_actions, "proceed"):
                return PlannedOperation(
                    action_type="proceed",
                    kwargs={},
                    confidence=0.8,
                    source="heuristic",
                    reason=reason if reason == "shop_score_below_threshold" else "shop_done",
                )
            return None

        if state_name == "rest":
            action = self._find_action(available_actions, "choose_rest_option")
            if action is not None:
                preferred_option = self._preferred_rest_option(summary_context, preferences)
                if preferred_option is not None:
                    self._log_rest_choice(summary_context, preferred_option)
                    return PlannedOperation(
                        action_type=action["type"],
                        kwargs={"option_index": preferred_option},
                        confidence=0.78,
                        source="heuristic",
                        reason="rest_option_preference",
                    )
                kwargs = {"option_index": 0}
                return PlannedOperation(action_type=action["type"], kwargs=kwargs, confidence=0.74, source="heuristic", reason="rest_preference_or_default")
            # 休息点无可挑选项（rest.options 空 / 已休息）-> 直接 proceed 离开，继续到地图。
            action = self._find_action(available_actions, "proceed")
            if action is not None:
                return PlannedOperation(action_type="proceed", kwargs={}, confidence=0.8, source="heuristic", reason="rest_proceed")

        if state_name == "chest":
            # 宝箱是**两步**：mod 的流程是 open_chest -> choose_treasure_relic -> proceed。
            # 开箱**前**只有 open_chest 在册（choose_treasure_relic / proceed 都不在册，
            # chest.relic_options 还是空的），而旧分支只找后两个 —— 规划不出任何动作，
            # 猫娘永久停在宝箱屏（实测 floor 10 卡了 6 分钟以上，step_count 一直是 0）。
            # 顺序：能领遗物就先领（避免"已开箱但没领"再按开箱键），其次开箱，最后离开。
            relic_action = self._find_action(available_actions, "choose_treasure_relic")
            if relic_action is not None:
                return PlannedOperation(action_type="choose_treasure_relic", kwargs={"option_index": 0}, confidence=0.85, source="heuristic", reason="chest_claim_relic")
            open_action = self._find_action(available_actions, "open_chest")
            if open_action is not None:
                return PlannedOperation(action_type=open_action["type"], kwargs={}, confidence=0.85, source="heuristic", reason="chest_open")
            action = self._find_action(available_actions, "proceed")
            if action is not None:
                return PlannedOperation(action_type=action["type"], kwargs={}, confidence=0.8, source="heuristic", reason="chest_default")

        if state_name == "bundle_selection":
            # 先确认再重选：choose_bundle 选中后仍然留在 available_actions 里，老顺序会不停重选 bundle 0
            # （每次阻塞到界面关闭为止），confirm_bundle 永远够不到。
            confirm_action = self._find_action(available_actions, "confirm_bundle")
            choose_action = self._find_action(available_actions, "choose_bundle")
            signature = self._available_actions_signature(available_actions)
            if signature != self._bundle_signature:
                self._bundle_signature = signature
                self._bundle_chosen = False
            if self._bundle_chosen and confirm_action is not None:
                return PlannedOperation(action_type=confirm_action["type"], kwargs={}, confidence=0.95, source="heuristic", reason="bundle_confirm")
            if choose_action is not None:
                self._bundle_chosen = True
                return PlannedOperation(action_type=choose_action["type"], kwargs={"option_index": 0}, confidence=0.8, source="heuristic", reason="bundle_choose")
            if confirm_action is not None:
                return PlannedOperation(action_type=confirm_action["type"], kwargs={}, confidence=0.9, source="heuristic", reason="bundle_confirm")
            action = self._find_action(available_actions, "proceed")
            if action is not None:
                return PlannedOperation(action_type=action["type"], kwargs={}, confidence=0.75, source="heuristic", reason="bundle_proceed")

        if state_name == "capstone_selection":
            # 逐项往前扫，绝不重复按同一个按钮：index 0 可能是返回/关闭，一直按它永远离不开这个栈。
            action = self._find_action(available_actions, "choose_capstone_option")
            if action is not None:
                option = self._next_capstone_option(available_actions)
                if option is not None:
                    return PlannedOperation(action_type=action["type"], kwargs={"option_index": option}, confidence=0.7, source="heuristic", reason="capstone_option")
            for fallback in ("proceed", "dismiss_modal", "close_main_menu_submenu"):
                fallback_action = self._find_action(available_actions, fallback)
                if fallback_action is not None:
                    return PlannedOperation(action_type=fallback_action["type"], kwargs={}, confidence=0.8, source="heuristic", reason=f"capstone_{fallback}")

        if state_name == "cards_view":
            action = self._find_action(available_actions, "close_cards_view") or self._find_action(available_actions, "proceed")
            if action is not None:
                return PlannedOperation(action_type=action["type"], kwargs={}, confidence=0.85, source="heuristic", reason="cards_view_close")

        if state_name == "modal":
            # 先 dismiss 再 confirm：confirm 可能落在"放弃本局"这类危险按钮上。
            action = self._find_action(available_actions, "dismiss_modal") or self._find_action(available_actions, "confirm_modal")
            if action is not None:
                return PlannedOperation(action_type=action["type"], kwargs={}, confidence=0.8, source="heuristic", reason="modal_dismiss")

        if state_name in {"game_over", "victory", "defeat"}:
            action = self._find_action(available_actions, "return_to_main_menu")
            if action is not None:
                return PlannedOperation(action_type=action["type"], kwargs={}, confidence=0.9, source="heuristic", reason="game_over_return_to_main_menu")

        if state_name == "combat":
            # 战斗推荐：优先取 mod 内 vendored CombatSolver（/solver/plan），heuristic 兜底。
            # loop_runner.tick 已在战斗时取好 /solver/plan 塞进 context["solver_plan"]。
            # 决策只用「首步」line[0].steps[0]（当前回合、带位置索引）。/solver/plan 不再有顶层 action/
            # card_index/card_id/target_index/reason——line 是唯一来源。
            solver_plan = context.get("solver_plan") if isinstance(context.get("solver_plan"), dict) else None
            if solver_plan and solver_plan.get("in_combat"):
                # 整回合缓存模式：按 solver_step_index 取当前回合第几步（循环逐个执行，不重查）。
                first_step = _line_step_at(solver_plan, context.get("solver_step_index"))
                solver_action = first_step.get("kind") if isinstance(first_step, dict) else None
                solver_card_index = first_step.get("card_index") if isinstance(first_step, dict) else None
                solver_card_id = first_step.get("card_id") if isinstance(first_step, dict) else None
                solver_target = first_step.get("target_index") if isinstance(first_step, dict) else None
                if solver_action == "play_card":
                    # 用 card_id 重新匹配实机手牌（mod 的 NekoAutoplayDriver 同样如此）：solver 给的是
                    # /solver/plan 捕获瞬间的位置卡号；捕获与执行之间手牌可能漂移，直接抽样会打在别的卡上。
                    effective_card_index = self._resolve_solver_card_index(snapshot, solver_card_id, solver_card_index)
                    if effective_card_index is not None:
                        kw = {"card_index": effective_card_index}
                        if solver_target is not None:
                            kw["target_index"] = solver_target
                        self._debug("[sts2_combat_plan] mod_solver recommended=%s hand_idx=%s", solver_card_id, effective_card_index)
                        return PlannedOperation(
                            action_type="play_card",
                            kwargs=kw,
                            confidence=0.9,
                            source="mod_solver",
                            reason="mod_combat_search",
                        )
                    # 实机手牌里没有这张卡 -> 落到启发式兜底
                if solver_action == "use_potion":
                    potion_index = self._solver_potion_option_index(snapshot, solver_card_id)
                    if potion_index is not None:
                        kw = {"option_index": potion_index}
                        if solver_target is not None:
                            kw["target_index"] = solver_target
                        self._debug("[sts2_combat_plan] mod_solver recommended potion=%s", potion_index)
                        return PlannedOperation(
                            action_type="use_potion",
                            kwargs=kw,
                            confidence=0.9,
                            source="mod_solver",
                            reason="mod_solver_potion",
                        )
                if solver_action == "end_turn":
                    return PlannedOperation(
                        action_type="end_turn",
                        kwargs={},
                        confidence=0.9,
                        source="mod_solver",
                        reason="mod_solver_end_turn",
                    )
            self._debug(
                "[sts2_combat_plan] incoming=%s block=%s energy=%s guidance=%s hand=%s",
                self._incoming_attack_total(snapshot),
                self._current_block(snapshot),
                self._safe_int(snapshot.get("raw_state", {}).get("combat", {}).get("player", {}).get("energy")) or 0,
                guidance_override,
                [
                    {
                        "index": self._safe_int(card.get("index")),
                        "name": str(card.get("name") or card.get("card_id") or "?"),
                        "damage": self._card_damage(card),
                        "block": self._card_block(card),
                        "cost": self._card_energy_cost(card),
                    }
                    for card in (snapshot.get("raw_state", {}).get("combat", {}).get("hand") if isinstance(snapshot.get("raw_state", {}).get("combat", {}).get("hand"), list) else [])
                    if isinstance(card, dict) and bool(card.get("playable"))
                ],
            )
            sequence = self._plan_combat_turn_sequence(snapshot, available_actions, preferences, guidance_override)
            if sequence:
                self._debug("[sts2_combat_plan] chosen_sequence=%s", sequence)
                first = sequence[0]
                kwargs = {"card_index": first["card_index"]}
                if first.get("target_index") is not None:
                    kwargs["target_index"] = first["target_index"]
                return PlannedOperation(
                    action_type="play_card",
                    kwargs=kwargs,
                    confidence=0.82 if first.get("reason") != "combat_lethal_sequence" else 0.98,
                    source="heuristic",
                    reason=str(first.get("reason") or "combat_sequence"),
                )
            action = self._preferred_combat_action(available_actions, snapshot, preferences, guidance_override)
            if action is not None:
                effective_override = dict(guidance_override)
                if not effective_override.get("prefer_attack") and self._incoming_attack_total(snapshot) > self._current_block(snapshot):
                    effective_override["prefer_defense"] = True
                preferred_card_index = self._preferred_combat_card_index(snapshot, preferences, effective_override)
                kwargs = self._combat_kwargs(snapshot, action, preferred_card_index)
                return PlannedOperation(action_type=action["type"], kwargs=kwargs, confidence=0.65, source="heuristic", reason="combat_preference_or_default")

        return None

    def _find_action(self, actions: list[dict[str, Any]], action_name: str) -> dict[str, Any] | None:
        target = str(action_name or "").strip().lower()
        for action in actions:
            if not isinstance(action, dict):
                continue
            raw = action.get("raw") if isinstance(action.get("raw"), dict) else {}
            candidate = str(action.get("type") or raw.get("name") or raw.get("action") or "").strip().lower()
            if candidate == target:
                return action
        return None

    def _solver_potion_option_index(self, snapshot: dict[str, Any], potion_id: str | None) -> int | None:
        """用 potion_id 回查 snapshot.raw_state.run.potions[] 的真实下标（含空槽），且要求 can_use。

        mod /solver/plan 的 use_potion 给的是 potion_id；插件的 use_potion 动作要 option_index，
        所以必须回查完整 potions 列表（不能用压缩后的占用槽下标）。
        """
        if not potion_id:
            return None
        raw = snapshot.get("raw_state") if isinstance(snapshot.get("raw_state"), dict) else {}
        run = raw.get("run") if isinstance(raw.get("run"), dict) else {}
        for p in run.get("potions") or []:
            if not isinstance(p, dict):
                continue
            if str(p.get("potion_id") or "") != potion_id:
                continue
            if not p.get("can_use"):
                return None
            if isinstance(p.get("index"), int):
                return p["index"]
        return None

    def _selected_character_index(self, summary_context: dict[str, Any]) -> int | None:
        payload = summary_context.get("payload") if isinstance(summary_context.get("payload"), dict) else {}
        selected_character_id = payload.get("selected_character_id")
        if not selected_character_id:
            return None
        raw_state = payload.get("character_select") if isinstance(payload.get("character_select"), dict) else {}
        characters = raw_state.get("characters") if isinstance(raw_state.get("characters"), list) else []
        for character in characters:
            if not isinstance(character, dict):
                continue
            if character.get("character_id") == selected_character_id:
                try:
                    return int(character.get("index"))
                except Exception:
                    return None
        return None

    def _main_menu_kwargs(self, action: dict[str, Any], summary_context: dict[str, Any]) -> dict[str, Any]:
        action_type = str(action.get("type") or "").strip().lower()
        if action_type != "choose_timeline_epoch":
            return {}
        payload = summary_context.get("payload") if isinstance(summary_context.get("payload"), dict) else {}
        timeline = payload.get("timeline") if isinstance(payload.get("timeline"), dict) else {}
        slots = timeline.get("slots") if isinstance(timeline.get("slots"), list) else []
        for slot in slots:
            if not isinstance(slot, dict):
                continue
            if not bool(slot.get("is_actionable")):
                continue
            try:
                return {"option_index": int(slot.get("index"))}
            except Exception:
                continue
        return {"option_index": 0}

    def _first_available_map_index(self, summary_context: dict[str, Any]) -> int | None:
        payload = summary_context.get("payload") if isinstance(summary_context.get("payload"), dict) else {}
        nodes = payload.get("travelable_nodes") if isinstance(payload.get("travelable_nodes"), list) else []
        for index, node in enumerate(nodes):
            if not isinstance(node, dict):
                continue
            if "index" in node:
                try:
                    return int(node.get("index"))
                except Exception:
                    continue
            return index
        return None

    def _preferred_event_option(self, summary_context: dict[str, Any], preferences: dict[str, Any]) -> int | None:
        # 1) 硬覆盖：玩家用自然语言钉死了某个选项，优先级高于任何评分。
        pinned = self._preferred_option_index(preferences)
        if pinned is not None:
            return pinned

        # 2) 纯启发式打分（事件房已无 LLM 评分融合，见 _rank_event_options）。
        ranked = self._rank_event_options(summary_context)
        return ranked[0] if ranked else None

    def _rank_event_options(self, summary_context: dict[str, Any]) -> list[int]:
        """事件选项按启发式分数**从好到坏**排序（返回选项下标）。

        与 ``_preferred_event_option`` 的评分口径完全一致，唯一差别是不取最高分而是保留整条顺序，
        供游玩模式的扰动层挑选次优/最差——`. 0` 就是原来的最优。
        """
        payload = summary_context.get("payload") if isinstance(summary_context.get("payload"), dict) else {}
        event_options = payload.get("event_options") if isinstance(payload.get("event_options"), list) else []
        current_hp = self._safe_int(payload.get("current_hp")) or 0
        max_hp = self._safe_int(payload.get("max_hp")) or 0
        gold = self._safe_int(payload.get("gold")) or 0
        hp_ratio = (current_hp / max_hp) if max_hp > 0 else 0.0
        guidance_override = self._guidance_override_from_payload(summary_context)

        hei_scores: dict[int, int] = {}
        indexed: list[tuple[int, dict[str, Any]]] = []
        for index, option in enumerate(event_options):
            if not isinstance(option, dict):
                continue
            option_index = self._safe_int(option.get("index"))
            if option_index is None:
                option_index = index
            hei_scores[option_index] = self._score_event_option(option, hp_ratio, gold)
            indexed.append((option_index, option))
        hei_norm = self._normalize_percentile(hei_scores)

        scored: list[tuple[float, int]] = []
        for option_index, option in indexed:
            final = hei_norm.get(option_index, 50.0)
            text = self._event_option_text(option)
            if guidance_override.get("prefer_defense") and self._option_loses_hp(text):
                final -= 15.0
            if guidance_override.get("prefer_attack") and any(tok in text for tok in ["relic", "遗物", "gold", "金币", "card", "卡牌"]):
                final += 10.0
            scored.append((final, option_index))
        # 与 _preferred_event_option 同口径：分数高的在前，同分时下标小的在前。
        scored.sort(key=lambda item: (-item[0], item[1]))
        return [option_index for _, option_index in scored]

    @staticmethod
    def _normalize_percentile(scores: dict[int, int]) -> dict[int, float]:
        """把一组整数分 min-max 归一化到 0-100（单值/全等分 → 50），供与 LLM 0-100 分融合。"""
        if not scores:
            return {}
        values = [float(v) for v in scores.values()]
        lo, hi = min(values), max(values)
        if hi <= lo:
            return {k: 50.0 for k in scores}
        return {k: 100.0 * (float(v) - lo) / (hi - lo) for k, v in scores.items()}

    @staticmethod
    def _option_loses_hp(text: str) -> bool:
        """事件选项是否掉血（健壮匹配，容忍 "lose 7 HP"/"Lose HP"/中文表述）。"""
        t = str(text or "").lower()
        return (
            "失去生命" in t
            or "掉血" in t
            or "lose hp" in t
            or "lose health" in t
            or re.search(r"lose\s+\d+\s*(hp|health)", t) is not None
        )

    def _preferred_option_index(self, preferences: dict[str, Any]) -> int | None:
        record = preferences.get("record") if isinstance(preferences.get("record"), dict) else None
        if isinstance(record, dict):
            value = record.get("value") if isinstance(record.get("value"), dict) else {}
            preferred = self._safe_int(value.get("preferred_option_index"))
            if preferred is not None:
                return preferred
        records = preferences.get("records") if isinstance(preferences.get("records"), list) else []
        for item in records:
            if not isinstance(item, dict):
                continue
            value = item.get("value") if isinstance(item.get("value"), dict) else {}
            preferred = self._safe_int(value.get("preferred_option_index"))
            if preferred is not None:
                return preferred
        return None

    def _preferred_reward_option(self, summary_context: dict[str, Any], preferences: dict[str, Any]) -> int | None:
        reward_cards = self._reward_candidates(summary_context)
        deck = summary_context.get("payload", {}).get("deck") if isinstance(summary_context.get("payload"), dict) else {}
        archetype_tags = [str(tag).strip().lower() for tag in self._infer_archetype_tags(deck)] if isinstance(deck, dict) else []
        records = preferences.get("records") if isinstance(preferences.get("records"), list) else []
        deck_policy = self._reward_deck_policy(records)
        guidance_override = self._guidance_override_from_payload(summary_context)
        best_candidate: tuple[int, int] | None = None
        for index, card in enumerate(reward_cards if isinstance(reward_cards, list) else []):
            if not isinstance(card, dict):
                continue
            score = self._score_reward_card(card, deck_policy, archetype_tags)
            if guidance_override.get("prefer_defense"):
                if self._card_block(card) > 0:
                    score += 18
                if self._reward_dynamic_value(card, "HpLoss") > 0:
                    score -= 12
            if guidance_override.get("prefer_attack"):
                if self._card_damage(card) > 0:
                    score += 14
            card_index = self._safe_int(card.get("index"))
            if card_index is None:
                card_index = index
            candidate = (-score, card_index)
            if best_candidate is None or candidate < best_candidate:
                best_candidate = candidate
        if best_candidate is not None and best_candidate[0] < 0:
            return best_candidate[1]
        preferred = self._preferred_option_index(preferences)
        if preferred is not None:
            return preferred
        return self._first_record_option(preferences)

    def _reward_claim_signature(self, summary_context: dict[str, Any]) -> str:
        rewards = self._reward_claimable_list(summary_context)
        return "|".join(f"{item.get('index')}:{item.get('reward_type')}" for item in rewards)

    @staticmethod
    def _reward_claimable_list(summary_context: dict[str, Any]) -> list[dict[str, Any]]:
        payload = summary_context.get("payload") if isinstance(summary_context.get("payload"), dict) else {}
        reward = payload.get("reward") if isinstance(payload.get("reward"), dict) else {}
        rewards = reward.get("rewards") if isinstance(reward.get("rewards"), list) else []
        return [item for item in rewards if isinstance(item, dict) and bool(item.get("claimable", True))]

    def note_reward_claim_dispatched(self, option_index: Any) -> None:
        """服务层在**真的把 claim_reward 发出去**之后调用。

        "已经试过"必须由执行推动，不能由规划推动 —— 规划每 tick 都跑（事件流约 1 次/秒），
        而执行约 2.5 秒一次，用规划推进会把候选在几秒内白白耗光（选牌光标踩过这个坑）。
        """
        index = self._safe_int(option_index)
        if index is None:
            return
        self._reward_claim_tried.add(index)

    def _next_reward_claim_index(self, summary_context: dict[str, Any]) -> int | None:
        """该领哪一个奖励；都领不动就返回 None，交给调用方去推进界面。

        为什么需要这张账：**药水栏满时"领药水奖励"发不出去，而 mod 照样回 completed** ——
        奖励清单不变、`step_count` 照涨，于是插件每 tick 重发同一个 `claim_reward(0)`（药水），
        无限重领（实测 floor 13 的奖励屏卡了 5 分钟以上）。药水排到最后，领过的下标不再重发。
        """
        rewards = self._reward_claimable_list(summary_context)
        if not rewards:
            return None
        signature = self._reward_claim_signature(summary_context)
        if signature != self._reward_claim_key:
            self._reward_claim_key = signature
            self._reward_claim_tried = set()

        def rank(item: dict[str, Any]) -> tuple[int, int]:
            # 药水最容易"领不动"（栏位满），排到最后；其余按下标顺序。
            is_potion = 1 if str(item.get("reward_type") or "").strip().lower() == "potion" else 0
            index = self._safe_int(item.get("index"))
            return (is_potion, index if index is not None else 0)

        for item in sorted(rewards, key=rank):
            index = self._safe_int(item.get("index"))
            if index is None or index in self._reward_claim_tried:
                continue
            return index
        return None

    def _reward_candidates(self, summary_context: dict[str, Any]) -> list[dict[str, Any]]:
        payload = summary_context.get("payload") if isinstance(summary_context.get("payload"), dict) else {}
        reward_cards = payload.get("reward_cards") if isinstance(payload.get("reward_cards"), list) else []
        if reward_cards:
            return reward_cards
        selection_cards = payload.get("selection_cards") if isinstance(payload.get("selection_cards"), list) else []
        if selection_cards:
            return selection_cards
        reward = payload.get("reward") if isinstance(payload.get("reward"), dict) else {}
        card_options = reward.get("card_options") if isinstance(reward.get("card_options"), list) else []
        if card_options:
            return card_options
        return []

    def _preferred_remove_option(self, summary_context: dict[str, Any], preferences: dict[str, Any]) -> int | None:
        selection_cards = summary_context.get("payload", {}).get("selection_cards") if isinstance(summary_context.get("payload"), dict) else []
        best_candidate: tuple[int, int] | None = None
        for index, card in enumerate(selection_cards if isinstance(selection_cards, list) else []):
            if not isinstance(card, dict):
                continue
            if self._is_unremovable_card(card):
                continue
            score = self._score_remove_card(card)
            card_index = self._safe_int(card.get("index"))
            if card_index is None:
                card_index = index
            candidate = (score, -card_index)
            if best_candidate is None or candidate < best_candidate:
                best_candidate = candidate
        if best_candidate is not None:
            return -best_candidate[1]
        preferred = self._preferred_option_index(preferences)
        if preferred is not None:
            return preferred
        return self._first_record_option(preferences)

    def _preferred_route_option(self, preferences: dict[str, Any]) -> int | None:
        preferred = self._preferred_option_index(preferences)
        if preferred is not None:
            return preferred
        return self._first_record_option(preferences)

    # ---- 商店购买策略（规格 docs/shop_scoring.md）------------------------------------------
    #
    # `_score_reward_card` 的原始分越高越好（命中 deck 策略 +40、archetype +20、分支 +35~50，
    # 核心卡常 ≥40）。老代码里：
    #   `_SHOP_HIGH_VALUE_SCORE = 25` —— "原始分 25 以上就算高价值卡，直接买"；
    #   `_SHOP_ANY_CARD_SCORE = 1`    —— 前面都没得买时，1 分以上的卡也凑合买。
    # 这两个常量现在**并入统一池子**：它们不再是"买/不买"的开关，而是"原始分 -> 补缺分"的标尺
    # （见 `_score_shop_card`）：25 分折成补缺分约 0.42，扣掉代价惩罚后**大致落在门槛附近**，
    # 于是老口径（25 分以上就买）只对"便宜或真缺得厉害的卡"继续成立 —— 这正是玩家要的收紧。
    _SHOP_HIGH_VALUE_SCORE = 25
    _SHOP_ANY_CARD_SCORE = 1
    #: 「补缺分 = 1.0」对应的原始分。取 60 ≈ 2.4 × 老的"高价值线"（`_SHOP_HIGH_VALUE_SCORE` = 25），
    #: 让老口径的边界大致落在门槛上，于是老口径（25 分以上就买）在新口径里只对"便宜或真缺得厉害的卡"成立。
    _SHOP_CARD_FIT_SCALE = SHOP_CARD_FIT_SCALE
    #: 前方有精英时，从选路那一步起向前看几层找精英（规格：「复用它判断"前方有精英"」）。
    _SHOP_ELITE_LOOKAHEAD_ROWS = 4

    def _resolve_solver_card_index(self, snapshot: dict[str, Any] | None, card_id: str | None, fallback_index: int | None) -> int | None:
        """把 /solver/plan 推荐的卡（card_id）重新映射到实机手牌位置，避免位置索引漂移。

        与 mod 的 NekoAutoplayDriver.DecideCombatAsync 一致：优先按 card_id 精确匹配且可打出；
        匹配不到再退回 solver 给的位置索引（仅当该位置卡可打出）；都不行返回 None（交给启发式）。
        """
        hand: Any = []
        if isinstance(snapshot, dict):
            raw_state = snapshot.get("raw_state") if isinstance(snapshot.get("raw_state"), dict) else {}
            combat = raw_state.get("combat") if isinstance(raw_state.get("combat"), dict) else {}
            hand = combat.get("hand") if isinstance(combat.get("hand"), list) else []
        if card_id:
            for i, card in enumerate(hand):
                if isinstance(card, dict) and str(card.get("card_id") or "") == str(card_id) and bool(card.get("playable", True)):
                    return i
        if fallback_index is not None and 0 <= fallback_index < len(hand):
            card = hand[fallback_index]
            if isinstance(card, dict) and bool(card.get("playable", True)):
                return fallback_index
        return None

    def _shop_visit_signature(self, summary_context: dict[str, Any]) -> str:
        """这一次逛的是哪家店：换店（楼层变 / 货品集合变）就重新算"没看过"。

        只看楼层不够——同一层的两次商店（罕见但可能）会互相顶掉；带上货品身份最稳。
        """
        payload = summary_context.get("payload") if isinstance(summary_context.get("payload"), dict) else {}
        ids: list[str] = []
        for field in ("shop_cards", "shop_relics", "shop_potions"):
            items = payload.get(field)
            if not isinstance(items, list):
                continue
            for item in items:
                if not isinstance(item, dict):
                    continue
                ids.append(str(item.get("card_id") or item.get("relic_id") or item.get("potion_id") or item.get("index")))
        return f"{payload.get('floor')}|" + "|".join(ids)

    def _selection_can_confirm(self, summary_context: dict[str, Any]) -> bool:
        """选牌屏是否已经满足可确认条件。

        mod 只对战斗内手牌选择填 can_confirm（BuildSelectionPayload 取自 combat-hand 元数据），牌库/网格
        选择屏恒为 false。所以这里保守判断：can_confirm 为真 → 可确认；否则不主动确认（select_deck_card
        自己会在选够后点确认按钮），避免在没选够时白点一次。
        """
        payload = summary_context.get("payload") if isinstance(summary_context.get("payload"), dict) else {}
        return bool(payload.get("selection_can_confirm"))

    def _next_selection_option(self, summary_context: dict[str, Any], preferences: dict[str, Any], context: dict[str, Any]) -> int | None:
        """选牌屏要按的 option_index：多选时逐张推进，绝不重复按同一张。

        先按偏好（/solver/plan 的 choice_card_id -> 删牌启发式）；那张在这一屏已经按过就换下一张没按过的。
        全部按过仍无法推进时返回 None，交给调用方走确认/离开分支。已按过的集合以"选牌屏签名"为键，
        换屏或候选集合变化即重置（新的一屏从干净状态开始）。

        **只在"真的按了"之后才推进光标**（见 note_selection_option_pressed，由 service 在成功派发
        select_deck_card 后调用）。历史教训：这里曾经在**规划时**就 add 进 _selection_pressed，而
        plan() 是**每 tick 都被调用一次**的（事件流约 1 次/秒），自动玩步进却约 2.5 秒一次 ——
        于是四个候选在几秒内被"规划"白白耗光，屏幕上一个都没选中（selected_count 仍为 0），
        此后 plan() 永远返回 None，猫娘就**永久停在这一屏**不动了（玩家看到的"卡住"）。
        """
        payload = summary_context.get("payload") if isinstance(summary_context.get("payload"), dict) else {}
        cards = payload.get("selection_cards") if isinstance(payload.get("selection_cards"), list) else []
        if not cards:
            return self._preferred_choice_option(summary_context, preferences, context)

        signature = "|".join(
            str(card.get("card_id") or card.get("index")) for card in cards if isinstance(card, dict)
        )
        if signature != self._selection_cursor_key:
            self._selection_cursor_key = signature
            self._selection_pressed = set()
            self._selection_exhausted = 0

        count = len(cards)
        preferred = self._preferred_choice_option(summary_context, preferences, context)
        if preferred is not None and 0 <= preferred < count and preferred not in self._selection_pressed:
            return preferred
        for index in range(count):
            if index not in self._selection_pressed:
                return index
        # 这一屏的候选都按过了。
        self._selection_exhausted += 1
        if self._selection_exhausted >= self._SELECTION_EXHAUSTED_RETRIES:
            # 按遍了整屏却没换过屏（多半是一次都没真正落地）。此时若一直返回 None，这一屏就再也
            # 出不了动作 = 死锁；宁可重头再按一遍，也不让猫娘永久停手。
            self._debug(
                "[sts2_selection] 候选按遍仍未推进，重置光标重按 (key=%s pressed=%s)",
                signature,
                sorted(self._selection_pressed),
            )
            self._selection_pressed = set()
            self._selection_exhausted = 0
            return preferred if (preferred is not None and 0 <= preferred < count) else 0
        return None

    def note_selection_option_pressed(self, option_index: Any) -> None:
        """服务层在**真的把 select_deck_card 发出去**之后调用：把该下标记进"已按过"。

        规划与执行不是一一对应的（每 tick 都会规划，但只有少数 tick 会真正执行），
        所以光标必须由"执行"推动，而不是由"规划"推动。
        """
        index = self._safe_int(option_index)
        if index is None:
            return
        self._selection_pressed.add(index)

    def _preferred_choice_option(self, summary_context: dict[str, Any], preferences: dict[str, Any], context: dict[str, Any]) -> int | None:
        """选牌（消耗/拉弃牌堆/生成到手/变换等，弹的是人选牌界面）：优先用 /solver/plan 的 choice_card_id
        （solver 已优化选哪张），命中不到再走删牌启发式。避免卡死在选牌上。"""
        pending = context.get("pending_card_choice_id") if isinstance(context, dict) else None
        if pending:
            payload = summary_context.get("payload") if isinstance(summary_context.get("payload"), dict) else {}
            cards = payload.get("selection_cards") if isinstance(payload.get("selection_cards"), list) else []
            for i, card in enumerate(cards):
                if isinstance(card, dict) and str(card.get("card_id") or "") == str(pending):
                    idx = self._safe_int(card.get("index"))
                    return idx if idx is not None else i
        return self._preferred_remove_option(summary_context, preferences)

    def _shop_affordable_items(self, summary_context: dict[str, Any]) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
        payload = summary_context.get("payload") if isinstance(summary_context.get("payload"), dict) else {}

        def affordable(items: Any) -> list[dict[str, Any]]:
            out: list[dict[str, Any]] = []
            for item in items if isinstance(items, list) else []:
                if isinstance(item, dict) and item.get("is_stocked", True) and item.get("enough_gold", True):
                    out.append(item)
            return out

        return (
            affordable(payload.get("shop_cards")),
            affordable(payload.get("shop_relics")),
            affordable(payload.get("shop_potions")),
        )

    # ---- 商店：统一评分池（牌组量化 × 覆盖度 × 商品量化）----------------------------------
    #
    # 规格 docs/shop_scoring.md。三个老入口（_preferred_shop_relic_index / _preferred_shop_potion_index
    # / _shop_best_card_index）在这里**收敛成一个** `_shop_preferred_purchase()`：卡 / 遗物 / 药水 /
    # 删牌进同一个池子比总分，取最高分；全部低于门槛就一分不花。
    # `_shop_best_card_index` 仍然保留（选牌屏/扰动排序还在用它），但商店采购不再走它。

    def _shop_preferred_purchase(
        self,
        summary_context: dict[str, Any],
        preferences: dict[str, Any],
        snapshot: dict[str, Any] | None = None,
        available_actions: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any] | None:
        """商店买什么：把可买的每一项打分，买**最高分**的；都不够高就什么都不买。

        算法（规格式伪代码）：

            可买分 = 补缺分(牌组缺口 − 遗物覆盖) × 情境加权 − 代价惩罚 − 重复惩罚
            药水分 = 药水缺口 × 精英加权 − 代价惩罚（栏位满则不评）
            遗物预算 = 金币 − 药水预留 − 底钱（不够就不买遗物）

        返回 ``None`` 表示"没什么可看的"（调用方照旧关门/推进）；返回带 ``action_type`` 的 dict 表示
        买它；返回只有 ``reason`` 的 dict 表示**评估过了但一分不花**（reason 会写进 PlannedOperation，
        玩家能在 last_decision_reason 里看到是 `shop_score_below_threshold`）。
        """
        run = self._shop_run_data(summary_context, snapshot)
        gold = run["gold"]
        floor = run["floor"]
        gaps = self._deck_gap_vector(run["deck"], current_hp=run["current_hp"], max_hp=run["max_hp"], floor=floor)
        coverage = self._relic_coverage(run["relics"])
        residual = {dim: max(0.0, gaps.get(dim, 0.0) - coverage.get(dim, 0.0)) for dim in SHOP_DIMENSIONS}
        elite_ahead = self._shop_elite_ahead(summary_context, snapshot)
        policy = self._shop_policy(preferences)
        base_threshold = self._shop_threshold(gold=gold, elite_ahead=elite_ahead, gaps=gaps, residual=residual, policy=policy)

        cards, relics, potions = self._shop_affordable_items(summary_context)
        occupied, slots = self._shop_potion_slots(run["potions"], potions)
        slots_full = slots > 0 and occupied >= slots
        owned_potion_categories = {self._potion_category(item) for item in run["potions"] if isinstance(item, dict) and item.get("potion_id")}
        needed_categories = set(SHOP_ELITE_POTION_NEEDS) if elite_ahead else {"defense", "burst", "aoe", "heal", "cleanse"}
        missing_categories = needed_categories - owned_potion_categories
        potion_boost = float(policy.get("elite_potion_boost", SHOP_ELITE_POTION_BOOST)) if elite_ahead else 1.0

        # 买遗物之前先扣掉"留给药水的钱"和"下一个商店的底钱"（规格「三、4」）。
        gold_floor = int(policy.get("gold_floor", SHOP_GOLD_FLOOR) or 0)
        cheapest_potion = min([self._safe_int(item.get("price")) or 0 for item in potions if isinstance(item, dict)] or [SHOP_POTION_PRICE_FALLBACK])
        potion_reserve = self._shop_potion_reserve(policy, elite_ahead=elite_ahead, empty_slots=max(0, slots - occupied), potion_price=cheapest_potion)
        if not potions and "potion_reserve_gold" not in policy:
            # 这家店**没有药水可买**：预留就没有意义（钱花不出去，下一次能不能碰到药水也不知道）。
            # 不为买不到的东西压着一笔钱，否则会白白错过大遗物。
            potion_reserve = 0
        relic_budget = gold - potion_reserve - gold_floor

        allowed = None
        if isinstance(available_actions, list):
            allowed = {str(action.get("type") or "").strip().lower() for action in available_actions if isinstance(action, dict)}
        records = preferences.get("records") if isinstance(preferences.get("records"), list) else []
        deck_policy = self._reward_deck_policy(records)
        archetype_tags = [str(tag).strip().lower() for tag in self._infer_archetype_tags(run["deck"])]

        entries: list[dict[str, Any]] = []
        skipped: list[str] = []

        def action_ok(action_type: str) -> bool:
            return allowed is None or action_type in allowed

        for relic in relics:
            index = self._safe_int(relic.get("index"))
            if index is None:
                continue
            if not action_ok("buy_relic"):
                continue
            if self._shop_relic_is_duplicate(relic, run["relics"]):
                # 重复惩罚：同名 / 同 relic_id **直接排除**（规格「二、通用部分」）。
                skipped.append(f"遗物「{self._shop_item_name(relic)}」→ 不买(原因=与已有遗物重复)")
                continue
            price = self._safe_int(relic.get("price")) or 0
            score, detail = self._score_shop_relic(relic, residual, gold=gold)
            entry = {
                "kind": "relic",
                "action_type": "buy_relic",
                "option_index": index,
                "name": self._shop_item_name(relic),
                "price": price,
                "score": score,
                "detail": detail,
                "blocked": "",
            }
            if price > relic_budget:
                # 规格「三、4」：遗物预算 = 金币 − 药水预留 − 底钱。不够就不买遗物。
                entry["blocked"] = (
                    f"留给药水 {potion_reserve} + 下一个商店的底钱 {gold_floor}：遗物预算={relic_budget} < 价格 {price}"
                )
            entries.append(entry)

        for card in cards:
            index = self._safe_int(card.get("index"))
            if index is None or not action_ok("buy_card"):
                continue
            price = self._safe_int(card.get("price")) or 0
            score, detail = self._score_shop_card(card, residual, gold=gold, deck_policy=deck_policy, archetype_tags=archetype_tags, deck=run["deck"])
            entries.append(
                {
                    "kind": "card",
                    "action_type": "buy_card",
                    "option_index": index,
                    "name": self._shop_item_name(card),
                    "price": price,
                    "score": score,
                    "detail": detail,
                    "blocked": "",
                }
            )

        if not slots_full:
            for potion in potions:
                index = self._safe_int(potion.get("index"))
                if index is None or not action_ok("buy_potion"):
                    continue
                price = self._safe_int(potion.get("price")) or 0
                score, detail = self._score_shop_potion(
                    potion,
                    missing_categories=missing_categories,
                    owned_categories=owned_potion_categories,
                    boost=potion_boost,
                    gold=gold,
                )
                entries.append(
                    {
                        "kind": "potion",
                        "action_type": "buy_potion",
                        "option_index": index,
                        "name": self._shop_item_name(potion),
                        "price": price,
                        "score": score,
                        "detail": detail,
                        "blocked": "",
                    }
                )
        else:
            skipped.append(f"药水栏位已满（{occupied}/{slots}）→ 整类不评")

        removal = self._shop_card_removal(summary_context)
        if removal is not None and action_ok("remove_card_at_shop"):
            price = self._safe_int(removal.get("price")) or 0
            score, detail = self._score_shop_removal(residual, gold=gold)
            entries.append(
                {
                    "kind": "remove",
                    "action_type": "remove_card_at_shop",
                    "option_index": None,
                    "name": "删牌服务",
                    "price": price,
                    "score": score,
                    "detail": detail,
                    "blocked": "",
                }
            )

        best = None
        # 代价惩罚统一在这里扣（规格：`分数 = 补缺分 × 情境加权 − 代价惩罚`）：每一件都按
        # `价格 / 当前金币` 扣，花掉一半以上的钱再重罚一次。放在一处算，免得四种商品各写一遍还写歪。
        for entry in entries:
            entry["penalty"] = self._shop_cost_penalty(int(entry.get("price") or 0), gold)
            entry["score"] = entry["score"] - entry["penalty"]
        for entry in entries:
            if entry["blocked"]:
                continue
            if best is None or entry["score"] > best["score"]:
                best = entry
        best_blocked = None
        for entry in entries:
            if not entry["blocked"]:
                continue
            if best_blocked is None or entry["score"] > best_blocked["score"]:
                best_blocked = entry

        threshold = self._entry_threshold(base_threshold, entry=best, elite_ahead=elite_ahead, policy=policy)
        decision: dict[str, Any] = {"action_type": None, "score": None, "threshold": threshold, "reason": "shop_close"}
        if best is None:
            if best_blocked is not None:
                decision.update(reason="shop_score_below_threshold", blocked=best_blocked["blocked"], score=best_blocked["score"])
            reason = str(decision["reason"])
        elif best["score"] >= threshold:
            decision.update(
                action_type=str(best["action_type"]),
                kwargs={"option_index": best["option_index"]} if best["option_index"] is not None else {},
                score=best["score"],
                kind=best["kind"],
                name=best["name"],
                confidence={"relic": 0.72, "card": 0.7, "potion": 0.66, "remove": 0.7}.get(str(best["kind"]), 0.7),
                reason="shop_scored_buy",
            )
            reason = "shop_scored_buy"
        else:
            decision.update(reason="shop_score_below_threshold", score=best["score"])
            reason = "shop_score_below_threshold"

        self._log_shop_decision(
            summary_context,
            entries=entries,
            skipped=skipped,
            best=best,
            best_blocked=best_blocked,
            threshold=threshold,
            base_threshold=base_threshold,
            policy=policy,
            gold=gold,
            elite_ahead=elite_ahead,
            relic_budget=relic_budget,
            potion_reserve=potion_reserve,
            missing_categories=missing_categories,
            reason=reason,
        )
        return decision

    def _shop_run_data(self, summary_context: dict[str, Any], snapshot: dict[str, Any] | None) -> dict[str, Any]:
        """商店决策要用的局面数据：金币 / 血量 / 层数 / 牌组 / 已有遗物 / 已有药水。

        为什么不能只读 `summary_context.payload`：`_build_shop_context` 里那份 `deck` 取的是
        `raw_state["deck"]`，而 mod 的 /state **没有顶层 deck** —— 牌组在 `run.deck[]` 里、已有遗物在
        `run.relics[]`（只有它带 description）、药水栏位在 `run.potions[]`（只有它带 occupied）。
        所以这里一律以 `raw_state.run.*` 为权威来源；payload 里那几项（gold/hp/floor）优先，因为它们
        已经在别的分支里用过、口径一致。
        """
        payload = summary_context.get("payload") if isinstance(summary_context.get("payload"), dict) else {}
        raw_state = snapshot.get("raw_state") if isinstance(snapshot, dict) and isinstance(snapshot.get("raw_state"), dict) else {}
        run = raw_state.get("run") if isinstance(raw_state.get("run"), dict) else {}

        def first_int(*values: Any) -> int:
            for value in values:
                parsed = self._safe_int(value)
                if parsed is not None:
                    return parsed
            return 0

        deck = payload.get("deck") if isinstance(payload.get("deck"), dict) else {}
        if not (deck.get("cards") if isinstance(deck.get("cards"), list) else []):
            deck = {"cards": run.get("deck")} if isinstance(run.get("deck"), list) else {}
        relics = payload.get("relics") if isinstance(payload.get("relics"), list) else []
        if not relics:
            relics = run.get("relics") if isinstance(run.get("relics"), list) else []
        potions = payload.get("potions") if isinstance(payload.get("potions"), list) else []
        if not potions:
            potions = run.get("potions") if isinstance(run.get("potions"), list) else []
        return {
            "gold": first_int(payload.get("gold"), run.get("gold")),
            "current_hp": first_int(payload.get("current_hp"), run.get("current_hp")),
            "max_hp": first_int(payload.get("max_hp"), run.get("max_hp")),
            "floor": first_int(payload.get("floor"), run.get("floor"), snapshot.get("floor") if isinstance(snapshot, dict) else None),
            "deck": deck if isinstance(deck, dict) else {},
            "relics": [item for item in relics if isinstance(item, dict)],
            "potions": [item for item in potions if isinstance(item, dict)],
        }

    @staticmethod
    def _clamp01(value: float) -> float:
        return 0.0 if value < 0.0 else (1.0 if value > 1.0 else value)

    @staticmethod
    def _shop_item_name(item: dict[str, Any]) -> str:
        for field in ("name", "card_id", "relic_id", "potion_id"):
            text = str(item.get(field) or "").strip()
            if text:
                return text
        return "?"

    @staticmethod
    def _item_text(item: dict[str, Any]) -> str:
        return " ".join(
            str(item.get(field) or "")
            for field in ("name", "card_id", "relic_id", "potion_id", "description", "rules_text", "resolved_rules_text", "effect", "usage", "card_type", "type")
        ).lower()

    # ---- 牌组量化（缺口向量）--------------------------------------------------------------

    def _deck_gap_vector(
        self,
        deck: dict[str, Any],
        *,
        current_hp: int = 0,
        max_hp: int = 0,
        floor: int = 0,
    ) -> dict[str, float]:
        """牌组量化 → 缺口向量（六维 0..1，越大越缺）。规格「一、牌组量化」那张表。

        六个维度：输出 / 格挡生存 / 抽牌循环 / 费用曲线 / 状态群伤 / 牌组质量。
        **全 0 表示"什么都不缺"**（那就别买）。量法都只用 mod 已给的 deck[] 字段
        （card_type / upgraded / energy_cost / dynamic_values / resolved_rules_text），不额外猜。

        为什么按"每回合能打出多少"而不是"牌组总伤害"：牌组越大总伤害越高，但一回合只抽 5 张、只打
        3 张 —— 用总量算会让"堆了一堆打击"的胖牌组显得很强。所以取**平均每张牌的数值 × 每回合出牌数**。
        """
        cards = [
            card
            for card in (deck.get("cards") if isinstance(deck, dict) and isinstance(deck.get("cards"), list) else [])
            if isinstance(card, dict)
        ]
        if not cards:
            # 读不到牌组就按"没缺口"处理：宁可这一家不买，也不要凭猜乱花钱（玩家的原话就是这个）。
            return {dim: 0.0 for dim in SHOP_DIMENSIONS}
        size = len(cards)
        damage_total = 0.0
        block_total = 0.0
        draw_cards = 0
        aoe_cards = 0
        status_cards = 0
        zero_cost = 0
        cost_sum = 0.0
        basic_unupgraded = 0
        curses = 0
        for card in cards:
            text = self._item_text(card)
            card_type = str(card.get("card_type") or card.get("type") or "").lower()
            damage_total += self._deck_card_damage(card)
            block_total += self._card_block(card)
            if any(token in text for token in _DECK_DRAW_TOKENS):
                draw_cards += 1
            if any(token in text for token in _DECK_AOE_TOKENS):
                aoe_cards += 1
            if any(token in text for token in _DECK_STATUS_TOKENS):
                status_cards += 1
            cost = self._card_energy_cost(card)
            cost_sum += cost
            if cost <= 0:
                zero_cost += 1
            name_text = f"{card.get('name') or ''} {card.get('card_id') or ''}".lower()
            if not bool(card.get("upgraded")) and any(token in name_text for token in _DECK_BASIC_CARD_TOKENS):
                basic_unupgraded += 1
            if any(token in card_type for token in _DECK_CURSE_TYPE_TOKENS):
                curses += 1

        per_turn_damage = (damage_total / size) * SHOP_CARDS_PER_TURN
        per_turn_block = (block_total / size) * SHOP_CARDS_PER_TURN
        target_damage = SHOP_TARGET_DAMAGE_BASE + SHOP_TARGET_DAMAGE_PER_FLOOR * max(0, floor)
        target_block = SHOP_TARGET_BLOCK_BASE + SHOP_TARGET_BLOCK_PER_FLOOR * max(0, floor)
        gap_output = self._clamp01((target_damage - per_turn_damage) / target_damage)
        gap_defense = self._clamp01((target_block - per_turn_block) / target_block)
        if max_hp > 0 and current_hp > 0 and (current_hp / max_hp) < 0.6:
            # 血量低且格挡薄 → 缺口高（规格那张表的判据）。
            gap_defense = min(1.0, gap_defense + 0.2)

        # 牌组 > 14 张才开始嫌循环不动；有抽牌手段就按张数抹平（每 2 张抽牌牌抹掉一整档）。
        gap_draw = self._clamp01((size - SHOP_DRAW_SIZE_BASE) / SHOP_DRAW_SIZE_SPAN)
        gap_draw *= 1.0 - min(1.0, draw_cards / SHOP_DRAW_CARD_CREDIT)

        avg_cost = cost_sum / size
        zero_share = zero_cost / size
        gap_energy = self._clamp01((avg_cost - 1.2) / 0.8)
        gap_energy *= 1.0 - 0.5 * min(1.0, zero_share / 0.25)

        # 状态/群伤：群伤算 1 张、单体状态算半张；这个维度封顶 0.7（规格说这种情境是"缺口中"）。
        status_units = aoe_cards + status_cards * SHOP_STATUS_CARD_CREDIT
        gap_aoe = 0.7 * self._clamp01(1.0 - status_units / SHOP_AOE_TARGET_UNITS)

        gap_quality = self._clamp01(
            0.5 * min(1.0, basic_unupgraded / SHOP_QUALITY_BASIC_TARGET)
            + 0.5 * min(1.0, curses / SHOP_QUALITY_CURSE_TARGET)
        )
        return {
            SHOP_DIM_OUTPUT: round(gap_output, 4),
            SHOP_DIM_DEFENSE: round(gap_defense, 4),
            SHOP_DIM_DRAW: round(gap_draw, 4),
            SHOP_DIM_ENERGY: round(gap_energy, 4),
            SHOP_DIM_AOE: round(gap_aoe, 4),
            SHOP_DIM_QUALITY: round(gap_quality, 4),
        }

    def _deck_card_damage(self, card: dict[str, Any]) -> float:
        """牌组里一张牌"一回合能打出多少伤害"（含多段：Damage × Repeat/hit_count）。"""
        damage = self._card_damage(card)
        if damage <= 0:
            return 0.0
        repeat = self._reward_dynamic_value(card, "Repeat")
        hits = repeat or self._safe_int(card.get("hit_count")) or 1
        return float(damage * max(1, hits))

    # ---- 已有遗物 / 已有药水的覆盖度 -------------------------------------------------------

    def _relic_effect_dims(self, relic: dict[str, Any]) -> set[str]:
        """遗物 → 它覆盖哪几维缺口（规格「一之二」的类别表：输出/防御/能量/抽牌/生存/经济/条件）。

        生存/回复归到「格挡/生存」这一维（牌组量化里它就叫这个名字）；经济类**没有**对应的缺口维度，
        所以不进这个集合（钱够时"加金币"不补任何缺口 —— 这也是玩家说的"评估一下那些是真正需要的"）。
        """
        text = self._item_text(relic)
        dims: set[str] = set()
        for dim, tokens in _RELIC_EFFECT_DIM_TOKENS.items():
            if any(token in text for token in tokens):
                dims.add(dim)
        if any(token in text for token in _RELIC_SURVIVAL_TOKENS):
            dims.add(SHOP_DIM_DEFENSE)
        return dims

    def _relic_coverage(self, relics: list[dict[str, Any]]) -> dict[str, float]:
        """已有遗物 → 覆盖度向量：同类累加但**边际递减** `覆盖度 = 1 − 0.6^n`（规格「一之二」）。

        为什么要边际递减：手里已经有"力量+2"时，再买一件加力量的遗物几乎没有边际收益 ——
        `1-0.6^1 = 0.4`（第 1 件几乎补满 40% 缺口）、`1-0.6^2 = 0.64`（第 2 件只补 24%）、
        第 3 件起基本无价值（0.784）。一件遗物横跨多类别就按类别**分摊**（半个计入每一类）。
        """
        counts: dict[str, float] = {dim: 0.0 for dim in SHOP_DIMENSIONS}
        for relic in relics:
            if not isinstance(relic, dict):
                continue
            dims = self._relic_effect_dims(relic)
            if not dims:
                continue
            share = 1.0 / len(dims)
            for dim in dims:
                counts[dim] += share
        return {dim: (0.0 if n <= 0 else round(1.0 - 0.6 ** n, 4)) for dim, n in counts.items()}

    def _shop_relic_is_duplicate(self, relic: dict[str, Any], owned: list[dict[str, Any]]) -> bool:
        """这把遗物是不是已经有一件了（同 relic_id 或同名）—— 重复惩罚是"直接排除"，不是降分。"""
        relic_id = str(relic.get("relic_id") or "").strip().lower()
        name = str(relic.get("name") or "").strip().lower()
        for item in owned:
            if not isinstance(item, dict):
                continue
            if relic_id and relic_id == str(item.get("relic_id") or "").strip().lower():
                return True
            if name and name == str(item.get("name") or "").strip().lower():
                return True
        return False

    def _potion_category(self, item: dict[str, Any]) -> str:
        """药水 → 用途类别（单体爆发 / 群伤 / 减伤 / 回血 / 清状态 / 抽牌 / 其它）。

        判定顺序是刻意的：`EXPLOSIVE_POTION`（对所有敌人造成伤害）必须落到"群伤"，不能被"伤害"先判成
        "单体爆发"；`BLOCK_POTION` 先于"回血"（它只给格挡）。
        """
        text = self._item_text(item)
        for tokens, category in (
            (_POTION_AOE_TOKENS, "aoe"),
            (_POTION_DEFENSE_TOKENS, "defense"),
            (_POTION_HEAL_TOKENS, "heal"),
            (_POTION_CLEANSE_TOKENS, "cleanse"),
            (_POTION_BURST_TOKENS, "burst"),
            (_POTION_DRAW_TOKENS, "draw"),
        ):
            if any(token in text for token in tokens):
                return category
        return "other"

    def _shop_potion_slots(self, run_potions: list[dict[str, Any]], shop_potions: list[dict[str, Any]]) -> tuple[int, int]:
        """(已占用, 总栏位)。

        规格里写的是 `shop.potions[].occupied`，但 mod 的 ShopPotionPayload **没有这个字段**
        （只有 index/potion_id/name/rarity/usage/price/is_stocked/enough_gold —— 而且栏位满时它直接把
        `enough_gold` 算成 false）。所以以 `run.potions[]`（每格都带 occupied/potion_id）为准，
        shop 那份只在 run 读不到时兜底；两边都读不到就返回 (0, 0) = "未知"，此时**不**因此拦下药水。
        """
        slots = [item for item in run_potions if isinstance(item, dict)]
        if slots:
            occupied = sum(1 for item in slots if item.get("occupied", bool(item.get("potion_id"))))
            return occupied, len(slots)
        shop_slots = [item for item in shop_potions if isinstance(item, dict) and "occupied" in item]
        if shop_slots:
            return sum(1 for item in shop_slots if item.get("occupied")), len(shop_slots)
        return 0, 0

    # ---- 情境与门槛 -----------------------------------------------------------------------

    def _shop_policy(self, preferences: dict[str, Any]) -> dict[str, Any]:
        """玩家自己配过的商店策略（`instruction_type == "shop_policy"`）。

        与 `route_policy` / `deck_policy` 一个路子：没配过就用模块常量的保守默认。
        可配键就是规格「五、配置项」那几个：score_threshold / elite_potion_boost /
        potion_reserve_gold / gold_floor（外加战斗用药水的 potion_hp_loss_threshold）。
        """
        records = preferences.get("records") if isinstance(preferences.get("records"), list) else []
        for record in records:
            if not isinstance(record, dict):
                continue
            value = record.get("value") if isinstance(record.get("value"), dict) else {}
            if str(value.get("instruction_type") or "") != "shop_policy":
                continue
            return value
        return {}

    def _shop_elite_ahead(self, summary_context: dict[str, Any], snapshot: dict[str, Any] | None = None) -> bool:
        """前方有没有精英（决定药水加权、门槛、遗物预算）。

        玩家口径：「买两瓶药可以冲精英」。麻烦在于**商店界面拿不到地图**：mod 的 `BuildMapPayload`
        一离开地图屏就返回 null，所以进店时 `raw_state.map` 是空的。于是这里按三级取：
        1. payload 里显式给了 `elite_ahead` / `route_elite_ahead`（策略/测试注入口）；
        2. 快照里恰有地图（选路那一刻、或以后 mod 改了行为）→ 现算；
        3. 否则用**选路时记下的结论**（`_note_route_choice`，由 evaluate_map_route 那一套算出来的）。
        """
        payload = summary_context.get("payload") if isinstance(summary_context.get("payload"), dict) else {}
        for field in ("elite_ahead", "route_elite_ahead"):
            flag = payload.get(field)
            if isinstance(flag, bool):
                return flag
        graph = self._map_graph_from_snapshot(snapshot)
        if graph:
            raw_state = snapshot.get("raw_state") if isinstance(snapshot, dict) and isinstance(snapshot.get("raw_state"), dict) else {}
            game_map = raw_state.get("map") if isinstance(raw_state.get("map"), dict) else {}
            current = game_map.get("current_node") if isinstance(game_map.get("current_node"), dict) else None
            if current is not None:
                return self._route_elite_lookahead(graph, current)
        return bool(self._route_elite_ahead)

    def _route_elite_lookahead(self, graph: dict[str, Any], start: dict[str, Any]) -> bool:
        """从 `start` 这个节点往下看 `_SHOP_ELITE_LOOKAHEAD_ROWS` 层，路上有没有精英/Boss。

        复用 `_map_graph_index` / `_map_child_keys`（`evaluate_map_route` 那一套用的同一份图），
        所以坐标语义与链式递推完全一致：children 是 `{row,col}`，row 越大越靠近 boss。
        """
        index = self._map_graph_index(graph)
        if not index:
            return False
        row = self._safe_int(start.get("row"))
        col = self._safe_int(start.get("col"))
        if row is None or col is None:
            return False
        frontier: list[tuple[int, int]] = [(row, col)]
        seen: set[tuple[int, int]] = set()
        for _ in range(max(1, self._SHOP_ELITE_LOOKAHEAD_ROWS)):
            nxt: list[tuple[int, int]] = []
            for key in frontier:
                if key in seen:
                    continue
                seen.add(key)
                node = index.get(key)
                if node is None:
                    continue
                if self._map_node_weight_key(node) in {"elite", "boss"}:
                    return True
                nxt.extend(self._map_graph_child_keys_checked(index, node))
            frontier = nxt
            if not frontier:
                break
        return False

    def _map_graph_child_keys_checked(self, index: dict[tuple[int, int], dict[str, Any]], node: dict[str, Any]) -> list[tuple[int, int]]:
        return [key for key in self._map_child_keys(node) if key in index]

    def _note_route_choice(self, node: dict[str, Any] | None, graph: dict[str, Any] | None = None) -> None:
        """记下"我选的这条路前面有没有精英"。

        商店界面拿不到地图（见 `_shop_elite_ahead`），所以结论必须在**选路那一刻**算好存起来。
        同一个值还供战斗用药水判断"这是不是精英/Boss 战"（§八节流）。
        """
        node_type = self._map_node_weight_key(node) if isinstance(node, dict) else ""
        self._last_route_node_type = node_type
        if node_type in {"elite", "boss"}:
            self._route_elite_ahead = True
            return
        self._route_elite_ahead = self._route_elite_lookahead(graph, node) if (graph and isinstance(node, dict)) else False

    def _shop_threshold(
        self,
        *,
        gold: int,
        elite_ahead: bool,
        gaps: dict[str, float],
        residual: dict[str, float],
        policy: dict[str, Any],
    ) -> float:
        """统一门槛（随情境浮动，规格「三、3」）。

        - 前方有精英 → 药水门槛下调（只在药水那一项上减，见 `_entry_threshold`）；
        - 金币很多（>300）→ 门槛略降，避免"攒着不花"；
        - 金币很少（<100）→ 门槛升高，优先留钱；
        - **遗物覆盖度整体很高（构筑已成型）→ 门槛上调**，更倾向留钱到下一个商店。
        """
        try:
            threshold = float(policy.get("score_threshold", SHOP_SCORE_THRESHOLD))
        except (TypeError, ValueError):
            threshold = SHOP_SCORE_THRESHOLD
        if gold > SHOP_GOLD_RICH:
            threshold += SHOP_THRESHOLD_RICH_DELTA
        elif gold < SHOP_GOLD_POOR:
            threshold += SHOP_THRESHOLD_POOR_DELTA
        total_gap = sum(gaps.values())
        total_residual = sum(residual.values())
        if total_gap > 0 and (total_gap - total_residual) / total_gap >= SHOP_COVERAGE_MATURE_RATIO:
            threshold += SHOP_MATURE_THRESHOLD_DELTA
        return max(SHOP_THRESHOLD_MIN, min(SHOP_THRESHOLD_MAX, threshold))

    def _entry_threshold(self, base_threshold: float, *, entry: dict[str, Any] | None, elite_ahead: bool, policy: dict[str, Any]) -> float:
        """单项门槛：只有药水会在"前方有精英"时下调（规格「三、3」第一句）。"""
        if not elite_ahead or entry is None or entry.get("kind") != "potion":
            return base_threshold
        try:
            delta = float(policy.get("elite_threshold_delta", SHOP_ELITE_THRESHOLD_DELTA))
        except (TypeError, ValueError):
            delta = SHOP_ELITE_THRESHOLD_DELTA
        return max(SHOP_THRESHOLD_MIN, base_threshold + delta)

    def _shop_potion_reserve(self, policy: dict[str, Any], *, elite_ahead: bool, empty_slots: int, potion_price: int) -> int:
        """买遗物前要给"药水"预留多少钱（规格「五」：默认 冲精英时=补满栏位的钱）。"""
        configured = policy.get("potion_reserve_gold")
        parsed = self._safe_int(configured)
        if parsed is not None:
            return max(0, parsed)
        if not elite_ahead:
            return 0
        return max(0, empty_slots) * max(0, potion_price)

    @staticmethod
    def _shop_cost_penalty(price: int, gold: int) -> float:
        """代价惩罚：`价格 / 当前金币`（花掉一半以上的钱要重罚）。"""
        if gold <= 0:
            return SHOP_COST_PENALTY_SCALE * 2 + SHOP_COST_PENALTY_HALF_BONUS
        ratio = max(0.0, float(price) / float(gold))
        penalty = ratio * SHOP_COST_PENALTY_SCALE
        if ratio >= 0.5:
            penalty += SHOP_COST_PENALTY_HALF_BONUS
        return penalty

    # ---- 单件商品的补缺分 -----------------------------------------------------------------

    def _score_shop_relic(self, relic: dict[str, Any], residual: dict[str, float], *, gold: int) -> tuple[float, str]:
        """遗物：与缺口的匹配度 + 稀有度 + 关键词 − 条件苛刻降权（规格「二、分类细则」）。

        读不出效果类别时按"最缺那一维 × 0.5"折算：mod 的商店遗物 payload 只有
        `index/relic_id/name/rarity/price/is_stocked/enough_gold`（**没有 description**，只有已有遗物
        `run.relics[]` 才带 description），所以中文名字/英文 id 认不出来是常态。这个先验让"牌组真的
        缺东西"时还能买遗物，牌组没事时（缺口全 0）一律不买 —— 正是玩家要的那条线。
        折算只看**效果维度**（输出/格挡/抽牌/费用/群伤），不算"牌组质量"——遗物基本不会帮你升级或删牌。
        """
        text = self._item_text(relic)
        dims = self._relic_effect_dims(relic)
        if dims:
            matched = max(residual.get(dim, 0.0) for dim in dims)
            detail = "补缺=" + " ".join(f"{SHOP_DIM_LABELS[dim]}{residual.get(dim, 0.0):.2f}" for dim in sorted(dims))
        else:
            effect_gaps = [residual.get(dim, 0.0) for dim in SHOP_DIMENSIONS if dim != SHOP_DIM_QUALITY]
            matched = SHOP_UNKNOWN_RELIC_GAP_RATIO * max(effect_gaps or [0.0])
            detail = f"补缺={matched:.2f}(读不出效果类别→最缺维度×{SHOP_UNKNOWN_RELIC_GAP_RATIO})"
        rarity = str(relic.get("rarity") or "").strip().lower()
        rarity_bonus = SHOP_RELIC_RARITY_BONUS.get(rarity, 6.0)
        keyword_hits = sum(1 for token in _RELIC_STRONG_TOKENS if token in text)
        keyword_bonus = min(SHOP_RELIC_KEYWORD_BONUS_CAP, SHOP_RELIC_KEYWORD_BONUS * keyword_hits)
        harsh = any(token in text for token in _RELIC_HARSH_TOKENS)
        score = matched * 100.0 + rarity_bonus + keyword_bonus - (SHOP_RELIC_CONDITION_PENALTY if harsh else 0.0)
        detail += " 稀有%s=%+.0f" % (rarity or "?", rarity_bonus)
        if keyword_bonus:
            detail += " 关键词=%+.0f" % keyword_bonus
        if harsh:
            detail += " 条件苛刻=%+.0f" % (-SHOP_RELIC_CONDITION_PENALTY)
        return score, detail

    def _score_shop_card(
        self,
        card: dict[str, Any],
        residual: dict[str, float],
        *,
        gold: int,
        deck_policy: dict[str, Any],
        archetype_tags: list[str],
        deck: dict[str, Any],
    ) -> tuple[float, str]:
        """卡牌：沿用 `_score_reward_card` 的评分，接进统一池子（规格「二、分类细则」）。

        折算方式：原始分 / `_SHOP_CARD_FIT_SCALE`(60) = 补缺分，再乘"这张卡命中的那一维有多缺"
        （`0.25 + 0.75 × 缺口`）。没命中的牌（诅咒/状态，或读不出效果）缺口算 0 → 至多 0.25 倍，
        于是**牌组没缺口就不买卡**（规格「三、5」），而老口径"25 分以上就买"在便宜/真缺时仍然成立。
        """
        raw = float(self._score_reward_card(card, deck_policy, archetype_tags))
        # `_SHOP_ANY_CARD_SCORE` 的老语义是"原始分 1 以上就凑合买"；新口径里它成了**入库下限**：
        # 连 1 分都不到的卡（读不出效果、策略也不认）补缺分为 0，于是只剩代价惩罚 → 永远低于门槛。
        if raw < self._SHOP_ANY_CARD_SCORE:
            fit = 0.0
        else:
            fit = self._clamp01(raw / self._SHOP_CARD_FIT_SCALE) if self._SHOP_CARD_FIT_SCALE > 0 else 0.0
        dims = self._card_effect_dims(card)
        gap_fit = max([residual.get(dim, 0.0) for dim in dims], default=0.0)
        score = fit * (0.25 + 0.75 * gap_fit) * 100.0
        if dims:
            dim_label = "/".join(SHOP_DIM_LABELS[dim] for dim in sorted(dims))
            gap_detail = f"最高{gap_fit:.2f}({dim_label})"
        else:
            gap_detail = "未命中维度0.00"
        detail = f"补缺={gap_detail} 原始分={raw:.0f}"
        if self._is_duplicate_heavy_card(card, deck):
            # 卡牌与牌组里已有同名过多：降分（规格「二、通用部分」的重复惩罚）。
            score *= 0.5
            detail += " 同名过多×0.5"
        return score, detail

    def _card_effect_dims(self, card: dict[str, Any]) -> set[str]:
        """这张卡补的是哪几维（攻击→输出、格挡→格挡、抽牌→抽牌、能量→费用、群伤/状态→群伤/状态）。

        诅咒/状态牌**不补**任何维度（它们只会把牌组拖坏）——所以永远不该在商店里买。
        """
        text = self._item_text(card)
        card_type = str(card.get("card_type") or card.get("type") or "").lower()
        if any(token in card_type for token in _DECK_CURSE_TYPE_TOKENS):
            return set()
        dims: set[str] = set()
        if any(token in (card_type + " " + text) for token in _DECK_ATTACK_TYPE_TOKENS) or self._deck_card_damage(card) > 0:
            dims.add(SHOP_DIM_OUTPUT)
        if self._card_block(card) > 0 or any(token in text for token in ("block", "格挡", "护盾")):
            dims.add(SHOP_DIM_DEFENSE)
        if any(token in text for token in _DECK_DRAW_TOKENS):
            dims.add(SHOP_DIM_DRAW)
        if self._reward_dynamic_value(card, "Energy") > 0 or any(token in text for token in ("能量", "energy")):
            dims.add(SHOP_DIM_ENERGY)
        if any(token in text for token in _DECK_AOE_TOKENS) or any(token in text for token in _DECK_STATUS_TOKENS):
            dims.add(SHOP_DIM_AOE)
        return dims

    def _is_duplicate_heavy_card(self, card: dict[str, Any], deck: dict[str, Any]) -> bool:
        """牌组里同名牌是否已经很多（>=3 张）：重复惩罚（规格「卡牌与牌组里已有同名过多降分」）。"""
        name = str(card.get("name") or card.get("card_id") or "").strip().lower()
        if not name:
            return False
        cards = deck.get("cards") if isinstance(deck.get("cards"), list) else []
        same = sum(
            1
            for item in cards
            if isinstance(item, dict) and str(item.get("name") or item.get("card_id") or "").strip().lower() == name
        )
        return same >= 3

    def _score_shop_potion(
        self,
        potion: dict[str, Any],
        *,
        missing_categories: set[str],
        owned_categories: set[str],
        boost: float,
        gold: int,
    ) -> tuple[float, str]:
        """药水：按"还缺哪几类"评分（规格「一之二」「二」）。

        - 命中"冲精英还缺的那一类" → 高分；**已经有的那一类** → 低分（栏位有限，重复买同一类不值）；
        - 前方有精英时整体加权上调（这就是"买两瓶药冲精英"）；
        - 栏位满**整类不评**（调用方在 `_shop_preferred_purchase` 里就跳过了）。
        """
        category = self._potion_category(potion)
        priority = _POTION_CATEGORY_PRIORITY.get(category, 0.3)
        if category in missing_categories:
            fit = SHOP_POTION_NEEDED_FIT * priority
            note = "还缺这一类"
        elif category in owned_categories:
            fit = SHOP_POTION_COVERED_FIT * priority
            note = "同类已有"
        else:
            fit = SHOP_POTION_BASELINE_FIT.get(category, 0.2)
            note = "非冲精英必需品"
        score = fit * 100.0 * max(0.1, boost)
        return score, f"用途={_POTION_CATEGORY_LABELS.get(category, category)}({note}) 补缺={fit:.2f} 情境={boost:.2f}"

    def _score_shop_removal(self, residual: dict[str, float], *, gold: int) -> tuple[float, str]:
        """删牌服务：分数来自"牌组质量"缺口（未升级的打击/防御、诅咒/状态牌的张数）。"""
        gap = residual.get(SHOP_DIM_QUALITY, 0.0)
        return gap * 100.0, f"补缺=质量{gap:.2f}"

    # ---- 日志 -----------------------------------------------------------------------------

    def _log_shop_decision(
        self,
        summary_context: dict[str, Any],
        *,
        entries: list[dict[str, Any]],
        skipped: list[str],
        best: dict[str, Any] | None,
        best_blocked: dict[str, Any] | None,
        threshold: float,
        base_threshold: float,
        policy: dict[str, Any],
        gold: int,
        elite_ahead: bool,
        relic_budget: int,
        potion_reserve: int,
        missing_categories: set[str],
        reason: str,
    ) -> None:
        """每件商品一行 + 一句结论（规格「四、日志与可观测」）。

        玩家口径要求"日志要说实话"：老实现给的 reason 是 `shop_high_value_relic`，而它其实只判断了
        "买得起"。这里把每项的分数构成、门槛、以及"没买时最高分是多少"都写清楚，方便事后调参。
        `_debug` 在本文件里是空函数，所以这些行必须走 info 级。
        """
        if self._logger is None:
            return
        kind_labels = {"relic": "遗物", "card": "卡牌", "potion": "药水", "remove": "删牌"}
        lines: list[str] = []
        for entry in entries:
            penalty = float(entry.get("penalty") or 0.0)
            own_threshold = self._entry_threshold(base_threshold, entry=entry, elite_ahead=elite_ahead, policy=policy)
            if entry["blocked"]:
                verdict = f"不买({entry['blocked']})"
            elif best is not None and entry is best and reason == "shop_scored_buy":
                verdict = "买"
            elif entry["score"] >= own_threshold:
                # 够门槛但不是最高分：**如实说**。（以前这里会被写成"低于门槛"，那是假日志。）
                verdict = f"不买(够门槛 {own_threshold:.1f}，但最高分是 {best['score']:.1f} 的{kind_labels.get(str(best['kind']), best['kind'])})"
            else:
                verdict = f"不买(低于门槛 {own_threshold:.1f})"
            lines.append(
                f"[sts2_shop] item={kind_labels.get(str(entry['kind']), str(entry['kind']))}「{entry['name']}」"
                f" score={entry['score']:.1f} ({entry['detail']} 价={entry['price']} 代价={-penalty:.1f}) → {verdict}"
            )
        lines.extend(f"[sts2_shop] {line}" for line in skipped)
        if best is not None and reason == "shop_scored_buy":
            lines.append(
                f"[sts2_shop] 最高分={best['score']:.1f}（{kind_labels.get(str(best['kind']), best['kind'])}「{best['name']}」）"
                f" ≥ 门槛 {threshold:.1f} → 买"
            )
        elif best is not None:
            lines.append(
                f"[sts2_shop] 最高分={best['score']:.1f}（{kind_labels.get(str(best['kind']), best['kind'])}「{best['name']}」）"
                f" < 门槛 {threshold:.1f} → 一分不花，钱留到下一个商店"
            )
        elif best_blocked is not None:
            lines.append(
                f"[sts2_shop] 最高分={best_blocked['score']:.1f} 但被预算拦下（{best_blocked['blocked']}）"
                f"：留给药水 {potion_reserve} + 底钱，遗物预算={relic_budget} → 一分不花"
            )
        else:
            lines.append("[sts2_shop] 没有可买项 → 一分不花")
        lines.append(
            f"[sts2_shop] 金币={gold} 门槛={threshold:.1f}(基础 {base_threshold:.1f}) 前方有精英={elite_ahead} "
            f"还缺药水类别={sorted(missing_categories) or '-'} 决定={reason}"
        )
        key = f"{self._shop_visit_signature(summary_context)}|" + "\n".join(lines)
        if key == self._shop_log_key:
            return
        self._shop_log_key = key
        try:
            for line in lines:
                self._logger.info(line)
        except Exception:
            pass

    def _shop_card_removal(self, summary_context: dict[str, Any]) -> dict[str, Any] | None:
        """删牌服务（`shop.card_removal`）：只有牌组质量缺口高才值得买。

        注意 `remove_card_at_shop` 动作**不接 option_index**；下一步的 `card_selection_remove` 屏由
        现有的 `_preferred_remove_option` 挑诅咒→打击/防御。
        """
        payload = summary_context.get("payload") if isinstance(summary_context.get("payload"), dict) else {}
        shop = payload.get("shop") if isinstance(payload.get("shop"), dict) else {}
        removal = shop.get("card_removal") if isinstance(shop.get("card_removal"), dict) else None
        if removal is None:
            # 兜底：payload 里也可能把删牌服务铺平在顶层（`_build_shop_context` 一直在铺平别的字段）。
            flat = payload.get("card_removal")
            removal = flat if isinstance(flat, dict) else None
        if removal is None:
            return None
        if removal.get("used") is True or removal.get("available") is False:
            return None
        return removal

    def _shop_best_card_index(self, summary_context: dict[str, Any], preferences: dict[str, Any], min_score: int) -> int | None:
        """按 `_score_reward_card` 原始分取最好的卡（选牌屏/扰动排序仍在用）。

        商店采购**不再**走这个方法：那边走统一评分池 `_shop_preferred_purchase`。
        """
        cards, _relics, _potions = self._shop_affordable_items(summary_context)
        if not cards:
            return None
        payload = summary_context.get("payload") if isinstance(summary_context.get("payload"), dict) else {}
        deck = payload.get("deck") if isinstance(payload.get("deck"), dict) else {}
        archetype_tags = [str(tag).strip().lower() for tag in self._infer_archetype_tags(deck)] if isinstance(deck, dict) else []
        records = preferences.get("records") if isinstance(preferences.get("records"), list) else []
        deck_policy = self._reward_deck_policy(records)
        best: tuple[int, int] | None = None
        for card in cards:
            score = self._score_reward_card(card, deck_policy, archetype_tags)
            if score < min_score:
                continue
            index = self._safe_int(card.get("index"))
            if index is None:
                index = self._safe_int(card.get("card_index"))
            if index is None:
                continue
            if best is None or score > best[0]:
                best = (score, index)
        return best[1] if best else None

    def _preferred_map_index(self, summary_context: dict[str, Any], preferences: dict[str, Any], snapshot: dict[str, Any] | None = None) -> int | None:
        payload = summary_context.get("payload") if isinstance(summary_context.get("payload"), dict) else {}
        travelable_nodes = payload.get("travelable_nodes") if isinstance(payload.get("travelable_nodes"), list) else []
        current_hp = self._safe_int(payload.get("current_hp")) or 0
        max_hp = self._safe_int(payload.get("max_hp")) or 0
        gold = self._safe_int(payload.get("gold")) or 0
        hp_ratio = (current_hp / max_hp) if max_hp > 0 else 0.0
        route_policy = self._route_policy(preferences)
        # 优先走**链式递推**：拿到整幕 DAG 就对每个候选评估"递归到 boss 的整条路线"。
        # 拿不到图（旧 mod / payload 里没有 map.nodes）才退回单步评分，行为与旧版一致。
        graph = self._map_graph_from_snapshot(snapshot)
        if graph:
            scored: list[tuple[int, float]] = []
            best_route: tuple[float, int] | None = None
            nodes_by_index: dict[int, dict[str, Any]] = {}
            for position, node in enumerate(travelable_nodes):
                if not isinstance(node, dict):
                    continue
                route_score = self.evaluate_map_route(
                    graph,
                    node,
                    route_policy=route_policy,
                    hp_ratio=hp_ratio,
                    gold=gold,
                )
                if route_score is None:
                    best_route = None
                    break
                node_index = self._safe_int(node.get("index"))
                if node_index is None:
                    node_index = position
                scored.append((node_index, route_score))
                nodes_by_index[node_index] = node
                candidate = (-route_score, node_index)
                if best_route is None or candidate < best_route:
                    best_route = candidate
            if best_route is not None:
                self._log_map_route(scored, best_route[1], hp_ratio=hp_ratio, gold=gold)
                # 顺手记下"这条路前面有没有精英/是不是精英房"：商店里拿不到地图（见 _shop_elite_ahead），
                # 战斗里也只有节点类型能告诉我们这是不是精英/Boss 战（见 in_elite_or_boss_combat）。
                self._note_route_choice(nodes_by_index.get(best_route[1]), graph)
                # 有图就一律取评估最优：**"每条路都不好"正是最需要认真挑的时候**，
                # 这时退回"随手取最左边"等于把评估结果扔了。
                return best_route[1]
        best_candidate: tuple[int, int] | None = None
        best_node: dict[str, Any] | None = None
        for index, node in enumerate(travelable_nodes):
            if not isinstance(node, dict):
                continue
            score = self._score_map_node(node, route_policy, hp_ratio, gold)
            node_index = self._safe_int(node.get("index"))
            if node_index is None:
                node_index = index
            candidate = (-score, node_index)
            if best_candidate is None or candidate < best_candidate:
                best_candidate = candidate
                best_node = node
        if best_candidate is not None and best_candidate[0] < 0:
            self._note_route_choice(best_node, graph)
            return best_candidate[1]
        preferred = self._first_record_option(preferences)
        chosen = preferred if preferred is not None else self._first_available_map_index(summary_context)
        # 拿不到图也要记账（至少知道"选的是不是精英房"）；此时算不出"前方有没有精英"。
        for index, node in enumerate(travelable_nodes):
            if not isinstance(node, dict):
                continue
            node_index = self._safe_int(node.get("index"))
            if node_index is None:
                node_index = index
            if node_index == chosen:
                self._note_route_choice(node, graph)
                break
        return chosen

    def _log_rest_choice(self, summary_context: dict[str, Any], chosen: int) -> None:
        """把篝火的选择用 info 级记一次（同一决定只记一次）。

        玩家会直接质问"为什么又回血不锻造"，所以这里必须留痕：选项 id 列表 + 血量 + 选中的下标。
        """
        if self._logger is None:
            return
        payload = summary_context.get("payload") if isinstance(summary_context.get("payload"), dict) else {}
        rest = payload.get("rest") if isinstance(payload.get("rest"), dict) else {}
        options = rest.get("options") if isinstance(rest.get("options"), list) else []
        ids = [str(item.get("option_id") or item.get("title") or "?") for item in options if isinstance(item, dict)]
        key = f"{chosen}|{'/'.join(ids)}"
        if key == self._rest_choice_log_key:
            return
        self._rest_choice_log_key = key
        try:
            self._logger.info(
                "[sts2_rest] 选项=%s 血=%s/%s -> 选 option_index=%s",
                ids,
                payload.get("current_hp"),
                payload.get("max_hp"),
                chosen,
            )
        except Exception:
            pass

    def _preferred_rest_option(self, summary_context: dict[str, Any], preferences: dict[str, Any]) -> int | None:
        """篝火选哪一项：**默认优先锻造（升级卡牌）**，血低才回血。

        玩家口径：「插件偏爱休息回血，而不是锻造卡牌。锻造可以升级卡牌、提升数值、降低费用，
        对战力提升很大，是需要优先思考的」。原来的实现是

            preferred_option = self._preferred_route_option(preferences) or 0

        —— 要么用**路由**偏好（跟篝火选项没关系），要么直接取下标 0，也就是"列表第一项"，
        而列表第一项通常就是回血。所以插件事实上永远在回血。

        现在的口径：

        1. 玩家显式配过的偏好优先（他说话算数）；
        2. 血比 < `REST_HEAL_HP_RATIO` → 回血优先（快死了还去锻造就是送）；
        3. 否则**锻造/升级优先**（升数值、降费用，战力涨得比几十点血值）；
        4. 认不出类型就取第一个**可用**选项。

        选项类型靠 `option_id` / `title` / `description` 的关键词判断 —— mod 发的是 `option_id`
        （见 GameStateService 的 RestOptionPayload），不是 `type`/`name`。
        """
        payload = summary_context.get("payload") if isinstance(summary_context.get("payload"), dict) else {}
        rest = payload.get("rest") if isinstance(payload.get("rest"), dict) else {}
        options = [item for item in (rest.get("options") if isinstance(rest.get("options"), list) else []) if isinstance(item, dict)]
        if not options:
            return None

        def option_index(position: int, item: dict[str, Any]) -> int:
            index = self._safe_int(item.get("index"))
            return index if index is not None else position

        # 1) 玩家显式偏好（route/rest preference）—— 只要落在范围内就听他的。
        explicit = self._preferred_route_option(preferences)
        if explicit is not None and any(option_index(position, item) == explicit for position, item in enumerate(options)):
            return explicit

        current_hp = self._safe_int(payload.get("current_hp")) or 0
        max_hp = self._safe_int(payload.get("max_hp")) or 0
        hp_ratio = (current_hp / max_hp) if max_hp > 0 else 1.0

        def kind(item: dict[str, Any]) -> str:
            text = " ".join(
                str(item.get(key) or "") for key in ("option_id", "title", "description", "name", "type")
            ).lower()
            if any(token in text for token in _REST_SMITH_TOKENS):
                return "smith"
            if any(token in text for token in _REST_HEAL_TOKENS):
                return "heal"
            return "other"

        usable = [(position, item) for position, item in enumerate(options) if bool(item.get("is_enabled", True))]
        pool = usable or list(enumerate(options))
        wanted = "heal" if hp_ratio < REST_HEAL_HP_RATIO else "smith"
        for position, item in pool:
            if kind(item) == wanted:
                return option_index(position, item)
        for position, item in pool:
            if kind(item) == "other":
                return option_index(position, item)
        return option_index(*pool[0])

    def _reward_kwargs(self, summary_context: dict[str, Any], action: dict[str, Any], preferred_option: int | None) -> dict[str, Any]:
        action_type = str(action.get("type") or "").strip().lower()
        if action_type == "claim_reward":
            return {"option_index": preferred_option if preferred_option is not None else 0}
        if action_type == "choose_reward_card":
            return {"option_index": preferred_option if preferred_option is not None else 0}
        return {}

    def _guidance_override(self, context: dict[str, Any]) -> dict[str, bool]:
        summary_context = context.get("summary_context") if isinstance(context.get("summary_context"), dict) else {}
        return self._guidance_override_from_payload(summary_context)

    def _guidance_override_from_payload(self, summary_context: dict[str, Any]) -> dict[str, bool]:
        decision_payload = summary_context.get("decision_payload") if isinstance(summary_context.get("decision_payload"), dict) else {}
        instructions = decision_payload.get("instructions") if isinstance(decision_payload.get("instructions"), list) else []
        guidance_lines = [
            str(item.get("content") or "")
            for item in instructions
            if isinstance(item, dict) and str(item.get("source") or "") == "neko_guidance"
        ]
        merged = " ".join(guidance_lines).lower()
        if not merged:
            guidance = decision_payload.get("guidance") if isinstance(decision_payload.get("guidance"), dict) else {}
            pending = guidance.get("pending") if isinstance(guidance.get("pending"), list) else []
            merged = " ".join(str(item.get("content") or "") for item in pending if isinstance(item, dict)).lower()
        if not merged:
            return {}
        prefer_defense = any(token in merged for token in ["先防", "优先防", "保血", "保命", "别贪", "不要贪", "求稳", "稳一点"])
        prefer_attack = any(token in merged for token in ["优先输出", "抢伤害", "压血", "收掉", "收尾", "斩杀"])
        return {
            "prefer_defense": prefer_defense,
            "prefer_attack": prefer_attack,
            "avoid_greed": any(token in merged for token in ["别贪", "不要贪"]),
            "prefer_lethal": any(token in merged for token in ["斩杀", "收掉", "收尾"]),
            "conservative": prefer_defense or any(token in merged for token in ["求稳", "稳一点"]),
        }

    def _preferred_combat_action(self, actions: list[dict[str, Any]], snapshot: dict[str, Any], preferences: dict[str, Any], guidance_override: dict[str, bool]) -> dict[str, Any] | None:
        play_card = self._find_action(actions, "play_card")
        end_turn = self._find_action(actions, "end_turn")
        if play_card is None:
            return end_turn
        incoming_attack = self._incoming_attack_total(snapshot)
        current_block = self._current_block(snapshot)
        gap = max(0, incoming_attack - current_block)
        if guidance_override.get("prefer_defense") and gap > 0:
            preferred_card_index = self._preferred_combat_card_index(snapshot, preferences, guidance_override)
            if preferred_card_index is not None:
                return play_card
        if guidance_override.get("prefer_attack"):
            if gap >= 6:
                preferred_card_index = self._preferred_combat_card_index(snapshot, preferences, {"prefer_defense": True})
                if preferred_card_index is not None:
                    return play_card
            return play_card
        return play_card or end_turn

    def _preferred_combat_card_index(self, snapshot: dict[str, Any], preferences: dict[str, Any], guidance_override: dict[str, bool] | None = None) -> int | None:
        guidance_override = guidance_override or {}
        preferred = self._first_record_option(preferences)
        raw_state = snapshot.get("raw_state") if isinstance(snapshot.get("raw_state"), dict) else {}
        combat = raw_state.get("combat") if isinstance(raw_state.get("combat"), dict) else {}
        hand = combat.get("hand") if isinstance(combat.get("hand"), list) else []
        if guidance_override.get("prefer_defense"):
            defensive_index = self._first_matching_playable_card(hand, ["block", "护盾", "格挡", "defend"])
            if defensive_index is not None:
                return defensive_index
        if guidance_override.get("prefer_attack"):
            attack_index = self._first_matching_playable_card(hand, ["attack", "damage", "伤害", "strike"])
            if attack_index is not None:
                return attack_index
        if preferred is not None:
            return preferred
        for card in hand:
            if not isinstance(card, dict):
                continue
            if bool(card.get("playable")):
                return self._safe_int(card.get("index"))
        return None

    def _combat_kwargs(self, snapshot: dict[str, Any], action: dict[str, Any], card_index: int | None) -> dict[str, Any]:
        if action.get("type") != "play_card" or card_index is None:
            return {}
        kwargs: dict[str, Any] = {"card_index": card_index}
        raw_state = snapshot.get("raw_state") if isinstance(snapshot.get("raw_state"), dict) else {}
        combat = raw_state.get("combat") if isinstance(raw_state.get("combat"), dict) else {}
        hand = combat.get("hand") if isinstance(combat.get("hand"), list) else []
        enemies = combat.get("enemies") if isinstance(combat.get("enemies"), list) else []
        selected_card = next(
            (card for card in hand if isinstance(card, dict) and self._safe_int(card.get("index")) == card_index),
            None,
        )
        if selected_card is not None:
            target_index = self._preferred_target_for_card(selected_card, enemies)
        else:
            target_index = self._preferred_combat_target_index(snapshot, card_index)
        if target_index is not None:
            kwargs["target_index"] = target_index
        return kwargs

    def _preferred_combat_target_index(self, snapshot: dict[str, Any], card_index: int) -> int | None:
        raw_state = snapshot.get("raw_state") if isinstance(snapshot.get("raw_state"), dict) else {}
        combat = raw_state.get("combat") if isinstance(raw_state.get("combat"), dict) else {}
        hand = combat.get("hand") if isinstance(combat.get("hand"), list) else []
        for card in hand:
            if not isinstance(card, dict):
                continue
            if self._safe_int(card.get("index")) != card_index:
                continue
            valid_target_indices = card.get("valid_target_indices") if isinstance(card.get("valid_target_indices"), list) else []
            if not valid_target_indices:
                return None
            try:
                return int(valid_target_indices[0])
            except Exception:
                return None
        return None

    def _first_matching_playable_card(self, hand: list[dict[str, Any]], keywords: list[str]) -> int | None:
        normalized_keywords = [keyword.lower() for keyword in keywords]
        for card in hand:
            if not isinstance(card, dict) or not bool(card.get("playable")):
                continue
            haystack = " ".join(
                [
                    str(card.get("name") or ""),
                    str(card.get("id") or ""),
                    str(card.get("description") or ""),
                    str(card.get("effect") or ""),
                    str(card.get("card_type") or ""),
                ]
            ).lower()
            if any(keyword in haystack for keyword in normalized_keywords):
                return self._safe_int(card.get("index"))
        return None

    def _plan_combat_turn_sequence(
        self,
        snapshot: dict[str, Any],
        actions: list[dict[str, Any]],
        preferences: dict[str, Any],
        guidance_override: dict[str, bool],
    ) -> list[dict[str, Any]]:
        play_card = self._find_action(actions, "play_card")
        if play_card is None:
            return []
        raw_state = snapshot.get("raw_state") if isinstance(snapshot.get("raw_state"), dict) else {}
        combat = raw_state.get("combat") if isinstance(raw_state.get("combat"), dict) else {}
        player = combat.get("player") if isinstance(combat.get("player"), dict) else {}
        hand = [card for card in (combat.get("hand") if isinstance(combat.get("hand"), list) else []) if isinstance(card, dict) and bool(card.get("playable"))]
        enemies = combat.get("enemies") if isinstance(combat.get("enemies"), list) else []
        current_energy = self._safe_int(player.get("energy")) or 0
        current_block = self._safe_int(player.get("block")) or 0
        incoming_attack = self._incoming_attack_total(snapshot)

        lethal_cards = self._find_labeled_lethal_sequence(hand, current_energy, enemies)
        if lethal_cards:
            self._debug("[sts2_combat_plan] lethal_sequence=%s", lethal_cards)
            return lethal_cards

        effective_override = dict(guidance_override)
        if not effective_override.get("prefer_attack") and incoming_attack > current_block:
            effective_override["prefer_defense"] = True

        sequence: list[dict[str, Any]] = []
        remaining_cards = list(hand)
        remaining_energy = current_energy
        simulated_block = current_block
        threshold = 6

        while remaining_cards and remaining_energy > 0:
            gap = max(0, incoming_attack - simulated_block)
            if effective_override.get("prefer_attack"):
                if gap >= threshold:
                    next_card = self._best_defense_card(remaining_cards, remaining_energy)
                    reason = "combat_attack_priority_sequence"
                else:
                    next_card = self._best_attack_card(remaining_cards, remaining_energy) or self._best_defense_card(remaining_cards, remaining_energy)
                    reason = "combat_attack_priority_sequence"
            elif effective_override.get("prefer_defense"):
                if gap > 0:
                    next_card = self._best_defense_card(remaining_cards, remaining_energy)
                    reason = "combat_defense_sequence"
                else:
                    next_card = self._best_attack_card(remaining_cards, remaining_energy)
                    reason = "combat_defense_sequence"
            else:
                next_card = self._best_defense_card(remaining_cards, remaining_energy) if gap > 0 else self._best_attack_card(remaining_cards, remaining_energy)
                reason = "combat_preference_or_default"

            self._debug(
                "[sts2_combat_step] gap=%s energy=%s block=%s mode=%s next=%s",
                gap,
                remaining_energy,
                simulated_block,
                reason,
                {
                    "index": self._safe_int(next_card.get("index")) if isinstance(next_card, dict) else None,
                    "name": str(next_card.get("name") or next_card.get("card_id") or "?") if isinstance(next_card, dict) else None,
                    "damage": self._card_damage(next_card) if isinstance(next_card, dict) else None,
                    "block": self._card_block(next_card) if isinstance(next_card, dict) else None,
                    "cost": self._card_energy_cost(next_card) if isinstance(next_card, dict) else None,
                },
            )

            if next_card is None:
                break
            cost = self._card_energy_cost(next_card)
            if cost > remaining_energy:
                break
            remaining_energy -= cost
            simulated_block += self._card_block(next_card)
            sequence.append(
                {
                    "card_index": self._safe_int(next_card.get("index")) or 0,
                    "target_index": self._preferred_target_for_card(next_card, enemies),
                    "reason": reason,
                }
            )
            remaining_cards = [card for card in remaining_cards if card is not next_card]
            if not effective_override.get("prefer_attack") and simulated_block >= incoming_attack:
                effective_override["prefer_defense"] = False
                effective_override["prefer_attack"] = True

        return sequence

    def _find_labeled_lethal_sequence(self, cards: list[dict[str, Any]], current_energy: int, enemies: list[dict[str, Any]]) -> list[dict[str, Any]]:
        for enemy_index, enemy in enumerate(enemies):
            if not isinstance(enemy, dict):
                continue
            enemy_hp = self._safe_int(enemy.get("current_hp") if enemy.get("current_hp") is not None else enemy.get("hp")) or 0
            if enemy_hp <= 0:
                continue
            lethal_cards = self._find_lethal_card_sequence(cards, current_energy, enemy_hp)
            if not lethal_cards:
                continue
            return [
                {
                    "card_index": self._safe_int(card.get("index")) or 0,
                    "target_index": self._lethal_target_index(card, enemy_index),
                    "reason": "combat_lethal_sequence",
                }
                for card in lethal_cards
            ]
        return []

    def _best_defense_card(self, cards: list[dict[str, Any]], current_energy: int) -> dict[str, Any] | None:
        candidates = [card for card in cards if self._card_energy_cost(card) <= current_energy and self._card_block(card) > 0]
        if not candidates:
            return None
        return max(candidates, key=self._card_block)

    def _best_attack_card(self, cards: list[dict[str, Any]], current_energy: int) -> dict[str, Any] | None:
        candidates = [card for card in cards if self._card_energy_cost(card) <= current_energy and self._card_damage(card) > 0]
        if not candidates:
            return None
        return max(candidates, key=self._card_damage)

    def _preferred_target_for_card(self, card: dict[str, Any], enemies: list[dict[str, Any]]) -> int | None:
        valid_target_indices = card.get("valid_target_indices") if isinstance(card.get("valid_target_indices"), list) else []
        if not valid_target_indices:
            return None
        damage = self._card_damage(card)
        best_target = None
        best_priority: tuple[int, int, int] | None = None
        for target_index in valid_target_indices:
            try:
                normalized_index = int(target_index)
            except Exception:
                continue
            if normalized_index >= len(enemies) or normalized_index < 0:
                continue
            enemy = enemies[normalized_index]
            if not isinstance(enemy, dict):
                continue
            hp = self._safe_int(enemy.get("current_hp") if enemy.get("current_hp") is not None else enemy.get("hp")) or 0
            incoming = self._safe_int(enemy.get("intent_damage")) or 0
            lethal = 1 if damage > 0 and hp <= damage else 0
            priority = (lethal, incoming, -hp)
            if best_priority is None or priority > best_priority:
                best_priority = priority
                best_target = normalized_index
        return best_target if best_target is not None else self._lethal_target_index(card, 0)

    def _card_block(self, card: dict[str, Any]) -> int:
        for item in card.get("dynamic_values") if isinstance(card.get("dynamic_values"), list) else []:
            if not isinstance(item, dict):
                continue
            if str(item.get("name") or "").strip().lower() == "block":
                parsed = self._safe_int(item.get("current_value"))
                if parsed is not None:
                    return parsed
        block = self._safe_int(card.get("block"))
        if block is not None:
            return block
        effect_text = " ".join(
            [
                str(card.get("resolved_rules_text") or ""),
                str(card.get("description") or ""),
                str(card.get("effect") or ""),
            ]
        )
        lowered = effect_text.lower()
        if "block" not in lowered and "护盾" not in effect_text and "格挡" not in effect_text:
            return 0
        for token in effect_text.replace("Gain", " ").replace("Block", " ").split():
            parsed = self._safe_int(token)
            if parsed is not None:
                return parsed
        match = re.search(r"获得\s*(\d+)\s*点?(?:格挡|护盾|护甲)", effect_text)
        if match:
            return int(match.group(1))
        return 0

    def _is_unremovable_card(self, card: dict[str, Any]) -> bool:
        if bool(card.get("unremovable")) or bool(card.get("cannot_remove")):
            return True
        text = " ".join(
            [
                str(card.get("name") or ""),
                str(card.get("description") or ""),
                str(card.get("effect") or ""),
                str(card.get("rules_text") or ""),
                str(card.get("resolved_rules_text") or ""),
            ]
        ).lower()
        return "eternal" in text or "永恒" in text

    def _score_remove_card(self, card: dict[str, Any]) -> int:
        text = " ".join(
            [
                str(card.get("name") or ""),
                str(card.get("description") or ""),
                str(card.get("effect") or ""),
                str(card.get("rules_text") or ""),
                str(card.get("resolved_rules_text") or ""),
                str(card.get("card_type") or card.get("type") or ""),
                str(card.get("rarity") or ""),
            ]
        ).lower()
        if any(token in text for token in ["curse", "诅咒"]):
            return -100
        if any(token in text for token in ["status", "状态"]):
            return -90
        if any(token in text for token in ["strike", "打击"]):
            return -60
        if any(token in text for token in ["defend", "防御"]):
            return -50
        if any(token in text for token in ["draw", "skim", "coolheaded", "抽牌", "过牌"]):
            return 60
        if any(token in text for token in ["energy", "turbo", "double energy", "能量"]):
            return 70
        if any(token in text for token in ["focus", "orb", "电球", "充能"]):
            return 65
        return 10

    def _route_policy(self, preferences: dict[str, Any]) -> dict[str, Any]:
        records = preferences.get("records") if isinstance(preferences.get("records"), list) else []
        for record in records:
            if not isinstance(record, dict):
                continue
            value = record.get("value") if isinstance(record.get("value"), dict) else {}
            if str(value.get("instruction_type") or "") != "route_policy":
                continue
            return value
        return {}

    def _score_event_option(self, option: dict[str, Any], hp_ratio: float, gold: int) -> int:
        text = self._event_option_text(option)
        score = 0
        if any(token in text for token in ["lose hp", "hp", "失去生命", "掉血", "流血"]):
            score -= 20 if hp_ratio < 0.6 else 8
        if any(token in text for token in ["gain gold", "gold", "获得金币", "金币"]):
            score += 10 if gold < 150 else 4
        if any(token in text for token in ["relic", "遗物", "获得遗物"]):
            score += 14
        if any(token in text for token in ["card", "卡牌", "获得卡"]):
            score += 8
        if any(token in text for token in ["remove", "删除", "移除"]):
            score += 12
        if any(token in text for token in ["curse", "诅咒"]):
            score -= 18
        return score

    def _event_option_text(self, option: dict[str, Any]) -> str:
        return " ".join(
            [
                str(option.get("label") or ""),
                str(option.get("text") or ""),
                str(option.get("description") or ""),
            ]
        ).lower()

    def _score_map_node(self, node: dict[str, Any], route_policy: dict[str, Any], hp_ratio: float, gold: int) -> int:
        node_type = self._map_node_weight_key(node)
        weights = self._route_effective_weights(route_policy, hp_ratio, gold)
        return int(weights.get(node_type, 0))

    def _route_effective_weights(self, route_policy: dict[str, Any], hp_ratio: float, gold: int) -> dict[str, float]:
        """把"内置打底 -> 玩家配置 -> 低血替换"整理成一张明确的权重表。

        优先级：内置保守表 < 玩家的 route_policy.weights；血量低于阈值时整张表换成低血表
        （玩家配了 conditions.low_hp.weights 就用玩家的，否则用内置 SAFE_MAP_LOW_HP_WEIGHTS）。

        注意：低血替换现在对**自动选路**也生效了。以前只有玩家手点的"定个路线 safe"会走
        `_safe_route_preferences`（带 conditions），自动选路拿到的 route_policy 是空表，于是
        `_score_map_node` 内部的兜底里只有商店/精英两处硬编码的血量判断 —— 血再低也不会整体转向休息。
        """
        base: dict[str, float] = {}
        user_weights = route_policy.get("weights") if isinstance(route_policy.get("weights"), dict) else {}
        if user_weights:
            for key, value in user_weights.items():
                try:
                    base[str(key).strip().lower()] = float(value)
                except (TypeError, ValueError):
                    continue
        else:
            # 没配过就用与历史行为一致的内置值（这样"未配置"时选路口径不变）。
            base = {
                "rest": 12.0,
                "shop": 8.0 if gold >= ROUTE_BOSS_PREP_GOLD else 4.0,
                "elite": -10.0 if hp_ratio < SAFE_MAP_HP_RATIO_BELOW else -4.0,
                "monster": 3.0,
                "event": 4.0,
                "treasure": 5.0,
                "question": 4.0,
            }
        conditions = route_policy.get("conditions") if isinstance(route_policy.get("conditions"), dict) else {}
        low_hp = conditions.get("low_hp") if isinstance(conditions.get("low_hp"), dict) else {}
        try:
            threshold = float(low_hp.get("hp_ratio_below", SAFE_MAP_HP_RATIO_BELOW))
        except (TypeError, ValueError):
            threshold = SAFE_MAP_HP_RATIO_BELOW
        if hp_ratio < threshold:
            low_table = low_hp.get("weights") if isinstance(low_hp.get("weights"), dict) else SAFE_MAP_LOW_HP_WEIGHTS
            for key, value in low_table.items():
                try:
                    base[str(key).strip().lower()] = float(value)
                except (TypeError, ValueError):
                    continue
        return base

    # ---- 全图 DAG 的读取与链式递推 ---------------------------------------------------------

    def _map_graph_from_snapshot(self, snapshot: dict[str, Any] | None) -> dict[str, Any]:
        """从 snapshot 里取出整幕地图 DAG（mod /state 的 map.nodes）。

        为什么从 snapshot 取而不是塞进 summary_context.payload：那张图有几十个节点，
        payload 会跟着 sync 包推给 NEKO，之前已经因为"往状态里塞大对象"踩过 ZMQ 超时。
        图只在规划器内部用，就地取最省事。
        """
        if not isinstance(snapshot, dict):
            return {}
        raw_state = snapshot.get("raw_state") if isinstance(snapshot.get("raw_state"), dict) else {}
        game_map = raw_state.get("map") if isinstance(raw_state.get("map"), dict) else {}
        nodes = [node for node in (game_map.get("nodes") if isinstance(game_map.get("nodes"), list) else []) if isinstance(node, dict)]
        if not nodes:
            return {}
        return {"nodes": nodes, "boss_node": game_map.get("boss_node"), "rows": game_map.get("rows")}

    def _map_graph_index(self, graph: dict[str, Any]) -> dict[tuple[int, int], dict[str, Any]]:
        """(row, col) -> 节点。mod 的 children/parents 是坐标对，必须靠这张索引才能解出类型。"""
        index: dict[tuple[int, int], dict[str, Any]] = {}
        for node in graph.get("nodes") if isinstance(graph.get("nodes"), list) else []:
            if not isinstance(node, dict):
                continue
            row = self._safe_int(node.get("row"))
            col = self._safe_int(node.get("col"))
            if row is None or col is None:
                continue
            index[(row, col)] = node
        return index

    def _map_boss_row(self, graph: dict[str, Any], index: dict[tuple[int, int], dict[str, Any]]) -> int | None:
        boss = graph.get("boss_node") if isinstance(graph.get("boss_node"), dict) else {}
        row = self._safe_int(boss.get("row"))
        if row is not None:
            return row
        rows = [r for (r, _c), node in index.items() if bool(node.get("is_boss"))]
        return max(rows) if rows else None

    def _map_child_keys(self, node: dict[str, Any]) -> list[tuple[int, int]]:
        """节点的子节点坐标列表（mod 给的是 children: [{row, col}, ...]）。"""
        keys: list[tuple[int, int]] = []
        for child in node.get("children") if isinstance(node.get("children"), list) else []:
            if not isinstance(child, dict):
                continue
            row = self._safe_int(child.get("row"))
            col = self._safe_int(child.get("col"))
            if row is None or col is None:
                continue
            keys.append((row, col))
        return keys

    def evaluate_map_route(
        self,
        graph: dict[str, Any],
        start: dict[str, Any],
        *,
        route_policy: dict[str, Any] | None = None,
        hp_ratio: float = 1.0,
        gold: int = 0,
        lookahead_rows: int | None = None,
    ) -> float | None:
        """从 `start` 这个候选节点出发、递归到 boss 的**整条路线**评分。

        - 分支点取 **max**：到那一层我们还会再选一次，所以是"沿途都选最好"而不是求和。
        - 每远一层乘 `ROUTE_DISCOUNT_PER_ROW`：远处的不确定性更大。
        - 同类节点按出现次数递减（`ROUTE_REPEAT_DECAY`）：两个休息点不等于两倍休息。
        - 临门（距 boss <= ROUTE_BOSS_PREP_ROWS 层）的休息/商店加权：补血、把金币花掉。
        - 精英按血量缓和：血越满，精英那点机会成本越低（精英给遗物+金币，未必是坏事）。

        返回 None 表示拿不到图（旧 mod / 非地图界面），调用方应退回单步评分。
        """
        index = self._map_graph_index(graph)
        if not index:
            return None
        row = self._safe_int(start.get("row"))
        col = self._safe_int(start.get("col"))
        if row is None or col is None or (row, col) not in index:
            return None
        policy = route_policy if isinstance(route_policy, dict) else {}
        weights = self._route_effective_weights(policy, hp_ratio, gold)
        try:
            depth = int(lookahead_rows if lookahead_rows is not None else (policy.get("lookahead_depth") or ROUTE_LOOKAHEAD_ROWS_FALLBACK))
        except (TypeError, ValueError):
            depth = ROUTE_LOOKAHEAD_ROWS_FALLBACK
        boss_row = self._map_boss_row(graph, index)
        return self._route_value(
            index,
            (row, col),
            weights=weights,
            hp_ratio=hp_ratio,
            gold=gold,
            boss_row=boss_row,
            depth=max(1, depth),
            counters={},
            path=set(),
        )

    def _route_value(
        self,
        index: dict[tuple[int, int], dict[str, Any]],
        key: tuple[int, int],
        *,
        weights: dict[str, float],
        hp_ratio: float,
        gold: int,
        boss_row: int | None,
        depth: int,
        counters: dict[str, int],
        path: set[tuple[int, int]],
    ) -> float:
        node = index.get(key)
        if node is None or depth < 0 or key in path:
            return 0.0
        node_type = self._map_node_weight_key(node)
        value = float(weights.get(node_type, 0.0))

        # 同类饱和：第 k 次出现按 ROUTE_REPEAT_DECAY^(k-1) 递减。
        seen = counters.get(node_type, 0)
        value *= ROUTE_REPEAT_DECAY ** seen

        # 精英缓和：血越满，精英那点机会成本越低。
        if node_type == "elite" and value < 0:
            relief = min(1.0, max(0.0, hp_ratio)) * ROUTE_ELITE_RELIEF_MAX
            value *= (1.0 - relief)

        # 临门一脚：紧邻 boss 的休息/商店价值远高于中途。
        if node_type in {"rest", "shop"} and self._is_boss_prep_row(node, boss_row):
            if node_type != "shop" or gold >= ROUTE_BOSS_PREP_GOLD:
                value *= ROUTE_BOSS_PREP_MULTIPLIER

        counters[node_type] = seen + 1
        path.add(key)
        best_child = 0.0
        if depth > 0:
            for child_key in self._map_child_keys(node):
                best_child = max(
                    best_child,
                    self._route_value(
                        index,
                        child_key,
                        weights=weights,
                        hp_ratio=hp_ratio,
                        gold=gold,
                        boss_row=boss_row,
                        depth=depth - 1,
                        counters=counters,
                        path=path,
                    ),
                )
        path.discard(key)
        counters[node_type] = seen
        return value + ROUTE_DISCOUNT_PER_ROW * best_child

    @staticmethod
    def _map_node_weight_key(node: dict[str, Any]) -> str:
        """node_type -> 权重表的键。认不出就原样返回（玩家仍可为原始名配权重）。"""
        raw = str(node.get("type") or node.get("node_type") or "").strip().lower()
        if not raw:
            return ""
        return _MAP_NODE_TYPE_ALIASES.get(raw, raw)

    @staticmethod
    def _character_list(summary_context: dict[str, Any]) -> list[dict[str, Any]]:
        payload = summary_context.get("payload") if isinstance(summary_context.get("payload"), dict) else {}
        raw = payload.get("character_select") if isinstance(payload.get("character_select"), dict) else {}
        chars = raw.get("characters") if isinstance(raw.get("characters"), list) else []
        return [c for c in chars if isinstance(c, dict)]

    def _character_list_signature(self, summary_context: dict[str, Any]) -> str:
        return "|".join(
            f"{c.get('index')}:{c.get('character_id')}" for c in self._character_list(summary_context)
        )

    def _random_character_index(self, summary_context: dict[str, Any]) -> int | None:
        """游戏自带的「随机」候选的下标（跳过锁着的）。"""
        for character in self._character_list(summary_context):
            if not bool(character.get("is_random")) or bool(character.get("is_locked")):
                continue
            index = self._safe_int(character.get("index"))
            if index is not None:
                return index
        return None

    def _selected_character_is_random(self, summary_context: dict[str, Any]) -> bool:
        payload = summary_context.get("payload") if isinstance(summary_context.get("payload"), dict) else {}
        selected = str(payload.get("selected_character_id") or "")
        if not selected:
            return False
        for character in self._character_list(summary_context):
            if str(character.get("character_id") or "") == selected:
                return bool(character.get("is_random"))
        return False

    def _log_character_choice(self, decision: str, option_index: Any) -> None:
        """把选人界面的决定用 info 级记一次（同一决定只记一次）。

        为什么要留痕：`_debug` 在本文件里是**空函数**，玩家问"角色真随机了吗"时，
        日志里必须能看出"按了随机的那个候选"，而不是只能靠猜。
        """
        if self._logger is None:
            return
        key = f"{decision}:{option_index}"
        if key == self._character_choice_log_key:
            return
        self._character_choice_log_key = key
        try:
            self._logger.info("[sts2_character_select] decision=%s option_index=%s", decision, option_index)
        except Exception:
            pass

    def note_character_select_dispatched(self, option_index: Any) -> None:
        """服务层在**真的发出了** select_character 之后调用（用于"随机"只按一次）。"""
        if self._safe_int(option_index) is None:
            return
        self._character_pick_done = True

    @staticmethod
    def _is_boss_prep_row(node: dict[str, Any], boss_row: int | None) -> bool:
        if boss_row is None:
            return False
        own_row = node.get("row")
        if not isinstance(own_row, int) or isinstance(own_row, bool):
            try:
                own_row = int(own_row)
            except (TypeError, ValueError):
                return False
        return 0 <= (boss_row - own_row) <= ROUTE_BOSS_PREP_ROWS

    def _score_reward_card(self, card: dict[str, Any], deck_policy: dict[str, set[str] | list[dict[str, Any]]], archetype_tags: list[str]) -> int:
        text = " ".join(
            [
                str(card.get("name") or ""),
                str(card.get("description") or ""),
                str(card.get("effect") or ""),
                str(card.get("rules_text") or ""),
                str(card.get("resolved_rules_text") or ""),
                str(card.get("card_type") or card.get("type") or ""),
            ]
        ).lower()
        energy_gain = self._reward_dynamic_value(card, "Energy")
        score = 0
        tag_keywords = {
            "draw": ["draw", "抽牌", "skim", "coolheaded"],
            "defense": ["block", "护盾", "格挡", "defend", "charge battery"],
            "energy": ["energy", "能量", "turbo", "double energy"],
            "orb_focus": ["orb", "focus", "电球", "充能", "defragment", "frost"],
            "expensive_attack": ["2 cost", "3 cost", "x cost", "meteor", "heavy blade"],
            "attack": ["damage", "伤害", "attack", "strike"],
            "strength": ["strength", "力量"],
            "self_damage_attack": ["失去", "lose", "hp loss", "hemokinesis", "御血术"],
            "slow_block_only": ["block", "护盾", "格挡"],
        }
        for tag in deck_policy.get("prefer_tags", set()):
            if any(keyword in text for keyword in tag_keywords.get(tag, [tag])):
                score += 40
        for tag in deck_policy.get("avoid_tags", set()):
            if any(keyword in text for keyword in tag_keywords.get(tag, [tag])):
                score -= 40
        for tag in deck_policy.get("archetype_bias", set()):
            if tag in archetype_tags and any(keyword in text for keyword in tag_keywords.get(tag, [tag])):
                score += 20

        score += self._base_reward_score(text, energy_gain)
        # 数值打分：用 dynamic_values 的实际量级（伤害×连击/格挡/抽牌/能量/易伤），按费用与自损扣减。
        score += self._reward_value_score(card)

        branches = deck_policy.get("branches") if isinstance(deck_policy.get("branches"), list) else []
        matched_branch = self._matched_reward_branch(branches, archetype_tags, text)
        if matched_branch is not None:
            prefer_tags = matched_branch.get("prefer_tags") if isinstance(matched_branch.get("prefer_tags"), list) else []
            avoid_tags = matched_branch.get("avoid_tags") if isinstance(matched_branch.get("avoid_tags"), list) else []
            prefer_cards = matched_branch.get("prefer_cards") if isinstance(matched_branch.get("prefer_cards"), list) else []
            avoid_cards = matched_branch.get("avoid_cards") if isinstance(matched_branch.get("avoid_cards"), list) else []
            for tag in prefer_tags:
                if any(keyword in text for keyword in tag_keywords.get(str(tag).strip().lower(), [str(tag).strip().lower()])):
                    score += 35
            for tag in avoid_tags:
                if any(keyword in text for keyword in tag_keywords.get(str(tag).strip().lower(), [str(tag).strip().lower()])):
                    score -= 35
            if any(str(card_name).strip().lower() in text for card_name in prefer_cards):
                score += 50
            if any(str(card_name).strip().lower() in text for card_name in avoid_cards):
                score -= 50
        return score

    def _reward_deck_policy(self, records: list[dict[str, Any]]) -> dict[str, Any]:
        prefer_tags: set[str] = set()
        avoid_tags: set[str] = set()
        archetype_bias: set[str] = set()
        branches: list[dict[str, Any]] = []
        for record in records:
            if not isinstance(record, dict):
                continue
            value = record.get("value") if isinstance(record.get("value"), dict) else {}
            if str(value.get("instruction_type") or "") != "deck_policy":
                continue
            prefer_tags.update(str(tag).strip().lower() for tag in (value.get("prefer_tags") if isinstance(value.get("prefer_tags"), list) else []) if str(tag).strip())
            avoid_tags.update(str(tag).strip().lower() for tag in (value.get("avoid_tags") if isinstance(value.get("avoid_tags"), list) else []) if str(tag).strip())
            archetype_bias.update(str(tag).strip().lower() for tag in (value.get("archetype_bias") if isinstance(value.get("archetype_bias"), list) else []) if str(tag).strip())
            if isinstance(value.get("branches"), list):
                branches.extend(item for item in value.get("branches") if isinstance(item, dict))
        return {
            "prefer_tags": prefer_tags,
            "avoid_tags": avoid_tags,
            "archetype_bias": archetype_bias,
            "branches": branches,
        }

    def _base_reward_score(self, text: str, energy_gain: int) -> int:
        score = 0
        if any(token in text for token in ["block", "护盾", "格挡"]):
            score += 20
        if any(token in text for token in ["draw", "抽牌"]):
            score += 18
        if any(token in text for token in ["damage", "伤害", "attack", "攻击"]):
            score += 12
        if any(token in text for token in ["strength", "力量"]):
            score += 10
        if energy_gain > 0 or any(token in text for token in ["获得能量", "gain energy", "恢复能量", "refund energy"]):
            score += 16 + (energy_gain * 4)
        return score

    def _reward_value_score(self, card: dict[str, Any]) -> float:
        """基于卡牌 dynamic_values 的实际量级打分（补充关键词打分的盲区：不区分 4 伤和 9 伤）。

        伤害×连击、格挡、抽牌、能量、易伤/虚弱均按量加分；自损(HpLoss)与费用扣分。
        """
        dmg = self._reward_dynamic_value(card, "Damage")
        blk = self._reward_dynamic_value(card, "Block")
        draw = self._reward_dynamic_value(card, "Cards")
        energy = self._reward_dynamic_value(card, "Energy")
        vuln = self._reward_dynamic_value(card, "VulnerablePower")
        weak = self._reward_dynamic_value(card, "WeakPower")
        hp_loss = self._reward_dynamic_value(card, "HpLoss")
        # 多段攻击：Repeat 动态值（如 3伤×3）优先，其次 hit_count。
        repeat = self._reward_dynamic_value(card, "Repeat")
        hits = max(1, repeat or self._safe_int(card.get("hit_count")) or 1)
        cost = self._safe_int(card.get("energy_cost")) or 0
        star_cost = self._safe_int(card.get("star_cost")) or 0
        return (
            dmg * hits * 1.0
            + blk * 0.8
            + draw * 6.0
            + energy * 8.0
            + vuln * 3.0
            + weak * 2.0
            - hp_loss * 2.5
            - cost * 2.0
            - star_cost * 1.0
        )

    def _reward_dynamic_value(self, card: dict[str, Any], field_name: str) -> int:
        for item in card.get("dynamic_values") if isinstance(card.get("dynamic_values"), list) else []:
            if not isinstance(item, dict):
                continue
            if str(item.get("name") or "").strip().lower() == field_name.strip().lower():
                return self._safe_int(item.get("current_value")) or 0
        return 0

    def _matched_reward_branch(self, branches: list[dict[str, Any]], archetype_tags: list[str], text: str) -> dict[str, Any] | None:
        for branch in branches:
            when_has = branch.get("when_has") if isinstance(branch.get("when_has"), list) else []
            if when_has and not any(str(token).strip().lower() in text for token in when_has):
                continue
            branch_archetypes = [str(tag).strip().lower() for tag in (branch.get("archetype_bias") if isinstance(branch.get("archetype_bias"), list) else []) if str(tag).strip()]
            if branch_archetypes and not any(tag in archetype_tags for tag in branch_archetypes):
                continue
            return branch
        return None

    def _infer_archetype_tags(self, deck: dict[str, Any]) -> list[str]:
        cards = deck.get("cards") if isinstance(deck.get("cards"), list) else []
        names = [str(card.get("name") or card.get("card_id") or "").lower() for card in cards if isinstance(card, dict)]
        tags: list[str] = []
        if any(keyword in name for name in names for keyword in ("orb", "zap", "coolheaded", "ball lightning", "电")):
            tags.append("orb_focus")
        if any(keyword in name for name in names for keyword in ("block", "defend", "charge battery", "防")):
            tags.append("defense")
        if any(keyword in name for name in names for keyword in ("draw", "skim", "coolheaded", "抽")):
            tags.append("draw")
        if any(keyword in name for name in names for keyword in ("energy", "turbo", "double energy", "能量")):
            tags.append("energy")
        return tags

    def _lethal_combat_operation(self, snapshot: dict[str, Any], actions: list[dict[str, Any]]) -> PlannedOperation | None:
        play_card = self._find_action(actions, "play_card")
        if play_card is None:
            return None
        raw_state = snapshot.get("raw_state") if isinstance(snapshot.get("raw_state"), dict) else {}
        combat = raw_state.get("combat") if isinstance(raw_state.get("combat"), dict) else {}
        hand = combat.get("hand") if isinstance(combat.get("hand"), list) else []
        enemies = combat.get("enemies") if isinstance(combat.get("enemies"), list) else []
        current_energy = self._safe_int(combat.get("player", {}).get("energy")) or 0
        playable_cards = [card for card in hand if isinstance(card, dict) and bool(card.get("playable"))]
        for enemy_index, enemy in enumerate(enemies):
            if not isinstance(enemy, dict):
                continue
            enemy_hp = self._safe_int(enemy.get("current_hp") if enemy.get("current_hp") is not None else enemy.get("hp")) or 0
            if enemy_hp <= 0:
                continue
            lethal_cards = self._find_lethal_card_sequence(playable_cards, current_energy, enemy_hp)
            if lethal_cards:
                first = lethal_cards[0]
                kwargs: dict[str, Any] = {"card_index": self._safe_int(first.get("index")) or 0}
                target_index = self._lethal_target_index(first, enemy_index)
                if target_index is not None:
                    kwargs["target_index"] = target_index
                return PlannedOperation(action_type="play_card", kwargs=kwargs, confidence=0.98, source="heuristic", reason="combat_lethal_sequence")
        return None

    def _find_lethal_card_sequence(self, cards: list[dict[str, Any]], current_energy: int, enemy_hp: int) -> list[dict[str, Any]]:
        ordered = sorted(cards, key=self._card_damage, reverse=True)
        chosen: list[dict[str, Any]] = []
        remaining_energy = current_energy
        remaining_hp = enemy_hp
        for card in ordered:
            cost = self._card_energy_cost(card)
            if cost > remaining_energy:
                continue
            damage = self._card_damage(card)
            if damage <= 0:
                continue
            chosen.append(card)
            remaining_energy -= cost
            remaining_hp -= damage
            if remaining_hp <= 0:
                return chosen
        return []

    def _card_damage(self, card: dict[str, Any]) -> int:
        for item in card.get("dynamic_values") if isinstance(card.get("dynamic_values"), list) else []:
            if not isinstance(item, dict):
                continue
            if str(item.get("name") or "").strip().lower() == "damage":
                parsed = self._safe_int(item.get("current_value"))
                if parsed is not None:
                    return parsed
        damage = self._safe_int(card.get("damage"))
        if damage is not None:
            return damage
        effect_text = " ".join(
            [
                str(card.get("resolved_rules_text") or ""),
                str(card.get("description") or ""),
                str(card.get("effect") or ""),
            ]
        )
        for token in effect_text.replace("Deal", " ").replace("damage", " ").split():
            parsed = self._safe_int(token)
            if parsed is not None:
                return parsed
        match = re.search(r"造成\s*(\d+)\s*点?伤害", effect_text)
        if match:
            return int(match.group(1))
        return 0

    def _card_energy_cost(self, card: dict[str, Any]) -> int:
        cost = self._safe_int(card.get("energy_cost"))
        if cost is not None:
            return max(0, cost)
        return 1

    def _lethal_target_index(self, card: dict[str, Any], fallback_target: int) -> int | None:
        valid_target_indices = card.get("valid_target_indices") if isinstance(card.get("valid_target_indices"), list) else []
        if not valid_target_indices:
            return None
        if fallback_target in valid_target_indices:
            return fallback_target
        try:
            return int(valid_target_indices[0])
        except Exception:
            return None

    def _incoming_attack_total(self, snapshot: dict[str, Any]) -> int:
        raw_state = snapshot.get("raw_state") if isinstance(snapshot.get("raw_state"), dict) else {}
        combat = raw_state.get("combat") if isinstance(raw_state.get("combat"), dict) else {}
        enemies = combat.get("enemies") if isinstance(combat.get("enemies"), list) else []
        total = 0
        for enemy in enemies:
            if not isinstance(enemy, dict):
                continue
            direct = self._safe_int(enemy.get("intent_damage"))
            if direct is not None:
                total += direct
                continue
            intents = enemy.get("intents") if isinstance(enemy.get("intents"), list) else []
            enemy_total = 0
            for intent in intents:
                if not isinstance(intent, dict):
                    continue
                total_damage = self._safe_int(intent.get("total_damage"))
                if total_damage is not None:
                    enemy_total += total_damage
                    continue
                damage = self._safe_int(intent.get("damage")) or 0
                hits = self._safe_int(intent.get("hits")) or 1
                enemy_total += damage * hits
            total += enemy_total
        return total

    def _current_block(self, snapshot: dict[str, Any]) -> int:
        raw_state = snapshot.get("raw_state") if isinstance(snapshot.get("raw_state"), dict) else {}
        combat = raw_state.get("combat") if isinstance(raw_state.get("combat"), dict) else {}
        player = combat.get("player") if isinstance(combat.get("player"), dict) else {}
        return self._safe_int(player.get("block")) or 0

    # ---- 战斗中用药水（规格 docs/shop_scoring.md 「八」）------------------------------------
    #
    # 玩家口径（2026-10-07）：「还有每次对战如果损血大于8就鼓励使用药水」。
    # 与商店那条是同一套思路（药水是"能换命"的资源，该用就用、但不乱用），只是触发点从
    # "前方有精英"换成"这一下要挨超过 8 点"。这里是**纯决策**，记账（同一回合只喝一瓶）由 service 做。

    def in_elite_or_boss_combat(self, snapshot: dict[str, Any] | None = None) -> bool:
        """这一场是不是精英/Boss 战（§八节流：普通小怪战更省、精英/Boss 更愿意喝）。

        唯一可用的线索是"上一次选路选了什么节点"（`_note_route_choice`）：mod 的
        `combat.enemies[]` **没有** elite/boss 标记，实测层面分不出来。所以拿不到线索就返回 False，
        也就是**按玩家的原始口径（8 点）**走，绝不自作主张把阈值调高。
        """
        return self._last_route_node_type in {"elite", "boss"}

    def _combat_potion_threshold(self, *, elite_or_boss: bool | None = None) -> float:
        """这一场用的是哪条触发线。

        默认就是玩家说的 8 点。已经**明确知道**是普通小怪战时按
        `COMBAT_POTION_TRASH_THRESHOLD_MULTIPLIER`（1.25 → 10 点）略微收紧一点 —— 小怪战把药水喝光
        不划算；精英/Boss 战与"不知道是哪一场"都保持 8 点，不会把玩家的 8 点线悄悄抬高。
        """
        threshold = COMBAT_POTION_HP_LOSS_THRESHOLD
        if elite_or_boss is None:
            elite_or_boss = self.in_elite_or_boss_combat()
        if not elite_or_boss and self._last_route_node_type == "monster":
            threshold *= COMBAT_POTION_TRASH_THRESHOLD_MULTIPLIER
        return threshold

    def _combat_expected_damage(self, snapshot: dict[str, Any]) -> tuple[int | None, int]:
        """这一下要挨多少：`Σ intents[].total_damage − player.block`（不够减就取 0）。

        返回 `(预计损血, 意图总伤害)`；**拿不到意图数据时返回 `(None, 0)`** —— 玩家口径是
        "宁可不喝，也不乱灌"，所以读不到意图就不触发这条规则（`intents[]` 整个缺失、
        或每一条都没有伤害字段时都算"读不到"；意图是"给自己上 buff"这种非攻击则算读到了伤害 0）。
        """
        raw_state = snapshot.get("raw_state") if isinstance(snapshot.get("raw_state"), dict) else {}
        combat = raw_state.get("combat") if isinstance(raw_state.get("combat"), dict) else {}
        enemies = combat.get("enemies") if isinstance(combat.get("enemies"), list) else []
        alive = [enemy for enemy in enemies if isinstance(enemy, dict) and enemy.get("is_alive", True)]
        if not alive:
            return None, 0
        incoming = 0
        known = False
        for enemy in alive:
            direct = self._safe_int(enemy.get("intent_damage"))
            if direct is not None:
                incoming += direct
                known = True
                continue
            intents = enemy.get("intents")
            if not isinstance(intents, list):
                continue
            for intent in intents:
                if not isinstance(intent, dict):
                    continue
                total = self._safe_int(intent.get("total_damage"))
                if total is not None:
                    incoming += total
                    known = True
                    continue
                damage = self._safe_int(intent.get("damage"))
                if damage is not None:
                    incoming += damage * (self._safe_int(intent.get("hits")) or 1)
                    known = True
        if not known:
            return None, 0
        return max(0, incoming - self._current_block(snapshot)), incoming

    def _current_hp(self, snapshot: dict[str, Any]) -> int:
        raw_state = snapshot.get("raw_state") if isinstance(snapshot.get("raw_state"), dict) else {}
        combat = raw_state.get("combat") if isinstance(raw_state.get("combat"), dict) else {}
        player = combat.get("player") if isinstance(combat.get("player"), dict) else {}
        run = raw_state.get("run") if isinstance(raw_state.get("run"), dict) else {}
        hp = self._safe_int(player.get("current_hp"))
        if hp is None:
            hp = self._safe_int(run.get("current_hp"))
        return hp or 0

    def _current_max_hp(self, snapshot: dict[str, Any]) -> int:
        raw_state = snapshot.get("raw_state") if isinstance(snapshot.get("raw_state"), dict) else {}
        combat = raw_state.get("combat") if isinstance(raw_state.get("combat"), dict) else {}
        player = combat.get("player") if isinstance(combat.get("player"), dict) else {}
        run = raw_state.get("run") if isinstance(raw_state.get("run"), dict) else {}
        max_hp = self._safe_int(player.get("max_hp"))
        if not max_hp:
            max_hp = self._safe_int(run.get("max_hp"))
        return max_hp or 0

    def _potion_target_index(self, potion: dict[str, Any], snapshot: dict[str, Any]) -> int | None:
        """有指向的药水要指谁（`requires_target=true` 时 `valid_target_indices` 必须非空）。

        伤害类挑**血最少**的那只敌人（能秒就秒，少挨几轮）；其余（指向队友/自己）取第一个合法值。
        """
        if not bool(potion.get("requires_target")):
            return None
        valid: list[int] = []
        for item in potion.get("valid_target_indices") if isinstance(potion.get("valid_target_indices"), list) else []:
            parsed = self._safe_int(item)
            if parsed is not None:
                valid.append(parsed)
        if not valid:
            return None
        if str(potion.get("target_index_space") or "") != "enemies":
            return valid[0]
        raw_state = snapshot.get("raw_state") if isinstance(snapshot.get("raw_state"), dict) else {}
        combat = raw_state.get("combat") if isinstance(raw_state.get("combat"), dict) else {}
        enemies = combat.get("enemies") if isinstance(combat.get("enemies"), list) else []

        def enemy_hp(index: int) -> int:
            if 0 <= index < len(enemies) and isinstance(enemies[index], dict):
                return self._safe_int(enemies[index].get("current_hp") if enemies[index].get("current_hp") is not None else enemies[index].get("hp")) or 0
            return 1 << 30

        return min(valid, key=enemy_hp)

    @staticmethod
    def _combat_potion_score(category: str, *, lethal: bool, hp_ratio: float) -> float:
        """一瓶药水"能不能把这 8 点以上损血压下去"的评分（规格「用哪一瓶」的 1~5 条）。

        1. 减伤/闪避/格挡 → 最高；2. 群伤/清场 → 次高；3. 回血 → 只在"预计会死"或血量 < 30% 时才算数
        （**回血不减少这一下的伤害**，所以平时是 0 = 不因这条规则触发）；4. 清状态 → 与减伤同档的备选；
        5. 抽牌等 → 0（战斗里不当保命手段）。
        """
        if category == "heal":
            if lethal:
                return COMBAT_POTION_LETHAL_HEAL_SCORE
            if hp_ratio < COMBAT_POTION_LOW_HP_RATIO:
                return COMBAT_POTION_LOW_HP_HEAL_SCORE
            return 0.0
        return float(COMBAT_POTION_SCORES.get(category, 0.0))

    def decide_combat_potion(
        self,
        snapshot: dict[str, Any],
        *,
        already_used_this_turn: bool = False,
        elite_or_boss: bool | None = None,
        threshold: float | None = None,
    ) -> dict[str, Any]:
        """战斗中要不要喝药水、喝哪一瓶（规格「八」）。**永远返回 dict，不抛异常。**

        判据：
        - 预计损血 = `Σ intents[].total_damage − player.block`，`> 8` 触发（精英/Boss 战与"不知道"
          都按 8；已知普通小怪战按 10）；
        - **保命优先**：`current_hp − 预计损血 <= 0` → 无条件喝（这条压过"没到线"和"用哪一瓶"）；
        - 同一回合只喝一瓶（`already_used_this_turn` 由 service 用 runtime_state 记账传进来）；
        - 意图数据缺失 → 不触发。

        返回字段：`use` / `option_index` / `target_index` / `name` / `category` / `score` /
        `incoming` / `expected_loss` / `lethal` / `log`（`log` 是一句给人看的中文，service 加前缀
        `[sts2_potion] ` 打出去）。
        """
        if threshold is None:
            threshold = self._combat_potion_threshold(elite_or_boss=elite_or_boss)
        decision: dict[str, Any] = {
            "use": False,
            "option_index": None,
            "target_index": None,
            "name": "",
            "category": "",
            "score": 0.0,
            "incoming": 0,
            "expected_loss": 0,
            "lethal": False,
            "already_used_this_turn": bool(already_used_this_turn),
            "threshold": threshold,
            "log": "",
        }
        expected, incoming = self._combat_expected_damage(snapshot)
        if expected is None:
            decision["log"] = "拿不到意图数据（intents[] 缺失/没有伤害字段）→ 不触发用药水"
            return decision
        decision["incoming"] = incoming
        decision["expected_loss"] = expected
        current_hp = self._current_hp(snapshot)
        max_hp = self._current_max_hp(snapshot)
        hp_ratio = (current_hp / max_hp) if max_hp > 0 else 1.0
        lethal = current_hp > 0 and (current_hp - expected) <= 0
        decision["lethal"] = lethal

        if already_used_this_turn:
            decision["log"] = f"预计损血={expected} 但不喝（原因：本回合已经喝过一瓶）"
            return decision
        if not lethal and expected <= threshold:
            decision["log"] = f"预计损血={expected} ≤ {threshold:g} → 不喝（没到用药水的线）"
            return decision

        raw_state = snapshot.get("raw_state") if isinstance(snapshot.get("raw_state"), dict) else {}
        run = raw_state.get("run") if isinstance(raw_state.get("run"), dict) else {}
        potions = run.get("potions") if isinstance(run.get("potions"), list) else []
        candidates: list[tuple[float, int, dict[str, Any], str, int | None]] = []
        for position, potion in enumerate(potions):
            if not isinstance(potion, dict) or not potion.get("potion_id"):
                continue
            if not potion.get("can_use"):
                continue
            target_index = self._potion_target_index(potion, snapshot)
            if bool(potion.get("requires_target")) and target_index is None:
                # 需要目标却没有合法目标：这一瓶现在按不下去，跳过。
                continue
            index = self._safe_int(potion.get("index"))
            if index is None:
                index = position
            category = self._potion_category(potion)
            score = self._combat_potion_score(category, lethal=lethal, hp_ratio=hp_ratio)
            candidates.append((score, index, potion, category, target_index))
        if not candidates:
            decision["log"] = f"预计损血={expected} 但不喝（原因：手里没有可用的药水）"
            return decision
        candidates.sort(key=lambda item: (-item[0], item[1]))
        score, index, potion, category, target_index = candidates[0]
        name = str(potion.get("name") or potion.get("potion_id") or "药水")
        label = _POTION_CATEGORY_LABELS.get(category, category)
        if score <= 0 and not lethal:
            decision["log"] = f"预计损血={expected} 但不喝（原因：手里只有{label}类，不当保命手段）"
            return decision
        decision.update(
            use=True,
            option_index=index,
            target_index=target_index,
            name=name,
            category=category,
            score=score,
        )
        if lethal:
            # 保命优先：会死就喝，没到线也喝（规格「八、判据」：「这条永远压过其它考虑」）。
            # 只剩抽牌类时评分是 0，但仍然喝 —— 它排在所有保命类后面，所以有更好的就先喝更好的。
            qualifier = "无条件用" if expected <= threshold else "用"
            decision["log"] = f"预计损血={expected} 会致命（血量 {current_hp}）→ {qualifier}〈{name}〉({label}, 评分 {score:.0f})"
        else:
            decision["log"] = f"预计损血={expected} > {threshold:g} → 用〈{name}〉({label}, 评分 {score:.0f})"
        return decision

    def _first_record_option(self, preferences: dict[str, Any]) -> int | None:
        records = preferences.get("records") if isinstance(preferences.get("records"), list) else []
        for record in records:
            if not isinstance(record, dict):
                continue
            value = record.get("value") if isinstance(record.get("value"), dict) else {}
            preferred = value.get("preferred_option_index")
            normalized = self._safe_int(preferred)
            if normalized is not None:
                return normalized
        return None

    def _safe_int(self, value: Any) -> int | None:
        try:
            if value is None:
                return None
            return int(value)
        except Exception:
            return None

    # ---- 游玩模式（扰动）用的候选排序 ------------------------------------------------------
    #
    # 这里只做两件事：把"该屏有哪些**合法**下标"枚举出来，以及"按**既有评分**把这些下标排好序"。
    # 评分一律复用各屏原来的 _score_* 函数，不另写一套口径——排序的第 0 个必须就是规划器原本会选的
    # 那一个，否则"不扰动"就不等于改动前的行为。
    #
    # 扰动层（service.run_autoplay_step）只在非 serious 档才调用这些方法：serious 档连排序都不算，
    # 保证默认档零影响。

    #: 下标键：只有 play_card 用 card_index，其余选项类动作都是 option_index。
    _INDEX_KEY_BY_ACTION = {"play_card": "card_index"}
    _DEFAULT_INDEX_KEY = "option_index"

    #: 任一为 False 就认为该项不可选（宁可少给候选，也不能扰动出一个非法动作）。
    _UNSELECTABLE_FLAGS = ("playable", "enabled", "is_enabled", "affordable", "can_afford")

    #: 用 _score_remove_card 排名的选牌屏（"移除/删牌"）。其它选牌屏（升级/变换/附魔）没有对应评分，
    #: 走通用枚举。
    _REMOVE_SELECTION_SCREENS = {"card_selection_remove", "card_selection_delet"}

    @classmethod
    def index_key_for(cls, action_type: Any) -> str:
        """该动作类型用哪个 kwargs 键承载"选第几个"。"""
        return cls._INDEX_KEY_BY_ACTION.get(str(action_type or "").strip().lower(), cls._DEFAULT_INDEX_KEY)

    @classmethod
    def _item_blocked(cls, item: dict[str, Any]) -> bool:
        return any(flag in item and item.get(flag) is False for flag in cls._UNSELECTABLE_FLAGS)

    @classmethod
    def _item_indices(cls, items: Any) -> list[int]:
        """一列选项 dict → 下标列表（优先用 item['index']，缺省按位置）。不可选的排除掉。"""
        result: list[int] = []
        if not isinstance(items, list):
            return result
        for position, item in enumerate(items):
            if not isinstance(item, dict) or cls._item_blocked(item):
                continue
            raw = item.get("index")
            result.append(int(raw) if isinstance(raw, int) and not isinstance(raw, bool) else position)
        return result

    @staticmethod
    def _operation_dict(operation: Any) -> dict[str, Any]:
        if isinstance(operation, dict):
            return operation
        if hasattr(operation, "as_dict"):
            try:
                return dict(operation.as_dict())
            except Exception:
                return {}
        return {}

    @staticmethod
    def _context_parts(context: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
        summary_context = context.get("summary_context") if isinstance(context.get("summary_context"), dict) else {}
        strategy_context = context.get("strategy_context") if isinstance(context.get("strategy_context"), dict) else {}
        snapshot = context.get("snapshot") if isinstance(context.get("snapshot"), dict) else {}
        return summary_context, strategy_context, snapshot

    def legal_option_indices(self, context: dict[str, Any], operation: Any) -> list[int]:
        """当前操作所在屏的**合法**下标集合；枚举不出就返回空列表（= 不扰动）。"""
        action_type = str(self._operation_dict(operation).get("action_type") or "").strip().lower()
        summary_context, _strategy_context, snapshot = self._context_parts(context)
        payload = summary_context.get("payload") if isinstance(summary_context.get("payload"), dict) else {}
        raw_state = snapshot.get("raw_state") if isinstance(snapshot.get("raw_state"), dict) else {}

        if action_type == "choose_event_option":
            return self._item_indices(payload.get("event_options"))
        if action_type == "choose_map_node":
            nodes = payload.get("travelable_nodes")
            if not isinstance(nodes, list) or not nodes:
                nodes = payload.get("available_nodes")
            return self._item_indices(nodes)
        if action_type == "choose_reward_card":
            return self._item_indices(self._reward_candidates(summary_context))
        if action_type == "select_deck_card":
            return self._item_indices(payload.get("selection_cards"))
        if action_type == "choose_rest_option":
            return self._item_indices(payload.get("rest_options"))
        if action_type == "buy_card":
            return self._item_indices(payload.get("shop_cards"))
        if action_type == "buy_relic":
            return self._item_indices(payload.get("shop_relics"))
        if action_type == "buy_potion":
            return self._item_indices(payload.get("shop_potions"))
        if action_type == "play_card":
            return [index for index, _card in self._playable_hand(context)]
        if action_type == "choose_treasure_relic":
            chest = raw_state.get("chest") if isinstance(raw_state.get("chest"), dict) else {}
            return self._item_indices(chest.get("relic_options"))
        if action_type == "choose_bundle":
            return self._item_indices(raw_state.get("bundles"))
        if action_type == "choose_capstone_option":
            # capstone 选项在 payload 里没有列表，只有按钮。规划器自己的游标正是扫
            # 0.._MAX_CAPSTONE_SWEEP-1（见 _next_capstone_option），所以这个范围就是它会去碰的下标集合。
            return list(range(self._MAX_CAPSTONE_SWEEP))
        # claim_reward / proceed / confirm_* / end_turn 等没有"选第几个"，不扰动。
        return []

    def _playable_hand(self, context: dict[str, Any]) -> list[tuple[int, dict[str, Any]]]:
        _summary_context, _strategy_context, snapshot = self._context_parts(context)
        raw_state = snapshot.get("raw_state") if isinstance(snapshot.get("raw_state"), dict) else {}
        combat = raw_state.get("combat") if isinstance(raw_state.get("combat"), dict) else {}
        hand = combat.get("hand") if isinstance(combat.get("hand"), list) else []
        result: list[tuple[int, dict[str, Any]]] = []
        for position, card in enumerate(hand):
            if not isinstance(card, dict) or not bool(card.get("playable")):
                continue
            raw = card.get("index")
            index = int(raw) if isinstance(raw, int) and not isinstance(raw, bool) else position
            result.append((index, card))
        return result

    def _ranked_option_indices(self, context: dict[str, Any], operation: Any) -> list[int]:
        """按各屏**既有评分**给出的"从好到坏"顺序；没有评分可用的屏返回空列表。"""
        action_type = str(self._operation_dict(operation).get("action_type") or "").strip().lower()
        summary_context, strategy_context, _snapshot = self._context_parts(context)
        preferences = strategy_context.get("preferences") if isinstance(strategy_context.get("preferences"), dict) else {}
        classification = context.get("classification") if isinstance(context.get("classification"), dict) else {}
        state_name = str(classification.get("state_name") or "").strip().lower()

        if action_type == "choose_event_option":
            return self._rank_event_options(summary_context)
        if action_type == "choose_map_node":
            return self._rank_map_nodes(summary_context, preferences)
        if action_type == "choose_reward_card":
            return self._rank_reward_cards(summary_context, preferences)
        if action_type == "select_deck_card" and state_name in self._REMOVE_SELECTION_SCREENS:
            return self._rank_remove_options(summary_context)
        return []

    def _rank_map_nodes(self, summary_context: dict[str, Any], preferences: dict[str, Any]) -> list[int]:
        """与 _preferred_map_index 同口径：分数高的在前，同分时下标小的在前。"""
        payload = summary_context.get("payload") if isinstance(summary_context.get("payload"), dict) else {}
        nodes = payload.get("travelable_nodes")
        if not isinstance(nodes, list) or not nodes:
            nodes = payload.get("available_nodes")
        nodes = nodes if isinstance(nodes, list) else []
        current_hp = self._safe_int(payload.get("current_hp")) or 0
        max_hp = self._safe_int(payload.get("max_hp")) or 0
        gold = self._safe_int(payload.get("gold")) or 0
        hp_ratio = (current_hp / max_hp) if max_hp > 0 else 0.0
        route_policy = self._route_policy(preferences)

        scored: list[tuple[int, int]] = []
        for position, node in enumerate(nodes):
            if not isinstance(node, dict):
                continue
            raw = node.get("index")
            index = int(raw) if isinstance(raw, int) and not isinstance(raw, bool) else position
            scored.append((self._score_map_node(node, route_policy, hp_ratio, gold), index))
        scored.sort(key=lambda item: (-item[0], item[1]))
        return [index for _score, index in scored]

    def _rank_reward_cards(self, summary_context: dict[str, Any], preferences: dict[str, Any]) -> list[int]:
        """与 _preferred_reward_option 同口径（含 guidance 调整）：分数高的在前，同分时下标小的在前。"""
        payload = summary_context.get("payload") if isinstance(summary_context.get("payload"), dict) else {}
        reward_cards = self._reward_candidates(summary_context)
        deck = payload.get("deck") if isinstance(payload.get("deck"), dict) else {}
        archetype_tags = [str(tag).strip().lower() for tag in self._infer_archetype_tags(deck)] if isinstance(deck, dict) else []
        records = preferences.get("records") if isinstance(preferences.get("records"), list) else []
        deck_policy = self._reward_deck_policy(records)
        guidance_override = self._guidance_override_from_payload(summary_context)

        scored: list[tuple[int, int]] = []
        for position, card in enumerate(reward_cards if isinstance(reward_cards, list) else []):
            if not isinstance(card, dict):
                continue
            score = self._score_reward_card(card, deck_policy, archetype_tags)
            if guidance_override.get("prefer_defense"):
                if self._card_block(card) > 0:
                    score += 18
                if self._reward_dynamic_value(card, "HpLoss") > 0:
                    score -= 12
            if guidance_override.get("prefer_attack"):
                if self._card_damage(card) > 0:
                    score += 14
            raw = card.get("index")
            index = int(raw) if isinstance(raw, int) and not isinstance(raw, bool) else position
            scored.append((score, index))
        scored.sort(key=lambda item: (-item[0], item[1]))
        return [index for _score, index in scored]

    def _rank_remove_options(self, summary_context: dict[str, Any]) -> list[int]:
        """与 _preferred_remove_option 同口径：分数高的在前，同分时下标**大**的在前。"""
        payload = summary_context.get("payload") if isinstance(summary_context.get("payload"), dict) else {}
        selection_cards = payload.get("selection_cards") if isinstance(payload.get("selection_cards"), list) else []
        scored: list[tuple[int, int]] = []
        for position, card in enumerate(selection_cards):
            if not isinstance(card, dict) or self._is_unremovable_card(card):
                continue
            raw = card.get("index")
            index = int(raw) if isinstance(raw, int) and not isinstance(raw, bool) else position
            scored.append((self._score_remove_card(card), index))
        scored.sort(key=lambda item: (-item[0], -item[1]))
        return [index for _score, index in scored]

    def ordered_candidates(self, context: dict[str, Any], operation: Any) -> list[int]:
        """给扰动层的候选列表，**最优在前**。

        * 第 0 个一定就是操作自己选的那个下标——所以"不扰动"== 取第 0 个 == 改动前的行为；
        * 其余候选一律取自该屏的**合法**下标集合；
        * 拿不到任何合法替代时只返回 ``[最优]``，扰动层据此放弃（永远退得回最优）；
        * 操作本身不带下标（claim_reward / proceed 之类）时返回空列表。
        """
        planned = self._operation_dict(operation)
        action_type = str(planned.get("action_type") or "").strip().lower()
        kwargs = planned.get("kwargs") if isinstance(planned.get("kwargs"), dict) else {}
        best = self._safe_int(kwargs.get(self.index_key_for(action_type)))
        if best is None:
            return []

        legal = self.legal_option_indices(context, operation)
        ranked = self._ranked_option_indices(context, operation)
        if ranked and legal:
            legal_set = set(legal)
            pool = [index for index in ranked if index in legal_set]
        else:
            pool = list(ranked) if ranked else list(legal)
        return [best] + [index for index in pool if index != best]

    def build_perturbed_operation(self, context: dict[str, Any], operation: Any, candidate: int) -> dict[str, Any] | None:
        """把操作的下标换成 ``candidate``，并补齐随之变化的附属参数。

        战斗卡牌的目标**必须跟着新卡走**：沿用旧卡的目标可能超出新卡的 ``valid_target_indices``，
        那就是一个非法动作。换不出合法操作时返回 ``None``，调用方放弃扰动、退回最优。
        """
        planned = self._operation_dict(operation)
        action_type = str(planned.get("action_type") or "").strip().lower()
        kwargs = planned.get("kwargs") if isinstance(planned.get("kwargs"), dict) else {}
        key = self.index_key_for(action_type)
        best = self._safe_int(kwargs.get(key))
        if best is None or candidate == best:
            return None

        new_kwargs = dict(kwargs)
        new_kwargs[key] = candidate
        if action_type == "play_card":
            _summary_context, _strategy_context, snapshot = self._context_parts(context)
            raw_state = snapshot.get("raw_state") if isinstance(snapshot.get("raw_state"), dict) else {}
            combat = raw_state.get("combat") if isinstance(raw_state.get("combat"), dict) else {}
            hand = combat.get("hand") if isinstance(combat.get("hand"), list) else []
            enemies = combat.get("enemies") if isinstance(combat.get("enemies"), list) else []
            card = next(
                (item for item in hand if isinstance(item, dict) and self._safe_int(item.get("index")) == candidate),
                None,
            )
            if card is None or not bool(card.get("playable")):
                return None
            new_kwargs.pop("target_index", None)
            target_index = self._preferred_target_for_card(card, enemies)
            if target_index is not None:
                new_kwargs["target_index"] = target_index

        perturbed = dict(planned)
        perturbed["kwargs"] = new_kwargs
        return perturbed


__all__ = ["STS2HeuristicPlanner"]
