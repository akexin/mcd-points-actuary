#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""麦麦积分精算师 —— 积分价值引擎单元测试（离线，无需 MCP Token）。

覆盖四类引擎与全部边界：
  1. 金额 / 时间 / 面值解析等基础工具
  2. 价值矩阵：排序、倍数差、陷阱识别、上架周期判定、机会成本
  3. 抽奖期望值：dominated / robust / likely / depends / unknown 五种判定
  4. 组合背包：0/1 约束、件数上限、"不超预算"、余数利用
  5. 到期止损：兑换 + 抽奖兜底 + 损失量化
"""
from __future__ import annotations

import datetime as dt
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

from points_engine import (  # noqa: E402
    BundlePlan, Prize, RedemptionOffer, WINDOW_ACTIVE, WINDOW_PREHEAT, WINDOW_STALE,
    analyze_lottery, build_value_matrix, fmt_yuan, fmt_ypp, offer_from_raw,
    opportunity_cost_cents, parse_face_value, parse_mcd_time, plan_bundles, plan_expiry,
    prize_from_raw, to_cents, window_state,
)

NOW = dt.datetime(2026, 10, 9, 15, 0, 0)


def offer(spu, name, point, face_yuan, *, cat="测试", up="2024-01-01 00:00:00",
          down="2030-12-31 23:59:59", status=2, pricing="face") -> RedemptionOffer:
    """构造测试用兑换标的（face_yuan 为元，内部转分）。"""
    return RedemptionOffer(
        spu_id=spu, name=name, point=point, face_value_cents=to_cents(face_yuan),
        pricing=pricing, category=cat, status=status, up_time=up, down_time=down,
    )


class TestAmountUtils(unittest.TestCase):
    def test_to_cents_accepts_yuan_forms(self):
        self.assertEqual(to_cents(18.8), 1880)
        self.assertEqual(to_cents(3), 300)
        self.assertEqual(to_cents("21.9"), 2190)
        self.assertEqual(to_cents("¥5.90"), 590)
        self.assertEqual(to_cents("6.9元"), 690)
        self.assertEqual(to_cents(None), 0)

    def test_to_cents_rejects_bool(self):
        # bool 是 int 的子类，若不显式拦截会把 True 当成 1 元
        with self.assertRaises(TypeError):
            to_cents(True)

    def test_formatting(self):
        self.assertEqual(fmt_yuan(1880), "¥18.80")
        self.assertEqual(fmt_ypp(0.376), "0.3760 元/积分")


class TestFaceValueParsing(unittest.TestCase):
    def test_price_prefix(self):
        self.assertEqual(parse_face_value("18.8元2份麦辣鸡翅"), (1880, "face"))
        self.assertEqual(parse_face_value("21.9元巨无霸可乐组合"), (2190, "face"))

    def test_reduce_coupon(self):
        self.assertEqual(parse_face_value("下单立减3元券"), (300, "reduce"))

    def test_discount_needs_reference(self):
        # 没有原价就绝不定价 —— 宁可标记 unknown，也不臆造金额
        self.assertEqual(parse_face_value("麦辣三件套5折券"), (0, "unknown"))
        self.assertEqual(parse_face_value("麦辣三件套5折券", 3350), (1675, "discount"))
        self.assertEqual(parse_face_value("双层鳕鱼三件套6折券", 3500), (1400, "discount"))

    def test_reference_fallback(self):
        self.assertEqual(parse_face_value("麦麦脆汁鸡两件套", 3000), (3000, "reference"))

    def test_unknown(self):
        self.assertEqual(parse_face_value("罗技键鼠套装"), (0, "unknown"))


class TestWindowState(unittest.TestCase):
    def test_active_stale_preheat(self):
        active = offer("1", "A", 50, 10, up="2026-01-01 00:00:00", down="2026-12-31 23:59:59")
        stale = offer("2", "B", 50, 10, up="2024-01-01 00:00:00", down="2026-03-21 23:59:59")
        preheat = offer("3", "C", 50, 10, up="2026-11-01 00:00:00", down="2026-12-31 23:59:59")
        self.assertEqual(window_state(active, NOW), WINDOW_ACTIVE)
        self.assertEqual(window_state(stale, NOW), WINDOW_STALE)
        self.assertEqual(window_state(preheat, NOW), WINDOW_PREHEAT)

    def test_parse_time(self):
        self.assertEqual(parse_mcd_time("2026-10-09 15:00:00"), NOW)
        self.assertEqual(parse_mcd_time("2026-10-09"), dt.datetime(2026, 10, 9))
        self.assertIsNone(parse_mcd_time(""))


class TestValueMatrix(unittest.TestCase):
    def setUp(self):
        # 刻意复刻真实数据里的极端价差：同积分换到的价值差 13.7 倍
        self.offers = [
            offer("a", "18.8元麦辣鸡腿汉堡两件套", 50, 18.8),
            offer("b", "6.9元可乐麦炫酷", 50, 6.9),
            offer("c", "24.8元2份板烧鸡腿堡", 500, 24.8),
            offer("d", "21.9元巨无霸可乐组合", 800, 21.9),
        ]

    def test_ranking_and_spread(self):
        m = build_value_matrix(self.offers, now=NOW)
        self.assertEqual(m.best.spu_id, "a")
        self.assertEqual(m.worst.spu_id, "d")
        # (18.8/50) ÷ (21.9/800) = 0.376 / 0.027375 ≈ 13.735
        self.assertAlmostEqual(m.spread, 13.735, places=2)

    def test_benchmark_prefers_active_items(self):
        """基准必须取"周期内最优"——否则会把已结束活动的价格当成可用机会价值。"""
        offers = [
            offer("old", "历史最佳券", 50, 30.0, down="2026-03-21 23:59:59"),   # 已过周期
            offer("new", "当前最佳券", 50, 9.0),                                # 周期内
        ]
        m = build_value_matrix(offers, now=NOW)
        self.assertEqual(m.best.spu_id, "old")                 # 全库最优仍是历史券
        self.assertAlmostEqual(m.benchmark_ypp, 0.18, places=4)  # 但基准取周期内的 9/50

    def test_active_spread_ignores_stale(self):
        offers = [
            offer("old", "历史券", 50, 30.0, down="2026-03-21 23:59:59"),
            offer("n1", "当前券A", 50, 9.0),
            offer("n2", "当前券B", 100, 3.0),
        ]
        m = build_value_matrix(offers, now=NOW)
        self.assertEqual(len(m.active), 2)
        self.assertEqual(len(m.stale), 1)
        # 周期内：9/50 = 0.18 与 3/100 = 0.03 → 0.18 / 0.03 = 6 倍（历史券不参与）
        self.assertAlmostEqual(m.active_spread, 6.0, places=2)

    def test_traps_detected(self):
        m = build_value_matrix(self.offers, now=NOW)
        self.assertIn("d", [t.spu_id for t in m.traps])
        self.assertNotIn("a", [t.spu_id for t in m.traps])

    def test_unpriced_excluded_from_ranking(self):
        offers = self.offers + [offer("x", "无法定价的实物", 6666, 0, pricing="unknown")]
        m = build_value_matrix(offers, now=NOW)
        self.assertNotIn("x", [o.spu_id for o in m.ranked])
        self.assertIn("x", [o.spu_id for o in m.unpriced])

    def test_unredeemable_filtered(self):
        offers = [offer("a", "上架券", 50, 18.8), offer("b", "售罄券", 50, 99.0, status=3)]
        m = build_value_matrix(offers, now=NOW)
        self.assertEqual([o.spu_id for o in m.ranked], ["a"])

    def test_opportunity_cost(self):
        m = build_value_matrix(self.offers, now=NOW)
        # 基准 = 周期内最优 = 18.8/50 = 0.376 元/积分
        # 用 800 积分换 ¥21.9 的机会成本 = 0.376 × 800 − 21.9 = ¥278.90
        worst = next(o for o in self.offers if o.spu_id == "d")
        cost = opportunity_cost_cents(worst, m.benchmark_ypp)
        self.assertAlmostEqual(cost / 100, 0.376 * 800 - 21.9, places=2)
        self.assertAlmostEqual(cost / 100, 278.90, places=2)

    def test_empty_offers(self):
        m = build_value_matrix([], now=NOW)
        self.assertEqual(m.ranked, [])
        self.assertEqual(m.benchmark_ypp, 0.0)


class TestLotteryAnalysis(unittest.TestCase):
    """五种判定各构造一个必然命中的场景。"""

    @staticmethod
    def _coupons(*cents):
        return [Prize(name=f"券{i}", kind="coupon", face_value_cents=c)
                for i, c in enumerate(cents)]

    def test_dominated_when_max_below_required(self):
        # required = 24 × 0.376 × 100 = 902 分
        a = analyze_lottery(24, self._coupons(100, 200, 300), 0.376)
        self.assertEqual(a.verdict, "dominated")
        self.assertIn("不需要任何概率假设", a.reason)
        self.assertIsNone(a.breakeven_prob)

    def test_robust_by_floor(self):
        # required = 24 分，最低奖 100 分已高于打平线 → 保底占优
        a = analyze_lottery(24, self._coupons(100, 200, 300), 0.01)
        self.assertEqual(a.verdict, "robust")
        self.assertIn("保底", a.reason)

    def test_robust_by_pessimistic_expectation(self):
        # required = 150；最低奖 100 低于打平线，但扣掉最高奖后均值 (100+200)/2 = 150 ≥ 150
        a = analyze_lottery(24, self._coupons(100, 200, 200), 0.0625)
        self.assertEqual(a.verdict, "robust")
        self.assertEqual(a.required_ev_cents, 150)
        self.assertIn("永远抽不到最高奖", a.reason)

    def test_likely_when_uniform_passes_but_pessimistic_fails(self):
        # required = 150；全池均值 (50+50+400)/3 ≈ 167 ≥ 150，扣掉最高奖后均值 50 < 150
        a = analyze_lottery(24, self._coupons(50, 50, 400), 0.0625)
        self.assertEqual(a.verdict, "likely")
        self.assertGreaterEqual(a.expected_if_uniform_cents, 150)
        self.assertLess(a.expected_pessimistic_cents, 150)

    def test_depends_with_breakeven_probability(self):
        # required = 200；max 300 ≥ 200，扣最高奖后均值 10
        # p = (200 − 10) / (300 − 10) = 190/290 ≈ 0.6552
        a = analyze_lottery(24, self._coupons(10, 10, 300), 1 / 12)
        self.assertEqual(a.verdict, "depends")
        self.assertEqual(a.required_ev_cents, 200)
        self.assertAlmostEqual(a.breakeven_prob, 190 / 290, places=4)

    def test_points_prize_valued_at_opportunity_cost(self):
        """积分奖要按"这些积分本可换到的价值"折算，而不是按面值 0 处理。"""
        prizes = [Prize(name="30积分", kind="points", points=30)]
        a = analyze_lottery(24, prizes, 0.1)
        # 30 积分 × 0.1 元/积分 = 3 元 = 300 分
        self.assertEqual(a.max_prize_cents, 300)
        self.assertEqual(a.unpriced_count, 0)

    def test_unknown_when_nothing_priced(self):
        prizes = [Prize(name="神秘奖品", kind="coupon", face_value_cents=0)]
        a = analyze_lottery(24, prizes, 0.1)
        self.assertEqual(a.verdict, "unknown")
        self.assertEqual(a.unpriced_count, 1)

    def test_zero_benchmark(self):
        a = analyze_lottery(24, self._coupons(100), 0.0)
        self.assertEqual(a.verdict, "unknown")


class TestPlanBundles(unittest.TestCase):
    def setUp(self):
        self.offers = [
            offer("a", "高效小食", 50, 18.8),
            offer("b", "高效两件套", 100, 18.8),
            offer("c", "中效四件套", 500, 24.8),
            offer("d", "低效组合", 800, 21.9),
        ]

    def test_zero_one_constraint(self):
        """同一个标的不能重复兑换 —— 否则 50 积分的高效券会被算法刷到离谱地步。"""
        plans = plan_bundles(100, self.offers, top=1, max_kinds=5)
        self.assertEqual(len(plans), 1)
        self.assertEqual(len(plans[0].picks), 1)
        self.assertEqual(plans[0].spent_point, 50)

    def test_max_kinds_respected(self):
        offers = [offer(f"x{i}", f"券{i}", 50, 20.0) for i in range(6)]
        plans = plan_bundles(300, offers, top=1, max_kinds=2)
        self.assertEqual(len(plans[0].picks), 2)
        self.assertEqual(plans[0].spent_point, 100)

    def test_not_required_to_spend_exactly(self):
        """必须允许"不超预算"：精确用满会让 1500 积分因凑不出整额而直接无解。"""
        offers = [offer("a", "券A", 100, 18.8), offer("b", "券B", 100, 18.8)]
        plans = plan_bundles(150, offers, top=1, max_kinds=5)
        self.assertTrue(plans)
        self.assertEqual(plans[0].spent_point, 100)
        self.assertEqual(plans[0].leftover_point, 50)

    def test_picks_highest_value(self):
        plans = plan_bundles(150, self.offers, top=1, max_kinds=2)
        # a(50pt,¥18.8) + b(100pt,¥18.8) = 150pt / ¥37.6 为最优
        self.assertEqual(plans[0].total_value_cents, to_cents(37.6))
        self.assertEqual(plans[0].spent_point, 150)

    def test_budget_too_small(self):
        self.assertEqual(plan_bundles(10, self.offers), [])

    def test_zero_or_negative(self):
        self.assertEqual(plan_bundles(0, self.offers), [])
        self.assertEqual(plan_bundles(-5, self.offers), [])

    def test_unpriced_never_selected(self):
        offers = [offer("x", "无法定价", 50, 0, pricing="unknown")]
        self.assertEqual(plan_bundles(100, offers), [])

    def test_leftover_small_use(self):
        offers = [offer("a", "券A", 100, 18.8), offer("b", "小食", 50, 6.9)]
        plans = plan_bundles(120, offers, top=1, max_kinds=5)
        # 用 100 分换 A，余 20 分不足 50 → 无余额方案
        self.assertEqual(plans[0].leftover_point, 20)
        self.assertEqual(plans[0].leftover_value_cents, 0)

    def test_alternatives_are_distinct(self):
        plans = plan_bundles(600, self.offers, top=3, max_kinds=3)
        sigs = {tuple(sorted((o.spu_id, q) for o, q in p.picks)) for p in plans}
        self.assertEqual(len(sigs), len(plans))


class TestPlanExpiry(unittest.TestCase):
    def setUp(self):
        self.offers = [
            offer("a", "高效小食", 50, 18.8),
            offer("b", "高效两件套", 100, 18.8),
            offer("c", "中效四件套", 500, 24.8),
        ]

    def test_exchange_then_lottery(self):
        lottery = analyze_lottery(24, [Prize(name="立减3元券", kind="coupon",
                                             face_value_cents=300),
                                       Prize(name="立减2元券", kind="coupon",
                                             face_value_cents=200)], 0.376)
        self.assertEqual(lottery.verdict, "dominated")  # 上限 300 < 打平线 902
        p = plan_expiry(800, self.offers, draw_point=24, lottery=lottery)
        self.assertTrue(p.bundles)
        # 抽奖被严格劣汰时，不应建议把积分丢给抽奖
        self.assertEqual(p.lottery_draws, 0)

    def test_lottery_used_when_not_dominated(self):
        offers = [offer("a", "小券", 50, 1.0)]  # 效率 0.02
        lottery = analyze_lottery(24, [Prize(name="立减3元券", kind="coupon",
                                             face_value_cents=300)], 0.02)
        self.assertNotEqual(lottery.verdict, "dominated")
        p = plan_expiry(100, offers, draw_point=24, lottery=lottery)
        # 50 分换券，余 50 分可抽 2 次
        self.assertEqual(p.lottery_draws, 2)
        self.assertEqual(p.lost_point, 2)

    def test_loss_quantified(self):
        p = plan_expiry(13, [], draw_point=24)
        self.assertEqual(p.salvaged_cents, 0)
        self.assertEqual(p.lost_point, 13)
        self.assertGreaterEqual(p.lost_cents, 0)

    def test_zero_expiring(self):
        p = plan_expiry(0, self.offers)
        self.assertEqual(p.lost_point, 0)
        self.assertEqual(p.bundles, [])


class TestRawConverters(unittest.TestCase):
    def test_offer_from_raw(self):
        raw = {"spuName": "18.8元2份麦辣鸡翅", "spuId": 6113, "point": "200",
               "shopId": 2, "catName": "到店专用", "status": 2,
               "upTime": "2024-12-31 11:45:03", "downTime": "2030-12-31 23:59:59"}
        o = offer_from_raw(raw)
        self.assertEqual(o.spu_id, "6113")
        self.assertEqual(o.point, 200)
        self.assertEqual(o.face_value_cents, 1880)
        self.assertTrue(o.redeemable)

    def test_prize_from_raw_points(self):
        p = prize_from_raw({"name": "30积分", "typeText": "积分奖"})
        self.assertEqual(p.kind, "points")
        self.assertEqual(p.points, 30)

    def test_prize_from_raw_coupon_with_refs(self):
        p = prize_from_raw({"name": "麦辣三件套5折券", "typeText": "优惠券"},
                           {"麦辣三件套5折券": 3350})
        self.assertEqual(p.kind, "coupon")
        self.assertEqual(p.face_value_cents, 1675)


class TestMenuPriceMatching(unittest.TestCase):
    """券名 ↔ 菜单单品的模糊匹配。

    这里的两个 None 断言都是**实测中真实误配后固化的回归用例** ——
    0.6 的重合度阈值会把"小食四件套"错配到"BFF四宫格小食盘"，
    并让活动商品"麦咖啡™奶铁品鉴会"套用饮品"麦咖啡™奶铁"的价格。
    """

    MENU = {
        "1": {"name": "麦辣鸡腿汉堡三件套", "currentPrice": 33.5},
        "2": {"name": "板烧鸡腿堡三件套", "currentPrice": 34.5},
        "3": {"name": "双层深海鳕鱼堡三件套", "currentPrice": 35.0},
        "4": {"name": "经典麦旋风", "currentPrice": 15.5},
        "5": {"name": "BFF四宫格小食盘", "currentPrice": 40.3},
        "6": {"name": "麦咖啡™奶铁", "currentPrice": 13.9},
        "7": {"name": "板烧鸡腿堡四件套", "currentPrice": 28.0},
    }

    def setUp(self):
        sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))
        from run_points_live import match_menu_price  # noqa: PLC0415
        self.match = match_menu_price

    def test_matches_three_piece_combos(self):
        self.assertEqual(self.match("麦辣三件套5折券", self.MENU),
                         ("麦辣鸡腿汉堡三件套", 3350))
        self.assertEqual(self.match("板烧三件套5折券", self.MENU),
                         ("板烧鸡腿堡三件套", 3450))
        self.assertEqual(self.match("双层鳕鱼三件套6折券", self.MENU),
                         ("双层深海鳕鱼堡三件套", 3500))

    def test_matches_short_name(self):
        self.assertEqual(self.match("麦旋风5折券", self.MENU), ("经典麦旋风", 1550))

    def test_regression_no_match_for_partial_overlap(self):
        # 3/5 = 0.6 的重合度不足以证明是同一件商品 —— 宁可不定价
        self.assertIsNone(self.match("小食四件套5.5折券", self.MENU))

    def test_regression_activity_items_never_priced(self):
        # 卖的是场次不是餐品，绝不能套用同名饮品价格
        self.assertIsNone(self.match("麦咖啡™奶铁品鉴会", self.MENU))
        self.assertIsNone(self.match("麦当劳亲子读书会", self.MENU))
        self.assertIsNone(self.match("麦当劳奇妙生日派对", self.MENU))

    def test_ignores_zero_and_missing_prices(self):
        menu = {"1": {"name": "经典麦旋风", "currentPrice": 0},
                "2": {"name": "经典麦旋风", "currentPrice": None}}
        self.assertIsNone(self.match("麦旋风5折券", menu))


if __name__ == "__main__":
    unittest.main(verbosity=2)
