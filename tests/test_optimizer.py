#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""优化引擎单元测试（离线运行，无需 MCP Token）。

    python -m unittest discover -s tests -v
"""
from __future__ import annotations

import json
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

from optimize_deals import (  # noqa: E402
    Constraints,
    Coupon,
    Item,
    evaluate,
    estimate_best_coupon,
    optimize,
    to_cents,
    fmt_yuan,
)

DATA = os.path.join(os.path.dirname(__file__), "..", "data")


def load_items():
    with open(os.path.join(DATA, "sample_menu.json"), encoding="utf-8") as f:
        return [Item.from_dict(d) for d in json.load(f)]


def load_coupons():
    with open(os.path.join(DATA, "sample_coupons.json"), encoding="utf-8") as f:
        return [Coupon.from_dict(d) for d in json.load(f)]


class TestMoney(unittest.TestCase):
    def test_to_cents(self):
        self.assertEqual(to_cents(24.5), 2450)
        self.assertEqual(to_cents("12.5"), 1250)
        self.assertEqual(to_cents(8), 800)

    def test_fmt(self):
        self.assertEqual(fmt_yuan(2450), "¥24.50")


class TestCouponEstimate(unittest.TestCase):
    def setUp(self):
        self.items = {i.code: i for i in load_items()}
        self.coupons = load_coupons()

    def test_threshold_applies(self):
        combo = [self.items["BURGER-BIGMAC"], self.items["FRIES-M"]]  # 24.5 + 12 = 36.5 >= 30
        coupon, discount = estimate_best_coupon(combo, self.coupons)
        self.assertEqual(discount, 500)
        self.assertIsNotNone(coupon)

    def test_item_reduce(self):
        combo = [self.items["CHICKEN-MCNUGGETS-5"], self.items["COLA-M"]]
        coupon, discount = estimate_best_coupon(combo, self.coupons)
        self.assertEqual(coupon.id, "C2")
        self.assertEqual(discount, 400)

    def test_buy_get_requires_both(self):
        # 仅含中薯条：不满足"买薯条送可乐"
        coupon, _ = estimate_best_coupon([self.items["FRIES-M"]], self.coupons)
        self.assertNotEqual(coupon.id if coupon else None, "C3")
        # 同时含两者：可乐免单（9.5 元）
        coupon, discount = estimate_best_coupon(
            [self.items["FRIES-M"], self.items["COLA-M"]], self.coupons
        )
        self.assertEqual(coupon.id, "C3")
        self.assertEqual(discount, 950)

    def test_picks_max_discount(self):
        # 圣代单独买 8 元：C4（免费，减 8 元）应优于其他
        combo = [self.items["SUNDAE"]]
        coupon, discount = estimate_best_coupon(combo, self.coupons)
        self.assertEqual(coupon.id, "C4")
        self.assertEqual(discount, 800)


class TestConstraints(unittest.TestCase):
    def setUp(self):
        self.items = load_items()
        self.coupons = load_coupons()

    def test_budget_filter(self):
        cons = Constraints(budget_cents=to_cents(20), max_items=3)
        plans, _ = optimize(self.items, self.coupons, cons, top=5)
        self.assertTrue(all(p.final_cents <= 2000 for p in plans))

    def test_must_include(self):
        cons = Constraints(must_include=("BURGER-BIGMAC",), max_items=3)
        plans, _ = optimize(self.items, self.coupons, cons, top=5)
        self.assertTrue(plans)
        for p in plans:
            self.assertIn("BURGER-BIGMAC", [i.code for i in p.items])

    def test_exclude(self):
        cons = Constraints(exclude=("BURGER-BIGMAC",), max_items=2)
        plans, _ = optimize(self.items, self.coupons, cons, top=5)
        for p in plans:
            self.assertNotIn("BURGER-BIGMAC", [i.code for i in p.items])

    def test_calorie_cap(self):
        cons = Constraints(max_calories=400, max_items=2)
        plans, _ = optimize(self.items, self.coupons, cons, top=5)
        for p in plans:
            self.assertIsNotNone(p.calories)
            self.assertLessEqual(p.calories, 400)

    def test_sorted_ascending(self):
        cons = Constraints(max_items=3)
        plans, _ = optimize(self.items, self.coupons, cons, top=10)
        finals = [p.final_cents for p in plans]
        self.assertEqual(finals, sorted(finals))

    def test_infeasible_returns_empty(self):
        # 必点商品不在菜单中 → 无解（注：不能只靠极小预算，因为存在"免费"券）
        cons = Constraints(must_include=("NOT-EXIST",), max_items=2)
        plans, _ = optimize(self.items, self.coupons, cons, top=5)
        self.assertEqual(plans, [])


class TestEvaluate(unittest.TestCase):
    def test_saved_pct(self):
        items = load_items()
        combo = [i for i in items if i.code in ("BURGER-BIGMAC", "FRIES-M")]
        plan = evaluate(combo, load_coupons(), Constraints(max_items=2))
        self.assertIsNotNone(plan)
        self.assertEqual(plan.subtotal_cents, 3650)
        self.assertEqual(plan.final_cents, 3150)
        self.assertGreater(plan.saved_pct, 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
