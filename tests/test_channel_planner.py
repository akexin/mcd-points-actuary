#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""v3 跨渠道到手成本规划器 —— 单元测试（离线，无需 MCP Token）。

覆盖重点：
  1. 新增券型（折扣 / 单品折扣 / 件数门槛）的抵扣计算
  2. 券叠加：同组互斥、跨组叠加、叠加张数限流
  3. **积分机会成本必须参与选券** —— 这是 v3 最核心的判据，
     也是实现过程中真实踩过的坑（见 ``test_point_coupon_not_chosen_when_costly``）
  4. 渠道费用：起送价、配送费、满额免配送、平台标价系数
  5. 跨渠道比价与全渠道组合求解的目标函数与预算判定
"""
from __future__ import annotations

import json
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

from channel_planner import (  # noqa: E402
    Channel, ChannelPlan, apply_coupons, breakeven_ypp, channel_savings,
    compare_channels, optimize_across_channels, point_bundle_coupon, point_coupon,
    price_in_channel,
)
from optimize_deals import (  # noqa: E402
    Constraints, Coupon, Item, fmt_yuan, single_coupon_discount, to_cents,
)

DATA = os.path.join(os.path.dirname(__file__), "..", "data")


def item(code, name, yuan) -> Item:
    return Item(code=code, name=name, price_cents=to_cents(yuan))


A = item("A", "巨无霸", 24.5)
B = item("B", "中薯条", 12.0)
C = item("C", "中杯可乐", 9.5)
D = item("D", "圣代", 8.0)

COMBO = [A, B, C]          # 小计 ¥46.00


def coupon(cid, ctype, **kw) -> Coupon:
    return Coupon(id=cid, name=kw.pop("name", cid), type=ctype, **kw)


class TestNewCouponTypes(unittest.TestCase):
    """v3 为跨平台新增的券型。"""

    def test_discount_rate(self):
        c = coupon("H8", "discount", rate=0.8)              # 8 折
        self.assertEqual(single_coupon_discount(COMBO, c), to_cents(46 * 0.2))

    def test_discount_with_threshold(self):
        c = coupon("H8", "discount", rate=0.8, threshold_cents=to_cents(50))
        self.assertEqual(single_coupon_discount(COMBO, c), 0)   # 46 < 50，不满足

    def test_discount_with_cap(self):
        c = coupon("H5", "discount", rate=0.5, cap_cents=to_cents(10))
        # 46 × 50% = 23，但封顶 10
        self.assertEqual(single_coupon_discount(COMBO, c), 1000)

    def test_item_discount_half_price(self):
        c = coupon("HALF", "item_discount", item_code="A", rate=0.5)
        self.assertEqual(single_coupon_discount(COMBO, c), to_cents(12.25))

    def test_item_discount_requires_item(self):
        c = coupon("HALF", "item_discount", item_code="ZZZ", rate=0.5)
        self.assertEqual(single_coupon_discount(COMBO, c), 0)

    def test_min_items_gate(self):
        c = coupon("M2", "threshold_reduce", reduce_cents=to_cents(5), min_items=4)
        self.assertEqual(single_coupon_discount(COMBO, c), 0)   # 只有 3 件
        c2 = coupon("M2", "threshold_reduce", reduce_cents=to_cents(5), min_items=3)
        self.assertEqual(single_coupon_discount(COMBO, c2), 500)

    def test_from_dict_reads_v3_fields(self):
        c = Coupon.from_dict({
            "id": "X", "type": "discount", "rate": 0.88, "cap": 12.0,
            "point_cost": 200, "stack_group": "platform", "channel": "eleme",
            "min_items": 2,
        })
        self.assertAlmostEqual(c.rate, 0.88)
        self.assertEqual(c.cap_cents, 1200)
        self.assertEqual(c.point_cost, 200)
        self.assertEqual(c.stack_group, "platform")
        self.assertEqual(c.channel, "eleme")
        self.assertEqual(c.min_items, 2)


class TestCouponStacking(unittest.TestCase):
    """券商叠加：同组互斥、跨组叠加、张数限流。"""

    def setUp(self):
        self.plat_big = coupon("P-G", "threshold_reduce", threshold_cents=to_cents(39),
                               reduce_cents=to_cents(8), stack_group="platform")
        self.plat_small = coupon("P-S", "threshold_reduce", threshold_cents=to_cents(30),
                                 reduce_cents=to_cents(3), stack_group="platform")
        self.shop = coupon("S", "threshold_reduce", threshold_cents=to_cents(30),
                           reduce_cents=to_cents(8), stack_group="shop")
        self.bzb = coupon("B", "threshold_reduce", reduce_cents=to_cents(5),
                          stack_group="bzb")

    def test_group_is_mutually_exclusive(self):
        app = apply_coupons(COMBO, [self.plat_big, self.plat_small, self.shop, self.bzb],
                            allow_stack=True, max_stack=5)
        ids = {c.id for c in app.coupons}
        self.assertIn("P-G", ids)          # 同组取更优的 8 元
        self.assertNotIn("P-S", ids)

    def test_cross_group_stacks(self):
        app = apply_coupons(COMBO, [self.plat_big, self.shop, self.bzb],
                            allow_stack=True, max_stack=5)
        self.assertEqual({c.id for c in app.coupons}, {"P-G", "S", "B"})
        self.assertEqual(app.discount_cents, to_cents(21))      # 8 + 8 + 5

    def test_max_stack_limits_order_level(self):
        app = apply_coupons(COMBO, [self.plat_big, self.shop, self.bzb],
                            allow_stack=True, max_stack=2)
        self.assertEqual(len(app.coupons), 2)
        # 按净收益降序 → 8 与 8（P-G 与 S），5 元的被挤掉
        self.assertEqual(app.discount_cents, to_cents(16))

    def test_item_level_applies_before_order_level(self):
        half = coupon("HALF", "item_discount", item_code="A", rate=0.5)   # 减 12.25
        app = apply_coupons(COMBO, [half, self.shop], allow_stack=True, max_stack=2)
        self.assertEqual(app.item_level_cents, to_cents(12.25))
        # 满减作用于 46 − 12.25 = 33.75，仍 ≥ 30，故减 8
        self.assertEqual(app.order_level_cents, to_cents(8))
        self.assertEqual(app.payable_cents, to_cents(25.75))

    def test_no_stack_matches_v1_semantics(self):
        """allow_stack=False 时所有券同池竞争，只留一张 —— v1 语义必须原样保留。"""
        app = apply_coupons(COMBO, [self.plat_big, self.shop, self.bzb], allow_stack=False)
        self.assertEqual(len(app.coupons), 1)
        self.assertEqual(app.discount_cents, to_cents(8))

    def test_discount_never_exceeds_goods(self):
        c = coupon("FREE", "discount", rate=0.0)
        app = apply_coupons(COMBO, [c], allow_stack=True)
        self.assertEqual(app.payable_cents, 0)
        self.assertEqual(app.discount_cents, to_cents(46))

    def test_empty_combo(self):
        app = apply_coupons([], [self.shop], allow_stack=True)
        self.assertEqual(app.discount_cents, 0)


class TestPointCostAwareSelection(unittest.TestCase):
    """积分成本必须参与选券 —— v3 的核心判据。"""

    def setUp(self):
        # 抵扣 24.5 元但要 500 积分的兑换券（抵扣额远大于普通券）
        self.point_c = point_coupon("PTS", "积分兑换·巨无霸（500积分）", "A", 500)
        self.cash_c = coupon("CASH", "threshold_reduce", name="满30减5",
                             threshold_cents=to_cents(30), reduce_cents=to_cents(5))

    def test_point_coupon_not_chosen_when_costly(self):
        """**回归用例**：影子价格 0.138 时，500 积分的机会成本是 ¥69，
        远高于它抵扣的 ¥24.5 —— 引擎必须选普通券而不是积分兑换券。

        实现初版按"抵扣额最大"择优，导致推荐"用 500 积分换 24.5 元汉堡"这种明显亏损方案。
        """
        app = apply_coupons(COMBO, [self.point_c, self.cash_c],
                            allow_stack=False, shadow_ypp=0.138)
        self.assertEqual([c.id for c in app.coupons], ["CASH"])

    def test_point_coupon_chosen_when_cheap(self):
        """积分很便宜（0.01 元/积分）时，500 积分机会成本仅 ¥5 < 抵扣 ¥24.5 → 应被选中。"""
        app = apply_coupons(COMBO, [self.point_c, self.cash_c],
                            allow_stack=False, shadow_ypp=0.01)
        self.assertEqual([c.id for c in app.coupons], ["PTS"])
        self.assertEqual(app.point_cost, 500)

    def test_zero_shadow_falls_back_to_discount(self):
        """影子价格为 0 = 把积分当免费 → 退化为纯抵扣额比较（v1 行为）。"""
        app = apply_coupons(COMBO, [self.point_c, self.cash_c],
                            allow_stack=False, shadow_ypp=0.0)
        self.assertEqual([c.id for c in app.coupons], ["PTS"])

    def test_point_coupon_rejected_when_net_negative(self):
        """净收益为负的券即便单独存在也不应被应用。"""
        app = apply_coupons([A], [point_coupon("PTS", "兑换", "A", 500)], shadow_ypp=0.138)
        self.assertEqual(app.coupons, [])
        self.assertEqual(app.discount_cents, 0)


class TestChannelPricing(unittest.TestCase):
    def test_min_order_rejects(self):
        ch = Channel(code="x", name="X", min_order_cents=to_cents(50))
        q = price_in_channel(COMBO, ch)          # 46 < 50
        self.assertFalse(q.feasible)
        self.assertIn("起送价", q.reason)

    def test_delivery_and_packaging_added(self):
        ch = Channel(code="x", name="X", delivery_fee_cents=to_cents(3.5),
                     packaging_fee_cents=to_cents(1.5))
        q = price_in_channel(COMBO, ch)
        self.assertEqual(q.cash_cents, to_cents(51))     # 46 + 3.5 + 1.5

    def test_free_delivery_threshold(self):
        ch = Channel(code="x", name="X", delivery_fee_cents=to_cents(9),
                     free_delivery_threshold_cents=to_cents(39))
        q = price_in_channel(COMBO, ch)                  # 46 ≥ 39 → 免配送
        self.assertEqual(q.delivery_cents, 0)
        small = price_in_channel([C], ch)                # 9.5 < 39 → 收配送费
        self.assertEqual(small.delivery_cents, to_cents(9))

    def test_price_factor(self):
        ch = Channel(code="x", name="X", price_factor=1.2)
        q = price_in_channel(COMBO, ch)
        self.assertEqual(q.goods_cents, to_cents(55.2))

    def test_landed_includes_point_opportunity_cost(self):
        """积分券的成本必须计入到手价，并按净收益决定用不用。"""
        # 50 积分换 ¥24.5 的 A：机会成本 50 × 0.138 = ¥6.90 → 净收益 ¥17.60，划算
        cheap = Channel(code="x", name="X", coupons=(point_coupon("P", "兑换A", "A", 50),))
        q = price_in_channel(COMBO, cheap, shadow_ypp=0.138)
        self.assertEqual(q.cash_cents, to_cents(21.5))       # 46 − 24.5
        self.assertEqual(q.point_value_cents, to_cents(6.9))
        self.assertEqual(q.landed_cents, to_cents(28.4))

        # 200 积分换同一件：机会成本 ¥27.60 > 抵扣 ¥24.50 → 净收益为负，直接不用
        pricey = Channel(code="y", name="Y", coupons=(point_coupon("P", "兑换A", "A", 200),))
        q2 = price_in_channel(COMBO, pricey, shadow_ypp=0.138)
        self.assertEqual(q2.point_cost, 0)
        self.assertEqual(q2.cash_cents, to_cents(46))


class TestChannelComparison(unittest.TestCase):
    def setUp(self):
        self.pickup = Channel(code="pickup", name="到店自取",
                              coupons=(coupon("C1", "threshold_reduce", name="满30减5",
                                              threshold_cents=to_cents(30),
                                              reduce_cents=to_cents(5)),))
        self.meituan = Channel(
            code="mt", name="美团", source="external",
            delivery_fee_cents=to_cents(3.5), packaging_fee_cents=to_cents(1.5),
            min_order_cents=to_cents(20), allow_stack=True, max_stack=2,
            coupons=(
                coupon("P", "threshold_reduce", name="平台满39减8",
                       threshold_cents=to_cents(39),
                       reduce_cents=to_cents(8), stack_group="platform"),
                coupon("S", "threshold_reduce", name="商家满30减8", threshold_cents=to_cents(30),
                       reduce_cents=to_cents(8), stack_group="shop"),
            ),
        )

    def test_compare_sorted_by_landed(self):
        quotes = compare_channels(COMBO, [self.pickup, self.meituan])
        cents = [q.landed_cents for q in quotes]
        self.assertEqual(cents, sorted(cents))
        # 美团：46 − 16 + 5 = 35；到店：46 − 5 = 41
        self.assertEqual(quotes[0].channel.code, "mt")
        self.assertEqual(quotes[0].landed_cents, to_cents(35))
        self.assertEqual(quotes[1].landed_cents, to_cents(41))

    def test_infeasible_sorted_last(self):
        tiny = Channel(code="t", name="小", min_order_cents=to_cents(999))
        quotes = compare_channels(COMBO, [tiny, self.pickup])
        self.assertEqual(quotes[-1].channel.code, "t")
        self.assertFalse(quotes[-1].feasible)

    def test_channel_gap(self):
        """渠道差价必须体现在报告用的汇总里。"""
        quotes = compare_channels(COMBO, [self.pickup, self.meituan])
        best = min((q for q in quotes if q.feasible), key=lambda q: q.landed_cents)
        plans = [ChannelPlan(combo=COMBO, best=best, quotes=quotes)]
        s = channel_savings(plans, [self.pickup, self.meituan])
        self.assertEqual(s["best_channel"], "美团")
        self.assertEqual(s["best_landed"], to_cents(35))     # 46−16 + 3.5 + 1.5
        self.assertEqual(s["worst_landed"], to_cents(41))    # 46−5
        self.assertEqual(s["channel_gap"], to_cents(6))


class TestCrossChannelOptimize(unittest.TestCase):
    def setUp(self):
        self.items = [A, B, C, D]
        self.channels = [
            Channel(code="pickup", name="到店自取",
                    coupons=(coupon("C1", "threshold_reduce", name="满30减5",
                                    threshold_cents=to_cents(30),
                                    reduce_cents=to_cents(5)),)),
            Channel(code="mt", name="美团", source="external",
                    delivery_fee_cents=to_cents(3.5), packaging_fee_cents=to_cents(1.5),
                    min_order_cents=to_cents(20), allow_stack=True, max_stack=2,
                    coupons=(coupon("P", "threshold_reduce", name="满39减8",
                                    threshold_cents=to_cents(39),
                                    reduce_cents=to_cents(8), stack_group="platform"),)),
        ]

    def test_budget_judged_on_landed_cost(self):
        """预算按**到手成本**判定（含渠道费），而不是券后商品价。

        美团券后 46−8=38 看似低于 40，但加上配送打包 5 元后到手 43 > 40，必须被排除。
        """
        cons = Constraints(budget_cents=to_cents(40), max_items=3)
        plans, _ = optimize_across_channels([A, B, C], [self.channels[1]], cons, top=5)
        self.assertTrue(all(p.landed_cents <= to_cents(40) for p in plans))

    def test_must_include_respected(self):
        cons = Constraints(must_include=("A",), max_items=3)
        plans, _ = optimize_across_channels(self.items, self.channels, cons, top=3)
        for p in plans:
            self.assertIn("A", [i.code for i in p.combo])

    def test_objective_cash_vs_landed(self):
        ch = Channel(code="p", name="P",
                     coupons=(point_coupon("PTS", "兑换A", "A", 1),))   # 1 积分极便宜
        cons = Constraints(max_items=1)
        landed, _ = optimize_across_channels([A], [ch], cons, top=1,
                                             shadow_ypp=0.138, objective="landed")
        cash, _ = optimize_across_channels([A], [ch], cons, top=1,
                                           shadow_ypp=0.138, objective="cash")
        self.assertTrue(landed and cash)
        # 两种目标下选出的都是同一个"用 1 积分换 A"的方案，但排序键不同
        self.assertEqual(landed[0].best.point_cost, 1)
        self.assertEqual(cash[0].best.cash_cents, 0)

    def test_no_feasible_plan(self):
        cons = Constraints(budget_cents=1, max_items=3)
        plans, _ = optimize_across_channels(self.items, self.channels, cons, top=5)
        self.assertEqual(plans, [])


class TestBundlePrice(unittest.TestCase):
    """``bundle_price``：积分商城与平台最常见的「X 元买这个组合」。"""

    def test_single_item(self):
        c = coupon("B1", "bundle_price", item_codes=("A",), fixed_cents=to_cents(20))
        self.assertEqual(single_coupon_discount(COMBO, c), to_cents(24.5 - 20))

    def test_multi_item(self):
        c = coupon("B2", "bundle_price", item_codes=("A", "C"), fixed_cents=to_cents(21.9))
        self.assertEqual(single_coupon_discount(COMBO, c), to_cents(34.0 - 21.9))

    def test_requires_every_listed_item(self):
        # 购物车缺 C 时不可用（组合券必须含齐全部指定商品）
        c = coupon("B2", "bundle_price", item_codes=("A", "C"), fixed_cents=to_cents(21.9))
        self.assertEqual(single_coupon_discount([A, B], c), 0)

    def test_no_discount_when_price_not_lower(self):
        # 打包价 ≥ 菜单单点价 → 抵扣为 0，不允许出现负抵扣
        c = coupon("B3", "bundle_price", item_codes=("A",), fixed_cents=to_cents(30))
        self.assertEqual(single_coupon_discount(COMBO, c), 0)

    def test_applied_before_order_level_coupon(self):
        # 组合特价属"单品级"，先落地；整单券再作用于其余额
        bundle = coupon("B2", "bundle_price", item_codes=("A", "C"),
                        fixed_cents=to_cents(21.9), stack_group="bundle")
        thresh = coupon("T1", "threshold_reduce", threshold_cents=to_cents(20),
                        reduce_cents=to_cents(3), stack_group="plat")
        app = apply_coupons(COMBO, [bundle, thresh], allow_stack=True, max_stack=2)
        self.assertEqual(app.item_level_cents, to_cents(12.1))
        self.assertEqual(app.order_level_cents, to_cents(3))      # 余 ¥24.0 ≥ 20
        self.assertEqual(app.payable_cents, to_cents(46.0 - 12.1 - 3))

    def test_point_bundle_coupon_rejected_when_points_are_valuable(self):
        """积分券的抵扣再大，也可能因积分机会成本而**不该用** —— v3 的核心判据。"""
        c = point_bundle_coupon("P1", "21.9元巨无霸可乐组合", ("A", "C"),
                                to_cents(21.9), point_cost=800)
        d = single_coupon_discount(COMBO, c)
        self.assertEqual(d, to_cents(24.5 + 9.5 - 21.9))          # ¥12.10
        # 影子价格 0.138 元/积分 → 800 积分机会成本 ¥110.4，远超抵扣 ¥12.1
        app = apply_coupons(COMBO, [c], allow_stack=False, shadow_ypp=0.138)
        self.assertEqual(app.discount_cents, 0)
        # 积分即将过期（视为沉没成本）时，同一张券立刻变划算
        app2 = apply_coupons(COMBO, [c], allow_stack=False, shadow_ypp=0.0)
        self.assertEqual(app2.discount_cents, to_cents(12.1))
        self.assertAlmostEqual(breakeven_ypp(d, 800), 0.015125, places=6)

    def test_breakeven_ypp(self):
        self.assertAlmostEqual(breakeven_ypp(to_cents(14.1), 800), 0.017625, places=6)
        self.assertEqual(breakeven_ypp(to_cents(14.1), 0), 0.0)     # 免费券无打平线


class TestSampleData(unittest.TestCase):
    """样例数据必须能被解析并产生合理结果 —— 防止 JSON 手写错误溜过去。"""

    def test_channels_load(self):
        with open(os.path.join(DATA, "sample_channels.json"), encoding="utf-8") as f:
            raw = json.load(f)["channels"]
        channels = [Channel.from_dict(c) for c in raw]
        self.assertEqual(len(channels), 4)
        codes = {c.code for c in channels}
        self.assertIn("meituan", codes)
        self.assertTrue(any(c.allow_stack for c in channels))
        self.assertTrue(all(c.coupons for c in channels))

    def test_meituan_stack_beats_pickup(self):
        with open(os.path.join(DATA, "sample_channels.json"), encoding="utf-8") as f:
            channels = [Channel.from_dict(c) for c in json.load(f)["channels"]]
        with open(os.path.join(DATA, "sample_menu.json"), encoding="utf-8") as f:
            menu = {i.code: i for i in (Item.from_dict(d) for d in json.load(f))}
        combo = [menu["BURGER-BIGMAC"], menu["FRIES-M"], menu["COLA-M"]]
        quotes = {q.channel.code: q for q in compare_channels(combo, channels, shadow_ypp=0.138)}
        self.assertLess(quotes["meituan"].landed_cents, quotes["mcd-pickup"].landed_cents)
        # 到店自取绝不应选中"500 积分换巨无霸"（机会成本 ¥69 远高于抵扣 ¥24.5）
        self.assertNotIn("PTS-BIGMAC", [c.id for c in quotes["mcd-pickup"].coupons])


if __name__ == "__main__":
    unittest.main(verbosity=2)
