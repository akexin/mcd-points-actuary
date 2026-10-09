#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""券叠加的**通用约束求解** —— 用「券 → 约束编译 + 与券型无关的求解器」替代逐类券硬编码。

问题陈述
--------
给定购物车（商品集合，每件有标价）与券集合，选出一个子集 ``S``，使

    净收益 = Σ抵扣(S) − Σ积分成本(S)      （积分成本 = 消耗积分 × 影子价格）

最大，约束为：

============  ==================================================================
C1 互斥       同一 ``stack_group`` 至多 1 张
C2 限张数     整单级券张数 ≤ ``max_stack``
C3 **商品池**  每件商品被抵扣的总额 ≤ 该商品标价
C4 适用条件   券自身的门槛 / 需含商品 / 最少件数
============  ==================================================================

C3 是 v3 现行实现的漏洞所在
--------------------------
``channel_planner._best_per_group`` 只在**组内**择优，跨组的两张单品券若都指向
同一件商品，会各自按**原价**算出抵扣再相加 —— 只在总额处 ``min(..., goods)``。
后果是"抵扣额偶尔正确但归因错误"，极端情况下会选出一个**实际不可行**的券集
（同一件商品的折扣被叠加两次）。

本模块把「商品池」显式建模为**可扣减账本**（每件商品剩余可抵扣额），
券在账本上顺序扣减，从根上消除重复抵扣。
回归测试 ``test_item_pool_no_double_count`` 用一个最小反例锁住这一点。

求解策略
--------
搜索空间 = 各互斥组「选一张 or 不选」的直积。组数通常 ≤ 8、组内 ≤ 4，
直积在千级以内 → **分支定界精确求解**；超出 ``item_exact_limit`` 时退化为
**束搜索（beam）**，并在结果 ``method`` 里显式标注走了哪条路。

两条路径共用同一套「券在账本上落地」的语义，所以结论可比、可交叉验证。

整单级券的两种口径
------------------
- ``order_mode="middle"``（**默认，保持 v3 已公布数字不变**）：
  每张整单券都对「扣除单品优惠后的金额」独立计算，求和后 clamp。
- ``order_mode="sequential"``（可选，更贴近平台实际）：
  逐张在**前一张扣减后的余额**上计算。

``sequential`` 通常给出更小的抵扣额，是**敏感性分析**工具 ——
默认不启用，以免悄悄改变已提交的结论。
"""
from __future__ import annotations

import argparse
import os
import sys
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from optimize_deals import ITEM_LEVEL_TYPES, Coupon, Item, fmt_yuan  # noqa: E402

ROOT = os.path.dirname(HERE)

METHOD_SINGLE = "single"   # 不允许叠加 → 单张最优
METHOD_EXACT = "exact"     # 分支定界，保证全局最优
METHOD_BEAM = "beam"       # 束搜索，近似最优（搜索空间超限）

ORDER_MIDDLE = "middle"
ORDER_SEQUENTIAL = "sequential"


# --------------------------------------------------------------------------- #
# 结果
# --------------------------------------------------------------------------- #
@dataclass
class SolveResult:
    coupons: List[Coupon] = field(default_factory=list)
    applied: List[Tuple[Coupon, int]] = field(default_factory=list)   # 券 → 实际抵扣（分）
    discount_cents: int = 0
    point_cost: int = 0
    net_cents: int = 0
    item_level_cents: int = 0
    order_level_cents: int = 0
    goods_cents: int = 0
    method: str = METHOD_SINGLE
    order_mode: str = ORDER_MIDDLE
    item_nodes: int = 0            # 单品级搜索实际展开的状态数
    item_space: int = 0            # 单品级搜索的完整空间大小（不剪枝）
    order_trials: int = 0          # 整单级试算次数（贪心，无指数空间）
    pool_used: Dict[str, int] = field(default_factory=dict)   # 各商品被抵扣的金额
    warnings: List[str] = field(default_factory=list)

    @property
    def payable_cents(self) -> int:
        return max(0, self.goods_cents - self.discount_cents)

    @property
    def nodes(self) -> int:
        return self.item_nodes + self.order_trials

    @property
    def nodes_unpruned(self) -> int:
        return self.item_space + self.order_trials

    @property
    def pruning_ratio(self) -> float:
        return (round(1 - self.item_nodes / self.item_space, 4)
                if self.item_space else 0.0)

    def to_dict(self) -> Dict[str, object]:
        return {
            "coupons": [c.name for c in self.coupons],
            "applied": [{"name": c.name, "type": c.type, "discount": fmt_yuan(d)}
                        for c, d in self.applied],
            "discount": fmt_yuan(self.discount_cents),
            "point_cost": self.point_cost,
            "net": fmt_yuan(self.net_cents),
            "item_level": fmt_yuan(self.item_level_cents),
            "order_level": fmt_yuan(self.order_level_cents),
            "goods": fmt_yuan(self.goods_cents),
            "payable": fmt_yuan(self.payable_cents),
            "pool_used": {k: fmt_yuan(v) for k, v in self.pool_used.items()},
            "method": self.method,
            "order_mode": self.order_mode,
            "item_nodes": self.item_nodes,
            "item_space": self.item_space,
            "order_trials": self.order_trials,
            "pruning_ratio": self.pruning_ratio,
            "warnings": list(self.warnings),
        }


# --------------------------------------------------------------------------- #
# 商品池账本
# --------------------------------------------------------------------------- #
class Ledger:
    """每件商品的**剩余可抵扣额**。

    用 ``__slots__`` 而不是 ``@dataclass(slots=True)``：后者需要 Python 3.10+，
    而本项目声明支持 3.9。这个类在搜索里会被大量复制（每个分支一份），
    是唯一值得手动抠内存的类。
    """

    __slots__ = ("left", "used")

    def __init__(self, items: Sequence[Item]):
        self.left: Dict[str, int] = {i.code: i.price_cents for i in items}
        self.used: Dict[str, int] = {}

    def copy(self) -> "Ledger":
        other = Ledger.__new__(Ledger)
        other.left = dict(self.left)
        other.used = dict(self.used)
        return other

    def total(self) -> int:
        return sum(self.left.values())

    def has(self, code: str) -> bool:
        return bool(code) and self.left.get(code, 0) > 0

    def take(self, code: str, amount: int) -> int:
        """从 ``code`` 至多扣 ``amount`` 分，返回实际扣减额（C3 在此强制）。"""
        if not code or amount <= 0:
            return 0
        avail = self.left.get(code, 0)
        got = min(amount, avail)
        if got:
            self.left[code] = avail - got
            self.used[code] = self.used.get(code, 0) + got
        return got

    def group_value(self, codes: Sequence[str]) -> int:
        return sum(self.left.get(c, 0) for c in codes)


# --------------------------------------------------------------------------- #
# 单张券的静态上界（分支定界用，必须 admissible：不能低估）
# --------------------------------------------------------------------------- #
def _static_cap_cents(c: Coupon, prices: Dict[str, int]) -> int:
    """券在**完全不受挤占**时的抵扣上界（分）。"""
    if c.type == "threshold_reduce":
        return c.reduce_cents
    if c.type == "item_free":
        return prices.get(c.item_code, 0)
    if c.type == "item_reduce":
        return min(c.reduce_cents, prices.get(c.item_code, 0))
    if c.type == "buy_get":
        return prices.get(c.get_code, 0)
    if c.type == "item_discount":
        p = prices.get(c.item_code, 0)
        d = int(round(p * (1.0 - c.rate)))
        return min(d, c.cap_cents) if c.cap_cents else d
    if c.type == "discount":
        total = sum(prices.values())
        if total < c.threshold_cents:
            return 0
        d = int(round(total * (1.0 - c.rate)))
        return min(d, c.cap_cents) if c.cap_cents else d
    if c.type == "bundle_price":
        if not c.item_codes:
            return 0
        group = sum(prices.get(x, 0) for x in c.item_codes)
        return max(0, group - c.fixed_cents)
    return 0


def _net_cap_cents(c: Coupon, prices: Dict[str, int], shadow_ypp: float) -> int:
    return _static_cap_cents(c, prices) - _pts_cents(c.point_cost, shadow_ypp)


def _pts_cents(points: int, shadow_ypp: float) -> int:
    """积分消耗折算成「分」。全模块统一用分做比较，避免分/元混用。"""
    if points <= 0 or shadow_ypp <= 0:
        return 0
    return int(round(points * shadow_ypp * 100))


# --------------------------------------------------------------------------- #
# 券在账本上落地
# --------------------------------------------------------------------------- #
def apply_item_coupon(c: Coupon, ledger: Ledger) -> int:
    """单品级券在账本上落地：返回实际抵扣（分）并扣减账本（C3 在此强制）。"""
    if c.type == "item_free":
        return ledger.take(c.item_code, ledger.left.get(c.item_code, 0))
    if c.type == "item_reduce":
        return ledger.take(c.item_code, c.reduce_cents)
    if c.type == "buy_get":
        if not ledger.has(c.buy_code) or not ledger.has(c.get_code):
            return 0
        # 赠送品免单：从赠送品的剩余额里扣
        return ledger.take(c.get_code, ledger.left.get(c.get_code, 0))
    if c.type == "item_discount":
        if not ledger.has(c.item_code):
            return 0
        avail = ledger.left.get(c.item_code, 0)
        d = int(round(avail * (1.0 - c.rate)))
        if c.cap_cents:
            d = min(d, c.cap_cents)
        return ledger.take(c.item_code, d)
    if c.type == "bundle_price":
        codes = sorted(set(c.item_codes))
        if not codes or not all(ledger.has(x) for x in c.item_codes):
            return 0
        group = ledger.group_value(codes)
        need = group - c.fixed_cents
        if need <= 0:
            return 0
        # 组合特价的让利按各商品剩余额**比例分摊**，避免把钱全记在某一件上
        got = 0
        for code in codes:
            avail = ledger.left.get(code, 0)
            share = int(round(need * avail / group)) if group else 0
            got += ledger.take(code, share)
        short = need - got                      # 四舍五入掉的零头，用剩余额补齐
        for code in codes:
            if short <= 0:
                break
            took = ledger.take(code, short)
            got += took
            short -= took
        return got
    return 0


def apply_order_coupon(c: Coupon, base_cents: int) -> int:
    """整单级券在金额 ``base_cents`` 上的抵扣；不改账本（顺序口径由调用方决定）。"""
    if c.type == "threshold_reduce":
        if base_cents < c.threshold_cents:
            return 0
        return min(c.reduce_cents, base_cents)
    if c.type == "discount":
        if base_cents < c.threshold_cents:
            return 0
        d = int(round(base_cents * (1.0 - c.rate)))
        if c.cap_cents:
            d = min(d, c.cap_cents)
        return min(d, base_cents)
    return 0


def is_item_level(c: Coupon, item_level_types: Sequence[str] = tuple(ITEM_LEVEL_TYPES)) -> bool:
    return c.type in item_level_types


def eligible(c: Coupon, item_count: int) -> bool:
    """结构性适用条件（C4）。金额门槛在各自的落地函数里判定。"""
    if c.min_items and item_count < c.min_items:
        return False
    if c.type in ("item_reduce", "item_free", "item_discount") and not c.item_code:
        return False
    if c.type == "buy_get" and not (c.buy_code and c.get_code):
        return False
    if c.type == "bundle_price" and not c.item_codes:
        return False
    return True


# --------------------------------------------------------------------------- #
# 单品级搜索：分支定界 / 束搜索
# --------------------------------------------------------------------------- #
def _groups(coupons: Sequence[Coupon]) -> List[Tuple[str, List[Coupon]]]:
    """按 ``stack_group`` 分组，键排序以保证结果可复现。"""
    buckets: Dict[str, List[Coupon]] = {}
    for c in coupons:
        buckets.setdefault(c.stack_group, []).append(c)
    return [(g, sorted(buckets[g], key=lambda x: x.id)) for g in sorted(buckets)]


def _bounds(groups: Sequence[Tuple[str, List[Coupon]]],
            prices: Dict[str, int], shadow_ypp: float) -> List[int]:
    """``suffix[i]`` = 第 i 组及其后所有组的净收益上界之和（admissible）。"""
    per_group = [max([0] + [_net_cap_cents(c, prices, shadow_ypp) for c in cs])
                 for _, cs in groups]
    suffix = [0] * (len(groups) + 1)
    for i in range(len(groups) - 1, -1, -1):
        suffix[i] = suffix[i + 1] + per_group[i]
    return suffix


def _search_exact(groups: Sequence[Tuple[str, List[Coupon]]], combo: Sequence[Item], *,
                  prices: Dict[str, int], shadow_ypp: float
                  ) -> Tuple[List[Tuple[Coupon, int]], int]:
    """分支定界：组数少时给出**全局最优**，返回 ``(applied, 展开状态数)``。"""
    suffix = _bounds(groups, prices, shadow_ypp)
    state = {"nodes": 0, "best": [], "best_net": 0}

    def dfs(gi: int, ledger: Ledger, chosen: List[Tuple[Coupon, int]],
            discount: int, pts_cost: int) -> None:
        state["nodes"] += 1
        net = discount - pts_cost
        if net > state["best_net"]:
            state["best_net"], state["best"] = net, list(chosen)
        if gi >= len(groups):
            return
        if net + suffix[gi] <= state["best_net"]:
            return                                  # 乐观上界仍不及最优 → 整枝剪掉
        for c in groups[gi][1]:
            if _net_cap_cents(c, prices, shadow_ypp) <= 0:
                continue                            # 静态就亏（积分太贵），不必展开
            sub = ledger.copy()
            gain = apply_item_coupon(c, sub)
            if gain <= 0:
                continue
            chosen.append((c, gain))
            dfs(gi + 1, sub, chosen, discount + gain,
                pts_cost + _pts_cents(c.point_cost, shadow_ypp))
            chosen.pop()

    dfs(0, Ledger(combo), [], 0, 0)
    return state["best"], state["nodes"]


def _search_beam(groups: Sequence[Tuple[str, List[Coupon]]], combo: Sequence[Item], *,
                 prices: Dict[str, int], shadow_ypp: float, width: int
                 ) -> Tuple[List[Tuple[Coupon, int]], int]:
    """束搜索：逐组保留 top-``width`` 状态，返回 ``(applied, 生成状态数)``。

    排序键 = 已得净收益 + 剩余组乐观上界（与精确搜索同一套 bound，故可比）。
    """
    suffix = _bounds(groups, prices, shadow_ypp)
    # state = (积分成本分, 账本, 已选券, 已得抵扣)
    states: List[Tuple[int, Ledger, List[Tuple[Coupon, int]], int]] = [
        (0, Ledger(combo), [], 0)]
    generated = 0
    for gi, (_, cs) in enumerate(groups):
        nxt: List[Tuple[int, Ledger, List[Tuple[Coupon, int]], int]] = []
        for pts, led, chosen, disc in states:
            nxt.append((pts, led.copy(), list(chosen), disc))          # 不选这一组
            for c in cs:
                if _net_cap_cents(c, prices, shadow_ypp) <= 0:
                    continue
                sub = led.copy()
                gain = apply_item_coupon(c, sub)
                if gain <= 0:
                    continue
                nxt.append((pts + _pts_cents(c.point_cost, shadow_ypp), sub,
                            chosen + [(c, gain)], disc + gain))
        generated += len(nxt)
        nxt.sort(key=lambda s: (-(s[3] - s[0] + suffix[gi + 1]), -s[3],
                                tuple(c.id for c, _ in s[2])))
        states = nxt[:width] if len(nxt) > width else nxt
    best = max(states, key=lambda s: (s[3] - s[0], len(s[2])), default=None)
    return (list(best[2]) if best else []), generated


def _search_item_pool(combo: Sequence[Item], pool: Sequence[Coupon], *,
                      prices: Dict[str, int], shadow_ypp: float,
                      exact_limit: int, beam_width: int
                      ) -> Tuple[List[Tuple[Coupon, int]], int, int, str]:
    """在单品级券池内搜索净收益最大的子集，返回 ``(applied, nodes, space, method)``。"""
    groups = _groups(pool)
    space = 1
    for _, cs in groups:
        space *= (len(cs) + 1)
    if space <= exact_limit:
        applied, nodes = _search_exact(groups, combo, prices=prices, shadow_ypp=shadow_ypp)
        return applied, nodes, space, METHOD_EXACT
    applied, nodes = _search_beam(groups, combo, prices=prices,
                                  shadow_ypp=shadow_ypp, width=beam_width)
    return applied, nodes, space, METHOD_BEAM


# --------------------------------------------------------------------------- #
# 主入口
# --------------------------------------------------------------------------- #
def solve(
    combo: Sequence[Item],
    coupons: Sequence[Coupon],
    *,
    allow_stack: bool = False,
    max_stack: int = 1,
    shadow_ypp: float = 0.0,
    order_mode: str = ORDER_MIDDLE,
    item_exact_limit: int = 200_000,
    beam_width: int = 256,
    item_level_types: Sequence[str] = tuple(ITEM_LEVEL_TYPES),
) -> SolveResult:
    """求解购物车上的最优券应用方案。"""
    combo = list(combo)
    goods = sum(i.price_cents for i in combo)
    prices = {i.code: i.price_cents for i in combo}
    result = SolveResult(goods_cents=goods, order_mode=order_mode)
    if not combo or goods <= 0 or not coupons:
        return result

    usable = [c for c in coupons if eligible(c, len(combo))]
    item_pool = [c for c in usable if c.type in item_level_types]
    order_pool = [c for c in usable if c.type not in item_level_types]

    if not allow_stack:
        # v1 语义：所有券同池竞争，只留净收益最大的一张
        best_c: Optional[Coupon] = None
        best_gain = 0
        best_net = 0
        for c in sorted(usable, key=lambda x: x.id):
            led = Ledger(combo)
            gain = (apply_item_coupon(c, led) if c.type in item_level_types
                    else apply_order_coupon(c, goods))
            if gain <= 0:
                continue
            net = gain - _pts_cents(c.point_cost, shadow_ypp)
            if net > best_net:
                best_c, best_gain, best_net = c, gain, net
        result.method = METHOD_SINGLE
        result.item_space = result.item_nodes = len(usable)
        result.order_trials = 0
        if best_c is not None:
            led = Ledger(combo)
            if best_c.type in item_level_types:
                apply_item_coupon(best_c, led)
            result.coupons = [best_c]
            result.applied = [(best_c, best_gain)]
            result.discount_cents = best_gain
            result.point_cost = best_c.point_cost
            result.net_cents = best_net
            result.item_level_cents = best_gain if best_c.type in item_level_types else 0
            result.order_level_cents = 0 if best_c.type in item_level_types else best_gain
            result.pool_used = {k: v for k, v in led.used.items() if v}
        return result

    # ① 单品级：在商品池账本上搜索
    item_applied, item_nodes, item_space, m_item = _search_item_pool(
        combo, item_pool, prices=prices, shadow_ypp=shadow_ypp,
        exact_limit=item_exact_limit, beam_width=beam_width)
    item_total = min(sum(d for _, d in item_applied), goods)
    middle = goods - item_total

    # ② 整单级：作用于扣除单品优惠后的金额（口径见模块 docstring）
    order_applied: List[Tuple[Coupon, int]] = []
    trials = 0
    if order_pool and middle > 0:
        # (a) 组内互斥（C1）：每组只留净收益最高的一张；净收益 ≤ 0 的整组丢弃
        winners: List[Tuple[int, Coupon]] = []
        for _g, cs_ in _groups(order_pool):
            best_c: Optional[Coupon] = None
            best_net = 0
            for c in cs_:
                d = apply_order_coupon(c, middle)
                if d <= 0:
                    continue
                net = d - _pts_cents(c.point_cost, shadow_ypp)
                if net > best_net:
                    best_c, best_net = c, net
            if best_c is not None:
                winners.append((best_net, best_c))
        # (b) 组间竞争：按净收益降序，受整单券张数上限约束（C2）
        winners.sort(key=lambda x: (-x[0], x[1].id))
        balance = middle
        for _net, c in winners:
            if len(order_applied) >= max(1, max_stack):
                break
            base = middle if order_mode == ORDER_MIDDLE else balance
            d = apply_order_coupon(c, base)
            trials += 1
            if d <= 0:
                continue
            if d - _pts_cents(c.point_cost, shadow_ypp) <= 0:
                continue                      # 净收益 ≤ 0 → 永不用
            order_applied.append((c, d))
            balance -= d
        order_total = min(sum(d for _, d in order_applied), middle)
    else:
        order_total = 0

    selected = [c for c, _ in item_applied] + [c for c, _ in order_applied]
    result.applied = item_applied + order_applied
    result.coupons = selected
    result.discount_cents = item_total + order_total
    result.point_cost = sum(c.point_cost for c in selected)
    result.net_cents = result.discount_cents - _pts_cents(result.point_cost, shadow_ypp)
    result.item_level_cents = item_total
    result.order_level_cents = order_total
    result.item_nodes = item_nodes
    result.item_space = item_space
    result.order_trials = trials
    result.method = METHOD_BEAM if m_item == METHOD_BEAM else METHOD_EXACT

    # 商品池账本：重放一遍选中的券，得到「谁被抵扣了多少」（供瀑布图归因用）
    led = Ledger(combo)
    for c, _ in item_applied:
        apply_item_coupon(c, led)
    result.pool_used = {k: v for k, v in led.used.items() if v}

    if result.method == METHOD_BEAM:
        result.warnings.append(
            f"单品级搜索空间超过精确阈值，已退化为束搜索（宽度 {beam_width}）"
            f"→ 可能非全局最优")
    if order_mode == ORDER_SEQUENTIAL:
        result.warnings.append(
            "整单级券按『逐张在前一张扣减后的余额上计算』求解（sequential）；"
            "与默认口径 middle 的结果不可直接比较")

    # 自检：归因一致 + C3 未被破坏
    if sum(d for _, d in result.applied) != result.discount_cents:
        result.warnings.append("归因异常：逐券抵扣之和 ≠ 总抵扣额")
    if result.discount_cents > goods:
        result.warnings.append("归因异常：总抵扣超过商品总额（约束 C3 被破坏）")
    for code, used in result.pool_used.items():
        if used > prices.get(code, 0):
            result.warnings.append(
                f"归因异常：商品 {code} 被抵扣 {fmt_yuan(used)} > 标价 "
                f"{fmt_yuan(prices.get(code, 0))}（约束 C3 被破坏）")
    return result


# --------------------------------------------------------------------------- #
# CLI：口径对比（middle vs sequential）
# --------------------------------------------------------------------------- #
def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description="券叠加通用约束求解（含口径对比）")
    ap.add_argument("--items", default=os.path.join(ROOT, "data", "sample_menu.json"))
    ap.add_argument("--channels", default=os.path.join(ROOT, "data", "sample_channels.json"))
    ap.add_argument("--channel", default="", help="渠道 code；缺省取第一个")
    ap.add_argument("--cart", nargs="+", required=True, help="购物车商品编码")
    ap.add_argument("--shadow-ypp", type=float, default=0.1380)
    ap.add_argument("--max-stack", type=int, default=2)
    return ap


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    from channel_planner import _channels, _items  # noqa: PLC0415

    items = {i.code: i for i in _items(args.items)}
    combo = [items[c] for c in args.cart if c in items]
    chans = _channels(args.channels)
    ch = next((c for c in chans if c.code == args.channel), chans[0]) if chans else None
    if not ch or not combo:
        print("  购物车或渠道为空，无可求解")
        return 1

    print(f"  渠道：{ch.name}（券 {len(ch.coupons)} 张，"
          f"{'可叠加 ≤ ' + str(ch.max_stack) + ' 张' if ch.allow_stack else '单张'}）")
    print(f"  购物车：{' + '.join(i.name for i in combo)} = "
          f"{fmt_yuan(sum(i.price_cents for i in combo))}")
    for mode in (ORDER_MIDDLE, ORDER_SEQUENTIAL):
        r = solve(combo, ch.coupons, allow_stack=ch.allow_stack,
                  max_stack=args.max_stack, shadow_ypp=args.shadow_ypp,
                  order_mode=mode)
        print(f"\n  ── order_mode={mode} ──")
        print(f"    方法 {r.method} · 单品级展开 {r.item_nodes}/{r.item_space} 状态"
              f"（剪掉 {r.pruning_ratio * 100:.1f}%）· 整单级试算 {r.order_trials} 次")
        for c, d in r.applied:
            print(f"      {c.name}（{c.type}）→ {fmt_yuan(d)}")
        print(f"    抵扣 {fmt_yuan(r.discount_cents)} · 积分 {r.point_cost} · "
              f"应付 {fmt_yuan(r.payable_cents)} · 净收益 {fmt_yuan(r.net_cents)}")
        if r.pool_used:
            print("    商品池占用：" + "，".join(
                f"{items[k].name if k in items else k} {fmt_yuan(v)}"
                for k, v in r.pool_used.items()))
        for w in r.warnings:
            print(f"    ! {w}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
