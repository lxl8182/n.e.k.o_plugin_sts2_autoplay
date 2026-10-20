"""★死亡归因契约测试（`death_attribution.py`）。

## 为什么需要这一组

玩家的口径：「教训是需要反思整个链路的……遇到群怪就死就反思有没有拿 aoe 卡，
遇到 boss 和精英就死需要反思选卡的数值和强化卡选择」。
**死亡归因是其余五维的触发器** —— 先知道"死在什么上"，才知道该反思哪一维。

而改造前 `catgirl_memory._heuristic_lessons` **只读 `decision_memory` 的 reason 文本**，
从不读牌组、不读分层损血、不读敌人类型 ⇒
50 局真实记忆里三条死亡教训**全是同一句兜底句**，抽不出任何 token。

## 本文件钉住的三件事

1. **非战斗房必须被滤掉** —— 否则 `room:MAP`（实测 15435 血）会成为"最大死因"
2. **死因判据与真实数据形态匹配** —— 含 Boss 误判修复、群怪"合计占比"判据
3. **证据不足时不编造** —— `UNKNOWN` 必须不产教训
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_PLUGIN_ROOT = Path(__file__).resolve().parents[1]
if str(_PLUGIN_ROOT.parent.parent.parent) not in sys.path:
    sys.path.insert(0, str(_PLUGIN_ROOT.parent.parent.parent))

from plugin.plugins.sts2_autoplay.death_attribution import (  # noqa: E402
    DEATH_KIND_AOE,
    DEATH_KIND_ATTRITION,
    DEATH_KIND_BOSS,
    DEATH_KIND_BURST,
    DEATH_KIND_ELITE,
    DEATH_KIND_UNKNOWN,
    attribution_to_lessons,
    build_death_attribution,
    classify_death,
    is_combat_room,
)


# --------------------------------------------------------------------------------------
# 1. 非战斗房过滤（★实测陷阱）
# --------------------------------------------------------------------------------------
@pytest.mark.parametrize(
    "room,expected",
    [
        # `service._room_identity` 无 id 时的占位兜底
        ("room:MAP", False),
        ("room:REST", False),
        ("room:SHOP", False),
        ("room:CHEST", False),
        # 已知非战斗事件
        ("NEOW", False),
        ("", False),
        # 真敌人
        ("FUZZY_WURM_CRAWLER", True),
        ("VANTOM", True),
        # 群怪（`+` 连接）
        ("CALCIFIED_CULTIST+SEAPUNK", True),
        ("LEAF_SLIME_M+LEAF_SLIME_S", True),
    ],
)
def test_non_combat_rooms_are_filtered(room: str, expected: bool) -> None:
    """★非战斗房必须被滤掉。

    实测（50 局真实记忆）跨局"累计损血最高"排名：

        room:MAP            15435 血   ← 若不滤，会成为"最大死因"
        NEOW                 6505
        room:REST            5958
        room:SHOP            2817
        FUZZY_WURM_CRAWLER   1843      ← 真正的头号敌人

    不滤会得出「最该防的是地图和商店」这种荒谬结论。
    """
    assert is_combat_room(room) is expected


def test_non_combat_pollution_would_change_the_verdict() -> None:
    """★把"过滤真的有用"做成可证伪断言，而不是只写在注释里。

    构造一局：非战斗房损血远大于战斗房。若不过滤，
    最重房会是 `room:MAP`，判据会得出错误的死因。
    """
    run = {
        "outcome": "defeat",
        "floor": 10,
        "route": [
            {"room": "room:MAP", "floor": 3, "hp_lost": 999},      # 非战斗，应被滤
            {"room": "room:REST", "floor": 5, "hp_lost": 500},     # 非战斗，应被滤
            {"room": "FUZZY_WURM_CRAWLER", "floor": 7, "hp_lost": 30},
            {"room": "NIBBIT", "floor": 8, "hp_lost": 20},
            {"room": "SEAPUNK", "floor": 9, "hp_lost": 25},
        ],
    }
    attribution = build_death_attribution(run)
    assert attribution["worst_room"] == "FUZZY_WURM_CRAWLER", (
        f"最重房必须是真敌人，实际 {attribution['worst_room']} —— 非战斗房没被滤掉"
    )
    assert attribution["combat_lost"] == 75, (
        f"战斗损血应只算三场真战斗(30+20+25=75)，实际 {attribution['combat_lost']}"
    )


# --------------------------------------------------------------------------------------
# 2. 死因判据
# --------------------------------------------------------------------------------------
def _deaths(*pairs):
    return [{"room": room, "lost": lost} for room, lost in pairs]


def test_boss_is_recognized_even_without_boss_in_name() -> None:
    """★Boss 必须按 **id** 识别，不能只靠名字里含 "boss"。

    实测教训：`VANTOM` 出现在真实记忆里（玩家点名「死在Vantom手下」），
    但房名**不含** "boss" 字样 ⇒ 只靠 hint 会把它误判成精英
    （改造中确实误判过一次：floor=17 死于 Vantom 的一局被归成 `elite`）。
    """
    kind = classify_death(
        deaths=_deaths(("VANTOM", 200), ("NIBBIT", 20)),
        total_floors=17,
        lost_total=220,
    )
    assert kind == DEATH_KIND_BOSS, f"VANTOM 是 Boss，实际判成 {kind}"


def test_boss_takes_priority_over_attrition() -> None:
    """Boss 是**具体身份**证据，必须优先于"跨多房"这类统计判据。"""
    kind = classify_death(
        deaths=_deaths(("VANTOM", 100), ("NIBBIT", 60), ("SEAPUNK", 50), ("TOADPOLE", 40)),
        total_floors=17,
        lost_total=250,
    )
    assert kind == DEATH_KIND_BOSS, (
        f"多房且有 Boss 时应判 Boss（具体身份优先），实际 {kind}"
    )


def test_elite_is_recognized_by_known_id() -> None:
    """精英敌人在游戏里的 `enemy_id` 常**不含** "elite" ⇒ 必须靠白名单。"""
    for room in ("MAWLER", "VINE_SHAMBLER", "BYRDONIS"):
        kind = classify_death(
            deaths=_deaths((room, 100), ("NIBBIT", 10)),
            total_floors=14,
            lost_total=110,
        )
        assert kind == DEATH_KIND_ELITE, f"{room} 应判精英，实际 {kind}"


def test_multi_enemy_uses_aggregate_share_not_single_room() -> None:
    """★群怪判据用**合计占比**，不是"最重那一间"。

    实测驱动：真实 50 局里群怪房**很常见但不占主导** —— 典型分布：

        VINE_SHAMBLER                615  (18.9%)   ← 单敌，最重
        EYE_WITH_TEETH+FOGMOG        230  ( 7.1%)   ★群怪
        LEAF_SLIME_M+LEAF_SLIME_S    189  ( 5.8%)   ★群怪
        FLYCONID+SNAPPING_JAXFRUIT   165  ( 5.1%)   ★群怪
        ─────────────────────────────────────────
        群怪合计                      584+ (≈18%)

    单看"最重那间"永远选不到群怪（它们各自都小），
    但**合计**过半说明这一局确实主要被多敌消耗。

    ★★ fixture 的关键设计（这里踩过一个坑）：
      **最重的那间必须是单敌**，且群怪**各自都更小**。
      我第一版写成"3 个群怪房各 30 + 1 个单敌 20" ——
      `max()` 在并列时取第一个，恰好取到群怪房，
      于是"只看最重那间"也返回 AOE ⇒ **变异逃逸**（变异测试实测发现）。

      ⇒ 必须让"最重那间"与"群怪"是**不同的房**，断言才有区分力。
    """
    kind = classify_death(
        deaths=_deaths(
            ("BIG_SINGLE_FOE", 40),   # ← 最重，但是**单敌**
            ("A+ARROW", 25),          # ← 群怪，各自更小
            ("B+BEAR", 25),
            ("C+CAT", 20),
        ),
        total_floors=12,
        lost_total=110,
    )
    assert kind == DEATH_KIND_AOE, (
        f"群怪合计 70/110 = 64%（且最重那间是单敌）应判 AOE，实际 {kind}。"
        "若判成别的，说明用的是『最重那一间』而不是『合计』。"
    )


def test_single_room_burst_when_dominant() -> None:
    """单房被快速打空（占比 ≥60%）⇒ 短轴爆发缺口。

    ⚠️ fixture 刻意**不用** `TERROR_EEL` 这类精英名 ——
       精英检查在 burst 之前（具体身份优先），用精英名会先被判成 elite。
       （我第一版正是这么写的，跑出 `elite != burst`，是**测试挑错了名字**，不是代码错。）
    """
    kind = classify_death(
        deaths=_deaths(("SOME_BIG_FOE", 80), ("SINGLE", 30), ("OTHER", 10)),
        total_floors=14,
        lost_total=120,
    )
    assert kind == DEATH_KIND_BURST, f"单房 80/120=67% 应判 burst，实际 {kind}"


def test_elite_priority_over_burst() -> None:
    """★精英/Boss 的**具体身份**优先于"单房占比"这个统计判据。

    这也是实测修正的一部分：真实数据里 `TERROR_EEL` 单房占 64%，
    但它**是精英** ⇒ 该反思"数值与强化"，而不是笼统的"短轴爆发"。
    """
    kind = classify_death(
        deaths=_deaths(("TERROR_EEL", 80), ("SINGLE", 30), ("OTHER", 10)),
        total_floors=14,
        lost_total=120,
    )
    assert kind == DEATH_KIND_ELITE, (
        f"TERROR_EEL 是精英且占 67%，应优先判 elite，实际 {kind}"
    )


def test_attrition_needs_no_floor_gate() -> None:
    """★长轴判据**不看层数**（原设 `floor>=8` 会让 10 局早期死亡全落 UNKNOWN）。

    实测：死在 5~7 层、跨 5~8 房、最重房只占 20~44% ——
    形态与"长轴被磨死"**完全一致**，只是磨得更快。
    死在低层不代表"不是被磨死的"，只代表**前期输出/防御浓度都不够**。
    """
    kind = classify_death(
        deaths=_deaths(("A", 40), ("B", 35), ("C", 30), ("D", 25)),
        total_floors=6,  # ← 低层也必须能判
        lost_total=130,
    )
    assert kind == DEATH_KIND_ATTRITION, (
        f"跨 4 房、无单一主因（最重仅 31%）应判 attrition，实际 {kind}"
    )


def test_two_rooms_split_is_unknown_not_attrition() -> None:
    """★两房 50/50 ⇒ **UNKNOWN**（不是 attrition，也不是 burst）。

    这是**刻意的保守**：
      * `worst_share = 0.5 < 0.6` ⇒ 不是"单房被打空"
      * `len(per_room) = 2 < 3`  ⇒ 不够"跨多房"的证据量

    ⇒ 两个判据都不满足 ⇒ **承认判不出来**，交给上层用兜底句。
      这比硬塞进某一类更诚实（与全局"证据不足就别编"一致）。

    ★ 与真实数据形态一致：实测 50 局里 rooms=2 的 3 局
      都是某一房主导（判 burst/elite），**没有** 50/50 的自然样本 ——
      本测试覆盖的是**构造边界**，不是真实分布。
    """
    kind = classify_death(
        deaths=_deaths(("A", 50), ("B", 50)),
        total_floors=8,
        lost_total=100,
    )
    assert kind == DEATH_KIND_UNKNOWN, (
        f"两房各半证据不足，应判 unknown（不编造），实际 {kind}"
    )


def test_no_deaths_is_unknown() -> None:
    """**证据不足 ⇒ 不编造。**"""
    assert classify_death(deaths=[], total_floors=5, lost_total=0) == DEATH_KIND_UNKNOWN


# --------------------------------------------------------------------------------------
# 3. 教训产出：只在输、且只在有证据时
# --------------------------------------------------------------------------------------
def test_unknown_produces_no_lesson() -> None:
    """★`UNKNOWN` 必须**不产教训** —— 证据不足就别编。

    这与全局口径一致（"宁可没影响，也不要用模糊匹配乱改决策"）：
    生产不出教训时，上层会继续用兜底句，而不是被一条瞎猜的教训带偏。
    """
    assert attribution_to_lessons({"kind": DEATH_KIND_UNKNOWN, "outcome": "defeat"}) == []


def test_victory_produces_no_lesson() -> None:
    """赢了不该产"别这样"的教训。"""
    assert attribution_to_lessons({"kind": DEATH_KIND_ELITE, "outcome": "victory"}) == []


def test_defeat_produces_dimension_specific_lesson() -> None:
    """★产出必须指向**维度**，不是"下次选防御节点"这类空话。

    玩家原话：「不能单纯的说"这局优先选防御/回血节点"，需要多维」。
    """
    for kind, keyword in (
        (DEATH_KIND_ELITE, "精英"),
        (DEATH_KIND_AOE, "群伤"),
        (DEATH_KIND_ATTRITION, "格挡浓度"),
        (DEATH_KIND_BURST, "格挡"),
    ):
        lessons = attribution_to_lessons({"kind": kind, "outcome": "defeat"})
        assert lessons, f"{kind} 应产出教训"
        assert keyword in lessons[0], (
            f"{kind} 的教训应指向维度关键词「{keyword}」，实际「{lessons[0]}」"
        )


def test_death_lessons_are_distinct_across_kinds() -> None:
    """不同死因必须产出**不同**教训 —— 否则"多维反思"退化成一句话。"""
    kinds = [DEATH_KIND_ELITE, DEATH_KIND_AOE, DEATH_KIND_ATTRITION, DEATH_KIND_BURST]
    texts = [
        attribution_to_lessons({"kind": k, "outcome": "defeat"})[0] for k in kinds
    ]
    assert len(set(texts)) == len(kinds), f"不同死因的教训重复了：{texts}"


def test_build_attribution_keys_are_stable() -> None:
    """`build_death_attribution` 的键必须恒定（缺数据给中性值，调用方无需判空）。"""
    empty = build_death_attribution({})
    for key in (
        "kind", "outcome", "floor", "combat_rooms", "combat_lost",
        "worst_room", "worst_lost", "worst_share",
        "multi_enemy_rooms", "multi_enemy_lost", "multi_enemy_share",
        "elite_rooms", "boss_rooms",
    ):
        assert key in empty, f"缺少键 {key}"
    assert empty["kind"] == DEATH_KIND_UNKNOWN
