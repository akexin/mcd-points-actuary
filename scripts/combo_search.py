#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""组合枚举的**分支定界**搜索 —— 用可采纳的下界，把"不可能赢"的子树整棵砍掉。

问题
----
v3 求"最低到手价"靠的是**全量枚举**：候选池里取 k 件，k 从 ``min_items`` 走到
``max_items``，每个组合都完整算一遍全渠道报价。池子 14 项时只有 469 个组合，
无所谓；换成全菜单（60+ 项）且允许 4 件，就是 52 万个组合 —— 而且**绝大多数注定赢不了**。

本模块做的事
------------
把组合空间组织成一棵**按价格升序**的搜索树，边走边算"这个前缀再怎么加也赢不了"，
然后整棵剪掉。四个抓手里，前三个对应评审提的三点，第四个是把 v3 的可行性判定提前：

1. **门槛剪枝**：到手成本 ≥ 商品小计 − 抵扣上界 + 费用下界。商品小计沿子树只增不减，
   所以一个孩子一旦触到阈值，**它的所有后代**同样出局。
2. **优先高价值券**：券池预处理出"每个互斥组能给出的最大抵扣"，**按组求和**作为抵扣上界。
3. **互斥关系**：组内最多生效 1 张 ⇒ 按组取最大再求和仍然可采纳；
   把整池券一起求和会把界放到失效（等于没剪）。
4. **约束前置**：``exclude`` 命中的候选整棵砍掉，``must_include`` 未命中的位数超过
   剩余坑位就整棵砍掉。v3 是在**叶子**上用 ``_satisfies`` 过滤，代价一样但白算了一路
   报价；这里只把它提前，判定结果逐位等同（测试与 ``_satisfies`` 全组合对拍）。

记账刻意分成三笔，因为把两种剪枝混成一个数字就是在给自己发奖状：
``cuts`` 只记**下界**剪掉的子树，``feasibility_cuts`` 只记**约束**剪掉的子树，
``full_nodes`` 是 v3 真会枚举的原始组合数（含不可行组合）。

剪枝阈值与「为什么剪掉的必然进不了前 N」
----------------------------------------
设 ``W(t)`` 是**当前已找到的前 N 名里最差的那个成本**。``W`` 只会变小不会变大。
在一个节点用 ``下界 > W(t)`` 作剪枝条件时，被剪掉的整棵子树里每个组合的成本都

    成本 ≥ 该子节点的下界 > W(t) ≥ W(最终)

也就是**严格差于最终的前 N 名边界**。所以剪枝不会误伤前 N 名。这条推理依赖三点，
三者都在测试里单独钉住：

- 下界必须真的可采纳（测试用暴力枚举逐组验证 ``成本 ≥ 下界``）
- 下界必须对商品小计**单调不减**（否则"后代更差"不成立）
- 只有在前 N 名**已满**时才启用阈值

``nodes``：搜索树访问的节点数 ｜ ``full_nodes``：不剪枝时的完整组合数
｜ ``evaluations``：真正调用报价函数的次数（耗时都在这里）

等价性是硬要求
--------------
剪枝只能省时间，**不允许改变答案**。测试里同一份输入跑 ``full`` 与 ``pruned`` 逐字段比对；
另有一组随机小规模用例与**独立实现**的暴力枚举对拍。下界写错会让测试直接红，
而不是静悄悄给出一个便宜但买不到的方案。

已知边界
--------
抵扣上界按"每组最大抵扣之和"估，是**可采纳但偏松**的下界。券池里封顶越紧，剪枝越有效；
若存在无封顶的折扣券（``discount`` 无 ``cap``），该组上界会退化成"不打折"，
剪枝基本不生效 —— **宁可少剪，也不能剪错**。``bound_quality()`` 会把这件事量化出来。
"""
from __future__ import annotations

import argparse
import itertools
import json
import math
import os
import sys
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)

from optimize_deals import ITEM_LEVEL_TYPES, Constraints, Coupon, Item  # noqa: E402

COMBOS_FULL = "full"        # 基线：笛卡尔积全量枚举（v3 现行）
COMBOS_PRUNED = "pruned"    # 分支定界

#: 给不出界时的哨兵。**刻意选一个大数而不是"随便填个合理值"** ——
#: 随手填一个看起来合理的数，就会把最优解剪掉。
UNBOUNDED = 10 ** 12


# --------------------------------------------------------------------------- #
# 统计
# --------------------------------------------------------------------------- #
@dataclass(slots=True)
class SearchStats:
    """一次搜索的账本。剪掉了多少、有没有改变答案，全靠它说话。"""

    method: str = COMBOS_FULL
    pool: int = 0
    k_min: int = 0
    k_max: int = 0
    nodes: int = 0             # 搜索树访问节点数（含中间前缀）
    leaves: int = 0            # 实际枚举到的完整组合数（= 调用报价函数的次数）
    full_nodes: int = 0        # v3 会枚举的原始组合数，含不可行组合
    evaluations: int = 0       # 真正调用报价函数的次数（耗时都在这里）
    cuts: int = 0              # 被**下界**整棵剪掉的子树数
    feasibility_cuts: int = 0  # 被**约束**（exclude / must_include 不可达）整棵剪掉的子树数
    best_cents: Optional[int] = None

    @property
    def saved_nodes(self) -> int:
        return max(0, self.full_nodes - self.leaves)

    @property
    def saved_ratio(self) -> float:
        return self.saved_nodes / self.full_nodes if self.full_nodes else 0.0

    @property
    def eval_ratio(self) -> float:
        return self.evaluations / self.full_nodes if self.full_nodes else 0.0

    def to_dict(self) -> Dict[str, Any]:
        return {"method": self.method, "pool": self.pool, "k": [self.k_min, self.k_max],
                "nodes": self.nodes, "leaves": self.leaves,
                "full_nodes": self.full_nodes, "evaluations": self.evaluations,
                "cuts": self.cuts, "feasibility_cuts": self.feasibility_cuts,
                "best_cents": self.best_cents,
                "saved_ratio": round(self.saved_ratio, 4),
                "eval_ratio": round(self.eval_ratio, 4)}


def count_combos(n: int, k_min: int, k_max: int) -> int:
    """``Σ_k C(n, k)`` —— 用 ``math.comb``（C 实现），不用手写阶乘循环。"""
    return sum(math.comb(n, k) for k in range(k_min, min(k_max, n) + 1))


# --------------------------------------------------------------------------- #
# 约束校验：位掩码版
# --------------------------------------------------------------------------- #
class ComboIndex:
    """把候选池预处理成**掩码 + 价格数组**，让每个节点的约束检查退化成位运算。

    组合数量级上万时，``{i.code for i in combo}`` 这种每个节点新建一个 set 的写法
    是主要的常数开销；换成整数掩码后，``must`` / ``exclude`` 只是两次 ``&``。
    """

    __slots__ = ("items", "prices", "codes", "must_mask", "exclude_mask",
                 "min_items", "max_items", "missing_required")

    def __init__(self, items: Sequence[Item], cons: Constraints) -> None:
        # 按价格升序 —— 剪枝依赖这个顺序：前缀越贵，后面的越不可能翻盘。
        ordered = sorted(items, key=lambda i: (i.price_cents, i.code))
        self.items: Tuple[Item, ...] = tuple(ordered)
        self.prices: Tuple[int, ...] = tuple(i.price_cents for i in ordered)
        self.codes: Tuple[str, ...] = tuple(i.code for i in ordered)
        pos = {code: i for i, code in enumerate(self.codes)}
        self.must_mask = 0
        for code in cons.must_include:
            if code in pos:
                self.must_mask |= 1 << pos[code]
        self.exclude_mask = 0
        for code in cons.exclude:
            if code in pos:
                self.exclude_mask |= 1 << pos[code]
        # 必选商品压根不在池子里 ⇒ 池子里任何组合都不可能满足要求。
        # 单独记下来，是为了能给出"为什么一个方案都没有"的原因，而不是静默返回空表。
        self.missing_required: Tuple[str, ...] = tuple(
            code for code in cons.must_include if code not in pos)
        self.min_items = int(cons.min_items)
        self.max_items = int(cons.max_items)

    def __len__(self) -> int:
        return len(self.items)

    def feasible(self, mask: int, count: int) -> bool:
        """位掩码版约束检查 —— 与 ``optimize_deals._satisfies`` 的前三条等价。

        这里是**参考写法**：搜索路径为了省常数用增量式判定（见 ``_visit_exact``），
        测试拿本函数与 ``_satisfies`` 全组合对拍，证明两者逐位等同。

        例外：``must_include`` 里的编码若不在池子里，掩码无从设置。那种情况由
        :func:`search` 读 ``missing_required`` 提前短路，不在本函数的职责内。
        """
        if not (self.min_items <= count <= self.max_items):
            return False
        if mask & self.must_mask != self.must_mask:
            return False
        return not (mask & self.exclude_mask)


# --------------------------------------------------------------------------- #
# 抵扣上界（必须可采纳）
# --------------------------------------------------------------------------- #
def coupon_ceiling(c: Coupon, pool: Sequence[Item]) -> int:
    """单张券在 **``pool`` 里任意购物车**上能给出的抵扣上界。

    逐条对齐 :func:`optimize_deals.single_coupon_discount` 的判定语义 ——
    尤其是**「券面缺字段 ⇒ 这张券永远用不上 ⇒ 上界是 0」**这一条：
    漏掉它会让上界被抬到"池子里最贵那件商品的价格"，整个下界随之失效
    （实测就踩过：一个没有 ``item_code`` 的 ``item_free`` 把上界抬到 ¥59.50，
    于是剪枝率从两位数掉到 2%）。

    类型不认识就返回 ``UNBOUNDED`` —— 宁可放弃剪枝，也不能凭空给界。

    ⚠️ ``pool`` 必须是**整个候选池**，不能是当前购物车：上界要对所有超集成立，
    拿当前购物车算会把上界低估，从而把下界抬高、把最优解剪掉。
    """
    t = str(getattr(c, "type", "") or "")
    by_code = {i.code: i.price_cents for i in pool}

    # 件数门槛本身就够不着（池子比门槛还小）⇒ 这张券用不上。
    # 只做"一定用不上"的排除：门槛低于池子大小时不作任何推断，那属于购物车层面的事。
    min_items = int(getattr(c, "min_items", 0) or 0)
    if min_items and min_items > len(pool):
        return 0

    if t == "threshold_reduce":
        return int(c.reduce_cents)
    if t in ("item_reduce", "item_free"):
        single = str(getattr(c, "item_code", "") or "")
        if not single or single not in by_code:
            return 0                      # 缺 item_code / 池里没有 → 用不上
        return by_code[single] if t == "item_free" else min(int(c.reduce_cents), by_code[single])
    if t == "buy_get":
        buy = str(getattr(c, "buy_code", "") or "")
        get = str(getattr(c, "get_code", "") or "")
        if not buy or not get or buy not in by_code or get not in by_code:
            return 0
        return by_code[get]
    if t == "discount":
        cap = int(getattr(c, "cap_cents", 0) or 0)
        return cap if cap > 0 else UNBOUNDED
    if t == "item_discount":
        single = str(getattr(c, "item_code", "") or "")
        if not single or single not in by_code:
            return 0
        rate = float(getattr(c, "rate", 0.0) or 0.0)
        cap = int(getattr(c, "cap_cents", 0) or 0)
        ceiling = int(round(by_code[single] * (1.0 - rate)))
        return min(ceiling, cap) if cap > 0 else ceiling
    if t == "bundle_price":
        bundle = tuple(str(x) for x in (getattr(c, "item_codes", ()) or ()))
        if not bundle:
            return 0
        if any(code not in by_code for code in bundle):
            return 0                      # 池子里凑不齐 → 永远用不上
        fixed = int(getattr(c, "fixed_cents", 0) or 0)
        return max(0, sum(by_code[code] for code in bundle) - fixed)
    return UNBOUNDED


def discount_ceiling(
    coupons: Sequence[Coupon],
    pool: Sequence[Item],
    *,
    allow_stack: bool = True,
    max_stack: Optional[int] = None,
    goods_cap: Optional[int] = None,
) -> int:
    """整池券在**一次报价**里能给出的抵扣上界。

    必须照着 :func:`channel_planner.apply_coupons` 的**两段式**结构来估：
    单品级券先落地，整单级券再作用于扣完后的金额。因此

    - 单品级：按互斥组取最大再求和 —— 组内最多生效 1 张
    - 整单级：同样按组取最大，但整单级还要受 ``max_stack`` 限流 ⇒ 只取**前 max_stack 个组**

    ⚠️ **绝不能把两段的组名混进一个字典**。``stack_group`` 只区分"同组互斥"，
    不含"单品级/整单级"的维度：一个空 ``stack_group`` 的单品券和一个空 ``stack_group``
    的整单券，引擎会让它们**同时生效**（分属两个阶段），而混着取最大值只算一张，
    上界就会**低于真实抵扣**，下界随之被抬高，最优解被静默剪掉。
    样例数据里「麦当劳·到店自取」正好是这种：默认组里同时挂着一张 ``item_free``
    和一张 ``threshold_reduce``。

    ``goods_cap`` 给的是"这个渠道上商品小计的**上**界"（按渠道标价系数折算的整池金额）。
    抵扣不可能超过它 —— 引擎里 ``item_total`` 与 ``middle`` 就是按这个金额封顶的。
    """
    if not coupons:
        return 0
    item_groups: Dict[str, int] = {}
    order_groups: Dict[str, int] = {}
    for c in coupons:
        ceiling = coupon_ceiling(c, pool)
        if ceiling >= UNBOUNDED:
            return UNBOUNDED
        key = str(getattr(c, "stack_group", "") or "__default__")
        is_item_level = str(getattr(c, "type", "")) in ITEM_LEVEL_TYPES
        bucket = item_groups if is_item_level else order_groups
        if ceiling > bucket.get(key, 0):
            bucket[key] = ceiling

    if not allow_stack:
        # 单张券竞争：所有券（不分阶段）同池比净收益，只有一张能落地。
        total = max([*item_groups.values(), *order_groups.values()], default=0)
    else:
        total = sum(item_groups.values()) + _top_sum(order_groups.values(), max_stack)

    if goods_cap is not None:
        total = min(total, int(goods_cap))
    return UNBOUNDED if total >= UNBOUNDED else total


def _top_sum(values: Any, n: Optional[int]) -> int:
    """取最大的 ``n`` 个求和；``n`` 为 None 表示不限。"""
    vals = sorted(values, reverse=True)
    if n is None:
        return sum(vals)
    return sum(vals[: max(1, int(n))])


@dataclass(slots=True)
class BoundInfo:
    """下界的三段构成 —— 打印出来，"这个界凭什么成立"就是可复核的。"""

    price_factor_min: float
    packaging_min_cents: int
    discount_ceiling_cents: int
    slack_cents: int
    unbounded: bool

    def explain(self) -> str:
        if self.unbounded:
            return ("抵扣上界给不出来（券池里有无封顶券）→ **不剪枝**。"
                    "宁可少剪，也不能剪错。")
        return (f"下界 = (小计 × {self.price_factor_min:g}) − {self.slack_cents} 分取整余量"
                f" − min(小计, {self.discount_ceiling_cents} 分) + {self.packaging_min_cents} 分打包费")


def make_bound(
    channels: Sequence[Any],
    pool: Sequence[Item],
    *,
    item_count: int,
) -> Tuple[Callable[[int], int], BoundInfo]:
    """构造"给定商品小计 ⇒ 到手成本下界"的函数，并返回它的构成说明。

    三段都必须**小于等于**真实值：商品按最小标价系数打折取整、配送费只能取 0
    （满额会被免掉）、抵扣不可能超过商品本身。积分机会成本 ≥ 0，不进下界。
    """
    if not channels:
        info = BoundInfo(1.0, 0, 0, 0, True)
        return (lambda goods: 0), info

    pf_min = min(float(getattr(ch, "price_factor", 1.0) or 1.0) for ch in channels)
    pack_min = min(int(getattr(ch, "packaging_fee_cents", 0) or 0) for ch in channels)
    pool_prices = [i.price_cents for i in pool]
    ceiling = 0
    for ch in channels:
        pf = float(getattr(ch, "price_factor", 1.0) or 1.0)
        # 这个渠道上商品小计的上界：整池都买一遍也就这么多，抵扣不可能超过它。
        goods_cap = sum(int(round(p * pf)) for p in pool_prices)
        ceiling = max(ceiling, discount_ceiling(
            getattr(ch, "coupons", ()) or (), pool,
            allow_stack=bool(getattr(ch, "allow_stack", False)),
            max_stack=int(getattr(ch, "max_stack", 1) or 1),
            goods_cap=goods_cap,
        ))
    unbounded = ceiling >= UNBOUNDED
    slack = max(1, int(item_count))                 # 每件商品留 1 分取整余量
    info = BoundInfo(pf_min, pack_min, ceiling, slack, unbounded)

    if unbounded:
        return (lambda goods: 0), info

    def bound(goods_cents: int) -> int:
        goods_lb = int(goods_cents * pf_min) - slack
        if goods_lb <= 0:
            return pack_min
        return goods_lb - min(goods_lb, ceiling) + pack_min

    return bound, info


# --------------------------------------------------------------------------- #
# 搜索
# --------------------------------------------------------------------------- #
class _Window:
    """维护"当前前 N 名里最差的那个成本"，作为剪枝阈值。

    这个值只会**变小不会变大**（新进来的只会更好），剪枝安全性就建立在这一点上：
    ``下界 > 当前最差`` 意味着这棵子树里每个组合都严格差于**最终**的前 N 名边界。
    """

    __slots__ = ("keep", "costs")

    def __init__(self, keep: int) -> None:
        self.keep = max(1, int(keep))
        self.costs: List[int] = []

    def offer(self, cost: int) -> None:
        self.costs.append(cost)
        if len(self.costs) > self.keep:
            self.costs.sort()
            del self.costs[self.keep:]

    def threshold(self) -> Optional[int]:
        return max(self.costs) if len(self.costs) >= self.keep else None


def search(
    index: ComboIndex,
    evaluate: Callable[[Tuple[Item, ...]], Optional[Tuple[Tuple[int, ...], Any]]],
    *,
    bound: Optional[Callable[[int], int]] = None,
    keep: int = 5,
    engine: str = COMBOS_FULL,
) -> Tuple[List[Tuple[Tuple[int, ...], Any]], SearchStats]:
    """在 ``index`` 上搜索。

    ``evaluate(combo)`` 返回 ``(排序键元组, 载荷)`` 或 ``None``（不可行）；
    键元组第 0 位必须是可以与下界直接比较的**成本（分）**，越小越好。

    两个引擎的**枚举顺序完全一致**（k 由小到大，组内按候选池下标的字典序），
    所以最终排序的稳定性也一致 —— 成本打平时不会各挑一个"同样便宜但不同"的方案。

    两者只在"下界剪枝"上不同：约束剪枝（``exclude`` / ``must_include`` 可达性）
    两个引擎都开，它不是启发式。因此 ``stats.method`` 只反映下界剪枝有没有真的用上 ——
    传了 ``engine=pruned`` 却没给 ``bound``，会如实记成 ``full``。
    """
    n = len(index)
    k_min, k_max = index.min_items, min(index.max_items, n)
    stats = SearchStats(method=engine, pool=n, k_min=k_min, k_max=k_max,
                        full_nodes=count_combos(n, k_min, k_max))
    use_prune = engine == COMBOS_PRUNED and bound is not None
    stats.method = COMBOS_PRUNED if use_prune else COMBOS_FULL
    results: List[Tuple[Tuple[int, ...], Any]] = []
    if n == 0 or k_max < k_min:
        return results, stats
    if index.missing_required:
        # 必选商品不在候选池里 ⇒ 池子里每个组合都不满足要求，一个都不用算。
        # 提前返回是为了让调用方能从 missing_required 拿到可解释的原因。
        return results, stats

    window = _Window(keep)

    for k in range(k_min, k_max + 1):
        _visit_exact(index, k, stats, evaluate, bound if use_prune else None,
                     results, window)

    results.sort(key=lambda r: r[0])
    stats.best_cents = results[0][0][0] if results else None
    return results[:keep], stats


def _visit_exact(index: ComboIndex, k: int, stats: SearchStats, evaluate, bound,
                 results: List[Any], window: "_Window") -> None:
    """枚举**恰好 k 件**的子集，顺序与 ``itertools.combinations(pool, k)`` 一致。

    约束剪枝在 ``full`` 与 ``pruned`` 两个引擎里**都开**：它不是启发式，而是把
    v3 在叶子上的 ``_satisfies`` 判定提前，判定结果逐位等同（有测试对拍）。
    这样两个引擎的差异就只剩"下界剪枝"一件事，对照才干净。
    """
    n = len(index)
    prices = index.prices
    items = index.items
    must = index.must_mask
    excl = index.exclude_mask
    picked: List[Item] = []

    def walk(start: int, goods: int, mask: int) -> None:
        stats.nodes += 1
        need = k - len(picked)
        # must_include 可达性：剩下的坑位盖不住还没选上的必选项 ⇒ 整棵子树不可行
        if (must & ~mask).bit_count() > need:
            stats.feasibility_cuts += 1
            return
        if need == 0:
            stats.leaves += 1
            stats.evaluations += 1
            got = evaluate(tuple(picked))
            if got is not None:
                results.append(got)
                window.offer(got[0][0])
            return
        if n - start < need:
            return
        # 阈值在每个节点取一次即可：window 只会变小不会变大，
        # 拿着一个偏大的旧阈值继续跑只会"少剪"，不会剪错。
        thr = window.threshold() if bound is not None else None
        last = n - need                       # 还差 need 件，起点不能越过这里
        for i in range(start, last + 1):
            bit = 1 << i
            if bit & excl:
                stats.feasibility_cuts += 1
                continue                      # 这一件出局，后面的兄弟仍可能可行
            next_goods = goods + prices[i]
            if thr is not None and bound(next_goods) > thr:
                # 价格升序 ⇒ 这个孩子赢不了，它后面的兄弟同样赢不了，整段跳出。
                stats.cuts += 1
                break
            picked.append(items[i])
            walk(i + 1, next_goods, mask | bit)
            picked.pop()

    walk(0, 0, 0)


# --------------------------------------------------------------------------- #
# 对拍：暴力枚举（独立实现，小规模专用）
# --------------------------------------------------------------------------- #
def brute_force(
    items: Sequence[Item],
    cons: Constraints,
    evaluate: Callable[[Tuple[Item, ...]], Optional[Tuple[Tuple[int, ...], Any]]],
    *,
    keep: int = 5,
) -> List[Tuple[Tuple[int, ...], Any]]:
    """第三方裁判：每个 k 的笛卡尔积完整跑一遍，不做任何剪枝。

    与 ``search`` 用的是**独立写法**，所以两边对上才是真信号，
    而不是同一段逻辑的自证。
    """
    n = len(items)
    out: List[Tuple[Tuple[int, ...], Any]] = []
    for k in range(cons.min_items, min(cons.max_items, n) + 1):
        for combo in itertools.combinations(items, k):
            codes = {i.code for i in combo}
            if any(c not in codes for c in cons.must_include):
                continue
            if any(c in codes for c in cons.exclude):
                continue
            got = evaluate(combo)
            if got is not None:
                out.append(got)
    out.sort(key=lambda r: r[0])
    return out[:keep]


# --------------------------------------------------------------------------- #
# 输出
# --------------------------------------------------------------------------- #
def print_stats(stats: SearchStats, *, title: str = "") -> None:
    from termtext import columns, rule, table_width  # noqa: PLC0415

    specs = [("指标", 22, "<"), ("值", 14, ">"), ("", 2, "<"), ("说明", 0, "<")]
    rows = [
        ("引擎", stats.method, "", "full=全量枚举基线 / pruned=分支定界"),
        ("候选池", f"{stats.pool} 项", "", f"组合区间 k={stats.k_min}..{stats.k_max}"),
        ("原始组合数", f"{stats.full_nodes:,}", "", "v3 会枚举的全部组合（含不可行组合）"),
        ("实际枚举", f"{stats.leaves:,}", "",
         f"比原始组合少 {stats.saved_nodes:,}（{stats.saved_ratio * 100:.1f}%）"),
        ("报价调用", f"{stats.evaluations:,}", "",
         f"占原始组合的 {stats.eval_ratio * 100:.1f}%"),
        ("下界剪枝", f"{stats.cuts:,}", "", "被「再涨也赢不了」整棵砍掉的子树"),
        ("约束剪枝", f"{stats.feasibility_cuts:,}", "", "被 exclude / must_include 砍掉的子树"),
        ("访问节点", f"{stats.nodes:,}", "", "含中间前缀"),
    ]
    if title:
        print(f"  {title}")
    print(rule(table_width(specs)))
    for line in columns(specs, rows):
        print(line)
    print(rule(table_width(specs)))


def print_bound(info: BoundInfo) -> None:
    print(f"  下界构成：{info.explain()}")


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def _load(path: str) -> Any:
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description="组合枚举剪枝：枚举量 / 耗时 / 等价性对照")
    ap.add_argument("--items", default=os.path.join(ROOT, "data", "sample_menu.json"))
    ap.add_argument("--channels", default=os.path.join(ROOT, "data", "sample_channels.json"))
    ap.add_argument("--min-items", type=int, default=1)
    ap.add_argument("--max-items", type=int, default=3)
    ap.add_argument("--shadow-ypp", type=float, default=0.138)
    ap.add_argument("--top", type=int, default=5)
    ap.add_argument("--json", action="store_true")
    return ap


def main(argv: Optional[List[str]] = None) -> int:
    import time  # noqa: PLC0415

    from channel_planner import _channels, _items, compare_channels  # noqa: PLC0415

    args = build_parser().parse_args(argv)
    items = _items(args.items)
    channels = _channels(args.channels)
    cons = Constraints(min_items=args.min_items, max_items=args.max_items)
    index = ComboIndex(items, cons)
    bound_fn, info = make_bound(channels, index.items, item_count=args.max_items)

    def evaluate(combo):
        quotes = compare_channels(list(combo), channels, shadow_ypp=args.shadow_ypp)
        ok = [q for q in quotes if q.feasible]
        if not ok:
            return None
        best = min(ok, key=lambda q: q.landed_cents)
        return (best.landed_cents, len(combo)), tuple(sorted(i.code for i in combo))

    out: Dict[str, Any] = {}
    for engine in (COMBOS_FULL, COMBOS_PRUNED):
        t0 = time.perf_counter()
        res, st = search(index, evaluate, bound=bound_fn, keep=args.top, engine=engine)
        ms = (time.perf_counter() - t0) * 1000
        out[engine] = {
            "stats": st.to_dict(), "ms": round(ms, 2),
            "top": [{"cost_cents": k[0], "items": sorted(v)} for k, v in res],
        }
        if not args.json:
            print_stats(st, title=f"── {engine} ──  {ms:.1f} ms")
            for i, (key, codes) in enumerate(res, 1):
                print(f"    {i}. ¥{key[0] / 100:.2f}  {' + '.join(sorted(codes))}")
            print()

    if args.json:
        print(json.dumps({**out, "bound": info.explain()}, ensure_ascii=False, indent=2))
        return 0

    print_bound(info)
    a, b = out[COMBOS_FULL], out[COMBOS_PRUNED]
    same = a["top"] == b["top"]
    print(f"\n  结果一致：{'是' if same else '** 否 —— 剪枝改变了答案，这是 bug **'}")
    if not same:
        return 1
    if b["ms"] > 0:
        print(f"  耗时：{a['ms']:.1f} ms → {b['ms']:.1f} ms（{a['ms'] / b['ms']:.2f}×）")
    fa, fb = a["stats"], b["stats"]
    # 两个比率分开报：混成一个数字会把"约束本来就该过滤的"算进剪枝的战功里。
    print(f"  相对 v3 原始组合数（{fa['full_nodes']:,}）："
          f"{fa['leaves']:,} → {fb['leaves']:,}（总省 {fb['saved_ratio'] * 100:.1f}%）")
    bound_saved = fa["leaves"] - fb["leaves"]
    pct = (bound_saved / fa["leaves"] * 100) if fa["leaves"] else 0.0
    print(f"  其中**下界剪枝**单独贡献：{bound_saved:,} 个组合（占可行组合的 {pct:.1f}%）"
          f"，砍掉 {fb['cuts']:,} 棵子树")
    return 0


if __name__ == "__main__":
    sys.exit(main())
