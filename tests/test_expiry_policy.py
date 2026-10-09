#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""跨期积分策略（序贯兑换）的单元测试。

本模块的说法很硬 —— "v3 的 0.1380 在余额偏大时高估了 100%" —— 所以测试也必须硬：
背包必须与**穷举**逐一对上（:class:`TestValueTable`），回溯清单必须与背包值**自洽**
（:class:`TestSolvePicks`），而不是只看它有没有跑出个像样的数字。

覆盖率最高的三个断言：
1. 有界背包的特解 == 笛卡尔积穷举的最优值（随机 60 组）
2. ``sum(picks 的面值) == best[balance]`` 且 ``sum(picks 的积分) == spent``
3. 余额饱和时边际效率必须恰好为 0（这就是"高估"的定量形式）
"""
from __future__ import annotations

import datetime as _dt
import io
import itertools
import os
import random
import sys
import unittest
from contextlib import redirect_stdout

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

from expiry_policy import (  # noqa: E402
    ExpirySequence, RedeemableLot, lots_from_offers, plan_sequence, print_sequence,
    solve_picks, value_table,
)
from points_engine import (  # noqa: E402
    WINDOW_ACTIVE, RedemptionOffer, _load, _load_offers, build_value_matrix,
    window_state,
)

DATA = os.path.join(os.path.dirname(__file__), "..", "data")
PRODUCTS = os.path.join(DATA, "sample_points_products.json")
REFS = os.path.join(DATA, "sample_reference_prices.json")

NOW = _dt.datetime(2026, 10, 10, 9, 0, 0)
TODAY = NOW.date()


# --------------------------------------------------------------------------- #
# 参照实现：穷举
# --------------------------------------------------------------------------- #
def _brute_force(lots, balance, days_left, today=None):
    """第三方裁判：把每个标的的可换件数展开成笛卡尔积，逐组求和取最大。

    只在小规模上用 —— 它的意义是**不依赖**待测代码的任何中间结果。
    """
    us = [l for l in lots if l.point > 0 and l.value_cents > 0]
    if not us:
        return 0
    ranges = [range(0, min(l.max_qty(days_left, today), balance // l.point) + 1)
              for l in us]
    best = 0
    for combo in itertools.product(*ranges):
        if sum(l.point * q for l, q in zip(us, combo)) > balance:
            continue
        best = max(best, sum(l.value_cents * q for l, q in zip(us, combo)))
    return best


def _lot(name, point, cents, period_days=30, per_period=1):
    return RedeemableLot(name=name, point=point, value_cents=cents,
                         period_days=period_days, per_period=per_period)


def _real_lots(period_days=30, per_period=1, now=NOW):
    refs = {k: int(v) for k, v in _load(REFS).items()
            if not str(k).startswith("_") and isinstance(v, (int, float))}
    return lots_from_offers(_load_offers(PRODUCTS, refs),
                            period_days=period_days, per_period=per_period, now=now)


# --------------------------------------------------------------------------- #
class TestMaxQty(unittest.TestCase):
    def test_unlimited_when_period_zero(self):
        l = _lot("不限次", 100, 1090, period_days=0)
        self.assertEqual(l.max_qty(36, TODAY), 10 ** 6)

    def test_period_truncation_rounds_up(self):
        l = _lot("月限一次", 50, 690, period_days=30, per_period=1)
        self.assertEqual(l.max_qty(36, TODAY), 2)     # 36 天跨 2 个周期
        self.assertEqual(l.max_qty(30, TODAY), 1)
        self.assertEqual(l.max_qty(1, TODAY), 1)
        self.assertEqual(l.max_qty(60, TODAY), 2)

    def test_per_period_multiplies(self):
        l = _lot("月限三次", 50, 690, period_days=30, per_period=3)
        self.assertEqual(l.max_qty(36, TODAY), 6)

    def test_expired_balance_returns_zero(self):
        self.assertEqual(_lot("x", 50, 690).max_qty(0, TODAY), 0)
        self.assertEqual(_lot("x", 50, 690).max_qty(-3, TODAY), 0)

    def test_past_window_returns_zero(self):
        l = RedeemableLot("过期", 50, 690, period_days=30,
                          window_end=TODAY - _dt.timedelta(days=1))
        self.assertEqual(l.max_qty(36, TODAY), 0)

    def test_degenerate_values_return_zero(self):
        self.assertEqual(_lot("零积分", 0, 690).max_qty(36, TODAY), 0)
        self.assertEqual(_lot("零面值", 50, 0).max_qty(36, TODAY), 0)

    def test_ypp(self):
        self.assertAlmostEqual(_lot("派", 50, 690).ypp, 0.138, places=6)
        self.assertEqual(_lot("零", 0, 690).ypp, 0.0)


class TestValueTable(unittest.TestCase):
    """有界背包必须与穷举完全一致 —— 这是整个模块的地基。"""

    def test_empty_lots_all_zero(self):
        t = value_table([], 200, 36, TODAY)
        self.assertEqual(len(t), 201)
        self.assertTrue(all(v == 0 for v in t))

    def test_monotone_non_decreasing(self):
        t = value_table(_real_lots(), 1500, 36, TODAY)
        self.assertTrue(all(t[i] <= t[i + 1] for i in range(len(t) - 1)))

    def test_three_real_lots_matches_brute_force(self):
        lots = [_lot("派", 50, 690), _lot("小食", 100, 1090), _lot("组合", 500, 1790)]
        for balance in (0, 49, 50, 99, 100, 149, 150, 400, 600, 649, 700, 1000):
            with self.subTest(balance=balance):
                self.assertEqual(value_table(lots, balance, 36, TODAY)[balance],
                                 _brute_force(lots, balance, 36, TODAY))

    def test_random_cases_match_brute_force(self):
        rng = random.Random(20261009)
        for case in range(60):
            lots, balance = [], rng.randint(0, 400)
            for i in range(rng.randint(1, 4)):
                lots.append(_lot(f"标的{i}", rng.choice([30, 50, 100, 120]),
                                 rng.choice([290, 690, 1090, 1790]),
                                 period_days=30, per_period=rng.randint(1, 2)))
            days_left = rng.choice([10, 31, 36, 90])
            with self.subTest(case=case, balance=balance, days=days_left):
                self.assertEqual(
                    value_table(lots, balance, days_left, TODAY)[balance],
                    _brute_force(lots, balance, days_left, TODAY))

    def test_time_truncation_lowers_value(self):
        """到期日把「每期限换次数」截断 ⇒ 可挽回值下降。这就是时间价值的算法来源。"""
        lots = [_lot("月限一次", 100, 1090)]
        self.assertEqual(value_table(lots, 1000, 90, TODAY)[1000], 3270)   # 3 期
        self.assertEqual(value_table(lots, 1000, 36, TODAY)[1000], 2180)   # 2 期
        self.assertEqual(value_table(lots, 1000, 5, TODAY)[1000], 1090)    # 1 期


class TestSolvePicks(unittest.TestCase):
    def test_reconstruction_is_self_consistent(self):
        lots = _real_lots()
        for balance in (50, 130, 400, 800, 1500, 5000):
            with self.subTest(balance=balance):
                best = value_table(lots, balance, 36, TODAY)[balance]
                picks, salvaged = solve_picks(lots, balance, 36, TODAY)
                self.assertEqual(salvaged, best)
                self.assertEqual(sum(l.value_cents * q for l, q in picks), best)
                self.assertLessEqual(sum(l.point * q for l, q in picks), balance)

    def test_respects_max_qty(self):
        lots = [_lot("月限一次", 50, 690, period_days=30, per_period=1)]
        picks, salvaged = solve_picks(lots, 9999, 36, TODAY)
        self.assertEqual(sum(q for _, q in picks), 2)
        self.assertEqual(salvaged, 1380)

    def test_aggregates_by_identity_not_name(self):
        """同名但积分档位不同的标的必须保持为两项 —— 商城实测就存在这种。"""
        a = _lot("10.9元指定小食任选", 100, 1090)
        b = _lot("10.9元指定小食任选", 200, 2180)
        picks, salvaged = solve_picks([a, b], 600, 36, TODAY)
        self.assertEqual(len(picks), 2)
        self.assertEqual(len({id(l) for l, _ in picks}), 2)
        self.assertEqual({id(l) for l, _ in picks}, {id(a), id(b)})
        self.assertEqual(sum(l.point * q for l, q in picks), 600)
        self.assertEqual(sum(l.value_cents * q for l, q in picks), salvaged)

    def test_matches_brute_force_on_random_cases(self):
        rng = random.Random(4242)
        for case in range(30):
            lots = [_lot(f"标的{i}", rng.choice([30, 50, 100]),
                         rng.choice([290, 690, 1090]), period_days=30)
                    for i in range(rng.randint(1, 3))]
            balance = rng.randint(0, 400)
            picks, salvaged = solve_picks(lots, balance, 36, TODAY)
            with self.subTest(case=case):
                self.assertEqual(salvaged, _brute_force(lots, balance, 36, TODAY))
                self.assertLessEqual(sum(l.point * q for l, q in picks), balance)

    def test_zero_balance(self):
        picks, salvaged = solve_picks(_real_lots(), 0, 36, TODAY)
        self.assertEqual((picks, salvaged), ([], 0))

    def test_no_lots(self):
        self.assertEqual(solve_picks([], 500, 36, TODAY), ([], 0))

    def test_picks_sorted_by_efficiency(self):
        picks, _ = solve_picks(_real_lots(), 1500, 36, TODAY)
        ypps = [l.ypp for l, _ in picks]
        self.assertEqual(ypps, sorted(ypps, reverse=True))


class TestPlanSequence(unittest.TestCase):
    def test_saturated_balance_marginal_is_exactly_zero(self):
        """容量被吃满 ⇒ 边际效率为 0。v3 的 0.1380 此时高估 100%，这是最极端的一档。"""
        lots = [_lot("月限两次", 50, 690, period_days=30, per_period=1)]
        p = plan_sequence(1500, TODAY + _dt.timedelta(days=36), lots, now=NOW)
        self.assertEqual(p.capacity_point, 100)
        self.assertEqual(p.dead_point, 1400)
        self.assertEqual(p.marginal_ypp, 0.0)
        self.assertAlmostEqual(p.best_lot_ypp, 0.138, places=6)
        self.assertAlmostEqual(p.ypp_gap, 0.138, places=6)
        self.assertEqual(p.suggested_shadow_ypp, 0.0)

    def test_small_balance_marginal_equals_best_lot(self):
        lots = [_lot("月限两次", 50, 690, period_days=30, per_period=1)]
        p = plan_sequence(50, TODAY + _dt.timedelta(days=36), lots, now=NOW)
        self.assertAlmostEqual(p.marginal_ypp, p.best_lot_ypp, places=9)
        self.assertAlmostEqual(p.average_ypp, 0.138, places=6)
        self.assertEqual(p.dead_point, 0)
        self.assertEqual(p.ypp_gap, 0.0)

    def test_marginal_never_above_best_lot(self):
        lots = _real_lots()
        for balance in (50, 200, 700, 1500, 3000, 5000):
            with self.subTest(balance=balance):
                p = plan_sequence(balance, TODAY + _dt.timedelta(days=36), lots, now=NOW)
                self.assertLessEqual(p.marginal_ypp, p.best_lot_ypp + 1e-9)

    def test_capacity_and_dead_point_add_up(self):
        for balance in (0, 50, 137, 1300, 1500, 4000):
            with self.subTest(balance=balance):
                p = plan_sequence(balance, TODAY + _dt.timedelta(days=36),
                                  _real_lots(), now=NOW)
                self.assertEqual(p.capacity_point + p.dead_point, balance)

    def test_no_attribution_anomaly_on_real_pool(self):
        for balance in (100, 500, 1500, 5000):
            with self.subTest(balance=balance):
                p = plan_sequence(balance, TODAY + _dt.timedelta(days=36),
                                  _real_lots(), now=NOW)
                self.assertFalse([n for n in p.notes if "归因异常" in n])
                self.assertEqual(p.lost_cents, 0)

    def test_leftover_and_spent_add_up(self):
        for balance in (0, 137, 1500):
            with self.subTest(balance=balance):
                p = plan_sequence(balance, TODAY + _dt.timedelta(days=36),
                                  _real_lots(), now=NOW)
                self.assertEqual(p.spent + p.leftover, balance)

    def test_zero_balance(self):
        p = plan_sequence(0, TODAY + _dt.timedelta(days=36), _real_lots(), now=NOW)
        self.assertEqual(p.picks, [])
        self.assertEqual(p.salvaged_cents, 0)
        self.assertEqual(p.marginal_ypp, 0.0)

    def test_expired_balance(self):
        p = plan_sequence(1500, TODAY - _dt.timedelta(days=1), _real_lots(), now=NOW)
        self.assertEqual(p.picks, [])
        self.assertLess(p.days_left, 0)
        self.assertTrue(any("已过期" in n for n in p.notes))

    def test_no_usable_lots(self):
        lots = [_lot("零积分", 0, 690)]
        p = plan_sequence(500, TODAY + _dt.timedelta(days=36), lots, now=NOW)
        self.assertEqual(p.picks, [])
        self.assertFalse(p.feasible)
        self.assertEqual(p.leftover, 500)
        self.assertTrue(any("没有任何可执行的兑换标的" in n for n in p.notes))

    def test_start_by_is_within_window(self):
        p = plan_sequence(1500, TODAY + _dt.timedelta(days=36), _real_lots(), now=NOW)
        self.assertIsNotNone(p.start_by)
        self.assertGreaterEqual(p.start_by, TODAY)
        self.assertLessEqual(p.start_by, p.expiry)
        self.assertGreaterEqual(p.days_until_start, 0)

    def test_acting_later_salvages_less(self):
        """序贯视角的核心命题：拖到后面才动手，可挽回价值必然下降。"""
        lots = _real_lots(period_days=30, per_period=1, now=NOW)
        early = plan_sequence(1000, TODAY + _dt.timedelta(days=36), lots, now=NOW)
        lazy_now = NOW + _dt.timedelta(days=25)
        late = plan_sequence(1000, TODAY + _dt.timedelta(days=36), lots, now=lazy_now)
        self.assertGreater(early.salvaged_cents, late.salvaged_cents)
        self.assertGreater(early.capacity_available, late.capacity_available)

    def test_marginal_ypp_at_clamps(self):
        p = plan_sequence(1500, TODAY + _dt.timedelta(days=36), _real_lots(), now=NOW)
        self.assertEqual(p.marginal_ypp_at(0), 0.0)
        self.assertEqual(p.marginal_ypp_at(-5), 0.0)
        huge = p.marginal_ypp_at(10 ** 9)
        self.assertAlmostEqual(huge, p.marginal_ypp_at(p.balance), places=9)
        # N 涨到余额时，区间平均退化为累计平均 —— 这是口径自洽性的一个硬校验。
        self.assertAlmostEqual(huge, p.average_ypp, places=9)
        # 而「最后一份」的后向边际与它不同：前者问"花掉它损失什么"，后者问"整批摊平多少"。
        self.assertAlmostEqual(
            p.marginal_ypp,
            (p._best[p.balance] - p._best[p.balance - 50]) / 50.0 / 100.0, places=9)

    def test_marginal_ypp_at_is_interval_average_not_point_diff(self):
        """回归：单点差分在整数背包里是块状的，会给出 6.9 元/积分这种荒谬费率。"""
        lots = [_lot("月限两次", 50, 690, period_days=30, per_period=1)]
        p = plan_sequence(1500, TODAY + _dt.timedelta(days=36), lots, now=NOW)
        self.assertLessEqual(p.marginal_ypp_at(50), p.best_lot_ypp + 1e-9)
        self.assertLessEqual(p.marginal_ypp, p.best_lot_ypp + 1e-9)
        self.assertLessEqual(p.next_ypp, p.best_lot_ypp + 1e-9)

    def test_curve_is_prefix_average(self):
        p = plan_sequence(1500, TODAY + _dt.timedelta(days=36), _real_lots(), now=NOW)
        self.assertTrue(p.curve)
        for upto, avg, _marg in p.curve:
            with self.subTest(upto=upto):
                self.assertAlmostEqual(avg, p.average_ypp_at(upto), places=9)
                self.assertLessEqual(upto, p.balance)

    def test_daily_rate(self):
        p = plan_sequence(1500, TODAY + _dt.timedelta(days=36), _real_lots(), now=NOW)
        self.assertAlmostEqual(p.daily_rate, p.spent / p.days_left, places=6)

    def test_to_dict_is_json_ready(self):
        import json
        p = plan_sequence(1500, TODAY + _dt.timedelta(days=36), _real_lots(), now=NOW)
        blob = json.dumps(p.to_dict(), ensure_ascii=False)
        back = json.loads(blob)
        self.assertEqual(back["balance"], 1500)
        self.assertEqual(back["capacity"]["point"], p.capacity_point)
        self.assertEqual(len(back["curve"]), len(p.curve))

    def test_repeat_is_deterministic(self):
        a = plan_sequence(1500, TODAY + _dt.timedelta(days=36), _real_lots(), now=NOW)
        b = plan_sequence(1500, TODAY + _dt.timedelta(days=36), _real_lots(), now=NOW)
        self.assertEqual(a.to_dict(), b.to_dict())


class TestPrintSequence(unittest.TestCase):
    """回归：``print_sequence`` 曾在边际曲线全零时 ZeroDivisionError。"""

    def _capture(self, p):
        buf = io.StringIO()
        with redirect_stdout(buf):
            print_sequence(p)
        return buf.getvalue()

    def test_empty_plan_does_not_crash(self):
        """余额买不起池子里最小的一份 ⇒ picks 为空，走的是纯 notes 分支。"""
        p = plan_sequence(100, TODAY + _dt.timedelta(days=36),
                          [_lot("太贵", 200, 2180)], now=NOW)
        self.assertEqual(p.picks, [])
        self.assertEqual(p.capacity_point, 0)
        self.assertEqual(p.dead_point, 100)
        out = self._capture(p)
        self.assertIn("换不出任何额外价值", out)
        self.assertIn("边际效率", out)

    def test_truly_unusable_pool_does_not_crash(self):
        p = plan_sequence(500, TODAY + _dt.timedelta(days=36),
                          [_lot("零积分", 0, 690)], now=NOW)
        out = self._capture(p)
        self.assertIn("没有任何可执行的兑换标的", out)

    def test_zero_balance_does_not_crash(self):
        p = plan_sequence(0, TODAY + _dt.timedelta(days=36), _real_lots(), now=NOW)
        self._capture(p)

    def test_normal_plan_prints_every_section(self):
        p = plan_sequence(1500, TODAY + _dt.timedelta(days=36), _real_lots(), now=NOW)
        out = self._capture(p)
        for token in ("建议兑换", "影子价格", "最优标的效率", "余额处平均效率",
                      "边际", "有效兑换容量", "效率断崖", "最晚开始日"):
            with self.subTest(token=token):
                self.assertIn(token, out)

    def test_columns_line_up(self):
        """表格必须按**显示宽度**对齐（中文占两列），不是按字符数。"""
        from termtext import dwidth
        p = plan_sequence(1500, TODAY + _dt.timedelta(days=36), _real_lots(), now=NOW)
        out = self._capture(p)
        rows = [ln for ln in out.splitlines() if "元/积分   ←" in ln]
        self.assertTrue(rows)
        widths = {dwidth(ln.split("元/积分")[0]) for ln in rows}
        self.assertEqual(len(widths), 1, f"列未对齐：{sorted(widths)}")


class TestLotsFromOffers(unittest.TestCase):
    def _offer(self, **kw):
        base = dict(spu_id="1", name="标的", point=100, face_value_cents=1090,
                    status=2, up_time="2026-01-01 00:00:00",
                    down_time="2026-12-30 23:59:59")
        base.update(kw)
        return RedemptionOffer(**base)

    def test_down_time_is_a_string_and_gets_parsed(self):
        """回归：``down_time`` 是字符串，直接与 date 比较会 TypeError。"""
        o = self._offer()
        self.assertIsInstance(o.down_time, str)
        self.assertEqual(window_state(o, NOW), WINDOW_ACTIVE)
        [lot] = lots_from_offers([o], now=NOW)
        self.assertIsInstance(lot.window_end, _dt.date)
        self.assertEqual(lot.window_end, _dt.date(2026, 12, 30))
        self.assertEqual(lot.value_cents, 1090)

    def test_past_window_excluded_when_only_active(self):
        o = self._offer(down_time="2026-01-01 00:00:00")
        self.assertEqual(lots_from_offers([o], now=NOW), [])
        self.assertEqual(len(lots_from_offers([o], now=NOW, only_active=False)), 1)

    def test_unpriced_offers_skipped(self):
        self.assertEqual(lots_from_offers([self._offer(point=0)], now=NOW), [])
        self.assertEqual(lots_from_offers([self._offer(face_value_cents=0)], now=NOW), [])

    def test_throttle_is_explicit_not_invented(self):
        [lot] = lots_from_offers([self._offer()], period_days=7, per_period=2, now=NOW)
        self.assertEqual((lot.period_days, lot.per_period), (7, 2))


class TestShippedData(unittest.TestCase):
    """真实标的池上的端到端结论 —— 把"高估"这条论断钉在实测数据上。"""

    @classmethod
    def setUpClass(cls):
        refs = {k: int(v) for k, v in _load(REFS).items()
                if not str(k).startswith("_") and isinstance(v, (int, float))}
        cls.ypp = build_value_matrix(_load_offers(PRODUCTS, refs)).benchmark_ypp
        cls.lots = lots_from_offers(_load_offers(PRODUCTS, refs), now=NOW)

    def test_pool_is_non_empty(self):
        self.assertGreater(len(self.lots), 3)

    def test_best_lot_matches_published_benchmark(self):
        p = plan_sequence(100, TODAY + _dt.timedelta(days=36), self.lots, now=NOW)
        self.assertAlmostEqual(p.best_lot_ypp, self.ypp, places=4)

    def test_large_balance_is_over_estimated(self):
        p = plan_sequence(1500, TODAY + _dt.timedelta(days=36), self.lots, now=NOW)
        self.assertGreater(p.ypp_gap, 0.10)
        self.assertLess(p.average_ypp, p.best_lot_ypp)
        self.assertEqual(p.suggested_shadow_ypp, p.marginal_ypp)


if __name__ == "__main__":
    unittest.main(verbosity=2)
