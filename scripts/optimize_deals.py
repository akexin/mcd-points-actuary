#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""麦麦积分精算师 · v1 能力：点餐组合优化引擎（Stage 1 剪枝层）。

> 说明：v2 主线已转向「积分资产最优配置」（见 ``points_engine.py``）。
> 本模块作为 v1 能力保留 —— 它把"怎么买最省"算到极致，
> 同时也沉淀了两阶段架构（本地剪枝 + MCP 精算）的工程范式。

职责
----
麦当劳 MCP 提供的是"精确计价"能力（``calculate-price``），单次只能算一个购物车。
若把门店菜单的所有组合都丢给 MCP 计价，会迅速撞上 **600 次/分钟** 的限流。
因此本引擎负责：

1. 在本地对候选组合做**穷举 + 约束剪枝**，算出一个**估算到手价**；
2. 只把最有希望省钱的 Top-K 组合交给 MCP 精确计价（Stage 2，由 Agent 编排）。

设计原则
--------
- 本模块**不发起任何网络请求**，完全可离线运行、可单元测试。
- 金额一律以 **分（int）** 参与计算，避免浮点误差；仅在输出时转元。
- 估算券逻辑是**保守近似**：真实优惠以 MCP ``calculate-price`` 返回为准。

用法
----
    python optimize_deals.py --menu data/sample_menu.json \
        --coupons data/sample_coupons.json --max-items 4 --budget 40 --top 10
"""
from __future__ import annotations

import argparse
import itertools
import json
import math
import sys
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Tuple

DISCLAIMER = "估算结果仅供排序参考，最终优惠与应付金额以麦当劳 MCP calculate-price 为准。"


# --------------------------------------------------------------------------- #
# 金额工具
# --------------------------------------------------------------------------- #
def to_cents(value: Any) -> int:
    """将"元"（int/float/str）安全转换为"分"整数。"""
    if isinstance(value, int):
        return value * 100
    if isinstance(value, float):
        return int(round(value * 100))
    if isinstance(value, str):
        return int(round(float(value.strip().replace("¥", "").replace("元", "")) * 100))
    raise TypeError(f"无法解析金额: {value!r}")


def fmt_yuan(cents: int) -> str:
    return f"¥{cents / 100:.2f}"


# 手写的组合数被换成标准库实现 —— ``math.comb`` 是 C 版本，在剪枝预判的热路径上
# 每轮都要调上百次，这里的差距不是"风格问题"。（见 tools/bench.py 的对照。）
_comb = math.comb


# --------------------------------------------------------------------------- #
# 数据模型
# --------------------------------------------------------------------------- #
@dataclass(frozen=True, slots=True)
class Item:
    code: str
    name: str
    price_cents: int
    category: str = ""
    calories: Optional[float] = None
    tags: Tuple[str, ...] = ()

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "Item":
        return cls(
            code=str(d["code"]),
            name=str(d.get("name", d["code"])),
            price_cents=to_cents(d.get("price", 0)),
            category=str(d.get("category", "")),
            calories=(float(d["calories"]) if d.get("calories") is not None else None),
            tags=tuple(d.get("tags", ()) or ()),
        )


@dataclass(frozen=True, slots=True)
class Coupon:
    """优惠券的通用描述（跨渠道统一模型）。

    type 取值（估算语义）：
      - ``threshold_reduce``：满 threshold 元减 reduce 元
      - ``item_reduce``     ：指定商品 item_code 立减 reduce 元
      - ``buy_get``         ：买 buy_code 送 get_code（购物车需同时含两者，get_code 免单）
      - ``item_free``       ：指定商品 item_code 免费
      - ``discount``        ：整单折扣，rate 为折后比例（0.8 = 8 折），cap_cents 为封顶
      - ``item_discount``   ：单品折扣，作用于 item_code 这件商品
      - ``bundle_price``    ：**组合特价**，购物车含齐 ``item_codes`` 全部商品时，
        这一组按 ``fixed_cents`` 结算。用于表达积分商城与平台最常见的
        "X 元买这个组合"形态（例：「21.9 元巨无霸可乐组合」）。

    v3 新增字段用于**跨平台**场景（第三方外卖平台与官方渠道规则差异很大）：
      - ``rate`` / ``cap_cents``：折扣率与封顶额，支撑"8 折""半价周末"这类形态
      - ``item_codes`` / ``fixed_cents``：组合特价券的适用商品集合与打包价
      - ``point_cost``：获取这张券要消耗的积分（0 = 不需积分）。
        积分是**有成本的**（有更好的用途），因此渠道定价时必须把它算进到手成本。
      - ``stack_group``：**互斥组**。同组内只能选一张，不同组可以叠加；
        空字符串视为同一个默认组 —— 因此"单笔只用 1 张券"的 v1 语义天然包含在内。
      - ``min_items``：最少件数门槛

    限制（**必须显式声明**）：``bundle_price`` 要求每个 ``item_codes`` 商品
    在购物车中**各出现一次**。购物车模型是"商品集合"，无法表达"2 份某单品"，
    因此「X 元 2 份 Y」这类券不适用本模型，需在数据层排除而非勉强近似。
    """
    id: str
    name: str
    type: str
    threshold_cents: int = 0
    reduce_cents: int = 0
    item_code: str = ""
    buy_code: str = ""
    get_code: str = ""
    # ---- v3：跨平台与叠加 ----
    rate: float = 0.0
    cap_cents: int = 0
    point_cost: int = 0
    stack_group: str = ""
    channel: str = ""
    min_items: int = 0
    # ---- v3：组合特价 ----
    item_codes: Tuple[str, ...] = ()
    fixed_cents: int = 0

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "Coupon":
        return cls(
            id=str(d["id"]),
            name=str(d.get("name", d["id"])),
            type=str(d["type"]),
            threshold_cents=to_cents(d.get("threshold", 0)),
            reduce_cents=to_cents(d.get("reduce", 0)),
            item_code=str(d.get("item_code", "")),
            buy_code=str(d.get("buy_code", "")),
            get_code=str(d.get("get_code", "")),
            rate=float(d.get("rate", 0) or 0),
            cap_cents=to_cents(d.get("cap", 0)),
            point_cost=int(d.get("point_cost", 0) or 0),
            stack_group=str(d.get("stack_group", "") or ""),
            channel=str(d.get("channel", "") or ""),
            min_items=int(d.get("min_items", 0) or 0),
            item_codes=tuple(str(c) for c in (d.get("item_codes", ()) or ())),
            fixed_cents=to_cents(d.get("fixed", 0)),
        )


#: 单品级券型：作用于**单件商品或指定组合**，先于整单级券落地。
#: 放在这里而不是渠道层，是因为它属于"券的语义"本身 —— 券语义的唯一实现是本模块，
#: ``channel_planner`` 与 ``coupon_solver`` 都从这里取，避免两处各写一份导致漂移。
ITEM_LEVEL_TYPES = frozenset({"item_free", "item_reduce", "item_discount", "buy_get", "bundle_price"})


@dataclass(slots=True)
class Constraints:
    budget_cents: Optional[int] = None
    min_items: int = 1
    max_items: int = 4
    max_calories: Optional[float] = None
    must_include: Tuple[str, ...] = ()
    exclude: Tuple[str, ...] = ()


@dataclass(slots=True)
class Plan:
    items: List[Item]
    subtotal_cents: int
    coupon_id: Optional[str]
    coupon_name: Optional[str]
    discount_cents: int
    final_cents: int
    calories: Optional[float]

    @property
    def saved_pct(self) -> float:
        return round(self.discount_cents / self.subtotal_cents * 100, 1) if self.subtotal_cents else 0.0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "items": [{"code": i.code, "name": i.name, "price": fmt_yuan(i.price_cents)} for i in self.items],
            "item_codes": [i.code for i in self.items],
            "subtotal": fmt_yuan(self.subtotal_cents),
            "coupon": self.coupon_name,
            "coupon_id": self.coupon_id,
            "discount": fmt_yuan(self.discount_cents),
            "final_price": fmt_yuan(self.final_cents),
            "final_price_cents": self.final_cents,
            "calories": self.calories,
            "saved_pct": self.saved_pct,
        }


# --------------------------------------------------------------------------- #
# 优惠券估算
# --------------------------------------------------------------------------- #
def single_coupon_discount(combo: List[Item], c: Coupon, subtotal: Optional[int] = None) -> int:
    """计算**单张**券在给定购物车上的抵扣额（分）；不可用则返回 0。

    本函数是券体系唯一的判定入口 —— 渠道叠加求解、积分成本核算都建立在它之上，
    避免"同一套券语义在两处各写一遍"导致的漂移。
    """
    if not combo:
        return 0
    if subtotal is None:
        subtotal = sum(i.price_cents for i in combo)
    if c.min_items and len(combo) < c.min_items:
        return 0
    codes = {i.code for i in combo}

    if c.type == "threshold_reduce":
        if subtotal < c.threshold_cents:
            return 0
        return min(c.reduce_cents, subtotal)

    if c.type in ("item_reduce", "item_free"):
        if not c.item_code or c.item_code not in codes:
            return 0
        target = next(i for i in combo if i.code == c.item_code)
        if c.type == "item_free":
            return target.price_cents
        return min(c.reduce_cents, target.price_cents)

    if c.type == "buy_get":
        # 需购物车同时含 buy_code 与 get_code，赠送商品（get_code）免单
        if not c.buy_code or not c.get_code:
            return 0
        if c.buy_code not in codes or c.get_code not in codes:
            return 0
        target = next(i for i in combo if i.code == c.get_code)
        return target.price_cents

    if c.type == "discount":
        # 整单折扣：rate 为折后比例（0.8 = 8 折），cap_cents 为封顶
        if subtotal < c.threshold_cents:
            return 0
        d = int(round(subtotal * (1.0 - c.rate)))
        if c.cap_cents:
            d = min(d, c.cap_cents)
        return min(d, subtotal)

    if c.type == "item_discount":
        # 单品折扣：第三方平台常见的"半价周末""单品直降"
        if not c.item_code or c.item_code not in codes:
            return 0
        target = next(i for i in combo if i.code == c.item_code)
        d = int(round(target.price_cents * (1.0 - c.rate)))
        if c.cap_cents:
            d = min(d, c.cap_cents)
        return min(d, target.price_cents)

    if c.type == "bundle_price":
        # 组合特价：含齐 item_codes 全部商品时，这一组按 fixed_cents 结算
        if not c.item_codes or not set(c.item_codes) <= codes:
            return 0
        group = sum(i.price_cents for i in combo if i.code in set(c.item_codes))
        return max(0, min(group - c.fixed_cents, subtotal))

    return 0


def estimate_best_coupon(combo: List[Item], coupons: Iterable[Coupon]) -> Tuple[Optional[Coupon], int]:
    """返回 (最优券, 抵扣分)。采用保守假设：单笔仅使用 1 张最优券。"""
    subtotal = sum(i.price_cents for i in combo)
    best_coupon: Optional[Coupon] = None
    best_discount = 0
    for c in coupons:
        d = single_coupon_discount(combo, c, subtotal)
        if d > best_discount:
            best_discount, best_coupon = d, c
    return best_coupon, best_discount


# --------------------------------------------------------------------------- #
# 约束校验与评估
# --------------------------------------------------------------------------- #
def _satisfies(combo: List[Item], cons: Constraints) -> bool:
    codes = {i.code for i in combo}
    if not cons.min_items <= len(combo) <= cons.max_items:
        return False
    if any(code not in codes for code in cons.must_include):
        return False
    if any(code in codes for code in cons.exclude):
        return False
    if cons.max_calories is not None:
        if any(i.calories is None for i in combo):
            return False  # 有热量约束但数据缺失 → 视为不可行，交由 MCP 复核
        if sum(i.calories for i in combo) > cons.max_calories:
            return False
    return True


def evaluate(combo: List[Item], coupons: Iterable[Coupon], cons: Constraints) -> Optional[Plan]:
    if not _satisfies(combo, cons):
        return None
    subtotal = sum(i.price_cents for i in combo)
    coupon, discount = estimate_best_coupon(combo, coupons)
    final = subtotal - discount
    if cons.budget_cents is not None and final > cons.budget_cents:
        return None
    calories = sum(i.calories for i in combo) if all(i.calories is not None for i in combo) else None
    return Plan(
        items=list(combo),
        subtotal_cents=subtotal,
        coupon_id=coupon.id if coupon else None,
        coupon_name=coupon.name if coupon else None,
        discount_cents=discount,
        final_cents=final,
        calories=calories,
    )


# --------------------------------------------------------------------------- #
# 枚举与剪枝
# --------------------------------------------------------------------------- #
def _prune_items(items: List[Item], cons: Constraints, max_combos: int) -> Tuple[List[Item], bool]:
    """组合数过多时，用启发式缩减候选集。

    关键点：**不能只保留最便宜的商品** —— 那样在真实菜单上会选出全是调味酱的组合。
    改为**按分类轮转取样**（每个分类各取最便宜的若干项），保证候选池覆盖主食、小食、
    饮品等多个品类，同时把组合数压到可控范围。
    """
    n = len(items)
    total = 0
    for k in range(cons.min_items, min(cons.max_items, n) + 1):
        total += _comb(n, k)
    if total <= max_combos:
        return items, False

    keep: Dict[str, Item] = {it.code: it for it in items if it.code in cons.must_include}

    groups: Dict[str, List[Item]] = {}
    for it in sorted(items, key=lambda x: (x.price_cents, x.code)):
        groups.setdefault(it.category or "未分类", []).append(it)

    target = max(cons.max_items * 3, cons.max_items + 4)
    idx = 0
    while len(keep) < target:
        progressed = False
        for key in sorted(groups):
            bucket = groups[key]
            if idx < len(bucket):
                keep.setdefault(bucket[idx].code, bucket[idx])
                progressed = True
                if len(keep) >= target:
                    break
        if not progressed:  # 所有分类都已取尽
            break
        idx += 1
    return list(keep.values()), True




def optimize(
    items: List[Item],
    coupons: List[Coupon],
    cons: Constraints,
    top: int = 10,
    max_combos: int = 200_000,
) -> Tuple[List[Plan], bool]:
    """返回 (按到手价升序的 Top-N 方案, 是否启用了启发式剪枝)。"""
    pool, pruned = _prune_items(items, cons, max_combos)
    plans: List[Plan] = []
    n = len(pool)
    for k in range(cons.min_items, min(cons.max_items, n) + 1):
        for combo in itertools.combinations(pool, k):
            plan = evaluate(list(combo), coupons, cons)
            if plan is not None:
                plans.append(plan)

    plans.sort(key=lambda p: (p.final_cents, -p.discount_cents, len(p.items)))
    return plans[:top], pruned


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def _load(path: str) -> Any:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def _parse_constraints(args: argparse.Namespace) -> Constraints:
    return Constraints(
        budget_cents=to_cents(args.budget) if args.budget is not None else None,
        min_items=args.min_items,
        max_items=args.max_items,
        max_calories=args.max_calories,
        must_include=tuple(args.must_include or ()),
        exclude=tuple(args.exclude or ()),
    )


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="v1 能力 · 点餐组合优化引擎（本地）")
    p.add_argument("--menu", required=True, help="门店菜单 JSON（含 code/name/price，可选 calories/category/tags）")
    p.add_argument("--coupons", default=None, help="优惠券 JSON（可选）")
    p.add_argument("--min-items", type=int, default=1)
    p.add_argument("--max-items", type=int, default=4)
    p.add_argument("--budget", default=None, help="预算上限（元）")
    p.add_argument("--max-calories", type=float, default=None)
    p.add_argument("--must-include", nargs="*", default=None, help="必点商品 code")
    p.add_argument("--exclude", nargs="*", default=None, help="排除商品 code")
    p.add_argument("--top", type=int, default=10)
    p.add_argument("--json", action="store_true", help="以 JSON 输出")
    return p


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    items = [Item.from_dict(d) for d in _load(args.menu)]
    coupons = [Coupon.from_dict(d) for d in _load(args.coupons)] if args.coupons else []
    cons = _parse_constraints(args)
    plans, pruned = optimize(items, coupons, cons, top=args.top)

    if args.json:
        print(json.dumps(
            {"disclaimer": DISCLAIMER, "pruned": pruned, "plans": [p.to_dict() for p in plans]},
            ensure_ascii=False, indent=2,
        ))
        return 0

    if not plans:
        print("未找到满足约束的组合。请放宽 --budget / --max-items 或检查 --must-include 是否在菜单中。")
        return 1

    print(f"共 {len(plans)} 个候选方案（{'已启用启发式剪枝' if pruned else '全量枚举'}）：\n")
    for rank, p in enumerate(plans, 1):
        names = " + ".join(i.name for i in p.items)
        print(f"{rank:>2}. 到手 {fmt_yuan(p.final_cents)}  |  原价 {fmt_yuan(p.subtotal_cents)}"
              f"  省 {fmt_yuan(p.discount_cents)} ({p.saved_pct}%)")
        print(f"    {names}")
        print(f"    券：{p.coupon_name or '无'}"
              + (f"   热量：{p.calories:.0f} kcal" if p.calories is not None else ""))
    print(f"\n{DISCLAIMER}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
