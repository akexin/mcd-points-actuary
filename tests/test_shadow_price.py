#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""影子价格动态化 / 保守分位估计的单元测试。

这个模块的全部风险集中在一条上：**在没有历史的时候假装有历史**。
所以下面的断言里，关于"样本不足必须退化并声明"的那几条是最重要的。
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from types import SimpleNamespace

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

from shadow_price import (  # noqa: E402
    DEFAULT_HISTORY, MODE_CONSERVATIVE, MODE_CURRENT, ShadowSeries, ShadowSnapshot,
    estimate, median, quantile, snapshot_from_matrix, slope,
)


def _offer(ypp: float):
    return SimpleNamespace(yuan_per_point=ypp)


def _matrix(active, ranked=None):
    return SimpleNamespace(active=[_offer(x) for x in active],
                           ranked=[_offer(x) for x in (ranked or active)])


class TestStatistics(unittest.TestCase):
    def test_quantile_linear_interpolation(self):
        self.assertEqual(quantile([1, 2, 3, 4], 0.0), 1.0)
        self.assertEqual(quantile([1, 2, 3, 4], 1.0), 4.0)
        self.assertEqual(quantile([1, 2, 3, 4], 0.25), 1.75)
        self.assertEqual(quantile([1, 2, 3, 4], 0.5), 2.5)

    def test_quantile_degenerate_inputs(self):
        self.assertEqual(quantile([], 0.25), 0.0)
        self.assertEqual(quantile([7.0], 0.25), 7.0)
        self.assertEqual(quantile([5, 1, 3], 0.5), 3.0)      # 内部排序

    def test_median_matches_quantile_half(self):
        xs = [0.1380, 0.0274, 0.0695, 0.3760]
        self.assertEqual(median(xs), quantile(xs, 0.5))

    def test_slope_sign(self):
        self.assertLess(slope([0, 1, 2, 3], [0.4, 0.3, 0.2, 0.1]), 0)
        self.assertGreater(slope([0, 1, 2, 3], [0.1, 0.2, 0.3, 0.4]), 0)
        self.assertEqual(slope([0], [0.1]), 0.0)
        self.assertEqual(slope([1, 1], [0.1, 0.2]), 0.0)      # x 无方差


class TestSnapshot(unittest.TestCase):
    def test_snapshot_from_matrix_uses_active_window(self):
        """主口径取**周期内**（active）最优；全库最差只作参考。"""
        m = _matrix(active=[0.1380, 0.1090, 0.0358],
                    ranked=[0.3760, 0.1380, 0.1090, 0.0358, 0.0274])
        s = snapshot_from_matrix(m, store_code="1990346", now=_dt(2026, 10, 9))
        self.assertEqual(s.best_ypp, 0.1380)
        self.assertEqual(s.worst_ypp, 0.0358)      # 只看周期内
        self.assertEqual(s.median_ypp, 0.1090)     # 全库中位
        self.assertEqual(s.active_offers, 3)
        self.assertEqual(s.priced_offers, 5)
        self.assertEqual(s.store_code, "1990346")
        self.assertEqual(s.date, "2026-10-09")

    def test_snapshot_falls_back_to_ranked_when_no_active(self):
        m = _matrix(active=[], ranked=[0.2, 0.1])
        s = snapshot_from_matrix(m)
        self.assertEqual(s.best_ypp, 0.2)
        self.assertEqual(s.worst_ypp, 0.1)

    def test_roundtrip_dict(self):
        s = ShadowSnapshot(date="2026-10-09", best_ypp=0.138, median_ypp=0.0695,
                           worst_ypp=0.0274, active_offers=7, priced_offers=20,
                           store_code="1990346", source="test")
        self.assertEqual(ShadowSnapshot.from_dict(s.to_dict()).key(), s.key())


class TestSeries(unittest.TestCase):
    def test_append_dedupes_same_day_same_store(self):
        ser = ShadowSeries()
        a = ShadowSnapshot(date="2026-10-09", best_ypp=0.1300, store_code="1990346")
        b = ShadowSnapshot(date="2026-10-09", best_ypp=0.1400, store_code="1990346")
        self.assertTrue(ser.append(a))
        self.assertFalse(ser.append(b))                 # 同日重跑 → 覆盖，不新增
        self.assertEqual(len(ser.snapshots), 1)
        self.assertEqual(ser.snapshots[0].best_ypp, 0.1400)

    def test_different_store_or_day_both_kept(self):
        ser = ShadowSeries()
        ser.append(ShadowSnapshot(date="2026-10-09", best_ypp=0.1, store_code="A"))
        ser.append(ShadowSnapshot(date="2026-10-09", best_ypp=0.2, store_code="B"))
        ser.append(ShadowSnapshot(date="2026-10-10", best_ypp=0.3, store_code="A"))
        self.assertEqual(len(ser.snapshots), 3)

    def test_sorted_is_chronological(self):
        ser = ShadowSeries()
        for d, v in (("2026-10-11", 0.1), ("2026-10-09", 0.2), ("2026-10-10", 0.3)):
            ser.append(ShadowSnapshot(date=d, best_ypp=v))
        self.assertEqual([s.date for s in ser.sorted()],
                         ["2026-10-09", "2026-10-10", "2026-10-11"])

    def test_save_load_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "h.json")
            ser = ShadowSeries()
            ser.append(ShadowSnapshot(date="2026-10-09", best_ypp=0.138,
                                      median_ypp=0.0695, worst_ypp=0.0274,
                                      active_offers=7, priced_offers=20,
                                      store_code="1990346"))
            ser.save(path)
            back = ShadowSeries.load(path)
            self.assertEqual(len(back.snapshots), 1)
            self.assertEqual(back.snapshots[0].best_ypp, 0.138)
            self.assertEqual(back.snapshots[0].active_offers, 7)
            with open(path, encoding="utf-8") as f:
                self.assertIn("_note", json.load(f))     # 文件自带来源说明

    def test_load_missing_file_is_empty(self):
        self.assertEqual(ShadowSeries.load("/definitely/not/here.json").snapshots, [])

    def test_load_accepts_bare_list(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "h.json")
            with open(path, "w", encoding="utf-8") as f:
                json.dump([{"date": "2026-10-09", "best_ypp": 0.13}], f)
            self.assertEqual(len(ShadowSeries.load(path).snapshots), 1)


class TestEstimate(unittest.TestCase):
    def _series(self, values):
        ser = ShadowSeries()
        for i, v in enumerate(values):
            ser.append(ShadowSnapshot(date=f"2026-10-{i + 1:02d}", best_ypp=v,
                                      store_code="S"))
        return ser

    def test_degraded_when_insufficient_samples(self):
        """最关键的一条：只有 1–2 次实测时，不许编造分位数。"""
        est = estimate(self._series([0.1380]), window=6, min_samples=3)
        self.assertTrue(est.degraded)
        self.assertEqual(est.conservative, est.point)         # 退化为单点
        self.assertIn("不假装有历史", est.note())
        self.assertEqual(est.samples, 1)

    def test_degraded_with_two_samples_too(self):
        est = estimate(self._series([0.1380, 0.1100]), min_samples=3)
        self.assertTrue(est.degraded)
        self.assertEqual(est.conservative, est.point)

    def test_conservative_is_p25_of_window(self):
        est = estimate(self._series([0.100, 0.120, 0.140, 0.160]), min_samples=3)
        self.assertFalse(est.degraded)
        self.assertEqual(est.point, 0.160)                    # 主口径 = 最新
        self.assertAlmostEqual(est.conservative, quantile([0.1, 0.12, 0.14, 0.16], 0.25))
        self.assertLess(est.conservative, est.point)

    def test_conservative_never_above_point(self):
        """保守值**永远不高于**今天实测的最优效率。

        这是个真实踩到的坑：序列 [0.16, 0.15, 0.14, 0.13, 0.12] 陡降时，
        近窗 P25 = 0.13 **高于**最新值 0.12 —— 分位兜底根本挡不住"刚发生的下跌"。
        所以估计里再加一次 min，并把 clamp 记录下来。构造上恒成立，故对
        随机序列也做一次抽查。
        """
        import random
        rng = random.Random(20261009)
        for _ in range(200):
            vals = [round(rng.uniform(0.01, 0.4), 4) for _ in range(rng.randint(3, 8))]
            est = estimate(self._series(vals), min_samples=3)
            self.assertLessEqual(est.conservative, est.point + 1e-12, vals)
            self.assertLessEqual(est.low, est.conservative + 1e-12, vals)

    def test_clamp_is_reported_on_steep_drop(self):
        est = estimate(self._series([0.160, 0.150, 0.140, 0.130, 0.120]), min_samples=3)
        self.assertTrue(est.clamped)
        self.assertEqual(est.conservative, est.point)
        self.assertIn("压回最新值", est.note())
        self.assertLess(est.trend, 0)                          # 确实在下降

    def test_p25_kept_when_market_is_flat_or_rising(self):
        est = estimate(self._series([0.100, 0.120, 0.140, 0.160]), min_samples=3)
        self.assertFalse(est.clamped)
        self.assertLess(est.conservative, est.point)

    def test_window_limits_samples(self):
        ser = self._series([0.01, 0.02, 0.03, 0.04, 0.05, 0.06, 0.07, 0.08])
        est = estimate(ser, window=3, min_samples=2)
        self.assertEqual(est.samples, 3)
        self.assertEqual(est.dates, ["2026-10-06", "2026-10-07", "2026-10-08"])

    def test_range_and_spread(self):
        est = estimate(self._series([0.05, 0.10, 0.20]), min_samples=2)
        self.assertEqual(est.low, 0.05)
        self.assertEqual(est.high, 0.20)
        self.assertEqual(est.spread_ratio, 4.0)

    def test_value_dispatch(self):
        est = estimate(self._series([0.05, 0.10, 0.20]), min_samples=2)
        self.assertEqual(est.value(MODE_CURRENT), est.point)
        self.assertEqual(est.value(MODE_CONSERVATIVE), est.conservative)
        self.assertEqual(est.value("nonsense"), est.point)    # 未知模式回落到主口径

    def test_mode_labels_exist(self):
        from shadow_price import MODE_TEXT
        self.assertIn(MODE_CURRENT, MODE_TEXT)
        self.assertIn(MODE_CONSERVATIVE, MODE_TEXT)

    def test_empty_series(self):
        est = estimate(ShadowSeries())
        self.assertTrue(est.degraded)
        self.assertEqual(est.point, 0.0)
        self.assertEqual(est.value(MODE_CONSERVATIVE), 0.0)

    def test_to_dict_json_safe(self):
        est = estimate(self._series([0.05, 0.10, 0.20]), min_samples=2)
        self.assertIn("note", json.loads(json.dumps(est.to_dict())))


class TestShippedHistory(unittest.TestCase):
    """仓库里那份实测历史必须能被解析，且主口径与线上已公布数字一致。"""

    def test_real_history_matches_published_number(self):
        ser = ShadowSeries.load(DEFAULT_HISTORY)
        self.assertTrue(ser.snapshots, "data/shadow_price_history.json 不应为空")
        est = estimate(ser, window=6)
        self.assertAlmostEqual(est.point, 0.1380, places=4)
        self.assertTrue(est.degraded, "目前只有 1 条实测 → 必须标记为退化")

    def test_real_history_records_active_count(self):
        ser = ShadowSeries.load(DEFAULT_HISTORY)
        snap = ser.sorted()[-1]
        self.assertEqual(snap.store_code, "1990346")
        self.assertEqual(snap.active_offers, 7)


def _dt(y, m, d):
    import datetime
    return datetime.datetime(y, m, d, 12, 0, 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
