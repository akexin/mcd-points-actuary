#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""抽奖蒙特卡洛回测的单元测试。

最重要的一组是 :class:`TestAnalyticCrossCheck`：``uniform`` 与 ``pessimistic``
两种概率假设**有闭式解**（就是 ``analyze_lottery`` 里的全池等概率期望与悲观期望），
所以模拟器的均值必须与它们对上 —— 这是唯一能证明"模拟没写错"的硬校验。
"""
from __future__ import annotations

import json
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

from lottery_mc import (  # noqa: E402
    A_LONG_TAIL, A_OPTIMISTIC, A_PESSIMISTIC, A_UNIFORM, ASSUMPTIONS, ASSUMPTION_TEXT,
    MonteCarloResult, StressTest, load_prizes, monte_carlo_lottery, stress_test,
    weights_for,
)
from points_engine import (  # noqa: E402
    Prize, _load, _load_offers, analyze_lottery, build_value_matrix,
)

DATA = os.path.join(os.path.dirname(__file__), "..", "data")
LOTTERY = os.path.join(DATA, "sample_lottery.json")
PRODUCTS = os.path.join(DATA, "sample_points_products.json")
REFS = os.path.join(DATA, "sample_reference_prices.json")

TRIALS = 20_000


def _real_pool():
    refs = {k: int(v) for k, v in _load(REFS).items()
            if not str(k).startswith("_") and isinstance(v, (int, float))}
    ypp = build_value_matrix(_load_offers(PRODUCTS, refs)).benchmark_ypp
    prizes, draw_point = load_prizes(LOTTERY, REFS)
    return prizes, draw_point, ypp


def _two_value_pool():
    """受控两值奖池：{¥1, ¥5} 等概率，打平线 ¥3 → 亏损概率应恰为 50%。"""
    prizes = [Prize(name="小奖", face_value_cents=100),
              Prize(name="大奖", face_value_cents=500)]
    return prizes, 3, 1.0        # draw_point=3, ypp=1.0 → required = 300 分


class TestAnalyticCrossCheck(unittest.TestCase):
    """模拟器正确性的硬校验：MC 均值必须收敛到解析值。"""

    def test_uniform_mean_matches_expected_if_uniform(self):
        prizes, draw_point, ypp = _real_pool()
        a = analyze_lottery(draw_point, prizes, ypp)
        mc = monte_carlo_lottery(prizes, draw_point, ypp, trials=TRIALS,
                                 assumption=A_UNIFORM)
        self.assertIsNotNone(mc.analytic_mean_cents)
        self.assertEqual(mc.analytic_mean_cents, a.expected_if_uniform_cents)
        self.assertLess(abs(mc.per_draw_mean_cents - a.expected_if_uniform_cents), 25)

    def test_pessimistic_mean_matches_expected_pessimistic(self):
        prizes, draw_point, ypp = _real_pool()
        a = analyze_lottery(draw_point, prizes, ypp)
        mc = monte_carlo_lottery(prizes, draw_point, ypp, trials=TRIALS,
                                 assumption=A_PESSIMISTIC)
        self.assertEqual(mc.analytic_mean_cents, a.expected_pessimistic_cents)
        self.assertLess(abs(mc.per_draw_mean_cents - a.expected_pessimistic_cents), 25)

    def test_assumptions_without_analytic_solution_leave_gap_none(self):
        prizes, draw_point, ypp = _real_pool()
        for a in (A_LONG_TAIL, A_OPTIMISTIC):
            mc = monte_carlo_lottery(prizes, draw_point, ypp, trials=2_000,
                                     assumption=a)
            self.assertIsNone(mc.analytic_mean_cents)
            self.assertIsNone(mc.analytic_gap_cents)

    def test_two_value_pool_loss_prob_is_exactly_half(self):
        """{¥1, ¥5} 等概率、打平 ¥3 → 亏损概率 50%（走大数定律，容差给足）。"""
        prizes, draw_point, ypp = _two_value_pool()
        mc = monte_carlo_lottery(prizes, draw_point, ypp, trials=TRIALS)
        self.assertAlmostEqual(mc.p_loss_single, 0.5, delta=0.02)
        self.assertAlmostEqual(mc.per_draw_mean_cents, 300.0, delta=10)

    def test_dominated_pool_loses_almost_surely(self):
        """奖池最高值 < 打平线 → 亏损概率必须是 1.0（必亏）。"""
        prizes = [Prize(name="a", face_value_cents=100), Prize(name="b", face_value_cents=200)]
        mc = monte_carlo_lottery(prizes, 5, 1.0, trials=3_000)   # required = 500
        self.assertEqual(mc.p_loss_single, 1.0)
        self.assertEqual(mc.p_loss_total, 1.0)


class TestWeights(unittest.TestCase):
    def test_uniform_weights_equal(self):
        self.assertEqual(weights_for([1, 2, 3], A_UNIFORM), [1.0, 1.0, 1.0])

    def test_pessimistic_zeroes_exactly_one_max(self):
        """必须只置零**一个**最高值 —— 与解析口径 values[:-1] 严格一致。"""
        w = weights_for([1, 5, 5], A_PESSIMISTIC)
        self.assertEqual(w, [1.0, 1.0, 0.0])
        self.assertEqual(sum(1 for x in w if x == 0), 1)

    def test_long_tail_is_decreasing(self):
        w = weights_for([10, 20, 30], A_LONG_TAIL)
        self.assertGreater(w[0], w[1])
        self.assertGreater(w[1], w[2])

    def test_optimistic_is_increasing(self):
        w = weights_for([10, 20, 30], A_OPTIMISTIC)
        self.assertLess(w[0], w[1])
        self.assertLess(w[1], w[2])

    def test_unknown_assumption_raises(self):
        with self.assertRaises(ValueError):
            weights_for([1, 2], "nope")

    def test_single_prize_pool_falls_back_to_uniform(self):
        """退化输入：只有一项奖品时，pessimistic 会把唯一权重置零。

        必须回落到均匀权重 —— 否则 ``random.choices`` 抛
        "Total of weights must be greater than zero"，整个回测直接崩。
        """
        self.assertEqual(weights_for([500], A_PESSIMISTIC), [1.0])
        mc = monte_carlo_lottery([Prize(name="唯一奖", face_value_cents=500)],
                                 1, 1.0, trials=1_000, assumption=A_PESSIMISTIC)
        self.assertEqual(mc.per_draw_mean_cents, 500.0)

    def test_pessimistic_single_highest_prize_removed_from_value(self):
        """两值奖池：pessimistic 的均值应恰为**低**的那一项。"""
        prizes = [Prize(name="低", face_value_cents=100),
                  Prize(name="高", face_value_cents=900)]
        mc = monte_carlo_lottery(prizes, 1, 1.0, trials=2_000,
                                 assumption=A_PESSIMISTIC)
        self.assertEqual(mc.per_draw_mean_cents, 100.0)


class TestReproducibility(unittest.TestCase):
    def test_same_seed_same_result(self):
        prizes, draw_point, ypp = _real_pool()
        a = monte_carlo_lottery(prizes, draw_point, ypp, trials=3_000, seed=42)
        b = monte_carlo_lottery(prizes, draw_point, ypp, trials=3_000, seed=42)
        self.assertEqual(a.per_draw_mean_cents, b.per_draw_mean_cents)
        self.assertEqual(a.total_median, b.total_median)
        self.assertEqual(a.p_loss_single, b.p_loss_single)
        self.assertEqual(a.histogram, b.histogram)

    def test_different_seed_still_converges(self):
        prizes, draw_point, ypp = _real_pool()
        a = monte_carlo_lottery(prizes, draw_point, ypp, trials=TRIALS, seed=1)
        b = monte_carlo_lottery(prizes, draw_point, ypp, trials=TRIALS, seed=2)
        self.assertLess(abs(a.per_draw_mean_cents - b.per_draw_mean_cents), 30)

    def test_invalid_arguments_rejected(self):
        prizes, draw_point, ypp = _real_pool()
        for kwargs in ({"trials": 0}, {"draws": 0}):
            with self.assertRaises(ValueError):
                monte_carlo_lottery(prizes, draw_point, ypp, **kwargs)
        with self.assertRaises(ValueError):
            monte_carlo_lottery(prizes, draw_point, ypp, assumption="bogus")


class TestDistribution(unittest.TestCase):
    def test_percentiles_are_monotone(self):
        prizes, draw_point, ypp = _real_pool()
        mc = monte_carlo_lottery(prizes, draw_point, ypp, trials=5_000, draws=5,
                                 seed=7)
        self.assertLessEqual(mc.total_worst, mc.total_p05)
        self.assertLessEqual(mc.total_p05, mc.total_p25)
        self.assertLessEqual(mc.total_p25, mc.total_median)
        self.assertLessEqual(mc.total_median, mc.total_p75)
        self.assertLessEqual(mc.total_p75, mc.total_p95)
        self.assertLessEqual(mc.total_p95, mc.total_best)

    def test_histogram_counts_sum_to_trials(self):
        prizes, draw_point, ypp = _real_pool()
        mc = monte_carlo_lottery(prizes, draw_point, ypp, trials=5_000, seed=3)
        self.assertEqual(sum(c for _, c in mc.histogram), 5_000)

    def test_total_mean_scales_with_draws(self):
        prizes, draw_point, ypp = _real_pool()
        one = monte_carlo_lottery(prizes, draw_point, ypp, trials=5_000, draws=1,
                                  seed=11)
        ten = monte_carlo_lottery(prizes, draw_point, ypp, trials=5_000, draws=10,
                                  seed=11)
        self.assertAlmostEqual(ten.total_mean_cents, one.per_draw_mean_cents * 10,
                               delta=one.per_draw_mean_cents)

    def test_more_draws_lowers_loss_probability(self):
        """多次抽奖的分散效应：合计亏损概率应随次数下降（本例期望为正）。"""
        prizes, draw_point, ypp = _real_pool()
        p1 = monte_carlo_lottery(prizes, draw_point, ypp, trials=5_000, draws=1,
                                 seed=5).p_loss_total
        p20 = monte_carlo_lottery(prizes, draw_point, ypp, trials=5_000, draws=20,
                                  seed=5).p_loss_total
        self.assertLess(p20, p1)

    def test_value_per_point_is_a_ypp(self):
        """实测口径的「每积分换回多少元」要能直接与影子价格比。"""
        prizes, draw_point, ypp = _real_pool()
        mc = monte_carlo_lottery(prizes, draw_point, ypp, trials=2_000)
        self.assertAlmostEqual(mc.value_per_point,
                               mc.per_draw_mean_cents / 100.0 / draw_point, places=9)


class TestStressTest(unittest.TestCase):
    def test_covers_all_assumptions(self):
        prizes, draw_point, ypp = _real_pool()
        st = stress_test(prizes, draw_point, ypp, trials=2_000)
        self.assertEqual([r.assumption for r in st.rows], list(ASSUMPTIONS))
        self.assertTrue(all(a in ASSUMPTION_TEXT for a in ASSUMPTIONS))

    def test_loss_prob_interval_is_ordered(self):
        prizes, draw_point, ypp = _real_pool()
        st = stress_test(prizes, draw_point, ypp, trials=2_000)
        self.assertLessEqual(st.min_loss_prob, st.max_loss_prob)
        self.assertGreaterEqual(st.min_loss_prob, 0.0)
        self.assertLessEqual(st.max_loss_prob, 1.0)

    def test_robust_win_when_even_worst_case_is_safe(self):
        """低打平线的奖池：连最不利假设都大概率不亏 → robust_win。"""
        st = stress_test([Prize(name="a", face_value_cents=1000)],
                         1, 0.01, trials=2_000)
        self.assertEqual(st.robust_verdict, "robust_win")
        self.assertIn("不依赖概率假设", st.robust_text)

    def test_robust_lose_when_even_best_case_loses(self):
        """高打平线的奖池：连最有利假设都大概率亏 → robust_lose。

        打平线 = 1 积分 × 2.0 元/积分 = ¥2.00 > 唯一的奖品 ¥1.00。
        """
        st = stress_test([Prize(name="a", face_value_cents=100)],
                         1, 2.0, trials=2_000)
        self.assertEqual(st.robust_verdict, "robust_lose")
        self.assertIn("不建议抽", st.robust_text)

    def test_depends_when_assumptions_disagree(self):
        st = stress_test([Prize(name="a", face_value_cents=100),
                          Prize(name="b", face_value_cents=1000)],
                         3, 1.0, trials=4_000)
        self.assertEqual(st.robust_verdict, "depends")
        self.assertIn("依赖概率假设", st.robust_text)

    def test_to_dict_json_safe(self):
        prizes, draw_point, ypp = _real_pool()
        st = stress_test(prizes, draw_point, ypp, trials=1_000)
        blob = json.loads(json.dumps(st.to_dict(), ensure_ascii=False))
        self.assertEqual(len(blob["rows"]), len(ASSUMPTIONS))
        self.assertIn("conclusion", blob)

    def test_empty_pool_is_handled(self):
        mc = monte_carlo_lottery([], 24, 0.138, trials=100)
        self.assertEqual(mc.per_draw_mean_cents, 0.0)
        self.assertEqual(mc.histogram, [])

    def test_defaults_are_stable(self):
        from lottery_mc import DEFAULT_SEED, DEFAULT_TRIALS
        self.assertEqual(DEFAULT_SEED, 20261009)
        self.assertGreaterEqual(DEFAULT_TRIALS, 10_000)


class TestShippedData(unittest.TestCase):
    def test_real_lottery_pool_parses(self):
        prizes, draw_point = load_prizes(LOTTERY, REFS)
        self.assertGreaterEqual(len(prizes), 5)
        self.assertEqual(draw_point, 24)

    def test_real_pool_stress_runs(self):
        prizes, draw_point, ypp = _real_pool()
        st = stress_test(prizes, draw_point, ypp, trials=3_000, draws=1)
        # 单次抽奖大概率有两位数百分比的亏损概率（这正是"期望值好看≠不亏"）
        self.assertGreater(st.rows[0].p_loss_single, 0.10)


if __name__ == "__main__":
    unittest.main(verbosity=2)
