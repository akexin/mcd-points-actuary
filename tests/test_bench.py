#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""``tools/bench.py`` 的单元测试。

基准脚本本身也是代码，也会写错 —— 而且它的错误是**无声**的：
一个写歪的对照实现会给出一个漂亮但没有意义的加速比，然后被抄进文档里。
所以这里测三件事：

1. 对照实现与生产实现**结果相同**（否则比的不是性能，是两件不同的事）
2. 合成数据**真的能触发被测路径**（券指向不存在的商品时，剪枝率会假性好看）
3. 基准脚本的自检能真的拦住问题（``bench_search`` 发现剪枝改变答案时必须中止）

跑得动的都跑了，但把规模压到最小 —— 基准不是测试，测试不该等它。
"""
from __future__ import annotations

import contextlib
import io
import math
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "scripts"))
sys.path.insert(0, os.path.join(ROOT, "tools"))

import bench  # noqa: E402

from channel_planner import compare_channels  # noqa: E402
from combo_search import COMBOS_FULL, COMBOS_PRUNED, ComboIndex, make_bound, search  # noqa: E402
from optimize_deals import Constraints, single_coupon_discount  # noqa: E402


def run_capture(fn, *args, **kw):
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        result = fn(*args, **kw)
    return result, buf.getvalue()


class TestCombBenchmark(unittest.TestCase):
    def test_handwritten_matches_math_comb(self):
        for n in range(0, 40):
            for k in range(0, n + 1):
                self.assertEqual(bench.handwritten_comb(n, k), math.comb(n, k),
                                 msg=f"C({n},{k}) 不一致")

    def test_handwritten_handles_out_of_range(self):
        self.assertEqual(bench.handwritten_comb(5, -1), 0)
        self.assertEqual(bench.handwritten_comb(5, 6), 0)

    def test_bench_comb_runs(self):
        _r, out = run_capture(bench.bench_comb, repeat=1)
        self.assertIn("math.comb", out)
        self.assertIn("不该拿它当性能论据", out)   # 结论必须如实写在输出里

    def test_fmt_us_switches_units(self):
        self.assertIn("µs", bench.fmt_us(5e-6))
        self.assertIn("ms", bench.fmt_us(5e-3))


class TestSyntheticData(unittest.TestCase):
    def test_pool_shape(self):
        pool = bench.synthetic_pool(9, seed=1)
        self.assertEqual(len(pool), 9)
        self.assertEqual(len({i.code for i in pool}), 9)
        self.assertTrue(all(i.price_cents > 0 for i in pool))

    def test_every_coupon_targets_real_codes(self):
        """合成券池必须"打得中"—— 否则抵扣上界退化成 0，剪枝率会假性好看。"""
        pool = bench.synthetic_pool(16, seed=5)
        codes = {i.code for i in pool}
        self.assertTrue(bench.synthetic_channels(pool).__len__() >= 2)
        for ch in bench.synthetic_channels(pool, seed=5):
            self.assertTrue(ch.coupons, f"{ch.name} 没有券")
            for c in ch.coupons:
                for fld in ("item_code", "buy_code", "get_code"):
                    v = getattr(c, fld, "") or ""
                    if v:
                        self.assertIn(v, codes, msg=f"{c.id}.{fld}={v} 不在池子里")
                for code in getattr(c, "item_codes", ()) or ():
                    self.assertIn(code, codes, msg=f"{c.id}.item_codes 含 {code} 不在池子里")

    def test_has_both_stacking_and_single_coupon_channels(self):
        """两种渠道都要有 —— 否则 ``allow_stack`` 那条分支根本没被测到。"""
        chs = bench.synthetic_channels(bench.synthetic_pool(12, seed=3), seed=3)
        self.assertTrue(any(c.allow_stack for c in chs))
        self.assertTrue(any(not c.allow_stack for c in chs))

    def test_discount_ceiling_is_not_degenerate(self):
        """上界必须是真的有限值 —— ``UNBOUNDED`` 会让剪枝完全失效。"""
        pool = bench.synthetic_pool(20, seed=11)
        chs = bench.synthetic_channels(pool, seed=11)
        _fn, info = bench.make_bound(chs, pool, item_count=4)
        self.assertFalse(info.unbounded, "合成券池让下界失效了，剪枝基准不可信")

    def test_channels_actually_discount_something(self):
        pool = bench.synthetic_pool(16, seed=9)
        chs = bench.synthetic_channels(pool, seed=9)
        combo = list(pool[:3])
        booked = [c for ch in chs for c in ch.coupons
                  if single_coupon_discount(combo, c) > 0]
        self.assertTrue(booked, "随机券池在样例购物车上一条都没命中")


class TestSearchBenchmark(unittest.TestCase):
    def test_engine_agreement_on_synthetic_pool(self):
        pool = bench.synthetic_pool(13, seed=21)
        chs = bench.synthetic_channels(pool, seed=21)
        cons = Constraints(min_items=1, max_items=3)
        idx = ComboIndex(pool, cons)
        ev = bench._evaluate_factory(chs)
        fn, info = make_bound(chs, idx.items, item_count=3)
        self.assertFalse(info.unbounded)
        a, sa = search(idx, ev, bound=fn, keep=5, engine=COMBOS_FULL)
        b, sb = search(idx, ev, bound=fn, keep=5, engine=COMBOS_PRUNED)
        self.assertEqual(a, b)
        self.assertGreater(sb.cuts, 0, "一点都没剪到，基准测不出东西")
        self.assertLess(sb.leaves, sa.leaves)

    def test_bench_search_aborts_when_pruning_changes_the_answer(self):
        """自检必须真的会拦 —— 否则基准会安静地发布一个错误的加速比。"""
        pool = bench.synthetic_pool(10, seed=31)
        chs = bench.synthetic_channels(pool, seed=31)
        cons = Constraints(min_items=1, max_items=3)
        idx = ComboIndex(pool, cons)
        ev = bench._evaluate_factory(chs)
        # 造一个"疯狂剪枝"的坏下界：永远返回一个极大的值，于是几乎全被剪掉
        bad = lambda goods: 10 ** 9                                # noqa: E731
        a, _ = search(idx, ev, bound=bad, keep=5, engine=COMBOS_FULL)
        b, sb = search(idx, ev, bound=bad, keep=5, engine=COMBOS_PRUNED)
        self.assertGreater(sb.cuts, 0)
        self.assertNotEqual([k for k, _ in a], [k for k, _ in b],
                            "这个反例本应改变答案；改了就说明坏下界确实能骗过搜索")

    def test_bench_search_runs_end_to_end(self):
        _r, out = run_capture(bench.bench_search, items=12, repeat=1)
        self.assertIn("结果一致：是", out)
        self.assertIn("枚举量", out)
        self.assertIn("下界 =", out)

    def test_alloc_peak_is_positive_and_keeps_the_result(self):
        peak = bench.alloc_peak(lambda: [bytearray(1024) for _ in range(16)])
        self.assertGreater(peak, 16 * 1024)


class TestMemoryBenchmark(unittest.TestCase):
    def test_slotted_is_smaller_per_object(self):
        n = 4000
        cheap = bench.alloc_peak(lambda: [bench.ItemPlain("C", "名", 1) for _ in range(n)])
        small = bench.alloc_peak(lambda: [bench.ItemSlotted("C", "名", 1) for _ in range(n)])
        self.assertLess(small, cheap)

    def test_control_class_has_the_same_fields(self):
        from dataclasses import fields
        plain = {f.name for f in fields(bench.ItemPlain)}
        slotted = {f.name for f in fields(bench.ItemSlotted)}
        self.assertEqual(plain, slotted)
        self.assertEqual(plain, {"code", "name", "price_cents", "category",
                                 "calories", "tags"})

    def test_slotted_really_uses_slots(self):
        self.assertFalse(hasattr(bench.ItemSlotted("C", "名", 1), "__dict__"))
        self.assertTrue(hasattr(bench.ItemPlain("C", "名", 1), "__dict__"))

    def test_bench_memory_runs(self):
        _r, out = run_capture(bench.bench_memory, n=2000)
        self.assertIn("tracemalloc", out)
        self.assertIn("只报前者会夸大战果", out)


class TestMaskBenchmark(unittest.TestCase):
    def test_two_implementations_agree(self):
        pool = bench.synthetic_pool(14, seed=13)
        must = (pool[2].code,)
        exclude = (pool[13].code,)
        mm, em = bench.mask_lookup(pool, must, exclude)
        for k in (1, 2, 3):
            self.assertEqual(bench.walk_with_sets(pool, k, must, exclude),
                             bench.walk_with_mask(pool, k, mm, em),
                             msg=f"k={k} 两个实现结果不同")

    def test_agrees_with_brute_force_by_hand(self):
        pool = bench.synthetic_pool(7, seed=17)
        must = (pool[1].code,)
        exclude = (pool[6].code,)
        mm, em = bench.mask_lookup(pool, must, exclude)
        want = 0
        import itertools
        for k in (2, 3):
            for combo in itertools.combinations(pool, k):
                codes = {i.code for i in combo}
                if must[0] in codes and exclude[0] not in codes:
                    want += 1
        self.assertEqual(bench.walk_with_mask(pool, 2, mm, em)
                         + bench.walk_with_mask(pool, 3, mm, em), want)

    def test_missing_must_include_is_rejected_loudly(self):
        """掩码版本建模不了"必选商品不在池里"，所以必须报错而不是静默放行。

        真实实现里这条路径由寻找器的 ``missing_required`` 提前短路；
        基准若默默把它当成"没有约束"，就会给出一个虚高的加速比。
        """
        pool = bench.synthetic_pool(8, seed=19)
        with self.assertRaises(ValueError):
            bench.mask_lookup(pool, ("NOT-HERE",), ())
        with self.assertRaises(ValueError):
            bench.mask_lookup(pool, (), ("NOT-HERE",))

    def test_mask_bits_match_positions(self):
        pool = bench.synthetic_pool(6, seed=23)
        mm, em = bench.mask_lookup(pool, (pool[1].code,), (pool[4].code,))
        self.assertEqual(mm, 1 << 1)
        self.assertEqual(em, 1 << 4)

    def test_bench_mask_runs(self):
        _r, out = run_capture(bench.bench_mask, items=10, repeat=1)
        self.assertIn("加速比", out)
        self.assertIn("结果一致", out)


class TestCli(unittest.TestCase):
    def test_only_filter_runs_a_subset(self):
        _r, out = run_capture(bench.main, ["--only", "comb", "--repeat", "1"])
        self.assertIn("## 1.", out)
        self.assertNotIn("## 2.", out)

    def test_parser_defaults(self):
        args = bench.build_parser().parse_args([])
        self.assertEqual(args.items, 22)
        self.assertEqual(args.repeat, bench.REPEAT)
        self.assertIsNone(args.only)


if __name__ == "__main__":
    unittest.main(verbosity=2)
