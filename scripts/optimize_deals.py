#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""麦麦省钱精算师 —— 本地组合优化引擎（Stage 1 剪枝层）。

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


# --------------------------------------------------------------------------- #
# 数据模型
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
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


@dataclass(frozen=True)
class Coupon:
    """优惠券的通用描述。

    type 取值（估算语义）：
      - ``threshold_reduce``：满 threshold 元减 reduce 元
      - ``item_reduce``     ：指定商品 item_code 立减 reduce 元
      - ``buy_get``         ：买 buy_code 送 get_code（购物车需同时含两者，get_code 免单）
      - ``item_free``       ：指定商品 item_code 免费
    """
    id: str
    name: str
    type: str
    threshold_cents: int = 0
    reduce_cents: int = 0
    item_code: str = ""
    buy_code: str = ""
    get_code: str = ""

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
        )


@dataclass
class Constraints:
    budget_cents: Optional[int] = None
    min_items: int = 1
    max_items: int = 4
    max_calories: Optional[float] = None
    must_include: Tuple[str, ...] = ()
    exclude: Tuple[str, ...] = ()


@dataclass
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
def estimate_best_coupon(combo: List[Item], coupons: Iterable[Coupon]) -> Tuple[Optional[Coupon], int]:
    """返回 (最优券, 抵扣分)。采用保守假设：单笔仅使用 1 张最优券。"""
    subtotal = sum(i.price_cents for i in combo)
    codes = {i.code for i in combo}
    best_coupon: Optional[Coupon] = None
    best_discount = 0

    for c in coupons:
        if c.type == "threshold_reduce":
            if subtotal >= c.threshold_cents:
                d = min(c.reduce_cents, subtotal)
            else:
                continue
        elif c.type in ("item_reduce", "item_free"):
            if not c.item_code or c.item_code not in codes:
                continue
            target = next(i for i in combo if i.code == c.item_code)
            d = target.price_cents if c.type == "item_free" else min(c.reduce_cents, target.price_cents)
        elif c.type == "buy_get":
            # 需购物车同时含 buy_code 与 get_code，赠送商品（get_code）免单
            if not c.buy_code or not c.get_code:
                continue
            if c.buy_code not in codes or c.get_code not in codes:
                continue
            target = next(i for i in combo if i.code == c.get_code)
            d = target.price_cents
        else:
            continue

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
    """组合数过多时，先用启发式缩减候选集（保留必点项与低价项）。"""
    n = len(items)
    total = 0
    for k in range(cons.min_items, min(cons.max_items, n) + 1):
        total += _comb(n, k)
    if total <= max_combos:
        return items, False

    keep: Dict[str, Item] = {}
    for it in items:
        if it.code in cons.must_include:
            keep[it.code] = it
    for it in sorted(items, key=lambda x: x.price_cents):
        if len(keep) >= max(cons.max_items * 3, cons.max_items + 4):
            break
        keep.setdefault(it.code, it)
    return list(keep.values()), True


def _comb(n: int, k: int) -> int:
    if k < 0 or k > n:
        return 0
    r = 1
    for i in range(1, k + 1):
        r = r * (n - i + 1) // i
    return r


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
    p = argparse.ArgumentParser(description="麦麦省钱精算师 · 本地组合优化引擎")
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
