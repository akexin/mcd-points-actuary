#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""麦麦积分精算师 · v3 跨渠道到手成本规划器。

命题
----
同一份麦当劳订单，在**不同渠道**的到手价可以差出很多钱：

    麦当劳到店自取  → 无配送费、无打包费，但只能用官方券（单笔 1 张）
    麦当劳麦乐送    → 有配送费，满额可免
    美团外卖        → 有配送费与打包费，但**平台券 + 商家券 + 必点榜券可叠加**
    饿了么          → 配送费更高，但红包门槛低

再叠加第三层变量 —— **积分**：积分可以换券抵扣现金，但积分本身是有机会成本的
（同样 200 积分，换对东西值 ¥75，换错东西只值 ¥5.5）。

于是"哪里点最便宜"变成一个**三变量联合决策**：

    渠道（自有 / 第三方）× 券的组合（单张 / 可叠加）× 支付手段（现金 / 积分）

本模块把这三维统一到**一个目标函数**上：

    到手成本 = 现金支出 + 积分消耗 × 积分影子价格

其中「积分影子价格」由 :mod:`points_engine` 的兑换基准 `ypp`（元/积分）给出 ——
即"这些积分如果拿去做最优兑换、能保证换回多少现金价值"。
这样现金与积分才能放在同一个刻度上比较，而不是靠感觉。

券叠加的建模口径（**显式声明，便于复核**）
------------------------------------------
1. **单品级券先落地**：``item_free`` / ``item_reduce`` / ``item_discount`` / ``buy_get``
   作用于单件商品，等价于第三方平台常见的"半价周末""单品直降"。
2. **整单级券后应用**：``threshold_reduce`` / ``discount`` 作用在扣除单品级优惠后的金额上。
3. **同 ``stack_group`` 内互斥，跨组可叠加**；空组视为同一个默认组，
   因此"单笔只用 1 张券"的 v1 语义天然包含在内。
4. ``max_stack`` 限制**整单级券**的张数（单品级属于平台定价，不占叠加额度）。

真实平台的叠加规则远比这复杂（且会变），所以本项目只承诺：**在声明的口径下，
结论是可复算的**；口径变更时结论会同步变更。
"""
from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, field, replace
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from combo_search import (  # noqa: E402
    COMBOS_FULL, COMBOS_PRUNED, ComboIndex, make_bound, search,
)
from optimize_deals import (
    ITEM_LEVEL_TYPES, Constraints, Coupon, Item, _prune_items, _satisfies, fmt_yuan,
    single_coupon_discount, to_cents,
)

DISCLAIMER = (
    "到手价由「商品价 + 渠道费 − 券 − 积分机会成本」估算得出；"
    "第三方平台的券面与叠加规则会随时调整，实际应付金额以下单页为准。"
)

SOURCE_TEXT = {"mcp": "麦当劳官方(实时)", "external": "第三方平台(用户提供)"}

#: 券叠加求解引擎
ENGINE_LEGACY = "legacy"          # v3 现行分步实现（已公布数字的来源）
ENGINE_CONSTRAINT = "constraint"  # 通用约束求解器（额外保证商品池约束 C3）


# --------------------------------------------------------------------------- #
# 渠道与报价模型
# --------------------------------------------------------------------------- #
@dataclass(frozen=True, slots=True)
class Channel:
    """一个点餐渠道：自有或第三方，含其费用结构与券池。"""

    code: str
    name: str
    source: str = "mcp"                      # mcp | external
    delivery_fee_cents: int = 0
    packaging_fee_cents: int = 0
    min_order_cents: int = 0                 # 起送价（按商品原价小计判定）
    free_delivery_threshold_cents: int = 0   # 满此金额免配送费（0 = 不免）
    price_factor: float = 1.0                # 平台标价系数（第三方可能上浮）
    coupons: Tuple[Coupon, ...] = ()
    allow_stack: bool = False                # 是否允许多张券叠加
    max_stack: int = 1                       # 整单级券最多叠加张数
    notes: str = ""

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "Channel":
        policy = d.get("coupon_policy") or {}
        return cls(
            code=str(d["code"]),
            name=str(d.get("name", d["code"])),
            source=str(d.get("source", "external")),
            delivery_fee_cents=to_cents(d.get("delivery_fee", 0)),
            packaging_fee_cents=to_cents(d.get("packaging_fee", 0)),
            min_order_cents=to_cents(d.get("min_order", 0)),
            free_delivery_threshold_cents=to_cents(d.get("free_delivery_threshold", 0)),
            price_factor=float(d.get("price_factor", 1.0) or 1.0),
            coupons=tuple(Coupon.from_dict(c) for c in d.get("coupons", []) or ()),
            allow_stack=bool(policy.get("allow_stack", d.get("allow_stack", False))),
            max_stack=int(policy.get("max_stack", d.get("max_stack", 1)) or 1),
            notes=str(d.get("notes", "")),
        )

    @property
    def is_external(self) -> bool:
        return self.source == "external"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "code": self.code, "name": self.name,
            "source": SOURCE_TEXT.get(self.source, self.source),
            "delivery_fee": fmt_yuan(self.delivery_fee_cents),
            "packaging_fee": fmt_yuan(self.packaging_fee_cents),
            "min_order": fmt_yuan(self.min_order_cents),
            "stack": f"最多 {self.max_stack} 张" if self.allow_stack else "单张",
            "coupon_count": len(self.coupons),
            "notes": self.notes,
        }


@dataclass(slots=True)
class CouponApplication:
    """券在某个购物车上的落地结果。"""

    coupons: List[Coupon] = field(default_factory=list)
    discount_cents: int = 0
    point_cost: int = 0
    goods_cents: int = 0
    payable_cents: int = 0         # 券后商品应付（不含渠道费）
    item_level_cents: int = 0
    order_level_cents: int = 0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "coupons": [c.name for c in self.coupons],
            "discount": fmt_yuan(self.discount_cents),
            "point_cost": self.point_cost,
            "goods": fmt_yuan(self.goods_cents),
            "payable": fmt_yuan(self.payable_cents),
        }


@dataclass(slots=True)
class Quote:
    """某个渠道对某个购物车的完整报价。"""

    channel: Channel
    items: List[Item]
    goods_cents: int = 0
    discount_cents: int = 0
    delivery_cents: int = 0
    packaging_cents: int = 0
    cash_cents: int = 0            # 实付现金
    point_cost: int = 0            # 消耗积分
    point_value_cents: int = 0     # 积分的机会成本
    landed_cents: int = 0          # 统一到手成本 = 现金 + 积分机会成本
    coupons: List[Coupon] = field(default_factory=list)
    feasible: bool = True
    reason: str = ""

    @property
    def saved_vs_goods(self) -> int:
        """相对"按渠道标价单品相加"省下的钱（含券与积分机会成本）。"""
        return max(0, self.goods_cents - self.landed_cents)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "channel": self.channel.name,
            "channel_code": self.channel.code,
            "items": " + ".join(i.name for i in self.items),
            "goods": fmt_yuan(self.goods_cents),
            "coupons": [c.name for c in self.coupons],
            "discount": fmt_yuan(self.discount_cents),
            "delivery": fmt_yuan(self.delivery_cents),
            "packaging": fmt_yuan(self.packaging_cents),
            "cash": fmt_yuan(self.cash_cents),
            "point_cost": self.point_cost,
            "point_value": fmt_yuan(self.point_value_cents),
            "landed": fmt_yuan(self.landed_cents),
            "landed_cents": self.landed_cents,
            "feasible": self.feasible,
            "reason": self.reason,
        }


# --------------------------------------------------------------------------- #
# 券叠加求解
# --------------------------------------------------------------------------- #
def _point_value_cents(coupon: Coupon, shadow_ypp: float) -> int:
    """券所消耗积分的机会成本（分）。"""
    if coupon.point_cost <= 0 or shadow_ypp <= 0:
        return 0
    return int(round(coupon.point_cost * shadow_ypp * 100))


def _net_cents(discount_cents: int, coupon: Coupon, shadow_ypp: float) -> int:
    """券的**净收益**（分）= 抵扣额 − 为拿到它而付出的积分机会成本。

    这是整个 v3 的关键判据：一张"抵扣 24.5 元但要 500 积分"的兑换券，
    在影子价格 0.138 元/积分下净收益是 ``24.5 − 69 = −44.5``，
    **必须被判为不划算** —— 否则引擎会推荐"用 500 积分去换 24.5 元的汉堡"这种明显亏损的方案。
    """
    return discount_cents - _point_value_cents(coupon, shadow_ypp)


def _best_per_group(combo: List[Item], coupons: Sequence[Coupon],
                    subtotal: int, shadow_ypp: float = 0.0) -> List[Tuple[Coupon, int]]:
    """按 ``stack_group`` 分组，用**净收益**择优，每组取一张。

    注意择优依据是净收益而非抵扣额 —— 后者会把高积分成本的兑换券误选为最优。
    """
    best: Dict[str, Tuple[Coupon, int]] = {}
    for c in coupons:
        d = single_coupon_discount(combo, c, subtotal)
        if d <= 0:
            continue
        net = _net_cents(d, c, shadow_ypp)
        if net <= 0:      # 用了反而更亏，直接不用
            continue
        cur = best.get(c.stack_group)
        if cur is None or net > _net_cents(cur[1], cur[0], shadow_ypp):
            best[c.stack_group] = (c, d)
    return list(best.values())


def apply_coupons(
    combo: List[Item],
    coupons: Sequence[Coupon],
    *,
    allow_stack: bool = False,
    max_stack: int = 1,
    shadow_ypp: float = 0.0,
    engine: str = ENGINE_LEGACY,
) -> CouponApplication:
    """求解购物车上的最优券应用方案。

    ``allow_stack=False`` 时等价于 v1 的"单笔仅用 1 张最优券"：
    所有券（不分单品级/整单级）同池竞争，只留净收益最大的一张。

    ``shadow_ypp`` 给定时，带积分成本的券会按其**净收益**参与竞争；
    取 0 表示把积分当免费（此时退化为 v1 的纯抵扣额比较）。

    ``engine`` 选择求解器：

    - ``legacy``（默认）：本函数内的分步实现，**v3 已公布数字的来源**，保持不变。
    - ``constraint``：交给 :mod:`coupon_solver` 的通用约束求解器。两者在
      全部样例与线上券池上给出**逐分相同**的抵扣额（2548 用例 × 4 个影子价格
      零差异，见 ``tests/test_coupon_solver.py``），但约束版额外保证
      **每件商品被抵扣的总额不超过其标价**，并给出逐券归因与剪枝统计。
    """
    if engine == ENGINE_CONSTRAINT:
        return apply_coupons_constraint(
            combo, coupons, allow_stack=allow_stack,
            max_stack=max_stack, shadow_ypp=shadow_ypp)

    goods = sum(i.price_cents for i in combo)
    if not combo or goods <= 0 or not coupons:
        return CouponApplication(goods_cents=goods, payable_cents=max(0, goods))

    if not allow_stack:
        best_c: Optional[Coupon] = None
        best_d = 0
        best_net = 0
        for c in coupons:
            d = single_coupon_discount(combo, c, goods)
            if d <= 0:
                continue
            net = _net_cents(d, c, shadow_ypp)
            if net > best_net:
                best_net, best_d, best_c = net, d, c
        if best_c is None:
            return CouponApplication(goods_cents=goods, payable_cents=goods)
        return CouponApplication(
            coupons=[best_c], discount_cents=best_d, point_cost=best_c.point_cost,
            goods_cents=goods, payable_cents=goods - best_d, item_level_cents=best_d,
        )

    # ① 单品级券先落地
    item_picked = _best_per_group(
        combo, [c for c in coupons if c.type in ITEM_LEVEL_TYPES], goods, shadow_ypp
    )
    item_total = min(sum(d for _, d in item_picked), goods)
    middle = goods - item_total

    # ② 整单级券作用于扣除单品优惠后的金额，按叠加张数限流（净收益降序）
    order_picked = _best_per_group(
        combo, [c for c in coupons if c.type not in ITEM_LEVEL_TYPES], middle, shadow_ypp
    )
    order_picked.sort(key=lambda x: -_net_cents(x[1], x[0], shadow_ypp))
    order_picked = order_picked[: max(1, max_stack)]
    order_total = min(sum(d for _, d in order_picked), middle)

    selected = [c for c, _ in item_picked] + [c for c, _ in order_picked]
    return CouponApplication(
        coupons=selected,
        discount_cents=item_total + order_total,
        point_cost=sum(c.point_cost for c in selected),
        goods_cents=goods,
        payable_cents=goods - item_total - order_total,
        item_level_cents=item_total,
        order_level_cents=order_total,
    )


def apply_coupons_constraint(
    combo: List[Item],
    coupons: Sequence[Coupon],
    *,
    allow_stack: bool = False,
    max_stack: int = 1,
    shadow_ypp: float = 0.0,
    order_mode: str = "middle",
) -> CouponApplication:
    """用**通用约束求解器**（:mod:`coupon_solver`）求解，返回同一套 ``CouponApplication``。

    这一层只是适配器：把 ``SolveResult`` 映射回渠道层的数据结构，
    好让 ``price_in_channel`` / ``compare_channels`` 一行不用改就能换引擎。
    延迟导入避免与 ``coupon_solver`` 形成模块级循环。
    """
    from coupon_solver import solve  # noqa: PLC0415

    r = solve(combo, coupons, allow_stack=allow_stack, max_stack=max_stack,
              shadow_ypp=shadow_ypp, order_mode=order_mode)
    return CouponApplication(
        coupons=list(r.coupons),
        discount_cents=r.discount_cents,
        point_cost=r.point_cost,
        goods_cents=r.goods_cents,
        payable_cents=r.payable_cents,
        item_level_cents=r.item_level_cents,
        order_level_cents=r.order_level_cents,
    )


# --------------------------------------------------------------------------- #
# 渠道定价
# --------------------------------------------------------------------------- #
def price_in_channel(
    combo: Sequence[Item],
    channel: Channel,
    *,
    shadow_ypp: float = 0.0,
) -> Quote:
    """计算某个购物车在某渠道的到手成本。

    ``shadow_ypp`` 为积分的影子价格（元/积分，通常取 :mod:`points_engine` 的
    ``benchmark_ypp``）。取 0 表示"把积分当免费"，此时排序退化为纯现金比较。
    """
    items = list(combo)
    if channel.price_factor != 1.0:
        items = [replace(i, price_cents=int(round(i.price_cents * channel.price_factor)))
                 for i in items]
    goods = sum(i.price_cents for i in items)

    if goods <= 0:
        return Quote(channel=channel, items=items, feasible=False, reason="空购物车")

    if goods < channel.min_order_cents:
        return Quote(
            channel=channel, items=items, goods_cents=goods, feasible=False,
            reason=f"未达起送价（差 {fmt_yuan(channel.min_order_cents - goods)}）",
        )

    app = apply_coupons(items, channel.coupons, allow_stack=channel.allow_stack,
                        max_stack=channel.max_stack, shadow_ypp=shadow_ypp)

    delivery = channel.delivery_fee_cents
    if channel.free_delivery_threshold_cents and goods >= channel.free_delivery_threshold_cents:
        delivery = 0

    cash = app.payable_cents + delivery + channel.packaging_fee_cents
    point_value = int(round(app.point_cost * shadow_ypp * 100))
    return Quote(
        channel=channel, items=items, goods_cents=goods,
        discount_cents=app.discount_cents, delivery_cents=delivery,
        packaging_cents=channel.packaging_fee_cents, cash_cents=cash,
        point_cost=app.point_cost, point_value_cents=point_value,
        landed_cents=cash + point_value, coupons=app.coupons,
    )


def compare_channels(
    combo: Sequence[Item],
    channels: Sequence[Channel],
    *,
    shadow_ypp: float = 0.0,
) -> List[Quote]:
    """同一购物车在所有渠道的报价，按到手成本升序（不可行的排最后）。"""
    quotes = [price_in_channel(combo, ch, shadow_ypp=shadow_ypp) for ch in channels]
    quotes.sort(key=lambda q: (not q.feasible, q.landed_cents))
    return quotes


# --------------------------------------------------------------------------- #
# 跨渠道组合优化
# --------------------------------------------------------------------------- #
@dataclass(slots=True)
class ChannelPlan:
    """一个购物车 + 其在最优渠道上的报价。"""

    combo: List[Item]
    best: Quote
    quotes: List[Quote]

    @property
    def landed_cents(self) -> int:
        return self.best.landed_cents

    @property
    def cash_cents(self) -> int:
        return self.best.cash_cents

    def to_dict(self) -> Dict[str, Any]:
        return {
            "best_channel": self.best.channel.name,
            "items": " + ".join(i.name for i in self.combo),
            "item_codes": [i.code for i in self.combo],
            "landed": fmt_yuan(self.landed_cents),
            "landed_cents": self.landed_cents,
            "cash": fmt_yuan(self.cash_cents),
            "point_cost": self.best.point_cost,
            "coupons": [c.name for c in self.best.coupons],
            "all_channels": [q.to_dict() for q in self.quotes],
        }


def optimize_across_channels(
    items: Sequence[Item],
    channels: Sequence[Channel],
    cons: Constraints,
    *,
    top: int = 5,
    shadow_ypp: float = 0.0,
    max_combos: int = 200_000,
    objective: str = "landed",
    engine: str = COMBOS_FULL,
) -> Tuple[List[ChannelPlan], bool]:
    """在全渠道上求解到手成本最低的 Top-N 方案。

    预算约束在这里**按到手成本**判定（而不是 v1 的券后商品价），
    因为用户真正关心的是"我最后要付出多少"。

    ``objective`` 取 ``landed``（现金+积分机会成本，默认）或 ``cash``（只看现金）。

    ``engine`` 选择组合枚举方式：

    - ``full``（默认）：笛卡尔积全量枚举 —— **v1/v2/v3 已公布数字的来源**，保持逐字节不变。
    - ``pruned``：:mod:`combo_search` 的分支定界。用"再涨也赢不了"的可采纳下界
      整棵剪掉子树，枚举量大幅下降，**结果与 ``full`` 逐字段相同**（见
      ``tests/test_combo_search.py`` 的对拍与 ``tools/bench.py`` 的实测）。

    ``pruned`` 不做任何取舍：不可行的组合照样会被 :func:`optimize_deals._satisfies`
    与渠道定价判掉，下界只负责让它们**更早**被判掉。
    """
    # 预算交给渠道定价后再判定，因此这里先摘掉 budget
    base = Constraints(
        budget_cents=None,
        min_items=cons.min_items,
        max_items=cons.max_items,
        max_calories=cons.max_calories,
        must_include=cons.must_include,
        exclude=cons.exclude,
    )
    pool, pruned = _prune_items(list(items), base, max_combos)
    k_max = min(base.max_items, len(pool))
    pos = {it.code: n for n, it in enumerate(pool)}

    def score(combo_list: List[Item]) -> Optional[Tuple[Tuple[Any, ...], ChannelPlan]]:
        """购物车 → ``((成本, 件数, 池内下标元组), 方案)``；约束不满足 / 不可行 / 超预算 → None。

        约束判定**只在这里**做一次，两个引擎共用同一份语义 ——
        剪枝路径里的掩码是对它的**加速**，不是它的替代品，
        所以即便掩码写错也不会改变答案（只会少省一点时间）。

        排序键是**全序**：末位那个下标元组把"成本打平"的情况也定死。
        只按 ``(成本, 件数)`` 排是不够的 —— 分支定界只保留前 N 条，
        并列者选谁就会取决于各自的枚举顺序，两个引擎会各挑一个"同样便宜但不同"的方案。
        全序则保证它们挑的是同一个。``key[0]`` 仍是成本，下界比较的就是它。
        """
        if not _satisfies(combo_list, base):
            return None
        quotes = compare_channels(combo_list, channels, shadow_ypp=shadow_ypp)
        feasible = [q for q in quotes if q.feasible]
        if not feasible:
            return None
        pick = (lambda q: q.landed_cents) if objective == "landed" else (lambda q: q.cash_cents)
        best = min(feasible, key=pick)
        if cons.budget_cents is not None and best.landed_cents > cons.budget_cents:
            return None
        primary = best.landed_cents if objective == "landed" else best.cash_cents
        plan = ChannelPlan(combo=combo_list, best=best, quotes=quotes)
        return (primary, len(combo_list), tuple(pos[i.code] for i in combo_list)), plan

    import itertools
    scored: List[Tuple[Tuple[Any, ...], ChannelPlan]] = []
    if engine == COMBOS_PRUNED and pool:
        idx = ComboIndex(pool, base)
        bound_fn, _info = make_bound(channels, idx.items, item_count=k_max)

        def evaluate(combo):
            # 搜索按价格升序给组合；还原成**池子顺序**，
            # 否则 ChannelPlan.combo 的展示顺序与下标元组都会与全量枚举不一致。
            ordered = sorted(combo, key=lambda it: pos[it.code])
            return score(ordered)

        scored, _stats = search(idx, evaluate, bound=bound_fn, keep=top, engine=COMBOS_PRUNED)
    else:
        for k in range(base.min_items, k_max + 1):
            for combo in itertools.combinations(pool, k):
                got = score(list(combo))
                if got is not None:
                    scored.append(got)

    scored.sort(key=lambda row: row[0])
    return [plan for _key, plan in scored[:top]], pruned


def channel_savings(plans: Sequence[ChannelPlan], channels: Sequence[Channel]) -> Dict[str, Any]:
    """汇总"选对渠道"省下多少钱，用于报告里的决策依据。"""
    if not plans:
        return {}
    p = plans[0]
    ranked = sorted((q for q in p.quotes if q.feasible), key=lambda q: q.landed_cents)
    worst = ranked[-1]
    return {
        "best_channel": p.best.channel.name,
        "best_landed": p.best.landed_cents,
        "worst_channel": worst.channel.name,
        "worst_landed": worst.landed_cents,
        "channel_gap": worst.landed_cents - p.best.landed_cents,
    }


# --------------------------------------------------------------------------- #
# 积分 → 券（把 v2 的积分资产接进 v3 的渠道券池）
# --------------------------------------------------------------------------- #
def point_coupon(coupon_id: str, name: str, item_code: str, point_cost: int,
                 stack_group: str = "points") -> Coupon:
    """把"积分兑换券"表达成一张带积分成本的 ``item_free`` 券。

    之所以能这样建模：用积分兑换某个套餐，等价于"这个套餐免费，但我付出了积分"。
    只要给这张券打上 ``point_cost``，统一目标函数就会自动比较
    「用券省现金」与「积分留作他用」，无需额外规则。
    """
    return Coupon(
        id=coupon_id, name=name, type="item_free", item_code=item_code,
        point_cost=point_cost, stack_group=stack_group,
    )


def point_bundle_coupon(coupon_id: str, name: str, item_codes: Sequence[str],
                        fixed_cents: int, point_cost: int,
                        stack_group: str = "points") -> Coupon:
    """把"X 元换这个组合"的积分餐品券表达成 ``bundle_price`` 券。

    积分商城绝大多数餐品券长这样（「21.9 元巨无霸可乐组合」「28.8 元巨无霸四件套」）：
    付出积分后，按一个**打包价**拿走若干件商品。用 ``bundle_price`` 表达后，
    抵扣额 = 这几件商品的菜单原价 − 打包价，再由统一目标函数扣掉积分机会成本。
    """
    return Coupon(
        id=coupon_id, name=name, type="bundle_price",
        item_codes=tuple(item_codes), fixed_cents=fixed_cents,
        point_cost=point_cost, stack_group=stack_group,
    )


def breakeven_ypp(discount_cents: int, point_cost: int) -> float:
    """一张积分券的**打平影子价格**（元/积分）。

    含义：只有当 1 积分的机会价值低于这个数时，用这张券才是赚的。
    把它与 :attr:`points_engine.ValueMatrix.benchmark_ypp`（现实可得的最优兑换效率）
    相比，就能一句话回答"这券该不该换" —— 不需要预测任何概率。
    """
    if point_cost <= 0:
        return 0.0
    return discount_cents / 100.0 / point_cost


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def _load(path: str) -> Any:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def _items(path: str) -> List[Item]:
    data = _load(path)
    rows = data.get("data", data) if isinstance(data, dict) else data
    return [Item.from_dict(d) for d in rows]


def _channels(path: str) -> List[Channel]:
    data = _load(path)
    rows = data.get("channels", data) if isinstance(data, dict) else data
    return [Channel.from_dict(d) for d in rows]


def _print_quotes(combo: List[Item], quotes: List[Quote]) -> None:
    print(f"购物车：{' + '.join(i.name for i in combo)}")
    print(f"{'渠道':<20}{'券后':>9}{'配送':>8}{'打包':>8}{'现金':>9}"
          f"{'积分':>6}{'到手成本':>11}  券")
    print("-" * 92)
    for q in quotes:
        if not q.feasible:
            print(f"{q.channel.name:<20}{'—':>9}{'—':>8}{'—':>8}{'—':>9}{'—':>6}"
                  f"{'不可行':>11}  {q.reason}")
            continue
        coupon_txt = " + ".join(c.name for c in q.coupons) or "无"
        print(f"{q.channel.name:<20}{fmt_yuan(q.goods_cents - q.discount_cents):>9}"
              f"{fmt_yuan(q.delivery_cents):>8}{fmt_yuan(q.packaging_cents):>8}"
              f"{fmt_yuan(q.cash_cents):>9}{q.point_cost:>6}{fmt_yuan(q.landed_cents):>11}  {coupon_txt}")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="v3 · 跨渠道到手成本规划器")
    p.add_argument("--menu", required=True, help="菜单 JSON")
    p.add_argument("--channels", required=True, help="渠道与券 JSON")
    p.add_argument("--shadow-ypp", type=float, default=0.0,
                   help="积分影子价格（元/积分）；0 表示把积分当免费")
    sub = p.add_subparsers(dest="cmd", required=True)

    c = sub.add_parser("compare", help="给定购物车比较各渠道到手价")
    c.add_argument("--items", nargs="+", required=True, help="菜品 code")
    c.add_argument("--json", action="store_true")

    o = sub.add_parser("optimize", help="全渠道求解最便宜组合")
    o.add_argument("--min-items", type=int, default=1)
    o.add_argument("--max-items", type=int, default=3)
    o.add_argument("--budget", default=None, help="到手成本上限（元）")
    o.add_argument("--must-include", nargs="*", default=None)
    o.add_argument("--exclude", nargs="*", default=None)
    o.add_argument("--objective", choices=("landed", "cash"), default="landed")
    o.add_argument("--top", type=int, default=5)
    o.add_argument("--engine", choices=(COMBOS_FULL, COMBOS_PRUNED), default=COMBOS_FULL,
                   help="组合枚举引擎：full=全量枚举（默认，已公布数字的来源）；"
                        "pruned=分支定界，结果相同但枚举量更少")
    o.add_argument("--json", action="store_true")
    return p


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    items = _items(args.menu)
    channels = _channels(args.channels)
    by_code = {i.code: i for i in items}

    if args.cmd == "compare":
        combo = [by_code[c] for c in args.items if c in by_code]
        missing = [c for c in args.items if c not in by_code]
        if missing:
            print(f"菜单中找不到：{missing}", file=sys.stderr)
        quotes = compare_channels(combo, channels, shadow_ypp=args.shadow_ypp)
        if args.json:
            print(json.dumps([q.to_dict() for q in quotes], ensure_ascii=False, indent=2))
        else:
            _print_quotes(combo, quotes)
            ok = [q for q in quotes if q.feasible]
            if len(ok) > 1:
                print(f"\n选对渠道比最贵的省 {fmt_yuan(ok[-1].landed_cents - ok[0].landed_cents)}")
        print(f"\n{DISCLAIMER}")
        return 0

    if args.cmd == "optimize":
        cons = Constraints(
            budget_cents=to_cents(args.budget) if args.budget is not None else None,
            min_items=args.min_items, max_items=args.max_items,
            must_include=tuple(args.must_include or ()),
            exclude=tuple(args.exclude or ()),
        )
        plans, pool_pruned = optimize_across_channels(
            items, channels, cons, top=args.top,
            shadow_ypp=args.shadow_ypp, objective=args.objective, engine=args.engine,
        )
        if args.json:
            print(json.dumps([p.to_dict() for p in plans], ensure_ascii=False, indent=2))
            return 0 if plans else 1
        if not plans:
            print("没有满足约束的方案，请放宽 --budget / --max-items。")
            return 1
        label = "已剪枝" if pool_pruned else "全量枚举"
        if args.engine == COMBOS_PRUNED:
            label += "（组合枚举：分支定界）"
        print(f"全渠道求解（{label}，"
              f"目标={'到手成本' if args.objective == 'landed' else '现金'}）：\n")
        for i, p in enumerate(plans, 1):
            print(f"{i}. 到手 {fmt_yuan(p.landed_cents)}（现金 {fmt_yuan(p.cash_cents)}"
                  f"{f' + {p.best.point_cost} 积分' if p.best.point_cost else ''}）"
                  f" @ {p.best.channel.name}")
            print(f"   {' + '.join(i.name for i in p.combo)}")
            if p.best.coupons:
                print(f"   券：{' + '.join(c.name for c in p.best.coupons)}")
        s = channel_savings(plans, channels)
        if s:
            print(f"\n渠道差价：最优 {s['best_channel']} {fmt_yuan(s['best_landed'])}"
                  f" vs 最贵 {s['worst_channel']} {fmt_yuan(s['worst_landed'])}"
                  f" → 选对渠道省 {fmt_yuan(s['channel_gap'])}")
        print(f"\n{DISCLAIMER}")
        return 0

    return 1


if __name__ == "__main__":
    sys.exit(main())
